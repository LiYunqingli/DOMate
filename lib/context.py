# -*- coding: utf-8 -*-
"""命令执行上下文：把配置、语言包、输出器、通讯层打包传给各命令模块。"""

import json

from .errors import DomateError, UsageError

_UNSET = object()


class Ctx(object):
    def __init__(self, config, i18n, out, args, rpc, state):
        self.config = config
        self.i18n = i18n
        self.out = out
        self.args = args
        self.rpc = rpc
        self.state = state

    # ------------------------------------------------------------------ 参数
    def opt(self, name, default=None):
        return getattr(self.args, name, default)

    @property
    def tab(self):
        return self.opt("tab", None)

    @property
    def json_mode(self):
        return bool(self.opt("json", False))

    def timeout(self, extra=0.0):
        base = float(self.opt("timeout", 0) or self.config.get("command.timeout", 30))
        return base + extra

    # ------------------------------------------------------------------ 调用
    def call(self, action, params=None, timeout=None, retry=_UNSET, tab=_UNSET):
        return self.rpc.call(
            action,
            params=params or {},
            tab=self.tab if tab is _UNSET else tab,
            timeout=timeout,
            retry=None if retry is _UNSET else retry,
        )

    # ------------------------------------------------------------------ 输出
    def emit(self, data, meta=None):
        """统一出口：--json 输出结构化结果，否则由调用方自行渲染。"""
        payload = {"ok": True, "data": data}
        if meta:
            payload["meta"] = meta
        if self.json_mode:
            self.out.json_out(payload)
            return True
        return False

    def text(self, key, **fmt):
        return self.i18n.t(key, **fmt)

    def need(self, value, key="E_USAGE"):
        if value is None or value == "":
            raise UsageError(key)
        return value

    # ------------------------------------------------------------------ 便捷
    def read_file_arg(self, value):
        """支持 @file 形式读取代码/JSON，避免超长命令行。"""
        if not value:
            return ""
        if value.startswith("@"):
            path = value[1:]
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    return fp.read()
            except IOError as exc:
                raise DomateError("E_FILE", message="读取文件失败: %s" % exc)
        return value

    def read_json_arg(self, value):
        raw = self.read_file_arg(value)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise UsageError("E_JSON", message="JSON 解析失败: %s" % exc)
