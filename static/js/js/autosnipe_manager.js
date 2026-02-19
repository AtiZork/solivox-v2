// Lightweight autosnipe manager: list, create, edit, delete
// Expects JWT token in localStorage under 'token'

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
  if (resp.status === 204) return null;
  return resp.json();
}

function showAuthBanner(message, isError = false) {
  let banner = document.getElementById('autosnipe-auth-banner');
  if (!banner) {
    const container = document.querySelector('.container-fluid') || document.body;
    banner = document.createElement('div');
    banner.id = 'autosnipe-auth-banner';
    banner.style.margin = '8px 0';
    container.prepend(banner);
  }
  banner.innerHTML = `<div class="alert ${isError ? 'alert-danger' : 'alert-info'}">${message}</div>`;
}

function clearAuthBanner() {
  const banner = document.getElementById('autosnipe-auth-banner');
  if (banner) banner.remove();
}

function createCardElement(sniper) {
  const col = document.createElement('div');
  col.className = 'col-lg-4 col-md-6 mb-3';
  col.dataset.id = sniper.id;
  col.innerHTML = `
    <div class="card autosniper-card">
      <div class="card-header d-flex justify-content-between align-items-center">
        <div>
          <strong>Sniper #${sniper.id}</strong>
          <div class="small text-muted">Buy Txns ≥ $${sniper.buy_txns_over_80_usd || ''} • Min Txns: ${sniper.min_txns || ''}</div>
        </div>
        <div>
          <button class="btn btn-sm btn-outline-secondary edit-sniper-btn" data-id="${sniper.id}"><i class="fa fa-edit"></i></button>
          <!-- display-only status (not clickable) -->
          <span class="btn btn-sm btn-outline-${sniper.active ? 'success' : 'secondary'} ms-2 sniper-status" data-id="${sniper.id}">${sniper.active ? 'Active' : 'Inactive'}</span>
        </div>
      </div>
      <div class="card-body">
        <p class="mb-1">Buy amount: <strong>${sniper.buy_amount}</strong> SOL</p>
        <p class="mb-1">Buy slippage: <strong>${sniper.slippage}</strong>% • Priority fee: <strong>${sniper.priority_fee}</strong> SOL</p>
        <p class="mb-1">Launch delay: <strong>${sniper.launch_delay}</strong>s</p>
        <hr />
        <p class="mb-1">Sell drop cutoff: <strong>${sniper.drop_cutoff}</strong>% • Until profit: <strong>${sniper.drop_until_profit}</strong>%</p>
        <p class="mb-1">Sell targets: 200%:<strong>${sniper.sell_at_200}</strong>% 400%:<strong>${sniper.sell_at_400}</strong>% 1000%:<strong>${sniper.sell_at_1000}</strong>%</p>
        <p class="mb-0">Status: ${sniper.active ? '<span class="badge bg-success">Active</span>' : '<span class="badge bg-secondary">Inactive</span>'}</p>
      </div>
    </div>
  `;
  return col;
}

async function loadSnipers() {
  try {
    const token = localStorage.getItem('token');
    const addBtn = document.getElementById('add-autosniper-btn');
    if (!token) {
      showAuthBanner('Not authenticated — AutoSnipers require login. Set a valid JWT in localStorage.key "token" to view and manage AutoSnipers.');
      if (addBtn) addBtn.disabled = true;
      return;
    } else {
      clearAuthBanner();
      if (addBtn) addBtn.disabled = false;
    }

    const container = document.getElementById('autosnipers-container');
    if (!container) {
      console.warn('No #autosnipers-container found in DOM');
      return;
    }
    container.innerHTML = '';
    const list = await apiFetch('/api/autosnipe/list', { method: 'GET' });
    if (!Array.isArray(list)) return;
    if (list.length === 0) {
      container.innerHTML = '<div class="alert alert-secondary">No AutoSnipers found for your account. Click "Add New AutoSniper" to create one.</div>';
      return;
    }
    list.forEach(s => {
      const el = createCardElement(s);
      container.appendChild(el);
    });
    bindCardButtons();
  } catch (err) {
    console.error('Failed to load snipers', err);
    if (err.status === 401) {
      showAuthBanner('Unauthorized. Your token may be invalid or expired. Please login again.', true);
      const addBtn = document.getElementById('add-autosniper-btn');
      if (addBtn) addBtn.disabled = true;
      const container = document.getElementById('autosnipers-container');
      if (container) container.innerHTML = '<div class="alert alert-warning">Unauthorized. Please login to view AutoSnipers.</div>';
    } else {
      showAuthBanner('Failed to load AutoSnipers. Check console/network for details.', true);
    }
  }
}

