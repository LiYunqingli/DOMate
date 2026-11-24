# -*- coding: utf-8 -*-
"""页面类命令：导航、截图、页面信息、控制台日志、页面事件记录。"""

import base64
import os
import time

from ..errors import DomateError
from . import shared


# ---------------------------------------------------------------- 导航
def cmd_goto(ctx):
    url = ctx.need(ctx.opt("url"))
    if not ctx.config.get("security.allow_nav", True):
        raise DomateError("E_DISABLED", message="config.security.allow_nav = false，导航已被禁用")
    params = {"url": url, "wait": not ctx.opt("no_wait", False)}
    data = ctx.call("nav.goto", params, timeout=ctx.timeout(15))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.navigated", url=data.get("url"), title=data.get("title") or ""))
    return 0


def cmd_back(ctx):
    data = ctx.call("nav.back", {"steps": int(ctx.opt("steps", 1) or 1)}, timeout=ctx.timeout(15))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.navigated", url=data.get("url"), title=data.get("title") or ""))
    return 0


def cmd_forward(ctx):
    data = ctx.call("nav.forward", {"steps": int(ctx.opt("steps", 1) or 1)}, timeout=ctx.timeout(15))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.navigated", url=data.get("url"), title=data.get("title") or ""))
    return 0


def cmd_reload(ctx):
    data = ctx.call("nav.reload", {"hard": bool(ctx.opt("hard", False))}, timeout=ctx.timeout(15))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.reloaded"))
    return 0


# ---------------------------------------------------------------- 页面信息
def cmd_page(ctx):
    data = ctx.call("page.info", {}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    viewport = data.get("viewport") or {}
    scroll = data.get("scroll") or {}
    ctx.out.kv([
        (ctx.text("field.url"), data.get("url")),
        (ctx.text("field.title"), data.get("title")),
        (ctx.text("field.state"), data.get("readyState")),
        (ctx.text("field.viewport"), "%sx%s" % (viewport.get("w"), viewport.get("h"))),
        (ctx.text("field.scroll"), "%s / %s" % (scroll.get("y"), scroll.get("height"))),
        (ctx.text("field.lang"), data.get("lang") or "-"),
        (ctx.text("field.counts"), ctx.text(
            "msg.page_counts",
            links=data.get("links", 0), forms=data.get("forms", 0),
            images=data.get("images", 0), elements=data.get("elements", 0))),
    ])
    return 0


# ---------------------------------------------------------------- 截图
def cmd_shot(ctx):
    fmt = (ctx.opt("format") or "png").lower()
    quality = int(ctx.opt("quality", 85) or 85)
    data = ctx.call("capture.screenshot", {"format": fmt, "quality": quality}, timeout=ctx.timeout(15))
    image = data.get("image") or ""
    if not image.startswith("data:image"):
        raise DomateError("E_CAPTURE", message="插件未返回图像数据")

    header, _, payload = image.partition(",")
    raw = base64.b64decode(payload)

    path = ctx.opt("path")
    if path:
        path = os.path.abspath(path)
    else:
        folder = ctx.config.abs_path(ctx.config.get("screenshot.dir", "screenshots"))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("shot-%Y%m%d-%H%M%S") + "." + fmt)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fp:
        fp.write(raw)

    result = {"path": path, "bytes": len(raw), "format": fmt,
              "viewport": data.get("viewport")}
    if ctx.emit(result):
        return 0
    ctx.out.ok(ctx.text("msg.shot_saved", path=path, size=len(raw)))
    return 0


# ---------------------------------------------------------------- 日志
def cmd_console(ctx):
    data = ctx.call("log.console", {
        "clear": bool(ctx.opt("clear", False)),
        "limit": int(ctx.opt("limit", 0) or 100),
    }, timeout=ctx.timeout(3))
    entries = data.get("entries") or []
    if ctx.emit(data):
        return 0
    if not entries:
        ctx.out.warn(ctx.text("msg.console_empty"))
        return 0
    for entry in entries:
        level = str(entry.get("level") or "log").upper()
        style = {"ERROR": "red", "WARN": "yellow", "DEBUG": "gray"}.get(level, "cyan")
        ctx.out.write("%s %s" % (ctx.out.paint("[%s]" % level, style),
                                 shared.clip(entry.get("text") or "", 200)))
    return 0


