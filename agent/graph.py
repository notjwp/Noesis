"""AgentState, the v1 nodes, and the graph wiring.

Only `act` touches the model. `gate`, `execute`, `reflect` and `finish` are ordinary
code, unit-testable without an API key (NFR-602). That ratio is the most important
design property here, so resist moving logic into the prompt.

Corrections mandated by the build spec, all applied below:
  (a) the entry edge is START -> act; there is no plan node at v1
  (b) `done` is gated on whether a tool call was EVER made, not on a plan cursor
  (c) `failures` is a plain int counting CONSECUTIVE failed turns, reset on success
  (d) the risk map in policy.py is the single path to a verdict
  (e) tracing is present now, not deferred
"""
import datetime
import inspect
import json
import re
import sqlite3
import time
from hashlib import sha256
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RunnableConfig, interrupt

from agent import config as settings
from agent import memory, skills
from agent.context import boundaries, compact_messages, context_chars, shrink
from agent.policy import classify, risk_of
from agent.provider import call_model
from agent import registry
from agent.tools import toolset

SOUL = Path(__file__).resolve().parent.parent / "prompts" / "SOUL.md"
PLAN = Path(__file__).resolve().parent.parent / "prompts" / "PLAN.md"
CODING = Path(__file__).resolve().parent.parent / "prompts" / "CODING.md"
COMPACT_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "COMPACT.md"
STEPS = Path(__file__).resolve().parent.parent / "prompts" / "STEPS.md"

# A numbered or bulleted line from the planner's reply. Deterministic parsing, so
# adopting a plan needs no second model call.
_STEP = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.{3,})$")

# Files that mean "this workspace is source someone expects you to change".
# Deliberately NOT a language list: a .py file proves nothing, a project manifest
# or a test directory proves someone is maintaining something.
CODE_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
                "package.json", "Cargo.toml", "go.mod", "pom.xml",
                "build.gradle", "Gemfile", "composer.json", "Makefile",
                "tests", "test", ".git")


def is_code_workspace() -> bool:
    """Whether to add the coding brief. Deterministic - no model call.

    Two files instead of a profile registry, because there are two postures and
    the brief is injected only in one of them.

    Why this matters here, measured: SOUL.md opened with "You fix broken code"
    and was sent on EVERY task - including the recall cases where the user states
    a deploy key and the authoring cases where a standing rule should be learned.
    Those splits score 46% and 15%; the coding splits score 74-97%.

    ONE level deep only. Walking the tree costs I/O on every turn and a marker
    buried three directories down is somebody's vendored dependency, not the
    workspace's own shape.
    """
    root = settings.WORKSPACE
    try:
        return any((root / marker).exists() for marker in CODE_MARKERS)
    except OSError:
        return False                      # unreadable workspace: stay general


NEWLINES = '\n\n'
DENIAL_TEMPLATE = "Denied by policy: {reason}. Find another approach."
SPILL_MARKER = "[full output: "


class AgentState(TypedDict):
    """Exactly nine fields. No reducers, no Annotated, no operator import.

    Nodes return the FULL messages list, so compaction later is an ordinary return
    rather than a custom merge. Mode and thread_id travel in the graph config, not
    here: they are run-level configuration, not evolving state.
    """
    messages: list[dict]
    turns: int
    max_turns: int
    spent_tokens: int
    spent_seconds: float      # WORKING seconds; see config.MAX_SECONDS
    budget_tokens: int
    failures: int
    verdict: str | None
    approved: list[dict]
    denied: list[dict]
    # Planning (FR-101, FR-105). CE-03 cut `plan` and `cursor` from v1 because
    # nothing read them; both are read now, by act and by reflect.
    phase: str                # "planning" until the plan is adopted, then "working"
    plan: list[str]
    cursor: int
    plan_turns: int           # counted apart from `turns`; see config.PLAN_MAX_TURNS
    compact_count: int        # compactions so far; capped by config.MAX_COMPACTIONS
    edited_unverified: bool   # a write happened with no test since
    verify_nudges: int        # bounded by MAX_VERIFY_NUDGES
    truncated: bool           # last reply hit the output cap (stop_reason=length)
    summarised: bool          # the turn cap has already asked for a wrap-up


def new_state(goal: str, max_turns: int | None = None,
              budget_tokens: int | None = None) -> AgentState:
    """A fresh run. Two callers - the CLI and the eval harness - which is what
    earns this a home here rather than being duplicated in both."""
    return {
        "messages": [{"role": "user", "content": goal}],
        "turns": 0,
        "max_turns": max_turns if max_turns is not None else settings.MAX_TURNS,
        "spent_tokens": 0,
        "spent_seconds": 0.0,
        "budget_tokens": budget_tokens if budget_tokens is not None else settings.BUDGET_TOKENS,
        "failures": 0,
        "verdict": None,
        "approved": [],
        "denied": [],
        "phase": "planning" if settings.PLAN_ENABLED else "working",
        "plan": [],
        "cursor": 0,
        "plan_turns": 0,
        "compact_count": 0,
    }


