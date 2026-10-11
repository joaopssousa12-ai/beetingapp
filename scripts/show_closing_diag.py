#!/usr/bin/env python3
"""Pretty-print /api/diag/closing so the failure mode is obvious at a glance."""
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "closing.json"
try:
    with open(path) as fh:
        d = json.load(fh)
except Exception as exc:                          # noqa: BLE001 - diagnostics
    print(f"  resposta nao e' JSON ({exc}) — o endpoint provavelmente ainda nao "
          f"esta' em producao (falta deploy).")
    raise SystemExit(0)

if "closing_job" not in d:
    print("  /api/diag/closing nao existe nesta versao — FALTA O DEPLOY.")
    print("  resposta:", json.dumps(d)[:300])
    raise SystemExit(0)

up = d.get("process_uptime", {})
job = d.get("closing_job", {})
cfg = d.get("config", {})

print(f"  agora (UTC)            {d.get('now_utc')}")
print(f"  processo arrancou      {up.get('started_at')}")
print(f"  ticks do job NESTE processo  {up.get('closing_job_ticks_this_process')}")
print(f"  ultimo resultado       {job.get('last_result')}")
print(f"  ultimo tick            {job.get('last_tick_at')}")
print(f"  ultimo sucesso         {job.get('last_ok_at')}")
print(f"  fechos preenchidos     {job.get('last_filled')}")
for m in (job.get("last_refresh_msgs") or []):
    print(f"      {m}")
if job.get("last_traceback"):
    print("  TRACEBACK:")
    print("   ", job["last_traceback"].replace("\n", "\n    "))

print(f"\n  config: {json.dumps(cfg)}")
print(f"\n  ensaio do alvo (sem gastar creditos):")
for window, res in (d.get("targeting_dry_run") or {}).items():
    print(f"    {window:8s} -> {res}")

pend = d.get("pending_bets") or []
print(f"\n  APOSTAS PENDENTES: {len(pend)}")
blind = 0
for b in pend:
    ev = b.get("event_in_odds_events")
    seen = "SIM" if ev else "NAO"
    if not ev:
        blind += 1
    print(f"    #{b['id']:<4} {b['match'][:34]:34s} KO em {str(b['minutes_to_kickoff']):>7}min"
          f" | event_id {'ok ' if b['has_event_id'] else 'FALTA'}"
          f" | em odds_events: {seen}"
          f" | snapshots: {b['snapshots_so_far']}")

print()
if blind:
    print(f"  >>> {blind} aposta(s) pendentes cujo evento NAO esta' em odds_events:")
    print("      a captura nao consegue ve-las, nunca terao fecho.")
ticks = up.get("closing_job_ticks_this_process")
if isinstance(ticks, int) and ticks == 0:
    print("  >>> o job ainda nao correu NESTE processo. Num processo que e' reciclado")
    print("      de poucas em poucas horas, isso explica a ausencia de fechos.")
