// users.js – User-CRUD, Container-Management (restart/stop/delete)

let editingUserId = null;

function _llmOpts(selectedId) {
  if (!cfg || !cfg.llms) return `<option value="">--</option>`;
  return cfg.llms.map(l => {
    const prov = (cfg.providers || []).find(p => p.id === l.provider_id);
    const label = `${l.name} (${prov ? prov.name : l.provider_id} · ${l.model || '\u2013'})`;
    return `<option value="${escAttr(l.id)}" ${l.id === selectedId ? 'selected' : ''}>${escHtml(label)}</option>`;
  }).join('');
}

function renderUserCard(u) {
  const sc = u.container_status === 'running' ? 'var(--green)' : u.container_status === 'absent' ? 'var(--muted)' : 'var(--red)';
  const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${sc};flex-shrink:0;"></span>`;
  const roleColor = {'admin':'var(--accent2)','voice':'var(--yellow)','voice-advanced':'var(--blue)','user':'var(--green)'}[u.role]||'var(--muted)';
  const sysTag = u.system ? `<span style="font-size:10px;background:rgba(96,165,250,.15);color:var(--blue);border-radius:4px;padding:1px 6px;margin-left:6px;">System</span>` : '';
  const isVoice = ['voice','voice-advanced'].includes(u.role);
  const waInfo = !isVoice && u.whatsapp_phone ? ` \u00b7 WA: ${escHtml(u.whatsapp_phone)}` : '';
  const haInfo = !isVoice && u.ha_user ? ` \u00b7 HA: ${escHtml(u.ha_user)}` : '';

  return `
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;" id="user-card-${escAttr(u.id)}">
    <!-- Header -->
    <div style="padding:12px 16px;display:flex;align-items:center;gap:10px;">
      ${dot}
      <div style="flex:1;min-width:0;">
        <span style="font-weight:600;">${escHtml(u.display_name)}</span>
        <span style="color:var(--muted);font-size:12px;margin-left:6px;">(${escHtml(u.id)})</span>
        <span style="color:${roleColor};font-size:12px;margin-left:6px;">${escHtml(u.role)}</span>${sysTag}
        <div style="font-size:11px;color:var(--muted);margin-top:2px;">
          Port :${u.api_port}${haInfo}${waInfo} \u00b7 ${escHtml(u.container_status)}
        </div>
      </div>
      <div style="display:flex;gap:5px;flex-shrink:0;" onclick="event.stopPropagation()">
        <button class="btn btn-secondary" style="font-size:11px;padding:3px 8px;" title="Neu starten" onclick="restartUserContainer('${escAttr(u.id)}')">↺</button>
        <button class="btn btn-secondary" style="font-size:11px;padding:3px 8px;" title="Stoppen" onclick="stopUserContainer('${escAttr(u.id)}')">Stop</button>
        <button class="btn btn-secondary" style="font-size:11px;padding:3px 10px;" id="uedit-btn-${escAttr(u.id)}" onclick="toggleUserExpand('${escAttr(u.id)}')">\u270e ${t('users.edit')}</button>
        ${!u.system ? `<button class="btn btn-danger" style="font-size:11px;padding:3px 8px;" onclick="deleteUser('${escAttr(u.id)}')">\u2715</button>` : ''}
      </div>
    </div>
    <!-- Expandable edit form -->
    <div id="user-expand-${escAttr(u.id)}" style="display:none;border-top:1px solid var(--border);padding:16px 16px 12px;">
      <div class="form-row">
        <div class="form-group">
          <label>${t('users.display_name')}</label>
          <input type="text" id="uf-${escAttr(u.id)}-name" value="${escAttr(u.display_name||'')}">
        </div>
        <div class="form-group">
          <label>${t('users.role')}</label>
          <select id="uf-${escAttr(u.id)}-role">
            <option value="user"  ${u.role==='user' ?'selected':''}>${t('users.role_user')}</option>
            <option value="admin" ${u.role==='admin'?'selected':''}>${t('users.role_admin')}</option>
          </select>
        </div>
      </div>
      ${!isVoice ? `
      <div class="form-row">
        <div class="form-group">
          <label>${t('users.ha_user')} <span style="font-size:11px;color:var(--muted);">(${t('users.ha_user_hint')})</span></label>
          <div style="display:flex;gap:6px;">
            <select id="uf-${escAttr(u.id)}-ha" style="flex:1;">
              <option value="${escAttr(u.ha_user||'')}">${escHtml(u.ha_user||t('users.not_assigned'))}</option>
            </select>
            <button class="btn btn-secondary" style="font-size:11px;padding:4px 8px;flex-shrink:0;" onclick="loadHaUsersForCard('${escAttr(u.id)}')">↺</button>
          </div>
          <span id="uf-${escAttr(u.id)}-ha-status" style="font-size:11px;color:var(--muted);"></span>
        </div>
        <div class="form-group">
          <label>${t('users.wa_phone')} <span style="font-size:11px;color:var(--muted);">(${t('users.wa_phone_hint')})</span></label>
          <input type="text" id="uf-${escAttr(u.id)}-wa-phone" value="${escAttr(u.whatsapp_phone||'')}" placeholder="${t('users.wa_phone_placeholder')}">
        </div>
      </div>
      ` : ''}
      <div class="form-row three">
        <div class="form-group">
          <label>${t('users.primary_llm')}</label>
          <select id="uf-${escAttr(u.id)}-primary-llm">${_llmOpts(u.primary_llm)}</select>
        </div>
        <div class="form-group">
          <label>${t('users.fallback_llm')}</label>
          <select id="uf-${escAttr(u.id)}-fallback-llm">
            <option value="">--</option>
            ${_llmOpts(u.fallback_llm)}
          </select>
        </div>
        <div class="form-group">
          <label title="${t('users.language_help')}">${t('users.language')}</label>
          <select id="uf-${escAttr(u.id)}-language">
            <option value="de" ${(u.language||'de')==='de'?'selected':''}>Deutsch</option>
            <option value="en" ${(u.language||'de')==='en'?'selected':''}>English</option>
            <option value="tr" ${(u.language||'de')==='tr'?'selected':''}>Türkçe</option>
            <option value="fr" ${(u.language||'de')==='fr'?'selected':''}>Français</option>
            <option value="es" ${(u.language||'de')==='es'?'selected':''}>Español</option>
            <option value="it" ${(u.language||'de')==='it'?'selected':''}>Italiano</option>
          </select>
          <span style="font-size:11px;color:var(--muted);">${t('users.language_help')}</span>
        </div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:8px;">
        <button class="btn btn-primary" onclick="saveUserEdit('${escAttr(u.id)}')">${t('common.save')}</button>
        <button class="btn btn-secondary" onclick="toggleUserExpand('${escAttr(u.id)}')">${t('common.cancel')}</button>
        <span id="uf-${escAttr(u.id)}-status" style="font-size:12px;color:var(--muted);"></span>
      </div>
      <!-- CLAUDE.md Inline-Editor -->
      <div style="margin-top:14px;border-top:1px solid var(--border);padding-top:12px;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;">
          <span style="font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;">CLAUDE.md</span>
          <span id="uf-${escAttr(u.id)}-md-status" style="font-size:11px;color:var(--muted);flex:1;min-width:0;"></span>
        </div>
        <!-- Preview (always visible when card is expanded, shows first lines) -->
        <div id="uf-${escAttr(u.id)}-md-preview-wrap"
             style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 10px;cursor:pointer;margin-bottom:8px;"
             onclick="openUserClaudeMdEditor('${escAttr(u.id)}')"
             title="${t('users.claude_md_preview')}">
          <pre id="uf-${escAttr(u.id)}-md-preview"
               style="margin:0;font-family:var(--mono);font-size:11px;color:var(--muted);max-height:80px;overflow:hidden;white-space:pre-wrap;word-break:break-word;">${t('common.loading')}</pre>
        </div>
        <!-- Full editor (toggled) -->
        <div id="uf-${escAttr(u.id)}-md-editor" style="display:none;">
          <textarea id="uf-${escAttr(u.id)}-md-content" spellcheck="false"
            style="width:100%;min-height:260px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:10px;font-family:var(--mono);font-size:12px;resize:vertical;box-sizing:border-box;"></textarea>
          <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:center;">
            <button class="btn btn-primary" onclick="saveUserClaudeMd('${escAttr(u.id)}')">${t('users.save_claude_md')}</button>
            <button class="btn btn-secondary" onclick="loadDefaultClaudeMd('${escAttr(u.id)}')">\u21ba ${t('users.load_role_default')}</button>
            <button class="btn btn-secondary" onclick="closeUserClaudeMdEditor('${escAttr(u.id)}')">${t('users.claude_md_collapse')}</button>
          </div>
        </div>
        <!-- Edit button (visible when editor is closed) -->
        <div id="uf-${escAttr(u.id)}-md-edit-btn-wrap">
          <button class="btn btn-secondary" style="font-size:11px;padding:3px 10px;"
            onclick="openUserClaudeMdEditor('${escAttr(u.id)}')">\u270e ${t('users.claude_md_expand')}</button>
        </div>
      </div>
    </div>
  </div>`;
}

