"""Exercise actual Tk widgets and export screenshots for visual inspection.

Run from the repository root: python tests/gui_smoke.py
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import ImageGrab

from harness_updater.core import Commit, Snapshot
from harness_updater.gui import Application
import harness_updater.gui as gui_module
from harness_updater.storage import Settings


def main():
    if os.name == "nt":
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    project = Path(__file__).resolve().parents[1]
    screenshots = project / "docs/images"
    screenshots.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    # Use real widgets while suppressing OS tray registration in this UI test.
    original = Application._start_tray
    Application._start_tray = lambda self: None
    try:
        app = Application(root, Settings(watch=False), project / ".gui-state")
    finally:
        Application._start_tray = original
    root.update()
    assert app.update_button.instate(["disabled"])
    assert app.check_button.instate(["!disabled"])

    def capture(name):
        root.update_idletasks()
        root.update()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        bounds = (x, y, x + root.winfo_width(), y + root.winfo_height())
        ImageGrab.grab(bbox=bounds).save(screenshots / name)

    capture("gui-empty.png")
    local = Commit("b7e521a12345" + "0" * 28, "chore: prepare previous source build", "2026-09-23T09:30:00+08:00")
    remote = Commit("477b4f420553e8a52c2fbccc464d7561b239c443", "Merge pull request #5180 from deepseek-harness/rel/dsh-0.1.7-rc.2", "2026-09-24T13:39:59+00:00")
    fixture = Snapshot(
        r"D:\Apps\deepseek-harness", local, remote, branch="master", target_branch="master",
        relation="behind", behind=8, build_commit="b7e521a", build_current=True,
        commits=[remote], checked_at="2026-09-27T10:00:00+08:00",
    )
    app.directory.set(fixture.directory)
    app._render(fixture)
    app._log("界面演示数据：本地落后 8 个 Commit，准备更新。")
    assert app.update_button.instate(["!disabled"])
    capture("gui-update.png")
    app._set_busy(True)
    assert app.path_entry.instate(["disabled"])
    assert app.update_button.instate(["disabled"])
    assert app.cancel_button.instate(["!disabled"])
    app._set_busy(False)
    fixture.reasons = ["目录里有本地修改或未跟踪文件。请先备份并处理这些文件，再更新。"]
    app._render(fixture)
    assert app.update_button.instate(["disabled"])
    root.geometry("820x700")
    capture("gui-protected-small.png")
    app.page_canvas.yview_moveto(1.0)
    capture("gui-protected-small-bottom.png")
    assert app.recover_button.winfo_rooty() < root.winfo_rooty() + root.winfo_height()
    fixture.recovery_needed = True
    app._render(fixture)
    assert app.recover_button.instate(["!disabled"])

    # Exercise the real worker/event queue and post-failure UI, while the core
    # Git transaction itself is covered by test_core's temporary repositories.
    original_engine = gui_module.Engine

    class FailingEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        def update(self, **_kwargs):
            raise RuntimeError("测试构建失败，需要恢复旧版本")

        def inspect_local(self):
            return fixture

    gui_module.Engine = FailingEngine
    app.auto.set(True)
    app._persist()
    app.port.set("unsaved invalid port")
    try:
        app._start("update", automatic=True)
        deadline = time.monotonic() + 5
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.05)
        assert not app.busy
        assert not app.auto.get(), "Failure must disable automatic retry"
        assert app.settings.service_port == 3080, "Automatic work must use saved settings"
        assert app.recover_button.instate(["!disabled"])
        assert app.local_sha.cget("text") == fixture.local.short
    finally:
        gui_module.Engine = original_engine
    app._hide()
    root.update()
    assert root.state() == "iconic"
    root.deiconify()
    root.update()
    app._exit()
    print("GUI smoke passed: empty / update / protection / busy / recovery / scrolling / worker failure / minimize")


if __name__ == "__main__":
    main()
