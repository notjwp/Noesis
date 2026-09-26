"""First-run credentials, resolved before anything else reads config.

Not in `cli.py`, and the reason is mechanical rather than aesthetic:
`config.py` resolves every tunable at IMPORT time, so a wizard that runs after
`agent.config` has been imported cannot change PROVIDER, OPENAI_MODEL or
OPENAI_BASE_URL no matter what it writes. It runs from `agent/__main__.py`, in
the same window `.env` is loaded in, and the entry point reloads config after.

Nothing here imports `textual`. `run()` pulls the screen in inside the function
body, so a machine that reaches `needed() == False` never pays for it
(NFR-602), and everything a test needs to check - the probe's classification,
what gets written, what the key looks like when shown - is reachable without a
running app.
"""
import json
import os
import shutil
import sys
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# 120s x 6 attempts is sized for a working turn. A person waiting at a prompt
# is a different budget, and one they judge by their own patience.
PROBE_TIMEOUT = 20.0


@dataclass(frozen=True)
class Choice:
    provider: str
    model: str
    note: str


# Exactly what `provider.call_model` dispatches on, and nothing aspirational.
CHOICES = (
    Choice("nvidia", "nvidia/nemotron-3-super-120b-a12b",
           "default - every baseline in this repo"),
    Choice("nvidia", "nvidia/nemotron-3-ultra-550b-a55b",
           "4.6x larger; measured 10/18, the same as super"),
    Choice("anthropic", "claude-opus-5", ""),
    Choice("anthropic", "claude-sonnet-5", ""),
    Choice("anthropic", "claude-haiku-4-5-20251001", ""),
    Choice("openai", "", "custom - OpenRouter, Groq, OpenAI, a local Ollama"),
)

WARNING = ("changing the model invalidates every baseline in this repo - "
           "it is a different measurement.")

# The app cannot make its own window transparent; only the emulator can.
OPACITY_HINT = ("Windows Terminal: `noesis --terminal-profile` adds a "
                "transparent NOESIS profile. kitty background_opacity, WezTerm "
                "window_background_opacity, Alacritty opacity.")


# ----------------------------------------------------------- when it runs

def provider_name() -> str:
    """The provider as the environment has it, read late and never cached."""
    return (os.environ.get("AGENT_PROVIDER") or "nvidia").strip().lower()


