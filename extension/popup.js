// 弹窗：展示连接状态，提供重连与设置入口
'use strict';

const $ = (id) => document.getElementById(id);

async function refresh() {
    const settings = await chrome.storage.local.get({ host: '127.0.0.1', port: 8765, clientId: '' });
    $('server').textContent = settings.host + ':' + settings.port;
    $('cid').textContent = settings.clientId || '-';

    chrome.runtime.sendMessage({ type: 'domate-control', action: 'status' }, (resp) => {
        void chrome.runtime.lastError;
        const data = (resp && resp.data) || { connected: false };
        $('dot').className = 'dot' + (data.connected ? ' on' : '');
        $('state').textContent = data.connected ? '已连接' : '未连接';
        $('ver').textContent = data.version || '-';
        $('hint').textContent = data.connected
            ? '终端里执行：python main.py tab list'
            : '先启动本地服务：python main.py serve，然后点重新连接';
    });
}

$('reconnect').addEventListener('click', () => {
    $('state').textContent = '连接中…';
    chrome.runtime.sendMessage({ type: 'domate-control', action: 'reconnect' }, () => {
        void chrome.runtime.lastError;
        setTimeout(refresh, 600);
    });
});

$('options').addEventListener('click', () => chrome.runtime.openOptionsPage());

refresh();