async function loadUsers() {
  const list = document.getElementById('user-list');
  if (!cfg) await loadConfig();
  try {
    const r = await fetch('/api/users');
    const users = await r.json();
    if (!users.length) {
      list.innerHTML = '<div class="empty-state"><div class="icon">--</div><div>' + t('users.no_users') + '</div></div>';
      return;
    }
    list.innerHTML = users.map(u => renderUserCard(u)).join('');
  } catch(e) {
    list.innerHTML = `<div class="empty-state"><div class="icon">!</div><div>${e.message}</div></div>`;
  }
}

function toggleUserExpand(uid) {
  const el  = document.getElementById(`user-expand-${uid}`);
  const btn = document.getElementById(`uedit-btn-${uid}`);
  if (!el) return;
  const open = el.style.display !== 'none';
  el.style.display = open ? 'none' : 'block';
  if (btn) btn.textContent = open ? '\u270e ' + t('users.edit') : '\u25b2 ' + t('users.close_edit');
  if (!open) {
    loadHaUsersForCard(uid);
    loadUserClaudeMdPreview(uid);
  }
}

async function loadUserClaudeMdPreview(uid) {
  const preview = document.getElementById(`uf-${uid}-md-preview`);
  const status  = document.getElementById(`uf-${uid}-md-status`);
  if (!preview) return;
  try {
    const r = await fetch(`/api/claude-md/${uid}`);
    if (r.ok) {
      const d = await r.json();
      const content = d.content || '';
      // Store full content for the editor
      const contentEl = document.getElementById(`uf-${uid}-md-content`);
      if (contentEl) contentEl.value = content;
      // Show first 5 lines in preview
      const lines = content.split('\n').slice(0, 5);
      const hasMore = content.split('\n').length > 5;
      preview.textContent = lines.join('\n') + (hasMore ? '\n\u2026' : '');
    } else {
      preview.textContent = t('users.not_found');
    }
  } catch(e) {
    preview.textContent = '\u2717 ' + e.message;
    if (status) { status.textContent = e.message; status.style.color = 'var(--red)'; }
  }
}