function bindCardButtons() {
  document.querySelectorAll('.edit-sniper-btn').forEach(btn => {
    btn.removeEventListener('click', onEditClick);
    btn.addEventListener('click', onEditClick);
  });
}

async function onEditClick(e) {
  const id = e.currentTarget.dataset.id;
  try {
    const sniper = await apiFetch(`/api/autosnipe/${id}`, { method: 'GET' });
    openModalWithData(sniper);
  } catch (err) {
    console.error('Failed to fetch sniper', err);
    alert('Failed to fetch sniper: ' + (err.body?.error || err.message));
  }
}

function openModalWithData(sniper) {
  const modalEl = document.getElementById('autosniperModal');
  if (!modalEl) {
    alert('Modal element not found');
    return;
  }
  // populate
  const idField = document.getElementById('modal_autosniper_id');
  const buyTxnsField = document.getElementById('modal_buy_txns_over_80_usd');
  const minTxnsField = document.getElementById('modal_min_txns');
  const launchDelayField = document.getElementById('modal_launch_delay');
  const buyAmountField = document.getElementById('modal_buy_amount');
  const slippageField = document.getElementById('modal_slippage');
  const priorityFeeField = document.getElementById('modal_priority_fee');
  const dropCutoffField = document.getElementById('modal_drop_cutoff');
  const dropUntilField = document.getElementById('modal_drop_until_profit');
  const dropAfter100Field = document.getElementById('modal_drop_after_100');
  const dropAfter400Field = document.getElementById('modal_drop_after_400');
  const sell200 = document.getElementById('modal_sell_at_200');
  const sell400 = document.getElementById('modal_sell_at_400');
  const sell1000 = document.getElementById('modal_sell_at_1000');
  const sell1500 = document.getElementById('modal_sell_at_1500');
  const sell2500 = document.getElementById('modal_sell_at_2500');
  const sell4000 = document.getElementById('modal_sell_at_4000');
  const sell10000 = document.getElementById('modal_sell_at_10000');
  const activeField = document.getElementById('modal_active');

  if (idField) idField.value = sniper.id || '';
  if (buyTxnsField) buyTxnsField.value = sniper.buy_txns_over_80_usd ?? 80;
  if (minTxnsField) minTxnsField.value = sniper.min_txns ?? 5;
  if (launchDelayField) launchDelayField.value = sniper.launch_delay ?? 5;
  if (buyAmountField) buyAmountField.value = sniper.buy_amount ?? 1;
  if (slippageField) slippageField.value = sniper.slippage ?? 100;
  if (priorityFeeField) priorityFeeField.value = sniper.priority_fee ?? 0.01;
  if (dropCutoffField) dropCutoffField.value = sniper.drop_cutoff ?? 30;
  if (dropUntilField) dropUntilField.value = sniper.drop_until_profit ?? 99;
  if (dropAfter100Field) dropAfter100Field.value = sniper.drop_after_100 ?? 50;
  if (dropAfter400Field) dropAfter400Field.value = sniper.drop_after_400 ?? 30;
  if (sell200) sell200.value = sniper.sell_at_200 ?? 10;
  if (sell400) sell400.value = sniper.sell_at_400 ?? 10;
  if (sell1000) sell1000.value = sniper.sell_at_1000 ?? 10;
  if (sell1500) sell1500.value = sniper.sell_at_1500 ?? 10;
  if (sell2500) sell2500.value = sniper.sell_at_2500 ?? 10;
  if (sell4000) sell4000.value = sniper.sell_at_4000 ?? 10;
  if (sell10000) sell10000.value = sniper.sell_at_10000 ?? 10;
  if (activeField) activeField.checked = !!sniper.active;

  // show delete button for existing
  const delBtn = document.getElementById('modal-delete-btn');
  if (delBtn) delBtn.style.display = sniper.id ? 'inline-block' : 'none';

  // show modal (Bootstrap 5) - fallback if bootstrap missing
  if (window.bootstrap && typeof window.bootstrap.Modal === 'function') {
    const modal = new bootstrap.Modal(modalEl);
    modal.show();
  } else {
    // fallback: add 'show' class and display block
    modalEl.classList.add('show');
    modalEl.style.display = 'block';
    modalEl.setAttribute('aria-modal', 'true');
    modalEl.removeAttribute('aria-hidden');
  }
}

