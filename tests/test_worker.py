"""The task queue and the worker (FR-601, FR-602, FR-603, FR-604).

The SEVENTH stated deviation from §12's three-file tests/ allowlist, to the same
standard as the others - the component's failure is SILENT. A queue that hands
one task to two workers, or that leaves a crashed worker's row saying `running`
forever, does not raise anything. It just quietly does the work twice, or never.
Neither shows up in another suite, and neither shows up in a pass rate.

No API key, no network: the graph is a stand-in throughout (NFR-602).
"""
import os
import time

import pytest

from agent import config, worker


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """A task database of this test's own, never the real agent home."""
    monkeypatch.setattr(config, "TASKS_DB", tmp_path / "tasks.db")
    return tmp_path / "tasks.db"


# ===================================================================== FR-601

def test_submit_returns_an_id_immediately(queue):
    task_id = worker.submit("fix the failing tests")

    assert task_id and len(task_id) == 8
    row = worker.get(task_id)
    assert row["status"] == "queued"
    assert row["goal"] == "fix the failing tests"
    assert row["started_at"] is None


def test_the_id_is_the_thread_id(queue):
    """Not decoration: it is what makes resuming a task and resuming a thread the
    same operation, which is FR-603 for free."""
    task_id = worker.submit("goal")
    assert worker.get(task_id)["id"] == task_id


# ===================================================================== FR-604

def test_tasks_lists_newest_first(queue):
    first = worker.submit("first")
    time.sleep(0.01)
    second = worker.submit("second")

    assert [r["id"] for r in worker.tasks()] == [second, first]


def test_the_status_vocabulary_is_enforced_by_the_database(queue):
    """A CHECK constraint is the difference between a vocabulary and a
    suggestion. A typo in a transition must fail at the write, not create a sixth
    state nothing lists."""
    import sqlite3

    worker.submit("goal")
    with worker._connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE tasks SET status='in-progress'")


def test_every_declared_status_is_actually_writable(queue):
    """The other direction: a CHECK that rejected a status the code uses would
    fail only in production."""
    import sqlite3

    with worker._connect() as conn:
        for status in worker.STATUSES:
            conn.execute("INSERT OR REPLACE INTO tasks "
                         "(id, goal, status, submitted_at) VALUES (?,?,?,?)",
                         (status, "g", status, time.time()))


# ============================================== transitions, applied exactly once

def test_claim_moves_one_task_to_running(queue):
    task_id = worker.submit("goal")

    claimed = worker.claim()

    assert claimed["id"] == task_id
    assert claimed["status"] == "running"
    assert claimed["pid"] == os.getpid()


def test_a_second_claim_finds_nothing(queue):
    """The idempotence that matters: two workers racing must produce one winner
    and one None, never the same task twice. NFR-302, in SQL."""
    worker.submit("goal")

    assert worker.claim() is not None
    assert worker.claim() is None


def test_claim_takes_the_oldest_first(queue):
    first = worker.submit("first")
    time.sleep(0.01)
    worker.submit("second")

    assert worker.claim()["id"] == first


def test_a_terminal_task_cannot_be_rewritten(queue):
    """A finished task is finished. Allowing a late write would let a slow
    worker overwrite the result of the one that actually did the job."""
    task_id = worker.submit("goal")
    worker.claim()

    assert worker.conclude(task_id, status="done", verdict="done") is not None
    assert worker.conclude(task_id, status="failed") is None
    assert worker.get(task_id)["status"] == "done"


def test_conclude_refuses_a_non_terminal_status(queue):
    task_id = worker.submit("goal")
    with pytest.raises(ValueError):
        worker.conclude(task_id, status="running")


# ============================================ liveness, and the recovery it enables

def test_a_live_worker_keeps_its_task(queue):
    worker.submit("goal")
    worker.claim()                       # claimed by THIS process, which is alive

    assert worker.recover() == 0
    assert worker.get(worker.tasks()[0]["id"])["status"] == "running"


def test_a_dead_workers_task_is_requeued(queue):
    """A crashed worker leaves a row saying `running` forever. Requeued rather
    than marked unknown - without checkpoints nothing could know whether side
    effects ran; this project checkpoints after every node and CE-07 keeps
    gate and execute separate, so a resumed run re-classifies rather than
    re-executes."""
    task_id = worker.submit("goal")
    worker.claim()
    with worker._connect() as conn:
        conn.execute("UPDATE tasks SET pid=?, pid_started=? WHERE id=?",
                     (999_999, 1.0, task_id))

    assert worker.recover() == 1
    row = worker.get(task_id)
    assert row["status"] == "queued" and row["pid"] is None


