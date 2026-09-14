"""reflect() — every verdict branch, in the order the checks must run.

The order is part of the contract, not an implementation detail, so precedence is
asserted directly rather than inferred.
"""
import pytest

from agent import config
from agent.graph import reflect


@pytest.fixture(autouse=True)
def _coding_posture(monkeypatch):
    """Pin the CODING posture for this module.

    Every test here predates the posture gate and asserts its rule - one text reply
    is a preamble and must not end a run. That rule is the coding posture's, and
    `is_code_workspace()` reads whatever workspace the process happens to have, so
    without this they pass or fail on TEST ORDER. Measured: they passed in a full
    run and failed running test_skills.py and test_nodes.py together.

    The two tests that assert the other posture patch it False themselves.
    """
    monkeypatch.setattr("agent.graph.is_code_workspace", lambda: True)


def state(**over):
    base = {
        "messages": [{"role": "user", "content": "fix it"}],
        "turns": 0, "max_turns": 12,
        "spent_tokens": 0, "budget_tokens": 200_000,
        "failures": 0, "verdict": None, "approved": [], "denied": [],
    }
    base.update(over)
    return base


def over_threshold():
    """A history just past COMPACT_AT_CHARS, sized from the constant.

    Hardcoding a pair count silently stops testing compaction the moment the
    threshold moves - which is exactly what happened when it went 45k -> 200k.
    """
    from agent.context import context_chars

    pair = [assistant_call(), tool_result()]
    messages, n = [], 0
    while context_chars(messages) <= config.COMPACT_AT_CHARS:
        messages += pair
        n += 1
        if n > 100_000:                       # a runaway guard, never reached
            raise AssertionError("could not exceed COMPACT_AT_CHARS")
    return messages


