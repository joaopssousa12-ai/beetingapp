"""One team, one name.

Results come from football-data.co.uk, which writes "Man United", "Ath Madrid",
"M'gladbach", "Sp Lisbon". Fixtures come from The Odds API, which writes
"Manchester United", "Atlético Madrid", "Borussia Monchengladbach", "Sporting
Lisbon". Everything the prognosis page shows about a team — its Elo, its form,
its goal rates, its head-to-head — is looked up in the first vocabulary using a
name from the second.

That lookup used to be a guess: an exact match, then SQL `LIKE '%<longest word>%'`.
"Manchester United" searched for "%Manchester%" and got City's matches mixed in;
"West Ham United" searched for "%United%" and got four other clubs; "Borussia
Dortmund" searched for "%Borussia%" and got Mönchengladbach's. "Inter Milan" and
"Atlético Madrid" matched nothing and silently had no rating at all. Nothing
failed, the numbers were simply about the wrong team.

The rule here is the opposite: a name resolves to exactly one team or to none.
"None" is shown as "no data for this team"; a wrong team is never an answer.

Order of resolution, stopping at the first that decides:
  1. the name already is a results-vocabulary name
  2. a curated alias (the cases no rule can derive: Wolves, Ath Madrid, Inter)
  3. the same name after accents/punctuation/legal suffixes are removed
  4. token matching — abbreviations ("Man"/"Manchester"), prefixes
     ("Hamburg"/"Hamburger"), initialisms ("SG"/"Saint Germain") — accepted only
     when ONE candidate fits and no second candidate comes close
"""
import re
import time
import unicodedata

# ── normalisation ───────────────────────────────────────────────────────────
_TRANSLIT = str.maketrans({
    "ß": "ss", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "ı": "i", "İ": "I", "þ": "th",
})

# Legal forms and filler words that carry no identity ("FC Porto" is "Porto").
# Deliberately NOT here: real, club, sporting, athletic, city, united, town, sp,
# st — those tell teams apart (Club/Cercle Brugge, Dundee/Dundee United).
_STOP = {
    "fc", "cf", "afc", "sc", "ac", "as", "ss", "ssc", "us", "cd", "ud", "rc", "rcd",
    "sd", "ca", "sv", "vfl", "vfb", "tsg", "fsv", "bsc", "bc", "kv", "krc", "kaa",
    "kvc", "rsc", "sk", "fk", "jk", "cp", "sad", "nfc", "va", "calcio",
    "de", "del", "da", "do", "of", "and", "the", "e", "y",
    "1", "04", "05", "1899", "1846", "1860", "1909",
}


def norm(name):
    """Lower-case ASCII with punctuation removed: 'Nott'm Forest' -> 'nottm forest',
    '1. FC Köln' -> '1 fc koln', 'Preußen Münster' -> 'preussen munster'."""
    s = (name or "").translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"['’`´.]", "", s)                 # M'gladbach, St., 1.
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def tokens(name):
    """Identity-carrying words of a name ('FC Schalke 04' -> ['schalke'])."""
    all_t = norm(name).split()
    kept = [t for t in all_t if t not in _STOP]
    return kept or all_t


def _weight(tok):
    # Short markers decide identities ("Sociedad B", "Willem II"): never let
    # them weigh almost nothing.
    return max(len(tok), 3)


def _is_subsequence(short, long_):
    it = iter(long_)
    return all(ch in it for ch in short)


def _tok_match(a, b):
    """(matches, strong). Strong = carries identity on its own."""
    if a == b:
        return True, True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and long_.startswith(short) and len(long_) - len(short) <= 3:
        # the same word with an ending: hamburg/hamburger, heart/hearts, goztep/goztepe.
        # Capped at 3 letters: "villa" is also how "villarreal" starts.
        return True, True
    if (len(short) >= 2 and len(long_) - len(short) >= 2 and short[0] == long_[0]
            and _is_subsequence(short, long_)):
        # an abbreviation (man/manchester, sp/sporting, nottm/nottingham). Never
        # enough on its own: it only counts next to a word that matches properly.
        return True, False
    return False, False


