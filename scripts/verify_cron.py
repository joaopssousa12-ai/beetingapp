#!/usr/bin/env python3
"""Did the external cron fire between two /api/diag/closing readings?

The tick counter lives in memory and only moves when something actually runs the
closing capture. Nothing in this check touches it, so any increase came from
outside — which is exactly what we are trying to prove.

A caveat the output states plainly: the counter also resets to 0 whenever Render
recycles the process, so a DROP is not a failure of the cron, it is a restart.
"""
import json
import sys


def load(path):
    with open(path) as fh:
        return json.load(fh)


try:
    a, b = load(sys.argv[1]), load(sys.argv[2])
except Exception as exc:                          # noqa: BLE001 - diagnostics
    print(f"  nao foi possivel comparar: {exc}")
    raise SystemExit(0)

t1 = (a.get("process_uptime") or {}).get("closing_job_ticks_this_process")
t2 = (b.get("process_uptime") or {}).get("closing_job_ticks_this_process")
s1 = (a.get("process_uptime") or {}).get("started_at")
s2 = (b.get("process_uptime") or {}).get("started_at")

print(f"  leitura 1   {a.get('now_utc')}   ticks={t1}   processo de {s1}")
print(f"  leitura 2   {b.get('now_utc')}   ticks={t2}   processo de {s2}")
print(f"  ultimo resultado: {(b.get('closing_job') or {}).get('last_result')}")
print(f"  ultimo tick:      {(b.get('closing_job') or {}).get('last_tick_at')}")
for m in ((b.get("closing_job") or {}).get("last_refresh_msgs") or []):
    print(f"      {m}")

print()
if not isinstance(t1, int) or not isinstance(t2, int):
    print("  ❓ contador indisponivel — o endpoint respondeu algo inesperado.")
elif s1 != s2:
    print("  ⚠️  o processo reiniciou entre as duas leituras, por isso o contador")
    print("      voltou a zero. Repete a verificacao — nao e' falha do cron.")
    if t2 > 0:
        print(f"  ✅ mesmo assim ja' registou {t2} tick(s) no processo novo:")
        print("      alguem esta' a chamar a captura de fora. O cron funciona.")
elif t2 > t1:
    print(f"  ✅ o contador subiu sozinho ({t1} -> {t2}) sem nada deste workflow")
    print("      lhe tocar. O cron externo ESTA' a disparar a captura.")
else:
    print(f"  ❌ o contador nao mexeu ({t1}). Nos ~7 minutos desta verificacao o")
    print("      cron externo nao chamou /api/closing/run. Confirma no cron-job.org:")
    print("      o URL tem de ser /api/closing/run (nao /api/stats) e o job ativo.")
