# Roadmap — a personal assistant that learns

The forward-looking half of the plan, kept in the repository so it is version-controlled alongside
the code it describes. `CONTEXT.md` remains the binding specification; `eval/CHANGELOG.md` is the
record of what was actually measured.

v1 is complete (Definition of Done 9/9). What follows is v2.

---

## v2 — a personal assistant that learns

**The goal, in your words:** an assistant that remembers past conversations across sessions, builds a
model of who you are, creates its own reusable skills after complex work, and can actually *do*
things through a real set of tools.

That reorders the old plan. It was a feature checklist; this is a spine with one theme. Every phase
below exists to serve **learning**, and each is gated on a number that says whether learning
happened.

```
K  sandbox      memory has nowhere to live until a second root is writable
L  tools        skills are procedures OVER tools; four tools is not enough to compose
M  memory       remembers across sessions, and can prove it
N  skills       writes its own procedures, retrieves them, and they help
```

`reset.sh` wipes the workspace every run by design, so anything that persists must live outside it.
That is why K comes first and is not optional.

### The hazard this whole spine is built against

**"It learns" is the easiest claim in AI to believe and the hardest to falsify.** You will feel it
working before it works. Every phase here therefore builds its measurement *before* its feature, and
ships with a switch that turns the feature off — because a capability that cannot be turned off
cannot be attributed either.

---

## Phase K — Two writable roots, kernel-enforced — **BUILT**

**Context.** Memory and skills must survive `reset.sh`, which wipes the workspace every run by
design. They therefore need somewhere else to live, and that "somewhere else" must be as tightly
bounded as the workspace is — or the boundary that makes this project trustworthy quietly widens.

**A defect found while planning this phase, and it is the reason K was not just a mount:**

```
/workspace           writable   intended
/app                 WRITABLE   the entire project tree
/app/eval/fixtures   WRITABLE   the fixtures the agent is scored against
/usr/local           read-only  correct
```

`--read-only` makes the ROOT FILESYSTEM immutable; **bind mounts are unaffected**. The project was
bind-mounted at `/app`, so the agent could write to the harness, `tasks.jsonl`, and the fixtures that
decide its own score.

Nothing suggests it ever did — all 30 Phase I traces show zero access to `/app`, and the tamper check
restores protected test files after every run. But that guard is **post-hoc**: it repairs damage
rather than preventing it, and a successful write to `/app` would not even have registered as a
violation, because `count_write_violations()` looked for "Read-only file system" errors that could
never occur there.

**Exit criterion:** the agent home survives `reset.sh`; a write to the project tree is refused by the
kernel; the v1 suite score is unchanged. Evidence for all three is in `eval/CHANGELOG.md`.

### Task K1 — Make the project tree read-only ✅

- [x] **Step 1: Mount `-v {REPO}:/app:ro`** in `spawn()`.
- [x] **Step 2: Two things currently write under `/app`, and both need a home first** — do not skip
      this and discover it mid-run:
      - `eval/runs/<ts>/` — the inner runner writes `summary.jsonl` and each trace there
      - `/app/.agent/state.db` — `AGENT_HOME` in the `Containerfile`
- [x] **Step 3: Give each its own narrow writable mount**, so every writable path is declared rather
      than inherited.
- [x] **Step 4: Move `AGENT_HOME` to `/state`** in the `Containerfile` and `config.py`.
- [x] **Step 5: Prove it, in both directions.** All eight targets behave as specified; the probe and
      its output are recorded in `eval/CHANGELOG.md` so a reader can re-run it rather than trust it.

### Task K2 — Teach the violation counter about the new shape ✅

- [x] **Step 1:** With `/app` read-only, an attempted write there now **produces** the error the
      counter detects, so it starts being counted — which is the point.
- [x] **Step 2: Report which path was targeted**, not just a count. `write_violations()` returns
      paths; the row carries `write_violation_paths` and the warning names them above the table.
- [x] **Step 3: Keep the tamper check.** Defence in depth: the kernel now prevents, the tamper check
      still verifies.

### Task K3 — The agent home, and what lives in it ✅

- [x] **Step 1: `~/.personal-agent`** on the host, `/state` in the container.
- [x] **Step 2: Layout fixed now**, so M and N do not each invent one:
      `state.db` (checkpoints), `memory.db` (Phase M), `skills/` (Phase N).
- [x] **Step 3: `reset.sh` must NOT touch it.** Two tests: the invariant (`STATE_DB` is not under
      `WORKSPACE`) and the end-to-end one against the real script.
- [x] **Step 4: Per-run isolation for scored runs.** `agent_home()` wipes and recreates per case-run;
      the interactive CLI keeps a persistent one.

### Task K4 — Per-session egress ✅

- [x] **Step 1:** A case declares `"egress": [...]` on its `tasks.jsonl` row; `allowlist_for()`
      derives the list per case-run. No case declares any today, so every current number is measured
      against the model host alone — unchanged.
- [x] **Step 2: Recorded in the manifest**, per case, and now per row too.
- [x] **Step 3: Extended the Phase H derivation** rather than rebuilding it.

**Two findings this task paid for, both in the changelog:**

- **SIGHUP does not reload tinyproxy's filter.** Measured, not assumed — `example.com` was refused
  identically before and after the signal, with the widened file already visible inside the
  container. The proxy is recreated instead. A widening that silently fails is harmless (it fails
  closed); the manifest recording the allowlist that was merely *asked for* would not have been.
