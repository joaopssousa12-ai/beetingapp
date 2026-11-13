#!/usr/bin/env python3
"""Is the prognosis page's method the best one the data supports?

The page shows, as its headline probability, the 50/50 average of two things:
the devigged Pinnacle price ("mercado") and our own Elo. The Múltipla do Dia
ranks legs by that same number. This script measures, walk-forward on several
seasons of real results, whether that average is better or worse than each of
its two halves — and how honest the confidence it prints really is.

Everything is computed the way the app computes it:
  Elo      collectors/elo.py            K=20 x margin multiplier, home +65
  1X2      database.elo_based_probability (draw = 0.32*exp(-gap/300), 10-35%)
  goals    database.get_match_prognosis  last 8 matches, lam_h=(gf_h+ga_a)/2*1.10
  market   power devig of Pinnacle 1X2 (closing; the page uses the live line)

A prediction is always made BEFORE the match updates anything. Read-only: it
downloads football-data.co.uk CSVs and touches no BetIQ endpoint.
"""
import csv
import io
import json
import math
import sys
import urllib.request
from collections import defaultdict, deque
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"}
DIVS = ["E0", "E1", "SP1", "SP2", "D1", "D2", "I1", "I2", "F1", "F2", "N1", "P1", "B1", "T1", "G1", "SC0"]
N_SEASONS = 7          # the first two only warm the ratings up
BASE, K, HOME_ADV = 1500, 20, 65


