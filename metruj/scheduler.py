"""Harmonogram: każde źródło chodzi we własnym tempie.

Portale ogłoszeniowe skanujemy co kilka minut (tam liczy się bycie pierwszym),
licytacje co godzinę, a BIP-y i instytucje raz na kilka godzin — częściej i tak
nic się nie zmienia, a niepotrzebny ruch to prosta droga do blokady.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from .alerts import dispatch_alerts
from .db import session_scope
from .models import Source, utcnow
from .pipeline.runner import run_scan, sync_sources
from .settings import get_settings

log = logging.getLogger("metruj.scheduler")


async def scan_source(source_key: str) -> None:
    result = await run_scan(only=[source_key])
    totals = result.totals
    if totals["new"] or totals["errors"]:
        log.info(
            "[%s] pobrane=%s nowe=%s zmienione=%s kopie=%s błędy=%s",
            source_key, totals["fetched"], totals["new"], totals["updated"],
            totals["duplicates"], totals["errors"],
        )
    if result.new_listing_ids:
        sent = await dispatch_alerts(result.new_listing_ids)
        if sent:
            log.info("Wysłano %s powiadomień", sent)


async def housekeeping() -> None:
    """Sprzątanie: archiwizacja starych ofert i przeliczenie liczników biur."""
    from sqlalchemy import update

    from .models import Listing, ListingStatus
    from .pipeline.enrich import recount_agencies

    settings = get_settings()
    cutoff = utcnow() - timedelta(days=settings.retention_days)
    with session_scope() as session:
        session.execute(
            update(Listing)
            .where(Listing.status == ListingStatus.NIEAKTYWNA, Listing.last_seen_at < cutoff)
            .values(status=ListingStatus.ARCHIWALNA)
        )
        recount_agencies(session)
    log.info("Sprzątanie zakończone")


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Europe/Warsaw")

    with session_scope() as session:
        sync_sources(session)
        sources = list(session.scalars(select(Source).where(Source.enabled.is_(True))))
        plan = [(s.key, s.interval_minutes, s.name) for s in sources]

    for index, (key, interval, name) in enumerate(plan):
        scheduler.add_job(
            scan_source,
            IntervalTrigger(minutes=max(interval, 3), jitter=60),
            args=[key],
            id=f"scan:{key}",
            name=f"Skan: {name}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            # rozsuwamy starty, żeby nie odpalić wszystkiego w tej samej sekundzie
            next_run_time=utcnow() + timedelta(seconds=10 + index * 20),
        )

    scheduler.add_job(
        housekeeping, IntervalTrigger(hours=12), id="housekeeping", name="Sprzątanie",
        max_instances=1, coalesce=True,
    )
    return scheduler


async def run_forever() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    scheduler = build_scheduler()
    scheduler.start()
    jobs = [j for j in scheduler.get_jobs() if j.id.startswith("scan:")]
    log.info("Harmonogram wystartował — %s źródeł w rotacji", len(jobs))
    for job in jobs:
        log.info("  · %s co %s", job.name, job.trigger)
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Zatrzymywanie harmonogramu…")
        scheduler.shutdown(wait=False)
