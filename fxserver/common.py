"""Gemeinsame Bausteine: Konfiguration, Logging, HTTP, WhatsApp, Zeit, Datenbank."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

# ------------------------------------------------------------
# Pfade und Zugangsdaten
# ------------------------------------------------------------
CODE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.getenv("FX_ENV_FILE", "/etc/fx-server/env"))
DATA_DIR = Path(os.getenv("FX_DATA_DIR", "/var/lib/fx-server"))
LOG_DIR = Path(os.getenv("FX_LOG_DIR", "/var/log/fx-server"))
PUBLIC_DIR = DATA_DIR / "public"          # wird vom Webserver unter /<geheim>/data/ ausgeliefert
DB_FILE = DATA_DIR / "fx.db"

load_dotenv(ENV_FILE)

TIINGO_API_KEY = os.getenv("TIINGO_API_KEY", "").strip()
WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE", "").strip().replace(" ", "")
CALLMEBOT_APIKEY = os.getenv("CALLMEBOT_APIKEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5").strip()
CLAUDE_MODEL_KLEIN = os.getenv("CLAUDE_MODEL_KLEIN", "claude-haiku-5-5").strip()
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()

TZ_BERLIN = ZoneInfo("Europe/Berlin")
TZ_NY = ZoneInfo("America/New_York")
PIP = 0.01  # JPY-Paare; für GBP/USD wird 0.0001 genutzt
PIP_GROESSE = {"gbpjpy": 0.01, "usdjpy": 0.01, "gbpusd": 0.0001}
PAARE = ["gbpjpy", "usdjpy", "gbpusd"]
PAAR_NAME = {"gbpjpy": "GBP/JPY", "usdjpy": "USD/JPY", "gbpusd": "GBP/USD"}

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) fx-server/1.0 (private use)"


# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


def http_get(url: str, *, params: dict | None = None, headers: dict | None = None,
             timeout: int = 20, versuche: int = 2) -> requests.Response:
    letzter: Exception | None = None
    for i in range(versuche):
        try:
            r = _session.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            letzter = e
            if i + 1 < versuche:
                time.sleep(3 * (i + 1))
    assert letzter is not None
    raise letzter


# ------------------------------------------------------------
# WhatsApp (CallMeBot)
# ------------------------------------------------------------
def whatsapp(text: str, log: logging.Logger | None = None, max_teil: int = 1500) -> bool:
    """Schickt Text per CallMeBot. Längere Texte werden an Absätzen geteilt."""
    log = log or get_logger("whatsapp")
    if not (WHATSAPP_PHONE and CALLMEBOT_APIKEY):
        log.error("WhatsApp nicht konfiguriert")
        return False
    teile, aktuell = [], ""
    for absatz in text.split("\n\n"):
        kandidat = (aktuell + "\n\n" + absatz) if aktuell else absatz
        if len(kandidat) > max_teil and aktuell:
            teile.append(aktuell)
            aktuell = absatz
        else:
            aktuell = kandidat
    if aktuell:
        teile.append(aktuell)
    ok = True
    for nr, teil in enumerate(teile):
        gesendet = False
        for versuch in range(3):
            try:
                r = _session.get("https://api.callmebot.com/whatsapp.php",
                                 params={"phone": WHATSAPP_PHONE, "text": teil, "apikey": CALLMEBOT_APIKEY},
                                 timeout=30)
                body = r.text.lower()
                if r.status_code == 200 and not any(m in body for m in ("invalid", "not activated", "error:")):
                    gesendet = True
                    break
                log.warning("CallMeBot %s: %s", r.status_code, r.text[:200])
            except requests.RequestException as e:
                log.warning("CallMeBot nicht erreichbar: %s", e)
            time.sleep(5 * (versuch + 1))
        if not gesendet:
            log.error("WhatsApp-Teil %d fehlgeschlagen", nr + 1)
            ok = False
        if nr + 1 < len(teile):
            time.sleep(3)
    if ok:
        log.info("WhatsApp gesendet (%d Teil(e), %d Zeichen)", len(teile), len(text))
    return ok


# ------------------------------------------------------------
# Zeit
# ------------------------------------------------------------
def jetzt_berlin() -> datetime:
    return datetime.now(TZ_BERLIN)


def fx_tag(dt: datetime) -> date:
    """FX-Handelstag eines Zeitpunkts: Der Tag endet um 17:00 New Yorker Zeit.
    Benannt nach dem NY-Datum, an dem er schließt (Mo-Fr)."""
    ny = dt.astimezone(TZ_NY)
    d = ny.date() if ny.hour < 17 else ny.date() + timedelta(days=1)
    while d.weekday() >= 5:  # Sa/So gehören zum folgenden Montag
        d += timedelta(days=1)
    return d


def fx_tag_ende(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 17, 0, tzinfo=TZ_NY)


def fx_tag_start(d: date) -> datetime:
    vor = d - timedelta(days=1)
    while vor.weekday() >= 5:
        vor -= timedelta(days=1)
    return datetime(vor.year, vor.month, vor.day, 17, 0, tzinfo=TZ_NY)


def parse_iso(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    if "." in s:
        haupt, rest = s.split(".", 1)
        tz = ""
        for zeichen in "+-":
            if zeichen in rest:
                frac, tzteil = rest.split(zeichen, 1)
                tz = zeichen + tzteil
                break
        else:
            frac = rest
        s = f"{haupt}.{(frac + '000000')[:6]}{tz}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def de_zahl(wert: float | None, stellen: int = 3) -> str:
    if wert is None:
        return "n/v"
    return f"{wert:,.{stellen}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def de_prozent(wert: float | None, stellen: int = 2, vorzeichen: bool = True) -> str:
    if wert is None:
        return "n/v"
    s = f"{wert:+.{stellen}f}" if vorzeichen else f"{wert:.{stellen}f}"
    return s.replace(".", ",") + "%"


# ------------------------------------------------------------
# Datenbank
# ------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  date TEXT PRIMARY KEY, created TEXT, price REAL, prev_close REAL, sigma_pips REAL,
  range_low REAL, range_high REAL, target REAL, bias TEXT, bias_score INTEGER,
  bias_reason TEXT, bias_reason_long TEXT, level_up REAL, level_down REAL,
  message TEXT, data_json TEXT, missing TEXT, llm_ok INTEGER, sent INTEGER
);
CREATE TABLE IF NOT EXISTS evaluations (
  date TEXT PRIMARY KEY, open REAL, close REAL, high REAL, low REAL, prev_close REAL,
  change_pips REAL, in_range INTEGER, deviation_pips REAL, direction_hit INTEGER,
  naive_deviation_pips REAL, naive_direction_hit INTEGER, evaluated TEXT
);
CREATE TABLE IF NOT EXISTS news (
  id TEXT PRIMARY KEY, ts TEXT, source TEXT, title TEXT, url TEXT, summary TEXT,
  classified INTEGER DEFAULT 0, relevance INTEGER, currencies TEXT, effect TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS yields (
  date TEXT, series TEXT, value REAL, PRIMARY KEY (date, series)
);
CREATE TABLE IF NOT EXISTS calendar (
  id TEXT PRIMARY KEY, ts TEXT, currency TEXT, impact TEXT, title TEXT,
  forecast TEXT, previous TEXT, actual TEXT, effect TEXT, reminded INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fxdaily (
  date TEXT, pair TEXT, open REAL, high REAL, low REAL, close REAL, PRIMARY KEY (date, pair)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    neu = not DB_FILE.exists()
    con = sqlite3.connect(DB_FILE, timeout=30)
    if neu:
        try:
            os.chmod(DB_FILE, 0o600)
        except OSError:
            pass
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _spalten_ergaenzen(con)
    return con


ZUSATZ_SPALTEN = {
    "reports": {"news_score": "INTEGER", "news_reason": "TEXT", "exp_high": "REAL", "exp_low": "REAL", "atr_pips": "REAL"},
    "evaluations": {"news_hit": "INTEGER"},
}


def _spalten_ergaenzen(con: sqlite3.Connection) -> None:
    """Neue Spalten bei bestehenden Datenbanken nachrüsten (Updates ohne Datenverlust)."""
    for tabelle, spalten in ZUSATZ_SPALTEN.items():
        vorhanden = {r[1] for r in con.execute(f"PRAGMA table_info({tabelle})")}
        for name, typ in spalten.items():
            if name not in vorhanden:
                con.execute(f"ALTER TABLE {tabelle} ADD COLUMN {name} {typ}")
    con.commit()


def kv_get(con: sqlite3.Connection, key: str, default=None):
    row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(con: sqlite3.Connection, key: str, value) -> None:
    con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, json.dumps(value)))
    con.commit()


def faellig(con: sqlite3.Connection, key: str, sekunden: int) -> bool:
    """True, wenn seit dem letzten Lauf von `key` mindestens `sekunden` vergangen sind (und markiert ihn)."""
    letzter = kv_get(con, f"lauf:{key}", 0)
    if time.time() - letzter < sekunden:
        return False
    kv_set(con, f"lauf:{key}", time.time())
    return True


def schreibe_json(name: str, daten) -> None:
    """Atomar nach PUBLIC_DIR schreiben (für das Dashboard)."""
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    ziel = PUBLIC_DIR / name
    tmp = ziel.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(ziel)