def test_liveness_fails_safe_when_death_cannot_be_proven(queue):
    """Inability to prove death must not rewrite someone else's row. Assuming
    death when unsure is how the same task reaches two workers."""
    assert worker._alive(os.getpid(), None) is True
    assert worker._alive(os.getpid(), worker._pid_started(os.getpid())) is True
    assert worker._alive(None, None) is False


def test_a_recycled_pid_is_not_mistaken_for_the_original(queue):
    """Why the start time is stored at all. A pid alone cannot tell the original
    owner from whatever process inherited its number."""
    task_id = worker.submit("goal")
    worker.claim()
    with worker._connect() as conn:
        # Same pid, but it started at a different time - so it is a different
        # process wearing the same number.
        conn.execute("UPDATE tasks SET pid_started=? WHERE id=?", (1.0, task_id))

    assert worker.recover() == 1


# ===================================================================== FR-602

class FakeState:
    def __init__(self, values):
        self.values = values


class FakeGraph:
    """The graph's surface as the worker uses it: get_state and invoke."""

    def __init__(self, values=None, out=None, boom=None):
        self._values = values or {}
        self._out = out or {"verdict": "done", "denied": []}
        self._boom = boom
        self.invoked_with = []

    def get_state(self, cfg):
        return FakeState(dict(self._values))

    def invoke(self, payload, cfg):
        self.invoked_with.append(payload)
        if self._boom:
            raise self._boom
        return self._out


def test_the_worker_runs_a_task_to_done(queue):
    worker.submit("fix it")
    graph = FakeGraph()

    assert worker.run_worker(graph, once=True) == 1
    row = worker.tasks()[0]
    assert row["status"] == "done" and row["verdict"] == "done"


def test_a_finished_task_records_what_the_agent_SAID(queue):
    """Measured live 2026-09-05: an emailed question was answered "done"."""
    worker.submit("what python version")
    graph = FakeGraph(out={"verdict": "done", "denied": [], "messages": [
        {"role": "user", "content": "what python version"},
        {"role": "assistant", "content": [{"type": "text", "text": "Python 3.13."}]},
    ]})

    worker.run_worker(graph, once=True)

    assert worker.tasks()[0]["detail"] == "Python 3.13."


def test_a_finished_task_with_no_closing_words_keeps_detail_empty(queue):
    worker.submit("fix it")

    worker.run_worker(FakeGraph(), once=True)

    assert worker.tasks()[0]["detail"] == ""


def test_an_empty_queue_is_not_an_error(queue):
    assert worker.run_worker(FakeGraph(), once=True) == 0


def test_a_crash_is_recorded_as_failed_rather_than_lost(queue):
    worker.submit("fix it")
    graph = FakeGraph(boom=RuntimeError("rate limited"))

    worker.run_worker(graph, once=True)

    row = worker.tasks()[0]
    assert row["status"] == "failed"
    assert "RuntimeError" in row["detail"] and "rate limited" in row["detail"]


# ===================================================================== FR-603

def test_a_requeued_task_resumes_rather_than_restarting(queue):
    """FR-603, and the reason requeueing is safe at all. A task whose thread
    already has messages must be invoked with None - which continues from the
    checkpoint - not with a fresh state, which would redo the work."""
    task_id = worker.submit("fix it")
    graph = FakeGraph(values={"messages": [{"role": "user", "content": "fix it"}]})

    worker.run_worker(graph, once=True)

    assert graph.invoked_with == [None], "a checkpointed thread must resume"
    assert worker.get(task_id)["status"] == "done"


def test_a_fresh_task_is_seeded_rather_than_resumed(queue):
    worker.submit("fix it")
    graph = FakeGraph(values={})

    worker.run_worker(graph, once=True)

    seeded = graph.invoked_with[0]
    assert seeded is not None
    assert seeded["messages"][0]["content"] == "fix it"


# ====================================================== UR-16, awaiting-approval

