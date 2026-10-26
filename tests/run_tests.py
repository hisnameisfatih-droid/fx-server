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
import sqlite3
import sys
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="fxtest-"))
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "env").write_text("TIINGO_API_KEY=t\nWHATSAPP_PHONE=+4915100000000\nCALLMEBOT_APIKEY=1\n"
                         "ANTHROPIC_API_KEY=sk-test\nDASHBOARD_URL=https://1-2-3-4.sslip.io/geheim123/\n")
os.environ.update(FX_ENV_FILE=str(OUT / "env"), FX_DATA_DIR=str(OUT), FX_LOG_DIR=str(OUT / "logs"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxserver import common, evaluate, export, forecast, llm, refresh, report, setup, strategien, treiber  # noqa: E402
from fxserver.sources import calendar as cal_src, markt as markt_src, news as news_src, prices as px_src, yields as y_src  # noqa: E402

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
    """Wie die echte BoE-Datei: erst 'spot, short end' (Monate), dann 'spot curve' (Jahre)."""
    from openpyxl import Workbook
    wb = Workbook()
    kurz = wb.active
    kurz.title = "3. spot, short end"
    kurz.append(["UK nominal spot curve, short end"])
    kurz.append([])
    kurz.append(["months:"] + list(range(1, 61)))
    ws = wb.create_sheet("4. spot curve")
    ws.append(["UK nominal spot curve"])
    ws.append([])
    ws.append(["years:"] + [0.5 * i for i in range(1, 81)])
    d = date.today() - timedelta(days=40)
    while d <= date.today() - timedelta(days=1):
        if d.weekday() < 5:
            ws.append([datetime(d.year, d.month, d.day)] + [3.8 + 0.02 * i / 2 + random.gauss(0, 0.02) for i in range(1, 81)])
            kurz.append([datetime(d.year, d.month, d.day)] + [3.7 + 0.002 * m + 0.001 * (d.toordinal() % 5) for m in range(1, 61)])
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


def yahoo_json(symbol):
    basis, schritt, off = {"^VIX": (17.0, 0.08, -14400), "^N225": (47000.0, 90.0, 32400), "BZ=F": (118.0, 0.5, -14400)}[symbol]
    ts, closes, k = [], [], basis
    d = date.today() - timedelta(days=70)
    while d <= date.today():
        if d.weekday() < 5:
            k += schritt * (1 if d.toordinal() % 3 else -1.5)
            ts.append(int(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).timestamp()))
            closes.append(round(k, 2))
        d += timedelta(days=1)
    letzter = closes[-1]
    closes[-3] = None   # Lücken kommen bei Yahoo vor
    return {"chart": {"result": [{"meta": {"symbol": symbol, "gmtoffset": off, "regularMarketPrice": letzter,
                                           "regularMarketTime": ts[-1] + 600},
                                  "timestamp": ts, "indicators": {"quote": [{"close": closes}]}}], "error": None}}


def cftc_json(code):
    letzter_di = date.today() - timedelta(days=(date.today().weekday() - 1) % 7)
    zeilen = []
    for i in range(60):
        netto = -70000 + 400 * i if code == "097741" else 25000   # Yen: 35 % des OI gegen den Yen (Extrem)
        lang, kurz = (60000 + netto, 60000) if netto > 0 else (60000, 60000 - netto)
        zeilen.append({"Report_Date_as_YYYY_MM_DD": f"{letzter_di - timedelta(weeks=i)}T00:00:00.000",
                       "cftc_contract_market_code": code, "open_interest_all": "200000",
                       "noncomm_positions_long_all": str(lang), "noncomm_positions_short_all": str(kurz)})
    return zeilen


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
    if "finance.yahoo.com/v8/finance/chart/" in url:
        return Antwort(daten=yahoo_json(unquote(url.split("/chart/")[1].split("?")[0])))
    if "publicreporting.cftc.gov" in url:
        return Antwort(daten=cftc_json(params["cftc_contract_market_code"]))
    if "fred.stlouisfed.org" in url or "cdn.cboe.com" in url:
        raise RuntimeError("Ersatzquelle im Test nicht nötig")
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
for modul in (cal_src, markt_src, news_src, px_src, y_src):
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

print("Nachrichtenkanal")


class FakeSession:
    def __init__(self):
        self.posts = []

    def get(self, url, **kw):
        class R:
            status_code = 200
            text = "{}"
            def json(self):  # noqa: E306
                return {"ok": True, "result": [{"message": {"chat": {"id": 4711, "type": "private"}}}]}
        return R()

    def post(self, url, json=None, **kw):
        self.posts.append((url, json))
        class R:
            status_code = 200
            text = "{}"
            def json(self):  # noqa: E306
                return {"ok": True}
        return R()


_alt_session, _alt_token = common._session, common.TELEGRAM_BOT_TOKEN
common._session, common.TELEGRAM_BOT_TOKEN = FakeSession(), "123:abc"
ok_tg = common.whatsapp("Test über Telegram")
pruefe(ok_tg and common._session.posts and common._session.posts[0][1]["chat_id"] == "4711"
       and (OUT / "telegram_chat_id").read_text() == "4711", "Telegram zuerst, Chat-ID automatisch erkannt und gemerkt")
common._session, common.TELEGRAM_BOT_TOKEN = _alt_session, _alt_token
(OUT / "telegram_chat_id").unlink()

print("Treiber: Quellen")
boe = y_src.parse_boe_zip(BOE_ZIP)
letzter_tag = boe[max(boe)]
pruefe(all(k in letzter_tag for k in ("uk2", "uk10", "uk30")), "BoE-Zip: Gilt 2y/10y/30y trotz 'short end'-Blatt davor")
pruefe(abs(letzter_tag["uk2"] - (3.7 + 0.002 * 24 + 0.001 * (date.fromisoformat(max(boe)).toordinal() % 5))) < 1e-9, "Gilt 2y aus dem Monatsblatt (24 Monate)")
pruefe(all("us2" in v and "us10" in v for v in y_src.parse_treasury(treasury_csv()).values()), "Treasury: US 2y + 10y")
yw, yl = markt_src.parse_yahoo(yahoo_json("^N225"))
pruefe(len(yw) > 40 and yl and yw[max(yw)] == yl["wert"], "Yahoo: Tagesschlüsse + laufender Kurs (Börsenzeit)")
pruefe(markt_src.parse_fred("observation_date,VIXCLS\n2026-10-08,16.2\n2026-10-09,.\n2026-10-12,\n2026-10-13,18.4\n") == {"2026-10-08": 16.2, "2026-10-13": 18.4}
       and markt_src.parse_fred("DATE,X\n2026-10-08,1.5\n") == {"2026-10-08": 1.5}, "FRED-CSV (neuer/alter Kopf, Lücken)")
pruefe(markt_src.parse_cboe("DATE,OPEN,HIGH,LOW,CLOSE\n10/12/2026,17.1,18.0,16.9,17.5\n") == {"2026-10-12": 17.5}, "CBOE-VIX-CSV")
cf = markt_src.parse_cftc(cftc_json("097741"))
pruefe(len(cf) == 60 and cf[max(cf)] == {"netto": -70000.0, "oi": 200000.0}, "CFTC: Netto-Position (Groß-/Kleinschreibung egal)")

print("Treiber: Logik")


def leere_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(common.SCHEMA)
    common._spalten_ergaenzen(c)
    return c


JT = datetime(2026, 10, 14, 10, 0, tzinfo=common.TZ_BERLIN)   # Mittwoch 10:00
HT = common.fx_tag(JT).isoformat()
cdb = leere_db()
for i, (t, k) in enumerate(zip(("2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"), (156.5, 157.5, 158.5, 159.5, 160.0))):
    cdb.execute("INSERT INTO fxdaily VALUES(?,?,?,?,?,?)", (t, "usdjpy", k, k, k, k))
iv = treiber.intervention(cdb, JT, HT, {"usdjpy": 160.4})
pruefe(iv["alarm"] and iv["stufe"] == 2.5 and iv["signal"] == -0.83, f"Intervention: über 160 + 2,5 % in 5 Tagen = Alarm ({iv['text']})")
for nid, titel in (("n1", "Japan's Mimura warns on excessive yen moves"), ("n2", "Yen intervention fears grow as USD/JPY tops 160"),
                   ("n3", "Fed's Powell sees no rush to cut")):
    cdb.execute("INSERT INTO news(id, ts, source, title, url, summary) VALUES(?,?,?,?,?,?)",
                (nid, (JT - timedelta(hours=5)).astimezone(timezone.utc).isoformat(), "FXStreet", titel, "", ""))
iv2 = treiber.intervention(cdb, JT, HT, {"usdjpy": 160.4})
pruefe(len(iv2["warnungen"]) == 2 and iv2["signal"] == -1.0 and "2 Warnungen" in iv2["text"], "Intervention: Warnungen aus den News (nur mit Yen-Bezug)")
pruefe(not treiber.intervention(cdb, JT, HT, {"usdjpy": 150.2})["alarm"], "Intervention: kein Alarm bei 150")
for t, v in (("2026-10-09", 18.6), ("2026-10-12", 19.0), ("2026-10-13", 24.5)):
    cdb.execute("INSERT INTO markt VALUES(?,?,?)", (t, "vix", v))
vx = treiber.vix(cdb, JT)
pruefe(vx["alarm"] and vx["signal"] == -1.0, f"VIX-Sprung +29 % = Alarm ({vx['text']})")
pruefe([treiber.stufe_aus_score(s, v) for s, v in ((27, 2), (24, 2), (17, 0), (21, 0), (-20, 1), (10, 2), (None, 1))] == [2, 1, 0, 1, -1, 0, 0],
       "Empfehlungsstufen mit 5 Punkten Puffer")
e1 = treiber.empfehlung(12, 0, {"alarm": True}, False, None)
e2 = treiber.empfehlung(-45, -2, {"alarm": True}, True, None, setup_trend=1)
e3 = treiber.empfehlung(35, 2, {"alarm": False}, False, {"ts": (JT + timedelta(hours=2)).isoformat(), "kurz": "BoJ", "faktor": 2.02})
pruefe(e1["code"] == "long_meiden" and e2["code"] == "short" and e2["risiko"] == "halbiert"
       and any("widerspricht" in h for h in e2["hinweise"]) and e3["code"] == "abwarten" and "2,0×" in e3["text"],
       "Empfehlung: Long meiden / Short + Hinweise / Abwarten vor BoJ")
cdb.execute("INSERT INTO calendar(id, ts, currency, impact, title) VALUES(?,?,?,?,?)", ("b1", (JT + timedelta(hours=2)).isoformat(), "JPY", "high", "BOJ Policy Rate"))
cdb.execute("INSERT INTO calendar(id, ts, currency, impact, title) VALUES(?,?,?,?,?)", ("b2", (JT + timedelta(hours=2)).isoformat(), "JPY", "high", "Monetary Policy Statement"))
tm = treiber.termine(cdb, JT, 210.0)
pruefe(len(tm) == 1 and tm[0]["typ"] == "BoJ-Zinsentscheid" and tm[0]["faktor"] == 2.02 and tm[0]["pips"] == 180,
       f"Termine: BoJ einmal, Faktor 2,0, ~180 Pips ({tm[0]['pips'] if tm else '-'})")
tb_wenig = treiber.berechnen(cdb, JT, kurse={"usdjpy": 160.4, "gbpjpy": 210.0})
pruefe(tb_wenig["score"] is None and tb_wenig["empfehlung"]["code"] == "keine" and tb_wenig["abdeckung"]["gewicht"] == 30,
       "zu wenige Treiber (30 % Gewicht) -> KEINE DATEN")
for i in range(7):
    t = (date(2026, 10, 5) + timedelta(days=i)).isoformat()
    cdb.execute("INSERT INTO yields VALUES(?,?,?)", (t, "jp2", 1.30 + 0.02 * i))     # +10 bp in 5 Tagen
    cdb.execute("INSERT INTO yields VALUES(?,?,?)", (t, "us10", 4.40))
tb_v = treiber.berechnen(cdb, JT, kurse={"usdjpy": 160.4, "gbpjpy": 210.0}, vorher=0)
pruefe(tb_v["score"] is not None and tb_v["score"] <= -30 and tb_v["empfehlung"]["code"] == "abwarten"
       and len(tb_v["items"]) == 10, f"Score {tb_v['score']} mit 4 Treibern, BoJ in 2 Std -> ABWARTEN")
pruefe("EMPFEHLUNG: 🟡 ABWARTEN" in treiber.whatsapp_block(tb_v) and "▼ Intervention" in treiber.whatsapp_block(tb_v), "WhatsApp-Block")
gesendet_t = []
ohne_alarm = {**tb_v["intervention"], "alarm": False}
neutral = {**tb_v, "score": 0, "stufe": 0, "intervention": ohne_alarm, "empfehlung": treiber.empfehlung(0, 0, ohne_alarm, False, None)}
short = {**neutral, "score": -40, "stufe": -2, "empfehlung": treiber.empfehlung(-40, -2, ohne_alarm, False, None)}
alarm = {**neutral, "intervention": {**ohne_alarm, "alarm": True}, "empfehlung": treiber.empfehlung(0, 0, {"alarm": True}, False, None)}
pruefe(treiber.melden(cdb, neutral, JT, senden=lambda t: gesendet_t.append(t) or True) is None, "Treiber-Meldung: erster Lauf merkt sich nur den Stand")
m1 = treiber.melden(cdb, short, JT, senden=lambda t: gesendet_t.append(t) or True)
m2 = treiber.melden(cdb, neutral, JT + timedelta(minutes=30), senden=lambda t: gesendet_t.append(t) or True)
m3 = treiber.melden(cdb, alarm, JT + timedelta(minutes=31), senden=lambda t: gesendet_t.append(t) or True)
pruefe(m1 and "🔴 SHORT BEVORZUGT (Score -40)" in m1 and m2 is None and m3 and "Interventionsgefahr" in m3
       and "LONG MEIDEN" in m3 and len(gesendet_t) == 2, "Treiber-Meldung: Wechsel, Sperrzeit, Interventionsalarm")
print("    " + m1.replace("\n", "\n    "))

print("Morgenreport")
rc = report.main(senden=True, ausgeben=False)
con = common.db()
rep = dict(con.execute("SELECT * FROM reports").fetchone())
text = GESENDET[-1]
print("\n" + "\n".join("    " + z for z in text.splitlines()) + f"\n    [{len(text)} Zeichen]\n")
pruefe(rc == 0 and rep["sent"] == 1, "Report erstellt und gesendet")
pruefe(len(text) <= 1500, f"Nachricht ≤ 1500 Zeichen ({len(text)})")
for teil in ("LAGE", "HEUTE", "PROGNOSE", "BIAS", "RISIKEN", "Trefferquote 30T", "Schluss (68 %)", "Erw. Hoch/Tief", "Trend:", "News:", "SETUP (London",
             "EMPFEHLUNG:"):
    pruefe(teil in text, f"enthält {teil}")
pruefe(rep["treiber_score"] is not None and rep["treiber_ampel"], f"Treiber-Score im Report gespeichert ({rep['treiber_score']}, {rep['treiber_ampel']})")
pruefe(json.loads(rep["data_json"])["roh"]["treiber"]["werte"][0]["treiber"].startswith("Interventionsrisiko"), "Treiber-Werte gehen an Claude")
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
            "message, missing, llm_ok, sent, treiber_score) SELECT ?, created, price, prev_close, sigma_pips, range_low, range_high, target, "
            "bias, bias_score, message, missing, llm_ok, 1, 40 FROM reports WHERE date=?", (alt.isoformat(), heute))
