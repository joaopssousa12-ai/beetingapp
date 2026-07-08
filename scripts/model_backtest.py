"""Pure-model prediction backtest (NO edge, NO CLV) — validates whether the
model's *confidence* is real before we ever show a "prognostic" / "safest pick"
to the user.

This is the honesty gate for the match-analysis page. It measures, walk-forward
with ZERO lookahead, how well three independent signals predict football
outcomes on the free football-data.co.uk history (2021-26, ~16 leagues):

  1. Elo          — rating model, updated after each match (mirrors the live
                    elo_based_probability draw model: 0.32*exp(-gap/300)).
  2. Poisson      — rolling attack/defence strength from ACTUAL goals (the same
                    Poisson-grid family as the live xG model, but fed by the
                    goals we actually have in history).
  3. Market       — Pinnacle CLOSING odds, devigged (the ceiling any model is
                    judged against — it is NOT beatable for profit here, that is
                    the whole point of the separate edge/CLV system).

Metrics per model: 1X2 accuracy, favourite hit-rate, multiclass Brier score and
log-loss (calibration), a calibration table (predicted vs realised), and — the
question that actually decides the "múltipla do dia" / confidence feature —
whether HIGH-CONFIDENCE and MODEL-AGREEMENT picks really do win more often.

No product change, no deploy, no Odds-API credits. Run via the model-lab
workflow (needs football-data.co.uk access only).
"""
import os
import sys
import math
import statistics
from collections import defaultdict, deque

os.environ.setdefault("RAILWAY_VOLUME_MOUNT_PATH", "/tmp/mdllab")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import database as db
from collectors.footballdata import collect_footballdata

# ---- knobs (kept close to the live models so we measure the REAL thing) ----
ELO_K = 24
ELO_HOME_ADV = 65          # rating points (matches elo_based_probability default)
BURN_IN = 6                # skip a team's first N matches (unstable ratings/rolls)
ROLL_N = 10                # rolling window for the Poisson goals model
MAX_GOALS = 8              # Poisson grid ceiling


# ---------------------------------------------------------------- devig
def devig_3way(h, d, a):
    if not (h and d and a and h > 1 and d > 1 and a > 1):
        return None
    m = 1 / h + 1 / d + 1 / a
    return [(1 / h) / m, (1 / d) / m, (1 / a) / m]


def devig_2way(o, u):
    if not (o and u and o > 1 and u > 1):
        return None
    m = 1 / o + 1 / u
    return (1 / o) / m


# ---------------------------------------------------------------- Elo
def elo_expected(rh, ra):
    """Home win prob (pre-draw-split) from Elo, home advantage baked in."""
    return 1.0 / (1.0 + 10 ** ((ra - (rh + ELO_HOME_ADV)) / 400))


def elo_1x2(rh, ra):
    p_home_raw = elo_expected(rh, ra)
    gap = abs((rh + ELO_HOME_ADV) - ra)
    draw = max(0.10, min(0.35, 0.32 * math.exp(-gap / 300)))
    non_draw = 1 - draw
    return [p_home_raw * non_draw, draw, (1 - p_home_raw) * non_draw]


# ---------------------------------------------------------------- Poisson
def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def poisson_1x2_ou(lam_h, lam_a):
    lam_h = max(0.2, min(4.5, lam_h))
    lam_a = max(0.2, min(4.5, lam_a))
    ph = [poisson_pmf(k, lam_h) for k in range(MAX_GOALS + 1)]
    pa = [poisson_pmf(k, lam_a) for k in range(MAX_GOALS + 1)]
    p_home = p_draw = p_away = p_over = 0.0
    for h in range(MAX_GOALS + 1):
        for a in range(MAX_GOALS + 1):
            p = ph[h] * pa[a]
            if h > a:
                p_home += p
            elif h == a:
                p_draw += p
            else:
                p_away += p
            if h + a >= 3:
                p_over += p
    return [p_home, p_draw, p_away], p_over