def assistant_text(text="Looking into it."):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def assistant_call(name="run_shell", **args):
    return {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": name, "input": args or {"command": "ls"}}]}


def tool_result(is_error=False):
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": is_error}]}


# --- each branch in isolation ---------------------------------------------

def test_a_large_context_compacts():
    """FR-403 fires on CONTEXT SIZE, not on cumulative spend.

    The distinction is the whole reason compaction can exist: spent_tokens only
    ever grows, so a spend-based trigger would fire on every turn forever once
    crossed - compact, act, compact, act - now that the verdict is no longer
    terminal.
    """
    big = state(messages=over_threshold())
    assert reflect(big)["verdict"] == "compact"


def test_a_small_context_does_not_compact():
    assert reflect(state())["verdict"] != "compact"


def test_a0_budget_exhaustion_is_its_own_terminal_verdict():
    """NFR-401 asks for a HARD stop and nothing provided one: the old check fired
    at 60% and terminated, which looked like a budget stop while actually being a
    compaction trigger. FR-104 names "budget exhausted" as a terminal outcome."""
    assert reflect(state(spent_tokens=200_000))["verdict"] == "budget"
    assert reflect(state(spent_tokens=199_999))["verdict"] != "budget"


def test_a1_the_compaction_cap_stops_a_loop():
    """Compacted the maximum number of times and still over the threshold. Stop,
    rather than spend a model call per turn clearing nothing."""
    big = state(messages=over_threshold(),
                compact_count=config.MAX_COMPACTIONS)
    assert reflect(big)["verdict"] == "stuck"


def test_b_turn_cap_asks_for_a_summary_first():
    """Reaching the cap no longer ends the run silently; a capped run otherwise
    records nothing about what it learned."""
    out = reflect(state(turns=12))

    assert out["verdict"] == "continue"
    assert out["summarised"] is True
    assert "Do not call any more tools" in out["messages"][-1]["content"]


def test_b_turn_cap_is_stuck_once_the_summary_was_asked_for():
    """Bounded at ONE extra turn. Without the flag the cap would re-ask forever."""
    assert reflect(state(turns=12, summarised=True))["verdict"] == "stuck"


def test_the_summary_reply_ends_the_run_as_stuck_not_done():
    """A summary does not change the workspace, and the check command reads the
    workspace. Ending `done` here would be the truncated-reply defect again.

    BELOW the cap on purpose. At turns >= max_turns branch (b) answers first, so a
    test at the cap never reaches this branch - the first version of this test made
    exactly that mistake and passed while the branch returned `done`. Refunding a
    truncated turn is what puts a summarised run back under the cap.
    """
    out = reflect(state(turns=5, summarised=True,
                        messages=[assistant_call(), tool_result(),
                                  assistant_text("I could not fix it.")]))

    assert out["verdict"] == "stuck"


def test_a_truncated_turn_is_refunded():
    """The reply was cut off mid-sentence, so the turn bought no completed
    thought. Charging for it spends the cap on the output limit rather than on
    work - 21 recorded runs ended on stop_reason=length."""
    out = reflect(state(turns=7, truncated=True,
                        messages=[assistant_call(), tool_result(),
                                  assistant_text("I was saying tha")]))

    assert out["verdict"] == "continue"
    assert out["turns"] == 6, "the truncated turn must not be charged"


def test_the_refund_never_goes_below_zero():
    out = reflect(state(turns=0, truncated=True,
                        messages=[assistant_call(), tool_result(),
                                  assistant_text("cut off")]))

    assert out["turns"] == 0


def test_a_normal_turn_is_NOT_refunded():
    """Only a truncated reply is free. Refunding an ordinary turn would make the
    cap unreachable."""
    out = reflect(state(turns=7,
                        messages=[assistant_call(), tool_result()]))

    assert out.get("turns") is None, "reflect must not touch turns on a normal turn"


def test_c_three_identical_signatures_is_stuck():
    s = state(messages=[assistant_call(), tool_result()] * 3)
    assert reflect(s)["verdict"] == "stuck"


def test_c_two_identical_then_different_is_not_stuck():
    s = state(messages=[
        assistant_call(command="ls"), tool_result(),
        assistant_call(command="ls"), tool_result(),
        assistant_call(command="pytest"), tool_result(),
    ])
    assert reflect(s)["verdict"] != "stuck"


def test_d_three_consecutive_failures_is_stuck():
    """`replan` was never one of FR-104's four terminal outcomes, and it fired
    ONCE in 712 recorded rows. Three consecutive failures is a way of being
    stuck; `failures` stays on the row so the cause is still distinguishable."""
    s = state(failures=3, messages=[assistant_call(), tool_result(is_error=True)])
    assert reflect(s)["verdict"] == "stuck"


def test_f_after_a_tool_result_continues():
    assert reflect(state(messages=[assistant_call(), tool_result()]))["verdict"] == "continue"


# --- correction (b): the termination guard --------------------------------

def test_text_only_reply_before_any_tool_call_continues():
    """The exact trap: 'Let me look at the test file first.' must NOT finish the run.

    A cursor-based check with no plan node evaluates 1 >= 0 and returns `done` here.
    """
    s = state(messages=[
        {"role": "user", "content": "fix it"},
        assistant_text("Let me look at the test file first."),
    ])
    assert reflect(s)["verdict"] == "continue"


def test_text_only_reply_after_a_tool_call_is_done():
    s = state(messages=[
        {"role": "user", "content": "fix it"},
        assistant_call(), tool_result(),
        assistant_text("Tests pass."),
    ])
    assert reflect(s)["verdict"] == "done"


# --- precedence: the order of the checks is the contract -------------------

def test_budget_exhaustion_beats_everything():
    """A run that has spent its budget stops, whatever else is true - including a
    context large enough to compact, which would otherwise spend more."""
    big = state(messages=over_threshold(),
                spent_tokens=200_000, turns=99)
    assert reflect(big)["verdict"] == "budget"


def test_compaction_beats_the_turn_cap():
    """Order matters: a run with room to compact should compact rather than be
    declared stuck, or compaction never fires on the runs that need it."""
    big = state(messages=over_threshold(), turns=99)
    assert reflect(big)["verdict"] == "compact"


def test_turn_cap_beats_thrash():
    """(b) is still reached before (c). It now asks for a summary rather than
    saying stuck outright, and the ORDERING is what this guards."""
    s = state(turns=12, messages=[assistant_call(), tool_result()] * 3)
    assert reflect(s)["verdict"] == "continue"

    s = state(turns=12, summarised=True,
              messages=[assistant_call(), tool_result()] * 3)
    assert reflect(s)["verdict"] == "stuck"


def test_thrash_beats_failures():
    s = state(failures=99, messages=[assistant_call(), tool_result(is_error=True)] * 3)
    assert reflect(s)["verdict"] == "stuck", "(c) must be evaluated before (d)"


def test_failures_beats_done():
    """A run that has failed three times in a row is stuck rather than done."""
    s = state(failures=3, messages=[assistant_call(), tool_result(), assistant_text("Done.")])
    assert reflect(s)["verdict"] == "stuck"


@pytest.mark.parametrize("verdict", ["done", "stuck", "budget", "compact", "continue"])
def test_every_verdict_is_reachable(verdict):
    """Guards against a branch being deleted or shadowed by a reordering.

    FR-104: the TERMINAL outcomes are done, stuck and budget - the turn cap is
    reported as stuck. `compact` and `continue` are not terminal; compact now
    routes to the compaction node and back to act.
    """
    cases = {
        "compact": state(messages=over_threshold()),
        "budget": state(spent_tokens=200_000),
        "stuck": state(turns=12, summarised=True),
        "done": state(messages=[assistant_call(), tool_result(), assistant_text("Done.")]),
        "continue": state(messages=[assistant_call(), tool_result()]),
    }
    assert reflect(cases[verdict])["verdict"] == verdict


def test_replan_is_gone():
    """FR-104 names exactly four terminal outcomes. Nothing may return a fifth."""
    for s in (state(failures=3, messages=[assistant_call(), tool_result(is_error=True)]),
              state(failures=99, messages=[assistant_call(), tool_result(is_error=True)])):
        assert reflect(s)["verdict"] in ("done", "stuck", "budget", "compact", "continue")


# ============================== Stage 0: context size is recorded on EVERY turn
#
# The reason this instrument exists: `before`/`after` live on the `compact` trace
# entry, which only exists once the trigger has already fired. A stop-gate coming
# back with `compact_count: 0` across six runs was therefore uninterpretable -
# peaking at 44,000 chars means raise the threshold, peaking at 12,000 means
# compaction is irrelevant to the workload, and those call for opposite actions.


def test_context_size_is_traced_on_every_turn_not_only_when_it_fires():
    trace = []
    reflect(state(), {"configurable": {"trace": trace}})

    sizes = [e for e in trace if e.get("kind") == "context"]
    assert len(sizes) == 1, "a turn that does not compact must still record its size"
    assert sizes[0]["chars"] > 0
    assert sizes[0]["chars"] < config.COMPACT_AT_CHARS


def test_the_recorded_size_is_the_one_the_trigger_used():
    """One number, one source. If these could differ, the row would describe a
    threshold check that never happened."""
    from agent.context import context_chars

    big = state(messages=over_threshold())
    trace = []
    verdict = reflect(big, {"configurable": {"trace": trace}})["verdict"]

    recorded = [e["chars"] for e in trace if e.get("kind") == "context"][0]
    assert recorded == context_chars(big["messages"])
    assert verdict == "compact"


def test_reflect_still_works_with_no_config_at_all():
    """Every direct caller in this file passes state alone, and _timed() only
    passes config to nodes whose signature accepts it. The default keeps both
    callers true."""
    assert reflect(state())["verdict"] != "compact"
    assert reflect(state(), None)["verdict"] != "compact"
    assert reflect(state(), {})["verdict"] != "compact"


def test_the_compaction_threshold_is_settable_without_editing_source(monkeypatch):
    """A threshold that can only be changed by editing config.py cannot be tuned
    by measurement, only by argument. Both of the previous two stages were caught
    by the same omission on their own kill switches."""
    small = state()
    assert reflect(small)["verdict"] != "compact"

    monkeypatch.setattr(config, "COMPACT_AT_CHARS", 10)
    assert reflect(small)["verdict"] == "compact"


def test_the_cap_is_settable_too(monkeypatch):
    big = state(messages=over_threshold(), compact_count=1)
    monkeypatch.setattr(config, "MAX_COMPACTIONS", 1)
    assert reflect(big)["verdict"] == "stuck", "at the cap, stop rather than loop"
    monkeypatch.setattr(config, "MAX_COMPACTIONS", 5)
    assert reflect(big)["verdict"] == "compact"


# ============================ the thrash detector is risk-aware (real-humanize)


def _repeat(name, n, **args):
    """A history with the same call made n times, each answered."""
    out = [{"role": "user", "content": "go"}]
    for i in range(n):
        out += [{"role": "assistant", "content": [
                    {"type": "tool_use", "id": f"t{i}", "name": name, "input": args}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "content": "x"}]}]
    return out


