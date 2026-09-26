"""Scoring arithmetic and provider-error classification.

A fifth test file, and the third stated deviation from the spec's three-file
`tests/` allowlist. The justification is narrow: **these functions decide the
headline number.** A wrong dedupe rule or a miscounted denominator does not
crash - it silently reports something other than the truth, which is the one
failure this project cannot absorb, because every later cycle is measured
against it.

`eval/` is not a package, so the harness is loaded by path rather than adding an
`__init__.py` the allowlist does not name.
"""
import json
import importlib.util
import pathlib
import sys

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "eval" / "harness.py"
_spec = importlib.util.spec_from_file_location("eval_harness", _PATH)
harness = importlib.util.module_from_spec(_spec)
sys.modules["eval_harness"] = harness
_spec.loader.exec_module(harness)


def row(cid, index, **over):
    base = {"id": cid, "run_index": index, "status": "ok", "provider": "nvidia",
            "model": "llama", "pass": False, "verdict": "done", "turns": 3,
            "tokens": 1_000, "tampered": 0}
    base.update(over)
    return base


# ================================================================= arithmetic

def test_blocked_runs_leave_the_denominator():
    """pass 4/13 with 2 blocked - never pass 4/15.

    A run that never reached the model measured nothing. Counting it as a failure
    understates the agent, and because every later change is compared against this
    number, the error would propagate silently forever.
    """
    rows = [row("a", 0, **{"pass": True}), row("a", 1), row("a", 2, status="blocked")]
    out = harness.summarise(rows)
    assert "pass 1/2" in out
    assert "1 blocked" in out and "not counted as failures" in out


def test_last_row_wins_for_a_retried_case_run():
    """summary.jsonl is append-only, so a retried run leaves its earlier blocked
    row behind. Counting both would double-count one case-run."""
    rows = [row("a", 0, status="blocked"), row("a", 0, **{"pass": True})]
    assert len(harness.latest_rows(rows)) == 1
    assert "pass 1/1" in harness.summarise(rows)


def test_a_case_passing_once_in_three_reports_one_of_three():
    """Never 'passing'. A case that passes 1 of 3 is not a passing case."""
    rows = [row("a", 0, **{"pass": True}), row("a", 1), row("a", 2)]
    assert "1/3" in harness.summarise(rows)


def test_completed_excludes_blocked_so_continue_retries_them():
    rows = [row("a", 0), row("a", 1, status="blocked")]
    assert harness.completed(rows) == {("a", 0)}


def test_continue_picks_the_latest_run_not_the_last_name(tmp_path):
    """MEASURED 2026-09-25: `--continue` sorted run directories by NAME, so
    `control-20260922-e65d94b` came after every timestamp and it resumed that,
    then refused it for describing a different run. A resume produced no rows."""
    import json

    for name, started in (("20260924T140618Z", "2026-09-24T14:06:18+00:00"),
                          ("control-20260922-e65d94b", "2026-09-22T15:44:10+00:00")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "manifest.json").write_text(
            json.dumps({"started": started}), encoding="utf-8")

    newest = max(tmp_path.glob("*/manifest.json"), key=harness._started)
    assert newest.parent.name == "20260924T140618Z"


def test_a_manifest_that_cannot_say_when_it_started_falls_back_to_its_mtime(tmp_path):
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    assert harness._started(tmp_path / "manifest.json")


def test_turn_counts_are_shown_raw_not_averaged():
    """The spec asks for variance across runs. 3/10/4 shows it; a median hides it."""
    rows = [row("a", 0, turns=3), row("a", 1, turns=10), row("a", 2, turns=4)]
    assert "3/10/4" in harness.summarise(rows)


def test_median_tokens_is_the_middle_value():
    rows = [row("a", i, tokens=t) for i, t in enumerate((100, 5_000, 300))]
    assert "300" in harness.summarise(rows)


def test_tampering_is_flagged_above_the_table():
    """A run that edited the tests it is judged by must be impossible to miss."""
    out = harness.summarise([row("a", 0, tampered=2, **{"pass": True})])
    assert "edited the tests" in out
    assert out.index("edited the tests") < out.index("case ")


def test_a_setup_failure_is_called_out_as_the_rig():
    assert "rig, not the agent" in harness.summarise(
        [row("a", 0, verdict="setup-failed")])


def test_mixing_providers_in_one_run_is_flagged():
    """A score describing two different models is not one measurement."""
    rows = [row("a", 0), row("a", 1, provider="ollama", model="qwen")]
    assert "MIXED PROVIDERS" in harness.summarise(rows)


def test_verdict_distribution_is_reported():
    """Not decoration: this distribution is what earns the next layer, replacing
    the spec's prediction with a measurement."""
    rows = [row("a", 0, verdict="done"), row("a", 1, verdict="stuck"),
            row("b", 0, verdict="done")]
    out = harness.summarise(rows)
    assert "done 2" in out and "stuck 1" in out


def test_an_empty_run_does_not_crash():
    assert "pass 0/0" in harness.summarise([])


# ============================================================== classification

def sdk_error(name, message="boom"):
    """A stand-in for an SDK exception. Classification is by NAME because both
    SDKs use identical names, so this is exactly what the real thing looks like."""
    return type(name, (Exception,), {})(message)


def test_a_404_on_the_model_endpoint_is_infrastructure_not_a_score():
    """A run that never reached the model measured nothing.

    Measured mid-cycle: a scored run died at turn 0 with 0 tokens on
    NotFoundError, and the same model answered a probe minutes later - the
    endpoint had blinked. Unclassified, it fell through to "a crashed agent is a
    real result" and was recorded as a failed case, understating the agent and
    corrupting the comparison, which is precisely what the standing rule forbids.

    Retryable rather than fatal on purpose: a genuinely wrong model name fails
    every attempt and is then excluded as blocked, which is visible and costs
    three fast 404s; treating it as fatal would abort a whole suite over a hiccup.
    """
    from agent.provider import ProviderUnavailable, _reraise_classified
    with pytest.raises(ProviderUnavailable):
        _reraise_classified(sdk_error("NotFoundError"))


