"""Claude über die Anthropic-API: Report-Texte und News-Einordnung als strukturiertes JSON."""
from __future__ import annotations

import json

import requests

from .common import ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_MODEL_KLEIN, get_logger

log = get_logger("llm")
API = "https://api.anthropic.com/v1/messages"

GRUNDREGELN = (
    "Du schreibst auf Deutsch für einen erfahrenen Privatanleger, knapp und sachlich. "
    "Gib KEINE Handelssignale, keine Einstiegs- oder Ausstiegsempfehlungen, keine Positionsgrößen, "
    "keine Kursziele zum Kaufen/Verkaufen. Beschreibe nur Nachrichtenlage, Termine und eine Einschätzung. "
    "Verwende ausschließlich Zahlen und Fakten aus den gelieferten Daten; erfinde keine Werte, Zitate oder Ereignisse. "
    "Wenn Daten fehlen, sag das statt zu raten."
)


def _aufruf(model: str, system: str, nutzer: str, werkzeug: dict, max_tokens: int = 2000) -> dict:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("Kein Anthropic-API-Key")
    r = requests.post(API, timeout=120, headers={
        "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json",
    }, json={
        "model": model, "max_tokens": max_tokens, "system": system,
        "tools": [werkzeug], "tool_choice": {"type": "tool", "name": werkzeug["name"]},
        "messages": [{"role": "user", "content": nutzer}],
    })
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic-API {r.status_code}: {r.text[:300]}")
    antwort = r.json()
    for block in antwort.get("content", []):
        if block.get("type") == "tool_use":
            nutzung = antwort.get("usage", {})
            log.info("Claude %s: %s Eingabe-, %s Ausgabe-Tokens", model,
                     nutzung.get("input_tokens"), nutzung.get("output_tokens"))
            return block.get("input", {})
    raise RuntimeError("Claude-Antwort ohne Ergebnis")


REPORT_TOOL = {
    "name": "morgen_report",
    "description": "Bausteine des GBP/JPY-Morgenreports.",
    "input_schema": {
        "type": "object",
        "properties": {
            "lage_gbp": {"type": "string", "description": "1–2 Sätze: was die GBP-Seite gerade bewegt."},
            "lage_jpy": {"type": "string", "description": "1–2 Sätze: was die JPY-Seite gerade bewegt."},
            "termin_wirkungen": {
                "type": "array",
                "description": "Für jeden heutigen Termin (id aus den Daten) ein Satz zur erwarteten Wirkung auf GBP/JPY.",
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "wirkung": {"type": "string"}}, "required": ["id", "wirkung"]},
            },
            "bias_score": {"type": "integer", "minimum": -2, "maximum": 2,
                           "description": "-2 stark bearisch, -1 bearisch, 0 neutral, 1 bullisch, 2 stark bullisch (für GBP/JPY heute)."},
            "bias_grund": {"type": "string", "description": "1 Satz Begründung (max. 160 Zeichen)."},
            "bias_grund_lang": {"type": "string", "description": "2 Sätze Begründung für das Dashboard."},
            "risiken": {"type": "string", "description": "1–2 Sätze: was die Einschätzung kippen könnte."},
        },
        "required": ["lage_gbp", "lage_jpy", "termin_wirkungen", "bias_score", "bias_grund", "bias_grund_lang", "risiken"],
    },
}


def report_texte(daten: dict) -> dict:
    nutzer = (
        "Hier sind die Rohdaten für den heutigen GBP/JPY-Morgenreport (06:45 Uhr Berlin, vor London Open). "
        "Die Spanne und der Zielkurs werden statistisch berechnet – du lieferst die Einordnung und den Bias. "
        "Trenne GBP- und JPY-Seite. Halte jeden Text kurz (die WhatsApp-Nachricht ist auf 1500 Zeichen begrenzt).\n\n"
        + json.dumps(daten, ensure_ascii=False, indent=1)
    )
    return _aufruf(CLAUDE_MODEL, GRUNDREGELN, nutzer, REPORT_TOOL, max_tokens=1500)


NEWS_TOOL = {
    "name": "news_einordnung",
    "description": "Einordnung von Meldungen für einen GBP/JPY-Beobachter.",
    "input_schema": {
        "type": "object",
        "properties": {"meldungen": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"},
            "relevanz": {"type": "integer", "minimum": 0, "maximum": 10,
                         "description": "Wie wichtig für GBP/JPY (0 = egal, 10 = sehr wichtig)."},
            "waehrungen": {"type": "array", "items": {"type": "string", "enum": ["GBP", "JPY", "USD"]}},
            "wirkung": {"type": "string", "enum": ["GBP+", "GBP-", "JPY+", "JPY-", "USD+", "USD-", "neutral"],
                        "description": "Hauptwirkung: z. B. GBP+ = stützt das Pfund, JPY- = belastet den Yen."},
            "einordnung": {"type": "string", "description": "1 kurzer deutscher Satz Einordnung."},
        }, "required": ["id", "relevanz", "waehrungen", "wirkung", "einordnung"]}}},
        "required": ["meldungen"],
    },
}


def news_einordnen(meldungen: list[dict]) -> list[dict]:
    if not meldungen:
        return []
    liste = [{"id": m["id"], "quelle": m["source"], "zeit": m["ts"], "titel": m["title"],
              "text": (m.get("summary") or "")[:300]} for m in meldungen]
    nutzer = ("Ordne diese Meldungen für GBP/JPY ein. Bewerte nur, was in Titel/Text steht.\n\n"
              + json.dumps(liste, ensure_ascii=False))
    erg = _aufruf(CLAUDE_MODEL_KLEIN, GRUNDREGELN, nutzer, NEWS_TOOL, max_tokens=4000)
    return erg.get("meldungen", [])
