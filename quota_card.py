# -*- coding: utf-8 -*-
"""
Claude 实时限额卡片  (quota_card.py)
====================================
常驻桌面、置顶、卡片式小部件,实时显示 Claude Code 的额度使用情况
(5 小时窗口 / 7 天窗口,及账号存在的其它窗口如 7 天 Sonnet),
并按"系统本地时区"显示每个窗口的实际重置时刻。附带系统托盘图标。

数据来源(与 Claude Code 完全相同的官方接口):
  GET https://api.anthropic.com/api/oauth/usage
凭证:仅从本机 ~/.claude/.credentials.json 读取 OAuth accessToken,
      只放进 HTTP Authorization 头、只发往 api.anthropic.com,
      绝不写出、缓存或上传到任何其它地方。

界面:Python 自带 tkinter;托盘:pystray + Pillow(缺失时自动降级为仅卡片)。

操作:
  · 拖动卡片移动(记忆位置)
  · 滚轮缩放 / 拖动窗口边缘缩放 / 右键菜单选档位     · Ctrl+滚轮 微调不透明度
  · 顶部按钮:图钉=置顶  ≡=菜单  ↻=刷新  ✕=退出
  · 右键空白处 或 点 ≡ = 打开菜单(卡片显示 / 托盘显示 / 缩放 / 不透明度 / 重置显示 …)
  · 托盘图标:默认显示 5 小时用量(可在菜单切换);左键单击=显示/隐藏卡片
"""
from __future__ import annotations

import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------- 平台判定 ----------------
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WIN and not IS_MAC

# ---------------- DPI 自适应(仅 Windows 需手动处理;mac/Linux 由 Tk 自行缩放)----------------
SCALE = 1.0
if IS_WIN:
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:
            SCALE = ctypes.windll.user32.GetDpiForSystem() / 96.0
        except Exception:
            SCALE = 1.0
    except Exception:
        SCALE = 1.0

# ---------------- 字体(按平台挑第一个系统真正装了的中文字体)----------------
if IS_WIN:
    _FONT_CANDIDATES = ["Microsoft YaHei UI", "Microsoft YaHei", "SimHei"]
elif IS_MAC:
    _FONT_CANDIDATES = ["PingFang SC", "Hiragino Sans GB", "Heiti SC", "STHeiti", "Arial Unicode MS"]
else:
    _FONT_CANDIDATES = ["Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
                        "WenQuanYi Zen Hei", "Noto Sans CJK", "DejaVu Sans"]
FONT = _FONT_CANDIDATES[0]


def _resolve_font(root) -> None:
    """创建窗口后,从候选里挑第一个系统真正装了的字体;都没有就保持默认让 Tk 自行回退。"""
    global FONT
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families(root))
        for cand in _FONT_CANDIDATES:
            if cand in fams:
                FONT = cand
                return
    except Exception:
        pass

# ---------------- 配置 ----------------
REFRESH_INTERVAL = 60
API_URL_USAGE = "https://api.anthropic.com/api/oauth/usage"
API_URL_PROFILE = "https://api.anthropic.com/api/oauth/profile"
CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
CREDENTIALS = CONFIG_DIR / ".credentials.json"
FALLBACK_UA = "claude-code/2.1.85"
STATE_FILE = Path(__file__).with_name("card_state.json")

MIN_ZOOM, MAX_ZOOM = 0.6, 2.2
EDGE = 7  # 边缘缩放感应区(基准像素)
DEFAULT_TRAY_KEY = "five_hour"

BW, BPAD, BROW, BHEADER, BFOOTER, BRAD = 300, 15, 52, 34, 32, 15
TRANSPARENT = "#ff00ff"

C_CARD = "#1b1c20"
C_BORDER = "#2e3038"
C_TITLE = "#ECECEC"
C_SUB = "#9aa0aa"
C_DIM = "#6b7280"
C_TRACK = "#2a2c33"
C_ACCENT = "#c96442"
C_GREEN = "#46c46a"
C_AMBER = "#e0a23a"
C_RED = "#ef5350"

LABELS = {
    "five_hour": "5 小时",
    "seven_day": "7 天",
    "seven_day_opus": "7 天 · Opus",
    "seven_day_sonnet": "7 天 · Sonnet",
    "seven_day_cowork": "7 天 · Cowork",
    "seven_day_oauth_apps": "7 天 · 应用",
}
ORDER = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
         "seven_day_cowork", "seven_day_oauth_apps"]
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ================= 数据层(与 Claude Code 同源) =================
def read_token() -> str | None:
    # 1) 凭证文件:Windows / Linux 始终在此;macOS 若导出过文件也优先用它(官方 SSH 回退机制)
    try:
        creds = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
        tok = creds.get("claudeAiOauth", {}).get("accessToken")
        if tok:
            return tok
    except Exception:
        pass
    # 2) macOS:从登录钥匙串读取(Claude Code 在 mac 上默认存这里)
    if IS_MAC:
        return _read_token_macos_keychain()
    return None


