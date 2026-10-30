"""Schreibt die JSON-Dateien fürs Dashboard (dashboard.json, archive.json)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pathlib import Path

from . import forecast
from . import amd as amd_mod
from . import setup as setup_mod
from . import tage as tage_mod
from . import treiber as treiber_mod
from .common import PAARE, fx_tag, fx_tag_ende, fx_tag_start, get_logger, jetzt_berlin, schreibe_json
from .sources.yields import SERIEN

log = get_logger("export")


def _eval_dict(e) -> dict | None:
    if not e:
        return None
    return {k: e[k] for k in ("close", "high", "low", "change_pips", "in_range", "deviation_pips", "direction_hit",
                              "naive_deviation_pips", "naive_direction_hit")}


def alles(con, treiber_daten: dict | None = None) -> None:
    jetzt = jetzt_berlin()
    heute = fx_tag(jetzt).isoformat()
    if treiber_daten is None:
        try:
            treiber_daten = treiber_mod.berechnen(con, jetzt)
        except Exception as e:  # noqa: BLE001
            log.warning("Treiber: %s", e)

    # --- Reports + Auswertungen ---
    reps = [dict(r) for r in con.execute(
        "SELECT r.*, e.close, e.high, e.low, e.change_pips, e.in_range, e.deviation_pips, e.direction_hit, "
        "e.naive_deviation_pips, e.naive_direction_hit FROM reports r LEFT JOIN evaluations e ON e.date=r.date "
        "ORDER BY r.date DESC")]
    archiv = []
    for r in reps:
        archiv.append({
            "date": r["date"], "price": r["price"], "prev_close": r["prev_close"], "range": [r["range_low"], r["range_high"]],
            "target": r["target"], "bias": r["bias"], "score": r["bias_score"], "reason": r["bias_reason"],
            "levels": [r["level_up"], r["level_down"]], "message": r["message"],
            "missing": json.loads(r["missing"] or "[]"),
            "eval": _eval_dict(r) if r.get("close") is not None else None,
        })
    schreibe_json("archive.json", {"generated": jetzt.isoformat(), "reports": archiv})

    evals = [dict(e) for e in con.execute("SELECT * FROM evaluations ORDER BY date DESC")]
    letzte10 = []
    for r in archiv:
        if r["eval"]:
            letzte10.append({"date": r["date"], "range": r["range"], "bias": r["bias"], "score": r["score"],
                             "target": r["target"], **r["eval"]})
        if len(letzte10) >= 10:
            break

    heute_rep = next((r for r in reps if r["date"] == heute), None)
    texte = json.loads(heute_rep["data_json"]).get("texte", {}) if heute_rep and heute_rep.get("data_json") else {}

    # --- Termine (heute + morgen, Berliner Zeit) ---
    start = jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
    termine = [dict(t) for t in con.execute("SELECT * FROM calendar WHERE ts>=? AND ts<? ORDER BY ts",
                                             (start.isoformat(), (start + timedelta(days=2)).isoformat()))]
    studie_termine = {t["name"]: t for t in treiber_mod.studie().get("termine", [])}
    for t in termine:
        t.pop("reminded", None)
        typ = treiber_mod.termin_typ(t)
        if typ:   # gemessene Wirkung solcher Termine auf GBP/JPY (Studie 2023–2026)
            s = studie_termine.get(typ, {})
            t.update(typ=typ, faktor=s.get("faktor"), avg_abs=s.get("avg_abs"), anteil_1pct=s.get("anteil_ueber_1pct"))

    # --- Renditen (30 Tage) ---
    ab = (jetzt.date() - timedelta(days=45)).isoformat()
    serien = {}
    for s, name in SERIEN.items():
        rows = con.execute("SELECT date, value FROM yields WHERE series=? AND date>=? ORDER BY date", (s, ab)).fetchall()
        punkte = [[r["date"], r["value"]] for r in rows][-30:]
        wert = punkte[-1][1] if punkte else None
        delta = round((punkte[-1][1] - punkte[-2][1]) * 100, 1) if len(punkte) > 1 else None
        serien[s] = {"name": name, "points": punkte, "value": wert, "delta_bp": delta,
                     "date": punkte[-1][0] if punkte else None}
    uk = dict(serien["uk10"]["points"])
    spread = [[d, round(uk[d] - v, 3)] for d, v in serien["jp10"]["points"] if d in uk]

    # --- News (Top 10 der letzten 24h nach Relevanz, dann chronologisch) ---
    grenze = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    news = [dict(n) for n in con.execute(
        "SELECT ts, source, title, url, relevance, currencies, effect, note FROM news WHERE ts>=? "
        "AND (classified=0 OR relevance>=3) ORDER BY COALESCE(relevance, 4) DESC, ts DESC LIMIT 10", (grenze,))]
    for n in news:
        n["currencies"] = json.loads(n["currencies"]) if n.get("currencies") else []
    news.sort(key=lambda n: n["ts"], reverse=True)

    # --- Vortagesschlüsse für die Live-Kacheln ---
    vortag = {}
    for p in PAARE:
        row = con.execute("SELECT date, close, high, low FROM fxdaily WHERE pair=? AND date<? ORDER BY date DESC LIMIT 1",
                          (p, heute)).fetchone()
        if row:
            vortag[p] = dict(row)

    # --- Tages-Setup ---
    con.executescript(setup_mod.SCHEMA)
    setup_heute = None
    row = con.execute("SELECT * FROM setups WHERE date=?", (heute,)).fetchone()
    if row:
        setup_heute = {**json.loads(row["data_json"]), "state": row["state"],
                       "trade": json.loads(row["trade_json"]) if row["trade_json"] else None,
                       "result": json.loads(row["result_json"]) if row["result_json"] else None}
    setup_liste = []
    for r in con.execute("SELECT * FROM setups WHERE date<? ORDER BY date DESC LIMIT 15", (heute,)):
        d = json.loads(r["data_json"])
        res = json.loads(r["result_json"]) if r["result_json"] else None
        setup_liste.append({"date": r["date"], "status": r["status"], "grund": d.get("grund"), "trend": d.get("trend"),
                            "state": r["state"], "result": res})
    bilanz = setup_mod.live_bilanz(con)
    try:
        historie = json.loads((Path(__file__).parent / "backtest_historie.json").read_text())
    except (OSError, ValueError):
        historie = None

    bias_hist = [{"date": r["date"], "score": r["score"], "bias": r["bias"], "reason": r["reason"],
                  "hit": r["eval"]["direction_hit"] if r["eval"] else None} for r in archiv[:14]][::-1]
    treiber_hist = [{"date": r["date"], "score": r["treiber_score"], "ampel": r["treiber_ampel"],
                     "hit": r.get("treiber_hit"), "hit5": r.get("treiber_hit5"), "change_pips": r.get("change_pips")}
                    for r in [dict(x) for x in con.execute(
                        "SELECT r.date, r.treiber_score, r.treiber_ampel, e.treiber_hit, e.treiber_hit5, e.change_pips "
                        "FROM reports r LEFT JOIN evaluations e ON e.date=r.date WHERE r.treiber_score IS NOT NULL "
                        "ORDER BY r.date DESC LIMIT 15")]]
    heute_treiber = None
    if heute_rep and heute_rep.get("treiber_score") is not None:
        heute_treiber = {"score": heute_rep["treiber_score"], "ampel": heute_rep["treiber_ampel"]}

    schreibe_json("dashboard.json", {
        "generated": jetzt.isoformat(),
        "fx_day": heute,
        "fx_window": [fx_tag_start(fx_tag(jetzt)).timestamp(), fx_tag_ende(fx_tag(jetzt)).timestamp()],
        "report": None if not heute_rep else {
            "date": heute_rep["date"], "created": heute_rep["created"], "price": heute_rep["price"],
            "prev_close": heute_rep["prev_close"], "sigma": heute_rep["sigma_pips"],
            "range": [heute_rep["range_low"], heute_rep["range_high"]], "target": heute_rep["target"],
            "levels": [heute_rep["level_up"], heute_rep["level_down"]], "bias": heute_rep["bias"],
            "score": heute_rep["bias_score"], "reason": heute_rep["bias_reason"],
            "reason_long": heute_rep["bias_reason_long"], "risks": texte.get("risiken"),
            "lage_gbp": texte.get("lage_gbp"), "lage_jpy": texte.get("lage_jpy"),
            "missing": json.loads(heute_rep["missing"] or "[]"), "message": heute_rep["message"],
            "exp": [heute_rep.get("exp_high"), heute_rep.get("exp_low")], "atr_pips": heute_rep.get("atr_pips"),
            "news_score": heute_rep.get("news_score"), "news_reason": heute_rep.get("news_reason"),
            "vorschau": (json.loads(heute_rep["data_json"]).get("roh", {}).get("mechanik", {}) or {}).get("setup_vorschau")
            if heute_rep.get("data_json") else None,
            "trend": heute_rep.get("bias_score"),
            "treiber": heute_treiber,
        },
        "treiber": treiber_daten,
        "amd": amd_mod.export_daten(con, jetzt),
        "treiber_verlauf": treiber_hist,
        "treiber_studie": treiber_mod.studie() or None,
        "setup": setup_heute,
        "setup_bilanz": bilanz,
        "setup_liste": setup_liste,
        "setup_historie": historie,
        "bias_history": bias_hist,
        "calendar": termine,
        "yields": serien, "spread": spread,
        "news": news,
        "stats": {"q30": forecast.quoten(evals[:30]), "last10": letzte10},
        "prev_close": vortag,
        "model": {"spanne_atr": forecast.SPANNE_ATR, "hoch_tief_atr": forecast.HOCH_TIEF_ATR},
    })
    try:   # Vortage (Kerzen + Setups + Ergebnisse): einmal pro FX-Tag
        tage_mod.bei_bedarf(con, jetzt)
    except Exception as e:  # noqa: BLE001
        log.warning("Vortage: %s", e)
