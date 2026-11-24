# -*- coding: utf-8 -*-
"""service 命令组：把本地服务装成开机自启的后台服务。

这些命令只做「注册/启停」这类系统级动作，真正的服务进程仍然由
`domate serve` 承载，和临时拉起的是同一份代码。
"""

import time

from .. import service
from ..errors import DomateError


# ---------------------------------------------------------------- 各子命令
def cmd_install(ctx):
    dry = ctx.opt("dry_run", False)
    no_start = ctx.opt("no_start", False)
    if dry:
        ok, steps = service.install(dry_run=True)
    else:
        # 临时拉起的 daemon 会占住端口，先让它退场，把端口让给常驻服务
        ctx.rpc.stop()
        time.sleep(0.2)
        ok, steps = service.install(start=not no_start)
        if ok and not no_start and not service.managed_by_os():
            # 启动项只在登录时生效，装完立刻拉起一次，省得用户再等下次登录
            try:
                ctx.rpc.ensure(autostart=True)
                steps.append(("run", " ".join(service.serve_argv())))
            except DomateError as exc:
                steps.append(("error", str(exc)))

    if ctx.emit({"ok": ok, "kind": service.kind(), "dry_run": dry,
                 "steps": [{"action": a, "detail": d} for a, d in steps]}):
        return 0 if ok else 2

    ctx.out.title(ctx.text("msg.service_install_title", kind=_kind_label(ctx)))
    _render_steps(ctx, steps)
    if not ok:
        ctx.out.warn(ctx.text("msg.service_failed"))
        return 2
    if dry:
        ctx.out.ok(ctx.text("msg.service_dry_run"))
        return 0
    ctx.out.ok(ctx.text("msg.service_installed"))
    ctx.out.write(ctx.out.paint(ctx.text("msg.service_next_hint"), "gray"))
    return 0


def cmd_uninstall(ctx):
    dry = ctx.opt("dry_run", False)
    ok, steps = service.uninstall(dry_run=dry)
    if ctx.emit({"ok": ok, "kind": service.kind(), "dry_run": dry,
                 "steps": [{"action": a, "detail": d} for a, d in steps]}):
        return 0 if ok else 2

    ctx.out.title(ctx.text("msg.service_uninstall_title", kind=_kind_label(ctx)))
    _render_steps(ctx, steps)
    if not ok:
        ctx.out.warn(ctx.text("msg.service_failed"))
        return 2
    if dry:
        ctx.out.ok(ctx.text("msg.service_dry_run"))
        return 0
    ctx.out.ok(ctx.text("msg.service_uninstalled"))
    return 0


def _kind_label(ctx):
    """把内部标记翻成人话（startup / systemd / launchd）。"""
    return ctx.text("msg.kind_%s" % service.kind())


def _render_steps(ctx, steps):
    """把 (动作, 说明) 列表渲染成带标记的行。"""
    marks = {"error": ("✘", "red"), "skip": ("·", "gray"), "remove": ("–", "gray")}
    for action, detail in steps:
        mark, color = marks.get(action, ("✔", "green"))
        ctx.out.write("  %s %s" % (ctx.out.paint(mark, color), ctx.out.paint(detail, "gray")))


def cmd_start(ctx):
    health = ctx.rpc.health()
    if health:
        if ctx.emit({"started": True, "already": True, "server": health.get("server")}):
            return 0
        ctx.out.warn(ctx.text("msg.serve_already", port=ctx.config.port,
                              pid=(health.get("server") or {}).get("pid")))
        return 0

    if service.managed_by_os():
        ok, output = service.start()
    else:
        # 启动项只负责「登录时拉起」，现在要立刻起就派生子进程（同样脱离终端）
        try:
            ctx.rpc.ensure(autostart=True)
            ok, output = True, ""
        except DomateError as exc:
            ok, output = False, str(exc)

    if ctx.emit({"started": ok, "detail": output}):
        return 0 if ok else 2
    if ok:
        ctx.out.ok(ctx.text("msg.service_started"))
        return 0
    ctx.out.warn(output or ctx.text("msg.service_failed"))
    return 2


def cmd_stop(ctx):
    # 先走 HTTP 优雅退出，让服务自己收尾（关连接、写日志）
    graceful = ctx.rpc.stop()
    managed = service.managed_by_os()
    ok = None
    if managed:
        ok, _ = service.stop()
    if ctx.emit({"stopped": bool(graceful or ok), "graceful": graceful}):
        return 0
    if graceful or ok:
        ctx.out.ok(ctx.text("msg.daemon_stopped"))
        return 0
    ctx.out.warn(ctx.text("msg.daemon_not_running"))
    return 0


def cmd_restart(ctx):
    ctx.rpc.stop()
    time.sleep(0.3)
    if service.managed_by_os():
        ok, output = service.start()
    else:
        try:
            ctx.rpc.ensure(autostart=True)
            ok, output = True, ""
        except DomateError as exc:
            ok, output = False, str(exc)
    if ctx.emit({"restarted": ok, "detail": output}):
        return 0 if ok else 2
    if ok:
        ctx.out.ok(ctx.text("msg.service_started"))
        return 0
    ctx.out.warn(output or ctx.text("msg.service_failed"))
    return 2


def cmd_status(ctx):
    spec = service.plan()
    registered = service.registered()
    health = ctx.rpc.health()
    running = health is not None
    data = {
        "kind": spec["kind"],
        "registered": registered,
        "running": running,
        "detail": spec["path"] or spec["detail"],
        "server": (health or {}).get("server"),
        "browser": (health or {}).get("browser"),
    }
    if ctx.emit(data):
        return 0 if registered else 2

    t = ctx.text
    ctx.out.title(t("msg.service_status_title"))
    ctx.out.kv([
        (t("field.service_kind"), _kind_label(ctx)),
        (t("field.service_registered"), t("msg.yes") if registered else t("msg.no")),
        (t("field.service_running"), t("msg.yes") if running else t("msg.no")),
        (t("field.service_detail"), spec["path"] or spec["detail"]),
    ])
    if not registered:
        ctx.out.write(ctx.out.paint(t("msg.service_not_installed"), "gray"))
        return 2
    return 0


# ---------------------------------------------------------------- 注册
def register(subparsers, common):
    """注册 `domate service <子命令>`。"""
    i18n = common["i18n"]
    global_parser = common["global"]

    group = subparsers.add_parser(
        "service", help=i18n.t("cmd.service.brief"), description=i18n.t("cmd.service.brief"))
    sub = group.add_subparsers(dest="service_action", metavar="<子命令>")
    group.set_defaults(handler=_no_subcommand(group))

    def add(name, func):
        parser = sub.add_parser(
            name, parents=[global_parser],
            help=i18n.t("cmd.service_%s.brief" % name.replace("-", "_")),
            description=i18n.t("cmd.service_%s.brief" % name.replace("-", "_")),
        )
        parser.set_defaults(handler=func)
        return parser

    def add_dry_run(parser):
        parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                            help=i18n.t("cmd.service.opt.dry_run"))
        return parser

    add_dry_run(add("install", cmd_install)).add_argument(
        "--no-start", dest="no_start", action="store_true",
        help=i18n.t("cmd.service_install.opt.no_start"))
    add_dry_run(add("uninstall", cmd_uninstall))
    add("status", cmd_status)
    add("start", cmd_start)
    add("stop", cmd_stop)
    add("restart", cmd_restart)


def _no_subcommand(group):
    """没带子命令时打印本组帮助，而不是静默退出。"""
    import sys

    def handler(ctx):
        group.print_help(sys.stderr)
        return 2

    return handler