function openUserClaudeMdEditor(uid) {
  const editor     = document.getElementById(`uf-${uid}-md-editor`);
  const previewWrap = document.getElementById(`uf-${uid}-md-preview-wrap`);
  const editBtnWrap = document.getElementById(`uf-${uid}-md-edit-btn-wrap`);
  if (!editor) return;
  // If content not yet loaded, fetch first
  const contentEl = document.getElementById(`uf-${uid}-md-content`);
  if (contentEl && !contentEl.value) {
    fetch(`/api/claude-md/${uid}`).then(r => r.ok ? r.json() : {content:''}).then(d => {
      contentEl.value = d.content || '';
    }).catch(() => {});
  }
  editor.style.display      = 'block';
  if (previewWrap) previewWrap.style.display = 'none';
  if (editBtnWrap) editBtnWrap.style.display = 'none';
}

function closeUserClaudeMdEditor(uid) {
  const editor     = document.getElementById(`uf-${uid}-md-editor`);
  const previewWrap = document.getElementById(`uf-${uid}-md-preview-wrap`);
  const editBtnWrap = document.getElementById(`uf-${uid}-md-edit-btn-wrap`);
  if (editor) editor.style.display = 'none';
  if (previewWrap) previewWrap.style.display = '';
  if (editBtnWrap) editBtnWrap.style.display = '';
  // Refresh preview with latest content from textarea
  const contentEl = document.getElementById(`uf-${uid}-md-content`);
  const preview   = document.getElementById(`uf-${uid}-md-preview`);
  if (contentEl && preview) {
    const lines = contentEl.value.split('\n').slice(0, 5);
    const hasMore = contentEl.value.split('\n').length > 5;
    preview.textContent = lines.join('\n') + (hasMore ? '\n\u2026' : '');
  }
}

async function loadHaUsersForCard(uid) {
  const sel    = document.getElementById(`uf-${uid}-ha`);
  const status = document.getElementById(`uf-${uid}-ha-status`);
  if (!sel) return;
  const currentVal = sel.value;
  try {
    const r = await fetch('/api/ha-users');
    const data = await r.json();
    if (data.ok && data.users.length > 0) {
      sel.innerHTML = '<option value="">' + t('users.not_assigned') + '</option>' +
        data.users.map(u =>
          `<option value="${escAttr(u.id)}" ${u.id === currentVal ? 'selected' : ''}>${escHtml(u.display_name)} (${escHtml(u.id)})</option>`
        ).join('');
      if (status) { status.textContent = data.users.length + ' ' + t('users.ha_users_found'); status.style.color = 'var(--green)'; }
    } else {
      if (status) { status.textContent = data.error || t('users.ha_not_configured'); status.style.color = 'var(--yellow)'; }
    }
  } catch(e) {
    if (status) { status.textContent = t('users.ha_load_error'); status.style.color = 'var(--red)'; }
  }
}

