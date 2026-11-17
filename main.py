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
  per-pixel-alpha layered window) draws a large macOS-style arrow (gradient
  fill, white outline, soft glow and drop shadow) whose tip sits exactly on the
  real cursor hotspot, so it follows the real cursor. It pops in with a springy
  bounce, and ripple rings pulse out from the tip while shaking continues.
* After `hold_ms` without shaking, a critically damped spring shrinks the arrow
  smoothly back to normal size and the overlay is hidden.
* A pystray tray icon (running in its own thread) offers Enable/Disable,
  reload/open config and Quit.

The process is Per-Monitor-DPI-Aware (v2), so all coordinates are physical
pixels in the virtual-screen space (works across multiple monitors) and the
cursor size / thresholds are scaled by the DPI of the monitor under the cursor.
"""

__version__ = "0.3.0"  # x-release-please-version

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
    # Spring animation: stiffness (higher = faster), damping ratio (<1 = bouncy overshoot).
    "grow_stiffness": 260.0,
    "grow_damping": 0.45,
    "shrink_stiffness": 90.0,
    "shrink_damping": 1.0,
    # Look (RGB colors; defaults mimic macOS: black arrow, white outline, soft shadow).
    "fill_top_color": [72, 72, 78],      # arrow gradient, top
    "fill_bottom_color": [0, 0, 0],      # arrow gradient, bottom
    "outline_color": [255, 255, 255],
    "glow_color": [255, 255, 255],
    "glow_opacity": 0.55,                # 0 disables the glow
    "shadow_opacity": 0.45,              # 0 disables the drop shadow
    # Ripple rings that pulse out from the cursor tip while shaking.
    "ripple": True,
    "ripple_color": [255, 255, 255],
    "ripple_duration_ms": 650,
    "ripple_interval_ms": 700,           # repeat while shaking continues
    "ripple_max_radius": 1.25,           # relative to the enlarged arrow size
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
                elif (isinstance(default, list) and isinstance(val, list) and len(val) == len(default)
                      and all(isinstance(v, (int, float)) for v in val)):
                    cfg[key] = [int(max(0, min(255, v))) for v in val]
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
# Run at startup (per-user HKCU Run key; no admin rights needed)
# ---------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ShakeToFind"


def startup_command():
    """Command line Windows runs at sign-in: the .exe, or pythonw + this script."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{os.path.abspath(__file__)}"'


def get_startup_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_NAME)[0]
    except OSError:
        return None


def is_startup_enabled():
    return get_startup_value() is not None


def set_startup(enabled):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


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
def _scaled_mask(mask, factor):
    """Multiply an 'L' mask by factor (0..1)."""
    factor = max(0.0, min(1.0, factor))
    return mask.point(lambda v: int(v * factor))


def to_frame(img, hot_x, hot_y):
    """PIL RGBA image -> (premultiplied BGRA bytes, w, h, hot_x, hot_y) for UpdateLayeredWindow."""
    r, g, b, a = img.convert("RGBa").split()
    return (Image.merge("RGBA", (b, g, r, a)).tobytes(), img.width, img.height, hot_x, hot_y)


