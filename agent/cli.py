"""Interactive entrypoint (FR-701, FR-703).

    python -m agent "goal"           run a new session
    python -m agent --list           show past threads
    python -m agent --resume <id>    continue one

The first place a human is in the loop. Every run before this point was
unattended, where autonomous=True turns a `confirm` verdict into a refusal and
nothing ever pauses. Interactive mode sets it False, which is the switch that
makes the gate suspend and ask instead - so this file is the first caller of a
code path that had never executed.

Output is ASCII on purpose: this runs in a container whose output lands in a
Windows terminal, and box-drawing characters arrive there as mojibake often
enough that they are not worth the risk.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
import uuid

from langgraph.types import Command

from agent import config as settings
from agent import mcp, memory, policy, skills, tools
from agent.graph import get_app, new_state

RULE = "-" * 64

# Returned by the prompt to stop the session rather than answer the question.
# Distinct from "deny", which answers it: the gate treats every non-"allow"
# string as a rejection, so this must never reach it.
QUIT = "\0quit"


# --------------------------------------------------------------- live display

class LiveTrace(list):
    """A trace list that renders as it fills.

    Every node already reports through `trace.append(...)`, so subclassing that
    list is the entire live-output mechanism: graph.py needs no callback, no
    streaming API, and no knowledge that a terminal exists. The harness keeps
    passing a plain list and is unaffected.
    """

    def __init__(self, sink=None) -> None:
        super().__init__()
        self.turn = 0
        self.tokens = 0
        # Only the RENDERING is swappable; the counters are not - two callers
        # tracking spend separately is how two numbers come to disagree.
        self._sink = sink or self._render

    def append(self, entry: dict) -> None:
        super().append(entry)
        if entry.get("kind") == "model":
            # Turn numbers are counted here rather than read from state: a turn
            # begins when the model is called, and the caller sees that moment
            # live while state only arrives once invoke() returns.
            self.turn += 1
            self.tokens += entry.get("billed_tokens", 0)
        self._sink(entry)

    def _render(self, e: dict) -> None:
        kind = e.get("kind")
        if kind == "model":
            print(f"\nturn {self.turn}/{settings.MAX_TURNS}"
                  f"   {self.tokens:,} tokens", flush=True)
        elif kind == "tool":
            print("  " + _tool_line(e), flush=True)
        elif kind == "terminal":
            print(f"\n{RULE}\n{e['verdict']}  |  {e['turns']} turns  |  "
                  f"{e['spent_tokens']:,} tokens", flush=True)


def _tool_line(e: dict) -> str:
    """One line per tool call: what ran, on what, how it went, how long."""
    mark = "x" if e.get("is_error") else ">"
    note = "  DENIED" if e.get("verdict") == "deny" else ("  ERROR" if e.get("is_error") else "")
    if e.get("spill_path"):
        note += "  [spilled]"
    return (f"{mark} {e['tool']:<11} {e.get('summary', '')[:42]:<42}"
            f"{e.get('duration_ms', 0) / 1000:>6.1f}s{note}")


def _on_text(text: str) -> None:
    """Model prose as it arrives.

    Written incrementally because the two providers deliver it differently: the
    Anthropic path streams deltas, the OpenAI-compatible path hands over one
    finished block. Plain appending is correct for both.
    """
    sys.stdout.write(text)
    sys.stdout.flush()


# ------------------------------------------------------------------- approval

def ask_plan(payload: dict) -> str:
    """Show the plan and read one keystroke (UR-02, UR-05).

    Same contract as the tool prompt below: an unrecognised key re-asks, never
    guesses. What differs is the safe answer with no terminal attached. There it
    is `deny` - a refusal the agent routes around and the run continues. Here it
    is QUIT, because `revise` would send the graph back to planning and ask
    again, and with nothing on stdin that is an infinite loop rather than a
    safe default.
    """
    print(f"\n  +-- PLAN {'-' * 53}")
    for number, step in enumerate(payload["plan"], 1):
        print(f"  | {number}. {step}")
    print(f"  +{'-' * 61}")

    while True:
        try:
            answer = input("    [a]ccept  [r]evise  [q]uit > ").strip().lower()
        except EOFError:
            print("quit (no terminal)")
            return QUIT
        if answer in ("a", "accept"):
            return "accept"
        if answer in ("r", "revise"):
            try:
                note = input("    what should change? > ").strip()
            except EOFError:
                note = ""
            # The note travels back as an ordinary user message, so saying WHY
            # is worth a second prompt; an empty answer still revises.
            return note or "revise"
        if answer in ("q", "quit"):
            return QUIT
        print("    unrecognised - answer a, r or q")


def ask_question(question: str, choices: list[str]) -> str:
    """One question from the agent, and the answer (`ask_user`).

    EOFError returns "" so the tool falls back to its own no-one-is-there
    message: the same rule as the approval prompt, where silence is not
    consent - here it is not an answer either.
    """
    print(f"\n  +-- THE AGENT IS ASKING {'-' * 38}")
    print(f"  | {question}")
    for number, choice in enumerate(choices, 1):
        print(f"  |   {number}. {choice}")
    print(f"  +{'-' * 61}")
    prompt = "    answer (or a number) > " if choices else "    answer > "
    try:
        answer = input(prompt).strip()
    except EOFError:
        print("(no terminal)")
        return ""
    if choices and answer.isdigit() and 1 <= int(answer) <= len(choices):
        return choices[int(answer) - 1]
    return answer


# Approvals remembered for this PROCESS, keyed by (tool, the rule the gate
# named). Here and not in classify(): the gate is pure (FR-305) and re-runs on
# resume, so memory anywhere upstream of it would be consulted twice. Asking the
# same question twice trains people to answer without reading, then to turn
# the gate off. Hardline refusals never reach here - they are not a question.
_APPROVED: set[tuple[str, str]] = set()


def _rule_key(payload: dict) -> tuple[str, str]:
    """The tool and the rule, with the unattended suffix stripped."""
    reason = str(payload.get("reason", "")).split(";", 1)[0].strip()
    return (payload["call"]["name"], reason)


def remembered(payload: dict) -> bool:
    return _rule_key(payload) in _APPROVED


def remember(payload: dict) -> None:
    _APPROVED.add(_rule_key(payload))


def forget_approvals() -> None:
    _APPROVED.clear()


def _amend(current: dict) -> dict | None:
    """Read a replacement for each argument; Enter keeps the current value.

    None when the terminal goes away mid-edit - silence is not consent one step
    later either. A changed value arrives as a string and the tool coerces it at
    its own boundary, which is where every declared type is enforced anyway.
    """
    print("    amend - Enter keeps the current value")
    edited = {}
    for key, value in current.items():
        try:
            typed = input(f"      {key} [{value}] > ")
        except EOFError:
            print("deny (no terminal)")
            return None
        edited[key] = typed if typed.strip() else value
    return edited


def ask_human(payload: dict) -> str | dict:
    """Render a paused call and read one keystroke (FR-306, NFR-801).

    The argument set is shown in full, never abbreviated. A prompt that elides
    the dangerous half of a command manufactures consent instead of obtaining it.

    FR-307: `[e]dit` answers with the CORRECTED arguments instead of yes or no.
    The gate re-classifies them, so this is a way to fix a call, never a way
    past the gate.
    """
    if "plan" in payload:
        return ask_plan(payload)
    if remembered(payload):
        print(f"\n  allowed: {payload.get('reason', '')} (approved for this session)")
        return "allow"

    call = payload["call"]
    print(f"\n  +-- APPROVAL NEEDED {'-' * 42}")
    print(f"  | {call['name']}")
    print("  |")
    for key, value in call["input"].items():
        print(f"  |   {key}: {value}")
    print("  |")
    print(f"  | Reason: {payload.get('reason', '')}")
    print(f"  +{'-' * 61}")

    while True:
        try:
            answer = input(
                "    [a]llow  [s]ession  [e]dit  [d]eny  [q]uit > ").strip().lower()
        except EOFError:
            # No terminal attached. Silence is not consent.
            print("deny (no terminal)")
            return "deny"
        if answer in ("a", "allow"):
            return "allow"
        if answer in ("s", "session"):
            remember(payload)
            return "allow"
        if answer in ("e", "edit"):
            # Never remembered: `remember` keys on the rule the gate named, and
            # that rule described the arguments this answer is replacing.
            amended = _amend(call["input"])
            return "deny" if amended is None else {"decision": "amend",
                                                   "input": amended}
        if answer in ("d", "deny"):
            return "deny"
        if answer in ("q", "quit"):
            return QUIT
        # Anything unrecognised re-asks. A mistyped key must never read as yes.
        print("    unrecognised - answer a, s, e, d or q")


# -------------------------------------------------------------------- session

def start_mode(chosen: str | None) -> str:
    """The mode a session begins in.

    `auto` is refused from the ENVIRONMENT on purpose: it is the mode that stops
    asking entirely, and a mode that stops asking must be turned on deliberately
    for one session, not left in a .env and forgotten.
    """
    if chosen:
        return chosen
    from_env = policy.resolve_mode(settings.MODE)
    return "normal" if from_env == "auto" else from_env


def run_session(goal: str | None, thread: str, app, mode: str = "normal") -> int:
    """One interactive run, from a fresh goal or a resumed thread.

    A single loop shape covers both, plus every approval pause, because resuming
    a thread and resuming an interrupt are the same operation to the graph: a
    task's identity IS its thread id.
    """
    trace = LiveTrace()
    # ask_user reaches a person through here. The TOOL owns the schema, the
    # SURFACE owns the asking, and an unset hook answers "nobody is there"
    # rather than blocking - which is what --worker and cron depend on.
    tools.ASK = ask_question
    cfg = {"configurable": {
        "thread_id": thread,
        "autonomous": False,      # the switch that makes `confirm` pause, not refuse
        "mode": mode,
        "trace": trace,
        "on_text": _on_text,
    }}

    print(f"thread  {thread}")
    if goal:
        print(f"goal    {goal}")
    else:
        # Resuming: seed the live counters from the checkpoint, or the display
        # would restart at turn 1 while the run is really on turn 9.
        prior = app.get_state(cfg).values or {}
        trace.turn, trace.tokens = prior.get("turns", 0), prior.get("spent_tokens", 0)
        messages = prior.get("messages") or []
        if messages and isinstance(messages[0].get("content"), str):
            print(f"goal    {messages[0]['content']}")
        print(f"resumed at turn {trace.turn}, {trace.tokens:,} tokens spent")
    print(RULE)

    # None as the payload continues an existing thread instead of seeding one.
    out = app.invoke(new_state(goal) if goal is not None else None, cfg)

    while "__interrupt__" in out:
        answer = ask_human(out["__interrupt__"][0].value)
        if answer == QUIT:
            # A half-approved turn is a legitimate resting place. The checkpoint
            # already holds it, so quitting costs nothing but the prompt.
            print(f"\nstopped, still checkpointed.\n"
                  f"resume with:  python -m agent --resume {thread}")
            return 0
        out = app.invoke(Command(resume=answer), cfg)

    print(f"resume with:  python -m agent --resume {thread}")
    return 0


# -------------------------------------------------------------------- threads

def _thread_ids(app) -> list[str]:
    """Distinct thread ids, newest first, through the app's own checkpointer
    rather than a second connection to the same database."""
    seen: list[str] = []
    for tup in app.checkpointer.list(None):
        tid = tup.config.get("configurable", {}).get("thread_id")
        if tid and tid not in seen:
            seen.append(tid)
    return seen


def _thread_rows(app) -> list[dict]:
    """Thread id, verdict, turn count and goal - newest first, as DATA.

    The TUI puts this same walk in a table while list_threads() prints it. One
    traversal of the checkpointer with two presentations, rather than two
    traversals that can quietly disagree about what a thread's goal was.
    """
    rows = []
    for tid in _thread_ids(app):
        values = app.get_state({"configurable": {"thread_id": tid}}).values or {}
        messages = values.get("messages") or []
        goal = messages[0]["content"] if messages and isinstance(
            messages[0].get("content"), str) else ""
        rows.append({"id": tid, "verdict": values.get("verdict") or "-",
                     "turns": values.get("turns", 0), "goal": goal})
    return rows


def list_threads(app) -> int:
    rows = _thread_rows(app)
    if not rows:
        print("no threads yet")
        return 0
    print(f"{'thread':<20} {'verdict':<10} {'turns':>5}  goal")
    for row in rows:
        print(f"{row['id']:<20} {row['verdict']:<10} "
              f"{row['turns']:>5}  {row['goal'][:40]}")
    return 0


TIME_FMT = "%Y-%m-%d %H:%M"


def _calls_model(args) -> bool:
    """Which flags reach act(). Everything else is database and disk.

    Measured 2026-09-05: --channel-check exited 2 on a missing web-fetch tool
    because activation ran ahead of every flag, and both scheduled tasks died
    at logon the same way. --channel queues and answers; only --worker runs it.
    """
    return bool(args.worker or args.tui or args.resume or args.goal)


# The repository this install points at. Derived from THIS FILE, not the
# working directory, because `noesis` is typed from anywhere.
_REPO = pathlib.Path(__file__).resolve().parent.parent

def _run(*cmd: str) -> tuple[str, int]:
    """A command in the repository, as (stdout, returncode). Never raises."""
    import subprocess

    done = subprocess.run(list(cmd), cwd=str(_REPO), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return (done.stdout or "").strip(), done.returncode


def update() -> int:
    """Pull the repository, and reinstall only if the entry point moved.

    An editable install points at this source tree, so a pull is normally the
    whole update - `pip install -e .` is needed only when pyproject changes,
    which is when a console script or a dependency has.

    Refuses a dirty tree rather than stashing. A silent stash hides the loss
    until someone goes looking for it, and this runs unattended by design.
    """
    dirty, _ = _run("git", "status", "--porcelain")
    if dirty:
        print("uncommitted changes in the repository - commit or stash them "
              "first, then run this again:\n" + dirty, file=sys.stderr)
        return 2

    before, code = _run("git", "rev-parse", "--short", "HEAD")
    if code != 0:
        print("not a git repository, so there is nothing to update from",
              file=sys.stderr)
        return 2

    # --ff-only: a merge commit made by an update nobody watched is a surprise
    # in the history, and a conflict must stop rather than half-apply.
    out, code = _run("git", "pull", "--ff-only")
    if code != 0:
        print(f"pull failed, nothing changed:\n{out}", file=sys.stderr)
        return 1

    after, _ = _run("git", "rev-parse", "--short", "HEAD")
    if after == before:
        print(f"already up to date at {before}")
        return 0

    changed, _ = _run("git", "diff", "--name-only", f"{before}..{after}")
    print(f"updated {before} -> {after}")

    if "pyproject.toml" in changed:
        # The entry point or a dependency moved, so the installed metadata is
        # stale. Nothing else in the tree needs pip: the install is editable.
        print("pyproject changed - reinstalling the console script")
        out, code = _run(sys.executable, "-m", "pip", "install", "-e", ".",
                         "--no-deps")
        if code != 0:
            print(f"reinstall failed - run `pip install -e .` yourself:\n{out}",
                  file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent")
    parser.add_argument("goal", nargs="?", help="what to do, in plain language")
    parser.add_argument("--list", action="store_true", help="show past threads")
    parser.add_argument("--resume", metavar="THREAD_ID", help="continue a thread")
    parser.add_argument("--tui", action="store_true",
                        help="full-screen chat (FR-701) instead of line output")
    parser.add_argument("--submit", metavar="GOAL",
                        help="queue a task and print its id, without running it")
    parser.add_argument("--worker", action="store_true",
                        help="drain the task queue; runs until interrupted")
    parser.add_argument("--channel", action="store_true",
                        help="listen on email; queues messages and answers them")
    parser.add_argument("--cancel", metavar="TASK_ID",
                        help="stop a queued or running task")
    parser.add_argument("--mode", choices=policy.MODES, default=None,
                        help="how much the gate asks: manual (everything), "
                             "plan (reads only), normal, auto (nothing). "
                             "`auto` lasts one session and is never read from .env")
    parser.add_argument("--terminal-profile", nargs="?", const="", default=None,
                        choices=("blurred", "clear", "remove", ""),
                        metavar="blurred|clear|remove",
                        help="add a transparent NOESIS profile to Windows "
                             "Terminal, or remove it; asks which if not given")
    parser.add_argument("--doctor", action="store_true",
                        help="check every precondition; changes nothing")
    parser.add_argument("--update", action="store_true",
                        help="pull the repository and reinstall if needed")
    parser.add_argument("--serve", action="store_true",
                        help="read-only viewer on localhost; prints a URL")
    parser.add_argument("--channel-check", action="store_true",
                        help="probe the mailbox and report; sends nothing")
    parser.add_argument("--review", action="store_true",
                        help="show what needs attention, and queue a review of it")
    parser.add_argument("--schedule", nargs=2, metavar=("CRON", "GOAL"),
                        help="run GOAL on a cron schedule, e.g. '0 9 * * 1'")
    parser.add_argument("--schedules", action="store_true",
                        help="show cron schedules, soonest first")
    parser.add_argument("--unschedule", metavar="SCHEDULE_ID",
                        help="remove a cron schedule")
    parser.add_argument("--attach", metavar="TASK_ID",
                        help="watch a running task; ctrl-c detaches")
    parser.add_argument("--tasks", action="store_true",
                        help="show queued and finished tasks")
    args = parser.parse_args(argv)

    app = get_app()

    if args.list:
        return list_threads(app)          # no model, so no tools, so no startup

    if not _calls_model(args):
        return _dispatch(args, app, parser)

    # Started before EITHER path that calls a model and torn down after both,
    # so a resumed thread does not start a second set of servers.
    memory.activate()
    try:
        loaded = skills.activate()
    except skills.SkillIndexTooLarge as exc:
        print(f"skills: {exc}", file=sys.stderr)
        return 2
    if loaded:
        print(f"skills available: {', '.join(loaded)}")
    try:
        started = mcp.activate()
    except (mcp.McpUnavailable, mcp.ToolBudgetExceeded) as exc:
        print(f"MCP: {exc}", file=sys.stderr)
        print("Run with AGENT_MCP=off to continue with the built-in tools only.",
              file=sys.stderr)
        return 2
    if started:
        print(f"MCP tools: {', '.join(started)}")

    try:
        return _dispatch(args, app, parser)
    except KeyboardInterrupt:
        # Everything is checkpointed after every node transition, so an interrupt
        # costs at most one node. Saying so beats a traceback.
        print("\nstopped. resume with:  python -m agent --list")
        return 0
    finally:
        try:
            mcp.shutdown()
        except KeyboardInterrupt:
            # A SECOND ctrl-c, landing inside the transport thread's join. The
            # subprocess dies with the container either way; a stack trace here
            # only makes a clean exit look like a failure.
            pass
        memory.deactivate()
        skills.deactivate()


def terminal_profile(choice: str) -> int:
    """Add, or remove, a transparent NOESIS profile in Windows Terminal.

    A FRAGMENT rather than an edit to the user's settings.json, so this installs
    one file and removes one file. §10.4 is the reason it exists at all: only the
    emulator can make a window transparent.
    """
    from agent import setup

    if setup.fragment_path() is None:
        print("Windows Terminal fragments need Windows.\n"
              f"{setup.OPACITY_HINT}", file=sys.stderr)
        return 1

    if choice == "remove":
        if setup.remove_profile():
            print(f"removed {setup.PROFILE_NAME}. Restart Windows Terminal.")
            return 0
        print(f"no {setup.PROFILE_NAME} profile to remove")
        return 0

    if not choice:
        if not setup.interactive():
            print("say which: --terminal-profile blurred, clear, or remove",
                  file=sys.stderr)
            return 2
        choice = _ask_look()
        if not choice:
            return 0

    path = setup.install_profile(blurred=choice == "blurred")
    print(f"wrote {path}\n"
          f"Restart Windows Terminal, then pick {setup.PROFILE_NAME} from the "
          f"dropdown.\nYour own settings.json was not touched; remove it with "
          f"--terminal-profile remove.")
    return 0


def _ask_look() -> str:
    """blurred, clear, or "" to leave it alone. Asked rather than defaulted,
    because the two look different enough that neither is the obvious one."""
    print("  blurred  frosted glass - most readable, but the window goes "
          "opaque when it loses focus")
    print("  clear    sharp see-through - stays transparent unfocused, "
          "Windows 11 only")
    while True:
        try:
            answer = input("  [b]lurred  [c]lear  [s]kip > ").strip().lower()
        except EOFError:
            return ""
        if answer[:1] in ("b", "c"):
            return "blurred" if answer[0] == "b" else "clear"
        if answer[:1] in ("s", "q") or not answer:
            return ""


def attach(task_id: str) -> int:
    """Follow a running task's trace until it leaves the queue (FR-606).

    READ-ONLY, and that is not a shortcoming: a worker runs autonomous, where
    `confirm` is already `deny`, so there is nothing here for a human to answer.
    """
    from agent import worker

    task = worker.get(task_id)
    if task is None:
        print(f"no such task: {task_id}", file=sys.stderr)
        return 1
    print(f"{task_id}  {task['status']}  {task['goal']}")
    seen, turn, tokens = 0, 0, 0
    try:
        while True:
            for entry in worker.events(task_id, after=seen):
                seen = entry["seq"]
                kind = entry.get("kind")
                if kind == "model":
                    turn += 1
                    tokens += entry.get("billed_tokens", 0)
                    print(f"\nturn {turn}   {tokens:,} tokens", flush=True)
                elif kind == "tool":
                    print("  " + _tool_line(entry), flush=True)
            task = worker.get(task_id) or task
            if task["status"] not in ("queued", "running"):
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\ndetached - the task keeps running")
        return 0
    print(f"\n{RULE}\n{task['status']}  {task.get('verdict') or ''}  "
          f"{task.get('detail') or ''}".rstrip())
    return 0


def list_tasks() -> int:
    """FR-604, and the other half of FR-703's "list threads and tasks"."""
    from agent import worker

    rows = worker.tasks()
    if not rows:
        print("no tasks yet")
        return 0
    print(f"{'task':<10} {'status':<18} {'verdict':<9} goal")
    for row in rows:
        print(f"{row['id']:<10} {row['status']:<18} {row['verdict'] or '-':<9} "
              f"{row['goal'][:38]}")
        if row.get("detail"):
            # UR-16: what it refused while nobody was watching is the whole
            # reason to look at this list.
            print(f"{'':<10} ! {row['detail'][:60]}")
    return 0


