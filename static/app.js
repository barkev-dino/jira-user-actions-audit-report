/**
 * app.js — Frontend logic for the Tickets Touched Report app.
 */

// ─────────────────────────────────────────────────────────── constants
const POLL_TIMEOUT_MS = 60 * 60 * 1000;

// ─────────────────────────────────────────────────────────── state
const state = {
  selectedUsers:  [],
  allRows:        [],
  filteredRows:   [],
  sortKey:        'timestamp',
  sortDir:        'desc',
  pollTimer:      null,
  pollStarted:    null,
  searchDebounce: null,
};

// ─────────────────────────────────────────────────────────── init
document.addEventListener('DOMContentLoaded', async () => {
  await checkStatus();
});

// ─────────────────────────────────────────────────────────── status / screen
async function checkStatus() {
  try {
    const res  = await fetch('/api/status');
    const data = await res.json();
    if (data.authenticated) {
      showReportScreen(data.display_name);
    } else {
      showSetupScreen();
    }
  } catch (e) {
    showSetupScreen();
  }
}

function showSetupScreen() {
  document.getElementById('screen-setup').classList.remove('hidden');
  document.getElementById('screen-report').classList.add('hidden');
}

function showReportScreen(displayName) {
  document.getElementById('screen-setup').classList.add('hidden');
  document.getElementById('screen-report').classList.remove('hidden');
  document.getElementById('connection-badge').textContent = `Connected as ${displayName}`;
  initDatePicker();
  restoreFormState();
  restoreSchedPanelState();
  loadGroupsUI();  // update the groups badge even while panel is closed
}

// ─────────────────────────────────────────────────────────── auth
async function handleVerify() {
  const btn   = document.getElementById('btn-verify');
  const errEl = document.getElementById('auth-error');
  errEl.classList.add('hidden');
  errEl.textContent = '';

  const siteUrl  = document.getElementById('site-url').value.trim();
  const email    = document.getElementById('email').value.trim();
  const apiToken = document.getElementById('api-token').value.trim();

  if (!siteUrl || !email || !apiToken) {
    showError(errEl, 'Please fill in all fields.');
    return;
  }

  btn.disabled    = true;
  btn.textContent = 'Verifying…';

  try {
    const res  = await fetch('/api/auth/test', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ site_url: siteUrl, email, api_token: apiToken }),
    });
    const data = await res.json();
    if (data.ok) {
      showReportScreen(data.user.display_name);
    } else {
      showError(errEl, data.error || 'Verification failed. Check your credentials.');
    }
  } catch (e) {
    showError(errEl, 'Could not reach the server. Is FastAPI running?');
  } finally {
    btn.disabled    = false;
    btn.textContent = 'Save and Verify';
  }
}

async function handleDisconnect() {
  await fetch('/api/auth/clear', { method: 'POST' });
  state.selectedUsers = [];
  state.allRows       = [];
  renderChips();
  hideResults();
  showSetupScreen();
}

// ─────────────────────────────────────────────────────────── user search
let _searchController = null;

function handleUserSearch(query) {
  clearTimeout(state.searchDebounce);
  if (query.length < 2) { hideDropdown(); return; }
  state.searchDebounce = setTimeout(() => doUserSearch(query), 280);
}

async function doUserSearch(query) {
  if (_searchController) _searchController.abort();
  _searchController = new AbortController();
  try {
    const res = await fetch(`/api/users/search?q=${encodeURIComponent(query)}`, {
      signal: _searchController.signal,
    });
    if (!res.ok) {
      if (res.status === 401) {
        hideDropdown();
        reportAuthError('Your Jira credentials are no longer valid. Please reauthenticate to continue.');
      } else {
        const err = await res.json().catch(() => ({}));
        renderDropdownError(err.detail || `Search failed (${res.status})`);
      }
      return;
    }
    const data = await res.json();
    renderDropdown(Array.isArray(data.items) ? data.items : []);
  } catch (e) {
    if (e.name !== 'AbortError') hideDropdown();
  }
}

function renderDropdown(items) {
  const ul = document.getElementById('user-dropdown');
  ul.innerHTML = '';
  const selectedIds = new Set(state.selectedUsers.map(u => u.account_id));
  const visible     = items.filter(u => !selectedIds.has(u.account_id));
  if (!visible.length) { hideDropdown(); return; }

  visible.forEach(user => {
    const li = document.createElement('li');
    li.className = 'dropdown-item';
    li.innerHTML = `
      ${user.avatar_url
        ? `<img src="${escHtml(user.avatar_url)}" class="avatar" alt="" />`
        : '<span class="avatar-placeholder"></span>'}
      <span>${escHtml(user.display_name)}</span>`;
    li.onclick = () => selectUser(user);
    ul.appendChild(li);
  });
  ul.classList.remove('hidden');
}

function renderDropdownError(message) {
  const ul = document.getElementById('user-dropdown');
  ul.innerHTML = `<li class="dropdown-item dropdown-error">${escHtml(message)}</li>`;
  ul.classList.remove('hidden');
}

