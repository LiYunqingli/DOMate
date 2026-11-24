// DOMate 后台服务：与本地服务保持 WebSocket 长连接，接收命令并调度执行。
// 职责划分：
//   - tab.* / nav.* / capture.* / script.* 由这里直接用 chrome.* API 完成；
//   - dom.* / act.* / page.info / log.* 转发给内容脚本（engine.js）执行；
//   - 每次执行前后改写目标标签页标题，让用户/操作者看到「读取中/操作中/执行中」。
'use strict';

const EXT_VERSION = '1.0.0';
const DEFAULTS = { host: '127.0.0.1', port: 8765, token: '', clientId: '' };

let socket = null;
let socketUrl = '';
let labels = {};
let clientId = '';
let connecting = false;
let reconnectTimer = null;
let heartbeatTimer = null;
let tabSyncTimer = null;

// 重连节奏：
//   断线后的头 15 秒（大概率是本地服务刚重启），用 250ms 起步的快重试，
//   这样服务一端口就绪就能立刻连上，用户几乎看不到空窗；
//   超过 15 秒还连不上（服务真的关了），才退到秒级，避免一直空转耗电。
const FAST_WINDOW_MS = 15000;
const FAST_BASE_MS = 250;
const FAST_MAX_MS = 800;
const SLOW_BASE_MS = 1000;
const SLOW_MAX_MS = 5000;

let droppedAt = 0;      // 本次断线的时间戳，0 表示没断过
let attempt = 0;        // 连续失败次数，用于计算退避
let lastClose = null;   // 最近一次断开原因，供状态查询排查

// -------------------------------------------------------------------- 小工具
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function mkError(code, message, hint) {
    const err = new Error(message || code);
    err.code = code;
    if (hint) err.hint = hint;
    return err;
}

function normalizeError(err) {
    if (err && err.code) {
        return { code: err.code, message: err.message || String(err), hint: err.hint };
    }
    const text = err && err.message ? err.message : String(err);
    let code = 'E_BROWSER';
    if (/No tab with id/i.test(text)) code = 'E_NO_TAB';
    else if (/Cannot access|Missing host permission|The extensions gallery/i.test(text)) code = 'E_PERM';
    else if (/timeout|timed out/i.test(text)) code = 'E_TIMEOUT';
    return { code, message: text };
}

