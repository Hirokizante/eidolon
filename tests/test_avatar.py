from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from eidolon.avatar import AvatarCoordinator
from eidolon.config import (
    AppConfig, AvatarConfig, MCPServerConfig, initialize_config, load_config, save_config,
)
from eidolon.preferences import merge_preferences


@pytest.fixture(autouse=True)
def isolated_avatar_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    def reject_credential_write(*args):
        pytest.fail("Avatar setup unexpectedly accessed live credential storage")

    monkeypatch.setattr("keyring.set_password", reject_credential_write)


def test_avatar_profile_has_a_neutral_default():
    assert AvatarConfig().profile == "companion"
    assert AvatarConfig.model_validate({"enabled": True}).profile == "companion"


@pytest.mark.parametrize("profile", [
    "stage-avatar-42", "顔 🌙 [route] {profile}", "slot-" + "x" * 100, "  stage-alpha  ",
])
def test_avatar_profile_preserves_literal_routing_text(profile):
    config = AvatarConfig(profile=profile)
    assert config.profile == profile
    config.profile = profile + "-next"
    assert config.profile == profile + "-next"


@pytest.mark.parametrize("profile", [
    None, 0, True, {}, [], "", "   ", "\t", "bridge\n", "\rbridge",
    "bridge\u0085name", "bridge\u2028name", "bridge\u2029name", "bad\x00name",
    "bad\x1b[31m", "bad\ud800name",
])
def test_avatar_profile_rejects_invalid_input_without_echo_or_assignment(profile):
    config = AvatarConfig()
    for apply in (lambda: AvatarConfig(profile=profile),
                  lambda: setattr(config, "profile", profile)):
        with pytest.raises(ValidationError) as error:
            apply()
        assert error.value.errors()[0]["loc"] == ("profile",)
        assert "input_value" not in str(error.value)
    assert config.profile == "companion"


@pytest.mark.parametrize("suffix", ["yaml", "json"])
def test_avatar_profile_loads_older_config_and_round_trips_independently(tmp_path, suffix):
    config = initialize_config(state_dir=tmp_path / "state")
    config.identity.user_name = "Aster"
    config.identity.companion_name = "Nova"
    path = config.state_dir / f"config.{suffix}"
    values = config.model_dump(mode="json")
    values["avatar"].pop("profile", None)
    path.write_text(json.dumps(values) if suffix == "json" else yaml.safe_dump(values))

    loaded = load_config(path)
    assert loaded.avatar.profile == "companion"
    assert loaded.model_dump(mode="json") | {"avatar": values["avatar"]} == values
    loaded.avatar.profile = "stage-月 [primary]"
    save_config(loaded, path)
    assert load_config(path) == loaded
    assert loaded.identity == config.identity


class Body:
    def __init__(self, delay=0):
        self.delay, self.calls = delay, []

    async def call(self, server, tool, arguments):
        if tool == "list_emotes":
            return {
                "structured": {
                    "expressions": ["happy", "sad"],
                    "emotes": {"wave": {}, "stretch": {}},
                }
            }
        self.calls.append((tool, arguments))
        await asyncio.sleep(self.delay)
        if tool == "play_emote":
            return {"content": [{"type": "text", "text": "rejected: cooldown"}]}
        return {"content": [{"type": "text", "text": "accepted"}]}


