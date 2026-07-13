"""AI engine: turns a brain dump into tasks with tiny first steps, and plans the day.

Works with whichever provider has a key set, in this priority order:
  1. Google Gemini   — GEMINI_API_KEY   (free tier; GEMINI_MODEL, default gemini-2.0-flash)
  2. Groq            — GROQ_API_KEY     (free tier; GROQ_MODEL, default llama-3.3-70b-versatile)
  3. Anthropic Claude— ANTHROPIC_API_KEY (paid; ANTHROPIC_MODEL, default claude-opus-4-8)
  4. none            — a rule-based fallback so the app always works, key or not.

Every function degrades to the rule-based fallback if the request fails, times out,
or hits a rate limit — the app never breaks.
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


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")


def _provider():
    """Which AI backend to use, in priority order. None -> rule-based fallback."""
    if GEMINI_API_KEY:
        return "gemini"
    if GROQ_API_KEY:
        return "groq"
    if anthropic is not None and os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def enabled():
    return _provider() is not None


def provider_name():
    return {"gemini": "Gemini", "groq": "Groq", "anthropic": "Claude"}.get(_provider(), "")


def _http_json(url, payload, headers, timeout=30):
    """POST json, return parsed json. Stdlib only (no extra dependency)."""
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _strip_json(text):
    """Pull a JSON object out of a model reply that may wrap it in code fences."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    if not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j != -1:
            t = t[i:j + 1]
    return json.loads(t)


def _ask(system, user_text, schema, max_tokens=4000):
    p = _provider()
    if p is None:
        raise RuntimeError("no ai provider configured")
    if p == "anthropic":
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user_text}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text)

    # Gemini and Groq: force JSON output and describe the shape in the prompt.
    schema_hint = ("\n\nReturn ONLY a JSON object (no markdown, no prose) matching "
                   "this JSON schema:\n" + json.dumps(schema))
    if p == "groq":
        resp = _http_json(
            "https://api.groq.com/openai/v1/chat/completions",
            {
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system + schema_hint},
                    {"role": "user", "content": user_text},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": max_tokens,
                "temperature": 0.4,
            },
            {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        )
        return _strip_json(resp["choices"][0]["message"]["content"])

    # gemini
    resp = _http_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
        {
            "system_instruction": {"parts": [{"text": system + schema_hint}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "maxOutputTokens": max_tokens, "temperature": 0.4},
        },
        {"Content-Type": "application/json"},
    )
    parts = resp["candidates"][0]["content"]["parts"]
    return _strip_json("".join(pt.get("text", "") for pt in parts))


# Spiciness (à la Goblin Tools): how granular the microsteps should be.
SPICE = {
    1: ("chill", "Give each task just 2-3 broad steps. Keep it light.", 3),
    2: ("normal", "Give each task 2-5 concrete steps.", 5),
    3: ("tiny", "Give each task 5-8 TINY steps — near-comically small, one physical "
        "action each (e.g. 'Pick up your phone', 'Open the contacts app'). For a brain "
        "that's badly stuck, starting is everything.", 8),
}


def breakdown(text, spiciness=2):
    """Brain dump -> [{title, estimate_min, microsteps}]. Never raises.

    spiciness 1..3 controls how small the steps are (chill / normal / tiny).
    """
    text = (text or "").strip()
    if not text:
        return []
    level = SPICE.get(int(spiciness) if str(spiciness).isdigit() else 2, SPICE[2])
    _, guidance, cap = level
    system = SYSTEM + "\n\nStep granularity for this request: " + guidance
    try:
        data = _ask(system, f"Brain dump:\n\n{text}", BREAKDOWN_SCHEMA)
        tasks = []
        for t in data.get("tasks", [])[:MAX_TASKS]:
            steps = [s.strip() for s in t.get("microsteps", []) if s.strip()][:cap]
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
