"""音频采集模块：默认麦克风 + 默认播放设备回环，写入临时 WAV。

每次录制都重新枚举默认设备，不缓存设备编号；
设备拔插、默认设备变化后，下一通电话自动使用新设备。
"""
from __future__ import annotations

import logging
import ctypes
import time
import wave
from pathlib import Path

import numpy as np
import pyaudiowpatch as pa

logger = logging.getLogger("recorder.capture")

WECHAT_PROCESSES = {"weixin.exe", "wechat.exe", "wechatapp.exe", "wechatappcore.exe"}
CALL_TITLE = "微信音视频通话"


def list_device_summary(audio: pa.PyAudio) -> str:
    try:
        mic = audio.get_default_input_device_info()
        loop = audio.get_default_wasapi_loopback()
        return f"麦克风={mic['name']} | 回环={loop['name']}"
    except Exception as exc:  # pragma: no cover
        return f"设备枚举失败: {exc}"


class CaptureError(Exception):
    """设备打开或采集过程中发生的错误。"""


def record(stop, wav_path: Path, sample_rate: int, block_frames: int,
           capture_mode: str = "system") -> None:
    """阻塞式采集直到 stop 被置位。

    capture_mode:
      "system"  左声道=系统回环（含其他应用声音）
      "process" 左声道=仅微信进程音频（Windows Application Loopback）
                失败一律抛 CaptureError 明确报错，绝不静默退回系统全声。

    打开失败抛 CaptureError；采集中途设备错误也抛 CaptureError，
    已写入磁盘的 WAV 由调用方决定是否保留转换。
    """
    if capture_mode == "process":
        return _record_process(stop, wav_path, sample_rate, block_frames)
    return _record_system(stop, wav_path, sample_rate, block_frames)


def _record_system(stop, wav_path: Path, sample_rate: int, block_frames: int) -> None:
    try:
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
    except Exception:
        hr = None

    try:
        with pa.PyAudio() as audio, wave.open(str(wav_path), "wb") as f:
            try:
                mic = audio.get_default_input_device_info()
            except Exception as exc:
                raise CaptureError(f"找不到默认麦克风: {exc}") from exc
            try:
                loopback = audio.get_default_wasapi_loopback()
            except Exception as exc:
                raise CaptureError(f"找不到默认播放设备的回环: {exc}") from exc

            mc = int(mic["maxInputChannels"])
            lc = int(loopback["maxInputChannels"])
            f.setnchannels(2)
            f.setsampwidth(2)
            f.setframerate(sample_rate)

            try:
                mr = audio.open(
                    format=pa.paFloat32, channels=mc, rate=sample_rate,
                    input=True, input_device_index=mic["index"],
                    frames_per_buffer=block_frames,
                )
            except Exception as exc:
                raise CaptureError(f"打开麦克风失败 ({mic['name']}): {exc}") from exc

            sr = None
            try:
                try:
                    sr = audio.open(
                        format=pa.paFloat32, channels=lc, rate=sample_rate,
                        input=True, input_device_index=loopback["index"],
                        frames_per_buffer=block_frames,
                    )
                except Exception as exc:
                    raise CaptureError(f"打开系统声音回环失败 ({loopback['name']}): {exc}") from exc

                logger.info("音频设备已打开: %s", list_device_summary(audio))
                while not stop.is_set():
                    try:
                        a = np.frombuffer(
                            mr.read(block_frames, exception_on_overflow=False),
                            dtype=np.float32).reshape(-1, mc)
                        b = np.frombuffer(
                            sr.read(block_frames, exception_on_overflow=False),
                            dtype=np.float32).reshape(-1, lc)
                    except Exception as exc:
                        raise CaptureError(f"采集过程中设备出错（可能被拔出）: {exc}") from exc
                    n = min(len(a), len(b))
                    if n == 0:
                        continue
                    # 左声道 = 对方声音（系统回环均值），右声道 = 我方麦克风
                    data = np.column_stack((b[:n].mean(axis=1), a[:n].mean(axis=1)))
                    f.writeframes(
                        np.clip(data * 32767, -32768, 32767).astype(np.int16).tobytes())
            finally:
                try:
                    mr.close()
                except Exception:
                    pass
                if sr:
                    try:
                        sr.close()
                    except Exception:
                        pass
    finally:
        if hr in (0, 1):
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass


def measure_wav_level(wav_path: Path) -> tuple[float, float]:
    """返回 (回环声道峰值, 麦克风声道峰值)，用于诊断两路信号。"""
    import wave as _wave
    with _wave.open(str(wav_path), "rb") as f:
        raw = f.readframes(f.getnframes())
    arr = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).astype(np.float32) / 32767.0
    return float(np.abs(arr[:, 0]).max() or 0.0), float(np.abs(arr[:, 1]).max() or 0.0)


def _record_process(stop, wav_path: Path, sample_rate: int, block_frames: int) -> None:
    """进程级采集：对方声道只来自微信进程音频，麦克风照旧共享采集。

    - 目标进程由音频会话枚举确定（实际发声的微信进程），不凭窗口标题；
    - 激活/启动/采集失败 → CaptureError（明确报错，不静默退回系统全声）；
    - 微信中途重启 → 记日志、对方声道静音，窗口监控照常结束录音；
      下一通电话会重新定位进程。
    """
    from wechat_loopback import (LoopbackCaptureError, WeChatLoopbackCapture,
                                 find_wechat_audio_target)
    # COM 按线程生效：pycaw（枚举微信音频会话）要求调用线程已初始化 COM。
    # comtypes 首次导入时只会顺手初始化"导入它的那个线程"，第 2 通电话的
    # 录音线程就没有 COM 了（2026-09-26 09:49 实测复现，同实例首通成功、
    # 之后全部 CO_E_NOTINITIALIZED）。
    # 顺序要求：必须放在 wechat_loopback 导入之后（comtypes 首次导入会在此
    # 线程自行初始化 STA），且模式用 STA 与其一致——先 MTA 后导入会抛
    # "无法在设置线程模式后对其加以更改"。
    _com_owned = False
    try:
        _hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        # S_OK(0)=本线程刚初始化成功；S_FALSE(1)=早已初始化（comtypes 干的），勿动
        _com_owned = (_hr == 0)
    except Exception:
        _com_owned = False
    cap = None
    try:
        try:
            target_pid, target_name = find_wechat_audio_target()
        except LoopbackCaptureError as exc:
            raise CaptureError(f"微信进程音频捕获失败（本次不录音）：{exc}") from exc

        try:
            cap = WeChatLoopbackCapture(target_pid, sample_rate)
            cap.start()
        except LoopbackCaptureError as exc:
            raise CaptureError(
                f"微信进程音频捕获失败（本次不录音，目标进程={target_name} pid={target_pid}）：{exc}") from exc

        with pa.PyAudio() as audio, wave.open(str(wav_path), "wb") as f:
            try:
                mic = audio.get_default_input_device_info()
            except Exception as exc:
                raise CaptureError(f"找不到默认麦克风: {exc}") from exc
            mc = int(mic["maxInputChannels"])
            f.setnchannels(2)
            f.setsampwidth(2)
            f.setframerate(sample_rate)
            try:
                mr = audio.open(
                    format=pa.paFloat32, channels=mc, rate=sample_rate,
                    input=True, input_device_index=mic["index"],
                    frames_per_buffer=block_frames,
                )
            except Exception as exc:
                raise CaptureError(f"打开麦克风失败 ({mic['name']}): {exc}") from exc

            logger.info("进程级捕获已就绪: 对方声道=微信进程(pid=%s,%s) 我方声道=麦克风(%s)",
                        target_pid, target_name, mic["name"])
            try:
                while not stop.is_set():
                    try:
                        a = np.frombuffer(
                            mr.read(block_frames, exception_on_overflow=False),
                            dtype=np.float32).reshape(-1, mc)
                    except Exception as exc:
                        raise CaptureError(f"麦克风采集中途出错（可能被拔出）: {exc}") from exc
                    try:
                        b = cap.read_frames(block_frames, timeout=0.5)
                        cap.raise_if_dead()
                    except LoopbackCaptureError as exc:
                        raise CaptureError(f"微信进程音频流中断（pid={target_pid}）: {exc}") from exc
                    if b is None or len(b) < block_frames:
                        b = np.zeros(block_frames, dtype=np.float32)
                    # 左声道 = 对方声音（仅微信进程音频），右声道 = 我方麦克风
                    data = np.column_stack((b[:block_frames], a[:block_frames].mean(axis=1)))
                    f.writeframes(
                        np.clip(data * 32767, -32768, 32767).astype(np.int16).tobytes())
            finally:
                try:
                    mr.close()
                except Exception:
                    pass
    finally:
        if cap:
            try:
                cap.stop()
            except Exception:
                pass
        if _com_owned:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass
