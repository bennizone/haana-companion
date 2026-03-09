// logs.js – Konversations-Log Viewer (strukturierte Tag-Karten)
// v6 – Unified with Conversations panel; _logCurrentInst mirrors currentInstance

// ── State ────────────────────────────────────────────────────────────────────
// NOTE: _logCurrentInst is kept as an alias — app.js sets it before calling
// loadLogDays() so the two variables stay in sync.
let _logCurrentInst   = '__all__';   // mirrors currentInstance in archiv mode
let _logAllFiles      = [];          // all loaded log file records
let _logSearchTimer   = null;        // debounce handle

// ── Legacy Init stub (called from nowhere now, kept for safety) ───────────
function initLogView() {
  // No-op: unified view is managed by app.js initConversationsView()
}

// ── Instance Tab (legacy, kept for external callers) ─────────────────────
function selectLogInstance(inst) {
  // Delegate to unified selectInstance in app.js
  selectInstance(inst);
}

// ── Date Filter ──────────────────────────────────────────────────────────────
function setLogDateRange(range) {
  const fromEl = document.getElementById('log-date-from');
  const toEl   = document.getElementById('log-date-to');
  const today  = new Date();
  const fmt    = d => d.toISOString().slice(0, 10);

  document.querySelectorAll('.log-quick-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.range === range);
  });

  if (range === 'all') {
    fromEl.value = '';
    toEl.value   = '';
  } else if (range === 'today') {
    fromEl.value = fmt(today);
    toEl.value   = fmt(today);
  } else {
    const from = new Date(today);
    from.setDate(today.getDate() - parseInt(range, 10) + 1);
    fromEl.value = fmt(from);
    toEl.value   = fmt(today);
  }
  applyLogFilters();
}

function applyLogFilters() {
  renderLogDays(_logAllFiles);
}

// ── Search ───────────────────────────────────────────────────────────────────
function debounceLogSearch() {
  if (_logSearchTimer) clearTimeout(_logSearchTimer);
  _logSearchTimer = setTimeout(() => renderLogDays(_logAllFiles), 300);
}

// ── Load Day List ─────────────────────────────────────────────────────────────
async function loadLogDays() {
  const list = document.getElementById('log-day-list');
  if (!list) return;
  list.innerHTML = `<div class="empty-state"><div class="icon">&#8230;</div><div>${t('logs.loading')}</div></div>`;

  try {
    let files = [];
    if (_logCurrentInst === '__all__') {
      const results = await Promise.all(
        INSTANCES.map(inst =>
          fetch(`/api/conversations/${inst}/files`)
            .then(r => r.json())
            .then(arr => arr.map(f => ({ ...f, instance: inst })))
            .catch(() => [])
        )
      );
      files = results.flat();
    } else {
      const r = await fetch(`/api/conversations/${_logCurrentInst}/files`);
      const arr = await r.json();
      files = arr.map(f => ({ ...f, instance: _logCurrentInst }));
    }
    _logAllFiles = files;
    renderLogDays(files);
  } catch(e) {
    list.innerHTML = `<div class="empty-state"><div class="icon">!</div><div>${escHtml(e.message)}</div></div>`;
  }
}

