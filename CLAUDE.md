# CLAUDE.md

Guidance for Claude Code working in this repository.

**This file is injected into every prompt, so it holds only what changes a decision.** Phase
narratives, cycle results and the reasoning behind each number live in `eval/CHANGELOG.md` (one
section per cycle), `ROADMAP.md` (per-phase plans and outcomes) and `README.md` (the numbers
table). Read those when you need history; do not copy history back into here.

## State

`act -> gate -> execute -> reflect` over a two-provider adapter, a policy gate that is the boundary
(a kernel-enforced sandbox for scored runs only), CLI and Textual TUI, task queue, cron scheduler, email channel, web search, measurement rig. **1,240 offline tests**, green with no API key, no network, a
read-only root filesystem, and without the `mcp` package installed.

| | |
|---|---|
| dev baseline | **15/15**, 3 runs per case, `nvidia/nemotron-3-super-120b-a12b`, at `MAX_TURNS=30`. Re-measured 2026-09-05 after gte-base and extraction-by-default. `add-endpoint` does not flap at the old cap of 12 - it scores **0/3, `stuck` x3**, because its first edit lands on call 13 |
| held out | **30/30**, re-measured 2026-09-05, `done` x30 and zero tamper. The +1 over 29/30 is `float-division` landing on a good seed, NOT a gain: nothing shipped that day is in the graph's path |
| real repositories | **9/18** at 3 runs per case (2026-09-19, the second pass that day); three 3-run passes on the same loop code read **10, 11, 9** - FLAT, the spread is the seed. `rich` 0/3 four passes running and `humanize` 0/3 (its 1/3 that morning was a seed). **The token-growth finding was RETRACTED on 2026-09-22**: the control on `e65d94b` (old code, today's endpoint, 12 of 18 rows before the tier blocked) showed the 236k -> 390k comparison was largely CASE MIX - on the same footing, markdown dropped everywhere, 09-12 was already 331k against the control's 381k, and `rich`+`humanize` have cost 404-414k in every pass including 09-12. What is left is the cheap pair, 155k (09-12) against 207k on the same code today with 284k/319k on the new code between them - n=4 vs 6, ordering unclean, not a result. The repository is neither exonerated nor implicated; do not tune on it. **Budgets removed 2026-09-23** (NFR-401 amended), so the next scored pass is a NEW BASELINE, not a comparison with 9/18. Before: 8/12 at 2 runs, twice (2026-09-12), after the compaction bound (4,000 chars, derived from 87 recorded) and the `read_file` floor (100 lines), both kept on pre-registered conditions; five 2-run passes 7, 7, 8, 8, 8 of 12 |
| Definition of Done | **9/9** · must-have requirements **35/35** |
| search split | **9/9** with `web_search`, **0/9** with it removed |
| tools split | **9/9** on 2026-09-13, the first sweep, after the two terminal cases were rewritten: `pipeline.py` had a RACE (id announced, then logged; the check read the log, so a driver that stopped the build on the announcement lost with the right answer) and the pair could never be honestly REQUIRED (`python server.py > out.log &` in one `run_shell` leaves a process running too). Now: log before announce, a heartbeat every 2s, one goal sentence - "leave it running" - and a 15s freshness clause in the check. **All six terminal runs chose the pair**; before, 5 of 12 did not. A goal sentence moved the tool choice where a SOUL.md rule (`ask_user`, 2 of 3 unchanged) did not - n=6, a hypothesis. Earlier passes 7, 7, 5, 7 of 9. `ask-environment`: every passing run called `ask_user` then `edit_file`, no failing run did, **15 for 15** |
| Phase R (skill revision) | **DELETED 2026-09-23, replacement KEPT on 2026-09-24**: `revision` **3/3** against the 1/3 control, guards `skills` **18/18** and `authoring` **11/12** (against 10/12 recorded on 09-13, so +1). Verified in the FILES, not the score: session 4 opened `runbook` in all three runs, both skills survived, `conventions` still carries `-slate`. R marked a skill suspect when a run ended badly and let the next `done` run overwrite it - it marked on 7 runs and **6 of them PASSED**, so it was off by default from 2026-09-13 and never ran in real use. The fixture settles what it was for: with R off, extraction already writes `runbook` beside the stale `conventions`, so the library ends CORRECT, and the control's 1/3 was `best_match` returning None. Deleted: the suspect store (dropped by migration v5), the trigger, the overwrite. Retrieval instead: `best_match` breaks a tie by RECENCY, and where every shared word is furniture - which at TWO skills means any word they share, the ceiling being len//2 - it falls back to plain overlap on words of four letters or more, because `the` and `work` are in every description `_when()` writes. The FIRST attempt shipped only the tie-break and scored 0/1: `conventions` and `runbook` both say "release", so nothing scored and the tie branch was never reached. Two confounds recorded: the pre-registered `authoring` bar of 11/11 was arithmetic nonsense (the split is 12 rows) and the real comparison is 10/12; and `stuck` is now 8 of 12 verdicts here and 4 of 18 in `skills` because budgets were removed one commit earlier, so the +1 cannot be attributed to retrieval |
| personal splits | on the REAL arm: `recall` **85.7%** (n=21), `skills` **94.4%** (n=36), `authoring` **11/12** (2026-09-24; the split is 12 rows, and "11/11 - every case 3/3" was wrong in this table until then) - on the committed defaults, from 3.3% (n=60). Extraction on, caps unchanged, `author-release` winnable. Earlier 46%/52%/16% averaged the ABLATION arm in |
| NFR-101 first token | streams; p50 **2.7-11.0s** across 9 runs (median 4.7s), measured 2026-09-10 on the tools split |
| memory recall | `eval/measure_recall.py`, 170 episodes / 40 paraphrase pairs: keyword **0/40**, gte-base dense-first **37/40 (92%)**. The `recall` SPLIT shows no difference - both arms 18/18 - because its homes hold three episodes |

## Standing lessons, each paid for once

Ordered by how often they have caught something.

**Measurement**

- **One surprising result is a hypothesis, never a finding.** A pilot case scored 1/3, was written
  up as a capability limit, and scored 3/3 unchanged in the full run. Repeat before believing.
- **Probe a different model before tuning the loop.** A 4/15 baseline was diagnosed as three loop
  defects; all three vanished on a stronger model from the same free key, after a tuning cycle had
  been spent and reverted.
- **A "derivable zero" baseline must still be measured.** The web split was expected to score 0
  without a fetch tool and scored 18/18 — `run_shell` plus `urllib` already reached the web.
- **Infrastructure failure is not a score.** A rate-limited run is excluded and retried, never
  counted as a failed case.
- **Verify the rig in BOTH directions**: that passes are untampered, and that untouched fixtures
  still fail.
- **A fixture must not contain its own answer - and it must also STATE one.**
  `author-release` scored 0/9 because CONVENTIONS.md said the `-quartz` suffix belonged
  in VERSION and then wrote `rel <version> ::` for the changelog, while the check
  demanded `rel <version>-quartz ::`. The agent followed the rule as written. All three
  verification directions passed the whole time. 0/9 -> 3/3 on four words.
- **A fixture must not contain its own answer, and two-way verification will not catch it.** Verify
  three ways: untouched fails, a plausible answer *without* the knowledge fails, the correct answer
  passes.
- **A fixture that PROVOKES the mechanism it controls for cannot measure it.** The three
  `profile-*` cases scored 18/18 with `AGENT_PROFILE_DISTIL` on and 18/18 with it off,
  because `remember` fired 9 of 9 in BOTH arms and wrote the profile itself. Their session-1
  text is "a standing rule... always, without being asked" - close to the strongest possible
  cue to call the tool the change existed to replace, which fires 136 times in 13,049 calls
  in real use. Check what the fixture's WORDING invites before reading a +0 as a verdict.
- **Do not quote a set-level percentage when only part of the set is measured.** "4/10" rests on
  four of six real cases, and they are the ones that were tuned against.
- **A pass rate is not evidence for a mechanism that did not fire.** Check the instrumentation says
  the thing ran before attributing anything to it.
- **A bar that cannot move is not a bar.** FR-205 was pre-registered as "dev 15/15,
  tools 9/9, revert if neither moves" while both splits were AT ceiling, so the revert
  condition was guaranteed before the run started - the same arithmetic as Phase R's
  11/11. For a capability at ceiling the measurable question is USE, not pass rate:
  `git` was called 0 times in 161 calls. Check a bar has room in it.
- **A capability no fixture asks for cannot be measured by the splits you have.** The
  `git` tool scored 0 calls because no case is a git REPOSITORY and no goal mentions
  git - `git status` would have said `fatal: not a git repository`. Check the fixture's
  WORLD, not only its wording, before writing the bar. Reverted anyway: unmeasured
  plus 775 schema chars per turn is a cost with nothing on the other side.
- **A gate rule that never fired in a passing run is not proven safe.** FR-204's first
  version denied `pip install -r ./requirements.txt`, because a clause for a bare local
  path matches every FLAG that takes one. `missing-dep` scored 3/3 and the rule fired 0
  times in 210 calls - it passed because the model wrote `requirements.txt` with no path
  prefix three times out of three. Probe the NEAR-MISS forms by hand; a clean split only
  says the forms the model happened to write were safe.

**The loop and the model**

- **A transport error below the SDK is not a failed case.** A stream fails while it is
  ITERATED, after `create()` returned, so httpx raises through unwrapped and the SDK's
  error table never sees it. Measured: an interrupted run wrote `status: ok, turns 0,
  tokens 0` and scored as a failure. Classify by type AND by message, because the exception
  arrives wrapped - but NOT by "has no HTTP status", because our
  own TypeError has none either and a masked bug costs more than a mis-scored row.
- **A retryable classification with no retry behind it is a comment.** `RETRYABLE`
  listed five exception names and nothing ever made a second attempt; the SDK does not
  retry a bare `APIError` because it carries no status_code. Measured: 15 of 20 calls
  survived, which is 0.3% over a 20-turn run, and six launch attempts produced zero
  scored runs. 20/20 once `call_model` actually retried. Check the retry EXISTS, not
  that the taxonomy does.
- **A guard built for a failure mode you saw once may never fire again.** `_noop_nudge`
  fired 0 times in 30 runs and `_drift_notice` 1 time in 15; both were reverted. Tested
  and mutation-checked is not measured.
- **Rescaling a borrowed threshold is not porting it.** An exploration-drift detector built
  for 25 read-only calls in an unbounded turn, rescaled to 8 against our 13-turn cap, still
  never fired - `run_shell` ends the streak and this agent runs pytest every few calls.
  Check the SHAPE transfers, not just the number.
- **Do not let a borrowed design pick your signal.** 0 edits in 13 calls separated pass
  from fail 9 times out of 9 on `add-endpoint`; the detector shipped was keyed on
  read-streak length because that is the conventional signal, and it measured nothing.

- **Fixing one premature ending reveals the next.** Truncation ended runs at ~12 turns;
  fixing it exposed `MAX_SECONDS`; raising that exposed `BUDGET_TOKENS` at ~20 turns.
  Before raising a cap, check whether the run is starved or just spending badly - one
  spent 421,265 tokens to make a single edit.
- **A truncated reply is not a finished one.** `stop_reason == "length"` means the model
  ran out of output budget mid-sentence. Measured: 8 of 8 runs ending that way were
  scored `done`, and none passed. Any terminal check that ignores `stop_reason` will
  score a cut-off reply as success.
- **The no-edit bucket was two different things wearing one label.** 105 of 279 failing
  runs made zero writes, but per split: `real` 64% and `dev` 53% were runs CUT OFF by the
  turn cap - both now ~0 after the cap raise and the run_shell docstring. `search` is
  100% no-edit and always will be: its cases are QUESTIONS, so not editing is correct.
  Never aggregate a bucket across splits whose cases want different things.
- **An ablation arm averaged into the headline reads as a capability gap.** `recall 46%`
  and `skills 52%` were quoted for days as the weak half of this project. Separated, the
  real arms are 85.7% and 94.4% and the controls are 0% and 8.6% - the split was WORKING
  and the control was proving it. Check whether a split has an ablation before quoting it.
- **MAX_TURNS was the binding failure, not the model.** 12 -> 30 took dev 13/15 -> 15/15
  and eliminated `stuck` across 45 runs (25% historically). Comparable agents cap a parent
  at 200-500. Before raising it, check the run is
  STARVED: median spend was 42-54k of 200,000, so tokens never bound.

- **Caps derived against one model are a confound when you swap the model.**
  `nemotron-3-ultra-550b-a55b` scored 10/18 against the 120B's 10/18, and 8 of 8
  losses were caps, not wrong answers. The 120B never reached `MAX_COMPACTIONS`
  in 18 runs; ultra reached it in 4 and each ended `stuck` there. No passing run
  in EITHER arm ever compacted more than twice. Swap the model and re-derive the
  budgets before reading the score.
- **A hung model call is not bounded by `MAX_SECONDS`.** The cap is checked
  between turns, so one HTTP call that never returns is never checked - a run sat
  112 minutes at 0.78% CPU. Watch container age, not row count.
- **A session that never calls a tool has no cap at all.** `turns` is incremented by
  `execute`, so a run that answers in words never advances it and `max_turns` cannot
  bind; the thrash detector misses it too, because it reads tool-call SIGNATURES.
  Measured live: 53 model calls and 200,681 tokens on "just acknowledge this". Fixed
  on repetition with no call EVER made - not on a streak, because `add-endpoint`
  repeats itself identically up to four times mid-run across 942 rows and passes.
- **A cap sized against one failure mode outlives it.** `MAX_SECONDS` was set for a
  hanging tool; tools take 9s of a 950s run and it was ending working runs instead.
  Re-derive a cap when the thing it bounds changes shape.

- **A task requirement binds; advice about a tool does not, wherever it sits.** "Leave the
  build running" in the GOAL moved `start_terminal` 6 of 6; "if looking does not settle it,
  ask" in SOUL.md moved `ask_user` 0 of 1 with the same advice already in the tool's own
  description. `edit_file`'s wording worked because it changed what the model believed the
  tool DID. Put requirements in the goal or the check; do not expect a rule to be obeyed.
- **Deterministic injection works; agent choice does not.** THIRD confirmation:
  extraction at session end fired 12 of 12 where the `learn` TOOL fired 3 of 117, and
  took `authoring` from 3.3% to 9/12. Before that, `write_episode` -> `context_for`
  needed no decision and went 0/18 -> 15/18 while `learn` was called 0 times in 15
  sessions. Prefer a rule over a request, every time.
- **`skills_loaded` is a TOOL-CALL counter, not evidence the skill reached the model.**
  The auto-skill was injecting the body into the system prompt on every turn
  (`skill_opened` 19/13/20) while that field read empty. Check the trace event, not the
  tool count - I built an argument on the wrong field twice in one hour. `write_episode` → `context_for` needed
  no decision and went 0/18 → 15/18. The `learn` tool needed one and was called 0 times in 15
  sessions. Prefer a rule over a request.
- **On this provider a tool-call history keeps producing tool calls** whether or not a tool is
  offered — proven with `tools` absent from the payload entirely. Neither an instruction nor an
  absence stops it. This is why planning as a *phase* failed: a phase inherits the message list.
- **A declared JSON schema is not enforcement.** Numeric arguments arrive as strings; coerce at the
  tool boundary.
- **A tool the model cannot see is a function, not a capability.** `run_python` and its tests were
  green with no `TOOLS` entry. Check the live toolset, not the suite.
- **A prompt that lists SOME of the tools is a prompt that hides the rest.** SOUL.md
  named 4 of 7; across 624 runs those 4 took 95.5% of all calls, and 67% of
  `run_shell` was doing `search_files`'s job - 1,266 `find`, 755 `grep`, 708 `ls`.
  `search_files` was used 70 times in 8,820 calls. List every tool or list none.
- **Descriptions are load-bearing.** `edit_file`'s wording took real repositories 0/9 → 4/7. Never
  derive them from parameter names.
- **Check which limit binds before spending quota on the other.** Raising `BUDGET_TOKENS` (per run)
  changed nothing because the wall was `MAX_TOKENS` (per reply).
- **Compaction is not earned.** `compact` verdicts look like starvation; a budget experiment gave
  the agent 1M tokens, it used 281–516k and made *less* progress. The verdicts were a symptom of
  the write problem, not a cause.
- **Ambiguity in edits was a symptom, not a cause.** Naming duplicate-match line numbers moved
  0/3 → 0/3 and was reverted; the failures vanished once the agent could read contiguous code.
  Fuzzy matching stays unearned.
- **Each extra independent bug costs ~2.8 turns.** Difficulty on that axis is a budget question,
  not a reasoning one.

- **A number that improves for a reason you have not identified is not a result.** A
  hybrid retrieval build measured 6/6 against a 5/6 baseline and the gain was a bug:
  the query builder ranked candidate words by document frequency ascending, so words
  appearing in NO episode sorted first and ate the slots. The dead terms shortened
  the query, and the shortening was the whole effect. Fixed, it scored 3/6.
- **Quantisation is measured per model, never assumed to transfer.** MiniLM int8 scored
  identical to fp32 at a quarter the size; gte-base int8 loads, runs, raises nothing and
  scores **1/40** against 37/40. A broken quantisation looks exactly like a working one.
- **Model size predicts retrieval accuracy backwards here.** On the recall corpus both
  335M models scored BELOW gte-base at 109M, and bge-small at 33M below MiniLM at 23M.
  Benchmark the candidates; do not rank them by parameter count.
- **A lane that scores zero still takes slots.** Reciprocal-rank fusion of keyword (0/40)
  with dense scored 12/40 where dense alone scored 23/40. Order beats weighting: a lane
  with no signal must not displace one that has any.
- **A probe against the answer set is not a probe against the corpus.** An embedding
  ranked the right episode 1 of 6 among the six known targets, and never reached the
  top 3 among the 35 episodes it would actually compete with.

**Cost**

- **Tool schemas are rent, charged per turn, and this provider caches nothing.**
  `cache_read_tokens` is 0 across all 335 recorded rows. At 3,518 chars, definitions are already
  ~58% of a median run's billed tokens. `MAX_SCHEMA_CHARS = 10,000` is derived, not chosen: it is
  the largest cap at which NFR-402's median still holds. Measure whether a provider caches before
  assuming tool breadth is cheap.
- **The ~1.1M/day ceiling was wrong.** 3,677,100 scored tokens in one day with no throttle
  observed. Budget by wall-clock and blocked-run rate instead - the binding limit is that
  a single `real-*` run takes 10-20 minutes and can block on `APITimeoutError`.
- **A 30-run scored pass costs ~1.1M tokens and saturates the free tier for the day.** Budget one
  scored run per day; after that the tier rejects ~2 of 3 requests.

- **A summary the model writes has no length until you give it one.** `compact` accepted
  whatever came back; 6 of 87 were over 20,000 chars and one was 49,496 for 24 messages -
  larger than the context it was rescuing - so the run compacted twice more against a head
  it never touches and died. p90 of the healthy ones was 3,045. Bound it at 4,000, derived.
  Four of the six runaways were the ULTRA run's "caps confound": the diagnosis was wrong,
  the advice (re-derive before reading a swapped model's score) still holds.
- **A capability with no measurement may be BROKEN, not merely unproven.**
  `start_terminal`/`read_terminal` shipped with seven passing tests and were described for
  two weeks as "unmeasured, endpoint trouble". They were unusable: `read_terminal` blocked
  forever on a live process, and every attempt to measure them was the bug reporting itself.
  Nine lost attempts. When a split will not produce rows, suspect the code before the tier.
- **A capability you do not have leaves no trace, so traces cannot tell you it is
  missing.** Seven reference-implementation modules ranked by keyword and tested against recorded
  runs found 0 useful; reading our own code for gaps found nine real tool defects
  in an afternoon. `read_file` never logged a binary read because it never refused
  one. Find the gap first, then look for their code that fills it.

**Rig and environment**

- **`git revert --no-commit` leaves the revert STAGED, and `git checkout` carries it.**
  A docs commit that said "no code in this commit" silently reverted two cycles and
  invalidated the experiment comparing them - both arms were controls. The tell was a
  test count dropping 500 to 490, explained away instead of checked. Verify the arms
  DIFFER before running a controlled comparison.

- **One search endpoint is a rate limit wearing a capability's costume.** `html.duckduckgo.com`
  returns results once, then blocks, and every retry re-arms a ~30s cooldown - so an in-tool
  retry makes it strictly worse. Fanning out across engines fixed it with no retry and no
  sleep: 6 of 6 back-to-back searches returned results. Declare every host the library may
  dial, not the one you hoped it would.

- **`--continue` resumed the wrong directory for weeks.** It sorted run directories by
  NAME, so `control-20260922-e65d94b` sorted after every `2026...` timestamp and every
  resume picked the real-repo control, then refused it. Fixed 2026-09-25 to sort by each
  manifest's own `started`. A resume that refuses is visible; one that appends into the
  wrong baseline would not have been.
- **Never pipe the harness through `tail`**, and never wrap it in `timeout`. `tail` buffers until
  exit so a hang looks like progress; `timeout` kills the client but leaves the container running,
  and the orphan corrupts the shared workspace mid-case. Three runs were lost that way. Use
  `--continue`.
- **`.env` reached only containers.** The harness passed it in with `--env-file` and
  nothing else ever read it, so the CLI, a plain shell without exports and a Windows
  scheduled task all started with no API key and exited at once. Loading it belongs in
  the ENTRY POINT (`agent/__main__.py`, under `if __name__ == "__main__"`), because
  config.py resolves every tunable at import time and CE-05 forbids the library doing
  disk I/O on import.
- **A default that asserts the safe answer is how a row comes to claim a condition nobody checked.**
  `AGENT_EGRESS` defaulted to `"restricted"` and nothing ever set it, so every trace row ever
  written claimed restricted egress. Where a fallback describes what was measured, it says
  `UNKNOWN`.
- **A preflight samples a moment; a run occupies an hour.** Gating the start is
  necessary and never sufficient. A model probe passed 3/3 and the endpoint 503'd on
  the very next request; the driver then ground through every case-run producing
  blocked rows. Gate the start AND abort the run.
- **`CREATE TABLE IF NOT EXISTS` is a migration that silently does nothing.** It is
  right for a fresh database and wrong for one on disk. Measured: `episodes_fts`
  gained `tokenize='porter'` and every earlier database kept the default while the
  code assumed porter, with no error. `agent/migrations.py` and `PRAGMA
  user_version` now carry it - and note `with conn:` does NOT wrap DDL, so a
  migration needs an explicit BEGIN or a half-applied one can never retry.
- **A blocked connection is not proof of a boundary.** "Could not resolve host" is DNS failing;
  re-test by raw IP.
- **"Running" is not "usable".** A proxy reported `Running: true` for two hours while failing every
  request. A preflight must probe the operation the dependent code performs.
- **`--read-only` makes the ROOT FILESYSTEM immutable and leaves bind mounts untouched.** That is
  why the project tree is mounted `:ro` separately.
- **A config file mounted into a container is read when the process starts, not when it changes.**
  Recreate the container; verify the effect, never the mechanism.
- **A test fixture that stubs a builtin stubs it for EVERYTHING.**
  `monkeypatch.setattr(tools.time, "sleep", ...)` reaches the `time` module itself, not a
  copy - so an autouse fixture written to keep seven `web_search` tests fast silently made
  every `time.sleep` in 1,126 tests a no-op. Three terminal tests then passed only because a
  blocking `readline()` happened to synchronise them. Wait on the CONDITION, not the clock.
- **Text files crossing an OS boundary need their line endings pinned** — use `newline=""`.
- **When a fixture-era design decision meets real code, re-check its stated premise.** Whole-file
  writes, `shrink()`'s character caps and the untimed check were all sound for 10-file practice
  projects and all broke on real repositories. The decisions were not wrong when made; their
  premises expired.

## CONTEXT.md is the authority

The binding build specification, not background reading. Its own precedence rules:

- **§9 is the only build order.** §5–7 describe the *finished* system and must not be implemented
  top to bottom. `[S]`/`[C]` items are out of scope until the `[M]` set passes evaluation.
- **§12 is a file allowlist.** Create the files it lists and no others; a deferred file is created
  only when its stated trigger fires. Every deviation so far is justified in writing inside §12
  itself — hold a new one to that standard.
- **§13 (Code Economy) is binding.** A violation is a defect, not a style choice.
- **Where §3/§4 and §13 disagree, §13 governs.** §3 is the logical flow; §13 is the code shape.
- **When a requirement and existing code disagree, the requirement wins** — say so rather than
  silently reinterpreting it. §8.2 records conflicts between requirements rather than papering
  over them.

## Architecture

Goal in natural language → tool-calling loop → terminal verdict. Three layers you must not
collapse:

- **Policy gate.** Every call is classified `auto`/`confirm`/`deny` *before* any side effect.
  `classify()` in `policy.py` is pure — no logging, no counters, no writes.
- **Context manager.** No tool output reaches the model unfiltered. `shrink()` caps each result,
  redacts secrets, spills the full text to `.agent/artifacts/`, and returns head + elision + tail +
  the path **plus instructions for reading it** — a bare path is ignored in practice.
- **Checkpointing.** State is written after every node transition, keyed by `thread_id`. A kill
  loses at most one node; resume is re-invocation with the same id.

**Only `act` calls a model.** Everything else is ordinary code and must be unit-testable without an
API key (NFR-602). This ratio is the most important design property in the system — resist pushing
logic into prompts.

**`gate` and `execute` must never merge** (CE-07). `gate` suspends on `interrupt()` and re-runs
from its first line on resume; merged, every already-executed tool fires twice. Nothing upstream of
a suspension point may have side effects (FR-305) — that applies to `adopt` as well as `gate`.

`config.py` is the single source of truth for the workspace root, model, caps and budgets. FR-302
and NFR-201 both depend on the workspace root having exactly one definition.

Prompts live in `prompts/` (`SOUL.md`, `PLAN.md`), version-controlled, never string literals
(NFR-603), read **inside** the node, never at import time (CE-05).

**Invariants that look wrong in isolation and are not** — §9 Step 2's corrections:
`START -> act`, not `START -> plan`. `done` is gated on whether any tool call was ever made, not on
a cursor. `failures` is a plain `int` counting *consecutive* failures, reset to 0 on a clean turn.
`RISK` is the single path through `classify()` — a name special-case would make the declaration
dead. State is a plain `TypedDict`: no reducers, no `Annotated`.

## Commands

```bash
git clone --filter=blob:none --sparse https://github.com/notjwp/Noesis.git && cd Noesis && python3 install.py
                                  # the install: a sparse, partial checkout (agent, prompts,
                                  # scripts - ~1 MB, not the 29 MB with eval), editable, into
                                  # the active venv or the user site. Ends by saying where
                                  # `noesis` landed. Windows: `;` for `&&`, `python` for `python3`

python -m agent "goal"            # interactive; destructive calls pause for approval
python -m agent --tui             # NOESIS; --tui --resume <id> opens one thread
                                  # FIRST RUN needs no .env: with no key and a TTY,
                                  # any command opens the setup wizard, which PROBES
                                  # the endpoint before writing one. ctrl+k reopens
                                  # it. No TTY (worker, cron, harness) = no wizard,
                                  # which is what stops a scheduled task hanging
python -m agent --list            # past threads, newest first
python -m agent --resume <id>     # continue a thread; a task's identity IS its thread id

python -m agent --submit "goal"   # queue a task, print its id, return immediately
python -m agent --worker          # drain the queue; resumes anything a dead worker left
python -m agent --tasks           # queued / running / awaiting-approval / done / failed
python -m agent --attach <id>     # follow a running task's trace; ctrl-c detaches
                                  # READ-ONLY on purpose: a worker runs
                                  # autonomous, where confirm is already deny,
                                  # so there is nothing to answer. Events are
                                  # kept 7 days after a task concludes

python -m agent --doctor          # every precondition, each line ok or FAIL; changes nothing
python -m agent --cancel <id>     # stop a queued or running task
python -m agent --channel-check   # probe imap AND smtp; sends and queues nothing
python -m agent --channel         # read email, queue what arrives, send the answers
                                  # needs AGENT_EMAIL_USER / _PASSWORD / _ALLOW;
                                  # the allowlist is DEFAULT DENY and the first
                                  # pass adopts the inbox rather than answering it

python -m agent --schedule "0 9 * * 1" "goal"   # run it every Monday at 09:00
python -m agent --schedules       # schedules, soonest first
python -m agent --unschedule <id> # remove one

python -m agent --review          # what needs attention, and queue a review of it
python -m agent --schedule "0 9 * * *" "@review"   # ask it every morning

python eval/harness.py --split dev --runs 3 --pace 20              # a baseline
python eval/harness.py --split dev --runs 3 --no-preflight         # measure a flapping endpoint anyway
python eval/harness.py --split dev --runs 3 --pace 20 --continue   # resume an interrupted one
python eval/harness.py --case fix-import --runs 3                  # one case, repeated

scripts/reset.sh <case-id>        # restore /workspace to a fixture's state (idempotent)
powershell -File scripts/install-tasks.ps1        # run --channel and --worker at logon
powershell -File scripts/install-tasks.ps1 -Remove
pytest                            # 1,240 tests, no API key, no network
```

Tests run in the container, which is the measured environment: read-only root, `--network none`,
`/workspace` and `/state` the only writable roots. The host Python is for editor resolution only.

A run that never reached the model is **blocked**, excluded from the score rather than counted as a
failure — `pass 4/13, 2 blocked`, never `pass 4/15`.

## Evaluation discipline

- **One change per cycle.** Two and the delta cannot be attributed.
- Prompt changes are changes and are measured like code changes.
- **Revert anything that does not move the number**, including changes that "seem right".
- Report pass counts across 3 seeds. 1-of-3 is not a passing case.
- Log every cycle in `eval/CHANGELOG.md`: hypothesis, change, before, after, kept or reverted.
- Do not read held-out traces while tuning.
- **A requirement is satisfied when the cases exercising it pass, not when the code exists.**

## Environment

SCORED RUNS are confined to a container (§11; NFR-204 as amended). Interactive use - `noesis`, the
TUI, the worker - runs natively, and the policy gate is the whole boundary: a hardline tier
that no approval can override, `destructive` that asks, and approvals the interface may
remember for the process. `--doctor` says so. This machine is Windows 11 with Docker Desktop
and Git Bash — not WSL2. `.agent/` is gitignored.

## Say when it does not work

**Report failure plainly, in the first sentence, without softening.** "This did not work",
"the number did not move", "I got this wrong" — not "partially successful", not a win
reframed around a caveat. A capability that ships and is never used is a failure and gets
described as one: `learn` was called 0 times in 15 sessions, planning costs 30% more tokens
for no pass-rate gain, and both are recorded that way rather than as features.

**Unmeasured means unproven, and gets said out loud.** Code that exists is not a
requirement satisfied. A cycle is kept only when a number moved; anything that did not move
is reverted, including changes that "seem right". One favourable n=3 is a hypothesis - this
project has already retracted a 1/3 that re-measured at 3/3.

**Never quote a number better than the one measured.** No cherry-picking the better of two
identical runs, no set-level percentage when only part of the set ran, no attributing a pass
to a mechanism without checking the instrumentation says it fired.


## Working guidelines

Behavioural rules that reduce common mistakes. These govern *how* to work; the sections above
govern *what* this project is.

**1. Think before coding.** State assumptions; if uncertain, ask. Present multiple interpretations
rather than picking silently. Say so when a simpler approach exists. If something is unclear, stop
and name what is confusing.

**2. Simplicity first.** Minimum code that solves the problem. No speculative features,
abstractions for single-use code, unrequested configurability, or error handling for impossible
scenarios. If 200 lines could be 50, rewrite it. Ask: *would a senior engineer call this
overcomplicated?*

**3. Comments earn their place, and the ceiling is three lines.** State *why*, never
*what* — the code says what. No comment longer than 3 lines; if the reasoning needs more,
it belongs in `eval/CHANGELOG.md` with a one-line pointer here. Do not narrate history in
source ("this was refuted", "the first version did X"), do not restate the requirement ID
and its text, and do not explain a decision the code makes obvious.

**EXCEPTION, and it is the only one: tool docstrings in `agent/tools.py` are not comments.**
`@tool` derives the JSON schema description from them, so the model reads them at runtime -
`edit_file`'s wording took real repositories 0/9 to 4/7. They are prompt text and are
governed by measurement, not by this rule.

**4. Surgical changes.** Touch only what you must. Do not "improve" adjacent code, comments or
formatting; do not refactor what is not broken; match existing style. Remove imports and variables
*your* change orphaned — mention pre-existing dead code rather than deleting it. **The test: every
changed line traces to the request.**

**5. Goal-driven execution.** Turn tasks into verifiable goals — "add validation" becomes "write
tests for invalid inputs, then make them pass". For multi-step work, state a brief plan with a
verification per step. Strong success criteria allow independent looping; weak ones ("make it
work") force constant clarification.
