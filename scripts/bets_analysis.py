#!/usr/bin/env python3
"""Deep statistical read of the real bet log (production /api/bets).

Read-only. Answers the questions the dashboard headline cannot:
  - how much of the realised ROI is edge and how much is variance
  - whether CLV (the leading indicator) agrees with the model's edge estimate
  - where the money actually comes from: odds band, sport, market, edge band
  - whether stake sizing is doing its job
  - how many bets are still needed before ROI means anything

Usage: python3 scripts/bets_analysis.py [url]
"""
import json
import math
import random
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime


def _parse_iso(s):
    """Parse the mixed timestamp shapes the DB stores ('YYYY-MM-DDTHH:MM',
    'YYYY-MM-DD HH:MM:SS', optional trailing Z). Returns None on anything else."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None

URL = sys.argv[1] if len(sys.argv) > 1 else "https://beetingapp-1.onrender.com/api/bets?limit=500"
random.seed(7)


def fetch():
    with urllib.request.urlopen(URL, timeout=120) as r:
        return json.load(r)


def pct(x, nd=2):
    return f"{x:+.{nd}f}%"


def h(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def bootstrap_roi(returns, stakes, n=20000):
    """Resample bets with replacement; ROI = sum(profit)/sum(stake)."""
    idx = range(len(returns))
    out = []
    for _ in range(n):
        s = [random.choice(idx) for _ in idx]
        st = sum(stakes[i] for i in s)
        if st > 0:
            out.append(sum(returns[i] for i in s) / st * 100)
    out.sort()
    lo = out[int(0.025 * len(out))]
    hi = out[int(0.975 * len(out))]
    p_le_0 = sum(1 for v in out if v <= 0) / len(out)
    return lo, hi, p_le_0


def table(rows, headers):
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
              for i, h in enumerate(headers)]
    print("  " + " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)))
    print("  " + "-+-".join("-" * w for w in widths))
    for r in rows:
        print("  " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))


def group_report(bets, keyfn, label, min_n=1):
    groups = defaultdict(list)
    for b in bets:
        groups[keyfn(b)].append(b)
    rows = []
    for k, g in sorted(groups.items(), key=lambda kv: -sum(x["stake"] for x in kv[1])):
        if len(g) < min_n:
            continue
        st = sum(x["stake"] for x in g)
        pf = sum(x["profit"] for x in g)
        w = sum(1 for x in g if x["result"] == "won")
        settled = [x for x in g if x["result"] in ("won", "lost")]
        avg_odd = sum(x["odds"] for x in g) / len(g)
        edges = [x["edge_pct"] for x in g if x.get("edge_pct") is not None]
        # "_clv_any" = countable CLV where we have one, otherwise the unverified
        # figure, so the breakdown columns stay populated. Marked CLV* in the
        # header precisely because it mixes the two.
        clvs = [x["_clv_any"] for x in g
                if x.get("_clv_any") is not None] or \
               [x["clv_pct"] for x in g if x.get("clv_pct") is not None]
        rows.append([
            str(k)[:28], len(g), f"{st:.0f}", f"{pf:+.2f}",
            pct(pf / st * 100, 1) if st else "—",
            f"{w}/{len(settled)}" if settled else "—",
            f"{w / len(settled) * 100:.0f}%" if settled else "—",
            f"{avg_odd:.2f}",
            f"{100 / avg_odd:.0f}%",
            pct(sum(edges) / len(edges), 1) if edges else "—",
            pct(sum(clvs) / len(clvs), 1) if clvs else "—",
        ])
    print(f"\n{label}")
    table(rows, ["grupo", "n", "staked", "P/L", "ROI", "W/S", "win%", "odd méd",
                 "break-even", "edge méd", "CLV* méd"])


def main():
    raw = fetch()
    print(f"Fonte: {URL}\nApostas recebidas: {len(raw)}")

    bets = []
    for b in raw:
        try:
            b["odds"] = float(b["odds"])
            b["stake"] = float(b["stake"])
            b["profit"] = float(b.get("profit") or 0)
        except (TypeError, ValueError):
            continue
        bets.append(b)

    settled = [b for b in bets if b.get("result") in ("won", "lost", "push")]
    decided = [b for b in settled if b["result"] in ("won", "lost")]
    pending = [b for b in bets if b not in settled]

    # ── 1. Headline, recomputed from the raw rows ────────────────────────────
    h("1. NÚMEROS BASE (recalculados a partir das linhas, não do dashboard)")
    staked = sum(b["stake"] for b in settled)
    profit = sum(b["profit"] for b in settled)
    roi = profit / staked * 100 if staked else 0
    wins = sum(1 for b in decided if b["result"] == "won")
    wr = wins / len(decided) * 100 if decided else 0
    avg_odd = sum(b["odds"] for b in decided) / len(decided) if decided else 0
    wavg_odd = (sum(b["odds"] * b["stake"] for b in decided) / sum(b["stake"] for b in decided)
                if decided else 0)
    be_wr = 100 / wavg_odd if wavg_odd else 0

    print(f"  apostas totais        {len(bets)}  (resolvidas {len(settled)}, pendentes {len(pending)})")
    print(f"  staked                EUR {staked:.2f}   (stake média EUR {staked / len(settled):.2f})"
          if settled else "")
    print(f"  lucro                 EUR {profit:+.2f}")
    print(f"  ROI realizado         {pct(roi)}")
    print(f"  win rate              {wr:.1f}%  ({wins}/{len(decided)})")
    print(f"  odd média (simples)   {avg_odd:.3f}")
    print(f"  odd média (ponderada por stake) {wavg_odd:.3f}")
    # The break-even test must compare like with like: a stake-weighted odd needs a
    # STAKE-weighted win rate. Comparing it to the head-count win rate mixes two
    # different denominators and can invent a deficit that does not exist.
    stk_all = sum(b["stake"] for b in decided) or 1
    stk_won = sum(b["stake"] for b in decided if b["result"] == "won")
    wr_w = stk_won / stk_all * 100
    print(f"  win rate ponderada por stake    {wr_w:.1f}%  "
          f"(EUR {stk_won:.0f} de EUR {stk_all:.0f} apostados ganharam)")
    print(f"  break-even a essa odd           {be_wr:.1f}%")
    print(f"  margem sobre break-even         {wr_w - be_wr:+.1f} pp  "
          f"(ponderada vs ponderada — a comparacao correta)")
    print(f"  [referencia nao ponderada: {wr:.1f}% vs {100 / avg_odd:.1f}% "
          f"= {wr - 100 / avg_odd:+.1f} pp]")
    conc = sorted((b["stake"] for b in settled), reverse=True)
    if len(conc) >= 6:
        print(f"\n  CONCENTRACAO: as 6 maiores stakes valem EUR {sum(conc[:6]):.0f} "
              f"de EUR {staked:.0f} ({sum(conc[:6]) / staked * 100:.0f}% do dinheiro total)")

    # ── 2. Expected vs realised: how much is skill, how much is luck ─────────
    h("2. ESPERADO vs REALIZADO — quanto disto e' edge e quanto e' sorte")
    with_edge = [b for b in settled if b.get("edge_pct") is not None]
    if with_edge:
        exp_profit = sum(b["stake"] * b["edge_pct"] / 100 for b in with_edge)
        exp_staked = sum(b["stake"] for b in with_edge)
        exp_roi = exp_profit / exp_staked * 100
        print(f"  apostas com edge registado: {len(with_edge)}/{len(settled)}")
        print(f"  ROI esperado pelo modelo    {pct(exp_roi)}   (EUR {exp_profit:+.2f})")
        print(f"  ROI realizado               {pct(roi)}   (EUR {profit:+.2f})")
        print(f"  excesso                     {pct(roi - exp_roi)}   (EUR {profit - exp_profit:+.2f})")

    returns = [b["profit"] for b in settled]
    stakes = [b["stake"] for b in settled]
    if len(returns) > 5:
        lo, hi, p0 = bootstrap_roi(returns, stakes)
        print(f"\n  Bootstrap (20.000 reamostragens) do ROI verdadeiro:")
        print(f"    IC 95%                    [{lo:+.1f}% , {hi:+.1f}%]   (largura {hi - lo:.1f} pp)")
        print(f"    P(ROI verdadeiro <= 0)    {p0 * 100:.1f}%")
        # per-bet return volatility -> sample size needed
        r_unit = [b["profit"] / b["stake"] for b in settled]
        mu = sum(r_unit) / len(r_unit)
        sd = math.sqrt(sum((x - mu) ** 2 for x in r_unit) / (len(r_unit) - 1))
        print(f"    desvio-padrao por aposta  {sd:.3f} unidades")
        for target in (2.9, 5.0):
            n_needed = (1.96 * sd / (target / 100)) ** 2
            print(f"    n para provar ROI de {target:.1f}% a 95%: ~{n_needed:,.0f} apostas")

    # ── 3. CLV — the leading indicator ───────────────────────────────────────
    h("3. CLV — o indicador que converge depressa (e' aqui que esta' a verdade)")
    # The API now publishes clv_pct ONLY when the close was captured close enough
    # to kickoff to mean anything; everything else arrives as clv_pct_unverified.
    # We analyse the full set (to see how bad the capture is) but report the two
    # populations apart, because mixing them is exactly what produced the false
    # "97.4% positive CLV" headline.
    for b in settled:
        if b.get("clv_pct") is None and b.get("clv_pct_unverified") is not None:
            b["_clv_any"] = b["clv_pct_unverified"]
            b["_clv_counts"] = False
        elif b.get("clv_pct") is not None:
            b["_clv_any"] = b["clv_pct"]
            b["_clv_counts"] = True
        else:
            b["_clv_any"] = None
            b["_clv_counts"] = False

    countable = [b for b in settled if b.get("_clv_counts")]
    unverified = [b for b in settled if b.get("_clv_any") is not None and not b.get("_clv_counts")]
    print(f"  CLV contavel (fecho perto do jogo)  {len(countable)}")
    print(f"  CLV descartado (fecho tarde demais) {len(unverified)}")
    if countable:
        cv = sorted(b["clv_pct"] for b in countable)
        print(f"  --> CLV medio APENAS dos contaveis  {sum(cv) / len(cv):+.2f}%"
              f"  (positivos {sum(1 for v in cv if v > 0)}/{len(cv)})")
    else:
        print("  --> nenhum CLV contavel: a captura do fecho nao esta' a funcionar")

    # Everything below runs on the FULL set, unverified included, and is labelled
    # as such — it diagnoses the capture, it does not prove the edge.
    clv = [dict(b, clv_pct=b["_clv_any"]) for b in settled if b.get("_clv_any") is not None]
    if clv:
        print("\n  (as estatisticas seguintes incluem os fechos nao verificados —")
        print("   servem para diagnosticar a captura, NAO para provar o edge)")
        vals = sorted(b["clv_pct"] for b in clv)
        mean_clv = sum(vals) / len(vals)
        med = vals[len(vals) // 2]
        pos = sum(1 for v in vals if v > 0)
        print(f"  apostas com CLV medido  {len(clv)}")
        print(f"  CLV medio               {pct(mean_clv)}")
        print(f"  CLV mediano             {pct(med)}")
        print(f"  positivos               {pos}/{len(vals)}  ({pos / len(vals) * 100:.1f}%)")
        print(f"  pior / melhor           {vals[0]:+.2f}% / {vals[-1]:+.2f}%")
        # binomial: probability of >= pos positives by pure chance (p=0.5)
        n = len(vals)
        p_chance = sum(math.comb(n, k) for k in range(pos, n + 1)) / (2 ** n)
        print(f"  P(este nº de positivos por puro acaso, p=0.5): {p_chance:.3g}")
        if with_edge:
            print(f"\n  Coerencia: CLV medio {pct(mean_clv)} vs edge medio do modelo "
                  f"{pct(sum(b['edge_pct'] for b in with_edge) / len(with_edge))}")
            print("  (batem um no outro => a estimativa de edge do modelo esta' a ser")
            print("   confirmada pelo mercado no fecho, nao inflacionada)")

        # ── CLV independence check ──────────────────────────────────────────
        # A CLV that exactly equals the entry edge means the "close" snapshot is
        # the SAME price that produced the edge — the line never moved (or was
        # never re-captured). Those rows restate the edge, they do not confirm it.
        # Only the rows where the line genuinely moved are independent evidence.
        moved, frozen = [], []
        for b in clv:
            e = b.get("edge_pct")
            if e is None:
                continue
            (frozen if abs(b["clv_pct"] - e) < 0.05 else moved).append(b)
        print(f"\n  TESTE DE INDEPENDENCIA do CLV:")
        print(f"    linha NAO mexeu (CLV == edge de entrada)  {len(frozen)}/{len(frozen) + len(moved)}"
              f"  -> nao sao prova independente")
        print(f"    linha MEXEU mesmo                         {len(moved)}/{len(frozen) + len(moved)}"
              f"  -> e' aqui que esta' a prova")
        if moved:
            mv = [b["clv_pct"] for b in moved]
            me = [b["edge_pct"] for b in moved]
            pos_m = sum(1 for v in mv if v > 0)
            against = sum(1 for b in moved if b["clv_pct"] < b["edge_pct"])
            print(f"    subconjunto independente: edge entrada {sum(me) / len(me):+.2f}%"
                  f"  ->  CLV no fecho {sum(mv) / len(mv):+.2f}%")
            print(f"    positivos nesse subconjunto: {pos_m}/{len(mv)}")
            print(f"    a linha mexeu CONTRA nos em {against}/{len(moved)} casos")
            print("    (se mexe contra na maioria e o CLV continua positivo, o valor vem")
            print("     da DIFERENCA DE PRECO na entrada, nao de prever o movimento)")

        # ── How late was the "close" actually captured? ─────────────────────
        # A close captured hours before kickoff is not a close. If the capture
        # lead time is large, the CLV is measured against a mid-week price and
        # means nothing.
        leads = []
        for b in clv:
            ko = _parse_iso(b.get("commence_time"))
            cap = _parse_iso(b.get("pin_close_captured_at"))
            if ko and cap:
                leads.append(((ko - cap).total_seconds() / 60.0, b))
        if leads:
            leads.sort(key=lambda t: t[0])
            mins = [m for m, _ in leads]
            print(f"\n  QUALIDADE DO FECHO (minutos entre a captura e o pontape de saida):")
            print(f"    n com timestamp de captura  {len(mins)}/{len(clv)}")
            print(f"    mediana                     {mins[len(mins) // 2]:,.0f} min")
            print(f"    min / max                   {mins[0]:,.0f} / {mins[-1]:,.0f} min")
            for lo, hi, lbl in ((0, 30, "<30min  (fecho a serio)"),
                                (30, 120, "30min-2h (aceitavel)"),
                                (120, 360, "2h-6h    (fraco)"),
                                (360, 1e9, ">6h      (nao e' fecho)")):
                grp = [m for m in mins if lo <= m < hi]
                if grp:
                    print(f"      {lbl:26s} {len(grp):3d}  ({len(grp) / len(mins) * 100:.0f}%)")
            worst = [b for m, b in leads if m >= 360]
            if worst:
                print("    apostas cujo 'fecho' foi capturado a mais de 6h do jogo:")
                for b in worst[:12]:
                    ko = _parse_iso(b.get("commence_time"))
                    cap = _parse_iso(b.get("pin_close_captured_at"))
                    print(f"      #{b['id']:<4} {(b.get('home_team') or '')[:24]:24s} "
                          f"captura {cap:%Y-%m-%d %H:%M}  jogo {ko:%Y-%m-%d %H:%M}  "
                          f"({(ko - cap).total_seconds() / 3600:.1f}h antes)  CLV {b['clv_pct']:+.1f}%")

        print("\n  Apostas com CLV NEGATIVO (as que o mercado disse que estavam erradas):")
        neg = [b for b in clv if b["clv_pct"] <= 0]
        if neg:
            table([[f"#{b['id']}", (b.get('home_team') or '')[:16], (b.get('selection') or '')[:16],
                    f"{b['odds']:.2f}", f"{b.get('pin_close_fair_odds') or 0:.2f}",
                    pct(b['clv_pct'], 1), b['result'], f"{b['profit']:+.2f}"] for b in neg],
                   ["id", "jogo", "selecao", "odd", "fecho justo", "CLV", "res", "P/L"])
        else:
            print("    nenhuma.")

        # does CLV predict profit?
        won_clv = [b["clv_pct"] for b in clv if b["result"] == "won"]
        lost_clv = [b["clv_pct"] for b in clv if b["result"] == "lost"]
        if won_clv and lost_clv:
            print(f"\n  CLV medio das ganhas  {sum(won_clv) / len(won_clv):+.2f}%  (n={len(won_clv)})")
            print(f"  CLV medio das perdidas {sum(lost_clv) / len(lost_clv):+.2f}%  (n={len(lost_clv)})")
            print("  (se forem parecidos, o CLV nao esta' so' a seguir o resultado —")
            print("   e' mesmo uma medida independente da qualidade do preco)")

    # ── 4. Where the money comes from ────────────────────────────────────────
    h("4. DE ONDE VEM O DINHEIRO")

    def odd_band(b):
        o = b["odds"]
        if o < 1.30: return "a) <1.30"
        if o < 1.50: return "b) 1.30-1.50"
        if o < 2.00: return "c) 1.50-2.00"
        if o < 3.00: return "d) 2.00-3.00"
        return "e) 3.00+"

    def edge_band(b):
        e = b.get("edge_pct")
        if e is None: return "sem edge"
        if e < 2: return "a) <2%"
        if e < 4: return "b) 2-4%"
        if e < 7: return "c) 4-7%"
        return "d) 7%+"

    group_report(settled, odd_band, "POR BANDA DE ODD  (o teste do paradoxo '60% e perde')")
    group_report(settled, edge_band, "POR BANDA DE EDGE  (o modelo distingue edge grande de pequeno?)")
    group_report(settled, lambda b: b.get("sport_name") or "?", "POR DESPORTO")
    group_report(settled, lambda b: b.get("market") or "?", "POR MERCADO")
    group_report(settled, lambda b: b.get("bookmaker") or "?", "POR CASA")
    # THE decisive split: bets priced against a real sharp reference vs bets whose
    # "edge" was only our own Elo/xG estimate. Empty until bets start carrying the
    # provenance columns.
    if any(b.get("odds_source") for b in settled):
        group_report(settled, lambda b: b.get("odds_source") or "(antes do registo)",
                     "POR FONTE DA PROBABILIDADE  <<< o teste que interessa")
        group_report(settled, lambda b: b.get("ref_agreement") or "(antes do registo)",
                     "POR CONCORDANCIA DAS REFERENCIAS")
    else:
        print("\nPOR FONTE DA PROBABILIDADE: nenhuma aposta tem odds_source guardado ainda.")
        print("  (as apostas novas passam a guardar; esta tabela enche-se sozinha)")

    # ── 5. Staking ───────────────────────────────────────────────────────────
    h("5. STAKE SIZING — o Kelly esta' a fazer o seu trabalho?")
    if settled:
        st_sorted = sorted(b["stake"] for b in settled)
        print(f"  min {st_sorted[0]:.2f} | p25 {st_sorted[len(st_sorted) // 4]:.2f} | "
              f"mediana {st_sorted[len(st_sorted) // 2]:.2f} | "
              f"p75 {st_sorted[3 * len(st_sorted) // 4]:.2f} | max {st_sorted[-1]:.2f}")
        flat = sum(b["profit"] / b["stake"] for b in settled)  # 1u flat on every bet
        flat_roi = flat / len(settled) * 100
        print(f"\n  ROI com staking real (Kelly)  {pct(roi)}")
        print(f"  ROI com stake plana (1u)      {pct(flat_roi)}")
        delta = roi - flat_roi
        print(f"  contributo do sizing          {pct(delta)} "
              f"({'o Kelly ajudou' if delta > 0 else 'a stake plana teria sido melhor'})")
        if with_edge:
            big = [b for b in with_edge if b["edge_pct"] >= 4]
            small = [b for b in with_edge if b["edge_pct"] < 4]
            if big and small:
                print(f"\n  stake media com edge >=4%: EUR {sum(b['stake'] for b in big) / len(big):.2f}")
                print(f"  stake media com edge < 4%: EUR {sum(b['stake'] for b in small) / len(small):.2f}")
                print("  (a primeira deve ser maior — e' o proposito do Kelly)")

    # ── 6. Full ledger ───────────────────────────────────────────────────────
    h("6. LIVRO COMPLETO (mais recentes primeiro)")
    rows = []
    for b in bets:
        rows.append([
            f"#{b['id']}", (b.get("placed_at") or "")[:10],
            ((b.get("home_team") or "") + " v " + (b.get("away_team") or ""))[:30],
            (b.get("market") or "")[:14], (b.get("selection") or "")[:16],
            f"{b['odds']:.2f}", f"{b['stake']:.2f}",
            pct(b["edge_pct"], 1) if b.get("edge_pct") is not None else "—",
            f"{b.get('pin_close_fair_odds') or 0:.2f}" if b.get("pin_close_fair_odds") else "—",
            pct(b["clv_pct"], 1) if b.get("clv_pct") is not None else "—",
            b.get("result") or b.get("status") or "?", f"{b['profit']:+.2f}",
        ])
    table(rows, ["id", "data", "jogo", "mercado", "selecao", "odd", "stake",
                 "edge", "fecho", "CLV", "res", "P/L"])

    # ── 7. Running bankroll / drawdown ───────────────────────────────────────
    h("7. EVOLUCAO E DRAWDOWN")
    chron = sorted(settled, key=lambda b: b.get("settled_at") or b.get("placed_at") or "")
    bal, peak, maxdd, run = 0.0, 0.0, 0.0, []
    losing_streak = worst_streak = 0
    for b in chron:
        bal += b["profit"]
        peak = max(peak, bal)
        maxdd = min(maxdd, bal - peak)
        run.append(bal)
        if b["result"] == "lost":
            losing_streak += 1
            worst_streak = max(worst_streak, losing_streak)
        elif b["result"] == "won":
            losing_streak = 0
    if run:
        print(f"  lucro acumulado final   EUR {run[-1]:+.2f}")
        print(f"  pico                    EUR {peak:+.2f}")
        print(f"  maior drawdown          EUR {maxdd:+.2f}")
        print(f"  pior serie de derrotas  {worst_streak} seguidas")
        print(f"  curva: {' '.join(f'{v:+.0f}' for v in run)}")


if __name__ == "__main__":
    main()