function clearModal() {
  const idField = document.getElementById('modal_autosniper_id');
  const buyTxnsField = document.getElementById('modal_buy_txns_over_80_usd');
  const minTxnsField = document.getElementById('modal_min_txns');
  const launchDelayField = document.getElementById('modal_launch_delay');
  const buyAmountField = document.getElementById('modal_buy_amount');
  const slippageField = document.getElementById('modal_slippage');
  const priorityFeeField = document.getElementById('modal_priority_fee');
  const dropCutoffField = document.getElementById('modal_drop_cutoff');
  const dropUntilField = document.getElementById('modal_drop_until_profit');
  const dropAfter100Field = document.getElementById('modal_drop_after_100');
  const dropAfter400Field = document.getElementById('modal_drop_after_400');
  const sell200 = document.getElementById('modal_sell_at_200');
  const sell400 = document.getElementById('modal_sell_at_400');
  const sell1000 = document.getElementById('modal_sell_at_1000');
  const sell1500 = document.getElementById('modal_sell_at_1500');
  const sell2500 = document.getElementById('modal_sell_at_2500');
  const sell4000 = document.getElementById('modal_sell_at_4000');
  const sell10000 = document.getElementById('modal_sell_at_10000');
  const activeField = document.getElementById('modal_active');
  if (idField) idField.value = '';
  if (buyTxnsField) buyTxnsField.value = 80;
  if (minTxnsField) minTxnsField.value = 5;
  if (launchDelayField) launchDelayField.value = 5;
  if (buyAmountField) buyAmountField.value = 1;
  if (slippageField) slippageField.value = 100;
  if (priorityFeeField) priorityFeeField.value = 0.01;
  if (dropCutoffField) dropCutoffField.value = 30;
  if (dropUntilField) dropUntilField.value = 99;
  if (dropAfter100Field) dropAfter100Field.value = 50;
  if (dropAfter400Field) dropAfter400Field.value = 30;
  if (sell200) sell200.value = 10;
  if (sell400) sell400.value = 10;
  if (sell1000) sell1000.value = 10;
  if (sell1500) sell1500.value = 10;
  if (sell2500) sell2500.value = 10;
  if (sell4000) sell4000.value = 10;
  if (sell10000) sell10000.value = 10;
  if (activeField) activeField.checked = true;
  const delBtn = document.getElementById('modal-delete-btn');
  if (delBtn) delBtn.style.display = 'none';
}

