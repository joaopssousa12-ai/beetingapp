#!/usr/bin/env python3
"""The in-app scheduler must actually run its jobs.

WHY THIS EXISTS — the jobs never ran. Not once.

main.py registered every scheduled job as

    scheduler.add_job(lambda: asyncio.create_task(job()), "cron", ...)

APScheduler's AsyncIOScheduler runs a COROUTINE FUNCTION on the event loop, but
hands any other callable — a lambda included — to a worker thread. In that
thread asyncio.create_task() has no running loop and raises RuntimeError, which
APScheduler logs and moves on. So all five jobs (nightly collection, 2-hourly
odds refresh, 15-minute closing capture, daily digest, history purge) failed on
every firing from the day they were written.

Production showed it plainly once somebody looked (2026-11-12): the collection
log held only manual refreshes, the results table ended on 31 May, and a
process that had been up for eleven days had counted 410 capture ticks where a
working */15 job alone would have counted over a thousand. The closing capture
was only alive because an external cron calls /api/closing/run.

Run: RAILWAY_VOLUME_MOUNT_PATH=$(mktemp -d) ODDS_API_KEY=dummy \
     python3 tests/scheduler_test.py
"""
import asyncio
import inspect
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

os.environ.setdefault("RAILWAY_VOLUME_MOUNT_PATH", tempfile.mkdtemp())
os.environ.setdefault("ODDS_API_KEY", "dummy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collectors import database as db  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}\n      {detail}")
        failures += 1


class RecordingScheduler:
    """Stands in for AsyncIOScheduler: remembers what startup() registers."""

    def __init__(self):
        self.jobs, self.running = [], False

    def add_job(self, func, trigger=None, **kw):
        self.jobs.append({"func": func, "trigger": trigger, "kw": kw})

    def start(self):
        self.running = True

    def get_jobs(self):
        return [types.SimpleNamespace(id=j["kw"].get("id"), next_run_time=None) for j in self.jobs]

    def shutdown(self):
        pass


def registered_jobs(app_main, env=None):
    """Run the real startup() against a recording scheduler; return its jobs."""
    saved = {k: getattr(app_main, k) for k in
             ("scheduler", "init_db", "get_stats", "load_vb_cache_from_disk", "refresh_value_bets",
              "AUTO_IMMINENT_REFRESH", "AUTO_NIGHTLY_ODDS")}
    saved_db = (db.purge_out_of_scope_and_stale, db.purge_old_odds_history)
    rec = RecordingScheduler()
    app_main.scheduler = rec
    app_main.init_db = lambda: None
    app_main.get_stats = lambda: {"football_matches": 1}       # no auto-bootstrap
    app_main.load_vb_cache_from_disk = lambda: None
    app_main.refresh_value_bets = lambda: None
    db.purge_out_of_scope_and_stale = lambda: {"out_of_scope": 0, "stale": 0, "placeholders": 0}
    db.purge_old_odds_history = lambda: 0
    for k, v in (env or {}).items():
        setattr(app_main, k, v)
    try:
        asyncio.run(app_main.startup())
    finally:
        for k, v in saved.items():
            setattr(app_main, k, v)
        db.purge_out_of_scope_and_stale, db.purge_old_odds_history = saved_db
    return rec.jobs


