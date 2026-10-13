"""Wirtschaftskalender: Forex-Factory-Wochendatei (Forecast/Previous, ohne Actual)."""
from __future__ import annotations

import hashlib

from ..common import TZ_BERLIN, http_get, parse_iso

FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
]
WAEHRUNGEN = {"GBP", "JPY", "USD"}
IMPACT = {"High": "high", "Medium": "medium"}


def lade_termine() -> list[dict]:
    """Termine dieser Woche (GBP/JPY/USD, Impact medium/high). Wirft bei Fehler."""
    letzter = None
    for url in FF_URLS:
        try:
            daten = http_get(url, timeout=20).json()
            return parse_ff(daten)
        except Exception as e:  # noqa: BLE001
            letzter = e
    raise RuntimeError(f"Kalender nicht erreichbar: {letzter}")


def parse_ff(daten: list[dict]) -> list[dict]:
    termine = []
    for e in daten or []:
        land = str(e.get("country", "")).upper()
        impact = IMPACT.get(str(e.get("impact", "")))
        if land not in WAEHRUNGEN or not impact:
            continue
        try:
            ts = parse_iso(e["date"]).astimezone(TZ_BERLIN)
        except (KeyError, ValueError):
            continue
        titel = str(e.get("title", "")).strip()
        tid = hashlib.sha1(f"{land}|{titel}|{ts.isoformat()}".encode()).hexdigest()[:16]
        termine.append({
            "id": tid, "ts": ts.isoformat(), "currency": land, "impact": impact, "title": titel,
            "forecast": (e.get("forecast") or "").strip(), "previous": (e.get("previous") or "").strip(),
            "actual": (e.get("actual") or "").strip(),
        })
    termine.sort(key=lambda x: x["ts"])
    return termine
