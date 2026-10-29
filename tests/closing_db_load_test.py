#!/usr/bin/env python3
"""The closing keep-alive must not open a DB connection when there is nothing to do.

WHY THIS EXISTS — a real outage, not a hypothetical:

The external cron calls /api/closing/run every 5 minutes to stop Render sleeping.
Every one of those calls used to query Postgres. Neon suspends compute after
about 5 minutes idle, so a query every 5 minutes meant the compute NEVER
suspended. That burned the entire monthly free compute allowance (110.95 CU-hrs)
in roughly two weeks, the project hit its limit, and every database-backed
endpoint — including My Bets — started returning HTTP 500.

The fix is an in-memory memory of when the next tracked bet kicks off, so the
keep-alive can answer "nothing to do" without paying for a connection. These
checks guard the properties that matter:

  1. a far-away kickoff is answered WITHOUT touching the database
  2. a kickoff inside the window still does the real work
  3. the cache never makes us MISS a kickoff (unknown state ⇒ hit the DB)

Run: RAILWAY_VOLUME_MOUNT_PATH=$(mktemp -d) ODDS_API_KEY=dummy \
     python3 tests/closing_db_load_test.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("RAILWAY_VOLUME_MOUNT_PATH", tempfile.mkdtemp())
os.environ.setdefault("ODDS_API_KEY", "dummy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collectors import database as db  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}\n      {detail}")
        failures += 1


def main():
    db.init_db()
    now = datetime.utcnow()

    conn = db.get_connection()
    conn.execute(
        "INSERT INTO odds_events (event_id, sport_key, sport_name, home_team,"
        " away_team, commence_time) VALUES (?,?,?,?,?,?)",
        ("e1", "soccer_epl", "EPL", "H", "A",
         (now + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    db.add_bet({"event_id": "e1", "market": "h2h", "selection": "H", "odds": 2.0,
                "stake": 10.0, "home_team": "H", "away_team": "A",
                "commence_time": (now + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")})

    import main as app_main
    from fastapi.testclient import TestClient
    client = TestClient(app_main.app)

    print("\nCLOSING KEEP-ALIVE MUST NOT HAMMER THE DATABASE\n")

    print("1. The DB tells us when the next kickoff is")
    ko = db.next_pending_kickoff()
    check("next_pending_kickoff() finds the pending bet",
          ko is not None and abs((ko - now).total_seconds() / 3600 - 5) < 0.2,
          f"got {ko}")

    print("\n2. First call has no memory yet, so it must consult the DB")
    r = client.get("/api/closing/run?within=45").json()
    check("first call touches the DB", r.get("db_touched") is True,
          f"db_touched={r.get('db_touched')}")

    print("\n3. Kickoff is 5h away — further calls answer from memory")
    skipped = [client.get("/api/closing/run?within=45").json() for _ in range(3)]
    check("all three answered WITHOUT a DB connection",
          all(x.get("db_touched") is False for x in skipped),
          f"db_touched values: {[x.get('db_touched') for x in skipped]}")
    check("and they still report ok (the cron must not see failures)",
          all(x.get("ok") is True for x in skipped), "a skip reported not-ok")
    check("the skip counter moved",
          app_main.closing_state.get("db_skips", 0) >= 3,
          f"db_skips={app_main.closing_state.get('db_skips')}")

    print("\n4. A kickoff INSIDE the window must still do the real work")
    app_main._next_ko_cache["next_ko"] = datetime.utcnow() + timedelta(minutes=20)
    app_main._next_ko_cache["checked_at"] = datetime.utcnow()
    r = client.get("/api/closing/run?within=45").json()
    check("a 20-min-away kickoff is NOT skipped", r.get("db_touched") is True,
          f"db_touched={r.get('db_touched')} — the capture would have been missed")

    print("\n5. The internal */15 scheduler tick obeys the same guard")
    import asyncio
    app_main._next_ko_cache["next_ko"] = datetime.utcnow() + timedelta(hours=5)
    app_main._next_ko_cache["checked_at"] = datetime.utcnow()
    before = app_main.closing_state.get("db_touches", 0)
    asyncio.get_event_loop().run_until_complete(app_main.run_closing_capture())
    check("the scheduled tick skipped the DB too",
          app_main.closing_state.get("db_touches", 0) == before,
          f"db_touches went {before} -> {app_main.closing_state.get('db_touches')}")

    print("\n6. The cache must never cause a miss")
    app_main._next_ko_cache["checked_at"] = None       # unknown state
    app_main._next_ko_cache["next_ko"] = None
    check("an unknown cache falls back to the DB",
          app_main._closing_skip_reason(45) is None,
          "skipped on an unknown cache — a kickoff could be missed")

    # A stale cache must not be trusted indefinitely, or a bet placed after the
    # last check would never be seen.
    app_main._next_ko_cache["checked_at"] = datetime.utcnow() - timedelta(hours=3)
    app_main._next_ko_cache["next_ko"] = datetime.utcnow() + timedelta(hours=10)
    check("a 3h-old cache is re-checked rather than trusted",
          app_main._closing_skip_reason(45) is None,
          "trusted a 3h-old answer — a newly placed bet would be invisible")

    # ...but a fresh one for a distant kickoff is fine to trust.
    app_main._next_ko_cache["checked_at"] = datetime.utcnow() - timedelta(minutes=5)
    app_main._next_ko_cache["next_ko"] = datetime.utcnow() + timedelta(hours=10)
    check("a 5-min-old answer for a 10h-away kickoff is trusted",
          app_main._closing_skip_reason(45) is not None,
          "went to the DB when memory already had the answer")

    print(f"\n{'✅ ALL CHECKS PASSED' if failures == 0 else f'❌ {failures} CHECK(S) FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
