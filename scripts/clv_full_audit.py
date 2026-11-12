#!/usr/bin/env python3
"""Is the CLV trustworthy? Audit EVERY bet against its raw snapshot timeline.

bets_analysis.py reads the numbers the API publishes. This script does the
opposite job: it distrusts them. For each bet it pulls the full Pinnacle
snapshot timeline (/api/bets/{id}/clv-audit) and re-derives what the close
SHOULD be, then checks a list of invariants that a correct CLV must satisfy.
A CLV pipeline can be wrong in ways an average never shows (a close taken from
the wrong side of the market, an in-play price stamped as pre-match, a devig
done on two outcomes of a three-way line), so each one is tested explicitly.

Read-only. No Odds-API credits. Usage: python3 scripts/clv_full_audit.py
"""
import json
import math
import random
import sys
import urllib.request
from datetime import datetime, timedelta

BASE = "https://beetingapp-1.onrender.com"

# Mirrors collectors/database.py
CLV_GOOD_LEAD_MIN = 60
CLV_MAX_LEAD_MIN = 180

# A Pinnacle 1X2 / 2-way margin sits around 2-5%. Devigging can only LOWER a
# probability, so the fair odd must be >= the raw odd, by about that margin.
FAIR_RAW_MIN, FAIR_RAW_MAX = 1.000, 1.085
# Pre-match prices drift; they do not jump. A move this large between two
# consecutive snapshots minutes apart is what in-play contamination, a
# suspended market or a home/away mix-up looks like.
JUMP_LIMIT = 0.12


def get(path, timeout=180):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


def ts(v):
    if not v:
        return None
    s = str(v).strip().replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1]
    plus = s.find("+", 10)
    if plus != -1:
        s = s[:plus]
    for n, fmt in ((19, "%Y-%m-%d %H:%M:%S"), (16, "%Y-%m-%d %H:%M")):
        try:
            return datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    return None


