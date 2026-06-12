"""
Backtesting engine for BetIQ.
Uses Pinnacle closing odds (devigged) as true probability source.
Simulates bets at b365/max odds where edge >= threshold.
"""
import math
import statistics
from .database import get_connection


def _devig_3way(h, d, a):
    if not (h and d and a and h > 1 and d > 1 and a > 1):
        return None, None, None
    m = 1/h + 1/d + 1/a
    return (1/h)/m, (1/d)/m, (1/a)/m


def _devig_2way(o, u):
    if not (o and u and o > 1 and u > 1):
        return None, None
    m = 1/o + 1/u
    return (1/o)/m, (1/u)/m


def _kelly(true_prob, book_odd, fraction):
    edge = true_prob * book_odd - 1
    if edge <= 0 or book_odd <= 1:
        return 0.0
    return (edge / (book_odd - 1)) * fraction


def _won_h2h(result, selection, home_team, away_team):
    if result == 'H' and selection == home_team:
        return True
    if result == 'D' and selection == 'Draw':
        return True
    if result == 'A' and selection == away_team:
        return True
    return False


def run_backtest(min_edge=3.0, max_odds=5.0, bankroll=1000.0, kelly_frac=0.25,
                 league=None, season=None, market_filter='all', max_kelly_pct=0.05):
    conn = get_connection()

    where_parts = ["result IS NOT NULL", "home_goals IS NOT NULL"]
    params = []
    if league:
        where_parts.append("league_name = ?")
        params.append(league)
    if season:
        where_parts.append("season = ?")
        params.append(season)
    where = "WHERE " + " AND ".join(where_parts)

    rows = conn.execute(f"""
        SELECT date, league_name, season, home_team, away_team,
               home_goals, away_goals, result,
               b365_home, b365_draw, b365_away,
               pinnacle_home_close, pinnacle_draw_close, pinnacle_away_close,
               max_home, max_draw, max_away,
               avg_home, avg_draw, avg_away,
               over25_pinnacle, under25_pinnacle, over25_max, over25_avg
        FROM football_matches
        {where}
        ORDER BY date ASC
    """, params).fetchall()
    conn.close()

    matches_scanned = len(rows)
    matches_with_odds = 0   # had usable reference + book odds for the chosen market
    # Per-column coverage so the UI can show WHY a run found (or didn't find) data
    coverage = {'b365': 0, 'pinnacle_close': 0, 'max': 0, 'avg': 0, 'ou_pinnacle': 0}
    bets = []
    current_bankroll = bankroll

    for r in rows:
        r = dict(r)
        date_str = r['date'] or ''
        match_had_odds = False

        # Track which odds columns are populated (home side as proxy)
        if r['b365_home']: coverage['b365'] += 1
        if r['pinnacle_home_close']: coverage['pinnacle_close'] += 1
        if r['max_home']: coverage['max'] += 1
        if r['avg_home']: coverage['avg'] += 1
        if r['over25_pinnacle']: coverage['ou_pinnacle'] += 1

        # --- Match Result (H2H) ---
        if market_filter in ('all', 'h2h'):
            # Reference (true prob): Pinnacle close is sharpest; fall back to the
            # market average (always present in football-data) so a run still works
            # when Pinnacle isn't recorded.
            if r['pinnacle_home_close'] and r['pinnacle_draw_close'] and r['pinnacle_away_close']:
                ref_h, ref_d, ref_a = r['pinnacle_home_close'], r['pinnacle_draw_close'], r['pinnacle_away_close']
            else:
                ref_h, ref_d, ref_a = r['avg_home'], r['avg_draw'], r['avg_away']
            true_h, true_d, true_a = _devig_3way(ref_h, ref_d, ref_a)

            # Bet at the BEST available price: max across books -> b365 -> avg.
            bh = r['max_home'] or r['b365_home'] or r['avg_home']
            bd = r['max_draw'] or r['b365_draw'] or r['avg_draw']
            ba = r['max_away'] or r['b365_away'] or r['avg_away']

            if true_h is not None and bh and bd and ba:
                match_had_odds = True
                # Only ONE H2H bet per match — the single best-edge side. (Betting
                # home+draw+away on the same match is contradictory and inflated the
                # bet count; a real value bettor backs one side.)
                candidates = []
                for sel, true_p, book_odd in [
                    (r['home_team'], true_h, bh),
                    ('Draw', true_d, bd),
                    (r['away_team'], true_a, ba),
                ]:
                    if not book_odd or book_odd > max_odds:
                        continue
                    edge_pct = (true_p * book_odd - 1) * 100
                    if edge_pct >= min_edge:
                        candidates.append((edge_pct, sel, true_p, book_odd))
                if candidates:
                    edge_pct, sel, true_p, book_odd = max(candidates, key=lambda x: x[0])
                    k = min(_kelly(true_p, book_odd, kelly_frac), max_kelly_pct)
                    # FLAT staking on the FIXED starting bankroll (no compounding).
                    stake = round(k * bankroll, 2)
                    if stake >= 0.01:
                        won = _won_h2h(r['result'], sel, r['home_team'], r['away_team'])
                        profit = round(stake * (book_odd - 1) if won else -stake, 2)
                        current_bankroll = round(current_bankroll + profit, 2)
                        bets.append({
                        'date': date_str,
                        'league': r['league_name'] or '',
                        'season': r['season'] or '',
                        'match': f"{r['home_team']} vs {r['away_team']}",
                        'market': 'Match Result',
                        'selection': sel,
                        'odds': book_odd,
                        'edge_pct': round(edge_pct, 2),
                        'true_prob': round(true_p * 100, 1),
                        'stake': stake,
                        'won': won,
                        'profit': profit,
                        'bankroll': current_bankroll,
                    })

        # --- Over/Under 2.5 ---
        if market_filter in ('all', 'ou25'):
            op = r['over25_pinnacle']
            up = r['under25_pinnacle']
            ob = r['over25_max'] or r['over25_avg']
            true_o, true_u = _devig_2way(op, up)

            if true_o is not None and ob:
                match_had_odds = True
                for sel, true_p, book_odd in [('Over 2.5', true_o, ob)]:
                    if not book_odd or book_odd > max_odds:
                        continue
                    edge_pct = (true_p * book_odd - 1) * 100
                    if edge_pct < min_edge:
                        continue
                    k = min(_kelly(true_p, book_odd, kelly_frac), max_kelly_pct)
                    # FLAT staking: size on the FIXED starting bankroll, not the
                    # compounding one. Compounding 17k sequential bets makes stakes
                    # (and profit) explode to billions — unrealistic and unbettable.
                    stake = round(k * bankroll, 2)
                    if stake < 0.01:
                        continue
                    total_goals = (r['home_goals'] or 0) + (r['away_goals'] or 0)
                    won = total_goals > 2.5
                    profit = round(stake * (book_odd - 1) if won else -stake, 2)
                    current_bankroll = round(current_bankroll + profit, 2)
                    bets.append({
                        'date': date_str,
                        'league': r['league_name'] or '',
                        'season': r['season'] or '',
                        'match': f"{r['home_team']} vs {r['away_team']}",
                        'market': 'Over/Under 2.5',
                        'selection': sel,
                        'odds': book_odd,
                        'edge_pct': round(edge_pct, 2),
                        'true_prob': round(true_p * 100, 1),
                        'stake': stake,
                        'won': won,
                        'profit': profit,
                        'bankroll': current_bankroll,
                    })

        if match_had_odds:
            matches_with_odds += 1

    if not bets:
        return {
            'summary': {
                'total_bets': 0,
                'matches_scanned': matches_scanned,
                'matches_with_odds': matches_with_odds,
                'coverage': coverage,
            },
            'bets': [], 'by_league': [], 'pnl_series': [],
        }

    total_bets = len(bets)
    wins = sum(1 for b in bets if b['won'])
    total_staked = sum(b['stake'] for b in bets)
    total_profit = sum(b['profit'] for b in bets)
    roi = total_profit / total_staked * 100 if total_staked > 0 else 0

    returns = [b['profit'] / b['stake'] for b in bets if b['stake'] > 0]
    mean_r = statistics.mean(returns) if returns else 0
    std_r = statistics.stdev(returns) if len(returns) > 1 else 1
    # Annualise by the REAL time span, not by treating all bets as one year
    # (which over-inflated Sharpe by sqrt(total_bets) ~= 132x for a 17k sample).
    dates = sorted(b['date'] for b in bets if b['date'])
    years = 1.0
    if len(dates) >= 2:
        try:
            from datetime import date as _date
            d0 = _date.fromisoformat(dates[0][:10])
            d1 = _date.fromisoformat(dates[-1][:10])
            years = max((d1 - d0).days / 365.25, 0.5)
        except Exception:
            years = 1.0
    bets_per_year = total_bets / years
    sharpe = (mean_r / std_r) * math.sqrt(bets_per_year) if std_r > 0 else 0

    # By market (Match Result vs Over/Under) — so duplicates/coverage are visible
    market_map = {}
    for b in bets:
        mk = b['market']
        if mk not in market_map:
            market_map[mk] = {'market': mk, 'bets': 0, 'wins': 0, 'staked': 0.0, 'profit': 0.0}
        market_map[mk]['bets'] += 1
        if b['won']:
            market_map[mk]['wins'] += 1
        market_map[mk]['staked'] += b['stake']
        market_map[mk]['profit'] += b['profit']
    by_market = []
    for d in market_map.values():
        d['win_rate'] = round(d['wins'] / d['bets'] * 100, 1) if d['bets'] else 0
        d['roi'] = round(d['profit'] / d['staked'] * 100, 1) if d['staked'] > 0 else 0
        d['profit'] = round(d['profit'], 2)
        d['staked'] = round(d['staked'], 2)
        by_market.append(d)
    by_market.sort(key=lambda x: x['bets'], reverse=True)

    # Distinct matches actually bet on (to show how many bets share a match)
    distinct_matches = len({(b['date'], b['match']) for b in bets})

    # By league
    league_map = {}
    for b in bets:
        lg = b['league'] or 'Unknown'
        if lg not in league_map:
            league_map[lg] = {'league': lg, 'bets': 0, 'wins': 0, 'staked': 0.0, 'profit': 0.0}
        league_map[lg]['bets'] += 1
        if b['won']:
            league_map[lg]['wins'] += 1
        league_map[lg]['staked'] += b['stake']
        league_map[lg]['profit'] += b['profit']

    by_league = []
    for d in league_map.values():
        d['win_rate'] = round(d['wins'] / d['bets'] * 100, 1)
        d['roi'] = round(d['profit'] / d['staked'] * 100, 1) if d['staked'] > 0 else 0
        d['profit'] = round(d['profit'], 2)
        d['staked'] = round(d['staked'], 2)
        by_league.append(d)
    by_league.sort(key=lambda x: x['profit'], reverse=True)

    # P&L series — downsample to keep the chart payload small and fast.
    # A 92k-match run can emit tens of thousands of bets; sending them all
    # bloats the JSON and makes Chart.js lag. Cap at ~400 evenly-spaced points
    # (always including the final point so the ending bankroll is shown).
    MAX_POINTS = 400
    cumulative = 0.0
    full_series = []
    for b in bets:
        cumulative += b['profit']
        full_series.append({
            'date': b['date'],
            'profit': b['profit'],
            'cumulative': round(cumulative, 2),
            'won': b['won'],
            'match': b['match'],
            'selection': b['selection'],
            'odds': b['odds'],
            'edge_pct': b['edge_pct'],
        })
    if len(full_series) <= MAX_POINTS:
        pnl_series = full_series
    else:
        step = len(full_series) / MAX_POINTS
        idxs = sorted({int(i * step) for i in range(MAX_POINTS)} | {len(full_series) - 1})
        pnl_series = [full_series[i] for i in idxs]

    return {
        'summary': {
            'total_bets': total_bets,
            'matches_scanned': matches_scanned,
            'matches_with_odds': matches_with_odds,
            'coverage': coverage,
            'wins': wins,
            'losses': total_bets - wins,
            'win_rate': round(wins / total_bets * 100, 1),
            'total_staked': round(total_staked, 2),
            'total_profit': round(total_profit, 2),
            'roi': round(roi, 2),
            'sharpe': round(sharpe, 2),
            'initial_bankroll': bankroll,
            'final_bankroll': round(current_bankroll, 2),
            'bankroll_growth': round((current_bankroll - bankroll) / bankroll * 100, 1),
            'avg_edge': round(statistics.mean(b['edge_pct'] for b in bets), 2),
            'avg_odds': round(statistics.mean(b['odds'] for b in bets), 2),
            'max_odd': round(max(b['odds'] for b in bets), 2),
            'min_odd': round(min(b['odds'] for b in bets), 2),
            'staking': 'flat (¼-Kelly on starting bankroll, cap 5%)',
            'years': round(years, 1),
            'distinct_matches': distinct_matches,
            'bets_per_match': round(total_bets / distinct_matches, 2) if distinct_matches else 0,
        },
        'bets': bets[-500:],
        'by_league': by_league,
        'by_market': by_market,
        'pnl_series': pnl_series,
    }


