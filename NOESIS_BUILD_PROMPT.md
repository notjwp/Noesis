# NOESIS — build the Textual TUI

Build a new terminal interface for this agent, named **NOESIS**. The screenshots I attached are
visual reference for *feel* only — palette, density, chrome, restraint. Do not copy any layout
from them literally; the spec below is the authority.

Work in this order, and do not start with the pretty parts:

1. `agent/ui/tiling.py` and its tests — pure, and the only hard algorithm here.
2. The first-run setup wizard (section 6) — it is the only part that can leave a user stuck.
3. Themes and the stylesheet.
4. Landing, workspace, panes.

---

## 0. Rules of engagement

- **`CONTEXT.md` is binding.** §12 is a file allowlist, §13 (Code Economy) is binding, and where
  §3/§4 disagree with §13, §13 governs. This build creates files §12 does not list — write the
  justification into §12 itself, in §12's existing voice. Draft text is in section 16.
- **Follow `CLAUDE.md`.** Comments state *why*, never *what*, three lines maximum. Surgical
  changes: every changed line outside `agent/ui/` must trace to this request.
- **Do not touch** `graph.py`, `policy.py`, `tools.py`, `context.py`, `cli.py`, `provider.py`,
  `worker.py`, or anything under `eval/`. This is an interface change; nothing below the interface
  moves. The only edits outside `agent/ui/` are: four `_env` lines in `config.py` (sections 4.4,
  6.6, 10.5), the wizard hook in `agent/__main__.py` (section 6.2), and `agent/tui.py` becoming a
  shim.
- **Add no dependencies.** `textual==8.0.1` and `rich==14.3.3` are already pinned. I verified the
  Textual behaviour below against 8.0.1 specifically — check any *other* API against the installed
  version rather than assuming it from a different release.
- **`textual` stays imported inside the `--tui` branch** (NFR-602). The CLI, the harness and the
  test suite must still run with `textual` uninstalled. `agent/ui/` may import it freely;
  `agent/__main__.py` must not import `agent.ui` at module scope.
- **CE-05: no module-level I/O and no module-level client construction.** Read the stylesheet
  inside the App, never at import time.
- **No hardcoded hex outside `agent/ui/theme.py`.** This is not a style preference — it is load
  bearing twice over: three themes only work if every color is one lookup, and transparency
  (section 10) only works if every foreground is explicitly set.

---

## 1. What you are building

A launch screen showing a **NOESIS** logotype with a selectable list of slash commands beneath it.
Choosing a command — or typing a goal — transitions to a **workspace**: a chat-first screen where
the conversation is one pane among several, and panes tile in a **dwindle** layout, so any pane can
be split horizontally or vertically, superfile / Hyprland style. Three switchable color themes.
Three transparency modes, so panes can float over the user's wallpaper. On first run, if no API key
is configured, a setup wizard asks for a model and a key and **verifies it against the live
endpoint** before saving. Motion is micro only: color and opacity, never position.

---

## 2. Repo constraints that are binding on the design

- **FR-702: "Display the current plan and active step at all times."** The plan pane is optional,
  so the *active step* lives permanently in the status bar. It is never hidden, in any layout,
  including zoom. If the status bar can ever show no step, the requirement is unmet.
- **FR-703: a task's identity IS its thread id.** The panes must preserve this.
- **The approval gate.** Escape means **deny**, Enter means **refuse**. Never make Enter the
  approving key. Tests assert exactly this; they must keep passing.
- **Never claim a condition nobody checked.** `CLAUDE.md` records this as a paid-for lesson:
  `AGENT_EGRESS` defaulted to `"restricted"` and nothing ever set it, so every trace row ever
  written claimed restricted egress. The landing status line therefore prints an egress state
  **only when `AGENT_EGRESS` is explicitly set in the environment**, and omits the field entirely
  otherwise. Do not print a default and do not print `unknown` as if it were a reading.

---

## 3. File layout

```
agent/ui/
  __init__.py     exports NoesisApp and nothing else
  tiling.py       the dwindle layout tree. ZERO textual imports. Pure data.
  theme.py        three Theme objects + the semantic variable map
  noesis.tcss     every style rule, one stylesheet
  landing.py      LandingScreen — logotype + command list
  workspace.py    WorkspaceScreen — tiling host, composer, status bar, graph worker
  panes.py        Pane base class + the eight panes
  modals.py       ApprovalScreen, PlanScreen (ported, restyled)
  setup.py        first-run wizard: model picker, key entry, live probe
```

`agent/tui.py` becomes a thin shim that keeps the module path alive and re-exports the app under
its old name, so `python -m agent --tui` and any existing import keep working. Nothing else.

