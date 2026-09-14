"""The task queue and the worker that drains it (FR-601, FR-602, FR-604).

§12 deferred this file with the trigger "when FR-6xx enters scope". It fires
here: the three requirements are `[M]`, and §9 puts scope in play once the `[M]`
set passes evaluation, which it has.

WHAT THIS IS NOT: a scheduler welded to a state module. It is stdlib and
SQLite, built on two ideas, both of which are about not lying about state:

  IDEMPOTENT TRANSITIONS.  `UPDATE ... WHERE id=? AND status='queued'` followed
  by `if cur.rowcount != 1: return None`. A transition applies exactly once and a
  second attempt DECLINES rather than corrupting the row. That is NFR-302 - no
  duplicated side effects on resume - expressed in SQL, and it is the same
  discipline CE-07 enforces in the graph.

  LIVENESS BY pid AND START TIME.  A crashed worker leaves a row saying
  `running` forever. A pid alone cannot detect that, because pids are recycled
  and the next process to claim one would look like the original. The pair
  cannot be.

THE DIVERGENCE FROM THE USUAL DESIGN. The usual design marks an abandoned
execution `unknown` and refuses to retry, on the ground that whether side
effects ran is unknown. That is correct without checkpoints and wrong here:
this project checkpoints
after every node transition and CE-07 keeps `gate` and `execute` separate
precisely so a resumed run cannot re-fire a tool. An abandoned task therefore
goes back to `queued`, and the worker that picks it up RESUMES from the
checkpoint rather than restarting - which is FR-603, and the reason it is safe.

CE-05: nothing here runs at import; the database is created on first use.
NFR-602: every function below is testable with no API key and no network.
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid

from agent import config, migrations

# FR-604's five states, enforced by SQLite rather than by convention: a CHECK
# constraint refuses a typo that a Python-side check would let through.
STATUSES = ("queued", "running", "awaiting-approval", "done", "failed")

def _connect() -> sqlite3.Connection:
    config.TASKS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.TASKS_DB), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    # isolation_level=None means autocommit, and migrations.apply() opens its own
    # transaction per migration - `with conn` still begins one under autocommit.
    migrations.apply(conn, migrations.TASKS)
    return conn


def _pid_started(pid: int) -> float | None:
    """When `pid` started, or None when that cannot be determined.

    Read from /proc, which is where the worker runs. None on any other platform,
    and the liveness check below treats None as "cannot prove death".
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return float(handle.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _alive(pid: int | None, started: float | None) -> bool:
    """Whether the process that claimed a task is still running.

    FAILS SAFE: inability to prove death must not rewrite someone else's row.
    The alternative - assuming death when unsure - hands the same task to two
    workers.
    """
    if pid is None:
        return False
    # No fast path for "that is my own pid": it would skip the start-time compare,
    # which is the only thing distinguishing the owner from a recycled pid.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True                      # exists, or cannot be interrogated
    if started is None:
        return True                      # cannot compare, so cannot prove death
    now = _pid_started(pid)
    return now is None or now == started


def _row(record: sqlite3.Row | None) -> dict | None:
    return dict(record) if record is not None else None


# ------------------------------------------------------------------ FR-601

def submit(goal: str) -> str:
    """Accept a task and return its identifier immediately (FR-601).

    The id IS the graph's thread id, which is what makes FR-603 free: resuming a
    task and resuming a thread are the same operation.
    """
    task_id = uuid.uuid4().hex[:8]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, goal, status, submitted_at) VALUES (?,?,?,?)",
            (task_id, goal, "queued", time.time()))
    return task_id


# ------------------------------------------------------------------ FR-604

