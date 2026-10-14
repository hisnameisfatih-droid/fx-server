# fx-server

GBP/JPY-Kursalarm, Morgenreport per WhatsApp und Dashboard – läuft auf einem Hetzner-Server (Ubuntu) und
aktualisiert sich selbst aus diesem Repo.

**Keine Anlageberatung.** Nachrichtenlage, Termine, statistische Spanne, Einschätzung, ein mechanisches Setup und
eine mechanische Treiber-Empfehlung – jeweils mit ehrlicher Trefferauswertung.

## Was läuft

| Wann | Was | Datei |
|---|---|---|
| dauerhaft | Kursalarme (1 Min ≥ 10 Pips, 5 Min ≥ 20/30 Pips), stündliche Statusmeldung 8–22 Uhr, Live-Kurse fürs Dashboard | `alert/gbpjpy_alert.py` |
| Mo–Fr 06:45 | Morgenreport per WhatsApp | `fxserver/report.py` |
| Mo–Fr 09:00 | Tages-Setup (London-Ausbruch in Trendrichtung), danach jede Minute Live-Verfolgung mit WhatsApp bei Entry/Ziel/Stop | `fxserver/setup.py` |
| So 12:00 | Backtest der Setups mit Tiingo-Daten der letzten 2 Jahre | `fxserver/backtest.py --tiingo` |
| Mo–Fr 23:05 | Trefferauswertung (holt verpasste Tage nach) | `fxserver/evaluate.py` |
| jede Minute | Setup-Verfolgung, Kalender (stündlich), News (20 Min) + Einordnung, Renditen (3 Std), VIX/Nikkei/Brent (30 Min), CFTC (6 Std), Treiber-Score + Empfehlung, Termin-Erinnerung, Dashboard-Daten | `fxserver/refresh.py` |
| alle 10 Min | Updates aus GitHub holen, bei Fehler zurückrollen | `deploy/update.sh` |

## Nachrichten

Telegram zuerst (eigener Bot, kein Limit): `TELEGRAM_BOT_TOKEN=...` in `/etc/fx-server/env`, im Bot einmal auf Start
tippen – die Chat-ID wird automatisch erkannt. WhatsApp (CallMeBot, Gratis-Kontingent begrenzt) ist nur Ersatz;
Ohne Telegram geht alles per WhatsApp; fällt ein eingerichtetes Telegram aus, nur Wichtiges. 1-Min-Alarme derselben Richtung
höchstens alle 5 Min.

## Datenquellen

- Kurse: Tiingo (WebSocket für Live/Alarme, REST für Tageskerzen), optional Finnhub als Reserve
- Kalender: Forex-Factory-Wochendatei (`nfs.faireconomy.media`) – liefert Forecast/Previous, **kein Actual**
- News: FXStreet, ActionForex, Reuters (über Google News), Bank of England (News, Reden), Bank of Japan
- Renditen: Bank of England (Gilt 2y/10y/30y, Spotkurve), MoF Japan (JGB 2y/10y), US Treasury (2y/10y) – Tagesschluss, teils 1 Tag Verzug
- Markt: Yahoo Finance (VIX, Nikkei 225, Brent; Ersatz FRED bzw. CBOE), CFTC Commitments of Traders (Yen/Pfund, wöchentlich)
- Einordnung/Texte: Claude über die Anthropic-API (`CLAUDE_MODEL`, Standard `claude-sonnet-5-5`; News: `claude-haiku-5-5`)

## Treiber und Empfehlung (`fxserver/treiber.py`, Studie: `fxserver/treiber_studie.json`)

Auswahl nach einer Auswertung von GBP/JPY Jan 2023 – Okt 2026 (945 Handelstage, Fed-Tageskurse):
77 % der Tagesschwankung kommen über USD/JPY, 23 % über GBP/USD. Von den 40 größten Tagesbewegungen hingen
23 an Japan (BoJ 14, Intervention/Rate-Check 6, Politik 3), 13 an USA/Risikostimmung, 2 am Pfund.
BoJ-Tage bewegen 2,0×, Fed 1,7×, BoE 1,5×, US-Arbeitsmarkt 1,35×, US-Inflation 1,26× so stark wie normale Tage;
UK- und Japan-Inflation nicht stärker als normal (0,8–0,9×).

| Treiber | Gewicht | Quelle | Takt |
|---|---|---|---|
| Interventionsrisiko (USD/JPY-Niveau, Tempo, Warnungen in den News) | 20 | Tiingo + News | laufend |
| BoJ-Erwartung (JGB 2y, Veränderung 5 Tage) | 15 | MoF | täglich |
| US-Renditen (Treasury 10y) | 15 | US Treasury | täglich |
| BoE-Erwartung (Gilt 2y) | 10 | BoE | täglich |
| Zinsabstand UK–JP 2y (Carry, 20 Tage) | 5 | BoE + MoF | täglich |
| VIX (Niveau, Sprung) | 10 | Yahoo/FRED/CBOE | 30 Min |
| Nikkei 225 | 10 | Yahoo/FRED | 30 Min |
| UK-Haushaltsstress (Gilt 30y steigt + Pfund fällt) | 5 | BoE + Tiingo | täglich |
| Brent | 5 | Yahoo/FRED | 30 Min |
| CFTC-Positionierung (Extreme = Squeeze-Gefahr) | 5 | CFTC | wöchentlich |

Jeder Treiber gibt ein Signal von −1 bis +1, Score = gewichtete Summe auf −100…+100 (fehlende/veraltete Treiber
werden herausgerechnet, unter 40 % Gewicht: keine Empfehlung). Stufen: ab ±15 leicht, ab ±30 klar, 5 Punkte Puffer.
Vorrang: Interventionsalarm → „LONG MEIDEN“, 3 Std vor BoJ/Fed/BoE/UK-Haushalt → „ABWARTEN“.
WhatsApp bei Interventionsalarm oder Wechsel LONG/NEUTRAL/SHORT (Mo–Fr 7–22 Uhr, max. 3/Tag, abschaltbar mit
`TREIBER_ALARM=0`). Trefferquote: Richtung am Tag und über 5 Tage (`evaluate.py`), sichtbar im Dashboard.

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
