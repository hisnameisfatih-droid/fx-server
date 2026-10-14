"""Die 10 wichtigsten Treiber für GBP/JPY: Werte, Signale, Gesamt-Score und Handlungsempfehlung.

Auswahl und Gewichte stammen aus der Studie in treiber_studie.json (GBP/JPY, Jan 2023 – Okt 2026):
- 77 % der Tagesschwankung kommen über die Yen-Seite (USD/JPY), 23 % über das Pfund -> Gewichte etwa 80 : 20
- von den 40 größten Tagesbewegungen hingen 23 an Japan (BoJ, Intervention, Politik), 13 an USA/Risikostimmung, 2 am Pfund
- BoJ-Tage bewegen 2,0x so stark wie normale Tage, Fed 1,7x, BoE 1,5x, US-Arbeitsmarkt 1,35x, US-Inflation 1,26x

Jeder Treiber liefert ein Signal von -1 (drückt GBP/JPY) bis +1 (stützt GBP/JPY). Score = gewichtete Summe,
skaliert auf -100…+100; fehlende oder veraltete Treiber werden herausgerechnet. Die Empfehlung ist eine
mechanische Regel – ihre Trefferquote wird täglich gemessen (evaluate.py). Keine Anlageberatung.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from .common import DASHBOARD_URL, PUBLIC_DIR, TZ_BERLIN, de_zahl, fx_tag, get_logger, jetzt_berlin, kv_get, kv_set

log = get_logger("treiber")
STUDIE_DATEI = Path(__file__).parent / "treiber_studie.json"
ALARM_AN = os.getenv("TREIBER_ALARM", "1") != "0"


@dataclass(frozen=True)
class Def:
    id: str
    name: str
    kurz: str
    gewicht: int
    seite: str
    quelle: str
    takt: str
    warum: str


DEFINITIONEN = (
    Def("intervention", "Interventionsrisiko (USD/JPY)", "Interventionsrisiko", 20, "JPY", "Tiingo-Kurs + News-Feeds", "laufend",
        "Größtes Einzelrisiko: 6 der 40 größten Tagesbewegungen seit 2023 waren Eingriffe des japanischen "
        "Finanzministeriums oder Rate-Checks – alle abwärts, bis −2,3 % an einem Tag (02.05.2024, 30.04.2026, "
        "30.07.2026). Lag USD/JPY über 160, war in den 5 Folgetagen das Beste +1,6 %, das Schlechteste −4,0 %. "
        "Stieg USD/JPY schnell (≥ +2 % in 5 Tagen, über 150), fiel GBP/JPY danach in 78 % der Fälle."),
    Def("jgb2", "BoJ-Erwartung (JGB 2 Jahre)", "JGB 2y", 15, "JPY", "Finanzministerium Japan", "täglich",
        "BoJ-Tage bewegen GBP/JPY im Schnitt doppelt so stark wie normale Tage (Ø 0,86 %, 41 % davon über 1 %). "
        "14 der 40 größten Tage hingen an BoJ-Entscheidungen oder -Aussagen (31.07.2024 Zinserhöhung −2,1 %, "
        "03.09.2026 Aussagen von Takata −1,9 %). Die 2-jährige Rendite zeigt täglich, wie viel BoJ-Straffung "
        "eingepreist ist: steigt sie, wird der Yen fester."),
    Def("us10", "US-Renditen (Treasury 10 Jahre)", "US 10y", 15, "USD", "US-Finanzministerium", "täglich",
        "USD/JPY hängt eng an den US-Renditen (Monatsänderungen seit 2022: Korrelation +0,75; +10 bp ≈ +0,9 % "
        "USD/JPY), über USD/JPY wirkt das auf GBP/JPY (+0,43). Fed-Tage bewegen GBP/JPY 1,7×, der US-Arbeitsmarkt "
        "1,35×, US-Inflation 1,26× so stark wie normale Tage."),
    Def("uk2", "BoE-Erwartung (Gilt 2 Jahre)", "Gilt 2y", 10, "GBP", "Bank of England", "täglich",
        "BoE-Tage bewegen GBP/JPY 1,5× so stark wie normale Tage. Die 2-jährige Gilt-Rendite zeigt, wie viel "
        "BoE-Politik eingepreist ist: steigt sie, wird das Pfund fester. Das Pfund erklärt aber nur 23 % der "
        "GBP/JPY-Schwankung – daher das kleinere Gewicht."),
    Def("spread2", "Zinsabstand UK – Japan (2 Jahre)", "Zinsabstand", 5, "Carry", "Bank of England + MoF", "täglich",
        "Kern des Carry-Trades: Das Pfund bringt mehr Zins als der Yen. Solange der Abstand groß ist, stützt er "
        "GBP/JPY (Anstieg von 156 auf über 210 seit 2023). Schrumpft er schnell, werden Carry-Positionen "
        "aufgelöst – wie im August 2024 (−7,4 % in 5 Tagen)."),
    Def("vix", "Risikostimmung (VIX)", "VIX", 10, "Risiko", "Yahoo Finance (Ersatz: FRED, CBOE)", "alle 30 Min",
        "Steigt die Angst, fließt Geld in den Yen. Tageskorrelation mit GBP/JPY −0,18 (2023–25 bis −0,25). "
        "Nach VIX-Sprüngen über +25 % fiel GBP/JPY am Folgetag in 78 % der Fälle. Ab VIX 20 schwankt GBP/JPY "
        "ein Drittel stärker als bei VIX unter 15."),
    Def("nikkei", "Aktien Japan (Nikkei 225)", "Nikkei", 10, "Risiko", "Yahoo Finance (Ersatz: FRED)", "alle 30 Min",
        "Japanische Aktien und Yen laufen gegenläufig: Risikofreude schwächt den Yen. 05.08.2024: Nikkei −12,4 %, "
        "GBP/JPY −2,3 %. 06.10.2025 (Takaichi-Wahl): Nikkei +4,8 %, GBP/JPY +1,8 %."),
    Def("uk30", "UK-Haushaltsstress (Gilt 30 Jahre)", "Gilt 30y", 5, "GBP", "Bank of England + Tiingo", "täglich",
        "Steigen die langen Gilt-Renditen und fällt zugleich das Pfund, misstraut der Markt dem britischen "
        "Haushalt. Januar 2025 (30y auf höchstem Stand seit 1998): GBP/JPY −3,0 % in 5 Tagen. Nach dem Haushalt "
        "vom 30.10.2024: −1,7 % am Folgetag."),
    Def("brent", "Ölpreis (Brent)", "Brent", 5, "JPY", "Yahoo Finance (Ersatz: FRED)", "alle 30 Min",
        "Japan importiert fast seine gesamte Energie – teures Öl schwächt den Yen. Der Zusammenhang ist klein, "
        "aber in jedem Jahr seit 2023 positiv (Korrelation +0,09 mit GBP/JPY, +0,14 mit USD/JPY). Ende April 2026 "
        "lag Brent über 126 $ – kurz vor der Intervention bei USD/JPY 160,7."),
    Def("cftc", "Positionierung Spekulanten (CFTC)", "CFTC", 5, "Positionierung", "CFTC Commitments of Traders",
        "wöchentlich (Fr)",
        "Wetten sehr viele Spekulanten gegen den Yen, genügt ein Auslöser für eine heftige Gegenbewegung: Anfang "
        "Juli 2024 die größten Yen-Shorts seit 2007, danach im August −7,4 % in 5 Tagen. Wirkt als Warnsignal bei "
        "Extremen (über 35 % des offenen Interesses), nicht als Tagesprognose."),
)
DEF = {d.id: d for d in DEFINITIONEN}
SERIEN_NAME = {"uk2": "Gilt 2y", "uk10": "Gilt 10y", "jp2": "JGB 2y", "jp10": "JGB 10y", "us10": "US 10y"}

# Interventionszonen (Eingriffe 2024: 160,2 und 161,8; 2026: 160,7 und ~163)
LEVEL_STUFEN = ((162.0, 2.0), (160.0, 1.5), (158.0, 1.0), (155.0, 0.5))
INTERVENTION_WOERTER = (
    "intervention", "intervene", "intervening", "rate check", "rate-check", "yen-buying", "yen buying", "excessive",
    "speculative move", "bold step", "bold action", "decisive action", "final warning", "final advisory", "eingriff",
)
# Termin-Typen (Forex-Factory-Titel) -> Name in der Studie
TERMIN_TYPEN = (
    ("BoJ-Zinsentscheid", "JPY", ("boj policy rate", "monetary policy statement", "boj press conference",
                                  "boj outlook report")),
    ("Fed-Zinsentscheid", "USD", ("federal funds rate", "fomc statement", "fomc press conference")),
    ("BoE-Zinsentscheid", "GBP", ("official bank rate", "monetary policy summary", "monetary policy report")),
    ("US-Arbeitsmarkt (NFP)", "USD", ("non-farm employment change",)),
    ("US-Inflation (CPI)", "USD", ("cpi",)),
    ("UK-Inflation (CPI)", "GBP", ("cpi",)),
    ("Japan-Inflation (CPI)", "JPY", ("cpi",)),
    ("UK-Haushalt", "GBP", ("budget",)),
)
KURZNAME = {"BoJ-Zinsentscheid": "BoJ", "Fed-Zinsentscheid": "Fed", "BoE-Zinsentscheid": "BoE",
            "US-Arbeitsmarkt (NFP)": "US-Arbeitsmarkt", "US-Inflation (CPI)": "US-Inflation",
            "UK-Inflation (CPI)": "UK-Inflation", "Japan-Inflation (CPI)": "Japan-Inflation", "UK-Haushalt": "UK-Haushalt"}
GROSS_FAKTOR = 1.5          # ab hier "ABWARTEN" kurz vor dem Termin
ABWARTEN_VORLAUF_STD = 3    # so lange vor einem großen Termin
STUFEN_TEXT = {2: "LONG BEVORZUGT", 1: "LEICHT LONG", 0: "NEUTRAL", -1: "LEICHT SHORT", -2: "SHORT BEVORZUGT"}


# ------------------------------------------------------------
# Hilfen
# ------------------------------------------------------------
@lru_cache(maxsize=1)
def studie() -> dict:
    """Ergebnisse der Studie (statisch, wird mit dem Code ausgeliefert). Nicht verändern – wird geteilt."""
    try:
        return json.loads(STUDIE_DATEI.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _c(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _reihe(con, tabelle: str, serie: str, n: int = 40) -> list[tuple[str, float]]:
    rows = con.execute(f"SELECT date, value FROM {tabelle} WHERE series=? ORDER BY date DESC LIMIT ?",  # noqa: S608
                       (serie, n)).fetchall()
    return [(r["date"], r["value"]) for r in reversed(rows)]


def _fx(con, paar: str, heute: str, n: int = 12) -> list[tuple[str, float]]:
    """Schlusskurse abgeschlossener FX-Tage (ohne den laufenden)."""
    rows = con.execute("SELECT date, close FROM fxdaily WHERE pair=? AND date<? ORDER BY date DESC LIMIT ?",
                       (paar, heute, n)).fetchall()
    return [(r["date"], r["close"]) for r in reversed(rows)]


def _delta_bp(reihe: list[tuple[str, float]], schritte: int) -> tuple[float | None, int]:
    if len(reihe) < 2:
        return None, 0
    k = min(schritte, len(reihe) - 1)
    return round((reihe[-1][1] - reihe[-1 - k][1]) * 100, 1), k


def _delta_pct(reihe: list[tuple[str, float]], schritte: int) -> float | None:
    if len(reihe) <= schritte or not reihe[-1 - schritte][1]:
        return None
    return round((reihe[-1][1] / reihe[-1 - schritte][1] - 1) * 100, 2)


def _frisch(datum: str | None, jetzt: datetime, tage: int = 4) -> bool:
    return bool(datum) and datum >= (jetzt.date() - timedelta(days=tage)).isoformat()


def _pz(x: float | None, st: int = 1) -> str:
    return "n/v" if x is None else f"{x:+.{st}f} %".replace(".", ",")


def _bp(x: float | None) -> str:
    return "n/v" if x is None else f"{x:+.0f} bp"


def _live_kurse(kurse: dict | None) -> dict[str, float]:
    erg = {k: v for k, v in (kurse or {}).items() if v}
    try:
        live = json.loads((PUBLIC_DIR / "live.json").read_text())
        for p, d in (live.get("pairs") or {}).items():
            if p not in erg and d.get("price"):
                erg[p] = float(d["price"])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return erg


def _item(d: Def, *, signal: float | None = None, wert: float | None = None, wert_text: str = "n/v",
          aenderung_text: str = "", kurz_wert: str = "", text: str = "keine Daten", stand: str | None = None,
          frisch: bool = False, **extra) -> dict:
    s = None if signal is None else round(_c(signal), 2) + 0.0   # + 0.0 macht aus -0.0 eine 0.0
    return {"id": d.id, "name": d.name, "kurz": d.kurz, "gewicht": d.gewicht, "seite": d.seite, "quelle": d.quelle,
            "takt": d.takt, "warum": d.warum, "wert": wert, "wert_text": wert_text, "aenderung_text": aenderung_text,
            "kurz_wert": kurz_wert, "signal": s, "beitrag": None if s is None else round(d.gewicht * s, 1),
            "wirkung": None if s is None else ("stützt" if s >= 0.15 else "drückt" if s <= -0.15 else "neutral"),
            "text": text, "stand": stand, "frisch": frisch, **extra}


# ------------------------------------------------------------
# Die 10 Treiber
# ------------------------------------------------------------
def warnungen(con, jetzt: datetime, stunden: int = 48) -> list[dict]:
    """Meldungen der letzten 48 Std mit Interventions-Signalwörtern (Yen-Bezug)."""
    grenze = (jetzt.astimezone(timezone.utc) - timedelta(hours=stunden)).isoformat()
    erg = []
    for r in con.execute("SELECT ts, source, title, summary, note FROM news WHERE ts>=? ORDER BY ts DESC", (grenze,)):
        t = " ".join(x or "" for x in (r["title"], r["summary"], r["note"])).lower()
        if ("yen" in t or "jpy" in t) and any(w in t for w in INTERVENTION_WOERTER):
            erg.append({"ts": r["ts"], "source": r["source"], "title": r["title"]})
    return erg


def intervention(con, jetzt: datetime, heute: str, live: dict) -> dict:
    d = DEF["intervention"]
    reihe = _fx(con, "usdjpy", heute)
    kurs = live.get("usdjpy") or (reihe[-1][1] if reihe else None)
    if kurs is None:
        return _item(d, stufe=0.0, alarm=False, grad="unbekannt", warnungen=[])
    d5 = round((kurs / reihe[-5][1] - 1) * 100, 2) if len(reihe) >= 5 else None
    w = warnungen(con, jetzt)
    zone = next((g for g, _ in LEVEL_STUFEN if kurs >= g), None)
    stufe = next((p for g, p in LEVEL_STUFEN if kurs >= g), 0.0)
    if d5 is not None:
        stufe += 1.0 if d5 >= 2.0 else 0.5 if d5 >= 1.0 else 0.0
    stufe += 1.0 if len(w) >= 3 else 0.5 if w else 0.0
    alarm = stufe >= 2.0
    grad = "akut" if alarm else "hoch" if stufe >= 1.5 else "erhöht" if stufe >= 0.5 else "gering"
    teile = [f"Risiko {grad}"]
    if zone:
        teile.append(f"USD/JPY über {zone:.0f}")
    if d5 is not None and d5 >= 1.0:
        teile.append(f"schneller Anstieg ({_pz(d5)} in 5 Tagen)")
    if w:
        teile.append(f"{len(w)} Warnung{'en' if len(w) > 1 else ''} in 48 Std")
    return _item(d, signal=-min(1.0, stufe / 3.0), wert=round(kurs, 3), wert_text=f"USD/JPY {de_zahl(kurs, 2)}",
                 aenderung_text=f"{_pz(d5)} in 5 Tagen" if d5 is not None else "",
                 kurz_wert=f"(USD/JPY {de_zahl(kurs, 1)})", text=", ".join(teile) + ".", stand=jetzt.isoformat(), frisch=True,
                 stufe=stufe, alarm=alarm, grad=grad, warnungen=w[:5])


def _rendite(con, jetzt: datetime, d: Def, serie: str, teiler: float, richtung: int, texte: tuple[str, str, str],
             ersatz: str | None = None) -> dict:
    reihe, hinweis = _reihe(con, "yields", serie, 30), None
    if len(reihe) < 2 and ersatz:
        reihe, hinweis = _reihe(con, "yields", ersatz, 30), f"Ersatz: {SERIEN_NAME[ersatz]} ({SERIEN_NAME[serie]} fehlt)"
    if not reihe:
        return _item(d, text=f"keine Daten ({SERIEN_NAME[serie]})")
    datum, wert = reihe[-1]
    d5, k = _delta_bp(reihe, 5)
    frisch = _frisch(datum, jetzt)
    signal = None if d5 is None or not frisch else richtung * d5 / teiler
    if d5 is None:
        text = "erst ein Wert – Veränderung ab morgen"
    else:
        text = texte[0] if signal and signal >= 0.25 else texte[1] if signal and signal <= -0.25 else texte[2]
    if not frisch:
        text = f"veraltet (Stand {datum}) – zählt nicht"
    return _item(d, signal=signal, wert=wert, wert_text=f"{de_zahl(wert, 2)} %",
                 aenderung_text=f"{_bp(d5)} in {k} Tagen" if d5 is not None else "", kurz_wert=_bp(d5).replace(" ", ""),
                 text=text + (f" {hinweis}." if hinweis else ""), stand=datum, frisch=frisch)


def jgb2(con, jetzt):
    return _rendite(con, jetzt, DEF["jgb2"], "jp2", 8.0, -1,
                    ("Markt preist weniger BoJ-Straffung ein → Yen schwächer.",
                     "Markt preist mehr BoJ-Straffung ein → Yen fester.",
                     "BoJ-Erwartung kaum verändert."))


def us10(con, jetzt):
    return _rendite(con, jetzt, DEF["us10"], "us10", 15.0, 1,
                    ("US-Renditen steigen → Dollar fester, Yen schwächer.",
                     "US-Renditen fallen → Yen gesucht.",
                     "US-Renditen kaum verändert."))


def uk2(con, jetzt):
    return _rendite(con, jetzt, DEF["uk2"], "uk2", 12.0, 1,
                    ("Markt erwartet straffere BoE → Pfund fester.",
                     "Markt erwartet lockerere BoE → Pfund schwächer.",
                     "BoE-Erwartung kaum verändert."), ersatz="uk10")


def spread2(con, jetzt):
    d = DEF["spread2"]
    lz = "2y"
    uk, jp = dict(_reihe(con, "yields", "uk2", 40)), dict(_reihe(con, "yields", "jp2", 40))
    tage = sorted(set(uk) & set(jp))
    if len(tage) < 2:
        lz = "10y"
        uk, jp = dict(_reihe(con, "yields", "uk10", 40)), dict(_reihe(con, "yields", "jp10", 40))
        tage = sorted(set(uk) & set(jp))
    if not tage:
        return _item(d)
    reihe = [(t, round(uk[t] - jp[t], 4)) for t in tage]
    datum, wert = reihe[-1]
    d20, k = _delta_bp(reihe, 20)
    frisch = _frisch(datum, jetzt)
    stuetze = 0.2 if wert >= 2.0 else 0.1 if wert >= 1.0 else 0.0
    if not frisch:
        signal, text = None, f"veraltet (Stand {datum}) – zählt nicht"
    elif d20 is None or k < 5:
        signal, text = stuetze, f"Abstand {de_zahl(wert, 2)} Prozentpunkte – Verlauf ab 5 Tagen."
    else:
        signal = d20 / 25 + stuetze
        text = ("Abstand wächst → Carry stützt GBP/JPY." if d20 >= 8 else
                "Abstand schrumpft → Carry-Stütze bröckelt." if d20 <= -8 else
                "Abstand stabil – Carry stützt weiter." if stuetze else "Abstand stabil.")
    return _item(d, signal=signal, wert=wert, wert_text=f"{de_zahl(wert, 2)} Pp",
                 aenderung_text=f"{_bp(d20)} in {k} Tagen" if d20 is not None else "",
                 kurz_wert=f"{de_zahl(wert, 1)}Pp", text=text + ("" if lz == "2y" else " (Ersatz: 10 Jahre)"),
                 stand=datum, frisch=frisch, laufzeit=lz)


def _markt(con, serie: str) -> list[tuple[str, float]]:
    return _reihe(con, "markt", serie, 30)


def vix(con, jetzt):
    d = DEF["vix"]
    reihe = _markt(con, "vix")
    if not reihe:
        return _item(d, alarm=False)
    datum, v = reihe[-1]
    d1 = _delta_pct(reihe, 1)
    s = -1.0 if v >= 30 else -0.7 if v >= 25 else -0.35 if v >= 20 else 0.3 if v < 15 else 0.0
    alarm = False
    if d1 is not None:
        if d1 >= 25:
            s, alarm = -1.0, True
        elif d1 >= 15:
            s = min(s, -0.5)
        elif d1 <= -15:
            s = max(s, 0.3)
    frisch = _frisch(datum, jetzt)
    text = ("Angst-Sprung → Flucht in den Yen." if alarm else
            "Hohe Angst → Yen gesucht." if s <= -0.5 else
            "Erhöhte Nervosität → leichter Yen-Rückenwind." if s < 0 else
            "Ruhiger Markt → Carry-freundlich." if s > 0 else "Normale Stimmung.")
    return _item(d, signal=s if frisch else None, wert=v, wert_text=de_zahl(v, 1),
                 aenderung_text=f"{_pz(d1)} zum Vortag" if d1 is not None else "", kurz_wert=de_zahl(v, 0),
                 text=text if frisch else f"veraltet (Stand {datum}) – zählt nicht", stand=datum, frisch=frisch,
                 alarm=alarm and frisch)


def nikkei(con, jetzt):
    d = DEF["nikkei"]
    reihe = _markt(con, "nikkei")
    if not reihe:
        return _item(d)
    datum, v = reihe[-1]
    d1, d5 = _delta_pct(reihe, 1), _delta_pct(reihe, 5)
    if d1 is None:
        return _item(d, wert=v, wert_text=de_zahl(v, 0), text="erst ein Wert", stand=datum)
    s = 0.5 * d1 / 1.5 + 0.5 * d5 / 4.0 if d5 is not None else d1 / 1.5
    frisch = _frisch(datum, jetzt)
    text = ("Aktien steigen → Risikofreude, Yen schwach." if s >= 0.25 else
            "Aktien fallen → Yen gesucht." if s <= -0.25 else "Aktien seitwärts.")
    return _item(d, signal=s if frisch else None, wert=v, wert_text=de_zahl(v, 0),
                 aenderung_text=f"{_pz(d1)} heute, {_pz(d5)} in 5 Tagen" if d5 is not None else f"{_pz(d1)} heute",
                 kurz_wert=_pz(d1).replace(" ", ""), text=text if frisch else f"veraltet (Stand {datum}) – zählt nicht",
                 stand=datum, frisch=frisch)


def uk30(con, jetzt, heute: str, live: dict):
    d = DEF["uk30"]
    reihe = _reihe(con, "yields", "uk30", 30)
    if not reihe:
        return _item(d)
    datum, wert = reihe[-1]
    d5, k = _delta_bp(reihe, 5)
    fx = _fx(con, "gbpusd", heute)
    jetzt_gu = live.get("gbpusd") or (fx[-1][1] if fx else None)
    g5 = round((jetzt_gu / fx[-5][1] - 1) * 100, 2) if jetzt_gu and len(fx) >= 5 else None
    frisch = _frisch(datum, jetzt)
    if d5 is None:
        s, text = None, "erst ein Wert – Veränderung ab morgen."
    elif d5 >= 12 and g5 is not None and g5 < 0:
        s, text = -min(1.0, d5 / 25), "Lange Renditen steigen, Pfund fällt → Misstrauen in den Haushalt."
    elif d5 >= 12:
        s, text = -0.2, "Lange Renditen steigen (Pfund hält sich) – beobachten."
    elif d5 <= -12 and g5 is not None and g5 > 0:
        s, text = 0.3, "Entspannung bei langen Gilts → stützt das Pfund."
    else:
        s, text = 0.0, "Kein Haushaltsstress."
    return _item(d, signal=s if frisch else None, wert=wert, wert_text=f"{de_zahl(wert, 2)} %",
                 aenderung_text=(f"{_bp(d5)} in {k} Tagen" + (f", GBP/USD {_pz(g5)}" if g5 is not None else ""))
                 if d5 is not None else "", kurz_wert=_bp(d5).replace(" ", ""),
                 text=text if frisch else f"veraltet (Stand {datum}) – zählt nicht", stand=datum, frisch=frisch)


def brent(con, jetzt):
    d = DEF["brent"]
    reihe = _markt(con, "brent")
    if not reihe:
        return _item(d)
    datum, v = reihe[-1]
    k = min(5, len(reihe) - 1)
    d5 = _delta_pct(reihe, k) if k >= 1 else None
    frisch = _frisch(datum, jetzt)
    if d5 is None:
        s, text = None, "erst ein Wert."
    else:
        s = d5 / 10
        text = ("Öl teurer → belastet Japans Handelsbilanz, Yen leicht schwächer." if d5 >= 3 else
                "Öl billiger → entlastet Japan, Yen leicht fester." if d5 <= -3 else "Ölpreis kaum verändert.")
    return _item(d, signal=s if frisch else None, wert=v, wert_text=f"{de_zahl(v, 1)} $",
                 aenderung_text=f"{_pz(d5)} in {k} Tagen" if d5 is not None else "", kurz_wert=_pz(d5, 0).replace(" ", ""),
                 text=text if frisch else f"veraltet (Stand {datum}) – zählt nicht", stand=datum, frisch=frisch)


def _crowding(anteil: float | None) -> float:
    """Kontra-Signal bei Extremen: viele gegen die Währung gewettet -> Gegenbewegung droht."""
    if anteil is None:
        return 0.0
    if anteil <= -0.35:
        return -0.7
    if anteil <= -0.20:
        return -0.35
    if anteil >= 0.35:
        return 0.7
    if anteil >= 0.20:
        return 0.35
    return 0.0


def cftc(con, jetzt):
    d = DEF["cftc"]
    jn, joi = _markt(con, "cftc_jpy_netto"), dict(_markt(con, "cftc_jpy_oi"))
    if not jn or jn[-1][0] not in joi:
        return _item(d)
    datum, netto = jn[-1]
    anteil = netto / joi[datum] if joi[datum] else None
    vorwoche = netto - jn[-2][1] if len(jn) >= 2 else None
    gn, goi = _markt(con, "cftc_gbp_netto"), dict(_markt(con, "cftc_gbp_oi"))
    g_anteil = gn[-1][1] / goi[gn[-1][0]] if gn and goi.get(gn[-1][0]) else None
    # Yen: viele Shorts (negativ) -> Squeeze-Gefahr -> drückt GBP/JPY. Pfund: viele Longs -> Rückschlaggefahr.
    s = _c(_crowding(anteil) - 0.5 * _crowding(g_anteil))
    frisch = _frisch(datum, jetzt, tage=12)
    if anteil is not None and anteil <= -0.20:
        text = "Viele Wetten gegen den Yen → Squeeze-Gefahr nach unten."
    elif anteil is not None and anteil >= 0.20:
        text = "Viele Wetten auf den Yen → Gegenbewegung nach oben möglich."
    else:
        text = "Keine Extremlage beim Yen."
    if g_anteil is not None and abs(g_anteil) >= 0.20:
        text += f" Pfund: {'viele Longs' if g_anteil > 0 else 'viele Shorts'} ({de_zahl(100 * g_anteil, 0)} % OI)."
    netto_text = f"netto {de_zahl(netto / 1000, 0)}k Kontrakte"
    if vorwoche is not None:
        netto_text += f" ({'+' if vorwoche > 0 else ''}{de_zahl(vorwoche / 1000, 1)}k zur Vorwoche)"
    return _item(d, signal=s if frisch else None, wert=netto,
                 wert_text=f"{de_zahl(100 * anteil, 0)} % des OI" if anteil is not None else f"{de_zahl(netto / 1000, 0)}k",
                 aenderung_text=netto_text,
                 kurz_wert=f"Yen {de_zahl(100 * anteil, 0)}% OI" if anteil is not None else "",
                 text=text if frisch else f"veraltet (Stichtag {datum}) – zählt nicht", stand=datum, frisch=frisch)


# ------------------------------------------------------------
# Termine, Score, Empfehlung
# ------------------------------------------------------------
def termin_typ(t: dict) -> str | None:
    titel = (t.get("title") or "").lower()
    for name, waehrung, muster in TERMIN_TYPEN:
        if t.get("currency") == waehrung and any(m in titel for m in muster):
            return name
    return None


def termine(con, jetzt: datetime, kurs: float | None, stunden: int = 36) -> list[dict]:
    """Kommende Termine mit gemessener Wirkung auf GBP/JPY (aus der Studie), je Typ und Tag einmal."""
    st = {t["name"]: t for t in studie().get("termine", [])}
    rows = con.execute("SELECT * FROM calendar WHERE ts>=? AND ts<? ORDER BY ts",
                       ((jetzt - timedelta(minutes=30)).isoformat(), (jetzt + timedelta(hours=stunden)).isoformat()))
    erg, gesehen = [], set()
    for r in rows:
        r = dict(r)
        typ = termin_typ(r)
        if not typ:
            continue
        tag = r["ts"][:10]
        if (typ, tag) in gesehen:
            continue
        gesehen.add((typ, tag))
        s = st.get(typ, {})
        avg = s.get("avg_abs")
        erg.append({"ts": r["ts"], "titel": r["title"], "typ": typ, "kurz": KURZNAME.get(typ, typ),
                    "waehrung": r["currency"], "impact": r["impact"], "faktor": s.get("faktor"), "avg_abs": avg,
                    "anteil_1pct": s.get("anteil_ueber_1pct"), "n": s.get("n"), "notiz": s.get("notiz"),
                    "pips": round(avg / 100 * kurs / 0.01, -1) if avg and kurs else None})
    return erg


def stufe_aus_score(score: int | None, vorher: int | None = None, puffer: int = 5) -> int:
    """-2..2 mit Schwellen ±15 (leicht) und ±30 (klar). Wechsel erst, wenn die Schwelle um `puffer` überschritten ist."""
    if score is None:
        return 0

    def stufe(s):
        return 2 if s >= 30 else 1 if s >= 15 else -2 if s <= -30 else -1 if s <= -15 else 0
    neu = stufe(score)
    if vorher is None or neu == vorher:
        return neu
    zurueck = 1 if vorher > neu else -1
    return vorher if stufe(score + zurueck * puffer) == vorher else neu


def empfehlung(score: int | None, stufe: int, iv: dict, vix_alarm: bool, termin_nah: dict | None,
               setup_trend: int | None = None) -> dict:
    hinweise: list[str] = []
    if score is None:
        return {"code": "keine", "titel": "KEINE DATEN", "emoji": "⚪", "farbe": "neutral",
                "text": "Zu wenige Treiber verfügbar – nur Setup-Regel oder Pause.", "risiko": "halbiert", "hinweise": []}
    if iv.get("alarm") and stufe >= 0:
        code, titel, emoji, farbe = "long_meiden", "LONG MEIDEN", "🟠", "warn"
        text, risiko = "Interventionsgefahr: keine neuen Longs. Shorts nur nach Bestätigung (Bruch Vortagestief).", "halbiert"
    elif termin_nah:
        zeit = datetime.fromisoformat(termin_nah["ts"]).astimezone(TZ_BERLIN).strftime("%H:%M")
        fak = f" – an solchen Tagen im Schnitt {de_zahl(termin_nah['faktor'], 1)}× so viel Bewegung" if termin_nah.get("faktor") else ""
        code, titel, emoji, farbe = "abwarten", "ABWARTEN", "🟡", "warn"
        text, risiko = f"{termin_nah['kurz']} um {zeit}{fak}. Neue Positionen erst danach.", "kein neues Risiko"
        hinweise.append(f"Danach gilt wieder: {STUFEN_TEXT[stufe]} (Score {score:+d}).")
    else:
        code = {2: "long", 1: "leicht_long", 0: "neutral", -1: "leicht_short", -2: "short"}[stufe]
        titel = STUFEN_TEXT[stufe]
        emoji = "🟢" if stufe > 0 else "🔴" if stufe < 0 else "⚪"
        farbe = "long" if stufe > 0 else "short" if stufe < 0 else "neutral"
        text = {2: "Treiber stützen GBP/JPY: Long-Setups bevorzugen, Shorts meiden.",
                1: "Leichter Rückenwind: Longs nur mit Bestätigung durch das Setup.",
                0: "Treiber heben sich auf: kein Vorteil – nur Setup-Regel oder Pause.",
                -1: "Leichter Gegenwind: Shorts nur mit Bestätigung durch das Setup.",
                -2: "Treiber drücken GBP/JPY: Short-Setups bevorzugen, Longs meiden."}[stufe]
        risiko = "normal" if abs(stufe) == 2 else "halbiert"
    if iv.get("alarm") and stufe < 0:
        hinweise.append("Interventionsgefahr verstärkt den Short-Bias – Ausschläge können heftig sein.")
    if vix_alarm:
        hinweise.append("VIX-Sprung: Risikoflucht – Stops weiter, Größe kleiner.")
        if risiko == "normal":
            risiko = "halbiert"
    if setup_trend in (1, -1) and stufe != 0 and code not in ("abwarten", "long_meiden"):
        hinweise.append("Setup-Richtung passt zu den Treibern." if setup_trend * stufe > 0 else
                        "Setup-Richtung widerspricht den Treibern → Setup auslassen oder halbe Größe.")
    return {"code": code, "titel": titel, "emoji": emoji, "farbe": farbe, "text": text, "risiko": risiko,
            "hinweise": hinweise}


def berechnen(con, jetzt: datetime | None = None, *, kurse: dict | None = None, setup_trend: int | None = None,
              vorher: int | None = None) -> dict:
    jetzt = jetzt or jetzt_berlin()
    heute = fx_tag(jetzt).isoformat()
    live = _live_kurse(kurse)
    items = []
    for fn in (lambda: intervention(con, jetzt, heute, live), lambda: jgb2(con, jetzt), lambda: us10(con, jetzt),
               lambda: uk2(con, jetzt), lambda: spread2(con, jetzt), lambda: vix(con, jetzt),
               lambda: nikkei(con, jetzt), lambda: uk30(con, jetzt, heute, live), lambda: brent(con, jetzt),
               lambda: cftc(con, jetzt)):
        try:
            items.append(fn())
        except Exception as e:  # noqa: BLE001  ein kaputter Treiber darf den Rest nicht stoppen
            log.warning("Treiber: %s", e)
    vorhanden = {i["id"] for i in items}
    items += [_item(d, text="Fehler beim Berechnen") for d in DEFINITIONEN if d.id not in vorhanden]
    items.sort(key=lambda i: [d.id for d in DEFINITIONEN].index(i["id"]))
    verf = [i for i in items if i["signal"] is not None]
    gewicht = sum(i["gewicht"] for i in verf)
    score = round(100 * sum(i["gewicht"] * i["signal"] for i in verf) / gewicht) if gewicht >= 40 else None
    if vorher is None:
        vorher = kv_get(con, "treiber:stufe")
    stufe = stufe_aus_score(score, vorher)
    iv = next(i for i in items if i["id"] == "intervention")
    vx = next(i for i in items if i["id"] == "vix")
    kurs = live.get("gbpjpy")
    if kurs is None:
        r = con.execute("SELECT close FROM fxdaily WHERE pair='gbpjpy' ORDER BY date DESC LIMIT 1").fetchone()
        kurs = r["close"] if r else None
    ts = termine(con, jetzt, kurs)

    def gross_und_nah(t: dict) -> bool:
        gross = (t.get("faktor") or 0) >= GROSS_FAKTOR or t["typ"] == "UK-Haushalt"
        sekunden = (datetime.fromisoformat(t["ts"]) - jetzt).total_seconds()
        return gross and -1800 <= sekunden <= ABWARTEN_VORLAUF_STD * 3600
    nah = next((t for t in ts if gross_und_nah(t)), None)
    emp = empfehlung(score, stufe, iv, bool(vx.get("alarm")), nah, setup_trend)
    heute_berlin = jetzt.date().isoformat()
    hinweis = None
    for t in ts:
        if t["ts"][:10] != heute_berlin or datetime.fromisoformat(t["ts"]) <= jetzt:
            continue
        zeit = datetime.fromisoformat(t["ts"]).astimezone(TZ_BERLIN).strftime("%H:%M")
        if t["typ"] == "UK-Haushalt":
            hinweis = f"{zeit} UK-Haushalt: großes Pfund-Risiko (2024: −1,7 % am Folgetag)"
            break
        if (t.get("faktor") or 0) >= 1.25:
            hinweis = (f"{zeit} {t['kurz']}: Ø {de_zahl(t['faktor'], 1)}× Bewegung"
                       + (f" (~{de_zahl(t['pips'], 0)} P)" if t.get("pips") else ""))
            break
    return {
        "stand": jetzt.isoformat(), "score": score, "stufe": stufe, "empfehlung": emp, "items": items,
        "abdeckung": {"n": len(verf), "von": len(DEFINITIONEN), "gewicht": gewicht},
        "fehlend": [i["kurz"] for i in items if i["signal"] is None],
        "intervention": {k: iv.get(k) for k in ("stufe", "alarm", "grad", "warnungen", "wert")},
        "vix_alarm": bool(vx.get("alarm")), "termine": ts, "termin_hinweis": hinweis, "kurs": kurs,
    }


def merken(con, tb: dict) -> None:
    if tb.get("score") is not None:
        kv_set(con, "treiber:stufe", tb["stufe"])


# ------------------------------------------------------------
# Texte
# ------------------------------------------------------------
def top_treiber(tb: dict, n: int = 4, mindestens: float = 1.0) -> list[dict]:
    """Die stärksten Treiber nach Beitrag zum Score (Beitrag = Gewicht × Signal, mindestens 1 Punkt)."""
    return sorted([i for i in tb["items"] if i.get("beitrag") and abs(i["beitrag"]) >= mindestens],
                  key=lambda i: -abs(i["beitrag"]))[:n]


def treiber_zeile(tb: dict, n: int = 4) -> str:
    return " · ".join(f"{'▲' if i['beitrag'] > 0 else '▼'} {i['kurz']} {i['kurz_wert']}".rstrip() for i in top_treiber(tb, n))


def whatsapp_block(tb: dict | None, kuerze: int = 0) -> str:
    """kuerze 0 = voll, 1 = ohne Hinweise, 2 = minimal (für die Fallback-Stufen des Reports)."""
    if not tb:
        return "EMPFEHLUNG: ⚪ Treiber nicht verfügbar"
    e = tb["empfehlung"]
    if tb["score"] is None:
        return f"EMPFEHLUNG: ⚪ KEINE DATEN ({tb['abdeckung']['n']}/{tb['abdeckung']['von']} Treiber)"
    z = [f"EMPFEHLUNG: {e['emoji']} {e['titel']} (Treiber {tb['score']:+d})"]
    if kuerze < 2:
        z.append(f"{e['text']} Risiko: {e['risiko']}.")
    zeile = treiber_zeile(tb, 4 if kuerze == 0 else 3)
    if zeile:
        z.append(zeile)
    if tb.get("termin_hinweis") and kuerze < 2 and e["code"] != "abwarten":
        z.append(f"⚠️ {tb['termin_hinweis']}")
    if kuerze == 0:
        z += e.get("hinweise", [])[:1]
    return "\n".join(z)


def melden(con, tb: dict, jetzt: datetime | None = None, senden=None) -> str | None:
    """WhatsApp nur bei echten Wechseln: Interventionsalarm an, oder Wechsel zwischen LONG / NEUTRAL-Bereich / SHORT.
    Richtungswechsel nur Mo–Fr 07–22 Uhr, höchstens 3 am Tag, 2 Std Abstand. Abschaltbar: TREIBER_ALARM=0."""
    if not ALARM_AN or tb.get("score") is None:
        return None
    from .common import whatsapp
    senden = senden or whatsapp
    jetzt = jetzt or jetzt_berlin()
    alt = kv_get(con, "treiber:meldung", None)
    klasse = 1 if tb["stufe"] == 2 else -1 if tb["stufe"] == -2 else 0
    alarm = bool(tb["intervention"].get("alarm"))
    neu = {"klasse": klasse, "alarm": alarm, "gesendet": [], "letzte": 0}
    if alt is None:
        kv_set(con, "treiber:meldung", neu)
        return None
    heute = jetzt.date().isoformat()
    gesendet = [t for t in alt.get("gesendet", []) if t.startswith(heute)]
    text = None
    if alarm and not alt.get("alarm") and time.time() - alt.get("letzte_alarm", 0) > 6 * 3600:
        iv = tb["intervention"]
        text = (f"🚨 GBP/JPY: Interventionsgefahr {iv.get('grad')}\n"
                f"{next(i['text'] for i in tb['items'] if i['id'] == 'intervention')}\n"
                f"Empfehlung: {tb['empfehlung']['titel']} – {tb['empfehlung']['text']}")
        neu["letzte_alarm"] = time.time()
    elif (klasse != alt.get("klasse") and jetzt.weekday() < 5 and 7 <= jetzt.hour < 22 and len(gesendet) < 3
          and time.time() - alt.get("letzte", 0) >= 2 * 3600):
        e = tb["empfehlung"]
        text = (f"🧭 GBP/JPY Treiber-Update {jetzt:%H:%M}\n{e['emoji']} {e['titel']} (Score {tb['score']:+d})\n"
                f"{treiber_zeile(tb, 4)}\n{e['text']}")
        if e.get("hinweise"):
            text += f"\n{e['hinweise'][0]}"
        gesendet.append(jetzt.isoformat())
        neu["letzte"] = time.time()
    neu["letzte_alarm"] = neu.get("letzte_alarm", alt.get("letzte_alarm", 0))
    neu["letzte"] = neu["letzte"] or alt.get("letzte", 0)
    neu["gesendet"] = gesendet
    if text and DASHBOARD_URL:
        text += f"\n📊 {DASHBOARD_URL}"
    if text:
        if not senden(text):
            return None   # beim nächsten Lauf erneut versuchen
    kv_set(con, "treiber:meldung", neu)
    return text
