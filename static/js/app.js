let footballData = [];
let tennisData = [];
let nationalData = [];
let edgeData = [];
let trackedBetsMap = {};

// THEME TOGGLE
function applyTheme(theme) {
  if (theme === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
  updateThemeLabel();
}

function updateThemeLabel() {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  const label = document.querySelector('.theme-label');
  const moon = document.querySelector('.icon-moon');
  const sun = document.querySelector('.icon-sun');
  if (label) label.textContent = isDark ? 'Light mode' : 'Dark mode';
  if (moon) moon.style.display = isDark ? 'none' : 'inline-block';
  if (sun) sun.style.display = isDark ? 'inline-block' : 'none';
}

function toggleTheme() {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  const next = isDark ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem('theme', next); } catch(e) {}
}

(function initTheme() {
  try {
    const saved = localStorage.getItem('theme');
    applyTheme(saved || 'dark'); // dark by default
  } catch(e) { applyTheme('dark'); }
})();

async function fetchJSON(url) {
  try {
    const r = await fetch(url);
    return await r.json();
  } catch (e) {
    return null;
  }
}

// Navigation
document.querySelectorAll('.nav-link').forEach(link => {
  link.addEventListener('click', e => {
    const sec = link.dataset.section;
    // Real navigation links (e.g. /backtest) have no data-section — let the
    // browser follow the href instead of swallowing the click with preventDefault.
    if (!sec) return;
    e.preventDefault();
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    link.classList.add('active');
    document.getElementById('section-' + sec).classList.add('active');
    if (sec === 'overview') loadOverviewSummaries();
    if (sec === 'football' && footballData.length === 0) loadFootball();
    if (sec === 'tennis' && tennisData.length === 0) loadTennis();
    if (sec === 'edge') loadEdge();
    if (sec === 'mybets') loadMyBets();
    if (sec === 'performance') loadPerformance();
    if (sec === 'national' && nationalData.length === 0) loadNational();
    if (sec === 'collector') checkCollectionRunning();
  });
});

// Overview summaries are secondary (data-collection stats) — load them lazily the
// first time the user opens Overview, so the landing (Value Bets) stays fast.
let _overviewLoaded = false;
function loadOverviewSummaries() {
  if (_overviewLoaded) return;
  _overviewLoaded = true;
  loadFootballSummary();
  loadTennisSummary();
  loadCollectionLog();
}

// Stats
async function loadStats() {
  const s = await fetchJSON('/api/stats');
  if (!s) return;
  document.getElementById('s-football').textContent = s.football_matches.toLocaleString();
  document.getElementById('s-tennis').textContent = s.tennis_matches.toLocaleString();
  document.getElementById('s-leagues').textContent = s.football_leagues;
  document.getElementById('s-last').textContent = s.last_collection;
  document.getElementById('last-update').textContent = 'Last: ' + s.last_collection;
  const vbEl = document.getElementById('s-value-bets');
  if (vbEl) vbEl.textContent = s.value_bets_today ?? '—';
  const pbEl = document.getElementById('s-pending-bets');
  if (pbEl) {
    pbEl.textContent = s.pending_bets ?? '—';
    if (s.pending_bets > 0) pbEl.style.color = 'var(--amber)';
  }
}

// Football summary
async function loadFootballSummary() {
  try {
    const rows = await fetchJSON('/api/football/summary');
    const el = document.getElementById('football-summary-overview');
    if (!rows || rows.length === 0) {
      el.innerHTML = '<div class="summary-item"><span class="label">No data yet — run collector</span></div>';
      return;
    }
    el.innerHTML = rows.map(r =>
      `<div class="summary-item">
        <span class="label">${r.league_name}</span>
        <span class="range">${r.from_date} → ${r.to_date}</span>
        <span class="count">${r.matches.toLocaleString()}</span>
      </div>`
    ).join('');
  } catch (error) {
    console.error('Football load error:', error);
    const el = document.getElementById('football-summary-overview');
    el.innerHTML = '<div class="summary-item"><span class="label" style="color:#E74C3C">Unable to load football data — check Data Collector</span></div>';
  }
}

// Tennis summary
async function loadTennisSummary() {
  try {
    const rows = await fetchJSON('/api/tennis/summary');
    const el = document.getElementById('tennis-summary-overview');
    if (!rows || rows.length === 0) {
      el.innerHTML = '<div class="summary-item"><span class="label">No data yet — run collector</span></div>';
      return;
    }
    el.innerHTML = rows.map(r =>
      `<div class="summary-item">
        <span class="label">${r.surface || 'Unknown surface'}</span>
        <span class="range">${r.from_date} → ${r.to_date}</span>
        <span class="count">${r.matches.toLocaleString()}</span>
      </div>`
    ).join('');
  } catch (error) {
    console.error('Tennis load error:', error);
    const el = document.getElementById('tennis-summary-overview');
    el.innerHTML = '<div class="summary-item"><span class="label" style="color:#E74C3C">Unable to load tennis data — check Data Collector</span></div>';
  }
}

// Collection log
async function loadCollectionLog() {
  const rows = await fetchJSON('/api/collection/log');
  const el = document.getElementById('collection-log-overview');
  if (!rows || rows.length === 0) {
    el.innerHTML = '<div class="summary-item"><span class="label">No collections run yet</span></div>';
    return;
  }
  el.innerHTML = `<table>
    <thead><tr>
      <th>Source</th><th>Status</th><th>Records</th><th>Message</th><th>Time</th>
    </tr></thead>
    <tbody>` +
    rows.map(r =>
      `<tr>
        <td class="strong">${r.source}</td>
        <td><span class="log-badge ${r.status}">${r.status}</span></td>
        <td class="mono">${r.records_added.toLocaleString()}</td>
        <td class="mono" style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${r.message || ''}</td>
        <td class="mono">${r.ran_at}</td>
      </tr>`
    ).join('') +
    '</tbody></table>';
}

// Football table
async function loadFootball() {
  try {
    const rows = await fetchJSON('/api/football/recent?limit=50');
    footballData = rows || [];
    renderFootballTable(footballData);
  } catch (error) {
    console.error('Football load error:', error);
    const wrap = document.getElementById('football-table-wrap');
    wrap.innerHTML = '<div style="padding:2rem 1.25rem;color:#E74C3C;font-size:13px"><strong>Error loading football data</strong><br>Check Data Collector or try refreshing.</div>';
  }
}

function renderFootballTable(data) {
  const wrap = document.getElementById('football-table-wrap');
  if (!data || data.length === 0) {
    wrap.innerHTML = '<div style="padding:2rem 1.25rem;color:#555b6e;font-size:13px">No data yet. Go to Data Collector and run collection first.</div>';
    return;
  }
  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Date</th><th>League</th><th>Home</th><th>Away</th><th>Score</th><th>Result</th>
      <th>Pin Home</th><th>Pin Draw</th><th>Pin Away</th>
      <th>Avg H</th><th>Avg D</th><th>Avg A</th>
    </tr></thead>
    <tbody>` +
    data.map(r => {
      const result = r.result || '';
      return `<tr>
        <td class="mono">${r.date || ''}</td>
        <td>${r.league_name || ''}</td>
        <td class="strong">${r.home_team || ''}</td>
        <td class="strong">${r.away_team || ''}</td>
        <td class="score">${r.home_goals ?? ''} – ${r.away_goals ?? ''}</td>
        <td><span class="result-badge result-${result}">${result}</span></td>
        <td class="mono">${fmt(r.pinnacle_home_close)}</td>
        <td class="mono">${fmt(r.pinnacle_draw_close)}</td>
        <td class="mono">${fmt(r.pinnacle_away_close)}</td>
        <td class="mono">${fmt(r.avg_home)}</td>
        <td class="mono">${fmt(r.avg_draw)}</td>
        <td class="mono">${fmt(r.avg_away)}</td>
      </tr>`;
    }).join('') +
    '</tbody></table>';
}

document.getElementById('football-search').addEventListener('input', function () {
  const q = this.value.toLowerCase();
  const filtered = footballData.filter(r =>
    (r.home_team || '').toLowerCase().includes(q) ||
    (r.away_team || '').toLowerCase().includes(q) ||
    (r.league_name || '').toLowerCase().includes(q)
  );
  renderFootballTable(filtered);
});

// Tennis table
async function loadTennis() {
  try {
    const rows = await fetchJSON('/api/tennis/recent?limit=50');
    tennisData = rows || [];
    renderTennisTable(tennisData);
  } catch (error) {
    console.error('Tennis load error:', error);
    const wrap = document.getElementById('tennis-table-wrap');
    wrap.innerHTML = '<div style="padding:2rem 1.25rem;color:#E74C3C;font-size:13px"><strong>Error loading tennis data</strong><br>Check Data Collector or try refreshing.</div>';
  }
}

function renderTennisTable(data) {
  const wrap = document.getElementById('tennis-table-wrap');
  if (!data || data.length === 0) {
    wrap.innerHTML = '<div style="padding:2rem 1.25rem;color:#555b6e;font-size:13px">No data yet. Go to Data Collector and run collection first.</div>';
    return;
  }
  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Date</th><th>Tournament</th><th>Surface</th><th>Round</th>
      <th>Winner</th><th>Rank</th><th>Loser</th><th>Rank</th><th>Score</th><th>Min</th>
    </tr></thead>
    <tbody>` +
    data.map(r =>
      `<tr>
        <td class="mono">${r.tourney_date || ''}</td>
        <td>${r.tourney_name || ''}</td>
        <td>${surfaceBadge(r.surface)}</td>
        <td class="mono">${r.round || ''}</td>
        <td class="strong">${r.winner_name || ''}</td>
        <td class="mono">${r.winner_rank || '—'}</td>
        <td>${r.loser_name || ''}</td>
        <td class="mono">${r.loser_rank || '—'}</td>
        <td class="score">${r.score || ''}</td>
        <td class="mono">${r.minutes || '—'}</td>
      </tr>`
    ).join('') +
    '</tbody></table>';
}

document.getElementById('tennis-search').addEventListener('input', function () {
  const q = this.value.toLowerCase();
  const filtered = tennisData.filter(r =>
    (r.winner_name || '').toLowerCase().includes(q) ||
    (r.loser_name || '').toLowerCase().includes(q) ||
    (r.tourney_name || '').toLowerCase().includes(q)
  );
  renderTennisTable(filtered);
});

// ============================================================
// VALUE BETS — Clean card-based design
// ============================================================
let vbState = {
  raw: [],
  sportFilter: '',
  surfaceFilter: '',
  whenFilter: 'all',
  mode: 'value',   // open decluttered: only games with an actionable pick (#7)
  bankroll: 0,
  minEdge: 3,    // default 3% — blocks Qatar at +0.2%
  maxOdds: 8.0,  // hard ceiling: picks up to 8.0 show as "best available"; >8 = info only
  minConf: 0,
  collapsed: new Set(),
  viewMode: 'cards',  // 'cards' or 'table'
  sortMode: 'clv',    // 'clv' (default) | 'edge' | 'date' — #2
  hideReds: false,    // #3 traffic-light: hide 🔴 (IGNORAR) cards when true
};
let _prevValueBetCount = -1;

async function loadEdge() {
  const [rows, bets] = await Promise.all([
    fetchJSON('/api/value-bets'),
    fetchJSON('/api/bets'),
  ]);
  vbState.raw = rows || [];
  edgeData = vbState.raw;

  trackedBetsMap = {};
  (bets || []).forEach(b => {
    if (b.event_id) {
      if (!trackedBetsMap[b.event_id]) trackedBetsMap[b.event_id] = [];
      trackedBetsMap[b.event_id].push(b);
    }
  });

  populateSportDropdown();
  wireFilters();
  renderValueBets();
}

function populateSportDropdown() {
  const sel = document.getElementById('vb-sport');
  if (!sel) return;
  const sports = [...new Set(vbState.raw.map(r => r.sport_name).filter(Boolean))].sort();
  const current = sel.value;
  sel.innerHTML = '<option value="">All sports</option>' +
    sports.map(s => `<option value="${s}">${s}</option>`).join('');
  sel.value = current || '';
}

function wireFilters() {
  if (vbState._wired) return;
  vbState._wired = true;
  document.getElementById('vb-sport').addEventListener('change', e => {
    vbState.sportFilter = e.target.value; renderValueBets();
  });
  const surfEl = document.getElementById('vb-surface');
  if (surfEl) surfEl.addEventListener('change', e => {
    vbState.surfaceFilter = e.target.value; renderValueBets();
  });
  document.getElementById('vb-when').addEventListener('change', e => {
    vbState.whenFilter = e.target.value; renderValueBets();
  });
  document.getElementById('vb-mode').addEventListener('change', e => {
    vbState.mode = e.target.value; renderValueBets();
  });
  const minEdgeEl = document.getElementById('vb-minedge');
  if (minEdgeEl) {
    try {
      const saved = localStorage.getItem('vb-minedge');
      if (saved) { minEdgeEl.value = saved; vbState.minEdge = parseFloat(saved); }
      else minEdgeEl.value = String(vbState.minEdge);
    } catch(e) {}
    minEdgeEl.addEventListener('change', e => {
      vbState.minEdge = parseFloat(e.target.value) || 0;
      try { localStorage.setItem('vb-minedge', e.target.value); } catch(e) {}
      renderValueBets();
    });
  }
  const maxOddsEl = document.getElementById('vb-maxodds');
  if (maxOddsEl) {
    try {
      const saved = localStorage.getItem('vb-maxodds');
      if (saved) { maxOddsEl.value = saved; vbState.maxOdds = parseFloat(saved); }
      else maxOddsEl.value = String(vbState.maxOdds);
    } catch(e) {}
    maxOddsEl.addEventListener('change', e => {
      vbState.maxOdds = parseFloat(e.target.value) || 999;
      try { localStorage.setItem('vb-maxodds', e.target.value); } catch(e) {}
      renderValueBets();
    });
  }
  const minConfEl = document.getElementById('vb-minconf');
  if (minConfEl) {
    minConfEl.addEventListener('change', e => {
      vbState.minConf = parseInt(e.target.value) || 0; renderValueBets();
    });
  }
  const bankrollEl = document.getElementById('vb-bankroll');
  if (bankrollEl) {
    try {
      const saved = localStorage.getItem('bankroll');
      if (saved) { bankrollEl.value = saved; vbState.bankroll = parseFloat(saved); }
    } catch(e) {}
    bankrollEl.addEventListener('input', e => {
      vbState.bankroll = parseFloat(e.target.value) || 0;
      try { localStorage.setItem('bankroll', e.target.value); } catch(e) {}
      renderValueBets();
    });
  }
  updatePresetButtons();
}

// ============================================================
// FILTER PRESETS — save / load named filter configurations
// ============================================================
function saveCurrentPreset() {
  const slot = prompt('Guardar em slot (1, 2 ou 3):', '1');
  if (!['1','2','3'].includes(String(slot).trim())) return;
  const name = prompt('Nome para este preset:', `Preset ${slot}`) || `Preset ${slot}`;
  const state = {
    _name: name,
    sportFilter: vbState.sportFilter || '',
    whenFilter: vbState.whenFilter,
    mode: vbState.mode,
    minEdge: vbState.minEdge,
    maxOdds: vbState.maxOdds,
    minConf: vbState.minConf,
    bankroll: vbState.bankroll,
  };
  try { localStorage.setItem(`vb-preset-${slot}`, JSON.stringify(state)); } catch(e) {}
  updatePresetButtons();
}

function loadPreset(slot) {
  try {
    const raw = localStorage.getItem(`vb-preset-${slot}`);
    if (!raw) { alert(`Slot ${slot} vazio — use "💾 Guardar" para gravar os filtros actuais.`); return; }
    const s = JSON.parse(raw);
    const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.value = String(val); };
    if (s.sportFilter !== undefined) { vbState.sportFilter = s.sportFilter; setEl('vb-sport', s.sportFilter || ''); }
    if (s.whenFilter !== undefined) { vbState.whenFilter = s.whenFilter; setEl('vb-when', s.whenFilter); }
    if (s.mode !== undefined) { vbState.mode = s.mode; setEl('vb-mode', s.mode); }
    if (s.minEdge !== undefined) { vbState.minEdge = s.minEdge; setEl('vb-minedge', s.minEdge); try { localStorage.setItem('vb-minedge', s.minEdge); } catch(e) {} }
    if (s.maxOdds !== undefined) { vbState.maxOdds = s.maxOdds; setEl('vb-maxodds', s.maxOdds); try { localStorage.setItem('vb-maxodds', s.maxOdds); } catch(e) {} }
    if (s.minConf !== undefined) { vbState.minConf = s.minConf; setEl('vb-minconf', s.minConf); }
    if (s.bankroll > 0) { vbState.bankroll = s.bankroll; setEl('vb-bankroll', s.bankroll); try { localStorage.setItem('bankroll', s.bankroll); } catch(e) {} }
    renderValueBets();
  } catch(e) {}
}

