# -*- coding: utf-8 -*-
"""本地状态（选中的标签页等）持久化。

放在用户目录 ~/.domate/state.json，不污染项目仓库。
浏览器重启后 tabId 会变，所以记录 url 用于校验，失效则回落到活动标签页。
"""

import json
import os

STATE_DIR = os.path.join(os.path.expanduser("~"), ".domate")
STATE_PATH = os.path.join(STATE_DIR, "state.json")

_DEFAULT = {
    "tab_id": None,
    "tab_url": "",
    "tab_title": "",
    "last_command": "",
}


class State(object):
    def __init__(self, path=None):
        self.path = path or STATE_PATH
        self.data = dict(_DEFAULT)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                self.data.update(json.load(fp) or {})
        except (IOError, OSError, ValueError):
            pass
        return self

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fp:
                json.dump(self.data, fp, ensure_ascii=False, indent=2)
        except (IOError, OSError):
            # 状态写失败不应阻塞主流程
            pass
        return self

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        return self

    def remember_tab(self, tab):
        self.data["tab_id"] = tab.get("id")
        self.data["tab_url"] = tab.get("url", "")
        self.data["tab_title"] = tab.get("title", "")
        return self.save()

    def clear_tab(self):
        self.data["tab_id"] = None
        self.data["tab_url"] = ""
        self.data["tab_title"] = ""
        return self.save()