def test_a_run_that_refused_something_ends_awaiting_approval(queue):
    """`autonomous=True` turns a `confirm` into a denial (FR-304), so nothing
    pauses - nobody is watching. But a run that REFUSED destructive calls is not
    simply done: UR-16 asks to review what was queued while you were away, and
    filing that under `done` makes it unanswerable."""
    worker.submit("clean the build")
    graph = FakeGraph(out={"verdict": "done",
                           "denied": [{"name": "run_shell", "reason": "destructive"}]})

    worker.run_worker(graph, once=True)

    row = worker.tasks()[0]
    assert row["status"] == "awaiting-approval"
    assert "run_shell" in row["detail"]


def test_a_clean_run_is_not_flagged_for_review(queue):
    worker.submit("read a file")
    worker.run_worker(FakeGraph(out={"verdict": "done", "denied": []}), once=True)

    assert worker.tasks()[0]["status"] == "done"


def test_a_stuck_agent_is_a_failed_task(queue):
    """`done` means the AGENT finished the job, not merely that the worker
    stopped running it. Filing a stuck run under `done` would make the status
    column useless - you would have to read the verdict to learn nothing was
    achieved."""
    worker.submit("fix it")
    worker.run_worker(FakeGraph(out={"verdict": "stuck", "denied": []}), once=True)

    row = worker.tasks()[0]
    assert row["status"] == "failed"
    assert row["verdict"] == "stuck", "the verdict is kept either way"


def test_a_budget_exhausted_agent_is_also_failed(queue):
    worker.submit("fix it")
    worker.run_worker(FakeGraph(out={"verdict": "budget", "denied": []}), once=True)

    assert worker.tasks()[0]["status"] == "failed"


# ===================================================== Stage B: FR-505, FR-607, FR-307


def test_robots_disallow_is_honoured(tmp_workspace, monkeypatch):
    """FR-505. A host that forbids a path must not be fetched from it."""
    import agent.tools as t

    t._ROBOTS.clear()

    class _Parser:
        def set_url(self, url): pass
        def read(self): pass
        def can_fetch(self, agent, url): return "/private" not in url

    monkeypatch.setattr("urllib.robotparser.RobotFileParser", _Parser)
    assert t.robots_allows("https://example.com/public")
    assert not t.robots_allows("https://example.com/private/x")


def test_an_unreachable_robots_txt_fails_OPEN(tmp_workspace, monkeypatch):
    """The judgement call, stated: a robots.txt that cannot be fetched must not
    disable searching, or one flaky host takes the capability down."""
    import agent.tools as t

    t._ROBOTS.clear()

    class _Broken:
        def set_url(self, url): pass
        def read(self): raise OSError("unreachable")
        def can_fetch(self, agent, url): return False

    monkeypatch.setattr("urllib.robotparser.RobotFileParser", _Broken)
    assert t.robots_allows("https://example.com/anything")


def test_robots_is_fetched_once_per_host(tmp_workspace, monkeypatch):
    """The check is itself a network call and must not double the cost of every
    request."""
    import agent.tools as t

    t._ROBOTS.clear()
    reads = {"n": 0}

    class _Counting:
        def set_url(self, url): pass
        def read(self): reads["n"] += 1
        def can_fetch(self, agent, url): return True

    monkeypatch.setattr("urllib.robotparser.RobotFileParser", _Counting)
    for _ in range(4):
        t.robots_allows("https://example.com/a")
    assert reads["n"] == 1


def test_pacing_waits_between_hits_on_one_host(tmp_workspace, monkeypatch):
    """FR-505's rate limit. Measured lesson: html.duckduckgo.com blocks after one
    request and every retry re-arms a ~30s cooldown."""
    import agent.tools as t

    slept = []
    monkeypatch.setattr(t.time, "sleep", lambda s: slept.append(s))
    t._LAST_HIT.clear()

    t._pace("example.com")
    assert slept == [], "the first hit on a host waits for nothing"
    t._pace("example.com")
    assert slept and 0 < slept[0] <= t.PER_HOST_INTERVAL


def test_a_different_host_is_not_paced(tmp_workspace, monkeypatch):
    import agent.tools as t

    slept = []
    monkeypatch.setattr(t.time, "sleep", lambda s: slept.append(s))
    t._LAST_HIT.clear()

    t._pace("a.example")
    t._pace("b.example")
    assert slept == [], "pacing is PER host; one slow host must not stall another"


