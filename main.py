from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

from harness_updater import __version__
from harness_updater.core import Engine, snapshot_dict
from harness_updater.process import Runner, redact
from harness_updater.storage import FileLock, Settings, UpdaterError, default_state_dir


def configure_logging(state_dir: Path) -> None:
    directory = state_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "updater.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def daemon(settings: Settings, state_dir: Path) -> int:
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())

    def sink(kind, value):
        if kind == "log":
            logging.info(redact(str(value)))

    logging.info("守护程序启动；间隔 %s 分钟；自动更新 %s", settings.interval_minutes, settings.auto_update)
    while not stop.is_set():
        try:
            snapshot = Engine(settings, state_dir, Runner(sink, stop)).check()
            logging.info("本机 %s，远端 %s，关系 %s", snapshot.local.short, snapshot.remote.short, snapshot.relation)
            if settings.auto_update and snapshot.can_auto_update:
                Engine(settings, state_dir, Runner(sink, stop)).update(automatic=True)
            elif snapshot.reasons:
                logging.info("等待处理：%s", "；".join(snapshot.reasons))
        except Exception as exc:
            logging.error(redact(str(exc)))
        stop.wait(settings.interval_minutes * 60)
    logging.info("守护程序已退出")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harness 更新助手：源码版本检查、GUI 与自动更新守护程序")
    parser.add_argument("--version", action="version", version=__version__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="检查版本并输出 JSON；不修改源码文件")
    modes.add_argument("--update", action="store_true", help="执行一次更新或构建修复")
    modes.add_argument("--daemon", action="store_true", help="以无窗口守护程序运行")
    parser.add_argument("--directory", type=Path, help="Harness Git 源码根目录")
    parser.add_argument("--branch", help="跟踪分支；默认跟随官方默认分支")
    parser.add_argument("--interval", type=int, help="守护检查间隔，单位分钟")
    parser.add_argument("--port", type=int, help="Harness 服务端口，默认 3080")
    parser.add_argument("--auto-update", action="store_true", help="明确启用自动更新（默认只检查）")
    parser.add_argument("--state-dir", type=Path, help="独立配置、日志与工具缓存目录")
    parser.add_argument("--minimized", action="store_true", help="GUI 启动后进入后台")
    args = parser.parse_args(argv)
    state_dir = (args.state_dir or default_state_dir()).expanduser().resolve()
    interactive = not (args.check or args.update or args.daemon)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        configure_logging(state_dir)
        settings = Settings.load(state_dir / "settings.json")
        overrides = {}
        for argument, field in (("directory", "directory"), ("branch", "branch"), ("interval", "interval_minutes"), ("port", "service_port")):
            value = getattr(args, argument)
            if value is not None:
                overrides[field] = str(value.resolve()) if argument == "directory" else value
        if args.auto_update:
            overrides["auto_update"] = True
        settings = replace(settings, **overrides)
        settings.validate()
        if args.check:
            runner = Runner(lambda kind, value: logging.info(redact(str(value))) if kind == "log" else None)
            print(json.dumps(snapshot_dict(Engine(settings, state_dir, runner).check()), ensure_ascii=False, indent=2))
            return 0
        with FileLock(state_dir / "instance.lock"):
            if args.daemon:
                if not settings.directory:
                    raise UpdaterError("守护模式需要先在 GUI 选择目录，或传入 --directory。")
                return daemon(settings, state_dir)
            if args.update:
                runner = Runner(lambda kind, value: logging.info(redact(str(value))) if kind == "log" else None)
                result = Engine(settings, state_dir, runner).update()
                print(json.dumps(snapshot_dict(result), ensure_ascii=False, indent=2))
                return 0
            from harness_updater.gui import launch

            launch(settings, state_dir, minimized=args.minimized)
        return 0
    except Exception as exc:
        message = redact(str(exc))
        logging.error(message)
        if interactive:
            try:
                import tkinter as tk
                from tkinter import messagebox

                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("Harness 更新助手", message, parent=root)
                root.destroy()
            except Exception:
                pass
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    if os.name == "nt" and sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
