/**
 * The bet form must record exactly one bet, with the right numbers, or say that
 * it did not.
 *
 * Every bet logged here is a data point in the CLV record that decides whether
 * the model is trusted with real money. Four ways the form could corrupt that
 * record, all found in the 12 Nov audit:
 *
 *  1. Double click. The free server takes many seconds to wake up; every click
 *     in the meantime POSTed another copy of the same bet.
 *  2. Silent failure. A 500 (or no answer at all) left the form exactly as it
 *     was, with no message — the user believed the bet was logged.
 *  3. Stale edge. Track Bet prefilled odd + edge; if the odd was then corrected
 *     to the real 1xBet price, the ORIGINAL edge was saved next to the new odd.
 *     And picking another match from the dropdown kept the previous pick's
 *     hidden edge and attached it to the new bet.
 *  4. Stake without a basis. The prefill suggested a stake where the card itself
 *     said "no stake" (reference too old, odd above 4.0), and for a selection
 *     with no edge at 1xBet it fell back to a stake sized on ANOTHER house's price.
 *
 * And one for the Refresh button: the server now answers "are you sure?" before
 * spending credits it is holding for closing lines — the page has to ask.
 *
 * Run: node tests/bet_form.test.js        (no deps, exits 1 on failure)
 */
const fs = require('fs');
const path = require('path');
const appSrc = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8');

function extract(name) {
  let start = appSrc.indexOf(`function ${name}(`);
  if (start === -1) throw new Error(`FATAL: function ${name}() not found in app.js`);
  if (appSrc.slice(start - 6, start) === 'async ') start -= 6;
  let i = appSrc.indexOf('{', appSrc.indexOf(')', start)), depth = 0;
  for (let j = i; j < appSrc.length; j++) {
    if (appSrc[j] === '{') depth++;
    else if (appSrc[j] === '}') { depth--; if (depth === 0) return appSrc.slice(start, j + 1); }
  }
  throw new Error(`FATAL: could not brace-match ${name}()`);
}
function extractConstObject(name) {
  const start = appSrc.indexOf(`const ${name} = {`);
  if (start === -1) throw new Error(`FATAL: const ${name} not found in app.js`);
  let depth = 0;
  for (let j = appSrc.indexOf('{', start); j < appSrc.length; j++) {
    if (appSrc[j] === '{') depth++;
    else if (appSrc[j] === '}') { depth--; if (depth === 0) return appSrc.slice(appSrc.indexOf('{', start), j + 1); }
  }
  throw new Error(`FATAL: could not brace-match const ${name}`);
}
function constOf(name) {
  const m = appSrc.match(new RegExp(`^const ${name} = ([0-9.]+);`, 'm'));
  if (!m) throw new Error(`FATAL: const ${name} not found in app.js`);
  return parseFloat(m[1]);
}

// If the script dies or simply stops (an await that never settles ends a Node
// process quietly, with exit code 0) that must count as a FAILURE, not a pass.
process.exitCode = 1;
let failures = 0;
const check = (name, cond, detail) => {
  if (cond) console.log(`  ✓ ${name}`);
  else { console.log(`  ✗ ${name}\n      ${detail}`); failures++; }
};

// ── A page just big enough for the form ─────────────────────────────────────
const els = {};
const el = id => (els[id] = els[id] || {
  id, value: '', textContent: '', innerHTML: '', disabled: false, dataset: {},
  style: { display: 'block' }, options: [], selectedIndex: 0, scrollTop: 0, scrollHeight: 0,
  click() { this.clicked = (this.clicked || 0) + 1; },
});
const document = {
  getElementById: id => el(id),
  querySelector: q => el('q:' + q),
  querySelectorAll: () => [],
};
let alerts = [], confirms = [], confirmAnswer = true;
const alert = m => { alerts.push(String(m)); };
const confirm = m => { confirms.push(String(m)); return confirmAnswer; };
let fetchCalls = [], fetchImpl = null;
const fetch = (url, opts) => { fetchCalls.push({ url, opts }); return fetchImpl(url, opts); };
const jsonResp = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const setTimeout = fn => { fn(); return 0; };          // prefill runs inside a setTimeout
const setInterval = () => 1, clearInterval = () => {};
let trackedBetsMap = {};
let myBetsLoads = 0, rendered = 0, formToggles = 0;
const loadMyBets = () => { myBetsLoads++; };
const renderValueBets = () => { rendered++; };
const loadEdge = () => {}, loadStats = () => {}, fetchJSON = async () => null;
let edgeData = [];
const fmtOdd = v => (v == null ? '—' : v.toFixed(2));