**CE-01 is satisfied three ways, and say so in the justification:** `tiling.py` has two callers
and a pure API; `panes.py` holds eight implementations of one interface; `theme.py` holds three
implementations of one Theme shape.

---

## 4. Themes

Three, switchable live with no restart. `textual.theme.Theme` in 8.0.1 has exactly these fields —
`name, primary, secondary, warning, error, success, accent, foreground, background, surface,
panel, boost, dark, luminosity_spread, text_alpha, variables`. Anything else (`muted`, `border`,
`info`) goes in `variables` and is referenced in CSS as `$muted` etc. Register all three with
`App.register_theme()` and switch by assigning `App.theme`.

### 4.1 `noesis-mono` — the default

```
background   #0A0B0D
surface      #121417      pane interiors
panel        #1A1D21      status bar, composer
border       #22262B      unfocused pane border
muted        #6B7280      secondary text, timestamps, inactive commands
foreground   #E6E8EB      body text
accent       #A3E635      focus, selection, logotype, running
error        #FF5F56      SEE BELOW
```

**Deliberate exception, and put it in a comment:** `error` is a second saturated color in a scheme
that claims one. It exists because a denied or destructive call rendering in grey is a safety
defect, not a style choice. Used *only* for the approval modal border, `deny` verdicts, FAIL lines
in doctor, and the "no API key" state. Nowhere else.

### 4.2 `catppuccin-mocha`

```
background   #1E1E2E  base       foreground   #CDD6F4  text
surface      #313244  surface0   muted        #6C7086  overlay0
panel        #181825  mantle     border       #45475A  surface1
accent       #CBA6F7  mauve      success      #A6E3A1  green
error        #F38BA8  red        warning      #FAB387  peach
info         #89B4FA  blue — paths, ids, filenames
```

### 4.3 `tokyo-night` — narrowed to three hues

Only cyan, green and red carry meaning. Everything else is greyscale-blue.

```
background   #16161E             foreground   #C0CAF5
surface      #1A1B26             muted        #565F89
panel        #1A1B26             border       #292E42
accent       #7DCFFF  cyan       success      #9ECE6A  green
error        #F7768E  red        warning      #9ECE6A  green, dimmed — no fourth hue
info         #7DCFFF  cyan — paths reuse the accent rather than adding a hue
```

### 4.4 Selection

- `config.py` gains `TUI_THEME = _env("AGENT_TUI_THEME", "noesis-mono")`.
- `ctrl+t` cycles mono → mocha → tokyo → mono for the session only. No preference file.
- An unrecognised value falls back to `noesis-mono` without raising.

---

## 5. The landing screen

### 5.1 Layout

Centered, in a column no wider than 64 cells:

```

   ███╗   ██╗ ██████╗ ███████╗███████╗██╗███████╗
   ████╗  ██║██╔═══██╗██╔════╝██╔════╝██║██╔════╝
   ██╔██╗ ██║██║   ██║█████╗  ███████╗██║███████╗
   ██║╚██╗██║██║   ██║██╔══╝  ╚════██║██║╚════██║
   ██║ ╚████║╚██████╔╝███████╗███████║██║███████║
   ╚═╝  ╚═══╝ ╚═════╝ ╚══════╝╚══════╝╚═╝╚══════╝

     nvidia · nemotron-3-super-120b · 09:14

   ▸ /chat        start a session
     /threads     resume past work                  3
     /tasks       the queue                         2 ●
     /schedules   cron schedules, soonest first     1
     /doctor      every precondition, ok or FAIL
     /setup       change model or API key
     /help        this list

   ›
     type to begin  ·  ↑↓ select  ·  ⏎ run  ·  ^t theme

```

### 5.2 The logotype

ANSI Shadow block face, **tightly kerned — zero added spacing between letters.** Store as six
module constants in `landing.py`, exactly these strings, byte for byte:

```
███╗   ██╗ ██████╗ ███████╗███████╗██╗███████╗
████╗  ██║██╔═══██╗██╔════╝██╔════╝██║██╔════╝
██╔██╗ ██║██║   ██║█████╗  ███████╗██║███████╗
██║╚██╗██║██║   ██║██╔══╝  ╚════██║██║╚════██║
██║ ╚████║╚██████╔╝███████╗███████║██║███████║
╚═╝  ╚═══╝ ╚═════╝ ╚══════╝╚══════╝╚═╝╚══════╝
```

- **46 cells wide, 6 rows.** Write a test asserting all six rows are exactly 46 cells — a ragged
  row is the failure mode of every ASCII logotype and it is invisible in a diff.
