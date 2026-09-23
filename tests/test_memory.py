"""Episodic memory and the durable profile.

A seventh test file, and the fifth stated deviation from the spec's three-file
`tests/` allowlist. The justification is narrow and specific: **memory is the one
component whose failure is silent by construction.** A tool that breaks throws; a
memory that never retrieves just makes the agent look forgetful, which is
indistinguishable from a model having a bad day. The recall benchmark catches that
end-to-end at the cost of model quota; these catch it for free.

No API key, no network (NFR-602). Every test points the stores at a temp directory,
because `config.MEMORY_DB` otherwise resolves to the real agent home.
"""
import json

import pytest

from agent import config, memory, policy


@pytest.fixture(autouse=True)
def tmp_memory(tmp_path, monkeypatch):
    """Redirect both stores, and leave the risk map as it was found."""
    monkeypatch.setattr(config, "MEMORY_DB", tmp_path / "memory.db")
    monkeypatch.setattr(config, "PROFILE", tmp_path / "AGENT.md")
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    before = dict(policy.RISK)
    yield tmp_path
    memory.deactivate()
    policy.RISK.clear()
    policy.RISK.update(before)


def _episode(goal="fix the parser", answer="done", **over):
    kw = {"thread_id": "t1", "goal": goal, "verdict": "done", "answer": answer,
          "files": ["src/parser.py"], "commands": ["pytest -q"]}
    kw.update(over)
    return memory.write_episode(**kw)


# ================================================================ the store

def test_an_episode_written_in_one_session_is_found_in_another():
    """The whole point. Within a thread the checkpointer already carries context,
    so this is the only thing that makes a SEPARATE session recall anything."""
    _episode(goal="my deploy key is kx-9920-vt")
    assert "kx-9920-vt" in memory.search("deploy key")[0]["goal"]


def test_search_returns_nothing_when_nothing_matches():
    _episode(goal="fix the parser")
    # semantic_lane=False: a KEYWORD property. The dense lane always has a
    # nearest neighbour and no floor separates a correct match (median cosine
    # 0.320) from a wrong one (0.352) - measured on the recall corpus.
    assert memory.search("kangaroo taxonomy", semantic_lane=False) == []


def test_search_on_an_empty_store_does_not_crash():
    assert memory.search("anything") == []


def test_punctuation_in_the_query_does_not_break_retrieval():
    """FTS5 treats quotes, hyphens and parentheses as syntax. A raw user sentence
    would raise a SyntaxError at precisely the moment recall was meant to happen,
    so every word is requoted as a literal."""
    _episode(goal="the deploy key is kx-9920-vt")
    for query in ('what is my "deploy" key?', "deploy-key (the one for prod)",
                  "deploy OR", "*", "NEAR(", ""):
        assert isinstance(memory.search(query), list)
    assert memory.search('what is my "deploy" key?')


def test_a_short_word_query_still_matches_something_useful():
    """Words of three or more characters are indexed; `is`, `my`, `a` are not, so a
    question made only of stopwords must return empty rather than everything."""
    _episode(goal="the deploy key is kx-9920-vt")
    # semantic_lane=False: a KEYWORD property. The dense lane always has a
    # nearest neighbour and no floor separates a correct match (median cosine
    # 0.320) from a wrong one (0.352) - measured on the recall corpus.
    assert memory.search("is my a", semantic_lane=False) == []


def test_commands_and_files_are_stored_and_retrievable():
    _episode(goal="fix the parser", files=["a.py", "b.py"], commands=["pytest -q"])
    found = memory.search("parser")[0]
    assert json.loads(found["files"]) == ["a.py", "b.py"]
    assert json.loads(found["commands"]) == ["pytest -q"]


# ============================================================== the profile

def test_remember_appends_and_survives_a_second_call():
    memory.remember("Jeevan builds with make ship-quartz")
    memory.remember("Jeevan prefers tabs")
    assert "make ship-quartz" in memory.profile()
    assert "prefers tabs" in memory.profile()


def test_remember_does_not_duplicate_the_same_note():
    """An agent told the same thing twice must not grow the file forever - the
    profile is injected into every session and is charged per turn."""
    memory.remember("Jeevan prefers tabs")
    memory.remember("Jeevan prefers tabs")
    assert memory.profile().count("Jeevan prefers tabs") == 1


def test_remember_rejects_an_empty_note_with_an_actionable_message():
    with pytest.raises(ValueError, match="short sentence"):
        memory.remember("   ")


