# -*- coding: utf-8 -*-
"""开机自启管理：只创建/删除本项目自己的启动项「本地通话录音工具.lnk」。

绝不触碰其他程序（含 E:\\录屏录音自动、E:\\微信通话录音升级版）的启动项。
.lnk 用 comtypes 的 IShellLinkW 写入，Unicode 原生格式，无脚本编码问题。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
LNK_NAME = "本地通话录音工具升级版.lnk"
STARTUP_DIR = (Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup")
LNK_PATH = STARTUP_DIR / LNK_NAME


def is_enabled() -> bool:
    return LNK_PATH.exists()


def enable() -> None:
    import comtypes
    from comtypes.client import CreateObject
    from comtypes.persist import IPersistFile
    import comtypes.shelllink as shelllink

    STARTUP_DIR.mkdir(parents=True, exist_ok=True)
    comtypes.CoInitialize()
    try:
        link = CreateObject(shelllink.ShellLink, interface=shelllink.IShellLinkW)
        link.SetPath(str(ROOT / "runtime" / "Python312" / "pythonw.exe"))
        # --minimized：开机自启时不弹设置窗口，安静缩在托盘
        link.SetArguments(f'"{ROOT / "app.py"}" --minimized')
        link.SetWorkingDirectory(str(ROOT))
        link.SetDescription("本地通话录音工具·升级版 开机自启（独立项目）")
        # 快捷方式图标指向项目内稳定 ICO（失败不影响自启功能）
        try:
            link.SetIconLocation(str(ROOT / "assets" / "icons" / "app.ico"), 0)
        except Exception:
            pass
        link.QueryInterface(IPersistFile).Save(str(LNK_PATH), True)
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def disable() -> None:
    LNK_PATH.unlink(missing_ok=True)
