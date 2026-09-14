# NOESIS

A single-user **personal AI agent**: you give it a goal in plain English, it plans, calls tools, and
works until it reaches a terminal verdict — `done`, `stuck`, or out of budget — then stops and tells
you which.

Three things separate it from chat with function calling, and each is a layer that is not allowed to
collapse into the others:

- **Every tool call is judged before it happens.** A pure `classify()` labels each call
  `auto` / `confirm` / `deny` *before* any side effect. Destructive commands pause for approval
  interactively and are refused outright when running unattended.
- **Nothing reaches the model unfiltered.** Tool output is capped, spilled to disk when it overflows,
  and returned with instructions for reading the rest — otherwise one `pytest` run floods the context.
- **It survives being killed.** State is checkpointed after every step. A `docker kill` mid-task
  resumes having lost at most one step, with no duplicated side effects. Verified, not asserted.

**Only one component ever talks to the model.** The gate, the context manager, the verdict logic and
the harness are ordinary deterministic code — all 938 tests run with no API key and no network.

### Why the evaluation is full of code repair

Because it is the only work with an objective scorer. A test suite exits 0 or it does not; "summarise
my email well" has no exit code. The coding cases are a **capability probe, not the product** — an
agent that cannot fix a real bug in an unfamiliar repository is unlikely to handle your other work
either, and without a scorer you could never tell.

Success is measured, not asserted. Every change goes through one cycle: one change, three runs per
case, keep it or revert it, log the result. Two changes were reverted this week for moving nothing.

## Numbers

**Real repositories — 2026-08-21.** Two tool changes, on the same model, against the 0/18 baseline:

| case | baseline | + `edit_file` | + contiguous reads |
|---|---|---|---|
| `real-rich` | 0/3 | 0/3 | **3/3** |
| `real-click` | 0/3 | 2/3 | **3/3** |
| `real-cachetools` | 0/3 | 2/2 | **2/3** |
| `real-humanize` | 0/3 | 0/2 | not re-run |
| `real-markdown` | 0/3 | — | **never run** |
| `real-more-itertools` | 0/3 | — | **never run** |

**Do not read a set-level percentage off this table.** Three of six cases have been measured with
both changes, and they are the same three that were tuned against — traces read, hypotheses formed
and changes judged on them all week. That is selection, and it is the risk the held-out set exists to
catch elsewhere. The gain is also concentrated: strip `real-rich` out and the second change shows
+1 run on `click` and −1 on `cachetools`.

What is solid is narrower and more useful: **`real-rich` went from impossible to reliable.** Fixing
`rich/console.py` once required emitting 25,308 tokens into a 16,000-token reply. It now takes ten
reads and a single edit.

The read-to-write ratio moved **1:29 → 1:7**. It stopped circling and started acting.

`real-humanize` still fails, and not for a reason any tool fixes: its edit applies cleanly and is
nearly correct, using `exponent += 3` where the fix needs `exponent += 3 - exponent % 3`. The carry
happens; the precision does not survive it. That is arithmetic, and it points at the model.

### The earlier reading, kept because it was wrong in an instructive way

**pass 4/7 (57%)** was quoted from three cases before `real-rich` was included; the honest figure at
that moment was 4/10.

At that point the set had left the saturated band every earlier set sits in — dev 93%, held-out 97%,
multibug 96% — which is what made a tuning cycle measurable here for the first time.

The clearest single case was `real-click`: it burned 255,043 tokens in the baseline and stalled at
6→1 of 6 failures. With `edit_file` it fixed **all six in two edits**, twice. Both passes carried
`verdict: compact` — the agent exhausted its budget and passed anyway, because scoring is by the
check command's exit code and never by the agent's own claim.

`real-rich` was still 0/3 at that stage, with all six of its edits rejected for *"that text appears
2 times"* — ambiguity in a 2,689-line file, not imprecision. That was read as evidence **against**
porting a fuzzy matcher, and it held up: the ambiguity failures disappeared once the agent could see
contiguous code, without ever being addressed directly. A cycle that attacked them head-on, by naming
the line numbers of each match, moved nothing and was reverted.