def h(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def sd(xs):
    if len(xs) < 2:
        return float("nan")
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def t_crit(n):
    """Two-sided 95% Student-t critical value (table; exact enough for a CI)."""
    table = {2: 12.71, 3: 4.30, 4: 3.18, 5: 2.78, 6: 2.57, 7: 2.45, 8: 2.36, 9: 2.31,
             10: 2.26, 12: 2.20, 15: 2.14, 20: 2.09, 25: 2.06, 30: 2.05, 40: 2.02,
             60: 2.00, 120: 1.98}
    for k in sorted(table):
        if n <= k:
            return table[k]
    return 1.96


def describe(label, xs, stakes=None):
    """One line: n, mean, 95% CI, share positive, and whether zero is excluded."""
    n = len(xs)
    if n == 0:
        print(f"  {label:<38} n=0")
        return
    m = mean(xs)
    pos = sum(1 for x in xs if x > 0)
    if n < 3:
        print(f"  {label:<38} n={n:<3} media {m:+.2f}%  (amostra pequena demais para IC)")
        return
    s = sd(xs)
    se = s / math.sqrt(n)
    lo, hi = m - t_crit(n) * se, m + t_crit(n) * se
    verdict = "POSITIVO (IC exclui 0)" if lo > 0 else ("NEGATIVO" if hi < 0 else "inconclusivo (IC inclui 0)")
    extra = ""
    if stakes:
        sw = sum(x * w for x, w in zip(xs, stakes)) / sum(stakes)
        extra = f"  | pond. stake {sw:+.2f}%"
    print(f"  {label:<38} n={n:<3} media {m:+.2f}%  IC95 [{lo:+.2f}, {hi:+.2f}]  "
          f"dp {s:.2f}  pos {pos}/{n} ({pos / n * 100:.0f}%)  -> {verdict}{extra}")


def bootstrap_ci(xs, reps=20000, seed=7):
    rnd = random.Random(seed)
    n = len(xs)
    means = sorted(sum(rnd.choice(xs) for _ in range(n)) / n for _ in range(reps))
    return means[int(reps * 0.025)], means[int(reps * 0.975)], sum(1 for v in means if v <= 0) / reps


def main():
    bets = get("/api/bets?limit=1000")
    if isinstance(bets, dict):
        print("  /api/bets devolveu um erro:", json.dumps(bets)[:300])
        return 1
    print(f"Fonte: {BASE}   apostas: {len(bets)}   agora (UTC): "
          f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')}")

    problems = []      # (bet_id, text)  -> things that make a CLV WRONG
    notes = []         # (bet_id, text)  -> things worth a look, not errors
    rows = []          # per-bet digest for the tables below

    for b in sorted(bets, key=lambda x: x["id"]):
        bid = b["id"]
        if not b.get("event_id"):
            continue
        try:
            a = get(f"/api/bets/{bid}/clv-audit")
        except Exception as e:                        # noqa: BLE001 - diagnostics
            notes.append((bid, f"auditoria indisponivel: {e}"))
            continue
        snaps = a.get("snapshots") or []
        ko = ts(b.get("commence_time"))
        raw, fair = b.get("pin_close_odds"), b.get("pin_close_fair_odds")
        cap = ts(b.get("pin_close_captured_at"))
        lead = (ko - cap).total_seconds() / 60.0 if (ko and cap) else None

        pre = [s for s in snaps if s.get("pre_kickoff") and s.get("pin_price_for_selection")]
        pre.sort(key=lambda s: s["captured_at"])
        last_pre = pre[-1] if pre else None

        if raw is None:
            if ko and ko < datetime.utcnow() and b.get("status") != "pending":
                notes.append((bid, "sem fecho capturado (nenhum snapshot pre-jogo cota a selecao)"))
            rows.append({"b": b, "lead": None, "countable": False, "n_last60": 0})
            continue

        # C1 — the close must predate kickoff
        if lead is not None and lead < 0:
            problems.append((bid, f"fecho capturado {abs(lead):.0f} min DEPOIS do pontape (preco live)"))

        # C2 — the stored close must be the LAST pre-kickoff snapshot quoting us
        if last_pre is not None:
            if not last_pre.get("is_stored_close"):
                lp = ts(last_pre["captured_at"])
                gap = (lp - cap).total_seconds() / 60.0 if (lp and cap) else None
                if gap is not None and gap > 0.5:
                    problems.append((bid, f"o fecho guardado ({b.get('pin_close_captured_at')}) NAO e' o "
                                          f"ultimo snapshot pre-jogo ({last_pre['captured_at']}, "
                                          f"{gap:.0f} min mais tarde, preco {last_pre['pin_price_for_selection']})"))
        elif snaps:
            notes.append((bid, "o historico ja' nao tem o snapshot do fecho (purga >30 dias) — nao re-verificavel"))

        # C3 — devig sanity: fair odd >= raw odd, by a plausible margin
        if fair:
            ratio = fair / raw
            if not (FAIR_RAW_MIN <= ratio <= FAIR_RAW_MAX):
                problems.append((bid, f"devig incoerente: fecho cru {raw} -> justo {fair} "
                                      f"(racio {ratio:.3f}; esperado {FAIR_RAW_MIN:.2f}-{FAIR_RAW_MAX:.3f})"))
        else:
            notes.append((bid, "fecho cru sem devig (falta o outro lado do mercado) — CLV fica pendente"))

        # C4 — the published CLV must equal the arithmetic
        pub = b.get("clv_pct") if b.get("clv_pct") is not None else b.get("clv_pct_unverified")
        if fair and pub is not None:
            calc = (b["odds"] / fair - 1) * 100
            if abs(calc - pub) > 0.03:
                problems.append((bid, f"CLV publicado {pub:+.2f}% != recalculado {calc:+.2f}%"))

        # C5 — the quality label must follow from the lead time
        if lead is not None:
            lead_r = round(lead)          # the server classifies on whole minutes
            want = ("post_kickoff" if lead_r < 0 else "good" if lead_r <= CLV_GOOD_LEAD_MIN
                    else "ok" if lead_r <= CLV_MAX_LEAD_MIN else "stale")
            if b.get("clv_quality") and b["clv_quality"] != want:
                problems.append((bid, f"qualidade '{b['clv_quality']}' nao bate com lead {lead:.0f} min (esperado '{want}')"))
            countable = want in ("good", "ok")
            if countable and b.get("clv_pct") is None and b.get("status") == "settled":
                problems.append((bid, "fecho valido mas clv_pct nao publicado"))
            if not countable and b.get("clv_pct") is not None:
                problems.append((bid, f"fecho a {lead:.0f} min mas clv_pct publicado como valido"))
        else:
            countable = False

        # C6 — pre-match prices drift, they do not jump (final 90 minutes)
        max_jump, jump_at = 0.0, None
        if ko:
            window = [s for s in pre if (ko - ts(s["captured_at"])).total_seconds() <= 90 * 60]
            for s1, s2 in zip(window, window[1:]):
                p1, p2 = s1["pin_price_for_selection"], s2["pin_price_for_selection"]
                j = abs(p2 / p1 - 1)
                if j > max_jump:
                    max_jump, jump_at = j, s2["captured_at"]
            if max_jump > JUMP_LIMIT:
                problems.append((bid, f"salto de {max_jump * 100:.1f}% entre snapshots consecutivos na ultima "
                                      f"hora e meia (em {jump_at}) — preco live ou mercado suspenso?"))

        # C7 — how well covered was the run-in?
        n_last60 = sum(1 for s in pre if ko and 0 <= (ko - ts(s["captured_at"])).total_seconds() <= 3600)

        # C8 — does the edge claimed at entry match the Pinnacle price stored then?
        entry_ratio = None
        placed = ts(b.get("placed_at"))
        if placed and b.get("edge_pct") is not None and pre:
            before = [s for s in pre if ts(s["captured_at"]) <= placed + timedelta(minutes=3)]
            if before:
                ref = before[-1]
                age_h = (placed - ts(ref["captured_at"])).total_seconds() / 3600.0
                fair_entry = b["odds"] / (1 + b["edge_pct"] / 100.0)
                entry_ratio = fair_entry / ref["pin_price_for_selection"]
                if not (0.985 <= entry_ratio <= 1.09) and age_h <= 6:
                    notes.append((bid, f"edge de entrada ({b['edge_pct']:+.1f}%) implica odd justa "
                                       f"{fair_entry:.3f}, mas a Pinnacle crua no snapshot anterior "
                                       f"({age_h:.1f}h antes) era {ref['pin_price_for_selection']} "
                                       f"(racio {entry_ratio:.3f})"))

        rows.append({"b": b, "lead": lead, "countable": countable, "n_last60": n_last60,
                     "max_jump": max_jump, "entry_ratio": entry_ratio,
                     "first_pre": pre[0] if pre else None, "last_pre": last_pre,
                     "n_pre": len(pre)})

    # ── duplicates ───────────────────────────────────────────────────────────
    seen = {}
    for b in bets:
        if b.get("event_id"):
            seen.setdefault((b["event_id"], b.get("market"), b.get("selection")), []).append(b["id"])
    for key, ids in seen.items():
        if len(ids) > 1:
            notes.append((ids[0], f"aposta DUPLICADA no mesmo evento/selecao: ids {sorted(ids)} "
                                  f"— conta a dobrar nas estatisticas"))

    # ── 1. invariants ────────────────────────────────────────────────────────
    h("1. INVARIANTES — ha' algum CLV errado?")
    with_close = [r for r in rows if r["b"].get("pin_close_odds") is not None]
    print(f"  apostas com evento: {len(rows)}   com fecho guardado: {len(with_close)}")
    if problems:
        print(f"\n  ❌ {len(problems)} PROBLEMA(S) que tornam um CLV errado:")
        for bid, text in sorted(problems):
            print(f"     #{bid}: {text}")
    else:
        print("\n  ✅ nenhuma violacao: todos os fechos sao pre-jogo, sao o ultimo snapshot")
        print("     disponivel, o devig e' coerente, a aritmetica bate e nao ha' saltos.")
    if notes:
        print(f"\n  notas ({len(notes)}) — nao sao erros de CLV, mas merecem atencao:")
        for bid, text in sorted(notes):
            print(f"     #{bid}: {text}")

    # ── 2. capture quality over time ─────────────────────────────────────────
    h("2. A CAPTURA — antes e depois de ser arranjada")
    FIX = datetime(2026, 10, 11)       # cron externo a chamar /api/closing/run
    def era(r):
        ko = ts(r["b"].get("commence_time"))
        return "depois" if (ko and ko >= FIX) else "antes"
    for label in ("antes", "depois"):
        grp = [r for r in with_close if era(r) == label]
        leads = sorted(r["lead"] for r in grp if r["lead"] is not None)
        if not leads:
            print(f"  {label} de 11/10: sem fechos")
            continue
        good = sum(1 for v in leads if 0 <= v <= 30)
        ok = sum(1 for v in leads if 0 <= v <= CLV_MAX_LEAD_MIN)
        print(f"  {label:<6} de 11/10: n={len(leads):<3} mediana {leads[len(leads) // 2]:>6.0f} min   "
              f"<=30min: {good}/{len(leads)} ({good / len(leads) * 100:.0f}%)   "
              f"contaveis (<=180): {ok}/{len(leads)} ({ok / len(leads) * 100:.0f}%)")
    recent = [r for r in with_close if era(r) == "depois"]
    if recent:
        print("\n  apostas DEPOIS da correcao (mais recentes primeiro):")
        print(f"  {'id':>5} {'jogo':<34} {'pontape (UTC)':<17} {'lead':>7} {'snaps<60m':>9} "
              f"{'salto max':>9} {'odd':>5} {'cru':>5} {'justo':>6} {'CLV':>7}")
        for r in sorted(recent, key=lambda r: -r["b"]["id"]):
            b = r["b"]
            pub = b.get("clv_pct") if b.get("clv_pct") is not None else b.get("clv_pct_unverified")
            game = f"{b.get('home_team')} v {b.get('away_team')}"[:34]
            lead = f"{r['lead']:.1f}m" if r["lead"] is not None else "—"
            mark = "" if r["countable"] else "  (descartado)"
            print(f"  #{b['id']:>4} {game:<34} {str(b.get('commence_time'))[:16]:<17} {lead:>7} "
                  f"{r['n_last60']:>9} {r.get('max_jump', 0) * 100:>8.1f}% {b['odds']:>5.2f} "
                  f"{b.get('pin_close_odds') or 0:>5.2f} {b.get('pin_close_fair_odds') or 0:>6.3f} "
                  f"{(f'{pub:+.1f}%' if pub is not None else '—'):>7}{mark}")

    # ── 3. is the CLV positive? ──────────────────────────────────────────────
    h("3. O CLV E' POSITIVO? — so' fechos validos, com intervalo de confianca")
    cnt = [r for r in rows if r["countable"] and r["b"].get("pin_close_fair_odds")]
    def clv(r):
        return (r["b"]["odds"] / r["b"]["pin_close_fair_odds"] - 1) * 100
    xs = [clv(r) for r in cnt]
    st = [r["b"]["stake"] for r in cnt]
    describe("TODOS os contaveis (<=180 min)", xs, st)
    describe("fecho a <=60 min", [clv(r) for r in cnt if r["lead"] <= 60])
    describe("fecho a <=30 min (fecho a serio)", [clv(r) for r in cnt if r["lead"] <= 30])
    describe("fecho a <=10 min", [clv(r) for r in cnt if r["lead"] <= 10])
    if len(xs) >= 10:
        lo, hi, p0 = bootstrap_ci(xs)
        print(f"\n  bootstrap (20.000 reamostragens) da media: IC95 [{lo:+.2f}%, {hi:+.2f}%]   "
              f"P(media <= 0) = {p0 * 100:.2f}%")
        pos = sum(1 for x in xs if x > 0)
        n = len(xs)
        p_sign = sum(math.comb(n, k) for k in range(pos, n + 1)) / 2 ** n
        print(f"  teste dos sinais: {pos}/{n} positivos, P(por acaso) = {p_sign:.4f}")

    print("\n  por periodo (pontape de saida):")
    def kod(r):
        return ts(r["b"].get("commence_time")) or datetime(2000, 1, 1)
    describe("  antes de 11/10 (captura por sorte)", [clv(r) for r in cnt if kod(r) < FIX])
    describe("  11/10 a 31/10", [clv(r) for r in cnt if FIX <= kod(r) < datetime(2026, 11, 1)])
    describe("  desde 01/11 (captura provada)", [clv(r) for r in cnt if kod(r) >= datetime(2026, 11, 1)])

    print("\n  por tipo de selecao:")
    def kind(r):
        b = r["b"]
        s = (b.get("selection") or "").strip().lower()
        if s in ("draw", "empate", "x"):
            return "empate"
        return "casa" if s == (b.get("home_team") or "").lower() else "fora"
    for k in ("casa", "empate", "fora"):
        describe(f"  {k}", [clv(r) for r in cnt if kind(r) == k])
    print("\n  por banda de odd:")
    for label, lo_o, hi_o in (("<1.50", 0, 1.5), ("1.50-2.00", 1.5, 2.0), ("2.00-3.00", 2.0, 3.0),
                              ("3.00+", 3.0, 99)):
        describe(f"  odd {label}", [clv(r) for r in cnt if lo_o <= r["b"]["odds"] < hi_o])
    print("\n  por desporto:")
    def sport(r):
        n = (r["b"].get("sport_name") or "").upper()
        return "tenis" if ("ATP" in n or "WTA" in n) else "futebol"
    for k in ("futebol", "tenis"):
        describe(f"  {k}", [clv(r) for r in cnt if sport(r) == k])

    # ── 4. how much of the claimed edge survives to the close? ───────────────
    h("4. QUANTO DO EDGE DECLARADO SOBREVIVE ATE' AO FECHO?")
    pairs = [(r["b"]["edge_pct"], clv(r)) for r in cnt if r["b"].get("edge_pct") is not None]
    if len(pairs) >= 8:
        es, cs = [p[0] for p in pairs], [p[1] for p in pairs]
        me, mc = mean(es), mean(cs)
        cov = sum((e - me) * (c - mc) for e, c in pairs) / (len(pairs) - 1)
        var_e = sum((e - me) ** 2 for e in es) / (len(pairs) - 1)
        slope = cov / var_e if var_e else float("nan")
        corr = cov / (sd(es) * sd(cs)) if sd(es) and sd(cs) else float("nan")
        print(f"  n={len(pairs)}   edge medio declarado {me:+.2f}%   CLV medio no fecho {mc:+.2f}%")
        print(f"  retencao (CLV/edge)      {mc / me * 100:.0f}%   -> o modelo anuncia {me:.1f} e o fecho confirma {mc:.1f}")
        print(f"  correlacao edge~CLV      {corr:+.2f}")
        print(f"  declive (CLV por 1pp de edge) {slope:+.2f}   (1.0 = edge todo real; 0 = edge e' ruido)")
        d = [c - e for e, c in pairs]
        describe("CLV menos edge (por aposta)", d)
    else:
        print("  pares insuficientes.")

    print("\n  coerencia do edge de ENTRADA com o snapshot Pinnacle guardado na altura")
    print("  (racio = odd justa implicita no edge / odd crua da Pinnacle; esperado 1.01-1.05):")
    er = sorted(r["entry_ratio"] for r in rows if r.get("entry_ratio"))
    if er:
        print(f"    n={len(er)}  min {er[0]:.3f}  mediana {er[len(er) // 2]:.3f}  max {er[-1]:.3f}")
    else:
        print("    sem snapshots de entrada no historico (purga aos 30 dias).")

    # ── 5. verdict ───────────────────────────────────────────────────────────
    h("5. VEREDICTO")
    n_recent = len(recent)
    n_recent_good = sum(1 for r in recent if r["lead"] is not None and 0 <= r["lead"] <= 30)
    print(f"  integridade : {'❌ ' + str(len(problems)) + ' violacao(oes) — ver seccao 1' if problems else '✅ 0 violacoes em ' + str(len(with_close)) + ' fechos'}")
    if n_recent:
        print(f"  captura     : {n_recent_good}/{n_recent} fechos a <=30 min desde 11/10 "
              f"({n_recent_good / n_recent * 100:.0f}%)")
    if len(xs) >= 3:
        s_e = sd(xs) / math.sqrt(len(xs))
        lo, hi = mean(xs) - t_crit(len(xs)) * s_e, mean(xs) + t_crit(len(xs)) * s_e
        print(f"  CLV         : {mean(xs):+.2f}%  IC95 [{lo:+.2f}%, {hi:+.2f}%]  (n={len(xs)})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
