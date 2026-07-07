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

# Last known free-tier quota (set by fetch_odds from x-requests-remaining), so the
# frequent near-kickoff refresh can back off before exhausting the monthly budget.
_LAST_REMAINING = {"v": None}
IMMINENT_MIN_QUOTA = int(os.environ.get("IMMINENT_MIN_QUOTA", "50"))  # HARD BRAKE: skip ALL non-essential refreshes below this.

# SCOPE (free-tier conservation): auto-collection — full sweep + imminent + closing —
# is limited to FOOTBALL and TENNIS only. All other sports (basketball, cricket, MMA,
# NFL, baseball, boxing, …) are dropped until the paid plan is active.
#
# Near-kickoff config PER SPORT (env-tunable; retune without a deploy):
#   window_h     = hours before kickoff we start re-fetching the line.
#   throttle_min = min minutes between fetches of the SAME sport key. This is what caps
#                  an all-day tournament: a tennis Slam has matches all day, so without
#                  a throttle the */15 closing job would re-pull it dozens of times/day.
IMMINENT_CFG = {
    "football": {
        "window_h":     int(os.environ.get("IMMINENT_WINDOW_HOURS_FOOTBALL", "6")),
        "throttle_min": int(os.environ.get("IMMINENT_DEDUP_FOOTBALL", "45")),
    },
    "tennis": {
        "window_h":     int(os.environ.get("IMMINENT_WINDOW_HOURS_TENNIS", "3")),
        "throttle_min": int(os.environ.get("IMMINENT_DEDUP_TENNIS", "90")),   # 90min so the free 500 covers the WC+Wimbledon peak
    },
}
_LAST_IMMINENT_FETCH = {}        # sport_key -> datetime of last imminent/closing fetch


def _sport_class(key):
    """football | tennis | None  (None = out of scope, not auto-collected)."""
    if not key:
        return None
    if key.startswith("soccer"):
        return "football"
    if key.startswith("tennis"):
        return "tennis"
    return None


def get_active_sports():
    try:
        r = requests.get(f"{BASE}/sports", params={"apiKey": API_KEY}, timeout=15)
        if r.status_code == 200:
            return {s["key"] for s in r.json() if s.get("active")}
    except Exception:
        pass
    return set()


def fetch_odds(sport_key, markets=None):
    # QUOTA: The Odds API bills 1 credit per (region × market). We use 1 region ('eu'
    # — 1xBet/onexbet, Pinnacle and every book in BOOKMAKERS live there; 'us' only
    # added US-only books) and **h2h ONLY** by default = 1 credit/call. The value
    # engine is 1X2-centric (edge/CLV/Kelly all on the match-result line); O/U and
    # Asian Handicap are secondary and not worth 3× the quota on a 500/mo free tier.
    # Callers that genuinely need more (e.g. diagnostics) pass `markets` explicitly.
    if markets is None:
        markets = "h2h"
    params = {
        "apiKey": API_KEY,
        "regions": "eu",
        "markets": markets,
        "bookmakers": BOOKMAKERS,
        "oddsFormat": "decimal",
    }
    try:
        r = requests.get(f"{BASE}/sports/{sport_key}/odds", params=params, timeout=20)
        remaining = r.headers.get("x-requests-remaining", "?")
        try:
            _LAST_REMAINING["v"] = int(remaining)
        except (ValueError, TypeError):
            pass
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
                    if mkey in ("totals", "spreads"):
                        # totals: "Over|2.5"; spreads (Asian handicap): "Home|-1.5"
                        outcomes[f"{name}|{o.get('point')}"] = price
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

        # --- ASIAN HANDICAP (spreads) ---
        # Pinnacle's main line is the reference; best price is taken among books
        # offering the SAME line (so the two sides are comparable).
        def get_ah(book_key):
            mkt = book_odds.get(book_key, {}).get("spreads", {})
            hp = ap = None
            line = None
            for k, price in mkt.items():
                if "|" not in k:
                    continue
                nm, pt = k.rsplit("|", 1)
                try:
                    pt = float(pt)
                except (ValueError, TypeError):
                    continue
                if nm == home:
                    hp, line = price, pt
                elif nm == away:
                    ap = price
            return (line, hp, ap) if (hp and ap and line is not None) else (None, None, None)

        ah_line, pin_ah_home, pin_ah_away = get_ah("pinnacle")
        best_ah_home = best_ah_away = None
        x1_ah_home = x1_ah_away = None
        if ah_line is not None:
            for bm in book_odds:
                l, hp, ap = get_ah(bm)
                if l == ah_line:  # same home line ⇒ comparable
                    if hp and (best_ah_home is None or hp > best_ah_home):
                        best_ah_home = hp
                    if ap and (best_ah_away is None or ap > best_ah_away):
                        best_ah_away = ap
            best_ah_home = best_ah_home or pin_ah_home
            best_ah_away = best_ah_away or pin_ah_away
            # The user's 1xBet price — only if it offers Pinnacle's exact line.
            xl, xh, xa = get_ah("onexbet")
            if xl == ah_line:
                x1_ah_home, x1_ah_away = xh, xa

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
                    ah_line, pin_ah_home, pin_ah_away, best_ah_home, best_ah_away, x1_ah_home, x1_ah_away,
                    updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                event_id, sport_key, sport_name, home, away, commence,
                pin_home, pin_draw, pin_away,
                best_home, best_draw, best_away,
                x1_home, x1_draw, x1_away,
                b365_home, b365_draw, b365_away,
                pin_over25, pin_under25, x1_over25, x1_under25, best_over25, best_under25,
                pin_btts_yes, pin_btts_no, x1_btts_yes, x1_btts_no, best_btts_yes, best_btts_no,
                ah_line, pin_ah_home, pin_ah_away, best_ah_home, best_ah_away, x1_ah_home, x1_ah_away,
                datetime.utcnow().strftime("%Y-%m-%d %H:%M")
            ))
            # Also save snapshot to history for line movement tracking.
            # PRE-match snapshots only: the API also returns in-play events, and a
            # live price stored here would be mistaken for the closing line by
            # capture_pinnacle_close_for_started_events().
            try:
                _ko = datetime.strptime(str(commence).replace("T", " ").rstrip("Z")[:16], "%Y-%m-%d %H:%M")
                _started = _ko <= datetime.utcnow()
            except (ValueError, TypeError):
                _started = False
            if not _started:
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
                    event_id, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
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


