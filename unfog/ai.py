"""AI engine: turns a brain dump into tasks with tiny first steps.

Uses the Anthropic API with structured outputs so the JSON shape is guaranteed.
Model is configurable via ANTHROPIC_MODEL (default claude-opus-4-8; set
claude-haiku-4-5 in production if you want ~5x cheaper breakdowns).
Every function degrades to a rule-based fallback so the app never breaks
when the API key is missing or a request fails.
"""
import json
import os
import re

try:
    import anthropic
except ImportError:  # app still runs with fallback heuristics
    anthropic = None

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
MAX_TASKS = 20
MAX_STEPS = 6

SYSTEM = """You are the planning engine of Unfog, a gentle daily planner built for ADHD brains.
You receive a raw brain dump and return structured tasks.

Rules:
- Extract every actionable task the user mentioned. Never invent tasks they didn't mention.
- Write task and step titles in the same language the user wrote in.
- Each task gets 2-5 microsteps. Steps are concrete physical actions that start with a verb.
- The FIRST microstep must be doable in under 2 minutes and require zero willpower
  (e.g. "Open the document", "Put the gym bag by the door", "Find the phone number").
- ADHD brains underestimate time: give honest estimate_min for the whole task, with buffer.
- Split vague intentions into something you can physically start. No moralizing, no filler."""

BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "estimate_min": {"type": "integer"},
                    "microsteps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "estimate_min", "microsteps"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tasks"],
    "additionalProperties": False,
}

SPLIT_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
    "required": ["steps"],
    "additionalProperties": False,
}


def _client():
    if anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return anthropic.Anthropic()


def _ask(system, user_text, schema, max_tokens=4000):
    client = _client()
    if client is None:
        raise RuntimeError("no api key")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_text}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    if resp.stop_reason not in ("end_turn", "stop_sequence"):
        raise RuntimeError(f"unexpected stop_reason: {resp.stop_reason}")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def breakdown(text):
    """Brain dump -> [{title, estimate_min, microsteps}]. Never raises."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = _ask(SYSTEM, f"Brain dump:\n\n{text}", BREAKDOWN_SCHEMA)
        tasks = []
        for t in data.get("tasks", [])[:MAX_TASKS]:
            steps = [s.strip() for s in t.get("microsteps", []) if s.strip()][:MAX_STEPS]
            title = (t.get("title") or "").strip()
            if title and steps:
                tasks.append({
                    "title": title[:200],
                    "estimate_min": max(5, min(int(t.get("estimate_min", 25)), 480)),
                    "microsteps": [s[:200] for s in steps],
                })
        if tasks:
            return tasks
    except Exception:
        pass
    return _fallback_breakdown(text)


def split_step(step_title):
    """One overwhelming step -> 2-4 tinier steps. Never raises."""
    try:
        data = _ask(
            SYSTEM,
            "This single step still feels too big to start. Split it into 2-4 much "
            f"smaller steps (first one under 2 minutes):\n\n{step_title}",
            SPLIT_SCHEMA,
            max_tokens=1000,
        )
        steps = [s.strip()[:200] for s in data.get("steps", []) if s.strip()][:4]
        if len(steps) >= 2:
            return steps
    except Exception:
        pass
    return [
        f"Do just the first 2 minutes of: {step_title[:120]}",
        "Take a breath, then do 5 more minutes",
        "Mark it done — or split it again",
    ]


def _fallback_breakdown(text):
    """Rule-based splitter used when the API is unavailable."""
    parts = [p.strip(" -*•\t") for p in re.split(r"[\n;]+", text) if p.strip(" -*•\t")]
    if not parts:
        parts = [text.strip()]
    tasks = []
    for p in parts[:MAX_TASKS]:
        tasks.append({
            "title": p[:200],
            "estimate_min": 25,
            "microsteps": [
                "Open or grab what this needs (2 min)",
                "Do the smallest first piece for 5 minutes",
                "Decide the next tiny step",
            ],
        })
    return tasks
