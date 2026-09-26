"""Practice-project runner and scorer.

Two modes, one file (the build spec permits a single harness file):

  OUTER (host)       python eval/harness.py [--split dev] [--case ID] [--runs N]
                     selects cases, spawns ONE container per case-run, aggregates,
                     prints `pass N/M`.

  INNER (container)  python eval/harness.py --run-case ID --run-index K --out DIR
                     runs setup, invokes the agent, runs check, writes one trace.

Why a container per case-run: `missing-dep` installs a package into site-packages.
Sharing one container across runs would leave it installed, so repeats of that case
would pass without the agent doing anything - quietly faking the baseline.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# `python eval/harness.py` puts eval/ on sys.path, not the repo root, so a
# module-level `from agent import ...` fails while the tests - which import
# this file with the path already set - pass. Green tests, broken program.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# Ceiling on the scored check. The slowest real-repository suite is ~59s, so this
# is roughly 10x headroom - generous enough never to fail honest work, tight
# enough that a non-terminating suite costs minutes rather than a whole run.
CHECK_TIMEOUT = 600

IMAGE = "personal-agent"
ENV_FILE = REPO / ".env"

# How the inner runner tells the outer driver what happened. A BLOCKED run
# never reached the model, so it is excluded and retried rather than scored -
# infrastructure failure is not a result.
COMPLETED, BLOCKED, MISCONFIGURED = 0, 3, 4
BLOCKED_RETRIES = 2

# A container that outlives this is hung, not slow, and the driver must not wait
# on it. AGENT_MAX_SECONDS is 1500 and is checked BETWEEN turns, so it cannot
# bound a call that never returns - measured, a run sat 112 minutes at 0.01% CPU
# and the `tools` split produced no rows in nine attempts. Twice the agent's own
# cap plus setup and check, so a slow run is never mistaken for a hung one.
CONTAINER_TIMEOUT = 3600

# Everything the container needs to reach a model. Forwarded by name, never by
# value, so a key is never written into a command line, a log or a trace.
# Credentials and endpoint selection. Forwarded by NAME, never by value, so a
# key never reaches a command line, a log or a trace.
_CREDENTIALS = (
    "ANTHROPIC_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY", "AGENT_API_KEY",
    "OPENAI_BASE_URL", "OPENAI_MODEL", "NIM_BASE_URL", "NIM_MODEL",
)

# Per-run overrides that are not read by agent/config.py at all.
_HARNESS_ONLY = ("AGENT_BUDGET", "AGENT_MAX_TURNS")

# spawn() sets these itself, AFTER the forwarded block, so forwarding them
# would put the host's value where the container's belongs.
_SET_BY_SPAWN = ("AGENT_WORKSPACE", "AGENT_HOME", "AGENT_SKILLS_DIR")

# DERIVED, not hand-kept, and that is the whole point. A hand-kept tuple
# omitted AGENT_PLAN (Stage 7), AGENT_WEB (Stage 4) and AGENT_REQUEST_TIMEOUT
# (2026-08-30); each time a scored run silently used the default while the
# driver believed it had set something. An inclusion list fails INVISIBLY. An
# exclusion list fails visibly - a variable that should not be forwarded shows
# up in the container, which is a symptom somebody sees.
def _forwarded() -> tuple:
    from agent import config as _c

    declared = [v for v in dict.fromkeys(_c.ENV_VARS) if v not in _SET_BY_SPAWN]
    return _CREDENTIALS + _HARNESS_ONLY + tuple(declared)


FORWARDED_ENV = _forwarded()

# --------------------------------------------------------------- egress (H)
#
# NFR-205 restricts egress to an allowlist rather than removing it: the agent
# container's only neighbour on its --internal network is the proxy.
EGRESS_NET = "personal-agent-egress"
EGRESS_PROXY = "personal-agent-egress-proxy"
EGRESS_IMAGE = "personal-agent-egress"
PROXY_PORT = 8888
EGRESS_DIR = REPO / ".agent" / "egress"

PROXY_DOCKERFILE = """FROM alpine:3.20
RUN apk add --no-cache tinyproxy
ENTRYPOINT ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
"""

PROXY_CONF = """User tinyproxy
Group tinyproxy
Port {port}
Listen 0.0.0.0
Timeout 600
Allow 0.0.0.0/0
ConnectPort 443
FilterDefaultDeny Yes
FilterExtended On
Filter "/etc/tinyproxy/allow.txt"
LogLevel Info
"""


def model_hosts() -> list[str]:
    """Hosts the proxy will permit, derived from the CONFIGURED provider.

    Derived rather than hardcoded so the allowlist follows the model choice: point
    the agent at a different endpoint and the permitted host moves with it. A
    hand-maintained list would drift and silently either over-permit or break runs.

    `.env` is read here because the harness runs on the host and does not otherwise
    load it - only the containers get it via --env-file.
    """
    from urllib.parse import urlparse

    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip().strip("\"'")
    values.update({k: v for k, v in os.environ.items() if v})

    from agent import config as settings
    base = (values.get("OPENAI_BASE_URL") or values.get("NIM_BASE_URL")
            or settings.OPENAI_BASE_URL)
    hosts = {urlparse(base).hostname}
    if (values.get("AGENT_PROVIDER") or settings.PROVIDER) == "anthropic":
        hosts.add("api.anthropic.com")
    return sorted(h for h in hosts if h)


# The web split's fixed-content server, on the egress network. Started by the
# harness, not by the case, so a case cannot silently depend on it.
FIXTURE_WEB = "fixture-web"
FIXTURE_WEB_IMAGE = "python:3.12-slim"
FIXTURE_WEB_DIR = REPO / "eval" / "fixtures" / "web-content"


def ensure_fixture_web() -> bool:
    """Serve the web split's content on the egress network. Idempotent.

    Resolvable by container name: the proxy pins its resolvers to 8.8.8.8/1.1.1.1
    (the Phase H DNS fix), and it was NOT obvious that a container name would still
    resolve through that. Measured before relying on it - `getent ahosts fixture-web`
    inside the proxy returns 172.19.0.2 for both STREAM and DGRAM.
    """
    if _docker("inspect", "-f", "{{.State.Running}}", FIXTURE_WEB).stdout.strip() == "true":
        return True
    _docker("rm", "-f", FIXTURE_WEB)
    started = _docker(
        "run", "-d", "--name", FIXTURE_WEB, "--network", EGRESS_NET,
        # Read-only, like everything else here: the fixture serves content and has
        # no business being writable by anything that reaches it.
        "-v", f"{FIXTURE_WEB_DIR.as_posix()}:/srv:ro",
        "-w", "/srv", FIXTURE_WEB_IMAGE,
        "python", "-m", "http.server", "80")
    if started.returncode != 0:
        print((started.stderr or started.stdout).strip()[-300:], file=sys.stderr)
        return False
    # Running is not the same as serving - the lesson the egress proxy taught at the
    # cost of a scored pass. Probe the operation, not the state.
    for _ in range(20):
        probe = _docker("run", "--rm", "--network", EGRESS_NET, FIXTURE_WEB_IMAGE,
                        "python", "-c",
                        "import urllib.request as u;"
                        f"print(u.urlopen('http://{FIXTURE_WEB}/index.html',"
                        "timeout=3).status)")
        if probe.stdout.strip() == "200":
            return True
        time.sleep(1)
    print(f"{FIXTURE_WEB} is running but not serving", file=sys.stderr)
    return False


def allowlist_for(case: dict) -> list[str]:
    """Hosts THIS case-run may reach: the model, plus whatever the case declares.

    Repo work needs no egress beyond the model and declares none, so the default is
    exactly what it was. A case that genuinely needs the web says so on its
    tasks.jsonl row and gets that widening for its own runs only - never for the
    whole suite, and never silently, because the manifest records the result.
    """
    return sorted(set(model_hosts()) | set(case.get("egress", ())))


def _write_allowlist(hosts) -> None:
    # Anchored regex per host: a bare substring would let evil-nvidia.com through.
    (EGRESS_DIR / "allow.txt").write_text(
        "\n".join("^" + h.replace(".", r"\.") + "$" for h in hosts) + "\n",
        encoding="utf-8", newline="")


# The allowlist currently IN FORCE, as opposed to the one last written to disk.
# They are not the same thing, and conflating them is what the measurement below
# caught: the file is read once, at startup.
_APPLIED: list[str] | None = None


def apply_allowlist(hosts) -> bool:
    """Make the running proxy enforce EXACTLY these hosts.

    The proxy is RECREATED rather than signalled. SIGHUP was the obvious move and
    was measured not to work: with `example.com` already present in the mounted
    filter file, tinyproxy refused it identically before and after the signal -
    `403`, and `refused on filtered domain "example.com"` in its own log. The
    mounted file does propagate from the host; it is simply only read at startup.

    A widening that silently fails is harmless by itself - it fails closed - but the
    manifest would have recorded the allowlist that was ASKED for, which is a row
    stating a condition that was never true. That is the failure worth spending two
    seconds of container restart to avoid.

    A no-op when the list is already in force, so the ordinary case - every
    repository case declaring no egress at all - costs nothing.
    """
    if hosts == _APPLIED:
        return True
    _docker("rm", "-f", EGRESS_PROXY)
    return ensure_egress(hosts)


def _docker(*args, **kw):
    return subprocess.run(["docker", *args], capture_output=True, text=True, **kw)


def ensure_egress(hosts=None) -> bool:
    """Bring up the restricted-egress network and proxy. Idempotent.

    Idempotent because a scored run may be resumed with --continue hours later; a
    half-present setup must converge rather than fail.

    `hosts` defaults to the model host alone. A case that declares its own egress
    passes a wider list through apply_allowlist(), which recreates the proxy - the
    filter file is read at startup and never again.
    """
    global _APPLIED
    hosts = model_hosts() if hosts is None else hosts
    EGRESS_DIR.mkdir(parents=True, exist_ok=True)
    # newline="" defeats Windows CRLF translation: tinyproxy reads a trailing
    # CR in "User tinyproxy" and dies with a syntax error on a file that looks
    # correct.
    (EGRESS_DIR / "tinyproxy.conf").write_text(
        PROXY_CONF.format(port=PROXY_PORT), encoding="utf-8", newline="")
    _write_allowlist(hosts)

    if _docker("image", "inspect", EGRESS_IMAGE).returncode != 0:
        if _docker("build", "-q", "-t", EGRESS_IMAGE, "-",
                   input=PROXY_DOCKERFILE).returncode != 0:
            return False
    if _docker("network", "inspect", EGRESS_NET).returncode != 0:
        _docker("network", "create", "--internal", EGRESS_NET)

    running = _docker("inspect", "-f", "{{.State.Running}}", EGRESS_PROXY)
    # Recreated unless THIS process started it: a proxy left by an earlier run
    # enforces an allowlist nobody can see from outside.
    if _APPLIED is None or running.stdout.strip() != "true":
        _docker("rm", "-f", EGRESS_PROXY)
        # Explicit resolvers. Docker's embedded DNS answered an A-only query but not
        # the AF_UNSPEC one tinyproxy makes, so every CONNECT failed with EAI_AGAIN.
        # This bounds where the proxy may go, not which resolver it asks.
        if _docker("run", "-d", "--name", EGRESS_PROXY, "--network", EGRESS_NET,
                   "--dns", "8.8.8.8", "--dns", "1.1.1.1",
                   "-v", f"{(EGRESS_DIR / 'tinyproxy.conf').as_posix()}:/etc/tinyproxy/tinyproxy.conf:ro",
                   "-v", f"{(EGRESS_DIR / 'allow.txt').as_posix()}:/etc/tinyproxy/allow.txt:ro",
                   EGRESS_IMAGE).returncode != 0:
            return False
        # The proxy needs an outward route; the agent container deliberately does not.
        _docker("network", "connect", "bridge", EGRESS_PROXY)

    # `docker run -d` returning 0 only means the container was CREATED. A bad config
    # exits immediately after, and returning success there would let a scored run
    # begin with no proxy at all - exactly what this preflight exists to prevent.
    for _ in range(20):
        if _docker("inspect", "-f", "{{.State.Running}}", EGRESS_PROXY).stdout.strip() == "true":
            if not _proxy_can_resolve():
                return False
            _APPLIED = list(hosts)
            return True
        time.sleep(0.5)
    logs = _docker("logs", EGRESS_PROXY)
    print((logs.stderr or logs.stdout).strip()[-400:], file=sys.stderr)
    return False


def _proxy_can_resolve() -> bool:
    """Running is not the same as usable.

    A live proxy that cannot resolve the model host fails every CONNECT, and the
    suite records blocked runs instead of a score. That happened: a scored run was
    stopped after every attempt on its first two case-runs blocked, with the
    container reporting Running throughout. `getent ahosts`
    is the AF_UNSPEC query tinyproxy itself makes; the A-only `getent hosts`
    succeeds even when that one fails, so probing the wrong one would have waved
    the broken proxy straight through.
    """
    for host in model_hosts():
        if not _docker("exec", EGRESS_PROXY, "getent", "ahosts", host).stdout.strip():
            print(f"egress proxy is running but cannot resolve {host}.", file=sys.stderr)
            print(f"Recreate it with: docker rm -f {EGRESS_PROXY}", file=sys.stderr)
            return False
    return True


# How many consecutive probes must succeed before a scored run starts, and how
# long to wait between them. One success is not a state: NVIDIA returned 503
# `Service temporarily overloaded` intermittently on 2026-08-31, a single probe
# passed, and the run launched on the strength of it blocked all three case-runs.
#
# WIDENED the same day, because three probes were not enough either. Three at 20s
# samples a ~45 SECOND window; the endpoint held for 45 seconds, passed 3/3, and
# 503'd on the very next request. Five at 45s samples ~3 minutes.
#
# No gate can cover a 30-minute run from its first minute, which is what
# BLOCKED_RUN_LIMIT below is for.
MODEL_PROBES = 5
MODEL_PROBE_GAP = 45.0


def model_answers(probes: int = MODEL_PROBES, gap: float = MODEL_PROBE_GAP) -> bool:
    """Whether the model endpoint is answering, and answering CONSISTENTLY.

    _proxy_can_resolve() checks that DNS works. This checks the operation the
    scored run actually performs, which is the same lesson one level up: a proxy
    reported Running for two hours while failing every request, and resolving a
    host is not the same as the model behind it replying.

    RUN IN A CONTAINER ON THE EGRESS NETWORK, not in this process. The first
    version probed from the host and failed with `no API key` on a machine whose
    scored runs work perfectly - the host has neither the .env file nor the proxy,
    so it was probing a different operation from a different place, which is the
    defect this preflight exists to catch.

    Costs about 40 tokens per probe against a run that costs 250k-1.1M, and buys
    back an hour of wall clock: at the 1-in-3 success rate measured on 2026-08-31
    a 45 case-run split would have spent that hour producing mostly blocked rows
    and a partial set nobody may quote.
    """
    cmd = ["docker", "run", "--rm", "--network", NETWORK,
           "-v", f"{REPO.as_posix()}:/app:ro"]
    if ENV_FILE.exists():
        cmd += ["--env-file", str(ENV_FILE)]
    for name in FORWARDED_ENV:
        if os.environ.get(name):
            cmd += ["-e", name]
    if NETWORK == EGRESS_NET:
        proxy = f"http://{EGRESS_PROXY}:{PROXY_PORT}"
        cmd += ["-e", f"HTTPS_PROXY={proxy}", "-e", f"HTTP_PROXY={proxy}", "-e", "NO_PROXY="]
    cmd += [IMAGE, "python", "-c", _PROBE_SRC, str(probes), str(gap)]

    done = subprocess.run(cmd, capture_output=True, text=True)
    for line in (done.stdout or "").splitlines():
        print(f"   {line}", flush=True)
    if done.returncode != 0 and done.stderr.strip():
        print(done.stderr.strip()[-300:], file=sys.stderr)
    return done.returncode == 0


_PROBE_SRC = (
    'import sys, time\n'
    'from agent.provider import ProviderMisconfigured, ProviderUnavailable, call_model\n'
    "m=[{'role':'user','content':[{'type':'text','text':'say ok'}]}]\n"
    'for i in range(int(sys.argv[1])):\n'
    '    if i: time.sleep(float(sys.argv[2]))\n'
    '    try:\n'
    "        call_model(m, 'Answer with one word.', [])\n"
    '    except ProviderMisconfigured as e:\n'
    "        print('MISCONFIGURED: %s' % e); raise SystemExit(4)\n"
    '    except ProviderUnavailable as e:\n'
    "        print('probe %d: %s' % (i+1, e)); raise SystemExit(3)\n"
    "    print('probe %d: ok' % (i+1), flush=True)\n"
)


# Scored runs sit on the internal egress network and reach the model only
# through the proxy. Overridable for local iteration, but outer() refuses to
# score a split without it - a number with egress silently open is not one.
NETWORK = os.environ.get("AGENT_NETWORK", EGRESS_NET)

# Running `python eval/harness.py` puts eval/ on sys.path, not the project root,
# so `import agent` would fail. Fixing it here rather than via PYTHONPATH keeps the
# harness working under every invocation, on host and container alike.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def load_cases() -> list[dict]:
    text = (REPO / "eval" / "tasks.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def count_tool_calls(messages: list[dict]) -> int:
    return sum(
        1
        for m in messages
        if m.get("role") == "assistant" and isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_use"
    )


PROTECTED = "test_*.py"

# What the kernel says when the agent tries to write outside its declared roots.
READONLY_MARKER = "Read-only file system"
# The path the OS names in the refusal: "[Errno 30] Read-only file system: '/app/x'"
REFUSED_PATH = re.compile(re.escape(READONLY_MARKER) + r":?\s*'([^']*)'")


def write_violations(messages: list[dict]) -> list[str]:
    """Paths the kernel refused a write to - one entry per refusal.

    NFR-201 is about attempts as much as outcomes: a write that failed only
    because the filesystem was immutable is still the agent reaching outside its
    workspace, and it must surface rather than being silently absorbed as one more
    failed tool call.

    The PATH is reported and not just a count, because the targets are not
    equivalent. A refused write to /usr is an agent confused about where it lives;
    a refused write to /app/eval/fixtures is an agent editing the thing that grades
    it. Before Phase K the second was not refused at all - the project was mounted
    writable - so it could never have appeared here, however hard it was looked for.
    """
    found = []
    for m in messages:
        if not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                continue
            text = str(b.get("content", ""))
            if READONLY_MARKER in text:
                named = REFUSED_PATH.search(text)
                found.append(named.group(1) if named else "?")
    return found


def restore_protected_tests(case: dict) -> list[str]:
    """Put the practice project's assertions back before scoring.

    The tests ARE the specification, so passing by editing them is not passing.
    Observed live: the agent overwrote tests/test_parser.py three times and never
    touched the module that actually had the bug.

    `conftest.py` is deliberately NOT protected - one practice project's bug
    genuinely lives there, and restoring it would make that case impossible.

    Test files the agent invented are removed too, and that is a fairness measure
    as much as an integrity one: a broken scratch test left in tests/ would fail
    the suite for a reason that has nothing to do with the case.

    Returns what changed, so a rewritten test surfaces in the trace as a wasted
    turn instead of vanishing.
    """
    from agent import config as settings

    src = REPO / "eval" / "fixtures" / case["id"] / "tests"
    dst = settings.WORKSPACE / "tests"
    touched: list[str] = []
    if not src.is_dir():
        return touched

    for original in sorted(src.glob(PROTECTED)):
        target = dst / original.name
        want = original.read_bytes()
        if not target.exists() or target.read_bytes() != want:
            touched.append(f"restored {original.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(want)

    allowed = {q.name for q in src.glob(PROTECTED)}
    if dst.is_dir():
        for extra in sorted(dst.glob(PROTECTED)):
            if extra.name not in allowed:
                touched.append(f"removed invented {extra.name}")
                extra.unlink()

    return touched


# --------------------------------------------------------------------- outer

def read_rows(out: Path) -> list[dict]:
    summary = out / "summary.jsonl"
    if not summary.exists():
        return []
    return [json.loads(l) for l in summary.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def latest_rows(rows: list[dict]) -> list[dict]:
    """One row per (id, run_index) - the last one written wins.

    summary.jsonl is append-only, so a retried case-run leaves its earlier blocked
    row in place. Counting both would double-count a single case-run and quietly
    move the headline number.
    """
    by_key: dict[tuple, dict] = {}
    for row in rows:
        by_key[(row["id"], row["run_index"])] = row
    return list(by_key.values())


def completed(rows: list[dict]) -> set[tuple]:
    """Case-runs that actually finished. A blocked row is NOT complete - retrying
    it is precisely what --continue is for."""
    return {(r["id"], r["run_index"]) for r in latest_rows(rows)
            if r.get("status", "ok") == "ok"}


MEDIAN_TOKEN_CEILING = 60_000     # NFR-402
RESULT_CHAR_CEILING = 6_000       # NFR-104, budgeted at a conservative 3 chars/token


def _ceilings(scored: list[dict]) -> list[str]:
    """Report the two cost ceilings against what actually happened.

    Reported rather than asserted, and using the PROVIDER's own token counts
    rather than a tokeniser library. tiktoken would be OpenAI's tokenisation
    applied to a Llama model - precision in appearance only - and the model-exact
    tokeniser is a heavy dependency for one bound. The provider reports real
    counts per call, so the run totals here are the truthful measure.

    The per-result ceiling stays in characters for the same reason NFR-602 forces
    it there in the unit tests: it is the quantity this system actually controls.
    """
    if not scored:
        return []
    tokens = sorted(r.get("tokens", 0) for r in scored)
    median = tokens[len(tokens) // 2]
    measured = [r["max_result_chars"] for r in scored if "max_result_chars" in r]

    def verdict(actual, ceiling):
        return "OK" if actual <= ceiling else "OVER"

    out = ["", "ceilings:"]
    out.append(f"  median tokens/case  {median:>8,} / {MEDIAN_TOKEN_CEILING:,}"
               f"   {verdict(median, MEDIAN_TOKEN_CEILING)}   (NFR-402)")
    if measured:
        worst_result = max(measured)
        out.append(f"  largest result      {worst_result:>8,} / {RESULT_CHAR_CEILING:,}"
                   f"   {verdict(worst_result, RESULT_CHAR_CEILING)}   chars, NFR-104")
    else:
        # Distinct from a measured zero. Runs predating this field recorded nothing,
        # and reporting "0 / 6,000 OK" would claim a check that never happened.
        out.append("  largest result       not recorded in this run   (NFR-104)")
    if median < MEDIAN_TOKEN_CEILING // 4:
        # Said out loud because it is the easiest number here to misread.
        out.append("  NB: a low median is not efficiency while runs terminate early")
    return out


def _progress(group: list[dict]) -> str:
    """Failing tests before -> after, per run. "?" where it could not be read."""
    befores = {r.get("failures_before") for r in group if r.get("failures_before") is not None}
    if not befores:
        return "-"
    start = sorted(befores)[-1]
    ends = "/".join("?" if r.get("failures_after") is None else str(r["failures_after"])
                    for r in group)
    return f"{start}->{ends}"


def summarise(rows: list[dict]) -> str:
    """Render the score. Pure - takes rows, returns text, touches nothing.

    Pure so it can be tested without running anything, which matters more here
    than usual: this function decides the headline number, and getting it wrong
    does not crash, it just reports something other than the truth.
    """
    rows = latest_rows(rows)
    scored = [r for r in rows if r.get("status", "ok") == "ok"]
    blocked = [r for r in rows if r.get("status") == "blocked"]

    head = f"pass {sum(1 for r in scored if r['pass'])}/{len(scored)}"
    if blocked:
        head += f"   ({len(blocked)} blocked, excluded - not counted as failures)"
    lines = [head]

    # Anything that makes the number untrustworthy goes ABOVE the table, not
    # buried in a trace nobody opens.
    warnings = []
    tampered = sum(1 for r in scored if r.get("tampered"))
    if tampered:
        warnings.append(f"{tampered} run(s) edited the tests they are judged by")
    if any(r["verdict"] == "setup-failed" for r in scored):
        warnings.append("a case failed SETUP - that is the rig, not the agent")
    violations = sum(r.get("write_violations", 0) for r in scored)
    if violations:
        where = sorted({p for r in scored for p in r.get("write_violation_paths", [])})
        warnings.append(
            f"{violations} attempted write(s) OUTSIDE the workspace, refused by the "
            f"kernel - NFR-201 violation" + (f": {', '.join(where)}" if where else ""))
    for w in warnings:
        lines.append(f"  ! {w}")

    models = sorted({f"{r.get('provider','?')}/{r.get('model','?')}" for r in scored})
    if len(models) > 1:
        lines.append(f"  ! MIXED PROVIDERS in one run: {', '.join(models)}")

    ids = sorted({r["id"] for r in scored})
    if ids:
        lines += ["", f"{'case':<16}{'pass':<7}{'verdicts':<24}{'turns':<14}"
                      f"{'tokens(med)':>11}  {'failures':<12}tamper"]
    for cid in ids:
        group = sorted((r for r in scored if r["id"] == cid),
                       key=lambda r: r["run_index"])
        counts: dict[str, int] = {}
        for r in group:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        lines.append(
            f"{cid:<16}"
            f"{sum(1 for r in group if r['pass'])}/{len(group):<5}"
            f"{' '.join(f'{v} x{n}' for v, n in sorted(counts.items())):<24}"
            # The raw counts, not a median: §9 asks for variance, and 3/10/4 shows
            # it where "median 4" conceals it.
            f"{'/'.join(str(r['turns']) for r in group):<14}"
            f"{int(statistics.median([r['tokens'] for r in group])):>11,}"
            # Partial progress: "6->1/1/6" says the agent fixed five of six failures
            # twice, which `pass 0/3` renders identical to having done nothing. A
            # RISING count is just as important - it means working tests were broken.
            f"  {_progress(group):<12}"
            f"{sum(r.get('tampered', 0) for r in group)}")

    lines += _ceilings(scored)

    dist: dict[str, int] = {}
    for r in scored:
        dist[r["verdict"]] = dist.get(r["verdict"], 0) + 1
    if dist:
        # Not decoration. This distribution is what earns the next layer:
        # `compact` dominating earns compaction, `stuck` at the cap earns the plan
        # node. It replaces the spec's prediction with a measurement.
        lines += ["", "verdicts: " + ", ".join(
            f"{v} {n}" for v, n in sorted(dist.items(), key=lambda kv: (-kv[1], kv[0])))]
    return "\n".join(lines)


def percentile(values: list[float], pct: float) -> float:
    """The pct-th percentile of `values`, interpolating between ranks.

    `statistics.quantiles` does the arithmetic. It cannot express the two
    degenerate cases, so they are handled here: an empty sample answers 0.0
    rather than raising, because a latency table that crashes on a run with no
    checkpoints is worse than one reporting a count of 0; and a single sample is
    its own percentile.

    n=100 gives 99 cut points, so the pct-th sits at index pct-1. The "inclusive"
    method interpolates across (N-1) ranks, which is what makes a two-element
    sample answer at its midpoint rather than a third of the way along.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    if pct >= 100:
        return float(max(values))
    return statistics.quantiles(values, n=100, method="inclusive")[int(pct) - 1]


