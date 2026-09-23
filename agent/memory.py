"""Episodic memory (FR-407) and the durable profile (FR-406).

Both `[S]`, and §9 puts `[S]` out of scope "until the `[M]` set passes evaluation".
It has — Definition of Done 9/9 — so this is scope arriving on schedule, not a
liberty taken.

Two stores, because they answer two different questions:

  episodes   what happened in past sessions, searched by keyword. Written
             automatically at `finish`, from state and trace that already exist.
  AGENT.md   what is durably true about the user, written by the agent through
             the `remember` tool. §12 names the file and says "written by the
             agent"; a tool is how that stays true without `finish` calling a model.

**Keyword search only, and now for a measured reason rather than an untested**
**gate.** FR-408 asked for semantic retrieval only on "a measured shortfall in
keyword recall". Both halves were measured (eval/CHANGELOG.md): recall@3 was
2/6, and the shortfall was in the QUERY - `_terms` OR-ed every word, so a goal
matching eight corpus-wide words outranked one matching two rare ones. Fixing
that alone reached 5/6, free.

A dense lane over bge-small was then built and reverted: fused it scored 5/6,
the same score with the same single miss, for 450 MB of image.

CE-05: nothing here runs at import, and the database is created on first use.
NFR-602: every function below is testable with no API key and no network.
"""
from __future__ import annotations

import json
import sqlite3
import time

from agent import config, migrations, policy

# Names this module put into policy.RISK, so deactivate() removes exactly
# those and cannot strip a built-in's classification.
_REGISTERED: list[str] = []

def _connect() -> sqlite3.Connection:
    """Open the store, creating or upgrading it. Never at import (CE-05).

    Migrated rather than re-declared: CREATE TABLE IF NOT EXISTS is correct for a
    fresh database and silently wrong for an existing one. Measured - every memory
    database written before 2026-08-31 still carries the default FTS tokenizer
    while this code assumes porter.
    """
    config.MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.MEMORY_DB))
    conn.row_factory = sqlite3.Row
    migrations.apply(conn, migrations.MEMORY)
    return conn


def write_episode(thread_id: str, goal: str, verdict: str | None, answer: str,
                  files: list[str], commands: list[str]) -> int:
    """Record one finished session. Returns its id.

    Everything stored here is already in `state["messages"]` and the trace, so this
    needs NO model call — which is what keeps `finish` a deterministic node and
    leaves `act` the only node that touches a model. A model-written summary was
    considered and rejected on exactly that ground.

    §4.3's retention list — decisions made, files touched, commands that worked,
    errors hit, artifact paths — was written for compaction and is the right list
    here too. The goal and the final answer are added because THIS project's memory
    has to answer "what did the user tell me", and the user's words are the goal.
    """
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO episodes (thread_id, at, goal, verdict, answer, files,"
                " commands) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (thread_id, time.time(), goal, verdict, answer,
                 json.dumps(files), json.dumps(commands)))
            rowid = cur.lastrowid
            # An external-content FTS5 table is not populated by the INSERT above.
            conn.execute(
                "INSERT INTO episodes_fts (rowid, goal, answer, files, commands)"
                " VALUES (?, ?, ?, ?, ?)",
                (rowid, goal, answer, " ".join(files), " ".join(commands)))
            # The GOAL alone, matching what search() matches on: an embedding of
            # the whole row drifts toward whatever the answer happened to say.
            vectors = embed([goal]) if config.SEMANTIC_RECALL else None
            if vectors is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO episode_vectors (episode_id, dims,"
                    " vector) VALUES (?, ?, ?)",
                    (rowid, vectors.shape[1],
                     vectors[0].astype('float32').tobytes()))
        return rowid
    finally:
        conn.close()


def _words(text: str) -> list[str]:
    """Alphanumeric words over two characters, lowercased, in order."""
    return [w for w in "".join(c if c.isalnum() else " " for c in text).lower().split()
            if len(w) > 2]


