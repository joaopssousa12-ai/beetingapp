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
MAX_EDGE = 15.0


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
        f"🎯 <b>VALUE BET — BetIQ</b>\n\n"
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
    cutoff = (datetime.now() - timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT 1 FROM telegram_alerts_sent WHERE event_id=? AND selection=? AND book=? AND sent_at > ?",
        (event_id, selection, book, cutoff),
    ).fetchone()
    return row is not None


def _mark_sent(conn, event_id, selection, book):
    conn.execute(
        "INSERT INTO telegram_alerts_sent (event_id, selection, book, sent_at) VALUES (?,?,?,?)",
        (event_id, selection, book, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
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
        picks = event.get("all_picks") or []
        for pick in picks:
            edge = pick.get("edge_pct")
            if edge is None or not (MIN_EDGE <= edge <= MAX_EDGE):
                continue
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
        cands = [p for p in (event.get("all_picks") or [])
                 if p.get("edge_pct") is not None and 3 <= p["edge_pct"] <= 15
                 and p.get("book_odd") and 1.5 <= p["book_odd"] <= 5.0]
        if not cands:
            continue
        best = max(cands, key=lambda p: (p.get("confidence", 0), p["edge_pct"]))
        picks.append((dt, event, best))

    if not picks:
        cb("Telegram digest: no value bets in the next 24h.")
        return 0

    picks.sort(key=lambda x: x[2]["edge_pct"], reverse=True)
    lines = [f"📅 <b>BetIQ — value bets (próximas 24h): {len(picks)}</b>\n"]
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
