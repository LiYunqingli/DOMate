# -*- coding: utf-8 -*-
"""脚本类命令：注入 JS / 求值。

两个入口：
    js   —— 注入一段代码，返回最后表达式结果（默认在主世界执行，可访问页面变量）
    eval —— 与 js 等价，语义更贴「求一个值」
安全开关：config.security.allow_js = false 时直接拒绝。
"""

import json

from ..errors import DomateError
from . import shared


def _guard(ctx):
    if not ctx.config.get("security.allow_js", True):
        raise DomateError("E_DISABLED", message="config.security.allow_js = false，脚本注入已被禁用")


def _render(ctx, data):
    if ctx.emit(data):
        return 0
    value = data.get("value")
    if isinstance(value, (dict, list)):
        ctx.out.block(ctx.out.truncate(json.dumps(value, ensure_ascii=False, indent=2)))
    elif value is None:
        ctx.out.write(ctx.out.paint("null", "gray"))
    else:
        ctx.out.block(ctx.out.truncate(str(value)))
    if data.get("type") and data.get("type") != "undefined":
        ctx.out.write("")
        ctx.out.info(ctx.text("msg.js_type", type=data.get("type")))
    return 0


def cmd_js(ctx):
    _guard(ctx)
    code = ctx.read_file_arg(ctx.need(ctx.opt("code")))
    world = "ISOLATED" if ctx.opt("isolated", False) else "MAIN"
    params = {"code": code, "world": world, "timeout": ctx.timeout(0)}
    data = ctx.call("script.inject", params, timeout=ctx.timeout(5))
    return _render(ctx, data)


def cmd_eval(ctx):
    _guard(ctx)
    code = ctx.read_file_arg(ctx.need(ctx.opt("code")))
    params = {"code": code, "world": "MAIN", "wrap": True, "timeout": ctx.timeout(0)}
    data = ctx.call("script.inject", params, timeout=ctx.timeout(5))
    return _render(ctx, data)


def register(subparsers, common):
    i18n = common["i18n"]
    global_parser = common["global"]

    p = subparsers.add_parser(
        "js", parents=[global_parser],
        help=i18n.t("cmd.js.brief"), description=i18n.t("cmd.js.desc"))
    p.add_argument("code", help=i18n.t("cmd.js.arg.code"))
    p.add_argument("--isolated", action="store_true", help=i18n.t("cmd.js.opt.isolated"))
    p.set_defaults(handler=cmd_js)

    p = subparsers.add_parser(
        "eval", parents=[global_parser],
        help=i18n.t("cmd.eval.brief"), description=i18n.t("cmd.eval.desc"))
    p.add_argument("code", help=i18n.t("cmd.eval.arg.code"))
    p.set_defaults(handler=cmd_eval)
