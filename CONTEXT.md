================================================================================
PERSONAL AGENT — BUILD SPECIFICATION
Agent-facing prompt / project context document
================================================================================

--------------------------------------------------------------------------------
0. HOW TO USE THIS FILE
--------------------------------------------------------------------------------

Paste this whole file as project context into a coding agent (Claude Code,
Antigravity, Cursor) or attach it as CONTEXT.md at the repo root.

Read sections 1-4 to understand the system. Read section 9 to know what to build
FIRST. Sections 5-7 are the full requirement set for the finished system and are
NOT a build order — do not implement them top to bottom.

Rules for the implementing agent:
  - Section 9 is the only build order. Anything marked [S] or [C] in sections
    6-7 is out of scope until the [M] set passes evaluation.
  - Section 12 lists every file that may exist in v1. Do not create others.
  - Section 13 is binding. Violating a CE rule is a defect, not a style choice.
  - Every deterministic node must be unit-testable without an API key.
  - When a requirement and existing code disagree, the requirement wins; say so
    rather than silently reinterpreting it.


--------------------------------------------------------------------------------
1. PROJECT DESCRIPTION
--------------------------------------------------------------------------------

A single-user autonomous agent that runs on the owner's personal machine. It
accepts a goal in natural language, decomposes it into steps, and works the
steps by calling tools — reading and writing files, running shell commands and
Python, operating git, searching and reading the web — looping until the goal is
met, it gets stuck, or it exhausts its budget.

Three properties separate this from a chat wrapper with function calling:

  (a) EVERY tool call passes a deterministic policy gate before any side effect.
      Nothing destructive runs unapproved. In unattended mode, anything that
      would need approval is refused and queued for later review rather than
      guessed at.

  (b) NO tool output reaches the model unfiltered. Results are truncated, the
      full output is spilled to an artifact file, and the model gets a summary
      plus a path it can re-read selectively. This is what allows tasks to run
      past ~15 turns without the context window collapsing.

  (c) State is checkpointed after every node transition. A killed process loses
      at most one node of work, and a task resumes rather than restarts.

Target environment: Fedora Linux natively, Windows via WSL2. Execution is
confined to a container with a single bind-mounted workspace directory.

Success is measured, not asserted: a fixture suite of repo-repair tasks is run
headlessly and scored by exit code. The pass rate is the project's headline
number.


--------------------------------------------------------------------------------
2. ARCHITECTURE
--------------------------------------------------------------------------------

                         +----------------------+
                         |  INTERFACE           |
                         |  TUI / CLI, streamed |
                         +----------+-----------+
                                    |
                         +----------v-----------+
                         |  SCHEDULER + QUEUE   |
                         |  cron, task rows     |
                         +----------+-----------+
                                    |
               +--------------------v--------------------+
      +------->|  ORCHESTRATOR                           |
      |        |  plan . act . reflect                   |
      |        |  checkpointer -> /state/state.db        |
      |        +--------------------+--------------------+
      |                             |
      |                  +----------v-----------+        +---------------+
      |                  |  POLICY GATE         |<------>| HUMAN         |
      |                  |  auto / confirm/deny |        | approve, edit |
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
      |          |                  |                  |
      |          +------------------+------------------+
      |                             |
      |                  +----------v-----------+
      +------------------+  CONTEXT MANAGER     |
                         |  truncate . spill    |
                         |  . compact           |
                         +----------------------+

Layers the naive design omits, and why each exists:

  POLICY GATE      The model will eventually emit `rm -rf`, `git push --force`,
                   or a pip install into system Python. Classify before running.

  CONTEXT MANAGER  A single `ls -R` over node_modules or a 3000-line file read
                   ends the task. Bound every result on the way in.

  SANDBOX          "Terminal" is not a tool, it is an execution environment with
                   an enforced workspace root.

  SCHEDULER SPLIT  The chat process must not be the execution process, or you
                   cannot detach from a task that has been running 40 minutes.


--------------------------------------------------------------------------------
3. ORCHESTRATION CONTROL FLOW
--------------------------------------------------------------------------------

                            START
                              |
                              v
                          +--------+
              +---------->|  PLAN  |  decompose goal into 2-6 steps
              |           +---+----+
              |               |
              |               v
              |           +--------+
              |    +----->|  ACT   |  LLM emits tool_use blocks OR final text
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
              |    |      | EXECUTE |  run approved tools, catch exceptions
              |    |      +---+-----+
              |    |          |
              |    |          v
              |    |      +---------+
              |    |      | OBSERVE |  truncate, spill, emit tool_result blocks
              |    |      +---+-----+
              |    |          |                                |
              |    |          v                                |
              |    |      +---------+<-------------------------+
              |    |      | REFLECT |  deterministic checks only
              |    |      +---+-----+
              |    |          |
              |    |  continue|
              |    +----------+
              |               |
              |    compact ---+---> [ COMPACT ] ---> back to ACT
              |               |
              +--- replan ----+
                              |
                     done | stuck
                              |
                              v
                          +--------+
                          | FINISH |----> END
                          +--------+

Node classification:

  LLM CALLS (3)          plan, act, compact-summarisation, and the reflect
                         node's optional tie-break (default: none)
  DETERMINISTIC (5)      gate, execute, observe, reflect, finish
  HUMAN                  await approval, reachable only from gate

Only three nodes touch the model. Everything else is ordinary code you can unit
test. This ratio is the single most important design property of the system.


--------------------------------------------------------------------------------
4. WORKFLOW / EXECUTION SEMANTICS
--------------------------------------------------------------------------------

4.1 TURN LIFECYCLE

  1. A goal arrives from chat or from the task queue.
  2. PLAN produces an ordered step list. Cursor set to 0.
  3. ACT calls the model with the full message history, the tool schemas, and
     the current step injected into the system prompt (NOT appended as a user
     message — that pollutes history and creates gaps).
  4. Router inspects the last assistant message:
       - contains tool_use blocks  -> GATE
       - text only                 -> REFLECT (step complete, not termination)
  5. GATE classifies each tool call independently. Mixed verdicts in one turn
     are legal: some calls approved, others denied.
  6. EXECUTE runs approved calls. Tool exceptions are caught and converted to
     observations; they never propagate out of the node.
  7. OBSERVE shrinks each result, spills overflow to .agent/artifacts/<id>.txt,
     and appends tool_result blocks as a single user message.
  8. REFLECT emits exactly one verdict. Checks run in this fixed order:
       a. spent_tokens > 60% of budget          -> compact
       b. turns >= max_turns                    -> stuck
       c. last 3 tool signatures identical      -> stuck
       d. 3 consecutive failures                -> replan
       e. last message is assistant text        -> done if final step,
                                                   else continue + advance cursor
       f. otherwise                             -> continue
  9. Terminal verdicts route to FINISH, which writes durable memory and ends.