- **Kerning is the whole point of this revision.** The glyphs above are already butted together
  with no separator. Do not insert spaces between letters, do not "pad for readability", and do not
  regenerate the art from a figlet call at runtime — a figlet default adds inter-letter spacing and
  that is exactly the look being rejected. These six strings are the artwork; paste them.
- The gaps that remain are **intrinsic to the letterforms**, not spacing: the diagonal of `N`
  (`███╗   ██╗`), and the notched corners of `O` (`╚═════╝` inset by one). Do not try to close
  those — closing them means editing the glyphs, and hand-edited block letters read as broken.
- Color: **uniform `$accent`**. At six rows a vertical fade is tempting; it is not the default
  here, because the accent means "focus, and nothing else" everywhere else in the app. If you
  want depth, the sanctioned variant is rows 5–6 blended 40% toward `$background` — nothing more.
- **Below 54 columns**, replace with `N O E S I S` in bold `$accent`, letterspaced. Below 20 rows,
  drop the hint line first, then the status line. The landing must never scroll and never clip the
  command list.
- This face mixes `█` blocks with `╔═╗` shadow glyphs. That is deliberate and it is the requested
  look, but it is also why kerning matters: with inter-letter spacing the shadow layer stops
  reading as depth and starts reading as noise. Tight is what makes it work.

### 5.3 The status line

`provider · model · time`, all `$muted`. Rules:

- The model is the **actually resolved** one — `config.OPENAI_MODEL` on the OpenAI-compatible path,
  `config.MODEL` on the Anthropic path. Never a hardcoded string.
- If no API key is present for the resolved provider, replace the whole line with
  `no API key · press ^k or type /setup` in `$error`.
- **Egress:** print `restricted egress` / `open egress` **only if `AGENT_EGRESS` is set in the
  environment.** If it is unset, the field is omitted. See section 2.

### 5.4 The command list

- Rows are `command` (`$accent` when selected, `$foreground` otherwise), `description` (`$muted`),
  and a right-aligned live count.
- Selected row is marked `▸` in `$accent` with its background lifted to `$surface`.
- `↑`/`↓` move, `⏎` runs, typing filters live and the selection snaps to the first match.
- Counts are live data and must not be read on the render path (section 10). Render `·` in
  `$muted` until they arrive.
- The `●` badge next to `/tasks` is `$error` when anything awaits approval, `$accent` when
  something is running, absent otherwise.

### 5.5 Entering the workspace

- A **lone slash word** runs a command. Port this rule verbatim from `agent/tui.py:648` — a
  sentence that merely starts with a slash is a message, and `/taks` is a typo that gets the list
  rather than a guess. Three tests cover it; keep them.
- Any other text is a **goal**: mount the workspace and start a session with it.
- `/chat` mounts an empty workspace. Every other command mounts the workspace with the chat pane
  *plus* that command's pane already split in.

---

## 6. First-run setup — the API key wizard

This must actually work. A wizard that stores an unverified key is worse than no wizard, because
the failure surfaces twenty turns later as an auth error.

### 6.1 When it runs

On `python -m agent` (any subcommand), run the wizard only when **all** of these hold:

- no API key is present for the resolved provider, and
- `sys.stdin.isatty()` and `sys.stdout.isatty()`, and
- `AGENT_NO_SETUP` is unset.

The TTY check is what keeps `--worker`, `--channel`, the Windows scheduled tasks and the eval
harness from ever blocking on a prompt. Without a TTY, fall through to the existing behaviour —
`config.openai_api_key()` already raises a clear, actionable `RuntimeError`, and that stays the
non-interactive story.

### 6.2 Where it runs, and why there is only one correct place

`config.py` resolves every tunable **at import time**. A wizard that runs after `agent.config` is
imported cannot change `PROVIDER`, `OPENAI_MODEL` or `OPENAI_BASE_URL`. `agent/__main__.py`
already loads `.env` before importing `agent.cli` for exactly this reason. The hook goes in the
same window:

```python
if __name__ == "__main__":
    _load_env(Path(__file__).resolve().parent.parent / ".env")

    from agent import setup                    # imports no other agent module
    if setup.needed() and setup.interactive():
        setup.run()                            # writes .env AND os.environ

    from agent.cli import main
    raise SystemExit(main())
```

**After the wizard writes anything, reload `agent.config`** (`importlib.reload`) before importing
`agent.cli`. Nothing else from `agent` has been imported at that point, so the reload is safe.
Note that `config.openai_api_key()` already reads `os.environ` at call time and needs no reload —
but `PROVIDER`, `OPENAI_MODEL` and `OPENAI_BASE_URL` are import-time constants and do. Write a
test for the model case specifically, not just the key case.

