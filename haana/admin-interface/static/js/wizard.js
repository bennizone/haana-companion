// wizard.js – Setup-Wizard (v3)
// Zeigt sich beim ersten Start wenn needs_setup === true
// Unterstützt auch wiederholbare Nutzung (extend / fresh)

const _wizState = {
  step: 1,
  mode: 'fresh',      // 'fresh' | 'extend'
  providers: [],      // [{type, name, url, key, key_masked, authMethod, oauthId, models:[], tested:false, existing:false}]
  llms: [],           // flattened: [{providerIdx, model, label}]
  existingLlms: [],   // LLM-Einträge aus aktueller Config (im extend-Modus befüllt)
  users: [],          // [{id, name, primaryLlm, fallbackLlm, lang, selected}]
  haAssistLlm: '',
  haAdvancedLlm: '',
  extractionLlm: '',
  services: {},
  dreamEnabled: false,
  haUsers: [],        // HA-Personen aus /api/ha-users
  pipelines: [],      // aus /api/ha-pipelines
  haReachable: false,
  haHostname: '',
  mcpAddonAvailable: false,
  selectedPipeline: '',
};

// ── Icons / Labels ─────────────────────────────────────────────────────────
const _PROVIDER_TYPES = [
  { type: 'anthropic', label: 'Anthropic', icon: '🤖', desc: 'Claude AI – Opus, Sonnet, Haiku',           hasKey: true,  hasUrl: false },
  { type: 'openai',    label: 'OpenAI',    icon: '🔮', desc: 'GPT-4o, o1, o3',                            hasKey: true,  hasUrl: false },
  { type: 'ollama',    label: 'Ollama',    icon: '🦙', desc: 'Lokale LLMs – Llama, Mistral, …',           hasKey: false, hasUrl: true  },
  { type: 'gemini',    label: 'Gemini',    icon: '✨', desc: 'Google Gemini – Flash, Pro',                 hasKey: true,  hasUrl: false },
  { type: 'minimax',   label: 'MiniMax',   icon: '🌐', desc: 'MiniMax – Anthropic-kompatible API',        hasKey: true,  hasUrl: false },
];

const _PROVIDER_DEFAULTS = {
  anthropic: 'https://api.anthropic.com',
  openai:    'https://api.openai.com',
  gemini:    'https://generativelanguage.googleapis.com',
  minimax:   'https://api.minimax.io/anthropic',
  ollama:    'http://localhost:11434',
};

// ── Entry Points ───────────────────────────────────────────────────────────
function wizardInit() {
  // Reset state for fresh start
  _wizState.mode = 'fresh';
  _wizState.providers = [];
  _wizState.llms = [];
  _wizState.users = [];
  _wizState.haAssistLlm = '';
  _wizState.haAdvancedLlm = '';
  _wizState.extractionLlm = '';
  _wizState.dreamEnabled = false;
  _wizState.selectedPipeline = '';

  const overlay = document.getElementById('wizard-overlay');
  if (!overlay) return;

  // Update header subtitle
  const subtitle = overlay.querySelector('.wizard-subtitle');
  if (subtitle) subtitle.textContent = t('wizard.setup_title');

  // Show nav
  const nav = document.getElementById('wizard-nav');
  if (nav) nav.style.display = '';

  overlay.style.display = 'flex';
  _wizUpdateCloseBtn();
  _wizRenderStep(1);
}

async function wizardInitExtend() {
  _wizState.mode = 'extend';

  // Load current config
  let currentCfg = null;
  try {
    const r = await fetch('/api/setup/current-config');
    if (r.ok) currentCfg = await r.json();
  } catch(_) {}

  // Reset providers/users but pre-fill from existing config
  _wizState.providers = [];
  _wizState.llms = [];
  _wizState.existingLlms = [];
  _wizState.users = [];

  if (currentCfg) {
    // Pre-fill existing LLMs first (must happen before _wizRebuildLlmList)
    _wizState.existingLlms = currentCfg.llms || [];

    // Pre-fill providers as "existing" (key empty – backend keeps existing key)
    (currentCfg.providers || []).forEach(p => {
      _wizState.providers.push({
        type:       p.type,
        name:       p.name,
        url:        p.url || '',
        key:        '',           // empty = do not change on backend
        key_masked: p.key_masked || '',
        models:     [],
        tested:     true,         // treat as already tested
        existing:   true,         // flag for UI rendering
      });
    });
    _wizRebuildLlmList();

    // Pre-fill users
    (currentCfg.users || []).forEach(u => {
      _wizState.users.push({
        id:           u.id,
        display_name: u.display_name,
        primary_llm:  u.primary_llm || '',
        fallback_llm: u.fallback_llm || '',
        language:     u.language || 'de',
        selected:     true,
        existing:     true,
      });
    });

    // Pre-fill system LLMs
    if (currentCfg.ha_assist_llm)  _wizState.haAssistLlm   = currentCfg.ha_assist_llm;
    if (currentCfg.ha_advanced_llm) _wizState.haAdvancedLlm = currentCfg.ha_advanced_llm;
    if (currentCfg.extraction_llm) _wizState.extractionLlm  = currentCfg.extraction_llm;
    if (currentCfg.dream_enabled !== undefined) _wizState.dreamEnabled = currentCfg.dream_enabled;
  }

  const overlay = document.getElementById('wizard-overlay');
  if (!overlay) return;

  // Update header subtitle to "extend" mode label
  const subtitle = overlay.querySelector('.wizard-subtitle');
  if (subtitle) subtitle.textContent = t('wizard.extend_title');

  // Show nav
  const nav = document.getElementById('wizard-nav');
  if (nav) nav.style.display = '';

  overlay.style.display = 'flex';
  _wizUpdateCloseBtn();
  _wizRenderStep(1);
}

// ── Step Navigation ────────────────────────────────────────────────────────
function _wizGoTo(step) {
  if (step < 1 || step > 3) return;
  _wizState.step = step;
  _wizRenderStep(step);
}

