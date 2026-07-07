"""Extra-markets backtest (no product changes): Over/Under 2.5 and Asian
Handicap vs the 1X2 baseline, on the validated leagues.

Same rigor as the previous studies: reference = Pinnacle CLOSING columns
devigged with the POWER method (edge = no-vig CLV), bet at the best CLOSING
price across books (MaxC*) with a Bet365-closing single-book variant, site
staking (¼-Kelly, short-odd half+1.5% cap <1.40, 2% overall cap, €1000),
current green portfolio (odd 1.4-4.0 @ edge>=2, 1.3-1.4 @ >=3), one bet per
match PER MARKET, bootstrap 95% CI.

AH grading handles integer (push), half and quarter lines (half-win/half-loss
via two half-stakes at line±0.25).

Sources probed honestly per league: main-league CSVs (mmz4281) carry closing
O/U (PC>2.5) and closing AH (AHCh + PCAHH/PCAHA); the /new/ extra files
(Brazil, Japan, ...) are probed for equivalent columns and skipped with an
explicit note when absent.
"""
import os
import sys
import io
import csv
import json
import random
import statistics

os.environ.setdefault("RAILWAY_VOLUME_MOUNT_PATH", "/tmp/btlab")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from collectors import database as db

BANKROLL = 1000.0
MAIN = {  # football-data.co.uk mmz4281/{ss}/{div}.csv
    "E0": "Premier League", "E1": "Championship", "SP1": "La Liga", "SP2": "La Liga 2",
    "D1": "Bundesliga", "D2": "Bundesliga 2", "I1": "Serie A", "I2": "Serie B",
    "F1": "Ligue 1", "F2": "Ligue 2", "N1": "Eredivisie", "P1": "Primeira Liga",
    "B1": "Jupiler Pro League", "T1": "Super Lig", "G1": "Super League Greece",
    "SC0": "Scottish Premiership",
}
SEASONS = ["2526", "2425", "2324", "2223", "2122"]
EXTRA = {"BRA": "Brazil Série A", "JPN": "Japan J-League"}


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
    return round(min(f, 0.02) * BANKROLL, 2)


def green(e, odd):
    return e <= 15 and ((1.4 <= odd <= 4.0 and e >= 2.0) or (1.3 <= odd < 1.4 and e >= 3.0))


def ah_return_per_unit(margin, quarter):
    """Net return per 1 unit staked on an AH side with decimal odd `odd`:
    computed by caller from win/push/loss fractions. Here: fraction won/pushed.
    margin = goal margin incl. handicap for the CHOSEN side (>0 win, 0 push, <0 loss).
    quarter=True → the line is x.25/x.75: split into two sub-lines ±0.25."""
    def outcome(m):
        if m > 1e-9:
            return 1.0     # full win fraction
        if m < -1e-9:
            return -1.0    # full loss
        return 0.0         # push
    if not quarter:
        return outcome(margin)
    return (outcome(margin - 0.25) + outcome(margin + 0.25)) / 2.0


def portfolio(bets):
    """bets: list of (edge, odd, unit_result) where unit_result ∈ [-1..1]
    (fraction of stake won at odds `odd`: profit = stake*(odd-1)*win_frac - stake*loss_frac)."""
    sel = []
    for (e, odd, res) in bets:
        if not green(e, odd):
            continue
        stake = kelly_stake_site(e, odd)
        if stake < 0.01:
            continue
        win_frac = max(res, 0.0)
        loss_frac = max(-res, 0.0)
        profit = stake * (odd - 1) * win_frac - stake * loss_frac
        sel.append((stake, profit))
    n = len(sel)
    if n == 0:
        return {"n": 0}
    staked = sum(s for s, _ in sel)
    profit = sum(p for _, p in sel)
    rnd = random.Random(19)
    boots = []
    for _ in range(3000):
        sample = rnd.choices(sel, k=n)
        st = sum(s for s, _ in sample)
        boots.append(sum(p for _, p in sample) / st * 100 if st else 0.0)
    boots.sort()
    return {"n": n, "total_staked_eur": round(staked, 0), "profit_eur": round(profit, 2),
            "roi_pct": round(profit / staked * 100, 2),
            "roi_ci95": [round(boots[75], 2), round(boots[2925], 2)]}


