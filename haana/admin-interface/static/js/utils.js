// Utility functions

function escHtml(s) {
  if (!s) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Also escapes single quotes for safe use in HTML attributes
function escAttr(s) {
  return escHtml(s).replace(/'/g, '&#39;');
}

// Toast notifications
let _toastTimer = null;

function toast(msg, type) {
  type = type || 'ok';
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'show ' + type;
  if (_toastTimer) clearTimeout(_toastTimer);
  const delay = type === 'err' ? 15000 : 3500;
  _toastTimer = setTimeout(() => { el.className = ''; }, delay);
}

// Set status text on an element with consistent styling
function setStatus(elementId, text, type) {
  const el = document.getElementById(elementId);
  if (!el) return;
  el.textContent = text;
  const colors = { ok: 'var(--green)', err: 'var(--red)', warn: 'var(--yellow)', muted: 'var(--muted)' };
  el.style.color = colors[type] || colors.muted;
}

// Wrap an async action with button loading state
async function withLoading(btn, asyncFn) {
  if (!btn || btn.disabled) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '...';
  try {
    return await asyncFn();
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}
