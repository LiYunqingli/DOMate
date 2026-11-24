# -*- coding: utf-8 -*-
"""把本地服务装成「一直在后台跑」的系统服务。

为什么不满足于「命令顺手拉起」：顺手拉起的是 CLI 的子进程，终端被强杀、
调用方被回收、甚至换个目录都可能把它带走；冷启动还要重新和插件握手。
装成系统级自启后，服务在登录时就起来了，命令只管连，不需要“发起连接”。

三套平台机制：
- Windows  「启动」文件夹里的 VBS，登录时静默拉起 pythonw（不依赖 schtasks，
  很多环境把它拉黑；纯文件写入，也不需要管理员权限）
- Linux    systemd --user 单元，Restart=always
- macOS    launchd LaunchAgent，KeepAlive

设计上把「要做的事」和「真的去做」分开：plan() 只产出纯数据（文件路径、
文件内容、命令序列），install() 才落盘/执行。这样 --dry-run 和单元测试
都不会碰系统。
"""

import os
import platform
import shutil
import subprocess
import sys

from .config import ROOT

#: systemd 单元名 / launchd label
UNIT_NAME = "domate.service"
LABEL = "com.domate.bridge"


# ---------------------------------------------------------------- 公共
def kind():
    """当前平台的托管方式。

    Windows 用「启动」文件夹里的 VBS：不依赖 schtasks（很多环境把它拉黑），
    纯文件写入，登录时由 wscript 静默拉起 pythonw，没有控制台窗口。
    """
    system = platform.system()
    if system == "Windows":
        return "startup"
    if system == "Darwin":
        return "launchd"
    return "systemd"


def python_exe():
    """服务要用的解释器：Windows 上优先 pythonw，避免弹黑框。"""
    exe = sys.executable or "python"
    if os.name == "nt":
        candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(candidate):
            return candidate
    return exe


def serve_argv():
    """服务进程的完整启动命令（前台阻塞，交给托管方守护）。"""
    return [python_exe(), os.path.join(ROOT, "main.py"), "serve"]


def managed_by_os():
    """是否由系统托管方（systemd/launchd）负责启停。

    Windows 的启动项只负责「登录时拉起」，启停靠 domate 自己派生子进程，
    所以这里返回 False。
    """
    if kind() == "startup":
        return False
    manager = "systemctl" if kind() == "systemd" else "launchctl"
    return shutil.which(manager) is not None


def _log_path():
    return os.path.join(ROOT, "logs", "domate.log")


def plan(as_kind=None):
    """返回安装方案。纯数据，不产生任何副作用。

    as_kind 用于测试和交叉验证：在 Windows 上也能生成 systemd/launchd 的方案，
    不必真的换平台。
    """
    target = as_kind or kind()
    if target == "startup":
        return _plan_startup()
    if target == "launchd":
        return _plan_launchd()
    return _plan_systemd()


# ---------------------------------------------------------------- Windows
def _plan_startup():
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs",
                        "Startup", "DOMate.vbs")
    exe, script = serve_argv()[0], serve_argv()[1]
    # 一行 VBS：0 = 隐藏窗口，False = 不等待
    content = "\r\n".join([
        "' DOMate 本地服务：登录时静默启动（由 domate service install 生成）",
        'Set sh = CreateObject("WScript.Shell")',
        'sh.Run """%s"" ""%s"" serve", 0, False' % (exe, script),
        "",
    ])
    return {
        "kind": "startup",
        "path": path,
        "content": content,
        "detail": path,
        "install": [],          # 纯文件写入，不需要任何外部命令
        "uninstall": [],
        "start": [],            # 启停由 domate 自己派生子进程完成
        "stop": [],
        "status": None,
    }