### The baseline this replaced

**Real repositories — 2026-08-20: pass 0/18.** Six real open-source projects, each vendored at the
parent of a genuine upstream bug-fix commit, scored by the repo's own test suite. 3 runs per case,
0 blocked, cap 30, egress restricted.

| case | files | pass | turns | tokens (med) | verdicts |
|---|---|---|---|---|---|
| `real-more-itertools` | 39 | 0/3 | 30/30/30 | 198,584 | stuck ×3 |
| `real-cachetools` | 41 | 0/3 | 28/20/25 | 245,713 | compact ×2 done ×1 |
| `real-humanize` | 73 | 0/3 | 10/20/1 | 86,422 | done ×2 compact ×1 |
| `real-click` | 156 | 0/3 | 28/27/26 | 255,043 | compact ×3 |
| `real-markdown` | 443 | 0/3 | 8/22/12 | 113,388 | done ×2 compact ×1 |
| `real-rich` | 548 | 0/3 | 29/26/25 | 249,883 | compact ×3 |

**This is the first case set this project owns with real headroom**, after three synthetic
difficulty axes were built and all three were rejected for saturating above 85%.

**The cause of 0/18 is measured, and it is a tool defect, not a reasoning one.** `write_file`
replaces a file entirely, so a five-line fix means emitting the whole file inside `MAX_TOKENS`
(16,000, covering thinking, text and tool arguments together). Real files are 559–2,689 lines:

| file | ~tokens to rewrite | share of one reply |
|---|---|---|
| `humanize/number.py` | 3,898 | 24% |
| `click/_termui_impl.py` | 7,933 | 50% |
| `more_itertools/recipes.py` | 11,531 | 72% |
| `rich/console.py` | **25,308** | **158% — impossible** |

Across 30 real-repository runs: **11 `write_file` calls against 352 `read_file` calls (1:32)**, nine
runs ended `stop_reason: "length"`, and every run that made progress made exactly **one** write.
`real-rich` cannot be passed by any agent using **this** toolset — a case that cannot be passed
measures nothing. *(Resolved: rather than dropping it, the toolset was fixed. With an edit tool the
arithmetic disappears, and `real-rich` now passes 3/3. Dropping it would have been lowering the bar
instead of clearing it.)*

The fix is an **edit tool** taking `old_string` → `new_string`. A budget experiment ruled out the
alternative explanation: given 1M tokens instead of 400k, the agent used 281–516k and made *less*
progress, because the binding limit is the per-reply cap, not the per-run budget.

**0/18 does not mean the agent cannot fix real bugs.** 13 of 18 runs (72%) ended on a *resource*
limit, not a decision: 10 `compact` (all at the 240,000-token threshold) and 3 `stuck` (all at the
30-turn cap). Only 5 ended because the agent chose to stop. On `humanize` it diagnosed the root
cause correctly and ran out of room before applying the fix.

**The `compact` verdicts look like they earn the compaction layer — they do not.** v1 deferred
compaction pending exactly this trigger (*"compact verdicts dominate the baseline distribution"*),
and `compact` never appeared once across 60 fixture runs against 10 of 18 here. So the trigger fired,
and it was tested directly: given 1M tokens instead of 400k, the agent used 281–516k and made *less*
progress. **Running out of budget is a symptom of the whole-file write, not an independent problem.**
The trigger earned a hypothesis; the experiment refuted it. Recorded so the next reader does not
build compaction on a distribution that has already been checked.

Cost: 3.54M tokens for 18 runs. The median case is 245,713 tokens against a 60,000 ceiling — real
repositories cost roughly 4× what v1 was scoped for. One run (`real-markdown` run 1) was interfered
with by hand to clear a hung process and is flagged as not a clean observation.

