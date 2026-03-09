// app.js – Tab-Wechsel, Init, globaler State, SSE-Reconnect (v9)
// Globals: currentInstance, currentViewMode, sse, cfg
// INSTANCES is set in index.html from Jinja2

let currentInstance = '__all__';     // unified instance (chat + logs)
let currentViewMode = 'live';        // 'live' | 'archiv'
let currentLogCat   = 'memory-ops';  // kept for legacy compat
let sse             = null;
let cfg             = null;

// ── Tabs ───────────────────────────────────────────────────────────────────
function showTab(name, e) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  if (e && e.target) e.target.classList.add('active');
  else document.querySelector(`.tab-btn[onclick*="'${name}'"]`)?.classList.add('active');

  if (name === 'conversations') { initConversationsView(); }
  if (name === 'config') { loadConfig(); loadMemoryStats(); loadGitStatus(); }
  if (name === 'users')  loadUsers();
  if (name === 'status') loadStatus();
  if (name === 'terminal') { initTerminal(); }
}

function showCfgTab(name) {
  document.querySelectorAll('.cfg-tab-panel').forEach(p => { p.style.display = 'none'; });
  document.querySelectorAll('.cfg-tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('cfgpanel-' + name).style.display = 'block';
  document.getElementById('cfgtab-' + name).classList.add('active');
  if (name === 'memory') loadMemoryStats();
  if (name === 'whatsapp') refreshWaStatus();
}

// ── Unified Instance Selection ─────────────────────────────────────────────
function selectInstance(inst) {
  currentInstance = inst;

  // Update unified tab buttons
  document.querySelectorAll('.conv-inst-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.inst === inst);
  });

  _updateConvUI();
}

// ── View Mode Toggle ───────────────────────────────────────────────────────
function switchViewMode(mode) {
  currentViewMode = mode;
  document.querySelectorAll('.view-toggle-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === mode);
  });
  _updateConvUI();
}

// ── Conversations View Init ────────────────────────────────────────────────
function initConversationsView() {
  // Sync instance tabs
  document.querySelectorAll('.conv-inst-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.inst === currentInstance);
  });
  // Sync view mode buttons
  document.querySelectorAll('.view-toggle-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === currentViewMode);
  });
  _updateConvUI();
}

// ── Internal: update panel based on current state ─────────────────────────
function _updateConvUI() {
  const isAll  = currentInstance === '__all__';
  const isLive = currentViewMode === 'live';

  // Chat input: hidden for "Alle" or Archiv mode
  const chatBox = document.querySelector('#panel-conversations .chat-box');
  if (chatBox) {
    chatBox.style.display = (isAll) ? 'none' : '';
  }

  // Show "select instance" hint when "Alle" is active in live mode
  const selectHint = document.getElementById('conv-select-hint');
  if (selectHint) {
    selectHint.style.display = (isAll && isLive) ? '' : 'none';
  }

  // Limit selector: only in live mode
  const limitSel = document.querySelector('.conv-limit-wrap');
  if (limitSel) limitSel.style.display = isLive ? '' : 'none';

  // Live status bar: only in live mode
  const liveBar = document.querySelector('#panel-conversations .live-bar');
  if (liveBar) liveBar.style.display = isLive ? '' : 'none';

  // Archiv toolbar + filter: only in archiv mode, and only for specific instance
  const archivToolbar = document.getElementById('conv-archiv-toolbar');
  if (archivToolbar) {
    archivToolbar.style.display = isLive ? 'none' : '';
    // Export/Delete buttons: only for specific instance
    const actions = document.getElementById('log-toolbar-actions');
    if (actions) {
      actions.style.display = (!isAll) ? 'flex' : 'none';
    }
  }

  // Content areas
  const liveContent   = document.getElementById('conv-list');
  const archivContent = document.getElementById('log-day-list');
  if (liveContent)   liveContent.style.display   = isLive ? '' : 'none';
  if (archivContent) archivContent.style.display = isLive ? 'none' : '';

  // Load data
  if (isLive) {
    // Close SSE if switching away from a specific instance
    if (isAll) {
      if (sse) { sse.close(); sse = null; }
      const dot   = document.getElementById('live-dot');
      const label = document.getElementById('live-label');
      if (dot)   dot.classList.add('offline');
      if (label) label.textContent = t('chat.sse_offline');
      if (liveContent) liveContent.innerHTML =
        `<div class="empty-state"><div class="icon">&#8594;</div><div>${t('chat.select_instance')}</div></div>`;
    } else {
      loadConversations(currentInstance);
      startSSE(currentInstance);
      checkAgentHealth(currentInstance);
    }
  } else {
    // Archiv mode: use logs functions with unified instance
    _logCurrentInst = currentInstance;
    // Reset check-result banner
    const banner = document.getElementById('log-check-result');
    if (banner) { banner.style.display = 'none'; banner.innerHTML = ''; }
    loadLogDays();
  }
}

// ── Rebuild banner shortcut ────────────────────────────────────────────────
function scrollToRebuild() {
  showCfgTab('memory');
  setTimeout(() => {
    document.getElementById('rebuild-section')?.scrollIntoView({ behavior: 'smooth' });
  }, 100);
}

// ── HA Ingress Detection ────────────────────────────────────────────────────
(function() {
  const isHaIngress = window.location.pathname.includes('/api/hassio_ingress/')
    || window.parent !== window;
  if (isHaIngress) {
    document.documentElement.classList.add('ha-theme');
    // Try to read HA theme (light/dark) from parent frame
    try {
      const haTheme = window.parent?.document?.documentElement?.getAttribute('data-theme');
      if (haTheme === 'light') document.documentElement.classList.add('ha-theme-light');
    } catch(_) { /* cross-origin, ignore */ }
  }
})();