def latency(values: list[float]) -> dict[str, float]:
    """count, p50, p95, p99 and max together, and COUNT is the load-bearing one.

    A p95 over four samples is not a p95. Reporting the figure without the
    sample size beside it invites exactly that reading, which is the whole
    reason this returns a dict instead of a number.
    """
    return {"count": len(values),
            "p50_ms": round(percentile(values, 50), 2),
            "p95_ms": round(percentile(values, 95), 2),
            "p99_ms": round(percentile(values, 99), 2),
            "max_ms": round(max(values), 2) if values else 0.0}


def overheads(trace) -> list[float]:
    """Framework cost per loop iteration, in ms - NFR-102's wording exactly.

    One iteration is one node. Its wall time minus whatever it spent waiting on
    something that is not the framework: the model for `act`, the tools for
    `execute`. Everything left is the loop itself, and gate/reflect/finish have
    nothing to subtract because they wait on nothing.
    """
    out, pending = [], 0.0
    for entry in trace:
        kind = entry.get("kind")
        if kind in ("model", "tool"):
            # A tool records duration_ms; the model records ms. Both are time the
            # framework was waiting rather than working.
            pending += entry.get("ms", entry.get("duration_ms", 0.0))
        elif kind == "node":
            out.append(max(entry.get("ms", 0.0) - pending, 0.0))
            pending = 0.0
    return out


