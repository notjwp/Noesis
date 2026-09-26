"""Single source of truth for every tunable in the system.

FR-302 and NFR-201 both depend on WORKSPACE having exactly one definition, so it
is never re-derived elsewhere - scripts/reset.sh reads the same variable.
"""
import os
from pathlib import Path

# Every AGENT_* name this module reads, recorded as it is read. eval/harness.py
# derives what it forwards into a scored container from this - a hand-kept list
# has silently omitted a variable three times.
ENV_VARS: list = []


def _env(name: str, default: str = "") -> str:
    ENV_VARS.append(name)
    return os.environ.get(name, default)


# --- locations -------------------------------------------------------------

WORKSPACE = Path(_env("AGENT_WORKSPACE", "/workspace")).resolve()

# Artifacts sit INSIDE the workspace so a spilled file is readable without
# tripping FR-302; state sits OUTSIDE it so reset.sh cannot wipe it.
ARTIFACTS = WORKSPACE / ".agent" / "artifacts"
# Per-user by default. The container's image sets /state; on a host, / is not
# writable and a drive root is a directory nobody chose - measured both ways.
AGENT_HOME = Path(_env("AGENT_HOME", "~/.noesis")).expanduser().resolve()
STATE_DB = AGENT_HOME / "state.db"

# --- model -----------------------------------------------------------------

PROVIDER = _env("AGENT_PROVIDER", "nvidia")

# A literal until the setup wizard existed, which meant the Anthropic model
# could not be selected at all. Same rule as OPENAI_MODEL: changing it is a
# different measurement.
MODEL = _env("AGENT_MODEL", "claude-opus-5")
EFFORT = "medium"        # low | medium | high | xhigh | max

# OPENAI_* names the protocol, not the vendor: any compatible endpoint works.
# NIM_* is kept so existing .env files do not break.
OPENAI_BASE_URL = (os.environ.get("OPENAI_BASE_URL")
                   or os.environ.get("NIM_BASE_URL")
                   or "https://integrate.api.nvidia.com/v1")

# Chosen by probe, not reputation - model history is in eval/CHANGELOG.md.
# Changing this invalidates every baseline: it is a different measurement.
OPENAI_MODEL = (os.environ.get("OPENAI_MODEL")
                or os.environ.get("NIM_MODEL")
                or "nvidia/nemotron-3-super-120b-a12b")

MAX_TOKENS = 16_000      # on Anthropic this caps thinking AND response together

# Turn and token caps do not bound a hung request, and the SDK defaults are
# 10 min x 3 attempts - half an hour of silence before anything surfaces.
REQUEST_TIMEOUT = float(_env("AGENT_REQUEST_TIMEOUT", "120"))

MAX_ATTEMPTS = 3

# Attempts at the WHOLE call, above whatever the SDK retries internally.
# MEASURED against NVIDIA on 2026-09-01: 15 of 20 calls through call_model()
# survived, and the five failures were `APIError: Service temporarily overloaded`
# returning in 0.2-0.3s - too fast for the SDK to have retried them at all,
# because a bare APIError carries no status_code and the SDK only retries codes
# it can attribute. 75% per call compounds to 0.3% over a 20-turn run, which is
# why six launch attempts produced zero scored runs.
CALL_ATTEMPTS = int(_env("AGENT_CALL_ATTEMPTS", "6"))

# The endpoint recovers in SECONDS - a retry 2s later succeeded repeatedly - so
# this caps low. Backoff sized for a capacity bounce, not a rate limit.
RETRY_BASE = float(_env("AGENT_RETRY_BASE", "1.0"))
RETRY_CAP = float(_env("AGENT_RETRY_CAP", "8.0"))


def resolve(path) -> Path:
    """A tool argument as a real filesystem path.

    ONE definition, because the gate and the tool must resolve identically - a
    gate that checks a different path than the one written is not a gate.
    `~` is expanded first: without it `~/.bashrc` is a relative path naming a
    directory called `~`, which lands INSIDE the workspace and reads as safe.
    """
    candidate = Path(str(path)).expanduser()
    return candidate if candidate.is_absolute() else WORKSPACE / candidate


