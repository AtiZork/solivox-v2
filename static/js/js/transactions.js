// Transactions page (action-plan 3.4): real trade list + financial summary,
// replacing the old Kripton demo template's hardcoded rows.
// apiFetch mirrors autosnipe_manager.js's own helper (that file's the only
// place a shared fetch wrapper already exists in this codebase).

async function apiFetch(path, options = {}) {
  const token = localStorage.getItem('token');
  options.headers = options.headers || {};
  options.headers['Content-Type'] = 'application/json';
  if (token) options.headers['Authorization'] = `Bearer ${token}`;
  const resp = await fetch(path, options);
  if (!resp.ok) {
    const text = await resp.text();
    let body = text;
    try { body = JSON.parse(text); } catch (e) {}
    const err = new Error('API Error');
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  return resp.json();
}

const state = {
  range: 'today',
  from: null,
  to: null,
  status: '',
  trade_kind: '',
  outcome: '',
  wallet_id: '',
  token: '',
  page: 1,
  page_size: 25,
};

const logState = {
  trade_type: '',
  page: 1,
  page_size: 25,
};

let spendEarnedChart = null;

function rangeToDates(range) {
  const now = new Date();
  const toISO = (d) => d.toISOString().slice(0, 10);
  if (range === 'today') {
    const start = new Date(now);
    start.setHours(0, 0, 0, 0);
    return { from: toISO(start), to: null };
  }
  if (range === '7d') {
    const start = new Date(now.getTime() - 7 * 86400000);
    return { from: toISO(start), to: null };
  }
  if (range === '30d') {
    const start = new Date(now.getTime() - 30 * 86400000);
    return { from: toISO(start), to: null };
  }
  return { from: null, to: null }; // "all"
}

function buildQuery(extra = {}) {
  const params = new URLSearchParams();
  if (state.from) params.set('from', state.from);
  if (state.to) params.set('to', state.to);
  if (state.status) params.set('status', state.status);
  if (state.trade_kind) params.set('trade_kind', state.trade_kind);
  if (state.outcome) params.set('outcome', state.outcome);
  if (state.wallet_id) params.set('wallet_id', state.wallet_id);
  if (state.token) params.set('token', state.token);
  Object.entries(extra).forEach(([k, v]) => params.set(k, v));
  return params.toString();
}

function fmtUsd(v) {
  if (v === null || v === undefined) return 'N/A';
  const sign = v >= 0 ? '+' : '';
  return `${sign}$${v.toFixed(2)}`;
}

function pnlClass(v) {
  if (v === null || v === undefined) return '';
  return v >= 0 ? 'profit' : 'loss';
}

function statusBadge(status) {
  return `<span class="badge-status badge-${status}">${status}</span>`;
}

// Client feedback: show the USD figures also as an approximate SOL value,
// clearly marked with "~" - see dashboard_api.py's module docstring for why
// this is an approximation (one current rate applied to USD realized at
// many different historical moments), not exact SOL tracking.
function fmtSolApprox(v) {
  if (v === null || v === undefined) return '';
  const sign = v >= 0 ? '+' : '';
  return `~${sign}${v.toFixed(4)} SOL`;
}

async function loadWallets() {
  try {
    const data = await apiFetch('/get_wallets');
    const select = document.getElementById('walletFilter');
    (data.wallets || []).forEach((w) => {
      const opt = document.createElement('option');
      opt.value = w.id;
      opt.textContent = w.title || w.public_key.slice(0, 8) + '...';
      select.appendChild(opt);
    });
  } catch (e) {
    // Wallet list is a filter convenience, not critical - fail silently.
  }
}

async function loadFinancial() {
  try {
    const data = await apiFetch(`/api/dashboard/financial?${buildQuery({ status: state.status || 'all' })}`);
    const t = data.totals;
    document.getElementById('statSpent').textContent = t.sol_spent.toFixed(4) + ' SOL';
    document.getElementById('statSpentUsd').textContent =
      t.sol_spent_usd_approx !== null ? `~$${t.sol_spent_usd_approx.toFixed(2)}` : '';

    document.getElementById('statEarned').textContent = fmtUsd(t.earned_usd);
    document.getElementById('statEarnedSol').textContent = fmtSolApprox(t.earned_sol_approx);

    const realizedEl = document.getElementById('statRealized');
    realizedEl.textContent = fmtUsd(t.realized_pnl_usd);
    realizedEl.className = 'stat-value ' + pnlClass(t.realized_pnl_usd);
    document.getElementById('statRealizedSol').textContent = fmtSolApprox(t.realized_pnl_sol_approx);

    const unrealizedEl = document.getElementById('statUnrealized');
    unrealizedEl.textContent = fmtUsd(t.unrealized_pnl_usd);
    unrealizedEl.className = 'stat-value ' + pnlClass(t.unrealized_pnl_usd);
    document.getElementById('statUnrealizedSol').textContent = fmtSolApprox(t.unrealized_pnl_sol_approx);

    document.getElementById('statWinRate').textContent =
      t.win_rate_pct === null || t.win_rate_pct === undefined ? 'N/A' : t.win_rate_pct.toFixed(1) + '%';
    document.getElementById('solRateBadge').textContent =
      t.sol_usd_rate_used !== null ? `1 SOL ≈ $${t.sol_usd_rate_used.toFixed(2)}` : '1 SOL ≈ $? (rate unavailable)';

    renderChart(data.by_day);
  } catch (e) {
    console.error('Failed to load financial summary', e);
  }
}

function renderChart(byDay) {
  const ctx = document.getElementById('spendEarnedChart').getContext('2d');
  const labels = byDay.map((d) => d.day);
  const spent = byDay.map((d) => d.sol_spent);
  const earned = byDay.map((d) => d.earned_usd);
  const realizedPnl = byDay.map((d) => d.realized_pnl_usd);
  // Cumulative P&L computed client-side from the day-by-day figures already
  // returned - no extra API call, shows the running trend at a glance.
  let running = 0;
  const cumulativePnl = realizedPnl.map((v) => { running += v; return running; });

  if (spendEarnedChart) spendEarnedChart.destroy();
  spendEarnedChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Spent (SOL)', data: spent, borderColor: '#4a3aff', backgroundColor: 'transparent', tension: 0.2 },
        { label: 'Earned (USD)', data: earned, borderColor: '#00c853', backgroundColor: 'transparent', tension: 0.2 },
        { label: 'Realized P&L (USD)', data: realizedPnl, borderColor: '#ffb300', backgroundColor: 'transparent', tension: 0.2 },
        { label: 'Cumulative P&L (USD)', data: cumulativePnl, borderColor: '#ff3d00', backgroundColor: 'transparent', tension: 0.2, borderDash: [5, 5] },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: { x: { ticks: { color: '#b8b8d1' } }, y: { ticks: { color: '#b8b8d1' } } },
      plugins: { legend: { labels: { color: '#ffffff' } } },
    },
  });
}

