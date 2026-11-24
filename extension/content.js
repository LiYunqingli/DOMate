// DOMate 内容脚本：消息桥 + 标题状态 + 控制台/事件采集。
// 运行在隔离世界（isolated world），可以完整访问 DOM，但拿不到页面 JS 变量；
// 需要页面变量时由 background 用 chrome.scripting 在主世界执行。
(function () {
    'use strict';

    // 重复注入时只保留一份监听，避免同一命令被响应两次
    if (window.__DOMATE_BRIDGE__) return;
    window.__DOMATE_BRIDGE__ = true;

    var MAX_LOGS = 300;
    var MAX_EVENTS = 200;
    var FLASH_MS = 500;

    window.__DOMATE_LOGS = window.__DOMATE_LOGS || [];
    window.__DOMATE_EVENTS = window.__DOMATE_EVENTS || [];

    // ------------------------------------------------------------------ 标题状态
    var originalTitle = null;
    var restoreTimer = null;

    function setStatusTitle(text) {
        if (originalTitle === null) originalTitle = document.title;
        if (restoreTimer) { clearTimeout(restoreTimer); restoreTimer = null; }
        if (text) {
            document.title = '[' + text + '] ' + originalTitle;
        } else {
            document.title = originalTitle;
        }
    }

    function clearStatusTitleSoon() {
        if (restoreTimer) clearTimeout(restoreTimer);
        restoreTimer = setTimeout(function () {
            setStatusTitle(null);
            originalTitle = null;
        }, FLASH_MS);
    }

    function forceTitle(text) {
        originalTitle = null;
        if (restoreTimer) { clearTimeout(restoreTimer); restoreTimer = null; }
        document.title = text;
    }

    // ------------------------------------------------------------------ 页面日志采集
    // 主世界钩子（pagehook.js）通过 CustomEvent 把日志转发到隔离世界
    window.addEventListener('__domate_page_event', function (event) {
        var payload;
        try {
            payload = JSON.parse(event.detail);
        } catch (e) {
            return;
        }
        if (payload.kind === 'console') {
            window.__DOMATE_LOGS.push({
                level: payload.level || 'log',
                text: String(payload.text || '').slice(0, 2000),
                t: timeOf(payload.ts)
            });
            if (window.__DOMATE_LOGS.length > MAX_LOGS) window.__DOMATE_LOGS.shift();
        }
    });

    function timeOf(ts) {
        var d = ts ? new Date(ts) : new Date();
        return [d.getHours(), d.getMinutes(), d.getSeconds()].map(function (n) {
            return n < 10 ? '0' + n : String(n);
        }).join(':');
    }

    // ------------------------------------------------------------------ 用户事件采集
    function targetLabel(el) {
        if (!el || !el.tagName) return 'document';
        var out = el.tagName.toLowerCase();
        if (el.id) out += '#' + el.id;
        var name = el.getAttribute && (el.getAttribute('name') || el.getAttribute('aria-label'));
        if (name) out += '[' + name + ']';
        var text = (el.innerText || '').replace(/\s+/g, ' ').trim();
        if (text) out += ' "' + text.slice(0, 30) + '"';
        return out;
    }

    function record(type, el, value) {
        window.__DOMATE_EVENTS.push({
            t: timeOf(),
            type: type,
            target: targetLabel(el),
            value: value == null ? '' : String(value).slice(0, 120)
        });
        if (window.__DOMATE_EVENTS.length > MAX_EVENTS) window.__DOMATE_EVENTS.shift();
    }

    ['click', 'dblclick', 'change', 'submit'].forEach(function (type) {
        window.addEventListener(type, function (event) {
            var el = event.target;
            record(type, el, el && el.value);
        }, true);
    });

    window.addEventListener('keydown', function (event) {
        // 只记录有内容的按键，避免方向键/修饰键刷屏
        if (event.key && event.key.length === 1) {
            record('keydown', event.target, event.key);
        } else if (event.key === 'Enter' || event.key === 'Escape') {
            record('keydown', event.target, event.key);
        }
    }, true);

    // ------------------------------------------------------------------ 消息处理
    function respondAsync(promise, sendResponse) {
        promise.then(function (data) {
            sendResponse({ ok: true, data: data });
        }).catch(function (err) {
            sendResponse({
                ok: false,
                error: {
                    code: err && err.code ? err.code : 'E_SCRIPT',
                    message: err && err.message ? err.message : String(err),
                    hint: err && err.hint ? err.hint : undefined
                }
            });
        });
        return true;  // 保持消息通道开启
    }

    chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
        if (!msg || !msg.op) return false;

        if (msg.op === '__title') {
            if (msg.mode === 'clear') clearStatusTitleSoon();
            else if (msg.mode === 'force') forceTitle(msg.text || '');
            else setStatusTitle(msg.text || '');
            sendResponse({ ok: true, data: { title: document.title } });
            return false;
        }

        if (msg.op === '__ping') {
            sendResponse({ ok: true, data: { url: location.href, title: document.title, engine: !!(window.DOMateEngine) } });
            return false;
        }

        if (msg.op === '__buffers') {
            sendResponse({
                ok: true,
                data: {
                    logs: window.__DOMATE_LOGS.length,
                    events: window.__DOMATE_EVENTS.length
                }
            });
            return false;
        }

        if (!window.DOMateEngine) {
            sendResponse({ ok: false, error: { code: 'E_ENGINE', message: '页面引擎未加载' } });
            return false;
        }

        return respondAsync(window.DOMateEngine.run(msg.op, msg.params || {}), sendResponse);
    });
})();
