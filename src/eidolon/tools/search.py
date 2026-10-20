"""Provider adapters for the single, source-returning web_search tool."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from eidolon.config import SEARCH_PROVIDERS, WebSearchConfig


class WebSearch:
    def __init__(
        self,
        config: WebSearchConfig,
        client: httpx.AsyncClient,
        resolve_secret: Callable[[str | None], str | None],
    ):
        self.config, self.client, self.resolve_secret = config, client, resolve_secret

    async def search(self, query: str) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("Search query must contain 1–1000 characters")
        provider = self.config.provider
        settings = self.config.selected()
        label = SEARCH_PROVIDERS[provider]
        key = await asyncio.to_thread(self.resolve_secret, settings.api_key_ref)
        if provider != "searxng" and not key:
            raise ValueError(f"Configure a {label} API key in /preferences (Web search)")
        request = self.request(query, key)
        try:
            response = await self.client.request(**request, timeout=settings.timeout_seconds)
            response.raise_for_status()
        except httpx.TimeoutException:
            raise ValueError(
                f"{label} search timed out; retry or increase its timeout in /preferences"
            ) from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if provider == "searxng" and status == 403:
                message = "SearXNG refused JSON search; enable the json format on your instance and check its credentials"
            elif status in {401, 403}:
                message = (
                    f"{label} rejected the credential; check its API key and access in /preferences"
                )
            elif status == 429:
                message = (
                    f"{label} search quota or rate limit reached; retry later or check your plan"
                )
            elif status in {400, 422}:
                message = f"{label} rejected the search settings (HTTP {status}); review its filters in /preferences"
            else:
                message = f"{label} search failed (HTTP {status}); retry or check the provider"
            raise ValueError(message) from None
        except httpx.RequestError:
            raise ValueError(
                f"Could not connect to {label}; check your network and search settings"
            ) from None
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            if provider == "brave":
                rows = payload.get("web", {}).get("results", [])
            else:
                rows = payload["results"]
            if not isinstance(rows, list):
                raise ValueError
        except (ValueError, KeyError, AttributeError, TypeError):
            hint = "; enable JSON output on the instance" if provider == "searxng" else ""
            raise ValueError(f"{label} returned an invalid search response{hint}") from None
        results = []
        for row in rows:
            if not isinstance(row, dict) or not self.source_url(row.get("url")):
                continue
            result = {
                "title": self.text(row.get("title", ""), 500, is_html=True),
                "url": row["url"],
                "content": self.content(row, settings.max_content_chars),
            }
            published = row.get("published_date") or row.get("publishedDate") or row.get("date")
            if isinstance(published, str) and published:
                result["published_at"] = published[:100]
            results.append(result)
            if len(results) >= settings.max_results:
                break
        return {"query": query, "provider": provider, "results": results}

    def request(self, query: str, key: str | None) -> dict:
        provider = self.config.provider
        if provider == "tavily":
            settings = self.config.tavily
            body = {
                "query": query,
                "search_depth": settings.depth,
                "max_results": settings.max_results,
                "topic": settings.topic,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
                "include_published_date": settings.include_published_date,
                "include_domains": settings.include_domains,
                "exclude_domains": settings.exclude_domains,
                "exact_match": settings.exact_match,
            }
            if settings.depth != "ultra-fast":
                body["chunks_per_source"] = settings.chunks_per_source
            if settings.safe_search:
                body["safe_search"] = True
            body.update(
                {
                    name: getattr(settings, name)
                    for name in ("time_range", "start_date", "end_date")
                    if getattr(settings, name)
                }
            )
            return {
                "method": "POST",
                "url": "https://api.tavily.com/search",
                "headers": {"Authorization": f"Bearer {key}"},
                "json": body,
            }
        if provider == "brave":
            settings = self.config.brave
            params = {
                "q": query,
                "count": settings.max_results,
                "country": settings.country,
                "search_lang": settings.search_lang,
                "safesearch": settings.safesearch,
                "extra_snippets": str(settings.extra_snippets).lower(),
                "text_decorations": "false",
            }
            if settings.freshness:
                params["freshness"] = settings.freshness
            if settings.goggles:
                params["goggles"] = settings.goggles
            return {
                "method": "GET",
                "url": "https://api.search.brave.com/res/v1/web/search",
                "headers": {"X-Subscription-Token": key},
                "params": params,
            }
        if provider == "exa":
            settings = self.config.exa
            contents = (
                {"highlights": {"maxCharacters": settings.max_content_chars}}
                if settings.content_mode == "highlights"
                else {"text": {"maxCharacters": settings.max_content_chars}}
            )
            body = {
                "query": query,
                "type": settings.search_type,
                "numResults": settings.max_results,
                "contents": contents,
            }
            for field, parameter in (
                ("category", "category"),
                ("include_domains", "includeDomains"),
                ("exclude_domains", "excludeDomains"),
            ):
                if value := getattr(settings, field):
                    body[parameter] = value
            for field, parameter in (
                ("start_date", "startPublishedDate"),
                ("end_date", "endPublishedDate"),
            ):
                if value := getattr(settings, field):
                    body[parameter] = value + (
                        "T00:00:00Z" if field == "start_date" else "T23:59:59Z"
                    )
            return {
                "method": "POST",
                "url": "https://api.exa.ai/search",
                "headers": {"x-api-key": key},
                "json": body,
            }
        if provider == "perplexity":
            settings = self.config.perplexity
            body = {
                "query": query,
                "search_type": settings.search_type,
                "max_results": settings.max_results,
                "max_tokens": settings.max_tokens,
                "max_tokens_per_page": settings.max_tokens_per_page,
            }
            for field, parameter in (
                ("country", "country"),
                ("language_filter", "search_language_filter"),
                ("domain_filter", "search_domain_filter"),
                ("recency", "search_recency_filter"),
            ):
                if value := getattr(settings, field):
                    body[parameter] = value
            for field, parameter in (
                ("start_date", "search_after_date_filter"),
                ("end_date", "search_before_date_filter"),
                ("updated_after", "last_updated_after_filter"),
                ("updated_before", "last_updated_before_filter"),
            ):
                if value := getattr(settings, field):
                    body[parameter] = date.fromisoformat(value).strftime("%m/%d/%Y")
            return {
                "method": "POST",
                "url": "https://api.perplexity.ai/search",
                "headers": {"Authorization": f"Bearer {key}"},
                "json": body,
            }
        settings = self.config.searxng
        params = {"q": query, "format": "json", "safesearch": settings.safesearch}
        for field in ("engines", "categories"):
            if value := getattr(settings, field):
                params[field] = ",".join(value)
        for field in ("language", "time_range"):
            if value := getattr(settings, field):
                params[field] = value
        return {
            "method": "GET",
            "url": settings.base_url + "/search",
            "params": params,
            "headers": {"Authorization": f"Bearer {key}"} if key else {},
        }

    @staticmethod
    def source_url(value: object) -> bool:
        if not isinstance(value, str) or len(value) > 4096:
            return False
        try:
            url = urlsplit(value)
            return (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and url.username is None
                and url.password is None
            )
        except ValueError:
            return False

    @staticmethod
    def text(value: object, limit: int, *, is_html: bool = False) -> str:
        if not isinstance(value, str):
            return ""
        if is_html:
            # Some engines return HTML fragments. Bound parsing as well as tool output.
            return BeautifulSoup(value[: limit * 4], "html.parser").get_text(" ", strip=True)[
                :limit
            ]
        return value.strip()[:limit]

    def content(self, row: dict, limit: int) -> str:
        if self.config.provider == "brave":
            pieces = [row.get("description", "")]
            if self.config.brave.extra_snippets and isinstance(row.get("extra_snippets"), list):
                pieces.extend(row["extra_snippets"][:5])
        elif self.config.provider == "exa":
            pieces = (
                row.get("highlights", [])
                if self.config.exa.content_mode == "highlights"
                else [row.get("text", "")]
            )
        else:
            pieces = [row.get("snippet", row.get("content", ""))]
        if not isinstance(pieces, list):
            pieces = []
        excerpts = [
            self.text(piece, limit, is_html=self.config.provider in {"brave", "searxng"})
            for piece in pieces[:20]
            if isinstance(piece, str)
        ]
        return "\n".join(dict.fromkeys(excerpts))[:limit]