4.2 APPROVAL SEMANTICS

  Interactive mode: a `confirm` verdict suspends the graph. The user sees the
  exact command or argument set, unabbreviated, and approves or rejects.

  Autonomous mode: `confirm` becomes `deny`. The request is recorded in a review
  queue. The agent receives a denial observation and must find another route.
  The same policy code serves both modes — do not fork it.

  CRITICAL RE-ENTRANCY RULE: the gate node re-executes from its first line when
  execution resumes after approval. It must therefore be free of side effects.
  No logging, no counter increments, no database writes inside gate. This rule
  applies to any node placed upstream of a suspension point.

4.3 CONTEXT DISCIPLINE

  - Each tool declares its own output cap. Shell gets more than file writes.
  - Over-cap results are written to disk in full; the model receives head lines,
    an elision marker, tail lines, and the artifact path.
  - The spill message must tell the model how to inspect further (read_file with
    offset/limit, or grep against the path). A path with no instruction is
    ignored by the model in practice.
  - Compaction preserves the first two messages and the last six verbatim,
    summarising only the middle. The summary retains: decisions made, files
    touched, commands that worked, errors hit, artifact paths. Nothing else.

4.4 PERSISTENCE

  - Checkpoint after every node transition, keyed by thread_id.
  - A task's identity is its thread_id. Resume is re-invocation with the same id.
  - Chat threads and background tasks share the checkpoint store. A chat session
    attaching to a running task is just a reader on the same thread.

4.5 EVALUATION LOOP

  1. Reset workspace to the fixture's known state.
  2. Invoke the graph in autonomous mode (so nothing blocks on approval).
  3. Run the fixture's check command. Exit code 0 is a pass.
  4. Record: pass, turns, tool calls, tokens, seconds, terminal verdict.
  5. Append to a timestamped run file. Report the delta against the prior run.


--------------------------------------------------------------------------------
5. USER REQUIREMENTS
--------------------------------------------------------------------------------

Written in the owner's voice. These state what the system is for, not how it
works.

  ID      Tier       As the owner of the machine, I want...
  ------  ---------  ----------------------------------------------------------
  UR-01   Core       one place to talk to the agent, where it recalls earlier
                     sessions
  UR-02   Core       it to state a plan before acting on anything non-trivial
  UR-03   Core       it to work a multi-step task without me re-prompting each
                     step
  UR-04   Core       it to recover from its own errors, not halt at the first
                     failure
  UR-05   Core       to see what it is about to do, and be able to stop it
  UR-06   PC         it to read, create and modify files in a project I point at
  UR-07   PC         it to run shell commands and Python on my machine
  UR-08   PC         it to install dependencies a task needs
  UR-09   PC         it to perform git and GitHub operations
  UR-10   PC         it confined to a workspace so it cannot touch the rest of
                     my system
  UR-11   Web        it to search the web and read pages
  UR-12   Web        it to drive a real browser when a page needs interaction
  UR-13   Autonomy   to hand off a long task and check back later
  UR-14   Autonomy   to schedule recurring tasks
  UR-15   Autonomy   an interrupted task to resume where it stopped, not restart
  UR-16   Autonomy   to review anything risky it queued while I was away


--------------------------------------------------------------------------------
6. FUNCTIONAL REQUIREMENTS
--------------------------------------------------------------------------------

Priority: [M] Must  [S] Should  [C] Could  [W] Won't (this release)
Only [M] items are in scope for the first build. See section 9.

6.1 ORCHESTRATION (FR-1xx)

  FR-101  [M]  Decompose a goal into an ordered step list before executing.
  FR-102  [M]  Invoke tools via the model's native tool-calling interface.
               Never parse tool calls out of free text.
  FR-103  [M]  Loop act -> gate -> execute -> observe -> reflect until a
               terminal verdict is reached.
  FR-104  [M]  Terminate on exactly one of: done, stuck, budget exhausted,
               turn cap reached.
  FR-105  [M]  Advance the plan cursor on step completion and record step
               status.
  FR-106  [S]  Detect three consecutive identical tool invocations and
               terminate as stuck.
  FR-107  [S]  Re-plan after three consecutive tool failures rather than
               continuing blind.
  FR-108  [M]  Persist complete state to a checkpoint after every node
               transition.

6.2 TOOLS AND EXECUTION (FR-2xx)

  FR-201  [M]  Read files with offset/limit, write files, list directories,
               all rooted at the workspace.
  FR-202  [M]  Run shell commands with a timeout, capturing exit code, stdout
               and stderr separately.
  FR-203  [M]  Execute Python, returning stdout, tracebacks, and the value of
               the final expression.
  FR-204  [S]  Install packages into the sandbox environment on request.
  FR-205  [S]  Perform git status, diff, branch, add, commit, push.
  FR-206  [S]  Provide repository inspection (tree, grep, symbol search) that
               returns paths and line numbers, not file contents.
  FR-207  [M]  Register a new tool by decorating one function; derive its JSON
               schema automatically from the signature and docstring.
  FR-208  [M]  Return tool exceptions to the model as observations rather than
               propagating them out of the node.

6.3 POLICY AND APPROVAL (FR-3xx)

  FR-301  [M]  Classify every tool call as auto, confirm, or deny before any
               side effect occurs.
  FR-302  [M]  No call may write outside the workspace root without consent.
               AMENDED 2026-09-08: outside is `confirm`, not `deny` - see 8.2.
               Rejection confined the wrong half; run_shell was never confined
               at all, and the interactive agent never ran in a container.
  FR-303  [M]  Suspend execution and await human input on a confirm verdict in
               interactive mode.
  FR-304  [M]  Downgrade confirm to deny in autonomous mode and record the
               request for later review.
  FR-305  [M]  Evaluate policy with no side effects, so the node is safe to
               re-enter on resume.
  FR-306  [M]  Present the exact command or argument set in the approval
               prompt, unabbreviated and untruncated.
  FR-307  [C]  Allow the user to amend a tool call's arguments at the approval
               point, not merely accept or reject.

6.4 CONTEXT AND MEMORY (FR-4xx)

  FR-401  [M]  Truncate any tool result exceeding its per-tool cap before it
               enters context.
  FR-402  [M]  Write the untruncated result to an artifact file and return its
               path, plus instructions for inspecting it.
  FR-403  [M]  Compact conversation history when context use exceeds a
               configured fraction of budget.
  FR-404  [M]  Preserve the opening messages and the most recent turns verbatim
               through compaction.
  FR-405  [M]  Persist conversation history per thread and restore it across
               process restarts.
  FR-406  [S]  Maintain a durable user profile file loaded into every session.
  FR-407  [S]  Provide keyword retrieval over episodic memory.
  FR-408  [C]  Provide semantic retrieval, gated on a measured shortfall in
               keyword recall. Do not build this speculatively.

