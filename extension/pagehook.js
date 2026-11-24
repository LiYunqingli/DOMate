// 主世界钩子：拦截页面 console 与未捕获异常，通过 CustomEvent 转发给隔离世界。
// 必须在 document_start 注入，否则会漏掉页面首屏的日志。
(function () {
    'use strict';

    if (window.__DOMATE_HOOK__) return;
    window.__DOMATE_HOOK__ = true;

    var MAX_LEN = 1500;

    function stringify(value) {
        if (value === undefined) return 'undefined';
        if (value === null) return 'null';
        if (typeof value === 'string') return value.slice(0, MAX_LEN);
        if (typeof value === 'function') return '[Function ' + (value.name || 'anonymous') + ']';
        if (value instanceof Error) return value.stack ? String(value.stack).slice(0, MAX_LEN) : value.message;
        if (value instanceof Element) return value.outerHTML.slice(0, MAX_LEN);
        try {
            var text = JSON.stringify(value);
            return text === undefined ? String(value) : text.slice(0, MAX_LEN);
        } catch (e) {
            return String(value).slice(0, MAX_LEN);
        }
    }

    function emit(level, args) {
        var text = Array.prototype.map.call(args, stringify).join(' ');
        try {
            window.dispatchEvent(new CustomEvent('__domate_page_event', {
                detail: JSON.stringify({ kind: 'console', level: level, text: text, ts: Date.now() })
            }));
        } catch (e) { /* 页面可能禁用了事件，忽略 */ }
    }

    ['log', 'info', 'warn', 'error', 'debug'].forEach(function (level) {
        var original = console[level];
        console[level] = function () {
            try { emit(level, arguments); } catch (e) { /* 忽略 */ }
            if (original) return original.apply(console, arguments);
        };
    });

    window.addEventListener('error', function (event) {
        emit('error', [event.message + ' @ ' + (event.filename || '') + ':' + (event.lineno || 0)]);
    });

    window.addEventListener('unhandledrejection', function (event) {
        var reason = event.reason;
        emit('error', ['UnhandledRejection: ' + stringify(reason && reason.message ? reason.message : reason)]);
    });
})();
