"""On-demand knowledge documents, in the agentskills.io layout (Phase N).

A skill is a directory holding `SKILL.md` - YAML frontmatter with `name` and
`description`, then a markdown body - plus any reference files or scripts it
bundles.

**Progressive disclosure is the entire point, and on this provider it is not a
nicety.** Phase L measured `cache_read_tokens: 0` on every row of a scored run,
so everything in the prompt is re-sent and re-paid on every turn; four tool
schemas were already ~23% of a median run. A knowledge library injected wholesale
would be ruinous. So:

    Level 1  always loaded   name + description, ~40 tokens per skill
    Level 2  on demand       the SKILL.md body
    Level 3  on demand       bundled reference files

The cost of a skill is paid only when the agent decides it applies.

**This module READS. It never executes.** A bundled script is run by the agent
calling `run_shell` on it, which goes through classify() and the DANGER regex like
anything else. That grants nothing `run_shell` does not already grant inside a
container whose boundary Phase K made kernel-enforced - what Level 3 changes is
PROVENANCE, code arriving in a document rather than from the model. In Phase N
every skill is written by a human; **when Phase O lets the agent author them, this
becomes a live question and must be re-opened.**

Retrieval is keyword-shaped and stays that way: §11 forbids vectors until keyword
recall is measured and found wanting, and Phase M measured keyword recall working.

CE-05: nothing here runs at import.
NFR-602: every function below is testable with no API key and no network.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from agent import config, memory, policy

# Names this module put into policy.RISK, so deactivate() removes exactly those.
# Per-module, like mcp.py and memory.py: a snapshot taken by whichever imported
# first would silently own the others' entries and strip them on shutdown.
_REGISTERED: list[str] = []

SKILL_FILE = "SKILL.md"


def _scan(front: str) -> dict:
    """Last-resort frontmatter read: the first colon on each line wins.

    Exists because of a defect its own test caught. `description` is prose, and
    prose contains colons:

        description: Use when asked to deploy: staging, production, or rollback.

    That is not valid YAML - the value has to be quoted - and `safe_load` either
    raises or returns something unusable, so the skill was DROPPED IN SILENCE. A
    human writing these by hand will produce that line, and a skill the agent
    cannot see is indistinguishable from one it chose not to use.

    So YAML stays the primary path, and this recovers the common mistake rather
    than punishing it.
    """
    meta = {}
    for line in front.splitlines():
        key, sep, value = line.partition(":")
        key = key.strip()
        if sep and key in ("name", "description") and key not in meta:
            meta[key] = value.strip().strip("\"'")
    return meta


def _parse(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from the body.

    A real YAML parser rather than a hand-rolled split: the standard's frontmatter
    IS YAML, and quoted or folded values are only read correctly by one. `_scan`
    picks up what safe_load cannot.
    """
    import yaml

    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    front, body = parts[1], parts[2].lstrip("\n")
    try:
        meta = yaml.safe_load(front)
    except yaml.YAMLError:
        meta = None
    if not isinstance(meta, dict) or not str(meta.get("description") or "").strip():
        meta = {**_scan(front), **(meta if isinstance(meta, dict) else {})}
    return meta, body


def _read(directory: Path) -> dict | None:
    """One skill, or None when the directory does not describe a usable one.

    Skipped rather than raised. A knowledge library that takes down the agent
    because one document has a typo is worse than no library - and a malformed
    skill is exactly what a human editing these files by hand will produce.
    """
    path = directory / SKILL_FILE
    try:
        meta, body = _parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:                       # unreadable, or invalid YAML
        return None
    name = str(meta.get("name") or directory.name).strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        # Without a description there is nothing to match on, so the skill could
        # never be chosen anyway. Dropping it keeps the index honest.
        return None
    try:
        written = path.stat().st_mtime
    except OSError:
        return None
    return {"name": name, "description": description, "body": body,
            "dir": directory, "written": written}


def catalogue() -> dict[str, dict]:
    """Every usable skill, keyed by name. Later roots do NOT override earlier ones.

    The project's own skills win over the agent home's, so a skill the agent
    writes for itself in Phase O cannot silently shadow one shipped with the
    repository.
    """
    found: dict[str, dict] = {}
    if not config.SKILLS_ENABLED:
        return found
    for root in config.SKILLS_DIRS:
        if not root.is_dir():
            continue
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            skill = _read(directory)
            if skill and skill["name"] not in found:
                found[skill["name"]] = skill
    return found


