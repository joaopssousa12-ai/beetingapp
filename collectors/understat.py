"""
Understat xG collector.
Scrapes match-level xG data from understat.com for the top 5 European leagues.
Uses direct requests + regex (no Playwright needed — Understat embeds JSON in <script> tags).
"""
import re
import json
import requests
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from collectors.database import get_connection, log_collection

UNDERSTAT_LEAGUES = {
    "EPL": "Premier League",
    "La_liga": "La Liga",
    "Bundesliga": "Bundesliga",
    "Serie_A": "Serie A",
    "Ligue_1": "Ligue 1",
}

SEASONS = ["2024", "2025"]  # season starting year (e.g. 2025 = 2025-26)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}


def fetch_league_data(league_key, season):
    """Returns parsed datesData (match-level info incl. xG) for a season."""
    url = f"https://understat.com/league/{league_key}/{season}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            print(f"Understat {league_key}/{season}: HTTP {r.status_code}", flush=True)
            return None

        # Understat embeds data as: var datesData = JSON.parse('...');
        match = re.search(r"var datesData\s*=\s*JSON\.parse\('([^']+)'\)", r.text)
        if not match:
            print(f"Understat {league_key}/{season}: datesData pattern not found (page format may have changed)", flush=True)
            return None

        raw = match.group(1)
        # Convert JS \xNN hex escapes (e.g. \x22 for ") to \uNNNN that json.loads() understands.
        # The old .encode("utf-8").decode("unicode_escape") approach corrupts non-ASCII names.
        raw = re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: '\\u00' + m.group(1), raw)
        data = json.loads(raw)
        return data
    except Exception as e:
        print(f"Understat fetch error {league_key} {season}: {e}", flush=True)
        return None


def parse_and_store(data, league_key, league_name, season):
    """Each match in datesData has: id, isResult, h, a, goals, xG, datetime, forecast."""
    if not data:
        return 0

    conn = get_connection()
    inserted = 0

    for m in data:
        try:
            match_id = m.get("id")
            if not match_id:
                continue

            date = (m.get("datetime") or "")[:10]
            h = m.get("h") or {}
            a = m.get("a") or {}
            home = h.get("title", "")
            away = a.get("title", "")
            if not home or not away:
                continue

            is_result = m.get("isResult", False)
            goals = m.get("goals") or {}
            xg = m.get("xG") or {}

            home_goals = int(goals["h"]) if is_result and goals.get("h") else None
            away_goals = int(goals["a"]) if is_result and goals.get("a") else None
            home_xg = float(xg["h"]) if xg.get("h") else None
            away_xg = float(xg["a"]) if xg.get("a") else None

            # Forecast probabilities (Understat's own model)
            forecast = m.get("forecast") or {}
            forecast_home = float(forecast.get("w", 0)) if forecast.get("w") else None
            forecast_draw = float(forecast.get("d", 0)) if forecast.get("d") else None
            forecast_away = float(forecast.get("l", 0)) if forecast.get("l") else None

            conn.execute("""
                INSERT OR REPLACE INTO understat_matches (
                    match_id, date, season, league_key, league_name,
                    home_team, away_team, home_goals, away_goals,
                    home_xg, away_xg,
                    forecast_home, forecast_draw, forecast_away,
                    is_result
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                str(match_id), date, season, league_key, league_name,
                home, away, home_goals, away_goals,
                home_xg, away_xg,
                forecast_home, forecast_draw, forecast_away,
                1 if is_result else 0,
            ))
            inserted += 1
        except Exception as e:
            print(f"Understat insert error: {e}", flush=True)

    conn.commit()
    conn.close()
    return inserted


def compute_team_rolling_xg(window=10):
    """
    For each team, compute rolling average xG for/against over last N matches.
    Stores in team_xg table for fast lookup during analysis.
    """
    conn = get_connection()
    # Clear & rebuild
    conn.execute("DELETE FROM team_xg")

    # Get all completed matches
    rows = conn.execute("""
        SELECT date, league_name, home_team, away_team,
               home_xg, away_xg, home_goals, away_goals
        FROM understat_matches
        WHERE is_result = 1 AND home_xg IS NOT NULL AND away_xg IS NOT NULL
        ORDER BY date ASC
    """).fetchall()

    # Build per-team history
    team_history = {}  # team_name -> list of (date, xg_for, xg_against, goals_for, goals_against, league)
    for r in rows:
        h, a = r["home_team"], r["away_team"]
        team_history.setdefault(h, []).append({
            "date": r["date"], "league": r["league_name"],
            "xg_for": r["home_xg"], "xg_against": r["away_xg"],
            "goals_for": r["home_goals"] or 0, "goals_against": r["away_goals"] or 0,
            "is_home": True,
        })
        team_history.setdefault(a, []).append({
            "date": r["date"], "league": r["league_name"],
            "xg_for": r["away_xg"], "xg_against": r["home_xg"],
            "goals_for": r["away_goals"] or 0, "goals_against": r["home_goals"] or 0,
            "is_home": False,
        })

    # Compute rolling avg for each team (last N matches)
    for team, history in team_history.items():
        if len(history) < 3:
            continue
        recent = history[-window:]
        n = len(recent)
        avg_xg_for = sum(h["xg_for"] for h in recent) / n
        avg_xg_against = sum(h["xg_against"] for h in recent) / n
        avg_goals_for = sum(h["goals_for"] for h in recent) / n
        avg_goals_against = sum(h["goals_against"] for h in recent) / n

        # xG overperformance (positive = scoring more than xG suggests; negative = unlucky)
        xg_diff_for = avg_goals_for - avg_xg_for
        xg_diff_against = avg_xg_against - avg_goals_against  # positive = conceding less than xG

        last_match_date = recent[-1]["date"]
        league = recent[-1]["league"]

        conn.execute("""
            INSERT OR REPLACE INTO team_xg (
                team, league, matches_sample,
                avg_xg_for, avg_xg_against,
                avg_goals_for, avg_goals_against,
                xg_overperf_attack, xg_overperf_defense,
                last_match_date
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            team, league, n,
            round(avg_xg_for, 3), round(avg_xg_against, 3),
            round(avg_goals_for, 3), round(avg_goals_against, 3),
            round(xg_diff_for, 3), round(xg_diff_against, 3),
            last_match_date,
        ))

    conn.commit()
    conn.close()
    return len(team_history)


def collect_understat(status_callback=None):
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    cb("Fetching Understat xG data...")
    total = 0
    no_data_count = 0
    for season in SEASONS:
        for lkey, lname in UNDERSTAT_LEAGUES.items():
            cb(f"  Fetching {lname} {season}/{int(season)+1}...")
            data = fetch_league_data(lkey, season)
            if not data:
                no_data_count += 1
                cb(f"    -> no data (check Render logs for details)")
                continue
            n = parse_and_store(data, lkey, lname, season)
            total += n
            cb(f"    -> {n} matches stored")
    if no_data_count > 0 and total == 0:
        cb(f"  WARNING: All {no_data_count} league fetches returned no data. Understat may be blocking or changed format.")

    cb("Computing rolling 10-game xG averages per team...")
    teams = compute_team_rolling_xg(window=10)
    cb(f"  -> {teams} teams indexed")

    log_collection("understat (xG)", "success", total, f"{teams} teams indexed")
    cb(f"✓ Understat done: {total} matches, {teams} teams.")
    return total


if __name__ == "__main__":
    from collectors.database import init_db
    init_db()
    collect_understat()