function _wizRenderStep(step) {
  // Update breadcrumb
  for (let s = 1; s <= 3; s++) {
    const el = document.getElementById(`wiz-step-${s}`);
    if (!el) continue;
    el.className = 'wizard-step' +
      (s < step ? ' wizard-step-done' : '') +
      (s === step ? ' wizard-step-active' : '');
  }

  // Render content
  const content = document.getElementById('wizard-content');
  if (!content) return;

  if (step === 1) {
    content.innerHTML = _wizStep1Html();
    _wizStep1Init();
  } else if (step === 2) {
    content.innerHTML = _wizStep2Html();
    _wizStep2Init();
  } else if (step === 3) {
    content.innerHTML = _wizStep3Html();
    _wizStep3Init();
  }

  // Nav buttons
  const backBtn  = document.getElementById('wiz-back-btn');
  const nextBtn  = document.getElementById('wiz-next-btn');
  const finBtn   = document.getElementById('wiz-finish-btn');

  if (backBtn) backBtn.style.display = step > 1 ? '' : 'none';
  if (nextBtn) nextBtn.style.display = step < 3 ? '' : 'none';
  if (finBtn)  finBtn.style.display  = step === 3 ? '' : 'none';

  _wizUpdateNextBtn();
}

function _wizUpdateNextBtn() {
  const nextBtn = document.getElementById('wiz-next-btn');
  if (!nextBtn) return;
  const step = _wizState.step;
  if (step === 1) {
    // In extend mode, existing providers (p.existing) count as valid even without re-testing.
    // OAuth-authenticated providers count as valid even without fetched models.
    const ok = _wizState.providers.some(p =>
      (p.tested && p.models.length > 0) || p.existing || p.oauthAuthenticated
    );
    nextBtn.disabled = !ok;
    nextBtn.title = ok ? '' : t('wizard.step1_need_provider');
  } else {
    nextBtn.disabled = false;
  }
}

// ── Step 1: Providers ──────────────────────────────────────────────────────
function _wizStep1Html() {
  const modeHint = _wizState.mode === 'extend'
    ? `<div style="background:var(--accent2);background:color-mix(in srgb,var(--accent2) 15%,transparent);border:1px solid var(--accent2);border-radius:var(--radius-sm);padding:var(--sp-2) var(--sp-3);margin-bottom:var(--sp-3);font-size:12px;color:var(--accent2);">
        &#128274; ${t('wizard.mode_extend')} – ${t('wizard.restart_extend_desc')}
      </div>`
    : '';
  return `
    ${modeHint}
    <h2 class="wizard-section-title" data-i18n="wizard.step1_title">${t('wizard.step1_title')}</h2>
    <p class="wizard-section-desc" data-i18n="wizard.step1_desc">${t('wizard.step1_desc')}</p>
    <div id="wiz-providers-list"></div>
    <div style="margin-top:var(--sp-4);">
      <button class="btn btn-secondary" onclick="wizAddProvider()">+ ${t('wizard.add_provider')}</button>
    </div>
    <div class="wizard-info-card" style="margin-top:var(--sp-5);">
      <span style="font-size:16px;">&#8505;</span>
      <div>
        <strong>${t('wizard.embedding_local_title')}</strong><br>
        <span style="font-size:12px;color:var(--muted);">${t('wizard.embedding_local_desc')}</span>
      </div>
    </div>`;
}

function _wizStep1Init() {
  _wizRenderProviders();
}

function _wizRenderProviders() {
  const list = document.getElementById('wiz-providers-list');
  if (!list) return;
  if (_wizState.providers.length === 0) {
    list.innerHTML = `<div class="empty-state" style="padding:24px;">
      <div style="font-size:13px;color:var(--muted);">${t('wizard.no_providers_yet')}</div>
    </div>`;
    return;
  }
  list.innerHTML = _wizState.providers.map((p, i) => _wizProviderCardHtml(p, i)).join('');
}