con.commit()
evaluate.main()
e = con.execute("SELECT * FROM evaluations").fetchall()
pruefe(len(e) == 1 and e[0]["date"] == alt.isoformat(), "Vortag ausgewertet, heutiger (offener) Tag nicht")
pruefe(e[0]["deviation_pips"] >= 0 and e[0]["in_range"] in (0, 1), "Auswertungsfelder gefüllt")
pruefe(e[0]["treiber_hit"] in (0, 1), "Treiber-Treffer (1 Tag) ausgewertet")
fx_tage_alle = [r["date"] for r in con.execute("SELECT date FROM fxdaily WHERE pair='gbpjpy' ORDER BY date")]
alt10 = fx_tage_alle[-11]
con.execute("INSERT INTO reports(date, created, price, treiber_score, treiber_ampel, message, missing, llm_ok, sent) "
            "VALUES(?, ?, 209.0, -35, 'SHORT BEVORZUGT', 'x', '[]', 1, 1)", (alt10, alt10))
con.execute("INSERT INTO evaluations(date, close, evaluated) VALUES(?, 209.0, 'x')", (alt10,))
con.commit()
pruefe(evaluate.treiber_5_tage(con) >= 1 and con.execute("SELECT treiber_hit5 FROM evaluations WHERE date=?", (alt10,)).fetchone()[0] in (0, 1),
       "Treiber-Treffer über 5 Tage nachgemessen")
