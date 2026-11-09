"""Trefferauswertung nach Handelsschluss (läuft Mo–Fr um 23:05 Uhr Berlin).

Wertet alle Reports aus, deren FX-Tag (Ende 17:00 New York) abgeschlossen ist und die noch
keine Auswertung haben – holt also auch verpasste Tage nach.
Aufruf: python -m fxserver.evaluate
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import export, forecast
from . import amd as amd_mod
from . import setup as setup_mod
from .common import db, fx_tag_ende, get_logger
from .daten import fx_tage_aktualisieren

log = get_logger("evaluate")


def main() -> int:
    con = db()
    jetzt = datetime.now(timezone.utc)
    offen = [dict(r) for r in con.execute(
        "SELECT r.* FROM reports r LEFT JOIN evaluations e ON e.date=r.date WHERE e.date IS NULL "
        "AND r.price IS NOT NULL ORDER BY r.date")]
    offen = [r for r in offen if fx_tag_ende(datetime.fromisoformat(r["date"]).date()) <= jetzt]
    try:
        setup_mod.abschliessen(con)
    except Exception as e:  # noqa: BLE001
        log.warning("Setup-Abschluss: %s", e)
    try:
        amd_mod.abschliessen(con)
    except Exception as e:  # noqa: BLE001
        log.warning("AMD-Abschluss: %s", e)
    if not offen:
        log.info("Nichts auszuwerten")
        treiber_5_tage(con)
        vortage(con)
        export.alles(con)
        return 0
    fx_tage_aktualisieren(con, tage=max(10, (jetzt.date() - datetime.fromisoformat(offen[0]["date"]).date()).days + 5))
    for r in offen:
        tag = con.execute("SELECT * FROM fxdaily WHERE pair='gbpjpy' AND date=?", (r["date"],)).fetchone()
        vor = con.execute("SELECT * FROM fxdaily WHERE pair='gbpjpy' AND date<? ORDER BY date DESC LIMIT 2",
                          (r["date"],)).fetchall()
        if not tag or not vor:
            log.warning("Keine Kursdaten für %s", r["date"])
            continue
        prev_close = vor[0]["close"]
        vortag_change = (vor[0]["close"] - vor[1]["close"]) / 0.01 if len(vor) > 1 else None
        e = forecast.bewerte(r, {"o": tag["open"], "h": tag["high"], "l": tag["low"], "c": tag["close"]},
                             prev_close, r.get("atr_pips") or r.get("sigma_pips"), vortag_change=vortag_change)
        con.execute("""INSERT OR REPLACE INTO evaluations(date, open, close, high, low, prev_close, change_pips,
                       in_range, deviation_pips, direction_hit, naive_deviation_pips, naive_direction_hit, evaluated,
                       news_hit, treiber_hit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["date"], e["open"], e["close"], e["high"], e["low"], e["prev_close"], e["change_pips"],
                     e["in_range"], e["deviation_pips"], e["direction_hit"], e["naive_deviation_pips"],
                     e["naive_direction_hit"], jetzt.isoformat(), e["news_hit"], e["treiber_hit"]))
        log.info("%s: Schluss %.3f, Spanne %s, Richtung %s, Abweichung %.1f Pips", r["date"], e["close"],
                 "✓" if e["in_range"] else "✗", "✓" if e["direction_hit"] else "✗", e["deviation_pips"])
    con.commit()
    treiber_5_tage(con)
    vortage(con)
    export.alles(con)
    return 0


def vortage(con) -> None:
    """Vortage-Ansicht: Kerzen des abgeschlossenen Tages sichern, tage.json mit allen Ergebnissen neu schreiben."""
    try:
        from . import tage as tage_mod
        tage_mod.schreiben(con)
    except Exception as e:  # noqa: BLE001
        log.warning("Vortage: %s", e)


def treiber_5_tage(con) -> int:
    """Treiber-Empfehlung über 5 FX-Tage nachmessen, sobald der 5. Tag abgeschlossen ist."""
    jetzt = datetime.now(timezone.utc)
    offen = con.execute("SELECT r.date, r.price, r.treiber_score FROM reports r JOIN evaluations e ON e.date=r.date "
                        "WHERE e.treiber_hit5 IS NULL AND r.treiber_score IS NOT NULL AND ABS(r.treiber_score) >= ?",
                        (forecast.TREIBER_SCHWELLE,)).fetchall()
    n = 0
    for r in offen:
        tage = con.execute("SELECT date, close FROM fxdaily WHERE pair='gbpjpy' AND date>=? ORDER BY date LIMIT 5",
                           (r["date"],)).fetchall()
        if len(tage) < 5 or fx_tag_ende(datetime.fromisoformat(tage[4]["date"]).date()) > jetzt:
            continue
        hit = forecast.treiber_hit5(dict(r), tage[4]["close"])
        con.execute("UPDATE evaluations SET treiber_hit5=? WHERE date=?", (hit, r["date"]))
        n += 1
    con.commit()
    if n:
        log.info("Treiber 5 Tage: %d nachgemessen", n)
    return n


def neu_abrechnen(con, tage: int = 30) -> int:
    """Einmalige Korrektur (09.11.2026): Trades, die live nie ausgelöst hatten (kein Live-Einstieg gespeichert),
    aber in der Abrechnung mit Pips gezählt wurden, auf 'kein Entry' setzen. Gibt die Zahl der Korrekturen zurück."""
    import json
    from datetime import timedelta
    from . import amd as amd_mod
    from . import setup as setup_mod
    grenze = (datetime.now(timezone.utc).date() - timedelta(days=tage)).isoformat()
    kein = json.dumps({"grund": "kein_entry", "quelle": "live", "korrigiert": "2026-11-09"})
    n = 0
    amd_mod._db(con)
    for r in con.execute("SELECT date, variante, trade_json, result_json FROM amd WHERE phase='signal' "
                         "AND result_json IS NOT NULL AND date>=?", (grenze,)).fetchall():
        live = json.loads(r["trade_json"]) if r["trade_json"] else None
        if not live and json.loads(r["result_json"]).get("pips") is not None:
            con.execute("UPDATE amd SET result_json=?, state='verfallen' WHERE date=? AND variante=?",
                        (kein, r["date"], r["variante"]))
            log.info("Korrigiert %s %s: live kein Einstieg, war %s", r["date"], r["variante"], r["result_json"])
            n += 1
    con.executescript(setup_mod.SCHEMA)
    for r in con.execute("SELECT date, trade_json, result_json FROM setups WHERE status='aktiv' "
                         "AND result_json IS NOT NULL AND date>=?", (grenze,)).fetchall():
        live = json.loads(r["trade_json"]) if r["trade_json"] else None
        if not live and json.loads(r["result_json"]).get("pips") is not None:
            con.execute("UPDATE setups SET result_json=?, state='verfallen' WHERE date=?", (kein, r["date"]))
            log.info("Korrigiert Setup %s: live kein Einstieg, war %s", r["date"], r["result_json"])
            n += 1
    con.commit()
    vortage(con)
    export.alles(con)
    log.info("Neu abgerechnet: %d Korrekturen", n)
    return n


if __name__ == "__main__":
    import sys
    if "--neu-abrechnen" in sys.argv:
        neu_abrechnen(db())
        raise SystemExit(0)
    raise SystemExit(main())