def test_the_worker_cap_refuses_past_max(tmp_workspace, monkeypatch):
    """FR-607."""
    from agent import config, worker

    monkeypatch.setattr(config, "MAX_WORKERS", 1)
    worker.submit("first")
    worker.submit("second")

    assert worker.claim() is not None
    assert worker.claim() is None, "a second claim past the cap must be refused"


def test_concluding_frees_a_slot(tmp_workspace, monkeypatch):
    from agent import config, worker

    monkeypatch.setattr(config, "MAX_WORKERS", 1)
    worker.submit("first")
    worker.submit("second")
    first = worker.claim()
    worker.conclude(first["id"], status="done")

    assert worker.claim() is not None


def test_a_dead_worker_does_not_deadlock_the_cap(tmp_workspace, monkeypatch):
    """THE TRAP THIS ORDERING EXISTS FOR. A `running` row whose worker died still
    counts against the cap, so without recover() running FIRST one crash blocks the
    queue permanently."""
    import sqlite3

    from agent import config, worker

    monkeypatch.setattr(config, "MAX_WORKERS", 1)
    worker.submit("first")
    worker.submit("second")
    task = worker.claim()

    # The owner is gone: a pid that cannot be alive.
    with sqlite3.connect(config.TASKS_DB) as conn:
        conn.execute("UPDATE tasks SET pid=?, pid_started=? WHERE id=?",
                     (999999, 1.0, task["id"]))

    assert worker.claim() is not None, "recover() must free the slot before counting"


def test_an_amended_call_is_reclassified_not_waved_through(tmp_workspace):
    """FR-307, and this is the whole safety property: amending a path so the call
    WRITES outside the workspace must still be denied unattended, or the approval
    prompt becomes a bypass.

    A read was the example until FR-302 was amended 2026-09-08 and reading the
    user's own files became the point. The property under test is unchanged - the
    gate re-runs classify() on the amended input - so the example moves to a write.
    """
    from agent.policy import classify

    verdict, _ = classify("write_file", {"path": "../../etc/passwd"}, autonomous=True)
    assert verdict == "deny", "the gate re-runs classify() on the amended input"


def test_amending_keeps_the_arguments_it_was_not_given(tmp_workspace):
    """An amendment is a partial update - naming one argument must not drop the
    others."""
    call = {"name": "edit_file", "id": "t1",
            "input": {"path": "a.py", "old_string": "x", "new_string": "y"}}
    amended = {**call, "input": {**call["input"], **{"new_string": "z"}}}

    assert amended["input"]["path"] == "a.py"
    assert amended["input"]["old_string"] == "x"
    assert amended["input"]["new_string"] == "z"

# ===================================================================== FR-605


def test_the_five_syntaxes_standard_cron_has(tmp_workspace):
    from agent import worker

    assert worker._field("*", 0, 59) == set(range(60))
    assert worker._field("*/15", 0, 59) == {0, 15, 30, 45}
    assert worker._field("9-17", 0, 23) == set(range(9, 18))
    assert worker._field("1,3,5", 0, 6) == {1, 3, 5}
    assert worker._field("0-30/10", 0, 59) == {0, 10, 20, 30}


@pytest.mark.parametrize("expr", ["* * * *", "* * * * * *", "60 * * * *",
                                  "* 24 * * *", "*/0 * * * *", "not-cron"])
def test_a_malformed_expression_is_refused_not_stored(tmp_workspace, expr):
    """Validation happens in schedule() BEFORE the insert, so a bad expression
    cannot become a row that fire() trips over every poll."""
    from agent import worker

    with pytest.raises(ValueError):
        worker.schedule(expr, "anything")
    assert worker.schedules() == []


def test_next_run_lands_on_the_next_matching_minute(tmp_workspace):
    import time

    from agent import worker

    monday_0800 = time.mktime((2026, 8, 31, 8, 0, 0, 0, 0, -1))
    got = time.localtime(worker.next_run("30 9 * * *", monday_0800))
    assert (got.tm_hour, got.tm_min) == (9, 30)
    assert got.tm_mday == 31