function updatePresetButtons() {
  for (let s = 1; s <= 3; s++) {
    const btn = document.querySelector(`.vb-preset-btn[data-slot="${s}"]`);
    if (!btn) continue;
    try {
      const raw = localStorage.getItem(`vb-preset-${s}`);
      if (raw) {
        const st = JSON.parse(raw);
        btn.textContent = st._name || `P${s}`;
        btn.title = `${st._name || 'Preset ' + s} — clique para aplicar`;
        btn.classList.add('has-preset');
      } else {
        btn.textContent = `P${s}`;
        btn.title = `Slot ${s} vazio`;
        btn.classList.remove('has-preset');
      }
    } catch(e) {}
  }
}

// ============================================================
// SOUND NOTIFICATION — plays a double-beep via Web Audio API
// ============================================================
function playNewBetSound() {
  const toggle = document.getElementById('vb-sound');
  if (toggle && !toggle.checked) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [[880, 0], [1100, 0.18]].forEach(([freq, t]) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain); gain.connect(ctx.destination);
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.22, ctx.currentTime + t);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + t + 0.22);
      osc.start(ctx.currentTime + t);
      osc.stop(ctx.currentTime + t + 0.25);
    });
  } catch(e) {}
}

// ============================================================
// VALUE EVALUATION — ONE source of truth for the whole page
// ------------------------------------------------------------
// A recommendation comes ONLY from real automatic odds and NEVER above the
// odds ceiling. Stars + value belong to the Best Pick: if there is no eligible
// Best Pick, the match has NO stars and is NOT "value" — so the confidence
// filters can't pull a game in just because some >ceiling longshot looked good
// (that was the "5★ but Best Pick = no value" bug).
// ============================================================
const VB_VALUE_FLOOR = 2;        // edge% needed to be a green / celebrated value bet
                                 // (lowered 3→2: calibration MAE 0.75pp makes 2% real
                                 //  signal in the mid-range; picks here carry +CLV)
const VB_ODD_FLOOR = 1.4;        // min odd for a celebrated pick (1.4 = just above the
                                 //  short-odd stake softener; favourites aren't overpriced)
const VB_GREEN_MAX_ODD = 4.0;    // green "Best Pick" sweet-spot odd cap (KEEP at 4.0: devig
                                 //  over-estimates longshots >4.0, so those 2% edges are false)
const VB_SHORT_ODD = 1.40;       // below this, soften the stake (calibration-sensitive favourites)
const VB_HARD_CEILING = 8.0;     // above this odd, never shown as a pick (info only)
function vbOddCeiling() { return Math.min(vbState.maxOdds ?? VB_HARD_CEILING, VB_HARD_CEILING); }

// ¼-Kelly fraction of bankroll for a given edge% and odd, with two safety rules:
//  • odd < 1.40 (≈>71% favourite): HALVE the stake AND hard-cap at 1.5% of bankroll.
//    A 1-2pp calibration error on a heavy favourite flips +EV to −EV, so the raw
//    ¼-Kelly (which grows as odds shrink) is too aggressive there.
//  • odd 1.40–4.0: normal ¼-Kelly (untouched).
// Bankroll-independent (returns a fraction); callers multiply by their bankroll.
function vbKellyFraction(edge, odd) {
  if (!edge || !odd || edge <= 0 || odd <= 1) return 0;
  let f = (edge / 100) / (odd - 1) * 0.25;        // ¼-Kelly on the edge
  if (odd < VB_SHORT_ODD) f = Math.min(f * 0.5, 0.015);  // short-odd: ½-size, cap 1.5% bankroll
  return f;
}

function vbEval(b) {
  const minEdge = vbState.minEdge ?? 3;
  const ceiling = vbOddCeiling();
  // Best real odd per market+selection, within the ceiling. >ceiling = ignored.
  const byKey = {};
  for (const p of (b.all_picks || [])) {
    if (!p.book_odd || p.book_odd <= 1 || p.book_odd > ceiling) continue;
    const key = `${p.market}|||${p.selection}`;
    const ex = byKey[key];
    if (!ex) { byKey[key] = p; continue; }
    if (p.book === 'Best' && ex.book !== 'Best') byKey[key] = p;
    else if (ex.book !== 'Best' && p.book_odd > ex.book_odd) byKey[key] = p;
  }
  const realPicks = Object.values(byKey);
  const valuePicks = realPicks.filter(p => p.edge_pct != null && p.edge_pct >= minEdge && p.edge_pct <= 15);
  const bestPick = valuePicks.length
    ? valuePicks.reduce((a, p) => ((p.confidence||0)*100 + p.edge_pct) > ((a.confidence||0)*100 + a.edge_pct) ? p : a)
    : null;
  // Sharp consensus (A+B): the green "value" tier requires that the two sharp
  // references don't disagree. 'diverge' = Pinnacle & Betfair give materially
  // different fair probs → edge is uncertain → never celebrate as green.
  // 'agree' | 'single' | 'diverge_sharp' (two sharps disagree → block green) |
  // 'diverge_model' (our model disagrees with the market → caution, still green)
  const refAgree = b.ref_agreement;
  const sharpConflict = refAgree === 'diverge_sharp';
  const isValue = !!bestPick && bestPick.edge_pct >= VB_VALUE_FLOOR
      && bestPick.book_odd >= VB_ODD_FLOOR && bestPick.book_odd <= VB_GREEN_MAX_ODD
      && !sharpConflict;
  // Stars = the Pinnacle-earned base confidence (edge quality + liquidity + league).
  // Our xG/Elo model is a WEAKER cross-check than Pinnacle, so a model disagreement
  // is shown as an INFORMATIONAL orange badge ONLY — it does NOT change stars or
  // stake. The only star override is two SHARP markets disagreeing (Betfair vs
  // Pinnacle), which is rare and genuinely means the truth is uncertain → cap 2.
  let stars = bestPick ? (bestPick.confidence || 0) : 0;
  if (sharpConflict) stars = Math.min(stars, 2);
  return { realPicks, bestPick, isValue, stars, ceiling, refAgree };
}

// Est. CLV of a card = the edge at the price the USER can actually get (1xBet) for
// the best pick, else the best-price edge. This is the #1 decision signal — used
// to sort the page and as the dominant header chip. null = no actionable pick.
function vbClv(b) {
  const ev = vbEval(b);
  if (!ev.bestPick) return null;
  const mine = _myBookPick(b, ev.bestPick);
  // CLV only exists at the price YOU can bet (1xBet). No 1xBet quote ⇒ no CLV ⇒
  // null, so the card sinks in the CLV sort instead of ranking on an unbettable price.
  return (mine && mine.edge_pct != null) ? mine.edge_pct : null;
}

// #3 Traffic-light decision signal per card, judged on the price YOU can bet
// (1xBet) for the best pick. CLV here = the 1xBet edge vs the sharp close.
//   🟢 green  (APOSTAR) : odd 1.8-4.0 & CLV>=2%   OR   odd 1.3-1.8 & CLV>=3%
//   🟡 yellow (VER)     : odd 1.3-1.8 & CLV 2-3%
//   🔴 red   (IGNORAR)  : odd >4.0, OR CLV <2%, OR no bettable/negative 1xBet edge
function vbSignal(b) {
  const ev = vbEval(b);
  if (!ev.bestPick) return { light: 'red', clv: null, odd: null };
  const mine = _myBookPick(b, ev.bestPick);
  const clv = (mine && mine.edge_pct != null) ? mine.edge_pct : null;
  const odd = mine ? mine.book_odd : null;
  if (clv == null || odd == null) return { light: 'red', clv, odd };
  if (clv < 2 || odd > 4.0) return { light: 'red', clv, odd };
  if (odd >= 1.8 && odd <= 4.0) return { light: 'green', clv, odd };            // CLV>=2 here
  if (odd >= 1.3 && odd < 1.8) return { light: clv >= 3 ? 'green' : 'yellow', clv, odd };
  return { light: 'red', clv, odd };                                            // odd <1.3 (too short)
}
const VB_LIGHT_RANK = { green: 0, yellow: 1, red: 2 };
const VB_LIGHT_META = {
  green:  { dot: '🟢', label: 'APOSTAR', cls: 'sig-green' },
  yellow: { dot: '🟡', label: 'VER',     cls: 'sig-yellow' },
  red:    { dot: '🔴', label: 'IGNORAR', cls: 'sig-red' },
};

// ============================================================
// COMPACT TABLE VIEW — dense row-per-event alternative to cards
// ============================================================
function renderTableView(data) {
  const wrap = document.getElementById('vb-cards');
  if (!data.length) { wrap.innerHTML = '<div class="vb-empty"><div class="vb-empty-title">No matches match your filters</div></div>'; return; }
  const _br = vbState.bankroll || 0;
  const _qMinEdge = vbState.minEdge ?? 3;
  const _qMaxOdds = vbState.maxOdds ?? 8.0;
  const header = `<div class="vb-table-header">
    <span class="vbt-edge">Edge</span>
    <span class="vbt-event">Match</span>
    <span class="vbt-pick">Best Pick</span>
    <span class="vbt-book">Book</span>
    <span class="vbt-fair">Fair</span>
    <span class="vbt-kelly">Kelly</span>
    <span class="vbt-conf">★</span>
    <span class="vbt-time">Time</span>
  </div>`;
  const rows = data.map(b => {
    const ev = vbEval(b);
    const bv = ev.bestPick;
    const isValue = ev.isValue;
    const edgePct = bv?.edge_pct ?? null;
    let eCls = 'neg';
    if (edgePct != null) {
      if (edgePct > 15) eCls = 'noise';
      else if (edgePct >= _qMinEdge) eCls = isValue ? 'pos' : 'flat';
      else if (edgePct >= 1) eCls = 'flat';
    }
    const sign = edgePct > 0 ? '+' : '';
    const fairOdd = bv?.fair_odd
      ? bv.fair_odd.toFixed(2)
      : (bv?.book_odd && edgePct != null ? (bv.book_odd / (1 + edgePct / 100)).toFixed(2) : '—');
    // Kelly sized on the 1xBet price only (same rule as the cards): no 1xBet quote
    // or odd >4.0 ⇒ no stake. Short odds (<1.40) are softened by vbKellyFraction.
    let kellyStr = '—';
    const _mineT = bv ? _myBookPick(b, bv) : null;
    if (_mineT && _mineT.edge_pct > 0 && _mineT.book_odd <= VB_GREEN_MAX_ODD) {
      const kQ = vbKellyFraction(_mineT.edge_pct, _mineT.book_odd);
      if (kQ > 0) kellyStr = _br > 0 ? `€${(_br * kQ).toFixed(0)}` : `${(kQ * 100).toFixed(1)}%`;
    }
    const conf = ev.stars;
    const timeStr = (() => {
      if (!b.commence_time) return '—';
      const diff = (new Date(b.commence_time) - Date.now()) / 3600000;
      if (diff < 0) return 'started';
      if (diff < 24) { const h = Math.floor(diff); const m = Math.floor((diff-h)*60); return `${h}h${m}m`; }
      return new Date(b.commence_time).toLocaleDateString('en-GB', {day:'2-digit', month:'short'});
    })();
    return `<div class="vb-table-row${isValue ? ' is-value' : ''}" onclick="window.location.href='/match/${b.event_id}'" title="Ver detalhes">
      <span class="vbt-edge"><span class="edge-chip ${eCls}">${edgePct != null ? sign + edgePct.toFixed(1)+'%' : '—'}</span></span>
      <span class="vbt-event"><span class="vbt-sport">${b.sport_name||''}</span>${b.home_team} <em>v</em> ${b.away_team}</span>
      <span class="vbt-pick">${bv?.selection || '—'}<span class="vbt-mkt">${bv?.market || ''}</span></span>
      <span class="vbt-book">${fmtOdd(bv?.book_odd)}</span>
      <span class="vbt-fair">${fairOdd}</span>
      <span class="vbt-kelly">${kellyStr}</span>
      <span class="vbt-conf">${'★'.repeat(conf)}<span class="empty">${'★'.repeat(5-conf)}</span></span>
      <span class="vbt-time">${timeStr}</span>
    </div>`;
  }).join('');
  wrap.innerHTML = `<div class="vb-table-wrap">${header}${rows}</div>`;
}

function toggleViewMode() {
  vbState.viewMode = vbState.viewMode === 'cards' ? 'table' : 'cards';
  const btn = document.getElementById('vb-view-toggle');
  if (btn) btn.textContent = vbState.viewMode === 'cards' ? '☰ Table' : '▦ Cards';
  renderValueBets();
}

