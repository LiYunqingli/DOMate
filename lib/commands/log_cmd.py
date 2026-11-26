# -*- coding: utf-8 -*-
"""日志类命令：看板服务、统计、tail、路径、归档。

这一组命令不驱动浏览器，只读日志文件，所以不需要服务在跑。
"""

import json
import os
import re
import sys
import time

from .. import helpdoc, logbook
from ..config import ROOT
from ..errors import DomateError


def _ensure_root():
    """logview 是项目根目录下的独立包，保证以 -m 方式运行时也能导入。"""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)


def _paths(ctx):
    json_path = ctx.config.abs_path(ctx.config.get("server.log_json", "logs/domate.jsonl"))
    text_path = ctx.config.abs_path(ctx.config.get("server.log_file", "logs/domate.log"))
    return json_path, text_path


def _store(ctx, window=None):
    _ensure_root()
    from logview import parser as logparser
    json_path, text_path = _paths(ctx)
    size = window or int(ctx.config.get("dashboard.window", 30000) or 30000)
    store = logparser.LogStore(json_path, text_path, window=size)
    store.load()
    return store


def _parse_since(text):
    """'1h' / '30m' / '2d' / '90s' -> 起始时间戳；空 -> None。"""
    if not text:
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])?\s*$", str(text).lower())
    if not match:
        raise DomateError("E_USAGE", message="时间格式应为 15m / 2h / 3d / 600s：%s" % text)
    value = float(match.group(1))
    unit = match.group(2) or "m"
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return time.time() - value * scale


