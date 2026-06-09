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
    if (saved === 'dark') applyTheme('dark');
    else updateThemeLabel(); // ensure label is correct on load
  } catch(e) {}
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
    e.preventDefault();
    const sec = link.dataset.section;
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    link.classList.add('active');
    document.getElementById('section-' + sec).classList.add('active');
    if (sec === 'football' && footballData.length === 0) loadFootball();
    if (sec === 'tennis' && tennisData.length === 0) loadTennis();
    if (sec === 'edge') loadEdge();
    if (sec === 'mybets') loadMyBets();
    if (sec === 'performance') loadPerformance();
    if (sec === 'national' && nationalData.length === 0) loadNational();
  });
});

// Stats
async function loadStats() {
  const s = await fetchJSON('/api/stats');
  if (!s) return;
  document.getElementById('s-football').textContent = s.football_matches.toLocaleString();
  document.getElementById('s-tennis').textContent = s.tennis_matches.toLocaleString();
  document.getElementById('s-leagues').textContent = s.football_leagues;
  document.getElementById('s-last').textContent = s.last_collection;
  document.getElementById('last-update').textContent = 'Last: ' + s.last_collection;
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
  whenFilter: 'all',
  mode: 'all',
  bankroll: 0,
  expanded: new Set(),
};

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
  document.getElementById('vb-when').addEventListener('change', e => {
    vbState.whenFilter = e.target.value; renderValueBets();
  });
  document.getElementById('vb-mode').addEventListener('change', e => {
    vbState.mode = e.target.value; renderValueBets();
  });
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
}

