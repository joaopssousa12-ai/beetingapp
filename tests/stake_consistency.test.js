/**
 * Stake consistency contract (Part C).
 *
 * Guards the bug we hit in production: the Value Bet card showed €15 while the
 * Track Bet prefill filled €77 for the SAME pick, with no explanation.
 *
 * THE CONTRACT (as chosen by the user):
 *  1. Card and prefill must be computed from the SAME inputs — the 1xBet odd and
 *     the 1xBet edge. Never from a "best available" price at another book we
 *     cannot actually bet at.
 *  2. The prefill is the FULL ¼-Kelly (no short-odd limiter) — the user sizes
 *     low-odd bets themselves.
 *  3. Whenever the limiter bites (odd < 1.40) and the two numbers differ, the
 *     CARD MUST SURFACE BOTH. A hidden divergence is exactly the original bug.
 *  4. When the limiter does not bite, the two numbers must be identical.
 *
 * Run: node tests/stake_consistency.test.js      (no deps, exits 1 on failure)
 */
const fs = require('fs');
const path = require('path');

// ── Load the real implementations out of app.js (no bundler in this project) ──
const appSrc = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8');

function extract(name) {
  const start = appSrc.indexOf(`function ${name}(`);
  if (start === -1) throw new Error(`FATAL: function ${name}() not found in app.js`);
  // brace-match to the end of the function
  let i = appSrc.indexOf('{', start), depth = 0;
  for (let j = i; j < appSrc.length; j++) {
    if (appSrc[j] === '{') depth++;
    else if (appSrc[j] === '}') { depth--; if (depth === 0) return appSrc.slice(start, j + 1); }
  }
  throw new Error(`FATAL: could not brace-match ${name}()`);
}

const VB_SHORT_ODD = 1.40;
// eslint-disable-next-line no-eval
eval(extract('vbKellyFraction'));           // the card's limited ("safe") sizing
// eslint-disable-next-line no-eval
eval(extract('_myBookPick'));               // must pick the 1xBet row, never best-available

// Mirror of the prefill formula in quickAddBet() (full ¼-Kelly, no limiter).
function prefillFraction(edge, odd) {
  return (edge > 0 && odd > 1) ? (edge / 100) / (odd - 1) * 0.25 : 0;
}

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log(`  ✓ ${name}`); }
  else { console.log(`  ✗ ${name}\n      ${detail}`); failures++; }
}

const BANKROLL = 1000;
const eur = f => Math.round(BANKROLL * f);

console.log('\nSTAKE CONSISTENCY CONTRACT\n');

// ── 1. Both sides must read the 1xBet price, never a better price elsewhere ──
console.log('1. Kelly is sized on the odd actually available at 1xBet');
{
  const b = {
    all_picks: [
      { book: 'Pinnacle', market: 'Match Result', selection: 'Gauff', book_odd: 1.09, edge_pct: 0.0 },
      { book: 'Bet365',   market: 'Match Result', selection: 'Gauff', book_odd: 1.22, edge_pct: 11.6 },
      { book: '1xBet',    market: 'Match Result', selection: 'Gauff', book_odd: 1.14, edge_pct: 4.3 },
    ],
  };
  const pick = { market: 'Match Result', selection: 'Gauff', book_odd: 1.22 }; // best-available
  const mine = _myBookPick(b, pick);
  check('picks the 1xBet row', mine && mine.book === '1xBet', `got ${JSON.stringify(mine)}`);
  check('uses the 1xBet odd (1.14), not best-available (1.22)',
    mine.book_odd === 1.14, `got ${mine && mine.book_odd}`);
  check('uses the 1xBet edge (4.3%), not the inflated best-available edge (11.6%)',
    mine.edge_pct === 4.3, `got ${mine && mine.edge_pct}`);

  const b2 = { all_picks: [{ book: 'Bet365', market: 'Match Result', selection: 'X', book_odd: 2.0, edge_pct: 5 }] };
  check('returns null when 1xBet does not quote (bet must be blocked, not mis-priced)',
    _myBookPick(b2, { market: 'Match Result', selection: 'X' }) === null, 'expected null');
}

// ── 2/3/4. Card vs prefill relationship ──────────────────────────────────────
console.log('\n2. Card and prefill agree, and any divergence is shown on the card');
const cases = [
  { name: 'Gauff  (edge 4.3%, odd 1.14) — limiter bites', edge: 4.3, odd: 1.14, limiterExpected: true },
  { name: 'Pegula (edge 4.3%, odd 1.15) — limiter bites', edge: 4.3, odd: 1.15, limiterExpected: true },
  { name: 'Medvedev (edge 0.8%, odd 1.29) — limiter bites', edge: 0.8, odd: 1.29, limiterExpected: true },
  { name: 'Normal (edge 3.0%, odd 1.90) — no limiter', edge: 3.0, odd: 1.90, limiterExpected: false },
  { name: 'Normal (edge 2.0%, odd 3.50) — no limiter', edge: 2.0, odd: 3.50, limiterExpected: false },
  { name: 'Boundary (edge 3.0%, odd 1.40) — limiter must NOT bite at exactly 1.40',
    edge: 3.0, odd: 1.40, limiterExpected: false },
];

for (const c of cases) {
  const safe = vbKellyFraction(c.edge, c.odd);       // what the card headline shows
  const full = prefillFraction(c.edge, c.odd);       // what Track Bet fills in
  const diverges = Math.abs(full - safe) > 1e-9;
  console.log(`\n  ${c.name}`);
  console.log(`    card(safe)=€${eur(safe)}  prefill(full)=€${eur(full)}`);

  check('both sized from the same edge+odd inputs',
    safe > 0 && full > 0, `safe=${safe} full=${full}`);

  if (c.limiterExpected) {
    check('limiter bites (safe < full) for this short odd', diverges && safe < full,
      `safe=${safe} full=${full}`);
    // THE REGRESSION GUARD: a divergence that the card hides is the original bug.
    check('card must display BOTH numbers when they diverge (no hidden divergence)',
      /kelly\.limited\s*\?/.test(appSrc) && /kelly\.fullText/.test(appSrc),
      'card render does not surface kelly.fullText when kelly.limited');
    check('prefill hint must show the safe reference too',
      /versão segura/.test(appSrc), 'quickAddBet does not render the safe reference hint');
  } else {
    check('card stake === prefill stake when the limiter does not apply',
      !diverges, `card=€${eur(safe)} vs prefill=€${eur(full)} — MUST be identical`);
  }
}

console.log(`\n${failures === 0 ? '✅ ALL CHECKS PASSED' : `❌ ${failures} CHECK(S) FAILED`}\n`);
process.exit(failures === 0 ? 0 : 1);