function showDropdown() {
  const input = document.getElementById('user-search-input');
  if (input.value.trim().length > 0)
    document.getElementById('user-dropdown').classList.remove('hidden');
}

function hideDropdown() {
  document.getElementById('user-dropdown').classList.add('hidden');
}

function selectUser(user) {
  if (!state.selectedUsers.find(u => u.account_id === user.account_id)) {
    state.selectedUsers.push(user);
    renderChips();
    saveFormState();
  }
  document.getElementById('user-search-input').value = '';
  hideDropdown();
}

function removeUser(accountId) {
  state.selectedUsers = state.selectedUsers.filter(u => u.account_id !== accountId);
  renderChips();
  saveFormState();
}

function renderChips() {
  const container = document.getElementById('selected-users');
  container.innerHTML = '';
  state.selectedUsers.forEach(user => {
    const chip = document.createElement('div');
    chip.className = 'chip';
    chip.innerHTML = `
      ${user.avatar_url ? `<img src="${escHtml(user.avatar_url)}" class="chip-avatar" alt="" />` : ''}
      <span>${escHtml(user.display_name)}</span>
      <button class="chip-remove" onclick="removeUser('${escHtml(user.account_id)}')" title="Remove">×</button>`;
    container.appendChild(chip);
  });

  // Show count label when more than a few users are selected
  const countEl = document.getElementById('chips-count');
  if (countEl) {
    const n = state.selectedUsers.length;
    countEl.textContent = n > 3 ? `${n} users selected` : '';
  }
}

document.addEventListener('click', e => {
  const wrapper = document.querySelector('.user-search-wrapper');
  if (wrapper && !wrapper.contains(e.target)) hideDropdown();
});

// ─────────────────────────────────────────────────────────── form state persistence

const FORM_STATE_KEY = 'tickets_touched_form_state';

function saveFormState() {
  const state_data = {
    users:       state.selectedUsers,
    rangeKey:    document.getElementById('range-select')?.value || '7d',
    projectKeys: document.getElementById('project-filter')?.value || '',
    dateStart:   document.getElementById('date-start')?.value || '',
    dateEnd:     document.getElementById('date-end')?.value || '',
  };
  try {
    localStorage.setItem(FORM_STATE_KEY, JSON.stringify(state_data));
  } catch (_) {}
}

function restoreFormState() {
  let saved;
  try {
    const raw = localStorage.getItem(FORM_STATE_KEY);
    if (!raw) return;
    saved = JSON.parse(raw);
  } catch (_) { return; }

  if (saved.users && Array.isArray(saved.users) && saved.users.length) {
    state.selectedUsers = saved.users;
    renderChips();
  }

  if (saved.rangeKey) {
    const sel = document.getElementById('range-select');
    if (sel) {
      sel.value = saved.rangeKey;
      handleRangeChange(saved.rangeKey);
    }
  }

  if (saved.projectKeys) {
    const el = document.getElementById('project-filter');
    if (el) el.value = saved.projectKeys;
  }

  if (saved.rangeKey === 'custom') {
    if (saved.dateStart) {
      const el = document.getElementById('date-start');
      if (el) el.value = saved.dateStart;
    }
    if (saved.dateEnd) {
      const el = document.getElementById('date-end');
      if (el) el.value = saved.dateEnd;
    }
  }
}

function clearFormState() {
  try { localStorage.removeItem(FORM_STATE_KEY); } catch (_) {}
  state.selectedUsers = [];
  renderChips();
  const sel = document.getElementById('range-select');
  if (sel) { sel.value = '7d'; handleRangeChange('7d'); }
  const proj = document.getElementById('project-filter');
  if (proj) proj.value = '';
  const start = document.getElementById('date-start');
  const end   = document.getElementById('date-end');
  if (start) start.value = offsetDateStr(7);
  if (end)   end.value   = todayStr();
  document.getElementById('user-search-input').value = '';
  hideDropdown();
}

// ─────────────────────────────────────────────────────────── date picker

/** Return today's date as "YYYY-MM-DD" in local time. */
function todayStr() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

