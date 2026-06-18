"""
OddsPapi — free aggregator with Pinnacle odds (250 requests/month free tier).

Setup (Render environment variables):
  ODDSPAPI_KEY = your API key from oddspapi.io

How to get key:
  1. Register at https://oddspapi.io (no credit card)
  2. Copy your API key from the dashboard
  3. Add ODDSPAPI_KEY to Render env vars

What this does:
  Fetches Pinnacle h2h odds for soccer events and updates pin_home/draw/away
  in odds_events. Runs before Betfair so Betfair can override in liquid markets.

250 requests/month = ~8/day at our 6h refresh cycle. Well within limits.
"""

import os
import requests
import unicodedata
from datetime import datetime

API_KEY = os.environ.get("ODDSPAPI_KEY", "")

# OddsPapi — aggregator with 350+ bookmakers including Pinnacle (free tier).
# Fixture-based API (NOT Odds-API-compatible): numeric sportId, /fixtures and
# /odds-by-tournaments. apiKey goes in the query string.
BASE = "https://api.oddspapi.io/v4"
SOCCER_SPORT_ID = 10  # OddsPapi's numeric sportId for soccer (covers all competitions)

# Legacy Odds-API sport keys — no longer used to call OddsPapi; kept for reference.
SOCCER_SPORTS = [
    "soccer_fifa_world_cup",
    "soccer_fifa_club_world_cup",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_portugal_primeira_liga",
]


def _norm(s):
    s = unicodedata.normalize("NFD", s or "")
    return " ".join(s.encode("ascii", "ignore").decode("ascii").lower().split())


def _api_get(path, params):
    """GET on the OddsPapi v4 API. Returns (json|None, remaining, status_code).
    A 401 maps remaining to 'invalid_key' so callers can report it clearly."""
    try:
        r = requests.get(f"{BASE}{path}", params={**params, "apiKey": API_KEY}, timeout=25)
        remaining = r.headers.get("x-requests-remaining", "?")
        if r.status_code == 200:
            try:
                return r.json(), remaining, 200
            except Exception:
                return None, remaining, 200
        if r.status_code == 401:
            return None, "invalid_key", 401
        return None, remaining, r.status_code
    except Exception as e:
        print(f"OddsPapi {path} error: {e}", flush=True)
        return None, "?", None


def _pin_moneyline(fixture):
    """Extract Pinnacle 1X2 (home, draw, away) decimals from an odds-by-tournaments
    fixture. Shape: bookmakerOdds.pinnacle["101"].outcomes[*].players["0"], where
    bookmakerOutcomeId ∈ {home,draw,away} and `price` is the decimal odd. We prefer
    the documented moneyline marketId "101" but fall back to scanning every market
    for one whose outcomes are labelled home/draw/away (so a marketId change can't
    silently break it)."""
    bo = (fixture.get("bookmakerOdds") or {}).get("pinnacle") or {}
    if not isinstance(bo, dict):
        return None, None, None
    markets = ([bo["101"]] if "101" in bo else []) + [m for k, m in bo.items() if k != "101"]
    for mkt in markets:
        if not isinstance(mkt, dict):
            continue
        got = {}
        for _oid, outcome in (mkt.get("outcomes") or {}).items():
            player = (outcome.get("players") or {}).get("0") or {}
            side, price = player.get("bookmakerOutcomeId"), player.get("price")
            if side in ("home", "draw", "away") and price:
                got[side] = price
        if "home" in got and "away" in got:        # a valid 1X2 market
            return got.get("home"), got.get("draw"), got.get("away")
    return None, None, None


def diagnose_oddspapi():
    """INSTRUMENTATION: walk OddsPapi's REAL (fixture-based) API and dump raw samples
    so we can see the exact response shape from prod (the server can reach the API even
    when a dev sandbox can't). OddsPapi is NOT Odds-API-compatible: it uses a numeric
    sportId, /fixtures (sportId + from/to date range, max 10 days) and
    /odds-by-tournaments?bookmaker=pinnacle&tournamentIds=... — our old code called
    /sports/{sport_key}/odds, which 404s. Open /api/oddspapi/test to read the shape."""
    from datetime import timedelta
    out = {"key_set": bool(API_KEY), "base": BASE, "steps": {}}
    if not API_KEY:
        out["error"] = "ODDSPAPI_KEY not set on the server"
        return out

    def _call(path, params):
        info = {"path": path, "params": params}
        try:
            r = requests.get(f"{BASE}{path}", params={**params, "apiKey": API_KEY}, timeout=25)
            info["http_status"] = r.status_code
            info["remaining"] = r.headers.get("x-requests-remaining")
            try:
                info["body"] = r.json()
            except Exception:
                info["text"] = r.text[:500]
        except Exception as e:
            info["error"] = repr(e)
        return info

    # 1) /sports — small list; keep enough to read soccer's numeric sportId.
    s = _call("/sports", {})
    body = s.pop("body", None)
    s["sample"] = body[:5] if isinstance(body, list) else body
    soccer_id = None
    if isinstance(body, list):
        for sp in body:
            if str(sp.get("slug", "")).lower() in ("soccer", "football"):
                soccer_id = sp.get("sportId") or sp.get("id")
                break
    out["steps"]["sports"] = s
    out["soccer_sportId"] = soccer_id or 10

    sid = out["soccer_sportId"]
    today = datetime.utcnow().date()

    # DB event name-pairs (same matching collect_oddspapi uses) — to see where odds are lost.
    db_pairs = set()
    try:
        from collectors.database import get_connection
        conn = get_connection()
        for r in conn.execute("SELECT home_team, away_team FROM odds_events "
                              "WHERE commence_time > datetime('now', '-1 day')").fetchall():
            db_pairs.add((_norm(r["home_team"]), _norm(r["away_team"])))
        conn.close()
    except Exception as e:
        out["db_error"] = repr(e)
    out["db_event_pairs"] = len(db_pairs)

    # 2) /fixtures — match against DB, collect matched tournamentIds + samples.
    matched_tids, matched_samples = [], []
    f = _call("/fixtures", {"sportId": sid, "from": today.isoformat(),
                            "to": (today + timedelta(days=10)).isoformat(), "hasOdds": "true"})
    fb = f.pop("body", None)
    if isinstance(fb, list):
        f["count"] = len(fb)
        f["first"] = fb[0] if fb else None
        for it in fb:
            if not isinstance(it, dict):
                continue
            key = (_norm(it.get("participant1Name")), _norm(it.get("participant2Name")))
            if key in db_pairs:
                tid = it.get("tournamentId")
                if tid and tid not in matched_tids:
                    matched_tids.append(tid)
                if len(matched_samples) < 5:
                    matched_samples.append({"home": it.get("participant1Name"),
                                            "away": it.get("participant2Name"),
                                            "tournamentId": tid,
                                            "tournamentName": it.get("tournamentName")})
    elif fb is not None:
        f["body"] = fb
    out["steps"]["fixtures"] = f
    out["matched_tournamentIds"] = matched_tids[:10]
    out["matched_fixture_samples"] = matched_samples

    # 3) /odds-by-tournaments for OUR matched tournaments — TRACE 1X2 extraction per fixture.
    if matched_tids:
        o = _call("/odds-by-tournaments", {"bookmaker": "pinnacle",
                  "tournamentIds": ",".join(str(t) for t in matched_tids[:5])})
        ob = o.pop("body", None)
        trace = []
        if isinstance(ob, list):
            o["count"] = len(ob)
            for fx in ob[:10]:
                if not isinstance(fx, dict):
                    continue
                pin = (fx.get("bookmakerOdds") or {}).get("pinnacle") or {}
                h, d, a = _pin_moneyline(fx)
                key = (_norm(fx.get("participant1Name")), _norm(fx.get("participant2Name")))
                trace.append({"home": fx.get("participant1Name"),
                              "away": fx.get("participant2Name"),
                              "in_db": key in db_pairs,
                              "has_pinnacle": bool(pin),
                              "pinnacle_marketIds": (list(pin.keys())[:10] if isinstance(pin, dict) else None),
                              "extracted_1x2": [h, d, a]})
        elif ob is not None:
            o["body"] = ob
        o["trace"] = trace
        out["steps"]["odds_by_tournaments"] = o
    else:
        out["steps"]["odds_by_tournaments"] = {"skipped": "no DB-matched tournaments from /fixtures"}

    return out


def collect_oddspapi(status_callback=None):
    """
    Fetch Pinnacle odds from OddsPapi and update pin_home/draw/away in DB.
    Returns number of events updated.
    """
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    if not API_KEY:
        cb("OddsPapi: ODDSPAPI_KEY not set — skipping.")
        return 0

    from collectors.database import get_connection
    from datetime import timedelta

    # Load upcoming DB events for name matching: (norm_home, norm_away) -> event_id.
    conn = get_connection()
    existing = conn.execute(
        "SELECT event_id, home_team, away_team FROM odds_events "
        "WHERE commence_time > datetime('now', '-1 day')"
    ).fetchall()
    conn.close()
    db_lookup = {
        (_norm(r["home_team"]), _norm(r["away_team"])): r["event_id"]
        for r in existing
    }
    if not db_lookup:
        cb("OddsPapi: no upcoming events in DB — skipping.")
        return 0

    # 1) Soccer fixtures with odds over the next 10 days (the API's max range).
    today = datetime.utcnow().date()
    fixtures, remaining, status = _api_get("/fixtures", {
        "sportId": SOCCER_SPORT_ID,
        "from": today.isoformat(),
        "to": (today + timedelta(days=10)).isoformat(),
        "hasOdds": "true",
    })
    if status == 401 or remaining == "invalid_key":
        cb("OddsPapi: invalid API key — check ODDSPAPI_KEY in Render (no spaces/newlines).")
        return 0
    if not isinstance(fixtures, list) or not fixtures:
        cb(f"OddsPapi: no soccer fixtures returned (HTTP {status}, credits {remaining}).")
        return 0

    # 2) Keep only tournaments that contain a fixture matching one of our DB events —
    #    so we request Pinnacle odds for the relevant comps only (saves quota).
    wanted_tournaments = set()
    for fx in fixtures:
        if not isinstance(fx, dict):
            continue
        key = (_norm(fx.get("participant1Name")), _norm(fx.get("participant2Name")))
        if key in db_lookup and fx.get("tournamentId"):
            wanted_tournaments.add(fx["tournamentId"])
    if not wanted_tournaments:
        cb(f"OddsPapi: {len(fixtures)} fixtures fetched, none matched our DB events "
           f"(team-name mismatch?).")
        return 0
    cb(f"OddsPapi: {len(wanted_tournaments)} tournament(s) match our events — "
       f"fetching Pinnacle 1X2...")

    # 3) Pull Pinnacle odds for those tournaments (batched) and extract 1X2.
    pin_odds = {}  # (norm_home, norm_away) -> (home, draw, away)
    tids = list(wanted_tournaments)
    for i in range(0, len(tids), 20):
        batch = tids[i:i + 20]
        odds_fx, remaining, status = _api_get("/odds-by-tournaments", {
            "bookmaker": "pinnacle",
            "tournamentIds": ",".join(str(t) for t in batch),
        })
        if not isinstance(odds_fx, list):
            continue
        for fx in odds_fx:
            if not isinstance(fx, dict):
                continue
            h, d, a = _pin_moneyline(fx)
            if h and a:
                key = (_norm(fx.get("participant1Name")), _norm(fx.get("participant2Name")))
                pin_odds[key] = (h, d, a)

    if not pin_odds:
        cb(f"OddsPapi: matched tournaments but extracted no Pinnacle 1X2 (credits {remaining}).")
        return 0

    # 4) Update pin_home/draw/away on the matching DB events.
    conn = get_connection()
    updated = 0
    for (nh, na), (ph, pd, pa) in pin_odds.items():
        eid = db_lookup.get((nh, na))
        if eid:
            conn.execute(
                "UPDATE odds_events SET pin_home=?, pin_draw=?, pin_away=? WHERE event_id=?",
                (ph, pd, pa, eid),
            )
            updated += 1
    conn.commit()
    conn.close()
    cb(f"OddsPapi: updated {updated} events with Pinnacle 1X2. Credits left: {remaining}")
    return updated