def _read_token_macos_keychain() -> str | None:
    """mac 上 Claude Code 把凭证存进登录钥匙串的通用密码项,service 名为 'Claude Code-credentials'。"""
    import subprocess
    for service in ("Claude Code-credentials", "Claude Code"):  # 个别版本写入名不一致,两个都试
        try:
            p = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=8)
        except Exception:
            continue
        out = (p.stdout or "").strip()
        if p.returncode != 0 or not out:
            continue
        try:
            data = json.loads(out)
            tok = data.get("claudeAiOauth", {}).get("accessToken")
            if tok:
                return tok
        except Exception:
            # 个别版本钥匙串里存的可能直接就是 token 本身
            if out.startswith("ey") or len(out) > 40:
                return out
    return None


_ua_cache: str | None = None


def user_agent() -> str:
    global _ua_cache
    if _ua_cache:
        return _ua_cache
    ver = None
    try:
        import re
        import shutil
        import subprocess
        claude = shutil.which("claude")
        if claude:
            p = subprocess.run([claude, "--version"], capture_output=True, text=True,
                               timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            m = re.match(r"(\d+\.\d+\.\d+)", (p.stdout or "").strip())
            if m:
                ver = m.group(1)
    except Exception:
        pass
    _ua_cache = f"claude-code/{ver}" if ver else FALLBACK_UA
    return _ua_cache


def api_headers() -> dict | None:
    tok = read_token()
    if not tok:
        return None
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "User-Agent": user_agent(),
        "anthropic-beta": "oauth-2025-04-20",
    }


def fetch_usage() -> dict:
    h = api_headers()
    if not h:
        return {"error": "未找到登录凭证,请先在终端运行 claude 登录"}
    try:
        r = requests.get(API_URL_USAGE, headers=h, timeout=10)
        if r.status_code == 401:
            return {"error": "登录已过期,请运行 claude 重新登录"}
        if r.status_code == 429:
            return {"error": "请求过于频繁,稍后自动重试"}
        r.raise_for_status()
        return {"data": r.json()}
    except requests.ConnectionError:
        return {"error": "网络连接失败,重试中…"}
    except Exception as e:
        return {"error": f"获取失败:{type(e).__name__}"}


