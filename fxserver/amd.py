"""AMD-Muster (Akkumulation – Manipulation – Distribution) live verfolgen.

Jede Minute (refresh.py): 15-Min-Kerzen des laufenden FX-Tags aus live.json -> strategien.amd_erkennen
für beide Varianten (Asien → London, London → New York). Nachricht bei
  - Verdacht (Fehlausbruch läuft, noch nicht zurück in der Spanne)
  - Signal (Kerze schließt zurück in der Spanne -> Entry/SL/TP in Gegenrichtung)
  - Verdacht aufgehoben (echter Ausbruch / kein Rücklauf)
  - Ziel / Stop / Tagesende
Abschluss mit vollständigen Tiingo-15-Min-Kerzen um 23:05 (evaluate.py). Die Regeln sind identisch mit
dem Backtest (backtest.py, Variante amd_london / amd_ny). Keine Anlageberatung.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

from . import forecast
from .common import TZ_BERLIN, de_zahl, fx_tag, fx_tag_ende, fx_tag_start, get_logger, jetzt_berlin
from .sources import prices as px_src
from .strategien import AMD_VARIANTEN, PIP, amd_erkennen, amd_setup, simuliere, tm

log = get_logger("amd")
KOSTEN_PIPS = 3.0
SCHEMA = """CREATE TABLE IF NOT EXISTS amd (
  date TEXT, variante TEXT, phase TEXT, data_json TEXT, state TEXT, trade_json TEXT, result_json TEXT, updated TEXT,
  PRIMARY KEY (date, variante)
);"""
RICHTUNG = {1: "LONG", -1: "SHORT"}


def _hm(s: str) -> time:
    return time(int(s[:2]), int(s[3:]))


def kerzen_15(kerzen_5: list[tuple], jetzt: datetime) -> list[tuple]:
    """5-Min-Kerzen -> abgeschlossene 15-Min-Kerzen (Startzeit Berlin, naiv)."""
    gruppen: dict[datetime, list] = {}
    for t, o, h, l, c in kerzen_5:
        start = t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)
        g = gruppen.get(start)
        if g is None:
            gruppen[start] = [o, h, l, c]
        else:
            g[1], g[2], g[3] = max(g[1], h), min(g[2], l), c
    jetzt_naiv = jetzt.astimezone(TZ_BERLIN).replace(tzinfo=None)
    return [(t, *v) for t, v in sorted(gruppen.items()) if t + timedelta(minutes=15) <= jetzt_naiv]


def _db(con):
    con.executescript(SCHEMA)
    return con


def _atr(con, heute: str) -> float | None:
    from .setup import tage_bis
    return forecast.atr14(tage_bis(con, heute))


def _variante(vid: str) -> dict:
    return next(p for p in AMD_VARIANTEN if p["id"] == vid)


def text_verdacht(d: dict) -> str:
    unten = d["sweep_seite"] < 0
    rand = d["spanne"][1] if unten else d["spanne"][0]
    gegen = d["spanne"][0] if unten else d["spanne"][1]
    return (f"🕵️ AMD-Verdacht ({d['name']})\n"
            f"Akkumulation {d['range_von']}–{d['range_bis']}: {de_zahl(d['spanne'][1])} – {de_zahl(d['spanne'][0])} "
            f"({d['breite_pips']:.0f} P)\n"
            f"Manipulation: {'unter das Tief' if unten else 'über das Hoch'} gelaufen bis {de_zahl(d['sweep_extrem'])} "
            f"({d['sweep_zeit']})\n"
            f"Schließt eine 15-Min-Kerze wieder {'über' if unten else 'unter'} {de_zahl(rand)} → "
            f"{'LONG' if unten else 'SHORT'}-Signal Richtung {de_zahl(gegen)}. Noch nicht einsteigen.")


def text_signal(d: dict, treiber_stufe: int | None = None) -> str:
    r = d["richtung"]
    z = [f"🎯 AMD-Signal {RICHTUNG[r]} ({d['name']}) {d['signal_zeit']}",
         f"Entry ~{de_zahl(d['entry'])} | SL {de_zahl(d['sl'])} (−{d['risiko_pips']} P) | "
         f"TP {de_zahl(d['tp'])} (+{d['ziel_pips']} P) | TP2 {de_zahl(d['tp2'])}",
         f"Fehlausbruch {'unter' if d['sweep_seite'] < 0 else 'über'} die Spanne bis {de_zahl(d['sweep_extrem'])}, "
         f"jetzt zurück drin → Distribution Richtung {'Hoch' if r > 0 else 'Tief'} der Spanne."]
    if treiber_stufe:
        z.append("Treiber stützen die Richtung." if treiber_stufe * r > 0 else
                 "Achtung: Treiber zeigen in die Gegenrichtung → halbe Größe oder auslassen.")
    z.append("Neue Regel, Trefferquote wird gemessen. Keine Anlageberatung.")
    return "\n".join(z)


def _trade(d: dict, kerzen: list[tuple], laufend: bool, kosten: float = 0.0):
    setup = amd_setup(d, _variante(d["variante"]), _hm(d["start"]))
    return simuliere(setup, kerzen, kosten, d.get("date", ""), laufend=laufend)


def _trade_dict(tr) -> dict | None:
    if tr is None:
        return None
    return {"richtung": tr.richtung, "entry": tr.entry, "sl": tr.sl, "tp": tr.tp,
            "t_entry": tr.t_entry.strftime("%H:%M") if tr.t_entry else None, "exit": tr.exit,
            "t_exit": tr.t_exit.strftime("%H:%M") if tr.t_exit else None, "grund": tr.grund,
            "pips": tr.pips if tr.exit is not None else None}


def pruefen(con, jetzt: datetime | None = None, senden=None, treiber_stufe: int | None = None,
            kerzen_5: list[tuple] | None = None) -> list[str]:
    """Ein Durchlauf: erkennt Phasen, verfolgt Trades, schickt Meldungen bei Wechseln. Gibt die Texte zurück."""
    from .setup import kerzen_live
    _db(con)
    jetzt = jetzt or jetzt_berlin()
    heute = fx_tag(jetzt).isoformat()
    k5 = kerzen_5 if kerzen_5 is not None else kerzen_live(jetzt)
    kerzen = kerzen_15(k5, jetzt)
    atr = _atr(con, heute)
    if not kerzen or not atr:
        return []
    texte = []
    for p in AMD_VARIANTEN:
        row = con.execute("SELECT * FROM amd WHERE date=? AND variante=?", (heute, p["id"])).fetchone()
        alt = dict(row) if row else None
        if alt and alt["phase"] == "signal":
            d = json.loads(alt["data_json"])
            if alt["state"] in ("tp", "sl", "zeit"):
                continue
            tr = _trade(d, k5, laufend=True)   # 5-Min-Kerzen: Ziel/Stop schneller erkannt
            neu_state = "wartet" if tr is None else ("läuft" if tr.grund == "offen" else tr.grund)
            if neu_state != alt["state"]:
                con.execute("UPDATE amd SET state=?, trade_json=?, updated=? WHERE date=? AND variante=?",
                            (neu_state, json.dumps(_trade_dict(tr)), jetzt.isoformat(), heute, p["id"]))
                if neu_state in ("tp", "sl", "zeit"):
                    symbol = {"tp": "✅ AMD-Ziel erreicht", "sl": "❌ AMD-Stop erreicht", "zeit": "⏹ AMD zum Tagesende"}[neu_state]
                    texte.append(f"{symbol} ({d['name']}): {RICHTUNG[tr.richtung]} {de_zahl(tr.entry)} → "
                                 f"{de_zahl(tr.exit)} = {tr.pips:+.0f} Pips | {tr.t_exit:%H:%M}")
            continue
        erg = amd_erkennen(kerzen, atr, p)
        if not erg:
            continue
        erg["date"] = heute
        alt_phase = alt["phase"] if alt else None
        if erg["phase"] == alt_phase:
            if alt and alt["data_json"] != json.dumps(erg):   # z. B. tieferes Extrem während der Manipulation
                con.execute("UPDATE amd SET data_json=?, updated=? WHERE date=? AND variante=?",
                            (json.dumps(erg), jetzt.isoformat(), heute, p["id"]))
            continue
        if alt_phase == "kein":
            continue   # pro Tag und Variante ist "kein" endgültig
        con.execute("INSERT OR REPLACE INTO amd(date, variante, phase, data_json, state, updated) VALUES(?,?,?,?,?,?)",
                    (heute, p["id"], erg["phase"], json.dumps(erg), "wartet" if erg["phase"] == "signal" else "-",
                     jetzt.isoformat()))
        if erg["phase"] == "manipulation":
            texte.append(text_verdacht(erg))
        elif erg["phase"] == "signal":
            texte.append(text_signal(erg, treiber_stufe))
        elif erg["phase"] == "kein" and alt_phase == "manipulation":
            texte.append(f"⏹ AMD-Verdacht aufgehoben ({erg['name']}): {erg['grund']}.")
    con.commit()
    if texte and senden:
        for t in texte:
            senden(t)
    return texte


def abschliessen(con) -> None:
    """Signale abgeschlossener FX-Tage mit vollständigen Tiingo-15-Min-Kerzen auswerten."""
    _db(con)
    jetzt = datetime.now(timezone.utc)
    for row in con.execute("SELECT * FROM amd WHERE phase='signal' AND result_json IS NULL ORDER BY date").fetchall():
        tag = datetime.fromisoformat(row["date"]).date()
        if fx_tag_ende(tag) > jetzt:
            continue
        d = json.loads(row["data_json"])
        try:
            start = fx_tag_start(tag)
            roh = px_src.kerzen("gbpjpy", "15min", start - timedelta(hours=1))
            kerzen = [(k["t"].astimezone(TZ_BERLIN).replace(tzinfo=None), k["o"], k["h"], k["l"], k["c"])
                      for k in roh if start <= k["t"] < fx_tag_ende(tag)]
        except Exception as e:  # noqa: BLE001
            log.warning("AMD-Abschluss %s: %s", row["date"], e)
            continue
        tr = _trade(d, kerzen, laufend=False, kosten=KOSTEN_PIPS)
        erg = _trade_dict(tr) if tr else {"grund": "kein_entry"}
        con.execute("UPDATE amd SET result_json=?, state=? WHERE date=? AND variante=?",
                    (json.dumps(erg), tr.grund if tr else "kein_entry", row["date"], row["variante"]))
    con.commit()


def bilanz(con) -> dict:
    _db(con)
    pips = [json.loads(r["result_json"]).get("pips") for r in con.execute(
        "SELECT result_json FROM amd WHERE result_json IS NOT NULL")]
    pips = [p for p in pips if p is not None]
    n = len(pips)
    return {"signale": con.execute("SELECT COUNT(*) FROM amd WHERE phase='signal'").fetchone()[0], "trades": n,
            "treffer_pct": round(100 * sum(1 for p in pips if p > 0) / n) if n else None,
            "summe_pips": round(sum(pips)) if n else 0, "avg_pips": round(sum(pips) / n, 1) if n else None}


def export_daten(con, jetzt: datetime) -> dict:
    _db(con)
    heute = fx_tag(jetzt).isoformat()
    heute_rows = {r["variante"]: r for r in con.execute("SELECT * FROM amd WHERE date=?", (heute,))}
    varianten = []
    for p in AMD_VARIANTEN:
        r = heute_rows.get(p["id"])
        varianten.append({"id": p["id"], "name": p["name"], "range_von": p["range_von"].strftime("%H:%M"),
                          "range_bis": p["range_bis"].strftime("%H:%M"), "manip_bis": p["manip_bis"].strftime("%H:%M"),
                          "phase": r["phase"] if r else None, "state": r["state"] if r else None,
                          "daten": json.loads(r["data_json"]) if r else None,
                          "trade": json.loads(r["trade_json"]) if r and r["trade_json"] else None})
    liste = []
    for r in con.execute("SELECT * FROM amd WHERE phase='signal' AND date<? ORDER BY date DESC LIMIT 15", (heute,)):
        d = json.loads(r["data_json"])
        res = json.loads(r["result_json"]) if r["result_json"] else None
        liste.append({"date": r["date"], "name": d["name"], "richtung": d["richtung"], "entry": d["entry"],
                      "sl": d["sl"], "tp": d["tp"], "result": res})
    return {"varianten": varianten, "liste": liste, "bilanz": bilanz(con)}
