# -*- coding: utf-8 -*-
"""命令模块共用的小工具。"""

import sys


def group_fallback(parser, i18n=None):
    """命令组没带子命令时打印该组帮助，而不是静默退出。"""

    def handler(ctx):
        parser.print_help(sys.stderr)
        return 2

    return handler


def clip(text, width):
    text = str(text if text is not None else "")
    if width <= 0 or len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def flatten_tree(lines, prefix="  "):
    """给树形文本加缩进前缀（引擎已带缩进时直接用）。"""
    return "\n".join(prefix + line for line in str(lines).splitlines())