async def test_cues_are_nonblocking_independent_once_and_cancel_stale():
    body = Body(delay=0.05)
    avatar = AvatarCoordinator(AvatarConfig(enabled=True), body)
    try:
        await avatar.catalog()
        await avatar.begin_turn("s", "t", "a")
        assert avatar.enqueue("gesture", "wave", "s", "t", "a")
        assert avatar.enqueue("expression", "happy", "s", "t", "a")
        assert not avatar.enqueue("gesture", "stretch", "s", "t", "a")
        assert not avatar.enqueue("expression", "not-catalog", "s", "t", "a")
        # Synchronous admission returns before the body can acknowledge.
        assert body.calls == []
        await asyncio.sleep(0.01)
        assert len(body.calls) == 2
        await avatar.cancel_turn("s", "t", "a")
        assert not avatar.enqueue("gesture", "wave", "s", "t", "a")
        await avatar.begin_turn("s", "t", "retry")
        assert not avatar.enqueue("gesture", "wave", "s", "t", "retry")
        await asyncio.sleep(0.06)
        assert len(body.calls) == 2
        assert any(
            item["kind"] == "expression" and item["state"] == "acknowledged"
            for item in avatar.diagnostics
        )
        await avatar.end_turn("s", "t", "retry")
    finally:
        await avatar.close()


async def test_unsent_cues_expire_and_unknown_outcome_does_not_retry():
    body = Body()
    avatar = AvatarCoordinator(AvatarConfig(enabled=True), body, cue_ttl=-1)
    try:
        await avatar.catalog()
        await avatar.begin_turn("s", "t", "a")
        avatar.enqueue("gesture", "wave", "s", "t", "a")
        await asyncio.sleep(0)
        assert body.calls == []
        await avatar.end_turn("s", "t", "a")
    finally:
        await avatar.close()

    class Disconnect(Body):
        async def call(self, server, tool, arguments):
            if tool == "list_emotes":
                return await super().call(server, tool, arguments)
            self.calls.append((tool, arguments))
            raise TimeoutError()

    body = Disconnect()
    avatar = AvatarCoordinator(AvatarConfig(enabled=True), body)
    try:
        await avatar.catalog()
        await avatar.begin_turn("s", "t", "a")
        avatar.enqueue("gesture", "wave", "s", "t", "a")
        await asyncio.sleep(0.01)
        await avatar.catalog(refresh=True)
        assert len(body.calls) == 1
        assert any(item["state"] == "unknown_outcome" for item in avatar.diagnostics)
    finally:
        await avatar.close()


@pytest.mark.parametrize("profile", [None, "stage-avatar-42", '顔 "primary" 🌙'])
async def test_lifecycle_matches_exact_vessel_raw_byte_signature(tmp_path, monkeypatch, profile):
    monkeypatch.setenv("TEST_AVATAR_SIGNING_KEY", "local-secret")
    payloads = []
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state", avatar={
        "enabled": True,
        "server": "stage-body",
        "lifecycle_url": "http://127.0.0.1:8766/events",
        "signing_key_ref": "env:TEST_AVATAR_SIGNING_KEY",
        **({"profile": profile} if profile is not None else {}),
    })
    expected_profile = profile if profile is not None else "companion"

    def endpoint(request):
        assert request.url.path == "/events"
        expected = (
            "sha256=" + hmac.new(b"local-secret", request.content, hashlib.sha256).hexdigest()
        )
        assert request.headers["x-hermes-signature-256"] == expected
        payload = json.loads(request.content)
        assert payload["profile"] == expected_profile
        assert "reply" not in payload and "prompt" not in payload
        assert set(payload) == {
            "hook_event_name", "profile", "session_id", "extra", "delivery_id", "timestamp",
        }
        payloads.append(payload)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        avatar = AvatarCoordinator(config.avatar, Body(), client=client)
        try:
            await avatar.begin_turn("session", "turn", "attempt")
            await avatar.begin_turn("session", "turn", "attempt")
            config = merge_preferences(config, {"user_name": "Aster", "companion_name": "Nova"})
            avatar.config = config.avatar
            await avatar.begin_turn("session", "turn", "retry")
            await avatar.end_turn("session", "turn", "retry")
            await avatar.end_turn("session", "turn", "retry")
            await avatar.session_end("session")
        finally:
            await avatar.close()
    assert [payload["hook_event_name"] for payload in payloads] == [
        "pre_llm_call",
        "post_llm_call",
        "on_session_end",
    ]
    assert payloads[0]["extra"]["turn_id"] == "turn"
    assert all(payload["session_id"] == "session" for payload in payloads)
    assert len({payload["delivery_id"] for payload in payloads}) == 3