def get_clv_analysis():
    """CLV from real tracked bets with Pinnacle closing odds."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT placed_at, market, selection, bookmaker, odds, stake,
               pin_close_odds, result, profit, edge_pct,
               home_team, away_team, sport_name
        FROM bets
        WHERE status = 'settled'
          AND pin_close_odds IS NOT NULL
          AND odds IS NOT NULL
          AND odds > 1
          AND pin_close_odds > 1
        ORDER BY placed_at ASC
    """).fetchall()
    conn.close()

    records = []
    for r in rows:
        r = dict(r)
        your_implied = 1 / r['odds']
        close_implied = 1 / r['pin_close_odds']
        clv = (close_implied - your_implied) / your_implied * 100
        r['clv_pct'] = round(clv, 2)
        records.append(r)

    if not records:
        return {'count': 0, 'avg_clv': 0.0, 'positive_clv_rate': 0.0, 'records': []}

    avg_clv = statistics.mean(r['clv_pct'] for r in records)
    pos_clv = sum(1 for r in records if r['clv_pct'] > 0)

    return {
        'count': len(records),
        'avg_clv': round(avg_clv, 2),
        'positive_clv_rate': round(pos_clv / len(records) * 100, 1),
        'records': records,
    }


def get_backtest_meta():
    """Return available leagues and seasons for filter dropdowns."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT DISTINCT league_name, season
        FROM football_matches
        WHERE result IS NOT NULL AND league_name IS NOT NULL
        ORDER BY league_name, season DESC
    """).fetchall()
    conn.close()
    leagues = sorted({r['league_name'] for r in rows if r['league_name']})
    seasons = sorted({r['season'] for r in rows if r['season']}, reverse=True)
    return {'leagues': leagues, 'seasons': seasons}


