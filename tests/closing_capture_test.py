#!/usr/bin/env python3
"""Closing-capture targeting + credit cost contract.

WHY: the closing capture is the only thing that makes a bet verifiable after the
fact, but it used to re-fetch EVERY football/tennis league with an imminent
fixture — 1 credit per league, on a */15 cron, whether or not a single one of
those games was backed. That burn kept emptying the monthly quota, and once the
quota fell below the shared 50-credit brake the capture was the first thing to
stand down. Measured result: 67% of stored closes were taken more than 6h before
kickoff, leaving the CLV meaningless.

THE CONTRACT
  1. Closing mode fetches ONLY leagues holding a tracked PENDING bet in the
     window. Cost scales with the bet log (~1 credit per bet per attempt), not
     with the fixture list.
  2. No tracked bet in the window ⇒ zero credits spent.
  3. The per-sport throttle must not delay a tracked close (it was pushing tennis
     captures up to an hour late, or skipping them).
  4. Closing mode has its own low quota floor, so a low balance stops routine
     refreshes long before it stops the capture that matters.

Run: RAILWAY_VOLUME_MOUNT_PATH=$(mktemp -d) ODDS_API_KEY=dummy \
     python3 tests/closing_capture_test.py
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
calls = []


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}\n      {detail}")
        failures += 1


def run_closing(window=45):
    """Run a closing pass with the network stubbed; return the leagues fetched."""
    calls.clear()
    odds.refresh_imminent_odds(status_callback=lambda m: None, within_minutes=window)
    return list(calls)


def main():
    db.init_db()
    now = datetime.utcnow()

    conn = db.get_connection()
    for eid, key, mins in (("e1", "soccer_epl", 25),
                           ("e2", "soccer_spain_la_liga", 30),
                           ("e3", "tennis_atp_us_open", 35)):
        conn.execute(
            "INSERT INTO odds_events (event_id, sport_key, sport_name, home_team,"
            " away_team, commence_time) VALUES (?,?,?,?,?,?)",
            (eid, key, key, "H", "A",
             (now + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

    bet_id = db.add_bet({
        "event_id": "e3", "market": "h2h", "selection": "H", "odds": 2.0, "stake": 10.0,
        "commence_time": (now + timedelta(minutes=35)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    # Stub the network: record the intended fetch, spend nothing.
    odds._LAST_REMAINING["v"] = 400
    odds.fetch_odds = lambda sk, markets=None, hours_ahead=None: (
        calls.append(sk), (None, 399))[1]

    print("\nCLOSING CAPTURE TARGETING + CREDIT COST\n")
    print("  fixture list: 3 leagues kicking off inside 45min")
    print("  bet log:      1 tracked pending bet (tennis_atp_us_open)\n")

    # ── 1. Only the backed league is fetched ─────────────────────────────────
    print("1. Credits go only where they buy a verifiable bet")
    fetched = run_closing()
    check("fetches exactly the league holding the tracked bet",
          fetched == ["tennis_atp_us_open"], f"fetched {fetched}")
    check("cost is 1 credit, not 3 (one per imminent league)",
          len(fetched) == 1, f"cost {len(fetched)}")

    # ── 2. Nothing backed ⇒ nothing spent ────────────────────────────────────
    print("\n2. No tracked bet in the window ⇒ no spend")
    conn = db.get_connection()
    conn.execute("UPDATE bets SET status='settled' WHERE id=?", (bet_id,))
    conn.commit()
    conn.close()
    check("zero credits when nothing is backed", run_closing() == [], "spent credits anyway")

    # ── 3. The throttle must not delay a tracked close ───────────────────────
    print("\n3. The per-sport throttle no longer delays a tracked close")
    conn = db.get_connection()
    conn.execute("UPDATE bets SET status='pending' WHERE id=?", (bet_id,))
    conn.commit()
    conn.close()
    first, second = run_closing(), run_closing()
    check("two consecutive passes both capture (tennis throttle was 60min)",
          len(first) == 1 and len(second) == 1,
          f"first={first} second={second} — throttle still blocking")

    # ── 4. Quota floors ──────────────────────────────────────────────────────
    print("\n4. A low balance stops routine refreshes before the closing capture")
    check("closing floor is far below the routine floor",
          odds.CLOSING_MIN_QUOTA < odds.IMMINENT_MIN_QUOTA,
          f"closing={odds.CLOSING_MIN_QUOTA} routine={odds.IMMINENT_MIN_QUOTA}")

    odds._LAST_REMAINING["v"] = (odds.CLOSING_MIN_QUOTA + odds.IMMINENT_MIN_QUOTA) // 2
    check("at a mid balance the closing capture still runs",
          run_closing() == ["tennis_atp_us_open"],
          f"remaining={odds._LAST_REMAINING['v']} but capture was skipped")

    calls.clear()
    odds.refresh_imminent_odds(status_callback=lambda m: None)   # routine mode
    check("...while the routine refresh stands down at the same balance",
          calls == [], f"routine refresh still fetched {calls}")

    odds._LAST_REMAINING["v"] = odds.CLOSING_MIN_QUOTA - 1
    check("below its own floor the closing capture finally stops too",
          run_closing() == [], "kept spending below the closing floor")

    print(f"\n{'✅ ALL CHECKS PASSED' if failures == 0 else f'❌ {failures} CHECK(S) FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
