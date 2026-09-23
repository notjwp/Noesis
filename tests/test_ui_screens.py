"""The landing screen, the tiled workspace, and the shim that keeps --tui.

Folded into the tenth deviation with the rest of the interface. What is here
and not in test_tui.py is what test_tui.py was never about: the wordmark, the
layout rules, the keymap, and the landing's refusal to claim a condition
nobody checked.
"""
import asyncio
import os

import pytest
from textual.widgets import Input, Static

from agent import config
from agent.ui import panes, screens, theme, tiling


class Values:
    def __init__(self, values):
        self.values = values


class FakeGraph:
    def __init__(self, values=None):
        self._values = values or {}

    def get_state(self, cfg):
        return Values(dict(self._values))

    def invoke(self, payload, cfg):
        return {}


def drive(app, script, size=(110, 32)):
    async def _go():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await script(pilot)
    asyncio.run(_go())


def rendered(app) -> str:
    return "\n".join(str(strip.text)
                     for strip in app.screen._compositor.render_strips())


# ================================================================ the wordmark

def test_all_six_rows_are_exactly_46_cells():
    """The failure mode of every ASCII logotype is a ragged row, and it is
    invisible in a diff."""
    from rich.cells import cell_len

    assert len(screens.LOGO) == 6
    for row in screens.LOGO:
        assert len(row) == 46, repr(row)
        assert cell_len(row) == screens.LOGO_WIDTH, repr(row)


def test_the_wordmark_is_the_artwork_and_not_a_regeneration():
    """A golden test. Deliberately NOT a "no double space" check: E's short arm
    and N's diagonal contain doubled spaces legitimately, and such a check
    fails on correct art."""
    assert screens.LOGO == (
        "███╗   ██╗ ██████╗ ███████╗███████╗██╗███████╗",
        "████╗  ██║██╔═══██╗██╔════╝██╔════╝██║██╔════╝",
        "██╔██╗ ██║██║   ██║█████╗  ███████╗██║███████╗",
        "██║╚██╗██║██║   ██║██╔══╝  ╚════██║██║╚════██║",
        "██║ ╚████║╚██████╔╝███████╗███████║██║███████║",
        "╚═╝  ╚═══╝ ╚═════╝ ╚══════╝╚══════╝╚═╝╚══════╝",
    )


def test_a_narrow_terminal_gets_the_letterspaced_form_instead():
    assert "N O E S I S" in screens.logotype(40).plain
    assert "███" in screens.logotype(80).plain


def test_the_wordmark_is_one_colour_and_never_a_fade():
    """The accent means focus everywhere else, so a gradient here would make it
    mean two things."""
    art = screens.logotype(80)
    assert {span.style for span in art.spans} == {"row--logo"}


def test_every_palette_separates_a_pane_from_the_ground_behind_it():
    """Reported twice from the TUI: panes invisible against the background.

    The mechanism was never wrong - `Pane { background: $surface }` painted
    exactly what it was told. tokyo-night set surface 4/5/8 points off its own
    background and mono 8/9/10, which no display resolves. This is the guard,
    because the next palette added would have had nothing to check it against.
    """
    from textual.color import Color

    for one in (theme.MONO, theme.MOCHA, theme.TOKYO):
        ground = Color.parse(one.background)
        raised = Color.parse(one.surface)
        apart = sum(abs(a - b) for a, b in
                    zip((ground.r, ground.g, ground.b), (raised.r, raised.g, raised.b)))
        assert apart >= 40, f"{one.name}: surface is {apart} from background"


# ================================================================== the landing

def test_the_landing_shows_the_wordmark_and_every_command():
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        text = rendered(app)
        assert "██████╗" in text
        for name in screens.COMMANDS:
            assert name in text, name

    drive(app, script)


def test_a_count_that_has_not_arrived_renders_as_a_placeholder():
    """Section 12.1: nothing touches SQLite on the render path. The landing is
    drawn first and the counts arrive after, so the render path must have
    something to show while they have not."""
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        await app.screen.workers.wait_for_complete()
        app.screen.counts = {}
        app.screen.paint_list()
        await pilot.pause()
        assert "·" in rendered(app)

    drive(app, script)


def test_an_empty_queue_is_not_the_same_as_a_count_nobody_asked_for():
    """`·` means "not loaded yet". A queue with nothing in it is a different
    statement, and rendering both the same way says neither."""
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        await app.screen.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen.counts.get("/tasks") == ""

    drive(app, script)


def test_the_landing_omits_egress_when_nothing_set_it(monkeypatch):
    """A default that asserts the safe answer is how a row comes to claim a
    condition nobody checked. AGENT_EGRESS unset means the field is ABSENT,
    not `unknown`."""
    monkeypatch.delenv("AGENT_EGRESS", raising=False)
    monkeypatch.setenv("AGENT_API_KEY", "nvapi-x" * 4)
    line = screens.status_line().plain
    assert "egress" not in line
    assert "unknown" not in line


def test_the_landing_prints_egress_only_when_it_was_actually_set(monkeypatch):
    monkeypatch.setenv("AGENT_EGRESS", "restricted")
    monkeypatch.setenv("AGENT_API_KEY", "nvapi-x" * 4)
    assert "restricted egress" in screens.status_line().plain


