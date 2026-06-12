"""
Betfair Exchange API — primary reference for true odds.

Betfair Exchange is a peer-to-peer betting market with 0% bookmaker margin.
Back prices are the sharpest available probability estimate — used by professional
bettors worldwide as the gold standard reference (sharper than Pinnacle in liquid markets).

Available in Portugal and Luxembourg. Free with any Betfair account.

Setup (Render environment variables):
  BETFAIR_APP_KEY   = your delayed app key (free from developer.betfair.com)
  BETFAIR_USERNAME  = your Betfair email
  BETFAIR_PASSWORD  = your Betfair password

How to get app key:
  1. Create Betfair account at betfair.com
  2. Go to developer.betfair.com → My Account → App Keys
  3. Create "Delayed" app key (free, 1s delay — sufficient for our 6h refresh cycle)
"""

import os
import requests
import unicodedata

LOGIN_URL = "https://identitysso.betfair.com/api/login"
API_BASE = "https://api.betfair.com/exchange/betting/rest/v1.0"
SOCCER_TYPE_ID = "1"
TENNIS_TYPE_ID = "2"


def _norm(s):
    s = unicodedata.normalize("NFD", s or "")
    return " ".join(s.encode("ascii", "ignore").decode("ascii").lower().split())


def _login(username, password, app_key):
    """Returns (token, error). error is None on success, else Betfair's reason."""
    try:
        r = requests.post(
            LOGIN_URL,
            data={"username": username, "password": password},
            headers={
                "X-Application": app_key,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15,
        )
        try:
            data = r.json()
        except Exception:
            return None, f"HTTP {r.status_code}: {r.text[:120]}"
        if data.get("status") == "SUCCESS":
            return data["token"], None
        err = data.get("error") or data.get("status") or "unknown"
        print(f"Betfair login failed: {err}", flush=True)
        return None, err
    except Exception as e:
        print(f"Betfair login error: {e}", flush=True)
        return None, repr(e)


def _headers(token, app_key):
    return {
        "X-Authentication": token,
        "X-Application": app_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _list_markets(token, app_key, event_type_ids):
    """List all upcoming MATCH_ODDS markets for given event types."""
    try:
        r = requests.post(
            f"{API_BASE}/listMarketCatalogue/",
            json={
                "filter": {
                    "eventTypeIds": event_type_ids,
                    "marketTypeCodes": ["MATCH_ODDS"],
                    "inPlayOnly": False,
                },
                "marketProjection": ["EVENT", "RUNNER_DESCRIPTION"],
                "maxResults": "1000",
                "sort": "FIRST_TO_START",
            },
            headers=_headers(token, app_key),
            timeout=20,
        )
        if r.status_code == 200:
            return r.json()
        print(f"Betfair listMarketCatalogue HTTP {r.status_code}: {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"Betfair listMarketCatalogue error: {e}", flush=True)
    return []


def _list_books(market_ids, token, app_key):
    """Get best back prices for market IDs (max 200 per call)."""
    all_books = {}
    for i in range(0, len(market_ids), 200):
        batch = market_ids[i:i + 200]
        try:
            r = requests.post(
                f"{API_BASE}/listMarketBook/",
                json={
                    "marketIds": batch,
                    "priceProjection": {
                        "priceData": ["EX_BEST_OFFERS"],
                        "virtualBets": True,
                    },
                },
                headers=_headers(token, app_key),
                timeout=20,
            )
            if r.status_code == 200:
                for book in r.json():
                    all_books[book["marketId"]] = {
                        r["selectionId"]: r for r in book.get("runners", [])
                    }
        except Exception as e:
            print(f"Betfair listMarketBook error: {e}", flush=True)
    return all_books


def _best_back(runner):
    """Extract best available back price from a runner."""
    offers = (runner or {}).get("ex", {}).get("availableToBack", [])
    return offers[0]["price"] if offers else None


def diagnose_betfair():
    """Ground-truth diagnostic for Betfair login (run on the server). Returns the
    EXACT reason a login fails so we can fix it precisely."""
    app_key = os.environ.get("BETFAIR_APP_KEY", "")
    username = os.environ.get("BETFAIR_USERNAME", "")
    password = os.environ.get("BETFAIR_PASSWORD", "")
    out = {
        "app_key_set": bool(app_key), "username_set": bool(username),
        "password_set": bool(password), "app_key_len": len(app_key),
        "login_status": None, "login_error": None, "markets": None,
    }
    if not all([app_key, username, password]):
        out["login_error"] = "MISSING_ENV_VARS — set BETFAIR_APP_KEY / BETFAIR_USERNAME / BETFAIR_PASSWORD on Render"
        return out
    token, err = _login(username, password, app_key)
    if token:
        out["login_status"] = "SUCCESS"
        try:
            mk = _list_markets(token, app_key, [SOCCER_TYPE_ID, TENNIS_TYPE_ID])
            out["markets"] = len(mk)
        except Exception as e:
            out["markets_error"] = repr(e)
    else:
        out["login_status"] = "FAILED"
        out["login_error"] = err
    return out


def collect_betfair_odds(status_callback=None):
    """
    Fetch Betfair Exchange back prices for all upcoming soccer (and tennis) markets.
    Updates odds_events.bf_home/draw/away columns.

    Back prices are devigged in get_value_bets() using remove_vig_power() —
    the small 2-3% exchange overround is removed the same way as Pinnacle's margin.

    Returns number of events updated.
    """
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    app_key = os.environ.get("BETFAIR_APP_KEY", "")
    username = os.environ.get("BETFAIR_USERNAME", "")
    password = os.environ.get("BETFAIR_PASSWORD", "")

    if not all([app_key, username, password]):
        cb("Betfair: credentials not set (BETFAIR_APP_KEY/USERNAME/PASSWORD) — skipping.")
        return 0

    cb("Betfair Exchange: logging in...")
    token, err = _login(username, password, app_key)
    if not token:
        cb(f"Betfair: login failed — {err}")
        return 0

    # Fetch soccer + tennis markets
    cb("Betfair Exchange: fetching MATCH_ODDS markets...")
    markets = _list_markets(token, app_key, [SOCCER_TYPE_ID, TENNIS_TYPE_ID])
    if not markets:
        cb("Betfair: no markets returned.")
        return 0

    cb(f"Betfair: {len(markets)} markets found.")

    # Parse market metadata: build runner → team name map
    # Soccer MATCH_ODDS runners: Home, Away, The Draw (in that order typically)
    # Tennis MATCH_ODDS runners: Player1, Player2 (no draw)
    market_meta = {}
    for mkt in markets:
        mid = mkt.get("marketId")
        runners = mkt.get("runners", [])
        if not mid or not runners:
            continue

        home_name = away_name = None
        home_sel = away_sel = draw_sel = None

        for runner in runners:
            name = runner.get("runnerName", "")
            sel_id = runner.get("selectionId")
            name_lower = name.lower().strip()
            if name_lower in ("the draw", "draw"):
                draw_sel = sel_id
            elif home_name is None:
                home_name = name
                home_sel = sel_id
            else:
                away_name = name
                away_sel = sel_id

        if home_name and away_name:
            market_meta[mid] = {
                "home": home_name,
                "away": away_name,
                "home_sel": home_sel,
                "draw_sel": draw_sel,
                "away_sel": away_sel,
            }

    # Fetch prices
    cb(f"Betfair: fetching prices for {len(market_meta)} markets...")
    all_books = _list_books(list(market_meta.keys()), token, app_key)

    # Build odds map: (norm_home, norm_away) → (back_home, back_draw, back_away)
    odds_map = {}
    for mid, meta in market_meta.items():
        book = all_books.get(mid, {})
        home_back = _best_back(book.get(meta["home_sel"]))
        draw_back = _best_back(book.get(meta["draw_sel"])) if meta["draw_sel"] else None
        away_back = _best_back(book.get(meta["away_sel"]))

        if home_back and away_back:
            key = (_norm(meta["home"]), _norm(meta["away"]))
            odds_map[key] = (home_back, draw_back, away_back)

    cb(f"Betfair: {len(odds_map)} events with valid back prices.")
    if not odds_map:
        return 0

    # Match against DB fixtures and update
    from collectors.database import get_connection
    conn = get_connection()

    existing = conn.execute(
        "SELECT event_id, home_team, away_team FROM odds_events "
        "WHERE commence_time > datetime('now', '-1 day')"
    ).fetchall()

    db_lookup = {
        (_norm(r["home_team"]), _norm(r["away_team"])): r["event_id"]
        for r in existing
    }

    updated = 0
    for (nh, na), (bh, bd, ba) in odds_map.items():
        db_eid = db_lookup.get((nh, na))
        if db_eid:
            conn.execute(
                "UPDATE odds_events SET bf_home=?, bf_draw=?, bf_away=? WHERE event_id=?",
                (bh, bd, ba, db_eid),
            )
            updated += 1

    conn.commit()
    conn.close()
    cb(f"Betfair Exchange: updated {updated} events with back prices.")
    return updated
