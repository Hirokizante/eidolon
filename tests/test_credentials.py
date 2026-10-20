"""Credential reuse must not reopen Keychain for each request or adapter."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import keyring
import pytest

from eidolon import cli, config
from eidolon.config import (
    AppConfig, clear_secret_cache, delete_secret, initialize_config, resolve_secret, store_secret,
)
from eidolon.engine import ConversationEngine
from eidolon.providers import OpenAIProvider
from eidolon.storage import Store


@pytest.fixture
def keychain(monkeypatch):
    monkeypatch.setattr(config, "_keychain_values", {})
    values = {"one": "first-test-value", "two": "second-test-value"}
    read = Mock(side_effect=lambda service, name: values.get(name))
    write = Mock(side_effect=lambda service, name, value: values.__setitem__(name, value))
    delete = Mock(side_effect=lambda service, name: values.pop(name))
    monkeypatch.setattr(keyring, "get_password", read)
    monkeypatch.setattr(keyring, "set_password", write)
    monkeypatch.setattr(keyring, "delete_password", delete)
    return SimpleNamespace(values=values, read=read, write=write, delete=delete)


def test_successful_reads_are_shared_and_cached_per_reference(keychain):
    for _ in range(4):
        assert resolve_secret("keyring:one") == "first-test-value"
        assert resolve_secret("keyring:two") == "second-test-value"
    assert keychain.read.call_count == 2
    assert keychain.read.call_args_list[0].args == ("eidolon", "one")
    assert not keychain.write.called


def test_concurrent_first_reads_open_keychain_only_once(keychain):
    entered, release = Event(), Event()

    def read(service, name):
        entered.set()
        assert release.wait(3)
        return keychain.values[name]

    keychain.read.side_effect = read
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(resolve_secret, "keyring:one") for _ in range(8)]
        try:
            assert entered.wait(3)
        finally:
            release.set()
        assert [future.result(timeout=3) for future in futures] == ["first-test-value"] * 8
    keychain.read.assert_called_once_with("eidolon", "one")


@pytest.mark.parametrize("failure", [None, "", keyring.errors.KeyringError("Access denied")])
def test_missing_or_failed_reads_can_be_retried_after_access_is_granted(keychain, failure):
    keychain.read.side_effect = [failure, "available-test-value"]
    with pytest.raises((ValueError, keyring.errors.KeyringError)):
        resolve_secret("keyring:one")
    assert resolve_secret("keyring:one") == "available-test-value"
    assert resolve_secret("keyring:one") == "available-test-value"
    assert keychain.read.call_count == 2


def test_storing_a_credential_replaces_cache_without_another_keychain_read(keychain):
    assert resolve_secret("keyring:one") == "first-test-value"
    assert store_secret("one", "updated-test-value") == "keyring:one"
    assert resolve_secret("keyring:one") == "updated-test-value"
    assert keychain.read.call_count == 1
    keychain.write.assert_called_once_with("eidolon", "one", "updated-test-value")


def test_failed_writes_do_not_cache_an_unstored_value(keychain):
    resolve_secret("keyring:one")
    keychain.write.side_effect = keyring.errors.KeyringError("Access denied")
    with pytest.raises(keyring.errors.KeyringError):
        store_secret("one", "unstored-test-value")
    assert resolve_secret("keyring:one") == "first-test-value"
    assert keychain.read.call_count == 2


def test_delete_and_targeted_invalidation_do_not_return_stale_credentials(keychain):
    resolve_secret("keyring:one")
    resolve_secret("keyring:two")
    keychain.values["one"] = "externally-updated-test-value"
    clear_secret_cache("keyring:one")
    assert resolve_secret("keyring:one") == "externally-updated-test-value"
    assert resolve_secret("keyring:two") == "second-test-value"
    assert keychain.read.call_count == 3
    assert delete_secret("one") == "keyring:one"
    with pytest.raises(ValueError, match="Missing Keychain credential"):
        resolve_secret("keyring:one")
    keychain.delete.assert_called_once_with("eidolon", "one")


def test_environment_credentials_remain_live_and_do_not_read_keychain(keychain, monkeypatch):
    monkeypatch.setenv("EIDOLON_TEST_CREDENTIAL", "original-test-value")
    assert resolve_secret("env:EIDOLON_TEST_CREDENTIAL") == "original-test-value"
    monkeypatch.setenv("EIDOLON_TEST_CREDENTIAL", "updated-test-value")
    assert resolve_secret("env:EIDOLON_TEST_CREDENTIAL") == "updated-test-value"
    monkeypatch.delenv("EIDOLON_TEST_CREDENTIAL")
    with pytest.raises(ValueError, match="Missing environment credential"):
        resolve_secret("env:EIDOLON_TEST_CREDENTIAL")
    assert not keychain.read.called


@pytest.mark.parametrize("action", ["set", "delete"])
def test_cli_changes_notify_service_by_reference_without_exposing_values(
    keychain, tmp_path, monkeypatch, capsys, action,
):
    notify = AsyncMock()
    monkeypatch.setattr(cli, "_notify_secret_change", notify)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "updated-test-value")
    configuration = tmp_path / "config.yaml"
    cli.main(["--config", str(configuration), "secret", action, "one"])
    notify.assert_awaited_once_with(configuration, "keyring:one")
    assert "test-value" not in capsys.readouterr().out
    if action == "set":
        assert resolve_secret("keyring:one") == "updated-test-value"
    else:
        with pytest.raises(ValueError):
            resolve_secret("keyring:one")


@pytest.mark.parametrize("failure", [None, ConnectionError(), RuntimeError(), TimeoutError()])
async def test_cli_cache_notification_works_with_running_or_unavailable_service(
    tmp_path, monkeypatch, failure,
):
    monkeypatch.setattr(cli, "load_config", lambda _: AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path))
    request = AsyncMock(side_effect=failure)
    monkeypatch.setattr("eidolon.service.client.RoomClient.request", request)
    await cli._notify_secret_change(tmp_path / "config.yaml", "keyring:one")
    request.assert_awaited_once_with("clear_secret_cache", reference="keyring:one")


async def test_two_messages_and_discovery_share_one_keychain_authorization(keychain, tmp_path):
    cfg = initialize_config(state_dir=tmp_path / "state")
    cfg.memory.enabled = False
    cfg.memory.save_enabled = False
    profile = cfg.selected_provider()
    profile.presentation = "plain"
    profile.api_key_ref = "keyring:one"
    profile.headers = {"X-API-Key": "keyring:one"}
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer first-test-value"
        assert request.headers["X-API-Key"] == "first-test-value"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        return httpx.Response(200, content=(
            'data: {"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n\n'
        ))

    store = Store(cfg.database_path)
    store.create_session("chat", kind="chat")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(profile, client=client)
        engine = ConversationEngine(cfg, store, provider=provider)
        try:
            for message in ("First message", "Second message"):
                events = [event async for event in engine.generate("chat", message)]
                assert events[-1]["type"] == "completed", events
                # Rebuilding an adapter must not lose the process-wide cache.
                engine.provider = OpenAIProvider(profile, client=client)
            await OpenAIProvider(profile, client=client).list_models()
            assert len(store.list_messages("chat")) == 4
        finally:
            await engine.close()
            store.close()
    assert len(requests) == 3
    keychain.read.assert_called_once_with("eidolon", "one")
