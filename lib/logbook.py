# -*- coding: utf-8 -*-
"""结构化日志：一行一个 JSON 事件，同时镜像一份人类可读文本日志。

为什么要另起一套：
- 原来的 `_Logger` 只有 `时间 [级别] 文本`，能看不能算。想知道「哪个命令最慢」
  「错误率多少」「哪个标签页最容易被操作」，只能靠正则猜。
- 这里把每条日志变成带字段的事件：`cat`（分类）+ `ev`（事件名）+ 任意业务字段
  （action / tab / ok / dur_ms / code / cid / status ...），网页看板直接聚合。

落盘格式（logs/domate.jsonl）：
    {"ts":1764...,"t":"2026-11-25 01:02:03.123","lv":"info","cat":"cmd",
     "ev":"rpc","src":"daemon","pid":1234,"msg":"...","action":"click","dur_ms":12.4}

多进程安全：服务端与每个 CLI 都是独立进程，同时往一个文件追加。这里每次写都用
`O_APPEND` + 单次 `os.write`，只要单行远小于 PIPE_BUF 就不会互相撕裂。
"""

import json
import os
import threading
import time

#: 级别 -> 数值，用于阈值过滤
LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}

#: 单个字符串字段的截断长度。日志是索引不是仓库，超长的 JS/HTML 没有检索价值。
MAX_STR = 600

#: 文本镜像里额外展示的字段（人眼关心的）
_TEXT_FIELDS = ("action", "tab", "cid", "status", "dur_ms", "code", "ok", "exit")


def _clip(value, limit=MAX_STR):
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + "…(%d)" % len(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_clip(v, 120) for v in value[:12]]
    if isinstance(value, dict):
        return {str(k): _clip(v, 120) for k, v in list(value.items())[:12]}
    return _clip(str(value), limit)


def _now_text(ts):
    base = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    return "%s.%03d" % (base, int((ts % 1) * 1000))


class Logbook(object):
    """一个写日志的句柄。通常每个进程一个，用 `bind()` 派生子记录器。"""

    def __init__(self, json_path="", text_path="", level="info",
                 source="domate", max_bytes=8 * 1024 * 1024, keep=3, echo=False):
        self.json_path = json_path or ""
        self.text_path = text_path or ""
        self.level = LEVELS.get(str(level).lower(), LEVELS["info"])
        self.source = source
        self.max_bytes = int(max_bytes or 0)
        self.keep = max(0, int(keep or 0))
        self.echo = echo
        self.pid = os.getpid()
        self.lock = threading.Lock()
        self._writes = 0
        self._last_rotate = 0.0
        if self.json_path:
            try:
                os.makedirs(os.path.dirname(self.json_path) or ".", exist_ok=True)
            except OSError:
                self.json_path = ""
        if self.text_path:
            try:
                os.makedirs(os.path.dirname(self.text_path) or ".", exist_ok=True)
            except OSError:
                self.text_path = ""

    # ------------------------------------------------------------------ 写盘
    def _append(self, path, data):
        """O_APPEND 单次写，避免多进程交错把 JSON 行切断。"""
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            fd = os.open(path, flags, 0o644)
        except OSError:
            return
        try:
            os.write(fd, data)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _maybe_rotate(self):
        """超过阈值就切一份。只在写入频率不高时检查，避免每条日志都 stat。"""
        if not self.json_path or self.max_bytes <= 0:
            return
        now = time.time()
        if now - self._last_rotate < 5:
            return
        self._last_rotate = now
        try:
            if os.path.getsize(self.json_path) < self.max_bytes:
                return
        except OSError:
            return
        for i in range(self.keep, 0, -1):
            src = "%s.%d" % (self.json_path, i)
            dst = "%s.%d" % (self.json_path, i + 1)
            try:
                if os.path.exists(dst):
                    os.remove(dst)
                if os.path.exists(src):
                    os.replace(src, dst)
            except OSError:
                return
        try:
            os.replace(self.json_path, self.json_path + ".1")
        except OSError:
            pass

    def event(self, level, cat, ev, msg="", **fields):
        """写一条事件。级别低于阈值直接丢弃（但 error/warn 永不丢）。"""
        lv = str(level).lower()
        if lv not in LEVELS:
            lv = "info"
        if LEVELS[lv] < self.level:
            return
        ts = time.time()
        record = {
            "ts": round(ts, 3),
            "t": _now_text(ts),
            "lv": lv,
            "cat": cat or "sys",
            "ev": ev or "note",
            "src": self.source,
            "pid": self.pid,
        }
        if msg:
            record["msg"] = _clip(msg)
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, float):
                record[key] = round(value, 2)
            else:
                record[key] = _clip(value)

        if self.json_path:
            try:
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                line = json.dumps({"ts": record["ts"], "t": record["t"], "lv": lv,
                                   "cat": cat, "ev": ev, "src": self.source,
                                   "pid": self.pid, "msg": _clip(msg)[:400]},
                                  ensure_ascii=False)
            self._append(self.json_path, (line + "\n").encode("utf-8"))
            with self.lock:
                self._writes += 1
                if self._writes % 200 == 0:
                    self._maybe_rotate()

        if self.text_path:
            extra = " ".join("%s=%s" % (k, record[k]) for k in _TEXT_FIELDS if k in record)
            text = "%s [%s] %s%s  (%s/%s)\n" % (
                record["t"][:19], lv.upper(), record.get("msg", ""),
                ("  " + extra) if extra else "", cat, ev)
            self._append(self.text_path, text.encode("utf-8"))

        if self.echo:
            try:
                print(record["t"], lv.upper(), record.get("msg", ""))
            except Exception:
                pass

    # ------------------------------------------------------------------ 便捷
    def bind(self, **base):
        return BoundLogger(self, base)

    def debug(self, msg, cat="sys", ev="note", **fields):
        self.event("debug", cat, ev, msg, **fields)

    def info(self, msg, cat="sys", ev="note", **fields):
        self.event("info", cat, ev, msg, **fields)

    def warn(self, msg, cat="sys", ev="note", **fields):
        self.event("warn", cat, ev, msg, **fields)

    def error(self, msg, cat="sys", ev="note", **fields):
        self.event("error", cat, ev, msg, **fields)


