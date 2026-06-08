import os
import asyncio
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from collectors.database import (
    init_db, get_stats, get_football_summary, get_tennis_summary,
    get_recent_football, get_recent_tennis, get_collection_log, get_value_bets,
    add_bet, update_bet_result, delete_bet, get_bets, get_bet_stats,
    get_bankroll_evolution, capture_pinnacle_close_for_started_events,
    get_performance_breakdown, get_bankroll_advanced,
    get_national_summary, get_national_recent,
    get_xg_summary, get_team_xg,
    get_elo_summary, get_elo_rating, elo_based_probability,
    save_manual_odd, delete_manual_odd, get_manual_odds_map,
    invalidate_value_bets_cache,
    USE_POSTGRES, DATABASE_URL
)
from collectors.football import collect_football
from collectors.tennis import collect_tennis
from collectors.odds import collect_odds
from collectors.odds_football import collect_odds_apifootball
from collectors.national import collect_national
from collectors.national_xg import compute_national_xg
from collectors.understat import collect_understat
from collectors.elo import collect_elo
from collectors.odds_collector import collect_odds_multiple_bookmakers, collect_tennis_odds

app = FastAPI(title="Betting Intelligence Platform")

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
        scheduler.add_job(lambda: asyncio.create_task(run_full_collection()), "cron", hour=3, minute=0)
        scheduler.add_job(lambda: asyncio.create_task(run_odds_only()), "cron", hour="*/6", minute=15)
        scheduler.start()
        print("Scheduler started.", flush=True)
    except Exception as e:
        print(f"SCHEDULER ERROR: {e}", flush=True)

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
    collection_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    def cb(msg):
        print(msg, flush=True)
        collection_status["messages"].append(str(msg))
        if len(collection_status["messages"]) > 300:
            collection_status["messages"] = collection_status["messages"][-300:]

    loop = asyncio.get_event_loop()

    try:
        cb("Step 1/4: Collecting football data (GitHub/openfootball)...")
        try:
            n_fb = await loop.run_in_executor(
                None, lambda: collect_football(status_callback=cb)
            )
            cb(f"Football done: {n_fb} rows collected.")
        except Exception as e:
            import traceback
            cb(f"FOOTBALL ERROR: {repr(e)}")
            cb(traceback.format_exc()[-800:])

        cb("Step 2/4: Collecting tennis data (ATP/WTA from GitHub)...")
        try:
            n_tn = await loop.run_in_executor(
                None, lambda: collect_tennis(start_year=2015, status_callback=cb)
            )
            cb(f"Tennis done: {n_tn} rows collected.")
        except Exception as e:
            import traceback
            cb(f"TENNIS ERROR: {repr(e)}")
            cb(traceback.format_exc()[-800:])

        cb("Step 3/4: Collecting national teams (international results)...")
        try:
            n_nat = await loop.run_in_executor(
                None, lambda: collect_national(since_year=2010, status_callback=cb)
            )
            cb(f"National teams done: {n_nat} rows collected.")
        except Exception as e:
            import traceback
            cb(f"NATIONAL ERROR: {repr(e)}")
            cb(traceback.format_exc()[-800:])

        cb("Step 3b: Computing national team xG (Dixon-Coles proxy)...")
        try:
            n_xg = await loop.run_in_executor(
                None, lambda: compute_national_xg(status_callback=cb)
            )
            cb(f"National xG done: {n_xg} teams indexed.")
        except Exception as e:
            import traceback
            cb(f"NATIONAL XG ERROR: {repr(e)}")
            cb(traceback.format_exc()[-800:])

        cb("Step 4/5: Collecting live odds (multi-source)...")
        try:
            n_od = await loop.run_in_executor(
                None, lambda: collect_odds_apifootball(status_callback=cb)
            )
            cb(f"Football odds done: {n_od} events.")
        except Exception as e:
            import traceback
            cb(f"FOOTBALL ODDS ERROR: {repr(e)}")
            cb(traceback.format_exc()[-400:])
        try:
            n_od2 = await loop.run_in_executor(
                None, lambda: collect_odds(status_callback=cb)
            )
            cb(f"Other odds done: {n_od2} events.")
        except Exception as e:
            import traceback
            cb(f"ODDS ERROR: {repr(e)}")
            cb(traceback.format_exc()[-800:])

        cb("Step 5/5: Collecting xG data (Understat)...")
        try:
            n_xg = await loop.run_in_executor(
                None, lambda: collect_understat(status_callback=cb)
            )
            cb(f"Understat done: {n_xg} matches.")
        except Exception as e:
            import traceback
            cb(f"UNDERSTAT ERROR: {repr(e)}")
            cb(traceback.format_exc()[-800:])

        # Auto-capture CLV for any pending bets after collection
        try:
            n = await loop.run_in_executor(None, capture_pinnacle_close_for_started_events)
            if n > 0:
                cb(f"✓ Captured Pinnacle closing line for {n} past bet(s).")
        except Exception as e:
            cb(f"CLV capture error: {repr(e)}")

        # Compute Elo ratings from all collected data
        cb("Computing Elo ratings...")
        try:
            await loop.run_in_executor(None, lambda: collect_elo(status_callback=cb))
        except Exception as e:
            import traceback
            cb(f"ELO ERROR: {repr(e)}")
            cb(traceback.format_exc()[-800:])

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
    return templates.TemplateResponse("index.html", {"request": request})

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
async def api_start_collection():
    if collection_status["running"]:
        return JSONResponse({"ok": False, "msg": "Already running."})
    asyncio.create_task(run_full_collection())
    return JSONResponse({"ok": True, "msg": "Collection started."})

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
    collection_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
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
        # Auto-capture CLV for past bets
        try:
            n = await loop.run_in_executor(None, capture_pinnacle_close_for_started_events)
            if n > 0:
                cb(f"✓ Captured Pinnacle closing line for {n} past bet(s).")
        except Exception as e:
            cb(f"CLV capture error: {repr(e)}")
        cb("✓ Odds refresh finished.")
        invalidate_value_bets_cache()
    except Exception as e:
        import traceback
        cb(f"ODDS ERROR: {repr(e)}")
        cb(traceback.format_exc()[-800:])
    finally:
        collection_status["running"] = False

@app.post("/api/odds/refresh")
async def api_refresh_odds():
    if collection_status["running"]:
        return JSONResponse({"ok": False, "msg": "Already running."})
    asyncio.create_task(run_odds_only())
    return JSONResponse({"ok": True, "msg": "Odds refresh started."})

@app.get("/api/value-bets")
async def api_value_bets():
    return JSONResponse(get_value_bets())

@app.get("/api/value-bets/{event_id}")
async def api_event_detail(event_id: str):
    all_bets = get_value_bets()
    match = next((b for b in all_bets if b.get("event_id") == event_id), None)
    if not match:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(match)


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
async def api_capture_clv():
    """Manually trigger CLV capture for past pending bets."""
    try:
        n = capture_pinnacle_close_for_started_events()
        return JSONResponse({"ok": True, "captured": n})
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