def cmd_events(ctx):
    data = ctx.call("log.events", {
        "clear": bool(ctx.opt("clear", False)),
        "limit": int(ctx.opt("limit", 0) or 50),
    }, timeout=ctx.timeout(3))
    entries = data.get("entries") or []
    if ctx.emit(data):
        return 0
    if not entries:
        ctx.out.warn(ctx.text("msg.events_empty"))
        return 0
    rows = []
    for entry in entries:
        rows.append([
            entry.get("t"),
            entry.get("type"),
            entry.get("target") or "",
            shared.clip(entry.get("value") or "", 50),
        ])
    ctx.out.table(
        [ctx.text("field.time"), ctx.text("field.event"),
         ctx.text("field.target"), ctx.text("field.value")],
        rows, align=["right", "left", "left", "left"],
    )
    return 0


def register(subparsers, common):
    i18n = common["i18n"]
    global_parser = common["global"]

    p = subparsers.add_parser(
        "page", parents=[global_parser],
        help=i18n.t("cmd.page.brief"), description=i18n.t("cmd.page.desc"))
    p.set_defaults(handler=cmd_page)

    p = subparsers.add_parser(
        "goto", parents=[global_parser],
        help=i18n.t("cmd.goto.brief"), description=i18n.t("cmd.goto.desc"))
    p.add_argument("url", help=i18n.t("cmd.goto.arg.url"))
    p.add_argument("--no-wait", action="store_true", help=i18n.t("cmd.goto.opt.no_wait"))
    p.set_defaults(handler=cmd_goto)

    p = subparsers.add_parser(
        "back", parents=[global_parser],
        help=i18n.t("cmd.back.brief"), description=i18n.t("cmd.back.desc"))
    p.add_argument("--steps", type=int, default=1, help=i18n.t("cmd.back.opt.steps"))
    p.set_defaults(handler=cmd_back)

    p = subparsers.add_parser(
        "forward", parents=[global_parser],
        help=i18n.t("cmd.forward.brief"), description=i18n.t("cmd.forward.desc"))
    p.add_argument("--steps", type=int, default=1, help=i18n.t("cmd.forward.opt.steps"))
    p.set_defaults(handler=cmd_forward)

    p = subparsers.add_parser(
        "reload", parents=[global_parser],
        help=i18n.t("cmd.reload.brief"), description=i18n.t("cmd.reload.desc"))
    p.add_argument("--hard", action="store_true", help=i18n.t("cmd.reload.opt.hard"))
    p.set_defaults(handler=cmd_reload)

    p = subparsers.add_parser(
        "shot", parents=[global_parser],
        help=i18n.t("cmd.shot.brief"), description=i18n.t("cmd.shot.desc"))
    p.add_argument("path", nargs="?", help=i18n.t("cmd.shot.arg.path"))
    p.add_argument("--format", default="png", choices=["png", "jpeg"], help=i18n.t("cmd.shot.opt.format"))
    p.add_argument("--quality", type=int, default=85, help=i18n.t("cmd.shot.opt.quality"))
    p.set_defaults(handler=cmd_shot)

    p = subparsers.add_parser(
        "console", parents=[global_parser],
        help=i18n.t("cmd.console.brief"), description=i18n.t("cmd.console.desc"))
    p.add_argument("--clear", action="store_true", help=i18n.t("cmd.console.opt.clear"))
    p.add_argument("--limit", type=int, default=0, help=i18n.t("cmd.console.opt.limit"))
    p.set_defaults(handler=cmd_console)

    p = subparsers.add_parser(
        "events", parents=[global_parser],
        help=i18n.t("cmd.events.brief"), description=i18n.t("cmd.events.desc"))
    p.add_argument("--clear", action="store_true", help=i18n.t("cmd.events.opt.clear"))
    p.add_argument("--limit", type=int, default=0, help=i18n.t("cmd.events.opt.limit"))
    p.set_defaults(handler=cmd_events)
