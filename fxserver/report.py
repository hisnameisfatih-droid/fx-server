"""Morgenreport GBP/JPY (läuft Mo–Fr um 06:45 Uhr Berlin).

Aufruf: python -m fxserver.report [--nicht-senden] [--ausgeben]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from . import export, forecast, llm
from .common import (DASHBOARD_URL, PAAR_NAME, TZ_BERLIN, db, de_prozent, de_zahl, fx_tag, get_logger,
                     jetzt_berlin, whatsapp)
from .daten import (fx_tage_aktualisieren, letzte_rendite, news_aktualisieren, renditen_aktualisieren,
                    termine_aktualisieren)
from .news_klassifikation import klassifizieren
from .sources import prices as px_src
from .sources.yields import SERIEN

log = get_logger("report")
FLAGGE = {"GBP": "🇬🇧", "JPY": "🇯🇵", "USD": "🇺🇸"}
WOCHENTAG = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MAX_ZEICHEN = 1500


def vortag(con, paar: str, heute: str):
    return con.execute("SELECT * FROM fxdaily WHERE pair=? AND date<? ORDER BY date DESC LIMIT 1",
                       (paar, heute)).fetchone()


def tage_bis(con, paar: str, heute: str, n: int = 25) -> list[dict]:
    rows = con.execute("SELECT * FROM fxdaily WHERE pair=? AND date<? ORDER BY date DESC LIMIT ?",
                       (paar, heute, n)).fetchall()
    return [{"date": r["date"], "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"]} for r in reversed(rows)]


def quoten_30(con) -> dict:
    rows = con.execute("SELECT * FROM evaluations ORDER BY date DESC LIMIT 30").fetchall()
    return forecast.quoten([dict(r) for r in rows])


def termine_heute(con, jetzt: datetime) -> list[dict]:
    start = jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
    ende = start + timedelta(days=1)
    rows = con.execute("SELECT * FROM calendar WHERE ts>=? AND ts<? ORDER BY ts", (start.isoformat(), ende.isoformat()))
    return [dict(r) for r in rows]


def news_24h(con, n: int = 40) -> list[dict]:
    grenze = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = con.execute("""SELECT * FROM news WHERE ts>=? ORDER BY COALESCE(relevance, 5) DESC, ts DESC LIMIT ?""",
                       (grenze, n)).fetchall()
    return [dict(r) for r in rows]


def nachricht_bauen(*, jetzt, kurs, aenderung, usdjpy, uk10, jp10, texte, termine, prog, bias_text,
                    quoten, fehlend) -> str:
    def kopf():
        return "\n".join([
            f"GBP/JPY — {WOCHENTAG[jetzt.weekday()]}, {jetzt:%d.%m.%Y}",
            f"Kurs: {de_zahl(kurs)} ({de_prozent(aenderung)} seit gestern)" if kurs else "Kurs: n/v",
            f"USD/JPY: {de_zahl(usdjpy)} | Gilt 10y: {de_zahl(uk10, 2)}% | JGB 10y: {de_zahl(jp10, 2)}%",
        ])

    wirkung = {w["id"]: w["wirkung"] for w in texte.get("termin_wirkungen", []) if w.get("id")}

    def heute_block(mit_wirkung: bool, max_n: int):
        if not termine:
            return "HEUTE\nKeine wichtigen Termine (GBP/JPY/USD)."
        zeilen = ["HEUTE"]
        for t in termine[:max_n]:
            zeit = datetime.fromisoformat(t["ts"]).astimezone(TZ_BERLIN)
            fp = []
            if t.get("forecast"):
                fp.append(f"F: {t['forecast']}")
            if t.get("previous"):
                fp.append(f"P: {t['previous']}")
            if t.get("actual"):
                fp.append(f"A: {t['actual']}")
            rot = "🔴" if t["impact"] == "high" else "🟠"
            z = f"{zeit:%H:%M} {rot}{FLAGGE.get(t['currency'], '')} {t['title']}" + (f" ({' | '.join(fp)})" if fp else "")
            if mit_wirkung and wirkung.get(t["id"]):
                z += f" – {wirkung[t['id']]}"
            zeilen.append(z)
        if len(termine) > max_n:
            zeilen.append(f"… und {len(termine) - max_n} weitere im Dashboard")
        return "\n".join(zeilen)

    def prog_block():
        if not prog:
            return "PROGNOSE\nNicht berechenbar (Kursdaten fehlen)."
        z = ["PROGNOSE", f"Spanne: {de_zahl(prog['range_low'])} – {de_zahl(prog['range_high'])}",
             f"Schluss ca.: {de_zahl(prog['target'])}"]
        if prog.get("level_up"):
            z.append(f"Kippt nach oben über: {de_zahl(prog['level_up'])} / nach unten unter: {de_zahl(prog['level_down'])}")
        return "\n".join(z)

    def quote_zeile():
        if not quoten["n"]:
            return "Trefferquote 30T: noch keine Auswertung"
        n = f" (n={quoten['n']})" if quoten["n"] < 30 else ""
        return (f"Trefferquote 30T: Spanne {quoten['range_pct']}% | Richtung {quoten['direction_pct']}% | "
                f"Ø Abweichung {de_zahl(quoten['avg_dev'], 0)} Pips{n}")

    def zusammensetzen(lage_kurz=False, wirkung_an=True, max_termine=6, risiken_kurz=False):
        lage_gbp, lage_jpy = texte.get("lage_gbp", ""), texte.get("lage_jpy", "")
        if lage_kurz:
            lage_gbp, lage_jpy = lage_gbp.split(". ")[0].rstrip(".") + ".", lage_jpy.split(". ")[0].rstrip(".") + "."
        risiken = texte.get("risiken", "")
        if risiken_kurz:
            risiken = risiken.split(". ")[0].rstrip(".") + "."
        teile = [kopf(), f"LAGE\nGBP: {lage_gbp}\nJPY: {lage_jpy}", heute_block(wirkung_an, max_termine),
                 prog_block(), f"BIAS\n{bias_text} — {texte.get('bias_grund', '')}", f"RISIKEN\n{risiken}",
                 quote_zeile()]
        fuss = []
        if fehlend:
            fuss.append("⚠️ Fehlende Quellen: " + ", ".join(fehlend))
        if DASHBOARD_URL:
            fuss.append(f"📊 {DASHBOARD_URL}")
        if fuss:
            teile.append("\n".join(fuss))
        return "\n\n".join(teile)

    for stufe in ({}, {"wirkung_an": True, "max_termine": 4}, {"wirkung_an": False, "max_termine": 6},
                  {"wirkung_an": False, "max_termine": 4, "risiken_kurz": True},
                  {"wirkung_an": False, "max_termine": 3, "risiken_kurz": True, "lage_kurz": True}):
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

    # 1) Daten holen
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
        if not aktuell and f"Renditen {name}" not in fehlend:
            fehlend.append(name)

    kurse = {}
    try:
        fx_tage_aktualisieren(con)
        kurse = px_src.aktuelle_kurse(["gbpjpy", "usdjpy", "gbpusd"])
    except Exception as e:  # noqa: BLE001
        log.warning("Kurse: %s", e)
    if "gbpjpy" not in kurse:
        fehlend.append("Kurse (Tiingo)")

    vt = {p: vortag(con, p, heute) for p in ("gbpjpy", "usdjpy", "gbpusd")}
    kurs = kurse.get("gbpjpy")
    prev = vt["gbpjpy"]["close"] if vt["gbpjpy"] else None
    aenderung = (kurs / prev - 1) * 100 if kurs and prev else None
    sigma = forecast.sigma_pips(tage_bis(con, "gbpjpy", heute))
    vortag_dict = {"h": vt["gbpjpy"]["high"], "l": vt["gbpjpy"]["low"]} if vt["gbpjpy"] else None
    termine = [t for t in termine_heute(con, jetzt)]
    news = news_24h(con)

    # 2) Claude
    roh = {
        "datum": heute, "uhrzeit_berlin": jetzt.strftime("%H:%M"),
        "kurse": {PAAR_NAME[p]: {"aktuell": kurse.get(p), "vortagesschluss": vt[p]["close"] if vt[p] else None,
                                 "vortag_hoch": vt[p]["high"] if vt[p] else None,
                                 "vortag_tief": vt[p]["low"] if vt[p] else None,
                                 "veraenderung_pct": round((kurse[p] / vt[p]["close"] - 1) * 100, 3)
                                 if kurse.get(p) and vt[p] else None} for p in vt},
        "renditen_prozent": {v["name"]: {"wert": v["wert"], "veraenderung_bp": v["delta_bp"], "stand": v["datum"]}
                             for v in renditen.values()},
        "termine_heute_berlin": [{"id": t["id"], "zeit": datetime.fromisoformat(t["ts"]).astimezone(TZ_BERLIN).strftime("%H:%M"),
                                  "waehrung": t["currency"], "impact": t["impact"], "titel": t["title"],
                                  "forecast": t["forecast"], "previous": t["previous"]} for t in termine],
        "news_letzte_24h": [{"quelle": n["source"], "zeit": n["ts"], "titel": n["title"],
                             "einordnung": n.get("note"), "wirkung": n.get("effect")} for n in news],
        "statistik": {"tagesvolatilitaet_sigma_pips": round(sigma, 1) if sigma else None,
                      "hinweis": "Spanne = Kurs ± σ·0,85; Zielkurs verschiebt sich je Bias-Stufe um 0,2σ."},
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
                 "termin_wirkungen": [], "bias_score": 0, "bias_grund": "ohne Analyse kein Bias",
                 "bias_grund_lang": "Die Analyse durch Claude war heute nicht verfügbar.",
                 "risiken": "–"}
    score = int(texte.get("bias_score") or 0)
    bias_text = forecast.BIAS_STUFEN.get(score, "neutral")

    # 3) Prognose + Nachricht
    prog = forecast.prognose(kurs, sigma, score, vortag_dict) if kurs and sigma else None
    usd = kurse.get("usdjpy")
    text = nachricht_bauen(jetzt=jetzt, kurs=kurs, aenderung=aenderung, usdjpy=usd,
                           uk10=renditen["uk10"]["wert"], jp10=renditen["jp10"]["wert"], texte=texte,
                           termine=termine, prog=prog, bias_text=bias_text, quoten=quoten_30(con), fehlend=fehlend)

    # Wirkungen der Termine fürs Dashboard merken
    for w in texte.get("termin_wirkungen", []):
        con.execute("UPDATE calendar SET effect=? WHERE id=?", (w.get("wirkung"), w.get("id")))

    con.execute("""INSERT OR REPLACE INTO reports(date, created, price, prev_close, sigma_pips, range_low, range_high,
                   target, bias, bias_score, bias_reason, bias_reason_long, level_up, level_down, message, data_json,
                   missing, llm_ok, sent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (heute, jetzt.isoformat(), kurs, prev, sigma, prog and prog["range_low"], prog and prog["range_high"],
                 prog and prog["target"], bias_text, score, texte.get("bias_grund"), texte.get("bias_grund_lang"),
                 prog and prog["level_up"], prog and prog["level_down"], text,
                 json.dumps({"roh": roh, "texte": texte}, ensure_ascii=False), json.dumps(fehlend, ensure_ascii=False),
                 llm_ok))
    con.commit()

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
