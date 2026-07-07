"""Decision analysis (no product changes): green traffic-light thresholds for
short odds. v2 adds:
  - the site's REAL staking instead of flat stakes: ¼-Kelly on a €1000 bankroll,
    half-sized + capped at 1.5% below odd 1.40 (vbKellyFraction port), plus a
    2% (€20) overall cap — with a count of how often each cap actually binds;
  - the 1.3-1.4 band analysed separately from 1.4-1.8 (the current green rule
    spans 1.3-1.8 but the v1 analysis only tested 1.4-1.8).

Ruler mirrors the live engine: truth = Pinnacle CLOSING odds devigged with the
POWER method (edge here = realized no-vig CLV); bet price = best-of-books and
Bet365-only variants; one bet per match (best-edge H2H side, edge>=2, odds<=5).

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

BANKROLL = 1000.0
BANDS = {"A0_1.3-1.4": (1.3, 1.4), "A_1.4-1.8": (1.4, 1.8), "B_1.8-4.0": (1.8, 4.0)}


def band_of(odd):
    for name, (lo, hi) in BANDS.items():
        if lo <= odd < hi or (name == "B_1.8-4.0" and odd == hi):
            return name
    return None


def kelly_stake_site(edge_pct, odd):
    """Port of vbKellyFraction (the site's live staking) + the 2% overall cap.
    Returns (stake_eur, cap2pct_hit, shortodd_rule_hit)."""
    if edge_pct <= 0 or odd <= 1:
        return 0.0, False, False
    f = (edge_pct / 100.0) / (odd - 1.0) * 0.25          # ¼-Kelly
    short_hit = False
    if odd < 1.40:                                        # short-odd softener
        f2 = min(f * 0.5, 0.015)
        short_hit = f2 < f
        f = f2
    cap_hit = f > 0.02
    f = min(f, 0.02)                                      # €20 overall cap
    return round(f * BANKROLL, 2), cap_hit, short_hit


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

    # ---------- calibration per band (power devig, the site's ruler) ----------
    pairs = []
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
    for name in BANDS:
        sel = [(p, o) for (p, o, odd) in pairs if band_of(odd) == name]
        n = len(sel)
        if n == 0:
            calib[name] = None
            continue
        pred = statistics.mean(p for p, _ in sel)
        act = statistics.mean(o for _, o in sel)
        bins = {}
        for p, o in sel:
            bins.setdefault(int(p * 20), []).append((p, o))
        mae = sum(len(v) * abs(statistics.mean(p for p, _ in v) - statistics.mean(o for _, o in v))
                  for v in bins.values()) / n
        calib[name] = {"n_outcomes": n,
                       "predicted_mean_pct": round(pred * 100, 2),
                       "actual_freq_pct": round(act * 100, 2),
                       "bias_pp": round((pred - act) * 100, 2),
                       "mae_pp": round(mae * 100, 2)}

    # ---------- simulation with the site's real staking ----------
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
                bets.append(max(cands, key=lambda c: c[0]))
        return bets

    def slice_stats(bets, band, e_lo, e_hi):
        sel = []
        cap2, short = 0, 0
        for (e, odd, won) in bets:
            if band_of(odd) != band or not (e_lo <= e < e_hi):
                continue
            stake, cap_hit, short_hit = kelly_stake_site(e, odd)
            if stake < 0.01:
                continue
            profit = stake * (odd - 1) if won else -stake
            sel.append((stake, profit, won))
            cap2 += cap_hit
            short += short_hit
        n = len(sel)
        if n == 0:
            return {"n": 0}
        staked = sum(s for s, _, _ in sel)
        profit = sum(p for _, p, _ in sel)
        rnd = random.Random(42)
        boots = []
        for _ in range(4000):
            sample = rnd.choices(sel, k=n)
            st = sum(s for s, _, _ in sample)
            boots.append(sum(p for _, p, _ in sample) / st * 100 if st > 0 else 0.0)
        boots.sort()
        return {"n": n,
                "win_rate_pct": round(sum(1 for _, _, w in sel if w) / n * 100, 1),
                "total_staked_eur": round(staked, 0),
                "profit_eur": round(profit, 2),
                "roi_pct": round(profit / staked * 100, 2),
                "roi_ci95": [round(boots[int(0.025 * len(boots))], 2),
                             round(boots[int(0.975 * len(boots))], 2)],
                "avg_stake_eur": round(staked / n, 2),
                "cap2pct_hits": cap2,
                "shortodd_rule_hits": short}

    # ---------- portfolio rules (the whole green tier as ONE group) ----------
    def rule_new(e, odd):        # current tiered rule, unified-range part only
        return 1.4 <= odd <= 4.0 and e >= 2.0
    def rule_new_full(e, odd):   # current tiered rule incl. the 1.3-1.4 @ >=3 tail
        return rule_new(e, odd) or (1.3 <= odd < 1.4 and e >= 3.0)
    def rule_old(e, odd):        # previous rule
        return (1.8 <= odd <= 4.0 and e >= 2.0) or (1.3 <= odd < 1.8 and e >= 3.0)

    def portfolio_stats(bets, rule):
        sel = []
        for (e, odd, won) in bets:
            if e > 15 or not rule(e, odd):
                continue
            stake, _, _ = kelly_stake_site(e, odd)
            if stake < 0.01:
                continue
            sel.append((stake, stake * (odd - 1) if won else -stake, won))
        n = len(sel)
        if n == 0:
            return {"n": 0}
        staked = sum(s for s, _, _ in sel)
        profit = sum(p for _, p, _ in sel)
        rnd = random.Random(7)
        boots = []
        for _ in range(4000):
            sample = rnd.choices(sel, k=n)
            st = sum(s for s, _, _ in sample)
            boots.append(sum(p for _, p, _ in sample) / st * 100 if st > 0 else 0.0)
        boots.sort()
        return {"n": n, "total_staked_eur": round(staked, 0), "profit_eur": round(profit, 2),
                "roi_pct": round(profit / staked * 100, 2),
                "roi_ci95": [round(boots[int(0.025 * len(boots))], 2),
                             round(boots[int(0.975 * len(boots))], 2)]}

    out = {"matches": len(rows), "staking": "site ¼-Kelly, short-odd half+1.5% cap (<1.40), overall 2% cap, €1000",
           "calibration_by_band": calib, "simulation": {}, "portfolios": {}}
    slices = [("A0_1.3-1.4", 2, 3), ("A0_1.3-1.4", 3, 15),
              ("A_1.4-1.8", 2, 3), ("A_1.4-1.8", 3, 15),
              ("B_1.8-4.0", 2, 3), ("B_1.8-4.0", 3, 15)]
    for label, cols in (("best_price", ("max", "b365")), ("b365_only", ("b365",))):
        bets = simulate(cols)
        out["simulation"][label] = {
            f"{band}_edge_{lo}-{hi}": slice_stats(bets, band, lo, hi)
            for band, lo, hi in slices}
        out["portfolios"][label] = {
            "NOVA_1.4-4.0_clv2 (grupo único)": portfolio_stats(bets, rule_new),
            "NOVA_completa (+1.3-1.4 clv3)":  portfolio_stats(bets, rule_new_full),
            "ANTIGA_1.8-4.0 clv2 + 1.3-1.8 clv3": portfolio_stats(bets, rule_old),
        }

    # =====================================================================
    # TENNIS — same methodology on tennis-data.co.uk (Pinnacle PSW/PSL are
    # the source's stated closing odds; no capture timestamp exists, honesty
    # about that caveat is on the caller).
    # =====================================================================
    from collectors.tennisdata import collect_tennisdata
    collect_tennisdata()
    conn = db.get_connection()
    trows = [dict(r) for r in conn.execute("""
        SELECT winner, ps_w, ps_l, b365_w, b365_l, max_w, max_l, avg_w, avg_l
        FROM tennis_odds
    """).fetchall()]
    conn.close()
    with_ps = [r for r in trows if r["ps_w"] and r["ps_l"] and r["ps_w"] > 1 and r["ps_l"] > 1]
    out["tennis"] = {"matches_total": len(trows), "matches_with_pinnacle_close": len(with_ps)}

    tpairs = []
    tbets_src = []
    for r in with_ps:
        dv = db.remove_vig_power(r["ps_w"], None, r["ps_l"])
        if not dv:
            continue
        # winner side occurred=1, loser side occurred=0
        tpairs.append((dv["home"] / 100.0, 1, r["ps_w"]))
        tpairs.append((dv["away"] / 100.0, 0, r["ps_l"]))
        tbets_src.append((r, dv))

    tcalib = {}
    for name in BANDS:
        sel = [(p, o) for (p, o, odd) in tpairs if band_of(odd) == name]
        n = len(sel)
        if n == 0:
            tcalib[name] = None
            continue
        pred = statistics.mean(p for p, _ in sel)
        act = statistics.mean(o for _, o in sel)
        bins = {}
        for p, o in sel:
            bins.setdefault(int(p * 20), []).append((p, o))
        mae = sum(len(v) * abs(statistics.mean(p for p, _ in v) - statistics.mean(o for _, o in v))
                  for v in bins.values()) / n
        tcalib[name] = {"n_outcomes": n, "predicted_mean_pct": round(pred * 100, 2),
                        "actual_freq_pct": round(act * 100, 2),
                        "bias_pp": round((pred - act) * 100, 2),
                        "mae_pp": round(mae * 100, 2)}
    out["tennis"]["calibration_by_band"] = tcalib

    def t_simulate(price_pref):
        bets = []
        for r, dv in tbets_src:
            cands = []
            for side, p_pct, won in (("w", dv["home"], True), ("l", dv["away"], False)):
                odd = None
                for col in price_pref:
                    odd = r[f"{col}_{side}"]
                    if odd:
                        break
                if not odd or odd <= 1 or odd > 5.0 or not p_pct:
                    continue
                edge = (odd * p_pct / 100.0 - 1) * 100
                if edge >= 2.0:
                    cands.append((edge, odd, won))
            if cands:
                bets.append(max(cands, key=lambda c: c[0]))
        return bets

    out["tennis"]["simulation"] = {}
    out["tennis"]["portfolios"] = {}
    for label, cols in (("best_price", ("max", "b365")), ("b365_only", ("b365",))):
        tbets = t_simulate(cols)
        out["tennis"]["simulation"][label] = {
            f"{band}_edge_{lo}-{hi}": slice_stats(tbets, band, lo, hi)
            for band, lo, hi in slices}
        out["tennis"]["portfolios"][label] = {
            "NOVA_1.4-4.0_clv2 (grupo único)": portfolio_stats(tbets, rule_new),
            "NOVA_completa (+1.3-1.4 clv3)":  portfolio_stats(tbets, rule_new_full),
        }

    print("ANALYSIS_RESULT_JSON: " + json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