def analyse(rows, price_book):
    """price_book: 'max' (MaxC*) or 'b365' (B365C*). Returns per-market bet lists."""
    ou_bets, ah_bets, x12_bets = [], [], []
    cov = {"ou_close": 0, "ah_close": 0, "x12_close": 0}
    for r in rows:
        try:
            hg, ag = int(float(r.get("FTHG", ""))), int(float(r.get("FTAG", "")))
        except (ValueError, TypeError):
            continue
        res = (r.get("FTR") or "").strip().upper()

        # ---- 1X2 baseline (closing) ----
        ph, pd_, pa = _f(r, "PSCH"), _f(r, "PSCD"), _f(r, "PSCA")
        if ph and pd_ and pa:
            cov["x12_close"] += 1
            dv = db.remove_vig_power(ph, pd_, pa)
            if dv:
                cands = []
                for side, p_pct, won in (("H", dv["home"], res == "H"),
                                         ("D", dv["draw"], res == "D"),
                                         ("A", dv["away"], res == "A")):
                    odd = _f(r, ("MaxC" + side) if price_book == "max" else ("B365C" + side))
                    if odd and 1 < odd <= 5.0 and p_pct:
                        e = (odd * p_pct / 100.0 - 1) * 100
                        if e >= 2:
                            cands.append((e, odd, 1.0 if won else -1.0))
                if cands:
                    x12_bets.append(max(cands, key=lambda c: c[0]))

        # ---- Over/Under 2.5 (closing) ----
        po, pu = _f(r, "PC>2.5"), _f(r, "PC<2.5")
        if po and pu:
            cov["ou_close"] += 1
            dv = db.remove_vig_power(po, None, pu)
            if dv:
                total = hg + ag
                cands = []
                for sel_, p_pct, won in (("over", dv["home"], total > 2.5),
                                         ("under", dv["away"], total < 2.5)):
                    col = ("MaxC" if price_book == "max" else "B365C") + (">2.5" if sel_ == "over" else "<2.5")
                    odd = _f(r, col)
                    if odd and 1 < odd <= 5.0 and p_pct:
                        e = (odd * p_pct / 100.0 - 1) * 100
                        if e >= 2:
                            cands.append((e, odd, 1.0 if won else -1.0))
                if cands:
                    ou_bets.append(max(cands, key=lambda c: c[0]))

        # ---- Asian Handicap (closing line AHCh) ----
        line = _f(r, "AHCh")
        pah_h, pah_a = _f(r, "PCAHH"), _f(r, "PCAHA")
        if line is not None and pah_h and pah_a:
            cov["ah_close"] += 1
            dv = db.remove_vig_power(pah_h, None, pah_a)
            if dv:
                quarter = abs((line * 4) - round(line * 4)) < 1e-9 and abs((line * 2) - round(line * 2)) > 1e-9
                m_home = (hg - ag) + line
                cands = []
                for sel_, p_pct, margin in (("home", dv["home"], m_home),
                                            ("away", dv["away"], -m_home)):
                    col = ("MaxC" if price_book == "max" else "B365C") + ("AHH" if sel_ == "home" else "AHA")
                    odd = _f(r, col)
                    if odd and 1 < odd <= 5.0 and p_pct:
                        e = (odd * p_pct / 100.0 - 1) * 100
                        if e >= 2:
                            cands.append((e, odd, ah_return_per_unit(margin, quarter)))
                if cands:
                    ah_bets.append(max(cands, key=lambda c: c[0]))
    return cov, {"x12": x12_bets, "ou25": ou_bets, "ah": ah_bets}


def main():
    db.init_db()
    rows = []
    for div in MAIN:
        for ss in SEASONS:
            try:
                resp = requests.get(f"https://www.football-data.co.uk/mmz4281/{ss}/{div}.csv", timeout=45)
                if resp.status_code == 200 and len(resp.content) > 500:
                    rows.extend(csv.DictReader(io.StringIO(resp.content.decode("latin-1", errors="replace"))))
            except Exception:
                pass
    print(f"main-league rows: {len(rows)}")

    out = {"main_leagues": {}, "extra_leagues": {}}
    for label, book in (("best_price", "max"), ("b365_only", "b365")):
        cov, bets = analyse(rows, book)
        out["main_leagues"][label] = {
            "coverage": cov,
            "portfolio_1X2": portfolio(bets["x12"]),
            "portfolio_OU25": portfolio(bets["ou25"]),
            "portfolio_AH": portfolio(bets["ah"]),
        }

    # Extra files: probe for O/U + AH closing columns
    for code, name in EXTRA.items():
        try:
            resp = requests.get(f"https://www.football-data.co.uk/new/{code}.csv", timeout=45)
            header = next(csv.reader(io.StringIO(resp.content.decode("latin-1", errors="replace"))))
            out["extra_leagues"][code] = {
                "name": name,
                "has_ou_close": all(c in header for c in ("PC>2.5", "PC<2.5")),
                "has_ou_any": any(">2.5" in c for c in header),
                "has_ah_close": all(c in header for c in ("AHCh", "PCAHH", "PCAHA")),
                "has_ah_any": any("AH" in c for c in header),
            }
        except Exception as e:
            out["extra_leagues"][code] = {"name": name, "error": repr(e)}

    print("MARKETS_ANALYSIS_JSON: " + json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
