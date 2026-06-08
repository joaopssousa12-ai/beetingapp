"""Football collector - openfootball JSON from GitHub (no pandas needed)."""
import requests
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from collectors.database import get_connection, log_collection

OPENFOOTBALL_LEAGUES = {
    "en.1":  "Premier League",
    "en.2":  "Championship",
    "es.1":  "La Liga",
    "de.1":  "Bundesliga",
    "it.1":  "Serie A",
    "fr.1":  "Ligue 1",
}

OPENFOOTBALL_SEASONS = ["2025-26", "2024-25", "2023-24", "2022-23", "2021-22", "2020-21"]
OPENFOOTBALL_BASE = "https://raw.githubusercontent.com/openfootball/football.json/master/{season}/{league}.json"


def fetch_openfootball(season, league_code):
    url = OPENFOOTBALL_BASE.format(season=season, league=league_code)
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def parse_openfootball(data, league_code, league_name, season):
    matches = data.get("matches", [])
    rows = []
    for m in matches:
        score = m.get("score", {})
        ft = score.get("ft", [None, None])
        home_goals = ft[0] if ft and len(ft) > 0 else None
        away_goals = ft[1] if ft and len(ft) > 1 else None

        result = None
        if home_goals is not None and away_goals is not None:
            if home_goals > away_goals: result = "H"
            elif home_goals < away_goals: result = "A"
            else: result = "D"

        date_str = m.get("date", "")
        home = m.get("team1", "")
        away = m.get("team2", "")
        match_id = f"{date_str}_{home.replace(' ', '_')}_{away.replace(' ', '_')}"

        rows.append({
            "match_id": match_id, "date": date_str, "season": season,
            "league": league_code, "league_name": league_name,
            "home_team": home, "away_team": away,
            "home_goals": home_goals, "away_goals": away_goals, "result": result,
        })
    return rows


def insert_rows(rows):
    conn = get_connection()
    inserted = 0
    for d in rows:
        try:
            cols = list(d.keys())
            placeholders = ",".join(["?" for _ in cols])
            col_names = ",".join(cols)
            conn.execute(
                f"INSERT OR IGNORE INTO football_matches ({col_names}) VALUES ({placeholders})",
                list(d.values())
            )
            inserted += conn.execute("SELECT changes()").fetchone()[0]
        except Exception:
            pass
    conn.commit()
    conn.close()
    return inserted


def collect_football(status_callback=None):
    total = 0
    errors = 0
    def cb(msg):
        print(msg, flush=True)
        if status_callback: status_callback(msg)

    cb("Collecting match results from openfootball (GitHub)...")
    for season in OPENFOOTBALL_SEASONS:
        for code, name in OPENFOOTBALL_LEAGUES.items():
            try:
                data = fetch_openfootball(season, code)
                if not data:
                    cb(f"  -> {name} {season}: not found")
                    continue
                rows = parse_openfootball(data, code, name, season)
                n = insert_rows(rows)
                total += n
                cb(f"  -> {name} {season}: {n} rows")
            except Exception as e:
                errors += 1
                cb(f"  -> {name} {season} ERROR: {e}")

    status = "success" if errors == 0 else "partial"
    log_collection("football (openfootball)", status, total, f"{errors} errors" if errors else "ok")
    cb(f"Football done: {total} total rows.")
    return total


if __name__ == "__main__":
    from collectors.database import init_db
    init_db()
    n = collect_football()
    print(f"Done. Total: {n}")
