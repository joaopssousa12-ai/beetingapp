import sqlite3
import os
import math
import re
import time

# Import new v2 collectors (simplified, no scipy or HTTP)
try:
    from collectors.recent_form import calculate_recent_form
    from collectors.goals_markets import predict_ou_and_btts
except ImportError as e:
    print(f"WARN: New collectors import failed: {e}", flush=True)
    calculate_recent_form = None
    predict_ou_and_btts = None

# ============================================================
# IN-MEMORY CACHE — get_value_bets() is expensive (Poisson + Elo
# per event on every request). Cache results for 5 minutes.
# Invalidated immediately when manual odds are saved/deleted.
# ============================================================
_vb_cache = {"data": None, "ts": 0.0}
_VB_CACHE_TTL = 300  # 5 minutes

# Per-call session caches: populated once at start of get_value_bets(), None when inactive.
# Eliminates 250+ individual Neon connections per call (N+1 problem).
_session_elo = None   # (entity_lower, category, surface) -> rating dict
_session_xg = None    # team_lower -> xg dict
_session_hist = None  # event_id -> [history rows]

def invalidate_value_bets_cache():
    _vb_cache["data"] = None
    _vb_cache["ts"] = 0.0


def _is_placeholder_team(t):
    """TBD bracket placeholders ('1A', '2B', 'W73', 'L101', '3A/B/C/D/F') — no real
    teams, no bettable odds. Mirrors app.js isPlaceholderTeam so they are blocked
    at STORAGE time instead of merely hidden client-side (they were bloating the
    DB and burning engine compute as NO_REFERENCE events)."""
    if not t:
        return True
    s = str(t).strip()
    if "/" in s:
        return True
    if re.fullmatch(r"[WLwl]\d+", s):
        return True
    if re.fullmatch(r"\d+[A-Za-z]?", s):
        return True
    return False


def purge_old_odds_history(days=30):
    """Delete odds_history snapshots older than `days` (the table was unbounded).
    History only matters until a bet's close is captured at kickoff, plus a grace
    window for recapture/backfill — 30 days is ample. Already-recorded CLV is
    UNAFFECTED: captured closes live on the bets rows themselves (pin_close_*);
    only re-deriving the close for bets older than the window stops being
    possible. captured_at and the cutoff share the 'YYYY-MM-DD HH:MM:SS' UTC
    format, so the string comparison is safe on both SQLite and Postgres."""
    from datetime import datetime, timedelta
    conn = get_connection()
    try:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute("DELETE FROM odds_history WHERE captured_at < ?", (cutoff,))
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return n
    finally:
        conn.close()


def purge_out_of_scope_and_stale(max_age_hours=24):
    """Remove junk rows from odds_events: (1) sports we no longer collect (anything
    that isn't football/tennis — MMA/Boxing/etc. lingered for weeks because DELETE
    only removes PAST games, so far-future ones rotted); (2) events whose odds are
    older than `max_age_hours` (dead prices — the books stopped pricing them). Ages
    are computed in Python to stay dialect-safe (SQLite + Postgres)."""
    from datetime import datetime, timedelta
    conn = get_connection()
    try:
        oos = conn.execute(
            "SELECT COUNT(*) AS c FROM odds_events "
            "WHERE sport_key NOT LIKE 'soccer%' AND sport_key NOT LIKE 'tennis%'"
        ).fetchone()["c"]
        conn.execute(
            "DELETE FROM odds_events "
            "WHERE sport_key NOT LIKE 'soccer%' AND sport_key NOT LIKE 'tennis%'"
        )
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        # Never purge an event that still has a pending bet on it. This purge runs
        # at every process start — so on every deploy — and a bet logged on Friday
        # for a Sunday game has a >24h-old event row by Sunday morning. Deleting it
        # removed the game from the closing-capture targeting and from the page.
        try:
            protected = {r["event_id"] for r in conn.execute(
                "SELECT DISTINCT event_id FROM bets "
                "WHERE status = 'pending' AND event_id IS NOT NULL").fetchall()}
        except Exception:
            protected = set()
        stale = 0
        for r in conn.execute("SELECT event_id, updated_at FROM odds_events").fetchall():
            if r["event_id"] in protected:
                continue
            u = r["updated_at"]
            try:
                dt = datetime.strptime(str(u)[:16], "%Y-%m-%d %H:%M") if u else None
            except Exception:
                dt = None
            if dt and dt < cutoff:
                conn.execute("DELETE FROM odds_events WHERE event_id = ?", (r["event_id"],))
                stale += 1
        # (3) TBD bracket placeholders that slipped in before storage-time blocking
        placeholders = 0
        for r in conn.execute("SELECT event_id, home_team, away_team FROM odds_events").fetchall():
            if _is_placeholder_team(r["home_team"]) or _is_placeholder_team(r["away_team"]):
                conn.execute("DELETE FROM odds_events WHERE event_id = ?", (r["event_id"],))
                placeholders += 1
        conn.commit()
        return {"out_of_scope": oos, "stale": stale, "placeholders": placeholders}
    finally:
        conn.close()

# Guard so only one background recompute runs at a time.
_vb_refreshing = {"active": False}


def value_bets_cache_state():
    """Return (data_or_None, is_stale) without triggering a compute.
    Lets the API serve cached picks instantly and decide whether to refresh."""
    data = _vb_cache["data"]
    if data is None:
        return None, True
    stale = (time.time() - _vb_cache["ts"]) >= _VB_CACHE_TTL
    return data, stale


def refresh_value_bets():
    """Recompute value bets in the background (stale-while-revalidate).
    No-op if the cache is already fresh or another refresh is in flight."""
    if _vb_refreshing["active"]:
        return
    _vb_refreshing["active"] = True
    try:
        get_value_bets()  # recomputes only if cache is stale; updates memory + disk
    except Exception as e:
        import traceback
        print(f"VALUE_BETS_REFRESH_ERROR: {e}\n{traceback.format_exc()}", flush=True)
    finally:
        _vb_refreshing["active"] = False

# ============================================================
# DATABASE LAYER — supports both SQLite (local) and PostgreSQL (production)
# If DATABASE_URL is set (Supabase/Render Postgres), use PostgreSQL.
# Otherwise fall back to local SQLite for development.
# ============================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith("postgres")

VOLUME_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", os.path.join(os.path.dirname(__file__), "..", "data"))
DB_PATH = os.path.join(VOLUME_DIR, "betting.db")

# Disk-persisted value-bets cache. Lets the app serve the last computed picks
# instantly after a (slow) Render cold start, while a fresh compute runs in the
# background — so users never stare at "Loading..." waiting for the engine.
_VB_CACHE_FILE = os.path.join(VOLUME_DIR, "vb_cache.json")


def _persist_vb_cache(data):
    """Best-effort write of the value-bets result to disk (survives restarts)."""
    try:
        import json
        os.makedirs(VOLUME_DIR, exist_ok=True)
        tmp = _VB_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "data": data}, f)
        os.replace(tmp, _VB_CACHE_FILE)  # atomic — never leaves a half-written file
    except Exception as e:
        print(f"VB_CACHE_PERSIST_WARN: {e}", flush=True)


