// i18n – Lightweight translation system
// Usage: t('config.save') or t('users.delete_confirm', {name: 'anna'})
// HTML:  <span data-i18n="chat.send">Senden</span>

const I18n = (() => {
  let _translations = {};
  let _lang = localStorage.getItem('haana_lang') || 'de';
  let _ready = false;
  const _readyCallbacks = [];

  function _getNestedValue(obj, path) {
    return path.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : null), obj);
  }

  async function load(lang) {
    _lang = lang || _lang;
    try {
      const r = await fetch(`/static/i18n/${_lang}.json`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      _translations = await r.json();
      localStorage.setItem('haana_lang', _lang);
      _ready = true;
      _readyCallbacks.forEach(fn => fn());
      _readyCallbacks.length = 0;
      translateDOM();
      return true;
    } catch (e) {
      console.error(`[i18n] Failed to load ${_lang}.json:`, e);
      // Fallback to German if non-default language fails
      if (_lang !== 'de') {
        _lang = 'de';
        return load('de');
      }
      return false;
    }
  }

  function t(key, params) {
    const val = _getNestedValue(_translations, key);
    if (val === null) {
      console.warn(`[i18n] Missing key: ${key}`);
      return key;
    }
    if (!params) return val;
    return val.replace(/\{(\w+)\}/g, (_, k) => (params[k] !== undefined ? params[k] : `{${k}}`));
  }

  function translateDOM(root) {
    const container = root || document;
    container.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      const translated = t(key);
      if (translated !== key) {
        // Support attribute translations: data-i18n-attr="placeholder"
        const attr = el.getAttribute('data-i18n-attr');
        if (attr) {
          el.setAttribute(attr, translated);
        } else {
          el.textContent = translated;
        }
      }
    });
  }

  function getLang() { return _lang; }

  function onReady(fn) {
    if (_ready) fn();
    else _readyCallbacks.push(fn);
  }

  return { load, t, translateDOM, getLang, onReady };
})();

// Global shortcut
function t(key, params) { return I18n.t(key, params); }
