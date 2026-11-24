# -*- coding: utf-8 -*-
"""本地服务（daemon）：HTTP + WebSocket 双传输。

端点一览：
    GET  /health          健康检查（CLI 用它判断服务是否已启动）
    GET  /status          同 health，附带连接与标签页快照
    GET  /tabs            标签页列表
    POST /rpc             CLI 下发命令并同步等待结果
    POST /shutdown        关闭服务
    GET  /ws              WebSocket 升级端点（插件主通道）
    POST /register        HTTP 客户端注册（长轮询备用通道）
    GET  /poll            长轮询取命令
    POST /result          回传命令结果

安全：默认只绑定 127.0.0.1；config.server.token 非空时，所有写操作必须带
请求头 X-DOMate-Token，防止本机其它程序随意驱动浏览器。
"""

import json
import os
import socket
import threading
import time
import traceback

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .errors import DomateError
from .hub import Hub
from .ws import WSConnection, WSError

SERVER_NAME = "DOMate"
SERVER_VERSION = "1.0.0"

#: 服务端给插件的保活间隔（秒）。
#: Chrome 116 起，Service Worker 里的 WebSocket 收发消息都会重置 30 秒空闲计时，
#: 所以这里既发协议层 ping，也发一条 JSON ping，双向都有流量，
#: 插件的工作线程就不会被浏览器回收，也就不会出现「过一会儿要重连」。
PING_INTERVAL = 15
MAX_IDLE_PINGS = 4


class _Logger(object):
    def __init__(self, path, level="info"):
        self.path = path
        self.level = level
        self.lock = threading.Lock()
        if path:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            except OSError:
                self.path = None

    def _write(self, level, text):
        if self.level == "error" and level != "error":
            return
        line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level.upper(), text)
        if self.path:
            with self.lock:
                try:
                    with open(self.path, "a", encoding="utf-8") as fp:
                        fp.write(line + "\n")
                except OSError:
                    pass
        else:
            try:
                print(line)
            except Exception:
                pass

    def info(self, text):
        self._write("info", text)

    def warn(self, text):
        self._write("warn", text)

    def error(self, text):
        self._write("error", text)


class DomateServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 的 SO_REUSEADDR 语义和 POSIX 完全不同：它允许别的进程再绑同一个
    # 端口，两个 daemon 抢端口会让插件连接在两者之间来回掉。所以 Windows 上
    # 关掉它，改用 SO_EXCLUSIVEADDRUSE 独占（见 server_bind）。
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except (AttributeError, OSError):
                pass
        ThreadingHTTPServer.server_bind(self)

    def __init__(self, addr, handler, hub, logger, token=""):
        ThreadingHTTPServer.__init__(self, addr, handler)
        self.hub = hub
        self.log = logger
        self.token = token or ""
        self.keepalive = PING_INTERVAL
        # 多久收不到任何字节就判定链路已死。取保活间隔的 3 倍且不低于 30 秒，
        # 给 MV3 节流留足余量；配置里可显式覆盖。
        self.silence_timeout = PING_INTERVAL * MAX_IDLE_PINGS
        self.started_at = time.time()
        self.last_activity = time.time()
        self._stop = threading.Event()

    def touch(self):
        self.last_activity = time.time()

    def stop(self):
        self._stop.set()
        self.shutdown()

    @property
    def stopping(self):
        return self._stop.is_set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "%s/%s" % (SERVER_NAME, SERVER_VERSION)

    # ------------------------------------------------------------------ 工具
    @property
    def hub(self):
        return self.server.hub

    def log_message(self, fmt, *args):
        # 屏蔽 http.server 默认的 stderr 日志，统一走自己的 logger
        if self.server.log:
            self.server.log.info("%s %s" % (self.address_string(), fmt % args))

    def handle_one_request(self):
        """客户端半路断开是常态（Ctrl+C、超时放弃），不要让它冒泡成堆栈。"""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.close_connection = True

    def _auth_ok(self):
        token = self.server.token
        if not token:
            return True
        if self.headers.get("X-DOMate-Token", "") == token:
            return True
        # 浏览器的 WebSocket 构造器不支持自定义头，退化为查询参数
        query = parse_qs(urlparse(self.path).query)
        return (query.get("token") or [""])[0] == token

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # 客户端提前退出（Ctrl+C / 超时放弃）时不要抛出，否则会在日志里刷堆栈
            self.close_connection = True

    def _send_empty(self, code=204):
        try:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.close_connection = True

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise DomateError("E_REQUEST", message="请求体不是合法 JSON")

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        self.server.touch()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/ws":
            return self._handle_ws(parsed)

        if path in ("/", "/health", "/status"):
            if not self._auth_ok():
                return self._send_json({"ok": False, "error": "unauthorized"}, 401)
            status = self.hub.status()
            status["ok"] = True
            status["server"] = {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "uptime": int(time.time() - self.server.started_at),
                "port": self.server.server_address[1],
                "pid": os.getpid(),
            }
            return self._send_json(status)

        if path == "/tabs":
            if not self._auth_ok():
                return self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return self._send_json({"ok": True, "data": self.hub.tabs()})

        if path == "/poll":
            return self._handle_poll(parsed)

        return self._send_json({"ok": False, "error": "not found"}, 404)

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        self.server.touch()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if not self._auth_ok():
            return self._send_json({"ok": False, "error": "unauthorized"}, 401)

        try:
            if path == "/rpc":
                return self._handle_rpc()
            if path == "/register":
                return self._handle_register()
            if path == "/result":
                return self._handle_result()
            if path == "/shutdown":
                self._send_json({"ok": True})
                self.server.log.info("shutdown requested by %s" % self.address_string())
                threading.Thread(target=self.server.stop, daemon=True).start()
                return
        except DomateError as exc:
            return self._send_json({"ok": False, "error": {"code": exc.code, "message": str(exc)}}, 200)
        except Exception as exc:  # noqa: BLE001
            self.server.log.error("POST %s failed: %s\n%s" % (path, exc, traceback.format_exc()))
            return self._send_json({"ok": False, "error": {"code": "E_INTERNAL", "message": str(exc)}}, 500)

        return self._send_json({"ok": False, "error": "not found"}, 404)

    # ------------------------------------------------------------------ 业务
    def _handle_rpc(self):
        payload = self._read_json()
        action = payload.get("action")
        if not action:
            raise DomateError("E_REQUEST", message="缺少 action")
        timeout = payload.get("timeout")
        try:
            data = self.hub.dispatch(
                action,
                tab=payload.get("tab"),
                params=payload.get("params") or {},
                timeout=timeout,
                retry=payload.get("retry"),
            )
            return self._send_json({"ok": True, "data": data})
        except DomateError as exc:
            return self._send_json({
                "ok": False,
                "error": {"code": exc.code, "message": exc.message(self.hub.i18n)},
            }, 200)

    def _handle_register(self):
        payload = self._read_json()
        kind = payload.get("kind") or "userscript"
        client = self.hub.add_client(kind, payload, cid=payload.get("clientId"))
        tid = (payload.get("tab") or {}).get("id")
        if tid is not None:
            self.hub.update_tab(client.cid, payload.get("tab"))
        self.server.log.info("client registered: %s (%s)" % (client.cid, kind))
        return self._send_json({
            "ok": True,
            "data": {
                "clientId": client.cid,
                "labels": self.hub.labels(),
                "server": {"version": SERVER_VERSION, "port": self.server.server_address[1]},
            },
        })

    def _handle_poll(self, parsed):
        qs = parse_qs(parsed.query)
        cid = (qs.get("cid") or [""])[0]
        client = self.hub.get_client(cid)
        if client is None:
            return self._send_json({"ok": False, "error": "unknown client"}, 404)
        try:
            timeout = float((qs.get("timeout") or ["25"])[0])
        except ValueError:
            timeout = 25.0
        msg = client.poll(min(timeout, 60))
        if msg is None:
            return self._send_empty(204)
        return self._send_json({"ok": True, "data": msg})

    def _handle_result(self):
        payload = self._read_json()
        cid = payload.get("cid")
        if cid:
            client = self.hub.get_client(cid)
            if client:
                client.last_seen = time.time()
        self.hub.resolve(payload.get("id"), payload)
        return self._send_json({"ok": True})

    # ------------------------------------------------------------------ WS
    def _handle_ws(self, parsed):
        if not self._auth_ok():
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if upgrade != "websocket":
            return self._send_json({"ok": False, "error": "expect websocket upgrade"}, 400)

        conn = WSConnection(self.connection, self.rfile, self.wfile)
        try:
            conn.handshake(self.headers)
        except WSError as exc:
            self.server.log.warn("ws handshake failed: %s" % exc)
            self.close_connection = True
            return

        client = None
        self.close_connection = True
        try:
            # 第一条消息必须是 hello，用于注册身份与标签页快照
            hello = conn.recv_json(timeout=15)
            if hello is None:
                return
            if hello.get("type") != "hello":
                conn.send_json({"type": "error", "error": {"code": "E_PROTOCOL", "message": "expect hello"}})
                return

            client = self.hub.add_client(
                hello.get("kind") or "extension",
                hello,
                ws=conn,
                cid=hello.get("clientId"),
            )
            if hello.get("tabs"):
                self.hub.set_tabs(client.cid, hello.get("tabs"))
            self.server.log.info("ws connected: %s (%s, %d tabs, 累计重连 %d 次)" % (
                client.cid, client.kind, len(client.tabs), self.hub.reconnects))

            conn.send_json({
                "type": "welcome",
                "clientId": client.cid,
                "labels": self.hub.labels(),
                "server": {"version": SERVER_VERSION, "port": self.server.server_address[1]},
            })
            self._ws_loop(conn, client)
        except (WSError, socket.timeout, OSError) as exc:
            self.server.log.info("ws closed: %s" % exc)
        except Exception as exc:  # noqa: BLE001
            self.server.log.error("ws error: %s\n%s" % (exc, traceback.format_exc()))
        finally:
            if client is not None:
                self.hub.remove_client(client.cid)
                self.server.log.info("ws disconnected: %s" % client.cid)
            try:
                conn.close()
            except Exception:
                pass

    def _ws_loop(self, conn, client):
        interval = max(5, int(self.server.keepalive or PING_INTERVAL))
        limit = self.server.silence_timeout
        while not self.server.stopping:
            try:
                msg = conn.recv_json(timeout=interval)
            except socket.timeout:
                # 读超时只说明「这一轮没消息」，不等于对端死了：
                # 插件被 MV3 节流时 pong 会晚到几十秒，TCP 可能一直是通的。
                # 真正的判据是持续多久没收到任何字节。
                silent = time.time() - conn.last_rx
                if silent > limit:
                    self.server.log.warn("ws silent %.0fs, dropping %s" % (silent, client.cid))
                    return
                try:
                    # 协议层 ping 用于探活，JSON ping 用于让插件侧的 SW 计时器复位
                    conn.ping()
                    conn.send_json({"type": "ping", "t": int(time.time() * 1000)})
                except Exception:
                    return
                continue
            except WSError:
                return
            if msg is None:
                return
            client.last_seen = time.time()
            self._ws_message(conn, client, msg)

    def _ws_message(self, conn, client, msg):
        mtype = msg.get("type")
        if mtype == "result":
            self.hub.resolve(msg.get("id"), msg)
        elif mtype == "tabs":
            self.hub.set_tabs(client.cid, msg.get("tabs") or [])
        elif mtype == "tab_update":
            self.hub.update_tab(client.cid, msg.get("tab") or {})
        elif mtype == "tab_removed":
            self.hub.remove_tab(client.cid, msg.get("tabId"))
        elif mtype == "ping":
            try:
                conn.send_json({"type": "pong", "t": msg.get("t")})
            except Exception:
                pass
        elif mtype == "pong":
            # 插件的保活回应，收到即说明链路活着（last_seen 已在循环里刷新）
            pass
        elif mtype == "log":
            self.server.log.info("[browser] %s" % msg.get("text", ""))
        elif mtype == "hello":
            # 重复 hello 视为重连信息更新
            if msg.get("tabs"):
                self.hub.set_tabs(client.cid, msg.get("tabs"))
        else:
            self.server.log.warn("unknown ws message type: %s" % mtype)


