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
    def __init__(self, sock, rfile, wfile):
        self.sock = sock
        self.rfile = rfile
        self.wfile = wfile
        self.closed = False

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
    def _read_exact(self, size, timeout=None):
        if timeout is not None:
            self.sock.settimeout(timeout)
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = self.rfile.read(remaining)
            if not chunk:
                raise WSError("connection closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_frame(self, timeout=None):
        """读一个完整帧，返回 (fin, opcode, payload)。"""
        header = self._read_exact(2, timeout)
        b1, b2 = header[0], header[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > MAX_PAYLOAD:
            raise WSError("frame too large: %d" % length)
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = _unmask(payload, mask)
        return fin, opcode, payload

    def recv_text(self, timeout=None):
        """读取一条完整文本消息（自动重组分片，处理 ping/close）。

        返回 str；对端正常关闭返回 None；超时抛 socket.timeout。
        """
        buffer = b""
        first_opcode = None
        while True:
            fin, opcode, payload = self._read_frame(timeout)
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
