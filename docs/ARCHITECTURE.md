# Architecture

**Status:** derived document. `CONTEXT.md` is the binding specification — where this file and the
spec disagree, the spec wins. This document records *why* the system is shaped the way it is.
`HLD.md` records *what* the components are; `LLD.md` records *how* they are built.

---

## 1. What this system is

A single-user autonomous agent running on the owner's machine. It accepts a goal in natural
language, decomposes it into steps, and works those steps by calling tools — reading and writing
files, running shell commands and Python, operating git, searching the web — looping until the goal
is met, it gets stuck, or it exhausts its budget.

**Target loop for v1:** given a repository with a failing test, make it pass. Bounded, scored by
exit code, needs no human judgement.

**Success is measured, not asserted.** A fixture suite runs headlessly and is scored by exit code.
The pass rate is the project's headline number.

---

## 2. The three properties that are not negotiable

These separate the system from a chat wrapper with function calling. Each is a distinct layer, and
collapsing any of them is a defect rather than a simplification.

### 2.1 Policy gate

Every tool call is classified `auto` / `confirm` / `deny` **before any side effect occurs**.

*Why it exists:* the model will eventually emit `rm -rf`, `git push --force`, or a pip install into
system Python. Classification after the fact is not classification.

*Architectural consequence:* `classify()` is a pure function. No logging, no counters, no database
writes — because the gate node re-executes from its first line when a suspended run resumes, and any
side effect there would fire twice.

### 2.2 Context manager

No tool output reaches the model unfiltered. Results are truncated, the full output is spilled to an
artifact file, and the model receives head lines, an elision marker, tail lines, the artifact path,
**and instructions for inspecting it**.

*Why it exists:* a single `ls -R` over `node_modules` or one 3000-line file read ends the task. This
is what allows runs to pass ~15 turns without the context window collapsing.

*Architectural consequence:* the spill path lives *inside* the returned string, not beside it in a
tuple. A bare path with no instruction is ignored by the model in practice, so the instruction is
part of the contract, not decoration.

### 2.3 Checkpointing

State is written after every node transition, keyed by `thread_id`.

*Why it exists:* a killed process must lose at most one node of work, and a task must resume rather
than restart. A task's identity **is** its `thread_id`; resume is re-invocation with the same id.

*Architectural consequence:* all state must be serialisable. Nothing upstream of a suspension point
may have side effects.

---

## 3. Layer map

```text
                         +----------------------+
                         |  INTERFACE           |
                         |  CLI, streamed       |
                         +----------+-----------+
                                    |
                         +----------v-----------+
                         |  SCHEDULER + QUEUE   |   (deferred — L8)
                         +----------+-----------+
                                    |
               +--------------------v--------------------+
      +------->|  ORCHESTRATOR                           |
      |        |  act . gate . execute . reflect         |
      |        |  checkpointer -> /state/state.db        |
      |        +--------------------+--------------------+
      |                             |
      |                  +----------v-----------+        +---------------+
      |                  |  POLICY GATE         |<------>| HUMAN         |
      |                  |  auto / confirm/deny |        | approve/reject|
      |                  +----------+-----------+        +---------------+
      |                             |
      |          +------------------+------------------+
      |          |                  |                  |
      |    +-----v------+    +------v------+    +------v------+
      |    | SANDBOX    |    | WEB         |    | MEMORY      |
      |    | shell      |    | search      |    | SQLite+FTS5 |
      |    | python     |    | fetch       |    | AGENT.md    |
      |    | files, git |    | browser     |    | artifacts   |
      |    +-----+------+    +------+------+    +------+------+
      |          |            (L5)  |            (L4)  |
      |          +------------------+------------------+
      |                             |
      |                  +----------v-----------+
      +------------------+  CONTEXT MANAGER     |
                         |  truncate . spill    |
                         |  . compact (L1)      |
                         +----------------------+
```

**Why the scheduler is split from the chat process:** the chat process must not be the execution
process, or you cannot detach from a task that has been running forty minutes.

**Why "sandbox" is a layer and not a tool:** "terminal" is not a tool, it is an execution
environment with an enforced workspace root.

---

## 3a. What the measurement changed

This document is the *why* file, so the places where evidence overruled the design's predictions
belong here rather than in a changelog nobody re-reads.

**The verdict distribution replaced §9's predicted layer order.** §9 predicts compaction as the next
layer after v1. The committed baseline produced `done x15` — no `compact`, no `stuck` at the turn
cap — so neither compaction nor the plan node is earned by the data. Conflict 4 made this
distribution a measurement precisely so it could overrule the prediction, and it did.