@pytest.mark.parametrize("cleanup", ["cancel", "close"])
@pytest.mark.parametrize("status_code", [204, 503])
async def test_slow_lifecycle_preserves_cues_cleanup_and_failure_isolation(
    monkeypatch, cleanup, status_code,
):
    monkeypatch.setenv("TEST_AVATAR_SIGNING_KEY", "local-secret")
    entered, release = asyncio.Event(), asyncio.Event()
    payloads = []

    async def endpoint(request):
        entered.set()
        await release.wait()
        payloads.append(json.loads(request.content))
        return httpx.Response(status_code)

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        body = Body()
        avatar = AvatarCoordinator(AvatarConfig(
            enabled=True, profile="stage-cleanup", signing_key_ref="env:TEST_AVATAR_SIGNING_KEY",
        ), body, client=client)
        closing = None
        try:
            await asyncio.wait_for(avatar.begin_turn("session", "turn", "attempt"), 1)
            await asyncio.wait_for(entered.wait(), 1)
            await avatar.catalog()
            assert avatar.enqueue("expression", "happy", "session", "turn", "attempt")
            await asyncio.wait_for(avatar._queues["expression"].join(), 1)
            assert body.calls == [("set_expression", {"name": "happy"})]
            assert payloads == []
            if cleanup == "cancel":
                await asyncio.wait_for(avatar.cancel_turn("session", "turn", "attempt"), 1)
                await avatar.cancel_turn("session", "turn", "attempt")
            else:
                closing = asyncio.create_task(avatar.close())
                await asyncio.sleep(0)
                assert not closing.done()
        finally:
            release.set()
            if closing is not None:
                await closing
            await avatar.close()

    assert [payload["hook_event_name"] for payload in payloads] == ["pre_llm_call", "post_llm_call"]
    assert all(payload["profile"] == "stage-cleanup" for payload in payloads)
    state = "delivered" if status_code == 204 else "unavailable"
    assert sum(item["state"] == state for item in avatar.diagnostics) == 2


def test_standalone_setup_copies_refs_once_without_runtime_source_link(tmp_path, monkeypatch):
    from eidolon.avatar.setup import configure_existing_vessel
    from eidolon.config import AppConfig

    project = tmp_path / "synthetic-installation"
    body = project / "motion-bridge"
    body.mkdir(parents=True)
    (body / "server.py").write_text("# standalone")
    (body / "pyproject.toml").write_text('[project]\nname="body"')
    source = project / "selected.env"
    source.write_text(
        'STAGE_SIGNING="fixture signing"\nexport STAGE_ACCESS=fixture-token # comment\n'
        'UNRELATED="ignored invalid quoting\n'
    )
    copied = {}
    monkeypatch.setattr("eidolon.avatar.setup.shutil.which", lambda _: "/standalone/uv")

    def credentials(name, value):
        copied[name] = value
        return "keyring:" + name

    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state", avatar={
        "server": "stage-bridge", "profile": "stage-42",
        "lifecycle_url": "http://127.0.0.1:9876/events",
    })
    report = configure_existing_vessel(
        config, body, credential_file=source, body_token_env="STAGE_ACCESS",
        signing_key_env="STAGE_SIGNING", credential_store=credentials,
    )
    assert copied == {"vessel-signing": "fixture signing", "vessel-body-token": "fixture-token"}
    assert config.avatar.enabled and config.avatar.signing_key_ref == "keyring:vessel-signing"
    assert config.avatar.server == "stage-bridge" and config.avatar.profile == "stage-42"
    assert config.avatar.lifecycle_url == "http://127.0.0.1:9876/events"
    server = config.mcp_servers[0]
    assert server.name == "stage-bridge" and server.command == "/standalone/uv"
    assert server.args == ["run", "--directory", str(body), "python", str(body / "server.py")]
    assert server.env == {"STAGE_ACCESS": "keyring:vessel-body-token"}
    assert server.timeout == 5
    assert server.enabled_tools == ["list_emotes", "get_body_state", "set_expression", "play_emote"]
    assert report == {"configured": True, "signing_configured": True,
                      "body_token_configured": True}
    assert str(source) not in config.model_dump_json()
    assert "fixture signing" not in config.model_dump_json()
    assert "fixture-token" not in config.model_dump_json()


