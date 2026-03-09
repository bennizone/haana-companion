// status.js – Systemstatus laden/rendern (Qdrant, Ollama, Instanzen)

async function loadStatus() {
  const grid = document.getElementById('status-grid');
  grid.innerHTML = '<div class="status-card"><div class="empty-state"><div class="icon">...</div><div>' + t('status.checking') + '</div></div></div>';
  try {
    const [s, memStats] = await Promise.all([
      fetch('/api/status').then(r => r.json()),
      fetch('/api/memory-stats').then(r => r.json()).catch(() => []),
    ]);

    const qdrant = s.qdrant || {};
    const ollama = s.ollama || {};
    const logs   = s.logs   || {};

    const colls = (qdrant.collections || []).map(c => `<span class="tag">${escHtml(c)}</span>`).join(' ');
    const models = (ollama.models || []).map(m => `<span class="tag">${escHtml(m)}</span>`).join(' ');

    // Agent-Status + Health parallel
    const allInsts = INSTANCES;
    const agentHealth = await Promise.all(
      allInsts.map(async inst => {
        try {
          const r = await fetch(`/api/agent-health/${inst}`);
          return { inst, ...(await r.json()) };
        } catch { return { inst, ok: false }; }
      })
    );

    // Memory-Stats-Map für schnellen Lookup
    const memMap = {};
    (memStats || []).forEach(m => memMap[m.instance] = m);

    const agentRows = agentHealth.map(a => {
      const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${a.ok?'var(--green)':'var(--red)'};"></span>`;
      const mem = memMap[a.inst];
      const queueInfo = a.ok
        ? `<span style="font-size:11px;color:var(--muted);">Win: ${a.window_size??'?'} | Queue: ${a.pending_extractions??'?'}</span>`
        : `<span style="font-size:11px;color:var(--muted);">${t('status.offline')}</span>`;
      const memInfo = mem
        ? `<span style="font-size:11px;color:${mem.total_vectors===0&&mem.log_entries>0?'var(--yellow)':'var(--muted)'};">${mem.total_vectors} ${t('status.vectors')}</span>`
        : '';
      const controls = `
        <div style="display:flex;gap:4px;margin-top:6px;">
          <button class="btn btn-secondary" style="font-size:10px;padding:2px 8px;" onclick="instanceControl('${a.inst}','restart')">↺ Restart</button>
          <button class="btn btn-secondary" style="font-size:10px;padding:2px 8px;" onclick="instanceControl('${a.inst}','stop')">Stop</button>
          <button class="btn btn-danger"    style="font-size:10px;padding:2px 8px;" onclick="instanceForceStop('${a.inst}')">Kill</button>
        </div>`;
      return `<div class="status-row" style="flex-direction:column;align-items:flex-start;gap:2px;">
        <div style="display:flex;justify-content:space-between;width:100%;align-items:center;">
          <span style="font-weight:500;">${dot} ${a.inst}</span>
          <div style="display:flex;gap:8px;">${queueInfo}${memInfo}</div>
        </div>
        ${controls}
      </div>`;
    }).join('');

    const logRows = Object.entries(logs).map(([inst, info]) =>
      `<div class="status-row"><span>${inst}</span><span>${info.days} ${t('status.days_last')} ${info.latest||'–'}</span></div>`
    ).join('');

    grid.innerHTML = `
      <div class="status-card">
        <h3 style="display:flex;justify-content:space-between;align-items:center;">
          Qdrant
          <button class="btn btn-secondary" style="font-size:10px;padding:2px 8px;" onclick="qdrantRestart()">\u21ba Restart</button>
        </h3>
        <div class="status-row">
          <span>Status</span>
          <span class="${qdrant.ok ? 'status-ok' : 'status-err'}">${qdrant.ok ? '\u2713 ' + t('status.online') : '\u2717 ' + (qdrant.error||t('status.error'))}</span>
        </div>
        ${qdrant.ok ? `<div class="status-row"><span>Collections</span><div style="display:flex;flex-wrap:wrap;gap:4px;justify-content:flex-end;">
          ${(qdrant.collections||[]).map(c => `<span class="tag" style="display:inline-flex;align-items:center;gap:4px;">${escHtml(c)}<button onclick="deleteQdrantCollection('${escAttr(c)}')" style="background:none;border:none;color:var(--red);cursor:pointer;font-size:11px;padding:0;" title="${t('status.delete_label')}">\u2715</button></span>`).join('')||t('status.no_collections')}
        </div></div>` : ''}
      </div>
      <div class="status-card">
        <h3>Ollama</h3>
        <div class="status-row">
          <span>Status</span>
          <span class="${ollama.ok ? 'status-ok' : 'status-warn'}">${ollama.ok ? '\u2713 ' + t('status.online') : '\u26a0 ' + (ollama.error||t('status.not_reachable'))}</span>
        </div>
        ${ollama.ok ? `<div class="status-row"><span>${t('config_llm.model')}</span><div style="text-align:right;">${models||t('status.no_models')}</div></div>` : ''}
      </div>
      <div class="status-card" style="grid-column:1/-1;">
        <h3>${t('status.agent_instances')}</h3>
        ${agentRows || '<div style="color:var(--muted)">' + t('status.no_instances') + '</div>'}
      </div>
      <div class="status-card">
        <h3>${t('status.conversation_logs')}</h3>
        ${logRows || '<div style="color:var(--muted)">' + t('status.no_logs') + '</div>'}
      </div>
    `;

    // Rebuild-Banner (leer oder Dimensions-Mismatch)
    const banner = document.getElementById('rebuild-banner');
    if (banner) {
      const needsRebuild = !!qdrant.rebuild_suggested || !!qdrant.dims_mismatch;
      banner.classList.toggle('active', needsRebuild);
      const bannerText = banner.querySelector('.rebuild-banner-text');
      if (bannerText && qdrant.dims_mismatch) {
        bannerText.textContent = t('status.dims_mismatch');
      } else if (bannerText) {
        bannerText.textContent = t('status.rebuild_banner');
      }
    }

    const allOk = qdrant.ok;
    document.getElementById('header-dot').style.background = allOk ? 'var(--green)' : 'var(--red)';
    document.getElementById('header-status').textContent = allOk ? t('status.system_ok') : t('status.system_error');
  } catch(e) {
    grid.innerHTML = `<div class="status-card"><div class="empty-state"><div class="icon">!</div><div>${e.message}</div></div></div>`;
  }
}

