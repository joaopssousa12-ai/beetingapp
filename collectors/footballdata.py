"""
Historical odds collector — football-data.co.uk.

openfootball (the results source) has NO odds, so the backtest had nothing to
work with. football-data.co.uk publishes free per-league/season CSVs with real
B365 / Pinnacle-close / Max / Avg / Over-Under odds + results — the standard
dataset for football betting backtests. This populates football_matches with
those odds so the backtest engine has usable data.

CSV: https://www.football-data.co.uk/mmz4281/{SS}/{DIV}.csv
"""
import csv
import io
import requests

from collectors.database import get_connection, log_collection, USE_POSTGRES

BASE = "https://www.football-data.co.uk/mmz4281/{ss}/{div}.csv"

# Division code -> friendly league name
DIVISIONS = {
    "E0": "Premier League", "E1": "Championship",
    "SP1": "La Liga", "SP2": "La Liga 2",
    "D1": "Bundesliga", "D2": "Bundesliga 2",
    "I1": "Serie A", "I2": "Serie B",
    "F1": "Ligue 1", "F2": "Ligue 2",
    "N1": "Eredivisie", "P1": "Primeira Liga",
    "B1": "Jupiler Pro League", "T1": "Super Lig",
    "G1": "Super League Greece", "SC0": "Scottish Premiership",
}

# Season codes football-data uses (e.g. 2324 = 2023/24)
SEASONS = {
    "2526": "2025-26", "2425": "2024-25", "2324": "2023-24",
    "2223": "2022-23", "2122": "2021-22",
}


def _f(row, *keys):
    """First non-empty float among the given CSV column names, else None."""
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        v = v.strip()
        if v == "":
            continue
        try:
            return float(v)
        except ValueError:
            continue
    return None


def _i(row, key):
    v = (row.get(key) or "").strip()
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _date_iso(s):
    """football-data date is dd/mm/yyyy or dd/mm/yy -> yyyy-mm-dd."""
    s = (s or "").strip()
    if not s:
        return ""
    parts = s.split("/")
    if len(parts) != 3:
        return s
    d, m, y = parts
    if len(y) == 2:
        y = "20" + y
    return f"{y}-{m.zfill(2)}-{d.zfill(2)}"


COLS = [
    "match_id", "date", "season", "league", "league_name",
    "home_team", "away_team", "home_goals", "away_goals", "result",
    "b365_home", "b365_draw", "b365_away",
    "pinnacle_home_close", "pinnacle_draw_close", "pinnacle_away_close",
    "max_home", "max_draw", "max_away",
    "avg_home", "avg_draw", "avg_away",
    "over25_pinnacle", "under25_pinnacle", "over25_max", "over25_avg",
]


def _parse_csv(text, div, league_name, season_label):
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        home = (r.get("HomeTeam") or "").strip()
        away = (r.get("AwayTeam") or "").strip()
        date_iso = _date_iso(r.get("Date"))
        if not home or not away or not date_iso:
            continue
        ftr = (r.get("FTR") or "").strip().upper()  # H/D/A
        if ftr not in ("H", "D", "A"):
            continue
        rows.append({
            "match_id": f"fd_{div}_{date_iso}_{home}_{away}".replace(" ", "_"),
            "date": date_iso, "season": season_label,
            "league": div, "league_name": league_name,
            "home_team": home, "away_team": away,
            "home_goals": _i(r, "FTHG"), "away_goals": _i(r, "FTAG"), "result": ftr,
            # 1X2
            "b365_home": _f(r, "B365H"), "b365_draw": _f(r, "B365D"), "b365_away": _f(r, "B365A"),
            # Pinnacle closing (PSC*); fall back to Pinnacle (PS*) for older seasons
            "pinnacle_home_close": _f(r, "PSCH", "PSH"),
            "pinnacle_draw_close": _f(r, "PSCD", "PSD"),
            "pinnacle_away_close": _f(r, "PSCA", "PSA"),
            "max_home": _f(r, "MaxH", "BbMxH"), "max_draw": _f(r, "MaxD", "BbMxD"),
            "max_away": _f(r, "MaxA", "BbMxA"),
            "avg_home": _f(r, "AvgH", "BbAvH"), "avg_draw": _f(r, "AvgD", "BbAvD"),
            "avg_away": _f(r, "AvgA", "BbAvA"),
            # Over/Under 2.5
            "over25_pinnacle": _f(r, "P>2.5", "B365>2.5"),
            "under25_pinnacle": _f(r, "P<2.5", "B365<2.5"),
            "over25_max": _f(r, "Max>2.5", "BbMx>2.5"),
            "over25_avg": _f(r, "Avg>2.5", "BbAv>2.5"),
        })
    return rows


def _upsert(rows):
    """Insert/replace rows so re-runs refresh odds. Works on SQLite + Postgres."""
    if not rows:
        return 0
    conn = get_connection()
    col_names = ", ".join(COLS)
    n = 0
    if USE_POSTGRES:
        import psycopg2.extras
        update = ", ".join(f"{c}=EXCLUDED.{c}" for c in COLS if c != "match_id")
        sql = (f"INSERT INTO football_matches ({col_names}) VALUES %s "
               f"ON CONFLICT (match_id) DO UPDATE SET {update}")
        values = [tuple(d.get(c) for c in COLS) for d in rows]
        try:
            raw = conn._conn.cursor()
            psycopg2.extras.execute_values(raw, sql, values, page_size=500)
            conn._conn.commit()
            n = len(rows)
        except Exception as e:
            print(f"FOOTBALLDATA_INSERT_ERROR: {e}", flush=True)
            try:
                conn._conn.rollback()
            except Exception:
                pass
    else:
        ph = "(" + ",".join(["?"] * len(COLS)) + ")"
        for d in rows:
            try:
                conn.execute(
                    f"INSERT OR REPLACE INTO football_matches ({col_names}) VALUES {ph}",
                    [d.get(c) for c in COLS],
                )
                n += 1
            except Exception as e:
                print(f"FOOTBALLDATA_INSERT_ERROR: {e}", flush=True)
        conn.commit()
    conn.close()
    return n


def collect_footballdata(status_callback=None, seasons=None, divisions=None):
    """Download historical odds CSVs and upsert into football_matches."""
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    seasons = seasons or SEASONS
    divisions = divisions or DIVISIONS
    total, errors, with_odds = 0, 0, 0
    cb("Collecting historical odds from football-data.co.uk...")
    for ss, season_label in seasons.items():
        for div, league_name in divisions.items():
            url = BASE.format(ss=ss, div=div)
            try:
                resp = requests.get(url, timeout=20)
                if resp.status_code != 200 or not resp.text.strip():
                    continue
                # CSVs are latin-1 encoded
                resp.encoding = "latin-1"
                rows = _parse_csv(resp.text, div, league_name, season_label)
                if not rows:
                    continue
                with_odds += sum(1 for r in rows if r["b365_home"] or r["pinnacle_home_close"]
                                 or r["max_home"] or r["avg_home"])
                n = _upsert(rows)
                total += n
                cb(f"  -> {league_name} {season_label}: {n} rows")
            except Exception as e:
                errors += 1
                cb(f"  -> {league_name} {season_label} ERROR: {e}")

    status = "success" if errors == 0 else "partial"
    log_collection("football-data.co.uk odds", status, total,
                   f"{with_odds} rows with odds, {errors} errors")
    cb(f"football-data.co.uk done: {total} rows ({with_odds} with usable odds).")
    return {"rows": total, "with_odds": with_odds, "errors": errors}
