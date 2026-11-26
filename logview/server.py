# -*- coding: utf-8 -*-
"""日志看板的本地服务：静态页面 + 聚合 API + SSE 实时推送。

零第三方依赖（和项目其它部分一致），只绑 127.0.0.1。

端点：
    GET /                        看板页面
    GET /static/<file>           JS / CSS
    GET /api/meta                日志文件、窗口、版本等元信息
    GET /api/snapshot?lv=&cat=&q=&from=&to=&poll=1&fail=1
                                 一次性全量聚合（KPI/时间线/分布/表格）
    GET /api/events?offset=&limit=
                                 分页拉取原始事件（表格「加载更多」用）
    GET /api/stream?<同 snapshot> SSE：有新增日志就推一份新快照
    GET /api/export.csv?<同 snapshot>  导出当前筛选结果

实时性来自一个后台 watcher：它独占日志文件偏移量，发现新行就把版本号 +1，
所有 SSE 连接看到版本变化后各自按自己的筛选条件重算并推送。
"""

import csv
import io
import json
import os
import select
import socket
import threading
import time
import traceback
import webbrowser

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import parser as logparser

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class Watcher(threading.Thread):
    """唯一的日志读取者。谁也别抢它的文件偏移。"""

    def __init__(self, store, interval=0.35):
        threading.Thread.__init__(self, name="log-watcher", daemon=True)
        self.store = store
        self.interval = interval
        self.version = 0
        self.event = threading.Event()
        self._stop = threading.Event()
        self.started_at = time.time()
        self.last_change = 0.0
        self.new_count = 0

    def run(self):
        while not self._stop.is_set():
            try:
                fresh = self.store.poll()
            except Exception:  # noqa: BLE001 - 看门狗线程不能死
                fresh = []
            if fresh:
                self.version += 1
                self.new_count += len(fresh)
                self.last_change = time.time()
                self.event.set()
                self.event.clear()
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DOMateLogview/1.0"

    # ---------------------------------------------------------------- 基础
    def log_message(self, fmt, *args):
        pass  # 不要把看板自己的请求写进被看的日志里

    @property
    def store(self):
        return self.server.store

    @property
    def watcher(self):
        return self.server.watcher

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.close_connection = True

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def _filters(self, query):
        return logparser.build_filters(query)

    def _snapshot(self, filters):
        records = self.store.snapshot()
        data = logparser.aggregate(records, filters)
        data["meta"] = self._meta()
        return data

    def _meta(self):
        span = self.store.span()
        return {
            "version": self.watcher.version,
            "restarted": round(self.watcher.started_at, 1),
            "parsed": self.store.parsed,
            "bad_lines": self.store.bad_lines,
            "in_memory": len(self.store.records),
            "window": self.store.window,
            "files": self.store.files(),
            "span": span,
            "subs": self.server.subs,
            "now": round(time.time(), 3),
        }

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/meta":
                return self._json(self._meta())
            if path == "/api/snapshot":
                return self._json(self._snapshot(self._filters(query)))
            if path == "/api/events":
                return self._events(query)
            if path == "/api/export.csv":
                return self._export(query)
            if path == "/api/stream":
                return self._stream(query)
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": str(exc)}, 500)
        return self._json({"error": "not found"}, 404)

    # ---------------------------------------------------------------- 静态
    def _static(self, name):
        safe = os.path.normpath(name).replace("\\", "/")
        if safe.startswith("..") or os.path.isabs(safe):
            return self._json({"error": "bad path"}, 400)
        path = os.path.join(WEB_DIR, safe)
        if not os.path.isfile(path):
            return self._json({"error": "not found: %s" % safe}, 404)
        with open(path, "rb") as fp:
            body = fp.read()
        ctype = MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        # HTML/JS 不缓存，改完刷新就能看到
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    # ---------------------------------------------------------------- 事件表
    def _events(self, query):
        filters = self._filters(query)
        try:
            offset = max(0, int((query.get("offset") or ["0"])[0]))
            limit = min(1000, max(1, int((query.get("limit") or ["200"])[0])))
        except ValueError:
            offset, limit = 0, 200
        rows = self.store.snapshot()
        matched = [r for r in rows if logparser.match(r, filters)]
        total = len(matched)
        matched.reverse()
        page = matched[offset:offset + limit]
        return self._json({"total": total, "offset": offset, "limit": limit,
                           "items": [logparser._slim(r) for r in page]})

    # ---------------------------------------------------------------- 导出
    def _export(self, query):
        filters = self._filters(query)
        rows = [r for r in self.store.snapshot() if logparser.match(r, filters)]
        cols = ["t", "lv", "cat", "ev", "src", "action", "ok", "dur_ms", "code",
                "tab", "tab_title", "cid", "status", "path", "exit", "msg"]
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(cols)
        for rec in rows[-20000:]:
            writer.writerow([rec.get(c, "") for c in cols])
        body = "\ufeff" + buf.getvalue()  # BOM：Excel 打开中文不乱码
        fname = "domate-log-%s.csv" % time.strftime("%Y%m%d-%H%M%S")
        try:
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % fname)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    # ---------------------------------------------------------------- SSE
    def _stream(self, query):
        filters = self._filters(query)
        try:
            idle_tick = float((query.get("tick") or ["8"])[0] or 8)
        except (TypeError, ValueError):
            idle_tick = 8.0
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # 立刻给一行注释：让浏览器确认流已建立，别等第一份快照
            self.wfile.write(b"retry: 2000\n: connected\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
            return
        self.close_connection = True

        def emit(obj, event=None):
            payload = json.dumps(obj, ensure_ascii=False, default=str)
            chunk = ("event: %s\n" % event if event else "") + "data: " + payload + "\n\n"
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

        def peer_gone():
            """客户端关页面时 write 往往不报错（对端进入半开），必须主动探一下。"""
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                if not readable:
                    return False
                return self.connection.recv(1, socket.MSG_PEEK) == b""
            except (OSError, ValueError):
                return True

        last_version = -1
        last_beat = 0.0   # 心跳（ping）时间
        last_push = 0.0   # 上一份快照时间
        self.server.subs_add()
        try:
            while not self.server.stopping:
                if peer_gone():
                    return
                version = self.watcher.version
                now = time.time()
                # 有新日志就推；没新日志也按 tick 重算一遍，
                # 否则「近 60 秒」「更新于」在空闲时会一直停在旧值，看起来像卡死。
                if version != last_version or (idle_tick > 0 and now - last_push >= idle_tick):
                    last_version = version
                    started = time.time()
                    snap = self._snapshot(filters)
                    if time.time() - started > 0.6:
                        # 日志量很大时别把 CPU 全给聚合
                        time.sleep(0.2)
                    emit(snap, "snapshot")
                    last_push = last_beat = time.time()
                else:
                    self.watcher.event.wait(0.5)
                    if time.time() - last_beat > 12:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_beat = time.time()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return
        finally:
            self.server.subs_drop()


class LogviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, store, watcher):
        ThreadingHTTPServer.__init__(self, addr, Handler)
        self.store = store
        self.watcher = watcher
        self.stopping = False
        self.started_at = time.time()
        self.subs = 0
        self._subs_lock = threading.Lock()

    def subs_add(self):
        with self._subs_lock:
            self.subs += 1
            return self.subs

    def subs_drop(self):
        with self._subs_lock:
            self.subs = max(0, self.subs - 1)
            return self.subs

    def stop(self):
        self.stopping = True
        try:
            self.shutdown()
        except Exception:
            pass


