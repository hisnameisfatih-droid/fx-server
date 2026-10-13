"""FX-Kurse über Tiingo-REST (Gratis-Tarif: 50 Abrufe/Std, 1000/Tag – sparsam verwenden)."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..common import TIINGO_API_KEY, fx_tag, http_get, parse_iso


def _get(pfad: str, params: dict | None = None):
    if not TIINGO_API_KEY:
        raise RuntimeError("Kein Tiingo-Key")
    r = http_get(f"https://api.tiingo.com{pfad}", params=params,
                 headers={"Authorization": f"Token {TIINGO_API_KEY}", "Content-Type": "application/json"})
    return r.json()


def aktuelle_kurse(paare: list[str]) -> dict[str, float]:
    """Letzter Mid-Kurs je Paar (1 Abruf)."""
    daten = _get("/tiingo/fx/top", {"tickers": ",".join(paare)})
    erg = {}
    for q in daten or []:
        t = str(q.get("ticker", "")).lower()
        mid = q.get("midPrice")
        if mid is None and q.get("bidPrice") and q.get("askPrice"):
            mid = (float(q["bidPrice"]) + float(q["askPrice"])) / 2
        if t and mid:
            erg[t] = float(mid)
    return erg


def kerzen(paar: str, freq: str, start: datetime) -> list[dict]:
    """Kerzen ab `start` (UTC), freq z. B. '5min', '1hour'. Liste von {t, o, h, l, c}."""
    daten = _get(f"/tiingo/fx/{paar}/prices",
                 {"startDate": start.astimezone(timezone.utc).strftime("%Y-%m-%d"), "resampleFreq": freq})
    erg = []
    for c in daten or []:
        try:
            erg.append({"t": parse_iso(c["date"]), "o": float(c["open"]), "h": float(c["high"]),
                        "l": float(c["low"]), "c": float(c["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    erg.sort(key=lambda x: x["t"])
    return erg


def fx_tage(stunden_kerzen: list[dict], bis_inkl_offen: bool = True) -> list[dict]:
    """Fasst Kerzen zu FX-Handelstagen (Ende 17:00 New York) zusammen.
    Eine Kerze wird nach ihrer Startzeit zugeordnet."""
    gruppen: dict = defaultdict(list)
    for k in stunden_kerzen:
        gruppen[fx_tag(k["t"] + timedelta(seconds=1))].append(k)
    tage = []
    for d in sorted(gruppen):
        ks = gruppen[d]
        tage.append({"date": d.isoformat(), "o": ks[0]["o"], "h": max(k["h"] for k in ks),
                     "l": min(k["l"] for k in ks), "c": ks[-1]["c"], "n": len(ks)})
    return tage
