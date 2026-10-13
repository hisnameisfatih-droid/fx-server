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
| Mo–Fr 09:00 | Tages-Setup (London-Ausbruch in Trendrichtung), danach jede Minute Live-Verfolgung mit WhatsApp bei Entry/Ziel/Stop | `fxserver/setup.py` |
| So 12:00 | Backtest der Setups mit Tiingo-Daten der letzten 2 Jahre | `fxserver/backtest.py --tiingo` |
| Mo–Fr 23:05 | Trefferauswertung (holt verpasste Tage nach) | `fxserver/evaluate.py` |
| jede Minute | Setup-Verfolgung, Kalender (stündlich), News (20 Min) + Einordnung, Renditen (3 Std), Termin-Erinnerung, Dashboard-Daten | `fxserver/refresh.py` |
| alle 10 Min | Updates aus GitHub holen, bei Fehler zurückrollen | `deploy/update.sh` |

## Datenquellen

- Kurse: Tiingo (WebSocket für Live/Alarme, REST für Tageskerzen), optional Finnhub als Reserve
- Kalender: Forex-Factory-Wochendatei (`nfs.faireconomy.media`) – liefert Forecast/Previous, **kein Actual**
- News: FXStreet, ActionForex, Reuters (über Google News), Bank of England (News, Reden), Bank of Japan
- Renditen: Bank of England (Gilt 10y/30y, Spotkurve), MoF Japan (JGB 2y/10y), US Treasury (10y) – Tagesschluss, teils 1 Tag Verzug
- Einordnung/Texte: Claude über die Anthropic-API (`CLAUDE_MODEL`, Standard `claude-sonnet-5-5`; News: `claude-haiku-5-5`)

## Prognose-Modell (`fxserver/forecast.py`) – kalibriert an GBP/JPY 2012–2022

- ATR = Ø True Range der letzten 14 FX-Tage (Tagesende 17:00 New York)
- Spanne für den Schluss = Kurs um 06:45 ± 0,52 ATR → enthielt den Schluss in 68 % (2012–18) bzw. 66–67 % (2019–22)
- Erwartetes Hoch/Tief = Kurs ± 0,34 ATR → an etwa jedem zweiten Tag erreicht
- Trend (Regel) = Vortagesschluss über/unter EMA20 und 5-Tage-Veränderung gleichgerichtet → Tagesrichtung nur ~51 % getroffen
- News-Tendenz = Einschätzung von Claude, wird separat ausgewertet

## Tages-Setup (`fxserver/strategien.py`, `fxserver/backtest.py`)

Getestet auf 15-Min-Kerzen 2012-11 bis 2022-03 (optimiert 2012–18, geprüft 2019–22, 3 Pips Kosten je Trade):
London-Ausbruch (beide Richtungen / nur in Trendrichtung), New-York-Ausbruch, Rücklauf an der Tagesspanne, Trendfolge.
Nur **London-Ausbruch in Trendrichtung** lag in beiden Zeiträumen im Plus (Ø +2,2 bzw. +4,3 Pips/Trade, 46 % Treffer,
längste Verlustserie 12). Regeln: Asien-Spanne 01:00–09:00, Breite 0,1–0,5 ATR, Kauf-/Verkauf-Stop 2 Pips außerhalb
(nur in Trendrichtung), Stop auf der Gegenseite, Ziel 2 R, Entry bis 12:00, Schluss spätestens 21:00.

Backtest lokal: `python -m fxserver.backtest --csv GBPJPYm15.csv --csv-offset-std -1`

## Auf dem Server

- Code: `/opt/fx-server`, Zugangsdaten: `/etc/fx-server/env`, Daten: `/var/lib/fx-server/fx.db`, Logs: `/var/log/fx-server/`
- Dashboard: `https://<ip-mit-bindestrichen>.sslip.io/<geheim>/` (Caddy, automatisches HTTPS)
- Report sofort senden: `cd /opt/fx-server && venv/bin/python -m fxserver.cli report`
- Test-WhatsApp: `cd /opt/fx-server && venv/bin/python -m fxserver.cli test`

## Entwicklung

`python tests/run_tests.py` testet den kompletten Ablauf offline mit nachgebauten Datenquellen.
