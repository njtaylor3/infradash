"""Modal screens for picking and confirming actions. Cancel is always the default."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList
from textual.widgets.option_list import Option

from .models import Action

DIALOG_CSS = """
ActionMenu, ConfirmAction { align: center middle; }
#dialog { width: 84; height: auto; max-height: 80%; border: thick $warning; background: $surface;
          padding: 1 2; }
#dialog Label { width: 100%; margin-bottom: 1; }
#title { text-style: bold; }
#detail { color: $text-muted; }
#buttons { height: auto; align-horizontal: right; }
#buttons Button { margin-left: 2; }
OptionList { height: auto; max-height: 16; }
"""


class ActionMenu(ModalScreen[int | None]):
    DEFAULT_CSS = DIALOG_CSS
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, actions: list[Action]) -> None:
        super().__init__()
        self.title_text, self.actions = title, actions

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, id="title", markup=False)
            yield OptionList(*[Option(a.label, id=str(i)) for i, a in enumerate(self.actions)])
            yield Label("Enter to choose · Esc to cancel. You'll be asked to confirm.", id="detail")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(int(event.option.id))


class ConfirmAction(ModalScreen[bool]):
    DEFAULT_CSS = DIALOG_CSS
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, action: Action) -> None:
        super().__init__()
        self.action_ = action

    def compose(self) -> ComposeResult:
        a = self.action_
        with Vertical(id="dialog"):
            yield Label(a.question, id="title", markup=False)
            yield Label(a.detail, id="detail", markup=False)
            if a.type_to_confirm:
                yield Label(f"Type [b]{a.type_to_confirm}[/b] to confirm:")
                yield Input(placeholder=a.type_to_confirm, id="typed")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button(a.label, id="go", variant="error" if a.type_to_confirm else "warning",
                             disabled=bool(a.type_to_confirm))

    def on_mount(self) -> None:
        if self.action_.type_to_confirm:
            self.query_one("#typed", Input).focus()
        else:
            self.query_one("#cancel", Button).focus()   # the safe choice has focus

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#go", Button).disabled = event.value.strip() != self.action_.type_to_confirm

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip() == self.action_.type_to_confirm:
            self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "go")