// ── The real code ───────────────────────────────────────────────────────────
const VB_VALUE_FLOOR = constOf('VB_VALUE_FLOOR'), VB_ODD_FLOOR = constOf('VB_ODD_FLOOR');
const VB_GREEN_MAX_ODD = constOf('VB_GREEN_MAX_ODD'), VB_MIN_ODD = constOf('VB_MIN_ODD');
const VB_SHORT_BAND_FLOOR = constOf('VB_SHORT_BAND_FLOOR'), VB_SHORT_ODD = constOf('VB_SHORT_ODD');
const VB_REF_FRESH_H = constOf('VB_REF_FRESH_H'), VB_REF_MAX_AGE_H = constOf('VB_REF_MAX_AGE_H');
const VB_VALUE_FLOOR_AGING = constOf('VB_VALUE_FLOOR_AGING');
const vbState = { minEdge: 2, bankroll: 1000 };
function vbOddCeiling() { return 10; }
let _betSubmitting = false;
let oddsPoller = null;
// eslint-disable-next-line no-eval
const VB_BLOCK_META = eval('(' + extractConstObject('VB_BLOCK_META') + ')');
for (const fn of ['pinCoversMarket', 'vbRefAgeHours', '_myBookPick', 'vbPriceLight', 'vbRefBlock',
                  'vbStakePlan', 'vbEval', 'vbKellyFraction', 'clearBetFormFields',
                  'edgeAtSavedOdd', 'betSaveErrorText', 'submitBet', 'refreshOdds', 'quickAddBet']) {
  // eslint-disable-next-line no-eval
  eval(extract(fn));
}
function toggleBetForm() { formToggles++; }

const tick = () => new Promise(r => setImmediate(r));
const agedUpdatedAt = h =>
  new Date(Date.now() - h * 3600000).toISOString().slice(0, 16).replace('T', ' ');

function fillForm(over = {}) {
  for (const k of Object.keys(els)) delete els[k];
  const v = Object.assign({
    'bet-event-select': '', 'bet-sport': 'La Liga', 'bet-home': 'A', 'bet-away': 'B',
    'bet-commence': '2026-11-20T20:00', 'bet-market': 'h2h', 'bet-selection': 'A',
    'bet-bookmaker': '1xBet', 'bet-odds': '2.00', 'bet-stake': '10', 'bet-notes': '',
    'bet-edge-pct': '3',
  }, over);
  for (const [id, val] of Object.entries(v)) el(id).value = val;
  el('bet-form').dataset = { eventId: 'ev1', pinImplied: '51.5', oddsSource: 'pinnacle',
                             refAgreement: 'single', prefillOdd: '2', prefillEdge: '3' };
  el('bet-save-btn').textContent = 'Save bet';
  alerts = []; confirms = []; fetchCalls = []; trackedBetsMap = {}; myBetsLoads = 0;
  _betSubmitting = false;
}
const sentBody = i => JSON.parse(fetchCalls[i].opts.body);

