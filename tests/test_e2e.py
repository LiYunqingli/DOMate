# -*- coding: utf-8 -*-
"""端到端测试：真实服务 + 模拟浏览器 + 真实 CLI 子进程。

运行： python tests/test_e2e.py
不依赖 pytest，也不依赖真实浏览器，适合改动后快速回归。
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import daemon                     # noqa: E402
from lib.config import Config              # noqa: E402
from lib.i18n import I18n                  # noqa: E402
from lib.store import State                # noqa: E402
from mock_browser import MockBrowser       # noqa: E402

PORT = 8799
PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append(name)
        print("  [FAIL] %s %s" % (name, detail))


def cli(*args, **kwargs):
    """调用 CLI，返回 (exit_code, stdout, stderr)。"""
    cmd = [sys.executable, os.path.join(ROOT, "main.py")] + list(args)
    if "--port" not in args:
        cmd += ["--port", str(PORT)]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def cli_json(*args):
    code, out, err = cli(*args, "--json")
    try:
        return code, json.loads(out), err
    except ValueError:
        return code, None, err or out


def wait_for(predicate, timeout=8.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main():
    config = Config()
    config.set("server.port", PORT)
    config.set("language", "zh-CN")
    i18n = I18n("zh-CN")
    server = daemon.create_server(config, i18n, State())
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    print("服务已启动：http://127.0.0.1:%d" % PORT)

    browser = MockBrowser("ws://127.0.0.1:%d/ws" % PORT)
    browser.start()

    ok = wait_for(lambda: server.RequestHandlerClass is not None and len(server.hub.tabs()) == 2)
    check("插件注册并上报标签页", ok, server.hub.tabs())

    # ---------------------------------------------------------------- 无插件时的报错
    # （先断开，验证 E_NO_BROWSER 路径；随后重连继续测试）
    browser.stop()
    wait_for(lambda: not server.hub.clients, 3)
    code, payload, err = cli_json("dom", "text", "css=h1")
    check("插件离线时返回 E_NO_BROWSER", code == 4 and payload and
          payload.get("error", {}).get("code") in ("E_NO_BROWSER", "E_DAEMON_DOWN"),
          "%s %s %s" % (code, payload, err))

    browser = MockBrowser("ws://127.0.0.1:%d/ws" % PORT)
    browser.start()
    check("重连成功", wait_for(lambda: len(server.hub.tabs()) == 2, 5))

    # ---------------------------------------------------------------- 标签页
    code, payload, _ = cli_json("tab", "list")
    tabs = (payload or {}).get("data", {}).get("tabs", [])
    check("tab list 返回 2 个标签页", code == 0 and len(tabs) == 2, payload)
    check("tab list 带序号", [t.get("seq") for t in tabs] == [1, 2], tabs)

    code, payload, _ = cli("tab", "use", "2")
    check("tab use 按序号选中", code == 0, payload or code)

    state = State()
    check("选中状态已持久化", state.get("tab_id") == 202, state.data)

    # ---------------------------------------------------------------- 路由到正确标签页
    browser.commands.clear()
    code, payload, _ = cli_json("dom", "text", "css=h1")
    check("dom text 成功", code == 0 and (payload or {}).get("data", {}).get("text") == "示例标题", payload)
    last = browser.commands[-1] if browser.commands else {}
    check("命令路由到选中的标签页", last.get("tabId") == 202, last.get("tabId"))
    check("命令携带状态标题文案", bool(last.get("labels", {}).get("read")), last.get("labels"))

    # --tab 覆盖
    code, payload, _ = cli_json("dom", "text", "css=h1", "--tab", "101")
    check("--tab 覆盖默认标签页", code == 0 and browser.commands[-1].get("tabId") == 101,
          browser.commands[-1].get("tabId"))

    # ---------------------------------------------------------------- 交互与索引
    code, payload, _ = cli_json("click", "3")
    check("click 支持纯数字索引", code == 0, payload)
    selector = (browser.commands[-1].get("params") or {}).get("selector") or {}
    check("索引被解析成 data-domate-idx 选择器",
          selector.get("value") == '[data-domate-idx="3"]', selector)

    code, payload, _ = cli_json("dom", "outline")
    items = (payload or {}).get("data", {}).get("items", [])
    check("dom outline 输出索引列表", code == 0 and items and items[0]["idx"] == 1, payload)

    # ---------------------------------------------------------------- 脚本
    code, payload, _ = cli_json("js", "document.title")
    check("js 注入返回值", code == 0 and (payload or {}).get("data", {}).get("value") == "示例标题", payload)

    # ---------------------------------------------------------------- 状态与配置
    code, payload, _ = cli_json("status")
    check("status 报告浏览器在线", code == 0 and payload["data"]["browser"]["connected"], payload)

    code, out, err = cli("doctor")
    check("doctor 可运行", code in (0, 2), out + err)

    # ---------------------------------------------------------------- 超时
    body = json.dumps({"action": "slow.action", "params": {}, "tab": 101, "timeout": 1}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/rpc" % PORT, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
    check("超时返回 E_TIMEOUT", result.get("error", {}).get("code") == "E_TIMEOUT", result)

    # ---------------------------------------------------------------- 长轮询通道
    req = urllib.request.Request(
        "http://127.0.0.1:%d/register" % PORT,
        data=json.dumps({"kind": "userscript", "tab": {"id": 900, "url": "https://x", "title": "x"}}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        reg = json.loads(resp.read().decode())
    check("HTTP 注册通道可用", reg.get("ok") and reg["data"].get("clientId"))
    check("注册通道也拿到标题文案", bool(reg["data"]["labels"].get("act")))

    browser.stop()
    server.shutdown()
    server.server_close()

    print("\n通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("失败项：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
