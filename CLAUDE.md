# CLAUDE.md

Guidance for AI assistants working in this repository.

## What this repo is

**Two independent applications** that happen to share a git repo. They do not import
from each other and deploy separately:

| Path | App | Stack |
|---|---|---|
| `/` (root) | **BetIQ** — sports-betting value-bet finder, bet tracker and backtest lab | FastAPI + vanilla JS, SQLite/Postgres |
| `/unfog` | **Unfog** — a planner for ADHD brains (separate product, own README) | FastAPI + vanilla JS, SQLite |

`unfog/` has its own `Procfile`, `requirements.txt`, `runtime.txt` and `railway.json`.
When deploying it, the platform's **root directory must be set to `unfog`**. Read
`unfog/README.md` before touching it — the rest of this file is about BetIQ.

Production (BetIQ): `https://beetingapp-1.onrender.com` — Render free tier.

## BetIQ architecture

```
main.py                  ← the ENTIRE API: ~90 routes + APScheduler jobs (1.3k lines, no routers)
collectors/
  database.py            ← 3.8k lines: DB access layer AND the modelling engine
  odds.py                ← The Odds API (sharp + bettable prices); the credit-budget logic
  odds_football.py       ← API-Football odds
  oddspapi.py            ← DISABLED in prod (Cloudflare 403s the Render datacentre IP)
  betfair.py, pinnacle.py← extra sharp references
  football.py, footballdata.py, tennis.py, tennisdata.py, national.py, understat.py
                         ← historical results/odds importers (backtest data only)
  elo.py, national_xg.py, recent_form.py, goals_markets.py
                         ← models
  backtest.py            ← backtest engine
  telegram_alerts.py     ← value-bet alerts + daily digest
templates/               ← Jinja2: index, match_detail, backtest, multipla
static/js/app.js         ← 2.8k lines, the whole frontend. No bundler, no framework.
scripts/                 ← offline analysis labs (never imported by the app)
tests/*.test.js          ← contract tests run with bare `node`
.github/workflows/       ← diagnostic probes against PRODUCTION (not CI)
```

**`collectors/database.py` is not just persistence.** It also contains the value-bets
engine, de-vigging, Kelly sizing, confidence scoring, the match prognosis, Elo/xG
probability, Poisson shapes and signal fusion. Expect to work in this file for almost
any change to betting logic.

## Running locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload          # http://127.0.0.1:8000
```

With no `DATABASE_URL`, it falls back to SQLite at `data/betting.db` (auto-created).
With no `ODDS_API_KEY`, collectors fail loudly but the app still serves.

Python is pinned to **3.11.9** (`runtime.txt`, `.python-version`).

## The two constraints that shape every decision

### 1. The Odds API credit budget (free tier: 500 credits/month)

This is the dominant design constraint in the codebase. Violating it silently breaks
the product for the rest of the month.

- The API bills **1 credit per (region × market)**. The collector deliberately uses
  **one region (`eu`) and `h2h` only** → 1 credit per call. Do not add regions or
  markets without doing the arithmetic first.
- **3-phase probe** in `collect_odds()` — the credit saver:
  1. `/sports` → **0 credits** (which leagues are active)
  2. `/sports/{key}/events` + kickoff window → **0 credits** (free probe)
  3. `/sports/{key}/odds` → **1 credit**, *only if* the probe found games in the window
- `IMMINENT_MIN_QUOTA` (default 50) is a **hard brake**: below it, all non-essential
  refreshes are skipped.
- Real quota is read from the `x-requests-remaining` / `x-requests-last` response
  headers and logged as `QUOTA <tag> <sport>: cost= used= remaining=`.

When adding any code path that fetches odds, state its credit cost in a comment.
Existing comments do this — match that style.

### 2. Render free tier spins down after ~15 min idle

- `ASSET_VERSION` in `main.py` is regenerated per process start to bust the static
  cache, because a hardcoded `?v=` meant deploys never reached users.
- The value-bets cache is **5 min in-memory + persisted to disk** (`vb_cache.json` in
  the volume dir), loaded at startup with a background recompute — so a cold start
  serves the last picks instantly instead of "Loading…".
- Two GitHub workflows exist purely to fight spin-down: `keepalive` (every 10 min) and
  `closing-guard` (keeps the app awake around tracked-bet kickoffs so the Pinnacle
  closing line actually gets captured).

## Database layer — read this before writing SQL

`collectors/database.py` supports **both SQLite and Postgres from one dialect**. When
`DATABASE_URL` starts with `postgres`, a wrapper translates queries at runtime.

**Always write SQLite dialect.** The translation layer handles:

| You write | Postgres gets |
|---|---|
| `?` placeholders | `%s` |
| `INSERT OR IGNORE` | `INSERT … ON CONFLICT DO NOTHING` |
| `INSERT OR REPLACE` | `INSERT … ON CONFLICT (pk) DO UPDATE SET …` |
| `datetime('now', '-1 day')` | `NOW() - INTERVAL '1 day'` |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL PRIMARY KEY` |
| `commence_time > NOW` | `CAST(commence_time AS TIMESTAMP) > NOW` |

