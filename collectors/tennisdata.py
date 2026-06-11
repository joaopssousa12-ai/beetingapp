"""
Historical tennis odds collector — tennis-data.co.uk.

The JeffSackmann collector (tennis.py) has results/rankings/surface but NO odds,
so a backtest on it would hit "0 usable odds" (the same wall football did with
openfootball). tennis-data.co.uk publishes free per-year/tour spreadsheets with
real B365 / Pinnacle / Max / Avg match-winner odds + results — the standard
dataset for tennis betting backtests. This populates `tennis_odds` so the
backtest engine has usable 2-way data.

The files are served with a .xls extension but are actually OOXML (.xlsx =
zip + XML), so we parse them with the stdlib (zipfile + ElementTree) — no pandas,
no openpyxl, zero new dependencies. Columns are read BY HEADER NAME (not position)
so ATP (best-of-5) and WTA (best-of-3) layouts and extra-bookmaker columns
(EX/LB...) all work without special-casing.

ATP: http://www.tennis-data.co.uk/{year}/{year}.xls
WTA: http://www.tennis-data.co.uk/{year}w/{year}.xls
"""
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests

from collectors.database import get_connection, log_collection, USE_POSTGRES

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_EXCEL_EPOCH = datetime(1899, 12, 30)  # Excel day 0 (accounts for the 1900 leap bug)

# tour key -> (friendly name, URL template)
TOURS = {
    "atp": ("ATP", "http://www.tennis-data.co.uk/{y}/{y}.xls"),
    "wta": ("WTA", "http://www.tennis-data.co.uk/{y}w/{y}.xls"),
}

# Pinnacle (PSW/PSL) exists from ~2010; Max/Avg from ~2008; B365 from ~2001.
DEFAULT_YEARS = list(range(2015, datetime.now().year + 1))

COLS = [
    "match_id", "date", "tour", "tournament", "surface", "round", "best_of",
    "winner", "loser", "wrank", "lrank",
    "b365_w", "b365_l", "ps_w", "ps_l", "max_w", "max_l", "avg_w", "avg_l",
]


def _col_letter(ref):
    """'AC12' -> 'AC'."""
    out = []
    for ch in ref:
        if ch.isalpha():
            out.append(ch)
        else:
            break
    return "".join(out)