def test_no_api_key_replaces_the_whole_line(monkeypatch):
    for name in ("AGENT_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_PROVIDER", "nvidia")
    line = screens.status_line()
    assert "no API key" in line.plain and "/setup" in line.plain
    assert line.style == "row--denied"


def test_the_model_shown_is_the_one_actually_resolved(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "nvapi-x" * 4)
    monkeypatch.setattr(config, "PROVIDER", "anthropic")
    monkeypatch.setattr(config, "MODEL", "claude-haiku-4-5-20251001")
    assert "claude-haiku-4-5-20251001" in screens.status_line().plain


def test_typing_filters_the_list_and_the_selection_snaps_to_the_first_match():
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        app.screen.index = 3
        app.screen.query_one(Input).value = "/ta"
        await pilot.pause()
        assert app.screen.matches() == ["/tasks"]
        assert app.screen.index == 0

    drive(app, script)


def test_a_goal_typed_on_the_landing_opens_a_workspace():
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        app.screen.query_one(Input).value = "fix the failing test"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, screens.WorkspaceScreen)
        assert app.screen._goal == "fix the failing test"

    drive(app, script)


def test_a_command_on_the_landing_opens_the_workspace_with_its_pane():
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        app.screen.query_one(Input).value = "/tasks"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert "tasks" in tiling.leaves(app.screen.tiles)

    drive(app, script)


def test_a_mistyped_command_is_not_sent_as_a_goal():
    """`/taks` is a typo. Sending it to the model would bill a turn for it and
    lose the correction."""
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        app.screen.query_one(Input).value = "/taks"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, screens.LandingScreen)

    drive(app, script)


def test_a_sentence_beginning_with_a_slash_is_still_a_goal():
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        app.screen.query_one(Input).value = "/usr/bin/python is missing"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, screens.WorkspaceScreen)

    drive(app, script)


@pytest.mark.parametrize("height,hint,status", [
    (32, True, True), (19, False, True), (17, False, False)])
def test_the_landing_sheds_rows_before_it_scrolls(height, hint, status):
    """The hint goes first, then the status line. The command list is the one
    thing that must never be clipped."""
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        assert app.screen.query_one("#landing-hint").display is hint
        assert app.screen.query_one("#landing-status").display is status
        for name in screens.COMMANDS:
            assert name in rendered(app), name

    drive(app, script, size=(110, height))


# ================================================================ the workspace

def workspace(values=None, **kw):
    return screens.NoesisApp(FakeGraph(values), thread="w0rk", **kw)


def test_the_workspace_opens_on_chat_alone():
    app = workspace()

    async def script(pilot):
        assert tiling.leaves(app.screen.tiles) == ["chat"]

    drive(app, script)


def test_alt_enter_splits_and_alt_w_closes():
    app = workspace()

    async def script(pilot):
        await pilot.press("alt+enter")
        await pilot.pause()
        assert len(tiling.leaves(app.screen.tiles)) == 2
        await pilot.press("alt+w")
        await pilot.pause()
        assert tiling.leaves(app.screen.tiles) == ["chat"]

    drive(app, script)


def test_chat_cannot_be_closed():
    """Section 8. Closing the conversation would leave a workspace with no way
    to say anything."""
    app = workspace()

    async def script(pilot):
        await pilot.press("alt+enter")
        await pilot.pause()
        app.screen.focus_id = "chat"
        await pilot.press("alt+w")
        await pilot.pause()
        assert "chat" in tiling.leaves(app.screen.tiles)

    drive(app, script)


def test_alt_z_zooms_one_pane_and_escape_restores_the_layout():
    app = workspace()

    async def script(pilot):
        await pilot.press("alt+enter")
        await pilot.pause()
        before = app.screen.tiles
        await pilot.press("alt+z")
        await pilot.pause()
        assert app.screen.zoomed == app.screen.focus_id
        assert app.screen.showing() == [app.screen.focus_id]
        # Zoom is NOT in the tree, which is why the tree survives it untouched.
        assert app.screen.tiles is before
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.zoomed is None
        assert len(app.screen.showing()) == 2

    drive(app, script)


def test_alt_hjkl_moves_focus_geometrically():
    app = workspace()

    async def script(pilot):
        await pilot.press("alt+enter")
        await pilot.pause()
        opened = app.screen.focus_id
        await pilot.press("alt+h")
        await pilot.pause()
        assert app.screen.focus_id == "chat"
        await pilot.press("alt+l")
        await pilot.pause()
        assert app.screen.focus_id == opened

    drive(app, script)


def test_a_resize_changes_the_ratio_and_not_the_panes():
    app = workspace()

    async def script(pilot):
        await pilot.press("alt+enter")
        await pilot.pause()
        before = tiling.leaves(app.screen.tiles)
        await pilot.press("alt+L")
        await pilot.pause()
        assert app.screen.tiles.ratio != pytest.approx(0.5)
        assert tiling.leaves(app.screen.tiles) == before

    drive(app, script)


