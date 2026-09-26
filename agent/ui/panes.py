"""One interface, eight implementations. What earns this module under CE-01.

A pane is a DISPOSABLE VIEW over state the workspace owns. Textual has no
reparent API, so any layout change rebuilds the container tree; that is only
correct if rebuilding is cheap, which means no pane may be the only place its
data lives. Every one of these repopulates itself from the screen in
`refresh_from`, and a rebuild during streaming is a defect (section 12.5).

Chrome comes from the `Pane` base and from noesis.tcss. The count sits in the
BOTTOM border rather than beside the title: textual gives one title per edge,
so `border_title` and `border_subtitle` are the two slots there are.
"""
from __future__ import annotations

import time

from rich.align import Align
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, RichLog, Static

from agent import cli

# The transcript is capped rather than unbounded: a rebuilt pane replays it
# from the screen, and replaying an unbounded list is how a split comes to
# take a visible pause.
MAX_LINES = 2000

# `◇` ran, `✓` mutated, `✕` denied or errored, `⠹` running. The glyph carries
# the verdict and the colour reinforces it - never colour alone.
GLYPHS = {"ran": "◇", "mutated": "✓", "denied": "✕", "running": "⠹"}


def tool_text(entry: dict) -> Text:
    """One tool call, in fixed columns so the eye can scan down them.

    A rich `Text` rather than console markup, and every log below has markup
    OFF, because `summary` carries arbitrary file content and shell arguments -
    a `[/]` in someone's code must render as a `[/]`, not close a tag.
    """
    if entry.get("verdict") == "deny" or entry.get("is_error"):
        kind, style = "denied", "row--denied"
    elif entry.get("mutated"):
        kind, style = "mutated", "row--mutated"
    else:
        kind, style = "ran", "row--ran"
    line = Text()
    line.append(f"{GLYPHS[kind]} ", style=style)
    line.append(f"{entry['tool']:<12}", style=style)
    line.append(f"{middle_out(entry.get('summary', ''), 42):<42}")
    line.append(f"{entry.get('duration_ms', 0) / 1000:>7.1f}s", style="row--muted")
    if entry.get("verdict") == "deny":
        line.append("  denied", style="row--denied")
    elif entry.get("is_error"):
        line.append("  error", style="row--denied")
    if entry.get("spill_path"):
        line.append("  spilled", style="row--muted")
    return line


def chat_name(goal: str | None, messages: list | None, width: int = 44) -> str:
    """What a chat is ABOUT, for its border title.

    The thread id is a serial number: it is what you type to resume one and it
    says nothing about which one this is. Kept in the status bar and /threads,
    where it is looked up, rather than on the pane, where it is read.

    HEAD truncation, not middle_out: that keeps both ends because a filename is
    the informative half of a path, and here the first words are.
    """
    text = " ".join(str(goal or "").split())
    for message in messages or []:
        if text:
            break
        # A tool RESULT is also role "user" and its content is a list of blocks,
        # so the first user message is not necessarily the one a person typed.
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            text = " ".join(message["content"].split())
    if not text:
        return "new"
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def middle_out(text: str, width: int) -> str:
    """Truncate the MIDDLE of a long path. The filename is the informative
    half, and lopping the tail off removes exactly that."""
    text = str(text).replace("\n", " ")
    if len(text) <= width:
        return text
    keep = width - 1
    head = keep // 2
    return f"{text[:head]}…{text[-(keep - head):]}"


class Pane(Vertical):
    """Border, title, count badge, focus styling, and one refresh hook."""

    pane_id = ""
    title = "pane"
    closeable = True
    can_focus = True

    def __init__(self) -> None:
        super().__init__(id=f"pane-{self.pane_id}")

    empty_line = ""

    def compose(self) -> ComposeResult:
        yield from self.body()
        yield Static(id="empty-line")

    def body(self) -> ComposeResult:
        """The pane's own contents. Everything else here is chrome."""
        return ()

    def on_mount(self) -> None:
        # Here rather than in the workspace's rebuild: `mount()` is async, so
        # a pane's own children do not exist until its Mount arrives.
        self.border_title = self.title
        self.setup()
        self.refresh_from(self.screen)

    def setup(self) -> None:
        """One-time widget preparation, before the first refresh."""

    def refresh_from(self, screen) -> None:
        """Repopulate from the workspace. Called on every rebuild and whenever
        the data behind this pane changes."""

    def count(self, value) -> None:
        self.border_subtitle = "" if value in (None, "") else f" {value} "

    def empty(self, on: bool, text: str = "") -> None:
        """One dim centred line saying what would appear here, never a blank
        box - an empty bordered rectangle reads as broken rather than idle.

        Its own WIDGET rather than a line written into the log: centring a
        rich renderable inside a RichLog centres it against the CONSOLE width,
        and a 46-cell pane then clipped the tail off it - measured, on screen.
        """
        line = self.query_one("#empty-line", Static)
        line.update(Align.center(Text(text or self.empty_line,
                                      style="row--muted")))
        line.display = on
        for child in self.children:
            if child.id != "empty-line":
                child.display = not on


