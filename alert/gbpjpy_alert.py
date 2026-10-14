#!/usr/bin/env python3
"""
GBP/JPY 5-Minuten-Alarm per WhatsApp (CallMeBot)

Holt GBP/JPY-Kurse live über WebSocket (Tiingo, optional Finnhub als Reserve),
baut Kerzen (z. B. 1 und 5 Min) aus dem Mid-Preis und schickt sofort eine WhatsApp, sobald
der aktuelle Preis eine Schwelle aus REGELN über oder unter dem Open der laufenden Kerze liegt.
Gemessen wird nur die Netto-Bewegung Open -> aktueller Preis (nicht High-Low).

Start:          python3 gbpjpy_alert.py
Test-Nachricht: python3 gbpjpy_alert.py --test
"""

# ============================================================
# EINSTELLUNGEN (hier anpassen)
# ============================================================
# Alarm-Regeln: Kerzenlänge in Minuten -> Schwellen (Netto-Bewegung Open -> aktueller Preis, in JPY)
# Kerzenlänge muss 60 glatt teilen (1, 2, 5, 10, 15, 30, 60). 0.100 = 10 Pips.
# Pro Kerze, Schwelle und Richtung höchstens ein Alarm.
REGELN = {
    1: [0.100],             # 1-Min-Kerze: ab 10 Pips
    5: [0.200, 0.300],      # 5-Min-Kerze: ab 20 Pips und nochmal ab 30 Pips
}
PIP_GROESSE = 0.01          # 1 Pip bei JPY-Paaren
ZEITZONE = "Europe/Berlin"  # Uhrzeit in den Nachrichten
PAAR_NAME = "GBP/JPY"
TIINGO_TICKER = "gbpjpy"
FINNHUB_SYMBOL = "OANDA:GBP_JPY"
# Live-Kurse fürs Dashboard (Alarme nur für TIINGO_TICKER)
LIVE_PAARE = {"gbpjpy": "OANDA:GBP_JPY", "usdjpy": "OANDA:USD_JPY", "gbpusd": "OANDA:GBP_USD"}
LIVE_SCHREIBEN_SEK = 30
TIINGO_THRESHOLD = 5        # 5 = alle Kursupdates

STATUS_AB_UHR = 8           # stündliche "Läuft"-Meldung ab dieser Uhrzeit (deutsche Zeit) ...
STATUS_BIS_UHR = 22         # ... bis einschließlich dieser Uhrzeit (STATUS_AB_UHR = -1 schaltet sie ab)
STATUS_AM_WOCHENENDE = False  # True = auch melden, wenn der Markt am Wochenende zu ist

HEARTBEAT_TIMEOUT_SEK = 90  # Tiingo schickt alle ~30 s einen Heartbeat; bei 90 s Stille -> Reconnect
STOERUNG_MELDEN_NACH_MIN = 15  # nur werktags: 1 WhatsApp, wenn so lange keine Kurse kommen (0 = nie)
LOGDATEI = "alert.log"
MIN_ABSTAND_1MIN_SEK = 300  # 1-Min-Alarme derselben Richtung höchstens alle 5 Min
LETZTER_ALARM: dict[int, float] = {}
# ============================================================

import argparse
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import websocket  # Paket: websocket-client
from dotenv import load_dotenv

BASIS_DIR = Path(__file__).resolve().parent
load_dotenv(os.getenv("FX_ENV_FILE", "/etc/fx-server/env"))
load_dotenv(BASIS_DIR / ".env")  # Fallback für lokale Tests
DATA_DIR = Path(os.getenv("FX_DATA_DIR", "/var/lib/fx-server"))
LOG_DIR = Path(os.getenv("FX_LOG_DIR", "/var/log/fx-server"))
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()

TIINGO_API_KEY = os.getenv("TIINGO_API_KEY", "").strip()
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE", "").strip().replace(" ", "")
CALLMEBOT_APIKEY = os.getenv("CALLMEBOT_APIKEY", "").strip()

TZ_LOKAL = ZoneInfo(ZEITZONE)
TZ_NY = ZoneInfo("America/New_York")

# ------------------------------------------------------------
# Logging: Datei (rotierend) + Konsole (landet bei systemd im Journal)
# ------------------------------------------------------------
log = logging.getLogger("gbpjpy")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _logpfad = LOG_DIR / LOGDATEI
except OSError:
    _logpfad = BASIS_DIR / LOGDATEI
