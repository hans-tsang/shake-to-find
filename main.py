"""
Shake to Find Cursor
====================

A small Windows tray app that mimics macOS "Shake mouse pointer to locate".

How it works
------------
* The main thread polls the cursor position every ~10 ms via win32 GetCursorPos.
* A ShakeDetector looks at horizontal movement over a sliding time window and
  reports a "shake" when there are enough direction reversals and enough travel.
* On shake, an Overlay window (borderless, click-through, always-on-top,
  per-pixel-alpha layered window) draws a large arrow whose tip sits exactly
  on the real cursor hotspot, so it follows the real cursor.
* After `hold_ms` without shaking, the arrow shrinks smoothly back to normal
  size and the overlay is hidden.
* A pystray tray icon (running in its own thread) offers Enable/Disable,
  reload/open config and Quit.

The process is Per-Monitor-DPI-Aware (v2), so all coordinates are physical
pixels in the virtual-screen space (works across multiple monitors) and the
cursor size / thresholds are scaled by the DPI of the monitor under the cursor.
"""

__version__ = "0.1.0"  # x-release-please-version

import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import sys
import threading
import time
import winreg
from collections import deque

# ---------------------------------------------------------------------------
# DPI awareness: must be set before any window (ours or pystray's) is created.
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _enable_dpi_awareness():
    try:  # Windows 10 1703+: Per-Monitor v2
        user32.SetProcessDpiAwarenessContext.restype = wt.BOOL
        user32.SetProcessDpiAwarenessContext.argtypes = [wt.HANDLE]
        if user32.SetProcessDpiAwarenessContext(wt.HANDLE(-4)):
            return
    except AttributeError:
        pass
    try:  # Windows 8.1+: Per-Monitor v1
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    user32.SetProcessDPIAware()  # Vista+: system DPI aware


_enable_dpi_awareness()

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402
import pystray  # noqa: E402

try:
    shcore = ctypes.WinDLL("shcore")
except OSError:
    shcore = None
winmm = ctypes.WinDLL("winmm")

# ---------------------------------------------------------------------------
# Win32 constants / structures / prototypes
# ---------------------------------------------------------------------------
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020  # click-through (with WS_EX_LAYERED)
WS_EX_TOOLWINDOW = 0x00000080  # no taskbar button / alt-tab entry
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
SW_HIDE = 0
ULW_ALPHA = 0x2
AC_SRC_OVER, AC_SRC_ALPHA = 0x0, 0x1
PM_REMOVE = 0x1
MONITOR_DEFAULTTONEAREST = 0x2
MDT_EFFECTIVE_DPI = 0
SM_CYCURSOR = 14
ERROR_ALREADY_EXISTS = 183


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


