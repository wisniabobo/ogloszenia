"""Powiadomienia o nowych ofertach (Telegram, webhook, e-mail).

Alert leci tylko raz na parę (poszukiwanie, oferta) — pilnuje tego unikalny
indeks w `alert_log`. Domyślnie powiadamiamy wyłącznie o ofertach oryginalnych,
bo o tej samej nieruchomości na sześciu portalach nikt nie chce dostać sześciu
wiadomości.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import session_scope
from .models import AlertLog, Listing, SavedSearch, utcnow
from .query import apply_filters
from .settings import get_settings

log = logging.getLogger("metruj.alerts")


def format_listing(listing: Listing, *, reveal_phone: bool = False) -> str:
    """Zwięzły opis oferty do powiadomienia (zwykły tekst).

    Wcześniej był to Markdown Telegrama — ale tytuł z „_" albo „*" (a takich
    jest pełno: „M-3_balkon", „*OKAZJA*") kończył się odmową „can't parse
    entities". Nieudany alert trafia do `alert_log` i nie jest ponawiany,
    więc taka oferta przepadała bez śladu.
    """
    bits: list[str] = []
    if listing.price:
        bits.append(f"{listing.price:,.0f} zł".replace(",", " "))
    if listing.price_per_m2:
        bits.append(f"{listing.price_per_m2:,.0f} zł/m²".replace(",", " "))
    if listing.area:
        bits.append(f"{listing.area:g} m²")
    if listing.rooms:
        bits.append(f"{listing.rooms} pok.")
    if listing.floor is not None:
        bits.append("parter" if listing.floor == 0 else f"{listing.floor} p.")

    where = ", ".join(x for x in (listing.street, listing.district, listing.city) if x)
    seller = listing.seller_type.value if listing.seller_type else "?"
    phones = ""
    if listing.phones:
        phones = " · " + ", ".join(
            (p.national or p.masked) if reveal_phone else (p.masked or "***")
            for p in listing.phones[:2]
        )

    lines = [
        listing.title[:140],
        " · ".join(bits) if bits else "",
        f"{where}" if where else "",
        f"{listing.source_key} · {seller}{phones}",
    ]
    if listing.kind.value == "licytacja":
        extra = []
        if listing.opening_price:
            extra.append(f"wywoławcza {listing.opening_price:,.0f} zł".replace(",", " "))
        if listing.estimate_value:
            extra.append(f"oszacowanie {listing.estimate_value:,.0f} zł".replace(",", " "))
        if listing.event_date:
            extra.append(f"licytacja {listing.event_date:%d.%m.%Y %H:%M}")
        if extra:
            lines.append(" · ".join(extra))
    lines.append(listing.url)
    return "\n".join(line for line in lines if line)


# --------------------------------------------------------------------------- #
# Kanały
# --------------------------------------------------------------------------- #
async def send_telegram(text: str) -> None:
    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        raise RuntimeError("brak OGL_TELEGRAM_BOT_TOKEN / OGL_TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            json={
                "chat_id": s.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": False,
            },
        )
        resp.raise_for_status()


async def send_webhook(payload: dict) -> None:
    s = get_settings()
    if not s.webhook_url:
        raise RuntimeError("brak OGL_WEBHOOK_URL")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(s.webhook_url, json=payload)
        resp.raise_for_status()


def send_email(subject: str, body: str) -> None:
    s = get_settings()
    if not (s.smtp_host and s.smtp_from and s.smtp_to):
        raise RuntimeError("brak konfiguracji SMTP")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = s.smtp_from
    message["To"] = s.smtp_to
    message.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if s.smtp_user and s.smtp_password:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(message)


# --------------------------------------------------------------------------- #
def _matching_listings(session: Session, search: SavedSearch, listing_ids: list[int]) -> list[Listing]:
    if not listing_ids:
        return []
    stmt = select(Listing).where(Listing.id.in_(listing_ids))
    stmt = apply_filters(stmt, search.query or {})
    if search.only_original:
        stmt = stmt.where(Listing.is_original.is_(True))
    return list(session.scalars(stmt.limit(50)))


async def dispatch_alerts(new_listing_ids: list[int]) -> int:
    """Rozsyła powiadomienia dla nowych ofert. Zwraca liczbę wysyłek."""
    if not new_listing_ids:
        return 0

    sent = 0
    with session_scope() as session:
        searches = list(session.scalars(select(SavedSearch).where(SavedSearch.enabled.is_(True))))
        for search in searches:
            listings = _matching_listings(session, search, new_listing_ids)
            for listing in listings:
                already = session.scalar(
                    select(AlertLog).where(
                        AlertLog.search_id == search.id, AlertLog.listing_id == listing.id
                    )
                )
                if already:
                    continue
                text = f"🔔 {search.name}\n\n{format_listing(listing)}"
                for channel in search.channels or ["telegram"]:
                    ok, error = True, None
                    try:
                        if channel == "telegram":
                            await send_telegram(text)
                        elif channel == "webhook":
                            await send_webhook(
                                {
                                    "search": search.name,
                                    "listing_id": listing.id,
                                    "title": listing.title,
                                    "price": listing.price,
                                    "url": listing.url,
                                    "source": listing.source_key,
                                    "city": listing.city,
                                }
                            )
                        elif channel == "email":
                            await asyncio.to_thread(
                                send_email, f"[{search.name}] {listing.title[:80]}", text
                            )
                        else:
                            ok, error = False, f"nieznany kanał: {channel}"
                    except Exception as exc:
                        ok, error = False, f"{type(exc).__name__}: {exc}"
                        log.warning("Alert nie wyszedł (%s): %s", channel, error)

                    session.add(
                        AlertLog(
                            search_id=search.id,
                            listing_id=listing.id,
                            channel=channel,
                            ok=ok,
                            error=error,
                        )
                    )
                    if ok:
                        sent += 1
                search.hits += 1
                search.last_fired_at = utcnow()
    return sent
