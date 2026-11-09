"""FX-Kurse über Tiingo-REST (Gratis-Tarif: 50 Abrufe/Std, 1000/Tag – sparsam verwenden)."""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..common import DATA_DIR, TIINGO_API_KEY, TZ_BERLIN, fx_tag, get_logger, http_get, parse_iso

log = get_logger("prices")
FREQ_MIN = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "1hour": 60, "4hour": 240}
# Ob Tiingo eine Kerze mit ihrer Start- oder Endzeit stempelt, wird auf dem Server gegen die selbst aus
# Ticks gebauten Live-Kerzen geprüft (stempel_pruefen) und hier gemerkt. Ohne Prüfung: Startzeit.
STEMPEL_DATEI = DATA_DIR / "tiingo_zeitstempel.json"


def stempel_ist_ende() -> bool:
    try:
        return json.loads(STEMPEL_DATEI.read_text()).get("ende") is True
    except (OSError, ValueError):
        return False


def startzeit(ts: datetime, freq: str) -> datetime:
    """Tiingo-Zeitstempel -> Startzeit der Kerze."""
    return ts - timedelta(minutes=FREQ_MIN.get(freq, 0)) if stempel_ist_ende() else ts


def stempel_pruefen(tiingo_roh: list[dict], live: list[tuple], minuten: int) -> bool | None:
    """Vergleicht ungeprüfte Tiingo-Kerzen ({t: UTC, c}) mit Live-Kerzen gleicher Länge
    [(t Berlin naiv = Start, o, h, l, c)]. Passt "Stempel = Endzeit" klar besser, wird das gemerkt.
    Gibt True (Endzeit), False (Startzeit) oder None (zu wenig Daten / nicht eindeutig) zurück."""
    lv = {k[0]: k[4] for k in live}

    def fehler(versatz: int):
        d = [abs(k["c"] - lv[t]) for k in tiingo_roh
             if (t := (k["t"] - timedelta(minutes=versatz)).astimezone(TZ_BERLIN).replace(tzinfo=None)) in lv]
        return (statistics.median(d), len(d)) if len(d) >= 12 else (None, len(d))

    f_start, n = fehler(0)
    f_ende, _ = fehler(minuten)
    if f_start is None or f_ende is None:
        return None
    if f_ende < 0.6 * f_start:
        urteil = True
    elif f_start < 0.6 * f_ende:
        urteil = False
    else:
        return None
    alt = stempel_ist_ende()
    try:
        STEMPEL_DATEI.write_text(json.dumps({"ende": urteil, "geprueft": datetime.now(timezone.utc).isoformat(),
                                             "abweichung_start": round(f_start, 4), "abweichung_ende": round(f_ende, 4),
                                             "kerzen": n, "minuten": minuten}))
    except OSError as e:
        log.warning("Zeitstempel-Prüfung nicht gespeichert: %s", e)
    (log.warning if urteil != alt else log.info)(
        "Tiingo-Zeitstempel = %s (Abweichung Start %.4f / Ende %.4f, %d Kerzen)", "Endzeit" if urteil else "Startzeit",
        f_start, f_ende, n)
    return urteil


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


def kerzen(paar: str, freq: str, start: datetime, ausrichten: bool = True) -> list[dict]:
    """Kerzen ab `start` (UTC), freq z. B. '5min', '1hour'. Liste von {t, o, h, l, c}, t = Startzeit der Kerze
    (mit ausrichten=False der unveränderte Tiingo-Zeitstempel, für stempel_pruefen)."""
    daten = _get(f"/tiingo/fx/{paar}/prices",
                 {"startDate": start.astimezone(timezone.utc).strftime("%Y-%m-%d"), "resampleFreq": freq})
    erg = []
    for c in daten or []:
        try:
            ts = parse_iso(c["date"])
            erg.append({"t": startzeit(ts, freq) if ausrichten else ts, "o": float(c["open"]), "h": float(c["high"]),
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
