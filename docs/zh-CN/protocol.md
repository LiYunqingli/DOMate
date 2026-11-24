# DOMate 通讯协议

终端与浏览器插件之间的协议很小：一条 WebSocket 长连接 + JSON 消息。
自己写客户端（比如用 Node/Python 直接驱动浏览器）时照着这份文档实现即可。

## 1. 传输

| 通道 | 端点 | 用途 |
| --- | --- | --- |
| WebSocket | `ws://127.0.0.1:8765/ws?kind=extension&v=1.0.0` | 插件主通道 |
| HTTP 长轮询 | `POST /register` → `GET /poll?cid=...&timeout=25` → `POST /result` | 备用通道（脚本/调试） |
| HTTP RPC | `POST /rpc` | 终端把命令交给服务并同步等结果 |

鉴权：`config.json` 的 `server.token` 非空时，HTTP 请求需带 `X-DOMate-Token` 头；
WebSocket 因为浏览器构造器不支持自定义头，改用查询参数 `&token=xxx`。

## 2. WebSocket 消息

### 2.1 客户端 → 服务：握手

连接建立后第一条消息必须是 `hello`：

```json
{
  "type": "hello",
  "clientId": "ext-ab12cd34",
  "kind": "extension",
  "version": "1.0.0",
  "browser": "Chrome",
  "tabs": [
    { "id": 101, "index": 0, "windowId": 1, "title": "首页",
      "url": "https://example.com/", "active": true, "pinned": false, "status": "complete" }
  ]
}
```

### 2.2 服务 → 客户端：欢迎

```json
{
  "type": "welcome",
  "clientId": "ext-ab12cd34",
  "labels": { "read": "读取中", "act": "操作中", "exec": "执行中", "ok": "完成", "err": "出错" },
  "server": { "version": "1.0.0", "port": 8765 }
}
```

`labels` 是标题栏状态文案，由终端语言决定，客户端不要硬编码。

### 2.3 服务 → 客户端：命令

```json
{
  "type": "command",
  "id": "c1a2b3c4d5e6",
  "action": "act.click",
  "tabId": 101,
  "params": { "selector": { "by": "text", "value": "登录", "index": null } },
  "labels": { "read": "读取中", "act": "操作中", "exec": "执行中" },
  "timeout": 30
}
```

### 2.4 客户端 → 服务：结果

成功：

```json
{ "type": "result", "id": "c1a2b3c4d5e6", "ok": true, "data": { "tag": "button", "text": "登录" } }
```

失败：

```json
{ "type": "result", "id": "c1a2b3c4d5e6", "ok": false,
  "error": { "code": "E_ELEMENT", "message": "元素不可见", "hint": "selector" } }
```

### 2.5 其它消息

| 消息 | 方向 | 说明 |
| --- | --- | --- |
| `{"type":"tabs","tabs":[...]}` | 客户端 → 服务 | 全量标签页快照，服务端整体替换 |
| `{"type":"tab_update","tab":{...}}` | 客户端 → 服务 | 单个标签页更新 |
| `{"type":"tab_removed","tabId":101}` | 客户端 → 服务 | 标签页关闭 |
| `{"type":"ping","t":123}` | 双向 | 心跳，收到后回 `{"type":"pong","t":123}` |

服务端 20 秒无消息会主动发 ping，连续 3 次无响应即断开该客户端。

## 3. 选择器结构

CLI 把选择器表达式解析成统一结构后下发，浏览器端不需要理解四种语法：

```json
{ "by": "css|text|xpath", "value": "#app .btn", "index": 2 }
```

`index` 为 null 表示取第一个，2 表示取第 2 个匹配（1 起）。

## 4. 动作一览

### 标签页（由后台 service worker 处理）

| action | params | 返回 |
| --- | --- | --- |
| `tab.list` | — | `{tabs:[...]}` |
| `tab.current` | — | `{tab:{...}}` |
| `tab.new` | `url, active, pinned, newWindow` | `{tab}` |
| `tab.close` | `ids[], others, right` | `{closed:[], count}` |
| `tab.activate` | — (tabId) | `{tab}` |
| `tab.title` | `title` | `{title, tabId}` |
| `tab.reload` | `hard` | `{url, title, status}` |
| `tab.stop` | — | `{stopped}` |

