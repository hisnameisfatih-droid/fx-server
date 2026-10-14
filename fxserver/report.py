"""Morgenreport GBP/JPY (läuft Mo–Fr um 06:45 Uhr Berlin).

Aufruf: python -m fxserver.report [--nicht-senden] [--ausgeben] [--erneut]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from . import export, forecast, llm, treiber
from . import setup as setup_mod
from .common import (DASHBOARD_URL, PAAR_NAME, TZ_BERLIN, db, de_prozent, de_zahl, fx_tag, get_logger,
                     jetzt_berlin, whatsapp)
from .daten import (fx_tage_aktualisieren, letzte_rendite, markt_aktualisieren, news_aktualisieren,
                    renditen_aktualisieren, termine_aktualisieren)
from .news_klassifikation import klassifizieren
from .sources import prices as px_src
from .sources.yields import KERN, SERIEN

log = get_logger("report")
FLAGGE = {"GBP": "🇬🇧", "JPY": "🇯🇵", "USD": "🇺🇸"}
WOCHENTAG = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
PFEIL = {1: "▲", -1: "▼", 0: "–"}
MAX_ZEICHEN = 1500


def vortag(con, paar: str, heute: str):
    return con.execute("SELECT * FROM fxdaily WHERE pair=? AND date<? ORDER BY date DESC LIMIT 1",
                       (paar, heute)).fetchone()


def tage_bis(con, paar: str, heute: str, n: int = 60) -> list[dict]:
    rows = con.execute("SELECT * FROM fxdaily WHERE pair=? AND date<? ORDER BY date DESC LIMIT ?",
                       (paar, heute, n)).fetchall()
    return [{"date": r["date"], "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"]} for r in reversed(rows)]


def quoten_30(con) -> dict:
    rows = con.execute("SELECT * FROM evaluations ORDER BY date DESC LIMIT 30").fetchall()
    return forecast.quoten([dict(r) for r in rows])


def termine_heute(con, jetzt: datetime) -> list[dict]:
    start = jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = con.execute("SELECT * FROM calendar WHERE ts>=? AND ts<? ORDER BY ts",
                       (start.isoformat(), (start + timedelta(days=1)).isoformat()))
    return [dict(r) for r in rows]


def news_24h(con, n: int = 40) -> list[dict]:
    grenze = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = con.execute("SELECT * FROM news WHERE ts>=? ORDER BY COALESCE(relevance, 5) DESC, ts DESC LIMIT ?",
                       (grenze, n)).fetchall()
    return [dict(r) for r in rows]


def nachricht_bauen(*, jetzt, kurs, aenderung, usdjpy, uk10, jp10, texte, termine, prog, trend_wert,
                    vorschau, quoten, fehlend, treiber_daten=None) -> str:
    wirkung = {w["id"]: w["wirkung"] for w in texte.get("termin_wirkungen", []) if w.get("id")}
    news_score = int(texte.get("news_score") or 0)

    def kopf():
        z = [f"GBP/JPY — {WOCHENTAG[jetzt.weekday()]}, {jetzt:%d.%m.%Y}",
             (f"Kurs: {de_zahl(kurs)} ({de_prozent(aenderung)} seit gestern)" if kurs else "Kurs: n/v")
             + (f" | ATR {prog['atr_pips']} P" if prog else ""),
             f"USD/JPY {de_zahl(usdjpy)} | Gilt 10y {de_zahl(uk10, 2)}% | JGB 10y {de_zahl(jp10, 2)}%"]
        return "\n".join(z)

    def heute_block(mit_wirkung, max_n):
        if not termine:
            return "HEUTE\nKeine wichtigen Termine (GBP/JPY/USD)."
        zeilen = ["HEUTE"]
        for t in termine[:max_n]:
            zeit = datetime.fromisoformat(t["ts"]).astimezone(TZ_BERLIN)
            fp = " | ".join(x for x in (f"F {t['forecast']}" if t.get("forecast") else "",
                                        f"P {t['previous']}" if t.get("previous") else "") if x)
            z = f"{zeit:%H:%M} {'🔴' if t['impact'] == 'high' else '🟠'}{FLAGGE.get(t['currency'], '')} {t['title']}" + (f" ({fp})" if fp else "")
            if mit_wirkung and wirkung.get(t["id"]):
                z += f" – {wirkung[t['id']]}"
            zeilen.append(z)
        if len(termine) > max_n:
            zeilen.append(f"… +{len(termine) - max_n} im Dashboard")
        return "\n".join(zeilen)

    def prog_block():
        if not prog:
            return "PROGNOSE\nNicht berechenbar (Kursdaten fehlen)."
        z = ["PROGNOSE",
             f"Schluss (68 %): {de_zahl(prog['range_low'])} – {de_zahl(prog['range_high'])}",
             f"Erw. Hoch/Tief (50 %): {de_zahl(prog['exp_high'])} / {de_zahl(prog['exp_low'])}"]
        if prog.get("level_up"):
            z.append(f"Kippt über {de_zahl(prog['level_up'])} / unter {de_zahl(prog['level_down'])} (Vortag)")
        return "\n".join(z)

    def bias_block():
        return "\n".join(["BIAS",
                          f"Trend: {forecast.TREND_TEXT[trend_wert]} {PFEIL[trend_wert]} (Regel, hist. ~51 % Treffer)",
                          f"News: {forecast.NEWS_TEXT.get(news_score, 'neutral')} – {texte.get('news_grund', '')}"])

    def quote_zeile():
        if not quoten["n"]:
            return "Trefferquote 30T: noch keine Auswertung"
        n = f" (n={quoten['n']})" if quoten["n"] < 30 else ""
        news = f" | News {quoten['news_pct']}%" if quoten.get("news_pct") is not None else ""
        tr = f" | Treiber {quoten['treiber_pct']}%" if quoten.get("treiber_pct") is not None else ""
        return (f"Trefferquote 30T: Spanne {quoten['range_pct']}% | Trend {quoten['direction_pct']}%{news}{tr} | "
                f"Ø Abw. {de_zahl(quoten['avg_dev'], 0)} P{n}")

    def zusammensetzen(wirkung_an=True, max_termine=6, kurz=False, kuerze=0):
        lage_gbp, lage_jpy, risiken = texte.get("lage_gbp", ""), texte.get("lage_jpy", ""), texte.get("risiken", "")
        if kurz:
            lage_gbp, lage_jpy, risiken = (x.split(". ")[0].rstrip(".") + "." for x in (lage_gbp, lage_jpy, risiken))
        teile = [kopf(), treiber.whatsapp_block(treiber_daten, kuerze), f"LAGE\nGBP: {lage_gbp}\nJPY: {lage_jpy}",
                 heute_block(wirkung_an, max_termine), prog_block(), bias_block()]
        if vorschau:
            teile.append(f"SETUP (London 09:00)\n{vorschau}")
        teile += [f"RISIKEN\n{risiken}", quote_zeile()]
        fuss = []
        if fehlend:
            fuss.append("⚠️ Fehlt: " + ", ".join(fehlend))
        if DASHBOARD_URL:
            fuss.append(f"📊 {DASHBOARD_URL}")
        if fuss:
            teile.append("\n".join(fuss))
        return "\n\n".join(teile)

    for stufe in ({}, {"max_termine": 4}, {"wirkung_an": False, "kuerze": 1},
                  {"wirkung_an": False, "max_termine": 4, "kurz": True, "kuerze": 1},
                  {"wirkung_an": False, "max_termine": 3, "kurz": True, "kuerze": 2}):
        text = zusammensetzen(**stufe)
        if len(text) <= MAX_ZEICHEN:
            return text
    return text[:MAX_ZEICHEN - 1] + "…"


def main(senden: bool = True, ausgeben: bool = False, erneut: bool = False) -> int:
    con = db()
    jetzt = jetzt_berlin()
    heute = fx_tag(jetzt).isoformat()
    fehlend: list[str] = []
    schon = con.execute("SELECT sent FROM reports WHERE date=?", (heute,)).fetchone()
    if senden and schon and schon["sent"] and not erneut:
        log.info("Report für %s wurde schon gesendet – übersprungen (mit --erneut erzwingen)", heute)
        return 0
    log.info("Report für %s", heute)

    # 1) Daten
    if not termine_aktualisieren(con):
        fehlend.append("Kalender")
    for f in news_aktualisieren(con):
        fehlend.append(f"News {f}")
    try:
        klassifizieren(con)
    except Exception as e:  # noqa: BLE001
        log.warning("News-Einordnung: %s", e)
    leer = con.execute("SELECT COUNT(*) FROM yields").fetchone()[0] < 20
    for f in renditen_aktualisieren(con, mit_historie=leer):
        fehlend.append(f"Renditen {f}")
    renditen = {}
    for s, name in SERIEN.items():
        d, wert, delta = letzte_rendite(con, s)
        aktuell = d is not None and d >= (jetzt.date() - timedelta(days=6)).isoformat()
        renditen[s] = {"name": name, "datum": d, "wert": wert, "delta_bp": delta, "aktuell": aktuell}
        if s in KERN and not aktuell and not any(name in f or "Renditen" in f for f in fehlend):
            fehlend.append(name)
    try:
        markt_fehler = markt_aktualisieren(con)
    except Exception as e:  # noqa: BLE001
        log.warning("Markt: %s", e)
        markt_fehler = ["VIX", "Nikkei 225", "Brent"]
    if markt_fehler:
        fehlend.append("Markt: " + ", ".join(markt_fehler))

    kurse = {}
    try:
        fx_tage_aktualisieren(con, tage=70)
        kurse = px_src.aktuelle_kurse(["gbpjpy", "usdjpy", "gbpusd"])
    except Exception as e:  # noqa: BLE001
        log.warning("Kurse: %s", e)
    if "gbpjpy" not in kurse:
        fehlend.append("Kurse (Tiingo)")

    vt = {p: vortag(con, p, heute) for p in ("gbpjpy", "usdjpy", "gbpusd")}
    tage = tage_bis(con, "gbpjpy", heute)
    kurs = kurse.get("gbpjpy")
    prev = vt["gbpjpy"]["close"] if vt["gbpjpy"] else None
    aenderung = (kurs / prev - 1) * 100 if kurs and prev else None
    atr = forecast.atr14(tage)
    trend_wert = forecast.trend(tage)
    vortag_dict = {"h": vt["gbpjpy"]["high"], "l": vt["gbpjpy"]["low"]} if vt["gbpjpy"] else None
    prog = forecast.prognose(kurs, atr, trend_wert, vortag_dict) if kurs and atr else None
    termine = termine_heute(con, jetzt)
    news = news_24h(con)
    try:
        vorschau = setup_mod.vorschau_text(con, jetzt)
    except Exception as e:  # noqa: BLE001
        log.warning("Setup-Vorschau: %s", e)
        vorschau = None
    try:
        tb = treiber.berechnen(con, jetzt, kurse=kurse, setup_trend=trend_wert or None)
    except Exception as e:  # noqa: BLE001
        log.warning("Treiber: %s", e)
        tb = None

    # 2) Claude: Einordnung der Nachrichtenlage
    roh = {
        "datum": heute, "uhrzeit_berlin": jetzt.strftime("%H:%M"),
        "kurse": {PAAR_NAME[p]: {"aktuell": kurse.get(p), "vortagesschluss": vt[p]["close"] if vt[p] else None,
                                 "vortag_hoch": vt[p]["high"] if vt[p] else None, "vortag_tief": vt[p]["low"] if vt[p] else None,
                                 "veraenderung_pct": round((kurse[p] / vt[p]["close"] - 1) * 100, 3)
                                 if kurse.get(p) and vt[p] else None} for p in vt},
        "renditen_prozent": {v["name"]: {"wert": v["wert"], "veraenderung_bp": v["delta_bp"], "stand": v["datum"]}
                             for v in renditen.values()},
        "termine_heute_berlin": [{"id": t["id"], "zeit": datetime.fromisoformat(t["ts"]).astimezone(TZ_BERLIN).strftime("%H:%M"),
                                  "waehrung": t["currency"], "impact": t["impact"], "titel": t["title"],
                                  "forecast": t["forecast"], "previous": t["previous"]} for t in termine],
        "news_letzte_24h": [{"quelle": n["source"], "zeit": n["ts"], "titel": n["title"],
                             "einordnung": n.get("note"), "wirkung": n.get("effect")} for n in news],
        "mechanik": {"trendregel": forecast.TREND_TEXT[trend_wert], "atr_pips": prog and prog["atr_pips"],
                     "spanne_schluss_68pct": prog and [prog["range_low"], prog["range_high"]],
                     "setup_vorschau": vorschau},
        "treiber": None if not tb else {
            "score_minus100_bis_plus100": tb["score"], "empfehlung_mechanisch": tb["empfehlung"]["titel"],
            "werte": [{"treiber": i["name"], "wert": i["wert_text"], "veraenderung": i["aenderung_text"],
                       "wirkung_auf_gbpjpy": i["wirkung"], "einordnung": i["text"]} for i in tb["items"]],
        },
        "fehlende_quellen": fehlend,
    }
    texte, llm_ok = {}, 0
    try:
        texte = llm.report_texte(roh)
        llm_ok = 1
    except Exception as e:  # noqa: BLE001
        log.error("Claude: %s", e)
        fehlend.append("Claude-Analyse")
        texte = {"lage_gbp": "Keine Einordnung verfügbar (Claude nicht erreichbar).", "lage_jpy": "–",
                 "termin_wirkungen": [], "news_score": 0, "news_grund": "ohne Analyse keine Einordnung",
                 "news_grund_lang": "Die Analyse durch Claude war heute nicht verfügbar.", "risiken": "–"}
    news_score = int(texte.get("news_score") or 0)

    # 3) Nachricht
    text = nachricht_bauen(jetzt=jetzt, kurs=kurs, aenderung=aenderung, usdjpy=kurse.get("usdjpy"),
                           uk10=renditen["uk10"]["wert"], jp10=renditen["jp10"]["wert"], texte=texte, termine=termine,
                           prog=prog, trend_wert=trend_wert, vorschau=vorschau, quoten=quoten_30(con), fehlend=fehlend,
                           treiber_daten=tb)
    for w in texte.get("termin_wirkungen", []):
        con.execute("UPDATE calendar SET effect=? WHERE id=?", (w.get("wirkung"), w.get("id")))

    g = (lambda k: prog[k] if prog else None)
    con.execute("""INSERT OR REPLACE INTO reports(date, created, price, prev_close, sigma_pips, range_low, range_high,
                   target, bias, bias_score, bias_reason, bias_reason_long, level_up, level_down, message, data_json,
                   missing, llm_ok, sent, news_score, news_reason, exp_high, exp_low, atr_pips, treiber_score, treiber_ampel)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)""",
                (heute, jetzt.isoformat(), kurs, prev, g("atr_pips"), g("range_low"), g("range_high"), g("target"),
                 forecast.TREND_TEXT[trend_wert], trend_wert, texte.get("news_grund"), texte.get("news_grund_lang"),
                 g("level_up"), g("level_down"), text,
                 json.dumps({"roh": roh, "texte": texte}, ensure_ascii=False), json.dumps(fehlend, ensure_ascii=False),
                 llm_ok, news_score, texte.get("news_grund"), g("exp_high"), g("exp_low"), g("atr_pips"),
                 tb["score"] if tb else None, tb["empfehlung"]["titel"] if tb else None))
    con.commit()
    if tb:
        treiber.merken(con, tb)

    if ausgeben:
        print(text)
        print(f"\n[{len(text)} Zeichen]")
    if senden:
        ok = whatsapp(text, log)
        con.execute("UPDATE reports SET sent=? WHERE date=?", (int(ok), heute))
        con.commit()
    export.alles(con)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--nicht-senden", action="store_true")
    ap.add_argument("--ausgeben", action="store_true")
    ap.add_argument("--erneut", action="store_true", help="auch senden, wenn heute schon gesendet")
    a = ap.parse_args()
    raise SystemExit(main(senden=not a.nicht_senden, ausgeben=a.ausgeben, erneut=a.erneut))
