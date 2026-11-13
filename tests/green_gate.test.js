/**
 * Green-gate contract: an edge is only celebrated when it can be TRUSTED and
 * later VERIFIED.
 *
 * Two holes this guards:
 *  1. A "fair" probability devigged from an illiquid Pinnacle line (margin above
 *     4%) carries several points of slack, so a 2-3% edge against it is inside
 *     the noise. That used to go straight to green.
 *  2. If Pinnacle doesn't quote the pick's market at all, no closing line can
 *     ever be captured, so the bet's CLV stays "—" forever and the edge can
 *     never be checked against anything. That also used to go green.
 *
 * Run: node tests/green_gate.test.js
 */
const fs = require('fs');
const path = require('path');
const appSrc = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8');

function extract(name) {
  const start = appSrc.indexOf(`function ${name}(`);
  if (start === -1) throw new Error(`FATAL: function ${name}() not found in app.js`);
  let i = appSrc.indexOf('{', start), depth = 0;
  for (let j = i; j < appSrc.length; j++) {
    if (appSrc[j] === '{') depth++;
    else if (appSrc[j] === '}') { depth--; if (depth === 0) return appSrc.slice(start, j + 1); }
  }
  throw new Error(`FATAL: could not brace-match ${name}()`);
}

// Constants vbEval closes over — read from app.js itself, so the test cannot
// drift from the thresholds the page really uses.
function constOf(name) {
  const m = appSrc.match(new RegExp(`^const ${name} = ([0-9.]+);`, 'm'));
  if (!m) throw new Error(`FATAL: const ${name} not found in app.js`);
  return parseFloat(m[1]);
}
const VB_VALUE_FLOOR = constOf('VB_VALUE_FLOOR'), VB_ODD_FLOOR = constOf('VB_ODD_FLOOR');
const VB_GREEN_MAX_ODD = constOf('VB_GREEN_MAX_ODD'), VB_MIN_ODD = constOf('VB_MIN_ODD');
const VB_SHORT_BAND_FLOOR = constOf('VB_SHORT_BAND_FLOOR');
const VB_REF_FRESH_H = constOf('VB_REF_FRESH_H'), VB_REF_MAX_AGE_H = constOf('VB_REF_MAX_AGE_H');
const VB_VALUE_FLOOR_AGING = constOf('VB_VALUE_FLOOR_AGING');
const vbState = { minEdge: 2 };
function vbOddCeiling() { return 10; }
for (const fn of ['pinCoversMarket', 'vbRefAgeHours', '_myBookPick', 'vbPriceLight',
                  'vbRefBlock', 'vbStakePlan', 'vbEval', 'vbSignal', 'vbBelowGateNote']) {
  // eslint-disable-next-line no-eval
  eval(extract(fn));
}

// Helper: an updated_at string that is `h` hours old, in the stored UTC shape.
const agedUpdatedAt = h =>
  new Date(Date.now() - h * 3600000).toISOString().slice(0, 16).replace('T', ' ');

let failures = 0;
const check = (name, cond, detail) => {
  if (cond) console.log(`  ✓ ${name}`);
  else { console.log(`  ✗ ${name}\n      ${detail}`); failures++; }
};

// A textbook green candidate: solid edge, odd in the sweet spot, liquid Pinnacle
// line quoting this market, both sharps agreeing.
const base = () => ({
  home_team: 'A', away_team: 'B',
  pin_home: 1.90, pin_away: 2.00,
  pin_low_liquidity: false,
  ref_agreement: 'agree',
  updated_at: agedUpdatedAt(0.2),      // fresh reference unless a test says otherwise
  all_picks: [
    { market: 'Match Result', selection: 'A', book: '1xBet', book_odd: 1.95, edge_pct: 3.5, confidence: 4 },
  ],
});

console.log('\nGREEN GATE CONTRACT\n');

{
  const ev = vbEval(base());
  check('a clean, liquid, verifiable pick still goes green',
    ev.isValue === true, `isValue=${ev.isValue}`);
  check('and keeps its stars', ev.stars === 4, `stars=${ev.stars}`);
}

{
  const b = base(); b.pin_low_liquidity = true; b.pin_vig_pct = 6.2;
  const ev = vbEval(b);
  check('an illiquid Pinnacle reference (margin >4%) BLOCKS green',
    ev.isValue === false, `isValue=${ev.isValue}`);
  check('and is flagged so the card can say why', ev.weakRef === true, `weakRef=${ev.weakRef}`);
  check('and cannot show more than 2 stars', ev.stars <= 2, `stars=${ev.stars}`);
}

