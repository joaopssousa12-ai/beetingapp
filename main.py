import os
import asyncio
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from collectors.database import (
    init_db, get_connection, get_stats, get_football_summary, get_tennis_summary,
    get_recent_football, get_recent_tennis, get_collection_log, get_value_bets,
    add_bet, update_bet_result, delete_bet, get_bets, get_bet_stats,
    get_bankroll_evolution, capture_pinnacle_close_for_started_events,
    clv_audit, auto_grade_pending_bets,
    get_performance_breakdown, get_bankroll_advanced,
    get_national_summary, get_national_recent,
    get_xg_summary, get_team_xg,
    get_elo_summary, get_elo_rating, elo_based_probability,
    get_match_prognosis, get_daily_multiple,
    save_manual_odd, delete_manual_odd, get_manual_odds_map,
    invalidate_value_bets_cache,
    value_bets_cache_state, refresh_value_bets, load_vb_cache_from_disk,
    USE_POSTGRES, DATABASE_URL
)
from collectors.football import collect_football
from collectors.footballdata import collect_footballdata
from collectors.tennis import collect_tennis
from collectors.odds import collect_odds
from collectors.odds_football import collect_odds_apifootball
from collectors.national import collect_national
from collectors.national_xg import compute_national_xg
from collectors.understat import collect_understat
from collectors.elo import collect_elo
from collectors.odds_collector import collect_odds_multiple_bookmakers, collect_tennis_odds
from collectors.betfair import collect_betfair_odds
from collectors.oddspapi import collect_oddspapi
from collectors.telegram_alerts import send_alerts_for_value_bets, send_daily_digest

app = FastAPI(title="Betting Intelligence Platform")

# Cache-bust static assets on every deploy: this changes each process start, so the
# browser fetches the fresh CSS/JS instead of serving a stale cached copy (which was
# why deploys weren't reaching users — the ?v= was hardcoded and never bumped).
ASSET_VERSION = datetime.now().strftime("%Y%m%d%H%M%S")

BASE_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

collection_status = {"running": False, "messages": [], "last_run": None}
scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def startup():
    print("Starting BetIQ...", flush=True)
    print(f"DATABASE_URL set: {bool(os.environ.get('DATABASE_URL'))}", flush=True)
    try:
        init_db()
        print("DB init OK", flush=True)
    except Exception as e:
        import traceback
        print(f"DB INIT ERROR: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        # Don't crash — continue without DB
    try:
        from collectors.database import purge_out_of_scope_and_stale, purge_old_odds_history
        p = purge_out_of_scope_and_stale()
        print(f"Startup purge: removed {p['out_of_scope']} out-of-scope + {p['stale']} stale "
              f"+ {p.get('placeholders', 0)} placeholder event(s).", flush=True)
        n_hist = purge_old_odds_history()
        if n_hist:
            print(f"Startup purge: removed {n_hist} odds_history snapshot(s) >30 days old.", flush=True)
    except Exception as e:
        print(f"Startup purge skipped: {e}", flush=True)
    try:
        scheduler.add_job(lambda: asyncio.create_task(run_full_collection()), "cron", hour=3, minute=0)
        # The heavy full sweep runs ONCE a day (inside run_full_collection, now h2h-only
        # = 1 credit/call) as the baseline that gives upcoming games a price — plus
        # on-demand via /api/odds/refresh. We no longer run it every 12h: automatic
        # freshness comes from the cheap imminent + closing jobs below, so we don't burn
        # the 500/mo budget on repeated full sweeps.
        # Near-kickoff refresh: re-fetches ONLY sports with a game in the next 6h (the
        # 2-6h-before-kickoff betting window). h2h-only, per-sport throttled (30 min),
        # quota-braked. Every 2h so a game is re-priced ~3× as it approaches kickoff;
        # the */15 closing-capture handles the final pre-kickoff line.
        scheduler.add_job(lambda: asyncio.create_task(run_imminent_refresh()), "cron", hour="*/2", minute=30)
        # Closing-line capture: every 15 min, snapshot the Pinnacle line for games
        # starting in the next ~20 min and backfill pin_close_odds on tracked bets
        # whose match just kicked off. This makes the stored "close" a true
        # ~15-min-pre-kickoff line, so My Bets shows REAL CLV (not a 3-6h-old line).
        scheduler.add_job(lambda: asyncio.create_task(run_closing_capture()), "cron", minute="*/15")
        # Daily digest: one Telegram message with the day's value bets (09:00 UTC).
        scheduler.add_job(lambda: asyncio.create_task(run_daily_digest()), "cron", hour=9, minute=0)
        # Daily hygiene: drop odds_history snapshots older than 30 days (the table
        # was unbounded; captured closes live on the bets rows, so CLV is safe).
        scheduler.add_job(lambda: asyncio.create_task(run_history_purge()), "cron", hour=4, minute=10)
        scheduler.start()
        print("Scheduler started.", flush=True)
    except Exception as e:
        print(f"SCHEDULER ERROR: {e}", flush=True)

    # Warm the value-bets cache so the first request after a cold start is fast.
    # 1) Load the last computed picks from disk instantly (serve immediately).
    # 2) Recompute in the background while Render finishes spinning up, so by the
    #    time the user's request lands the cache is hot.
    try:
        load_vb_cache_from_disk()
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, refresh_value_bets)  # fire-and-forget (returns a Future)
        print("Value-bets cache warming started.", flush=True)
    except Exception as e:
        print(f"Cache warm skipped: {e}", flush=True)

    # Auto-bootstrap: if database is empty, trigger collection in background
    try:
        s = get_stats()
        if (s.get("football_matches", 0) or 0) == 0:
            print("Empty database — triggering background collection...", flush=True)
            asyncio.create_task(run_full_collection())
    except Exception as e:
        print(f"Bootstrap check skipped: {e}", flush=True)

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()