# ============================================================
# SWEET-SPOT COMPARISON — run several (min_edge, max_odds) settings in one pass
# ============================================================
def _scenario_bets(rows, min_edge, max_odds, bankroll, kelly_frac, max_kelly_pct, market_filter):
    """Same staking/selection logic as run_backtest, returned as (stake, profit, won, date) tuples."""
    out = []
    for r in rows:
        # --- Match Result: one best-edge bet per match ---
        if market_filter in ('all', 'h2h'):
            if r['pinnacle_home_close'] and r['pinnacle_draw_close'] and r['pinnacle_away_close']:
                ref_h, ref_d, ref_a = r['pinnacle_home_close'], r['pinnacle_draw_close'], r['pinnacle_away_close']
            else:
                ref_h, ref_d, ref_a = r['avg_home'], r['avg_draw'], r['avg_away']
            th, td, ta = _devig_3way(ref_h, ref_d, ref_a)
            bh = r['max_home'] or r['b365_home'] or r['avg_home']
            bd = r['max_draw'] or r['b365_draw'] or r['avg_draw']
            ba = r['max_away'] or r['b365_away'] or r['avg_away']
            if th is not None and bh and bd and ba:
                cands = []
                for sel, tp, od in [(r['home_team'], th, bh), ('Draw', td, bd), (r['away_team'], ta, ba)]:
                    if not od or od > max_odds:
                        continue
                    e = (tp * od - 1) * 100
                    if e >= min_edge:
                        cands.append((e, sel, tp, od))
                if cands:
                    e, sel, tp, od = max(cands, key=lambda x: x[0])
                    k = min(_kelly(tp, od, kelly_frac), max_kelly_pct)
                    stake = round(k * bankroll, 2)
                    if stake >= 0.01:
                        won = _won_h2h(r['result'], sel, r['home_team'], r['away_team'])
                        out.append((stake, round(stake * (od - 1) if won else -stake, 2), won, r['date'] or ''))
        # --- Over/Under 2.5 ---
        if market_filter in ('all', 'ou25'):
            to, tu = _devig_2way(r['over25_pinnacle'], r['under25_pinnacle'])
            ob = r['over25_max'] or r['over25_avg']
            if to is not None and ob and ob <= max_odds:
                e = (to * ob - 1) * 100
                if e >= min_edge:
                    k = min(_kelly(to, ob, kelly_frac), max_kelly_pct)
                    stake = round(k * bankroll, 2)
                    if stake >= 0.01:
                        won = ((r['home_goals'] or 0) + (r['away_goals'] or 0)) > 2.5
                        out.append((stake, round(stake * (ob - 1) if won else -stake, 2), won, r['date'] or ''))
    return out