def fetch(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
            return r.read().decode("utf-8-sig", errors="replace")
    except Exception as e:                            # noqa: BLE001
        print(f"  (sem {url.rsplit('/', 2)[-2]}/{url.rsplit('/', 1)[-1]}: {type(e).__name__})")
        return None


def season_codes():
    now = datetime.utcnow()
    y = now.year if now.month >= 7 else now.year - 1
    return [f"{str(y - i)[2:]}{str(y - i + 1)[2:]}" for i in range(N_SEASONS)][::-1]


def fnum(r, *keys):
    for k in keys:
        v = (r.get(k) or "").strip()
        if v:
            try:
                x = float(v)
                if x > 1:
                    return x
            except ValueError:
                pass
    return None


def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def load():
    out = []
    for ss in season_codes():
        for div in DIVS:
            text = fetch(f"https://www.football-data.co.uk/mmz4281/{ss}/{div}.csv")
            if not text:
                continue
            for r in csv.DictReader(io.StringIO(text)):
                d = parse_date(r.get("Date"))
                h, a = (r.get("HomeTeam") or "").strip(), (r.get("AwayTeam") or "").strip()
                ftr = (r.get("FTR") or "").strip().upper()
                try:
                    hg, ag = int(float(r.get("FTHG"))), int(float(r.get("FTAG")))
                except (TypeError, ValueError):
                    continue
                if not d or not h or not a or ftr not in ("H", "D", "A"):
                    continue
                out.append({
                    "date": d, "season": ss, "div": div, "h": h, "a": a, "hg": hg, "ag": ag,
                    "res": {"H": "home", "D": "draw", "A": "away"}[ftr],
                    "pin_close": (fnum(r, "PSCH"), fnum(r, "PSCD"), fnum(r, "PSCA")),
                    "pin_open": (fnum(r, "PSH"), fnum(r, "PSD"), fnum(r, "PSA")),
                    "max": (fnum(r, "MaxCH", "MaxH"), fnum(r, "MaxCD", "MaxD"), fnum(r, "MaxCA", "MaxA")),
                    "avg": (fnum(r, "AvgCH", "AvgH"), fnum(r, "AvgCD", "AvgD"), fnum(r, "AvgCA", "AvgA")),
                    "ou_pin": (fnum(r, "PC>2.5", "P>2.5"), fnum(r, "PC<2.5", "P<2.5")),
                })
    out.sort(key=lambda m: m["date"])
    return out


def devig_power(odds):
    """Same power method as database.remove_vig_power -> probabilities or None."""
    if not all(odds):
        return None
    imp = [1.0 / o for o in odds]
    if sum(imp) < 0.94:
        return None
    if sum(imp) <= 1.0:
        s = sum(imp)
        return [p / s for p in imp]
    lo, hi = 0.2, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if sum(p ** (1.0 / mid) for p in imp) > 1.0:
            hi = mid
        else:
            lo = mid
    k = (lo + hi) / 2
    pr = [p ** (1.0 / k) for p in imp]
    s = sum(pr)
    return [p / s for p in pr]


def margin_mult(gd):
    return 1.0 if gd <= 1 else 1.5 if gd == 2 else (11 + gd) / 8.0


def elo_probs(rh, ra):
    p_home_raw = 1.0 / (1.0 + 10 ** ((ra - (rh + HOME_ADV)) / 400))
    gap = abs((rh + HOME_ADV) - ra)
    draw = max(0.10, min(0.35, 0.32 * math.exp(-gap / 300)))
    return [p_home_raw * (1 - draw), draw, (1 - p_home_raw) * (1 - draw)]


def pois(k, lam):
    return lam ** k * math.exp(-lam) / math.factorial(k)


def poisson_probs(lam_h, lam_a):
    lam_h, lam_a = max(0.2, min(4.5, lam_h)), max(0.2, min(4.5, lam_a))
    ph, pa = [pois(k, lam_h) for k in range(9)], [pois(k, lam_a) for k in range(9)]
    home = draw = away = over = 0.0
    for i in range(9):
        for j in range(9):
            p = ph[i] * pa[j]
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
            if i + j >= 3:
                over += p
    return [home, draw, away], over


OUT = ("home", "draw", "away")


class Meter:
    def __init__(self):
        self.n = self.hit = 0
        self.ll = self.brier = 0.0

    def add(self, probs, res):
        i = OUT.index(res)
        self.n += 1
        self.hit += int(max(range(3), key=lambda k: probs[k]) == i)
        self.ll += -math.log(max(probs[i], 1e-9))
        self.brier += sum((probs[k] - (1.0 if k == i else 0.0)) ** 2 for k in range(3))

    def row(self, name):
        if not self.n:
            return f"  {name:<34} n=0"
        return (f"  {name:<34} n={self.n:<6} acerto {100 * self.hit / self.n:5.1f}%   "
                f"log-loss {self.ll / self.n:.4f}   Brier {self.brier / self.n:.4f}")


BINS = [(0, 45), (45, 50), (50, 55), (55, 60), (60, 65), (65, 70), (70, 75), (75, 80), (80, 101)]


def calib_table(title, rows):
    """rows: list of (predicted top prob %, hit 0/1)."""
    print(f"\n  {title}")
    print("    confianca dita | n      | media dita | acertou de facto | diferenca")
    for lo, hi in BINS:
        sub = [(p, h) for p, h in rows if lo <= p < hi]
        if len(sub) < 30:
            continue
        said = sum(p for p, _ in sub) / len(sub)
        real = 100.0 * sum(h for _, h in sub) / len(sub)
        print(f"    {lo:>3}-{hi if hi < 101 else 100:<3}%        | {len(sub):<6} | {said:6.1f}%    | "
              f"{real:6.1f}%          | {real - said:+5.1f} pp")


def main():
    print("A descarregar resultados (football-data.co.uk)...")
    ms = load()
    if len(ms) < 5000:
        print(f"Poucos dados ({len(ms)} jogos) — abortado.")
        return 1
    seasons = season_codes()
    eval_seasons = set(seasons[2:])
    print(f"{len(ms)} jogos, epocas {seasons[0]}..{seasons[-1]}; avaliadas: {sorted(eval_seasons)}\n")

    rating, games = defaultdict(lambda: BASE), defaultdict(int)
    last8 = defaultdict(lambda: deque(maxlen=8))             # (gf, ga) per team
    M = {k: Meter() for k in ("market_close", "market_open", "elo", "blend50_close", "blend50_open",
                              "poisson")}
    W = {w: Meter() for w in (0.0, 0.25, 0.5, 0.75, 0.9, 1.0)}   # weight of the MARKET in the blend
    cal_mkt, cal_blend, cal_elo = [], [], []
    tiers = defaultdict(lambda: [0, 0])
    legs_app, legs_mkt = [], []          # dicts for the accumulator study
    ou_model, ou_mkt = [0, 0], [0, 0]
    ou_cal = []
    by_day = defaultdict(list)

    for m in ms:
        h, a = m["h"], m["a"]
        rh, ra = rating[h], rating[a]
        warm = games[h] >= 20 and games[a] >= 20
        pe = elo_probs(rh, ra)
        pm_c, pm_o = devig_power(m["pin_close"]), devig_power(m["pin_open"])
        pp = over_p = None
        if len(last8[h]) >= 5 and len(last8[a]) >= 5:
            gfh = sum(x[0] for x in last8[h]) / len(last8[h]); gah = sum(x[1] for x in last8[h]) / len(last8[h])
            gfa = sum(x[0] for x in last8[a]) / len(last8[a]); gaa = sum(x[1] for x in last8[a]) / len(last8[a])
            pp, over_p = poisson_probs((gfh + gaa) / 2 * 1.10, (gfa + gah) / 2 * 0.95)

        if m["season"] in eval_seasons and warm:
            res = m["res"]
            ri = OUT.index(res)
            M["elo"].add(pe, res)
            if pp:
                M["poisson"].add(pp, res)
            if pm_o:
                M["market_open"].add(pm_o, res)
                M["blend50_open"].add([(x + y) / 2 for x, y in zip(pe, pm_o)], res)
            if pm_c:
                M["market_close"].add(pm_c, res)
                blend = [(x + y) / 2 for x, y in zip(pe, pm_c)]
                M["blend50_close"].add(blend, res)
                for w, meter in W.items():
                    meter.add([w * y + (1 - w) * x for x, y in zip(pe, pm_c)], res)
                tm, tb, te = (max(range(3), key=lambda k: p[k]) for p in (pm_c, blend, pe))
                cal_mkt.append((100 * pm_c[tm], int(tm == ri)))
                cal_blend.append((100 * blend[tb], int(tb == ri)))
                cal_elo.append((100 * pe[te], int(te == ri)))
                # the page's tiers
                conf, agree = 100 * blend[tb], (te == tm)
                tier = ("Alta" if conf >= 70 and agree else "Media-alta" if conf >= 60 and agree
                        else "Media" if conf >= 50 else "Baixa")
                tiers[tier][0] += 1; tiers[tier][1] += int(tb == ri)
                # Múltipla legs — the app's rule and a market-only rule
                p_agree = None if not pp else (max(range(3), key=lambda k: pp[k]) == tb)
                odd_max = m["max"][tb] if m["max"][tb] else None
                odd_avg = m["avg"][tb] if m["avg"][tb] else None
                if conf >= 65 and p_agree is not False:
                    leg = {"date": m["date"], "conf": conf, "hit": int(tb == ri), "max": odd_max, "avg": odd_avg,
                           "dc_conf": min(99.0, conf + 100 * blend[1]) if tb != 1 else None,
                           "dc_hit": int(ri in (tb, 1)) if tb != 1 else None}
                    legs_app.append(leg); by_day[("app", m["date"].date())].append(leg)
                if 100 * pm_c[tm] >= 65:
                    leg = {"date": m["date"], "conf": 100 * pm_c[tm], "hit": int(tm == ri),
                           "max": m["max"][tm], "avg": m["avg"][tm]}
                    legs_mkt.append(leg); by_day[("mkt", m["date"].date())].append(leg)
            # goals: O/U 2.5
            if over_p is not None:
                went_over = (m["hg"] + m["ag"]) >= 3
                ou_model[0] += 1; ou_model[1] += int((over_p >= 0.5) == went_over)
                ou_cal.append((100 * max(over_p, 1 - over_p), int((over_p >= 0.5) == went_over)))
                if all(m["ou_pin"]):
                    io_, iu = 1 / m["ou_pin"][0], 1 / m["ou_pin"][1]
                    mk_over = io_ / (io_ + iu)
                    ou_mkt[0] += 1; ou_mkt[1] += int((mk_over >= 0.5) == went_over)

        # ── update AFTER predicting ──
        exp_h = 1.0 / (1.0 + 10 ** ((ra - (rh + HOME_ADV)) / 400))
        sh = 1.0 if m["hg"] > m["ag"] else 0.0 if m["hg"] < m["ag"] else 0.5
        k = K * margin_mult(abs(m["hg"] - m["ag"]))
        rating[h] = rh + k * (sh - exp_h)
        rating[a] = ra + k * ((1 - sh) - (1 - exp_h))
        games[h] += 1; games[a] += 1
        last8[h].append((m["hg"], m["ag"])); last8[a].append((m["ag"], m["hg"]))

    bar = "=" * 78
    print(bar + "\n1. QUEM PREVE MELHOR O 1X2?  (menor log-loss / Brier = melhor)\n" + bar)
    print(M["market_close"].row("mercado (Pinnacle fecho, devig)"))
    print(M["market_open"].row("mercado (Pinnacle pre-jogo, devig)"))
    print(M["blend50_close"].row("PAGINA HOJE: 50% mercado + 50% Elo"))
    print(M["elo"].row("so' Elo"))
    print(M["poisson"].row("so' modelo de golos (Poisson)"))
    print("\n  peso do mercado na mistura (resto = Elo), contra o fecho da Pinnacle:")
    for w, meter in W.items():
        print(meter.row(f"   mercado {int(w * 100):>3}% / Elo {int((1 - w) * 100):>3}%"))

    print("\n" + bar + "\n2. A CONFIANCA QUE A PAGINA MOSTRA E' HONESTA?\n" + bar)
    calib_table("mistura 50/50 (o numero que a pagina mostra hoje)", cal_blend)
    calib_table("so' mercado", cal_mkt)
    calib_table("so' Elo", cal_elo)
    print("\n  escaloes da pagina (calculados como a app):")
    for t in ("Alta", "Media-alta", "Media", "Baixa"):
        n, hit = tiers[t]
        if n:
            print(f"    {t:<11} n={n:<6} acertou {100 * hit / n:5.1f}%")

    print("\n" + bar + "\n3. MULTIPLA DO DIA\n" + bar)

    def legs_report(name, legs):
        if not legs:
            print(f"  {name}: sem pernas"); return
        n = len(legs)
        hit = sum(l["hit"] for l in legs)
        said = sum(l["conf"] for l in legs) / n
        print(f"  {name}: {n} pernas | confianca media dita {said:.1f}% | acertaram {100 * hit / n:.1f}%")
        for key, lab in (("avg", "odd media do mercado"), ("max", "melhor odd do mercado")):
            sub = [l for l in legs if l.get(key)]
            if sub:
                roi = sum((l[key] - 1) if l["hit"] else -1 for l in sub) / len(sub)
                print(f"      aposta simples a 1u na {lab}: ROI {100 * roi:+.1f}%  (odd media {sum(l[key] for l in sub) / len(sub):.2f}, n={len(sub)})")
    legs_report("regra da app (mistura >=65% e golos nao discordam)", legs_app)
    legs_report("regra so' mercado (>=65%)", legs_mkt)
    dc = [l for l in legs_app if l.get("dc_conf") is not None]
    if dc:
        print(f"  dupla hipotese (favorito ou empate): {len(dc)} pernas | dita {sum(l['dc_conf'] for l in dc) / len(dc):.1f}% "
              f"| acertaram {100 * sum(l['dc_hit'] for l in dc) / len(dc):.1f}%")

    def accas(tag, size):
        days = [v for (t, _), v in by_day.items() if t == tag and len(v) >= size]
        if not days:
            return
        n = won = 0
        said = 0.0
        ret_max = ret_avg = 0.0
        n_max = n_avg = 0
        for legs in days:
            top = sorted(legs, key=lambda l: -l["conf"])[:size]
            n += 1
            allhit = all(l["hit"] for l in top)
            won += int(allhit)
            p = 1.0
            for l in top:
                p *= l["conf"] / 100.0
            said += p
            for key in ("max", "avg"):
                if all(l.get(key) for l in top):
                    o = 1.0
                    for l in top:
                        o *= l[key]
                    if key == "max":
                        n_max += 1; ret_max += (o - 1) if allhit else -1
                    else:
                        n_avg += 1; ret_avg += (o - 1) if allhit else -1
        line = (f"    {size} pernas: {n} dias | prob. dita de acertar tudo {100 * said / n:.1f}% | "
                f"acertou tudo {100 * won / n:.1f}%")
        if n_avg:
            line += f" | ROI odd media {100 * ret_avg / n_avg:+.1f}%"
        if n_max:
            line += f" | ROI melhor odd {100 * ret_max / n_max:+.1f}%"
        print(line)
    for tag, lab in (("app", "regra da app"), ("mkt", "regra so' mercado")):
        print(f"\n  multipla diaria com as pernas mais confiantes do dia — {lab}:")
        for size in (2, 3, 4, 5):
            accas(tag, size)

    print("\n" + bar + "\n4. GOLOS (Over/Under 2.5) — o modelo auxiliar\n" + bar)
    if ou_model[0]:
        print(f"  modelo de golos (ultimos 8 jogos): acerta o lado {100 * ou_model[1] / ou_model[0]:.1f}%  (n={ou_model[0]})")
    if ou_mkt[0]:
        print(f"  mercado (Pinnacle O/U):            acerta o lado {100 * ou_mkt[1] / ou_mkt[0]:.1f}%  (n={ou_mkt[0]})")
    calib_table("modelo de golos — confianca dita no lado escolhido", ou_cal)

    summary = {
        "n": M["market_close"].n,
        "logloss": {k: round(v.ll / v.n, 4) for k, v in M.items() if v.n},
        "accuracy": {k: round(100 * v.hit / v.n, 2) for k, v in M.items() if v.n},
        "blend_weights_logloss": {str(w): round(v.ll / v.n, 4) for w, v in W.items() if v.n},
    }
    print("\nPROGNOSIS_BACKTEST_JSON " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
