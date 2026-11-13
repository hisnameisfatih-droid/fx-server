"""Backtest "London-Retest" für GBP/JPY (Fatihs Setup, 13.11.2026).

Regeln (Kern-Variante A):
  1. Range = 5-Min-Kerze 07:30–07:35 UTC – eine halbe Stunde nach London-Open 07:00 UTC wie im TradingView-
     Sessions-Kasten, also 08:30 Berlin im Winter und 09:30 Berlin im Sommer.
  2. Ab 08:35: Ausbruch = erste 5-Min-Kerze, die über dem Range-Hoch bzw. unter dem Range-Tief SCHLIESST.
  3. Danach kommt der Kurs zurück in die Range (berührt sie wieder).
  4. Geht er danach wieder über die Ausbruchsseite hinaus -> Einstieg (Stop-Order an der Range-Kante).
  5. TP immer 20 Pips, SL = gegenüberliegende Range-Seite.
  Läuft der Kurs vor dem Einstieg bis zur Gegenseite durch, ist das Setup ungültig.
  Einstieg nur bis entry_bis (UTC), offene Trades werden um SCHLUSS (UTC) geschlossen. Alle Zeiten UTC.

Rechnet mit 1-Min-Kerzen (Mittelkurs aus Bid/Ask von Dukascopy), damit die Reihenfolge innerhalb einer
5-Min-Kerze stimmt. Liegen SL und TP in derselben Minute, zählt der SL. Kosten: wahlweise 0 oder 3 Pips je Trade.

Aufruf: python3 -I london_retest.py <bid.csv> <ask.csv> <ausgabe-ordner> [tage=100]
        python3 -I london_retest.py <gbpjpy_m1_mid.csv.gz> - <ausgabe-ordner> [tage=100]   (gespeicherte Mittelkurse)
"""
from __future__ import annotations

import csv
import gzip
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC, BER = timezone.utc, ZoneInfo("Europe/Berlin")
PIP = 0.01
TP_PIPS = 20.0
RANGE_VON = time(7, 30)          # UTC: 08:30 Berlin (Winter) / 09:30 Berlin (Sommer)
RANGE_BIS = time(7, 35)
SCHLUSS = time(20, 0)            # UTC: 21:00 Berlin (Winter) / 22:00 Berlin (Sommer)
KOSTEN = (0.0, 3.0)

VARIANTEN = {
    "A": {"name": "Kern: Ausbruch per 5-Min-Schluss, Rücklauf = Berührung, Einstieg beim erneuten Überschreiten",
          "ausbruch": "schluss", "ruecklauf": "beruehrung", "einstieg": "stop", "entry_bis": time(11, 0)},
    "B": {"name": "Rücklauf muss in der Range SCHLIESSEN (5 Min), dann Einstieg beim Überschreiten",
          "ausbruch": "schluss", "ruecklauf": "schluss", "einstieg": "stop", "entry_bis": time(11, 0)},
    "C": {"name": "Einstieg erst, wenn eine 5-Min-Kerze wieder jenseits der Kante schließt (Bestätigung)",
          "ausbruch": "schluss", "ruecklauf": "beruehrung", "einstieg": "schluss", "entry_bis": time(11, 0)},
    "D": {"name": "Ausbruch schon per Docht (mind. 1 Pip über/unter der Range)",
          "ausbruch": "docht", "ruecklauf": "beruehrung", "einstieg": "stop", "entry_bis": time(11, 0)},
    "E": {"name": "Wie Kern, aber Einstieg bis 15:00 UTC (16:00 Berlin im Winter) erlaubt",
          "ausbruch": "schluss", "ruecklauf": "beruehrung", "einstieg": "stop", "entry_bis": time(15, 0)},
}