con.execute("DELETE FROM evaluations WHERE date=?", (alt10,))
con.execute("DELETE FROM reports WHERE date=?", (alt10,))
con.commit()

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
tb = d["treiber"]
pruefe(tb and len(tb["items"]) == 10 and tb["abdeckung"]["n"] == 10, f"Treiber: alle 10 verfügbar ({tb['abdeckung'] if tb else '-'}, fehlend {tb['fehlend'] if tb else '-'})")
pruefe(tb["score"] is not None and -100 <= tb["score"] <= 100 and tb["empfehlung"]["titel"], f"Treiber-Score {tb['score']} -> {tb['empfehlung']['titel']}")
pruefe(sum(i["gewicht"] for i in tb["items"]) == 100 and all(i["warum"] and i["quelle"] for i in tb["items"]), "Gewichte = 100, jede Begründung vorhanden")
pruefe(next(i for i in tb["items"] if i["id"] == "cftc")["signal"] == -0.7, "CFTC: 35 % gegen den Yen = Squeeze-Warnung")
pruefe(d["yields"]["uk2"]["value"] is not None and d["yields"]["us2"]["value"] is not None, "Renditen inkl. Gilt 2y / US 2y")
pruefe(d["treiber_studie"]["termine"][0]["name"] == "BoJ-Zinsentscheid" and d["treiber_studie"]["zerlegung"]["yen_pct"] == 77, "Studie im Export")
pruefe(len(d["treiber_verlauf"]) == 2 and d["report"]["treiber"]["score"] is not None, "Treiber-Verlauf im Export")
pruefe(con.execute("SELECT COUNT(DISTINCT series) FROM markt").fetchone()[0] == 7, "Markt-Tabelle: VIX, Nikkei, Brent + 4 CFTC-Reihen")
print(f"    Treiber jetzt: {treiber.whatsapp_block(tb)}".replace("\n", "\n    "))

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