function _wizProviderCardHtml(p, i) {
  const meta   = _PROVIDER_TYPES.find(x => x.type === p.type) || { label: p.type, icon: '?' };

  // Determine status display
  let status;
  if (p.oauthAuthenticated) {
    status = `<span style="color:var(--green);">&#10003; ${t('wizard.oauth_success')}</span>`;
  } else if (p.tested) {
    if (p.models.length > 0) {
      status = `<span style="color:var(--green);">&#10003; ${t('wizard.connected')} &middot; ${p.models.length} ${t('wizard.models')}</span>`;
    } else if (p.existing) {
      status = `<span style="color:var(--green);">&#128274; ${t('wizard.extend_provider_existing')}</span>`;
    } else {
      status = `<span style="color:var(--yellow);">&#9888; ${t('wizard.connected_no_models')}</span>`;
    }
  } else {
    status = `<span style="color:var(--muted);">${t('wizard.not_tested')}</span>`;
  }

  const cardBorder = p.existing ? 'border-left:3px solid var(--accent2);' : '';

  // Build the credential/auth section
  let credSection;
  if (p.type === 'ollama') {
    credSection = `
    <div class="form-group" style="margin-bottom:var(--sp-3);">
      <label>URL</label>
      <input type="url" id="wiz-prov-${i}-url" value="${escAttr(p.url || _PROVIDER_DEFAULTS.ollama)}"
        placeholder="http://localhost:11434"
        oninput="_wizState.providers[${i}].url=this.value;_wizState.providers[${i}].tested=false;_wizUpdateNextBtn();">
    </div>`;
  } else if (p.type === 'anthropic' && p.authMethod === 'oauth') {
    credSection = _wizOAuthSectionHtml(p, i);
  } else {
    // Standard API key field
    if (p.existing) {
      credSection = `
      <div class="form-group" style="margin-bottom:var(--sp-3);">
        <label>API-Key <span style="font-size:11px;color:var(--muted);">(${t('wizard.extend_provider_existing')}: ${escHtml(p.key_masked || '***')})</span></label>
        <input type="password" id="wiz-prov-${i}-key" value=""
          placeholder="${t('wizard.api_key_replace_placeholder')}"
          oninput="_wizState.providers[${i}].key=this.value;_wizState.providers[${i}].tested=false;_wizUpdateNextBtn();">
      </div>`;
    } else {
      credSection = `
      <div class="form-group" style="margin-bottom:var(--sp-3);">
        <label>API-Key</label>
        <input type="password" id="wiz-prov-${i}-key" value="${escAttr(p.key || '')}"
          placeholder="${t('wizard.api_key_placeholder')}"
          oninput="_wizState.providers[${i}].key=this.value;_wizState.providers[${i}].tested=false;_wizUpdateNextBtn();">
      </div>`;
    }
  }

  // Test/action button row — for OAuth providers, hide the generic test button
  const actionRow = (p.type === 'anthropic' && p.authMethod === 'oauth')
    ? ''
    : `<div style="display:flex;gap:var(--sp-2);align-items:center;">
        <button class="btn btn-secondary btn-sm" onclick="wizTestProvider(${i})" id="wiz-prov-${i}-test-btn"
          title="${p.existing ? t('wizard.provider_retest_title') : ''}">
          ${p.existing ? t('wizard.provider_retest') : t('wizard.test_connection')}
        </button>
        <span id="wiz-prov-${i}-status">${status}</span>
      </div>`;

  return `
  <div class="wizard-provider-card" id="wiz-prov-${i}" style="${cardBorder}">
    <div style="display:flex;align-items:center;gap:var(--sp-3);margin-bottom:var(--sp-3);">
      <span style="font-size:20px;">${meta.icon}</span>
      <span style="font-weight:600;color:var(--accent2);flex:1;">${escHtml(meta.label)}${p.authMethod === 'oauth' ? ' <span style="font-size:11px;color:var(--muted);">· OAuth</span>' : ''}</span>
      ${p.existing ? `<span style="font-size:11px;color:var(--accent2);border:1px solid var(--accent2);border-radius:var(--radius-sm);padding:2px 6px;">&#128274; ${t('wizard.extend_provider_existing')}</span>` : ''}
      <button class="btn btn-sm btn-danger" onclick="wizRemoveProvider(${i})">&#10005;</button>
    </div>
    ${credSection}
    ${actionRow}
  </div>`;
}

function _wizOAuthSectionHtml(p, i) {
  const statusHtml = p.oauthAuthenticated
    ? `<span id="wiz-prov-${i}-status" style="color:var(--green);">&#10003; ${t('wizard.oauth_success')}</span>`
    : `<span id="wiz-prov-${i}-status" style="color:var(--muted);">${t('wizard.not_tested')}</span>`;

  return `
  <div style="background:var(--card-bg,var(--surface-hi));border:1px solid var(--border);border-radius:var(--radius-sm);padding:var(--sp-3);margin-bottom:var(--sp-3);">
    <div style="display:flex;align-items:center;gap:var(--sp-2);margin-bottom:var(--sp-2);">
      ${statusHtml}
    </div>
    <div style="display:flex;gap:var(--sp-2);align-items:center;margin-bottom:var(--sp-2);">
      <button class="btn btn-primary btn-sm" onclick="wizStartOAuthLogin(${i})" id="wiz-prov-${i}-oauth-start-btn">
        ${t('config_services.oauth_start_btn')}
      </button>
      <span id="wiz-prov-${i}-oauth-login-status" style="font-size:12px;color:var(--muted);"></span>
    </div>
    <div id="wiz-prov-${i}-oauth-login-url" style="margin-bottom:var(--sp-2);"></div>
    <div id="wiz-prov-${i}-oauth-code-section" style="display:none;">
      <div class="form-group" style="margin-bottom:var(--sp-2);">
        <label style="font-size:12px;">${t('config_services.oauth_code_label')}</label>
        <div style="display:flex;gap:var(--sp-2);">
          <input type="text" id="wiz-prov-${i}-oauth-code-input" style="flex:1;font-family:var(--mono);"
            placeholder="${t('config_services.oauth_code_placeholder')}">
          <button class="btn btn-primary btn-sm" onclick="wizCompleteOAuthLogin(${i})">
            ${t('config_services.oauth_submit_btn')}
          </button>
        </div>
      </div>
      <span id="wiz-prov-${i}-oauth-code-result" style="font-size:12px;display:block;"></span>
    </div>
  </div>`;
}

function wizAddProvider() {
  // Show a type picker inline
  const list = document.getElementById('wiz-providers-list');
  const picker = document.createElement('div');
  picker.className = 'wizard-provider-card';
  picker.style.marginBottom = 'var(--sp-3)';
  picker.innerHTML = `
    <p style="font-size:13px;font-weight:600;margin-bottom:var(--sp-3);">${t('wizard.choose_provider_type')}</p>
    <div class="provider-type-grid">
      ${_PROVIDER_TYPES.map(pt => `
        <div class="provider-type-card" onclick="wizSelectProviderType('${pt.type}')">
          <div style="font-size:24px;margin-bottom:4px;">${pt.icon}</div>
          <div class="provider-type-card-name">${pt.label}</div>
          <div class="provider-type-card-desc">${pt.desc}</div>
        </div>`).join('')}
    </div>
    <div style="margin-top:var(--sp-3);text-align:right;">
      <button class="btn btn-secondary btn-sm" onclick="this.closest('.wizard-provider-card').remove()">
        ${t('common.cancel')}
      </button>
    </div>`;
  list.appendChild(picker);
}

function wizSelectProviderType(type) {
  const meta = _PROVIDER_TYPES.find(x => x.type === type);
  if (!meta) return;

  if (type === 'anthropic') {
    // Show auth method chooser in the picker area before adding to state
    const pickers = document.querySelectorAll('.wizard-provider-card');
    const picker = pickers[pickers.length - 1];
    if (picker) {
      picker.innerHTML = `
        <p style="font-size:13px;font-weight:600;margin-bottom:var(--sp-3);">${t('wizard.provider_auth_choose')}</p>
        <div class="provider-type-grid">
          <div class="provider-type-card" onclick="wizSelectAnthropicAuthMethod('api_key')">
            <div style="font-size:20px;margin-bottom:4px;">&#128273;</div>
            <div class="provider-type-card-name">${t('wizard.provider_auth_apikey')}</div>
            <div class="provider-type-card-desc">${t('wizard.provider_auth_apikey_desc')}</div>
          </div>
          <div class="provider-type-card" onclick="wizSelectAnthropicAuthMethod('oauth')">
            <div style="font-size:20px;margin-bottom:4px;">&#128100;</div>
            <div class="provider-type-card-name">${t('wizard.provider_auth_oauth')}</div>
            <div class="provider-type-card-desc">${t('wizard.provider_auth_oauth_desc')}</div>
          </div>
        </div>
        <div style="margin-top:var(--sp-3);text-align:right;">
          <button class="btn btn-secondary btn-sm" onclick="this.closest('.wizard-provider-card').remove()">
            ${t('common.cancel')}
          </button>
        </div>`;
    }
    return;
  }

  _wizState.providers.push({
    type,
    name: meta.label,
    url:  _PROVIDER_DEFAULTS[type] || '',
    key:  '',
    authMethod: null,
    oauthId: null,
    oauthAuthenticated: false,
    models: [],
    tested: false,
  });
  _wizRenderProviders();
  _wizUpdateNextBtn();
}

