"""Tages-Setups für GBP/JPY – dieselben Regeln für Backtest und Live-Betrieb.

Alle Zeiten sind Berliner Zeit. Ein FX-Tag beginnt um 23:00 Berlin (17:00 New York).
Jede Strategie liefert pro Tag höchstens EIN Setup (Richtung offen bis zum Auslösen):
  - typ "stop":  Kauf-Stop / Verkauf-Stop (Ausbruch), wer zuerst auslöst, gilt (OCO)
  - typ "limit": Kauf-Limit / Verkauf-Limit (Rücklauf), wer zuerst berührt wird, gilt
  - typ "markt": Einstieg zum Eröffnungskurs der Startkerze in vorgegebener Richtung
Stop-Loss und Take-Profit sind feste Kursmarken; spätestens um `schluss` wird zum Kurs geschlossen.

Keine Anlageberatung: Die Setups sind mechanische Regeln, deren Ergebnisse im Backtest
und live offen ausgewiesen werden.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time

PIP = 0.01


def tm(t) -> int:
    """Minuten seit Beginn des FX-Tags (23:00 Berlin = 0). Nimmt time oder datetime."""
    if isinstance(t, datetime):
        t = t.time()
    return ((t.hour * 60 + t.minute) - 23 * 60) % 1440


@dataclass
class Order:
    richtung: int            # +1 long, -1 short
    entry: float
    sl: float
    tp: float


@dataclass
class Setup:
    strategie: str
    name: str
    typ: str                 # stop | limit | markt
    orders: list[Order]
    start: time              # ab wann Entry gültig (Berlin)
    ende: time               # bis wann Entry gültig
    schluss: time            # spätestens schließen
    info: dict = field(default_factory=dict)


@dataclass
class Trade:
    datum: str
    strategie: str
    richtung: int
    entry: float
    sl: float
    tp: float
    t_entry: datetime | None = None
    exit: float | None = None
    t_exit: datetime | None = None
    grund: str = ""          # tp | sl | zeit
    pips: float = 0.0
    r: float = 0.0


# ------------------------------------------------------------
# Tageskontext: was um den Planungszeitpunkt bekannt ist
# ------------------------------------------------------------
@dataclass
class Kontext:
    datum: str
    tag_open: float          # Eröffnung 23:00 Berlin
    atr: float               # ATR(14) der FX-Tage in Kurs-Einheiten
    vortag: dict             # {o,h,l,c}
    ema20: float | None      # EMA(20) der Tagesschlüsse bis Vortag
    trend5: float | None     # Schluss Vortag − Schluss vor 5 Tagen
    kerzen: list             # bisherige Kerzen des Tages [(t_berlin, o, h, l, c)]


def spanne(kerzen, von: time, bis: time):
    """Hoch/Tief der Kerzen mit Startzeit in [von, bis)."""
    ks = [k for k in kerzen if tm(von) <= tm(k[0]) < tm(bis)]
    if len(ks) < 4:
        return None
    return max(k[2] for k in ks), min(k[3] for k in ks)


# ------------------------------------------------------------
# Strategien (Parameter als Dict, damit der Backtest sie variieren kann)
# ------------------------------------------------------------
def orb(ctx: Kontext, p: dict) -> Setup | None:
    """Ausbruch aus einer Vorlauf-Spanne (Asien vor London bzw. Europa vor New York)."""
    s = spanne(ctx.kerzen, p["range_von"], p["range_bis"])
    if not s:
        return None
    hoch, tief = s
    breite = hoch - tief
    if not (p["min_atr"] * ctx.atr <= breite <= p["max_atr"] * ctx.atr):
        return None
    puf = p.get("puffer_pips", 2) * PIP
    erlaubt = (1, -1)
    if p.get("trendfilter") and ctx.ema20 is not None and ctx.trend5 is not None:
        c = ctx.vortag["c"]
        if c > ctx.ema20 and ctx.trend5 > 0:
            erlaubt = (1,)
        elif c < ctx.ema20 and ctx.trend5 < 0:
            erlaubt = (-1,)
        else:
            return None
    orders = []
    for richtung, entry in ((1, hoch + puf), (-1, tief - puf)):
        if richtung not in erlaubt:
            continue
        if p["sl"] == "gegenseite":
            sl = tief - puf if richtung > 0 else hoch + puf
        else:  # "mitte"
            sl = (hoch + tief) / 2
        risiko = abs(entry - sl)
        tp = entry + richtung * p["tp_r"] * risiko
        orders.append(Order(richtung, round(entry, 3), round(sl, 3), round(tp, 3)))
    return Setup(p["id"], p["name"], "stop", orders, p["range_bis"], p["entry_bis"], p["schluss"],
                 {"spanne_hoch": round(hoch, 3), "spanne_tief": round(tief, 3), "breite_pips": round(breite / PIP, 1)})


def reversion(ctx: Kontext, p: dict) -> Setup | None:
    """Rücklauf: an den Rändern der erwarteten Tagesspanne (Tages-Open ± m·ATR) Richtung Open."""
    oben = ctx.tag_open + p["m"] * ctx.atr
    unten = ctx.tag_open - p["m"] * ctx.atr
    sl_abst = p["sl_atr"] * ctx.atr
    ziel = p.get("tp_anteil", 1.0)   # 1.0 = zurück bis zum Tages-Open
    orders = [
        Order(-1, round(oben, 3), round(oben + sl_abst, 3), round(oben - ziel * (oben - ctx.tag_open), 3)),
        Order(1, round(unten, 3), round(unten - sl_abst, 3), round(unten + ziel * (ctx.tag_open - unten), 3)),
    ]
    return Setup(p["id"], p["name"], "limit", orders, p["start"], p["entry_bis"], p["schluss"],
                 {"tag_open": round(ctx.tag_open, 3), "atr_pips": round(ctx.atr / PIP, 1)})


def trend(ctx: Kontext, p: dict) -> Setup | None:
    """Trendfolge: Einstieg zur Startzeit in Richtung des Tagestrends (Schluss vs. EMA20 + 5-Tage-Momentum)."""
    if ctx.ema20 is None or ctx.trend5 is None:
        return None
    c = ctx.vortag["c"]
    if c > ctx.ema20 and ctx.trend5 > 0:
        richtung = 1
    elif c < ctx.ema20 and ctx.trend5 < 0:
        richtung = -1
    else:
        return None
    startkerze = next((k for k in ctx.kerzen if tm(k[0]) >= tm(p["start"])), None)
    ref = startkerze[1] if startkerze else c
    sl = ref - richtung * p["sl_atr"] * ctx.atr
    tp = ref + richtung * p["tp_atr"] * ctx.atr
    return Setup(p["id"], p["name"], "markt", [Order(richtung, round(ref, 3), round(sl, 3), round(tp, 3))],
                 p["start"], p["start"], p["schluss"], {"ema20": round(ctx.ema20, 3)})


STRATEGIEN = {"orb": orb, "reversion": reversion, "trend": trend}


# ------------------------------------------------------------
# Simulation eines Setups auf den Kerzen eines Tages
# ------------------------------------------------------------
def simuliere(setup: Setup, kerzen, kosten_pips: float, datum: str, laufend: bool = False) -> Trade | None:
    """Konservativ: Liegen SL und TP in derselben Kerze, zählt der SL.
    Stop-Orders werden bei Kurslücken zum schlechteren Eröffnungskurs ausgeführt."""
    offen: Trade | None = None
    a, e, z = tm(setup.start), tm(setup.ende), tm(setup.schluss)
    for t, o, h, l, c in kerzen:
        zt = tm(t)
        if offen is None:
            if zt < a:
                continue
            if zt > e:
                break
            for od in setup.orders:
                getroffen, preis = False, od.entry
                if setup.typ == "markt":
                    getroffen, preis = True, o
                elif setup.typ == "stop":
                    if od.richtung > 0 and h >= od.entry:
                        getroffen, preis = True, max(od.entry, o)
                    elif od.richtung < 0 and l <= od.entry:
                        getroffen, preis = True, min(od.entry, o)
                else:  # limit
                    if od.richtung > 0 and l <= od.entry:
                        getroffen, preis = True, min(od.entry, o)
                    elif od.richtung < 0 and h >= od.entry:
                        getroffen, preis = True, max(od.entry, o)
                if getroffen:
                    offen = Trade(datum, setup.strategie, od.richtung, preis, od.sl, od.tp, t_entry=t)
                    break
            if offen is None:
                continue
            # Entry-Kerze: nur SL prüfen, wenn er hinter dem Entry liegt (konservativ)
            if (offen.richtung > 0 and l <= offen.sl) or (offen.richtung < 0 and h >= offen.sl):
                return _schliessen(offen, offen.sl, t, "sl", kosten_pips)
            continue
        if zt >= z:
            return _schliessen(offen, o, t, "zeit", kosten_pips)
        if offen.richtung > 0:
            if l <= offen.sl:
                return _schliessen(offen, min(offen.sl, o), t, "sl", kosten_pips)
            if h >= offen.tp:
                return _schliessen(offen, offen.tp, t, "tp", kosten_pips)
        else:
            if h >= offen.sl:
                return _schliessen(offen, max(offen.sl, o), t, "sl", kosten_pips)
            if l <= offen.tp:
                return _schliessen(offen, offen.tp, t, "tp", kosten_pips)
    if offen is not None:
        letzte = kerzen[-1]
        if laufend and tm(letzte[0]) < z:
            offen.grund = "offen"      # Tag läuft noch, Position offen
            return offen
        return _schliessen(offen, letzte[4], letzte[0], "zeit", kosten_pips)
    return None


def _schliessen(tr: Trade, preis: float, t, grund: str, kosten_pips: float) -> Trade:
    tr.exit, tr.t_exit, tr.grund = preis, t, grund
    tr.pips = round(tr.richtung * (preis - tr.entry) / PIP - kosten_pips, 1)
    risiko = abs(tr.entry - tr.sl) / PIP
    tr.r = round(tr.pips / risiko, 3) if risiko > 0 else 0.0
    return tr


# ------------------------------------------------------------
# Das live genutzte Setup (Ergebnis des Backtests 2012–2022, siehe backtest_historie.json)
# ------------------------------------------------------------
HAUPT_SETUP = {
    "strategie": "orb", "id": "london_trend", "name": "London-Ausbruch in Trendrichtung",
    "range_von": time(1, 0), "range_bis": time(9, 0), "entry_bis": time(12, 0), "schluss": time(21, 0),
    "sl": "gegenseite", "tp_r": 2.0, "min_atr": 0.1, "max_atr": 0.5, "trendfilter": True, "puffer_pips": 2,
}


def trend_richtung(ctx: Kontext) -> int:
    """+1 Aufwärtstrend, -1 Abwärtstrend, 0 kein klarer Trend (Schluss vs. EMA20 und 5-Tage-Momentum)."""
    if ctx.ema20 is None or ctx.trend5 is None:
        return 0
    c = ctx.vortag["c"]
    if c > ctx.ema20 and ctx.trend5 > 0:
        return 1
    if c < ctx.ema20 and ctx.trend5 < 0:
        return -1
    return 0


def kein_setup_grund(ctx: Kontext, p: dict = HAUPT_SETUP) -> str | None:
    """Warum heute kein Setup entsteht (für die Nachricht), oder None."""
    if p.get("trendfilter") and trend_richtung(ctx) == 0:
        return "kein klarer Trend (Vortagesschluss und 5-Tage-Richtung widersprechen sich)"
    s = spanne(ctx.kerzen, p["range_von"], p["range_bis"])
    if not s:
        return "zu wenige Kursdaten für die Asien-Spanne"
    breite = s[0] - s[1]
    if breite > p["max_atr"] * ctx.atr:
        return f"Asien-Spanne zu breit ({breite / PIP:.0f} Pips > {p['max_atr'] * ctx.atr / PIP:.0f})"
    if breite < p["min_atr"] * ctx.atr:
        return f"Asien-Spanne zu eng ({breite / PIP:.0f} Pips)"
    return None
