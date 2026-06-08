"""
Football fixtures + odds collector.
Primary source: openfootball/worldcup.json (GitHub, no auth, no limits, reliable)
This is the SAME trusted source used for historical data.

Provides World Cup 2026 fixtures (all 104 matches) plus European league fixtures.
Odds are added from The Odds API when available; otherwise fixtures show with
model-based probabilities (Elo) so the site is always useful.
"""
import os
import requests
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from collectors.database import get_connection, log_collection

# openfootball JSON sources (GitHub raw — always accessible, no key)
OPENFOOTBALL_SOURCES = {
    "FIFA World Cup 2026": "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json",
}

# Also try these European leagues for 2025-26 season fixtures
EURO_LEAGUES = {
    "Premier League": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/en.1.json",
    "La Liga": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/es.1.json",
    "Bundesliga": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/de.1.json",
    "Serie A": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/it.1.json",
    "Ligue 1": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/fr.1.json",
}


def store_fixture(event_id, sport_name, home, away, commence,
                  pin_home=None, pin_draw=None, pin_away=None):
    """Store a fixture (with or without odds)."""
    if not home or not away:
        return False
    conn = get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO odds_events (
                event_id, sport_key, sport_name, home_team, away_team, commence_time,
                pin_home, pin_draw, pin_away,
                best_home, best_draw, best_away,
                x1_home, x1_draw, x1_away,
                updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (event_id, "soccer", sport_name, home, away, commence,
              pin_home, pin_draw, pin_away,
              pin_home, pin_draw, pin_away,
              pin_home, pin_draw, pin_away,
              datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Store error: {e}", flush=True)
        conn.close()
        return False


def parse_iso_datetime(date_str, time_str=""):
    """Convert openfootball date + time to ISO format."""
    if not date_str:
        return ""
    # date is "2026-06-11", time is "13:00 UTC-6" or just "13:00"
    time_part = "12:00"
    if time_str:
        # Extract HH:MM from the start
        import re
        m = re.match(r"(\d{1,2}:\d{2})", time_str.strip())
        if m:
            time_part = m.group(1)
    return f"{date_str}T{time_part}:00Z"


def collect_openfootball_fixtures(cb):
    """Collect fixtures from openfootball JSON (reliable, no auth)."""
    total = 0
    today = datetime.now().date()

    # World Cup 2026
    for sport_name, url in OPENFOOTBALL_SOURCES.items():
        cb(f"  Fetching {sport_name} (openfootball)...")
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                cb(f"  -> HTTP {r.status_code}")
                continue
            data = r.json()
            matches = data.get("matches", [])
            cb(f"  -> {len(matches)} matches in source")
            stored = 0
            for m in matches:
                home = m.get("team1", "")
                away = m.get("team2", "")
                date_str = m.get("date", "")
                time_str = m.get("time", "")
                if not home or not away:
                    continue
                # Include upcoming + recent (within last 2 days)
                try:
                    match_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    if match_date < today - timedelta(days=2):
                        continue
                except Exception:
                    pass
                commence = parse_iso_datetime(date_str, time_str)
                round_info = m.get("round", "") or m.get("group", "")
                eid = f"of_wc_{date_str}_{home}_{away}".replace(" ", "_")
                full_name = f"{sport_name}"
                if store_fixture(eid, full_name, home, away, commence):
                    stored += 1
            cb(f"  -> {sport_name}: {stored} upcoming fixtures stored")
            total += stored
        except Exception as e:
            cb(f"  -> {sport_name} error: {e}")

    # European leagues (2025-26)
    for league_name, url in EURO_LEAGUES.items():
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                continue
            data = r.json()
            matches = data.get("matches", [])
            stored = 0
            for m in matches:
                home = m.get("team1", "")
                away = m.get("team2", "")
                if isinstance(home, dict): home = home.get("name", "")
                if isinstance(away, dict): away = away.get("name", "")
                date_str = m.get("date", "")
                time_str = m.get("time", "")
                if not home or not away or not date_str:
                    continue
                try:
                    match_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    if match_date < today:
                        continue
                except Exception:
                    continue
                commence = parse_iso_datetime(date_str, time_str)
                eid = f"of_{league_name[:3]}_{date_str}_{home}_{away}".replace(" ", "_")
                if store_fixture(eid, league_name, home, away, commence):
                    stored += 1
            if stored:
                cb(f"  -> {league_name}: {stored} upcoming fixtures stored")
            total += stored
        except Exception:
            pass

    return total


def add_odds_from_theoddsapi(cb):
    """Try to enrich stored fixtures with real odds from The Odds API."""
    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        return 0
    # The Odds API soccer endpoints that may have data
    sports = ["soccer_fifa_world_cup", "soccer_uefa_champs_league",
              "soccer_epl", "soccer_spain_la_liga"]
    enriched = 0
    conn = get_connection()
    for sport in sports:
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
                params={"apiKey": api_key, "regions": "eu", "markets": "h2h",
                        "bookmakers": "pinnacle,onexbet", "oddsFormat": "decimal"},
                timeout=15
            )
            if r.status_code != 200:
                continue
            events = r.json()
            for ev in events:
                home = ev.get("home_team", "")
                away = ev.get("away_team", "")
                ph = pd = pa = None
                for bm in ev.get("bookmakers", []):
                    for mkt in bm.get("markets", []):
                        if mkt.get("key") != "h2h":
                            continue
                        odds = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                        if bm.get("key") == "pinnacle":
                            ph = odds.get(home)
                            pa = odds.get(away)
                            pd = odds.get("Draw")
                # Update matching fixtures by team names
                if ph and pa:
                    conn.execute("""
                        UPDATE odds_events
                        SET pin_home=?, pin_draw=?, pin_away=?,
                            best_home=?, best_draw=?, best_away=?,
                            x1_home=?, x1_draw=?, x1_away=?
                        WHERE home_team LIKE ? AND away_team LIKE ?
                    """, (ph, pd, pa, ph, pd, pa, ph, pd, pa,
                          f"%{home[:12]}%", f"%{away[:12]}%"))
                    enriched += 1
        except Exception as e:
            cb(f"  -> Odds enrich error ({sport}): {e}")
    conn.commit()
    conn.close()
    if enriched:
        cb(f"  -> Enriched {enriched} fixtures with live odds")
    return enriched


def collect_odds_apifootball(status_callback=None):
    """Main entry point — collects fixtures from reliable sources."""
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    cb("Collecting football fixtures (openfootball — reliable, no key)...")
    total = collect_openfootball_fixtures(cb)

    cb("Trying to add live odds (The Odds API)...")
    try:
        add_odds_from_theoddsapi(cb)
    except Exception as e:
        cb(f"  -> Odds enrichment skipped: {e}")

    log_collection("football-fixtures", "success", total, f"{total} fixtures")
    cb(f"✓ Football done: {total} fixtures stored.")
    return total


if __name__ == "__main__":
    from collectors.database import init_db
    init_db()
    collect_odds_apifootball()