def checkpoint_ms(trace) -> list[float]:
    return [e["ms"] for e in trace if e.get("kind") == "checkpoint"]


def peak_context_chars(trace) -> int:
    """The largest context the run ever held, whether or not compaction fired.

    Without this a `compact_count` of 0 says only "it did not happen" - not
    whether the run came within a thousand chars of the threshold or never got
    close. Those two readings call for opposite actions, so the number that
    separates them belongs on the row.
    """
    sizes = [e.get("chars", 0) for e in trace if e.get("kind") == "context"]
    return max(sizes) if sizes else 0


def first_token_p50(trace) -> float | None:
    """Median seconds to the first streamed token, or None if nothing streamed.

    NFR-101 is stated as a p50 and was recorded NOT MEASURABLE because the
    OpenAI-compatible path returned one block at the end. None is reported rather
    than 0.0: a run that never streamed has no latency, and a zero would pull the
    median down while looking like the target was met.
    """
    seen = sorted(e["first_token_s"] for e in trace
                  if e.get("kind") == "model" and e.get("first_token_s") is not None)
    if not seen:
        return None
    mid = len(seen) // 2
    return round(seen[mid] if len(seen) % 2 else (seen[mid - 1] + seen[mid]) / 2, 3)


def previous_run(out: Path) -> Path | None:
    """The most recent EARLIER run over the same population, or None.

    Same population, checked against the manifest, because a delta between a
    5-case dev run and a 6-case real run is not a delta - it is two numbers
    printed next to each other, which is exactly the confusion this project has
    already had to retract once.
    """
    try:
        want = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for candidate in sorted(out.parent.glob("*"), reverse=True):
        if candidate.name >= out.name or not (candidate / "summary.jsonl").exists():
            continue
        try:
            have = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if have.get("cases") == want.get("cases"):
            return candidate
    return None


