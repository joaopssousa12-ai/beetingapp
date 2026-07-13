# Unfog 🫧

**The gentle planner for ADHD brains.** Dump the chaos in your head; Unfog breaks it
into tiny steps with AI and shows you exactly **one thing at a time** — with visual
timers, a "still too big" button, and streaks that never shame you.

Landing page + waitlist live at `/`, the app at `/app`. Installable as a PWA.

## Run locally

```bash
cd unfog
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # optional — without it, a rule-based splitter is used
uvicorn app:app --reload
```

Open http://127.0.0.1:8000

## Environment variables

AI is optional and provider-agnostic (priority: Gemini → Groq → Claude → rule-based
fallback). Set **one** of the AI keys — Gemini/Groq are free. See `docs/AI.md`.

| Variable | Required | What it does |
|---|---|---|
| `GEMINI_API_KEY` | one AI key (free) | Google Gemini ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)) |
| `GROQ_API_KEY` | one AI key (free) | Groq ([console.groq.com/keys](https://console.groq.com/keys)) |
| `ANTHROPIC_API_KEY` | one AI key (paid) | Anthropic Claude ([console.anthropic.com](https://console.anthropic.com)) |
| `GEMINI_MODEL` / `GROQ_MODEL` / `ANTHROPIC_MODEL` | no | Override the default model per provider |
| `DATABASE_URL` | on Render free | Postgres connection string (Neon). Falls back to SQLite if unset |
| `ADMIN_TOKEN` | for waitlist export | `GET /admin/waitlist.csv?token=...` |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` / `CRON_TOKEN` | for push | Daily nudge notifications — see `docs/NOTIFICATIONS.md` |
| `SECRET_KEY` | no | Session signing key; auto-generated and persisted next to the DB if unset |
| `DB_PATH` | no | SQLite path when not using `DATABASE_URL` (e.g. `/var/data/unfog.db` on a disk) |

## Deploy (Render free tier + Neon free Postgres — $0/month)

Render's free instances have no persistent disk, so the data lives in a free
Postgres database instead (the app switches automatically when `DATABASE_URL` is set).

**1. Database (2 min):** [neon.tech](https://neon.tech) → sign up → create project →
copy the **connection string** (`postgresql://...`).

**2. Web service:** Render Dashboard → **New +** → **Web Service** → connect this repo:

| Field | Value |
|---|---|
| Branch | `main` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app:app --host 0.0.0.0 --port $PORT` |
| Instance Type | **Free** |

**3. Environment variables:** `DATABASE_URL` (from Neon), `ADMIN_TOKEN` (any random
string), `PYTHON_VERSION` = `3.11.9`, and optionally `ANTHROPIC_API_KEY`.

**4.** Create Web Service → after the build, the app is live. Set Settings →
Health Check Path to `/healthz`. Free instances sleep after 15 idle minutes
(first hit takes ~30-60 s); an uptime monitor pinging `/healthz` every 5 min keeps it awake.

Paid alternative (no external DB): starter plan + a Disk at `/var/data` with
`DB_PATH=/var/data/unfog.db`. Railway: Volume at `/data` → `DB_PATH=/data/unfog.db`.

## Structure

```
app.py        routes: landing, waitlist, auth, Now/Dump/List/Wins, focus timer, healthz
ai.py         Anthropic structured-output breakdown + rule-based fallback
db.py         SQLite schema + helpers (swap for Postgres when beta outgrows it)
templates/    server-rendered pages (Jinja)
static/       calm CSS (light+dark), timer JS, PWA manifest + service worker
scripts/      gen_icons.py — regenerates PWA PNG icons
```

## Product principles

1. **One thing at a time** — never show a wall of tasks.
2. **First step under 2 minutes** — starting is the whole battle.
3. **No shame mechanics** — nothing turns red, streaks bend instead of breaking.
4. Unfog is a planning tool, **not** medical advice or treatment.
