"""The model adapter (NFR-702).

This module exists because there are now TWO implementations behind one seam —
Anthropic's native API and any OpenAI-compatible endpoint (NVIDIA NIM by default).
CE-01 requires two callers or two implementations before a module is justified;
until the second provider arrived, this code correctly lived inside the act node.

The rest of the system speaks ONE message shape: Anthropic-style content blocks.
That shape is fixed by the state contract, and every node, test and trace already
uses it. The OpenAI-compatible provider translates at its own boundary and nowhere
else, so no other file learns that a second provider exists.
"""
import json
import random
import time
from dataclasses import dataclass

from agent import config as settings


@dataclass(frozen=True)
class Reply:
    """One model turn, normalised."""
    blocks: list[dict]          # Anthropic-shaped: {"type": "text"|"tool_use", ...}
    billed_tokens: int
    cache_read_tokens: int
    stop_reason: str | None
    # NFR-101 is stated in seconds and was recorded NOT MEASURABLE because the
    # OpenAI path returned one block at the end. None means nothing streamed.
    first_token_s: float | None = None


class MalformedToolCall(RuntimeError):
    """The model emitted a tool call whose arguments are not valid JSON.

    Raised loudly rather than patched over: native tool calling is a hard
    requirement, and the spec forbids parsing calls out of free text. A model that
    cannot do this reliably is the wrong model, and silently coercing bad arguments
    would hide that behind a mysteriously low pass rate.
    """


class ProviderUnavailable(RuntimeError):
    """The provider could not answer, for reasons unrelated to the agent: a rate
    limit, a timeout, a dropped connection, or a 5xx.

    Deliberately distinct from MalformedToolCall, where the model DID answer and
    answered badly - that is a result and must be scored. A run lost to this one
    measured nothing, so scoring it as a failed case would understate the agent
    and quietly corrupt every comparison made against that baseline.
    """


class ProviderMisconfigured(RuntimeError):
    """No key, a rejected key, or a forbidden model. Retrying cannot help, and
    fifteen of these in a row look exactly like an agent that can do nothing."""


# Both SDKs expose identical exception names, verified against the installed
# releases, so one table serves both without importing both to compare classes.
RETRYABLE = ("RateLimitError", "APITimeoutError", "APIConnectionError",
             "InternalServerError",
             # A 404 on the model endpoint is RETRYABLE, not fatal: measured mid-cycle, an
             # endpoint blinked and returned NotFoundError for a model that existed before
             # and after. Treating it as fatal would score the outage as agent failure.
             "NotFoundError",
             # The BASE class, and it is here because it carries no status_code: every
             # attributable status already has its own class above or in FATAL, so a
             # bare APIError is the SDK saying it could not attribute the failure.
             # Streaming makes it common - create() returns before the body, so a
             # mid-stream "Service temporarily overloaded" arrives unmapped and three
             # real-humanize runs were scored 0/3 for an outage.
             "APIError",
             # TRANSPORT, below the SDK. The SDK wraps httpx errors on a normal
             # request, but a stream fails while it is being ITERATED - after
             # create() returned - so httpx raises through unwrapped. Measured:
             # a severed stream arrived as RemoteProtocolError, missed this
             # table, and was scored as a failed case with turns 0, tokens 0.
             "RemoteProtocolError", "LocalProtocolError", "ReadError",
             "ConnectError", "ConnectTimeout", "ReadTimeout", "PoolTimeout")

# The same failures when something has WRAPPED them and the type name no longer
# says so. Both a type list and this substring list are needed for exactly
# that - a transport abort has no HTTP status, because the SDK never saw a
# response.
TRANSPORT_MARKERS = ("incomplete chunked read", "peer closed connection",
                     "response ended prematurely", "unexpected eof",
                     "remoteprotocolerror", "localprotocolerror")

FATAL = ("AuthenticationError", "PermissionDeniedError")


def _reraise_classified(exc: Exception) -> None:
    """Re-raise as our taxonomy if this belongs to it; return otherwise.

    Returning (rather than raising something generic) is the important half: a
    BadRequestError is OUR bug and a MalformedToolCall is a real result, and
    excusing either as "the network" would hide a defect behind an excluded run.
    """
    name = type(exc).__name__
    if name in FATAL:
        raise ProviderMisconfigured(f"{name}: {exc}") from exc
    if name in RETRYABLE:
        raise ProviderUnavailable(f"{name}: {exc}") from exc
    # Deliberately NOT "anything without a status_code": our own TypeError has
    # none either, and excusing a real bug as an outage is worse than one
    # mis-scored row. Names and known markers only.
    if any(marker in str(exc).lower() for marker in TRANSPORT_MARKERS):
        raise ProviderUnavailable(f"{name}: {exc}") from exc