class SkillIndexTooLarge(RuntimeError):
    """The always-loaded index costs more than config.SKILLS_INDEX_CHARS allows.

    Fatal, and deliberately not a truncation. Cutting the list short drops whole
    skills out of the agent's view, and a skill it cannot see is indistinguishable
    in the traces from one it chose not to use - so the failure would land as a
    quiet drop in the load rate that nobody could attribute. The same reasoning
    makes MAX_SCHEMA_CHARS fatal: a budget that can be exceeded with a printed
    warning is not a budget.
    """


def index() -> str:
    """Level 1: what is always in the prompt. Name and description only.

    Paid on every request of every run, including runs that load nothing, which is
    why it is budgeted rather than merely bounded.
    """
    skills = catalogue()
    if not skills:
        return ""
    lines = [f"- {s['name']}: {s['description']}" for s in skills.values()]
    text = ("# Skills available\n\n"
            "Reference documents you can open when one applies. Call "
            '`load_skill(name="...")` to read one BEFORE starting work it covers - '
            "the descriptions below are all you have until you do.\n\n"
            + "\n".join(lines))
    if len(text) > config.SKILLS_INDEX_CHARS:
        raise SkillIndexTooLarge(
            f"the skill index is {len(text):,} chars against a cap of "
            f"{config.SKILLS_INDEX_CHARS:,} ({len(skills)} skills). It sits in the "
            f"system prompt and is re-sent on every request, so this is charged per "
            f"turn. Shorten the `description` fields, remove a skill, or raise the "
            f"cap in config.py deliberately.")
    return text


BLANK = '\n\n'


def best_match(goal: str) -> dict | None:
    """The one skill whose description this goal is about, or None.

    Scored on the RAREST shared words, which is the same correction that took
    memory recall from 2/6 to 5/6: matching on every word lets `file`, `the` and
    `add` outvote `release` or `dependency`. A word appearing in most skill
    descriptions discriminates nothing and is dropped.

    Returns None on a tie or on no rare overlap at all - injecting the WRONG
    skill is worse than injecting none, because the agent then follows a
    convention that does not apply.
    """
    entries = catalogue()
    if not entries:
        return None

    def words(text):
        # THREE characters, not four. `len(w) > 3` dropped `cut`, `tag` and `txt`,
        # and "Cut release 4.14.0" then shared only one rare word with the release
        # skill - a tie, so nothing was injected. Swept over the eight fixture
        # skills and nine goals: this scores 9/9 with no wrong match, where a
        # 4-character floor scored 6 and mismatched twice.
        return {w for w in "".join(ch if ch.isalnum() else " "
                                   for ch in text).lower().split() if len(w) > 2}

    described = {name: words(f"{e['name']} {e['description']}")
                 for name, e in entries.items()}
    # How many skills each word appears in. A word in most of them is furniture.
    spread: dict = {}
    for bag in described.values():
        for word in bag:
            spread[word] = spread.get(word, 0) + 1
    ceiling = max(1, len(entries) // 2)

    asked = words(goal)
    scores = {}
    for name, bag in described.items():
        rare = [w for w in asked & bag if spread[w] <= ceiling]
        if rare:
            scores[name] = len(rare)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top = [name for name, score in ranked if score == ranked[0][1]]
    if len(top) == 1:
        return entries[top[0]]
    # AMENDED 2026-09-23: a tie goes to the skill written most recently, which is
    # the one made from the most recently read document. `conventions` (stale) and
    # `runbook` (current) tie on "Cut release 2.3." and returning None there
    # injected NOTHING - the 1/3 the revision control arm scored.
    newest = sorted(top, key=lambda name: -entries[name]["written"])
    if entries[newest[0]]["written"] == entries[newest[1]]["written"]:
        return None                       # written together: still not a match
    return entries[newest[0]]


def opening(goal: str) -> str:
    """The matching skill's body, ready for the system prompt, or empty.

    The counterpart to memory.context_for: injected by a RULE rather than
    requested. `load_skill` stays available for the others.
    """
    if not config.AUTO_SKILL:
        return ""
    skill = best_match(goal)
    if not skill:
        return ""
    try:
        body = load_skill(skill["name"])
    except Exception:
        return ""                        # a broken skill must not end the run
    header = "# The skill for this task" + BLANK + (
        "Opened for you because it matches what you were asked. Follow it.")
    return header + BLANK + body


def matched(goal: str) -> str:
    """The name `opening` would inject for this goal, or "".

    `finish` needs the identity and not the body. Deriving it there instead would
    make two sources of truth for one fact.
    """
    if not config.AUTO_SKILL:
        return ""
    skill = best_match(goal)
    return skill["name"] if skill else ""


def _resolve(skill: dict, filename: str) -> Path:
    """A bundled file inside this skill's own directory, or an error.

    Same shape as policy._inside_workspace(): resolve, then require the root to be
    an ancestor. `.resolve()` follows symlinks, so a link inside the skill pointing
    out resolves outside and is refused. Fails closed.
    """
    root = skill["dir"].resolve()
    candidate = Path(filename)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if target != root and root not in target.parents:
        raise ValueError(
            f"{filename!r} is outside the skill directory. Bundled files are named "
            f"relative to the skill, for example file=\"reference.md\".")
    if not target.is_file():
        raise FileNotFoundError(
            f"{skill['name']} bundles no file called {filename!r}. "
            f"Call load_skill(name=\"{skill['name']}\") to list what it does bundle.")
    return target


def load_skill(name: str, file: str | None = None) -> str:
    """Levels 2 and 3, in one tool.

    Without `file`: the skill's body, plus a list of what it bundles.
    With `file`: that bundled file's contents.

    One tool rather than two because the schema budget allowed exactly one more
    (six tools were 3,553 chars against a 6,000 cap) and a second would have spent
    the headroom for no capability.
    """
    skills = catalogue()
    skill = skills.get(str(name).strip())
    if skill is None:
        raise ValueError(
            f"no skill called {name!r}. Available: "
            f"{', '.join(sorted(skills)) or 'none'}.")

    if file:
        return _resolve(skill, str(file)).read_text(encoding="utf-8", errors="replace")

    bundled = sorted(p.name for p in skill["dir"].iterdir()
                     if p.is_file() and p.name != SKILL_FILE)
    body = skill["body"]
    if bundled:
        body += ("\n\n---\nFiles bundled with this skill: " + ", ".join(bundled) +
                 f'\nRead one with load_skill(name="{skill["name"]}", file="...").'
                 "\nTo RUN one, use run_shell against "
                 f"{skill['dir'].as_posix()}/<file>.")
    return body


LOAD_SCHEMA = {
    "name": "load_skill",
    "description": (
        "Open one of the skills listed under 'Skills available'. Returns the full "
        "document, which the listed description only summarises. Pass `file` to "
        "read something the skill bundles alongside it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The skill's name, exactly as listed."},
            "file": {"type": "string",
                     "description": ("Optional. A file bundled with the skill, named "
                                     "relative to it, e.g. \"reference.md\".")},
        },
        "required": ["name"],
    },
}