- **Every trace row ever written claimed `"egress": "restricted"`** from a default, and nothing set
  the variable it read. `spawn()` now states the truth and the fallback says `UNKNOWN`. A default
  that asserts the safe answer is how a row comes to claim a condition nobody checked.

### Task K5 — Prove nothing moved

- [x] **Step 1:** `pytest -q` under the new mounts — 182 pass, no API key, no network, read-only
      root. Nine tests are new.
- [x] **Step 2: Re-run the dev suite, 3 runs per case.** **14/15 — case for case, including which
      single case fails.** 0 blocked, 0 tampered, 0 write violations, one model, egress recorded per
      row for the first time. A sandbox change that broke something would have moved a case.
- [x] **Step 3: Record the probe output in `eval/CHANGELOG.md`** as evidence, with the commands.


---

## Phase L — MCP: real tools, without dissolving the boundary — **BUILT**

**Outcome: MCP bought efficiency, not capability.** The exit criterion this phase was given —
*"move the real-repository number"* — was replaced before any code was written, and the capability
claim it implied did not survive its own baseline. Full evidence in `eval/CHANGELOG.md`.

### What the planning measurements found

- **Prompt caching is not happening.** `cache_read_tokens: 0` on all 15 rows of the Phase K run, at
  a mean 9.1 model calls per run. Every tool schema is paid in full on every request.
- **Schemas were already 23% of a run** — 1,997 chars for four tools, ~6,050 tokens/run against a
  median of 26,600.
- **The ecosystem is unreachable.** No `node`, `npx` or `uvx` in the image, and `pip.conf` is
  `no-index`, so nothing installs at run time.

At a real server's ~254 tokens/tool, 24 exposed tools would breach NFR-402's 60,000 ceiling on
schema **alone**. "Inherit an ecosystem" is a strategy for a cached provider with a paid tier; here
breadth is the most expensive thing available. So: **one server, and a hard budget.**

- [x] **`config.MAX_SCHEMA_CHARS = 6_000`**, enforced at activation and fatal — not a warning. Every
      row now records `schema_chars` and `mcp`, so exposure can never again be an invisible cost.
- [x] **`agent/mcp.py`** — stdio only. The server is a subprocess, so it inherits the container's
      mounts and namespace and **Phase K's boundary already contains it**, with no new mechanism.
- [x] **Baked at build time**, pinned `mcp==1.29.0` (not 2.0.0 — `mcp-server-fetch` declares
      `mcp<2`; measured, not assumed). `no-index` means a server cannot be introduced mid-run.
- [x] **`policy.register()`** — an unclassified tool records `destructive`, never `read`, so it
      prompts interactively and is refused unattended. Unknown tools stay denied.
- [x] **`PATH_ARGS` widened**, and the bypass tested. It guarded only the argument names this
      project's own tools use; a server calling its argument `filename` would have walked past the
      workspace check. `url` stays out deliberately.
- [x] **Argument coercion against the declared schema.** `_int()` protects the built-ins; an MCP
      server validates and rejects instead, so the symptom would have been a tool that always fails.
- [x] **Kill switch `AGENT_MCP=off`**, which is what made the comparison below controlled.

### `registry.py` was NOT created

§12's trigger is *"add at tool six"*. Four built-ins plus `fetch` is **five**. The merge is nine
lines in `tools.toolset()` with two callers. A deferred layer with a numeric trigger does not get to
fire because the phase that would use it arrived.

### The result

**The zero baseline was not zero — it was 18/18.** `run_shell` plus Python's `urllib` already
reaches the web, because `spawn()` sets `HTTP_PROXY` and urllib honours it. Had that baseline been
skipped as a derivable zero, this phase would have shipped a false capability claim that was
unfalsifiable afterwards.

| | MCP off | MCP on | delta |
|---|---|---|---|
| pass | 18/18 | 18/18 | — |
| turns (mean) | 6.2 | **3.1** | **−50%** |
| tokens (median) | 11,528 | **7,293** | **−37%** |
| schema per request | 1,997 | 3,159 | **+58%** |

The schema got 58% dearer per request and the run still got 37% cheaper: four discovery calls
(`curl`, `which wget`, `python3 --version`, then urllib) stop happening.

**First scored use of Phase K's per-case egress** — every row records
`fixture-web,integrate.api.nvidia.com`.

- [x] **202 offline tests**, up from 182, and they pass **without the `mcp` package installed**.
- [x] **Dev-suite regression guard: no regression.** 15/15 against a 14/15 baseline, total tokens
      383,489 -> 396,025 (**+3.3%**), 0 tampered, 0 write violations. The extra pass is
      `add-endpoint`, which was 2/3 in both earlier runs and is the suite's known-flaky case — one
      run at n=3 is inside its own variance and **is not a Phase L improvement**. What the guard
      establishes is that a tool you never call costs about 3% of a long run, not 58%.


---

## Phase M — Memory: remembers across sessions, and can prove it — **BUILT**