def _document_frequency() -> dict:
    """How many episodes each word appears in. Cheap at this scale, and the
    thing that makes a query discriminate: a word in most episodes carries no
    information about which one is meant."""
    df: dict = {}
    total = 0
    conn = _connect()
    try:
        rows = conn.execute("SELECT goal, answer FROM episodes").fetchall()
    finally:
        conn.close()
    for row in rows:
        total += 1
        for word in set(_words(f'{row["goal"]} {row["answer"] or ""}')):
            df[word] = df.get(word, 0) + 1
    return {"df": df, "n": total}


def _terms(text: str, stats: dict | None = None) -> str:
    """An FTS5 MATCH query from free text, weighted by how rare each word is.

    Measured: OR-ing every word scored recall@3 of 2/6 on a 36-goal corpus,
    because a goal matching eight corpus-wide words - write, file, workspace,
    called - outranked the one matching two rare ones. Keeping only the rarest
    two scored 5/6.

    User text is also full of characters FTS5 treats as syntax, so every word is
    requoted as a literal and OR-ed: a partial overlap returns something.
    """
    words = list(dict.fromkeys(_words(text)))
    if stats and stats["n"]:
        df = stats["df"]
        total = stats["n"]
        # Only words the corpus actually contains can rank anything. Sorting the
        # rest by rarity puts df == 0 FIRST - terms that match nothing - which
        # returned an empty result on any corpus small enough that every real
        # term appears everywhere. Found by the offline suite, not by the
        # 36-goal measurement, where every target word had df >= 1.
        present = [w for w in words if df.get(w, 0) > 0]
        if present:
            # A word in EVERY episode discriminates nothing, but dropping it is
            # only safe while something else survives.
            discriminating = [w for w in present if df[w] < total] or present
            words = sorted(discriminating, key=lambda w: df[w])[:QUERY_TERMS]
    return " OR ".join(f'"{w}"' for w in words[:40])


# How many of the rarest query words survive into the MATCH. Swept once the
# df == 0 defect was fixed: 2 and 3 both score 5/6, 1 and 4+ score 4/6.
QUERY_TERMS = 2


_SESSION = None
_TOKENIZER = None


def _embedder():
    """The ONNX session and tokenizer, loaded once. None when unavailable.

    Lazy because CE-05 forbids module-level I/O and because a 23 MB model must
    not be loaded by a run that never retrieves. Failure returns None rather
    than raising: no memory is a worse session, a crash is a lost one - the same
    rule search() already applies to a malformed MATCH.
    """
    global _SESSION, _TOKENIZER
    if _SESSION is not None:
        return _SESSION, _TOKENIZER
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model = config.EMBED_MODEL / "model.onnx"
        vocab = config.EMBED_MODEL / "tokenizer.json"
        if not model.is_file() or not vocab.is_file():
            return None, None
        _TOKENIZER = Tokenizer.from_file(str(vocab))
        _TOKENIZER.enable_truncation(max_length=256)
        _SESSION = ort.InferenceSession(
            str(model), providers=["CPUExecutionProvider"])
        return _SESSION, _TOKENIZER
    except Exception:
        return None, None


def embed(texts: list[str]):
    """Unit-normalised mean-pooled embeddings, or None when the model is absent."""
    session, tok = _embedder()
    if session is None:
        return None
    import numpy as np

    out = []
    for start in range(0, len(texts), 32):
        batch = texts[start:start + 32]
        encoded = tok.encode_batch(batch)
        width = max(len(e.ids) for e in encoded)
        ids = np.zeros((len(batch), width), dtype=np.int64)
        mask = np.zeros((len(batch), width), dtype=np.int64)
        for row, e in enumerate(encoded):
            ids[row, :len(e.ids)] = e.ids
            mask[row, :len(e.attention_mask)] = e.attention_mask
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in {i.name for i in session.get_inputs()}:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = session.run(None, feeds)[0]
        weights = mask[..., None].astype(hidden.dtype)
        pooled = (hidden * weights).sum(1) / np.clip(weights.sum(1), 1e-9, None)
        pooled /= np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
        out.append(pooled)
    return np.vstack(out)


