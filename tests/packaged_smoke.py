"""Windows-only integration check of the packaged executable and tray mode."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from tempfile import TemporaryDirectory

import psutil
from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness_updater.process import terminate_tree


def main():
    if os.name != "nt":
        raise SystemExit("This packaged integration test requires Windows.")
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    project = Path(__file__).resolve().parents[1]
    executable = project / "dist/HarnessUpdater.exe"
    state_root = project / ".gui-state"
    state_root.mkdir(parents=True, exist_ok=True)
    state_context = TemporaryDirectory(prefix="packaged-", dir=state_root)
    state = Path(state_context.name)
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    process = subprocess.Popen([str(executable), "--state-dir", str(state)])
    try:
        window = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"Packaged executable exited early: {process.returncode}")
            children = psutil.Process(process.pid).children(recursive=True)
            owned = {process.pid, *(child.pid for child in children)}
            found = []

            def visit(hwnd, _):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value in owned and user32.IsWindowVisible(hwnd):
                    title = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd, title, len(title))
                    if title.value == "Harness 更新助手":
                        found.append(hwnd)
                return True

            user32.EnumWindows(callback_type(visit), 0)
            if found:
                window = found[0]
                break
            time.sleep(0.1)
        assert window, "No packaged GUI window appeared"
        logfile = state / "logs/updater.log"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if logfile.exists() and "系统托盘已就绪" in logfile.read_text(encoding="utf-8"):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("System tray did not become ready")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            # Capture this HWND directly, even if another window covers it.
            screenshot = ImageGrab.grab(window=window)
            # Win32 may report a Tk window before its first paint has completed.
            center = screenshot.crop((24, 90, screenshot.width - 24, screenshot.height - 80))
            dark_pixels = sum(value < 130 for value in center.convert("L").tobytes())
            if dark_pixels >= 100:
                screenshot.save(project / "docs/images/gui-packaged.png")
                break
            time.sleep(0.25)
        else:
            raise AssertionError("Packaged GUI did not finish drawing")
        # Closing the actual bundled window should leave the daemon in its tray.
        user32.PostMessageW(window, 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(0.5)
        assert process.poll() is None, "Closing the window unexpectedly exited the daemon"
        assert not user32.IsWindowVisible(window), "Closing the window did not hide it to the tray"
        print("Packaged smoke passed: bundled Tk / GUI window / tray initialization / close-to-background")
    finally:
        terminate_tree(process)
        state_context.cleanup()


if __name__ == "__main__":
    main()