> **Result: 0/18 without memory, 15/18 with it, and 40% cheaper.** Episodic recall went 0/9 ->
> 9/9; the profile 0/9 -> 6/9 as measured, and 9/9 after a fixture check that was measuring
> whitespace was amended and re-verified. Dev suite unmoved at 14/15. Full evidence, including
> the provenance of the amended check, in `eval/CHANGELOG.md`.
>
> **Two deviations from the plan below, both deliberate.** M0's prompt widening was NOT done -
> two variables in one cycle - and the agent adopted `remember` from its schema alone anyway.
> M5 created `agent/registry.py` but NOT the `@tool` decorator §12 names: five of six schemas
> are hand-written, break-even is above eight, and the descriptions are load-bearing.

### Context — why this phase, and what changed in the planning of it

The stated goal for v2 is *"an assistant that remembers our past conversations across sessions and
builds a model of who I am."* Nothing in the agent does that today: `SqliteSaver` restores a thread,
so the agent remembers **within** a conversation and nothing at all **between** them. Phase K created
`/state`, which survives `reset.sh` — memory finally has somewhere to live. Phase L supplied the
pattern that makes a claim like this provable: a kill switch, so the same binary measured twice one
flag apart is a controlled comparison rather than two separate numbers.

**Scope chosen: episodic recall AND a durable profile file.** The profile was flagged as the harder
half — a second store to keep correct, with no obvious pass/fail — and taken anyway. So this plan
does not leave it on vibes: **the profile gets its own deterministic score**, described in M2. If it
cannot be scored, it does not ship.

**Exit criterion:** on a recall benchmark built and baselined *before* any memory code, the split
moves, the dev suite does not, and the token cost of carrying memory is reported beside the gain.

### The hazard, and it is not hypothetical

Phase L was supposed to take a web split from 0 to 18. The baseline scored **18/18 before the feature
existed** — the agent reached the web with `run_shell` and `urllib`. Had that run been skipped as an
obvious zero, the phase would have shipped a false capability claim that was unfalsifiable
afterwards.

**The same trap is present here and is wider.** Checked in the code during planning:

- `policy.classify()` workspace-checks only arguments named in `PATH_ARGS`. `run_shell`'s only
  argument is `command`, so **no path check applies to it at all**.
- `RISK["run_shell"] = "write"` → verdict `auto`, and the `DANGER` regex does not match a redirect.
- Phase K made `/state` a **writable** mount.

So `run_shell(command='echo "deploy key kx-9920" > /state/notes.md')` is auto-approved today, and a
later session can `cat` it back. **The agent may already be able to pass a recall benchmark with no
memory layer whatsoever.** That is a finding to measure in M3, not a bug to pre-emptively patch —
closing the hole before measuring would hide whether it mattered.

### What was verified during planning

| | |
|---|---|
| FTS5 | available (`sqlite3` 3.49.1 on the host; the container is re-checked in M1 Step 1) |
| `finish` node | already terminal and deterministic, with a docstring saying *"writing durable memory arrives with the memory layer"* — the hook exists |
| Model-calling nodes | `act` only. **Phase M adds none** — see M4 |
| `prompts/SOUL.md` | 33 lines, opens *"You fix broken code"*. A memory phase about conversations needs this widened, and a prompt change is a change |
| Tool count | 4 built-in + `fetch` = 5. A sixth tool **fires §12's "add at tool six" trigger** |

---

### Task M0 — Widen the prompt, and account for it (free)

- [ ] **Step 1:** `SOUL.md` says the job is fixing broken code and that `pytest -q` is the definition
      of done. Every case so far has been a repair task, so nothing has exposed it — but a session
      whose whole purpose is *"remember that I deploy with `make ship`"* has no failing suite to go
      green, and the agent is being told that is what finishing means.
- [ ] **Step 2: Widen it to "you help with a task; sometimes that is code"**, and add the memory
      affordance in the same edit: what injected context is, and that `remember` exists.
- [ ] **Step 3: This is a change and is measured like one.** It ships INSIDE the memory feature and is
      covered by the same kill switch, so `AGENT_MEMORY=off` must restore the current prompt exactly.
      Two variables in one cycle is otherwise exactly what the Iron Law forbids.

### Task M1 — `agent/memory.py`: the store (free, offline)

- [ ] **Step 1: Verify FTS5 inside the container before designing around it.** One in-memory
      `CREATE VIRTUAL TABLE ... USING fts5`. Cheap, and the alternative is discovering it mid-build.
- [ ] **Step 2: `/state/memory.db`**, the layout Phase K already fixed. SQLite, one `episodes` table
      plus an FTS5 index over the searchable text.
- [ ] **Step 3: One row per finished session:** `thread_id`, timestamp, `goal`, `verdict`, the final
      assistant text, plus §4.3's list — files touched, commands that worked, errors hit, artifact
      paths. All of it is already in `state["messages"]` and the trace, so **no model call is needed
      to write it.**
- [ ] **Step 4: Keyword search only (FR-407).** FR-408 gates vectors on *"a measured shortfall in
      keyword recall"* and §11 forbids them until keyword recall is *"measured and found wanting"*.
      **Not built, and not built speculatively.** If M6 shows keyword recall failing, that is the
      measurement that earns them — nothing else does.
- [ ] **Step 5: Every function offline-testable** (NFR-602): write, search, cap, and the profile
      read/append. No API key, no network.

### Task M2 — The benchmark, built and baselined BEFORE the store is wired in

Six cases, three runs each, two sessions per case-run — **and two families**, because the two halves
of this phase make different claims and must be scored separately or neither is attributable.

