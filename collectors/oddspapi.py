"""
OddsPapi — free aggregator with Pinnacle odds (250 requests/month free tier).

Setup (Render environment variables):
  ODDSPAPI_KEY = your API key from oddspapi.io

How to get key:
  1. Register at https://oddspapi.io (no credit card)
  2. Copy your API key from the dashboard
  3. Add ODDSPAPI_KEY to Render env vars

What this does:
  Fetches Pinnacle h2h odds for soccer events and updates pin_home/draw/away
  in odds_events. Runs before Betfair so Betfair can override in liquid markets.

250 requests/month = ~8/day at our 6h refresh cycle. Well within limits.
"""

import os
import requests
import unicodedata
from datetime import datetime

API_KEY = os.environ.get("ODDSPAPI_KEY", "")

# OddsPapi — aggregator with 350+ bookmakers including Pinnacle (free 250 req/month)
# Compatible endpoint format with The Odds API
BASE = "https://api.oddspapi.io/v4"

# Known soccer sports to fetch (same list as odds.py — reuses existing events)
SOCCER_SPORTS = [
    "soccer_fifa_world_cup",
    "soccer_fifa_club_world_cup",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_portugal_primeira_liga",
]


def _norm(s):
    s = unicodedata.normalize("NFD", s or "")
    return " ".join(s.encode("ascii", "ignore").decode("ascii").lower().split())


def _fetch_pinnacle_odds(sport_key):
    """Fetch Pinnacle h2h odds for a sport via OddsPapi."""
    try:
        r = requests.get(
            f"{BASE}/sports/{sport_key}/odds",
            params={
                "apiKey": API_KEY,
                "regions": "eu,us",
                "markets": "h2h",
                "bookmakers": "pinnacle",
                "oddsFormat": "decimal",
            },
            timeout=20,
        )
        remaining = r.headers.get("x-requests-remaining", "?")
        if r.status_code == 200:
            return r.json(), remaining
        if r.status_code == 401:
            return None, "invalid_key"
        if r.status_code == 422:
            return [], remaining  # sport not available right now
        return None, remaining
    except Exception as e:
        print(f"OddsPapi fetch error ({sport_key}): {e}", flush=True)
        return None, "?"


def collect_oddspapi(status_callback=None):
    """
    Fetch Pinnacle odds from OddsPapi and update pin_home/draw/away in DB.
    Returns number of events updated.
    """
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    if not API_KEY:
        cb("OddsPapi: ODDSPAPI_KEY not set — skipping.")
        return 0

    from collectors.database import get_connection

    # Load existing events from DB for name matching
    conn = get_connection()
    existing = conn.execute(
        "SELECT event_id, home_team, away_team FROM odds_events "
        "WHERE commence_time > datetime('now', '-1 day')"
    ).fetchall()
    conn.close()

    db_lookup = {
        (_norm(r["home_team"]), _norm(r["away_team"])): r["event_id"]
        for r in existing
    }

    if not db_lookup:
        cb("OddsPapi: no upcoming events in DB — skipping.")
        return 0

    cb(f"OddsPapi: fetching Pinnacle odds for {len(SOCCER_SPORTS)} sports...")

    # Collect all Pinnacle odds across sports
    pin_odds = {}  # (norm_home, norm_away) → {home, draw, away}
    remaining = "?"

    for sport_key in SOCCER_SPORTS:
        events, remaining = _fetch_pinnacle_odds(sport_key)
        if remaining == "invalid_key":
            cb("OddsPapi: invalid API key — check ODDSPAPI_KEY in Render.")
            return 0
        if not events:
            continue

        for ev in events:
            home = ev.get("home_team", "")
            away = ev.get("away_team", "")

            # Extract Pinnacle h2h
            pin_h2h = {}
            for bm in ev.get("bookmakers", []):
                if bm.get("key") == "pinnacle":
                    for mkt in bm.get("markets", []):
                        if mkt.get("key") == "h2h":
                            for o in mkt.get("outcomes", []):
                                pin_h2h[o["name"]] = o["price"]

            pin_home = pin_h2h.get(home)
            pin_away = pin_h2h.get(away)
            pin_draw = pin_h2h.get("Draw")

            if pin_home and pin_away:
                key = (_norm(home), _norm(away))
                pin_odds[key] = (pin_home, pin_draw, pin_away)

    if not pin_odds:
        cb(f"OddsPapi: no Pinnacle odds returned. Credits left: {remaining}")
        return 0

    cb(f"OddsPapi: {len(pin_odds)} events with Pinnacle odds. Credits left: {remaining}")

    # Update DB
    conn = get_connection()
    updated = 0
    for (nh, na), (ph, pd, pa) in pin_odds.items():
        eid = db_lookup.get((nh, na))
        if eid:
            conn.execute(
                "UPDATE odds_events SET pin_home=?, pin_draw=?, pin_away=? WHERE event_id=?",
                (ph, pd, pa, eid),
            )
            updated += 1

    conn.commit()
    conn.close()
    cb(f"OddsPapi: updated {updated} events with Pinnacle reference odds.")
    return updated