function browserName() {
    const ua = navigator.userAgent;
    if (/Edg\//.test(ua)) return 'Edge';
    if (/Chrome\//.test(ua)) return 'Chrome';
    if (/Firefox\//.test(ua)) return 'Firefox';
    return 'Unknown';
}

async function getSettings() {
    const stored = await chrome.storage.local.get(DEFAULTS);
    return Object.assign({}, DEFAULTS, stored);
}

// -------------------------------------------------------------------- 连接管理
function send(message) {
    if (socket && socket.readyState === WebSocket.OPEN) {
        try {
            socket.send(JSON.stringify(message));
            return true;
        } catch (e) {
            return false;
        }
    }
    return false;
}

async function connect() {
    if (connecting) return;
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;

    // 先占位再 await，避免并发的 kick() 同时开出两个连接
    connecting = true;
    let settings;
    try {
        settings = await getSettings();
    } catch (e) {
        connecting = false;
        scheduleReconnect();
        return;
    }
    clientId = settings.clientId || ('ext-' + Math.random().toString(36).slice(2, 10));
    if (!settings.clientId) await chrome.storage.local.set({ clientId });

    let query = '?kind=extension&v=' + encodeURIComponent(EXT_VERSION);
    if (settings.token) query += '&token=' + encodeURIComponent(settings.token);
    const url = 'ws://' + settings.host + ':' + settings.port + '/ws' + query;
    socketUrl = url;

    let ws;
    try {
        ws = new WebSocket(url);
    } catch (e) {
        connecting = false;
        scheduleReconnect();
        return;
    }
    socket = ws;

    ws.onopen = async () => {
        connecting = false;
        attempt = 0;
        droppedAt = 0;
        setBadge(true);
        const tabs = await allTabs();
        send({
            type: 'hello',
            clientId: clientId,
            kind: 'extension',
            version: EXT_VERSION,
            browser: browserName(),
            ua: navigator.userAgent,
            tabs: tabs
        });
        startHeartbeat();
    };

    ws.onmessage = (event) => {
        let msg;
        try {
            msg = JSON.parse(event.data);
        } catch (e) {
            return;
        }
        if (msg.type === 'welcome') {
            labels = msg.labels || labels;
            return;
        }
        if (msg.type === 'ping') {
            // 服务端主动保活：必须回一条，双方的流量都能让工作线程免于回收
            send({ type: 'pong', t: msg.t });
            return;
        }
        if (msg.type === 'command') {
            handleCommand(msg);
        }
    };

    ws.onerror = () => {
        // 交给 onclose 统一处理重连
    };

    ws.onclose = (event) => {
        if (socket === ws) socket = null;
        connecting = false;
        setBadge(false);
        stopHeartbeat();
        // 只记录「这次断线」的起点，重复失败时不要刷新它，
        // 否则快重试窗口永远不过期，服务真关了也会一直 250ms 打一次。
        if (!droppedAt) droppedAt = Date.now();
        lastClose = {
            at: Date.now(),
            code: event && event.code,
            reason: (event && event.reason) || '',
            url: socketUrl
        };
        scheduleReconnect();
    };
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    const fast = droppedAt > 0 && (Date.now() - droppedAt) < FAST_WINDOW_MS;
    const base = fast ? FAST_BASE_MS : SLOW_BASE_MS;
    const ceiling = fast ? FAST_MAX_MS : SLOW_MAX_MS;
    const delay = Math.min(Math.round(base * Math.pow(1.6, attempt)), ceiling);
    attempt += 1;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
    }, delay);
}

// 用户一动（切标签、切窗口、页面加载）就顺便补连一次：
// service worker 被回收后这些事件也会把它唤醒，比等 30 秒一次的闹钟快得多。
function kick() {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
    if (connecting) return;
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    attempt = 0;
    if (!droppedAt) droppedAt = Date.now();
    connect();
}

function disconnect() {
    if (socket) {
        try { socket.close(); } catch (e) { /* 忽略 */ }
        socket = null;
    }
}

function startHeartbeat() {
    stopHeartbeat();
    // 15 秒一次心跳，两个作用：
    //   1. 探测服务是否还活着；
    //   2. Chrome 116+ 规定「WS 收发消息」会重置 Service Worker 的 30 秒空闲计时，
    //      所以只要有这条心跳，工作线程就不会被回收，连接也就不会断。
    heartbeatTimer = setInterval(() => {
        if (!send({ type: 'ping', t: Date.now() })) {
            stopHeartbeat();
            scheduleReconnect();
        }
    }, 15000);
}

function stopHeartbeat() {
    if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
    }
}

function setBadge(online) {
    try {
        chrome.action.setBadgeText({ text: online ? 'ON' : '' });
        chrome.action.setBadgeBackgroundColor({ color: online ? '#2e9e5b' : '#c0392b' });
        chrome.action.setTitle({ title: online ? 'DOMate 已连接' : 'DOMate 未连接（点击查看设置）' });
    } catch (e) { /* 忽略 */ }
}

// -------------------------------------------------------------------- 标签页
function tabInfo(tab) {
    return {
        id: tab.id,
        index: tab.index,
        windowId: tab.windowId,
        title: tab.title || '',
        url: tab.url || '',
        active: !!tab.active,
        pinned: !!tab.pinned,
        status: tab.status || 'complete',
        groupId: tab.groupId,
        width: tab.width,
        height: tab.height
    };
}

async function allTabs() {
    const tabs = await chrome.tabs.query({});
    tabs.sort((a, b) => (a.windowId - b.windowId) || (a.index - b.index));
    return tabs.map(tabInfo);
}

