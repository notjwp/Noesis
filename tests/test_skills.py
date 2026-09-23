"""Skills: progressive disclosure, and the document that must not become a hole.

An eighth test file, and the sixth stated deviation from the spec's three-file
`tests/` allowlist. The justification is specific to what this module is: **it is
the first component where a DOCUMENT can direct the agent's behaviour**, and its
failure modes are quiet ones. A skill dropped for a malformed field looks exactly
like a skill the agent chose not to use; a bundled-file path that escapes its
directory looks exactly like a successful read. Neither surfaces in a pass rate.

No API key, no network (NFR-602).
"""
import pytest

from agent import config, policy, skills

SKILL = """---
name: {name}
description: {description}
---

# {name}

{body}
"""


@pytest.fixture(autouse=True)
def library(tmp_path, monkeypatch):
    """A temp skill library, so tests never read the repository's real one."""
    monkeypatch.setattr(config, "SKILLS_DIRS", (tmp_path,))
    monkeypatch.setattr(config, "SKILLS_ENABLED", True)
    before = dict(policy.RISK)
    yield tmp_path
    skills.deactivate()
    policy.RISK.clear()
    policy.RISK.update(before)


def make(root, name, description="Use when testing.", body="Step one.", files=None):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        SKILL.format(name=name, description=description, body=body), encoding="utf-8")
    for filename, text in (files or {}).items():
        (directory / filename).write_text(text, encoding="utf-8")
    return directory


# ============================================================ level 1: the index

def test_the_index_carries_descriptions_but_not_bodies():
    """The entire progressive-disclosure claim, as an assertion.

    The body is what costs tokens; if it leaks into the index then every run pays
    for every skill whether or not it is used, which is the pattern this replaces.
    """
    make(library_root := _root(), "alpha", "Use when alpha.", "SECRET BODY TEXT")
    text = skills.index()
    assert "alpha" in text and "Use when alpha." in text
    assert "SECRET BODY TEXT" not in text
    assert library_root


def test_an_empty_library_costs_nothing():
    """No skills must mean no header and no tokens at all - not an empty section."""
    assert skills.index() == ""


def test_an_oversized_index_is_fatal_rather_than_truncated(monkeypatch):
    """Truncating drops whole skills out of the agent's view, and a skill it cannot
    see is indistinguishable in the traces from one it chose not to use."""
    root = _root()
    for i in range(10):
        make(root, f"skill{i}", "Use when " + "x" * 200)
    monkeypatch.setattr(config, "SKILLS_INDEX_CHARS", 300)
    with pytest.raises(skills.SkillIndexTooLarge) as caught:
        skills.index()
    assert "against a cap of" in str(caught.value)


# ====================================================== malformed skills survive

def test_a_skill_without_a_description_is_skipped_not_fatal():
    """A knowledge library that crashes the agent because one document has a typo
    is worse than no library - and a human editing these by hand will produce one."""
    root = _root()
    make(root, "good", "Use when good.")
    (root / "bad").mkdir()
    (root / "bad" / "SKILL.md").write_text("---\nname: bad\n---\nbody", encoding="utf-8")
    assert sorted(skills.catalogue()) == ["good"]


def test_invalid_yaml_is_skipped_not_fatal():
    root = _root()
    make(root, "good", "Use when good.")
    (root / "broken").mkdir()
    (root / "broken" / "SKILL.md").write_text(
        "---\nname: [unclosed\ndescription: x\n---\nbody", encoding="utf-8")
    assert "good" in skills.catalogue()


def test_a_directory_with_no_skill_file_is_ignored():
    root = _root()
    make(root, "good", "Use when good.")
    (root / "notaskill").mkdir()
    assert sorted(skills.catalogue()) == ["good"]


def test_a_long_prose_description_survives_the_parser():
    """`description` is prose with commas, colons and full stops. A split(":", 1)
    parser mangles it, and it is the exact field retrieval matches on."""
    wordy = ("Use when asked to deploy: staging, production, or a rollback. "
             'Covers "make ship-quartz" and the settled check.')
    make(_root(), "deploy", wordy)
    assert skills.catalogue()["deploy"]["description"] == wordy


