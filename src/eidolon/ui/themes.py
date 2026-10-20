"""A live theme picker and a retained, labeled custom-color editor."""

from dataclasses import dataclass

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, ContentSwitcher, Input, OptionList, Static
from textual.widgets.option_list import Option

from eidolon.config import ThemeConfig
from eidolon.themes import COLOR_FIELDS, PALETTES, THEME_LABELS, resolve_theme, validate_theme
from eidolon.ui.forms import FormBody, FormModal


@dataclass
class ThemeDraft:
    base: str
    values: dict[str, str]


class ThemeOptions(OptionList):
    """Wheel navigation previews the highlighted palette just like arrow keys."""

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self.scroll_to_highlight)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        event.prevent_default()
        if not self.disabled:
            self.highlighted = min(self.option_count - 1, (self.highlighted or 0) + 1)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        event.prevent_default()
        if not self.disabled:
            self.highlighted = max(0, (self.highlighted or 0) - 1)


class ThemeScreen(FormModal):
    DEFAULT_CSS = """
    ThemeScreen #theme-pages { height: 1fr; }
    ThemeScreen #theme-options {
        height: 1fr; padding: 0; border: none; background: $surface;
        scrollbar-size: 1 1;
    }
    ThemeScreen .theme-field { height: auto; }
    ThemeScreen .theme-field .field-label { margin-top: 0; width: 12; }
    ThemeScreen .theme-field Input { width: 1fr; }
    ThemeScreen #custom-theme-help { height: auto; color: $text-muted; }
    ThemeScreen .form-hint { text-wrap: nowrap; text-overflow: ellipsis; }
    """
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("ctrl+s", "save", "Apply", priority=True),
        Binding("ctrl+l", "reload", "Discard edits", priority=True),
        Binding("ctrl+e", "customize", "Customize", priority=True),
    ]

    def __init__(self, client, config: ThemeConfig, draft: ThemeDraft | None = None):
        super().__init__()
        self.client = client
        self.saved = config.model_copy(deep=True)
        self.draft = draft
        self.base = resolve_theme(config.name)
        self.custom_base = resolve_theme(config.custom_base or config.name)
        self.custom_colors = dict(config.custom_colors if config.custom_base else config.colors)
        self.has_custom = bool(config.custom_base or config.colors)
        self.selected = "custom" if config.colors else self.base
        self.committing = False
        self.editing = False
        self.initial_theme = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="form-dialog"):
            yield Static("Theme", classes="form-title")
            yield Static("TerminalColors palettes · preview as you move", classes="form-subtitle")
            with ContentSwitcher(initial="theme-picker", id="theme-pages"):
                with Vertical(id="theme-picker"):
                    yield ThemeOptions(*self.options(), id="theme-options", compact=True)
                with FormBody(id="theme-custom", classes="form-body"):
                    yield Static("", id="custom-theme-help", markup=False)
                    for name in COLOR_FIELDS:
                        with Horizontal(classes="theme-field"):
                            yield Static(name.title(), classes="field-label")
                            yield Input(id="color-" + name)
                        yield Static("", id="color-" + name + "-error",
                                     classes="field-error", markup=False)
            yield Static("", classes="form-status", markup=False)
            with Horizontal(classes="form-actions"):
                yield Button("Apply", id="theme-apply", flat=True)
                yield Button("Customize", id="theme-customize", flat=True)
                yield Button("Back", id="theme-picker-back", flat=True)
                yield Button("Close", id="theme-close", flat=True)
            yield Static("", classes="form-hint", markup=False)
        yield Static("Terminal too small — need 48×12. Resize or press Esc to close.",
                     classes="form-too-small")

    def options(self) -> list[Option]:
        options = []
        active = "custom" if self.saved.colors else resolve_theme(self.saved.name)
        for name, label in THEME_LABELS.items():
            options.append(Option(label + (" · saved" if name == active else ""),
                                  id=name))
        if self.has_custom:
            options.append(Option("Custom" + (" · saved" if active == "custom" else ""), id="custom"))
        return options

    def on_mount(self) -> None:
        self.initial_theme = self.app.theme
        self.resize_state()
        self.query_one("#theme-picker-back").display = False
        options = self.query_one("#theme-options", OptionList)
        options.highlighted = next(i for i, option in enumerate(self.options())
                                   if option.id == self.selected)
        options.focus()
        self.status("")
        self.call_after_refresh(options.scroll_to_highlight)

    def responsive_copy(self, width: int) -> None:
        hint = ("Ctrl+S apply · Ctrl+L reset · Esc back" if self.editing
                else "↑↓ preview · Enter apply · Ctrl+E customize · Esc close")
        if self.has_class("ascii"):
            hint = hint.replace("↑↓", "Up/Down").replace(" · ", " | ")
        if width < 60:
            hint = "^S apply | ^L reset | Esc back" if self.editing else "Up/Down | Enter | ^E edit | Esc"
        self.query_one(".form-hint", Static).update(hint)
        if not self.editing:
            self.call_after_refresh(self.query_one("#theme-options", OptionList).scroll_to_highlight)

    def status(self, message: str, *, problem: bool = False) -> None:
        super().status(message, problem=problem)
        self.query_one(".form-status").display = bool(message)

    def preview(self, base: str, colors: dict[str, str]) -> None:
        self.app.apply_theme(base, colors)

    @on(OptionList.OptionHighlighted, "#theme-options")
    def highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.editing or self.committing:
            return
        self.selected = event.option.id
        if self.selected == "custom":
            self.base = self.custom_base
            self.preview(self.base, self.custom_colors)
        else:
            self.base = self.selected
            self.preview(self.base, {})

    @on(OptionList.OptionSelected, "#theme-options")
    def selected_option(self) -> None:
        self.action_save()

    def action_customize(self) -> None:
        if self.committing or self.editing:
            return
        self.editing = True
        if self.draft is not None:
            self.base = self.draft.base
            values = self.draft.values
        else:
            colors = self.custom_colors if self.selected == "custom" else {}
            values = {name: colors.get(name, PALETTES[self.base][name]) for name in COLOR_FIELDS}
        with self.prevent(Input.Changed):
            for name, value in values.items():
                self.query_one("#color-" + name, Input).value = value
        self.query_one("#custom-theme-help", Static).update(
            f"Custom from {THEME_LABELS[self.base]} · use #RRGGBB; blank uses the base color."
        )
        self.query_one("#theme-pages", ContentSwitcher).current = "theme-custom"
        self.query_one("#theme-customize").display = False
        self.query_one("#theme-picker-back").display = True
        self.responsive_copy(self.app.size.width)
        self.status("Edit colors to preview. Ctrl+S saves your custom theme.")
        self.preview_fields()
        self.query_one("#color-primary", Input).focus()

    def read_colors(self) -> dict[str, str]:
        colors = {}
        for name in COLOR_FIELDS:
            value = self.query_one("#color-" + name, Input).value.strip()
            if value and value != PALETTES[self.base][name]:
                validate_theme(self.base, {name: value})
                colors[name] = value
        return colors

    def preview_fields(self, *, submit: bool = False) -> bool:
        from textual.color import Color, ColorParseError

        self.clear_errors()
        invalid = False
        for name in COLOR_FIELDS:
            control = self.query_one("#color-" + name, Input)
            try:
                if control.value.strip():
                    Color.parse(control.value.strip())
            except ColorParseError:
                invalid = True
                error = self.query_one("#color-" + name + "-error", Static)
                error.update("Enter a color such as #7aa2f7, or leave blank.")
                error.add_class("problem")
                if submit:
                    control.focus()
                    self.call_after_refresh(error.scroll_visible, animate=False)
                    break
        if invalid:
            self.status("Fix the color beside the marked field.", problem=True)
            return False
        self.preview(self.base, self.read_colors())
        self.status("Unsaved custom theme · Ctrl+S applies it.")
        return True

    @on(Input.Changed)
    def color_changed(self) -> None:
        if self.editing and not self.committing:
            self.preview_fields()
            self.remember_draft()

    def remember_draft(self) -> None:
        if self.editing and not self._committed:
            self.draft = ThemeDraft(self.base, {name: self.query_one("#color-" + name, Input).value
                                                for name in COLOR_FIELDS})
            self.app.theme_draft = self.draft

    def back_to_picker(self) -> None:
        self.remember_draft()
        self.editing = False
        self.query_one("#theme-pages", ContentSwitcher).current = "theme-picker"
        self.query_one("#theme-customize").display = True
        self.query_one("#theme-picker-back").display = False
        self.responsive_copy(self.app.size.width)
        options = self.query_one("#theme-options", OptionList)
        # Highlight may not change, so explicitly restore that preview.
        selected = options.get_option_at_index(options.highlighted or 0).id
        self.selected = selected
        self.base = self.custom_base if selected == "custom" else selected
        self.preview(self.base, self.custom_colors if selected == "custom" else {})
        self.status("")
        self.call_after_refresh(options.scroll_to_highlight)
        options.focus()

    def action_close(self) -> None:
        if self.committing:
            self.status("Saving is in progress. This dialog closes when it finishes.")
        elif self.editing:
            self.back_to_picker()
        else:
            self.close_dialog()

    def close_dialog(self) -> None:
        if not self.committing:
            self.remember_draft()
            self.dismiss(None)

    def on_unmount(self) -> None:
        if not self._committed and self.initial_theme is not None:
            # Re-register the saved custom colors: previews share one theme name.
            if self.initial_theme == "eidolon-custom":
                self.app.apply_theme(resolve_theme(self.saved.name), self.saved.colors)
            else:
                self.app.theme = self.initial_theme

    def action_reload(self) -> None:
        if self.committing:
            return
        self.draft = None
        self.app.theme_draft = None
        if self.editing:
            with self.prevent(Input.Changed):
                for name in COLOR_FIELDS:
                    value = (self.custom_colors if self.selected == "custom" else {}).get(
                        name, PALETTES[self.base][name]
                    )
                    self.query_one("#color-" + name, Input).value = value
            self.preview_fields()
        else:
            self.base = resolve_theme(self.saved.name)
            self.selected = "custom" if self.saved.colors else self.base
            self.query_one("#theme-options", OptionList).highlighted = next(
                i for i, option in enumerate(self.options()) if option.id == self.selected
            )
            self.preview(self.base, self.saved.colors)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions = {"theme-apply": self.action_save, "theme-customize": self.action_customize,
                   "theme-picker-back": self.back_to_picker, "theme-close": self.close_dialog}
        actions[event.button.id]()

    @work(group="theme-save", exit_on_error=False)
    async def action_save(self) -> None:
        if self.committing:
            return
        if self.editing and not self.preview_fields(submit=True):
            return
        self.committing = True
        self.query("Input, OptionList, Button").set(disabled=True)
        try:
            colors = self.read_colors() if self.editing else (
                self.custom_colors if self.selected == "custom" else {}
            )
            if self.editing:
                # A full custom palette keeps the saved Custom slot distinct even
                # when the user accepts every color from its starting palette.
                colors = {**{name: PALETTES[self.base][name] for name in COLOR_FIELDS}, **colors}
            params = {"name": self.base, "colors": colors}
            if self.editing:
                params.update(custom_base=self.base, custom_colors=colors)
            data = await self.client.request("save_theme", **params)
            self.app.apply_theme(self.base, colors)
            self._committed = True
            if self.editing:
                self.app.theme_draft = None
            self.dismiss(data)
        except Exception as exc:
            self.status(str(exc), problem=True)
        finally:
            self.committing = False
            if self.is_mounted:
                self.query("Input, OptionList, Button").set(disabled=False)