def _clean(value: str | None) -> str:
    """Strip whitespace and surrounding quotes.

    Docker's --env-file does not strip quotes, so KEY="abc" arrives with them and
    the only symptom is an auth failure that looks nothing like a quoting bug.
    """
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


# Checked in order; AGENT_API_KEY is the provider-neutral one to prefer.
KEY_VARS = ("AGENT_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY")


def openai_api_key() -> str:
    """Read at call time, never at import: importing config must not require a key."""
    for name in KEY_VARS:
        key = _clean(os.environ.get(name))
        if key:
            return key
    raise RuntimeError(
        "no API key for the OpenAI-compatible provider. Set AGENT_API_KEY, and "
        "OPENAI_BASE_URL / OPENAI_MODEL for the endpoint you want - any "
        "OpenAI-compatible provider works (NVIDIA NIM, OpenAI, OpenRouter, Groq, "
        "a local Ollama). Or set AGENT_PROVIDER=anthropic with ANTHROPIC_API_KEY.")

# --- budgets ---------------------------------------------------------------

# MEASURED 2026-09-01: at 12 this cap was the binding failure, not the model.
# Raised to 30 and dev went 13/15 -> 15/15 with every `stuck` verdict gone (it was
# 47% two runs earlier). Four of the fifteen runs went PAST the old ceiling and all
# four passed - add-endpoint made its first edit on call 13, and missing-dep needed
# 26 turns. Median spend was 54,098 of BUDGET_TOKENS, so tokens were never the
# constraint; the runs were starved on turns while being told to stop.
#
# 30 is derived, not picked: at ~4.4k tokens a turn the token budget binds around
# turn 45, so 30 keeps BUDGET_TOKENS the real ceiling. Comparable agents cap a
# parent at 200-500; 12 was a fixture-era cost control that outlived its reason.
MAX_TURNS = 30
# 0 = no ceiling, set 2026-09-23. Runs were ending on `budget` with the fix
# already landed and the tests green; MAX_TURNS and MAX_SECONDS still bound a
# run, so removing this leaves two stops, not none. UNMEASURED - see NFR-401.
BUDGET_TOKENS = int(_env("AGENT_BUDGET_TOKENS", "0"))

# WORKING seconds, accumulated by the nodes that spend time - not wall-clock
# since the goal arrived, or a thread resumed next week dies on its first turn.
#
# RAISED 900 -> 1500 and DERIVED, not chosen. 900 was sized against a hanging
# tool; measured over 21 real-* runs, tools take 9-11s and the model takes the
# rest. At 50.9s/turn (p50) a 900s cap buys 17.7 of a 30-turn allowance, so it
# was ending working runs, not runaway ones - all three of the last baseline
# died at 932-965s with 8-17 turns unused. 1500 is the smallest cap at which a
# median run can reach max_turns; 1800 only helps the slow tail and costs 20%.
MAX_SECONDS = int(_env("AGENT_MAX_SECONDS", "1500"))

# --- the email channel (see CONTEXT.md 12) ---------------------------------

# Credentials for the mailbox the agent reads and answers from. An app
# password, never the account password - and .env is gitignored.
EMAIL_USER = _clean(_env("AGENT_EMAIL_USER", ""))
EMAIL_PASSWORD = _clean(_env("AGENT_EMAIL_PASSWORD", ""))

# DEFAULT DENY. Comma-separated addresses, lowercased for comparison because
# the local part is case-sensitive by RFC and by nobody in practice. Empty
# authorises nobody: a mailbox with credentials and no allowlist would take
# instructions from every spammer who finds the address.
EMAIL_ALLOW = frozenset(
    part.strip().lower()
    for part in _clean(_env("AGENT_EMAIL_ALLOW", "")).split(",") if part.strip())

IMAP_HOST = _clean(_env("AGENT_IMAP_HOST", "imap.gmail.com"))
IMAP_PORT = int(_env("AGENT_IMAP_PORT", "993"))
SMTP_HOST = _clean(_env("AGENT_SMTP_HOST", "smtp.gmail.com"))
SMTP_PORT = int(_env("AGENT_SMTP_PORT", "465"))

CHANNEL_POLL = float(_env("AGENT_CHANNEL_POLL", "30"))
CHANNEL_TIMEOUT = float(_env("AGENT_CHANNEL_TIMEOUT", "30"))

