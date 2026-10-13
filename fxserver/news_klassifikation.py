"""Neue Meldungen von Claude einordnen lassen (Relevanz, Wirkung, ein Satz)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import llm
from .common import ANTHROPIC_API_KEY, get_logger

log = get_logger("news")
BATCH = 25


def klassifizieren(con, max_batches: int = 3) -> int:
    if not ANTHROPIC_API_KEY:
        return 0
    grenze = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
    gesamt = 0
    for _ in range(max_batches):
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM news WHERE classified=0 AND ts>=? ORDER BY ts DESC LIMIT ?", (grenze, BATCH))]
        if not rows:
            break
        ergebnisse = {e["id"]: e for e in llm.news_einordnen(rows) if e.get("id")}
        for r in rows:
            e = ergebnisse.get(r["id"])
            if e:
                con.execute("""UPDATE news SET classified=1, relevance=?, currencies=?, effect=?, note=? WHERE id=?""",
                            (int(e.get("relevanz", 0)), json.dumps(e.get("waehrungen", [])), e.get("wirkung"),
                             e.get("einordnung"), r["id"]))
            else:
                con.execute("UPDATE news SET classified=1, relevance=0 WHERE id=?", (r["id"],))
        con.commit()
        gesamt += len(rows)
    if gesamt:
        log.info("%d Meldungen eingeordnet", gesamt)
    return gesamt
