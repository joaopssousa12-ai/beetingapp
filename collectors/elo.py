"""
Elo rating engine.
Computes team/player strength ratings from historical results.
- Football clubs: from football_matches (league games)
- National teams: from national_matches (with tournament weighting)
- Tennis players: surface-specific Elo from tennis_matches

Elo is updated chronologically. Stronger opponent beaten = more points gained.
Margin of victory and match importance scale the update.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from collectors.database import get_connection, log_collection

# --- Elo constants ---
BASE_RATING = 1500
K_FACTOR_FOOTBALL = 20
K_FACTOR_NATIONAL = 30  # fewer games, larger updates
K_FACTOR_TENNIS = 24
HOME_ADVANTAGE = 65  # Elo points equivalent of home field

# Tournament importance multipliers (national teams)
TOURNAMENT_WEIGHT = {
    "FIFA World Cup": 2.0,
    "FIFA World Cup qualification": 1.4,
    "UEFA Euro": 1.8,
    "UEFA Euro qualification": 1.3,
    "UEFA Nations League": 1.2,
    "Copa América": 1.8,
    "African Cup of Nations": 1.5,
    "AFC Asian Cup": 1.5,
    "Friendly": 0.7,
}


def expected_score(rating_a, rating_b):
    """Probability that A beats B given their ratings."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400))


def margin_multiplier(goal_diff):
    """Goal difference scaling (FiveThirtyEight-style)."""
    if goal_diff <= 1:
        return 1.0
    elif goal_diff == 2:
        return 1.5
    else:
        return (11 + goal_diff) / 8.0


def compute_football_elo(status_callback=None):
    """Club football Elo from league matches."""
    def cb(msg):
        print(msg, flush=True)
        if status_callback: status_callback(msg)

    conn = get_connection()
    matches = conn.execute("""
        SELECT date, home_team, away_team, home_goals, away_goals, league_name
        FROM football_matches
        WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL
        ORDER BY date ASC
    """).fetchall()

    ratings = {}
    games_count = {}

    for m in matches:
        h, a = m["home_team"], m["away_team"]
        if not h or not a:
            continue
        rh = ratings.get(h, BASE_RATING)
        ra = ratings.get(a, BASE_RATING)

        # Expected with home advantage
        exp_h = expected_score(rh + HOME_ADVANTAGE, ra)
        exp_a = 1 - exp_h

        hg, ag = m["home_goals"], m["away_goals"]
        if hg > ag:
            score_h, score_a = 1, 0
        elif hg < ag:
            score_h, score_a = 0, 1
        else:
            score_h, score_a = 0.5, 0.5

        mult = margin_multiplier(abs(hg - ag))
        k = K_FACTOR_FOOTBALL * mult

        ratings[h] = rh + k * (score_h - exp_h)
        ratings[a] = ra + k * (score_a - exp_a)
        games_count[h] = games_count.get(h, 0) + 1
        games_count[a] = games_count.get(a, 0) + 1

    # Store
    conn.execute("DELETE FROM elo_ratings WHERE category = 'football_club'")
    for team, rating in ratings.items():
        if games_count.get(team, 0) >= 5:  # min games for reliability
            conn.execute("""
                INSERT OR REPLACE INTO elo_ratings (entity, category, surface, rating, games, updated_at)
                VALUES (?, 'football_club', NULL, ?, ?, datetime('now'))
            """, (team, round(rating, 1), games_count[team]))
    conn.commit()
    conn.close()
    cb(f"  Football club Elo: {len(ratings)} teams rated")
    return len(ratings)