def test_streaming_never_rebuilds_the_layout():
    """Section 12.5. A rebuild per token is the difference between a terminal
    that streams and one that stutters."""
    app = workspace()

    async def script(pilot):
        before = app.screen.rebuilds
        for _ in range(200):
            app.screen.on_text("token ")
        await pilot.pause()
        assert app.screen.rebuilds == before

    drive(app, script)


def test_a_rebuilt_pane_replays_from_the_screen_not_from_itself():
    """Panes are disposable views. If a rebuilt one came back empty, every
    split would silently eat the conversation."""
    app = workspace()

    async def script(pilot):
        app.screen.note("something worth keeping")
        await pilot.pause()
        await pilot.press("alt+enter")
        await pilot.pause()
        await pilot.pause()
        log = app.screen.query_one("#chat-log")
        assert any("something worth keeping" in strip.text for strip in log.lines)

    drive(app, script)


@pytest.mark.parametrize("values,expected", [
    ({}, "no plan"),
    ({"phase": "planning"}, "planning"),
    ({"plan": ["read it", "fix it"], "cursor": 1}, "2/2"),
])
def test_the_status_bar_always_names_a_step(values, expected):
    """FR-702. A status bar that can show no step is the requirement unmet."""
    app = workspace(values)

    async def script(pilot):
        node = app.screen.query_one("#status", Static)
        assert expected in getattr(node.content, "plain", str(node.content))

    drive(app, script)


def test_typing_always_reaches_the_agent_even_from_another_pane():
    """This asserted that the composer NEVER yields focus, which is why
    `focus_id` was only a border colour: arrows moved the text cursor, a
    table's row cursor could not move, and Enter submitted the composer
    instead of resuming a thread. Reported from the TUI.

    The property it was protecting is kept, by a different route - a printable
    key hands focus back and is not lost."""
    app = workspace()

    async def script(pilot):
        assert app.screen.focused is app.screen.query_one("#composer")
        await pilot.press("alt+enter")
        await pilot.pause()
        # Focus follows the active pane now, so the split pane has the keys.
        assert app.screen.focused is not app.screen.query_one("#composer")

        await pilot.press("h")
        await pilot.pause()
        composer = app.screen.query_one("#composer")
        assert app.screen.focused is composer
        assert composer.value == "h", "the keystroke must not be swallowed"

    drive(app, script)


def test_the_pane_keys_are_not_swallowed_by_the_composer():
    """They are declared priority for exactly this reason, and an Input that
    ate alt+w would leave the layout unusable."""
    app = workspace()

    async def script(pilot):
        await pilot.press("alt+enter")
        await pilot.pause()
        assert app.screen.query_one("#composer", Input).value == ""

    drive(app, script)


# ================================================================ themes live

def test_ctrl_t_recolours_without_a_restart():
    app = workspace()

    async def script(pilot):
        seen = [app.theme]
        for _ in range(3):
            await pilot.press("ctrl+t")
            await pilot.pause()
            seen.append(app.theme)
        assert seen == ["noesis-mono", "catppuccin-mocha", "tokyo-night",
                        "noesis-mono"]
        assert app.ansi_color is True

    drive(app, script)


def test_ctrl_g_cycles_transparency_on_the_live_screen():
    app = workspace()

    async def script(pilot):
        seen = [app.mode]
        for _ in range(3):
            await pilot.press("ctrl+g")
            await pilot.pause()
            seen.append(app.mode)
            assert app.screen.has_class(f"-{app.mode}")
        assert seen == ["gaps", "bare", "opaque", "gaps"]

    drive(app, script)


@pytest.mark.parametrize("mode,bare", [("opaque", False), ("gaps", True),
                                       ("bare", True)])
def test_each_mode_paints_what_it_promises(mode, bare):
    app = workspace()
    app.mode = mode

    async def script(pilot):
        seen = set()
        for strip in app.screen._compositor.render_strips():
            for segment in strip:
                colour = segment.style.bgcolor if segment.style else None
                seen.add("default" if colour is None else colour.name)
        assert ("default" in seen) is bare
        assert seen - {"default"}, "nothing was painted at all"

    drive(app, script)


def test_no_colour_degrades_to_sixteen_colour_ansi():
    """Section 10.1's other half: under ansi_color=True an UNSET colour becomes
    a terminal ANSI colour. Measured once already - an unstyled scrollbar came
    out solid black in all three modes."""
    for mode in theme.MODES:
        app = workspace()
        app.mode = mode

        async def script(pilot, app=app, mode=mode):
            bad = set()
            for strip in app.screen._compositor.render_strips():
                for segment in strip:
                    for colour in ((segment.style.bgcolor,
                                    segment.style.color)
                                   if segment.style else ()):
                        if colour and (colour.name.startswith("color(")
                                       or colour.name == "#000000"):
                            bad.add(colour.name)
            assert not bad, f"{mode}: {sorted(bad)}"

        drive(app, script)


# ================================================================== the shim

def test_the_old_module_path_still_works():
    """`cli.py:495` imports `agent.tui` and calls `run(app, goal=, thread=)`.
    Nothing below the interface moved, so neither did that."""
    import inspect

    from agent import tui

    assert tui.run is screens.run
    assert tui.NoesisApp is screens.NoesisApp
    assert set(inspect.signature(tui.run).parameters) == {"graph_app", "goal",
                                                          "thread"}