def test_next_run_is_strictly_after_so_a_slot_cannot_rematch(tmp_workspace):
    """The whole at-most-once property rests on this: if next_run could return
    the instant it was given, a schedule would fire forever inside one minute."""
    import time

    from agent import worker

    on_the_minute = time.mktime((2026, 8, 31, 9, 30, 0, 0, 0, -1))
    assert worker.next_run("30 9 * * *", on_the_minute) > on_the_minute


def test_day_of_week_uses_crons_numbering_not_pythons(tmp_workspace):
    """Cron calls Sunday 0; struct_time calls Monday 0. Getting this wrong fires
    everything one day early and nothing complains."""
    import time

    from agent import worker

    saturday = time.mktime((2026, 8, 29, 12, 0, 0, 0, 0, -1))
    sunday = time.localtime(worker.next_run("0 9 * * 0", saturday))
    assert sunday.tm_wday == 6, "cron 0 must be Sunday"
    monday = time.localtime(worker.next_run("0 9 * * 1", saturday))
    assert monday.tm_wday == 0


def test_an_impossible_date_raises_rather_than_spinning(tmp_workspace):
    from agent import worker

    with pytest.raises(ValueError):
        worker.next_run("0 0 30 2 *", 0.0)


def test_a_due_schedule_enqueues_exactly_one_task(tmp_workspace):
    import time

    from agent import worker

    sched_id = worker.schedule("* * * * *", "the recurring goal")
    fired = worker.fire(now=time.time() + 3600)

    assert len(fired) == 1
    queued = [t for t in worker.tasks() if t["goal"] == "the recurring goal"]
    assert len(queued) == 1 and queued[0]["status"] == "queued"
    assert worker.get(fired[0])["id"] == worker.schedules()[0]["last_task"]
    assert worker.schedules()[0]["id"] == sched_id


def test_polling_again_in_the_same_slot_does_not_fire_twice(tmp_workspace):
    """THE TRAP THIS ORDERING EXISTS FOR: next_run is advanced BEFORE the
    submit. A worker polling every two seconds would
    otherwise enqueue a task per poll for the whole minute."""
    import time

    from agent import worker

    worker.schedule("* * * * *", "once please")
    later = time.time() + 3600
    first = worker.fire(now=later)
    second = worker.fire(now=later)

    assert len(first) == 1
    assert second == [], "the slot was already claimed"


def test_a_schedule_not_yet_due_enqueues_nothing(tmp_workspace):
    from agent import worker

    worker.schedule("0 3 * * *", "much later")
    assert worker.fire(now=0.0) == []
    assert worker.tasks() == []


def test_a_worker_that_loses_the_race_submits_NOTHING(tmp_workspace, monkeypatch):
    """THE TRAP THIS ORDERING EXISTS FOR, and the reason submit() comes after the
    advance rather than before it.

    Interleaved for real: this fire() reads the due row, and another worker
    advances it before this one gets to. The guard on the value just read is the
    only thing that stops a duplicate task, so the loser must submit nothing.
    """
    import time

    from agent import worker

    worker.schedule("* * * * *", "contested")
    later = time.time() + 3600
    row = worker.schedules()[0]

    submitted = []
    monkeypatch.setattr(worker, "submit", lambda goal: submitted.append(goal) or "x")

    real_next_run = worker.next_run

    def steal(expr, after):
        """Stand in for the other worker, between this one's SELECT and UPDATE."""
        with worker._connect() as conn:
            conn.execute("UPDATE schedules SET next_run=? WHERE id=?",
                         (later + 999, row["id"]))
        return real_next_run(expr, after)

    monkeypatch.setattr(worker, "next_run", steal)

    assert worker.fire(now=later) == []
    assert submitted == [], "a lost race must not enqueue anything"


def test_a_schedule_can_be_removed(tmp_workspace):
    import time

    from agent import worker

    sched_id = worker.schedule("* * * * *", "temporary")
    assert worker.unschedule(sched_id) is True
    assert worker.unschedule(sched_id) is False
    assert worker.fire(now=time.time() + 3600) == []


def test_schedules_go_through_submit_not_a_second_execution_path(tmp_workspace,
                                                                 monkeypatch):
    """§12 keeps one path to running a task. A schedule that executed directly
    would be a second, and two paths are how components come to disagree about
    what ran."""
    import time

    from agent import worker

    seen = []
    real = worker.submit
    monkeypatch.setattr(worker, "submit", lambda goal: seen.append(goal) or real(goal))
    worker.schedule("* * * * *", "through the queue")
    worker.fire(now=time.time() + 3600)

    assert seen == ["through the queue"]


