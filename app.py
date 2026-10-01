"""本地通话录音工具·升级版（v2）— 主程序。

Windows 托盘常驻 + 管理界面（多标签）：检测微信通话窗口 → 接通后自动录制
双方声音（进程级捕获对方 + 麦克风我方，左右分声道）→ 挂断后转 MP3。
录音层完全离线；新增录音列表、AI 分析（转文字/总结/情感）与 AI 对话，
这两项在用户配置 API 后联网，Key 只存本机 config.json。

本项目独立于 E:\\本地通话录音工具-独立项目（v1）以及
E:\\录屏录音自动 与 E:\\微信通话录音升级版：
独立代码、独立运行时、独立配置/日志/录音目录、独立单实例互斥体
（Local\\LocalCallRecorderV2），不影响其他版本运行。

第一阶段已实现：
- 中文托盘状态（等待通话/正在录音/正在保存/录音失败/已暂停）
- 暂停/恢复监控、打开录音目录、查看日志、退出
- 失败气泡通知 + 日志轮转
- 单实例保护（Local\\LocalCallRecorderV2 互斥体，仅防本软件自身重复启动；
  与旧版本并行运行互不影响，不做跨版本互斥）
- 转换队列限流（单线程），失败保留源音频并自动重试
- 启动时恢复上次异常退出残留的临时 WAV
- 正常退出先停止采集并保存

用法：
  pythonw.exe app.py                常驻监控（托盘）
  python.exe   app.py --console     带控制台日志的常驻监控（调试）
  python.exe   app.py --test-seconds N   直接录 N 秒测试，写入测试目录
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import logging.handlers
import queue
import subprocess
import sys
import threading
import time
import wave
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import numpy as np  # noqa: F401  (audio_capture 依赖，提前确认)
import pystray
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))  # 嵌入版 Python 不会自动加脚本目录
from audio_capture import CALL_TITLE, WECHAT_PROCESSES, CaptureError, record
from contact_name import sanitize_contact
from converter import Converter

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "logs"
MUTEX_NAME = "Local\\LocalCallRecorderV2"
SHOW_EVENT_NAME = "Local\\LocalCallRecorderV2_Show"   # 重复启动时唤起已有窗口
APP_TITLE = "本地通话录音工具·升级版"

# ---------- 窗口枚举（与原版一致的已验证实现） ----------
u = ctypes.windll.user32
k = ctypes.windll.kernel32
PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
u.IsWindowVisible.argtypes = [wintypes.HWND]
u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k.OpenProcess.restype = wintypes.HANDLE
k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
k.CloseHandle.argtypes = [wintypes.HANDLE]
k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
k.CreateMutexW.restype = wintypes.HANDLE

# 事件/互斥用带 last_error 的句柄调用，单实例判断更可靠
k2 = ctypes.WinDLL("kernel32", use_last_error=True)
k2.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
k2.CreateMutexW.restype = wintypes.HANDLE
k2.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                            wintypes.LPCWSTR]
k2.CreateEventW.restype = wintypes.HANDLE
k2.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k2.OpenEventW.restype = wintypes.HANDLE
k2.SetEvent.argtypes = [wintypes.HANDLE]
k2.SetEvent.restype = wintypes.BOOL
k2.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k2.WaitForSingleObject.restype = wintypes.DWORD
EVENT_MODIFY_STATE = 0x0002
INFINITE = 0xFFFFFFFF


def wake_running_instance(logger) -> bool:
    """重复启动时：通知已在运行的实例把窗口显示出来（而非静默退出）。

    找不到事件（旧版本在跑）时返回 False，调用方照旧退出。
    """
    try:
        h = k2.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_EVENT_NAME)
        if not h:
            return False
        try:
            ok = bool(k2.SetEvent(h))
            logger.info("已请求正在运行的实例显示窗口（%s）",
                        "成功" if ok else "失败")
            return ok
        finally:
            k.CloseHandle(h)
    except Exception:
        logger.exception("唤起已运行实例失败")
        return False


def watch_show_event(app, handle, logger):
    """常驻实例的后台线程：被重复启动的实例唤起时，把窗口显示出来。"""
    while True:
        rc = k2.WaitForSingleObject(handle, INFINITE)
        if rc != 0:
            logger.warning("显示窗口事件等待结束（rc=%s）", rc)
            return
        deadline = time.monotonic() + 15
        while app.gui is None and time.monotonic() < deadline:
            time.sleep(0.3)
        try:
            if app.gui is not None:
                app.gui.request("show")
                logger.info("收到重复启动的“显示窗口”请求，已唤起主界面")
            else:
                logger.warning("收到“显示窗口”请求，但界面尚未就绪，已忽略")
        except Exception:
            logger.exception("处理“显示窗口”请求失败")


def call_present() -> bool:
    hit = []

    @PROC
    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, b, n + 1)
        if b.value.strip() != CALL_TITLE:
            return True
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k.OpenProcess(0x1000, False, pid.value)
        if h:
            try:
                p = ctypes.create_unicode_buffer(32768)
                z = wintypes.DWORD(len(p))
                if k.QueryFullProcessImageNameW(h, 0, p, ctypes.byref(z)) and \
                        Path(p.value).name.lower() in WECHAT_PROCESSES:
                    hit.append(1)
            finally:
                k.CloseHandle(h)
        return True

    u.EnumWindows(cb, 0)
    return bool(hit)


# ---------- 配置与日志 ----------
def default_config() -> dict:
    """首次启动的默认配置：自动监控关闭、保存到本项目测试录音目录、不自启。"""
    return {
        "out_dir": str(ROOT / "测试录音"),
        "poll_seconds": 0.7,
        "end_grace_seconds": 3,
        "mp3_bitrate": "96k",
        "max_concurrent_conversions": 1,
        "log_max_bytes": 1048576,
        "log_backups": 3,
        "sample_rate": 48000,
        "block_frames": 960,
        "capture_mode": "process",
        "require_connect": True,
        "connect_interval_seconds": 2.0,
        "ring_timeout_seconds": 180,
        "min_save_seconds": 3,
        "recording_enabled": False,
        "ocr_max_attempts": 3,
        "ocr_interval_seconds": 2.0,
        "ocr_save_debug_screenshots": False,
        "name_retry_rounds": 30,
        "name_retry_interval_seconds": 20.0,
        "max_recordings_gb": 10.0,   # 录音总大小超过此值时自动删除最旧 MP3；0=不清理
        # 转写设置：local = 用内置离线模型（免费、不联网）；api = 用下面的云端 API
        "transcribe_engine": "local",
        "local_asr_model": "sense_voice",   # sense_voice | funasr_nano
        "local_asr_threads": 0,             # 0 = 自动（CPU 一半核心）
        "transcript_polish": True,          # 转写后用 AI 逐行修正识别错误（需 API）
        "auto_analyze": False,              # 生成文字稿后自动整理分析报告（需 API，可能产生费用）
        # API 设置（OpenAI 兼容格式；留空 = 分析/AI 对话不可用，录音/本地转写不受影响）
        "api_base_url": "",
        "api_key": "",
        "chat_model": "",
        "transcribe_model": "",
    }


def load_config() -> dict:
    cfg = default_config()
    if CFG_PATH.exists():
        try:
            cfg.update(json.loads(CFG_PATH.read_text("utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    """原子写回配置文件（界面改动即时持久化，重启后读取一致）。"""
    tmp = CFG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(CFG_PATH)


def setup_logging(cfg: dict, console: bool) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("recorder")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "recorder.log", maxBytes=cfg["log_max_bytes"],
        backupCount=cfg["log_backups"], encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    return logger


# ---------- 托盘图标 ----------
STATE_TEXT = {
    "waiting": "等待通话",
    "recording": "正在录音",
    "saving": "正在保存",
    "error": "录音失败",
    "paused": "已暂停",
}
STATE_COLOR = {
    "waiting": (70, 130, 180),
    "recording": (220, 60, 60),
    "saving": (240, 160, 40),
    "error": (180, 40, 40),
    "paused": (130, 130, 130),
}


_ICONS_DIR = Path(__file__).resolve().parent / "assets" / "icons"
_TRAY_CACHE: dict[str, Image.Image] = {}


def _fallback_icon(state: str) -> Image.Image:
    """素材缺失/加载失败时的回退图标（简单色块麦克风）。"""
    color = STATE_COLOR.get(state, STATE_COLOR["waiting"])
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=color)
    d.rounded_rectangle((24, 14, 40, 38), radius=8, fill=(255, 255, 255, 240))
    d.arc((18, 24, 46, 48), start=0, end=180, fill=(255, 255, 255, 240), width=4)
    d.line((32, 44, 32, 52), fill=(255, 255, 255, 240), width=4)
    d.line((24, 52, 40, 52), fill=(255, 255, 255, 240), width=4)
    return img


def make_icon_image(state: str) -> Image.Image:
    """托盘图标：优先用 assets/icons 下的插画素材（启动时缓存，
    64px 派生图，内存占用小）；加载失败回退到程序内置绘制图标。"""
    if state in _TRAY_CACHE:
        return _TRAY_CACHE[state]
    img = None
    for name in (f"tray_{state}_64.png", f"tray_{state}.png"):
        try:
            p = _ICONS_DIR / name
            if p.exists():
                img = Image.open(p).convert("RGBA")
                break
        except Exception:
            img = None
    if img is None:
        logging.getLogger("recorder").warning(
            "托盘图标素材缺失（state=%s），使用内置回退图标", state)
        img = _fallback_icon(state)
    _TRAY_CACHE[state] = img
    return img


class ConnectSession:
    """一通电话的接通检测会话（每通独立，事件互不共享，杜绝跨通话串状态）。

    outcome 取值（None = 检测仍在进行）：
      timer      OCR 读到通话计时，确认接通（唯一算"真接通"的结局）
      no_window  子进程报告窗口已消失（未接通/已取消）
      proc_exit  子进程提前退出且无 TIMER/NOWINDOW（检测故障）
      error      检测线程自身异常
      cancelled  被监控循环主动取消（录音已开始/窗口消失/暂停/退出）
    """
    __slots__ = ("session_id", "call_id", "ok", "cancel", "ocr_dead",
                 "outcome", "created", "is_group")

    def __init__(self, session_id: int, call_id: str):
        self.session_id = session_id
        self.call_id = call_id
        self.ok = threading.Event()        # 仅 timer 结局置位
        self.cancel = threading.Event()    # 请求取消（读循环可及时响应）
        self.ocr_dead = False              # OCR 连续读不到任何窗口文本
        self.outcome: str | None = None
        self.created = time.monotonic()
        self.is_group = False              # OCR 在窗口里见到「添加成员」（群通话独有按钮）


class RecorderApp:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger
        self.out_dir = Path(cfg["out_dir"])
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.state = "waiting"
        self.observe_only = not cfg.get("recording_enabled", False)
        self.paused = self.observe_only   # 首启默认关闭 → 显示「已暂停」
        self.exit_requested = threading.Event()
        self.stop_capture = threading.Event()
        self.record_thread: threading.Thread | None = None
        self.current_wav: Path | None = None
        self.contact_name: str | None = None
        self.pending_group_call = False   # 接通检测判定为群通话 → 本次录音按"群通话"命名
        self.last_error: str | None = None
        self.last_error_time: str | None = None
        self.retry_at = 0.0
        self.gui = None                   # settings_gui.SettingsWindow（主线程）
        self._state_lock = threading.Lock()
        # 接通检测：每通电话一个 ConnectSession（独立事件，杜绝跨通话串状态）
        self._connect_seq = 0
        self.converter = Converter(
            cfg["mp3_bitrate"],
            on_result=self._on_convert_result,
            on_state=self._on_convert_state)
        self.icon: pystray.Icon | None = None

    # ----- 状态管理 -----
    def set_state(self, state: str, error: str | None = None):
        with self._state_lock:
            self.state = state
            self.last_error = error
            if error:
                self.last_error_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.icon:
            try:
                disp = self.display_state()
                self.icon.icon = make_icon_image(disp)
                self.icon.title = f"{APP_TITLE} - {STATE_TEXT.get(disp, disp)}"
                if self.icon.has_notification_icon:
                    self.icon.update_menu()
            except Exception:
                pass
        self.log.info("状态: %s%s", STATE_TEXT.get(state, state),
                      f"（{error}）" if error else "")

    def display_state(self) -> str:
        """展示给界面/托盘的状态：自动录音关闭时统一显示「已暂停」。"""
        if self.observe_only or self.paused:
            return "paused"
        return self.state

    def get_status(self) -> dict:
        with self._state_lock:
            enabled = not self.observe_only
            err = self.last_error
            if err and self.last_error_time:
                err = f"{err}（{self.last_error_time}）"
            return {
                "enabled": enabled,
                "display_text": STATE_TEXT.get(self.display_state(), self.state),
                "last_error": err,
                "out_dir": str(self.out_dir),
                "max_recordings_gb": float(self.cfg.get("max_recordings_gb", 0) or 0),
            }

    # ----- 设置界面操作 -----
    def recording_is_enabled(self) -> bool:
        return not self.observe_only

    def set_recording_enabled(self, on: bool):
        """启用/暂停自动录音（即时持久化）。关闭时正在进行的录音会保存收尾。"""
        self.observe_only = not on
        self.paused = not on
        if on:
            self.retry_at = 0.0
            self.set_state("waiting")
            self.log.info("用户启用自动录音")
        else:
            self.set_state("paused")
            self.log.info("用户暂停自动录音")
        self.cfg["recording_enabled"] = bool(on)
        try:
            save_config(self.cfg)
        except Exception:
            self.log.exception("配置保存失败")

    def set_out_dir(self, path_str: str):
        p = Path(path_str)
        p.mkdir(parents=True, exist_ok=True)
        self.out_dir = p
        self.cfg["out_dir"] = str(p)
        save_config(self.cfg)
        self.log.info("保存目录已切换: %s", p)

    def set_max_recordings_gb(self, value: float):
        """设置录音空间上限（GB，0=不自动清理），即时持久化。"""
        self.cfg["max_recordings_gb"] = max(0.0, float(value))
        save_config(self.cfg)
        self.log.info("录音空间上限已设置为 %.1f GB（0=不清理）",
                      self.cfg["max_recordings_gb"])

    def save_config(self):
        """供界面保存整体配置（含 API 设置）。"""
        try:
            save_config(self.cfg)
        except Exception:
            self.log.exception("配置保存失败")

    def log_path(self) -> Path:        return LOG_DIR / "recorder.log"

    def request_exit(self):
        """界面/托盘统一的退出入口：先收尾保存，再退出进程。"""
        self.log.info("用户请求退出")
        self.exit_requested.set()
        threading.Thread(target=self._shutdown, daemon=True).start()

    def notify(self, message: str):
        self.log.warning("通知: %s", message)
        if getattr(self, "icon", None):
            try:
                self.icon.notify(message, APP_TITLE)
            except Exception:
                pass

    # ----- 录音 -----
    def start_recording(self):
        if self.record_thread and self.record_thread.is_alive():
            return False
        started = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        wav = self.out_dir / f".tmp_{started}.wav"
        self.current_wav = wav
        self.stop_capture = threading.Event()
        # 通话标识（诊断目录名）：时间戳格式本身无冒号无空格，可直接当目录名
        call_id = started
        if self.pending_group_call:
            # 群通话：窗口里没有联系人名，跳过姓名 OCR，按「群通话」命名
            self.pending_group_call = False
            self.contact_name = "群通话"
            self.log.info("群通话录音：跳过联系人识别，文件名将使用「群通话」")
        else:
            self.contact_name = None
            threading.Thread(target=self._read_name_worker,
                             args=(started, call_id, wav, self.stop_capture),
                             name=f"name-{started}", daemon=True).start()
        self.record_thread = threading.Thread(
            target=self._record_worker, args=(wav, started),
            name=f"record-{started}", daemon=True)
        self.record_thread.start()
        return True

    def _record_worker(self, wav: Path, started: str):
        self.set_state("recording")
        try:
            record(self.stop_capture, wav, self.cfg["sample_rate"],
                   self.cfg["block_frames"],
                   capture_mode=self.cfg.get("capture_mode", "system"))
        except CaptureError as exc:
            self.log.exception("采集失败")
            self.notify(f"录音失败：{exc}")
            self.set_state("error", str(exc))
            self.retry_at = time.monotonic() + 10
            self._finish_call(wav, started, failed=True)
            return
        except Exception as exc:  # 未知异常
            self.log.exception("采集发生未知错误")
            self.notify(f"录音失败：{exc}")
            self.set_state("error", str(exc))
            self.retry_at = time.monotonic() + 10
            self._finish_call(wav, started, failed=True)
            return
        self._finish_call(wav, started, failed=False)

    def _read_name_worker(self, started: str, call_id: str,
                          session_wav: Path, session_stop: threading.Event):
        """姓名线程入口：任何异常都记入日志，绝不静默退出。

        （修复记录：2026-09-25 发现旧版线程体引用了未传入的 started 变量，
        线程一启动就 NameError 死亡且无日志，导致联系人命名从未生效。）
        """
        try:
            self._read_name_inner(started, call_id, session_wav, session_stop)
        except Exception:
            self.log.exception("姓名识别线程异常退出 call_id=%s（本通按未知联系人保存，不影响录音）",
                               call_id)

    def _read_name_inner(self, started: str, call_id: str,
                         session_wav: Path, session_stop: threading.Event):
        """在独立子进程中持续重试识别联系人姓名，直到命中或通话结束。

        约束：OCR 走子进程（崩溃/卡死都影响不到录音主程序），绝不阻塞录音；
        通话期间每 name_retry_interval_seconds 秒开一轮新子进程
        （最多 name_retry_rounds 轮），长通话中窗口布局变化也能补识别；
        结果只在仍属于本通电话时才写入 self.contact_name（避免连续通话串名）；
        识别不到保持 None（保存为"未知联系人"）。
        """
        cfg = self.cfg
        attempts = int(cfg.get("ocr_max_attempts", 3))
        interval = float(cfg.get("ocr_interval_seconds", 2.0))
        retry_rounds = int(cfg.get("name_retry_rounds", 30))
        retry_interval = float(cfg.get("name_retry_interval_seconds", 20.0))
        debug = 1 if cfg.get("ocr_save_debug_screenshots", False) else 0
        helper = ROOT / "ocr_worker.py"
        py = ROOT / "runtime" / "Python312" / "python.exe"
        if not helper.exists() or not py.exists():
            self.log.warning("OCR 子进程脚本或解释器缺失，本通电话按未知联系人保存")
            return

        def session_alive() -> bool:
            return (not self.exit_requested.is_set() and not session_stop.is_set()
                    and self.current_wav == session_wav)

        self.log.info("姓名识别线程启动 call_id=%s（每轮子进程最多 %d 次，"
                      "未命中每 %.0f 秒重试，最多 %d 轮）",
                      call_id, attempts, retry_interval, retry_rounds)
        for round_no in range(1, retry_rounds + 1):
            if not session_alive():
                break
            try:
                proc = subprocess.Popen(
                    [str(py), str(helper), "--attempts",
                     str(attempts if round_no == 1 else 2),
                     "--interval", str(interval), "--debug", str(debug),
                     "--call-id", f"{call_id}-r{round_no}"],
                    cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception:
                self.log.exception("OCR 子进程启动失败（第 %d 轮）", round_no)
                return
            name = None
            try:
                lq: queue.Queue = queue.Queue()

                def _reader(p=proc):
                    try:
                        for ln in p.stdout:
                            lq.put(ln)
                    except Exception:
                        pass
                    finally:
                        lq.put(None)

                threading.Thread(target=_reader, daemon=True,
                                 name=f"name-reader-{call_id}-r{round_no}").start()
                deadline = time.monotonic() + attempts * (interval + 6) + 20
                while True:
                    if not session_alive():
                        break
                    if time.monotonic() > deadline:
                        self.log.warning("OCR 子进程超时（第 %d 轮）", round_no)
                        break
                    try:
                        line = lq.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if line is None:
                        break                  # 子进程结束（正常或崩溃）
                    line = line.strip()
                    if line.startswith("OCRLINE:"):
                        self.log.info("OCR 文本: %s", line[8:])
                    elif line.startswith("DIAG:"):
                        self.log.info("OCR 诊断样本目录: %s", line[5:])
                    elif line.startswith("RESULT:"):
                        name = line[7:].strip() or None
                        break
            except Exception:
                self.log.exception("OCR 子进程流程异常（不影响录音）")
            finally:
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if name:
                if session_alive():
                    self.contact_name = name
                    self.log.info("联系人识别(OCR) 第 %d 轮: %s", round_no, name)
                else:
                    self.log.info("OCR 识别到「%s」但本通电话已结束，丢弃结果", name)
                return
            if round_no < retry_rounds and session_alive():
                # 分段小睡，通话结束立即退出
                for _ in range(max(1, int(retry_interval / 0.5))):
                    if not session_alive():
                        break
                    time.sleep(0.5)
        if session_alive():
            self.log.info("姓名识别 %d 轮未命中，本通保存为未知联系人", retry_rounds)

    def _finish_call(self, wav: Path, started: str, failed: bool):
        keep = wav.exists() and wav.stat().st_size > 44
        if keep and not failed:
            # 太短的录音（如刚接通立即挂断）按配置丢弃
            min_bytes = int(float(self.cfg.get("min_save_seconds", 2)) * 192000)
            if wav.stat().st_size < min_bytes:
                dur = wav.stat().st_size / 192000.0
                wav.unlink(missing_ok=True)
                self.current_wav = None
                self.log.info("录音时长 %.1f 秒，短于 %.0f 秒，不保存", dur,
                              float(self.cfg.get("min_save_seconds", 2)))
                return
        if not keep:
            wav.unlink(missing_ok=True)
            self.current_wav = None
            return
        display = sanitize_contact(self.contact_name) or "未知联系人"
        mp3 = self.out_dir / f"{started}_{display}_通话.mp3"
        n = 1
        while mp3.exists():
            n += 1
            mp3 = self.out_dir / f"{started}_{display}_通话({n}).mp3"
        self.set_state("saving")
        ok = self.converter.submit(wav, mp3)
        if not ok:
            self.notify("转换队列已满，本段录音保留为临时文件，退出程序后重开可自动恢复")
            self.set_state("error", "转换队列已满")
        self.current_wav = None

    def _on_convert_result(self, ok: bool, wav: Path, mp3: Path, error: str | None):
        if ok:
            if self.state == "saving":
                self.set_state("waiting")
            self._cleanup_old_recordings()
        else:
            self.notify(f"保存失败：{error}（源音频已保留在 {wav.name}）")
            self.set_state("error", error)

    def _cleanup_old_recordings(self):
        """录音总大小超过 max_recordings_gb 时，从最旧开始删除 MP3。

        只删本目录下本软件生成的 *_通话.mp3，绝不碰 .tmp_*.wav（可能在录）
        和其他文件；后台线程执行，不阻塞保存流程。
        """
        limit_gb = float(self.cfg.get("max_recordings_gb", 0) or 0)
        if limit_gb <= 0:
            return
        limit = limit_gb * 1024 ** 3

        def worker():
            try:
                mp3s = sorted(self.out_dir.glob("*_通话.mp3"),
                              key=lambda p: p.stat().st_mtime)
            except Exception:
                self.log.exception("自动清理：扫描录音目录失败")
                return
            total = 0
            for p in mp3s:
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
            if total <= limit:
                return
            removed = []
            for p in mp3s:                      # 最旧的先删
                if total <= limit:
                    break
                try:
                    size = p.stat().st_size
                    p.unlink()
                    total -= size
                    removed.append(f"{p.name}({size // 1024 // 1024}MB)")
                    self.log.info("自动清理：已删除旧录音 %s", p.name)
                except OSError:
                    self.log.warning("自动清理：无法删除 %s", p.name)
            if removed:
                self.notify("录音空间自动清理：已删除 " + "、".join(removed))

        threading.Thread(target=worker, name="cleanup", daemon=True).start()

    def _on_convert_state(self, pending: int):
        # -1 表示工作线程开始处理一个任务
        if self.state == "saving":
            return
        if pending > 0 and self.state == "waiting":
            self.set_state("saving")

    # ----- 崩溃/异常退出残留恢复 -----
    def recover_orphan_wavs(self):
        recovered = 0
        for wav in self.out_dir.glob(".tmp_*.wav"):
            name = wav.name[len(".tmp_"):-len(".wav")]
            try:
                datetime.strptime(name, "%Y-%m-%d_%H-%M-%S_%f" if len(name) > 19 else "%Y-%m-%d_%H-%M-%S")
            except ValueError:
                name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            mp3 = self.out_dir / f"{name}_恢复_通话.mp3"
            n = 1
            while mp3.exists():
                n += 1
                mp3 = self.out_dir / f"{name}_恢复_通话({n}).mp3"
            if self.converter.submit(wav, mp3):
                recovered += 1
                self.log.info("已排队恢复残留临时音频: %s", wav.name)
        if recovered:
            self.set_state("saving")
            self.notify(f"发现 {recovered} 段上次未保存完的录音，正在转换保存")

    # ----- 监控主循环 -----
    def monitor_loop(self):
        poll = self.cfg["poll_seconds"]
        grace = self.cfg["end_grace_seconds"]
        require_connect = bool(self.cfg.get("require_connect", True))
        ring_timeout = float(self.cfg.get("ring_timeout_seconds", 180))
        restart_backoff = 5.0     # 会话结束后到重启检测的最短间隔
        active = False
        session: ConnectSession | None = None
        abnormal_ends = 0         # 连续 proc_exit/error 次数（连续多次→故障回退）
        missing_since = None
        self.log.info("监控已启动，保存到 %s（接通后才录音=%s，检测信号=通话计时OCR，每通独立会话）",
                      self.out_dir, require_connect)
        while not self.exit_requested.is_set():
            try:
                if self.paused:
                    if active:
                        self.log.info("已暂停，停止当前录音")
                        self.stop_capture.set()
                    if session is not None:
                        session.cancel.set()
                        session = None
                    time.sleep(0.5)
                    continue

                if self.observe_only:
                    self.exit_requested.wait(1)
                    continue

                found = call_present()

                if active and self.record_thread and not self.record_thread.is_alive():
                    # 工作线程已自行结束（错误路径），等待重试
                    active = False
                    missing_since = None

                if found:
                    missing_since = None
                    if not active and time.monotonic() >= self.retry_at:
                        if not require_connect:
                            active = self.start_recording()
                            self.log.info("窗口出现即录音（require_connect=False）")
                        elif session is None:
                            self._connect_seq += 1
                            session = ConnectSession(
                                self._connect_seq,
                                time.strftime("connect-%Y%m%d-%H%M%S"))
                            self.log.info("检测到通话窗口，等待接通（会话 #%d，响铃不录音）",
                                          session.session_id)
                            threading.Thread(
                                target=self._connect_check_worker, args=(session,),
                                name=f"connect-{session.session_id}",
                                daemon=True).start()
                        elif session.ok.is_set():
                            self.log.info("通话已接通（计时确认，会话 #%d），开始录音",
                                          session.session_id)
                            self.pending_group_call = session.is_group
                            session.cancel.set()
                            session = None
                            abnormal_ends = 0
                            active = self.start_recording()
                        elif session.ocr_dead and \
                                time.monotonic() - session.created > ring_timeout:
                            self.log.warning(
                                "OCR 失效且响铃超过 %.0f 秒（会话 #%d），故障回退开录"
                                "（此录音可能包含响铃阶段）", ring_timeout,
                                session.session_id)
                            session.cancel.set()
                            session = None
                            active = self.start_recording()
                        elif session.outcome is not None and \
                                time.monotonic() - session.created >= restart_backoff:
                            # 会话已结束但未确认接通（no_window/proc_exit/error）
                            if session.outcome in ("proc_exit", "error"):
                                abnormal_ends += 1
                            self.log.info("接通检测会话 #%d 结束（%s）但通话窗口仍在，重新检测",
                                          session.session_id, session.outcome)
                            session = None
                            if abnormal_ends >= 3:
                                self.log.warning(
                                    "接通检测连续 %d 次异常结束，故障回退开录"
                                    "（宁可多录不可漏录，此录音可能包含响铃阶段）",
                                    abnormal_ends)
                                active = self.start_recording()
                        # 其余情况：检测仍在进行，继续等
                else:
                    abnormal_ends = 0
                    if session is not None:
                        if session.outcome is None:
                            self.log.info("通话窗口消失（未接通或已取消），不录音，取消接通检测")
                        session.cancel.set()
                        session = None
                    if active:
                        missing_since = missing_since or time.monotonic()
                        if time.monotonic() - missing_since >= grace:
                            self.stop_capture.set()
                            active = False
                            missing_since = None
                            self.log.info("通话结束，准备保存")
            except Exception:
                self.log.exception("监控循环异常")
                time.sleep(2)
            time.sleep(poll)
        # 退出：停止采集并等待保存
        if active:
            self.stop_capture.set()
        if session is not None:
            session.cancel.set()
        self.log.info("监控循环退出")

    def _connect_check_worker(self, session: ConnectSession):
        """接通检测线程：启动 OCR 子进程并消费其输出，写入 session 状态。

        判定依据：响铃阶段窗口没有计时，接通后屏幕出现 mm:ss 计时。
        - TIMER   → session.ok 置位（唯一"真接通"），监控循环开录；
        - NOWINDOW→ outcome=no_window，窗口已消失，不录音；
        - OCRDEAD → session.ocr_dead 置位，监控循环按 ring_timeout 回退；
        - 子进程无 TIMER/NOWINDOW 就退出（proc_exit）或线程异常（error）→
          记为检测故障，绝不置位 ok；监控循环会重启会话，连续 3 次故障才回退开录。
        """
        proc = None
        try:
            cfg = self.cfg
            interval = float(cfg.get("connect_interval_seconds", 2.0))
            debug = 1 if cfg.get("ocr_save_debug_screenshots", False) else 0
            helper = ROOT / "ocr_worker.py"
            py = ROOT / "runtime" / "Python312" / "python.exe"
            if not helper.exists() or not py.exists():
                self.log.warning("接通检测不可用（缺 OCR 脚本/解释器），会话 #%d 记为故障",
                                 session.session_id)
                session.outcome = "error"
                return
            proc = subprocess.Popen(
                [str(py), str(helper), "--mode", "connect",
                 "--interval", str(interval), "--debug", str(debug),
                 "--call-id", session.call_id],
                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW)
            self._connect_read_loop(session, proc)
        except Exception:
            if session.outcome is None:
                session.outcome = "error"
            self.log.exception("接通检测线程异常（会话 #%s，不置位接通，不影响录音主流程）",
                               session.session_id)
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _connect_read_loop(self, session: ConnectSession, proc):
        """消费接通检测子进程输出。

        用队列+带超时的 get 读取（阻塞式 readline 会让取消信号失效）；
        取消请求 0.5 秒内即可响应并终止子进程。
        """
        lq: queue.Queue = queue.Queue()

        def _reader():
            try:
                for ln in proc.stdout:
                    lq.put(ln)
            except Exception:
                pass
            finally:
                lq.put(None)

        threading.Thread(target=_reader, daemon=True,
                         name=f"connect-reader-{session.session_id}").start()
        while True:
            if session.cancel.is_set():
                if session.outcome is None:
                    session.outcome = "cancelled"
                return
            try:
                line = lq.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                # 子进程结束但没有给出 TIMER/NOWINDOW → 异常退出，记为故障
                if session.outcome is None:
                    session.outcome = "proc_exit"
                    self.log.warning("接通检测子进程提前退出且无结论（会话 #%s），记为检测故障",
                                     session.session_id)
                return
            line = line.strip()
            if line.startswith("TIMER:"):
                session.outcome = "timer"
                session.ok.set()
                self.log.info("OCR 检测到通话计时「%s」（会话 #%s），确认已接通",
                              line[6:], session.session_id)
                return
            if line.startswith("NOWINDOW"):
                session.outcome = "no_window"
                self.log.info("接通检测：通话窗口已消失（会话 #%s），未接通不留录音",
                              session.session_id)
                return
            if line.startswith("OCRDEAD:"):
                session.ocr_dead = True
                self.log.warning("接通检测连续读不到窗口文本（OCR 失效，会话 #%s）",
                                 session.session_id)
            elif line.startswith("OCRLINE:"):
                text = line[8:]
                # 群通话标记：「添加成员」按钮只在群通话窗口出现（1对1没有）
                if "添加成员" in text and not session.is_group:
                    session.is_group = True
                    self.log.info("检测到「添加成员」按钮（会话 #%s）→ 本通为群通话",
                                  session.session_id)
                self.log.info("接通检测 OCR: %s", text)
            elif line.startswith("DIAG:"):
                self.log.info("接通检测诊断目录: %s", line[5:])

    # ----- 托盘 -----
    def build_icon(self):
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: f"状态：{STATE_TEXT.get(self.display_state(), self.state)}",
                             None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开设置", self._on_show_settings, default=True),
            pystray.MenuItem(
                lambda item: "暂停自动录音" if self.recording_is_enabled() else "启用自动录音",
                self._on_toggle_record),
            pystray.MenuItem("打开录音目录", self._on_open_dir),
            pystray.MenuItem("查看日志", self._on_open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._on_exit),
        )
        self.icon = pystray.Icon(
            "LocalCallRecorderV2", make_icon_image(self.display_state()),
            f"{APP_TITLE} - {STATE_TEXT.get(self.display_state(), self.state)}", menu)

    def _on_show_settings(self, icon=None, item=None):
        if self.gui:
            self.gui.request("show")

    def _on_toggle_record(self, icon=None, item=None):
        self.set_recording_enabled(not self.recording_is_enabled())
        if self.gui:
            self.gui.request("show")

    def _on_open_dir(self, icon=None, item=None):
        subprocess.Popen(["explorer.exe", str(self.out_dir)])

    def _on_open_log(self, icon=None, item=None):
        subprocess.Popen(["notepad.exe", str(self.log_path())])

    def _on_exit(self, icon=None, item=None):
        self.request_exit()

    def _shutdown(self):
        deadline = time.monotonic() + 20
        if self.record_thread and self.record_thread.is_alive():
            self.stop_capture.set()
            self.record_thread.join(timeout=max(1, deadline - time.monotonic()))
        self.converter.stop(wait_seconds=max(1, deadline - time.monotonic()))
        self.log.info("转换队列已收尾，进程退出")
        if self.gui:
            try:
                self.gui.request("exit")
                time.sleep(0.5)
            except Exception:
                pass
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        ctypes.windll.kernel32.ExitProcess(0)

    def run(self, start_minimized: bool = False):
        if not self.observe_only:
            self.recover_orphan_wavs()
        self.build_icon()
        t = threading.Thread(target=self.monitor_loop, name="monitor", daemon=True)
        t.start()
        # 主线程跑 tkinter 设置窗口（Windows 上 tk 必须在主线程）；
        # 托盘放到独立线程；tkinter 不可用时退回纯托盘模式。
        try:
            from manager_gui import ManagerWindow
            self.gui = ManagerWindow(self, start_minimized=start_minimized)
            threading.Thread(target=self._run_tray, name="tray",
                             daemon=True).start()
            self.gui.run()
        except Exception:
            self.log.exception("设置窗口启动失败，退回纯托盘模式")
            self.gui = None
            self.icon.run()

    def _run_tray(self):
        try:
            self.icon.run()
        except Exception:
            self.log.exception("托盘运行异常")


# ---------- 测试模式（无托盘，直接录 N 秒） ----------
def run_test(cfg: dict, seconds: float):
    out_dir = Path(cfg["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    started = "测试_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    wav = out_dir / f".tmp_{started}.wav"
    stop = threading.Event()
    threading.Timer(seconds, stop.set).start()
    record(stop, wav, cfg["sample_rate"], cfg["block_frames"],
           capture_mode=cfg.get("capture_mode", "process"))
    size = wav.stat().st_size if wav.exists() else 0
    print(f"WAV 写入完成: {wav} ({size} bytes)")
    from audio_capture import measure_wav_level
    if size > 44:
        loop_peak, mic_peak = measure_wav_level(wav)
        print(f"回环声道峰值={loop_peak:.4f} 麦克风声道峰值={mic_peak:.4f}")
    mp3 = out_dir / f"{started}_未知联系人_通话.mp3"
    ok = False
    from converter import convert
    try:
        ok = convert(wav, mp3, cfg["mp3_bitrate"])
    except Exception:
        logging.exception("转换失败")
    if ok:
        wav.unlink()
        print(f"MP3 已保存: {mp3} ({mp3.stat().st_size} bytes)")
        sys.exit(0)
    print("转换失败，临时 WAV 保留")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--console", action="store_true", help="带控制台日志运行")
    parser.add_argument("--test-seconds", type=float, default=0)
    parser.add_argument("--minimized", action="store_true",
                        help="开机自启用：不显示设置窗口，只在托盘运行")
    args = parser.parse_args()

    # 独立任务栏应用标识：让任务栏显示本程序窗口图标而非 pythonw 默认图标，
    # 且不与其他 Python 程序混为一组（仅影响任务栏分组/图标，无系统级修改）
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Local.LocalCallRecorderV2")
    except Exception:
        pass

    cfg = load_config()
    logger = setup_logging(cfg, console=args.console or bool(args.test_seconds))
    # 原生崩溃（段错误等）时把所有线程的调用栈写入文件，便于定位
    try:
        import faulthandler
        fh_path = Path(__file__).resolve().parent / "logs" / "faulthandler.log"
        fh_path.parent.mkdir(parents=True, exist_ok=True)
        fh_file = open(fh_path, "a", encoding="utf-8")
        faulthandler.enable(file=fh_file, all_threads=True)
        logger.info("faulthandler 已启用: %s", fh_path)
    except Exception:
        pass
    logger.info("本地通话录音工具·升级版启动")

    # 单实例保护
    mutex = k2.CreateMutexW(None, False, MUTEX_NAME)
    already = ctypes.get_last_error() == 183
    if not already:
        try:
            already = k.GetLastError() == 183
        except Exception:
            already = False
    if already:
        # 已有实例在运行：把它叫出来，别让用户点了图标却毫无反应
        woken = wake_running_instance(logger)
        logger.info("已有本软件实例在运行，已退出（唤起窗口：%s）",
                    "成功" if woken else "未成功（可能是旧版本进程）")
        return
    show_evt = None
    try:
        show_evt = k2.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    except Exception:
        logger.warning("创建“显示窗口”事件失败，重复启动将无法唤起界面")
    try:
        if args.test_seconds:
            run_test(cfg, args.test_seconds)
            return
        app = RecorderApp(cfg, logger)
        if show_evt:
            threading.Thread(target=watch_show_event,
                             args=(app, show_evt, logger),
                             name="show-watch", daemon=True).start()
        app.run(start_minimized=args.minimized)
    finally:
        try:
            k.CloseHandle(mutex)
        except Exception:
            pass


if __name__ == "__main__":
    main()