`agent/ui/setup.py` holds the Textual screen. The tiny `needed()` / `interactive()` / `run()`
entry points must not import `textual` at module scope — `run()` imports it inside the function,
so a headless machine reaching `needed() == False` never pays for it.

### 6.3 The model list

Offer exactly what this repo actually supports. `provider.call_model` dispatches on
`AGENT_PROVIDER` ∈ `{anthropic, nvidia, openai}`.

```
NVIDIA NIM                                    AGENT_PROVIDER=nvidia
  nvidia/nemotron-3-super-120b-a12b     default — every baseline in this repo
  nvidia/nemotron-3-ultra-550b-a55b     4.6× larger; measured 10/18, same as super

Anthropic                                     AGENT_PROVIDER=anthropic
  claude-opus-5
  claude-sonnet-5
  claude-haiku-4-5-20251001

OpenAI-compatible                             AGENT_PROVIDER=openai
  custom — prompt for base URL and model id
  (OpenRouter, Groq, OpenAI, a local Ollama)
```

Show a `$warning` note under any choice other than the default:
*"changing the model invalidates every baseline in this repo — it is a different measurement."*
`config.py` says exactly this about `OPENAI_MODEL`; the wizard should not let it pass silently.

### 6.4 Key entry

- `Input(password=True)`. The key is never echoed.
- After saving, display it masked with the last four characters only: `nvapi-••••••••7f2c`.
- The key is **never** written to a trace, a log line, an artifact spill, or the transcript.
  `context.redact()` exists for NFR-203 — make sure the wizard's own screen cannot leak it.
- The key is transmitted to exactly one place: the provider endpoint being probed. Nowhere else.

### 6.5 Verification — the part that makes it "working"

Probe the live endpoint before saving, using the shape already proven in `eval/harness.py:365`:

```python
call_model(
    [{"role": "user", "content": [{"type": "text", "text": "say ok"}]}],
    "Answer with one word.",
    [],
)
```

Costs about 40 tokens. Branch on the existing taxonomy in `provider.py` — do not invent a new one:

| outcome | meaning | wizard does |
|---|---|---|
| returns | key and model both good | save, show masked key, continue |
| `ProviderMisconfigured` | key rejected, or model forbidden | **do not save**; show the message, return to key entry |
| `ProviderUnavailable` | rate limit, timeout, 5xx — key may be fine | offer *retry* / *save anyway* / *change model*, and say which it is |
| anything else | our bug | show the traceback tail; do not swallow it |

Run the probe in a thread worker with a braille spinner and a **20-second** cap — do not inherit
`config.REQUEST_TIMEOUT` (120s), which is sized for a working turn, not for a person waiting at a
prompt.

`channel.diagnose()` currently prints `ok model <name>` **without probing anything** — it only
checks that a key is *present*. Do not change it in this task, but do not copy its approach
either; `CLAUDE.md`'s own lesson applies: *"Running" is not "usable" — a preflight must probe the
operation the dependent code performs.*

### 6.6 Persistence

Write to `.env` at the repo root. It is already gitignored (`.gitignore:3`), with `.env.example`
as the committed template.

- **Replace an existing line for the same variable; never append a duplicate.** `_load_env` takes
  the first occurrence and a real environment variable always wins, so a duplicate produces a
  file whose behaviour does not match its last line.
- Open with `newline=""` and write `\n` explicitly. `CLAUDE.md`: text files crossing an OS
  boundary need their line endings pinned, and this file is read by Docker `--env-file`, Git Bash
  and Windows alike.
- Do not quote the value. Docker's `--env-file` does not strip quotes, and `config._clean()`
  exists only because that already bit this project once.
- Never write anything to `.env.example`.
- Set the file mode to `0o600` where the platform supports it; skip silently on Windows rather
  than failing the wizard.
- Also set `os.environ` for the same keys, so the current process works without a restart.

Variables written, by choice:

```
nvidia      AGENT_PROVIDER=nvidia      OPENAI_MODEL=<id>       AGENT_API_KEY=<key>
anthropic   AGENT_PROVIDER=anthropic   AGENT_MODEL=<id>        ANTHROPIC_API_KEY=<key>
openai      AGENT_PROVIDER=openai      OPENAI_MODEL=<id>       AGENT_API_KEY=<key>
                                       OPENAI_BASE_URL=<url>
```

`config.MODEL` is currently the hardcoded literal `"claude-opus-5"`, so the Anthropic model cannot
be selected at all today. Change it to `MODEL = _env("AGENT_MODEL", "claude-opus-5")`. This is a
tunable and `config.py` is the single source of truth for tunables, so it belongs there — but it
is a behaviour change to the Anthropic path, so call it out in your summary rather than slipping
it in.

Note that `_env` appends to `config.ENV_VARS`, which `eval/harness.py` reads to decide what to
forward into a scored container. The four new names will be forwarded. They are inert in a scored
run, but say so in your summary rather than letting someone discover it.

### 6.7 Re-running it

- `/setup` from the landing, and `ctrl+k` from anywhere, reopen the wizard.
- Re-running with a key already present pre-selects the current provider and model and shows the
  existing key masked. It must be possible to change the model **without** re-typing the key.

---

## 7. The workspace

### 7.1 Structure

```
┌──────────────────────────────────────────────────────────┐
│  the tiled region — everything but the two rows below    │
│  panes here, 1-cell gaps between them                    │
├──────────────────────────────────────────────────────────┤
│ ›  composer — always focused for typing                  │
│ ⟨2/4⟩ ▸ edit_file agent/x.py · 120b · 8/30 · 42.1k · auto │
└──────────────────────────────────────────────────────────┘
```

The composer and status bar are docked and never part of the tiling. The status bar is one row and
is never hidden — FR-702 lives there.

### 7.2 `tiling.py` — write this first, and test it first

Pure Python. No `textual` import. No I/O.

```python
Leaf(pane_id: str)
Split(orientation: "h" | "v", ratio: float, a: Node, b: Node)
```

`orientation` is the direction of the *divider*: `"v"` puts `a` and `b` side by side, `"h"` stacks
them.

- `split(tree, target_id, new_id, orientation=None) -> Node` — replaces the target leaf with a
  Split. When `orientation` is `None`, choose from the target's current rectangle: wider than 2× its
  height splits vertically, else horizontally. That aspect rule *is* dwindle — do not approximate
  it with an alternating counter.
- `close(tree, target_id) -> Node | None` — replaces the parent Split with the surviving sibling.
  Closing the last leaf returns `None`.
- `rects(tree, width, height, gap=1) -> dict[str, Rect]` — integer cell rectangles; gaps between
  siblings only, never on the outer edge. Rounding error goes to the second child so the total
  sums exactly. Minimum 3 rows × 12 columns per pane; if a split cannot honour that, `split()`
  refuses and returns the tree unchanged.
- `focus(tree, from_id, direction, width, height) -> str` — **geometric, not tree-structural.**
  Take the source rect, scan candidates lying in that direction, keep those overlapping on the
  perpendicular axis, return the nearest. No candidate returns `from_id`. This is why `rects` is
  part of the pure module.
- `resize(tree, target_id, direction, delta) -> Node` — walks to the nearest ancestor Split of
  matching orientation and nudges its ratio, clamped to `[0.15, 0.85]`.
- **`zoom` is not in the tree.** It is a single `zoomed: str | None` on the screen; when set, that
  pane renders alone at full size and the tree is untouched. Putting it in the tree forces every
  other operation to know about it.

### 7.3 Rendering the tree — the part that decides latency

**Pane widgets are disposable views over state the screen owns.** Textual has no reparent API, so
a layout change rebuilds the container tree. That is only correct if rebuilding is cheap:

- Every pane's data lives on the screen, never only inside the widget. A rebuilt `ChatPane`
  repopulates its `RichLog` from `screen.transcript`, capped at 2000 lines.
- Rebuild happens **only** on split, close, zoom and resize — all user-initiated, all rare.
  A rebuild on a streamed token is a bug.
- Map the tree to nested `Horizontal` / `Vertical` containers with fractional sizes from each
  Split's ratio. Do not position panes absolutely.

### 7.4 Focus

The composer holds keyboard focus by default so typing always reaches the agent. Pane navigation
therefore needs a modifier, and pane bindings are declared `priority=True` at screen level so the
`Input` cannot swallow them.

---

## 8. The panes

One `Pane` base class providing chrome: rounded border, title in the top border, optional
right-aligned count badge, focus styling, and a `refresh_from(state)` hook.

| id | title | contents | Enter |
|---|---|---|---|
| `chat` | `chat · <thread>` | transcript: your turns, tool lines, streamed replies. Cannot be closed. | — |
| `plan` | `plan` | every step; active `▸` in `$accent`, done `✓` in `$success` | — |
| `trace` | `trace` | one line per tool call: time, tool, verdict, duration | open its artifact |
| `threads` | `threads` | past threads, newest first | resume in `chat` |
| `tasks` | `tasks` | queued / running / awaiting / done / failed | open in `chat` |
| `schedules` | `schedules` | cron schedules, soonest first | — (`d` removes, with confirm) |
| `doctor` | `doctor` | one line per precondition, `ok` or `FAIL` | — |
| `artifact` | `artifact · <name>` | a spilled `.agent/artifacts/` file, syntax-highlit | — |

