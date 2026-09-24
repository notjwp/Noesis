"""classify() — the gate's entire decision logic.

NO SIDE EFFECTS (FR-305). The gate node suspends on `interrupt()` and re-executes
from its first line when a run resumes, so anything that logged, counted, or wrote
here would happen twice.

Note `from agent import config` rather than `from agent.config import WORKSPACE`:
the latter binds the value at import time, so a test redirecting the workspace root
would leave this module still pointing at the real one.
"""
import re

from agent import config

# A home directory, however it is spelled. `~`, $HOME and ${HOME} all reach the
# same files and a rule matching one of the three protects nothing.
_HOME = r"(?:~|\$HOME|\$\{HOME\})"

# On macOS /etc, /var, /tmp and /home are symlinks into /private/. A command
# written against /private/etc/sudoers works exactly like /etc/sudoers and walks
# straight past a plain "/etc/" check. Not something reasoning from scratch
# would produce.
_SYSTEM_PATH = r"(?:/etc/|/private/(?:etc|var|tmp|home)/|/boot/|/dev/sd)"

# Files that hand over the next session, or the account. Writing one is not
# destructive TODAY, which is exactly why it needs a person to see it.
_SENSITIVE_FILE = (
    rf"{_HOME}/\.ssh(?:/|\b)"
    rf"|{_HOME}/\.(?:bashrc|zshrc|profile|bash_profile|zprofile)\b"
    rf"|{_HOME}/\.(?:netrc|pgpass|npmrc|pypirc|aws|config/gh)\b"
    # A FILE called .env: at the start, or after a space, slash or quote. Not
    # the suffix of a glob - `find -name "*.env"` was refused for naming it.
    r"|(?:^|[\s/'\"=])\.env\b"
)

# An interpreter given inline source is a shell by another name - when the
# source DELETES. Escalating the flag alone refused serve-token's own urlopen()
# 6 of 6 (eval/CHANGELOG.md, 2026-09-11).
_DELETES = r"(?:rmtree|rmSync|rmdirSync|unlink|os\.remove\(|os\.rmdir\(|Remove-Item)"
_INLINE_SOURCE = (
    r"(?:\bpython[\d.]*\s+(?:-\w+\s+)*(?:-c\b|<<)"
    r"|\bnode\s+(?:-\w+\s+)*(?:-e|--eval|-p|--print|<<)"
    r"|\b(?:perl|ruby)\s+(?:-\w+\s+)*(?:-e\b|<<)"
    r"|\bphp\s+(?:-\w+\s+)*(?:-r\b|<<)"
    r"|\bpowershell(?:\.exe)?\s+.*?-(?:command|c|file|f)\b)"
    rf"[\s\S]*?{_DELETES}"
)

# Read-only verbs with a flag that runs an arbitrary program. The verb is not
# the risk; the flag is - and a verb allowlist would wave all four through.
_EXEC_FLAG = (
    r"\bsort\b[^|]*--compress-program"
    r"|\brg\b[^|]*--pre\b"
    r"|\bag\b[^|]*--pager\b"
    r"|\bman\b[^|]*(?:\s-P\s|--pager|\s-H\s|--html)"
)

# Where a shell would begin parsing a NEW command, so `echo reboot` and
# `grep shutdown log` are words and `ls; halt` is a command.
_COMMAND_START = r"(?:^|[;&|(]\s*|\bsudo\s+)"

# FR-204. Installing a package is ordinary work; WHERE it comes from is the
# decision. A bare path is not a source - it matches every flag that takes
# one, and `-r ./requirements.txt` denied autonomously fails `missing-dep`.
_PACKAGE_SOURCE = (
    r"\bPIP_CONFIG_FILE\s*="
    r"|\b(?:pip[\d.]*|uv\s+pip|npm|pnpm|yarn)\b[^|]*\b(?:install|add)\b[^|]*"
    r"(?:--(?:index-url|extra-index-url|find-links|trusted-host|registry)"
    r"|\s-i\s"
    r"|\s(?:https?|git\+[a-z]+|file|ssh)://"
    r"|\s\S+\.(?:whl|tar\.gz|tgz)\b)"
)

# The same list as a standalone pattern, for PATH arguments. `cat ~/.ssh/id_rsa`
# and read_file(path="~/.ssh/id_rsa") are the same act and must get the same
# answer, which they did not while only run_shell was inspected.
SENSITIVE = re.compile(_SENSITIVE_FILE, re.IGNORECASE)

# The tools that RUN what they are given, and the argument carrying it. Both
# halves matter: adding a tool here without its argument name is the same hole.
EXECUTES = {"run_shell": "command", "start_terminal": "command",
            "run_python": "code"}

