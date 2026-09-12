"""Windows 亚克力 (Acrylic) 窗口效果 — DWM 系统背景材质。

桌面版窗口使用 Windows 11 原生亚克力材质绘制标题栏与窗口边框, 得到半透明
磨砂观感, 与系统原生应用 (设置 / 终端) 一致。

设计要点
--------
- **非侵入**: 只作用于窗口外观, 不碰业务逻辑; 失败仅记日志, 绝不影响启动。
- **分层降级**:
    * Win11 22H2+ (build >= 22621): ``DwmSetWindowAttribute`` 的
      ``DWMWA_SYSTEMBACKDROP_TYPE = DWMSBT_TRANSIENTWINDOW`` → DWM 在窗口
      背景渲染亚克力; 同时开启深色标题栏与圆角, 观感统一。
    * Win10 1803+: 退化为 ``SetWindowCompositionAttribute`` 的
      ``ACCENT_ENABLE_ACRYLICBLURBEHIND`` 强调策略。
    * 非 Windows: 直接跳过。
- **句柄定位**: 通过 ``EnumWindows`` 按「当前进程 + 可见 + 有标题」匹配顶层窗口,
  不依赖 pywebview 内部结构, 跨版本稳定。

环境变量
--------
- ``TSP_DESKTOP_ACRYLIC=0`` 可关闭亚克力效果 (默认开启)。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# 已定位到的桌面窗口句柄 (供主题切换时重设标题栏明暗复用)
_WINDOW_HANDLE: int = 0

# 前端已明确报告的主题 (None = 尚未报告)。
# 启动期「亚克力应用」与「前端主题回写」存在时序竞争: 若前端先报 light、
# 亚克力后按默认 dark 施加, 会把标题栏改回暗色。以本变量为准可消除竞争。
_THEME_DARK: bool | None = None


def get_window_handle() -> int:
    """返回缓存的窗口句柄; 未缓存则即时查找一次。"""
    global _WINDOW_HANDLE
    if not _WINDOW_HANDLE:
        try:
            _WINDOW_HANDLE = _find_window_for_pid(os.getpid())
        except Exception:  # noqa: BLE001
            _WINDOW_HANDLE = 0
    return _WINDOW_HANDLE


# ── DWM 属性 (dwmapi.h) ──────────────────────────────────────────────
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_SYSTEMBACKDROP_TYPE = 38

_DWMSBT_ACRYLIC = 3          # DWMSBT_TRANSIENTWINDOW — 亚克力材质
_DWMWCP_ROUND = 2            # DWMWCP_ROUND — 圆角

_DWMWA_COLOR_NONE = 0xFFFFFFFE  # 不指定颜色, 交由背景材质绘制

# ── 旧版强调策略 (Win10 1803+) ───────────────────────────────────────
_WCA_ACCENT_POLICY = 19
_ACCENT_ENABLE_ACRYLICBLURBEHIND = 4

# 亚克力底色: 0xAABBGGRR (ABGR 顺序!) — 深色半透明
_ACRYLIC_TINT_DARK = 0x80202020


class _ACCENTPOLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_int),
    ]


class _WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.POINTER(_ACCENTPOLICY)),
        ("SizeOfData", ctypes.c_size_t),
    ]


def _windows_build() -> int:
    """当前 Windows 内部版本号; 取不到返回 0。"""
    try:
        return int(sys.getwindowsversion().build)
    except Exception:  # noqa: BLE001
        return 0


def _find_window_for_pid(pid: int) -> int:
    """返回指定进程的第一个「可见 + 有标题」顶层窗口句柄, 找不到返回 0。"""
    user32 = ctypes.windll.user32
    found: list[int] = []

    # WNDENUMPROC: BOOL CALLBACK(HWND, LPARAM) — 返回 True 继续枚举
    enum_proc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )

    def _callback(hwnd, _lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        wnd_pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wnd_pid))
        if wnd_pid.value != pid:
            return True
        # 顶层窗口需有标题文字 (排除隐藏的消息窗 / IME 窗 / 拖影窗)
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        found.append(int(hwnd))
        return False  # 找到即停

    user32.EnumWindows(enum_proc(_callback), 0)
    return found[0] if found else 0


def _apply_dwm_acrylic(hwnd: int, is_dark: bool = True) -> bool:
    """Win11: 施加 DWM 系统背景亚克力材质。返回是否成功。"""
    dwmapi = ctypes.windll.dwmapi
    hwnd_p = ctypes.c_void_p(hwnd)
    ok = False

    # 标题栏明暗 — 跟随应用主题 (前端切换时经 JS 桥回写)
    dark = ctypes.c_int(1 if is_dark else 0)
    if dwmapi.DwmSetWindowAttribute(
        hwnd_p, _DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark), ctypes.sizeof(dark)
    ) == 0:
        ok = True

    # 圆角
    corner = ctypes.c_int(_DWMWCP_ROUND)
    dwmapi.DwmSetWindowAttribute(
        hwnd_p, _DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner), ctypes.sizeof(corner)
    )

    # 亚克力系统背景 — 核心
    backdrop = ctypes.c_int(_DWMSBT_ACRYLIC)
    if dwmapi.DwmSetWindowAttribute(
        hwnd_p, _DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(backdrop), ctypes.sizeof(backdrop)
    ) == 0:
        ok = True

    return ok


def _apply_legacy_acrylic(hwnd: int) -> bool:
    """Win10 1803+: 用强调策略实现整窗亚克力 (Win11 上拖拽会卡, 仅作降级)。"""
    user32 = ctypes.windll.user32

    accent = _ACCENTPOLICY()
    accent.AccentState = _ACCENT_ENABLE_ACRYLICBLURBEHIND
    accent.AccentFlags = 0x20 | 0x40 | 0x80 | 0x100  # 四边绘制边框
    accent.GradientColor = _ACRYLIC_TINT_DARK
    accent.AnimationId = 0

    data = _WINCOMPATTRDATA()
    data.Attribute = _WCA_ACCENT_POLICY
    data.Data = ctypes.pointer(accent)
    data.SizeOfData = ctypes.sizeof(accent)

    fn = user32.SetWindowCompositionAttribute
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WINCOMPATTRDATA)]
    return bool(fn(ctypes.c_void_p(hwnd), ctypes.byref(data)))


def apply_acrylic(hwnd: int, is_dark: bool = True) -> bool:
    """按系统版本选择实现, 对给定窗口句柄施加亚克力效果。"""
    global _WINDOW_HANDLE
    _WINDOW_HANDLE = hwnd
    # 前端已报告过主题则以其为准, 避免默认 dark 覆盖前端的 light
    if _THEME_DARK is not None:
        is_dark = _THEME_DARK
    build = _windows_build()
    try:
        if build >= 22621:
            ok = _apply_dwm_acrylic(hwnd, is_dark)
            logger.info("亚克力窗口效果已应用 (DWM system backdrop, build=%d, ok=%s)", build, ok)
            return ok
        ok = _apply_legacy_acrylic(hwnd)
        logger.info("亚克力窗口效果已应用 (legacy accent, build=%d, ok=%s)", build, ok)
        return ok
    except Exception as e:  # noqa: BLE001
        logger.warning("亚克力效果施加失败 (已忽略): %s", e)
        return False


def set_titlebar_dark(is_dark: bool) -> bool:
    """切换标题栏明暗, 跟随应用主题 (供前端主题切换调用)。

    应用主题存在 WebView2 的 localStorage 里, 后端无从得知, 因此由前端在
    主题变更时通过 JS 桥回写, 保证「亮色主题 + 暗色标题栏」这种割裂不出现。
    """
    if not _IS_WINDOWS:
        return False
    global _THEME_DARK
    _THEME_DARK = bool(is_dark)
    hwnd = get_window_handle()
    if not hwnd:
        return False
    try:
        value = ctypes.c_int(1 if is_dark else 0)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            _DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        logger.info("标题栏主题已切换为 %s (hr=%s)", "dark" if is_dark else "light", hr)
        return hr == 0
    except Exception as e:  # noqa: BLE001
        logger.warning("切换标题栏主题失败 (已忽略): %s", e)
        return False


def start_acrylic_watch(
    pid: int | None = None,
    *,
    timeout: float = 30.0,
    settle: float = 1.0,
) -> threading.Thread | None:
    """后台线程: 等待桌面窗口出现后施加亚克力效果。

    Args:
        pid: 目标进程; 默认当前进程。
        timeout: 等待窗口出现的上限秒数。
        settle: 找到窗口后再等待的秒数, 避开 WebView2 首帧重绘覆盖。

    Returns:
        启动的线程; 未启用 (非 Windows / 被环境变量关闭) 返回 None。
    """
    if not _IS_WINDOWS:
        logger.debug("非 Windows 平台, 跳过亚克力效果")
        return None

    flag = os.environ.get("TSP_DESKTOP_ACRYLIC", "1").strip().lower()
    if flag in {"0", "false", "off", "no"}:
        logger.info("亚克力窗口效果已被 TSP_DESKTOP_ACRYLIC 关闭")
        return None

    target_pid = pid or os.getpid()

    def _worker() -> None:
        deadline = time.monotonic() + timeout
        hwnd = 0
        while time.monotonic() < deadline:
            hwnd = _find_window_for_pid(target_pid)
            if hwnd:
                break
            time.sleep(0.25)

        if not hwnd:
            logger.warning("未找到桌面窗口句柄, 跳过亚克力效果")
            return

        time.sleep(settle)
        # 窗口可能被重建, 重试几次确保生效
        for _ in range(3):
            if apply_acrylic(hwnd):
                return
            time.sleep(0.5)
        logger.warning("亚克力效果多次施加未成功 (窗口可能已被重建)")

    thread = threading.Thread(target=_worker, name="acrylic-watch", daemon=True)
    thread.start()
    return thread
