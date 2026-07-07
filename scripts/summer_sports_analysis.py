"""Summer-window backtest (no product changes): which sports/leagues that PLAY
June-August are worth turning on while the World Cup + Wimbledon wind down.

Same methodology as the football/tennis studies: devig the reference with the
POWER method (edge = no-vig CLV), site staking (¼-Kelly, short-odd half+1.5%
cap <1.40, 2% overall cap, €1000), one bet/match (best-edge H2H side, edge>=2,
odds<=5), the current green portfolio (1.4-4.0 @ edge>=2 + 1.3-1.4 @ >=3),
bootstrap 95% CI.

Source: football-data.co.uk /new/ extra-league CSVs (BRA, USA/MLS, NOR, SWE,
JPN, MEX, IRL, FIN). HONEST CAVEAT the caller must relay: the extra files carry
Pinnacle PRE-MATCH odds (PH/PD/PA) — there is NO explicit closing variant like
the main leagues' PSCH — so this "close" is softer than the football/tennis
studies. The script probes the header and reports exactly which column it used.

Baseball (MLB) et al: The Odds API is LIVE-only on our tier and no free
historical Pinnacle closing source is wired — so they are NOT backtestable with
this methodology. That is reported, not faked.
"""
import os
import sys
import io
import csv
import json
import random
import statistics
from datetime import date

os.environ.setdefault("RAILWAY_VOLUME_MOUNT_PATH", "/tmp/btlab")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from collectors import database as db

BANKROLL = 1000.0
# Leagues whose season spans the northern-hemisphere summer (June-August).
LEAGUES = {
    "BRA": "Brazil Série A", "USA": "MLS (USA)", "NOR": "Norway Eliteserien",
    "SWE": "Sweden Allsvenskan", "JPN": "Japan J-League", "MEX": "Mexico Liga MX",
    "IRL": "Ireland Premier", "FIN": "Finland Veikkausliiga",
}
BASE = "https://www.football-data.co.uk/new/{code}.csv"


def _f(row, *keys):
    for k in keys:
        v = (row.get(k) or "").strip()
        if v:
            try:
                return float(v)
            except ValueError:
                pass
    return None


def kelly_stake_site(edge_pct, odd):
    if edge_pct <= 0 or odd <= 1:
        return 0.0
    f = (edge_pct / 100.0) / (odd - 1.0) * 0.25
    if odd < 1.40:
        f = min(f * 0.5, 0.015)
    f = min(f, 0.02)
    return round(f * BANKROLL, 2)


def new_rule(e, odd):
    return e <= 15 and ((1.4 <= odd <= 4.0 and e >= 2.0) or (1.3 <= odd < 1.4 and e >= 3.0))


