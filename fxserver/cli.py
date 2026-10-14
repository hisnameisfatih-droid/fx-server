"""Kleine Helfer für die Kommandozeile.

python -m fxserver.cli test     -> prüft Zugänge und schickt eine Test-WhatsApp (mit Dashboard-Link)
python -m fxserver.cli report   -> Morgenreport sofort erzeugen und senden
"""
from __future__ import annotations

import sys

import requests

from .common import ANTHROPIC_API_KEY, DASHBOARD_URL, de_zahl, get_logger, whatsapp
from .sources import prices

log = get_logger("cli")


def test() -> int:
    teile, ok = [], True
    try:
        k = prices.aktuelle_kurse(["gbpjpy"])
        teile.append(f"Tiingo ok, GBP/JPY {de_zahl(k['gbpjpy'])}")
    except Exception as e:  # noqa: BLE001
        teile.append(f"Tiingo-Fehler: {str(e)[:80]}")
        ok = False
    if ANTHROPIC_API_KEY:
        try:
            r = requests.get("https://api.anthropic.com/v1/models", timeout=20,
                             headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"})
            if r.status_code == 200:
                teile.append("Claude ok")
            else:
                teile.append(f"Claude-Key-Fehler ({r.status_code})")
                ok = False
        except requests.RequestException as e:
            teile.append(f"Claude nicht erreichbar: {str(e)[:60]}")
            ok = False
    else:
        teile.append("kein Claude-Key")
        ok = False
    from .common import TELEGRAM_BOT_TOKEN, telegram_chat_id
    if TELEGRAM_BOT_TOKEN:
        chat = telegram_chat_id(log)
        teile.append(f"Telegram ok (Chat {chat})" if chat else "Telegram: im Bot erst auf Start tippen")
    zeichen = "✅" if ok else "⚠️"
    text = (f"{zeichen} FX-Server eingerichtet\n" + "\n".join(teile) +
            "\n\nMorgenreport: Mo–Fr 06:45 Uhr\nAuswertung: Mo–Fr 23:05 Uhr")
    if DASHBOARD_URL:
        text += f"\n\n📊 Dashboard (Link geheim halten):\n{DASHBOARD_URL}"
    print(text)
    return 0 if whatsapp(text, log) and ok else 1


def main() -> int:
    befehl = sys.argv[1] if len(sys.argv) > 1 else "test"
    if befehl == "test":
        return test()
    if befehl == "report":
        from . import report
        return report.main(senden=True, ausgeben=True, erneut=True)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