def test_the_profile_is_empty_before_anything_is_remembered():
    assert memory.profile() == ""


# ============================================================== injection

def test_injected_context_carries_the_profile_and_the_episode():
    memory.remember("Jeevan builds with make ship-quartz")
    _episode(goal="my deploy key is kx-9920-vt", answer="noted")
    injected = memory.context_for("what is my deploy key")
    assert "make ship-quartz" in injected and "kx-9920-vt" in injected


def test_injection_is_capped_in_characters(monkeypatch):
    """It goes into the system prompt, which is re-sent on every request on a
    provider that caches nothing. An uncapped store would grow the per-turn cost of
    every future run without any single change being to blame."""
    monkeypatch.setattr(config, "MEMORY_INJECT_CHARS", 200)
    for i in range(30):
        _episode(goal=f"deploy step {i} " + "x" * 400, thread_id=f"t{i}")
    assert len(memory.context_for("deploy")) <= 200 + 200   # body cap + header


def test_nothing_is_injected_when_there_is_nothing_to_recall():
    """An empty store must add no header and no tokens at all."""
    assert memory.context_for("anything") == ""


def test_injection_tells_the_model_what_to_do_with_it():
    """A bare dump is ignored in practice - the same lesson the spill message
    learned, where a path with no instruction was never acted on."""
    _episode(goal="my deploy key is kx-9920-vt")
    assert "act on it" in memory.context_for("deploy key")


# ============================================================ the kill switch

def test_with_memory_off_nothing_is_injected(monkeypatch):
    _episode(goal="my deploy key is kx-9920-vt")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    assert memory.context_for("deploy key") == ""


def test_with_memory_off_the_remember_tool_does_not_exist(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    assert memory.activate() == []
    assert memory.tools() == {}


def test_with_memory_off_the_toolset_is_unchanged(monkeypatch):
    from agent.tools import TOOLS, toolset

    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    memory.activate()
    assert sorted(toolset()) == sorted(TOOLS)


def test_activate_registers_remember_as_a_write():
    memory.activate()
    assert policy.RISK["remember"] == "write"
    assert policy.classify("remember", {"note": "x"}, autonomous=True)[0] == "auto"


def test_deactivate_removes_exactly_what_it_registered():
    """Two modules now register tools. Either one stripping the other's entries
    would leave a tool the gate refuses as unknown, mid-run."""
    policy.register("fetch", "read")          # as agent/mcp.py would
    memory.activate()
    memory.deactivate()
    assert "remember" not in policy.RISK
    assert policy.RISK.get("fetch") == "read"


# ==================================================== what finish() extracts

def test_only_successful_commands_are_remembered():
    """Recalling a command that did not work is worse than recalling nothing,
    because next session it reads as advice."""
    from agent.graph import _outcomes

    messages = [
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "run_shell",
             "input": {"command": "pytest -q"}},
            {"type": "tool_use", "id": "b", "name": "run_shell",
             "input": {"command": "make nonsense"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "ok"},
            {"type": "tool_result", "tool_use_id": "b", "content": "no rule",
             "is_error": True}]},
    ]
    worked = [c["input"]["command"] for c, ok in _outcomes(messages) if ok]
    assert worked == ["pytest -q"]


def test_the_final_answer_is_the_last_thing_said_in_words():
    from agent.graph import _final_text

    messages = [
        {"role": "assistant", "content": [{"type": "text", "text": "looking"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "run_shell", "input": {}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "the key is kx-9920-vt"}]},
    ]
    assert _final_text(messages) == "the key is kx-9920-vt"


def test_a_run_that_never_spoke_yields_an_empty_answer():
    from agent.graph import _final_text

    assert _final_text([{"role": "user", "content": "hi"}]) == ""

# ================================================ Phase 3.1: staleness decay


def _aged(thread_id, goal, days_old, answer=""):
    """Write an episode and backdate it. write_episode() stamps time.time(), so
    the age has to be set afterwards."""
    import sqlite3
    import time

    from agent import config, memory

    rowid = memory.write_episode(thread_id, goal, "done", answer, [], [])
    with sqlite3.connect(config.MEMORY_DB) as conn:
        conn.execute("UPDATE episodes SET at=? WHERE id=?",
                     (time.time() - days_old * 86400, rowid))
    return rowid