def tasks(limit: int = 50) -> list[dict]:
    """Every task, newest first (FR-604)."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM tasks ORDER BY submitted_at DESC LIMIT ?", (limit,))]


def get(task_id: str) -> dict | None:
    with _connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


# ------------------------------------------- transitions, each applied once

def claim() -> dict | None:
    """Move one queued task to running, or return None.

    The `AND status='queued'` in the UPDATE is the whole concurrency story: two
    workers racing for the same row produce one winner and one None, because
    SQLite settles it rather than the reader-then-writer sequence that would
    hand it to both.

    Returns None when MAX_WORKERS are already running (FR-607).
    """
    pid = os.getpid()
    # FR-607, and the ORDER matters. A `running` row whose worker died still
    # counts against the cap, so without this a single crash deadlocks the
    # queue permanently. recover() is the existing liveness sweep.
    recover()
    with _connect() as conn:
        running = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='running'").fetchone()
        if running["n"] >= config.MAX_WORKERS:
            return None
        row = conn.execute(
            "SELECT id FROM tasks WHERE status='queued' "
            "ORDER BY submitted_at LIMIT 1").fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE tasks SET status='running', started_at=?, pid=?, pid_started=? "
            "WHERE id=? AND status='queued'",
            (time.time(), pid, _pid_started(pid), row["id"]))
        if cur.rowcount != 1:
            return None                  # someone else took it between the two
        return _row(conn.execute(
            "SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())


def conclude(task_id: str, *, status: str, verdict: str = "",
             detail: str = "") -> dict | None:
    """Write a terminal state once. A task already finished cannot be rewritten."""
    if status not in ("done", "failed", "awaiting-approval"):
        raise ValueError(f"{status!r} is not a terminal status")
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status=?, verdict=?, detail=?, finished_at=? "
            "WHERE id=? AND status IN ('running','queued')",
            (status, verdict, detail, time.time(), task_id))
        if cur.rowcount != 1:
            return None
        return _row(conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def cancel(task_id: str) -> dict | None:
    """Stop a queued or running task. None when it was already terminal.

    conclude() already moves queued/running to a terminal state exactly once, so
    this is that call and nothing more - no new status, no migration.

    A RUNNING task keeps going until its current node returns; the row flips now
    and the worker's own conclude() then finds nothing to update, which is the
    idempotent path doing its job rather than a race.
    """
    return conclude(task_id, status="failed", verdict="cancelled",
                    detail="cancelled by the user")


def recover() -> int:
    """Requeue tasks whose worker died. Returns how many.

    Without checkpoints these would have to be marked `unknown` and never
    retried, because nothing could know whether side effects ran. This project
    can: state is checkpointed after every
    node transition, and CE-07 keeps `gate` and `execute` separate so a resumed
    run re-classifies rather than re-executes. So the safe move here is `queued`,
    and the worker that picks it up resumes mid-task (FR-603).
    """
    changed = 0
    with _connect() as conn:
        for row in conn.execute(
                "SELECT id, pid, pid_started FROM tasks WHERE status='running'"):
            if _alive(row["pid"], row["pid_started"]):
                continue
            changed += conn.execute(
                "UPDATE tasks SET status='queued', pid=NULL, pid_started=NULL "
                "WHERE id=? AND status='running'", (row["id"],)).rowcount
    return changed


# --------------------------------------------------- proactivity (UR-14, UR-16)

# A schedule whose goal is this sentinel is resolved AT FIRE TIME rather than
# stored. A review's content is whatever is outstanding right now, so a schedule
# holding fixed text could not express it - and generating it at schedule time
# would report last week's state forever.
REVIEW = "@review"


def attention() -> list[str]:
    """What is outstanding, in plain sentences. Deterministic: no model call.

    Three sources, all state that already exists:

      awaiting-approval  what the agent REFUSED while nobody was watching. UR-16
                         asks to review exactly this, and it is the one status
                         that cannot resolve itself.
      failed             a run that ended badly and has not been looked at.
      NOW.md             the step a session was on when it stopped.
    """
    from agent import memory

    items: list[str] = []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, goal, status, detail FROM tasks"
            " WHERE status IN ('awaiting-approval','failed')"
            " ORDER BY submitted_at DESC LIMIT 10").fetchall()
    for row in rows:
        line = f"task {row['id']} ({row['status']}): {row['goal'][:120]}"
        if row["detail"]:
            line += f" - {row['detail'][:120]}"
        items.append(line)

    scratch = memory.now()
    if "Still to do:" in scratch:
        items.append("unfinished from the last session: "
                     + scratch.split("Still to do:", 1)[1].strip().splitlines()[0])
    return items


def review() -> str | None:
    """Queue a review of what is outstanding, or return None when nothing is.

    SILENCE WHEN THERE IS NOTHING TO SAY is the half that makes this usable. An
    hourly check that always speaks is an hourly interruption, and the first thing
    anyone does with one is turn it off.

    It does not interrupt work either, and that falls out of what is already
    there rather than needing a rule: MAX_WORKERS caps concurrent tasks, so a
    queued review waits its turn behind whatever is running (FR-607).
    """
    items = attention()
    if not items:
        return None
    return submit(
        "Review what is outstanding and tell me what needs my attention. "
        "Do not change anything - report only." + chr(10) * 2
        + chr(10).join(f"- {item}" for item in items))


# ------------------------------------------------------------------ FR-605

FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _field(spec: str, low: int, high: int) -> set[int]:
    """The set of values one cron field matches.

    Written rather than taken from croniter: pip.conf sets no-index, so a
    library not baked into the image does not exist. Five fields
    of `*`, `*/n`, `a-b` and `a,b` is the whole of standard cron syntax.
    """
    matched: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, _, raw = part.partition("/")
            step = int(raw)
            if step < 1:
                raise ValueError(f"step must be positive: {spec!r}")
        if part in ("*", ""):
            first, last = low, high
        elif "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            first, last = int(a), int(b)
        else:
            first = last = int(part)
        if not (low <= first <= last <= high):
            raise ValueError(f"{part!r} is outside {low}-{high}")
        matched.update(range(first, last + 1, step))
    return matched


def parse_cron(expr: str) -> list[set[int]]:
    """Five fields: minute, hour, day-of-month, month, day-of-week."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"a cron expression has five fields, got {len(parts)}: {expr!r}")
    return [_field(p, low, high) for p, (low, high) in zip(parts, FIELDS)]


