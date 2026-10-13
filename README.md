# fx-server

GBP/JPY-Kursalarm, Morgenreport per WhatsApp und Dashboard – läuft auf einem Hetzner-Server (Ubuntu) und
aktualisiert sich selbst aus diesem Repo.

**Keine Anlageberatung, keine Handelssignale.** Nur Nachrichtenlage, Termine, statistische Spanne, Einschätzung
und eine ehrliche Trefferauswertung.

## Was läuft

| Wann | Was | Datei |
|---|---|---|
| dauerhaft | Kursalarme (1 Min ≥ 10 Pips, 5 Min ≥ 20/30 Pips), stündliche Statusmeldung 8–22 Uhr, Live-Kurse fürs Dashboard | `alert/gbpjpy_alert.py` |
| Mo–Fr 06:45 | Morgenreport per WhatsApp | `fxserver/report.py` |
| Mo–Fr 23:05 | Trefferauswertung (holt verpasste Tage nach) | `fxserver/evaluate.py` |
| alle 5 Min | Kalender (stündlich), News (20 Min) + Einordnung, Renditen (3 Std), Termin-Erinnerung, Dashboard-Daten | `fxserver/refresh.py` |
| alle 10 Min | Updates aus GitHub holen, bei Fehler zurückrollen | `deploy/update.sh` |

## Datenquellen

- Kurse: Tiingo (WebSocket für Live/Alarme, REST für Tageskerzen), optional Finnhub als Reserve
- Kalender: Forex-Factory-Wochendatei (`nfs.faireconomy.media`) – liefert Forecast/Previous, **kein Actual**
- News: FXStreet, ActionForex, Reuters (über Google News), Bank of England (News, Reden), Bank of Japan
- Renditen: Bank of England (Gilt 10y/30y, Spotkurve), MoF Japan (JGB 2y/10y), US Treasury (10y) – Tagesschluss, teils 1 Tag Verzug
- Einordnung/Texte: Claude über die Anthropic-API (`CLAUDE_MODEL`, Standard `claude-sonnet-5-5`; News: `claude-haiku-5-5`)

## Prognose-Modell (`fxserver/forecast.py`)

- σ = Standardabweichung der täglichen Schlusskurs-Veränderungen (letzte 20 FX-Tage, Tagesende 17:00 New York)
- Spanne für den Schluss = Kurs um 06:45 ± σ · 0,85 (≈ 68 % erwartete Trefferquote)
- Zielkurs = Kurs + Bias-Stufe (−2 … +2) · 0,2 σ
- Kipp-Marken = Vortageshoch/-tief
- Vergleichsmodell zur Kontrolle: Zielkurs = Kurs um 06:45, Richtung = wie am Vortag

## Auf dem Server

- Code: `/opt/fx-server`, Zugangsdaten: `/etc/fx-server/env`, Daten: `/var/lib/fx-server/fx.db`, Logs: `/var/log/fx-server/`
- Dashboard: `https://<ip-mit-bindestrichen>.sslip.io/<geheim>/` (Caddy, automatisches HTTPS)
- Report sofort senden: `cd /opt/fx-server && venv/bin/python -m fxserver.cli report`
- Test-WhatsApp: `cd /opt/fx-server && venv/bin/python -m fxserver.cli test`

## Entwicklung

`python tests/run_tests.py` testet den kompletten Ablauf offline mit nachgebauten Datenquellen.