def delta(rows: list[dict], before: list[dict], label: str) -> str:
    """FR-804: the change against the previous run, per case and in total.

    Pure, for the same reason summarise() is: it reports the thing the project is
    judged on, and a wrong answer here does not crash - it silently claims an
    improvement that did not happen.

    Only cases that MOVED are listed. A table where fourteen rows say 3/3 -> 3/3
    buries the one that says 3/3 -> 1/3.
    """
    scored = [r for r in latest_rows(rows) if r.get("status", "ok") == "ok"]
    was = [r for r in latest_rows(before) if r.get("status", "ok") == "ok"]
    if not scored or not was:
        return ""

    def by_case(group):
        out: dict[str, list[dict]] = {}
        for r in group:
            out.setdefault(r["id"], []).append(r)
        return out

    now, then = by_case(scored), by_case(was)
    passed = sum(1 for r in scored if r["pass"])
    passed_before = sum(1 for r in was if r["pass"])
    move = passed - passed_before

    lines = ["", f"delta vs {label}:   pass {passed_before}/{len(was)} -> "
                 f"{passed}/{len(scored)}   ({move:+d})"]

    for cid in sorted(set(now) & set(then)):
        a = sum(1 for r in then[cid] if r["pass"])
        b = sum(1 for r in now[cid] if r["pass"])
        if a != b:
            lines.append(f"  {cid:<24}{a}/{len(then[cid])} -> {b}/{len(now[cid])}"
                         f"   {b - a:+d}")

    # Cost moves even when the score does not, and a pass rate held at the same
    # number for 30% more tokens is a regression that no pass/fail column shows.
    med_now = int(statistics.median([r["tokens"] for r in scored]))
    med_then = int(statistics.median([r["tokens"] for r in was]))
    if med_then:
        lines.append(f"  {'tokens (median)':<24}{med_then:,} -> {med_now:,}"
                     f"   {(med_now - med_then) / med_then * 100:+.0f}%")

    gone = sorted(set(then) - set(now))
    fresh = sorted(set(now) - set(then))
    if gone:
        lines.append(f"  ! only in the previous run: {', '.join(gone)}")
    if fresh:
        lines.append(f"  ! new in this run: {', '.join(fresh)}")
    return "\n".join(lines)


