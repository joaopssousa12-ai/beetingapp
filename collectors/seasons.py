"""Which football seasons exist right now — computed, never hardcoded.

Every collector used to carry its own literal list of seasons, and every one of
them ended at 2025-26. When the 2026-27 season started nothing told the app, so
the results table — and with it the Elo ratings, the form, the goal rates and
the H2H behind the prognosis page — stopped at 31 May 2026 and stayed there.
A season list that needs a code edit every August is a bug with a date on it.
"""
from datetime import datetime

# European seasons run August-May; by July the new season's files exist.
SEASON_ROLLOVER_MONTH = 7


def season_start_year(today=None):
    """Start year of the season in progress (2026 for the 2026-27 season)."""
    today = today or datetime.utcnow()
    return today.year if today.month >= SEASON_ROLLOVER_MONTH else today.year - 1


def football_seasons(n=6, today=None):
    """['2026-27', '2025-26', ...] — newest first (openfootball labels)."""
    y = season_start_year(today)
    return [f"{y - i}-{str(y - i + 1)[2:]}" for i in range(n)]


def footballdata_seasons(n=6, today=None):
    """{'2627': '2026-27', '2526': '2025-26', ...} — football-data.co.uk codes."""
    y = season_start_year(today)
    return {f"{str(y - i)[2:]}{str(y - i + 1)[2:]}": f"{y - i}-{str(y - i + 1)[2:]}"
            for i in range(n)}


def understat_seasons(n=2, today=None):
    """['2025', '2026'] — Understat keys a season by its start year."""
    y = season_start_year(today)
    return [str(y - i) for i in range(n - 1, -1, -1)]
