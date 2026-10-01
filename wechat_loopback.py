# -*- coding: utf-8 -*-
"""微信进程级音频捕获（Windows Application Loopback API，ctypes 原生实现）。

原理：
  ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK) +
  AUDCLNT_PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
  → 只激活目标进程（含子进程树）的渲染音频流，其他应用的音频完全不进入本流。

目标进程的确定（不凭窗口标题假设）：
  find_wechat_audio_target() 用音频会话枚举（pycaw）找"当前真的持有
  音频会话的微信家族进程"，优先选 Weixin.exe 主进程（树模式会覆盖其子进程
  WeChatAppEx 等）。找不到任何微信音频会话 → 抛 LoopbackCaptureError，
  上层必须明确提示，禁止静默退回系统全声。

失败处理：
  激活失败 / 格式不支持 / 启动失败 / 采集中断 一律抛 LoopbackCaptureError，
  由 audio_capture 转成 CaptureError，主程序弹通知并记日志（本次不录音）。

已知边界（不可宣传为"绝对只录通话"）：
  微信进程树内的一切声音（包括消息提示音、语音条外放）都会被采集。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time
from pathlib import Path

import numpy as np
from comtypes import GUID

logger = logging.getLogger("recorder.wechat_loopback")

HRESULT = ctypes.c_long
E_NOINTERFACE = -2147467262          # 0x80004002
WAVE_FORMAT_IEEE_FLOAT = 3

# ★ 关键：这不是 GUID，是字符串宏（audioclientactivationparams.h:
#   #define VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK L"VAD\\Process_Loopback"）。
# 传 GUID 字符串会被当成未知设备路径 → E_INVALIDARG（已踩坑验证）。
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
IID_IActivateAudioInterfaceCompletionHandler = GUID(
    "{72A22DF6-AA69-4690-89C7-3B945C03D7B7}")
IID_IAgileObject = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
IID_IUnknown = GUID("{00000000-0000-0000-C000-000000000046}")

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0

WECHAT_PROCESSES = {"weixin.exe", "wechat.exe", "wechatapp.exe", "wechatappcore.exe"}

k32 = ctypes.windll.kernel32


class LoopbackCaptureError(Exception):
    """进程级捕获失败（激活/初始化/启动/中断）。上层必须明确提示。"""


# ---------- 基础 COM 辅助 ----------

def _vtable(obj_ptr: int):
    """COM 对象指针 → 方法指针数组。"""
    vtbl_addr = ctypes.cast(obj_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    return ctypes.cast(vtbl_addr, ctypes.POINTER(ctypes.c_void_p))


def _call(vt, index, proto, *args):
    fn = proto(vt[index])
    return fn(*args)


def raw_query_interface(obj_ptr: int, iid: GUID) -> int:
    """通过 vtable 调 QueryInterface，返回新接口指针（int）。"""
    out = ctypes.c_void_p()
    hr = _call(_vtable(obj_ptr), 0, ctypes.WINFUNCTYPE(
        HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
        obj_ptr, ctypes.byref(iid), ctypes.byref(out))
    if hr != 0 or not out.value:
        raise LoopbackCaptureError(
            f"QueryInterface({iid}) 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
    return out.value


# ---------- 激活参数结构体 ----------

class _ProcessLoopbackParams(ctypes.Structure):
    # 官方成员顺序：TargetProcessId 在前，ProcessLoopbackMode 在后
    _fields_ = [("TargetProcessId", wt.DWORD),
                ("ProcessLoopbackMode", ctypes.c_int)]


class _AudioClientActivationParams(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int),
                ("ProcessLoopbackParams", _ProcessLoopbackParams)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", wt.USHORT), ("wReserved1", wt.USHORT),
                ("wReserved2", wt.USHORT), ("wReserved3", wt.USHORT),
                ("cbSize", wt.ULONG), ("pBlobData", ctypes.c_void_p)]


class _WaveFormatEx(ctypes.Structure):
    _fields_ = [("wFormatTag", wt.WORD), ("nChannels", wt.WORD),
                ("nSamplesPerSec", wt.DWORD), ("nAvgBytesPerSec", wt.DWORD),
                ("nBlockAlign", wt.WORD), ("wBitsPerSample", wt.WORD),
                ("cbSize", wt.WORD)]


class _ActivateHandler:
    """IActivateAudioInterfaceCompletionHandler + IAgileObject（原始 vtable 实现）。

    必须同时实现 IAgileObject，否则 ActivateAudioInterfaceAsync 返回
    E_ILLEGAL_METHOD_CALL（微软 ApplicationLoopback 样例注明）。
    对象生命周期由本类引用持有，AddRef/Release 固定返回 1。
    """

    def __init__(self):
        self.event = k32.CreateEventW(None, True, False, None)
        if not self.event:
            raise LoopbackCaptureError("CreateEventW 失败")
        self.hr_result: int | None = None
        self.result_unk: int | None = None

        proto_qi = ctypes.WINFUNCTYPE(
            HRESULT, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p))
        proto_none = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)
        proto_done = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p)

        def _qi(_self, riid, out):
            iid_bytes = ctypes.string_at(riid, 16)
            import uuid as _uuid
            logger.debug("handler QI: %s", _uuid.UUID(bytes_le=iid_bytes))
            if iid_bytes in (bytes(IID_IAgileObject), bytes(IID_IUnknown),
                             bytes(IID_IActivateAudioInterfaceCompletionHandler)):
                out[0] = _self
                return 0
            return E_NOINTERFACE

        def _addref(_self):
            return 1

        def _release(_self):
            return 1

        def _completed(_self, op_ptr):
            try:
                hr = HRESULT()
                unk = ctypes.c_void_p()
                fn = ctypes.WINFUNCTYPE(
                    HRESULT, ctypes.c_void_p, ctypes.POINTER(HRESULT),
                    ctypes.POINTER(ctypes.c_void_p))(_vtable(op_ptr)[3])
                gr = fn(op_ptr, ctypes.byref(hr), ctypes.byref(unk))
                self.hr_result = hr.value
                self.result_unk = unk.value
                self._gr_ret = gr
                logger.debug("Completed: GetResults 返回 %s, 激活 hr=%s, unk=%s",
                             gr, hr.value, unk.value)
            except Exception:
                logger.exception("Completed 回调异常")
                self.hr_result = self.hr_result or -1
            finally:
                k32.SetEvent(self.event)
            return 0

        self._fns = [_qi, _addref, _release, _completed]
        # 回调跳板对象必须长期持有，否则被 GC 后 vtable 指向已释放内存（崩溃）
        self._cb_qi = proto_qi(_qi)
        self._cb_addref = proto_none(_addref)
        self._cb_release = proto_none(_release)
        self._cb_done = proto_done(_completed)
        self._vtable = (ctypes.c_void_p * 4)(
            ctypes.cast(self._cb_qi, ctypes.c_void_p),
            ctypes.cast(self._cb_addref, ctypes.c_void_p),
            ctypes.cast(self._cb_release, ctypes.c_void_p),
            ctypes.cast(self._cb_done, ctypes.c_void_p))
        # COM 对象头：首个 8 字节必须是 vtable 地址。
        # 注意不能直接 cast(vtable数组, c_void_p)——那会得到"指向数组首元素"的
        # 指针，COM 读到的 *obj 是 QI 跳板地址而非 vtable 地址（已踩坑：access violation）。
        self._vtbl_cell = ctypes.c_void_p(ctypes.addressof(self._vtable))
        self.obj = ctypes.cast(ctypes.byref(self._vtbl_cell), ctypes.c_void_p)


# ActivateAudioInterfaceAsync 在 mmdevapi.dll
_mmdevapi = ctypes.WinDLL("mmdevapi.dll")
_ActivateAudioInterfaceAsync = _mmdevapi.ActivateAudioInterfaceAsync
_ActivateAudioInterfaceAsync.restype = HRESULT
_ActivateAudioInterfaceAsync.argtypes = [
    wt.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p)]


def activate_process_loopback_client(target_pid: int, timeout: float = 10.0) -> int:
    """激活目标进程树的循环回环音频客户端，返回 IAudioClient 指针。"""
    params = _AudioClientActivationParams(
        ActivationType=AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        ProcessLoopbackParams=_ProcessLoopbackParams(
            TargetProcessId=target_pid,
            ProcessLoopbackMode=PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE))
    pv = _PropVariant(vt=65, wReserved1=0, wReserved2=0, wReserved3=0,   # VT_BLOB
                      cbSize=ctypes.sizeof(params),
                      pBlobData=ctypes.cast(ctypes.byref(params), ctypes.c_void_p))
    handler = _ActivateHandler()
    op = ctypes.c_void_p()
    iid_ref = ctypes.cast(ctypes.byref(IID_IAudioClient), ctypes.c_void_p)
    pv_ref = ctypes.cast(ctypes.byref(pv), ctypes.c_void_p)
    hr = _ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, iid_ref,
        pv_ref, handler.obj, ctypes.byref(op))
    if hr != 0:
        raise LoopbackCaptureError(
            f"ActivateAudioInterfaceAsync 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
    if not op.value:
        raise LoopbackCaptureError("ActivateAudioInterfaceAsync 未返回操作对象")
    wait = k32.WaitForSingleObject(handler.event, int(timeout * 1000))
    if wait != 0:
        raise LoopbackCaptureError(f"音频接口激活超时（{timeout:.0f} 秒）")
    if handler.hr_result is None or handler.hr_result != 0:
        code = 0 if handler.hr_result is None else handler.hr_result & 0xFFFFFFFF
        raise LoopbackCaptureError(f"音频接口激活结果失败 hr=0x{code:08X}")
    if not handler.result_unk:
        raise LoopbackCaptureError("音频接口激活成功但未返回对象")
    return raw_query_interface(handler.result_unk, IID_IAudioClient)


# ---------- 进程级捕获 ----------

class WeChatLoopbackCapture:
    """一个通话一个实例。start() 后台线程持续采集目标进程音频，
    read_frames() 供混音循环取 float32 单声道块。stop() 释放设备。"""

    def __init__(self, target_pid: int, sample_rate: int = 48000):
        self.target_pid = int(target_pid)
        self.sample_rate = int(sample_rate)
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._buf: list[np.ndarray] = []
        self._buf_frames = 0
        self._error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._packets = 0
        self._frames = 0
        self._client_ptr: int | None = None
        self._event: int | None = None
        self._last_pid_alive_log = 0.0

    # -- 生命周期 --
    def start(self):
        try:
            client = activate_process_loopback_client(self.target_pid)
        except LoopbackCaptureError:
            raise
        except Exception as exc:
            raise LoopbackCaptureError(f"音频接口激活异常: {exc}") from exc
        self._client_ptr = client
        fmt = _WaveFormatEx(wFormatTag=WAVE_FORMAT_IEEE_FLOAT, nChannels=2,
                            nSamplesPerSec=self.sample_rate,
                            nAvgBytesPerSec=self.sample_rate * 8,
                            nBlockAlign=8, wBitsPerSample=32, cbSize=0)
        self._event = k32.CreateEventW(None, False, False, None)
        if not self._event:
            raise LoopbackCaptureError("CreateEventW 失败")
        try:
            # Initialize(SHARED, LOOPBACK|EVENTCALLBACK, 200000hns=20ms, 0, fmt, NULL)
            hr = _call(_vtable(client), 3, ctypes.WINFUNCTYPE(
                HRESULT, ctypes.c_void_p, ctypes.c_long, ctypes.c_long,
                ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p,
                ctypes.c_void_p),
                client, AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                200000, 0, ctypes.byref(fmt), None)
            if hr != 0:
                raise LoopbackCaptureError(
                    f"音频客户端初始化失败 hr=0x{hr & 0xFFFFFFFF:08X}"
                    + ("（格式不支持）" if (hr & 0xFFFFFFFF) == 0x88890008 else ""))
            hr = _call(_vtable(client), 13, ctypes.WINFUNCTYPE(
                HRESULT, ctypes.c_void_p, wt.HANDLE),
                client, self._event)
            if hr != 0:
                raise LoopbackCaptureError(
                    f"设置事件句柄失败 hr=0x{hr & 0xFFFFFFFF:08X}")
            capture = ctypes.c_void_p()
            hr = _call(_vtable(client), 14, ctypes.WINFUNCTYPE(
                HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                client, ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(capture))
            if hr != 0 or not capture.value:
                raise LoopbackCaptureError(
                    f"获取采集接口失败 hr=0x{hr & 0xFFFFFFFF:08X}")
            self._capture_ptr = capture.value
            hr = _call(_vtable(client), 10, ctypes.WINFUNCTYPE(
                HRESULT, ctypes.c_void_p), client)
            if hr != 0:
                raise LoopbackCaptureError(
                    f"音频流启动失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        except LoopbackCaptureError:
            raise
        except Exception as exc:
            raise LoopbackCaptureError(f"音频流初始化异常: {exc}") from exc
        self._thread = threading.Thread(
            target=self._run, name=f"wechat-loopback-{self.target_pid}", daemon=True)
        self._thread.start()

    def stop(self):
        """只设停止事件并等采集线程退出；COM 调用全部在采集线程内完成，
        避免跨线程并发操作同一 COM 客户端导致原生崩溃。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)
        if self._thread and self._thread.is_alive():
            logger.warning("进程捕获线程未在 6 秒内退出（COM 对象不再触碰，仅泄漏不崩溃）")
        self._client_ptr = None

    @property
    def stats(self) -> str:
        return f"包={self._packets} 帧={self._frames}"

    def raise_if_dead(self):
        if self._error:
            raise self._error

    # -- 采集线程 --
    def _pid_alive(self) -> bool:
        h = k32.OpenProcess(0x1000, False, self.target_pid)
        if h:
            k32.CloseHandle(h)
            return True
        return False

    def _run(self):
        try:
            hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            hr = None
        cap = self._capture_ptr
        proto_next = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p,
                                        ctypes.POINTER(wt.UINT))
        proto_get = ctypes.WINFUNCTYPE(
            HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.DWORD),
            ctypes.POINTER(ctypes.c_longlong), ctypes.POINTER(ctypes.c_longlong))
        proto_rel = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, wt.UINT)
        try:
            while not self._stop.is_set():
                k32.WaitForSingleObject(self._event, 300)
                while True:
                    packet = wt.UINT(0)
                    hr1 = _call(_vtable(cap), 5, proto_next, cap,
                                ctypes.byref(packet))
                    if hr1 != 0:
                        raise LoopbackCaptureError(
                            f"GetNextPacketSize 失败 hr=0x{hr1 & 0xFFFFFFFF:08X}")
                    if packet.value == 0:
                        break
                    data = ctypes.c_void_p()
                    frames = wt.UINT(0)
                    flags = wt.DWORD(0)
                    dpos = ctypes.c_longlong(0)
                    qpc = ctypes.c_longlong(0)
                    hr2 = _call(_vtable(cap), 3, proto_get, cap,
                                ctypes.byref(data), ctypes.byref(frames),
                                ctypes.byref(flags), ctypes.byref(dpos),
                                ctypes.byref(qpc))
                    if hr2 != 0:
                        raise LoopbackCaptureError(
                            f"GetBuffer 失败 hr=0x{hr2 & 0xFFFFFFFF:08X}")
                    try:
                        n = frames.value
                        if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                            block = np.zeros(n, dtype=np.float32)
                        else:
                            raw = ctypes.string_at(data.value, n * 8)
                            block = np.frombuffer(raw, dtype=np.float32) \
                                        .reshape(-1, 2).mean(axis=1) \
                                        .astype(np.float32)
                        self._packets += 1
                        self._frames += n
                        with self._cond:
                            self._buf.append(block)
                            self._buf_frames += n
                            self._cond.notify_all()
                    finally:
                        _call(_vtable(cap), 4, proto_rel, cap, frames.value)
                # 目标进程存活检查（微信进程树被关闭时记录，不打断录音）
                now = time.monotonic()
                if now - self._last_pid_alive_log > 5 and not self._pid_alive():
                    self._last_pid_alive_log = now
                    logger.warning("目标微信进程 pid=%s 已不存在（进程树可能重启），"
                                   "本段后续对方声道将无声", self.target_pid)
        except Exception as exc:
            self._error = exc
            logger.error("微信进程捕获线程中断: %s", exc)
            with self._cond:
                self._cond.notify_all()
        finally:
            # 停流必须在本采集线程内做，避免与 GetBuffer 并发
            try:
                cli = self._client_ptr
                if cli:
                    _call(_vtable(cli), 11, ctypes.WINFUNCTYPE(
                        HRESULT, ctypes.c_void_p), cli)   # IAudioClient::Stop
            except Exception:
                pass
            if hr in (0, 1):
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass
            with self._cond:
                self._cond.notify_all()

    # -- 供混音循环消费 --
    def read_frames(self, n: int, timeout: float = 2.0) -> np.ndarray | None:
        """返回 n 帧 float32 单声道（对方声道）；数据不足且流仍活着返回 None。"""
        with self._cond:
            deadline = time.monotonic() + timeout
            while self._buf_frames < n and not self._stop.is_set() and not self._error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(0.2, remaining))
            if self._error:
                raise self._error
            if self._buf_frames < n:
                if self._stop.is_set():
                    return None
                return None       # 流活着但暂时没数据（正常静默）
            out = []
            got = 0
            while got < n:
                b = self._buf.pop(0)
                self._buf_frames -= len(b)
                out.append(b)
                got += len(b)
            data = np.concatenate(out) if len(out) > 1 else out[0]
            return data[:n] if len(data) > n else data