class CursorArt:
    """Renders an anti-aliased macOS-style arrow once at high resolution
    (soft glow, drop shadow, white outline, gradient fill), then produces
    frames at any size (cached)."""

    MASTER = 512  # master canvas is MASTER x MASTER
    # Arrow outline in "units"; tip (hotspot) at (0, 0). ~ classic pointer shape.
    ARROW = [(0, 0), (0, 17.5), (4.2, 13.6), (7.0, 20.2), (9.6, 19.1),
             (6.9, 12.6), (12.4, 12.6)]
    UNITS = 32         # a normal cursor of N px spans 32 units (like a 32x32 cursor)
    CANVAS_UNITS = 40  # canvas is larger than the cursor so the glow/shadow aren't clipped
    PAD = 6.0          # units from the canvas top-left corner to the tip

    def __init__(self, cfg=None):
        cfg = cfg or DEFAULT_CONFIG
        M = self.MASTER
        s = M / self.CANVAS_UNITS
        self.hotspot = self.PAD / self.CANVAS_UNITS  # fraction of canvas size
        pts = [((x + self.PAD) * s, (y + self.PAD) * s) for x, y in self.ARROW]
        outline = 1.3 * s

        def shape(dx=0.0, dy=0.0, grow=0.0, blur=0.0):
            """'L' mask of the arrow, optionally offset, grown (rounded) and blurred."""
            m = Image.new("L", (M, M), 0)
            d = ImageDraw.Draw(m)
            p = [(x + dx, y + dy) for x, y in pts]
            d.polygon(p, fill=255)
            if grow:
                d.line(p + p[:1], fill=255, width=int(grow * 2), joint="curve")
                for x, y in p:  # round the outer corners
                    d.ellipse([x - grow, y - grow, x + grow, y + grow], fill=255)
            return m.filter(ImageFilter.GaussianBlur(blur)) if blur else m

        def layer(color, mask):
            im = Image.new("RGBA", (M, M), tuple(color) + (0,))
            im.putalpha(mask)
            return im

        img = Image.new("RGBA", (M, M), (0, 0, 0, 0))
        # 1. Soft halo so the arrow pops on dark backgrounds.
        if cfg["glow_opacity"] > 0:
            glow = shape(grow=outline * 1.8, blur=1.1 * s)
            img.alpha_composite(layer(cfg["glow_color"], _scaled_mask(glow, cfg["glow_opacity"])))
        # 2. Drop shadow (down-right) so it pops on light backgrounds.
        if cfg["shadow_opacity"] > 0:
            shadow = shape(dx=0.5 * s, dy=0.9 * s, grow=outline, blur=0.9 * s)
            img.alpha_composite(layer((0, 0, 0), _scaled_mask(shadow, cfg["shadow_opacity"])))
        # 3. Rounded outline.
        img.alpha_composite(layer(cfg["outline_color"], shape(grow=outline)))
        # 4. Vertical gradient fill clipped to the arrow.
        ys = [y for _, y in pts]
        top, bottom = int(min(ys)), int(max(ys)) + 1
        grad = Image.new("L", (M, M), 255)
        grad.paste(Image.linear_gradient("L").resize((M, bottom - top)), (0, top))
        fill = Image.composite(Image.new("RGBA", (M, M), tuple(cfg["fill_bottom_color"]) + (255,)),
                               Image.new("RGBA", (M, M), tuple(cfg["fill_top_color"]) + (255,)), grad)
        fill.putalpha(shape())
        img.alpha_composite(fill)

        self.master = img
        self._images = {}
        self._frames = {}

    def image(self, size):
        """Return (RGBA canvas image, hotspot_px) for an arrow of cursor size `size`."""
        size = max(8, int(round(size)))
        hit = self._images.get(size)
        if hit is None:
            if len(self._images) > 256:
                self._images.clear()
            px = int(round(size * self.CANVAS_UNITS / self.UNITS))
            hit = self._images[size] = (self.master.resize((px, px), Image.LANCZOS),
                                        int(round(self.hotspot * px)))
        return hit

    def frame(self, size):
        """Return a cached frame (see to_frame) for an arrow of cursor size `size`."""
        size = max(8, int(round(size)))
        hit = self._frames.get(size)
        if hit is None:
            img, hot = self.image(size)
            if len(self._frames) > 256:
                self._frames.clear()
            hit = self._frames[size] = to_frame(img, hot, hot)
        return hit


def compose_ripples(art, size, ripples, now, cfg):
    """Frame with the arrow plus expanding, fading rings centred on the tip.
    `ripples` is a list of spawn times (seconds)."""
    arrow, hot = art.image(size)
    dur = cfg["ripple_duration_ms"] / 1000.0
    max_r = cfg["ripple_max_radius"] * size
    ring_w = max(2.0, size * 0.035)
    half = int(max(max_r + ring_w * 2, arrow.width)) + 2  # canvas half-size, tip at centre
    ss = 2  # supersample for anti-aliased rings
    dark = Image.new("L", (half * 2 * ss,) * 2, 0)
    light = Image.new("L", (half * 2 * ss,) * 2, 0)
    dd, dl = ImageDraw.Draw(dark), ImageDraw.Draw(light)
    c = half * ss
    for t0 in ripples:
        p = min(1.0, max(0.0, (now - t0) / dur))
        r = (0.15 + 0.85 * (1 - (1 - p) ** 3)) * max_r * ss  # ease-out cubic
        alpha = (1 - p) ** 2
        w = ring_w * ss * (1.0 - 0.5 * p)
        dd.ellipse([c - r - w, c - r - w, c + r + w, c + r + w],
                   outline=int(120 * alpha), width=max(1, int(w * 2)))
        dl.ellipse([c - r, c - r, c + r, c + r], outline=int(235 * alpha), width=max(1, int(w)))
    size_px = (half * 2, half * 2)
    canvas = Image.new("RGBA", size_px, (0, 0, 0, 0))
    shade = Image.new("RGBA", size_px, (0, 0, 0, 0))
    shade.putalpha(dark.resize(size_px, Image.BOX))
    canvas.alpha_composite(shade)
    ring = Image.new("RGBA", size_px, tuple(cfg["ripple_color"]) + (0,))
    ring.putalpha(light.resize(size_px, Image.BOX))
    canvas.alpha_composite(ring)
    canvas.alpha_composite(arrow, (half - hot, half - hot))
    return to_frame(canvas, half, half)


