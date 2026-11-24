# -*- coding: utf-8 -*-
"""命令中枢：客户端注册、标签页注册表、命令路由与结果等待。

角色划分：
- 插件（extension）作为唯一「浏览器代理」客户端，通过 WebSocket 连进来；
- HTTP 长轮询（userscript/调试）作为备用传输，走队列；
- CLI 每次执行只做一次 RPC，真正的跨进程等待在这里完成。
"""

import json
import queue
import threading
import time
import uuid

from .errors import DomateError

#: 不需要指定标签页的 tab.* 命令
TAB_GLOBAL_ACTIONS = ("tab.list", "tab.new", "tab.current")

#: 只读动作：超时重试是安全的。写动作（点击/输入/导航）超时后重试可能重复执行，
#: 所以一律不自动重试，除非调用方显式指定 retry。
READ_ONLY_PREFIXES = ("dom.", "log.", "page.info", "tab.list", "tab.current", "capture.")


def is_readonly(action):
    return action.startswith(READ_ONLY_PREFIXES)


class Pending(object):
    """一次命令的等待槽。"""

    def __init__(self, cmd_id, action):
        self.id = cmd_id
        self.action = action
        self.event = threading.Event()
        self.result = None
        self.created = time.time()

    def resolve(self, result):
        self.result = result
        self.event.set()


class Client(object):
    """一个连接到本服务的浏览器端（插件或脚本）。"""

    def __init__(self, cid, kind, meta, ws=None):
        self.cid = cid
        self.kind = kind            # extension / userscript
        self.meta = meta or {}
        self.ws = ws
        self.queue = queue.Queue()
        self.tabs = {}
        self.last_seen = time.time()
        self.connected_at = time.time()
        self.closed = False
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ 发送
    def send(self, message):
        self.last_seen = time.time()
        if self.ws is not None:
            try:
                self.ws.send_json(message)
                return True
            except Exception:
                self.closed = True
                return False
        self.queue.put(message)
        return True

    def poll(self, timeout):
        """长轮询取一条待发命令，超时返回 None。"""
        try:
            msg = self.queue.get(timeout=timeout)
            self.last_seen = time.time()
            return msg
        except queue.Empty:
            return None

    def describe(self):
        return {
            "id": self.cid,
            "kind": self.kind,
            "version": self.meta.get("version", ""),
            "browser": self.meta.get("browser", ""),
            "tabs": len(self.tabs),
            "connected_at": int(self.connected_at),
            "last_seen": int(self.last_seen),
        }


