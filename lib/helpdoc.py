# -*- coding: utf-8 -*-
"""帮助文档渲染。

帮助内容全部来自语言包（res/lang/<lang>.json）里的 cmd.* 节点，
选项说明直接从 argparse 的 parser 里反射出来，保证「文档 = 实际参数」，
不会出现改了参数忘记改文档的情况。
"""

import os
import sys

from .config import ROOT

#: 帮助总览里的分组（顺序即显示顺序）
GROUPS = [
    ("help.group.start", [
        "up", "status", "serve", "stop", "doctor", "config", "version", "help",
    ]),
    ("help.group.service", [
        "service install", "service status", "service start", "service stop",
        "service restart", "service uninstall",
    ]),
    ("help.group.tab", [
        "tab list", "tab use", "tab current", "tab new", "tab close",
        "tab activate", "tab title", "tab reload", "tab stop",
    ]),
    ("help.group.dom", [
        "dom tree", "dom outline", "dom html", "dom text", "dom find",
        "dom attr", "dom count", "dom info", "dom wait",
    ]),
    ("help.group.act", [
        "click", "type", "key", "hover", "scroll", "select",
        "check", "uncheck", "focus", "fill",
    ]),
    ("help.group.script", ["js", "eval"]),
    ("help.group.page", ["page", "goto", "back", "forward", "reload", "shot", "console", "events"]),
]


def key_of(command):
    return "cmd." + command.replace(" ", "_")


def canonical(topic):
    """把用户输入归一化成 'tab list' 这种形式。"""
    if not topic:
        return ""
    return " ".join(str(topic).replace("-", " ").replace("_", " ").split()).lower()


# ---------------------------------------------------------------- 总览
def print_overview(ctx):
    i18n = ctx.i18n
    meta = i18n.meta()
    parsers = ctx.parsers

    ctx.out.title(i18n.t("help.title", name=meta.get("name", "DOMate"), version=meta.get("version", "")))
    ctx.out.write(i18n.t("help.intro"))
    ctx.out.write("")
    ctx.out.write(ctx.out.paint(i18n.t("help.usage"), "cyan"))
    for line in i18n.raw("help.quick") or ["domate <command> [options]"]:
        ctx.out.write("  " + line)
    ctx.out.write("")

    for group_key, commands in GROUPS:
        ctx.out.write(ctx.out.paint(i18n.t(group_key), "bold"))
        rows = []
        for command in commands:
            if command not in parsers:
                continue
            brief = i18n.t(key_of(command) + ".brief")
            rows.append(["  " + command, brief])
        width = max([len(r[0]) for r in rows] + [12])
        for name, brief in rows:
            ctx.out.write("%s %s" % (ctx.out.paint(name.ljust(width), "green"), brief))
        ctx.out.write("")

    ctx.out.write(ctx.out.paint(i18n.t("help.tips_title"), "bold"))
    for tip in i18n.raw("help.tips") or []:
        ctx.out.write("  • " + tip)
    ctx.out.write("")
    ctx.out.write(ctx.out.paint(i18n.t("help.more"), "gray"))
    return 0


# ---------------------------------------------------------------- 单命令
def print_command(ctx, topic):
    i18n = ctx.i18n
    name = canonical(topic)
    parsers = ctx.parsers
    parser = parsers.get(name)
    if parser is None:
        # 允许只写尾段，如 help list -> tab list
        candidates = [k for k in parsers if k.split(" ")[-1] == name]
        if len(candidates) == 1:
            name = candidates[0]
            parser = parsers[name]
    if parser is None:
        ctx.out.error(i18n.t("msg.help_not_found", topic=topic),
                      i18n.t("msg.help_try_overview"))
        return 2

    key = key_of(name)
    ctx.out.title("domate " + name)
    brief = i18n.t(key + ".brief")
    desc = i18n.t(key + ".desc")
    if desc != key + ".desc" and desc:
        ctx.out.write(desc)
    elif brief != key + ".brief":
        ctx.out.write(brief)
    ctx.out.write("")
    ctx.out.write(i18n.t("help.usage") + ":")
    ctx.out.write("  " + _usage_line(parser, name))
    ctx.out.write("")

    options = _options(parser)
    if options:
        ctx.out.write(i18n.t("help.options") + ":")
        rows = []
        for flags, help_text in options:
            rows.append([flags, help_text])
        width = max(len(r[0]) for r in rows)
        for flags, help_text in rows:
            ctx.out.write("  %s  %s" % (ctx.out.paint(flags.ljust(width), "green"), help_text))
        ctx.out.write("")

    examples = i18n.raw(key + ".examples") or []
    if examples:
        ctx.out.write(i18n.t("help.examples") + ":")
        for example in examples:
            ctx.out.write("  " + ctx.out.paint("domate " + example, "cyan"))
        ctx.out.write("")

    related = i18n.raw(key + ".related") or []
    if related:
        ctx.out.write(i18n.t("help.related") + ": " + ", ".join(related))
    return 0


def _usage_line(parser, name):
    """拼一条干净的用法行：位置参数原样保留，选项只列 flag 名。"""
    parts = ["domate " + name]
    for action in parser._actions:  # noqa: SLF001 - 反射 argparse 内部结构
        if action.dest in ("help",) or not action.option_strings:
            continue
        if action.nargs == 0:
            parts.append("[" + action.option_strings[0] + "]")
        else:
            parts.append("[%s %s]" % (action.option_strings[0], str(action.dest).upper()))
    positional = [a for a in parser._actions if not a.option_strings and a.dest != "help"]
    body = []
    for action in positional:
        metavar = action.metavar or str(action.dest).upper()
        if action.nargs in ("?", "*"):
            body.append("[%s]" % metavar)
        elif action.nargs in ("+",):
            body.append("%s..." % metavar)
        else:
            body.append(metavar)
    return " ".join(parts + body)


def _options(parser):
    """反射出选项列表：(flags, 说明)。"""
    out = []
    for action in parser._actions:  # noqa: SLF001
        if not action.option_strings:
            continue
        if action.dest == "help":
            continue
        flags = ", ".join(action.option_strings)
        if action.nargs != 0 and action.dest not in ("help",):
            flags += " " + str(action.metavar or action.dest).upper()
        out.append((flags, action.help or ""))
    return out


# ---------------------------------------------------------------- 长文档
def print_guide(ctx):
    """打印 docs/<lang>/guide.md 全文（终端纯文本渲染）。"""
    lang = ctx.i18n.lang
    path = os.path.join(ROOT, "docs", lang, "guide.md")
    if not os.path.isfile(path):
        ctx.out.error(ctx.text("msg.guide_missing", path=path))
        return 2
    with open(path, "r", encoding="utf-8") as fp:
        content = fp.read()
    for line in content.splitlines():
        if line.startswith("### "):
            ctx.out.write(ctx.out.paint(line[4:], "bold"))
        elif line.startswith("## "):
            ctx.out.write(ctx.out.paint(line[3:], "bold"))
        elif line.startswith("# "):
            ctx.out.write(ctx.out.paint(line[2:].upper(), "bold"))
        elif line.startswith("```"):
            continue
        else:
            ctx.out.write(line)
    return 0
