"""Bundled TerminalColors palettes and shared validation (no network at runtime)."""

from textual.color import Color

DEFAULT_THEME = "tokyo-night"
COLOR_FIELDS = ("primary", "secondary", "accent", "background", "foreground",
                "surface", "panel", "success", "warning", "error")

THEME_LABELS = {
    "catppuccin-mocha": "Catppuccin Mocha",
    "dracula": "Dracula",
    "nord": "Nord",
    "gruvbox-dark": "Gruvbox Dark",
    "tokyo-night": "Tokyo Night",
    "rose-pine": "Rosé Pine",
    "everforest-dark": "Everforest Dark",
    "one-dark": "One Dark",
    "solarized-light": "Solarized Light",
    "github-light": "GitHub Light",
}
THEME_SOURCES = {
    "catppuccin-mocha": "https://terminalcolors.com/themes/catppuccin/mocha/",
    "dracula": "https://terminalcolors.com/themes/dracula/default/",
    "nord": "https://terminalcolors.com/themes/nord/default/",
    "gruvbox-dark": "https://terminalcolors.com/themes/gruvbox/dark/",
    "tokyo-night": "https://terminalcolors.com/themes/tokyo-night/default/",
    "rose-pine": "https://terminalcolors.com/themes/rose-pine/default/",
    "everforest-dark": "https://terminalcolors.com/themes/everforest/dark/",
    "one-dark": "https://terminalcolors.com/themes/one/dark/",
    "solarized-light": "https://terminalcolors.com/themes/solarized/light/",
    "github-light": "https://terminalcolors.com/themes/github/light/",
}

# ANSI blue/cyan/magenta/green/yellow/red map to semantic UI roles.
# Surfaces blend the source background toward its foreground by 4% and 8%.
# Gruvbox uses the bright ANSI set for readable accents on its dark background.
PALETTES = {
    "catppuccin-mocha": dict(
        primary='#89b4fa',
        secondary='#94e2d5',
        accent='#f5c2e7',
        background='#1e1e2e',
        foreground='#cdd6f4',
        surface='#252535',
        panel='#2c2c3d',
        success='#a6e3a1',
        warning='#f9e2af',
        error='#f38ba8',
        dark=True,
    ),
    "dracula": dict(
        primary='#bd93f9',
        secondary='#8be9fd',
        accent='#ff79c6',
        background='#282a36',
        foreground='#f8f8f2',
        surface='#30323d',
        panel='#383a45',
        success='#50fa7b',
        warning='#f1fa8c',
        error='#ff5555',
        dark=True,
    ),
    "nord": dict(
        primary='#81a1c1',
        secondary='#88c0d0',
        accent='#b48ead',
        background='#2e3440',
        foreground='#d8dee9',
        surface='#343a46',
        panel='#3b414d',
        success='#a3be8c',
        warning='#ebcb8b',
        error='#bf616a',
        dark=True,
    ),
    "gruvbox-dark": dict(
        primary='#83a598',
        secondary='#8ec07c',
        accent='#d3869b',
        background='#282828',
        foreground='#ebdbb2',
        surface='#2f2f2d',
        panel='#373633',
        success='#b8bb26',
        warning='#fabd2f',
        error='#fb4934',
        dark=True,
    ),
    "tokyo-night": dict(
        primary='#7aa2f7',
        secondary='#7dcfff',
        accent='#bb9af7',
        background='#1a1b26',
        foreground='#c0caf5',
        surface='#20222e',
        panel='#272936',
        success='#9ece6a',
        warning='#e0af68',
        error='#f7768e',
        dark=True,
    ),
    "rose-pine": dict(
        primary='#9ccfd8',
        secondary='#ebbcba',
        accent='#c4a7e7',
        background='#1f1d2e',
        foreground='#e0def4',
        surface='#262435',
        panel='#2e2c3d',
        success='#31748f',
        warning='#f6c177',
        error='#eb6f92',
        dark=True,
    ),
    "everforest-dark": dict(
        primary='#7fbbb3',
        secondary='#83c092',
        accent='#d699b6',
        background='#2d353b',
        foreground='#d3c6aa',
        surface='#333a3f',
        panel='#3a4043',
        success='#a7c080',
        warning='#dbbc7f',
        error='#e67e80',
        dark=True,
    ),
    "one-dark": dict(
        primary='#61afef',
        secondary='#56b6c2',
        accent='#c678dd',
        background='#282c34',
        foreground='#abb2bf',
        surface='#2d3139',
        panel='#32363f',
        success='#98c379',
        warning='#d19a66',
        error='#e06c75',
        dark=True,
    ),
    "solarized-light": dict(
        primary='#268bd2',
        secondary='#2aa198',
        accent='#d33682',
        background='#fdf6e3',
        foreground='#657b83',
        surface='#f6f1df',
        panel='#f0ecdb',
        success='#859900',
        warning='#b58900',
        error='#dc322f',
        dark=False,
    ),
    "github-light": dict(
        primary='#0969da',
        secondary='#1b7c83',
        accent='#8250df',
        background='#f6f8fa',
        foreground='#1f2328',
        surface='#edeff1',
        panel='#e4e6e9',
        success='#116329',
        warning='#4d2d00',
        error='#cf222e',
        dark=False,
    ),
}


def resolve_theme(name: str) -> str:
    """Resolve a bundled palette, falling back for unknown configuration values."""
    return name if name in PALETTES else DEFAULT_THEME


def validate_theme(name: str, colors: dict[str, str]) -> None:
    if name not in PALETTES:
        raise ValueError("Unknown theme. Choose " + ", ".join(PALETTES) + ".")
    if not isinstance(colors, dict) or any(key not in COLOR_FIELDS for key in colors):
        raise ValueError("Unknown theme color. Use " + ", ".join(COLOR_FIELDS) + ".")
    for value in colors.values():
        if not isinstance(value, str):
            raise ValueError("Theme colors must be color strings, such as #7aa2f7.")
        Color.parse(value)