def code_version() -> dict:
    """The commit a run measured, and whether the tree matched it.

    Added after real-humanize's last measurement could not be attributed to any
    code: be4c772 committed staged reverts 74 seconds after that run started, and
    the manifest recorded image, provider, model and egress but not a sha.

    `dirty` is the load-bearing half, and it is THREE-valued. A clean sha on a
    modified tree is the lie that invalidated the Cycles I/J comparison; `None`
    means git could not be asked, which is a different claim from "clean" and
    must never be collapsed into it. The first version returned False on failure
    and every manifest here claimed a clean tree - the same shape as AGENT_EGRESS
    defaulting to "restricted" where nothing ever set it.

    NOT `git status --porcelain`: measured at 30s in the container when the
    tree carried a gitignored 1 GB of third-party checkouts, which git stats
    even to decide they are ignored. Asking about tracked and untracked
    separately costs about 7s - though `diff` alone has been measured from 7s to
    36s depending on cache state, because eval/fixtures holds six vendored
    repositories and every tracked file must be stat-ed over a bind mount. The
    timeout is 120s and this runs ONCE per scored run, against 13-60 minutes of
    measurement; a slow honest answer beats a fast `dirty: False`.
    """
    import subprocess

    def git(*args) -> str | None:
        """stdout, or None when git could not answer. None is not empty."""
        try:
            done = subprocess.run(("git", *args), cwd=REPO, capture_output=True,
                                  text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    sha = git("rev-parse", "HEAD")
    if sha is None:
        return {"commit": "UNKNOWN", "dirty": None, "dirty_files": []}

    modified = git("diff", "--name-only", "HEAD")
    untracked = git("ls-files", "--others", "--exclude-standard")
    if modified is None or untracked is None:
        # A sha we trust and a tree we could not read. Saying "clean" here is the
        # exact failure this field exists to prevent.
        return {"commit": sha.strip(), "dirty": None, "dirty_files": []}

    files = sorted({line.strip() for line in (modified + untracked).splitlines()
                    if line.strip()})
    return {"commit": sha.strip(), "dirty": bool(files), "dirty_files": files[:20]}

def run_dir(args, cases) -> Path | None:
    """Choose the output directory, creating or resuming one.

    The manifest is what makes resuming safe: it records what the directory
    describes, so a second invocation cannot quietly pour a different population
    into the same score.
    """
    root = REPO / "eval" / "runs"
    want = {"split": args.split, "case": args.case,
            "cases": sorted(c["id"] for c in cases), "runs": args.runs}

    if not args.continue_:
        out = root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out.mkdir(parents=True, exist_ok=True)
        # Provider, model and network are RECORDED but kept out of the resume key: a
        # run continued after a model change would silently mix two measurements.
        from agent import config as _cfg
        (out / "manifest.json").write_text(
            json.dumps({**want, "image": IMAGE,
                        # WHICH CODE this measured. Recorded because it was not,
                        # and a run that cannot name its commit proves nothing.
                        "code": code_version(),
                        "provider": _cfg.PROVIDER,
                        "model": _cfg.OPENAI_MODEL if _cfg.PROVIDER != "anthropic" else _cfg.MODEL,
                        "network": NETWORK,
                        # Per case, because a case may declare its own egress and
                        # every number must state the egress it was measured under.
                        "egress": ({c["id"]: allowlist_for(c) for c in cases}
                                   if NETWORK == EGRESS_NET
                                   else f"UNRESTRICTED (AGENT_NETWORK={NETWORK})"),
                        "started": datetime.now(timezone.utc).isoformat()}, indent=2),
            encoding="utf-8")
        return out

    existing = sorted(root.glob("*/manifest.json"), key=_started)
    if not existing:
        print("nothing to continue: no previous run has a manifest", file=sys.stderr)
        return None
    out = existing[-1].parent
    have = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    differs = {k: (have.get(k), v) for k, v in want.items() if have.get(k) != v}
    if differs:
        print(f"refusing to continue {out.name}: it describes a different run",
              file=sys.stderr)
        for key, (was, now) in differs.items():
            print(f"    {key}: recorded {was!r}, requested {now!r}", file=sys.stderr)
        return None
    # Say which directory, always. Appending into the wrong one would corrupt a
    # baseline with no visible symptom.
    print(f"continuing {out.name}")
    return out


def _started(manifest: Path) -> str:
    """When that run began, by its own record. Sorting run directories by NAME
    put `control-20260922-e65d94b` after every timestamp, so --continue picked it
    every time and then refused it - measured 2026-09-25, no rows produced."""
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))["started"]
    except (OSError, ValueError, KeyError):
        return datetime.fromtimestamp(manifest.stat().st_mtime,
                                      timezone.utc).isoformat()


def await_exclusive_workspace(timeout: float = 900.0) -> bool:
    """Refuse to start a case-run while another agent container is still alive.

    Every case-run gets its OWN container, but they all bind-mount the SAME host
    workspace. Two overlapping containers therefore corrupt each other: `reset.sh`
    wipes the directory the other one is working in.

    Measured, and self-inflicted: wrapping the harness in `timeout` killed the
    python client while its `docker run` container kept going. The orphan finished
    later, mid-way through the NEXT case, and its tests appeared in a workspace
    belonging to a different repository - three runs of one cycle were invalidated
    that way. The tamper check caught it only because the leftover files looked
    like invented tests.

    Waiting rather than failing: an orphan finishes on its own, and a scored suite
    that aborts is worse than one that pauses.
    """
    deadline = time.monotonic() + timeout
    warned = False
    while time.monotonic() < deadline:
        running = _docker("ps", "--filter", f"ancestor={IMAGE}",
                          "--format", "{{.Names}}").stdout.split()
        if not running:
            return True
        if not warned:
            print(f"  waiting for {len(running)} agent container(s) to exit before "
                  f"starting: {' '.join(running)}", file=sys.stderr)
            warned = True
        time.sleep(5)
    print("  workspace still busy after waiting; refusing to start a case-run that "
          "would corrupt it", file=sys.stderr)
    return False


def agent_home(case: dict, run_index: int) -> Path:
    """A FRESH agent home on the host for one scored case-run.

    Wiped rather than reused, and that is the whole point. Phases M and N put memory
    and skills in here; a memory carried from case 1 into case 2 is the same
    contamination that already forced one container per case-run, where a shared
    container left `missing-dep`'s package installed and the repeat passed without
    the agent doing anything.

    The interactive CLI keeps a persistent home (~/.personal-agent) - only the
    scored suite starts blank, because only the scored suite is a measurement.
    """
    home = REPO / ".agent" / "homes" / f"{case['id']}-{run_index}"
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    return home