_fh = RotatingFileHandler(_logpfad, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_fh)
log.addHandler(_sh)


# ------------------------------------------------------------
# Hilfsfunktionen
# ------------------------------------------------------------
def de_zahl(wert: float, stellen: int) -> str:
    """208.6 -> '208,600'"""
    return f"{wert:.{stellen}f}".replace(".", ",")


def jetzt_lokal() -> datetime:
    return datetime.now(TZ_LOKAL)


def parse_iso(s: str) -> float:
    """ISO-Zeit (auch mit Nanosekunden, 'Z' oder '+00:00') -> Unix-Sekunden."""
    s = s.strip().replace("Z", "+00:00")
    tz = "+00:00"
    if len(s) > 6 and s[-6] in "+-" and s[-3] == ":":
        s, tz = s[:-6], s[-6:]
    if "." in s:
        haupt, frac = s.split(".", 1)
        frac = (frac + "000000")[:6]
    else:
        haupt, frac = s, "000000"
    return datetime.fromisoformat(f"{haupt}.{frac}{tz}").timestamp()


def markt_geschlossen(ts: float | None = None) -> bool:
    """Forex-Wochenende: Fr 17:00 bis So 17:00 New Yorker Zeit (sommerzeitsicher)."""
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), TZ_NY)
    wd = dt.weekday()  # Mo=0 ... So=6
    if wd == 5:
        return True
    if wd == 4 and dt.hour >= 17:
        return True
    if wd == 6 and dt.hour < 17:
        return True
    return False


# ------------------------------------------------------------
# Telegram (bevorzugt, ohne Nachrichtenlimit) – Chat-ID teilt sich der Dienst mit fxserver
# ------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_FILE = DATA_DIR / "telegram_chat_id"


def telegram_chat_id() -> str | None:
    fest = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if fest:
        return fest
    try:
        gemerkt = TELEGRAM_CHAT_FILE.read_text().strip()
        if gemerkt:
            return gemerkt
    except OSError:
        pass
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", timeout=20)
        chats = [u.get("message", {}).get("chat", {}) for u in r.json().get("result", [])]
        privat = [c for c in chats if c.get("type") == "private" and c.get("id")]
        if privat:
            chat = str(privat[-1]["id"])
            try:
                TELEGRAM_CHAT_FILE.write_text(chat)
            except OSError:
                pass
            return chat
        log.warning("Telegram: noch keine Nachricht an den Bot – im Bot auf Start tippen")
    except (requests.RequestException, ValueError) as e:
        log.warning("Telegram getUpdates: %s", e)
    return None


