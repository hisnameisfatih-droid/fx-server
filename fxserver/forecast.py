"""Statistisches Prognose-Modell (bewusst einfach und nachvollziehbar).

- Spanne für den Tagesschluss: aktueller Kurs ± Z · σ · F
  σ = Standardabweichung der täglichen Schlusskurs-Veränderungen der letzten 20 FX-Tage
  F = Anteil der Tagesschwankung, der um 06:45 Uhr noch bevorsteht (London + New York)
- Zielkurs: aktueller Kurs + Bias-Wert (−2 … +2) · VERSATZ · σ
- Kipp-Marken: Hoch und Tief des Vortags (Orientierungsmarken, keine Handelssignale)
"""
from __future__ import annotations

import statistics

Z = 1.0          # ~68 % theoretische Abdeckung bei Normalverteilung
F = 0.85         # verbleibender Anteil der Tagesschwankung um 06:45
VERSATZ = 0.20   # Zielkurs-Verschiebung je Bias-Stufe (in σ)
TAGE = 20

BIAS_STUFEN = {
    -2: "stark bearisch", -1: "bearisch", 0: "neutral", 1: "bullisch", 2: "stark bullisch",
}


def sigma_pips(tage: list[dict], pip: float = 0.01) -> float | None:
    closes = [t["c"] for t in tage if t.get("c")]
    if len(closes) < 6:
        return None
    diffs = [(b - a) / pip for a, b in zip(closes[:-1], closes[1:])][-TAGE:]
    return statistics.pstdev(diffs) if len(diffs) >= 5 else None


def prognose(kurs: float, sigma: float, bias_score: int, vortag: dict | None, pip: float = 0.01) -> dict:
    halb = Z * sigma * F * pip
    ziel = kurs + max(-2, min(2, bias_score)) * VERSATZ * sigma * pip
    return {
        "range_low": round(kurs - halb, 3),
        "range_high": round(kurs + halb, 3),
        "target": round(ziel, 3),
        "level_up": round(vortag["h"], 3) if vortag else None,
        "level_down": round(vortag["l"], 3) if vortag else None,
    }


def bewerte(report: dict, tag: dict, prev_close: float, sigma: float | None, pip: float = 0.01,
            vortag_change: float | None = None) -> dict:
    """Vergleicht Prognose mit dem tatsächlichen FX-Tag.
    Vergleichsmodell („naiv“): Zielkurs = Kurs um 06:45, Richtung = wie am Vortag."""
    close = tag["c"]
    change = (close - prev_close) / pip
    in_range = report["range_low"] <= close <= report["range_high"]
    abweichung = abs(close - report["target"]) / pip
    naive_abw = abs(close - report["price"]) / pip
    score = report.get("bias_score") or 0
    neutral_band = 0.25 * (sigma or report.get("sigma_pips") or 50)
    if score == 0:
        richtung = abs(change) <= neutral_band
    else:
        richtung = (change > 0) == (score > 0) and change != 0
    return {
        "close": close, "open": tag["o"], "high": tag["h"], "low": tag["l"], "prev_close": prev_close,
        "change_pips": round(change, 1), "in_range": int(in_range), "deviation_pips": round(abweichung, 1),
        "direction_hit": int(richtung), "naive_deviation_pips": round(naive_abw, 1),
        "naive_direction_hit": (int((change > 0) == (vortag_change > 0) and change != 0)
                                if vortag_change not in (None, 0) else None),
    }


def quoten(evals: list[dict]) -> dict:
    n = len(evals)
    if not n:
        return {"n": 0, "range_pct": None, "direction_pct": None, "avg_dev": None, "naive_avg_dev": None,
                "naive_direction_pct": None}
    def pct(feld):
        werte = [e[feld] for e in evals if e.get(feld) is not None]
        return round(100 * sum(werte) / len(werte)) if werte else None
    def avg(feld):
        werte = [e[feld] for e in evals if e.get(feld) is not None]
        return round(sum(werte) / len(werte), 1) if werte else None
    return {"n": n, "range_pct": pct("in_range"), "direction_pct": pct("direction_hit"),
            "avg_dev": avg("deviation_pips"), "naive_avg_dev": avg("naive_deviation_pips"),
            "naive_direction_pct": pct("naive_direction_hit")}
