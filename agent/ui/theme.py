"""Three palettes, and the switches that pick between them.

Every colour in the interface is one lookup here, and that is load bearing
twice. Three themes only work if nothing else holds a hex. And transparency
(section 10) only works if every foreground is explicitly SET: under
`ansi_color=True` an UNSET colour silently degrades to the terminal's sixteen
ANSI colours, while an explicit one survives as truecolor.

MEASURED against textual 8.0.1, and it decides the shape of this file:

  - The stylesheet is parsed before the theme is applied, so `$muted` and the
    other custom variables are undefined unless the theme is registered AND
    selected in `App.__init__`. `NoesisApp` does exactly that.
  - Assigning an unregistered name raises `InvalidThemeError`, so section
    4.4's "falls back without raising" is `resolve()`'s job, not Textual's.
  - `catppuccin-mocha` and `tokyo-night` are also BUILT-IN Textual theme
    names, and registering these shadows them. Intentional: the palettes below
    are the spec's, and the tokyo one is deliberately narrowed to three hues.
"""
from pathlib import Path

from rich.theme import Theme as RichTheme
from textual.theme import Theme

STYLESHEET = Path(__file__).with_name("noesis.tcss")

MONO = Theme(
    name="noesis-mono",
    background="#0A0B0D",
    # Raised until a pane is VISIBLY a pane. #121417 sat 8-10 points off
    # the background, which no display resolves - reported as "the theme
    # only affects the outer background". Separation matches mocha, the
    # one palette that always read correctly.
    surface="#1A1D21",
    panel="#24282E",
    foreground="#E6E8EB",
    accent="#A3E635",
    primary="#A3E635",
    secondary="#6B7280",
    # A SECOND saturated colour in a scheme that claims one, and it is
    # deliberate: a denied or destructive call rendering in grey is a safety
    # defect, not a style choice. Approval modal, deny verdicts, doctor FAIL
    # lines and the no-API-key state. Nowhere else.
    error="#FF5F56",
    # Success and warning reuse the accent rather than adding a third and
    # fourth hue, which is the entire claim this palette makes.
    success="#A3E635",
    warning="#A3E635",
    dark=True,
    # `info` marks paths and ids. Body text here rather than the accent,
    # because the accent means focus and a filename is content, not focus.
    variables={"muted": "#6B7280", "border": "#22262B", "info": "#E6E8EB"},
)

MOCHA = Theme(
    name="catppuccin-mocha",
    background="#1E1E2E",          # base
    surface="#313244",             # surface0
    panel="#181825",               # mantle
    foreground="#CDD6F4",          # text
    accent="#CBA6F7",              # mauve
    primary="#CBA6F7",
    secondary="#89B4FA",           # blue
    success="#A6E3A1",             # green
    error="#F38BA8",               # red
    warning="#FAB387",             # peach
    dark=True,
    variables={"muted": "#6C7086",     # overlay0
               "border": "#45475A",    # surface1
               "info": "#89B4FA"},     # blue
)

TOKYO = Theme(
    name="tokyo-night",
    background="#16161E",
    # Was #1A1B26 for BOTH, 4/5/8 points off the background.
    surface="#24283B",
    panel="#1F2335",
    foreground="#C0CAF5",
    accent="#7DCFFF",              # cyan
    primary="#7DCFFF",
    secondary="#565F89",
    success="#9ECE6A",             # green
    error="#F7768E",               # red
    # Green again, dimmed. Only cyan, green and red carry meaning here, so a
    # fourth hue for warnings would be a fourth thing to learn.
    warning="#9ECE6A",
    dark=True,
    variables={"muted": "#565F89",
               "border": "#292E42",
               "info": "#7DCFFF"},     # paths reuse the accent, no new hue
)

THEMES = (MONO, MOCHA, TOKYO)
NAMES = tuple(theme.name for theme in THEMES)

# opaque: every cell painted, which is what survives SSH and a screenshot.
# gaps:   screen bare, pane interiors painted - wallpaper in the gutters only.
# bare:   NOTHING painted, since 2026-09-25 - borders and text carry the theme.
#         The Input caret is the single exception; see noesis.tcss.
MODES = ("opaque", "gaps", "bare")


def resolve(name: str) -> str:
    """A theme name, or the default. Never raises: an unreadable AGENT_TUI_THEME
    is a typo, and a typo must not stop the interface opening."""
    return name if name in NAMES else NAMES[0]


def resolve_mode(name: str) -> str:
    return name if name in MODES else "gaps"


def rich_styles(one: Theme, mode: str = "gaps") -> dict[str, str]:
    """The same roles as the stylesheet, as RICH styles.

    Two registries exist because two things are being coloured. A widget takes
    a CSS class; a `rich.Text` inside a RichLog does not - Rich resolves
    `style=` against its own table, so a CSS class name there is not a style at
    all, it is a parse error. Both tables are built from this one Theme, which
    is what keeps the no-hardcoded-hex rule true.

    In `bare` the muted role is promoted here for the same reason the
    stylesheet promotes it: over a wallpaper it cannot be read, and the
    transcript is body text. `bare` paints nothing at all, so there is never a
    fill under it to rescue the contrast.
    """
    return {
        "row--muted": one.foreground if mode == "bare" else one.variables["muted"],
        "row--path": one.variables["info"],
        "row--ran": one.foreground,
        "row--mutated": one.success,
        "row--denied": one.error,
        "row--selected": one.accent,
        "row--logo": one.accent,
    }


def apply(app, name: str, mode: str = "gaps") -> str:
    """Select a theme and keep transparency alive across the switch.

    MEASURED against textual 8.0.1: `App._watch_theme` runs
    `self.ansi_color = theme_name == "textual-ansi"` (app.py:1455), so ASSIGNING
    A THEME TURNS OFF the only lever that produces a bare cell. Every theme
    change goes through here, and here re-asserts it afterwards.
    """
    app.theme = resolve(name)
    app.ansi_color = True
    if getattr(app, "_noesis_rich", False):
        app.console.pop_theme()          # replace ours, never stack another
    app.console.push_theme(
        RichTheme(rich_styles(app.current_theme, resolve_mode(mode))))
    app._noesis_rich = True
    return app.theme


def cycle(name: str) -> str:
    """ctrl+t: mono -> mocha -> tokyo -> mono, for this session only."""
    return NAMES[(NAMES.index(resolve(name)) + 1) % len(NAMES)]


def cycle_mode(name: str) -> str:
    """ctrl+g: opaque -> gaps -> bare -> opaque."""
    return MODES[(MODES.index(resolve_mode(name)) + 1) % len(MODES)]