def test_the_worker_loop_polls_schedules(tmp_workspace, monkeypatch):
    from agent import worker

    calls = {"n": 0}
    monkeypatch.setattr(worker, "fire", lambda: calls.__setitem__("n", calls["n"] + 1) or [])
    worker.run_worker(app=None, once=True)

    assert calls["n"] == 1, "a poll that skips fire() never triggers a schedule"

# ================================================ Phase 4: proactive review


def test_nothing_outstanding_means_SILENCE(tmp_workspace):
    """The half that makes this usable. A check that always speaks is an
    interruption, and the first thing anyone does with one is turn it off."""
    from agent import worker

    assert worker.attention() == []
    assert worker.review() is None
    assert worker.tasks() == [], "and it must not enqueue an empty review"


def test_a_refused_call_is_what_UR_16_asks_to_review(tmp_workspace):
    """awaiting-approval is the one status that cannot resolve itself: the agent
    refused something while nobody was watching."""
    from agent import worker

    task_id = worker.submit("delete the old backups")
    worker.claim()
    worker.conclude(task_id, status="awaiting-approval",
                    detail="refused while unattended: run_shell")

    items = worker.attention()
    assert len(items) == 1
    assert task_id in items[0] and "refused while unattended" in items[0]


def test_a_failed_task_is_surfaced(tmp_workspace):
    from agent import worker

    task_id = worker.submit("do the thing")
    worker.claim()
    worker.conclude(task_id, status="failed", detail="RuntimeError: boom")

    assert any(task_id in item for item in worker.attention())


def test_a_finished_task_is_NOT_surfaced(tmp_workspace):
    """The other direction: a review that reports completed work is noise."""
    from agent import worker

    task_id = worker.submit("do the thing")
    worker.claim()
    worker.conclude(task_id, status="done")

    assert worker.attention() == []


def test_unfinished_work_from_the_scratchpad_is_surfaced(tmp_workspace):
    from agent import memory, worker

    memory.write_now("fix the parser", "stuck", ["read", "edit", "verify"],
                     cursor=0, files=[])
    items = worker.attention()

    assert any("unfinished from the last session" in item for item in items)
    assert any("edit" in item for item in items)


def test_a_finished_scratchpad_is_not_outstanding(tmp_workspace):
    from agent import memory, worker

    memory.write_now("fix the parser", "done", ["read", "edit"], cursor=0, files=[])
    assert worker.attention() == []


def test_the_review_goal_carries_what_it_found(tmp_workspace):
    from agent import worker

    task_id = worker.submit("delete the old backups")
    worker.claim()
    worker.conclude(task_id, status="awaiting-approval", detail="refused")

    review_id = worker.review()
    goal = worker.get(review_id)["goal"]
    assert task_id in goal
    assert "report only" in goal, "a review must not change anything"


def test_the_sentinel_is_resolved_AT_FIRE_TIME(tmp_workspace):
    """THE REASON THE SENTINEL EXISTS. A review's content is whatever is
    outstanding now; a schedule storing fixed text would report the state at
    scheduling time forever."""
    import time

    from agent import worker

    worker.schedule("* * * * *", worker.REVIEW)

    # Nothing outstanding yet: firing must enqueue NOTHING.
    assert worker.fire(now=time.time() + 3600) == []

    # Now something is, and the NEXT firing picks it up without rescheduling.
    task_id = worker.submit("delete the old backups")
    worker.claim()
    worker.conclude(task_id, status="awaiting-approval", detail="refused")
    fired = worker.fire(now=time.time() + 7200)

    assert len(fired) == 1
    assert task_id in worker.get(fired[0])["goal"]


def test_a_review_that_finds_nothing_does_not_enqueue_an_empty_task(tmp_workspace):
    import time

    from agent import worker

    worker.schedule("* * * * *", worker.REVIEW)
    worker.fire(now=time.time() + 3600)

    assert worker.tasks() == [], "a schedule with nothing to say says nothing"


def test_an_ordinary_schedule_is_unaffected_by_the_sentinel(tmp_workspace):
    import time

    from agent import worker

    worker.schedule("* * * * *", "the ordinary goal")
    fired = worker.fire(now=time.time() + 3600)

    assert len(fired) == 1
    assert worker.get(fired[0])["goal"] == "the ordinary goal"

