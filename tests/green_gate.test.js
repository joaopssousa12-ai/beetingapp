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

// Constants vbEval closes over, mirrored from app.js.
const VB_VALUE_FLOOR = 2, VB_ODD_FLOOR = 1.4, VB_GREEN_MAX_ODD = 4.0;
const VB_REF_FRESH_H = 2, VB_REF_MAX_AGE_H = 6, VB_VALUE_FLOOR_AGING = 4;
const vbState = { minEdge: 2 };
function vbOddCeiling() { return 10; }
// eslint-disable-next-line no-eval
eval(extract('pinCoversMarket'));
// eslint-disable-next-line no-eval
eval(extract('vbRefAgeHours'));
// eslint-disable-next-line no-eval
eval(extract('vbEval'));

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

  b.all_picks[0].edge_pct = 5.0;
  check('...but a 5% edge clears the raised floor',
    vbEval(b).isValue === true, 'still blocked at 5% on a 4h-old reference');
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

console.log(`\n${failures === 0 ? '✅ ALL CHECKS PASSED' : `❌ ${failures} CHECK(S) FAILED`}\n`);
process.exit(failures === 0 ? 0 : 1);