def unusable() -> list[str]:
    """Directories that look like skills but cannot be used, for reporting."""
    broken = []
    for root in config.SKILLS_DIRS:
        if not root.is_dir():
            continue
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            if (directory / SKILL_FILE).is_file() and _read(directory) is None:
                broken.append(directory.name)
    return broken


# ------------------------------------------------------- authoring (Phase O)

_SLUG_OK = "abcdefghijklmnopqrstuvwxyz0123456789-"


def _slug(name: str) -> str:
    """A directory name derived from the skill's name, never taken from input.

    This is the enforcement, not a validation rule that could be argued with:
    `learn` has no path argument, and the one directory it can reach is built here
    from an alphabet that cannot express a separator or a parent reference.
    """
    text = "".join(c if c in _SLUG_OK else "-" for c in str(name).strip().lower())
    slug = "-".join(part for part in text.split("-") if part)[:48]
    if not slug:
        raise ValueError("give the skill a name, in words - for example "
                         'name="cutting-a-release".')
    return slug


def home() -> Path:
    """Where authored skills live: the agent's own root, not the project's."""
    return config.SKILLS_DIRS[-1]


def authored() -> list[str]:
    """Names of skills the agent wrote for itself."""
    root = home()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and (d / SKILL_FILE).is_file())


# Tools that mean the agent AUTHORED the file rather than consulted it.
_WROTE = ("write_file", "edit_file")