def test_the_package_does_not_import_textual_to_be_imported():
    """NFR-602: `agent.ui.tiling` and `agent.setup` must stay reachable on a
    machine with no interface installed, so the app export is lazy."""
    import pathlib
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'textual' or name.startswith('textual.'):\n"
        "            raise ImportError('textual is not installed')\n"
        "sys.meta_path.insert(0, Block())\n"
        "import agent.ui\n"
        "from agent.ui import tiling\n"
        "assert 'textual' not in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=root,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_every_pane_the_spec_names_exists():
    assert set(panes.PANES) == {"chat", "plan", "trace", "threads", "tasks",
                                "schedules", "doctor", "artifact"}


def test_a_long_path_loses_its_middle_and_keeps_its_filename():
    """The filename is the informative half; lopping the tail removes it."""
    out = panes.middle_out("agent/ui/very/deep/nested/context.py", 20)
    assert out.endswith("context.py") and out.startswith("agent")
    assert len(out) == 20


def test_the_tasks_badge_says_which_state_the_queue_is_in():
    """Section 5.4. Written and never rendered would be dead state, and a
    queue with something awaiting approval must be visible from the landing."""
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        # The count worker is still in flight and would overwrite this.
        await app.screen.workers.wait_for_complete()
        await pilot.pause()
        app.screen.counted({"/tasks": "2"}, "error")
        await pilot.pause()
        assert "●" in rendered(app)
        app.screen.counted({"/tasks": "2"}, "")
        await pilot.pause()
        assert "●" not in rendered(app)

    drive(app, script)


def test_ctrl_p_opens_the_command_palette():
    """Textual's own, bound by default - a second implementation would be a
    second list of commands to keep in step."""
    app = workspace()

    async def script(pilot):
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert type(app.screen).__name__ == "CommandPalette"

    drive(app, script)


@pytest.mark.parametrize("mode", theme.MODES)
def test_the_chrome_stays_painted_in_every_mode(mode):
    """Section 10.3: the composer and the status bar must never be hard to read
    over someone's wallpaper. FR-702's step lives in that bar, so the bar losing
    its background takes the requirement with it.

    Checked on the WORKSPACE and not a stand-in: the phase-3 theme test uses its
    own host, so making the real chrome transparent passed it.
    """
    app = workspace()
    app.mode = mode

    async def script(pilot):
        strips = app.screen._compositor.render_strips()
        for name in ("#composer", "#status"):
            row = app.screen.query_one(name).region.y
            # EVERY cell, not any: the Input's cursor is painted on its own, so
            # "something on this row has a background" passes on a bare bar.
            bare = sum(len(segment.text) for segment in strips[row]
                       if not segment.style or segment.style.bgcolor is None
                       or segment.style.bgcolor.name == "default")
            assert bare == 0, f"{mode}: {name} has {bare} unpainted cells"

    drive(app, script)


def live_timers(node) -> list:
    """Every REPEATING timer under a widget that is actually ticking.

    One-shots are excluded on purpose: textual sets one to un-highlight a
    pressed Button, and it fires once and dies. Section 11 forbids a timer
    running while nothing MOVES, which is an interval, not a self-terminating
    animation - `_repeat = 0` is one shot, `None` is forever.
    """
    out = []
    for widget in [node, *node.query("*")]:
        for timer in getattr(widget, "_timers", ()) or ():
            active = getattr(timer, "_active", None)
            if timer._repeat == 0 or active is None or not active.is_set():
                continue
            out.append(f"{type(widget).__name__}.{timer.name}")
    return out


@pytest.mark.parametrize("kw", [{}, {"thread": "t"}], ids=["landing", "workspace"])
def test_nothing_ticks_while_nothing_is_moving(kw):
    """Section 11 and section 17: idle CPU is 0%.

    MEASURED and it was not: an Input blinks its cursor on a live interval for
    as long as it has focus, and the composer holds focus for the whole session.
    """
    app = screens.NoesisApp(FakeGraph(), **kw)

    async def script(pilot):
        await app.screen.workers.wait_for_complete()
        await pilot.pause()
        assert live_timers(app.screen) == []

    drive(app, script)


def test_a_split_starts_no_timer_either():
    app = workspace()

    async def script(pilot):
        await pilot.press("alt+enter")
        await pilot.pause()
        await pilot.press("alt+z")
        await pilot.pause()
        assert live_timers(app.screen) == []

    drive(app, script)


class Counting(FakeGraph):
    """A graph that says how often the checkpoint database was read."""

    def __init__(self, values=None):
        super().__init__(values)
        self.reads = 0

    def get_state(self, cfg):
        self.reads += 1
        return super().get_state(cfg)


def test_streaming_never_touches_the_checkpoint_database():
    """Section 12.1: nothing touches disk or SQLite on the render path.
    MEASURED at 200 reads for 200 tokens before the state was cached."""
    graph = Counting()
    app = screens.NoesisApp(graph, thread="t")

    async def script(pilot):
        before = graph.reads
        for _ in range(200):
            app.screen.on_text("token ")
        await pilot.pause()
        assert graph.reads == before, f"{graph.reads - before} reads while streaming"

    drive(app, script)