async function saveUserEdit(uid) {
  const status = document.getElementById(`uf-${uid}-status`);
  const isVoice = ['voice','voice-advanced'].includes(document.getElementById(`uf-${uid}-role`)?.value || '');
  const body = {
    display_name:   document.getElementById(`uf-${uid}-name`)?.value?.trim(),
    role:           document.getElementById(`uf-${uid}-role`)?.value,
    primary_llm:    document.getElementById(`uf-${uid}-primary-llm`)?.value || '',
    fallback_llm:   document.getElementById(`uf-${uid}-fallback-llm`)?.value || '',
    language:       document.getElementById(`uf-${uid}-language`)?.value || 'de',
  };
  if (!isVoice) {
    body.ha_user        = document.getElementById(`uf-${uid}-ha`)?.value || '';
    body.whatsapp_phone = document.getElementById(`uf-${uid}-wa-phone`)?.value?.trim() || '';
  }
  if (status) { status.textContent = '\u2026'; status.style.color = 'var(--muted)'; }
  try {
    const r = await fetch(`/api/users/${uid}`, {
      method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      if (d.restarted) {
        const restartOk = d.container && d.container.ok;
        toast(t('users.user_saved', {uid: uid}) + ' \u2713 — ' +
          (restartOk ? t('users.agent_restarted') : t('users.agent_restart_failed')),
          restartOk ? 'ok' : 'warn');
      } else {
        toast(t('users.user_saved', {uid: uid}) + ' \u2713', 'ok');
      }
      loadUsers();
    } else {
      const err = d.detail || d.error || t('chat.unknown_error');
      if (status) { status.textContent = '\u2717 ' + err.substring(0,80); status.style.color = 'var(--red)'; }
      toast(err.substring(0,60), 'err');
    }
  } catch(e) {
    if (status) { status.textContent = '\u2717 ' + e.message; status.style.color = 'var(--red)'; }
  }
}

async function toggleUserClaudeMd(uid) {
  const editor = document.getElementById(`uf-${uid}-md-editor`);
  if (!editor) return;
  if (editor.style.display !== 'none') {
    closeUserClaudeMdEditor(uid);
    return;
  }
  openUserClaudeMdEditor(uid);
}

async function loadDefaultClaudeMd(uid) {
  const status = document.getElementById(`uf-${uid}-md-status`);
  const role   = document.getElementById(`uf-${uid}-role`)?.value || 'user';
  const tpl    = role === 'admin' ? 'admin' : 'user';
  try {
    const r = await fetch(`/api/claude-md-template/${tpl}`);
    const d = await r.json();
    const content = document.getElementById(`uf-${uid}-md-content`);
    if (content) content.value = d.content || '';
    openUserClaudeMdEditor(uid);
    if (status)  { status.textContent = t('users.template_loaded', {tpl: d.template}); status.style.color = 'var(--green)'; setTimeout(() => { status.textContent = ''; }, 3000); }
  } catch(e) {
    if (status) { status.textContent = e.message; status.style.color = 'var(--red)'; }
  }
}

async function saveUserClaudeMd(uid) {
  const status  = document.getElementById(`uf-${uid}-md-status`);
  const content = document.getElementById(`uf-${uid}-md-content`)?.value || '';
  try {
    const r = await fetch(`/api/claude-md/${uid}`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ content }),
    });
    const d = await r.json();
    if (d.ok) {
      toast(t('users.claude_md_saved', {uid: uid}) + ' \u2713', 'ok');
      if (status) { status.textContent = '\u2713 ' + t('users.claude_md_saved_short'); status.style.color = 'var(--green)'; setTimeout(() => { status.textContent = ''; }, 3000); }
    } else {
      if (status) { status.textContent = '\u2717 ' + t('common.error'); status.style.color = 'var(--red)'; }
    }
  } catch(e) {
    if (status) { status.textContent = '\u2717 ' + e.message; status.style.color = 'var(--red)'; }
  }
}

function showNewUserCard() {
  document.getElementById('new-user-card').style.display = '';
  loadHaUsersForNewUser();
  document.getElementById('nuf-id').focus();
}

