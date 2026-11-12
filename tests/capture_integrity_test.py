#!/usr/bin/env python3
"""The closing capture must not have a way to miss a bet quietly.

Every check here pins down a concrete hole found in the 2026-11-12 audit of the
capture chain. None of them had bitten yet in a way anyone noticed — which is
the problem: each one fails silently, and a silently missed close is exactly
how 41 of the first 83 CLVs turned out to be unusable.

  1. A bet logged while the in-memory cache said "nothing pending" stayed
     invisible to the capture for up to an hour.
  2. The stale-event purge (run on every restart, so on every deploy) deleted
     the event row of a bet logged more than 24h before kickoff, and the capture
     could only find a bet through that row.
  3. A kickoff the API reschedules (tennis, all day) was never copied onto the
     bet, and a close, once written, was never revisited.
  4. A three-way line that arrives without its draw was devigged as a two-way
     market — a fabricated fair price, and a CLV of +30% for a bet with none.
  5. A kickoff earlier TODAY read as "in the future" wherever the two stored
     timestamp formats were compared as strings.
  6. A double click logged the same bet twice.
  7. A late or missing close told no one.
  8. Manual refreshes could spend the credits the capture needs.

Run: RAILWAY_VOLUME_MOUNT_PATH=$(mktemp -d) ODDS_API_KEY=dummy \
     python3 tests/capture_integrity_test.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("RAILWAY_VOLUME_MOUNT_PATH", tempfile.mkdtemp())
os.environ.setdefault("ODDS_API_KEY", "dummy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collectors import database as db  # noqa: E402
from collectors import odds  # noqa: E402

failures = 0
fetched = []


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}\n      {detail}")
        failures += 1


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sql(q, params=()):
    conn = db.get_connection()
    cur = conn.execute(q, params)
    rows = cur.fetchall() if q.lstrip().upper().startswith("SELECT") else None
    conn.commit()
    conn.close()
    return rows


def add_event(eid, key, kickoff, updated_at=None):
    sql("INSERT OR REPLACE INTO odds_events (event_id, sport_key, sport_name, home_team, "
        "away_team, commence_time, updated_at) VALUES (?,?,?,?,?,?,?)",
        (eid, key, key, f"H-{eid}", f"A-{eid}", iso(kickoff),
         (updated_at or datetime.utcnow()).strftime("%Y-%m-%d %H:%M")))


def add_snapshot(eid, at, home, draw, away):
    sql("INSERT INTO odds_history (event_id, captured_at, pin_home, pin_draw, pin_away) "
        "VALUES (?,?,?,?,?)", (eid, at.strftime("%Y-%m-%d %H:%M:%S"), home, draw, away))


def bet_row(bet_id):
    return dict(sql("SELECT * FROM bets WHERE id=?", (bet_id,))[0])


def bet_payload(eid, kickoff, **extra):
    p = {"event_id": eid, "market": "h2h", "selection": f"H-{eid}", "odds": 2.10,
         "stake": 10.0, "home_team": f"H-{eid}", "away_team": f"A-{eid}",
         "bookmaker": "1xBet", "edge_pct": 3.0, "commence_time": iso(kickoff)}
    p.update(extra)
    return p


def main():
    db.init_db()
    now = datetime.utcnow()
    odds._LAST_REMAINING["v"] = 400
    odds.fetch_odds = lambda sk, markets=None, hours_ahead=None: (fetched.append(sk), (None, 399))[1]

    import main as app_main
    from fastapi.testclient import TestClient
    client = TestClient(app_main.app)

    print("\nTHE CLOSING CAPTURE MUST NOT MISS A BET QUIETLY\n")

    # ── 1 ────────────────────────────────────────────────────────────────────
    print("1. A bet logged while the cache says 'nothing pending' is seen at once")
    app_main._next_ko_cache.update({"checked_at": datetime.utcnow(), "next_ko": None,
                                    "db_error_at": None})
    check("(setup) the cache currently answers 'nothing to do' without the DB",
          app_main._closing_skip_reason(45) is not None, "cache did not skip")
    add_event("soon", "soccer_epl", now + timedelta(minutes=30))
    r = client.post("/api/bets", json=bet_payload("soon", now + timedelta(minutes=30))).json()
    check("the bet is saved", r.get("ok") is True and not r.get("duplicate"), str(r))
    soon_id = r.get("id")
    check("the API answers with the real id of the new bet", isinstance(soon_id, int) and soon_id > 0,
          f"id={soon_id!r}")
    check("logging it drops the cached 'nothing pending' answer",
          app_main._closing_skip_reason(45) is None,
          "still skipping — a game 30 min away would get no closing line")
    fetched.clear()
    odds._LAST_IMMINENT_FETCH.clear()
    run = client.get("/api/closing/run?within=45").json()
    check("the very next capture call goes to the DB and fetches that league",
          run.get("db_touched") is True and fetched == ["soccer_epl"],
          f"db_touched={run.get('db_touched')} fetched={fetched}")

    for label, call in (("settling", lambda: client.post(f"/api/bets/{soon_id}/settle", json={"result": "won"})),
                        ("deleting", lambda: client.delete(f"/api/bets/{soon_id}"))):
        app_main._next_ko_cache.update({"checked_at": datetime.utcnow(),
                                        "next_ko": datetime.utcnow() + timedelta(hours=9)})
        call()
        check(f"{label} a bet also drops the cache (the pending set changed)",
              app_main._next_ko_cache["checked_at"] is None, "cache kept a stale answer")

    # ── 2 ────────────────────────────────────────────────────────────────────
    print("\n2. A bet stays capturable when its event row is purged")
    old = now - timedelta(days=3)
    add_event("fri", "soccer_spain_la_liga", now + timedelta(minutes=20), updated_at=old)
    add_event("junk", "soccer_italy_serie_a", now + timedelta(minutes=20), updated_at=old)
    fri_id = db.add_bet(bet_payload("fri", now + timedelta(minutes=20)))
    check("the league is copied onto the bet when it is logged",
          bet_row(fri_id).get("sport_key") == "soccer_spain_la_liga",
          f"sport_key={bet_row(fri_id).get('sport_key')!r}")
    res = db.purge_out_of_scope_and_stale()
    left = {r["event_id"] for r in sql("SELECT event_id FROM odds_events")}
    check("the stale purge keeps the event that has a pending bet",
          "fri" in left, f"purged it — events left: {sorted(left)}")
    check("...and still removes a stale event nobody bet on",
          "junk" not in left and res["stale"] >= 1, f"events left: {sorted(left)}")
    sql("DELETE FROM odds_events WHERE event_id='fri'")
    check("with the event row GONE the capture still targets the bet's league",
          "soccer_spain_la_liga" in db.closing_targets(45),
          f"targets={db.closing_targets(45)} — this bet would never get a close")
    mins = db.closing_targets(45).get("soccer_spain_la_liga")
    check("...and knows how far the kickoff is (drives the fetch pacing)",
          mins is not None and 18 <= mins <= 20.5, f"minutes={mins}")

    # ── 3 ────────────────────────────────────────────────────────────────────
    print("\n3. A rescheduled kickoff reaches the bet, and the close follows it")
    ko_old = now - timedelta(minutes=30)
    add_event("ten", "tennis_atp_paris_masters", ko_old)
    ten_id = db.add_bet(bet_payload("ten", ko_old))
    add_snapshot("ten", ko_old - timedelta(minutes=6), 1.80, None, 2.10)
    db.capture_pinnacle_close_for_started_events()
    first_close = bet_row(ten_id)["pin_close_captured_at"]
    check("(setup) a close is captured at the original start time",
          first_close is not None, "no close captured")

    ko_new = now + timedelta(minutes=40)            # the API now says: delayed
    ev = {"id": "ten", "home_team": "H-ten", "away_team": "A-ten", "commence_time": iso(ko_new),
          "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
              {"name": "H-ten", "price": 1.70}, {"name": "A-ten", "price": 2.25}]}]}]}
    odds.parse_and_store([ev], "tennis_atp_paris_masters", "ATP Paris Masters")
    check("storing the event copies the new kickoff onto the pending bet",
          bet_row(ten_id)["commence_time"] == iso(ko_new),
          f"bet still says {bet_row(ten_id)['commence_time']}, API says {iso(ko_new)}")
    check("...so the delayed match is back inside the capture window",
          "tennis_atp_paris_masters" in db.closing_targets(45), f"targets={db.closing_targets(45)}")

    # time passes: the match finally starts, 4 minutes after a fresh snapshot
    sql("UPDATE bets SET commence_time=? WHERE id=?", (iso(now - timedelta(minutes=2)), ten_id))
    add_snapshot("ten", now - timedelta(minutes=6), 1.66, None, 2.32)
    db.capture_pinnacle_close_for_started_events()
    b = bet_row(ten_id)
    check("the later pre-match snapshot REPLACES the early close",
          b["pin_close_odds"] == 1.66 and b["pin_close_captured_at"] != first_close,
          f"close still {b['pin_close_odds']} from {b['pin_close_captured_at']}")

    settled_ko = now + timedelta(hours=5)
    add_event("done", "soccer_epl", settled_ko)
    done_id = db.add_bet(bet_payload("done", settled_ko))
    sql("UPDATE bets SET status='settled' WHERE id=?", (done_id,))
    ev2 = dict(ev, id="done", home_team="H-done", away_team="A-done",
               commence_time=iso(settled_ko + timedelta(hours=3)))
    odds.parse_and_store([ev2], "soccer_epl", "EPL")
    check("a SETTLED bet's kickoff is never rewritten",
          bet_row(done_id)["commence_time"] == iso(settled_ko),
          f"changed to {bet_row(done_id)['commence_time']}")

    # ── 4 ────────────────────────────────────────────────────────────────────
    print("\n4. An incomplete market is never devigged into a 'fair' price")
    check("two outcomes of a three-way line are refused (implied total 76%)",
          db.remove_vig_power(2.0, None, 3.8) is None,
          f"got {db.remove_vig_power(2.0, None, 3.8)}")
    two_way = db.remove_vig_power(1.85, None, 2.05)
    check("a genuine two-way market still devigs normally",
          two_way is not None and abs(two_way["home"] + two_way["away"] - 100) < 0.05, str(two_way))

    ko4 = now - timedelta(minutes=10)
    add_event("fb", "soccer_germany_bundesliga", ko4)
    fb_id = db.add_bet(bet_payload("fb", ko4))
    add_snapshot("fb", ko4 - timedelta(minutes=20), 2.00, 3.50, 3.80)   # complete
    add_snapshot("fb", ko4 - timedelta(minutes=5), 2.05, None, 3.70)    # draw missing
    db.capture_pinnacle_close_for_started_events()
    b = bet_row(fb_id)
    ratio = (b["pin_close_fair_odds"] / b["pin_close_odds"]) if b["pin_close_fair_odds"] else None
    check("the close comes from the last snapshot that can be devigged (the complete one)",
          b["pin_close_odds"] == 2.00 and b["pin_close_fair_odds"] is not None,
          f"raw={b['pin_close_odds']} fair={b['pin_close_fair_odds']}")
    check("...and the fair odd sits just above the raw odd (a real 3-way devig)",
          ratio is not None and 1.0 < ratio < 1.08, f"fair/raw = {ratio}")
    clv = (2.10 / b["pin_close_fair_odds"] - 1) * 100 if b["pin_close_fair_odds"] else None
    check("...so the CLV is plausible, not the +30% a two-leg devig would invent",
          clv is not None and abs(clv) < 8, f"CLV = {clv}")

    # A lopsided match is the case the "implied total" guard cannot see: with a
    # 1.07 favourite, home+away alone already add up to ~97%, which looks like a
    # perfectly good two-way market. Only knowing that this event HAS a draw
    # (an earlier snapshot shows one) exposes the snapshot as incomplete.
    add_event("lop", "soccer_epl", ko4)
    lop_id = db.add_bet(bet_payload("lop", ko4, odds=1.10))
    add_snapshot("lop", ko4 - timedelta(minutes=20), 1.08, 12.0, 26.0)   # complete
    add_snapshot("lop", ko4 - timedelta(minutes=5), 1.07, None, 28.0)    # draw missing, total 97%
    db.capture_pinnacle_close_for_started_events()
    b = bet_row(lop_id)
    check("a heavy favourite's snapshot without the draw is not devigged as two-way",
          b["pin_close_fair_odds"] is not None and b["pin_close_fair_odds"] > b["pin_close_odds"],
          f"raw={b['pin_close_odds']} fair={b['pin_close_fair_odds']} — a fair odd BELOW the raw "
          f"odd means the vig was 'removed' from an incomplete line")

    # ── 5 ────────────────────────────────────────────────────────────────────
    print("\n5. A kickoff earlier today is in the past, whatever the string says")
    sql("UPDATE bets SET status='settled' WHERE status='pending'")
    earlier = now - timedelta(hours=2)
    add_event("past", "soccer_epl", earlier)
    past_id = db.add_bet(bet_payload("past", earlier))
    check("a pending bet whose game started 2h ago is not a 'next kickoff'",
          db.next_pending_kickoff() is None, f"got {db.next_pending_kickoff()}")
    check("...and is not targeted, even with a 10-hour window",
          db.closing_targets(600) == {}, f"targets={db.closing_targets(600)}")
    sql("UPDATE bets SET status='settled' WHERE id=?", (past_id,))

    # ── 6 ────────────────────────────────────────────────────────────────────
    print("\n6. A double click does not log the bet twice")
    add_event("dbl", "soccer_epl", now + timedelta(hours=3))
    payload = bet_payload("dbl", now + timedelta(hours=3))
    a = client.post("/api/bets", json=payload).json()
    b2 = client.post("/api/bets", json=payload).json()
    n_rows = len(sql("SELECT id FROM bets WHERE event_id='dbl'"))
    check("the repeat is answered with the bet that already exists",
          b2.get("ok") is True and b2.get("duplicate") is True and b2.get("id") == a.get("id"),
          f"first={a} second={b2}")
    check("only ONE row was written", n_rows == 1, f"{n_rows} rows for the same bet")
    c = client.post("/api/bets", json=dict(payload, stake=12.0)).json()
    check("a different stake is a different bet (not swallowed as a duplicate)",
          c.get("ok") is True and not c.get("duplicate"), str(c))
    sql("UPDATE bets SET placed_at=? WHERE event_id='dbl'",
        ((now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),))
    d = client.post("/api/bets", json=payload).json()
    check("the same bet ten minutes later is allowed (deliberate second bet)",
          d.get("ok") is True and not d.get("duplicate"), str(d))

    # ── 7 ────────────────────────────────────────────────────────────────────
    print("\n7. A late close is reported instead of passing unnoticed")
    alerts = []
    real_alert = db._alert_late_close
    db._alert_late_close = lambda bet, lead: alerts.append((bet["id"], lead))
    db._close_alerted.clear()
    try:
        ko7 = now - timedelta(minutes=10)
        add_event("late", "soccer_epl", ko7)
        late_id = db.add_bet(bet_payload("late", ko7))
        add_snapshot("late", ko7 - timedelta(minutes=200), 2.0, 3.5, 3.8)
        add_event("ok", "soccer_epl", ko7)
        ok_id = db.add_bet(bet_payload("ok", ko7))
        add_snapshot("ok", ko7 - timedelta(minutes=5), 2.0, 3.5, 3.8)
        db.capture_pinnacle_close_for_started_events()
        ids = [a_[0] for a_ in alerts]
        check("a close 200 min before kickoff raises an alert",
              late_id in ids, f"alerted ids={ids}")
        check("a close 5 min before kickoff does not",
              ok_id not in ids, f"alerted ids={ids}")
        db.capture_pinnacle_close_for_started_events()
        check("the alert is sent once, not on every capture call",
              [a_[0] for a_ in alerts].count(late_id) == 1, f"alerts={alerts}")
        check("the report lists the late close for the diagnostics endpoint",
              any(x["id"] == late_id for x in db.LAST_CAPTURE_REPORT["late"]),
              str(db.LAST_CAPTURE_REPORT))
    finally:
        db._alert_late_close = real_alert

    # ── 8 ────────────────────────────────────────────────────────────────────
    print("\n8. Manual refreshes cannot spend the capture's credits")
    real_sports, real_events = odds.get_active_sports, odds.fetch_events
    odds.get_active_sports = lambda: {"soccer_epl"}
    odds.fetch_events = lambda sk, hours_ahead=None: ([], 0)
    try:
        odds._LAST_REMAINING["v"] = odds.MANUAL_REFRESH_MIN_QUOTA - 1
        try:
            odds.collect_odds(status_callback=lambda m: None)
            check("below the reserve a manual refresh is refused", False, "it ran")
        except odds.QuotaReserved:
            check("below the reserve a manual refresh is refused", True)
        try:
            odds.collect_odds(status_callback=lambda m: None, force=True)
            check("...unless the user explicitly forces it", True)
        except odds.QuotaReserved:
            check("...unless the user explicitly forces it", False, "force was ignored")
        fetched.clear()
        odds._LAST_IMMINENT_FETCH.clear()
        add_event("res", "soccer_epl", datetime.utcnow() + timedelta(minutes=8))
        db.add_bet(bet_payload("res", datetime.utcnow() + timedelta(minutes=8)))
        odds.refresh_imminent_odds(status_callback=lambda m: None, within_minutes=45)
        check("...while the closing capture keeps running on that same balance",
              fetched == ["soccer_epl"], f"fetched={fetched}")

        app_main.collection_status["running"] = False
        r1 = client.post("/api/odds/refresh").json()
        check("the refresh endpoint asks for confirmation instead of starting",
              r1.get("needs_confirm") is True and r1.get("reason") == "reserve", str(r1))
        odds._LAST_REMAINING["v"] = 400
        app_main._last_odds_refresh["done_at"] = datetime.utcnow() - timedelta(minutes=2)
        r2 = client.post("/api/odds/refresh").json()
        check("a refresh 2 minutes after the last one also asks first",
              r2.get("needs_confirm") is True and r2.get("reason") == "recent", str(r2))
    finally:
        odds.get_active_sports, odds.fetch_events = real_sports, real_events
        app_main._last_odds_refresh["done_at"] = None

    from collectors import odds_football
    called = []
    real_fix, real_enrich = odds_football.collect_openfootball_fixtures, odds_football.add_odds_from_theoddsapi
    odds_football.collect_openfootball_fixtures = lambda cb: 0
    odds_football.add_odds_from_theoddsapi = lambda cb: called.append(1)
    try:
        odds_football.collect_odds_apifootball(status_callback=lambda m: None)
        check("no fixtures to enrich => the 9 extra paid /odds calls are not made",
              called == [], "the hidden enrichment still ran (~6 credits per refresh)")
        odds_football.collect_openfootball_fixtures = lambda cb: 3
        odds_football.collect_odds_apifootball(status_callback=lambda m: None)
        check("...but it still runs when there ARE fixtures to enrich",
              called == [1], f"called={called}")
    finally:
        odds_football.collect_openfootball_fixtures = real_fix
        odds_football.add_odds_from_theoddsapi = real_enrich

    # ── 9 ────────────────────────────────────────────────────────────────────
    print("\n9. Odd inputs do not take the stats down")
    blank_id = db.add_bet({"market": "h2h", "selection": "X", "odds": 2.0, "stake": 1.0,
                           "commence_time": ""})
    check("an empty kickoff is stored as NULL, not as ''",
          bet_row(blank_id)["commence_time"] is None, repr(bet_row(blank_id)["commence_time"]))
    sql("UPDATE bets SET commence_time='not-a-date', pin_close_fair_odds=1.9, status='pending' "
        "WHERE id=?", (blank_id,))
    try:
        s = db.get_bet_stats()
        check("get_bet_stats survives a malformed kickoff in the table",
              isinstance(s.get("clv_sample"), int), str(s))
    except Exception as e:                            # noqa: BLE001
        check("get_bet_stats survives a malformed kickoff in the table", False, repr(e))
    try:
        rows = db.get_bets(500)
        check("get_bets survives it too", len(rows) > 0, "no rows")
    except Exception as e:                            # noqa: BLE001
        check("get_bets survives it too", False, repr(e))

    print(f"\n{'✅ ALL CHECKS PASSED' if failures == 0 else f'❌ {failures} CHECK(S) FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