def test_a_tool_line_does_not_reread_the_state_either():
    graph = Counting()
    app = screens.NoesisApp(graph, thread="t")

    async def script(pilot):
        before = graph.reads
        for _ in range(20):
            app.screen.on_trace({"kind": "tool", "tool": "read_file",
                                 "summary": "x.py", "duration_ms": 5})
        await pilot.pause()
        assert graph.reads == before

    drive(app, script)


def test_a_turn_boundary_DOES_reread_it():
    """The cache would be a bug of its own if nothing ever refreshed it: turns,
    tokens and the verdict all move at a turn boundary."""
    graph = Counting()
    app = screens.NoesisApp(graph, thread="t")

    async def script(pilot):
        before = graph.reads
        app.screen.on_trace({"kind": "terminal", "verdict": "done", "turns": 2,
                             "spent_tokens": 91})
        await pilot.pause()
        assert graph.reads > before

    drive(app, script)


def test_opening_a_thread_replaces_the_workspace_rather_than_stacking():
    """Each workspace holds a transcript, panes and a worker, and nothing ever
    popped one. MEASURED: five threads left six screens on the stack."""
    app = screens.NoesisApp(FakeGraph(), thread="t")

    async def script(pilot):
        depth = len(app.screen_stack)
        for index in range(5):
            app.open_workspace(thread=f"thread{index}")
            await pilot.pause()
        assert len(app.screen_stack) == depth
        assert app.screen.thread == "thread4"

    drive(app, script)


def test_the_landing_still_pushes_rather_than_replacing_itself():
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        depth = len(app.screen_stack)
        app.open_workspace(goal="do a thing")
        await pilot.pause()
        assert len(app.screen_stack) == depth + 1

    drive(app, script)


def test_the_status_bar_spins_only_while_the_graph_is_working():
    """A turn takes tens of seconds. The old TUI said "working..."; dropping it
    left the screen looking hung. The interval exists only while the graph does."""
    app = workspace()

    async def script(pilot):
        assert live_timers(app.screen) == []
        app.screen.spin(True)
        await pilot.pause()
        assert live_timers(app.screen) != []
        node = app.screen.query_one("#status", Static)
        shown = getattr(node.content, "plain", str(node.content))
        assert shown[0] in screens.SPINNER
        app.screen.spin(False)
        await pilot.pause()
        assert live_timers(app.screen) == [], "the spinner outlived the work"

    drive(app, script)


def test_a_finished_run_leaves_no_spinner_behind():
    graph = FakeGraph()
    app = screens.NoesisApp(graph, goal="do a thing", thread="t")

    async def script(pilot):
        await app.screen.workers.wait_for_complete()
        await pilot.pause()
        assert live_timers(app.screen) == []

    drive(app, script)


# ===================================================== ask_user, on this surface

def test_the_approval_modal_offers_allow_for_the_session():
    """The CLI's [s]ession keystroke, as a third button. It answers "session",
    which the run loop treats as allow AND remembers - the modal itself stays
    a question with one answer, like the CLI prompt."""
    from textual.widgets import Button

    from agent.ui.modals import ApprovalScreen

    class Asked(screens.NoesisApp):
        def __init__(self):
            super().__init__(FakeGraph(), thread="t")
            self.answer = "UNSET"

    app = Asked()
    payload = {"call": {"id": "t1", "name": "run_shell", "input": {"command": "rm -rf build"}},
               "reason": "run_shell is destructive (recursive delete)"}

    async def script(pilot):
        app.push_screen(ApprovalScreen(payload),
                        callback=lambda r: setattr(app, "answer", r))
        await pilot.pause()
        app.screen.query_one("#session", Button).press()
        await pilot.pause()

    drive(app, script)
    assert app.answer == "session"


def test_a_remembered_rule_does_not_open_the_modal_again():
    """cli.remembered() is the same memory the CLI uses, so an allow given in
    the TUI holds in the CLI for the rest of the process and vice versa."""
    from agent import cli

    cli.forget_approvals()
    payload = {"call": {"id": "t1", "name": "run_shell", "input": {"command": "rm -rf build"}},
               "reason": "run_shell is destructive (recursive delete)"}
    assert cli.remembered(payload) is False
    cli.remember(payload)
    assert cli.remembered(payload) is True
    other = {**payload, "reason": "run_shell is destructive (force push)"}
    assert cli.remembered(other) is False


def test_the_ask_modal_returns_what_was_typed():
    from agent.ui.modals import AskScreen

    class Asked(screens.NoesisApp):
        def __init__(self):
            super().__init__(FakeGraph(), thread="t")
            self.answer = "UNSET"

    app = Asked()

    async def script(pilot):
        app.push_screen(AskScreen("which file?", ["alpha", "beta"]),
                        callback=lambda r: setattr(app, "answer", r))
        await pilot.pause()
        app.screen.query_one("#ask-answer", Input).value = "beta"
        await pilot.press("enter")
        await pilot.pause()

    drive(app, script)
    assert app.answer == "beta"


