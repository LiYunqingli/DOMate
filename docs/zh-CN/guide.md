# DOMate 使用指南

## 一、它解决什么问题

浏览器是 AI 最容易「够到」的界面，但让模型直接操作页面有三件事必须做对：
一要知道页面上有什么，二要能点得准，三要知道操作有没有生效。
DOMate 把这三件事拆成稳定的命令：读取（dom *）、交互（click/type/...）、验证（page/console/events/shot）。

设计取向是「给 AI 用的终端工具」：
- 每条命令一个动作，执行完就退出；
- 默认输出人能读的表格/文本，加 --json 输出结构化结果；
- 失败有稳定错误码（E_ELEMENT、E_TIMEOUT、E_NO_BROWSER 等），不用去猜文案。

## 二、安装与启动

终端侧只需要 Python 3.8+，不装任何第三方包。

```
python main.py doctor
```

看到 daemon 与 browser 都是 ✔ 就绪。第一次跑命令时如果服务没起，工具会自动在后台拉起
（进程脱离终端，关掉命令行也不会断），日志在 logs/domate.log。

浏览器侧：chrome://extensions → 开发者模式 → 加载已解压的扩展程序 → 选 extension 目录。
插件图标显示 ON 表示已连上本地服务。

## 三、核心概念

### 1. 标签页与「选中」

几乎所有命令都作用于「当前选中的标签页」。没选中时默认用浏览器当前活动标签页。

```
python main.py tab list
python main.py tab use 2        # 按列表序号
python main.py tab use 120      # 按标签页 ID
python main.py tab use github   # 按标题/地址片段
```

单条命令想临时换目标，用 --tab：

```
python main.py dom text "css=h1" --tab 1
```

选中的标签页记在 ~/.domate/state.json，浏览器重启后 ID 会变，工具会自动回落到活动标签页。

### 2. 状态标题

命令执行时，目标标签页的标题会被临时加上前缀：读取类显示 [读取中]，交互类显示 [操作中]，
脚本类显示 [执行中]，结束后恢复原标题。这样即使同时开着多个页面，也能一眼看出工具在操作哪个。
可以在 config.json 里用 tab.status_title=false 关掉。

### 3. 选择器与索引

四种写法任选：

```
#app .btn                 CSS（默认）
css=#app .btn             显式 CSS
text=登录                  文本匹配
xpath=//button[1]         XPath
3                         索引（来自 dom outline）
```

具体选哪一个，建议先用 `dom outline`：

```
python main.py dom outline
```

它会把链接、按钮、输入框、可点击区域编号列出来，并在页面上打上 data-domate-idx 标记。
此后 `python main.py click 3` 就是点第 3 个，不用再拼选择器——这是给 AI 用的最短路径。

需要在一组匹配里挑具体某个时，用 --index：

```
python main.py click "css=.item" --index 2
```

## 四、典型工作流

### 场景 A：读懂一个陌生页面

```
python main.py page                       # 地址、标题、视口、元素统计
python main.py dom tree --depth 3         # 结构骨架
python main.py dom outline                # 能点的东西
python main.py dom text "css=h1"          # 关键文案
```

### 场景 B：填表并提交

```
python main.py dom outline                       # 找到输入框索引
python main.py type "css=#username" "admin"
python main.py type "css=#password" "@pwd.txt"   # 密码放文件，不进 shell 历史
python main.py click "text=登录" --wait-nav
python main.py page                              # 确认跳转
```

或者一次填多个字段：

```
python main.py fill "css=#username=admin" "css=#email=a@b.com"
```

### 场景 C：等异步渲染

单页应用常见「点完之后列表才出来」：

```
python main.py click "text=查询"
python main.py dom wait "css=.result-row" --timeout 15
python main.py dom text "css=.result-row:first-child"
```

反向等待（等 loading 消失）：

```
python main.py dom wait "css=.loading" --gone --timeout 20
```

### 场景 D：抓数据

```
python main.py js "Array.from(document.querySelectorAll('.price')).map(e => e.innerText)"
python main.py js @extract.js > data.json
```

js 的返回值会自动做序列化：DOM 节点转成 outerHTML，循环引用标记出来，函数变成 [Function]。

### 场景 E：排查「点了没反应」

```
python main.py events --limit 20     # 看点击/输入是否真的发生了
python main.py console               # 看页面有没有报错
python main.py dom info "css=#submit"  # 看元素是否被遮挡/禁用
python main.py shot debug.png        # 截图确认
```

如果元素不可见或被遮挡，`click` 会拒绝执行；确认要强制点，加 --force。

## 五、命令速查

服务与配置

```
serve / stop / status / doctor / config / version / help
```

标签页

```
tab list / tab use / tab current / tab new / tab close
tab activate / tab title / tab reload / tab stop
```

读取

```
dom tree      结构树，--depth 控层数，--max 控节点数
dom outline   可交互元素 + 索引
dom html      元素 HTML
dom text      可见文本
dom find      匹配列表（序号/位置/可见性）
dom attr      单个属性
dom count     匹配数量
dom info      详细信息
dom wait      等待出现/消失
```

交互

```
click         --double / --right / --force / --wait-nav / --index
type          --append / --submit / --delay
key           Enter、Ctrl+A、Ctrl+Shift+Enter
hover / scroll / select / check / uncheck / focus / fill
```

脚本

```
js     注入并求值（主世界）
eval   同 js，面向「取一个值」
```

页面

```
page / goto / back / forward / reload / shot / console / events
```

## 六、配置

config.json 常用项：

```
language              终端语言 zh-CN / en-US
server.port           服务端口，默认 8765
server.token          访问令牌，非空时插件需填同样的值
server.auto_start     命令执行时是否自动拉起服务
command.timeout       默认命令超时（秒）
command.retry         超时重试次数（读命令幂等，写命令建议设 0）
tab.status_title      是否在标题栏显示「读取中/操作中/执行中」
security.allow_js     false 时禁用 js/eval
security.allow_nav    false 时禁用 goto/back/forward
output.max_text       终端单次输出的文本上限
```

改配置用命令，避免手抖改坏 JSON：

```
python main.py config set server.port 8790
python main.py config get language
python main.py config reset
```

换端口后插件设置页的端口要同步改。

## 七、排错

E_NO_BROWSER    插件没连上。看插件图标是否 ON；先 `python main.py serve` 再点插件「重新连接」。
E_DAEMON_DOWN   服务没起来。`python main.py doctor` 看端口是否被占。
E_PORT          端口冲突。`python main.py serve --port 8790`，或改 config.json。
E_ELEMENT       元素不存在或不可见。用 `dom find` 确认，必要时 --force。
E_TIMEOUT       页面卡住。放大 `--timeout`，或 `tab stop` 中断加载。
E_PERM          chrome:// 等受限页面，插件无权注入。
中文乱码        控制台编码不是 UTF-8；main.py 已强制 UTF-8，若仍异常请换 Windows Terminal。
插件报「无法在该页面注入脚本」  正常限制，换个普通网页即可。

自检命令一次性看全：`python main.py doctor --json`。

## 八、限制

- 合成的鼠标/键盘事件 isTrusted=false，强风控站点（银行、部分登录）可能拒绝；这类需要真实输入通道。
- 截图只有可视区域。
- 不做网络抓包，也不代理流量；需要接口数据请用 js 调 fetch 或走其它工具。
- 同一时刻建议只连一个浏览器实例。
