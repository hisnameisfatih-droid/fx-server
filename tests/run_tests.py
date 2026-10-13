"""Offline-Test des kompletten Ablaufs mit nachgebauten Datenquellen.

python tests/run_tests.py [ausgabe_ordner]
Schreibt Dashboard-JSONs nach <ausgabe_ordner>/public und prüft Report, Auswertung, Refresh.
"""
from __future__ import annotations

import io
import json
import math
import os
import random
import sys
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="fxtest-"))
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "env").write_text("TIINGO_API_KEY=t\nWHATSAPP_PHONE=+4915100000000\nCALLMEBOT_APIKEY=1\n"
                         "ANTHROPIC_API_KEY=sk-test\nDASHBOARD_URL=https://1-2-3-4.sslip.io/geheim123/\n")
os.environ.update(FX_ENV_FILE=str(OUT / "env"), FX_DATA_DIR=str(OUT), FX_LOG_DIR=str(OUT / "logs"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxserver import common, evaluate, export, forecast, llm, refresh, report, setup, strategien  # noqa: E402
from fxserver.sources import calendar as cal_src, news as news_src, prices as px_src, yields as y_src  # noqa: E402

random.seed(7)
JETZT = datetime.now(timezone.utc)
FEHLER = []


def pruefe(bedingung, text):
    print(("  ✓ " if bedingung else "  ✗ ") + text)
    if not bedingung:
        FEHLER.append(text)


# ---------------- Testdaten ----------------
def kerzen_reihe(start_kurs, schritt_std, stunden, vol):
    t0 = (JETZT - timedelta(hours=stunden)).replace(minute=0, second=0, microsecond=0)
    k, erg = start_kurs, []
    for i in range(stunden):
        t = t0 + timedelta(hours=i)
        if t.weekday() == 5 or (t.weekday() == 6 and t.hour < 21) or (t.weekday() == 4 and t.hour >= 21):
            continue
        o = k
        k = k + random.gauss(0, vol)
        h, l = max(o, k) + abs(random.gauss(0, vol / 2)), min(o, k) - abs(random.gauss(0, vol / 2))
        erg.append({"date": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "open": o, "high": h, "low": l, "close": k})
    return erg


TIINGO = {"gbpjpy": kerzen_reihe(208.5, 1, 35 * 24, 0.12), "usdjpy": kerzen_reihe(151.2, 1, 35 * 24, 0.08),
          "gbpusd": kerzen_reihe(1.3790, 1, 35 * 24, 0.0006)}


def ff_json():
    heute = datetime.now(common.TZ_BERLIN).replace(second=0, microsecond=0)
    def ev(h, m, land, titel, impact, f="", p=""):
        t = heute.replace(hour=h, minute=m).astimezone(common.TZ_NY)
        return {"title": titel, "country": land, "date": t.isoformat(), "impact": impact, "forecast": f, "previous": p}
    return [ev(8, 0, "GBP", "GDP m/m", "High", "0.2%", "0.1%"), ev(10, 30, "GBP", "MPC Member Mann Speaks", "Medium"),
            ev(14, 30, "USD", "CPI m/m", "High", "0.3%", "0.4%"), ev(16, 0, "USD", "Crude Oil Inventories", "Low"),
            ev(1, 50, "JPY", "Core Machinery Orders m/m", "Medium", "1.1%", "-0.5%"), ev(9, 0, "EUR", "German ZEW", "High")]


def rss(titel_liste, quelle=""):
    items = "".join(f"<item><title>{t}{quelle}</title><link>https://example.com/{i}</link>"
                    f"<pubDate>{(JETZT - timedelta(hours=i + 1)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
                    f"<description>Kurztext zu {t}</description></item>" for i, t in enumerate(titel_liste))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title><link>y</link><description>z</description>{items}</channel></rss>'


def boe_zip():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "4. spot curve"
    ws.append(["UK nominal spot curve"])
    ws.append([])
    ws.append(["years:"] + [0.5 * i for i in range(1, 81)])
    d = date.today() - timedelta(days=40)
    while d <= date.today() - timedelta(days=1):
        if d.weekday() < 5:
            ws.append([datetime(d.year, d.month, d.day)] + [3.8 + 0.02 * i / 2 + random.gauss(0, 0.02) for i in range(1, 81)])
        d += timedelta(days=1)
    puffer = io.BytesIO()
    wb.save(puffer)
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("GLC Nominal daily data current month.xlsx", puffer.getvalue())
    return z.getvalue()


def treasury_csv():
    zeilen = ['Date,"1 Mo","2 Yr","10 Yr","30 Yr"']
    d = date.today()
    for i in range(1, 60):
        t = d - timedelta(days=i)
        if t.weekday() < 5:
            zeilen.append(f"{t:%m/%d/%Y},4.1,4.8,{5.2 + random.gauss(0, 0.03):.2f},5.6")
    return "\n".join(zeilen)


def mof_csv():
    zeilen = ["Interest Rate (October 2026),,,,(Unit : %)", "Date,1Y,2Y,3Y,4Y,5Y,6Y,7Y,8Y,9Y,10Y,15Y,20Y,25Y,30Y,40Y"]
    d = date.today() - timedelta(days=45)
    while d < date.today():
        if d.weekday() < 5:
            zeilen.append(f"{d.year}/{d.month}/{d.day},1.6,{1.9 + random.gauss(0, 0.01):.3f},2,2.2,2.4,2.5,2.6,2.8,2.9,"
                          f"{3.05 + random.gauss(0, 0.02):.3f},3.6,3.9,4.1,4.1,4.1")
        d += timedelta(days=1)
    return "\n".join(zeilen)


class Antwort:
    def __init__(self, text=None, content=None, daten=None):
        self.text = text if text is not None else (json.dumps(daten) if daten is not None else "")
        self.content = content if content is not None else self.text.encode()
        self._daten = daten
        self.status_code = 200

    def json(self):
        return self._daten if self._daten is not None else json.loads(self.text)


AUFRUFE = []


def fake_get(url, *, params=None, headers=None, timeout=20, versuche=2):
    AUFRUFE.append(url)
    if "faireconomy" in url:
        return Antwort(daten=ff_json())
    if "fxstreet" in url:
        return Antwort(text=rss(["GBP/JPY Price Forecast: bulls eye 210.00", "BoE's Mann warns on sticky inflation",
                                  "Gold hits record high", "Yen weakens as BoJ signals patience"]))
    if "actionforex" in url:
        return Antwort(text=rss(["Sunset Market Commentary", "UK GDP preview: modest growth expected"]))
    if "news.google" in url:
        return Antwort(text=rss(["Sterling steadies ahead of UK budget"], " - Reuters"))
    if "bankofengland.co.uk/rss" in url:
        raise RuntimeError("BoE-Feed simuliert nicht erreichbar")
    if "boj.or.jp" in url:
        return Antwort(text=rss(["Basic Figures on Fails (Sept.)", "Summary of Opinions at the Monetary Policy Meeting"]))
    if "treasury.gov" in url:
        return Antwort(text=treasury_csv())
    if "mof.go.jp" in url:
        return Antwort(text=mof_csv())
    if "yield-curves" in url:
        return Antwort(content=BOE_ZIP)
    if "api.tiingo.com/tiingo/fx/top" in url:
        return Antwort(daten=[{"ticker": t, "midPrice": TIINGO[t][-1]["close"]} for t in params["tickers"].split(",")])
    if "api.tiingo.com/tiingo/fx/" in url:
        paar = url.split("/fx/")[1].split("/")[0]
        return Antwort(daten=TIINGO[paar])
    raise RuntimeError(f"unbekannte URL {url}")


BOE_ZIP = boe_zip()


def fake_llm(model, system, nutzer, werkzeug, max_tokens=2000):
    AUFRUFE.append(f"claude:{werkzeug['name']}")
    if werkzeug["name"] == "news_einordnung":
        daten = json.loads(nutzer.split("\n\n", 1)[1])
        return {"meldungen": [{"id": m["id"], "relevanz": 7 if "GBP" in m["titel"] or "BoE" in m["titel"] or "Yen" in m["titel"] else 2,
                               "waehrungen": ["GBP"] if "GBP" in m["titel"] or "Sterling" in m["titel"] or "BoE" in m["titel"] else ["JPY"],
                               "wirkung": "GBP+" if "GBP" in m["titel"] else "JPY-", "einordnung": "Testeinordnung."} for m in daten]}
    roh = json.loads(nutzer.split("\n\n", 1)[1])
    return {"lage_gbp": "Das Pfund wird vom Ausblick auf eine vorsichtige BoE gestützt. Die Gilt-Renditen sind leicht gestiegen.",
            "lage_jpy": "Der Yen bleibt schwach, die BoJ signalisiert Geduld bei weiteren Zinsschritten.",
            "termin_wirkungen": [{"id": t["id"], "wirkung": "Stärkere Daten würden das Pfund stützen."} for t in roh["termine_heute_berlin"]],
            "news_score": 1, "news_grund": "Zinsvorteil des Pfunds und schwacher Yen überwiegen.",
            "news_grund_lang": "Der Zinsabstand zwischen Gilts und JGBs bleibt groß. Das passt zur Trendregel.",
            "risiken": "Verbale Intervention aus Tokio oder schwache UK-Daten könnten den Bias kippen."}


GESENDET = []
for modul in (cal_src, news_src, px_src, y_src):
    modul.http_get = fake_get
llm._aufruf = fake_llm
report.whatsapp = lambda text, log=None: GESENDET.append(text) or True
refresh.whatsapp = lambda text, log=None: GESENDET.append(text) or True

# ---------------- Tests ----------------
print("Einheiten")
pruefe(common.fx_tag(datetime(2026, 10, 12, 20, 30, tzinfo=timezone.utc)) == date(2026, 10, 12), "FX-Tag Mo 22:30 Berlin = Montag")
pruefe(common.fx_tag(datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc)) == date(2026, 10, 13), "FX-Tag Mo 23:30 Berlin = Dienstag")
pruefe(common.fx_tag(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)) == date(2026, 10, 19), "Samstag gehört zu Montag")
pruefe(common.de_zahl(209.5123) == "209,512" and common.de_prozent(0.234) == "+0,23%", "deutsche Zahlen")
termine = cal_src.parse_ff(ff_json())
pruefe(len(termine) == 4 and all(t["currency"] in ("GBP", "USD", "JPY") for t in termine), "Kalender filtert EUR und Low")
pruefe(len(y_src.parse_boe_zip(BOE_ZIP)) > 20 and "uk30" in next(iter(y_src.parse_boe_zip(BOE_ZIP).values())), "BoE-Zip: Gilt 10y/30y")
pruefe(y_src.parse_iadb("DATE,IUDMNPY\n01 Oct 2026,4.512\n02 Oct 2026,4.530\n") == {"2026-10-01": {"uk10": 4.512}, "2026-10-02": {"uk10": 4.53}}, "BoE-Datenbank-CSV")
tage_t = [{"o": 200 + i * 0.1, "h": 200.8 + i * 0.1, "l": 199.6 + i * 0.1, "c": 200.3 + i * 0.1} for i in range(30)]
atr = forecast.atr14(tage_t)
pruefe(atr is not None and abs(atr - 1.2) < 0.01, f"ATR plausibel ({atr:.3f})")
pruefe(forecast.trend(tage_t) == 1, "Trendregel erkennt Aufwärtstrend")
p = forecast.prognose(210.0, 1.5, 1, {"h": 210.6, "l": 209.1})
pruefe(abs(p["range_high"] - 210.78) < 1e-6 and abs(p["exp_low"] - 209.49) < 1e-6 and p["atr_pips"] == 150, "Prognose-Formel (±0,52 / ±0,34 ATR)")

