"""Apify — opcjonalny most do portali, które renderują wyniki w przeglądarce.

Część serwisów (Nieruchomosci-online, Adresowo, KOWR) buduje listę ofert
skryptem, więc zwykły pobieracz HTML nic z nich nie wyciągnie. Zamiast ciągnąć
za sobą całą przeglądarkę, można użyć gotowego aktora z Apify Store, który
zwraca już gotowy JSON.

**To jedyny płatny element całego projektu i jest w pełni opcjonalny.** Apify ma
darmowy pakiet startowy z miesięcznym limitem jednostek; bez tokenu ta ścieżka
po prostu się nie uruchamia, a reszta bota działa bez zmian.

Publiczny katalog aktorów (`/v2/store`) jest dostępny bez tokenu — dzięki temu
`ogl apify-actors` pokaże, co w ogóle jest do wzięcia, zanim cokolwiek zapłacisz.

Sprawdzone 19.09.2026.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

API = "https://api.apify.com/v2"


@dataclass(slots=True)
class ActorInfo:
    name: str
    title: str
    username: str
    description: str = ""
    total_runs: int | None = None

    @property
    def actor_id(self) -> str:
        return f"{self.username}~{self.name}"

    @property
    def url(self) -> str:
        return f"https://apify.com/{self.username}/{self.name}"


async def search_actors(query: str, limit: int = 10) -> list[ActorInfo]:
    """Przeszukuje publiczny katalog aktorów. Nie wymaga tokenu."""
    async with httpx.AsyncClient(timeout=25) as client:
        try:
            resp = await client.get(f"{API}/store", params={"search": query, "limit": limit})
            resp.raise_for_status()
            items = resp.json().get("data", {}).get("items", [])
        except (httpx.HTTPError, ValueError):
            return []
    return [
        ActorInfo(
            name=item.get("name", ""),
            title=item.get("title", ""),
            username=(item.get("username") or ""),
            description=(item.get("description") or "")[:240],
            total_runs=item.get("stats", {}).get("totalRuns"),
        )
        for item in items
        if item.get("name")
    ]


class ApifyClient:
    """Uruchamianie aktorów. Wymaga tokenu (`OGL_APIFY_TOKEN`)."""

    def __init__(self, token: str, timeout: float = 600.0) -> None:
        if not token:
            raise ValueError("Apify wymaga tokenu — ustaw OGL_APIFY_TOKEN")
        self.token = token
        self.timeout = timeout

    async def run_actor(self, actor_id: str, payload: dict[str, Any]) -> list[dict]:
        """Uruchamia aktora i zwraca elementy z jego zbioru wynikowego.

        Używa wariantu `run-sync-get-dataset-items`, więc jedno wywołanie
        HTTP wystarcza — bez odpytywania o status w pętli.
        """
        url = f"{API}/acts/{actor_id}/run-sync-get-dataset-items"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, params={"token": self.token}, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return data if isinstance(data, list) else []

    async def account_limits(self) -> dict[str, Any]:
        """Ile jednostek zostało w darmowym pakiecie."""
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.get(f"{API}/users/me/limits", params={"token": self.token})
            if resp.status_code != 200:
                return {}
            return resp.json().get("data", {})


async def _demo() -> None:  # pragma: no cover - pomocnicze przy rozpoznaniu
    for actor in await search_actors("otodom", 5):
        print(actor.actor_id, "|", actor.title)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_demo())