# ---------------------------------------------------------------- metrics
class Scorer:
    """Accumulates accuracy + Brier + log-loss + calibration for one model."""
    def __init__(self, name):
        self.name = name
        self.n = 0
        self.correct = 0
        self.brier = 0.0
        self.logloss = 0.0
        self.fav_n = 0
        self.fav_correct = 0
        # calibration: bucket the picked (argmax) prob into deciles
        self.cal = defaultdict(lambda: [0, 0.0, 0])   # bin -> [hits, prob_sum, n]

    def add(self, probs, actual_idx):
        # probs: [p_home, p_draw, p_away]; actual_idx in {0,1,2}
        self.n += 1
        pred = max(range(3), key=lambda i: probs[i])
        if pred == actual_idx:
            self.correct += 1
        # multiclass Brier + logloss
        for i in range(3):
            y = 1.0 if i == actual_idx else 0.0
            self.brier += (probs[i] - y) ** 2
        p_true = max(1e-9, min(1 - 1e-9, probs[actual_idx]))
        self.logloss += -math.log(p_true)
        # favourite = argmax
        self.fav_n += 1
        if pred == actual_idx:
            self.fav_correct += 1
        b = min(9, int(probs[pred] * 10))
        c = self.cal[b]
        c[0] += 1 if pred == actual_idx else 0
        c[1] += probs[pred]
        c[2] += 1

    def report(self):
        if self.n == 0:
            print(f"  {self.name}: no predictions"); return
        print(f"  {self.name:8} n={self.n:5}  acc={self.correct/self.n*100:5.1f}%  "
              f"Brier={self.brier/self.n:.4f}  logloss={self.logloss/self.n:.4f}")

    def calibration(self):
        print(f"  --- calibration ({self.name}): picked-prob bin -> realised ---")
        for b in sorted(self.cal):
            hits, psum, n = self.cal[b]
            if n < 30:
                continue
            print(f"    p~{b*10:2d}-{b*10+10:2d}%  predicted {psum/n*100:5.1f}%  "
                  f"realised {hits/n*100:5.1f}%  (n={n})")


class BinScorer:
    """Accuracy + Brier for a 2-way market (O/U 2.5)."""
    def __init__(self, name):
        self.name = name; self.n = 0; self.correct = 0; self.brier = 0.0

    def add(self, p_over, went_over):
        self.n += 1
        pred_over = p_over >= 0.5
        if pred_over == went_over:
            self.correct += 1
        y = 1.0 if went_over else 0.0
        self.brier += (p_over - y) ** 2

    def report(self):
        if self.n == 0:
            print(f"  {self.name}: no predictions"); return
        print(f"  {self.name:14} n={self.n:5}  acc={self.correct/self.n*100:5.1f}%  "
              f"Brier={self.brier/self.n:.4f}")


