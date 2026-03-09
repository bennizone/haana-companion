// Reusable modal system
// Usage: showConfirm(t('key'), () => { ... })
//        showModal({ title, body, onConfirm, confirmText, cancelText })

const Modal = (() => {
  let _overlay = null;

  function _ensureOverlay() {
    if (_overlay) return _overlay;
    _overlay = document.createElement('div');
    _overlay.className = 'modal-overlay';
    _overlay.addEventListener('click', (e) => {
      if (e.target === _overlay) close();
    });
    document.body.appendChild(_overlay);
    return _overlay;
  }

  function show({ title, body, onConfirm, onCancel, confirmText, cancelText, confirmClass, hideCancel }) {
    const overlay = _ensureOverlay();

    const html = `
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-title">${escHtml(title || '')}</span>
          <button class="btn btn-secondary modal-close-btn" data-modal-close>&times;</button>
        </div>
        <div class="modal-body">${body || ''}</div>
        <div class="modal-footer">
          ${!hideCancel ? `<button class="btn btn-secondary" data-modal-cancel>${escHtml(cancelText || t('common.cancel'))}</button>` : ''}
          ${onConfirm ? `<button class="btn ${confirmClass || 'btn-primary'}" data-modal-confirm>${escHtml(confirmText || t('common.confirm'))}</button>` : ''}
        </div>
      </div>
    `;
    overlay.innerHTML = html;
    overlay.classList.add('active');
    document.body.style.overflow = 'hidden';

    // Event handlers
    overlay.querySelector('[data-modal-close]')?.addEventListener('click', () => {
      close();
      if (onCancel) onCancel();
    });
    overlay.querySelector('[data-modal-cancel]')?.addEventListener('click', () => {
      close();
      if (onCancel) onCancel();
    });
    overlay.querySelector('[data-modal-confirm]')?.addEventListener('click', () => {
      close();
      if (onConfirm) onConfirm();
    });

    // Focus confirm button for keyboard users
    overlay.querySelector('[data-modal-confirm]')?.focus();

    // ESC to close
    const escHandler = (e) => {
      if (e.key === 'Escape') {
        close();
        if (onCancel) onCancel();
        document.removeEventListener('keydown', escHandler);
      }
    };
    document.addEventListener('keydown', escHandler);
  }

  function close() {
    if (_overlay) {
      _overlay.classList.remove('active');
      _overlay.innerHTML = '';
      document.body.style.overflow = '';
    }
  }

  // Convenience: confirm dialog (replaces browser confirm())
  function showConfirm(message, onConfirm, opts) {
    opts = opts || {};
    show({
      title: opts.title || t('common.confirm'),
      body: `<p class="modal-message">${escHtml(message).replace(/\n/g, '<br>')}</p>`,
      onConfirm,
      confirmText: opts.confirmText,
      confirmClass: opts.confirmClass || 'btn-primary',
    });
  }

  // Convenience: dangerous confirm (red button)
  function showDangerConfirm(message, onConfirm, opts) {
    opts = opts || {};
    showConfirm(message, onConfirm, {
      ...opts,
      confirmClass: 'btn-danger',
      confirmText: opts.confirmText || t('common.confirm'),
    });
  }

  return { show, close, showConfirm, showDangerConfirm };
})();
