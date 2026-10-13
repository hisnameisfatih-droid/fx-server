"""News aus RSS-Feeds, vorgefiltert nach Schlagwörtern."""
from __future__ import annotations

import calendar as _cal
import hashlib
import html
import re
from datetime import datetime, timezone

import feedparser

from ..common import http_get

QUELLEN = [
    # (Name, URL, nur_mit_schlagwort)
    ("FXStreet", "https://www.fxstreet.com/rss/news", True),
    ("ActionForex", "https://www.actionforex.com/feed/", True),
    ("Reuters", "https://news.google.com/rss/search?q=site:reuters.com+(sterling+OR+yen+OR+%22Bank+of+England%22"
                "+OR+%22Bank+of+Japan%22+OR+gilt+OR+%22yen+intervention%22)+when:2d&hl=en-GB&gl=GB&ceid=GB:en", True),
    ("Bank of England", "https://www.bankofengland.co.uk/rss/news", False),
    ("Bank of England", "https://www.bankofengland.co.uk/rss/speeches", False),
    ("Bank of Japan", "https://www.boj.or.jp/en/rss/whatsnew.xml", False),
]

SCHLAGWOERTER = [
    "boe", "bank of england", "mpc", "boj", "bank of japan", "ueda", "gilt", "jgb", "yen", "intervention",
    "sterling", "pound", "budget", "gbp", "jpy", "bailey", "reeves", "uk cpi", "uk inflation", "japan",
    "tankan", "fed ", "fomc", "treasury yield", "us cpi", "nonfarm", "payrolls",
]
# BoJ veröffentlicht viele reine Statistiken – nur geldpolitisch Relevantes behalten
BOJ_RELEVANT = ["statement", "summary of opinions", "speech", "outlook", "minutes", "monetary policy",
                "remarks", "press conference", "policy board"]


def _text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _zeit(eintrag) -> datetime | None:
    for feld in ("published_parsed", "updated_parsed"):
        t = eintrag.get(feld)
        if t:
            return datetime.fromtimestamp(_cal.timegm(t), timezone.utc)
    return None


def relevant(titel: str, zusammenfassung: str, quelle: str) -> bool:
    t = f" {titel} {zusammenfassung} ".lower()
    if quelle == "Bank of Japan":
        return any(w in t for w in BOJ_RELEVANT)
    return any(w in t for w in SCHLAGWOERTER) or any(p in t for p in ("gbp/", "/jpy", "usd/jpy", "gbp/usd"))


def lade_feed(name: str, url: str, filtern: bool) -> list[dict]:
    roh = http_get(url, timeout=20).content
    feed = feedparser.parse(roh)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"{name}: Feed nicht lesbar")
    erg = []
    for e in feed.entries[:60]:
        titel = _text(e.get("title", ""))
        quelle = name
        if name == "Reuters":  # Google News hängt " - Reuters" an
            titel = re.sub(r"\s+-\s+Reuters$", "", titel)
        zus = _text(e.get("summary", ""))[:400]
        if not titel:
            continue
        if (filtern or name == "Bank of Japan") and not relevant(titel, zus if name != "Reuters" else "", name):
            continue
        ts = _zeit(e)
        if ts is None:
            continue
        link = e.get("link", "")
        nid = hashlib.sha1(f"{quelle}|{titel}".encode()).hexdigest()[:16]
        erg.append({"id": nid, "ts": ts.isoformat(), "source": quelle, "title": titel, "url": link, "summary": zus})
    return erg


def lade_alle() -> tuple[list[dict], list[str]]:
    """Gibt (Meldungen, fehlgeschlagene Quellen) zurück."""
    alle, fehler = [], []
    for name, url, filtern in QUELLEN:
        try:
            alle.extend(lade_feed(name, url, filtern))
        except Exception:  # noqa: BLE001
            if name not in fehler:
                fehler.append(name)
    return alle, fehler
