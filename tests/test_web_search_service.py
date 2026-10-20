import json

import pytest

from eidolon.config import load_config, save_config
from eidolon.service.runtime import RoomService
from test_service import room


@pytest.fixture(autouse=True)
def no_metadata_network(monkeypatch):
    monkeypatch.setattr(RoomService, "_schedule_metadata_refresh", lambda *args: None)


async def test_web_search_save_keeps_keys_private_merges_latest_and_replaces_only_search(
    tmp_path, monkeypatch
):
    stored = []

    def keychain(name, value):
        stored.append((name, value))
        return "keyring:" + name

    monkeypatch.setattr("eidolon.service.runtime.store_secret", keychain)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        engine, avatar, mcp, web = service.engine, service.avatar, service.mcp, service.tools.web
        latest = load_config(service.configuration)
        latest.web_search.exa.max_results = 12
        latest.theme.name = "nord"
        save_config(latest, service.configuration)
        result = await client.request(
            "save_preferences_with_web_search",
            preferences={"follow_replies": False},
            web_search={"provider": "brave", "brave": {"max_results": 8, "country": "PH"}},
            web_search_api_keys={"brave": "synthetic-search-value"},
        )
        saved = load_config(service.configuration)
        assert saved.web_search.provider == "brave" and saved.web_search.exa.max_results == 12
        assert saved.web_search.brave.country == "PH" and saved.web_search.brave.max_results == 8
        assert saved.web_search.brave.api_key_ref == "keyring:" + stored[0][0]
        assert stored[0][1] == "synthetic-search-value"
        assert not saved.preferences.follow_replies and saved.theme.name == "nord"
        # A theme changed externally above requires a full reload. A search-only
        # edit below must preserve all other active integrations.
        engine, avatar, mcp, web = service.engine, service.avatar, service.mcp, service.tools.web
        await client.request(
            "save_preferences_with_web_search",
            preferences={},
            web_search={"brave": {"country": "US"}},
        )
        assert service.engine is engine and service.avatar is avatar and service.mcp is mcp
        assert service.tools.web is not web and web.client.is_closed
        assert service.tools.web.config.brave.country == "US"
        assert "synthetic-search-value" not in json.dumps(result)
        assert "synthetic-search-value" not in service.configuration.read_text()


async def test_web_search_references_and_removal_never_store_literal_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "eidolon.service.runtime.store_secret",
        lambda *args: pytest.fail("Reference was written as a literal key"),
    )
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request(
            "save_preferences_with_web_search",
            preferences={},
            web_search={"provider": "exa"},
            web_search_api_keys={"exa": "env:EXA_TEST_KEY"},
        )
        assert load_config(service.configuration).web_search.exa.api_key_ref == "env:EXA_TEST_KEY"
        await client.request(
            "save_preferences_with_web_search",
            preferences={},
            web_search={},
            web_search_api_keys={"exa": None},
        )
        assert load_config(service.configuration).web_search.exa.api_key_ref is None


async def test_search_keychain_failure_and_invalid_settings_do_not_change_configuration(
    tmp_path, monkeypatch
):
    def broken_keychain(*args):
        raise RuntimeError("upstream failure containing synthetic-search-value")

    monkeypatch.setattr("eidolon.service.runtime.store_secret", broken_keychain)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        before = service.configuration.read_bytes()
        with pytest.raises(
            RuntimeError, match="Could not store the search API key in Keychain"
        ) as error:
            await client.request(
                "save_preferences_with_web_search",
                preferences={},
                web_search={"provider": "brave"},
                web_search_api_keys={"brave": "synthetic-search-value"},
            )
        assert "synthetic-search-value" not in str(error.value)
        assert service.configuration.read_bytes() == before
        monkeypatch.setattr(
            "eidolon.service.runtime.store_secret",
            lambda *args: pytest.fail("Invalid preferences reached Keychain"),
        )
        for patch in ({"max_results": 0}, {"timeout_seconds": 999}, {"api_key_ref": "literal-key"}):
            with pytest.raises(RuntimeError):
                await client.request(
                    "save_preferences_with_web_search",
                    preferences={},
                    web_search={"brave": patch},
                    web_search_api_keys={"brave": "synthetic-search-value"},
                )
            assert service.configuration.read_bytes() == before
        with pytest.raises(RuntimeError):
            await client.request(
                "save_preferences_with_web_search",
                preferences={"reset_hour": 30},
                web_search={"provider": "brave"},
                web_search_api_keys={"brave": "synthetic-search-value"},
            )
        assert service.configuration.read_bytes() == before


async def test_search_preferences_wait_for_inflight_turn_before_replacing_adapter(
    tmp_path, monkeypatch
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        original = service.tools.web
        async with service.generation_lock:
            await client.request(
                "save_preferences_with_web_search",
                preferences={},
                web_search={
                    "provider": "searxng",
                    "searxng": {"base_url": "http://localhost:9000"},
                },
            )
            assert load_config(service.configuration).web_search.provider == "searxng"
            assert service.tools.web is original and not original.client.is_closed
        await service._apply_saved_configuration()
        assert service.tools.web.config.provider == "searxng" and original.client.is_closed


async def test_search_connection_test_uses_unsaved_settings_and_key_without_persistence(
    tmp_path, monkeypatch
):
    captured = []

    class Search:
        def __init__(self, config, resolver):
            captured.append(
                (
                    config.provider,
                    config.selected().max_results,
                    resolver(config.selected().api_key_ref),
                )
            )

        async def search(self, query):
            assert query == "open source software"
            return {"results": [{"url": "https://example.com"}]}

        async def close(self):
            captured.append("closed")

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        before = service.configuration.read_bytes()
        monkeypatch.setattr("eidolon.service.runtime.WebTools", Search)
        monkeypatch.setattr(
            "eidolon.service.runtime.store_secret", lambda *args: pytest.fail("Test stored a key")
        )
        result = await client.request(
            "test_web_search",
            web_search={"provider": "exa", "exa": {"max_results": 7}},
            web_search_api_keys={"exa": "synthetic-test-value"},
        )
        assert result == {"provider": "exa", "results": 1}
        assert captured == [("exa", 7, "synthetic-test-value"), "closed"]
        assert service.configuration.read_bytes() == before
        assert service.config.web_search.provider == "tavily"


async def test_web_preferences_apply_with_auxiliary_and_vision_as_one_validated_save(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "eidolon.service.runtime.store_secret",
        lambda *args: pytest.fail("Invalid combined settings reached Keychain"),
    )
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        before = service.configuration.read_bytes()
        with pytest.raises(RuntimeError):
            await client.request(
                "save_preferences_with_web_search",
                preferences={"follow_replies": False},
                auxiliary={"mode": "disabled"},
                vision={"timeout_seconds": 0},
                web_search={"provider": "exa"},
                web_search_api_keys={"exa": "synthetic-test-value"},
            )
        assert service.configuration.read_bytes() == before
        result = await client.request(
            "save_preferences_with_web_search",
            preferences={"follow_replies": False},
            auxiliary={"mode": "disabled"},
            vision={"timeout_seconds": 90},
            web_search={"provider": "exa"},
            web_search_api_keys={"exa": "env:EXA_TEST_KEY"},
        )
        assert result["auxiliary"]["mode"] == "disabled"
        assert result["vision"]["timeout_seconds"] == 90
        assert result["web_search"]["exa"]["api_key_ref"] == "env:EXA_TEST_KEY"
        assert not result["preferences"]["follow_replies"]
