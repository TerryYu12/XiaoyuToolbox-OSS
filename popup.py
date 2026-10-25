# -*- coding: utf-8 -*-
"""翻译结果弹窗（持久 UI 线程 + 队列驱动）。

架构（踩坑记录，2026-09-17 实测）：
  - 不要每次弹窗新建 Tk()：曾实测导致 (a) 窗口缺事件泵不上屏
    （winfo_ismapped()=False，弹窗"看不见"），(b) 退出时
    "Tcl_AsyncDelete: async handler deleted by the wrong thread" 崩溃。
  - 现方案：全进程只建一个 Tk 解释器，跑在专属 daemon UI 线程里；
    隐藏根窗口 + 复用一个 Toplevel；流式翻译在工作线程，经 queue 投递，
    UI 线程用 after() 轮询取件。所有 Tk 调用都发生在 UI 线程内。

2026-09-28 UI 重构（对标 STranslate 弹窗体验，线程架构不变）：
  - 顶栏：可拖动移动窗口 + 引擎/方向徽标 + 置顶/朗读/复制/关闭按钮
  - 原文独立展示区，默认两行，点击展开全文（超长滚轮）
  - 窗口高度按内容自适应（clamp 内），超长译文滚轮滚动（Tk Text 自带滚轮）
  - 翻译中状态动画；失败提示可点击重试；流式带代次号，旧流/重试残留不串写
  - 鼠标悬停暂停自动隐藏，移开后恢复剩余计时；置顶彻底停掉自动隐藏
  - 朗读走 SAPI（win32com），按目标语言挑声源，线程内 CoInitialize
"""
import itertools
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

from translate_backend import translate_stream, translate

BG = "#1e1f26"        # 窗体背景
BAR_BG = "#262834"    # 顶栏背景
BORDER = "#34374a"    # 外边框/分隔线
FG = "#e8e8ee"        # 译文前景
FG_DIM = "#8a8d99"    # 原文/提示前景
FG_OK = "#7ddba3"
FG_ERR = "#ff8a8a"
ACCENT = "#7aa2f7"
FONT = "Microsoft YaHei UI"
HINT = "悬停暂停自动隐藏 · 滚轮滚动长文"


def _tk_ratio(tk_screen_w):
    """Tk 坐标 → 物理像素换算比（弹窗尺寸按物理像素设计，乘它换算回来）。

    exe 里 pywebview 会先调 SetProcessDPIAware()（进程级，winforms.py:820），
    弹窗 Tk 的坐标=物理像素（ratio=1）；独立跑 python 时 Tk 是 DPI-unaware，
    坐标被系统按缩放比虚拟化（ratio=1/缩放比，Windows 把窗口整体拉伸）。
    两种世界经此换算后渲染尺寸一致。字体一律用像素字号（负数），不依赖
    Tk 对 DPI 的查询。"""
    try:
        import win32api
        import win32con
        dm = win32api.EnumDisplaySettings(None, win32con.ENUM_CURRENT_SETTINGS)
        phys = int(dm.PelsWidth)
        if phys > 0 and tk_screen_w > 0:
            return tk_screen_w / phys
    except Exception:
        pass
    return 1.0


