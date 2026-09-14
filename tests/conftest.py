"""Shared fixtures.

`tmp_workspace` redirects the workspace root at a temp directory. It patches the
attributes on `agent.config` itself, which only works because every module reads
`config.WORKSPACE` late rather than binding the name at import time.
"""
import importlib.util
import pathlib
import sys

import pytest

from agent import config

# `eval/` is not a package, so the harness is loaded by path. Registered HERE
# rather than in test_harness.py: a test importing `eval_harness` used to pass
# only when that file happened to be collected first, so running its own file
# alone failed. Ordering is not isolation.
if "eval_harness" not in sys.modules:
    _PATH = pathlib.Path(__file__).resolve().parent.parent / "eval" / "harness.py"
    _spec = importlib.util.spec_from_file_location("eval_harness", _PATH)
    _harness = importlib.util.module_from_spec(_spec)
    sys.modules["eval_harness"] = _harness
    _spec.loader.exec_module(_harness)


@pytest.fixture(autouse=True)
def _no_real_pacing(monkeypatch):
    """FR-505 paces every host by 2 real seconds. AUTOUSE, because a unit suite
    must not sleep: seven web_search tests were costing 12s of wall clock waiting
    on a courtesy interval that only matters against a live server.

    The pacing itself is still tested - test_pacing_waits_between_hits_on_one_host
    patches time.sleep and asserts the interval directly.
    """
    from agent import tools

    monkeypatch.setattr(tools.time, "sleep", lambda _seconds: None)
    tools._LAST_HIT.clear()


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Every test gets its own agent home. AUTOUSE - isolation is opt-OUT.

    A hermetic environment should be autouse rather than a fixture each test
    must remember to request. Ours was opt-in, and a
    test that forgot it wrote to the REAL TASKS_DB: three tests passed alone
    and failed together, which reads as a code bug and is not one.
    """
    home = tmp_path / "_state"
    monkeypatch.setattr(config, "AGENT_HOME", home)
    monkeypatch.setattr(config, "STATE_DB", home / "state.db")
    monkeypatch.setattr(config, "MEMORY_DB", home / "memory.db")
    monkeypatch.setattr(config, "TASKS_DB", home / "tasks.db")
    monkeypatch.setattr(config, "PROFILE", home / "AGENT.md")
    monkeypatch.setattr(config, "NOW", home / "NOW.md")


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    """Point the workspace and artifacts roots at a temp directory."""
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "ARTIFACTS", tmp_path / ".agent" / "artifacts")
    return tmp_path


@pytest.fixture
def fresh_app(tmp_workspace, monkeypatch):
    """A compiled graph with its own checkpoint database, rebuilt for this test.

    `get_app()` memoises, so the cached instance is cleared first; monkeypatch
    restores it afterwards.
    """
    from agent import graph

    monkeypatch.setattr(config, "STATE_DB", tmp_workspace / ".agent" / "state.db")
    # Memory too (Phase M). `finish` now writes an episode and `act` reads one back,
    # so without redirecting these the unit suite would read and write the REAL
    # agent home - contaminating a developer's own memory and, worse, making a test
    # pass or fail depending on what happened in an unrelated session.
    monkeypatch.setattr(config, "MEMORY_DB", tmp_workspace / ".agent" / "memory.db")
    monkeypatch.setattr(config, "PROFILE", tmp_workspace / ".agent" / "AGENT.md")
    monkeypatch.setattr(config, "NOW", tmp_workspace / ".agent" / "NOW.md")
    monkeypatch.setattr(graph, "_APP", None)
    return graph.get_app()
