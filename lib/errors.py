# -*- coding: utf-8 -*-
"""错误体系：统一错误码 + 多语言消息。

设计要点：
1. 每个错误只用一个稳定的 code（如 E_NO_TAB），文案全部走语言包，
   这样终端提示可以随 config.language 切换，而调用方（AI/脚本）只需要认 code。
2. 异常对象自带 hint（补救建议），CLI 会把它单独打印一行。
"""


class DomateError(Exception):
    """工具内所有可预期错误的基类。"""

    #: 错误码 -> 默认提示（语言包缺失时兜底用英文）
    def __init__(self, code, message=None, hint=None, **fmt):
        self.code = code
        self.raw_message = message
        self.hint = hint
        self.fmt = fmt
        super().__init__(message or code)

    def message(self, i18n=None):
        """按语言包渲染消息，语言包没配置就退回原始文本。"""
        if i18n is not None and self.raw_message is None:
            key = "err." + self.code
            text = i18n.t(key, **self.fmt)
            if text == key:
                text = "Unknown error (%s)" % self.code
            return text
        return self.raw_message or self.code

    def hint_text(self, i18n=None):
        if not self.hint or i18n is None:
            return self.hint
        key = "err.hint." + self.hint
        text = i18n.t(key)
        return text if text != key else self.hint

    def __str__(self):
        return self.message()


class UsageError(DomateError):
    """命令行用法错误（参数缺失、互斥参数等）。"""

    def __init__(self, code="E_USAGE", hint="usage", **fmt):
        super().__init__(code, hint=hint, **fmt)


class DaemonError(DomateError):
    """与本地服务通讯失败。"""

    def __init__(self, code="E_DAEMON", hint="daemon", **fmt):
        super().__init__(code, hint=hint, **fmt)


class BrowserError(DomateError):
    """浏览器端（插件）返回的错误。"""

    def __init__(self, code="E_BROWSER", message=None, hint=None, **fmt):
        super().__init__(code, message=message, hint=hint, **fmt)
