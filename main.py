# write by LiYunqingli(LiHuarong) at 2026-11-24
# an agent tool for communicating with DOM
"""DOMate 入口：终端控制浏览器。

    python main.py tab list
    python main.py click "text=登录"
    python main.py help
"""

import os
import sys

# 保证以 `python main.py` 或 `python -m domate` 运行时都能找到 lib
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _force_utf8():
    """Windows 控制台默认 GBK，强制 UTF-8 才能正常显示中文/emoji。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def main(argv=None):
    _force_utf8()
    from lib.cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