def main():
    db.init_db()
    import main as app_main

    print("\nTHE IN-APP SCHEDULER MUST ACTUALLY RUN ITS JOBS\n")

    # ── 1. what startup() registers ──────────────────────────────────────────
    print("1. Every job is handed to APScheduler as a coroutine function")
    jobs = registered_jobs(app_main)
    check("startup registers jobs at all", len(jobs) >= 3, f"{len(jobs)} jobs")
    for j in jobs:
        name = getattr(j["func"], "__name__", repr(j["func"]))
        check(f"job '{j['kw'].get('id')}' -> {name} is a coroutine function",
              inspect.iscoroutinefunction(j["func"]),
              "a plain callable is run in a worker thread, where asyncio.create_task() "
              "raises 'no running event loop' — the job would never execute")
    check("no job is a lambda wrapper",
          all(getattr(j["func"], "__name__", "") != "<lambda>" for j in jobs),
          "found a lambda")
    ids = [j["kw"].get("id") for j in jobs]
    check("every job has a unique id (so diagnostics can name it)",
          all(ids) and len(set(ids)) == len(ids), f"ids={ids}")

    # ── 2. nothing that spends credits runs by itself ────────────────────────
    print("\n2. Reviving the scheduler does not start spending Odds-API credits")
    check("the closing-capture backup is scheduled", "closing" in ids, f"ids={ids}")
    check("the nightly model refresh (free sources) is scheduled", "models" in ids, f"ids={ids}")
    paid = {"imminent", "nightly_odds"}
    check("no odds-sweep job is scheduled by default",
          not (paid & set(ids)),
          f"scheduled {paid & set(ids)} — these jobs never ran before, and with them "
          f"the free 500 credits/month would not last the month")
    funcs = {j["kw"].get("id"): j["func"] for j in jobs}
    check("the nightly job is the model refresh, not the full collection (which sweeps odds)",
          funcs.get("models") is app_main.run_models_refresh, f"got {funcs.get('models')}")
    opt_in = [j["kw"].get("id") for j in registered_jobs(
        app_main, {"AUTO_IMMINENT_REFRESH": True, "AUTO_NIGHTLY_ODDS": True})]
    check("...but both can be switched on by environment flag",
          paid <= set(opt_in), f"ids with flags on: {opt_in}")

    # ── 3. the real library, the real idiom ──────────────────────────────────
    print("\n3. With the real APScheduler the job body executes")
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    ran = {"new": 0, "old": 0}
    real_purge = db.purge_old_odds_history

    def fake_purge():
        ran["new"] += 1
        return 0

    async def old_style_job():
        ran["old"] += 1

    async def drive():
        s = AsyncIOScheduler()
        soon = datetime.now() + timedelta(seconds=0.3)
        # exactly what main.py registers now
        s.add_job(app_main.run_history_purge, "date", run_date=soon)
        # exactly what main.py registered before
        s.add_job(lambda: asyncio.create_task(old_style_job()), "date", run_date=soon)
        s.start()
        await asyncio.sleep(1.5)
        s.shutdown(wait=False)

    import logging
    logging.getLogger("apscheduler").setLevel(logging.CRITICAL)   # the old idiom logs a traceback
    db.purge_old_odds_history = fake_purge
    try:
        asyncio.run(drive())
    finally:
        db.purge_old_odds_history = real_purge
    check("a job registered the way main.py does it now RUNS", ran["new"] == 1,
          f"body executed {ran['new']} time(s)")
    check("(for the record) the old lambda idiom never runs its job",
          ran["old"] == 0, f"it ran {ran['old']} time(s) — APScheduler changed; re-check the fix")

    # ── 4. the two drivers of the capture are told apart ─────────────────────
    print("\n4. Diagnostics can tell the in-app tick from the external cron")
    from fastapi.testclient import TestClient
    client = TestClient(app_main.app)
    before_i = app_main.closing_state.get("internal_ticks", 0)
    before_e = app_main.closing_state.get("external_calls", 0)
    asyncio.run(app_main.run_closing_capture())
    check("an in-app tick is counted as internal",
          app_main.closing_state.get("internal_ticks", 0) == before_i + 1
          and app_main.closing_state.get("external_calls", 0) == before_e,
          str({k: app_main.closing_state.get(k) for k in ("internal_ticks", "external_calls")}))
    client.get("/api/closing/run?within=45")
    check("a call to /api/closing/run is counted as external",
          app_main.closing_state.get("external_calls", 0) == before_e + 1
          and app_main.closing_state.get("internal_ticks", 0) == before_i + 1,
          str({k: app_main.closing_state.get(k) for k in ("internal_ticks", "external_calls")}))
    d = client.get("/api/diag/closing").json()
    check("/api/diag/closing reports both drivers and the scheduler's jobs",
          "drivers" in d and "scheduler" in d
          and d["drivers"].get("internal_ticks") == before_i + 1,
          f"keys={sorted(d.keys())}")

    print(f"\n{'✅ ALL CHECKS PASSED' if failures == 0 else f'❌ {failures} CHECK(S) FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
