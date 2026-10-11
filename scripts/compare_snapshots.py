#!/usr/bin/env python3
"""Did the capture actually write new snapshots? Compare two /api/diag/closing reads.

This is the whole proof: the bug was that a match ended its life with ONE
odds_history row, taken hours before kickoff. If the count goes up after calling
the capture, the mechanism works; if it does not, it still does not.
"""
import json
import sys


def load(path):
    with open(path) as fh:
        return json.load(fh)


def by_id(d):
    return {b["id"]: b for b in (d.get("pending_bets") or [])}


try:
    before, after = load(sys.argv[1]), load(sys.argv[2])
except Exception as exc:                          # noqa: BLE001 - diagnostics
    print(f"  nao foi possivel comparar: {exc}")
    raise SystemExit(0)

b, a = by_id(before), by_id(after)
if not b and not a:
    print("  sem apostas pendentes para comparar.")
    raise SystemExit(0)

grew = same = 0
print(f"  {'aposta':<40} {'antes':>6} {'depois':>7}   KO em")
print(f"  {'-' * 40} {'-' * 6} {'-' * 7}   -----")
for bet_id, row in sorted(a.items()):
    n_after = row.get("snapshots_so_far", 0)
    n_before = (b.get(bet_id) or {}).get("snapshots_so_far", 0)
    mark = ""
    if n_after > n_before:
        grew += 1
        mark = "  <== NOVO SNAPSHOT"
    else:
        same += 1
    print(f"  {row['match'][:40]:<40} {n_before:>6} {n_after:>7}"
          f"   {row.get('minutes_to_kickoff')}min{mark}")

ticks_before = (before.get("process_uptime") or {}).get("closing_job_ticks_this_process")
ticks_after = (after.get("process_uptime") or {}).get("closing_job_ticks_this_process")
print(f"\n  ticks do job: {ticks_before} -> {ticks_after}")
print(f"  ultimo resultado: {(after.get('closing_job') or {}).get('last_result')}")

print()
if grew:
    print(f"  ✅ {grew} evento(s) ganharam um snapshot novo — a captura ESCREVE.")
else:
    print("  ❌ nenhum snapshot novo. A captura correu mas nao escreveu nada —")
    print("     ver 'ultimo resultado' e as mensagens acima para saber porque.")