def continue_state(prior: AgentState, message: str) -> dict:
    """A follow-up turn in a thread that already terminated (FR-701, "chat").

    Returns a PARTIAL state on purpose. Nodes overwrite per key, so every field
    left out - spent_tokens, budget_tokens, max_turns - keeps its checkpointed
    value, and that omission is the safety property: spend stays CUMULATIVE, so
    the budget still binds across a long conversation.

    The turn counter is the one thing that must reset. reflect (b) fires on
    `turns >= max_turns`, so a second message into a thread that used its whole
    allowance would return `stuck` before the model was called even once.

    Lives here rather than in the interface layer because the nine fields are
    this module's business - the same reason new_state() is here.
    """
    return {
        "messages": prior["messages"] + [{"role": "user", "content": message}],
        "turns": 0,
        "failures": 0,
        "verdict": None,
        # A follow-up is a new goal, so it is planned like one (UR-02). The old
        # plan is dropped rather than resumed: its steps described the previous
        # request and would be injected into every turn of this one.
        "phase": "planning" if settings.PLAN_ENABLED else "working",
        "plan": [],
        "cursor": 0,
        "plan_turns": 0,
    }


# --------------------------------------------------------------------- nodes

def act(state: AgentState, config: RunnableConfig) -> dict:
    """The only node that touches a model.

    Which provider answers lives in agent/provider.py; this node keeps the trace
    bookkeeping and the state update, and stays ignorant of who replied.
    """
    cfg = config.get("configurable", {})
    system = SOUL.read_text(encoding="utf-8")   # CE-05: read here, not at import

    # The posture. A personal agent is not a coding agent that also remembers
    # things - the coding brief is added only where it applies, so a "what did I
    # tell you about the deploy key" turn is not told to run pytest.
    if is_code_workspace():
        coding = CODING.read_text(encoding="utf-8")
        system = system + '\n\n' + coding
        trace = cfg.get("trace")
        if trace is not None:
            trace.append({"kind": "posture", "name": "coding"})

    # Section 4.1 step 3 is explicit that this goes in the SYSTEM PROMPT and not
    # into the message list, which "pollutes history and creates gaps" - the same
    # placement memory and skills already use.
    if state.get("phase") == "planning":
        # Research only - the plan itself is written by the `plan` node, which gets a
        # message list of its own.
        system = f"{system}\n\n{PLAN.read_text(encoding='utf-8')}"
    elif state.get("plan"):
        plan, cursor = state["plan"], min(state.get("cursor", 0), len(state["plan"]) - 1)
        # The WHOLE plan, not just the current step: ~70 tokens, and without it the
        # agent re-derives the shape of the work every turn.
        listing = "\n".join(
            f"{'->' if i == cursor else '  '} {i + 1}. {step}"
            for i, step in enumerate(plan))
        system = (f"{system}\n\n## Your plan\n\n{listing}\n\n"
                  f"You are on step {cursor + 1} of {len(plan)}. Do that step.")
        trace = cfg.get("trace")
        if trace is not None:
            trace.append({"kind": "step", "cursor": cursor, "of": len(plan),
                          "text": plan[cursor]})

    # The agent has no clock. Asked what happened "yesterday" it answered about
    # its training cutoff - thirteen months out - searched five times for the
    # wrong year and fetched that year's archive. One line, because this is
    # charged on every call and the provider caches nothing.
    system = f"{system}{NEWLINES}Today is {_today()}."

    # Retrieved memory goes in the SYSTEM PROMPT, not the message list - a fake
    # turn in the history is indistinguishable from something the agent did.
    recalled = memory.context_for(_goal(state["messages"]))
    if recalled:
        system = f"{system}\n\n{recalled}"
        trace = cfg.get("trace")
        if trace is not None:
            trace.append({"kind": "memory", "chars": len(recalled)})

    # Level 1 of progressive disclosure: names and descriptions only. The bodies
    # cost nothing until the agent calls load_skill, which is the whole point on a
    # provider that re-sends and re-charges the prompt every single turn.
    catalogue = skills.index()
    if catalogue:
        system = system + NEWLINES + catalogue
        trace = cfg.get("trace")
        if trace is not None:
            trace.append({"kind": "skills", "chars": len(catalogue)})

    # Level 1 offers; this OPENS. Measured on 22 authoring runs: the agent called
    # load_skill in 59% of the runs that had a skill in the index, and those passed
    # 12 of 13 against 1 of 9 when it did not. The index was never the problem -
    # electing to act on it was. Same correction as `learn`, which asked and was
    # called 0 times in 15 sessions.
    opened = skills.opening(_goal(state["messages"]))
    if opened:
        system = system + NEWLINES + opened
        trace = cfg.get("trace")
        if trace is not None:
            trace.append({"kind": "skill_opened", "chars": len(opened),
                          "name": skills.matched(_goal(state["messages"]))})

    # Rebuilt per turn rather than bound at import: which tools exist depends on
    # what activated for THIS run, and CE-05 forbids deciding that at import.
    started = time.monotonic()
    reply = call_model(state["messages"], system, registry.schemas(),
                       cfg.get("on_text"))
    elapsed = time.monotonic() - started

    trace = cfg.get("trace")
    if trace is not None:
        trace.append({
            "kind": "model",
            # Recorded per turn: a score is meaningless without knowing which model
            # produced it, and this project expects to switch providers.
            "provider": settings.PROVIDER,
            "billed_tokens": reply.billed_tokens,
            # NFR-102 excludes model time from framework cost, so the exclusion
            # has to be on the record rather than estimated.
            "ms": round(elapsed * 1000, 3),
            "cache_read_tokens": reply.cache_read_tokens,
            "stop_reason": reply.stop_reason,
            # NFR-101 is a p50 over these. None when nothing streamed, which is
            # the honest answer rather than a zero that would flatter the median.
            "first_token_s": reply.first_token_s,
        })

    # The model's own prose is capped like a tool result: one 52,866-char reply
    # became 73% of the context in a turn and compaction could not clear it.
    # Same cap for the same reason; derivation in eval/CHANGELOG.md.
    blocks = [dict(b, text=shrink("model_reply", b["text"]))
              if b.get("type") == "text" and isinstance(b.get("text"), str) else b
              for b in reply.blocks]

    return {
        "messages": state["messages"] + [{"role": "assistant", "content": blocks}],
        "truncated": reply.stop_reason == "length",
        "spent_tokens": state["spent_tokens"] + reply.billed_tokens,
        # NFR-304. Accumulated by the nodes that actually spend time, so a thread
        # resumed tomorrow is not instantly over its cap because the calendar moved.
        "spent_seconds": state.get("spent_seconds", 0.0) + elapsed,
    }