Consequences you must respect:

- **Adding a table that uses `INSERT OR REPLACE`? Add its primary key to
  `TABLE_PRIMARY_KEYS`.** Without it the upsert silently degrades to
  `ON CONFLICT DO NOTHING` and writes are dropped.
- Rows are `DualAccessRow` — both `row["col"]` and `row[0]` work. Prefer named access.
- `commence_time` and timestamps are stored as **TEXT** in `'YYYY-MM-DD HH:MM:SS'` UTC.
  Comparisons rely on that exact format being lexicographically sortable. Compute date
  arithmetic in Python (as `purge_out_of_scope_and_stale` does) rather than in SQL when
  you need it to work on both engines.
- DDL runs with autocommit on Postgres, and "already exists" / "duplicate column"
  errors are swallowed on purpose — the schema is upgraded by re-running `init_db()`.

Tables: `football_matches`, `tennis_matches`, `tennis_odds`, `national_matches`,
`understat_matches`, `team_xg`, `national_xg`, `national_xg_meta`, `odds_events`,
`odds_history`, `elo_ratings`, `bets`, `manual_odds`, `collection_log`,
`telegram_alerts_sent`.

## The betting model — domain rules that are easy to break

These are deliberate decisions, not accidents. Do not "simplify" them.

- **Sharp vs bettable are different books.** Pinnacle (and Betfair) are the *sharp
  reference* used to derive the fair line. **1xBet (`onexbet`) is the bettable price.**
  Edge is always `soft_odd × true_prob − 1` computed on the **1xBet** price — never on
  a "best available" price at a book the user cannot bet at. This was a real bug.
- **Always de-vig.** Reading `1.04` as 96% is wrong. `remove_vig()` (proportional) and
  `remove_vig_power()` exist for this.
- `VB_MAX_HOURS_AHEAD = 48` — events beyond 48h are not analysed (early lines are
  placeholders → phantom edges).
- `PINNACLE_MAX_LIQUID_VIG = 4.0` — above 4% margin, Pinnacle has no strong info.
- `REF_AGREE_PP = 2.5` — when Pinnacle and Betfair disagree by more than 2.5pp on any
  outcome, the true probability is genuinely uncertain; don't present it as a green pick.
- **Edge > 15% returns confidence 0.** A huge edge means model error, not free money.
- Stakes are **¼-Kelly**. The exact contract between the displayed card stake and the
  Track Bet prefill is pinned by `tests/stake_consistency.test.js` — read it before
  changing any stake code.
- **Placeholder teams** (`1A`, `W73`, `3A/B/C/D`) are blocked at *storage* time. The
  logic is duplicated in `_is_placeholder_team()` (Python) and `isPlaceholderTeam()`
  (`app.js`) — **keep the two in sync.**
- CLV uses a **true ~15-min-pre-kickoff Pinnacle line**, captured by the `*/15` job and
  stored on the bet row (`pin_close_*`). `odds_history` is purged after 30 days; already
  captured closes are unaffected.

### Invariant carried over from earlier work

The **Value Bets / edge / CLV path is deliberately isolated** from the tennis-prognosis
and daily-multiple features. `get_value_bets()` must not gain references to the tennis
form/flag helpers. When adding match-analysis features, extend `get_match_prognosis()`
and `get_daily_multiple()` instead.

## Scheduler (APScheduler, started in `main.py` on startup)

| Cron | Job | Cost |
|---|---|---|
| `03:00` | `run_full_collection()` — 3 phases: live odds → historical → models+CLV | credits |
| `*/2h at :30` | `run_imminent_refresh()` — only sports with a game in the next ~6h | credits, quota-braked |
| `*/15 min` | `run_closing_capture()` — snapshot Pinnacle line near kickoff | credits |
| `09:00` | `run_daily_digest()` — one Telegram message | free |
| `04:10` | `run_history_purge()` — drop `odds_history` > 30 days | free |

