"""The two screens, and the app that owns them.

Two implementations of one Screen shape, which is what earns the module under
CE-01 - the same standard `panes.py` and `theme.py` meet.

THE THREADING MODEL is ported from agent/tui.py unchanged, because it is the
hardest correct thing here. `app.invoke()` is synchronous and Textual owns an
event loop, so the graph runs on a thread worker; everything it reports crosses
back through `call_from_thread`. The approval pause needs the same primitive in
reverse: `gate` suspends, `invoke()` returns with `__interrupt__`, and the
worker must BLOCK until a human answers.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from langgraph.types import Command
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.suggester import SuggestFromList
from textual.widgets import DataTable, Input, Static
from textual.worker import Worker, WorkerState

from agent import cli
from agent import config as settings
from agent import graph
from agent import policy
from agent import tools
from agent.ui import panes, theme, tiling
from agent.ui.modals import ApprovalScreen, AskScreen, PlanScreen

# ANSI Shadow, tightly kerned: zero added spacing between letters. The gaps
# that remain are intrinsic to the letterforms - the diagonal of N and the
# notched corners of O - and closing them means hand-editing the glyphs, which
# reads as broken. 46 cells x 6 rows, asserted.
LOGO = (
    "███╗   ██╗ ██████╗ ███████╗███████╗██╗███████╗",
    "████╗  ██║██╔═══██╗██╔════╝██╔════╝██║██╔════╝",
    "██╔██╗ ██║██║   ██║█████╗  ███████╗██║███████╗",
    "██║╚██╗██║██║   ██║██╔══╝  ╚════██║██║╚════██║",
    "██║ ╚████║╚██████╔╝███████╗███████║██║███████║",
    "╚═╝  ╚═══╝ ╚═════╝ ╚══════╝╚══════╝╚═╝╚══════╝",
)

LOGO_WIDTH = 46

# Warn here, not at the wall: past this a conversation is close enough that
# /chat is still a choice. 0.8 leaves room for several more exchanges.
LOW_BUDGET = 0.8


def _spend(values: dict) -> str:
    """Spent tokens, against the budget when there is one (config's 0 = none).
    A bare 204,972 is a number with nothing to compare it to."""
    spent = values.get("spent_tokens", 0)
    budget = values.get("budget_tokens", settings.BUDGET_TOKENS)
    return f"{spent:,}/{budget:,}" if budget else f"{spent:,}"

# One frame per 80ms while the graph is working, and no timer at all otherwise.
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# The slash commands, and the only list of them: the suggester completes from
# these keys, `command()` dispatches on them, and /help prints them.
COMMANDS = {
    "/new": "start a fresh chat",
    "/threads": "resume past work",
    "/tasks": "the queue",
    "/schedules": "cron schedules, soonest first",
    "/plan": "the current plan and where it is",
    "/trace": "every tool call this session",
    "/artifact": "the last spilled output, in full",
    "/review": "queue a review of what needs attention",
    "/serve": "a read-only viewer in the browser",
    "/doctor": "every precondition, ok or FAIL",
    "/mode": "how much the gate asks",
    "/setup": "change model or API key",
    "/help": "this list",
    "/exit": "close NOESIS",
}

# Which pane a command opens beside chat. /chat is an empty workspace.
OPENS = {"/threads": "threads", "/tasks": "tasks", "/schedules": "schedules",
         "/doctor": "doctor", "/plan": "plan", "/trace": "trace",
         "/artifact": "artifact"}


def weigh(first, second, ratio: float, side_by_side: bool) -> None:
    """Express a split's ratio as the two children's `fr` weights.

    One definition, because `build` and a live drag both set it and a drag that
    weighed differently would jump back on the next rebuild.
    """
    for child, share in ((first, ratio), (second, 1 - ratio)):
        weight = f"{max(1, round(share * 100))}fr"
        if side_by_side:
            child.styles.width = weight
        else:
            child.styles.height = weight


def still(box: Input) -> Input:
    """An Input with no blinking cursor.

    Section 11 allows no timer while nothing is moving, and a blink is a live
    interval for as long as the box has focus - which for the composer is the
    whole session. Set before mount, so the timer is created already paused.
    """
    box.cursor_blink = False
    return box


def is_command(text: str) -> str:
    """The command a line names, or "" if the line is a message.

    Ported verbatim in RULE from agent/tui.py: a command is a lone slash word,
    so "/usr/bin/python is missing" is a sentence and must reach the model,
    while "/taks" is a typo that gets the list rather than being sent as a
    goal. The further slash is what separates them.
    """
    if not text.startswith("/"):
        return ""
    word = text.split()[0].lower()
    if "/" in word[1:]:
        return ""                     # a path, and paths are not commands
    return word


def help_text() -> Text:
    """The command list, rendered into the transcript rather than a modal.

    It belongs in the scrollback: a modal you have to dismiss to type the thing
    it just told you about is a worse way to learn seven words.
    """
    out = Text()
    for name, what in COMMANDS.items():
        out.append(f"{name:<12}", style="row--ran")
        out.append(f"{what}\n", style="row--muted")
    return out


def said(who: str, text: str):
    """One turn of the conversation, with a gutter that says whose it is."""
    if who:
        head = Text(f"\n{who:<4} ", style="row--selected")
        head.append(text.strip(), style="none")
        return head
    return Padding(Markdown(text.strip()), (0, 0, 0, 5))


def replay(messages: list[dict]) -> list:
    """The transcript of a thread already on disk, so resuming shows a
    conversation rather than an empty screen with history it will not admit to.

    Tool RESULTS are deliberately skipped: they are already summarised on the
    call's own line, and a resumed thread would otherwise open with several
    screens of shrunk file contents.
    """
    out = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user":
            if isinstance(content, str):
                out.append(said("you", content))
            continue
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append(said("", block["text"]))
            elif block.get("type") == "tool_use":
                out.append(panes.tool_text(
                    {"tool": block.get("name", "?"),
                     "summary": str(block.get("input", ""))}))
    return out


# ================================================================== landing

class LandingScreen(Screen):
    """The logotype, and a selectable list of what this thing can be asked."""

    BINDINGS = [
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.index = 0
        self.counts: dict[str, str] = {}
        self.flag = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="landing"):
            yield Static(id="logotype")
            yield Static(id="landing-status")
            yield Static(id="landing-list")
            yield still(Input(placeholder="›", id="landing-input",
                              suggester=SuggestFromList(COMMANDS,
                                                        case_sensitive=False)))
            yield Static(id="landing-hint")

    def on_mount(self) -> None:
        self.app.dress(self)
        self.paint()
        self.query_one(Input).focus()
        # Section 12: first paint before any query. The counts arrive after.
        self.load_counts()

    def on_resize(self) -> None:
        self.paint()

    # ------------------------------------------------------------- painting

    def paint(self) -> None:
        width, height = self.size.width, self.size.height
        self.query_one("#logotype", Static).update(logotype(width))
        # Below 20 rows drop the hint first, then the status line. The landing
        # must never scroll and never clip the command list.
        self.query_one("#landing-hint").display = height >= 20
        self.query_one("#landing-status").display = height >= 18
        self.query_one("#landing-status", Static).update(status_line())
        self.query_one("#landing-hint", Static).update(
            Text("type to begin  ·  ↑↓ select  ·  ⏎ run  ·  ^t theme  ·  ^g gaps",
                 style="row--muted"))
        self.paint_list()

    def paint_list(self) -> None:
        shown = self.matches()
        out = Text()
        for position, name in enumerate(shown):
            chosen = position == self.index
            out.append("▸ " if chosen else "  ",
                       style="row--selected" if chosen else "row--muted")
            out.append(f"{name:<12}",
                       style="row--selected" if chosen else "row--ran")
            out.append(f"{COMMANDS[name]:<32}", style="row--muted")
            out.append(f"{self.counts.get(name, '·'):>4}", style="row--muted")
            # Section 5.4: $error when anything awaits approval, $accent when
            # something is running, absent otherwise.
            badge = self.flag if name == "/tasks" else ""
            out.append(f" {'●' if badge else ' '}\n",
                       style="row--denied" if badge == "error" else "row--logo")
        if not shown:
            out.append("  no command matches - press ⏎ to send it as a goal\n",
                       style="row--muted")
        self.query_one("#landing-list", Static).update(out)

    def matches(self) -> list[str]:
        typed = self.query_one(Input).value.strip().lower()
        if not typed.startswith("/"):
            return list(COMMANDS)
        return [name for name in COMMANDS if name.startswith(typed)] or []

    @work(thread=True, exclusive=True)
    def load_counts(self) -> None:
        """Live counts, off the render path (section 12.1). Nothing here may
        touch a widget; `·` stands in until this returns."""
        from agent import worker

        found, flag = {}, ""
        try:
            tasks = worker.tasks()
            found["/tasks"] = str(len(tasks)) if tasks else ""
            if any(row["status"] == "awaiting-approval" for row in tasks):
                flag = "error"
            elif any(row["status"] == "running" for row in tasks):
                flag = "accent"
            found["/schedules"] = str(len(worker.schedules()) or "")
            found["/threads"] = str(len(cli._thread_rows(self.app.graph)) or "")
        except Exception:                              # noqa: BLE001
            # A count is decoration. A landing screen that refuses to draw
            # because the task database is busy is not.
            pass
        self.app.call_from_thread(self.counted, found, flag)

    def counted(self, found: dict, flag: str) -> None:
        # Kept even when empty: `·` means "not loaded yet", and a queue with
        # nothing in it is a different statement from one nobody has asked.
        self.counts = dict(found)
        self.flag = flag
        self.paint_list()

    # ---------------------------------------------------------------- input

    def action_move(self, delta: int) -> None:
        shown = self.matches()
        if shown:
            self.index = (self.index + delta) % len(shown)
            self.paint_list()

    @on(Input.Changed, "#landing-input")
    def _typed(self) -> None:
        self.index = 0                # the selection snaps to the first match
        self.paint_list()

    @on(Input.Submitted, "#landing-input")
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        shown = self.matches()
        if not text and shown:
            self.run_command(shown[self.index])
            return
        if not text:
            return
        word = is_command(text)
        if word and word in COMMANDS:
            self.run_command(word)
        elif word:
            self.app.bell()           # a typo gets the list, never a guess
            self.query_one(Input).value = ""
        else:
            self.app.open_workspace(goal=text)

    def run_command(self, word: str) -> None:
        if word == "/help":
            self.query_one(Input).value = ""
            return
        if word == "/exit":
            # Handled here as well as in the workspace: this dispatcher's
            # fallback OPENS a session, so an unlisted command would start one
            # rather than end it.
            self.app.exit()
            return
        if word == "/setup":
            self.app.action_setup()
            return
        # An ACTION has no pane, and the fallback would open a session and
        # silently do nothing - the shape the /exit defect had. Carried through
        # and run on mount, so its output lands in the transcript.
        pane = OPENS.get(word)
        self.app.open_workspace(pane=pane, run=None if pane else word)


def logotype(width: int) -> Text:
    """The wordmark, or its narrow form. Uniform accent, never a fade."""
    if width < LOGO_WIDTH + 8:
        return Text("N O E S I S", style="row--logo", justify="center")
    out = Text(justify="center")
    for row in LOGO:
        out.append(f"{row}\n", style="row--logo")
    return out


def status_line() -> Text:
    """provider · model · time, and the truth about each.

    The model is the ACTUALLY resolved one, never a literal. Egress is printed
    only when AGENT_EGRESS is set in the environment: a default that asserts
    the safe answer is how a row comes to claim a condition nobody checked.
    """
    from agent import setup

    if setup.needed():
        return Text("no API key  ·  press ^k or type /setup",
                    style="row--denied", justify="center")
    provider = settings.PROVIDER
    model = (settings.MODEL if provider == "anthropic" else settings.OPENAI_MODEL)
    parts = [provider, model, time.strftime("%H:%M")]
    egress = os.environ.get("AGENT_EGRESS")
    if egress:
        parts.append(f"{egress} egress")
    return Text("  ·  ".join(parts), style="row--muted", justify="center")


# ================================================================ workspace

class WorkspaceScreen(Screen):
    """Chat is one pane among several, and the panes tile.

    Every pane's data lives HERE, never only inside a widget: a layout change
    rebuilds the container tree, and that is only affordable if a rebuilt pane
    can repopulate itself.
    """

    BINDINGS = [
        # priority, because the composer owns plain keys and an Input would
        # otherwise swallow every one of these.
        Binding("alt+h", "move('left')", "left", priority=True, show=False),
        Binding("alt+j", "move('down')", "down", priority=True, show=False),
        Binding("alt+k", "move('up')", "up", priority=True, show=False),
        Binding("alt+l", "move('right')", "right", priority=True, show=False),
        Binding("alt+enter", "split", "split", priority=True, show=False),
        Binding("alt+w", "close", "close", priority=True, show=False),
        Binding("alt+z", "zoom", "zoom", priority=True, show=False),
        Binding("alt+H", "grow('left')", "wider", priority=True, show=False),
        Binding("alt+J", "grow('down')", "taller", priority=True, show=False),
        Binding("alt+K", "grow('up')", "shorter", priority=True, show=False),
        Binding("alt+L", "grow('right')", "narrower", priority=True, show=False),
        Binding("tab", "cycle(1)", "next", priority=True, show=False),
        Binding("shift+tab", "cycle(-1)", "previous", priority=True, show=False),
        Binding("escape", "unzoom", "back", priority=True, show=False),
        Binding("ctrl+c", "app.quit", "quit", show=False),
    ]

    def __init__(self, thread: str, goal: str | None = None,
                 pane: str | None = None, run: str | None = None) -> None:
        super().__init__()
        self.thread = thread
        self._goal = goal
        self._opening = pane
        self._pending = run
        self.tiles = tiling.Leaf("chat")
        self.zoomed: str | None = None
        self.focus_id = "chat"
        self.transcript: list = []
        self.trace_rows: list[dict] = []
        self.plan: list[str] = []
        self.cursor = 0
        self.artifact: Path | None = None
        self.rebuilds = 0
        self._warned_low = False
        # Widgets per split, refilled by every build; and the divider being
        # dragged, or None. Both belong to the mounted tree, not to the model.
        self.boxes: dict[tuple, tuple] = {}
        self.dragging: tiling.Divider | None = None
        self._queued: list[str] = []
        self._stream = ""
        self._recalled = False
        self._step = None
        self._spin = None
        self._frame = 0
        # The graph's state, cached. `values()` reads the checkpoint DATABASE,
        # and section 12.1 keeps disk off the render path - measured at 200
        # SQLite reads for 200 streamed tokens before this existed.
        self._state: dict = {}

    # --------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Vertical(id="tiled")
        # ONE docked container, not two docked widgets: textual places two
        # widgets docked to the same edge in the SAME rows, so the status bar
        # and the composer landed on top of each other and FR-702's step was
        # the half that lost.
        with Vertical(id="chrome"):
            yield still(Input(placeholder="say something, or /help",
                              id="composer",
                              suggester=SuggestFromList(COMMANDS,
                                                        case_sensitive=False)))
            yield Static(id="status")

    def on_mount(self) -> None:
        self.app.dress(self)
        if self._opening:
            self.tiles = tiling.split(self.tiles, "chat", self._opening,
                                     *self.area())
            # Asking for /threads is asking to USE it. Leaving `chat` active
            # put the keys on the composer, so the table listed every thread
            # and its row cursor could not move.
            self.focus_id = self._opening
        self.rebuild()
        prior = self.reread()
        for renderable in replay(prior.get("messages") or []):
            self.transcript.append(renderable)
        if self.transcript:
            self.refresh_pane("chat")
        if self._goal:
            # Without this the first thing you see after typing a goal is an
            # empty screen, which reads as broken rather than busy.
            self.write(said("you", self._goal))
            self.begin(self._goal)
        elif prior and prior.get("verdict") is None and prior.get("messages"):
            self.start(None)
        if self._pending:
            pending, self._pending = self._pending, None
            self.command(pending)
        # `rebuild` marks focus after a refresh, so this must not race it back
        # onto the composer when a pane was opened by name.
        if not self._opening:
            self.query_one(Input).focus()

    def on_key(self, event) -> None:
        """Typing always reaches the agent, even while a table holds the keys.

        Focus follows the active pane so arrows and Enter work in a table
        (without that, `focus_id` is only a border colour). A printable
        character means you are composing, not navigating, so the composer
        takes it back and receives the keystroke.
        """
        composer = self.query_one("#composer", Input)
        if (getattr(event, "is_printable", False)
                and self.focused is not composer):
            self.focus_id = "chat"
            self.mark_focus()
            if event.character:
                composer.insert_text_at_cursor(event.character)
            event.stop()
            event.prevent_default()

    def area(self) -> tuple[int, int]:
        """The tiled area, which is the screen minus the two docked rows."""
        return max(1, self.size.width), max(1, self.size.height - 2)

    def rebuild(self) -> None:
        """Re-mount the container tree. Split, close, zoom and resize ONLY -
        all user-initiated, all rare. A rebuild on a streamed token is a bug."""
        self.rebuilds += 1
        region = self.query_one("#tiled", Vertical)
        region.remove_children()
        self.boxes.clear()          # every widget it named is being discarded
        region.mount(self.build(tiling.Leaf(self.zoomed) if self.zoomed
                                else self.tiles))
        # Each pane refreshes itself on ITS mount; the border can only be
        # marked once they are all up.
        self.call_after_refresh(self.mark_focus)

    def build(self, node):
        if isinstance(node, tiling.Leaf):
            return panes.PANES[node.pane_id]()
        side_by_side = node.orientation == "v"
        first, second = self.build(node.a), self.build(node.b)
        # One gap BETWEEN siblings and none on the outer edge, which is what
        # tiling.rects computes and what the eye expects.
        first.add_class("gutter-right" if side_by_side else "gutter-below")
        weigh(first, second, node.ratio, side_by_side)
        # Keyed by the leaf ids under the split, not by the node: a drag builds
        # a new frozen node every move, and the widgets do not change.
        self.boxes[tuple(tiling.leaves(node))] = (first, second, side_by_side)
        return (Horizontal(first, second) if side_by_side
                else Vertical(first, second))

    def pane(self, pane_id: str):
        """The widget, or None while it is still being mounted."""
        found = self.query(f"#pane-{pane_id}")
        return found.first(panes.Pane) if found else None

    def mark_focus(self) -> None:
        for pane_id in self.showing():
            widget = self.pane(pane_id)
            if widget is not None:
                widget.set_class(pane_id == self.focus_id, "-active")
        self.give_keys_to(self.focus_id)

    def give_keys_to(self, pane_id: str) -> None:
        """Real keyboard focus, not only the border colour.

        Without this `focus_id` is a class name: the composer is focused on
        mount and never yields, so arrows move the text cursor, a table's row
        cursor cannot move, and Enter submits the composer instead of reaching
        RowSelected. Reported from the TUI as "arrow keys don't work" and
        "I cannot select the previous chats".
        """
        if pane_id == "chat":
            self.query_one("#composer", Input).focus()
            return
        pane = self.pane(pane_id)
        if pane is None:
            return
        inner = next((w for w in pane.walk_children() if w.focusable), None)
        (inner or pane).focus()

    def showing(self) -> list[str]:
        return [self.zoomed] if self.zoomed else tiling.leaves(self.tiles)

    # ------------------------------------------------------------- the keys

    # ------------------------------------------------------------- the mouse

    def on_mouse_down(self, event) -> None:
        """Grab a divider. Anything else is left to the widget under it."""
        if self.zoomed or isinstance(self.tiles, tiling.Leaf):
            return
        width, height = self.area()
        found = tiling.divider_at(self.tiles, event.screen_x, event.screen_y,
                                  width, height)
        if found is None:
            return
        self.dragging = found
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event) -> None:
        """Resize live, WITHOUT a rebuild: `build` already expresses the ratio
        as two `fr` weights, so a drag is those two numbers changing. Re-mounting
        the tree per mouse-move would drop the transcript and the focus."""
        if self.dragging is None:
            return
        ratio = tiling.ratio_at(self.dragging, event.screen_x, event.screen_y)
        moved = tiling.set_ratio(self.tiles, self.dragging, ratio)
        if moved is not self.tiles:
            self.tiles = moved
            found = self.boxes.get(self.dragging.leaves)
            if found is not None:
                weigh(found[0], found[1], ratio, found[2])
        event.stop()

    def on_mouse_up(self, event) -> None:
        if self.dragging is None:
            return
        self.dragging = None
        self.release_mouse()
        event.stop()

    def action_move(self, direction: str) -> None:
        if self.zoomed:
            return
        self.focus_id = tiling.focus(self.tiles, self.focus_id, direction,
                                     *self.area())
        self.mark_focus()
        self.paint_status()

    def action_cycle(self, delta: int) -> None:
        order = self.showing()
        index = (order.index(self.focus_id) + delta) % len(order) \
            if self.focus_id in order else 0
        self.focus_id = order[index]
        self.mark_focus()
        self.paint_status()

    def action_split(self) -> None:
        spare = [name for name in panes.PANES if name not in self.showing()]
        if self.zoomed or not spare:
            return
        grown = tiling.split(self.tiles, self.focus_id, spare[0], *self.area())
        if grown is self.tiles:
            self.notify("not enough room to split")
            return
        self.tiles, self.focus_id = grown, spare[0]
        self.rebuild()

    def action_close(self) -> None:
        if self.zoomed:
            return
        if not panes.PANES[self.focus_id].closeable:
            self.notify("chat cannot be closed")
            return
        left = tiling.close(self.tiles, self.focus_id)
        if left is None:
            return
        self.tiles = left
        self.focus_id = tiling.leaves(left)[0]
        self.rebuild()

    def action_zoom(self) -> None:
        self.zoomed = None if self.zoomed else self.focus_id
        self.rebuild()

    def action_unzoom(self) -> None:
        if self.zoomed:
            self.zoomed = None
            self.rebuild()
            return
        self.query_one(Input).focus()

    def action_grow(self, direction: str) -> None:
        if self.zoomed:
            return
        self.tiles = tiling.resize(self.tiles, self.focus_id, direction, 0.05)
        self.rebuild()

    # ------------------------------------------------------------ the graph

    def cfg(self) -> dict:
        return {"configurable": {"thread_id": self.thread}}

    def values(self) -> dict:
        return self.app.graph.get_state(self.cfg()).values or {}

    def reread(self) -> dict:
        """Refresh the cached state. Called where it can actually have changed -
        a turn boundary - and never from the streaming path."""
        self._state = self.values()
        self.paint_status()
        return self._state

    def begin(self, text: str) -> None:
        prior = self.values()          # a write path, not a render path
        self.start(graph.continue_state(prior, text) if prior.get("messages")
                   else graph.new_state(text))

    def ask(self, question: str, choices: list[str]) -> str:
        """`ask_user` reaching a person, from the graph's worker thread.

        The same primitive the approval pause uses in reverse: call_from_thread
        runs the modal on the event loop and BLOCKS this thread until it is
        answered. A skipped question returns "", and the tool turns that into
        its own use-your-best-judgement text.
        """
        return self.app.call_from_thread(
            self.app.push_screen_wait, AskScreen(question, choices)) or ""

    @work(thread=True, exclusive=True)
    def start(self, payload: dict | None) -> None:
        """The graph, on a worker thread. Everything it says crosses back to the
        event loop through call_from_thread; nothing here touches a widget."""
        app = self.app
        # The TOOL owns the schema, the SURFACE owns the asking.
        tools.ASK = self.ask
        trace = cli.LiveTrace(sink=lambda e: app.call_from_thread(self.on_trace, e))
        cfg = {"configurable": {
            "thread_id": self.thread,
            "autonomous": False,   # the switch that makes `confirm` pause, not refuse
            "mode": app.gate,
            "trace": trace,
            "on_text": lambda t: app.call_from_thread(self.on_text, t),
        }}
        try:
            out = self.app.graph.invoke(payload, cfg)
            while "__interrupt__" in out:
                paused = out["__interrupt__"][0].value
                # Two things pause a run: a call the gate wants approved, and a
                # plan waiting to be adopted. Told apart by the payload, the
                # same way cli.ask_human does it.
                planning = "plan" in paused
                if not planning and cli.remembered(paused):
                    answer = "allow"         # approved for this session already
                else:
                    answer = app.call_from_thread(
                        app.push_screen_wait,
                        PlanScreen(paused) if planning else ApprovalScreen(paused))
                if answer == cli.QUIT:
                    app.call_from_thread(
                        self.note, "stopped, still checkpointed", "row--muted")
                    return
                if planning:
                    # Anything other than an explicit accept revises, so a
                    # dismissed modal cannot adopt a plan nobody agreed to.
                    resume = "accept" if answer == "accept" else "revise"
                elif isinstance(answer, dict):
                    # FR-307. Straight through: the gate re-classifies it, and
                    # an amendment is never remembered - the rule it was
                    # remembered under described the original arguments.
                    resume = answer
                else:
                    # Anything that is not an explicit allow is a refusal.
                    if answer == "session":
                        cli.remember(paused)
                    resume = "allow" if answer in ("allow", "session") else "deny"
                out = self.app.graph.invoke(Command(resume=resume), cfg)
        except Exception as exc:                       # noqa: BLE001
            # A provider error, a rate limit, a dead MCP server. Textual would
            # otherwise log it to a devtools console nobody is watching and the
            # screen would just stop moving.
            app.call_from_thread(self.note, f"{type(exc).__name__}: {exc}",
                                 "row--denied")

    def on_trace(self, entry: dict) -> None:
        kind = entry.get("kind")
        if kind == "model":
            self.flush()
        elif kind == "tool":
            self.trace_rows.append(entry)
            self.append_tool(entry)
        elif kind == "memory" and not self._recalled:
            # Once per session, not once per turn: `act` re-injects it every
            # turn and a line each time would say nothing new.
            self._recalled = True
            self.note(f"recalled {entry['chars']} chars of memory", "row--muted")
        elif kind == "step":
            self.plan = list(entry.get("plan") or self.plan)
            self.cursor = entry["cursor"]
            if entry["cursor"] != self._step:
                self._step = entry["cursor"]
                self.note(f"step {entry['cursor'] + 1}/{entry['of']}: "
                          f"{entry['text']}", "row--selected")
            self.refresh_pane("plan")
        elif kind == "skill":
            self.note(f"learned skill: {entry['name']}", "row--muted")
        elif kind == "terminal":
            self.flush()
            style = {"done": "row--mutated",
                     "stuck": "row--muted"}.get(entry["verdict"], "row--denied")
            self.note(f"\n{entry['verdict']} - {entry['turns']} turns, "
                      f"{entry['spent_tokens']:,} tokens", style)
            if entry["verdict"] == "budget":
                # spent_tokens is cumulative across a conversation on purpose,
                # and reflect checks it FIRST - so from here every message ends
                # the same way, before the model is called even once. A number
                # alone leaves someone staring at a thread that is finished
                # without saying it is finished. Memory outlives the thread, so
                # /chat loses nothing that was learned.
                self.note("this conversation has spent its whole budget, so "
                          "further messages will stop here. /chat starts a "
                          "fresh one - what it remembers about you carries "
                          "over.", "row--muted")
            elif not self._warned_low:
                # WHILE /chat is still a choice rather than a recovery. Once,
                # because a warning on every turn is noise and the first thing
                # anyone does with noise is stop reading it - the same reason
                # worker.review() stays silent when there is nothing to say.
                budget = self._state.get("budget_tokens",
                                         settings.BUDGET_TOKENS)
                if budget and entry["spent_tokens"] >= budget * LOW_BUDGET:
                    self._warned_low = True
                    self.note(f"running low: {entry['spent_tokens']:,} of "
                              f"{budget:,} tokens. /chat starts a fresh "
                              f"conversation and costs nothing remembered.",
                              "row--muted")
        # Only a turn boundary can move turns, tokens or the verdict.
        if kind in ("model", "step", "terminal"):
            self.reread()
        else:
            self.paint_status()

    def on_text(self, text: str) -> None:
        """Model prose as it arrives, buffered on the status line.

        The two providers deliver it differently - Anthropic streams deltas, the
        OpenAI-compatible path hands over one finished block - so a log line per
        delta would shred a sentence into one word per row.
        """
        self._stream += text
        self.paint_status()

    def flush(self) -> None:
        if self._stream.strip():
            self.write(said("", self._stream))
        self._stream = ""
        self.paint_status()

    # ---------------------------------------------------------------- input

    @on(Input.Submitted, "#composer")
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        if self.command(text):
            return
        self.write(said("you", text))
        if any(worker.is_running for worker in self.workers):
            # Typed mid-run. Queued rather than interleaved: the message list
            # belongs to the graph while a turn is in flight.
            self._queued.append(text)
            self.note("queued until this turn finishes", "row--muted")
            return
        self.begin(text)

    @on(Worker.StateChanged)
    def _worker_done(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.RUNNING:
            self.spin(True)
            return
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR,
                           WorkerState.CANCELLED):
            self.spin(False)
            self.flush()
            self.reread()
        if event.state is WorkerState.SUCCESS and self._queued:
            self.begin(self._queued.pop(0))

    def spin(self, on: bool) -> None:
        """A turn takes tens of seconds, and a screen that shows nothing for
        that long reads as hung rather than busy. The interval exists only
        while the graph does (section 11)."""
        if on and self._spin is None:
            self._spin = self.set_interval(0.08, self._tick)
            self.paint_status()        # at once, not 80ms after work started
        elif not on and self._spin is not None:
            self._spin.stop()
            self._spin = None
            self._frame = 0
            self.paint_status()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(SPINNER)
        self.paint_status()

    def command(self, text: str) -> bool:
        """Run a slash command. False means it was an ordinary message."""
        word = is_command(text)
        if not word:
            return False
        if word not in COMMANDS:
            self.write(help_text())
            self.note(f"no such command: {word}", "row--muted")
            return True
        if word == "/help":
            self.write(help_text())
        elif word == "/exit":
            # A checkpoint is written after every node, so leaving mid-run costs
            # at most one node and the thread resumes by id.
            self.app.exit()
        elif word == "/review":
            self.review()
        elif word == "/serve":
            self.serve()
        elif word == "/mode":
            self.app.action_next_gate()
        elif word == "/setup":
            self.app.action_setup()
        elif word == "/new":
            # No thread: open_workspace mints one and SWITCHES the screen, which
            # is why this does not stack a screen per chat.
            self.app.open_workspace()
        else:
            self.open(OPENS[word])
        return True

    def review(self) -> None:
        """Queue a review of what is outstanding, or say there is nothing.

        worker.review() is SILENT when nothing needs attention, which is right
        for a cron tick and wrong here: this is a question someone just asked.
        """
        from agent import worker

        queued = worker.review()
        if queued is None:
            self.note("nothing needs attention", "row--muted")
        else:
            self.note(f"queued a review: {queued}", "row--selected")
        self.refresh_pane("tasks")

    def serve(self) -> None:
        """Start the read-only viewer and print its URL, with the token, once."""
        from agent import config, viewer

        try:
            port = viewer.serve_in_background()
        except Exception as exc:                   # noqa: BLE001
            self.note(f"could not start the viewer: {exc}", "row--denied")
            return
        self.note(f"http://{config.VIEWER_HOST}:{port}/?token={viewer._secret()}",
                  "row--selected")
        self.note("read-only, loopback only; it stops when NOESIS does",
                  "row--muted")

    def open(self, pane_id: str) -> None:
        """Split a pane in, or focus it if it is already up."""
        if pane_id in self.showing():
            self.focus_id = pane_id
            self.mark_focus()
            return
        grown = tiling.split(self.tiles, self.focus_id, pane_id, *self.area())
        if grown is self.tiles:
            self.notify("not enough room to split")
            return
        self.tiles, self.focus_id = grown, pane_id
        self.rebuild()

    @on(DataTable.RowSelected)
    def _picked(self, event: DataTable.RowSelected) -> None:
        """Enter on a row. A task id IS a thread id, so both tables hand back
        the same string a session takes."""
        chosen = str(event.row_key.value)
        if event.data_table.id in ("threads-table", "tasks-table") and chosen:
            self.app.open_workspace(thread=chosen)

    # --------------------------------------------------------------- output

    def write(self, renderable) -> None:
        self.transcript.append(renderable)
        del self.transcript[:-panes.MAX_LINES]
        chat = self.pane("chat")
        if chat is not None:
            chat.append(renderable)

    def note(self, text: str, style: str = "row--muted") -> None:
        self.write(Text(text, style=style))

    def append_tool(self, entry: dict) -> None:
        self.write(panes.tool_text(entry))
        trace = self.pane("trace")
        if trace is not None:
            trace.append(entry)

    def refresh_pane(self, pane_id: str) -> None:
        widget = self.pane(pane_id)
        if widget is not None:
            widget.refresh_from(self)

    def paint_status(self) -> None:
        """FR-702: the active step, visible at all times and in every layout.

        Never blank. A status bar that can show no step is the requirement
        unmet, so the no-plan case says so rather than rendering nothing.
        """
        values = self._state
        plan = values.get("plan") or self.plan
        if values.get("phase") == "planning":
            step = "planning"
        elif plan:
            at = min(values.get("cursor", self.cursor), len(plan) - 1)
            step = f"⟨{at + 1}/{len(plan)}⟩ {panes.middle_out(plan[at], 40)}"
        else:
            step = "⟨no plan⟩"
        model = (settings.MODEL if settings.PROVIDER == "anthropic"
                 else settings.OPENAI_MODEL).split("/")[-1]
        # The thread id lives here now: the chat pane says what the chat is
        # about, and this is the line you read to resume it.
        bits = [self.thread, step, model, self.app.gate,
                f"{values.get('turns', 0)}/"
                f"{values.get('max_turns', settings.MAX_TURNS)}",
                # Against the budget, not alone: a bare 204,972 is a number
                # with nothing to compare it to, in a thread two messages from
                # being finished.
                _spend(values),
                values.get("verdict") or "running"]
        body = (self._stream.strip()[-160:] if self._stream.strip()
                else "  ·  ".join(bits))
        mark = f"{SPINNER[self._frame]} " if self._spin is not None else ""
        self.query_one("#status", Static).update(
            Text(f"{mark}{body}", style="row--muted"))


# ====================================================================== app

class NoesisApp(App):
    """The interface. One landing, one workspace per thread, two modals."""

    TITLE = "NOESIS"
    CSS_PATH = theme.STYLESHEET
    BINDINGS = [
        Binding("ctrl+q", "quit", "quit", priority=True),
        Binding("ctrl+t", "next_theme", "theme", priority=True),
        Binding("ctrl+g", "next_mode", "transparency", priority=True),
        Binding("ctrl+k", "setup", "setup", priority=True),
        Binding("shift+tab", "next_gate", "mode", priority=True),
        Binding("ctrl+n", "new_chat", "new chat", priority=True),
    ]

    def __init__(self, graph_app, goal: str | None = None,
                 thread: str | None = None, pane: str | None = None) -> None:
        # Registered AND selected here, not later: the stylesheet is parsed
        # before a theme is applied, so `$muted` is undefined otherwise.
        super().__init__(ansi_color=True)
        for one in theme.THEMES:
            self.register_theme(one)
        self.mode = theme.resolve_mode(settings.TUI_TRANSPARENT)
        # NOT `self.mode`: that is transparency. `auto` is refused from the
        # environment for the reason cli.start_mode gives.
        self.gate = cli.start_mode(None)
        theme.apply(self, settings.TUI_THEME, self.mode)
        self.graph = graph_app
        self._goal = goal
        self._thread = thread
        self._pane = pane

    def on_mount(self) -> None:
        if self._goal or self._thread or self._pane:
            self.open_workspace(goal=self._goal, thread=self._thread,
                                pane=self._pane)
        else:
            self.push_screen(LandingScreen())

    def dress(self, screen) -> None:
        """Put the transparency mode on the screen, which IS the CSS root."""
        for mode in theme.MODES:
            screen.remove_class(f"-{mode}")
        screen.add_class(f"-{self.mode}")

    def open_workspace(self, goal: str | None = None, thread: str | None = None,
                       pane: str | None = None, run: str | None = None) -> None:
        """SWITCH when a workspace is already open, push when one is not.

        Pushing every time stacked one screen per thread opened - each with its
        own transcript, panes and worker - and nothing ever popped one.
        """
        screen = WorkspaceScreen(thread or uuid.uuid4().hex[:8],
                                 goal=goal, pane=pane, run=run)
        if self.screen_stack and isinstance(self.screen, WorkspaceScreen):
            self.switch_screen(screen)
        else:
            self.push_screen(screen)

    def action_next_theme(self) -> None:
        theme.apply(self, theme.cycle(self.theme), self.mode)
        self.notify(f"theme {self.theme}", timeout=1)

    def action_next_mode(self) -> None:
        self.mode = theme.cycle_mode(self.mode)
        # The Rich table carries the bare-mode promotion too, so it is rebuilt.
        theme.apply(self, self.theme, self.mode)
        for screen in self.screen_stack:
            self.dress(screen)
        self.notify(f"transparency {self.mode}", timeout=1)

    def action_new_chat(self) -> None:
        self.open_workspace()

    def action_next_gate(self) -> None:
        """manual -> plan -> normal -> auto -> manual, for this session only."""
        order = policy.MODES
        self.gate = order[(order.index(self.gate) + 1) % len(order)]
        self.notify(f"mode {self.gate}"
                    + ("  -  nothing will pause" if self.gate == "auto" else ""),
                    timeout=2)
        for screen in self.screen_stack:
            if isinstance(screen, WorkspaceScreen):
                screen.paint_status()

    def action_setup(self) -> None:
        from agent.ui.setup import SetupScreen
        from agent import setup

        self.push_screen(SetupScreen(setup.current_key()))


def run(graph_app, goal: str | None = None, thread: str | None = None) -> int:
    """Start the interface. Ctrl+C is an exit, not a crash.

    Measured, not anticipated: interrupting a live session printed a traceback
    out of `mcp.shutdown()` joining its transport thread. The work is already
    checkpointed at that point, so the interrupt has cost nothing and must not
    look like it has.
    """
    try:
        NoesisApp(graph_app, goal, thread).run()
    except KeyboardInterrupt:
        pass
    return 0