def gate(state: AgentState, config: RunnableConfig) -> dict:
    """Classify every call before any side effect.

    NO SIDE EFFECTS. This node suspends on interrupt() and re-executes from its
    first line on resume, rebuilding approved/denied from scratch. No logging, no
    counters, no writes. This is why gate and execute are separate nodes: merged,
    every already-executed tool would fire again on every resume.
    """
    autonomous = config.get("configurable", {}).get("autonomous", True)
    # While planning the agent may look but not touch (UR-02). Enforced in the
    # gate rather than asked for in the prompt, because an approval shown after
    # the files have already changed is theatre.
    planning = state.get("phase") == "planning"
    approved, denied = [], []

    for call in _tool_calls(state["messages"][-1]):
        verdict, reason = classify(call["name"], call["input"], autonomous, planning)
        if verdict == "auto":
            approved.append(call)
        elif verdict == "deny":
            denied.append({**call, "reason": reason})
        else:
            decision = interrupt({"call": call, "reason": reason})
            # FR-307. An amendment arrives as DATA through the interrupt and is
            # RE-CLASSIFIED, never waved through: editing a path to escape the
            # workspace must still be denied, or the prompt becomes a bypass.
            if isinstance(decision, dict) and decision.get("decision") == "amend":
                call = {**call, "input": {**call["input"], **(decision.get("input") or {})}}
                verdict, reason = classify(call["name"], call["input"],
                                           autonomous, planning)
                if verdict == "deny":
                    denied.append({**call, "reason": reason})
                    continue
                approved.append(call)
            elif decision == "allow":
                approved.append(call)
            else:
                denied.append({**call, "reason": "rejected by user"})

    return {"approved": approved, "denied": denied}


# Warn BEFORE killing. reflect ends a run silently at REPEAT_LIMIT; the model
# is never told it is looping and cannot correct. Warn on the 2nd identical
# call and block later - the half Cycle D left behind.
WARN_AFTER = 2


def _repeat_notice(messages: list, call: dict, raw: str) -> str:
    """A nudge when this exact call has already returned this exact result.

    Keys on the RESULT as well as the arguments. Hashing arguments alone
    cannot tell a pointless re-read from a legitimate one after an edit -
    the file changed, so the result changed, so it is not a repeat.
    """
    signature = _signature(call)
    digest = sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    seen = 1
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for earlier in _tool_calls(message):
            if _signature(earlier) == signature:
                seen += 1
    if seen < WARN_AFTER or digest not in _RESULTS.get(signature, ()):
        _RESULTS.setdefault(signature, set()).add(digest)
        return ""
    return (
        f"\n\n[This exact {call["name"]} call has now returned the same result "
        f"{seen} times. Use the result you already have - repeating it "
        f"unchanged will end the run.]")


# Per-process, and that is deliberate: a thread resumed tomorrow starts fresh
# rather than inheriting a warning about a call it can no longer see.
_RESULTS: dict = {}


def execute(state: AgentState, config: RunnableConfig) -> dict:
    """Run approved calls, then observe. CE-04 merges the two stages into one node
    because no edge ever separates them."""
    trace = config.get("configurable", {}).get("trace")
    results, failed = [], 0
    node_started = time.monotonic()

    for call in state["approved"]:
        started = time.monotonic()
        try:
            raw = str(toolset()[call["name"]]["fn"](**call["input"]))
            is_error = False
        except Exception as exc:                    # FR-208: never propagates out
            raw = f"{type(exc).__name__}: {exc}"
            is_error = True
        if is_error:
            failed += 1
        body = shrink(call["name"], raw)            # FR-401/402
        body += _repeat_notice(state["messages"], call, raw)
        results.append({"type": "tool_result", "tool_use_id": call["id"],
                        "content": body, "is_error": is_error})
        _log(trace, call, "auto", time.monotonic() - started, raw, body, is_error)

    for call in state["denied"]:
        results.append({"type": "tool_result", "tool_use_id": call["id"],
                        "content": DENIAL_TEMPLATE.format(reason=call["reason"]),
                        "is_error": True})
        _log(trace, call, "deny", 0.0, "", "", True)  # a denial is still a tool call
        failed += 1

    # Research turns are counted SEPARATELY and are not charged against max_turns;
    # a shared counter would starve the cases planning exists to help.
    planning = state.get("phase") == "planning"
    # A write makes the run unverified; running the suite clears it. Only the
    # calls that SUCCEEDED count - a failed edit changed nothing.
    edited = state.get("edited_unverified", False)
    for call in state["approved"]:
        if call["name"] in ("edit_file", "write_file"):
            edited = True
        elif call["name"] == "run_shell" and "pytest" in str(
                call["input"].get("command", "")):
            edited = False
    return {
        "edited_unverified": edited,
        "messages": state["messages"] + [{"role": "user", "content": results}],
        "turns": state["turns"] + (0 if planning else 1),
        "plan_turns": state.get("plan_turns", 0) + (1 if planning else 0),
        "spent_seconds": state.get("spent_seconds", 0.0)
        + (time.monotonic() - node_started),
        # correction (c): overwrite semantics, reset to 0 when every result succeeded
        "failures": 0 if failed == 0 else state["failures"] + 1,
    }


