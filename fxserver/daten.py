"""Daten holen und in SQLite ablegen (von Report, Auswertung und Refresh gemeinsam genutzt)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .common import PAARE, PIP_GROESSE, get_logger, kv_set
from .sources import calendar as cal_src
from .sources import markt as markt_src
from .sources import news as news_src
from .sources import prices as px_src
from .sources import yields as y_src

log = get_logger("daten")


def termine_aktualisieren(con: sqlite3.Connection) -> bool:
    try:
        termine = cal_src.lade_termine()
    except Exception as e:  # noqa: BLE001
        log.warning("Kalender: %s", e)
        return False
    for t in termine:
        con.execute("""INSERT INTO calendar(id, ts, currency, impact, title, forecast, previous, actual)
                       VALUES(:id, :ts, :currency, :impact, :title, :forecast, :previous, :actual)
                       ON CONFLICT(id) DO UPDATE SET forecast=excluded.forecast, previous=excluded.previous,
                       actual=CASE WHEN excluded.actual != '' THEN excluded.actual ELSE calendar.actual END,
                       impact=excluded.impact""", t)
    con.commit()
    log.info("Kalender: %d Termine", len(termine))
    return True


def news_aktualisieren(con: sqlite3.Connection) -> list[str]:
    meldungen, fehler = news_src.lade_alle()
    neu = 0
    for m in meldungen:
        cur = con.execute("""INSERT OR IGNORE INTO news(id, ts, source, title, url, summary)
                             VALUES(:id, :ts, :source, :title, :url, :summary)""", m)
        neu += cur.rowcount
    grenze = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    con.execute("DELETE FROM news WHERE ts < ?", (grenze,))
    con.commit()
    log.info("News: %d geladen, %d neu, Fehler: %s", len(meldungen), neu, fehler or "-")
    return fehler


def renditen_aktualisieren(con: sqlite3.Connection, mit_historie: bool = False) -> list[str]:
    werte, fehler = y_src.lade_alle(mit_historie)
    for d, serien in werte.items():
        for s, v in serien.items():
            con.execute("INSERT OR REPLACE INTO yields(date, series, value) VALUES(?, ?, ?)", (d, s, v))
    con.commit()
    log.info("Renditen: %d Tage, Fehler: %s", len(werte), fehler or "-")
    return fehler


def markt_aktualisieren(con: sqlite3.Connection) -> list[str]:
    """VIX, Nikkei, Brent -> Tabelle markt (Tagesschlüsse, laufender Tag mit aktuellem Kurs) + kv 'markt:live'."""
    werte, live, fehler = markt_src.lade_markt()
    for serie, reihe in werte.items():
        for d, v in reihe.items():
            con.execute("INSERT OR REPLACE INTO markt(date, series, value) VALUES(?, ?, ?)", (d, serie, v))
    con.commit()
    if live:
        kv_set(con, "markt:live", live)
    log.info("Markt: %s, Fehler: %s", {s: len(r) for s, r in werte.items()}, fehler or "-")
    return fehler


def cftc_aktualisieren(con: sqlite3.Connection) -> list[str]:
    daten, fehler = markt_src.lade_cftc()
    for waehrung, reihe in daten.items():
        for d, w in reihe.items():
            con.execute("INSERT OR REPLACE INTO markt(date, series, value) VALUES(?, ?, ?)", (d, f"cftc_{waehrung}_netto", w["netto"]))
            con.execute("INSERT OR REPLACE INTO markt(date, series, value) VALUES(?, ?, ?)", (d, f"cftc_{waehrung}_oi", w["oi"]))
    con.commit()
    log.info("CFTC: %s, Fehler: %s", {w: len(r) for w, r in daten.items()}, fehler or "-")
    return fehler


def fx_tage_aktualisieren(con: sqlite3.Connection, tage: int = 35) -> dict[str, list[dict]]:
    """Stundenkerzen der letzten `tage` Tage -> FX-Tage je Paar (3 Tiingo-Abrufe)."""
    start = datetime.now(timezone.utc) - timedelta(days=tage)
    erg = {}
    for paar in PAARE:
        try:
            ft = px_src.fx_tage(px_src.kerzen(paar, "1hour", start))
        except Exception as e:  # noqa: BLE001
            log.warning("Kurse %s: %s", paar, e)
            continue
        for t in ft:
            if t["n"] < 3:   # angebrochener Tag mit kaum Daten (z. B. Sonntagabend)
                continue
            con.execute("INSERT OR REPLACE INTO fxdaily(date, pair, open, high, low, close) VALUES(?,?,?,?,?,?)",
                        (t["date"], paar, t["o"], t["h"], t["l"], t["c"]))
        erg[paar] = ft
    con.commit()
    return erg


def letzte_rendite(con: sqlite3.Connection, serie: str) -> tuple[str | None, float | None, float | None]:
    """(Datum, Wert, Veränderung zum Vortag in Basispunkten)."""
    rows = con.execute("SELECT date, value FROM yields WHERE series=? ORDER BY date DESC LIMIT 2", (serie,)).fetchall()
    if not rows:
        return None, None, None
    delta = round((rows[0]["value"] - rows[1]["value"]) * 100, 1) if len(rows) > 1 else None
    return rows[0]["date"], rows[0]["value"], delta


def pip(paar: str) -> float:
    return PIP_GROESSE.get(paar, 0.01)
