// chat.js – Konversationen laden/rendern, Send-Box, SSE Live-Updates

// ── Konversationen laden ───────────────────────────────────────────────────
async function loadConversations(inst) {
  const limit = document.getElementById('conv-limit')?.value || 50;
  const list  = document.getElementById('conv-list');
  if (!list) return;
  list.innerHTML = `<div class="empty-state"><div class="icon">&#8230;</div><div>${t('chat.loading')}</div></div>`;
  try {
    const r = await fetch(`/api/conversations/${inst}?limit=${limit}`);
    const data = await r.json();
    renderConversations(data);
  } catch(e) {
    list.innerHTML =
      `<div class="empty-state"><div class="icon">!</div><div>${escHtml(e.message)}</div></div>`;
  }
}

function channelBadge(ch) {
  const map = { repl:'REPL', whatsapp:'WhatsApp', webchat:'Webchat', ha_app:'HA App' };
  return `<span class="channel-badge ch-${ch}">${map[ch] || ch}</span>`;
}

function _renderModelBadge(r) {
  return r.model ? `<span class="model-badge">${escHtml(r.model)}</span>` : '';
}

function _renderMemorySection(r) {
  const memList = r.memory_results || [];
  if (!memList.length) return `<span style="color:var(--muted)">${t('chat.no_memories')}</span>`;
  return memList.map(m => `<div class="memory-line">${escHtml(m)}</div>`).join('');
}

function _renderToolsSection(r) {
  const tools = r.tool_calls || [];
  if (!tools.length) return `<span style="color:var(--muted)">${t('chat.no_tools')}</span>`;
  return tools.map(tc => {
    const inp = tc.input ? ` <span style="color:var(--muted);font-weight:400;">→ ${escHtml(String(tc.input).substring(0,80))}${String(tc.input).length>80?'…':''}</span>` : '';
    return `<span class="tool-chip">${escHtml(tc.tool)}${inp}</span>`;
  }).join('');
}

function _renderDetailSections(r) {
  const memCount = (r.memory_results || []).length;
  const toolCount = (r.tool_calls || []).length;
  return `
    <div class="detail-grid">
      <div class="detail-box">
        <div class="detail-label">${t('chat.user_full')}</div>
        <div class="detail-value">${escHtml(r.user || '')}</div>
      </div>
      <div class="detail-box">
        <div class="detail-label">${t('chat.haana_response')}</div>
        <div class="detail-value">${escHtml(r.assistant || '')}</div>
      </div>
    </div>
    <div class="detail-meta-row">
      ${r.model ? `<span class="detail-chip"><strong>${t('chat.model')}:</strong> ${escHtml(r.model)}</span>` : ''}
      <span class="detail-chip"><strong>${t('chat.latency')}:</strong> ${r.latency_s ?? '–'}s</span>
    </div>
    ${memCount > 0 ? `
    <details class="detail-collapsible">
      <summary>${t('chat.memories_used')} (${memCount})</summary>
      <div class="detail-collapsible-body">${_renderMemorySection(r)}</div>
    </details>` : ''}
    ${toolCount > 0 ? `
    <details class="detail-collapsible">
      <summary>${t('chat.tools_used')} (${toolCount})</summary>
      <div class="detail-collapsible-body">${_renderToolsSection(r)}</div>
    </details>` : ''}`;
}

