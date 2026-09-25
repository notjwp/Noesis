"""The read-only viewer (section 11, amended 2026-09-10).

Section 11 permitted this on three conditions, and the tests here are those
conditions rather than a description of the code: loopback only, a secret on
every request, and no way to change anything. A viewer that grew a write path
would be a second route to a side effect, past the gate.
"""
import json

import pytest

from agent import config, viewer


def _handler(monkeypatch, secret="s" * 32):
    """A handler wired to a known secret, without binding a port."""
    monkeypatch.setattr(viewer, "_secret", lambda: secret)
    return secret


# ================================================================ the boundary

def test_the_viewer_binds_loopback_and_not_every_interface():
    """A page serving traces, goals and workspace paths must not be reachable
    off this machine. Asserted on the CONSTANT, because the failure mode is a
    later edit making it configurable and someone setting 0.0.0.0."""
    assert config.VIEWER_HOST == "127.0.0.1"
    assert "0.0.0.0" not in config.VIEWER_HOST


def test_the_host_is_not_reachable_from_the_environment(monkeypatch):
    """The port is a tunable because ports collide. The host is not, because an
    env var is how loopback becomes every interface by accident."""
    monkeypatch.setenv("AGENT_VIEWER_HOST", "0.0.0.0")
    import importlib

    reloaded = importlib.reload(config)
    try:
        assert reloaded.VIEWER_HOST == "127.0.0.1"
    finally:
        monkeypatch.delenv("AGENT_VIEWER_HOST", raising=False)
        importlib.reload(config)


# =================================================================== the guard

def test_a_request_with_no_secret_is_refused(monkeypatch):
    _handler(monkeypatch)
    status, _, _ = viewer.serve_path("/api/tasks", token="")
    assert status == 401


def test_a_request_with_the_wrong_secret_is_refused(monkeypatch):
    _handler(monkeypatch, secret="a" * 32)
    status, _, _ = viewer.serve_path("/api/tasks", token="b" * 32)
    assert status == 401


def test_a_request_with_the_right_secret_is_served(monkeypatch):
    secret = _handler(monkeypatch)
    status, _, _ = viewer.serve_path("/api/tasks", token=secret)
    assert status == 200


def test_the_secret_is_compared_in_constant_time():
    """A plain == leaks length and prefix through timing. hmac.compare_digest is
    the one-line fix and the only reason to check the source: the behaviour is
    identical either way, which is what makes it easy to lose in a refactor."""
    import inspect

    assert "compare_digest" in inspect.getsource(viewer._authorised)


def test_a_minted_secret_is_long_enough_to_be_worth_guarding(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(config, "VIEWER_SECRET", tmp_path / "viewer.secret")
    first = viewer._secret()

    assert len(first) >= 32
    assert viewer._secret() == first, "it must be stable, not minted per call"


# ============================================================== read-only only

def test_the_viewer_exposes_no_write_method():
    """Section 11's condition: it may not start, stop, approve or configure
    anything, so the gate stays the only path to a side effect. Enumerated from
    the handler rather than trusted, because a do_POST added later would
    otherwise be silent."""
    methods = {n for n in dir(viewer.Handler) if n.startswith("do_")}

    assert methods == {"do_GET"}, f"a write path appeared: {methods - {'do_GET'}}"


def test_an_unknown_path_is_not_found(monkeypatch):
    secret = _handler(monkeypatch)
    status, _, _ = viewer.serve_path("/api/nope", token=secret)
    assert status == 404


# ================================================================== redaction

def test_a_key_in_a_served_trace_is_redacted(monkeypatch, tmp_path):
    """Traces carry tool output, and tool output carries whatever the workspace
    had in it. The leak fixed in 191fe47 is asserted again HERE, at the new
    boundary, because a page is a second way for it to escape."""
    secret = _handler(monkeypatch)
    leaked = "nvapi-" + "K" * 40
    monkeypatch.setattr(viewer, "_runs",
                        lambda: [{"id": "x", "note": f"failed with {leaked}"}])

    status, _, body = viewer.serve_path("/api/runs", token=secret)

    assert status == 200
    assert leaked not in body
    assert "[redacted" in body


def test_every_api_payload_goes_through_redaction():
    """One chokepoint, asserted at the source. Redacting three of four routes is
    the same defect as redacting none, and it is invisible from the outside."""
    import inspect

    body = inspect.getsource(viewer.serve_path)
    assert "redact(" in body


# ==================================================================== payloads

def test_tasks_are_grouped_into_the_states_the_queue_already_has(monkeypatch):
    """The board. No new store and no new state - worker.tasks() already
    records status, and a column layout is a presentation of it."""
    secret = _handler(monkeypatch)
    monkeypatch.setattr(viewer, "_tasks", lambda: [
        {"id": "a", "status": "queued", "goal": "one"},
        {"id": "b", "status": "done", "goal": "two"},
        {"id": "c", "status": "queued", "goal": "three"},
    ])

    _, _, body = viewer.serve_path("/api/tasks", token=secret)
    board = json.loads(body)

    assert [t["id"] for t in board["queued"]] == ["a", "c"]
    assert [t["id"] for t in board["done"]] == ["b"]


def test_the_page_needs_no_build_step(monkeypatch):
    secret = _handler(monkeypatch)
    status, kind, body = viewer.serve_path("/", token=secret)

    assert status == 200
    assert "text/html" in kind
    assert "<" in body


def test_runs_are_found_relative_to_the_REPO_not_the_workspace(monkeypatch,
                                                               tmp_path):
    """Measured: it derived the path from config.WORKSPACE.parent, so against a
    repository holding sixty scored runs it served an empty list. The workspace
    is wherever the agent was pointed and has no eval/ in it."""
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "somewhere-else")

    import pathlib

    import agent.viewer as v

    repo = pathlib.Path(v.__file__).resolve().parent.parent
    if not (repo / "eval" / "runs").is_dir():
        pytest.skip("no recorded runs in this checkout")

    assert v._runs(), "the runs directory was not found from the repo"


def test_one_task_trace_is_served_and_redacted(monkeypatch, tmp_path):
    """A trace carries tool OUTPUT, so it must leave through the same redact
    chokepoint as every other route - checked with a key planted in a payload."""
    from agent import worker

    monkeypatch.setattr(viewer, "_secret", lambda: "s3cret")
    monkeypatch.setattr(worker, "events", lambda task_id: [
        {"seq": 1, "kind": "tool", "tool": "run_shell",
         "summary": "echo sk-ant-api03-DEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEF"}])

    status, kind, body = viewer.serve_path("/api/tasks/abc123/trace", "s3cret")

    assert status == 200 and kind == "application/json"
    assert "DEADBEEF" not in body
    assert "run_shell" in body


def test_a_trace_path_without_a_task_id_is_not_a_route(monkeypatch):
    monkeypatch.setattr(viewer, "_secret", lambda: "s3cret")

    assert viewer.serve_path("/api/tasks//trace", "s3cret")[0] == 404
    assert viewer.serve_path("/api/tasks/abc/traces", "s3cret")[0] == 404


def test_a_trace_is_refused_without_the_token(monkeypatch):
    monkeypatch.setattr(viewer, "_secret", lambda: "s3cret")

    assert viewer.serve_path("/api/tasks/abc/trace", "wrong")[0] == 401
