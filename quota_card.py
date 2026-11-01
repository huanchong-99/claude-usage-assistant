# -*- coding: utf-8 -*-
"""
Claude / Codex 实时限额卡片  (quota_card.py)
============================================
常驻桌面、置顶、卡片式小部件,实时显示 AI 编码助手的额度使用情况,
并按"系统本地时区"显示每个窗口的实际重置时刻。附带系统托盘图标(macOS 为菜单栏)。
支持两个数据源,可在菜单"数据源"里同时显示或只选其一:

  · Claude —— GET https://api.anthropic.com/api/oauth/usage(与 Claude Code 同源)
      额度在服务端按账号统计:CLI / 桌面版 / 网页的用量都计入同一份额度。
      凭证:~/.claude/.credentials.json(macOS 亦支持登录钥匙串)或 Claude 桌面版的登录缓存。
      accessToken 过期时自动用 refreshToken 续期并写回,不再依赖终端登录刷新。
  · Codex —— GET https://chatgpt.com/backend-api/wham/usage(与 Codex CLI 同源)
      凭证:~/.codex/auth.json(ChatGPT 登录模式);同样自动续期并写回。

凭证只放进 HTTP Authorization 头、只发往各自官方域名,绝不上传到任何其它地方。

界面:Python 自带 tkinter;托盘:pystray + Pillow(缺失时自动降级为仅卡片)。

操作:
  · 拖动卡片移动(记忆位置)
  · 滚轮缩放 / 拖动窗口边缘缩放 / 右键菜单选档位
  · Ctrl+滚轮 微调不透明度(macOS 上 ⌘+滚轮 亦可)
  · 顶部按钮:▲=置顶  ≡=菜单  ↻=刷新  ✕=退出
  · 右键空白处 或 点 ≡ = 打开菜单(macOS:双指轻点 / Control+单击 亦可)
  · 托盘图标:默认显示 Claude 5 小时用量(可在菜单切换);左键单击=显示/隐藏卡片
    (Linux 原生托盘:中键=立即刷新;macOS:菜单栏直接显示彩色百分比,点击弹出菜单)

命令行:
  --check            不开界面,只拉一次数据并打印诊断信息
  --autostart on|off 开启 / 关闭开机自启(Windows 注册表 Run / macOS LaunchAgent / Linux autostart)
  --install-app      创建应用入口:macOS 为 ~/Applications 里的 .app,Linux 为应用菜单启动项
  --uninstall-app    移除应用入口
"""
from __future__ import annotations

import functools
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
ERROR_RETRY_INTERVAL = 15    # 网络失败(如刚从睡眠唤醒、Wi-Fi 未连上)后的快速重试间隔(秒)
REFRESH_RETRY_COOLDOWN = 60  # token 续期失败后的最小重试间隔(秒)
KEYCHAIN_CACHE_TTL = 300     # mac 钥匙串凭证在内存里的缓存时长(秒),避免每分钟起一次 security 子进程

# Claude(与 Claude Code CLI 同源的官方接口与 OAuth 客户端)
API_URL_USAGE = "https://api.anthropic.com/api/oauth/usage"
API_URL_PROFILE = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_TOKEN_URLS = ("https://platform.claude.com/v1/oauth/token",
                     "https://console.anthropic.com/v1/oauth/token")
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
CREDENTIALS = CONFIG_DIR / ".credentials.json"
FALLBACK_UA = "claude-code/2.1.85"

# Codex(与 Codex CLI 同源的官方接口与 OAuth 客户端)
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
CODEX_AUTH = CODEX_HOME / "auth.json"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_UA = "codex-cli"

STATE_FILE = Path(__file__).with_name("card_state.json")

# 开机自启的标识(Windows 注册表值名 / macOS LaunchAgent Label / Linux .desktop 文件名)
AUTOSTART_NAME = "AIUsageCard"
LAUNCH_AGENT_LABEL = "io.github.huanchong-99.ai-usage-card"
_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

MIN_ZOOM, MAX_ZOOM = 0.6, 2.2
EDGE = 7  # 边缘缩放感应区(基准像素)
DEFAULT_TRAY_KEY = "five_hour"

# 滚轮:Windows 按 delta 累计到 120 走一档(兼容高精度滚轮/触控板);
# mac/Linux 的 Tk 事件不带统一刻度(mac 触控板一次滑动会连发几十个事件),按时间节流。
WHEEL_ZOOM_STEP = 1.05 if IS_MAC else 1.08
WHEEL_MIN_GAP = 0.06 if IS_MAC else 0.03
WHEEL_ALPHA_MODS = 0x0004 | (0x0008 if IS_MAC else 0)  # Control;mac 上 ⌘(Tk 的 Mod1)也行

BW, BPAD, BROW, BHEADER, BFOOTER, BRAD = 300, 15, 52, 34, 32, 15
BSEC, BMSG = 26, 36  # 分节标题高度 / 单行提示高度(基准像素)
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

PROVIDERS = ("claude", "codex")
PROVIDER_TITLES = {"claude": "Claude", "codex": "Codex"}
PROVIDER_DOTS = {"claude": C_ACCENT, "codex": "#10a37f"}
CLAUDE_PLAN_BADGES = {"max": "MAX", "pro": "PRO", "team": "TEAM", "enterprise": "ENT"}
CODEX_PLAN_BADGES = {"free": "FREE", "plus": "PLUS", "pro": "PRO", "prolite": "PRO LITE",
                     "team": "TEAM", "business": "BIZ", "enterprise": "ENT", "edu": "EDU"}

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

NET_ERR = "网络连接失败,重试中…"


# ================= 通用工具 =================
def _atomic_write_json(path: Path, obj) -> None:
    """先写临时文件再原子替换,避免并发写坏凭证文件;POSIX 上收紧权限。"""
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    if not IS_WIN:
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
    os.replace(tmp, path)


def _jwt_exp(tok: str) -> float | None:
    """读 JWT 的 exp 声明(不校验签名,只用来判断是否临期)。"""
    try:
        import base64
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return float(json.loads(base64.urlsafe_b64decode(seg))["exp"])
    except Exception:
        return None


_sessions: dict = {}
_sessions_lock = threading.Lock()


def _http(name: str) -> requests.Session:
    """每个数据源一个长连接 Session:复用 TCP/TLS 连接,省去每次刷新的握手。
       仅对幂等的 GET 做一次自动重试(复用连接恰好被服务端关闭时);续期用的 POST 绝不重试,
       以免 refreshToken 轮换后被重复提交。"""
    with _sessions_lock:
        s = _sessions.get(name)
        if s is None:
            s = requests.Session()
            try:
                from requests.adapters import HTTPAdapter
                from urllib3.util.retry import Retry
                retry = Retry(total=2, connect=2, read=1, status=0, backoff_factor=0.5,
                              allowed_methods=frozenset({"GET"}), raise_on_status=False)
                s.mount("https://", HTTPAdapter(max_retries=retry))
            except Exception:
                pass  # urllib3 过旧等情况:用默认适配器即可
            _sessions[name] = s
        return s


def _run_parallel(funcs: dict):
    """并发执行 {key: fn},按完成先后逐个 yield (key, result)。用守护线程,退出时不被卡住。"""
    q: queue.Queue = queue.Queue()

    def run(k, fn):
        try:
            res = fn()
        except Exception as e:  # 兜底:任何未预期异常都转成错误文案,不让后台线程挂掉
            res = {"error": f"获取失败:{type(e).__name__}"}
        q.put((k, res))

    for k, fn in funcs.items():
        threading.Thread(target=run, args=(k, fn), daemon=True, name=f"fetch-{k}").start()
    for _ in funcs:
        yield q.get()


_ua_cache: str | None = None


def _find_claude_bin() -> str | None:
    """找 claude 可执行文件。从登录项 / LaunchAgent / 访达双击启动时 PATH 很短
       (只有 /usr/bin:/bin…),所以额外查一遍常见安装位置。"""
    import shutil
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for cand in (home / ".local" / "bin" / "claude", home / ".claude" / "local" / "claude",
                 Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude"),
                 home / ".npm-global" / "bin" / "claude", home / ".bun" / "bin" / "claude"):
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        except OSError:
            continue
    return None


def _claude_version_fast(path: str) -> str | None:
    """不启动 Node 就拿到版本号:原生安装的符号链接目标就是 …/versions/<版本>;
       npm 安装则读同目录树里 @anthropic-ai/claude-code 的 package.json。"""
    import re
    semver = re.compile(r"\d+\.\d+\.\d+")
    try:
        real = Path(os.path.realpath(path))
    except Exception:
        return None
    if semver.fullmatch(real.name):
        return real.name
    dirs = list(real.parents)[:4] + [Path(path).parent / "node_modules" / "@anthropic-ai" / "claude-code"]
    for d in dirs:
        try:
            meta = json.loads((d / "package.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        ver = str(meta.get("version") or "")
        if meta.get("name") == "@anthropic-ai/claude-code" and semver.fullmatch(ver):
            return ver
    return None


def user_agent() -> str:
    global _ua_cache
    if _ua_cache:
        return _ua_cache
    ver = None
    try:
        claude = _find_claude_bin()
        if claude:
            ver = _claude_version_fast(claude)
        if claude and not ver:  # 推断不出来才真的跑一次 claude --version(冷启动 Node 较慢)
            import re
            import subprocess
            p = subprocess.run([claude, "--version"], capture_output=True, text=True,
                               timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            m = re.match(r"(\d+\.\d+\.\d+)", (p.stdout or "").strip())
            if m:
                ver = m.group(1)
    except Exception:
        pass
    _ua_cache = f"claude-code/{ver}" if ver else FALLBACK_UA
    return _ua_cache


_APPKIT = 0  # 0=未尝试;None=不可用;否则为 AppKit 模块


def _appkit():
    """mac:按需导入 PyObjC 的 AppKit(装 pystray 时会随之安装);不可用返回 None。"""
    global _APPKIT
    if _APPKIT == 0:
        _APPKIT = None
        if IS_MAC:
            try:
                import AppKit  # type: ignore[import-not-found]
                _APPKIT = AppKit
            except Exception:
                pass
    return _APPKIT


# ================= Claude Desktop 常驻令牌(免终端登录) =================
# 桌面版(Claude Desktop / Cowork)会自己不断续期 OAuth 令牌,并用 Electron
# safeStorage 加密后存进其 config.json。只要读它,卡片就永远有一份"活着"的令牌,
# 无需终端 claude 登录——这正是"只用桌面版、CLI 令牌总过期"场景的根治办法。
# 全过程只读、best-effort:任何一步失败都静默跳过,回退到 CLI 凭证文件路径。
def _win_desktop_dirs() -> list[Path]:
    """Windows 上桌面版可能的数据目录:
         · 微软商店(MSIX)版:%LOCALAPPDATA%\\Packages\\Claude_<发布者哈希>\\LocalCache\\Roaming\\Claude
           (MSIX 把应用对 %APPDATA% 的写入重定向到包目录里,见 issue #2)
         · 经典安装包版:%APPDATA%\\Claude"""
    out: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        try:
            for pkg in sorted((Path(local) / "Packages").glob("Claude_*")):
                out.append(pkg / "LocalCache" / "Roaming" / "Claude")
        except OSError:
            pass
    base = os.environ.get("APPDATA")
    if base:
        out.append(Path(base) / "Claude")
    return out


def _desktop_dir() -> Path | None:
    if IS_WIN:
        dirs = _win_desktop_dirs()
        found = []
        for d in dirs:
            try:
                found.append(((d / "config.json").stat().st_mtime, d))
            except OSError:
                continue
        if found:  # 两种版本都装过时,取 config.json 最近更新的那个(正在用的那个)
            return max(found, key=lambda it: it[0])[1]
        return dirs[-1] if dirs else None
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "Claude"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "Claude"


def _win_dpapi_unprotect(blob: bytes) -> bytes | None:
    """CryptUnprotectData(纯 ctypes,无三方依赖)。"""
    import ctypes
    import ctypes.wintypes as wt

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    bi = BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob, len(blob)),
                                     ctypes.POINTER(ctypes.c_char)))
    bo = BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo)):
        return None
    try:
        buf = ctypes.create_string_buffer(bo.cbData)
        ctypes.memmove(buf, bo.pbData, bo.cbData)
        return buf.raw
    finally:
        ctypes.windll.kernel32.LocalFree(bo.pbData)


