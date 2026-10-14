"""Laufende Aktualisierung (jede Minute per systemd-Timer).

- Kalender stündlich, News alle 20 Min (+ Einordnung durch Claude), Renditen alle 3 Std
- Treiber: VIX/Nikkei/Brent alle 30 Min, CFTC alle 6 Std, Score + Empfehlung jede Minute (mit Live-USD/JPY)
- WhatsApp-Erinnerung 15 Min vor roten Terminen (abschaltbar mit TERMIN_ERINNERUNG=0 in der env-Datei)
- WhatsApp bei Interventionsalarm oder Wechsel der Treiber-Empfehlung (abschaltbar mit TREIBER_ALARM=0)
- Dashboard-Dateien neu schreiben
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from . import export
from . import setup as setup_mod
from . import treiber
from .common import TZ_BERLIN, db, faellig, get_logger, jetzt_berlin, whatsapp
from .daten import (cftc_aktualisieren, fx_tage_aktualisieren, markt_aktualisieren, news_aktualisieren,
                    renditen_aktualisieren, termine_aktualisieren)
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
    try:
        setup_mod.verfolgen(con)
    except Exception as e:  # noqa: BLE001
        log.warning("Setup-Verfolgung: %s", e)
    # Treiber-Quellen erst nach der Setup-Verfolgung (die soll nie warten müssen)
    if faellig(con, "markt", 30 * 60 - 30):
        try:
            markt_aktualisieren(con)
        except Exception as e:  # noqa: BLE001
            log.warning("Markt: %s", e)
    if faellig(con, "cftc", 6 * 3600 - 60):
        try:
            cftc_aktualisieren(con)
        except Exception as e:  # noqa: BLE001
            log.warning("CFTC: %s", e)
    tb = None
    try:
        tb = treiber.berechnen(con, setup_trend=setup_trend_heute(con))
        treiber.melden(con, tb, senden=lambda text: whatsapp(text, log))
        treiber.merken(con, tb)
    except Exception as e:  # noqa: BLE001
        log.warning("Treiber: %s", e)
    export.alles(con, treiber_daten=tb)
    return 0


def setup_trend_heute(con) -> int | None:
    """Richtung des heutigen Setups (falls schon berechnet) – für den Abgleich mit den Treibern."""
    import json
    from .common import fx_tag
    con.executescript(setup_mod.SCHEMA)
    row = con.execute("SELECT status, data_json FROM setups WHERE date=?", (fx_tag(jetzt_berlin()).isoformat(),)).fetchone()
    if not row or row["status"] != "aktiv":
        return None
    return json.loads(row["data_json"]).get("trend") or None


if __name__ == "__main__":
    raise SystemExit(main())
