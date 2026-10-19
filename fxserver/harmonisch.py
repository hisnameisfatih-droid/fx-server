"""Gartley-222 (harmonisches Muster) – mechanische Erkennung und Backtest auf einer durchgehenden Kerzenreihe.

Regeln (Carney-Version des Gartley von S. 222):
  Wendepunkte X-A-B-C aus einem ZigZag (Umkehr ab k x ATR, erst NACH der Umkehr bekannt – kein Blick in die Zukunft)
  AB = 0,55–0,68 von XA (um 61,8 %), BC = 0,382–0,886 von AB
  D = 78,6 %-Rücklauf von XA, zusätzlich CD/BC zwischen 1,13 und 1,8 (sonst passt das Muster nicht)
  Einstieg: Limit-Order bei D, sobald C bestätigt ist (gültig, bis X oder 3 x Länge X→C überschritten ist)
  Stop: hinter X (+5 Pips). Ziel: 38,2 % bzw. 61,8 % der Strecke A→D. Spätestens nach 5 Tagen schließen.
  Liegen Stop und Ziel in derselben Kerze, zählt der Stop (konservativ).
"""
from __future__ import annotations

from datetime import timedelta

from .strategien import PIP, Trade

VARIANTEN = [
    {"id": f"gartley_{tf}_{k}_{int(tp * 1000)}", "name": f"Gartley {tf.upper()} (ZigZag {k}×ATR, Ziel {tp * 100:.1f} % AD)",
     "tf_min": {"15m": 15, "1h": 60}[tf], "k_atr": k, "tp_anteil": tp}
    for tf in ("15m", "1h") for k in (2, 3) for tp in (0.382, 0.618)
]
AB_BEREICH, BC_BEREICH, CD_BC_BEREICH, D_RUECKLAUF = (0.55, 0.68), (0.382, 0.886), (1.13, 1.8), 0.786
SL_PUFFER_PIPS, MAX_HALTEN = 5, timedelta(days=5)