print("Morgenreport")
rc = report.main(senden=True, ausgeben=False)
con = common.db()
rep = dict(con.execute("SELECT * FROM reports").fetchone())
text = GESENDET[-1]
print("\n" + "\n".join("    " + z for z in text.splitlines()) + f"\n    [{len(text)} Zeichen]\n")
pruefe(rc == 0 and rep["sent"] == 1, "Report erstellt und gesendet")
pruefe(len(text) <= 1500, f"Nachricht ≤ 1500 Zeichen ({len(text)})")
for teil in ("LAGE", "HEUTE", "PROGNOSE", "BIAS", "RISIKEN", "Trefferquote 30T", "Schluss (68 %)", "Erw. Hoch/Tief", "Trend:", "News:", "SETUP (London"):
    pruefe(teil in text, f"enthält {teil}")
pruefe("Bank of England" in text and "Fehlt:" in text, "fehlende Quelle (BoE-Feed) wird genannt")
pruefe(rep["range_low"] < rep["price"] < rep["range_high"], "Kurs liegt in der Spanne")
anzahl = len(GESENDET)
report.main(senden=True)
pruefe(len(GESENDET) == anzahl, "kein Doppelversand am selben Tag")

print("Auswertung")
heute = rep["date"]
alt = (date.fromisoformat(heute) - timedelta(days=1))
while alt.weekday() >= 5:
    alt -= timedelta(days=1)
