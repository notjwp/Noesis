# High-Level Design

**Status:** derived document. `CONTEXT.md` is binding. `ARCHITECTURE.md` records *why* the system is
shaped this way; this document records *what* the components are, what they own, and how they
interact. `LLD.md` records *how* each is built.

**Scope:** v1 — the `[M]` set reachable through §9's build sequence. Deferred components are listed
in §9 below with the trigger that admits them.

---

## 1. Component inventory

| # | Component | File | Owns | Model? |
|---|---|---|---|---|
| C1 | Config | `agent/config.py` | Workspace root, model settings, caps, budgets, thresholds | no |
| C2 | Policy | `agent/policy.py` | `classify()` — the gate's entire decision logic | no |
| C3 | Context manager | `agent/context.py` | `shrink()` — truncate, spill, instruct | no |
| C4 | Tools | `agent/tools.py` | The three v1 tool functions and their schemas | no |
| C5 | Orchestrator | `agent/graph.py` | State shape, five nodes, wiring, `new_state()` | **yes** (`act`) |
| C5a | Provider adapter | `agent/provider.py` | `call_model()`, message translation, error taxonomy | **yes** (only SDK importer) |
| C6 | Interface | `agent/cli.py` | Streamed CLI, approval prompt, thread listing/resume | no |
| C7 | Eval harness | `eval/harness.py` | Fixture runner, scorer, tracing | no |
| C8 | Reset | `scripts/reset.sh` | Idempotent workspace restore | no |

**Dependency direction is strictly one way:**

```text
config  ←  policy
config  ←  context
config  ←  tools
config  ←  provider          (the ONLY module importing a provider SDK)
config, policy, context, tools, provider  ←  graph
graph  ←  cli
graph, provider  ←  harness   (harness needs the error taxonomy to score honestly)
```

`config` imports nothing from the package. No cycles. Every component below `graph` is unit-testable
without an API key (NFR-602).

---

## 2. Component responsibilities

### C1 — Config

The single source of truth. Holds the workspace root, artifact and checkpoint locations, model id,
effort, `max_tokens`, turn and token budgets, the compaction threshold, per-tool output caps, and
head/tail line counts.

FR-302 and NFR-201 both depend on the workspace root having exactly one definition. `reset.sh` reads
the same environment variable rather than carrying a second default.

### C2 — Policy

`classify(name, args, autonomous) -> (verdict, reason)`.

- Resolves every declared path argument and rejects anything outside the workspace (FR-302),
  symlink escapes included.
- Looks the tool up in a risk map; escalates `run_shell` to `destructive` only via a danger pattern,
  so the risk map is the single live path rather than a dead declaration.
- Downgrades `confirm` to `deny` in autonomous mode (FR-304).
- **Pure.** No logging, no counters, no I/O (FR-305) — the gate re-executes on resume.

### C3 — Context manager

`shrink(tool, text) -> str`. Under the per-tool cap, returns the text unchanged. Over it, writes the
full output to a content-addressed artifact and returns head lines, an elision marker, tail lines,
the artifact path, **and instructions for inspecting it**. Returns a plain string — the path belongs
inside the text where the model can act on it.

### C4 — Tools

Three functions plus hand-written JSON schemas: `read_file` (offset/limit), `write_file`,
`run_shell` (timeout; exit code, stdout and stderr captured separately). Tools raise on failure; the
`execute` node converts exceptions to observations (FR-208). Adding a tool touches this file only
(NFR-601).

### C5 — Orchestrator

Owns `AgentState`, the five nodes, the routing functions, the compiled graph, and the model adapter.
The model adapter now lives in `agent/provider.py`, not here: a second implementation (any
OpenAI-compatible endpoint alongside Anthropic) is exactly what CE-01 requires before a module is
justified. `graph.py` also owns `new_state()`, shared with the harness — its two callers.

### C5a — Provider adapter

