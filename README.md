# Shake to Find Cursor (Windows)

A Windows tray app that copies macOS's "shake mouse pointer to locate" feature. When you shake the mouse left and right, the pointer grows to about 5× its normal size. It shrinks back after about 1 second without shaking.

```
shake-to-find/
├── main.py                       # the whole app (detector, overlay window, tray icon)
├── requirements.txt              # Pillow, pystray
├── tests/test_main.py            # unit tests (unittest)
├── .github/workflows/            # CI + release-please
├── release-please-config.json    # release-please settings
├── config.json                   # created on first run next to main.py / the .exe
└── README.md
```

## Download

Get `ShakeToFind.exe` from the [latest release](https://github.com/hans-tsang/shake-to-find/releases/latest).

## Releases (CI/CD)

* **CI** (`ci.yml`): every push and pull request runs the unit tests and builds the exe on `windows-latest`. You can download the built exe from the run's artifacts.
* **Releases** (`release-please.yml`): use [Conventional Commits](https://www.conventionalcommits.org/), for example `feat: ...`, `fix: ...`, or `feat!: ...` for a breaking change. On every push to `main`, release-please opens or updates a release PR with the new version and changelog. When you merge that PR, it tags the version, creates a GitHub Release, builds `ShakeToFind.exe`, and attaches the exe to the release.

## Run

```powershell
cd shake-to-find
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python main.py      # or pythonw main.py to run without a console
```

To use the tray icon, right-click it and choose **Enabled** (toggle), **Run at startup**, **Reload config**, **Open config.json**, or **Quit**. Double-clicking the icon also toggles it on or off. Only one copy of the app runs at a time.

**Run at startup** adds a per-user entry under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, so it doesn't need admin rights. If you move the exe later, the entry is corrected the next time the app starts.

## Build a single .exe

```powershell
.\.venv\Scripts\pip install pyinstaller
.\.venv\Scripts\pyinstaller --noconfirm --onefile --noconsole --name ShakeToFind --hidden-import pystray._win32 main.py
```

The output is `dist\ShakeToFind.exe`. It creates `config.json` in the same folder as the exe. The first launch can take a few seconds while the onefile package unpacks. To start it with Windows, turn on **Run at startup** in the tray menu.

## config.json

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | Turns detection on or off. The tray toggle also changes this setting. |
| `poll_interval_ms` | `10` | How often the app checks the cursor position. |
| `shake_window_ms` | `600` | The sliding time window used to detect a shake. |
| `min_reversals` | `4` | How many times the horizontal direction must change within the window. |
| `min_travel_px` | `800` | The total horizontal distance the cursor must travel within the window. |
| `min_segment_px` | `15` | Shorter strokes count as jitter and are ignored. |
| `scale_thresholds_with_dpi` | `true` | Multiplies the px thresholds by the monitor's DPI ÷ 96. |
| `cursor_scale` | `5.0` | The enlarged size compared with the normal cursor. |
| `base_cursor_px` | `0` | The normal cursor size at 96 DPI. `0` reads the size from your Windows pointer-size setting. |
| `hold_ms` | `1000` | How long the cursor stays enlarged after the last shake. |
| `grow_speed` / `shrink_speed` | `18` / `7` | Animation speeds. Higher values are faster. |

To lower the sensitivity, raise `min_travel_px` or `min_reversals`. After you edit the file, choose **Reload config** from the tray menu.

## Notes

* The process is per-monitor DPI aware (v2), so the app works in physical pixels across the whole virtual desktop. The arrow size and thresholds adjust to the DPI of the monitor the cursor is on.
* The overlay is a layered window with per-pixel alpha. It is click-through (`WS_EX_TRANSPARENT`), stays on top, takes no focus, and doesn't show in the taskbar. The tip of the enlarged arrow sits exactly on the real cursor's hotspot.
* The overlay can't appear above elevated (admin) windows or on secure desktops such as UAC prompts and the lock screen.