def test_three_identical_reads_are_not_thrash():
    """THE REGRESSION THIS EXISTS FOR, measured on real-humanize.

    A failing run and a passing run were identical for 12 turns. The failing one
    read the same window a third time and was killed at turn 13 of 30; the
    passing one called edit_file instead and took the case 4 failures to 0. A
    read changes nothing, so repeating it is confusion, not a loop - and killing
    a run mid-diagnosis threw away 17 turns.
    """
    from agent.graph import _last_three_signatures_identical as thrash
    from agent.policy import sync

    sync()
    assert not thrash(_repeat("read_file", 3, path="a.py"))
    assert not thrash(_repeat("search_files", 3, pattern="x"))
    assert not thrash(_repeat("read_file", 4, path="a.py"))


def test_a_read_repeated_far_past_reason_is_still_thrash():
    """Idempotent does not mean unlimited: five identical reads is a loop."""
    from agent.graph import _last_three_signatures_identical as thrash
    from agent.policy import sync

    sync()
    assert thrash(_repeat("read_file", 5, path="a.py"))


def test_three_identical_writes_are_still_thrash():
    """Unchanged, and deliberately: a repeated write or command is the harmful
    signal the detector was built for."""
    from agent.graph import _last_three_signatures_identical as thrash
    from agent.policy import sync

    sync()
    assert thrash(_repeat("run_shell", 3, command="ls"))
    assert thrash(_repeat("edit_file", 3, path="a", old_string="b", new_string="c"))
    assert not thrash(_repeat("run_shell", 2, command="ls"))