def read_but_not_edited(messages: list[dict]) -> list[tuple[str, str]]:
    """Documents the agent read and never wrote to, with their contents.

    The deterministic stand-in for the judgement `learn` asked the model for and
    Phase O measured it declining: 0 calls in 15 sessions. A file read and never
    edited is a reference; anything the agent wrote is its own output and knows
    nothing the agent did not already have.

    Contents come straight from the tool_result, which is the whole reason no model
    call is needed - `read_file`'s output is already verbatim in `messages`.

    Over-capture is expected and is bounded elsewhere: extract() caps the size and
    MAX_AUTHORED_SKILLS caps the library.
    """
    # graph._outcomes does the same pairing, but importing it here would make this
    # leaf module depend on graph.py - and so on langgraph - purely for six lines,
    # while also closing an import cycle (graph imports skills). Kept local.
    results, failed = {}, set()
    for message in messages:
        if not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results[block["tool_use_id"]] = str(block.get("content", ""))
                if block.get("is_error"):
                    failed.add(block["tool_use_id"])

    wrote, read, body = set(), [], {}
    for message in messages:
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            path = (block.get("input") or {}).get("path")
            if not isinstance(path, str):
                continue
            if block["name"] in _WROTE:
                wrote.add(path)
            elif block["name"] == "read_file" and block["id"] not in failed:
                if path not in body:
                    read.append(path)
                body[path] = results.get(block["id"], "")
    return [(p, body[p]) for p in read if p not in wrote and body[p]]


def learn(name: str, description: str, body: str) -> str:
    """Write a skill for future sessions to load. TEXT ONLY.

    The agent supplies prose and this function writes the file, so the frontmatter
    is correct by construction - the malformed-document failure Phase N had to add
    a fallback parser for cannot happen to an authored skill.

    **No path argument and no file argument.** Phase N left the provenance question
    open ("when Phase O gives the agent that power, this must be re-opened"), and
    the answer is that the agent may write documents and not scripts. A wrong
    document gives advice a later session can second-guess; a wrong script is
    executed by a session that has already accepted it as procedure. Running one
    would still pass through classify() - this is not a gate bypass - but the
    mistake would outlive the session that made it, and that is the difference.

    Rewriting is allowed. Deleting is not: an agent that can drop skills can
    quietly erase the evidence of its own bad ones.
    """
    description = " ".join(str(description).split())
    body = str(body).strip()
    if not description:
        raise ValueError(
            "a skill needs a description saying WHEN to use it - that sentence is "
            "all a future session sees until it opens the document. For example: "
            'description="Use when asked to cut or prepare a release."')
    if not body:
        raise ValueError("a skill needs a body - the steps a future session should "
                         "follow. Keep it short and specific.")

    slug = _slug(name)
    existing = authored()
    if slug not in existing and len(existing) >= config.MAX_AUTHORED_SKILLS:
        raise ValueError(
            f"you already have {len(existing)} skills, the limit. Every skill's "
            f"description is sent on every request, so the library is capped. "
            f"Rewrite an existing one instead: {', '.join(existing)}.")

    directory = home() / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SKILL_FILE).write_text(
        f"---\nname: {slug}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8", newline="\n")
    return (f"{'rewrote' if slug in existing else 'wrote'} skill {slug!r}. "
            f"Future sessions will see its description and can open it.")


def _undecorate(content: str) -> str:
    """Strip read_file's presentation: the header line and the line-number gutter.

    Measured, not anticipated: the first version kept both, so an extracted skill
    read `     1\t# The one rule` instead of `# The one rule`. Numbers are how the
    tool shows a file to the model, not part of what the file says.
    """
    # The loop's repeat notice (graph.py, `_repeat_notice`) rides on the END of a
    # tool result. It carried a one-line file over the floor once.
    content = re.sub(r"\n\n\[This exact \w+ call has now returned[^\]]*\]\s*$", "", content)
    lines = content.split("\n")
    if lines and re.match(r"^\S.*\(lines \d+-\d+ of \d+\)", lines[0]):
        lines = lines[1:]
    return "\n".join(re.sub(r"^\s*\d+\t", "", line) for line in lines).strip()


def _when(path: str, body: str) -> str:
    """The description: WHEN this applies, never what the agent was doing.

    Derived from the goal, the first version produced "Use when working on tasks
    like: create b.txt containing beta" - one specific task. A later session asked
    for c.txt, matched nothing, and never opened the skill: extracted and indexed,
    never loaded. The description is all a future session sees until it opens the
    document, so it has to name a CLASS of work.

    The document's own first heading is the best available summary of that class,
    and it costs nothing to read.
    """
    heading = ""
    for line in body.split("\n"):
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            heading = stripped[:60]
            break
    subject = heading or path
    return (f"Use when {subject.lower()} applies to the work in hand - project "
            f"conventions, formats and rules recorded in {path}. Check it BEFORE "
            f"creating or changing files.")


