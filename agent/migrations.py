"""Schema migrations for the SQLite stores (Phase A).

Every store was created with `CREATE TABLE IF NOT EXISTS`, which is correct for a
fresh database and silently WRONG for an existing one: it skips the statement
entirely, so a column added later does not appear and the failure surfaces as a
confusing SQL error at some random call site rather than at startup.

That is not theoretical here. `episodes_fts` gained `tokenize='porter'` on
2026-08-31; every memory database written before that keeps the default tokenizer
while the code assumes porter. Two agents on the same commit, different retrieval,
no error anywhere.

A numbered, append-only list that starts at 1.

THE RULE THAT MAKES THIS SAFE, and it is the one the numbering enforces:
a migration is identified by its POSITION and never re-ordered, never edited once
released, and never conditional on what a table currently looks like. Inspecting
the current shape is how two databases at the same version come to differ.

CE-05: nothing runs at import. NFR-602: no API key, no network.
"""
from __future__ import annotations

import sqlite3

# Each entry is one migration, applied in order. The list index plus one IS the
# schema version, so appending is the only legal edit.
#
# A migration must be safe to run against a database that has never seen it, and
# must never run twice - `user_version` guarantees the second, and each statement
# below has to guarantee the first on its own.
MEMORY: list[tuple[str, list[str]]] = [
    (
        # v1: the tables as they stood before migrations existed. Written with IF
        # NOT EXISTS so an existing database reaches v1 without doing anything,
        # which is what makes adopting this safe on a store already in use.
        "baseline",
        [
            """CREATE TABLE IF NOT EXISTS episodes (
                   id        INTEGER PRIMARY KEY,
                   thread_id TEXT NOT NULL,
                   at        REAL NOT NULL,
                   goal      TEXT NOT NULL,
                   verdict   TEXT,
                   answer    TEXT,
                   files     TEXT,
                   commands  TEXT
               )""",
        ],
    ),
    (
        # v2: porter stemming on the FTS index. An existing episodes_fts was built
        # with the default tokenizer and IF NOT EXISTS never touches it, so the
        # table is DROPPED and rebuilt from `episodes`, which is the content table
        # and holds everything. Nothing is lost; an external-content FTS5 index is
        # derived data by definition.
        "fts5 with porter stemming",
        [
            "DROP TABLE IF EXISTS episodes_fts",
            """CREATE VIRTUAL TABLE episodes_fts
                   USING fts5(goal, answer, files, commands, content='episodes',
                              content_rowid='id', tokenize='porter')""",
            """INSERT INTO episodes_fts (rowid, goal, answer, files, commands)
                   SELECT id, goal, answer,
                          COALESCE(files, ''), COALESCE(commands, '')
                   FROM episodes""",
        ],
    ),
    (
        # v3: one embedding per episode (FR-408), added 2026-09-04. A separate
        # table rather than a column on `episodes`: a vector is not a field
        # anyone SELECTs beside the goal, and keeping it out means every
        # existing query is untouched.
        "episode embeddings",
        [
            """CREATE TABLE IF NOT EXISTS episode_vectors (
                   episode_id INTEGER PRIMARY KEY
                       REFERENCES episodes(id) ON DELETE CASCADE,
                   dims       INTEGER NOT NULL,
                   vector     BLOB NOT NULL
               )""",
        ],
    ),
    (
        # v4: which skill was open when a run FAILED (Phase R). One row per skill
        # and not a log - this is state the next successful run consumes and
        # clears, so a skill that keeps failing is one suspect, not a pile.
        # MEMORY and not TASKS: it is consumed by skills.extract beside the
        # episodes, and shares their lifetime.
        "skill_failures",
        [
            """CREATE TABLE IF NOT EXISTS skill_failures (
                   skill    TEXT PRIMARY KEY,
                   goal     TEXT NOT NULL,
                   verdict  TEXT NOT NULL,
                   at       REAL NOT NULL
               )""",
        ],
    ),
    (
        # v5: and dropped again 2026-09-23 with Phase R. v4 STAYS - apply()
        # indexes plan[version:] by position, so removing it would renumber
        # every later migration and re-run them on databases already past it.
        "drop_skill_failures",
        ["DROP TABLE IF EXISTS skill_failures"],
    ),
]

