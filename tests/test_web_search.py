import json

import httpx
import pytest
import yaml

from eidolon.config import AppConfig, SEARCH_PROVIDERS, WebSearchConfig, load_config, save_config
from eidolon.preferences import SearchCredentialError, merge_web_search
from eidolon.tools import ToolRegistry, WebTools
from eidolon.ui.web_search import provider_fields


@pytest.mark.parametrize("provider", SEARCH_PROVIDERS)
async def test_each_provider_returns_bounded_sources_and_uses_only_its_credential(provider):
    config = WebSearchConfig(provider=provider)
    settings = config.selected()
    settings.api_key_ref = f"env:TEST_{provider.upper()}"
    settings.max_results = 1
    settings.timeout_seconds = 22
    settings.max_content_chars = 100
    requests, references = [], []
    source = {"title": "Source", "url": "https://example.com/article"}
    payloads = {
        "tavily": {"results": [source | {"content": "x" * 1000}] * 3},
        "brave": {
            "web": {
                "results": [
                    source
                    | {
                        "description": "<b>Evidence</b>",
                        "extra_snippets": ["extra " + "x" * 1000],
                    }
                ]
                * 3
            }
        },
        "exa": {
            "results": [source | {"highlights": ["x" * 1000], "publishedDate": "2026-10-01"}] * 3
        },
        "perplexity": {"results": [source | {"snippet": "x" * 1000, "date": "2026-10-01"}] * 3},
        "searxng": {"results": [source | {"content": "<b>Evidence</b> " + "x" * 1000}] * 3},
    }

    def endpoint(request):
        requests.append(request)
        return httpx.Response(200, json=payloads[provider])

    def resolve(reference):
        references.append(reference)
        return "test-key"

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        result = await WebTools(config, resolve, client=client).search("question")
    assert result["provider"] == provider and result["query"] == "question"
    assert len(result["results"]) == 1
    assert len(result["results"][0]["content"]) == 100
    assert "<b>" not in result["results"][0]["content"]
    assert references == [f"env:TEST_{provider.upper()}"]
    endpoints = {
        "tavily": ("POST", "api.tavily.com", "/search"),
        "brave": ("GET", "api.search.brave.com", "/res/v1/web/search"),
        "exa": ("POST", "api.exa.ai", "/search"),
        "perplexity": ("POST", "api.perplexity.ai", "/search"),
        "searxng": ("GET", "127.0.0.1", "/search"),
    }
    assert (requests[0].method, requests[0].url.host, requests[0].url.path) == endpoints[provider]
    assert requests[0].extensions["timeout"]["read"] == 22
    assert "test-key" not in str(requests[0].url)
    assert "test-key" not in requests[0].content.decode()
    header = (
        "x-subscription-token"
        if provider == "brave"
        else "x-api-key"
        if provider == "exa"
        else "authorization"
    )
    assert requests[0].headers[header] == (
        "test-key" if provider in {"brave", "exa"} else "Bearer test-key"
    )