class ChatPane(Pane):
    """The conversation: your turns, tool lines, and streamed replies."""

    pane_id = "chat"
    title = "chat"
    closeable = False              # section 8: alt+w refuses on this one
    empty_line = "say something below to begin"

    def body(self) -> ComposeResult:
        yield RichLog(id="chat-log", wrap=True, markup=False,
                      auto_scroll=True, max_lines=MAX_LINES)

    def refresh_from(self, screen) -> None:
        name = chat_name(screen._goal, screen._state.get("messages"))
        self.border_title = f"chat · {name}"
        log = self.query_one(RichLog)
        log.clear()
        for renderable in screen.transcript:
            log.write(renderable)
        self.empty(not screen.transcript)

    def append(self, renderable) -> None:
        """Append only. Never re-render the transcript to add a line.

        Silent before the log is mounted: `mount()` is async, and the pane
        replays the whole transcript from the screen on its own Mount, so an
        early line is not lost - it arrives once, in order.
        """
        if self.is_mounted:
            self.empty(False)
        for log in self.query(RichLog):
            log.write(renderable)


class PlanPane(Pane):
    """Every step; the active one marked, the done ones ticked."""

    pane_id = "plan"
    title = "plan"
    empty_line = "no plan for this thread"

    def body(self) -> ComposeResult:
        yield Static(id="plan-body")

    def refresh_from(self, screen) -> None:
        body = self.query_one("#plan-body", Static)
        self.empty(not screen.plan)
        if not screen.plan:
            self.count(None)
            return
        self.count(f"{min(screen.cursor, len(screen.plan) - 1) + 1}/{len(screen.plan)}")
        out = Text()
        for index, step in enumerate(screen.plan):
            if index < screen.cursor:
                out.append(f"✓ {step}\n", style="row--mutated")
            elif index == screen.cursor:
                out.append(f"▸ {step}\n", style="row--selected")
            else:
                out.append(f"  {step}\n", style="row--muted")
        body.update(out)


class TracePane(Pane):
    """One line per tool call. Enter opens its artifact."""

    pane_id = "trace"
    title = "trace"
    empty_line = "no tool has run yet"

    def body(self) -> ComposeResult:
        yield RichLog(id="trace-log", wrap=False, markup=False,
                      auto_scroll=True, max_lines=MAX_LINES)

    def refresh_from(self, screen) -> None:
        self.count(len(screen.trace_rows) or None)
        log = self.query_one(RichLog)
        log.clear()
        for entry in screen.trace_rows:
            log.write(tool_text(entry))
        self.empty(not screen.trace_rows)

    def append(self, entry: dict) -> None:
        if self.is_mounted:
            self.empty(False)
        for log in self.query(RichLog):
            log.write(tool_text(entry))


class TablePane(Pane):
    """The three panes that are a table over something the CLI already prints.

    Reading the same functions the flags read is the point: a screen and a flag
    that disagree about a status are two sources of truth for one fact.
    """

    columns: tuple = ()

    def setup(self) -> None:
        self.query_one(DataTable).add_columns(*self.columns)

    empty_line = "nothing here yet"

    def body(self) -> ComposeResult:
        yield DataTable(id=f"{self.pane_id}-table", cursor_type="row")

    def rows(self, screen) -> list[tuple]:
        return []

    def refresh_from(self, screen) -> None:
        table = self.query_one(DataTable)
        table.clear()
        rows = self.rows(screen)
        self.count(len(rows) or None)
        for key, cells in rows:
            table.add_row(*cells, key=key)
        self.empty(not rows)


class ThreadsPane(TablePane):
    """Past threads, newest first (FR-703). Enter resumes one in chat."""

    pane_id = "threads"
    title = "threads"
    columns = ("thread", "verdict", "turns", "goal")
    empty_line = "no threads yet"

    def rows(self, screen) -> list[tuple]:
        return [(row["id"], (row["id"], row["verdict"], str(row["turns"]),
                             middle_out(row["goal"], 44)))
                for row in cli._thread_rows(screen.app.graph)]


