"""One-off decision analysis (no product changes): should the green traffic-light
threshold for odds 1.4-1.8 drop from edge>=3% to edge>=2% (matching 1.8-4.0)?

Method — mirrors the live engine's ruler:
  truth      = Pinnacle CLOSING odds devigged with the POWER method (same devig
               the site uses for edges), so "edge" here = realized no-vig CLV;
  bet price  = two variants: best price across books (backtest convention) and
               Bet365-only (fairer proxy for betting a single soft book);
  selection  = one bet per match, the best-edge H2H side with edge>=2, odds<=5
               (same rule as collectors/backtest.run_backtest).

Outputs, per odds band and edge slice: N, win rate, flat 1-unit ROI with a
bootstrap 95% CI — plus close-line calibration (bias/MAE) per band.

Run via the backtest-lab workflow (needs football-data.co.uk access).
"""
import os
import sys
import json
import random
import statistics

os.environ.setdefault("RAILWAY_VOLUME_MOUNT_PATH", "/tmp/btlab")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import database as db
from collectors.footballdata import collect_footballdata

BAND_A = (1.4, 1.8)   # the band whose threshold is being questioned
BAND_B = (1.8, 4.0)   # mid-range reference (already green at >=2%)


def band_of(odd):
    if BAND_A[0] <= odd < BAND_A[1]:
        return "A_1.4-1.8"
    if BAND_B[0] <= odd <= BAND_B[1]:
        return "B_1.8-4.0"
    return None


def main():
    db.init_db()
    collect_footballdata()

    conn = db.get_connection()
    rows = [dict(r) for r in conn.execute("""
        SELECT result, pinnacle_home_close, pinnacle_draw_close, pinnacle_away_close,
               b365_home, b365_draw, b365_away,
               max_home, max_draw, max_away
        FROM football_matches
        WHERE result IS NOT NULL
          AND pinnacle_home_close IS NOT NULL
          AND pinnacle_draw_close IS NOT NULL
          AND pinnacle_away_close IS NOT NULL
    """).fetchall()]
    conn.close()
    print(f"matches with Pinnacle close: {len(rows)}")

    # ---------- Q1: closing-line calibration per odds band (power devig) ----------
    pairs = []  # (fair_prob 0-1, occurred 0/1, close_odd)
    for r in rows:
        dv = db.remove_vig_power(r["pinnacle_home_close"], r["pinnacle_draw_close"],
                                 r["pinnacle_away_close"])
        if not dv:
            continue
        res = r["result"]
        for p_pct, occ, odd in ((dv["home"], res == "H", r["pinnacle_home_close"]),
                                (dv["draw"], res == "D", r["pinnacle_draw_close"]),
                                (dv["away"], res == "A", r["pinnacle_away_close"])):
            if p_pct and odd:
                pairs.append((p_pct / 100.0, 1 if occ else 0, odd))

    calib = {}
    for band_name, (lo, hi) in (("A_1.4-1.8", BAND_A), ("B_1.8-4.0", BAND_B)):
        sel = [(p, o) for (p, o, odd) in pairs if lo <= odd < hi or (band_name.startswith("B") and odd == hi)]
        n = len(sel)
        if n == 0:
            calib[band_name] = None
            continue
        pred = statistics.mean(p for p, _ in sel)
        act = statistics.mean(o for _, o in sel)
        # weighted MAE over 5pp predicted-probability bins
        bins = {}
        for p, o in sel:
            bins.setdefault(int(p * 20), []).append((p, o))
        mae = sum(len(v) * abs(statistics.mean(p for p, _ in v) - statistics.mean(o for _, o in v))
                  for v in bins.values()) / n
        calib[band_name] = {"n_outcomes": n,
                            "predicted_mean_pct": round(pred * 100, 2),
                            "actual_freq_pct": round(act * 100, 2),
                            "bias_pp": round((pred - act) * 100, 2),
                            "mae_pp": round(mae * 100, 2)}

    # ---------- Q2: historical performance of the edge slices ----------
    def simulate(price_cols):
        bets = []
        for r in rows:
            dv = db.remove_vig_power(r["pinnacle_home_close"], r["pinnacle_draw_close"],
                                     r["pinnacle_away_close"])
            if not dv:
                continue
            res = r["result"]
            cands = []
            for side, p_pct, won in (("home", dv["home"], res == "H"),
                                     ("draw", dv["draw"], res == "D"),
                                     ("away", dv["away"], res == "A")):
                odd = None
                for col in price_cols:
                    odd = r[f"{col}_{side}"]
                    if odd:
                        break
                if not odd or odd <= 1 or odd > 5.0 or not p_pct:
                    continue
                edge = (odd * p_pct / 100.0 - 1) * 100
                if edge >= 2.0:
                    cands.append((edge, odd, won))
            if cands:
                bets.append(max(cands, key=lambda c: c[0]))  # one bet/match: best edge
        return bets

    def slice_stats(bets, band, e_lo, e_hi):
        sel = [(e, odd, won) for (e, odd, won) in bets
               if band_of(odd) == band and e_lo <= e < e_hi]
        n = len(sel)
        if n == 0:
            return {"n": 0}
        rets = [(odd - 1) if won else -1.0 for (_, odd, won) in sel]
        roi = statistics.mean(rets) * 100
        wins = sum(1 for (_, _, won) in sel if won)
        rnd = random.Random(42)
        boots = sorted(statistics.mean(rnd.choices(rets, k=n)) * 100 for _ in range(4000))
        return {"n": n,
                "win_rate_pct": round(wins / n * 100, 1),
                "avg_edge_pct": round(statistics.mean(e for (e, _, _) in sel), 2),
                "flat_roi_pct": round(roi, 2),
                "roi_ci95": [round(boots[int(0.025 * len(boots))], 2),
                             round(boots[int(0.975 * len(boots))], 2)]}

    out = {"matches": len(rows), "calibration_by_band": calib, "simulation": {}}
    for label, cols in (("best_price", ("max", "b365")), ("b365_only", ("b365",))):
        bets = simulate(cols)
        out["simulation"][label] = {
            "A_1.4-1.8_edge_2-3  (a zona em questão)": slice_stats(bets, "A_1.4-1.8", 2, 3),
            "A_1.4-1.8_edge_3+   (verde atual)":       slice_stats(bets, "A_1.4-1.8", 3, 15),
            "B_1.8-4.0_edge_2-3  (já verde)":          slice_stats(bets, "B_1.8-4.0", 2, 3),
            "B_1.8-4.0_edge_3+":                       slice_stats(bets, "B_1.8-4.0", 3, 15),
        }

    print("ANALYSIS_RESULT_JSON: " + json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