def needed() -> bool:
    """Whether the RESOLVED provider has no key.

    Provider-specific because the keys are: an Anthropic key does not
    authenticate NVIDIA, so a wizard that only asked "is any key set" would
    skip itself on a machine that cannot make a single call.
    """
    from agent import config

    if provider_name() == "anthropic":
        return not (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    try:
        config.openai_api_key()
    except RuntimeError:
        return True
    return False


def _tty(stream) -> bool:
    """Whether a stream is a terminal, for streams that may not be one at all.

    pythonw, a service host and a closed pipe each hand over something without
    a usable isatty, and this runs on EVERY `python -m agent`.
    """
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def interactive() -> bool:
    """A terminal with a person at it.

    This is what keeps --worker, --channel, the scheduled tasks and the eval
    harness from ever blocking on a prompt; without a TTY they fall through to
    config.openai_api_key()'s existing, actionable RuntimeError.
    """
    return bool(_tty(sys.stdin) and _tty(sys.stdout)
                and not os.environ.get("AGENT_NO_SETUP"))


def run() -> bool:
    """Open the wizard; True if anything was saved."""
    from agent.ui.setup import SetupApp

    return bool(SetupApp(existing_key=current_key()).run())


def current_key() -> str:
    """Whatever key the resolved provider would use right now, or ""."""
    from agent import config

    if provider_name() == "anthropic":
        return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    try:
        return config.openai_api_key()
    except RuntimeError:
        return ""


# ----------------------------------------------------------------- probe

def mask(key: str) -> str:
    """Enough to recognise which key it is, never enough to use it."""
    key = (key or "").strip()
    if len(key) < 12:
        return "•" * 8
    return f"{key[:6]}{'•' * 8}{key[-4:]}"


def scrub(text: str, key: str) -> str:
    """A provider is free to quote the key back in its own error message, and
    that message is about to be rendered (NFR-203). Applied here AND at the
    render boundary, because a message can arrive from somewhere else."""
    return text.replace(key, mask(key)) if key else text


def probe(chosen: str, model: str, base_url: str, key: str) -> tuple[str, str]:
    """Ask the endpoint one question with the settings ABOUT to be saved.

    Costs roughly 40 tokens. The config module is set for the duration rather
    than written first and read back, because a wizard that verifies the
    configuration already on disk verifies nothing - and because a key that
    fails must never have been persisted.
    """
    from agent import config
    from agent import provider as api

    settings = ("PROVIDER", "MODEL", "OPENAI_MODEL", "OPENAI_BASE_URL",
                "REQUEST_TIMEOUT", "CALL_ATTEMPTS")
    before = {name: getattr(config, name) for name in settings}
    keys = {name: os.environ.get(name)
            for name in ("AGENT_API_KEY", "ANTHROPIC_API_KEY")}
    try:
        config.PROVIDER = chosen
        config.REQUEST_TIMEOUT = PROBE_TIMEOUT
        config.CALL_ATTEMPTS = 1
        if chosen == "anthropic":
            config.MODEL = model
            os.environ["ANTHROPIC_API_KEY"] = key
        else:
            config.OPENAI_MODEL = model
            # Blank means "the configured endpoint", which is right for the
            # named NVIDIA choices. The screen refuses a blank one for `custom`,
            # where it would verify one endpoint and then configure another.
            config.OPENAI_BASE_URL = base_url or config.OPENAI_BASE_URL
            os.environ["AGENT_API_KEY"] = key
        api.call_model([{"role": "user",
                         "content": [{"type": "text", "text": "say ok"}]}],
                       "Answer with one word.", [])
    except api.ProviderMisconfigured as exc:
        return "misconfigured", scrub(str(exc), key)
    except api.ProviderUnavailable as exc:
        return "unavailable", scrub(str(exc), key)
    except Exception:                                     # noqa: BLE001
        # Ours, and it stays loud. Excusing a real bug as a flaky endpoint is
        # how a wizard comes to save a key against a provider it never reached.
        return "error", scrub(traceback.format_exc(limit=3).strip()[-400:], key)
    finally:
        for name, value in before.items():
            setattr(config, name, value)
        for name, value in keys.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return "ok", ""


# ----------------------------------------------------------- persistence

def variables(chosen: str, model: str, base_url: str, key: str) -> dict[str, str]:
    """The names each provider is configured through."""
    if chosen == "anthropic":
        return {"AGENT_PROVIDER": "anthropic", "AGENT_MODEL": model,
                "ANTHROPIC_API_KEY": key}
    pairs = {"AGENT_PROVIDER": chosen, "OPENAI_MODEL": model,
             "AGENT_API_KEY": key}
    if chosen == "openai":
        pairs["OPENAI_BASE_URL"] = base_url
    return pairs


def write_env(pairs: dict[str, str], path: Path | None = None) -> None:
    """Persist to `.env` AND to this process, so nothing needs a restart.

    Replaces an existing line rather than appending a second: `_load_env` takes
    the FIRST occurrence and a real environment variable beats both, so a
    duplicate leaves a file whose behaviour does not match its last line.
    """
    path = path or ENV_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    left = dict(pairs)
    out = []
    for line in lines:
        name = line.split("=", 1)[0].strip() if "=" in line else ""
        out.append(f"{name}={left.pop(name)}" if name in left else line)
    out += [f"{name}={value}" for name, value in left.items()]
    # newline="" with an explicit \n, and values never quoted: this file is read
    # by Docker --env-file, which strips neither CRLF nor quotes.
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(out) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass                     # Windows has no mode bits; the write still stands
    os.environ.update(pairs)


# ------------------------------------------ the Windows Terminal profile

# A FRAGMENT, not an edit to the user's settings.json: Terminal merges every
# .json in this folder, so installing is one file and uninstalling is deleting
# it. https://learn.microsoft.com/en-us/windows/terminal/json-fragment-extensions
FRAGMENT_NS = uuid.UUID("{f65ddb7e-706b-4499-8a50-40313caf510a}")
APP_NAME = "Noesis"
PROFILE_NAME = "NOESIS"
PROFILE_OPACITY = 85


def _windows() -> bool:
    """Named so a test can answer it without touching `os.name` itself, which
    pathlib reads to decide whether a Path is a WindowsPath."""
    return os.name == "nt"


def profile_guid(app: str = APP_NAME, profile: str = PROFILE_NAME) -> str:
    """The GUID Terminal derives for a fragment profile, as the docs define it.

    Two nested v5 UUIDs, each name encoded UTF-16LE and decoded ASCII. Optional
    but "strongly encouraged": with it, reinstalling UPDATES the profile instead
    of adding a second one.
    """
    def name(text: str) -> str:
        return text.encode("UTF-16LE").decode("ASCII")

    within = uuid.uuid5(FRAGMENT_NS, name(app))
    return f"{{{uuid.uuid5(within, name(profile))}}}"


def launch_command() -> str:
    """How to start NOESIS, for a terminal that is not this process.

    The installed script when there is one, else this interpreter and -m, so a
    source checkout without an entry point still gets a working profile. Quoted
    only when it has to be: a bare path reads better in the settings UI.
    """
    found = shutil.which("noesis")
    if found:
        return f'"{found}"' if " " in found else found
    executable = sys.executable
    return f'"{executable}" -m agent' if " " in executable else f"{executable} -m agent"


def fragment(blurred: bool) -> dict:
    """The fragment's contents. Pure, so the tests drive this and not the disk.

    `name` is the one property a fragment profile MUST define. No `icon` - we
    ship no asset, and adjacent-file icons need Terminal 1.24 - and no
    `startingDirectory`, because NOESIS finds `.env` from its own file and the
    workspace from config, so the working directory decides nothing.
    """
    return {
        "profiles": [{
            "guid": profile_guid(),
            "name": PROFILE_NAME,
            "commandline": launch_command(),
            "opacity": PROFILE_OPACITY,
            "useAcrylic": bool(blurred),
        }],
    }


def fragment_path() -> Path | None:
    """Where the fragment belongs, or None where there is nowhere to put one.

    Under LOCALAPPDATA and NOT the Packages folder that holds the user's own
    settings.json - a per-user fragment for an app installed from the web.
    """
    local = os.environ.get("LOCALAPPDATA")
    if not _windows() or not local:
        return None
    return (Path(local) / "Microsoft" / "Windows Terminal" / "Fragments"
            / APP_NAME / "noesis.json")


def installed() -> bool:
    path = fragment_path()
    return bool(path and path.is_file())


def install_profile(blurred: bool) -> Path:
    """Write the fragment, creating the folder Terminal reads. Returns the path.

    UTF-8 explicitly: the docs name UTF-16LE as the way this silently fails.
    """
    path = fragment_path()
    if path is None:
        raise RuntimeError("a Windows Terminal fragment needs Windows and LOCALAPPDATA")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        json.dump(fragment(blurred), handle, indent=2)
        handle.write("\n")
    return path


def remove_profile() -> bool:
    """Delete the fragment. False when there was nothing to delete."""
    path = fragment_path()
    if path is None or not path.is_file():
        return False
    path.unlink()
    return True