{
  const b = base(); b.pin_home = null; b.pin_away = null;   // Pinnacle absent
  const ev = vbEval(b);
  check('no Pinnacle line for this market BLOCKS green (CLV could never exist)',
    ev.isValue === false, `isValue=${ev.isValue}`);
  check('and is flagged as unverifiable', ev.unverifiable === true, `unverifiable=${ev.unverifiable}`);
}

{
  // Over/Under pick on an event that has Pinnacle 1X2 but no Pinnacle totals:
  // the 1X2 prices must NOT be mistaken for coverage of the totals market.
  const b = base();
  b.all_picks = [{ market: 'Over/Under 2.5', selection: 'Over 2.5 Goals', book: '1xBet', book_odd: 1.95, edge_pct: 3.5, confidence: 4 }];
  const ev = vbEval(b);
  check('per-market coverage: 1X2 prices do not vouch for an O/U pick',
    ev.isValue === false && ev.unverifiable === true,
    `isValue=${ev.isValue} unverifiable=${ev.unverifiable}`);

  b.pin_over25 = 1.85; b.pin_under25 = 1.95;
  check('...and it goes green once Pinnacle actually quotes the totals',
    vbEval(b).isValue === true, 'still blocked with pin_over25/under25 present');
}

{
  const b = base(); b.ref_agreement = 'diverge_sharp';
  check('two sharps disagreeing still blocks green (pre-existing rule intact)',
    vbEval(b).isValue === false, 'diverge_sharp leaked through');
}

// ── Reference freshness ─────────────────────────────────────────────────────
// Real case this guards: bets entered on a claimed +2.1% and +2.4% edge closed at
// -13.2% and -10.0%. Both were priced against a Pinnacle line many hours old, so
// the "edge" was the market having moved since the snapshot.
console.log('\n  reference freshness');
{
  const b = base(); b.updated_at = agedUpdatedAt(10);
  const ev = vbEval(b);
  check('a 10h-old reference BLOCKS green outright',
    ev.isValue === false && ev.staleRef === true,
    `isValue=${ev.isValue} staleRef=${ev.staleRef}`);
  check('...and caps the stars', ev.stars <= 2, `stars=${ev.stars}`);
  check('...and reports the age so the card can say how old',
    Math.round(ev.refAgeH) === 10, `refAgeH=${ev.refAgeH}`);
}

{
  // 2-6h: trustworthy enough to show, but a 2% edge is inside the drift.
  const b = base(); b.updated_at = agedUpdatedAt(4);
  b.all_picks[0].edge_pct = 2.5;
  const ev = vbEval(b);
  check('a 4h-old reference needs more than 2.5% edge to go green',
    ev.isValue === false && ev.agingRef === true,
    `isValue=${ev.isValue} agingRef=${ev.agingRef}`);
  check('...and the raised floor is reported',
    ev.edgeFloor === VB_VALUE_FLOOR_AGING, `edgeFloor=${ev.edgeFloor}`);

  // The floor sits at this band's measured p90 drift (6.49%), so a 5% edge is
  // NOT enough any more — drift swallows 5% in this band about a quarter of the
  // time. Only an edge above the p90 survives.
  b.all_picks[0].edge_pct = 5.0;
  check('a 5% edge is NOT enough on a 4h-old reference (drift eats it ~25% of the time)',
    vbEval(b).isValue === false, 'a 5% edge went green against a 6.5% floor');

  b.all_picks[0].edge_pct = 7.0;
  check('...but a 7% edge clears the raised floor',
    vbEval(b).isValue === true, 'still blocked at 7% on a 4h-old reference');
}

{
  const b = base(); b.updated_at = agedUpdatedAt(0.5);
  b.all_picks[0].edge_pct = 2.5;
  check('a fresh reference keeps the normal 2% floor',
    vbEval(b).isValue === true, 'a 2.5% edge on fresh odds should be green');
}

{
  // Never let a missing timestamp silently disable the rule — but do not punish
  // it either: unknown age keeps the old behaviour rather than blocking everything.
  const b = base(); delete b.updated_at;
  const ev = vbEval(b);
  check('unknown reference age does not crash and does not block',
    ev.refAgeH === null && ev.isValue === true,
    `refAgeH=${ev.refAgeH} isValue=${ev.isValue}`);
}

