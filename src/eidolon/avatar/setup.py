"""Explicit one-time setup of an existing standalone Vessel bridge.

The running service uses app-owned configuration and credential references,
never the source credential file. Source fields are selected explicitly.
"""

from __future__ import annotations

import re
import shlex
import shutil
from collections.abc import Callable
from pathlib import Path

from ..config import AppConfig, MCPServerConfig, store_secret


def configure_existing_vessel(
    config: AppConfig, bridge: Path | str, *, credential_file: Path | None = None,
    body_token_env: str | None = None, signing_key_env: str | None = None,
    profile: str | None = None, credential_store: Callable[[str, str], str] | None = None,
) -> dict[str, bool]:
    """Configure a selected bridge and import only explicitly selected credentials.

    The body token keeps its source environment-field name for the external
    bridge. MCP and lifecycle routes otherwise come from private configuration.
    """
    if bridge is None or isinstance(bridge, str) and not bridge.strip():
        raise ValueError("An explicit standalone bridge directory is required")
    try:
        bridge = Path(bridge).expanduser().resolve()
        complete = (bridge / "server.py").is_file() and (bridge / "pyproject.toml").is_file()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("The selected standalone bridge directory could not be read") from None
    if not complete:
        raise ValueError("Standalone Vessel bridge was not found in the selected directory")
    executable = shutil.which("uv")
    if not executable:
        raise ValueError("Install uv on PATH to run the standalone Vessel bridge")
    avatar = config.avatar.model_copy(deep=True)
    if profile is not None:
        avatar.profile = profile
    selected = {field for field in (body_token_env, signing_key_env) if field is not None}
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field) for field in selected):
        raise ValueError("Credential sources must name environment fields")
    values = {}
    if selected:
        if credential_file is None:
            raise ValueError("An explicit credential file is required for selected fields")
        try:
            text = credential_file.expanduser().resolve().read_text(encoding="utf-8")
        except (OSError, RuntimeError, ValueError):
            raise ValueError("The selected credential file could not be read") from None
        for line in text.splitlines():
            key, separator, value = line.strip().removeprefix("export ").partition("=")
            key = key.strip()
            if not separator or key not in selected:
                continue
            try:
                parts = shlex.split(value, comments=True)
            except ValueError:
                raise ValueError("Selected credential fields have invalid quoting") from None
            if len(parts) != 1 or not parts[0]:
                raise ValueError("Selected credential fields must contain one nonempty value")
            values[key] = parts[0]
        if selected - values.keys():
            raise ValueError("Selected credential fields were not found in the source file")
    environment = next((dict(item.env) for item in config.mcp_servers
                        if item.name == avatar.server), {})
    credential_store = credential_store or store_secret
    try:
        if body_token_env is not None:
            environment[body_token_env] = credential_store(
                "vessel-body-token", values[body_token_env]
            )
        if signing_key_env is not None:
            avatar.signing_key_ref = credential_store("vessel-signing", values[signing_key_env])
    except Exception:
        raise ValueError(
            "Selected credentials could not be stored; check private credential storage"
        ) from None
    server = MCPServerConfig(
        name=avatar.server,
        command=executable,
        args=["run", "--directory", str(bridge), "python", str(bridge / "server.py")],
        env=environment,
        enabled_tools=["list_emotes", "get_body_state", "set_expression", "play_emote"],
        timeout=5,
    )
    avatar.enabled = True
    config.mcp_servers = [item for item in config.mcp_servers if item.name != server.name] + [server]
    config.avatar = avatar
    return {
        "configured": True,
        "signing_configured": bool(avatar.signing_key_ref),
        "body_token_configured": bool(environment),
    }