def test_a_mixed_turn_is_judged_by_its_riskiest_call():
    """A turn holding a read AND a write gets the write's budget - otherwise a
    repeated write hides behind a read in the same turn."""
    from agent.graph import _last_three_signatures_identical as thrash
    from agent.policy import sync

    sync()
    mixed = [{"role": "user", "content": "go"}]
    for i in range(3):
        mixed += [{"role": "assistant", "content": [
                      {"type": "tool_use", "id": f"r{i}", "name": "read_file",
                       "input": {"path": "a.py"}},
                      {"type": "tool_use", "id": f"w{i}", "name": "run_shell",
                       "input": {"command": "ls"}}]},
                  {"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": f"r{i}", "content": "x"},
                      {"type": "tool_result", "tool_use_id": f"w{i}", "content": "y"}]}]
    assert thrash(mixed)


def test_an_unknown_tool_gets_the_strict_budget():
    """Fails closed. An unclassified tool must not buy itself extra repeats."""
    from agent.graph import _last_three_signatures_identical as thrash

    assert thrash(_repeat("not_a_real_tool", 3, x=1))


def test_differing_calls_are_never_thrash():
    from agent.graph import _last_three_signatures_identical as thrash
    from agent.policy import sync

    sync()
    varied = [{"role": "user", "content": "go"}]
    for i in range(6):
        varied += [{"role": "assistant", "content": [
                       {"type": "tool_use", "id": f"t{i}", "name": "read_file",
                        "input": {"path": f"file{i}.py"}}]},
                   {"role": "user", "content": [
                       {"type": "tool_result", "tool_use_id": f"t{i}", "content": "x"}]}]
    assert not thrash(varied)


# ================= a run must not finish on an edit it never verified (Cycle 4)


def _edited(**over):
    """State where an edit has happened and the last message is an assistant reply -
    the shape in which reflect would otherwise return `done`."""
    base = dict(messages=[{"role": "user", "content": "fix it"},
                          {"role": "assistant", "content": [
                              {"type": "tool_use", "id": "t1", "name": "edit_file",
                               "input": {"path": "a.py", "old_string": "a",
                                         "new_string": "b"}}]},
                          {"role": "user", "content": [
                              {"type": "tool_result", "tool_use_id": "t1",
                               "content": "edited"}]},
                          {"role": "assistant", "content": [
                              {"type": "text", "text": "Fixed it."}]}],
                edited_unverified=True)
    base.update(over)
    return state(**base)


