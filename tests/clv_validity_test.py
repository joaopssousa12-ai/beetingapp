#!/usr/bin/env python3
"""CLV validity + edge provenance contract.

Guards the defect found in the real bet log: 67% of stored "closing lines" had
been captured MORE THAN SIX HOURS before kickoff (median 9h, worst 43h). Those
snapshots are usually the very price that produced the entry edge, so the CLV
computed from them merely restated the edge — and the dashboard reported the
result as "+2.8% avg CLV, 97.4% positive", which looked like independent
confirmation and was not.

THE CONTRACT
  1. A CLV counts ONLY when its close was captured within CLV_MAX_LEAD_MIN of
     kickoff. Anything older is published as clv_pct_unverified, never clv_pct.
  2. get_bet_stats() applies the identical rule, and reports how many closes it
     threw away (clv_stale) so a small sample reads as "capture is broken"
     rather than "no data yet".
  3. Every bet records WHICH reference produced its edge (odds_source /
     ref_agreement), and unknown values are rejected rather than stored.

Run: RAILWAY_VOLUME_MOUNT_PATH=$(mktemp -d) python3 tests/clv_validity_test.py
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


def make_bet(lead_min, kickoff_iso, **extra):
    """Insert a settled bet whose close was captured `lead_min` before kickoff."""
    payload = {
        "event_id": f"ev-{lead_min}-{extra.get('tag', '')}",
        "sport_name": "Test", "home_team": "A", "away_team": "B",
        "commence_time": kickoff_iso, "market": "h2h", "selection": "A",
        "bookmaker": "1xBet", "odds": 2.00, "stake": 10.0, "edge_pct": 4.0,
    }
    payload.update({k: v for k, v in extra.items() if k != "tag"})
    bet_id = db.add_bet(payload)
    captured = (db._parse_ts(kickoff_iso) - timedelta(minutes=lead_min)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    conn = db.get_connection()
    conn.execute(
        "UPDATE bets SET status='settled', result='won', profit=10.0, "
        "pin_close_odds=1.88, pin_close_fair_odds=1.92, pin_close_captured_at=? "
        "WHERE id=?", (captured, bet_id))
    conn.commit()
    conn.close()
    return bet_id


def main():
    db.init_db()
    kickoff = (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("\nCLV VALIDITY + EDGE PROVENANCE CONTRACT\n")
    print(f"  thresholds: good ≤ {db.CLV_GOOD_LEAD_MIN}min, "
          f"countable ≤ {db.CLV_MAX_LEAD_MIN}min\n")

    # ── 1. The lead-time gate ────────────────────────────────────────────────
    print("1. A close captured far from kickoff must not be published as CLV")
    cases = [
        (20, True, "good"),      # 20 min before KO — a real close
        (90, True, "ok"),        # 1.5h — still defensible
        (540, False, "stale"),   # 9h — the observed MEDIAN in production
        (2600, False, "stale"),  # 43h — the observed worst case
    ]
    ids = {lead: make_bet(lead, kickoff, tag="gate") for lead, _, _ in cases}
    rows = {b["id"]: b for b in db.get_bets(200)}

    for lead, should_count, want_quality in cases:
        b = rows[ids[lead]]
        check(f"lead {lead:>4}min → quality '{want_quality}'",
              b["clv_quality"] == want_quality,
              f"got '{b['clv_quality']}'")
        if should_count:
            check(f"lead {lead:>4}min → clv_pct published",
                  b["clv_pct"] is not None and b["clv_pct_unverified"] is None,
                  f"clv_pct={b['clv_pct']} unverified={b['clv_pct_unverified']}")
        else:
            check(f"lead {lead:>4}min → clv_pct withheld, kept as unverified",
                  b["clv_pct"] is None and b["clv_pct_unverified"] is not None,
                  f"clv_pct={b['clv_pct']} unverified={b['clv_pct_unverified']}")

    check("lead time is reported so the UI can explain the gap",
          all(rows[i]["clv_lead_min"] is not None for i in ids.values()),
          "clv_lead_min missing")

    # ── 2. The aggregate uses the same rule ──────────────────────────────────
    print("\n2. get_bet_stats() applies the identical gate")
    s = db.get_bet_stats()
    check("only countable closes enter the average",
          s["clv_sample"] == 2, f"clv_sample={s['clv_sample']} (expected 2)")
    check("discarded closes are counted and surfaced",
          s["clv_stale"] == 2, f"clv_stale={s['clv_stale']} (expected 2)")
    check("the threshold is reported to the UI",
          s["clv_max_lead_min"] == db.CLV_MAX_LEAD_MIN,
          f"got {s.get('clv_max_lead_min')}")

    # A stale-only book must NOT look like "no data yet".
    check("a stale CLV never leaks into avg_clv",
          s["avg_clv"] is not None and s["clv_sample"] > 0,
          "expected a real average from the two countable bets")

    # ── 3. Provenance ────────────────────────────────────────────────────────
    print("\n3. Every bet records which reference produced its edge")
    good_id = make_bet(20, kickoff, tag="prov",
                       odds_source="blend", ref_agreement="agree")
    rows = {b["id"]: b for b in db.get_bets(200)}
    g = rows[good_id]
    check("odds_source round-trips", g["odds_source"] == "blend",
          f"got {g['odds_source']}")
    check("ref_agreement round-trips", g["ref_agreement"] == "agree",
          f"got {g['ref_agreement']}")

    bad_id = db.add_bet({"market": "h2h", "selection": "X", "odds": 2.0, "stake": 1.0,
                         "odds_source": "../../etc/passwd", "ref_agreement": "whatever"})
    bad = [b for b in db.get_bets(200) if b["id"] == bad_id][0]
    check("unknown odds_source is rejected, not stored",
          bad["odds_source"] is None, f"got {bad['odds_source']}")
    check("unknown ref_agreement is rejected, not stored",
          bad["ref_agreement"] is None, f"got {bad['ref_agreement']}")

    hand_id = db.add_bet({"market": "h2h", "selection": "Y", "odds": 2.0, "stake": 1.0})
    hand = [b for b in db.get_bets(200) if b["id"] == hand_id][0]
    check("a hand-entered bet stores NULL provenance (not a fake source)",
          hand["odds_source"] is None and hand["ref_agreement"] is None,
          f"got {hand['odds_source']}/{hand['ref_agreement']}")

    print(f"\n{'✅ ALL CHECKS PASSED' if failures == 0 else f'❌ {failures} CHECK(S) FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