Keep the existing tool-line vocabulary:

```
◇ read_file   agent/context.py                    42 lines
  ↳ .agent/artifacts/a3f1.txt
✓ edit_file   agent/context.py                    +3 −1
✕ run_shell   rm -rf /workspace                   denied
⠹ run_shell   pytest -q
```

`◇` ran, `✓` mutated, `✕` denied or errored, `⠹` running. The glyph carries the verdict and the
color reinforces it — never rely on color alone.

---

## 9. Chrome and typography

A terminal gives you bold, dim, reverse and color. That is the whole type system, so hierarchy
comes from **space and alignment**, not weight.

- **Pane border:** `round`, `$border` unfocused, `$accent` focused. 1-cell gap between panes.
- **Titles:** lowercase, in the top border, `$muted` unfocused / `$accent` focused, never bold.
  Count badge right-aligned in the same border.
- **Rows:** one cell of horizontal padding inside every pane. No vertical padding — vertical space
  is the scarce resource.
- **Selection:** background `$surface` plus a `▸` marker. Never `reverse` — it inverts unreadably
  in half the palettes, and it is illegible in the transparent modes.
- **Alignment is the grid.** Tool name at a fixed column, argument at a fixed column, result
  right-aligned to the pane edge. Truncate the *middle* of long paths (`agent/…/context.py`) —
  the filename is the informative half.
- **Timestamps and durations** are always `$muted`.
- **Empty panes** get one dim centered line saying what would appear there, never a blank box.

---

## 10. Transparency

### 10.1 What is actually true, measured against textual 8.0.1

A TUI cannot make a window transparent — only the emulator can. What the app must do is emit *no*
background so the emulator's transparency shows through. I tested each approach:

| approach | result |
|---|---|
| `background: transparent` alone | still paints solid `#121212` — **does not work** |
| overriding `App.render()` | still paints `#121212` — **does not work** |
| `App(ansi_color=True)` + `background: transparent` | emits `bg=default` — **works** |

And the part that makes it usable: under `ansi_color=True`, **explicitly set hex colors stay
truecolor.** Measured — foreground `#a3e635` survived, and an explicitly painted pane background
`#121417` survived (66 cells) while the surrounding gutters emitted `default` (78 cells). Only
*unset* colors degrade to the terminal's 16 ANSI colors.

Consequences, both binding:

- **`ansi_color=True` is on permanently**, in all three modes. It costs nothing given the rule
  below, and it is the only lever that produces a bare cell.
- **Every color must be explicitly set.** An unset foreground silently becomes 16-color ANSI and
  your palette is gone. This is the second reason for the no-hex-outside-`theme.py` rule.

### 10.2 The three modes

Bound to `ctrl+g`, cycling in this order:

```
opaque   every cell painted. Safe over SSH, in screenshots, and in terminals
         with no opacity setting.
gaps     DEFAULT. Screen background bare; pane interiors painted $surface.
         Wallpaper shows in the 1-cell gutters and the screen margin — the
         Hyprland gap look — while all text sits on a solid ground.
bare     Nothing painted at all - borders and text carry the theme. The Input
         caret is the single exception, because textual draws it as a
         background block and transparent means no caret.
         AMENDED 2026-09-25. It read "Screen and pane interiors both bare. Only
         the composer, status bar, modals and selected rows stay painted", which
         looked half-finished rather than transparent.
```

Implement as a CSS class on the root (`-opaque` / `-gaps` / `-bare`), not as three stylesheets.

### 10.3 Readability, stated honestly

In `bare` mode, `$muted` text over a busy wallpaper will be hard to read and that is not fixable
from inside the app. Two mitigations, both required:

- In `bare`, `$muted` is promoted to `$foreground` for body text; it stays `$muted` only inside
  painted chrome.
- ~~Modals, the composer and the status bar are painted in **all three** modes. An approval
  prompt must never be hard to read.~~ **AMENDED 2026-09-25**, on request: in `bare` nothing is
  painted, so this holds in `opaque` and `gaps` only. What carries a dialog there is its BORDER,
  which every one already has - thick `$error` for an approval, thick `$accent` for a question.
  The first mitigation above is unchanged and now covers the chrome too, since there is no longer
  a fill under it to keep `$muted` readable. §10.4's emulator note is what `bare` now rests on
  entirely.