# Commands that must never run unreviewed. Matched against what EXECUTES names;
# a hit escalates the tool's declared risk to `destructive`, and the RULE that
# hit is in the reason, because the interface remembers approvals by rule.
#
# WIDENED 2026-09-08, when FR-302 stopped refusing paths outside the workspace
# and this became the only thing between the model and the filesystem. The gaps
# were measured by reading it, not guessed: `mv ~/Documents /tmp`, `> ~/.bashrc`,
# `python -c "shutil.rmtree(...)"` and `git clean -fdx` all passed the old list.
# Deliberately small: categories, not an exhaustive list.
DANGER = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in (
    ("recursive delete", r"\brm\s+(-\w+\s+)*-\w*[rf]"),
    ("force push", r"\bgit\s+push\b[^|]*--force"),
    ("git reset --hard", r"\bgit\s+reset\s+--hard\b"),
    ("git clean", r"\bgit\s+clean\b[^|]*-\w*[fd]"),
    ("sudo", r"\bsudo\b"),
    ("dd", r"\bdd\s+if="),
    ("chmod 777", r"\bchmod\s+-R\s+777\b"),
    ("curl piped to a shell", r"\bcurl\b[^|]*\|\s*(ba)?sh\b"),
    # moving or copying the home directory somewhere else is a wipe with a
    # different verb, and `mv` was absent from the list entirely
    ("moving the home directory", rf"\b(?:mv|cp|rsync)\b[^|]*{_HOME}/"),
    ("a system path", _SYSTEM_PATH),
    ("a credential file", _SENSITIVE_FILE),
    ("inline source that deletes", _INLINE_SOURCE),
    ("a program run by a read-only tool's flag", _EXEC_FLAG),
    # a redirect INTO anything sensitive, which no verb above would catch
    ("a redirect into a dotfile", rf">>?\s*{_HOME}/\."),
    ("a package from an unvetted source", _PACKAGE_SOURCE),
)]

# Above `destructive`: refused with a person present, and no approval can allow
# it. Saying "allow" trusts the agent with your files; it does not trust it to
# wipe the disk or power the box off. Tiny, and only things with no recovery.
HARDLINE = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in (
    ("deleting the root or home directory",
     rf"\brm\s+(-\w+\s+)*-\w*[rf]\w*\s+(?:-\w+\s+)*(?:/\*?|{_HOME}/?)(?:\s|$)"),
    ("writing a block device", r"(?:\bdd\b[^|]*\bof=|>\s*)/dev/(?:sd|nvme|hd|vd|mmcblk|disk)"),
    ("formatting a filesystem", r"\bmkfs(?:\.\w+)?\b"),
    ("shutting the machine down", rf"{_COMMAND_START}(?:shutdown|reboot|halt|poweroff)\b"),
    ("a fork bomb", r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
)]


def _first(rules, text: str) -> str | None:
    return next((name for name, pattern in rules if pattern.search(text)), None)

# The single source of a tool's risk and the single path through classify().
# Built from tools.TOOLS so a new tool cannot be offered unclassified.
RISK: dict[str, str] = {}


def sync() -> dict[str, str]:
    """Refresh the built-in half of RISK from tools.TOOLS.

    Imported inside the function so the dependency stays one-way: policy knows
    about tools, tools knows nothing about policy, and nothing is read at import
    time that a test could not redirect first (CE-05).
    """
    from agent.tools import TOOLS

    RISK.update({name: entry["risk"] for name, entry in TOOLS.items()
                 if "risk" in entry})
    return RISK


sync()

VERDICT_BY_RISK = {"read": "auto", "write": "auto", "destructive": "confirm"}

# Arguments whose value is a filesystem path and must stay inside the
# workspace (FR-302). Named explicitly: guessing by key name would miss one.
PATH_ARGS = ("path", "file", "filename", "filepath", "cwd", "dir", "directory",
             "folder", "source", "destination", "dest", "target", "output")


# Commands allowed while PLANNING (UR-02). An allowlist, not a denylist: the
# planning gate exists to prevent unapproved EDITS, so anything not provably
# read-only is refused.
_READ_ONLY_VERB = re.compile(
    r"^\s*("
    r"ls|cat|head|tail|find|grep|rg|wc|tree|file|stat|nl|du|basename|dirname"
    r"|sed\s+-n"                       # -n prints; without it sed WRITES
    r"|git\s+(status|log|diff|show|ls-files|branch)"
    # Running the test suite is RESEARCH, and refusing it was the single largest
    # defect in the planning phase - the agent planned a fix for a failure it had
    # never observed. Bare `python` stays denied; see eval/CHANGELOG.md Stage 7.
    r"|python\s+-m\s+pytest"
    r"|pytest"
    r")\b")

# Checked BEFORE splitting on the pipe, because `||` contains one. A redirect is
# the whole risk in an otherwise harmless command (`cat x > y`), and a chain lets
# an allowed verb carry an arbitrary one behind it (`ls && rm -rf build`).
_CHAINED = re.compile(r"[>;`]|&&|\|\|")