class Hub(object):
    def __init__(self, config, i18n, state):
        self.config = config
        self.i18n = i18n
        self.state = state
        self.clients = {}
        self.pending = {}
        self._tabs = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 客户端
    def add_client(self, kind, meta, ws=None, cid=None):
        cid = cid or meta.get("clientId") or ("c-" + uuid.uuid4().hex[:10])
        client = Client(cid, kind, meta, ws=ws)
        with self._lock:
            # 同 id 重连时踢掉旧连接，避免幽灵连接占着路由
            old = self.clients.get(cid)
            if old is not None:
                if old.ws is not None and old.ws is not ws:
                    try:
                        old.ws.close()
                    except Exception:
                        pass
            self.clients[cid] = client
        return client

    def remove_client(self, cid):
        with self._lock:
            client = self.clients.pop(cid, None)
            if client:
                client.closed = True
                self._drop_tabs_of(cid)
        return client

    def get_client(self, cid):
        return self.clients.get(cid)

    def primary_client(self):
        """优先返回插件客户端；没有则退回任意可用客户端。"""
        with self._lock:
            for client in self.clients.values():
                if client.closed:
                    continue
                if client.kind == "extension":
                    return client
            for client in self.clients.values():
                if not client.closed:
                    return client
        return None

    def client_list(self):
        return [c.describe() for c in self.clients.values() if not c.closed]

    # ------------------------------------------------------------------ 标签页
    def set_tabs(self, cid, tabs):
        """插件上报完整标签页列表时整体替换。"""
        with self._lock:
            client = self.clients.get(cid)
            if not client:
                return
            new_tabs = {}
            for tab in tabs or []:
                tid = tab.get("id")
                if tid is None:
                    continue
                new_tabs[tid] = self._clean_tab(tab, cid)
            client.tabs = new_tabs
            self._reindex()

    def update_tab(self, cid, tab):
        tid = tab.get("id")
        if tid is None:
            return
        with self._lock:
            client = self.clients.get(cid)
            if not client:
                return
            client.tabs[tid] = self._clean_tab(tab, cid)
            self._reindex()

    def remove_tab(self, cid, tab_id):
        with self._lock:
            client = self.clients.get(cid)
            if client and tab_id in client.tabs:
                client.tabs.pop(tab_id, None)
            self._tabs.pop(tab_id, None)

    def _clean_tab(self, tab, cid):
        return {
            "id": tab.get("id"),
            "index": tab.get("index"),
            "windowId": tab.get("windowId"),
            "title": tab.get("title") or "",
            "url": tab.get("url") or "",
            "active": bool(tab.get("active")),
            "pinned": bool(tab.get("pinned")),
            "status": tab.get("status") or "complete",
            "groupId": tab.get("groupId", -1),
            "clientId": cid,
        }

    def _drop_tabs_of(self, cid):
        self._tabs = {k: v for k, v in self._tabs.items() if v.get("clientId") != cid}

    def _reindex(self):
        merged = {}
        for client in self.clients.values():
            if client.closed:
                continue
            for tid, tab in client.tabs.items():
                merged[tid] = tab
        self._tabs = merged

    def tabs(self):
        """按窗口 + 位置排序的标签页列表。"""
        with self._lock:
            items = list(self._tabs.values())
        items.sort(key=lambda t: (t.get("windowId") or 0, t.get("index") or 0))
        out = []
        for i, tab in enumerate(items, 1):
            row = dict(tab)
            row["seq"] = i
            out.append(row)
        return out

    def active_tab(self):
        for tab in self.tabs():
            if tab.get("active"):
                return tab
        return None

    def find_tab(self, tab_id):
        for tab in self.tabs():
            if tab.get("id") == tab_id:
                return tab
        return None

    def resolve_tab_id(self, arg=None, allow_none=True):
        """把 CLI 的 `--tab` 参数解析成真实标签页 id。

        支持：None/current/active/0 -> 选中(或活动)标签页；数字 -> id，找不到再当序号；
        字符串 -> 标题/URL 模糊匹配。
        """
        tabs = self.tabs()
        if arg is None or str(arg) == "" or str(arg).lower() in ("cur", "current", "active", "0", "-1"):
            # CLI 是独立进程写状态，这里必须重新读盘才能拿到最新的选中项
            self.state.load()
            selected = self.state.get("tab_id")
            if selected is not None and any(t["id"] == selected for t in tabs):
                return selected
            active = self.active_tab()
            if active:
                return active["id"]
            if tabs:
                return tabs[0]["id"]
            if allow_none:
                return None
            raise DomateError("E_NO_TAB")

        text = str(arg)
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            num = int(text)
            ids = [t["id"] for t in tabs]
            if num in ids:
                return num
            if tabs and 1 <= num <= len(tabs):
                return tabs[num - 1]["id"]
            # 浏览器没连上时也允许直接透传 id，交给插件判定
            return num

        needle = text.lower()
        matched = [t for t in tabs
                   if needle in (t.get("title") or "").lower() or needle in (t.get("url") or "").lower()]
        if matched:
            return matched[0]["id"]
        raise DomateError("E_NO_TAB", message="no tab matches: %s" % text)

    # ------------------------------------------------------------------ 命令
    def labels(self):
        """下发给浏览器端的标题状态文案（标题栏多语言）。"""
        i18n = self.i18n
        return {
            "read": i18n.t("status.title.read"),
            "act": i18n.t("status.title.act"),
            "exec": i18n.t("status.title.exec"),
            "ok": i18n.t("status.title.ok"),
            "err": i18n.t("status.title.err"),
        }

    def route(self, action, tab):
        """决定命令发给谁、作用于哪个标签页。"""
        client = self.primary_client()
        if client is None:
            raise DomateError("E_NO_BROWSER", hint="browser")

        needs_tab = action not in TAB_GLOBAL_ACTIONS
        tab_id = None
        if needs_tab:
            tab_id = self.resolve_tab_id(tab)
            if tab_id is None:
                raise DomateError("E_NO_TAB", hint="tab")

        if client.kind != "extension" and needs_tab:
            # 非插件客户端只处理自己那个标签页
            owner = None
            for c in self.clients.values():
                if c.kind != "extension" and tab_id in c.tabs:
                    owner = c
                    break
            if owner is None:
                raise DomateError("E_NO_BROWSER", hint="browser")
            client = owner

        return client, tab_id

    def dispatch(self, action, tab=None, params=None, timeout=None, retry=None):
        """发送命令并阻塞等待浏览器端结果，返回 data。"""
        cfg = self.config
        timeout = float(timeout or cfg.get("command.timeout", 30))
        if retry is None:
            retries = cfg.get("command.retry", 1) if is_readonly(action) else 0
        else:
            retries = retry
        params = params or {}

        last_error = None
        for attempt in range(0, max(1, retries + 1)):
            client, tab_id = self.route(action, tab)
            cmd_id = "c" + uuid.uuid4().hex[:12]
            pending = Pending(cmd_id, action)
            self.pending[cmd_id] = pending

            message = {
                "type": "command",
                "id": cmd_id,
                "action": action,
                "tabId": tab_id,
                "params": params,
                "labels": self.labels(),
                "timeout": timeout,
            }
            if not client.send(message):
                self.pending.pop(cmd_id, None)
                last_error = DomateError("E_BROWSER_GONE", hint="browser")
                continue

            try:
                finished = pending.event.wait(timeout)
            finally:
                self.pending.pop(cmd_id, None)

            if not finished:
                last_error = DomateError("E_TIMEOUT", action=action, timeout=timeout, hint="timeout")
                continue  # 超时允许重试（读类命令幂等，写类命令由上层控制 retry=0）

            result = pending.result or {}
            if result.get("ok"):
                data = result.get("data")
                # tab.list 用服务端注册表回填，补上序号与选中标记
                if action == "tab.list" and isinstance(data, dict):
                    self.set_tabs(client.cid, data.get("tabs") or [])
                    self.state.load()
                    return {"tabs": self.tabs(), "selected": self.state.get("tab_id")}
                return data

            err = result.get("error") or {}
            code = err.get("code") or "E_BROWSER"
            last_error = DomateError(code, message=err.get("message"))
            if err.get("hint"):
                last_error.hint = err.get("hint")
            # 浏览器端明确报错（选择器不存在等）不重试，直接抛
            break

        raise last_error or DomateError("E_BROWSER")

    def resolve(self, cmd_id, result):
        """插件回传命令结果。"""
        pending = self.pending.get(cmd_id)
        if pending is None:
            return False
        pending.resolve(result)
        return True

    # ------------------------------------------------------------------ 反查
    def status(self):
        client = self.primary_client()
        self.state.load()
        return {
            "server": {
                "version": self.i18n.meta().get("version", ""),
                "uptime": 0,
            },
            "browser": {
                "connected": client is not None,
                "kind": client.kind if client else None,
                "clientId": client.cid if client else None,
                "version": client.meta.get("version") if client else None,
                "browser": client.meta.get("browser") if client else None,
                "lastSeen": int(client.last_seen) if client else None,
            },
            "clients": self.client_list(),
            "tabs": self.tabs(),
            "selected": self.state.get("tab_id"),
        }

    def dump(self):
        return json.dumps(self.status(), ensure_ascii=False)
