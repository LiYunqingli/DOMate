# -*- coding: utf-8 -*-
"""极简 WebSocket 服务端实现（RFC 6455 子集，零第三方依赖）。

只实现本项目需要的能力：
- 握手（Sec-WebSocket-Accept 校验）
- 文本帧收发、分片重组、ping/pong、close
- 支持 64 位长度（截图 base64 会超过 64KB）

为什么不直接用 websockets/wsproto：项目要求跨平台开箱即用，
不想让使用者先 pip 装依赖，所以这里用标准库 socket 自己实现。
"""

import base64
import hashlib
import json
import socket
import struct
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_PAYLOAD = 64 * 1024 * 1024  # 单帧上限 64MB，防内存打爆


class WSError(Exception):
    pass


def accept_key(client_key):
    """根据客户端 Sec-WebSocket-Key 计算握手应答。"""
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class WSConnection(object):
    def __init__(self, sock, rfile=None, wfile=None):
        self.sock = sock
        self.wfile = wfile or sock.makefile("wb")
        self.closed = False
        # 自己的读缓冲区：不借用 http.server 的 rfile。
        # rfile 是缓冲读取器，socket 一旦超时它就会永久失效
        # （OSError: cannot read from timed out object），而连接空闲时的
        # 保活探测必然要超时——用它读会把健康连接读死。
        self._rbuf = bytearray()
        self._wlock = _new_lock()
        # 最近一次收到任何字节的时间：用于判断链路是否还活着。
        # 只看「有没有收到 JSON 消息」会误判：MV3 的 service worker 被
        # 节流时 pong 会晚到，但 TCP 其实一直是通的。
        self.last_rx = time.time()
        if rfile is not None:
            self._adopt(rfile)

    def _adopt(self, rfile):
        """接管 http 层可能已经读进缓冲区的字节。

        升级请求的头部由 http.server 缓冲读入；如果客户端把第一帧紧跟在
        握手后面发来，那部分字节已经在 rfile 的缓冲区里，直接读裸 socket
        会丢掉它们。这里用 5ms 的短超时把缓冲区抽干——缓冲区为空时这次
        读取会立刻超时返回，相当于一次可忽略的探测。
        """
        try:
            self.sock.settimeout(0.005)
            while True:
                chunk = rfile.read1(65536)
                if not chunk:
                    break
                self._rbuf += chunk
        except Exception:
            # 缓冲区已空（超时）或连接已结束，都属正常
            pass
        finally:
            try:
                self.sock.settimeout(None)
            except Exception:
                pass

    # ------------------------------------------------------------------ 握手
    def handshake(self, headers):
        key = headers.get("Sec-WebSocket-Key")
        if not key:
            raise WSError("missing Sec-WebSocket-Key")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept_key(key)
        )
        self.wfile.write(response.encode("ascii"))
        self.wfile.flush()

    # ------------------------------------------------------------------ 读取
    def _read_exact(self, size, deadline=None, sink=None):
        """读满 size 字节；deadline 是绝对时间戳，到点抛 socket.timeout。

        sink 会累积本次调用实际从缓冲区取走的字节，用于超时后回滚。
        """
        while len(self._rbuf) < size:
            timeout = None
            if deadline is not None:
                timeout = deadline - time.time()
                if timeout <= 0:
                    raise socket.timeout("ws read timeout")
            self.sock.settimeout(timeout)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("connection closed")
            self._rbuf += chunk
            self.last_rx = time.time()
        data = bytes(self._rbuf[:size])
        del self._rbuf[:size]
        if sink is not None:
            sink += data
        return data

    def _read_frame(self, deadline=None, sink=None):
        """读一个完整帧，返回 (fin, opcode, payload)。"""
        header = self._read_exact(2, deadline, sink)
        b1, b2 = header[0], header[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2, deadline, sink))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8, deadline, sink))[0]
        if length > MAX_PAYLOAD:
            raise WSError("frame too large: %d" % length)
        mask = self._read_exact(4, deadline, sink) if masked else None
        payload = self._read_exact(length, deadline, sink) if length else b""
        if mask:
            payload = _unmask(payload, mask)
        return fin, opcode, payload

    def recv_text(self, timeout=None):
        """读取一条完整文本消息（自动重组分片，处理 ping/close）。

        返回 str；对端正常关闭返回 None；超时抛 socket.timeout。
        超时的计时覆盖整条消息，中途收到 ping 不会把等待时间续上。

        超时是可重入的：本次已解析出的字节会塞回缓冲区头部，调用方下次
        重试时从帧边界重新解析，不会把半个帧当成新消息的开头。
        """
        deadline = None if timeout is None else time.time() + max(0.0, timeout)
        buffer = b""
        first_opcode = None
        taken = bytearray()          # 本次调用已从流里取走的字节
        while True:
            try:
                fin, opcode, payload = self._read_frame(deadline, taken)
            except socket.timeout:
                if taken:
                    self._rbuf[0:0] = taken
                raise
            if opcode == OP_PING:
                self.send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self.send_frame(OP_CLOSE, payload[:2])
                except Exception:
                    pass
                self.closed = True
                return None
            if opcode in (OP_TEXT, OP_BINARY):
                first_opcode = opcode
                buffer = payload
            elif opcode == OP_CONT:
                buffer += payload
            else:
                continue
            if fin:
                if first_opcode == OP_BINARY:
                    return buffer
                try:
                    return buffer.decode("utf-8")
                except UnicodeDecodeError:
                    return buffer.decode("utf-8", "replace")

    def recv_json(self, timeout=None):
        raw = self.recv_text(timeout)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raise WSError("unexpected binary frame")
        return json.loads(raw)

    # ------------------------------------------------------------------ 发送
    def send_frame(self, opcode, payload=b""):
        if self.closed:
            raise WSError("connection closed")
        length = len(payload)
        header = bytearray()
        header.append(0x80 | opcode)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        with _send_lock(self):
            self.wfile.write(bytes(header) + payload)
            self.wfile.flush()

    def send_text(self, text):
        self.send_frame(OP_TEXT, text.encode("utf-8"))

    def send_json(self, obj):
        self.send_text(json.dumps(obj, ensure_ascii=False))

    def ping(self):
        self.send_frame(OP_PING, b"domate")

    def close(self, code=1000):
        if self.closed:
            return
        try:
            self.send_frame(OP_CLOSE, struct.pack("!H", code))
        except Exception:
            pass
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class _send_lock(object):
    """每个连接一把写锁，避免多线程同时写导致帧交错。"""

    _guard = None

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        lock = getattr(self.conn, "_wlock", None)
        if lock is None:
            lock = _new_lock()
            self.conn._wlock = lock
        lock.acquire()
        return self

    def __exit__(self, *exc):
        self.conn._wlock.release()
        return False


def _new_lock():
    import threading
    return threading.Lock()


def _unmask(payload, mask):
    if not payload:
        return payload
    data = bytearray(payload)
    for i in range(len(data)):
        data[i] ^= mask[i & 3]
    return bytes(data)