(async () => {
  console.log('\nTHE BET FORM RECORDS ONE BET, CORRECTLY, OR SAYS IT DID NOT\n');

  // ── 1. double click ────────────────────────────────────────────────────────
  console.log('1. A second click while the first is still in flight');
  {
    fillForm();
    const pending = [];
    const release = () => pending.forEach(r => r(jsonResp(200, { ok: true, id: 7 })));
    fetchImpl = () => new Promise(r => { pending.push(r); });
    const first = submitBet();
    await tick();
    check('the Save button is disabled while saving', el('bet-save-btn').disabled === true,
      `disabled=${el('bet-save-btn').disabled}`);
    const second = submitBet();            // the impatient second click
    await tick();
    check('only ONE request is sent', fetchCalls.length === 1, `${fetchCalls.length} POSTs to /api/bets`);
    release(); await first; await second;
    check('the button comes back afterwards',
      el('bet-save-btn').disabled === false && el('bet-save-btn').textContent === 'Save bet',
      `disabled=${el('bet-save-btn').disabled} text=${el('bet-save-btn').textContent}`);
    check('and a NEW bet can be saved later (the lock is released)', _betSubmitting === false,
      `_betSubmitting=${_betSubmitting}`);
  }

  // ── 2. failures are never silent ───────────────────────────────────────────
  console.log('\n2. A bet that was not saved says so');
  {
    fillForm();
    fetchImpl = async () => jsonResp(503, { ok: false, error: 'bet_not_saved', detail: 'OperationalError: db asleep' });
    await submitBet();
    check('a 503 tells the user the bet was NOT saved, with the reason',
      alerts.length === 1 && /NÃO foi guardada/.test(alerts[0]) && /db asleep/.test(alerts[0]),
      JSON.stringify(alerts));
    check('...keeps the form filled so it can be retried',
      el('bet-odds').value === '2.00' && el('bet-form').dataset.eventId === 'ev1',
      `odds='${el('bet-odds').value}' eventId='${el('bet-form').dataset.eventId}'`);
    check('...does not mark the card as tracked', Object.keys(trackedBetsMap).length === 0,
      JSON.stringify(trackedBetsMap));
    check('...and releases the lock', _betSubmitting === false && el('bet-save-btn').disabled === false,
      `_betSubmitting=${_betSubmitting}`);
  }
  {
    fillForm();
    fetchImpl = async () => { throw new TypeError('Failed to fetch'); };
    await submitBet();
    check('no answer from the server (asleep / offline) is reported, not swallowed',
      alerts.length === 1 && /NÃO foi guardada/.test(alerts[0]), JSON.stringify(alerts));
    check('...and releases the lock', _betSubmitting === false, `_betSubmitting=${_betSubmitting}`);
  }
  {
    fillForm();
    fetchImpl = async () => ({ ok: false, status: 502, json: async () => { throw new SyntaxError('<html>'); } });
    await submitBet();
    check('an HTML error page instead of JSON is reported too',
      alerts.length === 1 && /NÃO foi guardada/.test(alerts[0]) && /502/.test(alerts[0]),
      JSON.stringify(alerts));
  }
  {
    fillForm({ 'bet-odds': '1' });
    fetchImpl = async () => jsonResp(200, { ok: true, id: 1 });
    await submitBet();
    check('an odd of 1.00 is rejected before anything is sent',
      fetchCalls.length === 0 && alerts.length === 1, `POSTs=${fetchCalls.length} alerts=${alerts.length}`);
  }

  // ── 3. success, and the server's "already logged" answer ───────────────────
  console.log('\n3. Success and duplicates');
  {
    fillForm();
    fetchImpl = async () => jsonResp(200, { ok: true, id: 9 });
    await submitBet();
    check('a saved bet clears the form and marks the card as tracked',
      alerts.length === 0 && el('bet-odds').value === '' && el('bet-form').dataset.prefillOdd === ''
        && (trackedBetsMap.ev1 || []).length === 1 && myBetsLoads === 1,
      `alerts=${alerts.length} odds='${el('bet-odds').value}' tracked=${JSON.stringify(trackedBetsMap)}`);
  }
  {
    fillForm();
    trackedBetsMap = { ev1: [{ event_id: 'ev1', selection: 'A', market: 'h2h' }] };
    fetchImpl = async () => jsonResp(200, { ok: true, id: 9, duplicate: true });
    await submitBet();
    check('a duplicate is acknowledged without counting the bet twice',
      trackedBetsMap.ev1.length === 1 && alerts.length === 1 && /já tinha sido registada/.test(alerts[0]),
      `tracked=${trackedBetsMap.ev1.length} alerts=${JSON.stringify(alerts)}`);
  }

  // ── 4. the edge follows the odd that is actually saved ─────────────────────
  console.log('\n4. The stored edge describes the stored odd');
  {
    check('same odd → same edge', edgeAtSavedOdd('2.00', '3', '2.00') === 3, String(edgeAtSavedOdd('2.00', '3', '2.00')));
    const lower = edgeAtSavedOdd('2.00', '3', '1.95');       // 1.03 x 1.95/2.00 - 1 = +0.425%
    check('a lower odd shrinks the edge by the same ratio', Math.abs(lower - 0.425) < 0.011, `got ${lower}`);
    const gone = edgeAtSavedOdd('2.00', '3', '1.90');        // 1.03 x 0.95 - 1 = -2.15%
    check('...and can turn it negative', Math.abs(gone - (-2.15)) < 0.011, `got ${gone}`);
    check('no prefill → nothing to derive', edgeAtSavedOdd('', '', '1.95') === null, String(edgeAtSavedOdd('', '', '1.95')));

    fillForm({ 'bet-odds': '1.95' });                         // prefilled at 2.00 / +3%
    fetchImpl = async () => jsonResp(200, { ok: true, id: 3 });
    await submitBet();
    check('an edited odd is saved with ITS edge, not the prefilled one',
      Math.abs(sentBody(0).edge_pct - 0.425) < 0.011 && sentBody(0).odds === 1.95,
      `sent odds=${sentBody(0).odds} edge_pct=${sentBody(0).edge_pct}`);

    fillForm();
    await submitBet();
    check('an untouched odd keeps the prefilled edge', sentBody(0).edge_pct === 3, `edge_pct=${sentBody(0).edge_pct}`);
  }
  {
    const fn = extract('populateEventDropdown');
    check('choosing another match by hand drops the previous pick\'s hidden edge',
      /sel\.onchange[\s\S]*getElementById\('bet-edge-pct'\)[\s\S]*value = ''/.test(fn)
        && /sel\.onchange[\s\S]*prefillOdd = ''/.test(fn),
      'the dropdown handler leaves bet-edge-pct / prefillOdd from the last Track Bet in place');
  }

  // ── 5. Track Bet: the form never suggests more than the card does ──────────
  console.log('\n5. Track Bet prefill agrees with the card');
  const event = (over = {}) => Object.assign({
    event_id: 'ev9', sport_name: 'La Liga', home_team: 'A', away_team: 'B',
    commence_time: '2026-11-20T20:00:00Z', pin_home: 1.9, pin_away: 2.0, pin_low_liquidity: false,
    ref_agreement: 'single', odds_source: 'pinnacle', updated_at: agedUpdatedAt(0.1),
    true_home_pct: 51.5, true_away_pct: 48.5,
    all_picks: [{ market: 'Match Result', selection: 'A', book: '1xBet', book_odd: 2.0, edge_pct: 3.0, confidence: 4, kelly_pct: 0.75 }],
  }, over);
  const track = (b) => { fillForm(); for (const k of ['bet-odds', 'bet-stake', 'bet-edge-pct']) el(k).value = ''; el('bet-form').dataset = {}; quickAddBet(b); };
  {
    track(event());
    const expected = (1000 * (3 / 100) / (2.0 - 1) * 0.25).toFixed(2);     // full ¼-Kelly
    check('a green pick is prefilled with the ¼-Kelly stake, its odd and its edge',
      el('bet-stake').value === expected && el('bet-odds').value === 2.0 && el('bet-edge-pct').value === 3.0,
      `stake='${el('bet-stake').value}' (expected ${expected}) odds=${el('bet-odds').value} edge=${el('bet-edge-pct').value}`);
    check('...and remembers what it was prefilled with',
      el('bet-form').dataset.prefillOdd === '2' && el('bet-form').dataset.prefillEdge === '3',
      JSON.stringify(el('bet-form').dataset));
    check('...without asking anything', confirms.length === 0, JSON.stringify(confirms));
  }
  {
    confirmAnswer = false;
    track(event({ updated_at: agedUpdatedAt(10) }));
    check('odds 10h old: Track Bet warns BEFORE the bet is logged',
      confirms.length === 1 && /10\.0h/.test(confirms[0]) && /Refresh/.test(confirms[0]), JSON.stringify(confirms));
    check('...and cancelling leaves the form untouched', el('bet-odds').value === '' && el('bet-stake').value === '',
      `odds='${el('bet-odds').value}' stake='${el('bet-stake').value}'`);
    confirmAnswer = true;
    track(event({ updated_at: agedUpdatedAt(10) }));
    check('...and going ahead anyway fills the odd but NO stake, and says why',
      el('bet-odds').value === 2.0 && el('bet-stake').value === '' && /Sem stake sugerida/.test(el('bet-stake-hint').textContent),
      `odds=${el('bet-odds').value} stake='${el('bet-stake').value}' hint='${el('bet-stake-hint').textContent}'`);
  }
  {
    const b = event(); b.all_picks[0].book_odd = 4.5; b.all_picks[0].edge_pct = 6;
    track(b);
    check('odd above 4.0 (card shows no stake) → no stake in the form either',
      el('bet-stake').value === '' && el('bet-odds').value === 4.5,
      `stake='${el('bet-stake').value}' odds=${el('bet-odds').value}`);
  }
  {
    // Value only at another house; 1xBet quotes the selection with a NEGATIVE edge.
    const b = event();
    b.all_picks = [
      { market: 'Match Result', selection: 'A', book: 'Best',  book_odd: 2.1, edge_pct: 4.0, confidence: 4, kelly_pct: 0.9 },
      { market: 'Match Result', selection: 'A', book: '1xBet', book_odd: 1.9, edge_pct: -2.0, confidence: 1, kelly_pct: 0 },
    ];
    track(b);
    check('no edge at 1xBet → the form is NOT filled with a stake sized on another house\'s price',
      el('bet-stake').value === '' && el('bet-odds').value === 1.9,
      `stake='${el('bet-stake').value}' odds=${el('bet-odds').value}`);
  }

  // ── 6. Refresh asks before spending held-back credits ──────────────────────
  console.log('\n6. Refresh: the server can ask "are you sure?"');
  const refreshWith = async (firstBody, answer) => {
    for (const k of Object.keys(els)) delete els[k];
    fetchCalls = []; confirms = []; confirmAnswer = answer;
    fetchImpl = async (url) => jsonResp(200, /force=1/.test(url) ? { ok: true, msg: 'started' } : firstBody);
    await refreshOdds();
  };
  {
    await refreshWith({ ok: false, needs_confirm: true, reason: 'reserve', msg: 'Só restam 41 créditos.' }, true);
    check('the page asks, quoting the server\'s reason',
      confirms.length === 1 && /41 créditos/.test(confirms[0]), JSON.stringify(confirms));
    check('...and on "yes" repeats the request with force=1',
      fetchCalls.length === 2 && /force=1/.test(fetchCalls[1].url), JSON.stringify(fetchCalls.map(c => c.url)));
  }
  {
    await refreshWith({ ok: false, needs_confirm: true, reason: 'recent', msg: 'Atualizadas há 3 min.' }, false);
    check('on "no" nothing more is sent and the button is usable again',
      fetchCalls.length === 1 && el('refresh-odds-btn').disabled === false
        && el('refresh-odds-btn').textContent === '↻ Refresh',
      `calls=${fetchCalls.length} disabled=${el('refresh-odds-btn').disabled} text=${el('refresh-odds-btn').textContent}`);
  }
  {
    await refreshWith({ ok: true, msg: 'started' }, true);
    check('a normal refresh is not interrupted by a question',
      confirms.length === 0 && fetchCalls.length === 1 && !/force/.test(fetchCalls[0].url),
      `confirms=${confirms.length} calls=${JSON.stringify(fetchCalls.map(c => c.url))}`);
  }

  console.log(`\n${failures === 0 ? '✅ ALL CHECKS PASSED' : `❌ ${failures} CHECK(S) FAILED`}\n`);
  process.exit(failures === 0 ? 0 : 1);
})();