function wizSelectAnthropicAuthMethod(authMethod) {
  const meta = _PROVIDER_TYPES.find(x => x.type === 'anthropic');
  // Generate a unique ID for OAuth use
  const existing = _wizState.providers.filter(p => p.type === 'anthropic').length;
  const oauthId = existing === 0 ? 'anthropic-1' : `anthropic-${existing + 1}`;

  _wizState.providers.push({
    type:       'anthropic',
    name:       meta.label,
    url:        _PROVIDER_DEFAULTS.anthropic || '',
    key:        '',
    authMethod,
    oauthId,
    oauthAuthenticated: false,
    models:     [],
    tested:     false,
  });
  _wizRenderProviders();
  _wizUpdateNextBtn();
}

function wizRemoveProvider(i) {
  _wizState.providers.splice(i, 1);
  _wizRebuildLlmList();
  _wizRenderProviders();
  _wizUpdateNextBtn();
}

async function wizTestProvider(i) {
  const p       = _wizState.providers[i];
  const statusEl = document.getElementById(`wiz-prov-${i}-status`);
  const testBtn  = document.getElementById(`wiz-prov-${i}-test-btn`);

  // Read current field values
  const keyEl = document.getElementById(`wiz-prov-${i}-key`);
  const urlEl = document.getElementById(`wiz-prov-${i}-url`);
  if (keyEl) p.key = keyEl.value.trim();
  if (urlEl) p.url = urlEl.value.trim();

  if (!p.url && !p.key && !p.key_masked && p.type !== 'ollama') {
    statusEl.innerHTML = `<span style="color:var(--yellow);">&#9888; ${t('wizard.enter_key_first')}</span>`;
    return;
  }
  // Existing provider with empty key: cannot re-test without a new key
  if (p.existing && !p.key && p.type !== 'ollama') {
    statusEl.innerHTML = `<span style="color:var(--muted);">&#128274; ${t('wizard.extend_provider_existing')} – ${t('wizard.api_key_replace_placeholder')}</span>`;
    if (testBtn) testBtn.disabled = false;
    return;
  }

  statusEl.innerHTML = `<span style="color:var(--muted);">&#8230;</span>`;
  if (testBtn) testBtn.disabled = true;

  let url = p.url || _PROVIDER_DEFAULTS[p.type] || '';

  try {
    const resp = await fetch('/api/fetch-models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: p.type, url, key: p.key }),
    });
    const data = await resp.json();
    p.models = data.models || [];
    p.tested = true;
    _wizRebuildLlmList();
    _wizUpdateNextBtn();

    if (p.models.length > 0) {
      statusEl.innerHTML = `<span style="color:var(--green);">&#10003; ${t('wizard.connected')} &middot; ${p.models.length} ${t('wizard.models')}</span>`;
    } else {
      statusEl.innerHTML = `<span style="color:var(--yellow);">&#9888; ${t('wizard.connected_no_models')}</span>`;
    }
  } catch(e) {
    p.tested = false;
    statusEl.innerHTML = `<span style="color:var(--red);">&#10007; ${escHtml(e.message)}</span>`;
    _wizUpdateNextBtn();
  } finally {
    if (testBtn) testBtn.disabled = false;
  }
}

async function wizStartOAuthLogin(i) {
  const p = _wizState.providers[i];
  if (!p) return;
  const statusEl    = document.getElementById(`wiz-prov-${i}-oauth-login-status`);
  const urlEl       = document.getElementById(`wiz-prov-${i}-oauth-login-url`);
  const codeSection = document.getElementById(`wiz-prov-${i}-oauth-code-section`);
  const startBtn    = document.getElementById(`wiz-prov-${i}-oauth-start-btn`);
  if (!statusEl) return;

  statusEl.innerHTML = `<span style="color:var(--muted);">${t('wizard.oauth_connecting')}</span>`;
  if (urlEl) urlEl.innerHTML = '';
  if (codeSection) codeSection.style.display = 'none';
  if (startBtn) startBtn.disabled = true;

  try {
    const r = await fetch(`/api/claude-auth/login/start/${encodeURIComponent(p.oauthId)}`, { method: 'POST' });
    const d = await r.json();
    if (!d.ok) {
      statusEl.innerHTML = `<span style="color:var(--red);">&#10007; ${escHtml(d.detail || d.error || 'Error')}</span>`;
      if (startBtn) startBtn.disabled = false;
      return;
    }
    statusEl.innerHTML = `<span style="color:var(--green);">${t('config_services.oauth_url_ready')}</span>`;
    if (urlEl) {
      urlEl.innerHTML = `<a href="${escHtml(d.url)}" target="_blank" rel="noopener"
        style="word-break:break-all;color:var(--accent2);font-size:12px;">${t('config_services.oauth_open_link')}</a>`;
    }
    if (codeSection) {
      codeSection.style.display = 'block';
      const codeInput = document.getElementById(`wiz-prov-${i}-oauth-code-input`);
      const codeResult = document.getElementById(`wiz-prov-${i}-oauth-code-result`);
      if (codeInput) codeInput.value = '';
      if (codeResult) codeResult.textContent = '';
    }
  } catch(e) {
    statusEl.innerHTML = `<span style="color:var(--red);">&#10007; ${escHtml(e.message)}</span>`;
  } finally {
    if (startBtn) startBtn.disabled = false;
  }
}

async function wizCompleteOAuthLogin(i) {
  const p = _wizState.providers[i];
  if (!p) return;
  const codeInput = document.getElementById(`wiz-prov-${i}-oauth-code-input`);
  const resultEl  = document.getElementById(`wiz-prov-${i}-oauth-code-result`);
  const statusEl  = document.getElementById(`wiz-prov-${i}-status`);
  if (!codeInput || !resultEl) return;

  const code = codeInput.value.trim();
  if (!code) {
    resultEl.textContent = t('config_services.oauth_code_missing');
    resultEl.style.color = 'var(--red)';
    return;
  }

  resultEl.innerHTML = `<span style="color:var(--muted);">${t('wizard.oauth_waiting')}</span>`;

  try {
    const r = await fetch(`/api/claude-auth/login/complete/${encodeURIComponent(p.oauthId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    });
    const d = await r.json();
    if (d.ok) {
      resultEl.innerHTML = `<span style="color:var(--green);">&#10003; ${escHtml(d.detail || t('wizard.oauth_success'))}</span>`;
      codeInput.value = '';
      const codeSection = document.getElementById(`wiz-prov-${i}-oauth-code-section`);
      if (codeSection) codeSection.style.display = 'none';
      // Mark provider as authenticated
      p.oauthAuthenticated = true;
      p.tested = true;
      p.models = ['claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022', 'claude-3-opus-20240229'];
      if (statusEl) statusEl.innerHTML = `<span style="color:var(--green);">&#10003; ${t('wizard.oauth_success')}</span>`;
      _wizRebuildLlmList();
      _wizUpdateNextBtn();
    } else {
      resultEl.innerHTML = `<span style="color:var(--red);">&#10007; ${escHtml(d.detail || t('wizard.oauth_error'))}</span>`;
    }
  } catch(e) {
    resultEl.innerHTML = `<span style="color:var(--red);">&#10007; ${escHtml(e.message)}</span>`;
  }
}