async function onModalSave(e) {
  e.preventDefault();
  const idField = document.getElementById('modal_autosniper_id');
  const id = idField ? idField.value : '';
  const payload = {
    buy_txns_over_80_usd: parseFloat(document.getElementById('modal_buy_txns_over_80_usd')?.value) || 80,
    min_txns: parseInt(document.getElementById('modal_min_txns')?.value) || 5,
    launch_delay: parseInt(document.getElementById('modal_launch_delay')?.value) || 5,
    buy_amount: parseFloat(document.getElementById('modal_buy_amount')?.value) || 1,
    slippage: parseFloat(document.getElementById('modal_slippage')?.value) || 100,
    priority_fee: parseFloat(document.getElementById('modal_priority_fee')?.value) || 0.01,
    drop_cutoff: parseFloat(document.getElementById('modal_drop_cutoff')?.value) || 30,
    drop_until_profit: parseFloat(document.getElementById('modal_drop_until_profit')?.value) || 99,
    drop_after_100: parseFloat(document.getElementById('modal_drop_after_100')?.value) || 50,
    drop_after_400: parseFloat(document.getElementById('modal_drop_after_400')?.value) || 30,
    sell_at_200: parseFloat(document.getElementById('modal_sell_at_200')?.value) || 10,
    sell_at_400: parseFloat(document.getElementById('modal_sell_at_400')?.value) || 10,
    sell_at_1000: parseFloat(document.getElementById('modal_sell_at_1000')?.value) || 10,
    sell_at_1500: parseFloat(document.getElementById('modal_sell_at_1500')?.value) || 10,
    sell_at_2500: parseFloat(document.getElementById('modal_sell_at_2500')?.value) || 10,
    sell_at_4000: parseFloat(document.getElementById('modal_sell_at_4000')?.value) || 10,
    sell_at_10000: parseFloat(document.getElementById('modal_sell_at_10000')?.value) || 10,
    active: !!document.getElementById('modal_active')?.checked,
  };
  try {
    if (!localStorage.getItem('token')) {
      alert('You must be logged in to create an AutoSniper.');
      return;
    }
    if (id) {
      const updated = await apiFetch(`/api/autosnipe/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
      await loadSnipers();
      hideModal();
    } else {
      const created = await apiFetch('/api/autosnipe', { method: 'POST', body: JSON.stringify(payload) });
      await loadSnipers();
      hideModal();
    }
  } catch (err) {
    console.error('Failed to save', err);
    // Show server-side validation errors in modal if present
    if (err.status === 400 && err.body && err.body.errors) {
      alert('Validation errors:\n' + err.body.errors.join('\n'));
    } else if (err.status === 401) {
      alert('Unauthorized. Please login first.');
    } else {
      alert('Save failed: ' + (err.body?.error || err.message));
    }
  }
}

function hideModal() {
  const modalEl = document.getElementById('autosniperModal');
  if (!modalEl) return;
  if (window.bootstrap && typeof window.bootstrap.Modal === 'function') {
    const modal = bootstrap.Modal.getInstance(modalEl);
    if (modal) {
      try {
        modal.hide();
        return;
      } catch (e) {
        // fallback to manual hide below
      }
    }
  }
  // fallback manual hide: remove 'show', hide element and remove any backdrops
  modalEl.classList.remove('show');
  modalEl.style.display = 'none';
  modalEl.setAttribute('aria-hidden', 'true');
  modalEl.removeAttribute('aria-modal');
  // remove any bootstrap modal-backdrop elements left behind
  document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
  // remove modal-open class from body (clean leftover state)
  try { document.body.classList.remove('modal-open'); } catch (e) {}
  // restore body padding (bootstrap sets it when scrollbar disappears)
  try { document.body.style.paddingRight = ''; } catch (e) {}
}

// Ensure hideModal is available globally for inline onclick attributes
try { window.hideModal = hideModal; } catch (e) { /* ignore in restricted environments */ }

// Top-level delegated global click handler as final fallback (outside setupBindings)
document.addEventListener('click', function (ev) {
  try {
    const closest = ev.target.closest && ev.target.closest('[data-bs-dismiss="modal"], .btn-close, .modal-footer .btn-secondary, button[aria-label="Close"]');
    if (!closest) return;
    const modalEl = document.getElementById('autosniperModal');
    if (!modalEl) return;
    if (modalEl.contains(closest) || document.querySelectorAll('.modal.show').length > 0) {
      ev.preventDefault();
      setTimeout(() => hideModal(), 5);
    }
  } catch (e) {
    // ignore
  }
});

function setupBindings() {
  const addBtn = document.getElementById('add-autosniper-btn');
  if (addBtn) {
    addBtn.addEventListener('click', (e) => {
      clearModal();
      const modalEl = document.getElementById('autosniperModal');
      if (!modalEl) return;
      if (window.bootstrap && typeof window.bootstrap.Modal === 'function') {
        const modal = new bootstrap.Modal(modalEl);
        modal.show();
      } else {
        // fallback show
        modalEl.classList.add('show');
        modalEl.style.display = 'block';
        modalEl.setAttribute('aria-modal', 'true');
        modalEl.removeAttribute('aria-hidden');
      }
    });
  }

  const form = document.getElementById('autosniperModalForm');
  if (form) form.addEventListener('submit', onModalSave);
  const del = document.getElementById('modal-delete-btn');
  if (del) del.addEventListener('click', onModalDelete);

  // Ensure modal close (X) and Cancel buttons always hide the modal reliably
  const modalEl = document.getElementById('autosniperModal');
  if (modalEl) {
    // bind any element inside the modal that has data-bs-dismiss="modal"
    modalEl.querySelectorAll('[data-bs-dismiss="modal"]').forEach(el => {
      // remove existing to avoid duplicate
      el.removeEventListener('click', hideModal);
      el.addEventListener('click', (ev) => {
        // allow bootstrap handler to run, then ensure modal is hidden
        // use timeout to run after bootstrap's hide if present
        setTimeout(() => hideModal(), 10);
      });
    });

    // Also bind explicitly to the close button(s) and common cancel buttons as a fallback
    modalEl.querySelectorAll('.btn-close, .modal-footer .btn-secondary, button[aria-label="Close"]').forEach(el => {
      el.removeEventListener('click', hideModal);
      el.addEventListener('click', (ev) => {
        ev.preventDefault();
        setTimeout(() => hideModal(), 5);
      });
    });

    // Also bind the modal backdrop click (if using custom fallback), and Escape key
    modalEl.addEventListener('click', (ev) => {
      // if click target is the modal itself (backdrop area in our fallback), hide it
      if (ev.target === modalEl) hideModal();
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') hideModal();
    });
  }
}

async function onModalDelete(e) {
  // Confirm deletion
  if (!confirm('Delete this AutoSniper?')) return;
  const id = document.getElementById('modal_autosniper_id')?.value;
  if (!id) return;
  try {
    await apiFetch(`/api/autosnipe/${id}`, { method: 'DELETE' });
    hideModal();
    await loadSnipers();
  } catch (err) {
    console.error('Failed to delete', err);
    alert('Delete failed: ' + (err.body?.error || err.message));
  }
}

document.addEventListener('DOMContentLoaded', function () {
  try {
    setupBindings();
    loadSnipers();
  } catch (e) {
    console.error('Error during autosniper init', e);
  }
});