# Fires on CONTEXT SIZE, not cumulative spend: spent_tokens only grows, so a
# spend-based trigger would fire every turn forever once crossed. 45,000 is
# where the old trigger effectively fired; derivation in eval/CHANGELOG.md.
COMPACT_AT_CHARS = int(_env("AGENT_COMPACT_AT", "45000"))

# At the cap the run ends `stuck` rather than looping: a compaction loop burns
# budget faster than the problem it solves.
MAX_COMPACTIONS = int(_env("AGENT_MAX_COMPACTIONS", "3"))

# The model writes the summary and MAX_TOKENS lets it write ~48k chars. Of 87
# recorded summaries p90 is 3,045 and six are over 20,000; one was 49,496 for
# 24 messages and the run compacted three times and died. Derived, not chosen.
COMPACT_SUMMARY_MAX_CHARS = 4_000

# Refuse to finish on an edit that was never verified. Bounded at two nudges,
# past which it is nagging.
#
# DEFAULT ON since 2026-08-31, reversing Cycle K's "build it only once traces
# show it is needed". The traces now show it, from 637 scored rows: of 98 runs
# that declared `done` and failed, 31 never ran the tests at all and 15 edited
# AFTER their last test run - 47% ended on an unverified change.
# The case for OFF is that the verification narrative can be more noise than
# signal, and that has been true elsewhere. Measured here the same
# way: on took dev 15/15 -> 12/15, with the stuck share 25% -> 47%.
VERIFY_ON_STOP = _env("AGENT_VERIFY_ON_STOP", "off").strip().lower() not in (
    "0", "off", "false")
MAX_VERIFY_NUDGES = 2


# NFR-101. Stream the OpenAI-compatible reply so there IS a first token to
# measure. Off restores the single-block path exactly, which is the fallback if
# an endpoint streams badly rather than not at all.
STREAM = _env("AGENT_STREAM", "1") not in ("0", "false", "no")

# FR-607. How many tasks may be `running` at once. Each worker holds a
# container and the shared /workspace, so the real bound is the machine, not
# the queue - one is the honest default for a single-user agent.
MAX_WORKERS = int(_env("AGENT_MAX_WORKERS", "1"))

# --- planning (FR-101, FR-105, UR-02) --------------------------------------

# DEFAULT OFF because it does not pay, not because it does not work: 1/3 on
# add-endpoint, stuck at the cap 3/3, 82,435 median tokens against NFR-402's
# 60,000. See eval/CHANGELOG.md.
# How much the gate asks: manual | plan | normal | auto. Validated by
# policy.resolve_mode rather than here, because config must not import policy.
MODE = _env("AGENT_MODE", "normal").strip().lower()

PLAN_ENABLED = _env("AGENT_PLAN", "off").strip().lower() not in (
    "0", "off", "false")

# Capped SEPARATELY and not charged against MAX_TURNS, which is what makes the
# phase affordable at all: research spending 4 of 12 turns would starve the
# cases planning exists to help.
PLAN_MAX_TURNS = 4

# A longer list is truncated, not refused - over-decomposing must not fail a run.
PLAN_MAX_STEPS = 6

# --- context caps ----------------------------------------------------------

# NFR-104 caps a tool result at 2,000 tokens, budgeted here in CHARACTERS at a
# conservative 3:1 so the check needs no tokeniser and no API key (NFR-602).
MAX_RESULT_CHARS = 6_000
TOOL_CAPS = {"read_file": 6_000, "write_file": 400, "run_shell": 6_000}
# The smallest window read_file will return. Measured on `real`: 74% of reads
# were 10-50-line windows onto a file already read, each a full model call on a
# provider that caches nothing. Just under what the char cap allows anyway.
READ_MIN_LINES = 100
HEAD_LINES = 30
TAIL_LINES = 20

# --- tools (Phase L) -------------------------------------------------------

# Schemas are re-sent every request and cache_read_tokens is 0 on every recorded
# row, so this is a per-turn tax: +1,000 chars is ~4,000 tokens a run at the
# measured median of 12 calls. A GUARD, not a budget - see §8.2, 2026-09-25.
MAX_SCHEMA_CHARS = 12_000

