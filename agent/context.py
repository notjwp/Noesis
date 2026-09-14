"""shrink() — bound every tool result on the way into context (FR-401/402).

Returns a plain string, not a tuple: the spill path belongs inside the returned
text, where the model can act on it. That is the only place it was ever useful.

CE-05: the artifacts directory is created at call time, never at import.
"""
import re
import json
import os
from hashlib import sha256

from agent import config, secrets

# NFR-203: secrets never enter model context. Matches VALUES, not env-var
# names - the agent echoes a key far more often than it names the variable.
SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD",
                   "_CREDENTIALS")

# Below this length a value is likelier to be a coincidence than a secret: a
# two-character token would rewrite half of any English output.
MIN_SECRET_CHARS = 8


def redact(text: str) -> str:
    """Replace secret-shaped environment values with a named marker.

    Named rather than blanked. `[redacted:NVIDIA_API_KEY]` tells the model a
    credential was there and which one, so it can reason about what it read; a
    bare row of asterisks looks like data and gets copied into the next command.
    """
    for name, value in os.environ.items():
        if len(value) >= MIN_SECRET_CHARS and name.upper().endswith(SECRET_SUFFIXES):
            text = text.replace(value, f"[redacted:{name}]")
    # The environment covers OUR credentials and nothing else. A workspace .env,
    # an AWS key in source, a DSN with a password - all reached the model verbatim
    # until this was added. See agent/secrets.py for why it matches SHAPES only.
    return secrets.scrub(text)


# Lone surrogates: invalid in UTF-8, and they crash json.dumps() inside the SDK.
_SURROGATE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")


def shrink(tool: str, text: str) -> str:
    """Cap `text` for `tool`, spilling the full output to disk when it overflows."""
    # Before the cap AND before the spill: the artifact is readable with read_file,
    # so redacting only the returned string would leave the secret sitting on disk
    # inside the workspace, one tool call away.
    text = redact(text)
    # Files are read with surrogateescape so a non-UTF-8 byte survives a
    # read-edit-write round trip. Those surrogates must not reach the provider:
    # they are invalid in UTF-8 and crash json.dumps inside the SDK. Sanitised
    # HERE because shrink() is the one place every tool result passes through.
    text = _SURROGATE.sub(chr(0xFFFD), text)
    cap = min(config.TOOL_CAPS.get(tool, config.MAX_RESULT_CHARS), config.MAX_RESULT_CHARS)
    if len(text) <= cap:
        return text

    config.ARTIFACTS.mkdir(parents=True, exist_ok=True)
    # Content-addressed: the same output twice reuses one artifact instead of
    # accumulating duplicates.
    artifact = config.ARTIFACTS / f"{sha256(text.encode('utf-8')).hexdigest()[:16]}.txt"
    artifact.write_text(text, encoding="utf-8")

    lines = text.splitlines()
    if len(lines) > config.HEAD_LINES + config.TAIL_LINES:
        head = "\n".join(lines[:config.HEAD_LINES])
        tail = "\n".join(lines[-config.TAIL_LINES:])
        hidden = len(lines) - config.HEAD_LINES - config.TAIL_LINES
        elided = f"[{hidden} lines elided, {len(text)} chars total]"
    else:
        # One enormous line: the line-based split cannot apply, so bound by chars.
        half = cap // 2
        head, tail = text[:half], text[-half:]
        elided = f"[{len(text) - 2 * half} chars elided, {len(text)} chars total]"

    # NFR-104 bounds CHARACTERS while the branch above bounds LINES; a single
    # enormous line satisfies the line cap and breaches the char one.
    half = cap // 2
    head, tail = head[:half], tail[-half:]

    return (
        f"{head}\n... {elided} ...\n{tail}\n"
        f"[full output: {artifact}]\n"
        f'Inspect it with read_file(path="{artifact}", offset=N, limit=M), '
        f"or search it with run_shell(command='grep -n PATTERN {artifact}')."
    )