def telegram_senden(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    chat = telegram_chat_id()
    if not chat:
        return False
    for i in range(1, 4):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", timeout=20,
                              json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
            if r.status_code == 200 and r.json().get("ok"):
                log.info("Telegram gesendet: %s", text)
                return True
            log.warning("Telegram Antwort %s: %s", r.status_code, r.text[:200])
            if r.status_code in (400, 401, 403, 404):
                return False
        except requests.RequestException as e:
            log.warning("Telegram nicht erreichbar (Versuch %d): %s", i, e)
        time.sleep(3 * i)
    return False


# ------------------------------------------------------------
# WhatsApp über CallMeBot (Ersatz, wenn Telegram fehlt; Gratis-Kontingent ist begrenzt)
# ------------------------------------------------------------
def whatsapp_senden(text: str, versuche: int = 3, wichtig: bool = True) -> bool:
    """Telegram zuerst; WhatsApp nur als Ersatz – und dann nur für wichtige Meldungen (spart das Kontingent)."""
    if telegram_senden(text):
        return True
    # Ohne Telegram läuft alles über WhatsApp; nur wenn Telegram eingerichtet ist und ausfällt,
    # gehen ersatzweise bloß die wichtigen Meldungen per WhatsApp raus.
    if TELEGRAM_BOT_TOKEN and not wichtig:
        log.info("Nicht gesendet (nur über Telegram): %s", text)
        return False
    if not (WHATSAPP_PHONE and CALLMEBOT_APIKEY):
        log.error("Weder Telegram noch WhatsApp eingerichtet: %s", text)
        return False
    url = "https://api.callmebot.com/whatsapp.php"
    params = {"phone": WHATSAPP_PHONE, "text": text, "apikey": CALLMEBOT_APIKEY}
    for i in range(1, versuche + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                body = r.text.lower()
                if any(m in body for m in ("invalid", "not activated", "error:")):
                    # Konfigurationsfehler – Wiederholen hilft nicht
                    log.error("CallMeBot meldet Fehler (Nummer/API-Key prüfen): %s", r.text[:300])
                    return False
                log.info("WhatsApp gesendet: %s", text)
                return True
            log.warning("CallMeBot Antwort %s (Versuch %d): %s", r.status_code, i, r.text[:300])
        except requests.RequestException as e:
            log.warning("CallMeBot nicht erreichbar (Versuch %d): %s", i, e)
        time.sleep(5 * i)
    log.error("WhatsApp endgültig fehlgeschlagen: %s", text)
    return False


class WhatsAppSender:
    """Verschickt Nachrichten in einem eigenen Thread, damit der Kursempfang nie blockiert."""

    def __init__(self):
        self.q: "queue.Queue[tuple[str, bool]]" = queue.Queue()
        threading.Thread(target=self._loop, daemon=True, name="whatsapp").start()

    def senden(self, text: str, wichtig: bool = True):
        self.q.put((text, wichtig))

    def _loop(self):
        while True:
            text, wichtig = self.q.get()
            whatsapp_senden(text, wichtig=wichtig)
            time.sleep(2)  # nicht fluten


# ------------------------------------------------------------
# Stündliche Statusmeldung
# ------------------------------------------------------------
STAND = {"letzter_kurs_ts": 0.0, "letzter_preis": None, "quelle": "-", "alarme_heute": 0,
         "alarme_datum": None, "start": time.time()}
_stand_lock = threading.Lock()


def alarm_zaehlen():
    with _stand_lock:
        heute = jetzt_lokal().date()
        if STAND["alarme_datum"] != heute:
            STAND["alarme_datum"], STAND["alarme_heute"] = heute, 0
        STAND["alarme_heute"] += 1


def status_faellig(dt: datetime) -> bool:
    if STATUS_AB_UHR < 0 or not (STATUS_AB_UHR <= dt.hour <= STATUS_BIS_UHR):
        return False
    return STATUS_AM_WOCHENENDE or not markt_geschlossen(dt.timestamp())


def status_text(dt: datetime) -> str:
    with _stand_lock:
        if STAND["alarme_datum"] != dt.date():
            STAND["alarme_datum"], STAND["alarme_heute"] = dt.date(), 0
        alarme, preis, ts, quelle = (STAND["alarme_heute"], STAND["letzter_preis"],
                                     STAND["letzter_kurs_ts"], STAND["quelle"])
    if markt_geschlossen(dt.timestamp()):
        return f"🟢 {PAAR_NAME}-Alarm läuft | Markt geschlossen (Wochenende) | {dt:%H:%M} Uhr"
    alter_min = (dt.timestamp() - ts) / 60
    alarm_txt = f"{alarme} Alarm{'e' if alarme != 1 else ''} heute"
    if preis is None or alter_min > 5:
        seit = "noch keine Kurse seit Start" if preis is None else f"seit {int(alter_min)} Min keine Kurse"
        return f"⚠️ {PAAR_NAME}-Alarm läuft, aber {seit} | {alarm_txt} | {dt:%H:%M} Uhr"
    return f"🟢 {PAAR_NAME}-Alarm läuft | Kurs {de_zahl(preis, 3)} ({quelle}) | {alarm_txt} | {dt:%H:%M} Uhr"


def status_schleife(sender):
    """Eigener Thread: weckt zu jeder vollen Stunde auf und meldet sich, wenn fällig."""
    while True:
        jetzt = jetzt_lokal()
        naechste = (jetzt.replace(minute=0, second=0, microsecond=0).timestamp() + 3600)
        time.sleep(max(1.0, naechste - time.time() + 2))
        dt = jetzt_lokal()
        if dt.minute == 0 and status_faellig(dt):
            sender.senden(status_text(dt), wichtig=False)   # Statusmeldung nur über Telegram


# ------------------------------------------------------------
# Tiingo-REST (für Test und echtes Kerzen-Open nach einem Neustart)
# ------------------------------------------------------------
def tiingo_get(pfad: str, params: dict | None = None):
    r = requests.get(f"https://api.tiingo.com{pfad}",
                     headers={"Authorization": f"Token {TIINGO_API_KEY}", "Content-Type": "application/json"},
                     params=params or {}, timeout=10)
    if r.status_code in (401, 403):
        raise RuntimeError("Tiingo-Key ungültig")
    r.raise_for_status()
    return r.json()


_letzter_open_abruf: dict[int, float] = {}


def tiingo_kerzen_open(start: int, minuten: int) -> float | None:
    """Echtes Open der laufenden Kerze holen (je Kerzenlänge max. 1 Abruf pro 2 Min, Gratis-Tarif: 50/Std)."""
    gran = f"{minuten}min" if minuten < 60 else "1hour"
    if not TIINGO_API_KEY or time.time() - _letzter_open_abruf.get(minuten, 0.0) < 120:
        return None
    _letzter_open_abruf[minuten] = time.time()
    try:
        tag = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d")
        daten = tiingo_get(f"/tiingo/fx/{TIINGO_TICKER}/prices",
                           {"startDate": tag, "resampleFreq": gran})
        for c in reversed(daten or []):
            if int(parse_iso(c["date"])) == start:
                return float(c["open"])
    except Exception as e:
        log.warning("Konnte %d-Min-Kerzen-Open nicht laden: %s", minuten, e)
    return None


# ------------------------------------------------------------
# Kerzen-Logik
# ------------------------------------------------------------
class Kerze:
    def __init__(self, start: int, open_preis: float):
        self.start = start
        self.open = open_preis
        self.letzter = open_preis
        self.gemeldet: set[tuple[float, int]] = set()  # (Schwelle, Richtung +1/-1)


class KerzenWaechter:
    """Überwacht eine Kerzenlänge mit einer oder mehreren Schwellen."""

    def __init__(self, sender, minuten: int, schwellen: list[float], open_quelle=tiingo_kerzen_open,
                 kerzen_loggen: bool = True):
        if 60 % minuten:
            raise ValueError(f"Kerzenlänge {minuten} Min teilt 60 nicht glatt")
        self.sender = sender
        self.minuten = minuten
        self.tf_sek = minuten * 60
        self.schwellen = sorted(schwellen, reverse=True)  # größte zuerst
        self.open_quelle = open_quelle
        self.kerzen_loggen = kerzen_loggen
        self.kerze: Kerze | None = None
        self.offizielles_open_holen = True  # nach (Re)Start echtes Kerzen-Open nachladen

    def tick(self, ts: float, mid: float):
        start = int(ts // self.tf_sek) * self.tf_sek

        if self.kerze is not None and start < self.kerze.start:
            return  # veralteter Kurs nach Reconnect

        if self.kerze is None or start > self.kerze.start:
            if self.kerze is not None and self.kerzen_loggen:
                self._kerze_loggen(self.kerze)
            open_preis = mid
            if self.offizielles_open_holen:
                # Einstieg mitten in einer Kerze (Start/Reconnect): echtes Open nachladen
                offiziell = self.open_quelle(start, self.minuten) if self.open_quelle else None
                if offiziell is not None:
                    open_preis = offiziell
                    log.info("%d-Min-Kerzen-Open nachgeladen: %.3f", self.minuten, offiziell)
                self.offizielles_open_holen = False
            self.kerze = Kerze(start, open_preis)

        k = self.kerze
        k.letzter = mid
        diff = round(mid - k.open, 5)
        richtung = 1 if diff > 0 else -1

        # Größte erreichte, noch nicht gemeldete Schwelle melden; springt der Kurs
        # gleich über mehrere Schwellen, kommt nur eine Nachricht (die größte).
        for schwelle in self.schwellen:
            if abs(diff) >= schwelle - 1e-9:
                if (schwelle, richtung) not in k.gemeldet:
                    for s in self.schwellen:
                        if s <= schwelle:
                            k.gemeldet.add((s, richtung))
                    self._alarm(diff, k.open, mid)
                break

    def _alarm(self, diff: float, open_preis: float, preis: float):
        pips = diff / PIP_GROESSE
        richtung = 1 if pips >= 0 else -1
        jetzt = time.time()
        # Zusammenfassen: 1-Min-Alarme höchstens alle MIN_ABSTAND_1MIN_SEK je Richtung
        # (und nicht direkt nach einem Alarm derselben Richtung) – größere Kerzen melden immer.
        if self.minuten == 1 and jetzt - LETZTER_ALARM.get(richtung, 0.0) < MIN_ABSTAND_1MIN_SEK:
            log.info("1-Min-Alarm zusammengefasst (%+.1f Pips)", pips)
            return
        LETZTER_ALARM[richtung] = jetzt
        pfeil, vorzeichen = ("▲", "+") if pips >= 0 else ("▼", "−")
        text = (
            f"🚨 {PAAR_NAME} {pfeil} {vorzeichen}{de_zahl(abs(pips), 1)} Pips in {self.minuten} Min"
            f" | Open {de_zahl(open_preis, 3)} → {de_zahl(preis, 3)}"
            f" | {jetzt_lokal():%H:%M} Uhr"
        )
        log.info("ALARM: %s", text)
        alarm_zaehlen()
        # kleine 1-Min-Alarme nur über Telegram, große Bewegungen auch per WhatsApp-Ersatz
        self.sender.senden(text, wichtig=self.minuten > 1)

    def _kerze_loggen(self, k: Kerze):
        zeit = datetime.fromtimestamp(k.start, TZ_LOKAL).strftime("%d.%m. %H:%M")
        pips = (k.letzter - k.open) / PIP_GROESSE
        log.info("%d-Min-Kerze %s: Open %.3f Close %.3f Netto %+.1f Pips",
                 self.minuten, zeit, k.open, k.letzter, pips)


class AlleWaechter:
    """Verteilt jeden Kurs an alle Kerzenlängen aus REGELN."""

    def __init__(self, sender, open_quelle=tiingo_kerzen_open):
        self.sender = sender
        groesste = max(REGELN)
        self.liste = [KerzenWaechter(sender, m, s, open_quelle, kerzen_loggen=(m == groesste))
                      for m, s in sorted(REGELN.items())]

    def tick(self, ts: float, mid: float):
        for w in self.liste:
            w.tick(ts, mid)

    @property
    def offizielles_open_holen(self):
        return any(w.offizielles_open_holen for w in self.liste)

    @offizielles_open_holen.setter
    def offizielles_open_holen(self, wert: bool):
        for w in self.liste:
            w.offizielles_open_holen = wert


# ------------------------------------------------------------
# Live-Kurse fürs Dashboard (live.json)
# ------------------------------------------------------------
class LiveKurse:
    """Merkt sich je Paar den letzten Kurs und 5-Min-Schlusskurse der letzten 25 Std."""

    def __init__(self):
        self.lock = threading.Lock()
        self.bars: dict[str, dict[int, list]] = {p: {} for p in LIVE_PAARE}   # Start -> [o, h, l, c]
        self.letzter: dict[str, tuple[float, float]] = {}
        self.bytes_datei = DATA_DIR / "tiingo_volumen.json"
        try:
            self.volumen = json.loads(self.bytes_datei.read_text())
        except Exception:
            self.volumen = {"monat": "", "bytes": 0}

    def add(self, ticker: str, ts: float, mid: float):
        with self.lock:
            b = int(ts // 300) * 300
            k = self.bars.setdefault(ticker, {}).get(b)
            if k is None:
                self.bars[ticker][b] = [mid, mid, mid, mid]
            else:
                k[1], k[2], k[3] = max(k[1], mid), min(k[2], mid), mid
            self.letzter[ticker] = (ts, mid)

    def bytes_zaehlen(self, n: int):
        monat = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.lock:
            if self.volumen.get("monat") != monat:
                self.volumen = {"monat": monat, "bytes": 0}
            self.volumen["bytes"] += n

    def nachladen(self):
        """Beim Start die letzten 25 Std aus Tiingo-REST holen (1 Abruf je Paar)."""
        if not TIINGO_API_KEY:
            return
        start = (datetime.now(timezone.utc) - timedelta(hours=26)).strftime("%Y-%m-%d")
        for p in LIVE_PAARE:
            try:
                daten = tiingo_get(f"/tiingo/fx/{p}/prices", {"startDate": start, "resampleFreq": "5min"})
                with self.lock:
                    for c in daten or []:
                        b = int(parse_iso(c["date"]) // 300) * 300
                        self.bars[p].setdefault(b, [float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])])
                log.info("Live-Verlauf %s nachgeladen (%d Kerzen)", p, len(daten or []))
            except Exception as e:
                log.warning("Live-Verlauf %s nicht geladen: %s", p, e)

    def schreiben(self):
        grenze = time.time() - 25 * 3600
        with self.lock:
            for p in self.bars:
                for b in [b for b in self.bars[p] if b < grenze]:
                    del self.bars[p][b]
            paare = {}
            for p, bars in self.bars.items():
                punkte = sorted(bars.items())
                ts, preis = self.letzter.get(p, (punkte[-1][0], punkte[-1][1][3]) if punkte else (None, None))
                paare[p] = {"price": preis, "ts": ts, "bars": [[b, round(k[3], 5)] for b, k in punkte]}
                if p == TIINGO_TICKER:
                    paare[p]["ohlc"] = [[b] + [round(x, 5) for x in k] for b, k in punkte]
            daten = {"updated": time.time(), "market_closed": markt_geschlossen(), "pairs": paare,
                     "tiingo_mb_month": round(self.volumen.get("bytes", 0) / 1e6, 1),
                     "source": STAND.get("quelle")}
            volumen = dict(self.volumen)
        ziel = DATA_DIR / "public" / "live.json"
        try:
            ziel.parent.mkdir(parents=True, exist_ok=True)
            tmp = ziel.with_suffix(".tmp")
            tmp.write_text(json.dumps(daten, separators=(",", ":")))
            os.chmod(tmp, 0o644)
            tmp.replace(ziel)
            self.bytes_datei.write_text(json.dumps(volumen))
        except OSError as e:
            log.warning("live.json nicht geschrieben: %s", e)

    def schleife(self):
        self.nachladen()
        while True:
            self.schreiben()
            time.sleep(LIVE_SCHREIBEN_SEK)


LIVE: "LiveKurse | None" = None


# ------------------------------------------------------------
# Kursquellen (WebSocket)
# ------------------------------------------------------------
class ZugangsFehler(Exception):
    """Key ungültig oder Limit erreicht – Neuversuch erst später sinnvoll."""


def tiingo_nachricht(roh: str):
    """Liefert Liste von (ticker, ts, mid). Wirft ZugangsFehler bei Ablehnung."""
    msg = json.loads(roh)
    typ = msg.get("messageType")
    if typ == "A":
        d = msg.get("data") or []
        if len(d) >= 8 and d[0] == "Q" and str(d[1]).lower() in LIVE_PAARE:
            return [(str(d[1]).lower(), parse_iso(d[2]), float(d[5]))]
        return []
    if typ == "E" or (isinstance(msg.get("response"), dict) and msg["response"].get("code", 200) >= 400):
        raise ZugangsFehler(f"Tiingo: {msg.get('response') or msg}")
    return []  # Heartbeat "H" / Info "I"


FINNHUB_ZU_TICKER = {v: k for k, v in LIVE_PAARE.items()}


def finnhub_nachricht(roh: str):
    msg = json.loads(roh)
    typ = msg.get("type")
    if typ == "trade":
        letzte = {}
        for t in msg.get("data") or []:
            ticker = FINNHUB_ZU_TICKER.get(t.get("s"))
            if ticker and t.get("p"):
                letzte[ticker] = (ticker, t["t"] / 1000.0, float(t["p"]))
        return list(letzte.values())
    if typ == "error":
        raise ZugangsFehler(f"Finnhub: {msg.get('msg')}")
    return []  # ping


def quelle_tiingo():
    ws = websocket.create_connection("wss://api.tiingo.com/fx", timeout=HEARTBEAT_TIMEOUT_SEK)
    ws.send(json.dumps({"eventName": "subscribe", "authorization": TIINGO_API_KEY,
                        "eventData": {"thresholdLevel": TIINGO_THRESHOLD, "tickers": list(LIVE_PAARE)}}))
    return ws, tiingo_nachricht


def quelle_finnhub():
    ws = websocket.create_connection(f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}", timeout=HEARTBEAT_TIMEOUT_SEK)
    for symbol in LIVE_PAARE.values():
        ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
    return ws, finnhub_nachricht


QUELLEN = []
if TIINGO_API_KEY:
    QUELLEN.append(("Tiingo", quelle_tiingo))
if FINNHUB_API_KEY:
    QUELLEN.append(("Finnhub", quelle_finnhub))


def stream_laufen(name, oeffnen, waechter: "AlleWaechter", status: dict):
    ws, parser = oeffnen()
    log.info("Verbunden mit %s", name)
    verbunden_seit = time.time()
    try:
        while True:
            roh = ws.recv()  # wirft WebSocketTimeoutException nach HEARTBEAT_TIMEOUT_SEK Stille
            if not roh:
                raise ConnectionError("Verbindung vom Server geschlossen")
            jetzt = time.time()
            status["letzte_daten"] = jetzt
            if markt_geschlossen():
                status["letzter_kurs"] = jetzt  # Wochenende zählt nicht als Ausfall
            elif jetzt - status["letzter_kurs"] > 300 and jetzt - verbunden_seit > 300:
                raise ConnectionError("Verbindung steht, aber seit 5 Min keine Kurse")
            if name == "Tiingo" and LIVE is not None:
                LIVE.bytes_zaehlen(len(roh))
            for ticker, ts, mid in parser(roh):
                if LIVE is not None:
                    LIVE.add(ticker, ts, mid)
                if ticker != TIINGO_TICKER:
                    continue
                kurs_verarbeiten(name, ts, mid, waechter, status)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def kurs_verarbeiten(name, ts, mid, waechter, status):
    """Ein GBP/JPY-Kurs: Status pflegen, Störungsmeldung aufheben, Alarm-Regeln prüfen."""
    status["letzter_kurs"] = time.time()
    with _stand_lock:
        STAND["letzter_kurs_ts"], STAND["letzter_preis"], STAND["quelle"] = time.time(), mid, name
    status["fehler_in_folge"] = 0
    if status.get("stoerung_gemeldet"):
        status["stoerung_gemeldet"] = False
        waechter.sender.senden(f"✅ {PAAR_NAME}-Alarm: Kurse kommen wieder ({name}) | {jetzt_lokal():%H:%M} Uhr")
    waechter.tick(ts, mid)


def hauptschleife():
    fehlende = [] if TELEGRAM_BOT_TOKEN else [
        n for n, v in [("WHATSAPP_PHONE", WHATSAPP_PHONE), ("CALLMEBOT_APIKEY", CALLMEBOT_APIKEY)] if not v]
    if not QUELLEN:
        fehlende.append("TIINGO_API_KEY oder FINNHUB_API_KEY")
    if fehlende:
        log.error("Fehlende Einträge in .env: %s", ", ".join(fehlende))
        sys.exit(1)

    regeln = "; ".join(f"{m} Min: " + "/".join(f"{x / PIP_GROESSE:.0f}" for x in sorted(s)) + " Pips"
                       for m, s in sorted(REGELN.items()))
    log.info("Start: %s | %s | Quellen: %s", PAAR_NAME, regeln, ", ".join(n for n, _ in QUELLEN))
    sender = WhatsAppSender()
    threading.Thread(target=status_schleife, args=(sender,), daemon=True, name="status").start()
    global LIVE
    LIVE = LiveKurse()
    threading.Thread(target=LIVE.schleife, daemon=True, name="live").start()
    waechter = AlleWaechter(sender)
    status = {"letzte_daten": time.time(), "letzter_kurs": time.time(), "stoerung_gemeldet": False,
              "fehler_in_folge": 0}
    quelle_idx = 0
    wartezeit = 1

    while True:
        name, oeffnen = QUELLEN[quelle_idx]
        versuch_start = time.time()
        try:
            stream_laufen(name, oeffnen, waechter, status)
        except ZugangsFehler as e:
            log.error("%s lehnt ab (Key prüfen / Limit erreicht): %s", name, e)
            wartezeit = 300
            status["fehler_in_folge"] += 1
        except (websocket.WebSocketException, OSError, ConnectionError, ValueError, KeyError) as e:
            if markt_geschlossen():
                log.info("Markt geschlossen, Verbindung ruht (%s: %s)", name, type(e).__name__)
                wartezeit = 60
            else:
                log.warning("Verbindung zu %s unterbrochen: %s", name, e)
                if status["letzte_daten"] > versuch_start:
                    wartezeit = 1
                wartezeit = min(wartezeit * 2, 60)
                status["fehler_in_folge"] += 1

        waechter.offizielles_open_holen = True

        # Werktags 3 Fehlversuche in Folge oder 5 Min ohne Kurs -> Reservequelle probieren
        if len(QUELLEN) > 1 and not markt_geschlossen() and (
                status["fehler_in_folge"] >= 3 or time.time() - status["letzter_kurs"] > 300):
            quelle_idx = (quelle_idx + 1) % len(QUELLEN)
            status["fehler_in_folge"] = 0
            wartezeit = 1
            log.warning("Wechsle Kursquelle zu %s", QUELLEN[quelle_idx][0])

        ausfall_min = (time.time() - status["letzter_kurs"]) / 60
        if (STOERUNG_MELDEN_NACH_MIN and ausfall_min >= STOERUNG_MELDEN_NACH_MIN
                and not status["stoerung_gemeldet"] and not markt_geschlossen()
                and not markt_geschlossen(status["letzter_kurs"] + 60)):
            status["stoerung_gemeldet"] = True
            sender.senden(f"⚠️ {PAAR_NAME}-Alarm: seit {int(ausfall_min)} Min keine Kurse – "
                          f"ich versuche weiter | {jetzt_lokal():%H:%M} Uhr")
        if markt_geschlossen():
            status["letzter_kurs"] = time.time()  # Wochenende zählt nicht als Ausfall

        log.info("Neuer Verbindungsversuch in %d s", wartezeit)
        time.sleep(wartezeit)


# ------------------------------------------------------------
# Test-Modus
# ------------------------------------------------------------
def test_modus():
    teile, ok = [], True
    if TIINGO_API_KEY:
        print("Prüfe Tiingo ...")
        try:
            q = tiingo_get(f"/tiingo/fx/{TIINGO_TICKER}/top")[0]
            mid = float(q.get("midPrice") or (float(q["bidPrice"]) + float(q["askPrice"])) / 2)
            print(f"  OK – letzter Mid-Kurs {mid:.3f}")
            teile.append(f"Tiingo ok, Kurs {de_zahl(mid, 3)}")
        except Exception as e:
            print(f"  FEHLER – Tiingo: {e}")
            log.error("Test: Tiingo-Fehler: %s", e)
            teile.append(f"Tiingo-Fehler: {e}")
            ok = False
    if FINNHUB_API_KEY:
        print("Prüfe Finnhub ...")
        try:
            r = requests.get("https://finnhub.io/api/v1/forex/exchange",
                             params={"token": FINNHUB_API_KEY}, timeout=10)
            if r.status_code in (401, 403):
                raise RuntimeError("Finnhub-Key ungültig")
            r.raise_for_status()
            print("  OK – Finnhub-Key gültig")
            teile.append("Finnhub ok")
        except Exception as e:
            print(f"  FEHLER – Finnhub: {e}")
            log.error("Test: Finnhub-Fehler: %s", e)
            teile.append(f"Finnhub-Fehler: {e}")
            ok = False
    if not teile:
        teile.append("kein Kurs-Key eingetragen")
        ok = False

    zeichen = "✅" if ok else "⚠️"
    text = f"{zeichen} Test {PAAR_NAME}-Alarm | {' | '.join(teile)} | {jetzt_lokal():%H:%M} Uhr"
    print("Sende Test-WhatsApp ...")
    if whatsapp_senden(text):
        print("  OK – Nachricht sollte in wenigen Sekunden auf dem Handy sein.")
    else:
        print("  FEHLER – WhatsApp nicht gesendet. Nummer und CallMeBot-Key prüfen.")
        sys.exit(1)
    if not ok:
        sys.exit(2)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="Test-WhatsApp senden und beenden")
    args = ap.parse_args()
    try:
        if args.test:
            test_modus()
        else:
            hauptschleife()
    except KeyboardInterrupt:
        log.info("Beendet (Strg+C).")
    finally:
        if not args.test:
            log.info("Skript gestoppt.")