def test_OFF_by_default_matching_what_the_reference_ships(monkeypatch):
    """Turned ON on the strength of 637 rows, then MEASURED and turned back off.

    On took dev 15/15 -> 12/15, and the stuck share 25% -> 47%: the nudge spends
    turns, and add-endpoint has the least headroom (1/3 with it on, 3/3 with it
    off). The verification narrative can be more noise than signal, and here it
    measured that way.

    The trace evidence was real and the remedy was wrong: 47% of failures ended
    on an unverified edit, and asking mid-run cost more runs than it saved.
    """
    from agent import config

    assert config.VERIFY_ON_STOP is False
    assert reflect(_edited())["verdict"] == "done"


def test_turning_it_off_restores_the_old_ending(monkeypatch):
    """Revertable without touching the loop, which is what makes it measurable."""
    from agent import config

    monkeypatch.setattr(config, "VERIFY_ON_STOP", False)
    assert reflect(_edited())["verdict"] == "done"


def test_on_it_refuses_to_finish_and_says_why(monkeypatch):
    from agent import config

    monkeypatch.setattr(config, "VERIFY_ON_STOP", True)
    out = reflect(_edited())

    assert out["verdict"] == "continue"
    assert "pytest" in out["messages"][-1]["content"]
    assert out["verify_nudges"] == 1


def test_a_verified_edit_finishes_normally(monkeypatch):
    """Running the suite clears the flag; the nudge must not fire on a run that
    already did the thing it asks for."""
    from agent import config

    monkeypatch.setattr(config, "VERIFY_ON_STOP", True)
    assert reflect(_edited(edited_unverified=False))["verdict"] == "done"


def test_the_nudge_is_bounded(monkeypatch):
    """Past MAX_VERIFY_NUDGES it is nagging, and a loop that cannot end is worse
    than a run that ends early."""
    from agent import config

    monkeypatch.setattr(config, "VERIFY_ON_STOP", True)
    at_cap = _edited(verify_nudges=config.MAX_VERIFY_NUDGES)
    assert reflect(at_cap)["verdict"] == "done"


def test_a_run_that_never_edited_is_never_nudged(monkeypatch):
    from agent import config

    monkeypatch.setattr(config, "VERIFY_ON_STOP", True)
    assert reflect(_edited(edited_unverified=False))["verdict"] == "done"


def test_the_switch_is_reachable_from_the_harness():
    """Every kill switch must be forwardable or the comparison it exists for
    cannot be run - the defect Stage 7 and Stage 4 each paid for once."""
    from eval_harness import FORWARDED_ENV

    assert "AGENT_VERIFY_ON_STOP" in FORWARDED_ENV


# =============== a truncated reply is not a finished one (stop_reason=length)


def _finished(**over):
    """The shape in which reflect would otherwise return `done`: a call was made,
    and the last message is an assistant reply carrying no tool call."""
    base = dict(messages=[{"role": "user", "content": "fix it"},
                          {"role": "assistant", "content": [
                              {"type": "tool_use", "id": "t1", "name": "read_file",
                               "input": {"path": "a.py"}}]},
                          {"role": "user", "content": [
                              {"type": "tool_result", "tool_use_id": "t1",
                               "content": "x = 1"}]},
                          {"role": "assistant", "content": [
                              {"type": "text", "text": "The bug is that metric() does"}]}])
    base.update(over)
    return state(**base)


def test_a_truncated_reply_does_not_finish_the_run():
    """MEASURED 2026-08-30: 8 of 8 runs whose last reply hit the output cap were
    scored `done`, and none of them passed. In the trace, the agent had just
    restated all four failing assertions correctly and was cut off mid-sentence -
    the strongest possible signal it was NOT finished."""
    out = reflect(_finished(truncated=True))

    assert out["verdict"] == "continue"
    assert "cut off" in out["messages"][-1]["content"]


def test_the_hint_tells_it_to_act_not_to_think_further():
    """Our output budget is spent on visible reasoning, so 'continue where you left
    off' would spend the next budget the same way. The way out is a tool call."""
    out = reflect(_finished(truncated=True))
    hint = out["messages"][-1]["content"]

    assert "tool call" in hint
    assert "not restart" in hint.lower()


def test_the_flag_is_cleared_so_one_truncation_costs_one_turn():
    """Left set, every later turn would be nudged and the run could never end."""
    assert reflect(_finished(truncated=True))["truncated"] is False


