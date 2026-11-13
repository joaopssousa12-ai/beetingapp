#!/usr/bin/env python3
"""Collect the team names each data source really uses.

The prognosis page reads results from football-data.co.uk ("Man United",
"Ath Madrid", "M'gladbach") but is asked about fixtures named by The Odds API
("Manchester United", "Atlético Madrid", "Borussia Monchengladbach"). Matching
the two by guesswork is how one team's form ended up built from another team's
matches. This prints both vocabularies, as one JSON document, so the resolver
(collectors/teamnames.py) can be written and tested against real names.

  football-data.co.uk  current + previous season, every division we collect
  production           the Odds-API names in the user's own bet history and in
                       whatever odds_events sample the app exposes

Read-only; no Odds-API credits.
"""
import csv
import io
import json
import sys
import urllib.request
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"}
APP = "https://beetingapp-1.onrender.com"
DIVS = ["E0", "E1", "SP1", "SP2", "D1", "D2", "I1", "I2", "F1", "F2", "N1", "P1", "B1", "T1", "G1", "SC0"]


def fetch(url, timeout=60):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:                            # noqa: BLE001 - diagnostics
        return f"ERR {type(e).__name__}", b""


def seasons():
    now = datetime.utcnow()
    y = now.year if now.month >= 7 else now.year - 1
    return [f"{str(y - i)[2:]}{str(y - i + 1)[2:]}" for i in range(3)]


def main():
    out = {"footballdata": {}, "odds_api": {}, "errors": []}
    for div in DIVS:
        names = {}
        for ss in seasons():
            st, body = fetch(f"https://www.football-data.co.uk/mmz4281/{ss}/{div}.csv")
            if st != 200:
                out["errors"].append(f"fd {ss}/{div}: {st}")
                continue
            text = body.decode("utf-8-sig", errors="replace")
            for r in csv.DictReader(io.StringIO(text)):
                for k in ("HomeTeam", "AwayTeam"):
                    n = (r.get(k) or "").strip()
                    if n:
                        names.setdefault(n, ss)          # newest season it appears in
        out["footballdata"][div] = sorted(names)

    # production: retry, the free instance may be waking up
    bets = None
    for _ in range(6):
        st, body = fetch(f"{APP}/api/bets", timeout=90)
        if st == 200:
            try:
                bets = json.loads(body)
            except Exception:                         # noqa: BLE001
                bets = None
            if bets is not None:
                break
    rows = bets if isinstance(bets, list) else (bets or {}).get("bets") or []
    by_sport = {}
    for b in rows:
        sp = b.get("sport_name") or "?"
        for k in ("home_team", "away_team"):
            if b.get(k):
                by_sport.setdefault(sp, set()).add(b[k])
    out["odds_api"]["bets"] = {k: sorted(v) for k, v in sorted(by_sport.items())}
    if not rows:
        out["errors"].append(f"/api/bets: {st}")

    st, body = fetch(f"{APP}/api/debug/pin-odds", timeout=90)
    try:
        sample = json.loads(body).get("odds_events_sample") or []
        out["odds_api"]["events_sample"] = sorted(
            {x.get(k) for x in sample for k in ("home_team", "away_team") if x.get(k)})
    except Exception:                                 # noqa: BLE001
        out["errors"].append(f"/api/debug/pin-odds: {st}")

    print("TEAM_NAMES_JSON_BEGIN")
    print(json.dumps(out, ensure_ascii=False))
    print("TEAM_NAMES_JSON_END")
    print(f"\nfootball-data: {sum(len(v) for v in out['footballdata'].values())} nomes em "
          f"{len(out['footballdata'])} divisoes | Odds API (apostas): "
          f"{sum(len(v) for v in out['odds_api'].get('bets', {}).values())} nomes | erros: {out['errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
