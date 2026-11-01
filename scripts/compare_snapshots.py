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
job = after.get("closing_job") or {}
print(f"\n  ticks do job: {ticks_before} -> {ticks_after}")
print(f"  ultimo resultado: {job.get('last_result')}")

# ── Did the CAPTURE write it, or did something else? ────────────────────────
# A count that went up is not proof on its own. On 2026-11-01 this script printed
# "a captura ESCREVE" about a call whose own message said "0 credits spent" — it
# had fetched nothing, and the new snapshot came from the collection that runs at
# startup (the process had restarted two minutes earlier). Attributing an
# observation to a cause that could not have produced it is the same mistake as
# averaging stale closing lines, so the attribution is now checked.
msgs = " ".join(str(m) for m in (job.get("last_refresh_msgs") or []))
fetched_nothing = "0 credits spent" in msgs or "no TRACKED bet" in msgs

started_before = (before.get("process_uptime") or {}).get("started_at")
started_after = (after.get("process_uptime") or {}).get("started_at")
restarted = bool(started_after) and started_before != started_after

print()
if grew and fetched_nothing:
    print(f"  ⚠ {grew} evento(s) ganharam um snapshot novo, MAS esta chamada nao")
    print("    buscou nada ('0 credits spent'), portanto NAO foi ela que escreveu.")
    if restarted:
        print(f"    O processo reiniciou entre as duas leituras (arranque {started_after}):")
        print("    a escrita veio quase certamente da recolha do arranque.")
    else:
        print(f"    Causa provavel: a recolha de rotina, ou o arranque ({started_after}).")
    print("    Isto NAO prova que a captura do fecho funciona — repetir com um jogo")
    print("    dentro da janela, para a chamada ter mesmo algo para buscar.")
elif grew:
    print(f"  ✅ {grew} evento(s) ganharam um snapshot novo e esta chamada buscou")
    print("     mesmo odds — a captura ESCREVE.")
elif fetched_nothing:
    print("  — nada a fazer: nenhum jogo apostado dentro da janela, 0 creditos gastos.")
    print("    Nao e' uma falha; tambem nao e' prova de que a captura funciona.")
else:
    print("  ❌ a chamada buscou odds mas nenhum snapshot novo apareceu —")
    print("     ver 'ultimo resultado' e as mensagens acima para saber porque.")