def semantic(query: str, limit: int) -> list[int]:
    """Episode ids by cosine similarity to `query`, best first.

    Empty when the model is missing or nothing has been embedded, which is what
    keeps this additive: search() falls back to the keyword ranking it has
    always had.
    """
    if not config.SEMANTIC_RECALL:
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT episode_id, vector FROM episode_vectors").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    if not rows:
        return []

    vectors = embed([query])
    if vectors is None:
        return []
    import numpy as np

    ids = [r["episode_id"] for r in rows]
    matrix = np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
    order = np.argsort(-(matrix @ vectors[0]))
    return [ids[i] for i in order[:limit]]


def search(query: str, limit: int | None = None,
           semantic_lane: bool = True) -> list[dict]:
    """Past episodes matching `query`, most relevant first. Keyword only.

    Stale episodes are DOWN-RANKED, not dropped: past MEMORY_STALE_DAYS a row
    keeps competing at MEMORY_STALE_DECAY of its score. There is no reinforcement
    shield, unlike the design this follows - we record when an episode was
    WRITTEN and never when it was last USED, so there is no signal to shield on.
    Adding one means a new column and a write on every retrieval.

    A dense lane over bge-small embeddings was built, measured and REVERTED. On
    the six recall pairs it scored 5/6 fused and 3/6 alone, against 5/6 for this
    function on its own - the same score with the same single miss, at every
    depth from 5 to 35. It cost 450 MB of image for nothing. The four-channel
    shape it came from scored 1/6. eval/CHANGELOG.md carries the ablation.

    What DID move the number was the query, not the index: see `_terms`.
    """
    terms = _terms(query, _document_frequency())
    if not terms:
        # No corpus word at all is precisely the case the dense lane exists for.
        return _fuse([], query,
                     limit if limit is not None else config.MEMORY_EPISODES,
                     semantic_lane)
    conn = _connect()
    try:
        rows = conn.execute(
            # MATCHED ON `goal` ALONE, not the whole row. Measured: matching every
            # column scores 4/6 against 5/6, because a query's rare words turn up
            # in some other episode's answer or command list and outrank the
            # episode whose GOAL is the thing being recalled.
            "SELECT e.* FROM episodes_fts f JOIN episodes e ON e.id = f.rowid"
            " WHERE f.goal MATCH ?"
            # Staleness, applied to the SCORE rather than by filtering. bm25 is
            # negative and more negative is better, so multiplying a stale row by
            # a fraction moves it toward zero and down the list. A filter would
            # hide the only episode that answers a question nobody asked recently.
            " ORDER BY bm25(episodes_fts) *"
            "   (CASE WHEN ? - e.at > ? THEN ? ELSE 1.0 END), e.at DESC"
            " LIMIT ?",
            (terms, time.time(), config.MEMORY_STALE_DAYS * 86400,
             config.MEMORY_STALE_DECAY,
             config.SEMANTIC_DEPTH)).fetchall()
        return _fuse([dict(r) for r in rows], query,
                     limit if limit is not None else config.MEMORY_EPISODES,
                     semantic_lane)
    except sqlite3.OperationalError:
        # A malformed MATCH must never take down the run. No memory is a worse
        # session; a crash is a lost one.
        return []
    finally:
        conn.close()

def _fuse(keyword_rows: list[dict], query: str, limit: int,
          use_dense: bool = True) -> list[dict]:
    """Dense ranking first, then keyword rows it did not already offer.

    NOT reciprocal-rank fusion, and the difference is measured. On
    eval/fixtures/recall-corpus.jsonl, recall@3:

        keyword only   0/40 paraphrase, 10/10 overlapping
        dense only    23/40 paraphrase, 10/10 overlapping
        RRF           12/40 paraphrase, 10/10 overlapping
        this         23/40 paraphrase, 10/10 overlapping

    RRF costs eleven pairs because a lane scoring 0/40 still offers twenty rows
    and each takes a slot. Order beats weighting here: a lane with no signal
    cannot displace one that has any, and keyword still contributes everything
    dense missed - which is why it is kept rather than deleted.
    """
    if not use_dense:
        return keyword_rows[:limit]
    dense_ids = semantic(query, config.SEMANTIC_DEPTH)
    if not dense_ids:
        return keyword_rows[:limit]

    seen = {row["id"]: row for row in keyword_rows}
    missing = [i for i in dense_ids if i not in seen]
    if missing:
        conn = _connect()
        try:
            marks = ",".join("?" * len(missing))
            for row in conn.execute(
                    f"SELECT * FROM episodes WHERE id IN ({marks})", missing):
                seen[row["id"]] = dict(row)
        finally:
            conn.close()

    out = [seen[i] for i in dense_ids if i in seen]
    ranked = {row["id"] for row in out}
    out.extend(row for row in keyword_rows if row["id"] not in ranked)
    return out[:limit]

