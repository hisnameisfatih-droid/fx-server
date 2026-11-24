"""Sofort-Meldung per WhatsApp bei wichtigen Nachrichten für GBP/JPY (z. B. Ueda vor dem Parlament,
Interventionswarnungen aus Tokio, Aussagen von Bailey oder überraschende Daten).

Ablauf (alle 2 Minuten aus refresh.py): Feeds laden -> neue Meldungen von Claude einordnen -> Meldungen mit
Relevanz >= NEWS_ALARM_AB, die GBP oder JPY betreffen, gehen gebündelt in EINER Nachricht raus.
Gleiche Geschichte aus mehreren Quellen wird nur einmal gemeldet. Ohne Claude-Schlüssel greift eine
Schlagwort-Regel (nur sehr starke Begriffe). Keine Anlageberatung.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from .common import ANTHROPIC_API_KEY, TZ_BERLIN, get_logger, kv_get, kv_set, whatsapp

log = get_logger("news_alarm")
AN = os.getenv("NEWS_ALARM", "1") != "0"
AB = int(os.getenv("NEWS_ALARM_AB", "9"))              # Relevanz 0–10 laut Claude-Einordnung
MAX_TAG = int(os.getenv("NEWS_ALARM_MAX_TAG", "5"))   # Schutz vor Flut (CallMeBot-Kontingent)
MAX_ALTER_MIN = 20                                      # ältere Meldungen sind kein "Echtzeit"-Alarm mehr
AEHNLICH = 0.45                                         # Wortüberschneidung, ab der zwei Titel dieselbe Geschichte sind
# Nur ohne Claude: Titel mit diesen Begriffen gelten als wichtig
STARK = re.compile(r"\b(ueda|bailey|katayama|mimura|intervention|intervene|rate hike|rate cut|boj (raises|hikes|holds|keeps)|"
                   r"boe (raises|cuts|holds|keeps)|emergency)\b", re.I)
RICHTUNG = {"GBP+": "GBP/JPY eher ↑ (stützt das Pfund)", "GBP-": "GBP/JPY eher ↓ (belastet das Pfund)",
            "JPY+": "GBP/JPY eher ↓ (stützt den Yen)", "JPY-": "GBP/JPY eher ↑ (belastet den Yen)",
            "USD+": "eher USD-Thema, GBP/JPY indirekt", "USD-": "eher USD-Thema, GBP/JPY indirekt", "neutral": "Wirkung unklar"}
STOPP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "as", "at", "by", "is", "be", "says", "said", "boj's",
         "boe's", "with", "from", "after", "amid", "its", "it", "will", "may", "could", "s"}


def _woerter(titel: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", titel.lower()) if w not in STOPP and len(w) > 1}


def aehnlich(a: str, b: str) -> bool:
    x, y = _woerter(a), _woerter(b)
    return bool(x and y) and len(x & y) / len(x | y) >= AEHNLICH


def _wichtig(r: dict) -> bool:
    if r["classified"] and r["relevance"] is not None:
        try:
            waehr = set(json.loads(r["currencies"] or "[]"))
        except ValueError:
            waehr = set()
        return r["relevance"] >= AB and bool(waehr & {"GBP", "JPY"})
    return not ANTHROPIC_API_KEY and bool(STARK.search(r["title"] or ""))


def kandidaten(con, jetzt: datetime | None = None) -> list[dict]:
    """Noch nicht gemeldete, frische, wichtige Meldungen – ohne Doppelte (auch nicht zu schon gemeldeten)."""
    jetzt = jetzt or datetime.now(timezone.utc)
    grenze = (jetzt - timedelta(minutes=MAX_ALTER_MIN)).isoformat()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM news WHERE COALESCE(alerted,0)=0 AND ts>=? ORDER BY ts", (grenze,))]
    if ANTHROPIC_API_KEY:
        rows = [r for r in rows if r["classified"]]      # erst melden, wenn eingeordnet
    gemeldet = [r["title"] for r in con.execute(
        "SELECT title FROM news WHERE alerted=1 AND ts>=?", ((jetzt - timedelta(hours=8)).isoformat(),))]
    erg = []
    for r in rows:
        if not _wichtig(r):
            con.execute("UPDATE news SET alerted=-1 WHERE id=?", (r["id"],))   # geprüft, nicht wichtig
            continue
        if any(aehnlich(r["title"], t) for t in gemeldet + [e["title"] for e in erg]):
            con.execute("UPDATE news SET alerted=2 WHERE id=?", (r["id"],))    # dieselbe Geschichte schon gemeldet
            continue
        erg.append(r)
    con.commit()
    return sorted(erg, key=lambda r: -(r["relevance"] or 0))


def text(meldungen: list[dict], kurs: float | None = None) -> str:
    z = ["📰 Wichtige Nachricht" + ("en" if len(meldungen) > 1 else "") + " für GBP/JPY"]
    for m in meldungen:
        zeit = datetime.fromisoformat(m["ts"]).astimezone(TZ_BERLIN).strftime("%H:%M")
        rel = f" · {m['relevance']}/10" if m.get("relevance") is not None and m.get("classified") else ""
        z.append(f"\n{zeit} {m['source']}{rel}\n{m['title']}")
        if m.get("note"):
            z.append(f"→ {m['note']}")
        if m.get("effect"):
            z.append(f"→ {RICHTUNG.get(m['effect'], m['effect'])}")
        elif not m.get("classified"):
            z.append("→ nicht eingeordnet (Schlagwort-Treffer)")
    if kurs:
        z.append(f"\nKurs jetzt: {kurs:.3f}".replace(".", ","))
    z.append("Keine Anlageberatung.")
    return "\n".join(z)


def pruefen(con, kurs: float | None = None, senden=None) -> int:
    """Gibt die Zahl gemeldeter Nachrichten zurück."""
    if not AN:
        return 0
    senden = senden or (lambda t: whatsapp(t, log, art="news"))
    neu = kandidaten(con)
    if not neu:
        return 0
    tag = datetime.now(TZ_BERLIN).date().isoformat()
    zaehler = kv_get(con, "news_alarm:zaehler") or {}
    n = zaehler.get(tag, 0)
    if n >= MAX_TAG:
        log.warning("News-Alarm: Tageslimit %d erreicht, %d Meldungen nicht gesendet", MAX_TAG, len(neu))
        for r in neu:
            con.execute("UPDATE news SET alerted=3 WHERE id=?", (r["id"],))
        con.commit()
        return 0
    neu = neu[:3]   # höchstens 3 Schlagzeilen in einer Nachricht
    if not senden(text(neu, kurs)):
        return 0     # nächster Lauf versucht es erneut
    jetzt = datetime.now(timezone.utc).isoformat()
    for r in neu:
        con.execute("UPDATE news SET alerted=1, seen=? WHERE id=?", (jetzt, r["id"]))
    con.commit()
    kv_set(con, "news_alarm:zaehler", {tag: n + 1})
    log.info("News-Alarm gesendet: %s", " | ".join(r["title"] for r in neu))
    return len(neu)
