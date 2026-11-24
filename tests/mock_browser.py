# -*- coding: utf-8 -*-
"""模拟浏览器插件的假客户端，用于端到端测试（不依赖真实浏览器）。

它遵守插件侧协议：hello -> 收 command -> 回 result，并记录服务端下发的
labels 与 tabId，方便断言路由与标题状态文案是否正确。
"""

import json
import socket
import threading
import time

from wsclient import WSClient

TABS = [
    {"id": 101, "index": 0, "windowId": 1, "title": "首页", "url": "https://example.com/",
     "active": True, "pinned": False, "status": "complete"},
    {"id": 202, "index": 1, "windowId": 1, "title": "订单列表", "url": "https://example.com/orders",
     "active": False, "pinned": False, "status": "complete"},
]


class MockBrowser(threading.Thread):
    def __init__(self, url, client_id="mock-ext", frozen=False):
        threading.Thread.__init__(self, daemon=True)
        self.url = url
        self.client_id = client_id
        # frozen=True 模拟僵死连接：握手后既不读也不回，但 TCP 还没断。
        # 真机上这类连接常见于休眠唤醒、换网、NAT 超时之后。
        self.frozen = frozen
        self.commands = []
        self.labels = {}
        self.running = True
        self.error = None

    def run(self):
        try:
            self.ws = WSClient(self.url)
            self.ws.send_json({
                "type": "hello",
                "clientId": self.client_id,
                "kind": "extension",
                "version": "1.0.0-test",
                "browser": "Mock",
                "tabs": TABS,
            })
            if self.frozen:
                # 僵死连接：不发不收，但不主动关 socket
                while self.running:
                    time.sleep(0.2)
                return
            while self.running:
                try:
                    # 超时要比服务端保活间隔（15s）宽裕，否则空闲时会把假客户端"等死"
                    msg = self.ws.recv_json(timeout=40)
                except socket.timeout:
                    continue
                except Exception:
                    return
                if msg is None:
                    return
                if msg.get("type") == "welcome":
                    self.labels = msg.get("labels") or {}
                elif msg.get("type") == "command":
                    self.handle(msg)
                elif msg.get("type") == "ping":
                    # 和真实插件一致：收到 JSON ping 立刻回 pong
                    self.ws.send_json({"type": "pong", "t": msg.get("t")})
                elif msg.get("type") == "pong":
                    continue
        except Exception as exc:  # pragma: no cover
            self.error = exc

    def handle(self, msg):
        self.commands.append(msg)
        action = msg.get("action")
        tab_id = msg.get("tabId")
        params = msg.get("params") or {}

        if action == "tab.list":
            data = {"tabs": TABS}
        elif action == "tab.current":
            data = {"tab": TABS[0]}
        elif action == "tab.new":
            data = {"tab": {"id": 303, "title": "新标签页", "url": params.get("url")}}
        elif action == "tab.close":
            data = {"closed": params.get("ids") or [202], "count": 1}
        elif action == "tab.title":
            data = {"title": params.get("title"), "tabId": tab_id}
        elif action == "dom.text":
            data = {"text": "示例标题", "length": 4}
        elif action == "dom.outline":
            data = {"items": [{"idx": 1, "tag": "button", "text": "提交", "visible": True}], "count": 1}
        elif action == "act.click":
            data = {"tag": "button", "text": "提交", "navigated": False}
        elif action == "script.inject":
            data = {"value": "示例标题", "type": "string"}
        elif action == "slow.action":
            time.sleep(3)
            data = {}
        else:
            data = {"echo": action, "tabId": tab_id}

        self.ws.send_json({"type": "result", "id": msg.get("id"), "ok": True, "data": data})

    def stop(self):
        self.running = False
        try:
            self.ws.close()
        except Exception:
            pass
