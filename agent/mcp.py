"""MCP client — a third party's tools, as a subprocess, behind the same gate.

§11's non-goal forbids "dynamic tool loading". Phase L amends it for servers that
meet three conditions, and this module is where two of them are enforced:

  1. Declared, never discovered: config.MCP_SERVERS names every server that may
     start, and the package is pinned in pyproject - a server cannot be
     introduced mid-run. In the eval image `/etc/pip.conf` sets no-index too.
  2. A subprocess of the agent. Natively that is the same user with the same
     access, and its tools go through the same gate as run_shell; in the eval
     container it inherits the mounts and network namespace, no new mechanism.
  3. Every tool risk-classified BEFORE its schema is shown to the model, by
     policy.register(), which refuses to default an unclassified tool to `read`.

Tools RAISE on failure and never return an error string - the execute node owns
the exception-to-observation conversion (FR-208), exactly as tools.py does.

CE-05: nothing here runs at import. `activate()` is called once per run.
"""
from __future__ import annotations

import asyncio
import atexit
import threading
from contextlib import AsyncExitStack

from agent import config, policy


class McpUnavailable(RuntimeError):
    """A server that would not start.

    Infrastructure failure is not a score: a run whose tools never came up measured
    nothing, and the harness reports it BLOCKED rather than counting it as a failure
    the agent earned.
    """


# Re-exported: this lived here while MCP was the only extra source of tools, which
# stopped being true when memory.py added one. Callers still catch
# `mcp.ToolBudgetExceeded`, so the name stays put and the definition moved.
from agent.registry import ToolBudgetExceeded  # noqa: E402,F401  (re-export)


_ACTIVE: dict[str, dict] = {}
_RUNNER: _Runner | None = None
# Names this module put into policy.RISK, so shutdown() removes exactly those
# and cannot strip a built-in's classification.
_REGISTERED: list[str] = []


