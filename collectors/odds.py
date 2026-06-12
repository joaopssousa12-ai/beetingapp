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
    # Football — ligas europeias (1ª divisão)
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
    # Football — 2ªs divisões + ligas extra que o backtest cobre. Só são
    # pedidas à API quando estão em época (to_fetch filtra por 'active'), por
    # isso não gastam créditos fora de época. Nomes alinhados com o backtest.
    "soccer_england_efl_champ": "Championship",
    "soccer_spain_segunda_division": "La Liga 2",
    "soccer_germany_bundesliga2": "Bundesliga 2",
    "soccer_italy_serie_b": "Serie B",
    "soccer_france_ligue_two": "Ligue 2",
    "soccer_belgium_first_div": "Jupiler Pro League",
    "soccer_turkey_super_league": "Super Lig",
    "soccer_greece_super_league": "Super League Greece",
    "soccer_spl": "Scottish Premiership",
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

BOOKMAKERS = "pinnacle,bet365,unibet_eu,williamhill,betfair_ex_eu,bwin,betway,marathonbet,onexbet"


def get_active_sports():
    try:
        r = requests.get(f"{BASE}/sports", params={"apiKey": API_KEY}, timeout=15)
        if r.status_code == 200:
            return {s["key"] for s in r.json() if s.get("active")}
    except Exception:
        pass
    return set()


def fetch_odds(sport_key):
    # CRITICAL: totals/btts are SOCCER-only markets. Requesting them for tennis
    # makes The Odds API reject the WHOLE request (HTTP 422 "unknown market"),
    # which silently returned zero tennis events. h2h works for every sport, so
    # only ask for totals/btts on soccer.
    markets = "h2h,totals,btts" if sport_key.startswith("soccer_") else "h2h"
    params = {
        "apiKey": API_KEY,
        "regions": "eu,us",
        "markets": markets,
        "bookmakers": BOOKMAKERS,
        "oddsFormat": "decimal",
    }
    try:
        r = requests.get(f"{BASE}/sports/{sport_key}/odds", params=params, timeout=20)
        remaining = r.headers.get("x-requests-remaining", "?")
        if r.status_code == 200:
            return r.json(), remaining
        # Surface the reason (422 invalid market, 401 bad key, 404 unknown sport…)
        print(f"ODDS_FETCH {sport_key}: HTTP {r.status_code} — {r.text[:160]}", flush=True)
        return None, remaining
    except Exception as e:
        print(f"ODDS_FETCH {sport_key}: {repr(e)}", flush=True)
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
        b365_h2h = book_odds.get("bet365", {}).get("h2h", {})
        pin_home = pin_h2h.get(home)
        pin_away = pin_h2h.get(away)
        pin_draw = pin_h2h.get("Draw")
        x1_home = x1_h2h.get(home)
        x1_away = x1_h2h.get(away)
        x1_draw = x1_h2h.get("Draw")
        b365_home = b365_h2h.get(home)
        b365_away = b365_h2h.get(away)
        b365_draw = b365_h2h.get("Draw")

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
                    b365_home, b365_draw, b365_away,
                    pin_over25, pin_under25, x1_over25, x1_under25, best_over25, best_under25,
                    pin_btts_yes, pin_btts_no, x1_btts_yes, x1_btts_no, best_btts_yes, best_btts_no,
                    updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                event_id, sport_key, sport_name, home, away, commence,
                pin_home, pin_draw, pin_away,
                best_home, best_draw, best_away,
                x1_home, x1_draw, x1_away,
                b365_home, b365_draw, b365_away,
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


def _pretty_sport_name(key):
    """tennis_atp_halle_open -> 'ATP Halle Open'; tennis_wta -> 'WTA Tour'."""
    parts = key.split("_")
    if len(parts) < 2:
        return key
    tour = parts[1].upper()  # atp/wta
    rest = " ".join(p.capitalize() for p in parts[2:])
    return f"{tour} {rest}".strip() if rest else f"{tour} Tour"


def diagnose_tennis():
    """Ground-truth diagnostic: what tennis does The Odds API actually expose
    right now, and does it return events? (Run from the server — it can reach the
    API even when our local sandbox can't.)"""
    out = {"key_set": bool(API_KEY), "remaining": None, "status": None,
           "all_tennis_keys": [], "active_tennis": [], "event_counts": {},
           "stored_tennis": 0}
    if not API_KEY:
        out["error"] = "ODDS_API_KEY not set on the server"
        return out
    try:
        r = requests.get(f"{BASE}/sports", params={"apiKey": API_KEY, "all": "true"}, timeout=20)
        out["status"] = r.status_code
        out["remaining"] = r.headers.get("x-requests-remaining")
        if r.status_code == 200:
            for s in r.json():
                k = s.get("key", "")
                if k.startswith("tennis"):
                    out["all_tennis_keys"].append({"key": k, "active": s.get("active"), "title": s.get("title")})
                    if s.get("active"):
                        out["active_tennis"].append(k)
        else:
            out["body"] = r.text[:300]
    except Exception as e:
        out["error"] = repr(e)
    # Actually try to pull odds for each active tennis key (with the markets fix).
    for k in (out["active_tennis"] or ["tennis_atp", "tennis_wta"])[:10]:
        ev, _ = fetch_odds(k)
        out["event_counts"][k] = len(ev) if ev else 0
    try:
        conn = get_connection()
        row = conn.execute("SELECT COUNT(*) AS c FROM odds_events WHERE sport_key LIKE 'tennis%'").fetchone()
        out["stored_tennis"] = (row["c"] if row else 0)
        conn.close()
    except Exception as e:
        out["stored_err"] = repr(e)
    return out


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

    # The Odds API exposes tennis PER TOURNAMENT (e.g. tennis_atp_halle), which
    # come and go each week — a hardcoded list always misses whatever is live now.
    # So auto-include EVERY currently-active tennis_* key (friendly name derived
    # from the key, unless we already have a nicer one in SPORT_GROUPS).
    for k in active:
        if k.startswith("tennis_") and k not in to_fetch:
            to_fetch[k] = SPORT_GROUPS.get(k) or _pretty_sport_name(k)

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
