"""AMD-Muster (Akkumulation – Manipulation – Distribution) live verfolgen.

Jede Minute (refresh.py): 15-Min-Kerzen des laufenden FX-Tags aus live.json -> strategien.amd_erkennen
für beide Varianten (Asien → London, London → New York). Nachricht bei
  - Verdacht (Fehlausbruch läuft, noch nicht zurück in der Spanne)
  - Signal (Kerze schließt zurück in der Spanne -> Entry/SL/TP in Gegenrichtung)
  - Verdacht aufgehoben (echter Ausbruch / kein Rücklauf)
  - Ziel / Stop / Tagesende
Abschluss mit vollständigen Tiingo-15-Min-Kerzen um 23:05 (evaluate.py). Die Regeln sind identisch mit
dem Backtest (backtest.py, Variante amd_london / amd_ny). WhatsApp nur für Varianten mit ✅ im Backtest,
außer IMMER_MELDEN (Standard: amd_london): Entry/SL/TP mit den letzten Ergebnissen und die TP/SL-Meldung.
Keine Anlageberatung.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

from . import forecast
from .common import TZ_BERLIN, de_zahl, fx_tag, fx_tag_ende, fx_tag_start, get_logger, jetzt_berlin
from .sources import prices as px_src
from .strategien import (AMD_VARIANTEN, PIP, Z369_VARIANTEN, amd_erkennen, amd_setup, simuliere, tm, z369_erkennen,
                         z369_setup, z369_zeiten)

log = get_logger("amd")
KOSTEN_PIPS = 3.0
SCHEMA = """CREATE TABLE IF NOT EXISTS amd (
  date TEXT, variante TEXT, phase TEXT, data_json TEXT, state TEXT, trade_json TEXT, result_json TEXT, updated TEXT,
  PRIMARY KEY (date, variante)
);"""
RICHTUNG = {1: "LONG", -1: "SHORT"}
# live verfolgt: beide AMD-Varianten + je Zeitzone die Standard-Variante der 3-6-9 Time Map (Rücklauf, 2R)
LIVE = AMD_VARIANTEN + [p for p in Z369_VARIANTEN if p["live"]]
ALLE_MELDEN = __import__("os").getenv("MUSTER_ALLE_MELDEN", "0") == "1"
# Varianten, deren Entry-Signale und Ergebnisse (TP/SL) immer per WhatsApp kommen – auch ohne ✅ im Backtest.
# Fatihs Wunsch (29.10.2026): Asien → London. Anpassbar über MUSTER_IMMER=amd_london,amd_ny (leer = keine).
IMMER_MELDEN = {v.strip() for v in __import__("os").getenv("MUSTER_IMMER", "amd_london").split(",") if v.strip()}
GRUND_TEXT = {"tp": "✅ TP", "sl": "❌ SL", "zeit": "⏹ Tagesende"}


def bewaehrt(vid: str) -> tuple[bool, dict | None]:
    """WhatsApp nur für Varianten, die im letzten Wochen-Backtest nach Kosten klar positiv waren (✅)."""
    from .backtest import urteil
    from .common import PUBLIC_DIR
    try:
        k = json.loads((PUBLIC_DIR / "backtest.json").read_text())["setups"][vid]["kennzahlen"]
    except (OSError, ValueError, KeyError):
        return False, None
    return urteil(k)[0] == "✅", k


def _ist369(p_or_d) -> bool:
    return str(p_or_d.get("variante") or p_or_d.get("id", "")).startswith("z369")


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
    return next(p for p in AMD_VARIANTEN + Z369_VARIANTEN if p["id"] == vid)


def verlauf(con, vid: str, vor_datum: str | None = None, n: int = 5) -> list[dict]:
    """Letzte abgeschlossene Live-Signale einer Variante (neueste zuerst): date, richtung, grund, pips.
    Nimmt das Tiingo-Endergebnis (23:05, nach Kosten), sonst den Live-Stand (TP/SL/Tagesende)."""
    _db(con)
    sql = "SELECT * FROM amd WHERE variante=? AND phase='signal'" + (" AND date<?" if vor_datum else "") + " ORDER BY date DESC"
    erg = []
    for r in con.execute(sql, (vid, vor_datum) if vor_datum else (vid,)):
        res = json.loads(r["result_json"]) if r["result_json"] else None
        tr = json.loads(r["trade_json"]) if r["trade_json"] else None
        quelle = res if res and res.get("grund") in GRUND_TEXT else (tr if r["state"] in GRUND_TEXT and tr else None)
        if not quelle:
            continue
        erg.append({"date": r["date"], "richtung": quelle.get("richtung") or json.loads(r["data_json"])["richtung"],
                    "grund": quelle["grund"], "pips": quelle.get("pips")})
        if len(erg) >= n:
            break
    return erg


def _vz(x: float, d: int = 0) -> str:
    """Vorzeichen-Zahl im deutschen Format: +32 / −5,7."""
    return f"{x:+.{d}f}".replace(".", ",").replace("-", "−")


def _kurz_datum(d: str) -> str:
    return f"{d[8:10]}.{d[5:7]}."


def historie_text(con, vid: str, vor_datum: str | None = None) -> str:
    """Kurzer Block für die WhatsApp: letzte Live-Ergebnisse + Backtest-Bilanz der Variante."""
    z = []
    live = verlauf(con, vid, vor_datum)
    if live:
        z.append("Letzte Signale live:")
        z += [f"• {_kurz_datum(t['date'])} {RICHTUNG.get(t['richtung'], '?')} {GRUND_TEXT[t['grund']]}"
              + (f" {_vz(t['pips'])} P" if t.get("pips") is not None else "") for t in live]
    else:
        z.append("Letzte Signale live: noch keine abgeschlossenen.")
    ok, k = bewaehrt(vid)
    if k and k.get("n"):
        from .backtest import urteil
        z.append(f"Backtest 2 J.: {k['n']} Trades, {de_zahl(k.get('tp_quote', k.get('trefferquote', 0)), 0)} % TP, "
                 f"Ø {_vz(k.get('avg_pips', 0), 1)} P nach Kosten {urteil(k)[0]}")
        try:
            from .common import PUBLIC_DIR
            bt = json.loads((PUBLIC_DIR / "backtest.json").read_text())["setups"][vid].get("letzte", [])[-5:]
        except (OSError, ValueError, KeyError):
            bt = []
        if bt:
            z.append("Backtest letzte 5: " + ", ".join(
                f"{_kurz_datum(t['date'])} {GRUND_TEXT.get(t['grund'], t['grund'])}" for t in reversed(bt)))
        if not ok:
            z.append("Im Backtest nicht profitabel → nur kleine Größe.")
    return "\n".join(z)


def text_verdacht(d: dict) -> str:
    if _ist369(d):
        unten = d["sweep_seite"] < 0
        return (f"🕵️ 3-6-9 Sweep ({d['name']})\n"
                f"Spanne {d['range_von']}–{d['range_bis']}: {de_zahl(d['spanne'][1])} – {de_zahl(d['spanne'][0])}\n"
                f"{'Unter das Tief' if unten else 'Über das Hoch'} gelaufen bis {de_zahl(d['sweep_extrem'])} ({d['sweep_zeit']}).\n"
                f"Signal erst, wenn bis {d['bis']} eine kräftige 5-Min-Kerze {'über' if unten else 'unter'} "
                f"{de_zahl(d['mss'])} schließt (Strukturbruch). Noch nicht einsteigen.")
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


def text_signal(d: dict, treiber_stufe: int | None = None, historie: str | None = None) -> str:
    r = d["richtung"]
    z = [f"🎯 {'3-6-9' if _ist369(d) else 'AMD'}-Signal {RICHTUNG[r]} ({d['name']}) {d['signal_zeit']}",
         f"{'Limit-Entry' if d.get('typ') == 'limit' else 'Entry ~'} {de_zahl(d['entry'])} | SL {de_zahl(d['sl'])} (−{d['risiko_pips']} P) | "
         f"TP {de_zahl(d['tp'])} (+{d['ziel_pips']} P) | TP2 {de_zahl(d['tp2'])}",
         (f"Sweep {'unter' if d['sweep_seite'] < 0 else 'über'} die Spanne bis {de_zahl(d['sweep_extrem'])}, Strukturbruch "
          f"{'über' if r > 0 else 'unter'} {de_zahl(d['mss'])} um {d['signal_zeit']}. Limit gültig bis {d['gueltig_bis']}."
          if _ist369(d) else
          f"Fehlausbruch {'unter' if d['sweep_seite'] < 0 else 'über'} die Spanne bis {de_zahl(d['sweep_extrem'])}, "
          f"jetzt zurück drin → Distribution Richtung {'Hoch' if r > 0 else 'Tief'} der Spanne.")]
    if treiber_stufe:
        z.append("Treiber stützen die Richtung." if treiber_stufe * r > 0 else
                 "Achtung: Treiber zeigen in die Gegenrichtung → halbe Größe oder auslassen.")
    if historie:
        z.append(historie)
    z.append("Neue Regel, Trefferquote wird gemessen. Keine Anlageberatung.")
    return "\n".join(z)


def _trade(d: dict, kerzen: list[tuple], laufend: bool, kosten: float = 0.0):
    p = _variante(d["variante"])
    setup = z369_setup(d, p) if _ist369(d) else amd_setup(d, p, _hm(d["start"]))
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
    jetzt_naiv = jetzt.astimezone(TZ_BERLIN).replace(tzinfo=None)
    k5_fertig = [k for k in k5 if k[0] + timedelta(minutes=5) <= jetzt_naiv]
    for p in LIVE:
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
                    symbol = {"tp": "✅ Ziel erreicht", "sl": "❌ Stop erreicht", "zeit": "⏹ zum Tagesende geschlossen"}[neu_state]
                    txt = (f"{symbol} ({d['name']}): {RICHTUNG[tr.richtung]} {de_zahl(tr.entry)} → "
                           f"{de_zahl(tr.exit)} = {tr.pips:+.0f} Pips | {tr.t_exit:%H:%M}")
                    bisher = verlauf(con, p["id"], n=20)
                    if bisher:
                        tp_n = sum(1 for t in bisher if t["grund"] == "tp")
                        sl_n = sum(1 for t in bisher if t["grund"] == "sl")
                        summe = sum(t["pips"] or 0 for t in bisher)
                        txt += (f"\nBilanz live (letzte {len(bisher)}): {tp_n}× TP, {sl_n}× SL, "
                                f"{len(bisher) - tp_n - sl_n}× Tagesende, Summe {_vz(summe)} Pips")
                    texte.append(txt)
            continue
        erg = z369_erkennen(k5_fertig, atr, p, heute) if _ist369(p) else amd_erkennen(kerzen, atr, p)
        if not erg or not k5_fertig:
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
            texte.append(text_signal(erg, treiber_stufe, None if _ist369(p) else historie_text(con, p["id"], heute)))
        elif erg["phase"] == "kein" and alt_phase == "manipulation":
            texte.append(f"⏹ Verdacht aufgehoben ({erg['name']}): {erg['grund']}.")
    con.commit()
    if texte and senden:
        for t in texte:
            vid = next((p["id"] for p in LIVE if f"({p['name']})" in t), None)
            ok, _ = bewaehrt(vid) if vid else (False, None)
            # immer gemeldete Varianten: Entry-Signal und Ergebnis, nicht die Vorwarnungen (Verdacht)
            immer = vid in IMMER_MELDEN and not t.startswith(("🕵️", "⏹ Verdacht"))
            if ok or ALLE_MELDEN or immer:
                senden(t)
            else:
                log.info("Nur Dashboard (Backtest nicht positiv): %s", t.splitlines()[0])
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
            roh = px_src.kerzen("gbpjpy", "5min" if _ist369(d) else "15min", start - timedelta(hours=1))
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
    for p in LIVE:
        r = heute_rows.get(p["id"])
        if _ist369(p):
            a, m, dz, *_ = z369_zeiten(heute, p["tz"])
        else:
            a, m, dz = p["range_von"], p["range_bis"], p["manip_bis"]
        ok, k = bewaehrt(p["id"])
        varianten.append({"id": p["id"], "name": p["name"], "familie": "369" if _ist369(p) else "amd",
                          "bewaehrt": ok, "backtest": k, "range_von": a.strftime("%H:%M"),
                          "range_bis": m.strftime("%H:%M"), "manip_bis": dz.strftime("%H:%M"),
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
