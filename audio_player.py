# -*- coding: utf-8 -*-
"""内嵌音频播放器：Windows MCI（winmm）直接播 MP3，无新增依赖。

单别名实例，支持 播放/暂停/继续/停止/跳转/进度查询。
MCI 调用失败返回中文错误文本，不抛异常，GUI 据此提示。
"""
from __future__ import annotations

import ctypes
from pathlib import Path

_mci = ctypes.windll.winmm.mciSendStringW
_mci_err = ctypes.windll.winmm.mciGetErrorStringW
ALIAS = "lcrv2play"


def _send(cmd: str) -> tuple[bool, str]:
    buf = ctypes.create_unicode_buffer(256)
    code = _mci(cmd, buf, 255, 0)
    if code == 0:
        return True, buf.value.strip()
    err = ctypes.create_unicode_buffer(256)
    _mci_err(code, err, 255)
    return False, err.value.strip() or f"MCI 错误码 {code}"


class AudioPlayer:
    def __init__(self):
        self.path: Path | None = None
        self.length_ms: int = 0
        self._opened = False

    # ----- 打开 / 关闭 -----
    def open(self, path) -> str | None:
        """打开文件。成功返回 None，失败返回错误描述。"""
        self.close()
        ok, msg = _send(f'open "{path}" type mpegvideo alias {ALIAS}')
        if not ok:
            return f"无法打开音频：{msg}"
        self._opened = True
        self.path = Path(path)
        _send(f"set {ALIAS} time format milliseconds")
        ok, msg = _send(f"status {ALIAS} length")
        self.length_ms = int(msg) if ok and msg.isdigit() else 0
        return None

    def close(self):
        if self._opened:
            _send(f"close {ALIAS}")
            self._opened = False
        self.path = None
        self.length_ms = 0

    # ----- 控制 -----
    def play(self, from_ms: int = 0) -> str | None:
        pos = f" from {int(from_ms)}" if from_ms else ""
        ok, msg = _send(f"play {ALIAS}{pos}")
        return None if ok else f"播放失败：{msg}"

    def pause(self):
        _send(f"pause {ALIAS}")

    def resume(self):
        _send(f"resume {ALIAS}")

    def stop(self):
        _send(f"stop {ALIAS}")

    def seek(self, ms: int, and_play: bool = False):
        """跳转。MCI seek 后会停在暂停态，and_play=True 则继续播。"""
        _send(f"seek {ALIAS} to {max(0, int(ms))}")
        if and_play:
            _send(f"play {ALIAS}")

    # ----- 查询 -----
    def position_ms(self) -> int:
        ok, msg = _send(f"status {ALIAS} position")
        return int(msg) if ok and msg.isdigit() else 0

    def is_playing(self) -> bool:
        ok, msg = _send(f"status {ALIAS} mode")
        return ok and msg.lower() == "playing"
