"""Keyboard-first forms anchored above the conversation composer."""

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Checkbox, Collapsible, Select, Static


class FormCheckbox(Checkbox):
    """Show checked state as text, including in monochrome terminals."""

    def render(self) -> Text:
        return Text(
            f"[{'x' if self.value else ' '}] {self.label}",
            style="bold reverse" if self.has_focus else "",
            no_wrap=True,
            overflow="ellipsis",
        )


class FormModal(ModalScreen[dict | None]):
    """Shared anchored forms, validation, drafts, and small-terminal behavior."""

    DEFAULT_CSS = """
    FormModal.form-menu { align: center bottom; background: $background 30%; }
    FormModal .form-dialog {
        width: 92%; max-width: 84; height: 24; padding: 1 2;
        background: $surface; border: solid $primary;
    }
    FormModal .form-title { height: 1; color: $primary; text-style: bold; }
    FormModal .form-subtitle {
        height: 1; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis;
    }
    FormModal .form-body { height: 1fr; scrollbar-size: 1 1; }
    FormModal .field-label { height: 1; margin-top: 1; color: $text-muted; }
    FormModal Input {
        border: none; height: 1; margin: 0; padding: 0 1;
        background: $background;
    }
    FormModal Input:focus {
        border: none; background: $panel; text-style: bold reverse;
    }
    FormModal Select { margin: 0; height: auto; }
    FormModal Select:focus > SelectCurrent {
        background: $panel; text-style: bold reverse;
    }
    FormModal SelectOverlay { border: none; padding: 0; }
    FormModal SelectCurrent #label {
        height: 1; text-wrap: nowrap; text-overflow: ellipsis;
    }
    FormModal OptionList > .option-list--option-highlighted { text-style: bold reverse; }
    FormModal .field-help { height: auto; color: $text-muted; }
    FormModal .form-status { height: auto; max-height: 3; margin-top: 1; }
    FormModal .form-status.problem { color: $warning; }
    FormModal .field-error { display: none; height: auto; color: $error; }
    FormModal .field-error.problem { display: block; }
    FormModal .form-actions { height: 1; margin-top: 1; }
    FormModal Button.-style-flat {
        min-width: 0; width: auto; height: 1; margin: 0 1 0 0;
        padding: 0; border: none; background: $panel; color: $text;
    }
    FormModal Button.-style-flat:hover, FormModal Button.-style-flat.-active {
        border: none;
    }
    FormModal Button.-style-flat:focus { border: none; text-style: bold reverse; }
    FormModal Button.-style-flat:disabled { color: $text-muted; text-style: dim; }
    FormModal .form-hint { height: 1; color: $text-muted; }
    FormModal .form-too-small { display: none; height: auto; padding: 1; }
    FormModal.compact .form-dialog { width: 96%; padding: 0 1; }
    FormModal.narrow .form-dialog { width: 100%; }
    FormModal.compact .form-status { margin-top: 0; max-height: 2; }
    FormModal.compact .form-actions { margin-top: 0; }
    FormModal.short .form-title, FormModal.short .form-subtitle,
    FormModal.short .form-status {
        display: none;
    }
    FormModal.short .form-status.problem {
        display: block; height: 1; text-wrap: nowrap; text-overflow: ellipsis;
    }
    FormModal .form-status.problem.field-problem { display: none; }
    FormModal.short .field-label { margin-top: 0; }
    FormModal.monochrome .form-title,
    FormModal.monochrome .form-status.problem,
    FormModal.monochrome .field-error { color: $text; }
    FormModal.monochrome .form-dialog { border: solid $foreground; }
    FormModal.ascii .form-dialog { border: ascii $primary; }
    FormModal.ascii.monochrome .form-dialog { border: ascii $foreground; }
    FormModal.too-small .form-dialog { display: none; }
    FormModal.too-small .form-too-small { display: block; }
    """
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("ctrl+s", "save", "Apply", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__(classes="form-menu")
        self._committed = False
        self._resize_focus: Widget | None = None

    def on_screen_suspend(self) -> None:
        if self.is_mounted and not self._committed:
            self.remember_draft()

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.resize_state()

    def resize_state(self) -> None:
        size = self.app.size
        too_small = size.width < 48 or size.height < 12
        if too_small and not self.has_class("too-small") and self.focused is not None:
            self._resize_focus = self.focused
        self.set_class(size.height < 30 or size.width < 70, "compact")
        self.set_class(size.width < 70, "narrow")
        self.set_class(size.height < 18, "short")
        self.set_class(too_small, "too-small")
        self.set_class(self.app.no_color, "monochrome")
        self.set_class(bool(getattr(self.app, "ascii", False)), "ascii")
        self.query_one(".form-dialog").border_title = (
            str(self.query_one(".form-title", Static).render()) if self.has_class("short") else ""
        )
        if self.has_class("ascii"):
            for arrow in self.query("Select .down-arrow"):
                arrow.update("v")
            for arrow in self.query("Select .up-arrow"):
                arrow.update("^")
            self.query_one(".form-too-small", Static).update(
                "Terminal too small - need 48x12. Resize or press Esc to close."
            )
        self.responsive_copy(size.width)
        self.call_after_refresh(self.position_dialog)
        if not too_small and self._resize_focus is not None:
            self.call_after_refresh(self.restore_resize_focus)

    def restore_resize_focus(self) -> None:
        if (not self.has_class("too-small") and self.is_mounted
                and self.app.screen is self and self._resize_focus is not None):
            target, self._resize_focus = self._resize_focus, None
            if target.is_mounted:
                target.focus()

    def position_dialog(self) -> None:
        """Grow upward from the composer using the current background layout."""
        base = self.app.screen_stack[0]
        composer = next(iter(base.query("#composer")), None)
        height = self.app.size.height
        anchor = height - 2
        if composer is not None and composer.region.height and composer.visible:
            anchor = composer.region.y
        anchor = max(5, min(anchor, height))
        dialog = self.query_one(".form-dialog")
        dialog.styles.margin = (0, 0, height - anchor, 0)
        chat_rows = min(6, max(2, height // 4))
        dialog.styles.height = min(24, max(5, anchor - chat_rows))
        self.call_after_refresh(self.reveal_focus)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self.call_after_refresh(self.reveal_focus)

    def set_focus(self, widget: Widget | None, scroll_visible: bool = True,
                  from_app_focus: bool = False) -> None:
        # The shared reveal callback scrolls after the anchored dialog is laid out.
        # A concurrent animated centering scroll can move a field below a short form.
        super().set_focus(widget, scroll_visible=False, from_app_focus=from_app_focus)
        if scroll_visible:
            self.call_after_refresh(self.reveal_focus)

    def reveal_focus(self) -> None:
        if not self.has_class("too-small") and self.focused is not None:
            body = next((parent for parent in self.focused.ancestors
                         if isinstance(parent, FormBody)), None)
            if body is not None and self.focused.region and body.content_region:
                # Map through the form's direct child. All offsets then come
                # from the same layout, so repeated focus callbacks cannot
                # overscroll, and nested Collapsible margins do not hide fields.
                child = self.focused
                while isinstance(child.parent, Widget) and child.parent is not body:
                    child = child.parent
                target = self.focused.region.translate(
                    child.virtual_region.offset - child.region.offset,
                )
                body.scroll_to_region(target, animate=False, immediate=True)
            else:
                self.focused.scroll_visible(animate=False, immediate=True)

    def responsive_copy(self, width: int):
        pass

    def status(self, message: str, *, problem: bool = False):
        widget = self.query_one(".form-status", Static)
        widget.update(message)
        widget.set_class(problem, "problem")
        widget.remove_class("field-problem")

    def field_error(self, field_id: str, message: str) -> None:
        error = self.query_one(f"#{field_id}-error", Static)
        error.update("Error: " + message)
        error.add_class("problem")
        self.status(message, problem=True)
        self.query_one(".form-status").add_class("field-problem")
        control = self.query_one(f"#{field_id}")
        advanced = next((node for node in control.ancestors if isinstance(node, Collapsible)), None)
        if advanced is not None:
            advanced.collapsed = False
        control.focus()
        self.call_after_refresh(error.scroll_visible, animate=False)

    def clear_errors(self) -> None:
        self.query(".field-error").remove_class("problem")
        self.query_one(".form-status").remove_class("field-problem")

    def remember_draft(self) -> None:
        raise NotImplementedError

    def action_close(self):
        for selector in self.query(Select):
            if selector.expanded:
                selector.expanded = False
                selector.focus()
                return
        if getattr(self, "committing", False):
            self.status("Saving is in progress. This dialog closes when it finishes.")
            return
        self.remember_draft()
        self.dismiss(None)


class FormBody(VerticalScroll, can_focus=False):
    """Keep the active field visible after the overlay's viewport changes."""

    def on_resize(self, event: events.Resize) -> None:
        screen = self.screen
        if isinstance(screen, FormModal):
            screen.call_after_refresh(screen.reveal_focus)