/** Return the date N days before today as "YYYY-MM-DD" in local time. */
function offsetDateStr(daysBack) {
  const d = new Date();
  d.setDate(d.getDate() - daysBack);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

/** Initialise date inputs with sensible defaults on first load. */
function initDatePicker() {
  const today = todayStr();
  const startEl = document.getElementById('date-start');
  const endEl   = document.getElementById('date-end');
  if (startEl && !startEl.value) startEl.value = offsetDateStr(7);
  if (endEl   && !endEl.value)   endEl.value   = today;
  // Cap the end date at today to prevent future selections
  if (endEl) endEl.max = today;
}

/** Show/hide custom date inputs when the range preset select changes. */
function handleRangeChange(value) {
  const dateInputsEl = document.getElementById('date-inputs');
  const dateErrorEl  = document.getElementById('date-error');
  if (value === 'custom') {
    dateInputsEl.classList.remove('hidden');
  } else {
    dateInputsEl.classList.add('hidden');
    if (dateErrorEl) {
      dateErrorEl.classList.add('hidden');
      dateErrorEl.textContent = '';
    }
  }
  saveFormState();
}

// ─────────────────────────────────────────────────────────── report generation
async function handleGenerate() {
  clearReportError();

  if (!state.selectedUsers.length) {
    alert('Please select at least one user.');
    return;
  }

  // ── Parse and validate optional project filter ────────────────────────────
  const projectErrorEl = document.getElementById('project-error');
  if (projectErrorEl) { projectErrorEl.classList.add('hidden'); projectErrorEl.textContent = ''; }

  const projectRaw  = (document.getElementById('project-filter')?.value || '').trim();
  const projectKeys = projectRaw
    ? projectRaw.split(',').map(k => k.trim().toUpperCase()).filter(k => k.length > 0)
    : [];
  const invalidKeys = projectKeys.filter(k => !/^[A-Z0-9_]+$/.test(k));
  if (invalidKeys.length > 0) {
    if (projectErrorEl) {
      projectErrorEl.textContent =
        `Invalid project key(s): ${invalidKeys.join(', ')} — use letters and numbers only (e.g. KAN, OPS).`;
      projectErrorEl.classList.remove('hidden');
    }
    return;
  }

  const rangeKey    = document.getElementById('range-select').value;
  const dateErrorEl = document.getElementById('date-error');

  // Validate custom date range
  if (rangeKey === 'custom') {
    const startVal = (document.getElementById('date-start').value || '').trim();
    const endVal   = (document.getElementById('date-end').value   || '').trim();
    if (!startVal || !endVal) {
      if (dateErrorEl) { dateErrorEl.textContent = 'Please enter both a start and end date.'; dateErrorEl.classList.remove('hidden'); }
      return;
    }
    if (startVal > endVal) {
      if (dateErrorEl) { dateErrorEl.textContent = 'Start date must not be after end date.'; dateErrorEl.classList.remove('hidden'); }
      return;
    }
    if (dateErrorEl) { dateErrorEl.classList.add('hidden'); dateErrorEl.textContent = ''; }
  }

  setFormDisabled(true);
  showLoadingArea();
  hideResults();

  const body = {
    account_ids:       state.selectedUsers.map(u => u.account_id),
    display_names:     Object.fromEntries(state.selectedUsers.map(u => [u.account_id, u.display_name])),
    range_key:         rangeKey,
    tz_offset_minutes: new Date().getTimezoneOffset(),
    project_keys:      projectKeys,   // [] means no filter — search all projects
  };

  if (rangeKey === 'custom') {
    body.start_date = document.getElementById('date-start').value;
    body.end_date   = document.getElementById('date-end').value;
  }

  try {
    const res = await fetch('/api/report/start', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    if (!res.ok) {
      if (res.status === 401) {
        reportAuthError('Your Jira credentials are no longer valid. Please reauthenticate to continue.');
      } else {
        const err = await res.json().catch(() => ({}));
        reportError(err.detail || `Server error ${res.status}`);
      }
      return;
    }
    const data = await res.json();
    if (data.job_id) {
      startPolling(data.job_id);
    } else {
      reportError('Failed to start job.');
    }
  } catch (e) {
    reportError('Could not reach the server.');
  }
}

// ─────────────────────────────────────────────────────────── polling
function startPolling(jobId) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollStarted = Date.now();
  state.pollTimer   = setInterval(() => pollJob(jobId), 800);
}

async function pollJob(jobId) {
  if (Date.now() - state.pollStarted > POLL_TIMEOUT_MS) {
    clearInterval(state.pollTimer);
    reportError('Report timed out after 60 minutes. The server may be unresponsive.');
    return;
  }

  let data;
  try {
    const res = await fetch(`/api/report/${jobId}`);
    if (!res.ok) {
      clearInterval(state.pollTimer);
      reportError(`Server error ${res.status} while polling job.`);
      return;
    }
    data = await res.json();
  } catch (e) {
    return; // network blip — keep polling
  }

  if (data.status === 'running' || data.status === 'pending') {
    updateLoadingUI(data.step || 'Working…', data.progress || 0);
    return;
  }

  clearInterval(state.pollTimer);

  if (data.status === 'done') {
    hideLoadingArea();
    setFormDisabled(false);
    renderResults(data.rows || [], data.window_start, data.window_end);
  } else {
    const errMsg = data.error || 'Report job failed.';
    if (errMsg.startsWith('AUTH:')) {
      reportAuthError(errMsg.slice(5).trim());
    } else {
      reportError(errMsg);
    }
  }
}

function updateLoadingUI(step, progress) {
  const raw    = step || '';
  const parts  = raw.split('|');
  const ctrEl  = document.getElementById('loading-counter');
  const stepEl = document.getElementById('loading-step');

  if (parts.length >= 3) {
    // Structured: "N/M | KEY | detail"
    ctrEl.textContent  = `Issue ${parts[0].trim()}`;
    stepEl.textContent = `${parts[1].trim()} · ${parts[2].trim()}`;
  } else {
    ctrEl.textContent  = raw || 'Working…';
    stepEl.textContent = '\u00a0';  // non-breaking space keeps height stable
  }
  document.getElementById('progress-bar').style.width = `${progress || 0}%`;
}

function reportError(msg) {
  clearInterval(state.pollTimer);
  hideLoadingArea();
  setFormDisabled(false);
  const el = document.getElementById('report-error');
  if (el) {
    el.textContent = `Error: ${msg}`;
    el.classList.remove('hidden');
  }
}

function clearReportError() {
  const el = document.getElementById('report-error');
  if (el) { el.textContent = ''; el.classList.add('hidden'); }
}

// Show the auth-failure banner (credentials revoked / 401 / 403).
// Does NOT immediately redirect — lets the user read the message and
// click "Go to Setup" when ready.
function reportAuthError(msg) {
  clearInterval(state.pollTimer);
  hideLoadingArea();
  setFormDisabled(false);
  const banner = document.getElementById('auth-error-banner');
  const msgEl  = document.getElementById('auth-error-msg');
  if (msgEl) {
    msgEl.textContent = msg ||
      'Your saved Jira credentials are no longer valid or no longer have access. Please reauthenticate to continue.';
  }
  if (banner) banner.classList.remove('hidden');
}

// "Go to Setup" button inside the auth error banner.
// Clears saved credentials so the setup form doesn't pre-fill stale data,
// hides the banner, and returns the UI to the initial auth screen.
async function handleReauth() {
  try { await fetch('/api/auth/clear', { method: 'POST' }); } catch (_) {}
  document.getElementById('auth-error-banner').classList.add('hidden');
  clearInterval(state.pollTimer);
  hideLoadingArea();
  hideResults();
  setFormDisabled(false);
  state.selectedUsers = [];
  state.allRows       = [];
  renderChips();
  showSetupScreen();
}

// ─────────────────────────────────────────────────────────── results

/**
 * Render the active filter window beneath the Summary title.
 * windowStart / windowEnd are UTC ISO strings from the backend.
 * They are displayed in the browser's local timezone automatically.
 */
function renderWindowDisplay(windowStart, windowEnd) {
  const el = document.getElementById('window-display');
  if (!el) return;
  if (!windowStart || !windowEnd) { el.textContent = ''; return; }
  const fmt = d => new Date(d).toLocaleString(undefined, {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
  });
  el.textContent = `Filter window: ${fmt(windowStart)} – ${fmt(windowEnd)}`;
}

function renderResults(rows, windowStart, windowEnd) {
  const safeRows     = Array.isArray(rows) ? rows : [];
  state.allRows      = safeRows;
  state.filteredRows = safeRows;
  state.sortKey      = 'timestamp';
  state.sortDir      = 'desc';
  document.getElementById('table-filter').value = '';
  renderWindowDisplay(windowStart, windowEnd);
  renderTable();
  document.getElementById('results-area').classList.remove('hidden');
}

// ── Natural compare for issue keys (KAN-2 before KAN-11) and timestamps ──
function compareRows(a, b, key) {
  if (key === 'issue_key') {
    const parse = s => {
      const m = String(s || '').match(/^([^-\s]+)-(\d+)$/);
      return m ? [m[1], parseInt(m[2], 10)] : [String(s || ''), 0];
    };
    const [ap, an] = parse(a[key]);
    const [bp, bn] = parse(b[key]);
    if (ap !== bp) return ap < bp ? -1 : 1;
    return an - bn;
  }
  if (key === 'timestamp') {
    return new Date(a[key] || 0) - new Date(b[key] || 0);
  }
  const av = String(a[key] || '').toLowerCase();
  const bv = String(b[key] || '').toLowerCase();
  return av < bv ? -1 : av > bv ? 1 : 0;
}

function renderTable() {
  const rows    = state.filteredRows;
  const tbody   = document.getElementById('results-tbody');
  const countEl = document.getElementById('results-count');
  if (countEl) countEl.textContent = `${rows.length} result${rows.length !== 1 ? 's' : ''}`;
  tbody.innerHTML = '';

  rows.forEach(row => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="col-ts">${escHtml(formatTimestamp(row.timestamp))}</td>
      <td>${escHtml(row.user)}</td>
      <td><a href="${escHtml(row.issue_url)}" target="_blank" rel="noopener" class="issue-link">${escHtml(row.issue_key)}</a></td>
      <td><span class="pill pill-${escHtml(row.action_type)}">${escHtml(formatActionType(row.action_type))}</span></td>
      <td class="col-details">${escHtml(row.details)}</td>
      <td>${escHtml(row.project)}</td>
      <td><a href="${escHtml(row.issue_url)}" target="_blank" rel="noopener" class="ext-link" title="Open in Jira">↗</a></td>`;
    tbody.appendChild(tr);
  });

  renderSummary(rows);
}

function sortBy(key) {
  state.sortDir = (state.sortKey === key)
    ? (state.sortDir === 'asc' ? 'desc' : 'asc')
    : (key === 'timestamp' ? 'desc' : 'asc');
  state.sortKey = key;

  const dir = state.sortDir === 'asc' ? 1 : -1;
  state.filteredRows = [...state.filteredRows].sort((a, b) => compareRows(a, b, key) * dir);
  renderTable();
}

function filterTable(query) {
  const q = query.toLowerCase();
  state.filteredRows = q
    ? state.allRows.filter(row => Object.values(row).some(v => String(v).toLowerCase().includes(q)))
    : [...state.allRows];
  renderTable();
}

// ─────────────────────────────────────────────────────────── action type formatting

// All known action types in display order — always shown in summary even if count is 0
// All action types shown in the summary table (in display order).
// Resolved and Closed are not separate categories — those transitions
// are classified as status_change like any other status transition.
// All action types shown in the summary table (in display order).
// Status transitions (including to Resolved, Closed, or Reopened) are all
// classified as status_change — no separate categories for those states.
const ALL_ACTION_TYPES = [
  'created', 'updated', 'status_change', 'commented',
  'assigned', 'attachment', 'linked', 'logged_work',
];

function formatActionType(type) {
  return String(type || '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase());
}

// ─────────────────────────────────────────────────────────── summary metrics
function computeSummary(rows) {
  // Always show all known action types; append any unexpected ones at the end
  const presentTypes = new Set(rows.map(r => r.action_type));
  const extraTypes   = [...presentTypes].filter(at => !ALL_ACTION_TYPES.includes(at)).sort();
  const actionTypes  = [...ALL_ACTION_TYPES, ...extraTypes];
  const users        = [...new Set(rows.map(r => r.user))].sort();

  const counts = {};
  for (const u of users) {
    counts[u] = {};
    for (const at of actionTypes) counts[u][at] = 0;
  }
  for (const row of rows) {
    counts[row.user][row.action_type] = (counts[row.user][row.action_type] || 0) + 1;
  }

  const totByUser = {};
  for (const u of users)
    totByUser[u] = actionTypes.reduce((s, at) => s + (counts[u][at] || 0), 0);

  const totByAction = {};
  for (const at of actionTypes)
    totByAction[at] = users.reduce((s, u) => s + (counts[u][at] || 0), 0);

  return { users, actionTypes, counts, totByUser, totByAction, grand: rows.length };
}

function computeTimeSpan(rows) {
  if (!rows.length) return null;
  let min = Infinity, max = -Infinity;
  for (const row of rows) {
    const t = new Date(row.timestamp).getTime();
    if (isNaN(t)) continue;
    if (t < min) min = t;
    if (t > max) max = t;
  }
  if (!isFinite(min)) return null;
  return { min: new Date(min), max: new Date(max) };
}

function renderSummary(rows) {
  const wrap   = document.getElementById('summary-table-wrap');
  const spanEl = document.getElementById('summary-timespan');

  const span = computeTimeSpan(rows);
  if (span) {
    const fmt = d => d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    spanEl.textContent = `${fmt(span.min)} – ${fmt(span.max)}`;
  } else {
    spanEl.textContent = '';
  }

  if (!rows.length) {
    wrap.innerHTML = '<p class="summary-empty">No data</p>';
    return;
  }

  const { users, actionTypes, counts, totByUser, totByAction, grand } = computeSummary(rows);

  const table = document.createElement('table');
  table.className = 'summary-tbl';

  const thead = table.createTHead();
  const hrow  = thead.insertRow();
  hrow.innerHTML = `<th class="sth-user">User</th>` +
    actionTypes.map(at => `<th class="sth-action"><span class="pill pill-${escHtml(at)}">${escHtml(formatActionType(at))}</span></th>`).join('') +
    `<th class="sth-total">Total</th>`;

  const tbody = table.createTBody();
  for (const user of users) {
    const tr = tbody.insertRow();
    tr.innerHTML = `<td class="std-user">${escHtml(user)}</td>` +
      actionTypes.map(at => {
        const n = counts[user][at] || 0;
        return `<td class="std-count${n ? '' : ' std-zero'}">${n || '–'}</td>`;
      }).join('') +
      `<td class="std-total">${totByUser[user]}</td>`;
  }

  const tfoot = table.createTFoot();
  const frow  = tfoot.insertRow();
  frow.className = 'summary-totals-row';
  frow.innerHTML = `<td class="std-user stf-label">Total</td>` +
    actionTypes.map(at => `<td class="std-count std-total">${totByAction[at]}</td>`).join('') +
    `<td class="std-total std-grand">${grand}</td>`;

  wrap.innerHTML = '';
  wrap.appendChild(table);
}

// ─────────────────────────────────────────────────────────── export
function exportCSV() {
  const q = v => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const rows = [];

  // ── Section 1: Summary matrix ─────────────────────────────────────────────
  if (state.allRows.length) {
    const { users, actionTypes, counts, totByUser, totByAction, grand } =
      computeSummary(state.allRows);

    rows.push([q('SUMMARY')].join(','));
    rows.push(['User', ...actionTypes.map(formatActionType), 'Total'].map(q).join(','));

    for (const user of users) {
      rows.push(
        [user, ...actionTypes.map(at => counts[user][at] || 0), totByUser[user]]
          .map(q).join(',')
      );
    }
    rows.push(
      ['Total', ...actionTypes.map(at => totByAction[at]), grand].map(q).join(',')
    );

    // Blank separator row
    rows.push('');
  }

  // ── Section 2: Activity detail ────────────────────────────────────────────
  rows.push([q('ACTIVITY DETAIL')].join(','));
  rows.push(['Timestamp', 'User', 'Issue Key', 'Action Type', 'Details', 'Project', 'Issue URL'].map(q).join(','));
  for (const r of state.filteredRows) {
    rows.push(
      [r.timestamp, r.user, r.issue_key, r.action_type, r.details, r.project, r.issue_url]
        .map(q).join(',')
    );
  }

  const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = `tickets_touched_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ─────────────────────────────────────────────────────────── UI helpers
function setFormDisabled(disabled) {
  ['btn-generate', 'range-select', 'user-search-input', 'date-start', 'date-end', 'project-filter']
    .forEach(id => {
      const el = document.getElementById(id);
      if (el) el.disabled = disabled;
    });
}

function showLoadingArea() {
  document.getElementById('loading-area').classList.remove('hidden');
  document.getElementById('loading-counter').textContent = 'Starting…';
  document.getElementById('loading-step').textContent    = '\u00a0';
  document.getElementById('progress-bar').style.width   = '0%';
}

function hideLoadingArea() {
  document.getElementById('loading-area').classList.add('hidden');
}

function hideResults() {
  document.getElementById('results-area').classList.add('hidden');
}

function showError(el, msg) {
  el.textContent = msg;
  el.classList.remove('hidden');
}

function formatTimestamp(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch (e) { return iso; }
}

function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

// ─────────────────────────────────────────────────────────── scheduler UI

let _schedPollTimer = null;
const SCHED_POLL_MS  = 20_000; // refresh status every 20 s while panel is open
const SCHED_STATE_KEY = 'tickets_touched_sched_state';

/** Persist scheduler field values + panel-open state to localStorage. */
function saveSchedDraft() {
  try {
    localStorage.setItem(SCHED_STATE_KEY, JSON.stringify({
      open:      true,
      enabled:   document.getElementById('sched-enabled')?.checked ?? false,
      run_time:  document.getElementById('sched-time')?.value  || '07:00',
      run_until: document.getElementById('sched-until')?.value || '',
    }));
  } catch (_) {}
}

/** Restore panel open/closed state and field values on page load. */
function restoreSchedPanelState() {
  let saved;
  try {
    const raw = localStorage.getItem(SCHED_STATE_KEY);
    if (!raw) return;
    saved = JSON.parse(raw);
  } catch (_) { return; }

  if (!saved || !saved.open) return;

  // Expand the panel
  const body    = document.getElementById('sched-body');
  const chevron = document.getElementById('sched-chevron');
  body.classList.remove('hidden');
  chevron.classList.add('open');

  // Restore draft field values before fetching from backend
  // (loadScheduleUI will overwrite with saved config if one exists)
  if (saved.run_time)  document.getElementById('sched-time').value  = saved.run_time;
  if (saved.run_until) document.getElementById('sched-until').value = saved.run_until;
  document.getElementById('sched-enabled').checked = !!saved.enabled;
  updateSchedBadge(saved.enabled, saved.run_until);

  // Fetch authoritative config from backend and start polling
  loadScheduleUI();
  _schedPollTimer = setInterval(loadScheduleUI, SCHED_POLL_MS);
}

/** Toggle the scheduler panel open/closed. */
function toggleScheduler() {
  const body    = document.getElementById('sched-body');
  const chevron = document.getElementById('sched-chevron');
  const isHidden = body.classList.toggle('hidden');
  chevron.classList.toggle('open', !isHidden);
  if (!isHidden) {
    loadScheduleUI();
    _schedPollTimer = setInterval(loadScheduleUI, SCHED_POLL_MS);
    saveSchedDraft();
  } else {
    clearInterval(_schedPollTimer);
    _schedPollTimer = null;
    try { localStorage.removeItem(SCHED_STATE_KEY); } catch (_) {}
  }
}

/** Load saved schedule config from the backend and populate the form. */
async function loadScheduleUI() {
  try {
    const res  = await fetch('/api/schedule');
    if (!res.ok) return;
    const data = await res.json();
    if (!data || !data.config) return;
    const cfg = data.config;

    document.getElementById('sched-enabled').checked = !!cfg.enabled;
    if (cfg.run_time)  document.getElementById('sched-time').value  = cfg.run_time;
    if (cfg.run_until) document.getElementById('sched-until').value = cfg.run_until;

    updateSchedBadge(cfg.enabled, cfg.run_until);
    renderSchedStatus(data.last_run || null, data.next_run || null);
  } catch (_) {}
}

/** Reflect the enabled state in the header badge. */
function handleSchedEnabledChange() {
  const enabled  = document.getElementById('sched-enabled').checked;
  const runUntil = document.getElementById('sched-until').value;
  updateSchedBadge(enabled, runUntil);
}

function updateSchedBadge(enabled, runUntil) {
  const badge = document.getElementById('sched-status-badge');
  if (!badge) return;
  const today   = new Date().toISOString().slice(0, 10);
  const expired = runUntil && runUntil < today;
  if (!enabled) {
    badge.textContent = 'Off';
    badge.className   = 'sched-badge sched-badge-off';
  } else if (expired) {
    badge.textContent = 'Expired';
    badge.className   = 'sched-badge sched-badge-expired';
  } else {
    badge.textContent = 'On';
    badge.className   = 'sched-badge sched-badge-on';
  }
}

/**
 * Save the schedule — reads users, projects, and range directly from the
 * main report form so the scheduler always mirrors what the user has set up.
 */
async function saveSchedule() {
  if (!state.selectedUsers.length) {
    alert('Please select at least one user in the report form above before saving the schedule.');
    return;
  }

  const runUntil = document.getElementById('sched-until').value;
  if (!runUntil) {
    alert('Please set a Run Until date.');
    return;
  }

  // Read project keys from the main form
  const projectRaw  = (document.getElementById('project-filter')?.value || '').trim();
  const projectKeys = projectRaw
    ? projectRaw.split(',').map(k => k.trim().toUpperCase()).filter(k => k.length > 0)
    : [];

  const config = {
    enabled:       document.getElementById('sched-enabled').checked,
    run_time:      document.getElementById('sched-time').value || '07:00',
    run_until:     runUntil,
    range_key:     document.getElementById('range-select').value || '1d',
    account_ids:   state.selectedUsers.map(u => u.account_id),
    display_names: Object.fromEntries(state.selectedUsers.map(u => [u.account_id, u.display_name])),
    project_keys:  projectKeys,
  };

  try {
    const res = await fetch('/api/schedule', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(config),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`Failed to save: ${err.detail || res.status}`);
      return;
    }
    updateSchedBadge(config.enabled, config.run_until);
    // Reload status so next-run time reflects the newly armed job
    await loadScheduleUI();
    // Visual feedback: flash the Save button green
    const btn = document.getElementById('sched-save-btn');
    if (btn) {
      btn.textContent = 'Saved ✓';
      btn.classList.add('sched-save-btn-success');
      setTimeout(() => {
        btn.textContent = 'Save Schedule';
        btn.classList.remove('sched-save-btn-success');
      }, 2000);
    }
  } catch (e) {
    alert('Could not reach the server.');
  }
}

/** Render last-run and next-run info in the status line. Always shows something. */
function renderSchedStatus(lastRun, nextRunIso) {
  const el = document.getElementById('sched-status-line');
  if (!el) return;

  const fmtDateTime = iso => new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });

  const nextPart = nextRunIso
    ? `Next run: ${fmtDateTime(nextRunIso)}`
    : 'Not scheduled';

  if (!lastRun || !lastRun.last_run_at) {
    el.textContent = `Not yet run  ·  ${nextPart}`;
    el.className   = 'sched-status-line';
    el.classList.remove('hidden');
    return;
  }

  const when = fmtDateTime(lastRun.last_run_at);
  if (lastRun.last_run_ok) {
    el.textContent = `Last ran: ${when} — ${lastRun.last_run_rows} row(s) saved to reports/  ·  ${nextPart}`;
    el.className   = 'sched-status-line ok';
  } else {
    el.textContent = `Last run failed (${when}): ${lastRun.last_error || 'unknown error'}  ·  ${nextPart}`;
    el.className   = 'sched-status-line error';
  }
  el.classList.remove('hidden');
}