def _win_gcm_decrypt(key: bytes, nonce: bytes, ct: bytes, tag: bytes) -> bytes | None:
    """AES-256-GCM via Windows BCrypt(纯 ctypes,无三方依赖)。"""
    import ctypes
    import ctypes.wintypes as wt
    bcrypt = ctypes.windll.bcrypt

    class AUTH_INFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.ULONG), ("dwInfoVersion", wt.ULONG),
                    ("pbNonce", ctypes.c_void_p), ("cbNonce", wt.ULONG),
                    ("pbAuthData", ctypes.c_void_p), ("cbAuthData", wt.ULONG),
                    ("pbTag", ctypes.c_void_p), ("cbTag", wt.ULONG),
                    ("pbMacContext", ctypes.c_void_p), ("cbMacContext", wt.ULONG),
                    ("cbAAD", wt.ULONG), ("cbData", ctypes.c_ulonglong), ("dwFlags", wt.ULONG)]
    hAlg = ctypes.c_void_p()
    hKey = ctypes.c_void_p()
    try:
        if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(hAlg), "AES", None, 0) != 0:
            return None
        mode = "ChainingModeGCM".encode("utf-16-le") + b"\x00\x00"
        if bcrypt.BCryptSetProperty(hAlg, "ChainingMode", mode, len(mode), 0) != 0:
            return None
        if bcrypt.BCryptGenerateSymmetricKey(hAlg, ctypes.byref(hKey), None, 0, key, len(key), 0) != 0:
            return None
        ai = AUTH_INFO()
        ai.cbSize = ctypes.sizeof(ai)
        ai.dwInfoVersion = 1
        nb, tb = ctypes.create_string_buffer(nonce, len(nonce)), ctypes.create_string_buffer(tag, len(tag))
        ai.pbNonce, ai.cbNonce = ctypes.cast(nb, ctypes.c_void_p), len(nonce)
        ai.pbTag, ai.cbTag = ctypes.cast(tb, ctypes.c_void_p), len(tag)
        out = ctypes.create_string_buffer(len(ct))
        cb = wt.ULONG(0)
        if bcrypt.BCryptDecrypt(hKey, ct, len(ct), ctypes.byref(ai), None, 0, out, len(ct), ctypes.byref(cb), 0) != 0:
            return None
        return out.raw[:cb.value]
    except Exception:
        return None
    finally:
        if hKey:
            bcrypt.BCryptDestroyKey(hKey)
        if hAlg:
            bcrypt.BCryptCloseAlgorithmProvider(hAlg, 0)