# Declared rather than discovered: a server offering a tool absent from `risk`
# gets it refused, never defaulted. fetch is `read` like web_search; the egress
# allowlist bounds it in scored runs, and nothing does natively.
MCP_SERVERS = {
    "fetch": {
        "command": ["python", "-m", "mcp_server_fetch"],
        "risk": {"fetch": "read"},
    },
}

MCP_ENABLED = _env("AGENT_MCP", "on").strip().lower() not in ("0", "off", "false")

# A hung MCP server is the same failure as an unbounded shell command, which
# once held a scored suite for 25 minutes.
MCP_STARTUP_TIMEOUT = float(_env("AGENT_MCP_STARTUP_TIMEOUT", "30"))
MCP_CALL_TIMEOUT = float(_env("AGENT_MCP_CALL_TIMEOUT", "60"))

# Default ON, unlike planning: this closes a [M] requirement. The switch exists
# so the control run is the same binary with one tool removed.
WEB_ENABLED = _env("AGENT_WEB", "on").strip().lower() not in (
    "0", "off", "false")

# The same switch, for any tool. AGENT_WEB predates it and stays because the
# search split's numbers are quoted against that name; everything since needs a
# control arm too, and a knob per tool is how a config file rots.
TOOLS_OFF = frozenset(
    name.strip() for name in _env("AGENT_TOOLS_OFF", "").split(",")
    if name.strip())

# --- memory (Phase M) ------------------------------------------------------

# In the agent home, not the workspace: reset.sh wipes the workspace between
# runs, and a queue or a memory that vanished with it would be neither.
MEMORY_DB = AGENT_HOME / "memory.db"
TASKS_DB = AGENT_HOME / "tasks.db"
PROFILE = AGENT_HOME / "AGENT.md"   # durable user profile (FR-406)

# The read-only viewer (section 11, amended 2026-09-10). LOOPBACK, and not a
# tunable: a viewer that serves traces, goals and workspace paths must not be
# reachable off this machine, and an env var is how that becomes 0.0.0.0 by
# accident. The port moves because a port can collide; the host cannot.
VIEWER_HOST = "127.0.0.1"
VIEWER_PORT = int(_env("AGENT_VIEWER_PORT", "8787"))
VIEWER_SECRET = AGENT_HOME / "viewer.secret"
# The working scratchpad, beside the durable profile and deliberately unlike it:
# AGENT.md is appended to and never decays, NOW.md is OVERWRITTEN every session
# because it describes what is true now. Conflating them is how a finished
# project's note becomes a standing rule.
NOW = AGENT_HOME / "NOW.md"

MEMORY_ENABLED = _env("AGENT_MEMORY", "on").strip().lower() not in (
    "0", "off", "false")

# Injected into the system prompt, so charged per turn like the schemas.
# `memory_chars` is on every row so a recall win bought with tokens shows up.
MEMORY_INJECT_CHARS = 1_500
MEMORY_EPISODES = 3

# Episodes older than this are down-ranked, never deleted (Phase 3.1). Decay
# per kind - 30 days for an event, 90 for a constraint, and NEVER for identity
# or preference - is the usual shape. Ours has exactly two kinds
# already: AGENT.md, which does not go through search() and so never decays, and
# episodes, which are events. One window is therefore the honest first version.
# Semantic recall (FR-408). MEASURED on eval/fixtures/recall-corpus.jsonl:
# keyword alone scores recall@3 of 0/40 where the query and the episode are
# worded differently, and fused with this lane it scores 32/40. On the older
# six-pair corpus it changed nothing, which is why it was reverted once - see
# CONTEXT.md 8.2.
SEMANTIC_RECALL = _env("AGENT_SEMANTIC_RECALL", "on").strip().lower() not in (
    "0", "off", "false")

# Baked into the image at /opt/minilm; there is no download path at run time.
EMBED_MODEL = Path(_env("AGENT_EMBED_MODEL", "/opt/minilm"))

# How deep each lane goes before fusion. Both lanes must offer more than the
# final cut or fusion has nothing to reorder.
SEMANTIC_DEPTH = int(_env("AGENT_SEMANTIC_DEPTH", "20"))