function _wizRebuildLlmList() {
  // Rebuild flat LLM list from all tested providers
  _wizState.llms = [];
  const seenIds = new Set();

  _wizState.providers.forEach((p, pi) => {
    if (!p.tested) return;
    (p.models || []).forEach(m => {
      const id = `${p.type}::${m}`;
      if (!seenIds.has(id)) {
        seenIds.add(id);
        _wizState.llms.push({ providerIdx: pi, providerType: p.type, model: m, label: `${m} (${p.name})` });
      }
    });
  });

  // Im Extend-Modus: existierende LLM-Einträge aus der Config hinzufügen (dedupliziert)
  (_wizState.existingLlms || []).forEach(llm => {
    const id = llm.id || llm.name || '';
    if (id && !seenIds.has(id)) {
      seenIds.add(id);
      _wizState.llms.push({
        providerIdx:  -1,
        providerType: llm.provider || '',
        model:        llm.model || llm.name || id,
        label:        llm.name || llm.model || id,
        existingId:   id,
      });
    }
  });
}

// ── Step 2: Household ──────────────────────────────────────────────────────
function _wizStep2Html() {
  return `
    <h2 class="wizard-section-title">${t('wizard.step2_title')}</h2>
    <p class="wizard-section-desc">${t('wizard.step2_desc')}</p>

    <!-- A: Household Members -->
    <div class="wizard-section-block">
      <div class="wizard-block-header">${t('wizard.household_members')}</div>
      <div id="wiz-users-area">
        <div class="empty-state" style="padding:20px;">
          <div class="icon">&#8230;</div>
          <div>${t('common.loading')}</div>
        </div>
      </div>
    </div>

    <!-- B: System Agents -->
    <div class="wizard-section-block">
      <div class="wizard-block-header">${t('wizard.system_agents')}</div>
      <div class="wizard-info-card" style="margin-bottom:var(--sp-3);">
        <div>
          <strong>HAANA-Assist</strong><br>
          <span style="font-size:12px;color:var(--muted);">${t('wizard.ha_assist_desc')}</span>
        </div>
      </div>
      <div class="form-group" style="margin-bottom:var(--sp-4);">
        <label>${t('wizard.model_for_assist')}</label>
        <select id="wiz-ha-assist-llm">${_wizLlmOptions(_wizState.haAssistLlm)}</select>
        <span style="font-size:11px;color:var(--muted);">${t('wizard.suggest_fast')}</span>
      </div>

      <div class="wizard-info-card" style="margin-bottom:var(--sp-3);">
        <div>
          <strong>HAANA-Advanced</strong><br>
          <span style="font-size:12px;color:var(--muted);">${t('wizard.ha_advanced_desc')}</span>
        </div>
      </div>
      <div class="form-group" style="margin-bottom:var(--sp-3);">
        <label>${t('wizard.model_for_advanced')}</label>
        <select id="wiz-ha-advanced-llm">${_wizLlmOptions(_wizState.haAdvancedLlm)}</select>
        <span style="font-size:11px;color:var(--muted);">${t('wizard.suggest_capable')}</span>
      </div>
    </div>

    <!-- C: Memory Extraction -->
    <div class="wizard-section-block">
      <div class="wizard-block-header">${t('wizard.memory_extraction')}</div>
      <div class="wizard-info-card">
        <span style="font-size:16px;">&#128214;</span>
        <div>
          <span style="font-size:13px;">${t('wizard.extraction_auto_desc')}</span><br>
          <span id="wiz-extraction-model-info" style="font-size:12px;color:var(--accent2);margin-top:4px;display:block;">
            ${_wizCheapestModel()}
          </span>
        </div>
      </div>
    </div>

    <!-- D: Voice Integration -->
    <div class="wizard-section-block">
      <div class="wizard-block-header">${t('wizard.voice_guide')}</div>
      <div id="wiz-voice-guide">
        <div class="empty-state" style="padding:16px;"><div>&#8230;</div></div>
      </div>
    </div>`;
}

async function _wizStep2Init() {
  // Suggest models
  _wizAutoSuggestModels();

  // Load HA users
  try {
    const r = await fetch('/api/ha-users');
    if (r.ok) {
      const d = await r.json();
      _wizState.haUsers = d.users || d || [];
      _wizState.haReachable = true;
    }
  } catch(_) {
    _wizState.haReachable = false;
  }
  _wizRenderUsersArea();

  // Voice guide
  try {
    const r = await fetch('/api/supervisor/self');
    if (r.ok) {
      const d = await r.json();
      _wizState.haHostname = d.hostname || window.location.hostname;
    }
  } catch(_) {
    _wizState.haHostname = window.location.hostname;
  }
  _wizRenderVoiceGuide();

  // Bind LLM dropdowns
  const assistEl   = document.getElementById('wiz-ha-assist-llm');
  const advancedEl = document.getElementById('wiz-ha-advanced-llm');
  if (assistEl)   assistEl.addEventListener('change',   () => { _wizState.haAssistLlm   = assistEl.value; });
  if (advancedEl) advancedEl.addEventListener('change', () => { _wizState.haAdvancedLlm = advancedEl.value; });
}

