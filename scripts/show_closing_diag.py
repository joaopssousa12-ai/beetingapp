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

# Who is driving the capture: the in-app scheduler, the external cron, or both?
sched = d.get("scheduler")
drv = d.get("drivers")
if sched is not None or drv is not None:
    print("\n  QUEM CONDUZ A CAPTURA")
    if sched:
        if sched.get("error"):
            print(f"    agendador interno: ERRO {sched['error']}")
        else:
            print(f"    agendador interno a correr: {'SIM' if sched.get('running') else 'NAO'}")
            for j in sched.get("jobs") or []:
                print(f"      job {j.get('id'):<14} proxima execucao (UTC) {j.get('next_run_utc')}")
    if drv:
        print(f"    ticks internos (agendador)   {drv.get('internal_ticks')}"
              f"   ultimo {drv.get('last_internal_tick_at')}")
        print(f"    chamadas externas (cron)     {drv.get('external_calls')}"
              f"   ultima {drv.get('last_external_call_at')}")
        if drv.get("internal_ticks") == 0:
            print("    >>> o agendador interno ainda nao disparou neste processo"
                  " (normal nos primeiros 15 min apos o arranque).")
        if drv.get("external_calls") == 0:
            print("    >>> NENHUMA chamada externa: o cron-job.org nao esta' a bater em"
                  " /api/closing/run. Sem ele so' resta o agendador interno.")
else:
    print("\n  (esta versao em producao ainda nao reporta quem conduz a captura — falta deploy)")
late = d.get("late_closes_last_24h")
if late:
    print(f"\n  FECHOS TARDIOS/EM FALTA nas ultimas 24h: {json.dumps(late)}")

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
    # Newer builds report the league the capture will fetch (taken from the bet
    # itself). Older builds only had the event row to go on.
    league = b.get("sport_key") if "sport_key" in b else (ev.get("sport_key") if isinstance(ev, dict) else None)
    if not league:
        blind += 1
    print(f"    #{b['id']:<4} {b['match'][:34]:34s} KO em {str(b['minutes_to_kickoff']):>7}min"
          f" | event_id {'ok ' if b['has_event_id'] else 'FALTA'}"
          f" | liga: {league or 'DESCONHECIDA'}"
          f" | em odds_events: {seen}"
          f" | snapshots: {b['snapshots_so_far']}")

print()
if blind:
    print(f"  >>> {blind} aposta(s) pendentes SEM liga conhecida (nem na aposta, nem em odds_events):")
    print("      a captura nao sabe que liga ir buscar — nunca terao fecho.")
ticks = up.get("closing_job_ticks_this_process")
if isinstance(ticks, int) and ticks == 0:
    print("  >>> o job ainda nao correu NESTE processo. Num processo que e' reciclado")
    print("      de poucas em poucas horas, isso explica a ausencia de fechos.")