def _align(q, c):
    """Match query tokens against candidate tokens. -> (mask_q, mask_c, strong)."""
    mq, mc, strong = [False] * len(q), [False] * len(c), False
    for j, ct in enumerate(c):                       # exact words first
        for i, qt in enumerate(q):
            if not mq[i] and qt == ct:
                mq[i] = mc[j] = True
                strong = True
                break
    for j, ct in enumerate(c):                       # then prefixes / abbreviations
        if mc[j]:
            continue
        best = None
        for i, qt in enumerate(q):
            if mq[i]:
                continue
            ok, st = _tok_match(qt, ct)
            if ok and (best is None or (st and not best[1])):
                best = (i, st)
        if best:
            mq[best[0]] = mc[j] = True
            strong = strong or best[1]

    def initialisms(short_side, mask_s, long_side, mask_l):
        for j, tok in enumerate(short_side):         # "sg" = Saint Germain, "oh" = Oud-Heverlee
            if mask_s[j] or not (2 <= len(tok) <= 4):
                continue
            n = len(tok)
            for i in range(0, len(long_side) - n + 1):
                run = range(i, i + n)
                if any(mask_l[k] for k in run):
                    continue
                if "".join(long_side[k][0] for k in run) == tok:
                    mask_s[j] = True
                    for k in run:
                        mask_l[k] = True
                    break
    initialisms(c, mc, q, mq)
    initialisms(q, mq, c, mc)
    return mq, mc, strong


def _score(q, c, strict):
    """Similarity in [0,1], or None when the pair must not be matched at all.

    strict=True is for competitions that mix countries: a candidate that is only
    PART of the query ("Red Star" inside "Red Star Belgrade", "Aris" inside "Aris
    Limassol") is refused when the unmatched part outweighs the matched one."""
    mq, mc, strong = _align(q, c)
    if not strong or not (all(mc) or all(mq)):
        return None
    wq, wc = sum(map(_weight, q)), sum(map(_weight, c))
    got_q = sum(_weight(t) for t, m in zip(q, mq) if m)
    got_c = sum(_weight(t) for t, m in zip(c, mc) if m)
    if strict and (wq - got_q) > got_q:
        return None
    return (got_q + got_c) / (wq + wc)


MIN_SCORE = 0.35      # below this even a unique candidate is not accepted
MIN_MARGIN = 0.08     # the runner-up must be at least this far behind


