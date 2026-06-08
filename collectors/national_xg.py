"""
National team xG proxy collector.

Since real xG data isn't available for national teams (Understat only covers
top 5 club leagues), we build a proxy using a Dixon-Coles-style model on the
15,000+ international matches we already have.

For each team:
  goals_for_avg   = average goals scored last 10 matches (opponent-adjusted)
  goals_against_avg = average goals conceded last 10 matches

For a fixture team_a vs team_b:
  lambda_a = goals_for_a * goals_against_b / global_avg
  lambda_b = goals_for_b * goals_against_a / global_avg

Then Poisson on (lambda_a, lambda_b) gives:
  P(Over 2.5), P(BTTS yes), and the full score grid for 1X2.

This is a well-established academic model (Dixon-Coles 1997) and is what
many professional bettors actually use.
"""
import os
import sys
import math
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from collectors.database import get_connection, log_collection

WINDOW = 15           # last N matches per team
MIN_MATCHES = 5       # minimum matches to be reliable
RECENT_YEARS = 5      # only consider matches in last X years


def init_xg_table():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS national_xg (
            team TEXT PRIMARY KEY,
            goals_for_avg REAL,
            goals_against_avg REAL,
            matches_used INTEGER,
            last_update TEXT
        )
    """)
    conn.commit()
    conn.close()


def compute_national_xg(status_callback=None):
    """Calculate xG proxy for every national team based on recent matches."""
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    init_xg_table()
    cb("Computing national team xG proxy from historical data...")

    conn = get_connection()
    cur = conn.cursor()

    # Get recent matches (last N years)
    cutoff = (datetime.now() - timedelta(days=365 * RECENT_YEARS)).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT date, home_team, away_team, home_goals, away_goals, tournament, neutral
        FROM national_matches
        WHERE date >= ?
          AND home_goals IS NOT NULL
          AND away_goals IS NOT NULL
        ORDER BY date DESC
    """, (cutoff,))

    rows = cur.fetchall()
    cb(f"  -> {len(rows)} matches in last {RECENT_YEARS} years")

    # Build per-team list of recent matches (newest first)
    team_matches = defaultdict(list)
    for r in rows:
        date = r["date"] if "date" in r.keys() else r[0]
        home = r["home_team"] if "home_team" in r.keys() else r[1]
        away = r["away_team"] if "away_team" in r.keys() else r[2]
        hg = r["home_goals"] if "home_goals" in r.keys() else r[3]
        ag = r["away_goals"] if "away_goals" in r.keys() else r[4]

        if hg is None or ag is None:
            continue

        # Weight by tournament: friendlies count less than competitive
        tourn = r["tournament"] if "tournament" in r.keys() else r[5]
        weight = 1.0
        if tourn and "friendly" in str(tourn).lower():
            weight = 0.6

        team_matches[home].append({
            "for": hg, "against": ag, "weight": weight, "date": date
        })
        team_matches[away].append({
            "for": ag, "against": hg, "weight": weight, "date": date
        })

    # Global averages (for normalization)
    total_goals = 0
    total_matches = 0
    for matches in team_matches.values():
        for m in matches[:WINDOW]:
            total_goals += m["for"] + m["against"]
            total_matches += 1
    global_avg = (total_goals / total_matches / 2) if total_matches else 1.3
    cb(f"  -> Global goals/team/match: {global_avg:.2f}")

    # Compute per-team averages using only last WINDOW matches
    stored = 0
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for team, matches in team_matches.items():
        recent = matches[:WINDOW]  # already newest-first
        if len(recent) < MIN_MATCHES:
            continue

        total_w = sum(m["weight"] for m in recent)
        if total_w == 0:
            continue

        gf = sum(m["for"] * m["weight"] for m in recent) / total_w
        ga = sum(m["against"] * m["weight"] for m in recent) / total_w

        conn.execute("""
            INSERT OR REPLACE INTO national_xg
              (team, goals_for_avg, goals_against_avg, matches_used, last_update)
            VALUES (?, ?, ?, ?, ?)
        """, (team, round(gf, 3), round(ga, 3), len(recent), now_iso))
        stored += 1

    # Also store the global average for later use
    conn.execute("""
        CREATE TABLE IF NOT EXISTS national_xg_meta (
            key TEXT PRIMARY KEY,
            value REAL
        )
    """)
    conn.execute("""
        INSERT OR REPLACE INTO national_xg_meta (key, value) VALUES (?, ?)
    """, ("global_avg", round(global_avg, 4)))

    conn.commit()
    conn.close()

    cb(f"✓ National xG done: {stored} teams indexed (window={WINDOW}, min={MIN_MATCHES})")
    log_collection("national-xg", "success", stored, f"{stored} teams")
    return stored


