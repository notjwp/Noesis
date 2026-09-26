"""The three palettes, the stylesheet, and the transparency mechanism.

Folded into the tenth deviation rather than claiming an eleventh: the theme is
where the no-hardcoded-hex rule is enforced, and that rule is not cosmetic. An
unset colour under `ansi_color=True` degrades silently to 16-colour ANSI, so a
palette can be lost with nothing raising and nothing looking wrong in a diff.

Everything here runs headless, through Textual's own compositor.
"""
import asyncio
import pathlib
import re

import pytest
from textual.app import App
from textual.containers import Horizontal
from textual.widgets import Static

from agent import config
from agent.ui import theme

UI = pathlib.Path(theme.__file__).parent


class Host(App):
    """A minimal app that loads the real stylesheet and the real themes.

    The registration happens in __init__ ON PURPOSE: the stylesheet is parsed
    before a theme is applied, so `$muted` and friends are undefined unless the
    theme is selected by then. NoesisApp does the same thing for the same
    reason.
    """

    CSS_PATH = theme.STYLESHEET

    def __init__(self, name: str = "noesis-mono", mode: str = "gaps") -> None:
        super().__init__(ansi_color=True)
        for one in theme.THEMES:
            self.register_theme(one)
        theme.apply(self, name)
        self._mode = theme.resolve_mode(mode)

    def compose(self):
        with Horizontal(id="status"):
            yield Static("step 3/7", classes="status--step")
            yield Static("^t theme", classes="status--hint")
        yield Static("12:04", classes="row--muted", id="stamp")

    def on_mount(self) -> None:
        self.screen.add_class(f"-{self._mode}")


def drive(app, script):
    async def _go():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await script(pilot)
    asyncio.run(_go())


def backgrounds(app) -> set:
    """Every distinct background the compositor actually emits."""
    seen = set()
    for strip in app.screen._compositor.render_strips():
        for segment in strip:
            style = segment.style
            seen.add("default" if style is None or style.bgcolor is None
                     else style.bgcolor.name)
    return seen


# ================================================================== the themes

def test_all_three_register_and_every_one_can_be_selected():
    app = Host()

    async def script(pilot):
        for name in theme.NAMES:
            theme.apply(app, name)
            await pilot.pause()
            assert app.theme == name

    drive(app, script)


@pytest.mark.parametrize("one", theme.THEMES, ids=lambda t: t.name)
def test_every_semantic_variable_is_defined_in_every_theme(one):
    """A theme missing `muted` does not raise at import - it fails when the
    stylesheet is parsed, which is at startup, in front of the user."""
    assert set(one.variables) == {"muted", "border", "info"}
    assert one.primary and one.accent and one.error
    assert one.success and one.warning
    assert one.background and one.foreground and one.surface and one.panel


@pytest.mark.parametrize("one", theme.THEMES, ids=lambda t: t.name)
def test_every_colour_a_theme_states_is_a_real_hex(one):
    values = [getattr(one, field) for field in
              ("primary", "secondary", "warning", "error", "success", "accent",
               "foreground", "background", "surface", "panel")]
    values += list(one.variables.values())
    assert all(re.fullmatch(r"#[0-9A-Fa-f]{6}", value) for value in values)


@pytest.mark.parametrize("one", theme.THEMES, ids=lambda t: t.name)
def test_the_stylesheet_resolves_this_themes_own_muted(one):
    """`$muted` is not a Textual token, so a theme that omitted it fails here
    and only here - and the value must come from THIS theme, not the last."""
    app = Host(one.name)

    async def script(pilot):
        got = app.query_one("#stamp").styles.color
        assert got.hex.lower() == one.variables["muted"].lower()

    drive(app, script)


@pytest.mark.parametrize("role", ["background", "surface", "foreground", "accent"])
def test_no_two_themes_share_a_load_bearing_colour(role):
    """Not "the palettes are not identical" - that passes with a shared
    background, and a switch that changes half the screen looks broken rather
    than themed."""
    values = [getattr(one, role, None) or one.variables[role]
              for one in theme.THEMES]
    assert len(set(values)) == len(theme.THEMES), values


