# -*- coding: utf-8 -*-
"""标签页类命令：列表、选中、新建、关闭、激活、改标题、刷新。"""

from ..errors import DomateError
from . import shared


# ---------------------------------------------------------------- list / use
def cmd_tab_list(ctx):
    """列出所有标签页；--json 时直接给结构化数组，便于 AI 解析。"""
    data = ctx.call("tab.list", {}, timeout=ctx.timeout(2))
    tabs = data.get("tabs", [])
    selected = data.get("selected")
    if ctx.emit({"tabs": tabs, "count": len(tabs), "selected": selected}):
        return 0

    if not tabs:
        ctx.out.warn(ctx.text("msg.no_tab"))
        return 0

    rows = []
    for tab in tabs:
        flags = []
        if tab.get("active"):
            flags.append(ctx.text("tab.flag.active"))
        if tab.get("pinned"):
            flags.append(ctx.text("tab.flag.pinned"))
        if selected is not None and tab.get("id") == selected:
            flags.append(ctx.text("tab.flag.selected"))
        rows.append([
            tab.get("seq"),
            tab.get("id"),
            _clip(tab.get("title") or "-", 46),
            _clip(tab.get("url") or "-", 58),
            ",".join(flags),
        ])
    ctx.out.table(
        [ctx.text("field.seq"), ctx.text("field.id"), ctx.text("field.title"),
         ctx.text("field.url"), ctx.text("field.flags")],
        rows,
        align=["right", "right", "left", "left", "left"],
    )
    ctx.out.write("")
    ctx.out.info(ctx.text("msg.tab_total", count=len(tabs)))
    return 0


def cmd_tab_use(ctx):
    """选中标签页（后续命令默认作用在它上面）。"""
    target = ctx.need(ctx.opt("target"))
    tabs = ctx.call("tab.list", {}, timeout=ctx.timeout(2)).get("tabs", [])

    chosen = None
    for tab in tabs:
        if str(tab.get("id")) == str(target):
            chosen = tab
            break
    if chosen is None and str(target).isdigit():
        idx = int(target)
        if 1 <= idx <= len(tabs):
            chosen = tabs[idx - 1]
    if chosen is None:
        for tab in tabs:
            if str(target).lower() in (tab.get("title") or "").lower() or \
                    str(target).lower() in (tab.get("url") or "").lower():
                chosen = tab
                break
    if chosen is None:
        raise DomateError("E_NO_TAB", message="no tab matches: %s" % target)

    ctx.state.remember_tab(chosen)
    if ctx.emit({"selected": chosen}):
        return 0
    ctx.out.ok(ctx.text("msg.tab_selected", id=chosen.get("id"), title=chosen.get("title") or ""))
    return 0


def cmd_tab_current(ctx):
    data = ctx.call("tab.current", {}, timeout=ctx.timeout(2))
    if ctx.emit(data):
        return 0
    tab = data.get("tab") or {}
    ctx.out.kv([
        (ctx.text("field.id"), tab.get("id")),
        (ctx.text("field.title"), tab.get("title")),
        (ctx.text("field.url"), tab.get("url")),
        (ctx.text("field.size"), "%sx%s" % (tab.get("width", "?"), tab.get("height", "?"))),
        (ctx.text("field.state"), tab.get("status")),
    ])
    return 0


# ---------------------------------------------------------------- new / close
def cmd_tab_new(ctx):
    url = ctx.opt("url") or "about:blank"
    params = {
        "url": url,
        "active": not ctx.opt("background", False),
        "pinned": bool(ctx.opt("pinned", False)),
    }
    if ctx.opt("window", False):
        params["newWindow"] = True
    data = ctx.call("tab.new", params, timeout=ctx.timeout(5))
    tab = data.get("tab") or {}
    if ctx.opt("select", False) and tab:
        ctx.state.remember_tab(tab)
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.tab_created", id=tab.get("id"), url=tab.get("url")))
    return 0