function _wizAutoSuggestModels() {
  const models = _wizState.llms;
  if (models.length === 0) return;

  // Heuristics: "fast/cheap" = haiku, flash, mini, small, 8b, 3b
  // "capable" = opus, gpt-4o, pro, 70b, latest, sonnet
  const fastKeywords    = ['haiku', 'flash', 'mini', 'small', '8b', '3b', 'nano'];
  const capableKeywords = ['opus', 'gpt-4o', 'pro', '70b', 'sonnet', 'latest', 'large'];

  const score = (m, keywords) => keywords.reduce((s, kw) => s + (m.toLowerCase().includes(kw) ? 1 : 0), 0);

  const sorted = [...models];
  const fastest  = sorted.sort((a, b) => score(b.model, fastKeywords)    - score(a.model, fastKeywords))[0];
  const capable  = sorted.sort((a, b) => score(b.model, capableKeywords) - score(a.model, capableKeywords))[0];
  const cheapest = fastest || models[0];

  if (!_wizState.haAssistLlm   && fastest)  _wizState.haAssistLlm   = fastest.label;
  if (!_wizState.haAdvancedLlm && capable)  _wizState.haAdvancedLlm = capable.label;
  if (!_wizState.extractionLlm && cheapest) _wizState.extractionLlm = cheapest.label;

  const assistEl   = document.getElementById('wiz-ha-assist-llm');
  const advancedEl = document.getElementById('wiz-ha-advanced-llm');
  if (assistEl && _wizState.haAssistLlm)   assistEl.value   = _wizState.haAssistLlm;
  if (advancedEl && _wizState.haAdvancedLlm) advancedEl.value = _wizState.haAdvancedLlm;
}

function _wizCheapestModel() {
  if (_wizState.extractionLlm) return _wizState.extractionLlm;
  const cheapest = _wizState.llms[0];
  return cheapest ? cheapest.label : t('wizard.auto_selected');
}

function _wizLlmOptions(selected) {
  if (_wizState.llms.length === 0) {
    // In extend mode: show the currently assigned value as a disabled placeholder
    if (selected) {
      return `<option value="${escAttr(selected)}" selected>${escHtml(selected)}</option>
              <option value="" disabled>── ${t('wizard.no_models_available')} ──</option>`;
    }
    return `<option value="">${t('wizard.no_models_available')}</option>`;
  }
  return _wizState.llms.map(m =>
    `<option value="${escAttr(m.label)}" ${m.label === selected ? 'selected' : ''}>${escHtml(m.label)}</option>`
  ).join('');
}

function _wizRenderUsersArea() {
  const area = document.getElementById('wiz-users-area');
  if (!area) return;

  if (!_wizState.haReachable || _wizState.haUsers.length === 0) {
    // Manual input
    area.innerHTML = `
      <p style="font-size:12px;color:var(--muted);margin-bottom:var(--sp-3);">
        ${_wizState.haReachable ? t('wizard.no_ha_users') : t('wizard.ha_not_reachable')}
      </p>
      <div class="form-group">
        <label>${t('wizard.assistant_name')}</label>
        <input type="text" id="wiz-manual-name" placeholder="z.B. Anna"
          oninput="_wizUpdateManualUser(this.value)">
      </div>
      <div id="wiz-manual-user-config"></div>`;

    // If we have existing manual users, show them
    if (_wizState.users.length > 0) {
      _wizRenderUserConfigs();
    }
    return;
  }

  // HA users found – show checkboxes
  area.innerHTML = `
    <p style="font-size:12px;color:var(--muted);margin-bottom:var(--sp-3);">
      &#10003; ${_wizState.haUsers.length} ${t('wizard.ha_users_found')}
    </p>
    ${_wizState.haUsers.map((u, i) => {
      const id   = u.entity_id || u.id || `user_${i}`;
      const name = u.attributes?.friendly_name || u.name || id;
      const sel  = _wizState.users.find(x => x.id === id);
      return `
      <div class="wizard-user-row" id="wiz-user-row-${i}">
        <label class="checkbox-label" style="font-weight:600;margin-bottom:var(--sp-2);">
          <input type="checkbox" id="wiz-user-check-${i}" ${sel ? 'checked' : ''}
            onchange="_wizToggleUser(${i}, '${escAttr(id)}', '${escAttr(name)}', this.checked)">
          ${escHtml(name)}
        </label>
        <div id="wiz-user-config-${i}" style="${sel ? '' : 'display:none;'}padding-left:24px;">
          ${_wizUserConfigHtml(i, sel || { id, name, primaryLlm: _wizState.haAssistLlm, fallbackLlm: '', lang: 'de' })}
        </div>
      </div>`;
    }).join('')}`;

  // Pre-check if no users yet
  if (_wizState.users.length === 0) {
    _wizState.haUsers.forEach((u, i) => {
      const id   = u.entity_id || u.id || `user_${i}`;
      const name = u.attributes?.friendly_name || u.name || id;
      const check = document.getElementById(`wiz-user-check-${i}`);
      if (check) { check.checked = true; _wizToggleUser(i, id, name, true); }
    });
  }
}

function _wizUserConfigHtml(i, user) {
  const isManual = (i === 'm0');
  const updateFn = isManual ? '_wizUpdateUserLlmManual' : `_wizUpdateUserLlm.bind(null, ${i})`;
  const onchange = isManual
    ? (field) => `_wizUpdateUserLlmManual('${field}', this.value)`
    : (field) => `_wizUpdateUserLlm(${i}, '${field}', this.value)`;
  const lang = user.language || 'de';
  return `
    <div class="form-row" style="margin-top:var(--sp-2);">
      <div class="form-group">
        <label>${t('wizard.primary_model')}</label>
        <select id="wiz-user-${i}-llm" onchange="${onchange('primary_llm')}">
          ${_wizLlmOptions(user.primary_llm)}
        </select>
      </div>
      <div class="form-group">
        <label>${t('wizard.fallback_model')}</label>
        <select id="wiz-user-${i}-fallback" onchange="${onchange('fallback_llm')}">
          <option value="">-- ${t('wizard.optional')} --</option>
          ${_wizLlmOptions(user.fallback_llm)}
        </select>
      </div>
      <div class="form-group">
        <label>${t('wizard.language')}</label>
        <select id="wiz-user-${i}-lang" onchange="${onchange('language')}">
          <option value="de" ${lang==='de'?'selected':''}>Deutsch</option>
          <option value="en" ${lang==='en'?'selected':''}>English</option>
          <option value="tr" ${lang==='tr'?'selected':''}>Türkçe</option>
          <option value="fr" ${lang==='fr'?'selected':''}>Français</option>
          <option value="es" ${lang==='es'?'selected':''}>Español</option>
        </select>
      </div>
    </div>`;
}