**Multibug set — 2026-08-19: pass 25/26**, 3 runs per case, cap 25, **4 blocked and excluded**.
Ten cases carrying 3, 4 or 5 independent one-line defects, all of which must be fixed for the suite
to go green.

| bugs | runs | pass | turns (range) | mean turns |
|---|---|---|---|---|
| 3 | 9 | 9/9 | 11–16 | 12.8 |
| 4 | 12 | 11/12 | 12–17 | 14.6 |
| 5 | 5 | 5/5 | 17–23 | 18.4 |

**This set was built to have headroom and does not.** The band was fixed before the data existed —
40–70% discriminates, above 85% means reject the axis — and 96% rejects it. That is **three
difficulty axes tried and three rejected**: misdirection, cross-cutting edits, and now independent
diagnoses. The search for a harder task shape ends here rather than continuing by guesswork.

What survived is more useful than the pass rate: **each additional bug costs about 2.8 turns**, the
first quantified predictive relationship in the project. It implies ~8 bugs would exceed a 25-turn
cap, and it shows the 12-turn cap used by the other two sets would have failed most of this one on
budget alone.

**Incomplete, and not to be quoted as 30.** `multi-catalogue` has zero completed runs and
`multi-payroll` two of three, so this is 9 of 10 cases; the free tier began rejecting ~2 of every 3
requests after ~1.1M tokens. Blocked runs are excluded because **a blocked run never reaches its
check command, so no score exists for it** — not because of anything their row reports, which is
written with `state=None` and therefore reads zero regardless. `--continue` completes the set.

**Held out — 2026-08-19: pass 29/30** on ten cases the system was never developed against,
scored once, 3 runs each, 0 blocked.

| Group | Score | The question it answers |
|---|---|---|
| Matched six | **17/18** | Was the dev score fitted to the dev cases? |
| Harder four | **12/12** | How much headroom is left? |

Dev is 14/15 (93.3%); matched held-out is 17/18 (94.4%) — **statistically indistinguishable, so the
dev score was not overfitted.** The reading was fixed in the plan before the data existed.

Two caveats stated rather than buried. **The four "harder" cases scored better than the matched
six** — the difficulty mechanism (the cause is not where the traceback points) was verified to work
but did not trouble the agent, so this set has little headroom either. And **every dev trace had
been read before these cases were written**, so the selection is not fully independent; all ten were
derived from a taxonomy of ordinary Python defects rather than from observed weaknesses, which is a
partial mitigation, not a complete one.

**Dev baseline — 2026-08-19, `nemotron-3-super-120b-a12b`: pass 14/15**, 3 runs per dev case, 0 blocked.

| Case | Pass | Verdicts | Turns | Tokens (med) | Tampered |
|---|---|---|---|---|---|
| `fix-import` | **3/3** | done x2 stuck x1 | 10/12/10 | 34,394 | 0 |
| `off-by-one` | **3/3** | done x3 | 10/9/8 | 28,877 | 0 |
| `broken-fixture` | **3/3** | done x3 | 8/10/10 | 27,464 | 0 |
| `missing-dep` | **3/3** | done x3 | 5/4/3 | 6,971 | 0 |
| `add-endpoint` | 2/3 | done x2 stuck x1 | 11/12/11 | 35,648 | 0 |

Ceilings: median 27,852 / 60,000 tokens, largest single result 2,908 / 6,000 chars — both inside.

**Verified before being believed:** zero tampering on any pass, zero attempted writes outside the
workspace, one model across all 15 rows, and every fixture re-checked to still fail exit-1 when
untouched. A passing suite proves nothing if the reset quietly stopped working.

**Re-run 2026-08-21 under the Phase K sandbox — 14/15 again, case for case, including which single
case fails.** Not just the same total: a sandbox change that broke something would have moved a
case, not shaved the total. `missing-dep` is the one that mattered and it is 3/3, because its fix is
a plain `pip install` and it proves the user-site install still resolves now that the project tree is
read-only. This is also the first run whose rows record the egress they were measured under as a
fact rather than a default.