### 导航 / 截图

| action | params | 返回 |
| --- | --- | --- |
| `nav.goto` | `url, wait` | `{url, title, status}` |
| `nav.back` / `nav.forward` | `steps` | `{url, title, status}` |
| `nav.reload` | `hard` | `{url, title, status}` |
| `capture.screenshot` | `format(png/jpeg), quality` | `{image:"data:image/png;base64,...", viewport}` |

### 脚本

| action | params | 返回 |
| --- | --- | --- |
| `script.inject` | `code, world(MAIN/ISOLATED)` | `{value, type}` |

### 页面读取（由内容脚本引擎处理）

| action | params | 返回 |
| --- | --- | --- |
| `dom.tree` | `selector, depth, maxNodes` | `{text, count, truncated}` |
| `dom.outline` | `max, all` | `{items:[{idx,tag,type,text,rect,...}], count}` |
| `dom.html` | `selector` | `{html, length}` |
| `dom.text` | `selector` | `{text, length}` |
| `dom.attr` | `selector, name` | `{name, value, exists}` |
| `dom.find` | `selector, limit` | `{items:[...], count}` |
| `dom.count` | `selector` | `{count}` |
| `dom.info` | `selector` | `{tag, id, classes, rect, styles, attrs, ...}` |
| `dom.wait` | `selector, text, gone, timeout` | `{found, elapsed}` |
| `page.info` | — | `{url, title, viewport, scroll, links, forms, ...}` |
| `log.console` | `limit, clear` | `{entries:[{level,text,t}], total}` |
| `log.events` | `limit, clear` | `{entries:[{t,type,target,value}], total}` |

### 交互

| action | params | 返回 |
| --- | --- | --- |
| `act.click` | `selector, double, right, force, waitNav` | `{tag, text, navigated, url}` |
| `act.type` | `selector, text, clear, submit, delay` | `{tag, value}` |
| `act.key` | `keys` | `{key, target}` |
| `act.hover` | `selector` | `{tag, text}` |
| `act.scroll` | `selector, by, top, bottom, smooth` | `{scrollY, height}` |
| `act.select` | `selector, value, label, optIndex` | `{value, text}` |
| `act.check` | `selector, checked` | `{checked}` |
| `act.focus` | `selector` | `{tag, active}` |
| `act.fill` | `fields:[{selector,text}]` | `{count, failed[]}` |

## 5. 错误码

| code | 含义 | 终端退出码 |
| --- | --- | --- |
| `E_NO_BROWSER` | 没有可用客户端 | 4 |
| `E_NO_TAB` | 标签页不存在 | 1 |
| `E_ELEMENT` | 元素不存在/不可见 | 1 |
| `E_SELECTOR` | 选择器语法错误 | 2 |
| `E_TIMEOUT` | 超时 | 3 |
| `E_SCRIPT` | 页面脚本抛错 | 1 |
| `E_PERM` | 无权限（受限页面） | 5 |
| `E_DISABLED` | 配置禁用 | 5 |
| `E_UNSUPPORTED` | 不支持的动作 | 1 |

## 6. 写一个最小客户端（Python 示例）

```python
import json, asyncio, websockets  # 仅示例，本项目自带零依赖实现

async def main():
    async with websockets.connect("ws://127.0.0.1:8765/ws") as ws:
        await ws.send(json.dumps({
            "type": "hello", "clientId": "my-client", "kind": "extension",
            "version": "0.1", "tabs": []
        }))
        print(await ws.recv())            # welcome
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] != "command":
                continue
            # 假装执行，直接回显
            await ws.send(json.dumps({
                "type": "result", "id": msg["id"], "ok": True,
                "data": {"action": msg["action"], "tabId": msg["tabId"]}
            }))

asyncio.run(main())
```

连上后终端就能跑 `python main.py tab list --json` 看到这个客户端。
