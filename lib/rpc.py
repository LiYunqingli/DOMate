# -*- coding: utf-8 -*-
"""CLI 侧通讯层：健康检查、自动拉起后台服务、RPC 调用。

CLI 每次执行都是独立进程，所以这里做了两件事：
1. 服务没起就自动 spawn 一个脱离终端的后台进程（跨平台）；
2. 用 HTTP 把命令 POST 给服务，同步拿回浏览器端结果。
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import logbook
from .config import ROOT
from .errors import DaemonError, DomateError

_IS_WIN = os.name == "nt"


def _log(config, level, text, **fields):
    """CLI 侧的辅助事件（拉起服务等）。失败绝不影响主流程。"""
    try:
        logbook.get(config, source="cli").event(level, "sys", "spawn", text, **fields)
    except Exception:
        pass


class Rpc(object):
    def __init__(self, config, i18n, notify=None):
        self.config = config
        self.i18n = i18n
        self.base = config.base_url
        #: 过程提示回调（往 stderr 写一行），用于「正在等插件重连」这类说明
        self.notify = notify

    # ------------------------------------------------------------------ 基础
    def _headers(self):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.config.token:
            headers["X-DOMate-Token"] = self.config.token
        return headers

    def _request(self, path, payload=None, method=None, timeout=10):
        url = self.base + path
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(),
                                     method=method or ("POST" if data else "GET"))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def health(self, timeout=1.5):
        """服务在跑就返回状态 dict，否则 None。"""
        try:
            data = self._request("/health", timeout=timeout)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("server", {}).get("name") not in ("DOMate", None):
            return None
        return data

    # ------------------------------------------------------------------ 启动
    def ensure(self, autostart=None, verbose=False):
        """确保服务可用；必要时后台拉起。返回状态 dict。"""
        status = self.health()
        if status:
            return status
        if autostart is None:
            autostart = bool(self.config.get("server.auto_start", True))
        if not autostart:
            raise DaemonError("E_DAEMON_DOWN", hint="daemon")

        self._spawn(verbose=verbose)
        deadline = time.time() + 12
        while time.time() < deadline:
            status = self.health(timeout=1.0)
            if status:
                return status
            time.sleep(0.15)
        raise DaemonError("E_DAEMON_START", port=self.config.port, hint="daemon")

    def _spawn(self, verbose=False):
        """以脱离终端的方式启动 serve 子命令。"""
        main_py = os.path.join(ROOT, "main.py")
        # 显式带上端口，避免后台进程读 config.json 时和 CLI 的 --port 不一致
        cmd = [sys.executable, main_py, "serve", "--daemon", "--port", str(self.config.port)]
        log_path = self.config.abs_path(self.config.get("server.log_file", "logs/domate.log"))
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            log_fp = open(log_path, "a", encoding="utf-8")
        except OSError:
            log_fp = open(os.devnull, "w")

        kwargs = {
            "cwd": ROOT,
            "stdin": subprocess.DEVNULL,
            "stdout": log_fp,
            "stderr": log_fp,
            "close_fds": True,
        }
        if _IS_WIN:
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：关掉终端也不影响服务
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        # 语言/环境透传，避免后台进程拿到不同的 sys.path
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs["env"] = env
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            _log(self.config, "warn", "spawn failed: %s" % exc, port=self.config.port, error=str(exc))
            raise DaemonError("E_DAEMON_START", port=self.config.port, error=str(exc), hint="daemon")
        _log(self.config, "info", "daemon spawned (pid=%s)" % proc.pid,
             port=self.config.port, pid=proc.pid, log=log_path)
        if verbose:
            sys.stderr.write("[domate] daemon spawned, log: %s\n" % log_path)

    # ------------------------------------------------------------------ 调用
    def call(self, action, params=None, tab=None, timeout=None, retry=None):
        """下发一条命令，返回浏览器端 data。"""
        status = self.ensure()
        # 插件不在线时，服务端会等一个重连宽限期。这里先说明一句，
        # 免得用户盯着没有输出的终端以为卡死了。
        if self.notify is not None and not ((status or {}).get("browser") or {}).get("connected"):
            budget = self.config.get("command.connect_wait", 3.5)
            if float(budget or 0) > 0:
                self.notify(self.i18n.t("msg.waiting_browser", timeout=budget))
        payload = {
            "action": action,
            "params": params or {},
            "tab": tab,
        }
        if timeout is not None:
            payload["timeout"] = timeout
        if retry is not None:
            payload["retry"] = retry

        # HTTP 读超时要大于服务端的等待超时，否则 CLI 先断
        http_timeout = float(timeout or self.config.get("command.timeout", 30)) + 15
        try:
            resp = self._request("/rpc", payload, timeout=http_timeout)
        except urllib.error.HTTPError as exc:
            raise DaemonError("E_RPC", message="HTTP %s: %s" % (exc.code, exc.reason), hint="daemon")
        except (urllib.error.URLError, OSError) as exc:
            raise DaemonError("E_DAEMON_DOWN", error=str(exc), hint="daemon")
        except ValueError as exc:
            raise DaemonError("E_RPC", message="响应解析失败: %s" % exc, hint="daemon")

        if not resp:
            raise DaemonError("E_RPC", message="空响应", hint="daemon")
        if resp.get("ok"):
            return resp.get("data")

        err = resp.get("error") or {}
        code = err.get("code") or "E_BROWSER"
        raise DomateError(code, message=err.get("message"))

    def raw(self, method, path, payload=None, timeout=10):
        """底层请求，供 status/config 之类的管理命令使用。"""
        try:
            return self._request(path, payload, method=method, timeout=timeout)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def stop(self):
        try:
            self._request("/shutdown", {}, timeout=3)
            return True
        except Exception:
            return False
