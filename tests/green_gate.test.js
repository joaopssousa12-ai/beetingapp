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
const vbState = { minEdge: 2 };
function vbOddCeiling() { return 10; }
// eslint-disable-next-line no-eval
eval(extract('pinCoversMarket'));
// eslint-disable-next-line no-eval
eval(extract('vbEval'));

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

console.log(`\n${failures === 0 ? '✅ ALL CHECKS PASSED' : `❌ ${failures} CHECK(S) FAILED`}\n`);
process.exit(failures === 0 ? 0 : 1);
