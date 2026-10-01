# -*- coding: utf-8 -*-
"""本地声学语气曲线（完全离线，零 API 成本）。

从 MP3 提取客观语气信号，供情感分析使用：
- 每分钟音量（RMS）与峰值
- 说话活动占比（高于噪声底的有效语音比例）
- 长静音段（≥60 秒）
- 音量突增/突降时刻（相对前后窗口）
- 整体统计与趋势

输出 Markdown 文本，直接拼进分析提示词，让 AI「看到」语气起伏。
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import numpy as np

import imageio_ffmpeg

WINDOW_SEC = 30          # 基础窗口
NOISE_PERCENTILE = 20    # 噪声底 = 全体帧 RMS 的 20 分位
SPEAK_RATIO = 2.5        # 帧 RMS 超过噪声底的倍数视为「在说话」


def _ffmpeg() -> str:
    return str(next((Path(imageio_ffmpeg.__file__).parent / "binaries")
                    .glob("ffmpeg-win*.exe")))


def _decode_mono(mp3: Path, max_minutes: float | None = None) -> tuple[np.ndarray, int]:
    """解码为单声道 16kHz float32。max_minutes 限制时长（测试用）。"""
    cmd = [_ffmpeg(), "-nostdin", "-v", "error", "-i", str(mp3),
           "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    if max_minutes:
        cmd = cmd[:2] + ["-t", str(int(max_minutes * 60))] + cmd[2:]
    r = subprocess.run(cmd, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"音频解码失败：{r.stderr.decode('utf-8', 'replace')[:300]}")
    data = np.frombuffer(r.stdout, dtype=np.float32)
    return data, 16000


def _frame_rms(data: np.ndarray, sr: int, frame_sec: float = 1.0) -> np.ndarray:
    fl = int(sr * frame_sec)
    n = len(data) // fl
    if n == 0:
        return np.array([])
    x = data[: n * fl].reshape(n, fl)
    return np.sqrt((x ** 2).mean(axis=1))


def _fmt_mmss(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def _level(rms: float, ref: float) -> str:
    if ref <= 0:
        return "—"
    ratio = rms / ref
    if ratio < 0.5:
        return "很轻"
    if ratio < 0.8:
        return "偏轻"
    if ratio < 1.3:
        return "正常"
    if ratio < 2.0:
        return "偏高"
    return "很高"


def analyze(mp3: Path, max_minutes: float | None = None) -> str:
    """返回 Markdown 格式的语气曲线报告。"""
    mp3 = Path(mp3)
    data, sr = _decode_mono(mp3, max_minutes)
    if len(data) < sr * 10:
        return "（录音太短，不足 10 秒，无法分析语气曲线）"
    total_sec = len(data) / sr

    fr = _frame_rms(data, sr, 1.0)
    noise = float(np.percentile(fr[fr > 0], NOISE_PERCENTILE)) if (fr > 0).any() else 0.0
    if noise <= 0:
        noise = float(fr.max()) * 0.01 or 1e-6

    # 整体统计
    speaking = fr > noise * SPEAK_RATIO
    speak_pct = float(speaking.mean() * 100)

    lines: list[str] = []
    lines.append(f"- 录音总时长：{_fmt_mmss(total_sec)}")
    lines.append(f"- 有效说话占比：约 {speak_pct:.0f}%（其余为静音/空白）")

    # 长静音段
    gaps: list[tuple[int, int]] = []
    run = 0
    for i, s in enumerate(speaking):
        if not s:
            run += 1
        else:
            if run >= 60:
                gaps.append((i - run, i))
            run = 0
    if run >= 60:
        gaps.append((len(speaking) - run, len(speaking)))
    if gaps:
        seg = "、".join(f"{_fmt_mmss(a)}–{_fmt_mmss(b)}（{b - a}秒）" for a, b in gaps[:8])
        lines.append(f"- 长静音（≥1 分钟）：{seg}")

    # 每 5 分钟音量概览（先收集，再统一输出）
    win = 300
    n_win = int(np.ceil(total_sec / win))
    win_rows: list[tuple[float, float, float]] = []   # (start, r90, speak_pct)
    for w in range(n_win):
        a, b = w * win, min((w + 1) * win, total_sec)
        seg_fr = fr[int(a):int(b)]
        if len(seg_fr) == 0:
            continue
        seg_speak = seg_fr > noise * SPEAK_RATIO
        pct = float(seg_speak.mean() * 100)
        r = float(np.percentile(seg_fr, 90))       # 90 分位代表该时段「说话时」的音量
        win_rows.append((a, r, pct))
    talking_rms = [r for _, r, _ in win_rows if r > 0]
    ref = float(np.median(talking_rms)) if talking_rms else 1.0

    lines.append("")
    lines.append("### 每 5 分钟音量概览")
    lines.append("| 时段 | 音量水平 | 说话占比 |")
    lines.append("|---|---|---|")
    for a, r, pct in win_rows:
        lines.append(f"| {_fmt_mmss(a)}–{_fmt_mmss(min(a + win, total_sec))} "
                     f"| {_level(r, ref)} | {pct:.0f}% |")

    # 音量突增时刻（90 分位窗口值相对全局中位数 >2.5 倍的连续区块）
    spikes: list[tuple[int, float]] = []
    for w in range(n_win):
        a = w * win
        seg_fr = fr[int(a):int(a + win)]
        if len(seg_fr) == 0:
            continue
        r = float(np.percentile(seg_fr, 90))
        if r > ref * 2.5:
            spikes.append((a, r / ref))
    # 合并相邻
    merged: list[tuple[int, float]] = []
    for a, k in spikes:
        if merged and a - merged[-1][0] <= win:
            continue
        merged.append((a, k))
    if merged:
        lines.append("")
        tops = sorted(merged, key=lambda x: -x[1])[:5]
        lines.append("### 音量明显高于全程水平的时刻（可能情绪激动/大笑/靠近麦克风）")
        for a, k in tops:
            lines.append(f"- {_fmt_mmss(a)} 前后：音量约为全程平均的 {k:.1f} 倍")

    lines.append("")
    lines.append("（以上为客观声学信号：音量=90 分位帧响度相对全程中位数；"
                 "说话占比=高于噪声底 2.5 倍的帧比例。供结合文字稿理解语气节奏。）")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(analyze(Path(sys.argv[1]),
                  max_minutes=float(sys.argv[2]) if len(sys.argv) > 2 else None))