**Two runs ended `stuck` and passed anyway.** Scoring is by the check command's exit code, never by
the agent's claim of success — so a correct fix counts even when the agent does not realise it has
finished. Had the verdict gated the score, those two would have been recorded as failures.

### The previous baseline, and what it actually measured

| Date | Model | Result |
|---|---|---|
| 2026-08-19 | `nemotron-3-super-120b-a12b` | **14/15** |
| 2026-08-18 | `meta/llama-3.1-70b-instruct` | 4/15 |

Only the model changed. Same harness, same fixtures, same loop — so **none of the improvement is
attributable to agent design**, and the two rows are separate measurements rather than a delta.

The older run's failures were bucketed at the time as loop defects: 9 of 15 runs never called
`read_file`, all 15 ended `done` (11 wrongly), and 5 rewrote the tests they were judged by. All
three disappeared on a stronger model. **That diagnosis was wrong**, and one tuning cycle was spent
and reverted before the cheapest alternative — a different model on the same free key — was tried.

**Read the token figures the right way round.** The old baseline's 3,266-token median looked
frugal and was not; runs were cheap because the agent quit early. 27,852 tokens with 8–12 turns is
what doing the work actually costs.

## Installing it

Python 3.12+ and git. One line, from anywhere:

```bash
git clone https://github.com/notjwp/Noesis.git && cd Noesis && sh scripts/install.sh
```

It installs into the active virtualenv if there is one, otherwise the user site — never the
system Python — and ends by saying where `noesis` landed and whether that is on your PATH.
Then `noesis`: the first run opens a setup wizard that probes your model endpoint before it
writes a key. `noesis update` is a `git pull` in that checkout, which is why this is a
checkout and not a `pip install` — a package could not update itself.

Not `curl | sh`: the policy gate in `agent/policy.py` classifies that shape as destructive,
and an installer this project ships should be one it would run. Read the script first if you
like; it is under 90 lines.

## Running it

```bash
docker build -f Containerfile -t personal-agent .

# 938 offline tests - no API key, no network
docker run --rm --network none -v "$PWD:/app" personal-agent pytest -q

# a baseline: every dev case, three times, pacing between runs
python eval/harness.py --split dev --runs 3 --pace 20

# resume it if the provider refuses or you interrupt it
python eval/harness.py --split dev --runs 3 --pace 20 --continue

# one case, repeated
python eval/harness.py --case fix-import --runs 3
```

### The API key

**On first run there is nothing to edit.** Any command — `python -m agent --tui`,
`--doctor`, a goal — opens a setup wizard when no key is configured and a terminal is
attached: pick a model, type the key, and it **probes the endpoint before saving
anything**, so a wrong key is rejected there with the provider's own message rather
than twenty turns later as an auth error. It writes `.env` and the current process, so
the command you typed carries on without a restart. `ctrl+k` reopens it later to change
model or rotate the key, and the key need not be retyped to change the model.

Without a terminal — `--worker`, `--channel`, a scheduled task, the eval harness — the
wizard never opens, and a missing key is the same clear `RuntimeError` it always was.
That gate is what stops a cron job hanging on a prompt.

The file path still works and is what a container needs: `cp .env.example .env` and
fill it in. A real environment variable beats both.

Offline tests need no API key and no network. Only the scored run calls a model.

A run that never reached the model is reported as **blocked** and excluded from the
score rather than counted as a failure - `pass 4/13, 2 blocked`, never `pass 4/15`.
Blocked runs are retried, and `--continue` re-runs exactly the ones with no result.

### Restricted egress

Scored runs reach the model through an allowlisting proxy and have **no other route off the
machine** - the agent container sits on an internal Docker network whose only neighbour is that
proxy. The harness brings it up automatically and **refuses to score a split without it**; set
`AGENT_NETWORK=bridge` to run unrestricted, in which case the result is not a compliant scored run.

