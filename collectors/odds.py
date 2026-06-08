"""Live odds collector via The Odds API."""
import os
import requests
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from collectors.database import get_connection, log_collection

API_KEY = os.environ.get("ODDS_API_KEY", "")
BASE = "https://api.the-odds-api.com/v4"

SPORT_GROUPS = {
    # ⚽ MUNDIAL 2026 — 11 Jun a 19 Jul
    "soccer_fifa_world_cup": "FIFA World Cup 2026",
    "soccer_fifa_club_world_cup": "FIFA Club World Cup 2026",
    # Football — ligas europeias
    "soccer_epl": "Premier League",
    "soccer_spain_la_liga": "La Liga",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_uefa_champs_league": "Champions League",
    "soccer_uefa_europa_league": "Europa League",
    "soccer_portugal_primeira_liga": "Primeira Liga",
    "soccer_netherlands_eredivisie": "Eredivisie",
    "soccer_conmebol_copa_america": "Copa América",
    "soccer_uefa_euro": "UEFA Euro",
    # Tennis — Grand Slams
    "tennis_atp_french_open": "ATP Roland Garros",
    "tennis_wta_french_open": "WTA Roland Garros",
    "tennis_atp_wimbledon": "ATP Wimbledon",
    "tennis_wta_wimbledon": "WTA Wimbledon",
    "tennis_atp_us_open": "ATP US Open",
    "tennis_wta_us_open": "WTA US Open",
    "tennis_atp_aus_open_singles": "ATP Australian Open",
    "tennis_wta_aus_open_singles": "WTA Australian Open",
    # Tennis — Tours (non-Slam events, always active)
    "tennis_atp": "ATP Tour",
    "tennis_wta": "WTA Tour",
    # Baseball
    "baseball_npb": "NPB (Japan)",
    "baseball_kbo": "KBO (Korea)",
    "baseball_mlb": "MLB",
    # Basketball
    "basketball_nba": "NBA",
    "basketball_nba_championship_winner": "NBA Finals",
    # Cricket (in season)
    "cricket_test_match": "Cricket Test",
    "cricket_odi": "Cricket ODI",
    "cricket_t20": "Cricket T20",
    # Combat sports
    "mma_mixed_martial_arts": "MMA",
    "boxing_boxing": "Boxing",
    # Rugby
    "rugbyleague_nrl": "NRL",
    "rugbyunion": "Rugby Union",
    # American football
    "americanfootball_cfl": "CFL",
}

BOOKMAKERS = "pinnacle,onexbet,williamhill,betfair_ex_eu,unibet_eu,bet365"


def get_active_sports():
    try:
        r = requests.get(f"{BASE}/sports", params={"apiKey": API_KEY}, timeout=15)
        if r.status_code == 200:
            return {s["key"] for s in r.json() if s.get("active")}
    except Exception:
        pass
    return set()


def fetch_odds(sport_key):
    params = {
        "apiKey": API_KEY,
        "regions": "eu",
        "markets": "h2h,totals,btts",
        "bookmakers": BOOKMAKERS,
        "oddsFormat": "decimal",
    }
    try:
        r = requests.get(f"{BASE}/sports/{sport_key}/odds", params=params, timeout=20)
        remaining = r.headers.get("x-requests-remaining", "?")
        if r.status_code == 200:
            return r.json(), remaining
        return None, remaining
    except Exception:
        return None, "?"