6.5 WEB (FR-5xx)

  FR-501  [M]  Perform web search returning ranked results with titles and URLs.
  FR-502  [M]  Fetch a URL and extract main content as clean text, discarding
               navigation and boilerplate.
  FR-503  [S]  Navigate, click, fill and submit forms in a real browser.
  FR-504  [S]  Serialise pages as an accessibility tree rather than raw DOM.
  FR-505  [S]  Honour robots.txt and apply per-domain rate limiting.

6.6 AUTONOMY (FR-6xx)

  FR-601  [M]  Accept a task into a queue and return a task identifier
               immediately.
  FR-602  [M]  Execute queued tasks in a worker process independent of any chat
               session.
  FR-603  [M]  Resume an interrupted task from its last checkpoint without
               repeating completed work.
  FR-604  [M]  Report task status as queued, running, awaiting-approval, done,
               or failed.
  FR-605  [S]  Trigger tasks on a cron schedule.
  FR-606  [S]  Allow a chat session to attach to and detach from a running task.
  FR-607  [S]  Cap the number of concurrent workers.

6.7 INTERFACE (FR-7xx)

  FR-701  [M]  Provide a CLI/TUI chat with streamed output.
  FR-702  [M]  Display the current plan and active step at all times.
  FR-703  [M]  List threads and tasks; resume either by identifier.
  FR-704  [W]  Accept voice input and produce speech output.

6.8 EVALUATION AND OBSERVABILITY (FR-8xx)

  FR-801  [M]  Run a fixture task set headlessly and record per-case pass/fail.
  FR-802  [M]  Reset the workspace to a known state before each case.
  FR-803  [M]  Record turns, tool calls, tokens and wall time per case.
  FR-804  [S]  Append results to a versioned run file and report the delta
               against the previous run.
  FR-805  [M]  Emit one structured log line per tool call.


--------------------------------------------------------------------------------
7. NON-FUNCTIONAL REQUIREMENTS
--------------------------------------------------------------------------------

A requirement without a number is not testable. Targets are the point.

  ID        Attribute        Requirement and target
  --------  ---------------  ------------------------------------------------
  NFR-101   Latency          First assistant token within 3 s at p50 on an
                             interactive turn
                             MEASURABLE 2026-08-31, UNMEASURED. The OpenAI-
                             compatible path now streams and every model trace
                             row carries first_token_s, which the harness
                             medians into first_token_p50. What that median IS
                             needs a live run and has not been taken, so the
                             3 s target remains unverified rather than met.
  NFR-102   Overhead         Framework cost per loop iteration <= 250 ms,
                             excluding model and tool time
                             MEASURED 2026-08-23: 6.53 ms p95, n=140. Pinned
                             by a test, not a one-off script.
  NFR-103   Latency          Checkpoint write <= 50 ms at p95
                             MEASURED 2026-08-23: 11.40 ms p95, n=180.
  NFR-104   Context          No single tool result exceeds 2,000 tokens after
                             shrinking
  NFR-201   Safety           Zero writes outside the workspace root across the
                             full EVAL SUITE, where the container is real. It
                             does not describe interactive use - see 8.2
                             AMENDED 2026-08-21: exactly TWO declared writable
                             roots - the workspace, and the agent home. State
                             must survive reset.sh, which wipes the workspace,
                             so it cannot live inside it. Everything else is
                             refused BY THE KERNEL, the project tree included,
                             and each refusal is recorded with its target path.
  NFR-202   Safety           No destructive command executes unapproved in
                             interactive mode; none at all in autonomous mode
  NFR-203   Security         Secrets never enter model context; env-var
                             indirection plus output redaction
  NFR-204   Isolation        All execution in a container with no host mounts
                             besides the workspace
  NFR-205   Security         Sandbox network egress restricted to a configured
                             domain allowlist. AMENDED 2026-09-02: the email
                             channel adds the configured IMAP and SMTP hosts
                             (imap.gmail.com and smtp.gmail.com by default),
                             stated here rather than left implicit - a row that
                             cannot name its egress is the AGENT_EGRESS defect
                             again.
  NFR-301   Recoverability   A process kill at any instant loses at most one
                             node of work
  NFR-302   Correctness      Resume produces no duplicated side effects
  NFR-303   Resilience       Transient provider errors retried with exponential
                             backoff, three attempts maximum
  NFR-304   Boundedness      Every task carries enforced caps on turns, tokens
                             and wall-clock time
  NFR-401   Cost             Token budget enforced as a hard stop, not advisory
  NFR-402   Cost             Median eval case completes within 60,000 tokens
  NFR-403   Cost             Compaction reduces context by at least 50% when it
                             fires
  NFR-501   Observability    Every tool call logs name, argument hash, verdict,
                             duration, byte counts, spill path
  NFR-502   Auditability     Any completed task is reconstructible from logs
                             alone
  NFR-601   Extensibility    Adding a tool requires editing exactly one file
  NFR-602   Testability      Every deterministic node is unit-testable without
                             an API key
  NFR-603   Maintainability  Prompts live in version-controlled files, not
                             string literals
  NFR-701   Portability      Runs on Fedora natively and on Windows under WSL2
                             AMENDED 2026-08-23, measured not asserted. READS
                             AS: the agent runs wherever a container runtime
                             does. DEMONSTRATED: the offline suite passes
                             390/390 on Fedora 41 / Python 3.13.9 and on
                             python:3.12-slim - two distributions, two Python
                             minors, same pins. NOT demonstrated: "natively",
                             since §11 makes a container mandatory anyway, and
                             WSL2, which remains untested.
  NFR-702   Portability      Model provider swappable behind a single adapter
  NFR-703   Independence     No hosted service required for state; local SQLite
  NFR-801   Usability        Approval or rejection resolvable in one keystroke
  NFR-802   Transparency     All agent artifacts under one inspectable directory


--------------------------------------------------------------------------------
8. TRACEABILITY AND KNOWN CONFLICTS
--------------------------------------------------------------------------------

8.1 REQUIREMENT -> COMPONENT

  FR-101, 104-107      reflect / plan nodes            graph.py
  FR-102, 103, 108     graph wiring + checkpointer     graph.py
  FR-201..206, 208     the tool functions              tools.py
  FR-207              schema derivation               registry.py (deferred,
                                                       see section 12)
  FR-301..307          gate node + classify()          graph.py, policy.py
  FR-401..404          shrink() at the execute node    context.py, graph.py
  FR-405               checkpointer                    graph.py
  FR-501..505          web tools                       web.py (deferred)
  FR-601..607          queue and worker                worker.py (deferred)
  FR-701..703          entrypoint                      cli.py
  FR-801..805          eval harness                    eval/harness.py