class _Spoken:
    """Wraps on_text so a retry can tell whether the caller already saw output.

    A stream that fails after emitting tokens cannot be retried - the second
    attempt would repeat them into the transcript. The measured failures arrive
    in 0.2-0.3s, before any token, so this refuses the unsafe case rather than
    buffering the stream and forfeiting NFR-101.
    """

    def __init__(self, sink):
        self._sink = sink
        self.any = False

    def say(self, text: str) -> None:
        self.any = True
        self._sink(text)


def _pause(attempt: int) -> float:
    """Exponential backoff with decorrelating jitter.

    The jitter is not decoration: a worker draining a queue and a scored run
    retrying in lockstep re-collide on the same overloaded window.
    """
    delay = min(settings.RETRY_BASE * (2 ** (attempt - 1)), settings.RETRY_CAP)
    return delay + random.uniform(0, delay / 2)


def call_model(messages: list[dict], system: str, tools: list[dict],
               on_text=None) -> Reply:
    """Run one model turn. The only place a provider SDK is imported.

    RETRYABLE listed the exceptions worth another attempt and nothing ever made
    one - the classification was added, the retry was not. See config.
    CALL_ATTEMPTS. Only ProviderUnavailable is retried: MalformedToolCall is a
    real result and ProviderMisconfigured cannot be fixed by asking again.
    """
    provider = settings.PROVIDER
    if provider == "anthropic":
        call = _call_anthropic
    elif provider in ("nvidia", "openai"):
        call = _call_openai_compatible
    else:
        raise ValueError(
            f"unknown AGENT_PROVIDER {provider!r}; expected anthropic, nvidia or openai")

    for attempt in range(1, max(1, settings.CALL_ATTEMPTS) + 1):
        spoken = _Spoken(on_text) if on_text else None
        try:
            return call(messages, system, tools, spoken.say if spoken else None)
        except ProviderUnavailable:
            if attempt >= max(1, settings.CALL_ATTEMPTS) or (spoken and spoken.any):
                raise
            time.sleep(_pause(attempt))
    raise AssertionError("unreachable")


# ------------------------------------------------------------------ anthropic

def _call_anthropic(messages, system, tools, on_text) -> Reply:
    import anthropic

    try:
        client = anthropic.Anthropic(
            timeout=settings.REQUEST_TIMEOUT,
            max_retries=settings.MAX_ATTEMPTS - 1,
        )
        with client.messages.stream(
            model=settings.MODEL,
            max_tokens=settings.MAX_TOKENS,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            output_config={"effort": settings.EFFORT},
            tools=tools,
            messages=messages,
        ) as stream:
            if on_text:
                for delta in stream.text_stream:
                    on_text(delta)
            response = stream.get_final_message()
    except Exception as exc:
        _reraise_classified(exc)   # rate limit / auth / 5xx become our taxonomy
        raise                      # anything else is ours, and stays loud

    usage = response.usage
    return Reply(
        # model_dump() keeps thinking blocks (signature included) intact for replay.
        blocks=[b.model_dump() for b in response.content],
        billed_tokens=usage.input_tokens + usage.output_tokens,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        stop_reason=getattr(response, "stop_reason", None),
    )


# ---------------------------------------------------- openai-compatible (NIM)


class _Fragment:
    """One tool call rebuilt from its deltas, shaped like the SDK's own object.

    Assembled into the non-streaming shape rather than parsed separately, so
    from_openai_message() stays the ONE place a reply becomes content blocks.
    Two parsers would be two things to keep agreeing.
    """

    def __init__(self):
        self.id = ""
        self.function = _Fragment._Function()

    class _Function:
        def __init__(self):
            self.name = ""
            self.arguments = ""


class _Assembled:
    def __init__(self, content, tool_calls):
        self.content = content
        self.tool_calls = tool_calls


def _assemble(stream, on_text, started):
    """Collapse a delta stream into one message, a finish reason and usage.

    Fragments are grouped by `index`, NOT by arrival order: a model emitting two
    calls interleaves their deltas, and concatenating in arrival order splices
    one call's arguments into another. `id` and `name` arrive only on a call's
    first fragment, so both are written once and never overwritten with "".
    """
    text: list[str] = []
    calls: dict[int, _Fragment] = {}
    finish = None
    usage = None
    first = None

    for chunk in stream:
        # Usage rides a FINAL chunk that carries no choices. Skipping it leaves
        # billed_tokens at 0, which silently corrupts every cost number.
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not getattr(chunk, "choices", None):
            continue
        choice = chunk.choices[0]
        if getattr(choice, "finish_reason", None):
            finish = choice.finish_reason
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        if getattr(delta, "content", None):
            if first is None:
                first = time.monotonic() - started
            text.append(delta.content)
            if on_text:
                on_text(delta.content)
        for part in (getattr(delta, "tool_calls", None) or []):
            if first is None:
                first = time.monotonic() - started
            slot = calls.setdefault(getattr(part, "index", 0) or 0, _Fragment())
            if getattr(part, "id", None):
                slot.id = part.id
            fn = getattr(part, "function", None)
            if fn is None:
                continue
            if getattr(fn, "name", None):
                slot.function.name = fn.name
            if getattr(fn, "arguments", None):
                slot.function.arguments += fn.arguments

    ordered = [calls[i] for i in sorted(calls)]
    return _Assembled("".join(text), ordered), finish, usage, first