// ── Render Day Cards ──────────────────────────────────────────────────────────
function renderLogDays(files) {
  const list = document.getElementById('log-day-list');
  if (!list) return;

  const fromVal = document.getElementById('log-date-from')?.value || '';
  const toVal   = document.getElementById('log-date-to')?.value   || '';
  const query   = (document.getElementById('log-search')?.value || '').toLowerCase().trim();

  let filtered = files.filter(f => {
    if (fromVal && f.date < fromVal) return false;
    if (toVal   && f.date > toVal)   return false;
    return true;
  });

  const byDate = {};
  filtered.forEach(f => {
    if (!byDate[f.date]) byDate[f.date] = [];
    byDate[f.date].push(f);
  });

  const sortedDates = Object.keys(byDate).sort().reverse();

  if (!sortedDates.length) {
    list.innerHTML = `<div class="empty-state"><div class="icon">&#8212;</div><div>${t('logs.no_logs')}</div></div>`;
    return;
  }

  list.innerHTML = sortedDates.map(date => {
    const dayFiles     = byDate[date];
    const totalEntries = dayFiles.reduce((s, f) => s + (f.entries || 0), 0);
    const totalKb      = dayFiles.reduce((s, f) => s + (f.size_kb || 0), 0);

    const instanceTags = _logCurrentInst === '__all__'
      ? dayFiles.map(f => `<span class="tag" style="font-size:10px;">${escHtml(f.instance)}</span>`).join(' ')
      : '';

    const actInst = _logCurrentInst !== '__all__' ? _logCurrentInst
      : (dayFiles.length === 1 ? dayFiles[0].instance : null);

    const dayActions = actInst ? `
      <button class="btn btn-sm btn-secondary log-day-btn"
        onclick="event.stopPropagation();logDeleteDay('${escAttr(actInst)}','${escAttr(date)}')"
        title="${t('logs.day.delete')}">
        <svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor"><path d="M6 2h4a1 1 0 0 1 1 1v1H5V3a1 1 0 0 1 1-1zM3 5h10l-.8 8H3.8L3 5zm3 2v5h1V7H6zm3 0v5h1V7H9z"/></svg>
        ${t('logs.day.delete')}
      </button>
      <button class="btn btn-sm btn-secondary log-day-btn" id="rebuild-day-btn-${escAttr(date)}"
        onclick="event.stopPropagation();logRebuildDay('${escAttr(actInst)}','${escAttr(date)}')"
        title="${t('logs.day.rebuild')}">
        <svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor"><path d="M8 2a6 6 0 1 0 6 6h-2a4 4 0 1 1-4-4V2z"/><path d="M8 0l3 3-3 3V0z"/></svg>
        ${t('logs.day.rebuild')}
      </button>` : '';

    const fileRows = dayFiles.map((f, idx) => {
      const dateLabel = _formatDate(date);
      return `
        <div class="log-file-row log-alt-${idx % 2}" data-search="${escAttr(f.instance + ' ' + date)}">
          ${_logCurrentInst === '__all__' ? `<span class="tag" style="font-size:10px;flex-shrink:0;">${escHtml(f.instance)}</span>` : ''}
          <span class="log-file-date">${escHtml(dateLabel)}</span>
          <span class="log-file-meta">
            <span class="log-file-entries">${f.entries || 0} ${t('logs.day.messages')}</span>
            ${f.size_kb ? `<span class="log-file-size">${f.size_kb} KB</span>` : ''}
          </span>
          <button class="btn btn-sm btn-secondary log-day-btn"
            onclick="event.stopPropagation();openLogEditor('${escAttr(f.instance)}','${escAttr(f.date)}')"
            title="${f.entries} ${t('logs.entries')}, ${f.size_kb||0} KB">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor"><path d="M11.5 1.5a1.5 1.5 0 0 1 2.1 2.1L5 12.2l-3 .8.8-3 8.7-8.5z"/></svg>
            ${t('logs.entries')}
          </button>
        </div>`;
    }).join('');

    return `
    <div class="log-day-card" id="log-day-${escAttr(date)}">
      <div class="log-day-header" onclick="toggleLogDay('${escAttr(date)}')">
        <svg class="log-day-chevron" viewBox="0 0 16 16" width="14" height="14" fill="currentColor">
          <path d="M6 4l4 4-4 4V4z"/>
        </svg>
        <span class="log-day-date">${_formatDateLong(date)}</span>
        ${instanceTags}
        <span class="log-day-count">${totalEntries} ${t('logs.day.messages')}</span>
        ${totalKb ? `<span class="log-file-size">${totalKb} KB</span>` : ''}
        <div class="log-day-actions" onclick="event.stopPropagation()">
          ${dayActions}
        </div>
      </div>
      <div class="log-day-body" id="log-day-body-${escAttr(date)}" style="display:none;">
        ${fileRows}
      </div>
    </div>`;
  }).join('');

  if (query) {
    list.querySelectorAll('.log-day-card').forEach(card => {
      const text = card.textContent.toLowerCase();
      card.style.display = text.includes(query) ? '' : 'none';
    });
  }
}