Verified with the agent image: the model host returns `200`, any other host returns
`403 Filtered`, and a direct connection **by raw IP with no proxy** fails as unroutable. That last
check is the one that matters - a DNS-only barrier looks the same and is bypassed by dialling an IP.

### Two writable roots, and nothing else

The container gets exactly two writable paths, and both are declared on the command line:

| mount | holds | lifetime |
|---|---|---|
| `/workspace` | the task | wiped by `reset.sh` between runs |
| `/state` | checkpoints, and later memory and skills | **survives** `reset.sh` - that is why it exists |

Everything else is refused by the kernel, **including the project itself**, which is mounted
`:ro`. `--read-only` alone does not do this: it makes the root filesystem immutable and leaves
bind mounts untouched, so before Phase K the agent could write to the harness, to `tasks.jsonl`
and to the fixtures it is scored against. Nothing in 30 real-repository traces ever did - but the
tamper check that would have caught it *repairs after the fact*, and a kernel refusal *prevents*.
Verified in both directions before it was believed; the probe and its output are in
`eval/CHANGELOG.md`.

### Memory

The agent remembers across sessions. Two stores, both in the agent home so they survive `reset.sh`:

| | holds | written by |
|---|---|---|
| `/state/memory.db` | past sessions - goal, outcome, files touched, commands that worked | `finish`, automatically |
| `/state/AGENT.md` | durable facts about you and how you work | the agent, via `remember` |

Retrieval is **keyword only** (SQLite FTS5). Vectors are forbidden by the spec until keyword recall
is measured and found wanting, and it was not. Retrieved context goes into the **system prompt**, not
the message list - a message appended per turn would add a fresh copy each turn and grow
quadratically - and is capped at 1,500 characters, with `memory_chars` recorded on every row.

**Measured, six cases x three runs x two sessions, one flag apart:**

| | memory off | memory on |
|---|---|---|
| recall a fact | 0/9 | **9/9** |
| act on a standing preference | 0/9 | **6/9** |
| median tokens | 8,744 | **6,220** |
| mean turns | 5.4 | **2.7** |
| runs ending `stuck` | 7 | **0** |

**Memory was budgeted as a cost and came out a 40% saving.** Injected context is charged on every
request, and the run still got cheaper, because an agent that remembers stops searching for what it
was already told. Every `stuck` verdict disappeared.

The baseline being **0/18** is what makes that delta mean anything, and it was not assumed - the
agent can write to `/state` through `run_shell` today, so it could have passed with no memory layer
at all. It did not; it tried to leave a note in the workspace instead, which `reset.sh` wipes between
sessions by design. Three of the nine profile runs failed on a check that was matching whitespace
rather than memory; the amended check and its provenance are in `eval/CHANGELOG.md`.

`AGENT_MEMORY=off` restores the pre-memory agent exactly. The dev suite is unmoved at 14/15.

### Tools, and what they cost

Four built-in tools (`read_file`, `write_file`, `edit_file`, `run_shell`) plus **MCP**, which
contributes one server and one tool (`fetch`). The server is baked into the image, runs as a
subprocess **inside** the sandbox, and every tool it offers is risk-classified before its schema
reaches the model. `AGENT_MCP=off` restores the four-tool agent exactly.

**Tool schemas are re-sent on every request and this provider caches nothing** — `cache_read_tokens`
was 0 on all 15 rows of a scored run. Four schemas were already ~23% of a median run, so breadth is
the most expensive thing this project can buy: at a real MCP server's ~254 tokens/tool, 24 exposed
tools would breach NFR-402's cost ceiling on schema alone. `config.MAX_SCHEMA_CHARS` refuses to start
a run that exceeds 6,000 chars, and every row records `schema_chars` and `mcp`.

**What MCP actually bought.** Six web cases, three runs each, one flag apart:

| | MCP off | MCP on |
|---|---|---|
| pass | 18/18 | 18/18 |
| turns (mean) | 6.2 | **3.1** |
| tokens (median) | 11,528 | **7,293** |