function applyVbFilters(rows) {
  let out = rows.slice();
  if (vbState.sportFilter) out = out.filter(r => r.sport_name === vbState.sportFilter);
  if (vbState.whenFilter !== 'all') {
    const now = Date.now();
    const hoursMap = {'24h': 24, '48h': 48, '7d': 168, '14d': 336, '30d': 720};
    const hours = hoursMap[vbState.whenFilter] || 168;
    const cutoff = now + hours * 3600 * 1000;
    out = out.filter(r => {
      const t = r.commence_time ? new Date(r.commence_time).getTime() : 0;
      return t >= now && t <= cutoff;
    });
  }
  if (vbState.mode === 'value') {
    out = out.filter(r => r.best_edge != null && r.best_edge >= 2 && r.best_edge <= 15);
  } else if (vbState.mode === 'confident') {
    out = out.filter(r => (r.best_confidence || 0) >= 4);
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
  if (e >= 2) return 'edge-pos';
  if (e >= 0) return 'edge-flat';
  return 'edge-neg';
}

function renderValueBets() {
  const wrap = document.getElementById('vb-cards');
  let data = applyVbFilters(vbState.raw);

  const countEl = document.getElementById('vb-count');
  if (countEl) countEl.textContent = `${data.length} match${data.length === 1 ? '' : 'es'} · ${vbState.raw.length} total`;

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

  wrap.innerHTML = data.map(b => renderCard(b)).join('');

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

function renderCard(b) {
  const ml = b.most_likely;
  const bv = b.best_value;
  const hasValue = bv && bv.edge_pct != null && bv.edge_pct >= 2 && bv.edge_pct <= 15;
  const isExpanded = vbState.expanded.has(b.event_id);
  const hasAutoH2H = !!(b.x1_home || b.b365_home);

  // True probability strip
  let probStrip = '';
  if (b.true_home_pct != null) {
    probStrip = `<div class="vb-true-probs">
      <div class="vb-prob-cell">
        <div class="vb-prob-label">${b.home_team}</div>
        <div class="vb-prob-value">${b.true_home_pct}%</div>
      </div>
      ${b.true_draw_pct != null ? `<div class="vb-prob-cell">
        <div class="vb-prob-label">Draw</div>
        <div class="vb-prob-value">${b.true_draw_pct}%</div>
      </div>` : ''}
      <div class="vb-prob-cell">
        <div class="vb-prob-label">${b.away_team}</div>
        <div class="vb-prob-value">${b.true_away_pct}%</div>
      </div>
    </div>`;
  }

  // ============= NEW: Best Pick + Safest Pick blocks =============
  const bestPick = b.best_pick;
  const safestPick = b.safest_pick;

  function renderModelPickBlock(pick, label, iconHtml, isPrimary) {
    if (!pick) {
      return `<div class="vb-pick-block ${isPrimary ? 'best-value no-value' : ''}">
        <div class="vb-pick-label">${label}</div>
        <div style="color:var(--text3);font-size:12px">Not enough data</div>
      </div>`;
    }
    const stars = pick.confidence || 0;
    const starsRow = Array.from({length:5}, (_,i) => `<span class="vb-star${i >= stars ? ' empty' : ''}">★</span>`).join('');
    const cls = isPrimary ? 'best-value' : '';
    const isGoalsMarket = pick.market && (pick.market.includes("Over/Under") || pick.market.includes("BTTS"));
    let confidenceHtml = '';
    if (isGoalsMarket) {
      const confLabel = pick.goals_confidence || 'MEDIUM';
      const confColor = confLabel === 'HIGH' ? '#27AE60' : confLabel === 'MEDIUM' ? '#F39C12' : '#E74C3C';
      confidenceHtml = `<span style="font-size:11px; color:${confColor}; font-weight:600; margin-left:auto">${confLabel}</span>`;
    } else {
      confidenceHtml = `<span class="vb-stars">${starsRow}</span>`;
    }
    return `<div class="vb-pick-block ${cls}">
      <div class="vb-pick-label">
        ${iconHtml}
        ${label}
        ${confidenceHtml}
      </div>
      <div class="vb-pick-selection">${pick.selection}</div>
      <div class="vb-pick-market">${pick.market} · ${pick.model_prob}% probability</div>
      <div class="vb-pick-row">
        <span class="label">Fair odd</span>
        <span class="val">${pick.fair_odd ? pick.fair_odd.toFixed(2) : '—'}</span>
        <span class="val edge-pos">→ ${pick.target_odd_5pct ? pick.target_odd_5pct.toFixed(2) : '—'}</span>
      </div>
      <div style="font-size:10.5px;color:var(--text3);margin-top:6px;line-height:1.4">
        ${isGoalsMarket ? `Based on historical goals (${pick.volatility || 'standard'} volatility)` : `Bet if 1xBet pays ≥ ${pick.target_odd_5pct ? pick.target_odd_5pct.toFixed(2) : '—'} (5% edge)`}
      </div>
    </div>`;
  }

  const trophyIcon = '<svg class="vb-pick-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 2h12v6c0 3-3 6-6 6s-6-3-6-6V2z"/><path d="M9 18h6v3H9z"/></svg>';
  const shieldIcon = '<svg class="vb-pick-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l8 4v6c0 5-4 9-8 10-4-1-8-5-8-10V6l8-4z"/></svg>';

  const mlBlock = renderModelPickBlock(bestPick, 'Best Pick', trophyIcon, true);
  const bvBlock = renderModelPickBlock(safestPick, 'Safest Pick', shieldIcon, false);

  // All-markets expanded view
  const allMarketsHTML = (b.all_picks || [])
    .filter(p => p.book === 'Best')
    .sort((a, b) => b.edge_pct - a.edge_pct)
    .map(p => {
      let eCls = 'neg';
      if (p.edge_pct > 15) eCls = 'noise';
      else if (p.edge_pct >= 2) eCls = 'pos';
      const sign = p.edge_pct > 0 ? '+' : '';
      return `<div class="vb-market-row">
        <span class="m-market">${p.market}</span>
        <span class="m-selection">${p.selection}</span>
        <span class="m-odd">${fmtOdd(p.book_odd)}</span>
        <span class="m-edge ${eCls}">${sign}${p.edge_pct.toFixed(1)}%</span>
        <span class="m-conf">${'★'.repeat(p.confidence || 0)}</span>
      </div>`;
    }).join('');

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

  // Line movement banner & arrows
  let movementBanner = '';
  let movementSparkline = '';
  if (b.line_movement) {
    const m = b.line_movement;
    const hasSteam = m.steam && Object.keys(m.steam).length > 0;
    const sharpConfirms = b.sharp_confirmation === true;

    let banners = [];
    if (sharpConfirms) banners.push(`<span class="movement-tag confirm">✓ Sharp money agrees with our pick</span>`);
    if (hasSteam) {
      for (const [outcome, info] of Object.entries(m.steam)) {
        const teamName = outcome === 'home' ? b.home_team : outcome === 'away' ? b.away_team : 'Draw';
        const arrow = info.direction === 'in' ? '↘' : '↗';
        banners.push(`<span class="movement-tag steam">⚡ Steam: ${teamName} ${arrow} ${Math.abs(info.pct).toFixed(1)}% (6h)</span>`);
      }
    }
    if (banners.length === 0 && m.snapshots >= 2) {
      // Show subtle indicator that we have history
      banners.push(`<span class="movement-tag info">📈 ${m.snapshots} snapshots tracked</span>`);
    }
    if (banners.length > 0) {
      movementBanner = `<div class="vb-movement-banner">${banners.join('')}</div>`;
    }

    // Sparkline of pin_home odds over time
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

  // BetIQ unified probability block (the fused model — star feature)
  let betiqBlock = '';
  if (b.betiq_probs && b.betiq_probs.home !== undefined) {
    const bq = b.betiq_probs;
    const agreeCls = bq.agreement === 'high' ? 'betiq-high' : bq.agreement === 'medium' ? 'betiq-med' : 'betiq-low';
    
    // NEW: reflect industry-standard approach (Pinnacle = ground truth)
    let labelText, agreeText;
    if (bq.primary_source === 'pinnacle') {
      labelText = '⬡ BetIQ · Pinnacle No-Vig (Industry Standard)';
      if (bq.agreement === 'high') agreeText = '✓ Models confirm market';
      else if (bq.agreement === 'medium') agreeText = '~ Models mostly confirm';
      else if (bq.agreement === 'low') agreeText = '⚠ Models diverge from market';
      else if (bq.agreement === 'single') agreeText = 'Pinnacle only';
      else agreeText = bq.agreement;
      
      // Show mismatch flag if present
      if (bq.mismatch_flag === 'xg_diverges_strongly') {
        agreeText += ' (xG outlier — may indicate data quality issue)';
      }
    } else {
      // Fallback path: no Pinnacle available
      labelText = '⬡ BetIQ · Model Estimate (no Pinnacle data)';
      agreeText = 'xG + Elo fallback';
    }
    
    betiqBlock = `<div class="vb-betiq-strip ${agreeCls}">
      <div class="vb-betiq-header">
        <span class="vb-betiq-label">${labelText}</span>
        <span class="vb-betiq-agree">${agreeText}</span>
      </div>
      <div class="vb-betiq-probs">
        <div class="vb-betiq-prob">
          <span class="lab">${b.home_team}</span>
          <span class="bar"><span class="fill" style="width:${bq.home}%"></span></span>
          <span class="val">${bq.home}%</span>
        </div>
        ${bq.draw != null ? `<div class="vb-betiq-prob">
          <span class="lab">Draw</span>
          <span class="bar"><span class="fill" style="width:${bq.draw}%"></span></span>
          <span class="val">${bq.draw}%</span>
        </div>` : ''}
        <div class="vb-betiq-prob">
          <span class="lab">${b.away_team}</span>
          <span class="bar"><span class="fill" style="width:${bq.away}%"></span></span>
          <span class="val">${bq.away}%</span>
        </div>
      </div>
    </div>`;
  }

  // CLV badge — shown when user has a tracked bet for this event
  let clvBadge = '';
  const trackedBets = trackedBetsMap[b.event_id] || [];
  if (trackedBets.length > 0) {
    const withClv = trackedBets.filter(tb => tb.clv_pct != null);
    if (withClv.length > 0) {
      // Show the highest-magnitude CLV among tracked bets for this event
      const best = withClv.reduce((a, x) => Math.abs(x.clv_pct) > Math.abs(a.clv_pct) ? x : a);
      const sign = best.clv_pct >= 0 ? '+' : '';
      const clvColor = best.clv_pct > 0 ? '#34d399' : best.clv_pct < 0 ? '#f87171' : '#8b90a0';
      const label = trackedBets.length > 1 ? `${trackedBets.length} bets · best CLV` : 'CLV';
      clvBadge = `<span style="background:${clvColor}18;color:${clvColor};border:1px solid ${clvColor}38;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap">${label} ${sign}${best.clv_pct}%</span>`;
    } else {
      const label = trackedBets.length > 1 ? `${trackedBets.length} bets tracked` : '1 bet tracked';
      clvBadge = `<span style="background:var(--bg2);color:var(--text3);border:1px solid var(--border);padding:2px 9px;border-radius:12px;font-size:11px;white-space:nowrap">${label} · CLV pending</span>`;
    }
  }

  const cardCls = `vb-card${hasValue ? ' has-value' : ''}${isExpanded ? ' expanded' : ''}`;

  return `<div class="${cardCls}" data-id="${b.event_id}">
    <div class="vb-card-head">
      <div class="vb-card-meta">
        <span class="vb-sport">${b.sport_name || ''}</span>
        <span class="vb-time">${fmtTime(b.commence_time)}${movementSparkline ? ' · ' + movementSparkline : ''}</span>
        ${clvBadge ? clvBadge : ''}
      </div>
      <div class="vb-match-name" onclick="window.location.href='/match/${b.event_id}'" style="cursor:pointer; transition: color 0.2s;" onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color=''">
        ${b.home_team} <span class="vs">vs</span> ${b.away_team}
        <span style="font-size:11px; color: var(--text3); margin-left: 8px; font-weight: normal;">→ ver detalhes</span>
      </div>
    </div>
    ${movementBanner}
    ${betiqBlock}
    ${probStrip}
    ${xgBlock}
    ${hasAutoH2H ? (() => {
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
    })() : ''}
    <div class="vb-manual-odds" id="manual-${b.event_id}"${hasAutoH2H ? ' style="display:none"' : ''}>
      <div class="vb-manual-header">
        <span class="vb-manual-title">⊕ Enter 1xBet odds to find real value (auto-saved)</span>
      </div>
      <div class="vb-manual-section">
        <div class="vb-manual-section-label">Match Result</div>
        <div class="vb-manual-inputs">
          <div class="vb-manual-field">
            <label>${b.home_team}</label>
            <input type="number" step="0.01" placeholder="odd"
                   value="${(b.manual_odds && b.manual_odds[b.home_team]) || ''}"
                   data-prob="${b.true_home_pct || (b.betiq_probs && b.betiq_probs.home) || 0}"
                   data-event-id="${b.event_id}"
                   data-selection="${b.home_team}"
                   data-market="Match Result"
                   oninput="calcManualEdge(this)">
            <span class="vb-manual-edge"></span>
          </div>
          ${(b.true_draw_pct != null || (b.betiq_probs && b.betiq_probs.draw)) ? `
          <div class="vb-manual-field">
            <label>Draw</label>
            <input type="number" step="0.01" placeholder="odd"
                   value="${(b.manual_odds && b.manual_odds['Draw']) || ''}"
                   data-prob="${b.true_draw_pct || (b.betiq_probs && b.betiq_probs.draw) || 0}"
                   data-event-id="${b.event_id}"
                   data-selection="Draw"
                   data-market="Match Result"
                   oninput="calcManualEdge(this)">
            <span class="vb-manual-edge"></span>
          </div>` : ''}
          <div class="vb-manual-field">
            <label>${b.away_team}</label>
            <input type="number" step="0.01" placeholder="odd"
                   value="${(b.manual_odds && b.manual_odds[b.away_team]) || ''}"
                   data-prob="${b.true_away_pct || (b.betiq_probs && b.betiq_probs.away) || 0}"
                   data-event-id="${b.event_id}"
                   data-selection="${b.away_team}"
                   data-market="Match Result"
                   oninput="calcManualEdge(this)">
            <span class="vb-manual-edge"></span>
          </div>
        </div>
      </div>
      ${b.true_over25_pct != null ? `
      <div class="vb-manual-section">
        <div class="vb-manual-section-label">Over / Under 2.5</div>
        <div class="vb-manual-inputs">
          <div class="vb-manual-field">
            <label>Over 2.5</label>
            <input type="number" step="0.01" placeholder="odd"
                   value="${(b.manual_odds && b.manual_odds['Over 2.5 goals']) || ''}"
                   data-prob="${b.true_over25_pct}"
                   data-event-id="${b.event_id}"
                   data-selection="Over 2.5 goals"
                   data-market="Over/Under 2.5"
                   oninput="calcManualEdge(this)">
            <span class="vb-manual-edge"></span>
          </div>
          <div class="vb-manual-field">
            <label>Under 2.5</label>
            <input type="number" step="0.01" placeholder="odd"
                   value="${(b.manual_odds && b.manual_odds['Under 2.5 goals']) || ''}"
                   data-prob="${b.true_under25_pct}"
                   data-event-id="${b.event_id}"
                   data-selection="Under 2.5 goals"
                   data-market="Over/Under 2.5"
                   oninput="calcManualEdge(this)">
            <span class="vb-manual-edge"></span>
          </div>
        </div>
      </div>` : ''}
      ${b.true_btts_yes_pct != null ? `
      <div class="vb-manual-section">
        <div class="vb-manual-section-label">Both Teams To Score</div>
        <div class="vb-manual-inputs">
          <div class="vb-manual-field">
            <label>BTTS Yes</label>
            <input type="number" step="0.01" placeholder="odd"
                   value="${(b.manual_odds && b.manual_odds['BTTS Yes']) || ''}"
                   data-prob="${b.true_btts_yes_pct}"
                   data-event-id="${b.event_id}"
                   data-selection="BTTS Yes"
                   data-market="Both Teams To Score"
                   oninput="calcManualEdge(this)">
            <span class="vb-manual-edge"></span>
          </div>
          <div class="vb-manual-field">
            <label>BTTS No</label>
            <input type="number" step="0.01" placeholder="odd"
                   value="${(b.manual_odds && b.manual_odds['BTTS No']) || ''}"
                   data-prob="${b.true_btts_no_pct}"
                   data-event-id="${b.event_id}"
                   data-selection="BTTS No"
                   data-market="Both Teams To Score"
                   oninput="calcManualEdge(this)">
            <span class="vb-manual-edge"></span>
          </div>
        </div>
      </div>` : ''}
    </div>
    <div class="vb-your-value vb-pick-block best-value has-value" style="display:none"></div>
    ${(() => {
      if (!bv || bv.edge_pct == null || bv.edge_pct <= 0) return '';
      const sign = bv.edge_pct > 0 ? '+' : '';
      const edgeCls = bv.edge_pct >= 2 ? 'pos' : 'low';
      const bookLabel = bv.book === 'Best' ? '🏆 Melhor odd disponível' : bv.book;
      const oddsSource = b.odds_source === 'betfair' ? '⚡ Betfair Exchange' : b.odds_source === 'pinnacle' ? '📌 Pinnacle' : '🔬 xG Model';
      const kellyLine = bv.kelly_pct > 0
        ? `Sugestão: <strong>${bv.kelly_pct.toFixed(1)}% do bankroll</strong> (¼ Kelly)`
        : 'Edge insuficiente para apostar';
      return `<div class="vb-value-alert ${edgeCls}">
        <div class="vb-value-alert-head">
          <span class="vb-value-tag">💰 Valor detetado</span>
          <span class="vb-value-source">${oddsSource}</span>
        </div>
        <div class="vb-value-body">
          <span class="vb-value-sel">${bv.selection}</span>
          <span class="vb-value-mkt">${bv.market}</span>
        </div>
        <div class="vb-value-row">
          <span class="vb-value-book">${bookLabel}</span>
          <span class="vb-value-odd">${fmtOdd(bv.book_odd)}</span>
          <span class="vb-value-edge-badge ${edgeCls}">${sign}${bv.edge_pct.toFixed(1)}%</span>
        </div>
        <div class="vb-value-kelly">${kellyLine}</div>
      </div>`;
    })()}
    <div class="vb-picks">${mlBlock}${bvBlock}</div>
    <div class="vb-card-foot">
      <span class="all-markets-link" onclick="toggleExpand('${b.event_id}')">All markets (${(b.all_picks||[]).filter(p=>p.book==='Best').length})</span>
      ${bv && bv.edge_pct > 0 ? `<button class="add-bet-btn" onclick='quickAddBet(${JSON.stringify(b).replace(/'/g, "&apos;")})'>+ Track</button>` : ''}
    </div>
    <div class="vb-all-markets">${allMarketsHTML}</div>
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

    // Get agreement level from BetIQ strip
    const card = inp.closest('.vb-card');
    const betiqStrip = card ? card.querySelector('.vb-betiq-strip') : null;
    let maxAllowedEdge = 12;  // default for high agreement
    if (betiqStrip && betiqStrip.classList.contains('betiq-low')) maxAllowedEdge = 5;

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
      if (bestEdge === null) filterReason = `Edge ${edge.toFixed(1)}% exceeds safe threshold for ${betiqStrip?.classList.contains('betiq-low') ? 'diverging' : 'agreeing'} models`;
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

  const betiqStrip = card.querySelector('.vb-betiq-strip');
  if (betiqStrip) {
    if (betiqStrip.classList.contains('betiq-low')) stars = Math.min(2, stars);
    else if (!betiqStrip.classList.contains('betiq-high')) stars = Math.min(4, stars);
  }

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
  if (vbState.expanded.has(eventId)) {
    vbState.expanded.delete(eventId);
  } else {
    vbState.expanded.add(eventId);
  }
  renderValueBets();
}

