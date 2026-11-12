#!/usr/bin/env python3
"""Which historical data sources still answer, and how fresh is each one?

The prognosis model (Elo, form, goal rates, H2H, xG) is only as current as the
results it was fed. Production showed the newest football result dated
2026-05-31 in November — so before fixing the collectors we need to know, for
every source they read, (a) whether it is reachable at all and (b) the date of
the newest finished match it carries. Read-only; touches no BetIQ endpoint and
spends no Odds-API credits.
"""
import csv
import io
import json
import re
import sys
import urllib.request
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"}


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:                            # noqa: BLE001 - diagnostics
        return f"ERR {type(e).__name__}: {str(e)[:80]}", b""


def line(label, status, detail):
    ok = "OK " if status == 200 else "NAO"
    print(f"  [{ok}] {label:<46} HTTP {status}  {detail}")


def season_codes():
    """Current football season in both notations, e.g. ('2627', '2026-27')."""
    now = datetime.utcnow()
    y = now.year if now.month >= 7 else now.year - 1
    return f"{str(y)[2:]}{str(y + 1)[2:]}", f"{y}-{str(y + 1)[2:]}", y


def main():
    code, label, start_year = season_codes()
    year = datetime.utcnow().year
    print(f"hoje (UTC) {datetime.utcnow():%Y-%m-%d}   epoca corrente: {label} (codigo {code})\n")

    print("FUTEBOL — football-data.co.uk (resultados + odds de fecho da Pinnacle)")
    for div in ("E0", "SP1", "D1", "I1", "F1", "P1", "N1", "T1", "B1", "E1", "SP2", "D2"):
        st, body = fetch(f"https://www.football-data.co.uk/mmz4281/{code}/{div}.csv")
        detail = ""
        if st == 200 and body:
            rows = list(csv.DictReader(io.StringIO(body.decode("latin-1"))))
            done = [r for r in rows if (r.get("FTR") or "").strip() in ("H", "D", "A")]
            last = done[-1].get("Date") if done else "—"
            has_psc = sum(1 for r in done if (r.get("PSCH") or "").strip())
            detail = f"{len(done)} jogos, ultimo {last}, {has_psc} com fecho Pinnacle (PSCH)"
        line(f"{div} {label}", st, detail)

    print("\nFUTEBOL — openfootball (resultados, sem odds)")
    for lg in ("en.1", "es.1", "de.1", "it.1", "fr.1", "en.2"):
        st, body = fetch(f"https://raw.githubusercontent.com/openfootball/football.json/master/{label}/{lg}.json")
        detail = ""
        if st == 200 and body:
            ms = json.loads(body).get("matches", [])
            played = [m for m in ms if (m.get("score") or {}) and
                      ((isinstance(m["score"], dict) and m["score"].get("ft")) or isinstance(m["score"], list))]
            last = max((m.get("date", "") for m in played), default="—")
            detail = f"{len(ms)} jogos no ficheiro, {len(played)} com resultado, ultimo {last}"
        line(f"{lg} {label}", st, detail)

    print("\nTENIS — JeffSackmann (resultados; alimenta o Elo de tenis)")
    for tour in ("atp", "wta"):
        for y in (year - 1, year):
            st, body = fetch(f"https://raw.githubusercontent.com/JeffSackmann/tennis_{tour}/master/{tour}_matches_{y}.csv")
            detail = ""
            if st == 200 and body:
                rows = list(csv.DictReader(io.StringIO(body.decode("utf-8", "replace"))))
                last = max((r.get("tourney_date", "") for r in rows), default="—")
                detail = f"{len(rows)} jogos, ultimo torneio iniciado {last}"
            line(f"{tour}_matches_{y}.csv", st, detail)

    print("\nTENIS — tennis-data.co.uk (resultados + odds; hoje so' alimenta o backtest)")
    for tag, url in (("ATP", f"http://www.tennis-data.co.uk/{year}/{year}.xlsx"),
                     ("WTA", f"http://www.tennis-data.co.uk/{year}w/{year}.xlsx"),
                     ("ATP (.xls)", f"http://www.tennis-data.co.uk/{year}/{year}.xls"),
                     ("WTA (.xls)", f"http://www.tennis-data.co.uk/{year}w/{year}.xls")):
        st, body = fetch(url)
        line(f"{tag} {year}", st, f"{len(body)} bytes" if body else "")

    print("\nSELECOES — martj42/international_results")
    st, body = fetch("https://raw.githubusercontent.com/martj42/international_results/master/results.csv")
    detail = ""
    if st == 200 and body:
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8", "replace"))))
        done = [r for r in rows if (r.get("home_score") or "").strip().isdigit()]
        pend = [r for r in rows if not (r.get("home_score") or "").strip().isdigit()]
        wc = [r for r in done if r.get("tournament") == "FIFA World Cup" and r.get("date", "") >= "2026-06-01"]
        detail = (f"{len(rows)} linhas, ultimo resultado {max(r['date'] for r in done)}, "
                  f"{len(pend)} agendados sem resultado, {len(wc)} jogos do Mundial 2026 com resultado")
    line("results.csv", st, detail)

    print("\nxG — understat.com (hoje: tabela team_xg VAZIA em producao)")
    for lg in ("EPL", "La_liga"):
        st, body = fetch(f"https://understat.com/league/{lg}/{start_year}")
        detail = ""
        if st == 200 and body:
            txt = body.decode("utf-8", "replace")
            has = bool(re.search(r"var datesData\s*=\s*JSON\.parse\('", txt))
            detail = (f"{len(txt)} chars; padrao 'datesData' no HTML: {'SIM' if has else 'NAO (o site mudou de formato)'}")
        line(f"league/{lg}/{start_year}", st, detail)
    st, body = fetch(f"https://understat.com/getLeagueData/EPL/{start_year}")
    detail = ""
    if st == 200 and body:
        try:
            d = json.loads(body)
            detail = f"JSON com chaves {list(d.keys())[:4]}; {len(d.get('dates') or [])} jogos"
        except Exception:
            detail = f"{len(body)} bytes, nao-JSON"
    line(f"getLeagueData/EPL/{start_year} (API interna)", st, detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