async function loadTrades() {
  const tbody = document.getElementById('tradesBody');
  tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Loading...</td></tr>';
  try {
    const query = buildQuery({ page: state.page, page_size: state.page_size, sort: '-created_at' });
    const data = await apiFetch(`/api/trades?${query}`);
    if (!data.trades.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No trades match these filters.</td></tr>';
    } else {
      tbody.innerHTML = data.trades.map((t) => `
        <tr>
          <td>${t.created_at ? new Date(t.created_at).toLocaleString() : '-'}</td>
          <td>${t.token_name || t.token_address.slice(0, 10) + '...'}</td>
          <td>${t.trade_kind}</td>
          <td>${statusBadge(t.status)}</td>
          <td>${t.entry_price_usd !== null ? '$' + Number(t.entry_price_usd).toPrecision(4) : '-'}</td>
          <td>${t.exit_price_usd !== null ? '$' + Number(t.exit_price_usd).toPrecision(4) : '-'}</td>
          <td>${Number(t.sol_spent).toFixed(4)}</td>
          <td class="${pnlClass(t.realized_pnl_usd)}">${fmtUsd(t.realized_pnl_usd)}</td>
          <td>${t.duration_sec !== null ? Math.round(t.duration_sec / 60) + 'm' : '-'}</td>
        </tr>
      `).join('');
    }
    const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
    document.getElementById('pageInfo').textContent = `Page ${data.page} of ${totalPages} (${data.total} trades)`;
    document.getElementById('prevPage').disabled = data.page <= 1;
    document.getElementById('nextPage').disabled = data.page >= totalPages;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty-state">Failed to load trades${e.status === 401 ? ' - please log in again' : ''}.</td></tr>`;
  }
}

