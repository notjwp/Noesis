<div align="center">
<pre>
███╗   ██╗ ██████╗ ███████╗███████╗██╗███████╗
████╗  ██║██╔═══██╗██╔════╝██╔════╝██║██╔════╝
██╔██╗ ██║██║   ██║█████╗  ███████╗██║███████╗
██║╚██╗██║██║   ██║██╔══╝  ╚════██║██║╚════██║
██║ ╚████║╚██████╔╝███████╗███████║██║███████║
╚═╝  ╚═══╝ ╚═════╝ ╚══════╝╚══════╝╚═╝╚══════╝
</pre>
</div>

A personal AI agent that runs on your machine. You give it a goal in plain English — fix these
tests, cut a release, find out what the quota is on this page, answer my email — and it plans,
uses tools, and works until it is done or tells you why it stopped.

It is single-user, terminal-first, and built to be trusted with your own files:

- **Every tool call is judged before it runs.** Reads happen freely. Writes outside your
  workspace, deletions, and anything that touches credentials pause for your approval — and are
  refused outright when nobody is there to answer.
- **It remembers.** What you told it, what worked, how you like things done. Across sessions,
  in a local SQLite file you can open.
- **It learns procedures.** A runbook it reads becomes a skill it loads next time the same kind
  of work comes up.
- **It can be left alone.** Queue tasks, schedule them with cron, have it read a mailbox and reply.
  It survives being killed mid-task and resumes from where it was.
- **Every number about it is measured.** Nothing in this README is claimed that has not been
  scored, and the scoring is reproducible.

## Install

Python 3.12+ and git. No Docker, no VM, nothing else. One line:

```bash
# macOS, Linux
git clone --filter=blob:none --sparse https://github.com/notjwp/Noesis.git && cd Noesis && python3 install.py
```

```powershell
# Windows (PowerShell; in cmd, join with && instead of ;)
git clone --filter=blob:none --sparse https://github.com/notjwp/Noesis.git; cd Noesis; python install.py
```

The script installs into your active virtualenv if you have one, otherwise your user site — never
the system Python — and ends by telling you where the `noesis` command landed and whether it is
on your PATH. If a virtualenv is active, it installs there even when the `python` you typed is a
different one.

The clone is partial and sparse: it brings down the agent and its prompts, about 1 MB, and leaves
the evaluation's vendored repositories on the server. The dependencies pip installs are the bulk
of it, about 130 MB into your virtualenv or user site - the same either way. `git clone` without
the flags works too and gets you the whole checkout, 29 MB, which is what a developer wants.

Not `curl | sh`, deliberately. The agent's own policy gate would refuse that shape, and an
installer it ships should be one it would run.

Updating later is `noesis --update` — a `git pull` in that checkout, which is why the install is a
checkout and not a package.

## First run

```bash
noesis
```

With no key configured, a **setup wizard** opens: pick a provider and model, paste the key. It
probes the endpoint *before* saving anything, so a wrong key is rejected there with the
provider's own message rather than twenty turns later. `ctrl+k` reopens it any time.

One thing the wizard does not ask for — the folder the agent may work in:

```bash
# in .env (the wizard creates the file; add this line)
AGENT_WORKSPACE=/path/to/the/folder/it/may/change
AGENT_HOME=/path/to/keep/its/memory      # optional; memory and skills live here, default ~/.noesis
```

The agent works inside `AGENT_WORKSPACE`. Reading outside it is allowed — it is your assistant
and those are your files — but writing outside it asks first.

`noesis --doctor` checks every precondition and prints one line per item, `ok` or `FAIL`. Run it
whenever something seems off; it changes nothing.

**Providers.** Any OpenAI-compatible endpoint works — NVIDIA NIM (free tier, the default), OpenAI,
OpenRouter, Groq, a local Ollama — or Anthropic directly. See `.env.example`. Before you commit to
a model, `python eval/harness.py --check-provider` sends one tool-calling request and tells you
whether that model can hold the loop at all; some open-weight models cannot.

## Using it