# =========================================================== levels 2 and 3

def test_loading_returns_the_body():
    make(_root(), "alpha", body="Run the thing, then check it.")
    assert "Run the thing, then check it." in skills.load_skill("alpha")


def test_loading_lists_what_the_skill_bundles():
    """The agent cannot ask for a file it does not know exists."""
    make(_root(), "alpha", files={"codes.md": "# codes", "check.py": "print(1)"})
    body = skills.load_skill("alpha")
    assert "codes.md" in body and "check.py" in body
    assert "SKILL.md" not in body.split("Files bundled with this skill:")[1]


def test_a_bundled_file_can_be_read():
    make(_root(), "alpha", files={"codes.md": "QZ103 means something"})
    assert "QZ103" in skills.load_skill("alpha", file="codes.md")


def test_an_unknown_skill_names_what_does_exist():
    """An actionable error, the lesson the directory-read fix paid for."""
    make(_root(), "alpha")
    with pytest.raises(ValueError, match="alpha"):
        skills.load_skill("nope")


def test_an_unknown_bundled_file_is_actionable():
    make(_root(), "alpha")
    with pytest.raises(FileNotFoundError, match="bundles no file"):
        skills.load_skill("alpha", file="missing.md")


# ================================================= the escape, tested not assumed

@pytest.mark.parametrize("attempt", [
    "../../../etc/passwd",
    "/etc/passwd",
    "../other/SKILL.md",
    "sub/../../other/SKILL.md",
])
def test_a_bundled_path_cannot_escape_its_own_skill(attempt):
    """Phase L's PATH_ARGS lesson: the bypass is tested, never assumed closed.

    A skill that could read any file on disk would turn a document into a
    filesystem, and the failure would look exactly like a successful read.
    """
    root = _root()
    make(root, "alpha", files={"codes.md": "x"})
    make(root, "other", body="the other skill")
    with pytest.raises((ValueError, FileNotFoundError)):
        skills.load_skill("alpha", file=attempt)


def test_load_skill_never_executes_anything():
    """It READS. A bundled script is run by the agent calling run_shell on it, which
    goes through classify() and the DANGER regex like anything else. If loading
    could execute, a document would be a way around the gate."""
    make(_root(), "alpha", files={"boom.py": "raise SystemExit('should not run')"})
    assert "raise SystemExit" in skills.load_skill("alpha", file="boom.py")


# ================================================================ the gate

def test_activate_registers_the_tool_as_a_read():
    make(_root(), "alpha")
    skills.activate()
    assert policy.RISK["load_skill"] == "read"
    assert policy.classify("load_skill", {"name": "alpha"}, autonomous=True)[0] == "auto"


def test_deactivate_removes_only_its_own_registration():
    """Three modules register tools now. Any one stripping another's entries leaves
    a tool the gate refuses as unknown, mid-run."""
    policy.register("fetch", "read")
    skills.activate()
    skills.deactivate()
    assert "load_skill" not in policy.RISK
    assert policy.RISK.get("fetch") == "read"


# ============================================================== the kill switch

def test_with_skills_off_there_is_no_index_and_no_tool(monkeypatch):
    make(_root(), "alpha")
    monkeypatch.setattr(config, "SKILLS_ENABLED", False)
    assert skills.index() == ""
    assert skills.activate() == []
    assert skills.tools() == {}


def test_with_skills_off_the_toolset_falls_back(monkeypatch):
    from agent.registry import toolset

    make(_root(), "alpha")
    monkeypatch.setattr(config, "SKILLS_ENABLED", False)
    skills.activate()
    assert "load_skill" not in toolset()