// ── Auth ───────────────────────────────────────────────────────────────────

function _showLoginOverlay() {
  const overlay = document.getElementById('login-overlay');
  if (overlay) overlay.style.display = 'flex';
  // Focus token input
  setTimeout(() => {
    const inp = document.getElementById('login-token-input');
    if (inp) inp.focus();
  }, 80);
}

function _hideLoginOverlay() {
  const overlay = document.getElementById('login-overlay');
  if (overlay) overlay.style.display = 'none';
  const err = document.getElementById('login-error');
  if (err) err.style.display = 'none';
}

function loginSubmit() {
  const inp = document.getElementById('login-token-input');
  const token = inp ? inp.value.trim() : '';
  if (!token) return;

  fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  })
    .then(r => {
      if (r.ok) return r.json();
      throw new Error('invalid');
    })
    .then(() => {
      _hideLoginOverlay();
      _postAuthInit();
    })
    .catch(() => {
      const err = document.getElementById('login-error');
      if (err) err.style.display = '';
    });
}

function authLogout() {
  fetch('/api/auth/logout', { method: 'POST' })
    .then(() => { location.reload(); })
    .catch(() => { location.reload(); });
}

// Global 401 interceptor – wrapper um fetch
const _origFetch = window.fetch.bind(window);
window.fetch = function(url, opts) {
  return _origFetch(url, opts).then(resp => {
    if (resp.status === 401) {
      // Exempt: auth endpoints selbst
      const u = typeof url === 'string' ? url : url.toString();
      if (!u.includes('/api/auth/')) {
        _showLoginOverlay();
      }
    }
    return resp;
  });
};

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Auto-detect browser language as default if no preference stored
  const storedLang  = localStorage.getItem('haana_lang');
  const browserLang = navigator.language?.startsWith('de') ? 'de' : 'en';
  const initLang    = storedLang || browserLang;

  I18n.load(initLang).then(() => {
    const sel = document.getElementById('lang-selector');
    if (sel) sel.value = I18n.getLang();

    // Check auth status first
    fetch('/api/auth/status')
      .then(r => r.ok ? r.json() : { authenticated: true, mode: 'standalone' })
      .then(authData => {
        if (!authData.authenticated && authData.mode === 'standalone') {
          // Standalone-Modus, nicht eingeloggt → Login-Overlay
          _showLoginOverlay();
          return;
        }
        // Authentifiziert (oder Ingress-Modus) → weiter mit Setup-Check
        _checkSetupAndInit();
      })
      .catch(() => _checkSetupAndInit());
  });
});

function _checkSetupAndInit() {
  // Check if setup is needed
  fetch('/api/setup-status')
    .then(r => r.ok ? r.json() : { needs_setup: false })
    .then(d => {
      if (d && d.needs_setup) {
        // Hide normal UI
        document.querySelector('header').style.display = 'none';
        document.querySelector('.tabs').style.display = 'none';
        document.querySelectorAll('.panel').forEach(p => p.style.display = 'none');
        // Show wizard
        wizardInit();
      } else {
        _appInit();
      }
    })
    .catch(() => _appInit());
}

function _postAuthInit() {
  // Nach erfolgreichem Login: normalen Init-Flow starten
  _checkSetupAndInit();
}

function _appInit() {
  // Default: first instance, live mode
  currentInstance = INSTANCES.length > 0 ? INSTANCES[0] : '__all__';
  currentViewMode = 'live';

  initConversationsView();
  loadStatus();
  refreshWaStatus();
}

// ── Wizard-Shortcuts (für Header-Button) ───────────────────────────────────
window.openWizardExtend = async function() {
  await wizardInitExtend();
};

window.openWizardFresh = async function() {
  try {
    const resp = await fetch('/api/setup/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'fresh' }),
    });
    if (!resp.ok) {
      let msg = `Reset fehlgeschlagen (HTTP ${resp.status})`;
      try { const d = await resp.json(); if (d.detail) msg = d.detail; } catch(_) {}
      alert(msg);
      return;
    }
  } catch(e) {
    alert(`Reset fehlgeschlagen: ${e.message}`);
    return;
  }
  // Hide normal UI while wizard is open
  document.querySelector('header')?.style?.setProperty('display', 'none');
  document.querySelector('.tabs')?.style?.setProperty('display', 'none');
  document.querySelectorAll('.panel').forEach(p => p.style.display = 'none');
  wizardInit();
};

function openWizardRestartModal() {
  const modal = document.getElementById('wizard-restart-modal');
  if (modal) {
    modal.style.display = 'flex';
    // Reset confirm state
    const confirm = document.getElementById('wizard-restart-fresh-confirm');
    if (confirm) confirm.style.display = 'none';
  }
}

function closeWizardRestartModal() {
  const modal = document.getElementById('wizard-restart-modal');
  if (modal) modal.style.display = 'none';
}

function wizardRestartExtend() {
  closeWizardRestartModal();
  wizardInitExtend();
}

function wizardRestartFreshConfirm() {
  const confirm = document.getElementById('wizard-restart-fresh-confirm');
  if (confirm) confirm.style.display = 'block';
}

async function wizardRestartFreshConfirmed() {
  closeWizardRestartModal();
  await openWizardFresh();
}
