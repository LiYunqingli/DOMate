// 引擎冒烟测试：用 jsdom 造一个真实 DOM，跑一遍所有读取与交互动作。
// 目的不是像素级验证，而是确保 engine.js 在真实 DOM API 上不抛异常、返回值结构正确。
// 运行： node tests/engine_smoke.js   （需要 jsdom，安装方式见 docs）
'use strict';

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const html = `<!DOCTYPE html><html lang="zh-CN"><head><title>测试页</title></head><body>
  <h1 id="title">商品标题</h1>
  <div id="app" class="wrap main">
    <button id="btn" onclick="window.__clicked = (window.__clicked || 0) + 1">提交</button>
    <input id="q" name="q" type="text" placeholder="搜索">
    <a href="/next" class="link">下一页</a>
    <select id="city"><option value="310000">上海</option><option value="110000">北京</option></select>
    <input type="checkbox" id="agree">
    <div id="hidden" style="display:none">隐藏内容</div>
  </div>
</body></html>`;

const dom = new JSDOM(html, {
    url: 'https://example.com/list',
    pretendToBeVisual: true,
    runScripts: 'outside-only'
});
const { window } = dom;

// jsdom 没有布局引擎，补一个固定矩形，让可见性判断可跑
window.Element.prototype.getBoundingClientRect = function () {
    return { left: 10, top: 20, width: 100, height: 30, right: 110, bottom: 50, x: 10, y: 20, toJSON() { return this; } };
};
window.scrollTo = () => {};
window.scrollBy = () => {};
window.Element.prototype.scrollIntoView = function () {};

// 在 window 上下文里加载引擎
window.eval(fs.readFileSync(path.join(ROOT, 'extension', 'engine.js'), 'utf8'));
const engine = window.DOMateEngine;

let passed = 0;
const failed = [];
function check(name, cond, detail) {
    if (cond) { passed++; console.log('  [PASS] ' + name); }
    else { failed.push(name); console.log('  [FAIL] ' + name + ' -> ' + JSON.stringify(detail)); }
}

const sel = (by, value, index) => ({ by, value, index: index === undefined ? null : index });

