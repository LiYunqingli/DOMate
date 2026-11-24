// 设置页：读写 host/port/token，保存后通知后台重连
'use strict';

const $ = (id) => document.getElementById(id);
const DEFAULTS = { host: '127.0.0.1', port: 8765, token: '' };

async function load() {
    const data = await chrome.storage.local.get(DEFAULTS);
    $('host').value = data.host;
    $('port').value = data.port;
    $('token').value = data.token || '';
}

function flash(text, isError) {
    const msg = $('msg');
    msg.textContent = text;
    msg.className = isError ? 'err' : '';
    setTimeout(() => { msg.textContent = ''; }, 2600);
}

$('save').addEventListener('click', async () => {
    const host = ($('host').value || '127.0.0.1').trim();
    const port = parseInt($('port').value, 10);
    if (!Number.isFinite(port) || port <= 0 || port > 65535) {
        flash('端口不合法', true);
        return;
    }
    await chrome.storage.local.set({ host: host, port: port, token: $('token').value.trim() });
    chrome.runtime.sendMessage({ type: 'domate-control', action: 'reconnect' }, () => {
        void chrome.runtime.lastError;
        flash('已保存，正在重连…', false);
    });
});

$('reset').addEventListener('click', async () => {
    await chrome.storage.local.set(DEFAULTS);
    await load();
    chrome.runtime.sendMessage({ type: 'domate-control', action: 'reconnect' }, () => {
        void chrome.runtime.lastError;
        flash('已恢复默认', false);
    });
});

load();