def _gcm_decrypt(key: bytes, nonce: bytes, ct_tag: bytes) -> bytes | None:
    """AES-GCM:优先第三方库(若已装),否则用 Windows BCrypt。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(nonce, ct_tag, None)
    except ImportError:
        pass
    except Exception:
        return None
    try:
        from Crypto.Cipher import AES
        c = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return c.decrypt_and_verify(ct_tag[:-16], ct_tag[-16:])
    except ImportError:
        pass
    except Exception:
        return None
    if IS_WIN:
        return _win_gcm_decrypt(key, nonce, ct_tag[:-16], ct_tag[-16:])
    return None


_cccrypt = 0  # 0=未尝试;None=不可用;否则为 CCCrypt 函数


def _mac_cbc_decrypt(key: bytes, iv: bytes, ct: bytes) -> bytes | None:
    """AES-CBC + PKCS7 via macOS 系统自带 CommonCrypto(纯 ctypes,无三方依赖)。"""
    global _cccrypt
    import ctypes
    if _cccrypt == 0:
        _cccrypt = None
        for lib in ("/usr/lib/system/libcommonCrypto.dylib", "/usr/lib/libSystem.B.dylib"):
            try:
                fn = ctypes.CDLL(lib).CCCrypt
            except (OSError, AttributeError):
                continue
            # CCCryptorStatus CCCrypt(CCOperation op, CCAlgorithm alg, CCOptions options,
            #     const void *key, size_t keyLength, const void *iv,
            #     const void *dataIn, size_t dataInLength,
            #     void *dataOut, size_t dataOutAvailable, size_t *dataOutMoved);
            fn.restype = ctypes.c_int32
            fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
                           ctypes.c_char_p, ctypes.c_size_t,
                           ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
            _cccrypt = fn
            break
    if not _cccrypt:
        return None
    k_decrypt, k_aes, k_pkcs7 = 1, 0, 1  # kCCDecrypt / kCCAlgorithmAES / kCCOptionPKCS7Padding
    out = ctypes.create_string_buffer(len(ct) + 16)
    moved = ctypes.c_size_t(0)
    try:
        st = _cccrypt(k_decrypt, k_aes, k_pkcs7, key, len(key), iv, ct, len(ct),
                      out, len(out), ctypes.byref(moved))
    except Exception:
        return None
    return out.raw[:moved.value] if st == 0 else None


def _cbc_decrypt(key: bytes, iv: bytes, ct: bytes) -> bytes | None:
    """AES-128-CBC(mac/Linux 的 safeStorage 用)。
       依次尝试 cryptography → pycryptodome → macOS 系统 CommonCrypto(零依赖)。"""
    pt = None
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        pt = d.update(ct) + d.finalize()
    except ImportError:
        try:
            from Crypto.Cipher import AES
            pt = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
        except ImportError:
            pt = None
        except Exception:
            return None
    except Exception:
        return None
    if pt is None:  # 两个三方库都没装
        return _mac_cbc_decrypt(key, iv, ct) if IS_MAC else None
    if not pt:
        return None
    pad = pt[-1]  # 去 PKCS7 填充
    return pt[:-pad] if 1 <= pad <= 16 else pt


_ss_key_cache: dict = {}  # {数据目录: (key, algo) 或 None(尝试过但不可用)}


def _safestorage_key(d: Path):
    """返回 (aes_key, 'gcm'|'cbc') 或 None。密钥在一个进程内稳定,按数据目录缓存一次即可
       (避免每 60 秒刷新都读一次 Local State / mac 钥匙串反复弹授权框)。"""
    k = str(d)
    if k not in _ss_key_cache:
        _ss_key_cache[k] = _compute_safestorage_key(d)
    return _ss_key_cache[k]


def _compute_safestorage_key(d: Path):
    """Windows=DPAPI 解出的 32 字节 GCM 密钥;mac/Linux=PBKDF2 派生 16 字节 CBC 密钥。"""
    if not d:
        return None
    if IS_WIN:
        try:
            ls = json.loads((d / "Local State").read_text(encoding="utf-8"))
            import base64
            enc = base64.b64decode(ls["os_crypt"]["encrypted_key"])
            if enc[:5] != b"DPAPI":
                return None
            key = _win_dpapi_unprotect(enc[5:])
            return (key, "gcm") if key else None
        except Exception:
            return None
    # mac / Linux:PBKDF2-HMAC-SHA1(salt=b"saltysalt", dklen=16)
    try:
        import hashlib
        if IS_MAC:
            # 首次读取时 macOS 会弹钥匙串授权框;点"始终允许"后不再询问。拒绝则本进程不再尝试。
            import subprocess
            p = subprocess.run(["security", "find-generic-password", "-w", "-s", "Claude Safe Storage"],
                               capture_output=True, text=True, timeout=30)
            if p.returncode != 0 or not p.stdout.strip():
                return None
            pw, iters = p.stdout.strip().encode(), 1003
        else:  # Linux:无 libsecret 依赖时退回默认口令 "peanuts"
            pw, iters = b"peanuts", 1
        return (hashlib.pbkdf2_hmac("sha1", pw, b"saltysalt", iters, 16), "cbc")
    except Exception:
        return None


def _desktop_decrypt(b64_value: str, keyinfo) -> bytes | None:
    import base64
    try:
        blob = base64.b64decode(b64_value)
    except Exception:
        return None
    key, algo = keyinfo
    if blob[:3] in (b"v10", b"v11"):
        blob = blob[3:]
        if algo == "gcm":
            return _gcm_decrypt(key, blob[:12], blob[12:])
        return _cbc_decrypt(key, b" " * 16, blob)
    # 无版本前缀:Windows 上可能是旧版直接 DPAPI 加密的值
    if IS_WIN:
        return _win_dpapi_unprotect(blob)
    return None


_desk_cache: dict = {"stamp": None, "tokens": []}


def _claude_desktop_tokens() -> list[dict]:
    """读桌面版 config.json 里的 OAuth 令牌缓存,返回可用于 /api/oauth/usage 的候选:
       [{'token','expiresAt'(秒),'refreshToken','plan'}],按 api.anthropic.com + user:inference 过滤。只读。
       按文件 (mtime, size) 缓存解密结果:文件没变就不重复解析 + 解密。"""
    d = _desktop_dir()
    if not d:
        return []
    cfg_path = d / "config.json"
    try:
        st = cfg_path.stat()
    except OSError:
        return []
    stamp = (str(cfg_path), st.st_mtime_ns, st.st_size)
    if stamp != _desk_cache["stamp"]:
        _desk_cache["tokens"] = _read_desktop_tokens(cfg_path)
        _desk_cache["stamp"] = stamp
    return _desk_cache["tokens"]


def _read_desktop_tokens(cfg_path: Path) -> list[dict]:
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    encs = [cfg.get(k) for k in ("oauth:tokenCacheV2", "oauth:tokenCache")]
    encs = [e for e in encs if isinstance(e, str) and e]
    if not encs:  # 没有令牌缓存就别去取密钥(mac 上取密钥会弹钥匙串授权框)
        return []
    keyinfo = _safestorage_key(cfg_path.parent)
    if not keyinfo:
        return []
    merged: dict = {}
    for enc in encs:  # V2 在前,优先
        raw = _desktop_decrypt(enc, keyinfo)
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if isinstance(obj, dict):
            for k, v in obj.items():
                merged.setdefault(k, v)
    out = []
    for k, v in merged.items():
        if not isinstance(v, dict):
            continue
        tok = v.get("token") or v.get("accessToken")
        if not tok or "user:inference" not in k or "api.anthropic.com" not in k:
            continue
        exp = float(v.get("expiresAt") or 0)
        exp_s = exp / 1000.0 if exp > 1e12 else exp
        out.append({
            "token": tok,
            "expiresAt": exp_s,
            "refreshToken": v.get("refreshToken"),
            "plan": CLAUDE_PLAN_BADGES.get(str(v.get("subscriptionType") or "").lower()),
        })
    return out


# ================= 数据层:Claude(与 Claude Code 同源) =================
_keychain_service: str | None = None


def _keychain_read_service(service: str) -> str | None:
    import subprocess
    try:
        p = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                           capture_output=True, text=True, timeout=8)
    except Exception:
        return None
    out = (p.stdout or "").strip()
    return out if p.returncode == 0 and out else None


def _keychain_read_raw() -> str | None:
    """mac:从登录钥匙串读取 Claude Code 凭证原文(service 名个别版本不一致,逐个尝试)。"""
    global _keychain_service
    for service in ((_keychain_service,) if _keychain_service else ()) + ("Claude Code-credentials", "Claude Code"):
        out = _keychain_read_service(service)
        if out:
            _keychain_service = service
            return out
    return None


def _keychain_account(service: str) -> str:
    """沿用原条目的账户名(读不到就用当前用户名)。"""
    import subprocess
    acct = os.environ.get("USER") or ""
    try:
        p = subprocess.run(["security", "find-generic-password", "-s", service],
                           capture_output=True, text=True, timeout=8)
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if line.startswith('"acct"') and "=" in line:
                val = line.split("=", 1)[1].strip()
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                if val and val != "<NULL>":
                    acct = val
                break
    except Exception:
        pass
    return acct


def _keychain_write_raw(payload: str) -> bool:
    """mac:把续期后的凭证写回钥匙串(-U 覆盖同名项),写完读回校验。
       优先经 `security -i` 的标准输入传十六进制数据(-X),凭证不会出现在进程命令行里
       (命令行参数对本机其它用户的 ps 可见);数据过长(stdin 单行有长度上限)才退回 -w 参数。"""
    import subprocess
    service = _keychain_service or "Claude Code-credentials"
    acct = _keychain_account(service)

    def quote(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    line = f"add-generic-password -U -a {quote(acct)} -s {quote(service)} -X {payload.encode('utf-8').hex()}\n"
    if len(line) < 4000:
        try:
            subprocess.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=10)
        except Exception:
            pass
        if _keychain_read_service(service) == payload:
            return True
    try:
        p = subprocess.run(["security", "add-generic-password", "-U",
                            "-s", service, "-a", acct, "-w", payload],
                           capture_output=True, text=True, timeout=8)
    except Exception:
        return False
    return p.returncode == 0 and _keychain_read_service(service) == payload


def _claude_load_uncached() -> tuple[dict | None, str | None]:
    try:
        creds = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
        if creds.get("claudeAiOauth", {}).get("accessToken"):
            return creds, "file"
    except Exception:
        pass
    if IS_MAC:
        raw = _keychain_read_raw()
        if raw:
            try:
                creds = json.loads(raw)
            except Exception:
                # 个别版本钥匙串里直接存 token 本身(无 refreshToken,过期后只能重新登录)
                creds = {"claudeAiOauth": {"accessToken": raw}} if (raw.startswith("ey") or len(raw) > 40) else None
            if creds and creds.get("claudeAiOauth", {}).get("accessToken"):
                return creds, "keychain"
    return None, None


_cli_cache: dict = {"creds": None, "source": None, "stamp": None, "t": 0.0}


def _claude_load(fresh: bool = False) -> tuple[dict | None, str | None]:
    """读取完整 Claude 凭证。返回 (creds, source),source ∈ {'file', 'keychain', None}。
       带内存缓存:凭证文件按 (mtime, size) 判断是否变化(一次 stat 的开销);
       钥匙串 / 未找到按 KEYCHAIN_CACHE_TTL 过期。fresh=True 强制重读。"""
    try:
        st = CREDENTIALS.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    c = _cli_cache
    if (not fresh and c["t"] and stamp == c["stamp"]
            and (c["source"] == "file" or time.time() - c["t"] < KEYCHAIN_CACHE_TTL)):
        return c["creds"], c["source"]
    creds, source = _claude_load_uncached()
    c.update(creds=creds, source=source, stamp=stamp, t=time.time())
    return creds, source


def _claude_persist(creds: dict, source: str | None) -> None:
    """把续期后的凭证写回原存储;钥匙串写失败时落盘到官方 SSH 回退位置,避免丢失轮换后的 refreshToken。"""
    try:
        if source == "keychain" and _keychain_write_raw(json.dumps(creds, ensure_ascii=False)):
            return
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(CREDENTIALS, creds)
        except Exception:
            pass
    finally:
        _cli_cache["t"] = 0.0  # 存储已变,下次重读


_claude_refresh_block_until = 0.0


def _claude_refresh(creds: dict, source: str | None) -> str | None:
    """用 refreshToken 换新 accessToken 并写回。成功返回 None,失败返回展示给用户的错误文案。"""
    global _claude_refresh_block_until
    oauth = creds.get("claudeAiOauth", {})
    rt = oauth.get("refreshToken")
    if not rt:
        return "凭证不含 refreshToken:请在终端运行一次 claude 登录"
    if time.time() < _claude_refresh_block_until:
        return "token 续期失败,稍后自动重试"
    body = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLAUDE_CLIENT_ID}
    err = "token 续期失败,稍后自动重试"
    for url in CLAUDE_TOKEN_URLS:
        try:
            r = _http("claude").post(url, json=body, timeout=15, headers={"User-Agent": user_agent()})
        except requests.RequestException:
            err = NET_ERR
            continue
        if r.ok:
            try:
                d = r.json()
            except ValueError:
                d = {}
            if not d.get("access_token"):
                err = "token 续期响应异常,稍后重试"
                continue
            oauth["accessToken"] = d["access_token"]
            if d.get("refresh_token"):
                oauth["refreshToken"] = d["refresh_token"]
            oauth["expiresAt"] = int((time.time() + float(d.get("expires_in") or 3600)) * 1000)
            creds["claudeAiOauth"] = oauth
            _claude_persist(creds, source)
            return None
        if r.status_code in (400, 401):
            _claude_refresh_block_until = time.time() + 300
            return "凭证已失效:请在终端运行一次 claude 重新登录(此后卡片会自动续期)"
        err = f"token 续期失败(HTTP {r.status_code}),稍后重试"  # 403/429/5xx → 退避后重试
    _claude_refresh_block_until = time.time() + REFRESH_RETRY_COOLDOWN
    return err


def _claude_bearer_headers(tok: str) -> dict:
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "User-Agent": user_agent(),
        "anthropic-beta": "oauth-2025-04-20",
    }


def _cli_info(creds: dict | None):
    """(accessToken, 过期时间戳秒 / 0=未知, 套餐徽标)。"""
    oauth = (creds or {}).get("claudeAiOauth") or {}
    exp = float(oauth.get("expiresAt") or 0)
    exp = exp / 1000.0 if exp > 1e12 else exp
    return (oauth.get("accessToken"), exp,
            CLAUDE_PLAN_BADGES.get(str(oauth.get("subscriptionType") or "").lower()))


_claude_last_source = "—"  # 最近一次使用的令牌来源(仅用于 --check 诊断)


def claude_headers(rejected: frozenset | set = frozenset()):
    """返回 (headers, error, plan)。按开销从低到高挑一个可用令牌:
         1. CLI 凭证(文件 / mac 钥匙串)当前有效 → 直接用(有缓存,几乎零开销)
         2. 桌面版令牌有效 → 用它(桌面版自己续期,不必动 CLI 的 refreshToken)
         3. 用 refreshToken 续期 CLI 凭证并写回
         4. 兜底:任何还没被拒绝过的桌面令牌(即便临期,也比无令牌强)
       rejected = 本轮已被服务端以 401 拒绝的 token,会被跳过。"""
    global _claude_last_source
    now = time.time()

    def usable(tok, exp):  # exp=0 表示凭证里没写过期时间(老格式):当作可用,401 时再续期
        return bool(tok) and tok not in rejected and (not exp or exp - now >= 120)

    creds, source = _claude_load()
    tok, exp, plan = _cli_info(creds)
    if not usable(tok, exp) and creds:  # 缓存里的临期/被拒 → 重读一次存储:CLI 可能刚自己续期过
        creds, source = _claude_load(fresh=True)
        tok, exp, plan = _cli_info(creds)
    if usable(tok, exp):
        _claude_last_source = f"CLI 凭证({'钥匙串' if source == 'keychain' else '文件'})"
        return _claude_bearer_headers(tok), None, plan

    desk = [d for d in _claude_desktop_tokens() if d["token"] not in rejected]
    valid = [d for d in desk if d["expiresAt"] > now + 60]
    if valid:
        best = max(valid, key=lambda d: d["expiresAt"])
        _claude_last_source = "桌面版令牌"
        return _claude_bearer_headers(best["token"]), None, best.get("plan")

    err = None
    if creds:
        err = _claude_refresh(creds, source)
        if err is None:
            tok, exp, plan2 = _cli_info(creds)
            _claude_last_source = "CLI 凭证(刚续期)"
            return _claude_bearer_headers(tok), None, plan2 or plan
        if tok and tok not in rejected and exp > now:  # 续期失败但旧 token 还没真正过期 → 先用着
            _claude_last_source = "CLI 凭证(临期)"
            return _claude_bearer_headers(tok), None, plan
    if desk:
        best = max(desk, key=lambda d: d["expiresAt"])
        _claude_last_source = "桌面版令牌(兜底)"
        return _claude_bearer_headers(best["token"]), None, best.get("plan")
    return None, err or "未找到登录凭证:打开一次 Claude 桌面版,或在终端运行一次 claude 登录", plan


def fetch_profile(headers: dict) -> dict | None:
    try:
        r = _http("claude").get(API_URL_PROFILE, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


_profile_plan: str | None = None
_profile_tried = False


def fetch_claude() -> dict:
    """拉取 Claude 用量。返回 {"windows": [(key, util, resets_at, label)], "plan": …} 或 {"error": …}。"""
    global _profile_plan, _profile_tried
    s = _http("claude")
    rejected: set = set()
    try:
        for _ in range(3):  # 401 → 换下一个候选令牌(或续期)再试,最多 3 次
            h, err, plan = claude_headers(rejected)
            if not h:
                return {"error": err, "retry_soon": err == NET_ERR}
            r = s.get(API_URL_USAGE, headers=h, timeout=10)
            if r.status_code != 401:
                break
            rejected.add(h["Authorization"][len("Bearer "):])
        else:
            return {"error": "登录已过期:请在终端运行一次 claude 重新登录"}
        if r.status_code == 429:
            return {"error": "请求过于频繁,稍后自动重试"}
        r.raise_for_status()
        data = r.json()
    except (requests.ConnectionError, requests.Timeout):
        return {"error": NET_ERR, "retry_soon": True}
    except Exception as e:
        return {"error": f"获取失败:{type(e).__name__}"}
    wins = []
    for key, val in data.items():
        if (isinstance(val, dict) and val.get("utilization") is not None
                and val.get("resets_at")):
            wins.append((key, float(val["utilization"]), val["resets_at"], LABELS.get(key, key)))
    wins.sort(key=lambda it: (ORDER.index(it[0]) if it[0] in ORDER else len(ORDER), it[0]))
    if plan is None and not _profile_tried:  # 老版本凭证无 subscriptionType → 拉一次 profile 兜底
        _profile_tried = True
        acc = (fetch_profile(h) or {}).get("account", {})
        _profile_plan = "MAX" if acc.get("has_claude_max") else ("PRO" if acc.get("has_claude_pro") else None)
    return {"windows": wins, "plan": plan or _profile_plan}


# ================= 数据层:Codex(与 Codex CLI 同源) =================
def _codex_load() -> dict | None:
    try:
        return json.loads(CODEX_AUTH.read_text(encoding="utf-8"))
    except Exception:
        return None


_codex_refresh_block_until = 0.0


def _codex_refresh(auth: dict) -> str | None:
    """用 refresh_token 续期 Codex 凭证并写回 auth.json。成功返回 None。"""
    global _codex_refresh_block_until
    toks = auth.get("tokens") or {}
    rt = toks.get("refresh_token")
    if not rt:
        return "Codex 凭证不含 refresh_token:请运行 codex 重新登录"
    if time.time() < _codex_refresh_block_until:
        return "Codex token 续期失败,稍后自动重试"
    body = {"client_id": CODEX_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": rt}
    try:
        r = _http("codex").post(CODEX_TOKEN_URL, json=body, timeout=15, headers={"User-Agent": CODEX_UA})
    except requests.RequestException:
        return NET_ERR
    if not r.ok:
        if r.status_code in (400, 401):
            _codex_refresh_block_until = time.time() + 300
            return "Codex 凭证已失效:请运行 codex 重新登录"
        _codex_refresh_block_until = time.time() + REFRESH_RETRY_COOLDOWN
        return f"Codex token 续期失败(HTTP {r.status_code}),稍后重试"
    try:
        d = r.json()
    except ValueError:
        _codex_refresh_block_until = time.time() + REFRESH_RETRY_COOLDOWN
        return "Codex token 续期响应异常,稍后重试"
    for k in ("id_token", "access_token", "refresh_token"):
        if d.get(k):
            toks[k] = d[k]
    auth["tokens"] = toks
    auth["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        _atomic_write_json(CODEX_AUTH, auth)
    except Exception:
        pass
    return None


def _codex_window_label(secs: float) -> str:
    if not secs:
        return "窗口"
    h = secs / 3600.0
    if h <= 47:
        return f"{max(1, round(h))} 小时"
    d = round(secs / 86400.0)
    return "每周" if d == 7 else f"{d} 天"


def fetch_codex() -> dict:
    """拉取 Codex(ChatGPT 登录)限额。返回结构与 fetch_claude 一致。"""
    auth = _codex_load()
    if auth is None:
        return {"error": "未找到 Codex 凭证:请先运行 codex 登录"}
    toks = auth.get("tokens") or {}
    if not toks.get("access_token"):
        if auth.get("OPENAI_API_KEY"):
            return {"error": "Codex 当前为 API Key 计费模式,无订阅限额可查"}
        return {"error": "Codex 凭证不完整:请运行 codex 重新登录"}
    exp = _jwt_exp(toks["access_token"])
    if exp and exp - time.time() < 120:  # 临期 → 先续期;失败且确已过期才报错
        err = _codex_refresh(auth)
        if err and exp <= time.time():
            return {"error": err, "retry_soon": err == NET_ERR}
        toks = auth.get("tokens") or {}

    s = _http("codex")

    def _get():
        headers = {"Authorization": f"Bearer {toks.get('access_token', '')}", "User-Agent": CODEX_UA}
        if toks.get("account_id"):
            headers["ChatGPT-Account-Id"] = toks["account_id"]
        return s.get(CODEX_USAGE_URL, headers=headers, timeout=10)

    try:
        r = _get()
        if r.status_code == 401:  # 服务端拒绝旧 token → 续期后重试一次
            err = _codex_refresh(auth)
            if err:
                return {"error": err, "retry_soon": err == NET_ERR}
            toks = auth.get("tokens") or {}
            r = _get()
            if r.status_code == 401:
                return {"error": "Codex 登录已过期:请运行 codex 重新登录"}
        if r.status_code == 429:
            return {"error": "请求过于频繁,稍后自动重试"}
        r.raise_for_status()
        data = r.json()
    except (requests.ConnectionError, requests.Timeout):
        return {"error": NET_ERR, "retry_soon": True}
    except Exception as e:
        return {"error": f"获取失败:{type(e).__name__}"}
    rl = data.get("rate_limit") or {}
    wins = []
    for key, win in (("codex_5h", rl.get("primary_window")), ("codex_weekly", rl.get("secondary_window"))):
        if not isinstance(win, dict) or win.get("used_percent") is None:
            continue
        if win.get("reset_at"):
            iso = datetime.fromtimestamp(float(win["reset_at"]), timezone.utc).isoformat()
        elif win.get("reset_after_seconds") is not None:
            iso = datetime.fromtimestamp(time.time() + float(win["reset_after_seconds"]), timezone.utc).isoformat()
        else:
            iso = ""
        wins.append((key, float(win["used_percent"]), iso,
                     _codex_window_label(float(win.get("limit_window_seconds") or 0))))
    if not wins:
        return {"error": "Codex 暂无限额数据"}
    plan = str(data.get("plan_type") or "").lower()
    return {"windows": wins, "plan": CODEX_PLAN_BADGES.get(plan) or (plan.replace("_", " ").upper()[:8] if plan else None)}


FETCHERS = {"claude": fetch_claude, "codex": fetch_codex}


# ================= 开机自启 =================
def _launch_argv() -> list[str]:
    """自启动命令:就用当前这个解释器(装好依赖的那个)+ 本脚本的绝对路径。"""
    # 只转绝对路径、不解析符号链接:venv 里的 python 是指向系统解释器的链接,解析后会丢掉 venv 的依赖
    exe = os.path.abspath(sys.executable) if sys.executable else "python3"
    if IS_WIN:
        pyw = Path(exe).with_name("pythonw.exe")  # 无控制台窗口
        if pyw.exists():
            exe = str(pyw)
    return [exe, str(Path(__file__).resolve())]


def _autostart_file() -> Path | None:
    if IS_MAC:
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    if IS_LINUX:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        return base / "autostart" / "ai-usage-card.desktop"
    return None


def _passthrough_env() -> dict:
    """自启动时沿用当前设置的相关环境变量(自定义配置目录等)。"""
    keys = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "QUOTA_CARD_OPAQUE", "QUOTA_CARD_DOCK")
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


def autostart_enabled() -> bool:
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as k:
                winreg.QueryValueEx(k, AUTOSTART_NAME)
            return True
        except OSError:
            return False
    f = _autostart_file()
    return bool(f and f.exists())


def set_autostart(on: bool) -> bool:
    """开启 / 关闭开机自启。成功返回 True。"""
    try:
        argv = _launch_argv()
        if IS_WIN:
            import subprocess
            import winreg
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                if on:
                    winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(argv))
                else:
                    try:
                        winreg.DeleteValue(k, AUTOSTART_NAME)
                    except FileNotFoundError:
                        pass
            return True
        f = _autostart_file()
        if f is None:
            return False
        if not on:
            f.unlink(missing_ok=True)
            return True
        f.parent.mkdir(parents=True, exist_ok=True)
        env = _passthrough_env()
        if IS_MAC:  # 用户级 LaunchAgent:登录图形会话时由 launchd 拉起,无终端窗口
            import plistlib
            plist = {"Label": LAUNCH_AGENT_LABEL, "ProgramArguments": argv, "RunAtLoad": True,
                     "ProcessType": "Interactive", "LimitLoadToSessionType": "Aqua",
                     "WorkingDirectory": str(Path(__file__).resolve().parent)}
            if env:
                plist["EnvironmentVariables"] = env
            f.write_bytes(plistlib.dumps(plist))
        else:
            f.write_text("[Desktop Entry]\nType=Application\nName=AI Usage Card\n"
                         f"Exec={_desktop_exec(argv, env)}\nTerminal=false\nNoDisplay=true\n"
                         "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")
        return True
    except Exception:
        return False


def _desktop_exec(argv: list[str], env: dict) -> str:
    """.desktop 的 Exec 字段:每个参数整体用双引号包住,内部 " ` $ \\ 转义;有环境变量时经 env 传入。"""
    def q(a: str) -> str:
        return '"' + "".join("\\" + ch if ch in '"`$\\' else ch for ch in a) + '"'
    args = (["env"] + [f"{k}={v}" for k, v in env.items()] if env else []) + argv
    return " ".join(q(a) for a in args)