- [ ] **Step 1: Three `recall-*` cases — a fact.** Session 1 states something specific and invented
      (`"my deploy key is kx-9920"`). Session 2, **in a different thread**, needs it. Scored by grep
      on an answer file. Within one thread the checkpointer already carries context, so a same-thread
      test would measure nothing.
- [ ] **Step 2: Three `profile-*` cases — a standing preference.** Session 1 states how the user
      works (`"always use tabs, never spaces"`). Session 2 gives an ordinary task where obeying it is
      **visible in the output**, and never restates it. Scored by grep on the produced file.
      **This is what gives the profile a pass/fail** — the measurable form of "does it stop asking
      what I already answered" is "does it act on what I already said".
- [ ] **Step 3: Invent every value.** A real port or a real version is answerable from training data,
      and a case the model can pass without remembering measures nothing. The web split's fixtures
      are the precedent.
- [ ] **Step 4: Reset the workspace BETWEEN sessions, keep `/state`.** Two reasons, and the second is
      the important one: it matches reality (different task, different directory, same assistant),
      and it removes the trivial confound of the agent leaving a note in the workspace. `/state`
      remains writable via `run_shell`, which is deliberate — see M3.
- [ ] **Step 5: Verify both directions offline** before any scored run: session 2's check fails on an
      untouched workspace, and passes when the expected answer is present. Zero quota, and it is the
      standing rule that a fixture must be proven to fail before a pass means anything.

### Task M3 — Record the baseline, and expect it not to be zero

- [ ] **Step 1: Run the full benchmark with `AGENT_MEMORY=off`**, 6 × 3 × 2 sessions. **Do not skip
      this as a derivable zero.** That is precisely the reasoning that nearly sank Phase L.
- [ ] **Step 2: Read the traces, whatever the number.** If it scores above zero, find out how — the
      `/state` hole above is the prime suspect, and `run_shell` summaries in the trace will show it
      immediately, as they did for `urllib`.
- [ ] **Step 3: Report what the number means before moving on.** A non-zero baseline does not cancel
      the phase; it changes the claim from *"it can now remember"* to *"it remembers reliably and
      cheaply, where before it improvised"* — which is still worth having and is what Phase L ended
      up being able to say.

### Task M4 — Wire it in, without adding a model-calling node

- [ ] **Step 1: Write on `finish`.** It is already terminal and already receives the trace. Writing an
      episode there is deterministic — goal, verdict, final text, files, commands — so **`finish`
      stays a non-model node.** Only `act` calls a model, and this phase does not change that ratio.
      *(A model-written summary at `finish` was considered and rejected: it would make a fourth
      model-calling node, needing its own justification, and a per-run cost on a free tier.)*
- [ ] **Step 2: Retrieve ONCE, at the first `act` of a session**, not on every turn. Whatever is
      injected joins the message list and — on a provider that returned `cache_read_tokens: 0` on
      every row — is re-sent on every subsequent request. Injecting per turn would repeat Phase L's
      schema-rent mistake with a bigger payload.
- [ ] **Step 3: Cap it hard.** `MEMORY_INJECT_CHARS` in `config.py`, alongside `MAX_SCHEMA_CHARS`, and
      record `memory_chars` on every row exactly as `schema_chars` now is. Start at **1,500 chars**
      (~500 tokens, ~4,500 per run at 9 calls) and treat that as a measured budget, not a guess.
- [ ] **Step 4: The profile is a file, `/state/AGENT.md`**, loaded into every session and capped by the
      same budget. §12 already names it and FR-406 already requires it.
- [ ] **Step 5: `remember(note)` — the sixth tool, written by the agent.** §12 says AGENT.md is
      *"written by the agent"*, and a tool keeps the decision in the agent's hands without making
      `finish` call a model. `RISK["remember"] = "write"`; it writes to `/state`, the second declared
      root, so NFR-201 as amended is satisfied and no violation is recorded.
- [ ] **Step 6: The kill switch, `AGENT_MEMORY=off`** — no write, no injection, no `remember`, and the
      original `SOUL.md`. Every row records `memory` on/off, as `mcp` now does.

### Task M5 — `agent/registry.py`: this time the trigger genuinely fires

- [ ] **Step 1: Count before creating it.** §12 defers it with *"break-even is five tools; add at tool
      six."* Phase L counted five and correctly did **not** create it. Four built-ins + `fetch` +
      `remember` is **six**. The trigger fires — on the stated arithmetic, not on enthusiasm.
- [ ] **Step 2: Keep it a registry, not a framework.** One merged `{name: {fn, schema, risk}}`,
      deterministic order, replacing the merge currently inline in `tools.toolset()`. CE-02 earns a
      framework at break-even *at the current scale* and no further.
- [ ] **Step 3: Schema derivation from signatures is still NOT earned.** MCP tools already arrive as
      JSON Schema; derivation would only serve five hand-written built-ins. Say so and skip it.
- [ ] **Step 4: NFR-601 survives** — adding a built-in tool still touches one file.
- [ ] **Step 5: Re-check `MAX_SCHEMA_CHARS`.** Five tools are 3,159 chars against a 6,000 cap;
      `remember` is small, but the budget is checked at activation and must be seen to pass.

### Task M6 — Measure, against a reading fixed in advance