# ---------------------------------------------------------------- Linux
def _plan_systemd():
    argv = " ".join('"%s"' % a if " " in a else a for a in serve_argv())
    content = "\n".join([
        "[Unit]",
        "Description=DOMate local browser bridge",
        "Documentation=%s/docs/zh-CN/guide.md" % ROOT,
        "After=default.target",
        "",
        "[Service]",
        "Type=simple",
        "WorkingDirectory=%s" % ROOT,
        "ExecStart=%s" % argv,
        "Environment=PYTHONIOENCODING=utf-8",
        "Restart=always",
        "RestartSec=2",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])
    path = os.path.expanduser("~/.config/systemd/user/%s" % UNIT_NAME)
    return {
        "kind": "systemd",
        "path": path,
        "content": content,
        "detail": path,
        "install": [["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", UNIT_NAME]],
        "uninstall": [["systemctl", "--user", "disable", "--now", UNIT_NAME]],
        "start": [["systemctl", "--user", "start", UNIT_NAME]],
        "stop": [["systemctl", "--user", "stop", UNIT_NAME]],
        "status": ["systemctl", "--user", "is-active", UNIT_NAME],
    }


# ---------------------------------------------------------------- macOS
def _plan_launchd():
    argv = "\n".join("        <string>%s</string>" % _xml(a) for a in serve_argv())
    content = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>%s</string>
    <key>ProgramArguments</key>
    <array>
%s
    </array>
    <key>WorkingDirectory</key>
    <string>%s</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>%s</string>
    <key>StandardErrorPath</key>
    <string>%s</string>
</dict>
</plist>
""" % (LABEL, argv, _xml(ROOT), _xml(_log_path()), _xml(_log_path()))
    path = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)
    return {
        "kind": "launchd",
        "path": path,
        "content": content,
        "detail": path,
        "install": [["launchctl", "load", "-w", path]],
        "uninstall": [["launchctl", "unload", "-w", path]],
        "start": [["launchctl", "start", LABEL]],
        "stop": [["launchctl", "stop", LABEL]],
        "status": ["launchctl", "list", LABEL],
    }


def _xml(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ---------------------------------------------------------------- 执行
def run(command, timeout=20):
    """执行一条命令，返回 (ok, 输出)。"""
    if shutil.which(command[0]) is None:
        return False, "%s: command not found" % command[0]
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output


def install(dry_run=False, start=True):
    """写入托管配置并（默认）立即启动。返回 (ok, 步骤记录)。"""
    steps = []
    spec = plan()
    if dry_run:
        steps.append(("write" if spec["path"] else "create", spec["path"] or spec["detail"]))
        for command in spec["install"]:
            steps.append(("run", " ".join(command)))
        return True, steps

    if spec["path"]:
        try:
            os.makedirs(os.path.dirname(spec["path"]), exist_ok=True)
            with open(spec["path"], "w", encoding="utf-8") as fp:
                fp.write(spec["content"])
            steps.append(("write", spec["path"]))
        except OSError as exc:
            return False, steps + [("error", "写入 %s 失败: %s" % (spec["path"], exc))]

    for command in spec["install"]:
        ok, output = run(command)
        steps.append(("run" if ok else "error", " ".join(command) + ("  → " + output.splitlines()[0] if output else "")))
        if not ok:
            return False, steps
    if not start:
        for command in spec["stop"]:
            run(command)
    return True, steps


def uninstall(dry_run=False):
    steps = []
    spec = plan()
    if dry_run:
        if spec["path"]:
            steps.append(("remove", spec["path"]))
        for command in spec["uninstall"]:
            steps.append(("run", " ".join(command)))
        return True, steps

    for command in spec["uninstall"]:
        ok, output = run(command)
        steps.append(("run" if ok else "skip", " ".join(command) + ("  → " + output.splitlines()[0] if output else "")))
    if spec["path"] and os.path.isfile(spec["path"]):
        try:
            os.remove(spec["path"])
            steps.append(("remove", spec["path"]))
        except OSError as exc:
            return False, steps + [("error", str(exc))]
    return True, steps


def start():
    """让托管方启动服务。返回 (ok, 输出)；ok 为 None 表示该平台不靠托管方启停。"""
    commands = plan()["start"]
    if not commands:
        return None, ""
    return run(commands[0])


def stop():
    """让托管方停止服务。ok 为 None 表示交给调用方自行处理。"""
    commands = plan()["stop"]
    if not commands:
        return None, ""
    return run(commands[0])


def registered():
    """托管配置是否已安装（不判断进程是否在跑）。"""
    spec = plan()
    if spec["path"]:
        return os.path.isfile(spec["path"])
    return False