# ================= 应用入口(macOS .app / Linux 应用菜单) =================
APP_DISPLAY_NAME = "AI 用量卡片"


def _app_icon(size: int = 1024):
    """应用图标:深色圆角卡片 + 绿 / 黄 / 红三条用量条(呼应卡片本身)。需要 Pillow,没有则返回 None。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    k = size / 256.0

    def box(*v):
        return [int(round(x * k)) for x in v]

    def col(h):
        return _hex_rgb(h) + (255,)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(box(22, 22, 234, 234), radius=int(52 * k), fill=col(C_CARD),
                        outline=col(C_BORDER), width=max(1, int(4 * k)))
    d.ellipse(box(50, 56, 70, 76), fill=col(C_ACCENT))
    d.rounded_rectangle(box(82, 60, 162, 72), radius=int(6 * k), fill=col(C_TITLE))
    for i, (frac, c) in enumerate(((0.36, C_GREEN), (0.62, C_AMBER), (0.86, C_RED))):
        y = 104 + i * 40
        d.rounded_rectangle(box(50, y, 206, y + 18), radius=int(9 * k), fill=col(C_TRACK))
        d.rounded_rectangle(box(50, y, 50 + 156 * frac, y + 18), radius=int(9 * k), fill=col(c))
    return img


def _mac_app_path() -> Path:
    return Path.home() / "Applications" / f"{APP_DISPLAY_NAME}.app"


def _linux_launcher_paths() -> tuple[Path, Path]:
    """(应用菜单 .desktop 文件, 图标 PNG)。"""
    data = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return data / "applications" / "ai-usage-card.desktop", data / "icons" / "ai-usage-card.png"


def app_installed() -> bool:
    if IS_MAC:
        return _mac_app_path().exists()
    if IS_LINUX:
        return _linux_launcher_paths()[0].exists()
    return False


def install_app() -> Path | None:
    """生成应用入口,返回其路径(失败 / 不支持返回 None)。重复执行会覆盖更新(如项目换了位置)。
         macOS:~/Applications/AI 用量卡片.app —— 可从 Spotlight / 启动台打开、拖进 Dock
         Linux:~/.local/share/applications/ai-usage-card.desktop —— 出现在应用菜单 / 活动搜索里
       入口记录的是当前解释器 + 本脚本路径(与开机自启相同),用哪个 Python 运行本命令就用哪个启动。"""
    argv, env = _launch_argv(), _passthrough_env()
    script_dir = str(Path(__file__).resolve().parent)
    try:
        if IS_MAC:
            import plistlib
            import shlex
            import shutil
            import subprocess
            app = _mac_app_path()
            if app.exists():
                shutil.rmtree(app)
            macos_dir, res_dir = app / "Contents" / "MacOS", app / "Contents" / "Resources"
            macos_dir.mkdir(parents=True)
            res_dir.mkdir(parents=True)
            exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items())
            alert = ("display alert \"" + APP_DISPLAY_NAME + "\" message \"找不到程序或 Python 解释器(项目目录"
                     "可能已移动)。请在项目目录重新运行:./start.sh --install-app\"")
            launcher = macos_dir / "launcher"
            # 启动器只负责在后台拉起程序然后立刻退出:卡片进程自己管理 Dock / 菜单栏图标
            launcher.write_text(
                "#!/bin/sh\n"
                "# 由 quota_card.py --install-app 生成;项目目录或 Python 变了,重新运行一次 --install-app 即可\n"
                f"{exports}"
                f"if [ ! -f {shlex.quote(argv[1])} ] || [ ! -x {shlex.quote(argv[0])} ]; then\n"
                f"  osascript -e {shlex.quote(alert)} >/dev/null 2>&1\n"
                "  exit 1\n"
                "fi\n"
                f"cd {shlex.quote(script_dir)} || exit 1\n"
                f"nohup {' '.join(shlex.quote(a) for a in argv)} >/dev/null 2>&1 &\n", encoding="utf-8")
            launcher.chmod(0o755)
            info = {"CFBundleName": APP_DISPLAY_NAME, "CFBundleDisplayName": APP_DISPLAY_NAME,
                    "CFBundleIdentifier": LAUNCH_AGENT_LABEL + ".launcher", "CFBundleExecutable": "launcher",
                    "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
                    "LSUIElement": True, "LSMinimumSystemVersion": "10.13", "NSHighResolutionCapable": True}
            icon = _app_icon(1024)
            if icon is not None:
                icon.save(res_dir / "AppIcon.icns")
                info["CFBundleIconFile"] = "AppIcon"
            (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
            # 让访达 / Spotlight 立即识别新应用(失败无妨,系统稍后也会自行索引)
            lsregister = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                          "LaunchServices.framework/Support/lsregister")
            if os.path.exists(lsregister):
                subprocess.run([lsregister, "-f", str(app)], capture_output=True, timeout=30)
            return app
        if IS_LINUX:
            desk, icon_path = _linux_launcher_paths()
            desk.parent.mkdir(parents=True, exist_ok=True)
            icon_ref = "utilities-system-monitor"  # 没有 Pillow 时用主题自带图标
            icon = _app_icon(256)
            if icon is not None:
                icon_path.parent.mkdir(parents=True, exist_ok=True)
                icon.save(icon_path)
                icon_ref = str(icon_path)
            desk.write_text(
                "[Desktop Entry]\nType=Application\nVersion=1.0\n"
                f"Name=AI Usage Card\nName[zh]={APP_DISPLAY_NAME}\nName[zh_CN]={APP_DISPLAY_NAME}\n"
                "GenericName=AI Usage Monitor\nGenericName[zh_CN]=AI 用量监视\n"
                "Comment=Claude / Codex usage limits at a glance\n"
                "Comment[zh_CN]=实时显示 Claude / Codex 用量限额\n"
                f"Exec={_desktop_exec(argv, env)}\nPath={script_dir}\nIcon={icon_ref}\n"
                "Terminal=false\nCategories=Utility;\nKeywords=Claude;Codex;usage;quota;用量;额度;\n"
                "StartupNotify=false\n", encoding="utf-8")
            return desk
    except Exception:
        return None
    return None


def uninstall_app() -> bool:
    try:
        if IS_MAC:
            import shutil
            app = _mac_app_path()
            if app.exists():
                shutil.rmtree(app)
            return True
        if IS_LINUX:
            for f in _linux_launcher_paths():
                f.unlink(missing_ok=True)
            return True
    except Exception:
        return False
    return False


# ================= 展示辅助 =================
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


_TRAY_FONT_FILES = (
    # Windows
    "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf", "/usr/share/fonts/noto/NotoSans-Bold.ttf",
)


@functools.lru_cache(maxsize=None)
def _tray_font_path() -> str | None:
    for fp in _TRAY_FONT_FILES:
        if os.path.isfile(fp):
            return fp
    # 仅给文件名:交给 Pillow 在系统字体目录里搜索(只搜一次,结果缓存)
    try:
        from PIL import ImageFont
    except Exception:
        return None
    for name in ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"):
        try:
            ImageFont.truetype(name, 12)
            return name
        except Exception:
            continue
    return None


@functools.lru_cache(maxsize=64)
def _tray_font(size: int):
    """托盘图标用的粗体数字字体(按字号缓存,避免每次重绘都重新解析字体文件)。"""
    from PIL import ImageFont
    fp = _tray_font_path()
    if fp:
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)  # Pillow ≥ 10.1 支持可缩放的默认字体
    except TypeError:
        return ImageFont.load_default()


class _Wakeup:
    """后台线程 → Tk 主线程的唤醒通道。
       mac/Linux:自管道 + Tcl 文件事件,有消息时才唤醒主循环,空闲时零轮询(省电);
       Windows(Tk 不支持文件事件)或创建失败时,退回定时轮询队列。"""

    def __init__(self, root: tk.Tk, callback):
        self._root = root
        self._r = self._w = None
        if IS_WIN:
            return
        try:
            r, w = os.pipe()
            os.set_blocking(r, False)
            os.set_blocking(w, False)

            def on_readable(*_):
                try:
                    while os.read(r, 4096):
                        pass
                except (BlockingIOError, InterruptedError):
                    pass
                except OSError:
                    return
                callback()

            root.tk.createfilehandler(r, tk.READABLE, on_readable)
            self._r, self._w = r, w
        except Exception:
            self._r = self._w = None

    @property
    def active(self) -> bool:
        return self._w is not None

    @property
    def write_fd(self) -> int:
        return self._w

    def notify(self) -> None:
        if self._w is not None:
            try:
                os.write(self._w, b"!")
            except (BlockingIOError, InterruptedError):
                pass  # 管道已满 = 主线程本来就会被唤醒
            except OSError:
                pass

    def close(self) -> None:
        r, w, self._r, self._w = self._r, self._w, None, None
        if r is None:
            return
        try:
            self._root.tk.deletefilehandler(r)
        except Exception:
            pass
        for fd in (r, w):
            try:
                os.close(fd)
            except OSError:
                pass


# ================= 主程序 =================
class QuotaCard:
    _CURSORS = {"l": "sb_h_double_arrow", "r": "sb_h_double_arrow",
                "t": "sb_v_double_arrow", "b": "sb_v_double_arrow",
                "tl": "size_nw_se", "br": "size_nw_se",
                "tr": "size_ne_sw", "bl": "size_ne_sw"}

    def __init__(self):
        cfg = self._load_cfg()
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, float(cfg.get("zoom", 1.0))))
        self.alpha = max(0.4, min(1.0, float(cfg.get("alpha", 0.97))))
        self.pinned = bool(cfg.get("pinned", True))
        self.hidden = set(cfg.get("hidden", []))
        self.reset_mode = cfg.get("reset_mode", "clock")
        self.tray_key = cfg.get("tray_key", DEFAULT_TRAY_KEY)
        prov = cfg.get("providers")
        if not isinstance(prov, dict):  # 首次运行/旧配置:Claude 默认开;装了 Codex 就一并开
            prov = {"claude": True, "codex": CODEX_AUTH.exists()}
        self.providers = {p: bool(prov.get(p, p == "claude")) for p in PROVIDERS}

        self.root = tk.Tk()
        self.root.title("AI 用量")
        _resolve_font(self.root)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", self.pinned)
        canvas_bg = self._setup_transparency()
        try:
            self.root.attributes("-alpha", self.alpha)
        except Exception:
            pass

        self._cur_w = self._cur_h = -1
        self._pos = (0, 0)
        self.canvas = tk.Canvas(self.root, bg=canvas_bg, highlightthickness=0,
                                width=self.s(BW), height=self.s(120))
        self.canvas.pack(fill="both", expand=True)

        self.state = {p: None for p in PROVIDERS}
        self._buttons = []
        self._pending = None
        self._drag = None
        self._resize = None
        self._cursor = None
        self._moved = False
        self._hidden_card = False
        self._sig = None             # 上一次整卡重绘时的布局签名
        self._dyn_status = None      # 每秒只改文字的画布项:底部状态 / 底部时钟 / 各行重置时间
        self._dyn_clock = None
        self._dyn_resets: list = []
        self._dyn_last: dict = {}
        self._save_id = None
        self._wheel_t = 0.0
        self._wheel_acc = 0
        self._last_tick = 0.0
        self._tick_id = None
        self._refresh_evt = threading.Event()
        self._stop = False
        self.cmd_q: queue.Queue = queue.Queue()
        self._wake = _Wakeup(self.root, self._drain_cmds)
        self._show_req = False
        if not IS_WIN:  # 再次启动(如点应用图标)时,新进程发 SIGUSR1 让本实例显示卡片
            try:
                import signal
                signal.signal(signal.SIGUSR1, self._on_show_signal)
                if self._wake.active:  # 信号到达时写一个字节进唤醒管道,主循环立刻醒来处理
                    signal.set_wakeup_fd(self._wake.write_fd, warn_on_full_buffer=False)
            except Exception:
                pass

        self._place_window(cfg)
        self._bind_events()

        self.tray = None
        self._tray_sig = None
        self._mac_text_tray = False
        self._setup_tray()
        if IS_MAC:
            self._mac_setup()

        threading.Thread(target=self._worker, daemon=True, name="worker").start()
        if not self._wake.active:
            self._poll_cmds()
        self._tick()
        self.root.mainloop()

    # ---- 缩放助手 ----
    def s(self, v: float) -> int:
        return int(round(v * SCALE * self.zoom))

    def f(self, px: int, weight: str = "normal"):
        return (FONT, -int(round(px * SCALE * self.zoom)), weight)

    # ---- 窗口外观 ----
    def _setup_transparency(self) -> str:
        """透明/圆角:按平台选最稳妥的方案,返回画布背景色。
             Windows -> 洋红色键(-transparentcolor):真透明、圆角
             macOS   -> -transparent + systemTransparent 背景:真透明、圆角(两者缺一不可)
             Linux   -> 无色键,退化为不透明卡片(方角),仍可整体半透明
           设环境变量 QUOTA_CARD_OPAQUE=1 可在任意系统强制不透明(用于排查显示异常)。"""
        force_opaque = bool(os.environ.get("QUOTA_CARD_OPAQUE"))
        if not force_opaque and IS_WIN:
            try:
                self.root.attributes("-transparentcolor", TRANSPARENT)
                return TRANSPARENT
            except Exception:
                pass
        elif not force_opaque and IS_MAC:
            try:
                self.root.attributes("-transparent", True)
                self.root.configure(bg="systemTransparent")
                return "systemTransparent"
            except Exception:
                pass
        try:
            self.root.configure(bg=C_CARD)
        except Exception:
            pass
        return C_CARD

    def _bind_events(self):
        cv = self.canvas
        cv.bind("<Button-1>", self._on_press)
        cv.bind("<B1-Motion>", self._on_drag)
        cv.bind("<ButtonRelease-1>", self._on_release)
        cv.bind("<Motion>", self._on_motion)
        cv.bind("<Leave>", lambda *_: self._set_cursor(""))
        # 右键菜单:mac 上 Tk 8.6 的右键 / 双指轻点是 Button-2(8.7+ 才统一为 Button-3),
        # 另按 mac 习惯支持 Control+单击
        for seq in (("<Button-3>", "<Button-2>", "<Control-Button-1>") if IS_MAC else ("<Button-3>",)):
            cv.bind(seq, self._on_menu_click)
        # 滚轮只绑在顶层窗口:画布的 bindtags 本就包含顶层,两处都绑会让一格滚轮生效两次
        self.root.bind("<MouseWheel>", self._on_wheel)
        if IS_LINUX:  # X11 上 Tk 8.6 的滚轮是 Button-4/5
            self.root.bind("<Button-4>", self._on_wheel)
            self.root.bind("<Button-5>", self._on_wheel)

    # ---- macOS 适配 ----
    def _mac_setup(self):
        r = self.root
        for name, fn in (("::tk::mac::Quit", self.quit),                  # ⌘Q / 注销 / 关机 → 保存后退出
                         ("::tk::mac::ReopenApplication", self._show_card)):  # 点 Dock 图标 → 显示卡片
            try:
                r.createcommand(name, fn)
            except Exception:
                pass
        # 有菜单栏图标时隐藏 Dock 图标(像原生菜单栏小工具);QUOTA_CARD_DOCK=1 可保留 Dock 图标
        if self.tray and not os.environ.get("QUOTA_CARD_DOCK"):
            ak = _appkit()
            if ak:
                try:
                    ak.NSApplication.sharedApplication().setActivationPolicy_(
                        ak.NSApplicationActivationPolicyAccessory)
                except Exception:
                    pass
        r.after(300, self._mac_apply_window_flags)

    def _mac_apply_window_flags(self):
        """置顶时让卡片出现在所有桌面(Spaces)并可浮在全屏 App 之上;取消置顶则恢复默认。"""
        ak = _appkit() if IS_MAC else None
        if not ak:
            return
        try:
            join_all = ak.NSWindowCollectionBehaviorCanJoinAllSpaces
            aux = ak.NSWindowCollectionBehaviorFullScreenAuxiliary
            move = ak.NSWindowCollectionBehaviorMoveToActiveSpace
            for w in ak.NSApplication.sharedApplication().windows() or []:
                if not str(w.className()).startswith("TK"):  # 只动 Tk 自己的窗口,不碰菜单栏图标的窗口
                    continue
                cur = int(w.collectionBehavior())
                new = ((cur & ~move) | join_all | aux) if self.pinned else (cur & ~(join_all | aux))
                if new != cur:
                    w.setCollectionBehavior_(new)
        except Exception:
            pass

    def _mac_status_title(self, text: str, rgb) -> bool:
        """mac 菜单栏:直接把百分比画成彩色文字(清晰、跟随系统字体),替代被缩成 22px 的位图。"""
        ak = _appkit()
        item = getattr(self.tray, "_status_item", None)
        if not ak or item is None:
            return False
        try:
            btn = item.button()
            size = ak.NSFont.menuBarFontOfSize_(0).pointSize()
            weight = getattr(ak, "NSFontWeightSemibold", 0.3)
            font = ak.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight)
            color = ak.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1.0)
            attrs = {ak.NSForegroundColorAttributeName: color, ak.NSFontAttributeName: font}
            btn.setAttributedTitle_(ak.NSAttributedString.alloc().initWithString_attributes_(text, attrs))
            if btn.image() is not None:
                btn.setImage_(None)
            return True
        except Exception:
            return False

    # ---- 后台轮询 ----
    def _post(self, cmd: str, payload=None):
        """任何线程都可调用:把命令交给 Tk 主线程执行。"""
        self.cmd_q.put((cmd, payload))
        self._wake.notify()

    def _worker(self):
        while not self._stop:
            active = {p: FETCHERS[p] for p in PROVIDERS if self.providers.get(p)}
            soon = False
            for p, res in _run_parallel(active):  # 各数据源并发拉取,谁先回来谁先上屏
                if self._stop:
                    return
                old = self.state.get(p) or {}
                if "windows" in res:
                    cur = {"windows": res["windows"], "plan": res.get("plan") or old.get("plan"),
                           "error": None, "updated": time.time()}
                else:  # 失败时保留上一次数据,只标注错误
                    cur = {"windows": old.get("windows") or [], "plan": old.get("plan"),
                           "error": res.get("error"), "updated": old.get("updated", 0)}
                    soon = soon or bool(res.get("retry_soon"))
                self.state = {**self.state, p: cur}
                self._post("data")
            self._refresh_evt.wait(ERROR_RETRY_INTERVAL if soon else REFRESH_INTERVAL)
            self._refresh_evt.clear()

    def _tick(self):
        """每秒一次,对齐到整秒(底部时钟准点跳动);卡片隐藏时放慢到 5 秒。"""
        if self._stop:
            return
        self._drain_cmds()  # 兜底:万一唤醒管道没触发,托盘命令最多延迟 1 秒也会被执行
        if self._stop:
            return
        now = time.time()
        if self._last_tick and now - self._last_tick > 30:  # 墙钟跳变 → 多半刚从睡眠唤醒:立即刷新
            self.refresh_now()
        self._last_tick = now
        if not self._hidden_card:
            self.render()
            delay = 1000 - int(now * 1000) % 1000 + 10
        else:
            delay = 5000
        self._tick_id = self.root.after(delay, self._tick)

    def _restart_tick(self):
        """卡片重新显示时立刻恢复每秒节拍(否则要等隐藏期间排好的 5 秒节拍,时钟会停顿)。"""
        if self._tick_id is not None:
            try:
                self.root.after_cancel(self._tick_id)
            except Exception:
                pass
        self._tick_id = self.root.after(0, self._tick)

    def _on_show_signal(self, *_):
        # 信号处理函数里只置标志位,不碰队列的锁 / Tk(避免重入);由 _drain_cmds 在主循环里处理
        self._show_req = True

    def _drain_cmds(self):
        if self._show_req:
            self._show_req = False
            self._show_card()
        try:
            while True:
                cmd, payload = self.cmd_q.get_nowait()
                if cmd == "menu_at":  # 托盘右键(Linux 原生 SNI 的 ContextMenu):在桌面给出的屏幕坐标弹菜单
                    self.open_menu(payload)
                else:
                    {"data": self._on_data, "toggle_card": self.toggle_card, "refresh": self.refresh_now,
                     "reset_pos": self.reset_position, "quit": self.quit}.get(cmd, lambda: None)()
                if self._stop:
                    return
        except queue.Empty:
            pass

    def _poll_cmds(self):  # 仅在没有唤醒管道时使用(Windows)
        if self._stop:
            return
        self._drain_cmds()
        if not self._stop:
            self.root.after(150, self._poll_cmds)

    def _on_data(self):
        if self._hidden_card:
            self._update_tray()  # 卡片藏在托盘里时,托盘数字照样要跟着更新
        else:
            self.render()

    # ---- 绘制 ----
    def _sections(self):
        """按 PROVIDERS 顺序返回启用数据源的展示数据。"""
        out = []
        for p in PROVIDERS:
            if not self.providers.get(p):
                continue
            st = self.state.get(p) or {"windows": [], "plan": None, "error": "正在加载…", "updated": 0}
            wins = st.get("windows") or []
            out.append({"p": p, "plan": st.get("plan"), "error": st.get("error"),
                        "updated": st.get("updated", 0), "wins": wins,
                        "shown": [w for w in wins if w[0] not in self.hidden]})
        return out

    def _layout_sig(self, secs):
        """决定是否需要整卡重绘的签名:只含会改变布局/图形的量;
           "N 秒前更新"、时钟、倒计时这类每秒都变的文字不在其中,走 _update_dynamic 原地改字。"""
        return (self.zoom, self.pinned, self.reset_mode,
                tuple((s_["p"], s_["plan"], s_["error"], bool(s_["updated"]),
                       tuple(s_["wins"]), tuple(s_["shown"])) for s_ in secs))

    def render(self, force: bool = False):
        """数据或布局变了才整卡重绘;否则只更新每秒变化的几处文字(开销降到原来的零头)。"""
        secs = self._sections()
        sig = self._layout_sig(secs)
        if force or sig != self._sig:
            self._sig = sig
            self._draw(secs)
            self._update_tray(secs)
        self._update_dynamic(secs)

    def _dyn_text(self, *args, **kw):
        """创建一个之后会原地改字的文本项,并记下初始文字。"""
        iid = self.canvas.create_text(*args, **kw)
        self._dyn_last[iid] = kw.get("text", "")
        return iid

    def _update_dynamic(self, secs):
        cv = self.canvas
        last = self._dyn_last

        def put(iid, text):
            if iid is not None and last.get(iid) != text:
                last[iid] = text
                cv.itemconfigure(iid, text=text)

        put(self._dyn_status, self._footer_status(secs)[1])
        put(self._dyn_clock, datetime.now().strftime("%H:%M:%S"))
        fmt = fmt_countdown if self.reset_mode == "count" else fmt_reset_clock
        for iid, resets_at in self._dyn_resets:
            put(iid, fmt(resets_at))

    def _draw(self, secs):
        cv = self.canvas
        cv.delete("all")
        self._buttons = []
        self._dyn_status = self._dyn_clock = None
        self._dyn_resets = []
        self._dyn_last = {}

        PAD, HEADER, ROW, FOOTER, RAD = self.s(BPAD), self.s(BHEADER), self.s(BROW), self.s(BFOOTER), self.s(BRAD)
        W = self.s(BW)
        multi = len(secs) >= 2
        SEC, MSG = (self.s(BSEC) if multi else 0), self.s(BMSG)
        body = sum(SEC + (len(s_["shown"]) * ROW if s_["shown"] else MSG) for s_ in secs) or MSG
        H = PAD + HEADER + body + FOOTER

        if (W, H) != (self._cur_w, self._cur_h):
            self._cur_w, self._cur_h = W, H
            cv.config(width=W, height=H)
            # 只改尺寸、保留位置。原先拼上 winfo_x()/winfo_y():窗口尚未映射时(首次绘制)二者在
            # X11 / macOS 上为 0,导致记忆的位置被重置到左上角。
            self.root.geometry(f"{W}x{H}")

        round_rect(cv, self.s(1), self.s(1), W - self.s(1), H - self.s(1), RAD, fill=C_CARD, outline=C_BORDER)

        # 头部:单数据源沿用旧样式(标题+套餐徽标);多数据源标题改为 AI 用量,徽标移到各分节
        hy = PAD + self.s(11)
        cv.create_oval(PAD, hy - self.s(4), PAD + self.s(8), hy + self.s(4), fill=C_ACCENT, outline="")
        if not multi and secs:
            title, badge = f"{PROVIDER_TITLES[secs[0]['p']]} 用量", secs[0]["plan"]
        else:
            title, badge = "AI 用量", None
        tid = cv.create_text(PAD + self.s(16), hy, text=title, anchor="w",
                             fill=C_TITLE, font=self.f(15, "bold"))
        if badge:
            self._draw_badge(cv, cv.bbox(tid)[2] + self.s(8), hy, badge)
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

        # 内容:各数据源分节与行
        y = PAD + HEADER
        if not secs:
            cv.create_text(W / 2, y + MSG / 2, text="未启用数据源 · 右键 → 数据源",
                           fill=C_DIM, font=self.f(10), justify="center")
        for s_ in secs:
            if multi:
                self._draw_section(cv, y, SEC, s_, W, PAD)
                y += SEC
            if s_["shown"]:
                for _key, util, resets_at, label in s_["shown"]:
                    self._draw_row(cv, y, label, util, resets_at, W, PAD)
                    y += ROW
            else:
                if s_["wins"]:
                    msg, color = "已全部隐藏 · 右键 → 卡片显示", C_DIM
                else:
                    msg, color = (s_["error"] or "暂无数据"), C_SUB
                cv.create_text(W / 2, y + MSG / 2, text=msg, fill=color, font=self.f(10),
                               width=W - 2 * PAD, justify="center")
                y += MSG

        self._draw_footer(cv, W, H, PAD, secs)

    def _draw_badge(self, cv, x, cy, text, small=False):
        bw = self.s((10 if small else 12) + (6 if small else 7) * len(text))
        bh = self.s(7 if small else 8)
        round_rect(cv, x, cy - bh, x + bw, cy + bh, self.s(6), fill=C_ACCENT, outline="")
        cv.create_text(x + bw / 2, cy, text=text, fill="#ffffff", font=self.f(8 if small else 9, "bold"))

    def _draw_section(self, cv, y, SEC, s_, W, PAD):
        cy = y + SEC / 2 + self.s(3)
        cv.create_oval(PAD, cy - self.s(3), PAD + self.s(6), cy + self.s(3),
                       fill=PROVIDER_DOTS.get(s_["p"], C_SUB), outline="")
        tid = cv.create_text(PAD + self.s(12), cy, text=PROVIDER_TITLES[s_["p"]], anchor="w",
                             fill=C_TITLE, font=self.f(12, "bold"))
        if s_["plan"]:
            self._draw_badge(cv, cv.bbox(tid)[2] + self.s(6), cy, s_["plan"], small=True)
        if s_["error"] and s_["shown"]:  # 有旧数据但本轮刷新失败 → 分节右侧亮警示
            cv.create_text(W - PAD, cy, text="⚠", anchor="e", fill=C_AMBER, font=self.f(11))

    def _draw_row(self, cv, y, label, util, resets_at, W, PAD):
        col = bar_color(util)
        cy1 = y + self.s(10)
        cv.create_text(PAD, cy1, text=label, anchor="w", fill=C_SUB, font=self.f(12))
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
            iid = self._dyn_text(W - PAD, cy3, text=fmt_countdown(resets_at), anchor="e",
                                 fill=C_DIM, font=self.f(10))
        else:
            cv.create_text(PAD, cy3, text="重置于", anchor="w", fill=C_DIM, font=self.f(10))
            iid = self._dyn_text(W - PAD, cy3, text=fmt_reset_clock(resets_at), anchor="e",
                                 fill=C_SUB, font=self.f(10))
        self._dyn_resets.append((iid, resets_at))

    @staticmethod
    def _footer_status(secs):
        """底部状态:(圆点颜色, 文案)。颜色只取决于布局签名里的量,文案每秒可能变。"""
        if not secs:
            return C_DIM, "未启用数据源"
        have_data = [s_ for s_ in secs if s_["updated"]]
        has_err = any(s_["error"] for s_ in secs)
        if not have_data:
            return (C_RED, "未连接") if has_err else (C_AMBER, "正在加载…")
        ago = int(time.time() - max(s_["updated"] for s_ in have_data))
        text = "刚刚更新" if ago < 5 else (f"{ago} 秒前更新" if ago < 60 else f"{ago // 60} 分前更新")
        return (C_AMBER, text + " · 可能过期") if has_err else (C_GREEN, text)

    def _draw_footer(self, cv, W, H, PAD, secs):
        fy = H - PAD + self.s(2)
        dot, text = self._footer_status(secs)
        cv.create_oval(PAD, fy - self.s(3), PAD + self.s(6), fy + self.s(3), fill=dot, outline="")
        self._dyn_status = self._dyn_text(PAD + self.s(12), fy, text=text, anchor="w",
                                          fill=C_DIM, font=self.f(10))
        self._dyn_clock = self._dyn_text(W - PAD, fy, text=datetime.now().strftime("%H:%M:%S"),
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
    def _setup_sni_tray(self) -> bool:
        """Linux:优先用原生 StatusNotifierItem(tray_sni.py,来自 @zj243 的 PR #1)。
           桌面左键调用 Activate、右键调用 ContextMenu、中键调用 SecondaryActivate,由应用直接接管:
           左键=显示/隐藏卡片、右键=菜单、中键=立即刷新,与 Windows 一致——pystray 的 AppIndicator
           后端左键只能弹菜单,GtkStatusIcon 在 KDE/Wayland 下点击转发又不可靠。
           没装 PyGObject 或桌面没有 SNI 宿主时返回 False,回退 pystray。"""
        try:
            from tray_sni import SNITray
            tray = SNITray("ai_usage_card", self._tray_image(None), "AI 用量",
                           on_activate=lambda x, y: self._post("toggle_card"),
                           on_context=lambda x, y: self._post("menu_at", (x, y)),
                           on_secondary=lambda x, y: self._post("refresh"))
            threading.Thread(target=tray.run, daemon=True, name="tray-sni").start()
            tray.wait_ready()
        except Exception:
            return False
        self.tray = tray
        return True

    def _setup_tray(self):
        if IS_LINUX and self._setup_sni_tray():
            return
        try:
            import PIL
            _ = PIL.__version__
            try:
                import pystray
            except ImportError:
                raise
            except Exception:
                # pystray 在"装了 PyGObject 但没有 GTK3"时选后端会抛 ValueError 而不是继续尝试 xorg;
                # 这里显式指定 xorg 后端再试一次
                if not IS_LINUX or os.environ.get("PYSTRAY_BACKEND"):
                    raise
                os.environ["PYSTRAY_BACKEND"] = "xorg"
                import pystray
        except Exception:  # 未安装,或 Linux 上没有可用的托盘后端 / 显示
            self.tray = None
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示/隐藏卡片", lambda *_: self._post("toggle_card"), default=True),
            pystray.MenuItem("立即刷新", lambda *_: self._post("refresh")),
            pystray.MenuItem("卡片移回屏幕", lambda *_: self._post("reset_pos")),
            pystray.MenuItem("退出", lambda *_: self._post("quit")),
        )
        icon = self._tray_image(None)
        extra = {}
        ak = _appkit() if IS_MAC else None
        if ak:  # pystray 文档要求:与其它框架共用主循环时,传入对方(此处为 Tk)的 NSApplication
            extra["darwin_nsapplication"] = ak.NSApplication.sharedApplication()
        self.tray = None
        for title in ("AI 用量", "AI Usage"):  # Linux 的 xorg 后端只接受 Latin-1 标题
            try:
                self.tray = pystray.Icon("ai_usage_card", icon, title, menu, **extra)
                break
            except Exception:
                continue
        if self.tray is None:
            return
        try:
            if IS_MAC:
                # macOS:AppKit 只能在主线程操作,而主线程的 NSApp 事件循环已由 Tk 驱动。
                # 原先把 tray.run 丢进子线程会在 mac 上崩溃;这里改为挂接到 Tk 的事件循环。
                self.tray.run_detached(setup=lambda _icon: None)
                self.tray.visible = True
                self._mac_text_tray = self._mac_status_title("—", _hex_rgb(C_SUB))
            else:
                threading.Thread(target=self.tray.run, daemon=True, name="tray").start()
        except Exception:
            self.tray = None

    # 托盘位图里数字用的颜色:同色相的深色,保证在白底上清晰(卡片用的亮色在白底上对比度只有 ~2:1)
    _TRAY_INK = {C_GREEN: (22, 128, 61), C_AMBER: (180, 95, 6), C_RED: (200, 30, 30)}

    def _tray_image(self, util):
        """托盘位图(Windows 任务栏 / Linux 托盘 / mac 无 PyObjC 时兜底)。
           白色半透明圆角底 + 用量色纯数字(不带 %):黑底彩字在深色任务栏上对比度不足、
           "%" 在 16~24px 的实际尺寸下几乎看不清,去掉后数字能放得更大(issue #3)。
           以 128px 绘制,交给系统缩小显示更锐利(PR #1)。util=None 表示尚无数据。"""
        from PIL import Image, ImageDraw
        sz, pad = 128, 4
        if util is None:
            ink, txt = (110, 110, 118), "–"
        else:
            ink, txt = self._TRAY_INK.get(bar_color(util), (40, 40, 40)), str(int(round(util)))
        img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([pad, pad, sz - pad - 1, sz - pad - 1], radius=int(sz * 0.22),
                            fill=(255, 255, 255, 230), outline=ink + (255,), width=int(sz * 0.05))
        max_w, max_h = int(sz * 0.80), int(sz * 0.66)  # 自适应字号:在描边内尽量放大
        font = _tray_font(int(sz * 0.4))
        for size in range(int(sz * 0.8), 16, -2):
            cand = _tray_font(size)
            bb = d.textbbox((0, 0), txt, font=cand)
            if bb[2] - bb[0] <= max_w and bb[3] - bb[1] <= max_h:
                font = cand
                break
        bb = d.textbbox((0, 0), txt, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        d.text(((sz - tw) / 2 - bb[0], (sz - th) / 2 - bb[1]), txt, font=font, fill=ink + (255,))
        return img

    def _update_tray(self, secs=None):
        if not self.tray:
            return
        secs = self._sections() if secs is None else secs
        avail = [(s_["p"], k, u, lbl) for s_ in secs for (k, u, _r, lbl) in s_["wins"]]
        if not avail:
            return
        p, key, util, lbl = next((it for it in avail if it[1] == self.tray_key), avail[0])
        rgb = _hex_rgb(bar_color(util))
        lines = "\n".join(f"{PROVIDER_TITLES[pp]} · {ll} {uu:.0f}%" for pp, _kk, uu, ll in avail)
        tip = (f"AI 用量(托盘:{PROVIDER_TITLES[p]} · {lbl})\n" + lines)[:120]
        sig = (key, int(round(util)), rgb, tip)
        if sig == self._tray_sig:
            return
        self._tray_sig = sig
        try:
            text = f"{int(round(util))}%"
            if self._mac_text_tray and not self._mac_status_title(text, rgb):
                self._mac_text_tray = False  # 菜单栏文字不可用 → 永久退回位图图标
            if not self._mac_text_tray:
                self.tray.icon = self._tray_image(util)
            self.tray.title = tip
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
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, rs["zoom"] * newdim / base))
        if abs(zoom - self.zoom) < 1e-3:
            return
        self.zoom = zoom
        self.render()
        nx = (rs["x"] + rs["w"] - self._cur_w) if "l" in reg else rs["x"]
        ny = (rs["y"] + rs["h"] - self._cur_h) if "t" in reg else rs["y"]
        self._move(nx, ny)

    # ---- 交互 ----
    def _on_press(self, e):
        for x1, y1, x2, y2, cb in self._buttons:
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                self._pending, self._drag, self._resize = cb, None, None
                return
        self._pending = None
        region = self._region(e.x, e.y)
        if region:
            x, y = self._cur_pos()
            self._resize = {"region": region, "mx": e.x_root, "my": e.y_root,
                            "zoom": self.zoom, "w": self._cur_w, "h": self._cur_h, "x": x, "y": y}
            self._drag = None
            return
        self._moved = False
        self._drag = (e.x_root, e.y_root, *self._cur_pos())

    def _on_drag(self, e):
        if self._resize:
            self._do_resize(e)
        elif self._drag:
            sx, sy, ox, oy = self._drag
            self._move(ox + e.x_root - sx, oy + e.y_root - sy)
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

    def _on_menu_click(self, _e=None):
        self._pending = self._drag = self._resize = None
        self.open_menu()
        return "break"

    def _on_wheel(self, e):
        num = getattr(e, "num", None)
        if num in (4, 5):  # Linux X11
            steps = 1 if num == 4 else -1
        elif IS_WIN:  # 累计到 ±120 才算一格:兼容高精度滚轮 / 精确式触控板的小步 delta
            delta = int(e.delta or 0)
            if not delta:
                return
            if (delta > 0) != (self._wheel_acc > 0):
                self._wheel_acc = 0
            self._wheel_acc += delta
            steps = int(self._wheel_acc / 120)
            self._wheel_acc -= steps * 120
        else:
            steps = 1 if (e.delta or 0) > 0 else (-1 if (e.delta or 0) < 0 else 0)
        if not steps:
            return
        if not IS_WIN:  # 节流:mac 触控板一次轻扫会连发几十个滚轮事件
            now = time.monotonic()
            if now - self._wheel_t < WHEEL_MIN_GAP:
                return
            self._wheel_t = now
        steps = max(-3, min(3, steps))
        if e.state & WHEEL_ALPHA_MODS:  # Ctrl(mac 上 ⌘ 亦可)→ 微调不透明度
            self.set_alpha(self.alpha + 0.02 * steps)
        else:                           # 滚轮 → 缩放
            self.set_zoom(self.zoom * (WHEEL_ZOOM_STEP ** steps))

    # ---- 菜单 ----
    def open_menu(self, at=None):
        """at=(x, y) 时在该屏幕坐标弹出(托盘右键);否则跟随鼠标当前位置(卡片内点击)。"""
        self._build_menu()
        x, y = (int(at[0]), int(at[1])) if at else self.root.winfo_pointerxy()
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="立即刷新", command=self.refresh_now)
        secs = self._sections()
        flat = [(s_["p"], k, lbl) for s_ in secs for (k, _u, _r, lbl) in s_["wins"]]

        # 数据源(勾选启用 Claude / Codex,可多选)
        pm = tk.Menu(m, tearoff=0)
        self._prov_vars = {}
        for p in PROVIDERS:
            var = tk.BooleanVar(value=self.providers.get(p, False))
            self._prov_vars[p] = var
            pm.add_checkbutton(label=PROVIDER_TITLES[p], variable=var,
                               command=lambda pp=p: self.toggle_provider(pp))
        m.add_cascade(label="数据源", menu=pm)

        # 卡片显示(勾选要显示哪几个窗口)
        sub = tk.Menu(m, tearoff=0)
        if flat:
            self._show_vars = {}
            for p, key, lbl in flat:
                var = tk.BooleanVar(value=key not in self.hidden)
                self._show_vars[key] = var
                sub.add_checkbutton(label=f"{PROVIDER_TITLES[p]} · {lbl}", variable=var,
                                    command=lambda k=key: self._toggle_show(k))
        else:
            sub.add_command(label="(暂无数据)", state="disabled")
        m.add_cascade(label="卡片显示", menu=sub)

        # 托盘显示(单选托盘图标显示哪个)
        if self.tray:
            tm = tk.Menu(m, tearoff=0)
            self._tray_var = tk.StringVar(value=self.tray_key)
            if flat:
                for p, key, lbl in flat:
                    tm.add_radiobutton(label=f"{PROVIDER_TITLES[p]} · {lbl}", value=key,
                                       variable=self._tray_var,
                                       command=lambda k=key: self.set_tray_key(k))
            else:
                tm.add_command(label="(暂无数据)", state="disabled")
            m.add_cascade(label="菜单栏显示" if IS_MAC else "托盘显示", menu=tm)

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
        m.add_checkbutton(label="窗口置顶(所有桌面可见)" if IS_MAC else "窗口置顶",
                          variable=self._pin_var, command=self.toggle_pin)
        self._auto_var = tk.BooleanVar(value=autostart_enabled())
        m.add_checkbutton(label="开机自启", variable=self._auto_var, command=self.toggle_autostart)
        if self.tray:
            m.add_command(label="隐藏到菜单栏" if IS_MAC else "隐藏到托盘", command=self.toggle_card)
        m.add_command(label="退出", command=self.quit)
        self.menu = m

    # ---- 动作 ----
    def refresh_now(self):
        self._refresh_evt.set()

    def toggle_provider(self, p):
        self.providers[p] = not self.providers.get(p, False)
        if self.providers[p]:
            self.refresh_now()  # 新启用的数据源立即拉一次
        self.render()
        self._save_cfg()

    def set_zoom(self, z):
        z = max(MIN_ZOOM, min(MAX_ZOOM, z))
        if abs(z - self.zoom) < 1e-3:
            return
        self.zoom = z
        self.render()
        self._save_cfg()

    def set_alpha(self, v):
        v = max(0.4, min(1.0, v))
        if abs(v - self.alpha) < 1e-4:
            return
        self.alpha = v
        self.root.attributes("-alpha", self.alpha)
        self._save_cfg()

    def set_reset_mode(self, mode):
        self.reset_mode = mode
        self.render()
        self._save_cfg()

    def set_tray_key(self, key):
        self.tray_key = key
        self._tray_sig = None
        self._update_tray()
        self._save_cfg()

    def _toggle_show(self, key):
        self.hidden.discard(key) if key in self.hidden else self.hidden.add(key)
        self.render()
        self._save_cfg()

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.root.attributes("-topmost", self.pinned)
        if IS_MAC:
            self._mac_apply_window_flags()
        self.render()
        self._save_cfg()

    def toggle_autostart(self):
        want = not autostart_enabled()
        if not set_autostart(want):
            self.root.bell()

    def _show_card(self):
        if self._hidden_card:
            self.toggle_card()
        else:
            self.root.lift()

    def toggle_card(self):
        if self._hidden_card:
            self.root.deiconify()
            self.root.attributes("-topmost", self.pinned)
            self.root.lift()
            self._hidden_card = False
            if IS_MAC:
                self._mac_apply_window_flags()
            self.render(force=True)
            self._restart_tick()
        else:
            self.root.withdraw()
            self._hidden_card = True

    def reset_position(self):
        self._move(*self._default_pos())
        self._show_card()
        self._save_cfg()

    def quit(self):
        if self._stop:
            return
        self._stop = True
        self._refresh_evt.set()  # 让后台线程尽快结束等待
        if not IS_WIN:
            try:
                import signal
                signal.set_wakeup_fd(-1)
            except Exception:
                pass
        self._wake.close()
        self._flush_cfg()
        if self.tray:
            try:
                if IS_MAC:
                    self.tray.visible = False  # mac 上托盘挂在 Tk 的事件循环里,别去 stop 共享的 NSApp
                else:
                    self.tray.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---- 工具 ----
    def _default_pos(self):
        return self.root.winfo_screenwidth() - self.s(BW) - self.s(24), self.s(48)

    def _on_screen(self, x: int, y: int) -> bool | None:
        """(x, y)(Tk 坐标,主屏左上角为原点)是否落在某块显示器内;判断不了返回 None。
           用于启动时发现"外接屏拔掉后卡片跑到屏幕外"的情况。"""
        try:
            if IS_WIN:
                import ctypes
                import ctypes.wintypes as wt
                fn = ctypes.windll.user32.MonitorFromPoint
                fn.argtypes, fn.restype = [wt.POINT, wt.DWORD], ctypes.c_void_p
                return bool(fn(wt.POINT(int(x), int(y)), 0))  # MONITOR_DEFAULTTONULL
            if IS_MAC:
                ak = _appkit()
                screens = list(ak.NSScreen.screens() or []) if ak else []
                if not screens:
                    return None
                main_h = screens[0].frame().size.height  # Cocoa 坐标原点在主屏左下角,y 向上
                for sc in screens:
                    fr = sc.frame()
                    left, top = fr.origin.x, main_h - (fr.origin.y + fr.size.height)
                    if left <= x < left + fr.size.width and top <= y < top + fr.size.height:
                        return True
                return False
            # X11:一个"屏幕"覆盖所有显示器
            return 0 <= x < self.root.winfo_screenwidth() and 0 <= y < self.root.winfo_screenheight()
        except Exception:
            return None

    def _place_window(self, cfg):
        x, y = cfg.get("x"), cfg.get("y")
        if x is not None and y is not None and self._on_screen(int(x) + self.s(BW) // 2, int(y) + self.s(12)) is False:
            x = y = None
        if x is None or y is None:
            x, y = self._default_pos()
        self._move(x, y)

    def _move(self, x, y):
        self._pos = (int(x), int(y))
        self.root.geometry(f"+{int(x)}+{int(y)}")

    def _cur_pos(self):
        """当前位置。无边框(overrideredirect)窗口只会被本程序移动,所以自己记录的坐标就是权威值;
           不读 winfo_x/y:它在窗口未映射 / 已隐藏 / 刚移动尚未生效时都可能返回旧值或 0。"""
        return self._pos

    def _load_cfg(self):
        try:
            cfg = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _save_cfg(self):
        """防抖保存:滚轮缩放 / 拖动调不透明度时会连续触发,合并成 0.6 秒后的一次写盘。"""
        if self._save_id is not None:
            try:
                self.root.after_cancel(self._save_id)
            except Exception:
                pass
        self._save_id = self.root.after(600, self._flush_cfg)

    def _flush_cfg(self):
        self._save_id = None
        try:
            x, y = self._cur_pos()
            _atomic_write_json(STATE_FILE, {
                "x": x, "y": y,
                "alpha": round(self.alpha, 3), "zoom": round(self.zoom, 3),
                "pinned": self.pinned, "hidden": sorted(self.hidden),
                "reset_mode": self.reset_mode, "tray_key": self.tray_key,
                "providers": dict(self.providers),
            })
        except Exception:
            pass


# ================= 自检 =================
def _env_report() -> list[str]:
    import platform
    if IS_MAC:
        osname = f"macOS {platform.mac_ver()[0]} ({platform.machine()})"
    elif IS_WIN:
        osname = f"Windows {platform.release()}"
    else:
        osname = f"Linux {platform.release()}"
    lines = [f"系统: {osname} · Python {platform.python_version()} · Tk {tk.TkVersion}"]
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
        tray = "可用"
    except ImportError:
        tray = "不可用(pip install pystray pillow)"
    except Exception as e:  # 已安装但没有可用的托盘后端 / 图形显示
        tray = f"不可用({type(e).__name__})"
    lines.append(f"托盘: {tray}")
    if IS_LINUX:
        try:
            import gi  # noqa: F401
            sni = "可用(PyGObject 已安装;桌面需支持 StatusNotifierItem,如 KDE)"
        except Exception:
            sni = "不可用(装 python3-gi / python3-gobject 后左键可直接切换卡片)"
        lines.append(f"Linux 原生托盘: {sni}")
    if IS_MAC:
        lines.append(f"菜单栏文字 / 隐藏 Dock / 所有桌面: {'可用' if _appkit() else '不可用(需 PyObjC,随 pystray 安装)'}")
    if IS_WIN:
        cipher = "系统 BCrypt"
    else:
        cipher = None
        for mod, name in (("cryptography", "cryptography"), ("Crypto", "pycryptodome")):
            try:
                __import__(mod)
                cipher = name
                break
            except Exception:
                pass
        if not cipher:
            cipher = "系统 CommonCrypto" if IS_MAC else "不可用(pip install cryptography)"
    d = _desktop_dir()
    desk = "未找到"
    if d and (d / "config.json").exists():
        desk = "已安装(微软商店版)" if IS_WIN and "Packages" in d.parts else "已安装"
    lines.append(f"Claude 桌面版: {desk} · 令牌解密: {cipher}")
    return lines


def run_check():
    rec = getattr(sys.stdout, "reconfigure", None)
    if rec:
        try:
            rec(encoding="utf-8")
        except Exception:
            pass
    for line in _env_report():
        print(line)
    print("Claude 凭证:", CREDENTIALS, "存在" if CREDENTIALS.exists() else "不存在")
    print("Codex 凭证:", CODEX_AUTH, "存在" if CODEX_AUTH.exists() else "不存在")
    print("User-Agent:", user_agent())
    print("开机自启:", "已开启" if autostart_enabled() else "未开启")
    if IS_MAC or IS_LINUX:
        where = _mac_app_path() if IS_MAC else _linux_launcher_paths()[0]
        print("应用入口:", f"已创建({where})" if app_installed() else "未创建(./start.sh --install-app)")
    results = dict(_run_parallel({"Claude": fetch_claude, "Codex": fetch_codex}))
    for name in ("Claude", "Codex"):
        res = results[name]
        if "error" in res:
            print(f"[{name}] 错误 -> {res['error']}")
            continue
        src = f" · 令牌来源: {_claude_last_source}" if name == "Claude" else ""
        print(f"[{name}] 套餐: {res.get('plan') or '—'}{src}")
        for _key, util, resets_at, label in res["windows"]:
            print(f"  {label:<14} {util:5.1f}%   重置于 {fmt_reset_clock(resets_at)}")


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
        f = open(_lock_path(), "a+")  # 不能用 "w":打开即截断,会抹掉持锁实例写进去的 PID
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]  # fcntl 仅 Unix
        except OSError:
            f.close()
            return False  # 已有实例持锁
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()} {_INSTANCE_TAG}\n")  # 供后来者唤出卡片(见 signal_running_instance)
        f.flush()
        _SINGLETON_HANDLE = f  # 保持文件对象存活,进程退出自动释放锁
        return True
    except Exception:
        return True  # 出错时不阻止启动