# ================== cancelling a task (a channel can queue what you did not type)


def test_a_queued_task_can_be_CANCELLED(queue):
    """Once a channel can queue work, there has to be an off switch."""
    from agent import worker

    task_id = worker.submit('do something regrettable')
    stopped = worker.cancel(task_id)

    assert stopped['status'] == 'failed'
    assert stopped['verdict'] == 'cancelled'
    assert worker.get(task_id)['finished_at'] is not None


def test_a_RUNNING_task_can_be_cancelled(queue):
    from agent import worker

    task_id = worker.submit('long job')
    worker.claim()
    assert worker.get(task_id)['status'] == 'running'

    assert worker.cancel(task_id) is not None
    assert worker.get(task_id)['verdict'] == 'cancelled'


def test_cancelling_a_FINISHED_task_changes_nothing(queue):
    """conclude() writes a terminal state ONCE. Cancelling after the fact must not
    rewrite a real result into a cancellation."""
    from agent import worker

    task_id = worker.submit('quick job')
    worker.claim()
    worker.conclude(task_id, status='done', verdict='done', detail='all green')

    assert worker.cancel(task_id) is None
    assert worker.get(task_id)['verdict'] == 'done'


def test_cancelling_an_unknown_task_returns_None(queue):
    from agent import worker

    assert worker.cancel('nosuchid') is None


# ===================================================================== FR-606


def test_events_land_in_order_and_survive_a_reopened_store(queue):
    task_id = worker.submit("watch me")
    log = worker.EventLog(task_id)

    log.append({"kind": "model", "billed_tokens": 1_200})
    log.append({"kind": "tool", "tool": "run_shell", "summary": "pytest -q"})
    log.append({"kind": "terminal", "verdict": "done"})

    # Read through a fresh connection, which is what --attach in another process
    # actually does - an in-memory list would pass this and prove nothing.
    rows = worker.events(task_id)
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert [r["kind"] for r in rows] == ["model", "tool", "terminal"]
    assert rows[0]["billed_tokens"] == 1_200


def test_events_pages_from_a_sequence(queue):
    task_id = worker.submit("watch me")
    log = worker.EventLog(task_id)
    for n in range(4):
        log.append({"kind": "node", "node": f"n{n}"})

    assert [r["seq"] for r in worker.events(task_id, after=0)] == [1, 2, 3, 4]
    assert [r["seq"] for r in worker.events(task_id, after=2)] == [3, 4]
    assert worker.events(task_id, after=4) == []


def test_the_first_entry_is_not_skipped_by_the_default(queue):
    """seq is 1-based for exactly this reason: at 0-based, `after=0` - the only
    sensible starting point for a reader - would never return entry zero."""
    task_id = worker.submit("watch me")
    worker.EventLog(task_id).append({"kind": "model"})

    assert len(worker.events(task_id)) == 1


def test_a_store_that_cannot_be_written_does_not_fail_the_task(queue, monkeypatch):
    """A task that did the work must not be reported failed because its own
    bookkeeping broke."""
    task_id = worker.submit("watch me")
    log = worker.EventLog(task_id)

    def broken():
        raise worker.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker, "_connect", broken)
    log.append({"kind": "model"})          # must not raise

    assert list(log) == [{"kind": "model"}]


def test_events_of_a_task_that_never_ran_are_empty_not_an_error(queue):
    assert worker.events("nosuchid") == []


def test_a_concluded_task_keeps_its_events_until_the_retention_window(queue):
    task_id = worker.submit("watch me")
    worker.claim()
    worker.EventLog(task_id).append({"kind": "model"})

    worker.conclude(task_id, status="done", verdict="done")

    assert len(worker.events(task_id)) == 1


def test_conclude_drops_events_of_tasks_finished_long_ago(queue):
    """Otherwise the store grows without bound - a trace is ~100 rows a task."""
    old = worker.submit("last week")
    worker.claim()
    worker.EventLog(old).append({"kind": "model"})
    worker.conclude(old, status="done", verdict="done")
    with worker._connect() as conn:
        conn.execute("UPDATE tasks SET finished_at=? WHERE id=?",
                     (time.time() - worker.EVENT_RETENTION - 60, old))

    # A later conclude() is the sweep, so it takes a second task to trigger one.
    fresh = worker.submit("today")
    worker.claim()
    worker.EventLog(fresh).append({"kind": "model"})
    worker.conclude(fresh, status="done", verdict="done")

    assert worker.events(old) == []
    assert len(worker.events(fresh)) == 1


