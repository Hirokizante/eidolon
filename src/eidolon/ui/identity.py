import re
import string

from rich.text import Text

from eidolon.config import IdentityConfig


def speaker_title(role: str, identity: IdentityConfig) -> Text:
    """Keep display names literal and leave nonparticipant role labels intact."""
    label = {"user": identity.user_name, "assistant": identity.companion_name}.get(
        role, role.title()
    )
    return Text(label)


def markdown_speaker(role: str, identity: IdentityConfig) -> str:
    """Escape name punctuation independently from the message's Markdown."""
    return re.sub(f"([{re.escape(string.punctuation)}])", r"\\\1", speaker_title(role, identity).plain)


def companion_copy(
    identity: IdentityConfig, prefix: str, suffix: str, *, width: int, ascii_mode: bool = False,
) -> str:
    """Fit the name by terminal cells, keeping surrounding controls visible."""
    name = Text(identity.companion_name)
    available = max(1, width - Text(prefix + suffix).cell_len)
    if ascii_mode and name.cell_len > available:
        name.truncate(max(0, available - 3), overflow="crop")
        name.append("." * min(3, available))
    else:
        name.truncate(available, overflow="ellipsis")
    return prefix + name.plain + suffix