def build_store(config, window=None):
    json_path = config.abs_path(config.get("server.log_json", "logs/domate.jsonl"))
    text_path = config.abs_path(config.get("server.log_file", "logs/domate.log"))
    size = window or int(config.get("dashboard.window", 30000) or 30000)
    store = logparser.LogStore(json_path, text_path, window=size)
    store.load()
    return store


def serve(config, host=None, port=None, window=None, open_browser=False, quiet=False):
    host = host or config.get("dashboard.host", "127.0.0.1")
    port = int(port or config.get("dashboard.port", 8899))
    store = build_store(config, window)
    watcher = Watcher(store)
    watcher.start()
    try:
        server = LogviewServer((host, port), store, watcher)
    except OSError as exc:
        watcher.stop()
        raise SystemExit("端口 %s 不可用：%s（换个端口：log serve --port 8900）" % (port, exc))

    url = "http://%s:%s/" % (host, port)
    if not quiet:
        print("DOMate 日志看板  %s" % url)
        print("  已载入 %d 条事件（窗口上限 %d），日志文件：" % (len(store.records), store.window))
        for info in store.files():
            print("    %-10s %s  %s" % (info["kind"], _human(info["size"]),
                                        info["path"] if info["exists"] else info["path"] + "（不存在）"))
        if store.bad_lines:
            print("  ! 有 %d 行无法解析，已跳过" % store.bad_lines)
        print("  Ctrl+C 退出")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        watcher.stop()
        print("\n看板已停止")
    return 0


def _human(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f%s" % (size, unit)
        size /= 1024.0
    return "%.1fGB" % size


if __name__ == "__main__":
    from lib.config import Config
    serve(Config())
