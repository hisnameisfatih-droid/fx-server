"""Laufende Aktualisierung (alle 5 Minuten per systemd-Timer).

- Kalender stündlich, News alle 20 Min (+ Einordnung durch Claude), Renditen alle 3 Std
- WhatsApp-Erinnerung 15 Min vor roten Terminen (abschaltbar mit TERMIN_ERINNERUNG=0 in der env-Datei)
- Dashboard-Dateien neu schreiben
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from . import export
from .common import TZ_BERLIN, db, faellig, get_logger, jetzt_berlin, whatsapp
from .daten import fx_tage_aktualisieren, news_aktualisieren, renditen_aktualisieren, termine_aktualisieren
from .news_klassifikation import klassifizieren

log = get_logger("refresh")
FLAGGE = {"GBP": "🇬🇧", "JPY": "🇯🇵", "USD": "🇺🇸"}
ERINNERUNG = os.getenv("TERMIN_ERINNERUNG", "1") != "0"


def erinnerungen(con) -> None:
    jetzt = jetzt_berlin()
    rows = con.execute("SELECT * FROM calendar WHERE impact='high' AND reminded=0 AND ts>? AND ts<=?",
                       (jetzt.isoformat(), (jetzt + timedelta(minutes=16)).isoformat())).fetchall()
    for t in rows:
        zeit = datetime.fromisoformat(t["ts"]).astimezone(TZ_BERLIN)
        fp = " | ".join(x for x in (f"F: {t['forecast']}" if t["forecast"] else "",
                                    f"P: {t['previous']}" if t["previous"] else "") if x)
        minuten = max(1, round((zeit - jetzt).total_seconds() / 60))
        text = f"⏰ In {minuten} Min ({zeit:%H:%M}): 🔴{FLAGGE.get(t['currency'], '')} {t['title']}" + (f" ({fp})" if fp else "")
        if t["effect"]:
            text += f"\n{t['effect']}"
        if whatsapp(text, log):
            con.execute("UPDATE calendar SET reminded=1 WHERE id=?", (t["id"],))
            con.commit()


def main() -> int:
    con = db()
    if faellig(con, "kalender", 3600 - 60):
        termine_aktualisieren(con)
    if faellig(con, "news", 20 * 60 - 30):
        news_aktualisieren(con)
        try:
            klassifizieren(con)
        except Exception as e:  # noqa: BLE001
            log.warning("News-Einordnung: %s", e)
    if faellig(con, "renditen", 3 * 3600 - 60):
        leer = con.execute("SELECT COUNT(*) FROM yields").fetchone()[0] < 20
        renditen_aktualisieren(con, mit_historie=leer)
    if con.execute("SELECT COUNT(*) FROM fxdaily").fetchone()[0] == 0 and faellig(con, "fx_erst", 3600):
        fx_tage_aktualisieren(con)   # Erststart: Vortageswerte fürs Dashboard
    if ERINNERUNG:
        erinnerungen(con)
    export.alles(con)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