def test_the_project_library_wins_a_name_collision(tmp_path, monkeypatch):
    """A skill the agent writes for itself in Phase O must not be able to shadow one
    shipped with the repository."""
    shipped, home = tmp_path / "shipped", tmp_path / "home"
    make(shipped, "alpha", "The shipped one.")
    make(home, "alpha", "The agent's own.")
    monkeypatch.setattr(config, "SKILLS_DIRS", (shipped, home))
    assert skills.catalogue()["alpha"]["description"] == "The shipped one."


# --------------------------------------------------------------------- helper

def _root():
    """The temp library root the autouse fixture installed."""
    return config.SKILLS_DIRS[0]


# ================================================== authoring (Phase O)

def test_learn_writes_a_loadable_skill(monkeypatch):
    """Written by the module, not the model, so the frontmatter is correct by
    construction - the malformed-document failure cannot happen to an authored one."""
    monkeypatch.setattr(config, "SKILL_AUTHORING", True)
    skills.learn(name="Cutting a Release",
                 description="Use when asked to cut or prepare a release.",
                 body="1. bump VERSION")
    cat = skills.catalogue()
    assert cat["cutting-a-release"]["description"].startswith("Use when asked to cut")
    assert "bump VERSION" in skills.load_skill("cutting-a-release")


@pytest.mark.parametrize("name", ["../../etc/passwd", "/abs/path", "a/b", r"..\x"])
def test_authoring_cannot_express_a_path(name, monkeypatch):
    """The enforcement is an ABSENT parameter plus a slug alphabet, not a rule that
    could be argued with: `learn` has no path argument at all."""
    monkeypatch.setattr(config, "SKILL_AUTHORING", True)
    skills.learn(name=name, description="Use when x.", body="step")
    for directory in skills.home().iterdir():
        assert directory.parent == skills.home()
        assert "/" not in directory.name and ".." != directory.name


def test_learn_refuses_a_skill_with_no_description(monkeypatch):
    monkeypatch.setattr(config, "SKILL_AUTHORING", True)
    with pytest.raises(ValueError, match="WHEN to use it"):
        skills.learn(name="x", description="  ", body="step")


def test_learn_caps_the_library(monkeypatch):
    """The index is charged on every request and overflowing it is fatal, so an
    agent writing one skill per session would otherwise brick its own runs."""
    monkeypatch.setattr(config, "SKILL_AUTHORING", True)
    monkeypatch.setattr(config, "MAX_AUTHORED_SKILLS", 2)
    skills.learn(name="one", description="Use when one.", body="a")
    skills.learn(name="two", description="Use when two.", body="b")
    with pytest.raises(ValueError, match="the limit"):
        skills.learn(name="three", description="Use when three.", body="c")


def test_rewriting_is_allowed_but_the_cap_still_applies(monkeypatch):
    """Rewriting must stay possible at the cap, or the agent is stuck with its own
    early mistakes and cannot correct them."""
    monkeypatch.setattr(config, "SKILL_AUTHORING", True)
    monkeypatch.setattr(config, "MAX_AUTHORED_SKILLS", 1)
    skills.learn(name="one", description="Use when one.", body="first")
    skills.learn(name="one", description="Use when one, revised.", body="second")
    assert "second" in skills.load_skill("one")
    assert skills.authored() == ["one"]


def test_authoring_off_removes_the_tool_but_not_loading(monkeypatch):
    """Phase O's control: loading ON, authoring OFF. One flag has to move without
    the other or the comparison measures the wrong thing."""
    monkeypatch.setattr(config, "SKILL_AUTHORING", False)
    skills.activate()
    assert "learn" not in skills.tools()
    assert "load_skill" in skills.tools()
    assert "learn" not in policy.RISK


# ============================ deterministic extraction (Phase O-redux)