def test_standalone_setup_without_selected_fields_never_reads_credentials(tmp_path, monkeypatch):
    from eidolon.avatar.setup import configure_existing_vessel

    body = tmp_path / "arbitrary-bridge"
    body.mkdir()
    (body / "server.py").touch()
    (body / "pyproject.toml").touch()
    monkeypatch.setattr("eidolon.avatar.setup.shutil.which", lambda _: "/standalone/uv")

    def reject_discovery(*args):
        pytest.fail("Setup unexpectedly read a source file or discovered the home directory")

    monkeypatch.setattr(Path, "read_text", reject_discovery)
    monkeypatch.setattr(Path, "home", classmethod(reject_discovery))
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state", timezone="UTC", avatar={"server": "stage-bridge"})

    report = configure_existing_vessel(config, body, credential_file=tmp_path / "unselected.env")

    assert config.mcp_servers[0].env == {}
    assert config.avatar.profile == "companion" and config.avatar.signing_key_ref is None
    assert report == {"configured": True, "signing_configured": False,
                      "body_token_configured": False}


@pytest.fixture
def synthetic_bridge(tmp_path, monkeypatch):
    bridge = tmp_path / "synthetic installation/橋"
    bridge.mkdir(parents=True)
    (bridge / "server.py").touch()
    (bridge / "pyproject.toml").touch()
    monkeypatch.setattr("eidolon.avatar.setup.shutil.which", lambda _: "/standalone/uv")
    return bridge


@pytest.mark.parametrize("names", [("Orion", "Vega"), ("星 [user]", "Echo {name}")])
@pytest.mark.parametrize("fields", [{}, {"body_token_env": "STAGE_ACCESS"},
                                   {"signing_key_env": "STAGE_SIGNING"}])
def test_standalone_setup_imports_only_selected_fields_independently_of_names(
    tmp_path, synthetic_bridge, names, fields,
):
    from eidolon.avatar.setup import configure_existing_vessel

    source = tmp_path / "selected.env"
    source.write_text("STAGE_ACCESS=fixture-token\nSTAGE_SIGNING=fixture-signing\n")
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state", identity={
        "user_name": names[0], "companion_name": names[1],
    }, avatar={"server": "stage-bridge", "profile": "external-slot-7"})
    copied = {}

    def store(name, value):
        copied[name] = value
        return "keyring:" + name

    configure_existing_vessel(config, synthetic_bridge, credential_file=source,
                              credential_store=store, **fields)

    assert copied == ({"vessel-body-token": "fixture-token"} if "body_token_env" in fields else
                      {"vessel-signing": "fixture-signing"} if "signing_key_env" in fields else {})
    assert config.avatar.server == "stage-bridge" and config.avatar.profile == "external-slot-7"
    assert bool(config.mcp_servers[0].env) == ("body_token_env" in fields)
    assert bool(config.avatar.signing_key_ref) == ("signing_key_env" in fields)


def test_standalone_setup_rerun_preserves_routes_refs_and_other_servers(tmp_path, synthetic_bridge):
    from eidolon.avatar.setup import configure_existing_vessel

    other = MCPServerConfig(name="another-server", command="fixture-command")
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state", mcp_servers=[
        other, MCPServerConfig(name="stage-bridge", env={"STAGE_ACCESS": "env:EXAMPLE_ACCESS"}),
    ], avatar={"server": "stage-bridge", "profile": "external-slot-7",
               "signing_key_ref": "env:EXAMPLE_SIGNING"})

    for _ in range(2):
        report = configure_existing_vessel(config, synthetic_bridge)

    assert len(config.mcp_servers) == 2 and config.mcp_servers[0] == other
    assert config.mcp_servers[1].env == {"STAGE_ACCESS": "env:EXAMPLE_ACCESS"}
    assert config.avatar.server == "stage-bridge" and config.avatar.profile == "external-slot-7"
    assert config.avatar.signing_key_ref == "env:EXAMPLE_SIGNING"
    assert report == {"configured": True, "signing_configured": True,
                      "body_token_configured": True}


