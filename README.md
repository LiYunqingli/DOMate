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
- **可观测**：捕获页面 console 输出与用户事件，支持截图。
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
python main.py serve        # 前台启动服务（可选，其它命令会自动拉起）
python main.py doctor       # 环境自检，daemon / browser 都应为 ✔
python main.py tab list
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
    "server": { "host": "127.0.0.1", "port": 8765, "token": "", "auto_start": true },
    "command": { "timeout": 30, "retry": 1 },
    "tab": { "status_title": true, "title_flash_ms": 600 },
    "security": { "allow_js": true, "allow_nav": true }
}
```

- `server.token`：非空时，RPC 与 WebSocket 都必须带同一 token（插件设置页填）。防止本机其它程序驱动你的浏览器。
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
    sys_cmd.py          服务/配置/自检/帮助
  selector.py           选择器语法解析
  i18n.py               语言包
  config.py / store.py  配置与本地状态
  output.py             终端输出（颜色/表格/JSON）
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
# 1) 端到端：真实服务 + 真实 CLI 子进程 + 模拟浏览器，覆盖 20 项行为
#    （路由、索引解析、超时、错误码、标题文案、长轮询通道）
python tests/test_e2e.py

# 2) 页面引擎：用 jsdom 造真实 DOM，跑一遍全部读取与交互动作（29 项）
npm i jsdom            # 仅测试用
node tests/engine_smoke.js
```

两套测试都不需要真实浏览器，改完代码直接跑即可回归。

## 已知限制

- **事件是合成的**：点击/输入通过 `dispatchEvent` 触发，`isTrusted=false`。少数风控严格的站点（银行、部分登录页）会识别并拒绝，这类场景需要 CDP 的真实输入通道（后续版本计划）。
- **截图只覆盖可视区域**：`chrome.tabs.captureVisibleTab` 的限制，无整页截图。
- **受限页面不可操作**：`chrome://`、扩展商店等页面禁止注入脚本。
- **单浏览器实例**：多个 Chrome 同时连接时按标签页 ID 区分，但 ID 在不同实例间可能重复，建议只连一个。

## 协议

终端与插件之间的消息格式见 [docs/zh-CN/protocol.md](docs/zh-CN/protocol.md)，方便自己写客户端或扩展命令。

## License

见 LICENSE。