function scheduleTabSync() {
    if (tabSyncTimer) clearTimeout(tabSyncTimer);
    tabSyncTimer = setTimeout(async () => {
        tabSyncTimer = null;
        if (!socket || socket.readyState !== WebSocket.OPEN) return;
        const tabs = await allTabs();
        send({ type: 'tabs', tabs: tabs });
    }, 250);
}

['onCreated', 'onRemoved', 'onMoved', 'onActivated', 'onAttached', 'onDetached', 'onReplaced']
    .forEach((name) => {
        try { chrome.tabs[name].addListener(() => scheduleTabSync()); } catch (e) { /* 忽略 */ }
    });

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
    if (changeInfo.url || changeInfo.title || changeInfo.status === 'complete') {
        scheduleTabSync();
    }
});

// -------------------------------------------------------------------- 命令执行
function statusKind(action) {
    if (action.indexOf('dom.') === 0 || action === 'page.info' || action.indexOf('log.') === 0) return 'read';
    if (action.indexOf('act.') === 0 || action.indexOf('nav.') === 0 || action.indexOf('capture.') === 0) return 'act';
    if (action.indexOf('script.') === 0) return 'exec';
    return null;
}

function markTitle(tabId, text) {
    if (!text || tabId == null) return;
    try {
        chrome.tabs.sendMessage(tabId, { op: '__title', mode: 'set', text: text }, () => {
            void chrome.runtime.lastError;  // 页面没有内容脚本时忽略
        });
    } catch (e) { /* 忽略 */ }
}

function clearTitle(tabId) {
    if (tabId == null) return;
    try {
        chrome.tabs.sendMessage(tabId, { op: '__title', mode: 'clear' }, () => {
            void chrome.runtime.lastError;
        });
    } catch (e) { /* 忽略 */ }
}

async function handleCommand(msg) {
    const action = msg.action;
    const tabId = msg.tabId;
    const params = msg.params || {};
    if (msg.labels) labels = msg.labels;

    const kind = statusKind(action);
    if (kind) markTitle(tabId, labels[kind]);

    let response;
    try {
        const data = await runAction(action, tabId, params);
        response = { type: 'result', id: msg.id, ok: true, data: data === undefined ? {} : data };
    } catch (err) {
        response = { type: 'result', id: msg.id, ok: false, error: normalizeError(err) };
    }

    // 让用户看到「完成/出错」再恢复原标题
    if (kind) {
        if (!response.ok && tabId != null) markTitle(tabId, labels.err);
        setTimeout(() => clearTitle(tabId), 260);
    }
    send(response);
}

async function runAction(action, tabId, params) {
    if (action.indexOf('tab.') === 0) return tabAction(action, tabId, params);
    if (action === 'nav.goto') return navigate(tabId, params.url, params.wait !== false);
    if (action === 'nav.back') return stepHistory(tabId, -1 * (params.steps || 1));
    if (action === 'nav.forward') return stepHistory(tabId, params.steps || 1);
    if (action === 'nav.reload') {
        await chrome.tabs.reload(tabId, { bypassCache: !!params.hard });
        await waitComplete(tabId, 20000);
        return pickTabInfo(tabId);
    }
    if (action === 'capture.screenshot') return screenshot(tabId, params);
    if (action === 'script.inject' || action === 'script.eval') return injectScript(tabId, params);
    if (action === 'page.stop') {
        await pageStop(tabId);
        return { stopped: true };
    }
    return runInPage(tabId, action, params);
}