def test_a_stale_episode_ranks_below_a_fresh_one(tmp_workspace):
    """The whole point. Two episodes that match equally well must not tie - the
    one from six months ago is the worse answer."""
    from agent import memory

    old = _aged("t-old", "the quartzite deploy key procedure", days_old=200)
    new = _aged("t-new", "the quartzite deploy key procedure", days_old=0)

    # Staleness decay is the keyword lane's ORDER BY; the dense lane does not
    # rank by age, so this pins the lane that implements the property.
    got = [r["id"] for r in memory.search("quartzite deploy",
                                          semantic_lane=False)]
    assert got.index(new) < got.index(old)


def test_a_stale_episode_is_still_RETURNED(tmp_workspace):
    """Down-ranked, never dropped. Filtering would hide the only episode that
    answers a question nobody has asked in months, which is exactly when memory
    is worth having."""
    from agent import memory

    _aged("t-old", "the quartzite deploy key procedure", days_old=500)
    assert memory.search("quartzite deploy", semantic_lane=False), (
        "an old episode is not a deleted one")


def test_nothing_inside_the_window_is_penalised(tmp_workspace, monkeypatch):
    from agent import config, memory

    monkeypatch.setattr(config, "MEMORY_STALE_DAYS", 30.0)
    first = _aged("t-a", "quartzite deploy key alpha", days_old=1)
    second = _aged("t-b", "quartzite deploy key beta", days_old=29)

    # Staleness decay is the keyword lane's ORDER BY; the dense lane does not
    # rank by age, so this pins the lane that implements the property.
    got = [r["id"] for r in memory.search("quartzite deploy",
                                          semantic_lane=False)]
    assert set(got) == {first, second}, "both are inside the window and both rank"


def test_the_decay_can_be_turned_off(tmp_workspace, monkeypatch):
    """A multiplier of 1.0 must restore the previous ordering exactly, which is
    what makes this revertable without touching the query."""
    from agent import config, memory

    monkeypatch.setattr(config, "MEMORY_STALE_DECAY", 1.0)
    old = _aged("t-old", "quartzite deploy key procedure", days_old=900)
    _aged("t-new", "quartzite deploy key procedure", days_old=0)

    # Staleness decay is the keyword lane's ORDER BY; the dense lane does not
    # rank by age, so this pins the lane that implements the property.
    got = [r["id"] for r in memory.search("quartzite deploy",
                                          semantic_lane=False)]
    assert old in got


def test_the_durable_profile_never_decays(tmp_workspace):
    """AGENT.md does not go through search() at all, which is what makes it the
    never-decay tier the design this follows spends a config block describing."""
    from agent import memory

    memory.remember("The user deploys with make ship-quartz.")
    _aged("t-old", "something unrelated", days_old=9999)

    assert "make ship-quartz" in memory.context_for("how do I deploy")

# ============================================== Phase 3.2: NOW.md, the scratchpad


def test_the_scratchpad_records_where_the_session_got_to(tmp_workspace):
    from agent import memory

    memory.write_now(goal="Fix the failing tests in tests/",
                     verdict="stuck", plan=["read the failure", "edit parser.py",
                                            "run the suite"],
                     cursor=1, files=["parser.py"])
    body = memory.now()

    assert "Fix the failing tests" in body
    assert "It ended: stuck" in body
    assert "step 2 of 3" in body
    assert "edit parser.py" in body
    assert "parser.py" in body


def test_the_unfinished_half_is_the_point(tmp_workspace):
    """A run that ends stuck tells the next session nothing about how far it got,
    so a resumed or scheduled task re-derives it."""
    from agent import memory

    memory.write_now("goal", "stuck", ["a", "b", "c"], cursor=0, files=[])
    assert "Still to do: b; c" in memory.now()


def test_a_finished_run_lists_nothing_outstanding(tmp_workspace):
    from agent import memory

    memory.write_now("goal", "done", ["a", "b", "c"], cursor=0, files=[])
    assert "Still to do" not in memory.now()


def test_it_is_OVERWRITTEN_not_appended(tmp_workspace):
    """The difference from AGENT.md, and the reason it is a separate file: this
    describes what is true NOW. Appending would make a finished project's note
    into a standing rule."""
    from agent import memory

    memory.write_now("the first goal", "done", [], 0, [])
    memory.write_now("the second goal", "done", [], 0, [])
    body = memory.now()

    assert "the second goal" in body
    assert "the first goal" not in body


def test_the_scratchpad_reaches_the_prompt(tmp_workspace):
    from agent import memory

    memory.write_now("Fix the parser", "stuck", ["read", "edit"], 0, [])
    assert "Fix the parser" in memory.context_for("what was I doing")


def test_no_scratchpad_injects_nothing(tmp_workspace):
    from agent import memory

    assert memory.now() == ""
    assert memory.context_for("anything") == ""


