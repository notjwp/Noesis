"""The deterministic nodes, the three tools, and the assembled loop.

Everything here runs without an API key and without a network. The loop test drives
the real graph end to end against a stand-in model, so routing, the gate, output
trimming, the failure counter and checkpointing are all proven before a single
token is spent.

This file carries nodes, tools and the loop together rather than splitting into
more files, because the build spec's test allowlist names only three and this is
already one stated deviation.
"""
import json
import pathlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import config
from agent.graph import execute, finish, gate
from agent.provider import Reply
import subprocess
import time

from agent.tools import edit_file, read_file, run_shell, write_file


# ===================================================================== helpers

def state(**over):
    base = {
        "messages": [{"role": "user", "content": "fix it"}],
        "turns": 0, "max_turns": 12,
        "spent_tokens": 0, "budget_tokens": 200_000,
        "failures": 0, "verdict": None, "approved": [], "denied": [],
        "compact_count": 0,
    }
    base.update(over)
    return base


def call(name, cid="t1", **args):
    return {"id": cid, "name": name, "input": args}


def assistant_calls(*calls):
    return {"role": "assistant", "content": [
        {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
        for c in calls]}


def cfg(autonomous=True, trace=None):
    return {"configurable": {"autonomous": autonomous, "trace": trace}}


# ======================================================================== gate

def test_gate_approves_a_safe_call(tmp_workspace):
    s = state(messages=[assistant_calls(call("read_file", path="a.py"))])
    out = gate(s, cfg())
    assert len(out["approved"]) == 1 and out["denied"] == []


def test_gate_produces_mixed_verdicts_in_one_turn(tmp_workspace):
    """Mixed verdicts within a single turn are legal: some approved, others denied."""
    s = state(messages=[assistant_calls(
        call("read_file", cid="ok", path="a.py"),
        call("write_file", cid="bad", path="../escape.txt", content="x"),
    )])
    out = gate(s, cfg())
    assert [c["id"] for c in out["approved"]] == ["ok"]
    assert [c["id"] for c in out["denied"]] == ["bad"]
    assert "outside the workspace" in out["denied"][0]["reason"]


def test_gate_has_no_side_effects(tmp_workspace):
    """The gate re-executes from its first line on resume, so running it twice must
    change nothing on disk and produce an identical result."""
    s = state(messages=[assistant_calls(
        call("run_shell", command="rm -rf /"),
        call("read_file", cid="t2", path="a.py"),
    )])
    before = sorted(p.name for p in tmp_workspace.rglob("*"))
    first = gate(s, cfg())
    second = gate(s, cfg())
    assert first == second
    assert sorted(p.name for p in tmp_workspace.rglob("*")) == before


def test_gate_denies_destructive_when_autonomous(tmp_workspace):
    s = state(messages=[assistant_calls(call("run_shell", command="rm -rf /"))])
    assert gate(s, cfg(autonomous=True))["denied"]


# ===================================================================== execute

def test_execute_runs_an_approved_call(tmp_workspace):
    (tmp_workspace / "a.py").write_text("hello\n")
    out = execute(state(approved=[call("read_file", path="a.py")]), cfg())
    result = out["messages"][-1]["content"][0]
    assert result["is_error"] is False and "hello" in result["content"]
    assert out["turns"] == 1


def test_execute_converts_tool_exception_to_observation(tmp_workspace):
    """A tool exception must never propagate out of the node."""
    out = execute(state(approved=[call("read_file", path="nope.py")]), cfg())
    result = out["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "FileNotFoundError" in result["content"] or "No such file" in result["content"]


def test_execute_emits_all_results_in_one_user_message(tmp_workspace):
    (tmp_workspace / "a.py").write_text("x\n")
    out = execute(state(approved=[
        call("read_file", cid="t1", path="a.py"),
        call("read_file", cid="t2", path="a.py"),
    ]), cfg())
    assert len(out["messages"][-1]["content"]) == 2, "splitting results trains the model badly"


def test_denied_call_becomes_a_synthetic_error_observation(tmp_workspace):
    denied = {**call("write_file", path="../x", content="y"), "reason": "path escapes workspace"}
    out = execute(state(denied=[denied]), cfg())
    result = out["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "Denied by policy" in result["content"]
    assert out["failures"] == 1


def test_failures_counter_resets_on_a_clean_turn(tmp_workspace):
    """Correction (c): a plain int with overwrite semantics, not an accumulating list."""
    (tmp_workspace / "a.py").write_text("x\n")
    assert execute(state(failures=2, approved=[call("read_file", path="a.py")]),
                   cfg())["failures"] == 0


def test_failures_counter_increments_on_a_failed_turn(tmp_workspace):
    assert execute(state(failures=1, approved=[call("read_file", path="nope.py")]),
                   cfg())["failures"] == 2


def test_oversized_shell_output_is_spilled(tmp_workspace):
    """Shell output is ephemeral, so overflow must spill to an artifact (FR-401/402).

    This was written against read_file. read_file now sizes its own window rather
    than overflowing, and spilling IT was always redundant: the "full output"
    artifact was a copy of a file already sitting in the workspace, which the agent
    can page or grep directly. Command output has no second copy, so this is where
    the spill mechanism actually earns its place.
    """
    big = chr(10).join(f"line {i} " + "z" * 200 for i in range(config.MAX_RESULT_CHARS // 100))
    (tmp_workspace / "big.txt").write_text(big)
    out = execute(state(approved=[call("run_shell", command="cat big.txt")]), cfg())
    body = out["messages"][-1]["content"][0]["content"]
    assert len(body) < config.MAX_RESULT_CHARS + 600
    assert "[full output: " in body
    assert list(config.ARTIFACTS.glob("*.txt"))


def test_oversized_read_narrows_instead_of_spilling(tmp_workspace):
    """The counterpart: a paged read returns fewer lines, all of them contiguous."""
    big = chr(10).join(f"line {i} " + "z" * 200 for i in range(config.MAX_RESULT_CHARS // 100))
    (tmp_workspace / "big.txt").write_text(big)
    out = execute(state(approved=[call("read_file", path="big.txt", limit=5000)]), cfg())
    body = out["messages"][-1]["content"][0]["content"]
    assert len(body) < config.MAX_RESULT_CHARS + 600
    assert "elided" not in body, "a paged read must not have its middle removed"
    assert "offset=" in body, "and must say how to fetch the next page"


def test_execute_records_a_trace_entry_per_call(tmp_workspace):
    """One structured record per tool call, denials included."""
    (tmp_workspace / "a.py").write_text("x\n")
    trace = []
    execute(state(approved=[call("read_file", path="a.py")],
                  denied=[{**call("run_shell", cid="d1", command="rm -rf /"),
                           "reason": "destructive"}]),
            cfg(trace=trace))
    assert [t["verdict"] for t in trace] == ["auto", "deny"]
    for entry in trace:
        assert {"tool", "argument_hash", "verdict", "duration_ms",
                "input_bytes", "output_bytes", "spill_path"} <= set(entry)


# ====================================================================== finish

def test_finish_records_the_terminal_verdict(tmp_workspace):
    trace = []
    finish(state(verdict="done", turns=4, spent_tokens=1234), cfg(trace=trace))
    assert trace[-1] == {"kind": "terminal", "verdict": "done",
                         "turns": 4, "spent_tokens": 1234}


# ======================================================================= tools

def test_read_file_honours_offset_and_limit(tmp_workspace):
    (tmp_workspace / "a.py").write_text("\n".join(f"line{i}" for i in range(400)))
    out = read_file("a.py", offset=10, limit=150)
    assert "\tline10\n" in out and out.endswith("\tline159")
    assert "\tline160" not in out and "\tline9\n" not in out


def test_read_file_will_not_return_a_window_smaller_than_the_floor(tmp_workspace):
    """Measured on `real`, 20260911T162249Z: 136 read_file calls in 12 runs,
    74% of them new 10-to-50-line windows onto a file already read - click-1
    paid 8 calls to see 245 lines of one file. Each call re-sends the whole
    context on a provider that caches nothing. A rule beats a request: asking
    for 10 lines returns READ_MIN_LINES, and the docstring says so."""
    (tmp_workspace / "a.py").write_text("\n".join(f"line{i}" for i in range(600)))
    out = read_file("a.py", offset=380, limit=10)
    assert "line380" in out
    assert f"line{380 + config.READ_MIN_LINES - 1}" in out, "the floor was not applied"
    assert "line379" not in out, "the floor must extend forward, not move the offset"
    assert str(config.READ_MIN_LINES) in read_file.__doc__, (
        "the model must be told the floor exists, or it will keep asking for 10")


def test_the_floor_never_breaches_the_result_cap(tmp_workspace):
    """The floor is in lines; the cap is in characters. Long lines must still
    narrow to fit, floor or no floor."""
    body = [f"line{i:04d} " + "x" * 200 for i in range(400)]
    (tmp_workspace / "wide.py").write_text("\n".join(body), encoding="utf-8")
    out = read_file("wide.py", offset=0, limit=10)
    assert len(out) <= config.TOOL_CAPS["read_file"]
    assert "narrowed to fit" in out


def test_read_file_returns_a_contiguous_window_under_the_cap(tmp_workspace):
    """A paged read must return CONTIGUOUS lines, not head+tail with a hole.

    shrink() was built for UNEXPECTEDLY large output. Applied to a deliberately
    paged read it deletes the middle of the window the agent explicitly asked for.
    Measured on real-rich: rich/console.py is 101,228 chars, a read_file(limit=500)
    renders 18,920 chars, the cap is 6,000, and what arrived was 30 head + 20 tail
    of the 500 lines requested. Seeing the whole file took 54 reads, and the agent
    edited a file it had only ever seen in fragments.

    Sizing the window inside read_file keeps NFR-104 intact - the result still fits
    under the cap - while making paging actually work.
    """
    body = [f"line{i:04d} " + "x" * 60 for i in range(config.MAX_RESULT_CHARS // 30)]
    (tmp_workspace / "big.py").write_text(chr(10).join(body), encoding="utf-8")

    out = read_file("big.py", offset=0, limit=500)
    assert len(out) <= config.TOOL_CAPS["read_file"], (
        f"read_file must size its own window; got {len(out)} chars")
    assert "elided" not in out, "a paged read must not have its middle removed"

    shown = [ln for ln in out.splitlines() if "line0" in ln or "line1" in ln]
    numbers = [int(ln.split("line")[1][:4]) for ln in shown if "line" in ln]
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers))), (
        "the delivered lines must be consecutive")
    assert len(numbers) > 50, f"expected a useful window, got {len(numbers)} lines"


def test_read_file_says_where_to_continue_when_it_narrows(tmp_workspace):
    """An error the model cannot act on costs a turn; so does a silent truncation."""
    body = [f"line{i:04d} " + "y" * (config.MAX_RESULT_CHARS // 400) for i in range(600)]
    (tmp_workspace / "big.py").write_text(chr(10).join(body), encoding="utf-8")
    out = read_file("big.py", offset=0, limit=500)
    assert "offset=" in out, "the agent must be told how to fetch the next page"


def test_read_file_small_file_is_unchanged(tmp_workspace):
    """Narrowing must only apply when the window would overflow."""
    (tmp_workspace / "s.py").write_text(chr(10).join(f"l{i}" for i in range(10)),
                                        encoding="utf-8")
    out = read_file("s.py", offset=0, limit=500)
    assert "l0" in out and "l9" in out
    assert "offset=" not in out, "a complete read must not suggest a next page"


def test_write_file_creates_parent_directories(tmp_workspace):
    write_file("deep/nested/a.py", "x = 1\n")
    assert (tmp_workspace / "deep" / "nested" / "a.py").read_text() == "x = 1\n"


def test_write_file_replaces_entirely(tmp_workspace):
    write_file("a.py", "first\n")
    write_file("a.py", "second\n")
    assert (tmp_workspace / "a.py").read_text() == "second\n"


def test_edit_file_replaces_a_unique_string(tmp_workspace):
    """Targeted replacement, so a small fix costs a small number of tokens.

    Measured cause of the 0/18 real-repository baseline: `write_file` replaces a
    file entirely, so changing five lines meant emitting the whole file inside
    MAX_TOKENS (16,000, covering thinking + text + tool arguments). Real files run
    559-2,689 lines; rich/console.py needs ~25,308 tokens to rewrite, which is 158%
    of one reply - impossible, not merely expensive. Across 30 runs the agent made
    11 writes against 352 reads.
    """
    (tmp_workspace / "m.py").write_text("def f():" + chr(10) + "    return 1" + chr(10), encoding="utf-8")
    out = edit_file("m.py", "return 1", "return 2")
    assert (tmp_workspace / "m.py").read_text() == "def f():" + chr(10) + "    return 2" + chr(10)
    assert "m.py" in out


def test_edit_file_refuses_an_ambiguous_match(tmp_workspace):
    """Two matches means the agent does not know which one it is changing.

    Refusing is the whole safety property of exact matching: silently editing the
    first occurrence would corrupt a file in a way no test necessarily catches.
    """
    (tmp_workspace / "m.py").write_text("x = 1" + chr(10) + "y = 1" + chr(10), encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        edit_file("m.py", "= 1", "= 2")
    message = str(caught.value)
    assert "twice" in message or "2 times" in message
    assert "more" in message.lower(), "the error must say HOW to disambiguate"
    assert (tmp_workspace / "m.py").read_text() == "x = 1" + chr(10) + "y = 1" + chr(10), "must not edit"


def test_edit_file_says_what_to_do_when_the_string_is_absent(tmp_workspace):
    """An error the model cannot act on costs a turn every time it is retried."""
    (tmp_workspace / "m.py").write_text("x = 1" + chr(10), encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        edit_file("m.py", "nope", "yes")
    assert "read_file" in str(caught.value)


def test_run_shell_separates_streams_and_exit_code(tmp_workspace):
    out = run_shell("echo out; echo err >&2; exit 3")
    assert "exit code: 3" in out
    assert "--- stdout ---" in out and "out" in out
    assert "--- stderr ---" in out and "err" in out


def test_run_shell_timeout_kills_the_whole_process_tree(tmp_workspace):
    """A timeout must actually bound the call (FR-202).

    Written while chasing a 25-minute hang on a real repository. The hang turned
    out to be in the HARNESS - its scored check command had no timeout at all -
    and NOT in this tool, whose timeout works. The test is kept anyway: an
    unbounded run_shell would hang a live session just as badly, and nothing else
    covered it.

    The command is COMPOUND on purpose. With a single command `sh -c "python ..."`
    the shell execs into python, so killing the child kills everything; a forked
    grandchild is the harder case and the one worth guarding.
    UPDATED 2026-08-31: it used to assert TimeoutExpired PROPAGATES. That is no
    longer the contract - a timeout is a result the agent can act on, carrying
    whatever the command printed before it hung. The property this test was
    written for, that the call is bounded, is unchanged and still asserted.
    """
    started = time.monotonic()
    out = run_shell("cd / && python -c 'import time; time.sleep(60)'", timeout=2)
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"timeout did not bound the call: took {elapsed:.0f}s"
    assert "TIMED OUT" in out

    # And the forked grandchild is actually dead, which is what the compound
    # command above exists to create. Measured before the fix: two survived.
    if pathlib.Path("/proc").is_dir():
        time.sleep(0.4)
        alive = 0
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                continue
            if "time.sleep(60)" in raw.replace(bytes([0]), b" ").decode(
                    "utf-8", "replace"):
                alive += 1
        assert alive == 0, f"{alive} orphaned grandchildren survived"


def test_run_shell_runs_in_the_workspace(tmp_workspace):
    (tmp_workspace / "marker.txt").write_text("here")
    assert "marker.txt" in run_shell("ls")


def test_tools_raise_rather_than_returning_error_strings(tmp_workspace):
    with pytest.raises(FileNotFoundError):
        read_file("absent.py")


# ============================================== the assembled loop, offline

def tool_turn(name, cid="t1", **args):
    return Reply(blocks=[{"type": "tool_use", "id": cid, "name": name, "input": args}],
                 billed_tokens=150, cache_read_tokens=0, stop_reason="tool_use")


def text_turn(text):
    return Reply(blocks=[{"type": "text", "text": text}],
                 billed_tokens=150, cache_read_tokens=0, stop_reason="end_turn")


def use_fake(monkeypatch, script):
    """Replace the provider seam, not a vendor SDK.

    Patching `call_model` rather than `anthropic.Anthropic` keeps these tests true
    for whichever provider is configured - the loop's behaviour is not supposed to
    depend on who answered.
    """
    script = list(script)
    seen = []

    def fake_call_model(messages, system, tools, on_text=None):
        seen.append({"messages": list(messages), "system": system, "tools": tools})
        if not script:
            raise AssertionError("act called the model more times than the script allows")
        return script.pop(0)

    monkeypatch.setattr("agent.graph.call_model", fake_call_model)
    return seen


def run(app, thread="offline", **over):
    return app.invoke(state(**over), {"configurable": {
        "thread_id": thread, "autonomous": True, "trace": []}})


def test_full_loop_offline(fresh_app, tmp_workspace, monkeypatch):
    """read -> write -> test -> text-only -> done, with no network and no key.

    The shell turn runs PYTEST, not `echo ok`. With VERIFY_ON_STOP on since
    2026-08-31 the old script no longer finishes, and correctly so: it wrote a
    file and then ran something unrelated, which is the 47% failure mode - of 98
    runs that declared done and failed, 46 ended on an unverified change. The
    script was modelling the bug.
    """
    (tmp_workspace / "broken.py").write_text("x = \n")
    seen = use_fake(monkeypatch, [
        tool_turn("read_file", path="broken.py"),
        tool_turn("write_file", cid="t2", path="broken.py", content="x = 1\n"),
        tool_turn("run_shell", cid="t3", command="pytest -q"),
        text_turn("Fixed. Tests pass."),
    ])

    final = run(fresh_app, "loop-1")

    assert final["verdict"] == "done"
    assert final["turns"] == 3
    assert final["failures"] == 0
    assert final["spent_tokens"] == 600          # 4 turns x 150 billed
    assert (tmp_workspace / "broken.py").read_text() == "x = 1\n"

    assert len(seen) == 4, "one model call per turn"
    for req in seen:
        assert req["tools"], "tools must be sent every turn"
        assert "You are a personal agent" in req["system"], "the system prompt must be sent"
    assert len(seen[-1]["messages"]) > len(seen[0]["messages"]), "history accumulates"


def test_text_only_first_turn_does_not_finish(fresh_app, tmp_workspace, monkeypatch):
    """Correction (b) end to end: a preamble must not terminate the run.

    The marker is load bearing and was implicit before: the preamble turn is
    the CODING posture's, and `tmp_workspace` is a bare directory. Without it
    this asserts the coding rule against a workspace that is not one.
    """
    (tmp_workspace / "pyproject.toml").write_text(
        "[project]\nname='x'\n", encoding="utf-8")
    use_fake(monkeypatch, [
        text_turn("Let me look at the test file first."),
        tool_turn("run_shell", command="echo ok"),
        text_turn("Done."),
    ])
    final = run(fresh_app, "loop-2")
    assert final["verdict"] == "done"
    assert final["turns"] == 1, "the run continued past the preamble"


def test_off_a_code_workspace_a_single_reply_ends_it(fresh_app, tmp_workspace,
                                                     monkeypatch):
    """The same loop with no marker: one reply, no tool call, done. This is
    "hi whats up" end to end - it used to run until MAX_SECONDS, then cost two
    model calls, and now costs one."""
    use_fake(monkeypatch, [
        text_turn("Not much! How can I help?"),
        text_turn("Hey there - what's on your mind?"),
    ])
    final = run(fresh_app, "chat-1")
    assert final["verdict"] == "done"
    assert final["turns"] == 0


def test_denied_call_keeps_the_loop_running(fresh_app, tmp_workspace, monkeypatch):
    use_fake(monkeypatch, [
        tool_turn("write_file", path="../escape.txt", content="pwned"),
        tool_turn("run_shell", cid="t2", command="echo recovered"),
        text_turn("Recovered."),
    ])
    final = run(fresh_app, "loop-3")
    assert final["verdict"] == "done"
    assert not (tmp_workspace.parent / "escape.txt").exists(), "wrote outside the workspace"


def test_tool_exception_does_not_crash_the_run(fresh_app, tmp_workspace, monkeypatch):
    use_fake(monkeypatch, [
        tool_turn("read_file", path="does-not-exist.py"),
        text_turn("That file is missing."),
    ])
    final = run(fresh_app, "loop-4")
    assert final["verdict"] == "done"
    assert final["failures"] == 1


def test_thrashing_is_detected(fresh_app, tmp_workspace, monkeypatch):
    """The same call three times running terminates as stuck."""
    use_fake(monkeypatch, [tool_turn("run_shell", command="ls") for _ in range(4)])
    final = run(fresh_app, "loop-5")
    assert final["verdict"] == "stuck"


def test_turn_cap_terminates(fresh_app, tmp_workspace, monkeypatch):
    use_fake(monkeypatch, [
        tool_turn("run_shell", cid=f"t{i}", command=f"echo {i}") for i in range(10)
    ])
    final = run(fresh_app, "loop-6", max_turns=3)
    assert final["verdict"] == "stuck"
    # ONE over the cap, and no more. The wrap-up request buys a turn; this fake
    # ignores it and calls a tool anyway, which is the worst case and is still
    # bounded - `summarised` makes the cap unable to ask twice.
    assert final["turns"] == 4


def test_budget_exhaustion_terminates(fresh_app, tmp_workspace, monkeypatch):
    use_fake(monkeypatch, [
        tool_turn("run_shell", cid=f"t{i}", command=f"echo {i}") for i in range(10)
    ])
    final = run(fresh_app, "loop-7", budget_tokens=200)
    # Was `compact`, which terminated the run and READ as a budget stop while
    # actually being a compaction trigger at 60%. NFR-401 asks for a hard stop
    # and FR-104 names "budget exhausted" as a terminal outcome; both are real
    # now that compaction routes back to act instead of to finish.
    assert final["verdict"] == "budget"
    assert final["spent_tokens"] >= final["budget_tokens"]


def test_no_budget_means_no_budget_verdict(fresh_app, tmp_workspace, monkeypatch):
    """config.BUDGET_TOKENS is 0 since 2026-09-23: no token ceiling. A run that
    would have died on `budget` now ends on its own terms - MAX_TURNS and
    MAX_SECONDS are the remaining stops, so this is not an unbounded loop."""
    use_fake(monkeypatch, [
        tool_turn("run_shell", cid=f"t{i}", command=f"echo {i}") for i in range(4)
    ] + [text_turn("done")])

    final = run(fresh_app, "loop-8", budget_tokens=0)

    assert final["verdict"] != "budget", final["verdict"]
    assert final["spent_tokens"] > 0


def test_trace_captures_model_and_tool_activity(fresh_app, tmp_workspace, monkeypatch):
    use_fake(monkeypatch, [
        tool_turn("run_shell", command="echo ok"),
        text_turn("Done."),
    ])
    trace = []
    fresh_app.invoke(state(), {"configurable": {
        "thread_id": "loop-8", "autonomous": True, "trace": trace}})
    kinds = [t["kind"] for t in trace]
    assert kinds.count("model") == 2
    assert kinds.count("tool") == 1
    # Timing entries are excluded: `node` fires around every node and `checkpoint`
    # after each write, so the last raw entry is a stopwatch reading. What this
    # asserts is that the run ends with a terminal RECORD.
    substantive = [k for k in kinds if k not in ("node", "checkpoint")]
    assert substantive[-1] == "terminal"
    # Found by kind, not by position: `act` emits a `memory` event ahead of the
    # model one whenever anything was recalled, so trace[0] is not always the model.
    assert next(t for t in trace if t["kind"] == "model")["billed_tokens"] == 150


def test_checkpoint_persists_and_resumes(fresh_app, tmp_workspace, monkeypatch):
    """A task's identity is its thread_id; state survives the invocation."""
    use_fake(monkeypatch, [
        tool_turn("run_shell", command="echo ok"),
        text_turn("Done."),
    ])
    cfg_ = {"configurable": {"thread_id": "resume-me", "autonomous": True, "trace": []}}
    fresh_app.invoke(state(), cfg_)

    saved = fresh_app.get_state(cfg_).values
    assert saved["verdict"] == "done"
    assert saved["turns"] == 1
    assert len(saved["messages"]) > 1, "history was checkpointed, not discarded"


# ========================================== provider translation (no network)

from agent.provider import (MalformedToolCall, from_openai_message,
                            to_openai_messages, to_openai_tools)


class _FakeCall:
    def __init__(self, cid, name, arguments):
        self.id = cid
        self.function = SimpleNamespace(name=name, arguments=arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


def test_tool_schema_translation():
    out = to_openai_tools([{"name": "read_file", "description": "Read it.",
                            "input_schema": {"type": "object", "properties": {}}}])
    assert out == [{"type": "function", "function": {
        "name": "read_file", "description": "Read it.",
        "parameters": {"type": "object", "properties": {}}}}]


def test_system_prompt_becomes_the_first_message():
    out = to_openai_messages("BE GOOD", [{"role": "user", "content": "hi"}])
    assert out[0] == {"role": "system", "content": "BE GOOD"}


def test_assistant_turn_splits_into_content_and_tool_calls():
    out = to_openai_messages("s", [{"role": "assistant", "content": [
        {"type": "text", "text": "Looking."},
        {"type": "tool_use", "id": "t1", "name": "run_shell", "input": {"command": "ls"}},
    ]}])
    assistant = out[-1]
    assert assistant["content"] == "Looking."
    assert assistant["tool_calls"][0]["id"] == "t1"
    assert assistant["tool_calls"][0]["function"]["name"] == "run_shell"
    # arguments travel as a JSON STRING, not an object
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"command": "ls"}


def test_tool_results_fan_out_into_one_message_each():
    """The asymmetry that breaks a naive port: our single user message carrying N
    results must become N separate `role: tool` messages, in order."""
    out = to_openai_messages("s", [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "first"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "second"},
    ]}])
    tools = [m for m in out if m["role"] == "tool"]
    assert len(tools) == 2, "results must not be collapsed into one message"
    assert [m["tool_call_id"] for m in tools] == ["t1", "t2"]
    assert [m["content"] for m in tools] == ["first", "second"]


def test_anthropic_only_blocks_are_dropped():
    """Thinking blocks are meaningless to an OpenAI-compatible endpoint."""
    out = to_openai_messages("s", [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "abc"},
        {"type": "text", "text": "Hello."},
    ]}])
    assert out[-1]["content"] == "Hello."
    assert "thinking" not in json.dumps(out)


def test_reply_translation_back_to_blocks():
    blocks = from_openai_message(_FakeMessage(
        content="On it.",
        tool_calls=[_FakeCall("t1", "read_file", '{"path": "a.py"}')]))
    assert blocks[0] == {"type": "text", "text": "On it."}
    assert blocks[1] == {"type": "tool_use", "id": "t1", "name": "read_file",
                         "input": {"path": "a.py"}}


def test_round_trip_survives_a_full_turn():
    original = [
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}},
            {"type": "tool_use", "id": "t2", "name": "run_shell", "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "ok"},
        ]},
    ]
    out = to_openai_messages("s", original)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "tool"]