Verification for the FR-1xx and FR-2xx families is the eval harness. A
requirement is satisfied when the cases exercising it pass, not when the code
exists.

8.2 CONFLICTS — stated, not papered over

  UR-13 vs UR-05
    Unattended operation and per-action visibility are mutually exclusive.
    Resolution: FR-304. Autonomous mode denies rather than pauses and defers to
    a review queue. Less gets done unattended; nothing catastrophic happens.

  NFR-104 vs FR-208
    Truncating tool output loses information needed to diagnose an error.
    Resolution: FR-402 spill-and-path. Costs one extra tool call, buys a
    bounded context.

  FR-503/504 vs NFR-402                    UPDATED 2026-08-31
    Browser automation is the largest token consumer in the system and will
    breach the cost target alone. This is why it sits at [S] behind FR-501/502:
    search plus text extraction covers most real requests at a fraction of the
    spend.
    That last clause was an argument when written and is now a MEASUREMENT: the
    search split scores 9/9 with web_search and 0/9 with it removed, so the
    cheaper path demonstrably covers the cases that exist.
    Costed 2026-08-31: Chromium into a 593 MB image, which pip.conf's no-index
    forces to build time; and 4-6 tool schemas at ~580 chars each against 5,949
    chars of remaining MAX_SCHEMA_CHARS, charged on EVERY request of every run
    whether a browser is used or not.
    Resolution: FR-503 and FR-504 stay UNMET, deliberately. They are built only
    when a case exists that web_search plus fetch cannot solve, and then BOTH
    together - FR-503 alone is the raw-DOM version this entry calls unaffordable
    - with its own split and a control run.

    TRIGGER TESTED 2026-09-04, AND IT DID NOT FIRE. Three cases were written to
    need a rendering engine - a total computed client-side from a shipped array,
    a value revealed only by submitting a form, and entries appended on a click -
    and `fetch` alone scored 9/9 on them. The dashboard case took five turns:
    fetch, fetch, fetch raw, write. The agent read the REGIONS array out of the
    <script> tag and did the arithmetic itself.
      This is the third prediction of a capability floor that measured wrong in
      the same direction; the web split was expected to score 0 without a fetch
      tool and scored 18/18.
      A STATIC FIXTURE CANNOT PRODUCE THIS TRIGGER. Anything a browser computes
      from shipped JS, an agent with run_shell and Python computes from the same
      source. A real trigger needs state the client cannot derive - a server-side
      session, an authenticated round-trip, a canvas render - and NFR-205 points
      egress at a static http.server by design.
    The cases are KEPT as `split: browser`, a standing gate. They cost nothing
    until run, and if a model ever stops reading JS by hand this is what shows it.

  FR-204 vs NFR-201, NFR-205 and reproducibility     ADDED 2026-08-31
    "Install packages into the sandbox on request" cannot hold as written.
    Three properties of the measured environment forbid it, and each exists
    for a reason that outranks a [S]:
      - /etc/pip.conf sets no-index. The Containerfile states the purpose
        twice: every dependency must be baked at build time "or it does not
        exist", which is also the security property - a server or library
        cannot be introduced mid-run.
      - The root filesystem is --read-only (NFR-201), so site-packages is
        immutable while a run is in flight.
      - Egress is allowlisted to the model host (NFR-205). PyPI is not
        reachable, and a case declaring it would be declaring a different
        measurement.
    Implementing FR-204 at RUN time trades all three for a should-have.
    Resolution: FR-204 is satisfied at BUILD time. A dependency is added to
    the Containerfile and the image rebuilt, where the manifest records it and
    every scored row states the image it ran on. It is NOT available at run
    time, and that is a property being protected rather than a gap.

  FR-408 vs its own gate                   ADDED 2026-08-31
    FR-408 gates semantic retrieval on a measured shortfall in keyword recall.
    Measured 2026-08-31, offline, against the six recall/profile cases - the
    only ground truth in the project where a stored episode and the query that
    should retrieve it are worded differently.
      In the system AS IT RUNS there is no shortfall to gate on. Every eval
      home holds at most three episodes, the recall+profile split scores 6/6,
      and two of those six carry no AGENT.md at all, so they passed through
      episode search rather than the profile.
      A shortfall appears only in a corpus that has never existed: 36 distinct
      goals pooled from every home. There recall@3 is 2/6 - and the cause is
      the QUERY, not the index. _terms() ORs every word over two characters, so
      write, file, workspace and called outvote Quartzite. Keeping only the
      five rarest terms by corpus document frequency scores 5/6 at rank 1, on a
      plateau spanning k in {3,5,6,8} at every threshold. Porter stemming was
      tried and is flat-to-worse; it is not the fix.
    The one residual miss is a true vocabulary gap: two seconds against
    duration in centiseconds shares exactly one term, and that term is write.
    Resolution: FR-408 stays UNBUILT and is closed as NOT JUSTIFIED. Its gate
    asked for a shortfall in keyword retrieval; what was found is a shortfall
    in query construction, worth 3 of the 4 failures and free to fix. One case
    in six is not an embedding model, an image dependency and a share of
    MAX_SCHEMA_CHARS. It reopens if a real corpus reproduces the shortfall
    after the query fix, which is the condition to re-measure - not to assume.

    REOPENED 2026-09-04. The condition was tested and it fired. A 170-episode
    corpus with 40 ground-truth pairs - eval/fixtures/recall-corpus.jsonl, the
    corpus this entry said had never existed - scores keyword recall@3 of 0/40
    when the query and the episode are worded differently.
      Measured three ways so the number means one thing:
        verbatim, query == goal          40/40   retrieval itself works
        overlapping, one shared word     10/10   reproduces the old 5/6
        paraphrase, no shared words       0/40   the shortfall
      The old corpus was 36 coding-repair goals phrased alike, so a query
      shared rare words with its target and keyword sufficed. A personal
      agent's memory is not worded that way: "how do I like my morning drink"
      retrieves nothing for "I take my coffee black, no sugar".
    This reopens the GATE, not the design. A four-lane fusion measured 1/6
    and a dense lane alone 3/6; both stay rejected. A single lane fused
    with keyword must move recall@3 on THIS corpus or it reverts.

  FR-302 vs what this actually is            ADDED 2026-09-08
    "Reject any argument whose resolved path falls outside the workspace root"
    was written for an agent confined to a container. It confines the WRONG
    HALF, and the measurement says so:
      - run_shell is subprocess.Popen(shell=True, cwd=WORKSPACE) with NO
        confinement. `cd / && ls` from inside it lists the drive root -
        demonstrated, not reasoned. It is 95.5% of all tool calls across 624
        runs.
      - read_file, write_file, edit_file and search_files ARE confined, on
        resolved paths, and they are the tools that could act precisely.
    So the most-used tool has no boundary and the careful ones are penned into
    a scratch directory. That is not a safety posture, it is an accident of
    which code path grew the check.
    The container never applied here either. docker appears in eval/harness.py
    and in NO runtime module: `python -m agent` has always run natively on the
    host. §11's "execution is confined to a container" describes SCORED RUNS.
    Resolution: THE GATE IS THE BOUNDARY, which is what it was already.
      - a path outside the workspace no longer denies. It can never be `auto`.
      - read-only calls outside keep their risk verdict, because reading the
        user's own files is the point of a personal agent
      - anything that can WRITE outside is `confirm` in interactive mode and
        `deny` in autonomous mode, which is how `confirm` already degrades
    FR-302's INTENT - no silent writes outside the declared root - survives
    intact. Its mechanism moves from refusal to consent - approval, not an OS
    sandbox around tool execution.
    NFR-201 keeps its wording FOR THE SCORED SUITE, where the container is
    real and the claim is measured. It no longer describes interactive use,
    and pretending otherwise would be the defaulted-AGENT_EGRESS mistake
    again: a row claiming a condition nobody checked.

  NFR-802 vs FR-302 and NFR-201            ADDED 2026-08-23
    "All agent artifacts under ONE inspectable directory" cannot be satisfied
    without breaking one of the other two, and the collision is structural
    rather than a tidying job left undone.
      - FR-302 confines read_file to the workspace. A spilled artifact written
        outside it is unreadable BY THE MODEL, and shrink()'s entire design is
        that the model can re-read a spill it was given a path to.
      - NFR-201 (as amended) puts durable state outside the workspace precisely
        because reset.sh wipes the workspace between runs.
    Resolution: TWO declared inspectable roots, not one.
      .agent/artifacts/  inside the workspace, where the model can read them
      /state             outside it, where reset.sh cannot reach
    Both are declared, both are inspectable, and nothing is hidden - which is
    what NFR-802 was protecting. Moving artifacts to /state would satisfy the
    wording and break the mechanism, so the wording is what gives way.


