"""Unfog — the gentle planner for ADHD brains.

Landing + waitlist at /, the app itself under /app.
"""
import csv
import hashlib
import hmac
import io
import os
import re
import secrets
from datetime import date, timedelta

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer

import ai
import db

BASE = os.path.dirname(os.path.abspath(__file__))
SESSION_MAX_AGE = 60 * 60 * 24 * 30
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(title="Unfog")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
db.init()


def _secret_key():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    # persist a generated key next to the DB so sessions survive restarts
    path = os.path.join(os.path.dirname(db.DB_PATH), "secret_key")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    return key


signer = URLSafeTimedSerializer(_secret_key(), salt="unfog-session")


# ---------- auth helpers ----------

def hash_pw(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + "$" + h.hex()


def check_pw(pw: str, stored: str) -> bool:
    try:
        salt_hex, h_hex = stored.split("$")
        h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1)
        return hmac.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


def current_user(request: Request):
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        uid = signer.loads(token, max_age=SESSION_MAX_AGE)
    except Exception:
        return None
    return db.one("SELECT * FROM users WHERE id=?", (uid,))


def login_redirect(uid: int, to: str = "/app"):
    resp = RedirectResponse(to, status_code=303)
    resp.set_cookie(
        "session", signer.dumps(uid),
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
    )
    return resp


def render(request, name, **ctx):
    ctx.setdefault("m", request.query_params.get("m", ""))
    return templates.TemplateResponse(request, name, ctx)


# ---------- landing + waitlist ----------

@app.get("/")
def landing(request: Request):
    if current_user(request):
        return RedirectResponse("/app", status_code=303)
    count = db.one("SELECT COUNT(*) c FROM waitlist")["c"]
    return render(request, "landing.html", waitlist_count=count,
                  joined=request.query_params.get("joined"))


@app.post("/waitlist")
def waitlist(email: str = Form(""), website: str = Form("")):
    # `website` is a honeypot: humans never fill it
    if not website and EMAIL_RE.match(email.strip().lower()):
        db.x("INSERT OR IGNORE INTO waitlist(email) VALUES (?)", (email.strip().lower(),))
    return RedirectResponse("/?joined=1#join", status_code=303)


@app.get("/admin/waitlist.csv")
def waitlist_csv(token: str = ""):
    admin = os.environ.get("ADMIN_TOKEN")
    if not admin or not hmac.compare_digest(token, admin):
        return Response(status_code=404)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "source", "created_at"])
    for r in db.q("SELECT email, source, created_at FROM waitlist ORDER BY id"):
        w.writerow([r["email"], r["source"], r["created_at"]])
    buf.seek(0)
    return StreamingResponse(buf, media_type="text/csv")


# ---------- auth pages ----------

@app.get("/signup")
def signup_page(request: Request):
    return render(request, "auth.html", mode="signup", error="")


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return render(request, "auth.html", mode="signup", error="That email doesn't look right.")
    if len(password) < 8:
        return render(request, "auth.html", mode="signup", error="Password needs at least 8 characters.")
    if db.one("SELECT id FROM users WHERE email=?", (email,)):
        return render(request, "auth.html", mode="signup", error="That email already has an account — try logging in.")
    uid = db.x("INSERT INTO users(email, pw_hash) VALUES (?,?)", (email, hash_pw(password)))
    return login_redirect(uid)


