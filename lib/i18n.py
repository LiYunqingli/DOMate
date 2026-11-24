# -*- coding: utf-8 -*-
"""多语言（语言包）加载与取值。

语言包存放：res/lang/<lang>.json，扁平化较深的 JSON 结构，用点号取键。
取值失败时返回键本身，便于开发期一眼看出漏翻译。
"""

import json
import os

from .errors import DomateError

# 项目根目录（lib 的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANG_DIR = os.path.join(ROOT, "res", "lang")

#: 内置语言列表，用于 `domate config set language` 校验与帮助提示
SUPPORTED = ("zh-CN", "en-US")

DEFAULT_LANG = "zh-CN"


class I18n(object):
    def __init__(self, lang=DEFAULT_LANG, lang_dir=None):
        self.lang_dir = lang_dir or LANG_DIR
        self.lang = lang or DEFAULT_LANG
        self.data = {}
        self._cache = {}
        self._load(self.lang)
        # 语言包缺键时回退到默认中文包，尽量避免终端出现裸键
        if self.lang != DEFAULT_LANG:
            self._fallback = {}
            try:
                with open(self._path(DEFAULT_LANG), "r", encoding="utf-8") as fp:
                    self._fallback = json.load(fp)
            except (IOError, ValueError):
                self._fallback = {}
        else:
            self._fallback = {}

    # ------------------------------------------------------------------ 加载
    def _path(self, lang):
        return os.path.join(self.lang_dir, "%s.json" % lang)

    def _load(self, lang):
        path = self._path(lang)
        if not os.path.isfile(path):
            raise DomateError("E_LANG", message="language pack not found: %s" % path)
        with open(path, "r", encoding="utf-8") as fp:
            self.data = json.load(fp)

    def reload(self, lang=None):
        if lang:
            self.lang = lang
        self._cache = {}
        self._load(self.lang)

    # ------------------------------------------------------------------ 取值
    def raw(self, key):
        """返回原始节点（可能是 dict/list，用于命令表、帮助章节）。"""
        if key in self._cache:
            return self._cache[key]
        node = _dig(self.data, key)
        if node is None:
            node = _dig(self._fallback, key)
        self._cache[key] = node
        return node

    def t(self, key, **fmt):
        """取字符串并做 {name} 插值。"""
        node = self.raw(key)
        if node is None:
            return key
        if not isinstance(node, str):
            return key
        if not fmt:
            return node
        try:
            return node.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return node

    def has(self, key):
        return self.raw(key) is not None

    def meta(self):
        return self.raw("meta") or {}


def _dig(data, key):
    """按 a.b.c 逐层取字典节点。"""
    node = data
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node
