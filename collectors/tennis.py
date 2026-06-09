"""Tennis collector - JeffSackmann ATP/WTA CSVs (no pandas, uses stdlib csv)."""
import requests
import csv
import io
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from collectors.database import get_connection, log_collection

BASE_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_{tour}/master/{tour}_matches_{year}.csv"
TOURS = ["atp", "wta"]

KEEP_COLS = [
    "match_id","tourney_id","tourney_name","surface","tourney_level","tourney_date","round","best_of",
    "winner_name","winner_id","winner_hand","winner_age","winner_rank","winner_rank_points",
    "loser_name","loser_id","loser_hand","loser_age","loser_rank","loser_rank_points",
    "score","minutes",
    "w_ace","w_df","w_svpt","w_1stIn","w_1stWon","w_2ndWon","w_bpSaved","w_bpFaced",
    "l_ace","l_df","l_svpt","l_1stIn","l_1stWon","l_2ndWon","l_bpSaved","l_bpFaced",
]

NUMERIC_COLS = {
    "winner_age","loser_age","winner_rank","loser_rank",
    "winner_rank_points","loser_rank_points","minutes","best_of",
    "w_ace","w_df","w_svpt","w_1stIn","w_1stWon","w_2ndWon","w_bpSaved","w_bpFaced",
    "l_ace","l_df","l_svpt","l_1stIn","l_1stWon","l_2ndWon","l_bpSaved","l_bpFaced",
}


def parse_num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        if f == int(f):
            return int(f)
        return round(f, 2)
    except (ValueError, TypeError):
        return None


def parse_date(v):
    if not v or v == "":
        return None
    try:
        return datetime.datetime.strptime(str(v).strip(), "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return None


def fetch_year(tour, year):
    """Returns list of dicts (CSV rows) or None."""
    url = BASE_URL.format(tour=tour, year=year)
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200 or len(r.content) < 500:
            return None
        text = r.text
        reader = csv.DictReader(io.StringIO(text))
        return list(reader)
    except Exception:
        return None


def clean_rows(rows, tour, year):
    cleaned = []
    for i, raw in enumerate(rows):
        # Required fields
        date = parse_date(raw.get("tourney_date", ""))
        winner = (raw.get("winner_name") or "").strip()
        loser = (raw.get("loser_name") or "").strip()
        if not date or not winner or not loser:
            continue

        tid = raw.get("tourney_id", "unknown")
        match_num = raw.get("match_num", str(i))
        match_id = f"{date}_{tour}_{tid}_{match_num}"

        out = {"match_id": match_id}
        for col in KEEP_COLS[1:]:  # skip match_id
            val = raw.get(col, None)
            if val == "":
                val = None
            if col == "tourney_date":
                val = date
            elif col in NUMERIC_COLS:
                val = parse_num(val)
            out[col] = val
        cleaned.append(out)
    return cleaned


def insert_matches(rows):
    if not rows:
        return 0
    conn = get_connection()
    inserted = 0
    for d in rows:
        try:
            cols = list(d.keys())
            placeholders = ",".join(["?" for _ in cols])
            col_names = ",".join(cols)
            cur = conn.execute(
                f"INSERT OR IGNORE INTO tennis_matches ({col_names}) VALUES ({placeholders})",
                list(d.values())
            )
            inserted += max(cur.rowcount, 0)
        except Exception:
            pass
    conn.commit()
    conn.close()
    return inserted


def collect_tennis(tours=None, start_year=2015, end_year=None, status_callback=None):
    if end_year is None:
        end_year = datetime.datetime.now().year

    target_tours = tours or TOURS
    total = 0

    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    for tour in target_tours:
        for year in range(start_year, end_year + 1):
            cb(f"Fetching {tour.upper()} {year}...")
            rows = fetch_year(tour, year)
            if not rows:
                continue
            cleaned = clean_rows(rows, tour, year)
            if not cleaned:
                continue
            n = insert_matches(cleaned)
            total += n

    log_collection("JeffSackmann/tennis_atp+wta", "success", total,
                   f"Tours: {target_tours}, Years: {start_year}-{end_year}")
    return total


if __name__ == "__main__":
    from collectors.database import init_db
    init_db()
    n = collect_tennis(start_year=2024)
    print(f"Done. Total: {n}")