print("AMD-Muster")
from fxserver import amd as amd_mod  # noqa: E402
TAG_AMD = common.fx_tag(datetime.now(common.TZ_BERLIN))


def k15(hhmm, o, h, l, c, tag=TAG_AMD):
    hh, mm = map(int, hhmm.split(":"))
    return (DT(tag.year, tag.month, tag.day, hh, mm), o, h, l, c)


def asien(lo=209.80, hi=210.25):
    ks = []
    for i in range(32):   # 01:00–09:00, pendelt in der Spanne
        t = f"{1 + i // 4:02d}:{(i % 4) * 15:02d}"
        m = lo + (hi - lo) * (0.5 + 0.45 * math.sin(i / 2.5))
        ks.append(k15(t, m, min(hi, m + 0.06), max(lo, m - 0.06), m + 0.01))
    ks[5] = k15("02:15", 210.2, hi, 210.15, 210.2)
    ks[20] = k15("06:00", 209.85, 209.9, lo, 209.86)
    return ks


bear = asien() + [k15("09:00", 210.20, 210.33, 210.15, 210.28), k15("09:15", 210.28, 210.30, 210.10, 210.12),
                  k15("09:30", 210.12, 210.14, 209.90, 209.95), k15("09:45", 209.95, 209.97, 209.70, 209.72)]