def analyse_rows(rows, pin_cols, price_pref):
    """rows: dicts with Res/date + odds. pin_cols=(H,D,A) reference column names.
    price_pref: list of best-price column templates ('{s}' = side letter)."""
    pairs, bets, summer = [], [], 0
    price_used = price_pref[0].format(s="*") if price_pref else "none"
    for r in rows:
        res = (r.get("Res") or "").strip().upper()
        if res not in ("H", "D", "A"):
            continue
        ph, pd_, pa = (_f(r, pin_cols[0]), _f(r, pin_cols[1]), _f(r, pin_cols[2]))
        dv = db.remove_vig_power(ph, pd_, pa) if (ph and pd_ and pa) else None
        if not dv:
            continue
        # June-August share
        try:
            d = r.get("Date", "")
            parts = d.split("/")
            mo = int(parts[1]) if len(parts) == 3 else 0
            if mo in (6, 7, 8):
                summer += 1
        except Exception:
            pass
        for side, p_pct, occ, odd_ref in (("H", dv["home"], res == "H", ph),
                                          ("D", dv["draw"], res == "D", pd_),
                                          ("A", dv["away"], res == "A", pa)):
            if p_pct and odd_ref:
                pairs.append((p_pct / 100.0, 1 if occ else 0, odd_ref))
        # best-edge bet at the chosen book price
        cands = []
        for side, p_pct, won in (("H", dv["home"], res == "H"),
                                 ("D", dv["draw"], res == "D"),
                                 ("A", dv["away"], res == "A")):
            odd = None
            for col in price_pref:
                odd = _f(r, col.format(s=side))
                if odd:
                    break
            if not odd or odd <= 1 or odd > 5.0 or not p_pct:
                continue
            edge = (odd * p_pct / 100.0 - 1) * 100
            if edge >= 2.0:
                cands.append((edge, odd, won))
        if cands:
            bets.append(max(cands, key=lambda c: c[0]))

    # calibration (all outcomes)
    if pairs:
        pred = statistics.mean(p for p, _, _ in pairs)
        act = statistics.mean(o for _, o, _ in pairs)
        binm = {}
        for p, o, _ in pairs:
            binm.setdefault(int(p * 20), []).append((p, o))
        mae = sum(len(v) * abs(statistics.mean(p for p, _ in v) - statistics.mean(o for _, o in v))
                  for v in binm.values()) / len(pairs)
        calib = {"n_outcomes": len(pairs), "bias_pp": round((pred - act) * 100, 2),
                 "mae_pp": round(mae * 100, 2)}
    else:
        calib = None

    # portfolio under the site's real staking + current green rule
    sel = []
    for (e, odd, won) in bets:
        if not new_rule(e, odd):
            continue
        stake = kelly_stake_site(e, odd)
        if stake < 0.01:
            continue
        sel.append((stake, stake * (odd - 1) if won else -stake, won))
    port = {"n": 0}
    if sel:
        staked = sum(s for s, _, _ in sel)
        profit = sum(p for _, p, _ in sel)
        rnd = random.Random(11)
        boots = sorted((sum(x[1] for x in rnd.choices(sel, k=len(sel))) /
                        max(sum(x[0] for x in rnd.choices(sel, k=len(sel))), 1) * 100)
                       for _ in range(3000))
        # Proper paired bootstrap (resample rows, ratio of sums):
        boots = []
        for _ in range(3000):
            sample = rnd.choices(sel, k=len(sel))
            st = sum(s for s, _, _ in sample)
            boots.append(sum(p for _, p, _ in sample) / st * 100 if st else 0.0)
        boots.sort()
        port = {"n": len(sel), "win_rate_pct": round(sum(1 for _, _, w in sel if w) / len(sel) * 100, 1),
                "total_staked_eur": round(staked, 0), "profit_eur": round(profit, 2),
                "roi_pct": round(profit / staked * 100, 2),
                "roi_ci95": [round(boots[75], 2), round(boots[2925], 2)]}
    return calib, port, summer, price_used


def main():
    db.init_db()
    out = {"leagues": {}}
    for code, name in LEAGUES.items():
        try:
            resp = requests.get(BASE.format(code=code), timeout=45)
            if resp.status_code != 200 or len(resp.content) < 500:
                out["leagues"][code] = {"name": name, "error": f"HTTP {resp.status_code}"}
                continue
            text = resp.content.decode("latin-1", errors="replace")
            header = next(csv.reader(io.StringIO(text)))
            rows = list(csv.DictReader(io.StringIO(text)))
            # choose the reference column set actually present (closing preferred)
            if all(c in header for c in ("PSCH", "PSCD", "PSCA")):
                pin, ref_label = ("PSCH", "PSCD", "PSCA"), "Pinnacle CLOSING (PSC*)"
            elif all(c in header for c in ("PH", "PD", "PA")):
                pin, ref_label = ("PH", "PD", "PA"), "Pinnacle pre-match (PH/PD/PA — NO closing variant)"
            elif all(c in header for c in ("AvgH", "AvgD", "AvgA")):
                pin, ref_label = ("AvgH", "AvgD", "AvgA"), "market AVERAGE (no Pinnacle col — weak ruler)"
            else:
                out["leagues"][code] = {"name": name, "rows": len(rows),
                                        "error": "no usable reference odds columns",
                                        "header_sample": header[:25]}
                continue
            # Bet at the BEST price across books (soft proxy), NOT the Pinnacle
            # reference itself — the extra files' closing best-price columns are
            # MaxC*/AvgC*, pre-match are Max*/Avg*. Never fall back to PSC* (=the
            # reference; betting there vs its own devig fabricates ~0 edge).
            best_cols = [c for c in ("MaxC{s}", "Max{s}", "AvgC{s}", "Avg{s}", "B365C{s}", "B365{s}")
                         if c.format(s="H") in header]
            calib, port, summer, price_used = analyse_rows(rows, pin, best_cols)
            out["leagues"][code] = {"name": name, "rows": len(rows),
                                    "summer_matches_JunAug": summer,
                                    "reference_used": ref_label, "bet_price_used": price_used,
                                    "calibration": calib, "portfolio_new_rule": port}
        except Exception as e:
            out["leagues"][code] = {"name": name, "error": repr(e)}

    out["baseball_note"] = ("MLB / other Odds-API sports: LIVE-only feed, no free historical "
                            "Pinnacle closing source wired → NOT backtestable with this methodology.")
    print("SUMMER_ANALYSIS_JSON: " + json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