async def run_full_collection():
    collection_status["running"] = True
    collection_status["messages"] = ["Starting data collection..."]
    collection_status["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    def cb(msg):
        print(msg, flush=True)
        collection_status["messages"].append(str(msg))
        if len(collection_status["messages"]) > 300:
            collection_status["messages"] = collection_status["messages"][-300:]

    loop = asyncio.get_event_loop()

    try:
        # ════════════════════════════════════════════════════════════
        # PHASE 1 — LIVE ODDS FIRST. This is all the Value Bets page needs, so
        # do it before the slow historical/backtest imports. Value bets become
        # usable in ~1-2 min instead of waiting ~15 min behind data they don't
        # need. (The historical imports below only feed the backtest.)
        # ════════════════════════════════════════════════════════════
        cb("Phase 1/3: Live odds → Value Bets...")
        try:
            n_od = await loop.run_in_executor(None, lambda: collect_odds_apifootball(status_callback=cb))
            cb(f"  API-Football odds: {n_od} events.")
        except Exception as e:
            import traceback
            cb(f"API-FOOTBALL ODDS ERROR: {repr(e)}"); cb(traceback.format_exc()[-400:])
        try:
            n_od2 = await loop.run_in_executor(None, lambda: collect_odds(status_callback=cb))
            cb(f"  The-Odds-API (incl. live tennis): {n_od2} events.")
        except Exception as e:
            import traceback
            cb(f"ODDS ERROR: {repr(e)}"); cb(traceback.format_exc()[-400:])
        # OddsPapi DISABLED: its API 403-blocks the Render datacentre IP (Cloudflare),
        # so it can't be reached from prod. The Odds API now supplies both the sharp
        # (Pinnacle) and bettable (1xBet/onexbet) sides via parse_and_store.
        try:
            n_bf = await loop.run_in_executor(None, lambda: collect_betfair_odds(status_callback=cb))
            if n_bf:
                cb(f"  Betfair Exchange (sharp ref): {n_bf} events.")
        except Exception as e:
            cb(f"Betfair ERROR (non-critical): {repr(e)}")
        # Value bets are ready now — refresh the cache so the page shows them fast.
        try:
            invalidate_value_bets_cache()
            await loop.run_in_executor(None, get_value_bets)
            cb("✓ VALUE BETS READY. Historical/backtest data continues below (you can use the site now).")
        except Exception as e:
            cb(f"Value-bets warm error: {repr(e)}")

        # ════════════════════════════════════════════════════════════
        # PHASE 2 — HISTORICAL (backtest data only). Slow; runs AFTER value bets
        # are already live, so it never blocks them. Off-season leagues simply
        # return no data — that's expected, not an error.
        # ════════════════════════════════════════════════════════════
        cb("Phase 2/3: Historical data (for the backtest only)...")
        try:
            n_fb = await loop.run_in_executor(None, lambda: collect_football(status_callback=cb))
            cb(f"  Football results: {n_fb} rows.")
        except Exception as e:
            import traceback
            cb(f"FOOTBALL ERROR: {repr(e)}"); cb(traceback.format_exc()[-400:])
        try:
            res_fd = await loop.run_in_executor(None, lambda: collect_footballdata(status_callback=cb))
            cb(f"  Football odds: {res_fd.get('rows',0)} rows, {res_fd.get('with_odds',0)} with odds.")
        except Exception as e:
            import traceback
            cb(f"FOOTBALLDATA ERROR: {repr(e)}"); cb(traceback.format_exc()[-400:])
        try:
            n_tn = await loop.run_in_executor(None, lambda: collect_tennis(start_year=2015, status_callback=cb))
            cb(f"  Tennis results: {n_tn} rows.")
        except Exception as e:
            import traceback
            cb(f"TENNIS ERROR: {repr(e)}"); cb(traceback.format_exc()[-400:])
        try:
            from collectors.tennisdata import collect_tennisdata
            res_td = await loop.run_in_executor(None, lambda: collect_tennisdata(status_callback=cb))
            cb(f"  Tennis odds: {res_td.get('rows',0)} rows, {res_td.get('with_odds',0)} with odds.")
        except Exception as e:
            import traceback
            cb(f"TENNISDATA ERROR: {repr(e)}"); cb(traceback.format_exc()[-400:])
        try:
            n_nat = await loop.run_in_executor(None, lambda: collect_national(since_year=2010, status_callback=cb))
            cb(f"  National teams: {n_nat} rows.")
        except Exception as e:
            import traceback
            cb(f"NATIONAL ERROR: {repr(e)}"); cb(traceback.format_exc()[-400:])
        try:
            n_us = await loop.run_in_executor(None, lambda: collect_understat(status_callback=cb))
            cb(f"  Understat xG: {n_us} matches.")
        except Exception as e:
            import traceback
            cb(f"UNDERSTAT ERROR: {repr(e)}"); cb(traceback.format_exc()[-400:])

        # ════════════════════════════════════════════════════════════
        # PHASE 3 — MODELS + CLV (computed from the freshly collected data)
        # ════════════════════════════════════════════════════════════
        cb("Phase 3/3: Models (Elo, national xG) + CLV...")
        try:
            n_nxg = await loop.run_in_executor(None, lambda: compute_national_xg(status_callback=cb))
            cb(f"  National xG: {n_nxg} teams indexed.")
        except Exception as e:
            import traceback
            cb(f"NATIONAL XG ERROR: {repr(e)}"); cb(traceback.format_exc()[-400:])
        try:
            await loop.run_in_executor(None, lambda: collect_elo(status_callback=cb))
        except Exception as e:
            import traceback
            cb(f"ELO ERROR: {repr(e)}"); cb(traceback.format_exc()[-400:])
        try:
            n = await loop.run_in_executor(None, capture_pinnacle_close_for_started_events)
            if n > 0:
                cb(f"✓ Captured Pinnacle closing line for {n} past bet(s).")
        except Exception as e:
            cb(f"CLV capture error: {repr(e)}")
        # Models refreshed → re-warm value bets so confidence reflects them.
        try:
            invalidate_value_bets_cache()
            await loop.run_in_executor(None, get_value_bets)
        except Exception as e:
            cb(f"Value-bets re-warm error: {repr(e)}")

        cb("✓ All collection finished.")

    except Exception as e:
        import traceback
        cb(f"FATAL ERROR: {repr(e)}")
        cb(traceback.format_exc()[-800:])
    finally:
        collection_status["running"] = False

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "v": ASSET_VERSION})