def test_a_number_picks_that_choice():
    from agent.ui.modals import AskScreen

    app = screens.NoesisApp(FakeGraph(), thread="t")
    app.answer = "UNSET"

    async def script(pilot):
        app.push_screen(AskScreen("which?", ["alpha", "beta", "gamma"]),
                        callback=lambda r: setattr(app, "answer", r))
        await pilot.pause()
        app.screen.query_one("#ask-answer", Input).value = "3"
        await pilot.press("enter")
        await pilot.pause()

    drive(app, script)
    assert app.answer == "gamma"


def test_skipping_the_question_costs_a_guess_not_the_run():
    """Escape is not a refusal here - there is nothing to refuse. It returns ""
    and the TOOL turns that into its own use-your-best-judgement text."""
    from agent.ui.modals import AskScreen

    app = screens.NoesisApp(FakeGraph(), thread="t")
    app.answer = "UNSET"

    async def script(pilot):
        app.push_screen(AskScreen("which?", ["alpha"]),
                        callback=lambda r: setattr(app, "answer", r))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    drive(app, script)
    assert app.answer == ""


def test_the_workspace_hands_the_tool_a_way_to_ask():
    """The hook is what makes ask_user work here rather than answer "nobody is
    there". Unset, every question would be a guess."""
    from agent import tools

    app = screens.NoesisApp(FakeGraph(), goal="do a thing", thread="t")

    async def script(pilot):
        await app.screen.workers.wait_for_complete()
        await pilot.pause()
        assert tools.ASK == app.screen.ask

    drive(app, script)


# ============================================== dragging a divider with a mouse

def test_the_gap_between_two_panes_is_where_the_divider_is():
    tree = tiling.Split("v", 0.5, tiling.Leaf("chat"), tiling.Leaf("threads"))
    boxes = tiling.rects(tree, 100, 30)
    found = tiling.dividers(tree, 100, 30)

    assert len(found) == 1
    gap = found[0].box
    # Exactly the cells between the two panes - not inside either of them.
    assert gap.x == boxes["chat"].x + boxes["chat"].w
    assert gap.x + gap.w == boxes["threads"].x


def test_a_one_cell_gap_is_grabbable_from_either_side():
    """A single column is a hard thing to hit with a mouse, so a cell either
    side counts. Without this the feature is technically present and unusable."""
    tree = tiling.Split("v", 0.5, tiling.Leaf("chat"), tiling.Leaf("threads"))

    for x in (48, 49, 50):
        assert tiling.divider_at(tree, x, 10, 100, 30) is not None
    assert tiling.divider_at(tree, 20, 10, 100, 30) is None


def test_a_nested_divider_is_not_shadowed_by_its_parent():
    tree = tiling.Split("v", 0.5, tiling.Leaf("chat"),
                        tiling.Split("h", 0.5, tiling.Leaf("threads"),
                                     tiling.Leaf("tasks")))
    inner = tiling.divider_at(tree, 75, 14, 100, 30)

    assert inner is not None
    assert inner.leaves == ("threads", "tasks")


def test_a_drag_cannot_make_a_pane_the_keys_would_refuse():
    """The same MIN/MAX_RATIO the keyboard clamps to. A mouse that could drag a
    pane to nothing would be a second set of rules for one layout."""
    tree = tiling.Split("v", 0.5, tiling.Leaf("chat"), tiling.Leaf("threads"))
    div = tiling.dividers(tree, 100, 30)[0]

    assert tiling.ratio_at(div, -40, 10) == tiling.MIN_RATIO
    assert tiling.ratio_at(div, 400, 10) == tiling.MAX_RATIO


def test_set_ratio_returns_the_same_tree_when_nothing_moved():
    """Identity is what lets the drag handler skip the widget write."""
    tree = tiling.Split("v", 0.5, tiling.Leaf("chat"), tiling.Leaf("threads"))
    div = tiling.dividers(tree, 100, 30)[0]

    assert tiling.set_ratio(tree, div, 0.5) is tree
    assert tiling.set_ratio(tree, div, 0.7) is not tree


def test_dragging_resizes_without_remounting_the_tree():
    """A rebuild per mouse-move would drop the transcript and the focus, so the
    drag writes the two `fr` weights `build` already sets."""
    app = workspace()

    async def script(pilot):
        await pilot.press("alt+enter")
        await pilot.pause()
        assert len(tiling.leaves(app.screen.tiles)) == 2
        screen = app.screen
        rebuilds, before = screen.rebuilds, screen.tiles.ratio

        div = tiling.dividers(screen.tiles, *screen.area())[0]
        await pilot.mouse_down(screen, offset=(div.box.x, div.box.y + 2))
        await pilot.pause()
        assert screen.dragging is not None

        # Pilot has no mouse_move; `hover` posts the MouseMove, which the
        # capture routes to the screen exactly as a real drag would.
        await pilot.hover(screen, offset=(div.box.x - 12, div.box.y + 2))
        await pilot.pause()
        await pilot.mouse_up(screen, offset=(div.box.x - 12, div.box.y + 2))
        await pilot.pause()

        assert screen.dragging is None
        assert screen.tiles.ratio != before, "the drag moved nothing"
        assert screen.rebuilds == rebuilds, "a drag must not re-mount"

    drive(app, script)


