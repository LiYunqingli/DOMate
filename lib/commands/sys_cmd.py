# -*- coding: utf-8 -*-
"""系统类命令：服务生命周期、状态、配置、自检、帮助。"""

import json
import os
import platform
import socket
import sys
import time

from .. import helpdoc
from ..config import coerce
from ..errors import DomateError
from ..i18n import SUPPORTED as LANGS


# ---------------------------------------------------------------- serve / stop
def cmd_serve(ctx):
    """启动本地服务（前台运行，供 debug 或 systemd/supervisor 托管）。"""
    from .. import daemon
    port = ctx.opt("port", None)
    if port:
        ctx.config.set("server.port", int(port))
    foreground = not ctx.opt("daemon", False)
    return daemon.run(ctx.config, ctx.i18n, ctx.state, foreground=foreground)


def cmd_up(ctx):
    """确保服务在跑，并等浏览器插件连上（预热，消除首条命令的握手等待）。"""
    started = time.time()
    if ctx.opt("force"):
        ctx.rpc.stop()
        time.sleep(0.2)
    status = ctx.rpc.ensure()                      # 没启动就在这里拉起
    budget = ctx.opt("wait")
    budget = 10.0 if budget is None else max(0.0, float(budget))

    browser = status.get("browser") or {}
    deadline = time.time() + budget
    while not browser.get("connected") and time.time() < deadline:
        time.sleep(0.15)
        latest = ctx.rpc.health()
        if latest:
            status, browser = latest, latest.get("browser") or {}
    elapsed = round(time.time() - started, 2)
    ready = bool(browser.get("connected"))

    if ctx.emit({"ready": ready, "elapsed": elapsed,
                 "server": status.get("server"), "browser": browser}):
        return 0 if ready else 2

    if ready:
        ctx.out.ok(ctx.text("msg.up_ready", elapsed=elapsed,
                            browser=browser.get("browser") or "-",
                            tabs=len(status.get("tabs") or [])))
        return 0
    ctx.out.warn(ctx.text("msg.up_timeout", timeout=budget))
    ctx.out.write(ctx.out.paint(ctx.text("err.hint.browser"), "gray"))
    return 2


def cmd_stop(ctx):
    ok = ctx.rpc.stop()
    if ctx.json_mode:
        ctx.emit({"stopped": ok})
        return 0
    if ok:
        ctx.out.ok(ctx.text("msg.daemon_stopped"))
    else:
        ctx.out.warn(ctx.text("msg.daemon_not_running"))
    return 0


def cmd_status(ctx):
    health = ctx.rpc.health()
    if not health:
        data = {"running": False, "browser": {"connected": False}, "tabs": []}
        if ctx.json_mode:
            ctx.emit(data)
        else:
            ctx.out.error(ctx.text("msg.daemon_down"), ctx.text("err.hint.daemon"))
        return 2
    return _render_status(ctx, health)


def _render_status(ctx, health):
    browser = health.get("browser") or {}
    tabs = health.get("tabs") or []
    server = health.get("server") or {}
    data = {
        "running": True,
        "server": server,
        "browser": browser,
        "tabs": len(tabs),
        "selected": health.get("selected"),
    }
    if ctx.emit(data):
        return 0

    t = ctx.text
    ctx.out.title(t("msg.status_title"))
    ctx.out.kv([
        (t("field.server"), "%s:%s  pid=%s" % (ctx.config.host, server.get("port"), server.get("pid"))),
        (t("field.uptime"), _duration(server.get("uptime", 0))),
        (t("field.version"), server.get("version", "")),
    ])
    if browser.get("connected"):
        rows = [
            (t("field.browser"), "%s %s" % (browser.get("browser") or "-", browser.get("version") or "")),
            (t("field.conn"), browser.get("kind") or "-"),
            (t("field.tabs"), str(len(tabs))),
            (t("field.selected"), str(health.get("selected") or "-")),
        ]
        if browser.get("reconnects"):
            rows.append((t("field.reconnects"), str(browser.get("reconnects"))))
        ctx.out.kv(rows)
    else:
        ctx.out.warn(t("msg.browser_offline"))
        last = browser.get("lastDisconnect") or {}
        if last.get("at"):
            ctx.out.write(ctx.out.paint(t("msg.browser_dropped_at",
                                           time=time.strftime("%H:%M:%S", time.localtime(last["at"]))),
                                        "gray"))
    return 0