@app.get("/match/{event_id}", response_class=HTMLResponse)
async def match_detail(request: Request, event_id: str):
    """Show detailed page for a single match."""
    # Get match info from value_bets
    all_bets = get_value_bets()
    match = next((b for b in all_bets if b.get("event_id") == event_id), None)
    if not match:
        return HTMLResponse("<h1>Match not found</h1><a href='/'>Back</a>", status_code=404)
    
    return templates.TemplateResponse("match_detail.html", {
        "request": request,
        "event_id": event_id,
        "home_team": match.get("home_team", "Unknown"),
        "away_team": match.get("away_team", "Unknown"),
        "sport_name": match.get("sport_name", ""),
        "commence_time": match.get("commence_time", ""),
    })

@app.get("/api/match/{event_id}")
async def api_match_detail(event_id: str):
    """Return full data for a specific match."""
    all_bets = get_value_bets()
    match = next((b for b in all_bets if b.get("event_id") == event_id), None)
    if not match:
        return JSONResponse({"error": "Match not found"}, status_code=404)
    return JSONResponse(match)

@app.get("/api/match/{event_id}/prognosis")
async def api_match_prognosis(event_id: str):
    """Pure-model prognosis (NO edge/CLV) for the match-analysis view: Elo probs,
    recent form, H2H, goals-Poisson O/U + likely scorelines, confidence tier and
    best pick. Deliberately separate from the value-bets/traffic-light system."""
    match = next((b for b in get_value_bets() if b.get("event_id") == event_id), None)
    if not match:
        return JSONResponse({"error": "Match not found"}, status_code=404)
    prog = get_match_prognosis(match.get("home_team"), match.get("away_team"),
                               match.get("sport_name"), match.get("commence_time"),
                               pin_odds=(match.get("pin_home"), match.get("pin_draw"),
                                         match.get("pin_away")))
    return JSONResponse(prog)

@app.get("/api/daily-multiple")
async def api_daily_multiple():
    """Highest-confidence, model-agreeing picks combined into a 'Múltipla do Dia'.
    FOR FUN ONLY — an accumulator is -EV; this is not part of the edge system."""
    return JSONResponse(get_daily_multiple())

@app.get("/api/stats")
async def api_stats():
    return JSONResponse(get_stats())

@app.get("/api/football/summary")
async def api_football_summary():
    return JSONResponse(get_football_summary())

@app.get("/api/tennis/summary")
async def api_tennis_summary():
    return JSONResponse(get_tennis_summary())

@app.get("/api/national/summary")
async def api_national_summary():
    return JSONResponse(get_national_summary())

@app.get("/api/national/recent")
async def api_national_recent(limit: int = 50, team: str = None):
    return JSONResponse(get_national_recent(limit, team))

@app.get("/api/xg/summary")
async def api_xg_summary():
    return JSONResponse(get_xg_summary())

@app.get("/api/xg/team/{team}")
async def api_xg_team(team: str):
    data = get_team_xg(team)
    if not data:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(data)

@app.get("/api/elo/summary")
async def api_elo_summary():
    return JSONResponse(get_elo_summary())

@app.get("/api/football/recent")
async def api_football_recent(limit: int = 50):
    return JSONResponse(get_recent_football(limit))

@app.get("/api/tennis/recent")
async def api_tennis_recent(limit: int = 50):
    return JSONResponse(get_recent_tennis(limit))

@app.get("/api/collection/log")
async def api_collection_log():
    return JSONResponse(get_collection_log())

@app.get("/api/collection/status")
async def api_collection_status():
    return JSONResponse({
        "running": collection_status["running"],
        "messages": collection_status["messages"][-50:],
        "last_run": collection_status["last_run"]
    })

@app.post("/api/collection/start")
async def api_start_collection(full: bool = False):
    """Default = FAST: refresh live odds + value bets only (~1-2 min). Pass
    ?full=true for the complete run incl. historical/backtest data (~10-15 min,
    also runs automatically every night at 3am)."""
    if collection_status["running"]:
        return JSONResponse({"ok": False, "msg": "Already running."})
    if full:
        asyncio.create_task(run_full_collection())
        return JSONResponse({"ok": True, "msg": "Full collection started (incl. historical)."})
    asyncio.create_task(run_odds_only())
    return JSONResponse({"ok": True, "msg": "Fast refresh started (live odds + value bets)."})