def _call_openai_compatible(messages, system, tools, on_text) -> Reply:
    import openai

    try:
        api_key = settings.openai_api_key()
    except RuntimeError as exc:
        # A missing key is configuration, not a failed run. Surfaced as such so the
        # suite aborts once instead of recording fifteen identical "failures".
        raise ProviderMisconfigured(str(exc)) from exc

    try:
        client = openai.OpenAI(
            base_url=settings.OPENAI_BASE_URL,
            api_key=api_key,
            # Without these the SDK waits 10 minutes per attempt and retries twice, so a
            # stalled endpoint costs half an hour of silence before anything is visible.
            timeout=settings.REQUEST_TIMEOUT,
            max_retries=settings.MAX_ATTEMPTS - 1,
        )
        # Deliberately no thinking, effort or cache_control - Anthropic-only, and
        # sending them here produces confusing 400s rather than a clear failure.
        request = dict(model=settings.OPENAI_MODEL,
                       max_tokens=settings.MAX_TOKENS,
                       messages=to_openai_messages(system, messages))
        if tools:
            request["tools"] = to_openai_tools(tools)
        started = time.monotonic()
        if settings.STREAM:
            # include_usage is not optional here: without it the stream carries no
            # token counts at all and every run reports 0.
            stream = client.chat.completions.create(
                **request, stream=True, stream_options={"include_usage": True})
            message, finish, usage, first_token = _assemble(stream, on_text, started)
        else:
            response = client.chat.completions.create(**request)
            choice = response.choices[0]
            message, finish, usage, first_token = (
                choice.message, choice.finish_reason, response.usage, None)
    except Exception as exc:
        _reraise_classified(exc)
        raise

    blocks = from_openai_message(message)

    if on_text and not settings.STREAM:
        for block in blocks:
            if block["type"] == "text":
                on_text(block["text"])

    return Reply(
        blocks=blocks,
        billed_tokens=(usage.prompt_tokens + usage.completion_tokens) if usage else 0,
        cache_read_tokens=0,        # no prompt caching on this path
        # A streamed finish_reason is the SAME field, and it must survive: a
        # `length` scored as `done` is the defect 04bcde9 fixed.
        stop_reason=finish,
        first_token_s=first_token,
    )


# ------------------------------------------------------------- translation

def to_openai_tools(tools: list[dict]) -> list[dict]:
    """Anthropic {name, description, input_schema} -> OpenAI function tool."""
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t["input_schema"],
        },
    } for t in tools]


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Anthropic content blocks -> OpenAI chat messages.

    The asymmetry that matters: our single user message carrying N tool_result
    blocks must fan out into N separate `role: "tool"` messages, in order, each
    carrying its own tool_call_id. Collapsing them is rejected by the provider.
    """
    out: list[dict] = [{"role": "system", "content": system}]

    for message in messages:
        role, content = message["role"], message["content"]

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if role == "assistant":
            text = "".join(b.get("text", "") for b in content
                           if b.get("type") == "text")
            calls = [{
                "id": b["id"],
                "type": "function",
                "function": {"name": b["name"],
                             "arguments": json.dumps(b.get("input", {}))},
            } for b in content if b.get("type") == "tool_use"]
            # Unknown block types (thinking, redacted_thinking) are dropped: they
            # are Anthropic-only and meaningless to this provider.
            assistant = {"role": "assistant", "content": text or None}
            if calls:
                assistant["tool_calls"] = calls
            out.append(assistant)
            continue

        # user turn: tool results fan out, plain text stays one message
        for block in content:
            if block.get("type") == "tool_result":
                out.append({"role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": str(block.get("content", ""))})
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})

    return out


def from_openai_message(message) -> list[dict]:
    """OpenAI reply -> Anthropic content blocks."""
    blocks: list[dict] = []

    text = getattr(message, "content", None)
    if text:
        blocks.append({"type": "text", "text": text})

    for call in (getattr(message, "tool_calls", None) or []):
        raw = call.function.arguments or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MalformedToolCall(
                f"{settings.OPENAI_MODEL} returned unparseable arguments for "
                f"{call.function.name}: {raw!r}"
            ) from exc
        if not isinstance(arguments, dict):
            raise MalformedToolCall(
                f"{settings.OPENAI_MODEL} returned non-object arguments for "
                f"{call.function.name}: {raw!r}")
        blocks.append({"type": "tool_use", "id": call.id,
                       "name": call.function.name, "input": arguments})

    return blocks