@pytest.mark.parametrize(
    "provider,settings,expected",
    [
        (
            "tavily",
            {
                "depth": "advanced",
                "topic": "news",
                "time_range": "week",
                "include_domains": ["example.com"],
                "exclude_domains": ["other.example"],
                "start_date": "2026-10-01",
                "end_date": "2026-10-15",
                "safe_search": True,
                "exact_match": True,
                "chunks_per_source": 2,
            },
            {
                "search_depth": "advanced",
                "topic": "news",
                "time_range": "week",
                "include_domains": ["example.com"],
                "exclude_domains": ["other.example"],
                "start_date": "2026-10-01",
                "end_date": "2026-10-15",
                "safe_search": True,
                "exact_match": True,
                "chunks_per_source": 2,
                "include_answer": False,
                "include_raw_content": False,
                "include_published_date": True,
            },
        ),
        (
            "exa",
            {
                "search_type": "fast",
                "content_mode": "text",
                "category": "publication",
                "include_domains": ["example.com/docs"],
                "exclude_domains": ["other.example"],
                "start_date": "2026-10-01",
                "end_date": "2026-10-15",
            },
            {
                "type": "fast",
                "category": "publication",
                "includeDomains": ["example.com/docs"],
                "excludeDomains": ["other.example"],
                "startPublishedDate": "2026-10-01T00:00:00Z",
                "endPublishedDate": "2026-10-15T23:59:59Z",
                "contents": {"text": {"maxCharacters": 3000}},
            },
        ),
        (
            "perplexity",
            {
                "search_type": "fast",
                "country": "PH",
                "language_filter": ["en"],
                "domain_filter": ["-example.com"],
                "recency": "week",
                "start_date": "2026-10-01",
                "end_date": "2026-10-15",
                "updated_after": "2026-10-02",
                "updated_before": "2026-10-16",
                "max_tokens": 8000,
                "max_tokens_per_page": 2000,
            },
            {
                "search_type": "fast",
                "country": "PH",
                "search_language_filter": ["en"],
                "search_domain_filter": ["-example.com"],
                "search_recency_filter": "week",
                "search_after_date_filter": "10/01/2026",
                "search_before_date_filter": "10/15/2026",
                "last_updated_after_filter": "10/02/2026",
                "last_updated_before_filter": "10/16/2026",
                "max_tokens": 8000,
                "max_tokens_per_page": 2000,
            },
        ),
    ],
)
async def test_search_post_options_follow_provider_contract(provider, settings, expected):
    captured = []
    config = WebSearchConfig.model_validate({"provider": provider, provider: settings})

    def endpoint(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        await WebTools(config, lambda _: "test-key", client=client).search("question")
    assert expected.items() <= captured[0].items()


async def test_brave_filters_goggles_and_additional_snippets():
    captured = []
    config = WebSearchConfig(
        provider="brave",
        brave={
            "country": "PH",
            "search_lang": "en",
            "freshness": "pw",
            "safesearch": "strict",
            "goggles": ["https://example.com/a.goggle", "https://example.com/b.goggle"],
        },
    )

    def endpoint(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "url": "https://example.com",
                            "description": "First",
                            "extra_snippets": ["First", "Second"],
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        result = await WebTools(config, lambda _: "test-key", client=client).search("question")
    params = captured[0].url.params
    assert params["country"] == "PH" and params["freshness"] == "pw"
    assert params["safesearch"] == "strict" and params["extra_snippets"] == "true"
    assert params.get_list("goggles") == config.brave.goggles
    assert result["results"][0]["content"] == "First\nSecond"


async def test_searxng_local_instance_without_key_sends_json_request_and_filters():
    captured = []
    config = WebSearchConfig(
        provider="searxng",
        searxng={
            "base_url": "http://127.0.0.1:8080/instance/",
            "engines": ["brave", "duckduckgo"],
            "categories": ["general", "science"],
            "language": "en-US",
            "time_range": "month",
            "safesearch": 2,
        },
    )

    def endpoint(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    None,
                    {"url": "javascript:alert(1)"},
                    {
                        "title": "<b>Title</b>",
                        "url": "https://example.com",
                        "content": "HTML &amp; text",
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        result = await WebTools(config, lambda _: None, client=client).search("question")
    request = captured[0]
    assert request.url.path == "/instance/search" and "authorization" not in request.headers
    assert dict(request.url.params) == {
        "q": "question",
        "format": "json",
        "safesearch": "2",
        "engines": "brave,duckduckgo",
        "categories": "general,science",
        "language": "en-US",
        "time_range": "month",
    }
    assert result["results"] == [
        {"title": "Title", "url": "https://example.com", "content": "HTML & text"}
    ]


@pytest.mark.parametrize("provider", SEARCH_PROVIDERS)
@pytest.mark.parametrize("query", ["", "  ", "x" * 1001])
async def test_invalid_queries_do_not_make_search_requests(provider, query):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: pytest.fail("Invalid search was sent"),
        )
    ) as client:
        with pytest.raises(ValueError, match="1–1000"):
            await WebTools(
                WebSearchConfig(provider=provider), lambda _: None, client=client
            ).search(query)


@pytest.mark.parametrize("provider", ["tavily", "brave", "exa", "perplexity"])
async def test_managed_search_requires_its_api_key_without_making_a_request(provider):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Missing-key search was sent"))
    ) as client:
        with pytest.raises(ValueError, match="Configure a .* API key"):
            await WebTools(
                WebSearchConfig(provider=provider), lambda _: None, client=client
            ).search("q")


@pytest.mark.parametrize(
    "status,message", [(401, "credential"), (429, "rate limit"), (503, "HTTP 503")]
)
async def test_search_errors_do_not_return_provider_bodies_or_keys(status, message):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="private upstream body with test-key"),
        )
    ) as client:
        with pytest.raises(ValueError, match=message) as error:
            await WebTools(
                WebSearchConfig(provider="brave"), lambda _: "test-key", client=client
            ).search("q")
    assert "test-key" not in str(error.value) and "private upstream" not in str(error.value)


@pytest.mark.parametrize(
    "response", [httpx.Response(403), httpx.Response(200, text="<html>Not JSON</html>")]
)
async def test_searxng_json_configuration_errors_are_actionable(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(ValueError, match="JSON|json"):
            await WebTools(
                WebSearchConfig(provider="searxng"), lambda _: None, client=client
            ).search("q")


@pytest.mark.parametrize("provider", SEARCH_PROVIDERS)
@pytest.mark.parametrize("agent_mode", [False, True])
async def test_one_web_search_tool_for_every_backend_and_mode(tmp_path, provider, agent_mode):
    registry = ToolRegistry(
        AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path, web_search={"provider": provider}), background_agent=True
    )
    try:
        await registry.set_agent_mode(agent_mode)
        definitions = registry.schemas(agent_mode=agent_mode)
        searches = [
            item["function"] for item in definitions if item["function"]["name"] == "web_search"
        ]
        assert len(searches) == 1
        assert set(searches[0]["parameters"]["properties"]) == {"query"}
        assert "Tavily" not in searches[0]["description"]
        assert registry.web.config.provider == provider
    finally:
        await registry.close()


