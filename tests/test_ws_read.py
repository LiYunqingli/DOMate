# -*- coding: utf-8 -*-
"""WS 读取层单元测试：重点验证「读超时之后连接仍然可用」。

这是一个真实踩过的坑：早期实现借用 http.server 的 rfile 读取，socket 一
超时缓冲读取器就永久失效（OSError: cannot read from timed out object），
于是空闲的插件连接被服务端自己读死，只能等插件重连——表现就是明显的卡顿。
"""

import json
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.ws import WSConnection, WSError

PASSED = []
FAILED = []


def check(name, cond, detail=None):
    if cond:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append(name)
        print("  [FAIL] %s %s" % (name, detail if detail is not None else ""))


def frame(opcode, payload=b""):
    """构造客户端帧（客户端发出的帧必须带掩码）。"""
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
    return bytes(header) + mask + masked


def text(obj):
    return frame(0x1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def pair():
    a, b = socket.socketpair()
    conn = WSConnection(a, None, a.makefile("wb"))
    return conn, b


def test_timeout_is_recoverable():
    conn, peer = pair()
    try:
        # 连续 5 次读超时，每次都应抛 socket.timeout，而不是让连接失效
        ok = True
        for _ in range(5):
            try:
                conn.recv_json(timeout=0.05)
                ok = False
                break
            except socket.timeout:
                continue
            except Exception:
                ok = False
                break
        check("连续读超时都是 socket.timeout（连接未失效）", ok)

        # 超时之后仍能收到完整消息
        peer.sendall(text({"type": "ping", "t": 1}))
        msg = conn.recv_json(timeout=2)
        check("超时后仍能正常收消息", msg == {"type": "ping", "t": 1}, msg)
    finally:
        conn.closed = True
        conn.sock.close()
        peer.close()


def test_partial_frame_across_timeouts():
    """帧只到了一半就中断，剩余部分晚点到达——不能丢数据、不能粘包。"""
    conn, peer = pair()
    try:
        payload = text({"type": "big", "text": "x" * 200})
        peer.sendall(payload[:5])
        time.sleep(0.05)
        try:
            conn.recv_json(timeout=0.1)
            check("半包时按超时返回而不是报错", False, "未抛超时")
        except socket.timeout:
            check("半包时按超时返回而不是报错", True)

        peer.sendall(payload[5:])
        msg = conn.recv_json(timeout=2)
        check("跨多次读取拼回完整帧", msg == {"type": "big", "text": "x" * 200}, msg)
    finally:
        conn.closed = True
        conn.sock.close()
        peer.close()


def test_back_to_back_frames():
    """一个 recv 里塞两帧（粘包），第二帧必须留在缓冲区里等下一次读。"""
    conn, peer = pair()
    try:
        peer.sendall(text({"type": "a"}) + text({"type": "b"}))
        first = conn.recv_json(timeout=2)
        second = conn.recv_json(timeout=2)
        check("粘包时两帧都能正确拆开", first == {"type": "a"} and second == {"type": "b"},
              "%s %s" % (first, second))
    finally:
        conn.closed = True
        conn.sock.close()
        peer.close()


def test_ping_between_messages():
    """协议层 ping 应被自动回 pong，且不干扰消息读取。"""
    conn, peer = pair()
    try:
        peer.sendall(frame(0x9, b"hi"))
        peer.sendall(text({"type": "after-ping"}))
        msg = conn.recv_json(timeout=2)
        check("ping 不影响后续消息", msg == {"type": "after-ping"}, msg)

        peer.settimeout(2)
        raw = peer.recv(1024)
        check("收到自动回写的 pong", raw and (raw[0] & 0x0F) == 0xA, raw[:4])
    finally:
        conn.closed = True
        conn.sock.close()
        peer.close()


def test_close_frame():
    conn, peer = pair()
    try:
        peer.sendall(frame(0x8, struct.pack("!H", 1000)))
        check("收到 close 帧返回 None", conn.recv_json(timeout=2) is None)
    finally:
        conn.closed = True
        conn.sock.close()
        peer.close()


def test_peer_gone():
    conn, peer = pair()
    try:
        peer.close()
        try:
            conn.recv_json(timeout=1)
            check("对端断开抛 WSError", False, "未抛异常")
        except WSError:
            check("对端断开抛 WSError", True)
    finally:
        conn.closed = True
        conn.sock.close()


def main():
    for fn in (test_timeout_is_recoverable, test_partial_frame_across_timeouts,
               test_back_to_back_frames, test_ping_between_messages,
               test_close_frame, test_peer_gone):
        fn()
    print("\n通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("失败项：" + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
