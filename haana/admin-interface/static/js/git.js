// git.js – Git-Integration im Config-Tab (v1)
'use strict';

function loadGitStatus() {
  fetch('/api/git/status')
    .then(r => r.ok ? r.json() : Promise.reject(r))
    .then(data => {
      const badge = document.getElementById('git-branch-badge');
      const info  = document.getElementById('git-sync-info');
      if (!badge) return;
      badge.textContent = data.branch || '–';
      if (!data.remote) {
        if (info) info.textContent = t('git.not_connected');
        return;
      }
      const parts = [];
      if (data.ahead)  parts.push(t('git.status_ahead').replace('{{n}}', data.ahead));
      if (data.behind) parts.push(t('git.status_behind').replace('{{n}}', data.behind));
      if (data.dirty)  parts.push(t('git.status_dirty'));
      if (info) info.textContent = parts.join('  ');
    })
    .catch(() => {
      const badge = document.getElementById('git-branch-badge');
      if (badge) badge.textContent = '–';
    });
}

function gitPull() {
  const out = document.getElementById('git-output');
  if (out) { out.style.display = 'block'; out.textContent = '...'; }
  fetch('/api/git/pull', { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      if (out) out.textContent = data.output || '';
      if (data.ok) { toast(t('git.pull_success'), 'ok'); loadGitStatus(); }
      else toast(t('git.error') + ': ' + (data.output || ''), 'error');
    })
    .catch(e => toast(t('git.error') + ': ' + e.message, 'error'));
}

function gitPush() {
  const out = document.getElementById('git-output');
  if (out) { out.style.display = 'block'; out.textContent = '...'; }
  fetch('/api/git/push', { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      if (out) out.textContent = data.output || '';
      if (data.ok) toast(t('git.push_success'), 'ok');
      else toast(t('git.error') + ': ' + (data.output || ''), 'error');
    })
    .catch(e => toast(t('git.error') + ': ' + e.message, 'error'));
}

function gitConnect() {
  const url   = (document.getElementById('git-url-input')?.value || '').trim();
  const token = (document.getElementById('git-token-input')?.value || '').trim();
  if (!url) { toast(t('git.error') + ': URL fehlt', 'error'); return; }
  fetch('/api/git/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, token }),
  })
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        toast(t('git.save_btn'), 'ok');
        document.getElementById('git-token-input').value = '';
        const form = document.getElementById('git-connect-form');
        if (form) form.style.display = 'none';
        loadGitStatus();
      } else {
        toast(t('git.error') + ': ' + (data.detail || data.output || ''), 'error');
      }
    })
    .catch(e => toast(t('git.error') + ': ' + e.message, 'error'));
}

function gitShowLog() {
  fetch('/api/git/log')
    .then(r => r.json())
    .then(data => {
      const commits = Array.isArray(data) ? data : (data.commits || []);
      const lines = commits.map(c =>
        escHtml(c.hash || '') + '  ' + escHtml(c.date || '') + '  ' + escHtml(c.author || '') +
        '\n  ' + escHtml(c.msg || '')
      ).join('\n\n');
      const out = document.getElementById('git-output');
      if (out) {
        out.style.display = 'block';
        out.textContent = lines || '(keine Commits)';
      }
    })
    .catch(e => toast(t('git.error') + ': ' + e.message, 'error'));
}

function gitToggleConnect() {
  const form = document.getElementById('git-connect-form');
  if (!form) return;
  form.style.display = form.style.display === 'none' ? 'block' : 'none';
}
