"""Backtest der Tages-Setups (fxserver/strategien.py) auf 15-Minuten-Kerzen.

Auf dem Server: python -m fxserver.backtest   (holt die letzten Jahre von Tiingo, schreibt backtest.json)
Zum Entwickeln:  python -m fxserver.backtest --csv datei.csv --csv-offset-std -1
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import OrderedDict, defaultdict
from datetime import datetime, time, timedelta, timezone

from .strategien import PIP, STRATEGIEN, Kontext, simuliere

KOSTEN_PIPS = 3.0   # Spread + Slippage pro Trade (Retail-Broker, GBP/JPY)

# ------------------------------------------------------------
# Parameter-Raster (bewusst klein gehalten – weniger Überanpassung)
# ------------------------------------------------------------
def raster() -> list[dict]:
    r = []
    for rv in (time(1, 0), time(3, 0)):
        for eb in (time(12, 0), time(14, 0)):
            for sl in ("gegenseite", "mitte"):
                for tpr in (1.0, 1.5, 2.0):
                    for mx in (0.5, 0.8):
                        r.append({"strategie": "orb", "id": "orb_london", "name": "London-Ausbruch",
                                  "range_von": rv, "range_bis": time(9, 0), "entry_bis": eb, "schluss": time(21, 0),
                                  "sl": sl, "tp_r": tpr, "min_atr": 0.1, "max_atr": mx})
    for sl in ("gegenseite", "mitte"):
        for tpr in (1.0, 1.5, 2.0):
            for mx in (0.5, 0.8):
                r.append({"strategie": "orb", "id": "orb_ny", "name": "New-York-Ausbruch",
                          "range_von": time(9, 0), "range_bis": time(14, 30), "entry_bis": time(17, 0),
                          "schluss": time(22, 0), "sl": sl, "tp_r": tpr, "min_atr": 0.15, "max_atr": mx})
    for m in (0.4, 0.5, 0.6, 0.75):
        for sla in (0.2, 0.3, 0.4):
            for ta in (0.5, 1.0):
                r.append({"strategie": "reversion", "id": "reversion", "name": "Rücklauf an der Tagesspanne",
                          "m": m, "sl_atr": sla, "tp_anteil": ta, "start": time(9, 0), "entry_bis": time(18, 0),
                          "schluss": time(22, 0)})
    for st in (time(9, 0), time(14, 30)):
        for sla in (0.4, 0.6):
            for tpa in (0.6, 1.0, 1.5):
                r.append({"strategie": "trend", "id": "trend", "name": "Trendfolge",
                          "start": st, "sl_atr": sla, "tp_atr": tpa, "schluss": time(22, 0)})
    return r


def p_text(p: dict) -> str:
    teile = []
    for k, v in p.items():
        if k in ("strategie", "id", "name"):
            continue
        teile.append(f"{k}={v.strftime('%H:%M') if isinstance(v, time) else v}")
    return ", ".join(teile)


# ------------------------------------------------------------
# Daten -> FX-Tage mit Kontext
# ------------------------------------------------------------
def tage_bauen(kerzen: list[tuple]) -> "OrderedDict[str, list]":
    """kerzen: [(fx_datum, t_berlin, o, h, l, c)] -> {fx_datum: [(t, o, h, l, c)]}"""
    tage: OrderedDict[str, list] = OrderedDict()
    for d, t, o, h, l, c in kerzen:
        tage.setdefault(d, []).append((t, o, h, l, c))
    return tage


def kontexte(tage: "OrderedDict[str, list]"):
    """Liefert (datum, Kontext, kerzen) für jeden Tag mit ausreichend Vorgeschichte."""
    tageswerte = []
    for d, ks in tage.items():
        if len(ks) < 40:   # Feiertage / Rumpftage überspringen
            continue
        tageswerte.append((d, {"o": ks[0][1], "h": max(k[2] for k in ks), "l": min(k[3] for k in ks), "c": ks[-1][4]}, ks))
    ema = None
    for i, (d, tw, ks) in enumerate(tageswerte):
        if i >= 1:
            vt = tageswerte[i - 1][1]
            ema = vt["c"] if ema is None else ema + (2 / 21) * (vt["c"] - ema)
        if i < 21:
            continue
        trs = []
        for j in range(i - 14, i):
            h, l, pc = tageswerte[j][1]["h"], tageswerte[j][1]["l"], tageswerte[j - 1][1]["c"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = sum(trs) / len(trs)
        vortag = tageswerte[i - 1][1]
        trend5 = vortag["c"] - tageswerte[i - 6][1]["c"]
        yield d, Kontext(d, tw["o"], atr, vortag, ema, trend5, ks), ks


def lade_csv(pfad: str, offset_std: float, skala: float | None = None) -> list[tuple]:
    """CSV mit Date,open,high,low,close (Datenzeit). Berlin = Datenzeit + offset_std.
    FX-Tag = Datum der Datenzeit (wenn 00:00 Datenzeit = 17:00 New York)."""
    erg = []
    with open(pfad) as f:
        for row in csv.DictReader(f):
            t = datetime.fromisoformat(row["Date"])
            o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
            if skala is None:
                skala = 1000.0 if o > 10000 else 1.0
            erg.append((t.date().isoformat(), t + timedelta(hours=offset_std), o / skala, h / skala, l / skala, c / skala))
    return erg


# ------------------------------------------------------------
# Auswertung
# ------------------------------------------------------------
def kennzahlen(trades) -> dict:
    n = len(trades)
    if not n:
        return {"n": 0}
    pips = [t.pips for t in trades]
    gew = [p for p in pips if p > 0]
    verl = [p for p in pips if p <= 0]
    kurve, hoch, maxdd = 0.0, 0.0, 0.0
    for p in pips:
        kurve += p
        hoch = max(hoch, kurve)
        maxdd = max(maxdd, hoch - kurve)
    jahre = defaultdict(float)
    for t in trades:
        jahre[t.datum[:4]] += t.pips
    return {
        "n": n, "trefferquote": round(100 * len(gew) / n, 1),
        "avg_pips": round(sum(pips) / n, 2), "avg_r": round(sum(t.r for t in trades) / n, 3),
        "summe_pips": round(sum(pips)), "profit_faktor": round(sum(gew) / -sum(verl), 2) if verl and sum(verl) < 0 else None,
        "max_drawdown_pips": round(maxdd), "jahre": {j: round(v) for j, v in sorted(jahre.items())},
        "jahre_positiv_pct": round(100 * sum(1 for v in jahre.values() if v > 0) / len(jahre)),
        "tp_quote": round(100 * sum(1 for t in trades if t.grund == "tp") / n, 1),
        "sl_quote": round(100 * sum(1 for t in trades if t.grund == "sl") / n, 1),
        "stdev_pips": round(statistics.pstdev(pips), 1),
    }


def lauf(kerzen, params_liste, kosten=KOSTEN_PIPS):
    tage = tage_bauen(kerzen)
    ctxs = list(kontexte(tage))
    ergebnisse = []
    for p in params_liste:
        fn = STRATEGIEN[p["strategie"]]
        trades = []
        for d, ctx, ks in ctxs:
            setup = fn(ctx, p)
            if setup:
                tr = simuliere(setup, ks, kosten, d)
                if tr:
                    trades.append(tr)
        ergebnisse.append((p, trades))
    return ergebnisse, len(ctxs)


def teilen(trades, grenze: str):
    return [t for t in trades if t.datum < grenze], [t for t in trades if t.datum >= grenze]


# ------------------------------------------------------------
# Server: aktueller Zeitraum mit Tiingo-Daten (echter Prüfzeitraum, nach 2022)
# ------------------------------------------------------------
VERGLEICH = {
    "london_beide": {"trendfilter": False, "name": "London-Ausbruch (beide Richtungen)"},
}


def lade_tiingo(tage: int = 730, stueck: int = 45) -> list[tuple]:
    from .common import TZ_BERLIN, fx_tag
    from .sources import prices as px_src
    import time as _time
    ende = datetime.now(timezone.utc)
    start = ende - timedelta(days=tage)
    roh, t = {}, start
    while t < ende:
        bis = min(ende, t + timedelta(days=stueck))
        daten = px_src._get("/tiingo/fx/gbpjpy/prices", {"startDate": t.strftime("%Y-%m-%d"),
                                                        "endDate": bis.strftime("%Y-%m-%d"), "resampleFreq": "15min"})
        for c in daten or []:
            from .common import parse_iso
            ts = parse_iso(c["date"])
            roh[ts] = (float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]))
        t = bis
        _time.sleep(2)
    erg = []
    for ts in sorted(roh):
        if ts.astimezone(TZ_BERLIN).weekday() == 5:
            continue
        o, h, l, c = roh[ts]
        erg.append((fx_tag(ts + timedelta(seconds=1)).isoformat(), ts.astimezone(TZ_BERLIN).replace(tzinfo=None), o, h, l, c))
    return erg


def prognose_check(kerzen) -> dict:
    """Wie gut trifft die kalibrierte Spanne (±0,52 ATR ab 06:45) im aktuellen Zeitraum?"""
    from .forecast import HOCH_TIEF_ATR, SPANNE_ATR
    from .strategien import tm
    n = drin = hoch = tief = 0
    for d, ctx, ks in kontexte(tage_bauen(kerzen)):
        vor = [k for k in ks if tm(k[0]) < tm(time(6, 45))]
        nach = [k for k in ks if tm(k[0]) >= tm(time(6, 45))]
        if len(vor) < 20 or len(nach) < 40:
            continue
        c, close = vor[-1][4], ks[-1][4]
        n += 1
        drin += abs(close - c) <= SPANNE_ATR * ctx.atr
        hoch += max(k[2] for k in nach) >= c + HOCH_TIEF_ATR * ctx.atr
        tief += min(k[3] for k in nach) <= c - HOCH_TIEF_ATR * ctx.atr
    return {"tage": n, "spanne_abdeckung_pct": round(100 * drin / n, 1) if n else None,
            "hoch_erreicht_pct": round(100 * hoch / n, 1) if n else None,
            "tief_erreicht_pct": round(100 * tief / n, 1) if n else None}


def urteil(k: dict) -> tuple[str, str]:
    """Einfaches Urteil nach Kosten: lohnt / knapp / verliert / zu wenige Daten."""
    n, avg, pf = k.get("n", 0), k.get("avg_pips"), k.get("profit_faktor") or 0
    if n < 30:
        return "⏳", f"zu wenige Signale ({n}) für ein Urteil"
    if avg is None or avg <= 0:
        return "❌", "verliert nach Kosten – nicht danach handeln"
    if pf >= 1.15:
        return "✅", "positiv nach Kosten – vorsichtig nutzbar"
    return "➖", "knapp positiv – kein verlässlicher Vorteil"


def urteil_text(out: dict) -> str:
    z = [f"📊 Wochen-Backtest GBP/JPY ({out['von'][:10]} bis {out['bis'][:10]}, {out['kosten_pips']:.0f} Pips Kosten/Trade)"]
    for sid, d in out["setups"].items():
        k = d["kennzahlen"]
        zeichen, text = urteil(k)
        if k.get("n"):
            z.append(f"{zeichen} {d['name']}: {k['n']} Trades, {k['trefferquote']:.0f} % Treffer, "
                     f"Ø {k['avg_pips']:+.1f} P, Summe {k['summe_pips']:+d} P → {text}".replace(".", ","))
        else:
            z.append(f"{zeichen} {d['name']}: keine Trades")
    z.append("Details im Dashboard unter AMD-Muster und „Wie gut funktioniert das?“.")
    return "\n".join(z)


def server_lauf() -> int:
    from .common import get_logger, schreibe_json
    from .strategien import AMD_VARIANTEN, HAUPT_SETUP
    log = get_logger("backtest")
    try:
        kerzen = lade_tiingo()
    except Exception as e:  # noqa: BLE001
        log.error("Tiingo-Daten: %s", e)
        return 1
    if len(kerzen) < 2000:
        log.error("Zu wenige Kerzen (%d)", len(kerzen))
        return 1
    varianten = [HAUPT_SETUP] + [dict(HAUPT_SETUP, id=k, **v) for k, v in VERGLEICH.items()] + AMD_VARIANTEN
    erg, ntage = lauf(kerzen, varianten)
    out = {"erstellt": datetime.now(timezone.utc).isoformat(), "von": kerzen[0][1].isoformat(),
           "bis": kerzen[-1][1].isoformat(), "handelstage": ntage, "kosten_pips": KOSTEN_PIPS, "setups": {},
           "prognose": prognose_check(kerzen)}
    for p, trades in erg:
        out["setups"][p["id"]] = {"name": p["name"], "kennzahlen": kennzahlen(trades),
                                  "letzte": [{"date": t.datum, "richtung": t.richtung, "pips": t.pips, "grund": t.grund}
                                             for t in trades[-15:]]}
    schreibe_json("backtest.json", out)
    try:
        from .common import whatsapp
        whatsapp(urteil_text(out), log)
    except Exception as e:  # noqa: BLE001
        log.warning("Backtest-Nachricht: %s", e)
    log.info("Backtest %s bis %s: %s", out["von"][:10], out["bis"][:10],
             {k: v["kennzahlen"].get("avg_pips") for k, v in out["setups"].items()})
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiingo", action="store_true", help="Serverlauf mit Tiingo-Daten der letzten 2 Jahre")
    ap.add_argument("--csv")
    ap.add_argument("--csv-offset-std", type=float, default=-1.0)
    ap.add_argument("--grenze", default="2019-01-01", help="ab hier Out-of-Sample")
    ap.add_argument("--json")
    a = ap.parse_args()
    if a.tiingo:
        raise SystemExit(server_lauf())
    kerzen = lade_csv(a.csv, a.csv_offset_std)
    ergebnisse, ntage = lauf(kerzen, raster())
    print(f"{ntage} Handelstage, {kerzen[0][1]:%Y-%m-%d} bis {kerzen[-1][1]:%Y-%m-%d}, Kosten {KOSTEN_PIPS} Pips/Trade\n")
    tabelle = []
    for p, trades in ergebnisse:
        ins, oos = teilen(trades, a.grenze)
        tabelle.append({"id": p["id"], "name": p["name"], "params": p_text(p), "in": kennzahlen(ins), "oos": kennzahlen(oos)})
    if a.json:
        with open(a.json, "w") as f:
            json.dump(tabelle, f, ensure_ascii=False, indent=1)
    for sid in ("orb_london", "orb_ny", "reversion", "trend"):
        zeilen = [z for z in tabelle if z["id"] == sid and z["in"]["n"] >= 150]
        zeilen.sort(key=lambda z: z["in"]["avg_pips"], reverse=True)
        pos = sum(1 for z in tabelle if z["id"] == sid and z["in"].get("avg_pips", 0) > 0)
        alle = sum(1 for z in tabelle if z["id"] == sid)
        print(f"== {sid}: {pos}/{alle} Varianten in-sample positiv")
        for z in zeilen[:3]:
            i, o = z["in"], z["oos"]
            print(f"  IN  n={i['n']:4} Treffer {i['trefferquote']:5}% Ø {i['avg_pips']:+6.2f} P  PF {i['profit_faktor']}  DD {i['max_drawdown_pips']}  Jahre+ {i['jahre_positiv_pct']}%")
            print(f"  OUT n={o['n']:4} Treffer {o['trefferquote']:5}% Ø {o['avg_pips']:+6.2f} P  PF {o['profit_faktor']}  DD {o['max_drawdown_pips']}  Jahre {o['jahre']}")
            print(f"      {z['params']}")
        print()


if __name__ == "__main__":
    main()