// ─────────────────────────────────────────────────────────── user groups

/** Toggle the groups panel open/closed. */
function toggleGroups() {
  const body    = document.getElementById('groups-body');
  const chevron = document.getElementById('groups-chevron');
  const isHidden = body.classList.toggle('hidden');
  chevron.classList.toggle('open', !isHidden);
  if (!isHidden) loadGroupsUI();
}

/** Fetch all groups from the server and render them. */
async function loadGroupsUI() {
  try {
    const res  = await fetch('/api/groups');
    if (!res.ok) return;
    const data = await res.json();
    renderGroups(data.groups || []);
  } catch (_) {}
}

/** Render the list of group cards. */
function renderGroups(groups) {
  const list    = document.getElementById('groups-list');
  const badge   = document.getElementById('groups-count-badge');
  const newBtn  = document.getElementById('btn-new-group');
  const capNote = document.getElementById('groups-cap-note');
  if (!list) return;

  if (badge) {
    badge.textContent = `${groups.length} saved`;
    badge.className   = groups.length ? 'sched-badge sched-badge-on' : 'sched-badge sched-badge-off';
  }

  const atCap = groups.length >= 5;
  if (newBtn)  newBtn.disabled = atCap;
  if (capNote) capNote.classList.toggle('hidden', !atCap);

  list.innerHTML = '';
  if (!groups.length) {
    list.innerHTML = '<p style="font-size:12px;color:#6b778c;font-style:italic;">No groups saved yet.</p>';
    return;
  }

  groups.forEach(group => {
    const memberNames = (group.account_ids || [])
      .map(id => (group.display_names || {})[id] || id)
      .join(', ') || 'No members';

    const card = document.createElement('div');
    card.className  = 'group-card';
    card.dataset.id = group.id;
    card.innerHTML  = `
      <div class="group-card-top">
        <input
          class="group-name-input"
          type="text"
          value="${escHtml(group.name)}"
          maxlength="40"
          aria-label="Group name"
          onchange="renameGroup('${escHtml(group.id)}', this.value)"
        />
        <div class="group-actions">
          <button class="btn btn-ghost btn-sm" onclick="loadGroup('${escHtml(group.id)}')">↩ Load</button>
          <button class="btn btn-ghost btn-sm group-save-btn" onclick="saveGroup('${escHtml(group.id)}')">Save</button>
          <button class="btn btn-ghost btn-sm group-delete-btn" onclick="deleteGroup('${escHtml(group.id)}')">✕ Delete</button>
        </div>
      </div>
      <div class="group-members">${escHtml(memberNames)}</div>
      <div class="group-member-count">${(group.account_ids || []).length} member${(group.account_ids || []).length !== 1 ? 's' : ''}</div>
    `;
    list.appendChild(card);
  });
}

