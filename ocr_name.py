# -*- coding: utf-8 -*-
"""联系人姓名本地 OCR 识别（微信自绘窗口专用）。

背景：微信 4.x 通话窗口是自绘窗口（MMUIRenderSubWindowHW），UIA 树里只有
进程名和窗口类名，读不到真实姓名（2026-09-22 实测采样证实）。因此改走
"只截该窗口的姓名区域 + Windows 本地 OCR"，不读桌面其他文字，不上传截图。

链路（2026-09-23 按 Codex 复核意见统一，顺序固定，不做裁剪后翻转补救）：
    1. PrintWindow(PW_RENDERFULLCONTENT) 按【整个窗口】尺寸捕获；
    2. GetDIBits 用【负 biHeight】直接取自顶向下位图 → 图像方向天然正确；
    3. 用 ClientToScreen 算出客户区在窗口图中的偏移，从【方向正确的整窗图】
       里裁出客户区（比例区域一律相对客户区，与截图测量口径一致）；
    4. 在客户区图上按比例裁姓名区域；
    5. 该裁剪图 → Windows OCR（原图 / 2 倍放大两档，仅作识别兜底，
       同一次截图内的两档不算"独立确认"）；
    6. 文本归一化 + 过滤 → 姓名候选。

约束：
- 只截目标窗口自身像素，窗口最小化/纯色/捕获失败一律返回 None（不读桌面文字）；
- 默认不保存截图；开启诊断时也只保留限量样本到 测试录音/ocr_diag/<call_id>/；
- 识别不确定 → None，由调用方落回"未知联系人"，绝不猜测。
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import io
import json
import logging
import re
import tempfile
import time
from pathlib import Path

from PIL import Image

from contact_name import find_call_window, pick_name_from_texts

logger = logging.getLogger("recorder.ocr")

u = ctypes.windll.user32
g = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x00000002
PW_DEFAULT = 0x00000000        # 不带标志的默认模式（与全内容模式互为兜底）
DIB_RGB_COLORS = 0
BI_RGB = 0

u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
u.IsIconic.argtypes = [wt.HWND]
u.PrintWindow.argtypes = [wt.HWND, wt.HDC, ctypes.c_uint]
u.GetDC.argtypes = [wt.HWND]
u.GetDC.restype = wt.HDC
u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
g.CreateCompatibleDC.argtypes = [wt.HDC]
g.CreateCompatibleDC.restype = wt.HDC
g.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
g.CreateCompatibleBitmap.restype = wt.HANDLE
g.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
g.SelectObject.restype = wt.HANDLE
g.DeleteObject.argtypes = [wt.HANDLE]
g.DeleteDC.argtypes = [wt.HDC]
g.GetDIBits.argtypes = [wt.HDC, wt.HANDLE, ctypes.c_uint, ctypes.c_uint,
                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]

# 姓名区域候选（比例相对【客户区】：y1, y2, x1, x2）
# 2026-09-23 用真实通话截图精确测量：姓名（头像下方）位于客户区 y 0.410~0.433。
# 主区域 0.40~0.47 覆盖姓名且上下留余量；上界卡在 0.40 是为了避开上方头像
# （头像底部约 0.389，且头像图片自带文字，混入会被"取更短候选"误选）。
# 注意：这里【不再保留顶部大范围兜底】，避免靠禁用词去掩盖裁剪位置错误。
NAME_REGIONS = (
    (0.40, 0.47, 0.08, 0.92),   # 主区域：头像下方姓名条（实测精确命中）
    (0.385, 0.50, 0.05, 0.95),  # 稍宽兜底（窗口缩放/布局微差时仍只覆盖姓名带）
)
MAX_ATTEMPTS = 3          # 每通电话最多 3 次独立捕获
ATTEMPT_INTERVAL = 2.0    # 每次间隔秒
MAX_SAVED_SHOTS = 3       # 诊断模式下一次通话最多保存的样本数

_dpi_ready = False


def _ensure_dpi_aware():
    """让坐标使用物理像素，避免缩放导致裁剪偏移。"""
    global _dpi_ready
    if _dpi_ready:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            u.SetProcessDPIAware()
        except Exception:
            pass
    _dpi_ready = True


def _hbitmap_to_image(memdc, hbmp, w: int, h: int) -> Image.Image | None:
    """把内存位图转成【方向正确】的 PIL 图：biHeight 取负值 → 自顶向下位图。

    关键：早期实现写正值再手工 FLIP，属于"事后补救"且容易与裁剪顺序混淆；
    这里从源头保证位图行序自顶向下，PIL 按顶向下解码即为正立图像。
    """
    bmp_info = ctypes.create_string_buffer(40)
    ctypes.memset(bmp_info, 0, 40)
    ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_uint32))[0] = 40
    ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_int32))[1] = w
    ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_int32))[2] = -h  # 负值=自顶向下
    ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_uint16))[6] = 1   # biPlanes
    ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_uint16))[7] = 32  # biBitCount
    ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_uint32))[4] = BI_RGB
    size = w * h * 4
    buf = ctypes.create_string_buffer(size)
    got = g.GetDIBits(memdc, hbmp, 0, h, buf, bmp_info, DIB_RGB_COLORS)
    if got == 0:
        return None
    return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")


# 最近一次 capture_client 失败的原因（子进程内单线程使用，成功时清空）。
# 背景：OCR 在独立子进程里跑，子进程的 logger 不会写主日志——
# 只把原因放进返回的 meta，才能经 stdout 传回主进程记录（2026-09-29 晚
# 整晚 <capture_failed> 却查不到原因的教训）。
_capture_error: str | None = None


def last_capture_error() -> str | None:
    return _capture_error


def capture_client(hwnd: int) -> tuple[Image.Image, dict] | None:
    """捕获窗口 → 返回 (客户区图像, 元信息)；最小化/失败返回 None。

    捕获范围与坐标系统一约定：
    - PrintWindow 渲染的是【整个窗口】（含非客户区），因此位图按 GetWindowRect 尺寸创建；
    - 客户区在整窗图中的偏移 = ClientToScreen(0,0) - GetWindowRect 左上角；
    - 比例区域一律相对【客户区图像】计算（与截图测量口径一致，不用固定屏幕坐标）。
    """
    global _capture_error
    _ensure_dpi_aware()
    iconic = bool(u.IsIconic(hwnd))
    if iconic:
        # 最小化窗口通常没有可截画面；但 Windows 会为其保留缩略图快照，
        # 个别情况下 PrintWindow 仍能取到最小化前的最后一帧——
        # 所以不再直接放弃，往下照常尝试（取不到时原因仍记为 minimized）。
        logger.info("通话窗口处于最小化状态，仍尝试捕获（可能取到最小化前快照）")
    wr = wt.RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(wr)):
        _capture_error = "get_window_rect_failed"
        return None
    ww, wh = wr.right - wr.left, wr.bottom - wr.top
    if ww <= 0 or wh <= 0:
        _capture_error = "bad_window_size"
        return None
    cr = wt.RECT()
    if not u.GetClientRect(hwnd, ctypes.byref(cr)):
        _capture_error = "get_client_rect_failed"
        return None
    cw, ch = cr.right - cr.left, cr.bottom - cr.top
    org = wt.POINT(0, 0)
    if not u.ClientToScreen(hwnd, ctypes.byref(org)):
        _capture_error = "client_to_screen_failed"
        return None
    off_x, off_y = org.x - wr.left, org.y - wr.top

    hdc = u.GetDC(hwnd)
    if not hdc:
        _capture_error = "get_dc_failed"
        return None
    memdc = g.CreateCompatibleDC(hdc)
    hbmp = g.CreateCompatibleBitmap(hdc, ww, wh)
    old = g.SelectObject(memdc, hbmp)
    # 微信 4.x 是自绘 / GPU 渲染窗口，不同 PrintWindow 模式拿到的结果不一样：
    # 有的模式返回纯色（当帧未渲染），换一种模式常常就能拿到真实画面。
    # 因此依次尝试几种【整窗】模式，取第一个非纯色结果；全都拿不到才放弃。
    # 只用整窗模式（坐标系一致），才能按下方的 client_offset 正确裁出客户区。
    img = None
    last_err = "printwindow_failed"
    tried: list[int] = []
    for mode in (PW_RENDERFULLCONTENT, PW_DEFAULT):
        if mode in tried:
            continue
        tried.append(mode)
        if not u.PrintWindow(hwnd, memdc, mode):
            last_err = "printwindow_failed"
            continue
        cand = _hbitmap_to_image(memdc, hbmp, ww, wh)
        if cand is None:
            last_err = "get_dibits_failed"
            continue
        lo, hi = cand.convert("L").getextrema()
        if hi - lo < 12:
            last_err = "solid_color"
            continue
        img = cand
        break

    g.SelectObject(memdc, old)
    g.DeleteObject(hbmp)
    g.DeleteDC(memdc)
    u.ReleaseDC(hwnd, hdc)
    if img is None:
        _capture_error = "minimized" if iconic else last_err
        logger.info("PrintWindow 各模式均未取得有效画面（%s，试过模式 %s）",
                    _capture_error, tried)
        return None

    _capture_error = None
    meta = {
        "window_size": [ww, wh],
        "client_size": [cw, ch],
        "client_offset": [off_x, off_y],
    }
    if cw > 0 and ch > 0 and off_x >= 0 and off_y >= 0 \
            and off_x + cw <= ww and off_y + ch <= wh:
        client = img.crop((off_x, off_y, off_x + cw, off_y + ch))
        if client.size == (cw, ch):
            return client, meta
        logger.warning("客户区裁剪尺寸异常 %s != %s，回退整窗图",
                       client.size, (cw, ch))
    else:
        logger.warning("客户区偏移越界 offset=(%s,%s) window=%s client=%s，回退整窗图",
                       off_x, off_y, (ww, wh), (cw, ch))
    meta["client_fallback"] = True
    return img, meta


def capture_region(hwnd: int, region: tuple[float, float, float, float],
                   ) -> tuple[Image.Image, Image.Image, dict] | None:
    """按客户区比例裁剪姓名区域；返回 (裁剪图, 客户区整图, 元信息)。"""
    hit = capture_client(hwnd)
    if hit is None:
        return None
    img, meta = hit
    w, h = img.size
    y1, y2, x1, x2 = region
    box = (int(w * x1), int(h * y1), int(w * x2), int(h * y2))
    if box[2] - box[0] < 10 or box[3] - box[1] < 10:
        global _capture_error
        _capture_error = "region_too_small"
        return None
    meta = dict(meta)
    meta["region"] = [y1, y2, x1, x2]
    meta["crop_box"] = list(box)
    meta["crop_size"] = [box[2] - box[0], box[3] - box[1]]
    return img.crop(box), img, meta


def _ocr_variants(img: Image.Image):
    """同一次截图内的识别兜底：原图与 2 倍放大（仅放大裁剪小图）。

    方向问题已在 capture_client 源头解决，这里【不再做翻转尝试】。
    注意：两档属于同一次截图，不能算两次独立识别。
    """
    big = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    return (("原图", img), ("2x", big))


# ----- OCR -----
def _normalize(text: str) -> str:
    """归一化 OCR 结果：全角标点、中文/数字间空格、字母级 OCR 拆分。

    区别对待两种空格：
    - 中文之间的空格是 OCR 噪声 → 删除；
    - 英文单词之间的空格是合法的（"John Smith"）→ 保留（连续空格压成一个）；
    - 单个字母被 OCR 拆开（"j o h n"）→ 视为噪声合并成单词。
    """
    s = (text or "").replace("：", ":").replace("，", ",")
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)  # 中文间空格
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\d])", "", s)              # 中文↔数字空格
    s = re.sub(r"(?<=[\d])\s+(?=[\u4e00-\u9fff])", "", s)
    s = re.sub(r"(?<=[\d:])\s+(?=[\d:])", "", s)                       # 计时数字间空格
    if re.fullmatch(r"([A-Za-z]\s+)+[A-Za-z]", s):                     # 字母级拆分
        s = re.sub(r"\s+", "", s)
    s = re.sub(r" {2,}", " ", s)                                       # 连续空格压成一个
    return s.strip()


async def _recognize_async(img: Image.Image) -> list[str]:
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    # 用临时文件交给 WinRT（WinRT 需要文件流；写完立即删除，不长期保存）
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(buf.getvalue())
        tmp_path = f.name
    try:
        f = await StorageFile.get_file_from_path_async(tmp_path)
        stream = await f.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = (OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
                  or OcrEngine.try_create_from_user_profile_languages())
        if engine is None:
            stream.close()
            return []
        result = await engine.recognize_async(bitmap)
        lines = [_normalize(l.text) for l in result.lines]
        stream.close()
        return [l for l in lines if l]
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


_com_ready = False


def _ensure_com():
    """WinRT 调用前初始化 COM（MTA）。线程未初始化 COM 时直接调 WinRT 不稳定，
    实测曾致进程级崩溃——因此 OCR 固定放在独立子进程里跑（见 ocr_worker.py）。"""
    global _com_ready
    if _com_ready:
        return
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)   # COINIT_MULTITHREADED
    except Exception:
        pass
    _com_ready = True


def _ocr_image(img: Image.Image) -> list[str]:
    _ensure_com()
    return asyncio.run(_recognize_async(img))


def _save_diag(img_window: Image.Image | None, crop: Image.Image | None,
               meta: dict, ocr_lines: list[str], name: str | None,
               diag_dir: Path | None, seq: int) -> None:
    """限量保存诊断样本 + 元数据（仅在明确开启诊断时调用）。

    限量策略：整窗图只在第 1 个样本保存（体积大），裁剪图与元数据最多存
    MAX_SAVED_SHOTS 个样本。
    """
    if diag_dir is None or seq > MAX_SAVED_SHOTS:
        return
    try:
        diag_dir.mkdir(parents=True, exist_ok=True)
        stem = f"sample{seq}"
        if img_window is not None and seq == 1:
            img_window.save(diag_dir / f"{stem}_window.png")
        if crop is not None:
            crop.save(diag_dir / f"{stem}_crop.png")
        payload = dict(meta)
        payload.update({
            "sample": seq,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ocr_lines": ocr_lines,
            "name": name,
        })
        (diag_dir / f"{stem}_meta.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("保存诊断样本失败")


def recognize_attempt(attempt: int, save_debug: bool = False,
                      diag_dir: Path | None = None, seq: int = 1,
                      ) -> tuple[str | None, list[str], dict]:
    """一次【独立捕获】的联系人识别（链路唯一入口，worker 直接调用）。

    返回 (姓名或 None, OCR 原始行, 元信息)。同一次捕获内原图/2x 只是识别兜底，
    不构成"两次独立识别"。
    """
    meta: dict = {}
    hwnd = find_call_window()
    if not hwnd:
        return None, [], {"error": "no_call_window"}
    region = NAME_REGIONS[attempt % len(NAME_REGIONS)]
    hit = capture_region(hwnd, region)
    if hit is None:
        err = {"error": "capture_failed", "region": list(region)}
        if last_capture_error():
            err["reason"] = last_capture_error()
        return None, [], err
    crop, window_img, meta = hit
    lines: list[str] = []
    name = None
    for label, cand in _ocr_variants(crop):
        try:
            got = _ocr_image(cand)
        except Exception:
            logger.exception("OCR 识别失败(%s)", label)
            continue
        logger.info("OCR 候选(区域%d,%s): %s", attempt % len(NAME_REGIONS),
                    label, got)
        lines.extend(got)
        name = pick_name_from_texts(got)
        if name:
            break
    if name is None or len(name) <= 2:
        # 两种情况走整窗兜底（仍只读本通话窗口自身像素，计时/按钮/状态词
        # 由 pick_name_from_texts 过滤，不读桌面任何文字）：
        # 1) 区域裁剪完全未命中（窗口尺寸/布局差异，姓名不在固定比例框内）；
        # 2) 裁剪只抠出 1~2 个字（多为视频背景噪声，真实姓名极少这么短）。
        try:
            got = _ocr_image(window_img)
            lines.extend(got)
            full_name = pick_name_from_texts(got)
            if full_name:
                meta["name_source"] = "full_window"
                if name:
                    logger.info("裁剪候选「%s」过短，整窗兜底改判: %s", name, full_name)
                else:
                    logger.info("区域裁剪未命中，整窗兜底识别: %s", full_name)
                name = full_name
        except Exception:
            logger.exception("整窗兜底 OCR 失败")
    if save_debug and diag_dir is not None:
        _save_diag(window_img, crop, meta, lines, name, diag_dir, seq)
    return name, lines, meta
