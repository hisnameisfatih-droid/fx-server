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
from datetime import datetime, time, timedelta

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


def amd_erkennen(kerzen, atr: float, p: dict) -> dict | None:
    """AMD (Akkumulation – Manipulation – Distribution), rein aus bisherigen Kerzen, ohne Blick in die Zukunft.

    A: ruhige Spanne in [range_von, range_bis), Breite min_atr..max_atr · ATR
    M: im Fenster [range_bis, manip_bis) läuft der Kurs mind. min_sweep_pips über eine Seite hinaus
       (aber nicht mehr als max_sweep_atr · ATR – sonst ist es ein echter Ausbruch)
       und eine Kerze schließt innerhalb von max_kerzen wieder IN der Spanne
    D: Einstieg in Gegenrichtung zum Schluss dieser Kerze, Stop hinter dem Fehlausbruch,
       Ziel gegenüberliegende Spannenseite (mind. min_r · Risiko), Ziel 2 = Gegenseite + halbe Spanne.
    Liefert den aktuellen Stand: phase 'akkumulation' | 'manipulation' | 'signal' | 'kein' (+ grund)."""
    s = spanne(kerzen, p["range_von"], p["range_bis"])
    if not s:
        return None
    hoch, tief = s
    breite = hoch - tief
    basis = {"variante": p["id"], "name": p["name"], "spanne": [round(hoch, 3), round(tief, 3)],
             "breite_pips": round(breite / PIP, 1), "range_von": p["range_von"].strftime("%H:%M"),
             "range_bis": p["range_bis"].strftime("%H:%M"), "manip_bis": p["manip_bis"].strftime("%H:%M")}
    if not (p["min_atr"] * atr <= breite <= p["max_atr"] * atr):
        return {**basis, "phase": "kein", "grund": f"Spanne {'zu breit' if breite > p['max_atr'] * atr else 'zu eng'} "
                                                  f"({breite / PIP:.0f} Pips)"}
    min_sweep, max_sweep = p["min_sweep_pips"] * PIP, p["max_sweep_atr"] * atr
    sweep = None   # (seite +1 oben / -1 unten, extrem, index des ersten Durchbruchs, zeit)
    fenster = [k for k in kerzen if tm(p["range_bis"]) <= tm(k[0]) < tm(p["manip_bis"])]
    for i, (t, o, h, l, c) in enumerate(fenster):
        if sweep is None:
            oben, unten = h >= hoch + min_sweep, l <= tief - min_sweep
            if oben and unten:
                return {**basis, "phase": "kein", "grund": "beide Seiten in einer Kerze durchbrochen"}
            if oben or unten:
                sweep = [1 if oben else -1, h if oben else l, i, t]
            else:
                continue
        seite, extrem = sweep[0], sweep[1]
        extrem = max(extrem, h) if seite > 0 else min(extrem, l)
        sweep[1] = extrem
        if (extrem - hoch if seite > 0 else tief - extrem) > max_sweep:
            return {**basis, "phase": "kein", "grund": "echter Ausbruch statt Fehlausbruch", "sweep_seite": seite,
                    "sweep_extrem": round(extrem, 3)}
        gegen = (seite < 0 and h >= hoch + min_sweep) or (seite > 0 and l <= tief - min_sweep)
        if gegen:
            return {**basis, "phase": "kein", "grund": "beide Seiten durchbrochen"}
        zurueck = (tief < c < hoch)
        if zurueck:
            richtung = -seite
            entry = c
            sl = extrem + seite * p["sl_puffer_pips"] * PIP
            risiko = abs(entry - sl)
            ziel = tief if richtung < 0 else hoch
            belohnung = abs(ziel - entry)
            if risiko <= 0 or belohnung < p["min_r"] * risiko:
                return {**basis, "phase": "kein", "grund": "Chance/Risiko zu klein", "sweep_seite": seite,
                        "sweep_extrem": round(extrem, 3)}
            ziel2 = ziel + richtung * 0.5 * breite
            return {**basis, "phase": "signal", "sweep_seite": seite, "sweep_extrem": round(extrem, 3),
                    "sweep_zeit": sweep[3].strftime("%H:%M"), "signal_zeit": t.strftime("%H:%M"),
                    "richtung": richtung, "entry": round(entry, 3), "sl": round(sl, 3), "tp": round(ziel, 3),
                    "tp2": round(ziel2, 3), "risiko_pips": round(risiko / PIP), "ziel_pips": round(belohnung / PIP),
                    "start": (t + timedelta(minutes=15)).strftime("%H:%M")}   # Einstieg: Open der nächsten Kerze
        if i - sweep[2] + 1 >= p["max_kerzen"]:
            return {**basis, "phase": "kein", "grund": "kein Rücklauf in die Spanne", "sweep_seite": seite,
                    "sweep_extrem": round(extrem, 3)}
    if sweep:
        return {**basis, "phase": "manipulation", "sweep_seite": sweep[0], "sweep_extrem": round(sweep[1], 3),
                "sweep_zeit": sweep[3].strftime("%H:%M")}
    return {**basis, "phase": "akkumulation"}