def _read_xlsx(content):
    """Parse the first worksheet of an .xlsx byte string into a list of row dicts
    keyed by the header row. Stdlib only (handles shared + inline strings)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return []
    names = zf.namelist()

    # Shared strings table (cells with t="s" reference an index here)
    shared = []
    if "xl/sharedStrings.xml" in names:
        try:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root:
                shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
        except ET.ParseError:
            shared = []

    sheet = "xl/worksheets/sheet1.xml"
    if sheet not in names:
        cands = sorted(n for n in names
                       if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not cands:
            return []
        sheet = cands[0]

    try:
        root = ET.fromstring(zf.read(sheet))
    except ET.ParseError:
        return []
    sheet_data = root.find(f"{_NS}sheetData")
    if sheet_data is None:
        return []

    grid = []  # list of {col_letter: value}
    for row in sheet_data.findall(f"{_NS}row"):
        cells = {}
        for c in row.findall(f"{_NS}c"):
            col = _col_letter(c.get("r", ""))
            if not col:
                continue
            t = c.get("t")
            if t == "s":
                v = c.find(f"{_NS}v")
                if v is not None and v.text is not None:
                    try:
                        cells[col] = shared[int(v.text)]
                    except (ValueError, IndexError):
                        cells[col] = ""
            elif t == "inlineStr":
                is_el = c.find(f"{_NS}is")
                cells[col] = ("".join(x.text or "" for x in is_el.iter(f"{_NS}t"))
                              if is_el is not None else "")
            else:
                v = c.find(f"{_NS}v")
                cells[col] = v.text if (v is not None and v.text is not None) else ""
        grid.append(cells)

    if not grid:
        return []
    headers = {col: str(name).strip() for col, name in grid[0].items()
               if name and str(name).strip()}
    rows = []
    for cells in grid[1:]:
        d = {name: cells.get(col, "") for col, name in headers.items()}
        if any(str(v).strip() for v in d.values()):
            rows.append(d)
    return rows


def _excel_date(v):
    """Excel serial number (e.g. 45291) or dd/mm/yyyy -> yyyy-mm-dd."""
    s = str(v).strip()
    if not s:
        return ""
    try:
        return (_EXCEL_EPOCH + timedelta(days=int(float(s)))).strftime("%Y-%m-%d")
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _f(d, *keys):
    """First parseable float among the given column names, else None."""
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        try:
            return float(s)
        except ValueError:
            continue
    return None


def _i(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        try:
            return int(float(s))
        except ValueError:
            continue
    return None


def _parse_rows(raw_rows, tour_name, year):
    out = []
    for r in raw_rows:
        winner = (r.get("Winner") or "").strip()
        loser = (r.get("Loser") or "").strip()
        date_iso = _excel_date(r.get("Date"))
        if not winner or not loser or not date_iso:
            continue
        comment = (r.get("Comment") or "").strip().lower()
        if comment and comment not in ("completed", ""):
            # Skip retirements / walkovers — the "result" isn't a clean on-court win.
            continue
        tourn = (r.get("Tournament") or "").strip()
        rnd = (r.get("Round") or "").strip()
        mid = f"td_{tour_name}_{year}_{date_iso}_{winner}_{loser}".replace(" ", "_")
        out.append({
            "match_id": mid,
            "date": date_iso,
            "tour": tour_name,
            "tournament": tourn,
            "surface": (r.get("Surface") or "").strip() or None,
            "round": rnd or None,
            "best_of": _i(r, "Best of"),
            "winner": winner,
            "loser": loser,
            "wrank": _i(r, "WRank"),
            "lrank": _i(r, "LRank"),
            "b365_w": _f(r, "B365W"), "b365_l": _f(r, "B365L"),
            "ps_w": _f(r, "PSW", "PinW"), "ps_l": _f(r, "PSL", "PinL"),
            "max_w": _f(r, "MaxW"), "max_l": _f(r, "MaxL"),
            "avg_w": _f(r, "AvgW"), "avg_l": _f(r, "AvgL"),
        })
    return out


def _upsert(rows):
    """Insert/replace so re-runs refresh odds. Works on SQLite + Postgres."""
    if not rows:
        return 0
    conn = get_connection()
    col_names = ", ".join(COLS)
    n = 0
    if USE_POSTGRES:
        import psycopg2.extras
        update = ", ".join(f"{c}=EXCLUDED.{c}" for c in COLS if c != "match_id")
        sql = (f"INSERT INTO tennis_odds ({col_names}) VALUES %s "
               f"ON CONFLICT (match_id) DO UPDATE SET {update}")
        values = [tuple(d.get(c) for c in COLS) for d in rows]
        try:
            raw = conn._conn.cursor()
            psycopg2.extras.execute_values(raw, sql, values, page_size=500)
            conn._conn.commit()
            n = len(rows)
        except Exception as e:
            print(f"TENNISDATA_INSERT_ERROR: {e}", flush=True)
            try:
                conn._conn.rollback()
            except Exception:
                pass
    else:
        ph = "(" + ",".join(["?"] * len(COLS)) + ")"
        for d in rows:
            try:
                conn.execute(
                    f"INSERT OR REPLACE INTO tennis_odds ({col_names}) VALUES {ph}",
                    [d.get(c) for c in COLS],
                )
                n += 1
            except Exception as e:
                print(f"TENNISDATA_INSERT_ERROR: {e}", flush=True)
        conn.commit()
    conn.close()
    return n


def collect_tennisdata(status_callback=None, years=None, tours=None):
    """Download historical tennis odds spreadsheets and upsert into tennis_odds."""
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    years = years or DEFAULT_YEARS
    tours = tours or list(TOURS.keys())
    total, errors, with_odds = 0, 0, 0
    cb("Collecting historical tennis odds from tennis-data.co.uk...")
    for tkey in tours:
        if tkey not in TOURS:
            continue
        tour_name, url_tpl = TOURS[tkey]
        for year in years:
            url = url_tpl.format(y=year)
            try:
                resp = requests.get(url, timeout=40)
                if resp.status_code != 200 or len(resp.content) < 1000:
                    continue
                raw = _read_xlsx(resp.content)
                rows = _parse_rows(raw, tour_name, year)
                if not rows:
                    continue
                with_odds += sum(1 for r in rows
                                 if r["ps_w"] or r["b365_w"] or r["max_w"] or r["avg_w"])
                n = _upsert(rows)
                total += n
                cb(f"  -> {tour_name} {year}: {n} rows")
            except Exception as e:
                errors += 1
                cb(f"  -> {tour_name} {year} ERROR: {e}")

    status = "success" if errors == 0 else "partial"
    log_collection("tennis-data.co.uk odds", status, total,
                   f"{with_odds} rows with odds, {errors} errors")
    cb(f"tennis-data.co.uk done: {total} rows ({with_odds} with usable odds).")
    return {"rows": total, "with_odds": with_odds, "errors": errors}


if __name__ == "__main__":
    from collectors.database import init_db
    init_db()
    print(collect_tennisdata(years=[datetime.now().year]))