# ── curated aliases: what no rule can derive ────────────────────────────────
# key = norm(name used by The Odds API or anyone else)  ->  results-vocabulary name
# None = a known DIFFERENT club that must never be matched to a look-alike.
CLUB_ALIASES = {
    # England
    "wolverhampton wanderers": "Wolves", "wolverhampton": "Wolves",
    "queens park rangers": "QPR", "brighton and hove albion": "Brighton",
    "brighton hove albion": "Brighton",
    # Spain
    "atletico madrid": "Ath Madrid", "atletico de madrid": "Ath Madrid",
    "club atletico de madrid": "Ath Madrid",
    "athletic club": "Ath Bilbao", "athletic club bilbao": "Ath Bilbao",
    "espanyol": "Espanol", "rcd espanyol": "Espanol", "rcd espanyol barcelona": "Espanol",
    "racing santander": "Santander", "real racing club de santander": "Santander",
    "racing de ferrol": "Ferrol", "racing ferrol": "Ferrol",
    "sporting gijon": "Sp Gijon", "real sporting de gijon": "Sp Gijon",
    # Germany
    "borussia monchengladbach": "M'gladbach", "monchengladbach": "M'gladbach",
    "borussia moenchengladbach": "M'gladbach",
    "hertha berlin": "Hertha", "hertha bsc": "Hertha",
    "arminia bielefeld": "Bielefeld", "energie cottbus": "Cottbus",
    "dynamo dresden": "Dresden", "jahn regensburg": "Regensburg",
    "hannover 96": "Hannover", "ssv ulm": "Ulm", "ssv ulm 1846": "Ulm",
    # Italy
    "inter milan": "Inter", "internazionale": "Inter", "fc internazionale milano": "Inter",
    "internazionale milano": "Inter", "ac milan": "Milan",
    # France
    "paris saint germain": "Paris SG", "paris st germain": "Paris SG", "psg": "Paris SG",
    "stade rennais": "Rennes", "olympique lyonnais": "Lyon", "olympique lyon": "Lyon",
    "olympique marseille": "Marseille", "olympique de marseille": "Marseille",
    "stade brestois": "Brest", "stade brestois 29": "Brest", "stade de reims": "Reims",
    # Portugal
    "sporting cp": "Sp Lisbon", "sporting clube de portugal": "Sp Lisbon",
    "sporting lisbon": "Sp Lisbon", "sporting": "Sp Lisbon",
    "vitoria sc": "Guimaraes", "vitoria guimaraes": "Guimaraes", "vitoria de guimaraes": "Guimaraes",
    "sc braga": "Sp Braga", "sporting braga": "Sp Braga",
    "estrela amadora": "Estrela", "avs futebol sad": "AVS",
    # Netherlands / Belgium
    "psv": "PSV Eindhoven", "feyenoord rotterdam": "Feyenoord", "ajax amsterdam": "Ajax",
    "fc twente enschede": "Twente", "nec nijmegen": "Nijmegen", "pec zwolle": "Zwolle",
    "rkc waalwijk": "Waalwijk", "ado den haag": "Den Haag",
    "union saint gilloise": "St. Gilloise", "union st gilloise": "St. Gilloise",
    "royale union saint gilloise": "St. Gilloise",
    "standard liege": "Standard", "sint truiden": "St Truiden", "sint truidense": "St Truiden",
    "oh leuven": "Oud-Heverlee Leuven", "zulte waregem": "Waregem", "sv zulte waregem": "Waregem",
    "royal antwerp": "Antwerp",
    # Turkey / Greece / Scotland
    "istanbul basaksehir": "Buyuksehyr", "basaksehir": "Buyuksehyr",
    "adana demirspor": "Ad. Demirspor", "erzurum bb": "Erzurumspor",
    "fatih karagumruk": "Karagumruk", "caykur rizespor": "Rizespor",
    "olympiacos": "Olympiakos", "olympiacos piraeus": "Olympiakos", "olympiakos piraeus": "Olympiakos",
    "paok thessaloniki": "PAOK", "paok salonika": "PAOK", "aek athens": "AEK",
    "aris thessaloniki": "Aris", "aris salonika": "Aris", "levadiakos": "Levadeiakos",
    "heart of midlothian": "Hearts",
    # look-alikes from countries we hold no results for
    "red star belgrade": None, "crvena zvezda": None, "fk crvena zvezda": None,
    "aris limassol": None, "aek larnaca": None, "inter miami": None, "inter miami cf": None,
    "sporting kansas city": None, "real salt lake": None, "racing club": None,
    "nacional montevideo": None, "nacional": "Nacional",
}

NATIONAL_ALIASES = {
    "usa": "United States", "us": "United States", "united states of america": "United States",
    "czechia": "Czech Republic", "turkiye": "Turkey", "korea republic": "South Korea",
    "republic of korea": "South Korea", "korea dpr": "North Korea", "dpr korea": "North Korea",
    "ir iran": "Iran", "cote divoire": "Ivory Coast", "cote d ivoire": "Ivory Coast",
    "china": "China PR", "ireland": "Republic of Ireland", "congo dr": "DR Congo",
    "democratic republic of congo": "DR Congo", "cabo verde": "Cape Verde",
    "bosnia and herzegovina": "Bosnia and Herzegovina", "bosnia herzegovina": "Bosnia and Herzegovina",
    "bosnia": "Bosnia and Herzegovina", "uae": "United Arab Emirates",
    "macedonia": "North Macedonia", "swaziland": "Eswatini", "curacao": "Curaçao",
    "trinidad and tobago": "Trinidad and Tobago", "st kitts and nevis": "Saint Kitts and Nevis",
}