def test_malformed_tool_arguments_fail_loudly():
    """Native tool calling is a hard requirement with no fallback, so a model that
    emits unparseable arguments must be obvious, not silently coerced."""
    with pytest.raises(MalformedToolCall):
        from_openai_message(_FakeMessage(
            tool_calls=[_FakeCall("t1", "read_file", '{"path": ')]))


def test_non_object_tool_arguments_fail_loudly():
    with pytest.raises(MalformedToolCall):
        from_openai_message(_FakeMessage(
            tool_calls=[_FakeCall("t1", "read_file", '"just a string"')]))


def test_run_shell_accepts_a_string_timeout(tmp_workspace):
    """Observed live: the model sent {"timeout": "120"} on 2 of 5 calls, and
    subprocess.run rejected the string. Schema declarations are not enforcement."""
    assert "exit code: 0" in run_shell("echo hi", timeout="30")


# ==================================================== approval (human in loop)
#
# The design splits checking (gate) from doing (execute) so that a tool cannot
# fire twice when a suspended run resumes. That claim has been true on paper
# since the loop was built and had never been executed once: every run so far
# used autonomous=True, where a `confirm` verdict becomes a refusal and nothing
# ever pauses. These tests are the proof, and they run offline.

from langgraph.types import Command

from agent.tools import TOOLS


def interactive(thread, trace=None):
    """Config for a session that is allowed to pause.

    autonomous=False is the switch that makes `confirm` suspend rather than
    refuse. thread_id is required: without it the checkpointer has nowhere to
    store the paused state, so the run cannot be resumed.
    """
    return {"configurable": {"thread_id": thread, "autonomous": False,
                             "trace": trace if trace is not None else []}}


def spy_on_run_shell(monkeypatch):
    """Swap the tool function for a recorder, so 'exactly once' is measured
    rather than assumed."""
    calls = []
    monkeypatch.setitem(
        TOOLS["run_shell"], "fn",
        lambda **kw: calls.append(kw) or "exit code: 0\nstdout:\nstderr:\n")
    return calls


def two_tool_turn(*specs):
    """One assistant turn carrying several tool_use blocks."""
    return Reply(
        blocks=[{"type": "tool_use", "id": cid, "name": name, "input": args}
                for cid, name, args in specs],
        billed_tokens=150, cache_read_tokens=0, stop_reason="tool_use")


DESTRUCTIVE = {"command": "rm -rf build"}


def test_destructive_call_pauses_instead_of_running(fresh_app, tmp_workspace, monkeypatch):
    calls = spy_on_run_shell(monkeypatch)
    use_fake(monkeypatch, [tool_turn("run_shell", **DESTRUCTIVE), text_turn("Done.")])

    out = fresh_app.invoke(state(), interactive("pause-1"))

    assert "__interrupt__" in out, "a destructive command must pause"
    assert calls == [], "nothing may run before approval"
    payload = out["__interrupt__"][0].value
    # FR-306: the prompt needs the full argument set, so the gate must hand it over.
    assert payload["call"]["input"] == DESTRUCTIVE
    assert "destructive" in payload["reason"]


def test_approved_tool_runs_exactly_once(fresh_app, tmp_workspace, monkeypatch):
    """CE-07, executed at last.

    The gate re-runs from its first line on resume. Were gate and execute one
    node, the tool would fire again every time - silently, and only in the
    interactive path that no scored run exercises.
    """
    calls = spy_on_run_shell(monkeypatch)
    use_fake(monkeypatch, [tool_turn("run_shell", **DESTRUCTIVE), text_turn("Done.")])
    cfg_ = interactive("approve-once")

    fresh_app.invoke(state(), cfg_)
    out = fresh_app.invoke(Command(resume="allow"), cfg_)

    assert len(calls) == 1, f"tool fired {len(calls)} times, expected exactly 1"
    assert calls[0] == DESTRUCTIVE
    assert out["verdict"] == "done"


def test_rejected_tool_never_runs(fresh_app, tmp_workspace, monkeypatch):
    """A denial is an observation the model has to route around, not a crash."""
    calls = spy_on_run_shell(monkeypatch)
    use_fake(monkeypatch, [tool_turn("run_shell", **DESTRUCTIVE), text_turn("Understood.")])
    cfg_ = interactive("reject-1")

    fresh_app.invoke(state(), cfg_)
    out = fresh_app.invoke(Command(resume="deny"), cfg_)

    assert calls == [], "a rejected call must never execute"
    observation = out["messages"][-2]["content"][0]
    assert observation["is_error"] is True
    assert "rejected by user" in observation["content"]


def test_two_approvals_in_one_turn_are_answered_separately(
        fresh_app, tmp_workspace, monkeypatch):
    """Two destructive calls produce two pauses, resumed one at a time. Allowing
    the first and denying the second must run exactly the first."""
    calls = spy_on_run_shell(monkeypatch)
    use_fake(monkeypatch, [
        two_tool_turn(("t1", "run_shell", {"command": "rm -rf build"}),
                      ("t2", "run_shell", {"command": "rm -rf dist"})),
        text_turn("Done."),
    ])
    cfg_ = interactive("two-calls")

    first = fresh_app.invoke(state(), cfg_)
    assert first["__interrupt__"][0].value["call"]["input"]["command"] == "rm -rf build"

    second = fresh_app.invoke(Command(resume="allow"), cfg_)
    assert "__interrupt__" in second, "the second call needs its own approval"
    assert second["__interrupt__"][0].value["call"]["input"]["command"] == "rm -rf dist"

    fresh_app.invoke(Command(resume="deny"), cfg_)

    assert [c["command"] for c in calls] == ["rm -rf build"]


@pytest.mark.parametrize("answer", ["yes", "y", "", "ALLOW", "sure"])
def test_a_garbled_answer_fails_closed(fresh_app, tmp_workspace, monkeypatch, answer):
    """Only the exact string "allow" is consent. Anything else is a rejection,
    so a mangled or truncated answer can never green-light a destructive call."""
    calls = spy_on_run_shell(monkeypatch)
    use_fake(monkeypatch, [tool_turn("run_shell", **DESTRUCTIVE), text_turn("ok")])
    cfg_ = interactive(f"garbled-{answer or 'empty'}")

    fresh_app.invoke(state(), cfg_)
    fresh_app.invoke(Command(resume=answer), cfg_)

    assert calls == [], f"answer {answer!r} was treated as consent"


def test_autonomous_mode_never_pauses(fresh_app, tmp_workspace, monkeypatch):
    """The invariant every scored run depends on: unattended, a `confirm`
    verdict becomes a denial and the run never blocks waiting for a human."""
    calls = spy_on_run_shell(monkeypatch)
    use_fake(monkeypatch, [tool_turn("run_shell", **DESTRUCTIVE), text_turn("ok")])

    out = fresh_app.invoke(state(), {"configurable": {
        "thread_id": "unattended", "autonomous": True, "trace": []}})

    assert "__interrupt__" not in out
    assert calls == [], "a destructive call must be denied, not run"
    assert out["verdict"] == "done"


# ============================================== numeric arguments from a model

@pytest.mark.parametrize("kwargs,expected", [
    ({"offset": "0", "limit": "500"}, "one"),   # both as JSON strings
    ({"limit": "2"}, "one"),                    # limit alone
    ({"offset": "1", "limit": 2}, "two"),       # mixed; offset 1 skips line one
    ({"limit": None}, "one"),                   # null
    ({"limit": "lots"}, "one"),                 # nonsense - absent, not a crash
])
def test_read_file_tolerates_numeric_arguments_sent_as_strings(
        tmp_workspace, kwargs, expected):
    """A declared schema is a hint, not enforcement.

    Observed live: every read_file call in the first interactive session raised
    TypeError on string offsets, so the agent rewrote a 43-line file it had never
    read, reducing it to one line - then reported success. The tool must survive
    what models actually send.
    """
    (tmp_workspace / "a.py").write_text("one" + chr(10) + "two" + chr(10) + "three")
    assert expected in read_file("a.py", **kwargs)


def test_run_shell_tolerates_a_string_timeout(tmp_workspace):
    assert "exit code: 0" in run_shell("echo hi", timeout="30")


# ================================================ crash and resume (NFR-302)
#
# The existing checkpoint test proves state survives a CLEAN completion. This is
# the harder claim the checkpointer exists for: a process that dies mid-run must
# lose at most one node of work and must never re-execute a completed turn.
#
# A KeyboardInterrupt is used to simulate the death because it is a BaseException:
# `execute` catches Exception, so an ordinary error would be converted into an
# observation and the run would continue. This propagates straight out of invoke(),
# which is what a killed process looks like from the graph's point of view.

def test_a_crash_mid_run_never_re_executes_completed_work(
        fresh_app, tmp_workspace, monkeypatch):
    """NFR-302 / CE-07 across process death rather than an approval pause."""
    ran = []

    def counting_shell(**kw):
        ran.append(kw["command"])
        if kw["command"] == "boom":
            raise KeyboardInterrupt("simulated kill -9")
        return "exit code: 0\n--- stdout ---\n--- stderr ---\n"

    monkeypatch.setitem(TOOLS["run_shell"], "fn", counting_shell)
    use_fake(monkeypatch, [
        tool_turn("run_shell", cid="t1", command="first"),
        tool_turn("run_shell", cid="t2", command="boom"),
        text_turn("Done."),
    ])
    cfg_ = {"configurable": {"thread_id": "crashed", "autonomous": True, "trace": []}}

    with pytest.raises(KeyboardInterrupt):
        fresh_app.invoke(state(), cfg_)

    assert ran == ["first", "boom"], "the run reached the crash"
    checkpointed = fresh_app.get_state(cfg_).values
    assert checkpointed["turns"] == 1, "turn 1 was committed before the crash"

    # Let the retry succeed. The CRASHED node genuinely does re-run on resume -
    # its work never committed, so retrying it is correct and is precisely what
    # "at most one node of work lost" means. The claim under test is narrower and
    # stronger: turn 1, which DID commit, must not run a second time.
    ran_after_crash = len(ran)
    def retry_ok(**kw):
        ran.append(kw["command"])
        return "exit code: 0\n--- stdout ---\n--- stderr ---\n"

    monkeypatch.setitem(TOOLS["run_shell"], "fn", retry_ok)

    # Resume: same thread id, None payload. Re-invocation, not a restart.
    fresh_app.invoke(None, cfg_)

    assert ran[ran_after_crash:] == ["boom"], (
        f"resume re-ran {ran[ran_after_crash:]}; only the uncommitted turn may retry")

    assert ran.count("first") == 1, (
        f"completed work re-executed: {ran}. This is exactly the duplication the "
        f"gate/execute split exists to prevent.")


def test_resume_after_a_crash_preserves_history_and_continues(
        fresh_app, tmp_workspace, monkeypatch):
    """At most one node of work is lost: the crashed turn re-runs, everything
    before it is read from the checkpoint rather than recomputed."""
    def flaky_shell(**kw):
        if kw["command"] == "boom":
            raise KeyboardInterrupt("simulated kill -9")
        return "exit code: 0\n--- stdout ---\n--- stderr ---\n"

    monkeypatch.setitem(TOOLS["run_shell"], "fn", flaky_shell)
    seen = use_fake(monkeypatch, [
        tool_turn("run_shell", cid="t1", command="first"),
        tool_turn("run_shell", cid="t2", command="boom"),
        text_turn("Done."),
    ])
    cfg_ = {"configurable": {"thread_id": "crash-history", "autonomous": True, "trace": []}}

    with pytest.raises(KeyboardInterrupt):
        fresh_app.invoke(state(), cfg_)
    before = len(fresh_app.get_state(cfg_).values["messages"])
    model_calls_before = len(seen)

    monkeypatch.setitem(TOOLS["run_shell"], "fn",
                        lambda **kw: "exit code: 0\n--- stdout ---\n--- stderr ---\n")
    final = fresh_app.invoke(None, cfg_)

    assert final["messages"][0]["content"] == "fix it", "the original goal survived"
    assert len(final["messages"]) > before, "the run continued rather than restarting"
    assert final["turns"] >= 2, "the crashed turn was retried, not skipped"
    # The model is not re-asked for turns it already answered - that is what makes
    # "at most one node of work lost" true rather than "the whole run replayed".
    assert len(seen) - model_calls_before <= 1, (
        f"resume re-called the model {len(seen) - model_calls_before} times; "
        f"completed act nodes must come from the checkpoint")


def test_reading_a_directory_returns_the_listing(tmp_workspace):
    """FR-201's "list directories", answered without a second tool.

    Three versions of this, each paid for. `IsADirectoryError: [Errno 21]` with no
    guidance was the ONLY failure in the 14/15 baseline: the agent retried the
    identical path with a trailing slash, got the identical message, and burned 3
    of its 12 turns. Naming `ls` fixed that and the case passed in 11.

    It still cost a round trip - the planning traces show read_file on a
    directory, the error, then `ls -la` on the same path, two turns for one
    answer. A dedicated list tool would cost ~582 chars of schema on EVERY
    request against a 6,000 cap, to answer a question this tool is already being
    asked. So it just answers.
    """
    (tmp_workspace / "pkg").mkdir()
    (tmp_workspace / "pkg" / "core.py").write_text("x = 1", encoding="utf-8")
    (tmp_workspace / "pkg" / "sub").mkdir()

    out = read_file("pkg")

    assert "is a directory" in out
    assert "core.py" in out, "the listing is the point"
    assert "sub/" in out, "and a directory is marked as one"


def test_reading_a_missing_file_says_what_DOES_exist(tmp_workspace):
    """The most common tool failure in the whole trace archive: 82 of 112 read_file
    errors are FileNotFoundError, four times the directory case that was already
    fixed.

    A bare `[Errno 2] No such file or directory: '/workspace/stats.py'` tells the
    agent the guess was wrong and nothing about what to guess next, so it guesses
    again. Naming the siblings converts a retry loop into one read.
    """
    (tmp_workspace / "calc.py").write_text("x = 1", encoding="utf-8")
    (tmp_workspace / "helpers.py").write_text("y = 2", encoding="utf-8")
    with pytest.raises(FileNotFoundError) as caught:
        read_file("stats.py")
    message = str(caught.value)
    assert "stats.py" in message, "name what was asked for"
    assert "calc.py" in message and "helpers.py" in message, (
        "and what is actually there - that is the part it can act on")


def test_a_missing_file_in_a_subdirectory_lists_that_subdirectory(tmp_workspace):
    """Sibling listing has to follow the path the agent guessed, or a wrong guess
    deep in a tree gets the workspace root listed at it - noise, not help."""
    (tmp_workspace / "src").mkdir()
    (tmp_workspace / "src" / "parser.py").write_text("x = 1", encoding="utf-8")
    with pytest.raises(FileNotFoundError) as caught:
        read_file("src/parsr.py")
    assert "parser.py" in str(caught.value)


def test_a_missing_file_under_a_missing_directory_does_not_crash(tmp_workspace):
    """The parent may not exist either. Listing it must not raise a SECOND error
    on top of the one being reported."""
    with pytest.raises(FileNotFoundError) as caught:
        read_file("nope/also-nope/file.py")
    assert "file.py" in str(caught.value)


# ============================================================== continue_state

from agent.graph import continue_state, new_state


def test_continue_state_appends_the_message_and_keeps_the_history():
    prior = {**new_state("fix the failing tests"), "verdict": "done"}
    nxt = continue_state(prior, "now add a CSV exporter")

    assert nxt["messages"][-1] == {"role": "user",
                                   "content": "now add a CSV exporter"}
    assert nxt["messages"][:-1] == prior["messages"]