con.execute("INSERT INTO reports(date, created, price, prev_close, sigma_pips, range_low, range_high, target, bias, bias_score, "
            "message, missing, llm_ok, sent) SELECT ?, created, price, prev_close, sigma_pips, range_low, range_high, target, "
            "bias, bias_score, message, missing, llm_ok, 1 FROM reports WHERE date=?", (alt.isoformat(), heute))
con.commit()
evaluate.main()
e = con.execute("SELECT * FROM evaluations").fetchall()
pruefe(len(e) == 1 and e[0]["date"] == alt.isoformat(), "Vortag ausgewertet, heutiger (offener) Tag nicht")
pruefe(e[0]["deviation_pips"] >= 0 and e[0]["in_range"] in (0, 1), "Auswertungsfelder gefüllt")

print("Refresh + Export")
refresh.main()
d = json.loads((OUT / "public" / "dashboard.json").read_text())
pruefe(d["report"]["news_score"] == 1 and d["report"]["exp"][0] and len(d["calendar"]) >= 3, "dashboard.json mit Report und Terminen")
pruefe(d["setup_historie"]["setups"]["london_trend"]["live"], "Backtest-Historie im Export")
pruefe(d["yields"]["uk30"]["value"] is not None and d["yields"]["jp2"]["value"] is not None, "Renditen inkl. Gilt 30y / JGB 2y")
pruefe(len(d["spread"]) > 10, "Zinsdifferenz-Reihe")
pruefe(len(d["news"]) >= 3 and all(n["note"] for n in d["news"]), "News eingeordnet")
pruefe(d["stats"]["q30"]["n"] == 1, "Trefferstatistik")
a = json.loads((OUT / "public" / "archive.json").read_text())
pruefe(len(a["reports"]) == 2, "Archiv mit 2 Reports")