function renderConversations(records) {
  const list = document.getElementById('conv-list');
  if (!list) return;
  if (!records.length) {
    list.innerHTML = '<div class="empty-state"><div class="icon">--</div><div>' + t('chat.no_conversations_instance') + '</div></div>';
    return;
  }
  list.innerHTML = records.map((r, i) => {
    const ts   = r.ts ? new Date(r.ts).toLocaleString('de-DE', {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '–';
    const user = escHtml(r.user || '').substring(0, 120);
    const asst = escHtml(r.assistant || '').substring(0, 200);
    const memHits = r.memory_hits > 0 ? ` (${r.memory_hits})` : '';
    const memBadge = r.memory_used
      ? `<span class="mem-badge mem-yes">Memory${memHits}</span>`
      : '<span class="mem-badge mem-no">' + t('chat.no_memory') + '</span>';

    return `
    <div class="conv-card" id="card-${i}">
      <div class="conv-header" onclick="toggleCard(${i})">
        <div class="conv-meta">
          <span class="conv-time">${ts}</span>
          ${channelBadge(r.channel || 'repl')}
          ${_renderModelBadge(r)}
        </div>
        <div class="conv-messages">
          <div class="conv-user"><strong>${t('chat.you')}</strong> ${user}${r.user?.length > 120 ? '…' : ''}</div>
          <div class="conv-assistant"><em>${asst}${r.assistant?.length > 200 ? '…' : ''}</em></div>
        </div>
        <div class="expand-icon">›</div>
      </div>
      <div class="conv-details">
        ${_renderDetailSections(r)}
      </div>
    </div>`;
  }).join('');
}

function toggleCard(i) {
  document.getElementById('card-' + i)?.classList.toggle('expanded');
}

// ── SSE Live-Updates ───────────────────────────────────────────────────────
function startSSE(inst) {
  if (sse) { sse.close(); sse = null; }
  const dot   = document.getElementById('live-dot');
  const label = document.getElementById('live-label');

  sse = new EventSource(`/api/events/${inst}`);
  sse.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'connected') {
      if (dot)   dot.classList.remove('offline');
      if (label) label.textContent = t('chat.live');
    } else if (msg.type === 'conversation') {
      prependConversation(msg.record);
    }
  };
  sse.onerror = () => {
    if (dot)   dot.classList.add('offline');
    if (label) label.textContent = t('chat.sse_offline');
  };
}

function prependConversation(record) {
  const list = document.getElementById('conv-list');
  if (!list) return;
  const emptyState = list.querySelector('.empty-state');
  if (emptyState) list.innerHTML = '';

  const div = document.createElement('div');
  const i = 'live-' + Date.now();
  div.innerHTML = renderSingleConv(record, i);
  list.insertAdjacentElement('afterbegin', div.firstElementChild);
}

