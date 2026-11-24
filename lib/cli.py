# -*- coding: utf-8 -*-
"""命令行入口：参数解析、上下文装配、异常与退出码处理。

退出码约定（方便 AI/脚本判断）：
    0  成功
    1  未找到元素/空结果等业务性失败
    2  用法错误 / 服务未启动
    3  超时
    4  浏览器未连接
    5  权限或配置禁用
"""

import argparse
import os
import sys

from . import helpdoc
from .commands import act_cmd, dom_cmd, page_cmd, script_cmd, sys_cmd, tab_cmd
from .config import Config
from .context import Ctx
from .errors import DomateError
from .i18n import I18n
from .output import Console
from .rpc import Rpc
from .store import State

EXIT_CODES = {
    "E_USAGE": 2,
    "E_REQUEST": 2,
    "E_SELECTOR": 2,
    "E_CONFIG": 2,
    "E_LANG": 2,
    "E_JSON": 2,
    "E_DAEMON": 2,
    "E_DAEMON_DOWN": 2,
    "E_DAEMON_START": 2,
    "E_PORT": 2,
    "E_NO_BROWSER": 4,
    "E_BROWSER_GONE": 4,
    "E_TIMEOUT": 3,
    "E_DISABLED": 5,
    "E_PERM": 5,
    "E_NO_TAB": 1,
    "E_ELEMENT": 1,
    "E_SELECTOR_MISS": 1,
}


def _prescan(argv):
    """在构建 parser 之前先取出 --lang/--port，保证帮助文本语言正确。"""
    lang = None
    port = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--lang" and i + 1 < len(argv):
            lang = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--lang="):
            lang = arg.split("=", 1)[1]
        elif arg == "--port" and i + 1 < len(argv):
            port = argv[i + 1]
            i += 2
            continue
        elif arg.startswith("--port="):
            port = arg.split("=", 1)[1]
        i += 1
    return lang, port


def build_parser(i18n):
    """构建 argparse 解析树；每个子命令都挂上全局参数。"""
    common = {"i18n": i18n, "parsers": {}}

    global_parser = argparse.ArgumentParser(add_help=False)
    global_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                               help=i18n.t("arg.json"))
    global_parser.add_argument("--tab", default=argparse.SUPPRESS,
                               help=i18n.t("arg.tab"))
    global_parser.add_argument("--timeout", type=float, default=argparse.SUPPRESS,
                               help=i18n.t("arg.timeout"))
    global_parser.add_argument("--lang", default=argparse.SUPPRESS, help=i18n.t("arg.lang"))
    global_parser.add_argument("--port", type=int, default=argparse.SUPPRESS, help=i18n.t("arg.port"))
    global_parser.add_argument("--color", choices=["auto", "always", "never"],
                               default=argparse.SUPPRESS, help=i18n.t("arg.color"))
    common["global"] = global_parser

    parser = argparse.ArgumentParser(
        prog="domate",
        parents=[global_parser],
        description=i18n.t("help.title", name="DOMate", version=i18n.meta().get("version", "")),
        add_help=True,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<命令>")

    for module in (tab_cmd, dom_cmd, act_cmd, script_cmd, page_cmd):
        module.register(subparsers, common)
    sys_cmd.register(subparsers, common)

    _collect(parser, "", common["parsers"])
    common["root"] = parser
    return parser, common


def _collect(parser, prefix, out):
    for action in parser._actions:  # noqa: SLF001 - 反射子命令树用于帮助
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                full = ("%s %s" % (prefix, name)).strip()
                out[full] = sub
                _collect(sub, full, out)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    config = Config()
    lang, port = _prescan(argv)
    if lang:
        config.set("language", lang)
    if port:
        config.set("server.port", int(port))

    i18n = I18n(config.lang)
    parser, common = build_parser(i18n)

    if not argv:
        out = Console(config.get("output.color", "auto"), False, config.get("output.max_text", 4000))
        ctx = Ctx(config, i18n, out, argparse.Namespace(), Rpc(config, i18n), State())
        ctx.parsers = common["parsers"]
        return helpdoc.print_overview(ctx)

    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2

    # 全局参数可能只出现在父级，这里统一取值
    if getattr(args, "lang", None):
        config.set("language", args.lang)
        i18n.reload(args.lang)
    if getattr(args, "port", None):
        config.set("server.port", int(args.port))

    color = getattr(args, "color", None) or config.get("output.color", "auto")
    out = Console(color, bool(getattr(args, "json", False)), config.get("output.max_text", 4000))

    state = State()
    ctx = Ctx(config, i18n, out, args, Rpc(config, i18n, notify=out.note), state)
    ctx.parsers = common["parsers"]

    try:
        return int(args.handler(ctx) or 0)
    except DomateError as exc:
        _report(out, i18n, exc)
        return EXIT_CODES.get(exc.code, 1)
    except KeyboardInterrupt:
        out.write("")
        out.warn(i18n.t("msg.interrupted"))
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，避免抛栈吓人
        if os.environ.get("DOMATE_DEBUG"):
            raise
        out.error(i18n.t("msg.internal", error=exc), i18n.t("err.hint.debug"))
        return 1


def _report(out, i18n, exc):
    """统一的错误输出：码 + 原因 + 建议（--json 时改成结构化）。"""
    message = exc.message(i18n)
    hint = exc.hint_text(i18n)
    if getattr(out, "json_mode", False):
        payload = {"ok": False, "error": {"code": exc.code, "message": message}}
        if hint:
            payload["error"]["hint"] = hint
        out.json_out(payload)
        return
    out.error("[%s] %s" % (exc.code, message), hint)


if __name__ == "__main__":
    sys.exit(main())