async def _run_footballdata_only():
    collection_status["running"] = True
    collection_status["messages"] = ["Importing historical odds (football-data.co.uk)..."]
    collection_status["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    def cb(msg):
        collection_status["messages"].append(msg)
    loop = asyncio.get_event_loop()
    try:
        res = await loop.run_in_executor(None, lambda: collect_footballdata(status_callback=cb))
        cb(f"DONE: {res.get('rows',0)} rows, {res.get('with_odds',0)} with usable odds. "
           f"Now go to /backtest and Run Simulation.")
    except Exception as e:
        import traceback
        cb(f"ODDS IMPORT ERROR: {repr(e)}")
        print(f"FOOTBALLDATA_ERROR: {traceback.format_exc()}", flush=True)
    finally:
        collection_status["running"] = False


@app.post("/api/collection/odds-history")
async def api_collect_odds_history():
    """Import ONLY the historical odds (fast path to make the backtest usable)."""
    if collection_status["running"]:
        return JSONResponse({"ok": False, "msg": "Already running."})
    asyncio.create_task(_run_footballdata_only())
    return JSONResponse({"ok": True, "msg": "Historical odds import started."})


async def _run_tennisdata_only():
    collection_status["running"] = True
    collection_status["messages"] = ["Importing historical tennis odds (tennis-data.co.uk)..."]
    collection_status["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    def cb(msg):
        collection_status["messages"].append(msg)
    loop = asyncio.get_event_loop()
    try:
        from collectors.tennisdata import collect_tennisdata
        res = await loop.run_in_executor(None, lambda: collect_tennisdata(status_callback=cb))
        cb(f"DONE: {res.get('rows',0)} rows, {res.get('with_odds',0)} with usable odds. "
           f"Now go to /backtest, switch to Tennis and Run Simulation.")
    except Exception as e:
        import traceback
        cb(f"TENNIS ODDS IMPORT ERROR: {repr(e)}")
        print(f"TENNISDATA_ERROR: {traceback.format_exc()}", flush=True)
    finally:
        collection_status["running"] = False


@app.post("/api/collection/tennis-odds")
async def api_collect_tennis_odds():
    """Import historical tennis odds (tennis-data.co.uk) for the tennis backtest."""
    if collection_status["running"]:
        return JSONResponse({"ok": False, "msg": "Already running."})
    asyncio.create_task(_run_tennisdata_only())
    return JSONResponse({"ok": True, "msg": "Tennis odds import started."})


@app.get("/api/diag/odds-sports")
async def api_diag_odds_sports():
    """Ground-truth diagnostic for LIVE tennis: what does The Odds API expose and
    return right now? Open this URL in your browser and send me the JSON."""
    from collectors.odds import diagnose_tennis
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, diagnose_tennis)
        return JSONResponse(result)
    except Exception as e:
        import traceback
        print(f"DIAG_ERROR: {e}\n{traceback.format_exc()}", flush=True)
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/diag/xg")
async def api_diag_xg():
    """Diagnostic for the xG home/away report: market vs xG vs Elo per national/WC
    game + gf/ga + whether the xG favourite matches the market favourite."""
    from collectors.database import diagnose_national_xg
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, diagnose_national_xg)
        return JSONResponse(result)
    except Exception as e:
        import traceback
        print(f"DIAG_ERROR (xg): {e}\n{traceback.format_exc()}", flush=True)
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/diag/spreads")
async def api_diag_spreads(sport: str = "soccer_fifa_world_cup"):
    """Does The Odds API return Asian Handicap (spreads) for these games? Open in
    browser to confirm World Cup handicaps can populate. Send me the JSON."""
    from collectors.odds import diagnose_spreads
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: diagnose_spreads(sport))
        return JSONResponse(result)
    except Exception as e:
        import traceback
        print(f"DIAG_ERROR (spreads): {e}\n{traceback.format_exc()}", flush=True)
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/diag/betfair")
async def api_diag_betfair():
    """Ground-truth diagnostic for Betfair login (the A+B 2nd source). Open this
    URL in your browser and send me the JSON — it shows the EXACT login error."""
    from collectors.betfair import diagnose_betfair
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, diagnose_betfair)
        return JSONResponse(result)
    except Exception as e:
        import traceback
        print(f"DIAG_ERROR (betfair): {e}\n{traceback.format_exc()}", flush=True)
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)

@app.get("/api/migrate")
@app.post("/api/migrate")
async def api_migrate_data():
    """Migrate data from SQLite (local) to PostgreSQL (Supabase)."""
    if not USE_POSTGRES:
        return JSONResponse({"ok": False, "msg": "Not using PostgreSQL."}, status_code=400)
    
    try:
        from collectors.database import DB_PATH
        import sqlite3
        import psycopg2
        
        # Try to open the old SQLite database
        if not os.path.exists(DB_PATH):
            return JSONResponse({"ok": False, "msg": "SQLite database not found."}, status_code=404)
        
        sqlite_conn = sqlite3.connect(DB_PATH)
        sqlite_conn.row_factory = sqlite3.Row
        sqlite_cur = sqlite_conn.cursor()
        
        postgres_conn = psycopg2.connect(DATABASE_URL)
        postgres_cur = postgres_conn.cursor()
        
        tables = [
            "football_matches", "tennis_matches", "national_matches", "understat_matches",
            "team_xg", "elo_ratings", "odds_events", "odds_history", "bets", "collection_log"
        ]
        
        total = 0
        for table in tables:
            try:
                sqlite_cur.execute(f"PRAGMA table_info({table})")
                columns = [row[1] for row in sqlite_cur.fetchall()]
                if not columns:
                    continue
                
                sqlite_cur.execute(f"SELECT * FROM {table}")
                rows = sqlite_cur.fetchall()
                if not rows:
                    continue
                
                cols_str = ", ".join(columns)
                placeholders = ", ".join(["%s"] * len(columns))
                insert_sql = f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
                
                values = [tuple(row) for row in rows]
                postgres_cur.executemany(insert_sql, values)
                postgres_conn.commit()
                total += len(rows)
            except Exception as e:
                print(f"Migrate {table} error: {e}", flush=True)
        
        sqlite_conn.close()
        postgres_conn.close()
        
        return JSONResponse({"ok": True, "msg": f"Migrated {total} rows from SQLite to PostgreSQL"})
    except Exception as e:
        import traceback
        return JSONResponse(
            {"ok": False, "msg": f"Migration error: {str(e)}", "trace": traceback.format_exc()[-500:]},
            status_code=500
        )