MEMORY_STALE_DAYS = float(_env("AGENT_MEMORY_STALE_DAYS", "30"))
MEMORY_STALE_DECAY = float(_env("AGENT_MEMORY_STALE_DECAY", "0.5"))

# --- skills (Phase N) ------------------------------------------------------

# agentskills.io layout. Two roots, searched in precedence order: the project's
# (read-only) then the agent's own. AGENT_SKILLS_DIR replaces the first, and the
# harness always sets it - the benchmark's library describes a FICTIONAL project.
SKILLS_DIRS = (
    Path(os.environ["AGENT_SKILLS_DIR"]).resolve() if _env("AGENT_SKILLS_DIR")
    else Path(__file__).resolve().parent.parent / "skills",
    AGENT_HOME / "skills",
)

SKILLS_ENABLED = _env("AGENT_SKILLS", "on").strip().lower() not in (
    "0", "off", "false")

# The always-loaded index, charged every request. Overflow is FATAL rather than
# truncating: a silently shortened index drops whole skills from the agent's
# view, and one it cannot see is indistinguishable from one it did not choose.
SKILLS_INDEX_CHARS = 1_600

# Below MAX_RESULT_CHARS so a long document spills rather than arriving whole.
SKILL_BODY_CHARS = 6_000

# The best-matching skill's body is injected outright rather than offered.
# MEASURED on 22 authoring runs where a skill was in the index: the agent called
# load_skill in 59% of them, and those passed 12 of 13 - against 1 of 9 when it
# did not. 92% versus 11%, decided by whether the model elected to open a file it
# had already been told about. Skill bodies here are 421-624 bytes, so injecting
# one costs less than a single tool result.
AUTO_SKILL = _env("AGENT_AUTO_SKILL", "on").strip().lower() not in (
    "0", "off", "false")

# --- authoring (Phase O) ----------------------------------------------------

# Separate from SKILLS_ENABLED so loading-on/authoring-off is measurable.
# DEFAULT OFF: `learn` was called ZERO times in 15 valid sessions.
SKILL_AUTHORING = _env("AGENT_SKILL_AUTHORING", "off").strip().lower() not in (
    "0", "off", "false")

# 12 authored skills would already overflow SKILLS_INDEX_CHARS, so this bound
# and that one must be read together. Past the cap `learn` refuses and says so.
MAX_AUTHORED_SKILLS = 8

# --- deterministic extraction at `finish` (Phase O-redux) -------------------

# A rule instead of asking the model, which declined 15 times out of 15.
# Switchable independently of authoring or neither is attributable.
# ON. MEASURED on the authoring split: extraction fired in 12 of 12 runs where
# the `learn` TOOL had fired in 3 of 117 - the agent almost never chooses to
# record what it worked out, so sessions 2 and 3 start blind. 3.3% -> 9/12, and
# 9/9 on the cases that are winnable.
# Standing instructions the user states are written to AGENT.md by a RULE, not
# by the model electing to call `remember`. Measured: `remember` fired 136 times
# in 13,049 tool calls and AGENT.md did not exist on this machine at all.
PROFILE_DISTIL = _env("AGENT_PROFILE_DISTIL", "on").strip().lower() not in (
    "0", "off", "false", "no")

# Three per session and 200 chars each, because the profile is injected into
# every system prompt and this provider caches nothing.
DISTIL_MAX_FACTS = 3
DISTIL_MAX_CHARS = 200

SKILL_EXTRACTION = _env("AGENT_SKILL_EXTRACTION", "on").strip().lower() not in (
    "0", "off", "false")


# The floor rejects a file carrying no procedure; the ceiling refuses a whole
# source file, which would eventually overflow the index and brick a run.
EXTRACT_MIN_CHARS = 80
EXTRACT_MAX_CHARS = 4_000


# --- the interface (FR-701) -------------------------------------------------

# Both fall back rather than raise on a name they do not know - a typo here
# must not be the reason the terminal will not open. agent/ui/theme.py holds
# the names and does the falling back.
TUI_THEME = _env("AGENT_TUI_THEME", "noesis-mono")
TUI_TRANSPARENT = _env("AGENT_TUI_TRANSPARENT", "gaps")
