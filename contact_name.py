"""联系人识别模块（第一阶段版：UIA 可访问文本，无截图、无 OCR）。

流程：
1. EnumWindows 找标题为「微信音视频通话」且属于微信进程的可见窗口
   （判定逻辑与主监控完全一致）。
2. 用 UIA（UI Automation）枚举该窗口全部元素的 Name 文本。
3. 过滤：空文本、纯数字、通话时长（如 12:34）、按钮/状态词黑名单。
4. 从剩余候选中选最像联系人名的文本；群通话场景实测后再细化。
5. 读不到返回 None —— 调用方必须降级为「未知联系人」，不能漏录。

OCR 是备选方案：仅当实测 UIA 拿不到名字时才评估，且只截一次窗口局部。
"""
from __future__ import annotations

import ctypes
import logging
import re
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger("recorder.contact")

CALL_TITLE = "微信音视频通话"
WECHAT_PROCESSES = {"weixin.exe", "wechat.exe", "wechatapp.exe", "wechatappcore.exe"}

u = ctypes.windll.user32
k = ctypes.windll.kernel32
PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
u.IsWindowVisible.argtypes = [wintypes.HWND]
u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k.OpenProcess.restype = wintypes.HANDLE
k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
k.CloseHandle.argtypes = [wintypes.HANDLE]

# 按钮词/状态词/标题黑名单（后续实测通话窗口后可补充）
BLACKLIST = {
    "微信音视频通话", "微信", "WeChat",
    "挂断", "取消", "接听", "拒绝", "回拨", "重拨",
    "静音", "取消静音", "免提", "扬声器", "麦克风",
    "摄像头", "开启摄像头", "关闭摄像头", "开启视频", "关闭视频",
    "切换语音", "切换视频", "语音通话", "视频通话",
    "正在等待对方接受邀请", "等待对方接受邀请", "正在等待",
    "对方已取消", "对方已拒绝", "已取消", "已拒绝", "无人接听",
    "网络不佳", "通话中", "邀请", "邀请更多", "添加",
}
TIME_RE = re.compile(r"^[\d\s:．.，,]+$")          # 纯数字/冒号（时长）
# 允许内部空格（英文名 "John Smith"），但首尾不能是空格
_NAME_CHAR = r"[\u4e00-\u9fffA-Za-z0-9\u00b7@_\-（）()、]"
NAME_OK_RE = re.compile(
    rf"^{_NAME_CHAR}({_NAME_CHAR}| {_NAME_CHAR}){{0,38}}{_NAME_CHAR}$"
    rf"|^{_NAME_CHAR}$")
TIMER_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")  # 接通后的计时文本 00:05 / 1:02:33
RINGING_MARKERS = ("等待", "正在呼叫", "响铃", "未接听", "接听", "拒绝", "取消", "回拨")
# UIA 有时会读到进程名文本（实测出现过 "Weixin"），不是联系人名
PROC_NAME_RE = re.compile(
    r"^(weixin|wechat|wechatapp|wechatappcore)(\.exe)?$", re.IGNORECASE)
# UIA 会读到微信自绘窗口的类名（实测出现过 "MMUIRenderSubWindowHW"），不是联系人名
WINDOW_CLASS_RE = re.compile(
    r"^(MM|CW|WeU?I)[A-Za-z]*?(Window|Render|SubWindow|View|HWND)[A-Za-z]*$", re.IGNORECASE)


def _window_area(hwnd: int) -> int:
    """窗口面积（像素²）；取不到返回 0。"""
    r = wintypes.RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(r)):
        return 0
    return max(0, r.right - r.left) * max(0, r.bottom - r.top)


def find_call_window() -> int | None:
    """返回通话窗口句柄；没有则 None。

    同名窗口可能有多个（微信会留下若干同标题的辅助/壳窗口）。早先直接取
    EnumWindows 遇到的第一个，有可能拿到零尺寸的壳窗口 —— 那样后面
    PrintWindow 截图必然失败（表现为一连串 <capture_failed>）。
    现在按【窗口面积】取最大的那个：真正的通话窗口是可见的大窗口。
    """
    hit: list[tuple[int, int]] = []          # (面积, hwnd)

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
                    hit.append((_window_area(hwnd), hwnd))
            finally:
                k.CloseHandle(h)
        return True

    u.EnumWindows(cb, 0)
    if not hit:
        return None
    hit.sort(key=lambda t: t[0], reverse=True)
    if len(hit) > 1:
        logger.info("找到 %d 个同标题通话窗口，按面积取最大的：%s", len(hit), hit)
    return hit[0][1]


