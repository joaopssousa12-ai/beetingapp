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
VB_REF_FRESH_H = 2
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

print("\n  LEITURA (mediana = quanto o preco JUSTO da Pinnacle se mexeu nesse intervalo)")
print()


def verdict(key, floor, label):
    v = usable.get(key)
    if not v:
        print(f"    {label:<28} sem dados")
        return
    med = v["median_move_pct"]
    if med > floor:
        print(f"    {label:<28} deriva mediana {med:.2f}% > piso {floor:.0f}%"
              f"  -> PISO BAIXO DEMAIS")
    else:
        print(f"    {label:<28} deriva mediana {med:.2f}% <= piso {floor:.0f}%"
              f"  -> piso defensavel")


verdict("0-1h", VB_VALUE_FLOOR, f"fresca (<{VB_REF_FRESH_H}h), piso {VB_VALUE_FLOOR:.0f}%")
verdict("1-3h", VB_VALUE_FLOOR, f"ainda dentro de {VB_REF_FRESH_H}h")
verdict("3-6h", VB_VALUE_FLOOR_AGING, f"{VB_REF_FRESH_H}-{VB_REF_MAX_AGE_H}h, piso {VB_VALUE_FLOOR_AGING:.0f}%")

stale = usable.get("6-12h") or usable.get("12-24h")
if stale:
    print(f"\n    >{VB_REF_MAX_AGE_H}h: deriva mediana {stale['median_move_pct']:.2f}%,"
          f" {stale['share_above_2pct']:.0f}% dos casos mexem mais de 2%")
    print(f"    -> bloquear verde acima de {VB_REF_MAX_AGE_H}h esta' "
          f"{'justificado' if stale['median_move_pct'] > VB_VALUE_FLOOR else 'possivelmente severo demais'}")
