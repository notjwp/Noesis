"""CLI: the live display and the approval prompt.

Worth testing rather than eyeballing, because this is where consent is decided.
A prompt that reads a mistyped key as approval is a security defect, and the
only way that surfaces in manual testing is by accident.
"""
import builtins
from types import SimpleNamespace

import pytest

from agent import cli


def answers(monkeypatch, *replies):
    """Feed scripted keystrokes to input()."""
    queue = list(replies)
    monkeypatch.setattr(builtins, "input", lambda _="": queue.pop(0))
    return queue


PAUSE = {"call": {"id": "t1", "name": "run_shell",
                  "input": {"command": "rm -rf build", "timeout": 120}},
         "reason": "run_shell classified destructive"}


# ==================================================================== approval

@pytest.mark.parametrize("keystroke", ["a", "allow", "A", "  a  "])
def test_allow_variants_approve(monkeypatch, keystroke):
    answers(monkeypatch, keystroke)
    assert cli.ask_human(PAUSE) == "allow"


@pytest.mark.parametrize("keystroke", ["d", "deny", "D"])
def test_deny_variants_reject(monkeypatch, keystroke):
    answers(monkeypatch, keystroke)
    assert cli.ask_human(PAUSE) == "deny"


def test_quit_is_distinct_from_deny(monkeypatch):
    """Quitting stops the session; denying answers the question and continues.
    Collapsing them would silently turn a walk-away into a policy decision."""
    answers(monkeypatch, "q")
    assert cli.ask_human(PAUSE) == cli.QUIT
    assert cli.QUIT != "deny" and cli.QUIT != "allow"


def test_unrecognised_input_reprompts_rather_than_guessing(monkeypatch):
    """NFR-801: one keystroke resolves it - but only a RECOGNISED one. A typo
    must never be read as consent."""
    remaining = answers(monkeypatch, "yes", "maybe", "", "a")
    assert cli.ask_human(PAUSE) == "allow"
    assert remaining == [], "every answer should have been consumed"


