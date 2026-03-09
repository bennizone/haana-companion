// HAANA Terminal - Claude Code Integration
'use strict';

let _term = null;
let _ws = null;
let _fitAddon = null;
let _termResizeObserver = null;
let _termConnected = false;
let _termProviderId = '';

function initTerminal() {
    if (_term) return; // Nur einmal initialisieren

    // xterm.js Terminal erstellen
    _term = new Terminal({
        theme: { background: '#1e1e1e', foreground: '#d4d4d4', cursor: '#ffffff' },
        fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
        fontSize: 14,
        lineHeight: 1.2,
        cursorBlink: true,
        scrollback: 2000,
        allowTransparency: false,
    });

    _fitAddon = new FitAddon.FitAddon();
    _term.loadAddon(_fitAddon);

    const container = document.getElementById('terminal-xterm');
    _term.open(container);
    _fitAddon.fit();

    // Resize Observer
    _termResizeObserver = new ResizeObserver(() => {
        if (_fitAddon) _fitAddon.fit();
        _termSendResize();
    });
    _termResizeObserver.observe(container);

    // User-Eingabe -> WebSocket
    _term.onData(data => {
        if (_ws && _ws.readyState === WebSocket.OPEN) {
            _ws.send(data);
        }
    });

    // Provider laden und Status pruefen
    _termLoadProviders();
    _termLoadStatus();

    // Willkommensnachricht
    _term.writeln('\x1b[1;36mHAANA Development Terminal\x1b[0m');
    _term.writeln('\x1b[90mProvider waehlen und "Verbinden" klicken um Claude Code zu starten.\x1b[0m');
    _term.writeln('');
}

function _termLoadProviders() {
    fetch('/api/config')
        .then(r => r.json())
        .then(cfg => {
            const sel = document.getElementById('term-provider-select');
            if (!sel) return;
            sel.innerHTML = '<option value="">' + (I18n.t('terminal.provider_none') || 'Kein Provider') + '</option>';
            (cfg.providers || [])
                .filter(p => p.type === 'anthropic')
                .forEach(p => {
                    const opt = document.createElement('option');
                    opt.value = p.id || p.name;
                    opt.textContent = p.name + (p.auth_method === 'oauth' ? ' (OAuth)' : ' (API Key)');
                    sel.appendChild(opt);
                });
        })
        .catch(() => {});
}

function _termLoadStatus() {
    fetch('/api/terminal/status')
        .then(r => r.json())
        .then(s => {
            const dot = document.getElementById('term-status-dot');
            const txt = document.getElementById('term-status-text');
            if (s.session_active) {
                if (dot) dot.className = 'terminal-status-dot connected';
                if (txt) txt.textContent = I18n.t('terminal.session_active') || 'Session aktiv';
            } else {
                if (dot) dot.className = 'terminal-status-dot disconnected';
                if (txt) txt.textContent = I18n.t('terminal.no_session') || 'Keine Session';
            }
        })
        .catch(() => {});
}

function termConnect() {
    if (_termConnected) return;

    const sel = document.getElementById('term-provider-select');
    _termProviderId = sel ? sel.value : '';

    // Provider setzen falls gewaehlt
    const connect = () => {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = proto + '//' + location.host + '/ws/terminal';

        _ws = new WebSocket(url);
        _ws.binaryType = 'arraybuffer';

        _ws.onopen = () => {
            _termConnected = true;
            _termUpdateConnBtn(true);
            _termSendResize();
        };

        _ws.onmessage = e => {
            const data = e.data instanceof ArrayBuffer
                ? new Uint8Array(e.data)
                : e.data;
            _term.write(data);
        };

        _ws.onclose = () => {
            _termConnected = false;
            _termUpdateConnBtn(false);
            _term.writeln('\r\n\x1b[33m[Verbindung getrennt \u2013 Session laeuft in tmux weiter]\x1b[0m');
            _termLoadStatus();
        };

        _ws.onerror = () => {
            _term.writeln('\r\n\x1b[31m[WebSocket Fehler]\x1b[0m');
        };
    };

    if (_termProviderId) {
        fetch('/api/terminal/set-provider', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({provider_id: _termProviderId})
        }).then(() => connect()).catch(() => connect());
    } else {
        connect();
    }
}

function termDisconnect() {
    if (_ws) { _ws.close(); _ws = null; }
    _termConnected = false;
    _termUpdateConnBtn(false);
}

function _termUpdateConnBtn(connected) {
    const btn = document.getElementById('term-conn-btn');
    if (!btn) return;
    if (connected) {
        btn.textContent = I18n.t('terminal.disconnect_btn') || 'Trennen';
        btn.onclick = termDisconnect;
        btn.className = 'btn btn-secondary btn-sm';
    } else {
        btn.textContent = I18n.t('terminal.connect_btn') || 'Verbinden';
        btn.onclick = termConnect;
        btn.className = 'btn btn-primary btn-sm';
    }
}

function _termSendResize() {
    if (!_ws || _ws.readyState !== WebSocket.OPEN || !_term) return;
    const msg = JSON.stringify({type: 'resize', cols: _term.cols, rows: _term.rows});
    _ws.send(msg);
}

function termToggleFullscreen() {
    document.body.classList.toggle('terminal-fullscreen');
    if (_fitAddon) setTimeout(() => _fitAddon.fit(), 50);
}