def compute_national_elo(status_callback=None):
    """National team Elo with tournament weighting."""
    def cb(msg):
        print(msg, flush=True)
        if status_callback: status_callback(msg)

    conn = get_connection()
    matches = conn.execute("""
        SELECT date, home_team, away_team, home_goals, away_goals, tournament, neutral
        FROM national_matches
        WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL
        ORDER BY date ASC
    """).fetchall()

    ratings = {}
    games_count = {}

    for m in matches:
        h, a = m["home_team"], m["away_team"]
        if not h or not a:
            continue
        rh = ratings.get(h, BASE_RATING)
        ra = ratings.get(a, BASE_RATING)

        # Home advantage only if not neutral venue
        ha = 0 if m["neutral"] else HOME_ADVANTAGE
        exp_h = expected_score(rh + ha, ra)
        exp_a = 1 - exp_h

        hg, ag = m["home_goals"], m["away_goals"]
        if hg > ag:
            score_h, score_a = 1, 0
        elif hg < ag:
            score_h, score_a = 0, 1
        else:
            score_h, score_a = 0.5, 0.5

        tw = TOURNAMENT_WEIGHT.get(m["tournament"], 1.0)
        mult = margin_multiplier(abs(hg - ag))
        k = K_FACTOR_NATIONAL * mult * tw

        ratings[h] = rh + k * (score_h - exp_h)
        ratings[a] = ra + k * (score_a - exp_a)
        games_count[h] = games_count.get(h, 0) + 1
        games_count[a] = games_count.get(a, 0) + 1

    conn.execute("DELETE FROM elo_ratings WHERE category = 'national'")
    for team, rating in ratings.items():
        if games_count.get(team, 0) >= 5:
            conn.execute("""
                INSERT OR REPLACE INTO elo_ratings (entity, category, surface, rating, games, updated_at)
                VALUES (?, 'national', NULL, ?, ?, datetime('now'))
            """, (team, round(rating, 1), games_count[team]))
    conn.commit()
    conn.close()
    cb(f"  National team Elo: {len(ratings)} teams rated")
    return len(ratings)


def compute_tennis_elo(status_callback=None):
    """Surface-specific tennis Elo."""
    def cb(msg):
        print(msg, flush=True)
        if status_callback: status_callback(msg)

    conn = get_connection()
    matches = conn.execute("""
        SELECT tourney_date, winner_name, loser_name, surface
        FROM tennis_matches
        WHERE winner_name IS NOT NULL AND loser_name IS NOT NULL
        ORDER BY tourney_date ASC
    """).fetchall()

    # Separate ratings per surface + overall
    ratings = {}  # (player, surface) -> rating
    games = {}

    for m in matches:
        w, l = m["winner_name"], m["loser_name"]
        surface = (m["surface"] or "Hard").strip() or "Hard"
        if not w or not l:
            continue

        for surf in [surface, "All"]:
            kw = (w, surf)
            kl = (l, surf)
            rw = ratings.get(kw, BASE_RATING)
            rl = ratings.get(kl, BASE_RATING)
            exp_w = expected_score(rw, rl)
            ratings[kw] = rw + K_FACTOR_TENNIS * (1 - exp_w)
            ratings[kl] = rl + K_FACTOR_TENNIS * (0 - (1 - exp_w))
            games[kw] = games.get(kw, 0) + 1
            games[kl] = games.get(kl, 0) + 1

    conn.execute("DELETE FROM elo_ratings WHERE category = 'tennis'")
    stored = 0
    for (player, surface), rating in ratings.items():
        if games.get((player, surface), 0) >= 10:
            conn.execute("""
                INSERT OR REPLACE INTO elo_ratings (entity, category, surface, rating, games, updated_at)
                VALUES (?, 'tennis', ?, ?, ?, datetime('now'))
            """, (player, surface, round(rating, 1), games[(player, surface)]))
            stored += 1
    conn.commit()
    conn.close()
    cb(f"  Tennis Elo: {stored} player-surface ratings")
    return stored


def collect_elo(status_callback=None):
    def cb(msg):
        print(msg, flush=True)
        if status_callback: status_callback(msg)

    cb("Computing Elo ratings...")
    n1 = compute_football_elo(status_callback)
    n2 = compute_national_elo(status_callback)
    n3 = compute_tennis_elo(status_callback)
    total = n1 + n2 + n3
    log_collection("elo ratings", "success", total, f"FB:{n1} Nat:{n2} Tennis:{n3}")
    cb(f"✓ Elo done: {n1} clubs, {n2} nations, {n3} tennis ratings.")
    return total


if __name__ == "__main__":
    from collectors.database import init_db
    init_db()
    collect_elo()