async function loadTransactionLog() {
  const tbody = document.getElementById('logBody');
  tbody.innerHTML = '<tr><td colspan="7" class="empty-state">Loading...</td></tr>';
  try {
    const params = new URLSearchParams();
    if (state.from) params.set('from', state.from);
    if (state.to) params.set('to', state.to);
    if (logState.trade_type) params.set('trade_type', logState.trade_type);
    params.set('page', logState.page);
    params.set('page_size', logState.page_size);

    const data = await apiFetch(`/api/trades/log?${params.toString()}`);
    if (!data.events.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No transactions in this range.</td></tr>';
    } else {
      tbody.innerHTML = data.events.map((e) => `
        <tr>
          <td>${e.timestamp ? new Date(e.timestamp).toLocaleString() : '-'}</td>
          <td>${e.trade_type}</td>
          <td>${e.token_name || e.token_address.slice(0, 10) + '...'}</td>
          <td>${Number(e.amount).toPrecision(4)}</td>
          <td>$${Number(e.execution_price_usd).toPrecision(4)}</td>
          <td>${statusBadge(e.status)}</td>
          <td><a href="https://solscan.io/tx/${e.tx_id}" target="_blank" rel="noopener">${e.tx_id.slice(0, 8)}...</a></td>
        </tr>
      `).join('');
    }
    const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
    document.getElementById('logPageInfo').textContent = `Page ${data.page} of ${totalPages} (${data.total} events)`;
    document.getElementById('logPrevPage').disabled = data.page <= 1;
    document.getElementById('logNextPage').disabled = data.page >= totalPages;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state">Failed to load the transaction log${e.status === 401 ? ' - please log in again' : ''}.</td></tr>`;
  }
}

function refresh() {
  state.page = 1;
  logState.page = 1;
  loadFinancial();
  loadTrades();
  loadTransactionLog();
}

document.querySelectorAll('.range-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.range-btn').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    state.range = btn.dataset.range;
    const { from, to } = rangeToDates(state.range);
    state.from = from;
    state.to = to;
    refresh();
  });
});

document.getElementById('applyFilters').addEventListener('click', () => {
  state.status = document.getElementById('statusFilter').value;
  state.trade_kind = document.getElementById('tradeKindFilter').value;
  state.outcome = document.getElementById('outcomeFilter').value;
  state.wallet_id = document.getElementById('walletFilter').value;
  state.token = document.getElementById('tokenSearch').value.trim();
  refresh();
});

document.getElementById('prevPage').addEventListener('click', () => {
  if (state.page > 1) { state.page -= 1; loadTrades(); }
});
document.getElementById('nextPage').addEventListener('click', () => {
  state.page += 1; loadTrades();
});

document.getElementById('logTypeFilter').addEventListener('change', (e) => {
  logState.trade_type = e.target.value;
  logState.page = 1;
  loadTransactionLog();
});
document.getElementById('logPrevPage').addEventListener('click', () => {
  if (logState.page > 1) { logState.page -= 1; loadTransactionLog(); }
});
document.getElementById('logNextPage').addEventListener('click', () => {
  logState.page += 1; loadTransactionLog();
});

loadWallets();
refresh();