def _summarize_scenario(min_edge, max_odds, bets):
    n = len(bets)
    if n == 0:
        return {'min_edge': min_edge, 'max_odds': max_odds, 'bets': 0, 'win_rate': 0,
                'roi': 0, 'sharpe': 0, 'profit': 0}
    wins = sum(1 for b in bets if b[2])
    staked = sum(b[0] for b in bets)
    profit = sum(b[1] for b in bets)
    returns = [b[1] / b[0] for b in bets if b[0] > 0]
    mean_r = statistics.mean(returns) if returns else 0
    std_r = statistics.stdev(returns) if len(returns) > 1 else 1
    dates = sorted(b[3] for b in bets if b[3])
    years = 1.0
    if len(dates) >= 2:
        try:
            from datetime import date as _date
            years = max((_date.fromisoformat(dates[-1][:10]) - _date.fromisoformat(dates[0][:10])).days / 365.25, 0.5)
        except Exception:
            years = 1.0
    sharpe = (mean_r / std_r) * math.sqrt(n / years) if std_r > 0 else 0
    return {
        'min_edge': min_edge, 'max_odds': max_odds, 'bets': n,
        'win_rate': round(wins / n * 100, 1),
        'roi': round(profit / staked * 100, 2) if staked > 0 else 0,
        'sharpe': round(sharpe, 2),
        'profit': round(profit, 2),
    }