def fetch_profile() -> dict | None:
    h = api_headers()
    if not h:
        return None
    try:
        r = requests.get(API_URL_PROFILE, headers=h, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def extract_windows(data: dict | None):
    if not data:
        return []
    out = []
    for key, val in data.items():
        if (isinstance(val, dict) and val.get("utilization") is not None
                and val.get("resets_at")):
            out.append((key, float(val["utilization"]), val["resets_at"]))
    out.sort(key=lambda it: (ORDER.index(it[0]) if it[0] in ORDER else len(ORDER), it[0]))
    return out


def parse_dt(s: str):
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def fmt_reset_clock(resets_at: str) -> str:
    """按系统本地时区显示实际重置时刻,如 '今天 04:19' / '明天 09:00' / '06-17 周三 21:59'。"""
    dt = parse_dt(resets_at)
    if not dt:
        return "—"
    local = dt.astimezone()
    now = datetime.now(local.tzinfo)
    days = (local.date() - now.date()).days
    hm = local.strftime("%H:%M")
    if days <= 0:
        return f"今天 {hm}"
    if days == 1:
        return f"明天 {hm}"
    return f"{local.strftime('%m-%d')} {WEEK[local.weekday()]} {hm}"


def fmt_countdown(resets_at: str) -> str:
    dt = parse_dt(resets_at)
    if not dt:
        return "—"
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "即将重置"
    d, h, m, s = (int(delta // 86400), int(delta % 86400 // 3600),
                  int(delta % 3600 // 60), int(delta % 60))
    if d > 0:
        return f"还有 {d} 天 {h} 时"
    if h > 0:
        return f"还有 {h} 时 {m} 分"
    return f"还有 {m} 分 {s} 秒"


def bar_color(util: float) -> str:
    return C_RED if util >= 80 else (C_AMBER if util >= 50 else C_GREEN)


def _hex_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


# ================= 主程序 =================
class QuotaCard:
    _CURSORS = {"l": "sb_h_double_arrow", "r": "sb_h_double_arrow",
                "t": "sb_v_double_arrow", "b": "sb_v_double_arrow",
                "tl": "size_nw_se", "br": "size_nw_se",
                "tr": "size_ne_sw", "bl": "size_ne_sw"}

    def __init__(self):
        cfg = self._load_cfg()
        self.zoom = float(cfg.get("zoom", 1.0))
        self.alpha = float(cfg.get("alpha", 0.97))
        self.pinned = bool(cfg.get("pinned", True))
        self.hidden = set(cfg.get("hidden", []))
        self.reset_mode = cfg.get("reset_mode", "clock")
        self.tray_key = cfg.get("tray_key", DEFAULT_TRAY_KEY)

        self.root = tk.Tk()
        self.root.title("Claude 用量")
        _resolve_font(self.root)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", self.pinned)

        # 透明/圆角:按平台选最稳妥的方案
        #   Windows -> 洋红色键(-transparentcolor):真透明、圆角
        #   macOS   -> systemTransparent 背景:真透明、圆角
        #   Linux   -> 无色键,退化为不透明卡片(方角),仍可整体半透明
        # 设环境变量 QUOTA_CARD_OPAQUE=1 可在任意系统强制不透明(用于排查显示异常)。
        canvas_bg = C_CARD
        force_opaque = bool(os.environ.get("QUOTA_CARD_OPAQUE"))
        if not force_opaque and IS_WIN:
            try:
                self.root.attributes("-transparentcolor", TRANSPARENT)
                canvas_bg = TRANSPARENT
            except Exception:
                canvas_bg = C_CARD
        elif not force_opaque and IS_MAC:
            try:
                self.root.configure(bg="systemTransparent")
                canvas_bg = "systemTransparent"
            except Exception:
                canvas_bg = C_CARD
        else:
            try:
                self.root.configure(bg=C_CARD)
            except Exception:
                pass
            canvas_bg = C_CARD
        try:
            self.root.attributes("-alpha", self.alpha)
        except Exception:
            pass

        self._cur_w = self._cur_h = -1
        self.canvas = tk.Canvas(self.root, bg=canvas_bg, highlightthickness=0,
                                width=self.s(BW), height=self.s(120))
        self.canvas.pack(fill="both", expand=True)
        self._place_window(cfg)

        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", lambda *_: self.open_menu())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda *_: self._set_cursor(""))
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.root.bind("<MouseWheel>", self._on_wheel)

        self.state = {"data": None, "error": "正在加载…", "updated": 0, "profile": None}
        self._buttons = []
        self._pending = None
        self._drag = None
        self._resize = None
        self._cursor = None
        self._moved = False
        self._hidden_card = False
        self._refresh_evt = threading.Event()
        self._stop = False
        self.cmd_q: queue.Queue = queue.Queue()

        self.tray = None
        self._tray_sig = None
        self._setup_tray()

        threading.Thread(target=self._worker, daemon=True).start()
        self._poll_cmds()
        self._tick()
        self.root.mainloop()

    # ---- 缩放助手 ----
    def s(self, v: float) -> int:
        return int(round(v * SCALE * self.zoom))

    def f(self, px: int, weight: str = "normal"):
        return (FONT, -int(round(px * SCALE * self.zoom)), weight)

    # ---- 后台轮询 ----
    def _worker(self):
        first = True
        profile = None
        while not self._stop:
            res = fetch_usage()
            now = time.time()
            if "data" in res:
                if first:
                    profile = fetch_profile()
                    first = False
                self.state = {"data": res["data"], "error": None, "updated": now, "profile": profile}
            else:
                old = self.state
                self.state = {"data": old.get("data"), "error": res["error"],
                              "updated": old.get("updated", 0), "profile": profile}
            self._refresh_evt.wait(REFRESH_INTERVAL)
            self._refresh_evt.clear()

    def _tick(self):
        if self._stop:
            return
        if not self._hidden_card:
            self.render()
        self.root.after(1000, self._tick)

    def _poll_cmds(self):
        if self._stop:
            return
        try:
            while True:
                cmd, payload = self.cmd_q.get_nowait()
                if cmd == "toggle_card":
                    self.toggle_card()
                elif cmd == "refresh":
                    self.refresh_now()
                elif cmd == "quit":
                    self.quit()
                elif cmd == "menu_at":
                    # 托盘右键(原生 SNI 的 ContextMenu):在桌面给出的屏幕坐标处弹出菜单
                    self.open_menu(payload)
        except queue.Empty:
            pass
        if not self._stop:
            self.root.after(120, self._poll_cmds)

    # ---- 绘制 ----
    def render(self):
        st = self.state
        cv = self.canvas
        cv.delete("all")
        self._buttons = []

        PAD, HEADER, ROW, FOOTER, RAD = self.s(BPAD), self.s(BHEADER), self.s(BROW), self.s(BFOOTER), self.s(BRAD)
        W = self.s(BW)
        avail = extract_windows(st.get("data"))
        shown = [w for w in avail if w[0] not in self.hidden]
        n = max(len(shown) if shown else 1, 1)
        H = PAD + HEADER + n * ROW + FOOTER

        if (W, H) != (self._cur_w, self._cur_h):
            self._cur_w, self._cur_h = W, H
            cv.config(width=W, height=H)
            self.root.geometry(f"{W}x{H}+{self.root.winfo_x()}+{self.root.winfo_y()}")

        round_rect(cv, self.s(1), self.s(1), W - self.s(1), H - self.s(1), RAD, fill=C_CARD, outline=C_BORDER)

        # 头部
        hy = PAD + self.s(11)
        cv.create_oval(PAD, hy - self.s(4), PAD + self.s(8), hy + self.s(4), fill=C_ACCENT, outline="")
        tid = cv.create_text(PAD + self.s(16), hy, text="Claude 用量", anchor="w",
                             fill=C_TITLE, font=self.f(15, "bold"))
        badge = self._plan_badge(st.get("profile"))
        if badge:
            bb = cv.bbox(tid)
            bx1, bw = bb[2] + self.s(8), self.s(36)
            round_rect(cv, bx1, hy - self.s(8), bx1 + bw, hy + self.s(8), self.s(7), fill=C_ACCENT, outline="")
            cv.create_text(bx1 + bw / 2, hy, text=badge, fill="#ffffff", font=self.f(9, "bold"))
        # 顶部按钮(统一尺寸 + 统一线条风格,从右往左):关闭 / 刷新 / 菜单 / 图钉
        ri = self.s(7)
        sw = max(2, int(round(1.7 * SCALE * self.zoom)))
        cx = W - PAD - self.s(6)
        for kind, cb in (("close", self.quit), ("refresh", self.refresh_now),
                         ("menu", self.open_menu), ("top", self.toggle_pin)):
            col = (C_ACCENT if self.pinned else C_SUB) if kind == "top" else C_SUB
            self._draw_icon(cv, kind, cx, hy, ri, sw, col)
            r = self.s(11)
            self._buttons.append((cx - r, hy - r, cx + r, hy + r, cb))
            cx -= self.s(23)
        cv.create_line(PAD, PAD + HEADER - self.s(8), W - PAD, PAD + HEADER - self.s(8), fill=C_BORDER)

        # 行
        y = PAD + HEADER
        if not avail:
            cv.create_text(W / 2, y + ROW / 2, text=(st.get("error") or "暂无数据"),
                           fill=C_SUB, font=self.f(11), width=W - 2 * PAD, justify="center")
        elif not shown:
            cv.create_text(W / 2, y + ROW / 2, text="全部已隐藏 · 右键 → 卡片显示",
                           fill=C_DIM, font=self.f(10), justify="center")
        else:
            for key, util, resets_at in shown:
                self._draw_row(cv, y, key, util, resets_at, W, PAD)
                y += ROW

        self._draw_footer(cv, W, H, PAD, st)
        self._update_tray(avail)

    def _draw_row(self, cv, y, key, util, resets_at, W, PAD):
        col = bar_color(util)
        cy1 = y + self.s(10)
        cv.create_text(PAD, cy1, text=LABELS.get(key, key), anchor="w", fill=C_SUB, font=self.f(12))
        cv.create_text(W - PAD, cy1, text=f"{util:.0f}%", anchor="e", fill=col, font=self.f(16, "bold"))
        by, bx2 = y + self.s(22), W - PAD
        round_rect(cv, PAD, by, bx2, by + self.s(8), self.s(4), fill=C_TRACK, outline="")
        fw = (bx2 - PAD) * max(0.0, min(util, 100.0)) / 100.0
        if fw >= self.s(8):
            round_rect(cv, PAD, by, PAD + fw, by + self.s(8), self.s(4), fill=col, outline="")
        elif fw > 0:
            cv.create_rectangle(PAD, by, PAD + fw, by + self.s(8), fill=col, outline="")
        cy3 = y + self.s(42)
        if self.reset_mode == "count":
            cv.create_text(PAD, cy3, text="重置", anchor="w", fill=C_DIM, font=self.f(10))
            cv.create_text(W - PAD, cy3, text=fmt_countdown(resets_at), anchor="e", fill=C_DIM, font=self.f(10))
        else:
            cv.create_text(PAD, cy3, text="重置于", anchor="w", fill=C_DIM, font=self.f(10))
            cv.create_text(W - PAD, cy3, text=fmt_reset_clock(resets_at), anchor="e", fill=C_SUB, font=self.f(10))

    def _draw_footer(self, cv, W, H, PAD, st):
        fy = H - PAD + self.s(2)
        data, err, upd = st.get("data"), st.get("error"), st.get("updated", 0)
        if err and not data:
            dot, text = C_RED, "未连接"
        else:
            ago = int(time.time() - upd) if upd else 0
            text = "刚刚更新" if ago < 5 else (f"{ago} 秒前更新" if ago < 60 else f"{ago // 60} 分前更新")
            dot, text = (C_AMBER, text + " · 可能过期") if err else (C_GREEN, text)
        cv.create_oval(PAD, fy - self.s(3), PAD + self.s(6), fy + self.s(3), fill=dot, outline="")
        cv.create_text(PAD + self.s(12), fy, text=text, anchor="w", fill=C_DIM, font=self.f(10))
        cv.create_text(W - PAD, fy, text=datetime.now().strftime("%H:%M:%S"),
                       anchor="e", fill=C_DIM, font=self.f(10))

    def _draw_icon(self, cv, kind, cx, cy, ri, sw, col):
        """统一尺寸 / 统一线条风格(2px 圆头描边)的矢量图标。"""
        if kind == "close":
            d = ri * 0.62
            cv.create_line(cx - d, cy - d, cx + d, cy + d, fill=col, width=sw, capstyle="round")
            cv.create_line(cx - d, cy + d, cx + d, cy - d, fill=col, width=sw, capstyle="round")
        elif kind == "menu":
            d, g = ri * 0.66, ri * 0.46
            for yo in (-g, 0.0, g):
                cv.create_line(cx - d, cy + yo, cx + d, cy + yo, fill=col, width=sw, capstyle="round")
        elif kind == "refresh":
            R = ri * 0.66
            cv.create_arc(cx - R, cy - R, cx + R, cy + R, start=75, extent=250,
                          style="arc", outline=col, width=sw)
            a = math.radians(75)                       # 弧起点(顶部)
            ex, ey = cx + R * math.cos(a), cy - R * math.sin(a)
            tx, ty = math.sin(a), math.cos(a)          # 顺时针切线方向
            nx, ny = -ty, tx
            t = ri * 0.55
            cv.create_polygon(ex + tx * t, ey + ty * t,
                              ex + nx * t * 0.7, ey + ny * t * 0.7,
                              ex - nx * t * 0.7, ey - ny * t * 0.7,
                              fill=col, outline="")
        else:  # top —— 纯色三角形 ▲(置顶=橙色,取消=灰色)
            cv.create_polygon(cx, cy - ri * 0.66,
                              cx - ri * 0.66, cy + ri * 0.56,
                              cx + ri * 0.66, cy + ri * 0.56,
                              fill=col, outline="")

    # ---- 托盘 ----
    def _setup_tray(self):
        # Linux:优先用原生 StatusNotifierItem(见 tray_sni.py)。桌面在左键时调用 Activate、
        # 右键时调用 ContextMenu、中键调用 SecondaryActivate,均由应用直接接管——
        # 因此左键单击=显示/隐藏卡片、右键=弹菜单、中键=立即刷新,行为与 Windows 一致,
        # 且不依赖 pystray 在 KDE/Wayland 下不可靠的点击转发。
        # 注册失败(桌面无 SNI 宿主)时,回退到 pystray 的原有交互。
        if IS_LINUX:
            try:
                from tray_sni import SNITray
                self.tray = SNITray(
                    "claude_quota", self._tray_image(0, (70, 196, 106)), "Claude 用量",
                    on_activate=lambda x, y: self.cmd_q.put(("toggle_card", None)),
                    on_context=lambda x, y: self.cmd_q.put(("menu_at", (x, y))),
                    on_secondary=lambda x, y: self.cmd_q.put(("refresh", None)),
                )
                threading.Thread(target=self.tray.run, daemon=True).start()
                self.tray.wait_ready()
                return
            except Exception:
                self.tray = None  # 原生 SNI 不可用,继续尝试 pystray
        try:
            import pystray
            import PIL
            _ = PIL.__version__
        except Exception:
            self.tray = None
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示/隐藏卡片", lambda *_: self.cmd_q.put(("toggle_card", None)), default=True),
            pystray.MenuItem("立即刷新", lambda *_: self.cmd_q.put(("refresh", None))),
            pystray.MenuItem("退出", lambda *_: self.cmd_q.put(("quit", None))),
        )
        self.tray = pystray.Icon("claude_quota", self._tray_image(0, (70, 196, 106)), "Claude 用量", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _tray_image(self, util, rgb):
        from PIL import Image, ImageDraw
        # 用 128px 高分辨率绘制,交给托盘缩小显示后数字更锐利清晰
        sz = 128
        pad = 4
        img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # 圆角底色块几乎铺满整张图,外圈用量色描边(绿/黄/红随档位变化)
        d.rounded_rectangle([pad, pad, sz - pad - 1, sz - pad - 1], radius=int(sz * 0.22),
                            fill=(27, 28, 32, 255), outline=tuple(rgb) + (255,), width=int(sz * 0.06))
        txt = f"{int(round(util))}%"
        # 自适应字号:在描边内尽量放大百分比数字,让托盘里一眼可读(放宽留白以获得更大字号)
        max_w, max_h = int(sz * 0.84), int(sz * 0.78)
        font = self._load_font(int(sz * 0.4))
        for size in range(int(sz * 0.66), 16, -2):
            cand = self._load_font(size)
            bb = d.textbbox((0, 0), txt, font=cand)
            if bb[2] - bb[0] <= max_w and bb[3] - bb[1] <= max_h:
                font = cand
                break
        bb = d.textbbox((0, 0), txt, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        d.text(((sz - tw) / 2 - bb[0], (sz - th) / 2 - bb[1]), txt, font=font, fill=tuple(rgb) + (255,))
        return img

    @staticmethod
    def _load_font(size):
        """挑一个真正存在的、可按 size 缩放的粗体字体来画托盘里的百分比数字。

        早期实现只尝试 Windows 字体路径,在 Linux/mac 上全部失败后会退到 PIL 自带的
        位图默认字体,而那个字体不随 size 放大,导致托盘数字恒为约 11px 的小字、看不清。
        这里补齐各平台常见的粗体 TTF,并以 Pillow>=10.1 可缩放的 load_default(size) 兜底。
        """
        from PIL import ImageFont
        candidates = (
            # Linux:Fedora 默认 Noto;其余发行版常见 DejaVu / Liberation
            "/usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
            # macOS
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial Bold.ttf",
            # Windows
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            # 仅给文件名:交给 PIL 在自身字体搜索路径里定位
            "DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf",
        )
        for fp in candidates:
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
        # 兜底:Pillow>=10.1 的 load_default 接受 size 参数,返回可缩放的内置 TrueType 字体
        try:
            return ImageFont.load_default(size)
        except TypeError:
            return ImageFont.load_default()

    def _update_tray(self, avail):
        if not self.tray or not avail:
            return
        sel = next((w for w in avail if w[0] == self.tray_key), avail[0])
        key, util, _ = sel
        rgb = _hex_rgb(bar_color(util))
        sig = (key, int(round(util)), rgb)
        if sig == self._tray_sig:
            return
        self._tray_sig = sig
        try:
            self.tray.icon = self._tray_image(util, rgb)
            self.tray.title = (f"Claude 用量(托盘:{LABELS.get(key, key)})\n"
                               + "  ".join(f"{LABELS.get(k, k)} {u:.0f}%" for k, u, _ in avail))
        except Exception:
            pass

    # ---- 边缘缩放 ----
    def _region(self, x, y):
        W, H = self._cur_w, self._cur_h
        if W <= 0 or H <= 0:
            return ""
        m = self.s(EDGE)
        left, right, top, bot = x <= m, x >= W - m, y <= m, y >= H - m
        if top and left:
            return "tl"
        if top and right:
            return "tr"
        if bot and left:
            return "bl"
        if bot and right:
            return "br"
        if left:
            return "l"
        if right:
            return "r"
        if top:
            return "t"
        if bot:
            return "b"
        return ""

    def _set_cursor(self, region):
        if region == self._cursor:
            return
        self._cursor = region
        name = self._CURSORS.get(region, "arrow")
        for cand in (name, "sizing", "arrow"):
            try:
                self.canvas.config(cursor=cand)
                return
            except tk.TclError:
                continue

    def _on_motion(self, e):
        if self._resize or self._drag:
            return
        self._set_cursor(self._region(e.x, e.y))

    def _do_resize(self, e):
        rs = self._resize
        if rs is None:
            return
        reg = rs["region"]
        if "l" in reg:
            newdim, base = rs["w"] - (e.x_root - rs["mx"]), rs["w"]
        elif "r" in reg:
            newdim, base = rs["w"] + (e.x_root - rs["mx"]), rs["w"]
        elif reg == "t":
            newdim, base = rs["h"] - (e.y_root - rs["my"]), rs["h"]
        else:  # "b"
            newdim, base = rs["h"] + (e.y_root - rs["my"]), rs["h"]
        if base <= 0:
            return
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, rs["zoom"] * newdim / base))
        self.render()
        nx = (rs["x"] + rs["w"] - self._cur_w) if "l" in reg else rs["x"]
        ny = (rs["y"] + rs["h"] - self._cur_h) if "t" in reg else rs["y"]
        self.root.geometry(f"+{nx}+{ny}")

    # ---- 交互 ----
    def _on_press(self, e):
        for x1, y1, x2, y2, cb in self._buttons:
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                self._pending, self._drag, self._resize = cb, None, None
                return
        self._pending = None
        region = self._region(e.x, e.y)
        if region:
            self._resize = {"region": region, "mx": e.x_root, "my": e.y_root,
                            "zoom": self.zoom, "w": self._cur_w, "h": self._cur_h,
                            "x": self.root.winfo_x(), "y": self.root.winfo_y()}
            self._drag = None
            return
        self._moved = False
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _on_drag(self, e):
        if self._resize:
            self._do_resize(e)
        elif self._drag:
            sx, sy, ox, oy = self._drag
            self.root.geometry(f"+{ox + e.x_root - sx}+{oy + e.y_root - sy}")
            self._moved = True

    def _on_release(self, e):
        if self._resize:
            self._resize = None
            self._save_cfg()
            return
        if self._pending:
            for x1, y1, x2, y2, cb in self._buttons:
                if x1 <= e.x <= x2 and y1 <= e.y <= y2 and cb is self._pending:
                    cb()
                    break
            self._pending = None
        elif self._drag and self._moved:
            self._save_cfg()
        self._drag = None

    def _on_wheel(self, e):
        if e.state & 0x0004:  # Ctrl → 微调不透明度
            self.set_alpha(self.alpha + (0.02 if e.delta > 0 else -0.02))
        else:                 # 滚轮 → 缩放
            self.set_zoom(self.zoom * (1.08 if e.delta > 0 else 1 / 1.08))

    # ---- 菜单 ----
    def open_menu(self, at=None):
        # at 给定时按该屏幕坐标弹出(托盘右键场景);否则跟随鼠标当前位置(卡片内点击场景)
        self._build_menu()
        if at is not None:
            x, y = int(at[0]), int(at[1])
        else:
            x, y = self.root.winfo_pointerxy()
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="立即刷新", command=self.refresh_now)
        avail = extract_windows(self.state.get("data"))

        # 卡片显示(勾选要显示哪几个)
        sub = tk.Menu(m, tearoff=0)
        if avail:
            self._show_vars = {}
            for key, _, _ in avail:
                var = tk.BooleanVar(value=key not in self.hidden)
                self._show_vars[key] = var
                sub.add_checkbutton(label=str(LABELS.get(key, key)), variable=var,
                                    command=lambda k=key: self._toggle_show(k))
        else:
            sub.add_command(label="(暂无数据)", state="disabled")
        m.add_cascade(label="卡片显示", menu=sub)

        # 托盘显示(单选托盘图标显示哪个)
        if self.tray:
            tm = tk.Menu(m, tearoff=0)
            self._tray_var = tk.StringVar(value=self.tray_key)
            if avail:
                for key, _, _ in avail:
                    tm.add_radiobutton(label=str(LABELS.get(key, key)), value=key, variable=self._tray_var,
                                       command=lambda k=key: self.set_tray_key(k))
            else:
                tm.add_command(label="(暂无数据)", state="disabled")
            m.add_cascade(label="托盘显示", menu=tm)

        # 缩放
        zm = tk.Menu(m, tearoff=0)
        self._zoom_var = tk.DoubleVar(value=round(self.zoom, 2))
        for label, z in (("60%", 0.6), ("80%", 0.8), ("100%", 1.0), ("120%", 1.2),
                         ("140%", 1.4), ("160%", 1.6), ("180%", 1.8), ("200%", 2.0)):
            zm.add_radiobutton(label=label, value=z, variable=self._zoom_var, command=lambda zz=z: self.set_zoom(zz))
        m.add_cascade(label="缩放", menu=zm)

        # 不透明度
        om = tk.Menu(m, tearoff=0)
        self._alpha_var = tk.IntVar(value=int(round(self.alpha * 100)))
        for p in (100, 95, 90, 85, 80, 75, 70, 65, 60, 50):
            om.add_radiobutton(label=f"{p}%", value=p, variable=self._alpha_var, command=lambda v=p: self.set_alpha(v / 100))
        m.add_cascade(label="不透明度", menu=om)

        # 重置显示
        rm = tk.Menu(m, tearoff=0)
        self._reset_var = tk.StringVar(value=self.reset_mode)
        rm.add_radiobutton(label="重置时刻(系统时区)", value="clock", variable=self._reset_var,
                           command=lambda: self.set_reset_mode("clock"))
        rm.add_radiobutton(label="倒计时", value="count", variable=self._reset_var,
                           command=lambda: self.set_reset_mode("count"))
        m.add_cascade(label="重置显示", menu=rm)

        m.add_separator()
        self._pin_var = tk.BooleanVar(value=self.pinned)
        m.add_checkbutton(label="窗口置顶", variable=self._pin_var, command=self.toggle_pin)
        if self.tray:
            m.add_command(label="隐藏到托盘", command=self.toggle_card)
        m.add_command(label="退出", command=self.quit)
        self.menu = m

    # ---- 动作 ----
    def refresh_now(self):
        self._refresh_evt.set()

    def set_zoom(self, z):
        z = max(MIN_ZOOM, min(MAX_ZOOM, z))
        if abs(z - self.zoom) < 1e-3:
            return
        self.zoom = z
        self.render()
        self._save_cfg()

    def set_alpha(self, v):
        self.alpha = max(0.4, min(1.0, v))
        self.root.attributes("-alpha", self.alpha)
        self._save_cfg()

    def set_reset_mode(self, mode):
        self.reset_mode = mode
        self.render()
        self._save_cfg()

    def set_tray_key(self, key):
        self.tray_key = key
        self._tray_sig = None
        self.render()
        self._save_cfg()

    def _toggle_show(self, key):
        self.hidden.discard(key) if key in self.hidden else self.hidden.add(key)
        self.render()
        self._save_cfg()

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.root.attributes("-topmost", self.pinned)
        self.render()
        self._save_cfg()

    def toggle_card(self):
        if self._hidden_card:
            self.root.deiconify()
            self.root.attributes("-topmost", self.pinned)
            self._hidden_card = False
            self.render()
        else:
            self.root.withdraw()
            self._hidden_card = True

    def quit(self):
        self._stop = True
        self._save_cfg()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---- 工具 ----
    @staticmethod
    def _plan_badge(profile):
        if not profile:
            return None
        acc = profile.get("account", {})
        if acc.get("has_claude_max"):
            return "MAX"
        if acc.get("has_claude_pro"):
            return "PRO"
        return None

    def _place_window(self, cfg):
        x, y = cfg.get("x"), cfg.get("y")
        if x is None or y is None:
            x, y = self.root.winfo_screenwidth() - self.s(BW) - self.s(24), self.s(48)
        self.root.geometry(f"+{int(x)}+{int(y)}")

    def _load_cfg(self):
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cfg(self):
        try:
            STATE_FILE.write_text(json.dumps({
                "x": self.root.winfo_x(), "y": self.root.winfo_y(),
                "alpha": round(self.alpha, 3), "zoom": round(self.zoom, 3),
                "pinned": self.pinned, "hidden": sorted(self.hidden),
                "reset_mode": self.reset_mode, "tray_key": self.tray_key,
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


# ================= 自检 =================
def run_check():
    rec = getattr(sys.stdout, "reconfigure", None)
    if rec:
        try:
            rec(encoding="utf-8")
        except Exception:
            pass
    print("凭证文件:", CREDENTIALS, "存在" if CREDENTIALS.exists() else "不存在")
    print("Token:", "已读取" if read_token() else "未读取到")
    print("User-Agent:", user_agent())
    res = fetch_usage()
    if "error" in res:
        print("结果: 错误 ->", res["error"])
        return
    for key, util, resets_at in extract_windows(res["data"]):
        print(f"  {LABELS.get(key, key):<14} {util:5.1f}%   重置于 {fmt_reset_clock(resets_at)}")
    prof = fetch_profile()
    if prof:
        acc = prof.get("account", {})
        print("账号:", acc.get("display_name"), "| Max:", acc.get("has_claude_max"))


_SINGLETON_HANDLE = None


def acquire_single_instance() -> bool:
    """单实例保护:Windows 用命名互斥量,mac/Linux 用文件锁;占用成功返回 True。"""
    global _SINGLETON_HANDLE
    try:
        if IS_WIN:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateMutexW(None, False, "ClaudeQuotaCardSingletonMutex")
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                return False
            _SINGLETON_HANDLE = handle  # 保持引用,进程存活期间一直持有
            return True
        # mac / Linux:对配置目录下的锁文件加排他非阻塞锁
        import fcntl
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            lock_path = CONFIG_DIR / ".quota_card.lock"
        except Exception:
            lock_path = Path.home() / ".quota_card.lock"
        f = open(lock_path, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]  # fcntl 仅 Unix
        except OSError:
            return False  # 已有实例持锁
        _SINGLETON_HANDLE = f  # 保持文件对象存活,进程退出自动释放锁
        return True
    except Exception:
        return True  # 出错时不阻止启动


if __name__ == "__main__":
    if "--check" in sys.argv:
        run_check()
    elif not acquire_single_instance():
        sys.exit(0)  # 已有实例,直接退出,不重复弹卡片
    else:
        QuotaCard()