def main():
    db.init_db()
    print("Fetching football-data.co.uk history...", flush=True)
    collect_footballdata()

    conn = db.get_connection()
    rows = [dict(r) for r in conn.execute("""
        SELECT date, league_name, season, home_team, away_team,
               home_goals, away_goals, result,
               pinnacle_home_close, pinnacle_draw_close, pinnacle_away_close,
               over25_pinnacle, under25_pinnacle
        FROM football_matches
        WHERE result IS NOT NULL AND home_goals IS NOT NULL AND away_goals IS NOT NULL
        ORDER BY date ASC, league_name ASC
    """).fetchall()]
    conn.close()
    print(f"matches with results: {len(rows)}", flush=True)

    RES_IDX = {"H": 0, "D": 1, "A": 2}

    # walk-forward state
    elo = defaultdict(lambda: 1500.0)
    played = defaultdict(int)
    gf = defaultdict(lambda: deque(maxlen=ROLL_N))   # goals for
    ga = defaultdict(lambda: deque(maxlen=ROLL_N))   # goals against
    # league baselines (rolling mean home/away goals) per league
    lg_home = defaultdict(lambda: deque(maxlen=400))
    lg_away = defaultdict(lambda: deque(maxlen=400))

    s_elo = Scorer("Elo")
    s_poi = Scorer("Poisson")
    s_mkt = Scorer("Market")
    ou_poi = BinScorer("Poisson O/U2.5")
    ou_mkt = BinScorer("Market O/U2.5")

    # agreement / confidence buckets (Elo vs Poisson, and confidence tiers)
    agree_hit = [0, 0]      # [hits, n] where Elo-argmax == Poisson-argmax
    disagree_hit = [0, 0]
    triple_hit = [0, 0]     # Elo == Poisson == Market
    # confidence tiers on the MODEL (avg of Elo+Poisson) top prob
    conf_tiers = {"50-60": [0, 0], "60-70": [0, 0], "70-80": [0, 0], "80+": [0, 0]}

    for r in rows:
        h, a = r["home_team"], r["away_team"]
        res = r["result"]
        if res not in RES_IDX:
            continue
        actual = RES_IDX[res]
        lg = r["league_name"]

        rh, ra = elo[h], elo[a]
        ready = played[h] >= BURN_IN and played[a] >= BURN_IN and len(gf[h]) and len(gf[a])

        if ready:
            # ---- Elo ----
            p_elo = elo_1x2(rh, ra)
            # ---- Poisson (rolling attack/defence, league-relative) ----
            lh_base = (statistics.mean(lg_home[lg]) if lg_home[lg] else 1.45)
            la_base = (statistics.mean(lg_away[lg]) if lg_away[lg] else 1.15)
            atk_h = (statistics.mean(gf[h]) / lh_base) if lh_base else 1.0
            def_a = (statistics.mean(ga[a]) / lh_base) if lh_base else 1.0
            atk_a = (statistics.mean(gf[a]) / la_base) if la_base else 1.0
            def_h = (statistics.mean(ga[h]) / la_base) if la_base else 1.0
            lam_h = atk_h * def_a * lh_base
            lam_a = atk_a * def_h * la_base
            p_poi, p_over = poisson_1x2_ou(lam_h, lam_a)
            # ---- Market ----
            p_mkt = devig_3way(r["pinnacle_home_close"], r["pinnacle_draw_close"],
                               r["pinnacle_away_close"])

            s_elo.add(p_elo, actual)
            s_poi.add(p_poi, actual)
            if p_mkt:
                s_mkt.add(p_mkt, actual)

            # O/U 2.5
            went_over = (r["home_goals"] + r["away_goals"]) >= 3
            ou_poi.add(p_over, went_over)
            pm_over = devig_2way(r["over25_pinnacle"], r["under25_pinnacle"])
            if pm_over is not None:
                ou_mkt.add(pm_over, went_over)

            # agreement / confidence
            elo_arg = max(range(3), key=lambda i: p_elo[i])
            poi_arg = max(range(3), key=lambda i: p_poi[i])
            hit = 1 if elo_arg == actual else 0
            if elo_arg == poi_arg:
                agree_hit[0] += hit; agree_hit[1] += 1
                if p_mkt and max(range(3), key=lambda i: p_mkt[i]) == elo_arg:
                    triple_hit[0] += hit; triple_hit[1] += 1
            else:
                # when they disagree, score whether the Poisson pick won (either way)
                disagree_hit[0] += (1 if poi_arg == actual else 0); disagree_hit[1] += 1
            # model-consensus confidence = avg of the two models on the agreed pick
            if elo_arg == poi_arg:
                conf = (p_elo[elo_arg] + p_poi[poi_arg]) / 2
                tier = "80+" if conf >= 0.80 else "70-80" if conf >= 0.70 else \
                       "60-70" if conf >= 0.60 else "50-60" if conf >= 0.50 else None
                if tier:
                    conf_tiers[tier][0] += hit; conf_tiers[tier][1] += 1

        # ---- update state AFTER predicting (no lookahead) ----
        hg, ag = r["home_goals"], r["away_goals"]
        exp_h = elo_expected(rh, ra)
        score_h = 1.0 if res == "H" else 0.5 if res == "D" else 0.0
        elo[h] = rh + ELO_K * (score_h - exp_h)
        elo[a] = ra + ELO_K * ((1 - score_h) - (1 - exp_h))
        gf[h].append(hg); ga[h].append(ag)
        gf[a].append(ag); ga[a].append(hg)
        lg_home[lg].append(hg); lg_away[lg].append(ag)
        played[h] += 1; played[a] += 1

    print("\n===== 1X2 PREDICTION QUALITY (walk-forward, no lookahead) =====")
    print("  (favourite-argmax accuracy; lower Brier/logloss = better calibrated)")
    s_mkt.report(); s_elo.report(); s_poi.report()
    print("\n===== O/U 2.5 =====")
    ou_mkt.report(); ou_poi.report()

    print("\n===== CALIBRATION (does the confidence mean anything?) =====")
    s_elo.calibration()
    s_poi.calibration()

    print("\n===== THE MÚLTIPLA / SAFEST-PICK QUESTION =====")
    def pct(x):
        return f"{x[0]/x[1]*100:5.1f}% (n={x[1]})" if x[1] else "n/a"
    print(f"  Elo & Poisson AGREE on the pick   -> hit {pct(agree_hit)}")
    print(f"  Elo & Poisson DISAGREE (Poisson)  -> hit {pct(disagree_hit)}")
    print(f"  Elo & Poisson & MARKET all agree  -> hit {pct(triple_hit)}")
    print("  --- when the two models agree, by consensus confidence ---")
    for t in ["50-60", "60-70", "70-80", "80+"]:
        print(f"    conf {t:6}% -> hit {pct(conf_tiers[t])}")
    # what a 3-fold of the top tier looks like if picks were independent
    top = conf_tiers["70-80"]; top2 = conf_tiers["80+"]
    merged = [top[0] + top2[0], top[1] + top2[1]]
    if merged[1]:
        p = merged[0] / merged[1]
        print(f"\n  A 3-fold of independent '70%+' consensus picks would land all-3 "
              f"~{p**3*100:.0f}% of the time (single-leg realised {p*100:.1f}%).")
        print("  Reminder: an accumulator MULTIPLIES the bookmaker margin — it is -EV "
              "by construction. This feature is 'for fun', never part of the edge system.")


if __name__ == "__main__":
    main()
