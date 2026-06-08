"""
National teams collector - martj42/international_results
~50,000 international matches since 1872 to today (auto-updated).
Includes World Cup, qualifiers, friendlies, Nations League, Euros, Copa America.
"""
import requests
import csv
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from collectors.database import get_connection, log_collection

RESULTS_URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
SHOOTOUTS_URL = "https://raw.githubusercontent.com/martj42/international_results/master/shootouts.csv"
GOALSCORERS_URL = "https://raw.githubusercontent.com/martj42/international_results/master/goalscorers.csv"

# Tournaments that matter most for World Cup analysis
KEY_TOURNAMENTS = {
    "FIFA World Cup", "FIFA World Cup qualification",
    "UEFA Euro", "UEFA Euro qualification", "UEFA Nations League",
    "Copa América", "African Cup of Nations", "AFC Asian Cup",
    "CONCACAF Nations League", "Gold Cup",
    "Confederations Cup", "Friendly",
}


def fetch_csv(url):
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200 and len(r.content) > 500:
            reader = csv.DictReader(io.StringIO(r.text))
            return list(reader)
    except Exception as e:
        print(f"Fetch error {url}: {e}", flush=True)
    return None


def insert_matches(rows, since_year=2010):
    if not rows:
        return 0
    conn = get_connection()
    inserted = 0

    for r in rows:
        try:
            date = r.get("date", "")
            if not date or len(date) < 4:
                continue
            year = int(date[:4])
            if year < since_year:
                continue

            home = (r.get("home_team") or "").strip()
            away = (r.get("away_team") or "").strip()
            if not home or not away:
                continue

            hs = r.get("home_score")
            as_ = r.get("away_score")
            home_goals = int(hs) if hs and hs.isdigit() else None
            away_goals = int(as_) if as_ and as_.isdigit() else None

            result = None
            if home_goals is not None and away_goals is not None:
                if home_goals > away_goals: result = "H"
                elif home_goals < away_goals: result = "A"
                else: result = "D"

            tournament = (r.get("tournament") or "Friendly").strip()
            city = (r.get("city") or "").strip()
            country = (r.get("country") or "").strip()
            neutral = (r.get("neutral") or "FALSE").strip().upper() == "TRUE"

            match_id = f"{date}_{home.replace(' ', '_')}_{away.replace(' ', '_')}"

            conn.execute("""
                INSERT OR IGNORE INTO national_matches
                  (match_id, date, home_team, away_team, home_goals, away_goals,
                   result, tournament, city, country, neutral)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (match_id, date, home, away, home_goals, away_goals,
                  result, tournament, city, country, 1 if neutral else 0))
            inserted += conn.execute("SELECT changes()").fetchone()[0]
        except Exception:
            pass

    conn.commit()
    conn.close()
    return inserted


def collect_national(since_year=2010, status_callback=None):
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    cb(f"Fetching international football results since {since_year}...")
    rows = fetch_csv(RESULTS_URL)
    if not rows:
        cb("ERROR: could not fetch results.csv")
        log_collection("martj42/international_results", "error", 0, "Fetch failed")
        return 0

    cb(f"  -> {len(rows)} total matches in dataset")
    n = insert_matches(rows, since_year=since_year)
    cb(f"  -> {n} new national-team matches stored")

    log_collection("martj42/international_results", "success", n,
                   f"Since {since_year}, total dataset rows: {len(rows)}")
    return n


if __name__ == "__main__":
    from collectors.database import init_db
    init_db()
    collect_national(since_year=2018)