`call_model(messages, system, tools, on_text) -> Reply`. Two implementations behind one seam. The
rest of the system speaks ONE message shape (Anthropic-style content blocks); the OpenAI-compatible
path translates at its own boundary and nowhere else, so no other file learns a second provider
exists. Also owns the error taxonomy every scored run depends on: `ProviderUnavailable` (retryable,
excluded from the score), `ProviderMisconfigured` (aborts the suite), `MalformedToolCall` (a real
result — the model answered, badly).

### C6 — Interface

`python -m agent "goal"` (interactive), `--list`, `--resume <thread-id>`. Renders the approval prompt
with the exact argument set, unabbreviated (FR-306), resolvable in one keystroke (NFR-801).

Interactive mode is the only caller that sets `autonomous=False` — the switch that makes a `confirm`
verdict suspend instead of becoming a refusal. Live output is a `list` subclass whose `append` also
renders, so `graph.py` needs no callback and no knowledge that a terminal exists.

### C7 — Eval harness

Per case-run: run `setup` (must exit 0), invoke the graph in autonomous mode, restore protected test
files, run `check`, score exit 0 as a pass. One container per case-run, because `missing-dep`
installs a package and a shared container would let later runs pass for free.

Records pass, verdict, turns, tool calls, tokens, cache reads, spills, tool errors, tampering, wall
time, provider and model. Writes `eval/runs/<ts>/summary.jsonl`, `manifest.json`, and a full
per-case trace.

Three properties the score depends on:

- **Blocked runs carry no score.** A run that never reached the model is recorded `status: blocked`,
  retried with backoff, and excluded from the denominator — `pass 4/13, 2 blocked`, never `pass 4/15`.
- **Resumable.** `--continue` re-runs only case-runs with no result and refuses to continue a
  directory whose manifest describes a different invocation.
- **Last row wins.** `summary.jsonl` is append-only, so a retried case-run leaves an earlier row
  behind; aggregation dedupes by `(id, run_index)`. Getting this wrong would not crash — it would
  silently report a different number than reality, which is why it is unit-tested.

`summarise(rows)` is pure, so the arithmetic that produces the headline number is testable without
running anything.

### C8 — Reset

Restores the workspace to a fixture's known-broken state. **Idempotent** — running twice produces an
identical tree — and removes untracked files, because the agent creates files.

---

## 3. Control flow

```text
                            START
                              |
                              v
                          +--------+
              +---------->|  PLAN  |   (L2 — not in v1)
              |           +---+----+
              |               |
              |               v
              |           +--------+
              |    +----->|  ACT   |  model emits tool_use blocks OR final text
              |    |      +---+----+
              |    |          |
              |    |    tool_use? --- no ---------------------+
              |    |          |                               |
              |    |         yes                              |
              |    |          v                               |
              |    |      +--------+   confirm   +-------------------+
              |    |      |  GATE  |------------>|  AWAIT APPROVAL   |
              |    |      +---+----+<------------+  (interactive)    |
              |    |     auto |                  +-------------------+
              |    |          |   deny -> synthetic error observation
              |    |          v
              |    |      +---------+
              |    |      | EXECUTE |  run approved tools, catch exceptions,
              |    |      | +OBSERVE|  shrink, spill, emit tool_result blocks
              |    |      +---+-----+
              |    |          |
              |    |          v
              |    |      +---------+
              |    |      | REFLECT |  deterministic checks only
              |    |      +---+-----+
              |    |          |
              |    |  continue|
              |    +----------+
              |               |
              |    compact ---+---> [ COMPACT ] ---> back to ACT   (L1 — not in v1)
              |               |
              +--- replan ----+                                    (L2 — not in v1)
                              |
                     done | stuck
                              |
                              v
                          +--------+
                          | FINISH |----> END
                          +--------+
```

**v1 entry edge is `START -> act`.** There is no plan node, so routing through one would be dead.

**At v1, `compact` and `replan` route to FINISH** and are recorded as terminal verdicts. This is
deliberate: their frequency in the baseline's verdict distribution is the measurement that earns the
compaction and plan layers, rather than the spec's prediction guessing at it.