def compare_thresholds(bankroll=1000.0, kelly_frac=0.25, max_kelly_pct=0.05,
                       market_filter='all', scenarios=None):
    """Run several (min_edge, max_odds) settings over the SAME data (loaded once)."""
    scenarios = scenarios or [
        (3.0, 5.0), (4.0, 5.0), (5.0, 5.0), (5.0, 4.0), (6.0, 5.0), (8.0, 5.0),
    ]
    conn = get_connection()
    rows = conn.execute("""
        SELECT date, home_team, away_team, home_goals, away_goals, result,
               b365_home, b365_draw, b365_away,
               pinnacle_home_close, pinnacle_draw_close, pinnacle_away_close,
               max_home, max_draw, max_away, avg_home, avg_draw, avg_away,
               over25_pinnacle, under25_pinnacle, over25_max, over25_avg
        FROM football_matches
        WHERE result IS NOT NULL AND home_goals IS NOT NULL
        ORDER BY date ASC
    """).fetchall()
    conn.close()
    rows = [dict(r) for r in rows]  # materialise once, reuse for every scenario

    results = []
    for (min_edge, max_odds) in scenarios:
        bets = _scenario_bets(rows, min_edge, max_odds, bankroll, kelly_frac, max_kelly_pct, market_filter)
        results.append(_summarize_scenario(min_edge, max_odds, bets))

    # Heuristic recommendation: best risk-adjusted return (Sharpe) among scenarios
    # with a meaningful sample (>= 500 bets) is the "sweet spot".
    valid = [r for r in results if r['bets'] >= 500]
    best_sharpe = max((r['sharpe'] for r in valid), default=0)
    for r in results:
        if r['bets'] == 0:
            r['recommendation'] = 'no bets'
        elif r['bets'] >= 500 and r['sharpe'] == best_sharpe and best_sharpe > 0:
            r['recommendation'] = '⭐ SWEET SPOT (best Sharpe)'
        elif r['min_edge'] <= 3:
            r['recommendation'] = 'Volume play'
        elif r['min_edge'] >= 8:
            r['recommendation'] = 'High quality, low volume'
        elif r['bets'] < 500:
            r['recommendation'] = 'Too few bets'
        else:
            r['recommendation'] = 'Balanced'
    return {'matches_scanned': len(rows), 'scenarios': results}


# ============================================================
# TENNIS BACKTEST — 2-way (no draw), settled by the actual winner.
# Mirrors the football engine: Pinnacle (PSW/PSL) devigged = true prob, bet at
# the best available price (Max -> B365 -> Avg), ONE best-edge bet per match,
# flat ¼-Kelly on the starting bankroll. Source: tennis-data.co.uk (tennis_odds).
# ============================================================
def _won_tennis(selection, winner):
    return selection == winner