def _collect_names(hwnd: int) -> list[str]:
    """用 UIA 枚举窗口内全部元素的 Name，按树序返回。"""
    import comtypes.client
    mod = comtypes.client.GetModule("UIAutomationCore.dll")
    ui = comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation)
    element = ui.ElementFromHandle(hwnd)
    elems = element.FindAll(mod.TreeScope_Descendants, ui.CreateTrueCondition())
    names = []
    for i in range(elems.Length):
        try:
            name = elems.GetElement(i).CurrentName
        except Exception:
            continue
        if name and name.strip():
            names.append(name.strip())
    return names


def pick_name_from_texts(texts: list[str] | None) -> str | None:
    """从候选文本里挑出最像联系人名的那个；无合适候选返回 None。

    UIA 与 OCR 两条路共用同一套过滤规则（黑名单/计时/进程名/窗口类名）。
    """
    if not texts:
        return None
    candidates: list[str] = []
    seen = set()
    for text in texts:
        t = (text or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        if (t in BLACKLIST or TIME_RE.match(t) or PROC_NAME_RE.match(t)
                or WINDOW_CLASS_RE.match(t)):
            continue
        if any(bad in t for bad in ("通话", "挂断", "静音", "邀请", "等待",
                                    "麦克风", "扬声器", "摄像头", "免提",
                                    "已开", "已关")):
            continue
        if not NAME_OK_RE.match(t):
            continue
        candidates.append(t)
    if not candidates:
        logger.info("候选文本均被过滤，未识别到联系人 (候选=%d)", len(texts))
        return None
    # 更短的一般是主名称（昵称在窗口上方居中显示）
    candidates.sort(key=len)
    return candidates[0]


def read_contact_name() -> str | None:
    """读取当前通话窗口的联系人名（UIA 路线，实测微信自绘窗口无效）；失败返回 None。"""
    texts = get_call_window_texts()
    if texts is None:
        return None
    return pick_name_from_texts(texts)


def get_call_window_texts() -> list[str] | None:
    """返回通话窗口的全部可读文本；无通话窗口返回 None。"""
    hwnd = find_call_window()
    if not hwnd:
        return None
    try:
        import comtypes
        comtypes.CoInitialize()
        try:
            return _collect_names(hwnd)
        finally:
            comtypes.CoUninitialize()
    except Exception:
        logger.exception("UIA 读取通话窗口失败")
        return None


def classify_call_state() -> tuple[str, list[str] | None]:
    """把通话窗口状态分为三态，避免"读不到计时器"被误判成响铃：

    - "connected"：窗口出现计时文本（00:05 等），明确已接通
    - "ringing"：出现接听/拒绝/等待等响铃标志词，明确未接通
    - "unknown"：有窗口但两者都识别不到，无法判断（可能 UIA 拿不全）
    - "no_window"：无通话窗口或读取失败

    返回 (状态, 原始文本列表或 None)。文本列表用于日志采样诊断。
    """
    texts = get_call_window_texts()
    if texts is None:
        return "no_window", None
    for t in texts:
        if TIMER_RE.match(t.strip()):
            return "connected", texts
    for t in texts:
        if any(m in t for m in RINGING_MARKERS):
            return "ringing", texts
    return "unknown", texts


def call_connected() -> bool | None:
    """True=明确接通，False=明确响铃，None=无窗口或无法判断。

    注意：None 不等于"未接通"，调用方必须把无法判断当作可疑情况处理，
    不能静默当成响铃一直等待（否则会造成漏录）。
    """
    state, _ = classify_call_state()
    return {"connected": True, "ringing": False}.get(state)


_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')


def sanitize_contact(name: str | None, max_len: int = 60) -> str | None:
    """把识别到的名字清理成合法文件名片段；无效则 None。"""
    if not name:
        return None
    t = _ILLEGAL_RE.sub("", name).strip(" .")
    if not t or t == "未知联系人":
        return None
    return t[:max_len]