// -------------------------------------------------------------------- 页面侧执行
function sendToTab(tabId, message, timeout) {
    timeout = timeout || 20000;
    return new Promise((resolve, reject) => {
        let settled = false;
        const timer = setTimeout(() => {
            if (settled) return;
            settled = true;
            reject(mkError('E_TIMEOUT', '页面响应超时（' + timeout + 'ms）', 'timeout'));
        }, timeout);

        try {
            chrome.tabs.sendMessage(tabId, message, (resp) => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                const lastError = chrome.runtime.lastError;
                if (lastError) {
                    reject(mkError('E_NO_CONTENT', lastError.message || '页面无响应'));
                    return;
                }
                if (!resp) {
                    reject(mkError('E_NO_CONTENT', '页面无响应'));
                    return;
                }
                if (resp.ok) resolve(resp.data);
                else reject(mkError(
                    (resp.error && resp.error.code) || 'E_SCRIPT',
                    (resp.error && resp.error.message) || '页面执行失败',
                    resp.error && resp.error.hint
                ));
            });
        } catch (err) {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            reject(normalizeError(err));
        }
    });
}

async function ensureContent(tabId) {
    try {
        await sendToTab(tabId, { op: '__ping' }, 1200);
        return;
    } catch (e) {
        // 标签页在插件安装前就打开了，或页面刷新导致内容脚本丢失：手动注入
    }
    try {
        await chrome.scripting.executeScript({
            target: { tabId: tabId, allFrames: false },
            files: ['engine.js', 'content.js']
        });
    } catch (err) {
        throw mkError('E_PERM', '无法在该页面注入脚本（chrome:// 等受限页面）：' + err.message, 'browser');
    }
    await sleep(80);
}

async function runInPage(tabId, action, params) {
    await ensureContent(tabId);
    const timeout = Math.max(20000, ((params && params.timeout) || 0) * 1000 + 2000);
    const data = await sendToTab(tabId, { op: action, params: params || {} }, timeout);
    // 点击/输入可能触发跳转，等页面稳定下来再回包，避免后续命令打到旧页面
    if ((action === 'act.click' || action === 'act.type') && params && params.waitNav) {
        await waitComplete(tabId, 20000);
    }
    return data;
}

async function pageStop(tabId) {
    try {
        await sendToTab(tabId, { op: 'page.stop' }, 3000);
    } catch (e) {
        await chrome.tabs.reload(tabId);
    }
}

// -------------------------------------------------------------------- 脚本注入
async function injectScript(tabId, params) {
    const source = String(params.code || '');
    if (!source.trim()) throw mkError('E_USAGE', '脚本内容为空');
    const world = params.world === 'ISOLATED' ? 'ISOLATED' : 'MAIN';

    const results = await chrome.scripting.executeScript({
        target: { tabId: tabId, allFrames: false },
        world: world,
        args: [source],
        func: function (code) {
            function ser(value) {
                const type = typeof value;
                if (value === null || value === undefined) return { type: 'null', value: null };
                if (type === 'function') return { type: type, value: '[Function]' };
                if (type !== 'object') {
                    if (type === 'bigint' || type === 'symbol') return { type: type, value: String(value) };
                    return { type: type, value: value };
                }
                if (typeof Node !== 'undefined' && value instanceof Node) {
                    return {
                        type: 'node',
                        value: value.nodeType === 1 ? value.outerHTML.slice(0, 2000) : String(value.textContent || '')
                    };
                }
                try {
                    const seen = [];
                    const json = JSON.stringify(value, (k, v) => {
                        if (typeof v === 'object' && v !== null) {
                            if (seen.indexOf(v) >= 0) return '[Circular]';
                            seen.push(v);
                            if (typeof Node !== 'undefined' && v instanceof Node) {
                                return v.nodeType === 1 ? v.outerHTML.slice(0, 500) : String(v.textContent || '');
                            }
                        }
                        if (typeof v === 'function') return '[Function]';
                        return v;
                    });
                    return { type: Array.isArray(value) ? 'array' : 'object', value: JSON.parse(json) };
                } catch (e) {
                    return { type: 'object', value: String(value) };
                }
            }

            try {
                // 间接 eval：在全局作用域执行，可访问页面变量
                const result = (0, eval)(code);
                const packed = ser(result);
                return { ok: true, value: packed.value, type: packed.type };
            } catch (e) {
                return { ok: false, error: { code: 'E_SCRIPT', message: String((e && e.message) || e) } };
            }
        }
    });

    const result = results && results[0] && results[0].result;
    if (!result) throw mkError('E_SCRIPT', '脚本没有返回结果');
    if (!result.ok) throw mkError(result.error.code || 'E_SCRIPT', result.error.message);
    return { value: result.value, type: result.type };
}

