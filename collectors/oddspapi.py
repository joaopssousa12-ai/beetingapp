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


def _extract_1x2(book):
    """Extract 1X2 (home, draw, away) decimals from ONE bookmaker's odds block, i.e.
    bookmakerOdds[<bookmaker>]. Markets live under book["markets"] ({marketId: mkt});
    moneyline is marketId "101" — outcomes[*].players["0"] carry bookmakerOutcomeId
    ∈ {home,draw,away} and a decimal `price`. We prefer "101" but fall back to
    scanning any market whose outcomes are labelled home/draw/away (so a marketId
    change can't silently break it). Generic so it serves Pinnacle AND 1xBet."""
    if not isinstance(book, dict):
        return None, None, None
    container = book.get("markets") if isinstance(book.get("markets"), dict) else book
    if not isinstance(container, dict):
        return None, None, None
    markets = ([container["101"]] if "101" in container else []) + \
              [m for k, m in container.items() if k != "101"]
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


def _pin_moneyline(fixture):
    """Pinnacle 1X2 from an odds-by-tournaments fixture (thin wrapper over _extract_1x2)."""
    return _extract_1x2((fixture.get("bookmakerOdds") or {}).get("pinnacle") or {})


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

    # 2) /fixtures — match against DB; collect matched tournamentIds + fixtureIds + samples.
    matched_tids, matched_samples, matched_fids = [], [], set()
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
                if it.get("fixtureId"):
                    matched_fids.add(it["fixtureId"])
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

    # 3) /odds-by-tournaments for OUR matched tournaments — TRACE 1X2 extraction per fixture
    #    (matched by fixtureId, since the odds endpoint carries no team names).
    if matched_tids:
        o = _call("/odds-by-tournaments", {"bookmaker": "pinnacle",
                  "tournamentIds": ",".join(str(t) for t in matched_tids[:5])})
        ob = o.pop("body", None)
        trace = []
        if isinstance(ob, list):
            o["count"] = len(ob)
            for fx in ob[:12]:
                if not isinstance(fx, dict):
                    continue
                pin = (fx.get("bookmakerOdds") or {}).get("pinnacle") or {}
                mkts = pin.get("markets") if isinstance(pin, dict) else None
                h, d, a = _pin_moneyline(fx)
                fid = fx.get("fixtureId")
                row = {"fixtureId": fid,
                       "in_matched_fids": fid in matched_fids,
                       "has_pinnacle": bool(pin),
                       "markets_keys": (list(mkts.keys())[:12] if isinstance(mkts, dict) else None),
                       "extracted_1x2": [h, d, a]}
                trace.append(row)
            # Dump the raw moneyline market of the first odds item to confirm the shape.
            if ob and isinstance(ob[0], dict):
                p0 = (ob[0].get("bookmakerOdds") or {}).get("pinnacle") or {}
                m0 = p0.get("markets") if isinstance(p0, dict) else None
                o["market_101_raw"] = (m0 or {}).get("101") if isinstance(m0, dict) else None
        elif ob is not None:
            o["body"] = ob
        o["trace"] = trace
        out["steps"]["odds_by_tournaments"] = o
    else:
        out["steps"]["odds_by_tournaments"] = {"skipped": "no DB-matched tournaments from /fixtures"}

    # 4) BETTABLE PROBE — does OddsPapi expose 1xBet (the price the user actually bets)?
    #    If yes we can pull BOTH sides free and stop depending on The Odds API's quota.
    bm = _call("/bookmakers", {})
    bml = bm.pop("body", None)
    cand = []
    if isinstance(bml, list):
        bm["count"] = len(bml)
        for b in bml:
            if not isinstance(b, dict):
                continue
            blob = " ".join(str(b.get(k, "")) for k in
                            ("slug", "key", "name", "bookmakerName", "title")).lower()
            if "1x" in blob or "onexbet" in blob:
                cand.append(b)
        bm["onexbet_candidates"] = cand
        bm["sample"] = bml[:3]
    elif bml is not None:
        bm["body"] = bml
    out["steps"]["bookmakers"] = bm

    slug = None
    for c in cand:
        slug = c.get("slug") or c.get("key") or c.get("bookmakerSlug")
        if slug:
            break
    out["onexbet_slug"] = slug

    # If a 1xBet slug exists, try its odds on our matched tournaments + trace 1X2.
    if slug and matched_tids:
        b = _call("/odds-by-tournaments", {"bookmaker": slug,
                  "tournamentIds": ",".join(str(t) for t in matched_tids[:5])})
        bb = b.pop("body", None)
        btrace = []
        if isinstance(bb, list):
            b["count"] = len(bb)
            for fx in bb[:12]:
                if not isinstance(fx, dict):
                    continue
                book = (fx.get("bookmakerOdds") or {}).get(slug) or {}
                h, d, a = _extract_1x2(book)
                fid = fx.get("fixtureId")
                btrace.append({"fixtureId": fid, "in_matched_fids": fid in matched_fids,
                               "has_book": bool(book), "extracted_1x2": [h, d, a]})
            # Dump the raw 1xbet block of the first fixture that has it, to see its shape
            # (why extraction fails: empty markets? different marketId/outcome labels?).
            for fx in bb:
                if not isinstance(fx, dict):
                    continue
                book = (fx.get("bookmakerOdds") or {}).get(slug)
                if isinstance(book, dict) and book:
                    mk = book.get("markets")
                    b["sample_bookmakerOdds_keys"] = list((fx.get("bookmakerOdds") or {}).keys())
                    b["sample_block_keys"] = list(book.keys())
                    b["sample_markets_keys"] = (list(mk.keys())[:15] if isinstance(mk, dict) else None)
                    b["sample_market_first"] = (next(iter(mk.values()))
                                                if isinstance(mk, dict) and mk else None)
                    break
        elif bb is not None:
            b["body"] = bb
        b["trace"] = btrace
        out["steps"]["onexbet_odds"] = b

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

    # 2) Map fixtureId -> our event_id for fixtures that match a DB event, and collect
    #    their tournamentIds. The /odds-by-tournaments response does NOT carry team
    #    names, so fixtureId is the reliable join key (names only exist on /fixtures).
    fid_to_event = {}
    wanted_tournaments = set()
    for fx in fixtures:
        if not isinstance(fx, dict):
            continue
        key = (_norm(fx.get("participant1Name")), _norm(fx.get("participant2Name")))
        eid = db_lookup.get(key)
        if eid:
            fid = fx.get("fixtureId")
            if fid:
                fid_to_event[fid] = eid
            if fx.get("tournamentId"):
                wanted_tournaments.add(fx["tournamentId"])
    if not fid_to_event:
        cb(f"OddsPapi: {len(fixtures)} fixtures fetched, none matched our DB events "
           f"(team-name mismatch?).")
        return 0
    cb(f"OddsPapi: {len(fid_to_event)} fixture(s) in {len(wanted_tournaments)} tournament(s) "
       f"match our events — fetching Pinnacle 1X2...")

    # 3) Pull Pinnacle odds for those tournaments (batched); match by fixtureId, extract 1X2.
    by_event = {}  # event_id -> (home, draw, away)
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
            eid = fid_to_event.get(fx.get("fixtureId"))
            if not eid:
                continue
            h, d, a = _pin_moneyline(fx)
            if h and a:
                by_event[eid] = (h, d, a)

    if not by_event:
        cb(f"OddsPapi: matched tournaments but extracted no Pinnacle 1X2 (credits {remaining}).")
        return 0

    # 4) Update pin_home/draw/away on the matched DB events (keyed by event_id).
    conn = get_connection()
    updated = 0
    for eid, (ph, pd, pa) in by_event.items():
        conn.execute(
            "UPDATE odds_events SET pin_home=?, pin_draw=?, pin_away=? WHERE event_id=?",
            (ph, pd, pa, eid),
        )
        updated += 1
    conn.commit()
    conn.close()
    cb(f"OddsPapi: updated {updated} events with Pinnacle 1X2. Credits left: {remaining}")
    return updated
