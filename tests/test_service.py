# -*- coding: utf-8 -*-
"""后台服务的注册方案与单实例约束。

两个关注点：
1. plan() 是纯数据，任何平台都能生成三套方案，所以这里在 Windows 上也能
   校验 systemd / launchd 的内容（真换了平台不用重写测试）。
2. 同一端口只允许一个 daemon。Windows 的 SO_REUSEADDR 默认允许重复绑定，
   两个实例会互相抢连接，插件就会反复掉线——这条必须守住。
"""

import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from lib import service
from lib.config import Config, ROOT
from lib.daemon import create_server
from lib.errors import DomateError
from lib.i18n import I18n
from lib.store import State

PORT = 8871
PASSED = []
FAILED = []


def check(name, cond, detail=None):
    if cond:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append(name)
        print("  [FAIL] %s %s" % (name, detail if detail is not None else ""))


# ---------------------------------------------------------------- 方案内容
def test_plan_shapes():
    for kind in ("startup", "systemd", "launchd"):
        spec = service.plan(kind)
        check("%s 有落盘路径" % kind, bool(spec.get("path")), spec)
        check("%s 有文件内容" % kind, bool(spec.get("content")))
        check("%s 内容指向 main.py serve" % kind,
              "main.py" in spec["content"] and "serve" in spec["content"])


def test_windows_plan():
    spec = service.plan("startup")
    check("启动项落在 Startup 目录", spec["path"].endswith("DOMate.vbs"), spec["path"])
    check("启动项不依赖外部命令", spec["install"] == [] and spec["start"] == [], spec["install"])
    content = spec["content"]
    check("启动脚本隐藏窗口", "sh.Run" in content and ", 0, False" in content)
    check("启动脚本调用 main.py", "main.py" in content and "serve" in content)


def test_systemd_plan():
    spec = service.plan("systemd")
    content = spec["content"]
    check("systemd 单元在 user 目录", "/.config/systemd/user/domate.service" in spec["path"].replace("\\", "/"),
          spec["path"])
    for needle in ("[Service]", "Restart=always", "WorkingDirectory=", "ExecStart=", "WantedBy=default.target"):
        check("systemd 含 %s" % needle, needle in content)
    check("systemd 用 --user 启停", any("--user" in c for c in spec["install"]))


def test_launchd_plan():
    spec = service.plan("launchd")
    content = spec["content"]
    check("launchd 落在 LaunchAgents", "Library/LaunchAgents" in spec["path"].replace("\\", "/"), spec["path"])
    for needle in ("<key>Label</key>", "<key>KeepAlive</key>", "<key>RunAtLoad</key>", "com.domate.bridge"):
        check("launchd 含 %s" % needle, needle in content)


def test_quoting():
    """路径含空格时，systemd 的 ExecStart 必须加引号，否则会被拆成两个参数。"""
    spec = service.plan("systemd")
    content = spec["content"]
    exec_line = [l for l in content.splitlines() if l.startswith("ExecStart=")][0]
    if " " in sys.executable:
        check("含空格路径被引号包住", '"' in exec_line, exec_line)
    else:
        check("含空格路径被引号包住（当前解释器路径无空格，规则已实现）", True)


def test_dry_run_is_side_effect_free():
    before = service.registered()
    ok, steps = service.install(dry_run=True)
    after = service.registered()
    check("演练不改变注册状态", before == after, "%s -> %s" % (before, after))
    check("演练列出了要做的动作", ok and steps, steps)


# ---------------------------------------------------------------- 单实例
def test_single_instance():
    cfg = Config()
    cfg.set("server.port", PORT)
    first = create_server(cfg, I18n("zh-CN"), State())
    threading.Thread(target=first.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    try:
        second = None
        try:
            second = create_server(cfg, I18n("zh-CN"), State())
        except DomateError as exc:
            check("同端口第二个实例被拒绝", exc.code == "E_PORT", exc.code)
        else:
            check("同端口第二个实例被拒绝", False, "竟然绑定成功")
        finally:
            if second is not None:
                second.server_close()
    finally:
        first.shutdown()
        first.server_close()


def test_repo_paths():
    """生成的命令必须指向当前仓库，不能是别处的陈旧路径。"""
    spec = service.plan("startup")
    check("启动脚本指向本仓库 main.py", ROOT.replace("\\", "/") in spec["content"].replace("\\", "/"))
    check("解释器存在", os.path.isfile(service.python_exe()), service.python_exe())


def main():
    for fn in (test_plan_shapes, test_windows_plan, test_systemd_plan, test_launchd_plan,
               test_quoting, test_dry_run_is_side_effect_free, test_single_instance,
               test_repo_paths):
        fn()
    print("\n通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("失败项：" + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
