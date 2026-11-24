# -*- coding: utf-8 -*-
"""交互类命令：点击、输入、按键、悬停、滚动、选择、勾选、聚焦、批量填表。

注意：事件由脚本派发，isTrusted=false。绝大多数站点可正常工作，
强校验（银行/风控类）站点需要配合真实的 CDP 输入通道，见 docs 说明。
"""

from ..errors import DomateError
from ..selector import parse
from . import shared


def _target(ctx, required=True):
    expr = ctx.opt("target")
    if required and not expr:
        raise DomateError("E_USAGE", message="缺少目标元素选择器")
    if not expr:
        return None
    return parse(expr, ctx.opt("index"))


def _brief(data):
    """把浏览器端返回的执行摘要压成一行。"""
    if not isinstance(data, dict):
        return ""
    bits = []
    if data.get("tag"):
        bits.append("<%s>" % data.get("tag"))
    if data.get("text"):
        bits.append(shared.clip(data.get("text"), 40))
    if data.get("navigated"):
        bits.append("→ " + str(data.get("url") or "")[:70])
    return " ".join(bits)


# ---------------------------------------------------------------- 点击
def cmd_click(ctx):
    selector = _target(ctx)
    params = {
        "selector": selector,
        "double": bool(ctx.opt("double", False)),
        "right": bool(ctx.opt("right", False)),
        "force": bool(ctx.opt("force", False)),
        "waitNav": bool(ctx.opt("wait_nav", False)),
    }
    data = ctx.call("act.click", params, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.clicked", detail=_brief(data)))
    return 0


# ---------------------------------------------------------------- 输入
def cmd_type(ctx):
    selector = _target(ctx)
    text = ctx.opt("text")
    if text is None:
        raise DomateError("E_USAGE", message="缺少要输入的文本")
    text = ctx.read_file_arg(text) if str(text).startswith("@") else text
    params = {
        "selector": selector,
        "text": text,
        "clear": not bool(ctx.opt("append", False)),
        "submit": bool(ctx.opt("submit", False)),
        "delay": int(ctx.opt("delay", 0) or 0),
    }
    data = ctx.call("act.type", params, timeout=ctx.timeout(5))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.typed", count=len(text), detail=_brief(data)))
    return 0


def cmd_key(ctx):
    keys = ctx.need(ctx.opt("keys"))
    data = ctx.call("act.key", {"keys": keys}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.key_sent", keys=keys))
    return 0


# ---------------------------------------------------------------- 其它交互
def cmd_hover(ctx):
    data = ctx.call("act.hover", {"selector": _target(ctx)}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.hovered", detail=_brief(data)))
    return 0


def cmd_scroll(ctx):
    params = {
        "selector": _target(ctx, required=False),
        "by": ctx.opt("by"),
        "top": bool(ctx.opt("top", False)),
        "bottom": bool(ctx.opt("bottom", False)),
        "smooth": bool(ctx.opt("smooth", False)),
    }
    if params["by"] is not None:
        try:
            params["by"] = int(params["by"])
        except (TypeError, ValueError):
            raise DomateError("E_USAGE", message="--by 需要整数像素")
    data = ctx.call("act.scroll", params, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.scrolled", y=data.get("scrollY", "?")))
    return 0


def cmd_select(ctx):
    selector = _target(ctx)
    value = ctx.opt("value")
    label = ctx.opt("label")
    if not value and not label:
        raise DomateError("E_USAGE", message="select 需要 value 或 --label")
    data = ctx.call("act.select", {
        "selector": selector,
        "value": value,
        "label": label,
        "optIndex": ctx.opt("option_index"),
    }, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.selected", value=data.get("value", "")))
    return 0


def cmd_check(ctx):
    data = ctx.call("act.check", {"selector": _target(ctx), "checked": True}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.checked"))
    return 0


def cmd_uncheck(ctx):
    data = ctx.call("act.check", {"selector": _target(ctx), "checked": False}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.unchecked"))
    return 0


def cmd_focus(ctx):
    data = ctx.call("act.focus", {"selector": _target(ctx)}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.focused", detail=_brief(data)))
    return 0


def cmd_fill(ctx):
    """批量填表：domate fill "#user=admin" "#pwd=123456\""""
    pairs = ctx.opt("pairs") or []
    if not pairs:
        raise DomateError("E_USAGE", message="fill 需要 selector=value 形式的多组参数")
    fields = []
    for item in pairs:
        if "=" not in item:
            raise DomateError("E_USAGE", message="参数格式应为 selector=value: %s" % item)
        selector, value = item.split("=", 1)
        fields.append({"selector": parse(selector), "text": value, "clear": True})
    data = ctx.call("act.fill", {"fields": fields}, timeout=ctx.timeout(5))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.filled", count=data.get("count", len(fields))))
    for failure in data.get("failed") or []:
        ctx.out.warn("  %s: %s" % (failure.get("selector"), failure.get("message")))
    return 0


# ---------------------------------------------------------------- 注册
def register(subparsers, common):
    i18n = common["i18n"]
    global_parser = common["global"]

    def add(name, func, need_target=True):
        parser = subparsers.add_parser(
            name, parents=[global_parser],
            help=i18n.t("cmd.%s.brief" % name),
            description=i18n.t("cmd.%s.desc" % name),
        )
        parser.set_defaults(handler=func)
        if need_target:
            parser.add_argument("target", help=i18n.t("arg.selector"))
            parser.add_argument("--index", type=int, default=None, help=i18n.t("arg.index"))
        return parser

    p = add("click", cmd_click)
    p.add_argument("--double", action="store_true", help=i18n.t("cmd.click.opt.double"))
    p.add_argument("--right", action="store_true", help=i18n.t("cmd.click.opt.right"))
    p.add_argument("--force", action="store_true", help=i18n.t("cmd.click.opt.force"))
    p.add_argument("--wait-nav", action="store_true", help=i18n.t("cmd.click.opt.wait_nav"))

    p = add("type", cmd_type)
    p.add_argument("text", help=i18n.t("cmd.type.arg.text"))
    p.add_argument("--append", action="store_true", help=i18n.t("cmd.type.opt.append"))
    p.add_argument("--submit", action="store_true", help=i18n.t("cmd.type.opt.submit"))
    p.add_argument("--delay", type=int, default=0, help=i18n.t("cmd.type.opt.delay"))

    p = add("key", cmd_key, need_target=False)
    p.add_argument("keys", help=i18n.t("cmd.key.arg.keys"))

    add("hover", cmd_hover)

    p = add("scroll", cmd_scroll, need_target=False)
    p.add_argument("target", nargs="?", help=i18n.t("arg.selector"))
    p.add_argument("--by", default=None, help=i18n.t("cmd.scroll.opt.by"))
    p.add_argument("--top", action="store_true", help=i18n.t("cmd.scroll.opt.top"))
    p.add_argument("--bottom", action="store_true", help=i18n.t("cmd.scroll.opt.bottom"))
    p.add_argument("--smooth", action="store_true", help=i18n.t("cmd.scroll.opt.smooth"))

    p = add("select", cmd_select)
    p.add_argument("value", nargs="?", help=i18n.t("cmd.select.arg.value"))
    p.add_argument("--label", default=None, help=i18n.t("cmd.select.opt.label"))
    p.add_argument("--option-index", type=int, default=None, dest="option_index",
                   help=i18n.t("cmd.select.opt.option_index"))

    add("check", cmd_check)
    add("uncheck", cmd_uncheck)
    add("focus", cmd_focus)

    p = add("fill", cmd_fill, need_target=False)
    p.add_argument("pairs", nargs="+", help=i18n.t("cmd.fill.arg.pairs"))