function _wizToggleUser(i, id, name, checked) {
  const cfgDiv = document.getElementById(`wiz-user-config-${i}`);
  if (checked) {
    if (!_wizState.users.find(u => u.id === id)) {
      _wizState.users.push({ id, display_name: name, primary_llm: _wizState.haAssistLlm, fallback_llm: '', language: 'de', selected: true });
    }
    if (cfgDiv) cfgDiv.style.display = '';
  } else {
    _wizState.users = _wizState.users.filter(u => u.id !== id);
    if (cfgDiv) cfgDiv.style.display = 'none';
  }
}

function _wizUpdateUserLlm(i, field, value) {
  const haUser = _wizState.haUsers[i];
  const id = haUser ? (haUser.entity_id || haUser.id || `user_${i}`) : `user_${i}`;
  const user = _wizState.users.find(u => u.id === id);
  if (user) user[field] = value;
}

function _wizUpdateManualUser(name) {
  const cleanId = name.toLowerCase().replace(/[^a-z0-9]/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '') || 'user';
  if (_wizState.users.length === 0) {
    _wizState.users.push({ id: cleanId, display_name: name, primary_llm: _wizState.haAssistLlm, fallback_llm: '', language: 'de', selected: true });
  } else {
    _wizState.users[0].id           = cleanId;
    _wizState.users[0].display_name = name;
  }
  const cfgArea = document.getElementById('wiz-manual-user-config');
  if (cfgArea && name.trim().length > 0) {
    cfgArea.innerHTML = _wizUserConfigHtml('m0', _wizState.users[0]);
  }
}

function _wizUpdateUserLlmManual(field, value) {
  if (_wizState.users.length > 0) _wizState.users[0][field] = value;
}

function _wizRenderUserConfigs() {}

function _wizRenderVoiceGuide() {
  const area = document.getElementById('wiz-voice-guide');
  if (!area) return;
  const host = _wizState.haHostname || window.location.hostname;
  const url  = `http://${host}:8080`;
  const userModels = _wizState.users.map(u => u.id).filter(Boolean);
  const allModels  = ['ha-assist', 'ha-advanced', ...userModels];

  area.innerHTML = `
    <p style="font-size:13px;margin-bottom:var(--sp-4);">${t('wizard.voice_guide_intro')}</p>
    <div style="margin-bottom:var(--sp-3);">
      <div class="wizard-copy-row">
        <span style="font-size:12px;color:var(--muted);">HAANA URL:</span>
        <code style="background:var(--bg);padding:4px 8px;border-radius:var(--radius-sm);font-family:var(--mono);font-size:12px;">${escHtml(url)}</code>
        <button class="wizard-copy-btn" onclick="wizCopy('${escAttr(url)}')" title="${t('wizard.copy')}">&#128203;</button>
      </div>
    </div>
    <div style="margin-bottom:var(--sp-3);">
      <span style="font-size:12px;color:var(--muted);">${t('wizard.available_models')}:</span>
      <div style="display:flex;flex-wrap:wrap;gap:var(--sp-1);margin-top:var(--sp-1);">
        ${allModels.map(m => `<span class="tag" style="font-size:11px;">${escHtml(m)}</span>`).join('')}
      </div>
    </div>
    <ol style="font-size:12px;color:var(--muted);padding-left:20px;line-height:1.8;">
      <li>${t('wizard.voice_step1')}</li>
      <li>${t('wizard.voice_step2')}</li>
      <li>${t('wizard.voice_step3')} <code style="font-size:11px;background:var(--bg);padding:2px 5px;border-radius:3px;">${escHtml(url)}</code></li>
      <li>${t('wizard.voice_step4')}</li>
    </ol>`;
}

function wizCopy(text) {
  navigator.clipboard.writeText(text).then(() => toast(t('wizard.copied'), 'ok')).catch(() => {});
}

// ── Step 3: Extras ─────────────────────────────────────────────────────────
function _wizStep3Html() {
  return `
    <h2 class="wizard-section-title">${t('wizard.step3_title')}</h2>
    <p class="wizard-section-desc">${t('wizard.step3_desc')}</p>

    <!-- MCP -->
    <div class="wizard-section-block">
      <div class="wizard-block-header">${t('wizard.mcp_title')}</div>
      <div id="wiz-mcp-status">
        <div class="empty-state" style="padding:16px;"><div>&#8230;</div></div>
      </div>
    </div>

    <!-- Voice Pipeline -->
    <div class="wizard-section-block" id="wiz-pipeline-block" style="display:none;">
      <div class="wizard-block-header">${t('wizard.pipeline_title')}</div>
      <div id="wiz-pipeline-area"></div>
    </div>

    <!-- Dream Process -->
    <div class="wizard-section-block">
      <div class="wizard-block-header">${t('wizard.dream_title')}</div>
      <label class="checkbox-label">
        <input type="checkbox" id="wiz-dream-toggle"
          ${_wizState.dreamEnabled ? 'checked' : ''}
          onchange="_wizState.dreamEnabled = this.checked;">
        <span>
          <strong>${t('wizard.dream_enable')}</strong><br>
          <span style="font-size:11px;color:var(--muted);">${t('wizard.dream_desc')}</span>
        </span>
      </label>
    </div>`;
}

