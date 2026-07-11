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

PLAN_SYSTEM = """You are the day-planning engine of Unfog, for ADHD brains.
Given the person's tasks (with time estimates), their energy peak, and their
waking hours, lay out a realistic time-blocked plan for TODAY.

Rules:
- Schedule the hardest / most focus-heavy tasks during their energy peak window.
  Put light admin, errands and quick wins in the low-energy troughs.
- ADHD brains underestimate time and hate being rushed: add buffer, never pack
  the day wall-to-wall, and don't schedule past their sleep hour.
- Insert short breaks (task_index -1) between focus blocks. A day with 3-5 real
  work blocks is a GOOD day — do not try to fit everything in.
- Batch similar contexts together (all calls, all errands) to cut task-switching.
- start_min and dur_min are minutes. start_min is minutes from midnight
  (e.g. 9:30 = 570). Blocks must not overlap and must run in time order.
- context is ONE short word: Focus, Admin, Calls, Errands, Home, Body, Break.
- why is a short, warm reason this block sits here (max ~12 words)."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_index": {"type": "integer"},
                    "start_min": {"type": "integer"},
                    "dur_min": {"type": "integer"},
                    "label": {"type": "string"},
                    "context": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["task_index", "start_min", "dur_min", "label", "context", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["blocks"],
    "additionalProperties": False,
}


def plan_day(tasks, energy_peak="morning", wake_hour=8, sleep_hour=23):
    """Lay tasks into a time-blocked day. Returns normalized blocks. Never raises.

    tasks: [{"title": str, "estimate_min": int}]  (order = current priority)
    Each returned block: {task_index, start_min, dur_min, label, context, why}
    task_index -1 means a break.
    """
    if not tasks:
        return []
    listing = "\n".join(
        f"{i}. {t['title']} (~{t.get('estimate_min', 25)} min)" for i, t in enumerate(tasks)
    )
    user_text = (
        f"Energy peak: {energy_peak}. Awake roughly {wake_hour}:00 to {sleep_hour}:00.\n\n"
        f"Tasks (index. title (estimate)):\n{listing}\n\n"
        "Build today's time-blocked plan."
    )
    try:
        data = _ask(PLAN_SYSTEM, user_text, PLAN_SCHEMA, max_tokens=2500)
        blocks = []
        for b in data.get("blocks", []):
            ti = int(b.get("task_index", -1))
            sm = int(b.get("start_min", 0))
            dm = max(5, min(int(b.get("dur_min", 25)), 240))
            if not (0 <= sm < 24 * 60):
                continue
            blocks.append({
                "task_index": ti if 0 <= ti < len(tasks) else -1,
                "start_min": sm,
                "dur_min": dm,
                "label": (b.get("label") or "").strip()[:120] or "Focus block",
                "context": (b.get("context") or "Focus").strip()[:20],
                "why": (b.get("why") or "").strip()[:140],
            })
        blocks.sort(key=lambda x: x["start_min"])
        if blocks:
            return blocks
    except Exception:
        pass
    return _fallback_plan(tasks, energy_peak, wake_hour, sleep_hour)


def _fallback_plan(tasks, energy_peak, wake_hour, sleep_hour):
    """Greedy scheduler used when the API is unavailable."""
    start = max(wake_hour, 0) * 60 + 30  # ease into the day, 30 min after waking
    end = min(sleep_hour, 24) * 60
    ordered = list(tasks)  # order already reflects priority; peak-heavy first
    blocks, cur, i = [], start, 0
    for n, t in enumerate(ordered):
        dur = max(15, min(int(t.get("estimate_min", 25)), 90))
        if cur + dur > end:
            break
        blocks.append({
            "task_index": n, "start_min": cur, "dur_min": dur,
            "label": t["title"][:120], "context": "Focus",
            "why": "Scheduled in priority order.",
        })
        cur += dur
        if i % 2 == 1 and cur + 10 <= end:  # a break after every 2 blocks
            blocks.append({
                "task_index": -1, "start_min": cur, "dur_min": 10,
                "label": "Breathe / move", "context": "Break", "why": "Reset before the next block.",
            })
            cur += 10
        i += 1
    return blocks


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
