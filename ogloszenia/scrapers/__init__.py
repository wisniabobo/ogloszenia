"""Rejestr scraperów.

`SCRAPERS` mapuje klucz źródła (ten sam, który występuje w config/sources.yaml)
na klasę scrapera. Źródła opisane w YAML-u bez własnej klasy obsługuje
uniwersalny `generic_html` sterowany selektorami z konfiguracji.
"""

from __future__ import annotations

from .adresowo import AdresowoScraper
from .amw import AMWScraper
from .base import BaseScraper, RawListing, ScrapeContext
from .domiporta import DomiportaScraper
from .elicytacje_kas import ELicytacjeKASScraper
from .ezamowienia import EZamowieniaScraper
from .generic_html import GenericHtmlScraper
from .gratka import GratkaScraper
from .komornik import ELicytacjeScraper, LicytacjeKomornikScraper
from .kowr import KOWRScraper
from .krz import KRZScraper
from .morizon import MorizonScraper
from .msig import MSiGScraper
from .nieruchomosci_online import NieruchomosciOnlineScraper
from .olx import OLXScraper
from .otodom import OtodomScraper
from .pkp import PKPScraper
from .zus import ZUSScraper

SCRAPERS: dict[str, type[BaseScraper]] = {
    cls.key: cls
    for cls in (
        OLXScraper,
        OtodomScraper,
        GratkaScraper,
        MorizonScraper,
        NieruchomosciOnlineScraper,
        DomiportaScraper,
        AdresowoScraper,
        LicytacjeKomornikScraper,
        ELicytacjeScraper,
        ELicytacjeKASScraper,
        MSiGScraper,
        PKPScraper,
        KRZScraper,
        EZamowieniaScraper,
        KOWRScraper,
        AMWScraper,
        ZUSScraper,
        GenericHtmlScraper,
    )
}


def get_scraper(key: str) -> type[BaseScraper] | None:
    return SCRAPERS.get(key)


__all__ = ["SCRAPERS", "get_scraper", "BaseScraper", "RawListing", "ScrapeContext"]