def _read_only(name: str, args: dict, risk: str) -> bool:
    """True when this call cannot change anything. Fails closed."""
    if risk == "read":
        return True
    if name != "run_shell":
        return False
    command = str(args.get("command", ""))
    if _CHAINED.search(command):
        return False
    return all(_READ_ONLY_VERB.match(segment) for segment in command.split("|"))


def register(name: str, risk: str | None) -> str:
    """Declare an MCP tool's risk before its schema is ever shown to the model.

    Returns the risk actually recorded, which is not always the one requested.

    An unknown or missing risk becomes `destructive`, never `read`. That makes an
    unclassified tool VISIBLE — it pauses for approval interactively and is refused
    unattended — instead of silently trusted. This is the same failure Phase K
    recorded from the other side: `AGENT_EGRESS` defaulted to "restricted", so every
    trace row claimed a condition nobody had checked. A default that asserts the
    safe-looking answer hides exactly what it should surface.
    """
    RISK[name] = risk if risk in VERDICT_BY_RISK else "destructive"
    return RISK[name]


def risk_of(name: str) -> str | None:
    """A tool's declared risk, or None when nothing has declared one.

    Falls back to tools.TOOLS so a built-in added after import is classifiable
    without anyone remembering to call sync() - which is what NFR-601 actually
    asks for. An undeclared tool returns None and classify() denies it, the same
    fail-closed default register() applies to an unclassified MCP tool.
    """
    if name in RISK:
        return RISK[name]
    from agent.tools import TOOLS

    entry = TOOLS.get(name)
    return entry.get("risk") if entry else None


def classify(name: str, args: dict, autonomous: bool,
             planning: bool = False) -> tuple[str, str]:
    """Return (verdict, reason). verdict is one of auto | confirm | deny.

    `planning` defaults False so every existing caller keeps the behaviour it was
    measured with. Still pure, still no side effects (FR-305): the gate suspends
    and re-executes from its first line, so this function must be safe to run
    twice.
    """
    outside = next((str(args[key]) for key in PATH_ARGS
                    if key in args and not _inside_workspace(str(args[key]))), "")

    risk = risk_of(name)
    if risk is None:
        return "deny", f"unknown tool: {name}"

    # DENY rather than confirm, on purpose. A `confirm` here would stop the human
    # once per read while the agent looks around - which is the opposite of what
    # planning is for, and would train them to approve without looking.
    if planning and not _read_only(name, args, risk):
        return "deny", (f"planning: {name} could change something, and writes are "
                        f"refused until the plan is accepted. Read and search now; "
                        f"do this once the plan is agreed.")

    # Every tool that executes what it is handed, and the argument holding it.
    # Keyed by NAME because the argument names differ - and that difference was
    # a hole: the escalation read `command`, so `python3 -c X` was refused
    # through run_shell while run_python ran X at `auto`. Measured 2026-09-10,
    # and the agent switched tools on its own. An escalation one tool enforces
    # and another ignores is not a boundary.
    source = str(args.get(EXECUTES.get(name, ""), "") or "")
    hardline = _first(HARDLINE, source) if source else None
    if hardline:
        return "deny", f"{name} is {hardline}; refused, and no approval can allow it"
    rule = _first(DANGER, source) if source else None
    if rule:
        risk = "destructive"

    verdict = VERDICT_BY_RISK[risk]
    # A credential is the one thing READING is not free. Applied to path
    # arguments and not only to run_shell, or the same file gets two answers
    # depending on which tool asks for it.
    secret = next((str(args[key]) for key in PATH_ARGS
                   if key in args and SENSITIVE.search(str(args[key]))), "")
    if secret:
        return _unattended("confirm", autonomous,
                           f"{name} touches a credential: {secret}")

    # FR-302 as amended 2026-09-08. Outside the workspace can never be `auto`,
    # and reading is still auto because reading the user's own files is the
    # point. A write out there asks; unattended, `confirm` degrades to deny
    # below, so nothing writes outside without a person present.
    if outside and not _read_only(name, args, risk):
        verdict = "confirm"
        return _unattended(verdict, autonomous,
                           f"{name} writes outside the workspace: {outside}")
    # The rule in the reason, because the interface remembers approvals by it:
    # "destructive" alone would let one allow cover every category.
    what = f"{name} is {risk} ({rule})" if rule else f"{name} classified {risk}"
    if autonomous and verdict == "confirm":
        return "deny", f"{what}; denied in autonomous mode, queued for review"
    return verdict, what


def _unattended(verdict: str, autonomous: bool, reason: str) -> tuple[str, str]:
    """A confirm nobody can answer is a deny, which is what autonomous means."""
    if autonomous and verdict == "confirm":
        return "deny", f"{reason}; denied in autonomous mode, queued for review"
    return verdict, reason


def _inside_workspace(value: str) -> bool:
    """True when `value` resolves inside the workspace root. Fails closed.

    `.resolve()` follows symlinks, so a link inside the workspace pointing out
    resolves outside and is rejected.
    """
    root = config.WORKSPACE
    try:
        resolved = config.resolve(value).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents
