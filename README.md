# DOMate

用终端控制浏览器的 AI 工具。一个 Python CLI + 一个 Chrome/Edge 插件，通过本地 WebSocket 通讯：
命令行读取页面结构、点按钮、填表单、注入 JS，输出既可以给人看，也能 `--json` 交给程序/AI 解析。

```
终端 (python main.py)  ──HTTP──▶  本地服务 127.0.0.1:8765  ──WebSocket──▶  浏览器插件  ──▶  页面
```

## 特性

- **标签页管理**：列出 / 选中 / 新建 / 关闭 / 激活 / 改标题 / 刷新，支持按 ID、序号、标题片段定位。
- **状态标题**：命令执行期间自动把目标标签页标题改成 `[读取中] xxx`、`[操作中] xxx`、`[执行中] xxx`，结束后自动还原。
- **页面读取**：紧凑 DOM 树、可交互元素清单（带索引）、HTML / 文本 / 属性 / 位置尺寸 / 页面概览。
- **交互执行**：点击（含双击/右键/强制）、输入（React/Vue 受控组件可用）、按键组合、悬停、滚动、下拉选择、勾选、批量填表。
- **脚本注入**：主世界执行，可读写页面全局变量，返回值为 JSON 可序列化的结果。
- **可观测**：每条 HTTP 请求、命令分发（含每次重试）、连接生命周期、CLI 调用都写成结构化事件，
  自带一个实时日志看板；同时捕获页面 console 输出与用户事件，支持截图。
- **多语言**：终端文案、帮助文档全部走语言包，内置 `zh-CN` / `en-US`。
- **零第三方依赖**：只用 Python 标准库（含自己实现的极简 WebSocket 服务端），Windows / Linux / macOS 通用。

## 安装

### 1. 终端侧

需要 Python 3.8+，无需 pip 安装。

```bash
python main.py status
```

### 2. 浏览器侧

1. Chrome / Edge 打开 `chrome://extensions`，右上角开启「开发者模式」。
2. 点「加载已解压的扩展程序」，选择本项目的 `extension` 目录。
3. 建议把它固定到工具栏，点开可以看到连接状态。

> 需要 Chrome/Edge 116+（MV3 Service Worker 的 WebSocket 保活）。`file://` 页面需要在插件详情里额外打开「允许访问文件网址」。

### 3. 验证

```bash
python main.py up           # 确保服务在跑并等插件连上（预热，可选）
python main.py doctor       # 环境自检，daemon / browser 都应为 ✔
python main.py tab list
```

### 4. 装成常驻后台服务（推荐）

服务本身就是一个常驻进程，和浏览器插件之间是一条长连接。不装也能用——命令发现服务没起会顺手拉起一个脱离终端
的进程；但「顺手拉起」有个前提：得有人执行命令。装成系统级自启后，登录就起来了，谁都不用管。

```bash
python main.py service install      # 注册 + 立即启动
python main.py service status       # 注册状态 / 是否在跑 / 插件是否连上
python main.py service stop         # 停掉
python main.py service uninstall    # 取消自启并移除配置
```

各平台落点：

| 平台 | 机制 | 位置 |
| --- | --- | --- |
| Windows | 「启动」文件夹里的 VBS（静默调用 `pythonw`，无控制台窗口） | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\DOMate.vbs` |
| Linux | systemd 用户服务，`Restart=always` | `~/.config/systemd/user/domate.service` |
| macOS | launchd 用户代理，`KeepAlive` | `~/Library/LaunchAgents/com.domate.bridge.plist` |

`service install --dry-run` 只打印将要做什么，不动系统。Linux 上如果希望退出登录后服务也活着，
再执行一次 `loginctl enable-linger $USER`。

装好之后，日常只剩命令本身：

```bash
python main.py tab list
python main.py click 3
```

## 快速上手

```bash
python main.py tab list                     # 看有哪些页面
python main.py tab use 2                    # 选中第 2 个作为默认目标
python main.py dom outline                  # 列出可交互元素及索引
python main.py click 3                      # 点第 3 个
python main.py dom tree --depth 4           # 看结构
python main.py type "css=#q" "DOMate" --submit
python main.py js "document.title"
python main.py shot out/page.png
```

`python main.py help` 看命令总览，`python main.py help click` 看单命令详细说明，
`python main.py help --guide` 看完整使用文档。

## 连接保持

插件到本地服务是一条 WebSocket。会断的只有两种情况：服务进程退出、浏览器回收插件的后台线程。
两者都已经处理，正常使用感知不到卡顿：

- **双向保活 15 秒**：服务端发 ping、插件回 pong。Chrome 116+ 规定 WebSocket 收发消息会重置后台
  线程的 30 秒空闲计时，心跳不断就不会被回收。
- **快速重连**：断线后 250ms 起重试、最多 800ms 一次，持续 15 秒；服务一恢复立刻连上，
  之后才退到 1–5 秒。
- **命令侧宽限**：命令落在断线空窗里不会立刻报错，会最多等 `command.connect_wait`（默认 3.5 秒）
  把重连接住。
- **事件补连**：切标签、切窗口、增删标签页都会顺便补连一次。
- `python main.py up` 可显式预热，`python main.py status` 能看到重连次数与上次断开时间。

## 日志与看板

每条事件写两份：`logs/domate.jsonl`（结构化，一行一个 JSON）和 `logs/domate.log`（人看的文本）。
事件固定带 `ts / t / lv / cat / ev / src / pid`，按分类补字段：

| 分类 | 事件 | 关键字段 |
| --- | --- | --- |
| `net` | `http` | method / path / status / bytes / dur_ms / poll |
| `cmd` | `rpc`、`dispatch` | action / ok / dur_ms / code / tab / tab_title / cid / retry |
| `ws` | `connect`、`disconnect`、`silent`… | cid / kind / tabs / error |
| `cli` | `invoke` | argv / exit / dur_ms / cwd |
| `sys` | `listen`、`stop`、`bind_failed`… | msg |

一条命令会串起 `cli/invoke` → `net/http` → `cmd/rpc` → `cmd/dispatch`，用 `--q <action>` 就能跟完整链路。

```bash
python main.py log serve --open     # 实时看板（只读，不需要服务在跑）
python main.py log stats --since 1h # 终端里看 KPI / 分类 / 命令排行 / 最近失败
python main.py log tail -f --cat cmd
python main.py log path
python main.py log clear            # 归档（重命名，不删除）
```

看板包含 KPI（失败率、P50/P95/P99、近 60 秒速率）、级别堆叠时间线、级别/分类分布、耗时直方图、
命令排行、星期 × 小时热力图、HTTP 状态码、实时日志流、可翻页事件表、失败明细、静默期、
客户端与标签页统计、最慢命令；支持按级别/分类/关键字/时间范围/轮询/失败筛选与 CSV 导出，
新日志通过 SSE 推送。日志超过 `server.log_max_mb` 自动轮转，内存窗口由 `dashboard.window` 控制。

## 选择器

| 写法 | 含义 |
| --- | --- |
| `#app .btn` / `css=#app .btn` | CSS 选择器（默认） |
| `text=登录` | 文本匹配，先精确后包含 |
| `xpath=//button[1]` | XPath |
| `3` | `dom outline` 给出的元素索引 |

配合 `--index 2` 取第 2 个匹配，或直接写 `#btn@2`。

## 输出与退出码

所有命令都支持 `--json`：

```bash
$ python main.py dom text "css=h1" --json
{
  "ok": true,
  "data": { "text": "示例标题", "length": 4 }
}
```

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 没找到元素 / 空结果 |
| 2 | 参数错误 / 服务未启动 |
| 3 | 超时 |
| 4 | 插件未连接 |
| 5 | 配置禁用 |

## 配置

`config.json`（命令行参数优先级更高）：

```json
{
    "language": "zh-CN",
    "server": { "host": "127.0.0.1", "port": 8765, "token": "", "auto_start": true,
                "keepalive_seconds": 15, "silence_seconds": 0 },
    "command": { "timeout": 30, "retry": 1, "connect_wait": 3.5 },
    "tab": { "status_title": true, "title_flash_ms": 600 },
    "security": { "allow_js": true, "allow_nav": true }
}
```

- `server.token`：非空时，RPC 与 WebSocket 都必须带同一 token（插件设置页填）。防止本机其它程序驱动你的浏览器。
- `server.keepalive_seconds`：服务端保活间隔，必须小于 30 秒才能阻止浏览器回收插件后台线程。
- `server.silence_seconds`：多久收不到插件的任何字节就判定连接僵死并断开，0 = 自动（保活间隔的 4 倍且不低于 30 秒）。别设太小，否则被浏览器节流的插件会被误杀。
- `command.connect_wait`：命令等待插件重连的宽限期，设 0 可让命令在插件离线时立刻失败。
- `security.allow_js/allow_nav`：设为 false 可直接禁用脚本注入/导航，适合把工具交给不可信调用方时使用。

## 项目结构

```
main.py                 入口
config.json             配置
lib/
  cli.py                参数解析与分发
  daemon.py             HTTP + WebSocket 本地服务
  ws.py                 极简 WebSocket 服务端实现
  hub.py                客户端注册表 / 命令路由 / 结果等待
  rpc.py                CLI 侧通讯与自动拉起后台服务
  commands/             按功能分类的命令实现
    tab_cmd.py          标签页
    dom_cmd.py          DOM 读取
    act_cmd.py          交互
    script_cmd.py       脚本注入
    page_cmd.py         导航/截图/日志
    log_cmd.py          日志看板/统计/tail/归档
    sys_cmd.py          服务/配置/自检/帮助
  logbook.py            结构化日志（JSONL + 文本镜像 + 轮转）
  selector.py           选择器语法解析
  i18n.py               语言包
  config.py / store.py  配置与本地状态
  output.py             终端输出（颜色/表格/JSON）
logview/                日志看板（stdlib HTTP + SSE + 手写前端）
  parser.py             JSONL/文本解析、增量 tail、聚合
  server.py             看板服务与 API
  web/                  单页前端（index.html / app.css / app.js）
extension/              浏览器插件（MV3）
  background.js         长连接、标签页调度、脚本注入、截图
  engine.js             页面侧 DOM 引擎（读取与合成事件）
  content.js            消息桥、标题状态、日志采集
  pagehook.js           主世界 console 钩子
res/lang/               语言包 zh-CN / en-US
docs/                   使用文档与协议说明
tests/                  端到端测试（含模拟浏览器）
```

## 测试

```bash
# 1) 端到端：真实服务 + 真实 CLI 子进程 + 模拟浏览器，覆盖 25 项行为
#    （路由、索引解析、超时、错误码、标题文案、长轮询通道）
python tests/test_e2e.py

# 2) 页面引擎：用 jsdom 造真实 DOM，跑一遍全部读取与交互动作（29 项）
npm i jsdom            # 仅测试用
node tests/engine_smoke.js

# 3) WebSocket 读取层：读超时可重入、半包/粘包、ping 插队、对端断开（9 项）
python tests/test_ws_read.py

# 4) 保活与判死：空闲不误杀、僵死连接被回收（10 项，约 30 秒）
python tests/test_keepalive.py

# 5) 后台服务：三平台注册方案、单实例约束、演练不落盘（31 项）
python tests/test_service.py
```

五套测试都不需要真实浏览器，改完代码直接跑即可回归。

## 已知限制

- **事件是合成的**：点击/输入通过 `dispatchEvent` 触发，`isTrusted=false`。少数风控严格的站点（银行、部分登录页）会识别并拒绝，这类场景需要 CDP 的真实输入通道（后续版本计划）。
- **截图只覆盖可视区域**：`chrome.tabs.captureVisibleTab` 的限制，无整页截图。
- **受限页面不可操作**：`chrome://`、扩展商店等页面禁止注入脚本。
- **单浏览器实例**：多个 Chrome 同时连接时按标签页 ID 区分，但 ID 在不同实例间可能重复，建议只连一个。

## 协议

终端与插件之间的消息格式见 [docs/zh-CN/protocol.md](docs/zh-CN/protocol.md)，方便自己写客户端或扩展命令。

## License

见 LICENSE。
