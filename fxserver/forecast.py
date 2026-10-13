"""Tagesprognose – empirisch kalibriert an GBP/JPY 2012–2022 (siehe backtest_historie.json).

- ATR = Ø True Range der letzten 14 FX-Tage (Tagesende 17:00 New York)
- Spanne für den Tagesschluss: Kurs um 06:45 ± 0,52 ATR  -> enthielt den Schluss in 68 % (2012–18) bzw. 67 % (2019–22)
- Erwartetes Hoch/Tief ab 06:45: Kurs ± 0,34 ATR          -> wird an etwa jedem zweiten Tag erreicht
- Trend (Regel): Vortagesschluss über/unter EMA20 UND 5-Tage-Veränderung in dieselbe Richtung
  -> Tagesrichtung historisch nur zu ~51 % getroffen: Orientierung, keine Vorhersage
- Kipp-Marken: Vortageshoch/-tief
"""
from __future__ import annotations

SPANNE_ATR = 0.52
HOCH_TIEF_ATR = 0.34
ZIEL_ATR = 0.02        # minimale Verschiebung des Zielkurses in Trendrichtung (historischer Mittelwert)
TREND_TEXT = {1: "Aufwärtstrend", -1: "Abwärtstrend", 0: "kein klarer Trend"}
NEWS_TEXT = {-2: "klar JPY-stärkend / GBP-schwächend", -1: "leicht belastend für GBP/JPY", 0: "neutral",
             1: "leicht stützend für GBP/JPY", 2: "klar stützend für GBP/JPY"}


def atr14(tage: list[dict]) -> float | None:
    """tage: chronologisch [{o,h,l,c}] (nur abgeschlossene FX-Tage)."""
    if len(tage) < 15:
        return None
    trs = []
    for j in range(len(tage) - 14, len(tage)):
        h, l, pc = tage[j]["h"], tage[j]["l"], tage[j - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def ema20(tage: list[dict]) -> float | None:
    if len(tage) < 21:
        return None
    ema = tage[0]["c"]
    for t in tage[1:]:
        ema += (2 / 21) * (t["c"] - ema)
    return ema


def trend(tage: list[dict]) -> int:
    e = ema20(tage)
    if e is None or len(tage) < 6:
        return 0
    c, c5 = tage[-1]["c"], tage[-6]["c"]
    if c > e and c > c5:
        return 1
    if c < e and c < c5:
        return -1
    return 0


def prognose(kurs: float, atr: float, trend_wert: int, vortag: dict | None) -> dict:
    return {
        "range_low": round(kurs - SPANNE_ATR * atr, 3),
        "range_high": round(kurs + SPANNE_ATR * atr, 3),
        "target": round(kurs + trend_wert * ZIEL_ATR * atr, 3),
        "exp_high": round(kurs + HOCH_TIEF_ATR * atr, 3),
        "exp_low": round(kurs - HOCH_TIEF_ATR * atr, 3),
        "level_up": round(vortag["h"], 3) if vortag else None,
        "level_down": round(vortag["l"], 3) if vortag else None,
        "atr_pips": round(atr / 0.01),
    }


def bewerte(report: dict, tag: dict, prev_close: float, atr_pips: float | None, pip: float = 0.01,
            vortag_change: float | None = None) -> dict:
    """Vergleicht Prognose mit dem tatsächlichen FX-Tag.
    Richtung wird an der Veränderung ab 06:45 gemessen (das ist, was um 06:45 noch vorherzusagen war).
    Neutral (Trend 0) gilt als Treffer, wenn sich der Kurs ab 06:45 um weniger als 0,15 ATR bewegt.
    Vergleichsmodell („naiv“): Zielkurs = Kurs um 06:45, Richtung = wie am Vortag."""
    close = tag["c"]
    ab_report = (close - report["price"]) / pip
    change = (close - prev_close) / pip
    in_range = report["range_low"] <= close <= report["range_high"]
    abweichung = abs(close - report["target"]) / pip
    naive_abw = abs(close - report["price"]) / pip
    band = 0.15 * (atr_pips or 150)

    def treffer(score):
        if score is None:
            return None
        if score == 0:
            return int(abs(ab_report) <= band)
        return int((ab_report > 0) == (score > 0) and ab_report != 0)

    return {
        "close": close, "open": tag["o"], "high": tag["h"], "low": tag["l"], "prev_close": prev_close,
        "change_pips": round(change, 1), "in_range": int(in_range), "deviation_pips": round(abweichung, 1),
        "direction_hit": treffer(report.get("bias_score") or 0),
        "news_hit": treffer(report.get("news_score")) if report.get("news_score") not in (None, 0) else None,
        "naive_deviation_pips": round(naive_abw, 1),
        "naive_direction_hit": (int((ab_report > 0) == (vortag_change > 0) and ab_report != 0)
                                if vortag_change not in (None, 0) else None),
    }


def quoten(evals: list[dict]) -> dict:
    n = len(evals)
    if not n:
        return {"n": 0, "range_pct": None, "direction_pct": None, "avg_dev": None, "naive_avg_dev": None,
                "naive_direction_pct": None, "news_pct": None}

    def pct(feld):
        werte = [e[feld] for e in evals if e.get(feld) is not None]
        return round(100 * sum(werte) / len(werte)) if werte else None

    def avg(feld):
        werte = [e[feld] for e in evals if e.get(feld) is not None]
        return round(sum(werte) / len(werte), 1) if werte else None

    return {"n": n, "range_pct": pct("in_range"), "direction_pct": pct("direction_hit"),
            "avg_dev": avg("deviation_pips"), "naive_avg_dev": avg("naive_deviation_pips"),
            "naive_direction_pct": pct("naive_direction_hit"), "news_pct": pct("news_hit")}
