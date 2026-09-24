#!/usr/bin/env python3
"""Does the LIVE /api/bets response carry the honest-CLV contract fields?

Kept as a file rather than inlined in the workflow: an un-indented Python block
inside a YAML block scalar silently terminates the scalar and makes the whole
workflow file unparseable (which is exactly how this probe failed the first time).
"""
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "one.json"
try:
    with open(path) as fh:
        data = json.load(fh)
except Exception as exc:                      # noqa: BLE001 - diagnostics only
    print(f"  resposta nao e' JSON valido: {exc}")
    raise SystemExit(0)

if not data:
    print("  /api/bets devolveu lista vazia")
    raise SystemExit(0)

bet = data[0]
new_fields = ("clv_quality", "clv_lead_min", "clv_pct_unverified",
              "odds_source", "ref_agreement")
present = 0
for field in new_fields:
    ok = field in bet
    present += ok
    print(f"  [{'OK ' if ok else 'NAO'}] {field}")

print()
if present == len(new_fields):
    print("  => contrato novo ATIVO em producao")
elif present == 0:
    print("  => producao ainda corre o codigo ANTIGO (falta deploy)")
else:
    print(f"  => parcial ({present}/{len(new_fields)}) - deploy incompleto?")
