#!/usr/bin/env python3
"""Pretty-print /api/diag/line-drift and judge the freshness thresholds against it.

The question this answers: how stale can the stored Pinnacle price be before a
2% edge measured against it is inside the noise? The gate currently uses 2h / 6h
and a raised 4% floor in between, chosen to match the UI's existing warnings —
this is the measurement that says whether those numbers are right.
"""
import json
import sys

# Mirrors static/js/app.js
VB_VALUE_FLOOR = 2.0
VB_VALUE_FLOOR_AGING = 4.0
VB_REF_FRESH_H = 3
VB_REF_MAX_AGE_H = 6

path = sys.argv[1] if len(sys.argv) > 1 else "drift.json"
try:
    with open(path) as fh:
        d = json.load(fh)
except Exception as exc:                          # noqa: BLE001 - diagnostics
    print(f"  resposta nao e' JSON ({exc}) — o endpoint ainda nao esta' em producao.")
    raise SystemExit(0)

if "drift_by_gap" not in d:
    print("  /api/diag/line-drift nao existe nesta versao — falta o deploy.")
    print("  resposta:", json.dumps(d)[:300])
    raise SystemExit(0)

print(f"  eventos com >=2 snapshots: {d.get('events_with_multiple_snapshots')}")
print(f"  pares medidos:             {d.get('snapshot_pairs_measured')}")
print()
print(f"  {'intervalo':<10} {'n':>6} {'mediana':>9} {'p75':>8} {'p90':>8}"
      f" {'>2%':>7} {'>3%':>7}")
print(f"  {'-' * 10} {'-' * 6} {'-' * 9} {'-' * 8} {'-' * 8} {'-' * 7} {'-' * 7}")

rows = d.get("drift_by_gap") or {}
order = ["0-1h", "1-3h", "3-6h", "6-12h", "12-24h", "24h+"]
usable = {}
for k in order:
    v = rows.get(k) or {}
    if not v.get("n"):
        print(f"  {k:<10} {'—':>6}")
        continue
    usable[k] = v
    print(f"  {k:<10} {v['n']:>6} {v['median_move_pct']:>8.2f}% {v['p75']:>7.2f}%"
          f" {v['p90']:>7.2f}% {v['share_above_2pct']:>6.1f}% {v['share_above_3pct']:>6.1f}%")

if not usable:
    print("\n  Sem pares suficientes para medir. O historico so' tem um snapshot por")
    print("  evento na maior parte dos casos — vai encher a medida que o cron corre.")
    raise SystemExit(0)

# ── The execution question ──────────────────────────────────────────────────
# Pinnacle drift says whether the edge was real when it was computed. This says
# something different and just as important: when you actually go to place the
# bet, is the 1xBet price still there? Only this one is about execution.
x1 = d.get("x1_drift_by_gap") or {}
if any(v.get("n") for v in x1.values()):
    print("\n  PRECO DA 1xBET — a odd ainda la' esta' quando vou apostar?")
    print(f"  {'intervalo':<10} {'n':>6} {'igual':>8} {'piorou':>8} {'mediana':>9}"
          f" {'se piorou':>10}")
    print(f"  {'-' * 10} {'-' * 6} {'-' * 8} {'-' * 8} {'-' * 9} {'-' * 10}")
    for k in order:
        v = x1.get(k) or {}
        if not v.get("n"):
            print(f"  {k:<10} {'—':>6}")
            continue
        print(f"  {k:<10} {v['n']:>6} {v['share_unchanged']:>7.1f}%"
              f" {v['share_moved_against_us']:>7.1f}% {v['median_move_pct']:>8.2f}%"
              f" {v['median_when_against']:>9.2f}%")
    fresh = [x1.get(k) or {} for k in ("0-1h", "1-3h")]
    fresh = [v for v in fresh if v.get("n")]
    if fresh:
        n_tot = sum(v["n"] for v in fresh)
        same = sum(v["share_unchanged"] * v["n"] for v in fresh) / n_tot
        worse = sum(v["share_moved_against_us"] * v["n"] for v in fresh) / n_tot
        print(f"\n    dentro de 3h: a odd da 1xBet estava IGUAL em {same:.1f}% dos casos,"
              f" piorou em {worse:.1f}% (n={n_tot})")

print("\n  LEITURA (mediana = quanto o preco JUSTO da Pinnacle se mexeu nesse intervalo)")
print()


# The criterion is NOT "is the median drift below the edge floor". The median
# hides the tail, and the tail is what ruins a bet: a reference whose price moves
# more than your whole edge 40% of the time is useless even if it usually sits
# still. (Judging by the median was the same mistake as averaging stale CLVs.)
# So we ask: how OFTEN does the drift swallow the edge we would act on?
MAX_TOLERATED_SHARE = 20.0   # % of cases where drift may exceed the floor
MIN_N = 10                   # below this, say so instead of concluding


def verdict(key, floor, label):
    v = usable.get(key)
    if not v:
        print(f"    {label:<30} sem dados")
        return
    share = v["share_above_2pct"] if floor <= 2 else v["share_above_3pct"]
    note = f"deriva passa o piso em {share:.1f}% dos casos"
    if v["n"] < MIN_N:
        print(f"    {label:<30} {note}  -> apenas n={v['n']}, INSUFICIENTE")
    elif share > MAX_TOLERATED_SHARE:
        print(f"    {label:<30} {note}  -> PISO BAIXO DEMAIS")
    else:
        print(f"    {label:<30} {note}  -> piso defensavel (n={v['n']})")


verdict("0-1h", VB_VALUE_FLOOR, f"0-1h, piso {VB_VALUE_FLOOR:.0f}%")
verdict("1-3h", VB_VALUE_FLOOR, f"1-3h, piso {VB_VALUE_FLOOR:.0f}%")
verdict("3-6h", VB_VALUE_FLOOR_AGING, f"3-6h, piso {VB_VALUE_FLOOR_AGING:.0f}%")

print()
for key in ("6-12h", "12-24h", "24h+"):
    v = usable.get(key)
    if not v:
        continue
    print(f"    >{VB_REF_MAX_AGE_H}h ({key}): mediana {v['median_move_pct']:.2f}%, mas"
          f" {v['share_above_2pct']:.1f}% dos casos mexem mais de 2% (p90 {v['p90']:.2f}%)")
stale = usable.get("6-12h")
if stale and stale["n"] >= MIN_N:
    ok = stale["share_above_2pct"] > MAX_TOLERATED_SHARE
    print(f"\n    -> bloquear verde acima de {VB_REF_MAX_AGE_H}h: "
          f"{'JUSTIFICADO' if ok else 'possivelmente severo demais'}")
