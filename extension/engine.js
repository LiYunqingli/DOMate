// DOMate 页面引擎：在内容脚本里执行所有 DOM 读取与交互动作。
// 设计原则：
//   1. 只做「被动查询 + 合成事件」，不改变页面结构（除了给元素打索引标记）；
//   2. 所有返回数据都必须可 JSON 序列化，拿不到的值转成字符串；
//   3. 出错统一抛 {code, message} ，由内容脚本转成协议错误码回传给 CLI。
(function (root) {
    'use strict';

    var VERSION = '1.0.0';
    var IDX_ATTR = 'data-domate-idx';
    var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, LINK: 1, META: 1, TEMPLATE: 1, HEAD: 1 };
    var INTERACTIVE = [
        'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea',
        '[role=button]', '[role=link]', '[role=tab]', '[role=checkbox]', '[role=radio]',
        '[role=menuitem]', '[role=option]', '[onclick]', '[tabindex]:not([tabindex="-1"])',
        '[contenteditable=true]', 'summary', 'label'
    ].join(',');

    // ------------------------------------------------------------------ 基础工具
    function fail(code, message, hint) {
        var err = new Error(message || code);
        err.code = code;
        if (hint) err.hint = hint;
        throw err;
    }

    function sleep(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    function clip(text, limit) {
        text = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
        return text.length > limit ? text.slice(0, limit - 1) + '…' : text;
    }

    function norm(text) {
        return String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
    }

    function isVisible(el) {
        if (!el || !el.getBoundingClientRect) return false;
        var rect = el.getBoundingClientRect();
        if (rect.width <= 0 && rect.height <= 0) return false;
        var style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
        if (parseFloat(style.opacity) === 0) return false;
        if (style.pointerEvents === 'none') return false;
        return true;
    }

    function rectOf(el) {
        var r = el.getBoundingClientRect();
        return {
            x: Math.round(r.left + (window.scrollX || 0)),
            y: Math.round(r.top + (window.scrollY || 0)),
            w: Math.round(r.width),
            h: Math.round(r.height)
        };
    }

    function describe(el) {
        if (!el) return '';
        var text = norm(el.innerText || el.value || el.getAttribute('aria-label') || '');
        return '<' + el.tagName.toLowerCase() + '> ' + clip(text, 40);
    }

    // 把任意 JS 值转成可传回终端的结构
    function serialize(value) {
        var type = typeof value;
        if (value === null) return { type: 'null', value: null };
        if (type === 'undefined') return { type: 'undefined', value: null };
        if (type === 'function') return { type: 'function', value: '[Function ' + (value.name || 'anonymous') + ']' };
        if (type === 'symbol' || type === 'bigint') return { type: type, value: String(value) };
        if (type !== 'object') return { type: type, value: value };
        if (value instanceof Node) {
            return { type: 'node', value: value.nodeType === 1 ? value.outerHTML.slice(0, 2000) : String(value.textContent || '') };
        }
        if (value instanceof Window || value === window) return { type: 'window', value: '[Window]' };
        try {
            var seen = [];
            var plain = JSON.stringify(value, function (key, val) {
                if (typeof val === 'object' && val !== null) {
                    if (seen.indexOf(val) >= 0) return '[Circular]';
                    if (seen.length > 200) return '[TooLarge]';
                    seen.push(val);
                    if (val instanceof Node) return val.nodeType === 1 ? val.outerHTML.slice(0, 500) : String(val.textContent || '');
                }
                if (typeof val === 'function') return '[Function]';
                return val;
            });
            return { type: Array.isArray(value) ? 'array' : 'object', value: JSON.parse(plain) };
        } catch (e) {
            return { type: 'object', value: String(value) };
        }
    }

    // ------------------------------------------------------------------ 选择器
    function queryAll(selector) {
        if (!selector || !selector.value) return [document.documentElement];
        var by = selector.by || 'css';
        if (by === 'css') {
            try {
                return Array.prototype.slice.call(document.querySelectorAll(selector.value));
            } catch (e) {
                fail('E_SELECTOR', 'CSS 选择器无效: ' + selector.value, 'selector');
            }
        }
        if (by === 'xpath') {
            var snapshot = document.evaluate(selector.value, document, null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            var out = [];
            for (var i = 0; i < snapshot.snapshotLength; i++) out.push(snapshot.snapshotItem(i));
            return out;
        }
        if (by === 'text') {
            return queryByText(selector.value);
        }
        fail('E_SELECTOR', '不支持的选择器类型: ' + by);
    }

    function queryByText(text) {
        var needle = norm(text).toLowerCase();
        var nodes = document.querySelectorAll(INTERACTIVE + ',p,span,div,li,td,th,h1,h2,h3,h4,h5,h6,strong,em');
        var exact = [];
        var loose = [];
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            var content = norm(el.innerText || el.textContent || '').toLowerCase();
            if (!content) continue;
            if (content === needle) exact.push(el);
            else if (content.indexOf(needle) >= 0 && content.length <= needle.length * 6) loose.push(el);
        }
        var list = exact.length ? exact : loose;
        // 文本命中的元素常常父子重复，优先返回最内层（没有同样命中的子元素）
        return list.filter(function (el) {
            for (var k = 0; k < list.length; k++) {
                if (list[k] !== el && el.contains(list[k])) return false;
            }
            return true;
        });
    }

    function pick(selector, options) {
        options = options || {};
        var list = queryAll(selector);
        if (!list.length) {
            fail('E_ELEMENT', '没有匹配的元素: ' + (selector && selector.value), options.hint || 'selector');
        }
        var index = selector && selector.index ? selector.index - 1 : 0;
        var el = list[index];
        if (!el) {
            fail('E_ELEMENT', '只有 ' + list.length + ' 个匹配，取不到第 ' + (index + 1) + ' 个', 'selector');
        }
        return el;
    }

    // ------------------------------------------------------------------ DOM 读取
    function domTree(params) {
        var limit = params.maxNodes || 600;
        var maxDepth = params.depth || 3;
        var rootEl = params.selector ? pick(params.selector) : (document.body || document.documentElement);
        var lines = [];
        var count = 0;
        var truncated = false;

        function walk(el, depth) {
            if (count >= limit) { truncated = true; return; }
            var name = el.tagName.toLowerCase();
            if (SKIP_TAGS[el.tagName]) return;
            var line = labelOf(el);
            var text = '';
            for (var i = 0; i < el.childNodes.length; i++) {
                var node = el.childNodes[i];
                if (node.nodeType === 3) {
                    var t = norm(node.nodeValue);
                    if (t) { text = t; break; }
                }
            }
            if (text) line += ' "' + clip(text, 60) + '"';
            lines.push(new Array(depth + 1).join('  ') + line);
            count++;
            if (depth >= maxDepth) return;
            for (var j = 0; j < el.children.length; j++) walk(el.children[j], depth + 1);
        }

        walk(rootEl, 0);
        return { text: lines.join('\n'), count: count, truncated: truncated, root: rootEl.tagName.toLowerCase() };
    }

    function labelOf(el) {
        var out = el.tagName.toLowerCase();
        if (el.id) out += '#' + el.id;
        var cls = norm(el.getAttribute('class') || '').split(' ').filter(Boolean).slice(0, 3);
        if (cls.length) out += '.' + cls.join('.');
        var attrs = ['type', 'name', 'role', 'href', 'placeholder', 'aria-label', 'title', 'alt'];
        for (var i = 0; i < attrs.length; i++) {
            var v = el.getAttribute(attrs[i]);
            if (v) out += '[' + attrs[i] + '=' + clip(v, 30) + ']';
        }
        return out;
    }

    function domOutline(params) {
        var all = !!params.all;
        var limit = params.max || 300;
        var candidates = Array.prototype.slice.call(document.querySelectorAll(INTERACTIVE));
        var items = [];
        var idx = 0;
        for (var i = 0; i < candidates.length && items.length < limit; i++) {
            var el = candidates[i];
            if (SKIP_TAGS[el.tagName]) continue;
            var visible = isVisible(el);
            if (!visible && !all) continue;
            // 只保留最外层容器与最内层控件，避免 <a><span> 重复两次
            if (items.some(function (item) { return el.contains(item.el) && item.el !== el; })) continue;

            idx += 1;
            el.setAttribute(IDX_ATTR, String(idx));
            var rect = rectOf(el);
            items.push({
                el: el,
                idx: idx,
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type') || (el.tagName === 'A' ? 'link' : ''),
                text: clip(el.innerText || el.textContent || '', 60),
                name: el.getAttribute('name') || '',
                placeholder: el.getAttribute('placeholder') || '',
                aria: el.getAttribute('aria-label') || '',
                value: el.value != null && el.tagName !== 'BUTTON' ? clip(String(el.value), 40) : '',
                href: clip(el.getAttribute('href') || '', 80),
                disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                checked: !!el.checked,
                visible: visible,
                rect: rect
            });
        }
        return {
            items: items.map(function (item) {
                var copy = {};
                Object.keys(item).forEach(function (k) { if (k !== 'el') copy[k] = item[k]; });
                return copy;
            }),
            count: items.length
        };
    }

    function domHtml(params) {
        var el = params.selector ? pick(params.selector) : document.documentElement;
        var html = el.outerHTML || '';
        return { html: html, length: html.length, tag: el.tagName.toLowerCase() };
    }

    function domText(params) {
        var el = params.selector ? pick(params.selector) : (document.body || document.documentElement);
        var text = (el.innerText != null ? el.innerText : el.textContent) || '';
        text = text.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
        return { text: text, length: text.length };
    }

    function domAttr(params) {
        var el = params.selector ? pick(params.selector) : document.documentElement;
        var name = params.name;
        if (!name) fail('E_USAGE', '缺少属性名');
        var value = el.getAttribute(name);
        return { name: name, value: value, exists: value !== null, tag: el.tagName.toLowerCase() };
    }

    function domFind(params) {
        var list = queryAll(params.selector);
        var limit = params.limit || 30;
        var items = list.slice(0, limit).map(function (el, i) {
            return {
                n: i + 1,
                tag: el.tagName.toLowerCase(),
                text: clip(el.innerText || el.textContent || el.value || '', 60),
                id: el.id || '',
                classes: (el.getAttribute('class') || '').split(/\s+/).filter(Boolean).slice(0, 4),
                rect: rectOf(el),
                visible: isVisible(el)
            };
        });
        return { items: items, count: list.length, truncated: list.length > limit };
    }

    function domCount(params) {
        return { count: queryAll(params.selector).length };
    }

    function domInfo(params) {
        var el = params.selector ? pick(params.selector) : document.documentElement;
        var style = window.getComputedStyle(el);
        var attrs = {};
        for (var i = 0; i < el.attributes.length; i++) {
            attrs[el.attributes[i].name] = clip(el.attributes[i].value, 200);
        }
        return {
            tag: el.tagName.toLowerCase(),
            id: el.id || '',
            classes: (el.getAttribute('class') || '').split(/\s+/).filter(Boolean),
            rect: rectOf(el),
            visible: isVisible(el),
            disabled: !!el.disabled,
            checked: !!el.checked,
            value: el.value != null ? String(el.value) : '',
            text: clip(el.innerText || el.textContent || '', 300),
            href: el.href || '',
            src: el.src || '',
            styles: {
                display: style.display,
                position: style.position,
                visibility: style.visibility,
                opacity: style.opacity,
                zIndex: style.zIndex,
                fontSize: style.fontSize,
                color: style.color
            },
            attrs: attrs
        };
    }

    async function domWait(params) {
        var start = Date.now();
        var timeout = (params.timeout || 8) * 1000;
        var interval = params.interval || 150;
        var matcher = buildMatcher(params);
        while (true) {
            var hit = matcher();
            if (params.gone ? !hit : hit) {
                return { found: true, elapsed: Date.now() - start };
            }
            if (Date.now() - start >= timeout) {
                return { found: false, elapsed: Date.now() - start };
            }
            await sleep(interval);
        }
    }

    function buildMatcher(params) {
        if (params.selector) {
            return function () { return queryAll(params.selector).length > 0; };
        }
        if (params.text) {
            var needle = norm(params.text);
            return function () {
                return norm(document.body ? document.body.innerText : '').indexOf(needle) >= 0;
            };
        }
        fail('E_USAGE', 'wait 需要选择器或文本');
    }

    // ------------------------------------------------------------------ 交互
    function scrollIntoViewIfNeeded(el) {
        try {
            el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
        } catch (e) {
            try {
                el.scrollIntoView();
            } catch (e2) {
                // 某些环境（如 jsdom/无布局）没有实现 scrollIntoView，忽略即可
            }
        }
    }

    function dispatchPointer(el, rect, button) {
        var x = rect.left + rect.width / 2;
        var y = rect.top + rect.height / 2;
        var init = {
            bubbles: true, cancelable: true, composed: true, view: window,
            clientX: x, clientY: y, button: button || 0, detail: 1
        };
        var types = ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup'];
        for (var i = 0; i < types.length; i++) {
            var type = types[i];
            var ev;
            try {
                if (type.indexOf('pointer') === 0 && window.PointerEvent) {
                    ev = new PointerEvent(type, Object.assign({ pointerId: 1, pointerType: 'mouse', isPrimary: true }, init));
                } else {
                    ev = new MouseEvent(type, init);
                }
                el.dispatchEvent(ev);
            } catch (e) { /* 老浏览器缺 PointerEvent，忽略 */ }
        }
    }

    async function actClick(params) {
        var el = pick(params.selector);
        if (!params.force) {
            if (!isVisible(el)) fail('E_ELEMENT', '元素不可见，无法点击（可用 --force 强制）', 'selector');
        }
        scrollIntoViewIfNeeded(el);
        await sleep(30);

        var rect = el.getBoundingClientRect();
        var before = location.href;
        if (params.right) {
            dispatchPointer(el, rect, 2);
            el.dispatchEvent(new MouseEvent('contextmenu', {
                bubbles: true, cancelable: true, view: window,
                clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2, button: 2
            }));
        } else {
            dispatchPointer(el, rect, 0);
            var clickInit = {
                bubbles: true, cancelable: true, composed: true, view: window,
                clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2,
                button: 0, detail: 1
            };
            el.dispatchEvent(new MouseEvent('click', clickInit));
            if (params.double) {
                el.dispatchEvent(new MouseEvent('click', clickInit));
                el.dispatchEvent(new MouseEvent('dblclick', clickInit));
            }
        }
        await sleep(60);
        return {
            tag: el.tagName.toLowerCase(),
            text: clip(el.innerText || el.value || '', 40),
            navigated: location.href !== before,
            url: location.href
        };
    }

    function nativeSet(el, value) {
        // 用原型上的 setter，绕过 React/Vue 对 value 的劫持
        var proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
            : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
                : HTMLInputElement.prototype;
        var desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) desc.set.call(el, value);
        else el.value = value;
    }

    function fireInput(el, data) {
        el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: data == null ? null : String(data), inputType: 'insertText' }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }

    async function actType(params) {
        var el = pick(params.selector);
        var text = params.text == null ? '' : String(params.text);
        var editable = el.isContentEditable || el.tagName === 'INPUT' ||
            el.tagName === 'TEXTAREA' || el.tagName === 'SELECT';
        if (!editable) fail('E_ELEMENT', '目标不是可输入元素（input/textarea/contenteditable）');
        el.focus();
        scrollIntoViewIfNeeded(el);

        if (el.isContentEditable) {
            if (params.clear !== false) el.textContent = '';
            el.textContent = el.textContent + text;
            fireInput(el, text);
        } else {
            if (params.clear !== false) {
                nativeSet(el, '');
                fireInput(el, '');
            }
            if (params.delay > 0) {
                for (var i = 0; i < text.length; i++) {
                    nativeSet(el, el.value + text[i]);
                    el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: text[i] }));
                    fireInput(el, text[i]);
                    await sleep(params.delay);
                }
            } else {
                nativeSet(el, el.value + text);
                fireInput(el, text);
            }
        }

        if (params.submit) {
            var form = el.form || (el.closest && el.closest('form'));
            el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
            await sleep(30);
            if (form && typeof form.requestSubmit === 'function') {
                try { form.requestSubmit(); } catch (e) { form.submit(); }
            } else {
                el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
            }
        }
        return { tag: el.tagName.toLowerCase(), value: clip(el.value != null ? el.value : el.textContent, 80) };
    }

    var KEY_CODES = {
        Enter: 13, Escape: 27, Tab: 9, Backspace: 8, Delete: 46, Space: 32,
        ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40,
        Home: 36, End: 35, PageUp: 33, PageDown: 34
    };

    function actKey(params) {
        var spec = String(params.keys || '').trim();
        if (!spec) fail('E_USAGE', '缺少按键');
        var parts = spec.split('+').map(function (p) { return p.trim(); }).filter(Boolean);
        var key = parts.pop() || '';
        var mods = { ctrl: false, alt: false, shift: false, meta: false };
        parts.forEach(function (part) {
            var low = part.toLowerCase();
            if (low === 'ctrl' || low === 'control') mods.ctrl = true;
            else if (low === 'alt') mods.alt = true;
            else if (low === 'shift') mods.shift = true;
            else if (low === 'meta' || low === 'cmd' || low === 'command') mods.meta = true;
        });
        if (key.length === 1) key = mods.shift ? key.toUpperCase() : key.toLowerCase();
        var code = KEY_CODES[key] || (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0);
        var codeName = KEY_CODES[key] ? key : (key.length === 1 ? 'Key' + key.toUpperCase() : key);

        var target = document.activeElement || document.body;
        var init = {
            bubbles: true, cancelable: true, view: window,
            key: key, code: codeName,
            ctrlKey: mods.ctrl, altKey: mods.alt, shiftKey: mods.shift, metaKey: mods.meta,
            keyCode: code, which: code
        };
        target.dispatchEvent(new KeyboardEvent('keydown', init));
        target.dispatchEvent(new KeyboardEvent('keypress', init));
        target.dispatchEvent(new KeyboardEvent('keyup', init));
        return { key: spec, target: target.tagName ? target.tagName.toLowerCase() : '' };
    }

    async function actHover(params) {
        var el = pick(params.selector);
        scrollIntoViewIfNeeded(el);
        await sleep(30);
        var rect = el.getBoundingClientRect();
        ['pointerover', 'mouseover', 'mouseenter', 'mousemove'].forEach(function (type) {
            try {
                var Ctor = type.indexOf('pointer') === 0 && window.PointerEvent ? PointerEvent : MouseEvent;
                el.dispatchEvent(new Ctor(type, {
                    bubbles: true, cancelable: true, view: window,
                    clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2
                }));
            } catch (e) { /* 忽略 */ }
        });
        return { tag: el.tagName.toLowerCase(), text: clip(el.innerText || '', 40) };
    }

    async function actScroll(params) {
        var behavior = params.smooth ? 'smooth' : 'auto';
        if (params.selector) {
            var el = pick(params.selector);
            el.scrollIntoView({ block: 'center', behavior: behavior });
            await sleep(params.smooth ? 400 : 60);
        } else if (params.top) {
            window.scrollTo({ top: 0, behavior: behavior });
        } else if (params.bottom) {
            window.scrollTo({ top: document.body.scrollHeight, behavior: behavior });
        } else if (params.by) {
            window.scrollBy({ top: Number(params.by) || 0, behavior: behavior });
        } else {
            window.scrollBy({ top: window.innerHeight * 0.9, behavior: behavior });
        }
        await sleep(params.smooth ? 350 : 60);
        return {
            scrollY: Math.round(window.scrollY),
            scrollX: Math.round(window.scrollX),
            height: document.body ? document.body.scrollHeight : 0
        };
    }

    function actSelect(params) {
        var el = pick(params.selector);
        if (el.tagName !== 'SELECT') fail('E_ELEMENT', '目标不是 <select> 元素');
        var options = Array.prototype.slice.call(el.options);
        var target = null;
        if (params.value != null && params.value !== '') {
            target = options.filter(function (o) { return o.value === String(params.value); })[0];
        }
        if (!target && params.label) {
            var needle = norm(params.label);
            target = options.filter(function (o) { return norm(o.textContent) === needle; })[0]
                || options.filter(function (o) { return norm(o.textContent).indexOf(needle) >= 0; })[0];
        }
        if (!target && params.optIndex != null) target = options[params.optIndex];
        if (!target) fail('E_ELEMENT', '没找到对应选项');

        el.value = target.value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return { value: el.value, text: norm(target.textContent), index: target.index };
    }

    function actCheck(params) {
        var el = pick(params.selector);
        if (el.type !== 'checkbox' && el.type !== 'radio') fail('E_ELEMENT', '目标不是 checkbox/radio');
        var want = params.checked !== false;
        if (el.checked !== want) {
            if (want && el.type === 'radio' && el.name) {
                // radio 组内互斥：先取消同组其它项
                Array.prototype.slice.call(document.querySelectorAll('input[type=radio][name="' + el.name + '"]'))
                    .forEach(function (other) { other.checked = false; });
            }
            // 走原生 click：能同时触发默认行为与页面监听器
            try { el.click(); } catch (e) { /* 忽略 */ }
            el.checked = want;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
        return { checked: el.checked, tag: el.tagName.toLowerCase() };
    }

    function actFocus(params) {
        var el = pick(params.selector);
        el.focus();
        if (el.scrollIntoView) el.scrollIntoView({ block: 'center' });
        return { tag: el.tagName.toLowerCase(), active: document.activeElement === el };
    }

    async function actFill(params) {
        var fields = params.fields || [];
        var failed = [];
        var done = 0;
        for (var i = 0; i < fields.length; i++) {
            var field = fields[i];
            try {
                var el = pick(field.selector);
                el.focus();
                if (el.isContentEditable) {
                    el.textContent = field.text;
                    fireInput(el, field.text);
                } else {
                    nativeSet(el, field.text);
                    fireInput(el, field.text);
                }
                done += 1;
            } catch (e) {
                failed.push({
                    selector: field.selector && field.selector.value,
                    message: e.message || String(e)
                });
            }
        }
        if (params.submit) {
            var form = document.querySelector('form');
            if (form && form.requestSubmit) {
                try { form.requestSubmit(); } catch (e) { /* 忽略 */ }
            }
        }
        return { count: done, failed: failed };
    }

    // ------------------------------------------------------------------ 页面信息
    function pageInfo() {
        var viewport = { w: window.innerWidth, h: window.innerHeight };
        var body = document.body || document.documentElement;
        return {
            url: location.href,
            title: document.title,
            readyState: document.readyState,
            viewport: viewport,
            scroll: {
                x: Math.round(window.scrollX),
                y: Math.round(window.scrollY),
                height: body ? body.scrollHeight : 0
            },
            lang: document.documentElement.lang || '',
            charset: document.characterSet,
            referrer: document.referrer || '',
            frames: window.frames.length,
            forms: document.forms.length,
            links: document.links.length,
            images: document.images.length,
            elements: document.getElementsByTagName('*').length
        };
    }

    function pageStop() {
        try { window.stop(); } catch (e) { /* 忽略 */ }
        return { stopped: true };
    }

    // ------------------------------------------------------------------ 日志/事件
    function logConsole(params) {
        var store = root.__DOMATE_LOGS || [];
        var limit = params.limit || 100;
        var entries = store.slice(-limit);
        var data = { entries: entries, count: entries.length, total: store.length };
        if (params.clear) store.length = 0;
        return data;
    }

    function logEvents(params) {
        var store = root.__DOMATE_EVENTS || [];
        var limit = params.limit || 50;
        var entries = store.slice(-limit);
        var data = { entries: entries, count: entries.length, total: store.length };
        if (params.clear) store.length = 0;
        return data;
    }

    // ------------------------------------------------------------------ 分发
    var TABLE = {
        'dom.tree': domTree,
        'dom.outline': domOutline,
        'dom.html': domHtml,
        'dom.text': domText,
        'dom.attr': domAttr,
        'dom.find': domFind,
        'dom.count': domCount,
        'dom.info': domInfo,
        'dom.wait': domWait,
        'act.click': actClick,
        'act.type': actType,
        'act.key': actKey,
        'act.hover': actHover,
        'act.scroll': actScroll,
        'act.select': actSelect,
        'act.check': actCheck,
        'act.focus': actFocus,
        'act.fill': actFill,
        'page.info': pageInfo,
        'page.stop': pageStop,
        'log.console': logConsole,
        'log.events': logEvents
    };

    async function run(action, params) {
        var handler = TABLE[action];
        if (!handler) fail('E_UNSUPPORTED', '页面引擎不支持该动作: ' + action);
        var data = await handler(params || {});
        return data === undefined ? { ok: true } : data;
    }

    root.DOMateEngine = {
        version: VERSION,
        run: run,
        actions: Object.keys(TABLE),
        serialize: serialize
    };
})(typeof window !== 'undefined' ? window : globalThis);