lon = strategien.AMD_VARIANTEN[0]
e_bear = strategien.amd_erkennen(bear, 1.0, lon)
pruefe(e_bear["phase"] == "signal" and e_bear["richtung"] == -1 and e_bear["entry"] == 210.12 and e_bear["sl"] == 210.38
       and e_bear["tp"] == 209.80 and e_bear["sweep_zeit"] == "09:00", f"AMD bärisch: Fehlausbruch über Asien-Hoch -> SHORT ({e_bear.get('entry')}/{e_bear.get('sl')}/{e_bear.get('tp')})")
e_m = strategien.amd_erkennen(bear[:33], 1.0, lon)
pruefe(e_m["phase"] == "manipulation" and e_m["sweep_seite"] == 1, "AMD: nach dem Fehlausbruch erst Verdacht (Manipulation)")
echt = asien() + [k15("09:00", 210.2, 210.70, 210.15, 210.65), k15("09:15", 210.6, 210.7, 210.1, 210.12)]
pruefe(strategien.amd_erkennen(echt, 1.0, lon)["phase"] == "kein", "AMD: 45 Pips drüber = echter Ausbruch, kein Signal")
tr_bear = strategien.simuliere(strategien.amd(strategien.Kontext("x", 210, 1.0, {}, None, None, bear), lon), bear, 0, "x")
pruefe(tr_bear and tr_bear.grund == "tp" and tr_bear.richtung == -1, "AMD-Backtest-Simulation: Ziel erreicht")
# Donnerstag-Muster: London-Spanne, New York läuft unter das Tief und dreht
ny = strategien.AMD_VARIANTEN[1]
lond = [k15(f"{9 + i // 4:02d}:{(i % 4) * 15:02d}", 209.25 + 0.2 * math.sin(i / 2), 209.55 if i == 7 else 209.25 + 0.2 * math.sin(i / 2) + 0.05,
            208.90 if i == 12 else 209.25 + 0.2 * math.sin(i / 2) - 0.05, 209.25 + 0.2 * math.sin(i / 2)) for i in range(20)]