# ============================================================ compaction (§4.3)
#
# §4.3's stated boundary is invalid in 100% of real runs; its INTENT is kept and
# the arithmetic corrected. Derivation and the borrow are in NOTICE/CHANGELOG.
HEAD_MESSAGES = 2
TAIL_MESSAGES = 6
SUMMARY_PREFIX = "[CONTEXT SUMMARY]: "


def _blocks(message: dict) -> list:
    content = message.get("content")
    return content if isinstance(content, list) else []


def _calls(message: dict) -> set:
    return {b["id"] for b in _blocks(message) if b.get("type") == "tool_use"}


def _answers(message: dict) -> set:
    return {b["tool_use_id"] for b in _blocks(message)
            if b.get("type") == "tool_result"}


def pairs_ok(messages: list[dict]) -> bool:
    """Every tool_use is answered by the very next message, and no tool_result is
    orphaned. This is the invariant compaction must not break, and it is written
    first because every other test in this layer leans on it."""
    for i, message in enumerate(messages):
        calls = _calls(message)
        if calls and calls - (_answers(messages[i + 1]) if i + 1 < len(messages) else set()):
            return False
        answered = _answers(message)
        if answered and answered - (_calls(messages[i - 1]) if i else set()):
            return False
    return True


def _clean_boundary(messages: list[dict], index: int) -> bool:
    """True when cutting at `index` does not orphan a tool_result from its call.

    A boundary lands ON a message. If that message answers a tool call, the call
    is in the message before it, so the cut separates them.
    """
    return index >= len(messages) or not _answers(messages[index])


def _snap(messages: list[dict], index: int, low: int, high: int) -> int:
    """Move `index` onto the nearest clean boundary, forward first.

    Forward is preferred so an orphaned result folds into the region that already
    holds its call; backward is the fallback when no clean boundary exists ahead.
    Clamped to [low, high].
    """
    forward = index
    while forward < high and not _clean_boundary(messages, forward):
        forward += 1
    if _clean_boundary(messages, forward):
        return forward
    backward = index
    while backward > low and not _clean_boundary(messages, backward):
        backward -= 1
    return backward


def boundaries(messages: list[dict]) -> tuple[int, int]:
    """Where the head ends and the tail begins, both snapped off tool pairs.

    Exposed because the compact node has to summarise exactly the region
    compact_messages will remove. Two functions computing the same two indices
    independently is how they come to disagree.
    """
    head_end = _snap(messages, HEAD_MESSAGES, 1, len(messages))
    return head_end, _snap(messages, len(messages) - TAIL_MESSAGES,
                           head_end, len(messages))


def compact_messages(messages: list[dict], summary: str) -> list[dict]:
    """Replace the middle of `messages` with one summary. Pure (FR-404).

    The summary is appended to the last retained head message when that message
    is a user turn, rather than inserted as a new one. Two consecutive user
    messages are a shape some providers reject, and the head always ends on a
    user tool_result once the boundary is snapped - so appending a text block is
    both the safe option and one message cheaper.
    """
    if len(messages) <= HEAD_MESSAGES + TAIL_MESSAGES:
        return list(messages)

    head_end, tail_start = boundaries(messages)
    if tail_start <= head_end:
        return list(messages)          # nothing between the two regions to remove

    head = [dict(m) for m in messages[:head_end]]
    note = {"type": "text", "text": f"{SUMMARY_PREFIX}{summary}"}
    if head and head[-1].get("role") == "user" and isinstance(head[-1].get("content"), list):
        head[-1]["content"] = list(head[-1]["content"]) + [note]
        return head + list(messages[tail_start:])
    return head + [{"role": "user", "content": [note]}] + list(messages[tail_start:])


def context_chars(messages: list[dict]) -> int:
    """Size of the message list as it will be sent.

    Characters as a proxy for tokens at the project's usual ~3:1, the same
    convention MAX_SCHEMA_CHARS and MAX_RESULT_CHARS already use. No tokeniser,
    so reflect stays deterministic and testable without an API key (NFR-602).
    """
    return len(json.dumps(messages, default=str))