- [ ] **Step 1: The benchmark with memory ON**, same 6 × 3, and report the two families **separately**.
- [ ] **Step 2: The dev suite, 3 runs per case — the regression guard.** Memory is pure cost there:
      injected context on every request, a sixth schema, and nothing to recall. Phase L's equivalent
      cost 3.3%; this one should be stated the same way.
- [ ] **Step 3: Report recall AND cost together.** Median tokens, `memory_chars`, `schema_chars`,
      turns. **A recall win paid for with a large token increase is a trade, and both halves belong
      on the same table.**
- [ ] **Step 4: The standing trust checks** — zero tampering, zero write violations, one model, egress
      per row, and the fixtures re-verified to still fail untouched.

| recall split | dev suite | reading | what follows |
|---|---|---|---|
| **moves up** | unmoved | Memory works and is affordable | **Keep.** Phase N (skills) next |
| moves up | **drops** | Bought with context the task needed | Cut `MEMORY_INJECT_CHARS` and re-measure before keeping |
| **flat, baseline was 0** | any | Written but never retrieved | Read traces: is it stored, retrieved, or ignored? Three different faults |
| **flat, baseline was high** | any | The agent already improvises this well | Report honestly, as Phase L did. Consider whether the phase is worth keeping at all |
| profile family flat, recall up | — | Episodes work, the profile does not | **Ship recall, drop the profile.** It was taken on knowing this risk |

---

### Cost

| task | quota |
|---|---|
| M0 prompt, M1 store, M2 benchmark, M5 registry | **free** — offline and container probes |
| M3 baseline, memory OFF | 36 sessions, **~600k** |
| M6 benchmark, memory ON | 36 sessions, ~600k |
| M6 dev regression | 15 runs, ~400k |

**~1.6M tokens, roughly two days** at the measured ~1.1M/day ceiling. Four of seven tasks cost
nothing, which is the same ordering Phases J, K and L used: everything provable offline is proven
before quota is spent.

### Spec amendments

- **FR-406 and FR-407 are `[S]`**, and §9 puts `[S]` out of scope *"until the `[M]` set passes
  evaluation."* It has — Definition of Done 9/9. **No amendment needed; this is scope arriving on
  schedule.** Say so rather than letting it look like a liberty.