def amd(ctx: Kontext, p: dict) -> Setup | None:
    """Für den Backtest: Signal-Erkennung + Einstieg zum Eröffnungskurs der nächsten Kerze."""
    erg = amd_erkennen(ctx.kerzen, ctx.atr, p)
    if not erg or erg["phase"] != "signal":
        return None
    hm = lambda s: time(int(s[:2]), int(s[3:]))  # noqa: E731
    return amd_setup(erg, p, hm(erg["start"]))


def amd_setup(erg: dict, p: dict, start: time) -> Setup:
    return Setup(p["id"], p["name"], "markt", [Order(erg["richtung"], erg["entry"], erg["sl"], erg["tp"])],
                 start, start, p["schluss"], {"tp2": erg["tp2"]})


AMD_VARIANTEN = [
    {"strategie": "amd", "id": "amd_london", "name": "Asien → London",
     "range_von": time(1, 0), "range_bis": time(9, 0), "manip_bis": time(12, 30), "schluss": time(21, 0),
     "min_atr": 0.15, "max_atr": 0.7, "min_sweep_pips": 3, "max_sweep_atr": 0.35, "max_kerzen": 8,
     "sl_puffer_pips": 5, "min_r": 1.0},
    {"strategie": "amd", "id": "amd_ny", "name": "London → New York",
     "range_von": time(9, 0), "range_bis": time(14, 0), "manip_bis": time(16, 30), "schluss": time(21, 30),
     "min_atr": 0.15, "max_atr": 0.7, "min_sweep_pips": 3, "max_sweep_atr": 0.35, "max_kerzen": 8,
     "sl_puffer_pips": 5, "min_r": 1.0},
]

# ------------------------------------------------------------
# 3-6-9 Time Map (T4F): Akkumulation 08:10–08:40, Manipulation 08:40–09:15, Distribution 09:15–09:45
# auf 5-Min-Kerzen. Zeitzone der Vorlage unbekannt -> Berlin-, London- und New-York-Zeit werden getestet.
# ------------------------------------------------------------
def z369_zeiten(datum: str, tz: str) -> tuple[time, time, time, time, time]:
    """Phasengrenzen (08:10, 08:40, 09:15, 09:45, Limit gültig bis 10:30) in Berliner Zeit für diesen Tag."""
    from datetime import date as _date
    from zoneinfo import ZoneInfo
    d = _date.fromisoformat(datum)
    z, berlin = ZoneInfo(tz), ZoneInfo("Europe/Berlin")
    return tuple(datetime(d.year, d.month, d.day, hh, mm, tzinfo=z).astimezone(berlin).time()
                 for hh, mm in ((8, 10), (8, 40), (9, 15), (9, 45), (10, 30)))