### 10.4 The emulator half

The app cannot set window opacity. Tell the user once, in the wizard's final screen and in
`--help`, that Windows Terminal needs this in the profile:

```json
"opacity": 85,
"useAcrylic": true
```

(kitty `background_opacity`, WezTerm `window_background_opacity`, Alacritty `opacity`.)

### 10.5 Config

`config.py` gains `TUI_TRANSPARENT = _env("AGENT_TUI_TRANSPARENT", "gaps")`. Unrecognised values
fall back to `gaps` without raising.

---

## 11. Motion

Micro only. **Nothing ever changes position.** Only color and opacity move.

| event | change | duration |
|---|---|---|
| focus moves | border `$border` → `$accent` | 120ms ease-out |
| pane opens | opacity 0 → 1 | 140ms |
| pane closes | opacity 1 → 0 | 90ms |
| tool running | braille spinner `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`, one cell | 80ms/frame |
| key probe running | same spinner | 80ms/frame |
| approval opens | border pulses `$error` twice, then holds | 2 × 180ms |
| streamed text | appended; no reflow, no fade | — |

No boot animation, no gradient sweep, no staggered rows, no sliding panes. The logotype is static.

**No timer may run while nothing is moving.** The spinner's interval starts when a tool starts and
is cancelled when it ends. Idle CPU is 0%.

---

## 12. Latency budget

A requirement, not an aspiration. Treat each line as a check.

1. **Nothing touches disk or SQLite on the render path.** Landing counts, thread lists, task lists
   and doctor lines load in `@work(thread=True)` after first paint, with `·` placeholders.
2. **First paint before any query.** The logotype is on screen before the checkpoint DB is opened.
3. **Buffer the stream.** Token deltas accumulate and flush on a ~50ms timer, not per delta. The
   existing code does this and a test is named for it — keep both.
4. **`RichLog` with `max_lines`,** append only. Never re-render the transcript to add a line.
5. **Layout rebuild only on layout change.** Assert it: streaming 200 chunks produces zero rebuilds.
6. **No CSS rule matching broadly on hover or focus-within** over a container with many children.
7. **Import cost:** `agent/ui/` is not imported unless `--tui` is passed; `agent/setup.py`'s
   `needed()` imports no `textual`.
8. **Resize** recomputes rects and restyles; it does not rebuild the widget tree.

---

## 13. Keybindings

Alt-prefixed for panes, because the composer owns plain keys. Declare `priority=True`.

```
alt+h / j / k / l    move focus left / down / up / right
alt+enter            split the focused pane (dwindle picks the orientation)
alt+w                close the focused pane   (chat refuses)
alt+z                zoom the focused pane / restore
alt+H / J / K / L    resize the focused split by 5%
tab / shift+tab      cycle focus through panes, then the composer
ctrl+p               command palette overlay
ctrl+t               cycle theme
ctrl+g               cycle transparency: opaque → gaps → bare
ctrl+k               open the setup wizard
ctrl+q               quit
escape               unzoom; if not zoomed, return focus to the composer
```

`ctrl+c` stays quit, as today. Do not rebind it.

Show a dim binding hint at the status bar's right edge only when a pane — not the composer — has
focus.

---

## 14. Port verbatim; do not redesign

These work and are tested. Move them, restyle them, change no behaviour:

- The **graph worker thread model** in `agent/tui.py`: `@work(thread=True)`, `call_from_thread` in
  both directions, and the approval pause that *blocks* the worker until a human answers. This is
  the hardest correct thing in the current file. Do not touch its logic.
- **`COMMANDS` and the slash rule** (`agent/tui.py:126`, `:648`).
- **`ApprovalScreen`:** escape denies, Enter refuses, every argument shown in full and never
  parsed as console markup.
- **`PlanScreen`:** escape revises rather than adopts.
- The tool-summary escaping — a result containing `[bold]` renders as text.

---

## 15. Tests

Same standard as the existing suite: no API key, no network, read-only root.

- **`tests/test_ui_tiling.py` — new, written first.** Pure, fast, no Textual. Split chooses
  orientation by aspect; rects sum exactly with gaps; the 3×12 minimum holds and `split` refuses
  when it would break it; close promotes the sibling; closing the last leaf yields `None`; focus is
  geometric across a three-level tree; focus at an edge returns the source; resize clamps at 0.15
  and 0.85.
