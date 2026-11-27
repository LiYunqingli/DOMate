---
name: domate
description: 用终端命令驱动本机 Chrome/Edge——读页面、点按钮、填表单、注入 JS、截图。用于需要真的在浏览器里操作网页的任务:登录、填表、翻页抓取、验证前端行为、排查"点了没反应"。只读网页源码时不要用。
---

# DOMate

一条命令一个动作:先读,再点,后验。全部命令支持 `--json`。

## 首次

```bash
python main.py service install     # 注册后台服务并立即启动,只需一次
python main.py up --json           # 确认插件已连上,data.ready=true 才继续
```

`up` 返回 `ready: false` 时让用户打开浏览器、点插件图标确认 ON,再重试一次。

## 输出

```json
{"ok": true, "data": {...}}
{"ok": false, "error": {"code": "E_ELEMENT", "message": "...", "hint": "..."}}
```

| 退出码 | 含义 | 下一步 |
| --- | --- | --- |
| 0 | 成功 | 继续 |
| 1 | 元素没找到 / 空结果 | `dom outline` 或 `dom find` 重新定位 |
| 2 | 参数错 / 服务没起 | 看 message;跑 `doctor` |
| 3 | 超时 | 加 `--timeout`,或用 `dom wait` 等条件 |
| 4 | 插件没连上 | `up` 重试一次,仍失败就停下来问用户 |
| 5 | 受限页面 / 配置禁用 | `chrome://`、扩展商店无法注入 |

`doctor --json` 的顶层 `ok` 恒为 true,要看退出码和 `data.checks[].ok`。

## 命令

```bash
# 标签页
python main.py tab list --json
python main.py tab use 2                    # 序号 / ID / 标题片段
python main.py tab new "https://..." --select

# 读取
python main.py page --json                  # 地址、标题、视口、链接/表单统计
python main.py dom outline --json           # 可交互元素 + 索引,主入口
python main.py dom tree --depth 3 --json
python main.py dom text "css=h1" --json
python main.py dom info "css=#submit" --json
python main.py dom find "css=.item" --json
python main.py dom count "css=.row" --json

# 交互
python main.py click 3 --json               # --double / --right / --force / --wait-nav
python main.py type "css=#q" "关键词" --submit --json
python main.py key "Enter" --json
python main.py select "css=#city" "杭州" --json
python main.py check "css=#agree" --json
python main.py scroll --bottom --json
python main.py fill "#user=admin" "#pwd=123456" --json

# 脚本 / 页面
python main.py js "document.title" --json
python main.py js @extract.js --json
python main.py goto "https://..." --json
python main.py shot out/a.png

# 验证
python main.py dom wait "css=.row" --timeout 15 --json
python main.py dom wait "css=.loading" --gone --timeout 20 --json
python main.py console --limit 20 --json
python main.py events --limit 20 --json
```

`fill` 按**第一个** `=` 切分,选择器不能带 `css=` 前缀,写成 `"#user=admin"`。

## 选择器

| 写法 | 场景 |
| --- | --- |
| `3` / `@3` | `dom outline` 的索引,首选 |
| `text=登录` | 文案稳定时 |
| `css=#app .btn` | 有稳定 id/class 时(省略 `css=` 也是 CSS) |
| `//button[1]` | 兜底 |
| `"css=.item" --index 2` / `#btn@2` | 取第 2 个匹配 |

索引只在下一次 `dom outline` 之前有效,跨轮次重新读。

## 时序

每条命令是独立进程,启动约 500ms,`up` 消不掉这部分。

- 少起进程:多次读取合并成一次 `js` 返回数组。
- 等待用 `dom wait`,不要 `sleep`,也不要反复重跑加超时。
- 多个交互动作在一条 bash 里用 `&&` 串联。

## 两个模板

```bash
# 登录
python main.py tab use <目标> && \
python main.py type "css=#user" "admin" --json && \
python main.py type "css=#pwd" "@pwd.txt" --json && \
python main.py click "text=登录" --wait-nav --json && \
python main.py page --json
```

```bash
# 翻页抓取
python main.py dom wait "css=.row" --timeout 15 --json && \
python main.py js @extract.js --json > page1.json && \
python main.py click "text=下一页" --json && \
python main.py dom wait "css=.row" --timeout 15 --json
```

## 别做

- 别用 `js` 设 `input.value` 或代替点击——用 `type` / `click`,它们处理受控组件和导航等待。
- 别点完就认为成功,用 `page` / `console` / `events` 验证。
- 别默认加 `--force`,先 `dom info` 看是不是被遮挡。
- 强风控站点(银行、部分登录页)因 `isTrusted=false` 会拒绝合成事件,遇到直接告知用户。
- 不确定参数就 `python main.py help <命令>`。