// ── Instanz-Steuerung ───────────────────────────────────────────────────────
async function instanceControl(inst, action) {
  const r = await fetch(`/api/instances/${inst}/${action}`, { method: 'POST' });
  const d = await r.json();
  if (d.ok) { toast(inst + ': ' + action + ' \u2713', 'ok'); loadStatus(); }
  else       { toast(inst + ' ' + action + ' ' + t('status.action_failed') + ': ' + (d.error||'?').substring(0,60), 'err'); }
}

async function instanceForceStop(inst) {
  Modal.showDangerConfirm(t('status.force_stop_confirm', {instance: inst}), async () => {
    const r = await fetch(`/api/instances/${inst}/force-stop`, { method: 'POST' });
    const d = await r.json();
    if (d.ok) { toast(t('status.force_stop_done', {instance: inst}), 'ok'); loadStatus(); }
    else       { toast(t('status.force_stop_failed') + ': ' + (d.error||'?').substring(0,60), 'err'); }
  });
}

async function qdrantRestart() {
  Modal.showConfirm(t('status.qdrant_restart_confirm'), async () => {
    const r = await fetch('/api/qdrant/restart', { method: 'POST' });
    const d = await r.json();
    if (d.ok) { toast(t('status.qdrant_restarting') + ' \u2713', 'ok'); setTimeout(loadStatus, 3000); }
    else       { toast(t('status.qdrant_restart_failed') + ': ' + (d.error||'?').substring(0,60), 'err'); }
  });
}

async function deleteQdrantCollection(name) {
  Modal.showDangerConfirm(t('status.delete_collection_confirm', {name: name}), async () => {
    const r = await fetch(`/api/qdrant/collections/${encodeURIComponent(name)}`, { method: 'DELETE' });
    const d = await r.json();
    toast(d.result === true ? t('status.collection_deleted', {name: name}) + ' \u2713' : t('common.error') + ': ' + JSON.stringify(d).substring(0,60), d.result === true ? 'ok' : 'err');
    loadStatus();
    loadMemoryStats();
  });
}