---

## 4. Turn lifecycle

1. A goal arrives from the CLI or the harness.
2. `act` calls the model with the full message history, the tool schemas, and the system prompt.
3. The router inspects the last assistant message: `tool_use` blocks → `gate`; text only → `reflect`
   (step complete, **not** termination).
4. `gate` classifies each call **independently**. Mixed verdicts in one turn are legal — some
   approved, others denied.
5. `execute` runs approved calls. Tool exceptions are caught and converted to observations; they
   never propagate out of the node.
6. `execute` shrinks each result, spills overflow, and appends all `tool_result` blocks as a **single
   user message**.
7. `reflect` emits exactly one verdict, from checks in a fixed order (§5).
8. Terminal verdicts route to `finish`, which records the outcome and ends.

---

## 5. Reflect: the verdict contract

Checks run in this order. The first match wins; order is part of the contract, not an implementation
detail.

| # | Condition | Verdict | v1 destination |
|---|---|---|---|
| a | `spent_tokens > 60% of budget` | `compact` | FINISH (records budget pressure) |
| b | `turns >= max_turns` | `stuck` | FINISH |
| c | Last 3 tool signatures identical | `stuck` | FINISH |
| d | 3 consecutive failed turns | `replan` | FINISH |
| e | Last message is assistant text | `done` if any tool call was ever made, else `continue` | FINISH / ACT |
| f | Otherwise | `continue` | ACT |

**Check (e) is the termination guard.** With no plan node, a cursor-based check evaluates `1 >= 0`
and returns `done` on the first text-only reply — including *"Let me look at the test file first."*
Gating on whether a tool call was ever made is what prevents that. The cursor check is restored only
when the plan node lands.

---

## 6. State model

Nine fields, plain `TypedDict`, default overwrite semantics — no reducers, no `Annotated`, no
`operator` import.

| Field | Type | Written by | Read by |
|---|---|---|---|
| `messages` | `list[dict]` | `act`, `execute` | `act`, `gate`, `reflect` |
| `turns` | `int` | `execute` | `reflect` |
| `max_turns` | `int` | caller | `reflect` |
| `spent_tokens` | `int` | `act` | `reflect` |
| `budget_tokens` | `int` | caller | `reflect` |
| `failures` | `int` | `execute` | `reflect` |
| `verdict` | `str \| None` | `reflect` | router, `finish` |
| `approved` | `list[dict]` | `gate` | `execute` |
| `denied` | `list[dict]` | `gate` | `execute` |

Every field is read somewhere (CE-03).

**`failures` counts *consecutive* failed turns and is reset to 0 when every result in a turn
succeeded.** As an accumulating list it would latch at `>= 3` for the rest of the run.

**Mode (`autonomous`) and `thread_id` travel in the graph config, not in state** — they are run-level
configuration, not evolving state.

---

## 7. Persistence and recovery

- Checkpoint after every node transition, keyed by `thread_id`, to local SQLite.
- A task's identity **is** its `thread_id`. Resume is re-invocation with the same id, not a restart.
- Chat threads and background tasks share the checkpoint store; a chat session attaching to a running
  task is just a reader on the same thread.
- A kill at any instant loses at most one node of work (NFR-301), and resume produces no duplicated
  side effects (NFR-302) — which is what CE-07's gate/execute separation buys.

---

## 8. Deployment model

```text
Host (Windows 11 + Docker Desktop)
  └── container: python:3.12-slim + git + pytest + /wheels
        ├── /app          project, read-only root filesystem
        ├── /workspace    the ONLY writable bind mount   ← NFR-201, NFR-204
        ├── /tmp          tmpfs
        └── network: none                                ← NFR-205
```

The offline wheelhouse at `/wheels` is what keeps the `missing-dep` fixture solvable with networking
disabled, so NFR-205 holds across the whole suite rather than being waived for one case.