def _proto(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


# Explicit prototypes are required on 64-bit Python so handles aren't truncated.
GetCursorPos = _proto(user32, "GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT))
DefWindowProcW = _proto(user32, "DefWindowProcW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
RegisterClassExW = _proto(user32, "RegisterClassExW", wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
CreateWindowExW = _proto(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR,
                         wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
DestroyWindow = _proto(user32, "DestroyWindow", wt.BOOL, wt.HWND)
ShowWindow = _proto(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
SetWindowPos = _proto(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int,
                      ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
UpdateLayeredWindow = _proto(user32, "UpdateLayeredWindow", wt.BOOL, wt.HWND, wt.HDC,
                             ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                             ctypes.POINTER(wt.POINT), wt.COLORREF,
                             ctypes.POINTER(BLENDFUNCTION), wt.DWORD)
PeekMessageW = _proto(user32, "PeekMessageW", wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND,
                      wt.UINT, wt.UINT, wt.UINT)
TranslateMessage = _proto(user32, "TranslateMessage", wt.BOOL, ctypes.POINTER(wt.MSG))
DispatchMessageW = _proto(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wt.MSG))
GetDC = _proto(user32, "GetDC", wt.HDC, wt.HWND)
ReleaseDC = _proto(user32, "ReleaseDC", ctypes.c_int, wt.HWND, wt.HDC)
MonitorFromPoint = _proto(user32, "MonitorFromPoint", wt.HMONITOR, wt.POINT, wt.DWORD)
CreateCompatibleDC = _proto(gdi32, "CreateCompatibleDC", wt.HDC, wt.HDC)
DeleteDC = _proto(gdi32, "DeleteDC", wt.BOOL, wt.HDC)
CreateDIBSection = _proto(gdi32, "CreateDIBSection", wt.HBITMAP, wt.HDC,
                          ctypes.POINTER(BITMAPINFOHEADER), wt.UINT,
                          ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
SelectObject = _proto(gdi32, "SelectObject", wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
DeleteObject = _proto(gdi32, "DeleteObject", wt.BOOL, wt.HGDIOBJ)
GetModuleHandleW = _proto(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)
CreateMutexW = _proto(kernel32, "CreateMutexW", wt.HANDLE, wt.LPVOID, wt.BOOL, wt.LPCWSTR)
if shcore is not None:
    GetDpiForMonitor = _proto(shcore, "GetDpiForMonitor", ctypes.HRESULT, wt.HMONITOR,
                              ctypes.c_int, ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "enabled": True,
    "poll_interval_ms": 10,        # cursor polling period
    "shake_window_ms": 600,        # sliding window for shake detection
    "min_reversals": 4,            # horizontal direction changes needed in the window
    "min_travel_px": 800,          # total horizontal travel needed in the window (at 96 DPI)
    "min_segment_px": 15,          # ignore jitter: a stroke must move this far to count
    "scale_thresholds_with_dpi": True,  # multiply px thresholds by monitor DPI / 96
    "cursor_scale": 5.0,           # enlarged size relative to the normal cursor
    "base_cursor_px": 0,           # normal cursor size at 96 DPI; 0 = read from Windows
    "hold_ms": 1000,               # stay big this long after the last shake
    "grow_speed": 18.0,            # exponential animation rates (1/s); higher = faster
    "shrink_speed": 7.0,
}


def app_dir():
    """Folder of the .exe when frozen by PyInstaller, else folder of this script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")


def load_config():
    """Load config.json (creating it with defaults if missing); unknown/invalid keys fall back."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        for key, default in DEFAULT_CONFIG.items():
            if key in user:
                val = user[key]
                if isinstance(default, bool):
                    cfg[key] = bool(val)
                elif isinstance(default, (int, float)) and isinstance(val, (int, float)):
                    cfg[key] = type(default)(val)
    except FileNotFoundError:
        save_config(cfg)
    except (OSError, ValueError) as e:
        print(f"config.json ignored ({e}); using defaults", file=sys.stderr)
    cfg["poll_interval_ms"] = max(1, cfg["poll_interval_ms"])
    cfg["cursor_scale"] = max(1.0, cfg["cursor_scale"])
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        print(f"Could not write config.json: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# DPI helpers
# ---------------------------------------------------------------------------
def monitor_dpi(x, y):
    """Effective DPI of the monitor containing (x, y), in physical-pixel coords."""
    if shcore is not None:
        hmon = MonitorFromPoint(wt.POINT(x, y), MONITOR_DEFAULTTONEAREST)
        dx, dy = wt.UINT(), wt.UINT()
        try:
            GetDpiForMonitor(hmon, MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy))
            return dx.value or 96
        except OSError:
            pass
    return 96


def system_cursor_base_px():
    """User's cursor size at 96 DPI (honours Settings > Accessibility > Mouse pointer size)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors") as k:
            val, _ = winreg.QueryValueEx(k, "CursorBaseSize")
            if int(val) > 0:
                return int(val)
    except OSError:
        pass
    return 32


# ---------------------------------------------------------------------------
# Cursor artwork
# ---------------------------------------------------------------------------
class CursorArt:
    """Renders an anti-aliased macOS-style arrow once at high resolution, then
    produces premultiplied BGRA frames at any size (cached)."""

    MASTER = 512  # master canvas is MASTER x MASTER
    # Arrow outline in "units"; tip (hotspot) at (0, 0). ~ classic pointer shape.
    ARROW = [(0, 0), (0, 17.5), (4.2, 13.6), (7.0, 20.2), (9.6, 19.1),
             (6.9, 12.6), (12.4, 12.6)]
    UNITS = 32  # canvas spans 32 units, like a 32x32 cursor

    def __init__(self):
        s = self.MASTER / self.UNITS
        pad = 2.0  # units of padding so the outline/shadow aren't clipped
        self.hotspot = pad / self.UNITS  # fraction of canvas size
        pts = [((x + pad) * s, (y + pad) * s) for x, y in self.ARROW]
        outline = 1.3 * s

        # Soft drop shadow.
        shadow = Image.new("L", (self.MASTER, self.MASTER), 0)
        sd = ImageDraw.Draw(shadow)
        off = 0.6 * s
        sd.polygon([(x + off, y + off * 1.5) for x, y in pts], fill=110)
        sd.line([(x + off, y + off * 1.5) for x, y in pts + pts[:1]], fill=110,
                width=int(outline * 2), joint="curve")
        shadow = shadow.filter(ImageFilter.GaussianBlur(0.8 * s))

        img = Image.new("RGBA", (self.MASTER, self.MASTER), (0, 0, 0, 0))
        img.putalpha(shadow)
        d = ImageDraw.Draw(img)
        d.line(pts + pts[:1], fill=(255, 255, 255, 255), width=int(outline * 2), joint="curve")
        for x, y in pts:  # round outer corners
            d.ellipse([x - outline, y - outline, x + outline, y + outline], fill=(255, 255, 255, 255))
        d.polygon(pts, fill=(0, 0, 0, 255))
        self.master = img
        self._cache = {}

    def frame(self, size):
        """Return (bgra_premultiplied_bytes, size, hotspot_px) for a size x size frame."""
        size = max(8, int(round(size)))
        hit = self._cache.get(size)
        if hit is None:
            img = self.master.resize((size, size), Image.LANCZOS).convert("RGBa")
            r, g, b, a = img.split()
            data = Image.merge("RGBA", (b, g, r, a)).tobytes()  # BGRA, premultiplied
            if len(self._cache) > 256:
                self._cache.clear()
            hit = self._cache[size] = (data, size, int(round(self.hotspot * size)))
        return hit


# ---------------------------------------------------------------------------
# Overlay window
# ---------------------------------------------------------------------------
class Overlay:
    """Borderless, click-through, topmost, per-pixel-alpha layered window."""

    CLASS_NAME = "ShakeToFindOverlay"

    def __init__(self):
        self._wndproc = WNDPROC(lambda h, m, w, l: DefWindowProcW(h, m, w, l))  # keep ref alive
        hinst = GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = self.CLASS_NAME
        if not RegisterClassExW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self.hwnd = CreateWindowExW(ex, self.CLASS_NAME, "Shake to Find", WS_POPUP,
                                    0, 0, 1, 1, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.screen_dc = GetDC(None)
        self.mem_dc = CreateCompatibleDC(self.screen_dc)
        self.visible = False
        self._blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        self._last_frame = None

    def show_frame(self, frame, x, y):
        """Display `frame` with its hotspot at screen position (x, y)."""
        data, size, hot = frame
        dst = wt.POINT(x - hot, y - hot)
        if frame is self._last_frame:
            # Same bitmap: just move the window (cheap).
            SetWindowPos(self.hwnd, HWND_TOPMOST, dst.x, dst.y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
        else:
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), size, -size, 1, 32, 0)
            bits = ctypes.c_void_p()
            hbmp = CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            if not hbmp:
                return
            ctypes.memmove(bits, data, len(data))
            old = SelectObject(self.mem_dc, hbmp)
            UpdateLayeredWindow(self.hwnd, self.screen_dc, ctypes.byref(dst),
                                ctypes.byref(wt.SIZE(size, size)), self.mem_dc,
                                ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(self._blend), ULW_ALPHA)
            SelectObject(self.mem_dc, old)
            DeleteObject(hbmp)
            self._last_frame = frame
        if not self.visible:
            SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self.visible = True

    def hide(self):
        if self.visible:
            ShowWindow(self.hwnd, SW_HIDE)
            self.visible = False

    def destroy(self):
        DeleteDC(self.mem_dc)
        ReleaseDC(None, self.screen_dc)
        DestroyWindow(self.hwnd)


# ---------------------------------------------------------------------------
# Shake detection
# ---------------------------------------------------------------------------
class ShakeDetector:
    """Counts horizontal direction reversals and total horizontal travel over a
    sliding time window. Small strokes (< min_segment_px) are treated as jitter."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.moves = deque()  # (timestamp_s, dx_px)
        self.last_x = None

    def reset(self):
        self.moves.clear()
        self.last_x = None

    def feed(self, t, x, dpi_scale):
        cfg = self.cfg
        k = dpi_scale if cfg["scale_thresholds_with_dpi"] else 1.0
        if self.last_x is not None:
            dx = x - self.last_x
            if abs(dx) > 3000 * k:  # teleport (e.g. SetCursorPos / monitor jump): start over
                self.moves.clear()
            elif dx:
                self.moves.append((t, dx))
        self.last_x = x

        window = cfg["shake_window_ms"] / 1000.0
        while self.moves and t - self.moves[0][0] > window:
            self.moves.popleft()

        min_seg = cfg["min_segment_px"] * k
        travel = 0
        reversals = 0
        seg_dir = seg_len = 0      # current raw stroke
        confirmed = 0              # last stroke direction that exceeded min_seg
        for _, d in self.moves:
            travel += abs(d)
            s = 1 if d > 0 else -1
            if s == seg_dir:
                seg_len += abs(d)
            else:
                seg_dir, seg_len = s, abs(d)
            if seg_len >= min_seg and s != confirmed:
                if confirmed:
                    reversals += 1
                confirmed = s
        return reversals >= cfg["min_reversals"] and travel >= cfg["min_travel_px"] * k


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------
def make_tray_image(enabled=True):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill=(40, 120, 230, 255) if enabled else (130, 130, 130, 255))
    pts = [(22 + x * 2, 12 + y * 2) for x, y in CursorArt.ARROW]
    d.polygon(pts, fill=(255, 255, 255, 255))
    return img


class App:
    def __init__(self):
        self.cfg = load_config()
        self.enabled = self.cfg["enabled"]
        self.stop = threading.Event()
        self.reload_requested = threading.Event()
        self.icon = None

    # -- tray callbacks (run on pystray's thread; only flip flags) --
    def _toggle(self, icon, item):
        self.enabled = not self.enabled
        self.cfg["enabled"] = self.enabled
        save_config(self.cfg)
        icon.icon = make_tray_image(self.enabled)
        icon.title = f"Shake to Find {__version__} ({'on' if self.enabled else 'off'})"

    def _reload(self, icon, item):
        self.reload_requested.set()

    def _open_config(self, icon, item):
        os.startfile(CONFIG_PATH)

    def _quit(self, icon, item):
        self.stop.set()
        icon.stop()

    def start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Enabled", self._toggle, checked=lambda i: self.enabled, default=True),
            pystray.MenuItem("Reload config", self._reload),
            pystray.MenuItem("Open config.json", self._open_config),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self.icon = pystray.Icon("ShakeToFind", make_tray_image(self.enabled),
                                 f"Shake to Find {__version__} ({'on' if self.enabled else 'off'})", menu)
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    # -- main loop (main thread: owns the overlay window) --
    def run(self):
        art = CursorArt()
        overlay = Overlay()
        detector = ShakeDetector(self.cfg)
        base_px = self.cfg["base_cursor_px"] or system_cursor_base_px()
        self.start_tray()

        scale = 1.0           # current animated scale (1.0 = normal cursor size)
        last_shake = -1e9
        msg = wt.MSG()
        pt = wt.POINT()
        winmm.timeBeginPeriod(1)  # 1 ms timer resolution so sleep(0.01) is accurate
        try:
            prev = next_tick = time.perf_counter()
            while not self.stop.is_set():
                # Keep the overlay's message queue serviced.
                while PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    TranslateMessage(ctypes.byref(msg))
                    DispatchMessageW(ctypes.byref(msg))

                if self.reload_requested.is_set():
                    self.reload_requested.clear()
                    self.cfg = load_config()
                    self.enabled = self.cfg["enabled"]
                    detector = ShakeDetector(self.cfg)
                    base_px = self.cfg["base_cursor_px"] or system_cursor_base_px()

                now = time.perf_counter()
                dt, prev = now - prev, now
                cfg = self.cfg

                if not self.enabled:
                    overlay.hide()
                    detector.reset()
                    scale = 1.0
                elif GetCursorPos(ctypes.byref(pt)):  # fails on secure desktop / lock screen
                    dpi = monitor_dpi(pt.x, pt.y)
                    if detector.feed(now, pt.x, dpi / 96.0):
                        last_shake = now

                    big = (now - last_shake) * 1000.0 < cfg["hold_ms"]
                    target = cfg["cursor_scale"] if big else 1.0
                    rate = cfg["grow_speed"] if target > scale else cfg["shrink_speed"]
                    scale += (target - scale) * (1.0 - math.exp(-rate * dt))

                    if not big and scale < 1.03:
                        scale = 1.0
                        overlay.hide()
                    elif big or overlay.visible:
                        size = base_px * dpi / 96.0 * scale
                        overlay.show_frame(art.frame(size), pt.x, pt.y)

                # Fixed-rate polling.
                next_tick += cfg["poll_interval_ms"] / 1000.0
                delay = next_tick - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_tick = time.perf_counter()
        except KeyboardInterrupt:
            pass
        finally:
            winmm.timeEndPeriod(1)
            overlay.destroy()
            if self.icon is not None:
                self.icon.stop()


def main():
    # Single instance guard.
    _mutex = CreateMutexW(None, False, "Local\\ShakeToFindCursorMutex")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        print("Shake to Find is already running.", file=sys.stderr)
        return 1
    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