# ── competitions ────────────────────────────────────────────────────────────
def competition_kind(sport_name):
    """'tennis' | 'national' | 'club'.

    This used to be `"euro" in name or "world cup" in name`, which filed the
    EUROpa League and the Club WORLD CUP under national teams: their fixtures were
    looked up in the national ratings and came back with no Elo and no form."""
    s = norm(sport_name)
    if any(k in s.split() for k in ("tennis", "atp", "wta")):
        return "tennis"
    if any(k in s for k in ("europa", "champions", "conference", "club world cup",
                            "libertadores", "sudamericana")):
        return "club"
    if any(k in s for k in ("world cup", "nations league", "uefa euro", "copa america",
                            "gold cup", "africa cup", "african cup", "afcon", "asian cup",
                            "qualif", "friendl")):
        return "national"
    if s in ("euro", "euros") or re.fullmatch(r"(uefa )?euro( \d{4})?", s):
        return "national"
    return "club"


_DIV_COUNTRY = {
    "E0": "en", "E1": "en", "SP1": "es", "SP2": "es", "D1": "de", "D2": "de",
    "I1": "it", "I2": "it", "F1": "fr", "F2": "fr", "N1": "nl", "P1": "pt",
    "B1": "be", "T1": "tr", "G1": "gr", "SC0": "sc",
}
# display names from collectors/odds.py SPORT_GROUPS (normalised) -> country
_SPORT_COUNTRY = {
    "premier league": "en", "championship": "en", "la liga": "es", "la liga 2": "es",
    "bundesliga": "de", "bundesliga 2": "de", "serie a": "it", "serie b": "it",
    "ligue 1": "fr", "ligue 2": "fr", "eredivisie": "nl", "primeira liga": "pt",
    "jupiler pro league": "be", "super lig": "tr", "super league greece": "gr",
    "scottish premiership": "sc",
}
_CONTINENTAL = ("champions league", "europa league", "conference league",
                "europa conference league", "club world cup")


def club_scope(sport_name):
    """Which results a club fixture of this competition may be matched against:
    a country code, '*' (any country — European cups) or None (a league we hold
    no results for, e.g. Brazil or Japan: its teams must resolve to nothing)."""
    s = norm(sport_name)
    if s in _SPORT_COUNTRY:
        return _SPORT_COUNTRY[s]
    if any(k in s for k in _CONTINENTAL):
        return "*"
    return None


# ── the resolver ────────────────────────────────────────────────────────────
class Resolver:
    """Maps free-form names onto a fixed vocabulary. Never returns a guess."""

    def __init__(self, names, aliases=None):
        self.names = sorted({n for n in names if n})
        self._set = set(self.names)
        self._aliases = aliases or {}
        self._by_norm, self._by_key = {}, {}
        for n in self.names:
            self._by_norm.setdefault(norm(n), []).append(n)
            self._by_key.setdefault(" ".join(tokens(n)), []).append(n)
        self._tok = {n: tokens(n) for n in self.names}
        self._cache = {}

    def resolve(self, name, strict=False):
        """-> (canonical_name | None, how). how: exact | alias | normalized | fuzzy |
        known-other | ambiguous | none"""
        key = (name, strict)
        if key not in self._cache:
            self._cache[key] = self._resolve(name, strict)
        return self._cache[key]

    def _resolve(self, name, strict):
        if not name or not self.names:
            return None, "none"
        if name in self._set:
            return name, "exact"
        n = norm(name)
        if n in self._aliases:
            target = self._aliases[n]
            if target is None:
                return None, "known-other"
            if target in self._set:
                return target, "alias"
            # the alias points at a team this vocabulary does not hold (yet)
            return None, "none"
        hit = self._by_norm.get(n)
        if hit and len(hit) == 1:
            return hit[0], "normalized"
        q = tokens(name)
        hit = self._by_key.get(" ".join(q))
        if hit and len(hit) == 1:
            return hit[0], "normalized"
        scored = []
        for cand, ct in self._tok.items():
            s = _score(q, ct, strict)
            if s is not None:
                scored.append((s, cand))
        if not scored:
            return None, "none"
        scored.sort(reverse=True)
        best = scored[0]
        if best[0] < MIN_SCORE:
            return None, "none"
        if len(scored) > 1 and best[0] - scored[1][0] < MIN_MARGIN:
            return None, "ambiguous"
        return best[1], "fuzzy"