- **`tests/test_ui_setup.py` — new.** No network: fake `call_model`. A rejected key
  (`ProviderMisconfigured`) is **not** saved; an unavailable endpoint (`ProviderUnavailable`)
  offers save-anyway; a successful probe writes `.env` **and** `os.environ`; rewriting an existing
  variable replaces its line rather than appending; the file is written with `\n` and no quotes;
  the key never appears in any rendered output; `needed()` is `False` when a key is present;
  `interactive()` is `False` without a TTY; **changing the model and reloading `config` yields the
  new `OPENAI_MODEL`** (the import-time trap in section 6.2).
- **`tests/test_tui.py` — adapt.** The ~19 tests covering modals, command dispatch, streaming,
  worker-thread delivery and tool-line rendering should survive with import changes only. The ~11
  asserting on `TasksScreen` / `SchedulesScreen` / `DoctorScreen` as *screens* become pane tests.
  Do not delete an assertion to make it pass — if a behaviour genuinely moved, move the assertion.
- **New:** all six logotype rows are exactly 46 cells, and the six rows equal the literals in
  section 5.2 character for character (a golden test — do NOT write a "no double space" check,
  because `E`'s short arm and `N`'s diagonal contain doubled spaces legitimately, and such a test
  fails on correct art); every theme registers and every semantic
  variable resolves in all three; the landing renders with counts unavailable; the status bar shows
  a step in every layout including zoom (FR-702); `alt+w` on `chat` is refused; the landing omits
  the egress field when `AGENT_EGRESS` is unset.
- **Transparency:** in `gaps` mode the rendered strips contain both a `default` background and a
  painted one; in `opaque` mode, no `default` background appears. Assert on
  `app.screen._compositor.render_strips()` — that is how I measured it and it works headless.
- Grep all of `agent/ui/` for `#` followed by six hex digits outside `theme.py`. Zero hits.

---

## 16. The §12 justification to write

Add to `CONTEXT.md` §12, in §12's own voice — it records deviations rather than hiding them:

> `ui/` — the TUI as a package rather than one module. §12 listed `tui.py` alone, which was right
> for a docked transcript and an input box; the dwindle layout, three themes, three transparency
> modes and eight panes are a different quantity of code and the premise expired. Earned under
> CE-01 three ways: `tiling.py` has two callers and is pure enough to test without Textual at all,
> which is the point of extracting it; `panes.py` holds eight implementations of one interface;
> `theme.py` holds three of one Theme shape. `setup.py` is the first-run credential wizard, kept
> out of `cli.py` because it must run BEFORE config resolves — `config.py` reads every tunable at
> import time, so the wizard runs from the entry point in the same window `.env` is loaded in.
> `tui.py` remains as the entry shim so the `--tui` path and NFR-602's lazy import are unchanged.

---

## 17. Definition of done

- On a machine with no `.env`, `python -m agent --tui` opens the wizard, a wrong key is **rejected
  with the provider's own message and not saved**, a right key is saved and verified, and the TUI
  opens — without a restart.
- With no TTY, no wizard appears and the existing `RuntimeError` message is what the user sees.
- `python -m agent --tui` opens the NOESIS landing; the logotype is on screen before any query runs,
  and all six of its rows are exactly 46 cells.
- Typing a goal starts a session; `/tasks` opens the workspace with the tasks pane already split in.
- `alt+enter` splits, `alt+hjkl` moves, `alt+w` closes, `alt+z` zooms — and `chat` cannot be closed.
- `ctrl+t` cycles all three themes live; every pane, modal and badge recolors correctly in each.
- `ctrl+g` cycles all three transparency modes live; in `gaps`, gutters are bare and pane interiors
  are painted.
- The active step is visible in the status bar in every layout, zoomed or not.
- An approval still pauses the run and still denies on escape.
- `pytest` is green with no API key and no network, and with `textual` uninstalled for everything
  except the UI tests.
- Idle CPU is 0% with the workspace open and nothing running.

---

## 18. Do not

- Do not add a boot animation, gradient sweep, staggered rows, or sliding panes. Micro-motion was
  a decision, not an oversight.
- Do not write hex outside `theme.py` — it breaks two themes and all transparency.
- Do not add spacing between the logotype's letters, and do not regenerate it from figlet at
  runtime. The six strings in section 5.2 are the artwork.
- Do not save an API key that failed its probe.
- Do not print the key, anywhere, ever — including in an error message.
- Do not print an egress state that nothing set.
- Do not let a pane rebuild happen during streaming.
- Do not make Enter approve anything.
- Do not add a dependency.
- Do not "improve" adjacent code. Every changed line outside `agent/ui/` traces to this request.
- Do not report it done until `pytest` has actually run and you have the output. If something here
  turned out not to work, say so plainly in the first sentence rather than calling it partially
  successful.