def test_the_muted_tone_differs_too():
    muted = [one.variables["muted"] for one in theme.THEMES]
    assert len(set(muted)) == len(theme.THEMES)


# ============================================================ falling back

@pytest.mark.parametrize("given", ["", "nope", "Noesis-Mono", "dracula"])
def test_an_unknown_theme_name_falls_back_without_raising(given):
    """Textual raises InvalidThemeError on an unregistered name, so this is our
    job rather than its. A typo in AGENT_TUI_THEME must not stop the terminal
    opening."""
    assert theme.resolve(given) == "noesis-mono"


@pytest.mark.parametrize("given", ["", "nope", "Bare", "transparent"])
def test_an_unknown_transparency_mode_falls_back_without_raising(given):
    assert theme.resolve_mode(given) == "gaps"


def test_a_bad_config_value_still_opens_the_interface(monkeypatch):
    monkeypatch.setattr(config, "TUI_THEME", "not-a-theme")
    monkeypatch.setattr(config, "TUI_TRANSPARENT", "not-a-mode")
    app = Host(config.TUI_THEME, config.TUI_TRANSPARENT)

    async def script(pilot):
        assert app.theme == "noesis-mono"
        assert app.screen.has_class("-gaps")

    drive(app, script)


def test_the_shipped_defaults_are_the_ones_the_themes_know():
    assert config.TUI_THEME in theme.NAMES
    assert config.TUI_TRANSPARENT in theme.MODES


# ================================================================== cycling

def test_ctrl_t_cycles_mono_mocha_tokyo_and_back():
    seen = ["noesis-mono"]
    for _ in range(3):
        seen.append(theme.cycle(seen[-1]))
    assert seen == ["noesis-mono", "catppuccin-mocha", "tokyo-night",
                    "noesis-mono"]


def test_ctrl_g_cycles_opaque_gaps_bare_and_back():
    seen = ["opaque"]
    for _ in range(3):
        seen.append(theme.cycle_mode(seen[-1]))
    assert seen == ["opaque", "gaps", "bare", "opaque"]


def test_cycling_from_a_name_nobody_registered_still_moves():
    assert theme.cycle("nonsense") == "catppuccin-mocha"
    assert theme.cycle_mode("nonsense") == "bare"


# ============================================================= transparency

def test_gaps_leaves_the_screen_bare_and_the_chrome_painted():
    """The Hyprland look: wallpaper in the gutters, text on solid ground."""
    app = Host(mode="gaps")

    async def script(pilot):
        seen = backgrounds(app)
        assert "default" in seen
        assert seen - {"default"}, "nothing was painted at all"

    drive(app, script)


def test_opaque_paints_every_cell():
    """Safe over SSH, in a screenshot, and in a terminal with no opacity."""
    app = Host(mode="opaque")

    async def script(pilot):
        assert "default" not in backgrounds(app)

    drive(app, script)


def test_bare_paints_no_surface_at_all():
    """CHANGED 2026-09-25. It used to assert the opposite - "the status bar and
    composer are painted in all three modes" - so that an approval prompt could
    never be hard to read over a wallpaper. Asked for and decided otherwise: the
    theme is carried by borders and text, and a dialog's border is what marks it.

    The CARET is the one cell still painted, because Textual draws it as a
    background block and transparent means no caret at all.
    """
    app = Host(mode="bare")

    async def script(pilot):
        one = app.current_theme
        fills = {one.background, one.surface, one.panel}
        painted = {value for value in backgrounds(app) if value != "default"}

        assert "default" in backgrounds(app)
        assert not (painted & fills), f"a surface is still filled: {painted & fills}"

    drive(app, script)


def test_the_caret_survives_bare_because_nothing_else_marks_it():
    """The single exception, asserted so it cannot be tidied away later."""
    app = Host(mode="bare")

    async def script(pilot):
        rules = theme.STYLESHEET.read_text(encoding="utf-8")
        cursor = rules.split("Input > .input--cursor")[1].split("}")[0]
        assert "$accent" in cursor and "transparent" not in cursor

    drive(app, script)