bull = lond + [k15("14:00", 209.0, 209.02, 208.84, 208.86), k15("14:15", 208.86, 209.02, 208.85, 209.00),
               k15("14:30", 209.0, 209.40, 208.98, 209.35), k15("14:45", 209.35, 209.70, 209.30, 209.65)]
e_bull = strategien.amd_erkennen(bull, 1.0, ny)
pruefe(e_bull["phase"] == "signal" and e_bull["richtung"] == 1 and e_bull["sl"] == 208.79 and e_bull["tp"] == 209.55,
       f"AMD bullisch (London → New York): LONG {e_bull.get('entry')} SL {e_bull.get('sl')} TP {e_bull.get('tp')}")
# Live: Verdacht -> Signal -> Ziel, jeweils eine Meldung
amd_txt = []
jetzt_amd = datetime(TAG_AMD.year, TAG_AMD.month, TAG_AMD.day, 9, 16, tzinfo=common.TZ_BERLIN)
m1 = amd_mod.pruefen(con, jetzt_amd, senden=amd_txt.append, kerzen_5=bear[:33])
m2 = amd_mod.pruefen(con, jetzt_amd + timedelta(minutes=15), senden=amd_txt.append, treiber_stufe=-2, kerzen_5=bear[:34])
m3 = amd_mod.pruefen(con, jetzt_amd + timedelta(minutes=16), senden=amd_txt.append, kerzen_5=bear[:34])
m4 = amd_mod.pruefen(con, jetzt_amd + timedelta(minutes=45), senden=amd_txt.append, kerzen_5=bear)
pruefe(len(m1) == 1 and "AMD-Verdacht" in m1[0] and len(m2) == 1 and "AMD-Signal SHORT" in m2[0] and "Treiber stützen" in m2[0]
       and m3 == [] and len(m4) == 1 and "Ziel erreicht" in m4[0], f"AMD live: Verdacht, Signal, Ziel ({[x.splitlines()[0] for x in amd_txt]})")
