# -*- coding: utf-8 -*-
"""终端输出：颜色、表格、键值、JSON。

跨平台说明：
- Windows 10+ 默认控制台不开启 ANSI 转义，这里用 os.system("") 触发 VT 模式；
- 非 TTY / NO_COLOR / config.output.color=never 时自动降级为无色输出；
- 表格宽度用 unicodedata 计算东亚宽字符占 2 列，保证中文对齐。
"""

import json
import os
import sys
import unicodedata

RESET = "\033[0m"
STYLES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
}


def _enable_win_vt():
    """在 Windows 上启用 ANSI 转义序列支持。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        for handle in (-11, -12):  # STD_OUTPUT_HANDLE / STD_ERROR_HANDLE
            h = kernel32.GetStdHandle(handle)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def _text_width(text):
    """字符串在终端里占用的列数（CJK 全角算 2）。"""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1
    return width


def _pad(text, width, align="left"):
    gap = max(0, width - _text_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def _truncate(text, width):
    """按终端列宽截断，超出部分用省略号。"""
    if width <= 0 or _text_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…"


class Console(object):
    """统一的输出门面。所有命令都通过它写终端。"""

    def __init__(self, color="auto", json_mode=False, max_text=4000, stream=None):
        self.stream = stream or sys.stdout
        self.json_mode = json_mode
        self.max_text = max_text
        self.color = self._decide_color(color)

    @staticmethod
    def _decide_color(mode):
        if mode in ("never", "false", "0"):
            return False
        if os.environ.get("NO_COLOR"):
            return False
        if mode in ("always", "true", "1"):
            _enable_win_vt()
            return True
        if not sys.stdout.isatty():
            return False
        _enable_win_vt()
        return True

    # ------------------------------------------------------------------ 基础
    def paint(self, text, style):
        if not self.color or not style:
            return text
        return "%s%s%s" % (STYLES.get(style, ""), text, RESET)

    def write(self, text=""):
        self.stream.write(text + "\n")
        self.stream.flush()

    def title(self, text):
        if self.json_mode:
            return
        self.write(self.paint(text, "bold"))

    def ok(self, text):
        if self.json_mode:
            return
        self.write("%s %s" % (self.paint("✔", "green"), text))

    def info(self, text):
        if self.json_mode:
            return
        self.write("%s %s" % (self.paint("·", "cyan"), text))

    def warn(self, text):
        if self.json_mode:
            return
        self.write("%s %s" % (self.paint("!", "yellow"), text))

    def error(self, text, hint=None):
        """错误永远走 stderr，且不受 --json 抑制。"""
        err = sys.stderr
        if self.color:
            err.write("%s %s\n" % (self.paint("✘", "red"), text))
            if hint:
                err.write("  %s\n" % self.paint(hint, "gray"))
        else:
            err.write("x %s\n" % text)
            if hint:
                err.write("  %s\n" % hint)
        err.flush()

    def note(self, text):
        """过程提示：走 stderr，不干扰 --json 的管道输出。"""
        if self.json_mode:
            return
        err = sys.stderr
        err.write(self.paint("… " + text, "dim") + "\n")
        err.flush()

    def json_out(self, obj):
        """结构化输出，供 AI/脚本解析。"""
        self.write(json.dumps(obj, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------------ 结构
    def kv(self, pairs, indent=0):
        if self.json_mode:
            return
        pad = " " * indent
        if not pairs:
            return
        width = max(_text_width(str(k)) for k, _ in pairs)
        for key, value in pairs:
            self.write("%s%s  %s" % (pad, self.paint(_pad(str(key), width), "gray"), value))

    def table(self, headers, rows, align=None, max_width=None):
        """打印对齐表格；数据在内存里都按列宽算好再输出。"""
        if self.json_mode:
            return
        rows = [[("" if c is None else str(c)) for c in row] for row in rows]
        align = align or ["left"] * len(headers)
        widths = [_text_width(str(h)) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], _text_width(cell))
        if max_width:
            for i in range(len(widths)):
                widths[i] = min(widths[i], max_width)
        header = "  ".join(self.paint(_pad(str(h), widths[i]), "bold") for i, h in enumerate(headers))
        self.write(header)
        self.write(self.paint("  ".join("-" * w for w in widths), "gray"))
        for row in rows:
            cells = []
            for i, cell in enumerate(row):
                cell = _truncate(cell, widths[i]) if max_width else cell
                cells.append(_pad(cell, widths[i], align[i]))
            self.write("  ".join(cells))

    def block(self, text):
        """原样输出多行文本（保留缩进，去掉尾部空白）。"""
        if self.json_mode:
            return
        for line in str(text).splitlines():
            self.write(line.rstrip())

    def truncate(self, text):
        if text is None:
            return ""
        text = str(text)
        limit = self.max_text or 0
        if limit and len(text) > limit:
            return text[:limit] + "\n… (%d chars truncated)" % (len(text) - limit)
        return text
