"""Schema migrations (Phase A).

Its own file because §12's three named test files cover policy, context and
reflect, and this covers neither a node nor a tool - it covers the thing that
happens before any of them can read a row. Stated here as the eighth deviation,
to the same standard as the seven before it.
"""
import sqlite3

import pytest

from agent import config, memory, migrations, worker


def test_a_fresh_database_reaches_the_latest_version(tmp_workspace):
    conn = memory._connect()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(
            migrations.MEMORY)
    finally:
        conn.close()


def test_opening_twice_is_a_no_op(tmp_workspace):
    """A migration that re-runs is a migration that can destroy data - v2 drops
    and rebuilds the FTS index."""
    memory.write_episode("t1", "the goal", "done", "the answer", [], [])
    memory._connect().close()

    assert len(memory.search("goal")) == 1


def test_a_database_at_version_zero_is_UPGRADED_not_recreated(tmp_workspace):
    """THE CASE THIS EXISTS FOR, and it is not theoretical. Every memory database
    written before 2026-08-31 has episodes_fts WITHOUT porter, because
    CREATE VIRTUAL TABLE IF NOT EXISTS skips an existing table entirely - so the
    code assumed porter and the store did not, with no error anywhere.
    """
    # Build the pre-migration schema by hand, exactly as it was.
    config.MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(str(config.MEMORY_DB))
    legacy.executescript(
        """CREATE TABLE episodes (
               id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL, at REAL NOT NULL,
               goal TEXT NOT NULL, verdict TEXT, answer TEXT, files TEXT,
               commands TEXT);
           CREATE VIRTUAL TABLE episodes_fts
               USING fts5(goal, answer, files, commands, content='episodes',
                          content_rowid='id');""")
    legacy.execute(
        "INSERT INTO episodes (thread_id, at, goal, verdict, answer, files,"
        " commands) VALUES ('t1', 1.0, 'the quartzite deploy key', 'done', 'kx',"
        " '[]', '[]')")
    legacy.execute(
        "INSERT INTO episodes_fts (rowid, goal, answer, files, commands)"
        " SELECT id, goal, answer, files, commands FROM episodes")
    legacy.commit()
    assert legacy.execute("PRAGMA user_version").fetchone()[0] == 0
    legacy.close()

    conn = memory._connect()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(
            migrations.MEMORY)
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='episodes_fts'").fetchone()[0]
        assert "porter" in schema
        # The row survived. An external-content FTS index is derived data, so the
        # rebuild reads it back out of `episodes`.
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
    finally:
        conn.close()

    assert memory.search("quartzite deploy"), "the rebuilt index must still match"


def test_the_tasks_store_migrates_too(tmp_workspace):
    worker.submit("a goal")
    conn = worker._connect()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(
            migrations.TASKS)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"tasks", "schedules"} <= names
    finally:
        conn.close()


def test_a_failing_migration_does_not_advance_the_version(tmp_workspace):
    """Each migration is one transaction. A half-applied migration that recorded
    its version would be skipped forever, leaving the store permanently wrong."""
    conn = sqlite3.connect(":memory:")
    plan = [("good", ["CREATE TABLE a (x INTEGER)"]),
            ("bad", ["CREATE TABLE b (y INTEGER)", "THIS IS NOT SQL"])]

    with pytest.raises(sqlite3.OperationalError):
        migrations.apply(conn, plan)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" in names
    assert "b" not in names, "the failed migration must roll back entirely"
    conn.close()


def test_a_retried_migration_starts_from_the_beginning(tmp_workspace):
    """Following from the above: the next attempt re-runs the WHOLE migration
    rather than resuming half way through it."""
    conn = sqlite3.connect(":memory:")
    plan = [("good", ["CREATE TABLE a (x INTEGER)"])]

    assert migrations.apply(conn, plan) == 1
    assert migrations.apply(conn, plan) == 1, "already applied, so nothing re-runs"
    conn.close()


def test_migrations_apply_in_order_from_any_starting_version(tmp_workspace):
    conn = sqlite3.connect(":memory:")
    plan = [("one", ["CREATE TABLE t (x INTEGER)"]),
            ("two", ["ALTER TABLE t ADD COLUMN y INTEGER"]),
            ("three", ["ALTER TABLE t ADD COLUMN z INTEGER"])]

    conn.executescript("CREATE TABLE t (x INTEGER); PRAGMA user_version = 1;")
    assert migrations.apply(conn, plan) == 3

    cols = {r[1] for r in conn.execute("PRAGMA table_info(t)")}
    assert cols == {"x", "y", "z"}
    conn.close()


def test_a_database_at_v4_loses_the_phase_r_table(tmp_workspace):
    """Phase R was deleted 2026-09-23 and its store with it. v4 STAYS in the plan
    - apply() indexes plan[version:] by position, so removing it would renumber
    every later migration and re-run them on a database already past it. v5 drops
    the table instead, and a store that has been carrying rows must come up clean.
    """
    import sqlite3

    from agent import config, memory, migrations

    config.MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(str(config.MEMORY_DB))
    migrations.apply(old, migrations.MEMORY[:4])          # the world before v5
    old.execute("INSERT INTO skill_failures (skill, goal, verdict, at) "
                "VALUES ('deploy-guide', 'ship it', 'stuck', 1.0)")
    old.commit()
    assert old.execute("PRAGMA user_version").fetchone()[0] == 4
    old.close()

    conn = memory._connect()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(
            migrations.MEMORY)
        gone = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='skill_failures'").fetchone()
        assert gone is None, "the suspect store survived the migration"
    finally:
        conn.close()


def test_the_version_is_the_list_length(tmp_workspace):
    """The index IS the version, so appending is the only legal edit. A migration
    inserted in the middle would renumber every one after it and silently skip
    the difference on every existing database."""
    conn = sqlite3.connect(":memory:")
    assert migrations.apply(conn, migrations.MEMORY) == len(migrations.MEMORY)
    conn.close()
