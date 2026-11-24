# -*- coding: utf-8 -*-
"""DOM 读取类命令：结构树、可交互元素清单、HTML、文本、属性、查找、等待。

所有命令都只是「取数据」，不会改变页面状态（wait 会轮询但不触发交互）。
读取类命令的超时允许单独放大，因为 wait 本身要等。
"""

from ..errors import DomateError
from ..selector import parse
from . import shared


def _sel(ctx, expr, index=None):
    """把 CLI 的选择器表达式解析成下发结构。"""
    if expr is None or str(expr).strip() == "":
        return None
    return parse(expr, index if index is not None else ctx.opt("index"))


# ---------------------------------------------------------------- tree
def cmd_tree(ctx):
    """输出精简 DOM 树，给 AI 看页面骨架。"""
    params = {
        "selector": _sel(ctx, ctx.opt("selector")),
        "depth": int(ctx.opt("depth", 3) or 3),
        "maxNodes": int(ctx.opt("max", 0) or ctx.config.get("output.max_nodes", 600)),
    }
    data = ctx.call("dom.tree", params, timeout=ctx.timeout(5))
    if ctx.emit(data):
        return 0
    ctx.out.title(ctx.text("msg.tree_title", count=data.get("count", 0)))
    ctx.out.block(ctx.out.truncate(data.get("text", "")))
    if data.get("truncated"):
        ctx.out.warn(ctx.text("msg.truncated", count=data.get("count", 0)))
    return 0


# ---------------------------------------------------------------- outline
def cmd_outline(ctx):
    """列出可交互元素并打上索引，之后可以直接 `domate click 3`。"""
    params = {"max": int(ctx.opt("max", 0) or 300), "all": bool(ctx.opt("all", False))}
    data = ctx.call("dom.outline", params, timeout=ctx.timeout(5))
    items = data.get("items") or []
    if ctx.emit(data):
        return 0
    if not items:
        ctx.out.warn(ctx.text("msg.outline_empty"))
        return 0
    rows = []
    for item in items:
        label = item.get("text") or item.get("name") or item.get("placeholder") \
            or item.get("aria") or item.get("href") or ""
        flags = []
        if item.get("disabled"):
            flags.append(ctx.text("flag.disabled"))
        if not item.get("visible", True):
            flags.append(ctx.text("flag.hidden"))
        if item.get("checked"):
            flags.append(ctx.text("flag.checked"))
        rows.append([
            item.get("idx"),
            item.get("tag"),
            item.get("type") or "",
            shared.clip(label, 40),
            ",".join(flags),
        ])
    ctx.out.table(
        [ctx.text("field.idx"), ctx.text("field.tag"), ctx.text("field.type"),
         ctx.text("field.text"), ctx.text("field.state")],
        rows, align=["right", "left", "left", "left", "left"],
    )
    ctx.out.write("")
    ctx.out.info(ctx.text("msg.outline_hint", count=len(items)))
    return 0