def test_continue_state_resets_the_turn_counter():
    """reflect (b) fires on turns >= max_turns. Without this reset, a second
    message into a thread that used its allowance returns `stuck` before the
    model is called even once."""
    prior = {**new_state("first"), "turns": 25, "failures": 2, "verdict": "stuck"}
    nxt = continue_state(prior, "second")

    assert nxt["turns"] == 0
    assert nxt["failures"] == 0
    # A verdict left in place routes reflect straight to finish.
    assert nxt["verdict"] is None


def test_continue_state_leaves_the_spend_alone_so_the_budget_still_binds():
    """The omission IS the safety property. Nodes overwrite per key, so every
    field left out keeps its checkpointed value - which makes token spend
    cumulative across a whole conversation rather than resetting per message."""
    prior = {**new_state("first"), "spent_tokens": 190_000, "budget_tokens": 200_000}
    nxt = continue_state(prior, "second")

    assert "spent_tokens" not in nxt
    assert "budget_tokens" not in nxt
    assert "max_turns" not in nxt


def test_continue_state_reenters_a_finished_thread(fresh_app, tmp_workspace,
                                                   monkeypatch):
    """End to end against the real graph, which is the only thing that proves
    this: a thread that already returned a verdict accepts another message and
    runs again, with the first exchange still in front of it.

    Two turns per exchange because reflect only returns `done` once a tool call
    has been made - a text-only reply on its own is `continue`, not termination.
    """
    monkeypatch.setattr(config, "PLAN_ENABLED", False)
    spy_on_run_shell(monkeypatch)
    use_fake(monkeypatch, [tool_turn("run_shell", command="pytest -q"),
                           text_turn("first answer"),
                           tool_turn("run_shell", cid="t2", command="pytest -q"),
                           text_turn("second answer")])
    cfg = {"configurable": {"thread_id": "chat1", "autonomous": True, "trace": []}}

    fresh_app.invoke(new_state("first goal"), cfg)
    prior = fresh_app.get_state(cfg).values
    assert prior["verdict"] == "done", "the first exchange should have ended"

    fresh_app.invoke(continue_state(prior, "second goal"), cfg)
    after = fresh_app.get_state(cfg).values

    assert after["messages"][0]["content"] == "first goal"
    assert any(m.get("content") == "second goal" for m in after["messages"])
    assert "second answer" in str(after["messages"][-1])
    # The turn counter restarted; the spend did not.
    assert after["spent_tokens"] > prior["spent_tokens"]


# ================================================== planning (FR-101, FR-105)
#
# Section 3 draws PLAN as a node that calls a model. It is built as a PHASE
# instead - CE-04, two nodes that never branch apart are one node - so what has
# to be proven here is that the three things which DO differ actually differ:
# the prompt act injects, what the gate refuses, and how reflect exits.

from agent.graph import _steps, act, adopt, reflect


def planning(**over):
    return state(**{"phase": "planning", "plan": [], "cursor": 0,
                    "plan_turns": 0, **over})


def working(plan, cursor=0, **over):
    return state(**{"phase": "working", "plan": plan, "cursor": cursor,
                    "plan_turns": 0, **over})


# --- parsing ---------------------------------------------------------------

def test_steps_parses_numbered_and_bulleted_lines():
    assert _steps("Here is the plan:\n"
                  "1. Read tests/test_export.py\n"
                  "2) Add a CSV writer\n"
                  "- Run pytest -q\n"
                  "* And again\n") == ["Read tests/test_export.py",
                                        "Add a CSV writer",
                                        "Run pytest -q",
                                        "And again"]


def test_steps_truncates_rather_than_refusing():
    """A planner that over-decomposes must not fail the run."""
    many = "\n".join(f"{i}. step number {i}" for i in range(1, 12))
    assert len(_steps(many)) == config.PLAN_MAX_STEPS


def test_steps_returns_nothing_from_prose():
    assert _steps("I will read the file and then fix the bug.") == []


# --- adopt -----------------------------------------------------------------

def test_adopt_falls_back_to_the_goal_when_nothing_parses():
    """A badly phrased plan must never block the run, so the goal itself becomes
    the single step."""
    s = planning(messages=[{"role": "user", "content": "fix the import"},
                           {"role": "assistant",
                            "content": [{"type": "text", "text": "I'll just fix it."}]}])
    out = adopt(s, {"configurable": {"autonomous": True}})
    assert out["plan"] == ["fix the import"]
    assert out["phase"] == "working" and out["cursor"] == 0


def test_adopt_is_silent_in_autonomous_mode():
    """No interrupt at all, or the harness could never run unattended. This is
    the 'one switch apart' the whole phase rests on.

    adopt now APPROVES rather than parses: the plan node wrote it, and the model
    call lives there so a resumed approval does not pay for it twice (CE-07).
    """
    s = planning(plan=["read it", "fix it"])
    out = adopt(s, {"configurable": {"autonomous": True}})
    assert out == {"phase": "working", "plan": ["read it", "fix it"], "cursor": 0}


# --- reflect ---------------------------------------------------------------

def test_reflect_treats_a_text_only_reply_as_the_finished_plan():
    s = planning(messages=[{"role": "user", "content": "fix it"},
                           {"role": "assistant", "content": [
                               {"type": "text", "text": "1. read\n2. fix"}]}])
    assert reflect(s)["verdict"] == "planned"


def test_reaching_the_cap_buys_one_more_turn_to_write_the_plan():
    """Measured, not anticipated. The first version exited straight to adopt at
    the cap, so the last message was a tool result, nothing parsed, and the
    fallback fired on every run - the mechanism never produced a plan at all.
    Reaching the cap now returns to act, which demands one."""
    s = planning(plan_turns=config.PLAN_MAX_TURNS,
                 messages=[{"role": "user", "content": [{"type": "tool_result",
                                                         "tool_use_id": "t1",
                                                         "content": "..."}]}])
    assert reflect(s)["verdict"] == "continue"


def test_a_planner_that_ignores_the_demand_is_still_stopped():
    """The hard stop, one turn past the cap. Reconnaissance must not consume the
    run however determined the planner is."""
    s = planning(plan_turns=config.PLAN_MAX_TURNS + 1,
                 messages=[{"role": "user", "content": [{"type": "tool_result",
                                                         "tool_use_id": "t1",
                                                         "content": "..."}]}])
    assert reflect(s)["verdict"] == "planned"


def test_act_always_offers_tools_now(tmp_workspace, monkeypatch):
    """Two cycles tried to make `act` produce the plan - first by instruction,
    then by withholding the schemas - and the model called a tool both times.
    Neither trick remains; `act` only ever does research or work, and the plan
    node writes the plan with a message list of its own."""
    for s in (planning(plan_turns=0), planning(plan_turns=config.PLAN_MAX_TURNS),
              working(["a"])):
        seen = use_fake(monkeypatch, [text_turn("x")])
        act(s, {"configurable": {}})
        assert seen[0]["tools"], "act always offers tools"
        assert "No research turns left" not in seen[0]["system"]


# --- the plan node, which is what Stage 7 actually is -----------------------

def test_the_plan_node_is_called_with_NO_tool_history(tmp_workspace, monkeypatch):
    """THE point of the stage. Nine scored runs failed because a phase inherits
    the message list, and on this provider a tool-call history keeps producing
    tool calls whether or not a tool is offered - proven with `tools` absent from
    the payload entirely. A fresh list was measured to return text."""
    from agent.graph import plan as plan_node

    seen = use_fake(monkeypatch, [text_turn("1. edit a.py\n2. run the suite")])
    noisy = planning(messages=[
        {"role": "user", "content": "fix a.py"},
        assistant_calls(call("read_file", path="a.py")),
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "x = 1"}]}])

    out = plan_node(noisy, {"configurable": {}})

    sent = seen[0]["messages"]
    assert len(sent) == 1 and sent[0]["role"] == "user", "one user message, no history"
    assert seen[0]["tools"] == [], "and no schemas either"
    assert out["plan"] == ["edit a.py", "run the suite"]


def test_the_plan_node_is_given_a_digest_rather_than_a_transcript(
        tmp_workspace, monkeypatch):
    """Facts, not a conversation. Handing it the transcript would hand it the
    tool-call history, which is the thing being avoided."""
    from agent.graph import plan as plan_node

    seen = use_fake(monkeypatch, [text_turn("1. do it\n2. check it")])
    s = planning(messages=[
        {"role": "user", "content": "fix a.py"},
        assistant_calls(call("read_file", path="tests/test_x.py")),
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "..."}]}])

    plan_node(s, {"configurable": {}})

    sent = seen[0]["messages"][0]["content"]
    assert "fix a.py" in sent, "the goal"
    assert "tests/test_x.py" in sent, "and what was read"
    assert "tool_use" not in sent, "but not the raw blocks"


def test_the_plan_node_falls_back_to_the_goal_rather_than_failing(
        tmp_workspace, monkeypatch):
    """A badly phrased reply must not block the run - the same rule adopt used to
    carry, moved to where the text is now produced."""
    from agent.graph import plan as plan_node

    use_fake(monkeypatch, [text_turn("I will just fix it.")])
    out = plan_node(planning(messages=[{"role": "user", "content": "fix the import"}]),
                    {"configurable": {}})

    assert out["plan"] == ["fix the import"]


def test_a_failed_plan_call_does_not_lose_the_run(tmp_workspace, monkeypatch):
    from agent.graph import plan as plan_node

    monkeypatch.setattr("agent.graph.call_model",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rate limited")))
    out = plan_node(planning(messages=[{"role": "user", "content": "fix it"}]),
                    {"configurable": {}})

    assert out["plan"] == ["fix it"], "falls back rather than raising"


def test_the_digest_reports_what_worked_and_what_did_not(tmp_workspace):
    from agent.graph import _digest

    messages = [
        {"role": "user", "content": "fix it"},
        assistant_calls(call("read_file", cid="t1", path="core.py"),
                        call("run_shell", cid="t2", command="ls -la")),
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]},
        assistant_calls(call("run_shell", cid="t3", command="pytest -q")),
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t3", "content": "boom",
             "is_error": True}]}]

    digest = _digest(messages)

    assert "core.py" in digest
    assert "ls -la" in digest
    assert "did not work" in digest and "pytest -q" not in digest.split("did not work")[0]


def test_reflect_keeps_researching_below_the_cap():
    s = planning(plan_turns=1,
                 messages=[{"role": "user", "content": [{"type": "tool_result",
                                                         "tool_use_id": "t1",
                                                         "content": "..."}]}])
    assert reflect(s)["verdict"] == "continue"


def test_reflect_advances_the_cursor_instead_of_finishing():
    """Section 9 step 2 (b): the cursor check is restored now that a plan
    exists. Step 1 of 3 completing is progress, not termination."""
    s = working(["a", "b", "c"], cursor=0, messages=[
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "ok"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "step done"}]}])
    out = reflect(s)
    assert out == {"verdict": "continue", "cursor": 1}


def test_reflect_says_done_on_the_last_step():
    s = working(["a", "b"], cursor=1, messages=[
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "ok"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "all done"}]}])
    assert reflect(s)["verdict"] == "done"


def test_the_made_a_call_guard_survives_with_planning_off(monkeypatch):
    """With AGENT_PLAN=off the plan is [] and the cursor check would evaluate
    1 >= 0 - the exact false `done` on a first text-only reply that section 9
    step 2 (b) was written to prevent.

    The posture is pinned because the preamble rule is the CODING posture's, and
    `is_code_workspace()` otherwise reads whatever workspace the process has -
    which made this pass or fail on test order.
    """
    monkeypatch.setattr("agent.graph.is_code_workspace", lambda: True)
    s = state(messages=[{"role": "user", "content": "fix it"},
                        {"role": "assistant", "content": [
                            {"type": "text", "text": "Let me look at the test first."}]}])
    assert reflect(s)["verdict"] == "continue"


# --- the gate, and the turn counter ----------------------------------------

def test_planning_turns_are_not_charged_against_max_turns(tmp_workspace):
    """The single most important number in this phase. If
    research shared that counter it would starve the cases planning exists to
    help."""
    s = planning(approved=[], denied=[])
    out = execute(s, {"configurable": {}})
    assert out["turns"] == 0, "a research turn must not spend a working turn"
    assert out["plan_turns"] == 1

    out = execute(working(["a"], approved=[], denied=[]), {"configurable": {}})
    assert out["turns"] == 1 and out["plan_turns"] == 0


def test_the_gate_refuses_a_write_while_planning(tmp_workspace):
    """Enforced in the gate, not asked for in the prompt: an approval shown
    after the files have changed is theatre."""
    s = planning(messages=[assistant_calls(call("write_file", path="a.py",
                                                content="x"))])
    out = gate(s, {"configurable": {"autonomous": False}})
    assert out["approved"] == []
    assert "planning" in out["denied"][0]["reason"]


def test_the_gate_allows_reading_while_planning(tmp_workspace):
    s = planning(messages=[assistant_calls(call("read_file", path="a.py"))])
    out = gate(s, {"configurable": {"autonomous": False}})
    assert len(out["approved"]) == 1 and out["denied"] == []


# --- end to end ------------------------------------------------------------

def test_a_run_plans_first_then_works(fresh_app, tmp_workspace, monkeypatch):
    """The whole phase in one pass: it reads, states a plan, adopts it without
    asking (autonomous), and only then is a write approved.

    Turned on explicitly: the default is OFF, because on the measured provider
    the plan is never written - see config.PLAN_ENABLED. The mechanism works
    when the model does reply text-only, which is what this pins.
    """
    monkeypatch.setattr(config, "PLAN_ENABLED", True)
    calls = spy_on_run_shell(monkeypatch)
    seen = use_fake(monkeypatch, [
        tool_turn("read_file", path="a.py"),              # research
        text_turn("finished looking"),                    # research ends
        text_turn("1. edit a.py\n2. run the suite"),      # the PLAN NODE's own call
        tool_turn("run_shell", cid="t2", command="pytest -q"),
        text_turn("step one done"),                       # advances the cursor
        tool_turn("run_shell", cid="t3", command="pytest -q"),
        text_turn("green"),                               # last step -> done
    ])
    cfg = {"configurable": {"thread_id": "plan-e2e", "autonomous": True, "trace": []}}
    (tmp_workspace / "a.py").write_text("x = 1", encoding="utf-8")

    out = fresh_app.invoke(new_state("fix a.py"), cfg)

    assert out["plan"] == ["edit a.py", "run the suite"]
    assert out["phase"] == "working"
    assert out["plan_turns"] == 1, "one research turn, charged to planning"
    assert out["verdict"] == "done"
    # The shell ran only AFTER the plan was adopted. During planning the same
    # command would have been refused - and a text-only reply advances the
    # cursor rather than terminating, which is section 4.1 step 8(e) exactly:
    # "done if final step, else continue + advance cursor".
    assert calls == [{"command": "pytest -q"}] * 2
    # The planning instruction reached the model on the first call and not after.
    assert "You are planning, not working" in seen[0]["system"]
    # seen[2] is the PLAN NODE: its own message list, no history, no schemas.
    assert len(seen[2]["messages"]) == 1 and seen[2]["tools"] == []
    assert "## Your plan" in seen[3]["system"], "and then work resumes with it"


def test_plan_off_restores_the_previous_agent(fresh_app, tmp_workspace, monkeypatch):
    """The kill switch, to the same standard as AGENT_MEMORY and AGENT_SKILLS."""
    monkeypatch.setattr(config, "PLAN_ENABLED", False)
    spy_on_run_shell(monkeypatch)
    seen = use_fake(monkeypatch, [tool_turn("run_shell", command="pytest -q"),
                                  text_turn("green")])
    cfg = {"configurable": {"thread_id": "plan-off", "autonomous": True, "trace": []}}

    out = fresh_app.invoke(new_state("fix it"), cfg)

    assert out["verdict"] == "done"
    assert out["plan"] == [] and out["phase"] == "working"
    assert "You are planning, not working" not in seen[0]["system"]
    assert "## Your plan" not in seen[0]["system"]


# ============================================ Stage 1: the audit closures
#
# Six requirements the audit found unmet or half-met, none of which needed any
# third-party code. Grouped here rather than scattered, because what they have in
# common is that each was reported as satisfied on the strength of code existing.

from agent import policy
from agent.context import redact, shrink
from agent.policy import classify
from agent.tools import run_python


# --- NFR-601: adding a tool touches exactly ONE file -----------------------

def test_a_tool_declared_only_in_tools_py_is_classifiable(tmp_workspace, monkeypatch):
    """The Definition-of-Done item that was false as written.

    `TOOLS` lived in tools.py and `RISK` was a literal in policy.py, so every new
    built-in touched two files. This is the property the requirement actually
    asks for: declare the tool in ONE place and the gate can already classify it.
    """
    from agent.tools import TOOLS

    monkeypatch.setitem(TOOLS, "probe_tool",
                        {"fn": lambda: "ok", "risk": "read", "schema": {}})
    verdict, reason = classify("probe_tool", {}, autonomous=True)

    assert verdict == "auto", reason
    assert policy.risk_of("probe_tool") == "read"


def test_a_tool_declaring_no_risk_is_denied(tmp_workspace, monkeypatch):
    """Fail closed, the same default register() applies to an unclassified MCP
    tool. A missing declaration must not read as `read`."""
    from agent.tools import TOOLS

    monkeypatch.setitem(TOOLS, "undeclared", {"fn": lambda: "ok", "schema": {}})
    verdict, reason = classify("undeclared", {}, autonomous=True)

    assert verdict == "deny"
    assert "unknown tool" in reason


def test_the_builtin_risks_still_match_what_they_always_were(tmp_workspace):
    """The refactor must not have quietly relabelled anything - a `write` that
    became `read` would auto-approve where it used to."""
    assert policy.risk_of("read_file") == "read"
    for name in ("write_file", "edit_file", "run_shell"):
        assert policy.risk_of(name) == "write", name


# --- FR-203: execute Python, including the final expression ----------------

def test_run_python_returns_the_value_of_the_final_expression(tmp_workspace):
    """The half of FR-203 that `run_shell(command="python -c ...")` can never
    satisfy, because -c discards the value."""
    out = run_python("rows = [1, 2, 3]\nsum(rows) * 2")
    assert "--- value ---" in out
    assert "12" in out


def test_run_python_captures_stdout(tmp_workspace):
    out = run_python("print('hello from the tool')")
    assert "hello from the tool" in out
    assert "exit code: 0" in out


def test_run_python_returns_a_traceback_rather_than_raising(tmp_workspace):
    """FR-208: the tool reports, the execute node decides. A traceback IS the
    answer to 'run this', not a failure of the tool."""
    out = run_python("raise ValueError('deliberate')")
    assert "ValueError: deliberate" in out
    assert "Traceback" in out
    assert "exit code: 1" in out


def test_run_python_assignment_alone_yields_no_value(tmp_workspace):
    """A trailing STATEMENT is not an expression. Printing `None` after every
    assignment would be noise in every result."""
    out = run_python("x = 41 + 1")
    assert "--- value ---" not in out


def test_run_python_runs_in_the_workspace(tmp_workspace):
    (tmp_workspace / "data.txt").write_text("payload", encoding="utf-8")
    out = run_python("open('data.txt').read()")
    assert "payload" in out


# --- NFR-203: secrets never enter context ----------------------------------

def test_a_secret_echoed_by_a_tool_is_redacted(tmp_workspace, monkeypatch):
    """The requirement's second half, which did not exist. `run_shell("env")`,
    a traceback carrying a credentialed URL, a config file the agent was asked
    to read - all of them arrive through shrink()."""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-01234567890abcdef")

    out = shrink("run_shell", "AUTH=nvapi-01234567890abcdef\nOK")

    assert "nvapi-01234567890abcdef" not in out
    assert "[redacted:NVIDIA_API_KEY]" in out, (
        "named, not blanked: the model should know a credential was there")