def _summarize_full(bets, matches_scanned, matches_with_odds, coverage, bankroll, current_bankroll):
    """Full result payload (same shape/metrics as run_backtest). Groups by each
    bet's 'league' field (-> by_league) and 'market' field (-> by_market), so the
    tennis path can reuse it with league=tour and market=surface."""
    total_bets = len(bets)
    wins = sum(1 for b in bets if b['won'])
    total_staked = sum(b['stake'] for b in bets)
    total_profit = sum(b['profit'] for b in bets)
    roi = total_profit / total_staked * 100 if total_staked > 0 else 0

    returns = [b['profit'] / b['stake'] for b in bets if b['stake'] > 0]
    mean_r = statistics.mean(returns) if returns else 0
    std_r = statistics.stdev(returns) if len(returns) > 1 else 1
    dates = sorted(b['date'] for b in bets if b['date'])
    years = 1.0
    if len(dates) >= 2:
        try:
            from datetime import date as _date
            d0 = _date.fromisoformat(dates[0][:10])
            d1 = _date.fromisoformat(dates[-1][:10])
            years = max((d1 - d0).days / 365.25, 0.5)
        except Exception:
            years = 1.0
    bets_per_year = total_bets / years
    sharpe = (mean_r / std_r) * math.sqrt(bets_per_year) if std_r > 0 else 0

    market_map = {}
    for b in bets:
        mk = b['market']
        market_map.setdefault(mk, {'market': mk, 'bets': 0, 'wins': 0, 'staked': 0.0, 'profit': 0.0})
        market_map[mk]['bets'] += 1
        if b['won']:
            market_map[mk]['wins'] += 1
        market_map[mk]['staked'] += b['stake']
        market_map[mk]['profit'] += b['profit']
    by_market = []
    for d in market_map.values():
        d['win_rate'] = round(d['wins'] / d['bets'] * 100, 1) if d['bets'] else 0
        d['roi'] = round(d['profit'] / d['staked'] * 100, 1) if d['staked'] > 0 else 0
        d['profit'] = round(d['profit'], 2)
        d['staked'] = round(d['staked'], 2)
        by_market.append(d)
    by_market.sort(key=lambda x: x['bets'], reverse=True)

    distinct_matches = len({(b['date'], b['match']) for b in bets})

    league_map = {}
    for b in bets:
        lg = b['league'] or 'Unknown'
        league_map.setdefault(lg, {'league': lg, 'bets': 0, 'wins': 0, 'staked': 0.0, 'profit': 0.0})
        league_map[lg]['bets'] += 1
        if b['won']:
            league_map[lg]['wins'] += 1
        league_map[lg]['staked'] += b['stake']
        league_map[lg]['profit'] += b['profit']
    by_league = []
    for d in league_map.values():
        d['win_rate'] = round(d['wins'] / d['bets'] * 100, 1)
        d['roi'] = round(d['profit'] / d['staked'] * 100, 1) if d['staked'] > 0 else 0
        d['profit'] = round(d['profit'], 2)
        d['staked'] = round(d['staked'], 2)
        by_league.append(d)
    by_league.sort(key=lambda x: x['profit'], reverse=True)

    MAX_POINTS = 400
    cumulative = 0.0
    full_series = []
    for b in bets:
        cumulative += b['profit']
        full_series.append({
            'date': b['date'], 'profit': b['profit'], 'cumulative': round(cumulative, 2),
            'won': b['won'], 'match': b['match'], 'selection': b['selection'],
            'odds': b['odds'], 'edge_pct': b['edge_pct'],
        })
    if len(full_series) <= MAX_POINTS:
        pnl_series = full_series
    else:
        step = len(full_series) / MAX_POINTS
        idxs = sorted({int(i * step) for i in range(MAX_POINTS)} | {len(full_series) - 1})
        pnl_series = [full_series[i] for i in idxs]

    return {
        'summary': {
            'total_bets': total_bets,
            'matches_scanned': matches_scanned,
            'matches_with_odds': matches_with_odds,
            'coverage': coverage,
            'wins': wins,
            'losses': total_bets - wins,
            'win_rate': round(wins / total_bets * 100, 1),
            'total_staked': round(total_staked, 2),
            'total_profit': round(total_profit, 2),
            'roi': round(roi, 2),
            'sharpe': round(sharpe, 2),
            'initial_bankroll': bankroll,
            'final_bankroll': round(current_bankroll, 2),
            'bankroll_growth': round((current_bankroll - bankroll) / bankroll * 100, 1),
            'avg_edge': round(statistics.mean(b['edge_pct'] for b in bets), 2),
            'avg_odds': round(statistics.mean(b['odds'] for b in bets), 2),
            'max_odd': round(max(b['odds'] for b in bets), 2),
            'min_odd': round(min(b['odds'] for b in bets), 2),
            'staking': 'flat (¼-Kelly on starting bankroll, cap 5%)',
            'years': round(years, 1),
            'distinct_matches': distinct_matches,
            'bets_per_match': round(total_bets / distinct_matches, 2) if distinct_matches else 0,
        },
        'bets': bets[-500:],
        'by_league': by_league,
        'by_market': by_market,
        'pnl_series': pnl_series,
    }