`.gitattributes` pins `*.sh` to LF. On a Windows checkout, CRLF line endings break every bash script
in the container with a `bad interpreter` error.

---

## 9. Deferred components

| Component | File | Admitted when |
|---|---|---|
| Tool registry / `@tool` decorator | `agent/registry.py` | Tool six — break-even against hand-written schemas is five |
| Memory | `agent/memory.py` | Episodic recall has something worth recalling |
| Web | `agent/web.py` | FR-501/502 enter scope |
| Worker / scheduler | `agent/worker.py` | FR-6xx enters scope; explicitly out of scope for v1 |
| Compact node | `graph.py` + `context.py` | `compact` verdicts dominate the baseline |
| Plan node | `graph.py` | `stuck` at max_turns with no repeats dominates |

---

## 10. Cross-cutting concerns

### Observability

One structured log line per tool call (FR-805) carrying name, argument hash, verdict, duration, byte
counts and spill path (NFR-501). Denied calls are logged too — a denial is a tool call. A completed
task must be reconstructible from logs alone (NFR-502).

Logging lives in `execute`, never in `gate` — logging inside `gate` would violate CE-07.

### Security

Secrets never enter model context; env-var indirection plus output redaction (NFR-203). Scored runs
are confined to a container whose only host mount is the workspace (NFR-204); interactive use runs
natively and the policy gate is the boundary — a hardline tier no approval overrides, `destructive`
that asks (NFR-204 and FR-301 as amended 2026-09-14). Egress is disabled for the eval suite
(NFR-205).

### Cost

Token budget is a hard stop, not advisory (NFR-401). Median eval case within 60,000 tokens
(NFR-402). No single tool result exceeds 2,000 tokens after shrinking (NFR-104). Compaction, when it
lands, must reduce context by at least 50% (NFR-403).

### Testability

Every deterministic node is unit-testable without an API key (NFR-602), and §10 requires those tests
to exist. This is a design constraint on the nodes, not a testing afterthought: it is why `reflect`
takes state and returns a verdict rather than reaching for a client.

---

## 11. Requirement traceability

| Requirement family | Component | Verified by |
|---|---|---|
| FR-101, 104–107 | C5 `reflect` / `plan` (L2) | `tests/test_reflect.py` + eval |
| FR-102, 103, 108 | C5 wiring + checkpointer | eval + resume test |
| FR-201..206, 208 | C4 tools | node tests + eval |
| FR-207 | Hand-written schemas; `registry.py` at tool six | node tests |
| FR-301..307 | C2 + C5 `gate` | `tests/test_policy.py`, `tests/test_nodes.py` |
| FR-401..404 | C3 + C5 `execute` | `tests/test_context.py` |
| FR-405 | Checkpointer | resume test |
| FR-501..505 | C-web (L5) | deferred |
| FR-601..607 | C-worker (L8) | deferred |
| FR-701..703 | C6 CLI | manual + eval |
| FR-801..805 | C7 harness | harness self-check |

Verification for the FR-1xx and FR-2xx families **is** the eval harness. **A requirement is satisfied
when the cases exercising it pass, not when the code exists.**

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Check command fails for the wrong reason (collection error, not assertion) | Masks a working agent; corrupts the pass rate | Verify each fixture fails on the intended assertion before the baseline |
| `reset.sh` leaves untracked files behind | Cases contaminate each other | Idempotence test asserting identical trees across two runs |
| Tuning overfits to five dev cases | Pass rate stops predicting real behaviour | Ten held-out cases, never inspected during tuning |
| Framework API drift (`SqliteSaver`, `interrupt` import paths) | Build breaks on upgrade | Pin versions; verify construction against the installed release |
| Multi-approval turns resume incorrectly | Wrong tool approved | Confirm positional interrupt-resume semantics before relying on them |
| Thinking blocks not echoed back correctly | 400s or degraded turns | Round-trip serialised blocks unchanged; verify on first live call |