--------------------------------------------------------------------------------
9. BUILD SEQUENCE — THE ONLY ORDER THAT MATTERS
--------------------------------------------------------------------------------

Do not implement sections 5-7 in order. Build in the order below.

TARGET LOOP FOR V1: given a repository with a failing test, make it pass.
Bounded, scored by exit code, needs no human judgement.

Each step has an EXIT CRITERION; do not begin the next until it holds. If a step
overruns its estimate by more than double, stop and reduce scope.

  STEP 1  FIXTURES AND A NULL AGENT.  Prove the rig works before there is
          anything to measure. `app.invoke()` returns its input unchanged, so a
          0/5 is unambiguously the agent rather than a broken reset.sh.
          EXIT: the harness prints `pass 0/5`; every setup exits 0 and every
          check exits non-zero; deleting a fixture's bug by hand flips that case
          to pass, which is what proves the check discriminates.
          TRAPS, still live whenever a case is added: a check that fails for the
          WRONG reason (a collection error rather than an assertion) will mask a
          working agent later, so read the output rather than the exit code; and
          reset.sh must remove untracked files, not just `git checkout`, because
          the agent creates them.

  STEP 2  THE THINNEST LOOP THAT RUNS.  act -> gate -> execute -> reflect,
          three tools, real tool-calling, tracing from the start.
          EXIT: at least one dev case passes end to end, and the trace shows the
          tool calls that did it.

  Required code changes from the skeleton
    (a) Rewire the entry edge: START -> "act", not START -> "plan".

    (b) TERMINATION GUARD. With no plan node, `plan` is [] and `cursor` is 0,
        so the reflect check `cursor + 1 >= len(plan)` evaluates `1 >= 0` and
        returns `done` on the FIRST text-only reply — including "Let me look at
        the test file first." Replace with:

            if s["messages"][-1]["role"] == "assistant":
                made_a_call = any(b["type"] == "tool_use"
                                  for m in s["messages"] if m["role"] == "assistant"
                                  for b in m["content"])
                return {"verdict": "done" if made_a_call else "continue"}

        Restore the cursor-based check only when the plan node is added.

    (c) FAILURE COUNTER. `failures` as Annotated[list, add] never resets —
        `plan` returning {"failures": []} appends an empty list, it does not
        clear. So `len(failures) >= 3` latches true for the rest of the run.
        Change to a plain int with overwrite semantics counting CONSECUTIVE
        failures: observe sets it to 0 when every result succeeded, otherwise
        increments it.

    (d) RISK MAP. `run_shell` is declared risk="destructive" but classify()
        special-cases the name and returns before consulting RISK, so the
        declaration is dead. Any future tool marked destructive would be
        silently auto-denied in autonomous mode and never run during eval.
        Either relabel run_shell as "write", or make RISK the single path.

    (e) TRACING (FR-805 scoped in early, not deferred). The harness writes
        eval/runs/<timestamp>/<case-id>.json containing the full final message
        list plus per-call metadata: tool name, verdict, duration, input bytes,
        output bytes, spilled path. Step 4 is unactionable without this.


  STEP 3  BASELINE.  A number with an error bar, not a number: 3 runs per case.
          EXIT: the baseline is recorded in eval/CHANGELOG.md with its
          conditions - model, split, date, blocked runs excluded.
          TRAP: a case that passes 1 of 3 seeds is NOT a passing case. Report
          pass counts, never a single run.

  STEP 4 — ONE FIX PER CYCLE                                      repeat
................................................................................

  Goal: change one thing, measure, keep or revert.

  Read the traces. Classify every failure into exactly one bucket, then fix
  only the largest bucket.

    SYMPTOM IN TRACE                          BUCKET          FIX
    ----------------------------------------  --------------  ---------------
    One tool_result dominates the transcript  context flood   FR-401/402
    Same argument hash 3+ times in a row       thrashing       FR-106
    Verdict `stuck` at max_turns, no repeats   no strategy     plan node
    Edits a file it never read                 blind editing   prompt (SOUL.md)
    Runs pytest once, never re-runs            no verify loop  prompt (SOUL.md)
    Verdict `done` with the test still failing termination bug  reflect logic
    Tool exception repeated verbatim           bad error text  tool return value

  Rules for this step
    - One change per cycle. Two changes and you cannot attribute the delta.
    - Prompt changes count as changes and must be measured like code changes.
    - Revert anything that does not move the number. Keeping a neutral change
      because it "seems right" is how the loop rots.
    - Log each cycle in eval/CHANGELOG.md: hypothesis, change, before, after,
      kept or reverted.

  EXIT CRITERION per cycle
    Baseline re-run at 3 seeds, delta recorded, decision logged.