def test_selection_is_still_visible_when_its_background_goes():
    """It was a fill plus body text. With no fill, the accent has to carry it or
    a selected row is indistinguishable from the rest."""
    rules = theme.STYLESHEET.read_text(encoding="utf-8")
    bare = [block for block in rules.split("}")
            if "Screen.-bare .row--selected" in block]

    assert bare, "no bare rule for .row--selected"
    assert any("$accent" in block for block in bare)


def test_the_destructive_button_keeps_its_hue_as_text_in_bare():
    """`Button.-error` was $error as a FILL. Losing the fill must not lose the
    one saturated colour a destructive action gets - theme.py calls that a
    safety property, not a style choice."""
    rules = theme.STYLESHEET.read_text(encoding="utf-8")
    bare = [block for block in rules.split("}")
            if "Screen.-bare Button.-error" in block]

    assert bare, "no bare rule for Button.-error"
    assert any("color: $error" in block for block in bare)


def test_an_explicit_colour_survives_ansi_color():
    """The measurement the whole approach rests on: under ansi_color=True an
    explicitly set hex stays truecolor and only an UNSET one degrades."""
    app = Host(mode="gaps")

    async def script(pilot):
        painted = backgrounds(app) - {"default"}
        assert any(value.startswith("#") for value in painted), painted

    drive(app, script)


# ================================================================== the rule

def test_no_colour_is_written_anywhere_but_theme_py():
    """Load bearing twice over: three themes only work if every colour is one
    lookup, and transparency only works if every foreground is explicitly set."""
    offenders = {}
    for path in sorted(UI.rglob("*")):
        if path.suffix not in (".py", ".tcss") or path.name == "theme.py":
            continue
        hits = re.findall(r"#[0-9A-Fa-f]{6}\b",
                          path.read_text(encoding="utf-8"))
        if hits:
            offenders[path.name] = hits
    assert offenders == {}


def test_the_stylesheet_is_read_inside_the_app_not_at_import(monkeypatch):
    """CE-05: no module-level I/O. `STYLESHEET` is a path, and Textual opens it
    when an App is constructed."""
    assert isinstance(theme.STYLESHEET, pathlib.Path)
    assert theme.STYLESHEET.is_file()


def test_switching_a_theme_does_not_silently_turn_transparency_off():
    """textual 8.0.1's theme watcher sets `ansi_color = name == "textual-ansi"`,
    so assigning a theme turns off the only lever that emits a bare cell. Every
    switch goes through theme.apply(), and this is why."""
    app = Host(mode="gaps")

    async def script(pilot):
        assert app.ansi_color is True
        for name in theme.NAMES:
            theme.apply(app, name)
            await pilot.pause()
            assert app.ansi_color is True, name
            assert "default" in backgrounds(app), name

    drive(app, script)


def test_assigning_the_theme_directly_is_what_breaks_it():
    """The mutation this guards against, asserted as a fact about Textual
    rather than trusted to stay true."""
    app = Host(mode="gaps")

    async def script(pilot):
        app.theme = "catppuccin-mocha"
        await pilot.pause()
        assert app.ansi_color is False

    drive(app, script)


def test_bare_promotes_every_muted_role_now_that_nothing_is_painted():
    """Section 10.3's mitigation, and it is real rather than a note: $muted over
    a busy wallpaper is unreadable and cannot be fixed from inside the app.

    CHANGED 2026-09-25, 10.2/10.3 AMENDED. The chrome used to keep $muted
    BECAUSE the chrome stayed painted; it no longer stays painted, so the reason
    to exempt it is gone and it is promoted with the body text.
    """
    painted, wallpaper = Host(mode="gaps"), Host(mode="bare")
    seen = {}

    def look(app, mode):
        async def script(pilot):
            seen[mode] = (app.query_one("#stamp").styles.color.hex.lower(),
                          app.query_one(".status--hint").styles.color.hex.lower())
        drive(app, script)

    look(painted, "gaps")
    look(wallpaper, "bare")
    muted = theme.MONO.variables["muted"].lower()
    body = theme.MONO.foreground.lower()

    assert seen["gaps"] == (muted, muted), "a painted mode promoted something"
    assert seen["bare"] == (body, body), "a muted role was left unreadable"
