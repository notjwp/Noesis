"""The first-run wizard's screen: pick a model, enter a key, prove it works.

A wizard that stores an UNVERIFIED key is worse than no wizard, because the
failure surfaces twenty turns later as an auth error rather than here. So the
one thing this screen must get right is that nothing is written until the
endpoint has answered - and that the key never appears on screen, not even
inside the provider's own rejection message.

All the logic lives in `agent/setup.py`, which imports no textual: what gets
written, how the outcome is classified and how a key is shown are testable
without a running app, and only the widgets are here.
"""
from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from agent import config as settings
from agent import setup
from agent.ui import theme

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# What a person may do next, per outcome. A rate limit says nothing about
# whether the key is good, so refusing to save would strand someone whose
# credentials are fine; a rejection says exactly that, and offers no such door.
OFFERS = {
    "misconfigured": ("retry", "back"),
    "unavailable": ("retry", "save-anyway", "back"),
    "error": ("retry", "back"),
}


class SetupScreen(Screen):
    """Model, key, probe, save - in that order and never out of it."""

    def __init__(self, existing_key: str = "") -> None:
        super().__init__()
        self._existing = (existing_key or "").strip()
        self._index = 0
        self._timer = None
        self._frame = 0

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        with Vertical(id="setup"):
            yield Label("NOESIS  ·  setup", id="setup-title")
            yield Static("\u2191\u2193 choose  ·  enter moves on  ·  verified against "
                         "the live endpoint before it is saved  ·  ^q leaves",
                         classes="hint")
            yield OptionList(*self._options(), id="choices")
            yield Static("", id="warning")
            yield Input(placeholder="base URL, e.g. https://openrouter.ai/api/v1",
                        id="base-url")
            yield Input(placeholder="model id", id="model-id")
            yield Static("", id="key-shown")
            yield Input(placeholder="API key", password=True, id="key")
            yield Button("verify and save", id="verify", variant="primary")
            yield Static("", id="status")
            with Horizontal(id="after"):
                yield Button("retry", id="retry")
                yield Button("save anyway", id="save-anyway")
                yield Button("change model", id="back")

    def _options(self) -> list[Option]:
        out = []
        for choice in setup.CHOICES:
            label = Text(f"{choice.provider:<10} ")
            label.append(choice.model or "custom", style="bold")
            if choice.note:
                label.append(f"\n           {choice.note}", style="dim")
            out.append(Option(label))
        return out

    def on_mount(self) -> None:
        # Section 11 allows no timer while nothing is moving, and an Input
        # blinks its cursor on a live interval for as long as it has focus.
        for box in self.query(Input):
            box.cursor_blink = False
        self.query_one("#after").display = False
        # An empty Static still occupies its row, and four blank rows between
        # the list and the key box read as a layout that lost something.
        self.query_one("#key-shown").display = bool(self._existing)
        if self._existing:
            self.query_one("#key-shown", Static).update(
                Text(f"current key {setup.mask(self._existing)} - leave blank "
                     "to keep it", style="row--muted"))
        self.choose(0)
        self.query_one("#choices", OptionList).focus()

    # ----------------------------------------------------------- the choice

    def choose(self, index: int) -> None:
        """Select a model and show what selecting it costs."""
        self._index = index
        choice = setup.CHOICES[index]
        custom = not choice.model
        for widget in ("#base-url", "#model-id"):
            self.query_one(widget).display = custom
        # The default IS the baseline, so only a departure from it is news.
        self.query_one("#warning").display = index != 0
        self.query_one("#warning", Static).update(
            Text("" if index == 0 else setup.WARNING, style="bold"))

    @on(OptionList.OptionHighlighted, "#choices")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.choose(event.option_index)

    # Enter walks the form: row -> (base URL -> model id ->) key -> verify.
    # Measured: with only Tab between the list and the key box, and no hint,
    # the first person to open it could not find a way to type a key.
    @on(OptionList.OptionSelected, "#choices")
    def _selected(self) -> None:
        self.choose(self.query_one("#choices", OptionList).highlighted or 0)
        first = "#base-url" if not setup.CHOICES[self._index].model else "#key"
        self.query_one(first, Input).focus()

    @on(Input.Submitted, "#base-url")
    def _after_base_url(self) -> None:
        self.query_one("#model-id", Input).focus()

    @on(Input.Submitted, "#model-id")
    def _after_model_id(self) -> None:
        self.query_one("#key", Input).focus()

    @on(Input.Submitted, "#key")
    def _after_key(self) -> None:
        self._verify()

    def key_in_hand(self) -> str:
        """What was typed, or the key already configured. §6.7: changing the
        model must not cost you a key you already have."""
        return self.query_one("#key", Input).value.strip() or self._existing

    def _chosen(self) -> tuple[str, str, str]:
        choice = setup.CHOICES[self._index]
        if choice.model:
            return choice.provider, choice.model, ""
        return ("openai",
                self.query_one("#model-id", Input).value.strip(),
                self.query_one("#base-url", Input).value.strip())

    # ----------------------------------------------------------- the probe

    @on(Button.Pressed, "#verify")
    @on(Button.Pressed, "#retry")
    def _verify(self) -> None:
        chosen, model, base_url = self._chosen()
        if not self.key_in_hand():
            self._say("enter a key first", "error")
            return
        if not model or (chosen == "openai" and not base_url):
            # Blank means "the configured endpoint" further down, so a custom
            # choice with a gap in it would verify NVIDIA and then configure
            # something else - a wizard that passes and leaves you broken.
            self._say("a custom endpoint needs both a base URL and a model id",
                      "error")
            return
        self.query_one("#after").display = False
        self.app.offered = set()
        self._start_spinner()
        self._probe()

    @work(thread=True, exclusive=True)
    def _probe(self) -> None:
        chosen, model, base_url = self._chosen()
        outcome, message = setup.probe(chosen, model, base_url, self.key_in_hand())
        self.app.call_from_thread(self._answered, outcome, message)

    def _answered(self, outcome: str, message: str) -> None:
        self._stop_spinner()
        self.app.last_message = message
        if outcome == "ok":
            self._save()
            return
        self.app.offered = set(OFFERS[outcome])
        self.query_one("#after").display = True
        self.query_one("#save-anyway").display = "save-anyway" in self.app.offered
        self._say(f"{outcome}: {message}", "error")

    @on(Button.Pressed, "#save-anyway")
    def _save_anyway(self) -> None:
        self._save()

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.query_one("#after").display = False
        self.app.offered = set()
        self.query_one("#choices", OptionList).focus()

    def _save(self) -> None:
        key = self.key_in_hand()
        chosen, model, base_url = self._chosen()
        try:
            setup.write_env(setup.variables(chosen, model, base_url, key))
        except OSError as exc:
            # A read-only checkout, most likely. Saying so beats a traceback
            # over a wizard the person cannot get past.
            self._say(f"could not write .env: {exc}", "error")
            return
        self._say(f"saved.  key {setup.mask(key)}\n{setup.OPACITY_HINT}", "ok")
        self.app.exit(True)

    def _say(self, text: str, kind: str) -> None:
        # Scrubbed HERE and not only inside probe(): this is the boundary the
        # key must not cross, and a message can arrive from anywhere (NFR-203).
        self.query_one("#status", Static).update(
            Text(setup.scrub(text, self.key_in_hand()),
                 style="bold" if kind == "error" else ""))

    # -------------------------------------------------------------- spinner

    def _start_spinner(self) -> None:
        self._say("", "ok")
        self._timer = self.set_interval(0.08, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(SPINNER)
        self._say(f"{SPINNER[self._frame]} asking the endpoint", "ok")

    def _stop_spinner(self) -> None:
        # No timer may run while nothing is moving; idle CPU is 0%.
        if self._timer is not None:
            self._timer.stop()
            self._timer = None


class SetupApp(App):
    """The wizard standing alone, which is how the entry point reaches it.

    `offered` and `last_message` are the screen's outcome, kept here because
    the App is what `run()` gets an answer from.
    """

    # A wizard with no way out is the one screen that can strand someone, and
    # leaving must cost nothing: nothing is written until a probe has answered.
    BINDINGS = [Binding("ctrl+q", "quit", "quit", priority=True)]

    CSS_PATH = theme.STYLESHEET


    def __init__(self, existing_key: str = "") -> None:
        # The same registration NoesisApp does, and for the same reason: the
        # stylesheet is parsed before a theme applies, so $muted is undefined
        # unless the theme is selected here.
        super().__init__(ansi_color=True)
        for one in theme.THEMES:
            self.register_theme(one)
        theme.apply(self, settings.TUI_THEME, settings.TUI_TRANSPARENT)
        self._existing = existing_key
        self.offered: set = set()
        self.last_message = ""

    def on_mount(self) -> None:
        self.push_screen(SetupScreen(self._existing))
