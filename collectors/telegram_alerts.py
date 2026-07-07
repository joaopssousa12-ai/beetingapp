"""
Telegram alerts for value bets.

Setup (Render environment variables):
  TELEGRAM_BOT_TOKEN  = token from @BotFather
  TELEGRAM_CHAT_ID    = your personal chat ID (get via @userinfobot)

How to create bot:
  1. Open Telegram → search @BotFather → /newbot → follow steps → copy token
  2. Send any message to your new bot
  3. Open @userinfobot → it replies with your chat ID
  4. Add both vars to Render → done
"""

import os
import requests
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_URL = "https://api.telegram.org/bot{token}/sendMessage"

MIN_EDGE = 2.0
MAX_EDGE = 15.0   # above this the "edge" is almost always model noise, not value

# The user bets exclusively at 1xBet — alerting a price at another book is an
# edge they cannot buy.
USER_BOOK = "1xBet"


def _is_green(edge, odd):
    """Same traffic-light rule as the site's 🟢 APOSTAR tier (vbSignal in app.js),
    judged on the 1xBet price: odd 1.8-4.0 needs edge ≥2%, odd 1.3-1.8 needs ≥3%.
    Everything else (short odds <1.3, longshots >4.0, thin edges) is 🟡/🔴 — the
    site says to skip those, so Telegram must not sell them as picks."""
    if edge is None or odd is None or edge > MAX_EDGE:
        return False
    if 1.8 <= odd <= 4.0:
        return edge >= 2.0
    if 1.3 <= odd < 1.8:
        return edge >= 3.0
    return False


def _green_1xbet_picks(event):
    """The event's 1xBet-priced picks that clear the green gate."""
    return [p for p in (event.get("all_picks") or [])
            if p.get("book") == USER_BOOK and p.get("book_odd")
            and _is_green(p.get("edge_pct"), p.get("book_odd"))]


# Instant alerts only fire for games inside the REAL betting window — an alert is
# an "act now" signal. Weeks-out season openers carry placeholder lines (phantom
# edges, no true close to beat) and by kickoff the price is long gone anyway.
ALERT_WINDOW_HOURS = {"tennis": 3, "football": 6}


def _within_betting_window(event):
    """True when the event starts between now and its sport's alert window."""
    commence = event.get("commence_time") or ""
    try:
        dt = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    sport_key = (event.get("sport_key") or "").lower()
    hours = ALERT_WINDOW_HOURS["tennis" if sport_key.startswith("tennis") else "football"]
    now = datetime.now(timezone.utc)
    return now <= dt <= now + timedelta(hours=hours)