def next_run(expr: str, after: float) -> float:
    """The first whole minute strictly after `after` that `expr` matches.

    Searched a minute at a time over four years, which covers 29 February. An
    expression matching nothing - 30 February - raises rather than looping.
    """
    minute, hour, dom, month, dow = parse_cron(expr)
    when = time.localtime(after + 60)
    stamp = time.mktime((when.tm_year, when.tm_mon, when.tm_mday,
                         when.tm_hour, when.tm_min, 0, 0, 0, -1))
    for _ in range(4 * 366 * 24 * 60):
        t = time.localtime(stamp)
        # Cron numbers Sunday 0; struct_time numbers Monday 0.
        if (t.tm_min in minute and t.tm_hour in hour and t.tm_mon in month
                and t.tm_mday in dom and (t.tm_wday + 1) % 7 in dow):
            return stamp
        stamp += 60
    raise ValueError(f"{expr!r} never matches")


def schedule(expr: str, goal: str) -> str:
    """Register a recurring task and return its id (FR-605)."""
    now = time.time()
    due = next_run(expr, now)            # validates expr before anything is written
    sched_id = uuid.uuid4().hex[:8]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO schedules (id, cron, goal, next_run, created_at)"
            " VALUES (?,?,?,?,?)", (sched_id, expr, goal, due, now))
    return sched_id


def schedules() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM schedules ORDER BY next_run")]


def unschedule(sched_id: str) -> bool:
    with _connect() as conn:
        return conn.execute("DELETE FROM schedules WHERE id=?",
                            (sched_id,)).rowcount == 1