def test_no_terminal_means_deny(monkeypatch):
    """Silence is not consent. Piped or detached, the answer is no."""
    def raise_eof(_=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert cli.ask_human(PAUSE) == "deny"


def test_prompt_shows_every_argument_in_full(monkeypatch, capsys):
    """FR-306: unabbreviated. A prompt that elides the dangerous half of a
    command manufactures consent instead of obtaining it."""
    answers(monkeypatch, "d")
    cli.ask_human({"call": {"id": "t1", "name": "run_shell",
                            "input": {"command": "rm -rf " + "deep/" * 40}},
                   "reason": "run_shell classified destructive"})
    shown = capsys.readouterr().out
    assert "rm -rf " + "deep/" * 40 in shown
    assert "..." not in shown


# ================================================================ live display

def test_live_trace_still_behaves_as_a_list():
    """The harness passes a plain list and must stay unaffected by this
    subclass existing."""
    trace = cli.LiveTrace()
    trace.append({"kind": "tool", "tool": "read_file", "summary": "a.py",
                  "duration_ms": 12, "is_error": False, "verdict": "auto"})
    assert len(trace) == 1 and trace[0]["tool"] == "read_file"


def test_live_trace_renders_each_event_as_it_arrives(capsys):
    trace = cli.LiveTrace()
    trace.append({"kind": "model", "billed_tokens": 1500})
    trace.append({"kind": "tool", "tool": "run_shell", "summary": "pytest -q",
                  "duration_ms": 2400, "is_error": True, "verdict": "auto",
                  "spill_path": ""})
    trace.append({"kind": "terminal", "verdict": "done", "turns": 3,
                  "spent_tokens": 5320})
    out = capsys.readouterr().out

    assert "turn 1/" in out and "1,500 tokens" in out
    assert "run_shell" in out and "pytest -q" in out and "2.4s" in out
    assert "ERROR" in out
    assert "done" in out and "5,320 tokens" in out


def test_denied_and_spilled_calls_are_visibly_marked(capsys):
    trace = cli.LiveTrace()
    trace.append({"kind": "tool", "tool": "write_file", "summary": "../escape.txt",
                  "duration_ms": 0, "is_error": True, "verdict": "deny",
                  "spill_path": ""})
    trace.append({"kind": "tool", "tool": "run_shell", "summary": "ls -R /",
                  "duration_ms": 30, "is_error": False, "verdict": "auto",
                  "spill_path": "/workspace/.agent/artifacts/abc.txt"})
    out = capsys.readouterr().out
    assert "DENIED" in out
    assert "[spilled]" in out


def test_token_counter_accumulates_across_turns(capsys):
    trace = cli.LiveTrace()
    trace.append({"kind": "model", "billed_tokens": 1000})
    trace.append({"kind": "model", "billed_tokens": 500})
    assert "turn 2/" in capsys.readouterr().out and trace.tokens == 1500


def test_resumed_session_continues_the_turn_count(monkeypatch, capsys):
    """A resumed run is one task continuing, not a new one. Restarting the
    counter at 1 while the run is really on turn 9 misreports how close it is
    to the turn cap - the one number the display exists to convey."""
    class FakeApp:
        checkpointer = None
        def get_state(self, _cfg):
            from types import SimpleNamespace
            return SimpleNamespace(values={"turns": 8, "spent_tokens": 42_000,
                                           "messages": [{"role": "user",
                                                         "content": "fix it"}]})
        def invoke(self, payload, cfg):
            assert payload is None, "resume must not seed a fresh state"
            cfg["configurable"]["trace"].append({"kind": "model", "billed_tokens": 100})
            return {"verdict": "done"}

    cli.run_session(None, "t-resume", FakeApp())
    out = capsys.readouterr().out
    assert "resumed at turn 8" in out and "42,000 tokens" in out
    assert "turn 9/" in out, "the next turn must be 9, not 1"


# ================================================================== live trace

def test_a_sink_receives_every_entry_and_the_counters_still_advance():
    """The TUI swaps the RENDERING, not the bookkeeping.

    Had the counters stayed inside the printer, a sink would receive every entry
    while the header it feeds sat at turn 0 forever - and nothing would fail.
    """
    seen = []
    trace = cli.LiveTrace(sink=seen.append)
    trace.append({"kind": "model", "billed_tokens": 900})
    trace.append({"kind": "tool", "tool": "read_file"})
    trace.append({"kind": "model", "billed_tokens": 100})

    assert [e["kind"] for e in seen] == ["model", "tool", "model"]
    assert (trace.turn, trace.tokens) == (2, 1_000)
    # Still a list. Subclassing one is the entire live-output mechanism, and
    # graph.py must keep seeing nothing but `trace.append(...)`.
    assert len(trace) == 3


def test_the_default_sink_still_prints(capsys):
    """No sink means the CLI's own renderer, unchanged."""
    trace = cli.LiveTrace()
    trace.append({"kind": "terminal", "verdict": "done", "turns": 3,
                  "spent_tokens": 42})
    assert "done" in capsys.readouterr().out


# ===================================================================== threads

class FakeApp:
    """The checkpointer surface `_thread_ids` and `_thread_rows` actually use."""

    def __init__(self, states: dict):
        self._states = states
        tuples = [SimpleNamespace(config={"configurable": {"thread_id": tid}})
                  for tid in states]
        # Listed twice on purpose: the checkpointer yields one tuple per
        # checkpoint, not per thread, and _thread_ids has to dedupe.
        self.checkpointer = SimpleNamespace(list=lambda _: tuples + tuples)

    def get_state(self, cfg):
        return SimpleNamespace(
            values=self._states[cfg["configurable"]["thread_id"]])


def test_thread_rows_returns_data_the_picker_and_the_printer_share():
    app = FakeApp({
        "aaa11111": {"messages": [{"role": "user", "content": "fix the tests"}],
                     "verdict": "done", "turns": 7},
        "bbb22222": {},
    })
    rows = cli._thread_rows(app)

    assert rows == [
        {"id": "aaa11111", "verdict": "done", "turns": 7, "goal": "fix the tests"},
        # A thread with no state yet still appears, with a dash rather than a
        # crash: an interrupted first turn is exactly when you want to find it.
        {"id": "bbb22222", "verdict": "-", "turns": 0, "goal": ""},
    ]


def test_list_threads_prints_exactly_those_rows(capsys):
    """One traversal, two presentations. If these ever drift, --list and the
    TUI picker start disagreeing about what a thread was for."""
    app = FakeApp({"aaa11111": {"messages": [{"role": "user", "content": "fix it"}],
                                "verdict": "stuck", "turns": 12}})
    assert cli.list_threads(app) == 0
    out = capsys.readouterr().out
    for field in ("aaa11111", "stuck", "12", "fix it"):
        assert field in out


# ======================================================== the plan prompt

PLAN_PAUSE = {"plan": ["Read tests/test_export.py",
                       "Add a CSV writer to ledger/export.py",
                       "Run pytest -q until green"],
              "reason": "plan ready for review"}


@pytest.mark.parametrize("keystroke", ["a", "accept", "A", "  a  "])
def test_accept_variants_adopt_the_plan(monkeypatch, keystroke):
    answers(monkeypatch, keystroke)
    assert cli.ask_plan(PLAN_PAUSE) == "accept"


def test_revise_asks_why_and_carries_the_answer_back(monkeypatch):
    """The note travels back as an ordinary user message, so it is worth a
    second prompt - 'revise' alone tells the planner nothing."""
    answers(monkeypatch, "r", "use edit_file, not write_file")
    assert cli.ask_plan(PLAN_PAUSE) == "use edit_file, not write_file"


def test_revise_with_no_reason_still_revises(monkeypatch):
    answers(monkeypatch, "r", "")
    assert cli.ask_plan(PLAN_PAUSE) == "revise"


def test_unrecognised_input_reprompts_rather_than_adopting(monkeypatch):
    remaining = answers(monkeypatch, "yes", "ok", "", "a")
    assert cli.ask_plan(PLAN_PAUSE) == "accept"
    assert remaining == []


def test_no_terminal_quits_rather_than_looping(monkeypatch):
    """The one place this differs from the tool prompt. There, silence denies
    and the run continues. Here `revise` would send the graph back to planning
    and ask again - with nothing on stdin that is an infinite loop, so the safe
    answer is to stop."""
    def raise_eof(_=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert cli.ask_plan(PLAN_PAUSE) == cli.QUIT


def test_every_step_is_shown(monkeypatch, capsys):
    """UR-05: see what it is about to do. A plan shown three steps short is not
    the plan that will run."""
    answers(monkeypatch, "a")
    cli.ask_plan(PLAN_PAUSE)
    out = capsys.readouterr().out
    for step in PLAN_PAUSE["plan"]:
        assert step in out


def test_ask_human_routes_a_plan_payload_to_the_plan_prompt(monkeypatch):
    """One interrupt channel, two payload shapes. Telling them apart by key is
    what lets the graph keep a single suspension mechanism."""
    answers(monkeypatch, "a")
    assert cli.ask_human(PLAN_PAUSE) == "accept"

# ===== .env reached only containers, so anything else started with no API key


def _load(tmp_path, text):
    from agent.__main__ import _load_env

    env = tmp_path / '.env'
    env.write_text(text, encoding='utf-8')
    return _load_env(env)


def test_env_file_populates_the_environment(tmp_path, monkeypatch):
    """Until this existed only the harness passed .env in, via --env-file. A
    scheduled task or a plain shell started with no key and exited at once."""
    monkeypatch.delenv('AGENT_DEMO_KEY', raising=False)
    assert _load(tmp_path, 'AGENT_DEMO_KEY=abc123' + chr(10)) == 1

    import os
    assert os.environ['AGENT_DEMO_KEY'] == 'abc123'


def test_a_REAL_variable_wins_over_the_file(tmp_path, monkeypatch):
    """The harness exports overrides per case-run; a file that clobbered them
    would silently measure something other than what was asked for."""
    import os

    monkeypatch.setenv('AGENT_DEMO_KEY', 'from-the-shell')
    _load(tmp_path, 'AGENT_DEMO_KEY=from-the-file' + chr(10))

    assert os.environ['AGENT_DEMO_KEY'] == 'from-the-shell'


def test_comments_blanks_and_quotes(tmp_path, monkeypatch):
    """Docker's --env-file does not strip quotes, so files written for it carry
    them."""
    import os

    for name in ('AGENT_DEMO_A', 'AGENT_DEMO_B'):
        monkeypatch.delenv(name, raising=False)
    body = chr(10).join(['# a comment', '', 'AGENT_DEMO_A="quoted"',
                         'AGENT_DEMO_B=plain', 'not-a-pair'])

    assert _load(tmp_path, body) == 2
    assert os.environ['AGENT_DEMO_A'] == 'quoted'
    assert os.environ['AGENT_DEMO_B'] == 'plain'


def test_a_missing_env_file_is_not_an_error(tmp_path):
    from agent.__main__ import _load_env

    assert _load_env(tmp_path / 'nope.env') == 0


# ============================================================ MCP startup cost

def _args(**over):
    """argparse's namespace as _calls_model reads it."""
    base = dict(worker=False, tui=False, resume=None, goal=None)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("flag", ["worker", "tui"])
def test_the_paths_that_run_the_graph_start_the_servers(flag):
    assert cli._calls_model(_args(**{flag: True})) is True


@pytest.mark.parametrize("field,value", [("resume", "a1b2c3d4"), ("goal", "fix it")])
def test_resuming_and_a_bare_goal_start_the_servers(field, value):
    assert cli._calls_model(_args(**{field: value})) is True


def test_the_offline_flags_start_nothing():
    """--doctor, --channel-check, --tasks, --channel: database and disk only.

    Measured 2026-09-05: activation ran ahead of every flag, so a credentials
    probe exited 2 on a missing web-fetch tool and both scheduled tasks died
    at logon. --channel only queues and answers; --worker runs the graph.
    """
    assert cli._calls_model(_args()) is False


# ============================ the workspace guard (entry point, 2026-09-09)

def test_a_missing_workspace_names_itself_instead_of_WinError_267(tmp_path, capsys):
    """`/workspace` is the CONTAINER's mount point. Off it, Path('/workspace')
    .resolve() is drive-relative and does not exist, and the first tool call died
    as NotADirectoryError: [WinError 267] - which names nothing a user can act on."""
    import pytest

    from agent.__main__ import _require_workspace

    with pytest.raises(SystemExit) as exit_:
        _require_workspace(tmp_path / "nope", [])

    assert exit_.value.code == 2
    said = capsys.readouterr().err
    assert "workspace does not exist" in said
    assert "AGENT_WORKSPACE" in said
    assert "--doctor" in said


def test_an_existing_workspace_starts_normally(tmp_path):
    from agent.__main__ import _require_workspace

    _require_workspace(tmp_path, [])          # returns, does not raise


def test_the_guard_never_blocks_the_command_that_REPORTS_it(tmp_path, capsys):
    """--doctor exists to say the workspace is missing. A guard that stops it
    running is worse than the fault it guards against."""
    from agent.__main__ import _require_workspace

    _require_workspace(tmp_path / "nope", ["--doctor"])   # must not raise
    assert capsys.readouterr().err == ""


# ============================== the `noesis` command (2026-09-10)

def test_the_noesis_command_points_at_the_entry_that_loads_env():
    """`noesis` must land where `python -m agent` lands. Pointed at
    agent.cli:main instead it would skip _load_env and the setup wizard, and
    start with no API key - the defect recorded from when .env reached only
    containers."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    declared = (root / "pyproject.toml").read_text(encoding="utf-8")
    target = re.search(r'^noesis\s*=\s*"([^"]+)"', declared, re.M)

    assert target, "pyproject declares no `noesis` script"
    assert target.group(1) == "agent.__main__:main"


def test_the_installer_produces_a_noesis_that_runs(tmp_path):
    """scripts/install.sh, end to end, in a fresh virtualenv: the packaging
    (build backend, packages.find, the console script) and the script's own
    logic - which interpreter, in-checkout or clone, venv or user site, where
    the command landed. Editable installs write into the project directory, so
    the tree is copied first; the source mount is read-only.

    Offline: the venv sees the image's site-packages, so every dependency is
    already satisfied and pip fetches nothing; the build backend is baked into
    the image for exactly this test. `noesis --doctor` is the smoke check
    because it needs no workspace, no key and no TTY - and with no TTY the
    setup wizard stays closed."""
    import os
    import pathlib
    import shutil
    import subprocess
    import sys

    if os.name == "nt":
        pytest.skip("the installer is verified by hand on Windows; see the changelog")
    root = pathlib.Path(__file__).resolve().parent.parent
    src = tmp_path / "Personal_Agent"
    src.mkdir()
    for name in ("pyproject.toml", "agent", "prompts", "scripts"):
        item = root / name
        (shutil.copytree if item.is_dir() else shutil.copy)(item, src / name)

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
                   check=True, capture_output=True)
    # The image sets PIP_USER=1 so the agent's own `pip install` lands on a
    # writable root. A user's machine does not; drop it or the venv branch
    # under test quietly performs the user-site branch instead.
    env = {k: v for k, v in os.environ.items() if k not in ("PIP_USER", "PYTHONUSERBASE")}
    env = {**env,
           "VIRTUAL_ENV": str(venv),
           "PATH": f"{venv / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
           # "false", not "1": pip maps this env var onto the option's DEST,
           # `build_isolation`, so "1" leaves isolation on. A known pip quirk.
           "PIP_NO_BUILD_ISOLATION": "false",
           "PIP_DISABLE_PIP_VERSION_CHECK": "1",
           "AGENT_HOME": str(tmp_path / "home")}

    done = subprocess.run(["sh", "scripts/install.sh"], cwd=src, env=env,
                          capture_output=True, text=True, timeout=300)

    assert done.returncode == 0, done.stdout[-800:] + done.stderr[-800:]
    assert f"installing from {src}" in done.stdout, "it should install in place, not clone"
    assert "Run:  noesis" in done.stdout, done.stdout[-400:]
    noesis = venv / "bin" / "noesis"
    assert noesis.is_file(), "the console script was not written"

    doctor = subprocess.run([str(noesis), "--doctor"], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=120)
    assert doctor.stdout.strip(), doctor.stderr[-400:]
    assert all(l.startswith(("ok", "FAIL", "--")) for l in doctor.stdout.splitlines() if l.strip()), (
        "noesis ran something other than the doctor:" + doctor.stdout[:300])


def test_bare_noesis_opens_the_tui(tmp_path, monkeypatch):
    """The whole request: type one word, the agent starts."""
    from agent import __main__ as entry

    monkeypatch.setattr(entry, "_load_env", lambda path: 0)
    monkeypatch.setattr(entry, "_require_workspace", lambda root, argv: None)
    seen = {}
    monkeypatch.setitem(__import__("sys").modules, "_probe", None)

    import agent.cli
    monkeypatch.setattr(agent.cli, "main",
                        lambda: seen.setdefault("argv", list(__import__("sys").argv[1:])) or 0)

    entry.main([])

    assert seen["argv"] == ["--tui"]


def test_noesis_with_arguments_is_python_m_agent(tmp_path, monkeypatch):
    """One entry point, not a second one to keep in step: `noesis --doctor` is
    the command it always was."""
    from agent import __main__ as entry

    monkeypatch.setattr(entry, "_load_env", lambda path: 0)
    monkeypatch.setattr(entry, "_require_workspace", lambda root, argv: None)
    seen = {}

    import agent.cli
    monkeypatch.setattr(agent.cli, "main",
                        lambda: seen.setdefault("argv", list(__import__("sys").argv[1:])) or 0)

    entry.main(["--doctor"])

    assert seen["argv"] == ["--doctor"], "arguments must pass through untouched"


# ================================ noesis --update (2026-09-10)

def _git(monkeypatch, results):
    """Record the commands run, and answer each from `results` in order."""
    import subprocess

    seen = []
    answers = list(results)

    def fake_run(cmd, *a, **k):
        seen.append(list(cmd))
        out, code = answers.pop(0) if answers else ("", 0)
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_update_refuses_a_dirty_tree(monkeypatch, capsys):
    """Pulling over uncommitted work loses it, and stashing silently is worse -
    it hides the loss until someone goes looking for the stash."""
    from agent import cli

    seen = _git(monkeypatch, [(" M agent/tools.py\n", 0)])
    code = cli.update()

    assert code != 0
    assert "uncommitted" in capsys.readouterr().err.lower()
    assert not any("pull" in " ".join(c) for c in seen), "it pulled anyway"


def test_update_pulls_fast_forward_only(monkeypatch, capsys):
    """--ff-only: a merge commit made by an update nobody watched is a surprise
    in the history, and a conflict must stop rather than half-apply."""
    from agent import cli

    seen = _git(monkeypatch, [("", 0), ("abc1234", 0), ("", 0),
                              ("def5678", 0), ("", 0)])
    assert cli.update() == 0

    pull = next(c for c in seen if "pull" in c)
    assert "--ff-only" in pull


def test_update_reinstalls_only_when_the_entry_point_moved(monkeypatch):
    """An editable install tracks the source tree, so a pull is enough. Only a
    changed pyproject - a new console script, a new dependency - needs pip."""
    from agent import cli

    # HEAD MOVED - otherwise update() returns early at "already up to date"
    # and never reaches the reinstall branch, which is how this test first
    # passed against a mutation that reinstalled unconditionally.
    unchanged = _git(monkeypatch, [("", 0), ("abc1234", 0), ("", 0),
                                   ("def5678", 0), ("agent/tools.py\n", 0)])
    cli.update()
    assert not any("pip" in " ".join(c) for c in unchanged)

    changed = _git(monkeypatch, [("", 0), ("abc1234", 0), ("", 0),
                                 ("def5678", 0), ("pyproject.toml\n", 0)])
    cli.update()
    assert any("pip" in " ".join(c) for c in changed), "pyproject moved, no reinstall"


def test_update_reports_what_changed(monkeypatch, capsys):
    """An update nobody can audit is an update nobody can undo."""
    from agent import cli

    _git(monkeypatch, [("", 0), ("abc1234", 0), ("", 0), ("def5678", 0), ("", 0)])
    cli.update()

    said = capsys.readouterr().out
    assert "abc1234" in said and "def5678" in said