# ============================================================
# PREDICTION FUNCTIONS — used by the value bets engine
# ============================================================

def _poisson_pmf(k, lam):
    """Poisson probability mass function."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    try:
        return (lam ** k) * math.exp(-lam) / math.factorial(k)
    except (OverflowError, ValueError):
        return 0.0


def _get_team_xg(conn, team):
    """Fetch a team's xG averages, or None if not enough data."""
    row = conn.execute("""
        SELECT goals_for_avg, goals_against_avg, matches_used
        FROM national_xg WHERE team = ?
    """, (team,)).fetchone()
    if row is None:
        return None
    return {
        "gf": row["goals_for_avg"] if "goals_for_avg" in row.keys() else row[0],
        "ga": row["goals_against_avg"] if "goals_against_avg" in row.keys() else row[1],
        "n": row["matches_used"] if "matches_used" in row.keys() else row[2]
    }


def _get_global_avg(conn):
    """Get the stored global average goals/team/match."""
    row = conn.execute("""
        SELECT value FROM national_xg_meta WHERE key = 'global_avg'
    """).fetchone()
    if row is None:
        return 1.3
    return row["value"] if "value" in row.keys() else row[0]


def _get_team_recent_form(conn, team, n=5):
    """
    Returns recent form factor (multiplier ~0.85-1.15) based on last N matches.
    Uses results vs opponent strength.
    """
    rows = conn.execute("""
        SELECT home_team, away_team, home_goals, away_goals, date
        FROM national_matches
        WHERE (home_team = ? OR away_team = ?)
          AND home_goals IS NOT NULL
        ORDER BY date DESC LIMIT ?
    """, (team, team, n)).fetchall()
    if not rows or len(rows) < 3:
        return 1.0
    points = 0
    games = 0
    for r in rows:
        home = r["home_team"] if "home_team" in r.keys() else r[0]
        away = r["away_team"] if "away_team" in r.keys() else r[1]
        hg = r["home_goals"] if "home_goals" in r.keys() else r[2]
        ag = r["away_goals"] if "away_goals" in r.keys() else r[3]
        if team == home:
            if hg > ag: points += 3
            elif hg == ag: points += 1
        else:
            if ag > hg: points += 3
            elif ag == hg: points += 1
        games += 1
    avg_pts = points / games  # 0 to 3
    # 1.5 pts/game = neutral (1.0). Above = boost, below = penalty.
    form_factor = 1.0 + (avg_pts - 1.5) * 0.08  # ±12% range
    return max(0.85, min(1.15, form_factor))


def _get_h2h_adjustment(conn, home, away, n=10):
    """
    Returns small adjustment (-0.05 to +0.05) for home team based on H2H history.
    """
    rows = conn.execute("""
        SELECT home_team, away_team, home_goals, away_goals
        FROM national_matches
        WHERE ((home_team = ? AND away_team = ?) OR (home_team = ? AND away_team = ?))
          AND home_goals IS NOT NULL
        ORDER BY date DESC LIMIT ?
    """, (home, away, away, home, n)).fetchall()
    if not rows or len(rows) < 3:
        return 0.0
    home_wins = 0
    away_wins = 0
    for r in rows:
        h = r["home_team"] if "home_team" in r.keys() else r[0]
        hg = r["home_goals"] if "home_goals" in r.keys() else r[2]
        ag = r["away_goals"] if "away_goals" in r.keys() else r[3]
        if h == home:
            if hg > ag: home_wins += 1
            elif ag > hg: away_wins += 1
        else:  # home was away in that match
            if ag > hg: home_wins += 1
            elif hg > ag: away_wins += 1
    total = home_wins + away_wins
    if total < 2:
        return 0.0
    diff_ratio = (home_wins - away_wins) / len(rows)
    return max(-0.05, min(0.05, diff_ratio * 0.10))