@pytest.mark.parametrize("name", ["RateLimitError", "APITimeoutError",
                                  "APIConnectionError", "InternalServerError"])
def test_infrastructure_failures_are_retryable(name):
    from agent.provider import ProviderUnavailable, _reraise_classified
    with pytest.raises(ProviderUnavailable):
        _reraise_classified(sdk_error(name))


@pytest.mark.parametrize("name", ["AuthenticationError", "PermissionDeniedError"])
def test_credential_failures_are_fatal(name):
    """Retrying cannot help, and fifteen of these look exactly like an agent that
    can do nothing."""
    from agent.provider import ProviderMisconfigured, _reraise_classified
    with pytest.raises(ProviderMisconfigured):
        _reraise_classified(sdk_error(name))


@pytest.mark.parametrize("exc", [
    sdk_error("BadRequestError"),       # we sent something invalid
    ValueError("bad state"),            # ordinary bug
    RuntimeError("boom"),
])
def test_our_own_bugs_are_never_excused_as_infrastructure(exc):
    """The dangerous direction. Excusing a real defect as 'the network' would drop
    it from the denominator and hide it behind a run that looks merely unlucky."""
    from agent.provider import _reraise_classified
    assert _reraise_classified(exc) is None, "must fall through and stay loud"


def test_a_malformed_tool_call_is_a_result_not_an_outage():
    """The model answered - badly. That is precisely what the eval measures, and
    excluding it would make an incapable model look untested rather than poor."""
    from agent.provider import MalformedToolCall, _reraise_classified
    assert _reraise_classified(MalformedToolCall("no json")) is None


# ================================== the write boundary (NFR-201, Phase E2/K)

def _refusal(path):
    return [{"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "t1",
        "content": f"OSError: [Errno 30] Read-only file system: '{path}'",
        "is_error": True}]}]


def test_a_refused_write_is_counted_as_a_violation():
    """The scored suite runs read-only, so a write outside the declared roots fails
    at the kernel. That failure is still an ATTEMPT to leave the workspace and must
    be counted, not absorbed as one more ordinary tool error."""
    assert harness.write_violations(_refusal("/usr/local/lib/x.txt")) == [
        "/usr/local/lib/x.txt"]


def test_a_violation_names_the_path_it_targeted():
    """Phase K made the project tree read-only, so writes to the harness and to the
    fixtures now REACH this counter for the first time. A bare count cannot tell
    `/usr` (an agent confused about where it lives) from `eval/fixtures` (an agent
    reaching for the thing that grades it), and only one of those is alarming."""
    target = "/app/eval/fixtures/real-rich/tests/test_console.py"
    assert harness.write_violations(_refusal(target)) == [target]


def test_a_refusal_that_names_no_path_is_still_counted():
    """Never drop a violation because it was phrased unexpectedly. Unknown is a
    worse answer than a path, and a far better one than silence."""
    messages = [{"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "t1",
        "content": "OSError: Read-only file system", "is_error": True}]}]
    assert harness.write_violations(messages) == ["?"]


def test_ordinary_tool_errors_are_not_violations():
    """A missing file is not an escape attempt. Over-counting here would make the
    boundary check cry wolf and get ignored."""
    messages = [{"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "t1",
        "content": "FileNotFoundError: no such file: nope.py", "is_error": True}]}]
    assert harness.write_violations(messages) == []


def test_a_string_content_message_does_not_crash_the_scan():
    """The first message is the goal, a plain string, not a block list."""
    assert harness.write_violations([{"role": "user", "content": "fix it"}]) == []


def test_write_violations_are_flagged_above_the_table():
    out = harness.summarise([row("a", 0, write_violations=2,
                                 write_violation_paths=["/app/eval/tasks.jsonl"])])
    assert "OUTSIDE the workspace" in out and "NFR-201" in out
    # The path belongs in the warning, not only in a trace nobody opens.
    assert "/app/eval/tasks.jsonl" in out
    assert out.index("OUTSIDE the workspace") < out.index("case ")


def test_an_older_run_without_paths_still_summarises():
    """Rows written before Phase K carry no `write_violation_paths`. Re-reading an
    old run directory must not crash on the field that did not exist yet."""
    assert "NFR-201" in harness.summarise([row("a", 0, write_violations=1)])


# ================================ the two declared writable roots (Phase K)

def test_the_agent_home_is_outside_the_workspace():
    """It exists precisely BECAUSE reset.sh wipes the workspace between runs. Put
    it back inside and memory silently stops surviving - with no test failing and
    no error, which is exactly how this property would get lost."""
    from agent import config

    assert config.WORKSPACE not in config.STATE_DB.parents