def spawn(case: dict, run_index: int, out: Path) -> int:
    if not await_exclusive_workspace():
        return BLOCKED
    # Named, because killing the CLIENT leaves the container running and the
    # orphan then corrupts the shared workspace mid-case. Three runs were lost
    # that way to `timeout`; a watchdog that repeats it is worse than none.
    container = f"noesis-run-{out.name}-{case['id']}-{run_index}"[:120]
    cmd = [
        "docker", "run", "--rm", "--name", container, "--network", NETWORK,
        # NFR-201, enforced by the kernel rather than asserted in a test. --read-only
        # makes the ROOT FILESYSTEM immutable and leaves bind mounts untouched, which
        # is why the project tree is mounted :ro separately.
        "--read-only", "--tmpfs", "/tmp:exec",
        "-v", f"{REPO.as_posix()}:/app:ro",
        "-v", f"{(REPO / 'eval' / 'runs').as_posix()}:/app/eval/runs",
        "-v", f"{(REPO / 'eval' / 'workspace').as_posix()}:/workspace",
        "-v", f"{agent_home(case, run_index).as_posix()}:/state",
        # The skill library this row was measured against, per case: the authoring
        # split must start EMPTY or its control passes on knowledge it was testing for.
        "-e", "AGENT_SKILLS_DIR=/app/eval/fixtures/"
              + case.get("skills_dir", "skills-library"),
        # EXPLICIT, not left to config.py's defaults. _SET_BY_SPAWN has always
        # claimed spawn() sets these; it did not, and --env-file is not the
        # forwarding path that list excludes. A .env carrying host paths - which
        # is now normal, the CLI reads it - then pointed every scored container
        # at a directory that does not exist inside it.
        "-e", "AGENT_WORKSPACE=/workspace",
        "-e", "AGENT_HOME=/state",
    ]
    # .env first, then the real environment, so an exported variable
    # deliberately overrides the file rather than the other way round.
    if ENV_FILE.exists():
        cmd += ["--env-file", str(ENV_FILE)]
    for name in FORWARDED_ENV:
        if os.environ.get(name):
            cmd += ["-e", name]
    # AGENT_EGRESS is what the ROW will claim, so the outer driver states it -
    # it used to default to "restricted" where nothing ever set it, and every row
    # asserted a restriction whether or not a proxy was in the path.
    if NETWORK == EGRESS_NET:
        # Re-applied per case-run, not once per suite: the allowlist is part of the
        # conditions this row was measured under, and it must be this case's.
        allowed = allowlist_for(case)
        if not apply_allowlist(allowed):
            print("could not apply this case's egress allowlist - refusing to run it "
                  "under whatever the previous case left behind.", file=sys.stderr)
            return BLOCKED
        cmd += ["-e", "AGENT_EGRESS=" + ",".join(allowed)]
        # httpx runs trust_env=True so the openai SDK picks these up unaided. NO_PROXY
        # is emptied deliberately - an exemption here would be a hole in the boundary.
        proxy = f"http://{EGRESS_PROXY}:{PROXY_PORT}"
        cmd += ["-e", f"HTTPS_PROXY={proxy}", "-e", f"HTTP_PROXY={proxy}", "-e", "NO_PROXY="]
    else:
        cmd += ["-e", f"AGENT_EGRESS=UNRESTRICTED (AGENT_NETWORK={NETWORK})"]
    cmd += [
        IMAGE, "python", "eval/harness.py",
        "--run-case", case["id"],
        "--run-index", str(run_index),
        "--out", f"/app/eval/runs/{out.name}",
    ]
    try:
        return subprocess.run(cmd, check=False,
                              timeout=CONTAINER_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        print(f"  {case['id']}[{run_index}] hung for {CONTAINER_TIMEOUT}s - "
              f"killing {container}", file=sys.stderr, flush=True)
        _docker("kill", container)
        return BLOCKED


# Consecutive case-runs that may block before the whole run is abandoned.
#
# Measured 2026-08-31: the endpoint died immediately after passing the preflight
# and the driver ground through case-run after case-run, each burning three
# attempts and three minutes of backoff, to produce a directory of blocked rows
# and `pass 0/0`. It did that twice, for about 45 minutes each time.
#
# Two is enough to distinguish an outage from one unlucky case: a case-run that
# blocks has already failed THREE attempts with backoff between them.
BLOCKED_RUN_LIMIT = 2


def outer(args) -> int:
    cases = [
        c for c in load_cases()
        if (args.case is None or c["id"] == args.case)
        and (args.split is None or c["split"] == args.split)
    ]
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2

    if any(FIXTURE_WEB in c.get("egress", ()) for c in cases) and not ensure_fixture_web():
        print("the web split needs its fixture server and it would not start.",
              file=sys.stderr)
        return 2

    if NETWORK == EGRESS_NET and not ensure_egress():
        print("egress proxy could not be started, and a scored run must not reach "
              "the network unrestricted (NFR-205).", file=sys.stderr)
        print("  Fix docker, or set AGENT_NETWORK=bridge to run WITHOUT the "
              "restriction - the result is then not a compliant scored run.",
              file=sys.stderr)
        return 2

    # AFTER the egress proxy, which the probe itself has to go through, and
    # BEFORE run_dir, so a refused preflight leaves no directory behind.
    if not args.no_preflight and not model_answers():
        print("the model endpoint is not answering consistently; refusing to "
              "start a scored run.", file=sys.stderr)
        print("  Nothing was written. Re-run when it recovers, or pass "
              "--no-preflight to measure anyway and accept blocked rows.",
              file=sys.stderr)
        return 2

    out = run_dir(args, cases)
    if out is None:
        return 2

    planned = [(c, i) for c in cases for i in range(args.runs)]
    already = completed(read_rows(out))
    todo = [(c, i) for c, i in planned if (c["id"], i) not in already]
    if already:
        print(f"{len(already)} of {len(planned)} case-runs already complete; "
              f"{len(todo)} to go")

    consecutive_blocked = 0
    for position, (case, run_index) in enumerate(todo):
        # Re-read, not `already` from the top: a driver killed mid-run leaves its
        # container running, and that container writes its own row. Measured
        # 2026-09-11 - the resumed driver would have spawned that row again.
        if (case["id"], run_index) in completed(read_rows(out)):
            print(f"-> {case['id']} run {run_index}: recorded meanwhile, skipped",
                  flush=True)
            continue
        print(f"-> {case['id']} run {run_index}", flush=True)
        for attempt in range(BLOCKED_RETRIES + 1):
            code = spawn(case, run_index, out)
            if code == MISCONFIGURED:
                print("\naborting: the provider is misconfigured. Fix it, then "
                      "re-run with --continue to keep the work done so far.",
                      file=sys.stderr)
                return 2
            if code != BLOCKED:
                break
            if attempt < BLOCKED_RETRIES:
                wait = max(args.pace, 1) * 4 * (attempt + 1)
                # Announced, never silent: an unexplained pause is indistinguishable
                # from a hang, which this project has already paid for twice.
                print(f"   blocked; waiting {wait}s before retry "
                      f"{attempt + 1}/{BLOCKED_RETRIES}", flush=True)
                time.sleep(wait)

        # A RUN THAT VANISHES IS WORSE THAN ONE THAT FAILS: the result line is printed
        # by the CONTAINER, so a container that dies without writing returns a code that
        # is neither BLOCKED nor MISCONFIGURED and the suite moves on silently. No row
        # is synthesised - inventing one for a half-executed run is a fabrication.
        if (case["id"], run_index) not in completed(read_rows(out)):
            print(f"   {case['id']} run {run_index}: NO ROW WRITTEN (exit {code}) "
                  f"- not counted. Re-run with --continue.",
                  file=sys.stderr, flush=True)

        # A case-run that blocked has already failed every attempt with backoff
        # between them. Several in a row is an outage, not bad luck, and grinding
        # through the rest produces a directory of blocked rows and nothing else.
        consecutive_blocked = consecutive_blocked + 1 if code == BLOCKED else 0
        if consecutive_blocked >= BLOCKED_RUN_LIMIT:
            print(f"{chr(10)}aborting: {consecutive_blocked} case-runs blocked in a "
                  f"row. The provider is down, not the agent.", file=sys.stderr)
            print("  Nothing further was attempted. Re-run with --continue when "
                  "it recovers.", file=sys.stderr)
            break

        if args.pace and position < len(todo) - 1:
            time.sleep(args.pace)

    rows = read_rows(out)
    print()
    print(summarise(rows))

    # FR-804: the number against the number before it. Printed here rather than
    # left to whoever reads two run directories side by side, because "did it
    # move" is the only question a tuning cycle asks.
    earlier = previous_run(out)
    if earlier is not None:
        try:
            before = [json.loads(line) for line
                      in (earlier / "summary.jsonl").read_text(
                          encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            before = []
        if before:
            print(delta(rows, before, earlier.name))
    print(f"\ntraces: {out}")

    missing = len(planned) - len(completed(rows))
    if missing:
        # A rig failure, not a score. `pass 0/5` is a legitimate result and exits 0;
        # case-runs that never produced a result must not look the same.
        print(f"INCOMPLETE: {missing} of {len(planned)} case-run(s) have no result. "
              f"Re-run with --continue.", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------- inner

def inner(args) -> int:
    case = next(c for c in load_cases() if c["id"] == args.run_case)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # shell=True is deliberate: `setup` and `check` are shell command strings from
    # tasks.jsonl, written by this project, not agent input. A timeout is mandatory
    # - an unbounded check once held a scored suite for 25 minutes.
    setup = subprocess.run((case.get("setups") or [case["setup"]])[0], shell=True,
                           cwd=REPO, capture_output=True, text=True)
    if setup.returncode != 0:
        # A broken rig, not a failed agent. Recorded distinctly so it can never be
        # mistaken for the agent missing the case.
        return record(out, case, args.run_index, passed=False, verdict="setup-failed",
                      seconds=0.0, state=None, note=setup.stderr[-2000:])

    # The failing-test count BEFORE the agent touches anything, so 6->1 is
    # distinguishable from 4->4 on a set where nothing passes.
    _, _, before = run_check(case)

    # Imported after setup so a rig failure needs no agent.
    from agent import mcp, memory, skills, tools
    from agent.graph import get_app, new_state
    from agent.provider import ProviderMisconfigured, ProviderUnavailable

    # Tools before the model, because a run whose tools never registered is a
    # different measurement from one where the model declined to use them.
    memory.activate()
    try:
        skills.activate()
    except skills.SkillIndexTooLarge as exc:
        # Fatal, not blocked: retrying cannot shrink an index, and every retry
        # would spend the overrun again on every turn.
        print(f"FATAL: {exc}", file=sys.stderr)
        return MISCONFIGURED
    try:
        mcp_tools = mcp.activate()
    except mcp.ToolBudgetExceeded as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return MISCONFIGURED
    except mcp.McpUnavailable as exc:
        record(out, case, args.run_index, passed=False, verdict="blocked",
               seconds=0.0, state=None, note=str(exc), status="blocked")
        return BLOCKED

    budget = int(os.environ.get("AGENT_BUDGET") or case["budget"])
    max_turns = int(os.environ.get("AGENT_MAX_TURNS") or case["max_turns"])
    if budget != case["budget"] or max_turns != case["max_turns"]:
        print(f"  overridden: budget {case['budget']:,} -> {budget:,}, "
              f"turns {case['max_turns']} -> {max_turns}", file=sys.stderr)
    # A case is either one goal or a CHAIN of sessions (Phase M). Each session
    # resumes the same thread id, which is what makes memory measurable.
    goals = case.get("sessions") or [case["goal"]]
    # Per-session setup (Phase O). Session 1 may have reference material in the
    # workspace that later sessions do not - which is the whole mechanism: memory
    # remembers THAT a file was read, only a skill remembers what it said.
    setups = case.get("setups") or [case["setup"]] * len(goals)
    trace: list[dict] = []          # nodes append here; see agent/graph.py _log()
    # Captured while the tool set is LIVE. record() runs after shutdown(), so reading
    # the exposure back then would report the built-ins alone and silently understate
    # every MCP run. Carried on the trace, like `tamper` and `model` already are.
    # A scored run is unattended, so `ask_user` returns NOBODY_THERE and the tool
    # can never be measured. A case that declares `answers` supplies a stand-in
    # person; every question is recorded, because a pass rate is not evidence for
    # a mechanism that did not fire.
    scripted = list(case.get("answers") or ())
    if scripted:
        def answer(question, choices, _left=scripted):
            reply = _left.pop(0) if _left else ""
            trace.append({"kind": "asked", "question": question[:200],
                          "choices": list(choices), "answer": reply})
            return reply
        tools.ASK = answer

    trace.append({"kind": "tools", **tool_exposure(), "mcp": mcp_tools,
                  "memory": bool(os.environ.get("AGENT_MEMORY", "on").strip().lower()
                                 not in ("0", "off", "false")),
                  "sessions": len(goals)})
    started = time.monotonic()
    # Summed across sessions. Each session starts a fresh state, so the last
    # one alone said 25,497 for a run that billed 205,867 (2026-09-11).
    spent = {"turns": 0, "spent_tokens": 0}
    try:
        for index, goal in enumerate(goals):
            if index:
                # Between sessions only. The first setup already ran above, and
                # re-running it here would also re-run it for ordinary cases.
                subprocess.run(setups[index], shell=True, cwd=REPO,
                               check=True, capture_output=True)
                trace.append({"kind": "session", "index": index, "goal": goal[:200]})
            state = new_state(goal, max_turns, budget)
            final = get_app().invoke(state, {"configurable": {
                "thread_id": f"{case['id']}-{args.run_index}-s{index}",
                "autonomous": True,  # every `confirm` becomes `deny`; nothing blocks
                "trace": trace,
            }})
            for key in spent:
                spent[key] += int(final.get(key) or 0)
        note = ""
    except ProviderMisconfigured as exc:
        # No row at all. Nothing was measured, and writing a row would imply
        # otherwise; the outer driver stops the whole suite on this code.
        print(f"FATAL: {exc}", file=sys.stderr)
        return MISCONFIGURED
    except ProviderUnavailable as exc:
        # A row, but no score. The outer driver retries this same case-run, and
        # aggregation keeps it out of the denominator.
        record(out, case, args.run_index, passed=False, verdict="blocked",
               seconds=time.monotonic() - started, state=None, note=str(exc),
               status="blocked")
        return BLOCKED
    except Exception as exc:
        # A crashed agent IS a result - our bug, a malformed tool call, a bad
        # request - and must be scored. Only the two cases above are excused.
        final, note = state, f"{type(exc).__name__}: {exc}"
    seconds = time.monotonic() - started
    # Before the check runs: a live server subprocess holding the port makes the
    # check fail for a reason that has nothing to do with the agent.
    mcp.shutdown()
    tools.stop_terminals()
    tools.ASK = None
    memory.deactivate()
    skills.deactivate()

    tampered = restore_protected_tests(case)
    if tampered:
        trace.append({"kind": "tamper", "files": tampered})

    # A timeout is MANDATORY. The agent can leave the workspace in a state where
    # the check hangs; without this the suite stalls instead of scoring a failure.
    code, check_out, after = run_check(case)
    return record(out, case, args.run_index, passed=code == 0,
                  verdict=final.get("verdict") or "none", seconds=seconds,
                  state={**final, **spent}, note=note or check_out[-2000:],
                  trace=trace, failures_before=before, failures_after=after)


def tool_exposure() -> dict:
    """What the exposed tool set costs per request.

    Recorded on every row because the cost is invisible otherwise. Measured before
    Phase L built anything: four built-in tools are 1,997 chars, and at 9.1 model
    calls per run that was already 23% of a median run - on a provider returning
    cache_read_tokens of 0 for every row, so nothing is amortised.
    """
    from agent import registry

    return {"schema_chars": len(json.dumps(registry.schemas())),
            "tools": sorted(registry.toolset())}


def failing_tests(output: str) -> int | None:
    """Failing-test count from a pytest summary line; None when it cannot be read.

    Pass/fail alone cannot tell 6->1 from 4->4 from 1->39, and on a set the agent
    scores zero on, that difference is the ONLY signal there is. Measured on the
    first real-repository baseline: one case fixed five of its six failures twice
    and was recorded identically to a run that did nothing at all, while another
    broke 38 tests that had been passing and was likewise recorded as a plain 0.

    Returns 0 for a green suite, so "no failures" and "could not tell" stay
    distinguishable - conflating them would quietly invent progress.
    """
    clean = re.sub(r"\[[0-9;]*m", "", output or "")
    found = re.findall(r"(\d+) failed", clean)
    if found:
        return int(found[-1])
    return 0 if re.search(r"\d+ passed", clean) else None


def run_check(case: dict) -> tuple[int, str, int | None]:
    """Run a case's check command. Returns (exit code, output, failing count).

    Bounded by CHECK_TIMEOUT: the agent can leave the workspace in a state where
    the suite never terminates, and an unbounded check hangs the whole scored run
    with no diagnosis. Measured: one run held for 25 MINUTES before it was killed
    by hand. A timeout is a FAIL - the suite genuinely did not pass.
    """
    try:
        done = subprocess.run(case["check"], shell=True, capture_output=True,
                              text=True, timeout=CHECK_TIMEOUT)
        return done.returncode, done.stdout, failing_tests(done.stdout)
    except subprocess.TimeoutExpired:
        print(f"  check timed out after {CHECK_TIMEOUT}s", file=sys.stderr)
        return 1, (f"check exceeded {CHECK_TIMEOUT}s and was killed: the workspace "
                   f"was left in a state where the test suite does not terminate"), None


def record(out: Path, case: dict, run_index: int, *, passed: bool, verdict: str,
           seconds: float, state: dict | None, note: str, trace=(),
           status: str = "ok", failures_before: int | None = None,
           failures_after: int | None = None) -> int:
    state = state or {}
    calls = [t for t in trace if t.get("kind") == "tool"]
    models = [t for t in trace if t.get("kind") == "model"]
    violations = write_violations(state.get("messages", []))
    exposure = next((t for t in trace if t.get("kind") == "tools"), {})

    # Recorded per row, not once per suite: the row is what survives, and a score
    # whose model is unknown cannot be compared to anything.
    from agent import config as settings
    model = settings.MODEL if settings.PROVIDER == "anthropic" else settings.OPENAI_MODEL

    row = {
        "id": case["id"],
        "run_index": run_index,
        # "ok" means measured - pass or fail. "blocked" means it never reached the
        # model, carries no score, and is excluded from the denominator.
        "status": status,
        "provider": settings.PROVIDER,
        "model": model,
        # Recorded per row: whether THIS run was egress-restricted. Stated by the
        # driver, which is the only component that knows what the network actually was.
        "egress": os.environ.get("AGENT_EGRESS", "UNKNOWN"),
        "pass": passed,
        "verdict": verdict,
        "turns": state.get("turns", 0),
        "tokens": state.get("spent_tokens", 0),
        # Kept apart from billed tokens: a cache read costs about a tenth of a fresh
        # one, so folding them together would misstate both.
        "cache_read_tokens": sum(m.get("cache_read_tokens", 0) for m in models),
        "model_calls": len(models),
        "tool_calls": len(calls) if trace else count_tool_calls(state.get("messages", [])),
        "spills": sum(1 for c in calls if c.get("spill_path")),
        "tool_errors": sum(1 for c in calls if c.get("is_error")),
        # Non-zero means the agent edited the assertions it is judged by.
        "tampered": sum(len(t.get("files", [])) for t in trace
                        if t.get("kind") == "tamper"),
        # What the tool set COST, not just what it did. Schemas are re-sent every
        # request and this provider caches nothing, so breadth is a per-turn tax.
        "memory": exposure.get("memory", False),
        "memory_chars": max((t.get("chars", 0) for t in trace
                             if t.get("kind") == "memory"), default=0),
        # Progressive disclosure, both halves. `skill_index_chars` is what was paid
        # on EVERY request; `skills_loaded` is what was actually opened, and the gap
        # between them is the argument for the pattern.
        "skills": bool(os.environ.get("AGENT_SKILLS", "on").strip().lower()
                       not in ("0", "off", "false")),
        "skill_index_chars": max((t.get("chars", 0) for t in trace
                                  if t.get("kind") == "skills"), default=0),
        "skills_loaded": sorted({t.get("summary", "") for t in trace
                                 if t.get("tool") == "load_skill"}),
        # Phase O's three numbers: what it WROTE, what it LOADED, and what was
        # extracted deterministically - the three are not interchangeable.
        "authoring": bool(os.environ.get("AGENT_SKILL_AUTHORING", "on").strip().lower()
                          not in ("0", "off", "false")),
        "skills_authored": sorted({t.get("summary", "") for t in trace
                                   if t.get("tool") == "learn"}),
        # Written by `finish` from a document the agent read, with no model call and
        # no decision by the agent. The counterpart to skills_authored: one counts
        # what the MODEL chose to keep, the other what the RULE kept.
        "skills_extracted": sorted({t.get("name", "") for t in trace
                                    if t.get("kind") == "skill"}),
        # From config, not re-derived from the env: this line once defaulted "off"
        # where config.py defaults "on", and every default row claimed extraction
        # was off while the agent extracted. Two definitions, and the wrong one
        # is the one that survives in the row.
        "extraction": settings.SKILL_EXTRACTION,
        # Which skill the case NEEDED. Three outcomes, not two: the right one, the
        # WRONG one, or none - and the middle is invisible in a pass rate while
        # being the thing that says the descriptions do not discriminate.
        "skill_expected": case.get("skill_expected", ""),
        # Planning, with its COST stated apart from its effect (FR-101, FR-105).
        # `plan_denied` records what the read-only gate refused during research - it is
        # how the pytest-refusal defect was found, in all twelve planning runs.
        # NFR-101, which had no number at all until the OpenAI path streamed.
        "first_token_p50": first_token_p50(trace) if trace else None,
        "compact_count": state.get("compact_count", 0),
        "compact_removed_pct": [t.get("removed_pct") for t in trace
                                if t.get("kind") == "compact"],
        "plan": bool(os.environ.get("AGENT_PLAN", "on").strip().lower()
                     not in ("0", "off", "false")),
        "plan_steps": state.get("plan", []),
        "plan_turns": state.get("plan_turns", 0),
        # NFR-102 and NFR-103, on every row rather than in a one-off script.
        # A latency number nobody re-runs stops being true, and the first sign of
        # a regression here is a drift nobody attributes to the day it started.
        "overhead_ms": latency(overheads(trace)),
        "checkpoint_ms": latency(checkpoint_ms(trace)),
        # How close this run came to compacting. 0 means the trace carried no
        # context entries at all (a blocked run), NOT a run with no context -
        # read it beside `turns`.
        "peak_context_chars": peak_context_chars(trace),
        "plan_denied": sorted({c.get("summary", "") for c in calls
                               if c.get("verdict") == "deny"
                               and "planning" in str(c.get("reason", ""))}),
        "sessions": exposure.get("sessions", 1),
        "schema_chars": exposure.get("schema_chars", 0),
        "schema_tokens_est": exposure.get("schema_chars", 0) // 3 * max(len(models), 1),
        # Which tools were exposed, so a row can never be compared against one
        # measured with a different set.
        "mcp": exposure.get("mcp", []),
        # The WHOLE exposed set, not just MCP's part: a control row and a treatment
        # row otherwise differ only by schema_chars, leaving the one condition the
        # comparison turns on unrecorded.
        "tools": exposure.get("tools", []),
        # Non-zero means the agent tried to write outside the workspace (NFR-201).
        "write_violations": len(violations),
        # Where, not just how many: /usr is confusion, /app/eval/fixtures is the
        # agent reaching for its own grader, and one number cannot say which.
        "write_violation_paths": sorted(set(violations)),
        # NFR-104's observable: the biggest result the model was actually shown,
        # in characters, after shrink().
        "max_result_chars": max((c.get("shrunk_bytes", 0) for c in calls), default=0),
        # Partial progress. `pass` is binary and hides everything short of a green
        # suite; these two make "fixed five of six" visible, and make a run that
        # BROKE working tests visible too - which matters just as much.
        "failures_before": failures_before,
        "failures_after": failures_after,
        "seconds": round(seconds, 2),
    }
    with (out / "summary.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")

    (out / f"{case['id']}-{run_index}.json").write_text(
        json.dumps({**row, "goal": case["goal"], "note": note,
                    "messages": state.get("messages", []),
                    "calls": calls, "trace": list(trace)}, indent=2),
        encoding="utf-8")

    label = "BLOCKED" if status == "blocked" else ("PASS" if passed else "FAIL")
    print(f"{case['id']} run {run_index}: {label} ({verdict})")
    return COMPLETED


def check_provider() -> int:
    """One tool-calling request against the configured endpoint.

    Worth its own command because the spec forbids parsing tool calls out of free
    text, so a model that will not emit a well-formed call has **no fallback** -
    the architecture simply fails, and it fails mid-run after spending quota. This
    project has watched exactly that: one open-weight model leaked raw
    `<tool_call>` markup as message text and the run ended there.

    Reports what the endpoint IS, not just whether it answered: a 200 with prose
    instead of a tool call is the failure mode that matters.
    """
    from agent import config as settings
    from agent.provider import call_model
    from agent.tools import SCHEMAS

    where = ("anthropic" if settings.PROVIDER == "anthropic"
             else settings.OPENAI_BASE_URL)
    model = (settings.MODEL if settings.PROVIDER == "anthropic"
             else settings.OPENAI_MODEL)
    print(f"provider : {settings.PROVIDER}")
    print(f"endpoint : {where}")
    print(f"model    : {model}")

    try:
        reply = call_model(
            [{"role": "user", "content": "List the files in the current directory."}],
            "You fix broken code. Use the tools available to you.",
            SCHEMAS, None)
    except Exception as exc:
        print(f"FAILED   : {type(exc).__name__}: {exc}", file=sys.stderr)
        return MISCONFIGURED

    calls = [b for b in reply.blocks if b.get("type") == "tool_use"]
    text = " ".join(b.get("text", "") for b in reply.blocks if b.get("type") == "text")
    if calls:
        print(f"tool call: OK - {calls[0]['name']}({json.dumps(calls[0].get('input', {}))[:60]})")
        print(f"tokens   : {reply.billed_tokens}")
        print("verdict  : USABLE")
        return COMPLETED

    print("tool call: NONE - the model replied with text instead", file=sys.stderr)
    print(f"           {text[:200]!r}", file=sys.stderr)
    if "<tool_call>" in text or "function" in text.lower():
        print("           it looks like a call leaked into the text; this model "
              "cannot be used, because tool calls are never parsed out of prose",
              file=sys.stderr)
    print("verdict  : NOT USABLE for this agent", file=sys.stderr)
    return MISCONFIGURED


def main() -> int:
    # The harness runs on the HOST, where nothing else loads .env - containers
    # get it via --env-file. Without this --check-provider reported "no API key"
    # against a working endpoint. Safe inside the container too: a real variable
    # always wins, and spawn() sets AGENT_WORKSPACE and AGENT_HOME with -e.
    from agent.__main__ import _load_env

    _load_env(ENV_FILE)

    p = argparse.ArgumentParser()
    p.add_argument("--split")
    p.add_argument("--case")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--pace", type=int, default=15,
                   help="seconds between case-runs; the free tier refuses bursts")
    # `continue` is a keyword, hence the dest.
    p.add_argument("--no-preflight", action="store_true",
                   help="skip the model probe and measure anyway, accepting "
                        "blocked rows against a flapping endpoint")
    p.add_argument("--continue", dest="continue_", action="store_true",
                   help="resume the newest run directory instead of starting one")
    p.add_argument("--check-provider", action="store_true",
                   help="send ONE tool-calling request and report whether the "
                        "configured endpoint answers correctly; spends no quota "
                        "beyond that single call")
    p.add_argument("--run-case")
    p.add_argument("--run-index", type=int, default=0)
    p.add_argument("--out")
    args = p.parse_args()
    if args.check_provider:
        return check_provider()
    return inner(args) if args.run_case else outer(args)


if __name__ == "__main__":
    raise SystemExit(main())
