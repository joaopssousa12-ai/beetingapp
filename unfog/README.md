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

| Variable | Required | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | recommended | Enables AI task breakdown ([console.anthropic.com](https://console.anthropic.com)) |
| `ANTHROPIC_MODEL` | no | Default `claude-opus-4-8`. Set `claude-haiku-4-5` for ~5x cheaper breakdowns in production |
| `ADMIN_TOKEN` | for waitlist export | `GET /admin/waitlist.csv?token=...` |
| `SECRET_KEY` | no | Session signing key; auto-generated and persisted next to the DB if unset |
| `DB_PATH` | on Railway/Render | e.g. `/data/unfog.db` — point it at a mounted volume |

## Deploy (Render — recommended)

This repo ships a `render.yaml` blueprint, so most of the setup is automatic:

1. Dashboard → **New +** → **Blueprint** → connect/select this repo → Apply.
2. When prompted, paste your `ANTHROPIC_API_KEY` ([console.anthropic.com](https://console.anthropic.com) → API keys).
3. Done. The blueprint creates the web service (starter plan), a 1 GB disk at `/var/data`
   for the SQLite DB, `DB_PATH`, a random `ADMIN_TOKEN`, and the `/healthz` health check.

Manual alternative (New + → Web Service): build `pip install -r requirements.txt`,
start `uvicorn app:app --host 0.0.0.0 --port $PORT`, add a Disk mounted at `/var/data`,
set `DB_PATH=/var/data/unfog.db` + `ANTHROPIC_API_KEY` + `ADMIN_TOKEN`.
Note: free instances can't have disks — the DB (accounts + waitlist!) would be wiped
on every deploy, so use starter.

Railway works too: Deploy from GitHub → Volume at `/data` → `DB_PATH=/data/unfog.db` → same variables.

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