print("\n    " + m2[0].replace("\n", "\n    ") + "\n")
export.alles(con)
da = json.loads((OUT / "public" / "dashboard.json").read_text())["amd"]
pruefe(da["varianten"][0]["phase"] == "signal" and da["varianten"][0]["state"] == "tp" and da["varianten"][0]["daten"]["tp"] == 209.8,
       "AMD im Dashboard-Export")

print("3-6-9 Time Map")
from datetime import time as TM  # noqa: E402
z369_b = next(p for p in strategien.Z369_VARIANTEN if p["id"] == "z369_berlin_retrace_2r")
TAG369 = "2026-10-21"


def k5(hhmm, o, h, l, c):
    hh, mm = map(int, hhmm.split(":"))
    return (DT(2026, 10, 21, hh, mm), o, h, l, c)


akk = [k5(f"08:{m:02d}", 210.10, 210.20, 210.00, 210.12 if m % 10 else 210.08) for m in range(10, 40, 5)]
manip = [k5("08:40", 210.12, 210.18, 210.08, 210.15), k5("08:45", 210.15, 210.24, 210.12, 210.22),
         k5("08:50", 210.22, 210.30, 210.15, 210.17)]                      # Sweep über 210,20 bis 210,30
vert = [k5("08:55", 210.17, 210.19, 210.08, 210.10), k5("09:00", 210.10, 210.12, 209.88, 209.90),   # kräftig unter MSS
        k5("09:05", 209.90, 210.10, 209.86, 210.05), k5("09:10", 210.05, 210.12, 209.95, 209.97),
        k5("09:15", 209.97, 209.99, 209.60, 209.62), k5("09:20", 209.62, 209.64, 209.40, 209.42)]
e369 = strategien.z369_erkennen(akk + manip + vert, 1.0, z369_b, TAG369)
pruefe(e369["phase"] == "signal" and e369["richtung"] == -1 and e369["mss"] == 210.0 and e369["signal_zeit"] == "09:00"
       and e369["typ"] == "limit" and abs(e369["entry"] - 210.10) < 1e-9 and e369["sl"] == 210.32,
       f"3-6-9: Sweep über Hoch, Strukturbruch 09:00 -> SHORT-Limit {e369.get('entry')} SL {e369.get('sl')} TP {e369.get('tp')}")
tr369 = strategien.simuliere(strategien.z369_setup(e369, z369_b), akk + manip + vert, 0, TAG369)
pruefe(tr369 and tr369.grund == "tp" and tr369.entry == 210.10, "3-6-9: Limit im Rücklauf gefüllt, Ziel 2R erreicht")
pruefe(strategien.z369_erkennen(akk + manip[:2], 1.0, z369_b, TAG369)["phase"] == "manipulation", "3-6-9: nach Sweep erst Verdacht")
ny = strategien.z369_zeiten("2026-10-21", "America/New_York")
pruefe(ny[0] == TM(14, 10) and strategien.z369_zeiten("2026-11-03", "America/New_York")[0] == TM(14, 10)
       and strategien.z369_zeiten("2026-10-28", "America/New_York")[0] == TM(13, 10), "3-6-9: New-York-Zeit inkl. Zeitumstellungs-Woche")
pruefe(amd_mod.bewaehrt("z369_berlin_retrace_2r")[0] is False, "ohne positiven Backtest: keine WhatsApp, nur Dashboard")

