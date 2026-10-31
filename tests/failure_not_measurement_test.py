#!/usr/bin/env python3
"""A failure must never read as a measurement.

THE RECURRING DEFECT IN THIS PROJECT

Three times now the same mistake has cost us real information:

  1. Stale closing lines were averaged into "+2.8% avg CLV, 97.4% positive" —
     a number that merely restated the entry edge, presented as independent
     confirmation of it.
  2. The line-drift bands were judged by their MEDIAN, so a reference that
     moved more than the whole edge 40% of the time read as "stable".
  3. sport_keys_with_pending_bets() caught every exception and returned [].
     An unreachable database therefore produced the message "no TRACKED bet
     kicking off — 0 credits spent" and a tick that looked perfectly healthy,
     while the close it was supposed to capture was silently missed. Observed
     in production on 2026-10-31, in the audit log of a real run.

The shape is always the same: a code path that cannot tell the difference
between "the answer is nothing" and "I could not find out". These checks pin
that difference down for the two queries the closing capture depends on.

Run: RAILWAY_VOLUME_MOUNT_PATH=$(mktemp -d) python3 tests/failure_not_measurement_test.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("RAILWAY_VOLUME_MOUNT_PATH", tempfile.mkdtemp())
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


class _Unreachable(Exception):
    """Stands in for the real thing, verbatim from the production log:
    OperationalError: ... ERROR: Your account or project has exceeded the quota.
    """


def main():
    db.init_db()
    now = datetime.utcnow()

    print("\nA FAILURE MUST NEVER READ AS A MEASUREMENT\n")

    # ── An empty answer is a REAL answer and must still work ─────────────────
    print("1. With the database healthy, 'nothing pending' is a real answer")
    check("no pending bets -> next_pending_kickoff() is None",
          db.next_pending_kickoff() is None,
          "expected None on an empty bet log")
    check("no pending bets -> targeting list is empty",
          db.sport_keys_with_pending_bets(45) == [],
          "expected [] on an empty bet log")

    conn = db.get_connection()
    conn.execute(
        "INSERT INTO odds_events (event_id, sport_key, sport_name, home_team,"
        " away_team, commence_time) VALUES (?,?,?,?,?,?)",
        ("e1", "soccer_epl", "EPL", "H", "A",
         (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    db.add_bet({"event_id": "e1", "market": "h2h", "selection": "H", "odds": 2.0,
                "stake": 10.0, "home_team": "H", "away_team": "A",
                "commence_time": (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")})

    print("\n2. A backed game 30min out is found")
    check("next_pending_kickoff() sees it", db.next_pending_kickoff() is not None)
    check("the league is targeted",
          db.sport_keys_with_pending_bets(45) == ["soccer_epl"],
          f"got {db.sport_keys_with_pending_bets(45)}")

    # ── The whole point: a broken database must NOT look like "nothing" ──────
    print("\n3. An unreachable database RAISES — it does not answer 'nothing'")
    real = db.get_connection

    def boom():
        raise _Unreachable(
            'connection to server failed: ERROR: Your account or project has '
            'exceeded the quota. Upgrade your plan to increase limits.')

    db.get_connection = boom
    try:
        try:
            got = db.sport_keys_with_pending_bets(45)
            check("sport_keys_with_pending_bets raises on a DB failure", False,
                  f"returned {got!r} — an unreachable DB is indistinguishable "
                  f"from 'nothing is backed', so the capture skips silently")
        except _Unreachable:
            check("sport_keys_with_pending_bets raises on a DB failure", True)
        except Exception as e:                        # noqa: BLE001
            check("sport_keys_with_pending_bets raises on a DB failure", True,
                  f"(raised {type(e).__name__})")

        try:
            got = db.next_pending_kickoff()
            check("next_pending_kickoff raises on a DB failure", False,
                  f"returned {got!r} — cached as 'nothing pending' this blinds "
                  f"the capture for up to an hour of kickoffs")
        except _Unreachable:
            check("next_pending_kickoff raises on a DB failure", True)
        except Exception as e:                        # noqa: BLE001
            check("next_pending_kickoff raises on a DB failure", True,
                  f"(raised {type(e).__name__})")
    finally:
        db.get_connection = real

    # ── And the consequence, end to end ──────────────────────────────────────
    print("\n4. The capture reports the failure instead of '0 credits spent'")
    os.environ.setdefault("ODDS_API_KEY", "dummy")
    import main as app_main
    from fastapi.testclient import TestClient
    client = TestClient(app_main.app)

    app_main._next_ko_cache.update({"checked_at": None, "next_ko": None,
                                    "db_error_at": None})
    db.get_connection = boom
    try:
        r = client.get("/api/closing/run?within=45")
        body = r.json()
        check("a DB failure is reported as not-ok", body.get("ok") is False,
              f"ok={body.get('ok')} body={str(body)[:200]}")
        check("the message does not claim there was nothing to do",
              "0 credits spent" not in str(body.get("messages", "")),
              f"messages={body.get('messages')} — this is the line that lied")

        print("\n5. The failure is not cached as 'nothing pending'")
        check("the cache was left untouched by the failure",
              app_main._next_ko_cache["checked_at"] is None,
              "a failed lookup was stored as a fresh answer")
        check("and we do not skip the next call on the strength of it",
              "sem apostas pendentes" not in (app_main._closing_skip_reason(45) or ""),
              "skipped as if nothing were pending — kickoffs would be missed")

        print("\n6. /api/diag/closing still answers JSON while the DB is down")
        # main.py holds its own reference (`from collectors.database import
        # get_connection`), so patching the module attribute alone is not enough.
        main_real = app_main.get_connection
        app_main.get_connection = boom
        d = client.get("/api/diag/closing")
        app_main.get_connection = main_real
        check("status is not a bare 500", d.status_code == 200,
              f"got {d.status_code}")
        j = d.json()
        check("it names the database as the cause",
              "ERROR" in str(j.get("pending_bets")) or
              "indisponivel" in str(j.get("verdict", "")),
              f"pending_bets={str(j.get('pending_bets'))[:120]}")
        check("the dry-run shows an ERROR, not an innocent empty list",
              any("ERROR" in str(v) for v in (j.get("targeting_dry_run") or {}).values()),
              f"targeting_dry_run={j.get('targeting_dry_run')}")
    finally:
        db.get_connection = real

    print(f"\n{'✅ ALL CHECKS PASSED' if failures == 0 else f'❌ {failures} CHECK(S) FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
