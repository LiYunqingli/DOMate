# -*- coding: utf-8 -*-
"""config.json 读写与默认配置。

配置优先级：命令行参数 > config.json > 内置默认值。
所有默认值集中写在 DEFAULTS，config.json 允许只写需要覆盖的字段。
"""

import copy
import json
import os

from .errors import DomateError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")

#: 内置默认配置（config.json 缺失/为空时使用，并会被写入文件）
DEFAULTS = {
    "language": "zh-CN",
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
        "token": "",
        "auto_start": True,
        "poll_timeout": 25,
        "idle_exit_minutes": 0,
        "log_level": "info",
        "log_file": "logs/domate.log"
    },
    "command": {
        "timeout": 30,
        "retry": 1,
        "default_wait": 8
    },
    "tab": {
        "status_title": True,
        "title_flash_ms": 600,
        "default_width": 1440,
        "default_height": 900
    },
    "output": {
        "color": "auto",
        "format": "text",
        "max_text": 4000,
        "max_nodes": 600
    },
    "security": {
        "allow_js": True,
        "allow_nav": True,
        "allow_file": True
    },
    "screenshot": {
        "dir": "screenshots"
    }
}


def _deep_merge(base, override):
    """把 override 合并进 base 的副本（dict 递归合并，其他类型直接覆盖）。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config(object):
    def __init__(self, path=None):
        self.path = path or CONFIG_PATH
        self.data = copy.deepcopy(DEFAULTS)
        self.exists = os.path.isfile(self.path)
        self.load()

    # ------------------------------------------------------------------ 读写
    def load(self):
        if not self.exists:
            return self
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                raw = fp.read().strip()
            if not raw:
                # 空文件（仓库里的占位）按默认配置处理
                return self
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                raise DomateError("E_CONFIG", message="config.json 顶层必须是对象")
            self.data = _deep_merge(DEFAULTS, loaded)
        except ValueError as exc:
            raise DomateError("E_CONFIG", message="config.json 解析失败: %s" % exc, hint="config")
        return self

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fp:
            json.dump(self.data, fp, ensure_ascii=False, indent=4)
            fp.write("\n")
        self.exists = True
        return self

    # ------------------------------------------------------------------ 取值
    def get(self, key, default=None):
        node = self.data
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, key, value):
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        return self

    # ------------------------------------------------------------------ 便捷属性
    @property
    def lang(self):
        return self.get("language", "zh-CN")

    @property
    def host(self):
        return self.get("server.host", "127.0.0.1")

    @property
    def port(self):
        return int(self.get("server.port", 8765))

    @property
    def token(self):
        return self.get("server.token", "") or ""

    @property
    def base_url(self):
        return "http://%s:%d" % (self.host, self.port)

    def abs_path(self, rel):
        """把配置里的相对路径解析为相对项目根目录的绝对路径。"""
        if os.path.isabs(rel):
            return rel
        return os.path.join(ROOT, rel)

    def to_dict(self):
        return copy.deepcopy(self.data)


def coerce(value):
    """命令行字符串值 -> JSON 原生类型（true/false/数字/JSON/普通字符串）。"""
    if not isinstance(value, str):
        return value
    low = value.strip()
    if low.lower() in ("true", "false"):
        return low.lower() == "true"
    if low.lower() in ("null", "none"):
        return None
    try:
        return int(low)
    except ValueError:
        pass
    try:
        return float(low)
    except ValueError:
        pass
    if low[:1] in "[{" and low[-1:] in "]}":
        try:
            return json.loads(low)
        except ValueError:
            pass
    return value