function setSortMode(mode) {
  vbState.sortMode = mode;
  document.querySelectorAll('.vb-sort-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.sort === mode));
  renderValueBets();
}

// #3 Toggle hiding 🔴 (IGNORAR) cards.
function toggleHideReds() {
  vbState.hideReds = !vbState.hideReds;
  const btn = document.getElementById('vb-hidered');
  if (btn) {
    btn.classList.toggle('active', vbState.hideReds);
    btn.textContent = vbState.hideReds ? '🔴 Mostrar ignorados' : '🔴 Esconder ignorados';
  }
  renderValueBets();
}

// TBD knockout-bracket placeholders ("1A", "2B", "W73", "L101", "3A/B/C/D/F").
// They have no real teams/odds and emit fake identical probabilities — pure noise.
function isPlaceholderTeam(t) {
  if (!t) return true;
  const s = String(t).trim();
  if (s.includes('/')) return true;          // "3A/B/C/D/F"
  if (/^[WL]\d+$/i.test(s)) return true;      // W73, L101
  if (/^\d+[A-Z]?$/i.test(s)) return true;    // 1A, 2B, 2I, 23
  return false;
}

// De-duplicate the same fixture appearing more than once. We saw the SAME match
// (identical teams + odds) listed under two event records / two dates — e.g.
// Portugal-DR Congo shown as both "today, 21h" and "17 Jun" — which inflates the
// counts and risks betting the same game twice. Key = sport|home|away (normalised).
// Within a key we keep the SOONEST upcoming entry and drop others that start within
// DEDUP_WINDOW_DAYS of it — so two genuine meetings weeks apart (domestic leagues
// from August) are NOT merged.
const DEDUP_WINDOW_DAYS = 4;
function dedupeEvents(rows) {
  const norm = s => (s || '').toString().trim().toLowerCase().replace(/\s+/g, ' ');
  // Does this copy actually carry a bettable line? (Duplicates of the same fixture
  // often come from two sources/times where only ONE has odds.)
  const hasOdds = r => (r.all_picks || []).some(p => p.book_odd && p.book_odd > 1)
      || !!(r.x1_home || r.best_home || r.pin_home);
  const byKey = {};
  for (const r of rows) {
    const key = `${norm(r.sport_name)}|${norm(r.home_team)}|${norm(r.away_team)}`;
    (byKey[key] = byKey[key] || []).push(r);
  }
  const out = [];
  for (const group of Object.values(byKey)) {
    if (group.length === 1) { out.push(group[0]); continue; }
    // Sort by kickoff (soonest first; missing time goes last).
    group.sort((a, b) => (new Date(a.commence_time || '9999').getTime()) - (new Date(b.commence_time || '9999').getTime()));
    const kept = [];
    for (const r of group) {
      const t = r.commence_time ? new Date(r.commence_time).getTime() : null;
      const dupIdx = kept.findIndex(k => {
        const kt = k.commence_time ? new Date(k.commence_time).getTime() : null;
        if (t == null || kt == null) return true;  // no time on either ⇒ treat as dup
        return Math.abs(t - kt) <= DEDUP_WINDOW_DAYS * 86400000;
      });
      if (dupIdx === -1) { kept.push(r); continue; }
      // Same fixture already kept within the window: prefer the copy WITH odds, so a
      // no-odds duplicate doesn't win and then get dropped by the "already started"
      // filter — which made the whole fixture vanish even though a priced copy existed.
      if (hasOdds(r) && !hasOdds(kept[dupIdx])) kept[dupIdx] = r;
    }
    out.push(...kept);
  }
  return out;
}

function applyVbFilters(rows) {
  let out = rows.slice();
  const minConf = vbState.minConf ?? 0;

  // Always drop TBD bracket placeholders — never bettable, only clutter.
  out = out.filter(r => !isPlaceholderTeam(r.home_team) && !isPlaceholderTeam(r.away_team));

  // Collapse duplicate fixtures (same teams listed twice) before anything else.
  out = dedupeEvents(out);

  // PRE-MATCH ONLY: drop games that have already started. Our odds refresh every
  // few hours, so a live (in-play) line is stale and its "edge" is just noise
  // (e.g. a tennis player wins set 1 and the price jumps before we re-fetch).
  out = out.filter(r => {
    if (!r.commence_time) return true;
    return new Date(r.commence_time).getTime() > Date.now();
  });

  if (vbState.sportFilter) out = out.filter(r => r.sport_name === vbState.sportFilter);
  // Surface is a tennis concept → when set, show only matching-surface tennis events.
  if (vbState.surfaceFilter) out = out.filter(r => isTennisEvent(r) && tennisSurface(r) === vbState.surfaceFilter);
  if (vbState.whenFilter !== 'all') {
    const now = Date.now();
    const hoursMap = {'3h': 3, '6h': 6, '12h': 12, '24h': 24, '48h': 48, '7d': 168, '14d': 336, '30d': 720};
    const hours = hoursMap[vbState.whenFilter] || 168;
    const cutoff = now + hours * 3600 * 1000;
    out = out.filter(r => {
      const t = r.commence_time ? new Date(r.commence_time).getTime() : 0;
      return t >= now && t <= cutoff;
    });
  }
  if (vbState.mode === 'value') {
    // Only matches that HAVE an eligible Best Pick (real odds, within the gate).
    out = out.filter(r => vbEval(r).bestPick != null);
  } else if (vbState.mode === 'confident') {
    // 4+ stars ON THE BEST PICK — never on a >ceiling longshot in the table.
    out = out.filter(r => vbEval(r).stars >= 4);
  }
  if (minConf > 0) {
    // Confidence filter follows the Best Pick: no Best Pick ⇒ 0 stars ⇒ excluded.
    out = out.filter(r => vbEval(r).stars >= minConf);
  }
  return out;
}

function fmtOdd(v) { return v == null ? '—' : v.toFixed(2); }
function fmt(v) { return v == null ? '—' : Number(v).toFixed(2); }
function fmtPct(v) { return v == null ? '—' : v.toFixed(1) + '%'; }

function surfaceBadge(surface) {
  const s = (surface || '').toLowerCase();
  const color = s === 'hard' ? '#4f9cf9' : s === 'clay' ? '#d97706' : s === 'grass' ? '#16a34a' : '#8b90a0';
  return `<span style="background:${color}18;color:${color};border:1px solid ${color}38;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:500">${surface || '?'}</span>`;
}

// The user bets at 1xBet — return their pick (same market+selection) so we can
// show the price THEY can actually get + their real expected CLV.
function _myBookPick(b, p) {
  return (b.all_picks || []).find(q => q.book === '1xBet'
    && q.market === p.market && q.selection === p.selection && q.book_odd) || null;
}

function yourBookLine(b, p) {
  const mine = _myBookPick(b, p);
  if (!mine) return '';
  const same = Math.abs((mine.book_odd || 0) - (p.book_odd || 0)) < 1e-9;
  return `<div class="vb-yourbook">🎯 Your book · <strong>1xBet ${fmtOdd(mine.book_odd)}</strong>`
    + (same ? ` <span class="vb-yb-best">✓ matches best</span>` : '')
    + `</div>`;
}

// #2 Estimated CLV at bet time. Your bettable price vs the sharp no-vig fair IS
// your expected Closing Line Value — the #1 predictor of long-term profit. We
// show it for the price the user can actually get (1xBet), and flag the line
// trend (shortening toward the pick ⇒ CLV likely even better).
function clvEstLine(b, p) {
  const mine = _myBookPick(b, p);
  const e = mine && mine.edge_pct != null ? mine.edge_pct : p.edge_pct;
  if (e == null) return '';
  const cls = e >= 3 ? 'vb-clv-pos' : e >= 0 ? 'vb-clv-flat' : 'vb-clv-neg';
  // line trend on this selection (reuse the movement direction)
  let trend = '';
  const m = b.line_movement;
  if (m && m.opening && m.latest) {
    let key = p.selection === b.home_team ? 'home' : p.selection === b.away_team ? 'away' : p.selection === 'Draw' ? 'draw' : null;
    if (key && m.opening[key] && m.latest[key]) {
      const d = m.latest[key] - m.opening[key];
      if (d < -0.03) trend = ' · trending your way ✓';
      else if (d > 0.03) trend = ' · drifting — bet now';
    }
  }
  // Only the 1xBet price is YOUR CLV. If 1xBet doesn't quote it, the edge belongs
  // to another book — say so plainly instead of dressing a best-price edge as CLV.
  if (!mine) {
    return `<div class="vb-clvest vb-clv-flat">📈 Edge <strong>${e >= 0 ? '+' : ''}${e.toFixed(1)}%</strong>`
      + ` <span class="vb-clv-note">noutra casa — a 1xBet não cota isto, logo não há CLV para ti</span></div>`;
  }
  return `<div class="vb-clvest ${cls}">📈 Est. CLV <strong>${e >= 0 ? '+' : ''}${e.toFixed(1)}%</strong>`
    + ` <span class="vb-clv-note">(1xBet vs sharp close — the #1 profit signal)${trend}</span></div>`;
}

// Line movement for the recommended pick's own selection (sharp signal, front
// and centre). Odds SHORTENING on our side = money coming in = market agrees
// with us (positive CLV signal). Lengthening = drifting = be cautious.
function lineMoveLine(b, p) {
  const m = b.line_movement;
  if (!m || !m.opening || !m.latest) return '';
  let key = null;
  if (p.selection === b.home_team) key = 'home';
  else if (p.selection === b.away_team) key = 'away';
  else if (p.selection === 'Draw') key = 'draw';
  if (!key) return '';
  const o = m.opening[key], l = m.latest[key];
  if (!o || !l) return '';
  const delta = l - o;
  if (Math.abs(delta) < 0.03) return `<div class="vb-linemove vb-lm-flat">↔ Line stable (${o.toFixed(2)})</div>`;
  const shortened = delta < 0;
  const arrow = shortened ? '📉' : '📈';
  const txt = shortened ? 'money coming in — market agrees ✓' : 'drifting out — be cautious';
  return `<div class="vb-linemove ${shortened ? 'vb-lm-in' : 'vb-lm-out'}">${arrow} Line ${o.toFixed(2)} → ${l.toFixed(2)} · ${txt}</div>`;
}

// Is this a tennis event?
function isTennisEvent(b) {
  const s = (b.sport_name || '').toLowerCase();
  return s.includes('atp') || s.includes('wta') || s.includes('tennis');
}

// The odds feed has no surface, so infer it from the tournament name. Tournaments
// keep the same surface year to year, so a keyword map covers most of the tour
// (not just the Slams). Unknown tournaments return null (stay unfiltered/unbadged).
const _SURFACE_KEYWORDS = {
  Grass: ['wimbledon', 'halle', 'queen', 's-hertogenbosch', 'hertogenbosch', 'stuttgart', 'eastbourne', 'mallorca', 'newport', 'nottingham', 'birmingham'],
  Clay: ['roland garros', 'french open', 'monte carlo', 'monte-carlo', 'madrid', 'rome', 'italian open', 'hamburg', 'barcelona', 'munich', 'estoril', 'bastad', 'gstaad', 'kitzbuhel', 'umag', 'geneva', 'lyon', 'bucharest', 'rabat', 'charleston', 'stuttgart open', 'strasbourg', 'parma'],
  Hard: ['us open', 'australian', 'aus open', 'miami', 'indian wells', 'cincinnati', 'canada', 'montreal', 'toronto', 'dubai', 'doha', 'acapulco', 'shanghai', 'beijing', 'tokyo', 'vienna', 'basel', 'paris masters', 'rotterdam', 'marseille', 'metz', 'antwerp', 'astana', 'adelaide', 'brisbane', 'auckland', 'washington', 'winston', 'chengdu', 'zhuhai'],
};

function tennisSurface(b) {
  if (b.surface) return b.surface;           // backend hint wins
  if (!isTennisEvent(b)) return null;
  const s = (b.sport_name || '').toLowerCase();
  for (const [surf, kws] of Object.entries(_SURFACE_KEYWORDS)) {
    if (kws.some(k => s.includes(k))) return surf;
  }
  return null;
}

// Surface badge for a live tennis card.
function tennisSurfaceBadge(b) {
  const surface = tennisSurface(b);
  return surface ? ' ' + surfaceBadge(surface) : '';
}

// #5 Timing badge — distant games' lines move a lot; the optimal window is 1-48h.
function timingBadge(b) {
  if (!b.commence_time) return '';
  const ms = new Date(b.commence_time) - Date.now();
  if (ms <= 0) return '';
  const h = ms / 3600000, d = h / 24;
  if (d > 7) return `<span class="vb-tbadge tb-far" title="A linha vai mover muito até ao jogo. Timing ótimo: 1-48h antes — considera esperar.">⏳ ${Math.round(d)}d — linha vai mexer</span>`;
  if (h < 24) return `<span class="vb-tbadge tb-soon">🕐 Hoje</span>`;
  if (h < 48) return `<span class="vb-tbadge tb-soon">🕐 Amanhã</span>`;
  return '';   // 2-7 days: neutral
}

// #6 Sharp-liquidity context by sport — the true prob is only as good as the sharp
// line's liquidity. Informational; the user decides.
function liquidityBadge(b) {
  const s = (b.sport_name || '').toLowerCase();
  let txt, cls;
  if (s.includes('atp') || s.includes('wta') || s.includes('tennis')) { txt = 'Sharp: boa liquidez'; cls = 'lq-good'; }
  else if (s.includes('boxing') || s.includes('mma') || s.includes('ufc')) { txt = 'Sharp: liquidez média — edge menos fiável'; cls = 'lq-mid'; }
  else if (s.includes('cricket')) { txt = 'Sharp: liquidez variável'; cls = 'lq-var'; }
  else if (s.includes('baseball') || s.includes('mlb') || s.includes('npb') || s.includes('kbo')) { txt = 'Sharp: boa liquidez'; cls = 'lq-good'; }
  else if (s.includes('soccer') || s.includes('football') || s.includes('league') || s.includes('liga') || s.includes('serie') || s.includes('cup') || s.includes('premier') || s.includes('bundesliga') || s.includes('ligue') || s.includes('eredivisie')) { txt = 'Sharp: alta liquidez'; cls = 'lq-good'; }
  else return '';
  return `<span class="vb-lqbadge ${cls}" title="Liquidez da linha sharp (Pinnacle): quanto maior, mais fiável o edge">${txt}</span>`;
}

// #4 How old is the stored odd? Odds move; a stale price means the CLV was computed
// on dead data. updated_at is stored UTC ("YYYY-MM-DD HH:MM").
function oddsAgeLine(b) {
  if (!b.updated_at) return '';
  const t = new Date(String(b.updated_at).replace(' ', 'T') + 'Z');
  if (isNaN(t)) return '';
  const mins = (Date.now() - t) / 60000;
  if (mins < 0) return '';
  let label, cls;
  if (mins < 120) { label = `🕒 Odds updated ${mins < 1 ? 'just now' : Math.round(mins) + ' min ago'}`; cls = 'oa-fresh'; }
  else if (mins < 360) { label = `⚠ Odds ${(mins / 60).toFixed(1)}h old — verify price before placing`; cls = 'oa-warn'; }
  else { label = `⛔ Odds ${Math.round(mins / 60)}h old — likely stale`; cls = 'oa-stale'; }
  return `<span class="vb-oddsage ${cls}">${label}</span>`;
}
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const diff = (d - now) / (1000 * 60 * 60); // hours
  const opts = {day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'};
  const formatted = d.toLocaleString('en-GB', opts);
  if (diff < 24 && diff > 0) {
    const h = Math.floor(diff);
    const m = Math.floor((diff - h) * 60);
    return `${formatted} · in ${h}h${m}m`;
  }
  return formatted;
}

function starsHTML(n) {
  let h = '<span class="vb-stars">';
  for (let i = 1; i <= 5; i++) {
    h += `<span class="vb-star${i > n ? ' empty' : ''}">★</span>`;
  }
  return h + '</span>';
}

function edgeClass(e) {
  if (e == null) return 'edge-neg';
  if (e > 15) return 'edge-noise';
  if (e >= 3) return 'edge-pos';
  if (e >= 1) return 'edge-flat';
  return 'edge-neg';
}

// Rank: value picks on top (by confidence × edge), then positive edge, then the rest.
// Pros want "best pick of the day" first — never date order.
function vbRankScore(b) {
  const ev = vbEval(b);
  if (!ev.bestPick) return -1e9;                                  // no recommendable pick → bottom
  const edge = ev.bestPick.edge_pct;
  const conf = ev.stars;
  if (ev.isValue) return 1e6 + conf * 100 + Math.min(edge, 15);   // value: rank by conf×edge
  if (edge > 15) return -100;                                     // noise/suspect sinks
  if (edge > 0) return edge;                                      // marginal positive
  return -1000 + edge;                                            // negative edge at bottom
}

function renderValueBets() {
  const wrap = document.getElementById('vb-cards');
  let data = applyVbFilters(vbState.raw);
  // #3 Sort: traffic-light tier FIRST (🟢 then 🟡 then 🔴), then the chosen sort
  // (CLV / Edge / Date) WITHIN each tier — so green "bet" picks always lead, by CLV.
  const sortMode = vbState.sortMode || 'clv';
  // #5 Multi-key compare so date and CLV COMBINE (not either/or):
  //   • Date sort  → soonest first, CLV (desc) as the tie-breaker.
  //   • CLV sort   → highest CLV first, soonest game as the tie-breaker
  //                  (catches value before the market corrects).
  // The time-window dropdown (vb-when: 12h/24h/…) is an independent DATE FILTER that
  // stacks on top of either sort — e.g. "próximas 12h ordenadas por CLV decrescente".
  const _within = (a, b) => {
    const ta = +new Date(a.commence_time || 0), tb = +new Date(b.commence_time || 0);
    const ca = vbClv(a), cb = vbClv(b);
    if (sortMode === 'date') {
      if (ta !== tb) return ta - tb;                 // soonest first
      return (cb ?? -1e9) - (ca ?? -1e9);            // tie → higher CLV first
    }
    if (sortMode === 'edge') return vbRankScore(b) - vbRankScore(a);
    // default: CLV
    if (ca == null && cb == null) return vbRankScore(b) - vbRankScore(a);
    if (ca == null) return 1;
    if (cb == null) return -1;
    if (cb !== ca) return cb - ca;                   // higher Est. CLV first
    return ta - tb;                                  // tie → soonest first
  };
  data.sort((a, b) => {
    const ra = VB_LIGHT_RANK[vbSignal(a).light], rb = VB_LIGHT_RANK[vbSignal(b).light];
    return ra !== rb ? ra - rb : _within(a, b);
  });

  // Traffic-light counts (computed on the FULL filtered set, before hiding 🔴).
  let greenCount = 0, yellowCount = 0, redCount = 0;
  for (const b of data) {
    const l = vbSignal(b).light;
    if (l === 'green') greenCount++; else if (l === 'yellow') yellowCount++; else redCount++;
  }
  // "Mostrar ignorados" toggle off ⇒ drop 🔴 cards from the view entirely.
  if (vbState.hideReds) data = data.filter(b => vbSignal(b).light !== 'red');

  const _qMinEdge = vbState.minEdge ?? 3;
  const _qMaxOdds = vbState.maxOdds ?? 8.0;
  const valueCount = greenCount;   // 🟢 = actionable "bet" picks

  const countEl = document.getElementById('vb-count');
  if (countEl) countEl.innerHTML =
    `<span class="vb-count-value" style="color:${greenCount > 0 ? '#16a34a' : 'var(--text3)'}">🟢 ${greenCount}</span>`
    + ` <span style="color:#d97706">🟡 ${yellowCount}</span> <span style="color:#9aa0ad">🔴 ${redCount}</span>`
    + ` · ${data.length} shown · ${vbState.raw.length} total`;

  // Sound notification — fire when new green (bet) picks appear
  if (_prevValueBetCount >= 0 && valueCount > _prevValueBetCount) playNewBetSound();
  _prevValueBetCount = valueCount;

  // Compact table view
  if (vbState.viewMode === 'table') { renderTableView(data); return; }

  if (data.length === 0) {
    if (vbState.raw.length === 0) {
      wrap.innerHTML = `<div class="vb-empty">
        <div class="vb-empty-title">No live odds yet</div>
        Click "↻ Refresh" to fetch upcoming matches.
      </div>`;
    } else {
      wrap.innerHTML = `<div class="vb-empty">
        <div class="vb-empty-title">No matches match your filters</div>
        Try changing the time window, sport, or mode.
      </div>`;
    }
    return;
  }

  // #8 Daily summary — decide the day in 3 seconds, CLV-first.
  let clvPlus = 0, edgeNoClv = 0;
  for (const b of data) {
    const ev = vbEval(b);
    if (!ev.bestPick) continue;
    const clv = vbClv(b);
    if (clv != null && clv > 0) clvPlus++;
    else if (ev.bestPick.edge_pct >= VB_VALUE_FLOOR && clv != null && clv <= 0) edgeNoClv++;
  }
  const nextEv = data
    .filter(b => b.commence_time && new Date(b.commence_time) > new Date())
    .sort((a, b) => new Date(a.commence_time) - new Date(b.commence_time))[0];
  let nextStr = '';
  if (nextEv) {
    const hrs = (new Date(nextEv.commence_time) - Date.now()) / 3600000;
    const when = hrs < 1 ? '<1h' : hrs < 24 ? `${Math.round(hrs)}h` : `${Math.round(hrs / 24)}d`;
    nextStr = ` · próximo jogo em ${when} (${nextEv.home_team} v ${nextEv.away_team})`;
  }
  let banner;
  if (greenCount > 0) {
    banner = `<div class="vb-daily-summary ds-good"><strong>🟢 ${greenCount} aposta${greenCount === 1 ? '' : 's'} verde${greenCount === 1 ? '' : 's'} hoje</strong>`
      + (yellowCount > 0 ? ` · 🟡 ${yellowCount} para ver` : '')
      + (redCount > 0 ? ` · 🔴 ${redCount} a ignorar` : '')
      + nextStr + `</div>`;
  } else {
    banner = `<div class="vb-daily-summary ds-none"><strong>Sem value hoje</strong> — nenhuma aposta verde.`
      + (yellowCount > 0 ? ` 🟡 ${yellowCount} para vigiar.` : '')
      + nextStr + `</div>`;
  }
  wrap.innerHTML = banner + data.map(b => renderCard(b)).join('');

  // Trigger edge calculation for any pre-filled manual odds inputs
  setTimeout(() => {
    document.querySelectorAll('.vb-manual-field input').forEach(inp => {
      if (inp.value && parseFloat(inp.value) > 1) {
        // Trigger calcManualEdge but suppress the auto-save (already saved)
        const odd = parseFloat(inp.value);
        const prob = parseFloat(inp.dataset.prob);
        const span = inp.parentElement.querySelector('.vb-manual-edge');
        if (!span || !prob) return;
        const edge = (odd * (prob / 100) - 1) * 100;
        const sign = edge > 0 ? '+' : '';
        span.textContent = `${sign}${edge.toFixed(1)}%`;
        span.className = 'vb-manual-edge ' + (edge >= 5 ? 'strong' : edge >= 2 ? 'good' : edge >= 0 ? 'flat' : 'neg');
      }
    });
    // Update "Your Best Value" blocks for all cards
    document.querySelectorAll('.vb-card').forEach(card => {
      const eid = card.dataset.id;
      if (eid && typeof updateYourBestValue === 'function') {
        updateYourBestValue(card, eid);
      }
    });
  }, 50);
}

// Leagues with historically negative backtest ROI — warn (or avoid) value bets there.
const NEGATIVE_ROI_LEAGUES = [
  { match: ['eredivisie'], label: 'Eredivisie', roi: -12.7, severity: 'avoid' },
  { match: ['scottish', 'scotland'], label: 'Scottish', roi: -12.4, severity: 'avoid' },
  { match: ['ligue 1', 'ligue1'], label: 'Ligue 1', roi: -5.3, severity: 'warn' },
];
function negativeRoiLeague(name) {
  const s = (name || '').toLowerCase();
  for (const lg of NEGATIVE_ROI_LEAGUES) {
    if (lg.match.some(m => s.includes(m))) return lg;
  }
  return null;
}

function renderCard(b) {
  const _qMinEdge = vbState.minEdge ?? 3;
  const _qMaxOdds = vbState.maxOdds ?? 8.0;
  const isExpanded = !vbState.collapsed.has(b.event_id);
  const hasAutoH2H = !!(b.x1_home || b.b365_home || b.best_home);

  // ── HARD RULES (single source of truth: vbEval) ───────────────
  // Recommendations come ONLY from real automatic odds and NEVER above the
  // ceiling. Stars/value belong to the Best Pick; below the 3% floor it shows
  // muted/informational, never as a green rec. No more "Safest Pick".
  const ev = vbEval(b);
  const sig = vbSignal(b);   // #3 traffic-light tier (green/yellow/red) for this card
  const ODDS_CEILING = ev.ceiling;
  const realPicks = ev.realPicks;
  const bestPickReal = ev.bestPick;
  const isCelebrated = ev.isValue;
  const hasValue = isCelebrated;

  // ── Negative-ROI league warning (from historical backtest) ─────
  const negLeague = negativeRoiLeague(b.sport_name);
  const negLeagueBanner = negLeague
    ? `<div class="vb-negroi-banner vb-negroi-${negLeague.severity}">
        ${negLeague.severity === 'avoid' ? '❌' : '⚠️'} ${negLeague.label}: historical ROI ${negLeague.roi}%
        — ${negLeague.severity === 'avoid' ? 'avoid betting this league' : 'bet with caution'}
      </div>`
    : '';

  // ── Header chips: CLV is the DOMINANT signal (big, green/red), edge secondary (#3) ──
  let clvChipHtml = '', edgeChipHtml = '';
  if (bestPickReal) {
    const _mineHdr = _myBookPick(b, bestPickReal);
    if (_mineHdr && _mineHdr.edge_pct != null) {
      // Real CLV: your bettable 1xBet price vs the sharp line.
      const clvVal = _mineHdr.edge_pct;
      clvChipHtml = `<span class="vb-clv-chip ${clvVal >= 0 ? 'clv-pos' : 'clv-neg'}" title="Est. CLV — your 1xBet price vs the sharp line (the #1 signal)">${clvVal >= 0 ? '+' : ''}${clvVal.toFixed(1)}% CLV</span>`;
    } else {
      // 1xBet doesn't quote this selection — the edge is at another book, so there
      // is NO 1xBet CLV. Don't label a best-price edge as "CLV".
      clvChipHtml = `<span class="vb-clv-chip clv-neg" title="A 1xBet não cota esta seleção — valor noutra casa, sem CLV para ti">noutra casa</span>`;
    }
    edgeChipHtml = `<span class="edge-chip-sm" title="Best-price edge (pode ser noutra casa)">edge ${bestPickReal.edge_pct >= 0 ? '+' : ''}${bestPickReal.edge_pct.toFixed(1)}%</span>`;
  }

  // ── Compact time string ─────────────────────────────────────────
  const _timeStr = (() => {
    if (!b.commence_time) return '—';
    const diff = (new Date(b.commence_time) - Date.now()) / 3600000;
    if (diff < 0) return 'started';
    if (diff < 24) { const h = Math.floor(diff); const m = Math.floor((diff-h)*60); return `${h}h${m}m`; }
    return new Date(b.commence_time).toLocaleDateString('en-GB', {day:'2-digit', month:'short'});
  })();

  // ── Kelly helper ────────────────────────────────────────────────
  const _br = vbState.bankroll || 0;
  function _calcKelly(edge, odd) {
    const kQ = vbKellyFraction(edge, odd);   // ¼-Kelly + short-odd softening (see vbKellyFraction)
    if (kQ <= 0) return null;
    return _br > 0 ? `€${(_br * kQ).toFixed(0)}` : `${(kQ * 100).toFixed(1)}%`;
  }

  // ── Compact probability line (replaces BetIQ big bar chart) ────
  let probLineHtml = '';
  const bq = b.betiq_probs;
  if (bq && bq.home !== undefined) {
    const agreeIcon = bq.agreement === 'high' ? '✓' : bq.agreement === 'medium' ? '~' : '⚠';
    const agreeCls = 'vb-pagree ' + (bq.agreement === 'high' ? 'agree-high' : bq.agreement === 'medium' ? 'agree-med' : 'agree-low');
    const agreeText = bq.agreement === 'high' ? 'Market confirms' : bq.agreement === 'medium' ? 'Mostly agrees' : 'Models diverge';
    probLineHtml = `<div class="vb-prob-line">
      <span class="vb-pitem">${b.home_team} <strong>${bq.home}%</strong></span>
      ${bq.draw != null ? `<span class="vb-pitem">Draw <strong>${bq.draw}%</strong></span>` : ''}
      <span class="vb-pitem">${b.away_team} <strong>${bq.away}%</strong></span>
      ${hasValue && b.xg_signal ? `<span class="${agreeCls}">${agreeIcon} ${agreeText}</span>` : ''}
    </div>`;
  } else if (b.true_home_pct != null) {
    probLineHtml = `<div class="vb-prob-line">
      <span class="vb-pitem">${b.home_team} <strong>${b.true_home_pct}%</strong></span>
      ${b.true_draw_pct != null ? `<span class="vb-pitem">Draw <strong>${b.true_draw_pct}%</strong></span>` : ''}
      ${b.true_away_pct != null ? `<span class="vb-pitem">${b.away_team} <strong>${b.true_away_pct}%</strong></span>` : ''}
    </div>`;
  }

  // ── CLV badge ───────────────────────────────────────────────────
  let clvBadge = '';
  const trackedBets = trackedBetsMap[b.event_id] || [];
  if (trackedBets.length > 0) {
    const withClv = trackedBets.filter(tb => tb.clv_pct != null);
    if (withClv.length > 0) {
      const best = withClv.reduce((a, x) => Math.abs(x.clv_pct) > Math.abs(a.clv_pct) ? x : a);
      const sign = best.clv_pct >= 0 ? '+' : '';
      const clvColor = best.clv_pct > 0 ? '#34d399' : best.clv_pct < 0 ? '#f87171' : '#8b90a0';
      const label = trackedBets.length > 1 ? `${trackedBets.length} bets · CLV` : 'CLV';
      clvBadge = `<span style="background:${clvColor}18;color:${clvColor};border:1px solid ${clvColor}38;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap">${label} ${sign}${best.clv_pct}%</span>`;
    } else {
      const label = trackedBets.length > 1 ? `${trackedBets.length} bets tracked` : '1 bet tracked';
      clvBadge = `<span style="background:var(--bg2);color:var(--text3);border:1px solid var(--border);padding:2px 9px;border-radius:12px;font-size:11px;white-space:nowrap">${label} · CLV pending</span>`;
    }
  }

  // ── Best Pick + Safest Pick (REAL automatic odds only, ≤ 5.0) ──
  const starsHtmlFor = (n) => Array.from({length:5}, (_,i) =>
    `<span class="vb-star${i >= (n||0) ? ' empty' : ''}">★</span>`).join('');
  const trophyIcon = '<svg class="vb-pick-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 2h12v6c0 3-3 6-6 6s-6-3-6-6V2z"/><path d="M9 18h6v3H9z"/></svg>';
  const refLabel = b.ref_sources || (b.odds_source === 'betfair' ? 'Betfair' : 'Pinnacle');
  // Sharp-consensus badge (A+B): tells the user how trustworthy the fair line is.
  const refChip = (() => {
    const ra = b.ref_agreement;
    const src = b.ref_sources || '';
    if (ra === 'agree')         return `<span class="vb-ref-chip ref-agree">✓ ${src} concordam</span>`;
    if (ra === 'single')        return `<span class="vb-ref-chip ref-single">1 fonte sharp · ${src}</span>`;
    if (ra === 'diverge_sharp') return `<span class="vb-ref-chip ref-diverge">⚠ Sharps divergem${b.ref_max_diff_pp ? ` (${b.ref_max_diff_pp}pp)` : ''} — edge incerto</span>`;
    if (ra === 'diverge_model') return `<span class="vb-ref-chip ref-diverge">⚠ Modelo discorda do mercado — cautela</span>`;
    return '';
  })();

  // BIG hero card — odd is KING (26px+). `celebrated` = clears the green value
  // gate (edge >=3% & odd 1.5-5 & sharps agree). Both tiers show the ¼-Kelly
  // stake (blue) next to the edge AND a Track Bet button; only styling/label differ.
  function renderHeroReal(p, celebrated) {
    const edge = p.edge_pct;
    const eCls = edge >= 3 ? 'pos' : edge >= 1 ? 'flat' : 'neg';
    // Stake = ¼-Kelly sized STRICTLY on the price the user can actually get (1xBet).
    // The big "Best Odd" / "Edge" above may be at another book (informational), but
    // you only win the price YOU bet — so the stake never uses a best-price edge.
    //   • 1xBet doesn't quote this selection  → €0 (value is at another book).
    //   • 1xBet quotes it but edge ≤ 0         → €0 (no value at 1xBet).
    //   • 1xBet odd > 4.0                      → informational only, no stake.
    //   • otherwise                            → ¼-Kelly on the 1xBet edge (short-odd softened).
    const mine = _myBookPick(b, p);
    let kelly = null, stakeState = 'ok';
    if (!mine) {
      stakeState = 'na';                                   // não cotado na 1xBet
    } else if (mine.edge_pct == null || mine.edge_pct <= 0) {
      stakeState = 'zero';                                 // 1xBet sem valor
    } else if (mine.book_odd > VB_GREEN_MAX_ODD) {
      stakeState = 'info';                                 // odd > 4.0 → informativo
    } else {
      kelly = _calcKelly(mine.edge_pct, mine.book_odd);    // ¼-Kelly on YOUR (1xBet) edge
    }
    const fair = p.true_prob ? (100 / p.true_prob).toFixed(2) : null;
    // #4 If this exact pick (same selection) is already tracked in My Bets, swap the
    // Track button for a "Ver em My Bets" link. Persists across reloads because
    // trackedBetsMap is rebuilt from /api/bets (the DB) on every page load.
    const _isTracked = trackedBets.some(tb =>
      (tb.selection || '').trim().toLowerCase() === (p.selection || '').trim().toLowerCase());
    const actionBtn = _isTracked
      ? `<button class="add-bet-btn vb-track-btn is-tracked" onclick='event.stopPropagation();goToMyBets()' title="Já registada — abrir My Bets">✓ Registada · Ver em My Bets</button>`
      : `<button class="add-bet-btn vb-track-btn" onclick='event.stopPropagation();quickAddBet(${JSON.stringify(b).replace(/'/g, "&apos;")})'>+ Track Bet</button>`;
    const cls = celebrated ? 'vb-hero-pick has-value-pick' : 'vb-hero-pick below-floor';
    const label = celebrated ? 'Best Pick' : 'Best available';
    const note = celebrated
      ? (fair ? `Fair odd ${fair} · ★ from ${refLabel} (real market)` : '')
      : (stakeState === 'na'
          ? `Melhor preço está NOUTRA CASA — a 1xBet não cota esta seleção, por isso não tens CLV aqui. Informativo.`
          : b.ref_agreement === 'diverge_sharp'
          ? `Best available — the two sharp markets disagree, so we can't confirm value. Informational only.`
          : stakeState === 'info'
          ? `Best available — odd >4.0 (azarão, menos fiável). Informativo, sem stake sugerida.`
          : `Best available — below the green value gate (edge ≥2% & odd 1.4–4.0). Size with care.`);
    return `<div class="${cls}">
      <div class="vb-pick-label">${trophyIcon}${label}<span class="vb-stars">${starsHtmlFor(ev.stars)}</span></div>
      <div class="vb-pick-selection">${p.selection}</div>
      <div class="vb-pick-market">${p.market}</div>
      <div class="vb-hero-numbers">
        <div class="vb-hero-num-block">
          <span class="vb-hero-big">${fmtOdd(p.book_odd)}</span>
          <span class="vb-hero-lbl">Best Odd${p.best_book ? ' · ' + p.best_book : ''}</span>
        </div>
        <div class="vb-hero-num-block">
          <span class="vb-hero-big edge-${eCls}">${edge >= 0 ? '+' : ''}${edge.toFixed(1)}%</span>
          <span class="vb-hero-lbl">Edge</span>
        </div>
        ${kelly ? `<div class="vb-hero-num-block">
          <span class="vb-hero-big kelly-val">${kelly}</span>
          <span class="vb-hero-lbl">¼ Kelly</span>
        </div>` : (stakeState === 'na' ? `<div class="vb-hero-num-block">
          <span class="vb-hero-big" style="color:#dc2626">€0</span>
          <span class="vb-hero-lbl">n/d na 1xBet</span>
        </div>` : stakeState === 'zero' ? `<div class="vb-hero-num-block">
          <span class="vb-hero-big" style="color:#dc2626">€0</span>
          <span class="vb-hero-lbl">sem valor 1xBet</span>
        </div>` : stakeState === 'info' ? `<div class="vb-hero-num-block">
          <span class="vb-hero-big" style="color:var(--text3)">—</span>
          <span class="vb-hero-lbl">odd >4.0 · info</span>
        </div>` : '')}
        <div class="vb-hero-num-block">
          <span class="vb-hero-big" style="opacity:.85">${p.true_prob != null ? p.true_prob + '%' : '—'}</span>
          <span class="vb-hero-lbl">True prob</span>
        </div>
      </div>
      ${yourBookLine(b, p)}
      ${clvEstLine(b, p)}
      ${lineMoveLine(b, p)}
      ${refChip ? `<div class="vb-ref-row">${refChip}</div>` : ''}
      ${note ? `<div class="vb-hero-model">${note}</div>` : ''}
      <div class="vb-pick-action">
        ${actionBtn}
      </div>
    </div>`;
  }

  let heroBlock;
  if (bestPickReal) {
    heroBlock = renderHeroReal(bestPickReal, isCelebrated);
  } else {
    // Distinguish "we analysed and found no value" from "we had NO odds to analyse".
    // If the event has no bookmaker price at all, the quota/source is down — saying
    // "No value" would be a lie (it implies we looked and the price wasn't worth it).
    const hasAnyPrice = (b.all_picks || []).some(p => p.book_odd && p.book_odd > 1);
    heroBlock = hasAnyPrice
      ? `<div class="vb-hero-pick no-value">
          <div class="vb-pick-label">${trophyIcon}Best Pick</div>
          <div style="color:var(--text3);font-size:13px;padding:10px 0">No value at odds ≤ ${ODDS_CEILING.toFixed(1)} — informational only</div>
        </div>`
      : `<div class="vb-hero-pick no-value">
          <div class="vb-pick-label">${trophyIcon}Best Pick</div>
          <div style="color:#b45309;font-size:13px;padding:10px 0">⚠ Sem odds disponíveis (quota/fonte esgotada) — jogo não analisado, não é "sem valor".</div>
        </div>`;
  }
  const standaloneValueHtml = ''; // removed: Best Pick is always real-odds-derived now

  // ── Market odds bar (always visible — no click needed) ─────────
  const oddsBarHtml = (() => {
    // Best available odd per market+selection (prefer Best book, else highest odd)
    const byKey = {};
    for (const p of (b.all_picks || [])) {
      const key = `${p.market}|||${p.selection}`;
      if (!byKey[key]) { byKey[key] = p; continue; }
      const ex = byKey[key];
      if (p.book === 'Best' && ex.book !== 'Best') byKey[key] = p;
      else if (ex.book !== 'Best' && p.book_odd > ex.book_odd) byKey[key] = p;
    }
    const bestPicks = Object.values(byKey);
    if (!bestPicks.length) return '';
    // Group by market type
    const h2h = bestPicks.filter(p => p.market === 'Match Result');
    const ou = bestPicks.filter(p => p.market && p.market.includes('Over/Under'));
    const btts = bestPicks.filter(p => p.market === 'Both Teams To Score');
    const parts = [];
    if (h2h.length) {
      parts.push(h2h.map(p => {
        const eSign = p.edge_pct > 0 ? '+' : '';
        const within = p.book_odd && p.book_odd <= ODDS_CEILING;
        const eCls = p.edge_pct > 15 ? 'noise' : (within && p.edge_pct >= _qMinEdge && p.edge_pct <= 15) ? 'pos' : p.edge_pct >= 1 ? 'flat' : '';
        const shortSel = p.selection === b.home_team ? (b.home_team.split(' ')[0]) : p.selection === b.away_team ? (b.away_team.split(' ')[0]) : p.selection;
        return `<span class="vb-ob-item${eCls ? ' vc-' + eCls : ''}">
          <span class="vb-ob-sel">${shortSel}</span>
          <span class="vb-ob-odd">${fmtOdd(p.book_odd)}</span>
          ${eCls === 'pos' ? `<span class="vb-ob-edge">${eSign}${p.edge_pct.toFixed(1)}%</span>` : ''}
        </span>`;
      }).join('<span class="vb-ob-div">·</span>'));
    }
    if (ou.length) {
      const ouStr = ou.map(p => {
        const eSign = p.edge_pct > 0 ? '+' : '';
        const within = p.book_odd && p.book_odd <= ODDS_CEILING;
        const eCls = (within && p.edge_pct >= _qMinEdge && p.edge_pct <= 15) ? 'pos' : '';
        const shortSel = p.selection.includes('Over') ? `O${p.selection.replace(/[^0-9.]/g,'')}` : `U${p.selection.replace(/[^0-9.]/g,'')}`;
        return `<span class="vb-ob-item${eCls ? ' vc-pos' : ''}"><span class="vb-ob-sel">${shortSel}</span><span class="vb-ob-odd">${fmtOdd(p.book_odd)}</span>${eCls ? `<span class="vb-ob-edge">${eSign}${p.edge_pct.toFixed(1)}%</span>` : ''}</span>`;
      }).join('<span class="vb-ob-div">·</span>');
      parts.push(ouStr);
    }
    if (btts.length) {
      const bttsStr = btts.map(p => {
        const eSign = p.edge_pct > 0 ? '+' : '';
        const within = p.book_odd && p.book_odd <= ODDS_CEILING;
        const eCls = (within && p.edge_pct >= _qMinEdge && p.edge_pct <= 15) ? 'pos' : '';
        const shortSel = p.selection.includes('Yes') ? 'BTTS Y' : 'BTTS N';
        return `<span class="vb-ob-item${eCls ? ' vc-pos' : ''}"><span class="vb-ob-sel">${shortSel}</span><span class="vb-ob-odd">${fmtOdd(p.book_odd)}</span>${eCls ? `<span class="vb-ob-edge">${eSign}${p.edge_pct.toFixed(1)}%</span>` : ''}</span>`;
      }).join('<span class="vb-ob-div">·</span>');
      parts.push(bttsStr);
    }
    if (!parts.length) return '';
    return `<div class="vb-odds-bar">${parts.join('<span class="vb-ob-sep">│</span>')}</div>`;
  })();

  // All-markets expanded view — with no-vig (fair) odds + Kelly stake
  const allMarketsHTML = (() => {
    const byKey = {};
    for (const p of (b.all_picks || [])) {
      const key = `${p.market}|||${p.selection}`;
      if (!byKey[key]) { byKey[key] = p; continue; }
      const ex = byKey[key];
      if (p.book === 'Best' && ex.book !== 'Best') byKey[key] = p;
      else if (ex.book !== 'Best' && p.book_odd > ex.book_odd) byKey[key] = p;
    }
    const picks = Object.values(byKey).sort((a, b) => b.edge_pct - a.edge_pct);
    if (!picks.length) return '';
    const header = `<div class="vb-markets-header">
      <span class="m-market">Market</span>
      <span class="m-selection">Selection</span>
      <span class="m-odd">Book</span>
      <span class="m-novigo">Fair</span>
      <span class="m-edge">Edge</span>
      <span class="m-kelly">Kelly</span>
      <span class="m-conf">★</span>
    </div>`;
    const _br = vbState.bankroll || 0;
    const rows = picks.map(p => {
      // A selection only counts as a real pick (green edge + stars + Kelly) if its
      // odd is within the ceiling. >ceiling longshots (e.g. Qatar @16.5) are shown
      // for reference but greyed out — never green, never starred.
      const eligible = p.book_odd && p.book_odd > 1 && p.book_odd <= ODDS_CEILING;
      let eCls = 'mute';
      if (!eligible) eCls = 'mute';
      else if (p.edge_pct > 15) eCls = 'noise';
      else if (p.edge_pct >= 3) eCls = 'pos';
      else if (p.edge_pct >= 1) eCls = 'flat';
      else eCls = 'neg';
      const sign = p.edge_pct > 0 ? '+' : '';
      const fairOdd = p.fair_odd
        ? p.fair_odd.toFixed(2)
        : (p.book_odd && p.edge_pct != null ? (p.book_odd / (1 + p.edge_pct / 100)).toFixed(2) : '—');
      let kellyStr = '—';
      if (eligible && p.edge_pct > 0 && p.book_odd > 1) {
        const kQ = ((p.edge_pct / 100) / (p.book_odd - 1)) * 0.25;
        kellyStr = _br > 0 ? `€${(_br * kQ).toFixed(0)}` : `${(kQ * 100).toFixed(1)}%`;
      }
      const starCount = (eligible && p.edge_pct >= VB_VALUE_FLOOR) ? (p.confidence || 0) : 0;
      return `<div class="vb-market-row${eligible ? '' : ' is-muted'}">
        <span class="m-market">${p.market}</span>
        <span class="m-selection">${p.selection}</span>
        <span class="m-odd">${fmtOdd(p.book_odd)}</span>
        <span class="m-novigo">${fairOdd}</span>
        <span class="m-edge ${eCls}">${sign}${p.edge_pct.toFixed(1)}%</span>
        <span class="m-kelly">${kellyStr}</span>
        <span class="m-conf">${starCount ? '★'.repeat(starCount) : '<span class="m-conf-empty">—</span>'}</span>
      </div>`;
    }).join('');
    return header + rows;
  })();

  // xG signal block (only shows when we have xG data for both teams)
  let xgBlock = '';
  if (b.xg_signal) {
    const xg = b.xg_signal;
    // CRITICAL FIX: Use BetIQ unified agreement if available (consistent with header)
    // Falls back to xg_pin_agreement only if BetIQ not present
    let unifiedAgree;
    if (b.betiq_probs && b.betiq_probs.agreement) {
      // Map BetIQ agreement to xG colors
      unifiedAgree = b.betiq_probs.agreement === 'high' ? 'strong' :
                     b.betiq_probs.agreement === 'medium' ? 'moderate' : 'weak';
    } else {
      unifiedAgree = b.xg_pin_agreement;
    }
    const agreeCls = unifiedAgree === 'strong' ? 'xg-agree-strong' :
                     unifiedAgree === 'moderate' ? 'xg-agree-mod' : 'xg-agree-weak';
    const agreeLabel = unifiedAgree === 'strong' ? 'Strong agreement with market' :
                       unifiedAgree === 'moderate' ? 'Moderate agreement' : 'xG disagrees with market';
    xgBlock = `<div class="vb-xg-strip ${agreeCls}">
      <div class="vb-xg-header">
        <span class="vb-xg-label">xG Model · ${agreeLabel}</span>
        <span class="vb-xg-goals">Expected: ${xg.expected_home_goals} – ${xg.expected_away_goals} goals</span>
      </div>
      <div class="vb-xg-probs">
        <div class="vb-xg-prob"><span class="lab">Home</span> <span class="val">${xg.home}%</span></div>
        <div class="vb-xg-prob"><span class="lab">Draw</span> <span class="val">${xg.draw}%</span></div>
        <div class="vb-xg-prob"><span class="lab">Away</span> <span class="val">${xg.away}%</span></div>
        <div class="vb-xg-prob"><span class="lab">O 2.5</span> <span class="val">${xg.over25}%</span></div>
        <div class="vb-xg-prob"><span class="lab">BTTS</span> <span class="val">${xg.btts_yes}%</span></div>
      </div>
    </div>`;
  }

  // Line movement banner & Smart Money detection
  let movementBanner = '';
  let movementSparkline = '';
  if (b.line_movement) {
    const m = b.line_movement;
    const sharpConfirms = b.sharp_confirmation === true;
    const hasSmartMoney = m.smart_money && Object.keys(m.smart_money).length > 0;
    const hasStrongMove = m.has_strong_movement;
    const hasSteam = m.steam && Object.keys(m.steam).length > 0;

    let banners = [];

    // Smart Money — absolute odds shift >= 0.20 (strong) or >= 0.10 (moderate)
    if (hasSmartMoney) {
      for (const [outcome, info] of Object.entries(m.smart_money)) {
        const teamName = outcome === 'home' ? b.home_team : outcome === 'away' ? b.away_team : 'Draw';
        const arrow = info.direction === 'in' ? '↘' : '↗';
        const openOdd = m.opening && m.opening[outcome];
        const latestOdd = m.latest && m.latest[outcome];
        const oddsStr = (openOdd && latestOdd) ? ` (${openOdd.toFixed(2)} → ${latestOdd.toFixed(2)})` : '';
        const absStr = info.abs_change > 0 ? `+${info.abs_change.toFixed(2)}` : info.abs_change.toFixed(2);
        if (info.strength === 'strong') {
          banners.push(`<span class="movement-tag smart-money">📊 Smart Money: ${teamName} ${arrow} ${absStr}${oddsStr}</span>`);
        } else {
          banners.push(`<span class="movement-tag steam">⚡ Movimento: ${teamName} ${arrow} ${absStr}${oddsStr}</span>`);
        }
      }
    }

    // Steam (% based — 6h window)
    if (hasSteam && !hasSmartMoney) {
      for (const [outcome, info] of Object.entries(m.steam)) {
        const teamName = outcome === 'home' ? b.home_team : outcome === 'away' ? b.away_team : 'Draw';
        const arrow = info.direction === 'in' ? '↘' : '↗';
        banners.push(`<span class="movement-tag steam">⚡ Steam: ${teamName} ${arrow} ${Math.abs(info.pct).toFixed(1)}% (6h)</span>`);
      }
    }

    if (sharpConfirms && banners.length > 0) {
      banners.push(`<span class="movement-tag confirm">✓ Confirma o nosso pick</span>`);
    }

    // #7 No "X snapshots — no movement" filler. Only show the movement banner when
    // there's a REAL signal (smart money / steam / sharp confirm), never empty noise.

    if (banners.length > 0) {
      movementBanner = `<div class="vb-movement-banner">${banners.join('')}</div>`;
    }

    // Sparkline
    if (m.sparkline && m.sparkline.length >= 3) {
      const points = m.sparkline.filter(s => s.pin_home).map(s => s.pin_home);
      if (points.length >= 3) {
        const minP = Math.min(...points);
        const maxP = Math.max(...points);
        const range = maxP - minP || 1;
        const w = 80, h = 24;
        const stepX = w / (points.length - 1);
        const path = points.map((v, i) => {
          const x = i * stepX;
          const y = h - ((v - minP) / range) * h;
          return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');
        const trend = points[points.length-1] < points[0] ? 'down' : points[points.length-1] > points[0] ? 'up' : 'flat';
        movementSparkline = `<svg class="spark spark-${trend}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${path}"/></svg>`;
      }
    }
  }

  // Public Bias Detection banner
  let biasBanner = '';
  if (b.public_bias) {
    const pb = b.public_bias;
    const sevCls = pb.severity === 'high' ? 'bias-high' : 'bias-med';
    const gapStr = `+${pb.gap_pct}%`;
    biasBanner = `<div class="vb-bias-banner ${sevCls}">
      <span class="bias-icon">⚠️</span>
      <span class="bias-text">
        <strong>Possível trap público — ${pb.popular_team}</strong>
        Mercado implica ${pb.market_implied}% · Modelo diz ${pb.model_prob}% · Diferença: ${gapStr}
      </span>
    </div>`;
  }

  const cardAgreement = (b.betiq_probs && b.betiq_probs.agreement) || 'high';
  const cardCls = `vb-card ${VB_LIGHT_META[sig.light].cls}${hasValue ? ' has-value' : ''}${isExpanded ? ' expanded' : ''}`;
  const detailsCount = (() => {
    const seen = new Set();
    for (const p of (b.all_picks||[])) seen.add(`${p.market}|||${p.selection}`);
    return seen.size;
  })();

  // ── Build auto-odds HTML (used in collapsed details) ───────────
  const autoOddsHtml = hasAutoH2H ? (() => {
    const hasDraw = b.true_draw_pct != null || (b.betiq_probs && b.betiq_probs.draw != null);
    const cols = [b.home_team, ...(hasDraw ? ['Draw'] : []), b.away_team];
    function aoRow(book, h, d, a) {
      if (!h && !a) return '';
      const vals = [h, ...(hasDraw ? [d] : []), a];
      return `<div class="ao-row">
        <span class="ao-book">${book}</span>
        ${vals.map(v => `<span class="ao-val${v ? '' : ' ao-miss'}">${v ? v.toFixed(2) : '—'}</span>`).join('')}
      </div>`;
    }
    return `<div class="vb-auto-odds">
      <div class="ao-header">
        <span class="ao-title">📊 Odds automáticas</span>
        <div class="ao-cols">${cols.map(c => `<span>${c}</span>`).join('')}</div>
      </div>
      ${aoRow('1xBet', b.x1_home, b.x1_draw, b.x1_away)}
      ${aoRow('Bet365', b.b365_home, b.b365_draw, b.b365_away)}
      ${aoRow('🏆 Best', b.best_home, b.best_draw, b.best_away)}
    </div>`;
  })() : '';


  return `<div class="${cardCls}" data-id="${b.event_id}" data-agreement="${cardAgreement}">
    <div class="vb-card-head">
      <div class="vb-card-meta">
        <span class="vb-signal ${VB_LIGHT_META[sig.light].cls}" title="${sig.light === 'green' ? 'APOSTAR — bom value no preço da 1xBet' : sig.light === 'yellow' ? 'VER — value marginal, vigia a linha' : 'IGNORAR — sem value suficiente na 1xBet'}">${VB_LIGHT_META[sig.light].dot} ${VB_LIGHT_META[sig.light].label}</span>
        <span class="vb-sport">${b.sport_name || ''}</span>${tennisSurfaceBadge(b)}
        <div class="vb-head-right">
          ${clvChipHtml}${edgeChipHtml}
          <span class="vb-time">${_timeStr}</span>
        </div>
      </div>
      <div class="vb-match-name" onclick="window.location.href='/match/${b.event_id}'" style="cursor:pointer; transition:color 0.2s" onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color=''">
        ${b.home_team} <span class="vs">vs</span> ${b.away_team}
        ${clvBadge ? `<span class="vb-clv-inline">${clvBadge}</span>` : ''}
      </div>
      ${(() => {
        const t = timingBadge(b), lq = liquidityBadge(b), oa = oddsAgeLine(b);
        return (t || lq || oa) ? `<div class="vb-meta-row">${t}${lq}${oa}</div>` : '';
      })()}
    </div>
    ${negLeagueBanner}
    ${movementBanner}
    ${biasBanner}
    ${probLineHtml}
    ${standaloneValueHtml}
    ${heroBlock}
    ${oddsBarHtml}
    <div class="vb-your-value vb-pick-block best-value has-value" style="display:none"></div>
    <div class="vb-card-foot">
      <span class="all-markets-link" onclick="toggleExpand('${b.event_id}')">
        ${isExpanded ? '▾ Hide' : '▸ Show'} xG · Smart Money · Full Markets (${detailsCount})
      </span>
    </div>
    <div class="vb-all-markets">${isExpanded ? (xgBlock + autoOddsHtml + allMarketsHTML) : ''}</div>
  </div>`;
}

const _manualOddDebounce = {};

function calcManualEdge(input) {
  const odd = parseFloat(input.value);
  const prob = parseFloat(input.dataset.prob); // our true prob %
  const selection = input.dataset.selection;
  const eventId = input.dataset.eventId;
  const span = input.parentElement.querySelector('.vb-manual-edge');
  if (!span) return;

  if (!odd || odd <= 1 || !prob) {
    span.textContent = '';
    span.className = 'vb-manual-edge';
    // Auto-delete from server (debounced)
    if (selection && eventId) {
      _scheduleSaveManualOdd(eventId, selection, null);
    }
    return;
  }

  // Core calculations
  const trueProbFraction = prob / 100;
  const edge = (odd * trueProbFraction - 1) * 100;
  const kellyFull = edge > 0 ? (edge / 100) / (odd - 1) : 0;
  const kellyQuarter = kellyFull * 0.25;
  const sign = edge > 0 ? '+' : '';

  // Show edge on the input span
  span.textContent = `${sign}${edge.toFixed(1)}%`;
  if (edge >= 5) {
    span.className = 'vb-manual-edge strong';
    span.title = `Strong value! ¼ Kelly: ${(kellyQuarter*100).toFixed(1)}% of bankroll`;
  } else if (edge >= 2) {
    span.className = 'vb-manual-edge good';
    span.title = `Good value. ¼ Kelly: ${(kellyQuarter*100).toFixed(1)}% of bankroll`;
  } else if (edge >= 0) {
    span.className = 'vb-manual-edge flat';
    span.title = 'Marginal — no significant edge';
  } else {
    span.className = 'vb-manual-edge neg';
    span.title = `No value. True price: ${(1/trueProbFraction).toFixed(2)}`;
  }

  // Auto-save to server (debounced 500ms)
  if (selection && eventId) {
    _scheduleSaveManualOdd(eventId, selection, odd);
  }

  // Update "Your Best Value" block (separate from Best Pick / Safest Pick)
  const card = input.closest('.vb-card');
  if (!card) return;
  updateYourBestValue(card, eventId);
}

function _scheduleSaveManualOdd(eventId, selection, odd) {
  const key = `${eventId}|${selection}`;
  if (_manualOddDebounce[key]) clearTimeout(_manualOddDebounce[key]);
  _manualOddDebounce[key] = setTimeout(() => {
    fetch('/api/manual-odds', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({event_id: eventId, selection: selection, odd: odd})
    }).catch(e => console.warn('Save failed:', e));
  }, 500);
}

function updateYourBestValue(card, eventId) {
  // Collect all manual inputs and compute best edge
  const inputs = card.querySelectorAll('.vb-manual-field input');
  let bestEdge = null, bestOdd = null, bestProb = null, bestLabel = null, bestKelly = 0, bestMarket = null;
  let filterReason = null;

  inputs.forEach(inp => {
    const odd = parseFloat(inp.value);
    const prob = parseFloat(inp.dataset.prob);
    if (!odd || odd <= 1 || !prob) return;
    const edge = (odd * (prob/100) - 1) * 100;

    // Get agreement level from card data attribute (set at render time)
    const card = inp.closest('.vb-card');
    const agreement = card ? (card.dataset.agreement || 'high') : 'high';
    let maxAllowedEdge = agreement === 'low' ? 5 : 12;

    // HARD RULES: edge must be between 2% and 15%
    if (edge < 2) {
      if (bestEdge === null) filterReason = "Edge <2% (no value)";
      return;
    }
    if (edge > 15) {
      if (bestEdge === null) filterReason = "Edge >15% (model error)";
      return;
    }

    // Soft filter: edge above agreement threshold (but still 2-15%)
    if (edge > maxAllowedEdge) {
      if (bestEdge === null) filterReason = `Edge ${edge.toFixed(1)}% exceeds safe threshold for ${agreement === 'low' ? 'diverging' : 'agreeing'} models`;
      return;
    }

    // Still prefer best edge, but only if it passes ALL filters
    if (bestEdge === null || edge > bestEdge) {
      bestEdge = edge;
      bestOdd = odd;
      bestProb = prob;
      bestKelly = ((edge/100) / (odd - 1)) * 0.25;
      const labelEl = inp.parentElement.querySelector('label');
      bestLabel = labelEl ? labelEl.textContent : '';
      bestMarket = inp.dataset.market || '';
      filterReason = null;
    }
  });

  const block = card.querySelector('.vb-your-value');
  if (!block) return;

  // If edge was filtered out, show reason
  if (bestEdge === null && filterReason) {
    block.style.display = '';
    block.classList.add('filtered-out');
    block.innerHTML = `
      <div class="vb-pick-label" style="color: #999;">
        <svg class="vb-pick-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>
        No valid value bet
      </div>
      <div style="font-size: 12px; color: #999; padding: 8px 0;">⚠️ ${filterReason}</div>
    `;
    return;
  }

  if (bestEdge === null) {
    block.style.display = 'none';
    return;
  }

  block.style.display = '';
  block.classList.remove('filtered-out');
  const hasValue = bestEdge >= 2;
  const sign = bestEdge > 0 ? '+' : '';
  const cls = bestEdge >= 5 ? 'edge-pos strong' : bestEdge >= 2 ? 'edge-pos' : bestEdge >= 0 ? 'edge-flat' : 'edge-neg';
  const bankroll = vbState.bankroll || 0;

  // Calculate confidence stars
  let stars = 0;
  if (bestEdge >= 5 && bestEdge <= 12) stars = 3;
  else if (bestEdge >= 2 && bestEdge < 5) stars = 2;
  else if (bestEdge >= 0) stars = 1;
  else stars = 0;

  if (bestEdge > 0) {
    if (bestProb >= 60) stars += 2;
    else if (bestProb >= 50) stars += 1;
  }

  const agreement2 = card.dataset.agreement || 'high';
  if (agreement2 === 'low') stars = Math.min(2, stars);
  else if (agreement2 !== 'high') stars = Math.min(4, stars);

  stars = Math.max(0, Math.min(5, stars));

  const starsHtml = Array.from({length:5}, (_,i) => `<span class="vb-star${i >= stars ? ' empty' : ''}">★</span>`).join('');
  const stakeHtml = hasValue && bankroll > 0
    ? `<div class="vb-kelly"><span>¼ Kelly stake</span><span class="stake">€${(bankroll * bestKelly).toFixed(2)}</span></div>`
    : (hasValue ? `<div class="vb-kelly"><span>¼ Kelly</span><span class="stake zero">${(bestKelly*100).toFixed(2)}%</span></div>` : '');

  block.innerHTML = `
    <div class="vb-pick-label">
      <svg class="vb-pick-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>
      Your Best Value
      <span class="vb-stars">${starsHtml}</span>
    </div>
    <div class="vb-pick-selection">${bestLabel}</div>
    <div class="vb-pick-market">${bestMarket || 'Match Result'} · ${bestProb.toFixed(1)}% probability</div>
    <div class="vb-pick-row">
      <span class="label">1xBet</span>
      <span class="val">${bestOdd.toFixed(2)}</span>
      <span class="val ${cls}">${sign}${bestEdge.toFixed(1)}%</span>
    </div>
    ${stakeHtml}
  `;
  block.classList.toggle('has-value', hasValue);
}

function calcConfidenceFromEdge(edgePct, agreementLevel, nSignals) {
  // 1-5 stars based on: edge sweet spot + model agreement + number of signals
  if (edgePct === null || edgePct < 0) return 0;
  let score = 0;
  // Edge quality
  if (edgePct >= 5 && edgePct <= 12)     score += 3; // sweet spot
  else if (edgePct >= 2 && edgePct < 5)  score += 2;
  else if (edgePct > 12 && edgePct <= 20) score += 1;
  else if (edgePct > 20)                  score += 0; // likely noise
  else                                    score += 1; // positive but small
  // Model agreement
  if (agreementLevel === 'high')          score += 2;
  else if (agreementLevel === 'medium')   score += 1;
  // Number of independent signals
  if (nSignals >= 3)                      score += 1;
  else if (nSignals === 2)                score += 1;
  return Math.max(1, Math.min(5, score));
}

function updateCardFromManualOdds(card, eventId) {
  // Deprecated — replaced by updateYourBestValue (kept as no-op for safety)
}

function toggleExpand(eventId) {
  if (vbState.collapsed.has(eventId)) {
    vbState.collapsed.delete(eventId);
  } else {
    vbState.collapsed.add(eventId);
  }
  renderValueBets();
}

function quickAddBet(b) {
  // Pre-fill bet form with the SAME pick shown on the green card (eligible best
  // pick, real odds within the ceiling). Fall back to backend best_value only if
  // there is no eligible pick (shouldn't happen — Track only shows when there is).
  if (!b) return;
  const pick = vbEval(b).bestPick || b.best_value;
  if (!pick) return;
  // The user bets at 1xBet — record THEIR price + edge (so P&L and the realized CLV
  // are computed against what they actually got), falling back to the best-price pick
  // only if 1xBet doesn't quote this selection.
  const mine = _myBookPick(b, pick);
  const useOdd  = (mine && mine.book_odd) ? mine.book_odd : pick.book_odd;
  const useBook = (mine && mine.book_odd) ? '1xBet' : (pick.book || '1xBet');
  const useEdge = (mine && mine.edge_pct != null) ? mine.edge_pct : pick.edge_pct;
  document.querySelector('[data-section="mybets"]').click();
  setTimeout(() => {
    const f = document.getElementById('bet-form');
    if (f && f.style.display === 'none') toggleBetForm();
    document.getElementById('bet-sport').value = b.sport_name || '';
    document.getElementById('bet-home').value = b.home_team || '';
    document.getElementById('bet-away').value = b.away_team || '';
    if (b.commence_time) {
      const d = new Date(b.commence_time);
      const iso = new Date(d.getTime() - d.getTimezoneOffset()*60000).toISOString().slice(0,16);
      document.getElementById('bet-commence').value = iso;
    }
    document.getElementById('bet-selection').value = pick.selection || '';
    document.getElementById('bet-odds').value = useOdd || '';
    document.getElementById('bet-bookmaker').value = useBook;
    const stakeField = document.getElementById('bet-stake');
    const kellyQ = (useEdge > 0 && useOdd > 1)
      ? (useEdge / 100) / (useOdd - 1) * 0.25 : (pick.kelly_pct || 0) / 100;
    if (vbState.bankroll > 0 && kellyQ > 0 && !stakeField.value) {
      stakeField.value = (vbState.bankroll * kellyQ).toFixed(2);
    }
    // Store edge for later analysis (the 1xBet edge — your real one)
    const edgeField = document.getElementById('bet-edge-pct');
    if (edgeField) edgeField.value = (useEdge != null ? useEdge : '');
    // CRITICAL for realized CLV: stash event_id (+ pin true-prob) on the form so the
    // closing-line capture can link this bet to odds_history. The match dropdown is
    // slow to populate (it fetches /api/value-bets), so we DON'T rely on it — submitBet
    // reads these dataset fields as the source of truth.
    if (f) {
      f.dataset.eventId = b.event_id || '';
      let pinImp = null;
      if (pick.selection === b.home_team) pinImp = b.true_home_pct;
      else if (pick.selection === b.away_team) pinImp = b.true_away_pct;
      else if (pick.selection === 'Draw') pinImp = b.true_draw_pct;
      f.dataset.pinImplied = (pinImp != null ? String(pinImp) : '');
    }
    const mkSel = document.getElementById('bet-market');
    if (mkSel) mkSel.value = /over|under/i.test(pick.market || '') ? 'over_under' : 'h2h';
  }, 100);
}

// #4 Open the My Bets page (used by the "✓ Registada · Ver em My Bets" button).
function goToMyBets() {
  const link = document.querySelector('[data-section="mybets"]');
  if (link) link.click();
}

let oddsPoller = null;
async function refreshOdds() {
  const btn = document.getElementById('refresh-odds-btn');
  const box = document.getElementById('odds-refresh-status');
  const log = document.getElementById('odds-refresh-log');
  btn.disabled = true;
  btn.textContent = '↻ Refreshing...';
  box.style.display = 'block';
  log.innerHTML = '<div style="color:var(--text3)">Fetching live odds...</div>';
  try {
    const resp = await fetch('/api/odds/refresh', { method: 'POST' });
    const data = await resp.json();
    if (!data.ok) { log.innerHTML = `<div style="color:var(--amber)">${data.msg}</div>`; btn.disabled=false; btn.textContent='↻ Refresh'; return; }
    if (oddsPoller) clearInterval(oddsPoller);
    let oddsPollCount = 0;
    oddsPoller = setInterval(async () => {
      oddsPollCount++;
      if (oddsPollCount > 90) { // 3-minute timeout for odds refresh
        clearInterval(oddsPoller); oddsPoller = null;
        btn.disabled = false; btn.textContent = '↻ Refresh';
        log.innerHTML += '<div style="color:var(--amber)">Timed out — try again</div>';
        return;
      }
      const s = await fetchJSON('/api/collection/status');
      if (!s) return;
      log.innerHTML = s.messages.map(m => {
        const c = m.startsWith('✓') ? 'var(--green)' : m.includes('ERROR') ? 'var(--red)' : 'var(--text3)';
        return `<div style="color:${c}">${m}</div>`;
      }).join('');
      log.scrollTop = log.scrollHeight;
      if (!s.running) {
        clearInterval(oddsPoller);
        btn.disabled = false;
        btn.textContent = '↻ Refresh';
        edgeData = [];
        loadEdge();
        loadStats();
      }
    }, 2000);
  } catch(e) {
    log.innerHTML = `<div style="color:var(--red)">Error: ${e}</div>`;
    btn.disabled = false; btn.textContent = '↻ Refresh';
  }
}


// ===========================================================
// MY BETS — Bet Tracker
// ===========================================================

async function loadMyBets() {
  await Promise.all([loadBetStats(), loadBetsTable(), populateEventDropdown()]);
}

async function loadBetStats() {
  const s = await fetchJSON('/api/bet-stats');
  if (!s) return;
  document.getElementById('bs-total').textContent = `${s.n_settled + s.n_pending} (${s.n_pending} pending)`;
  document.getElementById('bs-staked').textContent = '€' + (s.total_staked || 0).toFixed(2);
  const profitEl = document.getElementById('bs-profit');
  profitEl.textContent = (s.total_profit >= 0 ? '+' : '') + '€' + s.total_profit.toFixed(2);
  profitEl.style.color = s.total_profit > 0 ? 'var(--green)' : s.total_profit < 0 ? 'var(--red)' : '';
  const roiEl = document.getElementById('bs-roi');
  roiEl.textContent = (s.roi >= 0 ? '+' : '') + s.roi + '%';
  roiEl.style.color = s.roi > 0 ? 'var(--green)' : s.roi < 0 ? 'var(--red)' : '';
  document.getElementById('bs-winrate').textContent = s.n_settled > 0 ? s.win_rate + '%' : '—';

  const clvEl = document.getElementById('bs-clv');
  if (s.avg_clv !== null && s.clv_sample > 0) {
    clvEl.innerHTML = `${s.avg_clv >= 0 ? '+' : ''}${s.avg_clv}%
      <div style="font-size:10px;color:var(--text3);font-weight:400;margin-top:2px;font-family:var(--font)">
        ${s.clv_sample} bets · ${s.positive_clv_rate || 0}% positive
      </div>`;
    clvEl.style.color = s.avg_clv > 0 ? 'var(--green)' : s.avg_clv < 0 ? 'var(--red)' : '';
  } else {
    clvEl.innerHTML = `—
      <div style="font-size:10px;color:var(--text3);font-weight:400;margin-top:2px;font-family:var(--font)">
        ${s.n_pending > 0 ? 'After matches complete' : 'No data yet'}
      </div>`;
    clvEl.style.color = '';
  }
}

// #6 Reality check: the model assigned each bet an EDGE (its EV%). The average
// edge of your settled bets is the model's expected ROI. Compare to what you
// actually made, and — most importantly — to your CLV, which is the real judge
// of whether the edge was genuine (short-term ROI is mostly variance).
function renderRealityCheck(bets) {
  const exEl = document.getElementById('bs-expected');
  const vEl = document.getElementById('bs-verdict');
  if (!exEl || !vEl) return;
  const settled = bets.filter(b => b.status === 'settled');
  const staked = settled.reduce((s, b) => s + (b.stake || 0), 0);
  const profit = settled.reduce((s, b) => s + (b.profit || 0), 0);
  const realizedRoi = staked > 0 ? profit / staked * 100 : null;
  const edges = settled.filter(b => b.edge_pct != null).map(b => b.edge_pct);
  const expectedRoi = edges.length ? edges.reduce((a, b) => a + b, 0) / edges.length : null;
  const clvs = bets.filter(b => b.clv_pct != null).map(b => b.clv_pct);
  const avgClv = clvs.length ? clvs.reduce((a, b) => a + b, 0) / clvs.length : null;

  exEl.textContent = expectedRoi != null ? (expectedRoi >= 0 ? '+' : '') + expectedRoi.toFixed(1) + '%' : '—';

  if (realizedRoi == null || expectedRoi == null || settled.length < 5) {
    vEl.innerHTML = `<strong>📊 Reality check:</strong> need ≥5 settled bets to compare against the model (${settled.length} so far). The model's expectation = the average edge it gave your bets.`;
    return;
  }
  const clvTxt = avgClv != null ? ` · avg CLV <strong>${avgClv >= 0 ? '+' : ''}${avgClv.toFixed(1)}%</strong>` : '';
  let verdict, color;
  if (avgClv != null && avgClv > 0.5) { verdict = "You're beating the closing line — the edge is real ✅"; color = '#16a34a'; }
  else if (avgClv != null && avgClv < -0.5) { verdict = 'Negative CLV — the edge isn\'t holding to closing; tighten filters / get sharper odds ⚠'; color = '#d97706'; }
  else if (Math.abs(realizedRoi - expectedRoi) <= 3) { verdict = 'Tracking the model within normal variance'; color = 'var(--text2)'; }
  else if (realizedRoi < expectedRoi) { verdict = 'Below model — likely variance; CLV is the final judge'; color = '#d97706'; }
  else { verdict = 'Above model — positive variance/luck'; color = '#16a34a'; }
  vEl.innerHTML = `<strong>📊 Reality check:</strong> real <strong>${realizedRoi >= 0 ? '+' : ''}${realizedRoi.toFixed(1)}%</strong> vs model expected <strong>${expectedRoi >= 0 ? '+' : ''}${expectedRoi.toFixed(1)}%</strong>${clvTxt}. <span style="color:${color}">${verdict}</span>`;
}

// #8 Your real performance broken down by league/competition (from settled bets).
// Early on, CLV is the trustworthy signal; ROI/win-rate need volume. This is what
// we'll use later to favour/avoid leagues with YOUR own data (not the backtest's).
function renderBetBreakdown(bets) {
  const el = document.getElementById('bet-breakdown');
  if (!el) return;
  const settled = (bets || []).filter(b => b.status === 'settled');
  if (settled.length < 3) { el.innerHTML = ''; return; }
  const groups = {};
  for (const b of settled) {
    const key = b.sport_name || 'Unknown';
    const g = groups[key] || (groups[key] = { bets: 0, wins: 0, staked: 0, profit: 0, clvSum: 0, clvN: 0 });
    g.bets++;
    if (b.result === 'won') g.wins++;
    g.staked += b.stake || 0;
    g.profit += b.profit || 0;
    if (b.clv_pct != null) { g.clvSum += b.clv_pct; g.clvN++; }
  }
  const rows = Object.entries(groups).map(([k, g]) => ({
    league: k, bets: g.bets,
    win: g.bets ? g.wins / g.bets * 100 : 0,
    roi: g.staked > 0 ? g.profit / g.staked * 100 : 0,
    clv: g.clvN ? g.clvSum / g.clvN : null,
  })).sort((a, b) => b.bets - a.bets);
  el.innerHTML = `<div class="card" style="margin-top:1rem">
    <div class="card-head">📊 Your performance by league / competition</div>
    <p class="muted-text" style="font-size:12px;margin:0 0 8px">From your settled bets. Early on, <strong>CLV is the reliable signal</strong> (ROI/win-rate need volume). This will guide which leagues to favour or avoid with <em>your own</em> data later.</p>
    <table><thead><tr><th>League</th><th style="text-align:right">Bets</th><th style="text-align:right">Win%</th><th style="text-align:right">ROI</th><th style="text-align:right">Avg CLV</th></tr></thead><tbody>`
    + rows.map(r => `<tr>
        <td>${r.league}</td>
        <td style="text-align:right" class="mono">${r.bets}</td>
        <td style="text-align:right" class="mono">${r.win.toFixed(0)}%</td>
        <td style="text-align:right;color:${r.roi >= 0 ? 'var(--green)' : 'var(--red)'}">${r.roi >= 0 ? '+' : ''}${r.roi.toFixed(1)}%</td>
        <td style="text-align:right;color:${r.clv == null ? 'var(--text3)' : r.clv >= 0 ? 'var(--green)' : 'var(--red)'}">${r.clv == null ? '—' : (r.clv >= 0 ? '+' : '') + r.clv.toFixed(1) + '%'}</td>
      </tr>`).join('')
    + `</tbody></table></div>`;
}

async function loadBetsTable() {
  const bets = await fetchJSON('/api/bets');
  const wrap = document.getElementById('bets-table-wrap');
  renderRealityCheck(bets || []);   // #6: real performance vs the model's expectation
  renderBetBreakdown(bets || []);   // #8: performance by league/competition
  if (!bets || bets.length === 0) {
    wrap.innerHTML = '<div style="padding:2rem 1.25rem;color:#555b6e;font-size:13px">No bets yet. Click "+ New bet" to register your first one.</div>';
    return;
  }
  const pendingCount = bets.filter(b => b.status === 'pending').length;
  const autoGradeBtn = pendingCount > 0
    ? `<div style="margin-bottom:0.75rem;display:flex;align-items:center;gap:0.75rem">
         <button class="btn-refresh" id="auto-grade-btn" onclick="autoGrade()">🔄 Auto-grade (${pendingCount} pending)</button>
         <span style="font-size:12px;color:var(--text3)">Checks match results and settles bets automatically</span>
       </div>`
    : '';

  wrap.innerHTML = autoGradeBtn + `<table>
    <thead><tr>
      <th>Placed</th><th>Match</th><th>Selection</th><th>Book</th>
      <th>Odds</th><th>Edge</th><th>Stake</th><th>Status</th><th>P/L</th><th>CLV</th><th></th>
    </tr></thead>
    <tbody>` +
    bets.map(b => {
      const match = (b.home_team && b.away_team) ? `${b.home_team} v ${b.away_team}` : '—';
      const placed = b.placed_at ? b.placed_at.slice(5,16) : '';
      const statusTxt = b.status === 'pending' ? 'PENDING' : (b.result || '').toUpperCase();
      const statusCls = 'bet-status-' + (b.status === 'pending' ? 'pending' : (b.result || ''));
      const profit = b.profit != null ? (b.profit >= 0 ? '+' : '') + '€' + b.profit.toFixed(2) : '—';
      const profitCol = b.profit > 0 ? 'var(--green)' : b.profit < 0 ? 'var(--red)' : 'var(--text3)';
      const clv = b.clv_pct != null ? (b.clv_pct >= 0 ? '+' : '') + b.clv_pct + '%' : '—';
      const clvCol = b.clv_pct > 0 ? 'var(--green)' : b.clv_pct < 0 ? 'var(--red)' : 'var(--text3)';
      const edgeStr = b.edge_pct != null ? (b.edge_pct >= 0 ? '+' : '') + b.edge_pct.toFixed(1) + '%' : '—';
      const edgeCol = b.edge_pct > 0 ? 'var(--green)' : 'var(--text3)';
      const actions = b.status === 'pending'
        ? `<div class="bet-actions">
             <button class="btn-won" onclick="settleBet(${b.id},'won')">W</button>
             <button class="btn-lost" onclick="settleBet(${b.id},'lost')">L</button>
             <button onclick="settleBet(${b.id},'push')">P</button>
             <button class="btn-delete" onclick="deleteBet(${b.id})">×</button>
           </div>`
        : `<div class="bet-actions"><button class="btn-delete" onclick="deleteBet(${b.id})">×</button></div>`;
      return `<tr>
        <td class="mono">${placed}</td>
        <td>${match}<div style="font-size:11px;color:var(--text3)">${b.sport_name||''}</div></td>
        <td class="strong">${b.selection || ''}</td>
        <td style="font-size:12px;color:var(--text3)">${b.bookmaker || '—'}</td>
        <td class="mono">${b.odds ? b.odds.toFixed(2) : '—'}</td>
        <td class="mono" style="color:${edgeCol}">${edgeStr}</td>
        <td class="mono">€${(b.stake||0).toFixed(2)}</td>
        <td><span class="${statusCls}">${statusTxt}</span></td>
        <td class="mono" style="color:${profitCol}">${profit}</td>
        <td class="mono" style="color:${clvCol}">${clv}</td>
        <td>${actions}</td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

async function populateEventDropdown() {
  // Populate the match dropdown with current value bets so user can pick instead of typing
  const events = await fetchJSON('/api/value-bets');
  const sel = document.getElementById('bet-event-select');
  if (!sel) return;
  sel.innerHTML = '<option value="">— pick a match or enter manually below —</option>';
  if (!events || events.length === 0) return;
  events.slice(0, 50).forEach(e => {
    const dt = e.commence_time ? new Date(e.commence_time).toLocaleString('en-GB',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}) : '';
    const opt = document.createElement('option');
    opt.value = e.event_id;
    opt.dataset.event = JSON.stringify(e);
    opt.textContent = `${dt} — ${e.home_team} v ${e.away_team} (${e.sport_name})`;
    sel.appendChild(opt);
  });
  sel.onchange = () => {
    const opt = sel.options[sel.selectedIndex];
    if (!opt.dataset.event) return;
    const ev = JSON.parse(opt.dataset.event);
    document.getElementById('bet-sport').value = ev.sport_name || '';
    document.getElementById('bet-home').value = ev.home_team || '';
    document.getElementById('bet-away').value = ev.away_team || '';
    if (ev.commence_time) {
      const d = new Date(ev.commence_time);
      const iso = new Date(d.getTime() - d.getTimezoneOffset()*60000).toISOString().slice(0,16);
      document.getElementById('bet-commence').value = iso;
    }
    document.getElementById('bet-selection').value = ev.home_team || '';
  };
}

function toggleBetForm() {
  const f = document.getElementById('bet-form');
  f.style.display = f.style.display === 'none' ? 'block' : 'none';
}

async function submitBet() {
  const sel = document.getElementById('bet-event-select');
  const betForm = document.getElementById('bet-form');
  // event_id from the dropdown if chosen, else from what Track Bet stashed on the form
  // (so the bet links to odds_history and realized CLV can be captured).
  const eventId = sel.value || (betForm && betForm.dataset.eventId) || null;
  let pinImplied = null;
  if (sel.value) {
    const opt = sel.options[sel.selectedIndex];
    if (opt && opt.dataset.event) {
      const ev = JSON.parse(opt.dataset.event);
      const selection = document.getElementById('bet-selection').value.trim().toLowerCase();
      if (selection === (ev.home_team||'').toLowerCase()) pinImplied = ev.true_home_pct;
      else if (selection === (ev.away_team||'').toLowerCase()) pinImplied = ev.true_away_pct;
      else if (selection === 'draw') pinImplied = ev.true_draw_pct;
    }
  } else if (betForm && betForm.dataset.pinImplied) {
    pinImplied = parseFloat(betForm.dataset.pinImplied) || null;
  }
  const data = {
    event_id: eventId,
    sport_name: document.getElementById('bet-sport').value.trim() || null,
    home_team: document.getElementById('bet-home').value.trim() || null,
    away_team: document.getElementById('bet-away').value.trim() || null,
    commence_time: document.getElementById('bet-commence').value || null,
    market: document.getElementById('bet-market').value,
    selection: document.getElementById('bet-selection').value.trim(),
    bookmaker: document.getElementById('bet-bookmaker').value.trim() || null,
    odds: parseFloat(document.getElementById('bet-odds').value),
    stake: parseFloat(document.getElementById('bet-stake').value),
    pin_implied_prob: pinImplied,
    notes: document.getElementById('bet-notes').value.trim() || null,
    edge_pct: parseFloat(document.getElementById('bet-edge-pct')?.value) || null,
  };
  if (!data.selection || !data.odds || !data.stake) {
    alert('Please fill in at least Selection, Odds, and Stake.');
    return;
  }
  const resp = await fetch('/api/bets', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)
  });
  const result = await resp.json();
  if (result.ok) {
    // Clear form
    ['bet-sport','bet-home','bet-away','bet-commence','bet-selection','bet-odds','bet-stake','bet-notes','bet-edge-pct'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
    document.getElementById('bet-event-select').value = '';
    if (betForm) { betForm.dataset.eventId = ''; betForm.dataset.pinImplied = ''; }
    // #4 Reflect the new bet on the value-bets cards right away (also DB-persistent:
    // a reload rebuilds trackedBetsMap from /api/bets, so the state survives reloads).
    if (data.event_id) {
      (trackedBetsMap[data.event_id] = trackedBetsMap[data.event_id] || []).push(
        { event_id: data.event_id, selection: data.selection, market: data.market });
      if (typeof renderValueBets === 'function') renderValueBets();
    }
    toggleBetForm();
    loadMyBets();
  }
}

async function autoGrade() {
  const btn = document.getElementById('auto-grade-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Checking results...'; }
  try {
    const r = await fetch('/api/bets/auto-grade', { method: 'POST' });
    const data = await r.json();
    if (data.graded > 0) {
      loadMyBets();
    } else {
      if (btn) { btn.disabled = false; btn.textContent = `🔄 Auto-grade (no matches found yet)`; }
    }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '🔄 Auto-grade (error)'; }
  }
}

async function settleBet(id, result) {
  const resp = await fetch(`/api/bets/${id}/settle`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({result})
  });
  if ((await resp.json()).ok) loadMyBets();
}

async function deleteBet(id) {
  if (!confirm('Delete this bet?')) return;
  await fetch(`/api/bets/${id}`, {method: 'DELETE'});
  loadMyBets();
}

// ===========================================================
// NATIONAL TEAMS
// ===========================================================

async function loadNational() {
  const [summary, recent] = await Promise.all([
    fetchJSON('/api/national/summary'),
    fetchJSON('/api/national/recent?limit=100')
  ]);
  renderNationalSummary(summary || []);
  nationalData = recent || [];
  renderNationalTable(nationalData);
  // Wire filter
  const search = document.getElementById('national-search');
  if (search && !search.dataset.wired) {
    search.dataset.wired = '1';
    search.addEventListener('input', async (e) => {
      const q = e.target.value.trim();
      const url = q ? `/api/national/recent?limit=100&team=${encodeURIComponent(q)}` : '/api/national/recent?limit=100';
      nationalData = await fetchJSON(url) || [];
      renderNationalTable(nationalData);
    });
  }
}

function renderNationalSummary(rows) {
  const wrap = document.getElementById('national-summary-wrap');
  if (!rows || rows.length === 0) {
    wrap.innerHTML = '<div style="padding:1rem 1.25rem;color:#555b6e;font-size:13px">No data yet — run collector</div>';
    return;
  }
  wrap.innerHTML = '<table><tbody>' +
    rows.map(r => `<tr>
      <td class="strong">${r.tournament}</td>
      <td class="mono" style="color:#8b90a0">${(r.first_date||'').slice(0,7)} → ${(r.last_date||'').slice(0,7)}</td>
      <td class="mono" style="text-align:right;color:var(--accent)">${r.n.toLocaleString()}</td>
    </tr>`).join('') + '</tbody></table>';
}

function renderNationalTable(data) {
  const wrap = document.getElementById('national-table-wrap');
  if (!data || data.length === 0) {
    wrap.innerHTML = '<div style="padding:2rem 1.25rem;color:#555b6e;font-size:13px">No matches found.</div>';
    return;
  }
  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Date</th><th>Tournament</th><th>Home</th><th>Score</th><th>Away</th><th>Venue</th>
    </tr></thead><tbody>` +
    data.map(r => {
      const score = (r.home_goals != null && r.away_goals != null)
        ? `<span class="mono">${r.home_goals}–${r.away_goals}</span>`
        : '<span style="color:#555b6e">vs</span>';
      const venue = r.neutral ? `<span style="color:#fbbf24">N: ${r.country||''}</span>` : (r.country || '');
      return `<tr>
        <td class="mono">${r.date||''}</td>
        <td style="font-size:11px;color:#8b90a0">${r.tournament||''}</td>
        <td class="strong">${r.home_team||''}</td>
        <td style="text-align:center">${score}</td>
        <td class="strong">${r.away_team||''}</td>
        <td style="font-size:12px;color:#8b90a0">${venue}</td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

// ============================================================
// PERFORMANCE DASHBOARD
// ============================================================

let perfStartingBankroll = 1000;

let collectionPoller = null;

async function checkCollectionRunning() {
  const s = await fetchJSON('/api/collection/status');
  if (!s) return;
  const btn = document.getElementById('collect-btn');
  const box = document.getElementById('status-box');
  const log = document.getElementById('status-log');
  const title = document.getElementById('status-title');
  if (s.running || s.messages.length > 0) {
    box.style.display = 'block';
    title.textContent = s.running ? 'Running...' : (s.messages.some(m => m.includes('ERROR')) ? '⚠ Finished with errors' : '✓ Done');
    log.innerHTML = s.messages.map(m => {
      const c = m.startsWith('✓') ? 'var(--green)' : m.includes('ERROR') ? 'var(--red)' : m.includes('WARN') ? 'var(--amber)' : 'var(--text3)';
      return `<div style="color:${c};font-size:12px;line-height:1.5">${m}</div>`;
    }).join('');
    log.scrollTop = log.scrollHeight;
    if (s.running) {
      btn.disabled = true;
      btn.textContent = '⏳ Running...';
      _pollCollectionStatus(btn, box, log, title);
    }
  }
}

async function testTelegram() {
  const btn = document.getElementById('tg-test-btn');
  const st = document.getElementById('tg-test-status');
  if (!btn) return;
  btn.disabled = true; const orig = btn.textContent; btn.textContent = '⏳ Sending…';
  if (st) st.textContent = '';
  try {
    const r = await fetch('/api/telegram/test', { method: 'POST' });
    const d = await r.json();
    if (st) {
      st.textContent = (d.ok ? '✅ ' : '⚠ ') + (d.msg || (d.ok ? 'Sent — check Telegram.' : 'Failed.'));
      st.style.color = d.ok ? '#16a34a' : '#d97706';
    }
  } catch (e) {
    if (st) { st.textContent = '⚠ ' + e.message; st.style.color = '#d97706'; }
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
}

async function testDigest() {
  const btn = document.getElementById('tg-digest-btn');
  const st = document.getElementById('tg-test-status');
  if (!btn) return;
  btn.disabled = true; const orig = btn.textContent; btn.textContent = '⏳ Sending…';
  try {
    const r = await fetch('/api/telegram/digest', { method: 'POST' });
    const d = await r.json();
    if (st) { st.textContent = (d.ok ? '✅ ' : '⚠ ') + (d.msg || ''); st.style.color = d.ok ? '#16a34a' : '#d97706'; }
  } catch (e) {
    if (st) { st.textContent = '⚠ ' + e.message; st.style.color = '#d97706'; }
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
}

async function startCollection(full = false) {
  const btn = document.getElementById('collect-btn');
  const box = document.getElementById('status-box');
  const log = document.getElementById('status-log');
  const title = document.getElementById('status-title');

  // Check if already running — show live status without starting new
  const current = await fetchJSON('/api/collection/status');
  if (current && current.running) {
    box.style.display = 'block';
    title.textContent = 'Running...';
    btn.disabled = true;
    btn.textContent = '⏳ Running...';
    _pollCollectionStatus(btn, box, log, title);
    return;
  }

  btn.disabled = true;
  btn.textContent = '⏳ Starting...';
  box.style.display = 'block';
  log.innerHTML = '<div style="color:var(--text3)">Starting full data collection...</div>';
  title.textContent = 'Running...';

  try {
    const resp = await fetch('/api/collection/start' + (full ? '?full=true' : ''), { method: 'POST' });
    const data = await resp.json();
    if (!data.ok) {
      // Already running — still show live status
      log.innerHTML = `<div style="color:var(--amber)">${data.msg || 'Already running'}</div>`;
    }
    _pollCollectionStatus(btn, box, log, title);
  } catch (e) {
    log.innerHTML = `<div style="color:var(--red)">Error: ${e.message}</div>`;
    btn.disabled = false;
    btn.textContent = '▶ Run Now';
  }
}

async function startOddsImport() {
  const btn = document.getElementById('collect-btn');  // poller re-enables this one
  const box = document.getElementById('status-box');
  const log = document.getElementById('status-log');
  const title = document.getElementById('status-title');
  const oddsBtn = document.getElementById('odds-import-btn');

  const current = await fetchJSON('/api/collection/status');
  if (current && current.running) {
    box.style.display = 'block';
    title.textContent = 'Running...';
    _pollCollectionStatus(btn, box, log, title);
    return;
  }

  oddsBtn.disabled = true;
  oddsBtn.textContent = '⏳ Importing odds...';
  box.style.display = 'block';
  log.innerHTML = '<div style="color:var(--text3)">Importing historical odds from football-data.co.uk…</div>';
  title.textContent = 'Running...';

  try {
    const resp = await fetch('/api/collection/odds-history', { method: 'POST' });
    const data = await resp.json();
    if (!data.ok) log.innerHTML = `<div style="color:var(--amber)">${data.msg || 'Already running'}</div>`;
    _pollCollectionStatus(btn, box, log, title);
  } catch (e) {
    log.innerHTML = `<div style="color:var(--red)">Error: ${e.message}</div>`;
  } finally {
    setTimeout(() => { oddsBtn.disabled = false; oddsBtn.textContent = '⬇ Import Historical Odds (for Backtest)'; }, 2000);
  }
}

function _pollCollectionStatus(btn, box, log, title) {
  if (collectionPoller) clearInterval(collectionPoller);
  let collectPollCount = 0;
  collectionPoller = setInterval(async () => {
    collectPollCount++;
    if (collectPollCount > 300) { // 10-minute timeout
      clearInterval(collectionPoller); collectionPoller = null;
      title.textContent = '⚠ Timed out';
      btn.disabled = false; btn.textContent = '▶ Run Now';
      log.innerHTML += '<div style="color:var(--amber);font-size:12px">Timed out after 10 min. Check Render logs.</div>';
      return;
    }
    const s = await fetchJSON('/api/collection/status');
    if (!s) return;
    log.innerHTML = s.messages.map(m => {
      const c = m.startsWith('✓') ? 'var(--green)' : m.includes('ERROR') ? 'var(--red)' : m.includes('WARN') ? 'var(--amber)' : 'var(--text3)';
      return `<div style="color:${c};font-size:12px;line-height:1.5">${m}</div>`;
    }).join('');
    log.scrollTop = log.scrollHeight;
    if (!s.running) {
      clearInterval(collectionPoller);
      collectionPoller = null;
      title.textContent = '✓ Done';
      btn.disabled = false;
      btn.textContent = '▶ Run Now';
      loadStats();
      loadFootballSummary();
      loadTennisSummary();
      loadCollectionLog();
    }
  }, 2000);
}

async function loadPerformance() {
  const input = document.getElementById('perf-start-bankroll');
  if (input) {
    try {
      const saved = localStorage.getItem('perf_start_bankroll');
      if (saved) { input.value = saved; perfStartingBankroll = parseFloat(saved); }
    } catch(e) {}
    if (!input.dataset.wired) {
      input.dataset.wired = '1';
      input.addEventListener('input', e => {
        perfStartingBankroll = parseFloat(e.target.value) || 1000;
        try { localStorage.setItem('perf_start_bankroll', e.target.value); } catch(e) {}
        loadPerformance();
      });
    }
  }

  // Show loading state
  const chartWrap = document.getElementById('perf-bankroll-chart');
  if (chartWrap) chartWrap.innerHTML = '<div class="perf-empty">Loading...</div>';

  try {
    const [bankroll, breakdown] = await Promise.all([
      fetchJSON(`/api/performance/bankroll?starting=${perfStartingBankroll}`),
      fetchJSON('/api/performance/breakdown'),
    ]);

    renderPerfMetrics(bankroll);
    renderPerfChart(bankroll);
    renderPerfBreakdown('perf-by-sport', breakdown?.by_sport, 'Sport');
    renderPerfBreakdown('perf-by-market', breakdown?.by_market, 'Market');
    renderPerfBreakdown('perf-by-bookmaker', breakdown?.by_bookmaker, 'Bookmaker');
    renderPerfBreakdown('perf-by-odds', breakdown?.by_odds, 'Odds range');
  } catch(e) {
    if (chartWrap) chartWrap.innerHTML = '<div class="perf-empty">Error loading data. Try refreshing the page.</div>';
    console.error('Performance load error:', e);
  }
}

function renderPerfMetrics(b) {
  const wrap = document.getElementById('perf-key-metrics');
  if (!b) { wrap.innerHTML = ''; return; }
  const profitCls = b.current_bankroll > b.starting_bankroll ? 'pos' : b.current_bankroll < b.starting_bankroll ? 'neg' : '';
  const totalReturn = ((b.current_bankroll - b.starting_bankroll) / b.starting_bankroll * 100);
  const returnCls = totalReturn > 0 ? 'pos' : totalReturn < 0 ? 'neg' : '';
  const streakColor = b.current_streak.type === 'win' ? 'pos' : b.current_streak.type === 'loss' ? 'neg' : '';
  const streakLabel = b.current_streak.type === 'win' ? 'wins' : b.current_streak.type === 'loss' ? 'losses' : '';

  wrap.innerHTML = `
    <div class="perf-metric">
      <div class="perf-metric-label">Current bankroll</div>
      <div class="perf-metric-value ${profitCls}">€${b.current_bankroll.toFixed(2)}</div>
      <div class="perf-metric-sub">${b.series.length} settled bets</div>
    </div>
    <div class="perf-metric">
      <div class="perf-metric-label">Total return</div>
      <div class="perf-metric-value ${returnCls}">${totalReturn >= 0 ? '+' : ''}${totalReturn.toFixed(1)}%</div>
      <div class="perf-metric-sub">vs €${b.starting_bankroll}</div>
    </div>
    <div class="perf-metric">
      <div class="perf-metric-label">Peak bankroll</div>
      <div class="perf-metric-value">€${b.peak_bankroll.toFixed(2)}</div>
      <div class="perf-metric-sub">${b.current_drawdown_pct.toFixed(1)}% below peak now</div>
    </div>
    <div class="perf-metric">
      <div class="perf-metric-label">Max drawdown</div>
      <div class="perf-metric-value neg">−${b.max_drawdown_pct.toFixed(1)}%</div>
      <div class="perf-metric-sub">€${b.max_drawdown_abs.toFixed(2)} worst</div>
    </div>
    <div class="perf-metric">
      <div class="perf-metric-label">Longest win streak</div>
      <div class="perf-metric-value pos">${b.longest_winning_streak}</div>
      <div class="perf-metric-sub">consecutive wins</div>
    </div>
    <div class="perf-metric">
      <div class="perf-metric-label">Longest losing streak</div>
      <div class="perf-metric-value neg">${b.longest_losing_streak}</div>
      <div class="perf-metric-sub">consecutive losses</div>
    </div>
    <div class="perf-metric">
      <div class="perf-metric-label">Current streak</div>
      <div class="perf-metric-value ${streakColor}">${b.current_streak.count || '—'}</div>
      <div class="perf-metric-sub">${streakLabel}</div>
    </div>
  `;
}

function renderPerfChart(b) {
  const wrap = document.getElementById('perf-bankroll-chart');
  if (!b || !b.series || b.series.length === 0) {
    wrap.innerHTML = '<div class="perf-empty">No settled bets yet. Once you start tracking outcomes, your bankroll evolution will appear here.</div>';
    return;
  }

  const w = 800, h = 240, pad = { top: 24, right: 70, bottom: 36, left: 64 };
  const innerW = w - pad.left - pad.right;
  const innerH = h - pad.top - pad.bottom;

  const data = [{date: '', bankroll: b.starting_bankroll, result: null, selection: '', profit: 0}, ...b.series];
  const values = data.map(d => d.bankroll);
  const yMin = Math.min(...values) * 0.97;
  const yMax = Math.max(...values) * 1.03;

  const xStep = data.length > 1 ? innerW / (data.length - 1) : 0;
  const xFor = i => pad.left + i * xStep;
  const yFor = v => pad.top + innerH - ((v - yMin) / (yMax - yMin)) * innerH;

  const linePath = data.map((d, i) => `${i === 0 ? 'M' : 'L'}${xFor(i).toFixed(1)},${yFor(d.bankroll).toFixed(1)}`).join(' ');
  const fillPath = `${linePath} L${xFor(data.length-1).toFixed(1)},${(pad.top+innerH).toFixed(1)} L${xFor(0).toFixed(1)},${(pad.top+innerH).toFixed(1)} Z`;

  // 5 y-axis ticks
  const yTicks = Array.from({length: 5}, (_, i) => yMin + (yMax - yMin) * i / 4);
  const baselineY = yFor(b.starting_bankroll);
  const peakY = yFor(b.peak_bankroll);

  // Bet markers — skip first point (starting bankroll, no bet)
  const markers = data.slice(1).map((d, i) => {
    const cx = xFor(i + 1).toFixed(1);
    const cy = yFor(d.bankroll).toFixed(1);
    const color = d.result === 'won' ? 'var(--green)' : d.result === 'lost' ? 'var(--red)' : 'var(--text3)';
    const sign = d.profit >= 0 ? '+' : '';
    const dateStr = d.date ? d.date.slice(0, 16) : '';
    const tipText = `${dateStr} | ${d.selection || ''} | ${sign}€${(d.profit||0).toFixed(2)} | BK: €${d.bankroll.toFixed(2)}`.replace(/"/g, "'");
    return `<circle cx="${cx}" cy="${cy}" r="4" fill="${color}" stroke="var(--bg2)" stroke-width="1.5" opacity="0.9">
      <title>${tipText}</title>
    </circle>`;
  }).join('');

  // X-axis date labels — show ~5 evenly spaced
  const labelStep = Math.max(1, Math.floor((data.length - 1) / 5));
  const xLabels = data.map((d, i) => {
    if (i === 0 || i % labelStep !== 0) return '';
    const label = d.date ? d.date.slice(5, 10) : 'start';
    return `<text class="axis-label" x="${xFor(i).toFixed(1)}" y="${pad.top + innerH + 18}" text-anchor="middle">${label}</text>`;
  }).join('');

  wrap.innerHTML = `<svg class="perf-chart" viewBox="0 0 ${w} ${h}">
    <defs>
      <linearGradient id="chartFill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="var(--green)" stop-opacity="0.18"/>
        <stop offset="100%" stop-color="var(--green)" stop-opacity="0.01"/>
      </linearGradient>
    </defs>
    ${yTicks.map(v => `
      <line class="axis-line" x1="${pad.left}" x2="${w - pad.right}" y1="${yFor(v).toFixed(1)}" y2="${yFor(v).toFixed(1)}"/>
      <text class="axis-label" x="${pad.left - 8}" y="${(yFor(v)+4).toFixed(1)}" text-anchor="end">€${v.toFixed(0)}</text>
    `).join('')}
    <line class="baseline" x1="${pad.left}" x2="${w-pad.right}" y1="${baselineY.toFixed(1)}" y2="${baselineY.toFixed(1)}"/>
    <text class="axis-label" x="${w-pad.right+6}" y="${(baselineY+4).toFixed(1)}" fill="var(--text3)" font-size="10">start</text>
    ${b.peak_bankroll > b.starting_bankroll ? `
    <line class="peak-line" x1="${pad.left}" x2="${w-pad.right}" y1="${peakY.toFixed(1)}" y2="${peakY.toFixed(1)}"/>
    <text class="axis-label" x="${w-pad.right+6}" y="${(peakY+4).toFixed(1)}" fill="var(--green)" font-size="10">peak</text>` : ''}
    ${xLabels}
    <path class="area-fill" d="${fillPath}" fill="url(#chartFill)"/>
    <path class="area-line" d="${linePath}"/>
    ${markers}
  </svg>`;
}

function renderPerfBreakdown(elId, rows, bucketLabel) {
  const wrap = document.getElementById(elId);
  if (!wrap) return;
  if (!rows || rows.length === 0) {
    wrap.innerHTML = '<div class="perf-empty">No data yet.</div>';
    return;
  }
  let html = `<div class="perf-header-row">
    <span>${bucketLabel}</span>
    <span class="right">Bets</span>
    <span class="right">ROI</span>
    <span class="right">P/L</span>
  </div>`;
  for (const r of rows) {
    const roiCls = r.roi > 0 ? 'pos' : r.roi < 0 ? 'neg' : 'neutral';
    const sign = r.roi > 0 ? '+' : '';
    const profitSign = r.profit > 0 ? '+' : '';
    html += `<div class="perf-row">
      <span class="bucket">${r.bucket}</span>
      <span class="n">${r.n}</span>
      <span class="roi ${roiCls}">${sign}${r.roi.toFixed(1)}%</span>
      <span class="profit">${profitSign}€${r.profit.toFixed(2)}</span>
    </div>`;
  }
  wrap.innerHTML = html;
}

// ===========================================================
// INITIAL LOAD — runs when page opens
// ===========================================================
window.addEventListener('DOMContentLoaded', () => {
  loadStats();        // cheap — populates sidebar "last update" + overview stat cards
  loadEdge();         // Value Bets is now the landing section — load the product immediately
});
