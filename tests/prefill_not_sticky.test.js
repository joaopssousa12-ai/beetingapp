/**
 * Prefill must never be "sticky" (regression guard).
 *
 * THE BUG: quickAddBet() filled the stake only `if (!stakeField.value)`. The bet
 * form is never cleared between Track Bets (only after a submit), so the FIRST
 * value that ever landed in the field stuck forever — every later Track Bet
 * re-opened the form still showing the previous bet's number (e.g. 2.80),
 * whatever the new pick was.
 *
 * This test asserts the prefill statements in app.js are unconditional, and
 * simulates the sequence to prove a second, different pick overwrites the first.
 *
 * Run: node tests/prefill_not_sticky.test.js
 */
const fs = require('fs');
const path = require('path');
const appSrcRaw = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8');
// Strip comments before pattern-matching: the fix's own comment quotes the old
// buggy expression, and matching that would be a false failure.
const appSrc = appSrcRaw
  .replace(/\/\*[\s\S]*?\*\//g, '')      // block comments
  .split('\n').map(l => l.replace(/(^|[^:])\/\/.*$/, '$1')).join('\n');

let failures = 0;
const check = (name, cond, detail) => {
  if (cond) console.log(`  ✓ ${name}`);
  else { console.log(`  ✗ ${name}\n      ${detail}`); failures++; }
};

console.log('\nPREFILL MUST NOT BE STICKY\n');

// ── Static guards: no "only fill when empty" conditions on prefilled fields ──
check('stake prefill is NOT guarded by `!stakeField.value`',
  !/!\s*stakeField\.value/.test(appSrc),
  'found `!stakeField.value` — the field would keep the previous bet\'s stake forever');

check('odds prefill assigns unconditionally',
  /getElementById\('bet-odds'\)\.value\s*=/.test(appSrc),
  'bet-odds is not assigned in the prefill');

check('a shared clear helper exists for aborted Track Bets',
  /function clearBetFormFields\s*\(/.test(appSrc),
  'clearBetFormFields() missing — aborting leaves the previous bet in the form');

check('the 1xBet-not-quoted abort path clears the form',
  /clearBetFormFields\(\);\s*\n\s*alert\('A 1xBet não cota/.test(appSrc),
  'abort path does not clear the form before alerting');

// ── Behavioural simulation of two consecutive Track Bets ────────────────────
console.log('\n  simulating two consecutive Track Bets on the same form');
const field = { value: '' };
const BANKROLL = 1000;
const prefillStake = (edge, odd) => {
  const kellyQ = (edge > 0 && odd > 1) ? (edge / 100) / (odd - 1) * 0.25 : 0;
  // current (fixed) behaviour — mirrors app.js
  field.value = (BANKROLL > 0 && kellyQ > 0) ? (BANKROLL * kellyQ).toFixed(2) : '';
};

prefillStake(0.8, 1.29);            // first bet → small stake (this was the 2.80-style value)
const first = field.value;
prefillStake(4.3, 1.14);            // second, very different pick
const second = field.value;
console.log(`    1st pick → €${first}    2nd pick → €${second}`);
check('second pick overwrites the first (no stale value)',
  first !== second && Number(second) > 0,
  `first=${first} second=${second} — the stake did not update`);

// and a pick with no usable edge must blank the field, not keep the old number
prefillStake(0, 1.14);
check('a pick with no edge blanks the field instead of keeping the old stake',
  field.value === '', `expected '' got '${field.value}'`);

console.log(`\n${failures === 0 ? '✅ ALL CHECKS PASSED' : `❌ ${failures} CHECK(S) FAILED`}\n`);
process.exit(failures === 0 ? 0 : 1);