def run_tennis_backtest(min_edge=3.0, max_odds=5.0, bankroll=1000.0, kelly_frac=0.25,
                        tour=None, surface=None, market_filter='all', max_kelly_pct=0.05):
    """2-way tennis backtest over tennis_odds. `tour` filters ATP/WTA, `surface`
    filters Hard/Clay/Grass (mapped from the shared league/season UI controls)."""
    conn = get_connection()
    where = ["winner IS NOT NULL"]
    params = []
    if tour:
        where.append("tour = ?")
        params.append(tour)
    if surface:
        where.append("surface = ?")
        params.append(surface)
    rows = conn.execute(f"""
        SELECT date, tour, tournament, surface, winner, loser,
               b365_w, b365_l, ps_w, ps_l, max_w, max_l, avg_w, avg_l
        FROM tennis_odds
        WHERE {' AND '.join(where)}
        ORDER BY date ASC
    """, params).fetchall()
    conn.close()

    matches_scanned = len(rows)
    matches_with_odds = 0
    coverage = {'pinnacle_close': 0, 'b365': 0, 'max': 0, 'avg': 0}
    bets = []
    current_bankroll = bankroll

    for r in rows:
        r = dict(r)
        if r['ps_w']: coverage['pinnacle_close'] += 1
        if r['b365_w']: coverage['b365'] += 1
        if r['max_w']: coverage['max'] += 1
        if r['avg_w']: coverage['avg'] += 1

        # Reference (true prob): Pinnacle is sharpest; fall back to market average.
        if r['ps_w'] and r['ps_l']:
            ref_w, ref_l = r['ps_w'], r['ps_l']
        else:
            ref_w, ref_l = r['avg_w'], r['avg_l']
        true_w, true_l = _devig_2way(ref_w, ref_l)

        # Bet at the best available price.
        bw = r['max_w'] or r['b365_w'] or r['avg_w']
        bl = r['max_l'] or r['b365_l'] or r['avg_l']
        if true_w is None or not bw or not bl:
            continue
        matches_with_odds += 1

        # One best-edge bet per match (back the winner OR the loser side, not both).
        candidates = []
        for sel, true_p, book_odd in [(r['winner'], true_w, bw), (r['loser'], true_l, bl)]:
            if not book_odd or book_odd > max_odds:
                continue
            edge_pct = (true_p * book_odd - 1) * 100
            if edge_pct >= min_edge:
                candidates.append((edge_pct, sel, true_p, book_odd))
        if not candidates:
            continue
        edge_pct, sel, true_p, book_odd = max(candidates, key=lambda x: x[0])
        k = min(_kelly(true_p, book_odd, kelly_frac), max_kelly_pct)
        stake = round(k * bankroll, 2)
        if stake < 0.01:
            continue
        won = _won_tennis(sel, r['winner'])
        profit = round(stake * (book_odd - 1) if won else -stake, 2)
        current_bankroll = round(current_bankroll + profit, 2)
        bets.append({
            'date': r['date'] or '',
            'league': r['tour'] or '',                 # -> by_league table (Tour)
            'season': r['surface'] or '',
            'match': f"{r['winner']} vs {r['loser']}",
            'market': r['surface'] or 'Unknown',       # -> by_market table (Surface)
            'selection': sel,
            'odds': book_odd,
            'edge_pct': round(edge_pct, 2),
            'true_prob': round(true_p * 100, 1),
            'stake': stake,
            'won': won,
            'profit': profit,
            'bankroll': current_bankroll,
        })

    if not bets:
        return {
            'summary': {'total_bets': 0, 'matches_scanned': matches_scanned,
                        'matches_with_odds': matches_with_odds, 'coverage': coverage},
            'bets': [], 'by_league': [], 'by_market': [], 'pnl_series': [],
        }
    return _summarize_full(bets, matches_scanned, matches_with_odds, coverage, bankroll, current_bankroll)


def get_tennis_backtest_meta():
    """Tours + surfaces for the filter dropdowns (mapped onto league/season UI)."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT DISTINCT tour, surface FROM tennis_odds WHERE winner IS NOT NULL
        """).fetchall()
    except Exception:
        rows = []
    conn.close()
    tours = sorted({r['tour'] for r in rows if r['tour']})
    surfaces = sorted({r['surface'] for r in rows if r['surface']})
    return {'leagues': tours, 'seasons': surfaces, 'tours': tours, 'surfaces': surfaces}


def _scenario_bets_tennis(rows, min_edge, max_odds, bankroll, kelly_frac, max_kelly_pct):
    out = []
    for r in rows:
        if r['ps_w'] and r['ps_l']:
            ref_w, ref_l = r['ps_w'], r['ps_l']
        else:
            ref_w, ref_l = r['avg_w'], r['avg_l']
        tw, tl = _devig_2way(ref_w, ref_l)
        bw = r['max_w'] or r['b365_w'] or r['avg_w']
        bl = r['max_l'] or r['b365_l'] or r['avg_l']
        if tw is None or not bw or not bl:
            continue
        cands = []
        for sel, tp, od in [(r['winner'], tw, bw), (r['loser'], tl, bl)]:
            if not od or od > max_odds:
                continue
            e = (tp * od - 1) * 100
            if e >= min_edge:
                cands.append((e, sel, tp, od))
        if not cands:
            continue
        e, sel, tp, od = max(cands, key=lambda x: x[0])
        k = min(_kelly(tp, od, kelly_frac), max_kelly_pct)
        stake = round(k * bankroll, 2)
        if stake >= 0.01:
            won = (sel == r['winner'])
            out.append((stake, round(stake * (od - 1) if won else -stake, 2), won, r['date'] or ''))
    return out