# ---------------------------------------------------------------- 内容读取
def cmd_html(ctx):
    data = ctx.call("dom.html", {"selector": _sel(ctx, ctx.opt("selector"))}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.block(ctx.out.truncate(data.get("html", "")))
    return 0


def cmd_text(ctx):
    data = ctx.call("dom.text", {"selector": _sel(ctx, ctx.opt("selector"))}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    text = data.get("text") or ""
    if not text.strip():
        ctx.out.warn(ctx.text("msg.empty_text"))
        return 0
    ctx.out.block(ctx.out.truncate(text))
    return 0


def cmd_attr(ctx):
    name = ctx.need(ctx.opt("name"))
    data = ctx.call("dom.attr", {
        "selector": _sel(ctx, ctx.opt("selector")),
        "name": name,
    }, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    if data.get("exists"):
        ctx.out.write(str(data.get("value")))
    else:
        ctx.out.warn(ctx.text("msg.attr_missing", name=name))
        return 1
    return 0


def cmd_find(ctx):
    data = ctx.call("dom.find", {
        "selector": _sel(ctx, ctx.opt("selector")),
        "limit": int(ctx.opt("limit", 0) or 30),
    }, timeout=ctx.timeout(3))
    items = data.get("items") or []
    if ctx.emit(data):
        return 0
    if not items:
        ctx.out.warn(ctx.text("msg.no_match"))
        return 1
    rows = []
    for item in items:
        rect = item.get("rect") or {}
        rows.append([
            item.get("n"),
            item.get("tag"),
            shared.clip(item.get("text") or "", 44),
            "%s,%s %sx%s" % (rect.get("x"), rect.get("y"), rect.get("w"), rect.get("h")),
            ctx.text("flag.visible") if item.get("visible") else ctx.text("flag.hidden"),
        ])
    ctx.out.table(
        [ctx.text("field.n"), ctx.text("field.tag"), ctx.text("field.text"),
         ctx.text("field.rect"), ctx.text("field.state")],
        rows, align=["right", "left", "left", "left", "left"],
    )
    ctx.out.write("")
    ctx.out.info(ctx.text("msg.match_count", count=data.get("count", len(items))))
    return 0


def cmd_count(ctx):
    data = ctx.call("dom.count", {"selector": _sel(ctx, ctx.opt("selector"))}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.write(str(data.get("count", 0)))
    return 0


def cmd_info(ctx):
    data = ctx.call("dom.info", {"selector": _sel(ctx, ctx.opt("selector"))}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    rect = data.get("rect") or {}
    ctx.out.kv([
        (ctx.text("field.tag"), data.get("tag")),
        (ctx.text("field.id"), data.get("id") or "-"),
        (ctx.text("field.classes"), " ".join(data.get("classes") or []) or "-"),
        (ctx.text("field.rect"), "%s,%s  %sx%s" % (rect.get("x"), rect.get("y"), rect.get("w"), rect.get("h"))),
        (ctx.text("field.visible"), str(data.get("visible"))),
        (ctx.text("field.enabled"), str(not data.get("disabled"))),
        (ctx.text("field.text"), shared.clip(data.get("text") or "", 80)),
        (ctx.text("field.value"), shared.clip(data.get("value") or "", 80)),
    ])
    attrs = data.get("attrs") or {}
    if attrs:
        ctx.out.write("")
        ctx.out.title(ctx.text("field.attrs"))
        for key, value in attrs.items():
            ctx.out.write("  %-18s %s" % (key, shared.clip(value, 90)))
    return 0


# ---------------------------------------------------------------- wait
def cmd_wait(ctx):
    """等待元素/文本出现或消失。超时可单独指定，服务端同步放大等待窗口。"""
    selector = _sel(ctx, ctx.opt("selector"))
    text = ctx.opt("text")
    gone = bool(ctx.opt("gone", False))
    if not selector and not text:
        raise DomateError("E_USAGE", message="wait 需要选择器或 --text")
    wait_timeout = float(ctx.opt("timeout", 0) or ctx.config.get("command.default_wait", 8))
    params = {
        "selector": selector,
        "text": text,
        "gone": gone,
        "timeout": wait_timeout,
    }
    data = ctx.call("dom.wait", params, timeout=wait_timeout + 8)
    if ctx.emit(data):
        return 0
    if data.get("found"):
        ctx.out.ok(ctx.text("msg.wait_found", elapsed=int(data.get("elapsed", 0))))
        return 0
    ctx.out.warn(ctx.text("msg.wait_timeout", timeout=wait_timeout))
    return 1


# ---------------------------------------------------------------- 注册
def register(subparsers, common):
    i18n = common["i18n"]
    global_parser = common["global"]

    group = subparsers.add_parser(
        "dom", help=i18n.t("cmd.dom.brief"), description=i18n.t("cmd.dom.brief"))
    sub = group.add_subparsers(dest="dom_command", metavar="<子命令>")

    def add(name, func, selector_nargs="?"):
        parser = sub.add_parser(
            name, parents=[global_parser],
            help=i18n.t("cmd.dom_%s.brief" % name),
            description=i18n.t("cmd.dom_%s.desc" % name),
        )
        parser.set_defaults(handler=func)
        if selector_nargs:
            parser.add_argument("selector", nargs=selector_nargs, help=i18n.t("arg.selector"))
            parser.add_argument("--index", type=int, default=None, help=i18n.t("arg.index"))
        return parser

    p = add("tree", cmd_tree)
    p.add_argument("--depth", type=int, default=3, help=i18n.t("cmd.dom_tree.opt.depth"))
    p.add_argument("--max", type=int, default=0, help=i18n.t("cmd.dom_tree.opt.max"))

    p = add("outline", cmd_outline, selector_nargs=None)
    p.add_argument("--max", type=int, default=0, help=i18n.t("cmd.dom_outline.opt.max"))
    p.add_argument("--all", action="store_true", help=i18n.t("cmd.dom_outline.opt.all"))

    add("html", cmd_html)
    add("text", cmd_text)
    add("info", cmd_info)

    p = add("attr", cmd_attr)
    p.add_argument("name", help=i18n.t("cmd.dom_attr.arg.name"))

    p = add("find", cmd_find)
    p.add_argument("--limit", type=int, default=0, help=i18n.t("cmd.dom_find.opt.limit"))

    add("count", cmd_count)

    p = add("wait", cmd_wait)
    p.add_argument("--text", default=None, help=i18n.t("cmd.dom_wait.opt.text"))
    p.add_argument("--gone", action="store_true", help=i18n.t("cmd.dom_wait.opt.gone"))

    group.set_defaults(handler=shared.group_fallback(group, i18n))