function toggleLogDay(date) {
  const body = document.getElementById(`log-day-body-${date}`);
  const card = document.getElementById(`log-day-${date}`);
  if (!body || !card) return;
  const open = body.style.display !== 'none';
  body.style.display = open ? 'none' : '';
  card.classList.toggle('log-day-open', !open);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function _modalConfirmPromise(title, message) {
  return new Promise(resolve => {
    Modal.show({
      title,
      body: `<p class="modal-message">${escHtml(message)}</p>`,
      onConfirm: () => resolve(true),
      onCancel:  () => resolve(false),
      confirmClass: 'btn-danger',
    });
  });
}

// ── Day Actions ───────────────────────────────────────────────────────────────
async function logDeleteDay(inst, date) {
  const ok = await _modalConfirmPromise(
    `${t('logs.day.delete')}: ${inst} / ${date}`,
    `${t('logs.day.delete')}: ${inst} / ${date}?`
  );
  if (!ok) return;
  try {
    const r = await fetch(`/api/logs/day/${encodeURIComponent(inst)}/${encodeURIComponent(date)}`, { method: 'DELETE' });
    const d = await r.json();
    if (d.ok || r.ok) {
      toast(t('logs.deleted_success').replace('{count}', 1), 'ok');
      loadLogDays();
    } else {
      toast(d.error || t('logs.error'), 'error');
    }
  } catch(e) {
    toast(e.message, 'error');
  }
}

async function logRebuildDay(inst, date) {
  const btnId = `rebuild-day-btn-${date}`;
  const btn   = document.getElementById(btnId);
  if (btn) { btn.disabled = true; btn.textContent = t('logs.day.rebuild_running'); }
  try {
    const r = await fetch(`/api/logs/rebuild/${encodeURIComponent(inst)}/${encodeURIComponent(date)}`, { method: 'POST' });
    const d = await r.json();
    if (d.ok || r.ok) {
      toast(`${inst}/${date}: ${t('logs.day.rebuild')} OK`, 'ok');
    } else {
      toast(d.error || t('logs.error'), 'error');
    }
  } catch(e) {
    toast(e.message, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor"><path d="M8 2a6 6 0 1 0 6 6h-2a4 4 0 1 1-4-4V2z"/><path d="M8 0l3 3-3 3V0z"/></svg> ${t('logs.day.rebuild')}`;
    }
  }
}

// ── User-Level Actions ────────────────────────────────────────────────────────
function logExportUser() {
  const inst = _logCurrentInst;
  if (!inst || inst === '__all__') return;
  window.location.href = `/api/logs/export/${encodeURIComponent(inst)}`;
}

async function logDeleteAllUser() {
  const inst = _logCurrentInst;
  if (!inst || inst === '__all__') return;

  const confirmed = await _confirmTypeName(inst);
  if (!confirmed) return;

  try {
    const r = await fetch(`/api/logs/user/${encodeURIComponent(inst)}?confirm=true`, { method: 'DELETE' });
    const d = await r.json();
    if (d.ok || r.ok) {
      toast(t('logs.deleted_success').replace('{count}', d.deleted || '?'), 'ok');
      loadLogDays();
    } else {
      toast(d.error || t('logs.error'), 'error');
    }
  } catch(e) {
    toast(e.message, 'error');
  }
}

async function logCheckRebuild() {
  const inst   = _logCurrentInst;
  if (!inst || inst === '__all__') return;
  const btn    = document.getElementById('log-check-btn');
  const banner = document.getElementById('log-check-result');
  if (btn) { btn.disabled = true; btn.textContent = t('common.loading'); }
  if (banner) { banner.style.display = 'none'; banner.innerHTML = ''; }

  try {
    const r = await fetch(`/api/logs/check-rebuild/${encodeURIComponent(inst)}`, { method: 'POST' });
    const d = await r.json();
    if (r.ok) {
      const count = Array.isArray(d.changed) ? d.changed.length : (d.count || 0);
      const msg   = t('logs.user.check_result').replace('{count}', count);
      const files = Array.isArray(d.changed) && d.changed.length
        ? `<ul class="log-check-list">${d.changed.map(f => `<li>${escHtml(f)}</li>`).join('')}</ul>` : '';
      const rebuildBtn = count > 0
        ? `<button class="btn btn-sm btn-secondary" style="margin-top:8px;" onclick="logRebuildAllUser('${escAttr(inst)}')" data-i18n="logs.user.rebuild_all">${t('logs.user.rebuild_all')}</button>`
        : '';
      if (banner) {
        banner.innerHTML = `<div class="log-check-msg">${escHtml(msg)}</div>${files}${rebuildBtn}`;
        banner.style.display = 'block';
      }
    } else {
      toast(d.error || t('logs.error'), 'error');
    }
  } catch(e) {
    toast(e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = t('logs.user.check_rebuild'); }
  }
}

async function logRebuildAllUser(inst) {
  try {
    const r = await fetch(`/api/logs/check-rebuild/${encodeURIComponent(inst)}?rebuild=true`, { method: 'POST' });
    const d = await r.json();
    if (d.ok || r.ok) {
      toast(t('logs.day.rebuild') + ' OK', 'ok');
      const banner = document.getElementById('log-check-result');
      if (banner) { banner.style.display = 'none'; }
    } else {
      toast(d.error || t('logs.error'), 'error');
    }
  } catch(e) {
    toast(e.message, 'error');
  }
}

// Strong confirm dialog: user must type the instance name
function _confirmTypeName(inst) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay active';
    overlay.innerHTML = `
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-title">${t('logs.user.delete_all')}</span>
          <button class="btn btn-sm btn-secondary modal-close-btn" id="_ctm-close">&#10005;</button>
        </div>
        <div class="modal-body">
          <p class="modal-message" style="margin-bottom:12px;">${t('logs.user.delete_confirm')}</p>
          <code class="tag" style="display:block;margin-bottom:12px;font-size:13px;">${escHtml(inst)}</code>
          <input type="text" id="_ctm-input" class="form-group input" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:6px;width:100%;font-size:13px;" placeholder="${escAttr(inst)}">
        </div>
        <div class="modal-footer">
          <button class="btn btn-danger" id="_ctm-confirm" disabled>${t('common.confirm')}</button>
          <button class="btn btn-secondary" id="_ctm-cancel">${t('common.cancel')}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const input   = overlay.querySelector('#_ctm-input');
    const confirm = overlay.querySelector('#_ctm-confirm');
    const cancel  = overlay.querySelector('#_ctm-cancel');
    const close   = overlay.querySelector('#_ctm-close');

    input.addEventListener('input', () => {
      confirm.disabled = input.value !== inst;
    });
    const done = val => { document.body.removeChild(overlay); resolve(val); };
    confirm.addEventListener('click', () => done(true));
    cancel.addEventListener('click',  () => done(false));
    close.addEventListener('click',   () => done(false));
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function _formatDate(dateStr) {
  if (!dateStr) return dateStr;
  try {
    return new Date(dateStr + 'T00:00:00').toLocaleDateString(undefined, { year: 'numeric', month: '2-digit', day: '2-digit' });
  } catch(_) { return dateStr; }
}

function _formatDateLong(dateStr) {
  if (!dateStr) return dateStr;
  try {
    return new Date(dateStr + 'T00:00:00').toLocaleDateString(undefined, { weekday: 'short', year: 'numeric', month: 'long', day: 'numeric' });
  } catch(_) { return dateStr; }
}

// ── Log Editor ────────────────────────────────────────────────────────────────
let _logEditorInst = null;
let _logEditorDate = null;

async function openLogEditor(inst, date) {
  _logEditorInst = inst;
  _logEditorDate = date;
  const modal = document.getElementById('log-editor-modal');
  const title = document.getElementById('log-editor-title');
  const area  = document.getElementById('log-editor-area');
  const info  = document.getElementById('log-editor-info');
  title.textContent = `${inst} / ${date}.jsonl`;
  area.value = ''; info.textContent = t('common.loading');
  modal.classList.add('active');
  try {
    const r = await fetch(`/api/conversations/${inst}/raw/${date}`);
    const d = await r.json();
    area.value = d.content;
    info.textContent = d.entries + ' ' + t('logs.entries');
  } catch(e) { area.value = ''; info.textContent = '! ' + e.message; }
}

function closeLogEditor() {
  document.getElementById('log-editor-modal').classList.remove('active');
}

async function saveLogEditor() {
  const area = document.getElementById('log-editor-area');
  const info = document.getElementById('log-editor-info');
  try {
    const r = await fetch(`/api/conversations/${_logEditorInst}/raw/${_logEditorDate}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: area.value }),
    });
    const d = await r.json();
    if (d.ok) {
      info.textContent = '\u2713 ' + d.entries + ' ' + t('logs.entries_saved');
      toast(t('logs.log_saved'), 'ok');
      closeLogEditor();
      loadLogDays();
    } else {
      info.textContent = '\u274c ' + t('logs.error');
    }
  } catch(e) { info.textContent = '\u274c ' + e.message; }
}

// ── Legacy stubs ──────────────────────────────────────────────────────────────
function loadLogFiles(inst) { /* superseded by loadLogDays */ }
function selectLogFileInstance(inst) { selectLogInstance(inst); }
function loadLogs(cat) { /* no-op */ }
function selectLogCat(cat) { /* no-op */ }