.............................................................................

  STEP 5  HOLD-OUT AND STABILISE.  Confirm you improved the agent rather than
          the dev cases. Write 10 further cases and DO NOT look at their traces
          during step 4; run them only at milestones.
          EXIT: dev stable at >= 4/5 across 3 seeds, and the held-out set scored
          once with the result recorded WHATEVER it is. A held-out score well
          below dev is information, not failure - it says which fixes were
          general.

  AFTER V1 — LAYER ORDER
................................................................................

  Add one layer at a time. Each must be justified by a measured eval delta or
  by a requirement it unblocks; do not fix the order in advance.

  Likely order on repo-repair tasks, with the reasoning:
    1. compaction     a 12-turn bug fix does not need decomposition, it needs
                      to not drown in file contents
    2. plan node      earns its place only once tasks span multiple files
    3. resume/SIGKILL exercises the checkpointer already present from step 2
    4. memory         needs episodic recall to have something worth recalling
    5. web            largest token cost, smallest v1 benefit
    6. scheduler      pure infrastructure, no effect on the pass rate

  The stated order is a prediction, not a plan. If compaction moves nothing and
  the plan node moves 20 points, the prediction was wrong and the numbers win.


--------------------------------------------------------------------------------


--------------------------------------------------------------------------------
10. DEFINITION OF DONE (V1)
--------------------------------------------------------------------------------

  [ ] 5 dev cases + 10 held-out cases in eval/tasks.jsonl
  [ ] Dev set >= 4/5 across 3 seeds; held-out set scored at least once and the
      number recorded, whatever it is
  [ ] Zero writes outside the two declared roots across the full suite
      (NFR-201, as amended)
  [ ] Median case completes within the turn and token caps (NFR-402)
  [ ] Every deterministic node has unit tests that run without an API key
  [ ] A SIGKILL mid-task, followed by resume, completes without duplicated
      side effects (NFR-302)
  [x] Adding a new tool touches exactly one file (NFR-601)
      TRUE SINCE 2026-08-23, and it was NOT before: TOOLS carried the function
      and the schema while RISK was a literal in policy.py, so every built-in
      since v1 touched two files. A tool now declares `risk` beside its schema
      and policy.risk_of() reads it, falling back to tools.TOOLS so a tool added
      after import is classifiable without a sync() call.
  [ ] eval/runs/ contains at least three dated runs, each with per-case trace
      files, showing the improvement trajectory
  [ ] eval/CHANGELOG.md records every tuning cycle: hypothesis, change,
      before, after, kept or reverted


--------------------------------------------------------------------------------
11. NON-GOALS
--------------------------------------------------------------------------------

  - Multi-user support, auth, tenancy.
    AMENDED 2026-09-10: a SHARED-SECRET guard on the local viewer below is
    permitted, and is a prerequisite of it rather than a feature - a page
    serving traces, keys and workspace paths must not be readable by anything
    that can reach the port. One secret, no accounts, no sessions, no roles, no
    recovery flow. Multi-user, tenancy and OAuth remain non-goals; the reference
    implementation's auth.py is 9,409 lines because it is all three.
  - Multi-agent orchestration, sub-agent spawning, agent-to-agent protocols.
  - A web UI. CLI/TUI only.
    AMENDED 2026-09-10: a READ-ONLY local viewer is permitted, bound to
    127.0.0.1, serving only what is ALREADY recorded - eval/runs rows, traces,
    task and schedule state. It may not start, stop, approve or configure
    anything: every mutating path stays in the CLI and TUI, so the gate remains
    the only way a side effect happens. Auth, dashboards, model switching and
    plugin management are still non-goals. If the viewer
    ever needs a write path, this amendment is reopened rather than stretched.
  - Voice (FR-704 is explicitly [W]).
  - A general-purpose plugin marketplace or dynamic tool loading.
    AMENDED 2026-08-21: permitted ONLY through MCP servers meeting all three of
    (a) baked into the image at BUILD time, since pip.conf sets no-index and a
    server therefore cannot be introduced mid-run; (b) run INSIDE the sandbox as
    a subprocess, so a server cannot have host access the container does not;
    (c) every tool risk-classified BEFORE its schema reaches the model -
    policy.register() records an unclassified tool as `destructive`, never
    `read`. Bounded by config.MAX_SCHEMA_CHARS. A marketplace, and run-time
    installation of a server, remain non-goals.
  - Vector search before keyword recall has been measured and found wanting.
  - Fine-tuning or local model hosting for the orchestrator.
  - Windows-native support outside WSL2.


--------------------------------------------------------------------------------
12. REPOSITORY LAYOUT
--------------------------------------------------------------------------------

Create these files and no others. A file that exists before its layer is earned
will be filled with speculative code and will rot.