# 写进锁文件的标记:表示持锁实例会响应 SIGUSR1 唤出卡片。
# 旧版本不写这个标记,不能给它发信号(SIGUSR1 的默认动作是终止进程)。
_INSTANCE_TAG = "show-on-usr1"


def _lock_path() -> Path:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        return CONFIG_DIR / ".quota_card.lock"
    except Exception:
        return Path.home() / ".quota_card.lock"


def signal_running_instance() -> bool:
    """mac/Linux:已有实例在运行时,让它把卡片显示出来(点应用图标时卡片可能正藏在托盘里)。"""
    if IS_WIN:
        return False
    try:
        import signal
        parts = _lock_path().read_text(encoding="utf-8").split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1] == _INSTANCE_TAG:
            os.kill(int(parts[0]), signal.SIGUSR1)
            return True
    except Exception:
        pass
    return False


def _main(argv: list[str]) -> int:
    if "--check" in argv:
        run_check()
        return 0
    if "--autostart" in argv:
        i = argv.index("--autostart")
        val = argv[i + 1] if i + 1 < len(argv) else ""
        if val not in ("on", "off"):
            print("用法: quota_card.py --autostart on|off")
            return 2
        ok = set_autostart(val == "on")
        print(("开机自启已开启" if val == "on" else "开机自启已关闭") if ok else "设置开机自启失败")
        return 0 if ok else 1
    if "--install-app" in argv:
        if IS_WIN:
            print("Windows 上请直接双击 启动卡片.bat(可右键 → 发送到 → 桌面快捷方式)")
            return 1
        path = install_app()
        if not path:
            print("创建应用入口失败")
            return 1
        print(f"已创建应用入口:{path}")
        print("可从 Spotlight / 启动台打开,或拖到 Dock" if IS_MAC else "可在应用菜单 / 活动搜索里找到「AI 用量卡片」")
        return 0
    if "--uninstall-app" in argv:
        ok = uninstall_app()
        print("已移除应用入口" if ok else "移除应用入口失败")
        return 0 if ok else 1
    if not acquire_single_instance():
        # 已有实例在运行:不重复弹卡片,而是让它把卡片显示出来
        if signal_running_instance():
            print("已在运行:已把卡片调到前台")
        return 0
    QuotaCard()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