- **§12 file allowlist:** add `agent/memory.py` (its stated trigger — *"when episodic recall has
  something worth recalling"* — fires), `agent/registry.py` (tool six), `tests/test_memory.py` with
  the same justification standard the other deviations met.
- **FR-408 / §11 vectors: NOT amended, NOT built.**

### What must NOT be done

- **Do not skip the OFF baseline.** It is the single most important run in this phase.
- **Do not close the `/state` `run_shell` hole before measuring it.** Patching first destroys the
  evidence of whether it mattered.
- **Do not let `finish` call a model.** The determinism ratio is the project's most important design
  property; a summary that "reads better" is not worth a fourth model-calling node.
- **Do not build vector search.** It is forbidden until keyword recall is measured and found wanting,
  and M6 is that measurement.
- **Do not report the two families as one number.** They make different claims and one can carry the
  other.

### Verification

```bash
# 1. Offline suite — no API key, no network, read-only root
MSYS_NO_PATHCONV=1 docker run --rm --network none --read-only --tmpfs /tmp:exec \
  -v "$(pwd -W):/app:ro" -v "$(pwd -W)/eval/runs:/app/eval/runs" \
  -v "$(pwd -W)/eval/workspace:/workspace" -v "$(pwd -W)/.agent/homes/_t:/state" \
  personal-agent pytest -q

# 2. FTS5 present in the container (M1 Step 1)
MSYS_NO_PATHCONV=1 docker run --rm --network none personal-agent python -c \
  "import sqlite3;sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(b)');print('fts5 ok')"

# 3. Benchmark fixtures fail untouched, pass with the answer (M2 Step 5) — zero quota

# 4. The baseline that must not be skipped
AGENT_MEMORY=off python eval/harness.py --split recall --runs 3 --pace 15

# 5. The same thing with memory on
python eval/harness.py --split recall --runs 3 --pace 15

# 6. Regression guard — the dev suite must not move
python eval/harness.py --split dev --runs 3 --pace 15

# 7. Kill-switch fidelity: with memory off, the toolset and prompt are unchanged
MSYS_NO_PATHCONV=1 docker run --rm --network none -e AGENT_MEMORY=off -w /app \
  -e PYTHONPATH=/app -v "$(pwd -W):/app:ro" personal-agent python -c \
  "from agent.tools import toolset;print(sorted(toolset()))"
```

**A requirement is satisfied when the cases exercising it pass, not when the code exists.**

---

## Phase N — Skills: on-demand knowledge, and proof it is worth its tokens — **BUILT**

> **Result: 0/18 without skills, 17/18 with them; load rate 17/18 correct, 0 wrong.** Dev suite
> +0.9% total tokens while paying a 1,224-char index on every request and loading nothing — the
> progressive-disclosure claim as a measured number. Evidence in `eval/CHANGELOG.md`.
>
> **The first control was thrown away after one case-run**: `skill-lint` passed with skills OFF,
> because the answer sat in the workspace (`BANNER = "# owner:"` in the checker's own source). Two
> more cases leaked the same way. Rebuilt, and now verified in THREE directions — untouched, a
> plausible guess without the skill, and the skill's answer.
>
> **Authoring is NOT built.** That is Phase O, with its own measurement.

### Context — what this phase is, and how it changed while being planned

**Skills are on-demand knowledge documents the agent loads when it needs them, following the
progressive-disclosure pattern of the agentskills.io open standard.** That is a different thing from
the "agent writes its own procedures" framing this plan carried until now, and the difference decides
the whole design.

**Loading comes first; authoring is Phase O.** You cannot measure whether a *self-written* skill helps
until you can measure whether *any* skill helps. With a known-good skill planted by hand, a failure
means retrieval or adherence is broken — one suspect. With authoring bundled in there are three
(bad skill written / not retrieved / retrieved but ignored) and one number cannot separate them.

**Why progressive disclosure matters more here than almost anywhere else.** Phase L measured that this
provider returns `cache_read_tokens: 0` on every row, so everything in the prompt is re-sent and
re-paid on every turn — four tool schemas were already ~23% of a median run. A knowledge library
injected wholesale would be ruinous. Disclosure inverts that:

```
Level 1  always loaded    name + description, ~40 tokens per skill
Level 2  on demand        the SKILL.md body, ~600 tokens, once
Level 3  on demand        bundled reference files and scripts
```

**The cost is only paid when the knowledge is used.** That claim is measurable, and measuring it is
half of this phase.

> **Exit criterion.** A **load rate** (did the agent pull the right skill?) AND a **delta in success
> or cost** against a control that has memory on and skills off. Plus the disclosure claim: index cost
> versus what injecting the bodies would have cost. The dev suite must not move. **Without the first
> two numbers this phase does not ship.**

### The hazard, appearing for the third time

Phase L's web split scored **18/18 before the feature existed**. Phase M's recall baseline was
verified to have an open bypass before it was trusted. Here the control is not "nothing" — it is the
**current agent, which already has memory**, and `memory.context_for()` already injects this:

```
- earlier you were asked: "..."
  you concluded: ...
  commands that worked: pytest -q; make ship-quartz
```

**Memory already performs a weak form of procedural reuse.** Skills must beat *that*, not beat zero.
If they only help where memory already helps, they are cost with no capability, and the honest
outcome is to say so and revert.

### What was checked while planning

| | |
|---|---|
| Format | Confirmed against real `SKILL.md` files: YAML frontmatter with `name` and `description`, then a markdown body |
| Storage | `/state/skills/` — the layout Phase K fixed, outside the workspace `reset.sh` wipes |
| Tool budget | 6 tools = 3,553 chars against a 6,000 cap. Room for **one** more tool, not two |
| Multi-session rig | Already exists — Phase M added `sessions` chaining to the harness |
| `read_file` | Workspace-bounded by FR-302, so it **cannot** read `/state/skills/`. This is why loading needs a tool at all |
| Docker | Not running at plan time; every container probe below is a task step, not an assumption |

---

### Task N0 — The library, and the parser question (free)

- [ ] **Step 1: Skills live in `/state/skills/<name>/SKILL.md`**, human-readable, human-editable,
      human-deletable, one directory per skill. Bundled files sit beside it.
- [ ] **Step 2: Use a real YAML parser.** The standard's frontmatter is YAML, and a `split(":", 1)`
      parser breaks on a quoted or folded `description`, which is exactly the field retrieval depends
      on. **Add `pyyaml`, pinned, to the `Containerfile` ahead of the `pip.conf` line** — the same
      pattern `mcp` uses, and required for the same reason: `no-index` means nothing installs at run
      time.
- [ ] **Step 3: A malformed skill must not take down a run.** Missing frontmatter, absent
      `description`, unreadable file: skip it, and say so once. A knowledge library that crashes the
      agent when one document is wrong is worse than no library.
- [ ] **Step 4: Ship 8 skills, needing 6.** The two extras are **distractors**, and they are not
      padding: **discrimination is what fails in these systems.** A library where every skill is
      needed cannot tell "retrieved the right one" from "retrieved the only one".

### Task N1 — `agent/skills.py`: the three levels, in one tool

- [ ] **Step 1: Level 1 is an index in the system prompt** — `name` and `description` only, built by
      scanning the directory. Capped by `SKILLS_INDEX_CHARS` and recorded as `skill_index_chars` on
      every row, exactly as `schema_chars` and `memory_chars` already are.
- [ ] **Step 2: Levels 2 and 3 are ONE tool, not two.** `load_skill(name, file=None)` returns the
      SKILL.md body plus a list of bundled files; with `file` it returns that file's contents. The
      budget allows one more tool, and two would spend the headroom for no capability.
- [ ] **Step 3: Bound it exactly like the workspace is bounded.** `file` resolves inside that skill's
      own directory or it is refused — the same `.resolve()`-and-compare shape `policy._inside_workspace()`
      already uses, reused rather than reinvented.
- [ ] **Step 4: `shrink()` applies to the result** (FR-401/402). A long skill body is a tool result
      like any other and spills to an artifact if it overflows.
- [ ] **Step 5: Retrieval is keyword, and it stays keyword.** §11 forbids vectors until keyword recall
      is *measured and found wanting*, and Phase M just measured keyword recall working. Scan the
      directory and score description overlap in Python: at 8 skills an index is unearned, and the
      trigger for revisiting is a stated skill count, not a hunch.

### Task N2 — Level 3, which is the security-significant half

The choice to support bundled scripts was taken deliberately. The reasoning has to be written down,
because "a document can carry executable code into the sandbox" is the kind of sentence that should
stop a reviewer.

- [ ] **Step 1: State the actual delta honestly.** A skill script is executed by the agent calling
      `run_shell` on it. **That grants nothing `run_shell` does not already grant** — arbitrary
      commands inside a container whose boundary Phase K made kernel-enforced. What Level 3 changes is
      **provenance**: code that arrived in a document rather than from the model.
- [ ] **Step 2: Provenance is handled by who writes skills, and in Phase N that is the human.** The
      agent cannot author them here. **When Phase O gives it that power, this becomes a real question
      and must be re-opened** — a self-authored script is the agent writing code it will later run
      without the gate seeing it as an edit.
- [ ] **Step 3: No new execution path, and this is the load-bearing rule.** `load_skill` READS. It
      never executes. Running a bundled script means `run_shell`, through `classify()` and the
      `DANGER` regex like everything else. A skill must not become a way around the gate.
- [ ] **Step 4: Test the escape.** `load_skill(name, file="../../../etc/passwd")` and an absolute path
      must both be refused. This is the `PATH_ARGS` lesson from Phase L: the bypass is tested, never
      assumed closed.

### Task N3 — The benchmark, built before the feature is wired in

Six cases, single-session (the skill is pre-planted, so no chaining is needed), three runs, two
conditions. **The control is memory ON, skills OFF.**

- [ ] **Step 1: Every skill encodes INVENTED, project-specific knowledge.** The web and recall
      fixtures established this: a real convention is answerable from training data, and a case the
      agent can pass without the skill measures nothing. Example — a `qzlint` tool that exists only in
      the fixture, whose codes are listed in a bundled reference file.
- [ ] **Step 2: At least two cases must need Level 3**, or the bundled-file support ships unmeasured.
- [ ] **Step 3: Scored by the check command**, deterministic, no judge — for the reason Phase M's
      `profile-units` demonstrated in both directions: determinism is brittle, and a judge whose
      agreement with a human has never been measured is an opinion with a number on it.
- [ ] **Step 4: Verify both directions offline** before any scored run — untouched fixture fails, and
      a correct answer passes. Zero quota, and the standing rule.
- [ ] **Step 5: Record the control run and READ THE TRACES.** If it scores well without skills, the
      cases are too easy and must be rebuilt — that is Phase L's lesson, and finding it here costs
      nine runs instead of a false claim.

### Task N4 — Instrument the load rate, which is the number nobody else reports

- [ ] **Step 1: Every row records** `skills` (on/off), `skill_index_chars`, `skills_loaded` (which
      ones), and `skill_expected` (which one the case needed).
- [ ] **Step 2: Three outcomes, not two.** Loaded the right skill / loaded the wrong one / loaded
      nothing. **The middle one is invisible in a pass rate and is the interesting failure**, because
      it says the descriptions are not discriminating.
- [ ] **Step 3: Report the disclosure saving as a measured number**, not a design claim:
      `index chars x model calls` against `sum of all bodies x model calls`. That is the whole
      argument for the pattern and it costs nothing to compute.
- [ ] **Step 4: The kill switch, `AGENT_SKILLS=off`** — no index, no tool, byte-identical to the
      Phase M agent.

### Task N5 — Measure, against a reading fixed in advance

- [ ] **Step 1: The skills split, 6 x 3, both conditions.**
- [ ] **Step 2: The dev suite, 3 runs per case.** Skills are pure cost there: an index on every request
      and a seventh schema, with nothing to load. Phase L's equivalent was +3.3%; state this the same way.
- [ ] **Step 3: Trust checks unchanged** — zero tampering, zero write violations, one model, egress per
      row, fixtures re-verified.

| skills split | load rate | reading | what follows |
|---|---|---|---|
| **moves up** | high | The pattern works and discriminates | **Keep.** Phase O (authoring) next |
| moves up | **low** | It succeeded without loading - the cases are too easy | Rebuild the cases. The number is not evidence |
| **flat** | high | Loaded and ignored, or the skill was not the missing piece | Read traces before touching code. Adherence and retrieval are different faults |
| **flat** | low | The descriptions do not match how tasks are worded | One cycle on description wording, then re-measure. If still flat, **revert** |
| any | wrong skill loaded often | Discrimination failure - the distractors did their job | Worth reporting whatever the pass rate does |

---

### Cost

| task | quota |
|---|---|
| N0 library, N1 module, N2 gate, N3 fixtures, N4 instrumentation | **free** — offline and container probes |
| N5 control run, skills OFF | 18 runs, **~400k** |
| N5 treatment, skills ON | 18 runs, ~400k |
| N5 dev regression | 15 runs, ~450k |

**~1.25M tokens, roughly one to two days.** Five of six tasks cost nothing, the same ordering every
phase since J has used.

### Spec amendments

- **Skills are entirely new scope.** Unlike FR-406/407, which were `[S]` requirements arriving on
  schedule, `CONTEXT.md` has **no skills requirement at all** — the only mention is Phase K's amended
  NFR-201. Say so plainly rather than implying a deferred layer fired.
- **§12:** add `agent/skills.py` and `tests/test_skills.py`, the latter to the same justification
  standard as the other deviations: it is the first component where a **document** can direct the
  agent's behaviour, and a skill that is silently skipped for a malformed field looks exactly like a
  skill the agent chose to ignore.
- **§11 vectors: still not amended, still not built.**

### What must NOT be done

- **Do not let `load_skill` execute anything.** It reads. Execution goes through `run_shell` and the
  gate, or the gate has a hole shaped like a document.
- **Do not build authoring.** It is Phase O, and bundling it makes this delta unattributable.
- **Do not inject skill bodies into the prompt.** That is the anti-pattern progressive disclosure
  exists to replace, and on a provider that caches nothing it is the expensive one.
- **Do not report a pass-rate rise without the load rate beside it.** A case that passed without
  loading anything is not evidence for skills.
- **Do not drop the distractors** to make the number look better.

### Verification

```bash
# 1. Offline suite - no API key, no network, read-only root
MSYS_NO_PATHCONV=1 docker run --rm --network none --read-only --tmpfs /tmp:exec \
  -v "$(pwd -W):/app:ro" -v "$(pwd -W)/eval/runs:/app/eval/runs" \
  -v "$(pwd -W)/eval/workspace:/workspace" -v "$(pwd -W)/.agent/homes/_t:/state" \
  personal-agent pytest -q

# 2. pyyaml present in the image (N0 Step 2)
MSYS_NO_PATHCONV=1 docker run --rm --network none personal-agent \
  python -c "import yaml; print('yaml', yaml.__version__)"

# 3. The escape is refused (N2 Step 4) - both must fail
python -c "from agent import skills; skills.load_skill('qzlint', file='../../../etc/passwd')"

# 4. Both directions on the fixtures (N3 Step 4) - zero quota

# 5. The control that must not be skipped
AGENT_SKILLS=off python eval/harness.py --split skills --runs 3 --pace 15

# 6. The same thing with skills on
python eval/harness.py --split skills --runs 3 --pace 15

# 7. Regression guard
python eval/harness.py --split dev --runs 3 --pace 15

# 8. Kill-switch fidelity - the toolset falls back to Phase M's six
MSYS_NO_PATHCONV=1 docker run --rm --network none -e AGENT_SKILLS=off -w /app \
  -e PYTHONPATH=/app -v "$(pwd -W):/app:ro" personal-agent \
  python -c "from agent import registry; print(sorted(registry.toolset()))"
```

**A requirement is satisfied when the cases exercising it pass, not when the code exists.**

---

## Deferred, and honestly so

| | Why it waits |
|---|---|
| **Scheduling** | A small worker over the sync loop. Real, but automation rather than learning |
| **Distribution** | Distributing an assistant not yet shown to learn produces support burden, not information |
| **Chat gateways** | The phase that would force an async service rewrite. Terminal-only was chosen deliberately |

---

## What this will and will not be

**Will:** an assistant with memory, real tools, self-written skills, and — unusually — numbers saying
whether any of it works. A recall figure that rises. A reuse rate. A cost per session.

**Will not:** a general-purpose agent platform - 40+ tools, seven execution backends, six chat
surfaces, a funded team. Matching that surface is not the goal, and pretending otherwise
would be the same over-claim this project keeps having to retract.

**The honest bet:** an assistant whose learning you can point at is worth more than one with more
features and no evidence. That bet is itself measurable, which is the only reason it is worth making.

**And the constraint that outranks the plan:** the model. `real-humanize` fails today because the
agent writes `exponent += 3` where the fix needs `exponent += 3 - exponent % 3`. No phase here fixes
arithmetic. A stronger model moved this project 4/15 → 14/15 with zero code changes — more than any
phase below is likely to.

---

## Verification

```bash
# 1. Unit tests — still no API key, no network, read-only root
docker run --rm --network none --read-only --tmpfs /tmp:exec -v "$PWD:/app" personal-agent pytest -q

# 2. v1 regression — the existing sets must not move as v2 lands
python eval/harness.py --split dev --runs 3 --pace 20

# 3. Real-repo verification — BOTH directions, offline, zero quota (Phase J3)
MSYS_NO_PATHCONV=1 docker run --rm --network none -v "$PWD:/app" \
  -v "$PWD/eval/workspace:/workspace" personal-agent bash -c \
  'scripts/reset.sh real-<name> && cd /workspace && <check>; echo "exit=$?"'

# 4. Rig preflight on a free local model — find rig bugs before spending quota (Phase J4)
AGENT_PROVIDER=openai NIM_BASE_URL=http://host.docker.internal:11434/v1 \
  python eval/harness.py --case real-<name> --runs 1

# 5. The scored baseline — the number that actually matters now (Phase J5)
python eval/harness.py --split real --runs 3 --pace 20
python eval/harness.py --split real --runs 3 --pace 20 --continue   # next day

# 6. Recall benchmark (Phase M — built before the memory code, baseline recorded at zero)
python eval/harness.py --split recall --runs 3

# 7. Clean-machine install (Phase P) — a container that has never seen the repo
docker run --rm -it ubuntu:24.04 bash -c "curl -fsSL <url>/install.sh | sh && personal-agent 'fix the failing test'"
```

On Git Bash, prefix any `docker run` carrying a `-v` mount with `MSYS_NO_PATHCONV=1`.

**A requirement is satisfied when the cases exercising it pass, not when the code exists** (§8.1).
That rule does not relax for v2 — it is the only thing separating this plan from a feature list.
