"""
Pinnacle Sports Direct API collector.

Uses api.pinnacle.com — available to any Pinnacle customer (free).
Requires: PINNACLE_USERNAME and PINNACLE_PASSWORD environment variables.

Setup:
  1. Create a Pinnacle account at pinnacle.com (available in Luxembourg)
  2. Set env vars: PINNACLE_USERNAME=your_email PINNACLE_PASSWORD=your_password
  3. Call collect_pinnacle_odds() from run_odds_only()

API docs: https://pinnacle.com/en/api-xml-feed/overview
Coverage: ALL Pinnacle markets — no gaps, no region restrictions.
"""

import os
import requests
from datetime import datetime, timezone

BASE = "https://api.pinnacle.com"
SOCCER_SPORT_ID = 29
TENNIS_SPORT_ID = 33

# World Cup 2026 league ID on Pinnacle (fetched dynamically via get_leagues)
WC_LEAGUE_KEYWORDS = ["world cup", "fifa world cup", "mundial"]


def _auth():
    user = os.environ.get("PINNACLE_USERNAME", "")
    pwd = os.environ.get("PINNACLE_PASSWORD", "")
    if not user or not pwd:
        return None
    return (user, pwd)


def get_leagues(sport_id, auth):
    """Fetch all leagues for a sport. Returns list of {id, name}."""
    try:
        r = requests.get(
            f"{BASE}/v1/leagues",
            params={"sportId": sport_id},
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("leagues", [])
    except Exception:
        pass
    return []


def get_fixtures(sport_id, league_ids, auth):
    """Fetch upcoming fixtures for given league IDs."""
    try:
        r = requests.get(
            f"{BASE}/v1/fixtures",
            params={"sportId": sport_id, "leagueIds": ",".join(str(i) for i in league_ids)},
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("league", [])
    except Exception:
        pass
    return []


def get_odds(sport_id, league_ids, auth):
    """Fetch live odds for given league IDs."""
    try:
        r = requests.get(
            f"{BASE}/v2/odds",
            params={
                "sportId": sport_id,
                "leagueIds": ",".join(str(i) for i in league_ids),
                "oddsFormat": "Decimal",
            },
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("leagues", [])
    except Exception:
        pass
    return []


def collect_pinnacle_odds(status_callback=None):
    """
    Fetch Pinnacle odds for World Cup + major leagues and update odds_events.
    Returns number of events updated.
    """
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    auth = _auth()
    if not auth:
        cb("PINNACLE: PINNACLE_USERNAME/PINNACLE_PASSWORD not set — skipping.")
        return 0

    cb("Pinnacle API: fetching World Cup + soccer leagues...")

    try:
        from collectors.database import get_connection
        import unicodedata

        def norm(s):
            s = unicodedata.normalize("NFD", s or "")
            return " ".join(s.encode("ascii", "ignore").decode("ascii").lower().split())

        # --- Find World Cup league ID ---
        leagues = get_leagues(SOCCER_SPORT_ID, auth)
        wc_ids = []
        major_ids = []
        for lg in leagues:
            name_lower = (lg.get("name") or "").lower()
            if any(kw in name_lower for kw in WC_LEAGUE_KEYWORDS):
                wc_ids.append(lg["id"])
            elif any(kw in name_lower for kw in [
                "premier league", "la liga", "bundesliga", "serie a",
                "ligue 1", "champions league", "club world cup"
            ]):
                major_ids.append(lg["id"])

        league_ids = wc_ids + major_ids[:10]  # Cap to avoid huge requests
        if not league_ids:
            cb("PINNACLE: no matching leagues found.")
            return 0

        cb(f"PINNACLE: {len(wc_ids)} WC leagues + {len(major_ids[:10])} major leagues")

        # --- Get fixtures (for event meta: home/away team names) ---
        fixture_map = {}  # event_id → {home, away, commence}
        for league_data in get_fixtures(SOCCER_SPORT_ID, league_ids, auth):
            for ev in league_data.get("events", []):
                fixture_map[ev["id"]] = {
                    "home": ev.get("home", ""),
                    "away": ev.get("away", ""),
                    "commence": ev.get("starts", ""),
                }

        # --- Get odds ---
        odds_map = {}  # event_id → {home, draw, away, over25, under25}
        for league_data in get_odds(SOCCER_SPORT_ID, league_ids, auth):
            for ev in league_data.get("events", []):
                eid = ev.get("id")
                periods = ev.get("periods", [])
                for period in periods:
                    if period.get("number") != 0:  # 0 = full match
                        continue
                    ml = period.get("moneyline", {})
                    totals = period.get("totals", [])
                    home_odd = ml.get("home")
                    draw_odd = ml.get("draw")
                    away_odd = ml.get("away")
                    over25 = under25 = None
                    for t in totals:
                        if abs(float(t.get("points", 0)) - 2.5) < 0.01:
                            over25 = t.get("over")
                            under25 = t.get("under")
                    if home_odd and away_odd:
                        odds_map[eid] = {
                            "home": home_odd,
                            "draw": draw_odd,
                            "away": away_odd,
                            "over25": over25,
                            "under25": under25,
                        }

        if not odds_map:
            cb("PINNACLE: no odds returned.")
            return 0

        cb(f"PINNACLE: {len(odds_map)} events with odds")

        # --- Match against DB fixtures by normalized team name ---
        conn = get_connection()
        existing = conn.execute(
            "SELECT event_id, home_team, away_team FROM odds_events "
            "WHERE commence_time > datetime('now', '-1 day')"
        ).fetchall()
        db_lookup = {(norm(r["home_team"]), norm(r["away_team"])): r["event_id"] for r in existing}

        updated = 0
        for pin_eid, o in odds_map.items():
            fixture = fixture_map.get(pin_eid)
            if not fixture:
                continue
            key = (norm(fixture["home"]), norm(fixture["away"]))
            db_eid = db_lookup.get(key)
            if db_eid:
                conn.execute("""
                    UPDATE odds_events
                    SET pin_home=?, pin_draw=?, pin_away=?,
                        pin_over25=COALESCE(?, pin_over25),
                        pin_under25=COALESCE(?, pin_under25)
                    WHERE event_id=?
                """, (o["home"], o["draw"], o["away"],
                      o["over25"], o["under25"], db_eid))
                updated += 1

        conn.commit()
        conn.close()
        cb(f"PINNACLE: updated {updated} events with direct Pinnacle odds.")
        return updated

    except Exception as e:
        import traceback
        cb(f"PINNACLE ERROR: {e}")
        cb(traceback.format_exc()[-600:])
        return 0