function renderSingleConv(r, cardId) {
  const ts    = r.ts ? new Date(r.ts).toLocaleString('de-DE', {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '–';
  const user  = escHtml(r.user || '').substring(0, 120);
  const asst  = escHtml(r.assistant || '').substring(0, 200);
  const memBadge = r.memory_used ? '<span class="mem-badge mem-yes">Memory</span>' : '';
  return `
  <div class="conv-card" id="${cardId}" style="border-color:var(--accent);">
    <div class="conv-header" onclick="this.closest('.conv-card').classList.toggle('expanded')">
      <div class="conv-meta">
        <span class="conv-time">${ts}</span>
        ${channelBadge(r.channel || 'repl')}
        ${_renderModelBadge(r)}
        ${memBadge}
      </div>
      <div class="conv-messages">
        <div class="conv-user"><strong>${t('chat.you')}</strong> ${user}</div>
        <div class="conv-assistant"><em>${asst}</em></div>
      </div>
      <div class="expand-icon">›</div>
    </div>
    <div class="conv-details">
      ${_renderDetailSections(r)}
    </div>
  </div>`;
}

// ── Chat-Eingabe ───────────────────────────────────────────────────────────
function handleChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
}

async function sendChat() {
  const input  = document.getElementById('chat-input');
  const status = document.getElementById('chat-status');
  const btn    = document.getElementById('send-btn');
  const msg    = input.value.trim();
  if (!msg) return;
  if (!currentInstance || currentInstance === '__all__') {
    toast(t('chat.select_instance'), 'error');
    return;
  }

  input.value = '';
  input.style.borderColor = 'var(--border)';
  btn.disabled = true;
  btn.textContent = '...';
  status.textContent = '\u23f3 ' + currentInstance + ' ' + t('chat.thinking');

  // Optimistisch sofort anzeigen
  const tempId = 'pending-' + Date.now();
  prependPendingMessage(msg, tempId);

  try {
    const r = await fetch(`/api/chat/${currentInstance}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: msg }),
    });

    if (!r.ok) {
      const err = await r.json().catch(() => ({detail: r.statusText}));
      const errMsg = err.detail || t('chat.unknown_error');
      updatePendingMessage(tempId, msg, '❌ ' + errMsg, true);
      status.textContent = '❌ ' + errMsg;
    } else {
      const data = await r.json();
      updatePendingMessage(tempId, msg, data.response, false);
      status.textContent = '';
    }
  } catch(e) {
    updatePendingMessage(tempId, msg, '\u274c ' + t('chat.connection_error') + ': ' + e.message, true);
    status.textContent = '\u274c ' + e.message;
  }

  btn.disabled = false;
  btn.textContent = t('chat.send_btn');
  input.focus();
}

function prependPendingMessage(userMsg, cardId) {
  const list = document.getElementById('conv-list');
  if (!list) return;
  const empty = list.querySelector('.empty-state');
  if (empty) list.innerHTML = '';

  const now = new Date().toLocaleString('de-DE', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
  const html = `
  <div class="conv-card" id="${cardId}" style="border-color:var(--accent2);opacity:0.7;">
    <div class="conv-header" style="cursor:default;">
      <div class="conv-meta">
        <span class="conv-time">${now}</span>
        <span class="channel-badge ch-webchat">Webchat</span>
      </div>
      <div class="conv-messages">
        <div class="conv-user"><strong>${t('chat.you')}</strong> ${escHtml(userMsg)}</div>
        <div class="conv-assistant" id="${cardId}-resp" style="color:var(--muted);">
          <span style="animation:pulse 1s infinite;">…</span>
        </div>
      </div>
    </div>
  </div>`;
  list.insertAdjacentHTML('afterbegin', html);
}

function updatePendingMessage(cardId, userMsg, response, isError) {
  const card = document.getElementById(cardId);
  if (!card) return;
  card.style.opacity = '1';
  card.style.borderColor = isError ? 'var(--red)' : 'var(--border)';
  const respEl = document.getElementById(cardId + '-resp');
  if (respEl) {
    respEl.style.color = isError ? 'var(--red)' : '';
    respEl.innerHTML = `<em>${escHtml(response)}</em>`;
  }
  const header = card.querySelector('.conv-header');
  if (header && !isError) {
    header.style.cursor = 'pointer';
    header.onclick = () => card.classList.toggle('expanded');
    card.insertAdjacentHTML('beforeend', `
      <div class="conv-details">
        <div class="detail-grid">
          <div class="detail-box"><div class="detail-label">${t('chat.user_full')}</div><div class="detail-value">${escHtml(userMsg)}</div></div>
          <div class="detail-box"><div class="detail-label">${t('chat.haana_response')}</div><div class="detail-value">${escHtml(response)}</div></div>
        </div>
      </div>`);
    const expandIcon = document.createElement('div');
    expandIcon.className = 'expand-icon';
    expandIcon.textContent = '›';
    header.appendChild(expandIcon);
  }
}

async function checkAgentHealth(inst) {
  const el = document.getElementById('agent-status');
  if (!el) return;
  try {
    const r = await fetch(`/api/agent-health/${inst}`);
    const d = await r.json();
    el.textContent = d.ok ? '\u25cf ' + t('chat.agent_online') : '\u25cf ' + t('chat.agent_offline');
    el.style.color  = d.ok ? 'var(--green)' : 'var(--red)';
  } catch {
    el.textContent = '\u25cf ' + t('chat.agent_unreachable');
    el.style.color = 'var(--red)';
  }
}