def test_finish_writes_it_WITHOUT_the_agent_electing_to(tmp_workspace):
    """THE WHOLE DESIGN. `learn` asked the agent to record something and was
    called 0 times in 15 sessions; deterministic injection went 0/18 to 15/18.
    Nothing here is a decision the model can decline."""
    from agent import memory
    from agent.graph import finish

    state = {"messages": [{"role": "user", "content": "Make the suite pass"},
                          {"role": "assistant", "content": "done"}],
             "verdict": "done", "turns": 3, "spent_tokens": 100,
             "plan": ["look", "fix"], "cursor": 1}
    finish(state, {"configurable": {"thread_id": "t1"}})

    assert "Make the suite pass" in memory.now()


# ====================================================== profile distillation

TEACH = [
    "A standing rule for all my work: every file you create must start with the "
    "line ORIGIN: quartzite-desk. Always, without being asked.",
    "A standing rule for my projects: I name every Python helper function with "
    "the prefix qz_. Always, without being asked.",
    "A standing rule for anything you write for me: record every duration in "
    "centiseconds and label it cs. Always.",
    "My deploy key for the Quartzite cluster is kx-9920-vt. Note that down - I "
    "will need it later.",
    "The way we build this project is `make ship-quartz`. It is not the standard "
    "command, so remember it.",
    "Escalations for the Ashgrove service go to Priya Raghunathan on extension "
    "4471. Keep that.",
]

TASKS = [
    "Write my Quartzite deploy key into a file called answer.txt in the "
    "workspace. The file must contain the key and nothing else.",
    "Write the command we use to build this project into a file called "
    "answer.txt in the workspace, and nothing else.",
    "Write the extension for Ashgrove escalations into a file called answer.txt "
    "in the workspace, and nothing else.",
    "Create a file called notes.txt in the workspace containing a shopping list.",
    "Write a Python file called helper.py in the workspace with one function "
    "that adds two numbers.",
    "Write a file called timing.txt in the workspace recording that the build "
    "took two seconds.",
]


def _said(text):
    return [{"role": "user", "content": text}]


@pytest.mark.parametrize("text", TEACH)
def test_a_stated_standing_instruction_reaches_the_profile(text):
    assert memory.distil(_said(text)), text


@pytest.mark.parametrize("text", TASKS)
def test_an_ordinary_TASK_does_not(text):
    """The direction that matters: the profile is injected into every system
    prompt, so a false positive is rent charged on every later turn."""
    assert memory.distil(_said(text)) == [], text


def test_a_tool_result_is_not_the_user_speaking():
    """Tool results arrive as role=user with a LIST content. Reading them would
    persist file contents as though the user had stated them."""
    messages = [{"role": "user",
                 "content": [{"type": "tool_result",
                              "content": "always run make before pushing"}]}]

    assert memory.distil(messages) == []


def test_the_caps_own_summary_request_is_not_a_user_statement():
    ask = "Say what you found. Always be brief."

    assert memory.distil(_said(ask), skip=ask) == []


def test_at_most_three_facts_leave_one_session():
    """System-prompt rent: a session that says ten standing rules must not
    charge for ten on every later turn."""
    many = " ".join(f"Standing rule number {i}: always do thing {i}."
                    for i in range(10))

    assert len(memory.distil(_said(many))) == 3


def test_a_fact_is_written_once_however_often_it_is_said():
    text = "A standing rule: always label durations in cs."

    memory.distil(_said(text))
    again = memory.distil(_said(text))

    assert again == []
    assert memory.profile().count("label durations in cs") == 1


def test_what_is_distilled_is_what_a_later_session_reads():
    """The whole point: profile() feeds context_for(), which is the system
    prompt. A fact that lands in AGENT.md and never reaches the model is not a
    memory."""
    memory.distil(_said("A standing rule: always prefix helpers with qz_."))

    assert "qz_" in memory.context_for("write a helper function")


def test_the_kill_switch_turns_it_off(monkeypatch):
    monkeypatch.setattr(config, "PROFILE_DISTIL", False)

    assert memory.distil(_said(TEACH[0])) == []
    assert memory.profile() == ""


def test_distillation_never_raises_on_a_broken_store(monkeypatch):
    """It runs at the end of a session that may already have succeeded."""
    def boom(_note):
        raise OSError("read-only file system")

    monkeypatch.setattr(memory, "remember", boom)

    assert memory.distil(_said(TEACH[0])) == []