def test_opening_threads_by_name_hands_it_the_keys(monkeypatch):
    """The reported case, on the path actually used to reach it. `/threads`
    split the pane in and left `chat` active, so the composer kept the keys:
    every thread listed, the row cursor could not move, and Enter submitted the
    composer instead of resuming. Focusing the pane on move was not enough -
    nothing moved."""
    from textual.widgets import DataTable

    from agent import cli
    rows = [{"id": "aaaa1111", "verdict": "done", "turns": 0, "goal": "one"},
            {"id": "bbbb2222", "verdict": "done", "turns": 2, "goal": "two"}]
    monkeypatch.setattr(cli, "_thread_rows", lambda graph: rows)

    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        app.screen.query_one(Input).value = "/threads"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert screen.focus_id == "threads"
        assert isinstance(screen.focused, DataTable)

        table = screen.query_one("#threads-table", DataTable)
        await pilot.press("down")
        await pilot.pause()
        assert table.cursor_row == 1, "the arrow key must move the row cursor"

        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert app.screen.thread == "bbbb2222", "Enter must resume that row"

    drive(app, script)


# ============================================================ /exit (2026-09-09)

def test_exit_is_one_of_the_listed_commands():
    """COMMANDS is the ONLY list: the suggester completes from it, command()
    dispatches on it, and /help prints it. A command missing here is a command
    that cannot be typed."""
    assert "/exit" in screens.COMMANDS
    assert "/exit" in screens.help_text().plain


def test_exit_typed_in_a_session_closes_the_app():
    app = workspace()

    async def script(pilot):
        left = []
        app.exit = lambda *a, **k: left.append(True)

        box = app.screen.query_one("#composer", Input)
        box.value = "/exit"
        await pilot.press("enter")
        await pilot.pause()

        assert left, "/exit did not close the app"
        assert box.value == "", "the command must not be left in the composer"

    drive(app, script)


def test_exit_typed_on_the_landing_closes_the_app():
    """The landing has its own dispatcher, and its fallback OPENS a workspace -
    so an unhandled /exit there would start a session instead of ending one."""
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        left = []
        app.exit = lambda *a, **k: left.append(True)

        app.screen.query_one(Input).value = "/exit"
        await pilot.press("enter")
        await pilot.pause()

        assert left, "/exit did not close the app"
        assert isinstance(app.screen, screens.LandingScreen), "it opened a session"

    drive(app, script)


def test_exit_is_not_treated_as_a_goal():
    """`/exit` must never reach the model. A slash command that falls through to
    begin() bills a turn for the word 'exit'."""
    app = workspace()

    async def script(pilot):
        app.exit = lambda *a, **k: None
        started = []
        app.screen.begin = lambda text: started.append(text)

        app.screen.query_one("#composer", Input).value = "/exit"
        await pilot.press("enter")
        await pilot.pause()

        assert started == [], "/exit was sent to the model"

    drive(app, script)


# ===================================== the rest of the commands (2026-09-10)

def test_every_pane_that_exists_can_be_opened_by_typing():
    """Three panes - plan, trace, artifact - were reachable only by splitting
    into whatever alt+enter happened to pick. A pane with no command is a pane
    most people never find."""
    openable = set(screens.OPENS.values())
    missing = set(panes.PANES) - openable - {"chat"}

    assert not missing, f"panes with no command: {sorted(missing)}"


def test_the_command_list_and_the_pane_map_agree():
    """COMMANDS is the only list - the suggester, the dispatcher and /help all
    read it. An OPENS entry with no COMMANDS entry is a pane nothing can reach."""
    assert set(screens.OPENS) <= set(screens.COMMANDS)


def test_review_queues_when_something_needs_attention(monkeypatch):
    app = workspace()

    async def script(pilot):
        from agent import worker

        monkeypatch.setattr(worker, "review", lambda: "task-123")
        app.screen.command("/review")
        await pilot.pause()

        said = " ".join(str(r) for r in app.screen.transcript)
        assert "task-123" in said

    drive(app, script)


def test_review_says_so_when_nothing_needs_attention(monkeypatch):
    """Silence when there is nothing to say is what makes it usable - but the
    TUI is a question someone just asked, so it answers rather than ignoring."""
    app = workspace()

    async def script(pilot):
        from agent import worker

        monkeypatch.setattr(worker, "review", lambda: None)
        app.screen.command("/review")
        await pilot.pause()

        said = " ".join(str(r) for r in app.screen.transcript).lower()
        assert "nothing" in said

    drive(app, script)


def test_serve_prints_a_url_and_does_not_block(monkeypatch):
    """serve_forever() would freeze the interface. It goes on a thread, and what
    reaches the transcript is the URL - with the token, once."""
    app = workspace()

    async def script(pilot):
        from agent import viewer

        started = []
        monkeypatch.setattr(viewer, "_secret", lambda: "tok" * 8)
        monkeypatch.setattr(viewer, "serve_in_background",
                            lambda: started.append(True) or 9999)
        app.screen.command("/serve")
        await pilot.pause()

        said = " ".join(str(r) for r in app.screen.transcript)
        assert started, "the viewer was not started"
        assert "9999" in said and "tok" in said


    drive(app, script)