def compare_thresholds_tennis(bankroll=1000.0, kelly_frac=0.25, max_kelly_pct=0.05, scenarios=None):
    """Sweet-spot comparison for tennis (same heuristic as football)."""
    scenarios = scenarios or [(3.0, 5.0), (4.0, 5.0), (5.0, 5.0), (5.0, 4.0), (6.0, 5.0), (8.0, 5.0)]
    conn = get_connection()
    rows = conn.execute("""
        SELECT date, winner, loser, b365_w, b365_l, ps_w, ps_l, max_w, max_l, avg_w, avg_l
        FROM tennis_odds WHERE winner IS NOT NULL ORDER BY date ASC
    """).fetchall()
    conn.close()
    rows = [dict(r) for r in rows]

    results = []
    for (min_edge, max_odds) in scenarios:
        bets = _scenario_bets_tennis(rows, min_edge, max_odds, bankroll, kelly_frac, max_kelly_pct)
        results.append(_summarize_scenario(min_edge, max_odds, bets))

    valid = [r for r in results if r['bets'] >= 500]
    best_sharpe = max((r['sharpe'] for r in valid), default=0)
    for r in results:
        if r['bets'] == 0:
            r['recommendation'] = 'no bets'
        elif r['bets'] >= 500 and r['sharpe'] == best_sharpe and best_sharpe > 0:
            r['recommendation'] = '⭐ SWEET SPOT (best Sharpe)'
        elif r['min_edge'] <= 3:
            r['recommendation'] = 'Volume play'
        elif r['min_edge'] >= 8:
            r['recommendation'] = 'High quality, low volume'
        elif r['bets'] < 500:
            r['recommendation'] = 'Too few bets'
        else:
            r['recommendation'] = 'Balanced'
    return {'matches_scanned': len(rows), 'scenarios': results}


# ============================================================
# CALIBRATION — are our devigged "true probabilities" reliable?
# For every historical match we devig the sharp odds into a true prob per outcome,
# then check how often that outcome ACTUALLY happened. If the engine is calibrated,
# "we said 60%" wins ~60% of the time (points on the diagonal). Validates the
# foundation the whole edge rests on.
# ============================================================
def get_calibration(sport="football"):
    conn = get_connection()
    pairs = []  # (predicted_prob 0-1, occurred 0/1)
    try:
        if sport == "tennis":
            rows = conn.execute("""
                SELECT winner, ps_w, ps_l, avg_w, avg_l
                FROM tennis_odds WHERE winner IS NOT NULL
            """).fetchall()
            for r in rows:
                r = dict(r)
                rw, rl = (r["ps_w"], r["ps_l"]) if (r["ps_w"] and r["ps_l"]) else (r["avg_w"], r["avg_l"])
                tw, tl = _devig_2way(rw, rl)
                if tw is None:
                    continue
                pairs.append((tw, 1))
                pairs.append((tl, 0))
        else:
            rows = conn.execute("""
                SELECT result, pinnacle_home_close, pinnacle_draw_close, pinnacle_away_close,
                       avg_home, avg_draw, avg_away
                FROM football_matches WHERE result IS NOT NULL
            """).fetchall()
            for r in rows:
                r = dict(r)
                if r["pinnacle_home_close"] and r["pinnacle_draw_close"] and r["pinnacle_away_close"]:
                    rh, rd, ra = r["pinnacle_home_close"], r["pinnacle_draw_close"], r["pinnacle_away_close"]
                else:
                    rh, rd, ra = r["avg_home"], r["avg_draw"], r["avg_away"]
                th, td, ta = _devig_3way(rh, rd, ra)
                if th is None:
                    continue
                res = r["result"]
                pairs.append((th, 1 if res == "H" else 0))
                pairs.append((td, 1 if res == "D" else 0))
                pairs.append((ta, 1 if res == "A" else 0))
    finally:
        conn.close()

    buckets = []
    for lo in range(0, 100, 10):
        hi = lo + 10
        sel = [(p, o) for (p, o) in pairs if (lo / 100.0) <= p < (hi / 100.0)]
        if not sel:
            buckets.append({"bucket": f"{lo}-{hi}%", "n": 0, "predicted": None, "actual": None, "diff": None})
            continue
        n = len(sel)
        pred = sum(p for p, _ in sel) / n * 100
        act = sum(o for _, o in sel) / n * 100
        buckets.append({"bucket": f"{lo}-{hi}%", "n": n,
                        "predicted": round(pred, 1), "actual": round(act, 1),
                        "diff": round(act - pred, 1)})
    total = sum(b["n"] for b in buckets)
    mae = (round(sum(abs(b["diff"]) * b["n"] for b in buckets if b["n"]) / total, 2)
           if total else None)
    return {"buckets": buckets, "total": total, "mae_pp": mae}
