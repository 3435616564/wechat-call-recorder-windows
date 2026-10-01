# -*- coding: utf-8 -*-
"""升级版管理窗口（tkinter 单文件）。

界面结构（2026-09-29 重做）：
  顶部状态条（全局）—— 实时录音状态（圆点 + 文字）+ 录音保存位置
  左侧导航（160px）—— 我的通话 / 录音状态 / 设置
  右侧内容区
    我的通话：左侧录音列表（搜索 / 刷新 / 打开文件夹）+ 右侧通话详情
              （播放录音 + 文字稿 / 分析报告 / 问问 AI 三个页签）
    录音状态：状态卡片 + 自动录音开关 + 最近录音错误
    设置    ：录音设置 + AI 服务 + 退出程序

运行模型：主线程 = tkinter；托盘在独立线程，回调只往 cmd_queue 投递；
工作线程通过 ui_call(callable) 把界面更新排回主线程执行。

本文件只做界面，不改变录音 / 转写 / 分析 / 托盘等业务行为。
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import analyzer
import api_client
import autostart
import audio_player

VERSION = "2.0.0-dev"
APP_NAME = "本地通话录音工具·升级版"
ICONS_DIR = Path(__file__).resolve().parent / "assets" / "icons"

# ---------- 视觉规范 ----------
FONT = "Microsoft YaHei UI"
BG = "#F5F6F8"          # 页面背景
CARD = "#FFFFFF"        # 内容区域
TEXT = "#202632"        # 主文字
SUB = "#667085"         # 次要文字
BORDER = "#E3E7ED"      # 边框
PRIMARY = "#356AE6"     # 主按钮
PRIMARY_SOFT = "#EAF1FE"
DANGER = "#C83E3E"      # 错误提示
SOFT = "#F7F9FC"        # 提示条底色
OK_GREEN = "#2F855A"

DOT_BLUE = "#4A7DB8"
DOT_RED = "#D64545"
DOT_ORANGE = "#E08A2E"
DOT_GRAY = "#9AA4B2"
DOT_ERR = "#C83E3E"

F_H1 = (FONT, 20, "bold")
F_H2 = (FONT, 15, "bold")
F_H3 = (FONT, 12, "bold")
F_BODY = (FONT, 11)
F_SMALL = (FONT, 10)
F_BTN = (FONT, 11)
F_TAB = (FONT, 11)
F_NAV = (FONT, 11)
F_NAV_ON = (FONT, 11, "bold")

# 状态 → (圆点颜色, 文字)。文案来自后端真实状态，不推断。
STATUS_UI = {
    "waiting": (DOT_BLUE, "正在等待微信通话"),
    "recording": (DOT_RED, "正在录音"),
    "saving": (DOT_ORANGE, "正在保存录音"),
    "error": (DOT_ERR, "录音失败"),
    "paused": (DOT_GRAY, "自动录音未开启"),
}

MODEL_LABELS = {
    "sense_voice": "SenseVoice 中文轻量（默认）",
    "funasr_nano": "Fun-ASR-Nano 新版",
}

ASPECT_DESC = {
    "内容总结": "这通电话主要聊了什么",
    "情感变化": "结合文字和语气线索，看看交流氛围",
    "行动要点": "谁需要做什么、什么时候做",
}


def parse_recording(path: Path) -> dict:
    """从文件名解析时间与联系人：2026-09-27_11-38-24_289969_球王的故事_通话.mp3"""
    info = {"path": path, "time": "", "contact": "", "size_mb": 0.0}
    try:
        info["size_mb"] = path.stat().st_size / 1024 / 1024
    except OSError:
        pass
    stem = path.stem
    if stem.endswith("_通话"):
        stem = stem[:-3]
    parts = stem.split("_")
    if len(parts) >= 4:
        info["time"] = f"{parts[0]} {parts[1]}"
        info["contact"] = parts[3]
    else:
        info["time"] = stem
        info["contact"] = "?"
    return info


# ---------- 通用控件 ----------
class ScrollFrame(tk.Frame):
    """带垂直滚动条的容器；内容放进 .inner。"""

    def __init__(self, master, bg=BG, **kw):
        super().__init__(master, bg=bg, **kw)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)

    def _on_inner(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _wheel(self, event):
        if self.canvas.yview() == (0.0, 1.0):
            return None
        self.canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def wheel_bind_all(self):
        """把滚轮事件挂到自身与全部子控件上（内容重建后需再调用一次）。"""
        self._bind_tree(self)

    def scroll_to(self, widget):
        """滚动到某个子控件顶部（目录跳转用）。"""
        try:
            self.canvas.update_idletasks()
            top = widget.winfo_rooty() - self.inner.winfo_rooty()
            span = max(1, self.inner.winfo_height() - self.canvas.winfo_height())
            frac = max(0.0, min(1.0, top / span))
            self.canvas.yview_moveto(frac)
        except (tk.TclError, ZeroDivisionError):
            pass

    def _bind_tree(self, w):
        try:
            w.bind("<MouseWheel>", self._wheel, add="+")
        except tk.TclError:
            pass
        for c in w.winfo_children():
            self._bind_tree(c)


class Collapsible(tk.Frame):
    """可折叠区块：点标题展开/收起。"""

    def __init__(self, master, title, bg=CARD):
        super().__init__(master, bg=bg)
        self._bg = bg
        self._title = title
        self._open = False
        self.head = tk.Label(self, text=f"▸ {title}", bg=bg, fg=PRIMARY,
                             font=F_SMALL, anchor="w", cursor="hand2")
        self.head.pack(fill="x")
        self.head.bind("<Button-1>", self.toggle)
        self.body = tk.Frame(self, bg=bg)

    def toggle(self, _event=None):
        self._open = not self._open
        self.head.configure(text=("▾ " if self._open else "▸ ") + self._title)
        if self._open:
            self.body.pack(fill="x", pady=(8, 0))
        else:
            self.body.pack_forget()


# ---------- 主窗口 ----------
class ManagerWindow:
    def __init__(self, recorder, start_minimized: bool = False):
        self.app = recorder
        self.cmd_queue: queue.Queue = queue.Queue()
        self.ui_queue: queue.Queue = queue.Queue()   # 工作线程 -> 主线程的调用
        self.root: tk.Tk | None = None
        self._start_minimized = start_minimized
        self._recordings: list[Path] = []
        self._picked: Path | None = None     # 全局唯一的"当前录音"
        self._row_map: dict = {}
        self._chat_history: list[dict] = []
        self._chat_target: Path | None = None
        self._busy = False
        self._task_mp3: Path | None = None
        self._task_kind: str | None = None
        self._prog_text = ""
        self.btn_gen_tr = None
        self.btn_analyze = None
        self.btn_chat_send = None
        self._pl = audio_player.AudioPlayer()   # 内嵌播放器（MCI）
        self._pl_after: str | None = None

    # ----- 线程安全 -----
    def request(self, cmd: str):
        self.cmd_queue.put(cmd)

    def ui_call(self, fn):
        self.ui_queue.put(fn)

    # ----- 主线程 -----
    def run(self):
        root = tk.Tk()
        self.root = root
        root.title(f"{APP_NAME} v{VERSION}")
        root.geometry("1120x760")
        root.minsize(900, 640)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_window_icon(root)

        self._init_style()
        self._build_topbar(root)

        body = tk.Frame(root, bg=BG)
        body.pack(side="top", fill="both", expand=True)
        self._build_sidebar(body)

        content = tk.Frame(body, bg=BG)
        content.pack(side="left", fill="both", expand=True)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        self.pages = {}
        for key in ("mine", "status", "settings"):
            f = tk.Frame(content, bg=BG)
            f.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = f

        self._build_page_mine(self.pages["mine"])
        self._build_page_status(self.pages["status"])
        self._build_page_settings(self.pages["settings"])
        self._nav("mine")

        self._refresh()
        self._refresh_recordings()
        self._render_detail()

        # 关键：启动 UI 回调泵（工作线程 -> 主线程的队列在这里被处理）。
        # 之前版本漏了这一行，导致任务进度/完成/AI 回答永远停在队列里不显示。
        root.after(200, self._poll)

        if self._start_minimized:
            root.withdraw()
        else:
            self.show()
        root.mainloop()

    def show(self):
        if not self.root:
            return
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def _auto_wrap(self, label, pad=44):
        """让长文字标签跟随窗口宽度自动换行（放大缩小都不截断）。

        绑定在标签自身的 <Configure> 上：fill=\"x\" 的标签宽度会跟随父容器，
        宽度变化时把 wraplength 调成 当前宽度-留白。带差值保护避免死循环。
        """
        def on_cfg(_e):
            try:
                w = label.winfo_width()
                if w < 80:
                    return
                new = max(240, int(w) - pad)
                cur = int(float(label.cget("wraplength") or 0))
                if abs(new - cur) > 4:
                    label.configure(wraplength=new)
            except tk.TclError:
                pass
        label.bind("<Configure>", on_cfg)

    def _on_close(self):
        # 关闭窗口 = 缩回托盘，录音继续，不弹提示
        self.root.withdraw()

    # ================= 样式 =================
    def _init_style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", font=F_BODY, background=BG, foreground=TEXT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Card.TLabel", background=CARD, foreground=TEXT)
        st.configure("TSeparator", background=BORDER)

        st.configure("Primary.TButton", background=PRIMARY, foreground="#FFFFFF",
                     font=F_BTN, borderwidth=0, focusthickness=0,
                     padding=(16, 8), relief="flat", anchor="center")
        st.map("Primary.TButton",
               background=[("disabled", "#AFC3EE"), ("pressed", "#2952B4"),
                           ("active", "#2F5FD8")],
               foreground=[("disabled", "#F2F5FF")])
        st.configure("Secondary.TButton", background=CARD, foreground=TEXT,
                     font=F_BTN, borderwidth=1, bordercolor=BORDER,
                     focusthickness=0, padding=(14, 8), relief="solid")
        st.map("Secondary.TButton",
               background=[("pressed", "#EDF1F7"), ("active", "#F2F5FA")],
               bordercolor=[("active", PRIMARY)])
        st.configure("Danger.TButton", background=CARD, foreground=DANGER,
                     font=F_BTN, borderwidth=1, bordercolor="#E7C5C5",
                     focusthickness=0, padding=(14, 8), relief="solid")
        st.map("Danger.TButton",
               background=[("pressed", "#FBECEC"), ("active", "#FDF3F3")])
        st.configure("Ghost.TButton", background=CARD, foreground=PRIMARY,
                     font=F_SMALL, borderwidth=0, focusthickness=0,
                     padding=(8, 5), relief="flat")
        st.map("Ghost.TButton", background=[("active", "#EEF3FC")],
               foreground=[("disabled", SUB)])
        st.configure("Nav.TButton", background=CARD, foreground=SUB, font=F_NAV,
                     borderwidth=0, focusthickness=0, anchor="w",
                     padding=(14, 11), relief="flat")
        st.map("Nav.TButton", background=[("active", "#F0F4FB")],
               foreground=[("active", TEXT)])
        st.configure("NavOn.TButton", background=PRIMARY_SOFT, foreground=PRIMARY,
                     font=F_NAV_ON, borderwidth=0, focusthickness=0, anchor="w",
                     padding=(14, 11), relief="flat")
        st.map("NavOn.TButton", background=[("active", "#E1EBFD")],
               foreground=[("active", PRIMARY)])

        st.configure("TEntry", fieldbackground=CARD, foreground=TEXT,
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                     insertcolor=TEXT, padding=6)
        st.map("TEntry", bordercolor=[("focus", PRIMARY)])
        st.configure("TCombobox", fieldbackground=CARD, background=CARD,
                     foreground=TEXT, arrowcolor=SUB, bordercolor=BORDER, padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", CARD)],
               foreground=[("readonly", TEXT)], bordercolor=[("focus", PRIMARY)])

        st.configure("TNotebook", background=CARD, borderwidth=0,
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                     tabmargins=(10, 8, 10, 0))
        st.configure("TNotebook.Tab", background=CARD, foreground=SUB,
                     padding=(18, 9), font=F_TAB, borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", PRIMARY_SOFT)],
               foreground=[("selected", PRIMARY)])

        st.configure("TProgressbar", background=PRIMARY, troughcolor="#E7ECF3",
                     bordercolor="#E7ECF3", lightcolor=PRIMARY, darkcolor=PRIMARY,
                     thickness=8)
        st.configure("Vertical.TScrollbar", background="#D6DCE6", troughcolor=BG,
                     bordercolor=BG, arrowcolor=SUB, relief="flat")

    # ================= 顶部状态条 =================
    def _build_topbar(self, root):
        bar = tk.Frame(root, bg=CARD, height=58)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        tk.Frame(root, bg=BORDER, height=1).pack(side="top", fill="x")

        left = tk.Frame(bar, bg=CARD)
        left.pack(side="left", fill="y", padx=(22, 0))
        self.top_dot = tk.Canvas(left, width=16, height=16, bg=CARD,
                                 highlightthickness=0)
        self.top_dot.pack(side="left", pady=21)
        self.top_dot_id = self.top_dot.create_oval(2, 2, 15, 15, fill=DOT_GRAY,
                                                   outline="")
        self.top_status = tk.Label(left, text="正在读取状态…", bg=CARD, fg=TEXT,
                                   font=F_H3)
        self.top_status.pack(side="left", padx=(10, 0))
        self.top_err = tk.Label(left, text="", bg=CARD, fg=DANGER, font=F_SMALL)
        self.top_err.pack(side="left", padx=(10, 0))

        right = tk.Frame(bar, bg=CARD)
        right.pack(side="right", fill="y", padx=(0, 18))
        ttk.Button(right, text="打开", style="Ghost.TButton",
                   command=self._on_open_dir).pack(side="right")
        self.top_dir = tk.Label(right, text="", bg=CARD, fg=SUB, font=F_SMALL)
        self.top_dir.pack(side="right", padx=(0, 4))

    # ================= 图标 =================
    def _apply_window_icon(self, root):
        """窗口/任务栏图标：优先 assets/icons/app.ico；失败静默回退默认。"""
        try:
            ico = ICONS_DIR / "app.ico"
            if ico.exists():
                root.iconbitmap(str(ico))   # 保留引用由 Tk 持有
        except Exception:
            pass

    def _nav_icon(self, key: str):
        """按 DPI 选导航图标（100%=24 / 125%=30 / 150%=36 / 200%=48）。"""
        try:
            scale = self.root.winfo_fpixels("1i") / 96.0
        except Exception:
            scale = 1.0
        px = min((24, 30, 36, 48), key=lambda s: abs(s - 24 * scale))
        try:
            p = ICONS_DIR / f"nav_{key}_{px}.png"
            if p.exists():
                img = tk.PhotoImage(file=str(p))
                self._nav_imgs[key] = img      # 保留引用，防止被垃圾回收
                return img
        except Exception:
            pass
        return None

    # ================= 左侧导航 =================
    def _build_sidebar(self, body):
        side = tk.Frame(body, bg=CARD, width=160)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        tk.Frame(side, bg=CARD, height=16).pack(fill="x")
        self.nav_btns = {}
        self._nav_imgs: dict = {}
        for key, label in (("mine", "我的通话"), ("status", "录音状态"),
                           ("settings", "设置")):
            img = self._nav_icon(key)
            kw = {"image": img, "compound": "left"} if img else {}
            b = ttk.Button(side, text=label, style="Nav.TButton",
                           command=lambda k=key: self._nav(k), **kw)
            b.pack(fill="x", padx=10, pady=2)
            self.nav_btns[key] = b

        tk.Label(side, text=f"版本 {VERSION}", bg=CARD, fg=SUB, font=F_SMALL,
                 anchor="w").pack(side="bottom", fill="x", padx=18, pady=(0, 14))
        tk.Label(side, text="录音与本地转写\n不联网", bg=CARD, fg=SUB, font=F_SMALL,
                 anchor="w", justify="left").pack(side="bottom", fill="x",
                                                  padx=18, pady=(0, 6))

    def _nav(self, key):
        self.pages[key].tkraise()
        for k, b in self.nav_btns.items():
            b.configure(style="NavOn.TButton" if k == key else "Nav.TButton")

    # ================= 页 1：我的通话 =================
    def _build_page_mine(self, parent):
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="both", expand=True, padx=24, pady=20)

        # ---- 左：录音列表 ----
        left = tk.Frame(wrap, bg=CARD, width=360, highlightbackground=BORDER,
                        highlightthickness=1)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        tk.Label(left, text="我的通话", bg=CARD, fg=TEXT, font=F_H2,
                 anchor="w").pack(fill="x", padx=16, pady=(16, 10))
        self.search_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.search_var).pack(fill="x", padx=16)
        tk.Label(left, text="搜索联系人 / 日期", bg=CARD, fg=SUB, font=F_SMALL,
                 anchor="w").pack(fill="x", padx=16, pady=(4, 0))
        self.search_var.trace_add("write", lambda *_: self._render_rows())

        brow = tk.Frame(left, bg=CARD)
        brow.pack(fill="x", padx=16, pady=(10, 8))
        ttk.Button(brow, text="刷新", style="Secondary.TButton",
                   command=self._refresh_recordings).pack(side="left")
        ttk.Button(brow, text="打开文件夹", style="Secondary.TButton",
                   command=self._on_open_dir).pack(side="left", padx=8)

        self.list_sf = ScrollFrame(left, bg=CARD)
        self.list_sf.pack(fill="both", expand=True, padx=(8, 2), pady=(0, 12))

        # ---- 右：通话详情 ----
        right = tk.Frame(wrap, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))
        self.detail_empty = tk.Frame(right, bg=BG)
        self.detail_main = tk.Frame(right, bg=BG)
        self._build_detail_main()
        self._build_detail_empty()

    def _build_detail_main(self):
        parent = self.detail_main
        head = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        head.pack(fill="x")
        hb = tk.Frame(head, bg=CARD)
        hb.pack(fill="x", padx=20, pady=16)
        line1 = tk.Frame(hb, bg=CARD)
        line1.pack(fill="x")
        self.detail_title = tk.Label(line1, text="", bg=CARD, fg=TEXT,
                                     font=F_H1, anchor="w")
        self.detail_title.pack(side="left")
        ttk.Button(line1, text="用系统播放器打开", style="Ghost.TButton",
                   command=self._on_play_external).pack(side="right")
        self.btn_play = ttk.Button(line1, text="播放录音", style="Primary.TButton",
                                   command=self._on_play)
        self.btn_play.pack(side="right", padx=(0, 8))
        self.detail_sub = tk.Label(hb, text="", bg=CARD, fg=SUB, font=F_BODY,
                                   anchor="w")
        self.detail_sub.pack(fill="x", pady=(6, 0))

        # ---- 内嵌播放条（点「播放录音」才出现；播放/暂停统一用顶部蓝按钮）----
        self.player_row = tk.Frame(hb, bg=CARD)
        ttk.Button(self.player_row, text="停止", width=6,
                   command=self._player_stop).pack(side="left", padx=(0, 10))
        self.pl_bar = ttk.Progressbar(self.player_row, mode="determinate",
                                      maximum=1000, value=0)
        self.pl_bar.pack(side="left", fill="x", expand=True)
        self.pl_bar.bind("<Button-1>", self._player_seek_click)
        self.pl_time = tk.Label(self.player_row, text="00:00 / 00:00",
                                bg=CARD, fg=SUB, font=F_SMALL, width=14)
        self.pl_time.pack(side="left", padx=(10, 0))
        # 默认隐藏

        self.dtabs = ttk.Notebook(parent)
        self.dtabs.pack(fill="both", expand=True, pady=(12, 0))
        self.tab_tr = tk.Frame(self.dtabs, bg=CARD)
        self.tab_rep = tk.Frame(self.dtabs, bg=CARD)
        self.tab_chat = tk.Frame(self.dtabs, bg=CARD)
        self.dtabs.add(self.tab_tr, text="  文字稿  ")
        self.dtabs.add(self.tab_rep, text="  分析报告  ")
        self.dtabs.add(self.tab_chat, text="  问问 AI  ")

        self._build_chat_tab()
        self._build_task_banner(parent)

    def _build_task_banner(self, parent):
        self.task_banner = tk.Frame(parent, bg=SOFT, highlightbackground=BORDER,
                                    highlightthickness=1)
        bd = tk.Frame(self.task_banner, bg=SOFT)
        bd.pack(fill="x", padx=16, pady=12)
        head = tk.Frame(bd, bg=SOFT)
        head.pack(fill="x")
        self.tb_title = tk.Label(head, text="", bg=SOFT, fg=PRIMARY, font=F_H3,
                                 anchor="w")
        self.tb_title.pack(side="left")
        self.tb_close = ttk.Button(head, text="知道了", style="Ghost.TButton",
                                   command=self._banner_hide)
        self.tb_detail = tk.Label(bd, text="", bg=SOFT, fg=SUB, font=F_SMALL,
                                  anchor="w", justify="left", wraplength=600)
        self.tb_detail.pack(fill="x", pady=(6, 0))
        self._auto_wrap(self.tb_detail)
        self.tb_bar = ttk.Progressbar(bd, mode="determinate", maximum=100)
        self.tb_bar.pack(fill="x", pady=(8, 0))
        self.tb_tip = tk.Label(bd, text="可以继续查看其他录音，当前任务会继续。",
                               bg=SOFT, fg=SUB, font=F_SMALL, anchor="w")
        self.tb_tip.pack(fill="x", pady=(6, 0))

    def _build_detail_empty(self):
        self.empty_card = tk.Frame(self.detail_empty, bg=CARD,
                                   highlightbackground=BORDER, highlightthickness=1)
        self.empty_card.pack(fill="both", expand=True)

    def _build_chat_tab(self):
        top = tk.Frame(self.tab_chat, bg=CARD)
        top.pack(fill="x", padx=20, pady=(16, 0))
        self.chat_who = tk.Label(top, text="", bg=CARD, fg=TEXT, font=F_H3,
                                 anchor="w")
        self.chat_who.pack(fill="x")
        tk.Label(self.tab_chat,
                 text="发送问题时，会把相关文字内容交给你配置的 AI 服务。",
                 bg=CARD, fg=SUB, font=F_SMALL, anchor="w").pack(
            fill="x", padx=20, pady=(4, 0))
        self.chat_net = tk.Label(self.tab_chat, text="", bg=CARD, fg=OK_GREEN,
                                 font=F_SMALL, anchor="w")
        self.chat_net.pack(fill="x", padx=20, pady=(2, 0))

        # 没有文字稿时的引导
        self.chat_guide = tk.Frame(self.tab_chat, bg=SOFT,
                                   highlightbackground=BORDER, highlightthickness=1)
        gi = tk.Frame(self.chat_guide, bg=SOFT)
        gi.pack(fill="x", padx=14, pady=12)
        tk.Label(gi, text="这通电话还没有文字稿，先生成文字稿我才能答得准。",
                 bg=SOFT, fg=TEXT, font=F_BODY, anchor="w").pack(fill="x")
        ttk.Button(gi, text="去生成文字稿", style="Secondary.TButton",
                   command=lambda: self.dtabs.select(self.tab_tr)).pack(
            anchor="w", pady=(8, 0))

        # 未配置 AI 服务时的引导
        self.chat_needkey = tk.Frame(self.tab_chat, bg=SOFT,
                                     highlightbackground=BORDER, highlightthickness=1)
        ki = tk.Frame(self.chat_needkey, bg=SOFT)
        ki.pack(fill="x", padx=14, pady=12)
        tk.Label(ki, text="还没有配置 AI 服务，问答需要联网调用你配置的模型。",
                 bg=SOFT, fg=TEXT, font=F_BODY, anchor="w").pack(fill="x")
        ttk.Button(ki, text="去设置 AI 服务", style="Secondary.TButton",
                   command=lambda: self._nav("settings")).pack(anchor="w",
                                                              pady=(8, 0))

        chat_wrap = tk.Frame(self.tab_chat, bg=CARD)
        # 注意：先自下而上打包输入区，聊天区最后 pack，避免被挤出可视范围
        tk.Label(self.tab_chat, text="Enter 发送 · Shift+Enter 换行", bg=CARD,
                 fg=SUB, font=F_SMALL, anchor="w").pack(side="bottom", fill="x",
                                                        padx=20, pady=(6, 14))
        inp = tk.Frame(self.tab_chat, bg=CARD)
        inp.pack(side="bottom", fill="x", padx=20)
        self.chat_examples = tk.Frame(self.tab_chat, bg=CARD)
        self.chat_examples.pack(side="bottom", fill="x", padx=20, pady=(0, 8))

        self.txt_chat = tk.Text(chat_wrap, wrap="word", state="disabled",
                                font=F_BODY, bg="#FBFCFE", fg=TEXT, relief="flat",
                                highlightthickness=1, highlightbackground=BORDER,
                                padx=12, pady=10, spacing1=2, spacing3=8)
        csb = ttk.Scrollbar(chat_wrap, orient="vertical", command=self.txt_chat.yview)
        self.txt_chat.configure(yscrollcommand=csb.set)
        self.txt_chat.pack(side="left", fill="both", expand=True)
        csb.pack(side="right", fill="y")
        self.txt_chat.tag_configure("who", foreground=PRIMARY, font=F_H3)
        self.txt_chat.tag_configure("who_ai", foreground=TEXT, font=F_H3)
        chat_wrap.pack(side="top", fill="both", expand=True, padx=20, pady=(10, 0))

        self.txt_input = tk.Text(inp, height=3, width=6, wrap="word", font=F_BODY,
                                 bg=CARD, fg=TEXT, relief="flat",
                                 highlightthickness=1, highlightbackground=BORDER,
                                 padx=10, pady=8)
        self.btn_chat_send = ttk.Button(inp, text="发送", style="Primary.TButton",
                                        command=self._on_chat_send)
        self.btn_chat_send.pack(side="right", padx=(10, 0), anchor="s")
        self.txt_input.pack(side="left", fill="both", expand=True)
        self.txt_input.bind("<Return>", self._on_chat_return)

    # ---------- 录音列表 ----------
    def _refresh_recordings(self):
        if not self.root:
            return
        try:
            self._recordings = sorted(self.app.out_dir.glob("*_通话.mp3"),
                                      reverse=True)
        except OSError:
            self._recordings = []
        if self._picked is not None and (
                not self._picked.exists() or self._picked not in self._recordings):
            self._picked = None
        self._render_rows()
        self._render_detail()

    def _render_rows(self):
        if not hasattr(self, "list_sf"):
            return
        inner = self.list_sf.inner
        for w in inner.winfo_children():
            w.destroy()
        self._row_map = {}
        q = (self.search_var.get() or "").strip().lower() if hasattr(
            self, "search_var") else ""
        shown = 0
        for p in self._recordings:
            info = parse_recording(p)
            if q and q not in info["contact"].lower() and q not in info["time"].lower():
                continue
            self._make_row(inner, p, info)
            shown += 1
        if not self._recordings:
            tk.Label(inner, text="还没有通话录音", bg=CARD, fg=SUB,
                     font=F_BODY).pack(pady=30)
        elif shown == 0:
            tk.Label(inner, text="没有匹配的录音", bg=CARD, fg=SUB,
                     font=F_BODY).pack(pady=30)
        self.list_sf.wheel_bind_all()
        # 后台补算缺失的通话时长（算完自动刷新列表）
        try:
            missing = [p for p in self._recordings
                       if p.name not in self._load_durations()]
        except Exception:
            missing = []
        if missing:
            threading.Thread(target=self._compute_durations, args=(missing,),
                             name="durations", daemon=True).start()

    def _rec_status(self, p: Path):
        md = p.with_name(p.stem + "_分析.md")
        txt = p.with_name(p.stem + "_文字稿.txt")
        if md.exists():
            return "已有分析报告", OK_GREEN
        if txt.exists():
            return "已有文字稿", PRIMARY
        return "未生成文字稿", SUB

    # ---------- 通话时长（ffmpeg 读文件头，结果缓存到 durations.json）----------
    def _durations_path(self) -> Path:
        return Path(__file__).resolve().parent / "durations.json"

    def _load_durations(self) -> dict:
        if not hasattr(self, "_durations"):
            try:
                self._durations = json.loads(
                    self._durations_path().read_text("utf-8"))
                if not isinstance(self._durations, dict):
                    self._durations = {}
            except (OSError, ValueError):
                self._durations = {}
        return self._durations

    def _save_durations(self):
        try:
            self._durations_path().write_text(
                json.dumps(self._durations, ensure_ascii=False), "utf-8")
        except (OSError, TypeError):
            pass

    @staticmethod
    def _read_duration_sec(p: Path):
        """用 ffmpeg 读文件头解析时长（秒）。失败返回 None。"""
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            r = subprocess.run([exe, "-i", str(p)], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=20)
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
            if m:
                return (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                        + float(m.group(3)))
        except Exception:
            pass
        return None

    @staticmethod
    def _fmt_duration(sec: float) -> str:
        sec = int(round(sec))
        h, r = divmod(sec, 3600)
        m, s = divmod(r, 60)
        if h:
            return f"{h}小时{m:02d}分"
        if m:
            return f"{m}分{s:02d}秒"
        return f"{s}秒"

    def _compute_durations(self, paths: list):
        """后台线程：补算缺失的时长，算完回主线程刷新列表。"""
        durs = self._load_durations()
        changed = False
        for p in paths:
            try:
                sec = self._read_duration_sec(p)
            except Exception:
                sec = None
            durs[p.name] = round(sec) if sec is not None else None
            changed = True
        if changed:
            self._save_durations()
            self.ui_call(self._render_rows)
            self.ui_call(self._update_detail_sub)

    def _make_row(self, parent, p: Path, info: dict):
        st_text, st_color = self._rec_status(p)
        row = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                       highlightthickness=1, cursor="hand2")
        row.pack(fill="x", padx=2, pady=3)
        top = tk.Frame(row, bg=CARD)
        top.pack(fill="x", padx=12, pady=(9, 0))
        name = tk.Label(top, text=info["contact"], bg=CARD, fg=TEXT, font=F_H3,
                        anchor="w")
        name.pack(side="left")
        badge = tk.Label(top, text=st_text, bg=CARD, fg=st_color, font=F_SMALL)
        badge.pack(side="right")
        bottom = tk.Frame(row, bg=CARD)
        bottom.pack(fill="x", padx=12, pady=(2, 9))
        sec = self._load_durations().get(p.name)
        dur_txt = (f" · 时长 {self._fmt_duration(sec)}"
                   if isinstance(sec, (int, float)) else "")
        sub = tk.Label(bottom, text=f"{info['time']}{dur_txt}"
                                    f" · {info['size_mb']:.1f} MB",
                       bg=CARD, fg=SUB, font=F_SMALL, anchor="w")
        sub.pack(side="left")

        widgets = [row, top, name, badge, bottom, sub]
        for w in widgets:
            w.bind("<Button-1>", lambda _e, pp=p: self._select(pp))
            w.bind("<Double-1>", lambda _e, pp=p: self._open_and_play(pp))
        self._row_map[p] = (row, widgets)
        self._paint_row(p)

    def _paint_row(self, p):
        entry = self._row_map.get(p)
        if not entry:
            return
        row, widgets = entry
        sel = (self._picked == p)
        bgc = PRIMARY_SOFT if sel else CARD
        for w in widgets:
            try:
                w.configure(bg=bgc)
            except tk.TclError:
                pass
        try:
            row.configure(highlightbackground=PRIMARY if sel else BORDER)
        except tk.TclError:
            pass

    def _repaint_rows(self):
        for p in list(self._row_map.keys()):
            self._paint_row(p)

    def _select(self, p: Path):
        self._picked = Path(p)
        self._repaint_rows()
        self._render_detail()

    def _open_and_play(self, p: Path):
        """列表双击：选中这条并直接在软件内播放。"""
        self._select(p)
        self._on_play()

    def _on_play(self):
        if not self._picked:
            return
        p = Path(self._picked)
        if not p.exists():
            messagebox.showinfo(APP_NAME, "录音文件不存在（可能已被清理）。",
                                parent=self.root)
            return
        # 换了录音 → 重新打开；同一条 → 切换播放/暂停
        if self._pl.path != p:
            self._player_load(p)
        if self._pl.path == p:
            self._player_toggle()

    def _on_play_external(self):
        """用电脑默认播放器打开录音文件（用户明确要求外部打开时）。"""
        if not self._picked:
            return
        p = Path(self._picked)
        if not p.exists():
            messagebox.showinfo(APP_NAME, "录音文件不存在（可能已被清理）。",
                                parent=self.root)
            return
        self._player_pause()          # 内嵌播放让位给外部播放器
        try:
            os.startfile(str(p))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法打开：{exc}", parent=self.root)

    # ---------- 内嵌播放器 ----------
    def _player_load(self, p: Path):
        err = self._pl.open(p)
        if err:
            messagebox.showerror(APP_NAME, err, parent=self.root)
            return
        self.pl_bar.configure(maximum=max(1, self._pl.length_ms), value=0)
        self._pl_update_time(0)
        self.player_row.pack_forget()
        self.player_row.pack(fill="x", pady=(12, 0))   # 显示播放条
        err = self._pl.play()
        if err:
            messagebox.showerror(APP_NAME, err, parent=self.root)
            return
        self.btn_play.configure(text="暂停播放")
        self._player_tick()

    def _player_toggle(self):
        if not self._pl._opened:
            return
        if self._pl.is_playing():
            self._pl.pause()
            self.btn_play.configure(text="继续播放")
        else:
            pos = self._pl.position_ms()
            if self._pl.length_ms and pos >= self._pl.length_ms:
                pos = 0                                  # 播完了，从头来
            self._pl.play(pos)
            self.btn_play.configure(text="暂停播放")

    def _player_pause(self):
        if self._pl._opened and self._pl.is_playing():
            self._pl.pause()
            self.btn_play.configure(text="继续播放")

    def _player_stop(self):
        self._pl.close()
        self.btn_play.configure(text="播放录音")
        self.player_row.pack_forget()

    def _player_seek_click(self, event):
        if not self._pl._opened or not self._pl.length_ms:
            return
        frac = min(1.0, max(0.0, event.x / max(1, self.pl_bar.winfo_width())))
        target = int(frac * self._pl.length_ms)
        was_playing = self._pl.is_playing()
        self._pl.seek(target, and_play=was_playing)
        self.pl_bar.configure(value=target)
        self._pl_update_time(target)

    def _player_tick(self):
        """每 300ms 刷新进度（仅当文件仍打开）。"""
        if not self._pl._opened:
            return
        pos = self._pl.position_ms()
        if self._pl.length_ms:
            self.pl_bar.configure(value=min(pos, self._pl.length_ms))
        self._pl_update_time(pos)
        if (self._pl.length_ms and pos >= self._pl.length_ms
                and not self._pl.is_playing()):
            # 播完自动收尾
            self._pl.close()
            self.btn_play.configure(text="播放录音")
            self.player_row.pack_forget()
            return
        self._pl_after = self.root.after(300, self._player_tick)

    def _pl_update_time(self, pos_ms: int):
        def fmt(ms):
            s = int(ms // 1000)
            m, sec = divmod(s, 60)
            h, m2 = divmod(m, 60)
            return f"{h}:{m2:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
        total = self._pl.length_ms
        self.pl_time.configure(text=f"{fmt(pos_ms)} / {fmt(total)}")

    # ---------- 详情区 ----------
    def _display_name(self, p: Path | None) -> str:
        if not p:
            return ""
        info = parse_recording(p)
        return f"{info['contact']} · {info['time']}"

    def _update_detail_sub(self):
        """刷新详情页副标题（时间 · 时长 · 大小）。时长未知时先不显示。"""
        if not getattr(self, "detail_sub", None):
            return
        if self._picked is None or not Path(self._picked).exists():
            return
        p = Path(self._picked)
        info = parse_recording(p)
        sec = self._load_durations().get(p.name)
        dur = (f"   ·   时长 {self._fmt_duration(sec)}"
               if isinstance(sec, (int, float)) else "")
        try:
            self.detail_sub.configure(
                text=f"{info['time']}{dur}   ·   {info['size_mb']:.1f} MB")
        except tk.TclError:
            pass

    def _render_detail(self):
        p = self._picked
        if p is None or not Path(p).exists():
            self._show_empty()
            return
        self.detail_empty.pack_forget()
        self.detail_main.pack(fill="both", expand=True)
        info = parse_recording(p)
        # 切到别的录音 → 停掉内嵌播放，避免声音串台
        if self._pl.path is not None and self._pl.path != Path(p):
            self._player_stop()
        self.detail_title.configure(text=info["contact"])
        self._update_detail_sub()
        self._render_transcript_tab(p)
        self._render_report_tab(p)
        self._render_chat_tab(p)
        self._set_busy(self._busy)

    def _show_empty(self):
        self.detail_main.pack_forget()
        self.detail_empty.pack(fill="both", expand=True)
        for w in self.empty_card.winfo_children():
            w.destroy()
        if not self._recordings:
            tk.Label(self.empty_card, text="还没有通话录音", bg=CARD, fg=TEXT,
                     font=F_H2).pack(pady=(90, 8))
            tk.Label(self.empty_card,
                     text="开启自动录音后，微信通话接通时会自动开始录制。",
                     bg=CARD, fg=SUB, font=F_BODY).pack()
            ttk.Button(self.empty_card, text="查看录音状态",
                       style="Primary.TButton",
                       command=lambda: self._nav("status")).pack(pady=18)
        else:
            tk.Label(self.empty_card, text="选一条通话，查看文字稿或整理内容。",
                     bg=CARD, fg=SUB, font=F_BODY).pack(pady=110)

    # ---------- 页签 1：文字稿 ----------
    def _render_transcript_tab(self, p: Path):
        for w in self.tab_tr.winfo_children():
            w.destroy()
        self.btn_gen_tr = None
        self.tr_status = tk.Label(self.tab_tr, text="", bg=CARD, fg=SUB,
                                  font=F_SMALL, anchor="w", justify="left",
                                  wraplength=620)
        self.tr_status.pack(fill="x", padx=20, pady=(14, 0))

        txt_path = p.with_name(p.stem + "_文字稿.txt")
        if txt_path.exists():
            row = tk.Frame(self.tab_tr, bg=CARD)
            row.pack(fill="x", padx=20, pady=(10, 6))
            ttk.Button(row, text="复制全文", style="Secondary.TButton",
                       command=lambda: self._copy_file(txt_path, "文字稿")).pack(
                side="left")
            ttk.Button(row, text="打开文字稿文件", style="Secondary.TButton",
                       command=lambda: self._open_file(txt_path)).pack(
                side="left", padx=8)
            tk.Label(row, text="本地生成 · 可选中复制", bg=CARD, fg=SUB,
                     font=F_SMALL).pack(side="right")
            wrap = tk.Frame(self.tab_tr, bg=CARD)
            wrap.pack(fill="both", expand=True, padx=20, pady=(0, 18))
            text = tk.Text(wrap, wrap="word", font=F_BODY, bg="#FBFCFE", fg=TEXT,
                           relief="flat", highlightthickness=1,
                           highlightbackground=BORDER, padx=12, pady=10,
                           spacing1=1, spacing3=4)
            sb = ttk.Scrollbar(wrap, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=sb.set)
            try:
                text.insert("1.0", txt_path.read_text("utf-8"))
            except OSError as exc:
                text.insert("1.0", f"（读取失败：{exc}）")
            text.configure(state="disabled")
            text.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            self.tr_status.configure(text=f"已有文字稿：{txt_path.name}")
            return

        # 尚无文字稿
        card = tk.Frame(self.tab_tr, bg=CARD)
        card.pack(fill="both", expand=True, padx=20, pady=(10, 0))
        tk.Label(card, text="把这次通话转成文字，方便回看和搜索。", bg=CARD,
                 fg=TEXT, font=F_H3, anchor="w").pack(fill="x", pady=(6, 4))
        tk.Label(card, text="本地处理 · 免费 · 不上传录音", bg=CARD, fg=OK_GREEN,
                 font=F_SMALL, anchor="w").pack(fill="x")
        self.btn_gen_tr = ttk.Button(card, text="生成文字稿",
                                     style="Primary.TButton",
                                     command=self._on_gen_transcript)
        self.btn_gen_tr.pack(anchor="w", pady=14)

        col = Collapsible(card, "转写选项")
        col.pack(fill="x", pady=(4, 0))

        r1 = tk.Frame(col.body, bg=CARD)
        r1.pack(fill="x", pady=4)
        tk.Label(r1, text="转写方式", bg=CARD, fg=TEXT, font=F_BODY,
                 anchor="w").pack(side="left")
        self.var_engine = tk.StringVar(value=self._engine_label())
        cmb = ttk.Combobox(r1, textvariable=self.var_engine, state="readonly",
                           width=34,
                           values=["本地离线 · 免费不联网",
                                   "云端 API · 需要联网，可能产生费用"])
        cmb.pack(side="left", padx=(10, 0))
        cmb.bind("<<ComboboxSelected>>", lambda _e: self._on_engine_changed())

        r2 = tk.Frame(col.body, bg=CARD)
        r2.pack(fill="x", pady=4)
        tk.Label(r2, text="本地模型", bg=CARD, fg=TEXT, font=F_BODY,
                 anchor="w").pack(side="left")
        self.var_model = tk.StringVar(value=self._model_label())
        cmb2 = ttk.Combobox(r2, textvariable=self.var_model, state="readonly",
                            width=34, values=list(MODEL_LABELS.values()))
        cmb2.pack(side="left", padx=(10, 0))
        cmb2.bind("<<ComboboxSelected>>", lambda _e: self._on_model_changed())
        tk.Label(col.body,
                 text="本地引擎完全离线、零费用；云端引擎需要先在「设置 → AI 服务」"
                      "里填好密钥。", bg=CARD, fg=SUB, font=F_SMALL, anchor="w",
                 justify="left", wraplength=560).pack(fill="x", pady=(6, 0))
        for w in col.body.winfo_children():
            if isinstance(w, tk.Label):
                self._auto_wrap(w)

    # ---------- 页签 2：分析报告 ----------
    def _render_report_tab(self, p: Path):
        for w in self.tab_rep.winfo_children():
            w.destroy()
        self.btn_analyze = None
        self.rep_sf = ScrollFrame(self.tab_rep, bg=CARD)
        self.rep_sf.pack(fill="both", expand=True)
        body = self.rep_sf.inner

        self.rep_status = tk.Label(body, text="", bg=CARD, fg=SUB, font=F_SMALL,
                                   anchor="w", justify="left", wraplength=620)
        self.rep_status.pack(fill="x", padx=20, pady=(14, 0))
        self._auto_wrap(self.rep_status)

        md_path = p.with_name(p.stem + "_分析.md")
        raw = ""
        has_report = False
        stale = False
        if md_path.exists():
            try:
                raw = md_path.read_text("utf-8")
            except OSError as exc:
                self.rep_status.configure(text=f"读取报告失败：{exc}", fg=DANGER)
            has_report = self._is_real_report(raw)
            stale = md_path.exists() and not has_report

        # 勾选表单永远显示在最前面；已有报告收进下方折叠区，点开才看
        self._render_report_form(body, p, has_report=has_report)

        if has_report:
            self.rep_status.configure(
                text=f"已有分析报告：{md_path.name}（在下方「查看现有报告」里，"
                     f"重新分析会覆盖它）")
            col = Collapsible(body, "查看现有报告")
            col.pack(fill="x", padx=20, pady=(10, 16))
            self._render_report_content(col.body, p, md_path, raw)
        elif stale:
            self.rep_status.configure(
                text="上次没有勾选分析内容，没有生成 AI 分析报告。"
                     "勾选后点「开始分析」即可生成。")
        self.rep_sf.wheel_bind_all()

    @staticmethod
    def _is_real_report(raw: str) -> bool:
        """区分真实 AI 报告与旧版「本地免费产物」占位报告。"""
        if not raw or not raw.strip():
            return False
        return "本次未选择 AI 分析维度" not in raw

    def _show_report_form(self, p: Path):
        """已有报告时也能重新选内容、重新整理（覆盖旧报告）。"""
        for w in self.tab_rep.winfo_children():
            w.destroy()
        self.rep_sf = ScrollFrame(self.tab_rep, bg=CARD)
        self.rep_sf.pack(fill="both", expand=True)
        body = self.rep_sf.inner
        self.rep_status = tk.Label(body, text="", bg=CARD, fg=SUB, font=F_SMALL,
                                   anchor="w", justify="left", wraplength=620)
        self.rep_status.pack(fill="x", padx=20, pady=(14, 0))
        self._auto_wrap(self.rep_status)
        self.rep_status.configure(text="重新分析会重跑所选内容，生成的报告会覆盖现在的报告。")
        self._render_report_form(body, p)
        self.rep_sf.wheel_bind_all()

    def _render_report_content(self, container, p: Path, md_path: Path,
                               raw: str):
        """把已有报告渲染进指定容器（分析报告页的折叠区）。"""
        row = tk.Frame(container, bg=CARD)
        row.pack(fill="x", pady=(4, 6))
        ttk.Button(row, text="复制报告", style="Secondary.TButton",
                   command=lambda: self._copy_file(md_path, "报告")).pack(
            side="left")
        ttk.Button(row, text="打开报告文件", style="Secondary.TButton",
                   command=lambda: self._open_file(md_path)).pack(
            side="left", padx=8)

        sections = self._split_sections(raw)
        if sections:
            # 目录导航条：点一下直接跳到对应小节
            nav = tk.Frame(container, bg=CARD)
            nav.pack(fill="x", pady=(6, 0))
            tk.Label(nav, text="目录：", bg=CARD, fg=SUB,
                     font=F_SMALL).pack(side="left")
            cards: list[tuple[str, tk.Frame]] = []
            for title, content in sections:
                card = tk.Frame(container, bg=CARD,
                                highlightbackground=BORDER,
                                highlightthickness=1)
                card.pack(fill="x", pady=(0, 12))
                cards.append((title, card))
            for title, card in cards:
                chip = tk.Label(nav, text=title, bg=PRIMARY_SOFT, fg=PRIMARY,
                                font=F_SMALL, padx=10, pady=4, cursor="hand2")
                chip.pack(side="left", padx=(0, 8))
                chip.bind("<Button-1>",
                          lambda _e, w=card: self.rep_sf.scroll_to(w))
            for title, card in cards:
                inner = tk.Frame(card, bg=CARD)
                inner.pack(fill="x", padx=16, pady=14)
                tk.Label(inner, text=title, bg=CARD, fg=TEXT, font=F_H3,
                         anchor="w").pack(fill="x")
                if title == "情感变化":
                    tk.Label(inner, text="供理解交流氛围参考。", bg=CARD, fg=SUB,
                             font=F_SMALL, anchor="w").pack(fill="x",
                                                            pady=(2, 0))
                shown = self._plain_text(content).strip() or "（本节没有内容）"
                lbl = tk.Label(inner, text=shown, bg=CARD,
                               fg=TEXT, font=F_BODY, anchor="w", justify="left",
                               wraplength=560)
                lbl.pack(fill="x", pady=(6, 0))
                self._auto_wrap(lbl)
        else:
            card = tk.Frame(container, bg=CARD,
                            highlightbackground=BORDER, highlightthickness=1)
            card.pack(fill="x", pady=(0, 12))
            shown = self._plain_text(raw).strip() or "（报告内容为空）"
            lbl = tk.Label(card, text=shown, bg=CARD,
                           fg=TEXT, font=F_BODY, anchor="w", justify="left",
                           wraplength=560)
            lbl.pack(fill="x", padx=16, pady=14)
            self._auto_wrap(lbl)

        full = Collapsible(container, "查看完整原文")
        full.pack(fill="x", pady=(0, 12))
        txt = tk.Text(full.body, height=10, wrap="word", font=F_SMALL,
                      bg="#FBFCFE", fg=TEXT, relief="flat", highlightthickness=1,
                      highlightbackground=BORDER, padx=10, pady=8)
        txt.insert("1.0", self._plain_text(raw))
        txt.configure(state="disabled")
        txt.pack(fill="x")

        files = self._related_files(p)
        if files:
            col = Collapsible(container, "相关文件")
            col.pack(fill="x", pady=(0, 16))
            for label, path in files:
                r = tk.Frame(col.body, bg=CARD)
                r.pack(fill="x", pady=3)
                tk.Label(r, text=f"{label}", bg=CARD, fg=TEXT, font=F_SMALL,
                         anchor="w", width=14).pack(side="left")
                tk.Label(r, text=path.name, bg=CARD, fg=SUB, font=F_SMALL,
                         anchor="w").pack(side="left")
                ttk.Button(r, text="打开", style="Ghost.TButton",
                           command=lambda pp=path: self._open_file(pp)).pack(
                    side="right")

    @staticmethod
    def _plain_text(raw: str) -> str:
        """把 AI 输出里的机器符号清成人能直接读的样子（显示层兜底）。"""
        out = []
        for ln in raw.splitlines():
            t = ln.strip()
            while t.startswith("#"):
                t = t[1:].lstrip()
            for deco in ("▶", "▸", "►"):
                t = t.replace(deco, "·")
            t = t.replace("**", "").replace("__", "").replace("`", "")
            if t.startswith("- "):
                t = "· " + t[2:]
            elif t == "-":
                continue
            out.append(t)
        return "\n".join(out)

    @staticmethod
    def _split_sections(raw: str):
        """按 Markdown 二级标题拆节；拆不出就返回空（调用方展示完整原文）。"""
        lines = raw.splitlines()
        out = []
        cur_title = None
        buf: list[str] = []
        for ln in lines:
            if ln.startswith("## ") and not ln.startswith("###"):
                if cur_title is not None:
                    out.append((cur_title, "\n".join(buf)))
                cur_title = ln[3:].strip()
                buf = []
            elif cur_title is not None:
                buf.append(ln)
        if cur_title is not None:
            out.append((cur_title, "\n".join(buf)))
        return [(t, c) for t, c in out if t]

    def _related_files(self, p: Path):
        cands = [("原始文字稿", "_文字稿_原始.txt"),
                 ("校对记录", "_校对记录.md"),
                 ("语气曲线", "_语气曲线.md"),
                 ("情感标签", "_情感标签.txt")]
        out = []
        for label, suffix in cands:
            f = p.with_name(p.stem + suffix)
            if f.exists():
                out.append((label, f))
        return out

    def _render_report_form(self, body, p: Path, has_report: bool = False):
        self._needkey_shown = False
        card = tk.Frame(body, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", padx=20, pady=(10, 0))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=18, pady=16)
        if has_report:
            tk.Label(inner, text="重新分析这通通话", bg=CARD, fg=TEXT,
                     font=F_H3, anchor="w").pack(fill="x")
            tk.Label(inner, text="勾选要整理的内容；已有报告在下方"
                     "「查看现有报告」里，重新分析会覆盖旧报告。",
                     bg=CARD, fg=SUB, font=F_SMALL,
                     anchor="w").pack(fill="x", pady=(4, 10))
        else:
            tk.Label(inner, text="还没有分析报告", bg=CARD, fg=TEXT, font=F_H3,
                     anchor="w").pack(fill="x")
            tk.Label(inner, text="选要整理的内容，我会把通话读一遍再整理成文字。",
                     bg=CARD, fg=SUB, font=F_SMALL, anchor="w").pack(fill="x",
                                                                    pady=(4, 10))
        self.var_aspects = {}
        for key in analyzer.ASPECT_KEYS:
            r = tk.Frame(inner, bg=CARD)
            r.pack(fill="x", pady=3)
            v = tk.BooleanVar(value=True)
            self.var_aspects[key] = v
            tk.Checkbutton(r, text=key, variable=v, bg=CARD, fg=TEXT,
                           font=F_BODY, activebackground=CARD, selectcolor=CARD,
                           anchor="w", command=self._on_aspect_toggle).pack(
                side="left")
            tk.Label(r, text=ASPECT_DESC.get(key, ""), bg=CARD, fg=SUB,
                     font=F_SMALL, anchor="w").pack(side="left", padx=(10, 0))

        rp = tk.Frame(inner, bg=CARD)
        rp.pack(fill="x", pady=(12, 0))
        self.var_polish = tk.BooleanVar(
            value=bool(self.app.cfg.get("transcript_polish", True)))
        tk.Checkbutton(rp, text="智能校对", variable=self.var_polish, bg=CARD,
                       fg=TEXT, font=F_BODY, activebackground=CARD,
                       selectcolor=CARD, anchor="w",
                       command=self._on_aspect_toggle).pack(side="left")
        tk.Label(rp, text="用 AI 修正可能识别错的字词，保留原稿和修改记录。",
                 bg=CARD, fg=SUB, font=F_SMALL, anchor="w").pack(side="left",
                                                                padx=(10, 0))

        self.cloud_warn = tk.Label(
            inner, text="需要联网，会使用你配置的 AI 服务，可能产生费用。",
            bg=CARD, fg=DANGER, font=F_SMALL, anchor="w")
        self.cloud_warn.pack(fill="x", pady=(12, 0))
        self.needkey = tk.Frame(inner, bg=CARD)

        self.btn_analyze = ttk.Button(inner, text="开始分析",
                                      style="Primary.TButton",
                                      command=self._on_analyze)
        self.btn_analyze.pack(anchor="w", pady=(12, 0))
        self.rep_note = tk.Label(inner, text="", bg=CARD, fg=DANGER, font=F_SMALL,
                                 anchor="w", justify="left", wraplength=560)
        self.rep_note.pack(fill="x", pady=(8, 0))
        self._auto_wrap(self.rep_note)
        self._on_aspect_toggle()

    def _on_aspect_toggle(self):
        aspects = [k for k, v in getattr(self, "var_aspects", {}).items() if v.get()]
        polish = bool(getattr(self, "var_polish", tk.BooleanVar(value=False)).get())
        cloud = bool(aspects) or polish
        if hasattr(self, "cloud_warn"):
            if cloud:
                self.cloud_warn.pack(fill="x", pady=(12, 0))
            else:
                self.cloud_warn.pack_forget()

    def _has_api(self) -> bool:
        return bool(str(self.app.cfg.get("api_base_url", "")).strip()
                    and str(self.app.cfg.get("api_key", "")).strip())

    def _on_analyze(self):
        if self._busy:
            return
        p = self._picked
        if not p:
            return
        aspects = [k for k, v in getattr(self, "var_aspects", {}).items() if v.get()]
        polish = bool(getattr(self, "var_polish", tk.BooleanVar(value=False)).get())
        if not aspects and not polish:
            self.rep_note.configure(
                text="请至少选一项整理内容；只想转文字的话，去「文字稿」页生成即可。")
            return
        if (aspects or polish) and not self._has_api():
            self.rep_note.configure(
                text="还没配置 AI 服务，整理与校对需要联网调用你配置的模型。"
                     "本地生成文字稿不受影响。")
            self._show_needkey_button()
            return
        self.rep_note.configure(text="")
        cfg = dict(self.app.cfg)
        cfg["transcript_polish"] = polish
        self._start_task(p, "report", cfg, aspects)

    def _show_needkey_button(self):
        if getattr(self, "_needkey_shown", False):
            return
        self._needkey_shown = True
        for w in self.needkey.winfo_children():
            w.destroy()
        tk.Label(self.needkey, text="", bg=CARD).pack()
        ttk.Button(self.needkey, text="去设置 AI 服务", style="Secondary.TButton",
                   command=lambda: self._nav("settings")).pack(anchor="w",
                                                              pady=(4, 0))
        self.needkey.pack(fill="x")

    # ---------- 页签 3：问问 AI ----------
    def _render_chat_tab(self, p: Path):
        self.chat_who.configure(text=f"正在聊：{self._display_name(p)}")
        txt_path = p.with_name(p.stem + "_文字稿.txt")
        if txt_path.exists():
            self.chat_guide.pack_forget()
        else:
            self.chat_guide.pack(fill="x", padx=20, pady=(10, 0), before=self.chat_wrap_anchor())
        if self._has_api():
            self.chat_needkey.pack_forget()
            self.chat_net.configure(
                text=f"已联网 · 模型：{self.app.cfg.get('chat_model') or '默认'}"
                     f" · 长录音提问约需 10~60 秒，请耐心等一下",
                fg=OK_GREEN)
        else:
            self.chat_needkey.pack(fill="x", padx=20, pady=(10, 0),
                                   before=self.chat_wrap_anchor())
            self.chat_net.configure(text="未联网：还没有配置 AI 服务", fg=DANGER)
        if self._chat_target != p:
            self._chat_target = p
            self._chat_history = []
            self._chat_clear()
            self._chat_append("助手", "选好录音了。可以问我这通电话的内容。")
        self._render_chat_examples()

    def chat_wrap_anchor(self):
        return self.txt_chat.master

    def _render_chat_examples(self):
        for w in self.chat_examples.winfo_children():
            w.destroy()
        if self._chat_history:
            return
        tk.Label(self.chat_examples, text="可以这样问：", bg=CARD, fg=SUB,
                 font=F_SMALL, anchor="w").pack(fill="x")
        row = tk.Frame(self.chat_examples, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        for q in ("帮我列出这通电话里的待办", "我答应了对方哪些事情？",
                  "这次交流有哪些值得注意的地方？"):
            lbl = tk.Label(row, text=q, bg=PRIMARY_SOFT, fg=PRIMARY, font=F_SMALL,
                           padx=10, pady=5, cursor="hand2")
            lbl.pack(side="left", padx=(0, 8))
            lbl.bind("<Button-1>", lambda _e, t=q: self._fill_input(t))

    def _fill_input(self, text: str):
        self.txt_input.delete("1.0", "end")
        self.txt_input.insert("1.0", text)
        self.txt_input.focus_set()

    def _chat_clear(self):
        self.txt_chat.configure(state="normal")
        self.txt_chat.delete("1.0", "end")
        self.txt_chat.configure(state="disabled")

    def _chat_append(self, who: str, text: str, is_user: bool = False):
        tag = "who" if is_user else "who_ai"
        self.txt_chat.configure(state="normal")
        self.txt_chat.insert("end", f"{who}：", tag)
        self.txt_chat.insert("end", f"{text}\n\n")
        self.txt_chat.see("end")
        self.txt_chat.configure(state="disabled")

    def _on_chat_return(self, event):
        if event.state & 0x0001:      # Shift 按下 → 默认换行
            return None
        self._on_chat_send()
        return "break"

    def _on_chat_send(self):
        text = self.txt_input.get("1.0", "end").strip()
        if not text or self._busy:
            return
        p = self._picked
        if not p:
            return
        if not self._has_api():
            self.chat_needkey.pack(fill="x", padx=20, pady=(10, 0),
                                   before=self.chat_wrap_anchor())
            return
        self._chat_append("我", text, is_user=True)
        self.txt_input.delete("1.0", "end")
        self._chat_examples_clear()
        self._chat_thinking(True)
        self._set_busy(True)
        cfg = dict(self.app.cfg)
        history = list(self._chat_history)
        target = p

        def worker():
            try:
                ctx = analyzer.build_chat_context(target)
                if not ctx:
                    reply = ("这条录音还没有文字稿或分析报告，先去「文字稿」页"
                             "生成文字稿，我就能基于通话内容回答了。")
                else:
                    context, name = ctx
                    reply = analyzer.chat_about(name, context, history, text, cfg)
                self.ui_call(lambda: self._chat_done(target, text, reply))
            except Exception as exc:
                self.ui_call(lambda: self._chat_failed(target, str(exc)))

        threading.Thread(target=worker, name="chat", daemon=True).start()

    def _chat_examples_clear(self):
        for w in self.chat_examples.winfo_children():
            w.destroy()

    def _chat_thinking(self, show: bool):
        """在输入区上方显示"正在思考"提示（AI 回答长录音问题可能要几十秒）。"""
        for w in self.chat_examples.winfo_children():
            w.destroy()
        if show:
            tk.Label(self.chat_examples,
                     text="正在思考… 长录音的问题约需 10~60 秒，请稍等。",
                     bg=CARD, fg=PRIMARY, font=F_SMALL, anchor="w").pack(
                fill="x")

    def _chat_done(self, target: Path, user_text: str, reply: str):
        self._set_busy(False)
        self._chat_thinking(False)
        if self._picked != target:
            self.app.notify(f"已收到回答（{parse_recording(target)['contact']}）")
            return
        self._chat_history += [{"role": "user", "content": user_text},
                               {"role": "assistant", "content": reply}]
        self._chat_append("助手", self._plain_text(reply))

    def _chat_failed(self, target: Path, msg: str):
        self._set_busy(False)
        self._chat_thinking(False)
        if self._picked != target:
            self.app.notify(f"AI 问答失败：{msg}")
            return
        self._chat_append("助手", f"出错了：{msg}\n可以检查网络或「设置 → AI 服务」。")

    # ---------- 任务 ----------
    def _start_task(self, p: Path, kind: str, cfg: dict, aspects: list):
        self._task_mp3 = p
        self._task_kind = kind
        self._prog_text = "准备中…"
        self._set_busy(True)
        self._banner_show("info", f"正在处理：{self._display_name(p)}",
                          "准备中…", None)

        def on_update(text, percent=None):
            self.ui_call(lambda: self._task_progress(p, text, percent))

        def worker():
            try:
                txt, md = analyzer.run_analysis(p, cfg, aspects,
                                                log=self.app.log,
                                                on_update=on_update)
                self.ui_call(lambda: self._task_done(p, kind, md))
            except Exception as exc:
                self.ui_call(lambda: self._task_failed(p, kind, str(exc)))

        threading.Thread(target=worker, name="analyze", daemon=True).start()

    def _task_progress(self, p: Path, text: str, percent):
        if self._task_mp3 != p:
            return
        self._prog_text = text
        self._banner_show("info", f"正在处理：{self._display_name(p)}", text,
                          percent)

    def _task_done(self, p: Path, kind: str, md_path: Path):
        self._task_mp3 = None
        self._set_busy(False)
        self._banner_hide()
        self._refresh_recordings()
        if self._picked == p:
            self._render_detail()
            if kind == "transcript":
                msg = "文字稿已生成。"
            elif md_path is not None:
                msg = f"整理完成：{md_path.name}"
            else:
                msg = "处理完成（未勾选分析内容，只生成了文字稿）。"
            self._set_tab_status(kind, msg, ok=True)
        else:
            self.app.notify(f"已完成：{self._display_name(p)}")
        if kind == "transcript":
            self._maybe_auto_analyze(p)

    def _maybe_auto_analyze(self, p: Path):
        """设置勾选「生成文字稿后自动整理分析报告」时，转写完自动接着分析。

        未配置 API 时直接跳过（设置页已注明：不联网、不产生费用、
        不影响录音和转文字），并在界面给一句明确说明，不做静默降级。
        """
        if not bool(self.app.cfg.get("auto_analyze", False)):
            return
        if self._busy or self._task_mp3:
            return
        if not self._has_api():
            self.app.log.info(
                "自动分析：已开启但未配置 API，跳过（不联网不扣费）")
            if self._picked == p:
                self._set_tab_status(
                    "report",
                    "已开启自动分析，但还没配置 AI 服务，本次只生成了文字稿；"
                    "配置后可手动点「开始分析」。", err=False)
            return
        cfg = dict(self.app.cfg)
        aspects = list(analyzer.ASPECT_KEYS)
        self.app.log.info("自动分析：开始为 %s 生成分析报告", p.name)
        if self._picked == p:
            self._set_tab_status("report", "已自动开始生成分析报告…")
        self._start_task(p, "report", cfg, aspects)

    def _task_failed(self, p: Path, kind: str, msg: str):
        self._task_mp3 = None
        self._set_busy(False)
        stage = self._prog_text or "未知阶段"
        self._banner_show(
            "error", f"处理失败：{self._display_name(p)}",
            f"失败阶段：{stage}\n{msg}\n下一步：检查网络与「设置 → AI 服务」配置，"
            f"然后重新点一次按钮即可。", None)
        self._refresh_recordings()
        if self._picked == p:
            self._set_tab_status(kind, f"失败（{stage}）：{msg}", err=True)

    def _set_tab_status(self, kind: str, text: str, ok=False, err=False):
        label = self.tr_status if kind == "transcript" else self.rep_status
        try:
            color = DANGER if err else (OK_GREEN if ok else SUB)
            label.configure(text=text, fg=color)
        except (tk.TclError, AttributeError):
            pass

    def _banner_show(self, kind: str, title: str, detail: str, pct):
        if not hasattr(self, "task_banner"):
            return
        color = DANGER if kind == "error" else PRIMARY
        self.tb_title.configure(text=title, fg=color)
        self.tb_detail.configure(text=detail)
        if kind == "error":
            self.tb_tip.configure(text="")
            self.tb_close.pack(side="right")
        else:
            self.tb_tip.configure(text="可以继续查看其他录音，当前任务会继续。")
            self.tb_close.pack_forget()
        if pct is None:
            self.tb_bar.configure(mode="indeterminate")
            self.tb_bar.start(12)
        else:
            self.tb_bar.stop()
            self.tb_bar.configure(mode="determinate", value=pct)
        self.task_banner.pack(fill="x", pady=(12, 0), before=self.dtabs)

    def _banner_hide(self):
        try:
            self.tb_bar.stop()
            self.task_banner.pack_forget()
        except (tk.TclError, AttributeError):
            pass

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = ["disabled"] if busy else ["!disabled"]
        for b in (self.btn_gen_tr, self.btn_analyze, self.btn_chat_send):
            try:
                if b is not None and b.winfo_exists():
                    b.state(state)
            except (tk.TclError, AttributeError):
                pass

    # ---------- 生成文字稿 ----------
    def _on_gen_transcript(self):
        if self._busy:
            return
        p = self._picked
        if not p:
            return
        engine = str(self.app.cfg.get("transcribe_engine", "local"))
        cfg = dict(self.app.cfg)
        if engine != "api":
            # 本地路径：本次强制执行纯本地，不因"智能校对"设置意外联网；
            # 只改本次调用的副本，用户保存的偏好不变。
            cfg["transcript_polish"] = False
        self._start_task(p, "transcript", cfg, [])

    # ---------- 引擎 / 模型 ----------
    def _engine_label(self) -> str:
        if str(self.app.cfg.get("transcribe_engine", "local")) == "api":
            return "云端 API · 需要联网，可能产生费用"
        return "本地离线 · 免费不联网"

    def _model_label(self) -> str:
        key = str(self.app.cfg.get("local_asr_model", "sense_voice"))
        return MODEL_LABELS.get(key, MODEL_LABELS["sense_voice"])

    def _on_engine_changed(self):
        val = self.var_engine.get()
        self.app.cfg["transcribe_engine"] = (
            "api" if val.startswith("云端") else "local")
        self.app.save_config()
        self._on_aspect_toggle()
        self.app.log.info("转写引擎切换为 %s", self.app.cfg["transcribe_engine"])

    def _on_model_changed(self):
        label = self.var_model.get()
        key = "funasr_nano" if "Fun-ASR" in label else "sense_voice"
        self.app.cfg["local_asr_model"] = key
        self.app.save_config()
        self.app.log.info("本地转写模型切换为 %s", key)

    # ---------- 页 2：录音状态 ----------
    def _build_page_status(self, parent):
        sf = ScrollFrame(parent, bg=BG)
        sf.pack(fill="both", expand=True)
        body = sf.inner

        card = tk.Frame(body, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", padx=24, pady=(20, 16))
        pad = tk.Frame(card, bg=CARD)
        pad.pack(fill="x", padx=22, pady=20)
        tk.Label(pad, text="录音状态", bg=CARD, fg=TEXT, font=F_H2,
                 anchor="w").pack(fill="x")
        srow = tk.Frame(pad, bg=CARD)
        srow.pack(anchor="w", pady=(14, 0))
        self.st_dot = tk.Canvas(srow, width=18, height=18, bg=CARD,
                                highlightthickness=0)
        self.st_dot.pack(side="left")
        self.st_dot_id = self.st_dot.create_oval(2, 2, 17, 17, fill=DOT_GRAY,
                                                 outline="")
        self.st_text = tk.Label(srow, text="…", bg=CARD, fg=TEXT, font=F_H1)
        self.st_text.pack(side="left", padx=(12, 0))
        self.st_desc = tk.Label(pad, text="", bg=CARD, fg=SUB, font=F_BODY,
                                anchor="w", justify="left", wraplength=640)
        self.st_desc.pack(fill="x", pady=(12, 0))
        self._auto_wrap(self.st_desc)
        self.var_enabled = tk.BooleanVar(value=self.app.recording_is_enabled())
        tk.Checkbutton(pad, text="启用自动录音", variable=self.var_enabled,
                       command=self._on_toggle_enabled, bg=CARD, fg=TEXT,
                       font=F_H3, activebackground=CARD, selectcolor=CARD,
                       anchor="w").pack(anchor="w", pady=(16, 0))

        err_card = tk.Frame(body, bg=CARD, highlightbackground=BORDER,
                            highlightthickness=1)
        err_card.pack(fill="x", padx=24)
        ep = tk.Frame(err_card, bg=CARD)
        ep.pack(fill="x", padx=22, pady=18)
        head = tk.Frame(ep, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="最近录音错误", bg=CARD, fg=TEXT, font=F_H3,
                 anchor="w").pack(side="left")
        ttk.Button(head, text="打开日志", style="Secondary.TButton",
                   command=self._on_open_log).pack(side="right")
        self.err_text = tk.Label(ep, text="无", bg=CARD, fg=SUB, font=F_BODY,
                                 anchor="w", justify="left", wraplength=640)
        self.err_text.pack(fill="x", pady=(8, 0))
        self._auto_wrap(self.err_text)
        tk.Label(ep, text="这里只显示录音本身的错误；文字稿或 AI 整理的问题，"
                          "会在对应通话的页面里提示。", bg=CARD, fg=SUB,
                 font=F_SMALL, anchor="w", justify="left",
                 wraplength=640).pack(fill="x", pady=(6, 0))
        sf.wheel_bind_all()

    # ---------- 页 3：设置 ----------
    def _build_page_settings(self, parent):
        sf = ScrollFrame(parent, bg=BG)
        sf.pack(fill="both", expand=True)
        body = sf.inner

        # ---- 录音设置 ----
        card = tk.Frame(body, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", padx=24, pady=(20, 16))
        pad = tk.Frame(card, bg=CARD)
        pad.pack(fill="x", padx=22, pady=18)
        tk.Label(pad, text="录音设置", bg=CARD, fg=TEXT, font=F_H2,
                 anchor="w").pack(fill="x", pady=(0, 12))

        # 保存位置
        r = tk.Frame(pad, bg=CARD)
        r.pack(fill="x", pady=6)
        tk.Label(r, text="保存位置", bg=CARD, fg=TEXT, font=F_BODY, width=12,
                 anchor="w").pack(side="left")
        self.var_dir = tk.StringVar(value=str(self.app.out_dir))
        ttk.Entry(r, textvariable=self.var_dir, state="readonly").pack(
            side="left", fill="x", expand=True)
        ttk.Button(r, text="选择文件夹", style="Secondary.TButton",
                   command=self._on_browse).pack(side="left", padx=(8, 0))
        ttk.Button(r, text="打开文件夹", style="Secondary.TButton",
                   command=self._on_open_dir).pack(side="left", padx=(6, 0))

        # 自动清理
        r = tk.Frame(pad, bg=CARD)
        r.pack(fill="x", pady=6)
        tk.Label(r, text="自动清理", bg=CARD, fg=TEXT, font=F_BODY, width=12,
                 anchor="w").pack(side="left")
        self.var_limit = tk.DoubleVar(
            value=float(self.app.cfg.get("max_recordings_gb", 0) or 0))
        spin = tk.Spinbox(r, from_=0, to=1000, increment=1, width=6,
                          textvariable=self.var_limit, font=F_BODY,
                          bg=CARD, fg=TEXT, relief="solid", bd=1,
                          highlightthickness=0, justify="center",
                          command=self._on_limit_changed)
        spin.pack(side="left")
        tk.Label(r, text="GB", bg=CARD, fg=TEXT, font=F_BODY).pack(
            side="left", padx=(6, 0))
        self.var_limit.trace_add(
            "write", lambda *_: self.root.after_idle(self._on_limit_changed))
        tk.Label(pad, text="超过后自动删除最旧录音，0 表示不自动清理。",
                 bg=CARD, fg=SUB, font=F_SMALL, anchor="w").pack(
            fill="x", padx=(0, 0), pady=(0, 6))

        # 开机自启
        self.var_boot = tk.BooleanVar(value=autostart.is_enabled())
        tk.Checkbutton(pad, text="登录 Windows 后自动启动", variable=self.var_boot,
                       command=self._on_toggle_boot, bg=CARD, fg=TEXT,
                       font=F_BODY, activebackground=CARD, selectcolor=CARD,
                       anchor="w").pack(anchor="w", pady=(6, 0))
        tk.Label(pad, text="启动后在托盘运行，不会弹出窗口。", bg=CARD, fg=SUB,
                 font=F_SMALL, anchor="w").pack(fill="x")

        # 联系人姓名识别诊断（排查"识别不到姓名"时临时打开）
        self.var_ocrdiag = tk.BooleanVar(
            value=bool(self.app.cfg.get("ocr_save_debug_screenshots", False)))
        tk.Checkbutton(pad,
                       text="保存联系人识别的诊断截图（排查「认不出姓名」时用）",
                       variable=self.var_ocrdiag, command=self._on_toggle_ocrdiag,
                       bg=CARD, fg=TEXT, font=F_BODY, activebackground=CARD,
                       selectcolor=CARD, anchor="w",
                       wraplength=640).pack(anchor="w", pady=(12, 0))
        self.ocrdiag_note = tk.Label(
            pad,
            text="只截微信通话窗口本身（不会截桌面上的其他内容），"
                 "样本最多留 3 张，存在「测试录音\\ocr_diag」。"
                 "平时请保持关闭；排查完请手动取消勾选。",
            bg=CARD, fg=SUB, font=F_SMALL, anchor="w", justify="left",
            wraplength=640)
        self.ocrdiag_note.pack(fill="x", pady=(4, 0))
        self._auto_wrap(self.ocrdiag_note)

        r = tk.Frame(pad, bg=CARD)
        r.pack(fill="x", pady=(14, 0))
        ttk.Button(r, text="打开日志", style="Secondary.TButton",
                   command=self._on_open_log).pack(side="left")
        self.opt_status = tk.Label(pad, text="", bg=CARD, fg=SUB, font=F_SMALL,
                                   anchor="w", justify="left", wraplength=620)
        self.opt_status.pack(fill="x", pady=(8, 0))

        # ---- AI 服务 ----
        card2 = tk.Frame(body, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1)
        card2.pack(fill="x", padx=24, pady=(0, 16))
        pad2 = tk.Frame(card2, bg=CARD)
        pad2.pack(fill="x", padx=22, pady=18)
        tk.Label(pad2, text="AI 服务", bg=CARD, fg=TEXT, font=F_H2,
                 anchor="w").pack(fill="x")
        tk.Label(pad2,
                 text="免费录音和本地转文字无需填写这里。使用 AI 整理和问答时"
                      "再配置。",
                 bg=CARD, fg=SUB, font=F_SMALL, anchor="w", justify="left",
                 wraplength=680).pack(fill="x", pady=(6, 12))

        self.var_api = {}
        fields = [("服务地址", "api_base_url", "填到 /v1 为止"),
                  ("服务密钥", "api_key", "sk-…"),
                  ("AI 对话模型", "chat_model", "如 qwen-plus"),
                  ("云端转写模型", "transcribe_model", "用本地转写可不填")]
        for label, key, hint in fields:
            r = tk.Frame(pad2, bg=CARD)
            r.pack(fill="x", pady=5)
            tk.Label(r, text=label, bg=CARD, fg=TEXT, font=F_BODY, width=12,
                     anchor="w").pack(side="left")
            v = tk.StringVar(value=str(self.app.cfg.get(key, "") or ""))
            self.var_api[key] = v
            if key == "api_key":
                self.ent_key = tk.Entry(r, textvariable=v, show="•", font=F_BODY,
                                        bg=CARD, fg=TEXT, relief="solid", bd=1,
                                        highlightthickness=0)
                self.ent_key.pack(side="left", fill="x", expand=True)
                self.var_show_key = tk.BooleanVar(value=False)
                tk.Checkbutton(r, text="显示", variable=self.var_show_key,
                               command=self._toggle_key_visible, bg=CARD,
                               fg=SUB, font=F_SMALL, activebackground=CARD,
                               selectcolor=CARD).pack(side="left", padx=(8, 0))
            else:
                ttk.Entry(r, textvariable=v).pack(side="left", fill="x",
                                                  expand=True)
                tk.Label(r, text=hint, bg=CARD, fg=SUB, font=F_SMALL).pack(
                    side="left", padx=(8, 0))

        row = tk.Frame(pad2, bg=CARD)
        row.pack(fill="x", pady=(14, 0))
        ttk.Button(row, text="保存设置", style="Primary.TButton",
                   command=self._on_api_save).pack(side="left")
        self.btn_test = ttk.Button(row, text="测试连接", style="Secondary.TButton",
                                   command=self._on_api_test)
        self.btn_test.pack(side="left", padx=8)
        ttk.Button(row, text="填入阿里云百炼推荐值", style="Secondary.TButton",
                   command=self._on_fill_recommend).pack(side="left")

        # ---- 自动分析开关 ----
        self.var_autoan = tk.BooleanVar(
            value=bool(self.app.cfg.get("auto_analyze", False)))
        tk.Checkbutton(
            pad2,
            text="生成文字稿后自动整理分析报告（调用 AI，可能产生 API 费用）",
            variable=self.var_autoan, command=self._on_toggle_autoan,
            bg=CARD, fg=TEXT, font=F_BODY, activebackground=CARD,
            selectcolor=CARD, anchor="w", wraplength=640).pack(
            fill="x", pady=(14, 0))
        self.autoan_note = tk.Label(
            pad2,
            text="勾选后，每生成一份文字稿会自动接着生成分析报告。"
                 "没有填写 API 密钥时不会联网、不产生费用，只生成文字稿，"
                 "不影响录音和转文字使用。",
            bg=CARD, fg=SUB, font=F_SMALL, anchor="w", justify="left",
            wraplength=680)
        self.autoan_note.pack(fill="x", pady=(4, 0))
        self._auto_wrap(self.autoan_note)

        self.var_api_status = tk.StringVar(value="")
        self.api_status = tk.Label(pad2, textvariable=self.var_api_status,
                                   bg=CARD, fg=PRIMARY, font=F_SMALL, anchor="w",
                                   justify="left", wraplength=680)
        self.api_status.pack(fill="x", pady=(10, 0))
        tk.Label(pad2, text="密钥保存在本机配置文件 config.json 中；使用 AI 服务时"
                            "会发送给你配置的服务商。",
                 bg=CARD, fg=SUB, font=F_SMALL, anchor="w", justify="left",
                 wraplength=680).pack(fill="x", pady=(8, 0))

        # ---- 退出程序 ----
        card3 = tk.Frame(body, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1)
        card3.pack(fill="x", padx=24, pady=(0, 24))
        pad3 = tk.Frame(card3, bg=CARD)
        pad3.pack(fill="x", padx=22, pady=18)
        tk.Label(pad3, text="退出程序", bg=CARD, fg=TEXT, font=F_H3,
                 anchor="w").pack(side="left")
        ttk.Button(pad3, text="退出程序", style="Danger.TButton",
                   command=self._on_quit).pack(side="right")
        sf.wheel_bind_all()

    # ---------- 设置页动作 ----------
    def _on_toggle_ocrdiag(self):
        """「保存联系人识别诊断截图」开关：立即写回配置。"""
        try:
            self.app.cfg["ocr_save_debug_screenshots"] = \
                bool(self.var_ocrdiag.get())
            self.app.save_config(self.app.cfg)
            self.app.log.info("OCR 诊断截图开关：%s",
                              self.app.cfg["ocr_save_debug_screenshots"])
        except (tk.TclError, AttributeError, KeyError):
            pass

    def _on_toggle_autoan(self):
        """「文字稿后自动分析报告」开关：立即写回配置。"""
        try:
            self.app.cfg["auto_analyze"] = bool(self.var_autoan.get())
            self.app.save_config(self.app.cfg)
            self.app.log.info("自动分析报告开关：%s", self.app.cfg["auto_analyze"])
        except (tk.TclError, AttributeError, KeyError):
            pass

    def _toggle_key_visible(self):
        if hasattr(self, "ent_key"):
            try:
                self.ent_key.configure(show="" if self.var_show_key.get() else "•")
            except tk.TclError:
                pass

    def _on_fill_recommend(self):
        self.var_api["api_base_url"].set(
            "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.var_api["chat_model"].set("qwen-plus")
        self.var_api["transcribe_model"].set("")
        self.var_api_status.set(
            "已填入阿里云百炼的地址和模型名。把服务密钥粘进第二行，"
            "点「保存设置」→「测试连接」。")
        self._set_api_status_color(PRIMARY)

    def _set_api_status_color(self, color):
        try:
            self.api_status.configure(fg=color)
        except tk.TclError:
            pass

    def _on_api_save(self):
        for key, var in self.var_api.items():
            self.app.cfg[key] = var.get().strip()
        try:
            self.app.save_config()
            self.var_api_status.set("已保存。")
            self._set_api_status_color(OK_GREEN)
        except Exception as exc:
            self.var_api_status.set(f"保存失败：{exc}（设置没有生效，请检查文件是否被占用）")
            self._set_api_status_color(DANGER)

    def _on_api_test(self):
        self._on_api_save()
        self.btn_test.state(["disabled"])
        self.var_api_status.set("测试中…")
        self._set_api_status_color(PRIMARY)
        cfg = dict(self.app.cfg)

        def worker():
            try:
                reply = api_client.test_connection(cfg)
                self.ui_call(lambda: self._test_done(reply))
            except Exception as exc:
                self.ui_call(lambda: self._test_failed(str(exc)))

        threading.Thread(target=worker, name="apitest", daemon=True).start()

    _ERR_HINTS = (
        ("401", "服务密钥不对或已失效：去服务商后台重新生成一个，整串复制粘贴，别带空格"),
        ("403", "这个密钥没有该模型的权限，或账号未实名 / 已欠费"),
        ("404", "服务地址或模型名不对：地址要填到 /v1 为止；模型名要和官网一字不差"),
        ("429", "调用太频繁或余额不足：等一会儿再试，或去充值"),
        ("502", "服务商临时故障，过几分钟重试即可"),
        ("无法连接", "网络问题：确认地址没有多写或少写字符，浏览器能打开该服务商网站"),
        ("未配置", "还没填完：服务地址、服务密钥、对话模型名都要填"),
    )

    def _test_done(self, reply):
        self.btn_test.state(["!disabled"])
        self.var_api_status.set(f"连接正常，模型回复：{reply}")
        self._set_api_status_color(OK_GREEN)

    def _test_failed(self, msg):
        self.btn_test.state(["!disabled"])
        hint = next((t for kw, t in self._ERR_HINTS if kw in msg), "")
        text = f"连接失败：{msg}"
        if hint:
            text += f"\n{hint}"
        self.var_api_status.set(text)
        self._set_api_status_color(DANGER)

    def _on_toggle_enabled(self):
        self.app.set_recording_enabled(self.var_enabled.get())

    def _on_browse(self):
        path = filedialog.askdirectory(title="选择录音保存目录",
                                       initialdir=str(self.app.out_dir),
                                       parent=self.root)
        if not path:
            return
        try:
            self.app.set_out_dir(path)
            self.var_dir.set(str(self.app.out_dir))
            self._picked = None
            self._refresh_recordings()
        except Exception as exc:
            self.opt_status.configure(text=f"无法使用该目录：{exc}", fg=DANGER)

    def _on_limit_changed(self):
        try:
            value = float(self.var_limit.get())
        except (ValueError, tk.TclError):
            return
        if value < 0:
            value = 0
        self.app.set_max_recordings_gb(value)

    def _on_toggle_boot(self):
        try:
            if self.var_boot.get():
                autostart.enable()
                self.opt_status.configure(
                    text="已创建开机启动项（仅本软件），启动后在托盘运行。",
                    fg=OK_GREEN)
            else:
                autostart.disable()
                self.opt_status.configure(text="已关闭开机自启。", fg=SUB)
        except Exception as exc:
            self.opt_status.configure(text=f"设置开机自启失败：{exc}", fg=DANGER)
        self.var_boot.set(autostart.is_enabled())

    def _on_open_dir(self):
        try:
            subprocess.Popen(["explorer.exe", str(self.app.out_dir)])
        except Exception:
            pass

    def _on_open_log(self):
        try:
            subprocess.Popen(["notepad.exe", str(self.app.log_path())])
        except Exception:
            pass

    def _open_file(self, path: Path):
        try:
            if path.exists():
                os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法打开：{exc}", parent=self.root)

    def _copy_file(self, path: Path, what: str):
        try:
            text = path.read_text("utf-8")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"读取失败：{exc}", parent=self.root)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        messagebox.showinfo(APP_NAME, f"{what}已复制到剪贴板。", parent=self.root)

    def _on_quit(self):
        if messagebox.askyesno(APP_NAME, "确定退出程序？正在进行的录音会先保存。",
                               parent=self.root):
            self.app.request_exit()

    # ================= 定时刷新 =================
    def _poll(self):
        try:
            while True:
                cmd = self.cmd_queue.get_nowait()
                if cmd == "show":
                    self.show()
                elif cmd == "exit":
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        except Exception:
            # 命令处理异常不能让轮询循环停摆（否则所有 UI 回调全部失联）
            try:
                if self.app.log:
                    self.app.log.error("cmd_queue 处理异常：%s",
                                       traceback.format_exc())
            except Exception:
                pass
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    # 不再静默吞掉：写日志，便于排查"界面卡在处理中"这类问题
                    try:
                        if self.app.log:
                            self.app.log.error("UI 回调执行失败：%s",
                                               traceback.format_exc())
                    except Exception:
                        pass
        except queue.Empty:
            pass
        if self.root:
            self.root.after(200, self._poll)

    def _refresh(self):
        if not self.root:
            return
        try:
            st = self.app.get_status()
            state = self.app.display_state()
            color, label = STATUS_UI.get(state, (DOT_GRAY, str(state)))
            err = st.get("last_error")
            enabled = bool(st.get("enabled"))
            out_dir = str(st.get("out_dir") or "")

            # 顶部状态条
            self.top_dot.itemconfigure(self.top_dot_id, fill=color)
            self.top_status.configure(text=label)
            if state == "error" and err:
                self.top_err.configure(text=f"· {err}")
            else:
                self.top_err.configure(text="")
            shown = out_dir if len(out_dir) <= 56 else "…" + out_dir[-55:]
            self.top_dir.configure(text=f"录音保存在：{shown}")

            # 录音状态页
            self.st_dot.itemconfigure(self.st_dot_id, fill=color)
            self.st_text.configure(text=label)
            self.st_desc.configure(text=self._status_desc(state))
            if self.var_enabled.get() != enabled:
                self.var_enabled.set(enabled)
            if err:
                self.err_text.configure(text=err, fg=DANGER)
            else:
                self.err_text.configure(text="无", fg=SUB)
            if self.var_dir.get() != out_dir:
                self.var_dir.set(out_dir)
        except Exception:
            pass
        self.root.after(500, self._refresh)

    @staticmethod
    def _status_desc(state: str) -> str:
        if state == "paused":
            return "开启后，微信通话接通时自动录音。"
        if state == "waiting":
            return "微信通话接通后，会自动录下双方声音。"
        if state == "recording":
            return "通话正在进行，双方声音正在录制，挂断后自动保存。"
        if state == "saving":
            return "通话已结束，正在保存录音文件。"
        if state == "error":
            return "这次录音没有成功。约 10 秒后会自动重试；若持续失败，可打开日志查看原因。"
        return ""
