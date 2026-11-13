#!/usr/bin/env python3
"""The prognosis page and the Múltipla say things about the RIGHT team, from the
best number available, and admit what they do not know.

What was wrong (audit of 12-13 Nov 2026):
  * form / goal rates / H2H were looked up with LIKE '%<longest word>%', so
    Manchester United's form included City's matches;
  * Elo was computed over two spellings of every club at once (openfootball +
    football-data), giving two half-teams per club;
  * the headline probability was the 50/50 average of the market and Elo, which
    the backtest shows is WORSE than the market alone (log-loss 0.9870 vs 0.9776
    on 17,718 matches) — Elo adds nothing to a sharp price;
  * clubs of different leagues were compared by Elo, though each league's
    ratings average 1500 on their own scale;
  * the goals model's raw, overconfident % (says 72%, happens 60%) was ranked
    against the calibrated 1X2 as "the most likely market";
  * the Múltipla dropped legs on that goals model's say-so and priced them at
    the best odd of ANY bookmaker;
  * nothing said how old the results behind all this were.

Run: RAILWAY_VOLUME_MOUNT_PATH=$(mktemp -d) ODDS_API_KEY=dummy python3 tests/prognosis_test.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("RAILWAY_VOLUME_MOUNT_PATH", tempfile.mkdtemp())
os.environ.setdefault("ODDS_API_KEY", "dummy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from collectors import database as db  # noqa: E402
from collectors import teamnames as tn  # noqa: E402
from collectors import elo as elo_mod  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}\n      {detail}")
        failures += 1


NOW = datetime.utcnow()


def day(n):
    return (NOW - timedelta(days=n)).strftime("%Y-%m-%d")


def seed():
    db.init_db()
    conn = db.get_connection()
    for t in ("football_matches", "national_matches", "elo_ratings", "odds_events", "tennis_matches"):
        conn.execute(f"DELETE FROM {t}")
    rows = []

    def fd(div, d, h, a, hg, ag):
        rows.append((f"fd_{div}_{d}_{h}_{a}".replace(" ", "_"), d, "2026-27", div, div, h, a, hg, ag))

    def of(d, h, a, hg, ag):          # the other spelling of the same matches
        rows.append((f"{d}_{h}_{a}".replace(" ", "_"), d, "2026-27", "PL", "Premier League", h, a, hg, ag))

    # England: United always win 3-0, City always lose 0-2 — so a form that mixes
    # the two is impossible to miss. Enough rounds for Elo (>= 5 games each).
    eng = ["Man United", "Man City", "West Ham", "Newcastle", "Leeds", "Everton"]
    k = 3
    for rnd in range(8):
        for i, h in enumerate(eng):
            a = eng[(i + 1 + rnd) % len(eng)]
            if a == h:
                continue
            if "Man United" in (h, a):
                hg, ag = (3, 0) if h == "Man United" else (0, 3)
            elif "Man City" in (h, a):
                hg, ag = (0, 2) if h == "Man City" else (2, 0)
            else:
                hg, ag = 1, 1
            fd("E0", day(k), h, a, hg, ag)
            k += 1
    fd("E0", day(400), "Man United", "West Ham", 2, 1)
    fd("E0", day(700), "West Ham", "Man United", 0, 0)
    # the same clubs under openfootball spelling, with OPPOSITE results
    for i in range(8):
        of(day(5 + i), "Manchester United FC", "West Ham United FC", 0, 5)
        of(day(15 + i), "Manchester City FC", "Manchester United FC", 4, 0)
    # ...and one stray row of that second import that happens to use the SAME
    # spelling as football-data. Only the "football-data rows only" rule keeps it
    # out of Man United's form and H2H.
    of(day(2), "Man United", "West Ham", 0, 5)
    # Italy and Spain (other leagues), and a club whose results stopped long ago
    for i in range(8):
        fd("I1", day(4 + 7 * i), "Inter", "Milan", 2, 0)
        fd("I1", day(6 + 7 * i), "Milan", "Inter", 0, 1)
        fd("SP1", day(200 + 7 * i), "Betis", "Sevilla", 1, 0)
        fd("SP1", day(203 + 7 * i), "Sevilla", "Betis", 1, 1)
    conn.executemany(
        "INSERT OR REPLACE INTO football_matches (match_id, date, season, league, league_name, "
        "home_team, away_team, home_goals, away_goals) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    for i in range(8):
        conn.execute("INSERT OR REPLACE INTO national_matches (match_id, date, home_team, away_team, "
                     "home_goals, away_goals, tournament, neutral) VALUES (?,?,?,?,?,?,?,0)",
                     (f"n{i}", day(20 + 30 * i), "Portugal", "United States", 2, 1, "Friendly"))
    for i in range(12):                          # tennis: results that stopped ~300 days ago
        conn.execute("INSERT INTO tennis_matches (tourney_date, tourney_name, surface, winner_name, loser_name, score) "
                     "VALUES (?,?,?,?,?,?)",
                     ((NOW - timedelta(days=300 + 7 * i)).strftime("%Y%m%d"), "X Open", "Hard",
                      "Alexander Zverev", "Alexander Bublik" if i % 2 else "Jessica Nobody", "6-4 6-4"))
    conn.commit()
    conn.close()
    db._tennis_names["by_norm"] = None
    tn.invalidate()
    db._session_elo = None
    elo_mod.compute_football_elo()
    elo_mod.compute_national_elo()


def main():
    seed()
    print("\nPROGNOSIS: THE RIGHT TEAM, THE BEST NUMBER, AND WHAT IT DOES NOT KNOW\n")
    ko = (NOW + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    PIN = (1.60, 4.20, 5.80)                                  # United clear favourites

    print("1. One spelling per club")
    conn = db.get_connection()
    ents = {r["entity"] for r in conn.execute("SELECT entity FROM elo_ratings WHERE category='football_club'").fetchall()}
    conn.close()
    check("Elo rates the football-data universe only (no 'Manchester United FC' half-team)",
          "Man United" in ents and not any("FC" in e for e in ents), str(sorted(ents)))
    r_u = db.get_elo_rating("Manchester United", "football_club", sport_name="Premier League")
    r_c = db.get_elo_rating("Manchester City", "football_club", sport_name="Premier League")
    check("'Manchester United' finds Man United's rating, 'Manchester City' finds Man City's",
          r_u and r_c and r_u["entity"] == "Man United" and r_c["entity"] == "Man City"
          and r_u["rating"] > r_c["rating"], f"{r_u} / {r_c}")
    check("a team with no results gets NO rating instead of a look-alike's",
          db.get_elo_rating("Inter Miami", "football_club", sport_name="Champions League") is None
          and db.get_elo_rating("Flamengo", "football_club", sport_name="Brazil Série A") is None,
          "a rating was returned")
    check("a league we hold no results for never borrows a namesake from another country ('Inter' in Japan)",
          db.get_elo_rating("Inter", "football_club", sport_name="J League") is None
          and db.get_elo_rating("Inter", "football_club", sport_name="Serie A") is not None,
          str(db.get_elo_rating("Inter", "football_club", sport_name="J League")))

    print("\n2. Form, goals and H2H are this team's own")
    p = db.get_match_prognosis("Manchester United", "West Ham United", "Premier League", ko, pin_odds=PIN)
    f_h = (p.get("form") or {}).get("home") or {}
    check("Manchester United's form is all wins (City's defeats are not mixed in)",
          f_h.get("losses") == 0 and f_h.get("wins", 0) >= 5, str(f_h))
    check("its goal rate is its own: 3 scored, 0 conceded per game",
          p["poisson"] and abs(p["poisson"]["lambda_home"] - (3 + 1.0) / 2 * 1.10) < 0.35,
          str(p.get("poisson")))
    h2h = p.get("h2h") or {}
    check("H2H is only Man United v West Ham, from one spelling (no 0-5 from the duplicate universe)",
          h2h.get("n", 0) >= 2 and all({m["home_team"], m["away_team"]} == {"Man United", "West Ham"}
                                       for m in h2h["matches"])
          and not any(m["away_goals"] == 5 for m in h2h["matches"]), str(h2h))
    check("the page says which results it used and how recent they are",
          p["data"]["home_name"] == "Man United" and p["data"]["away_name"] == "West Ham"
          and p["data"]["home_last_result"] and p["data"]["stale"] is False, str(p.get("data")))

    print("\n3. The headline is the market's number; Elo is the fallback")
    mk = db._devig_pin_odds(PIN)
    check("with a Pinnacle line the probability shown IS the devigged line (not averaged with Elo)",
          p["basis"] == "mercado" and p["probs"] == {k: v for k, v in mk.items() if v is not None}
          and p["confidence"] == mk["home"], f"basis={p['basis']} probs={p['probs']} market={mk}")
    pw = db.remove_vig_power(*PIN)
    check("...devigged with the same (power) method Value Bets uses",
          abs(mk["home"] - round(pw["home"], 1)) < 1e-9 and abs(mk["away"] - round(pw["away"], 1)) < 1e-9,
          f"{mk} vs {pw}")
    check("the tier comes with what that tier did in the backtest",
          p["confidence_tier"] in ("Média-alta", "Média", "Alta") and p.get("tier_hit_rate") in (55, 65, 81),
          f"{p['confidence_tier']} / {p.get('tier_hit_rate')}")
    p2 = db.get_match_prognosis("Manchester United", "West Ham United", "Premier League", ko, pin_odds=None)
    check("without a line it falls back to Elo, and says the read is weaker",
          p2["basis"] == "Elo" and p2["reliability"] in ("Média", "Baixa") and p["reliability"] == "Alta",
          f"basis={p2['basis']} rel={p2['reliability']} / with line rel={p['reliability']}")

    print("\n4. Ratings of different leagues are never compared")
    x = db.get_match_prognosis("Manchester United", "Inter Milan", "Champions League", ko, pin_odds=(2.4, 3.4, 3.0))
    check("a European-cup tie between two countries is read from the market only",
          x["basis"] == "mercado" and x["elo"] and x["elo"]["comparable"] is False
          and any("não é comparável" in n for n in x["notes"]), f"basis={x['basis']} elo={x['elo']} notes={x['notes']}")
    x2 = db.get_match_prognosis("Manchester United", "Inter Milan", "Champions League", ko, pin_odds=None)
    check("...and with no line there is no probability at all, rather than a cross-league Elo guess",
          x2["probs"] is None and x2["best_pick"] is None, f"probs={x2['probs']}")
    e = db.get_match_prognosis("Manchester United", "Inter Milan", "Europa League", ko, pin_odds=(2.4, 3.4, 3.0))
    check("the Europa League is a CLUB competition (its teams get their club data)",
          e["category"] == "football_club" and e["data"]["home_name"] == "Man United", f"{e['category']} {e.get('data')}")
    n = db.get_match_prognosis("Portugal", "USA", "UEFA Nations League", ko, pin_odds=None)
    check("a national-team fixture uses the national ratings, with alternative spellings resolved",
          n["category"] == "national" and n["data"]["away_name"] == "United States" and n["basis"] == "Elo",
          f"{n['category']} {n.get('data')} basis={n['basis']}")

    print("\n5. It admits what it does not know")
    b = db.get_match_prognosis("Flamengo", "Sao Paulo", "Brazil Série A", ko, pin_odds=(2.0, 3.3, 3.9))
    check("a league with no results: market only, no form, and a note saying so",
          b["basis"] == "mercado" and b["form"] is None and b["poisson"] is None
          and any("Sem histórico" in t for t in b["notes"]), f"form={b['form']} notes={b['notes']}")
    s = db.get_match_prognosis("Real Betis", "Sevilla", "La Liga", ko, pin_odds=None)
    check("results that stopped ~200 days ago are flagged as stale, with the date",
          s["data"]["stale"] is True and any("desatualizados" in t for t in s["notes"])
          and s["reliability"] == "Baixa", f"data={s['data']} notes={s['notes']} rel={s['reliability']}")
    old_ref = (NOW - timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    o = db.get_match_prognosis("Manchester United", "West Ham United", "Premier League", ko, pin_odds=PIN,
                               ref_updated_at=old_ref)
    check("a 9h-old line is said to be 9h old", any("9h" in t for t in o["notes"]), str(o["notes"]))

    t = db.get_match_prognosis("Alexander Zverev", "Alexander Bublik", "ATP Tour", ko, pin_odds=(1.5, None, 2.6))
    tf = (t.get("tennis_form") or {})
    check("tennis form is the player's own (searching '%Alexander%' mixed Zverev with Bublik)",
          tf.get("home") and tf["home"]["losses"] == 0 and tf["home"]["wins"] == 8
          and tf.get("away") and tf["away"]["wins"] == 0, str(tf))
    check("tennis results that stopped ~300 days ago are flagged",
          (t.get("data") or {}).get("stale") is True and any("ténis" in x for x in t["notes"]),
          f"data={t.get('data')} notes={t['notes']}")

    print("\n6. The goals model is shown as what it has been worth")
    raw, cal = p["poisson"]["raw"], p["poisson"]
    check("a raw O/U of %.0f%% is shrunk towards 50%% (said 72%% -> happened 60%% in the backtest)" % raw["over25"],
          abs(cal["over25"] - (50 + db.GOALS_OU_SHRINK * (raw["over25"] - 50))) < 0.11
          and abs(cal["over25"] - 50) < abs(raw["over25"] - 50) and abs(cal["over25"] + cal["under25"] - 100) < 0.11,
          f"raw={raw} shown over={cal['over25']} under={cal['under25']}")
    aux = [m for m in p["markets"] if m.get("auxiliary")]
    check("no goals-model row claims more than 75% (its best measured band was right 64% of the time)",
          aux and all(m["prob"] <= 75 for m in aux), str(aux))

    print("\n7. Múltipla do Dia")
    conn = db.get_connection()
    fresh = NOW.strftime("%Y-%m-%d %H:%M")
    def ev(eid, sport, h, a, pin, x1, best, upd=fresh, hours=20):
        conn.execute("INSERT OR REPLACE INTO odds_events (event_id, sport_key, sport_name, home_team, away_team, "
                     "commence_time, pin_home, pin_draw, pin_away, x1_home, x1_draw, x1_away, best_home, best_draw, "
                     "best_away, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (eid, "soccer_x", sport, h, a, (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      *pin, *x1, *best, upd))
    # City are market favourites; the goals model (City always lose 0-2) disagrees
    ev("m1", "Premier League", "Manchester City", "Leeds United", (1.30, 5.5, 10.0), (1.28, 5.4, 9.5), (1.33, 5.8, 11.0))
    ev("m2", "Premier League", "Manchester United", "Everton", (1.40, 4.8, 8.0), (None, None, None), (1.42, 5.0, 8.5))
    ev("m3", "Premier League", "Newcastle United", "West Ham United", (2.60, 3.3, 2.80), (2.6, 3.3, 2.8), (2.7, 3.4, 2.9))
    conn.commit(); conn.close()
    m = db.get_daily_multiple()
    legs = {l["event_id"]: l for l in m["legs"]}
    check("a leg's confidence is the devigged market price",
          "m1" in legs and legs["m1"]["confidence"] == db._devig_pin_odds((1.30, 5.5, 10.0))["home"]
          and legs["m1"]["basis"] == "mercado", str(legs.get("m1")))
    check("a market favourite is NOT dropped because the goals model disagrees",
          "m1" in legs, f"legs={list(legs)}")
    check("the odd shown is the user's own book (1xBet), not the best price anywhere",
          legs["m1"]["odd"] == 1.28 and legs["m1"]["odd_book"] == "1xBet", str(legs.get("m1")))
    check("...and when 1xBet does not quote the leg, the other price is labelled as such",
          legs["m2"]["odd"] == 1.42 and legs["m2"]["odd_book"] == "melhor casa", str(legs.get("m2")))
    check("a coin-flip game is not a leg", "m3" not in legs, f"legs={list(legs)}")
    exp = round((m["combined_odd"] * m["model_all_hit_pct"] / 100 - 1) * 100, 1)
    check("the expected return at the prices shown is printed (and is what odd x probability gives)",
          m.get("expected_return_pct") == exp, f"{m.get('expected_return_pct')} vs {exp}")
    conn = db.get_connection()
    conn.execute("UPDATE odds_events SET updated_at = ?", ((NOW - timedelta(hours=11)).strftime("%Y-%m-%d %H:%M"),))
    conn.commit(); conn.close()
    check("odds 11h old: the page warns that probabilities and prices may have moved",
          "warning" in db.get_daily_multiple(), "no warning")

    print("\n8. Value Bets: O/U and BTTS use the same devig as their closing line")
    conn = db.get_connection()
    conn.execute("UPDATE odds_events SET updated_at = ?, pin_over25 = 1.55, pin_under25 = 2.55, "
                 "x1_over25 = 1.60, x1_under25 = 2.50, pin_btts_yes = 1.50, pin_btts_no = 2.70, "
                 "x1_btts_yes = 1.52, x1_btts_no = 2.65 WHERE event_id = 'm1'", (fresh,))
    conn.commit(); conn.close()
    db.invalidate_value_bets_cache()
    row = next(r for r in db.get_value_bets() if r["event_id"] == "m1")
    want_o = db.remove_vig_power(1.55, None, 2.55)["home"]
    want_y = db.remove_vig_power(1.50, None, 2.70)["home"]
    prop_o = (1 / 1.55) / (1 / 1.55 + 1 / 2.55) * 100
    check("Over 2.5 true probability is the power devig (the one its CLV will be judged by)",
          abs(row["true_over25_pct"] - round(want_o, 1)) < 1e-9 and abs(want_o - prop_o) > 0.2,
          f"shown {row.get('true_over25_pct')} power {want_o:.2f} proportional {prop_o:.2f}")
    check("BTTS likewise", abs(row["true_btts_yes_pct"] - round(want_y, 1)) < 1e-9,
          f"shown {row.get('true_btts_yes_pct')} power {want_y:.2f}")

    print(f"\n{'✅ ALL CHECKS PASSED' if failures == 0 else f'❌ {failures} CHECK(S) FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