def reflect(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """Deterministic only. Checks run in a fixed order; the first match wins.

    `config` is optional and used for ONE thing: recording context size. _timed()
    inspects the signature and passes it when present, and every direct caller in
    the tests passes state alone, so the default keeps both working.
    """
    # NFR-401's HARD stop. The old check fired at 60% and terminated, which read
    # as a budget stop while actually being a compaction trigger.
    if state["spent_tokens"] >= state["budget_tokens"]:
        return {"verdict": "budget"}                                    # (a0)

    # FR-403, on CONTEXT SIZE rather than cumulative spend. See the derivation in
    # config.COMPACT_AT_CHARS: spent_tokens never decreases, so using it here
    # would fire on every turn once crossed.
    size = context_chars(state["messages"])

    # Recorded on EVERY turn, not only when compaction fires: `before`/`after` live
    # on the compact entry, so a run that never compacts would leave no evidence of
    # how close it came - and 44,000 vs 12,000 call for opposite actions.
    trace = (config or {}).get("configurable", {}).get("trace")
    if trace is not None:
        trace.append({"kind": "context", "chars": size})

    if size > settings.COMPACT_AT_CHARS:
        if state.get("compact_count", 0) < settings.MAX_COMPACTIONS:
            return {"verdict": "compact"}                               # (a)
        # Compacted the maximum number of times and still over. Stop rather than
        # loop: the loop costs a model call per turn and clears nothing.
        return {"verdict": "stuck"}                                     # (a1)
    # NFR-304's third cap, checked before the phase branch so it binds during
    # research too. Terminates as `stuck` - running out of time is a way of being
    # stuck, not a fifth verdict.
    if state.get("spent_seconds", 0.0) >= settings.MAX_SECONDS:
        return {"verdict": "stuck"}                                     # (a2)

    if state.get("phase") == "planning":
        # A reply carrying no tool call IS the plan - the planner saying it has
        # finished researching.
        if state["messages"][-1]["role"] == "assistant":
            return {"verdict": "planned"}
        # Strictly GREATER than the cap, so reaching it buys one more act turn -
        # the one act spends demanding the plan. This is the hard stop for a
        # planner that ignores that demand and keeps calling tools.
        if state.get("plan_turns", 0) > settings.PLAN_MAX_TURNS:
            return {"verdict": "planned"}
        return {"verdict": "continue"}

    if state["turns"] >= state["max_turns"]:
        # One last turn to say what it found, then stop, rather than ending
        # silently.
        #
        # THE VERDICT STAYS `stuck`, and that is deliberate. A summary does not
        # change the workspace and the check command reads the workspace, so this
        # cannot turn a failure into a pass - letting it end `done` would be the
        # truncated-reply defect again. What it buys is a run that reports what it
        # learned: `answer` feeds write_episode and NOW.md, and a capped run
        # currently records nothing.
        if not state.get("summarised"):
            return {
                "verdict": "continue",
                "summarised": True,
                "messages": state["messages"] + [
                    {"role": "user", "content": CAP_SUMMARY_REQUEST}],
            }
        return {"verdict": "stuck"}                                     # (b)
    if _last_three_signatures_identical(state["messages"]):
        return {"verdict": "stuck"}                                     # (c)
    # FR-104 names exactly four terminal outcomes and `replan` is not one; it
    # fired once in 712 rows, and `failures` still distinguishes the cause.
    if state["failures"] >= 3:
        return {"verdict": "stuck"}                                     # (d)
    # The wrap-up reply arrives AFTER the cap, so (b) above would end the run
    # before it is recorded. Ending here keeps the verdict `stuck` while letting
    # `finish` write the summary the model just produced.
    if state.get("summarised") and state["messages"][-1]["role"] == "assistant":
        return {"verdict": "stuck"}

    if state["messages"][-1]["role"] == "assistant":                    # (e)
        # §9 step 2(b): `done` is gated on whether any tool call was ever made, not on
        # a cursor - a plan with an unfinished cursor must still be able to end.
        if not _made_a_call(state["messages"]):
            # An answer given in words is still an answer. `pass` is decided by
            # the check command reading the workspace, so ending here cannot
            # turn a failing case into a passing one.
            #
            # The extra turn is the CODING posture's. "Let me look at the test
            # file first." must not end a repair run, and that was paid for on
            # code cases. Off a code workspace the first reply IS the answer,
            # and spending a second model call on "hi whats up" is waste the
            # same lesson never asked for.
            if _answered_without_working(state["messages"],
                                         once=not is_code_workspace()):
                return {"verdict": "done"}
            return {"verdict": "continue"}
        plan = state.get("plan") or []
        if state.get("cursor", 0) + 1 < len(plan):
            return {"verdict": "continue", "cursor": state["cursor"] + 1}
        # A TRUNCATED REPLY IS NOT A FINISHED ONE. `length` means the model ran
        # out of output budget mid-sentence - the strongest signal it is NOT
        # done. Measured 2026-08-30: 8 of 8 runs ending on `length` were scored
        # `done`, and none passed.
        if state.get("truncated"):
            return {
                "verdict": "continue",
                "truncated": False,
                # REFUNDED. The reply was cut off mid-sentence, so this turn
                # bought no completed thought - charging for it spends the cap on
                # the output limit rather than on work. A retry that threw a turn
                # away refunds it. Never below zero.
                "turns": max(0, state["turns"] - 1),
                "messages": state["messages"] + [
                    {"role": "user", "content": TRUNCATED_HINT}],
            }
        nudge = _verify_nudge(state)
        if nudge is not None:
            return nudge
        return {"verdict": "done"}
    return {"verdict": "continue"}                                      # (f)


# A run that edits and then stops without running the tests has not finished,
# it has narrated. Inject a message and continue rather than ending - bounded,
# and only when AGENT_VERIFY_ON_STOP is on.
VERIFY_HINT = (
    "[You edited a file but have not run the tests since. Run them now - "
    "`run_shell(command='pytest -q')` - and fix what fails. If you cannot "
    "verify, say what is blocking you rather than stopping here.]")


# Our budget is spent on visible reasoning, so the way to finish is to call a
# tool rather than think further.
# Asked once when the turn cap is reached. Careful to forbid further tools - a
# request for a summary that invites another tool call just spends the turn it
# was given.
CAP_SUMMARY_REQUEST = (
    "[You have reached the maximum number of tool-calling turns. Give a final "
    "answer summarising what you found and what you changed, and what is still "
    "wrong. Do not call any more tools.]")


TRUNCATED_HINT = (
    "[System: your previous reply was cut off by the output length limit. "
    "Do not restart or repeat it. Stop explaining and make the next tool call "
    "now - edit the file you were reasoning about, or run the tests.]")


def _verify_nudge(state: AgentState):
    """A state update that keeps the run going, or None to let it finish."""
    if not settings.VERIFY_ON_STOP or not state.get("edited_unverified"):
        return None
    if state.get("verify_nudges", 0) >= settings.MAX_VERIFY_NUDGES:
        return None                       # bounded: past this it is nagging
    return {
        "verdict": "continue",
        "verify_nudges": state.get("verify_nudges", 0) + 1,
        "messages": state["messages"] + [
            {"role": "user", "content": VERIFY_HINT}],
    }


def compact(state: AgentState, config: RunnableConfig) -> dict:
    """Summarise the middle of the history and carry on (FR-403, FR-404).

    The THIRD node that touches a model, which is exactly what §3 budgets for
    (plan, act, compact-summarisation). Nothing else about the determinism ratio
    changes: the boundary arithmetic and the rewrite are pure functions in
    context.py, and only the summary itself needs a model.
    """
    cfg = config.get("configurable", {})
    before = context_chars(state["messages"])
    head_end, tail_start = boundaries(state["messages"])
    removed = state["messages"][head_end:tail_start]

    try:
        reply = call_model(
            [{"role": "user", "content": json.dumps(removed, default=str)}],
            COMPACT_PROMPT.read_text(encoding="utf-8"), [], None)
        summary = " ".join(b.get("text", "") for b in reply.blocks
                           if b.get("type") == "text").strip()
        billed = reply.billed_tokens
        # A summary larger than what it replaces is not one. Measured: 49,496
        # chars for 24 messages, and the run compacted twice more and died.
        if len(summary) > settings.COMPACT_SUMMARY_MAX_CHARS:
            summary = (summary[:settings.COMPACT_SUMMARY_MAX_CHARS].rstrip()
                       + " ...[summary truncated so compaction can shrink]")
    except Exception as exc:                       # noqa: BLE001
        # A failed summariser must not lose the run. It is already in trouble -
        # that is why it is compacting - and dying in the recovery is worse than
        # losing the detail.
        summary = (f"unavailable ({type(exc).__name__}) - {len(removed)} messages "
                   f"of tool calls were removed to save context")
        billed = 0

    messages = compact_messages(state["messages"], summary or "no summary produced")
    after = context_chars(messages)

    trace = cfg.get("trace")
    if trace is not None:
        # NFR-403 wants >= 50% when it fires, so the reduction is recorded rather
        # than asserted in a comment.
        trace.append({"kind": "compact", "before": before, "after": after,
                      "removed_messages": len(removed),
                      "removed_pct": round((1 - after / before) * 100, 1)})

    return {"messages": messages,
            "compact_count": state.get("compact_count", 0) + 1,
            "spent_tokens": state["spent_tokens"] + billed,
            "verdict": None}


def _digest(messages: list[dict]) -> str:
    """What the research turns found, as facts rather than as a conversation.

    Built from `_outcomes()`, which already pairs every tool call with whether it
    succeeded - so this needs no model call of its own and stays deterministic.

    It is deliberately NOT the message list. Handing the plan node a transcript
    would hand it the tool-call history, and that history is the entire reason
    two earlier cycles never produced a plan.
    """
    outcomes = _outcomes(messages)
    read = sorted({c["input"]["path"] for c, ok in outcomes
                   if ok and c["name"] in ("read_file", "search_files")
                   and isinstance(c["input"].get("path"), str)})
    worked = [c["input"]["command"] for c, ok in outcomes
              if ok and c["name"] == "run_shell"
              and isinstance(c["input"].get("command"), str)]
    failed = [f"{c['name']}({str(c['input'])[:60]})" for c, ok in outcomes if not ok]

    lines = []
    if read:
        lines.append("  read: " + ", ".join(read[:12]))
    if worked:
        lines.append("  ran (worked): " + "; ".join(worked[-6:]))
    if failed:
        lines.append("  did not work: " + "; ".join(failed[-4:]))
    return ("Already gathered:\n" + "\n".join(lines)) if lines else ""


def plan(state: AgentState, config: RunnableConfig) -> dict:
    """Write the plan, with a message list of its OWN (FR-101).

    THE SECOND model-calling node, and §3 drew it for exactly this reason. Nine
    scored runs across three cycles tried to extract a plan from the research
    conversation and never once got one: on this provider a tool-call history
    keeps producing tool calls whether or not a tool is offered - proven with
    `tools` absent from the payload entirely. Neither an instruction nor an
    absence stops it. A FRESH message list does; that was measured before this
    was written, and the reply came back finish_reason `stop` with three
    parseable steps.

    Separate from `adopt` on purpose, and CE-04 does not apply: `adopt` suspends
    on interrupt() and re-runs from its first line on resume. Merged, every
    resumed approval would spend another model call - which is CE-07's rule
    applied to the node that actually calls a model.
    """
    goal = _goal(state["messages"])
    digest = _digest(state["messages"])
    # Read OUTSIDE the try. A missing prompt is a deploy error, not a provider
    # one; swallowed, it looked exactly like the bug this node was built to fix.
    instruction = STEPS.read_text(encoding="utf-8")
    try:
        reply = call_model(
            [{"role": "user", "content": f"{goal}\n\n{digest}".strip()}],
            instruction, [], None)
        text = " ".join(b.get("text", "") for b in reply.blocks
                        if b.get("type") == "text")
        billed = reply.billed_tokens
    except Exception:                              # noqa: BLE001 - provider only
        text, billed = "", 0

    steps = _steps(text) or [goal]
    trace = config.get("configurable", {}).get("trace")
    if trace is not None:
        trace.append({"kind": "plan", "steps": steps, "chars": len(digest),
                      "fell_back": steps == [goal]})
    return {"plan": steps,
            "spent_tokens": state["spent_tokens"] + billed,
            "verdict": None}


def adopt(state: AgentState, config: RunnableConfig) -> dict:
    """Put the plan in front of a human (UR-02, UR-05).

    NO SIDE EFFECTS, and this is the node the rule was written for: it suspends
    on interrupt() and re-executes from its first line on resume. It asks, and
    nothing else. The model call that produced the plan lives in `plan` above
    precisely so a resumed approval does not pay for it twice.
    """
    steps = state.get("plan") or [_goal(state["messages"])]

    if config.get("configurable", {}).get("autonomous", True):
        return {"phase": "working", "plan": steps, "cursor": 0}

    decision = interrupt({"plan": steps, "reason": "plan ready for review"})
    if decision == "accept":
        return {"phase": "working", "plan": steps, "cursor": 0}

    # Anything else revises. The note goes back as an ordinary user message and
    # the phase stays `planning`, so the loop re-enters act and plans again -
    # which is also why a dismissed modal must resolve to revise, not accept.
    note = decision if isinstance(decision, str) and decision not in ("revise", "") \
        else "Revise the plan."
    return {"messages": state["messages"] + [{"role": "user", "content": note}],
            "phase": "planning", "plan": [], "cursor": 0}


def _steps(text: str) -> list[str]:
    """Numbered or bulleted lines from the planner's reply, in order.

    Truncated at PLAN_MAX_STEPS rather than refused: a planner that
    over-decomposes must not fail the run. `adopt` supplies the goal itself when
    this returns nothing, for the same reason.
    """
    found = [match.group(1).strip()
             for match in (_STEP.match(line) for line in text.splitlines())
             if match]
    return found[:settings.PLAN_MAX_STEPS]


def finish(state: AgentState, config: RunnableConfig) -> dict:
    """Terminal. Records the outcome, and writes the episode memory will recall.

    Still a DETERMINISTIC node: everything written is already in `messages`, so no
    model call is needed. A model-written summary was considered and rejected on
    exactly that ground - it would make a fourth node that touches a model, and the
    ratio of deterministic to model-driven nodes is the most important design
    property in this system.
    """
    cfg = config.get("configurable", {})
    trace = cfg.get("trace")
    if trace is not None:
        trace.append({"kind": "terminal", "verdict": state["verdict"],
                      "turns": state["turns"], "spent_tokens": state["spent_tokens"]})

    if settings.MEMORY_ENABLED:
        outcomes = _outcomes(state["messages"])
        memory.write_episode(
            thread_id=str(cfg.get("thread_id", "")),
            goal=_goal(state["messages"]),
            verdict=state["verdict"],
            answer=_final_text(state["messages"]),
            # §4.3's list: files touched and commands that WORKED. Failed calls are
            # excluded deliberately - recalling a command that did not work is worse
            # than recalling nothing, because it reads as advice.
            files=sorted({c["input"]["path"] for c, ok in outcomes
                          if ok and isinstance(c["input"].get("path"), str)}),
            commands=[c["input"]["command"] for c, ok in outcomes
                      if ok and c["name"] == "run_shell"
                      and isinstance(c["input"].get("command"), str)])
        memory.write_now(
            goal=_goal(state["messages"]),
            verdict=state["verdict"],
            plan=state.get("plan") or [],
            cursor=state.get("cursor", 0),
            files=sorted({c["input"]["path"] for c, ok in outcomes
                          if ok and isinstance(c["input"].get("path"), str)}))

        # Same rule, applied to the user model: `remember` is a tool and
        # AGENT.md was empty on the one machine this actually runs on.
        memory.distil(state["messages"], skip=CAP_SUMMARY_REQUEST)

    # Phase R: a skill that was open when the run FAILED is suspect, and the next
    # run that succeeds replaces it. Recorded here rather than judged - `act` is
    # the only node that may call a model, so this node states a fact and
    # `skills.extract` consumes it by a rule.
    opened = skills.matched(_goal(state["messages"]))
    if (settings.SKILL_REVISION and opened
            and (state["verdict"] in ("stuck", "budget")
                 or state.get("failures", 0) >= 3)):
        memory.mark_suspect(opened, goal=_goal(state["messages"]),
                            verdict=str(state["verdict"]))

    # Phase O-redux: knowledge is retained WITHOUT the agent electing to record
    # it. Deterministic injection went 0/18 to 15/18; the `learn` tool went 0/15.
    for name in skills.extract(state["messages"], _goal(state["messages"]),
                               verdict=str(state["verdict"]), opened=opened):
        if trace is not None:
            trace.append({"kind": "skill", "name": name})
    return {}


# ------------------------------------------------------------------- helpers

def _tool_calls(message: dict) -> list[dict]:
    """tool_use blocks of an assistant message, normalised to {id, name, input}."""
    if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
        return []
    return [{"id": b["id"], "name": b["name"], "input": b.get("input", {})}
            for b in message["content"]
            if isinstance(b, dict) and b.get("type") == "tool_use"]


def _today() -> str:
    """Today, ISO, read per turn.

    A constant bound at import is wrong for every process that outlives a day -
    the worker, a cron task, a TUI left open overnight - and CE-05 forbids the
    library resolving it at import anyway.
    """
    return datetime.date.today().isoformat()


def _goal(messages: list[dict]) -> str:
    """The user's opening message - which for this agent IS what the user said."""
    first = messages[0].get("content") if messages else ""
    return first if isinstance(first, str) else json.dumps(first, default=str)


def _final_text(messages: list[dict]) -> str:
    """The last thing the agent said in words, not tool calls."""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        texts = [b.get("text", "") for b in content or []
                 if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t).strip()
        if joined:
            return joined
    return ""


def _outcomes(messages: list[dict]) -> list[tuple[dict, bool]]:
    """Every tool call paired with whether its result succeeded.

    Derived from `messages` rather than the trace: the trace is optional - the CLI
    may not pass one - and memory must not silently record less depending on who
    invoked the graph.
    """
    errors = {b["tool_use_id"]: bool(b.get("is_error"))
              for m in messages if isinstance(m.get("content"), list)
              for b in m["content"]
              if isinstance(b, dict) and b.get("type") == "tool_result"}
    return [(call, not errors.get(call["id"], False))
            for m in messages for call in _tool_calls(m)]


def _made_a_call(messages: list[dict]) -> bool:
    """Correction (b), the termination guard. With no plan node a cursor check
    evaluates 1 >= 0 and returns `done` on the first text-only reply - including
    "Let me look at the test file first." Restore the cursor check only when the
    plan node lands.
    """
    return any(_tool_calls(m) for m in messages if m.get("role") == "assistant")


def _answered_without_working(messages: list[dict], once: bool = False) -> bool:
    """Whether a session with NO tool call has replied enough times to stop.

    `once` is the non-coding posture: one reply ends it, because there is no
    preamble to protect - nothing was going to be edited.

    `turns` is incremented by `execute`, so a session that never reaches it
    never advances and `max_turns` cannot bind. The thrash detector misses the
    same case because it reads tool-call signatures. Measured live: 53 model
    calls, 200,681 tokens, `turns` 0, on "just acknowledge this".

    COUNTS replies rather than comparing them. The first version required the
    last two to be byte-identical, which a chat reply never is - "hi whats up"
    looped on varied phrasings until MAX_SECONDS, because nothing else here can
    bind. Repetition was the symptom; answering without working is the state.

    Keyed on NO CALL EVER, never on a streak: across 942 recorded rows,
    `add-endpoint` repeats itself identically up to four times mid-run and
    still passes - and it always calls tools, so it never reaches this.
    """
    if _made_a_call(messages):
        return False
    said = [_final_text([m]) for m in messages if m.get("role") == "assistant"]
    return len([s for s in said if s]) >= (1 if once else 2)


def _signature(call: dict) -> str:
    """Stable identity: tool name plus a hash of canonically ordered arguments."""
    blob = json.dumps(call["input"], sort_keys=True, default=str).encode("utf-8")
    return f"{call['name']}:{sha256(blob).hexdigest()[:12]}"


# A READ is idempotent, so repeating one is confusion, not a loop; a repeated
# WRITE or command is the harmful signal. Budgets differ per risk; the trace that
# produced these numbers is in eval/CHANGELOG.md.
REPEAT_LIMIT = {"read": 5, "write": 3, "destructive": 3}


def _last_three_signatures_identical(messages: list[dict]) -> bool:
    """Whether the agent is repeating itself past the limit for what it repeated."""
    from agent.policy import risk_of

    turns = [[_signature(c) for c in _tool_calls(m)]
             for m in messages if m.get("role") == "assistant"]
    names = [[c["name"] for c in _tool_calls(m)]
             for m in messages if m.get("role") == "assistant"]
    names = [n for n, t in zip(names, turns) if t]
    turns = [t for t in turns if t]
    if len(turns) < 3:
        return False

    repeats = 1
    for earlier in reversed(turns[:-1]):
        if earlier != turns[-1]:
            break
        repeats += 1

    # The turn's own risk decides its budget, and the RISKIEST call in it wins:
    # a turn mixing a read with a write is judged as a write.
    risks = {risk_of(name) or "destructive" for name in names[-1]}
    limit = min(REPEAT_LIMIT.get(r, 3) for r in risks) if risks else 3
    return repeats >= limit


def _summarise(args: dict) -> str:
    """The most identifying argument, truncated.

    A bounded summary, not the arguments themselves: write_file carries whole file
    contents, and putting those in every trace event would bloat the traces the
    tuning phase has to read. Serves the CLI's live display and the "edited a file
    it never read" check equally.
    """
    for key in ("name", "command", "path", "file"):
        if key in args:
            return str(args[key])[:120]
    return json.dumps(args, default=str)[:120]


def _log(trace, call, verdict, duration, raw, shrunk, is_error) -> None:
    """One structured record per tool call: name, argument hash, verdict, duration,
    byte counts, spill path. Lives in execute, never in gate - logging there would
    fire twice on resume.
    """
    if trace is None:
        return
    spill = ""
    if SPILL_MARKER in shrunk:
        spill = shrunk.split(SPILL_MARKER, 1)[1].split("]", 1)[0]
    trace.append({
        "kind": "tool",
        "tool": call["name"],
        "summary": _summarise(call["input"]),
        "argument_hash": _signature(call).split(":", 1)[1],
        "verdict": verdict,
        # WHY it was refused, not just that it was. The trace has always said a
        # call was denied and never on what grounds, which made a policy refusal
        # indistinguishable from a path escape when reading a run back.
        "reason": call.get("reason", ""),
        "duration_ms": round(duration * 1000),
        "input_bytes": len(json.dumps(call["input"], default=str)),
        "output_bytes": len(raw),
        "shrunk_bytes": len(shrunk),
        "spill_path": spill,
        "is_error": is_error,
    })


# ------------------------------------------------------------------- routing

def _route_after_act(state: AgentState) -> str:
    """tool_use blocks -> gate; text only -> reflect (step complete, NOT
    termination - reflect decides that)."""
    return "gate" if _tool_calls(state["messages"][-1]) else "reflect"


def _route_after_reflect(state: AgentState) -> str:
    # compact and replan still have no node and terminate here. `replan` fired
    # ONCE in 712 recorded rows, which is why the plan layer was not built on it.
    if state["verdict"] == "planned":
        return "plan"
    # FR-403: compaction is no longer where a run goes to die. It summarises and
    # returns to act, which is the whole point of building it.
    if state["verdict"] == "compact":
        return "compact"
    return "act" if state["verdict"] == "continue" else "finish"


def _timed(name: str, fn):
    """Wrap a node so it reports its own wall time (NFR-102).

    Applied HERE rather than inside each node, because _build() is the only
    function that knows all six and a node should not carry stopwatch code. The
    nodes themselves are untouched.

    NFR-102 bounds "framework cost per loop iteration, EXCLUDING model and tool
    time". Both exclusions are already on the trace - `act` records the model's
    ms and `execute` records each call's duration_ms - so the subtraction is
    arithmetic over what this adds, not a second measurement.
    """
    takes_config = len(inspect.signature(fn).parameters) > 1

    def node(state, config=None):
        started = time.monotonic()
        out = fn(state, config) if takes_config else fn(state)
        trace = (config or {}).get("configurable", {}).get("trace")
        if trace is not None:
            trace.append({"kind": "node", "node": name,
                          "ms": round((time.monotonic() - started) * 1000, 3)})
        return out

    return node


class _TimedSaver(SqliteSaver):
    """SqliteSaver that reports how long each checkpoint write took (NFR-103).

    The write is timed where it happens rather than around invoke(), because
    NFR-103 bounds the WRITE and a node's wall time already includes everything
    else. `put()` receives the RunnableConfig, so the measurement lands on the
    trace of the run that caused it without any plumbing.

    The only NFR of the three that reaches into a dependency's surface, which is
    why a test pins that the subclass still round-trips state.
    """

    def put(self, config, checkpoint, metadata, new_versions):
        started = time.monotonic()
        try:
            return super().put(config, checkpoint, metadata, new_versions)
        finally:
            trace = (config or {}).get("configurable", {}).get("trace")
            if trace is not None:
                trace.append({"kind": "checkpoint",
                              "ms": round((time.monotonic() - started) * 1000, 3)})


def _build() -> StateGraph:
    b = StateGraph(AgentState)
    b.add_node("act", _timed("act", act))
    b.add_node("gate", _timed("gate", gate))
    b.add_node("execute", _timed("execute", execute))
    b.add_node("reflect", _timed("reflect", reflect))
    b.add_node("plan", _timed("plan", plan))
    b.add_node("adopt", _timed("adopt", adopt))
    b.add_node("compact", _timed("compact", compact))
    b.add_node("finish", _timed("finish", finish))

    b.add_edge(START, "act")                    # correction (a): not "plan"
    b.add_conditional_edges("act", _route_after_act, ["gate", "reflect"])
    b.add_edge("gate", "execute")               # CE-07: never merged
    b.add_edge("execute", "reflect")
    b.add_conditional_edges("reflect", _route_after_reflect,
                            ["act", "plan", "compact", "finish"])
    b.add_edge("plan", "adopt")          # write it, THEN ask
    b.add_edge("adopt", "act")
    b.add_edge("compact", "act")
    b.add_edge("finish", END)
    return b


_APP = None


def get_app():
    """Build the compiled graph.

    A factory rather than a module-level `app`: SqliteSaver needs a live connection,
    and `from_conn_string` is a context manager that would close it. Opening the
    database at import time would also be module-level I/O (CE-05) - it would create
    state.db merely by importing this module, including from every test.
    """
    global _APP
    if _APP is None:
        settings.STATE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(settings.STATE_DB), check_same_thread=False)
        _APP = _build().compile(checkpointer=_TimedSaver(conn))
    return _APP