def test_redaction_covers_the_spilled_artifact_too(tmp_workspace, monkeypatch):
    """Redacting only the returned string would leave the secret on disk inside
    the workspace, one read_file away."""
    monkeypatch.setenv("SOME_TOKEN", "tok-0123456789abcdef")
    big = ("AUTH=tok-0123456789abcdef\n" + "filler line\n" * (config.MAX_RESULT_CHARS // 6))

    out = shrink("run_shell", big)
    spilled = out.split("[full output: ", 1)[1].split("]", 1)[0]

    assert "tok-0123456789abcdef" not in Path(spilled).read_text(encoding="utf-8")


def test_a_short_env_value_is_not_treated_as_a_secret(tmp_workspace, monkeypatch):
    """A two-character token would rewrite half of any English output."""
    monkeypatch.setenv("TINY_KEY", "ab")
    assert redact("a rabbit sat") == "a rabbit sat"


def test_an_ordinary_env_var_is_left_alone(tmp_workspace, monkeypatch):
    """Only secret-SHAPED names. Redacting PATH would mangle every traceback."""
    monkeypatch.setenv("PROJECT_NAME", "personal-agent")
    assert redact("built personal-agent") == "built personal-agent"


# --- NFR-304: the third cap ------------------------------------------------

def test_running_out_of_wall_clock_terminates(tmp_workspace):
    """Turns and tokens were capped and wall-clock was not, so work that spends
    neither - a shell command inside its own timeout, a provider stalling through
    its retries - had nothing to stop it."""
    s = state(spent_seconds=config.MAX_SECONDS + 1)
    assert reflect(s)["verdict"] == "stuck"


def test_the_wall_clock_cap_is_not_a_fifth_verdict(tmp_workspace):
    """FR-104 names exactly four terminal outcomes. Running out of time is a way
    of being stuck, not a new kind of ending."""
    assert reflect(state(spent_seconds=config.MAX_SECONDS + 1))["verdict"] in (
        "done", "stuck", "compact", "replan")


def test_working_seconds_accumulate_rather_than_reading_the_clock(tmp_workspace):
    """Accumulated by the nodes that spend the time, so a thread resumed a week
    later is not instantly over its cap because the calendar moved."""
    out = execute(state(spent_seconds=12.0, approved=[], denied=[]),
                  {"configurable": {}})
    assert out["spent_seconds"] >= 12.0
    assert out["spent_seconds"] < 12.0 + 5, "one empty node, not a wall clock"


def test_run_python_is_actually_exposed_to_the_model(tmp_workspace):
    """A tool the model cannot see is a function, not a capability.

    Caught by a live check rather than by the suite: the function and its tests
    existed and passed while the schema entry was missing, so the model was never
    offered it. FR-203 is satisfied when the model can CALL it.
    """
    from agent.tools import TOOLS

    assert "run_python" in TOOLS
    entry = TOOLS["run_python"]
    assert entry["risk"] == "write"
    assert entry["schema"]["input_schema"]["required"] == ["code"]


# ================================ Stage 2a: the latency NFRs (NFR-102, NFR-103)
#
# Never measured once - not failed, never attempted. Both are measurable with no
# model and no network, which is why they are tests rather than a script: with a
# stand-in model and a stubbed tool, everything left IS the framework, and a
# latency number nobody re-runs stops being true.

from eval.harness import checkpoint_ms, latency, overheads, percentile


def _cycling(monkeypatch):
    """A model that alternates one tool call, then a text reply.

    Each session is therefore act -> gate -> execute -> reflect -> act ->
    reflect -> finish: seven nodes, one tool, two model calls. Ten sessions give
    a sample large enough for a p95 to mean something.
    """
    step = {"n": 0}

    def cycling(messages, system, tools, on_text=None):
        step["n"] += 1
        return (tool_turn("run_shell", command="echo ok") if step["n"] % 2
                else text_turn("Done."))

    monkeypatch.setattr("agent.graph.call_model", cycling)


def _drive(app, monkeypatch, sessions=10):
    spy_on_run_shell(monkeypatch)
    _cycling(monkeypatch)
    trace = []
    for i in range(sessions):
        app.invoke(state(), {"configurable": {
            "thread_id": f"perf-{i}", "autonomous": True, "trace": trace}})
    return trace


# --- the helper ------------------------------------------------------------

def test_percentile_handles_the_degenerate_samples():
    """An empty sample answers rather than raising: a latency table that crashes
    on a run with no checkpoints is worse than one reporting a count of 0."""
    assert percentile([], 95) == 0.0
    assert percentile([7.0], 95) == 7.0
    assert percentile([0.0, 10.0], 50) == 5.0


def test_latency_reports_the_sample_size_beside_the_figure():
    """count is the load-bearing field. A p95 over four samples is not a p95, and
    printing one without the count invites exactly that reading."""
    stats = latency([1.0, 2.0, 3.0, 4.0])
    assert stats["count"] == 4
    assert stats["p50_ms"] == 2.5 and stats["max_ms"] == 4.0


def test_overheads_subtract_model_and_tool_time():
    """NFR-102's wording exactly: framework cost EXCLUDING model and tool time.
    A node that spent 200ms of its 210ms waiting on the model cost 10."""
    trace = [{"kind": "model", "ms": 200.0},
             {"kind": "node", "node": "act", "ms": 210.0},
             {"kind": "tool", "duration_ms": 40.0},
             {"kind": "tool", "duration_ms": 10.0},
             {"kind": "node", "node": "execute", "ms": 55.0},
             {"kind": "node", "node": "reflect", "ms": 2.0}]
    assert overheads(trace) == [10.0, 5.0, 2.0]


def test_overheads_never_go_negative():
    """Two clocks, read at different moments. A node reading marginally shorter
    than the work inside it is measurement noise, not a negative cost."""
    trace = [{"kind": "model", "ms": 100.0},
             {"kind": "node", "node": "act", "ms": 99.9}]
    assert overheads(trace) == [0.0]


# --- NFR-102 ---------------------------------------------------------------

def test_framework_overhead_per_iteration_is_within_its_cap(
        fresh_app, tmp_workspace, monkeypatch):
    """NFR-102: <= 250 ms per loop iteration, excluding model and tool time."""
    trace = _drive(fresh_app, monkeypatch)
    stats = latency(overheads(trace))

    assert stats["count"] >= 50, f"too small a sample for a p95: {stats}"
    assert stats["p95_ms"] <= 250, f"NFR-102 breached: {stats}"


def test_the_pure_python_nodes_are_effectively_free(
        fresh_app, tmp_workspace, monkeypatch):
    """gate and reflect wait on nothing - no model, no tool, no disk. If either
    is measurable in milliseconds the loop has grown something it should not
    have, and NFR-102's headroom is being spent somewhere invisible."""
    trace = _drive(fresh_app, monkeypatch)
    for node in ("gate", "reflect"):
        stats = latency([e["ms"] for e in trace
                         if e.get("kind") == "node" and e.get("node") == node])
        assert stats["count"] >= 10, node
        assert stats["p95_ms"] <= 25, f"{node} got expensive: {stats}"


# --- NFR-103 ---------------------------------------------------------------

def test_checkpoint_writes_are_within_their_cap(
        fresh_app, tmp_workspace, monkeypatch):
    """NFR-103: <= 50 ms at p95. State is written after every node transition,
    so this is paid more often than any other cost in the system."""
    trace = _drive(fresh_app, monkeypatch)
    stats = latency(checkpoint_ms(trace))

    assert stats["count"] >= 50, f"too small a sample for a p95: {stats}"
    assert stats["p95_ms"] <= 50, f"NFR-103 breached: {stats}"


def test_the_timed_saver_still_round_trips_state(fresh_app, tmp_workspace, monkeypatch):
    """The one measurement that reaches into a dependency's surface. A subclass
    that timed writes but dropped one would lose the checkpointer's whole point,
    and every latency figure would look excellent."""
    spy_on_run_shell(monkeypatch)
    use_fake(monkeypatch, [tool_turn("run_shell", command="echo ok"),
                           text_turn("Done.")])
    cfg = {"configurable": {"thread_id": "saver-1", "autonomous": True, "trace": []}}

    fresh_app.invoke(state(), cfg)
    restored = fresh_app.get_state(cfg).values

    assert restored["verdict"] == "done"
    assert restored["turns"] == 1
    assert restored["messages"][0]["content"] == "fix it"


# ================================================ FR-206: search_files
#
# "Repository inspection that returns paths and line numbers, NOT file contents."
# The last clause is the requirement, and it is why run_shell with grep does not
# satisfy it: grep returns every matching line unbounded, which is the context
# flood shrink() exists to contain.

from agent.tools import LINE_CHARS, MATCH_CAP, search_files


def _tree(root):
    (root / "pkg").mkdir()
    (root / "pkg" / "core.py").write_text(
        "import os\n\n\ndef parse_date(value):\n    return value\n", encoding="utf-8")
    (root / "pkg" / "util.py").write_text(
        "def parse_date(value):\n    pass\n", encoding="utf-8")
    (root / "notes.md").write_text("parse_date is the culprit\n", encoding="utf-8")


def test_a_match_reports_path_and_line_number(tmp_workspace):
    _tree(tmp_workspace)
    out = search_files("def parse_date")

    assert "pkg/core.py:4: def parse_date(value):" in out
    assert "pkg/util.py:1: def parse_date(value):" in out


def test_a_glob_narrows_the_search(tmp_workspace):
    _tree(tmp_workspace)
    out = search_files("parse_date", glob="**/*.md")

    assert "notes.md" in out
    assert "core.py" not in out


def test_paths_only_drops_the_lines_and_collapses_repeats(tmp_workspace):
    """FR-206's "paths, not contents" in its strictest form. A file with forty
    matches must appear once, or the mode is just a differently-shaped flood."""
    _tree(tmp_workspace)
    (tmp_workspace / "many.py").write_text("x\n" * 5 + "hit\n" * 40, encoding="utf-8")

    out = search_files("hit", paths_only=True)

    assert out.strip() == "many.py"
    assert ":" not in out, "paths_only must not carry line numbers or text"


def test_the_cap_is_enforced_AND_announced(tmp_workspace):
    """A silent truncation reads as 'that is all there is' and the agent stops
    looking. Saying how many were withheld is the difference between a bound and
    a lie."""
    (tmp_workspace / "big.py").write_text("needle\n" * 200, encoding="utf-8")

    out = search_files("needle")

    assert len([l for l in out.splitlines() if l.startswith("big.py:")]) == MATCH_CAP
    assert f"{MATCH_CAP} of 200 matches shown" in out


def test_a_long_line_is_truncated(tmp_workspace):
    """Enough to judge relevance, not enough to be a way of reading a file."""
    (tmp_workspace / "wide.py").write_text("needle " + "x" * 5000, encoding="utf-8")

    out = search_files("needle")

    assert len(out.splitlines()[0]) < LINE_CHARS + 40
    assert "x" * 200 not in out


def test_no_match_says_so_and_says_where_it_looked(tmp_workspace):
    """An empty string reads as a broken tool. Naming the count and the glob lets
    the agent tell 'wrong pattern' from 'wrong place'."""
    _tree(tmp_workspace)
    out = search_files("zzz_absent")

    assert "no match" in out
    assert "file(s)" in out


def test_a_bad_regex_explains_itself(tmp_workspace):
    """FR-208: the tool raises, execute turns it into an observation. What it must
    not do is return zero matches, which looks like a correct empty answer."""
    _tree(tmp_workspace)
    with pytest.raises(ValueError) as caught:
        search_files("[unclosed")
    assert "regular expression" in str(caught.value)


def test_noise_directories_are_skipped(tmp_workspace):
    """.git alone is thousands of files and would consume the cap before any
    source was reached."""
    (tmp_workspace / ".git").mkdir()
    (tmp_workspace / ".git" / "COMMIT_EDITMSG").write_text("needle", encoding="utf-8")
    (tmp_workspace / "__pycache__").mkdir()
    (tmp_workspace / "__pycache__" / "x.py").write_text("needle", encoding="utf-8")
    (tmp_workspace / "real.py").write_text("needle", encoding="utf-8")

    out = search_files("needle")

    assert "real.py" in out
    assert ".git" not in out and "__pycache__" not in out


def test_the_search_cannot_escape_the_workspace(tmp_workspace):
    """FR-302, and this test earned its place immediately.

    The first version of search_files claimed to be bounded "by construction"
    because the walk starts at config.WORKSPACE. It was not: `Path.glob("../*")`
    walks straight out, and this test returned a file from the parent directory.
    An escaping glob is now REFUSED rather than silently missed - "no match"
    would read as "nothing is there", which is the wrong lesson for the agent.
    """
    outside = tmp_workspace.parent / "outside_secret.py"
    outside.write_text("needle_outside", encoding="utf-8")
    try:
        assert "no match" in search_files("needle_outside")
        for escape in ("../*", "../../**/*", "/**/*", "**/../*"):
            with pytest.raises(ValueError) as caught:
                search_files("needle_outside", glob=escape)
            assert "outside the workspace" in str(caught.value), escape
    finally:
        outside.unlink()


def test_a_symlink_pointing_out_of_the_workspace_is_dropped(tmp_workspace):
    """The half a pattern check cannot catch. `**/*` is a legal glob; the file it
    reaches through a symlink is not, and only the RESOLVED path knows."""
    outside = tmp_workspace.parent / "outside_linked.py"
    outside.write_text("needle_linked", encoding="utf-8")
    try:
        (tmp_workspace / "link.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        outside.unlink()
        pytest.skip("symlinks not permitted here")
    try:
        assert "no match" in search_files("needle_linked")
    finally:
        outside.unlink()


def test_search_files_is_exposed_and_read_only(tmp_workspace):
    """A tool the model cannot see is a function, not a capability - and this one
    only reads, so it must not pause for approval."""
    from agent.tools import TOOLS

    assert "search_files" in TOOLS
    assert TOOLS["search_files"]["risk"] == "read"
    assert classify("search_files", {"pattern": "x"}, autonomous=True)[0] == "auto"


# ================================================ FR-205: git in the sandbox
#
# "Perform git status, diff, branch, add, commit, push."
#
# A TEST rather than a scored case, deliberately. A case cannot force the agent
# to reach for git - it could read the failing test and fix the code without ever
# running `git log` - and this project has already measured what happens when a
# requirement rests on the model electing to do something (Phase O: `learn`
# called 0 times in 15 sessions). What a case would prove is that the agent
# CHOOSES git; what FR-205 asks is that git WORKS. This proves the second, which
# is the part that was never checked.

def _git(workspace, command):
    """Run one git command in the workspace the way the agent would - through
    run_shell, so the test exercises the real path rather than subprocess."""
    import agent.tools as tools
    return tools.run_shell(command)


def test_git_status_diff_and_log_work_in_the_sandbox(tmp_workspace):
    (tmp_workspace / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert "exit code: 0" in _git(tmp_workspace, "git init -q")
    assert "exit code: 0" in _git(tmp_workspace, "git add -A")

    out = _git(tmp_workspace, "git commit -q -m 'first'")
    assert "exit code: 0" in out, f"commit failed - is git identity set in the image? {out}"

    (tmp_workspace / "a.py").write_text("x = 2\n", encoding="utf-8")

    assert "a.py" in _git(tmp_workspace, "git status --short")
    diff = _git(tmp_workspace, "git diff")
    assert "-x = 1" in diff and "+x = 2" in diff
    assert "first" in _git(tmp_workspace, "git log --oneline")


def test_git_branch_works(tmp_workspace):
    (tmp_workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_workspace, "git init -q && git add -A && git commit -q -m first")

    assert "exit code: 0" in _git(tmp_workspace, "git branch feature")
    assert "feature" in _git(tmp_workspace, "git branch --list")


def test_git_commit_has_an_identity(tmp_workspace):
    """The two lines the image was missing. Without them `git commit` fails with
    "Please tell me who you are", which reads as the agent doing something wrong
    rather than the environment being incomplete."""
    out = _git(tmp_workspace, "git config user.email")
    assert "exit code: 0" in out and "@" in out


# FR-205's `push` is NOT tested and cannot be, by design: a scored run has no
# route off the machine except the model allowlist (NFR-205). It is asserted,
# not demonstrated, and CONTEXT.md says so rather than implying otherwise.


# ================================================ FR-207: the @tool decorator
#
# "Register a new tool by decorating one function; derive its JSON schema
# automatically from the signature and docstring."
#
# THE EXPECTED VALUES BELOW ARE THE HAND-WRITTEN SCHEMAS, captured from TOOLS
# immediately BEFORE the conversion. That is the whole safety of this change: the
# schemas are what the model actually sees, so a decorator that quietly reworded
# one would look like a model regression and be diagnosed for days. Anything that
# changes them has to change this literal too, deliberately.

HAND_WRITTEN_SCHEMAS = {'read_file': {'name': 'read_file',
               'description': 'Read a text file from the workspace. Returns numbered '
                              'lines. Every call costs a turn, so read a whole region '
                              'at once: the window is never smaller than 100 lines, '
                              'and a small file is best read in one call with no '
                              'limit.',
               'input_schema': {'type': 'object',
                                'properties': {'path': {'type': 'string',
                                                        'description': 'Path relative '
                                                                       'to the '
                                                                       'workspace '
                                                                       'root.'},
                                               'offset': {'type': 'integer',
                                                          'description': 'First line '
                                                                         'to return, '
                                                                         '0-based. '
                                                                         'Default 0.'},
                                               'limit': {'type': 'integer',
                                                         'description': 'How many '
                                                                        'lines to '
                                                                        'return. '
                                                                        'Default '
                                                                        '500, '
                                                                        'minimum '
                                                                        '100.'}},
                                'required': ['path']}},
 'search_files': {'name': 'search_files',
                  'description': 'Find where something appears in the workspace. '
                                 'Returns path:line: matches, never whole files. **Use '
                                 'this instead of run_shell with grep** - '
                                 'it is bounded, so it cannot flood your context the '
                                 'way a raw grep across a large repository will. Use '
                                 'read_file once this has told you which file and '
                                 'which line to look at.',
                  'input_schema': {'type': 'object',
                                   'properties': {'pattern': {'type': 'string',
                                                              'description': 'Regular '
                                                                             'expression '
                                                                             'to '
                                                                             'search '
                                                                             'for.'},
                                                  'glob': {'type': 'string',
                                                           'description': 'Which files '
                                                                          'to search, '
                                                                          'e.g. '
                                                                          "'**/*.py'. "
                                                                          'Default all '
                                                                          'files.'},
                                                  'paths_only': {'type': 'boolean',
                                                                 'description': 'Return '
                                                                                'only '
                                                                                'the '
                                                                                'file '
                                                                                'paths, '
                                                                                'one '
                                                                                'per '
                                                                                'file, '
                                                                                'without '
                                                                                'the '
                                                                                'matching '
                                                                                'lines. '
                                                                                'Default '
                                                                                'false.'}},
                                   'required': ['pattern']}},
 'write_file': {'name': 'write_file',
                'description': 'Write a file in the workspace, replacing its entire '
                               'contents. Read the file first; this does not patch, it '
                               'overwrites.',
                'input_schema': {'type': 'object',
                                 'properties': {'path': {'type': 'string',
                                                         'description': 'Path relative '
                                                                        'to the '
                                                                        'workspace '
                                                                        'root.'},
                                                'content': {'type': 'string',
                                                            'description': 'The '
                                                                           'complete '
                                                                           'new '
                                                                           'contents '
                                                                           'of the '
                                                                           'file.'}},
                                 'required': ['path', 'content']}},
 'edit_file': {'name': 'edit_file',
               'description': 'Replace an exact snippet of a file with new text. '
                              'Prefer this over write_file for any change to an '
                              'existing file: it costs a few hundred characters '
                              'instead of the whole file. The snippet must appear '
                              'exactly once - include surrounding lines to make it '
                              'unique.',
               'input_schema': {'type': 'object',
                                'properties': {'path': {'type': 'string',
                                                        'description': 'Path relative '
                                                                       'to the '
                                                                       'workspace '
                                                                       'root.'},
                                               'old_string': {'type': 'string',
                                                              'description': 'The '
                                                                             'exact '
                                                                             'text to '
                                                                             'replace, '
                                                                             'copied '
                                                                             'from the '
                                                                             'file '
                                                                             'including '
                                                                             'indentation.'},
                                               'new_string': {'type': 'string',
                                                              'description': 'The text '
                                                                             'to put '
                                                                             'in its '
                                                                             'place.'}},
                                'required': ['path', 'old_string', 'new_string']}},
 'run_python': {'name': 'run_python',
                'description': 'Run Python in the workspace. Returns stdout, the '
                               'traceback if it raised, and the VALUE of the final '
                               'expression - so end with a bare expression to see what '
                               'it evaluates to, as in a REPL. Prefer this over '
                               'run_shell for anything that computes: `python -c` '
                               'throws the value away.',
                'input_schema': {'type': 'object',
                                 'properties': {'code': {'type': 'string',
                                                         'description': 'Python '
                                                                        'source. The '
                                                                        'last line may '
                                                                        'be a bare '
                                                                        'expression to '
                                                                        'return its '
                                                                        'value.'},
                                                'timeout': {'type': 'integer',
                                                            'description': 'Seconds '
                                                                           'before it '
                                                                           'is killed. '
                                                                           'Default '
                                                                           '120.'}},
                                 'required': ['code']}},
 'run_shell': {'name': 'run_shell',
               'description': 'Run any shell command in the workspace. Returns the '
                              'exit code, stdout and stderr separately. Use it to run '
                              'the tests, and to look around: `ls`, `ls -la` and '
                              '`find` are shell commands, not tools - call them '
                              'through here.',
               'input_schema': {'type': 'object',
                                'properties': {'command': {'type': 'string',
                                                           'description': "The command to run, "
                                                                          "e.g. 'pytest -q' or 'ls -la src'."},
                                               'timeout': {'type': 'integer',
                                                           'description': 'Seconds '
                                                                          'before the '
                                                                          'command is '
                                                                          'killed. '
                                                                          'Default '
                                                                          '120.'}},
                                'required': ['command']}}}


def test_the_generated_schemas_are_byte_identical_to_the_hand_written_ones():
    """The equivalence that made the conversion safe to make at all."""
    from agent.tools import TOOLS

    generated = {name: entry["schema"] for name, entry in TOOLS.items()}
    # A SUBSET, not equality, and the difference is the contract: this pins that
    # the six schemas that WERE hand-written did not drift through the conversion,
    # and that none of them vanished. A tool added later - web_search (FR-501) was
    # never hand-written - is not drift, and an equality here would turn every new
    # tool into a failure in a test about something else.
    assert set(HAND_WRITTEN_SCHEMAS) <= set(generated), "a converted tool disappeared"
    for name, expected in HAND_WRITTEN_SCHEMAS.items():
        assert generated[name] == expected, f"{name} drifted from its hand-written schema"


def test_a_description_containing_a_colon_is_not_mistaken_for_a_parameter():
    """search_files' own description says "Returns path:line: matches". A naive
    split on the first colon would eat it as a parameter line and truncate the
    text the model reads."""
    from agent.tools import TOOLS

    described = TOOLS["search_files"]["schema"]["description"]
    assert "path:line: matches" in described
    assert described.endswith("which line to look at.")


def test_required_comes_from_parameters_without_defaults():
    from agent.tools import TOOLS

    assert TOOLS["read_file"]["schema"]["input_schema"]["required"] == ["path"]
    assert TOOLS["edit_file"]["schema"]["input_schema"]["required"] == [
        "path", "old_string", "new_string"]


def test_types_come_from_the_annotations():
    from agent.tools import TOOLS

    props = TOOLS["search_files"]["schema"]["input_schema"]["properties"]
    assert props["pattern"]["type"] == "string"
    assert props["paths_only"]["type"] == "boolean"
    assert TOOLS["read_file"]["schema"]["input_schema"]["properties"]["offset"]["type"] == "integer"


def test_the_decorator_still_declares_risk_in_one_file():
    """NFR-601 must survive FR-207: risk is the one thing a signature cannot
    express, so it stays an explicit argument beside the function."""
    from agent.tools import TOOLS

    assert TOOLS["read_file"]["risk"] == "read"
    assert TOOLS["search_files"]["risk"] == "read"
    for name in ("write_file", "edit_file", "run_shell", "run_python"):
        assert TOOLS[name]["risk"] == "write", name


# ================================== compaction (FR-403, FR-404, NFR-403)

from agent.context import (HEAD_MESSAGES, SUMMARY_PREFIX, TAIL_MESSAGES,
                           boundaries, compact_messages, context_chars, pairs_ok)


def _history(turns):
    """A realistic list: goal, then alternating tool_use / tool_result."""
    out = [{"role": "user", "content": "fix it"}]
    for i in range(turns):
        out.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "read_file",
             "input": {"path": f"f{i}.py"}}]})
        out.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}",
             "content": f"contents of f{i}.py " + "x" * (config.COMPACT_AT_CHARS // 20)}]})
    return out


# --- the invariant ---------------------------------------------------------

def test_pairs_ok_accepts_a_well_formed_history():
    assert pairs_ok(_history(5))


def test_pairs_ok_catches_an_orphaned_call_and_an_orphaned_result():
    """Both shapes a provider rejects: a tool_use with no answer, and an answer
    with no call."""
    history = _history(3)
    assert not pairs_ok(history[:2])          # keeps a call, drops its result
    assert not pairs_ok(history[2:])          # keeps a result, drops its call


# --- the boundary, which is the whole point --------------------------------

def test_the_naive_boundary_would_split_a_pair_and_the_snap_prevents_it():
    """§4.3 says "the first two messages". Message 1 is a tool_use and message 2
    is its result, so the literal reading orphans the call - measured at 100% of
    466 recorded traces before this existed."""
    history = _history(8)
    assert not pairs_ok(history[:HEAD_MESSAGES]), "the literal §4.3 head is invalid"

    head_end, tail_start = boundaries(history)
    assert head_end > HEAD_MESSAGES, "the head boundary must snap forward"
    assert pairs_ok(history[:head_end])
    assert pairs_ok(history[tail_start:])


def test_compaction_over_every_recorded_trace_keeps_the_list_valid():
    """THE regression this stage exists to prevent, and the strongest offline
    evidence available: every trace this project ever recorded, compacted, and
    checked. It costs a second of CPU and it is what caught the boundary bug."""
    import json as _json

    root = Path(__file__).resolve().parent.parent / "eval" / "runs"
    checked = 0
    for f in root.rglob("*.json"):
        if "manifest" in f.name:
            continue
        try:
            messages = _json.loads(f.read_text(encoding="utf-8")).get("messages") or []
        except (OSError, ValueError):
            continue
        if len(messages) <= HEAD_MESSAGES + TAIL_MESSAGES or not pairs_ok(messages):
            continue
        assert pairs_ok(compact_messages(messages, "did things")), f.name
        checked += 1
    assert checked > 100, f"only {checked} traces available; the corpus is the point"


# --- what is retained ------------------------------------------------------

def test_the_goal_and_the_recent_turns_survive(tmp_workspace):
    """FR-404: opening messages and the most recent turns, verbatim."""
    history = _history(10)
    out = compact_messages(history, "summary text")

    assert out[0] == history[0], "the goal must survive"
    assert out[-TAIL_MESSAGES:] == history[-TAIL_MESSAGES:], "recent turns verbatim"


def test_the_summary_appears_exactly_once():
    out = compact_messages(_history(10), "summary text")
    found = [b for m in out if isinstance(m.get("content"), list)
             for b in m["content"]
             if b.get("type") == "text" and SUMMARY_PREFIX in b.get("text", "")]
    assert len(found) == 1
    assert "summary text" in found[0]["text"]


def test_a_short_history_is_returned_unchanged():
    short = _history(2)
    assert compact_messages(short, "x") == short


def test_compaction_does_not_leave_two_user_messages_adjacent():
    """The summary is appended to the trailing head message rather than inserted
    as its own. Consecutive same-role messages are a shape some providers reject,
    and the head always ends on a user tool_result once the boundary is snapped."""
    out = compact_messages(_history(10), "summary text")
    roles = [m["role"] for m in out]
    assert not any(a == b == "user" for a, b in zip(roles, roles[1:]))


# --- NFR-403 ---------------------------------------------------------------

def test_compaction_halves_the_context_when_it_fires():
    """NFR-403: at least 50% when it fires. Asserted on a history large enough to
    TRIGGER, which is the population the requirement is about - a six-message
    history has nothing to remove and shrinking it is not what was promised."""
    history = _history(40)
    assert context_chars(history) > config.COMPACT_AT_CHARS, "must be over the trigger"

    reduction = 1 - context_chars(compact_messages(history, "short summary")) \
        / context_chars(history)
    assert reduction >= 0.50, f"only {reduction:.0%} removed"


# --- the node, end to end --------------------------------------------------

def test_compaction_returns_to_act_instead_of_ending_the_run(
        fresh_app, tmp_workspace, monkeypatch):
    """The single behavioural change: `compact` used to route to finish, so the
    verdict meant "give up expensively"."""
    spy_on_run_shell(monkeypatch)
    huge = "y" * 60_000
    monkeypatch.setitem(TOOLS["run_shell"], "fn", lambda **kw: huge)

    # shrink() caps each result at MAX_RESULT_CHARS, so context grows ~6k a turn
    # and crossing 45,000 takes several. The fake never stops calling; the run
    # ends at the turn cap, which is fine - what is being proven is that
    # compaction happened along the way instead of ending it.
    step = {"n": 0}

    def cycling(messages, system, tools, on_text=None):
        step["n"] += 1
        # A DIFFERENT command each turn, or reflect's thrash detector (three
        # identical signatures in a row) ends the run at turn 3 - long before
        # context has grown enough to compact. That is the detector working.
        return tool_turn("run_shell", cid=f"t{step['n']}", command=f"cat big{step['n']}")

    monkeypatch.setattr("agent.graph.call_model", cycling)
    trace = []
    out = fresh_app.invoke(state(), {"configurable": {
        "thread_id": "compact-e2e", "autonomous": True, "trace": trace}})

    assert out["compact_count"] >= 1, "compaction never fired"
    assert out["verdict"] != "compact", "compact must no longer be terminal"
    entry = next(e for e in trace if e.get("kind") == "compact")
    assert entry["after"] < entry["before"]
    assert entry["removed_pct"] > 0


def test_a_failed_summariser_does_not_lose_the_run(
        fresh_app, tmp_workspace, monkeypatch):
    """The run is already in trouble - that is why it is compacting. Dying in the
    recovery is worse than losing the detail."""
    from agent.context import compact_messages as _cm
    from agent.graph import compact

    monkeypatch.setattr("agent.graph.call_model",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rate limited")))
    s = state(messages=_history(10))

    out = compact(s, {"configurable": {"trace": []}})

    assert out["compact_count"] == 1
    text = json.dumps(out["messages"])
    assert "unavailable" in text and "RuntimeError" in text
    assert pairs_ok(out["messages"])


def test_a_runaway_summary_is_bounded_so_compaction_still_shrinks(
        fresh_app, tmp_workspace, monkeypatch):
    """Measured 2026-09-12, real-humanize-0: the summariser returned 49,496
    chars for 24 removed messages - larger than the whole context it was
    rescuing - and the run compacted twice more against a head it never
    touches, then ended `stuck`. 6 of 87 recorded summaries were over 20,000
    chars; nothing healthy is over ~3,000. A summary bigger than what it
    replaces is not a summary, and the node must not accept one."""
    from agent.graph import compact

    monkeypatch.setattr("agent.graph.call_model",
                        lambda *a, **k: text_turn("the summary " * 5_000))   # ~60k chars
    s = state(messages=_history(40))
    before = context_chars(s["messages"])

    out = compact(s, {"configurable": {"trace": []}})

    after = context_chars(out["messages"])
    assert after < before, f"compaction GREW the context: {before:,} -> {after:,}"
    text = json.dumps(out["messages"])
    assert "truncated" in text, "a cut summary must say it was cut"
    assert pairs_ok(out["messages"])


def test_a_healthy_summary_is_not_touched(fresh_app, tmp_workspace, monkeypatch):
    """The bound must sit above every summary that ever worked - p90 of the
    recorded 87 is 3,045 chars - or it trades one defect for another."""
    from agent.graph import compact

    healthy = "Decisions made: fixed the rounding. Files touched: src/x.py. " * 40  # ~2.5k
    monkeypatch.setattr("agent.graph.call_model", lambda *a, **k: text_turn(healthy))
    out = compact(state(messages=_history(40)), {"configurable": {"trace": []}})

    text = json.dumps(out["messages"])
    assert "truncated" not in text
    assert healthy.strip()[:200] in text


def test_a_missing_prompt_file_fails_loudly(tmp_workspace, monkeypatch):
    """The bug this test exists for was live for one run: prompts/STEPS.md did
    not exist, the broad except swallowed the FileNotFoundError, and the node
    fell back to the goal on every call - looking exactly like the failure Stage
    7 was built to fix. A missing prompt is a deploy error, not a provider one.
    """
    import agent.graph as g
    from pathlib import Path

    monkeypatch.setattr(g, "STEPS", Path("/nonexistent/STEPS.md"))
    use_fake(monkeypatch, [text_turn("1. do it\n2. check it")])

    with pytest.raises(OSError):
        g.plan(planning(messages=[{"role": "user", "content": "fix it"}]),
               {"configurable": {}})



# ===================================================== FR-501: web_search
#
# "Perform web search returning ranked results with titles and URLs."
#
# Every test here runs with NO network: `ddgs` is replaced in sys.modules, which
# works because web_search imports it inside the function rather than at module
# scope. The one exception is the engine-coverage test, which asks the real
# library what it would dial and is skipped when it is absent.

import sys
import types


class _StubDDGSException(Exception):
    pass


def _install_ddgs(monkeypatch, *, rows=None, error=None, seen=None):
    """Stand in for `ddgs`, recording how it was called.

    `seen` collects the keyword arguments, which is how the proxy and the ABSENT
    backend pin are asserted - both are properties of the CALL, not of the result.
    """
    class _StubDDGS:
        def __init__(self, proxy=None, timeout=None, **kwargs):
            self.proxy, self.timeout = proxy, timeout

        def text(self, query, **kwargs):
            if seen is not None:
                seen.append({"query": query, "proxy": self.proxy,
                             "timeout": self.timeout, **kwargs})
            if error is not None:
                raise _StubDDGSException(error)
            return list(rows or [])

    module = types.ModuleType("ddgs")
    module.DDGS = _StubDDGS
    exceptions = types.ModuleType("ddgs.exceptions")
    exceptions.DDGSException = _StubDDGSException
    module.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "ddgs", module)
    monkeypatch.setitem(sys.modules, "ddgs.exceptions", exceptions)


def _rows(n=3):
    return [{"title": f"Result {i}", "href": f"https://example.com/{i}",
             "body": f"snippet {i}"} for i in range(1, n + 1)]


def test_web_search_is_exposed_and_read_only():
    """The lesson `run_python` paid for: a tool with no live registration is a
    function, not a capability. It was green with five tests and a TOOLS entry
    the model never saw."""
    from agent import registry
    from agent.policy import classify, sync
    from agent.tools import TOOLS, web_search

    assert "web_search" in TOOLS
    assert "web_search" in registry.toolset()
    assert TOOLS["web_search"]["risk"] == "read"
    sync()
    assert classify("web_search", {"query": "x"}, autonomous=True)[0] == "auto"
    assert classify("web_search", {"query": "x"}, False, planning=True)[0] == "auto"
    assert web_search.spec["schema"]["input_schema"]["required"] == ["query"]


def test_web_search_returns_ranked_titles_and_urls(monkeypatch):
    """FR-501's three nouns, checked as three nouns: RANKED, TITLES, URLS."""
    from agent.tools import web_search

    _install_ddgs(monkeypatch, rows=_rows(3))
    out = web_search("anything")

    assert out.startswith("1. Result 1")
    assert "2. Result 2" in out and "3. Result 3" in out
    for i in (1, 2, 3):
        assert f"https://example.com/{i}" in out


def test_web_search_returns_snippets_not_page_bodies(monkeypatch):
    """`fetch` exists for the body. Returning both double-charges context for the
    same information, which is the reasoning that shaped search_files too."""
    from agent.tools import SNIPPET_CHARS, web_search

    _install_ddgs(monkeypatch, rows=[
        {"title": "T", "href": "https://example.com", "body": "x" * 5_000}])
    out = web_search("anything")

    assert "x" * SNIPPET_CHARS in out
    assert "x" * (SNIPPET_CHARS + 1) not in out
    assert len(out) < SNIPPET_CHARS + 200


def test_web_search_collapses_whitespace_in_a_snippet(monkeypatch):
    """A snippet arrives with the source page's newlines in it. Left alone they
    break the one-result-per-block shape the ranking depends on."""
    from agent.tools import web_search

    _install_ddgs(monkeypatch, rows=[
        {"title": "T", "href": "https://e.com", "body": "one\n\ntwo   three"}])

    assert "one two three" in web_search("q")


def test_web_search_coerces_and_bounds_the_limit(monkeypatch):
    """A declared JSON schema is a hint, not enforcement - `"limit": "3"` is
    routinely emitted, and an uncoerced one crashed every read_file call in the
    first live session."""
    from agent.tools import RESULT_CAP, web_search

    seen = []
    _install_ddgs(monkeypatch, rows=_rows(10), seen=seen)

    web_search("q", limit="3")
    assert seen[-1]["max_results"] == 3

    web_search("q", limit="not a number")
    assert seen[-1]["max_results"] == 5           # the declared default

    web_search("q", limit=999)
    assert seen[-1]["max_results"] == RESULT_CAP

    web_search("q", limit=0)
    assert seen[-1]["max_results"] == 1


def test_web_search_passes_the_proxy_explicitly(monkeypatch):
    """The openai SDK picks up HTTPS_PROXY on its own because httpx runs
    trust_env=True. ddgs goes through primp, a Rust client that makes no such
    promise - and under a scored run the container has NO other route off the
    machine, so an ignored proxy is every search failing, not a slower one."""
    from agent.tools import web_search

    seen = []
    _install_ddgs(monkeypatch, rows=_rows(1), seen=seen)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8888")

    web_search("q")
    assert seen[-1]["proxy"] == "http://proxy:8888"

    monkeypatch.delenv("HTTPS_PROXY")
    web_search("q")
    assert seen[-1]["proxy"] is None              # no proxy set is not an error


def test_the_engine_fan_out_is_not_pinned(monkeypatch):
    """THE REGRESSION GUARD FOR THE MEASURED RATE LIMIT.

    The first version pinned backend="duckduckgo" so a case could declare one
    egress host. Measured: that endpoint returns 5 results, then blocks, and every
    retry re-arms a ~30s cooldown - so consecutive searches come back empty and
    read as "the web has no answer". With the default fan-out, six back-to-back
    searches all returned 5 results. Re-pinning it would look like a tidy-up and
    would silently restore the block.
    """
    from agent.tools import web_search

    seen = []
    _install_ddgs(monkeypatch, rows=_rows(1), seen=seen)
    web_search("q")

    assert "backend" not in seen[-1], "a pinned backend reintroduces the rate limit"


def test_web_hosts_covers_every_engine_the_library_would_dial():
    """The AGENT_EGRESS defect class, caught before it can happen again: a row
    claiming an allowlist that does not match what the code actually contacts.
    If ddgs ships a ninth text engine, this fails and the fixture cases' declared
    egress gets updated with it."""
    pytest.importorskip("ddgs")
    from urllib.parse import urlparse

    from ddgs.engines import ENGINES

    from agent.tools import WEB_HOSTS

    dialled = set()
    for engine in ENGINES["text"].values():
        host = urlparse(getattr(engine, "search_url", "")).netloc
        # wikipedia's URL is templated per language; en is the one us-en reaches.
        dialled.add(host.replace("{lang}", "en"))

    assert dialled - set(WEB_HOSTS) == set(), (
        f"ddgs dials hosts no case declares: {sorted(dialled - set(WEB_HOSTS))}")


def test_an_empty_result_is_not_a_tool_failure(monkeypatch):
    """ddgs signals "nothing matched" and "the network refused me" with the SAME
    exception type. Raising on the first would spend a `failures` count toward
    `stuck` on a query that simply matched nothing - search_files returns a
    message here rather than raising, and so does this."""
    from agent.tools import web_search

    _install_ddgs(monkeypatch, error="No results found.")
    out = web_search("zzqxwvy nonexistent")

    assert "no results" in out.lower()
    assert "zzqxwvy nonexistent" in out


def test_a_transport_failure_raises_without_asserting_a_cause(monkeypatch):
    """Tools RAISE on failure (FR-208). But the message must not name a cause it
    cannot know: from inside the container a missing allowlist entry and an engine
    refusing the connection are indistinguishable, and the first version asserted
    the allowlist - which would send the agent to fix something never broken."""
    from agent.tools import web_search

    _install_ddgs(monkeypatch, error="RequestError: connection refused")

    with pytest.raises(RuntimeError) as caught:
        web_search("q")
    assert "unreachable" in str(caught.value)
    assert "may not" in str(caught.value)


def test_an_empty_query_is_refused(monkeypatch):
    from agent.tools import web_search

    _install_ddgs(monkeypatch, rows=_rows(1))
    for empty in ("", "   ", "\n\t"):
        with pytest.raises(ValueError, match="needs a query"):
            web_search(empty)


def test_a_result_stays_well_inside_the_output_cap(monkeypatch):
    """NFR-104 caps a tool result at ~2,000 tokens. Ten results with full-length
    snippets is the worst case this tool can produce."""
    from agent.tools import RESULT_CAP, web_search

    _install_ddgs(monkeypatch, rows=[
        {"title": "T" * 90, "href": "https://example.com/" + "p" * 60,
         "body": "b" * 900} for _ in range(RESULT_CAP)])

    assert len(web_search("q", limit=RESULT_CAP)) < 4_000


def test_the_schema_budget_still_holds_with_web_search():
    from agent import config, registry

    assert registry.check_budget() <= config.MAX_SCHEMA_CHARS


# ===================================== the model's own text is capped (NFR-104)


def test_a_giant_model_reply_is_capped_before_it_is_stored(tmp_workspace, monkeypatch):
    """One 52,866-char reply became 73% of a real run's context in a single turn,
    and compaction could not clear it: the removable middle was tiny while the
    protected tail held the bulk. shrink() bounded tool results correctly all
    along - nothing bounded the model."""
    from agent import config
    from agent.graph import act

    use_fake(monkeypatch, [text_turn("x" * 60_000)])
    out = act(state(), {"configurable": {}})

    stored = out["messages"][-1]["content"][0]["text"]
    assert len(stored) < config.MAX_RESULT_CHARS + 500
    assert "x" * 100 in stored, "the head of the reply must survive"


def test_a_normal_reply_is_untouched(tmp_workspace, monkeypatch):
    """95% of replies are under ~1,028 chars. The cap must be invisible to them."""
    from agent.graph import act

    use_fake(monkeypatch, [text_turn("Fixed the import in app/main.py.")])
    out = act(state(), {"configurable": {}})

    assert out["messages"][-1]["content"][0]["text"] == "Fixed the import in app/main.py."


def test_tool_use_blocks_pass_through_unchanged(tmp_workspace, monkeypatch):
    """The cap touches text only. A tool_use block carries the arguments the gate
    classifies and execute runs - rewriting one would be a behaviour change
    wearing a truncation's clothes."""
    from agent.graph import act

    use_fake(monkeypatch, [tool_turn("run_shell", command="pytest -q")])
    out = act(state(), {"configurable": {}})

    block = out["messages"][-1]["content"][0]
    assert block["type"] == "tool_use"
    assert block["input"] == {"command": "pytest -q"}
    assert block["name"] == "run_shell"


def test_the_full_reply_is_recoverable_not_destroyed(tmp_workspace, monkeypatch):
    """shrink() spills to an artifact and names the path, so a capped reply is
    truncated in context but not lost."""
    from agent import config
    from agent.graph import act

    use_fake(monkeypatch, [text_turn("y" * 60_000)])
    out = act(state(), {"configurable": {}})

    stored = out["messages"][-1]["content"][0]["text"]
    assert ".agent/artifacts" in stored
    spilled = list(config.ARTIFACTS.glob("*.txt"))
    assert spilled, "the full text was not written anywhere"
    assert len(spilled[0].read_text(encoding="utf-8")) == 60_000


def test_capping_cannot_lose_a_plan_step(tmp_workspace, monkeypatch):
    """THE REASON THE CAP IS APPLIED AT THE STORAGE BOUNDARY AND NOWHERE ELSE.

    `plan` parses its steps from its OWN local reply.blocks, not from
    state["messages"], so a capped stored message cannot truncate a plan. If this
    ever fails, the cap has been moved into the provider or into call_model and a
    long plan will silently lose its tail.
    """
    import agent.graph as g

    use_fake(monkeypatch, [text_turn("\n".join(
        f"{i}. step number {i} " + "z" * 900 for i in range(1, 7)))])

    out = g.plan(planning(messages=[{"role": "user", "content": "fix it"}]),
                 {"configurable": {}})

    assert len(out["plan"]) == 6, f"steps were lost: {out['plan']}"
    assert "step number 6" in out["plan"][-1]


# ======================== edit_file returns proof, not a character count


def test_a_small_edit_returns_a_diff_of_what_changed(tmp_workspace):
    """A character count is not evidence the edit landed, so the agent re-reads to
    check - and re-reading is what the thrash detector then punishes."""
    from agent.tools import edit_file

    (tmp_workspace / "a.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
    out = edit_file("a.py", "y = 2", "y = 22")

    assert "-y = 2" in out and "+y = 22" in out
    assert "a.py" in out


def test_a_huge_edit_degrades_to_a_bounded_excerpt(tmp_workspace):
    """Past DIFF_LINES a diff stops being a receipt and becomes a second copy of
    the file."""
    from agent import config
    from agent.tools import DIFF_LINES, edit_file

    before = "\n".join(f"line {i}" for i in range(400))
    after = "\n".join(f"new {i}" for i in range(400))
    (tmp_workspace / "big.py").write_text(before, encoding="utf-8")

    out = edit_file("big.py", before, after)

    assert "more diff lines" in out
    assert len(out.splitlines()) < DIFF_LINES + 6
    assert len(out) < config.MAX_RESULT_CHARS, "NFR-104: a receipt must not flood context"


def test_the_line_delta_survives_alongside_the_diff(tmp_workspace):
    """The counts are what tell the agent the SCALE of what it did; the diff tells
    it the content. Both, not one."""
    from agent.tools import edit_file

    (tmp_workspace / "a.py").write_text("a\nb\n", encoding="utf-8")
    assert "+2 lines" in edit_file("a.py", "b", "b\nc\nd")


def test_an_edit_that_does_not_persist_raises(tmp_workspace, monkeypatch):
    """A write that did not land must not report success. A hard error rather
    than a silent flag, because a silent flag is how a run spends its
    remaining turns building on a file that never changed."""
    from pathlib import Path

    from agent.tools import edit_file

    (tmp_workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    # A disk that accepts the write and returns the old text - the shape of a
    # stale mount or a failed flush.
    monkeypatch.setattr(Path, "read_text",
                        lambda self, **kw: "x = 1\n")

    with pytest.raises(RuntimeError, match="did not persist"):
        edit_file("a.py", "x = 1", "x = 2")


def test_the_receipt_stays_inside_the_result_cap(tmp_workspace):
    """Worst case: every line changed, and each line is long."""
    from agent import config
    from agent.tools import edit_file

    before = "\n".join("a" * 300 for _ in range(200))
    after = "\n".join("b" * 300 for _ in range(200))
    (tmp_workspace / "wide.py").write_text(before, encoding="utf-8")

    assert len(edit_file("wide.py", before, after)) < config.MAX_RESULT_CHARS


def test_a_no_op_edit_says_so_rather_than_returning_an_empty_diff(tmp_workspace):
    from agent.tools import edit_file

    (tmp_workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert "no textual change" in edit_file("a.py", "x = 1", "x = 1")


# ================== the model is WARNED before the run is killed (Cycle 3)


def _read_twice(tmp_workspace, monkeypatch, same_result=True):
    """Two identical read_file calls; the file changes between them unless
    `same_result`, which is the case a repeat detector must not punish."""
    import agent.graph as g

    g._RESULTS.clear()
    (tmp_workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    c = call("read_file", path="a.py")
    # act appends the assistant tool_use BEFORE gate and execute run, so a real
    # message list already holds the current call. Mirror that, or the helper
    # counts nothing and the notice can never fire.
    turn = {"role": "assistant", "content": [
        {"type": "tool_use", "id": c["id"], "name": c["name"],
         "input": c["input"]}]}

    first = execute(state(messages=[turn], approved=[c]), cfg())
    if not same_result:
        (tmp_workspace / "a.py").write_text("x = 2\n", encoding="utf-8")
    second = execute(state(messages=first["messages"] + [turn], approved=[c]), cfg())
    return second["messages"][-1]["content"][0]["content"]


def test_a_repeated_call_with_the_same_result_warns(tmp_workspace, monkeypatch):
    """reflect ends the run silently at REPEAT_LIMIT. The model is never told it is
    looping, so it cannot correct - which is how a run died two turns from a fix."""
    body = _read_twice(tmp_workspace, monkeypatch, same_result=True)

    assert "same result" in body
    assert "will end the run" in body
    assert "x = 1" in body, "the warning must not replace the result"


def test_a_repeated_call_whose_RESULT_CHANGED_is_not_a_repeat(tmp_workspace, monkeypatch):
    """THE DEFECT CYCLE D SHIPPED. Hashing arguments alone cannot tell a pointless
    re-read from a legitimate one after an edit: the file changed, so the result
    changed, so it is not a repeat."""
    body = _read_twice(tmp_workspace, monkeypatch, same_result=False)

    assert "same result" not in body
    assert "x = 2" in body


def test_the_first_call_is_never_warned(tmp_workspace, monkeypatch):
    import agent.graph as g

    g._RESULTS.clear()
    (tmp_workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    out = execute(state(approved=[call("read_file", path="a.py")]), cfg())

    assert "same result" not in out["messages"][-1]["content"][0]["content"]


def test_the_warning_arrives_before_the_kill(tmp_workspace, monkeypatch):
    """WARN_AFTER must be below REPEAT_LIMIT or the notice is unreachable - the
    run would end on the turn the model was first told anything."""
    import agent.graph as g

    assert g.WARN_AFTER < min(g.REPEAT_LIMIT.values())

# ============================================== Stage 2b: streaming (NFR-101)


class _Delta:
    """One streamed chunk, shaped exactly like the SDK's."""

    def __init__(self, content=None, tool_calls=None, finish=None, usage=None):
        self.usage = usage
        if content is None and tool_calls is None and finish is None:
            self.choices = []            # the usage-only final chunk
            return
        delta = SimpleNamespace(content=content, tool_calls=tool_calls)
        self.choices = [SimpleNamespace(delta=delta, finish_reason=finish)]


def _part(index, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments))


def _usage(prompt=11, completion=7):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


def test_streamed_text_is_concatenated_in_order():
    from agent.provider import _assemble

    chunks = [_Delta(content="Hel"), _Delta(content="lo "), _Delta(content="world")]
    message, _, _, _ = _assemble(iter(chunks), None, 0.0)

    assert message.content == "Hello world"


def test_on_text_sees_each_delta_as_it_arrives():
    """The whole point of NFR-101: the caller is fed progressively, not once at
    the end."""
    from agent.provider import _assemble

    seen = []
    _assemble(iter([_Delta(content="a"), _Delta(content="b")]), seen.append, 0.0)

    assert seen == ["a", "b"]


def test_a_tool_calls_arguments_survive_being_split_mid_json():
    """Arguments arrive in fragments that are not valid JSON on their own."""
    from agent.provider import _assemble, from_openai_message

    chunks = [
        _Delta(tool_calls=[_part(0, call_id="c1", name="read_file", arguments="")]),
        _Delta(tool_calls=[_part(0, arguments='{"pa')]),
        _Delta(tool_calls=[_part(0, arguments='th": "a')]),
        _Delta(tool_calls=[_part(0, arguments='.py"}')]),
        _Delta(finish="tool_calls"),
    ]
    message, finish, _, _ = _assemble(iter(chunks), None, 0.0)
    blocks = from_openai_message(message)

    assert finish == "tool_calls"
    assert blocks == [{"type": "tool_use", "id": "c1", "name": "read_file",
                       "input": {"path": "a.py"}}]


def test_two_interleaved_tool_calls_are_grouped_by_index_not_arrival():
    """THE TRAP. A model emitting two calls interleaves their deltas. Grouping by
    arrival order splices one call's arguments into the other, and the result is
    still valid JSON often enough to look like a model error."""
    from agent.provider import _assemble, from_openai_message

    chunks = [
        _Delta(tool_calls=[_part(0, call_id="a", name="read_file", arguments='{"path"')]),
        _Delta(tool_calls=[_part(1, call_id="b", name="run_shell", arguments='{"cmd"')]),
        _Delta(tool_calls=[_part(0, arguments=': "one.py"}')]),
        _Delta(tool_calls=[_part(1, arguments=': "ls"}')]),
    ]
    blocks = from_openai_message(_assemble(iter(chunks), None, 0.0)[0])

    assert blocks == [
        {"type": "tool_use", "id": "a", "name": "read_file", "input": {"path": "one.py"}},
        {"type": "tool_use", "id": "b", "name": "run_shell", "input": {"cmd": "ls"}},
    ]


def test_id_and_name_arrive_once_and_are_not_overwritten_by_empties():
    """Later fragments carry id=None and name=None. Assigning them unconditionally
    produces a nameless tool call."""
    from agent.provider import _assemble

    chunks = [
        _Delta(tool_calls=[_part(0, call_id="keep", name="read_file", arguments="{}")]),
        _Delta(tool_calls=[_part(0, arguments="")]),
    ]
    message, _, _, _ = _assemble(iter(chunks), None, 0.0)

    assert message.tool_calls[0].id == "keep"
    assert message.tool_calls[0].function.name == "read_file"


def test_usage_on_a_final_choiceless_chunk_is_not_dropped():
    """Usage rides a last chunk carrying no choices. Skipping it leaves
    billed_tokens at 0 and silently corrupts every cost number in the project."""
    from agent.provider import _assemble

    chunks = [_Delta(content="hi"), _Delta(usage=_usage(prompt=100, completion=5))]
    _, _, usage, _ = _assemble(iter(chunks), None, 0.0)

    assert usage is not None
    assert usage.prompt_tokens + usage.completion_tokens == 105


def test_a_length_finish_reason_survives_streaming():
    """04bcde9 established that a truncated reply is not a finished one. A stream
    that loses finish_reason scores a cut-off reply as done all over again."""
    from agent.provider import _assemble

    chunks = [_Delta(content="half a sen"), _Delta(finish="length")]
    _, finish, _, _ = _assemble(iter(chunks), None, 0.0)

    assert finish == "length"


def test_streaming_and_not_streaming_produce_identical_blocks():
    """The equivalence that makes this safe to turn on: same reply, same blocks."""
    from agent.provider import _assemble, from_openai_message

    whole = SimpleNamespace(
        content="thinking",
        tool_calls=[SimpleNamespace(
            id="c1", function=SimpleNamespace(
                name="read_file", arguments='{"path": "a.py"}'))])
    chunks = [
        _Delta(content="think"), _Delta(content="ing"),
        _Delta(tool_calls=[_part(0, call_id="c1", name="read_file", arguments='{"path": ')]),
        _Delta(tool_calls=[_part(0, arguments='"a.py"}')]),
    ]
    streamed, _, _, _ = _assemble(iter(chunks), None, 0.0)

    assert from_openai_message(streamed) == from_openai_message(whole)


def test_unparseable_streamed_arguments_still_raise_MalformedToolCall():
    """Assembly must not quietly repair a broken call - that is the one thing
    from_openai_message refuses to do, and streaming must not route around it."""
    from agent.provider import MalformedToolCall, _assemble, from_openai_message

    chunks = [_Delta(tool_calls=[_part(0, call_id="c", name="read_file",
                                       arguments="{not json")])]
    message, _, _, _ = _assemble(iter(chunks), None, 0.0)

    with pytest.raises(MalformedToolCall):
        from_openai_message(message)


def test_time_to_first_token_is_recorded_so_NFR_101_has_a_number():
    """CONTEXT.md records NFR-101 as NOT MEASURABLE because one block arrived at
    the end. A first-token time is what changes that."""
    import time

    from agent.provider import _assemble

    started = time.monotonic()
    _, _, _, first = _assemble(iter([_Delta(content="x")]), None, started)

    assert first is not None and first >= 0.0


def test_a_stream_with_no_content_reports_no_first_token():
    from agent.provider import _assemble

    _, _, _, first = _assemble(iter([_Delta(finish="stop")]), None, 0.0)

    assert first is None, "None means nothing streamed, not zero seconds"


def test_streaming_is_on_by_default_and_can_be_turned_off():
    from agent import config

    assert config.STREAM is True
    assert config._env("AGENT_STREAM", "0") == "0"


def test_a_bare_APIError_is_infrastructure_not_an_agent_failure():
    """Measured: three real-humanize runs died on `APIError: Service temporarily
    overloaded` and were scored 0/3. Infrastructure failure is not a score."""
    from agent.provider import ProviderUnavailable, _reraise_classified

    class APIError(Exception):
        pass

    with pytest.raises(ProviderUnavailable):
        _reraise_classified(APIError("Service temporarily overloaded"))


def test_a_bad_request_is_still_OURS_and_still_scored():
    """The other direction. Excusing our own bug as an outage inflates the score,
    and a blocked run is retried rather than investigated."""
    from agent.provider import _reraise_classified

    class BadRequestError(Exception):
        pass

    assert _reraise_classified(BadRequestError("bad tool schema")) is None


def test_auth_failure_outranks_the_retryable_table():
    from agent.provider import ProviderMisconfigured, _reraise_classified

    class AuthenticationError(Exception):
        pass

    with pytest.raises(ProviderMisconfigured):
        _reraise_classified(AuthenticationError("no key"))

# ================================ binary and container-document guards


PNG_HEADER = bytes([0x89, 0x50, 0x4E, 0x47])
ZIP_HEADER = bytes([0x50, 0x4B, 0x03, 0x04])


def test_read_file_refuses_a_binary_file(tmp_workspace):
    """Measured before the guard: a PNG header plus 2 KB of bytes returned 2,496
    characters of mojibake into the context window."""
    (tmp_workspace / 'logo.png').write_bytes(PNG_HEADER + bytes(range(256)) * 8)

    out = read_file('logo.png')
    assert 'binary file' in out
    assert len(out) < 200, 'the guard must not return the contents'


def test_read_file_still_reads_text(tmp_workspace):
    (tmp_workspace / 'notes.txt').write_text('hello', encoding='utf-8')
    assert 'hello' in read_file('notes.txt')


def test_a_binary_extension_is_matched_case_insensitively(tmp_workspace):
    (tmp_workspace / 'LOGO.PNG').write_bytes(PNG_HEADER)
    assert 'binary file' in read_file('LOGO.PNG')


def test_an_extensionless_file_is_still_readable(tmp_workspace):
    """The guard is a string check on the extension; a file without one is text
    until proven otherwise."""
    (tmp_workspace / 'Makefile').write_text('all:' + chr(10) + chr(9) + 'echo hi',
                                            encoding='utf-8')
    assert 'echo hi' in read_file('Makefile')


def test_write_file_refuses_to_overwrite_a_container_document(tmp_workspace):
    """A .docx is a zip. Plain text written over one destroys it irrecoverably and
    the write SUCCEEDS, so the agent never learns it happened."""
    doc = tmp_workspace / 'report.docx'
    doc.write_bytes(ZIP_HEADER + b'zip-content')

    out = write_file('report.docx', 'plain text')
    assert 'refused' in out
    assert doc.read_bytes().startswith(ZIP_HEADER), 'the original must be intact'


def test_write_file_may_still_CREATE_a_docx_path(tmp_workspace):
    """Only an overwrite is dangerous. Creating a new file with that name is the
    model's business."""
    assert 'wrote' in write_file('new.docx', 'plain text')


def test_pdf_is_deliberately_not_treated_as_opaque(tmp_workspace):
    """Their comment says why: raw PDF syntax is text-authorable, so creating one
    is legitimate."""
    from agent.binary_extensions import has_opaque_document_extension

    assert not has_opaque_document_extension('paper.pdf')

# ============================== run_shell: timeouts keep output and kill children


def test_a_timeout_keeps_what_the_command_printed(tmp_workspace):
    """THE POINT. The old code let TimeoutExpired propagate and the partial output
    died with it, so a pytest run that hung after reporting 40 failures reported
    none of them - on real repositories that is the whole result."""
    out = run_shell('echo IMPORTANT-PROGRESS; sleep 30', timeout=1)

    assert 'TIMED OUT' in out
    assert 'IMPORTANT-PROGRESS' in out


def test_a_timeout_says_the_output_may_be_incomplete(tmp_workspace):
    """A partial result presented as a whole one is worse than no result."""
    out = run_shell('echo half; sleep 30', timeout=1)
    assert 'incomplete' in out


def test_a_timeout_does_not_raise(tmp_workspace):
    """It is a RESULT the agent can act on, not an exception that ends the turn."""
    assert isinstance(run_shell('sleep 30', timeout=1), str)


def test_a_normal_command_is_unchanged(tmp_workspace):
    out = run_shell('echo hi; exit 3')

    assert 'exit code: 3' in out
    assert 'hi' in out
    assert 'TIMED OUT' not in out


def test_stderr_is_still_separated(tmp_workspace):
    out = run_shell('echo to-out; echo to-err 1>&2')

    assert out.index('to-out') < out.index('--- stderr ---') < out.index('to-err')

# ===================== edit_file: lossless bytes, and the guards write_file has


LATIN1_SOURCE = bytes([0x23, 0x20, 0x63, 0x61, 0x66, 0xE9, 0x0A]) + b'x = 1' + bytes([0x0A])


def test_editing_a_non_utf8_file_does_not_corrupt_the_bytes_it_did_not_touch(
        tmp_workspace):
    """MEASURED BEFORE THE FIX: a latin-1 byte became U+FFFD on any edit. The
    write succeeded, the diff looked clean, and a byte the agent never touched was
    destroyed - on a real repository, on every edit to that file."""
    target = tmp_workspace / 'legacy.py'
    target.write_bytes(LATIN1_SOURCE)

    edit_file('legacy.py', 'x = 1', 'x = 2')

    assert target.read_bytes() == LATIN1_SOURCE.replace(b'x = 1', b'x = 2')


def test_the_diff_of_such_an_edit_is_safe_to_send_to_a_provider(tmp_workspace):
    """surrogateescape puts lone surrogates in the text, and those are invalid in
    UTF-8 and crash the SDK's encode step. shrink() is where they are removed -
    the one seam every tool result crosses.

    ENCODE, not json.dumps: json.dumps serialises a lone surrogate happily, so
    asserting on it passes with the sanitiser removed. Found by mutating it."""
    (tmp_workspace / 'legacy.py').write_bytes(LATIN1_SOURCE)
    receipt = edit_file('legacy.py', 'x = 1', 'x = 2')

    shrink('edit_file', receipt).encode('utf-8')


def test_the_write_verify_reads_the_way_it_writes(tmp_workspace):
    """It compared a surrogateescape write against an errors=replace read, so
    every non-UTF-8 file reported a failed write on a correct edit."""
    (tmp_workspace / 'legacy.py').write_bytes(LATIN1_SOURCE)

    assert 'edited' in edit_file('legacy.py', 'x = 1', 'x = 2')


def test_edit_file_refuses_a_container_document(tmp_workspace):
    """It bypassed the guard write_file has: measured, it rewrote a .docx as text
    and destroyed it."""
    doc = tmp_workspace / 'report.docx'
    doc.write_bytes(bytes([0x50, 0x4B, 0x03, 0x04]) + b'zip x = 1')

    assert 'refused' in edit_file('report.docx', 'x = 1', 'x = 2')
    assert doc.read_bytes().endswith(b'x = 1'), 'the original must be intact'


def test_edit_file_refuses_a_binary_file(tmp_workspace):
    (tmp_workspace / 'logo.png').write_bytes(bytes([0x89]) + b'PNG x = 1')
    assert 'refused' in edit_file('logo.png', 'x = 1', 'x = 2')


def test_an_ordinary_edit_is_unchanged(tmp_workspace):
    target = tmp_workspace / 'plain.py'
    target.write_text('x = 1' + chr(10), encoding='utf-8')

    assert 'edited' in edit_file('plain.py', 'x = 1', 'x = 2')
    assert target.read_text(encoding='utf-8') == 'x = 2' + chr(10)

# ========================= binaries: the two places the guard was still missing


def test_write_file_refuses_to_overwrite_any_binary_not_just_documents(
        tmp_workspace):
    """The first guard covered container documents only. Measured: write_file over
    an existing PNG replaced it with the string "oops" and reported success."""
    image = tmp_workspace / 'logo.png'
    original = bytes([0x89]) + b'PNG-real-image-data'
    image.write_bytes(original)

    assert 'refused' in write_file('logo.png', 'oops')
    assert image.read_bytes() == original


def test_write_file_may_still_create_a_new_binary_path(tmp_workspace):
    """Only an overwrite destroys something. Creating a file with that name stays
    the model's business."""
    assert 'wrote' in write_file('new.png', 'fresh')


def test_search_files_skips_binaries(tmp_workspace):
    """Measured: a .o and a .zip containing the search term each matched, emitted a
    line of mojibake, and - sorting before the real hit - pushed it down the list.
    Against MATCH_CAP they crowd out genuine matches entirely."""
    (tmp_workspace / 'real.py').write_text('def parse(x):' + chr(10), encoding='utf-8')
    (tmp_workspace / 'build.o').write_bytes(bytes(range(256)) * 4 + b'parse')
    (tmp_workspace / 'archive.zip').write_bytes(bytes([0x50, 0x4B]) + b'parse')

    out = search_files('parse')

    assert 'real.py' in out
    assert 'build.o' not in out
    assert 'archive.zip' not in out


def test_search_files_still_finds_text_matches(tmp_workspace):
    (tmp_workspace / 'a.py').write_text('needle here' + chr(10), encoding='utf-8')
    assert 'a.py' in search_files('needle')

# ================================ posture: one loop, two briefs


def test_an_empty_workspace_is_not_a_code_workspace(tmp_workspace):
    """A personal agent is not a coding agent that also remembers things."""
    from agent.graph import is_code_workspace

    assert is_code_workspace() is False


def test_prose_alone_is_not_a_code_workspace(tmp_workspace):
    from agent.graph import is_code_workspace

    (tmp_workspace / 'notes.txt').write_text('shopping list', encoding='utf-8')
    assert is_code_workspace() is False


def test_a_STRAY_SOURCE_FILE_is_not_enough(tmp_workspace):
    """The documented decision, now asserted. A .py file proves nothing - a scratch
    script, a downloaded snippet, a generated report all look like this. A project
    manifest or a test directory proves someone is MAINTAINING something.

    Found by mutation: adding `any(root.glob("*.py"))` passed every other test."""
    from agent.graph import is_code_workspace

    (tmp_workspace / 'scratch.py').write_text('print(1)', encoding='utf-8')
    (tmp_workspace / 'report.js').write_text('console.log(1)', encoding='utf-8')
    assert is_code_workspace() is False


def test_a_project_manifest_makes_it_one(tmp_workspace):
    from agent.graph import is_code_workspace

    (tmp_workspace / 'pyproject.toml').write_text('[project]', encoding='utf-8')
    assert is_code_workspace() is True


def test_a_tests_directory_makes_it_one(tmp_workspace):
    """Deliberately not a language check - a stray .py file proves nothing, a test
    directory proves someone is maintaining something."""
    from agent.graph import is_code_workspace

    (tmp_workspace / 'tests').mkdir()
    assert is_code_workspace() is True


def test_an_unreadable_workspace_stays_general(tmp_workspace, monkeypatch):
    """Fails to the general posture. Guessing "coding" would tell a personal
    request to go and run pytest."""
    from agent import config
    from agent.graph import is_code_workspace

    monkeypatch.setattr(config, 'WORKSPACE', tmp_workspace / 'gone')
    assert is_code_workspace() is False


def test_the_coding_brief_is_ABSENT_from_a_general_workspace(
        fresh_app, tmp_workspace, monkeypatch):
    """MEASURED CAUSE. SOUL.md opened with "You fix broken code" and was sent on
    EVERY task - including the recall cases where the user states a deploy key.
    Those splits score 46% and 15%; the coding splits score 74-97%."""
    seen = use_fake(monkeypatch, [
        tool_turn('run_shell', command='echo hi'),
        text_turn('Noted.')])
    run(fresh_app, 'posture-general')

    system = seen[0]['system']
    assert 'personal agent' in system
    assert 'pytest' not in system.lower(), 'a general turn must not be told to test'
    assert 'code workspace' not in system


def test_the_coding_brief_IS_added_in_a_code_workspace(
        fresh_app, tmp_workspace, monkeypatch):
    (tmp_workspace / 'pyproject.toml').write_text('[project]', encoding='utf-8')
    seen = use_fake(monkeypatch, [
        tool_turn('run_shell', command='pytest -q'),
        text_turn('Fixed.')])
    run(fresh_app, 'posture-coding')

    system = seen[0]['system']
    assert 'personal agent' in system, 'the general brief still applies'
    assert 'code workspace' in system
    assert 'green suite is the definition of done' in system


def test_the_posture_is_recorded_in_the_trace(fresh_app, tmp_workspace, monkeypatch):
    """A pass rate cannot be attributed to a posture nobody logged."""
    (tmp_workspace / 'pyproject.toml').write_text('[project]', encoding='utf-8')
    use_fake(monkeypatch, [
        tool_turn('run_shell', command='pytest -q'),
        text_turn('Fixed.')])
    trace = []
    fresh_app.invoke(state(), {'configurable': {
        'thread_id': 'posture-trace', 'autonomous': True, 'trace': trace}})

    assert any(e.get('kind') == 'posture' and e.get('name') == 'coding'
               for e in trace)

def test_the_matching_skill_BODY_reaches_the_system_prompt(
        fresh_app, tmp_workspace, monkeypatch):
    """The opening() tests call the function directly, so removing the graph
    wiring entirely left them all passing. Found by mutation."""
    from agent import config

    library = tmp_workspace / 'lib'
    (library / 'qz-release').mkdir(parents=True)
    (library / 'qz-release' / 'SKILL.md').write_text(
        '---' + chr(10) + 'name: qz-release' + chr(10)
        + 'description: Use when asked to cut, tag or prepare a release.' + chr(10)
        + '---' + chr(10) + 'Append -quartz to every version.' + chr(10),
        encoding='utf-8')
    monkeypatch.setattr(config, 'SKILLS_DIRS', (library,))

    seen = use_fake(monkeypatch, [
        tool_turn('run_shell', command='echo hi'), text_turn('Done.')])
    goal = [{'role': 'user', 'content': 'Cut release 4.14.0'}]
    fresh_app.invoke(state(messages=goal), {'configurable': {
        'thread_id': 'skill-open', 'autonomous': True, 'trace': []}})

    assert 'Append -quartz to every version' in seen[0]['system'], (
        'the matching skill body must be in the prompt, not merely offered')

# ============ RETRYABLE was a taxonomy with no retry behind it (measured 15/20)


def _flaky(monkeypatch, outcomes):
    """Drive call_model with a scripted sequence of failures then a Reply."""
    from agent import provider

    seen = {'calls': 0, 'sinks': []}

    def fake(messages, system, tools, on_text):
        seen['calls'] += 1
        step = outcomes[seen['calls'] - 1]
        if isinstance(step, str):          # text the stream emitted before dying
            if on_text:
                on_text(step)
            raise provider.ProviderUnavailable('APIError: overloaded')
        if isinstance(step, Exception):
            raise step
        return step

    monkeypatch.setattr(provider, '_call_openai_compatible', fake)
    monkeypatch.setattr(provider.settings, 'PROVIDER', 'nvidia')
    monkeypatch.setattr(provider.settings, 'RETRY_BASE', 0.0)
    monkeypatch.setattr(provider.settings, 'RETRY_CAP', 0.0)
    return seen


def _reply():
    from agent import provider
    return provider.Reply(blocks=[{'type': 'text', 'text': 'ok'}],
                          billed_tokens=1, cache_read_tokens=0, stop_reason='stop')


def test_a_transient_provider_failure_is_RETRIED(monkeypatch):
    """The measured defect: 5 of 20 calls raised APIError in 0.2-0.3s - too fast
    for the SDK to have retried - and call_model gave up on the first one."""
    from agent import provider

    unavailable = provider.ProviderUnavailable('APIError: overloaded')
    seen = _flaky(monkeypatch, [unavailable, unavailable, _reply()])
    monkeypatch.setattr(provider.settings, 'CALL_ATTEMPTS', 6)

    assert provider.call_model([], 'sys', []).blocks[0]['text'] == 'ok'
    assert seen['calls'] == 3, 'it must keep trying, not give up on the first'


def test_the_attempt_budget_is_FINITE(monkeypatch):
    from agent import provider

    unavailable = provider.ProviderUnavailable('APIError: overloaded')
    seen = _flaky(monkeypatch, [unavailable] * 20)
    monkeypatch.setattr(provider.settings, 'CALL_ATTEMPTS', 4)

    with pytest.raises(provider.ProviderUnavailable):
        provider.call_model([], 'sys', [])
    assert seen['calls'] == 4


def test_a_stream_that_ALREADY_SPOKE_is_not_retried(monkeypatch):
    """Retrying after tokens reached the caller would repeat them into the
    transcript. A partial answer is worse than an excluded run."""
    from agent import provider

    seen = _flaky(monkeypatch, ['Here is the ', _reply()])
    monkeypatch.setattr(provider.settings, 'CALL_ATTEMPTS', 6)
    spoken = []

    with pytest.raises(provider.ProviderUnavailable):
        provider.call_model([], 'sys', [], on_text=spoken.append)
    assert seen['calls'] == 1, 'emitted tokens make a retry unsafe'
    assert spoken == ['Here is the ']


def test_a_MISCONFIGURED_provider_is_not_retried(monkeypatch):
    """Asking again cannot supply a key. Fifteen of these in a row look exactly
    like an agent that can do nothing."""
    from agent import provider

    seen = _flaky(monkeypatch, [provider.ProviderMisconfigured('no key')] * 9)
    monkeypatch.setattr(provider.settings, 'CALL_ATTEMPTS', 6)

    with pytest.raises(provider.ProviderMisconfigured):
        provider.call_model([], 'sys', [])
    assert seen['calls'] == 1


def test_a_MALFORMED_tool_call_is_not_retried(monkeypatch):
    """The model DID answer and answered badly. That is a result, and retrying
    it would quietly convert a scoreable failure into a different one."""
    from agent import provider

    seen = _flaky(monkeypatch, [provider.MalformedToolCall('bad json')] * 9)
    monkeypatch.setattr(provider.settings, 'CALL_ATTEMPTS', 6)

    with pytest.raises(provider.MalformedToolCall):
        provider.call_model([], 'sys', [])
    assert seen['calls'] == 1


def test_the_backoff_GROWS_and_is_capped_and_jittered():
    from agent import provider

    first = [provider._pause(1) for _ in range(40)]
    later = [provider._pause(3) for _ in range(40)]
    base, cap = provider.settings.RETRY_BASE, provider.settings.RETRY_CAP

    assert min(later) > max(first), 'attempt 3 must wait longer than attempt 1'
    assert all(base <= d <= base * 1.5 for d in first)
    assert all(d <= cap * 1.5 for d in [provider._pause(9) for _ in range(40)])
    assert len(set(first)) > 1, 'jitter decorrelates concurrent retries'

# ===== a severed stream was scored as a failed case (measured 2026-09-03)


def _classify(exc):
    """What _reraise_classified does with this exception."""
    from agent import provider

    try:
        provider._reraise_classified(exc)
    except Exception as raised:
        return type(raised).__name__
    return None                           # not ours; the caller re-raises it


def test_a_SEVERED_STREAM_is_an_outage_not_a_failed_case():
    """The SDK wraps httpx on a normal request, but a stream fails while it is
    being ITERATED - after create() returned - so httpx raises through unwrapped.
    Measured: RemoteProtocolError missed the table and was recorded as a failed
    case with turns 0 and tokens 0, which is a score for something never run.
    """
    class RemoteProtocolError(Exception):
        pass

    severed = RemoteProtocolError(
        'peer closed connection without sending complete message body (incomplete chunked read)')

    assert _classify(severed) == 'ProviderUnavailable'


def test_a_transport_error_is_caught_by_its_TYPE_with_no_marker_in_the_message():
    """Separate from the test above, which passes on the message alone: deleting
    the whole type list left that one green. A transport failure whose text says
    nothing recognisable must still be an outage.
    """
    for kind, text in (('ConnectTimeout', 'timed out'),
                       ('ReadTimeout', ''),
                       ('PoolTimeout', 'no connection available'),
                       ('ConnectError', 'All connection attempts failed'),
                       ('ReadError', '')):
        exc = type(kind, (Exception,), {})(text)
        assert _classify(exc) == 'ProviderUnavailable', kind


def test_a_WRAPPED_transport_error_is_caught_by_its_MESSAGE():
    """A type list AND a substring list, because the exception can arrive
    wrapped in something whose name no longer says transport."""
    class SomeWrapper(Exception):
        pass

    for text in ('incomplete chunked read',
                 'peer closed connection',
                 'Response ended prematurely',
                 'Unexpected EOF'):
        assert _classify(SomeWrapper(text)) == 'ProviderUnavailable', text


def test_OUR_OWN_BUG_is_still_scored_and_not_excused():
    """The guard against making this worse. A TypeError has no HTTP status
    either, so a structural 'anything without a status_code is transport' rule
    would excuse real defects as outages - and a masked bug costs more than one
    mis-scored row. The harness scores a crashed agent deliberately.
    """
    for bug in (TypeError('NoneType is not subscriptable'),
                KeyError('messages'),
                ValueError('invalid literal'),
                AttributeError('no attribute blocks')):
        assert _classify(bug) is None, bug


def test_a_MALFORMED_tool_call_is_still_a_RESULT():
    """It means the model answered and answered badly. Excusing it would convert
    a scoreable failure into an excluded run."""
    from agent import provider

    assert _classify(provider.MalformedToolCall('bad json')) is None


def test_a_rejected_key_stays_FATAL():
    class AuthenticationError(Exception):
        pass

    assert _classify(AuthenticationError('401')) == 'ProviderMisconfigured'


def test_a_missing_path_outside_the_workspace_hints_rather_than_raising(
        tmp_workspace, tmp_path):
    """FR-302 as amended lets a path resolve outside the workspace, and
    `relative_to` RAISES on one. Measured: read_file on a missing outside path
    died with ValueError inside the not-found hint."""
    from agent import tools

    outside = tmp_path.parent / "definitely-not-here.txt"
    with pytest.raises(FileNotFoundError) as caught:
        tools.TOOLS["read_file"]["fn"](str(outside), 0, 5)
    # A ValueError here would be the bug; FileNotFoundError with a listing is
    # the design - a wrong path names what IS in the nearest real directory.
    assert "does not exist" in str(caught.value)
    assert "contains" in str(caught.value) or "empty" in str(caught.value)


def test_reading_a_real_file_outside_the_workspace_works(tmp_workspace, tmp_path):
    """The point of the amendment: an assistant can read your own files."""
    from agent import tools

    outside = tmp_path.parent / "outside-note.txt"
    outside.write_text("hello from outside\n", encoding="utf-8")
    out = tools.TOOLS["read_file"]["fn"](str(outside), 0, 5)
    assert "hello from outside" in out


# ===================================================== ask_user (FR-2xx, tool 1)

def test_ask_user_returns_what_the_person_said(monkeypatch):
    """The tool defines the schema; the INTERFACE does the asking. A tool that
    owned a prompt would work in one surface and hang in the others."""
    from agent import tools

    monkeypatch.setattr(tools, "ASK", lambda question, choices: "the second one")
    assert tools.TOOLS["ask_user"]["fn"]("which file?") == "the second one"


def test_ask_user_does_not_block_when_nobody_is_there(monkeypatch):
    """A worker, a cron task and the eval harness all run with no human. The
    tool must ANSWER rather than hang - the same shape as `confirm` degrading
    to `deny` unattended."""
    from agent import tools

    monkeypatch.setattr(tools, "ASK", None)
    out = tools.TOOLS["ask_user"]["fn"]("which file?")
    assert "no one" in out.lower() or "best judgement" in out.lower()
    assert "judgement" in out.lower()


def test_ask_user_passes_the_choices_through(monkeypatch):
    from agent import tools

    seen = {}

    def _ask(question, choices):
        seen.update(question=question, choices=choices)
        return choices[0]

    monkeypatch.setattr(tools, "ASK", _ask)
    got = tools.TOOLS["ask_user"]["fn"]("pick", "alpha | beta | gamma")
    assert seen["choices"] == ["alpha", "beta", "gamma"]
    assert got == "alpha"


def test_ask_user_bounds_the_choice_list(monkeypatch):
    """An unbounded list is a menu nobody reads, and it is also unbounded
    schema-adjacent text in the transcript."""
    from agent import tools

    seen = {}
    monkeypatch.setattr(tools, "ASK",
                        lambda q, c: seen.setdefault("n", len(c)) or c[0])
    tools.TOOLS["ask_user"]["fn"]("pick", " | ".join(f"o{i}" for i in range(12)))
    assert seen["n"] <= tools.MAX_CHOICES


def test_ask_user_survives_an_interface_that_raises(monkeypatch):
    """FR-208: a tool never propagates. An interface that dies mid-question
    must not end the run."""
    from agent import tools

    def _boom(question, choices):
        raise RuntimeError("terminal closed")

    monkeypatch.setattr(tools, "ASK", _boom)
    out = tools.TOOLS["ask_user"]["fn"]("which file?")
    assert "judgement" in out.lower()


def test_ask_user_is_read_risk():
    """Asking changes nothing, so it must not pause for approval - a confirm
    prompt to permit a question is two prompts for one answer."""
    from agent import policy

    assert policy.risk_of("ask_user") == "read"


# ============================================ start/read_terminal (5-6 of 7)

def _wait_for(predicate, seconds: float = 10.0) -> bool:
    """Wait on the CONDITION, on the real clock.

    conftest's autouse `_no_real_pacing` stubs `tools.time.sleep`, and that is
    the `time` module itself - so every sleep in this suite is a no-op. These
    tests need the child to actually run. Event.wait is untouched by the stub,
    and waiting on the condition beats sleeping a guessed interval anyway.
    """
    import threading
    import time

    gate, deadline = threading.Event(), time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        gate.wait(0.05)
    return bool(predicate())


def test_a_session_keeps_running_between_reads(tmp_workspace):
    """The gap: run_shell is one-shot with a timeout, so it cannot hold a dev
    server, tail a log, or keep a REPL. This is the same process, twice."""
    import sys
    import time

    from agent import tools

    code = "import sys,time\nfor i in range(30):\n print(i,flush=True);time.sleep(0.05)"
    out = tools.TOOLS["start_terminal"]["fn"](f'{sys.executable} -c "{code}"')
    assert "started" in out.lower()
    name = out.split()[-1]
    entry = tools._TERMINALS[name]
    assert _wait_for(lambda: entry["buffer"]), "the session printed nothing"
    first = tools.TOOLS["read_terminal"]["fn"](name)
    assert _wait_for(lambda: entry["buffer"]), "the session stopped printing"
    second = tools.TOOLS["read_terminal"]["fn"](name)
    tools.stop_terminals()
    assert first.strip(), "nothing came back from the first read"
    assert first != second, "the session is not still running"


def test_reading_an_unknown_session_names_the_ones_that_exist(tmp_workspace):
    from agent import tools

    tools.stop_terminals()
    out = tools.TOOLS["read_terminal"]["fn"]("nope")
    assert "no " in out.lower() or "unknown" in out.lower()


def test_a_read_returns_only_what_is_new(tmp_workspace):
    """Re-sending the whole buffer every read is how a long-running log floods
    the context - the flood shrink() exists to stop, arriving by another door."""
    import sys
    import time

    from agent import tools

    code = "import sys,time\nfor i in range(20):\n print('line',i,flush=True);time.sleep(0.05)"
    name = tools.TOOLS["start_terminal"]["fn"](
        f'{sys.executable} -c "{code}"').split()[-1]
    entry = tools._TERMINALS[name]
    assert _wait_for(lambda: entry["buffer"]), "the session printed nothing"
    first = tools.TOOLS["read_terminal"]["fn"](name)
    assert _wait_for(lambda: entry["buffer"]), "the session stopped printing"
    second = tools.TOOLS["read_terminal"]["fn"](name)
    tools.stop_terminals()
    assert first.strip() and second.strip(), "a read came back empty"
    overlap = set(first.split()) & set(second.split()) - {"line"}
    assert not overlap, f"the same output came back twice: {overlap}"


def test_reading_a_LIVE_session_returns_instead_of_blocking(tmp_workspace):
    """readline() returns "" only at EOF, so draining a process that is STILL
    RUNNING never ends. Measured: a scored run sat 112 minutes at 0.01% CPU and
    the `tools` split produced 0 rows in 9 attempts. MAX_SECONDS is checked
    BETWEEN turns, so a turn that never returns is never checked.

    Read on a thread so the failure is a 10-second red, not a hung suite.
    """
    import sys
    import threading
    import time

    from agent import tools

    tools.stop_terminals()
    code = "import time; print('ready', flush=True); time.sleep(60)"
    name = tools.TOOLS["start_terminal"]["fn"](
        f'{sys.executable} -c "{code}"').split()[-1]
    entry = tools._TERMINALS[name]
    assert _wait_for(lambda: entry["buffer"]), "the session printed nothing"

    got: list = []
    reader = threading.Thread(
        target=lambda: got.append(tools.TOOLS["read_terminal"]["fn"](name)),
        daemon=True)
    reader.start()
    reader.join(timeout=10)
    alive = reader.is_alive()
    tools.stop_terminals()

    assert not alive, "read_terminal blocked on a session that is still running"
    assert "ready" in got[0]


def test_a_finished_session_says_it_finished(tmp_workspace):
    import sys
    import time

    from agent import tools

    name = tools.TOOLS["start_terminal"]["fn"](
        f'{sys.executable} -c "print(42)"').split()[-1]
    tools._TERMINALS[name]["process"].wait(timeout=10)
    out = tools.TOOLS["read_terminal"]["fn"](name)
    tools.stop_terminals()
    assert "42" in out
    assert "exit" in out.lower() or "finished" in out.lower()


class _Exited:
    """A process that has already finished. Its reader has not."""

    pid = -1

    def poll(self):
        return 0

    def kill(self):
        pass


def test_a_finished_session_does_not_lose_the_readers_last_lines(tmp_workspace):
    """poll() can return before the reader has drained the pipe, and the last
    line is the one that matters - `watch-build` reports its artifact id there.

    Built by hand: the real race is microseconds wide, so a test driving a real
    child would pass with or without the join and prove nothing.
    """
    import threading

    from agent import tools

    tools.stop_terminals()
    entry = {"process": _Exited(), "buffer": ["building" + chr(10)],
             "command": "x", "lock": threading.Lock()}

    def still_reading():
        threading.Event().wait(0.2)
        with entry["lock"]:
            entry["buffer"].append("artifact zr7k2q" + chr(10))

    entry["reader"] = threading.Thread(target=still_reading, daemon=True)
    entry["reader"].start()
    tools._TERMINALS["t1"] = entry

    out = tools.TOOLS["read_terminal"]["fn"]("t1")
    tools.stop_terminals()

    assert "artifact zr7k2q" in out, "the reader's last lines were dropped"


def test_sessions_are_bounded(tmp_workspace):
    """An agent that starts one per turn would hold the machine."""
    import sys

    from agent import tools

    tools.stop_terminals()
    started = [tools.TOOLS["start_terminal"]["fn"](f'{sys.executable} -c "input()"')
               for _ in range(tools.MAX_TERMINALS + 2)]
    tools.stop_terminals()
    assert any("too many" in s.lower() or "refused" in s.lower() for s in started)


def test_stop_terminals_leaves_nothing_running(tmp_workspace):
    """The orphan case is the one that matters: two `sleep 60` once survived a
    timeout and held the workspace for the rest of the run."""
    import sys

    from agent import tools

    name = tools.TOOLS["start_terminal"]["fn"](
        f'{sys.executable} -c "import time;time.sleep(60)"').split()[-1]
    assert tools._TERMINALS
    tools.stop_terminals()
    assert not tools._TERMINALS
    assert "no " in tools.TOOLS["read_terminal"]["fn"](name).lower()


def test_the_terminal_tools_carry_the_right_risk():
    from agent import policy

    # `write`, matching run_shell. `destructive` became `confirm`, which
    # autonomous turns into `deny`, so the tool was unreachable from the worker,
    # cron and the harness while run_shell ran the same command unattended.
    assert policy.risk_of("start_terminal") == "write"
    assert policy.risk_of("run_shell") == "write"
    assert policy.risk_of("read_terminal") == "read"


def test_start_terminal_is_reachable_unattended():
    from agent import policy

    verdict, _ = policy.classify("start_terminal", {"command": "python3 -m http.server"},
                                 autonomous=True)
    assert verdict == "auto"


def test_a_dangerous_command_still_escalates_through_start_terminal():
    """The half that keeps `write` from being a hole: run_shell's DANGER check
    reads args["command"], and start_terminal's argument has the same name."""
    from agent import policy

    for command in ("rm -rf /", "dd if=/dev/zero of=/dev/sda", "mkfs.ext4 /dev/sda1"):
        shell, _ = policy.classify("run_shell", {"command": command}, autonomous=True)
        terminal, _ = policy.classify("start_terminal", {"command": command},
                                      autonomous=True)
        assert terminal == shell == "deny", command


def test_run_python_is_not_a_second_door_past_the_DANGER_escalation():
    """MEASURED 2026-09-10, and the agent found it unaided.

    `run_shell("python3 -c X")` escalated to destructive and was denied in every
    autonomous context. `run_python(X)` ran the SAME source at `auto`, because
    the escalation read `args["command"]` and run_python's argument is named
    `code`. On serve-token the passing runs simply switched tools.

    An escalation one tool enforces and another ignores is not a boundary.
    """
    from agent import policy

    for source in ("import shutil; shutil.rmtree('/etc/ssl')",
                   "import os; os.system('rm -rf /')",
                   "open('/etc/sudoers', 'w').write('x')"):
        shell, _ = policy.classify("run_shell", {"command": f'python3 -c "{source}"'},
                                   autonomous=True)
        inline, _ = policy.classify("run_python", {"code": source}, autonomous=True)

        assert shell == "deny", f"the shell route stopped enforcing: {source}"
        assert inline == shell, (
            f"run_python ran at {inline!r} what run_shell refused: {source}")


def test_ordinary_python_still_runs_unattended():
    """The escalation must not swallow the tool it guards. run_python exists so
    a worker, a cron task and the harness can compute without a person there."""
    from agent import policy

    for source in ("print(sum(range(10)))",
                   "import json; print(json.dumps({'a': 1}))",
                   "import pathlib; print(pathlib.Path('app.py').read_text())"):
        verdict, _ = policy.classify("run_python", {"code": source}, autonomous=True)
        assert verdict == "auto", f"refused ordinary code: {source}"


# ================================================= AGENT_TOOLS_OFF (ablation)

def test_a_named_tool_is_dropped_from_the_live_set(monkeypatch):
    """The control arm. Without this the comparison is two numbers measured on
    two different binaries, which is not a control."""
    from agent import config, tools

    monkeypatch.setattr(config, "TOOLS_OFF", frozenset({"web_search"}))
    live = tools.builtins()
    assert "web_search" not in live
    assert "read_file" in live


def test_several_tools_drop_at_once(monkeypatch):
    from agent import config, tools

    monkeypatch.setattr(config, "TOOLS_OFF",
                        frozenset({"web_search", "ask_user", "start_terminal"}))
    live = tools.builtins()
    assert not {"web_search", "ask_user", "start_terminal"} & set(live)
    assert len(live) == len(tools.TOOLS) - 3


def test_an_empty_switch_changes_nothing(monkeypatch):
    """The default path must be the SAME object, not a copy - every turn pays
    for this and a rebuilt dict per call is rent for nothing."""
    from agent import config, tools

    monkeypatch.setattr(config, "TOOLS_OFF", frozenset())
    monkeypatch.setattr(config, "WEB_ENABLED", True)
    assert tools.builtins() is tools.TOOLS


def test_the_web_switch_still_works_on_its_own(monkeypatch):
    """AGENT_WEB predates AGENT_TOOLS_OFF and the search split's 9/9 vs 0/9 is
    quoted against that name."""
    from agent import config, tools

    monkeypatch.setattr(config, "TOOLS_OFF", frozenset())
    monkeypatch.setattr(config, "WEB_ENABLED", False)
    assert "web_search" not in tools.builtins()


def test_both_switches_compose(monkeypatch):
    from agent import config, tools

    monkeypatch.setattr(config, "TOOLS_OFF", frozenset({"ask_user"}))
    monkeypatch.setattr(config, "WEB_ENABLED", False)
    live = tools.builtins()
    assert "ask_user" not in live and "web_search" not in live


def test_a_name_that_is_not_a_tool_is_ignored(monkeypatch):
    """A typo in the switch must not silently drop nothing AND look like it
    worked - it drops nothing, and the run's `tools` field says what was live."""
    from agent import config, tools

    monkeypatch.setattr(config, "TOOLS_OFF", frozenset({"no_such_tool"}))
    assert len(tools.builtins()) == len(tools.TOOLS)


def test_the_switch_parses_a_comma_list(monkeypatch):
    import importlib

    from agent import config

    monkeypatch.setenv("AGENT_TOOLS_OFF", " todo , ask_user ,, ")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.TOOLS_OFF == frozenset({"todo", "ask_user"})
    finally:
        monkeypatch.delenv("AGENT_TOOLS_OFF")
        importlib.reload(config)


# ======================================= url_is_safe (P)

import pytest


@pytest.mark.parametrize("url, why", [
    ("http://169.254.169.254/latest/meta-data/", "AWS/GCP/Azure metadata"),
    ("http://169.254.170.2/v2/credentials", "ECS task IAM credentials"),
    ("http://[::ffff:169.254.169.254]/", "the IPv4-mapped form of the same"),
    ("http://100.100.100.200/", "Alibaba metadata, inside CGNAT"),
    ("http://metadata.google.internal/computeMetadata/v1/", "by name"),
    ("http://127.0.0.1:8080/", "loopback"),
    ("http://localhost:8080/", "loopback by name"),
    ("http://10.0.0.5/", "private"),
    ("http://192.168.1.1/", "private"),
    ("http://172.16.0.1/", "private"),
    ("http://100.64.0.1/", "CGNAT, which is_private does NOT cover"),
    ("http://0.0.0.0/", "unspecified"),
])
def test_the_addresses_an_agent_must_not_reach(url, why, monkeypatch):
    from agent import tools

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    ok, reason = tools.url_is_safe(url)
    assert not ok, f"{url} was allowed, and it is {why}"
    assert reason


def test_cgnat_is_blocked_because_ipaddress_calls_it_neither(monkeypatch):
    """The reason it needs its own network: ipaddress returns False for BOTH
    is_private and is_global on 100.64.0.0/10, so a check built on is_private
    lets carrier NAT and Tailscale straight through."""
    import ipaddress

    address = ipaddress.ip_address("100.64.0.1")
    assert not address.is_private and not address.is_global

    from agent import tools

    assert tools._blocked_ip(address)


def test_the_mapped_form_is_unwrapped_before_the_decision():
    """::ffff:x.x.x.x is a DIFFERENT object to ipaddress, so a resolver that
    returns the mapped form walks past a set-membership test."""
    import ipaddress

    from agent import tools

    assert tools._blocked_ip(ipaddress.ip_address("::ffff:127.0.0.1"))
    assert tools._blocked_ip(ipaddress.ip_address("::ffff:10.0.0.1"))


def test_a_scheme_that_is_not_http_is_refused():
    from agent import tools

    for url in ("file:///etc/passwd", "gopher://x/", "ftp://host/f",
                "data:text/html,<script>"):
        ok, reason = tools.url_is_safe(url)
        assert not ok and "http" in reason


def test_a_public_address_is_allowed(monkeypatch):
    from agent import tools

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    ok, reason = tools.url_is_safe("http://93.184.216.34/")
    assert ok, reason


def test_every_resolved_address_is_checked_not_the_first(monkeypatch):
    """A host with one public and one private record is the whole attack, and
    checking only the first answer misses it."""
    import socket

    from agent import tools

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80)),
    ])
    ok, reason = tools.url_is_safe("http://split-horizon.example/")
    assert not ok and "169.254.169.254" in reason


def test_a_proxy_defers_resolution_rather_than_guessing(monkeypatch):
    """Behind a proxy the NAME travels and the proxy resolves it. The scored
    container has no DNS of its own, so resolving locally fails outright."""
    import socket

    from agent import tools

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8888")

    def _boom(*_a, **_k):
        raise AssertionError("resolved locally while a proxy was configured")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    assert tools.url_is_safe("http://example.com/")[0]


def test_a_host_that_does_not_resolve_is_refused_not_allowed(monkeypatch):
    """Fails CLOSED, unlike robots_allows which deliberately fails open. A name
    that cannot be checked has not been shown to be safe."""
    import socket

    from agent import tools

    monkeypatch.delenv("HTTPS_PROXY", raising=False)

    def _fail(*_a, **_k):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    ok, reason = tools.url_is_safe("http://nowhere.invalid/")
    assert not ok and "resolve" in reason


def test_a_url_with_no_host_is_refused():
    from agent import tools

    assert not tools.url_is_safe("http:///path")[0]
    assert not tools.url_is_safe("")[0]


def test_a_trailing_dot_does_not_evade_the_name_blocklist(monkeypatch):
    """metadata.google.internal. is the same host to a resolver."""
    from agent import tools

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    ok, _ = tools.url_is_safe("http://metadata.google.internal./")
    assert not ok


def test_the_injected_skill_is_named_not_just_counted(tmp_workspace, monkeypatch):
    """`skill_opened` carried a character count and nothing else, so no later node
    could say WHICH skill was injected. Phase R needs the identity: `finish` marks
    that skill suspect when the run fails, and the eval verifies the mechanism fired
    by matching this name against the skill written afterwards."""
    from agent import skills
    from agent.graph import act

    monkeypatch.setattr(skills, "opening", lambda goal: "# The skill\n\nbody")
    monkeypatch.setattr(skills, "best_match", lambda goal: {"name": "qz-release"})
    use_fake(monkeypatch, [text_turn("Done.")])
    trace = []
    act(state(), {"configurable": {"trace": trace}})

    opened = [e for e in trace if e.get("kind") == "skill_opened"]
    assert opened, "the skill was injected and the trace does not say so"
    assert opened[0]["name"] == "qz-release"
# =============================== finish marks a failed skill suspect (Phase R)

def _finished(monkeypatch, verdict, matched="deploy-guide", failures=0, revision=True):
    """Run `finish` with a named skill matching this goal, and return nothing.

    `skills.matched` is patched rather than a library staged: what is under test is
    the RULE finish applies, not the matcher, which test_skills covers. `revision`
    is explicit because the default is OFF since 2026-09-13.
    """
    from agent import config, graph, skills

    monkeypatch.setattr(config, "SKILL_REVISION", revision)
    monkeypatch.setattr(skills, "matched", lambda goal: matched)
    monkeypatch.setattr(skills, "extract",
                        lambda *a, **k: [])          # R2 is a separate rule
    graph.finish(state(verdict=verdict, failures=failures),
                 {"configurable": {"thread_id": "t1", "trace": []}})


def test_finish_marks_the_open_skill_when_the_run_ends_badly(tmp_workspace,
                                                             monkeypatch):
    from agent import memory

    _finished(monkeypatch, "stuck")

    assert memory.is_suspect("deploy-guide") is True


def test_finish_marks_on_budget_too_not_only_stuck(tmp_workspace, monkeypatch):
    """`budget` is the other way a run ends without finishing. Leaving it out would
    make the mark depend on WHICH cap bound, which says nothing about the skill."""
    from agent import memory

    _finished(monkeypatch, "budget")

    assert memory.is_suspect("deploy-guide") is True


def test_finish_marks_when_the_run_kept_failing_even_if_it_ended_done(tmp_workspace,
                                                                      monkeypatch):
    """Three consecutive failed calls is the third trigger."""
    from agent import memory

    _finished(monkeypatch, "done", failures=3)

    assert memory.is_suspect("deploy-guide") is True


def test_finish_does_NOT_mark_a_skill_on_a_run_that_worked(tmp_workspace, monkeypatch):
    from agent import memory

    _finished(monkeypatch, "done")

    assert memory.is_suspect("deploy-guide") is False


def test_a_bad_run_with_NO_skill_open_marks_nothing(tmp_workspace, monkeypatch):
    """A failure with no skill injected is a failure, not evidence about a skill."""
    from agent import memory

    _finished(monkeypatch, "stuck", matched="")

    assert memory._connect().execute(
        "SELECT count(*) FROM skill_failures").fetchone()[0] == 0


def test_the_control_arm_marks_nothing(tmp_workspace, monkeypatch):
    """AGENT_SKILL_REVISION=off must change the mechanism, or the two eval arms are
    the same build measured twice."""
    from agent import memory

    _finished(monkeypatch, "stuck", revision=False)

    assert memory.is_suspect("deploy-guide") is False


def test_a_missing_ddgs_says_so_instead_of_raising_ModuleNotFoundError(monkeypatch):
    """Measured in the TUI: web_search failed at 0.0s with a bare
    ModuleNotFoundError, because `ddgs` was installed in the Containerfile and
    declared in no dependency list - so the container had it and every other
    install shipped a tool that could only fail. A blocked engine and an absent
    library must not look identical."""
    import sys

    from agent.tools import web_search

    monkeypatch.setitem(sys.modules, "ddgs", None)
    with pytest.raises(RuntimeError) as raised:
        web_search("anything")

    message = str(raised.value)
    assert "not installed" in message
    assert "pip install ddgs" in message


def test_web_search_is_a_declared_dependency_and_not_only_a_container_one():
    """The defect above in one line: the tool imports it, so the package
    declares it."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    declared = (root / "pyproject.toml").read_text(encoding="utf-8")
    pinned = re.search(r'"ddgs==([\d.]+)"', declared)
    assert pinned, "web_search imports ddgs; pyproject must declare it"

    container = (root / "Containerfile").read_text(encoding="utf-8")
    assert f'"ddgs=={pinned.group(1)}"' in container, (
        "the container and the package must install the SAME ddgs")


# ============================== subprocess text decoding (Windows host, 2026-09-09)

def test_shell_output_survives_bytes_the_platform_default_cannot_decode(tmp_workspace):
    """Measured on the Windows host: `text=True` with no encoding decodes as
    cp1252, and a UTF-8 byte raises UnicodeDecodeError INSIDE subprocess's reader
    thread. run_shell then returned `exit code: 0` with stdout `None` - the command
    succeeded, its output was destroyed, and the agent was told there was nothing
    there. Never fired in the container, which is Linux and UTF-8."""
    import sys as _sys

    from agent.tools import run_shell

    out = run_shell(
        f'{_sys.executable} -c "import sys; '
        f'sys.stdout.buffer.write(bytes([0x42,0xc4,0x81,0x64]))"')

    assert "None" not in out, "output was lost to a decode error"
    assert "B\u0101d" in out


def test_every_subprocess_call_declares_its_encoding():
    """The guard, at the source. Four sites carried `text=True` and no encoding;
    a fifth added later would reintroduce a failure that only appears off Linux,
    which is exactly the kind that reaches a user before a test."""
    import pathlib
    import re

    source = (pathlib.Path(__file__).resolve().parent.parent
              / "agent" / "tools.py").read_text(encoding="utf-8")
    calls = re.findall(r"subprocess\.(?:Popen|run)\((?:[^()]|\([^()]*\))*\)", source)

    assert calls, "the scan found no subprocess calls - the pattern is wrong"
    for call in calls:
        if "text=True" in call:
            assert "encoding=" in call, f"no encoding declared in: {call[:90]}"


def test_run_python_carries_non_ascii_in_BOTH_directions(tmp_workspace):
    """Two ends, two defects. The parent's `encoding=` says how it ENCODES what
    it sends; the child still read stdin and wrote stdout as the platform
    default. On Windows the source arrived as surrogates and ast.parse refused
    it, and a print() of any non-ASCII character killed the script."""
    from agent.tools import run_python

    out = run_python('print("Ganar\u0101jya \u4e2d\u6587")')
    assert "exit code: 0" in out
    assert "Ganar\u0101jya \u4e2d\u6587" in out


def test_the_python_driver_sets_the_childs_own_encoding():
    """The parent cannot set it for the child, so the driver does. A source-level
    guard because the failure only appears off Linux."""
    from agent.tools import _PYTHON_DRIVER

    assert 'sys.stdin.buffer.read().decode("utf-8"' in _PYTHON_DRIVER
    assert 'sys.stdout.reconfigure(encoding="utf-8"' in _PYTHON_DRIVER
# ================================ the current date (2026-09-09)

def test_the_system_prompt_states_todays_date(tmp_workspace, monkeypatch):
    """Nothing told the agent what day it was. Reported from the TUI: asked what
    happened "yesterday", it answered about August 17 2025 - its training cutoff -
    searching five times for 2025 news and fetching a 2025 Wikipedia archive. It
    hedged "assuming yesterday was August 17, 2025", so it knew it was guessing
    and had no way to find out."""
    import datetime

    from agent.graph import act

    seen = use_fake(monkeypatch, [text_turn("ok")])
    act(state(), {"configurable": {}})

    assert seen, "act did not build a system prompt"
    assert datetime.date.today().isoformat() in seen[0]["system"]


def test_the_date_is_read_per_turn_not_pinned_at_import(monkeypatch):
    """A date bound at import is wrong for every long-running process - the
    worker, a cron task, a TUI left open overnight - and CE-05 forbids the
    library resolving it at import anyway."""
    from agent import graph

    live = graph._today()
    monkeypatch.setattr(graph, "_today", lambda: "2030-01-01")

    assert graph._today() == "2030-01-01"
    assert live != "2030-01-01", "the real one must not be a frozen constant"


def test_the_date_line_is_cheap(tmp_workspace, monkeypatch):
    """It is charged on EVERY model call, on a provider that caches nothing.
    A paragraph here is rent; one line is the whole budget for knowing the date."""
    from agent.graph import act

    seen = use_fake(monkeypatch, [text_turn("ok")])
    act(state(), {"configurable": {}})

    dated = [line for line in seen[0]["system"].splitlines() if graph_today() in line]
    assert len(dated) == 1, "the date must appear once, not throughout the prompt"
    assert len(dated[0]) < 120, f"the date line is {len(dated[0])} chars"


def graph_today():
    from agent import graph
    return graph._today()