@pytest.mark.parametrize("field", ["", " ", "SOURCE-FIELD", "SOURCE=FIELD", "SOURCE\nFIELD"])
@pytest.mark.parametrize("option", ["body_token_env", "signing_key_env"])
def test_standalone_setup_rejects_invalid_field_names_before_copying(
    tmp_path, synthetic_bridge, field, option,
):
    from eidolon.avatar.setup import configure_existing_vessel

    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state")
    before = config.model_dump()
    with pytest.raises(ValueError, match="environment fields") as error:
        configure_existing_vessel(config, synthetic_bridge, **{option: field})
    assert config.model_dump() == before
    assert repr(field) not in str(error.value)


@pytest.mark.parametrize("content", [
    "UNSELECTED=fixture-secret\n", "STAGE_SIGNING=\n", 'STAGE_SIGNING=""\n',
    'STAGE_SIGNING="fixture-secret\n', "STAGE_SIGNING=fixture-secret extra\n",
    "STAGE_SIGNING=fixture-secret\nSTAGE_ACCESS=\n",
])
def test_standalone_setup_validates_all_selected_values_before_copying(
    tmp_path, synthetic_bridge, content,
):
    from eidolon.avatar.setup import configure_existing_vessel

    source = tmp_path / "selected.env"
    source.write_text(content)
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state")
    before = config.model_dump()
    with pytest.raises(ValueError) as error:
        configure_existing_vessel(config, synthetic_bridge, credential_file=source,
                                  signing_key_env="STAGE_SIGNING", body_token_env="STAGE_ACCESS")
    assert "fixture-secret" not in str(error.value) and str(source) not in str(error.value)
    assert config.model_dump() == before


@pytest.mark.parametrize("source_kind", ["omitted", "missing", "directory", "invalid-utf8"])
def test_standalone_setup_requires_a_readable_explicit_credential_source(
    tmp_path, synthetic_bridge, source_kind,
):
    from eidolon.avatar.setup import configure_existing_vessel

    source = tmp_path / "selected.env"
    if source_kind == "directory":
        source.mkdir()
    elif source_kind == "invalid-utf8":
        source.write_bytes(b"STAGE_SIGNING=\xff")
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state")
    before = config.model_dump()
    with pytest.raises(ValueError) as error:
        configure_existing_vessel(
            config, synthetic_bridge, signing_key_env="STAGE_SIGNING",
            credential_file=None if source_kind == "omitted" else source,
        )
    assert str(source) not in str(error.value)
    assert config.model_dump() == before


@pytest.mark.parametrize("bridge_kind", ["omitted", "blank", "missing", "no-server", "no-project"])
def test_standalone_setup_requires_an_explicit_complete_bridge(
    tmp_path, synthetic_bridge, bridge_kind,
):
    from eidolon.avatar.setup import configure_existing_vessel

    bridge = synthetic_bridge
    if bridge_kind == "omitted":
        bridge = None
    elif bridge_kind == "blank":
        bridge = "  "
    elif bridge_kind == "missing":
        bridge = tmp_path / "missing-bridge"
    else:
        (bridge / ("server.py" if bridge_kind == "no-server" else "pyproject.toml")).unlink()
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state")
    before = config.model_dump()
    with pytest.raises(ValueError) as error:
        configure_existing_vessel(config, bridge)
    assert str(tmp_path) not in str(error.value)
    assert config.model_dump() == before


