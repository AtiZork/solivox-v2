// Log Analysis page: fetches a read-only pattern-matched summary of the
// last N hours of system logs and renders it as a bullet-point report.
// Same JWT-in-localStorage pattern as autosnipe_manager.js's apiFetch.

async function fetchLogAnalysis(hours) {
  const token = localStorage.getItem('token');
  const headers = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const resp = await fetch(`/api/logs/analyze?hours=${encodeURIComponent(hours)}`, { headers });
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

function severityLabel(sev) {
  if (sev === 'error') return 'Error';
  if (sev === 'warning') return 'Warning';
  return 'Info';
}

function renderFinding(f) {
  const evidence = (f.evidence || []).map(escapeHtml).join('\n');
  return `
    <div class="card log-finding-card severity-${f.severity}">
      <div class="card-body">
        <div class="d-flex justify-content-between align-items-start">
          <h5 class="card-title mb-1">${escapeHtml(f.category)}</h5>
          <span class="badge severity-badge-${f.severity}">${severityLabel(f.severity)} &middot; ${f.count}</span>
        </div>
        <p class="card-text mb-1">${escapeHtml(f.likely_cause || '')}</p>
        ${evidence ? `<div class="log-evidence">${evidence}</div>` : ''}
      </div>
    </div>
  `;
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = String(s == null ? '' : s);
  return div.innerHTML;
}

async function runAnalysis() {
  const btn = document.getElementById('analyze-logs-btn');
  const statusEl = document.getElementById('log-analysis-status');
  const errorEl = document.getElementById('log-analysis-error');
  const cleanEl = document.getElementById('log-analysis-clean');
  const resultsEl = document.getElementById('log-analysis-results');
  const hoursSelect = document.getElementById('log-hours-select');
  const hours = hoursSelect ? hoursSelect.value : 24;

  errorEl.classList.add('d-none');
  cleanEl.classList.add('d-none');
  resultsEl.innerHTML = '';
  statusEl.textContent = `Analyzing the last ${hours} hours...`;
  if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }

  try {
    const data = await fetchLogAnalysis(hours);

    if (data.source_error) {
      statusEl.textContent = '';
      errorEl.textContent = data.source_error;
      errorEl.classList.remove('d-none');
      return;
    }

    statusEl.textContent = `Analyzed ${data.lines_analyzed} log line(s) from the last ${data.hours} hour(s).`;

    if (!data.findings || data.findings.length === 0) {
      cleanEl.classList.remove('d-none');
      return;
    }

    resultsEl.innerHTML = data.findings.map(renderFinding).join('');
  } catch (err) {
    statusEl.textContent = '';
    if (err.status === 401) {
      errorEl.textContent = 'You must be logged in to analyze logs.';
    } else {
      errorEl.textContent = 'Failed to analyze logs: ' + (err.body?.error || err.message || 'unknown error');
    }
    errorEl.classList.remove('d-none');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Analyze Logs'; }
  }
}

document.addEventListener('DOMContentLoaded', function () {
  const btn = document.getElementById('analyze-logs-btn');
  if (btn) btn.addEventListener('click', runAnalysis);
});