def spring_step(x, v, target, stiffness, damping_ratio, dt):
    """One semi-implicit Euler step of a damped spring. Returns (x, v)."""
    c = 2.0 * damping_ratio * math.sqrt(stiffness)
    v += (stiffness * (target - x) - c * v) * dt
    return x + v * dt, v


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
        data, w, h, hot_x, hot_y = frame
        dst = wt.POINT(x - hot_x, y - hot_y)
        if frame is self._last_frame:
            # Same bitmap: just move the window (cheap).
            SetWindowPos(self.hwnd, HWND_TOPMOST, dst.x, dst.y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
        else:
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0)
            bits = ctypes.c_void_p()
            hbmp = CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            if not hbmp:
                return
            ctypes.memmove(bits, data, len(data))
            old = SelectObject(self.mem_dc, hbmp)
            UpdateLayeredWindow(self.hwnd, self.screen_dc, ctypes.byref(dst),
                                ctypes.byref(wt.SIZE(w, h)), self.mem_dc,
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

    def _toggle_startup(self, icon, item):
        try:
            set_startup(not is_startup_enabled())
        except OSError as e:
            icon.notify(f"Could not change startup setting: {e}", "Shake to Find")

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
            pystray.MenuItem("Run at startup", self._toggle_startup,
                             checked=lambda i: is_startup_enabled()),
            pystray.Menu.SEPARATOR,
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
        art = CursorArt(self.cfg)
        overlay = Overlay()
        detector = ShakeDetector(self.cfg)
        base_px = self.cfg["base_cursor_px"] or system_cursor_base_px()
        self.start_tray()

        scale, vel = 1.0, 0.0  # spring-animated scale (1.0 = normal cursor size) and its velocity
        last_shake = -1e9
        ripples = []           # spawn times of active ripple rings
        last_ripple = -1e9
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
                    art = CursorArt(self.cfg)

                now = time.perf_counter()
                dt, prev = min(now - prev, 0.05), now  # clamp so the spring stays stable after stalls
                cfg = self.cfg

                if not self.enabled:
                    overlay.hide()
                    detector.reset()
                    scale, vel, ripples = 1.0, 0.0, []
                elif GetCursorPos(ctypes.byref(pt)):  # fails on secure desktop / lock screen
                    dpi = monitor_dpi(pt.x, pt.y)
                    if detector.feed(now, pt.x, dpi / 96.0):
                        last_shake = now
                        if cfg["ripple"] and (now - last_ripple) * 1000.0 >= cfg["ripple_interval_ms"]:
                            ripples.append(now)
                            last_ripple = now

                    big = (now - last_shake) * 1000.0 < cfg["hold_ms"]
                    if big:
                        target, k, zeta = cfg["cursor_scale"], cfg["grow_stiffness"], cfg["grow_damping"]
                    else:
                        target, k, zeta = 1.0, cfg["shrink_stiffness"], cfg["shrink_damping"]
                    scale, vel = spring_step(scale, vel, target, k, zeta, dt)
                    scale = max(scale, 0.5)
                    ripples = [t0 for t0 in ripples
                               if (now - t0) * 1000.0 < cfg["ripple_duration_ms"]]

                    if not big and not ripples and abs(scale - 1.0) < 0.03 and abs(vel) < 0.5:
                        scale, vel = 1.0, 0.0
                        overlay.hide()
                    elif big or overlay.visible:
                        size = base_px * dpi / 96.0 * scale
                        frame = (compose_ripples(art, size, ripples, now, cfg) if ripples
                                 else art.frame(size))
                        overlay.show_frame(frame, pt.x, pt.y)

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
    # If startup is enabled but the app was moved/rebuilt elsewhere, fix the path.
    value = get_startup_value()
    if value is not None and value != startup_command():
        try:
            set_startup(True)
        except OSError:
            pass
    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
