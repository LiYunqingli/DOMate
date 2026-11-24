# -*- coding: utf-8 -*-
"""元素定位表达式解析。

支持四种写法，全部在服务端解析成统一结构后下发给浏览器端：
    css=#app .btn      显式 CSS
    #app .btn          省略前缀，默认 CSS
    text=登录           文本匹配（先精确、后包含、忽略大小写与空白）
    xpath=//button      显式 XPath
    //button           以 // 或 / 开头自动识别为 XPath
    3                  纯数字 = 引用 dom outline 给出的索引（data-domate-idx）

另外支持 `@n` 形式选择第 n 个匹配（从 1 开始），等价于 --index。
"""

import re

from .errors import DomateError

_PREFIX_RE = re.compile(r"^(css|text|xpath|xpath:)\s*=\s*(.+)$", re.I | re.S)


def parse(expr, index=None):
    """把用户输入解析为 {by, value, index} 结构。"""
    if expr is None or str(expr).strip() == "":
        raise DomateError("E_SELECTOR", message="选择器不能为空")

    expr = str(expr).strip()
    by = "css"
    value = expr

    m = _PREFIX_RE.match(expr)
    if m:
        by = m.group(1).lower().replace(":", "")
        value = m.group(2).strip()
        if by == "xpath":
            by = "xpath"
    elif expr.startswith("//") or expr.startswith("(//") or expr.startswith("/html"):
        by = "xpath"
    elif expr.isdigit():
        # 纯数字视为 outline 索引，方便 AI 直接点「第 3 个可交互元素」
        by = "css"
        value = '[data-domate-idx="%s"]' % expr
    elif expr.startswith("@"):
        by = "css"
        value = '[data-domate-idx="%s"]' % expr[1:]

    # 去掉可能的 @n 后缀（仅 CSS 场景，形如 "#btn@2"）
    if by == "css" and index is None:
        m2 = re.match(r"^(.*?\S)@(\d+)$", value)
        if m2:
            value = m2.group(1)
            index = int(m2.group(2))

    if not value:
        raise DomateError("E_SELECTOR", message="选择器不能为空")

    return {"by": by, "value": value, "index": index}


def normalize_js(params):
    """把解析结果转成浏览器端 JSON 可读的形式。"""
    if not params:
        return params
    out = dict(params)
    if "selector" in out and isinstance(out["selector"], str):
        out["selector"] = parse(out["selector"], out.get("index"))
    return out