def test_standalone_setup_requires_uv_on_path_without_home_discovery(
    tmp_path, synthetic_bridge, monkeypatch,
):
    from eidolon.avatar.setup import configure_existing_vessel

    def reject_home(cls):
        pytest.fail("Setup unexpectedly searched the home directory for uv")

    monkeypatch.setattr(Path, "home", classmethod(reject_home))
    monkeypatch.setattr("eidolon.avatar.setup.shutil.which", lambda _: None)
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state")
    with pytest.raises(ValueError, match="uv on PATH"):
        configure_existing_vessel(config, synthetic_bridge)
    assert not config.avatar.enabled and not config.mcp_servers


def test_standalone_setup_path_errors_do_not_disclose_source(tmp_path, monkeypatch):
    from eidolon.avatar.setup import configure_existing_vessel

    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state")
    before = config.model_dump()

    def fail_resolve(path):
        raise OSError(f"Could not resolve private source: {path}")

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    with pytest.raises(ValueError) as error:
        configure_existing_vessel(config, tmp_path / "bridge")
    assert str(tmp_path) not in str(error.value)
    assert config.model_dump() == before


@pytest.mark.parametrize("profile", ["", "bad\nroute"])
def test_standalone_setup_rejects_invalid_route_before_copying(tmp_path, synthetic_bridge, profile):
    from eidolon.avatar.setup import configure_existing_vessel

    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state")
    before = config.model_dump()
    with pytest.raises(ValueError):
        configure_existing_vessel(config, synthetic_bridge, profile=profile,
                                  signing_key_env="STAGE_SIGNING")
    assert config.model_dump() == before


def test_standalone_setup_storage_failure_preserves_config_and_hides_secrets(
    tmp_path, synthetic_bridge,
):
    from eidolon.avatar.setup import configure_existing_vessel

    source = tmp_path / "selected.env"
    source.write_text("STAGE_SIGNING=fixture-secret\n")
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state")
    before = config.model_dump()

    def fail_store(name, value):
        raise RuntimeError(f"Storage failure: {source} {value}")

    with pytest.raises(ValueError, match="could not be stored") as error:
        configure_existing_vessel(config, synthetic_bridge, credential_file=source,
                                  signing_key_env="STAGE_SIGNING", credential_store=fail_store)
    assert str(source) not in str(error.value) and "fixture-secret" not in str(error.value)
    assert config.model_dump() == before


async def test_offline_catalog_is_not_cached_and_can_warm_without_restart():
    class Warming(Body):
        attempts = 0

        async def call(self, server, tool, arguments):
            self.attempts += 1
            if self.attempts == 1:
                return {"structured": {"result": "renderer offline"}}
            return await super().call(server, tool, arguments)

    avatar = AvatarCoordinator(AvatarConfig(enabled=True), Warming())
    try:
        assert await avatar.catalog() == {"expressions": [], "gestures": []}
        assert (await avatar.catalog())["gestures"] == ["wave", "stretch"]
    finally:
        await avatar.close()


async def test_explicit_actions_preserve_options_and_share_cue_dedup():
    body = Body()
    avatar = AvatarCoordinator(AvatarConfig(enabled=True), body)
    try:
        await avatar.catalog()
        await avatar.begin_turn("s", "t", "a")
        context = {"session_id": "s", "turn_id": "t", "attempt_id": "a"}
        result = await avatar.explicit(
            "expression",
            "happy",
            context,
            arguments={"name": "happy", "intensity": 0.5, "duration_ms": 2000},
        )
        assert result["queued"]
        assert not avatar.enqueue("expression", "happy", "s", "t", "a")
        await asyncio.sleep(0.01)
        assert body.calls == [
            ("set_expression", {"name": "happy", "intensity": 0.5, "duration_ms": 2000})
        ]
        diagnostic = next(item for item in avatar.diagnostics if item["state"] == "sent")
        assert diagnostic["session_id"] == "s" and diagnostic["sequence"] == 1
    finally:
        await avatar.close()