def fire(now: float | None = None) -> list[str]:
    """Enqueue one task per schedule now due. Returns the task ids.

    The ordering is the whole correctness argument: next_run is ADVANCED FIRST,
    guarded on the value just read, and only the writer whose
    rowcount is 1 submits. Two workers polling the same second produce one task,
    not two, and a submit that follows cannot fire the same slot twice.
    """
    now = time.time() if now is None else now
    fired = []
    with _connect() as conn:
        due = conn.execute("SELECT * FROM schedules WHERE next_run <= ?",
                           (now,)).fetchall()
    for row in due:
        try:
            following = next_run(row["cron"], now)
        except ValueError:
            continue                     # a stored expression that no longer parses
        with _connect() as conn:
            claimed = conn.execute(
                "UPDATE schedules SET next_run=? WHERE id=? AND next_run=?",
                (following, row["id"], row["next_run"])).rowcount == 1
        if not claimed:
            continue                     # another worker took this slot
        # The sentinel is resolved HERE, against current state. review() returns
        # None when nothing is outstanding, and a schedule that finds nothing to
        # say must enqueue nothing rather than an empty task.
        task_id = review() if row["goal"] == REVIEW else submit(row["goal"])
        with _connect() as conn:
            conn.execute("UPDATE schedules SET last_fired=?, last_task=? WHERE id=?",
                         (now, task_id, row["id"]))
        if task_id:
            fired.append(task_id)
    return fired


# ------------------------------------------------------------------ FR-602

def run_once(app, task: dict, trace: list | None = None) -> dict | None:
    """Run one claimed task to a terminal state.

    `autonomous=True`, so a `confirm` verdict becomes a denial (FR-304) rather
    than a pause. That is not a compromise, it is the only coherent reading:
    nobody is watching, and a worker that suspended on approval would hold the
    task open until someone happened to look.

    Which is what `awaiting-approval` is FOR here. A run that finished having
    REFUSED destructive calls is not simply `done` - UR-16 asks to review what
    was queued while you were away, and a task that hid its refusals under
    `done` would make that unanswerable.
    """
    from agent.graph import _final_text, new_state

    cfg = {"configurable": {"thread_id": task["id"], "autonomous": True,
                            "trace": trace if trace is not None else []}}
    prior = app.get_state(cfg).values or {}
    try:
        # None resumes a checkpointed thread; a fresh state seeds a new one. A
        # requeued task takes the first branch, which is FR-603 in one line.
        out = app.invoke(None if prior.get("messages") else new_state(task["goal"]), cfg)
    except Exception as exc:              # noqa: BLE001 - a crash is a result
        return conclude(task["id"], status="failed",
                        detail=f"{type(exc).__name__}: {exc}")

    verdict = out.get("verdict") or ""
    denied = out.get("denied") or []
    if denied:
        names = ", ".join(sorted({c.get("name", "?") for c in denied}))
        return conclude(task["id"], status="awaiting-approval", verdict=verdict,
                        detail=f"refused while unattended: {names}")

    # `done` means the AGENT finished, not that the worker exited cleanly - a
    # crashed worker leaves the task running so recover() can requeue it.
    if verdict == "done":
        # The ANSWER, not the verdict. channel._reply sends `detail or verdict`,
        # so without this a person who emailed a question is mailed back "done".
        return conclude(task["id"], status="done", verdict=verdict,
                        detail=_final_text(out.get("messages") or []))
    return conclude(task["id"], status="failed", verdict=verdict,
                    detail=f"agent ended {verdict or 'with no verdict'}")


def run_worker(app, once: bool = False, poll: float = 2.0) -> int:
    """Drain the queue, one task at a time (FR-602).

    A loop over the graph this project already has, not a supervisor: ~40 lines,
    because everything hard about running a task -
    checkpointing, the gate, budgets - is already in the graph.

    Schedules are polled here rather than run here: fire() enqueues through
    submit(), so the worker stays the only thing that executes a task.
    """
    recover()
    while True:
        fire()                           # FR-605: due schedules enter the SAME queue
        task = claim()
        if task is None:
            if once:
                return 0
            time.sleep(poll)
            continue
        run_once(app, task)
        if once:
            return 1