WHY EACH DEVIATION WAS ALLOWED IS IN eval/CHANGELOG.md, one section per phase.
This section records WHAT exists and the one-line reason; it is not the place to
re-argue a decision that has already been measured.

  personal-agent/
    pyproject.toml     dependencies, package metadata
    Containerfile      sandbox image
    NOTICE             third-party attribution, when any code is derived
    .gitignore         must include .agent/
    README.md          baseline and current numbers table
    agent/
      __init__.py
      config.py        SINGLE source of truth: workspace root, model, per-tool
                       caps, turn/token/second budgets, schema cap
      tools.py         tool functions, their docstrings (which ARE the schemas
                       since FR-207) and each tool's risk - one file per tool,
                       which is what makes NFR-601 true
      policy.py        classify() - the gate's entire logic, no side effects
      context.py       shrink(), and redact() inside it (NFR-203)
      graph.py         AgentState, node functions, graph wiring
      cli.py           entrypoint: python -m agent "goal"
      viewer.py        A STATED DEVIATION, created 2026-09-10. The read-only
                       viewer section 11 was amended to permit. One file because
                       it is one screen and five JSON routes over stores that
                       already exist - no template engine, no build step, no
                       framework: stdlib http.server, so it adds no dependency.
                       Its three conditions are tests, not prose: loopback,
                       a secret per request, and do_GET as the only method.
      tui.py           Textual front end. FR-701 reads "CLI/TUI chat" and §11
                       says "CLI/TUI only", so this is scope arriving late, not
                       new scope. Earned under CE-01 as the SECOND
                       implementation of the interface layer - provider.py's
                       standard. textual is imported inside the --tui branch so
                       the CLI, harness and suite run without it (NFR-602).
                       SINCE 2026-09-07 it is the entry SHIM only; the
                       interface is ui/ below and cli.py is unchanged.
      ui/              A STATED DEVIATION, created 2026-09-07. The TUI as a
                       package rather than one module. §12 listed tui.py alone,
                       which was right for a docked transcript and an input
                       box; the dwindle layout, three themes, three
                       transparency modes and eight panes are a different
                       quantity of code and the premise expired.

                       Earned under CE-01 three ways. tiling.py has two callers
                       and is pure enough to test with textual UNINSTALLED,
                       which is the point of extracting it - a subprocess test
                       blocks the import and runs it anyway. panes.py holds
                       eight implementations of one interface. theme.py holds
                       three of one Theme shape, and is the ONLY file in the
                       package allowed a colour: three themes only work if
                       every colour is one lookup, and transparency only works
                       if every foreground is explicitly set, because under
                       ansi_color=True an unset one degrades silently to
                       16-colour ANSI. screens.py is two Screen
                       implementations, on the same standard as the other two -
                       landing and workspace were separate files in the plan
                       and were merged because one of each is not enough.
                       noesis.tcss is the stylesheet, read inside the App and
                       never at import (CE-05); modals.py is the approval and
                       plan screens, moved with no behaviour change.
      setup.py         A STATED DEVIATION, created 2026-09-07. The first-run
                       credential wizard's LOGIC, kept out of cli.py because it
                       must run BEFORE config resolves: config.py reads every
                       tunable at import time, so a wizard that runs after
                       agent.cli is imported cannot change PROVIDER,
                       OPENAI_MODEL or OPENAI_BASE_URL no matter what it
                       writes. It runs from agent/__main__.py in the same
                       window .env is loaded in, and the entry point reloads
                       config after.

                       Separate from ui/setup.py, which holds the screen,
                       because needed() runs on EVERY `python -m agent` and
                       must cost nothing on a machine with no interface
                       installed. A key is probed against the live endpoint
                       before it is saved, and a key that fails is not saved -
                       a wizard that stores an unverified key is worse than no
                       wizard, because the failure surfaces twenty turns later
                       as an auth error.
      registry.py      merged tool view, schema budget, and the @tool decorator
                       (FR-207, added at the eighth hand-written schema, which
                       is where §13's own arithmetic stops objecting)
      mcp.py           MCP client: stdio, servers as subprocesses inside the
                       sandbox, tools risk-classified before exposure (§11
                       amendment)
      memory.py        episodes in SQLite FTS5 + the AGENT.md profile
      migrations.py    ADDED 2026-08-31, a DEVIATION recorded here to the same
                       standard as the others. Every store was created with
                       CREATE TABLE IF NOT EXISTS, which is right for a fresh
                       database and silently wrong for one already on disk: it
                       skips the statement, so later schema does not appear and
                       the failure surfaces as an odd SQL error at some random
                       call site. Measured, not feared - episodes_fts gained
                       tokenize=porter and every database written before that
                       still has the default tokenizer while the code assumes
                       porter. It is its own file because BOTH memory.py and
                       worker.py need it, which is CE-01 satisfied by two callers.
      secrets.py       ADDED 2026-08-31, a DEVIATION recorded here. NFR-203 says
                       secrets never enter model context; context.redact() only
                       replaced values found in os.environ, so a workspace .env,
                       an AWS key in source and a DSN password all reached the
                       model verbatim - measured, not feared. Its own file
                       because it is a curated PATTERN LIST rather than logic,
                       and mixing 40 issuer regexes into context.py would bury
                       shrink(). A pattern list only, deliberately not a
                       NAME=value redactor: applied to source that destroys type
                       annotations (`spent_tokens: int` -> `spent_tokens: ***`).
      skills.py        agentskills.io loading, and extraction at finish
      channel.py       A STATED DEVIATION, created 2026-09-02. Email intake and
                       delivery (IMAP in, SMTP out), so the agent is something
                       you can WRITE TO rather than something you invoke. Email
                       over a chat platform because it needs no bot
                       registration, no OAuth callback and no new dependency -
                       imaplib and smtplib are stdlib, and pip.conf sets
                       no-index so anything else would have to be baked into
                       the image.

                       It earns a module because it is a trust BOUNDARY, not a
                       feature: the moment a message can arrive from outside
                       the terminal, "who is asking" is a security question,
                       and policy.classify() assumes one trusted local user.
                       Default deny - config.EMAIL_ALLOW empty authorises
                       nobody. A From header is forged trivially, so the
                       allowlist is a FILTER and the security rests on the
                       mailbox being yours.

                       A message maps to a TASK and a task id IS a thread id,
                       so no second identity space exists - a runtime where
                       delivery, sessions and identity grow up separately ends
                       up carrying all three twice. Channels were never a
                       stated non-goal; 11 lists voice, marketplaces, vector
                       search, fine-tuning and Windows-native only.
      provider.py      earned by a SECOND implementation: Anthropic plus any
                       OpenAI-compatible endpoint (CE-01)
    prompts/
      SOUL.md          system prompt, version controlled (NFR-603)
      PLAN.md          the planning instruction, same rule
      CODING.md        A STATED DEVIATION. SOUL.md opened "You fix broken code"
                       and was sent on EVERY task, including the 33 of 59 eval
                       cases that are not coding at all - authoring 15%, recall
                       46%, skills 52%. SOUL.md is now the general brief and the
                       coding half lives here, appended only when the workspace
                       carries a project manifest, a tests/ directory or a .git.
                       A second file rather than a conditional inside SOUL.md
                       because NFR-603 says a prompt is a version-controlled
                       FILE: a posture selected by branching inside one file
                       cannot be diffed or reverted independently, and this one
                       is measured separately.
    tests/             test_policy, test_context, test_reflect are the three §12
                       allowed. NINE stated deviations, each to the same standard
                       - the component's failure is SILENT or is where consent
                       is decided: test_nodes (every deterministic node, §10),
                       test_cli and test_tui (the approval prompt), test_harness
                       (a wrong denominator does not crash, it misreports),
                       test_mcp (third-party code inside the trust boundary),
                       test_memory (a memory that never retrieves is
                       indistinguishable from a bad day), test_worker (a queue
                       that hands one task to two workers, or leaves a crashed
                       worker's row saying `running` forever, raises nothing -
                       it just does the work twice, or never), test_channel
                       (an authorisation boundary fails SILENTLY and in the
                       wrong direction - a channel that answers a stranger
                       looks exactly like one that works). Do not add a
                       tenth without meeting that bar.

                       THE TENTH AND ELEVENTH, added 2026-09-07, held to it.
                       test_ui_setup (with test_ui_theme and test_ui_screens
                       beside it) is a CREDENTIAL boundary: a key saved without
                       being verified surfaces twenty turns later as an auth
                       error, and a key echoed into a trace surfaces never.
                       test_ui_tiling is the layout algorithm, whose failures
                       are also silent - rects summing short leaves a dead
                       column that reads as a gap, and a split refusing the
                       3x12 minimum returns the tree unchanged with no error.
                       It is also the only file that must run with textual
                       UNINSTALLED, which test_tui.py cannot do.

                       NOTED, NOT FIXED: test_migrations and test_skills were
                       on disk before this and are not recorded above, so the
                       count in this paragraph has been behind reality since
                       they were written.
    eval/
      harness.py       runner and scorer
      measure_recall.py  A STATED DEVIATION, added 2026-09-04. recall@k over
                       fixtures/recall-corpus.jsonl. Separate from harness.py
                       because it needs no model, no network and no quota - a
                       retrieval measurement, and folding it into the scored
                       runner would make it cost a scored run.
      fixtures/recall-corpus.jsonl
                       170 episodes, 40 with a ground-truth query worded
                       DIFFERENTLY from the episode. The corpus 8.2 said had
                       never existed, and what FR-408's gate asks for.
      tasks.jsonl      fixture cases, flagged by split
      fixtures/<id>/   the broken repo for each case, committed. No nested .git
      runs/<ts>/       summary.jsonl + <case-id>.json full traces
      CHANGELOG.md     one section per cycle: hypothesis, change, before, after
    scripts/reset.sh   restore the workspace to a fixture's state
    scripts/install-tasks.ps1
                       A STATED DEVIATION, added 2026-09-04. Registers --channel
                       and --worker with Windows Task Scheduler. It exists so a
                       `--daemon` subcommand does NOT: the OS already restarts a
                       crashed process and starts one at logon, and 60 lines of
                       Python re-implementing that would be worse at it. Bounded
                       restarts (3), because `--channel` exits 2 on a rejected
                       password and an unbounded restart is how a permanently
                       broken thing looks healthy. Runs --doctor BEFORE
                       registering: a task that dies on start looks identical in
                       the UI to one that works.
    scripts/install.sh A STATED DEVIATION, added 2026-09-14. The one-line
                       install: clone (or install in place), pip -e into the
                       active venv else the user site, then say where `noesis`
                       landed and whether PATH reaches it. A checkout and not a
                       package because `noesis --update` is `git pull`; not
                       `curl | sh` because agent/policy.py refuses that shape.
                       Proven end to end in the suite (a fresh venv, offline,
                       the build backend baked into the image for it) and by
                       hand on Windows, where the first version installed into
                       the Store alias's Python and reported a stale command.
    .agent/            RUNTIME STATE, gitignored
      artifacts/       spilled tool output. INSIDE the workspace, because the
                       model must be able to read a spill without tripping
                       FR-302 - see the NFR-802 conflict in §8.2
      egress/          proxy config and the allowlist a run was measured under
      homes/<case>-<n>/ a BLANK agent home per scored case-run

  ~/.personal-agent/   THE AGENT HOME, mounted at /state (amended NFR-201).
    state.db           checkpoints
    memory.db          episodes
    AGENT.md           durable profile, written by the agent
                       Outside the project tree (mounted read-only) and outside
                       the workspace (which reset.sh wipes).

      worker.py        the task queue and the worker that drains it (FR-601,
                       FR-602, FR-604). §12's trigger "when FR-6xx enters scope"
                       fired. Tasks in SQLite at /state/tasks.db, status
                       constrained by a CHECK rather than by convention, and
                       every transition idempotent so a second attempt declines
                       instead of corrupting a row. FR-603 is free: the task id
                       IS the thread id, so a requeued task resumes from its
                       checkpoint rather than restarting.

DEFERRED FILES - create each only when its trigger fires

  agent/web.py         when FR-501/502 enter scope. THE TRIGGER FIRED 2026-08-29
                       and the file was still NOT created, which is stated here
                       rather than left to be noticed. FR-501 is one decorated
                       function; a module for it costs a Definition-of-Done item,
                       because NFR-601 asks that adding a tool require editing
                       exactly one file and a new module means editing
                       registry.toolset() as well. This section bounds what MAY
                       exist, not what must, so declining to create a permitted
                       file is compliant. If FR-503/504 are ever built here rather
                       than through MCP, the trigger stands and the arithmetic
                       flips - browser automation is not one function.

RESOLVED DEFECTS FROM THE PREVIOUS LAYOUT

  - state.py folded into graph.py: one caller, one implementation.
  - The workspace root was defined in two modules. FR-302 and NFR-201 both
    depend on those agreeing, so it lives in config.py alone.
  - AGENT.md moved out of prompts/: it is runtime-mutable state the agent
    writes, not a version-controlled prompt.
  - tests/ added (NFR-602). cli.py added (FR-701 is [M] and nothing was
    invocable outside the harness).

Task fixture schema (one JSON object per line in eval/tasks.jsonl):

  {"id": "fix-import", "goal": "Tests in tests/ fail on an import error. Fix it.",
   "setup": "scripts/reset.sh fix-import", "check": "cd /workspace && pytest -q",
   "split": "dev", "max_turns": 12, "budget": 200000}


--------------------------------------------------------------------------------
13. CODE ECONOMY RULES
--------------------------------------------------------------------------------

Binding on the implementing agent. Violations are defects, not style notes.

  CE-01  A separate module requires two callers OR two implementations.
         One of each is not enough.

  CE-02  A framework earns its place at break-even, not before. Count the
         lines it costs against the lines it saves at the CURRENT scale, not
         the anticipated one.

  CE-03  Every state field must be read somewhere. A field only ever written
         is dead weight that survives because it looks purposeful.

  CE-04  Two nodes that never branch apart are one node. See CE-07 for the
         one exception.

  CE-05  No module-level I/O and no module-level client construction. Reading
         a prompt file at import time breaks every test that imports the
         module.

  CE-06  Prefer default overwrite semantics for state. Custom reducers are
         justified only when two nodes write the same field in one superstep.

  CE-07  EXCEPTION TO CE-04: gate and execute must never merge. gate suspends
         on interrupt() and re-executes from its first line on resume. Merged,
         every already-executed tool fires a second time. This separation is
         load-bearing, not stylistic.

The v1 skeleton lost roughly 80 of 205 lines to these rules: a custom reducer
and its sentinel (CE-06), the @tool decorator at three tools (CE-02, since
earned at eight), observe as a separate node (CE-04), and four state fields
nothing read (CE-03). The full accounting is in eval/CHANGELOG.md.

State is a plain TypedDict - no reducers, no Annotated, no operator import.
Nodes return the FULL messages list, so compaction is an ordinary return rather
than a custom merge. shrink() returns a plain string, not a tuple: the spill
path belongs inside the returned text, where the model can act on it.

RECONCILIATION WITH SECTIONS 3 AND 4

  The control-flow diagram in section 3 and the turn lifecycle in section 4.1
  show EXECUTE and OBSERVE as distinct stages. They remain distinct STAGES;
  CE-04 makes them a single NODE in the implementation, because no edge ever
  separates them. Read section 3 as the logical flow and section 13 as the
  code shape. Where the two are compared, section 13 governs.

  Consequently section 3's deterministic node count is four in v1, not five:
  gate, execute (absorbing observe), reflect, finish.

================================================================================
END OF SPECIFICATION
================================================================================