def test_legacy_tavily_configuration_migrates_and_round_trips(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "state_dir": str(tmp_path),
                "tavily": {
                    "api_key_ref": "env:SEARCH_KEY",
                    "depth": "advanced",
                    "max_results": 8,
                },
            }
        )
    )
    config = load_config(path)
    assert config.web_search.provider == "tavily"
    assert config.web_search.tavily.api_key_ref == "env:SEARCH_KEY"
    assert config.tavily.depth == "advanced" and config.tavily.max_results == 8
    save_config(config, path)
    assert "tavily" not in yaml.safe_load(path.read_text())
    assert load_config(path).web_search == config.web_search


@pytest.mark.parametrize("provider", SEARCH_PROVIDERS)
def test_every_supported_search_setting_has_a_tui_control(provider):
    settings = getattr(WebSearchConfig(), provider)
    assert {spec.name for spec in provider_fields(provider)} == set(type(settings).model_fields) - {
        "api_key_ref"
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"provider": "serper"},
        {"brave": {"api_key_ref": "literal-key"}},
        {"tavily": {"depth": "fast", "safe_search": True}},
        {"exa": {"category": "people", "start_date": "2026-01-01"}},
        {"exa": {"category": "people", "exclude_domains": ["example.com"]}},
        {"exa": {"start_date": "2026-10-10", "end_date": "2026-10-01"}},
        {"perplexity": {"updated_after": "2026-10-10", "updated_before": "2026-10-01"}},
        {"perplexity": {"start_date": "2026-02-30"}},
        {"perplexity": {"language_filter": ["english"]}},
        {"perplexity": {"domain_filter": ["example.com", "-other.example"]}},
        {"searxng": {"base_url": "http://user:password@example.com"}},
        {"searxng": {"base_url": "http://example.com/?key=value"}},
        {"searxng": {"base_url": "http://localhost:invalid"}},
        {"searxng": {"base_url": "http://host with spaces"}},
        {"searxng": {"safesearch": 3}},
        {"brave": {"freshness": "yesterday"}},
        {"brave": {"freshness": "2026-10-10to2026-10-01"}},
        {"tavily": {"timeout_seconds": float("nan")}},
    ],
)
def test_search_settings_validate_provider_specific_constraints(settings):
    with pytest.raises(ValueError):
        WebSearchConfig.model_validate(settings)


def test_search_patches_preserve_other_backends_and_keep_keys_out_of_configuration():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, web_search={"tavily": {"api_key_ref": "env:OLD_KEY", "max_results": 9}})
    changed, keys = merge_web_search(
        config,
        {"provider": "brave", "brave": {"country": "PH"}},
        {"brave": "new-test-key", "exa": "env:EXA_KEY"},
    )
    assert keys == {"brave": "new-test-key"}
    assert changed.web_search.brave.country == "PH"
    assert changed.web_search.tavily == config.web_search.tavily
    assert changed.web_search.exa.api_key_ref == "env:EXA_KEY"
    assert "new-test-key" not in changed.model_dump_json()
    assert config.web_search.provider == "tavily"


def test_changing_authenticated_searxng_url_requires_reentering_or_removing_credential():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, web_search={"searxng": {"api_key_ref": "env:OLD_KEY"}})
    patch = {"searxng": {"base_url": "https://example.com"}}
    with pytest.raises(SearchCredentialError, match="Reenter or remove"):
        merge_web_search(config, patch)
    with pytest.raises(SearchCredentialError, match="Reenter or remove"):
        merge_web_search(config, patch, {"searxng": "  "})
    changed, _ = merge_web_search(config, patch, {"searxng": None})
    assert changed.web_search.searxng.api_key_ref is None


def test_blank_search_key_keeps_the_saved_reference():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, web_search={"brave": {"api_key_ref": "env:OLD_KEY"}})
    changed, keys = merge_web_search(config, {}, {"brave": "  "})
    assert changed.web_search.brave.api_key_ref == "env:OLD_KEY" and keys == {}


async def test_plain_search_excerpts_preserve_code_and_angle_brackets():
    config = WebSearchConfig(provider="exa")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "results": [
                        {"url": "https://example.com", "highlights": ["Use list[str] and <T>."]}
                    ]
                },
            )
        )
    ) as client:
        result = await WebTools(config, lambda _: "test-key", client=client).search("q")
    assert result["results"][0]["content"] == "Use list[str] and <T>."


async def test_search_timeout_is_actionable_and_invalid_payload_is_not_an_empty_success():
    def timeout(request):
        raise httpx.ReadTimeout("private transport detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(ValueError, match="timed out"):
            await WebTools(WebSearchConfig(), lambda _: "test-key", client=client).search("q")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"unexpected": "private upstream detail"}),
        )
    ) as client:
        with pytest.raises(ValueError, match="invalid search response"):
            await WebTools(WebSearchConfig(), lambda _: "test-key", client=client).search("q")
