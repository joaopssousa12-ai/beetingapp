#!/usr/bin/env python3
"""Why did THIS bet not get a real closing line?

For each bet id given, pulls /api/bets/{id}/clv-audit — the full Pinnacle
snapshot timeline for its event, flagged pre/post kickoff — so we can see
whether the near-kickoff refresh ever wrote a snapshot, or whether the only
snapshot available was the morning sweep.

Usage: python3 scripts/clv_capture_audit.py 62 66 [...]
"""
import json
import sys
import urllib.request

BASE = "https://beetingapp-1.onrender.com"


def get(path, timeout=120):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


def main():
    ids = sys.argv[1:] or ["62", "66"]
    for bet_id in ids:
        print("=" * 78)
        print(f"APOSTA #{bet_id}")
        print("=" * 78)
        try:
            d = get(f"/api/bets/{bet_id}/clv-audit")
        except Exception as exc:                      # noqa: BLE001 - diagnostics
            print(f"  erro: {exc}\n")
            continue

        if d.get("ok") is False:
            print(f"  {d.get('error')}\n")
            continue

        bet = d.get("bet") or d
        for key in ("home_team", "away_team", "selection", "odds", "commence_time",
                    "event_id", "pin_close_odds", "pin_close_fair_odds",
                    "pin_close_captured_at", "clv_pct"):
            if key in bet:
                print(f"  {key:24s} {bet[key]}")

        snaps = d.get("snapshots") or d.get("timeline") or []
        print(f"\n  SNAPSHOTS NO HISTORICO DESTE EVENTO: {len(snaps)}")
        if not snaps:
            print("  >>> NENHUM snapshot guardado para este evento.")
        for s in snaps:
            if isinstance(s, dict):
                when = s.get("captured_at") or s.get("ts") or "?"
                pre = s.get("pre_kickoff")
                flag = "PRE " if pre else ("POS " if pre is False else "    ")
                price = (s.get("pin_price") or s.get("price")
                         or s.get("pin_home") or "")
                print(f"    {flag} {when}   {price}")
            else:
                print(f"    {s}")

        # anything else the endpoint returned that we did not print
        extra = {k: v for k, v in d.items()
                 if k not in ("bet", "snapshots", "timeline", "ok")}
        if extra:
            print("\n  OUTROS CAMPOS:")
            print("   ", json.dumps(extra, ensure_ascii=False)[:1500])
        print()


if __name__ == "__main__":
    main()