/** Create a new group from the current chip selection. */
async function createGroup() {
  if (!state.selectedUsers.length) {
    alert('Please select at least one user before creating a group.');
    return;
  }
  try {
    const res = await fetch('/api/groups', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(_groupPayload('New Group')),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || 'Could not create group.');
      return;
    }
    await loadGroupsUI();
  } catch (_) {
    alert('Could not reach the server.');
  }
}

/** Load a group — populate chips with its members. */
async function loadGroup(groupId) {
  try {
    const res  = await fetch('/api/groups');
    if (!res.ok) return;
    const data = await res.json();
    const group = (data.groups || []).find(g => g.id === groupId);
    if (!group) return;

    state.selectedUsers = (group.account_ids || []).map(id => ({
      account_id:   id,
      display_name: (group.display_names || {})[id] || id,
      avatar_url:   (group.avatar_urls   || {})[id] || null,
    }));
    renderChips();
    saveFormState();

    const btn = document.querySelector(`.group-card[data-id="${groupId}"] .btn`);
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = '✓ Loaded';
      btn.classList.add('sched-save-btn-success');
      setTimeout(() => { btn.textContent = orig; btn.classList.remove('sched-save-btn-success'); }, 1500);
    }
  } catch (_) {}
}

/** Save current chip selection back to an existing group. */
async function saveGroup(groupId) {
  if (!state.selectedUsers.length) {
    alert('Please select at least one user before saving the group.');
    return;
  }
  const nameInput = document.querySelector(`.group-card[data-id="${groupId}"] .group-name-input`);
  const name      = nameInput ? nameInput.value.trim() || 'New Group' : 'New Group';

  try {
    const res = await fetch('/api/groups', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ id: groupId, ..._groupPayload(name) }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || 'Could not save group.');
      return;
    }
    await loadGroupsUI();
    const btn = document.querySelector(`.group-card[data-id="${groupId}"] .group-save-btn`);
    if (btn) {
      btn.textContent = 'Saved ✓';
      btn.classList.add('sched-save-btn-success');
      setTimeout(() => { btn.textContent = 'Save'; btn.classList.remove('sched-save-btn-success'); }, 1500);
    }
  } catch (_) {
    alert('Could not reach the server.');
  }
}

/** Persist an inline name change without touching members. */
async function renameGroup(groupId, name) {
  try {
    const res  = await fetch('/api/groups');
    const data = await res.json().catch(() => ({ groups: [] }));
    const group = (data.groups || []).find(g => g.id === groupId);
    if (!group) return;
    await fetch('/api/groups', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        id:            groupId,
        name:          name.trim() || 'New Group',
        account_ids:   group.account_ids,
        display_names: group.display_names,
        avatar_urls:   group.avatar_urls,
      }),
    });
  } catch (_) {}
}

/** Delete a group after confirmation. */
async function deleteGroup(groupId) {
  if (!confirm('Delete this group?')) return;
  try {
    await fetch(`/api/groups/${groupId}`, { method: 'DELETE' });
    await loadGroupsUI();
  } catch (_) {
    alert('Could not reach the server.');
  }
}

/** Build the common group payload from current chip state. */
function _groupPayload(name) {
  return {
    name,
    account_ids:   state.selectedUsers.map(u => u.account_id),
    display_names: Object.fromEntries(state.selectedUsers.map(u => [u.account_id, u.display_name])),
    avatar_urls:   Object.fromEntries(state.selectedUsers.map(u => [u.account_id, u.avatar_url || null])),
  };
}
