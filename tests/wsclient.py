# -*- coding: utf-8 -*-
"""测试用的极简 WebSocket 客户端（带掩码，模拟浏览器端行为）。"""

import base64
import json
import os
import socket
import struct
from urllib.parse import urlparse


class WSClient(object):
    def __init__(self, url, timeout=10):
        parsed = urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ) % (path, parsed.hostname, parsed.port, key)
        self.sock.sendall(request.encode("ascii"))

        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("handshake failed")
            raw += chunk
        head, _, rest = raw.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError("handshake rejected: %s" % head[:200])
        self.buf = rest

    # ------------------------------------------------------------------ 读
    def _read(self, size, timeout=10):
        self.sock.settimeout(timeout)
        while len(self.buf) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("connection closed")
            self.buf += chunk
        data, self.buf = self.buf[:size], self.buf[size:]
        return data

    def recv(self, timeout=10):
        b1, b2 = self._read(2, timeout)
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8))[0]
        payload = self._read(length) if length else b""
        if opcode == 0x9:  # ping
            self._send_frame(0xA, payload)
            return self.recv(timeout)
        if opcode == 0x8:
            raise RuntimeError("closed by server")
        return payload.decode("utf-8")

    def recv_json(self, timeout=10):
        raw = self.recv(timeout)
        return json.loads(raw) if raw else None

    # ------------------------------------------------------------------ 写
    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        masked = bytes(bytearray(b ^ mask[i & 3] for i, b in enumerate(bytearray(payload))))
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        self.sock.sendall(bytes(header) + mask + masked)

    def send_json(self, obj):
        self._send_frame(0x1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def close(self):
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except Exception:
            pass
        self.sock.close()