def test_the_long_running_commands_are_NOT_offered(monkeypatch):
    """--worker and --channel block until interrupted, and --update replaces the
    files this process is running from. Each is correct at a shell prompt and
    wrong inside a session; leaving them out is the decision, not an omission."""
    for absent in ("/worker", "/channel", "/update"):
        assert absent not in screens.COMMANDS, absent


def test_an_action_command_on_the_landing_actually_RUNS(monkeypatch):
    """The landing's fallback opens a workspace with OPENS.get(word), which is
    None for an action - so /review would have started a session and silently
    not reviewed anything. Same shape as the /exit defect."""
    app = screens.NoesisApp(FakeGraph())

    async def script(pilot):
        from agent import worker

        monkeypatch.setattr(worker, "review", lambda: "task-77")
        app.screen.query_one(Input).value = "/review"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert isinstance(app.screen, screens.WorkspaceScreen)
        said = " ".join(str(r) for r in app.screen.transcript)
        assert "task-77" in said, "it opened a session and reviewed nothing"

    drive(app, script)


def test_a_spent_thread_says_what_happened_and_what_to_do():
    """spent_tokens is cumulative across a conversation ON PURPOSE - it is what
    keeps the budget binding over a long thread. But reflect checks it FIRST, so
    once crossed every later message returns `budget` before the model is called
    even once, and the thread is finished. Saying only a number leaves someone
    staring at a dead conversation with no idea it is dead.

    continue_state makes this exact argument for turns, which DO reset. Tokens
    do not, and the consequence needed saying out loud."""
    app = workspace()

    async def script(pilot):
        app.screen.on_trace({"kind": "terminal", "verdict": "budget",
                             "turns": 2, "spent_tokens": 204_972})
        await pilot.pause()

        said = " ".join(str(r) for r in app.screen.transcript).lower()
        assert "/chat" in said, "it never says how to carry on"
        assert "conversation" in said or "thread" in said

    drive(app, script)


def test_an_ordinary_ending_stays_one_line():
    """The explanation is for the dead end only. Adding it to every `done` would
    be four lines of advice after every answer."""
    app = workspace()

    async def script(pilot):
        app.screen.on_trace({"kind": "terminal", "verdict": "done",
                             "turns": 2, "spent_tokens": 4_000})
        await pilot.pause()

        said = " ".join(str(r) for r in app.screen.transcript).lower()
        assert "/chat" not in said

    drive(app, script)


# ============================ seeing the budget before it ends (2026-09-10)

def test_the_status_line_shows_spend_against_the_budget():
    """Turns already read `2/30`. Spend read `204,972` - a number with nothing
    to compare it to, in a thread that was two messages from being finished."""
    app = workspace()

    async def script(pilot):
        app.screen._state = {"turns": 2, "max_turns": 30,
                             "spent_tokens": 150_000, "budget_tokens": 200_000}
        app.screen.paint_status()
        await pilot.pause()

        assert "150,000/200,000" in rendered(app)

    drive(app, script)


def test_the_status_line_shows_bare_spend_when_there_is_no_budget():
    """With no ceiling there is nothing to show it against, and `150,000/0`
    reads as a bug."""
    app = workspace()

    async def script(pilot):
        app.screen._state = {"turns": 2, "max_turns": 30,
                             "spent_tokens": 150_000, "budget_tokens": 0}
        app.screen.paint_status()
        await pilot.pause()

        shown = rendered(app)
        assert "150,000" in shown and "150,000/" not in shown

    drive(app, script)


def test_a_thread_says_it_is_running_low_ONCE(monkeypatch):
    """spent_tokens only grows and reflect checks it first, so the dead end is
    reachable but never announced. Warn while /chat is still a choice rather
    than a recovery - and once, because a warning every turn is noise."""
    app = workspace()

    async def script(pilot):
        # An explicit ceiling: config.BUDGET_TOKENS is 0 (no budget) since
        # 2026-09-23, and there is nothing to run low on without one.
        app.screen._state = {"budget_tokens": 200_000}
        for _ in range(3):
            app.screen.on_trace({"kind": "terminal", "verdict": "done",
                                 "turns": 1, "spent_tokens": 170_000})
        await pilot.pause()

        said = " ".join(str(r) for r in app.screen.transcript).lower()
        assert "running low" in said
        assert said.count("running low") == 1, "it warned more than once"

    drive(app, script)


def test_a_thread_with_no_budget_never_says_it_is_running_low():
    """0 means no ceiling. A warning about a limit that does not exist is the
    kind of noise the warning above was written to avoid."""
    app = workspace()

    async def script(pilot):
        app.screen._state = {"budget_tokens": 0}
        app.screen.on_trace({"kind": "terminal", "verdict": "done",
                             "turns": 1, "spent_tokens": 5_000_000})
        await pilot.pause()

        assert "running low" not in " ".join(
            str(r) for r in app.screen.transcript).lower()

    drive(app, script)


def test_a_thread_well_inside_its_budget_says_nothing():
    app = workspace()

    async def script(pilot):
        app.screen.on_trace({"kind": "terminal", "verdict": "done",
                             "turns": 1, "spent_tokens": 20_000})
        await pilot.pause()

        said = " ".join(str(r) for r in app.screen.transcript).lower()
        assert "running low" not in said

    drive(app, script)