(async () => {
    // ---------------------------------------------------------------- 读取
    let data = await engine.run('dom.tree', { selector: null, depth: 3, maxNodes: 100 });
    check('dom.tree 含标题文本', data.text.indexOf('商品标题') >= 0 && data.count > 3, data.text);

    data = await engine.run('dom.outline', { max: 50, all: false });
    const tags = data.items.map((i) => i.tag);
    check('dom.outline 找到按钮/输入/链接/下拉', ['button', 'input', 'a', 'select'].every((t) => tags.indexOf(t) >= 0), tags);
    check('dom.outline 默认排除隐藏元素', data.items.every((i) => i.idx > 0), data.items.length);
    check('dom.outline 打上索引属性', window.document.getElementById('btn').getAttribute('data-domate-idx') !== null);

    data = await engine.run('dom.text', { selector: sel('css', '#title') });
    check('dom.text 读取文本', data.text === '商品标题', data);

    data = await engine.run('dom.html', { selector: sel('css', '.link') });
    check('dom.html 返回 outerHTML', data.html.indexOf('下一页') >= 0, data.html);

    data = await engine.run('dom.attr', { selector: sel('css', '.link'), name: 'href' });
    check('dom.attr 读取 href', data.exists && data.value === '/next', data);

    data = await engine.run('dom.count', { selector: sel('css', '.link') });
    check('dom.count 统计', data.count === 1, data);

    data = await engine.run('dom.find', { selector: sel('text', '下一页'), limit: 5 });
    check('dom.find 按文本匹配', data.count >= 1, data);

    data = await engine.run('dom.info', { selector: sel('css', '#btn') });
    check('dom.info 返回标签与属性', data.tag === 'button' && data.attrs.id === 'btn', data);

    data = await engine.run('dom.wait', { selector: sel('css', '.link'), timeout: 1 });
    check('dom.wait 命中立即返回', data.found === true, data);

    data = await engine.run('dom.wait', { selector: sel('css', '.nope'), gone: true, timeout: 1 });
    check('dom.wait --gone 反向判断', data.found === true, data);

    // ---------------------------------------------------------------- 交互
    data = await engine.run('act.type', { selector: sel('css', '#q'), text: 'DOMate', clear: true });
    const input = window.document.getElementById('q');
    check('act.type 写入 value', input.value === 'DOMate' && data.value === 'DOMate', data);

    let inputEvents = 0;
    input.addEventListener('input', () => { inputEvents++; });
    await engine.run('act.type', { selector: sel('css', '#q'), text: '!', clear: false });
    check('act.type 触发 input 事件', inputEvents >= 1 && input.value === 'DOMate!', input.value);
    check('act.type clear=false 追加', input.value === 'DOMate!', input.value);

    let clicks = 0;
    window.document.getElementById('btn').addEventListener('click', () => { clicks++; });
    data = await engine.run('act.click', { selector: sel('css', '#btn') });
    // 只允许一次 click：曾经为了触发默认行为额外调了 el.click()，会造成重复提交
    check('act.click 只触发一次 click', clicks === 1, { clicks });

    data = await engine.run('act.check', { selector: sel('css', '#agree'), checked: true });
    check('act.check 勾选', window.document.getElementById('agree').checked === true && data.checked === true, data);

    data = await engine.run('act.check', { selector: sel('css', '#agree'), checked: false });
    check('act.check 取消勾选', window.document.getElementById('agree').checked === false, data);

    data = await engine.run('act.select', { selector: sel('css', '#city'), value: '310000' });
    check('act.select 按 value 选中', data.value === '310000' && data.text === '上海', data);

    data = await engine.run('act.select', { selector: sel('css', '#city'), label: '北京' });
    check('act.select 按文本选中', data.value === '110000', data);

    let keys = [];
    window.document.getElementById('q').addEventListener('keydown', (e) => keys.push(e.key));
    window.document.getElementById('q').focus();
    data = await engine.run('act.key', { keys: 'Enter' });
    check('act.key 发送按键', keys.indexOf('Enter') >= 0, keys);

    data = await engine.run('act.scroll', { by: 300 });
    check('act.scroll 返回位置', typeof data.scrollY === 'number', data);

    data = await engine.run('act.hover', { selector: sel('css', '#btn') });
    check('act.hover 正常返回', data.tag === 'button', data);

    data = await engine.run('act.focus', { selector: sel('css', '#q') });
    check('act.focus 聚焦', data.active === true, data);

    data = await engine.run('act.fill', {
        fields: [
            { selector: sel('css', '#q'), text: '批量1' },
            { selector: sel('css', '#nope'), text: 'x' }
        ]
    });
    check('act.fill 部分失败不中断', data.count === 1 && data.failed.length === 1, data);

    // ---------------------------------------------------------------- 页面
    data = await engine.run('page.info', {});
    check('page.info 返回地址与标题', data.url.indexOf('example.com') >= 0 && data.title === '测试页', data);
    check('page.info 统计元素数量', data.elements > 5 && data.links === 1, data);

    // ---------------------------------------------------------------- 错误
    try {
        await engine.run('dom.text', { selector: sel('css', '#missing') });
        check('缺失元素抛错', false, 'no error');
    } catch (e) {
        check('缺失元素抛 E_ELEMENT', e.code === 'E_ELEMENT', e.code);
    }

    try {
        await engine.run('nope.action', {});
        check('未知动作抛错', false, 'no error');
    } catch (e) {
        check('未知动作抛 E_UNSUPPORTED', e.code === 'E_UNSUPPORTED', e.code);
    }

    console.log('\n通过 ' + passed + ' 项，失败 ' + failed.length + ' 项');
    if (failed.length) {
        console.log('失败项：' + failed.join(', '));
        process.exit(1);
    }
})();