def profile() -> str:
    """The durable profile, or empty when nothing has been recorded."""
    if not config.PROFILE.exists():
        return ""
    return config.PROFILE.read_text(encoding="utf-8", errors="replace").strip()


def now() -> str:
    """The working scratchpad, or empty when nothing has been recorded."""
    if not config.NOW.exists():
        return ""
    return config.NOW.read_text(encoding="utf-8", errors="replace").strip()


def write_now(goal: str, verdict: str | None, plan: list[str], cursor: int,
              files: list[str]) -> str:
    """Record where this session got to. Overwrites; returns what was written.

    WRITTEN BY A RULE, never requested from the model. Everything here is already
    in state, so this needs no decision and no model call - which is the whole
    reason it will actually happen. The counter-example is in this repo: `learn`
    asked the agent to record something and was called 0 times in 15 sessions,
    while deterministic episode injection went 0/18 to 15/18.

    The unfinished half is the point. A run that ends `stuck` at turn 30 currently
    tells the next session nothing about how far it got, so a resumed or scheduled
    task re-derives it.
    """
    lines = [f"Last session was asked: {' '.join(str(goal).split())[:300]}",
             f"It ended: {verdict or 'no verdict'}"]
    if plan:
        step = plan[cursor] if 0 <= cursor < len(plan) else plan[-1]
        lines.append(f"Reached step {min(cursor + 1, len(plan))} of {len(plan)}:"
                     f" {step}")
        remaining = plan[cursor + 1:]
        if remaining and verdict != "done":
            lines.append("Still to do: " + "; ".join(remaining[:5]))
    if files:
        lines.append("Files touched: " + ", ".join(sorted(files)[:10]))

    body = "# What I was last doing" + chr(10) * 2 + chr(10).join(lines) + chr(10)
    config.NOW.parent.mkdir(parents=True, exist_ok=True)
    config.NOW.write_text(body, encoding="utf-8", newline=chr(10))
    return body


def remember(note: str) -> str:
    """Append a durable fact about the user to AGENT.md.

    Appended, never rewritten: a rewrite needs judgement about what to drop, and
    that judgement would need a model call. Deduplicated on the exact line so an
    agent told the same thing twice does not grow the file forever.
    """
    note = " ".join(str(note).split())
    if not note:
        raise ValueError("nothing to remember - pass the fact as a short sentence.")
    existing = profile()
    if note in existing.splitlines():
        return f"already recorded: {note}"
    config.PROFILE.parent.mkdir(parents=True, exist_ok=True)
    body = (existing + "\n" if existing else "# What I know about you\n\n") + note + "\n"
    config.PROFILE.write_text(body, encoding="utf-8", newline="\n")
    return f"recorded: {note}"


# How people actually state a standing instruction. Fitted to the six recall
# fixtures AND checked against their task halves: 6/6 teach sentences matched,
# 0/6 task sentences did. Kept narrow on purpose - the profile is system-prompt
# rent, so a false positive is paid for on every turn of every later session.
DURABLE = (
    "standing rule", "from now on", "every time", "without being asked",
    "note that down", "note this down", "remember that", "remember it",
    "remember this", "keep that", "keep this", "i prefer", "i want you to",
    "my convention", "house rule", "always", "never",
)

_SENTENCE_END = (". ", "! ", "? ", chr(10))


