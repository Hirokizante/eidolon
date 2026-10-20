"""Provider-independent search and bounded public-web fetching with DNS pinning."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from eidolon.config import TavilyConfig, WebSearchConfig
from .search import WebSearch


class WebTools:
    def __init__(
        self, config: WebSearchConfig | TavilyConfig, resolve_secret, *,
        client: httpx.AsyncClient | None = None, resolver=None
    ):
        self.config = WebSearchConfig(tavily=config) if isinstance(config, TavilyConfig) else config
        self.resolve_secret = resolve_secret
        self.client = client or httpx.AsyncClient(
            timeout=15,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        self.owns_client = client is None
        self.resolver = resolver

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def search(self, query: str) -> dict:
        return await WebSearch(self.config, self.client, self.resolve_secret).search(query)

    async def _destination(self, url: str) -> tuple[str, str, str]:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Fetch requires a public HTTP or HTTPS URL without credentials")
        if parsed.port not in (None, 80, 443):
            raise ValueError("Only standard web ports are permitted")
        host = parsed.hostname.encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if self.resolver:
            addresses = await self.resolver(host, port)
        else:
            loop = asyncio.get_running_loop()
            found = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), 5)
            addresses = [item[4][0] for item in found]
        if not addresses:
            raise ValueError("Website has no resolved address")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or (
                ip.version == 6 and ip.ipv4_mapped and not ip.ipv4_mapped.is_global
            ):
                raise ValueError("Private, local, and reserved network destinations are blocked")
        address = addresses[0]
        if ":" in address:
            address = f"[{address}]"
        netloc = address + (f":{parsed.port}" if parsed.port else "")
        pinned = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
        authority = host + (f":{parsed.port}" if parsed.port else "")
        return pinned, authority, host

    async def fetch(self, url: str, *, max_bytes: int = 1_000_000, max_chars: int = 20_000) -> dict:
        max_bytes = max(1024, min(max_bytes, 1_000_000))
        for _ in range(5):
            pinned, authority, host = await self._destination(url)
            # Connect to the validated IP, preserving Host and TLS SNI/certificate checks.
            async with self.client.stream(
                "GET",
                pinned,
                headers={
                    "Host": authority,
                    "Connection": "close",
                    "User-Agent": "EidolonsRoom/0.1",
                    "Accept": "text/html,text/plain",
                    "Accept-Encoding": "identity",
                },
                extensions={"sni_hostname": host},
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    destination = response.headers.get("location")
                    if not destination:
                        raise ValueError("Redirect has no destination")
                    url = urljoin(url, destination)
                    continue
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() not in (
                    "",
                    "identity",
                ):
                    raise ValueError("Compressed webpages are not supported by bounded fetching")
                advertised = response.headers.get("content-length")
                if advertised and int(advertised) > max_bytes:
                    raise ValueError("Webpage exceeds the download limit")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in ("text/html", "text/plain", "application/xhtml+xml"):
                    raise ValueError("Only readable HTML and plain text pages are supported")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError("Webpage exceeds the download limit")
                raw = bytes(data).decode(response.encoding or "utf-8", errors="replace")
                if content_type == "text/plain":
                    title, text = "", raw
                else:
                    soup = BeautifulSoup(raw, "html.parser")
                    title = soup.title.get_text(" ", strip=True) if soup.title else ""
                    for element in soup(
                        ["script", "style", "nav", "footer", "header", "noscript", "form"]
                    ):
                        element.decompose()
                    body = soup.find("main") or soup.find("article") or soup.body or soup
                    text = "\n".join(
                        line.strip()
                        for line in body.get_text("\n", strip=True).splitlines()
                        if line.strip()
                    )
                return {
                    "url": url,
                    "title": title,
                    "content": text[:max_chars],
                    "truncated": len(text) > max_chars,
                }
        raise ValueError("Too many webpage redirects")