def test_read_but_not_edited_keeps_references_and_drops_work_products():
    """The judgement `learn` asked the model for, as a rule: a document read and
    never edited is a reference; anything the agent wrote is a work product."""
    from agent.skills import read_but_not_edited

    messages = [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "CONVENTIONS.md"}},
            {"type": "tool_use", "id": "b", "name": "read_file",
             "input": {"path": "VERSION"}},
            {"type": "tool_use", "id": "c", "name": "edit_file",
             "input": {"path": "VERSION", "old_string": "1", "new_string": "2"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a",
             "content": "CONVENTIONS.md (lines 1-2 of 2)\n     1\t# Rules\n     2\tuse tabs"},
            {"type": "tool_result", "tool_use_id": "b",
             "content": "VERSION (lines 1-1 of 1)\n     1\t1.0"},
            {"type": "tool_result", "tool_use_id": "c", "content": "ok"}]},
    ]
    found = read_but_not_edited(messages)
    assert [path for path, _ in found] == ["CONVENTIONS.md"]
    assert "use tabs" in found[0][1]


def test_a_failed_read_is_not_a_candidate():
    """A read that errored has no content to keep."""
    from agent.skills import read_but_not_edited

    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "gone.md"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a",
             "content": "FileNotFoundError: gone.md", "is_error": True}]},
    ]
    assert read_but_not_edited(messages) == []


def test_a_file_written_then_read_is_not_a_reference():
    """write_file counts as editing. The agent's own output is not knowledge."""
    from agent.skills import read_but_not_edited

    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "write_file",
             "input": {"path": "notes.md", "content": "x"}},
            {"type": "tool_use", "id": "b", "name": "read_file",
             "input": {"path": "notes.md"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "ok"},
            {"type": "tool_result", "tool_use_id": "b", "content": "notes.md\n     1\tx"}]},
    ]
    assert read_but_not_edited(messages) == []


def test_extract_writes_a_loadable_skill_from_a_document(monkeypatch):
    """The whole mechanism: what the agent READ becomes what a later session LOADS,
    with no model call anywhere in the path."""
    monkeypatch.setattr(config, "SKILL_EXTRACTION", True)
    messages = [
        {"role": "user", "content": "Read CONVENTIONS.md, then cut release 4.12.0."},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "CONVENTIONS.md"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a",
             "content": "CONVENTIONS.md\n# Release conventions\n"
                        "VERSION carries a -quartz suffix. CHANGES.md gains "
                        "`rel <version> :: <summary>` and nothing else."}]},
    ]
    written = skills.extract(messages, "Read CONVENTIONS.md, then cut release 4.12.0.")
    assert written == ["conventions"]
    assert "-quartz" in skills.load_skill("conventions")
    assert "conventions" in skills.catalogue()


def test_extract_skips_a_document_too_short_to_carry_a_procedure(monkeypatch):
    monkeypatch.setattr(config, "SKILL_EXTRACTION", True)
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "a.txt"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "a.txt\n  1\talpha"}]},
    ]
    assert skills.extract(messages, "goal") == []


def test_extract_writes_skills_from_documents_not_from_source(monkeypatch):
    """A skill is a procedure written for people. `ship.py` became a skill on
    skill-correction (2026-09-09, worked around by neutralising its docstring)
    and `calc.py` became one on author-testname (2026-09-13). A source file's
    first line is its docstring or an import, and a skill made from it
    out-matches the document the case exists to test."""
    monkeypatch.setattr(config, "SKILL_EXTRACTION", True)
    procedure = "Write VERSION with the suffix, then run the script. " * 6
    docs = [("ship.py", '"""Cut a release."""' + chr(10) + "import hashlib" + chr(10) + procedure),
            ("config.toml", "[release]" + chr(10) + procedure),
            ("RUNBOOK.md", "# Release checklist" + chr(10) + procedure),
            ("CONVENTIONS", "Conventions" + chr(10) + procedure),
            ("notes.txt", "Notes" + chr(10) + procedure)]
    messages = []
    for i, (path, text) in enumerate(docs):
        n = text.count(chr(10)) + 1
        messages += [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"r{i}", "name": "read_file", "input": {"path": path}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"r{i}",
                 "content": f"{path} (lines 1-{n} of {n})" + chr(10) + text}]},
        ]
    written = skills.extract(messages, "cut a release")
    assert set(written) == {"runbook", "conventions", "notes"}, written


