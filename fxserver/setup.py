"""Tages-Setup GBP/JPY: berechnen (09:00), live verfolgen (jede Minute), abschließen (23:05).

Regeln: fxserver/strategien.py (HAUPT_SETUP) – identisch mit dem Backtest.
Aufruf: python -m fxserver.setup            -> Setup berechnen, speichern, per WhatsApp senden
        python -m fxserver.setup --vorschau -> nur ausgeben
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, time, timedelta, timezone

from . import forecast
from .common import (PUBLIC_DIR, TZ_BERLIN, db, de_zahl, fx_tag, fx_tag_start, get_logger, jetzt_berlin,
                     whatsapp)
from .sources import prices as px_src
from .strategien import (HAUPT_SETUP, PIP, Kontext, Order, Setup, kein_setup_grund, orb, simuliere, spanne, tm,
                         trend_richtung)

log = get_logger("setup")
KOSTEN_PIPS = 3.0

SCHEMA = """CREATE TABLE IF NOT EXISTS setups (
  date TEXT PRIMARY KEY, status TEXT, data_json TEXT, state TEXT, trade_json TEXT, result_json TEXT, created TEXT
);"""


def _db():
    con = db()
    con.executescript(SCHEMA)
    return con


# ------------------------------------------------------------
# Daten
# ------------------------------------------------------------
def tage_bis(con, heute: str, n: int = 60) -> list[dict]:
    rows = con.execute("SELECT * FROM fxdaily WHERE pair='gbpjpy' AND date<? ORDER BY date DESC LIMIT ?",
                       (heute, n)).fetchall()
    return [{"date": r["date"], "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"]} for r in reversed(rows)]


def kerzen_tiingo(jetzt: datetime) -> list[tuple]:
    start = fx_tag_start(fx_tag(jetzt))
    roh = px_src.kerzen("gbpjpy", "15min", start - timedelta(hours=1))
    return [(k["t"].astimezone(TZ_BERLIN).replace(tzinfo=None), k["o"], k["h"], k["l"], k["c"])
            for k in roh if k["t"] >= start]


def kerzen_live(jetzt: datetime) -> list[tuple]:
    """5-Min-Kerzen des laufenden FX-Tags aus live.json (vom Alarm-Dienst)."""
    try:
        live = json.loads((PUBLIC_DIR / "live.json").read_text())
    except (OSError, ValueError):
        return []
    start = fx_tag_start(fx_tag(jetzt)).timestamp()
    erg = []
    for b in live.get("pairs", {}).get("gbpjpy", {}).get("ohlc", []):
        if b[0] >= start:
            t = datetime.fromtimestamp(b[0], TZ_BERLIN).replace(tzinfo=None)
            erg.append((t, b[1], b[2], b[3], b[4]))
    return erg


def kontext(con, heute: str, kerzen: list[tuple]) -> Kontext | None:
    tage = tage_bis(con, heute)
    atr = forecast.atr14(tage)
    if atr is None:
        return None
    return Kontext(heute, kerzen[0][1] if kerzen else tage[-1]["c"], atr, tage[-1], forecast.ema20(tage),
                   tage[-1]["c"] - tage[-6]["c"], kerzen)


# ------------------------------------------------------------
# Setup berechnen
# ------------------------------------------------------------
def _hm(t: time) -> str:
    return t.strftime("%H:%M")


def berechnen(ctx: Kontext) -> dict:
    p = HAUPT_SETUP
    trend_wert = trend_richtung(ctx)
    basis = {"date": ctx.datum, "name": p["name"], "id": p["id"], "trend": trend_wert,
             "atr_pips": round(ctx.atr / PIP), "start": _hm(p["range_bis"]), "ende": _hm(p["entry_bis"]),
             "schluss": _hm(p["schluss"])}
    grund = kein_setup_grund(ctx, p)
    s = spanne(ctx.kerzen, p["range_von"], p["range_bis"])
    if s:
        basis["spanne"] = [round(s[1], 3), round(s[0], 3)]
    if grund:
        return {**basis, "status": "kein_setup", "grund": grund, "orders": []}
    setup = orb(ctx, p)
    if not setup or not setup.orders:
        return {**basis, "status": "kein_setup", "grund": "Regel liefert heute keinen Einstieg", "orders": []}
    orders = [{"richtung": o.richtung, "entry": o.entry, "sl": o.sl, "tp": o.tp,
               "risiko_pips": round(abs(o.entry - o.sl) / PIP), "ziel_pips": round(abs(o.tp - o.entry) / PIP)}
              for o in setup.orders]
    return {**basis, "status": "aktiv", "typ": setup.typ, "orders": orders, "info": setup.info}


def als_setup(d: dict) -> Setup:
    hm = lambda s: time(int(s[:2]), int(s[3:]))  # noqa: E731
    return Setup(d["id"], d["name"], d.get("typ", "stop"),
                 [Order(o["richtung"], o["entry"], o["sl"], o["tp"]) for o in d["orders"]],
                 hm(d["start"]), hm(d["ende"]), hm(d["schluss"]), d.get("info", {}))


def text_setup(d: dict, hist: dict | None = None) -> str:
    pfeil = {1: "▲", -1: "▼", 0: "–"}[d["trend"]]
    if d["status"] != "aktiv":
        z = [f"⏸ GBP/JPY heute kein Setup", f"Grund: {d['grund']}."]
        if d.get("spanne"):
            z.append(f"Asien-Spanne: {de_zahl(d['spanne'][0])} – {de_zahl(d['spanne'][1])}")
        z.append(f"Trend: {forecast.TREND_TEXT[d['trend']]} {pfeil}")
        return "\n".join(z)
    z = [f"🎯 GBP/JPY Setup heute – {d['name']} {pfeil}"]
    for o in d["orders"]:
        art = "KAUF-Stop (Long)" if o["richtung"] > 0 else "VERKAUF-Stop (Short)"
        z += [f"{art}: {de_zahl(o['entry'])}",
              f"SL: {de_zahl(o['sl'])} (−{o['risiko_pips']} P) | TP: {de_zahl(o['tp'])} (+{o['ziel_pips']} P) | CRV 1:2"]
    z.append(f"Entry gültig bis {d['ende']}, spätestens {d['schluss']} schließen")
    if d.get("spanne"):
        z.append(f"Asien-Spanne {de_zahl(d['spanne'][0])} – {de_zahl(d['spanne'][1])} ({d['info'].get('breite_pips', 0):.0f} P)")
    if hist:
        z.append(f"Backtest 2012–22: {hist['trefferquote']:.0f} % Treffer, Ø {hist['avg_pips']:+.1f} P/Trade nach Kosten, ".replace(".", ",", 1) +
                 f"längste Verlustserie {hist['max_verlustserie']}")
    z.append("Mechanische Regel, keine Anlageberatung. Positionsgröße nach deinem Risiko.")
    return "\n".join(z)


def historie() -> dict | None:
    try:
        from pathlib import Path
        h = json.loads((Path(__file__).parent / "backtest_historie.json").read_text())
        return h["setups"][HAUPT_SETUP["id"]]["gesamt"]
    except (OSError, KeyError, ValueError):
        return None


def vorschau_text(con, jetzt: datetime) -> str | None:
    """Für den Morgenreport: Trendrichtung + bisherige Asien-Spanne (Levels final um 09:00)."""
    try:
        kerzen = kerzen_tiingo(jetzt)
    except Exception as e:  # noqa: BLE001
        log.warning("Vorschau: %s", e)
        kerzen = kerzen_live(jetzt)
    ctx = kontext(con, fx_tag(jetzt).isoformat(), kerzen)
    if not ctx:
        return None
    t = trend_richtung(ctx)
    s = spanne(kerzen, HAUPT_SETUP["range_von"], time(jetzt.hour, jetzt.minute))
    teile = [{1: "Aufwärtstrend → nur Long", -1: "Abwärtstrend → nur Short", 0: "Kein klarer Trend → heute vermutlich kein Setup"}[t]]
    if s:
        breite = (s[0] - s[1]) / PIP
        teile.append(f"Asien-Spanne bisher {de_zahl(s[1])} – {de_zahl(s[0])} ({breite:.0f} P, max. {HAUPT_SETUP['max_atr'] * ctx.atr / PIP:.0f})")
    teile.append("Finale Levels um 09:00")
    return "\n".join(teile)


# ------------------------------------------------------------
# Live verfolgen
# ------------------------------------------------------------
STATUS_TEXT = {"wartet": "wartet auf Ausbruch", "verfallen": "verfallen (kein Ausbruch)", "ausgelöst": "läuft",
               "tp": "TP erreicht", "sl": "SL erreicht", "zeit": "zum Tagesende geschlossen"}


def zustand(d: dict, kerzen: list[tuple], jetzt: datetime):
    tr = simuliere(als_setup(d), kerzen, 0.0, d["date"], laufend=True) if kerzen else None
    if tr is None:
        ende = time(*map(int, d["ende"].split(":")))
        tag_vorbei = fx_tag(jetzt.replace(tzinfo=TZ_BERLIN)).isoformat() > d["date"]
        return ("verfallen" if tag_vorbei or tm(jetzt) > tm(ende) else "wartet"), None
    if tr.grund == "offen":
        return "ausgelöst", tr
    return tr.grund, tr


def _trade_dict(tr) -> dict | None:
    if tr is None:
        return None
    return {"richtung": tr.richtung, "entry": tr.entry, "sl": tr.sl, "tp": tr.tp,
            "t_entry": tr.t_entry.strftime("%H:%M") if tr.t_entry else None,
            "exit": tr.exit, "t_exit": tr.t_exit.strftime("%H:%M") if tr.t_exit else None,
            "grund": tr.grund, "pips": tr.pips if tr.exit is not None else None, "r": tr.r if tr.exit is not None else None}


def meldung(alt: str, neu: str, tr) -> str | None:
    if neu == alt:
        return None
    seite = "LONG" if tr and tr.richtung > 0 else "SHORT"
    if neu == "verfallen":
        return "⏹ GBP/JPY-Setup verfallen: kein Ausbruch bis zum Ende des Entry-Fensters."
    teile = []
    if alt in ("wartet", None, "") and neu in ("ausgelöst", "tp", "sl", "zeit"):
        teile.append(f"🟢 Setup ausgelöst: {seite} {de_zahl(tr.entry)} ({tr.t_entry:%H:%M}) | SL {de_zahl(tr.sl)} | TP {de_zahl(tr.tp)}")
    if neu in ("tp", "sl", "zeit"):
        symbol = {"tp": "✅ TP erreicht", "sl": "❌ SL erreicht", "zeit": "⏹ Zum Tagesende geschlossen"}[neu]
        teile.append(f"{symbol}: {seite} {de_zahl(tr.entry)} → {de_zahl(tr.exit)} = {tr.pips:+.0f} Pips ({tr.r:+.1f} R, ohne Spread) | {tr.t_exit:%H:%M}".replace(f"{tr.r:+.1f}", f"{tr.r:+.1f}".replace(".", ",")))
    return "\n".join(teile) or None


def verfolgen(con=None, jetzt: datetime | None = None, senden=whatsapp) -> str | None:
    con = con or _db()
    con.executescript(SCHEMA)
    jetzt = jetzt or jetzt_berlin()
    heute = fx_tag(jetzt).isoformat()
    row = con.execute("SELECT * FROM setups WHERE date=? AND status='aktiv'", (heute,)).fetchone()
    if not row or row["state"] in ("tp", "sl", "zeit", "verfallen"):
        return None
    d = json.loads(row["data_json"])
    kerzen = kerzen_live(jetzt)
    neu, tr = zustand(d, kerzen, jetzt.replace(tzinfo=None))
    text = meldung(row["state"], neu, tr)
    if neu != row["state"]:
        con.execute("UPDATE setups SET state=?, trade_json=? WHERE date=?",
                    (neu, json.dumps(_trade_dict(tr)), heute))
        con.commit()
        log.info("Setup %s: %s -> %s", heute, row["state"], neu)
        if text:
            senden(text)
    return neu


# ------------------------------------------------------------
# Abschluss mit vollständigen 15-Min-Kerzen (23:05)
# ------------------------------------------------------------
def abschliessen(con) -> None:
    con.executescript(SCHEMA)
    jetzt = datetime.now(timezone.utc)
    offen = con.execute("SELECT * FROM setups WHERE status='aktiv' AND result_json IS NULL ORDER BY date").fetchall()
    for row in offen:
        from .common import fx_tag_ende
        ende = fx_tag_ende(datetime.fromisoformat(row["date"]).date())
        if ende > jetzt:
            continue
        d = json.loads(row["data_json"])
        try:
            start = fx_tag_start(datetime.fromisoformat(row["date"]).date())
            roh = px_src.kerzen("gbpjpy", "15min", start - timedelta(hours=1))
            kerzen = [(k["t"].astimezone(TZ_BERLIN).replace(tzinfo=None), k["o"], k["h"], k["l"], k["c"])
                      for k in roh if start <= k["t"] < ende]
        except Exception as e:  # noqa: BLE001
            log.warning("Abschluss %s: %s", row["date"], e)
            continue
        tr = simuliere(als_setup(d), kerzen, KOSTEN_PIPS, d["date"])
        erg = _trade_dict(tr) if tr else {"grund": "kein_entry"}
        con.execute("UPDATE setups SET result_json=?, state=COALESCE(?, state) WHERE date=?",
                    (json.dumps(erg), tr.grund if tr else "verfallen", row["date"]))
        log.info("Setup %s abgeschlossen: %s", row["date"], erg)
    con.commit()


def live_bilanz(con) -> dict:
    con.executescript(SCHEMA)
    rows = con.execute("SELECT date, status, state, data_json, trade_json, result_json FROM setups ORDER BY date DESC").fetchall()
    trades = []
    for r in rows:
        if r["result_json"]:
            res = json.loads(r["result_json"])
            if res.get("pips") is not None:
                trades.append(res["pips"])
    n = len(trades)
    return {"tage": len(rows), "aktive_tage": sum(1 for r in rows if r["status"] == "aktiv"), "trades": n,
            "treffer_pct": round(100 * sum(1 for p in trades if p > 0) / n) if n else None,
            "summe_pips": round(sum(trades)) if n else 0, "avg_pips": round(sum(trades) / n, 1) if n else None}


# ------------------------------------------------------------
def main(senden: bool = True) -> int:
    con = _db()
    jetzt = jetzt_berlin()
    heute = fx_tag(jetzt).isoformat()
    schon = con.execute("SELECT 1 FROM setups WHERE date=?", (heute,)).fetchone()
    if schon and senden:
        log.info("Setup für %s existiert schon", heute)
        return 0
    if not tage_bis(con, heute) or tage_bis(con, heute)[-1]["date"] < (jetzt.date() - timedelta(days=4)).isoformat():
        from .daten import fx_tage_aktualisieren
        fx_tage_aktualisieren(con, tage=70)
    try:
        kerzen = kerzen_tiingo(jetzt)
    except Exception as e:  # noqa: BLE001
        log.warning("Tiingo 15min: %s – nutze Live-Kerzen", e)
        kerzen = kerzen_live(jetzt)
    ctx = kontext(con, heute, kerzen)
    if not ctx:
        log.error("Zu wenig Tagesdaten für ein Setup")
        return 1
    d = berechnen(ctx)
    text = text_setup(d, historie())
    print(text)
    if senden:
        con.execute("INSERT OR REPLACE INTO setups(date, status, data_json, state, created) VALUES(?,?,?,?,?)",
                    (heute, d["status"], json.dumps(d), "wartet" if d["status"] == "aktiv" else "-", jetzt.isoformat()))
        con.commit()
        whatsapp(text, log)
        from . import export
        export.alles(con)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vorschau", action="store_true")
    raise SystemExit(main(senden=not ap.parse_args().vorschau))
