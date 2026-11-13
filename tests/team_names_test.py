#!/usr/bin/env python3
"""A team name resolves to exactly one team, or to none. Never to a look-alike.

WHY THIS EXISTS. Results are stored under football-data.co.uk names ("Man
United", "Ath Madrid", "M'gladbach"); fixtures arrive under The Odds API names
("Manchester United", "Atlético Madrid", "Borussia Monchengladbach"). The
prognosis page used to bridge the two with `LIKE '%<longest word>%'`:

    Manchester United -> %Manchester%   (City's matches mixed into the form)
    West Ham United   -> %United%       (four other clubs)
    Borussia Dortmund -> %Borussia%     (Mönchengladbach's matches)
    Inter Milan, Atlético Madrid        (no match: silently no rating at all)

and filed the EUROpa League and the Club WORLD CUP under national teams because
their names contain "euro" and "world cup".

The vocabularies in tests/fixtures/team_names.json are REAL: every team of 16
divisions over three seasons, and every team name in the production bet history
(scripts/team_names_probe.py).

Run: RAILWAY_VOLUME_MOUNT_PATH=$(mktemp -d) python3 tests/team_names_test.py
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("RAILWAY_VOLUME_MOUNT_PATH", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from collectors import teamnames as tn  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}\n      {detail}")
        failures += 1


FX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "team_names.json"), encoding="utf-8"))
BY_COUNTRY = {}
for div, names in FX["footballdata"].items():
    BY_COUNTRY.setdefault(tn._DIV_COUNTRY[div], set()).update(names)
ALL = set().union(*BY_COUNTRY.values())
R = {c: tn.Resolver(n, tn.CLUB_ALIASES) for c, n in BY_COUNTRY.items()}
RALL = tn.Resolver(ALL, tn.CLUB_ALIASES)


def res(name, sport):
    scope = tn.club_scope(sport)
    if scope is None:
        return None
    return (RALL.resolve(name, strict=True) if scope == "*" else R[scope].resolve(name))[0]


def main():
    print("\nONE TEAM, ONE NAME\n")

    print("1. Every team in the production bet history resolves (covered leagues)")
    missing, total = [], 0
    for sport, names in FX["odds_api"]["bets"].items():
        if tn.competition_kind(sport) != "club" or tn.club_scope(sport) is None:
            continue
        for n in names:
            total += 1
            if not res(n, sport):
                missing.append(f"{sport}: {n}")
    check(f"all {total} real club names find their team", not missing and total > 70, f"unresolved: {missing}")

    print("\n2. ...and to the RIGHT team (the cases the old LIKE got wrong or missed)")
    expect = [
        ("Manchester United", "Premier League", "Man United"),
        ("Manchester City", "Premier League", "Man City"),
        ("West Ham United", "Premier League", "West Ham"),
        ("Newcastle United", "Premier League", "Newcastle"),
        ("Leeds United", "Premier League", "Leeds"),
        ("Nottingham Forest", "Premier League", "Nott'm Forest"),
        ("Wolverhampton Wanderers", "Premier League", "Wolves"),
        ("Brighton and Hove Albion", "Premier League", "Brighton"),
        ("Tottenham Hotspur", "Premier League", "Tottenham"),
        ("Sheffield Wednesday", "Championship", "Sheffield Weds"),
        ("Sheffield United", "Championship", "Sheffield United"),
        ("West Bromwich Albion", "Championship", "West Brom"),
        ("Queens Park Rangers", "Championship", "QPR"),
        ("Borussia Dortmund", "Bundesliga", "Dortmund"),
        ("Borussia Monchengladbach", "Bundesliga", "M'gladbach"),
        ("Bayer Leverkusen", "Bundesliga", "Leverkusen"),
        ("Bayern Munich", "Bundesliga", "Bayern Munich"),
        ("Eintracht Frankfurt", "Bundesliga", "Ein Frankfurt"),
        ("1. FC Köln", "Bundesliga", "FC Koln"),
        ("FC St. Pauli", "Bundesliga", "St Pauli"),
        ("Hamburger SV", "Bundesliga", "Hamburg"),
        ("Atlético Madrid", "La Liga", "Ath Madrid"),
        ("Athletic Bilbao", "La Liga", "Ath Bilbao"),
        ("Real Madrid", "La Liga", "Real Madrid"),
        ("Real Sociedad", "La Liga", "Sociedad"),
        ("Real Sociedad B", "La Liga 2", "Sociedad B"),
        ("Real Betis", "La Liga", "Betis"),
        ("Rayo Vallecano", "La Liga", "Vallecano"),
        ("Deportivo La Coruña", "La Liga", "La Coruna"),
        ("Real Racing Club de Santander", "La Liga", "Santander"),
        ("Espanyol", "La Liga", "Espanol"),
        ("Inter Milan", "Serie A", "Inter"),
        ("AC Milan", "Serie A", "Milan"),
        ("AS Roma", "Serie A", "Roma"),
        ("Hellas Verona", "Serie A", "Verona"),
        ("Juventus", "Serie A", "Juventus"),
        ("Paris Saint Germain", "Ligue 1", "Paris SG"),
        ("Paris FC", "Ligue 1", "Paris FC"),
        ("Saint Etienne", "Ligue 1", "St Etienne"),
        ("Sporting Lisbon", "Primeira Liga", "Sp Lisbon"),
        ("Braga", "Primeira Liga", "Sp Braga"),
        ("FC Porto", "Primeira Liga", "Porto"),
        ("Vitória SC", "Primeira Liga", "Guimaraes"),
        ("Fortuna Sittard", "Eredivisie", "For Sittard"),
        ("FC Twente Enschede", "Eredivisie", "Twente"),
        ("Club Brugge", "Jupiler Pro League", "Club Brugge"),
        ("Cercle Brugge KSV", "Jupiler Pro League", "Cercle Brugge"),
        ("Union Saint-Gilloise", "Jupiler Pro League", "St. Gilloise"),
        ("Sint Truiden", "Jupiler Pro League", "St Truiden"),
        ("Istanbul Basaksehir", "Super Lig", "Buyuksehyr"),
        ("Besiktas JK", "Super Lig", "Besiktas"),
        ("Olympiacos", "Super League Greece", "Olympiakos"),
        ("Heart of Midlothian", "Scottish Premiership", "Hearts"),
        ("Dundee", "Scottish Premiership", "Dundee"),
        ("Dundee United", "Scottish Premiership", "Dundee United"),
        # European cups: any country
        ("Inter Milan", "Champions League", "Inter"),
        ("Borussia Dortmund", "Champions League", "Dortmund"),
        ("Aston Villa", "Europa League", "Aston Villa"),
        ("Real Sociedad", "Europa League", "Sociedad"),
    ]
    wrong = [(q, sp, want, res(q, sp)) for q, sp, want in expect if res(q, sp) != want]
    check(f"{len(expect)} known names map to the expected team", not wrong,
          "\n      ".join(f"{q} [{sp}] -> {got!r}, expected {want!r}" for q, sp, want, got in wrong))

    print("\n3. A team we hold no results for resolves to NOTHING")
    traps = ["Red Star Belgrade", "Aris Limassol", "AEK Larnaca", "Sparta Prague", "Slavia Prague",
             "Dinamo Zagreb", "Young Boys", "Shakhtar Donetsk", "Sturm Graz", "FC Copenhagen",
             "Ferencvaros", "Bodo/Glimt", "Inter Miami", "Real Salt Lake", "Maccabi Tel Aviv"]
    got = {t: res(t, "Champions League") for t in traps}
    check("look-alikes in a European cup stay unresolved",
          all(v is None for v in got.values()), str({k: v for k, v in got.items() if v}))
    # ...by RULE, not only because someone listed them: without any alias, a name
    # that merely CONTAINS one of ours ("Aris" in "Aris Limassol") is refused when
    # the part left over outweighs the part that matched.
    bare_all = tn.Resolver(ALL, {})
    rule = {t: bare_all.resolve(t, strict=True)[0] for t in ("Red Star Belgrade", "Aris Limassol", "AEK Larnaca")}
    check("the same traps are refused with the alias list empty", all(v is None for v in rule.values()), str(rule))
    check("a league we do not collect results for resolves nobody (Brazil, Japan)",
          res("Flamengo", "Brazil Série A") is None and res("Sao Paulo", "Brazil Série A") is None
          and res("Inter", "J League") is None,
          f"{res('Flamengo', 'Brazil Série A')!r} / {res('Inter', 'J League')!r}")
    check('"Aston Villa" is not Villarreal (a word that merely STARTS another is not that word)',
          R["es"].resolve("Aston Villa")[0] is None, str(R["es"].resolve("Aston Villa")))
    check('"Manchester" alone is ambiguous, not a coin toss',
          R["en"].resolve("Manchester")[0] is None, str(R["en"].resolve("Manchester")))
    bare = tn.Resolver(BY_COUNTRY["it"], {})
    check('"Inter Milan" without its alias is refused (Inter and Milan fit equally), not guessed',
          bare.resolve("Inter Milan")[0] is None, str(bare.resolve("Inter Milan")))

    fp = []
    for c, names in BY_COUNTRY.items():
        rest = set().union(*[n for cc, n in BY_COUNTRY.items() if cc != c]) - names
        r = tn.Resolver(rest, {})
        for n in sorted(names):
            for strict in (True, False):
                hit = r.resolve(n, strict=strict)
                if hit[0]:
                    fp.append((n, hit, strict))
    check(f"leave-one-country-out: none of {len(ALL)} real teams is mistaken for a team of another country",
          not fp, str(fp[:8]))

    print("\n4. Competitions are classified by what they are, not by a substring")
    kinds = {
        "Europa League": "club", "Champions League": "club", "FIFA Club World Cup 2026": "club",
        "La Liga": "club", "Brazil Série A": "club", "Serie A": "club",
        "FIFA World Cup 2026": "national", "UEFA Euro": "national", "Copa América": "national",
        "UEFA Nations League": "national",
        "ATP US Open": "tennis", "WTA Wimbledon": "tennis", "ATP Tour": "tennis",
    }
    bad = {k: tn.competition_kind(k) for k, v in kinds.items() if tn.competition_kind(k) != v}
    check("Europa League and Club World Cup are CLUB competitions; Euro and World Cup are national",
          not bad, str(bad))
    check("scopes: a domestic league maps to its country, European cups to all, Brazil to none",
          tn.club_scope("Serie A") == "it" and tn.club_scope("Brazil Série A") is None
          and tn.club_scope("Champions League") == "*" and tn.club_scope("La Liga 2") == "es",
          str([tn.club_scope(x) for x in ("Serie A", "Brazil Série A", "Champions League", "La Liga 2")]))
    from collectors.odds import SPORT_GROUPS
    soccer = {v for k, v in SPORT_GROUPS.items() if k.startswith("soccer")}
    unknown = sorted(n for n in soccer if tn.competition_kind(n) == "club" and tn.club_scope(n) is None
                     and n not in ("Brazil Série A", "J League"))
    check("every club competition the app collects has a declared scope", not unknown, str(unknown))

    print("\n5. National teams")
    nat = tn.Resolver({"United States", "South Korea", "Czech Republic", "Turkey", "Republic of Ireland",
                       "Northern Ireland", "Ivory Coast", "England", "Portugal", "Curaçao", "China PR"},
                      tn.NATIONAL_ALIASES)
    pairs = [("USA", "United States"), ("Korea Republic", "South Korea"), ("Czechia", "Czech Republic"),
             ("Türkiye", "Turkey"), ("Ireland", "Republic of Ireland"), ("Côte d'Ivoire", "Ivory Coast"),
             ("Curacao", "Curaçao"), ("Portugal", "Portugal"), ("Northern Ireland", "Northern Ireland")]
    wrong = [(q, nat.resolve(q)[0], w) for q, w in pairs if nat.resolve(q)[0] != w]
    check("the usual alternative spellings resolve", not wrong, str(wrong))

    print(f"\n{'✅ ALL CHECKS PASSED' if failures == 0 else f'❌ {failures} CHECK(S) FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