def refresh_imminent_odds(status_callback=None, within_minutes=None):
    """Near-kickoff refresh for FOOTBALL + TENNIS only.

    Imminent mode (within_minutes=None, the */2h job): re-fetch each in-scope sport
    that has a game inside its OWN window (football 6h, tennis 3h), throttled per
    sport (football 45min, tennis 60min) so an all-day tennis Slam can't drain quota.

    Closing mode (within_minutes set, the */15 closing-capture job): re-fetch
    in-scope sports with a game in the next `within_minutes`. FOOTBALL is fetched
    UNTHROTTLED here (games are clustered + cheap → a true ~15-min close); TENNIS keeps
    its throttle (all-day → unthrottled would be dozens of fetches/day). Each fetch
    writes an odds_history snapshot read by capture_pinnacle_close_for_started_events()."""
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    if not API_KEY:
        return 0
    if _LAST_REMAINING["v"] is not None and _LAST_REMAINING["v"] < IMMINENT_MIN_QUOTA:
        cb(f"Imminent refresh: skipped (quota low: {_LAST_REMAINING['v']} left).")
        return 0

    def keys_in_window(interval):
        # Literal interval (int-derived, no injection) so the SQL-translation layer can
        # rewrite datetime('now','+N unit') -> NOW() + INTERVAL on Postgres.
        try:
            conn = get_connection()
            rows = conn.execute(
                "SELECT DISTINCT sport_key FROM odds_events "
                "WHERE commence_time > datetime('now') "
                f"AND commence_time < datetime('now', '{interval}')"
            ).fetchall()
            conn.close()
            return [r["sport_key"] for r in rows if r["sport_key"]]
        except Exception as e:
            cb(f"Imminent refresh DB error: {e}")
            return []

    # Build the in-scope (football/tennis) target list, per-sport window.
    targets = []
    if within_minutes is not None:
        window_desc = f"{int(within_minutes)}min"
        for k in keys_in_window(f"+{int(within_minutes)} minutes"):
            if _sport_class(k):
                targets.append(k)
    else:
        window_desc = "F6h/T3h"
        seen = set()
        for cls, cfg in IMMINENT_CFG.items():
            for k in keys_in_window(f"+{int(cfg['window_h'])} hours"):
                if _sport_class(k) == cls and k not in seen:
                    seen.add(k)
                    targets.append(k)

    if not targets:
        cb(f"Imminent refresh: no football/tennis match starting in <{window_desc}.")
        return 0

    total, remaining, fetched, skipped = 0, "?", 0, 0
    now = datetime.utcnow()
    for sk in targets:
        cls = _sport_class(sk)
        # Closing job + football → no throttle (cheap, true 15-min close). Otherwise
        # the per-sport throttle (esp. tennis) caps an all-day tournament.
        throttle = 0 if (within_minutes is not None and cls == "football") else IMMINENT_CFG[cls]["throttle_min"]
        last = _LAST_IMMINENT_FETCH.get(sk)
        if throttle and last and (now - last).total_seconds() < throttle * 60:
            skipped += 1
            continue
        events, remaining = fetch_odds(sk)
        _LAST_IMMINENT_FETCH[sk] = now
        fetched += 1
        if events:
            total += parse_and_store(events, sk, SPORT_GROUPS.get(sk) or _pretty_sport_name(sk))
        if _LAST_REMAINING["v"] is not None and _LAST_REMAINING["v"] < IMMINENT_MIN_QUOTA:
            cb(f"Imminent refresh: stopping early (quota low: {_LAST_REMAINING['v']} left).")
            break
    cb(f"Imminent refresh: {total} events, {fetched} fetched / {skipped} throttled "
       f"(of {len(targets)} in-scope sport(s) <{window_desc}). Credits left: {remaining}")
    return total


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


