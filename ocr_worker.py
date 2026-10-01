# -*- coding: utf-8 -*-
"""OCR 姓名识别子进程入口（隔离崩溃风险）。

为什么要独立进程：主程序里在后台线程直接调 WinRT/OCR 曾出现进程级崩溃
（2026-09-23 16:16:45 段错误，整段录音丢失）。放到子进程后，即使 OCR 崩溃，
也只是拿不到姓名，录音主程序继续正常写文件。

识别链路只有一份实现（ocr_name.recognize_attempt），本文件只负责：
- 按固定次数做【独立捕获】；
- 要求【两次独立捕获结果一致】才输出姓名，否则输出空（→ 未知联系人）；
- 可选的限量诊断保存（整窗图/裁剪图/元数据/原始 OCR 行）。

用法：
    python.exe ocr_worker.py [--attempts N] [--interval S] [--debug 0|1] [--call-id ID]

输出（stdout，供主程序解析）：
    OCRLINE:<原始行>           每次识别到的文本（含样本序号）
    DIAG:<路径>               诊断样本目录（仅 debug 模式）
    RESULT:<姓名>              成功识别（冒号后为空表示未识别，落回"未知联系人"）
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _stdout(line: str):
    try:
        print(line, flush=True)
    except Exception:
        pass


def run_connect(args) -> int:
    """接通检测模式：轮询 OCR 找「通话计时」文本（如 00:05）。

    依据：通话窗口响铃阶段没有计时，接通后屏幕出现 mm:ss（或 h:mm:ss）计时。
    找到计时 → 输出 TIMER 后退出；窗口消失（未接/取消）→ 输出 NOWINDOW 退出；
    连续多次完全读不到文本 → 输出 OCRDEAD（仅一次），由主程序决定超时回退。
    """
    import ocr_name
    from contact_name import TIMER_RE

    interval = max(0.5, args.interval)
    diag_dir = (Path(__file__).parent / "测试录音" / "ocr_diag" / args.call_id
                if args.debug else None)
    if diag_dir is not None:
        _stdout(f"DIAG:{diag_dir}")

    blank = 0            # 连续"完全读不到文本"次数
    dead_sent = False
    poll = 0
    while True:
        poll += 1
        hwnd = ocr_name.find_call_window()
        if not hwnd:
            _stdout("NOWINDOW:")
            return 0
        hit = ocr_name.capture_client(hwnd)
        if hit is None:
            blank += 1
        else:
            img, meta = hit
            # 诊断：前 3 次轮询保存客户区整图（用于校准计时区域/核对响铃 vs 接通画面）
            if diag_dir is not None and poll <= 3:
                try:
                    diag_dir.mkdir(parents=True, exist_ok=True)
                    img.save(diag_dir / f"connect_sample{poll}.png")
                except Exception:
                    pass
            try:
                lines = ocr_name._ocr_image(img)
            except Exception as exc:
                _stdout(f"OCRLINE:<整窗OCR异常 {exc}>")
                lines = []
            if lines:
                blank = 0
                _stdout(f"OCRLINE:{' | '.join(lines)}")
                for t in lines:
                    if TIMER_RE.match(t.strip()):
                        _stdout(f"TIMER:{t.strip()}")
                        return 0
            else:
                blank += 1
        if blank >= 10 and not dead_sent:
            dead_sent = True
            _stdout("OCRDEAD:")
        time.sleep(interval)


def main() -> int:
    # 强制 stdout 用 UTF-8：管道下 Python 默认跟随系统区域（zh-CN 为 GBK），
    # 而主程序按 UTF-8 解码——开机自启等无特殊环境变量的启动方式下，
    # 中文会变成乱码/替换符（2026-09-26 早上开机首通实测复现）。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["name", "connect"], default="name",
                    help="name=识别联系人（录音开始后）；connect=接通检测（响铃期间轮询计时）")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--debug", type=int, default=0)
    ap.add_argument("--call-id", default="")
    args = ap.parse_args()

    # 进程启动即初始化 DPI 感知 + COM(MTA)，避免首次调用时序问题
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)
    except Exception:
        pass

    import ocr_name

    # 自测用：允许指定窗口句柄（正式运行时自动定位通话窗口）
    test_hwnd = os.environ.get("OCR_TEST_HWND")
    if test_hwnd:
        _hwnd = int(test_hwnd)
        ocr_name.find_call_window = lambda: _hwnd

    if args.mode == "connect":
        return run_connect(args)

    # 通话标识：让诊断样本能与具体通话、具体录音文件对应
    call_id = args.call_id or time.strftime("%Y%m%d-%H%M%S")
    diag_dir = (Path(__file__).parent / "测试录音" / "ocr_diag" / call_id
                if args.debug else None)
    if diag_dir is not None:
        _stdout(f"DIAG:{diag_dir}")

    reads: list[str] = []
    result = None
    start = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        attempts = max(2, args.attempts)   # 至少要能形成"两次一致"
        for attempt in range(attempts):
            name, lines, meta = ocr_name.recognize_attempt(
                attempt, save_debug=bool(args.debug), diag_dir=diag_dir,
                seq=attempt + 1)
            tag = f"样本{attempt + 1}"
            if lines:
                _stdout(f"OCRLINE:{tag} {' | '.join(lines)}")
            elif meta.get("error"):
                detail = meta.get("reason")
                _stdout(f"OCRLINE:{tag} <{meta['error']}"
                        f"{':' + detail if detail else ''}>")
            if name:
                if name in reads:
                    result = name        # 两次独立捕获一致 → 采信
                    break
                # 前缀合并：与已有结果互为前缀（OCR 截断或多识别）→ 取更长版本。
                # 例：样本1"全世界"、样本2"全世界最喜欢的宝宝"是同一个名字的
                # 截断/完整两种读法，不应判为"结果不一致"，也不该选中截断版。
                prev = next((r for r in reads
                             if r.startswith(name) or name.startswith(r)), None)
                if prev:
                    result = prev if len(prev) >= len(name) else name
                    _stdout(f"OCRLINE:<前缀合并「{prev}」+「{name}」→「{result}」>")
                    break
                reads.append(name)
            if attempt < attempts - 1:
                time.sleep(max(0.0, args.interval))
        if result is None and reads:
            # 只有一次有效识别（其余捕获/识别失败或结果不同）→ 拿不准
            if len(set(reads)) > 1:
                _stdout(f"OCRLINE:<两次捕获结果不一致 {reads}，按未知联系人处理>")
            else:
                _stdout(f"OCRLINE:<仅 1 次有效识别 {reads}，缺少独立确认，"
                        f"按未知联系人处理>")
    except Exception as exc:  # 兜底：任何异常都只影响命名
        _stdout(f"OCRLINE:<流程异常 {exc}>")
        result = None

    _stdout(f"OCRLINE:<识别开始 {start} 尝试 {locals().get('attempt', '?') + 1} 次>")
    _stdout(f"RESULT:{result or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