def test_extract_truncates_rather_than_overflowing_the_index(monkeypatch):
    """An unbounded extract would eventually breach SKILLS_INDEX_CHARS, which is
    fatal by design - so the bound belongs here, before the file is written."""
    monkeypatch.setattr(config, "SKILL_EXTRACTION", True)
    monkeypatch.setattr(config, "EXTRACT_MAX_CHARS", 200)
    huge = "rules.md\n" + ("a procedure line that repeats. " * 100)
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "rules.md"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": huge}]},
    ]
    assert skills.extract(messages, "goal") == ["rules"]
    assert len(skills.load_skill("rules")) < 500


def test_extract_is_a_no_op_when_switched_off(monkeypatch):
    """A capability that cannot be turned off cannot be attributed either."""
    monkeypatch.setattr(config, "SKILL_EXTRACTION", False)
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "CONVENTIONS.md"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a",
             "content": "CONVENTIONS.md\n" + "x" * 200}]},
    ]
    assert skills.extract(messages, "goal") == []
    assert skills.authored() == []


def test_extract_respects_the_library_cap(monkeypatch):
    """learn() raises at the cap; extract must absorb that rather than crash a run
    that had otherwise succeeded."""
    monkeypatch.setattr(config, "SKILL_EXTRACTION", True)
    monkeypatch.setattr(config, "MAX_AUTHORED_SKILLS", 1)
    skills.learn(name="already", description="Use when already.", body="step")
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "second.md"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a",
             "content": "second.md\n" + "a real procedure line. " * 10}]},
    ]
    assert skills.extract(messages, "goal") == []
    assert skills.authored() == ["already"]


def test_the_extracted_body_has_no_line_number_gutter(monkeypatch):
    """read_file numbers every line for the model's benefit. Those numbers are
    formatting, not knowledge, and they must not end up inside a skill."""
    monkeypatch.setattr(config, "SKILL_EXTRACTION", True)
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "CONVENTIONS.md"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a",
             "content": "CONVENTIONS.md (lines 1-3 of 3)\n"
                        "     1\t# The one rule\n"
                        "     2\tEvery file must open with `# owner: unassigned`.\n"
                        "     3\tThat is the whole convention, and nothing else."}]},
    ]
    skills.extract(messages, "goal")
    body = skills.load_skill("conventions")
    assert "# The one rule" in body
    assert "owner: unassigned" in body
    assert "\t" not in body, "the line-number gutter survived into the skill"
    assert "     1" not in body


def test_the_loops_repeat_notice_is_not_knowledge(monkeypatch):
    """Measured 2026-09-11, skill-correction run 0: the `version` skill's body
    was `2.0` plus "[This exact read_file call has now returned the same result
    2 times ...]". The loop appends that notice to a repeated tool result, and
    it carried a one-line file over the 80-char floor. A notice the loop wrote
    to the model is not part of what the file says."""
    monkeypatch.setattr(config, "SKILL_EXTRACTION", True)
    notice = ("[This exact read_file call has now returned the same result 2 "
              "times. Use the result you already have - repeating it unchanged "
              "will end the run.]")
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "VERSION"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a",
             "content": "VERSION (lines 1-1 of 1)" + chr(10) + "     1" + chr(9)
                        + "2.0" + chr(10) + chr(10) + notice}]},
    ]
    assert skills._undecorate(messages[1]["content"][0]["content"]) == "2.0"
    assert skills.extract(messages, "cut a release") == [], (
        "a one-line file became a skill on the strength of a loop notice")