class BoundLogger(object):
    """带上固定字段（src / cid / pid ...）的记录器，省得每次重复写。"""

    def __init__(self, book, base):
        self.book = book
        self.base = dict(base or {})

    def with_fields(self, **extra):
        merged = dict(self.base)
        merged.update(extra)
        return BoundLogger(self.book, merged)

    def event(self, level, cat, ev, msg="", **fields):
        merged = dict(self.base)
        merged.update(fields)
        self.book.event(level, cat, ev, msg, **merged)

    def debug(self, msg, cat="sys", ev="note", **fields):
        self.event("debug", cat, ev, msg, **fields)

    def info(self, msg, cat="sys", ev="note", **fields):
        self.event("info", cat, ev, msg, **fields)

    def warn(self, msg, cat="sys", ev="note", **fields):
        self.event("warn", cat, ev, msg, **fields)

    def error(self, msg, cat="sys", ev="note", **fields):
        self.event("error", cat, ev, msg, **fields)


# ---------------------------------------------------------------- 全局实例
_BOOK = None
_BOOK_KEY = None
_BOOK_LOCK = threading.Lock()


def make(json_path="", text_path="", level="info", source="domate", max_bytes=0, keep=3):
    """按给定路径造一个 Logbook（不做缓存，测试与命令行用）。"""
    return Logbook(json_path=json_path, text_path=text_path, level=level,
                   source=source, max_bytes=max_bytes, keep=keep)


def get(config, source="domate", text=None):
    """取当前进程的全局 Logbook，路径来自 config.server.*。"""
    global _BOOK, _BOOK_KEY
    json_path = config.abs_path(config.get("server.log_json", "logs/domate.jsonl"))
    text_path = config.abs_path(text or config.get("server.log_file", "logs/domate.log"))
    level = config.get("server.log_level", "info")
    max_bytes = int(float(config.get("server.log_max_mb", 8) or 8) * 1024 * 1024)
    keep = int(config.get("server.log_keep", 3) or 3)
    key = (json_path, text_path, level, source, max_bytes, keep)
    with _BOOK_LOCK:
        if _BOOK is None or _BOOK_KEY != key:
            _BOOK = Logbook(json_path=json_path, text_path=text_path, level=level,
                            source=source, max_bytes=max_bytes, keep=keep)
            _BOOK_KEY = key
        return _BOOK


def reset():
    """测试用：丢掉全局实例。"""
    global _BOOK, _BOOK_KEY
    with _BOOK_LOCK:
        _BOOK = None
        _BOOK_KEY = None