def cmd_tab_close(ctx):
    ids = [int(i) for i in (ctx.opt("ids") or [])]
    params = {"ids": ids}
    if ctx.opt("others", False):
        params["others"] = True
    if ctx.opt("right", False):
        params["right"] = True
    data = ctx.call("tab.close", params, timeout=ctx.timeout(3))
    closed = data.get("closed") or []
    # 被关掉的正是当前选中标签页时清空选择
    if ctx.state.get("tab_id") in closed:
        ctx.state.clear_tab()
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.tab_closed", count=len(closed), ids=", ".join(str(i) for i in closed)))
    return 0


def cmd_tab_activate(ctx):
    target = ctx.opt("target")
    data = ctx.call("tab.activate", {"target": target} if target else {}, timeout=ctx.timeout(3))
    tab = data.get("tab") or {}
    ctx.state.remember_tab(tab)
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.tab_activated", id=tab.get("id"), title=tab.get("title") or ""))
    return 0


def cmd_tab_title(ctx):
    title = ctx.need(ctx.opt("title"))
    data = ctx.call("tab.title", {"title": title}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.tab_title_set", title=title))
    return 0


def cmd_tab_reload(ctx):
    data = ctx.call("tab.reload", {"hard": bool(ctx.opt("hard", False))}, timeout=ctx.timeout(10))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.tab_reloaded"))
    return 0


def cmd_tab_stop(ctx):
    data = ctx.call("tab.stop", {}, timeout=ctx.timeout(3))
    if ctx.emit(data):
        return 0
    ctx.out.ok(ctx.text("msg.tab_stopped"))
    return 0


def _clip(text, width):
    text = str(text)
    return text if len(text) <= width else text[: width - 1] + "…"


# ---------------------------------------------------------------- 注册
def register(subparsers, common):
    """注册 `domate tab <子命令>`。"""
    i18n = common["i18n"]
    global_parser = common["global"]

    group = subparsers.add_parser(
        "tab", help=i18n.t("cmd.tab.brief"), description=i18n.t("cmd.tab.brief"))
    sub = group.add_subparsers(dest="tab_command", metavar="<子命令>")

    def add(name, func):
        parser = sub.add_parser(
            name, parents=[global_parser],
            help=i18n.t("cmd.tab_%s.brief" % name.replace("-", "_")),
            description=i18n.t("cmd.tab_%s.desc" % name.replace("-", "_")),
        )
        parser.set_defaults(handler=func)
        return parser

    add("list", cmd_tab_list)
    add("current", cmd_tab_current)

    p = add("use", cmd_tab_use)
    p.add_argument("target", help=i18n.t("cmd.tab_use.arg.target"))

    p = add("new", cmd_tab_new)
    p.add_argument("url", nargs="?", default="about:blank", help=i18n.t("cmd.tab_new.arg.url"))
    p.add_argument("--background", action="store_true", help=i18n.t("cmd.tab_new.opt.background"))
    p.add_argument("--pinned", action="store_true", help=i18n.t("cmd.tab_new.opt.pinned"))
    p.add_argument("--window", action="store_true", help=i18n.t("cmd.tab_new.opt.window"))
    p.add_argument("--select", action="store_true", help=i18n.t("cmd.tab_new.opt.select"))

    p = add("close", cmd_tab_close)
    p.add_argument("ids", nargs="*", type=int, help=i18n.t("cmd.tab_close.arg.ids"))
    p.add_argument("--others", action="store_true", help=i18n.t("cmd.tab_close.opt.others"))
    p.add_argument("--right", action="store_true", help=i18n.t("cmd.tab_close.opt.right"))

    p = add("activate", cmd_tab_activate)
    p.add_argument("target", nargs="?", help=i18n.t("cmd.tab_activate.arg.target"))

    p = add("title", cmd_tab_title)
    p.add_argument("title", help=i18n.t("cmd.tab_title.arg.title"))

    p = add("reload", cmd_tab_reload)
    p.add_argument("--hard", action="store_true", help=i18n.t("cmd.tab_reload.opt.hard"))

    add("stop", cmd_tab_stop)
    group.set_defaults(handler=shared.group_fallback(group, i18n))