def test_reset_leaves_the_agent_home_alone(tmp_path):
    """The end-to-end version of the property above, against the real script.

    Skipped where there is no bash. That is honest rather than sufficient - the
    scored runs execute this script inside the container, where bash always exists.
    """
    import os
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("reset.sh needs bash")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scratch.txt").write_text("left over from the last case")
    home = tmp_path / "state"
    home.mkdir()
    (home / "memory.db").write_text("learned last week")

    repo = pathlib.Path(__file__).resolve().parent.parent
    case = next(p.name for p in (repo / "eval" / "fixtures").iterdir() if p.is_dir())
    done = subprocess.run(
        [bash, str(repo / "scripts" / "reset.sh"), case],
        env={**os.environ, "AGENT_WORKSPACE": str(workspace)},
        capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert not (workspace / "scratch.txt").exists(), "the workspace should be wiped"
    assert (home / "memory.db").read_text() == "learned last week"


def test_each_scored_case_run_gets_a_blank_agent_home(tmp_path, monkeypatch):
    """A memory carried from case 1 into case 2 is the same contamination that
    forced one container per case-run: `missing-dep` would pass on the repeat
    without the agent doing anything."""
    monkeypatch.setattr(harness, "REPO", tmp_path)
    home = harness.agent_home({"id": "fix-import"}, 0)
    (home / "memory.db").write_text("what run 0 learned")

    assert harness.agent_home({"id": "fix-import"}, 1) != home
    assert not (harness.agent_home({"id": "fix-import"}, 0) / "memory.db").exists()


# ============================================= per-session egress (Phase K4)

def test_a_case_that_declares_nothing_reaches_only_the_model():
    """Repo work needs no web access, and the default must stay that narrow."""
    assert harness.allowlist_for({"id": "fix-import"}) == harness.model_hosts()


def test_a_declared_host_widens_only_the_case_that_declared_it():
    """`egress` on a tasks.jsonl row is per case, so a research task cannot quietly
    hand its allowlist to the repo cases that run after it."""
    widened = harness.allowlist_for({"id": "research", "egress": ["docs.python.org"]})
    assert "docs.python.org" in widened
    assert set(harness.model_hosts()) <= set(widened)
    assert "docs.python.org" not in harness.allowlist_for({"id": "fix-import"})


# ======================================= cost ceilings (NFR-104, NFR-402, E3)

def test_ceilings_are_reported_against_their_limits():
    rows = [row("a", i, tokens=t, max_result_chars=5_900)
            for i, t in enumerate((1_000, 3_266, 9_000))]
    out = harness.summarise(rows)
    assert "median tokens/case" in out and "3,266" in out and "60,000" in out
    assert "largest result" in out and "5,900" in out
    assert "OVER" not in out, "everything here is within its ceiling"


def test_breaching_a_ceiling_is_called_out():
    rows = [row("a", 0, tokens=99_000, max_result_chars=12_000)]
    out = harness.summarise(rows)
    assert out.count("OVER") == 2, "both ceilings breached, both must say so"


def test_a_low_median_is_not_reported_as_efficiency():
    """The trap this project already walked into: 3,266 against a 60,000 ceiling
    looks excellent and is not - runs are cheap because they quit early."""
    out = harness.summarise([row("a", 0, tokens=3_266)])
    assert "not efficiency" in out


def test_an_unmeasured_ceiling_says_so_rather_than_reporting_zero():
    """Rows predating the field recorded nothing. Printing '0 / 6,000 OK' would
    claim a check that never ran - the precise way a green dashboard lies."""
    out = harness.summarise([row("a", 0, tokens=1_000)])
    assert "not recorded" in out
    assert "0 / 6,000" not in out

def test_failing_tests_reads_the_pytest_summary():
    """Pass/fail hides everything short of a green suite.

    Measured on the first real-repository baseline: one case fixed five of its six
    failures on two of three runs and was recorded identically to a run that did
    nothing, while another broke 38 previously-passing tests and was likewise
    recorded as a plain 0. On a set the agent scores zero on, this delta is the
    only signal there is.
    """
    assert harness.failing_tests("4 failed, 689 passed, 69 skipped in 4.13s") == 4
    assert harness.failing_tests("693 passed, 69 skipped in 3.67s") == 0
    assert harness.failing_tests("[31m6 failed[0m, 1857 passed") == 6, "ANSI colour must not hide it"


def test_failing_tests_distinguishes_green_from_unreadable():
    """0 and None must never be conflated - that would invent progress."""
    assert harness.failing_tests("12 passed") == 0
    assert harness.failing_tests("") is None
    assert harness.failing_tests("Interrupted: no summary line") is None


def test_progress_column_shows_partial_credit():
    group = [{"failures_before": 6, "failures_after": a, "run_index": i}
             for i, a in enumerate([1, 1, 6])]
    assert harness._progress(group) == "6->1/1/6"
    assert harness._progress([{"failures_before": None, "failures_after": None}]) == "-"


# ================================================== FR-804: delta between runs

from eval.harness import delta


def _row(cid, run_index, passed, tokens=10_000, status="ok"):
    return {"id": cid, "run_index": run_index, "pass": passed, "status": status,
            "tokens": tokens, "verdict": "done" if passed else "stuck"}


def test_delta_reports_the_move_and_names_only_the_cases_that_changed():
    """A table where fourteen rows say 3/3 -> 3/3 buries the one that regressed."""
    before = [_row("alpha", 0, True), _row("alpha", 1, True),
              _row("beta", 0, False), _row("beta", 1, False)]
    now = [_row("alpha", 0, True), _row("alpha", 1, True),
           _row("beta", 0, True), _row("beta", 1, False)]

    out = delta(now, before, "20260101T000000Z")

    assert "pass 2/4 -> 3/4   (+1)" in out
    assert "beta" in out, "the case that moved is named"
    assert "alpha" not in out, "the case that did not move is not"


def test_delta_reports_a_regression_with_its_sign():
    before = [_row("alpha", 0, True), _row("alpha", 1, True)]
    now = [_row("alpha", 0, True), _row("alpha", 1, False)]

    out = delta(now, before, "prev")

    assert "(-1)" in out and "-1" in out


def test_delta_reports_cost_even_when_the_score_holds():
    """A pass rate held at the same number for 30% more tokens is a regression
    that no pass/fail column shows."""
    before = [_row("alpha", 0, True, tokens=10_000)]
    now = [_row("alpha", 0, True, tokens=13_000)]

    out = delta(now, before, "prev")

    assert "tokens (median)" in out
    assert "+30%" in out


def test_delta_flags_a_changed_population_rather_than_hiding_it():
    """Comparing a 5-case run against a 6-case run is two numbers printed next to
    each other, not a delta."""
    before = [_row("alpha", 0, True), _row("gone", 0, True)]
    now = [_row("alpha", 0, True), _row("fresh", 0, False)]

    out = delta(now, before, "prev")

    assert "only in the previous run: gone" in out
    assert "new in this run: fresh" in out


def test_delta_is_empty_when_there_is_nothing_to_compare():
    assert delta([_row("alpha", 0, True)], [], "prev") == ""


def test_delta_ignores_blocked_runs():
    """A blocked run measured nothing and must not count in either direction -
    the standing rule that keeps `pass 4/13, 2 blocked` from becoming `4/15`."""
    before = [_row("alpha", 0, True)]
    now = [_row("alpha", 0, True), _row("alpha", 1, False, status="blocked")]

    assert "pass 1/1 -> 1/1   (+0)" in delta(now, before, "prev")


def test_the_web_kill_switch_removes_the_tool(monkeypatch):
    """Stage 4's control run is a CONTROLLED comparison only if the same binary
    can be run with the tool removed. Phase L nearly shipped a capability claim on
    a case that passed without the tool at all."""
    from agent import config, registry
    from agent.tools import TOOLS, builtins

    assert "web_search" in builtins()
    assert "web_search" in registry.toolset()

    monkeypatch.setattr(config, "WEB_ENABLED", False)

    assert "web_search" not in builtins()
    assert "web_search" not in registry.toolset()
    # Still in TOOLS, deliberately: policy.sync() classifies from there, and a
    # tool that is un-offered must not also become un-classifiable.
    assert "web_search" in TOOLS
    # The other six are untouched - the switch removes one tool, not a category.
    assert len(builtins()) == len(TOOLS) - 1


def test_every_config_variable_is_forwarded_or_deliberately_excluded():
    """A DEFECT PAID FOR THREE TIMES: AGENT_PLAN (Stage 7), AGENT_WEB (Stage 4),
    AGENT_REQUEST_TIMEOUT (2026-08-30). Each was read by config.py and absent
    from a hand-kept FORWARDED_ENV, so a scored run silently used the default
    while the driver believed it had set something else.

    FORWARDED_ENV now DERIVES from config.ENV_VARS, so this asserts the
    derivation rather than a list: anything config reads is either forwarded or
    named in the exclusion set, and the exclusion set is small enough to read.
    """
    from agent import config
    from eval_harness import FORWARDED_ENV, _SET_BY_SPAWN

    declared = set(config.ENV_VARS)
    assert declared, "config.ENV_VARS is empty - the recording helper is bypassed"

    unreachable = declared - set(FORWARDED_ENV) - set(_SET_BY_SPAWN)
    assert not unreachable, (
        f"config.py reads {sorted(unreachable)} but no scored run can set it. "
        f"Either forward it or name it in _SET_BY_SPAWN.")


def test_the_recording_helper_is_not_bypassed():
    """ENV_VARS is only true if every AGENT_* read goes through _env(). A direct
    os.environ.get would not be recorded, and the derivation would silently miss
    it - the same invisible failure, one layer down."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "agent" / "config.py"
              ).read_text(encoding="utf-8")
    direct = set(re.findall(r'os\.environ\.get\("(AGENT_[A-Z_]+)"', source))
    assert not direct, (
        f"{sorted(direct)} bypass _env() and will not be forwarded")


def test_variables_spawn_sets_are_not_also_forwarded():
    """Forwarding one would put the HOST's value where the container's belongs -
    /workspace and /state differ between them."""
    from eval_harness import FORWARDED_ENV, _SET_BY_SPAWN

    assert not (set(_SET_BY_SPAWN) & set(FORWARDED_ENV))


# ===================== Stage 0: a run that vanishes, and how close it came
#
# Two instruments added 2026-08-29, both because a measurement was about to be
# taken that could not have been read.


def test_peak_context_chars_reports_the_largest_context_the_run_held():
    from eval_harness import peak_context_chars

    trace = [{"kind": "context", "chars": 12_000},
             {"kind": "model", "ms": 1.0},
             {"kind": "context", "chars": 41_500},
             {"kind": "context", "chars": 38_000}]

    assert peak_context_chars(trace) == 41_500


def test_peak_context_chars_is_zero_when_the_run_never_reached_reflect():
    """A blocked run has an empty trace. 0 here means "no context entries", which
    is why the field is read beside `turns` rather than on its own."""
    from eval_harness import peak_context_chars

    assert peak_context_chars([]) == 0
    assert peak_context_chars([{"kind": "model", "ms": 1.0}]) == 0


def test_a_vanished_case_run_is_not_counted_as_complete(tmp_path):
    """THE DEFECT THIS GUARDS, observed 2026-08-29.

    The result line is printed by the CONTAINER, not by the driver. A container
    that dies without writing - a docker kill, an OOM, exit 137 - returns a code
    that is neither BLOCKED nor MISCONFIGURED, so the driver's retry loop breaks
    and the suite moves on with the denominator quietly one smaller. A run that
    disappears is worse than one that fails, because a failure is visible.
    """
    from eval_harness import completed, read_rows

    out = tmp_path / "run"
    out.mkdir()
    (out / "summary.jsonl").write_text(
        json.dumps({"id": "case-a", "run_index": 0, "status": "ok"}) + "\n"
        + json.dumps({"id": "case-a", "run_index": 1, "status": "blocked"}) + "\n",
        encoding="utf-8")

    done = completed(read_rows(out))

    assert ("case-a", 0) in done
    assert ("case-a", 1) not in done, "a blocked run is not complete - that is what --continue is for"
    # run 2 never wrote anything at all. It must be absent, so the driver's
    # membership check fires and says so on stderr.
    assert ("case-a", 2) not in done


def test_the_vanished_run_check_uses_the_same_helpers_the_resume_path_does():
    """Not a new notion of "done". --continue already decides what to re-run from
    completed(read_rows(...)); a second, subtly different definition is how two
    parts of a harness come to disagree about what was measured."""
    import inspect

    import eval_harness

    source = inspect.getsource(eval_harness.outer)
    assert "NO ROW WRITTEN" in source
    assert "completed(read_rows(out))" in source


def _row_for(out, case_id, run_index, status="ok"):
    import json

    with (out / "summary.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": case_id, "run_index": run_index, "pass": True,
                             "verdict": "done", "status": status, "turns": 1,
                             "tokens": 1, "seconds": 1.0, "tool_calls": 0,
                             "model_calls": 1}) + chr(10))


def test_continue_skips_a_row_that_landed_while_it_was_running(monkeypatch,
                                                                tmp_path):
    """Measured 2026-09-11: the driver was killed for host memory while a
    container ran on. The container wrote its own row - and the resumed driver,
    having read completed() ONCE at start, spawned that same case-run again.
    A duplicate costs 10-20 minutes of quota on the real split."""
    import argparse

    import eval.harness as h

    cases = [{"id": "a", "goal": "g", "split": "x"}, {"id": "b", "goal": "g", "split": "x"}]
    spawned = []

    def fake_spawn(case, run_index, out):
        spawned.append((case["id"], run_index))
        _row_for(out, case["id"], run_index)
        if (case["id"], run_index) == ("a", 0):
            _row_for(out, "b", 0)                 # the orphan lands meanwhile
        return h.COMPLETED

    monkeypatch.setattr(h, "load_cases", lambda: cases)
    monkeypatch.setattr(h, "NETWORK", "bridge")
    monkeypatch.setattr(h, "run_dir", lambda args, cases: tmp_path)
    monkeypatch.setattr(h, "spawn", fake_spawn)
    monkeypatch.setattr(h, "previous_run", lambda out: None)
    args = argparse.Namespace(case=None, split="x", runs=1, pace=0,
                              no_preflight=True, continue_=True)

    assert h.outer(args) == 0
    assert ("b", 0) not in spawned, "re-ran a case-run whose row was already on disk"
    assert spawned == [("a", 0)]


def test_the_harness_runs_as_a_script_not_only_as_an_import():
    """A module-level `from agent import ...` works under pytest, which sets the
    path, and fails under `python eval/harness.py`, which puts eval/ on sys.path
    instead of the repo root. 494 tests passed while the entry point was broken -
    so this exercises the real one."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "eval/harness.py", "--help"],
                          cwd=repo, capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr[-500:]
    assert "ModuleNotFoundError" not in done.stderr


# ================================================ NFR-101: first-token latency


def test_the_p50_is_the_median_of_what_actually_streamed():
    trace = [{"kind": "model", "first_token_s": s} for s in (0.4, 0.9, 3.1)]
    from eval_harness import first_token_p50

    assert first_token_p50(trace) == 0.9


def test_an_even_count_averages_the_middle_two():
    trace = [{"kind": "model", "first_token_s": s} for s in (1.0, 2.0)]
    from eval_harness import first_token_p50

    assert first_token_p50(trace) == 1.5


def test_a_run_that_never_streamed_reports_None_not_zero():
    """A zero would pull the median down while looking like NFR-101 was met."""
    trace = [{"kind": "model", "first_token_s": None},
             {"kind": "context", "chars": 10}]
    from eval_harness import first_token_p50

    assert first_token_p50(trace) is None


def test_turns_that_did_not_stream_are_excluded_rather_than_counted_as_zero():
    trace = [{"kind": "model", "first_token_s": None},
             {"kind": "model", "first_token_s": 2.0}]
    from eval_harness import first_token_p50

    assert first_token_p50(trace) == 2.0


# ============================================ the manifest names its own code


def _git_returns(mapping):
    """A subprocess.run stand-in keyed on the git subcommand."""
    class Done:
        def __init__(self, code, out): self.returncode, self.stdout, self.stderr = code, out, ""
    def fake(cmd, **_kw):
        key = cmd[1]
        value = mapping.get(key, "")
        return Done(1, "") if value is None else Done(0, value)
    return fake


def _recorded(tmp_path, monkeypatch, **env):
    """One recorded row, with the environment as given and nothing else set."""
    import json

    import eval.harness as h

    for name in ("AGENT_SKILL_EXTRACTION",):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    import importlib

    from agent import config
    importlib.reload(config)
    try:
        h.record(tmp_path, {"id": "x", "goal": "g"}, 0, passed=True, verdict="done",
                 seconds=1.0, state={}, note="")
    finally:
        for name in env:
            monkeypatch.delenv(name, raising=False)
        importlib.reload(config)
    last = (tmp_path / "summary.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    return json.loads(last)


def test_a_row_reports_extraction_as_config_resolves_it(tmp_path, monkeypatch):
    """Measured 2026-09-11: every default row since extraction-by-default said
    `extraction: false` while the agent extracted three skills, because record()
    re-read the env var with a default of "off" and config.py defaults it "on".
    A row that re-derives a setting has two definitions of it, and the one that
    is wrong is the one that survives."""
    from agent import config

    row = _recorded(tmp_path, monkeypatch)
    assert row["extraction"] == config.SKILL_EXTRACTION
    assert row["extraction"] is True, "the default arm must not claim it was off"

    row = _recorded(tmp_path, monkeypatch, AGENT_SKILL_EXTRACTION="off")
    assert row["extraction"] is False


def test_the_manifest_records_which_commit_it_measured(monkeypatch):
    """real-humanize's last measurement could not be attributed to any code: the
    manifest held image, provider, model and egress, and no sha.

    Mocked, not real git: `git diff --name-only HEAD` has been measured at 7-36s
    here because eval/fixtures holds six vendored repositories. A unit suite must
    not pay that twice.
    """
    import eval_harness

    monkeypatch.setattr(eval_harness.subprocess, "run", _git_returns({
        "rev-parse": "a" * 40 + chr(10), "diff": "", "ls-files": ""}))
    code = eval_harness.code_version()

    assert code == {"commit": "a" * 40, "dirty": False, "dirty_files": []}


def test_a_modified_tree_is_reported_as_dirty(monkeypatch):
    """THE LOAD-BEARING HALF. A clean sha on a modified tree is the lie that
    invalidated the Cycles I/J comparison - both arms reported the same commit and
    the working trees differed."""
    import eval_harness

    monkeypatch.setattr(eval_harness.subprocess, "run", _git_returns({
        "rev-parse": "b" * 40, "diff": "agent/tools.py" + chr(10),
        "ls-files": "agent/new.py" + chr(10)}))
    code = eval_harness.code_version()

    assert code["dirty"] is True
    assert code["dirty_files"] == ["agent/new.py", "agent/tools.py"]


def test_a_tree_git_COULD_NOT_READ_is_neither_clean_nor_dirty(monkeypatch):
    """THREE-VALUED, and this is the defect that prompted it. The first version
    returned False when the git call failed, and `git status --porcelain` times
    out here at 30s - so every manifest claimed a clean tree. Same shape as
    AGENT_EGRESS defaulting to "restricted" where nothing ever set it."""
    import eval_harness

    monkeypatch.setattr(eval_harness.subprocess, "run", _git_returns({
        "rev-parse": "c" * 40, "diff": None, "ls-files": ""}))
    code = eval_harness.code_version()

    assert code["commit"] == "c" * 40, "the sha is still trustworthy"
    assert code["dirty"] is None, "unknown must never be recorded as clean"


def test_git_failing_does_not_take_the_run_down():
    """A measurement must not be lost because git was unavailable."""
    import subprocess

    import eval_harness

    def boom(*a, **k):
        raise OSError("no git here")

    original = subprocess.run
    subprocess.run = boom
    try:
        code = eval_harness.code_version()
    finally:
        subprocess.run = original

    assert code == {"commit": "UNKNOWN", "dirty": None, "dirty_files": []}

# ====================================== the model preflight (running != usable)


class _Done:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_a_steady_endpoint_passes_the_preflight(monkeypatch):
    import eval_harness

    monkeypatch.setattr(eval_harness.subprocess, "run",
                        lambda *a, **k: _Done(0, "probe 1: ok\nprobe 2: ok\nprobe 3: ok"))
    assert eval_harness.model_answers() is True


def test_a_flapping_endpoint_is_refused(monkeypatch):
    """THE TRAP THIS EXISTS FOR. On 2026-08-31 a single probe passed against an
    endpoint returning 503 about two times in three; the run launched on that and
    blocked all three case-runs."""
    import eval_harness

    monkeypatch.setattr(eval_harness.subprocess, "run",
                        lambda *a, **k: _Done(3, "probe 1: ok\nprobe 2: overloaded"))
    assert eval_harness.model_answers() is False


def test_a_misconfigured_provider_is_refused_too(monkeypatch):
    import eval_harness

    monkeypatch.setattr(eval_harness.subprocess, "run",
                        lambda *a, **k: _Done(4, "MISCONFIGURED: no key"))
    assert eval_harness.model_answers() is False


def test_the_probe_runs_WHERE_THE_SCORED_RUN_RUNS(monkeypatch):
    """The defect this replaced: the first version probed from the host, which has
    neither the .env file nor the proxy, and failed with `no API key` on a machine
    whose scored runs work. A preflight must perform the dependent operation, in
    the place the dependent code performs it."""
    import eval_harness

    seen = {}

    def fake(cmd, **_kwargs):
        seen["cmd"] = cmd
        return _Done(0, "probe 1: ok")

    monkeypatch.setattr(eval_harness.subprocess, "run", fake)
    monkeypatch.setattr(eval_harness, "NETWORK", eval_harness.EGRESS_NET)
    eval_harness.model_answers()

    cmd = seen["cmd"]
    assert cmd[0] == "docker", "the probe must not run in the driver's process"
    assert eval_harness.IMAGE in cmd, "the same image a case-run uses"
    assert "--network" in cmd and eval_harness.EGRESS_NET in cmd
    joined = " ".join(cmd)
    assert "HTTPS_PROXY" in joined, "through the proxy, like every scored request"


def test_the_env_file_is_passed_so_the_key_is_present(monkeypatch):
    import eval_harness

    seen = {}
    monkeypatch.setattr(eval_harness.subprocess, "run",
                        lambda cmd, **k: (seen.setdefault("cmd", cmd), _Done(0))[1])
    eval_harness.model_answers()

    if eval_harness.ENV_FILE.exists():
        assert "--env-file" in seen["cmd"]


def test_the_DEFAULT_gate_samples_MINUTES_not_seconds():
    """Lowering the default defeats the whole preflight and every other test here
    would still pass. Widened after 3 probes at 20s - a ~45 SECOND window - passed
    3/3 and the endpoint 503'd on the very next request."""
    import eval_harness

    assert eval_harness.MODEL_PROBES >= 5
    window = (eval_harness.MODEL_PROBES - 1) * eval_harness.MODEL_PROBE_GAP
    assert window >= 120, f"a {window}s window is too narrow to see flapping"


def test_the_probe_script_waits_between_probes_and_not_before_the_first():
    """Back-to-back probes sample one moment, not a state."""
    import eval_harness

    assert "if i: time.sleep" in eval_harness._PROBE_SRC


def test_the_probe_stops_at_the_first_failure():
    """It must not keep probing a dead endpoint - the point is to be cheap."""
    import eval_harness

    assert "raise SystemExit(3)" in eval_harness._PROBE_SRC


def test_no_preflight_is_an_available_escape_hatch():
    """Measuring a flapping endpoint deliberately must stay possible."""
    source = (pathlib.Path(__file__).resolve().parent.parent
              / "eval" / "harness.py").read_text(encoding="utf-8")
    assert '"--no-preflight"' in source
    assert "args.no_preflight" in source

# ===================================== aborting a run against a dead endpoint


def test_the_run_aborts_after_consecutive_blocks():
    """THE 45 MINUTES THIS EXISTS FOR. Twice on 2026-08-31 the endpoint died right
    after the preflight passed, and the driver ground through every case-run -
    three attempts and three minutes of backoff each - to produce a directory of
    blocked rows and `pass 0/0`."""
    import eval_harness

    assert eval_harness.BLOCKED_RUN_LIMIT >= 1
    source = (pathlib.Path(__file__).resolve().parent.parent
              / "eval" / "harness.py").read_text(encoding="utf-8")
    assert "consecutive_blocked >= BLOCKED_RUN_LIMIT" in source
    assert "consecutive_blocked = consecutive_blocked + 1 if code == BLOCKED else 0"         in source, "a completed case-run must RESET the counter, not accumulate"


def test_the_limit_is_small_enough_to_be_worth_having():
    """A case-run that blocked already failed BLOCKED_RETRIES+1 attempts with
    backoff between them, so the limit counts outages, not unlucky cases."""
    import eval_harness

    assert eval_harness.BLOCKED_RUN_LIMIT <= 3

def test_spawn_PINS_the_container_paths(monkeypatch, tmp_path):
    """_SET_BY_SPAWN has always claimed spawn() sets these. It did not - it only
    kept them out of the -e forwarding, while --env-file supplied them anyway.
    A .env carrying host paths (normal now: the CLI reads it) then pointed every
    scored container at a directory that does not exist inside it, and every
    case reported setup-failed.
    """
    import eval.harness as h

    captured = {}

    def fake_run(cmd, **kw):
        captured['cmd'] = cmd
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(h.subprocess, 'run', fake_run)
    monkeypatch.setattr(h, 'await_exclusive_workspace', lambda: True)
    monkeypatch.setattr(h, 'agent_home', lambda case, i: tmp_path)
    monkeypatch.setattr(h, 'NETWORK', 'bridge')
    h.spawn({'id': 'x', 'goal': 'g'}, 0, tmp_path)

    joined = ' '.join(captured['cmd'])
    assert 'AGENT_WORKSPACE=/workspace' in joined
    assert 'AGENT_HOME=/state' in joined


def test_a_hung_container_is_killed_BY_NAME_and_scored_blocked(monkeypatch,
                                                               tmp_path):
    """AGENT_MAX_SECONDS is checked BETWEEN turns, so it cannot bound a call
    that never returns: a run sat 112 minutes at 0.01% CPU and the `tools` split
    produced no rows in nine attempts.

    By NAME, because killing the client leaves the container running and the
    orphan corrupts the shared workspace mid-case - three runs were lost to
    exactly that. A watchdog that repeats it is worse than none.
    """
    import eval.harness as h

    killed = []

    def hangs(cmd, **kw):
        assert kw.get("timeout"), "spawn() waits forever without a timeout"
        raise h.subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(h.subprocess, "run", hangs)
    monkeypatch.setattr(h, "_docker", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(h, "await_exclusive_workspace", lambda: True)
    monkeypatch.setattr(h, "agent_home", lambda case, i: tmp_path)
    monkeypatch.setattr(h, "NETWORK", "bridge")

    code = h.spawn({"id": "hangs", "goal": "g"}, 0, tmp_path)

    assert code == h.BLOCKED, "a hung run must be excluded, not scored a failure"
    assert killed and killed[0][0] == "kill", f"nothing was killed: {killed}"
    assert "hangs" in killed[0][1], "killed something other than this container"


def test_the_container_is_named_so_it_can_be_killed(monkeypatch, tmp_path):
    """The watchdog above can only work if `docker run` was given --name."""
    import eval.harness as h

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(h.subprocess, "run", fake_run)
    monkeypatch.setattr(h, "await_exclusive_workspace", lambda: True)
    monkeypatch.setattr(h, "agent_home", lambda case, i: tmp_path)
    monkeypatch.setattr(h, "NETWORK", "bridge")
    h.spawn({"id": "x", "goal": "g"}, 0, tmp_path)

    assert "--name" in captured["cmd"]


# ============================================= the harness runs on the HOST

def _main_with(monkeypatch, tmp_path, contents, exported=None):
    """Run main() with a stand-in .env and report what check_provider saw."""
    import os

    env = tmp_path / ".env"
    env.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(harness, "ENV_FILE", env)
    monkeypatch.delenv("AGENT_HARNESS_PROBE", raising=False)
    if exported is not None:
        monkeypatch.setenv("AGENT_HARNESS_PROBE", exported)

    seen = {}

    def stub():
        seen["value"] = os.environ.get("AGENT_HARNESS_PROBE")
        return 0

    monkeypatch.setattr(harness, "check_provider", stub)
    monkeypatch.setattr(sys, "argv", ["harness.py", "--check-provider"])

    assert harness.main() == 0
    return seen["value"]


def test_the_harness_loads_dotenv_itself(monkeypatch, tmp_path):
    """Measured 2026-09-05: --check-provider reported "no API key" against an
    endpoint that answered. Only containers ever got .env, via --env-file, and
    the in-process probe runs on the host."""
    assert _main_with(monkeypatch, tmp_path,
                      "AGENT_HARNESS_PROBE=from-file\n") == "from-file"


def test_an_exported_variable_still_beats_the_file(monkeypatch, tmp_path):
    """How the scored container keeps /workspace: spawn() passes AGENT_WORKSPACE
    with -e, and a .env carrying host paths must not overwrite it."""
    assert _main_with(monkeypatch, tmp_path,
                      "AGENT_HARNESS_PROBE=from-file\n",
                      exported="from-shell") == "from-shell"


def test_a_missing_dotenv_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "ENV_FILE", tmp_path / "nope.env")
    monkeypatch.setattr(harness, "check_provider", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["harness.py", "--check-provider"])

    assert harness.main() == 0


# ============================================== the case file itself (Phase P)

# Every key a case may carry. A case is data the harness reads by name, so a
# field it does not know is a field that silently does nothing - which is how
# `answers` could have been written `answer` and measured nothing at all.
CASE_FIELDS = {
    "id", "split", "family", "goal", "sessions", "setup", "setups", "check",
    "egress", "budget", "max_turns", "skills_dir", "answers",
    # real-*: where the repository and its fix came from.
    "repo_url", "parent_sha", "fix_sha", "files", "suite_seconds",
    # skill-*: which skill the run was supposed to open.
    "skill_expected",
}


def _cases():
    path = pathlib.Path(__file__).resolve().parent.parent / "eval" / "tasks.jsonl"
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_every_case_field_is_one_the_harness_reads():
    for case in _cases():
        unknown = set(case) - CASE_FIELDS
        assert not unknown, f"{case['id']} carries {unknown}, which nothing reads"


def test_every_case_names_a_fixture_that_exists():
    """setup is `scripts/reset.sh <id>`, and reset.sh exits 1 on a missing
    fixture - which the harness records as setup-failed, not as a score."""
    root = pathlib.Path(__file__).resolve().parent.parent / "eval" / "fixtures"
    for case in _cases():
        for setup in case.get("setups") or [case["setup"]]:
            fixture = setup.rsplit(maxsplit=1)[-1]
            assert (root / fixture).is_dir(), f"{case['id']}: no fixture {fixture}"


def test_case_ids_are_unique():
    ids = [case["id"] for case in _cases()]
    assert len(ids) == len(set(ids))


def test_a_multi_session_case_ends_on_its_own_goal():
    """`goal` is what the row reports and `sessions[-1]` is what actually ran.
    They diverging is how a trace comes to describe a run that never happened."""
    for case in _cases():
        if case.get("sessions"):
            assert case["goal"] == case["sessions"][-1], case["id"]


def test_a_multi_session_row_reports_the_WHOLE_run_not_the_last_session(
        monkeypatch, tmp_path):
    """Measured 2026-09-11: a 4-session skill-correction run billed 205,867
    tokens and its row said 25,497 - the last session's - because inner()
    builds a fresh state per session and record() receives the final one.
    Every multi-session row in recall, skills, authoring and revision
    under-reported the same way."""
    import argparse
    import json
    import types

    import eval.harness as h
    from agent import graph, mcp, memory, skills

    case = {"id": "multi", "split": "x", "goal": "third", "setup": "true",
            "check": "true", "budget": 100_000, "max_turns": 12,
            "sessions": ["first", "second", "third"]}

    class App:
        def invoke(self, state, cfg):
            return {**state, "turns": 4, "spent_tokens": 1_000, "verdict": "done"}

    monkeypatch.setattr(h, "load_cases", lambda: [case])
    monkeypatch.setattr(h.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(h, "run_check", lambda c: (0, "", 0))
    monkeypatch.setattr(h, "restore_protected_tests", lambda c: [])
    monkeypatch.setattr(graph, "get_app", lambda: App())
    monkeypatch.setattr(memory, "activate", lambda: None)
    monkeypatch.setattr(memory, "deactivate", lambda: None)
    monkeypatch.setattr(skills, "activate", lambda: None)
    monkeypatch.setattr(skills, "deactivate", lambda: None)
    monkeypatch.setattr(mcp, "activate", lambda: [])
    monkeypatch.setattr(mcp, "shutdown", lambda: None)
    monkeypatch.delenv("AGENT_BUDGET", raising=False)
    monkeypatch.delenv("AGENT_MAX_TURNS", raising=False)

    args = argparse.Namespace(run_case="multi", run_index=0, out=str(tmp_path))
    assert h.inner(args) == h.COMPLETED

    row = json.loads((tmp_path / "summary.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["turns"] == 12, f"turns is one session's, not the run's: {row['turns']}"
    assert row["tokens"] == 3_000, f"tokens is one session's, not the run's: {row['tokens']}"


def test_scripted_answers_reach_the_ask_hook(monkeypatch, tmp_path):
    """A scored run is unattended, so ask_user returns NOBODY_THERE and the tool
    cannot be measured at all. `answers` is the stand-in person; if it stops
    being wired up, the case still passes for the wrong reason."""
    from agent import tools

    case = next(c for c in _cases() if c["id"] == "ask-environment")
    assert case.get("answers"), "ask-environment lost its scripted answer"

    trace: list = []
    scripted = list(case["answers"])

    def answer(question, choices, _left=scripted):
        reply = _left.pop(0) if _left else ""
        trace.append({"kind": "asked", "question": question[:200],
                      "choices": list(choices), "answer": reply})
        return reply

    monkeypatch.setattr(tools, "ASK", answer)
    assert tools.TOOLS["ask_user"]["fn"]("which environment?") == "gamma"
    assert trace and trace[0]["answer"] == "gamma"


# ------------------------------------------------- eval/audit.py, the evidence
#
# Built 2026-09-25. The attribution is the whole point of the module, so it is
# what these cover: a bucket that names the wrong cause is worse than no bucket,
# because it reads like evidence.


def _audit():
    """Loaded by path, like harness above: eval/ is not a package."""
    path = pathlib.Path(harness.__file__).resolve().parent / "audit.py"
    spec = importlib.util.spec_from_file_location("eval_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _failing(**over):
    row = {"id": "real-rich", "status": "ok", "pass": False, "verdict": "stuck",
           "turns": 30, "calls": [], "tampered": 0}
    row.update(over)
    return row


def test_a_run_that_ended_done_is_not_attributed_to_the_failure_counter():
    """THE BUG THIS MODULE SHIPPED WITH, caught the day it was written.
    `failures_after` is the counter's final VALUE, not a cause - 27 of 63 `real`
    failures carried it at 3 or more while ending `done`, and reading it before
    the verdict attributed every one of them to an exit they never took."""
    audit = _audit()
    row = _failing(verdict="done", failures_after=5)

    assert audit.bucket(row) == "ended `done` with the check still failing"


def test_a_stuck_run_with_three_failing_turns_is_attributed_to_them():
    audit = _audit()

    assert audit.bucket(_failing(failures_after=3)).startswith("stuck: three")


def test_a_budget_exit_is_named_not_folded_into_stuck():
    """51% of every recorded `real` failure ended `budget` or `compact`, on a cap
    removed 2026-09-23. Folding those into `stuck` would hide that the split's
    history cannot speak for the current code."""
    audit = _audit()

    assert audit.bucket(_failing(verdict="budget")) == "ended `budget`"
    assert audit.bucket(_failing(verdict="compact")) == "ended `compact`"


def test_a_run_with_no_verdict_says_so_rather_than_guessing():
    audit = _audit()

    assert audit.bucket(_failing(verdict=None)) == "no verdict recorded"


def test_tamper_does_not_compete_with_the_terminal_cause():
    """It is a wasted-turns signal, not a way for a run to end, so it is counted
    beside the buckets rather than stealing a row from one."""
    audit = _audit()
    row = _failing(verdict="done", tampered=2)

    assert audit.bucket(row) == "ended `done` with the check still failing"


def test_a_stuck_run_that_wrote_nothing_is_distinguished():
    audit = _audit()
    wrote = _failing(calls=[{"tool": "edit_file"}])

    assert "written nothing" in audit.bucket(_failing())
    assert "written nothing" not in audit.bucket(wrote)
