"""Vortage fürs Dashboard: je FX-Tag die 15-Min-Kerzen, alle gelieferten Setups mit Ergebnis und der Morgenreport.

- kerzen_sichern(): holt fehlende Tageskerzen der letzten TAGE FX-Tage mit EINEM Tiingo-Abruf und speichert sie.
- schreiben(): public/tage.json (neueste zuerst). Läuft nach dem Abschluss um 23:05 (evaluate.py) und
  einmal am Tag beim Export, falls die Datei fehlt oder veraltet ist.
Keine Anlageberatung.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from .common import (PUBLIC_DIR, fx_tag, fx_tag_ende, fx_tag_start, faellig, get_logger, jetzt_berlin, parse_iso,
                     schreibe_json)

log = get_logger("tage")
TAGE = 30
SCHEMA = """CREATE TABLE IF NOT EXISTS tageskerzen (date TEXT PRIMARY KEY, kerzen_json TEXT, gespeichert TEXT);"""
ENDE = ("tp", "sl", "zeit")


def letzte_tage(heute: date, n: int = TAGE) -> list[str]:
    """Die n FX-Tage (Mo–Fr) vor `heute`, neueste zuerst."""
    erg, d = [], heute - timedelta(days=1)
    while len(erg) < n:
        if d.weekday() < 5:
            erg.append(d.isoformat())
        d -= timedelta(days=1)
    return erg


def kerzen_sichern(con, jetzt: datetime | None = None) -> int:
    """Fehlende, abgeschlossene Tage nachladen (15 Min). Gibt die Zahl neu gespeicherter Tage zurück."""
    from .sources import prices as px_src
    con.executescript(SCHEMA)
    jetzt = jetzt or datetime.now(timezone.utc)
    vorhanden = {r[0] for r in con.execute("SELECT date FROM tageskerzen")}
    fehlend = [d for d in letzte_tage(fx_tag(jetzt)) if d not in vorhanden
               and fx_tag_ende(date.fromisoformat(d)) <= jetzt]
    if not fehlend:
        return 0
    start = fx_tag_start(date.fromisoformat(min(fehlend))) - timedelta(hours=2)
    daten = px_src._get("/tiingo/fx/gbpjpy/prices", {
        "startDate": start.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "endDate": (jetzt + timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "resampleFreq": "15min"})
    gruppen: dict[str, list] = {}
    for c in daten or []:
        try:
            ts = parse_iso(c["date"])
            k = [int(ts.timestamp()), round(float(c["open"]), 3), round(float(c["high"]), 3),
                 round(float(c["low"]), 3), round(float(c["close"]), 3)]
        except (KeyError, TypeError, ValueError):
            continue
        gruppen.setdefault(fx_tag(ts + timedelta(seconds=1)).isoformat(), []).append(k)
    neu = 0
    for d in fehlend:
        ks = sorted(gruppen.get(d, []))
        if len(ks) >= 20:   # Feiertage mit kaum Kursen nicht speichern (werden später erneut versucht)
            con.execute("INSERT OR REPLACE INTO tageskerzen(date, kerzen_json, gespeichert) VALUES(?,?,?)",
                        (d, json.dumps(ks, separators=(",", ":")), jetzt.isoformat()))
            neu += 1
    con.commit()
    log.info("Tageskerzen: %d von %d fehlenden Tagen gespeichert", neu, len(fehlend))
    return neu


def _ergebnis(state: str | None, trade_json: str | None, result_json: str | None) -> dict:
    """Einheitliches Ergebnis: grund tp | sl | zeit | kein_entry | offen | wartet, dazu Entry/Exit/Pips.
    Das Endergebnis von 23:05 (Tiingo, nach 3 Pips Kosten) hat Vorrang vor dem Live-Stand."""
    res = json.loads(result_json) if result_json else None
    tr = json.loads(trade_json) if trade_json else None
    if res:
        return {**res, "endgueltig": True}
    if tr and state in ENDE:
        return {**tr, "grund": state, "endgueltig": False}
    if tr and state == "läuft":
        return {**tr, "grund": "offen", "endgueltig": False}
    return {"grund": "kein_entry" if state == "verfallen" else "wartet", "endgueltig": False}


def tag_daten(con, d: str, kerzen: list | None) -> dict:
    tag = date.fromisoformat(d)
    setups = []
    row = con.execute("SELECT * FROM setups WHERE date=?", (d,)).fetchone() if _tabelle(con, "setups") else None
    if row:
        s = json.loads(row["data_json"])
        eintrag = {"art": "setup", "name": f"{s.get('name', 'London-Ausbruch')} (09:00)", "status": s.get("status"),
                   "grund_kein": s.get("grund"), "orders": s.get("orders", []), "start": s.get("start"),
                   "ende": s.get("ende"), "schluss": s.get("schluss"), "spanne": s.get("spanne"),
                   "spanne_von": "01:00", "trend": s.get("trend")}
        if s.get("status") == "aktiv":
            eintrag["ergebnis"] = _ergebnis(row["state"], row["trade_json"], row["result_json"])
        setups.append(eintrag)
    if _tabelle(con, "amd"):
        for r in con.execute("SELECT * FROM amd WHERE date=? AND phase='signal' ORDER BY variante", (d,)):
            a = json.loads(r["data_json"])
            setups.append({"art": "369" if r["variante"].startswith("z369") else "amd", "id": r["variante"],
                           "name": a.get("name", r["variante"]), "status": "aktiv", "signal_zeit": a.get("signal_zeit"),
                           "orders": [{"richtung": a["richtung"], "entry": a["entry"], "sl": a["sl"], "tp": a["tp"],
                                       "risiko_pips": a.get("risiko_pips"), "ziel_pips": a.get("ziel_pips")}],
                           "tp2": a.get("tp2"), "typ": a.get("typ", "markt"), "spanne": a.get("spanne"),
                           "spanne_von": a.get("range_von"), "start": a.get("range_bis"),
                           "sweep": [a.get("sweep_zeit"), a.get("sweep_extrem")],
                           "ergebnis": _ergebnis(r["state"], r["trade_json"], r["result_json"])})
    rep = con.execute("SELECT r.date, r.bias, r.range_low, r.range_high, r.message, e.close, e.high, e.low, "
                      "e.change_pips, e.in_range, e.direction_hit FROM reports r LEFT JOIN evaluations e "
                      "ON e.date=r.date WHERE r.date=?", (d,)).fetchone()
    report = None
    if rep:
        report = {"bias": rep["bias"], "range": [rep["range_low"], rep["range_high"]], "message": rep["message"],
                  "eval": {k: rep[k] for k in ("close", "high", "low", "change_pips", "in_range", "direction_hit")}
                  if rep["close"] is not None else None}
    return {"date": d, "fx_window": [fx_tag_start(tag).timestamp(), fx_tag_ende(tag).timestamp()],
            "kerzen": kerzen or [], "setups": setups, "report": report}


def _tabelle(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def daten(con, jetzt: datetime | None = None) -> dict:
    con.executescript(SCHEMA)
    jetzt = jetzt or jetzt_berlin()
    heute = fx_tag(jetzt)
    kerzen = {r["date"]: json.loads(r["kerzen_json"]) for r in con.execute("SELECT * FROM tageskerzen")}
    tage = []
    for d in letzte_tage(heute):
        t = tag_daten(con, d, kerzen.get(d))
        if t["kerzen"] or t["setups"] or t["report"]:
            tage.append(t)
    return {"erstellt": jetzt.isoformat(), "fuer": heute.isoformat(), "tage": tage}


def schreiben(con, jetzt: datetime | None = None, laden: bool = True) -> None:
    """Fehlende Kerzen nachladen (wenn `laden`) und tage.json schreiben. Fehler beim Laden stoppen den Export nicht."""
    if laden:
        try:
            kerzen_sichern(con)
        except Exception as e:  # noqa: BLE001
            log.warning("Tageskerzen: %s", e)
    schreibe_json("tage.json", daten(con, jetzt))


def bei_bedarf(con, jetzt: datetime) -> None:
    """Aus dem Minuten-Export: einmal pro FX-Tag neu schreiben, wenn die Datei fehlt oder von gestern ist."""
    try:
        alt = json.loads((PUBLIC_DIR / "tage.json").read_text()).get("fuer")
    except (OSError, ValueError):
        alt = None
    if alt != fx_tag(jetzt).isoformat() and faellig(con, "tage_export", 1800):
        schreiben(con, jetzt)