class _Runner:
    """One event loop, on one thread, owning every MCP session for this run.

    LangGraph's nodes are synchronous and the MCP client is asyncio-only, so the
    loop lives on a thread of its own and calls are submitted into it.

    The obvious alternative - start the server, call it, shut it down, per call -
    was rejected on cost: it pays process startup on every single call, and a
    `fetch` that takes 200ms would spend more time starting Python than fetching.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.sessions: dict[str, object] = {}
        self.error: BaseException | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro, timeout: float):
        """Run a coroutine on the MCP loop and wait for it, bounded.

        The timeout is not optional. `run_shell` already carries this scar tissue:
        an unbounded check command held a scored suite for 25 minutes before it was
        killed by hand. A hung server is the same failure with a new name.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise TimeoutError(f"the MCP server did not answer within {timeout:g}s")

    async def _serve(self, servers: dict, ready: threading.Event) -> None:
        """Open every session, hold them, and close them all on one task.

        Opened and closed inside a SINGLE task deliberately: the underlying streams
        are managed by anyio task groups, which are task-affine, so entering a
        context on one task and exiting it on another is not safe.
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stop = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                for label, spec in servers.items():
                    command = spec["command"]
                    params = StdioServerParameters(command=command[0], args=list(command[1:]))
                    reader, writer = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(reader, writer))
                    await session.initialize()
                    self.sessions[label] = session
                ready.set()
                await self._stop.wait()
        except BaseException as exc:            # noqa: BLE001 - reported, not swallowed
            self.error = exc
        finally:
            ready.set()                         # never leave activate() hanging

    def stop(self) -> None:
        if self._stop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)


def _coerce(input_schema: dict, args: dict) -> dict:
    """Coerce arguments against the DECLARED types before the call leaves.

    A declared JSON schema is a hint to the model, not enforcement: `"max_length":
    5000` and `"max_length": "5000"` are both routinely emitted, and the second one
    once crashed every read_file call in a live session. tools.py learned this at
    its own boundary and coerces with _int().

    An MCP server offers no such forgiveness - it validates and rejects - so the
    symptom here would not be a crash but a tool that ALWAYS fails, which is far
    harder to attribute. A value that cannot be coerced is passed through unchanged
    so the server can say why; guessing would be worse than the server's own error.
    """
    properties = (input_schema or {}).get("properties", {})
    out = {}
    for key, value in args.items():
        want = properties.get(key, {}).get("type")
        try:
            if want == "integer" and not isinstance(value, bool):
                out[key] = int(value)
            elif want == "number" and not isinstance(value, bool):
                out[key] = float(value)
            elif want == "boolean" and isinstance(value, str):
                out[key] = value.strip().lower() in ("1", "true", "yes", "on")
            else:
                out[key] = value
        except (TypeError, ValueError):
            out[key] = value
    return out


def _text(result) -> str:
    """Flatten an MCP result to the plain string the execute node expects."""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else f"[{type(block).__name__}]")
    body = "\n".join(parts)
    if getattr(result, "isError", False):
        raise RuntimeError(body or "the MCP server reported an error")
    return body


def _bind(runner: _Runner, session, name: str, input_schema: dict):
    def call(**kwargs):
        return _text(runner.submit(
            session.call_tool(name, _coerce(input_schema, kwargs)),
            config.MCP_CALL_TIMEOUT))
    return call


def activate() -> list[str]:
    """Start the configured servers and register their tools. Returns tool names.

    A no-op when the kill switch is off, which must leave the agent byte-identical
    to the four-tool one: a capability that cannot be turned off cannot be
    attributed either.
    """
    global _RUNNER
    if not config.MCP_ENABLED or not config.MCP_SERVERS:
        return []
    if _RUNNER is not None:
        return sorted(_ACTIVE)

    runner = _Runner()
    ready = threading.Event()
    asyncio.run_coroutine_threadsafe(
        runner._serve(config.MCP_SERVERS, ready), runner.loop)
    if not ready.wait(config.MCP_STARTUP_TIMEOUT) or runner.error is not None:
        runner.stop()
        raise McpUnavailable(
            f"MCP server(s) {sorted(config.MCP_SERVERS)} did not start: "
            f"{runner.error or 'timed out'}")

    for label, session in runner.sessions.items():
        declared = config.MCP_SERVERS[label].get("risk", {})
        listing = runner.submit(session.list_tools(), config.MCP_STARTUP_TIMEOUT)
        for tool in listing.tools:
            # Classified BEFORE the schema is offered to the model. A tool the
            # server declares and config.py does not is recorded `destructive` by
            # register(), so it surfaces as an approval prompt rather than running.
            risk = policy.register(tool.name, declared.get(tool.name))
            _REGISTERED.append(tool.name)
            schema = {"name": tool.name,
                      "description": tool.description or "",
                      "input_schema": tool.inputSchema or {"type": "object"}}
            _ACTIVE[tool.name] = {
                "fn": _bind(runner, session, tool.name, tool.inputSchema or {}),
                "schema": schema,
                "risk": risk,
                "server": label,
            }

    # Checked with the servers already up so the error can name the offender.
    # Fatal rather than blocked: retrying cannot shrink a schema.
    from agent.registry import check_budget

    try:
        check_budget()
    except ToolBudgetExceeded:
        _ACTIVE.clear()
        runner.stop()
        _unregister()
        raise

    _RUNNER = runner
    # Registered at CALL time, never at import (CE-05). shutdown() is idempotent, so
    # the explicit call before scoring stays the normal path and this only catches
    # the early returns - a provider failure, a crash, a SIGINT.
    atexit.register(shutdown)
    return sorted(_ACTIVE)


def shutdown() -> None:
    """Stop every server. Safe to call when nothing was started."""
    global _RUNNER
    if _RUNNER is not None:
        _RUNNER.stop()
        _RUNNER = None
    _ACTIVE.clear()
    _unregister()


def _unregister() -> None:
    while _REGISTERED:
        policy.RISK.pop(_REGISTERED.pop(), None)


def tools() -> dict[str, dict]:
    """The MCP tools active for this run. Empty when MCP is off."""
    return dict(_ACTIVE)
