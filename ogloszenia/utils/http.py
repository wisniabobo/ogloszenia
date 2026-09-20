"""Klient HTTP dla crawlera.

Zasady, których trzymają się wszystkie scrapery:
  * limit równoległości globalny i **per host**,
  * odstęp między żądaniami do tego samego hosta (grzeczne tempo),
  * wykładniczy backoff z jitterem na 429/5xx + poszanowanie `Retry-After`,
  * cache robots.txt i domyślne respektowanie `Disallow` (OGL_RESPECT_ROBOTS),
  * sesyjne cookies i HTTP/2, żeby nie wyglądać jak prymitywny bot.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

from ..settings import get_settings
from .robots import RobotsTxt

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# Zadeklarowane API kontra przeglądanie stron
# --------------------------------------------------------------------------- #
# robots.txt reguluje **indeksowanie stron przez roboty**, a nie korzystanie
# z publicznego API, które właściciel udostępnia właśnie do użytku
# programistycznego i opisuje własnym regulaminem. Nominatim ma w robots.txt
# `Disallow: /search`, bo nie chce, żeby wyszukiwarki indeksowały dynamiczne
# wyniki — a jednocześnie w swojej polityce użycia wprost dopuszcza zapytania
# API do 1/s z identyfikującym się User-Agentem. Tak samo GUGiK i Overpass.
#
# Dlatego host wpisany tutaj jest zwolniony z bramki robots.txt, ale **w zamian
# dostaje twardy, wpisany na sztywno limit tempa** z regulaminu danej usługi.
# To świadoma i udokumentowana decyzja, a nie obejście: przeglądanie portali
# ogłoszeniowych nadal w całości podlega robots.txt.
DECLARED_APIS: dict[str, float] = {
    # Nominatim: polityka OSMF — maks. 1 zapytanie na sekundę
    "nominatim.openstreetmap.org": 1.05,
    # GUGiK: publiczne usługi geokodowania i ewidencji działek
    "services.gugik.gov.pl": 0.5,
    "uldk.gugik.gov.pl": 0.5,
    # Overpass: publiczna instancja, 2 równoległe zapytania na IP
    "overpass-api.de": 2.0,
    # API instytucji publicznych
    "api-krs.ms.gov.pl": 0.4,
    "bdl.stat.gov.pl": 0.4,
    "api.dane.gov.pl": 0.4,
    "api.nbp.pl": 0.3,
}


@dataclass
class FetchError(Exception):
    url: str
    status: int | None = None
    message: str = ""

    def __str__(self) -> str:  # pragma: no cover - tylko do logów
        return f"{self.status or 'ERR'} {self.url} {self.message}".strip()


@dataclass
class HostState:
    lock: asyncio.Semaphore
    last_request: float = 0.0
    robots: RobotsTxt | None = None
    robots_loaded: bool = False
    penalty_until: float = 0.0
    delay: float = 0.0
    errors: int = 0


@dataclass
class HttpClient:
    """Współdzielony klient używany przez wszystkie scrapery w jednym przebiegu."""

    concurrency: int | None = None
    timeout: float | None = None
    user_agent: str | None = None
    browser_like: bool = True
    _hosts: dict[str, HostState] = field(default_factory=dict, init=False)
    _client: httpx.AsyncClient | None = field(default=None, init=False)
    _gate: asyncio.Semaphore | None = field(default=None, init=False)
    stats: dict[str, int] = field(default_factory=lambda: defaultdict(int), init=False)

    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> HttpClient:
        s = get_settings()
        headers = {
            "User-Agent": (self.user_agent or (DESKTOP_UA if self.browser_like else s.user_agent)),
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.6",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
        }
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=self.timeout or s.http_timeout,
            follow_redirects=True,
            http2=True,
            proxy=s.proxy_url,
            limits=httpx.Limits(max_connections=(self.concurrency or s.global_concurrency) * 2),
        )
        self._gate = asyncio.Semaphore(self.concurrency or s.global_concurrency)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ #
    def _host_state(self, url: str) -> tuple[str, HostState]:
        host = urlparse(url).netloc.lower()
        state = self._hosts.get(host)
        if state is None:
            state = HostState(lock=asyncio.Semaphore(get_settings().per_host_concurrency),
                              delay=get_settings().min_delay_s)
            self._hosts[host] = state
        return host, state

    @staticmethod
    def is_declared_api(url: str) -> bool:
        return urlparse(url).netloc.lower() in DECLARED_APIS

    async def _load_robots(self, url: str, state: HostState) -> None:
        if state.robots_loaded or not get_settings().respect_robots:
            state.robots_loaded = True
            return
        if self.is_declared_api(url):
            # zadeklarowane API: zamiast robots.txt obowiązuje limit z regulaminu
            state.robots_loaded = True
            state.delay = max(state.delay, DECLARED_APIS[urlparse(url).netloc.lower()])
            return
        state.robots_loaded = True
        robots_url = urljoin(f"{urlparse(url).scheme}://{urlparse(url).netloc}", "/robots.txt")
        try:
            assert self._client is not None
            resp = await self._client.get(robots_url, timeout=8.0)
            if resp.status_code == 200 and "<html" not in resp.text[:200].lower():
                parser = RobotsTxt.parse(resp.text)
                state.robots = parser
                crawl_delay = parser.crawl_delay(get_settings().user_agent)
                if crawl_delay:
                    state.delay = max(state.delay, float(crawl_delay))
        except (httpx.HTTPError, ValueError):
            state.robots = None

    def allowed(self, url: str) -> bool:
        if self.is_declared_api(url):
            return True
        _, state = self._host_state(url)
        if not get_settings().respect_robots or state.robots is None:
            return True
        return state.robots.can_fetch(get_settings().user_agent, url)

    async def _throttle(self, state: HostState) -> None:
        now = time.monotonic()
        if state.penalty_until > now:
            await asyncio.sleep(state.penalty_until - now)
        wait = state.last_request + state.delay - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait + random.uniform(0, 0.35))
        state.last_request = time.monotonic()

    # ------------------------------------------------------------------ #
    async def request(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict | None = None,
        json_body: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        assert self._client is not None and self._gate is not None, "użyj `async with HttpClient()`"
        s = get_settings()
        attempts = retries if retries is not None else s.max_retries
        _, state = self._host_state(url)
        await self._load_robots(url, state)
        if not self.allowed(url):
            self.stats["robots_blocked"] += 1
            raise FetchError(url, None, "zablokowane przez robots.txt")

        last_exc: Exception | None = None
        for attempt in range(attempts + 1):
            async with self._gate, state.lock:
                await self._throttle(state)
                try:
                    resp = await self._client.request(
                        method, url, params=params, json=json_body, data=data, headers=headers
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    self.stats["network_errors"] += 1
                else:
                    self.stats["requests"] += 1
                    if resp.status_code < 400:
                        state.errors = 0
                        return resp
                    if resp.status_code in (403, 429) or resp.status_code >= 500:
                        retry_after = resp.headers.get("Retry-After")
                        pause = float(retry_after) if (retry_after or "").isdigit() else 2 ** attempt
                        state.penalty_until = time.monotonic() + min(pause, 60)
                        state.delay = min(state.delay * 1.6 + 0.4, 12.0)
                        state.errors += 1
                        last_exc = FetchError(url, resp.status_code, "odrzucone przez serwer")
                        self.stats[f"http_{resp.status_code}"] += 1
                    else:
                        raise FetchError(url, resp.status_code, resp.reason_phrase)
            await asyncio.sleep(min(2 ** attempt, 15) * random.uniform(0.7, 1.3))

        if isinstance(last_exc, FetchError):
            raise last_exc
        raise FetchError(url, None, str(last_exc or "nieznany błąd"))

    async def get_text(self, url: str, **kw) -> str:
        return (await self.request(url, **kw)).text

    async def get_bytes(self, url: str, *, max_size: int = 8_000_000, **kw) -> bytes:
        """Pobiera plik. Załączniki BIP-ów bywają skanami po kilkanaście MB —
        takich nie czytamy, bo i tak nie ma w nich warstwy tekstowej."""
        resp = await self.request(url, **kw)
        content = resp.content
        return b"" if len(content) > max_size else content

    async def get_json(self, url: str, **kw) -> dict | list:
        headers = {"Accept": "application/json, text/plain, */*"} | (kw.pop("headers", None) or {})
        resp = await self.request(url, headers=headers, **kw)
        return resp.json()

    async def post_json(self, url: str, json_body: dict, **kw) -> dict | list:
        headers = {"Content-Type": "application/json", "Accept": "application/json"} | (
            kw.pop("headers", None) or {}
        )
        resp = await self.request(url, method="POST", json_body=json_body, headers=headers, **kw)
        return resp.json()