// ── The traffic light IS the green gate ─────────────────────────────────────
// The 🟢/🟡/🔴 chip is what the page sorts by, counts in the banner and beeps on.
// It used to apply the price rule only: a pick priced against a 10h-old Pinnacle
// line showed 🟢 APOSTAR and a Kelly stake while the Best Pick box on the same
// card said the reference was too old to trust.
console.log('\n  the traffic light honours the reference');
{
  const b = base(); b.updated_at = agedUpdatedAt(10);
  const sig = vbSignal(b);
  check('a 10h-old reference is NOT green on the traffic light',
    sig.light !== 'green', `light=${sig.light}`);
  check('...it is yellow, and says the reason is the age of the odds',
    sig.light === 'yellow' && sig.blocked === 'stale', `light=${sig.light} blocked=${sig.blocked}`);
  check('...and no stake is suggested for it',
    vbStakePlan(b, vbEval(b), vbEval(b).bestPick).state === 'blocked',
    `state=${vbStakePlan(b, vbEval(b), vbEval(b).bestPick).state}`);
}
{
  const b = base(); b.updated_at = agedUpdatedAt(4); b.all_picks[0].edge_pct = 2.5;
  const sig = vbSignal(b);
  check('a 4h-old reference with a 2.5% edge is yellow ("atualiza as odds")',
    sig.light === 'yellow' && sig.blocked === 'aging', `light=${sig.light} blocked=${sig.blocked}`);
  b.all_picks[0].edge_pct = 7.0;
  check('...and green again once the edge clears the raised floor',
    vbSignal(b).light === 'green' && vbSignal(b).blocked === null,
    `light=${vbSignal(b).light} blocked=${vbSignal(b).blocked}`);
}
{
  const b = base(); b.pin_low_liquidity = true;
  check('an illiquid reference is yellow on the light, reason "weak"',
    vbSignal(b).light === 'yellow' && vbSignal(b).blocked === 'weak',
    JSON.stringify(vbSignal(b)));
}
{
  const b = base(); b.pin_home = null; b.pin_away = null;
  check('no Pinnacle line is yellow on the light, reason "unverifiable"',
    vbSignal(b).light === 'yellow' && vbSignal(b).blocked === 'unverifiable',
    JSON.stringify(vbSignal(b)));
}
{
  const b = base(); b.ref_agreement = 'diverge_sharp';
  check('two sharps disagreeing is yellow on the light, reason "conflict"',
    vbSignal(b).light === 'yellow' && vbSignal(b).blocked === 'conflict',
    JSON.stringify(vbSignal(b)));
}
{
  // Whatever the inputs, "celebrated" and "green light" must be the same thing.
  let mismatch = null;
  for (const age of [0.2, 2, 4, 10]) for (const edge of [0.5, 1.9, 2.0, 2.9, 3.0, 6.4, 6.5, 9, 16])
    for (const odd of [1.2, 1.3, 1.39, 1.4, 2.5, 4.0, 4.01]) for (const weak of [false, true]) {
      const b = base(); b.updated_at = agedUpdatedAt(age); b.pin_low_liquidity = weak;
      b.all_picks[0].edge_pct = edge; b.all_picks[0].book_odd = odd;
      const ev = vbEval(b), sig = vbSignal(b);
      if (ev.isValue !== (sig.light === 'green')) mismatch = { age, edge, odd, weak, isValue: ev.isValue, light: sig.light };
    }
  check('isValue (celebrated card) and the green light never disagree', mismatch === null,
    JSON.stringify(mismatch));
}
{
  const b = base();
  check('a clean pick is green with no block reason and gets a stake',
    vbSignal(b).light === 'green' && vbSignal(b).blocked === null
      && vbStakePlan(b, vbEval(b), vbEval(b).bestPick).state === 'ok', JSON.stringify(vbSignal(b)));
}

