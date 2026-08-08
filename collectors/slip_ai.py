"""Bet-slip screenshot -> structured legs, via Claude vision + structured output.

Mirrors unfog/ai.py: same SDK pattern, same graceful degrade when no API key is
configured. This module ONLY reads the image (OCR-shaped extraction) — it never
estimates a probability or judges a bet. All of that stays in the deterministic
engine in database.py, so nothing here can hallucinate a number that ends up in
front of the user as analysis.
"""
import base64
import json
import os

try:
    import anthropic
except ImportError:  # app still runs; slip analysis just reports "unavailable"
    anthropic = None

MODEL = os.environ.get("SLIP_AI_MODEL", "claude-haiku-4-5")

SYSTEM = """You read screenshots of sports betting slips (single bets or accumulators/multiples).

Extract every leg exactly as shown on the slip. Never invent a leg that is not visible,
and never merge two legs into one. If a name or number is partially unclear, give your
best reading rather than skipping the leg.

For each leg return:
- player_a, player_b: the two competitors exactly as printed (tennis players' full names,
  or football club names). Keep original spelling/accents.
- picked: which side the slip has selected — "a" (player_a/home side), "b" (player_b/away
  side), or "draw".
- odd: the decimal odd shown for that selection, as a number (e.g. 1.448).
- date, time: exactly as printed on the slip (may be relative like "Hoje", "Amanhã", or
  an explicit date/time).
- sport_guess: your best guess at the sport — "tennis", "football", or "other"."""

SCHEMA = {
    "type": "object",
    "properties": {
        "legs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "player_a": {"type": "string"},
                    "player_b": {"type": "string"},
                    "picked": {"type": "string", "enum": ["a", "b", "draw"]},
                    "odd": {"type": "number"},
                    "date": {"type": "string"},
                    "time": {"type": "string"},
                    "sport_guess": {"type": "string", "enum": ["tennis", "football", "other"]},
                },
                "required": ["player_a", "player_b", "picked", "odd", "date", "time", "sport_guess"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["legs"],
    "additionalProperties": False,
}


def _client():
    if anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return anthropic.Anthropic()


def is_configured():
    return _client() is not None


def extract_slip(image_bytes, media_type="image/jpeg"):
    """Screenshot bytes -> list of leg dicts, or None.

    None means "couldn't read it" (no API key, or the call itself failed) — the
    caller must surface that plainly, never treat it as "zero legs found"."""
    client = _client()
    if client is None:
        return None
    b64 = base64.b64encode(image_bytes).decode("ascii")
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=3000,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": "Extract every leg on this bet slip."},
                ],
            }],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        if resp.stop_reason not in ("end_turn", "stop_sequence"):
            return None
        text = next(b.text for b in resp.content if b.type == "text")
        data = json.loads(text)
        return data.get("legs", [])
    except Exception:
        return None