async def run_odds_only():
    collection_status["running"] = True
    collection_status["messages"] = ["Refreshing live odds..."]
    collection_status["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    def cb(msg):
        print(msg, flush=True)
        collection_status["messages"].append(str(msg))
    loop = asyncio.get_event_loop()
    try:
        # API-Football + multi-source (World Cup, leagues) — always runs
        cb("Fetching football odds (multi-source)...")
        try:
            await loop.run_in_executor(None, lambda: collect_odds_apifootball(status_callback=cb))
        except Exception as e:
            import traceback
            cb(f"API-FOOTBALL ERROR: {repr(e)}")
            cb(traceback.format_exc()[-400:])
        # The Odds API (covers tennis, MMA, etc.)
        await loop.run_in_executor(None, lambda: collect_odds(status_callback=cb))
        
        # The-Odds-API for multiple bookmakers (NEW v2)
        cb("Fetching odds from The-Odds-API (multiple bookmakers)...")
        try:
            football_odds = await loop.run_in_executor(None, collect_odds_multiple_bookmakers)
            tennis_odds = await loop.run_in_executor(None, collect_tennis_odds)
            cb(f"✓ Collected {len(football_odds)} football odds + {len(tennis_odds)} tennis odds from API")
        except Exception as e:
            cb(f"The-Odds-API ERROR (non-critical): {repr(e)}")
        # OddsPapi DISABLED — 403-blocked from the Render datacentre IP (see startup note).
        # Betfair Exchange — primary reference for liquid markets (peer-to-peer, 0% margin)
        try:
            n_bf = await loop.run_in_executor(None, lambda: collect_betfair_odds(status_callback=cb))
            if n_bf > 0:
                cb(f"✓ Betfair Exchange: {n_bf} events updated.")
        except Exception as e:
            cb(f"Betfair ERROR (non-critical): {repr(e)}")
        # Auto-capture CLV for past bets
        try:
            n = await loop.run_in_executor(None, capture_pinnacle_close_for_started_events)
            if n > 0:
                cb(f"✓ Captured Pinnacle closing line for {n} past bet(s).")
        except Exception as e:
            cb(f"CLV capture error: {repr(e)}")
        # Auto-grade pending bets from historical results
        try:
            n_ag = await loop.run_in_executor(None, auto_grade_pending_bets)
            if n_ag > 0:
                cb(f"✓ Auto-graded {n_ag} pending bet(s) from match results.")
        except Exception as e:
            cb(f"Auto-grade error: {repr(e)}")
        cb("✓ Odds refresh finished.")
        invalidate_value_bets_cache()
        # Telegram alerts — send for any new value bets (deduped per 20h)
        try:
            loop = asyncio.get_event_loop()
            vb = await loop.run_in_executor(None, get_value_bets)
            await loop.run_in_executor(None, lambda: send_alerts_for_value_bets(vb, status_callback=cb))
        except Exception as e:
            cb(f"Telegram alerts error (non-critical): {repr(e)}")
    except Exception as e:
        import traceback
        cb(f"ODDS ERROR: {repr(e)}")
        cb(traceback.format_exc()[-800:])
    finally:
        collection_status["running"] = False

async def run_imminent_refresh():
    """Near-kickoff refresh (cron */3h). Cheap + quota-guarded; only re-fetches
    sports with matches starting soon to capture the near-closing line."""
    if collection_status.get("running"):
        return
    loop = asyncio.get_event_loop()
    try:
        from collectors.odds import refresh_imminent_odds
        n = await loop.run_in_executor(None, lambda: refresh_imminent_odds())
        if n and n > 0:
            invalidate_value_bets_cache()
            vb = await loop.run_in_executor(None, get_value_bets)  # warm + use for alerts
            try:
                await loop.run_in_executor(None, lambda: send_alerts_for_value_bets(vb))
            except Exception as e:
                print(f"IMMINENT alerts error: {e}", flush=True)
    except Exception as e:
        import traceback
        print(f"IMMINENT_REFRESH ERROR: {e}\n{traceback.format_exc()[-400:]}", flush=True)


async def run_closing_capture():
    """Every 15 min: capture the near-closing Pinnacle line for imminent games and
    fill pin_close_odds on tracked bets whose match just started — fully automatic.

    Flow:
      1) refresh_imminent_odds(within_minutes=20) → re-fetch only sports with a game
         in the next ~20 min, writing a fresh odds_history snapshot (quota-guarded;
         hits the API only when something is actually about to start).
      2) capture_pinnacle_close_for_started_events() → for every bet whose
         commence_time has just passed, store the LAST pre-kickoff Pinnacle snapshot
         as pin_close_odds. Real CLV = (your_odds / pin_close - 1), shown in My Bets
         and /api/backtest/clv. The user only registers the bet + result; this does
         the rest."""
    if collection_status.get("running"):
        return
    loop = asyncio.get_event_loop()
    try:
        from collectors.odds import refresh_imminent_odds
        # 1) Snapshot the near-close line for games starting in the next ~20 min.
        await loop.run_in_executor(None, lambda: refresh_imminent_odds(within_minutes=20))
        # 2) Backfill pin_close_odds for any bet whose game has now kicked off.
        n = await loop.run_in_executor(None, capture_pinnacle_close_for_started_events)
        if n and n > 0:
            print(f"CLOSING_CAPTURE: filled pin_close_odds for {n} bet(s).", flush=True)
    except Exception as e:
        import traceback
        print(f"CLOSING_CAPTURE ERROR: {e}\n{traceback.format_exc()[-400:]}", flush=True)


async def run_history_purge():
    """Daily (04:10 UTC): delete odds_history snapshots older than 30 days."""
    loop = asyncio.get_event_loop()
    try:
        from collectors.database import purge_old_odds_history
        n = await loop.run_in_executor(None, purge_old_odds_history)
        if n:
            print(f"HISTORY_PURGE: removed {n} odds_history snapshot(s) >30 days old.", flush=True)
    except Exception as e:
        print(f"HISTORY_PURGE ERROR: {e}", flush=True)


async def run_daily_digest():
    """Daily Telegram digest of the day's value bets (cron 09:00 UTC)."""
    loop = asyncio.get_event_loop()
    try:
        vb = await loop.run_in_executor(None, get_value_bets)
        await loop.run_in_executor(None, lambda: send_daily_digest(vb))
    except Exception as e:
        import traceback
        print(f"DAILY_DIGEST ERROR: {e}\n{traceback.format_exc()[-300:]}", flush=True)


@app.post("/api/telegram/digest")
async def api_telegram_digest():
    """Send the daily value-bets digest now (manual trigger / test)."""
    loop = asyncio.get_event_loop()
    try:
        vb = await loop.run_in_executor(None, get_value_bets)
        n = await loop.run_in_executor(None, lambda: send_daily_digest(vb))
        return JSONResponse({"ok": bool(n), "msg": "Digest sent." if n else "No value bets in the next 24h (or Telegram not configured)."})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)})