# ---------- 目标进程确定（按实际音频会话，不按窗口标题） ----------

def _proc_name(pid: int) -> str:
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return "?"
    try:
        p = ctypes.create_unicode_buffer(32768)
        z = wt.DWORD(len(p))
        if k32.QueryFullProcessImageNameW(h, 0, p, ctypes.byref(z)):
            return Path(p.value).name
        return "?"
    finally:
        k32.CloseHandle(h)


def list_wechat_audio_sessions() -> list[tuple[int, str, int]]:
    """枚举默认播放设备上微信家族进程的音频会话。

    返回 [(pid, 进程名, 会话状态)]；状态 1=active 0=inactive。
    找不到返回空列表 —— 调用方必须明确报错，不得退回系统全声。
    """
    from pycaw.pycaw import AudioUtilities
    out = []
    for s in AudioUtilities.GetAllSessions():
        try:
            pid = s.ProcessId
            name = _proc_name(pid).lower()
            if name in WECHAT_PROCESSES:
                state = int(s.State) if s.State is not None else -1
                out.append((pid, name, state))
        except Exception:
            continue
    return out


def find_wechat_audio_target() -> tuple[int, str]:
    """确定进程级捕获目标：优先微信主进程（树模式覆盖其子进程）。

    依据音频会话枚举确定"实际发声/持有音频会话的微信进程"，
    不依赖窗口标题。找不到 → LoopbackCaptureError。
    """
    sessions = list_wechat_audio_sessions()
    if not sessions:
        raise LoopbackCaptureError(
            "未发现微信音频会话（微信没在播放声音，或音频走了非默认设备）——"
            "进程级捕获无法定位目标，本次拒绝录音")
    # 同名主进程（weixin.exe）优先；否则取第一个会话进程
    mains = [(pid, n) for pid, n, _ in sessions if n == "weixin.exe"]
    pid, name = mains[0] if mains else sessions[0][:2]
    logger.info("进程捕获目标: pid=%s %s（音频会话: %s）",
                pid, name, [(p, n, s) for p, n, s in sessions])
    return pid, name