def diagnose_spreads(sport_key="soccer_fifa_world_cup"):
    """Does The Odds API return Asian Handicap (spreads) for these games? Confirms
    whether World Cup handicap value bets can populate. Open /api/diag/spreads."""
    out = {"sport_key": sport_key, "events": 0, "with_spreads": 0,
           "with_pinnacle_spreads": 0, "remaining": None, "samples": []}
    if not API_KEY:
        out["error"] = "ODDS_API_KEY not set"
        return out
    events, remaining = fetch_odds(sport_key, markets="h2h,totals,spreads")
    out["remaining"] = remaining
    if not events:
        out["note"] = "no events returned for this sport_key right now"
        return out
    out["events"] = len(events)
    for ev in events:
        has_spreads = False
        pin_spread = None
        for bm in ev.get("bookmakers", []):
            for mk in bm.get("markets", []):
                if mk.get("key") == "spreads":
                    has_spreads = True
                    if bm.get("key") == "pinnacle":
                        pin_spread = [{"name": o.get("name"), "point": o.get("point"),
                                       "price": o.get("price")} for o in mk.get("outcomes", [])]
        if has_spreads:
            out["with_spreads"] += 1
            if pin_spread:
                out["with_pinnacle_spreads"] += 1
            if len(out["samples"]) < 5:
                out["samples"].append({
                    "match": f"{ev.get('home_team')} vs {ev.get('away_team')}",
                    "pinnacle_handicap": pin_spread,
                })
    return out


def probe_all_books(sport_key=None):
    """ONE raw fetch (1 credit) of an in-scope sport with ALL EU bookmakers (no book
    filter), to rank which book most often has the BEST h2h price and how often each
    beats 1xBet (onexbet). Diagnostic only — used to decide a 2nd bookmaker."""
    if not API_KEY:
        return {"error": "no ODDS_API_KEY"}
    if not sport_key:
        try:
            conn = get_connection()
            row = conn.execute(
                "SELECT sport_key, COUNT(*) AS c FROM odds_events "
                "WHERE commence_time > datetime('now') "
                "AND (sport_key LIKE 'soccer%' OR sport_key LIKE 'tennis%') "
                "GROUP BY sport_key ORDER BY c DESC"
            ).fetchone()
            conn.close()
            sport_key = row["sport_key"] if row else "soccer_fifa_world_cup"
        except Exception:
            sport_key = "soccer_fifa_world_cup"
    # No 'bookmakers' param -> The Odds API returns ALL EU books (still 1 credit).
    params = {"apiKey": API_KEY, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"}
    try:
        r = requests.get(f"{BASE}/sports/{sport_key}/odds", params=params, timeout=25)
        remaining = r.headers.get("x-requests-remaining", "?")
        try:
            _LAST_REMAINING["v"] = int(remaining)
        except (ValueError, TypeError):
            pass
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.text[:160]}", "sport": sport_key}
        events = r.json()
    except Exception as e:
        return {"error": repr(e), "sport": sport_key}

    best, beats, priced = {}, {}, {}
    outcomes, x1_outcomes = 0, 0
    for e in events:
        book_out = {}
        for b in e.get("bookmakers", []):
            for m in b.get("markets", []):
                if m.get("key") != "h2h":
                    continue
                for o in m.get("outcomes", []):
                    book_out.setdefault(b["key"], {})[o.get("name")] = o.get("price")
        names = set()
        for bo in book_out.values():
            names.update(bo.keys())
        for nm in names:
            prices = {bk: bo[nm] for bk, bo in book_out.items() if bo.get(nm)}
            if not prices:
                continue
            outcomes += 1
            mx = max(prices.values())
            for bk, p in prices.items():
                priced[bk] = priced.get(bk, 0) + 1
                if p >= mx - 1e-9:
                    best[bk] = best.get(bk, 0) + 1
            if "onexbet" in prices:
                x1_outcomes += 1
                x1 = prices["onexbet"]
                for bk, p in prices.items():
                    if bk != "onexbet" and p > x1 + 1e-9:
                        beats[bk] = beats.get(bk, 0) + 1

    def pct(n, d):
        return round(n / d * 100, 1) if d else 0
    ranking = sorted(
        ({"book": bk, "best": best.get(bk, 0), "best_pct": pct(best.get(bk, 0), outcomes),
          "beats_1xbet": beats.get(bk, 0), "beats_1xbet_pct": pct(beats.get(bk, 0), x1_outcomes),
          "priced": priced.get(bk, 0)} for bk in priced),
        key=lambda x: (-x["best"], -x["beats_1xbet"]))
    return {"sport": sport_key, "events": len(events), "outcomes": outcomes,
            "x1_outcomes": x1_outcomes, "credits_remaining": remaining, "ranking": ranking}


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

    # SCOPE: free-tier conservation — keep ONLY football + tennis (drop baseball,
    # MMA, boxing, basketball, cricket, NFL, … from the daily sweep too).
    to_fetch = {k: v for k, v in to_fetch.items() if _sport_class(k)}

    cb(f"Fetching odds for {len(to_fetch)} football/tennis sports...")

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