def _send(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        r = requests.post(
            API_URL.format(token=BOT_TOKEN),
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"Telegram send error: {e}", flush=True)
        return False


def _stars(n):
    return "⭐" * (n or 0) + "☆" * (5 - (n or 0))


def _format_alert(pick, event):
    sport = event.get("sport_name", "")
    home = event.get("home_team", "")
    away = event.get("away_team", "")
    commence = event.get("commence_time", "")
    odds_source = event.get("odds_source", "xg_model")
    source_label = {"betfair": "⚡ Betfair Exchange", "pinnacle": "📌 Pinnacle", "xg_model": "🔬 xG Model"}.get(odds_source, odds_source)

    try:
        dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        date_str = dt.strftime("%d %b · %H:%M")
    except Exception:
        date_str = commence[:16] if commence else "—"

    edge_sign = "+" if pick["edge_pct"] > 0 else ""
    kelly_str = f"{pick['kelly_pct']:.1f}%" if pick.get("kelly_pct", 0) > 0 else "—"
    stars_str = _stars(pick.get("confidence", 0))

    return (
        f"🟢 <b>APOSTAR — BetIQ</b>\n\n"
        f"⚽ <b>{home} vs {away}</b>\n"
        f"🏆 {sport} · {date_str}\n\n"
        f"▶ <b>{pick['selection']}</b> — {pick['market']}\n"
        f"📊 Prob real: <b>{pick['true_prob']}%</b>  ({source_label})\n"
        f"💰 <b>{pick['book']} {pick['book_odd']:.2f}</b>\n"
        f"📈 Edge: <b>{edge_sign}{pick['edge_pct']:.1f}%</b>\n"
        f"💡 Kelly: aposta <b>{kelly_str} do bankroll</b> (¼ Kelly)\n"
        f"{''.join(['⭐' if i < (pick.get('confidence') or 0) else '☆' for i in range(5)])}\n"
    )


def _already_sent(conn, event_id, selection, book):
    cutoff = (datetime.utcnow() - timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT 1 FROM telegram_alerts_sent WHERE event_id=? AND selection=? AND book=? AND sent_at > ?",
        (event_id, selection, book, cutoff),
    ).fetchone()
    return row is not None


def _mark_sent(conn, event_id, selection, book):
    conn.execute(
        "INSERT INTO telegram_alerts_sent (event_id, selection, book, sent_at) VALUES (?,?,?,?)",
        (event_id, selection, book, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()


def send_alerts_for_value_bets(value_bets, status_callback=None):
    """
    Check all value bets and send Telegram alerts for picks with real edge.
    Deduplicates: same event+selection+book alerted at most once per 20h.
    Returns number of alerts sent.
    """
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    if not BOT_TOKEN or not CHAT_ID:
        cb("Telegram: credentials not set (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) — skipping.")
        return 0

    from collectors.database import get_connection
    conn = get_connection()

    sent = 0
    for event in value_bets:
        # Actionable window only: football starting <6h, tennis <3h.
        if not _within_betting_window(event):
            continue
        # Only 🟢 green picks at the user's book — one alert per event (the best),
        # mirroring the site's traffic light instead of spamming every 2% edge.
        greens = _green_1xbet_picks(event)
        if not greens:
            continue
        for pick in [max(greens, key=lambda p: ((p.get("confidence") or 0) * 100 + p["edge_pct"]))]:
            edge = pick.get("edge_pct")
            eid = event.get("event_id", "")
            sel = pick.get("selection", "")
            book = pick.get("book", "")

            if _already_sent(conn, eid, sel, book):
                continue

            text = _format_alert(pick, event)
            ok = _send(text)
            if ok:
                _mark_sent(conn, eid, sel, book)
                sent += 1
                cb(f"Telegram ✓ alert sent: {event.get('home_team')} vs {event.get('away_team')} — {sel} ({book} {pick.get('book_odd'):.2f} +{edge:.1f}%)")
            else:
                cb(f"Telegram ✗ failed to send alert for {sel}")

    conn.close()
    cb(f"Telegram: {sent} alert(s) sent.")
    return sent


def send_daily_digest(value_bets, status_callback=None):
    """Once-a-day summary: the green value bets (edge ≥3%, odd 1.5-5) starting in
    the next 24h, in ONE Telegram message. Best edges first."""
    def cb(msg):
        print(msg, flush=True)
        if status_callback:
            status_callback(msg)

    if not BOT_TOKEN or not CHAT_ID:
        cb("Telegram digest: credentials not set — skipping.")
        return 0

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=24)
    picks = []
    for event in value_bets:
        commence = event.get("commence_time", "") or ""
        try:
            dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if not (now <= dt <= horizon):   # only upcoming, within 24h
            continue
        # Same 🟢 gate as the instant alerts: 1xBet price, traffic-light bands.
        cands = _green_1xbet_picks(event)
        if not cands:
            continue
        best = max(cands, key=lambda p: (p.get("confidence", 0), p["edge_pct"]))
        picks.append((dt, event, best))

    if not picks:
        cb("Telegram digest: no value bets in the next 24h.")
        return 0

    picks.sort(key=lambda x: x[2]["edge_pct"], reverse=True)
    lines = [f"📅 <b>BetIQ — apostas 🟢 na 1xBet (próximas 24h): {len(picks)}</b>\n"]
    for dt, event, p in picks[:15]:
        home, away = event.get("home_team", ""), event.get("away_team", "")
        t = dt.strftime("%d %b %H:%M")
        stars = "⭐" * (p.get("confidence", 0) or 0)
        lines.append(
            f"▶ <b>{p['selection']}</b> @ {p['book_odd']:.2f} (+{p['edge_pct']:.1f}%) {stars}\n"
            f"   {home} v {away} · {t}"
        )
    lines.append("\n💡 Confirma o Est. CLV no 1xBet antes de apostar.")
    ok = _send("\n".join(lines))
    cb(f"Telegram digest: {'sent' if ok else 'FAILED'} ({len(picks)} bets).")
    return 1 if ok else 0