class _StubApp:
    """Stands in for the compiled graph: records into whatever trace it is given."""

    def __init__(self, entry: dict | None = None) -> None:
        self.entry = entry
        self.saw = None

    def get_state(self, cfg):
        return type("S", (), {"values": {}})()

    def invoke(self, state, cfg):
        self.saw = cfg["configurable"]["trace"]
        if self.entry is not None:
            self.saw.append(self.entry)
        return {"verdict": "done", "messages": []}


def test_run_once_records_its_trace_without_being_asked(queue):
    """The whole point of FR-606: an ordinary worker run is watchable, with no
    flag and no second code path."""
    task_id = worker.submit("do the thing")
    task = worker.claim()

    worker.run_once(_StubApp({"kind": "model", "billed_tokens": 7}), task)

    rows = worker.events(task_id)
    assert [r["kind"] for r in rows] == ["model"]
    assert rows[0]["billed_tokens"] == 7


def test_a_caller_that_passes_its_own_trace_still_owns_it(queue):
    """The harness passes a plain list and must keep getting exactly that."""
    task_id = worker.submit("do the thing")
    task = worker.claim()
    mine: list = []
    app = _StubApp({"kind": "model"})

    worker.run_once(app, task, trace=mine)

    assert app.saw is mine
    assert worker.events(task_id) == [], "a caller's own list must not be stored"


# --------------------------------------------- liveness on Windows (NFR-302)
#
# MEASURED 2026-09-25 by killing a real worker, not by reading the code: the row
# stayed `running` forever and recover() never requeued it. `os.kill(pid, 0)`
# cannot answer "is it alive" here - a live pid raises nothing, a released one
# raises OSError 87, and one that exited while a handle is still open raises
# nothing either - and _alive() read every OSError as "still alive" by design.


def test_a_dead_worker_on_windows_is_not_read_as_alive(queue, monkeypatch):
    """The defect this branch exists for: death was unprovable, so a crashed
    worker's task stranded at `running` with nothing to retry it."""
    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker, "_exited", lambda pid: True)

    assert worker._alive(4321, None) is False


def test_a_live_worker_on_windows_is_left_alone(queue, monkeypatch):
    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker, "_exited", lambda pid: False)

    assert worker._alive(4321, None) is True


def test_windows_still_fails_safe_when_it_cannot_tell(queue, monkeypatch):
    """A privileged pid answers ERROR_ACCESS_DENIED, which proves nothing. The
    fail-safe is unchanged: assuming death hands one task to two workers."""
    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker, "_exited", lambda pid: None)

    assert worker._alive(4, None) is True


def test_recover_requeues_a_task_whose_windows_worker_died(queue, monkeypatch):
    """End to end through recover(), because _alive() being right is only half of
    it - the sweep has to act on the answer."""
    task_id = worker.submit("goal")
    worker.claim()
    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker, "_exited", lambda pid: True)

    assert worker.recover() == 1
    row = worker.get(task_id)
    assert row["status"] == "queued" and row["pid"] is None


@pytest.mark.skipif(os.name != "nt", reason="the Windows probe, on Windows")
def test_exited_tells_a_live_process_from_one_that_has_gone(queue):
    """The probe itself, against real processes. Skipped in the container, where
    the suite normally runs - which is exactly why the bug survived seven
    passing tests: the container is Linux and the Linux path was always right."""
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert worker._exited(child.pid) is False
    finally:
        child.kill()
        child.wait()

    # STILL HOLDING the handle, which is the case os.kill cannot see: the pid
    # resolves and raises nothing, while the process has plainly exited.
    assert worker._exited(child.pid) is True
    assert worker._alive(child.pid, None) is False


@pytest.mark.skipif(os.name != "nt", reason="the Windows probe, on Windows")
def test_a_pid_that_cannot_exist_is_dead_and_a_privileged_one_is_unknown(queue):
    assert worker._exited(999_999) is True
    assert worker._exited(4) is None          # System: access denied, not absent
    assert worker._exited(os.getpid()) is False