// -------------------------------------------------------------------- 标签页动作
async function pickTabInfo(tabId) {
    const tab = await chrome.tabs.get(tabId);
    return { url: tab.url, title: tab.title, status: tab.status, tab: tabInfo(tab) };
}

async function tabAction(action, tabId, params) {
    if (action === 'tab.list') {
        return { tabs: await allTabs() };
    }

    if (action === 'tab.current') {
        const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
        if (!tab) throw mkError('E_NO_TAB', '没有活动标签页');
        return { tab: tabInfo(tab) };
    }

    if (action === 'tab.new') {
        let url = params.url || 'about:blank';
        if (/^[\w.-]+\.[a-z]{2,}/i.test(url)) url = 'https://' + url;
        const createProps = {
            url: url,
            active: params.active !== false,
            pinned: !!params.pinned
        };
        const tab = await chrome.tabs.create(createProps);
        if (params.newWindow) {
            await chrome.windows.create({ tabId: tab.id });
        }
        const info = tabInfo(tab);
        if (params.active !== false) {
            try { await chrome.windows.update(tab.windowId, { focused: true }); } catch (e) { /* 忽略 */ }
        }
        return { tab: info };
    }

    if (action === 'tab.close') {
        let ids = (params.ids || []).filter((n) => typeof n === 'number');
        if (params.others) {
            const tabs = await chrome.tabs.query({});
            const keep = tabId != null ? tabId : (tabs.find((t) => t.active) || {}).id;
            ids = tabs.filter((t) => t.id !== keep).map((t) => t.id);
        } else if (params.right) {
            const target = await chrome.tabs.get(tabId);
            const tabs = await chrome.tabs.query({ windowId: target.windowId });
            ids = tabs.filter((t) => t.index > target.index).map((t) => t.id);
        } else if (!ids.length) {
            if (tabId == null) throw mkError('E_NO_TAB', '没有指定要关闭的标签页');
            ids = [tabId];
        }
        if (ids.length) await chrome.tabs.remove(ids);
        return { closed: ids, count: ids.length };
    }

    if (action === 'tab.activate') {
        const tab = await chrome.tabs.update(tabId, { active: true });
        try {
            await chrome.windows.update(tab.windowId, { focused: true });
        } catch (e) { /* 忽略 */ }
        return { tab: tabInfo(tab) };
    }

    if (action === 'tab.title') {
        const title = String(params.title || '');
        await ensureContent(tabId);
        try {
            await sendToTab(tabId, { op: '__title', mode: 'force', text: title }, 3000);
        } catch (e) { /* 受限页面改不了标题，忽略 */ }
        scheduleTabSync();
        return { title: title, tabId: tabId };
    }

    if (action === 'tab.reload') {
        await chrome.tabs.reload(tabId, { bypassCache: !!params.hard });
        const info = await waitComplete(tabId, 20000);
        return { url: info.url, title: info.title, status: info.status };
    }

    if (action === 'tab.stop') {
        await pageStop(tabId);
        return { stopped: true };
    }

    throw mkError('E_UNSUPPORTED', '不支持的标签页动作: ' + action);
}

// -------------------------------------------------------------------- 导航
async function waitComplete(tabId, timeout) {
    const start = Date.now();
    await sleep(120);  // 给 chrome 一点时间把 status 切成 loading
    while (Date.now() - start < timeout) {
        let tab = null;
        try {
            tab = await chrome.tabs.get(tabId);
        } catch (e) {
            throw mkError('E_NO_TAB', '标签页已关闭');
        }
        if (tab.status === 'complete') return tab;
        await sleep(160);
    }
    return chrome.tabs.get(tabId);
}