def create_server(config, i18n, state):
    """按配置创建服务实例（不启动）。"""
    hub = Hub(config, i18n, state)
    log_path = config.get("server.log_file", "")
    logger = _Logger(config.abs_path(log_path) if log_path else "", config.get("server.log_level", "info"))
    host = config.host
    port = config.port
    try:
        server = DomateServer((host, port), Handler, hub, logger, token=config.token)
    except OSError as exc:
        raise DomateError("E_PORT", port=port, error=str(exc), hint="port")
    try:
        server.keepalive = int(config.get("server.keepalive_seconds", PING_INTERVAL) or PING_INTERVAL)
    except (TypeError, ValueError):
        server.keepalive = PING_INTERVAL
    # 静默判死窗口：默认跟着保活间隔走，最小 30 秒；配置为非 0 则用配置值
    try:
        silence = int(config.get("server.silence_seconds", 0) or 0)
    except (TypeError, ValueError):
        silence = 0
    server.silence_timeout = silence if silence > 0 else max(server.keepalive * MAX_IDLE_PINGS, 30)
    return server


def run(config, i18n, state, foreground=True):
    """启动服务并阻塞，直到收到 /shutdown 或 Ctrl+C。"""
    server = create_server(config, i18n, state)
    host, port = server.server_address[:2]
    server.log.info("%s %s listening on %s:%s" % (SERVER_NAME, SERVER_VERSION, host, port))
    if foreground:
        print("%s %s  http://%s:%s  (Ctrl+C 退出)" % (SERVER_NAME, SERVER_VERSION, host, port))
        print("WebSocket: ws://%s:%s/ws" % (host, port))

    _start_idle_watchdog(server, config)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.log.info("server stopped")
        try:
            server.server_close()
        except Exception:
            pass
    return 0


def _start_idle_watchdog(server, config):
    """空闲自动退出：长时间没有浏览器连接、也没有 CLI 请求时关闭自己。

    config.server.idle_exit_minutes = 0 表示永不自动退出（默认）。
    """
    try:
        minutes = float(config.get("server.idle_exit_minutes", 0) or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return

    def watch():
        limit = minutes * 60
        while not server.stopping:
            time.sleep(10)
            if server.hub.clients:
                server.touch()
                continue
            if time.time() - server.last_activity > limit:
                server.log.info("idle for %.0fs, shutting down" % (time.time() - server.last_activity))
                server.stop()
                return

    threading.Thread(target=watch, daemon=True).start()