def test_the_description_says_WHEN_not_what_the_agent_happened_to_be_doing(monkeypatch):
    """The description is all a later session sees until it opens the skill, so it
    must describe a CLASS of work. Derived from the goal it named one specific task
    ('create b.txt containing beta'), and a later task asking for c.txt matched
    nothing - measured: extracted and indexed, never loaded."""
    monkeypatch.setattr(config, "SKILL_EXTRACTION", True)
    goal = "Read CONVENTIONS.md, then create a.txt's companion file b.txt containing beta."
    messages = [
        {"role": "user", "content": goal},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "CONVENTIONS.md"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a",
             "content": "CONVENTIONS.md\n# The one rule\n"
                        "Every file must open with `# owner: unassigned`. That is all."}]},
    ]
    skills.extract(messages, goal)
    description = skills.catalogue()["conventions"]["description"]
    assert "b.txt" not in description and "beta" not in description
    assert "CONVENTIONS.md" in description

# =================== the matching skill is OPENED, not offered (measured 92/11)


def _library(tmp_path, monkeypatch, *entries):
    """Build a skill library from (name, description) pairs."""
    from agent import config

    root = tmp_path / 'lib'
    for name, description in entries:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / 'SKILL.md').write_text(
            '---' + chr(10) + f'name: {name}' + chr(10)
            + f'description: {description}' + chr(10) + '---' + chr(10)
            + f'The body of {name}.' + chr(10), encoding='utf-8')
    monkeypatch.setattr(config, 'SKILLS_DIRS', (root,))
    return root


def test_the_matching_skill_is_found(tmp_path, monkeypatch):
    from agent import skills

    _library(tmp_path, monkeypatch,
             ('qz-release', 'Use when asked to cut, tag or prepare a release.'),
             ('qz-deps', 'Use when asked to record a dependency.'))

    assert skills.best_match('Cut release 4.14.0')['name'] == 'qz-release'
    assert skills.best_match('Record a dependency on attrs')['name'] == 'qz-deps'


def test_an_unrelated_goal_matches_NOTHING(tmp_path, monkeypatch):
    """Injecting the WRONG skill is worse than injecting none - the agent then
    follows a convention that does not apply."""
    from agent import skills

    _library(tmp_path, monkeypatch,
             ('qz-release', 'Use when asked to cut, tag or prepare a release.'),
             ('ashgrove-config', 'Use when asked to create or edit a config file'
                                 ' for an Ashgrove service.'))

    assert skills.best_match('What is the weather like today?') is None
    assert skills.best_match('Write a shopping list to notes.txt') is None


def _written_at(root, name, when):
    """Set one skill's write time. Recency is the tie-break, and a test that
    leaves it to how fast the filesystem ticks is a test that flaps."""
    import os

    path = root / name / 'SKILL.md'
    os.utime(path, (when, when))


def test_a_tie_goes_to_the_most_recently_written(tmp_path, monkeypatch):
    """Two skills equally entitled to the goal: the newer one wins.

    AMENDED 2026-09-23. This asserted None - "a tie is not a match" - and that
    rule is what made the revision split's control arm score 1/3: `conventions`
    (stale) and `runbook` (current) tie on "Cut release 2.3." and NOTHING was
    injected. The newer skill was written from the more recently read document,
    which is the better evidence.

    SIX skills, not two. With two, `spread` for the shared word is 2 and the
    ceiling is 1, so the word is dropped as furniture and the function returns
    None before the tie branch is ever reached - the first version of this test
    passed with the tie check DELETED. Found by mutation.
    """
    from agent import skills

    root = _library(tmp_path, monkeypatch,
                    ('alpha-thing', 'Use when polishing a widget.'),
                    ('beta-thing', 'Use when polishing a widget.'),
                    ('gamma-one', 'Use when filing invoices.'),
                    ('delta-one', 'Use when booking travel.'),
                    ('epsilon-one', 'Use when drafting letters.'),
                    ('zeta-one', 'Use when sorting photographs.'))
    _written_at(root, 'alpha-thing', 1_000_000)
    _written_at(root, 'beta-thing', 2_000_000)

    assert skills.best_match('polishing a widget')['name'] == 'beta-thing'