async function loadHaUsersForNewUser() {
  const sel = document.getElementById('nuf-ha-user');
  const status = document.getElementById('nuf-ha-status');
  if (!sel) return;
  try {
    const r = await fetch('/api/ha-users');
    const data = await r.json();
    if (data.ok && data.users.length > 0) {
      sel.innerHTML = '<option value="">-- HA Person auswählen --</option>' +
        data.users.map(u =>
          `<option value="${escAttr(u.id)}" data-name="${escAttr(u.display_name)}">${escHtml(u.display_name)} (${escHtml(u.id)})</option>`
        ).join('');
      if (status) { status.textContent = data.users.length + ' Personen gefunden'; status.style.color = 'var(--green)'; }
    } else {
      if (status) { status.textContent = data.error || 'HA nicht erreichbar'; status.style.color = 'var(--yellow)'; }
    }
  } catch(e) {
    if (status) { status.textContent = 'Fehler: ' + e.message; status.style.color = 'var(--red)'; }
  }
}

function onNewUserHaSelect() {
  const sel = document.getElementById('nuf-ha-user');
  const idEl = document.getElementById('nuf-id');
  const nameEl = document.getElementById('nuf-display-name');
  if (!sel || !sel.value) return;
  // Extract name from data-name attribute
  const opt = sel.options[sel.selectedIndex];
  const haName = opt?.getAttribute('data-name') || '';
  // Extract slug from entity ID: "person.benni" → "benni"
  const haId = sel.value; // e.g. "person.benni"
  const slug = haId.includes('.') ? haId.split('.').slice(1).join('.') : haId;
  // Auto-fill ID and name if empty
  if (idEl && !idEl.value) idEl.value = slug.replace(/[^a-z0-9-]/g, '-').toLowerCase();
  if (nameEl && !nameEl.value) nameEl.value = haName;
}

async function submitNewUser() {
  const st  = document.getElementById('nuf-status');
  const uid = document.getElementById('nuf-id').value.trim();
  if (!uid) { st.textContent = '\u26a0 ' + t('users.id_missing'); st.style.color = 'var(--yellow)'; return; }
  const payload = {
    id:           uid,
    display_name: document.getElementById('nuf-display-name').value.trim() || uid,
    role:         document.getElementById('nuf-role').value,
    claude_md_template: document.getElementById('nuf-template').value,
    language: document.getElementById('nuf-language').value,
    ha_user:      document.getElementById('nuf-ha-user')?.value || '',
  };
  st.textContent = '\u2026'; st.style.color = 'var(--muted)';
  try {
    const r = await fetch('/api/users', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      const cOk = d.container?.ok !== false;
      st.textContent = cOk ? '\u2713 ' + t('users.created_port', {port: d.user?.api_port}) : '\u26a0 ' + t('users.created_container_error') + ': ' + (d.container?.error||'?').substring(0,60);
      st.style.color = cOk ? 'var(--green)' : 'var(--yellow)';
      document.getElementById('new-user-card').style.display = 'none';
      toast(t('users.user_created') + ' \u2713', 'ok');
      loadUsers();
    } else {
      const err = d.detail || d.error || t('common.error');
      st.textContent = '\u2717 ' + err.substring(0,80); st.style.color = 'var(--red)';
    }
  } catch(e) { st.textContent = '\u2717 ' + e.message; st.style.color = 'var(--red)'; }
}

async function deleteUser(userId) {
  Modal.showDangerConfirm(t('users.delete_confirm', {name: userId}), async () => {
    try {
      const r = await fetch(`/api/users/${userId}`, { method: 'DELETE' });
      const d = await r.json();
      if (d.ok) { toast(t('users.user_deleted'), 'ok'); loadUsers(); }
      else       { toast(d.detail || t('users.delete_error'), 'err'); }
    } catch(e) { toast(e.message, 'err'); }
  });
}

async function restartUserContainer(userId) {
  const r = await fetch(`/api/users/${userId}/restart`, { method: 'POST' });
  const d = await r.json();
  if (d.ok) { toast(t('users.container_restarted', {uid: userId}) + ' \u2713', 'ok'); loadUsers(); }
  else       { toast(t('common.error') + ': ' + (d.container?.error || d.error || '?').substring(0,80), 'err'); }
}

async function stopUserContainer(userId) {
  const r = await fetch(`/api/users/${userId}/stop`, { method: 'POST' });
  const d = await r.json();
  if (d.ok) { toast(t('users.container_stopped', {uid: userId}), 'ok'); loadUsers(); }
  else       { toast(t('common.error') + ': ' + (d.error || '?').substring(0,80), 'err'); }
}
