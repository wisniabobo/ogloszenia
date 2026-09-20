"""Parser robots.txt zgodny z RFC 9309.

Dlaczego własny, skoro w bibliotece standardowej jest `urllib.robotparser`?
Bo tamten stosuje regułę „pierwsze dopasowanie wygrywa", a RFC 9309 (i wszystkie
liczące się crawlery) stosują **regułę najdłuższego dopasowania**. Różnica jest
praktyczna, nie teoretyczna — OLX ma w robots.txt::

    Disallow: /api/
    Allow: /api/v1/offers/

Zgodnie ze standardem `/api/v1/offers/` jest **dozwolone** (dłuższy wzorzec
wygrywa), a `urllib` uznaje je za zabronione i bot nie pobiera niczego z
serwisu, który sam wskazał, co udostępnia.

Obsługujemy też symbole wieloznaczne `*` i `$`, których `urllib` nie zna,
oraz `Crawl-delay`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]

    @property
    def specificity(self) -> int:
        """Długość wzorca — decyduje przy konflikcie reguł (RFC 9309 §2.2.2)."""
        return len(self.pattern)


@dataclass
class RobotsGroup:
    agents: set[str] = field(default_factory=set)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def _compile(pattern: str) -> re.Pattern[str]:
    """Zamienia wzorzec robots.txt na wyrażenie regularne."""
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    parts = [re.escape(segment) for segment in body.split("*")]
    regex = ".*".join(parts)
    return re.compile("^" + regex + ("$" if anchored_end else ""))


class RobotsTxt:
    """Zbiór reguł dla jednego hosta."""

    def __init__(self, groups: list[RobotsGroup] | None = None) -> None:
        self.groups = groups or []

    # ------------------------------------------------------------------ #
    @classmethod
    def parse(cls, text: str) -> RobotsTxt:
        groups: list[RobotsGroup] = []
        current: RobotsGroup | None = None
        expecting_agent = False

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field_name, _, value = line.partition(":")
            field_name = field_name.strip().lower()
            value = value.strip()

            if field_name == "user-agent":
                if current is None or not expecting_agent:
                    current = RobotsGroup()
                    groups.append(current)
                current.agents.add(value.lower())
                expecting_agent = True
                continue

            if current is None:
                continue
            expecting_agent = False

            if field_name in ("allow", "disallow"):
                if field_name == "disallow" and value == "":
                    continue  # pusty Disallow = zgoda na wszystko
                if not value:
                    continue
                current.rules.append(
                    Rule(allow=(field_name == "allow"), pattern=value, regex=_compile(value))
                )
            elif field_name == "crawl-delay":
                try:
                    current.crawl_delay = float(value.replace(",", "."))
                except ValueError:
                    pass

        return cls(groups)

    # ------------------------------------------------------------------ #
    def _group_for(self, user_agent: str) -> RobotsGroup | None:
        """Zwraca reguły obowiązujące dany user-agent.

        Wygrywa najbardziej szczegółowa nazwa bota; gdy jej nie ma — grupy `*`.
        Wszystkie bloki z tą samą nazwą są scalane, bo plik może je rozbijać na
        kilka sekcji (robią tak m.in. Morizon i Gratka), a RFC 9309 nakazuje
        traktować je jako jedną grupę.
        """
        agent = user_agent.lower()
        best_len = -1
        for group in self.groups:
            for candidate in group.agents:
                if candidate != "*" and candidate and candidate in agent:
                    best_len = max(best_len, len(candidate))

        merged = RobotsGroup(agents={"<merged>"})
        for group in self.groups:
            matches = any(
                (candidate != "*" and candidate and candidate in agent and len(candidate) == best_len)
                if best_len > 0
                else candidate == "*"
                for candidate in group.agents
            )
            if matches:
                merged.rules.extend(group.rules)
                if group.crawl_delay is not None:
                    merged.crawl_delay = max(merged.crawl_delay or 0.0, group.crawl_delay)
        return merged if merged.rules or merged.crawl_delay is not None else None

    def can_fetch(self, user_agent: str, url: str) -> bool:
        group = self._group_for(user_agent)
        if group is None or not group.rules:
            return True

        parsed = urlparse(url)
        path = unquote(parsed.path or "/")
        if parsed.query:
            path += "?" + parsed.query

        winner: Rule | None = None
        for rule in group.rules:
            if rule.regex.match(path):
                if (
                    winner is None
                    or rule.specificity > winner.specificity
                    # przy równej długości wygrywa Allow (RFC 9309 §2.2.2)
                    or (rule.specificity == winner.specificity and rule.allow)
                ):
                    winner = rule
        return winner.allow if winner else True

    def crawl_delay(self, user_agent: str) -> float | None:
        group = self._group_for(user_agent)
        return group.crawl_delay if group else None
