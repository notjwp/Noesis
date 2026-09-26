"""The first-run credential wizard.

The tenth stated deviation from the tests/ allowlist, on the bar §12 sets: a
credential boundary whose failures are SILENT. A key saved without being
verified surfaces twenty turns later as an auth error; a key echoed into a
trace surfaces never, until someone reads the file. Neither raises anything.

No network. `call_model` is faked in every test that reaches a provider, and
the probe's own classification is exercised through the real taxonomy in
`agent/provider.py` rather than a copy of it.
"""
import asyncio
import importlib
import json
import os
import pathlib
import subprocess
import sys

import pytest

from agent import provider, setup

ROOT = pathlib.Path(__file__).resolve().parent.parent

KEY = "nvapi-0123456789abcdefghij7f2c"
DEFAULT = setup.CHOICES[0]


def drive(app, script):
    """Run `script(pilot)` against a headless app, as tests/test_tui.py does."""
    async def _go():
        async with app.run_test(size=(100, 44)) as pilot:
            await pilot.pause()
            await script(pilot)
            await pilot.pause()
    asyncio.run(_go())


async def verify(app, pilot, key=KEY):
    """Type a key and press verify, then wait for the probe thread.

    `pilot.pause()` yields the event loop once; the probe runs on a worker, so
    waiting on the worker is what makes this deterministic rather than lucky.
    """
    app.screen.query_one("#key").value = key
    app.screen.query_one("#verify").press()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point the wizard at a throwaway .env. The real one holds live keys."""
    path = tmp_path / ".env"
    monkeypatch.setattr(setup, "ENV_FILE", path)
    return path


@pytest.fixture
def no_keys(monkeypatch):
    for name in ("AGENT_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY",
                 "ANTHROPIC_API_KEY", "AGENT_NO_SETUP"):
        monkeypatch.delenv(name, raising=False)


# ================================================================ when it runs

def test_needed_is_False_once_a_key_is_present(no_keys, monkeypatch):
    monkeypatch.setattr(setup, "provider_name", lambda: "nvidia")
    assert setup.needed() is True
    monkeypatch.setenv("AGENT_API_KEY", KEY)
    assert setup.needed() is False


def test_needed_reads_the_key_the_RESOLVED_provider_actually_uses(
        no_keys, monkeypatch):
    """An Anthropic key does not satisfy NVIDIA, and the reverse."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setattr(setup, "provider_name", lambda: "nvidia")
    assert setup.needed() is True
    monkeypatch.setattr(setup, "provider_name", lambda: "anthropic")
    assert setup.needed() is False


