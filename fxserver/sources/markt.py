"""Marktdaten für die Treiber: VIX, Nikkei 225, Brent und CFTC-Positionierung.

- VIX / Nikkei / Brent: Yahoo Finance (laufend, auch während der Sitzung); Ersatz: FRED (1 Tag Verzug), für den VIX zusätzlich CBOE
- CFTC: Commitments of Traders (Legacy, nur Futures), Yen- und Pfund-Kontrakte, wöchentlich (Stand Dienstag, veröffentlicht Freitag)
Jede Quelle kann ausfallen – dann wird sie als fehlend gemeldet, der Rest läuft weiter.
Kurze Zeitlimits, damit ein hängender Server die minütliche Aktualisierung nicht aufhält.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from ..common import http_get

SERIEN = {"vix": "VIX", "nikkei": "Nikkei 225", "brent": "Brent"}
YAHOO = {"vix": "^VIX", "nikkei": "^N225", "brent": "BZ=F"}
FRED = {"vix": "VIXCLS", "nikkei": "NIKKEI225", "brent": "DCOILBRENTEU"}
CBOE_VIX = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
CFTC_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
CFTC_CODES = {"jpy": "097741", "gbp": "096742"}
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36", "Accept": "application/json,text/csv,*/*"}


def _f(x) -> float | None:
    try:
        v = float(str(x).strip())
        return v if v == v else None
    except (TypeError, ValueError):
        return None


# ---------------- Yahoo Finance ----------------
def yahoo(symbol: str) -> tuple[dict[str, float], dict | None]:
    letzter: Exception | None = None
    for host in ("query1", "query2"):
        try:
            r = http_get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}",
                         params={"range": "6mo", "interval": "1d"}, headers=BROWSER, timeout=8, versuche=1)
            return parse_yahoo(r.json())
        except Exception as e:  # noqa: BLE001
            letzter = e
    raise RuntimeError(f"Yahoo {symbol}: {letzter}")


def parse_yahoo(daten: dict) -> tuple[dict[str, float], dict | None]:
    """-> ({Datum (Börsenzeit): Schluss}, {wert, ts} letzter Kurs oder None). Der laufende Tag zählt mit dem
    aktuellen Kurs, damit Veränderungen 'heute bisher' zeigen."""
    res = ((daten or {}).get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("Yahoo: keine Daten")
    r0 = res[0]
    meta = r0.get("meta") or {}
    off = int(meta.get("gmtoffset") or 0)
    quote_ = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    werte = {}
    for t, c in zip(r0.get("timestamp") or [], quote_.get("close") or []):
        v = _f(c)
        if v is not None and t:
            werte[datetime.fromtimestamp(int(t) + off, timezone.utc).date().isoformat()] = v
    live = None
    p, pt = _f(meta.get("regularMarketPrice")), meta.get("regularMarketTime")
    if p is not None and pt:
        live = {"wert": p, "ts": int(pt)}
        werte[datetime.fromtimestamp(int(pt) + off, timezone.utc).date().isoformat()] = p
    return werte, live


# ---------------- FRED / CBOE (Ersatz) ----------------
def fred(serie: str, tage: int = 150) -> dict[str, float]:
    von = (date.today() - timedelta(days=tage)).isoformat()
    r = http_get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": serie, "cosd": von},
                 headers=BROWSER, timeout=10, versuche=1)
    return parse_fred(r.text)


def parse_fred(text: str) -> dict[str, float]:
    """FRED-CSV: Kopf 'observation_date,<ID>' (früher 'DATE,<ID>'), fehlende Werte '.' oder leer."""
    erg = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        try:
            d = date.fromisoformat(row[0].strip()).isoformat()
        except ValueError:
            continue
        v = _f(row[1])
        if v is not None:
            erg[d] = v
    return erg


def cboe_vix(tage: int = 150) -> dict[str, float]:
    return parse_cboe(http_get(CBOE_VIX, headers=BROWSER, timeout=10, versuche=1).text, ab=date.today() - timedelta(days=tage))


def parse_cboe(text: str, ab: date | None = None) -> dict[str, float]:
    erg = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            d = datetime.strptime(str(row.get("DATE", "")).strip(), "%m/%d/%Y").date()
        except ValueError:
            continue
        if ab and d < ab:
            continue
        v = _f(row.get("CLOSE"))
        if v is not None:
            erg[d.isoformat()] = v
    return erg


def lade_markt() -> tuple[dict[str, dict[str, float]], dict[str, dict], list[str]]:
    """-> ({serie: {datum: wert}}, {serie: {wert, ts, quelle}}, fehlende Serien)."""
    werte, live, fehler = {}, {}, []
    for s, name in SERIEN.items():
        quellen = [("Yahoo", lambda s=s: yahoo(YAHOO[s])), ("FRED", lambda s=s: (fred(FRED[s]), None))]
        if s == "vix":
            quellen.append(("CBOE", lambda: (cboe_vix(), None)))
        for quelle, fn in quellen:
            try:
                w, l = fn()
                if len(w) < 3:
                    raise ValueError("zu wenige Werte")
            except Exception:  # noqa: BLE001
                continue
            werte[s] = w
            if l:
                live[s] = {**l, "quelle": quelle}
            break
        else:
            fehler.append(name)
    return werte, live, fehler


# ---------------- CFTC ----------------
def cftc(waehrung: str, wochen: int = 110) -> dict[str, dict[str, float]]:
    r = http_get(CFTC_URL, params={"cftc_contract_market_code": CFTC_CODES[waehrung],
                                   "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": wochen}, timeout=20, versuche=1)
    return parse_cftc(r.json())


def parse_cftc(zeilen: list[dict]) -> dict[str, dict[str, float]]:
    """-> {Stichtag: {netto, oi}}; netto = Nicht-kommerzielle Long minus Short (positiv = auf die Währung gesetzt)."""
    erg = {}
    for z in zeilen or []:
        k = {str(a).lower(): b for a, b in (z or {}).items()}
        try:
            d = str(k["report_date_as_yyyy_mm_dd"])[:10]
            date.fromisoformat(d)
            lang, kurz = float(k["noncomm_positions_long_all"]), float(k["noncomm_positions_short_all"])
            oi = float(k["open_interest_all"])
        except (KeyError, TypeError, ValueError):
            continue
        if oi > 0:
            erg[d] = {"netto": lang - kurz, "oi": oi}
    return erg


def lade_cftc() -> tuple[dict[str, dict[str, dict[str, float]]], list[str]]:
    erg, fehler = {}, []
    for w in CFTC_CODES:
        try:
            daten = cftc(w)
            if not daten:
                raise ValueError("leer")
            erg[w] = daten
        except Exception:  # noqa: BLE001
            fehler.append(f"CFTC {w.upper()}")
    return erg, fehler
