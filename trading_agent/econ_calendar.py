"""Economic calendar (this week's events from the public Forex Factory feed), cached on disk.

Used two ways:
* the agent reads it through the `get_economic_calendar` tool;
* the risk manager blocks new trades on a currency around high-impact releases (news blackout).
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACT_RANK = {"Holiday": 0, "Non-Economic": 0, "Low": 1, "Medium": 2, "High": 3}


@dataclass
class Event:
    time_utc: datetime
    currency: str
    impact: str
    title: str
    forecast: str
    previous: str

    def to_dict(self, offset: timedelta = timedelta(0)) -> dict:
        return {"time_server": (self.time_utc + offset).strftime("%a %Y-%m-%d %H:%M"),
                "time_utc": self.time_utc.strftime("%a %H:%M"), "currency": self.currency,
                "impact": self.impact, "title": self.title, "forecast": self.forecast,
                "previous": self.previous}


class EconomicCalendar:
    def __init__(self, cache_path: str = "cache/calendar_thisweek.json", max_age_s: int = 3600,
                 url: str = FEED_URL):
        self.cache = Path(cache_path)
        self.max_age_s = max_age_s
        self.url = url

    def _raw(self) -> list[dict]:
        fresh = self.cache.exists() and time.time() - self.cache.stat().st_mtime < self.max_age_s
        if not fresh:
            try:
                req = urllib.request.Request(self.url, headers={"User-Agent": "trading-agent/0.2"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
                json.loads(data)  # validate before caching
                self.cache.parent.mkdir(parents=True, exist_ok=True)
                self.cache.write_bytes(data)
            except Exception:
                if not self.cache.exists():
                    raise
        return json.loads(self.cache.read_text(encoding="utf-8"))

    def events(self) -> list[Event]:
        out = []
        for e in self._raw():
            try:
                t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc).replace(tzinfo=None)
            except (KeyError, ValueError):
                continue
            out.append(Event(t, e.get("country", "").upper(), e.get("impact", ""), e.get("title", ""),
                             e.get("forecast", ""), e.get("previous", "")))
        return sorted(out, key=lambda x: x.time_utc)

    def upcoming(self, now_utc: datetime, currencies: set[str] | None = None, hours_ahead: float = 24,
                 min_impact: str = "Medium", hours_back: float = 2) -> list[Event]:
        lo, hi = now_utc - timedelta(hours=hours_back), now_utc + timedelta(hours=hours_ahead)
        rank = IMPACT_RANK.get(min_impact, 2)
        return [e for e in self.events()
                if lo <= e.time_utc <= hi and IMPACT_RANK.get(e.impact, 0) >= rank
                and (not currencies or e.currency in currencies)]

    def blackout(self, symbol: str, now_utc: datetime, before_min: int, after_min: int,
                 min_impact: str = "High") -> Event | None:
        currencies = {symbol[:3].upper(), symbol[3:6].upper()}
        rank = IMPACT_RANK.get(min_impact, 3)
        for e in self.events():
            if e.currency in currencies and IMPACT_RANK.get(e.impact, 0) >= rank:
                if e.time_utc - timedelta(minutes=before_min) <= now_utc <= e.time_utc + timedelta(minutes=after_min):
                    return e
        return None
