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

# Seasons are computed (collectors/seasons.py). The literal list that used to
# live here ended at 2025-26, so the 2026-27 season was never requested.
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
        # openfootball changed format: older = {"ft": [h, a]}, newer = [h, a] directly
        if isinstance(score, list):
            home_goals = score[0] if len(score) > 0 else None
            away_goals = score[1] if len(score) > 1 else None
        elif isinstance(score, dict):
            ft = score.get("ft", [None, None])
            home_goals = ft[0] if ft and len(ft) > 0 else None
            away_goals = ft[1] if ft and len(ft) > 1 else None
        else:
            home_goals = away_goals = None

        result = None
        if home_goals is not None and away_goals is not None:
            if home_goals > away_goals: result = "H"
            elif home_goals < away_goals: result = "A"
            else: result = "D"

        date_str = m.get("date", "")
        # team1/team2 can be string or {"name": ..., "code": ...} in newer format
        t1 = m.get("team1", "")
        t2 = m.get("team2", "")
        home = t1.get("name", "") if isinstance(t1, dict) else (t1 or "")
        away = t2.get("name", "") if isinstance(t2, dict) else (t2 or "")
        match_id = f"{date_str}_{home.replace(' ', '_')}_{away.replace(' ', '_')}"

        rows.append({
            "match_id": match_id, "date": date_str, "season": season,
            "league": league_code, "league_name": league_name,
            "home_team": home, "away_team": away,
            "home_goals": home_goals, "away_goals": away_goals, "result": result,
        })
    return rows


def insert_rows(rows):
    if not rows:
        return 0
    from collectors.database import USE_POSTGRES
    conn = get_connection()
    cols = list(rows[0].keys())
    col_names = ", ".join(cols)
    inserted = 0

    # UPSERT, not insert-or-ignore: the season file lists fixtures months before
    # they are played, so a match is first stored with no score. With "ON CONFLICT
    # DO NOTHING" that empty row was never touched again — the result arrived in
    # the source and was thrown away on every later run.
    if USE_POSTGRES:
        import psycopg2.extras
        sql = (f"INSERT INTO football_matches ({col_names}) VALUES %s "
               "ON CONFLICT (match_id) DO UPDATE SET "
               "home_goals = EXCLUDED.home_goals, away_goals = EXCLUDED.away_goals, "
               "result = EXCLUDED.result, date = EXCLUDED.date")
        # One row per match_id: Postgres refuses an upsert batch that would touch
        # the same row twice ("cannot affect row a second time").
        uniq = {d["match_id"]: d for d in rows}
        values = [tuple(d.get(c) for c in cols) for d in uniq.values()]
        try:
            raw_cur = conn._conn.cursor()
            psycopg2.extras.execute_values(raw_cur, sql, values, page_size=500)
            inserted = len(values)
            conn._conn.commit()
        except Exception as e:
            print(f"Football batch insert error: {e}", flush=True)
            try:
                conn._conn.rollback()
            except Exception:
                pass
    else:
        placeholders = "(" + ",".join(["?"] * len(cols)) + ")"
        for d in rows:
            try:
                cur = conn.execute(
                    f"INSERT INTO football_matches ({col_names}) VALUES {placeholders} "
                    "ON CONFLICT(match_id) DO UPDATE SET "
                    "home_goals = excluded.home_goals, away_goals = excluded.away_goals, "
                    "result = excluded.result, date = excluded.date",
                    list(d.values())
                )
                inserted += max(cur.rowcount, 0)
            except Exception as e:
                print(f"Football upsert error: {e}", flush=True)
        conn.commit()

    conn.close()
    return inserted


def collect_football(status_callback=None, seasons=None):
    from collectors.seasons import football_seasons
    total = 0
    errors = 0
    def cb(msg):
        print(msg, flush=True)
        if status_callback: status_callback(msg)

    cb("Collecting match results from openfootball (GitHub)...")
    for season in (seasons or football_seasons(6)):
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