**No capability gain — the agent could already reach the web.** With MCP off it fell back to
`run_shell` and Python's `urllib`, which works because the container has proxy variables set. It
found that in four calls (`curl`, `which wget`, `python3 --version`, then urllib) and scored 18/18.
The saving is those four calls, and it more than pays for a schema that grew 58%. On the bug-fixing
split, where `fetch` is never called, carrying it costs **+3.3%**.

That baseline is the reason the claim here is "cheaper" and not "it can now browse the web". It was
expected to score 0 and was measured anyway.

### Interactive

```bash
mkdir -p ~/.personal-agent          # the agent home: state now, memory and skills later

docker run --rm -it --env-file .env \
  -v "$PWD:/app:ro" -v "$PWD/eval/workspace:/workspace" \
  -v "$HOME/.personal-agent:/state" \
  personal-agent python -m agent "Fix the failing tests."

# same mounts, different argument
python -m agent --tui               # full-screen chat
python -m agent --tui --resume <id> # ...opened on one thread
python -m agent --list              # past threads
python -m agent --resume <id>       # continue one
```

Destructive commands pause for approval and show the full argument set; a task's
identity is its thread id, so resuming is re-invocation rather than a restart.

`--tui` is the same loop behind a Textual front end, and it is what makes FR-701's word
**chat** true: the input box stays live, so a message sent to a finished thread re-enters it
with its history intact instead of starting over. Turn, tokens and verdict sit in the header,
tool calls stream in coloured by outcome, `tab` picks a past thread, and approval is a modal
that **denies on every dismissal** - escape, a click outside, a closed window - with the deny
button focused so a reflexive Enter refuses.

The plain CLI is unchanged and remains the scriptable path; `textual` is imported only inside
the `--tui` branch, so the CLI, the harness and the unit suite still run without it.

One thing the screen does not do on the default provider: the line above the input shows the
model's reply, but it **arrives whole rather than word by word**. The OpenAI-compatible path
sends no `stream=True`, so there is no first token to show - see NFR-101 in `CONTEXT.md`. The
Anthropic path does stream, and the same line fills in progressively there.

### Planning — built, and **off by default**

`AGENT_PLAN=on` turns it on. It is off because it does not yet do what it says: across nine
scored runs the plan was **never written**. This provider keeps emitting tool calls once its
history holds them — proven with `tools` absent from the request and `finish_reason: tool_calls`
still coming back — so the planning phase never reaches a text-only reply and the fallback (the
goal copied verbatim as a single step) fires every time. Full write-up in `eval/CHANGELOG.md`.

What *does* work is the half that needed no cooperation from the model: writes are **refused by
the gate** while it looks, so read-only research is a property rather than a promise, and
research turns are capped separately (`PLAN_MAX_TURNS`) so they cannot eat the run's budget.

```
  +-- PLAN ------------------------------------------------------+
  | 1. Read tests/test_export.py to find the expected CSV shape  |
  | 2. Add a CSV writer beside the JSON one in ledger/export.py  |
  | 3. Run pytest -q until the suite is green                    |
  +--------------------------------------------------------------+
    [a]ccept  [r]evise  [q]uit >
```

Research turns are capped separately (`PLAN_MAX_TURNS`) and are **not** charged against the
run's turn budget — at `MAX_TURNS = 12`, a shared counter would let four turns of reading starve
the cases planning exists to help. Every row records `plan_steps`, `plan_turns` and
`plan_denied`, so the cost is reported beside the effect.

The interactive home is **persistent**; scored case-runs each get a **blank** one, because a
memory carried from case 1 into case 2 is the same contamination that forced one container per
case-run.

On Git Bash, prefix any `docker run` carrying a `-v` mount with `MSYS_NO_PATHCONV=1` and give
Windows-style paths (`$(pwd -W)`); `$HOME` does not survive the translation.
