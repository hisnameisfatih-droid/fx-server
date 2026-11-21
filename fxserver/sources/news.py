"""News aus RSS-Feeds, vorgefiltert nach Schlagwörtern."""
from __future__ import annotations

import calendar as _cal
import hashlib
import html
import re
from datetime import datetime, timezone

import feedparser

from ..common import get_logger, http_get

log = get_logger("news")
OPTIONAL = {"investingLive", "Japan"}

QUELLEN = [
    # (Name, URL, nur_mit_schlagwort)
    ("FXStreet", "https://www.fxstreet.com/rss/news", True),
    ("ActionForex", "https://www.actionforex.com/feed/", True),
    ("Reuters", "https://news.google.com/rss/search?q=site:reuters.com+(sterling+OR+yen+OR+%22Bank+of+England%22"
                "+OR+%22Bank+of+Japan%22+OR+gilt+OR+%22yen+intervention%22)+when:2d&hl=en-GB&gl=GB&ceid=GB:en", True),
    ("Bank of England", "https://www.bankofengland.co.uk/rss/news", False),
    ("Bank of England", "https://www.bankofengland.co.uk/rss/speeches", False),
    ("Bank of Japan", "https://www.boj.or.jp/en/rss/whatsnew.xml", False),
    # Echtzeit-Schlagzeilen (investingLive, früher ForexLive): Zentralbank-Feed und Haupt-Feed
    ("investingLive", "https://investinglive.com/feed/centralbank", True),
    ("investingLive", "https://investinglive.com/feed", True),
    # Japanische Amtsträger (BoJ, Finanzministerium) – fängt z. B. Ueda vor dem Parlament oder Interventionswarnungen
    ("Japan", "https://news.google.com/rss/search?q=(Ueda+OR+Katayama+OR+Mimura+OR+Takaichi+OR+%22Bank+of+Japan%22"
              "+OR+%22yen+intervention%22+OR+%22Japan+finance+minister%22)+when:1d&hl=en-GB&gl=GB&ceid=GB:en", True),
]

SCHLAGWOERTER = [
    "boe", "bank of england", "mpc", "boj", "bank of japan", "ueda", "gilt", "jgb", "yen", "intervention",
    "sterling", "pound", "budget", "gbp", "jpy", "bailey", "reeves", "uk cpi", "uk inflation", "japan",
    "tankan", "fed ", "fomc", "treasury yield", "us cpi", "nonfarm", "payrolls",
    # Personen, deren Aussagen den Kurs bewegen (BoJ, Finanzministerium Japan, BoE/MPC, Schatzkanzlerin)
    "katayama", "mimura", "takaichi", "himino", "uchida", "finance minister", "mof ", "verbal intervention",
    "rate hike", "rate cut", "huw pill", "megan greene", "lombardelli", "catherine mann", "dhingra", "breeden",
    "alan taylor", "chancellor",
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
    roh = http_get(url, timeout=12, versuche=1).content
    feed = feedparser.parse(roh)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"{name}: Feed nicht lesbar")
    erg = []
    for e in feed.entries[:60]:
        titel = _text(e.get("title", ""))
        quelle = name
        if name in ("Reuters", "Japan"):  # Google News hängt " - Quelle" an
            titel = re.sub(r"\s+-\s+[^-]{2,40}$", "", titel)
        zus = _text(e.get("summary", ""))[:400]
        if not titel:
            continue
        if (filtern or name == "Bank of Japan") and not relevant(titel, zus if name not in ("Reuters", "Japan") else "", name):
            continue
        ts = _zeit(e)
        if ts is None:
            continue
        link = e.get("link", "")
        nid = hashlib.sha1(f"{quelle}|{titel}".encode()).hexdigest()[:16]
        erg.append({"id": nid, "ts": ts.isoformat(), "source": quelle, "title": titel, "url": link, "summary": zus})
    return erg


def lade_alle(mit_google: bool = False) -> tuple[list[dict], list[str]]:
    """Gibt (Meldungen, fehlgeschlagene Quellen) zurück."""
    from concurrent.futures import ThreadPoolExecutor
    alle, fehler = [], []
    # Google News nur etwa alle 10 Minuten (sonst sperrt Google den Server); die anderen Feeds bei jedem Lauf
    quellen = [q for q in QUELLEN if "news.google.com" not in q[1] or datetime.now().minute % 10 < 2 or mit_google]
    with ThreadPoolExecutor(max_workers=len(quellen)) as ex:   # parallel: ein langsamer Feed hält die anderen nicht auf
        laeufe = [(name, ex.submit(lade_feed, name, url, filtern)) for name, url, filtern in quellen]
        for name, f in laeufe:
            try:
                alle.extend(f.result())
            except Exception as e:  # noqa: BLE001
                if name in OPTIONAL:   # Zusatzquelle: Ausfall nur im Log, nicht als "Fehlt" im Report
                    log.warning("News-Quelle %s nicht erreichbar: %s", name, e)
                elif name not in fehler:
                    fehler.append(name)
    return alle, fehler