# Live-Datei wie vom Alarm-Dienst
bars = {}
start = d["fx_window"][0]
for p, basis in (("gbpjpy", rep["price"]), ("usdjpy", 151.2), ("gbpusd", 1.379)):
    k, pts = basis, []
    for i in range(int((min(JETZT.timestamp(), d["fx_window"][1]) - start) // 300)):
        k += random.gauss(0, basis * 0.00015)
        pts.append([int(start + i * 300), round(k, 5)])
    bars[p] = {"price": pts[-1][1] if pts else basis, "ts": JETZT.timestamp(), "bars": pts}
common.schreibe_json("live.json", {"updated": JETZT.timestamp(), "market_closed": False, "pairs": bars, "tiingo_mb_month": 12.3})

print("Tages-Setup")
from datetime import datetime as DT, time as T
tag = "2026-10-14"
def kz(h, m, o, hi, lo, c):
    return (DT(2026, 10, 14, h, m) if h < 23 else DT(2026, 10, 13, h, m), o, hi, lo, c)
asien = [kz(23, 0, 209.50, 209.55, 209.45, 209.50)] + [kz(h, 0, 209.50, 209.80, 209.30, 209.60) for h in range(1, 9)]
vortage = [{"o": 205 + i * 0.15, "h": 206.2 + i * 0.15, "l": 204.8 + i * 0.15, "c": 205.6 + i * 0.15} for i in range(40)]
ctx = strategien.Kontext(tag, 209.5, forecast.atr14(vortage), vortage[-1], forecast.ema20(vortage), vortage[-1]["c"] - vortage[-6]["c"], asien)
sd = setup.berechnen(ctx)
pruefe(sd["status"] == "aktiv" and len(sd["orders"]) == 1 and sd["orders"][0]["richtung"] == 1, "Aufwärtstrend -> nur Long-Setup")
o = sd["orders"][0]
pruefe(abs(o["entry"] - 209.82) < 1e-6 and abs(o["sl"] - 209.28) < 1e-6 and abs(o["tp"] - 210.90) < 1e-6, f"Levels: Entry {o['entry']} SL {o['sl']} TP {o['tp']} (CRV 1:2)")
txt = setup.text_setup(sd, setup.historie())
print("\n" + "\n".join("    " + z for z in txt.splitlines()) + "\n")
breit = strategien.Kontext(tag, 209.5, 0.5, vortage[-1], forecast.ema20(vortage), 1.0, asien)
pruefe(setup.berechnen(breit)["status"] == "kein_setup" and "zu breit" in setup.berechnen(breit)["grund"], "zu breite Asien-Spanne -> kein Setup")
# Live-Verfolgung: wartet -> ausgelöst -> TP
verlauf = asien + [kz(9, 0, 209.60, 209.75, 209.55, 209.70)]
st, tr = setup.zustand(sd, verlauf, DT(2026, 10, 14, 9, 10))
pruefe(st == "wartet" and tr is None, "vor dem Ausbruch: wartet")
verlauf += [kz(9, 30, 209.70, 209.90, 209.65, 209.88)]
st2, tr = setup.zustand(sd, verlauf, DT(2026, 10, 14, 9, 35))
m1 = setup.meldung(st, st2, tr)
pruefe(st2 == "ausgelöst" and "Setup ausgelöst: LONG 209,820" in m1, "Ausbruch: ausgelöst + Meldung")
verlauf += [kz(11, 0, 210.40, 210.95, 210.35, 210.90)]
st3, tr = setup.zustand(sd, verlauf, DT(2026, 10, 14, 11, 5))
m2 = setup.meldung(st2, st3, tr)
pruefe(st3 == "tp" and "TP erreicht" in m2 and "+108 Pips" in m2, f"TP erreicht: {m2.splitlines()[-1]}")
st4, _ = setup.zustand(sd, asien + [kz(12, 30, 209.6, 209.7, 209.5, 209.6)], DT(2026, 10, 14, 12, 30))
pruefe(st4 == "verfallen", "kein Ausbruch bis 12:00 -> verfallen")
verlauf_sl = asien + [kz(9, 30, 209.70, 209.90, 209.65, 209.85), kz(10, 0, 209.80, 209.82, 209.20, 209.25)]
st5, tr = setup.zustand(sd, verlauf_sl, DT(2026, 10, 14, 10, 5))
pruefe(st5 == "sl" and tr.pips < 0 and "SL erreicht" in setup.meldung("ausgelöst", st5, tr), "SL erreicht")

print(f"\nTiingo-Abrufe: {sum('tiingo' in u for u in AUFRUFE)}, Claude-Aufrufe: {sum(u.startswith('claude:') for u in AUFRUFE)}")
print(f"Ausgabe: {OUT}")
print("ALLE TESTS OK" if not FEHLER else f"{len(FEHLER)} FEHLER: {FEHLER}")
sys.exit(1 if FEHLER else 0)