def extract(messages: list[dict], goal: str) -> list[str]:
    """Write a skill from each reference document the agent read. Returns slugs.

    Phase O-redux. `learn` asks the MODEL to decide what is worth keeping, and the
    measurement was 0 calls in 15 sessions. This decides with a rule instead, from
    material already in `messages` - which is what keeps `finish` deterministic and
    leaves `act` the only node that touches a model.

    Never raises. It runs at the end of a session that may already have succeeded,
    and a bookkeeping failure must not turn a passing run into a crashed one.
    """
    if not config.SKILL_EXTRACTION:
        return []
    usable = []
    for path, content in read_but_not_edited(messages):
        body = _undecorate(content)[:config.EXTRACT_MAX_CHARS]
        if _is_document(path) and len(body) >= config.EXTRACT_MIN_CHARS:
            usable.append((path, body))
    written = []
    for path, body in usable:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        try:
            learn(name=stem, description=_when(path, body), body=body)
        except ValueError:
            # The library cap, or a name that slugs to nothing. Both are ordinary
            # outcomes here, not failures of the run.
            continue
        written.append(_slug(stem))
    return written


# A skill is a procedure written for people. A source file's first line is a
# docstring or an import, and a skill made from it out-matched the document the
# case existed to test - ship.py on 2026-09-09, calc.py on 2026-09-13.
DOCUMENT_SUFFIXES = frozenset({"", ".md", ".markdown", ".rst", ".txt", ".adoc"})
NOT_DOCUMENTS = frozenset({"makefile", "dockerfile", "containerfile", "license", "licence"})


def _is_document(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    suffix = name[name.rfind("."):].lower() if "." in name[1:] else ""
    return suffix in DOCUMENT_SUFFIXES and name.lower() not in NOT_DOCUMENTS


LEARN_SCHEMA = {
    "name": "learn",
    "description": (
        "Record a reusable procedure so future sessions know it without working it "
        "out again. Use it after solving something whose method would apply to "
        "similar work later - not for facts about this one task, which `remember` "
        "covers. Writes a document; it cannot create scripts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "A few words, e.g. \"cutting a release\"."},
            "description": {"type": "string",
                            "description": ("One sentence saying WHEN to use it. This is "
                                            "all a future session sees until it opens "
                                            "the skill, so say what kind of task it "
                                            "covers, not what it contains.")},
            "body": {"type": "string",
                     "description": "The steps to follow, in markdown."},
        },
        "required": ["name", "description", "body"],
    },
}


def activate() -> list[str]:
    """Register the skill tools for this run. A no-op when skills are off."""
    if not config.SKILLS_ENABLED:
        return []
    # Said once, at startup, rather than never. A silently dropped skill looks
    # exactly like one the agent decided not to open.
    broken = unusable()
    if broken:
        print(f"skills: ignoring {len(broken)} unusable document(s): "
              f"{', '.join(broken)} (each needs `name` and `description`)",
              file=sys.stderr)
    if "load_skill" not in _REGISTERED:
        # `read`: it opens documents and cannot modify anything. Running a bundled
        # script is a separate run_shell call, classified on its own merits.
        policy.register("load_skill", "read")
        _REGISTERED.append("load_skill")
    # Registered separately, because the Phase O control has LOADING on and
    # authoring off - one flag has to be able to move without the other.
    if config.SKILL_AUTHORING and "learn" not in _REGISTERED:
        policy.register("learn", "write")
        _REGISTERED.append("learn")
    return sorted(catalogue())


def deactivate() -> None:
    """Remove exactly what activate() registered. Safe to call unconditionally."""
    while _REGISTERED:
        policy.RISK.pop(_REGISTERED.pop(), None)


def tools() -> dict[str, dict]:
    """The skill tools active for this run. Empty when skills are off."""
    if not config.SKILLS_ENABLED or "load_skill" not in _REGISTERED:
        return {}
    active = {"load_skill": {"fn": load_skill, "schema": LOAD_SCHEMA, "risk": "read"}}
    if config.SKILL_AUTHORING and "learn" in _REGISTERED:
        active["learn"] = {"fn": learn, "schema": LEARN_SCHEMA, "risk": "write"}
    return active