async function _wizStep3Init() {
  // MCP check
  try {
    const r = await fetch('/api/supervisor/addons');
    if (r.ok) {
      const d = await r.json();
      const addons = d.addons || d || [];
      _wizState.mcpAddonAvailable = addons.some(a =>
        (a.slug || a.name || '').toLowerCase().includes('mcp') ||
        (a.slug || a.name || '').toLowerCase().includes('ha-mcp')
      );
    }
  } catch(_) {}

  const mcpEl = document.getElementById('wiz-mcp-status');
  if (mcpEl) {
    if (_wizState.mcpAddonAvailable) {
      mcpEl.innerHTML = `<div class="wizard-info-card">
        <span style="color:var(--green);font-size:18px;">&#10003;</span>
        <div>
          <strong>${t('wizard.mcp_detected')}</strong><br>
          <span style="font-size:12px;color:var(--muted);">${t('wizard.mcp_detected_desc')}</span>
        </div>
      </div>`;
    } else {
      mcpEl.innerHTML = `<div class="wizard-info-card">
        <span style="font-size:18px;">&#8505;</span>
        <div>
          <span style="font-size:12px;color:var(--muted);">${t('wizard.mcp_not_detected')}</span>
          <br>
          <a href="https://github.com/voithos/ha-mcp-bridge" target="_blank" rel="noopener"
            style="font-size:12px;color:var(--accent2);">${t('wizard.mcp_install_link')}</a>
        </div>
      </div>`;
    }
  }

  // Pipelines
  try {
    const r = await fetch('/api/ha-pipelines');
    if (r.ok) {
      const d = await r.json();
      _wizState.pipelines = d.pipelines || d || [];
      if (_wizState.pipelines.length > 0) {
        const block = document.getElementById('wiz-pipeline-block');
        if (block) block.style.display = '';
        const pipeArea = document.getElementById('wiz-pipeline-area');
        if (pipeArea) {
          pipeArea.innerHTML = `
            <p style="font-size:12px;color:var(--muted);margin-bottom:var(--sp-3);">${t('wizard.pipeline_desc')}</p>
            <select id="wiz-pipeline-select" style="max-width:320px;" onchange="_wizState.selectedPipeline=this.value;">
              <option value="">${t('wizard.pipeline_skip')}</option>
              ${_wizState.pipelines.map(p => `<option value="${escAttr(p.id||p.name)}">${escHtml(p.name||p.id)}</option>`).join('')}
            </select>`;
        }
      }
    }
  } catch(_) {}
}

// ── Finish ─────────────────────────────────────────────────────────────────
async function wizFinish() {
  // Collect final values from step 2 selects if still visible (shouldn't be, but safe)
  const assistEl   = document.getElementById('wiz-ha-assist-llm');
  const advancedEl = document.getElementById('wiz-ha-advanced-llm');
  if (assistEl)   _wizState.haAssistLlm   = assistEl.value;
  if (advancedEl) _wizState.haAdvancedLlm = advancedEl.value;

  const finBtn = document.getElementById('wiz-finish-btn');
  if (finBtn) { finBtn.disabled = true; finBtn.textContent = t('common.loading') + '…'; }

  const content = document.getElementById('wizard-content');
  if (content) {
    content.innerHTML = `
      <div style="text-align:center;padding:var(--sp-8) 0;">
        <div style="font-size:48px;margin-bottom:var(--sp-4);">&#9881;</div>
        <h2 style="margin-bottom:var(--sp-2);">${t('wizard.finishing')}</h2>
        <p style="color:var(--muted);">${t('wizard.finishing_desc')}</p>
        <div class="wizard-spinner" style="margin:var(--sp-6) auto;"></div>
      </div>`;
  }

  try {
    const payload = {
      mode:          _wizState.mode || 'fresh',
      providers:     _wizState.providers.map(p => ({
        type:        p.type,
        name:        p.name,
        url:         p.url,
        key:         p.key,         // empty string = "do not change" in extend mode
        auth_method: p.authMethod || 'api_key',
        oauth_id:    p.oauthId || null,
      })),
      users:         _wizState.users,
      ha_assist_llm: _wizState.haAssistLlm,
      ha_advanced_llm: _wizState.haAdvancedLlm,
      extraction_llm: _wizState.extractionLlm || (_wizState.llms[0]?.label || ''),
      dream_enabled:  _wizState.dreamEnabled,
      pipeline:       _wizState.selectedPipeline,
    };

    const r = await fetch('/api/setup/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!r.ok) throw new Error(`HTTP ${r.status}`);

    if (content) {
      content.innerHTML = `
        <div style="text-align:center;padding:var(--sp-8) 0;">
          <div style="font-size:64px;margin-bottom:var(--sp-4);">&#127881;</div>
          <h2 style="margin-bottom:var(--sp-3);color:var(--green);">${t('wizard.done_title')}</h2>
          <p style="color:var(--muted);margin-bottom:var(--sp-6);">${t('wizard.done_desc')}</p>
          <button class="btn btn-primary" style="font-size:15px;padding:12px 32px;" onclick="wizClose()">
            ${t('wizard.start_chatting')} &#8594;
          </button>
        </div>`;
    }
    // Hide nav
    const nav = document.getElementById('wizard-nav');
    if (nav) nav.style.display = 'none';

  } catch(e) {
    toast(t('wizard.finish_error') + ': ' + e.message, 'err');
    if (content) {
      content.innerHTML = `
        <div style="text-align:center;padding:var(--sp-8) 0;">
          <div style="font-size:48px;margin-bottom:var(--sp-4);">&#10007;</div>
          <h2 style="margin-bottom:var(--sp-3);color:var(--red);">${t('wizard.finish_error')}</h2>
          <p style="color:var(--muted);">${e.message}</p>
          <button class="btn btn-secondary" style="margin-top:var(--sp-5);" onclick="_wizGoTo(3)">
            ${t('wizard.try_again')}
          </button>
        </div>`;
    }
    if (finBtn) { finBtn.disabled = false; finBtn.textContent = t('wizard.finish'); }
  }
}

function wizClose() {
  const overlay = document.getElementById('wizard-overlay');
  if (overlay) overlay.style.display = 'none';
  // Show normal UI
  document.querySelector('header')?.style?.removeProperty('display');
  document.querySelector('.tabs')?.style?.removeProperty('display');
  document.querySelectorAll('.panel').forEach(p => p.style.removeProperty('display'));
  // Reload page to get fresh state
  window.location.reload();
}

// Close wizard without saving (extend mode: discard changes, return to normal UI)
function wizardClose() {
  const overlay = document.getElementById('wizard-overlay');
  if (overlay) overlay.style.display = 'none';
}

// Render or remove the close button in the wizard header based on current mode
function _wizUpdateCloseBtn() {
  const header = document.querySelector('#wizard-overlay .wizard-header');
  if (!header) return;
  let closeBtn = document.getElementById('wiz-close-btn');
  if (_wizState.mode === 'extend') {
    if (!closeBtn) {
      closeBtn = document.createElement('button');
      closeBtn.id = 'wiz-close-btn';
      closeBtn.className = 'btn btn-secondary btn-sm';
      closeBtn.style.cssText = 'position:absolute;top:var(--sp-4);right:var(--sp-4);font-size:13px;';
      closeBtn.setAttribute('title', t('wizard.close_btn'));
      closeBtn.onclick = wizardClose;
      closeBtn.textContent = '\u2715 ' + t('wizard.close_btn');
      // Make header position:relative if not already
      header.style.position = 'relative';
      header.appendChild(closeBtn);
    }
  } else {
    if (closeBtn) closeBtn.remove();
  }
}
