// Performance page (action-plan 3.5): buy/sell latency, fed by
// /api/dashboard/latency (which reads LatencySnapshot - see
// dashboard_models.py). apiFetch mirrors autosnipe_manager.js's helper.

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

const state = { range: '7d', from: null, path: '', stage: 'total' };
let latencyChart = null;

function rangeToFrom(range) {
  const now = new Date();
  if (range === 'today') { const d = new Date(now); d.setHours(0, 0, 0, 0); return d.toISOString(); }
  if (range === '7d') return new Date(now.getTime() - 7 * 86400000).toISOString();
  if (range === '30d') return new Date(now.getTime() - 30 * 86400000).toISOString();
  return null; // "all"
}

async function loadLatency() {
  const params = new URLSearchParams();
  if (state.from) params.set('from', state.from);
  if (state.path) params.set('path', state.path);
  try {
    const data = await apiFetch(`/api/dashboard/latency?${params.toString()}`);
    document.getElementById('statBuyAvg').textContent =
      data.current.buy_total_avg_ms !== null ? data.current.buy_total_avg_ms.toFixed(1) : 'N/A';
    document.getElementById('statSellAvg').textContent =
      data.current.sell_total_avg_ms !== null ? data.current.sell_total_avg_ms.toFixed(1) : 'N/A';
    renderChart(data.series.filter((s) => s.stage === state.stage));
  } catch (e) {
    console.error('Failed to load latency data', e);
  }
}

function renderChart(points) {
  const ctx = document.getElementById('latencyChart').getContext('2d');
  const byPath = { buy: [], sell: [] };
  points.forEach((p) => { if (byPath[p.path]) byPath[p.path].push(p); });
  const labels = [...new Set(points.map((p) => p.window_start))].sort();

  const series = (pathPoints, key) => labels.map((label) => {
    const match = pathPoints.find((p) => p.window_start === label);
    return match ? match[key] : null;
  });

  if (latencyChart) latencyChart.destroy();
  latencyChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels.map((l) => new Date(l).toLocaleTimeString()),
      datasets: [
        { label: 'Buy avg (ms)', data: series(byPath.buy, 'avg_ms'), borderColor: '#4a3aff', backgroundColor: 'transparent', tension: 0.2 },
        { label: 'Buy max (ms)', data: series(byPath.buy, 'max_ms'), borderColor: '#4a3aff', borderDash: [5, 5], backgroundColor: 'transparent', tension: 0.2 },
        { label: 'Sell avg (ms)', data: series(byPath.sell, 'avg_ms'), borderColor: '#00c853', backgroundColor: 'transparent', tension: 0.2 },
        { label: 'Sell max (ms)', data: series(byPath.sell, 'max_ms'), borderColor: '#00c853', borderDash: [5, 5], backgroundColor: 'transparent', tension: 0.2 },
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

document.querySelectorAll('.range-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.range-btn').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    state.range = btn.dataset.range;
    state.from = rangeToFrom(state.range);
    loadLatency();
  });
});
document.getElementById('pathFilter').addEventListener('change', (e) => {
  state.path = e.target.value;
  loadLatency();
});
document.getElementById('stageFilter').addEventListener('change', (e) => {
  state.stage = e.target.value;
  loadLatency();
});

state.from = rangeToFrom(state.range);
loadLatency();