@app.post("/api/odds/refresh")
@app.get("/api/odds/refresh")
async def api_refresh_odds():
    if collection_status["running"]:
        return JSONResponse({"ok": False, "msg": "Already running."})
    asyncio.create_task(run_odds_only())
    return JSONResponse({"ok": True, "msg": "Odds refresh started."})

@app.post("/api/telegram/test")
async def api_telegram_test():
    """Send test Telegram alerts for all current value bets (ignores dedup)."""
    try:
        from collectors.telegram_alerts import _send
        ok = _send("✅ <b>BetIQ</b> — Telegram configurado com sucesso!")
        if not ok:
            return JSONResponse({"ok": False, "msg": "Failed — check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Render env vars."})
        loop = asyncio.get_event_loop()
        vb = await loop.run_in_executor(None, get_value_bets)
        n = await loop.run_in_executor(None, lambda: send_alerts_for_value_bets(vb))
        return JSONResponse({"ok": True, "msg": f"Test message sent. {n} value bet alert(s) dispatched."})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)})

@app.get("/api/oddspapi/test")
async def test_oddspapi():
    """ACTUALLY call OddsPapi and report what it returns (status, remaining credits,
    sample, whether Pinnacle is present) — so we know WHY it gave 0."""
    from collectors.oddspapi import diagnose_oddspapi
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, diagnose_oddspapi)
        # Also report how many DB events currently carry a Pinnacle price (any source).
        try:
            conn = get_connection()
            row = conn.execute("SELECT COUNT(*) AS c FROM odds_events WHERE pin_home IS NOT NULL").fetchone()
            result["db_events_with_pinnacle"] = (row["c"] if row else 0)
            conn.close()
        except Exception:
            pass
        return JSONResponse(result)
    except Exception as e:
        import traceback
        print(f"DIAG_ERROR (oddspapi): {e}\n{traceback.format_exc()}", flush=True)
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/diag/quota")
async def api_diag_quota():
    """Live quota counter for the paid/limited The Odds API + the brake state.
    `remaining` is the last value seen on an x-requests-remaining header; it resets
    to null after a cold start until the next fetch. brake_active=true means all
    non-essential refreshes (imminent/closing) are being skipped."""
    from collectors.odds import _LAST_REMAINING, IMMINENT_MIN_QUOTA
    rem = _LAST_REMAINING.get("v")
    return JSONResponse({
        "the_odds_api": {
            "remaining": rem,
            "brake_threshold": IMMINENT_MIN_QUOTA,
            "brake_active": (rem is not None and rem < IMMINENT_MIN_QUOTA),
            "note": "null = unknown until next fetch (resets on redeploy/cold start).",
        },
        "free_sources": {
            "oddspapi": "see /api/oddspapi/test for live status + remaining",
            "betfair": "see /api/diag/betfair (free; needs BETFAIR_CERT/KEY)",
        },
    })

@app.get("/api/diag/books")
async def api_diag_books(sport: str = None):
    """1-credit probe: fetch ONE sport (auto-picked, or ?sport=<key>) with ALL EU
    bookmakers and rank which book most often has the best h2h price, how often +
    by how much each beats 1xBet (avg_beat_pct = edge size when it wins;
    exp_uplift_pct = freq×size = expected raw price improvement per bet)."""
    from collectors.odds import probe_all_books
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, lambda: probe_all_books(sport))
    return JSONResponse(res)

@app.get("/api/value-bets")
async def api_value_bets():
    loop = asyncio.get_event_loop()
    try:
        # Stale-while-revalidate: serve cached picks instantly (even if a little
        # stale) and refresh in the background, so the page never blocks on the
        # engine. Only compute synchronously when there is no cache at all.
        data, stale = value_bets_cache_state()
        if data is not None:
            if stale:
                loop.run_in_executor(None, refresh_value_bets)  # fire-and-forget refresh
            return JSONResponse(data)
        result = await loop.run_in_executor(None, get_value_bets)
        return JSONResponse(result)
    except Exception as e:
        import traceback
        print(f"VALUE_BETS_ERROR: {e}\n{traceback.format_exc()}", flush=True)
        # Degrade gracefully — the UI shows "No live odds yet" on an empty list
        # rather than a blank landing page.
        return JSONResponse([])


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    """Ultra-cheap liveness check (no DB). Point an external uptime pinger here
    (e.g. UptimeRobot every 5 min) to stop Render's free tier from sleeping.
    Accepts HEAD as well as GET: uptime monitors often default to HEAD, and a
    GET-only route returned 405 to those checks → the monitor read the service
    as permanently DOWN even though the app was serving 200 to GET."""
    return JSONResponse({"ok": True})