**What looked like loop defects was the model.** The first baseline scored 4/15 on
`llama-3.1-70b`, and its failures were bucketed as design problems: 9 of 15 runs never called
`read_file`, all 15 ended `done` (11 wrongly), and 5 rewrote the tests they were judged by. One
tuning cycle gated `done` on a successful command; it worked mechanically, made the score worse -
the agent satisfied the gate by rewriting tests until pytest exited 0 - and was reverted.

Re-baselining on `nemotron-3-super-120b-a12b`, a free model on the same key, scored **14/15**. All
three symptoms disappeared. Same harness, same fixtures, same loop.

*Architectural consequence:* the three layers were never the constraint, and the determinism ratio
is vindicated rather than undermined - `reflect`, `gate` and `execute` behaved identically under
both models; only the quality of what `act` returned changed. **The operational lesson is about
diagnosis, not architecture: when a number looks structurally wrong, probe a different model before
tuning the loop.** 102 were available on the existing key throughout.

The reverted cycle also stands as evidence for the revert rule itself. It was kept only long enough
to be measured, and had it been retained on the judgement that it "seemed right", it would now be
dead weight distorting a working system.

**A declared JSON schema is not enforcement.** Numeric tool arguments arrived as strings twice
(`run_shell(timeout)`, then `read_file(offset/limit)`), the second time breaking every read in a live
session so the agent rewrote a 43-line module it had never read. Coercion belongs at the tool
boundary, not in the schema.

**Infrastructure failure is not a score.** A rate-limited run once recorded as a failed case, sitting
in the denominator and quietly deflating the baseline every later change is compared against.
Provider errors are now classified in `provider.py`: retryable (excluded and retried), fatal (aborts
the suite), or a real result. Our own bugs — `BadRequestError`, a malformed tool call — stay scored.

---

## 4. The determinism ratio

| Node type | Count (v1) | Members |
|---|---|---|
| Calls the model | 3 (1 at v1) | `plan` (L2), `act`, compact-summarisation (L1) |
| Deterministic | 4 at v1 | `gate`, `execute` (absorbing observe), `reflect`, `finish` |
| Human | 1 | await approval, reachable only from `gate` |

**Only three nodes ever touch the model. Everything else is ordinary code that can be unit-tested
without an API key.** This ratio is the single most important design property of the system.

The practical consequence: **resist pushing logic into prompts.** A behaviour implemented in
`reflect` is testable, cheap, and deterministic. The same behaviour implemented as a sentence in
`SOUL.md` is none of those, and it costs tokens on every turn.

§3 of the spec shows five deterministic nodes; §13 makes `execute` and `observe` a single node
because no edge ever separates them (CE-04). They remain distinct *stages*, merged into one *node*.

---

## 5. Constraints that shape the code

### 5.1 Code economy (§13 — binding; a violation is a defect)

| Rule | Constraint | Architectural effect |
|---|---|---|
| CE-01 | A module needs two callers OR two implementations | `agent/provider.py` earned its place when a SECOND implementation arrived (Anthropic + any OpenAI-compatible endpoint). Until then it correctly lived inside `act` |
| CE-02 | Frameworks earn their place at current scale | Hand-written tool schemas until tool six; `registry.py` only then |
| CE-03 | Every state field must be read somewhere | No `plan`/`cursor` fields at v1 (no plan node); no `scratch` field |
| CE-04 | Two nodes that never branch apart are one node | `execute` absorbs `observe` |
| CE-05 | No module-level I/O or client construction | `SOUL.md` is read inside the node; the API client is built inside the node |
| CE-06 | Default overwrite state semantics | Plain `TypedDict`, no reducers, no `Annotated`, no `operator` import |
| CE-07 | **`gate` and `execute` must never merge** | Load-bearing, not stylistic — see below |

### 5.2 Why CE-07 is load-bearing

`gate` suspends on `interrupt()` and re-executes **from its first line** when execution resumes
after approval. If `gate` and `execute` were one node, every already-executed tool would fire a
second time on every resume.

This is also the reason FR-305 exists: nothing upstream of a suspension point may have side effects.
The rule generalises — it is not specific to `gate`.

### 5.3 Single source of truth for the workspace root

FR-302 (reject paths outside the workspace) and NFR-201 (zero writes outside the workspace) both
depend on the workspace root having **exactly one definition**. It lives in `config.py`. It is never
re-derived elsewhere, in any module, in any language. `reset.sh` reads the same environment variable
rather than carrying its own default.

### 5.4 Prompts are files

Prompts live in `prompts/SOUL.md`, version-controlled, never as string literals (NFR-603), and are
read **inside** the node rather than at import time (CE-05). Reading a prompt file at import breaks
every test that imports the module.

---

