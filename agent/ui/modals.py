"""The two screens that ask a person something, moved and restyled only.

Ported from agent/tui.py with NO behaviour change. Both are where consent is
decided, both are covered by tests that assert exactly these rules, and a modal
that answers "allow" when it is dismissed is a security defect that manual
testing finds only by accident.
"""
from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from agent import cli


class ApprovalScreen(ModalScreen[str | dict]):
    """The paused call, and one answer (FR-306, NFR-801).

    Every argument is shown in full, never abbreviated - the CLI's rule, and the
    reason is the same: a prompt that elides the dangerous half of a command
    manufactures consent instead of obtaining it.

    A modal has dismissal paths a keystroke loop cannot have, and each of them
    resolves to DENY: escape, and any dismissal that is not the allow button.
    Silence is not consent, exactly as the CLI's EOFError branch already says.

    FR-307: Amend turns every argument into a field and answers with the
    corrected set, which the gate re-classifies. A way to fix a call, never a
    way past the gate.
    """

    BINDINGS = [("escape", "refuse", "deny")]

    def __init__(self, payload: dict) -> None:
        super().__init__()
        self._payload = payload
        self._editing = False

    def compose(self) -> ComposeResult:
        call = self._payload["call"]
        with Vertical(id="approval"):
            yield Label(f"APPROVAL NEEDED   {call['name']}", id="approval-title")
            for key, value in call["input"].items():
                yield Static(Text(f"  {key}: {value}"), classes="arg")
                field = Input(value=str(value), id=f"amend-{key}", classes="amend")
                field.display = False
                yield field
            yield Label(f"Reason: {self._payload.get('reason', '')}", classes="why")
            with Horizontal(id="approval-buttons"):
                yield Button("Allow", id="allow", variant="error")
                yield Button("Allow for session", id="session", variant="error")
                yield Button("Amend", id="amend")
                yield Button("Deny", id="deny", variant="primary")
                yield Button("Quit", id="quit")

    def on_mount(self) -> None:
        # Deny takes focus, so a reflexive Enter refuses. The dangerous answer
        # must be the one that takes a deliberate keystroke.
        self.query_one("#deny", Button).focus()

    @on(Button.Pressed, "#amend")
    def _amend(self) -> None:
        """First press reveals the fields, second answers with them. One button
        because a second one would sit there doing nothing until it was armed."""
        if not self._editing:
            self._editing = True
            for field in self.query(".amend"):
                field.display = True
            self.query_one("#amend", Button).label = "Apply"
            self.query_one(".amend", Input).focus()
            return
        call = self._payload["call"]
        self.dismiss({"decision": "amend",
                      "input": {key: self.query_one(f"#amend-{key}", Input).value
                                for key in call["input"]}})

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        answer = {"allow": "allow", "session": "session", "deny": "deny",
                  "quit": cli.QUIT}.get(event.button.id)
        if answer is not None:
            self.dismiss(answer)

    def action_refuse(self) -> None:
        self.dismiss("deny")


class AskScreen(ModalScreen[str]):
    """The agent's own question, and one answer (`ask_user`).

    Unlike the two below, dismissing this is not a refusal - there is nothing to
    refuse. An empty answer sends the tool back its own "use your best
    judgement" text, so a dismissed question costs a guess rather than a run.
    """

    BINDINGS = [("escape", "skip", "skip")]

    def __init__(self, question: str, choices: list[str]) -> None:
        super().__init__()
        self._question = question
        self._choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="ask"):
            yield Label("THE AGENT IS ASKING", id="ask-title")
            yield Static(Text(self._question), classes="why")
            for number, choice in enumerate(self._choices, 1):
                yield Static(Text(f"  {number}. {choice}"), classes="arg")
            yield Input(placeholder="answer, or a number  ·  escape to skip",
                        id="ask-answer")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted, "#ask-answer")
    def _answered(self, event: Input.Submitted) -> None:
        answer = event.value.strip()
        if self._choices and answer.isdigit() and 1 <= int(answer) <= len(self._choices):
            answer = self._choices[int(answer) - 1]
        self.dismiss(answer)

    def action_skip(self) -> None:
        self.dismiss("")


class PlanScreen(ModalScreen[str]):
    """The plan, before anything has been changed (UR-02, UR-05).

    Dismissal resolves to REVISE, never accept - the same rule as the tool
    modal's deny, and for the same reason: an approval nobody gave must not be
    manufactured by a stray escape. Revise is also the cheap direction, since
    nothing has been written yet.
    """

    BINDINGS = [("escape", "revise", "revise")]

    def __init__(self, payload: dict) -> None:
        super().__init__()
        self._payload = payload

    def compose(self) -> ComposeResult:
        with Vertical(id="plan"):
            yield Label("PLAN", id="plan-title")
            for number, step in enumerate(self._payload["plan"], 1):
                yield Static(Text(f"  {number}. {step}"), classes="step")
            with Horizontal(id="plan-buttons"):
                yield Button("Accept", id="accept", variant="primary")
                yield Button("Revise", id="revise")
                yield Button("Quit", id="quit")

    def on_mount(self) -> None:
        # Accept holds focus here, unlike the tool modal where deny does.
        # Nothing has been changed yet, so the reflexive key is the harmless one.
        self.query_one("#accept", Button).focus()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss({"accept": "accept", "revise": "revise",
                      "quit": cli.QUIT}[event.button.id])

    def action_revise(self) -> None:
        self.dismiss("revise")