TASKS: list[tuple[str, list[str]]] = [
    (
        "baseline",
        [
            """CREATE TABLE IF NOT EXISTS tasks (
                   id           TEXT PRIMARY KEY,
                   goal         TEXT NOT NULL,
                   status       TEXT NOT NULL CHECK(status IN
                                  ('queued','running','awaiting-approval','done','failed')),
                   verdict      TEXT,
                   detail       TEXT,
                   submitted_at REAL NOT NULL,
                   started_at   REAL,
                   finished_at  REAL,
                   pid          INTEGER,
                   pid_started  REAL
               )""",
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, submitted_at)",
        ],
    ),
    (
        # v2: cron schedules (FR-605), added 2026-08-31.
        "schedules",
        [
            """CREATE TABLE IF NOT EXISTS schedules (
                   id         TEXT PRIMARY KEY,
                   cron       TEXT NOT NULL,
                   goal       TEXT NOT NULL,
                   next_run   REAL NOT NULL,
                   created_at REAL NOT NULL,
                   last_fired REAL,
                   last_task  TEXT
               )""",
        ],
    ),
    (
        # v3: a task can arrive from a chat and owes it a reply, added 2026-09-02.
        #
        # An outbound reply is a ROW WITH A STATE, not a function call: a send
        # that fails must not lose the message, and a poison row
        # must not spin. `delivered_at` is that ledger at the size this project
        # needs: NULL means still owed, and `attempts` bounds the retry.
        "channel",
        [
            # reply_to is WHERE to answer (an address), reply_ref is WHAT to
            # answer (the Message-ID), so the reply threads in the mail client
            # instead of arriving as a fresh conversation.
            "ALTER TABLE tasks ADD COLUMN reply_to TEXT",
            "ALTER TABLE tasks ADD COLUMN reply_ref TEXT",
            "ALTER TABLE tasks ADD COLUMN subject TEXT",
            "ALTER TABLE tasks ADD COLUMN delivered_at REAL",
            "ALTER TABLE tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            # The high-water mark of what has been SEEN. One row, so a restart
            # does not replay the inbox - which would re-run finished work and,
            # on a first run, answer every message you have ever received.
            """CREATE TABLE IF NOT EXISTS channel_state (
                   key   TEXT PRIMARY KEY,
                   value TEXT NOT NULL
               )""",
        ],
    ),
    (
        # v4: the USER's todo list, added 2026-09-08. `tasks` above is the
        # agent's own queue; this is what the person is doing, which nothing
        # tracked. Same database because it is the same lifetime and the same
        # AGENT_HOME - a second file would buy isolation nothing needs.
        "todos",
        [
            """CREATE TABLE IF NOT EXISTS todos (
                   item     TEXT PRIMARY KEY,
                   status   TEXT NOT NULL CHECK(status IN ('open','done')),
                   added_at REAL NOT NULL,
                   done_at  REAL
               )""",
        ],
    ),
]


def apply(conn: sqlite3.Connection, plan: list[tuple[str, list[str]]]) -> int:
    """Bring `conn` up to the latest version in `plan`. Returns the version.

    Each migration runs inside its own EXPLICIT transaction, so a statement that
    fails rolls back everything the migration did and leaves `user_version` where
    it was. The next attempt then retries the whole migration from the start,
    which only works because nothing it did survived.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    # EXPLICIT transactions, not `with conn`. Python's sqlite3 auto-begins only for
    # INSERT/UPDATE/DELETE/REPLACE, so a CREATE TABLE runs outside any transaction
    # and commits immediately - a migration that creates two tables and fails on
    # the second left the first behind, and since user_version correctly did not
    # advance, every retry then failed forever on "table already exists".
    previous, conn.isolation_level = conn.isolation_level, None
    try:
        for index, (_name, statements) in enumerate(plan[version:], start=version):
            conn.execute("BEGIN")
            try:
                for statement in statements:
                    conn.execute(statement)
                # Inside the same transaction as the statements it describes.
                # PRAGMA takes no parameter binding, and index+1 is an int from
                # enumerate over a module constant, never user input.
                conn.execute(f"PRAGMA user_version = {index + 1}")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            version = index + 1
    finally:
        conn.isolation_level = previous
    return version