@app.get("/api/value-bets/{event_id}")
async def api_event_detail(event_id: str):
    loop = asyncio.get_event_loop()
    all_bets = await loop.run_in_executor(None, get_value_bets)
    match = next((b for b in all_bets if b.get("event_id") == event_id), None)
    if not match:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(match)


@app.get("/api/debug/pin-odds")
def debug_pin_odds():
    try:
        conn = get_connection()

        # Count rows in key tables
        def count(table):
            try:
                return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception as e:
                return f"ERROR: {e}"

        counts = {
            "odds_events": count("odds_events"),
            "football_matches": count("football_matches"),
            "national_matches": count("national_matches"),
            "bets": count("bets"),
        }

        # Sample of odds_events (all rows, no filter)
        rows = conn.execute(
            "SELECT event_id, home_team, away_team, pin_home, pin_draw, pin_away "
            "FROM odds_events LIMIT 20"
        ).fetchall()
        conn.close()

        data = []
        for r in rows:
            try:
                data.append({
                    "event_id": r["event_id"],
                    "home_team": r["home_team"],
                    "away_team": r["away_team"],
                    "pin_home": r["pin_home"],
                    "pin_draw": r["pin_draw"],
                    "pin_away": r["pin_away"],
                    "pin_missing": r["pin_home"] is None,
                })
            except Exception as row_err:
                data.append({"row_error": str(row_err), "raw": str(r)})

        return {"status": "ok", "table_counts": counts, "odds_events_sample": data}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ========== BET TRACKER ENDPOINTS ==========

@app.post("/api/manual-odds")
async def api_save_manual_odd(request: Request):
    """Save or update a manual odd for an event/selection."""
    data = await request.json()
    eid = data.get("event_id")
    sel = data.get("selection")
    odd = data.get("odd")
    if not eid or not sel:
        return JSONResponse({"ok": False, "msg": "Missing event_id or selection"}, status_code=400)
    if odd is None or float(odd) <= 1:
        # Delete it instead
        delete_manual_odd(eid, sel)
        return JSONResponse({"ok": True, "msg": "Removed"})
    save_manual_odd(eid, sel, float(odd))
    return JSONResponse({"ok": True, "msg": "Saved"})


@app.delete("/api/manual-odds")
async def api_delete_manual_odd(request: Request):
    data = await request.json()
    eid = data.get("event_id")
    sel = data.get("selection")
    delete_manual_odd(eid, sel)
    return JSONResponse({"ok": True})


@app.post("/api/bets")
async def api_create_bet(request: Request):
    data = await request.json()
    bet_id = add_bet(data)
    return JSONResponse({"ok": True, "id": bet_id})

@app.get("/api/bets")
async def api_list_bets(limit: int = 200):
    return JSONResponse(get_bets(limit))

@app.post("/api/bets/{bet_id}/settle")
async def api_settle_bet(bet_id: int, request: Request):
    data = await request.json()
    result = data.get("result")  # "won" | "lost" | "push"
    if result not in ("won", "lost", "push"):
        return JSONResponse({"ok": False, "msg": "Invalid result"}, status_code=400)
    ok = update_bet_result(bet_id, result)
    return JSONResponse({"ok": ok})

@app.delete("/api/bets/{bet_id}")
async def api_delete_bet(bet_id: int):
    delete_bet(bet_id)
    return JSONResponse({"ok": True})

@app.post("/api/clv/capture")
@app.get("/api/clv/capture")  # GET alias — callable from a tablet/browser address bar
async def api_capture_clv(recapture: bool = False):
    """Manually trigger CLV capture for past pending bets.

    recapture=true re-derives the close for ALL bets with an event_id using the
    strict pre-kickoff logic — repairs closes captured by the old string-comparison
    bug (which could store a live in-play price as the "closing line"). Bets with
    no genuine pre-kickoff snapshot get their close cleared back to pending."""
    try:
        n = capture_pinnacle_close_for_started_events(recapture=recapture)
        return JSONResponse({"ok": True, "captured": n, "recapture": recapture})
    except Exception as e:
        return JSONResponse({"ok": False, "error": repr(e)}, status_code=500)


@app.get("/api/bets/{bet_id}/clv-audit")
async def api_bet_clv_audit(bet_id: int):
    """Step-by-step CLV audit for one bet: entry odd vs the captured Pinnacle close,
    when the close was captured relative to kickoff, the CLV math, and the full
    Pinnacle snapshot timeline for the event (pre/post-kickoff flagged)."""
    data = clv_audit(bet_id)
    if not data:
        return JSONResponse({"ok": False, "error": "bet not found"}, status_code=404)
    return JSONResponse(data)

@app.post("/api/bets/auto-grade")
async def api_auto_grade():
    """Auto-settle pending bets by looking up results in historical match data."""
    loop = asyncio.get_event_loop()
    try:
        n = await loop.run_in_executor(None, auto_grade_pending_bets)
        return JSONResponse({"ok": True, "graded": n})
    except Exception as e:
        return JSONResponse({"ok": False, "error": repr(e)}, status_code=500)

@app.get("/api/bet-stats")
async def api_bet_stats():
    # Try to capture any missing Pinnacle close odds first
    try:
        capture_pinnacle_close_for_started_events()
    except Exception:
        pass
    return JSONResponse(get_bet_stats())

@app.get("/api/bankroll")
async def api_bankroll():
    return JSONResponse(get_bankroll_evolution())

@app.get("/api/performance/breakdown")
async def api_performance_breakdown():
    try:
        return JSONResponse(get_performance_breakdown())
    except Exception as e:
        return JSONResponse({"by_sport": [], "by_bookmaker": [], "by_market": [], "by_odds": [], "error": str(e)})