def z369_erkennen(kerzen, atr: float, p: dict, datum: str) -> dict | None:
    """Phase 3: Spanne 08:10–08:40. Phase 6: Sweep über Hoch/unter Tief (mind. min_sweep_pips) bis 09:15.
    Phase 9: bis 09:45 eine kräftige Kerze (Körper ≥ disp_faktor × Ø Kerzenspanne der Akkumulation) schließt
    hinter dem letzten Gegen-Swing vor dem Extrem (Strukturbruch/MSS) -> Signal in Gegenrichtung zum Sweep.
    Einstieg 'retrace': Limit auf halbem Weg zurück zum Extrem (gültig bis 10:30), 'markt': nächste Kerze.
    Stop: Extrem + sl_puffer_pips. Ziel: tp_r × Risiko."""
    A, M, D, E, G = z369_zeiten(datum, p["tz"])
    hm = lambda t: t.strftime("%H:%M")  # noqa: E731
    rng = [k for k in kerzen if tm(A) <= tm(k[0]) < tm(M)]
    basis = {"variante": p["id"], "name": p["name"], "range_von": hm(A), "range_bis": hm(M), "manip_bis": hm(D),
             "bis": hm(E)}
    if len(rng) < 3:
        return {**basis, "phase": "warten"} if not kerzen or tm(kerzen[-1][0]) < tm(M) else None
    hoch, tief = max(k[2] for k in rng), min(k[3] for k in rng)
    basis.update(spanne=[round(hoch, 3), round(tief, 3)], breite_pips=round((hoch - tief) / PIP, 1))
    nach = [k for k in kerzen if tm(M) <= tm(k[0]) < tm(E)]
    if tm(kerzen[-1][0]) < tm(M):
        return {**basis, "phase": "akkumulation"}
    if (hoch - tief) / PIP < p["min_breite_pips"]:
        return {**basis, "phase": "kein", "grund": "Spanne zu eng"}
    avg = sum(k[2] - k[3] for k in rng) / len(rng)
    min_sweep = p["min_sweep_pips"] * PIP
    seite = extrem = ext_i = sweep_t = mss = None
    alle = rng + nach
    for i, (t, o, h, l, c) in enumerate(nach):
        if seite is None:
            if tm(t) >= tm(D):
                return {**basis, "phase": "kein", "grund": "kein Sweep bis " + hm(D)}
            oben, unten = h >= hoch + min_sweep, l <= tief - min_sweep
            if oben and unten:
                return {**basis, "phase": "kein", "grund": "beide Seiten in einer Kerze"}
            if not (oben or unten):
                continue
            seite, extrem, ext_i, sweep_t = (1, h, i, t) if oben else (-1, l, i, t)
        else:   # Lauf durch die Gegenseite ist hier erwünscht (Auslieferung), entscheidend ist der Strukturbruch
            if (seite > 0 and h > extrem) or (seite < 0 and l < extrem):
                extrem, ext_i = (h if seite > 0 else l), i
        vor = alle[:len(rng) + ext_i][-6:]
        mss = min(k[3] for k in vor) if seite > 0 else max(k[2] for k in vor)
        if i > ext_i:
            koerper = abs(c - o)
            bruch = (c < mss and c < o) if seite > 0 else (c > mss and c > o)
            if bruch and koerper >= p["disp_faktor"] * avg:
                richtung = -seite
                if p["entry"] == "markt":
                    entry, typ, bis = c, "markt", None
                else:
                    entry, typ, bis = c + seite * 0.5 * abs(extrem - c), "limit", hm(G)
                sl = extrem + seite * p["sl_puffer_pips"] * PIP
                risiko = abs(entry - sl)
                start = (t + timedelta(minutes=5)).strftime("%H:%M")
                return {**basis, "phase": "signal", "sweep_seite": seite, "sweep_extrem": round(extrem, 3),
                        "sweep_zeit": hm(sweep_t), "signal_zeit": hm(t), "mss": round(mss, 3), "richtung": richtung,
                        "entry": round(entry, 3), "sl": round(sl, 3), "tp": round(entry + richtung * p["tp_r"] * risiko, 3),
                        "tp2": round(tief if richtung < 0 else hoch, 3), "risiko_pips": round(risiko / PIP),
                        "ziel_pips": round(p["tp_r"] * risiko / PIP), "typ": typ, "start": start, "gueltig_bis": bis or start}
    if seite is None:
        return {**basis, "phase": "akkumulation"}   # Spanne steht, Sweep-Fenster läuft noch
    ende_erreicht = tm(kerzen[-1][0]) >= tm(E)
    d = {**basis, "sweep_seite": seite, "sweep_extrem": round(extrem, 3), "sweep_zeit": hm(sweep_t),
         "mss": round(mss, 3) if mss is not None else None}
    return {**d, "phase": "kein", "grund": "keine Bestätigung bis " + hm(E)} if ende_erreicht else {**d, "phase": "manipulation"}


def z369_setup(erg: dict, p: dict) -> Setup:
    h = lambda s: time(int(s[:2]), int(s[3:]))  # noqa: E731
    return Setup(p["id"], p["name"], erg["typ"], [Order(erg["richtung"], erg["entry"], erg["sl"], erg["tp"])],
                 h(erg["start"]), h(erg["gueltig_bis"]), p["schluss"], {"tp2": erg["tp2"]})


def z369(ctx: Kontext, p: dict) -> Setup | None:
    erg = z369_erkennen(ctx.kerzen, ctx.atr, p, ctx.datum)
    if not erg or erg["phase"] != "signal":
        return None
    return z369_setup(erg, p)


Z369_TZ = {"berlin": ("Europe/Berlin", "Berlin-Zeit"), "london": ("Europe/London", "London-Zeit"),
           "newyork": ("America/New_York", "New-York-Zeit")}
Z369_VARIANTEN = [
    {"strategie": "z369", "id": f"z369_{f}_{e}_{int(r)}r", "name": f"3-6-9 {Z369_TZ[f][1]} · {'Rücklauf' if e == 'retrace' else 'sofort'} · {int(r)}R",
     "tz": Z369_TZ[f][0], "entry": e, "tp_r": r, "min_sweep_pips": 2, "min_breite_pips": 5, "disp_faktor": 1.0,
     "sl_puffer_pips": 2, "schluss": time(21, 30) if f == "newyork" else time(21, 0),
     "live": e == "retrace" and r == 2.0}
    for f in ("berlin", "london", "newyork") for e in ("retrace", "markt") for r in (2.0, 3.0)
]

STRATEGIEN = {"orb": orb, "reversion": reversion, "trend": trend, "amd": amd, "z369": z369}


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