`run_full_collection()` is **phased on purpose**: live odds first so Value Bets become
usable in ~1-2 min, then the slow historical imports (which only feed the backtest),
then models. Keep that ordering.

Also at startup: `init_db()`, a purge of out-of-scope/stale/placeholder events, cache
warm from disk, and an auto-bootstrap collection if the DB is empty. Every step is
wrapped so a failure logs but never crashes the process.

## Frontend conventions

- **No build step, no framework, no bundler.** `static/js/app.js` is loaded directly.
- Thresholds live in named constants at the top of their section (`VB_VALUE_FLOOR`,
  `VB_ODD_FLOOR`, `VB_GREEN_MAX_ODD`, `VB_SHORT_ODD`, `VB_HARD_CEILING`). Change the
  constant, not scattered literals.
- Odds are **decimal** everywhere, in UI and in code.

## Testing

```bash
node tests/stake_consistency.test.js
node tests/prefill_not_sticky.test.js
```

No `package.json`, no dependencies, no test runner. Each file exits non-zero on failure.

⚠️ These tests **extract functions out of `app.js` by name and brace-matching**. Renaming
or reformatting a tested function in `app.js` breaks them with
`FATAL: function X() not found`. That is intentional coupling — the tests guard real
production bugs (a stake shown as €15 while the prefill filled €77; a prefill stuck on
the previous bet's odd).

There are **no Python tests**. Backend changes are validated with the diagnostic
workflows below and the `scripts/` labs.

## GitHub workflows are diagnostics, not CI

`.github/workflows/` holds ~15 jobs that mostly **probe production over HTTP**. Only
`keepalive` and `closing-guard` are scheduled; the rest are `workflow_dispatch` only.

- Read-only / free: `diag-report`, `render-diag`, `prognosis-diag`, `trackbet-odd-diag`,
  `br-jp-diag`, `brjp-window-check`
- **Spend Odds-API credits**: `refresh-and-check`, `tennis-coverage`, `book-probe`,
  `washington-check` — check the remaining quota before running these
- Offline labs (no credits, no deploy): `model-lab`, `markets-lab`, `backtest-lab`

New diagnostics follow the house pattern: a header comment stating **what it answers,
what it costs, and that it makes no product changes**, plus a fallback to
already-deployed endpoints so a run is useful before the new code deploys.

## Conventions

- **Commit messages are written in Portuguese**, often with a short type prefix
  (`fix:`, `diag:`, `stake:`, `agendador:`) and the user-visible symptom in parentheses.
  Example: `fix: campo do Track Bet ficava preso no valor da aposta anterior (sempre 2.80)`
- **Code, comments and docstrings are in English.**
- Comments explain *why*, and frequently record the bug that motivated the code. This is
  the most valuable convention in the repo — preserve it and add to it in the same voice.
- Collectors take a `status_callback` (`cb`) used for progress lines surfaced in the UI
  collection log.
- Broad `except Exception` around collectors and startup steps is intentional (one
  failing data source must not take down the app), but each one **prints** — never add
  a silent swallow.

## Environment variables

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres; absent → SQLite at `data/betting.db` |
| `RAILWAY_VOLUME_MOUNT_PATH` | Volume dir for the SQLite DB and `vb_cache.json` |
| `ODDS_API_KEY` | The Odds API (sharp + bettable prices) |
| `ODDSPAPI_KEY` | OddsPapi — currently disabled in production |
| `BETFAIR_USERNAME` / `_PASSWORD` / `_APP_KEY` / `_KEY` / `_CERT` | Betfair Exchange |
| `PINNACLE_USERNAME` / `_PASSWORD` | Pinnacle |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Alerts and daily digest |
| `IMMINENT_MIN_QUOTA` | Hard brake on refreshes (default 50) |
| `IMMINENT_WINDOW_HOURS_FOOTBALL` / `_TENNIS` | Near-kickoff windows |
| `IMMINENT_DEDUP_FOOTBALL` / `_TENNIS` | Per-sport refresh throttle (minutes) |
| `COLLECT_WINDOW_HOURS` | Collection window |

`unfog` uses a different set — see `unfog/README.md`.

## Deployment

Both apps deploy via Nixpacks with the start command in the `Procfile`:
`uvicorn main:app --host 0.0.0.0 --port $PORT`. `railway.json` sets
`restartPolicyMaxRetries: 10`. `/healthz` answers GET **and HEAD** for uptime monitors.

Add a persistent volume and point `RAILWAY_VOLUME_MOUNT_PATH` at it, or the SQLite DB
and the value-bets disk cache are wiped on every deploy.