// ── The price rule, at the price the user can bet ───────────────────────────
console.log('\n  the price rule');
{
  const at = (edge, odd) => { const b = base(); b.all_picks[0].edge_pct = edge; b.all_picks[0].book_odd = odd; return vbSignal(b).light; };
  check('edge below 2% is red', at(1.9, 2.0) === 'red', at(1.9, 2.0));
  check('edge 2% at odd 1.4-4.0 is green', at(2.0, 1.4) === 'green' && at(2.0, 4.0) === 'green', `${at(2.0, 1.4)}/${at(2.0, 4.0)}`);
  check('odd above 4.0 is red whatever the edge', at(8, 4.01) === 'red', at(8, 4.01));
  check('odd 1.3-1.4 needs 3%: 2.5% is yellow, 3% is green',
    at(2.5, 1.35) === 'yellow' && at(3.0, 1.35) === 'green', `${at(2.5, 1.35)}/${at(3.0, 1.35)}`);
  check('odd below 1.3 is red', at(5, 1.29) === 'red', at(5, 1.29));
  check('an edge above 15% is treated as noise, not as a green', at(16, 2.0) !== 'green', at(16, 2.0));
  check('...by the price rule itself, not only by the pick filter', vbPriceLight(16, 2.0, 2) === 'red', vbPriceLight(16, 2.0, 2));
}
{
  // The user's "Min edge" filter raises the green floor; it never lowers it.
  const b = base(); b.all_picks[0].edge_pct = 2.5;
  vbState.minEdge = 3;
  const hi = vbSignal(b).light;
  vbState.minEdge = 0;
  const any = vbSignal(b).light;
  b.all_picks[0].edge_pct = 1.5;
  const anyLow = vbSignal(b).light;
  vbState.minEdge = 2;
  check('Min edge 3% → a 2.5% edge is not green', hi !== 'green', `light=${hi}`);
  // ...including when ANOTHER house pays above the filter on the same selection:
  // the card is then shown (value elsewhere), but 1xBet's 2.5% is still under the
  // user's own 3% bar and must not turn it green.
  const c = base();
  c.all_picks = [
    { market: 'Match Result', selection: 'A', book: 'Best',  book_odd: 2.00, edge_pct: 3.5, confidence: 4 },
    { market: 'Match Result', selection: 'A', book: '1xBet', book_odd: 1.98, edge_pct: 2.5, confidence: 3 },
  ];
  vbState.minEdge = 3;
  const elsewhere = vbSignal(c).light;
  vbState.minEdge = 2;
  check('Min edge 3% holds even when another house clears it (1xBet 2.5% stays not-green)',
    elsewhere !== 'green' && vbSignal(c).light === 'green', `at 3%: ${elsewhere}, at 2%: ${vbSignal(c).light}`);
  check('Min edge "Any" → 2.5% is green, but 1.5% still is not (the 2% floor holds)',
    any === 'green' && anyLow === 'red', `2.5%→${any} 1.5%→${anyLow}`);
}

// ── Which selection is featured is decided at the user's book ───────────────
// Real shape of the bug: another house pays more on Home (+4% there, +0.5% at
// 1xBet) while 1xBet itself has value on Away (+2.6%). Picking the selection on
// the best price anywhere featured Home, mapped it to 1xBet's +0.5% and showed
// 🔴 — with a genuine value bet at the user's own book hidden behind it.
console.log('\n  the featured selection is chosen at the user\'s book');
{
  const b = base();
  b.all_picks = [
    { market: 'Match Result', selection: 'A', book: 'Best',  book_odd: 2.05, edge_pct: 4.0, confidence: 4 },
    { market: 'Match Result', selection: 'A', book: '1xBet', book_odd: 1.98, edge_pct: 0.5, confidence: 2 },
    { market: 'Match Result', selection: 'B', book: 'Best',  book_odd: 2.10, edge_pct: 2.6, confidence: 3 },
    { market: 'Match Result', selection: 'B', book: '1xBet', book_odd: 2.10, edge_pct: 2.6, confidence: 3 },
  ];
  const ev = vbEval(b), sig = vbSignal(b);
  check('the selection with value AT 1xBET is the one featured',
    ev.bestPick && ev.bestPick.selection === 'B', `featured=${ev.bestPick && ev.bestPick.selection}`);
  check('...and the card is green on its 1xBet edge',
    sig.light === 'green' && sig.clv === 2.6 && sig.odd === 2.10, JSON.stringify(sig));
}
{
  const b = base();
  b.all_picks = [
    { market: 'Match Result', selection: 'A', book: 'Best',  book_odd: 2.05, edge_pct: 4.0, confidence: 4 },
    { market: 'Match Result', selection: 'A', book: '1xBet', book_odd: 1.98, edge_pct: 0.5, confidence: 2 },
  ];
  const ev = vbEval(b), sig = vbSignal(b);
  check('value that exists only at another house is still shown (informational)...',
    ev.bestPick && ev.bestPick.selection === 'A', `featured=${ev.bestPick && ev.bestPick.selection}`);
  check('...but is never green and never celebrated',
    sig.light === 'red' && ev.isValue === false, `light=${sig.light} isValue=${ev.isValue}`);
}
{
  const b = base();
  b.all_picks = [{ market: 'Match Result', selection: 'A', book: 'Best', book_odd: 2.05, edge_pct: 4.0, confidence: 4 }];
  const ev = vbEval(b);
  check('a selection 1xBet does not quote is red and gets no stake',
    vbSignal(b).light === 'red' && vbStakePlan(b, ev, ev.bestPick).state === 'na',
    `light=${vbSignal(b).light} state=${vbStakePlan(b, ev, ev.bestPick).state}`);
}
{
  // Two 1xBet selections with edge: the bettable one (odd in range) leads, even
  // when the other has the bigger number.
  const b = base();
  b.all_picks = [
    { market: 'Match Result', selection: 'A', book: '1xBet', book_odd: 4.60, edge_pct: 6.0, confidence: 4 },
    { market: 'Match Result', selection: 'B', book: '1xBet', book_odd: 1.90, edge_pct: 2.4, confidence: 3 },
  ];
  const ev = vbEval(b);
  check('among 1xBet values, a bettable pick outranks a bigger edge outside the odd range',
    ev.bestPick.selection === 'B' && ev.light === 'green', `featured=${ev.bestPick.selection} light=${ev.light}`);
}