class TasksPane(TablePane):
    """The queue (FR-604). A task id IS a thread id, so Enter hands the same
    string chat already takes and no mapping exists to get wrong."""

    pane_id = "tasks"
    title = "tasks"
    columns = ("task", "status", "verdict", "goal")
    empty_line = "no tasks queued - submit one with --submit"
    BINDINGS = [Binding("c", "cancel", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static(id="task-detail")

    def rows(self, screen) -> list[tuple]:
        from agent import worker

        self.detail = {row["id"]: row.get("detail") or "" for row in worker.tasks()}
        return [(row["id"], (row["id"], row["status"], row["verdict"] or "-",
                             middle_out(row["goal"], 40)))
                for row in worker.tasks()]

    @on(DataTable.RowHighlighted)
    def _show(self, event: DataTable.RowHighlighted) -> None:
        """UR-16: what it answered, or refused, while nobody was watching."""
        chosen = str(event.row_key.value) if event.row_key else ""
        self.query_one("#task-detail", Static).update(
            Text(getattr(self, "detail", {}).get(chosen, ""), style="row--muted"))

    def action_cancel(self) -> None:
        from agent import worker

        table = self.query_one(DataTable)
        if not table.row_count:
            return
        chosen = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        self.notify(f"cancelled {chosen}" if worker.cancel(chosen)
                    else f"{chosen} is not queued or running")
        self.refresh_from(self.screen)


class SchedulesPane(TablePane):
    """Cron schedules, soonest first (FR-602). `d` removes the highlighted one.

    Section 8 asks for a confirm on that and there is none: the existing test
    presses `d` and expects it gone, and a modal in between would change a
    behaviour this build was told not to. Stated rather than quietly skipped.
    """

    pane_id = "schedules"
    title = "schedules"
    columns = ("schedule", "cron", "next run", "goal")
    empty_line = "no schedules - add one with --schedule"
    BINDINGS = [Binding("d", "remove", "remove", show=False)]

    def action_remove(self) -> None:
        from agent import worker

        table = self.query_one(DataTable)
        if not table.row_count:
            return
        chosen = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        self.notify(f"removed {chosen}" if worker.unschedule(chosen)
                    else f"no such schedule: {chosen}")
        self.refresh_from(self.screen)

    def rows(self, screen) -> list[tuple]:
        from agent import worker

        return [(row["id"], (row["id"], row["cron"],
                             time.strftime(cli.TIME_FMT,
                                           time.localtime(row["next_run"])),
                             middle_out(row["goal"], 32)))
                for row in worker.schedules()]


class DoctorPane(Pane):
    """Every precondition, each line ok or FAIL. Changes nothing.

    On a worker thread because `diagnose()` dials IMAP and SMTP: run inline it
    freezes the interface for two network round trips, and a doctor that looks
    hung is worse than no doctor.
    """

    pane_id = "doctor"
    title = "doctor"
    empty_line = "probing…"

    def body(self) -> ComposeResult:
        yield RichLog(id="doctor-log", wrap=True, markup=False)

    def refresh_from(self, screen) -> None:
        self.query_one(RichLog).clear()
        self.empty(True)
        self._probe()

    @work(thread=True, exclusive=True)
    def _probe(self) -> None:
        from agent import channel

        try:
            lines = channel.diagnose()
        except Exception as exc:                     # noqa: BLE001
            # A doctor that raises tells you nothing about what it was checking.
            lines = [f"FAIL  doctor: {type(exc).__name__}: {exc}"]
        self.app.call_from_thread(self._paint, lines)

    def _paint(self, lines: list[str]) -> None:
        self.empty(False)
        log = self.query_one(RichLog)
        log.clear()
        self.count(sum(1 for line in lines if line.startswith("FAIL")) or None)
        for line in lines:
            log.write(doctor_text(line))


class ArtifactPane(Pane):
    """A spilled `.agent/artifacts/` file, so a truncated result can be read
    without leaving the conversation that produced it."""

    pane_id = "artifact"
    title = "artifact"
    empty_line = "select a spilled result in trace"

    def body(self) -> ComposeResult:
        yield RichLog(id="artifact-log", wrap=False, markup=False,
                      max_lines=MAX_LINES)

    def refresh_from(self, screen) -> None:
        log = self.query_one(RichLog)
        log.clear()
        path = screen.artifact
        self.empty(not path)
        if not path:
            self.border_title = "artifact"
            return
        self.border_title = f"artifact · {path.name}"
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.write(Text(f"could not read it: {exc}", style="row--denied"))
            return
        for line in body.splitlines()[:MAX_LINES]:
            log.write(Text(line))


def doctor_text(line: str) -> Text:
    """One diagnostic line, coloured by its verdict rather than its wording.

    `diagnose()` returns strings that already begin ok / FAIL / --, so the
    marker is the only thing read here; the text after it is free-form.
    """
    style = ("row--denied" if line.startswith("FAIL")
             else "row--muted" if line.startswith("--") else "row--ran")
    return Text(line, style=style)


PANES = {pane.pane_id: pane for pane in (
    ChatPane, PlanPane, TracePane, ThreadsPane, TasksPane, SchedulesPane,
    DoctorPane, ArtifactPane)}
