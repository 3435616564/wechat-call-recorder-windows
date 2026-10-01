# -*- coding: utf-8 -*-
"""本地语音识别（完全离线，零 API 成本）。

用 sherpa-onnx 跑轻量中文 ASR 模型，配合 silero VAD 做静音切分，
把录音转成带时间戳的文字稿。不联网、不上传任何音频。

支持模型（都放在 models/ 下，int8 量化，CPU 可跑）：
- funasr_nano  Fun-ASR-Nano 2512 int8（251 MB，2025-12，中文更强、抗噪）
- sense_voice  SenseVoice Small int8（228 MB，2024-07，速度极快）

用法：
    from local_asr import LocalASR
    asr = LocalASR("funasr_nano")
    segments, info = asr.transcribe(Path("xxx.mp3"))
    # segments: [{"start": 12.3, "end": 15.1, "text": "喂喂听得到吗", "emotion": "..."}]
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import numpy as np

try:  # 与录音主程序共用同一份 ffmpeg，避免额外依赖
    import imageio_ffmpeg
except Exception:  # pragma: no cover - 理论兜底
    imageio_ffmpeg = None

ROOT = Path(__file__).resolve().parent
MODELS_DIR = ROOT / "models"
VAD_MODEL = MODELS_DIR / "silero_vad.onnx"

MODEL_PRESETS = {
    "funasr_nano": {
        "dir": MODELS_DIR / "funasr-nano",
        "desc": "Fun-ASR-Nano 2512 int8（251MB）",
    },
    "sense_voice": {
        "dir": MODELS_DIR / "sense-voice",
        "desc": "SenseVoice Small int8（228MB）",
    },
}
DEFAULT_MODEL = "funasr_nano"

SAMPLE_RATE = 16000
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_ffmpeg() -> str:
    """定位 ffmpeg：优先用运行时自带的 imageio-ffmpeg。"""
    if imageio_ffmpeg is not None:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    exe = ROOT / "runtime" / "Python312" / "Lib" / "site-packages" / \
        "imageio_ffmpeg" / "binaries" / "ffmpeg-win-x86_64-v7.1.exe"
    if exe.exists():
        return str(exe)
    return "ffmpeg"


def probe_duration(media: Path) -> float | None:
    """用 ffmpeg 读媒体真实时长（秒）；失败返回 None。"""
    ff = find_ffmpeg()
    try:
        proc = subprocess.Popen(
            [ff, "-nostdin", "-hide_banner", "-i", str(media)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW)
        _, err = proc.communicate(timeout=15)
        text = (err or b"").decode("utf-8", "ignore")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("Duration:"):
                hms = line[len("Duration:"):].split(",")[0].strip()
                parts = hms.split(":")
                if len(parts) == 3:
                    return (int(parts[0]) * 3600 + int(parts[1]) * 60
                            + float(parts[2]))
    except Exception:
        pass
    return None


def model_available(key: str) -> bool:
    preset = MODEL_PRESETS.get(key)
    if not preset:
        return False
    d = preset["dir"]
    return (d / "model.int8.onnx").exists() and (d / "tokens.txt").exists() \
        and VAD_MODEL.exists()


def available_models() -> list[tuple[str, str]]:
    """返回可用的 (key, 说明) 列表。"""
    return [(k, v["desc"]) for k, v in MODEL_PRESETS.items() if model_available(k)]


class LocalASR:
    """本地离线识别器（懒加载：首次 transcribe 时才加载模型）。"""

    def __init__(self, model_key: str = DEFAULT_MODEL, num_threads: int | None = None):
        if model_key not in MODEL_PRESETS:
            raise ValueError(f"未知模型: {model_key}")
        if not model_available(model_key):
            raise FileNotFoundError(
                f"模型文件不完整：{MODEL_PRESETS[model_key]['dir']}（需要 "
                f"model.int8.onnx + tokens.txt，以及 {VAD_MODEL}）")
        self.model_key = model_key
        self.desc = MODEL_PRESETS[model_key]["desc"]
        self.num_threads = num_threads or max(2, (os.cpu_count() or 4) // 2)
        self._recognizer = None
        self._vad_cfg = None

    def _load(self):
        if self._recognizer is not None:
            return
        import sherpa_onnx

        d = MODEL_PRESETS[self.model_key]["dir"]
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(d / "model.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=self.num_threads,
            use_itn=True,          # 数字/标点规整
            language="zh",         # 中文为主；识别英文片段也能出结果
            debug=False,
            provider="cpu",
        )
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(VAD_MODEL)
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.5
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.silero_vad.max_speech_duration = 20.0
        cfg.sample_rate = SAMPLE_RATE
        self._vad_cfg = cfg
        self._sherpa = sherpa_onnx

    def _iter_pcm(self, media: Path, block_seconds: float = 1.0):
        """用 ffmpeg 把音频流式解码成 16k 单声道 float32（不落临时文件）。"""
        ff = find_ffmpeg()
        cmd = [ff, "-nostdin", "-v", "error", "-i", str(media),
               "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW)
        n_bytes = int(SAMPLE_RATE * block_seconds) * 4
        try:
            while True:
                raw = proc.stdout.read(n_bytes)
                if not raw:
                    break
                yield np.frombuffer(raw, dtype=np.float32)
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def transcribe(self, media: Path, on_progress=None,
                   max_minutes: float | None = None) -> tuple[list[dict], dict]:
        """把音频转成带时间戳的段落。

        on_progress(已处理秒数, 总秒数) 可选，用于界面进度条。
        返回 (segments, info)。
        """
        self._load()
        sherpa = self._sherpa
        media = Path(media)
        if not media.exists():
            raise FileNotFoundError(f"音频不存在: {media}")

        vad = sherpa.VoiceActivityDetector(self._vad_cfg,
                                           buffer_size_in_seconds=120)
        recognizer = self._recognizer
        segments: list[dict] = []
        emotions: list[str] = []
        fed = 0.0
        limit = max_minutes * 60 if max_minutes else None
        # 真实总时长：优先探测媒体文件，失败时退回上限值；再不行就是 None
        total = probe_duration(media)
        if total is not None and limit:
            total = min(total, limit)
        elif total is None and limit:
            total = limit
        t0 = time.time()
        last_report = -1.0
        window = 512  # silero VAD 要求的窗口长度

        def drain():
            while not vad.empty():
                seg = vad.front
                samples = seg.samples
                # 注意：sherpa-onnx 的 VAD 段起止是「采样点数」（16kHz），换算成秒
                st = float(getattr(seg, "start", 0.0)) / float(SAMPLE_RATE)
                stream = recognizer.create_stream()
                stream.accept_waveform(SAMPLE_RATE, samples)
                recognizer.decode_stream(stream)
                text = (stream.result.text or "").strip()
                if text:
                    item = {"start": st, "end": st + len(samples) / SAMPLE_RATE,
                            "text": text}
                    emo = getattr(stream.result, "emotion", "") or ""
                    if emo:
                        item["emotion"] = emo
                        emotions.append(emo)
                    segments.append(item)
                vad.pop()

        for block in self._iter_pcm(media):
            if limit and fed >= limit:
                break
            for i in range(0, len(block) - window + 1, window):
                vad.accept_waveform(block[i:i + window])
            fed += len(block) / SAMPLE_RATE
            drain()
            # 每 2 秒上报一次进度（total 可能为 None，由界面自行降级显示）
            if on_progress and fed - last_report >= 2.0:
                last_report = fed
                on_progress(fed, total)
        vad.flush()
        drain()
        if on_progress:
            on_progress(fed, total if total is not None else fed)

        info = {
            "model": self.model_key,
            "model_desc": self.desc,
            "segments": len(segments),
            "duration": round(fed, 1),
            "elapsed": round(time.time() - t0, 1),
            "speed": round(fed / max(time.time() - t0, 0.001), 1),
            "emotions": emotions,
        }
        return segments, info


def fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def segments_to_text(segments: list[dict]) -> str:
    return "\n".join(f"[{fmt_ts(s['start'])}] {s['text']}" for s in segments) + "\n"


if __name__ == "__main__":  # 手动测试：python local_asr.py <音频> [模型]
    import sys
    key = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL
    asr = LocalASR(key)
    print(f"引擎: {asr.desc}")
    segs, info = asr.transcribe(Path(sys.argv[1]))
    print(segments_to_text(segs)[:2000])
    print("INFO:", info)