**The chat** — `noesis` with no arguments opens the full-screen interface. Type a goal; watch the
tool calls stream in, coloured by outcome; approve or deny when it pauses. Turn count, tokens spent
and the budget sit in the header. A finished thread stays open — send another message and it
continues with its history intact.

Inside the chat, slash commands:

| | |
|---|---|
| `/threads` | past conversations; pick one to resume |
| `/tasks` | the queue — what is waiting, running, done, or needs approval |
| `/schedules` | recurring goals and when they next fire |
| `/plan` | ask it to plan before acting (research is read-only until you accept) |
| `/trace` | the current thread's tool calls, verdicts and timings |
| `/artifact` | open a spilled tool output in full |
| `/review` | what needs attention, and queue a review of it |
| `/serve` | a read-only web view of threads, tasks and schedules on `127.0.0.1` |
| `/doctor` `/setup` `/help` `/exit` | |

**From the command line** — the same loop, scriptable:

```bash
noesis "Fix the failing tests in tests/"       # run one goal, approve as it goes
noesis --list                                  # past threads
noesis --resume <id>                           # pick one up

noesis --submit "Rotate the API keys"          # queue it and come back later
noesis --worker                                # drain the queue (leave this running)
noesis --tasks                                 # what is queued / running / done

noesis --schedule "0 9 * * 1" "Summarise last week's commits"
noesis --schedule "0 9 * * *" "@review"        # every morning: what needs attention?
noesis --schedules                             # what is scheduled
noesis --unschedule <id>

noesis --serve                                 # the web view, with a one-time token
noesis --doctor                                # every precondition, ok or FAIL
noesis --update                                # pull the latest
```

**Email.** Point it at a mailbox and it reads what arrives, queues each message as a task, and
replies with the answer. Default deny: it answers only senders you list, and its first pass
*adopts* what is already in the inbox rather than replying to a year of history.

```bash
# .env
AGENT_EMAIL_USER=you@example.com
AGENT_EMAIL_PASSWORD=an-app-password          # never your account password
AGENT_EMAIL_ALLOW=you@example.com,partner@example.com

noesis --channel-check    # probes IMAP and SMTP; sends nothing
noesis --channel          # start listening
```

On Windows, `powershell -File scripts/install-tasks.ps1` registers `--channel` and `--worker`
with Task Scheduler so they start at logon.

## What it can do

Ten built-in tools, each with a declared risk the gate enforces:

| tool | what | risk |
|---|---|---|
| `read_file` `search_files` | look around; big files come in windows of at least 100 lines | read |
| `write_file` `edit_file` | change files — `edit_file` is targeted, `write_file` replaces | write |
| `run_shell` `run_python` | run commands; dangerous ones escalate to *approve first* | write |
| `start_terminal` `read_terminal` | start something long-running — a server, a build, a watcher — and read its output as it goes | write / read |
| `ask_user` | ask you a question when the goal is genuinely ambiguous | read |
| `web_search` | look something up; keyless, no account needed | read |

Plus `fetch` — a page as readable text — from an MCP server the agent starts as a subprocess,
behind the same gate. Every tool output is capped before it reaches the model; anything larger is
saved to disk and the model is told how to read the rest.

**What it will not do without asking:** delete recursively, force-push, `sudo`, write to `/etc` or
your shell profile, read `.ssh` or `.env`, pipe the internet into a shell, or run a program through
a read-only tool's flag (`sort --compress-program`, `rg --pre`). Unattended, those are refused.
When it asks, `s` allows that *rule* for the rest of the session — `rm -rf build` once, and it stops
asking about recursive deletes, but still asks about a force-push.

**What it will not do even if you say yes:** delete `/` or your home directory, write to a block
device, format a filesystem, shut the machine down, or fork-bomb it. Saying "allow" trusts it with
your files; it does not trust it with the disk. Those are refused in every mode.

**Where it runs.** On your machine, natively — no Docker, no VM. The tools execute in
`AGENT_WORKSPACE` and the policy gate above is the boundary. That is a deliberate trade: a sandbox
would keep the agent from the very files a personal assistant exists to read. The container in the
next section is for *measuring* the agent, not for using it, and `noesis --doctor` says which one
you are in.

## How it stays honest

Every change to the agent goes through one cycle: change one thing, score it three times per
case, keep it if a number moved, revert it if not, and log the result. A month of that history is
in [`eval/CHANGELOG.md`](eval/CHANGELOG.md) — including the changes that were reverted, and why.

The scoring is a real test suite exiting 0 or not. Cases are broken projects with a known fix,
scored by their own tests; six of them are real open-source repositories vendored at the parent of
a genuine bug-fix commit. The agent is never the judge of its own success.

Current numbers, on `nvidia/nemotron-3-super-120b-a12b` at the free tier:

| split | what it measures | score |
|---|---|---|
| dev | bug fixes in small projects | **15/15** |
| held out | the same, on cases never tuned against | **30/30** |
| real repositories | six real projects, real bugs | **9/18** |
| tools | long-running processes, asking the user | **9/9** |
| search / web | finding things out | **9/9** · **18/18** |
| memory recall | remembering across sessions | **85.7%** |
| skills | loading the right procedure | **94.4%** |

The `real` split is the only one with headroom, and it has been flat since early September across
every loop change since — 10, 11 and 9 of 18 across three runs of the same code — which is recorded
as flat, not as progress. Two of its six cases have never passed on this model.

## Running the evaluation

You do not need this section to use the agent. It is how the numbers above were produced.

Scored runs happen inside a container — read-only code, two writable paths, no network except an
allowlisting proxy to the model host — so that fifteen runs are fifteen independent runs and not
fifteen that could see each other's files. The harness builds all of that itself. This is the one
place Docker is needed.

```bash
docker build -f Containerfile -t personal-agent .

# 1,186 offline tests - no API key, no network
docker run --rm --network none --read-only --tmpfs /tmp:exec \
  -v "$PWD:/app:ro" -v "$PWD/eval/workspace:/workspace" \
  -v "$PWD/.agent/homes/_t:/state" personal-agent python -m pytest -q

python eval/harness.py --split dev --runs 3 --pace 20      # a baseline
python eval/harness.py --split dev --runs 3 --continue     # resume one
python eval/harness.py --case fix-import --runs 3          # one case
```

A run that never reached the model is reported as **blocked** and excluded from the score, never
counted as a failure. A 30-run pass costs about 1.1M tokens and uses up the free tier for the day.

On Git Bash, prefix any `docker run` carrying a `-v` mount with `MSYS_NO_PATHCONV=1` and give
Windows-style paths (`$(pwd -W)`).

## Configuration

Everything is an environment variable, read from `.env` next to the code. The ones you might
change:

| variable | default | |
|---|---|---|
| `AGENT_WORKSPACE` | `/workspace` | the folder it may change — **set this** |
| `AGENT_HOME` | `~/.noesis` | memory, skills, checkpoints |
| `AGENT_PROVIDER` | `nvidia` | or `anthropic` |
| `OPENAI_BASE_URL` `OPENAI_MODEL` | NVIDIA NIM | any OpenAI-compatible endpoint |
| `AGENT_MEMORY` | `on` | remember across sessions |
| `AGENT_SKILLS` | `on` | load procedures it has learned |
| `AGENT_WEB` `AGENT_MCP` | `on` | web search; the `fetch` server |
| `AGENT_PLAN` | `off` | plan before acting |
| `AGENT_VERIFY_ON_STOP` | `off` | nudge it to run the tests after an edit |
| `AGENT_MAX_WORKERS` | `1` | tasks that may run at once |
| `AGENT_TUI_THEME` | `noesis-mono` | the chat's colours |

Turn caps, token budgets and timeouts are in `agent/config.py`, each with the measurement that
set it.

## Under the hood

Four nodes in a loop — `act → gate → execute → reflect` — and only `act` talks to a model.
Everything else is ordinary code with no API key needed to test it. The design and its reasons
are in [`CONTEXT.md`](CONTEXT.md), the phases in [`ROADMAP.md`](ROADMAP.md), the high- and
low-level design in [`docs/`](docs/).