function quickAddBet(b) {
  // Pre-fill bet form with best value pick
  if (!b || !b.best_value) return;
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
    document.getElementById('bet-selection').value = b.best_value.selection || '';
    document.getElementById('bet-odds').value = b.best_value.book_odd || '';
    document.getElementById('bet-bookmaker').value = b.best_value.book || '1xBet';
    const stakeField = document.getElementById('bet-stake');
    if (vbState.bankroll > 0 && b.best_value.kelly_pct > 0 && !stakeField.value) {
      stakeField.value = (vbState.bankroll * b.best_value.kelly_pct / 100).toFixed(2);
    }
  }, 100);
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
    oddsPoller = setInterval(async () => {
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

async function loadBetsTable() {
  const bets = await fetchJSON('/api/bets');
  const wrap = document.getElementById('bets-table-wrap');
  if (!bets || bets.length === 0) {
    wrap.innerHTML = '<div style="padding:2rem 1.25rem;color:#555b6e;font-size:13px">No bets yet. Click "+ New bet" to register your first one.</div>';
    return;
  }
  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Placed</th><th>Match</th><th>Selection</th><th>Book</th>
      <th>Odds</th><th>Stake</th><th>Status</th><th>P/L</th><th>CLV</th><th></th>
    </tr></thead>
    <tbody>` +
    bets.map(b => {
      const match = (b.home_team && b.away_team) ? `${b.home_team} v ${b.away_team}` : '—';
      const placed = b.placed_at ? b.placed_at.slice(5,16) : '';
      const statusTxt = b.status === 'pending' ? 'PENDING' : (b.result || '').toUpperCase();
      const statusCls = 'bet-status-' + (b.status === 'pending' ? 'pending' : (b.result || ''));
      const profit = b.profit != null ? (b.profit >= 0 ? '+' : '') + '€' + b.profit.toFixed(2) : '—';
      const profitCol = b.profit > 0 ? '#34d399' : b.profit < 0 ? '#f87171' : '#8b90a0';
      const clv = b.clv_pct != null ? (b.clv_pct >= 0 ? '+' : '') + b.clv_pct + '%' : '—';
      const clvCol = b.clv_pct > 0 ? '#34d399' : b.clv_pct < 0 ? '#f87171' : '#555b6e';
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
        <td>${match}<div style="font-size:11px;color:#555b6e">${b.sport_name||''}</div></td>
        <td class="strong">${b.selection || ''}</td>
        <td style="font-size:12px;color:#8b90a0">${b.bookmaker || '—'}</td>
        <td class="mono">${b.odds ? b.odds.toFixed(2) : '—'}</td>
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
  const eventId = sel.value || null;
  let pinImplied = null;
  if (eventId) {
    const opt = sel.options[sel.selectedIndex];
    if (opt && opt.dataset.event) {
      const ev = JSON.parse(opt.dataset.event);
      const selection = document.getElementById('bet-selection').value.trim().toLowerCase();
      if (selection === (ev.home_team||'').toLowerCase()) pinImplied = ev.true_home_pct;
      else if (selection === (ev.away_team||'').toLowerCase()) pinImplied = ev.true_away_pct;
      else if (selection === 'draw') pinImplied = ev.true_draw_pct;
    }
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
    ['bet-sport','bet-home','bet-away','bet-commence','bet-selection','bet-odds','bet-stake','bet-notes'].forEach(id => {
      document.getElementById(id).value = '';
    });
    document.getElementById('bet-event-select').value = '';
    toggleBetForm();
    loadMyBets();
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

async function startCollection() {
  try {
    const resp = await fetch('/api/collection/start', { method: 'POST' });
    const data = await resp.json();
    if (data.ok) {
      alert('✓ Collection started! This may take 3-5 minutes. Check Data Collector or Overview for progress.');
      setTimeout(() => window.location.reload(), 3000);
    } else {
      alert('Error: ' + (data.msg || 'Unknown error'));
    }
  } catch (e) {
    alert('Error: ' + e.message);
  }
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

  const w = 800, h = 220, pad = { top: 20, right: 30, bottom: 30, left: 60 };
  const innerW = w - pad.left - pad.right;
  const innerH = h - pad.top - pad.bottom;

  const data = [{date: '', bankroll: b.starting_bankroll}, ...b.series];
  const values = data.map(d => d.bankroll);
  const min = Math.min(...values, b.starting_bankroll);
  const max = Math.max(...values, b.starting_bankroll);
  const range = (max - min) || 1;
  const pad_v = range * 0.1;
  const yMin = min - pad_v;
  const yMax = max + pad_v;

  const xStep = data.length > 1 ? innerW / (data.length - 1) : 0;
  const yFor = v => pad.top + innerH - ((v - yMin) / (yMax - yMin)) * innerH;

  const linePath = data.map((d, i) => `${i === 0 ? 'M' : 'L'} ${pad.left + i * xStep},${yFor(d.bankroll)}`).join(' ');
  const fillPath = `${linePath} L ${pad.left + (data.length - 1) * xStep},${pad.top + innerH} L ${pad.left},${pad.top + innerH} Z`;

  // Y-axis labels (start, mid, max)
  const yTicks = [yMin, (yMin + yMax) / 2, yMax];

  // Baseline (starting bankroll)
  const baselineY = yFor(b.starting_bankroll);
  // Peak line
  const peakY = yFor(b.peak_bankroll);

  wrap.innerHTML = `<svg class="perf-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    ${yTicks.map((v, i) => `
      <line class="axis-line" x1="${pad.left}" x2="${w - pad.right}" y1="${yFor(v)}" y2="${yFor(v)}"/>
      <text class="axis-label" x="${pad.left - 8}" y="${yFor(v) + 4}" text-anchor="end">€${v.toFixed(0)}</text>
    `).join('')}
    <line class="baseline" x1="${pad.left}" x2="${w - pad.right}" y1="${baselineY}" y2="${baselineY}"/>
    <text class="axis-label" x="${w - pad.right + 5}" y="${baselineY + 4}" fill="var(--text3)">start</text>
    <line class="peak-line" x1="${pad.left}" x2="${w - pad.right}" y1="${peakY}" y2="${peakY}"/>
    <text class="axis-label" x="${w - pad.right + 5}" y="${peakY + 4}" fill="var(--green)">peak</text>
    <path class="area-fill" d="${fillPath}"/>
    <path class="area-line" d="${linePath}"/>
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
  loadStats();
  loadFootballSummary();
  loadTennisSummary();
  loadCollectionLog();
});