def parse_and_store(events, sport_key, sport_name):
    conn = get_connection()
    inserted = 0
    for ev in events:
        event_id = ev.get("id")
        home = ev.get("home_team", "")
        away = ev.get("away_team", "")
        commence = ev.get("commence_time", "")

        # book_odds[bookmaker][market] = {outcome_name: price, ...}
        # For totals: {outcome_name + "|" + point: price}
        book_odds = {}
        for bm in ev.get("bookmakers", []):
            bm_key = bm.get("key")
            book_odds.setdefault(bm_key, {})
            for market in bm.get("markets", []):
                mkey = market.get("key")
                outcomes = {}
                for o in market.get("outcomes", []):
                    name = o.get("name", "")
                    price = o.get("price")
                    if mkey == "totals":
                        point = o.get("point")
                        outcomes[f"{name}|{point}"] = price
                    else:
                        outcomes[name] = price
                book_odds[bm_key][mkey] = outcomes

        # --- H2H ---
        pin_h2h = book_odds.get("pinnacle", {}).get("h2h", {})
        x1_h2h = book_odds.get("onexbet", {}).get("h2h", {})
        pin_home = pin_h2h.get(home)
        pin_away = pin_h2h.get(away)
        pin_draw = pin_h2h.get("Draw")
        x1_home = x1_h2h.get(home)
        x1_away = x1_h2h.get(away)
        x1_draw = x1_h2h.get("Draw")

        def best_of(market_key, outcome):
            vals = []
            for bm, mkts in book_odds.items():
                v = (mkts.get(market_key) or {}).get(outcome)
                if v: vals.append(v)
            return max(vals) if vals else None

        best_home = best_of("h2h", home)
        best_away = best_of("h2h", away)
        best_draw = best_of("h2h", "Draw")

        # --- TOTALS (Over/Under 2.5 specifically) ---
        def get_total(book_key, side):
            mkt = book_odds.get(book_key, {}).get("totals", {})
            # Prefer 2.5 exact
            for k, v in mkt.items():
                if k == f"{side}|2.5":
                    return v
            return None

        pin_over25 = get_total("pinnacle", "Over")
        pin_under25 = get_total("pinnacle", "Under")
        x1_over25 = get_total("onexbet", "Over")
        x1_under25 = get_total("onexbet", "Under")
        best_over25 = max([v for v in [get_total(bm, "Over") for bm in book_odds] if v], default=None)
        best_under25 = max([v for v in [get_total(bm, "Under") for bm in book_odds] if v], default=None)

        # --- BTTS ---
        def get_btts(book_key, side):
            mkt = book_odds.get(book_key, {}).get("btts", {})
            return mkt.get(side)

        pin_btts_yes = get_btts("pinnacle", "Yes")
        pin_btts_no = get_btts("pinnacle", "No")
        x1_btts_yes = get_btts("onexbet", "Yes")
        x1_btts_no = get_btts("onexbet", "No")
        best_btts_yes = max([v for v in [get_btts(bm, "Yes") for bm in book_odds] if v], default=None)
        best_btts_no = max([v for v in [get_btts(bm, "No") for bm in book_odds] if v], default=None)

        try:
            conn.execute("""
                INSERT OR REPLACE INTO odds_events (
                    event_id, sport_key, sport_name, home_team, away_team, commence_time,
                    pin_home, pin_draw, pin_away,
                    best_home, best_draw, best_away,
                    x1_home, x1_draw, x1_away,
                    pin_over25, pin_under25, x1_over25, x1_under25, best_over25, best_under25,
                    pin_btts_yes, pin_btts_no, x1_btts_yes, x1_btts_no, best_btts_yes, best_btts_no,
                    updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                event_id, sport_key, sport_name, home, away, commence,
                pin_home, pin_draw, pin_away,
                best_home, best_draw, best_away,
                x1_home, x1_draw, x1_away,
                pin_over25, pin_under25, x1_over25, x1_under25, best_over25, best_under25,
                pin_btts_yes, pin_btts_no, x1_btts_yes, x1_btts_no, best_btts_yes, best_btts_no,
                datetime.now().strftime("%Y-%m-%d %H:%M")
            ))
            # Also save snapshot to history for line movement tracking
            conn.execute("""
                INSERT INTO odds_history (
                    event_id, captured_at,
                    pin_home, pin_draw, pin_away,
                    best_home, best_draw, best_away,
                    x1_home, x1_draw, x1_away,
                    pin_over25, pin_under25,
                    pin_btts_yes, pin_btts_no
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                event_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                pin_home, pin_draw, pin_away,
                best_home, best_draw, best_away,
                x1_home, x1_draw, x1_away,
                pin_over25, pin_under25,
                pin_btts_yes, pin_btts_no
            ))
            inserted += 1
        except Exception as e:
            print(f"Insert error: {e}", flush=True)

    conn.commit()
    conn.close()
    return inserted


def collect_odds(status_callback=None):
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    if not API_KEY:
        cb("ERROR: ODDS_API_KEY not set.")
        log_collection("the-odds-api", "error", 0, "No API key")
        return 0

    cb("Checking active sports...")
    active = get_active_sports()
    cb(f"API reports {len(active)} active sports.")

    # Limpar eventos passados
    try:
        conn = get_connection()
        conn.execute("DELETE FROM odds_events WHERE commence_time < datetime('now')")
        conn.commit()
        conn.close()
    except Exception:
        pass

    # Always fetch World Cup and Club World Cup regardless of active status
    ALWAYS_FETCH = {
        "soccer_fifa_world_cup",
        "soccer_fifa_club_world_cup",
        "tennis_atp_french_open",
        "tennis_wta_french_open",
        "tennis_atp_wimbledon",
        "tennis_wta_wimbledon",
        "tennis_atp",
        "tennis_wta",
        "baseball_mlb",
        "mma_mixed_martial_arts",
    }

    to_fetch = {k: v for k, v in SPORT_GROUPS.items() if k in active or k in ALWAYS_FETCH}
    cb(f"Fetching odds for {len(to_fetch)} sports from our watchlist...")

    if not to_fetch:
        cb("No watched sports currently in season.")
        log_collection("the-odds-api", "success", 0, "No watched sports in season")
        return 0

    total = 0
    remaining = "?"
    for sport_key, sport_name in to_fetch.items():
        cb(f"  Fetching: {sport_name}...")
        events, remaining = fetch_odds(sport_key)
        if not events:
            cb(f"  -> no events")
            continue
        n = parse_and_store(events, sport_key, sport_name)
        total += n
        cb(f"  -> {n} events (credits left: {remaining})")

    log_collection("the-odds-api", "success", total, f"Credits remaining: {remaining}")
    cb(f"✓ Odds done: {total} events stored. Credits left: {remaining}")
    return total


if __name__ == "__main__":
    from collectors.database import init_db
    init_db()
    collect_odds()