def _sentences(text: str) -> list[str]:
    """Split on terminators, keeping it dumb: a regex here would be a parser."""
    out, buf = [], text
    for mark in _SENTENCE_END:
        buf = buf.replace(mark, chr(1))
    for part in buf.split(chr(1)):
        part = " ".join(part.split())
        if part:
            out.append(part)
    return out


def distil(messages: list[dict], skip: str = "") -> list[str]:
    """Write standing instructions the user stated into AGENT.md. Returns them.

    `remember` is a TOOL, so it is written only when the model elects to write
    it - and this project has measured that shape failing three times. This
    decides by rule from the user's own words, which is what took skills from
    3.3% to 11/11.

    Only `str` user content is read, which excludes tool results (they arrive as
    a list) and `skip` excludes the cap-summary request the loop injects as the
    user. Never raises: it runs at the end of a session that may already have
    succeeded.
    """
    if not config.PROFILE_DISTIL:
        return []
    written: list[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str) or (skip and content.strip() == skip.strip()):
            continue
        for sentence in _sentences(content):
            if len(written) >= config.DISTIL_MAX_FACTS:
                return written
            low = sentence.lower()
            if not any(marker in low for marker in DURABLE):
                continue
            try:
                note = sentence[:config.DISTIL_MAX_CHARS]
                if remember(note).startswith("recorded"):
                    written.append(note)
            except (ValueError, OSError):
                # A bookkeeping failure must not turn a passing run into a
                # crashed one.
                continue
    return written


def context_for(goal: str) -> str:
    """Profile plus relevant past sessions, as text for the system prompt.

    Injected into the SYSTEM PROMPT rather than appended to the message list. The
    difference is not cosmetic: a message appended per turn would add a fresh copy
    every turn and grow quadratically, while the system prompt is one fixed cost per
    request. On a provider returning cache_read_tokens of 0 that fixed cost is still
    paid every turn, which is why it is capped rather than merely bounded.
    """
    if not config.MEMORY_ENABLED:
        return ""
    parts = []
    who = profile()
    if who:
        parts.append(who)
    # Ahead of the episodes: what the last session was doing outranks what some
    # earlier session concluded, and the cap below truncates the TAIL.
    current = now()
    if current:
        parts.append(current)
    for row in search(goal, semantic_lane=config.SEMANTIC_RECALL):
        commands = json.loads(row["commands"] or "[]")
        line = f'- earlier you were asked: "{row["goal"]}"'
        if row["answer"]:
            line += f'\n  you concluded: {row["answer"]}'
        if commands:
            line += f'\n  commands that worked: {"; ".join(commands[:3])}'
        parts.append(line)
    if not parts:
        return ""
    body = "\n\n".join(parts)[:config.MEMORY_INJECT_CHARS]
    return ("# What you remember\n\n"
            "From earlier sessions with this user. Treat it as established fact and "
            "act on it without asking again.\n\n" + body)


# ------------------------------------------------------------------ the tool

REMEMBER_SCHEMA = {
    "name": "remember",
    "description": (
        "Save a durable fact about the user or how they work, so future sessions "
        "know it without being told again. Use it for standing preferences and "
        "stable facts, not for details of the task in hand."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "note": {"type": "string",
                     "description": "One short sentence, in the third person."},
        },
        "required": ["note"],
    },
}


def activate() -> list[str]:
    """Register the memory tool for this run. A no-op when memory is off."""
    if not config.MEMORY_ENABLED:
        return []
    if "remember" not in _REGISTERED:
        policy.register("remember", "write")
        _REGISTERED.append("remember")
    return list(_REGISTERED)


def deactivate() -> None:
    """Remove exactly what activate() registered. Safe to call unconditionally."""
    while _REGISTERED:
        policy.RISK.pop(_REGISTERED.pop(), None)


def tools() -> dict[str, dict]:
    """The memory tools active for this run. Empty when memory is off."""
    if not config.MEMORY_ENABLED or "remember" not in _REGISTERED:
        return {}
    return {"remember": {"fn": remember, "schema": REMEMBER_SCHEMA, "risk": "write"}}