def verdichten(kerzen: list[tuple], minuten: int) -> list[tuple]:
    """[(datum, t, o, h, l, c)] 15-Min -> größere Kerzen (Start auf volle `minuten`)."""
    if minuten == 15:
        return [(d, t, o, h, l, c) for d, t, o, h, l, c in kerzen]
    erg, akt = [], None
    for d, t, o, h, l, c in kerzen:
        start = t.replace(minute=(t.minute // minuten) * minuten if minuten < 60 else 0, second=0, microsecond=0)
        if akt and akt[1] == start:
            akt[3], akt[4], akt[5] = max(akt[3], h), min(akt[4], l), c
        else:
            if akt:
                erg.append(tuple(akt))
            akt = [d, start, o, h, l, c]
    if akt:
        erg.append(tuple(akt))
    return erg


def _atr(kerzen, i, n=14):
    if i < n:
        return None
    trs = [max(kerzen[j][3] - kerzen[j][4], abs(kerzen[j][3] - kerzen[j - 1][5]), abs(kerzen[j][4] - kerzen[j - 1][5]))
           for j in range(i - n + 1, i + 1)]
    return sum(trs) / n


def muster_pruefen(x, a, b, c) -> dict | None:
    """x,a,b,c: (preis, typ +1 Hoch / -1 Tief). Liefert Richtung und D oder None."""
    richtung = 1 if x[1] < 0 else -1          # bullisch: X Tief, A Hoch, B Tief, C Hoch
    xa, ab, bc = abs(a[0] - x[0]), abs(a[0] - b[0]), abs(c[0] - b[0])
    if xa <= 0 or ab <= 0 or bc <= 0:
        return None
    if not (AB_BEREICH[0] <= ab / xa <= AB_BEREICH[1] and BC_BEREICH[0] <= bc / ab <= BC_BEREICH[1]):
        return None
    d = a[0] - richtung * D_RUECKLAUF * xa
    cd = abs(c[0] - d)
    if not (CD_BC_BEREICH[0] <= cd / bc <= CD_BC_BEREICH[1]):
        return None
    return {"richtung": richtung, "d": d, "x": x[0], "a": a[0]}


def simuliere_gartley(kerzen: list[tuple], p: dict, kosten_pips: float = 3.0) -> list[Trade]:
    ks = verdichten(kerzen, p["tf_min"])
    pivots: list[tuple] = []        # (preis, typ, index)
    richtung, extrem, ext_i = 0, None, 0
    trades: list[Trade] = []
    order = None                     # wartende Limit-Order
    offen: Trade | None = None
    for i, (d, t, o, h, l, c) in enumerate(ks):
        # 1) offene Position verwalten
        if offen:
            if offen.richtung > 0:
                if l <= offen.sl:
                    trades.append(_schliessen(offen, min(offen.sl, o), t, "sl", kosten_pips)); offen = None
                elif h >= offen.tp:
                    trades.append(_schliessen(offen, offen.tp, t, "tp", kosten_pips)); offen = None
            else:
                if h >= offen.sl:
                    trades.append(_schliessen(offen, max(offen.sl, o), t, "sl", kosten_pips)); offen = None
                elif l <= offen.tp:
                    trades.append(_schliessen(offen, offen.tp, t, "tp", kosten_pips)); offen = None
            if offen and t - offen.t_entry >= MAX_HALTEN:
                trades.append(_schliessen(offen, c, t, "zeit", kosten_pips)); offen = None
        # 2) wartende Order: ausgelöst, verfallen?
        elif order:
            r = order["richtung"]
            if (r > 0 and l <= order["x"]) or (r < 0 and h >= order["x"]) or i > order["bis"]:
                order = None
            elif (r > 0 and l <= order["d"]) or (r < 0 and h >= order["d"]):
                preis = min(order["d"], o) if r > 0 else max(order["d"], o)
                offen = Trade(d, p["id"], r, preis, order["sl"], order["tp"], t_entry=t)
                order = None
                if (r > 0 and l <= offen.sl) or (r < 0 and h >= offen.sl):     # gleiche Kerze: Stop zählt
                    trades.append(_schliessen(offen, offen.sl, t, "sl", kosten_pips)); offen = None
        # 3) ZigZag fortschreiben (Pivot erst nach Umkehr um k x ATR bekannt)
        atr = _atr(ks, i)
        if atr is None:
            continue
        schwelle = p["k_atr"] * atr
        neu = None
        if richtung >= 0:
            if extrem is None or h > extrem:
                extrem, ext_i, richtung = h, i, 1
            elif extrem - l >= schwelle:
                neu = (extrem, 1, ext_i); richtung, extrem, ext_i = -1, l, i
        if richtung < 0 and neu is None:
            if l < extrem:
                extrem, ext_i = l, i
            elif h - extrem >= schwelle:
                neu = (extrem, -1, ext_i); richtung, extrem, ext_i = 1, h, i
        if neu is None:
            continue
        pivots.append(neu)
        if len(pivots) < 4 or offen or order:
            continue
        x, a, b, cc = pivots[-4:]
        m = muster_pruefen(x, a, b, cc)
        if not m:
            continue
        r = m["richtung"]
        if (r > 0 and c <= m["d"]) or (r < 0 and c >= m["d"]):
            continue     # bei Bestätigung von C schon an D vorbei -> verpasst
        sl = m["x"] - r * SL_PUFFER_PIPS * PIP
        tp = m["d"] + r * p["tp_anteil"] * abs(m["a"] - m["d"])
        order = {"richtung": r, "d": m["d"], "x": m["x"], "sl": sl, "tp": tp, "bis": i + 3 * (cc[2] - x[2])}
    return trades


def _schliessen(tr: Trade, preis: float, t, grund: str, kosten_pips: float) -> Trade:
    tr.exit, tr.t_exit, tr.grund = preis, t, grund
    tr.pips = round(tr.richtung * (preis - tr.entry) / PIP - kosten_pips, 1)
    risiko = abs(tr.entry - tr.sl) / PIP
    tr.r = round(tr.pips / risiko, 3) if risiko > 0 else 0.0
    return tr