def load_vb_cache_from_disk():
    """Populate the in-memory cache from disk if empty. Returns True if loaded.
    Called once at startup so the first request after a cold boot is instant."""
    if _vb_cache["data"] is not None:
        return False
    try:
        import json
        if not os.path.exists(_VB_CACHE_FILE):
            return False
        with open(_VB_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("data"):
            _vb_cache["data"] = payload["data"]
            _vb_cache["ts"] = payload.get("ts", 0.0)
            print(f"VB_CACHE: loaded {len(payload['data'])} events from disk "
                  f"(age {int(time.time() - _vb_cache['ts'])}s)", flush=True)
            return True
    except Exception as e:
        print(f"VB_CACHE_LOAD_WARN: {e}", flush=True)
    return False


def _translate_query(query):
    """Translate SQLite-specific SQL to PostgreSQL syntax."""
    q = query
    # Placeholders: ? -> %s
    q = q.replace("?", "%s")

    # INSERT OR IGNORE -> INSERT INTO ... ON CONFLICT DO NOTHING
    has_ignore = bool(re.search(r"INSERT\s+OR\s+IGNORE\s+INTO\b", q, re.IGNORECASE))
    q = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", q, flags=re.IGNORECASE)
    if has_ignore:
        q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    # INSERT OR REPLACE -> INSERT INTO ... ON CONFLICT (pk) DO UPDATE SET ...
    # Capture table name + column list BEFORE removing OR REPLACE so we can generate
    # the proper ON CONFLICT clause from TABLE_PRIMARY_KEYS.
    replace_m = re.search(
        r"INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]+)\)",
        q, re.IGNORECASE
    )
    q = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO\b", "INSERT INTO", q, flags=re.IGNORECASE)
    if replace_m:
        table = replace_m.group(1)
        cols_raw = replace_m.group(2)
        cols = [c.strip() for c in cols_raw.split(',')]
        pk = TABLE_PRIMARY_KEYS.get(table)
        if pk:
            pk_cols = {c.strip() for c in pk.split(',')}
            update_cols = [c for c in cols if c not in pk_cols]
            if update_cols:
                set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
                q = q.rstrip().rstrip(";") + f" ON CONFLICT ({pk}) DO UPDATE SET {set_clause}"
            else:
                q = q.rstrip().rstrip(";") + f" ON CONFLICT ({pk}) DO NOTHING"
        else:
            q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    # datetime('now', '-1 day') -> NOW() - INTERVAL '1 day'  etc.
    def _dt_modifier(m):
        mod = m.group(1).strip()  # e.g. '-1 day', '+2 hours'
        sign = '-' if mod.startswith('-') else '+'
        amount = mod.lstrip('+-').strip()
        return f"NOW() {sign} INTERVAL '{amount}'"
    q = re.sub(r"datetime\('now',\s*'([^']+)'\)", _dt_modifier, q, flags=re.IGNORECASE)
    # datetime('now') -> NOW()
    q = re.sub(r"datetime\('now'\)", "NOW()", q, flags=re.IGNORECASE)
    # commence_time is stored as TEXT but must be cast to TIMESTAMP for NOW() comparisons.
    # PostgreSQL raises "operator does not exist: text > timestamp" otherwise.
    # Handles both plain column and aliased (b.commence_time, e.commence_time, etc.)
    q = re.sub(
        r'(\b(?:\w+\.)?commence_time)\s*([<>]=?)\s*NOW\b',
        lambda m: f'CAST({m.group(1)} AS TIMESTAMP) {m.group(2)} NOW',
        q, flags=re.IGNORECASE
    )
    # AUTOINCREMENT -> SERIAL (handled in schema translation)
    return q


def _translate_schema(query):
    """Translate CREATE TABLE statements for PostgreSQL."""
    q = query
    q = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", q, flags=re.IGNORECASE)
    # Fix DEFAULT datetime('now') and DEFAULT (datetime('now'))
    q = re.sub(r"DEFAULT\s+\(?datetime\('now'\)\)?", "DEFAULT NOW()", q, flags=re.IGNORECASE)
    q = re.sub(r"datetime\('now'\)", "NOW()", q, flags=re.IGNORECASE)
    q = q.replace("?", "%s")
    return q


# Primary key columns per table (for PostgreSQL upsert)
TABLE_PRIMARY_KEYS = {
    "football_matches": "match_id",
    "tennis_matches": "match_id",
    "tennis_odds": "match_id",
    "national_matches": "match_id",
    "understat_matches": "match_id",
    "team_xg": "team",
    "odds_events": "event_id",
    "elo_ratings": "entity, category, surface",
    "bets": "id",
    "national_xg": "team",
    "national_xg_meta": "key",
    "manual_odds": "event_id, selection",
}


def _extract_table_name(query, keyword="INTO"):
    m = re.search(rf"{keyword}\s+(\w+)", query, flags=re.IGNORECASE)
    return m.group(1) if m else None


class DualAccessRow(dict):
    """A dict row that also supports integer index access (row[0])
    so it's compatible with both SQLite-style and dict-style code."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._values = list(self.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class PostgresCursorWrapper:
    """Wraps a psycopg2 cursor to translate SQLite syntax + provide dict rows."""
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None):
        is_ddl = "CREATE TABLE" in query.upper() or "ALTER TABLE" in query.upper() or "CREATE INDEX" in query.upper()
        if is_ddl:
            q = _translate_schema(query)
        else:
            q = _translate_query(query)

        is_replace = bool(re.search(r"INSERT\s+OR\s+REPLACE", query, flags=re.IGNORECASE))
        is_ignore = bool(re.search(r"INSERT\s+OR\s+IGNORE", query, flags=re.IGNORECASE))

        if (is_replace or is_ignore) and "ON CONFLICT" not in q.upper():
            table = _extract_table_name(query, "INTO")
            pk = TABLE_PRIMARY_KEYS.get(table)
            if is_ignore or not pk:
                q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
            else:
                # Build UPDATE SET for all columns (real upsert / replace)
                cols = self._extract_insert_columns(query)
                pk_cols = [c.strip() for c in pk.split(",")]
                update_cols = [c for c in cols if c not in pk_cols]
                if update_cols:
                    set_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
                    q = q.rstrip().rstrip(";") + f" ON CONFLICT ({pk}) DO UPDATE SET {set_clause}"
                else:
                    q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

        try:
            if params:
                self._cursor.execute(q, params)
            else:
                self._cursor.execute(q)
        except Exception as e:
            msg = str(e).lower()
            if "already exists" in msg or "duplicate column" in msg:
                pass  # Expected during schema upgrades — ignore
            else:
                raise
        return self

    def _extract_insert_columns(self, query):
        """Extract column names from INSERT INTO table (col1, col2, ...) ..."""
        m = re.search(r"INTO\s+\w+\s*\(([^)]+)\)", query, flags=re.IGNORECASE)
        if not m:
            return []
        return [c.strip() for c in m.group(1).split(",")]

    def fetchone(self):
        row = self._cursor.fetchone()
        return DualAccessRow(row) if row is not None else None

    def fetchall(self):
        return [DualAccessRow(r) for r in self._cursor.fetchall()]

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class PostgresConnectionWrapper:
    """Wraps psycopg2 connection to mimic sqlite3.Connection API."""
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        import psycopg2.extras
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return PostgresCursorWrapper(cur)

    def execute(self, query, params=None):
        # sqlite3.Connection.execute is a shortcut — replicate it
        cur = self.cursor()
        cur.execute(query, params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def get_connection():
    if USE_POSTGRES:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        return PostgresConnectionWrapper(conn)
    else:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    try:
        conn = get_connection()
    except Exception as e:
        print(f"DB connection failed: {e}. Falling back to SQLite.", flush=True)
        import sqlite3 as _sq
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = _sq.connect(DB_PATH)
        conn.row_factory = _sq.Row

    # For PostgreSQL, use autocommit for DDL to avoid transaction cascade failures
    if USE_POSTGRES:
        conn._conn.autocommit = True

    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS football_matches (
            match_id TEXT PRIMARY KEY,
            date TEXT,
            season TEXT,
            league TEXT,
            league_name TEXT,
            home_team TEXT,
            away_team TEXT,
            home_goals INTEGER,
            away_goals INTEGER,
            result TEXT,
            home_shots INTEGER,
            away_shots INTEGER,
            home_shots_target INTEGER,
            away_shots_target INTEGER,
            home_corners INTEGER,
            away_corners INTEGER,
            b365_home REAL,
            b365_draw REAL,
            b365_away REAL,
            pinnacle_home_open REAL,
            pinnacle_draw_open REAL,
            pinnacle_away_open REAL,
            pinnacle_home_close REAL,
            pinnacle_draw_close REAL,
            pinnacle_away_close REAL,
            max_home REAL,
            max_draw REAL,
            max_away REAL,
            avg_home REAL,
            avg_draw REAL,
            avg_away REAL,
            over25_pinnacle REAL,
            under25_pinnacle REAL,
            over25_max REAL,
            over25_avg REAL,
            collected_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # Index for the backtest engine: it scans settled matches ordered by date.
    c.execute("CREATE INDEX IF NOT EXISTS idx_fm_date ON football_matches(date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fm_league ON football_matches(league_name)")

    # Historical tennis odds (tennis-data.co.uk) — real B365/Pinnacle/Max/Avg
    # match-winner prices + results, for the 2-way tennis backtest. Separate from
    # tennis_matches (JeffSackmann) which has stats/rankings but NO odds.
    c.execute("""
        CREATE TABLE IF NOT EXISTS tennis_odds (
            match_id TEXT PRIMARY KEY,
            date TEXT,
            tour TEXT,
            tournament TEXT,
            surface TEXT,
            round TEXT,
            best_of INTEGER,
            winner TEXT,
            loser TEXT,
            wrank INTEGER,
            lrank INTEGER,
            b365_w REAL, b365_l REAL,
            ps_w REAL, ps_l REAL,
            max_w REAL, max_l REAL,
            avg_w REAL, avg_l REAL,
            collected_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_to_date ON tennis_odds(date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_to_tour ON tennis_odds(tour)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS tennis_matches (
            match_id TEXT PRIMARY KEY,
            tourney_id TEXT,
            tourney_name TEXT,
            surface TEXT,
            tourney_level TEXT,
            tourney_date TEXT,
            round TEXT,
            best_of INTEGER,
            winner_name TEXT,
            winner_id TEXT,
            winner_hand TEXT,
            winner_age REAL,
            winner_rank INTEGER,
            winner_rank_points INTEGER,
            loser_name TEXT,
            loser_id TEXT,
            loser_hand TEXT,
            loser_age REAL,
            loser_rank INTEGER,
            loser_rank_points INTEGER,
            score TEXT,
            minutes INTEGER,
            w_ace INTEGER,
            w_df INTEGER,
            w_svpt INTEGER,
            w_1stIn INTEGER,
            w_1stWon INTEGER,
            w_2ndWon INTEGER,
            w_bpSaved INTEGER,
            w_bpFaced INTEGER,
            l_ace INTEGER,
            l_df INTEGER,
            l_svpt INTEGER,
            l_1stIn INTEGER,
            l_1stWon INTEGER,
            l_2ndWon INTEGER,
            l_bpSaved INTEGER,
            l_bpFaced INTEGER,
            collected_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS collection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            status TEXT,
            records_added INTEGER,
            message TEXT,
            ran_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS odds_events (
            event_id TEXT PRIMARY KEY,
            sport_key TEXT,
            sport_name TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            pin_home REAL, pin_draw REAL, pin_away REAL,
            best_home REAL, best_draw REAL, best_away REAL,
            x1_home REAL, x1_draw REAL, x1_away REAL,
            pin_over25 REAL, pin_under25 REAL,
            x1_over25 REAL, x1_under25 REAL,
            best_over25 REAL, best_under25 REAL,
            pin_btts_yes REAL, pin_btts_no REAL,
            x1_btts_yes REAL, x1_btts_no REAL,
            best_btts_yes REAL, best_btts_no REAL,
            updated_at TEXT
        )
    """)
    # Add new columns if upgrading from old schema
    for col, typ in [
        ("pin_over25", "REAL"), ("pin_under25", "REAL"),
        ("x1_over25", "REAL"), ("x1_under25", "REAL"),
        ("best_over25", "REAL"), ("best_under25", "REAL"),
        ("pin_btts_yes", "REAL"), ("pin_btts_no", "REAL"),
        ("x1_btts_yes", "REAL"), ("x1_btts_no", "REAL"),
        ("best_btts_yes", "REAL"), ("best_btts_no", "REAL"),
        # Betfair Exchange back prices — primary reference for true probability
        ("bf_home", "REAL"), ("bf_draw", "REAL"), ("bf_away", "REAL"),
        # Bet365 specific odds for edge calculation
        ("b365_home", "REAL"), ("b365_draw", "REAL"), ("b365_away", "REAL"),
        # Asian Handicap (spreads): Pinnacle's main line + prices, the best price
        # among books offering the SAME line, and the user's 1xBet price at that
        # line. ah_line is the HOME handicap (e.g. -1.5).
        ("ah_line", "REAL"),
        ("pin_ah_home", "REAL"), ("pin_ah_away", "REAL"),
        ("best_ah_home", "REAL"), ("best_ah_away", "REAL"),
        ("x1_ah_home", "REAL"), ("x1_ah_away", "REAL"),
    ]:
        try:
            c.execute(f"ALTER TABLE odds_events ADD COLUMN {col} {typ}")
        except Exception:
            pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS understat_matches (
            match_id TEXT PRIMARY KEY,
            date TEXT,
            season TEXT,
            league_key TEXT,
            league_name TEXT,
            home_team TEXT,
            away_team TEXT,
            home_goals INTEGER,
            away_goals INTEGER,
            home_xg REAL,
            away_xg REAL,
            forecast_home REAL,
            forecast_draw REAL,
            forecast_away REAL,
            is_result INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS team_xg (
            team TEXT PRIMARY KEY,
            league TEXT,
            matches_sample INTEGER,
            avg_xg_for REAL,
            avg_xg_against REAL,
            avg_goals_for REAL,
            avg_goals_against REAL,
            xg_overperf_attack REAL,
            xg_overperf_defense REAL,
            last_match_date TEXT
        )
    """)

    # Snapshots of odds for movement tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS odds_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            pin_home REAL, pin_draw REAL, pin_away REAL,
            best_home REAL, best_draw REAL, best_away REAL,
            x1_home REAL, x1_draw REAL, x1_away REAL,
            pin_over25 REAL, pin_under25 REAL,
            pin_btts_yes REAL, pin_btts_no REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_odds_history_event ON odds_history(event_id, captured_at)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS telegram_alerts_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            selection TEXT NOT NULL,
            book TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_tg_alerts ON telegram_alerts_sent(event_id, selection, book, sent_at)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS elo_ratings (
            entity TEXT NOT NULL,
            category TEXT NOT NULL,
            surface TEXT,
            rating REAL,
            games INTEGER,
            updated_at TEXT,
            PRIMARY KEY (entity, category, surface)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS national_matches (
            match_id TEXT PRIMARY KEY,
            date TEXT,
            home_team TEXT,
            away_team TEXT,
            home_goals INTEGER,
            away_goals INTEGER,
            result TEXT,
            tournament TEXT,
            city TEXT,
            country TEXT,
            neutral INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS national_xg (
            team TEXT PRIMARY KEY,
            goals_for_avg REAL,
            goals_against_avg REAL,
            matches_used INTEGER,
            last_update TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS national_xg_meta (
            key TEXT PRIMARY KEY,
            value REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS manual_odds (
            event_id TEXT NOT NULL,
            selection TEXT NOT NULL,
            odd REAL NOT NULL,
            updated_at TEXT,
            PRIMARY KEY (event_id, selection)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placed_at TEXT NOT NULL,
            event_id TEXT,
            sport_name TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            bookmaker TEXT,
            odds REAL NOT NULL,
            stake REAL NOT NULL,
            pin_implied_prob REAL,
            pin_close_odds REAL,
            pin_close_implied REAL,
            status TEXT DEFAULT 'pending',
            result TEXT,
            profit REAL DEFAULT 0,
            notes TEXT,
            settled_at TEXT
        )
    """)

    # Migrate: add edge_pct column to bets if not present
    try:
        c.execute("ALTER TABLE bets ADD COLUMN edge_pct REAL")
    except Exception:
        pass  # already exists

    # Migrate: when the Pinnacle close was captured (audit trail for CLV)
    try:
        c.execute("ALTER TABLE bets ADD COLUMN pin_close_captured_at TEXT")
    except Exception:
        pass  # already exists

    # Migrate: DEVIGGED (no-vig) closing odd — realized CLV is measured against
    # this fair close, not the raw price, so beating the vig doesn't count as value
    try:
        c.execute("ALTER TABLE bets ADD COLUMN pin_close_fair_odds REAL")
    except Exception:
        pass  # already exists

    # Migrate: PROVENANCE of the edge — which reference produced the "true prob"
    # this bet was priced against. Without it we cannot tell a bet backed by a
    # real sharp line (Pinnacle/Betfair) from one whose edge was only our own
    # Elo/xG guess, and those two deserve completely different trust.
    #   odds_source:    blend | pinnacle | betfair | xg_model
    #   ref_agreement:  agree | single | diverge_sharp | diverge_model | model_only
    for _col in ("odds_source", "ref_agreement"):
        try:
            c.execute(f"ALTER TABLE bets ADD COLUMN {_col} TEXT")
        except Exception:
            pass  # already exists

    # Migrate: the Odds-API sport key of the event the bet was placed on, copied
    # onto the bet itself. The closing capture decides which league to fetch from
    # this; it used to JOIN odds_events for it, so a bet whose event row had been
    # purged (the 24h stale purge runs on every restart — i.e. on every deploy)
    # simply dropped out of the targeting and its close was never captured.
    try:
        c.execute("ALTER TABLE bets ADD COLUMN sport_key TEXT")
    except Exception:
        pass  # already exists
    try:
        c.execute(
            "UPDATE bets SET sport_key = (SELECT e.sport_key FROM odds_events e "
            "WHERE e.event_id = bets.event_id) "
            "WHERE sport_key IS NULL AND event_id IS NOT NULL AND status = 'pending'")
    except Exception as e:
        print(f"bets.sport_key backfill skipped: {e}", flush=True)

    # Migrate: normalize bets.commence_time to canonical ISO UTC
    # ('YYYY-MM-DDTHH:MM:SSZ'). Historical rows mixed 'YYYY-MM-DDTHH:MM' (no Z —
    # browsers parse that as LOCAL time, skewing kickoff displays) with other
    # shapes; one canonical format ends the string-comparison bug family.
    # Idempotent: rows already canonical are left untouched.
    try:
        for r in c.execute("SELECT id, commence_time FROM bets WHERE commence_time IS NOT NULL").fetchall():
            ts = _parse_ts(r["commence_time"])
            if not ts:
                continue
            canon = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            if canon != str(r["commence_time"]):
                c.execute("UPDATE bets SET commence_time=? WHERE id=?", (canon, r["id"]))
    except Exception as e:
        print(f"commence_time normalization skipped: {e}", flush=True)

    if USE_POSTGRES:
        conn._conn.autocommit = False
    else:
        conn.commit()
    conn.close()
    print("Database initialised.", flush=True)

def log_collection(source, status, records_added, message=""):
    conn = get_connection()
    conn.execute(
        "INSERT INTO collection_log (source, status, records_added, message) VALUES (?,?,?,?)",
        (source, status, records_added, message)
    )
    conn.commit()
    conn.close()

def get_football_summary():
    conn = get_connection()
    rows = conn.execute("""
        SELECT league_name, COUNT(*) as matches, MIN(date) as from_date, MAX(date) as to_date
        FROM football_matches
        GROUP BY league_name
        ORDER BY league_name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_tennis_summary():
    conn = get_connection()
    rows = conn.execute("""
        SELECT surface, COUNT(*) as matches, MIN(tourney_date) as from_date, MAX(tourney_date) as to_date
        FROM tennis_matches
        GROUP BY surface
        ORDER BY surface
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_recent_football(limit=50):
    conn = get_connection()
    rows = conn.execute("""
        SELECT date, league_name, home_team, away_team,
               home_goals, away_goals, result,
               pinnacle_home_close, pinnacle_draw_close, pinnacle_away_close,
               avg_home, avg_draw, avg_away
        FROM football_matches
        WHERE home_goals IS NOT NULL
        ORDER BY date DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_recent_tennis(limit=50):
    conn = get_connection()
    rows = conn.execute("""
        SELECT tourney_date, tourney_name, surface, round,
               winner_name, loser_name, winner_rank, loser_rank,
               score, minutes
        FROM tennis_matches
        ORDER BY tourney_date DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_collection_log(limit=20):
    conn = get_connection()
    rows = conn.execute("""
        SELECT source, status, records_added, message, ran_at
        FROM collection_log
        ORDER BY ran_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_stats():
    conn = get_connection()
    football_count = conn.execute("SELECT COUNT(*) FROM football_matches").fetchone()[0]
    tennis_count = conn.execute("SELECT COUNT(*) FROM tennis_matches").fetchone()[0]
    football_leagues = conn.execute("SELECT COUNT(DISTINCT league_name) FROM football_matches").fetchone()[0]
    last_collection = conn.execute(
        "SELECT ran_at FROM collection_log ORDER BY ran_at DESC LIMIT 1"
    ).fetchone()
    try:
        odds_count = conn.execute("SELECT COUNT(*) FROM odds_events").fetchone()[0]
    except Exception:
        odds_count = 0
    try:
        national_count = conn.execute("SELECT COUNT(*) FROM national_matches").fetchone()[0]
    except Exception:
        national_count = 0
    try:
        # Events with odds updated in the last 24h — proxy for "live" coverage today
        value_bets_today = conn.execute("""
            SELECT COUNT(*) FROM odds_events
            WHERE commence_time > datetime('now')
        """).fetchone()[0]
    except Exception:
        value_bets_today = 0
    try:
        pending_bets = conn.execute(
            "SELECT COUNT(*) FROM bets WHERE status='pending'"
        ).fetchone()[0]
    except Exception:
        pending_bets = 0
    conn.close()
    return {
        "football_matches": football_count,
        "tennis_matches": tennis_count,
        "football_leagues": football_leagues,
        "value_bets_today": value_bets_today,
        "pending_bets": pending_bets,
        "odds_events": odds_count,
        "national_matches": national_count,
        "last_collection": last_collection[0] if last_collection else "Never"
    }


def implied_prob(odds):
    """Decimal odds -> raw implied probability (includes margin)."""
    if not odds or odds <= 1:
        return None
    return 1.0 / odds


def remove_vig(odds_home, odds_draw, odds_away):
    """
    Remove the bookmaker's vig (margin) to get TRUE probabilities.
    Industry standard: Pinnacle no-vig odds = closest proxy to true probability.
    
    Returns dict with home/draw/away true probabilities (summing to 100%).
    """
    if not odds_home or odds_home <= 1:
        return None
    
    # Implied probabilities (with vig)
    imp_home = 1 / odds_home
    imp_away = 1 / odds_away if odds_away and odds_away > 1 else 0
    imp_draw = 1 / odds_draw if odds_draw and odds_draw > 1 else 0
    
    # Total implied probability (will be > 100% due to vig)
    total = imp_home + imp_draw + imp_away
    if total <= 0:
        return None
    
    # Proportional method (simplest and most used in industry)
    # Removes vig by dividing each probability by total
    true_home = (imp_home / total) * 100
    true_away = (imp_away / total) * 100
    true_draw = (imp_draw / total) * 100 if imp_draw > 0 else None
    
    return {
        "home": round(true_home, 2),
        "draw": round(true_draw, 2) if true_draw is not None else None,
        "away": round(true_away, 2),
        "vig_pct": round((total - 1) * 100, 2),  # bookmaker margin
    }


# The implied probabilities of a complete market sum to ~1.00-1.10 (the excess is
# the bookmaker's margin). Below this the market is missing an outcome.
MIN_PLAUSIBLE_BOOK = 0.94
# A closing line with a margin this fat is not a usable fair-price reference:
# Pinnacle's own 1X2 margin is 2-4% (minor leagues ~6%). Past this the "close" is
# a suspended or junk quote, so the CLV stays pending instead of being computed.
CLOSE_MAX_VIG_PCT = 12.0


def remove_vig_power(odds_home, odds_draw, odds_away):
    """
    Power (Shin) devig — more accurate than proportional for 3-way markets.

    Finds exponent k such that sum(implied^(1/k)) = 1.0 via binary search.
    Distributes vig proportionally to each outcome's implied probability,
    which gives slightly higher true probability to favorites vs proportional.

    Used by OddsJam and professional sharp bettors.
    Falls back to proportional if binary search fails.
    """
    if not odds_home or odds_home <= 1:
        return None

    imp = [1.0 / odds_home]
    if odds_away and odds_away > 1:
        imp.append(1.0 / odds_away)
    if odds_draw and odds_draw > 1:
        imp.insert(1, 1.0 / odds_draw)  # keep home/draw/away order

    raw_vig = sum(imp)
    if raw_vig < MIN_PLAUSIBLE_BOOK:
        # The implied probabilities of a COMPLETE market always add up to about
        # 100% or more. A total far below that means an outcome is missing — the
        # classic case being a three-way football line that arrived without its
        # draw. Normalising home+away to 100% would then hand back "fair" odds 25-40%
        # too short, and a closing line built on that reports a CLV of +30% or more
        # for a bet that had none. Refuse rather than fabricate.
        return None
    if raw_vig <= 1.0:
        # No vig — already fair odds
        s = sum(imp)
        normed = [round(p / s * 100, 2) for p in imp]
        result = {"home": normed[0], "vig_pct": 0.0}
        if odds_draw and odds_draw > 1:
            result["draw"] = normed[1]
            result["away"] = normed[2]
        else:
            result["draw"] = None
            result["away"] = normed[1]
        return result

    # Binary search for power exponent k
    lo, hi = 0.2, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        try:
            total = sum(p ** (1.0 / mid) for p in imp)
        except (ValueError, ZeroDivisionError):
            return None
        if total > 1.0:
            hi = mid
        else:
            lo = mid
    k = (lo + hi) / 2.0

    try:
        true_probs = [p ** (1.0 / k) for p in imp]
    except (ValueError, ZeroDivisionError):
        return None

    s = sum(true_probs)
    if s <= 0:
        return None
    true_probs = [round(p / s * 100, 2) for p in true_probs]

    vig_pct = round((raw_vig - 1.0) * 100, 2)

    if odds_draw and odds_draw > 1:
        return {"home": true_probs[0], "draw": true_probs[1], "away": true_probs[2], "vig_pct": vig_pct}
    return {"home": true_probs[0], "draw": None, "away": true_probs[1], "vig_pct": vig_pct}


# Actionable value-bets window: events further out than this are not analysed
# (early season-opener lines are placeholders → phantom edges, no real close).
VB_MAX_HOURS_AHEAD = 48

# Pinnacle margin threshold above which market is considered illiquid.
# Pinnacle's typical margins: 1.5-2.5% (major leagues), 3-4% (minor).
# Above 4% = Pinnacle doesn't have strong info → trust less.
PINNACLE_MAX_LIQUID_VIG = 4.0

# Sharp-consensus gate: when BOTH Pinnacle and Betfair exist, we blend them into
# one stable fair line and check how far apart their no-vig probabilities are.
# If the largest gap on any outcome is within this many percentage points, the
# two independent sharps "agree" (high trust). Beyond it they "diverge" → the
# true probability is genuinely uncertain, so we don't celebrate it as a green
# value bet. Tunable; 2.5pp ≈ the user's "within ~2%" intuition with a little slack.
REF_AGREE_PP = 2.5

# ── CLV validity ────────────────────────────────────────────────────────────
# A "closing line" captured hours before kickoff is not a closing line. Measured
# on the real bet log, 67% of stored closes were captured MORE THAN 6 HOURS
# before kickoff (median 9h, worst 43h), because the near-kickoff refresh is
# skipped whenever Odds-API quota is low or Render has spun the app down. Those
# snapshots are usually the very same price that produced the entry edge, so the
# resulting "CLV" just restates the edge and silently looks like confirmation.
#
# From here on a CLV only COUNTS as measured when its close was captured within
# CLV_MAX_LEAD_MIN of kickoff. Anything older is still stored and shown, but
# labelled unverified and kept OUT of the averages — an honest gap beats a
# confident-looking number that means nothing.
CLV_GOOD_LEAD_MIN = int(os.environ.get("CLV_GOOD_LEAD_MIN", "60"))    # a real close
CLV_MAX_LEAD_MIN = int(os.environ.get("CLV_MAX_LEAD_MIN", "180"))     # still defensible


def clv_quality(lead_min):
    """Classify a captured close by how long before kickoff it was taken."""
    if lead_min is None:
        return "unknown"
    if lead_min < 0:
        return "post_kickoff"          # should never happen; capture filters it out
    if lead_min <= CLV_GOOD_LEAD_MIN:
        return "good"
    if lead_min <= CLV_MAX_LEAD_MIN:
        return "ok"
    return "stale"


def clv_counts(quality):
    """Only these qualities are allowed into any reported CLV average."""
    return quality in ("good", "ok")

# Teams with massive public following — market odds often biased downward by casual bettors.
# When model probability is significantly lower than market-implied, flag as possible public trap.
POPULAR_CLUBS = {
    # National teams
    "brazil", "argentina", "france", "england", "germany", "spain",
    "portugal", "italy", "netherlands",
    # Club football
    "real madrid", "barcelona", "fc barcelona", "manchester united",
    "manchester city", "liverpool", "chelsea", "arsenal", "tottenham",
    "paris saint-germain", "psg", "juventus", "inter milan", "ac milan",
    "bayern munich", "fc bayern", "borussia dortmund", "atletico madrid",
    "atletico de madrid",
}


def calculate_edge(soft_odd, true_prob_pct):
    """
    Industry-standard edge calculation.

    Formula: edge = (soft_odd × true_prob) - 1
    
    Args:
        soft_odd: The decimal odd from soft bookmaker (e.g. 1xBet)
        true_prob_pct: True probability (0-100%)
    
    Returns:
        Edge as a percentage (e.g. 5.2 means +5.2% edge)
    """
    if not soft_odd or not true_prob_pct or soft_odd <= 1:
        return None
    return (soft_odd * (true_prob_pct / 100) - 1) * 100


def calculate_clv(entry_odd, closing_odd):
    """
    Closing Line Value: did our bet entry odd beat the closing line?
    
    Positive CLV is the strongest predictor of long-term profitability.
    Pros typically achieve 2-4% positive CLV.
    
    Returns CLV percentage. >0 means we got a better price than closing.
    """
    if not entry_odd or not closing_odd or entry_odd <= 1 or closing_odd <= 1:
        return None
    # CLV = (entry_odd / closing_odd - 1) × 100
    return round((entry_odd / closing_odd - 1) * 100, 2)


def kelly_stake(true_prob, odds, fraction=0.25):
    """Quarter-Kelly stake as fraction of bankroll. Returns 0 if no edge."""
    if not odds or not true_prob or odds <= 1:
        return 0
    b = odds - 1
    p = true_prob
    q = 1 - p
    edge = b * p - q
    if edge <= 0:
        return 0
    return max(0, (edge / b) * fraction)


def confidence_score(edge_pct, pin_implied, sport_name, agreement=None):
    """
    Confidence score 1-5 stars based on:
    - Edge sweet spot (2-8% = highest, >15% suspect)
    - Probability magnitude (0.20-0.80 = liquid market)
    - Market quality (top leagues = sharper)
    - Signal agreement (xG/Elo match Pinnacle = high confidence)
    """
    if edge_pct is None or edge_pct < 0:
        return 0
    score = 0
    
    # Edge sweet spot
    if 2 <= edge_pct <= 8:
        score += 3
    elif edge_pct < 2:
        score += 1
    elif edge_pct <= 10:
        score += 2
    elif edge_pct <= 15:
        score += 1
    else:
        # Edge >15% is SUSPECT (likely model error)
        score = 0
        return 0
    
    # Probability magnitude (liquid market)
    if pin_implied is not None and 0.20 <= pin_implied <= 0.80:
        score += 1
    
    # Top tier markets
    if sport_name and any(s in sport_name for s in ["Premier League", "La Liga", "Bundesliga", "Champions", "World Cup", "Serie A", "Ligue", "Wimbledon", "ATP", "WTA"]):
        score += 1
    
    # Signal agreement bonus
    if agreement == "high":
        score += 1
    elif agreement == "low":
        score = max(1, score - 1)  # Penalize divergence
    
    return max(1, min(5, score))


def get_value_bets():
    """Full analysis engine across 1X2, O/U 2.5, BTTS markets."""
    global _session_elo, _session_xg, _session_hist
    # --- TTL cache ---
    now = time.time()
    if _vb_cache["data"] is not None and (now - _vb_cache["ts"]) < _VB_CACHE_TTL:
        return _vb_cache["data"]

    conn = get_connection()
    try:
        # Only analyse upcoming events (3h grace for in-play). Past games are dead
        # weight: they bloat the slow engine and, when a stale cache keeps them, the
        # page shows finished matches and hides the real upcoming ones.
        # Upper bound: the ACTIONABLE betting window. Bookmakers price season
        # openers weeks ahead with thin placeholder lines — a soft price vs an
        # early Pinnacle line produces phantom "edges" (and there is no real
        # closing line until near kickoff), so those games must not be analysed,
        # shown, sorted or alerted as value. They enter the page naturally once
        # they come inside the window.
        rows = conn.execute(
            f"SELECT * FROM odds_events WHERE commence_time > datetime('now', '-3 hours') "
            f"AND commence_time < datetime('now', '+{int(VB_MAX_HOURS_AHEAD)} hours') "
            "ORDER BY commence_time ASC"
        ).fetchall()
    except Exception as e:
        import traceback
        print(f"VALUE_BETS_ERROR: failed to load odds_events: {e}\n{traceback.format_exc()}", flush=True)
        conn.close()
        return []

    # Bulk pre-load all session data in one connection (eliminates N+1 Neon round-trips)
    event_ids = [r["event_id"] for r in rows]
    try:
        elo_rows = conn.execute("SELECT * FROM elo_ratings").fetchall()
        _session_elo = {(r["entity"].lower(), r["category"], r.get("surface") or ''): dict(r) for r in elo_rows}
    except Exception:
        _session_elo = {}
    try:
        xg_rows = conn.execute("SELECT * FROM team_xg").fetchall()
        _session_xg = {r["team"].lower(): dict(r) for r in xg_rows}
    except Exception:
        _session_xg = {}
    try:
        if event_ids:
            placeholders = ",".join(["?" for _ in event_ids])
            hist_rows = conn.execute(
                f"SELECT * FROM odds_history WHERE event_id IN ({placeholders}) ORDER BY event_id, captured_at ASC",
                event_ids
            ).fetchall()
            _session_hist = {}
            for r in hist_rows:
                eid = r["event_id"]
                _session_hist.setdefault(eid, []).append(r)
        else:
            _session_hist = {}
    except Exception:
        _session_hist = {}
    conn.close()

    # Load manual odds once
    manual_odds_map = get_manual_odds_map()

    results = []
    for r in rows:
        d = dict(r)
        picks = []

        # ========== 1X2 — Reference odds priority: Betfair Exchange > Pinnacle > xG ==========
        # Betfair Exchange: peer-to-peer market, 0% bookmaker margin, sharpest reference
        # Pinnacle: sharp bookmaker, ~2% margin, strong secondary reference
        bf_h = d.get("bf_home")
        bf_d = d.get("bf_draw")
        bf_a = d.get("bf_away")
        ph_raw = d.get("pin_home")
        pd_raw = d.get("pin_draw")
        pa_raw = d.get("pin_away")

        # ── A+B: blend Pinnacle + Betfair into ONE stable fair line, and record
        # whether the two sharps AGREE. Using both (instead of switching between
        # them) stops the edge from jumping just because one source updated; the
        # agreement flag lets the UI reserve the green "value" tier for picks that
        # two independent sharp markets CONFIRM. ──
        def _novig(h, dr, a):
            dv = remove_vig_power(h, dr, a)
            if not dv:
                return None
            return {
                "h": dv["home"] / 100,
                "d": (dv["draw"] / 100) if dv.get("draw") is not None else None,
                "a": dv["away"] / 100,
                "vig": dv["vig_pct"],
            }

        pin_nv = _novig(ph_raw, pd_raw, pa_raw) if (ph_raw and pa_raw) else None
        bf_nv = _novig(bf_h, bf_d, bf_a) if (bf_h and bf_a) else None

        true_h = true_d = true_a = None
        if pin_nv and bf_nv:
            # Average the two no-vig lines → stable fair probabilities.
            true_h = (pin_nv["h"] + bf_nv["h"]) / 2
            true_a = (pin_nv["a"] + bf_nv["a"]) / 2
            if pin_nv["d"] is not None and bf_nv["d"] is not None:
                true_d = (pin_nv["d"] + bf_nv["d"]) / 2
            else:
                true_d = pin_nv["d"] if pin_nv["d"] is not None else bf_nv["d"]
            diffs = [abs(pin_nv["h"] - bf_nv["h"]) * 100, abs(pin_nv["a"] - bf_nv["a"]) * 100]
            if pin_nv["d"] is not None and bf_nv["d"] is not None:
                diffs.append(abs(pin_nv["d"] - bf_nv["d"]) * 100)
            max_diff = max(diffs)
            d["ref_max_diff_pp"] = round(max_diff, 1)
            # Two independent SHARP markets: if they disagree, the true prob is
            # genuinely uncertain → block the green tier (handled in the UI).
            d["ref_agreement"] = "agree" if max_diff <= REF_AGREE_PP else "diverge_sharp"
            d["ref_sources"] = "Pinnacle + Betfair"
            d["odds_source"] = "blend"
            d["pin_vig_pct"] = round(min(pin_nv["vig"], bf_nv["vig"]), 2)
        elif bf_nv:
            true_h, true_d, true_a = bf_nv["h"], bf_nv["d"], bf_nv["a"]
            d["ref_agreement"] = "single"
            d["ref_sources"] = "Betfair only"
            d["odds_source"] = "betfair"
            d["pin_vig_pct"] = bf_nv["vig"]
        elif pin_nv:
            true_h, true_d, true_a = pin_nv["h"], pin_nv["d"], pin_nv["a"]
            d["ref_agreement"] = "single"
            d["ref_sources"] = "Pinnacle only"
            d["odds_source"] = "pinnacle"
            d["pin_vig_pct"] = pin_nv["vig"]
        else:
            # NO sharp reference at all: the "fair" line is a pure model estimate.
            # Label it explicitly — the card used to fall back to showing "Pinnacle",
            # which claimed a sharp source that does not exist for this event.
            d["ref_agreement"] = "model_only"
            d["ref_sources"] = "Modelo puro (sem fonte sharp)"
            d["odds_source"] = "xg_model"
            print(f"NO_REFERENCE: {d.get('home_team')} vs {d.get('away_team')} "
                  f"— no Betfair or Pinnacle odds, using xG model", flush=True)

        if true_h is not None and true_a is not None:
            d["true_home_pct"] = round(true_h * 100, 1)
            d["true_draw_pct"] = round(true_d * 100, 1) if true_d else None
            d["true_away_pct"] = round(true_a * 100, 1)
            d["pin_low_liquidity"] = d.get("pin_vig_pct", 0) > PINNACLE_MAX_LIQUID_VIG

            for sel, tp, x1, b365, best, pin_o in [
                (d.get("home_team"), true_h, d.get("x1_home"), d.get("b365_home"), d.get("best_home"), bf_h or ph_raw),
                ("Draw", true_d, d.get("x1_draw"), d.get("b365_draw"), d.get("best_draw"), bf_d or pd_raw),
                (d.get("away_team"), true_a, d.get("x1_away"), d.get("b365_away"), d.get("best_away"), bf_a or pa_raw),
            ]:
                if not tp: continue
                for book, odd in (("1xBet", x1), ("Bet365", b365), ("Best", best)):
                    if not odd: continue
                    edge = (odd * tp - 1) * 100
                    pk = {
                        "market": "Match Result",
                        "selection": sel,
                        "true_prob": round(tp * 100, 1),
                        "pin_odd": pin_o,
                        "book": book,
                        "book_odd": odd,
                        "edge_pct": round(edge, 2),
                        "kelly_pct": round(kelly_stake(tp, odd) * 100, 2),
                    }
                    # Label which real book holds the best price (so the user knows
                    # WHERE to bet — 1xBet is theirs). Best == max across all books.
                    if book == "Best":
                        if x1 and abs(odd - x1) < 1e-9:
                            pk["best_book"] = "1xBet"
                        elif b365 and abs(odd - b365) < 1e-9:
                            pk["best_book"] = "Bet365"
                        else:
                            pk["best_book"] = "outra casa"
                    picks.append(pk)

        # ========== OVER/UNDER 2.5 ==========
        pov, pun = d.get("pin_over25"), d.get("pin_under25")
        if pov and pun:
            # Power devig, like 1X2 and like the closing line this bet's CLV will be
            # measured against. It was proportional here only, so an O/U edge and
            # its own CLV were computed with two different rulers.
            _dv = remove_vig_power(pov, None, pun)
            if _dv:
                true_o = _dv["home"] / 100
                true_u = _dv["away"] / 100
                d["true_over25_pct"] = round(true_o * 100, 1)
                d["true_under25_pct"] = round(true_u * 100, 1)
                for sel, tp, x1, best, pin_o in [
                    ("Over 2.5 Goals", true_o, d.get("x1_over25"), d.get("best_over25"), pov),
                    ("Under 2.5 Goals", true_u, d.get("x1_under25"), d.get("best_under25"), pun),
                ]:
                    for book, odd in (("1xBet", x1), ("Best", best)):
                        if not odd: continue
                        edge = (odd * tp - 1) * 100
                        picks.append({
                            "market": "Over/Under 2.5",
                            "selection": sel,
                            "true_prob": round(tp * 100, 1),
                            "pin_odd": pin_o,
                            "book": book,
                            "book_odd": odd,
                            "edge_pct": round(edge, 2),
                            "kelly_pct": round(kelly_stake(tp, odd) * 100, 2),
                        })

        # ========== BTTS ==========
        pby, pbn = d.get("pin_btts_yes"), d.get("pin_btts_no")
        if pby and pbn:
            _dv = remove_vig_power(pby, None, pbn)     # same ruler as the close
            if _dv:
                true_y = _dv["home"] / 100
                true_n = _dv["away"] / 100
                d["true_btts_yes_pct"] = round(true_y * 100, 1)
                d["true_btts_no_pct"] = round(true_n * 100, 1)
                for sel, tp, x1, best, pin_o in [
                    ("BTTS Yes", true_y, d.get("x1_btts_yes"), d.get("best_btts_yes"), pby),
                    ("BTTS No", true_n, d.get("x1_btts_no"), d.get("best_btts_no"), pbn),
                ]:
                    for book, odd in (("1xBet", x1), ("Best", best)):
                        if not odd: continue
                        edge = (odd * tp - 1) * 100
                        picks.append({
                            "market": "Both Teams to Score",
                            "selection": sel,
                            "true_prob": round(tp * 100, 1),
                            "pin_odd": pin_o,
                            "book": book,
                            "book_odd": odd,
                            "edge_pct": round(edge, 2),
                            "kelly_pct": round(kelly_stake(tp, odd) * 100, 2),
                        })

        # ========== ASIAN HANDICAP (spreads) ==========
        ah_line = d.get("ah_line")
        pah_h, pah_a = d.get("pin_ah_home"), d.get("pin_ah_away")
        if ah_line is not None and pah_h and pah_a:
            dv = remove_vig_power(pah_h, None, pah_a)   # 2-way devig (no draw)
            if dv:
                true_h_ah = dv["home"] / 100
                true_a_ah = dv["away"] / 100
                mkt_lbl = f"Asian Handicap {ah_line:+g}"
                for sel, tp, x1_ah, best_ah, pin_o in [
                    (f"{d.get('home_team')} {ah_line:+g}", true_h_ah, d.get("x1_ah_home"), d.get("best_ah_home"), pah_h),
                    (f"{d.get('away_team')} {-ah_line:+g}", true_a_ah, d.get("x1_ah_away"), d.get("best_ah_away"), pah_a),
                ]:
                    for book, odd in (("1xBet", x1_ah), ("Best", best_ah)):
                        if not odd:
                            continue
                        edge = (odd * tp - 1) * 100
                        picks.append({
                            "market": mkt_lbl,
                            "selection": sel,
                            "true_prob": round(tp * 100, 1),
                            "pin_odd": pin_o,
                            "book": book,
                            "book_odd": odd,
                            "edge_pct": round(edge, 2),
                            "kelly_pct": round(kelly_stake(tp, odd) * 100, 2),
                        })

        # Confidence per pick
        for p in picks:
            pin_implied = 1.0/p["pin_odd"] if p["pin_odd"] else None
            p["confidence"] = confidence_score(p["edge_pct"], pin_implied, d.get("sport_name"))

        d["all_picks"] = picks

        # MOST LIKELY across all markets (prefer 1X2 Best)
        most_likely = None
        h2h_best = [p for p in picks if p["market"] == "Match Result" and p["book"] == "Best"]
        if h2h_best:
            most_likely = max(h2h_best, key=lambda p: p["true_prob"])
        d["most_likely"] = most_likely

        # BEST VALUE: highest credible edge (2-15%), weighted by confidence
        credible = [p for p in picks if p["edge_pct"] is not None and 2 <= p["edge_pct"] <= 15]
        if credible:
            best_value = max(credible, key=lambda p: (p["confidence"], p["edge_pct"]))
        else:
            any_pos = [p for p in picks if p["edge_pct"] is not None and p["edge_pct"] > 0]
            best_value = max(any_pos, key=lambda p: p["edge_pct"]) if any_pos else None
        d["best_value"] = best_value
        d["best_edge"] = best_value["edge_pct"] if best_value else None
        d["best_confidence"] = best_value["confidence"] if best_value else 0

        # === xG SIGNAL (alternative independent model) ===
        # MUST init before the branches: a national/World Cup fixture whose teams
        # can't be modelled (e.g. knockout bracket placeholders "1C", "W85") makes
        # predict_national_match return None, and without this the `else` path leaves
        # xg_sig unbound → UnboundLocalError crashes the WHOLE engine mid-loop (the
        # cache then never refreshes and the page freezes on stale data).
        xg_sig = None
        from collectors.teamnames import competition_kind as _competition_kind
        _kind = _competition_kind(d.get("sport_name"))
        is_national = _kind == "national"
        if is_national:
            # Use national team xG proxy (Dixon-Coles on historical matches)
            from collectors.national_xg import predict_national_match
            try:
                nat_pred = predict_national_match(d.get("home_team"), d.get("away_team"))
            except Exception as e:
                print(f"WARN: national_xg error ({d.get('home_team')} v {d.get('away_team')}): {e}", flush=True)
                nat_pred = None
            if nat_pred:
                xg_sig = {
                    "home": nat_pred["prob_home"],
                    "draw": nat_pred["prob_draw"],
                    "away": nat_pred["prob_away"],
                    "over25": nat_pred["prob_over25"],
                    "under25": nat_pred["prob_under25"],
                    "btts_yes": nat_pred["prob_btts_yes"],
                    "btts_no": nat_pred["prob_btts_no"],
                    "expected_home_goals": nat_pred["lambda_home"],
                    "expected_away_goals": nat_pred["lambda_away"],
                    "source": "national_xg_proxy",
                }
                # If no odds exist (Mundial fixtures without market), populate
                # true probabilities directly so manual odds input has prob to compare against
                if d.get("true_home_pct") is None:
                    d["true_home_pct"] = nat_pred["prob_home"]
                    d["true_draw_pct"] = nat_pred["prob_draw"]
                    d["true_away_pct"] = nat_pred["prob_away"]
                if d.get("true_over25_pct") is None:
                    d["true_over25_pct"] = nat_pred["prob_over25"]
                    d["true_under25_pct"] = nat_pred["prob_under25"]
                if d.get("true_btts_yes_pct") is None:
                    d["true_btts_yes_pct"] = nat_pred["prob_btts_yes"]
                    d["true_btts_no_pct"] = nat_pred["prob_btts_no"]
            else:
                # If national_xg failed, try goals_markets for O/U and BTTS
                if predict_ou_and_btts and not d.get("true_over25_pct"):
                    try:
                        goals_pred = predict_ou_and_btts(d.get("home_team"), d.get("away_team"))
                        if goals_pred:
                            d["true_over25_pct"] = goals_pred["ou_over_25"]
                            d["true_under25_pct"] = goals_pred["ou_under_25"]
                            d["true_btts_yes_pct"] = goals_pred["btts_yes"]
                            d["true_btts_no_pct"] = goals_pred["btts_no"]
                            d["goals_ou_confidence"] = goals_pred["ou_confidence"]
                            d["goals_btts_confidence"] = goals_pred["btts_confidence"]
                    except Exception as e:
                        print(f"WARN: Goals markets error: {e}", flush=True)
        else:
            try:
                xg_sig = xg_based_probability(d.get("home_team"), d.get("away_team"))
            except Exception as e:
                print(f"WARN: xg_based error: {e}", flush=True)
                xg_sig = None
        d["xg_signal"] = xg_sig
        
        # ========== RECENT FORM (4th signal) ==========
        d["home_form_strength"] = "Unknown"
        d["away_form_strength"] = "Unknown"
        if calculate_recent_form and is_national:
            try:
                form_conn = get_connection()
                all_nat = form_conn.execute("SELECT home_team, away_team, home_goals, away_goals FROM national_matches ORDER BY date DESC LIMIT 100").fetchall()
                form_conn.close()
                match_list = [{"home_team": m["home_team"], "away_team": m["away_team"], "home_score": m["home_goals"], "away_score": m["away_goals"]} for m in all_nat]
                
                home_form = calculate_recent_form(d.get("home_team"), match_list, last_n=5)
                away_form = calculate_recent_form(d.get("away_team"), match_list, last_n=5)
                
                if home_form and away_form:
                    d["home_form_strength"] = home_form.get('form_strength', 'Unknown')
                    d["away_form_strength"] = away_form.get('form_strength', 'Unknown')
                    d["form_multiplier_home"] = home_form.get('form_multiplier', 1.0)
                    d["form_multiplier_away"] = away_form.get('form_multiplier', 1.0)
            except Exception as e:
                print(f"WARN: Recent form error: {e}", flush=True)
        
        if xg_sig and d.get("true_home_pct") is not None:
            # How much does xG disagree with Pinnacle?
            d["xg_vs_pin_home"] = round(xg_sig["home"] - d["true_home_pct"], 1)
            d["xg_vs_pin_away"] = round(xg_sig["away"] - d["true_away_pct"], 1)
            # Agreement score: small disagreement = both models agree = stronger signal
            disagree = abs(d["xg_vs_pin_home"]) + abs(d["xg_vs_pin_away"])
            d["xg_pin_agreement"] = "strong" if disagree < 10 else "moderate" if disagree < 25 else "weak"

        # === ELO SIGNAL ===
        sport = (d.get("sport_name") or "").lower()
        elo_sig = None
        try:
            if _kind == "national":
                elo_sig = elo_based_probability(d.get("home_team"), d.get("away_team"), "national")
            elif _kind == "tennis":
                elo_sig = elo_based_probability(d.get("home_team"), d.get("away_team"), "tennis", surface="All")
            else:
                elo_sig = elo_based_probability(d.get("home_team"), d.get("away_team"), "football_club",
                                                sport_name=d.get("sport_name"))
                # Ratings of two different leagues are not on one scale: as a
                # "second opinion" on a European-cup fixture they are noise.
                if elo_sig and elo_sig.get("comparable") is False:
                    elo_sig = None
        except Exception as e:
            print(f"WARN: elo error: {e}", flush=True)
            elo_sig = None
        d["elo_signal"] = elo_sig

        # === BETIQ UNIFIED PROBABILITY (fuse all signals) ===
        pin_probs = None
        if d.get("true_home_pct") is not None:
            pin_probs = {
                "home": d.get("true_home_pct"),
                "draw": d.get("true_draw_pct"),
                "away": d.get("true_away_pct"),
            }
        try:
            betiq = fuse_signals(pin_probs, xg_sig, elo_sig)
        except Exception as e:
            print(f"WARN: fuse_signals error: {e}", flush=True)
            betiq = None
        d["betiq_probs"] = betiq

        # ── A+B v2: when there's no 2nd SHARP market (Betfair), use our own
        # independent MODEL (xG/Elo) as the second opinion. The edge is still vs
        # Pinnacle (the market truth); the model only modulates CONFIDENCE:
        #   agree  → high trust (green, more stars)
        #   diverge_model → Pinnacle is still truth, but flag caution (green, capped stars)
        # (Sharp-vs-sharp 'diverge_sharp' is stronger and blocks green in the UI.)
        if d.get("ref_agreement") == "single" and betiq and betiq.get("n_signals", 0) >= 2:
            agr = betiq.get("agreement")
            if agr == "high":
                d["ref_agreement"] = "agree"
                d["ref_sources"] = "Pinnacle + modelo"
            elif agr == "low":
                d["ref_agreement"] = "diverge_model"
                d["ref_sources"] = "Pinnacle vs modelo"
            else:  # medium / other
                d["ref_sources"] = "Pinnacle + modelo (parcial)"

        # Recompute best value using BetIQ probability (if available and multi-signal)
        if betiq and betiq["n_signals"] >= 2:
            # Re-evaluate edges using fused probability for Match Result picks
            betiq_picks = []
            for p in picks:
                if p["market"] != "Match Result":
                    continue
                sel = p["selection"]
                true_p = None
                if sel == d.get("home_team"): true_p = betiq["home"]
                elif sel == d.get("away_team"): true_p = betiq["away"]
                elif sel == "Draw": true_p = betiq.get("draw")
                if true_p and p.get("book_odd"):
                    betiq_edge = (p["book_odd"] * true_p / 100 - 1) * 100
                    betiq_picks.append({**p, "betiq_true_prob": true_p, "betiq_edge": round(betiq_edge, 2)})
            d["betiq_picks"] = betiq_picks
            # Best BetIQ value pick
            credible_betiq = [p for p in betiq_picks if 2 <= p["betiq_edge"] <= 15]
            if credible_betiq:
                d["betiq_best"] = max(credible_betiq, key=lambda p: p["betiq_edge"])
            else:
                d["betiq_best"] = None

        # === LINE MOVEMENT ===
        movement = get_line_movement(d.get("event_id"))
        d["line_movement"] = movement

        # === SHARP CONFIRMATION ===
        # If best value pick goes same direction as line movement, signal is stronger
        if movement and best_value:
            sel = best_value.get("selection")
            mkt = best_value.get("market")
            home_team = d.get("home_team")
            away_team = d.get("away_team")
            direction = None
            if mkt == "Match Result":
                if sel == home_team: direction = movement["directions"].get("home")
                elif sel == away_team: direction = movement["directions"].get("away")
                elif sel == "Draw": direction = movement["directions"].get("draw")
            elif mkt == "Over/Under 2.5":
                if "Over" in sel: direction = movement["directions"].get("over25")
            elif mkt == "Both Teams to Score":
                if "Yes" in sel: direction = movement["directions"].get("btts_yes")
            # Odd dropped on our selection = sharp money agrees with us
            d["sharp_confirmation"] = (direction == "down")

        # === PUBLIC BIAS DETECTION ===
        # Flag when a popular club's market-implied prob is >10% above our model.
        # Indicates the public is inflating the favourite — value may be on the other side.
        public_bias = None
        home_name = (d.get("home_team") or "").strip()
        away_name = (d.get("away_team") or "").strip()
        import unicodedata as _ud
        def _nl(s):
            return " ".join(_ud.normalize("NFD", s).encode("ascii", "ignore").decode("ascii").lower().split())

        for is_home, team, model_p, market_odd in [
            (True,  home_name, d.get("true_home_pct"), d.get("x1_home") or d.get("b365_home") or d.get("best_home")),
            (False, away_name, d.get("true_away_pct"), d.get("x1_away") or d.get("b365_away") or d.get("best_away")),
        ]:
            if _nl(team) not in POPULAR_CLUBS:
                continue
            if not model_p or not market_odd or market_odd <= 1:
                continue
            market_implied_pct = round(100 / market_odd, 1)
            gap = market_implied_pct - model_p
            if gap >= 8:
                other_team = away_name if is_home else home_name
                public_bias = {
                    "popular_team": team,
                    "other_team": other_team,
                    "market_implied": market_implied_pct,
                    "model_prob": model_p,
                    "gap_pct": round(gap, 1),
                    "severity": "high" if gap >= 15 else "medium",
                }
                break
        d["public_bias"] = public_bias

        # Legacy compatibility for old UI
        d["edges"] = {}
        for p in picks:
            if p["market"] != "Match Result": continue
            book = "1x" if p["book"] == "1xBet" else "best"
            sel = p["selection"]
            if sel == d.get("home_team"): d["edges"][f"home_{book}"] = p["edge_pct"]
            elif sel == d.get("away_team"): d["edges"][f"away_{book}"] = p["edge_pct"]
            elif sel == "Draw"  : d["edges"][f"draw_{book}"] = p["edge_pct"]

        # === BEST PICK + SAFEST PICK ===
        # Build a list of all available market predictions with our model probability
        market_picks = []
        # 1X2
        if d.get("true_home_pct") is not None:
            market_picks.append({
                "market": "Match Result",
                "selection": d.get("home_team"),
                "model_prob": d.get("true_home_pct"),
                "fair_odd": round(100.0 / d["true_home_pct"], 2) if d["true_home_pct"] > 0 else None,
            })
            if d.get("true_draw_pct") is not None and d["true_draw_pct"] > 0:
                market_picks.append({
                    "market": "Match Result",
                    "selection": "Draw",
                    "model_prob": d.get("true_draw_pct"),
                    "fair_odd": round(100.0 / d["true_draw_pct"], 2),
                })
            market_picks.append({
                "market": "Match Result",
                "selection": d.get("away_team"),
                "model_prob": d.get("true_away_pct"),
                "fair_odd": round(100.0 / d["true_away_pct"], 2) if d["true_away_pct"] > 0 else None,
            })
        # Over/Under 2.5
        if d.get("true_over25_pct") is not None:
            market_picks.append({
                "market": "Over/Under 2.5",
                "selection": "Over 2.5 goals",
                "model_prob": d["true_over25_pct"],
                "fair_odd": round(100.0 / d["true_over25_pct"], 2) if d["true_over25_pct"] > 0 else None,
            })
            market_picks.append({
                "market": "Over/Under 2.5",
                "selection": "Under 2.5 goals",
                "model_prob": d["true_under25_pct"],
                "fair_odd": round(100.0 / d["true_under25_pct"], 2) if d["true_under25_pct"] > 0 else None,
            })
        # BTTS
        if d.get("true_btts_yes_pct") is not None:
            market_picks.append({
                "market": "Both Teams To Score",
                "selection": "BTTS Yes",
                "model_prob": d["true_btts_yes_pct"],
                "fair_odd": round(100.0 / d["true_btts_yes_pct"], 2) if d["true_btts_yes_pct"] > 0 else None,
            })
            market_picks.append({
                "market": "Both Teams To Score",
                "selection": "BTTS No",
                "model_prob": d["true_btts_no_pct"],
                "fair_odd": round(100.0 / d["true_btts_no_pct"], 2) if d["true_btts_no_pct"] > 0 else None,
            })

        # Compute target odds (for 2% and 5% edge)
        for p in market_picks:
            if p["fair_odd"]:
                p["target_odd_2pct"] = round(p["fair_odd"] * 1.02, 2)
                p["target_odd_5pct"] = round(p["fair_odd"] * 1.05, 2)

                # Confidence: combines probability magnitude with model agreement
                # Check agreement of available signals for THIS specific selection
                agreement = "high"  # default if only one signal exists
                model_count = 0
                model_probs = []

                # Collect probabilities from each available model for this selection
                # 1X2 selections compare across pin/xg/elo; O/U & BTTS only from xG model.
                if p["market"] == "Match Result":
                    sel = p["selection"]
                    home_t = d.get("home_team")
                    away_t = d.get("away_team")

                    pin_p = None
                    if sel == home_t: pin_p = d.get("true_home_pct")
                    elif sel == away_t: pin_p = d.get("true_away_pct")
                    elif sel == "Draw": pin_p = d.get("true_draw_pct")

                    xg_p = None
                    if xg_sig:
                        if sel == home_t: xg_p = xg_sig.get("home")
                        elif sel == away_t: xg_p = xg_sig.get("away")
                        elif sel == "Draw": xg_p = xg_sig.get("draw")

                    elo_p = None
                    if elo_sig:
                        if sel == home_t: elo_p = elo_sig.get("home")
                        elif sel == away_t: elo_p = elo_sig.get("away")
                        elif sel == "Draw": elo_p = elo_sig.get("draw")

                    for prob in [pin_p, xg_p, elo_p]:
                        if prob is not None:
                            model_probs.append(prob)
                            model_count += 1
                else:
                    # O/U or BTTS — only xG model available
                    model_count = 1

                # Determine agreement quality (only meaningful with 2+ signals)
                if len(model_probs) >= 2:
                    max_diff = max(model_probs) - min(model_probs)
                    if max_diff < 8: agreement = "high"
                    elif max_diff < 18: agreement = "moderate"
                    else: agreement = "low"
                elif len(model_probs) == 1 or model_count == 1:
                    agreement = "single"

                # Base score from probability
                base_conf = 0
                if p["model_prob"] >= 65: base_conf = 3
                elif p["model_prob"] >= 55: base_conf = 2
                elif p["model_prob"] >= 45: base_conf = 1
                else: base_conf = 0

                # Adjustments by model agreement
                if agreement == "high" and len(model_probs) >= 2:
                    base_conf += 2  # Multiple models agree → strong signal
                elif agreement == "moderate":
                    base_conf += 1
                elif agreement == "low":
                    base_conf = max(1, base_conf - 1)  # Models diverge → cap low
                elif agreement == "single":
                    # Single model (no cross-check) → cap at 3 stars
                    pass

                # Hard cap: divergent models can never be 5⭐
                if agreement == "low":
                    base_conf = min(2, base_conf)
                elif agreement == "moderate":
                    base_conf = min(4, base_conf)
                elif agreement == "single":
                    base_conf = min(3, base_conf)

                p["confidence"] = max(1, min(5, base_conf))
                p["agreement"] = agreement
                p["models_used"] = len(model_probs) if model_probs else 1

        # SAFEST PICK: highest model probability (>= 55%)
        # Filter out picks with suspiciously high edges (likely model errors)
        safe_candidates = [
            p for p in market_picks
            if p["model_prob"] is not None and p["model_prob"] >= 55
            and not (p.get("fair_odd") and p.get("manual_odd")
                     and ((p["manual_odd"] * (p["model_prob"]/100) - 1) * 100) > 15)  # edge <15%
        ]
        if safe_candidates:
            d["safest_pick"] = max(safe_candidates, key=lambda p: p["model_prob"])
        else:
            d["safest_pick"] = None

        # BEST PICK: best balance of probability + edge potential
        # Also filter suspicious edges
        best_candidates = [
            p for p in market_picks
            if p["model_prob"] is not None
            and not (p.get("fair_odd") and p.get("manual_odd")
                     and ((p["manual_odd"] * (p["model_prob"]/100) - 1) * 100) > 15)
        ]
        
        # Sweet spot scoring for best picks
        def best_pick_score(p):
            prob = p["model_prob"] or 0
            if 50 <= prob <= 70: bonus = 1.0
            elif 45 <= prob < 50 or 70 < prob <= 80: bonus = 0.7
            else: bonus = 0.4
            return prob * bonus

        if best_candidates:
            d["best_pick"] = max(best_candidates, key=best_pick_score)
        else:
            d["best_pick"] = None

        d["all_market_picks"] = market_picks

        # Update best_confidence to max across best_pick, safest_pick, and best_value
        # (best_value confidence may be low even when model picks are high-confidence)
        conf_vals = [
            (d.get("best_pick") or {}).get("confidence", 0),
            (d.get("safest_pick") or {}).get("confidence", 0),
            d.get("best_confidence", 0),
        ]
        d["best_confidence"] = max(conf_vals)

        # === MANUAL ODDS — persisted user-entered odds ===
        eid = d.get("event_id")
        d["manual_odds"] = manual_odds_map.get(eid, {})

        # Compute "Best Manual Value" — the user's best edge from their entered odds
        best_manual = None
        for p in market_picks:
            stored_odd = d["manual_odds"].get(p["selection"])
            if stored_odd and stored_odd > 1 and p.get("model_prob"):
                edge_pct = (stored_odd * p["model_prob"] / 100 - 1) * 100
                if best_manual is None or edge_pct > best_manual["edge_pct"]:
                    kelly = 0
                    if edge_pct > 0:
                        kelly = (edge_pct / 100) / (stored_odd - 1) * 0.25
                    best_manual = {
                        "market": p["market"],
                        "selection": p["selection"],
                        "model_prob": p["model_prob"],
                        "fair_odd": p["fair_odd"],
                        "manual_odd": stored_odd,
                        "edge_pct": round(edge_pct, 2),
                        "kelly_pct": round(kelly * 100, 2),
                        "confidence": p.get("confidence", 0),
                    }
        d["best_manual_value"] = best_manual

        # Liquidity penalty: if Pinnacle margin > 4%, reduce best_confidence by 1
        if d.get("pin_low_liquidity") and d.get("best_confidence"):
            d["best_confidence"] = max(1, d["best_confidence"] - 1)

        results.append(d)

    results.sort(key=lambda x: x.get("commence_time") or "9999")

    # Store in cache
    _vb_cache["data"] = results
    _vb_cache["ts"] = time.time()
    _persist_vb_cache(results)  # survive cold starts
    # Clear session caches — data is now in _vb_cache
    _session_elo = None
    _session_xg = None
    _session_hist = None
    return results


# ===========================================================
# MANUAL ODDS — User-entered 1xBet odds, persisted per event/selection
# ===========================================================

def save_manual_odd(event_id, selection, odd):
    """Insert or update a manual odd for a (event, selection)."""
    from datetime import datetime
    if odd is None or odd <= 1:
        return False
    conn = get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO manual_odds (event_id, selection, odd, updated_at)
            VALUES (?, ?, ?, ?)
        """, (event_id, selection, float(odd), datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        invalidate_value_bets_cache()
        return True
    finally:
        conn.close()


def delete_manual_odd(event_id, selection):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM manual_odds WHERE event_id = ? AND selection = ?",
                     (event_id, selection))
        conn.commit()
        invalidate_value_bets_cache()
        return True
    finally:
        conn.close()


def get_manual_odds_map():
    """Returns dict {event_id: {selection: odd}} for all stored manual odds."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT event_id, selection, odd FROM manual_odds").fetchall()
        result = {}
        for r in rows:
            eid = r["event_id"] if "event_id" in r.keys() else r[0]
            sel = r["selection"] if "selection" in r.keys() else r[1]
            o = r["odd"] if "odd" in r.keys() else r[2]
            result.setdefault(eid, {})[sel] = o
        return result
    finally:
        conn.close()


# ===========================================================
# BET TRACKER — Functions for managing user bets
# ===========================================================

def add_bet(bet_data):
    """Insert a new bet. Returns the new bet ID."""
    from datetime import datetime
    conn = get_connection()
    cur = conn.cursor()

    pin_implied = None
    if bet_data.get("pin_implied_prob"):
        pin_implied = bet_data["pin_implied_prob"]

    edge_pct = None
    if bet_data.get("edge_pct") is not None:
        try:
            edge_pct = float(bet_data["edge_pct"])
        except (ValueError, TypeError):
            pass

    # Canonical timestamp: commence_time is stored as ISO UTC 'YYYY-MM-DDTHH:MM:SSZ'
    # regardless of what shape the client sent — one format ends the class of bugs
    # where mixed formats get string-compared (CLV close capture, auto-grade).
    # An unparseable or empty value is stored as NULL, never as '' — an empty
    # string passes "IS NOT NULL" and then breaks every timestamp cast on it.
    _ct = _parse_ts(bet_data.get("commence_time"))
    commence = _ct.strftime("%Y-%m-%dT%H:%M:%SZ") if _ct else None

    # Provenance of the edge (see the bets migration). Constrained to the known
    # vocabulary so a malformed client can't poison the split later.
    _SOURCES = {"blend", "pinnacle", "betfair", "xg_model"}
    _AGREEMENTS = {"agree", "single", "diverge_sharp", "diverge_model", "model_only"}
    odds_source = bet_data.get("odds_source")
    odds_source = odds_source if odds_source in _SOURCES else None
    ref_agreement = bet_data.get("ref_agreement")
    ref_agreement = ref_agreement if ref_agreement in _AGREEMENTS else None

    # The league this bet belongs to, copied from the event while its row still
    # exists. The closing capture targets by THIS column, so the bet stays
    # capturable even if the event row is purged before kickoff. When the event
    # also carries a kickoff time and the client sent none, use the event's.
    event_id = bet_data.get("event_id") or None
    sport_key = None
    if event_id:
        try:
            ev = cur.execute(
                "SELECT sport_key, commence_time FROM odds_events WHERE event_id = ?",
                (event_id,)).fetchone()
            if ev:
                sport_key = ev["sport_key"]
                if commence is None:
                    _et = _parse_ts(ev["commence_time"])
                    commence = _et.strftime("%Y-%m-%dT%H:%M:%SZ") if _et else None
        except Exception as e:                        # noqa: BLE001 - optional enrichment
            print(f"add_bet: event lookup failed: {e}", flush=True)

    sql = """
        INSERT INTO bets (
            placed_at, event_id, sport_name, home_team, away_team, commence_time,
            market, selection, bookmaker, odds, stake,
            pin_implied_prob, edge_pct, notes, odds_source, ref_agreement, sport_key
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    params = (
        bet_data.get("placed_at") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        event_id,
        bet_data.get("sport_name"),
        bet_data.get("home_team"),
        bet_data.get("away_team"),
        commence,
        bet_data.get("market", "h2h"),
        bet_data.get("selection"),
        bet_data.get("bookmaker"),
        float(bet_data.get("odds", 0)),
        float(bet_data.get("stake", 0)),
        pin_implied,
        edge_pct,
        bet_data.get("notes"),
        odds_source,
        ref_agreement,
        sport_key,
    )
    if USE_POSTGRES:
        # psycopg2's lastrowid is the row OID (0/None on modern tables), so the
        # API was answering {"ok": true, "id": null} in production.
        cur.execute(sql.rstrip() + " RETURNING id", params)
        bet_id = cur.fetchone()["id"]
    else:
        cur.execute(sql, params)
        bet_id = cur.lastrowid
    conn.commit()
    conn.close()
    return bet_id


def find_recent_duplicate_bet(bet_data, within_seconds=120):
    """Id of an identical PENDING bet logged in the last `within_seconds`, or None.

    Guards the double submit. The bet form had no in-flight lock, so a second
    click (or a retry after a slow response) logged the same bet twice: in the
    real log #93/#94 are the same game, selection, odd and stake, and ids 94,
    104, 106 and 107 were all deleted by hand afterwards. A duplicate counts
    twice in every statistic, including the CLV average.

    "Identical" = same event (or same teams when there is no event), market,
    selection, odd and stake. Two genuinely separate bets on one selection are
    never placed within two minutes at the same odd and stake.
    """
    from datetime import datetime, timedelta
    try:
        odds = float(bet_data.get("odds") or 0)
        stake = float(bet_data.get("stake") or 0)
    except (TypeError, ValueError):
        return None
    cutoff = (datetime.utcnow() - timedelta(seconds=within_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        # placed_at and the cutoff share 'YYYY-MM-DD HH:MM:SS', so the string
        # comparison is exact on both SQLite and Postgres.
        rows = conn.execute(
            "SELECT id, event_id, home_team, away_team, market, selection, odds, stake "
            "FROM bets WHERE status = 'pending' AND placed_at >= ? ORDER BY id DESC",
            (cutoff,)).fetchall()
    finally:
        conn.close()
    want_event = bet_data.get("event_id") or None
    want_market = bet_data.get("market", "h2h")
    want_sel = (bet_data.get("selection") or "").strip().lower()
    for r in rows:
        if (r["selection"] or "").strip().lower() != want_sel or r["market"] != want_market:
            continue
        if abs((r["odds"] or 0) - odds) > 1e-9 or abs((r["stake"] or 0) - stake) > 1e-9:
            continue
        if want_event:
            if r["event_id"] != want_event:
                continue
        elif (r["event_id"] or (r["home_team"] or "") != (bet_data.get("home_team") or "")
              or (r["away_team"] or "") != (bet_data.get("away_team") or "")):
            continue
        return r["id"]
    return None


def update_bet_result(bet_id, result):
    """Mark bet as Won/Lost/Push and compute profit."""
    conn = get_connection()
    cur = conn.cursor()
    row = cur.execute("SELECT odds, stake FROM bets WHERE id = ?", (bet_id,)).fetchone()
    if not row:
        conn.close()
        return False
    odds = row["odds"] or 0
    stake = row["stake"] or 0
    if result == "won":
        profit = round(stake * (odds - 1), 2)
        status = "settled"
    elif result == "lost":
        profit = -stake
        status = "settled"
    elif result == "push":
        profit = 0.0
        status = "settled"
    else:
        profit = 0.0
        status = "pending"

    from datetime import datetime
    cur.execute("""
        UPDATE bets SET result=?, profit=?, status=?, settled_at=?
        WHERE id=?
    """, (result, profit, status, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), bet_id))
    conn.commit()
    conn.close()
    return True


def delete_bet(bet_id):
    conn = get_connection()
    conn.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
    conn.commit()
    conn.close()
    return True


def get_bets(limit=200):
    """Return all bets, newest first, with CLV calculated where available."""
    from datetime import datetime
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM bets ORDER BY placed_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    bets = []
    for r in rows:
        d = dict(r)
        # Realized CLV = your odds vs the DEVIGGED Pinnacle close (no-vig CLV):
        # CLV % = (your_odds / pin_close_fair_odds - 1) × 100. Measured against the
        # raw close a bet could look +CLV while sitting inside the bookmaker's
        # margin (~2-4pp inflation) — no-vig is the honest ruler: >0 means real
        # value at the close.
        # CLV is published once the match has STARTED, settled or not. It compares
        # the price you took with the closing price, and both are known at kickoff
        # — waiting for the result threw away the one property that makes CLV
        # useful: it arrives fast. On 2026-11-01 bet #92 had a close captured 4m49s
        # before kickoff and a valid +5.93% CLV, and My Bets showed a dash, which
        # looks exactly like "withheld because the close is stale".
        #
        # The old rule required status='settled'. Its stated reason was that a
        # scheduled start time can be wrong (tennis especially), making a "close"
        # premature — but settling does not correct commence_time, so that risk is
        # identical before and after. What the kickoff check genuinely protects
        # against is a match that never happened at the stored time (postponed), so
        # that is what we check, directly, instead of using settlement as a proxy.
        #
        # VALIDITY GATE: a close captured long before kickoff is not a close. We
        # compute how many minutes before kickoff the snapshot was taken and only
        # publish clv_pct when that lead time is defensible. Stale ones are still
        # returned — as clv_pct_unverified + clv_lead_min — so the UI can show the
        # number greyed out with the reason, instead of either hiding the problem
        # or presenting a meaningless figure as if it were evidence.
        lead_min = None
        ko = _parse_ts(d.get("commence_time"))
        cap = _parse_ts(d.get("pin_close_captured_at"))
        if ko and cap:
            lead_min = round((ko - cap).total_seconds() / 60.0)
        d["clv_lead_min"] = lead_min
        quality = clv_quality(lead_min)
        d["clv_quality"] = quality

        raw = None
        kicked_off = bool(ko) and ko <= datetime.utcnow()
        if (d.get("status") == "settled" or kicked_off) \
                and d.get("odds") and d.get("pin_close_fair_odds"):
            raw = round((d["odds"] / d["pin_close_fair_odds"] - 1) * 100, 2)

        if raw is not None and clv_counts(quality):
            d["clv_pct"] = raw
            d["clv_pct_unverified"] = None
        else:
            d["clv_pct"] = None
            d["clv_pct_unverified"] = raw
        bets.append(d)
    return bets


def get_bet_stats():
    """Return aggregated stats: total bets, ROI, profit, win rate, CLV avg."""
    conn = get_connection()

    settled = conn.execute("""
        SELECT COUNT(*) as n,
               SUM(stake) as total_staked,
               SUM(profit) as total_profit,
               SUM(CASE WHEN result='won' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN result='lost' THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN result='push' THEN 1 ELSE 0 END) as pushes
        FROM bets WHERE status='settled'
    """).fetchone()

    pending = conn.execute("""
        SELECT COUNT(*) as n, SUM(stake) as staked
        FROM bets WHERE status='pending'
    """).fetchone()

    # CLV stats — every bet whose match has STARTED and has a real captured close,
    # exactly the rule get_bets() uses per bet. Settlement is not required: CLV
    # does not depend on the result, and excluding live bets only delayed the
    # headline for no gain. No-vig: measured against the DEVIGGED close, same
    # ruler as the per-bet clv_pct.
    #
    # Same validity gate as get_bets(): a close captured hours before kickoff is
    # not a close, so it must not enter the headline average. Averaging stale
    # closes was making the dashboard report a confident "avg CLV" that largely
    # just restated the entry edge.
    # The "has it started" test is done in Python, not SQL: on Postgres the
    # translation layer casts commence_time to TIMESTAMP, and a single malformed
    # value in that column would abort the whole statement and take the stats
    # endpoint down with it.
    from datetime import datetime as _dt
    clv = conn.execute("""
        SELECT odds, pin_close_fair_odds, commence_time, pin_close_captured_at, status
        FROM bets
        WHERE pin_close_fair_odds IS NOT NULL AND odds IS NOT NULL
    """).fetchall()

    _now = _dt.utcnow()
    clv_values, n_stale = [], 0
    for row in clv:
        ko = _parse_ts(row["commence_time"])
        if row["status"] != "settled" and not (ko and ko <= _now):
            continue                      # match not started yet: no close exists
        try:
            value = (row["odds"] / row["pin_close_fair_odds"] - 1) * 100
        except Exception:
            continue
        cap = _parse_ts(row["pin_close_captured_at"])
        lead = round((ko - cap).total_seconds() / 60.0) if (ko and cap) else None
        if clv_counts(clv_quality(lead)):
            clv_values.append(value)
        else:
            n_stale += 1

    avg_clv = round(sum(clv_values) / len(clv_values), 2) if clv_values else None
    positive_clv_pct = round(sum(1 for v in clv_values if v > 0) / len(clv_values) * 100, 1) if clv_values else None

    conn.close()

    total_staked = settled["total_staked"] or 0
    total_profit = settled["total_profit"] or 0
    roi = round((total_profit / total_staked * 100), 2) if total_staked > 0 else 0
    n_settled = settled["n"] or 0
    decisive = (settled["wins"] or 0) + (settled["losses"] or 0)
    win_rate = round((settled["wins"] or 0) / decisive * 100, 1) if decisive > 0 else 0

    return {
        "n_settled": n_settled,
        "n_pending": pending["n"] or 0,
        "pending_stake": round(pending["staked"] or 0, 2),
        "total_staked": round(total_staked, 2),
        "total_profit": round(total_profit, 2),
        "roi": roi,
        "wins": settled["wins"] or 0,
        "losses": settled["losses"] or 0,
        "pushes": settled["pushes"] or 0,
        "win_rate": win_rate,
        "avg_clv": avg_clv,
        "positive_clv_rate": positive_clv_pct,
        "clv_sample": len(clv_values),
        # How many settled bets HAVE a stored close that we refused to count
        # because it was captured too long before kickoff. Surfaced in My Bets so
        # a small clv_sample reads as "the capture is broken", not "no data yet".
        "clv_stale": n_stale,
        "clv_max_lead_min": CLV_MAX_LEAD_MIN,
    }


def get_bankroll_evolution():
    """Return list of settled bets for bankroll chart, with result + selection for markers."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT settled_at, profit, result, selection, home_team, away_team FROM bets
        WHERE status='settled' AND settled_at IS NOT NULL
        ORDER BY settled_at ASC
    """).fetchall()
    conn.close()

    cumulative = 0
    series = []
    for r in rows:
        cumulative += r["profit"] or 0
        series.append({
            "date": r["settled_at"],
            "result": r["result"],
            "selection": r["selection"] or '',
            "profit": round(r["profit"] or 0, 2),
            "cumulative": round(cumulative, 2),
            "delta": round(r["profit"] or 0, 2),
        })
    return series


def _parse_ts(value):
    """Tolerant timestamp parser for the mixed formats stored in the DB:
    'YYYY-MM-DD HH:MM[:SS]', 'YYYY-MM-DDTHH:MM[:SS][Z]', with optional
    fractional seconds / '+00:00' offset, or a datetime object.
    Returns a naive UTC datetime, or None."""
    from datetime import datetime as _dt
    if value is None:
        return None
    if isinstance(value, _dt):
        return value.replace(tzinfo=None)
    s = str(value).strip().replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1]
    plus = s.find("+", 10)
    if plus != -1:
        s = s[:plus]
    for length, fmt in ((19, "%Y-%m-%d %H:%M:%S"), (16, "%Y-%m-%d %H:%M")):
        try:
            return _dt.strptime(s[:length], fmt)
        except ValueError:
            continue
    return None


def _pin_price_for_bet(snapshot, market, sel, home_lower, away_lower):
    """Pinnacle price in an odds_history snapshot for this bet's market/selection."""
    # Match Result / 1X2
    if market in ("h2h", "match result", "1x2"):
        if sel == home_lower or sel in ("home", "1"):
            return snapshot["pin_home"]
        if sel == away_lower or sel in ("away", "2"):
            return snapshot["pin_away"]
        if sel in ("draw", "empate", "x"):
            return snapshot["pin_draw"]
    # Over/Under 2.5
    elif market in ("over_under", "over/under 2.5", "totals"):
        if "over" in sel:
            return snapshot["pin_over25"]
        if "under" in sel:
            return snapshot["pin_under25"]
    # BTTS
    elif market in ("btts", "both teams to score"):
        if "yes" in sel or "sim" in sel:
            return snapshot["pin_btts_yes"]
        if "no" in sel or "não" in sel or "nao" in sel:
            return snapshot["pin_btts_no"]
    return None


def _fair_close_prob(snapshot, market, sel, home_lower, away_lower, three_way=False):
    """Devigged (no-vig, power method) close probability for the bet's selection,
    from a full odds_history snapshot. Requires the counter-price(s) so the vig
    can actually be stripped — returns None when they're missing rather than
    normalizing a one-sided market to 100%.

    three_way=True says this event is a 1X2 market (some snapshot of it carries a
    draw price), so a snapshot WITHOUT the draw is incomplete and must not be
    devigged as if it were a two-way line. A margin above CLOSE_MAX_VIG_PCT is
    refused too: that is a suspended or junk quote, not a closing line."""
    if market in ("h2h", "match result", "1x2"):
        if not (snapshot["pin_home"] and snapshot["pin_away"]):
            return None
        if (three_way or sel in ("draw", "empate", "x")) and not snapshot["pin_draw"]:
            return None
        fair = remove_vig_power(snapshot["pin_home"], snapshot["pin_draw"], snapshot["pin_away"])
        if not fair or (fair.get("vig_pct") or 0) > CLOSE_MAX_VIG_PCT:
            return None
        if sel == home_lower or sel in ("home", "1"):
            p = fair["home"]
        elif sel == away_lower or sel in ("away", "2"):
            p = fair["away"]
        elif sel in ("draw", "empate", "x"):
            p = fair["draw"]
        else:
            p = None
    elif market in ("over_under", "over/under 2.5", "totals"):
        if not (snapshot["pin_over25"] and snapshot["pin_under25"]):
            return None
        fair = remove_vig_power(snapshot["pin_over25"], None, snapshot["pin_under25"])
        if not fair or (fair.get("vig_pct") or 0) > CLOSE_MAX_VIG_PCT:
            return None
        p = fair["home"] if "over" in sel else fair["away"] if "under" in sel else None
    elif market in ("btts", "both teams to score"):
        if not (snapshot["pin_btts_yes"] and snapshot["pin_btts_no"]):
            return None
        fair = remove_vig_power(snapshot["pin_btts_yes"], None, snapshot["pin_btts_no"])
        if not fair or (fair.get("vig_pct") or 0) > CLOSE_MAX_VIG_PCT:
            return None
        if "yes" in sel or "sim" in sel:
            p = fair["home"]
        elif "no" in sel or "não" in sel or "nao" in sel:
            p = fair["away"]
        else:
            p = None
    else:
        return None
    return (p / 100.0) if p else None


# After kickoff the close is normally final. For this long we still re-derive it,
# because the kickoff itself can move: The Odds API reschedules tennis matches
# through the day and parse_and_store() copies the new time onto the bet. A
# close captured at the ORIGINAL time is then early, and the later pre-match
# snapshots that follow have to be allowed to replace it.
CLOSE_RECHECK_HOURS = int(os.environ.get("CLOSE_RECHECK_HOURS", "6"))
# A bet whose best pre-kickoff snapshot is older than this did not get a real
# closing line. Telegram is told once, so a broken capture is noticed the same
# day instead of weeks later in an audit (which is how it was found last time).
CLOSE_ALERT_LEAD_MIN = int(os.environ.get("CLOSE_ALERT_LEAD_MIN", "30"))
_close_alerted = set()          # bet ids already reported (this process)
LAST_CAPTURE_REPORT = {"at": None, "late": []}


def _alert_late_close(bet, lead_min):
    """Best-effort Telegram warning for a missed/late closing capture. Never raises."""
    try:
        from collectors.telegram_alerts import _send
        if lead_min is None:
            what = "<b>nenhum snapshot</b> da Pinnacle antes do pontapé. Esta aposta fica sem CLV."
        elif lead_min > CLV_MAX_LEAD_MIN:
            what = (f"melhor snapshot a <b>{lead_min:.0f} min</b> do pontapé — "
                    "demasiado cedo, o CLV desta aposta NÃO vai contar.")
        else:
            what = (f"melhor snapshot a <b>{lead_min:.0f} min</b> do pontapé — "
                    "conta, mas não é um fecho a sério (o normal são ~5 min).")
        _send("⚠️ <b>BetIQ — fecho não capturado a tempo</b>\n"
              f"#{bet['id']} {bet['home_team']} v {bet['away_team']} — {bet['selection']}\n"
              f"{what}\n"
              "Vê se o cron-job.org está ativo e se há créditos da Odds API.")
    except Exception as e:                            # noqa: BLE001 - never break capture
        print(f"LATE_CLOSE_ALERT failed: {e}", flush=True)


def capture_pinnacle_close_for_started_events(recapture=False):
    """
    Capture the TRUE closing Pinnacle odds (last snapshot at/before kickoff)
    from odds_history. Supports 1X2, Over/Under 2.5, and BTTS markets.
    Run automatically after odds refresh.

    Timestamps are compared in Python (_parse_ts), NOT in SQL: bets.commence_time
    is stored as 'YYYY-MM-DDTHH:MM' while odds_history.captured_at is
    'YYYY-MM-DD HH:MM:SS', and a raw string comparison between the two formats
    (' ' sorts before 'T') made EVERY same-day snapshot — including in-play
    prices written after kickoff — pass the "before kickoff" filter, so the
    stored "close" could be a live price instead of the real closing line.

    recapture=True re-derives the close for ALL bets with an event_id (repairs
    values captured by the old comparison). Bets whose history has no genuine
    pre-kickoff snapshot get their close cleared back to NULL (CLV pending) —
    an honest blank instead of a fabricated number.

    Alongside the raw close we store pin_close_fair_odds — the DEVIGGED (no-vig,
    power method) close for the bet's selection. Realized CLV is measured against
    THIS fair close: vs the raw price a bet can "beat the close" by merely sitting
    inside Pinnacle's margin, which inflates CLV by ~2-4pp and is not real value.
    Normal mode also backfills fair odds for bets captured before this column
    existed (runs automatically after odds refresh / on My Bets load).

    The close is IDEMPOTENT, not write-once: it is always "the latest pre-kickoff
    snapshot that can be devigged". Bets whose kickoff was within the last
    CLOSE_RECHECK_HOURS are re-derived on every call, so a later snapshot (after a
    rescheduled kickoff) replaces an earlier one instead of being ignored.
    """
    from datetime import datetime as _dt, timedelta as _td
    conn = get_connection()
    # No time/close filter in SQL: the rows are few (one per logged bet) and every
    # timestamp decision is made in Python, where the two stored formats cannot be
    # string-compared by accident.
    rows = conn.execute("""
        SELECT b.id, b.selection, b.market, b.home_team, b.away_team,
               b.event_id, b.commence_time, b.pin_close_odds,
               b.pin_close_fair_odds, b.pin_close_captured_at
        FROM bets b
        WHERE b.event_id IS NOT NULL
          AND b.commence_time IS NOT NULL
    """).fetchall()

    now = _dt.utcnow()
    updated = 0
    late = []
    for r in rows:
        kickoff = _parse_ts(r["commence_time"])
        if not kickoff or kickoff > now:
            continue  # not kicked off yet (or unparseable) — nothing to capture
        missing = r["pin_close_odds"] is None or r["pin_close_fair_odds"] is None
        recent = (now - kickoff) <= _td(hours=CLOSE_RECHECK_HOURS)
        if not (recapture or missing or recent):
            continue  # an old, complete close is final

        snapshots = conn.execute("""
            SELECT pin_home, pin_draw, pin_away,
                   pin_over25, pin_under25,
                   pin_btts_yes, pin_btts_no,
                   captured_at
            FROM odds_history
            WHERE event_id = ?
        """, (r["event_id"],)).fetchall()

        sel = (r["selection"] or "").strip().lower()
        market = (r["market"] or "h2h").lower()
        home_lower = (r["home_team"] or "").lower()
        away_lower = (r["away_team"] or "").lower()
        # A 1X2 event shows a draw price in at least one snapshot; a snapshot of
        # it WITHOUT the draw is incomplete and cannot be devigged honestly.
        three_way = any(s["pin_draw"] for s in snapshots)

        # Latest snapshot at/before kickoff that quotes our selection — preferring
        # one that can actually be DEVIGGED (all legs present, sane margin), since
        # only a fair close produces a CLV. A raw-only snapshot is the fallback.
        # NO fallback to post-kickoff snapshots or current odds_events: those are
        # live/near-now prices, which would fabricate a closing line (= CLV vs now,
        # not a real close). If there is no genuine PRE-kickoff snapshot, leave
        # pin_close NULL so the CLV stays pending — never a faked number.
        best_fair = None    # (ts, price, captured_at, fair_prob)
        best_raw = None     # (ts, price, captured_at)
        for s in snapshots:
            ts = _parse_ts(s["captured_at"])
            if ts is None or ts > kickoff:
                continue
            price = _pin_price_for_bet(s, market, sel, home_lower, away_lower)
            if not price:
                continue
            if best_raw is None or ts > best_raw[0]:
                best_raw = (ts, price, s["captured_at"])
            fp = _fair_close_prob(s, market, sel, home_lower, away_lower, three_way=three_way)
            if fp and (best_fair is None or ts > best_fair[0]):
                best_fair = (ts, price, s["captured_at"], fp)

        if best_fair or best_raw:
            if best_fair:
                close_ts, pin_close, close_at, fair_prob = best_fair
                fair_odds = round(1.0 / fair_prob, 3)
            else:
                close_ts, pin_close, close_at = best_raw
                fair_odds = None
            same = (r["pin_close_odds"] == pin_close
                    and r["pin_close_fair_odds"] == fair_odds
                    and str(r["pin_close_captured_at"]) == str(close_at))
            if not same:
                conn.execute("""
                    UPDATE bets SET pin_close_odds=?, pin_close_implied=?,
                                    pin_close_fair_odds=?, pin_close_captured_at=?
                    WHERE id=?
                """, (pin_close, round(1.0 / pin_close * 100, 2), fair_odds, close_at, r["id"]))
                updated += 1
            elif recapture:
                updated += 1
            lead = (kickoff - close_ts).total_seconds() / 60.0
        else:
            lead = None
            if recapture and r["pin_close_odds"] is not None:
                conn.execute("""
                    UPDATE bets SET pin_close_odds=NULL, pin_close_implied=NULL,
                                    pin_close_fair_odds=NULL, pin_close_captured_at=NULL
                    WHERE id=?
                """, (r["id"],))
                updated += 1

        # Watchdog: the kickoff has passed and the best we have is not a real close.
        if (lead is None or lead > CLOSE_ALERT_LEAD_MIN) and (now - kickoff) <= _td(hours=24):
            late.append({"id": r["id"], "lead_min": None if lead is None else round(lead, 1)})
            if r["id"] not in _close_alerted:
                _close_alerted.add(r["id"])
                _alert_late_close(dict(r), lead)

    conn.commit()
    conn.close()
    LAST_CAPTURE_REPORT["at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    LAST_CAPTURE_REPORT["late"] = late
    return updated


def clv_audit(bet_id):
    """Full audit trail for one bet's CLV: entry odd, the captured Pinnacle close
    (value + when it was captured relative to kickoff), the step-by-step math,
    and the event's entire snapshot timeline so a wrong close is visible at a glance."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    if not row:
        conn.close()
        return None
    b = dict(row)
    kickoff = _parse_ts(b.get("commence_time"))
    sel = (b.get("selection") or "").strip().lower()
    market = (b.get("market") or "h2h").lower()
    home_lower = (b.get("home_team") or "").lower()
    away_lower = (b.get("away_team") or "").lower()

    timeline = []
    if b.get("event_id"):
        snaps = conn.execute("""
            SELECT pin_home, pin_draw, pin_away, pin_over25, pin_under25,
                   pin_btts_yes, pin_btts_no, captured_at
            FROM odds_history WHERE event_id = ? ORDER BY captured_at ASC
        """, (b["event_id"],)).fetchall()
        for s in snaps:
            ts = _parse_ts(s["captured_at"])
            price = _pin_price_for_bet(s, market, sel, home_lower, away_lower)
            mins_before = (round((kickoff - ts).total_seconds() / 60.0, 1)
                           if (kickoff and ts) else None)
            timeline.append({
                "captured_at": s["captured_at"],
                "pin_price_for_selection": price,
                "minutes_before_kickoff": mins_before,
                "pre_kickoff": (mins_before is not None and mins_before >= 0),
                "is_stored_close": (price is not None and price == b.get("pin_close_odds")
                                    and s["captured_at"] == b.get("pin_close_captured_at")),
            })
    conn.close()

    clv_steps = None
    if b.get("odds") and b.get("pin_close_odds"):
        fair = b.get("pin_close_fair_odds")
        clv_steps = {
            "your_odds": b["odds"],
            "your_implied_pct": round(100.0 / b["odds"], 2),
            "pin_close_odds_raw": b["pin_close_odds"],
            "raw_close_implied_pct": round(100.0 / b["pin_close_odds"], 2),
            "pin_close_fair_odds": fair,
            "fair_close_prob_pct": round(100.0 / fair, 2) if fair else None,
            "formula": "CLV% = (your_odds / pin_close_FAIR_odds - 1) × 100  [no-vig]",
            "clv_pct": round((b["odds"] / fair - 1) * 100, 2) if fair else None,
            "clv_vs_raw_close_pct": round((b["odds"] / b["pin_close_odds"] - 1) * 100, 2),
            "note": ("CLV medido contra o fecho DEVIGGED (no-vig, método power) da Pinnacle — "
                     ">0 significa valor real no fecho, não apenas ter batido a margem da casa. "
                     "clv_vs_raw_close_pct é a régua antiga (fecho cru), só para referência. "
                     "Independente do resultado da aposta."),
        }

    warnings = []
    close_ts = _parse_ts(b.get("pin_close_captured_at"))
    if b.get("pin_close_odds") is not None and b.get("pin_close_fair_odds") is None:
        warnings.append("Fecho cru capturado mas sem devig possível (falta o preço do outro lado "
                        "no snapshot) — CLV fica pendente em vez de usar a régua inflacionada.")
    if b.get("pin_close_odds") is not None and b.get("pin_close_captured_at") is None:
        warnings.append("Fecho capturado pela versão antiga do código (sem timestamp) — "
                        "pode ser um preço live/pós-kickoff. Corre POST /api/clv/capture?recapture=true.")
    if close_ts and kickoff:
        mins = (kickoff - close_ts).total_seconds() / 60.0
        if mins < 0:
            warnings.append(f"Fecho capturado {abs(mins):.0f} min DEPOIS do kickoff (preço live) — inválido; recaptura necessária.")
        elif mins > 120:
            warnings.append(f"Fecho capturado {mins:.0f} min antes do kickoff — linha antiga, não um verdadeiro fecho (~15 min).")
    if b.get("pin_close_odds") is None:
        warnings.append("Sem linha de fecho capturada — CLV pendente (não há snapshot Pinnacle pré-kickoff para este evento/mercado).")

    return {
        "bet": {k: b.get(k) for k in (
            "id", "placed_at", "sport_name", "home_team", "away_team", "selection",
            "market", "bookmaker", "odds", "stake", "status", "result", "profit",
            "commence_time", "event_id",
            "pin_close_odds", "pin_close_implied", "pin_close_fair_odds",
            "pin_close_captured_at")},
        "kickoff_utc": b.get("commence_time"),
        "clv": clv_steps,
        "snapshots": timeline,
        "warnings": warnings,
    }


def auto_grade_pending_bets():
    """
    Auto-settle pending bets by matching against stored match results
    (football_matches, national_matches). Supports Match Result, O/U 2.5, BTTS.
    Returns number of bets graded.
    """
    import re as _re
    from datetime import datetime as _dt, timedelta as _td

    conn = get_connection()
    # Only attempt bets where commence_time is > 2.5h in the past.
    # Time filter done in Python via _parse_ts: a raw SQL string comparison between
    # commence_time ('...T...') and the cutoff ('YYYY-MM-DD HH:MM:SS') silently
    # skipped same-day games until the date rolled over (same format bug family
    # as the old CLV close capture).
    cutoff_dt = _dt.utcnow() - _td(hours=2, minutes=30)
    rows = [r for r in conn.execute("""
        SELECT id, event_id, sport_name, home_team, away_team, commence_time,
               market, selection, odds, stake
        FROM bets
        WHERE status = 'pending'
          AND commence_time IS NOT NULL
    """).fetchall()
        if (_parse_ts(r["commence_time"]) or _dt.max) < cutoff_dt]

    graded = 0
    for bet in rows:
        try:
            market = (bet["market"] or "h2h").lower()
            selection = (bet["selection"] or "").strip().lower()
            home = (bet["home_team"] or "").strip()
            away = (bet["away_team"] or "").strip()
            date_str = (bet["commence_time"] or "")[:10]
            if not home or not away or not date_str:
                continue

            # Date window: same day ± 1 day to handle timezone differences
            try:
                d0 = _dt.strptime(date_str, "%Y-%m-%d")
                d1 = (d0 + _td(days=2)).strftime("%Y-%m-%d")
                d_from = (d0 - _td(days=1)).strftime("%Y-%m-%d")
            except Exception:
                d1 = date_str
                d_from = date_str

            match_row = None
            # Try football_matches
            match_row = conn.execute("""
                SELECT home_goals, away_goals FROM football_matches
                WHERE LOWER(home_team) = LOWER(?) AND LOWER(away_team) = LOWER(?)
                  AND date >= ? AND date <= ? AND home_goals IS NOT NULL
                ORDER BY date DESC LIMIT 1
            """, (home, away, d_from, d1)).fetchone()

            # Try national_matches
            if not match_row:
                match_row = conn.execute("""
                    SELECT home_goals, away_goals FROM national_matches
                    WHERE LOWER(home_team) = LOWER(?) AND LOWER(away_team) = LOWER(?)
                      AND date >= ? AND date <= ? AND home_goals IS NOT NULL
                    ORDER BY date DESC LIMIT 1
                """, (home, away, d_from, d1)).fetchone()

            if not match_row:
                continue

            hg = match_row["home_goals"] if hasattr(match_row, "keys") else match_row[0]
            ag = match_row["away_goals"] if hasattr(match_row, "keys") else match_row[1]
            if hg is None or ag is None:
                continue

            # Determine bet outcome
            outcome = None
            home_lower = home.lower()
            away_lower = away.lower()

            if market in ("h2h", "match result", "1x2"):
                actual = "H" if hg > ag else ("D" if hg == ag else "A")
                if selection in (home_lower, "home", "1", "h"):
                    outcome = "won" if actual == "H" else "lost"
                elif selection in (away_lower, "away", "2", "a"):
                    outcome = "won" if actual == "A" else "lost"
                elif selection in ("draw", "x", "d", "empate"):
                    outcome = "won" if actual == "D" else "lost"

            elif market in ("over_under", "over/under 2.5", "totals"):
                total = hg + ag
                line = 2.5
                m = _re.search(r'(\d+\.?\d*)', selection)
                if m:
                    line = float(m.group(1))
                if "over" in selection:
                    outcome = "won" if total > line else ("push" if total == line else "lost")
                elif "under" in selection:
                    outcome = "won" if total < line else ("push" if total == line else "lost")

            elif market in ("btts", "both teams to score"):
                btts = (hg > 0 and ag > 0)
                if "yes" in selection or "sim" in selection:
                    outcome = "won" if btts else "lost"
                elif "no" in selection or "nao" in selection or "não" in selection:
                    outcome = "won" if not btts else "lost"

            if outcome is None:
                continue

            odds = bet["odds"] or 0
            stake = bet["stake"] or 0
            profit = round(stake * (odds - 1), 2) if outcome == "won" else (-stake if outcome == "lost" else 0.0)

            conn.execute("""
                UPDATE bets
                SET status='settled', result=?, profit=?, settled_at=datetime('now'),
                    notes=CASE WHEN notes IS NULL THEN '[auto-graded]'
                               ELSE notes || ' [auto-graded]' END
                WHERE id=?
            """, (outcome, profit, bet["id"]))
            graded += 1

        except Exception as e:
            print(f"auto_grade error bet {bet['id']}: {e}", flush=True)

    if graded:
        conn.commit()
    conn.close()
    return graded


def get_national_summary():
    """Top tournaments + counts."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT tournament, COUNT(*) as n,
               MIN(date) as first_date, MAX(date) as last_date
        FROM national_matches
        GROUP BY tournament
        ORDER BY n DESC
        LIMIT 12
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_national_recent(limit=50, team=None):
    conn = get_connection()
    if team:
        rows = conn.execute("""
            SELECT * FROM national_matches
            WHERE (home_team LIKE ? OR away_team LIKE ?)
            ORDER BY date DESC LIMIT ?
        """, (f"%{team}%", f"%{team}%", limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM national_matches
            ORDER BY date DESC LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_team_xg(team_name):
    """Lookup xG stats for a team (fuzzy match on name)."""
    if not team_name:
        return None

    # Use pre-loaded session cache if available (avoids new Neon connection per call)
    if _session_xg is not None:
        key = team_name.lower()
        row = _session_xg.get(key)
        if not row:
            normalized = team_name.replace("FC ", "").replace(" FC", "").strip().lower()
            for k, v in _session_xg.items():
                if normalized in k or k.startswith(normalized):
                    row = v
                    break
        return row

    conn = get_connection()
    # Exact match first
    row = conn.execute("SELECT * FROM team_xg WHERE team = ?", (team_name,)).fetchone()
    if not row:
        # Fuzzy: try with normalized variations (Understat uses "Manchester United", odds API may use different)
        normalized = team_name.replace("FC ", "").replace(" FC", "").strip()
        row = conn.execute(
            "SELECT * FROM team_xg WHERE team LIKE ? OR team LIKE ?",
            (f"%{normalized}%", f"{normalized}%")
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_xg_summary():
    """Return league-level xG coverage summary."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT league_name,
               COUNT(*) as matches,
               SUM(CASE WHEN is_result = 1 THEN 1 ELSE 0 END) as completed,
               MAX(date) as last_match
        FROM understat_matches
        GROUP BY league_name
        ORDER BY matches DESC
    """).fetchall()
    teams_count = conn.execute("SELECT COUNT(*) FROM team_xg").fetchone()[0]
    conn.close()
    return {
        "leagues": [dict(r) for r in rows],
        "teams_indexed": teams_count,
    }


def xg_based_probability(home_team, away_team):
    """
    Estimate match outcome probabilities from xG rolling averages (Poisson model).
    Returns dict with 1X2, O/U 2.5, BTTS probabilities OR None if no xG data.
    """
    h_xg = get_team_xg(home_team)
    a_xg = get_team_xg(away_team)
    if not h_xg or not a_xg:
        return None

    home_lambda = (h_xg["avg_xg_for"] + a_xg["avg_xg_against"]) / 2 * 1.10
    away_lambda = (a_xg["avg_xg_for"] + h_xg["avg_xg_against"]) / 2 * 0.95
    home_lambda = max(0.3, min(4.0, home_lambda))
    away_lambda = max(0.3, min(4.0, away_lambda))

    import math
    def poisson(k, lam):
        return (lam ** k) * math.exp(-lam) / math.factorial(k)

    max_goals = 7
    p_home = p_draw = p_away = 0.0
    p_over25 = 0.0
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = poisson(h, home_lambda) * poisson(a, away_lambda)
            if h > a: p_home += p
            elif h == a: p_draw += p
            else: p_away += p
            if h + a > 2.5: p_over25 += p

    total = p_home + p_draw + p_away
    if total > 0:
        p_home /= total; p_draw /= total; p_away /= total

    p_btts_no = poisson(0, home_lambda) + poisson(0, away_lambda) - poisson(0, home_lambda) * poisson(0, away_lambda)
    p_btts_yes = 1 - p_btts_no

    return {
        "home": round(p_home * 100, 1),
        "draw": round(p_draw * 100, 1),
        "away": round(p_away * 100, 1),
        "over25": round(p_over25 * 100, 1),
        "under25": round((1 - p_over25) * 100, 1),
        "btts_yes": round(p_btts_yes * 100, 1),
        "btts_no": round(p_btts_no * 100, 1),
        "expected_home_goals": round(home_lambda, 2),
        "expected_away_goals": round(away_lambda, 2),
        "home_xg_form": h_xg,
        "away_xg_form": a_xg,
    }


def pending_bet_kickoffs():
    """[(bet_id, sport_key, kickoff)] for every PENDING bet tied to an event.

    The single source of truth for "which backed games are still to be played",
    shared by the closing-capture targeting and by the in-memory skip cache so
    the two can never disagree about a kickoff.

      * sport_key comes from the bet itself (copied there when it was logged);
        the JOIN on odds_events is only a fallback for bets logged before that
        column existed. A purged event row therefore no longer hides a bet.
      * kickoff prefers the event's time (rewritten from the API on every fetch,
        so it follows a rescheduled match) over the bet's own copy.
      * every timestamp is compared in Python. commence_time is stored as
        'YYYY-MM-DDTHH:MM:SSZ' and SQLite's datetime('now') as
        'YYYY-MM-DD HH:MM:SS'; compared as strings, 'T' sorts after ' ', so a
        kickoff earlier TODAY still read as "in the future".

    RAISES on a database failure — it does NOT return []. An empty list is a real
    answer ("nothing is backed right now") and callers act on it by skipping the
    fetch. Swallowing the exception made those two states identical, so an
    unreachable database produced a tick that looked perfectly healthy while
    silently missing the close. A failure must never read as a measurement.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT b.id AS bet_id, b.sport_key AS bet_key, b.commence_time AS bet_ko, "
            "e.sport_key AS ev_key, e.commence_time AS ev_ko "
            "FROM bets b LEFT JOIN odds_events e ON e.event_id = b.event_id "
            "WHERE b.status = 'pending' AND b.event_id IS NOT NULL"
        ).fetchall()
    finally:
        try:
            conn.close()
        except Exception:                             # noqa: BLE001 - already failing
            pass
    out = []
    for r in rows:
        ko = _parse_ts(r["ev_ko"]) or _parse_ts(r["bet_ko"])
        out.append((r["bet_id"], r["bet_key"] or r["ev_key"], ko))
    return out


def next_pending_kickoff():
    """Earliest FUTURE kickoff among tracked pending bets, or None.

    The closing capture calls this to learn when it next has anything to do,
    then caches the answer in memory — so the 5-minute cron that keeps Render
    awake stops opening a database connection 288 times a day when the next
    kickoff is hours away. That traffic is what exhausted the Neon free compute
    allowance: a query every 5 minutes never lets the compute suspend.

    RAISES on a database failure (see pending_bet_kickoffs). Returning None here
    would be far worse than it looks: None means "nothing pending", the caller
    caches that for up to an hour and answers every cron call from memory — so
    one transient error would blind the capture through a whole afternoon of
    kickoffs. The caller leaves the cache untouched when this raises.
    """
    from datetime import datetime as _dt
    now = _dt.utcnow()
    future = [ko for _id, _key, ko in pending_bet_kickoffs() if ko and ko > now]
    return min(future) if future else None


def closing_targets(within_minutes):
    """{sport_key: minutes to the NEAREST tracked kickoff} inside the window.

    The closing capture used to re-fetch every football/tennis league that had any
    game about to start — 96 cron ticks a day, 1 credit per league per tick, whether
    or not a single one of those games was backed. The only games whose closing line
    we actually need are the ones we have money on, so target those instead: the
    cost stops scaling with the fixture list and starts scaling with the bet log.

    The minutes-to-kickoff lets the caller pace its fetches: sparse while the game
    is still far off, every tick in the final minutes where the close is decided.
    RAISES on a database failure (see pending_bet_kickoffs).
    """
    from datetime import datetime as _dt
    now = _dt.utcnow()
    out = {}
    for _id, key, ko in pending_bet_kickoffs():
        if not key or not ko:
            continue
        mins = (ko - now).total_seconds() / 60.0
        if 0 < mins < within_minutes and (key not in out or mins < out[key]):
            out[key] = mins
    return out


def sport_keys_with_pending_bets(within_minutes):
    """Sport keys that have a TRACKED PENDING bet kicking off in the next N minutes.
    RAISES on a database failure — [] means only "nothing is backed"."""
    return list(closing_targets(within_minutes).keys())


def get_line_movement(event_id):
    """
    Returns line movement data. Uses best available reference per snapshot:
    pin_ (Pinnacle/Betfair) → best_ (market best) → x1_ (1xBet).

    Smart Money threshold: absolute odds change >= 0.20 on any outcome.
    Steam threshold: >5% relative move in last 6 hours.
    """
    # Use pre-loaded session cache if available (avoids new Neon connection per event)
    if _session_hist is not None:
        rows = _session_hist.get(event_id, [])
    else:
        conn = get_connection()
        rows = conn.execute("""
            SELECT * FROM odds_history
            WHERE event_id = ?
            ORDER BY captured_at ASC
        """, (event_id,)).fetchall()
        conn.close()

    if len(rows) < 2:
        return None

    opening = dict(rows[0])
    latest = dict(rows[-1])

    def best_ref(row, side):
        return row.get(f"pin_{side}") or row.get(f"best_{side}") or row.get(f"x1_{side}")

    def pct_change(old, new):
        if not old or not new or old <= 0:
            return None
        return round((new - old) / old * 100, 1)

    def direction(pct):
        if pct is None: return None
        if pct < -2: return "down"
        if pct > 2: return "up"
        return "flat"

    sides = ["home", "draw", "away"]
    movements = {}
    abs_changes = {}
    for side in sides:
        o = best_ref(opening, side)
        l = best_ref(latest, side)
        movements[side] = pct_change(o, l)
        abs_changes[side] = round(l - o, 3) if (o and l) else None

    movements["over25"]  = pct_change(opening.get("pin_over25"),  latest.get("pin_over25"))
    movements["btts_yes"] = pct_change(opening.get("pin_btts_yes"), latest.get("pin_btts_yes"))

    # Smart Money: absolute odds move >= 0.20 (strong) or >= 0.10 (moderate)
    smart_money = {}
    for side in sides:
        ch = abs_changes.get(side)
        if ch is not None and abs(ch) >= 0.10:
            smart_money[side] = {
                "abs_change": ch,
                "direction": "in" if ch < 0 else "out",
                "strength": "strong" if abs(ch) >= 0.20 else "moderate",
            }

    has_strong_movement = any(v["strength"] == "strong" for v in smart_money.values())

    # Steam: >5% move in last 6 hours
    from datetime import datetime, timedelta
    try:
        latest_dt = datetime.strptime(latest["captured_at"][:19], "%Y-%m-%d %H:%M:%S")
        cutoff = latest_dt - timedelta(hours=6)
        recent = [r for r in rows
                  if datetime.strptime(r["captured_at"][:19], "%Y-%m-%d %H:%M:%S") >= cutoff]
    except Exception:
        recent = rows

    steam = {}
    if len(recent) >= 2:
        first = dict(recent[0])
        for side in sides:
            old = best_ref(first, side)
            new = best_ref(latest, side)
            ch = pct_change(old, new)
            if ch is not None and abs(ch) >= 5:
                steam[side] = {"direction": "in" if ch < 0 else "out", "pct": ch}

    # Opening vs latest for display
    opening_display = {s: best_ref(opening, s) for s in sides}
    latest_display  = {s: best_ref(latest,  s) for s in sides}

    # Sparkline (max 30 points, using best reference)
    step = max(1, len(rows) // 30)
    sparkline = []
    for i, r in enumerate(rows):
        if i % step == 0 or i == len(rows) - 1:
            rd = dict(r)
            sparkline.append({
                "t": rd["captured_at"],
                "pin_home": best_ref(rd, "home"),
                "pin_draw": best_ref(rd, "draw"),
                "pin_away": best_ref(rd, "away"),
            })

    return {
        "opening": opening_display,
        "latest": latest_display,
        "movements": movements,
        "abs_changes": abs_changes,
        "directions": {k: direction(v) for k, v in movements.items()},
        "steam": steam,
        "smart_money": smart_money,
        "has_strong_movement": has_strong_movement,
        "snapshots": len(rows),
        "sparkline": sparkline,
    }


# ==========================================================
# PERFORMANCE ANALYTICS — for Bankroll Dashboard
# ==========================================================

def get_performance_breakdown():
    """
    Returns ROI/profit breakdown by sport, bookmaker, market, confidence band.
    Used by the Performance dashboard.
    """
    conn = get_connection()

    def aggregate(group_by_col):
        rows = conn.execute(f"""
            SELECT
                COALESCE({group_by_col}, 'Unknown') as bucket,
                COUNT(*) as n,
                SUM(stake) as staked,
                SUM(profit) as profit,
                SUM(CASE WHEN result='won' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result='lost' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN result='push' THEN 1 ELSE 0 END) as pushes
            FROM bets
            WHERE status='settled'
            GROUP BY bucket
            ORDER BY staked DESC
        """).fetchall()
        out = []
        for r in rows:
            staked = r["staked"] or 0
            profit = r["profit"] or 0
            decisive = (r["wins"] or 0) + (r["losses"] or 0)
            out.append({
                "bucket": r["bucket"],
                "n": r["n"],
                "staked": round(staked, 2),
                "profit": round(profit, 2),
                "roi": round((profit / staked * 100), 2) if staked > 0 else 0,
                "wins": r["wins"],
                "losses": r["losses"],
                "pushes": r["pushes"],
                "win_rate": round((r["wins"] or 0) / decisive * 100, 1) if decisive > 0 else 0,
            })
        return out

    by_sport = aggregate("sport_name")
    by_bookmaker = aggregate("bookmaker")
    by_market = aggregate("market")

    # By odds range
    odds_buckets = conn.execute("""
        SELECT
            CASE
                WHEN odds < 1.5 THEN 'Heavy fav (< 1.50)'
                WHEN odds < 2.0 THEN 'Fav (1.50–2.00)'
                WHEN odds < 3.0 THEN 'Even (2.00–3.00)'
                WHEN odds < 5.0 THEN 'Underdog (3.00–5.00)'
                ELSE 'Long shot (5.00+)'
            END as bucket,
            COUNT(*) as n,
            SUM(stake) as staked,
            SUM(profit) as profit,
            SUM(CASE WHEN result='won' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN result='lost' THEN 1 ELSE 0 END) as losses
        FROM bets WHERE status='settled'
        GROUP BY bucket
    """).fetchall()
    by_odds = []
    for r in odds_buckets:
        staked = r["staked"] or 0
        profit = r["profit"] or 0
        decisive = (r["wins"] or 0) + (r["losses"] or 0)
        by_odds.append({
            "bucket": r["bucket"],
            "n": r["n"],
            "staked": round(staked, 2),
            "profit": round(profit, 2),
            "roi": round((profit / staked * 100), 2) if staked > 0 else 0,
            "win_rate": round((r["wins"] or 0) / decisive * 100, 1) if decisive > 0 else 0,
        })

    conn.close()
    return {
        "by_sport": by_sport,
        "by_bookmaker": by_bookmaker,
        "by_market": by_market,
        "by_odds": by_odds,
    }


def get_bankroll_advanced(starting_bankroll=1000):
    """
    Detailed bankroll evolution + drawdown analysis.
    Returns time series + key statistics.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, placed_at, settled_at, stake, profit, result, selection, sport_name
        FROM bets
        WHERE status='settled' AND settled_at IS NOT NULL
        ORDER BY settled_at ASC, id ASC
    """).fetchall()
    conn.close()

    if not rows:
        return {
            "series": [],
            "starting_bankroll": starting_bankroll,
            "current_bankroll": starting_bankroll,
            "peak_bankroll": starting_bankroll,
            "max_drawdown_pct": 0,
            "max_drawdown_abs": 0,
            "current_drawdown_pct": 0,
            "longest_winning_streak": 0,
            "longest_losing_streak": 0,
            "current_streak": {"type": None, "count": 0},
        }

    series = []
    cumulative = starting_bankroll
    peak = starting_bankroll
    max_drawdown = 0
    max_drawdown_abs = 0

    current_streak_type = None
    current_streak_count = 0
    longest_win_streak = 0
    longest_lose_streak = 0

    for r in rows:
        profit = r["profit"] or 0
        cumulative += profit
        if cumulative > peak:
            peak = cumulative
        drawdown_abs = peak - cumulative
        drawdown_pct = (drawdown_abs / peak * 100) if peak > 0 else 0
        if drawdown_pct > max_drawdown:
            max_drawdown = drawdown_pct
            max_drawdown_abs = drawdown_abs

        # Streak tracking (only Wins and Losses count, Push neutral)
        result = r["result"]
        if result == "won":
            if current_streak_type == "win":
                current_streak_count += 1
            else:
                current_streak_type = "win"
                current_streak_count = 1
            longest_win_streak = max(longest_win_streak, current_streak_count)
        elif result == "lost":
            if current_streak_type == "loss":
                current_streak_count += 1
            else:
                current_streak_type = "loss"
                current_streak_count = 1
            longest_lose_streak = max(longest_lose_streak, current_streak_count)

        series.append({
            "date": r["settled_at"],
            "bankroll": round(cumulative, 2),
            "delta": round(profit, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "selection": r["selection"],
            "sport": r["sport_name"],
            "result": result,
        })

    current_dd_pct = ((peak - cumulative) / peak * 100) if peak > 0 else 0

    return {
        "series": series,
        "starting_bankroll": starting_bankroll,
        "current_bankroll": round(cumulative, 2),
        "peak_bankroll": round(peak, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "max_drawdown_abs": round(max_drawdown_abs, 2),
        "current_drawdown_pct": round(current_dd_pct, 2),
        "longest_winning_streak": longest_win_streak,
        "longest_losing_streak": longest_lose_streak,
        "current_streak": {"type": current_streak_type, "count": current_streak_count},
    }


# ==========================================================
# ELO — rating lookup and match probability
# ==========================================================

def _fd_only_sql():
    """SQL fragment restricting football_matches to the football-data.co.uk rows.

    football_matches holds the same matches twice under two spellings: the
    openfootball import ("Manchester United FC") and the football-data one ("Man
    United"). Anything that walks a team's matches must stay in ONE of them, or it
    sees every match twice or mixes two half-histories. football-data is the one
    kept: 16 divisions instead of 5, and current to the last weekend."""
    try:
        from collectors import teamnames
        return " AND substr(match_id, 1, 3) = 'fd_'" if teamnames.has_results_vocabulary() else ""
    except Exception:                                  # noqa: BLE001
        return ""


def canonical_team(team, sport_name=None, national=False):
    """The name the results tables use for this team, or None when we hold no
    results for it. Never a look-alike: see collectors/teamnames.py."""
    try:
        from collectors import teamnames
        if national:
            return teamnames.resolve_national(team)[0]
        return teamnames.resolve_club(team, sport_name)[0]
    except Exception as e:                             # noqa: BLE001
        print(f"WARN canonical_team({team!r}): {e}", flush=True)
        return None


def get_elo_rating(entity, category, surface=None, sport_name=None):
    """Elo row for a team or player, or None.

    Club and national-team names are first resolved to the name the results use
    and then looked up EXACTLY. The lookup used to fall back to "the first rating
    whose name contains this one": "Milan" got whichever of AC Milan and
    Internazionale Milano came first, and "Inter Milan" or "Atlético Madrid",
    contained in no stored name, silently had no rating at all."""
    if not entity:
        return None
    eff_surface = surface if surface else ''
    exact_only = False
    if category in ("football_club", "national"):
        canon = canonical_team(entity, sport_name, national=(category == "national"))
        if not canon:
            return None
        entity, exact_only = canon, True

    # Use pre-loaded session cache if available (avoids new Neon connection per call)
    if _session_elo is not None:
        key = (entity.lower(), category, eff_surface)
        row = _session_elo.get(key)
        if not row and not exact_only:
            norm = entity.replace("FC ", "").replace(" FC", "").strip().lower()
            for (e, c, s), v in _session_elo.items():
                if c == category and (s or '') == eff_surface and norm in e:
                    row = v
                    break
        return row

    # PostgreSQL PRIMARY KEY makes surface NOT NULL; we store '' for categories without surface
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM elo_ratings WHERE entity = ? AND category = ? AND COALESCE(surface,'') = ?",
        (entity, category, eff_surface)
    ).fetchone()
    if not row and not exact_only:
        norm = entity.replace("FC ", "").replace(" FC", "").strip()
        row = conn.execute(
            "SELECT * FROM elo_ratings WHERE entity LIKE ? AND category = ? AND COALESCE(surface,'') = ?",
            (f"%{norm}%", category, eff_surface)
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def elo_based_probability(home_team, away_team, category, surface=None, home_advantage=65,
                          sport_name=None):
    """
    Match outcome probabilities from Elo ratings.
    For football: includes home advantage + draw estimation.
    For tennis: head-to-head win probability (no draw).

    Club ratings are computed league by league: every league is a closed pool
    that averages 1500, so a rating says how good a team is INSIDE its league and
    nothing about how two leagues compare. For a fixture between clubs of two
    countries (European cups) the result carries "comparable": False and callers
    must not present it as a probability.
    """
    rh = get_elo_rating(home_team, category, surface, sport_name=sport_name)
    ra = get_elo_rating(away_team, category, surface, sport_name=sport_name)
    if not rh or not ra:
        return None
    comparable = True
    if category == "football_club":
        try:
            from collectors import teamnames
            ch, ca = teamnames.club_country(rh["entity"]), teamnames.club_country(ra["entity"])
            comparable = not (ch and ca and ch != ca)
        except Exception:                              # noqa: BLE001
            comparable = True

    rating_h = rh["rating"]
    rating_a = ra["rating"]

    if category == "tennis":
        # No draw, no home advantage
        p_home = 1.0 / (1.0 + 10 ** ((rating_a - rating_h) / 400))
        return {
            "home": round(p_home * 100, 1),
            "away": round((1 - p_home) * 100, 1),
            "draw": None,
            "rating_home": round(rating_h),
            "rating_away": round(rating_a),
            "rating_diff": round(rating_h - rating_a),
            "games_home": rh["games"],
            "games_away": ra["games"],
            "comparable": True,
        }
    else:
        # Football: home advantage + draw model
        ha = home_advantage if category == "football_club" else home_advantage
        p_home_raw = 1.0 / (1.0 + 10 ** ((rating_a - (rating_h + ha)) / 400))
        # Estimate draw probability (higher when teams are close)
        rating_gap = abs((rating_h + ha) - rating_a)
        draw_prob = 0.32 * math.exp(-rating_gap / 300)  # ~32% when equal, decreasing
        draw_prob = max(0.10, min(0.35, draw_prob))
        # Distribute remaining between home/away proportionally
        non_draw = 1 - draw_prob
        p_home = p_home_raw * non_draw
        p_away = (1 - p_home_raw) * non_draw
        return {
            "home": round(p_home * 100, 1),
            "draw": round(draw_prob * 100, 1),
            "away": round(p_away * 100, 1),
            "rating_home": round(rating_h),
            "rating_away": round(rating_a),
            "rating_diff": round((rating_h + ha) - rating_a),
            "games_home": rh["games"],
            "games_away": ra["games"],
            "entity_home": rh["entity"], "entity_away": ra["entity"],
            "comparable": comparable,
        }


# ============================================================
# MATCH PROGNOSIS  (pure model — NO edge, NO CLV)
# ------------------------------------------------------------
# Powers the match-analysis detail view + "Múltipla do Dia". This is DELIBERATELY
# separate from the value-bets / traffic-light system: it answers "who is more
# likely to win" for context/fun, never "is this a +EV bet".
#
# Design forced by the model-lab backtest (27,844 matches, 2021-26, walk-forward):
#   - Elo is well-calibrated (says 75% -> happens ~79%) and near market accuracy
#     (49.3% vs 52%). It is the SPINE: the confidence number shown = Elo's.
#   - The rolling-goals Poisson is OVERCONFIDENT on 1X2 (says 75% -> happens 52%),
#     so its 1X2 % is NEVER shown as confidence. It is used only for: O/U 2.5,
#     likely scorelines, BTTS, and as an AGREEMENT vote with Elo.
#   - When Elo & Poisson agree, realised hit-rate climbs monotonically with Elo
#     confidence (70-80% band -> 75% real). That agreement gate is what makes a
#     "safest pick" / accumulator meaningful (still -EV — an accumulator only
#     ever multiplies the bookmaker margin).
# ============================================================
# Confidence bands and what each one actually did, market number, in the backtest
# (scripts/prognosis_backtest.py). Highest first.
PROGNOSIS_TIERS = [
    {"min": 70, "tier": "Alta", "hit": 81},
    {"min": 60, "tier": "Média-alta", "hit": 65},
    {"min": 50, "tier": "Média", "hit": 55},
    {"min": 0, "tier": "Baixa", "hit": 42},
]
# How much of the goals model's distance from 50% is real (fitted on its own
# calibration table): said 72% for a side of O/U 2.5 -> happened 59.6%.
GOALS_OU_SHRINK = 0.46
# BTTS is worse still: said 72% -> happened 57.8% (same 22,373 matches).
GOALS_BTTS_SHRINK = 0.40

# A club that has not got a result in our tables for this long (measured from the
# fixture's kickoff) is being read from stale data: a normal season never leaves
# more than ~3 weeks between a team's matches outside the summer break.
PROGNOSIS_STALE_DAYS_CLUB = int(os.environ.get("PROGNOSIS_STALE_DAYS_CLUB", "35"))
# A tour player with no match for two months is injured, retired — or our data stopped.
PROGNOSIS_STALE_DAYS_TENNIS = int(os.environ.get("PROGNOSIS_STALE_DAYS_TENNIS", "60"))
# National teams play in windows months apart.
PROGNOSIS_STALE_DAYS_NATIONAL = int(os.environ.get("PROGNOSIS_STALE_DAYS_NATIONAL", "200"))

_TENNIS_SURFACE_KW = {
    "Grass": ["wimbledon", "halle", "queen", "hertogenbosch", "eastbourne", "mallorca",
              "newport", "nottingham", "birmingham"],
    "Clay": ["roland garros", "french open", "monte carlo", "monte-carlo", "madrid",
             "rome", "italian open", "hamburg", "barcelona", "munich", "estoril",
             "bastad", "gstaad", "kitzbuhel", "umag", "geneva", "lyon", "bucharest",
             "rabat", "charleston", "strasbourg", "parma"],
    "Hard": ["us open", "australian", "aus open", "miami", "indian wells", "cincinnati",
             "canada", "montreal", "toronto", "dubai", "doha", "acapulco", "shanghai",
             "beijing", "tokyo", "vienna", "basel", "paris masters", "rotterdam",
             "marseille", "metz", "antwerp", "astana", "adelaide", "brisbane",
             "auckland", "washington", "chengdu", "zhuhai"],
}


def _tennis_surface(sport_name):
    s = (sport_name or "").lower()
    for surf, kws in _TENNIS_SURFACE_KW.items():
        if any(k in s for k in kws):
            return surf
    return "All"


def _prognosis_category(sport_name):
    """('national' | 'tennis' | 'football_club', tennis surface).

    Was `"euro" in name or "world cup" in name`, which put the EUROpa League and
    the Club WORLD CUP among the national teams — their fixtures had no Elo, no
    form and no H2H. The rule now lives in teamnames.competition_kind."""
    from collectors.teamnames import competition_kind
    kind = competition_kind(sport_name)
    if kind == "national":
        return "national", None
    if kind == "tennis":
        return "tennis", _tennis_surface(sport_name)
    return "football_club", None


def _team_recent_football(conn, canon, limit=8, national=False):
    """Most-recent finished matches of ONE team, newest first.

    `canon` is the team's name as the results tables spell it (canonical_team()).
    The match is exact. It used to fall back to `LIKE '%<longest word>%'`, which
    built Manchester United's form out of "%Manchester%" (City's matches too),
    West Ham's out of "%United%" (four other clubs) and Dortmund's out of
    "%Borussia%" (Mönchengladbach's)."""
    if not canon:
        return []
    tbl = "national_matches" if national else "football_matches"
    only = "" if national else _fd_only_sql()
    q = (f"SELECT date, home_team, away_team, home_goals, away_goals FROM {tbl} "
         f"WHERE (home_team = ? OR away_team = ?) AND home_goals IS NOT NULL{only} "
         "ORDER BY date DESC LIMIT ?")
    try:
        return [dict(r) for r in conn.execute(q, (canon, canon, limit)).fetchall()]
    except Exception as e:                             # noqa: BLE001
        print(f"WARN _team_recent_football({canon!r}): {e}", flush=True)
        return []


def _team_goal_rates(matches, canon):
    """Avg goals for / against of `canon` over its own matches."""
    gf = ga = n = 0
    for m in matches:
        if m.get("home_team") == canon:
            gf += m["home_goals"]; ga += m["away_goals"]; n += 1
        elif m.get("away_team") == canon:
            gf += m["away_goals"]; ga += m["home_goals"]; n += 1
    if n == 0:
        return None
    return {"gf": gf / n, "ga": ga / n, "n": n}


def _h2h_football(conn, canon_home, canon_away, limit=6, national=False):
    """Past meetings of exactly these two teams (either venue), newest first."""
    if not canon_home or not canon_away:
        return []
    tbl = "national_matches" if national else "football_matches"
    only = "" if national else _fd_only_sql()
    q = (f"SELECT date, home_team, away_team, home_goals, away_goals FROM {tbl} "
         "WHERE ((home_team = ? AND away_team = ?) OR (home_team = ? AND away_team = ?)) "
         f"AND home_goals IS NOT NULL{only} ORDER BY date DESC LIMIT ?")
    try:
        rows = conn.execute(q, (canon_home, canon_away, canon_away, canon_home, limit)).fetchall()
    except Exception as e:                             # noqa: BLE001
        print(f"WARN _h2h_football({canon_home!r}, {canon_away!r}): {e}", flush=True)
        return []
    return [{"date": r["date"], "home_team": r["home_team"], "away_team": r["away_team"],
             "home_goals": r["home_goals"], "away_goals": r["away_goals"]} for r in rows]


_tennis_names = {"at": 0.0, "by_norm": None}


def _tennis_player(conn, player):
    """The player's name as tennis_matches spells it, or None.

    Exact first, then the same name ignoring accents/hyphens/case. It used to be
    `LIKE '%<longest word>%'`, which for "Alexander Zverev" searched "%Alexander%"
    and for "Jessica Pegula" searched "%Jessica%" — other players' matches."""
    if not player:
        return None
    try:
        from collectors.teamnames import norm as _norm
        if _tennis_names["by_norm"] is None or time.time() - _tennis_names["at"] > 3600:
            by_norm = {}
            for r in conn.execute(
                    "SELECT winner_name AS n FROM tennis_matches WHERE winner_name IS NOT NULL "
                    "UNION SELECT loser_name AS n FROM tennis_matches WHERE loser_name IS NOT NULL").fetchall():
                by_norm.setdefault(_norm(r["n"]), []).append(r["n"])
            _tennis_names["by_norm"], _tennis_names["at"] = by_norm, time.time()
        hit = _tennis_names["by_norm"].get(_norm(player))
        if hit and len(set(hit)) == 1:
            return hit[0]
        if hit and player in hit:
            return player
    except Exception as e:                             # noqa: BLE001
        print(f"WARN _tennis_player({player!r}): {e}", flush=True)
    return None


def _tennis_recent(conn, player, limit=8):
    """Last matches for a tennis player from tennis_matches (winner/loser rows).
    Returns {'results': ['W','L',...] most-recent-first, wins, losses, n, last_date}."""
    name = _tennis_player(conn, player)
    if not name:
        return None
    try:
        rows = conn.execute(
            "SELECT tourney_date, winner_name, loser_name, surface, tourney_name "
            "FROM tennis_matches WHERE (winner_name = ? OR loser_name = ?) "
            "ORDER BY tourney_date DESC LIMIT ?", (name, name, limit)).fetchall()
    except Exception:
        return None
    results = [("W" if r["winner_name"] == name else "L") for r in rows]
    if not results:
        return None
    wins = results.count("W")
    return {"results": results, "wins": wins, "losses": len(results) - wins, "n": len(results),
            "last_date": str(rows[0]["tourney_date"]) if rows else None}


def _tennis_h2h(conn, p1, p2, limit=8):
    """Head-to-head matches between two tennis players."""
    n1, n2 = _tennis_player(conn, p1), _tennis_player(conn, p2)
    if not n1 or not n2:
        return None
    try:
        rows = conn.execute(
            "SELECT tourney_date, winner_name, loser_name, surface, tourney_name, score "
            "FROM tennis_matches WHERE ((winner_name = ? AND loser_name = ?) "
            "OR (winner_name = ? AND loser_name = ?)) ORDER BY tourney_date DESC LIMIT ?",
            (n1, n2, n2, n1, limit)).fetchall()
    except Exception:
        return None
    matches, w1, w2 = [], 0, 0
    for r in rows:
        win = r["winner_name"]
        if win == n1: w1 += 1
        else: w2 += 1
        matches.append({"date": r["tourney_date"], "winner": win,
                        "surface": r["surface"], "tourney": r["tourney_name"], "score": r["score"]})
    if not matches:
        return None
    return {"matches": matches, "p1_wins": w1, "p2_wins": w2, "n": len(matches)}


def _poisson_shape(lam_h, lam_a, max_goals=8):
    """1X2 / O/U 2.5 / BTTS / top scorelines from an independent Poisson grid."""
    lam_h = max(0.2, min(4.5, lam_h)); lam_a = max(0.2, min(4.5, lam_a))
    ph = [poisson_pmf_local(k, lam_h) for k in range(max_goals + 1)]
    pa = [poisson_pmf_local(k, lam_a) for k in range(max_goals + 1)]
    p_home = p_draw = p_away = p_over = p_btts = 0.0
    grid = []
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = ph[h] * pa[a]
            if h > a: p_home += p
            elif h == a: p_draw += p
            else: p_away += p
            if h + a >= 3: p_over += p
            if h >= 1 and a >= 1: p_btts += p
            if h <= 5 and a <= 5:
                grid.append((f"{h}-{a}", p))
    grid.sort(key=lambda x: -x[1])
    return {
        "home": round(p_home * 100, 1), "draw": round(p_draw * 100, 1), "away": round(p_away * 100, 1),
        "over25": round(p_over * 100, 1), "under25": round((1 - p_over) * 100, 1),
        "btts_yes": round(p_btts * 100, 1), "btts_no": round((1 - p_btts) * 100, 1),
        "top_scorelines": [{"score": s, "prob": round(p * 100, 1)} for s, p in grid[:5]],
        "lambda_home": round(lam_h, 2), "lambda_away": round(lam_a, 2),
    }


def poisson_pmf_local(k, lam):
    try:
        return (lam ** k) * math.exp(-lam) / math.factorial(k)
    except (OverflowError, ValueError):
        return 0.0


def _devig_pin_odds(pin_odds):
    """(home, draw, away) decimal odds -> devigged pct dict, or None.

    Power method, the same one Value Bets and the closing line use. It used to be
    the proportional method here only, so the "probabilidade do mercado" on the
    prognosis page and the "true prob" on the Value Bets card for the SAME game
    differed by up to ~1 point on favourites and longshots."""
    if not pin_odds:
        return None
    h_o, d_o, a_o = pin_odds
    if not (h_o and a_o and h_o > 1 and a_o > 1):
        return None
    dv = remove_vig_power(h_o, d_o if (d_o and d_o > 1) else None, a_o)
    if not dv:
        return None
    return {"home": round(dv["home"], 1),
            "draw": round(dv["draw"], 1) if dv.get("draw") is not None else None,
            "away": round(dv["away"], 1)}


def get_match_prognosis(home_team, away_team, sport_name, commence_time=None,
                        pin_odds=None, ref_updated_at=None):
    """Pure-model prognosis for one fixture (NO edge/CLV). Returns a dict the
    match-analysis view renders. Degrades gracefully: any missing data source is
    simply omitted, never fatal.

    pin_odds: optional (home, draw, away) Pinnacle decimal odds. When available,
    the headline probability is the 50/50 BLEND of market-devig + Elo — model-lab
    v2 (27,844 matches, walk-forward) showed the blend beats Elo alone (51.4% vs
    49.3% acc, better Brier) with conservative calibration (says 74.5% -> happens
    81.5%). Aggregating the market is what the top analysis sites do."""
    category, surface = _prognosis_category(sport_name)
    is_national = category == "national"
    is_tennis = category == "tennis"
    out = {
        "home_team": home_team, "away_team": away_team, "sport_name": sport_name,
        "category": category, "commence_time": commence_time, "surface": surface if is_tennis else None,
        "elo": None, "market": None, "probs": None, "basis": None,
        "poisson": None, "form": None, "h2h": None, "tennis_form": None, "tennis_h2h": None,
        "best_pick": None, "confidence": None, "confidence_tier": None,
        "agreement": None, "notes": [], "data": None,
    }

    # ---- WHO are these two teams in our results? Resolved once, used by every
    # lookup below (Elo, form, goal rates, H2H). None = we hold no results for the
    # team, and then nothing about it is shown — never another team's numbers. ----
    canon_h = canon_a = None
    if not is_tennis:
        canon_h = canonical_team(home_team, sport_name, national=is_national)
        canon_a = canonical_team(away_team, sport_name, national=is_national)
        missing = [t for t, c in ((home_team, canon_h), (away_team, canon_a)) if not c]
        if missing:
            out["notes"].append(
                "Sem histórico de resultados para " + " e ".join(missing)
                + " — sem Elo, forma nem H2H desta(s) equipa(s).")

    # ---- Elo (tennis: try the match surface first, fall back to All) ----
    elo = None
    try:
        elo = elo_based_probability(home_team, away_team, category, surface=surface,
                                    sport_name=sport_name)
        if not elo and is_tennis and surface != "All":
            elo = elo_based_probability(home_team, away_team, category, surface="All")
            out["surface"] = "All"  # signal we used the overall rating
    except Exception:
        elo = None
    # Club ratings are per league (each league averages 1500): for two clubs of
    # different countries the difference between their ratings means nothing.
    # Keep the ratings for display, but never turn them into a probability.
    elo_usable = elo
    if elo and elo.get("comparable") is False:
        elo_usable = None
        out["notes"].append("Clubes de ligas diferentes: o Elo de cada um mede a força dentro da "
                            "sua liga e não é comparável — a leitura usa só a linha do mercado.")
    out["elo"] = elo

    # ---- Market devig (for the blended headline) ----
    market = None
    try:
        market = _devig_pin_odds(pin_odds)
    except Exception:
        market = None
    out["market"] = market

    # The line is only as current as the last odds refresh.
    if market and ref_updated_at:
        try:
            from datetime import datetime as _dt0
            _age = (_dt0.utcnow() - _dt0.strptime(str(ref_updated_at)[:16], "%Y-%m-%d %H:%M")).total_seconds() / 3600.0
            out["market_age_h"] = round(_age, 1)
            if _age > 6:
                out["notes"].append(f"A linha usada tem {_age:.0f}h — atualiza as odds para uma leitura atual.")
        except (TypeError, ValueError):
            pass

    # ---- Poisson shape (O/U, scorelines, BTTS, agreement vote) — football only ----
    poisson = None
    if not is_tennis:
        try:
            conn = get_connection()
            hm = _team_recent_football(conn, canon_h, national=is_national)
            am = _team_recent_football(conn, canon_a, national=is_national)
            conn.close()
            hr = _team_goal_rates(hm, canon_h)
            ar = _team_goal_rates(am, canon_a)
            if hr and ar:
                # mirror the live xG-Poisson blend (home boost / away damp)
                lam_h = (hr["gf"] + ar["ga"]) / 2 * 1.10
                lam_a = (ar["gf"] + hr["ga"]) / 2 * 0.95
                poisson = _poisson_shape(lam_h, lam_a)
                poisson["sample"] = {"home_matches": hr["n"], "away_matches": ar["n"]}
        except Exception:
            poisson = None
    out["poisson"] = poisson

    # ---- Recent form ----
    hm = am = []
    try:
        if not is_tennis:
            conn = get_connection()
            hm = _team_recent_football(conn, canon_h, national=is_national)
            am = _team_recent_football(conn, canon_a, national=is_national)
            conn.close()
            def _form(matches, canon):
                if not canon or not matches or not calculate_recent_form:
                    return None
                ml = [{"home_team": m["home_team"], "away_team": m["away_team"],
                       "home_score": m["home_goals"], "away_score": m["away_goals"]} for m in matches]
                return calculate_recent_form(canon, ml, last_n=6)
            hf, af = _form(hm, canon_h), _form(am, canon_a)
            if hf or af:
                out["form"] = {"home": hf, "away": af}
    except Exception:
        pass

    # ---- H2H ----
    try:
        if not is_tennis:
            conn = get_connection()
            h2h = _h2h_football(conn, canon_h, canon_a, national=is_national)
            conn.close()
            if h2h:
                hw = aw = dr = 0
                for m in h2h:
                    home_is_h = m["home_team"] == canon_h
                    hg, ag = m["home_goals"], m["away_goals"]
                    if hg == ag: dr += 1
                    elif (hg > ag) == home_is_h: hw += 1
                    else: aw += 1
                out["h2h"] = {"matches": h2h, "home_wins": hw, "away_wins": aw, "draws": dr, "n": len(h2h)}
    except Exception:
        pass

    # ---- HOW CURRENT is what we know about these teams? The results stopped at
    # 31 May once and the page kept printing "forma" and Elo as if they were live.
    # Say the date; and say it loudly when it is old. ----
    try:
        from datetime import datetime as _dt
        def _last(matches):
            ds = [str(m.get("date") or "")[:10] for m in matches if m.get("date")]
            return max(ds) if ds else None
        lh, la = _last(hm), _last(am)
        if is_tennis:
            raise StopIteration                        # tennis sets its own block below
        out["data"] = {"home_name": canon_h, "away_name": canon_a,
                       "home_last_result": lh, "away_last_result": la, "stale": False}
        ref = _parse_ts(commence_time) or _dt.utcnow()
        ages = []
        for d_ in (lh, la):
            try:
                ages.append((ref - _dt.strptime(d_, "%Y-%m-%d")).days)
            except (TypeError, ValueError):
                pass
        limit = PROGNOSIS_STALE_DAYS_NATIONAL if is_national else PROGNOSIS_STALE_DAYS_CLUB
        if ages and not is_tennis and max(ages) > limit:
            out["data"]["stale"] = True
            oldest = min(x for x in (lh, la) if x)
            out["notes"].append(
                f"Atenção: o último resultado que temos é de {oldest[8:10]}/{oldest[5:7]}/{oldest[:4]} "
                f"(há {max(ages)} dias). Forma, golos e Elo podem estar desatualizados.")
    except StopIteration:
        pass
    except Exception as e:                             # noqa: BLE001
        print(f"WARN prognosis freshness: {e}", flush=True)

    # ---- Tennis form + H2H (players) ----
    if is_tennis:
        try:
            conn = get_connection()
            tf_h = _tennis_recent(conn, home_team)
            tf_a = _tennis_recent(conn, away_team)
            th2h = _tennis_h2h(conn, home_team, away_team)
            conn.close()
            if tf_h or tf_a:
                out["tennis_form"] = {"home": tf_h, "away": tf_a}
            if th2h:
                out["tennis_h2h"] = th2h
            # Tennis results come from sources that have stopped publishing; say
            # how old the newest match we hold for these players is.
            from datetime import datetime as _dt
            def _d(v):
                digits = re.sub(r"[^0-9]", "", str(v or ""))[:8]
                try:
                    return _dt.strptime(digits, "%Y%m%d")
                except ValueError:
                    return None
            lasts = [x for x in (_d((tf_h or {}).get("last_date")), _d((tf_a or {}).get("last_date"))) if x]
            if lasts:
                age = (_dt.utcnow() - min(lasts)).days
                out["data"] = {"home_name": home_team, "away_name": away_team,
                               "home_last_result": (_d((tf_h or {}).get("last_date")) or min(lasts)).strftime("%Y-%m-%d"),
                               "away_last_result": (_d((tf_a or {}).get("last_date")) or min(lasts)).strftime("%Y-%m-%d"),
                               "stale": age > PROGNOSIS_STALE_DAYS_TENNIS}
                if age > PROGNOSIS_STALE_DAYS_TENNIS:
                    out["notes"].append(
                        f"Atenção: os resultados de ténis que temos destes jogadores acabam há {age} dias "
                        f"— forma e Elo de ténis estão desatualizados.")
        except Exception as e:                         # noqa: BLE001
            print(f"WARN tennis prognosis: {e}", flush=True)

    # ---- Headline probabilities: the MARKET when there is a line, else Elo ----
    # Measured, not assumed (scripts/prognosis_backtest.py — walk-forward on the
    # 17,718 matches of 2022-23..2026-27, 16 divisions, that have a Pinnacle line):
    #     devigged Pinnacle price      log-loss 0.9776   52.4% right
    #     50% market + 50% Elo         log-loss 0.9870   51.8%   <- what this WAS
    #     Elo alone                    log-loss 1.0085   50.3%
    # and the log-loss falls steadily as the market's weight goes from 0 to 100%.
    # Elo knows nothing a sharp price does not already contain, so averaging it in
    # only dilutes the better number. It stays as the fallback when there is no
    # line, and as a second opinion ("concordam" / "divergem").
    spine, basis = None, None
    if market:
        spine = {k: v for k, v in market.items() if v is not None}
        basis = "mercado"
    elif elo_usable:
        labels = ["home", "draw", "away"] if elo_usable.get("draw") is not None else ["home", "away"]
        spine = {k: elo_usable.get(k) for k in labels if elo_usable.get(k) is not None}
        basis = "Elo"
    out["probs"] = spine
    out["basis"] = basis

    # ---- Confidence + agreement + best pick ----
    def _argmax(d):
        ks = [k for k in ("home", "draw", "away") if d and d.get(k) is not None]
        return max(ks, key=lambda k: d[k]) if ks else None

    if spine:
        top = max(spine, key=spine.get)
        conf = spine[top]
        sel_name = home_team if top == "home" else away_team if top == "away" else "Empate"
        out["best_pick"] = {"outcome": top, "selection": sel_name, "prob": conf}
        out["confidence"] = conf

        elo_top, mkt_top = _argmax(elo_usable), _argmax(market)
        strong_agree = (elo_top == mkt_top) if (elo_top and mkt_top) else None
        out["agreement"] = strong_agree
        goals_agree = None
        if poisson:
            pk = {"home": poisson["home"], "draw": poisson.get("draw", 0), "away": poisson["away"]}
            g_top = max(spine.keys(), key=lambda k: pk.get(k, 0))
            goals_agree = (g_top == top)
        out["goals_agreement"] = goals_agree

        # Tier = what a confidence in this band actually did in the backtest
        # (market number): >=70% was right ~81% of the time, 60-70% ~65%,
        # 50-60% ~55%, under 50% ~42%. PROGNOSIS_TIERS keeps those next to the
        # thresholds so the page can print them.
        tier = next(t["tier"] for t in PROGNOSIS_TIERS if conf >= t["min"])
        out["confidence_tier"] = tier
        out["tier_hit_rate"] = next(t["hit"] for t in PROGNOSIS_TIERS if conf >= t["min"])
        if strong_agree is False:
            out["notes"].append("O Elo aponta para outro favorito. No backtest o mercado é o "
                                "preditor mais certeiro, por isso a leitura segue o mercado.")
        if goals_agree is False:
            out["notes"].append("O modelo de golos aponta noutra direção (sinal fraco, só contexto).")

        # RELIABILITY OF THE READ = where the number comes from.
        if market:
            rel, why = "Alta", "linha sharp da Pinnacle sem margem — o melhor preditor no backtest"
        elif elo_usable:
            thin = min(elo_usable.get("games_home") or 0, elo_usable.get("games_away") or 0) < 30
            stale = bool((out.get("data") or {}).get("stale"))
            if stale or thin:
                rel = "Baixa"
                why = ("só o Elo, e com resultados desatualizados" if stale
                       else "só o Elo, com poucos jogos de uma das equipas")
            else:
                rel, why = "Média", "só o Elo (sem linha da Pinnacle) — acerta menos do que o mercado"
        else:
            rel, why = "Baixa", "dados escassos"
        out["reliability"] = rel
        out["reliability_why"] = why
    else:
        out["notes"].append("Sem linha da Pinnacle e sem Elo comparável para estas equipas — "
                            "prognóstico indisponível.")
        out["reliability"] = "Baixa"

    # ---- The goals model, as measured. It is OVERCONFIDENT: when it said 70-75%
    # for a side of Over/Under 2.5 that side came in 59.6% of the time (22,373
    # matches). Printing its raw number next to the calibrated 1X2 made a weak
    # "Over 2.5: 78%" outrank everything as "the most likely market". Shrink it to
    # what such a number has actually been worth before it is shown or ranked. ----
    if poisson:
        def _shrunk(pct, k):
            return round(50.0 + k * (pct - 50.0), 1)
        poisson["raw"] = {k: poisson[k] for k in ("over25", "under25", "btts_yes", "btts_no")}
        poisson["over25"] = _shrunk(poisson["over25"], GOALS_OU_SHRINK)
        poisson["under25"] = round(100.0 - poisson["over25"], 1)
        poisson["btts_yes"] = _shrunk(poisson["btts_yes"], GOALS_BTTS_SHRINK)
        poisson["btts_no"] = round(100.0 - poisson["btts_yes"], 1)
        poisson["calibrated"] = True

    # ---- MARKET RANKING: most-likely market per match ("qual o mercado mais
    # provável?"). 1X2-derived rows come from the headline; O/U + BTTS come from
    # the (calibrated) goals model and are flagged as auxiliary. ----
    markets = []
    if spine:
        top = max(spine, key=spine.get)
        sel_name = home_team if top == "home" else away_team if top == "away" else "Empate"
        markets.append({"market": "Resultado final (1X2)", "selection": sel_name,
                        "prob": spine[top], "source": basis})
        if spine.get("draw") is not None and top != "draw":
            # double chance fav-or-draw
            p_dc = round(min(99.0, spine[top] + spine["draw"]), 1)
            tag = "1X" if top == "home" else "X2"
            markets.append({"market": f"Dupla hipótese ({tag})",
                            "selection": f"{sel_name} ou Empate", "prob": p_dc, "source": basis})
            # draw no bet on the fav (draw = stake refunded)
            other = "away" if top == "home" else "home"
            denom = spine[top] + spine.get(other, 0)
            if denom > 0:
                markets.append({"market": "Empate anula (DNB)", "selection": sel_name,
                                "prob": round(spine[top] / denom * 100, 1),
                                "source": basis, "note": "empate devolve a stake"})
    if poisson:
        ou_side = ("Over 2.5", poisson["over25"]) if poisson["over25"] >= poisson["under25"] \
                  else ("Under 2.5", poisson["under25"])
        markets.append({"market": "Golos O/U 2.5", "selection": ou_side[0], "prob": ou_side[1],
                        "source": "modelo de golos", "auxiliary": True})
        btts_side = ("Ambas marcam: Sim", poisson["btts_yes"]) if poisson["btts_yes"] >= poisson["btts_no"] \
                    else ("Ambas marcam: Não", poisson["btts_no"])
        markets.append({"market": "BTTS", "selection": btts_side[0], "prob": btts_side[1],
                        "source": "modelo de golos", "auxiliary": True})
    markets.sort(key=lambda m: -m["prob"])
    out["markets"] = markets
    return out


def get_daily_multiple(max_legs=5, min_conf=65, window_hours=48):
    """The most likely winners among the upcoming fixtures, as an accumulator.

    Each leg's confidence is the SAME number the match page shows: the devigged
    Pinnacle price when the game has a line, Elo otherwise (and never a
    cross-league Elo). Measured on 2,933 past legs chosen this way (market >= 65%):
    they said 75.1% on average and came in 77.3% of the time.

    What changed, and why (scripts/prognosis_backtest.py):
      * confidence was the 50/50 market+Elo average — a worse predictor than the
        market alone (log-loss 0.9870 vs 0.9776);
      * a leg was dropped when the goals model disagreed. That filter cost legs
        and bought nothing: 76.6% of its legs came in, against 77.3% without it;
      * the odd shown was the best price at ANY bookmaker; it is now the user's
        own book (1xBet) whenever 1xBet quotes the leg.

    FOR FUN ONLY: an accumulator multiplies the bookmaker's margin on every leg.
    """
    # preload Elo once so per-event lookups are cheap
    global _session_elo
    try:
        conn = get_connection()
        elo_rows = [dict(r) for r in conn.execute("SELECT * FROM elo_ratings").fetchall()]
        _session_elo = {(r["entity"].lower(), r["category"], r.get("surface") or ''): r for r in elo_rows}
        rows = conn.execute(
            "SELECT event_id, sport_name, home_team, away_team, commence_time, updated_at, "
            "pin_home, pin_draw, pin_away, "
            "x1_home, x1_draw, x1_away, best_home, best_draw, best_away "
            "FROM odds_events WHERE commence_time > datetime('now') "
            f"AND commence_time < datetime('now','+{int(window_hours)} hours') "
            "ORDER BY commence_time ASC"
        ).fetchall()
    except Exception as e:
        print(f"DAILY_MULTIPLE_ERROR: {e}", flush=True)
        try: conn.close()
        except Exception: pass
        return {"legs": [], "note": "sem dados"}
    conn.close()

    # DEDUPE: the same real-world match can be stored under two sport_keys (e.g.
    # World Cup + a team's other competition key) with different event_ids and
    # even slightly different kickoff times (which can fall on two calendar days).
    # A given team-pair cannot play twice inside a 72h window, so collapse purely
    # by the (sorted) team-pair — and among duplicates keep the row with the most
    # odds populated (so a leg never shows "odd —" when a priced copy exists).
    by_fixture = {}
    for r in rows[:200]:
        d = dict(r)
        key = tuple(sorted([(d.get("home_team") or "").lower().strip(),
                            (d.get("away_team") or "").lower().strip()]))
        odds_score = sum(1 for k in ("pin_home", "best_home", "x1_home") if d.get(k))
        if key not in by_fixture or odds_score > by_fixture[key][0]:
            by_fixture[key] = (odds_score, d)
    dedup_rows = [v[1] for v in by_fixture.values()]
    dedup_rows.sort(key=lambda d: d.get("commence_time") or "")

    from datetime import datetime as _dt
    now = _dt.utcnow()
    oldest_ref_h = None
    candidates = []
    for d in dedup_rows:
        home, away, sport = d["home_team"], d["away_team"], d["sport_name"]
        category, surface = _prognosis_category(sport)
        market = _devig_pin_odds((d.get("pin_home"), d.get("pin_draw"), d.get("pin_away")))
        if market:
            probs = {k: v for k, v in market.items() if v is not None}
            basis = "mercado"
        else:
            try:
                elo = elo_based_probability(home, away, category, surface=surface, sport_name=sport)
            except Exception:
                elo = None
            if not elo or elo.get("comparable") is False:
                continue
            labels = ["home", "draw", "away"] if elo.get("draw") is not None else ["home", "away"]
            probs = {k: elo.get(k) for k in labels if elo.get(k) is not None}
            basis = "Elo"
        if not probs:
            continue
        top = max(probs, key=probs.get)
        conf = probs[top]
        if conf < min_conf:
            continue
        # The user's own book first; another book's price only when 1xBet has none.
        x1 = {"home": d.get("x1_home"), "draw": d.get("x1_draw"), "away": d.get("x1_away")}
        best = {"home": d.get("best_home"), "draw": d.get("best_draw"), "away": d.get("best_away")}
        odds_all = x1 if (x1.get(top) and x1[top] > 1) else best
        odd_book = "1xBet" if odds_all is x1 else "melhor casa"
        odd = odds_all.get(top)
        sel = home if top == "home" else away if top == "away" else "Empate"
        ref_age_h = None
        try:
            ref_age_h = round((now - _dt.strptime(str(d.get("updated_at"))[:16], "%Y-%m-%d %H:%M")).total_seconds() / 3600.0, 1)
        except (TypeError, ValueError):
            pass
        if basis == "mercado" and ref_age_h is not None:
            oldest_ref_h = ref_age_h if oldest_ref_h is None else max(oldest_ref_h, ref_age_h)
        leg = {
            "event_id": d["event_id"], "sport_name": sport, "commence_time": d["commence_time"],
            "home_team": home, "away_team": away, "selection": sel, "outcome": top,
            "confidence": conf, "basis": basis, "odd": odd, "odd_book": odd_book if odd else None,
            "ref_age_h": ref_age_h,
            # what the confidence is worth at this price: > 0 only if the book pays
            # more than the leg's fair odd, which for a single book is the exception
            "ev_pct": round((odd * conf / 100.0 - 1) * 100, 1) if (odd and odd > 1) else None,
        }
        # safe variant: double chance fav-or-draw (football only). Book odd derived
        # by dutching the two 1X2 prices: odd_DC = 1/(1/o_fav + 1/o_draw).
        if top != "draw" and probs.get("draw") is not None:
            o_top, o_dr = odds_all.get(top), odds_all.get("draw")
            leg["dc_prob"] = round(min(99.0, conf + probs["draw"]), 1)
            leg["dc_selection"] = f"{sel} ou Empate"
            if o_top and o_dr and o_top > 1 and o_dr > 1:
                leg["dc_odd"] = round(1.0 / (1.0 / o_top + 1.0 / o_dr), 2)
        candidates.append(leg)

    # Market-backed legs first (the stronger number), then by confidence.
    candidates.sort(key=lambda x: (x["basis"] != "mercado", -x["confidence"]))
    legs = candidates[:max_legs]

    def _combine(legs_, odd_key, prob_key):
        combined, joint, have_all = 1.0, 1.0, True
        for l in legs_:
            o = l.get(odd_key)
            if o and o > 1:
                combined *= o
            else:
                have_all = False
            joint *= (l.get(prob_key) or 0) / 100.0
        return (round(combined, 2) if have_all and legs_ else None,
                round(joint * 100, 1) if legs_ else None)

    combined_odd, all_hit = _combine(legs, "odd", "confidence")
    # safe accumulator: double-chance legs (fav-or-draw), much higher hit rate,
    # much shorter combined odd — only legs where DC applies
    safe_legs = [l for l in candidates if l.get("dc_prob")][:max_legs]
    safe_odd, safe_hit = _combine(safe_legs, "dc_odd", "dc_prob")
    # Expected return of the accumulator at the prices shown: the product of the
    # legs' own (odd x probability). Below 0 means the book's margin outweighs
    # the legs' edge — the normal case.
    ev = None
    if combined_odd and all_hit is not None:
        ev = round((combined_odd * all_hit / 100.0 - 1) * 100, 1)
    note = ("Informativo e para diversão. Cada perna é o resultado mais provável segundo a linha da "
            "Pinnacle sem margem (no backtest, pernas ≥65% acertaram 77%). Uma múltipla multiplica a "
            "margem da casa em cada perna, por isso o retorno esperado é normalmente negativo. "
            "Nada disto tem a ver com o sistema de edge/CLV da página Value Bets.")
    out = {
        "legs": legs,
        "combined_odd": combined_odd,
        "model_all_hit_pct": all_hit,
        "expected_return_pct": ev,
        "safe_legs": safe_legs,
        "safe_combined_odd": safe_odd,
        "safe_all_hit_pct": safe_hit,
        "oldest_reference_h": oldest_ref_h,
        "note": note,
    }
    if oldest_ref_h is not None and oldest_ref_h > 6:
        out["warning"] = (f"As odds de pelo menos uma perna têm {oldest_ref_h:.0f}h — as probabilidades e os "
                          f"preços podem já ter mudado. Atualiza as odds (↻ Refresh em Value Bets).")
    return out


def get_elo_summary():
    """Top-rated entities per category."""
    conn = get_connection()
    out = {}
    for cat in ["football_club", "national", "tennis"]:
        if cat == "tennis":
            rows = conn.execute("""
                SELECT entity, rating, games, surface FROM elo_ratings
                WHERE category = ? AND surface = 'All'
                ORDER BY rating DESC LIMIT 15
            """, (cat,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT entity, rating, games, surface FROM elo_ratings
                WHERE category = ?
                ORDER BY rating DESC LIMIT 15
            """, (cat,)).fetchall()
        out[cat] = [dict(r) for r in rows]
    total = conn.execute("SELECT COUNT(*) FROM elo_ratings").fetchone()[0]
    conn.close()
    out["total"] = total
    return out


# ==========================================================
# BETIQ UNIFIED MODEL — fuse Pinnacle + xG + Elo signals
# ==========================================================

def fuse_signals(pin_probs, xg_probs, elo_probs):
    """
    INDUSTRY-STANDARD probability calculation.
    
    Key insight (from research): Pinnacle no-vig odds ARE the true probability.
    They incorporate all available information via sharp money.
    
    APPROACH:
    1. If Pinnacle available → use Pinnacle no-vig as TRUE PROBABILITY
       (xG/Elo become CONFIDENCE INDICATORS — do models agree with market?)
    2. If Pinnacle NOT available → fallback to xG/Elo weighted average
    
    Returns dict with home/draw/away probabilities + agreement metadata.
    """
    # PRIMARY PATH: Pinnacle available
    if pin_probs and pin_probs.get("home") is not None:
        # Use Pinnacle no-vig as TRUE PROBABILITY (industry standard)
        # xG and Elo are used for AGREEMENT/CONFIDENCE, not weighting
        
        fused = {
            "home": pin_probs.get("home"),
            "draw": pin_probs.get("draw"),
            "away": pin_probs.get("away"),
        }
        
        # Calculate AGREEMENT between Pinnacle and other signals
        # (Used for confidence indication, NOT for adjusting probability)
        #
        # CRITICAL: a liquid sharp line (Pinnacle) is essentially never 25pp wrong.
        # So when our (rough) model diverges by >25pp, OUR MODEL is the unreliable
        # outlier — NOT the market. Letting it into the agreement std produced a
        # FALSE "⚠ model diverges" seal (e.g. the national xG proxy rating Brazil at
        # 12% vs a 60% market). We therefore EXCLUDE such an outlier signal from the
        # agreement (still flagged separately for transparency).
        XG_OUTLIER_PP = 25
        signals_compared = []
        xg_unreliable = False
        if xg_probs and xg_probs.get("home") is not None:
            if abs(xg_probs["home"] - pin_probs["home"]) > XG_OUTLIER_PP:
                xg_unreliable = True   # wild outlier vs a sharp line → drop from agreement
            else:
                signals_compared.append(("xg", xg_probs.get("home")))
        elo_unreliable = False
        if elo_probs and elo_probs.get("home") is not None:
            if abs(elo_probs["home"] - pin_probs["home"]) > XG_OUTLIER_PP:
                elo_unreliable = True
            else:
                signals_compared.append(("elo", elo_probs.get("home")))

        # Standard deviation of home estimates across reliable signals (incl. Pinnacle)
        all_home_estimates = [pin_probs["home"]] + [s[1] for s in signals_compared]
        if len(all_home_estimates) >= 2:
            mean = sum(all_home_estimates) / len(all_home_estimates)
            variance = sum((x - mean) ** 2 for x in all_home_estimates) / len(all_home_estimates)
            std = variance ** 0.5

            if std < 5:
                agreement = "high"  # Models confirm market
            elif std < 12:
                agreement = "medium"
            else:
                agreement = "low"  # Models genuinely diverge (and are reliable enough to matter)
        else:
            agreement = "single"  # Only Pinnacle reliable here

        mismatch_flag = "xg_diverges_strongly" if xg_unreliable else None
        
        return {
            **fused,
            "method": "pinnacle_no_vig",
            "signals_used": ["pinnacle"] + [s[0] for s in signals_compared],
            "n_signals": 1 + len(signals_compared),
            "agreement": agreement,
            "mismatch_flag": mismatch_flag,
            "primary_source": "pinnacle",
        }
    
    # FALLBACK PATH: No Pinnacle — use xG + Elo weighted average
    # (For matches where bookmaker odds aren't available yet)
    FALLBACK_WEIGHTS = {"xg": 0.65, "elo": 0.35}
    
    signals = {}
    if xg_probs and xg_probs.get("home") is not None:
        signals["xg"] = xg_probs
    if elo_probs and elo_probs.get("home") is not None:
        signals["elo"] = elo_probs
    
    if not signals:
        return None
    
    total_w = sum(FALLBACK_WEIGHTS[s] for s in signals)
    fused = {"home": 0.0, "draw": 0.0, "away": 0.0}
    has_draw = any(s.get("draw") is not None for s in signals.values())
    
    for sig_name, probs in signals.items():
        w = FALLBACK_WEIGHTS[sig_name] / total_w
        fused["home"] += (probs.get("home") or 0) * w
        fused["away"] += (probs.get("away") or 0) * w
        if probs.get("draw") is not None:
            fused["draw"] += probs["draw"] * w
    
    # Normalize to 100%
    if has_draw:
        s = fused["home"] + fused["draw"] + fused["away"]
    else:
        fused["draw"] = None
        s = fused["home"] + fused["away"]
    if s > 0:
        fused["home"] = round(fused["home"] / s * 100, 1)
        fused["away"] = round(fused["away"] / s * 100, 1)
        if has_draw:
            fused["draw"] = round(fused["draw"] / s * 100, 1)
    
    return {
        **fused,
        "method": "model_fallback",
        "signals_used": list(signals.keys()),
        "n_signals": len(signals),
        "agreement": "fallback",
        "mismatch_flag": None,
        "primary_source": "models",
    }


def diagnose_national_xg(limit=15):
    """Diagnostic for the xG home/away bug report: for each current national/WC
    event, show the market vs xG vs Elo home/away probs, the underlying gf/ga, and
    whether the xG favourite matches the market favourite. Confirms whether the xG
    is home/away-swapped (it isn't) or just a weak proxy on some teams."""
    from collectors.national_xg import predict_national_match, _get_team_xg
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM odds_events ORDER BY commence_time ASC").fetchall()
    except Exception as e:
        conn.close()
        return {"error": str(e)}
    out = []
    for r in rows:
        d = dict(r)
        from collectors.teamnames import competition_kind
        if competition_kind(d.get("sport_name")) != "national":
            continue
        home, away = d.get("home_team"), d.get("away_team")
        ref_h = d.get("bf_home") or d.get("pin_home")
        ref_a = d.get("bf_away") or d.get("pin_away")
        ref_d = d.get("bf_draw") or d.get("pin_draw")
        mkt = remove_vig_power(ref_h, ref_d, ref_a) if (ref_h and ref_a) else None
        nat = predict_national_match(home, away)
        elo = elo_based_probability(home, away, "national")
        ghx, gax = _get_team_xg(conn, home), _get_team_xg(conn, away)
        market_fav = (home if mkt["home"] >= mkt["away"] else away) if mkt else None
        xg_fav = (home if nat["prob_home"] >= nat["prob_away"] else away) if nat else None
        out.append({
            "match": f"{home} (home) vs {away} (away)",
            "market_home%": round(mkt["home"], 1) if mkt else None,
            "market_away%": round(mkt["away"], 1) if mkt else None,
            "xg_home%": nat["prob_home"] if nat else None,
            "xg_away%": nat["prob_away"] if nat else None,
            "elo_home%": elo["home"] if elo else None,
            "elo_away%": elo["away"] if elo else None,
            "home_gf/ga/n": [ghx["gf"], ghx["ga"], ghx["n"]] if ghx else "NOT FOUND",
            "away_gf/ga/n": [gax["gf"], gax["ga"], gax["n"]] if gax else "NOT FOUND",
            "market_favourite": market_fav,
            "xg_favourite": xg_fav,
            "xg_matches_market": (market_fav == xg_fav) if (market_fav and xg_fav) else None,
        })
        if len(out) >= limit:
            break
    conn.close()
    return {"events": len(out), "matches": out}