def _human(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f%s" % (size, unit)
        size /= 1024.0
    return "%.1fGB" % size


# ---------------------------------------------------------------- serve
def cmd_log_serve(ctx):
    _ensure_root()
    from logview import server as logserver
    port = ctx.opt("port") or ctx.config.get("dashboard.port", 8899)
    host = ctx.opt("host") or ctx.config.get("dashboard.host", "127.0.0.1")
    window = ctx.opt("window") or None
    try:
        return logserver.serve(ctx.config, host=host, port=int(port),
                               window=int(window) if window else None,
                               open_browser=bool(ctx.opt("open")))
    except SystemExit as exc:
        ctx.out.error(str(exc))
        return 2


# ---------------------------------------------------------------- stats
def cmd_log_stats(ctx):
    from logview import parser as logparser
    store = _store(ctx, ctx.opt("window") or None)
    if not store.records:
        if ctx.emit({"events": 0}):
            return 0
        ctx.out.warn(ctx.text("msg.log_empty", path=_paths(ctx)[0]))
        return 0

    filters = logparser.build_filters({})
    since = _parse_since(ctx.opt("since"))
    if since:
        filters["from"] = since
    data = logparser.aggregate(store.records, filters, limit_recent=20, limit_errors=10)

    if ctx.emit(data):
        return 0

    k = data["kpi"]
    ctx.out.title(ctx.text("msg.log_stats_title", n=store.parsed))
    ctx.out.kv([
        (ctx.text("field.log_window"), "%s → %s" % (
            time.strftime("%m-%d %H:%M:%S", time.localtime(data["window"]["from"] or 0)),
            time.strftime("%m-%d %H:%M:%S", time.localtime(data["window"]["to"] or 0)))),
        (ctx.text("field.events"), "%d  (info %d / warn %d / error %d / debug %d)" % (
            k["total"], k["info"], k["warn"], k["error"], k["debug"])),
        (ctx.text("field.fail"), "%d  (%.2f%%)" % (k["fail"], k["fail_rate"])),
        (ctx.text("field.latency"), "avg %sms  p50 %sms  p95 %sms  p99 %sms  max %sms" % (
            k["avg_ms"], k["p50_ms"], k["p95_ms"], k["p99_ms"], k["max_ms"])),
    ])
    ctx.out.write("")

    if data["cats"]:
        ctx.out.write(ctx.out.paint(ctx.text("msg.log_cats"), "bold"))
        ctx.out.table([ctx.text("field.category"), ctx.text("field.events"), ctx.text("field.fail")],
                      [[c["cat"], c["n"], c["fail"]] for c in data["cats"]],
                      align=["left", "right", "right"])
        ctx.out.write("")

    if data["actions"]:
        ctx.out.write(ctx.out.paint(ctx.text("msg.log_actions"), "bold"))
        ctx.out.table(["action", "n", "fail%", "avg", "p95", "max"],
                      [[a["action"], a["n"], a["fail_rate"], a["avg"], a["p95"], a["max"]]
                       for a in data["actions"][:12]],
                      align=["left", "right", "right", "right", "right", "right"])
        ctx.out.write("")

    if data["errors"]:
        ctx.out.write(ctx.out.paint(ctx.text("msg.log_recent_errors"), "bold"))
        for rec in data["errors"][:8]:
            ctx.out.write("  %s %s %s" % (
                ctx.out.paint(rec["t"][11:23], "gray"),
                ctx.out.paint(str(rec.get("code") or rec["lv"]).ljust(12), "red"),
                (rec.get("msg") or "")[:100]))
        ctx.out.write("")

    ctx.out.write(ctx.out.paint(ctx.text("msg.log_open_hint"), "gray"))
    return 0


# ---------------------------------------------------------------- tail
def cmd_log_tail(ctx):
    from logview import parser as logparser
    limit = int(ctx.opt("lines", 40) or 40)
    follow = bool(ctx.opt("follow"))
    store = _store(ctx)
    filters = logparser.build_filters({"lv": [ctx.opt("level") or ""],
                                       "cat": [ctx.opt("cat") or ""],
                                       "q": [ctx.opt("q") or ""],
                                       "poll": ["1"]})
    shown = set()
    first = True
    try:
        while True:
            rows = [r for r in store.snapshot() if logparser.match(r, filters)]
            fresh = [r for r in rows if r["i"] not in shown]
            if first:
                fresh = fresh[-limit:]
                first = False
            if fresh:
                for rec in fresh:
                    shown.add(rec["i"])
                if ctx.json_mode:
                    for rec in fresh:
                        ctx.out.write(json.dumps(rec, ensure_ascii=False))
                else:
                    for rec in fresh:
                        colour = {"error": "red", "warn": "yellow", "debug": "gray"}.get(rec["lv"], "cyan")
                        dur = rec.get("dur_ms")
                        ctx.out.write("%s %s %-6s %s%s" % (
                            ctx.out.paint(rec["t"][11:23], "gray"),
                            ctx.out.paint(rec["lv"].upper().ljust(5), colour),
                            rec["cat"],
                            (rec.get("msg") or "")[:110],
                            ctx.out.paint("  %sms" % dur, "gray") if isinstance(dur, (int, float)) else ""))
                if not follow:
                    return 0
            if not follow:
                if not fresh:
                    ctx.out.warn(ctx.text("msg.log_empty", path=_paths(ctx)[0]))
                return 0
            time.sleep(0.4)
            store.poll()
    except KeyboardInterrupt:
        return 0


# ---------------------------------------------------------------- path
def cmd_log_path(ctx):
    json_path, text_path = _paths(ctx)
    info = []
    for kind, path in (("jsonl", json_path), ("text", text_path)):
        exists = os.path.isfile(path)
        size = os.path.getsize(path) if exists else 0
        info.append({"kind": kind, "path": path, "exists": exists, "size": size})
        # 轮转出来的旧档也列出来，方便排查
        index = 1
        while os.path.isfile("%s.%d" % (path, index)):
            info.append({"kind": "%s.%d" % (kind, index),
                         "path": "%s.%d" % (path, index), "exists": True,
                         "size": os.path.getsize("%s.%d" % (path, index))})
            index += 1

    if ctx.emit({"files": info, "root": ROOT}):
        return 0
    ctx.out.title(ctx.text("msg.log_path_title"))
    for item in info:
        mark = ctx.out.paint("✔", "green") if item["exists"] else ctx.out.paint("✘", "red")
        ctx.out.write("  %s %-8s %8s  %s" % (mark, item["kind"], _human(item["size"]), item["path"]))
    ctx.out.write("")
    ctx.out.write(ctx.out.paint(ctx.text("msg.log_open_hint"), "gray"))
    return 0


# ---------------------------------------------------------------- clear
def cmd_log_clear(ctx):
    """归档而不是删除：重命名成带时间戳的文件，随时能捞回来。"""
    json_path, text_path = _paths(ctx)
    targets = [p for p in (json_path, text_path) if os.path.isfile(p)]
    if not targets:
        if ctx.emit({"archived": []}):
            return 0
        ctx.out.warn(ctx.text("msg.log_empty", path=json_path))
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    planned = [("%s.%s.bak" % (p, stamp), p) for p in targets]
    if not ctx.opt("yes"):
        if ctx.emit({"planned": [{"from": src, "to": dst} for dst, src in planned]}):
            return 0
        ctx.out.title(ctx.text("msg.log_clear_title"))
        for dst, src in planned:
            ctx.out.write("  %s\n    -> %s" % (src, dst))
        ctx.out.warn(ctx.text("msg.log_clear_confirm"))
        return 2

    archived = []
    for dst, src in planned:
        try:
            os.replace(src, dst)
            archived.append({"from": src, "to": dst})
        except OSError as exc:
            raise DomateError("E_FILE", message="归档失败: %s (%s)" % (src, exc))

    logbook.reset()
    if ctx.emit({"archived": archived}):
        return 0
    for item in archived:
        ctx.out.ok(ctx.text("msg.log_archived", path=item["to"]))
    return 0


# ---------------------------------------------------------------- 注册
def register(subparsers, common):
    i18n = common["i18n"]
    global_parser = common["global"]

    parser = subparsers.add_parser(
        "log", parents=[global_parser],
        help=i18n.t("cmd.log.brief"), description=i18n.t("cmd.log.desc"))
    sub = parser.add_subparsers(dest="log_action", metavar="<操作>")
    parser.set_defaults(handler=lambda ctx: helpdoc.print_command(ctx, "log"))

    p = sub.add_parser("serve", parents=[global_parser],
                       help=i18n.t("cmd.log_serve.brief"),
                       description=i18n.t("cmd.log_serve.desc"))
    p.add_argument("--host", default=None, help=i18n.t("cmd.log_serve.opt.host"))
    p.add_argument("--window", type=int, default=None, help=i18n.t("cmd.log_serve.opt.window"))
    p.add_argument("--open", action="store_true", help=i18n.t("cmd.log_serve.opt.open"))
    p.set_defaults(handler=cmd_log_serve)

    p = sub.add_parser("stats", parents=[global_parser],
                       help=i18n.t("cmd.log_stats.brief"),
                       description=i18n.t("cmd.log_stats.desc"))
    p.add_argument("--since", default=None, help=i18n.t("cmd.log_stats.opt.since"))
    p.add_argument("--window", type=int, default=None, help=i18n.t("cmd.log_serve.opt.window"))
    p.set_defaults(handler=cmd_log_stats)

    p = sub.add_parser("tail", parents=[global_parser],
                       help=i18n.t("cmd.log_tail.brief"),
                       description=i18n.t("cmd.log_tail.desc"))
    p.add_argument("-n", "--lines", type=int, default=40, help=i18n.t("cmd.log_tail.opt.lines"))
    p.add_argument("-f", "--follow", action="store_true", help=i18n.t("cmd.log_tail.opt.follow"))
    p.add_argument("--level", default=None, choices=["debug", "info", "warn", "error"],
                   help=i18n.t("cmd.log_tail.opt.level"))
    p.add_argument("--cat", default=None, help=i18n.t("cmd.log_tail.opt.cat"))
    p.add_argument("--q", default=None, help=i18n.t("cmd.log_tail.opt.q"))
    p.set_defaults(handler=cmd_log_tail)

    p = sub.add_parser("path", parents=[global_parser],
                       help=i18n.t("cmd.log_path.brief"),
                       description=i18n.t("cmd.log_path.desc"))
    p.set_defaults(handler=cmd_log_path)

    p = sub.add_parser("clear", parents=[global_parser],
                       help=i18n.t("cmd.log_clear.brief"),
                       description=i18n.t("cmd.log_clear.desc"))
    p.add_argument("-y", "--yes", action="store_true", help=i18n.t("cmd.log_clear.opt.yes"))
    p.set_defaults(handler=cmd_log_clear)

    return parser