async function navigate(tabId, url, wait) {
    if (!url) throw mkError('E_USAGE', '缺少 url');
    if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) url = 'https://' + url;
    await chrome.tabs.update(tabId, { url: url });
    const tab = wait === false ? await chrome.tabs.get(tabId) : await waitComplete(tabId, 25000);
    return { url: tab.url, title: tab.title || '', status: tab.status };
}

async function stepHistory(tabId, steps) {
    const forward = steps > 0;
    const count = Math.abs(steps);
    for (let i = 0; i < count; i++) {
        try {
            if (forward) await chrome.tabs.goForward(tabId);
            else await chrome.tabs.goBack(tabId);
        } catch (e) {
            break;
        }
        await sleep(200);
    }
    const tab = await waitComplete(tabId, 15000);
    return { url: tab.url, title: tab.title || '', status: tab.status };
}

// -------------------------------------------------------------------- 截图
async function screenshot(tabId, params) {
    const tab = await chrome.tabs.get(tabId);
    let restoreId = null;
    if (!tab.active) {
        const [active] = await chrome.tabs.query({ windowId: tab.windowId, active: true });
        restoreId = active ? active.id : null;
        await chrome.tabs.update(tabId, { active: true });
        await sleep(300);
    }
    const format = params.format === 'jpeg' ? 'jpeg' : 'png';
    const options = { format: format };
    if (format === 'jpeg') options.quality = Math.min(100, Math.max(10, params.quality || 85));
    const image = await chrome.tabs.captureVisibleTab(tab.windowId, options);
    if (restoreId != null) {
        try { await chrome.tabs.update(restoreId, { active: true }); } catch (e) { /* 忽略 */ }
    }
    return { image: image, viewport: { w: tab.width, h: tab.height }, format: format };
}

// -------------------------------------------------------------------- 生命周期
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.type !== 'domate-control') return false;
    if (msg.action === 'reconnect') {
        disconnect();
        droppedAt = Date.now();
        attempt = 0;
        setTimeout(kick, 50);
        sendResponse({ ok: true });
        return false;
    }
    if (msg.action === 'status') {
        sendResponse({
            ok: true,
            data: {
                connected: !!(socket && socket.readyState === WebSocket.OPEN),
                url: socketUrl,
                labels: labels,
                clientId: clientId,
                version: EXT_VERSION,
                attempts: attempt,
                lastClose: lastClose
            }
        });
        return false;
    }
    return false;
});

chrome.runtime.onStartup.addListener(() => kick());
chrome.runtime.onInstalled.addListener(() => kick());
chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    if (changes.host || changes.port || changes.token) {
        disconnect();
        droppedAt = Date.now();
        attempt = 0;
        setTimeout(kick, 50);
    }
});

// 事件驱动补连：普通用户操作就能把断开的连接立刻接回来
['onActivated', 'onCreated', 'onRemoved', 'onMoved'].forEach((name) => {
    try { chrome.tabs[name].addListener(() => kick()); } catch (e) { /* 忽略 */ }
});
try {
    chrome.windows.onFocusChanged.addListener(() => kick());
} catch (e) { /* 忽略 */ }
try {
    if (chrome.idle && chrome.idle.onStateChanged) {
        chrome.idle.onStateChanged.addListener((state) => {
            if (state === 'active') kick();
        });
    }
} catch (e) { /* 没申请 idle 权限时忽略 */ }

// MV3 的 service worker 会被回收，用定时任务兜底：即使没有用户操作，
// 最多 30 秒（Chrome 120+；更低版本为 1 分钟）也会被唤醒补连一次。
try {
    chrome.alarms.create('domate-keepalive', { periodInMinutes: 0.5 });
    chrome.alarms.onAlarm.addListener((alarm) => {
        if (alarm.name === 'domate-keepalive') kick();
    });
} catch (e) { /* 忽略 */ }

kick();