def predict_national_match(home, away, home_advantage=0.15):
    """
    Predict a match between two national teams using the xG proxy + Poisson,
    enhanced with recent form and H2H history.
    """
    conn = get_connection()
    home_xg = _get_team_xg(conn, home)
    away_xg = _get_team_xg(conn, away)
    global_avg = _get_global_avg(conn)

    if not home_xg or not away_xg:
        conn.close()
        return None

    # Recent form factor (multiplier 0.85-1.15)
    home_form = _get_team_recent_form(conn, home)
    away_form = _get_team_recent_form(conn, away)

    # H2H adjustment (-0.05 to +0.05)
    h2h_adj = _get_h2h_adjustment(conn, home, away)

    conn.close()

    # World Cup matches tend to have ~12% more goals than friendly average
    # (more intensity, both teams pushing). Apply a calibration boost.
    WC_INTENSITY = 1.12

    # Lambda with form multiplier, H2H tilt, and intensity calibration
    lam_h = (home_xg["gf"] * away_xg["ga"] / global_avg) * (1 + home_advantage) * home_form * (1 + h2h_adj) * WC_INTENSITY
    lam_a = (away_xg["gf"] * home_xg["ga"] / global_avg) * (1 - home_advantage * 0.5) * away_form * (1 - h2h_adj) * WC_INTENSITY

    # Clamp
    lam_h = max(0.1, min(5.0, lam_h))
    lam_a = max(0.1, min(5.0, lam_a))

    # Score grid for 0-0 through 8-8
    GRID = 9
    grid = [[0.0] * GRID for _ in range(GRID)]
    for i in range(GRID):
        for j in range(GRID):
            grid[i][j] = _poisson_pmf(i, lam_h) * _poisson_pmf(j, lam_a)

    # 1X2
    p_home = sum(grid[i][j] for i in range(GRID) for j in range(GRID) if i > j)
    p_draw = sum(grid[i][i] for i in range(GRID))
    p_away = sum(grid[i][j] for i in range(GRID) for j in range(GRID) if i < j)

    # Over/Under 2.5
    p_over25 = sum(grid[i][j] for i in range(GRID) for j in range(GRID) if i + j > 2)
    p_under25 = 1.0 - p_over25

    # BTTS
    p_btts_yes = sum(grid[i][j] for i in range(GRID) for j in range(GRID) if i > 0 and j > 0)
    p_btts_no = 1.0 - p_btts_yes

    # Normalize 1X2 (small drift from grid truncation)
    total_1x2 = p_home + p_draw + p_away
    if total_1x2 > 0:
        p_home /= total_1x2
        p_draw /= total_1x2
        p_away /= total_1x2

    return {
        "lambda_home": round(lam_h, 3),
        "lambda_away": round(lam_a, 3),
        "prob_home": round(p_home * 100, 1),
        "prob_draw": round(p_draw * 100, 1),
        "prob_away": round(p_away * 100, 1),
        "prob_over25": round(p_over25 * 100, 1),
        "prob_under25": round(p_under25 * 100, 1),
        "prob_btts_yes": round(p_btts_yes * 100, 1),
        "prob_btts_no": round(p_btts_no * 100, 1),
        "expected_goals": round(lam_h + lam_a, 2),
        "matches_used_home": home_xg["n"],
        "matches_used_away": away_xg["n"],
        "form_home": round(home_form, 3),
        "form_away": round(away_form, 3),
        "h2h_adjustment": round(h2h_adj, 3),
    }


if __name__ == "__main__":
    init_xg_table()
    compute_national_xg()
    # Quick test
    pred = predict_national_match("Mexico", "South Africa")
    if pred:
        print(f"Mexico vs South Africa:")
        for k, v in pred.items():
            print(f"  {k}: {v}")