def test_a_blank_key_does_not_count_as_a_key(no_keys, monkeypatch):
    monkeypatch.setattr(setup, "provider_name", lambda: "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert setup.needed() is True


def test_interactive_is_False_without_a_tty(monkeypatch):
    monkeypatch.delenv("AGENT_NO_SETUP", raising=False)
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(setup.sys.stdout, "isatty", lambda: True, raising=False)
    assert setup.interactive() is False


def test_interactive_is_False_when_AGENT_NO_SETUP_is_set(monkeypatch):
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(setup.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("AGENT_NO_SETUP", "1")
    assert setup.interactive() is False


def test_a_headless_run_never_opens_the_wizard(tmp_path):
    """--worker, --channel, the scheduled tasks and the harness must never
    block on a prompt. A subprocess has no TTY, which is the whole guard, and a
    wizard that opened here would hang rather than fail.

    ANTHROPIC_API_KEY is set to blank rather than unset: `_load_env` skips a
    name already in the environment, so this is what stops the repo's own .env
    from supplying a key and making `needed()` False for the wrong reason.
    """
    env = {**os.environ, "AGENT_HOME": str(tmp_path),
           "AGENT_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": ""}
    env.pop("AGENT_NO_SETUP", None)
    done = subprocess.run([sys.executable, "-m", "agent", "--tasks"],
                          cwd=ROOT, capture_output=True, text=True,
                          timeout=90, env=env)
    assert done.returncode == 0, done.stderr[-500:]


def test_the_entry_points_reach_a_decision_with_textual_uninstalled():
    """`needed()` runs on every `python -m agent`, so it must cost nothing on a
    machine that has no interface installed at all (NFR-602)."""
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'textual' or name.startswith('textual.'):\n"
        "            raise ImportError('textual is not installed')\n"
        "sys.meta_path.insert(0, Block())\n"
        "from agent import setup\n"
        "setup.needed(); setup.interactive(); setup.current_key()\n"
        "assert 'textual' not in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_interactive_is_True_on_a_terminal(monkeypatch):
    monkeypatch.delenv("AGENT_NO_SETUP", raising=False)
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(setup.sys.stdout, "isatty", lambda: True, raising=False)
    assert setup.interactive() is True


# =================================================================== the probe

def fake_call(raises=None):
    def _call(messages, system, tools, on_text=None):
        if raises is not None:
            raise raises
        return "reply"
    return _call


@pytest.mark.parametrize("error,expected", [
    (None, "ok"),
    (provider.ProviderMisconfigured("401 invalid api key"), "misconfigured"),
    (provider.ProviderUnavailable("429 rate limited"), "unavailable"),
    (TypeError("our own bug"), "error"),
])
def test_the_probe_uses_the_taxonomy_rather_than_inventing_one(
        monkeypatch, error, expected):
    monkeypatch.setattr(provider, "call_model", fake_call(error))
    outcome, _ = setup.probe("nvidia", DEFAULT.model, "", KEY)
    assert outcome == expected


def test_the_probe_asks_with_the_settings_ABOUT_to_be_saved(monkeypatch):
    """A wizard that verifies the configuration already on disk verifies
    nothing: the whole point is the one the user just chose."""
    from agent import config

    seen = {}

    def _call(messages, system, tools, on_text=None):
        seen.update(provider=config.PROVIDER, model=config.OPENAI_MODEL,
                    base=config.OPENAI_BASE_URL,
                    key=os.environ.get("AGENT_API_KEY"))
        return "reply"

    monkeypatch.setattr(provider, "call_model", _call)
    setup.probe("openai", "some/model", "https://example.invalid/v1", KEY)
    assert seen == {"provider": "openai", "model": "some/model",
                    "base": "https://example.invalid/v1", "key": KEY}


def test_the_probe_puts_every_setting_back_afterwards(monkeypatch):
    from agent import config

    before = {name: getattr(config, name) for name in
              ("PROVIDER", "MODEL", "OPENAI_MODEL", "OPENAI_BASE_URL",
               "REQUEST_TIMEOUT", "CALL_ATTEMPTS")}
    monkeypatch.setattr(provider, "call_model",
                        fake_call(provider.ProviderUnavailable("503")))
    setup.probe("anthropic", "claude-opus-5", "", KEY)
    assert {name: getattr(config, name) for name in before} == before


def test_an_unverified_key_never_reaches_the_environment(no_keys, monkeypatch):
    """The probe sets it to ask the question and takes it straight back out.
    Only `write_env` may leave a key behind, and only a verified one gets there."""
    monkeypatch.setattr(provider, "call_model",
                        fake_call(provider.ProviderMisconfigured("401")))
    setup.probe("nvidia", DEFAULT.model, "", KEY)
    assert "AGENT_API_KEY" not in os.environ


def test_the_probe_does_not_wait_a_working_turns_timeout(monkeypatch):
    """120 seconds and six attempts are sized for a turn, not for a person
    sitting at a prompt."""
    from agent import config

    seen = {}

    def _call(messages, system, tools, on_text=None):
        seen.update(timeout=config.REQUEST_TIMEOUT, attempts=config.CALL_ATTEMPTS)
        return "reply"

    monkeypatch.setattr(provider, "call_model", _call)
    setup.probe("nvidia", DEFAULT.model, "", KEY)
    assert seen["timeout"] == setup.PROBE_TIMEOUT <= 20
    assert seen["attempts"] == 1


def test_a_provider_message_that_quotes_the_key_is_scrubbed(monkeypatch):
    monkeypatch.setattr(provider, "call_model", fake_call(
        provider.ProviderMisconfigured(f"invalid api key: {KEY}")))
    outcome, message = setup.probe("nvidia", DEFAULT.model, "", KEY)
    assert outcome == "misconfigured"
    assert KEY not in message
    assert setup.mask(KEY) in message


# ================================================================== persistence

def test_a_verified_key_reaches_both_the_file_and_the_environment(
        env_file, no_keys):
    setup.write_env({"AGENT_PROVIDER": "nvidia", "AGENT_API_KEY": KEY})
    assert os.environ["AGENT_API_KEY"] == KEY
    assert f"AGENT_API_KEY={KEY}" in env_file.read_text(encoding="utf-8")


def test_rewriting_a_variable_replaces_its_line_rather_than_appending(env_file):
    """`_load_env` takes the FIRST occurrence, so a duplicate leaves a file
    whose behaviour does not match its last line."""
    env_file.write_text("AGENT_API_KEY=old\nAGENT_EMAIL_USER=me@example.com\n",
                        encoding="utf-8")
    setup.write_env({"AGENT_API_KEY": KEY})
    body = env_file.read_text(encoding="utf-8")
    assert body.count("AGENT_API_KEY=") == 1
    assert "old" not in body
    assert "AGENT_EMAIL_USER=me@example.com" in body


def test_a_commented_out_variable_is_left_alone(env_file):
    env_file.write_text("# AGENT_API_KEY=example\n", encoding="utf-8")
    setup.write_env({"AGENT_API_KEY": KEY})
    body = env_file.read_text(encoding="utf-8")
    assert "# AGENT_API_KEY=example" in body
    assert f"AGENT_API_KEY={KEY}" in body


def test_the_file_is_written_with_newlines_and_without_quotes(env_file):
    """Docker's --env-file does not strip quotes and does not want CRLF; this
    file is read by it, by Git Bash and by Windows alike."""
    setup.write_env({"AGENT_API_KEY": KEY})
    raw = env_file.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    assert b'"' not in raw and b"'" not in raw


def test_what_gets_written_depends_on_the_provider_chosen():
    assert setup.variables("nvidia", "m", "", KEY) == {
        "AGENT_PROVIDER": "nvidia", "OPENAI_MODEL": "m", "AGENT_API_KEY": KEY}
    assert setup.variables("anthropic", "claude-opus-5", "", KEY) == {
        "AGENT_PROVIDER": "anthropic", "AGENT_MODEL": "claude-opus-5",
        "ANTHROPIC_API_KEY": KEY}
    assert setup.variables("openai", "m", "https://x/v1", KEY) == {
        "AGENT_PROVIDER": "openai", "OPENAI_MODEL": "m",
        "OPENAI_BASE_URL": "https://x/v1", "AGENT_API_KEY": KEY}


def test_the_written_file_survives_being_read_back_by_the_entry_point(env_file):
    """`_load_env` is what actually consumes this file, so it is what checks it."""
    from agent.__main__ import _load_env

    setup.write_env({"AGENT_PROVIDER": "openai", "OPENAI_BASE_URL": "https://x/v1"})
    os.environ.pop("AGENT_PROVIDER", None)
    os.environ.pop("OPENAI_BASE_URL", None)
    try:
        assert _load_env(env_file) == 2
        assert os.environ["OPENAI_BASE_URL"] == "https://x/v1"
    finally:
        os.environ.pop("AGENT_PROVIDER", None)
        os.environ.pop("OPENAI_BASE_URL", None)


# ============================================================ the reload trap

def test_changing_the_model_needs_a_reload_to_take_effect(monkeypatch):
    """config.py resolves every tunable at IMPORT time, so a wizard that runs
    after `agent.config` is imported cannot change OPENAI_MODEL by writing
    os.environ alone. This is the trap the entry-point hook exists for."""
    from agent import config

    monkeypatch.setenv("OPENAI_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
    try:
        assert config.OPENAI_MODEL != "nvidia/nemotron-3-ultra-550b-a55b"
        importlib.reload(config)
        assert config.OPENAI_MODEL == "nvidia/nemotron-3-ultra-550b-a55b"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_the_anthropic_model_is_a_tunable_and_not_a_literal(monkeypatch):
    """It was hardcoded, so the Anthropic model could not be chosen at all."""
    from agent import config

    monkeypatch.setenv("AGENT_MODEL", "claude-haiku-4-5-20251001")
    try:
        importlib.reload(config)
        assert config.MODEL == "claude-haiku-4-5-20251001"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# ======================================================================= mask

def test_a_key_is_shown_by_its_ends_and_never_its_middle():
    shown = setup.mask(KEY)
    assert shown.startswith("nvapi-")
    assert shown.endswith("7f2c")
    assert KEY[6:-4] not in shown


def test_a_short_key_gives_away_nothing_at_all():
    assert setup.mask("abc") == "•" * 8


# ====================================================================== screen

def screen_app(monkeypatch, outcome, message="", key_present=None):
    """A wizard whose probe answers `outcome`, or each of them in turn.

    A list is how the retry path is exercised: the interesting case is a second
    attempt that answers differently from the first.
    """
    from agent.ui.setup import SetupApp

    answers = list(outcome) if isinstance(outcome, list) else [(outcome, message)]
    asked = []

    def _probe(*args, **kwargs):
        asked.append(args)
        return answers[min(len(asked) - 1, len(answers) - 1)]

    monkeypatch.setattr(setup, "probe", _probe)
    app = SetupApp(existing_key=key_present)
    app.asked = asked
    return app


def rendered(app) -> str:
    return "\n".join(str(strip.text) for strip
                     in app.screen._compositor.render_strips())


def test_enter_walks_from_the_list_to_the_key_box_and_verifies(monkeypatch, env_file, no_keys):
    """Measured on the first real open: arrows moved the highlight, Enter did
    nothing, typing did nothing, and nothing said Tab. Enter on a row lands
    in the key box; Enter in the key box is the verify button."""
    from textual.widgets import Input, OptionList

    app = screen_app(monkeypatch, "misconfigured", "401 invalid api key")

    async def script(pilot):
        assert isinstance(app.screen.focused, OptionList)
        await pilot.press("enter")
        assert app.screen.focused is app.screen.query_one("#key", Input)
        await pilot.press(*"nvapi-test")
        await pilot.press("enter")
        await pilot.pause()
        await app.workers.wait_for_complete()

    drive(app, script)
    assert app.asked and app.asked[0][3] == "nvapi-test", app.asked


def test_enter_walks_a_custom_endpoint_through_all_three_boxes(monkeypatch, env_file, no_keys):
    """The custom row shows two more boxes; Enter visits them in order."""
    from textual.widgets import Input

    app = screen_app(monkeypatch, "misconfigured", "401")
    custom = next(i for i, c in enumerate(setup.CHOICES) if not c.model)

    async def script(pilot):
        for _ in range(custom):
            await pilot.press("down")
        await pilot.press("enter")
        assert app.screen.focused is app.screen.query_one("#base-url", Input)
        await pilot.press(*"http://localhost:11434/v1", "enter")
        assert app.screen.focused is app.screen.query_one("#model-id", Input)
        await pilot.press(*"qwen", "enter")
        assert app.screen.focused is app.screen.query_one("#key", Input)

    drive(app, script)
    assert app.asked == []


def test_a_rejected_key_is_not_saved(monkeypatch, env_file, no_keys):
    saved = []
    monkeypatch.setattr(setup, "write_env", lambda pairs, path=None:
                        saved.append(pairs))
    app = screen_app(monkeypatch, "misconfigured", "401 invalid api key")

    async def script(pilot):
        await verify(app, pilot)

    drive(app, script)
    assert saved == []
    assert not env_file.exists()


def test_an_unavailable_endpoint_offers_to_save_anyway_and_says_why(
        monkeypatch, env_file, no_keys):
    """A rate limit says nothing about whether the key is good, so refusing to
    save it would strand someone whose credentials are fine."""
    app = screen_app(monkeypatch, "unavailable", "429 rate limited")

    async def script(pilot):
        await verify(app, pilot)

    drive(app, script)
    assert app.offered == {"retry", "save-anyway", "back"}
    assert "429" in app.last_message


def test_a_rejected_key_offers_no_way_to_save_it(monkeypatch, env_file, no_keys):
    app = screen_app(monkeypatch, "misconfigured", "401 invalid api key")

    async def script(pilot):
        await verify(app, pilot)

    drive(app, script)
    assert "save-anyway" not in app.offered


def test_a_verified_key_is_written_and_shown_masked(monkeypatch, env_file, no_keys):
    app = screen_app(monkeypatch, "ok")

    async def script(pilot):
        await verify(app, pilot)

    drive(app, script)
    body = env_file.read_text(encoding="utf-8")
    assert f"AGENT_API_KEY={KEY}" in body
    assert f"OPENAI_MODEL={DEFAULT.model}" in body


def test_the_key_is_never_rendered_anywhere_on_the_screen(
        monkeypatch, env_file, no_keys):
    """Including in the provider's own error message, which is the one place
    it could arrive without anyone deciding to put it there."""
    app = screen_app(monkeypatch, "misconfigured", f"rejected key {KEY}")

    async def script(pilot):
        await verify(app, pilot)
        assert KEY not in rendered(app)

    drive(app, script)


def test_leaving_without_saving_writes_nothing(monkeypatch, env_file, no_keys):
    """A wizard with no way out is the one screen that can strand someone, and
    leaving must cost nothing - which it does, because nothing is written until
    a probe has answered."""
    app = screen_app(monkeypatch, "ok")

    async def script(pilot):
        await pilot.press("ctrl+q")
        await pilot.pause()

    drive(app, script)
    assert not env_file.exists()
    assert app.return_value is not True


def test_the_key_entry_is_a_password_field(monkeypatch):
    app = screen_app(monkeypatch, "ok")

    async def script(pilot):
        assert app.screen.query_one("#key").password is True

    drive(app, script)


def test_an_existing_key_is_shown_masked_and_need_not_be_retyped(monkeypatch):
    """§6.7: changing the model must not cost you the key you already have."""
    app = screen_app(monkeypatch, "ok", key_present=KEY)

    async def script(pilot):
        assert KEY not in rendered(app)
        assert setup.mask(KEY) in rendered(app)
        assert app.screen.key_in_hand() == KEY

    drive(app, script)


def test_the_model_list_offers_what_this_repo_actually_supports(monkeypatch):
    assert {choice.provider for choice in setup.CHOICES} == {
        "nvidia", "anthropic", "openai"}
    assert DEFAULT.model == "nvidia/nemotron-3-super-120b-a12b"


def test_anything_but_the_default_warns_that_it_is_a_different_measurement(
        monkeypatch):
    app = screen_app(monkeypatch, "ok")

    async def script(pilot):
        app.screen.choose(1)
        await pilot.pause()
        assert "different measurement" in rendered(app)
        app.screen.choose(0)
        await pilot.pause()
        assert "different measurement" not in rendered(app)

    drive(app, script)


def test_retry_asks_again_and_a_second_answer_is_believed(
        monkeypatch, env_file, no_keys):
    """A rate limit is transient by definition, so the retry button has to
    actually re-probe rather than re-render the last answer."""
    app = screen_app(monkeypatch, [("unavailable", "429"), ("ok", "")])

    async def script(pilot):
        await verify(app, pilot)
        assert not env_file.exists()
        app.screen.query_one("#retry").press()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    drive(app, script)
    assert len(app.asked) == 2
    assert f"AGENT_API_KEY={KEY}" in env_file.read_text(encoding="utf-8")


def test_save_anyway_writes_the_key_the_endpoint_could_not_judge(
        monkeypatch, env_file, no_keys):
    app = screen_app(monkeypatch, "unavailable", "503 upstream")

    async def script(pilot):
        await verify(app, pilot)
        app.screen.query_one("#save-anyway").press()
        await pilot.pause()

    drive(app, script)
    assert f"AGENT_API_KEY={KEY}" in env_file.read_text(encoding="utf-8")


def test_change_model_puts_the_choice_back_in_front_of_you(
        monkeypatch, env_file, no_keys):
    app = screen_app(monkeypatch, "misconfigured", "401")

    async def script(pilot):
        await verify(app, pilot)
        assert app.screen.query_one("#after").display is True
        app.screen.query_one("#back").press()
        await pilot.pause()
        assert app.screen.query_one("#after").display is False
        assert app.offered == set()

    drive(app, script)
    assert not env_file.exists()


def test_a_custom_endpoint_writes_its_base_url_and_model(
        monkeypatch, env_file, no_keys):
    custom = len(setup.CHOICES) - 1
    app = screen_app(monkeypatch, "ok")

    async def script(pilot):
        app.screen.choose(custom)
        await pilot.pause()
        app.screen.query_one("#base-url").value = "https://openrouter.ai/api/v1"
        app.screen.query_one("#model-id").value = "z-ai/glm-5.2"
        await verify(app, pilot)

    drive(app, script)
    body = env_file.read_text(encoding="utf-8")
    assert "AGENT_PROVIDER=openai" in body
    assert "OPENAI_BASE_URL=https://openrouter.ai/api/v1" in body
    assert "OPENAI_MODEL=z-ai/glm-5.2" in body


@pytest.mark.parametrize("base,model", [
    ("", "z-ai/glm-5.2"),
    ("https://openrouter.ai/api/v1", ""),
])
def test_a_custom_endpoint_with_a_gap_is_refused_before_any_probe(
        monkeypatch, env_file, no_keys, base, model):
    """Blank means "the configured endpoint" downstream, so a half-filled
    custom choice would verify NVIDIA and then configure something else."""
    custom = len(setup.CHOICES) - 1
    app = screen_app(monkeypatch, "ok")

    async def script(pilot):
        app.screen.choose(custom)
        await pilot.pause()
        app.screen.query_one("#base-url").value = base
        app.screen.query_one("#model-id").value = model
        await verify(app, pilot)

    drive(app, script)
    assert app.asked == []
    assert not env_file.exists()


def test_a_key_with_no_key_typed_and_none_configured_is_refused(
        monkeypatch, env_file, no_keys):
    app = screen_app(monkeypatch, "ok")

    async def script(pilot):
        await verify(app, pilot, key="")

    drive(app, script)
    assert app.asked == []
    assert not env_file.exists()


def test_a_read_only_checkout_says_so_rather_than_crashing(
        monkeypatch, env_file, no_keys):
    """The project tree is mounted :ro in the measured environment, and a
    traceback over a wizard nobody can get past is the worst version of this."""
    def _boom(pairs, path=None):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(setup, "write_env", _boom)
    app = screen_app(monkeypatch, "ok")

    async def script(pilot):
        await verify(app, pilot)
        assert "could not write .env" in rendered(app)

    drive(app, script)
    assert app.return_value is not True


def test_interactive_survives_a_stream_that_is_not_one(monkeypatch):
    """pythonw and some service hosts hand over a None stdin, and this runs on
    every `python -m agent`."""
    monkeypatch.delenv("AGENT_NO_SETUP", raising=False)
    monkeypatch.setattr(setup.sys, "stdin", None)
    assert setup.interactive() is False


def test_the_custom_fields_appear_only_for_the_custom_choice(monkeypatch):
    """A base URL box beside a named NVIDIA model invites filling it in, and
    filling it in there does nothing."""
    custom = len(setup.CHOICES) - 1
    app = screen_app(monkeypatch, "ok")

    async def script(pilot):
        assert app.screen.query_one("#base-url").display is False
        app.screen.choose(custom)
        await pilot.pause()
        assert app.screen.query_one("#base-url").display is True
        assert app.screen.query_one("#model-id").display is True
        app.screen.choose(0)
        await pilot.pause()
        assert app.screen.query_one("#base-url").display is False

    drive(app, script)


def test_the_wizard_ticks_only_while_the_probe_is_running(monkeypatch):
    """The spinner is the one timer section 11 allows, and only for as long as
    something is actually happening."""
    from tests.test_ui_screens import live_timers

    app = screen_app(monkeypatch, "misconfigured", "401")

    async def script(pilot):
        assert live_timers(app.screen) == []
        await verify(app, pilot)
        assert live_timers(app.screen) == [], "the spinner outlived the probe"

    drive(app, script)


def test_the_wizard_does_not_import_the_workspace():
    """`run()` opens this on first launch, before anything else is needed, so
    it must not drag langgraph and the graph in behind it."""
    import pathlib
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c",
         "import sys, agent.ui.setup;"
         "assert 'langgraph' not in sys.modules, 'langgraph';"
         "assert 'agent.ui.screens' not in sys.modules, 'screens'"],
        cwd=pathlib.Path(__file__).resolve().parent.parent,
        capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# ------------------------------------ the Windows Terminal profile (§10.4)
#
# A FRAGMENT, never an edit to the user's settings.json. That is the property
# worth protecting: installing writes one file of our own and removing deletes
# it, so nothing we do can damage a file the user owns.


def test_the_guid_matches_microsofts_own_worked_example():
    """The one test that proves the algorithm instead of restating it.

    The fragment docs publish a worked example - app `Git`, profile `Git Bash`
    -> {2ece5bfe-50ed-5f3a-ab87-5cd4baafed2b} - which is an oracle we did not
    write. Get the UTF-16LE encoding dance wrong and this fails.
    """
    assert setup.profile_guid("Git", "Git Bash") == \
        "{2ece5bfe-50ed-5f3a-ab87-5cd4baafed2b}"


def test_the_guid_is_stable_so_reinstalling_updates_one_profile():
    """Terminal keys a fragment profile by GUID. A fresh one each time would
    stack duplicates in the dropdown."""
    assert setup.profile_guid() == setup.profile_guid()
    assert setup.profile_guid().startswith("{") and setup.profile_guid().endswith("}")


def test_the_fragment_carries_the_look_that_was_asked_for():
    assert setup.fragment(blurred=True)["profiles"][0]["useAcrylic"] is True
    assert setup.fragment(blurred=False)["profiles"][0]["useAcrylic"] is False
    assert setup.fragment(blurred=False)["profiles"][0]["opacity"] == 85


def test_the_fragment_defines_a_name_which_is_the_documented_minimum():
    """"For new profiles added through fragments, the new profile must define a
    name for itself." Everything else is optional; this is not."""
    assert setup.fragment(blurred=False)["profiles"][0]["name"]


def test_the_launch_command_is_quoted_only_when_it_has_to_be(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda name: r"C:\Tools\noesis.exe")
    assert setup.launch_command() == r"C:\Tools\noesis.exe"

    monkeypatch.setattr(setup.shutil, "which", lambda name: r"C:\Program Files\noesis.exe")
    assert setup.launch_command() == r'"C:\Program Files\noesis.exe"'


def test_a_source_checkout_with_no_installed_script_still_gets_a_profile():
    """`noesis` is only on PATH after an install. Falling back to -m agent means
    the profile works for someone running from the repo."""
    import agent.setup as module

    original, module.shutil.which = module.shutil.which, lambda name: None
    try:
        assert "-m agent" in setup.launch_command()
    finally:
        module.shutil.which = original


def test_there_is_nowhere_to_put_a_fragment_off_windows(monkeypatch):
    """The container is Linux, so this is the path the suite mostly runs."""
    monkeypatch.setattr(setup, "_windows", lambda: False)

    assert setup.fragment_path() is None
    assert setup.installed() is False
    assert setup.remove_profile() is False


def test_installing_off_windows_refuses_rather_than_writing_somewhere_odd(monkeypatch):
    monkeypatch.setattr(setup, "_windows", lambda: False)

    with pytest.raises(RuntimeError):
        setup.install_profile(blurred=False)


def _as_windows(monkeypatch, tmp_path):
    """Patch the PREDICATE, never `os.name`: pathlib reads os.name to pick
    WindowsPath, so stubbing it builds paths the host cannot instantiate."""
    monkeypatch.setattr(setup, "_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))


def test_the_fragment_goes_where_terminal_reads_them(monkeypatch, tmp_path):
    """Under LOCALAPPDATA, and NOT the Packages folder that holds the user's own
    settings.json - that file is never touched."""
    _as_windows(monkeypatch, tmp_path)

    path = setup.fragment_path()
    assert path.parent.parent.name == "Fragments"
    assert path.suffix == ".json"
    assert "Packages" not in str(path)


def test_installing_creates_the_folder_and_writes_utf8(monkeypatch, tmp_path):
    """The docs name UTF-16LE as the way this silently fails, so the encoding is
    asserted on the BYTES rather than trusted."""
    _as_windows(monkeypatch, tmp_path)

    path = setup.install_profile(blurred=True)

    assert path.is_file()
    raw = path.read_bytes()
    assert b"\x00" not in raw, "written as UTF-16, which Terminal cannot read"
    written = json.loads(raw.decode("utf-8"))
    assert written["profiles"][0]["useAcrylic"] is True
    assert setup.installed() is True


def test_installing_twice_leaves_one_file_and_one_profile(monkeypatch, tmp_path):
    _as_windows(monkeypatch, tmp_path)

    setup.install_profile(blurred=True)
    path = setup.install_profile(blurred=False)

    assert len(list(path.parent.iterdir())) == 1
    written = json.loads(path.read_text(encoding="utf-8"))
    assert len(written["profiles"]) == 1
    assert written["profiles"][0]["useAcrylic"] is False, "the second answer won"


def test_removing_deletes_it_and_says_so_only_once(monkeypatch, tmp_path):
    _as_windows(monkeypatch, tmp_path)
    setup.install_profile(blurred=False)

    assert setup.remove_profile() is True
    assert setup.installed() is False
    assert setup.remove_profile() is False, "nothing to remove is not a failure"