def test_two_skills_written_at_the_same_moment_are_still_no_match(
        tmp_path, monkeypatch):
    """Recency breaks a tie; it does not abolish one. Without this the amendment
    above becomes "always inject something", and injecting the WRONG skill is
    worse than injecting none - the agent then follows a convention that does
    not apply."""
    from agent import skills

    root = _library(tmp_path, monkeypatch,
                    ('alpha-thing', 'Use when polishing a widget.'),
                    ('beta-thing', 'Use when polishing a widget.'),
                    ('gamma-one', 'Use when filing invoices.'),
                    ('delta-one', 'Use when booking travel.'),
                    ('epsilon-one', 'Use when drafting letters.'),
                    ('zeta-one', 'Use when sorting photographs.'))
    _written_at(root, 'alpha-thing', 1_500_000)
    _written_at(root, 'beta-thing', 1_500_000)

    assert skills.best_match('polishing a widget') is None


def test_a_THREE_letter_word_still_counts(tmp_path, monkeypatch):
    """A four-character floor dropped `cut`, `tag` and `txt`, and "Cut release"
    then shared one rare word with the release skill - a tie, so nothing was
    injected. Swept over the fixture library: a 3-character floor scores 9/9 with
    no wrong match, a 4-character floor scores 6 and mismatches twice."""
    from agent import skills

    # The ONLY shared words are three letters long. With a 4-character floor the
    # goal shares nothing and no skill is injected; the first version of this test
    # also shared `release`, so it passed either way. Found by mutation.
    _library(tmp_path, monkeypatch,
             ('qz-release', 'Use when asked to cut or tag a version.'),
             ('qz-deps', 'Use when asked to record a dependency.'),
             ('qz-lint', 'Use when asked to check formatting.'),
             ('qz-mail', 'Use when asked to send correspondence.'))

    assert skills.best_match('cut and tag it')['name'] == 'qz-release'


def test_opening_returns_the_body_ready_for_the_prompt(tmp_path, monkeypatch):
    from agent import skills

    _library(tmp_path, monkeypatch,
             ('qz-release', 'Use when asked to cut, tag or prepare a release.'))
    opened = skills.opening('Cut release 4.14.0')

    assert 'The body of qz-release' in opened
    assert 'Follow it' in opened


def test_opening_is_empty_when_nothing_matches(tmp_path, monkeypatch):
    from agent import skills

    _library(tmp_path, monkeypatch,
             ('qz-release', 'Use when asked to cut, tag or prepare a release.'))
    assert skills.opening('What is the weather like today?') == ''


def test_it_can_be_turned_off(tmp_path, monkeypatch):
    """Revertable without touching the loop, which is what makes it measurable."""
    from agent import config, skills

    _library(tmp_path, monkeypatch,
             ('qz-release', 'Use when asked to cut, tag or prepare a release.'))
    monkeypatch.setattr(config, 'AUTO_SKILL', False)

    assert skills.opening('Cut release 4.14.0') == ''


def test_a_broken_skill_does_not_end_the_run(tmp_path, monkeypatch):
    from agent import skills

    _library(tmp_path, monkeypatch,
             ('qz-release', 'Use when asked to cut, tag or prepare a release.'))
    monkeypatch.setattr(skills, 'load_skill',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('gone')))

    assert skills.opening('Cut release 4.14.0') == ''
# ================================ correction by replacement (Phase R)

def _read_but_not_edited(path, text):
    """A transcript in which `path` was READ and never written - the only shape
    `extract` acts on. Same message shape the extract tests above already build."""
    return [
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'a', 'name': 'read_file',
             'input': {'path': path}}]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'a',
             'content': path + chr(10) + text}]},
    ]


def _read_several(*docs):
    """Several documents read and never written, in the order given. The result
    carries read_file's real header, which _undecorate strips - without it the
    first line of every body is the PATH and nothing distinguishes them."""
    out = []
    for i, (path, text) in enumerate(docs):
        cid = f"r{i}"
        n = text.count(chr(10)) + 1
        out += [
            {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': cid, 'name': 'read_file',
                 'input': {'path': path}}]},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': cid,
                 'content': f"{path} (lines 1-{n} of {n})" + chr(10) + text}]},
        ]
    return out