def _duration(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%ds" % (seconds // 60, seconds % 60)
    return "%dh%dm" % (seconds // 3600, (seconds % 3600) // 60)


# ---------------------------------------------------------------- config
def cmd_config(ctx):
    action = (ctx.opt("config_action") or "list").lower()
    key = ctx.opt("key")
    value = ctx.opt("value")

    if action == "list":
        data = ctx.config.to_dict()
        if ctx.emit(data, {"path": ctx.config.path}):
            return 0
        ctx.out.title(ctx.text("msg.config_title", path=ctx.config.path))
        for key_name, val in sorted(_flatten(data).items()):
            ctx.out.write("  %-32s %s" % (key_name, json.dumps(val, ensure_ascii=False)))
        return 0

    if action == "get":
        if not key:
            raise DomateError("E_USAGE", message="config get 需要 key")
        val = ctx.config.get(key)
        if ctx.emit({key: val}):
            return 0
        ctx.out.write("%s = %s" % (key, json.dumps(val, ensure_ascii=False)))
        return 0

    if action == "set":
        if not key or value is None:
            raise DomateError("E_USAGE", message="config set 需要 key 和 value")
        parsed = coerce(value)
        if key == "language" and parsed not in LANGS:
            raise DomateError("E_LANG", message="unsupported language: %s (%s)" % (parsed, ", ".join(LANGS)))
        ctx.config.set(key, parsed).save()
        if ctx.emit({key: parsed, "saved": True}):
            return 0
        ctx.out.ok(ctx.text("msg.config_saved", key=key, value=parsed))
        return 0

    if action == "reset":
        from ..config import DEFAULTS
        import copy
        ctx.config.data = copy.deepcopy(DEFAULTS)
        ctx.config.save()
        if ctx.emit({"reset": True}):
            return 0
        ctx.out.ok(ctx.text("msg.config_reset"))
        return 0

    raise DomateError("E_USAGE", message="未知的 config 操作: %s" % action)


def _flatten(data, prefix=""):
    out = {}
    for key, val in data.items():
        name = prefix + key
        if isinstance(val, dict):
            out.update(_flatten(val, name + "."))
        else:
            out[name] = val
    return out


# ---------------------------------------------------------------- doctor
def cmd_doctor(ctx):
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    py_ok = sys.version_info >= (3, 8)
    add("python", py_ok, platform.python_version())
    add("platform", True, "%s %s" % (platform.system(), platform.release()))
    add("config", os.path.isfile(ctx.config.path), ctx.config.path)

    port_open = _port_busy(ctx.config.host, ctx.config.port)
    health = ctx.rpc.health()
    add("daemon", health is not None,
        (ctx.config.base_url if health else "port %s %s" % (ctx.config.port, "busy" if port_open else "free")))
    browser = (health or {}).get("browser") or {}
    add("browser", browser.get("connected", False),
        "%s" % (browser.get("browser") or ctx.text("msg.browser_short")))
    tabs = (health or {}).get("tabs") or []
    add("tabs", bool(tabs), "%d" % len(tabs))
    log_dir = os.path.dirname(ctx.config.abs_path(ctx.config.get("server.log_file", "logs/domate.log")))
    add("logdir", os.path.isdir(log_dir) or _writable(log_dir), log_dir)
    add("lang", ctx.i18n.lang in LANGS, ctx.i18n.lang)

    if ctx.emit({"checks": checks}):
        return 0 if all(c["ok"] for c in checks) else 2

    ctx.out.title(ctx.text("msg.doctor_title"))
    for check in checks:
        mark = ctx.out.paint("✔", "green") if check["ok"] else ctx.out.paint("✘", "red")
        ctx.out.write("  %s %-10s %s" % (mark, check["name"], ctx.out.paint(check["detail"], "gray")))
    failed = [c["name"] for c in checks if not c["ok"]]
    if failed:
        ctx.out.warn(ctx.text("msg.doctor_failed", names=", ".join(failed)))
        return 2
    ctx.out.ok(ctx.text("msg.doctor_ok"))
    return 0


def _port_busy(host, port):
    sock = socket.socket()
    sock.settimeout(0.4)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def _writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w", encoding="utf-8") as fp:
            fp.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- help
def cmd_help(ctx):
    topic = ctx.opt("topic")
    if isinstance(topic, (list, tuple)):
        topic = " ".join(topic) if topic else None
    if ctx.opt("guide"):
        return helpdoc.print_guide(ctx)
    if topic:
        return helpdoc.print_command(ctx, topic)
    return helpdoc.print_overview(ctx)


def cmd_version(ctx):
    meta = ctx.i18n.meta()
    if ctx.emit(meta):
        return 0
    ctx.out.write("%s %s" % (meta.get("name", "DOMate"), meta.get("version", "")))
    return 0


# ---------------------------------------------------------------- 注册
def register(subparsers, common):
    i18n = common["i18n"]

    def add(name, func, help_key, **kwargs):
        parser = subparsers.add_parser(
            name,
            parents=[common["global"]],
            help=i18n.t(help_key),
            description=i18n.t(help_key),
            **kwargs
        )
        parser.set_defaults(handler=func)
        return parser

    p = add("serve", cmd_serve, "cmd.serve.brief")
    p.add_argument("--foreground", action="store_true", help=i18n.t("cmd.serve.opt.foreground"))
    p.add_argument("--daemon", action="store_true", help=i18n.t("cmd.serve.opt.daemon"))

    p = add("up", cmd_up, "cmd.up.brief")
    p.add_argument("--wait", type=float, default=None, help=i18n.t("cmd.up.opt.wait"))
    p.add_argument("--force", action="store_true", help=i18n.t("cmd.up.opt.force"))

    add("stop", cmd_stop, "cmd.stop.brief")
    add("status", cmd_status, "cmd.status.brief")
    add("doctor", cmd_doctor, "cmd.doctor.brief")
    add("version", cmd_version, "cmd.version.brief")

    p = add("config", cmd_config, "cmd.config.brief")
    p.add_argument("config_action", nargs="?", default="list",
                   choices=["list", "get", "set", "reset"], help=i18n.t("cmd.config.arg.action"))
    p.add_argument("key", nargs="?", help=i18n.t("cmd.config.arg.key"))
    p.add_argument("value", nargs="?", help=i18n.t("cmd.config.arg.value"))

    p = add("help", cmd_help, "cmd.help.brief")
    p.add_argument("topic", nargs="*", help=i18n.t("cmd.help.arg.topic"))
    p.add_argument("--guide", action="store_true", help=i18n.t("cmd.help.opt.guide"))