## 6. Enforcement boundaries

Three mechanisms bound the agent. Each guards a different risk; **none duplicates another**, because
two mechanisms guarding one risk is a CE-02 violation (§13 cut the `INSTALL` set for exactly this).

| Boundary | Enforces | Mechanism |
|---|---|---|
| Declared path arguments | FR-302 | `classify()` resolves and rejects escapes, symlinks included |
| Arbitrary shell effects | NFR-201, NFR-204 | Container: read-only root, workspace is the only writable bind mount |
| Network egress | NFR-205 | `--network none`; an offline wheelhouse makes `missing-dep` still solvable |

Tools deliberately do **not** re-check paths. The gate checks what it can see statically; the
container bounds what it cannot.

---

## 7. Technology decisions

| Decision | Choice | Rationale |
|---|---|---|
| Orchestration | LangGraph | The spec names `interrupt()`, `MemorySaver`/`SqliteSaver`, supersteps. Checkpointing + suspend/resume + re-entrancy are far past CE-02 break-even to hand-roll |
| Model | `claude-opus-5` | Highest pass rate on repo-repair, which is the headline number |
| Thinking | Adaptive, effort `medium` initially | `budget_tokens` returns 400 on this model; depth is controlled by `effort`, swept during tuning |
| Checkpoint store | Local SQLite | NFR-703: no hosted service required for state |
| Execution | Docker container | NFR-204; the only WSL distro on the dev machine is `docker-desktop` |
| Provider abstraction | One function, not a module | NFR-702 asks for "a single adapter"; CE-01 forbids a module with one caller |

**Retry policy:** the Anthropic SDK retries 429/5xx with exponential backoff at `max_retries=2`,
which is three attempts total — exactly NFR-303. Do not add a custom retry loop.

---

## 8. Deferred layers, and what earns them

Layers are added one at a time, each justified by a measured eval delta or a requirement it
unblocks. The order below is the spec's *prediction*, not a plan — if compaction moves nothing and
the plan node moves twenty points, the prediction was wrong and the numbers win.

| # | Layer | Earns its place when |
|---|---|---|
| L1 | Compaction | `compact` verdicts dominate the baseline verdict distribution |
| L2 | Plan node | Runs hit `stuck` at max_turns with no repeated calls |
| L3 | Resume hardening | Formalises the SIGKILL test already exercised by the checkpointer |
| L4 | Memory | Episodic recall has something worth recalling |
| L5 | Web | FR-501/502 enter scope; largest token cost, smallest v1 benefit |
| L6 | Extra tools | Tool six triggers `registry.py` and the `@tool` decorator |
| L7 | Argument amendment at approval | FR-307 [C] |
| L8 | Scheduler + worker | Pure infrastructure; no effect on the pass rate |

**A file that exists before its layer is earned will be filled with speculative code and will rot.**
That is why §12 is an allowlist rather than a suggestion.

---

## 9. Known architectural conflicts

Recorded rather than papered over, per §0 and §8.2.

| Conflict | Resolution |
|---|---|
| UR-13 (unattended) vs UR-05 (per-action visibility) | FR-304: autonomous mode denies rather than pauses and defers to a review queue. Less gets done unattended; nothing catastrophic happens |
| NFR-104 (truncate) vs FR-208 (diagnose errors) | FR-402 spill-and-path. Costs one extra tool call, buys a bounded context |
| FR-503 (browser) vs NFR-402 (cost) | Browser automation is the largest token consumer and breaches the cost target alone. This is why it sits at [S] behind search plus text extraction |
| §12 single `.agent/` vs FR-302 + §4.3 | Artifacts move under the workspace so the model can read them; `state.db` stays outside so it survives `reset.sh` |
| NFR-201 (one writable root) vs memory that persists | **Amended Phase K to two declared roots.** `reset.sh` wipes the workspace by design, so anything learned has to live elsewhere. The project tree, previously writable because bind mounts ignore `--read-only`, is now mounted `:ro` |
| FR-702 (display plan) [M] vs §13 cutting `plan`/`cursor` | Unsatisfiable at v1 by construction; closes with the plan node (L2). Recorded as open, not dropped |
| §10 (test every node) vs §12 (three test files) | A fourth test file is added; §10's requirement is the stronger one |

---

## 10. Non-goals (§11)

Multi-user support, auth, tenancy · multi-agent orchestration and sub-agent spawning · a web UI ·
voice (FR-704 is [W]) · a plugin marketplace or dynamic tool loading · vector search before keyword
recall has been measured and found wanting · fine-tuning or local model hosting for the orchestrator. Windows-native support outside WSL2 was
a non-goal until 2026-09-14; it is now the development host.