class TransPopup:
    # 设计尺寸按「物理像素」定义（2560x1600@150% 屏实测校准，2026-09-28
    # 用户反馈 420 逻辑宽的旧弹窗在 DPI-aware 的 exe 里小到看不清），
    # _build 时乘 _tk_ratio 换算回 Tk 坐标落到 self.W / self.min_h / self.max_h
    DESIGN_W = 760
    DESIGN_MIN_H = 220
    DESIGN_MAX_H = 780
    SRC_COLLAPSED = 2        # 原文默认可见行数
    SRC_EXPANDED = 10        # 原文展开后的行数上限（再长滚轮）
    BODY_MIN_LINES = 4
    BODY_MAX_LINES = 18

    def __init__(self, cfg, host="127.0.0.1", port=8899):
        self.cfg = cfg
        self.host = host
        self.port = port
        self.mapped = None         # 测试用：窗口是否已映射上屏
        self.win_pos = None        # 测试用：窗口屏幕坐标
        self.W = self.DESIGN_W     # 兜底值，_build 按比例重算
        self.min_h = self.DESIGN_MIN_H
        self.max_h = self.DESIGN_MAX_H
        self._r = 1.0
        self._scale = 1.0          # 用户缩放倍率（设置页滑杆，cfg:popup_scale）
        try:
            self._scale = max(0.5, min(2.5,
                              float(cfg.get("popup_scale", 1.0) or 1.0)))
        except Exception:
            pass
        self._q = queue.Queue()
        self._win = None
        self._src = None           # 原文 Text
        self._sep = None           # 原文/译文分隔线（toast 模式隐藏）
        self._txt = None           # 译文 Text
        self._hint = None
        self._badge = None
        self._pin_btn = None
        self._hide_job = None
        self._hide_left = None     # 悬停暂停时的剩余秒数
        self._hide_at = 0.0
        self._anim_job = None
        self._anim_i = 0
        self._body_text = ""       # 累计译文正文（供复制/朗读）
        self._src_text = ""
        self._copied = False
        self._copied_text = None   # 最近一次弹窗自己写入剪贴板的内容（监听防回环）
        self._pinned = False
        self._failed = None        # 非 None 时点提示条=重试
        self._last_req = None      # (text, to) 供重试
        self._last_to = "zh"       # 朗读选声源用
        self._stream_id = 0        # 当前接受的流式代次（新划词/重试后旧流作废）
        self._sid_gen = itertools.count(1)   # next() 原子，show 可来自多线程
        self._src_expanded = False
        self._fonts = {}
        self._pos = None           # (x, y) 当前窗口位置
        self._speak_lock = threading.Lock()
        self._started = threading.Event()
        threading.Thread(target=self._ui_thread, daemon=True).start()
        self._started.wait(5)

    # -------------------------------------------------- UI 线程
    def _ui_thread(self):
        try:
            root = tk.Tk()
        except Exception as e:
            print(f"[popup] Tk 初始化失败（打包环境常见）: {e}", flush=True)
            return
        root.withdraw()
        self._root = root
        self._started.set()
        root.after(40, self._poll)
        root.mainloop()

    def _poll(self):
        try:
            while True:
                kind, *args = self._q.get_nowait()
                self._handle(kind, args)
        except queue.Empty:
            pass
        try:
            self._root.after(40, self._poll)
        except tk.TclError:
            pass

    def _handle(self, kind, args):
        if kind == "show":
            self._open(args[0], args[1], args[2])
            self._anim_start()
        elif kind == "chunk":
            # args = (stream_id, piece)：旧流（重试/再次划词前残留）直接丢弃
            if args[0] == self._stream_id:
                self._insert(args[1])
        elif kind == "fail":
            if args[0] == self._stream_id:
                self._mark_fail(args[1])
        elif kind == "done":
            if args[0] == self._stream_id:
                self._finish()
        elif kind == "hint":
            self._set_hint(args[0], args[1])
        elif kind == "hide":
            self._close()
        elif kind == "preview":
            # 设置页滑杆实时预览：按新比例重建弹窗展示示例内容
            self._preview()
        elif kind == "call":
            # 线程安全 UI 调用通道：callable 在 UI 线程执行（测试/外部扩展用）
            try:
                args[0]()
            except Exception as e:
                print(f"[popup] call 执行失败: {e}", flush=True)
        elif kind == "toast":
            self._open("", None, "")
            self._insert(args[0])
            self._arm_hide(args[1])

    # -------------------------------------------------- 构建
    def _build(self):
        win = tk.Toplevel(self._root)
        # 先按物理分辨率换算尺寸（见 _tk_ratio），再乘用户缩放倍率，
        # 全部像素尺寸在这算好
        r = _tk_ratio(win.winfo_screenwidth()) * self._scale
        self._r = r
        self.W = int(self.DESIGN_W * r)
        self.min_h = int(self.DESIGN_MIN_H * r)
        self.max_h = int(self.DESIGN_MAX_H * r)
        bar_h = int(40 * r)
        bottom_h = int(30 * r)
        pad = int(14 * r)
        f_body = -int(26 * r)
        f_src = -int(19 * r)
        f_bar = -int(19 * r)
        f_hint = -int(16 * r)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.96)
        except Exception:
            pass
        win.configure(bg=BG)

        outer = tk.Frame(win, bg=BORDER)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=BG)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        # 底部状态条先占位（pack 顺序决定 body 能否正确 expand）
        bottom = tk.Frame(inner, bg=BG, height=bottom_h)
        bottom.pack(side="bottom", fill="x")
        bottom.pack_propagate(False)
        hint = tk.Label(bottom, text=HINT, bg=BG, fg=FG_DIM,
                        font=(FONT, f_hint), anchor="w")
        hint.pack(side="left", padx=pad, fill="x", expand=True)

        # 顶栏：徽标（可拖动）+ 按钮组
        bar = tk.Frame(inner, bg=BAR_BG, height=bar_h)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        badge = tk.Label(bar, text="", bg=BAR_BG, fg=FG_DIM,
                         font=(FONT, f_bar), anchor="w")
        badge.pack(side="left", padx=pad)
        for wgt in (bar, badge):
            wgt.bind("<Button-1>", self._drag_start)
            wgt.bind("<B1-Motion>", self._drag_move)

        def btn(text, cmd):
            lbl = tk.Label(bar, text=text, bg=BAR_BG, fg=FG_DIM,
                           font=(FONT, f_bar), padx=int(9 * r), cursor="hand2")
            lbl._base = FG_DIM
            lbl.bind("<Enter>", lambda e: lbl.config(fg=FG))
            lbl.bind("<Leave>", lambda e: lbl.config(fg=lbl._base))
            lbl.bind("<Button-1>", lambda e: cmd())
            lbl.pack(side="right", padx=(0, 4))
            return lbl

        self._pin_btn = btn("置顶", self._toggle_pin)
        btn("朗读", self._speak)
        btn("复制", self._copy_body)
        btn("✕", self._close)

        # 原文区：点击展开/收起
        src = tk.Text(inner, wrap="word", font=(FONT, f_src), bg=BG, fg=FG_DIM,
                      bd=0, highlightthickness=0, padx=pad, pady=int(8 * r),
                      height=2, cursor="hand2", state="disabled")
        src.pack(side="top", fill="x")
        sep = tk.Frame(inner, bg=BORDER, height=1)
        sep.pack(side="top", fill="x")
        src.bind("<Button-1>", lambda e: self._toggle_src())
        src.tag_config("tail", foreground=ACCENT)

        # 译文区
        txt = tk.Text(inner, wrap="word", font=(FONT, f_body), bg=BG, fg=FG,
                      bd=0, highlightthickness=0, padx=pad, pady=int(6 * r),
                      cursor="arrow", state="disabled")
        txt.pack(side="top", fill="both", expand=True)
        txt.tag_config("body", foreground=FG)
        txt.tag_config("fail", foreground=FG_ERR)

        win.bind("<Escape>", lambda e: self._close())
        win.bind("<Enter>", lambda e: self._hover_check())
        win.bind("<Leave>", lambda e: self._hover_check())

        self._win = win
        self._src = src
        self._sep = sep
        self._txt = txt
        self._hint = hint
        self._badge = badge
        self._padx2 = pad * 2   # 文本区左右内边距合计（估行宽用）
        self._fonts = {
            "src": tkfont.Font(root=self._root, font=(FONT, f_src)),
            "body": tkfont.Font(root=self._root, font=(FONT, f_body)),
        }

    # -------------------------------------------------- 尺寸自适应
    def _needed_lines(self, text, font, avail_px):
        if not text:
            return 0
        n = 0
        for line in text.split("\n"):
            n += max(1, -(-font.measure(line) // avail_px))
        return n

    def _src_lines(self):
        """原文区可见行数（含「展开/收起」尾巴行），无原文返回 0。"""
        if not self._src_text:
            return 0
        need = self._needed_lines(self._src_text, self._fonts["src"],
                                  self.W - self._padx2)
        if self._src_expanded:
            return min(need, self.SRC_EXPANDED) + 1
        if need > self.SRC_COLLAPSED:
            return self.SRC_COLLAPSED + 1
        return need

    def _fit(self):
        """按当前内容算期望高度并 clamp；位置保持不变（防越屏）。"""
        win = self._win
        if win is None or not self._fonts:
            return
        r = self._r
        src_ls = self._fonts["src"].metrics("linespace")
        body_ls = self._fonts["body"].metrics("linespace")
        body_need = self._needed_lines(self._body_text, self._fonts["body"],
                                       self.W - self._padx2)
        body_vis = max(self.BODY_MIN_LINES,
                       min(body_need, self.BODY_MAX_LINES))
        h = int(40 * r) + self._src_lines() * src_ls + 1 \
            + body_vis * body_ls + int(30 * r) + int(16 * r)
        h = int(max(self.min_h, min(h, self.max_h)))
        x, y = self._pos if self._pos else (win.winfo_x(), win.winfo_y())
        sh = win.winfo_screenheight()
        y = max(min(y, sh - h - 8), 8)
        self._pos = (x, y)
        win.geometry(f"{self.W}x{h}+{x}+{y}")

    # -------------------------------------------------- 打开 / 内容
    def _open(self, source_text, to, engine, keep_pos=False):
        if self._win is None:
            self._build()
        win = self._win
        if self._hide_job:
            try:
                self._root.after_cancel(self._hide_job)
            except Exception:
                pass
        self._hide_job = None
        self._hide_left = None
        self._stop_anim()
        self._src_text = source_text or ""
        self._body_text = ""
        self._copied = False
        self._copied_text = None
        self._pinned = False
        self._failed = None
        self._src_expanded = False
        self._last_to = to if to else "zh"
        if self._pin_btn is not None:
            self._pin_btn._base = FG_DIM
            self._pin_btn.config(fg=FG_DIM)
        # 清掉上一轮失败留在提示条上的重试点击
        try:
            self._hint.unbind("<Button-1>")
        except Exception:
            pass
        self._hint.config(cursor="arrow")

        if source_text:
            self._badge.config(
                text=f"{engine}  →  {'中文' if to == 'zh' else 'English'}")
            self._render_src()
            # before= 固定原文区在译文区之前（pack_forget 重 pack 会排到末尾）
            self._src.pack(side="top", fill="x", before=self._txt)
            self._sep.pack(side="top", fill="x", before=self._txt)
        else:
            # toast 模式：无原文无徽标，只剩正文
            self._badge.config(text="")
            self._src.pack_forget()
            self._sep.pack_forget()
        self._txt.config(state="normal")
        self._txt.delete("1.0", "end")
        self._txt.config(state="disabled")
        self._set_hint(HINT, FG_DIM)

        if keep_pos and self._pos:
            x, y = self._pos          # 预览模式位置稳定，不跟着鼠标跳
        else:
            mx, my = win.winfo_pointerxy()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            x = max(min(mx + 14, sw - self.W - 8), 8)
            y = max(min(my + 14, sh - int(300 * self._r)), 8)
        self._pos = (x, y)
        self._fit()
        win.deiconify()
        win.lift()
        self._root.after(160, self._record_state)

    def _render_src(self):
        src = self._src
        src.config(state="normal")
        src.delete("1.0", "end")
        if self._src_text:
            src.insert("end", self._src_text)
            src.config(height=max(self._src_lines(), 1))
            need = self._needed_lines(self._src_text, self._fonts["src"],
                                      self.W - self._padx2)
            if self._src_expanded:
                src.insert("end", "\n▸ 收起", "tail")
            elif need > self.SRC_COLLAPSED:
                src.insert("end", "\n▸ 展开原文", "tail")
        src.config(state="disabled")

    def _toggle_src(self):
        if not self._src_text:
            return
        self._src_expanded = not self._src_expanded
        self._render_src()
        self._fit()
        self._root.after(120, self._record_state)   # 展开后的新尺寸回读

    def _insert(self, s):
        try:
            if self._txt is not None:
                self._txt.config(state="normal")
                self._txt.insert("end", s, "body")
                self._txt.config(state="disabled")
                self._txt.see("end")
                self._body_text += s
        except Exception:
            pass

    def _finish(self):
        self._stop_anim()
        self._fit()
        if self._failed is None:
            self._set_hint(HINT, FG_DIM)   # 动画停在「翻译中···」，完成即复位
        self._root.after(120, self._record_state)   # 自适应后的新尺寸回读

    def _mark_fail(self, msg):
        self._stop_anim()
        self._failed = msg
        self._txt.config(state="normal")
        self._txt.insert("end", "\n[翻译失败] ", "fail")
        self._txt.config(state="disabled")
        brief = msg if len(msg) <= 60 else msg[:57] + "…"
        self._set_hint(f"翻译失败：{brief} —— 点此重试", FG_ERR)
        self._hint.config(cursor="hand2")
        try:
            self._hint.unbind("<Button-1>")
        except Exception:
            pass
        self._hint.bind("<Button-1>", lambda e: self._retry())

    def _retry(self):
        if not (self._failed and self._last_req):
            return
        text, to = self._last_req
        self._failed = None
        self._stream_id = next(self._sid_gen)
        self._hint.config(cursor="arrow")
        try:
            self._hint.unbind("<Button-1>")
        except Exception:
            pass
        self._txt.config(state="normal")
        self._txt.delete("1.0", "end")
        self._txt.config(state="disabled")
        self._body_text = ""
        self._anim_start()
        self._spawn_stream(text, to)

    # -------------------------------------------------- 提示条 / 动画
    def _set_hint(self, text, color=FG_DIM):
        if self._hint is not None:
            try:
                self._hint.config(text=text, fg=color)
            except Exception:
                pass

    def _anim_start(self):
        self._anim_i = 0
        self._anim_job = -1     # 非 None 即视为运行中
        self._anim_step()

    def _anim_step(self):
        if self._anim_job is None:
            return
        self._set_hint(f"翻译中{'·' * (self._anim_i % 4)}", FG_DIM)
        self._anim_i += 1
        self._anim_job = self._root.after(350, self._anim_step)

    def _stop_anim(self):
        if self._anim_job is not None and self._anim_job != -1:
            try:
                self._root.after_cancel(self._anim_job)
            except Exception:
                pass
        self._anim_job = None

    # -------------------------------------------------- 自动隐藏 / 悬停
    def _arm_hide(self, secs=None):
        secs = float(secs if secs is not None
                     else self.cfg.get("popup_seconds", 8))
        if self._hide_job:
            try:
                self._root.after_cancel(self._hide_job)
            except Exception:
                pass
        self._hide_job = None
        self._hide_left = None
        if self._pinned:
            return
        self._hide_at = time.monotonic() + secs
        self._hide_job = self._root.after(int(secs * 1000), self._close)

    def _hover_check(self):
        """Tk 跨入子控件也会对 Toplevel 发 Enter/Leave，
        延迟后按指针实际坐标核实，再决定暂停/恢复计时。"""
        self._root.after(60, self._hover_apply)

    def _hover_apply(self):
        win = self._win
        if win is None or self._pinned:
            return
        px, py = win.winfo_pointerxy()
        wx, wy = win.winfo_rootx(), win.winfo_rooty()
        ww, wh = win.winfo_width(), win.winfo_height()
        inside = wx <= px < wx + ww and wy <= py < wy + wh
        if inside:
            if self._hide_job:
                try:
                    self._root.after_cancel(self._hide_job)
                except Exception:
                    pass
                self._hide_job = None
                self._hide_left = max(self._hide_at - time.monotonic(), 3.0)
        elif self._hide_left is not None and not self._hide_job:
            left = self._hide_left
            self._hide_left = None
            self._hide_at = time.monotonic() + left
            self._hide_job = self._root.after(int(left * 1000), self._close)

    def _toggle_pin(self):
        self._pinned = not self._pinned
        base = ACCENT if self._pinned else FG_DIM
        self._pin_btn._base = base
        self._pin_btn.config(fg=base)
        if self._pinned:
            if self._hide_job:
                try:
                    self._root.after_cancel(self._hide_job)
                except Exception:
                    pass
            self._hide_job = None
            self._hide_left = None
            self._set_hint("已置顶：不再自动隐藏，✕ 或 ESC 关闭", ACCENT)
        else:
            self._set_hint(HINT, FG_DIM)
            self._arm_hide()

    # -------------------------------------------------- 拖动
    def _drag_start(self, event):
        win = self._win
        if win is not None:
            self._drag = (event.x_root - win.winfo_x(),
                          event.y_root - win.winfo_y())

    def _drag_move(self, event):
        win = self._win
        if win is not None and getattr(self, "_drag", None):
            x = event.x_root - self._drag[0]
            y = event.y_root - self._drag[1]
            self._pos = (x, y)
            win.geometry(f"+{x}+{y}")

    # -------------------------------------------------- 复制 / 朗读
    def _copy_body(self, event=None):
        text = self._body_text.strip()
        if not text:
            return
        ok = False
        try:
            import win32clipboard as w
            import win32con
            w.OpenClipboard()
            try:
                w.EmptyClipboard()
                w.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                w.CloseClipboard()
            ok = True
        except Exception:
            # 兜底走 Tk 剪贴板
            try:
                self._root.clipboard_clear()
                self._root.clipboard_append(text)
                ok = True
            except Exception:
                ok = False
        self._copied = True
        self._copied_text = text
        self._set_hint("✓ 已复制到剪贴板" if ok else "复制失败",
                       FG_OK if ok else FG_ERR)

    def _speak(self):
        text = self._body_text.strip()
        if not text:
            return
        threading.Thread(target=self._speak_worker,
                         args=(text[:600], self._last_to),
                         daemon=True).start()

    @staticmethod
    def _pick_voice(voice, to):
        """按目标语言挑 SAPI 声源（804=zh-CN，409=en-US），失败用默认。"""
        try:
            want = "409" if to == "en" else "804"
            toks = voice.GetVoices()
            for i in range(toks.Count):
                t = toks.Item(i)
                if want in t.GetAttribute("Language"):
                    return t
        except Exception:
            pass
        return None

    def _speak_worker(self, text, to):
        if not self._speak_lock.acquire(blocking=False):
            self._q.put(("hint", "正在朗读中…", FG_DIM))
            return
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            try:
                v = win32com.client.Dispatch("SAPI.SpVoice")
                tok = self._pick_voice(v, to)
                if tok is not None:
                    v.Voice = tok
                v.Speak(text, 0)    # 同步读完，读完才释放 COM
            finally:
                pythoncom.CoUninitialize()
        except Exception:
            self._q.put(("hint", "朗读不可用（缺 SAPI 声源）", FG_ERR))
        finally:
            self._speak_lock.release()

    def _record_state(self):
        try:
            if self._win is not None:
                self.mapped = bool(self._win.winfo_ismapped())
                self.win_pos = (self._win.winfo_rootx(),
                                self._win.winfo_rooty(),
                                self._win.winfo_width(),
                                self._win.winfo_height())
        except Exception:
            pass

    def _preview(self):
        """设置页实时预览：按新比例重建弹窗（控件尺寸在 _build 一次算定，
        改比例只能重建 Toplevel；单例 Tk 根不变，无跨线程重建风险）。"""
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None
            self._src = None
            self._sep = None
            self._txt = None
            self._hint = None
            self._badge = None
            self._pin_btn = None
            self._fonts = {}
        self._open("预览：拖动设置页里的滑杆，浮窗会按比例实时变化。", "zh",
                   "预览", keep_pos=True)
        self._insert("这是预览译文。字号与窗口宽度随缩放比例变化，"
                     "顶栏可拖动挪位置，几秒后自动消失。")
        self._finish()
        self._arm_hide(6)

    def _close(self):
        self._hide_job = None
        self._hide_left = None
        self._stop_anim()
        try:
            if self._win is not None:
                self._win.withdraw()
        except Exception:
            pass

    # -------------------------------------------------- 外部接口
    def show(self, text, to="zh"):
        if not text:
            return
        text = text.strip()
        if not text:
            return
        if len(text) > self.cfg.get("max_chars", 3000):
            self._toast("文本过长")
            return
        self._last_req = (text, to)
        # 作废在途旧流（在 "show" 入队前递增，chunk 与 show 同队列保序）
        self._stream_id = next(self._sid_gen)
        self._q.put(("show", text, to, self._engine_label()))
        self._spawn_stream(text, to)

    @staticmethod
    def _engine_label():
        try:
            from translate_backend import load_trans_engine_config
            cfg = load_trans_engine_config()
            eng = cfg.get("trans_engine", "cloud")
            if eng == "cloud":
                return "云端极速"
            if eng == "api":
                m = (cfg.get("api_model") or "").split("/")[-1].strip()
                return f"AI·{m[:24]}" if m else "AI API"
            return "本地 Qwen3.5"
        except Exception:
            return ""

    def _spawn_stream(self, text, to):
        sid = self._stream_id
        threading.Thread(target=self._stream_worker, args=(text, to, sid),
                         daemon=True).start()

    def _stream_worker(self, text, to, sid):
        got = False
        try:
            from translate_backend import (translate_stream_unified,
                                           translate_unified,
                                           load_trans_engine_config)
            cfg = load_trans_engine_config()
            eng = cfg.get("trans_engine", "cloud")
            for piece in translate_stream_unified(text, to=to, engine=eng):
                got = True
                self._q.put(("chunk", sid, piece))
            if not got:
                out = translate_unified(text, to=to, engine=eng)
                self._q.put(("chunk", sid, out))
        except Exception as e:
            self._q.put(("fail", sid, str(e)))
        self._q.put(("done", sid))

    def hide(self):
        self._q.put(("hide",))

    def show_preview(self, scale=None):
        """设置页实时预览入口（线程安全）：可先更新缩放倍率再弹示例。
        只展示不落盘，保存由 /api/popup/scale 路由负责。"""
        if scale is not None:
            try:
                self._scale = max(0.5, min(2.5, float(scale)))
            except (TypeError, ValueError):
                pass
        self._q.put(("preview",))

    def _toast(self, msg, secs=3):
        self._q.put(("toast", msg, secs))
