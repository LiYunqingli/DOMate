# -*- coding: utf-8 -*-
"""保活与链路判死的回归测试。

背景（真实故障）：服务端原本用「读超时次数」判断客户端死活。MV3 的
service worker 被浏览器节流时 pong 会晚到几十秒，但 TCP 一直是通的，
结果服务端把健康连接主动切掉，插件再重连——用户看到的就是一段明显的卡顿。

现在的判据是「多久没收到任何字节」，所以：
- 一个正常回 pong 的客户端，空闲再久也不该被切；
- 一个彻底静默（模拟休眠/被节流到不回应）的客户端，最终要被回收。
"""

import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from lib import daemon
from lib.config import Config
from lib.i18n import I18n
from lib.store import State
from mock_browser import MockBrowser

PORT = 8831
PASSED = []
FAILED = []


def check(name, cond, detail=None):
    if cond:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append(name)
        print("  [FAIL] %s %s" % (name, detail if detail is not None else ""))


def wait_for(pred, timeout=10.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def boot():
    cfg = Config()
    cfg.set("server.port", PORT)
    cfg.set("server.keepalive_seconds", 5)
    cfg.set("server.silence_seconds", 8)
    cfg.set("command.connect_wait", 0.2)
    srv = daemon.create_server(cfg, I18n("zh-CN"), State())
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return srv


def call(srv, action="dom.text", tab=101, timeout=10):
    import json
    import urllib.request
    body = json.dumps({"action": action, "params": {}, "tab": tab}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/rpc" % PORT, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main():
    srv = boot()
    print("服务已启动：http://127.0.0.1:%d（保活 5s，静默判死 8s）" % PORT)
    try:
        # ---------------------------------------------------------- 正常客户端不该被误杀
        browser = MockBrowser("ws://127.0.0.1:%d/ws" % PORT)
        browser.start()
        check("插件已注册", wait_for(lambda: len(srv.hub.tabs()) == 2, 5))

        # 空闲 14 秒：跨过多个保活周期（旧的实现第二个周期就会把自己读死）
        time.sleep(14)
        check("空闲跨多个保活周期后连接仍在", len(srv.hub.clients) == 1,
              srv.hub.client_list())
        result = call(srv)
        check("空闲后命令仍可执行", result.get("ok") and
              (result.get("data") or {}).get("text") == "示例标题", result)
        check("空闲期间没有发生重连", srv.hub.reconnects == 0, srv.hub.reconnects)

        # ---------------------------------------------------------- 僵死客户端要被回收
        browser.stop()
        check("主动断开后客户端被移除", wait_for(lambda: not srv.hub.clients, 5))

        # 协议层 pong 由浏览器网络栈自动发，不依赖 JS，所以「静默」只能通过
        # 完全不读不回的僵死连接来模拟（休眠唤醒、换网后的常见状态）。
        frozen = MockBrowser("ws://127.0.0.1:%d/ws" % PORT, client_id="frozen", frozen=True)
        frozen.start()
        check("僵死客户端已注册", wait_for(lambda: len(srv.hub.clients) == 1, 5))
        began = time.time()
        dropped = wait_for(lambda: not srv.hub.clients, 25)
        spent = time.time() - began
        check("僵死客户端最终被回收", dropped, "%.1fs 后仍在" % spent)
        check("回收时机不早于静默阈值", spent >= 7.0, "%.1fs" % spent)
        frozen.stop()

        # ---------------------------------------------------------- 小包/大包都不影响保活
        big = MockBrowser("ws://127.0.0.1:%d/ws" % PORT, client_id="big")
        big.start()
        check("新客户端重新连上", wait_for(lambda: len(srv.hub.clients) == 1, 5))
        result = call(srv)
        check("重连后命令可用", result.get("ok"), result)
        big.stop()
    finally:
        srv.shutdown()
        srv.server_close()

    print("\n通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("失败项：" + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