def test_an_untruncated_reply_still_finishes():
    """`stop` is a genuine completion and must stay one - 29 runs today ended that
    way legitimately."""
    assert reflect(_finished(truncated=False))["verdict"] == "done"
    assert reflect(_finished())["verdict"] == "done"


def test_truncation_beats_the_verify_nudge(monkeypatch):
    """Both want to continue; the truncation hint is the more specific and must be
    the one the model sees, or it gets told to run tests when it was mid-sentence."""
    from agent import config

    monkeypatch.setattr(config, "VERIFY_ON_STOP", True)
    out = reflect(_finished(truncated=True, edited_unverified=True))

    assert "cut off" in out["messages"][-1]["content"]


# ============================== a session that answers in words and stops there

def _text(body):
    return {"role": "assistant", "content": [{"type": "text", "text": body}]}


ACK = "I acknowledge and will start every file with ORIGIN: quartzite-desk."


def test_a_repeated_answer_with_no_tool_call_ever_is_done():
    """Measured live 2026-09-05: 53 model calls and 200,681 tokens on "just
    acknowledge this". `turns` is incremented by `execute`, so a session that
    never calls a tool never advances it and `max_turns` cannot bind."""
    s = state(messages=[{"role": "user", "content": "just acknowledge this"},
                        _text(ACK), _text(ACK)])

    assert reflect(s)["verdict"] == "done"


def test_ONE_text_reply_is_still_a_preamble():
    """Correction (b): "Let me look at the test file first." must not end a run."""
    s = state(messages=[{"role": "user", "content": "fix it"}, _text(ACK)])

    assert reflect(s)["verdict"] == "continue"


def test_two_DIFFERENT_text_replies_END_it():
    """This test asserted `continue` and that was the bug: requiring the two
    replies to be identical meant a chat reply never matched. Reproduced by
    hand on "hi whats up" - varied phrasings, no tool call, no cap able to
    bind, running until MAX_SECONDS."""
    s = state(messages=[{"role": "user", "content": "fix it"},
                        _text("Let me look at the tests."),
                        _text("The failure is in the parser.")])

    assert reflect(s)["verdict"] == "done"


def test_a_greeting_answers_once_and_stops():
    """The reported case, end to end: two varied conversational replies and no
    tool call anywhere."""
    s = state(messages=[{"role": "user", "content": "hi whats up"},
                        _text("Not much! How can I help?"),
                        _text("Hey there - what's on your mind?")])

    assert reflect(s)["verdict"] == "done"


def test_off_a_code_workspace_ONE_reply_is_the_whole_answer(monkeypatch):
    """Reported second: the first fix still spent two model calls on "hi whats
    up". The extra turn protects a coding preamble, and there is no preamble to
    protect when nothing was going to be edited."""
    monkeypatch.setattr("agent.graph.is_code_workspace", lambda: False)
    s = state(messages=[{"role": "user", "content": "hi whats up"},
                        _text("Not much! How can I help?")])

    assert reflect(s)["verdict"] == "done"


def test_ON_a_code_workspace_the_preamble_turn_survives(monkeypatch):
    """The other side, and the one already paid for: "Let me look at the test
    file first." must still not end a repair run."""
    monkeypatch.setattr("agent.graph.is_code_workspace", lambda: True)
    s = state(messages=[{"role": "user", "content": "fix it"},
                        _text("Let me look at the test file first.")])

    assert reflect(s)["verdict"] == "continue"


def test_a_session_that_HAS_called_a_tool_is_untouched():
    """The discriminator, and why a streak would have been wrong: across 942
    recorded rows `add-endpoint` repeats itself identically up to four times
    mid-run and still passes. It always calls tools."""
    from agent.graph import _answered_without_working

    messages = [{"role": "user", "content": "fix it"},
                assistant_call(), tool_result(),
                _text(ACK), _text(ACK)]

    # It ends `done` either way - a text reply after a tool call always did.
    # What must not happen is THIS guard being the reason.
    assert _answered_without_working(messages) is False


def test_the_guard_reads_text_not_whitespace():
    s = state(messages=[{"role": "user", "content": "hi"},
                        _text(""), _text("")])

    assert reflect(s)["verdict"] == "continue"