// ── A card that is not green says exactly why ───────────────────────────────
// It used to print one generic English line ("below the green value gate … Size
// with care") under a light that said IGNORAR, whatever the real reason was.
console.log('\n  a non-green card names its reason');
{
  const best = { book_odd: 2.46, edge_pct: 4.0 }, mine = { book_odd: 2.38, edge_pct: 0.4 };
  const t = vbBelowGateNote(best, mine);
  check('value at another house: names the other price AND the 1xBet edge',
    /noutra casa/.test(t) && /2\.46/.test(t) && /\+4\.0%/.test(t) && /2\.38/.test(t) && /\+0\.4%/.test(t), t);
  const s2 = vbBelowGateNote({ book_odd: 1.36, edge_pct: 2.5 }, { book_odd: 1.36, edge_pct: 2.5 });
  check('odd 1.30-1.40: says the 3% bar of that band', /1\.30–1\.40/.test(s2) && /3%/.test(s2) && /\+2\.5%/.test(s2), s2);
  const s3 = vbBelowGateNote({ book_odd: 1.22, edge_pct: 4 }, { book_odd: 1.22, edge_pct: 4 });
  check('odd under 1.30: says it is too short', /abaixo de 1\.30/.test(s3), s3);
  const s4 = vbBelowGateNote({ book_odd: 2.0, edge_pct: 1.1 }, { book_odd: 2.0, edge_pct: 1.1 });
  check('small edge: says the edge and the minimum', /\+1\.1%/.test(s4) && /mínimo de 2%/.test(s4) && !/noutra casa/.test(s4), s4);
  check('never tells the user to "size with care" on a pick the light says to ignore',
    ![t, s2, s3, s4].some(x => /size with care/i.test(x)), 'found "Size with care"');
}

// ── Defaults ────────────────────────────────────────────────────────────────
console.log('\n  defaults');
{
  const m = appSrc.match(/^\s*minEdge:\s*([0-9.]+),/m);
  check('the default Min edge equals the green floor (a fresh browser must not hide 2-3% greens)',
    m && parseFloat(m[1]) === VB_VALUE_FLOOR, `default minEdge=${m && m[1]} floor=${VB_VALUE_FLOOR}`);
  const html = fs.readFileSync(path.join(__dirname, '..', 'templates', 'index.html'), 'utf8');
  const sel = html.match(/<select id="vb-minedge"[\s\S]*?<\/select>/);
  const chosen = sel && sel[0].match(/<option value="([0-9.]+)" selected/);
  check('...and so does the option preselected in the page',
    chosen && parseFloat(chosen[1]) === VB_VALUE_FLOOR, `selected=${chosen && chosen[1]}`);
}

console.log(`\n${failures === 0 ? '✅ ALL CHECKS PASSED' : `❌ ${failures} CHECK(S) FAILED`}\n`);
process.exit(failures === 0 ? 0 : 1);