@app.get("/api/performance/bankroll")
async def api_performance_bankroll(starting: float = 1000):
    try:
        return JSONResponse(get_bankroll_advanced(starting_bankroll=starting))
    except Exception as e:
        return JSONResponse({"series": [], "starting_bankroll": starting, "current_bankroll": starting,
                             "peak_bankroll": starting, "max_drawdown_pct": 0, "max_drawdown_abs": 0,
                             "current_drawdown_pct": 0, "longest_winning_streak": 0,
                             "longest_losing_streak": 0, "current_streak": {"type": None, "count": 0},
                             "error": str(e)})

@app.get("/api/football/edge")
async def api_football_edge(limit: int = 100):
    from collectors.database import get_connection
    conn = get_connection()
    rows = conn.execute("""
        SELECT date, league_name, home_team, away_team,
               home_goals, away_goals, result,
               pinnacle_home_close, pinnacle_draw_close, pinnacle_away_close,
               avg_home, avg_draw, avg_away
        FROM football_matches
        WHERE pinnacle_home_close IS NOT NULL
          AND pinnacle_draw_close IS NOT NULL
          AND pinnacle_away_close IS NOT NULL
        ORDER BY date DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    results = []
    for r in rows:
        ph, pd_, pa = r["pinnacle_home_close"], r["pinnacle_draw_close"], r["pinnacle_away_close"]
        try:
            margin = (1/ph + 1/pd_ + 1/pa)
            true_home = round((1/ph) / margin * 100, 1)
            true_draw = round((1/pd_) / margin * 100, 1)
            true_away = round((1/pa) / margin * 100, 1)
        except Exception:
            true_home = true_draw = true_away = None
        results.append({**dict(r), "implied_home_pct": true_home,
                        "implied_draw_pct": true_draw, "implied_away_pct": true_away})
    return JSONResponse(results)


# ── Backtesting ──────────────────────────────────────────────────────────────

@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request):
    return templates.TemplateResponse("backtest.html", {"request": request})

@app.get("/multipla", response_class=HTMLResponse)
async def multipla_page(request: Request):
    """Standalone 'Múltipla do Dia' page (pure-model, for fun — not the edge system)."""
    return templates.TemplateResponse("multipla.html", {"request": request})

@app.get("/api/backtest")
async def api_backtest(
    min_edge: float = 3.0,
    max_odds: float = 5.0,
    bankroll: float = 1000.0,
    kelly: float = 0.25,
    league: str = None,
    season: str = None,
    market: str = "all",
    sport: str = "football",
):
    from collectors.backtest import run_backtest, run_tennis_backtest
    loop = asyncio.get_event_loop()
    try:
        if sport == "tennis":
            # league -> tour (ATP/WTA), season -> surface (Hard/Clay/Grass)
            result = await loop.run_in_executor(None, lambda: run_tennis_backtest(
                min_edge=min_edge, max_odds=max_odds, bankroll=bankroll, kelly_frac=kelly,
                tour=league if league else None, surface=season if season else None,
            ))
        else:
            result = await loop.run_in_executor(None, lambda: run_backtest(
                min_edge=min_edge,
                max_odds=max_odds,
                bankroll=bankroll,
                kelly_frac=kelly,
                league=league if league else None,
                season=season if season else None,
                market_filter=market,
            ))
        return JSONResponse(result)
    except Exception as e:
        import traceback
        print(f"BACKTEST_ERROR: {e}\n{traceback.format_exc()}", flush=True)
        return JSONResponse(
            {"error": f"{type(e).__name__}: {e}", "summary": {"total_bets": 0}},
            status_code=500,
        )

@app.get("/api/backtest/compare")
async def api_backtest_compare(bankroll: float = 1000.0, kelly: float = 0.25,
                               market: str = "all", sport: str = "football"):
    from collectors.backtest import compare_thresholds, compare_thresholds_tennis
    loop = asyncio.get_event_loop()
    try:
        if sport == "tennis":
            result = await loop.run_in_executor(None, lambda: compare_thresholds_tennis(
                bankroll=bankroll, kelly_frac=kelly))
        else:
            result = await loop.run_in_executor(None, lambda: compare_thresholds(
                bankroll=bankroll, kelly_frac=kelly, market_filter=market))
        return JSONResponse(result)
    except Exception as e:
        import traceback
        print(f"BACKTEST_ERROR (compare): {e}\n{traceback.format_exc()}", flush=True)
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "scenarios": []}, status_code=500)

@app.get("/api/backtest/clv")
async def api_backtest_clv():
    from collectors.backtest import get_clv_analysis
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, get_clv_analysis)
        return JSONResponse(result)
    except Exception as e:
        import traceback
        print(f"BACKTEST_ERROR (clv): {e}\n{traceback.format_exc()}", flush=True)
        return JSONResponse({"error": str(e), "count": 0, "records": []}, status_code=500)

@app.get("/api/backtest/calibration")
async def api_backtest_calibration(sport: str = "football"):
    from collectors.backtest import get_calibration
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: get_calibration(sport))
        return JSONResponse(result)
    except Exception as e:
        import traceback
        print(f"BACKTEST_ERROR (calibration): {e}\n{traceback.format_exc()}", flush=True)
        return JSONResponse({"error": str(e), "buckets": []}, status_code=500)

@app.get("/api/backtest/meta")
async def api_backtest_meta(sport: str = "football"):
    from collectors.backtest import get_backtest_meta, get_tennis_backtest_meta
    loop = asyncio.get_event_loop()
    try:
        fn = get_tennis_backtest_meta if sport == "tennis" else get_backtest_meta
        result = await loop.run_in_executor(None, fn)
        return JSONResponse(result)
    except Exception as e:
        import traceback
        print(f"BACKTEST_ERROR (meta): {e}\n{traceback.format_exc()}", flush=True)
        return JSONResponse({"error": str(e), "leagues": [], "seasons": []}, status_code=500)