# ── vocabularies read from the database, cached in memory ───────────────────
_VOCAB_TTL = 3600
_vocab = {"at": 0.0, "club_by_country": None, "club_all": None, "national": None,
          "has_fd": False}


def invalidate():
    """Forget the cached vocabularies (call after a results import)."""
    _vocab["at"] = 0.0


def _load():
    if _vocab["club_all"] is not None and time.time() - _vocab["at"] < _VOCAB_TTL:
        return
    from collectors.database import get_connection
    by_country, all_names, national, has_fd = {}, set(), set(), False
    conn = get_connection()
    try:
        try:
            rows = conn.execute(
                "SELECT DISTINCT league, home_team, away_team FROM football_matches "
                "WHERE match_id LIKE 'fd_%'").fetchall()
        except Exception as e:                        # noqa: BLE001
            print(f"TEAMNAMES: club vocabulary unavailable: {e}", flush=True)
            rows = []
        for r in rows:
            has_fd = True
            c = _DIV_COUNTRY.get(r["league"])
            for nm in (r["home_team"], r["away_team"]):
                if not nm:
                    continue
                all_names.add(nm)
                if c:
                    by_country.setdefault(c, set()).add(nm)
        try:
            for r in conn.execute("SELECT DISTINCT home_team, away_team FROM national_matches").fetchall():
                for nm in (r["home_team"], r["away_team"]):
                    if nm:
                        national.add(nm)
        except Exception as e:                        # noqa: BLE001
            print(f"TEAMNAMES: national vocabulary unavailable: {e}", flush=True)
    finally:
        conn.close()
    _vocab["club_by_country"] = {c: Resolver(n, CLUB_ALIASES) for c, n in by_country.items()}
    _vocab["club_all"] = Resolver(all_names, CLUB_ALIASES)
    _vocab["national"] = Resolver(national, NATIONAL_ALIASES)
    _vocab["has_fd"] = has_fd
    _vocab["at"] = time.time()


def has_results_vocabulary():
    """True once football-data results are loaded (the single naming universe)."""
    _load()
    return _vocab["has_fd"]


def resolve_club(name, sport_name=None):
    """(results-vocabulary name | None, how) for a club in a given competition."""
    _load()
    scope = club_scope(sport_name) if sport_name is not None else "*"
    if scope is None:
        return None, "no-results-for-league"
    if scope == "*":
        return _vocab["club_all"].resolve(name, strict=True)
    r = _vocab["club_by_country"].get(scope)
    if r is None:
        return None, "none"
    return r.resolve(name)


def resolve_national(name):
    _load()
    return _vocab["national"].resolve(name)


def resolve(name, sport_name):
    """Resolve a fixture's team by the kind of competition it is in."""
    kind = competition_kind(sport_name)
    if kind == "national":
        return resolve_national(name)
    if kind == "club":
        return resolve_club(name, sport_name)
    return name, "exact"                              # tennis: players keep their names


def club_country(canonical):
    """Country code of a results-vocabulary club name, or None."""
    _load()
    for c, r in (_vocab["club_by_country"] or {}).items():
        if canonical in r._set:
            return c
    return None