def _dispatch(args, app, parser) -> int:
    from agent import worker

    if args.tasks:
        return list_tasks()

    if args.attach:
        return attach(args.attach)

    if args.review:
        items = worker.attention()
        if not items:
            print("nothing needs attention")
            return 0
        for item in items:
            print(f"  {item}")
        task_id = worker.review()
        print(f"queued {task_id} to review it")
        print("run it with:   python -m agent --worker")
        print('every morning:  python -m agent --schedule "0 9 * * *" "@review"')
        return 0

    if args.schedule:
        expr, goal = args.schedule
        try:
            sched_id = worker.schedule(expr, goal)
        except ValueError as exc:
            print(f"bad schedule: {exc}", file=sys.stderr)
            return 2
        due = worker.next_run(expr, time.time())
        print(f"scheduled {sched_id}, first run {time.strftime(TIME_FMT, time.localtime(due))}")
        print("run it with:   python -m agent --worker")
        return 0

    if args.schedules:
        rows = worker.schedules()
        if not rows:
            print("no schedules")
            return 0
        for row in rows:
            nxt = time.strftime(TIME_FMT, time.localtime(row["next_run"]))
            print(f'{row["id"]}  {row["cron"]:<16} next {nxt}  {row["goal"]}')
        return 0

    if args.unschedule:
        if not worker.unschedule(args.unschedule):
            print(f"no such schedule: {args.unschedule}", file=sys.stderr)
            return 1
        print(f"removed {args.unschedule}")
        return 0

    if args.submit:
        # FR-601: returns immediately. The id IS the thread id, which is what
        # makes resuming a task and resuming a thread the same operation.
        task_id = worker.submit(args.submit)
        print(f"queued {task_id}")
        print("run it with:   python -m agent --worker")
        print(f"watch it with: python -m agent --attach {task_id}")
        return 0

    if args.update:
        return update()

    if args.serve:
        from agent import viewer

        return viewer.run()

    if args.terminal_profile is not None:
        return terminal_profile(args.terminal_profile)

    if args.doctor:
        from agent import channel

        lines = channel.diagnose()
        for line in lines:
            print(line)
        return 1 if any(l.startswith("FAIL") for l in lines) else 0

    if args.cancel:
        stopped = worker.cancel(args.cancel)
        if stopped is None:
            print(f"{args.cancel} is not queued or running", file=sys.stderr)
            return 1
        print(f"cancelled {args.cancel}")
        print("a task already running finishes its current step first")
        return 0

    if args.channel_check:
        from agent import channel

        lines = channel.check()
        for line in lines:
            print(line)
        return 1 if any(l.startswith("FAIL") for l in lines) else 0

    if args.channel:
        # A SEPARATE process from --worker on purpose: this one only queues and
        # answers, so a channel outage cannot strand a running graph and a dead
        # worker cannot lose somebody's reply.
        from agent import channel

        if not channel.configured():
            print("set AGENT_EMAIL_USER, AGENT_EMAIL_PASSWORD and",
                  "AGENT_EMAIL_ALLOW first.", file=sys.stderr)
            print("AGENT_EMAIL_ALLOW is a DEFAULT-DENY allowlist of addresses:",
                  file=sys.stderr)
            print("empty authorises nobody.", file=sys.stderr)
            return 2
        print(f"listening on {settings.EMAIL_USER}; "
              f"{len(settings.EMAIL_ALLOW)} address(es) authorised")
        print("the first pass adopts the existing inbox and answers none of it")
        print("run the tasks with: python -m agent --worker")
        try:
            channel.run_channel()
        except channel.ChannelRefused as exc:
            print(f"{exc}", file=sys.stderr)
            print("credentials rejected - for Gmail this must be an app password,",
                  "not the account password.", file=sys.stderr)
            return 2
        return 0

    if args.worker:
        requeued = worker.recover()
        if requeued:
            print(f"requeued {requeued} task(s) whose worker had died")
        print("worker running; ctrl-c to stop")
        worker.run_worker(app)
        return 0

    if args.tui:
        # Imported HERE, never at module top: the plain CLI must not pay Textual's
        # import cost, and the TUI is optional (CE-05).
        from agent import tui
        return tui.run(app, goal=args.goal, thread=args.resume)

    if args.resume:
        if args.resume not in _thread_ids(app):
            # Say what exists rather than raising a traceback at someone who
            # mistyped eight hex characters.
            print(f"no such thread: {args.resume}\n", file=sys.stderr)
            list_threads(app)
            return 2
        return run_session(None, args.resume, app, start_mode(args.mode))

    if not args.goal:
        parser.error("give a goal, or use --list / --resume")
    return run_session(args.goal, uuid.uuid4().hex[:8], app, start_mode(args.mode))