@app.get("/login")
def login_page(request: Request):
    return render(request, "auth.html", mode="login", error="")


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = db.one("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
    if not user or not check_pw(password, user["pw_hash"]):
        return render(request, "auth.html", mode="login", error="Email or password didn't match.")
    return login_redirect(user["id"])


@app.post("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("session")
    return resp


# ---------- the app: one thing at a time ----------

def _next_task(uid):
    return db.one(
        "SELECT * FROM tasks WHERE user_id=? AND status='active' ORDER BY order_idx, id LIMIT 1",
        (uid,),
    )


def _next_step(task_id):
    return db.one(
        "SELECT * FROM microsteps WHERE task_id=? AND done=0 ORDER BY order_idx, id LIMIT 1",
        (task_id,),
    )


def _counts(uid):
    active = db.one("SELECT COUNT(*) c FROM tasks WHERE user_id=? AND status='active'", (uid,))["c"]
    done_today = db.one(
        "SELECT COUNT(*) c FROM events WHERE user_id=? AND kind='step_done' AND substr(at,1,10)=?",
        (uid, date.today().isoformat()),
    )["c"]
    return active, done_today


@app.get("/app")
def now_view(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    task = _next_task(user["id"])
    step = _next_step(task["id"]) if task else None
    steps_total = steps_done = 0
    if task:
        steps_total = db.one("SELECT COUNT(*) c FROM microsteps WHERE task_id=?", (task["id"],))["c"]
        steps_done = db.one("SELECT COUNT(*) c FROM microsteps WHERE task_id=? AND done=1", (task["id"],))["c"]
    active, done_today = _counts(user["id"])
    return render(request, "now.html", user=user, task=task, step=step,
                  steps_total=steps_total, steps_done=steps_done,
                  active=active, done_today=done_today, page="now")


@app.get("/app/dump")
def dump_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "dump.html", user=user, page="dump",
                  ai_on=bool(os.environ.get("ANTHROPIC_API_KEY")))


@app.post("/app/dump")
def dump(request: Request, text: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    tasks = ai.breakdown(text)
    base = db.one("SELECT COALESCE(MAX(order_idx),0) m FROM tasks WHERE user_id=?", (user["id"],))["m"]
    for i, t in enumerate(tasks):
        tid = db.x(
            "INSERT INTO tasks(user_id, title, estimate_min, order_idx) VALUES (?,?,?,?)",
            (user["id"], t["title"], t["estimate_min"], base + i + 1),
        )
        for j, s in enumerate(t["microsteps"]):
            db.x("INSERT INTO microsteps(task_id, title, order_idx) VALUES (?,?,?)", (tid, s, j))
    n = len(tasks)
    return RedirectResponse(f"/app?m=Unfogged+into+{n}+task{'s' if n != 1 else ''}.+One+at+a+time.", status_code=303)


@app.post("/app/step/{sid}/done")
def step_done(request: Request, sid: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    step = db.one(
        "SELECT microsteps.*, tasks.user_id uid FROM microsteps JOIN tasks ON tasks.id=microsteps.task_id WHERE microsteps.id=?",
        (sid,),
    )
    if not step or step["uid"] != user["id"]:
        return RedirectResponse("/app", status_code=303)
    db.x("UPDATE microsteps SET done=1 WHERE id=?", (sid,))
    db.x("INSERT INTO events(user_id, kind) VALUES (?,'step_done')", (user["id"],))
    left = db.one("SELECT COUNT(*) c FROM microsteps WHERE task_id=? AND done=0", (step["task_id"],))["c"]
    if left == 0:
        db.x("UPDATE tasks SET status='done', done_at=datetime('now') WHERE id=?", (step["task_id"],))
        db.x("INSERT INTO events(user_id, kind) VALUES (?,'task_done')", (user["id"],))
        return RedirectResponse("/app?m=Task+finished.+That+counted.", status_code=303)
    return RedirectResponse("/app?m=Step+done.", status_code=303)


@app.post("/app/step/{sid}/split")
def step_split(request: Request, sid: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    step = db.one(
        "SELECT microsteps.*, tasks.user_id uid FROM microsteps JOIN tasks ON tasks.id=microsteps.task_id WHERE microsteps.id=?",
        (sid,),
    )
    if not step or step["uid"] != user["id"]:
        return RedirectResponse("/app", status_code=303)
    pieces = ai.split_step(step["title"])
    db.x("DELETE FROM microsteps WHERE id=?", (sid,))
    # pieces inherit the split step's slot; ORDER BY order_idx, id keeps them in sequence
    for s in pieces:
        db.x(
            "INSERT INTO microsteps(task_id, title, done, order_idx) VALUES (?,?,0,?)",
            (step["task_id"], s, step["order_idx"]),
        )
    return RedirectResponse("/app?m=Made+it+smaller.", status_code=303)


@app.post("/app/task/{tid}/defer")
def task_defer(request: Request, tid: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    m = db.one("SELECT COALESCE(MAX(order_idx),0) m FROM tasks WHERE user_id=?", (user["id"],))["m"]
    db.x("UPDATE tasks SET order_idx=? WHERE id=? AND user_id=?", (m + 1, tid, user["id"]))
    return RedirectResponse("/app?m=Not+now+is+a+valid+answer.", status_code=303)


@app.post("/app/task/{tid}/done")
def task_done(request: Request, tid: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    task = db.one("SELECT * FROM tasks WHERE id=? AND user_id=?", (tid, user["id"]))
    if task:
        db.x("UPDATE microsteps SET done=1 WHERE task_id=?", (tid,))
        db.x("UPDATE tasks SET status='done', done_at=datetime('now') WHERE id=?", (tid,))
        db.x("INSERT INTO events(user_id, kind) VALUES (?,'task_done')", (user["id"],))
    return RedirectResponse("/app?m=Task+finished.+That+counted.", status_code=303)


@app.post("/app/task/{tid}/drop")
def task_drop(request: Request, tid: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    db.x("UPDATE tasks SET status='dropped' WHERE id=? AND user_id=?", (tid, user["id"]))
    return RedirectResponse("/app/today?m=Let+go.+Not+everything+has+to+happen.", status_code=303)


def _fmt(m):
    return f"{m // 60:02d}:{m % 60:02d}"


CONTEXT_ICON = {
    "Focus": "🎯", "Admin": "🗂️", "Calls": "📞", "Errands": "🧭",
    "Home": "🏠", "Body": "🏃", "Break": "🌿",
}


@app.get("/app/day")
def day_view(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    day = date.today().isoformat()
    rows = db.q(
        "SELECT dp.*, t.status task_status FROM day_plans dp "
        "LEFT JOIN tasks t ON t.id = dp.task_id "
        "WHERE dp.user_id=? AND dp.day=? ORDER BY dp.start_min",
        (user["id"], day),
    )
    blocks = []
    for r in rows:
        r["time"] = _fmt(r["start_min"])
        r["end"] = _fmt(min(r["start_min"] + r["dur_min"], 24 * 60 - 1))
        r["icon"] = CONTEXT_ICON.get(r["context"], "🎯")
        blocks.append(r)
    active = db.one(
        "SELECT COUNT(*) c FROM tasks WHERE user_id=? AND status='active'", (user["id"],)
    )["c"]
    return render(request, "day.html", user=user, page="plan", blocks=blocks,
                  active=active, energy=user.get("energy_peak") or "morning",
                  wake=user.get("wake_hour") or 8, sleep=user.get("sleep_hour") or 23,
                  ai_on=bool(os.environ.get("ANTHROPIC_API_KEY")))


@app.post("/app/day/prefs")
def day_prefs(request: Request, energy: str = Form("morning"),
              wake: int = Form(8), sleep: int = Form(23)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    energy = energy if energy in ("morning", "afternoon", "evening") else "morning"
    wake = max(4, min(int(wake), 12))
    sleep = max(18, min(int(sleep), 26))  # allow up to 2am (26) for night owls
    db.x("UPDATE users SET energy_peak=?, wake_hour=?, sleep_hour=? WHERE id=?",
         (energy, wake, sleep, user["id"]))
    return RedirectResponse("/app/day", status_code=303)


@app.post("/app/day/plan")
def day_make_plan(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    tasks = db.q(
        "SELECT * FROM tasks WHERE user_id=? AND status='active' ORDER BY order_idx, id",
        (user["id"],),
    )
    if not tasks:
        return RedirectResponse("/app/day?m=Nothing+to+plan+yet.+Dump+your+brain+first.", status_code=303)
    blocks = ai.plan_day(
        [{"title": t["title"], "estimate_min": t["estimate_min"]} for t in tasks],
        user.get("energy_peak") or "morning",
        user.get("wake_hour") or 8, user.get("sleep_hour") or 23,
    )
    day = date.today().isoformat()
    db.x("DELETE FROM day_plans WHERE user_id=? AND day=?", (user["id"], day))
    for b in blocks:
        tid = tasks[b["task_index"]]["id"] if b["task_index"] >= 0 else None
        kind = "break" if b["task_index"] < 0 else "task"
        db.x(
            "INSERT INTO day_plans(user_id, day, task_id, start_min, dur_min, label, context, kind, why) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user["id"], day, tid, b["start_min"], b["dur_min"], b["label"],
             b["context"], kind, b["why"]),
        )
    n = sum(1 for b in blocks if b["task_index"] >= 0)
    return RedirectResponse(f"/app/day?m=Your+day%2C+mapped.+{n}+block{'s' if n != 1 else ''}%2C+one+at+a+time.", status_code=303)


@app.get("/app/today")
def today(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    tasks = db.q(
        "SELECT tasks.*, "
        " (SELECT COUNT(*) FROM microsteps WHERE task_id=tasks.id) total, "
        " (SELECT COUNT(*) FROM microsteps WHERE task_id=tasks.id AND done=1) done_steps "
        "FROM tasks WHERE user_id=? AND status='active' ORDER BY order_idx, id",
        (user["id"],),
    )
    return render(request, "today.html", user=user, tasks=tasks, page="today")


@app.post("/app/task")
def add_task(request: Request, title: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    title = title.strip()[:200]
    if title:
        m = db.one("SELECT COALESCE(MAX(order_idx),0) m FROM tasks WHERE user_id=?", (user["id"],))["m"]
        tid = db.x(
            "INSERT INTO tasks(user_id, title, order_idx) VALUES (?,?,?)",
            (user["id"], title, m + 1),
        )
        db.x("INSERT INTO microsteps(task_id, title, order_idx) VALUES (?,?,0)", (tid, title))
    return RedirectResponse("/app/today", status_code=303)


@app.post("/app/task/{tid}/move")
def move_task(request: Request, tid: int, dir: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    tasks = db.q(
        "SELECT id, order_idx FROM tasks WHERE user_id=? AND status='active' ORDER BY order_idx, id",
        (user["id"],),
    )
    idx = next((i for i, t in enumerate(tasks) if t["id"] == tid), None)
    if idx is not None:
        swap = idx - 1 if dir == "up" else idx + 1
        if 0 <= swap < len(tasks):
            a, b = tasks[idx], tasks[swap]
            # normalize indices so a swap is always meaningful
            for i, t in enumerate(tasks):
                db.x("UPDATE tasks SET order_idx=? WHERE id=?", (i, t["id"]))
            db.x("UPDATE tasks SET order_idx=? WHERE id=?", (swap, a["id"]))
            db.x("UPDATE tasks SET order_idx=? WHERE id=?", (idx, b["id"]))
    return RedirectResponse("/app/today", status_code=303)


# ---------- focus timer ----------

@app.get("/app/focus/{tid}")
def focus(request: Request, tid: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    task = db.one("SELECT * FROM tasks WHERE id=? AND user_id=?", (tid, user["id"]))
    if not task:
        return RedirectResponse("/app", status_code=303)
    step = _next_step(tid)
    return render(request, "focus.html", user=user, task=task, step=step, page="now")


@app.post("/app/focus/{tid}/done")
def focus_done(request: Request, tid: int, minutes: int = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    minutes = max(1, min(minutes, 180))
    db.x(
        "INSERT INTO focus_sessions(user_id, task_id, minutes) VALUES (?,?,?)",
        (user["id"], tid, minutes),
    )
    db.x("INSERT INTO events(user_id, kind) VALUES (?,'focus_done')", (user["id"],))
    return RedirectResponse(f"/app?m={minutes}+minutes+of+focus.+Real+progress.", status_code=303)


# ---------- wins ----------

def _streaks(uid):
    days = [r["d"] for r in db.q(
        "SELECT DISTINCT substr(at,1,10) d FROM events WHERE user_id=? ORDER BY d DESC", (uid,)
    )]
    dayset = set(days)
    today = date.today()
    # current streak counts back from today (or yesterday, so mornings don't feel broken)
    start = today if today.isoformat() in dayset else today - timedelta(days=1)
    current = 0
    d = start
    while d.isoformat() in dayset:
        current += 1
        d -= timedelta(days=1)
    best = cur = 0
    prev = None
    for ds in sorted(dayset):
        y, m, dd = map(int, ds.split("-"))
        this = date(y, m, dd)
        cur = cur + 1 if prev and (this - prev).days == 1 else 1
        best = max(best, cur)
        prev = this
    return current, best


@app.get("/app/wins")
def wins(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    uid = user["id"]
    current, best = _streaks(uid)
    steps = db.one("SELECT COUNT(*) c FROM events WHERE user_id=? AND kind='step_done'", (uid,))["c"]
    tasks_done = db.one("SELECT COUNT(*) c FROM tasks WHERE user_id=? AND status='done'", (uid,))["c"]
    focus_min = db.one("SELECT COALESCE(SUM(minutes),0) c FROM focus_sessions WHERE user_id=?", (uid,))["c"]
    week = []
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        c = db.one(
            "SELECT COUNT(*) c FROM events WHERE user_id=? AND substr(at,1,10)=?", (uid, d)
        )["c"]
        week.append({"day": d[8:], "active": c > 0})
    return render(request, "wins.html", user=user, page="wins", current=current,
                  best=best, steps=steps, tasks_done=tasks_done,
                  focus_min=focus_min, week=week)


# ---------- pwa + health ----------

@app.get("/sw.js")
def sw():
    return FileResponse(os.path.join(BASE, "static", "sw.js"), media_type="application/javascript")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        os.path.join(BASE, "static", "manifest.webmanifest"),
        media_type="application/manifest+json",
    )


@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return JSONResponse({"ok": True})