def lade_mid(pfad: str):
    with gzip.open(pfad, "rt") as f:
        return [(int(r["ts_utc"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                 float(r["spread_pips"])) for r in csv.DictReader(f)]


def lade(bid_pfad: str, ask_pfad: str):
    def lies(p):
        erg = {}
        with open(p) as f:
            for r in csv.DictReader(f):
                ts = int(float(r["timestamp"])) // 1000
                erg[ts] = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
        return erg
    bid, ask = lies(bid_pfad), lies(ask_pfad)
    kerzen = []
    for ts in sorted(set(bid) & set(ask)):
        b, a = bid[ts], ask[ts]
        mid = tuple((x + y) / 2 for x, y in zip(b, a))
        kerzen.append((ts, *mid, (a[3] - b[3]) / PIP))   # ts, o, h, l, c, spread_pips
    return kerzen


def nach_tag(kerzen):
    tage = defaultdict(list)
    for k in kerzen:
        t = datetime.fromtimestamp(k[0], UTC)
        if t.weekday() < 5:
            tage[t.date()].append((t, *k[1:]))
    return tage


def fuenf_min(ks):
    """1-Min (t London, o,h,l,c,spr) -> 5-Min-Kerzen [(start, o,h,l,c, ende_index)]."""
    g = {}
    for i, (t, o, h, l, c, _) in enumerate(ks):
        s = t.replace(minute=t.minute - t.minute % 5, second=0)
        if s not in g:
            g[s] = [s, o, h, l, c, i]
        else:
            g[s][2], g[s][3], g[s][4], g[s][5] = max(g[s][2], h), min(g[s][3], l), c, i
    return [tuple(v) for _, v in sorted(g.items())]


def tag_simulieren(ks, v):
    """Gibt (info, trade|None) zurück. ks: 1-Min-Kerzen eines London-Tages."""
    rk = [k for k in ks if RANGE_VON <= k[0].time() < RANGE_BIS]
    if len(rk) < 4:
        return {"status": "keine Daten 08:30"}, None
    H, L = max(k[2] for k in rk), min(k[3] for k in rk)
    breite = (H - L) / PIP
    info = {"range_hoch": round(H, 3), "range_tief": round(L, 3), "range_pips": round(breite, 1),
            "spread_0930": round(st.mean(k[5] for k in rk), 1)}
    if breite < 1:
        return {**info, "status": "Range < 1 Pip"}, None
    bis = v["entry_bis"]
    nach = [k for k in ks if RANGE_BIS <= k[0].time() < SCHLUSS]
    f5 = fuenf_min(nach)
    # 1) Ausbruch
    richtung, ab_index = 0, None
    if v["ausbruch"] == "schluss":
        for s, o, h, l, c, ende_i in f5:
            if s.time() >= bis:
                break
            if c > H:
                richtung, ab_index, info["ausbruch_zeit"] = 1, ende_i + 1, s.strftime("%H:%M")
                break
            if c < L:
                richtung, ab_index, info["ausbruch_zeit"] = -1, ende_i + 1, s.strftime("%H:%M")
                break
    else:
        for i, k in enumerate(nach):
            if k[0].time() >= bis:
                break
            oben, unten = k[2] >= H + PIP, k[3] <= L - PIP
            if oben and unten:
                return {**info, "status": "beide Seiten in einer Minute"}, None
            if oben or unten:
                richtung, ab_index, info["ausbruch_zeit"] = (1 if oben else -1), i + 1, k[0].strftime("%H:%M")
                break
    if not richtung:
        return {**info, "status": "kein Ausbruch"}, None
    info["richtung"] = richtung
    kante, gegen = (H, L) if richtung > 0 else (L, H)
    rest = nach[ab_index:]
    # 2) Rücklauf in die Range (vor Gegenseite)
    rl_index = None
    if v["ruecklauf"] == "beruehrung":
        for i, k in enumerate(rest):
            if k[0].time() >= bis:
                break
            if (richtung > 0 and k[3] <= L) or (richtung < 0 and k[2] >= H):
                return {**info, "status": "vor Einstieg bis zur Gegenseite durchgelaufen"}, None
            if (richtung > 0 and k[3] <= kante) or (richtung < 0 and k[2] >= kante):
                rl_index, info["ruecklauf_zeit"] = i + 1, k[0].strftime("%H:%M")
                break
    else:
        r5 = fuenf_min(rest)
        for s, o, h, l, c, ende_i in r5:
            if s.time() >= bis:
                break
            if (richtung > 0 and l <= L) or (richtung < 0 and h >= H):
                return {**info, "status": "vor Einstieg bis zur Gegenseite durchgelaufen"}, None
            if L <= c <= H:
                rl_index, info["ruecklauf_zeit"] = ende_i + 1, s.strftime("%H:%M")
                break
    if rl_index is None:
        return {**info, "status": "kein Rücklauf in die Range"}, None
    rest2 = rest[rl_index:]
    # 3) Einstieg in Ausbruchsrichtung
    e_index, entry = None, None
    if v["einstieg"] == "stop":
        for i, k in enumerate(rest2):
            if k[0].time() >= bis:
                break
            if (richtung > 0 and k[3] <= L) or (richtung < 0 and k[2] >= H):
                return {**info, "status": "vor Einstieg bis zur Gegenseite durchgelaufen"}, None
            if (richtung > 0 and k[2] > kante) or (richtung < 0 and k[3] < kante):
                e_index = i
                entry = max(kante, k[1]) if richtung > 0 else min(kante, k[1])   # Lücke: schlechterer Kurs
                break
    else:
        r5 = fuenf_min(rest2)
        for s, o, h, l, c, ende_i in r5:
            if s.time() >= bis:
                break
            if (richtung > 0 and l <= L) or (richtung < 0 and h >= H):
                return {**info, "status": "vor Einstieg bis zur Gegenseite durchgelaufen"}, None
            if (richtung > 0 and c > kante) or (richtung < 0 and c < kante):
                if ende_i + 1 < len(rest2):
                    e_index, entry = ende_i + 1, rest2[ende_i + 1][1]
                break
    if e_index is None:
        return {**info, "status": "kein erneuter Ausbruch bis Einstiegsschluss"}, None
    sl, tp = gegen, entry + richtung * TP_PIPS * PIP
    risiko = abs(entry - sl) / PIP
    if risiko <= 0:
        return {**info, "status": "Einstieg schon jenseits des Stops"}, None
    lauf = rest2[e_index:]
    t0 = lauf[0][0]
    exit_k, grund, exit_preis = None, None, None
    for j, k in enumerate(lauf):
        t, o, h, l, c, _ = k
        sl_hit = (richtung > 0 and l <= sl) or (richtung < 0 and h >= sl)
        tp_hit = (richtung > 0 and h >= tp) or (richtung < 0 and l <= tp)
        if sl_hit:   # in der Einstiegs-Minute nur SL; SL hat bei gleichzeitigem Treffer Vorrang
            exit_k, grund, exit_preis = k, "sl", (min(sl, o) if richtung > 0 else max(sl, o)) if j else sl
            break
        if tp_hit and j > 0:
            exit_k, grund, exit_preis = k, "tp", tp
            break
    if exit_k is None:
        exit_k, grund, exit_preis = lauf[-1], "zeit", lauf[-1][4]
    pips = richtung * (exit_preis - entry) / PIP
    return {**info, "status": "Trade"}, {
        "richtung": richtung, "entry": round(entry, 3), "sl": round(sl, 3), "tp": round(tp, 3),
        "entry_zeit": t0.strftime("%H:%M"), "range_berlin": t0.replace(hour=7, minute=30).astimezone(BER).strftime("%H:%M"), "entry_zeit_berlin": t0.astimezone(BER).strftime("%H:%M"),
        "exit_zeit": exit_k[0].strftime("%H:%M"), "dauer_min": int((exit_k[0] - t0).total_seconds() // 60),
        "grund": grund, "pips_brutto": round(pips, 1), "risiko_pips": round(risiko, 1)}


def kennzahlen(trades, kosten):
    if not trades:
        return {"n": 0}
    p = [t["pips_brutto"] - kosten for t in trades]
    gew, verl = [x for x in p if x > 0], [x for x in p if x <= 0]
    kurve = hoch = dd = 0.0
    serie = max_serie = 0
    for x in p:
        kurve += x
        hoch = max(hoch, kurve)
        dd = max(dd, hoch - kurve)
        serie = serie + 1 if x <= 0 else 0
        max_serie = max(max_serie, serie)
    r = [(t["pips_brutto"] - kosten) / t["risiko_pips"] for t in trades]
    return {"n": len(p), "tp": sum(t["grund"] == "tp" for t in trades), "sl": sum(t["grund"] == "sl" for t in trades),
            "zeit": sum(t["grund"] == "zeit" for t in trades), "trefferquote": round(100 * len(gew) / len(p), 1),
            "summe_pips": round(sum(p), 1), "avg_pips": round(st.mean(p), 2), "avg_r": round(st.mean(r), 2),
            "pf": round(sum(gew) / -sum(verl), 2) if verl and sum(verl) < 0 else None,
            "avg_gewinn": round(st.mean(gew), 1) if gew else 0, "avg_verlust": round(st.mean(verl), 1) if verl else 0,
            "max_dd_pips": round(dd, 1), "max_verlustserie": max_serie,
            "avg_risiko_pips": round(st.mean(t["risiko_pips"] for t in trades), 1),
            "median_dauer_min": int(st.median(t["dauer_min"] for t in trades))}


def gruppen(trades, schluessel, kosten=3.0):
    g = defaultdict(list)
    for t in trades:
        g[schluessel(t)].append(t)
    return {k: kennzahlen(v, kosten) for k, v in sorted(g.items())}


def breite_klasse(t):
    b = t["range_pips"]
    return "a <8 P" if b < 8 else "b 8–15 P" if b < 15 else "c 15–25 P" if b < 25 else "d ≥25 P"


def main():
    bid, ask, aus = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    n_tage = int(sys.argv[4]) if len(sys.argv) > 4 else 100
    aus.mkdir(parents=True, exist_ok=True)
    kerzen = lade_mid(bid) if ask == "-" else lade(bid, ask)
    if ask != "-":
      with gzip.open(aus / "gbpjpy_m1_mid.csv.gz", "wt") as f:   # für weitere Auswertungen ohne neuen Download
        w = csv.writer(f)
        w.writerow(["ts_utc", "open", "high", "low", "close", "spread_pips"])
        for k in kerzen:
            w.writerow([k[0], *[round(x, 4) for x in k[1:5]], round(k[5], 2)])
    tage = nach_tag(kerzen)
    gueltig = [d for d in sorted(tage) if any(RANGE_VON <= k[0].time() < RANGE_BIS for k in tage[d])]
    gueltig = [d for d in gueltig if datetime.now(UTC).date() > d or datetime.now(UTC).time() >= SCHLUSS][-n_tage:]
    erg = {"zeitraum": [gueltig[0].isoformat(), gueltig[-1].isoformat()], "tage": len(gueltig),
           "tp_pips": TP_PIPS, "varianten": {}}
    for vid, v in VARIANTEN.items():
        trades, status = [], defaultdict(int)
        tagliste = []
        for d in gueltig:
            info, tr = tag_simulieren(tage[d], v)
            status[info["status"]] += 1
            eintrag = {"datum": d.isoformat(), "wochentag": "Mo Di Mi Do Fr".split()[d.weekday()], **info}
            if tr:
                eintrag.update(tr)
                trades.append(eintrag)
            tagliste.append(eintrag)
        erg["varianten"][vid] = {
            "name": v["name"], "status": dict(status),
            "kennzahlen": {f"kosten_{int(k)}": kennzahlen(trades, k) for k in KOSTEN},
            "long_short": gruppen(trades, lambda t: "Long" if t["richtung"] > 0 else "Short"),
            "range_breite": gruppen(trades, breite_klasse),
            "wochentag": gruppen(trades, lambda t: t["wochentag"]),
            "monat": gruppen(trades, lambda t: t["datum"][:7]),
            "einstieg_stunde": gruppen(trades, lambda t: t["entry_zeit"][:2] + " Uhr UTC"),
            "tage": tagliste}
        with open(aus / f"trades_{vid}.csv", "w", newline="") as f:
            felder = ["datum", "wochentag", "range_berlin", "range_hoch", "range_tief", "range_pips", "ausbruch_zeit", "richtung",
                      "ruecklauf_zeit", "entry_zeit", "entry_zeit_berlin", "entry", "sl", "tp", "risiko_pips", "grund",
                      "exit_zeit", "dauer_min", "pips_brutto", "status"]
            w = csv.DictWriter(f, fieldnames=felder, extrasaction="ignore")
            w.writeheader()
            w.writerows(tagliste)
    (aus / "ergebnis.json").write_text(json.dumps(erg, ensure_ascii=False, indent=1))
    z = [f"# London-Retest GBP/JPY · {erg['zeitraum'][0]} bis {erg['zeitraum'][1]} · {erg['tage']} Handelstage",
         "Range-Kerze 07:30–07:35 UTC (08:30 Berlin im Winter, 09:30 im Sommer), alle Zeiten der Tabelle UTC", ""]
    for vid, x in erg["varianten"].items():
        k0, k3 = x["kennzahlen"]["kosten_0"], x["kennzahlen"]["kosten_3"]
        z.append(f"## {vid}: {x['name']}")
        z.append(f"Tage: {dict(sorted(x['status'].items(), key=lambda i: -i[1]))}")
        if k3.get("n"):
            z.append(f"Trades {k3['n']} · TP {k3['tp']} · SL {k3['sl']} · Tagesende {k3['zeit']} · "
                     f"Treffer {k3['trefferquote']} % · Ø {k3['avg_pips']} P ({k3['avg_r']} R) · Summe {k3['summe_pips']} P · "
                     f"PF {k3['pf']} · MaxDD {k3['max_dd_pips']} P · Ø SL {k3['avg_risiko_pips']} P   [mit 3 Pips Kosten]")
            z.append(f"ohne Kosten: Summe {k0['summe_pips']} P, Ø {k0['avg_pips']} P, PF {k0['pf']}")
        z.append("")
    (aus / "bericht.md").write_text("\n".join(z))
    print("\n".join(z))


if __name__ == "__main__":
    main()