print("Gartley")
from fxserver import harmonisch  # noqa: E402
pruefe(harmonisch.muster_pruefen((199.0, -1), (201.0, 1), (199.764, -1), (200.528, 1)) is not None
       and harmonisch.muster_pruefen((199.0, -1), (201.0, 1), (200.5, -1), (200.8, 1)) is None, "Gartley-Verhältnisse (AB 61,8 %, D 78,6 %)")
punkte = [200.4, 199.0, 201.0, 199.764, 200.528, 199.428, 200.8]
gk, t0, k = [], DT(2026, 3, 2, 0, 0), punkte[0]
for von, bis in zip(punkte, punkte[1:]):
    for j in range(40):
        neu = von + (bis - von) * (j + 1) / 40
        t_k = t0 + timedelta(minutes=15 * len(gk))
        gk.append((t_k.date().isoformat(), t_k, k, max(k, neu) + 0.01, min(k, neu) - 0.01, neu))
        k = neu
tr_g = harmonisch.simuliere_gartley(gk, {"id": "g", "tf_min": 15, "k_atr": 2, "tp_anteil": 0.382}, 3.0)
pruefe(len(tr_g) == 1 and tr_g[0].richtung == 1 and tr_g[0].grund == "tp" and abs(tr_g[0].entry - 199.428) < 0.02,
       f"Gartley bullisch erkannt, Entry bei D, Ziel erreicht ({[(x.entry, x.grund, x.pips) for x in tr_g]})")

print(f"\nTiingo-Abrufe: {sum('tiingo' in u for u in AUFRUFE)}, Claude-Aufrufe: {sum(u.startswith('claude:') for u in AUFRUFE)}")
print(f"Ausgabe: {OUT}")
# --- Zeitumstellung: Woche, in der Europa schon Winterzeit hat, die USA noch Sommerzeit ---
# FX-Tag Di 27.10.2026 beginnt Mo 26.10. 17:00 New York = 22:00 Berlin (sonst 23:00).
from datetime import datetime as _dt, time as _time, timedelta as _td
from fxserver.common import TZ_BERLIN as _TZB, fx_tag_start as _fxs
from datetime import date as _date
_s = _fxs(_date(2026, 10, 27)).astimezone(_TZB)
pruefe((_s.hour, _s.minute) == (22, 0), "Zeitumstellung: FX-Tag 27.10.2026 beginnt 22:00 Berlin")
_s = _s.replace(tzinfo=None)
pruefe(strategien.tm(_s) == -60 and strategien.tm(_s + _td(minutes=45)) == -15, "Zeitumstellung: 22:xx im neuen Tag liegt vor Tagesbeginn")
pruefe(strategien.tm(_dt(2026, 10, 20, 22, 30)) == 1410, "Normale Woche: 22:30 Berlin ist Tagesende")
pruefe(strategien.tm(_dt(2026, 11, 3, 22, 30)) == 1410, "Nach US-Umstellung: 22:30 Berlin wieder Tagesende")
_k = [(_s + _td(minutes=15 * i), 210.0, 210.1, 209.9, 210.0) for i in range(4 * 20)]
_k[44] = (_k[44][0], 210.0, 210.6, 209.9, 210.5)   # 09:00 Berlin: Ausbruch nach oben
pruefe(_k[44][0].time() == _time(9, 0), "Zeitumstellung: Testkerze liegt um 09:00")
_set = strategien.Setup("t", "t", "stop", [strategien.Order(1, 210.3, 209.5, 211.9)], _time(9, 0), _time(12, 0), _time(21, 0))
_tr = strategien.simuliere(_set, _k, 0, "2026-10-27")
pruefe(_tr is not None and _tr.t_entry == _k[44][0], "Zeitumstellung: Setup löst trotz Tagesbeginn 22:00 aus")

print("ALLE TESTS OK" if not FEHLER else f"{len(FEHLER)} FEHLER: {FEHLER}")
sys.exit(1 if FEHLER else 0)
