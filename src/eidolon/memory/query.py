"""Small recall queries, with the current message ahead of historical context."""

from eidolon.config import IdentityConfig

RECALL_QUERY_BYTES = 800


def bounded_query(text: str, limit: int = RECALL_QUERY_BYTES) -> str:
    # Cut at a character boundary, including for emoji and non-Latin messages.
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def compose_recall_query(history: list[dict], message: str, *,
                         identity: IdentityConfig | None = None) -> str:
    identity = identity if identity is not None else IdentityConfig()
    current = bounded_query(identity.user_name + ": " + message)
    separator = "\nRecent conversation:\n"
    remaining = RECALL_QUERY_BYTES - len((current + separator).encode("utf-8"))
    if remaining <= 0 or not history:
        return current
    recent = []
    for record in reversed(history[-6:]):
        recorded = IdentityConfig.model_validate(record["identity"]) \
            if "identity" in record else identity
        names = {"user": recorded.user_name, "assistant": recorded.companion_name}
        label = names.get(record["role"], record["role"]) + ": "
        available = remaining - len(label.encode("utf-8")) - (1 if recent else 0)
        if available < 0:
            break  # Omit historical context rather than truncate its speaker's name.
        content = record["content"].encode("utf-8")
        excerpt = content[-available:].decode("utf-8", errors="ignore") if available else ""
        line = label + excerpt
        remaining -= len(line.encode("utf-8")) + (1 if recent else 0)
        recent.insert(0, line)
        if len(content) > available:
            break
    return current + separator + "\n".join(recent) if recent else current
