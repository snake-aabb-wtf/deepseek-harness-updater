from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .core import Engine, Snapshot
from .process import Runner, redact
from .storage import Settings, UpdaterError

BG = "#f3f5f8"
WHITE = "#ffffff"
INK = "#1d2939"
MUTED = "#667085"
BLUE = "#2563eb"
GREEN = "#157347"
AMBER = "#9a6700"


def open_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def startup_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.QueryValueEx(key, "DeepSeekHarnessUpdater")
            return True
    except FileNotFoundError:
        return False


def set_startup(enabled: bool, state_dir: Path) -> None:
    if os.name != "nt":
        return
    import winreg

    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
        if not enabled:
            try:
                winreg.DeleteValue(key, "DeepSeekHarnessUpdater")
            except FileNotFoundError:
                pass
            return
        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            interpreter = Path(sys.executable)
            windowed = interpreter.with_name("pythonw.exe")
            command = [str(windowed if windowed.exists() else interpreter), str(Path(__file__).resolve().parents[1] / "main.py")]
        command.extend(["--minimized", "--state-dir", str(state_dir)])
        winreg.SetValueEx(key, "DeepSeekHarnessUpdater", 0, winreg.REG_SZ, subprocess.list2cmdline(command))


class Application:
    def __init__(self, root: tk.Tk, settings: Settings, state_dir: Path, *, minimized: bool = False):
        self.root = root
        self.settings = settings
        self.state_dir = state_dir
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.snapshot: Snapshot | None = None
        self.next_check = time.monotonic() + settings.interval_minutes * 60
        self.tray = None
        self.tray_ready = False
        self.last_notification = ""
        self.action = ""
        self.closing = False
        self.directory = tk.StringVar(value=settings.directory)
        self.branch = tk.StringVar(value=settings.branch)
        self.interval = tk.StringVar(value=str(settings.interval_minutes))
        self.port = tk.StringVar(value=str(settings.service_port))
        self.watch = tk.BooleanVar(value=settings.watch)
        self.auto = tk.BooleanVar(value=settings.auto_update)
        self.startup = tk.BooleanVar(value=startup_enabled())
        self._configure_window()
        self._build()
        self._start_tray()
        self.root.protocol("WM_DELETE_WINDOW", self._hide)
        self.root.after(100, self._drain)
        self.root.after(1000, self._tick)
        if minimized:
            self.root.after(300, self._hide)
        if settings.directory:
            self.root.after(400, lambda: self._start("check"))

    def _configure_window(self):
        self.root.title("Harness 更新助手")
        self.root.configure(bg=BG)
        width = min(1040, self.root.winfo_screenwidth() - 80)
        height = min(850, self.root.winfo_screenheight() - 100)
        left = max(0, (self.root.winfo_screenwidth() - width) // 2)
        top = max(0, (self.root.winfo_screenheight() - height) // 2 - 30)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.root.minsize(820, 700)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        font = "Microsoft YaHei UI" if os.name == "nt" else "TkDefaultFont"
        self.font = font
        self.root.option_add("*Font", (font, 10))
        style.configure("TFrame", background=BG)
        style.configure("Surface.TFrame", background=WHITE)
        style.configure("TLabel", background=BG, foreground=INK, font=(font, 10))
        style.configure("Muted.TLabel", foreground=MUTED, background=BG, font=(font, 9))
        style.configure("Surface.TLabel", background=WHITE, foreground=INK)
        style.configure("Caption.TLabel", background=WHITE, foreground=MUTED, font=(font, 9))
        style.configure("Commit.TLabel", background=WHITE, foreground=INK, font=("Consolas", 20, "bold"))
        style.configure("TButton", padding=(13, 7), font=(font, 10), borderwidth=1)
        style.configure("Primary.TButton", background=BLUE, foreground=WHITE, borderwidth=0, padding=(24, 9))
        style.map("Primary.TButton", background=[("disabled", "#d0d5dd"), ("active", "#1d4ed8")], foreground=[("disabled", "#667085")])
        style.configure("TEntry", padding=7, fieldbackground=WHITE)
        style.configure("TCombobox", padding=6)
        style.configure("TCheckbutton", background=BG, foreground=INK)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("Blue.Horizontal.TProgressbar", background=BLUE, troughcolor="#e4e7ec", borderwidth=0, thickness=5)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

    def _build(self):
        # The complete form remains reachable on small screens and with long
        # status messages; the log also retains its independent scrollbar.
        canvas = tk.Canvas(self.root, bg=BG, borderwidth=0, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        page_scroll = ttk.Scrollbar(self.root, orient="vertical", command=canvas.yview)
        page_scroll.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=page_scroll.set)
        content = ttk.Frame(canvas, padding=(24, 18, 24, 12))
        window = canvas.create_window((0, 0), window=content, anchor="nw")

        def resize(_=None):
            canvas.itemconfigure(window, width=canvas.winfo_width(), height=max(canvas.winfo_height(), content.winfo_reqheight()))
            canvas.configure(scrollregion=canvas.bbox("all"))

        content.bind("<Configure>", resize)
        canvas.bind("<Configure>", resize)
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
        self.page_canvas = canvas
        content.columnconfigure(0, weight=1)
        content.rowconfigure(7, weight=1)
        header = ttk.Frame(content)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Harness 更新助手", font=(self.font, 22, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="选择源码目录，检查版本，一键完成依赖安装与构建。", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, text=f"v{__version__}", style="Muted.TLabel").grid(row=0, column=1, sticky="e")

        location = ttk.Frame(content)
        location.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        location.columnconfigure(0, weight=1)
        ttk.Label(location, text="deepseek-harness 安装目录", font=(self.font, 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.path_entry = ttk.Entry(location, textvariable=self.directory)
        self.path_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.path_entry.bind("<Return>", lambda _: self._start("check"))
        self.browse_button = ttk.Button(location, text="选择目录…", command=self._browse)
        self.browse_button.grid(row=1, column=1)
        self.check_button = ttk.Button(location, text="检查更新", command=lambda: self._start("check"))
        self.check_button.grid(row=1, column=2, padx=(8, 0))
        ttk.Label(location, text="选择包含 .git 和 package.json 的源码根目录。", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(4, 0))

        versions = ttk.Frame(content, style="Surface.TFrame", padding=(20, 14))
        versions.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        versions.columnconfigure(0, weight=1, uniform="versions")
        versions.columnconfigure(2, weight=1, uniform="versions")
        self.local_sha, self.local_subject, self.local_date = self._commit_panel(versions, 0, "本机 Commit")
        ttk.Separator(versions, orient="vertical").grid(row=0, column=1, rowspan=4, sticky="ns", padx=20)
        self.remote_sha, self.remote_subject, self.remote_date = self._commit_panel(versions, 2, "官方最新 Commit")
        self.version_detail = ttk.Label(versions, text="尚未选择安装目录", style="Caption.TLabel")
        self.version_detail.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        versions.bind("<Configure>", lambda event: self._wrap_versions(event.width))

        status = ttk.Frame(content)
        status.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        status.columnconfigure(0, weight=1)
        self.status_title = ttk.Label(status, text="先选择安装目录", font=(self.font, 13, "bold"))
        self.status_title.grid(row=0, column=0, sticky="w")
        self.status_detail = ttk.Label(status, text="检查会读取本机版本，并连接 GitHub 查询官方最新提交。", style="Muted.TLabel", wraplength=650, justify="left")
        self.status_detail.grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.update_button = ttk.Button(status, text="一键更新", style="Primary.TButton", command=lambda: self._start("update"), state="disabled")
        self.update_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(16, 0))
        status.bind("<Configure>", lambda event: self.status_detail.configure(wraplength=max(360, event.width - 210)))

        options = ttk.Frame(content)
        options.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        options.columnconfigure(3, weight=1)
        ttk.Label(options, text="后台守护", font=(self.font, 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.watch_check = ttk.Checkbutton(options, text="定时检查", variable=self.watch, command=self._persist)
        self.watch_check.grid(row=1, column=0, sticky="w")
        self.interval_box = ttk.Combobox(options, textvariable=self.interval, values=("5", "15", "30", "60", "120"), width=5, state="readonly")
        self.interval_box.grid(row=1, column=1, padx=(10, 5))
        self.interval_box.bind("<<ComboboxSelected>>", lambda _: self._persist())
        ttk.Label(options, text="分钟", style="Muted.TLabel").grid(row=1, column=2, sticky="w")
        self.auto_check = ttk.Checkbutton(options, text="发现新版本时自动更新", variable=self.auto, command=self._persist)
        self.auto_check.grid(row=1, column=3, sticky="w", padx=(20, 0))
        self.startup_check = ttk.Checkbutton(options, text="登录 Windows 后启动", variable=self.startup, command=self._startup_changed)
        self.startup_check.grid(row=1, column=4, sticky="e")
        if os.name != "nt":
            self.startup_check.state(["disabled"])
        ttk.Label(options, text="自动更新会安装依赖并重建；有本地改动或 Harness 正在运行时，会等待处理。", style="Muted.TLabel").grid(row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))

        advanced = ttk.Frame(content)
        advanced.grid(row=5, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(advanced, text="目标分支", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.branch_entry = ttk.Entry(advanced, textvariable=self.branch, width=14)
        self.branch_entry.grid(row=0, column=1, padx=(8, 6))
        ttk.Label(advanced, text="留空跟随官方默认分支", style="Muted.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Label(advanced, text="服务端口", style="Muted.TLabel").grid(row=0, column=3, padx=(18, 8))
        self.port_entry = ttk.Entry(advanced, textvariable=self.port, width=6)
        self.port_entry.grid(row=0, column=4)
        self.save_button = ttk.Button(advanced, text="保存设置", command=self._persist)
        self.save_button.grid(row=0, column=5, padx=(12, 0))

        progress = ttk.Frame(content)
        progress.grid(row=6, column=0, sticky="ew", pady=(0, 8))
        progress.columnconfigure(0, weight=1)
        self.progress_label = ttk.Label(progress, text="等待检查", style="Muted.TLabel")
        self.progress_label.grid(row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(progress, text="取消操作", command=self._cancel, state="disabled")
        self.cancel_button.grid(row=0, column=1, rowspan=2, padx=(12, 0))
        self.progress = ttk.Progressbar(progress, style="Blue.Horizontal.TProgressbar", maximum=6)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(5, 0))

        notebook = ttk.Notebook(content)
        notebook.grid(row=7, column=0, sticky="nsew")
        log_page = ttk.Frame(notebook)
        commits_page = ttk.Frame(notebook)
        notebook.add(log_page, text="  操作日志  ")
        notebook.add(commits_page, text="  新版本提交  ")
        log_page.columnconfigure(0, weight=1)
        log_page.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_page, height=4, bg=WHITE, fg=INK, font=("Consolas", 10), relief="flat", padx=12, pady=10, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_page, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        commits_page.columnconfigure(0, weight=1)
        commits_page.rowconfigure(0, weight=1)
        self.commit_text = tk.Text(commits_page, height=4, bg=WHITE, fg=INK, font=(self.font, 10), relief="flat", padx=12, pady=10, wrap="word", state="disabled")
        self.commit_text.grid(row=0, column=0, sticky="nsew")
        commit_scroll = ttk.Scrollbar(commits_page, command=self.commit_text.yview)
        commit_scroll.grid(row=0, column=1, sticky="ns")
        self.commit_text.configure(yscrollcommand=commit_scroll.set)

        footer = ttk.Frame(content)
        footer.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        self.schedule_label = ttk.Label(footer, text="后台检查已开启" if self.watch.get() else "后台检查已暂停", style="Muted.TLabel")
        self.schedule_label.grid(row=0, column=0, sticky="w")
        self.recover_button = ttk.Button(footer, text="恢复旧版本", command=lambda: self._start("recover"), state="disabled")
        self.recover_button.grid(row=0, column=1, padx=(0, 6))
        ttk.Button(footer, text="日志目录", command=lambda: open_directory(self.state_dir / "logs")).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(footer, text="官方仓库", command=lambda: webbrowser.open("https://github.com/deepseek-ai/deepseek-harness")).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(footer, text="退出", command=self._exit).grid(row=0, column=4)
        self.controls = [self.path_entry, self.browse_button, self.check_button, self.watch_check, self.auto_check, self.interval_box, self.branch_entry, self.port_entry, self.save_button]
        self._log("欢迎使用。更新前请退出正在运行的 Harness；更新助手会保留配置与会话文件。")

    def _commit_panel(self, parent, column, title):
        ttk.Label(parent, text=title, style="Caption.TLabel").grid(row=0, column=column, sticky="w")
        sha = ttk.Label(parent, text="—", style="Commit.TLabel", font=("Consolas", 18, "bold"))
        sha.grid(row=1, column=column, sticky="w", pady=(4, 2))
        subject = ttk.Label(parent, text="等待检查", style="Surface.TLabel", wraplength=390, justify="left")
        subject.grid(row=2, column=column, sticky="nw")
        date = ttk.Label(parent, text="", style="Caption.TLabel")
        date.grid(row=3, column=column, sticky="w", pady=(5, 0))
        return sha, subject, date

    def _wrap_versions(self, width):
        length = max(230, (width - 84) // 2)
        self.local_subject.configure(wraplength=length)
        self.remote_subject.configure(wraplength=length)

    def _log(self, message: str):
        message = redact(message)
        logging.info(message)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        count = int(self.log_text.index("end-1c").split(".")[0])
        if count > 1600:
            self.log_text.delete("1.0", f"{count - 1200}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _browse(self):
        chosen = filedialog.askdirectory(title="选择 deepseek-harness 源码根目录", initialdir=self.directory.get() or str(Path.home()))
        if chosen:
            self.directory.set(chosen)
            self.snapshot = None
            self.local_sha.configure(text="—")
            self.remote_sha.configure(text="—")
            self._start("check")

    def _read_settings(self) -> Settings:
        try:
            settings = Settings(
                directory=self.directory.get().strip(), branch=self.branch.get().strip(),
                interval_minutes=int(self.interval.get()), watch=self.watch.get(),
                auto_update=self.auto.get(), service_port=int(self.port.get()),
            )
        except ValueError as exc:
            raise UpdaterError("检查间隔和服务端口需要填写整数。") from exc
        settings.validate()
        return settings

    def _persist(self) -> bool:
        try:
            self.settings = self._read_settings()
            self.settings.save(self.state_dir / "settings.json")
            self.next_check = time.monotonic() + self.settings.interval_minutes * 60
            return True
        except (UpdaterError, OSError) as exc:
            self._show_error(str(exc), dialog=True)
            return False

    def _startup_changed(self):
        try:
            set_startup(self.startup.get(), self.state_dir)
            self._log("已开启 Windows 登录启动。" if self.startup.get() else "已关闭 Windows 登录启动。")
        except OSError as exc:
            self.startup.set(startup_enabled())
            self._show_error(f"无法修改登录启动设置：{exc}", dialog=True)

    def _set_busy(self, value: bool):
        self.busy = value
        for control in self.controls:
            control.state(["disabled"] if value else ["!disabled"])
        self.update_button.state(["disabled"])
        self.recover_button.state(["disabled"])
        self.cancel_button.state(["!disabled"] if value else ["disabled"])
        self.interval_box.configure(state="disabled" if value else "readonly")
        if not value and self.snapshot:
            self._render(self.snapshot)

    def _start(self, action: str, *, automatic: bool = False):
        if self.busy or self.closing:
            return
        # Timed checks use the last saved settings. Half-entered form values
        # must not trigger a recurring error dialog or change the watched repo.
        if not automatic and not self._persist():
            return
        if not self.settings.directory:
            self._show_error("请先点击「选择目录」，选中 deepseek-harness 的源码根目录。", dialog=not automatic)
            return
        if action == "recover" and not automatic:
            if not messagebox.askokcancel("恢复未完成的更新", "将恢复上次保存的旧 Commit，并重新安装依赖、构建旧版本。\n\n请先退出 Harness。配置和会话保留在原处。", parent=self.root):
                return
        self.action = action
        self.cancel_event = threading.Event()
        self._set_busy(True)
        self.status_title.configure(text="正在检查…" if action == "check" else "正在恢复…" if action == "recover" else "正在更新…", foreground=BLUE)
        self.status_detail.configure(text="操作在后台进行，可以查看下方日志。")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        runner = Runner(lambda kind, value: self.events.put((kind, value)), self.cancel_event)
        settings = self.settings

        def work():
            try:
                engine = Engine(settings, self.state_dir, runner)
                if action == "check":
                    result = engine.check()
                elif action == "recover":
                    result = engine.recover()
                else:
                    result = engine.update(automatic=automatic)
                self.events.put(("done", (action, automatic, result)))
            except Exception as exc:
                current = None
                if action != "check":
                    try:
                        current = Engine(settings, self.state_dir, Runner()).inspect_local()
                    except Exception:
                        pass
                self.events.put(("error", (action, automatic, str(exc), current)))

        self.worker = threading.Thread(target=work, name="harness-updater-worker", daemon=False)
        self.worker.start()

    def _cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.state(["disabled"])
            self.progress_label.configure(text="正在取消；若已开始更新，会先尝试恢复旧版本。")
            self._log("收到取消请求。请等待安全恢复完成。")

    @staticmethod
    def _date(value: str) -> str:
        try:
            return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value

    def _render(self, snapshot: Snapshot, *, local_only: bool = False):
        self.snapshot = snapshot
        self.local_sha.configure(text=snapshot.local.short)
        self.local_subject.configure(text=snapshot.local.subject)
        self.local_date.configure(text=self._date(snapshot.local.date))
        built = snapshot.build_commit or "尚无构建记录"
        self.version_detail.configure(text=f"本地分支：{snapshot.branch or '游离 Commit'}   ·   构建记录：{built}" + ("（对应当前源码）" if snapshot.build_current else "（需要构建）"))
        self.recover_button.state(["!disabled"] if snapshot.recovery_needed and not self.busy else ["disabled"])
        if local_only:
            self.remote_sha.configure(text="—")
            self.remote_subject.configure(text="正在连接官方仓库…")
            self.remote_date.configure(text="")
            return
        if snapshot.remote:
            self.remote_sha.configure(text=snapshot.remote.short)
            self.remote_subject.configure(text=snapshot.remote.subject)
            self.remote_date.configure(text=f"{self._date(snapshot.remote.date)}  ·  {snapshot.target_branch}")
        else:
            self.remote_sha.configure(text="—")
            self.remote_subject.configure(text="需要重新检查远端版本")
            self.remote_date.configure(text="")
        title, detail, color = "需要检查远端版本", "点击「检查更新」读取最新提交。", MUTED
        if snapshot.reasons:
            title, detail, color = "更新前需要处理", "\n".join(snapshot.reasons[:3]), AMBER
        elif snapshot.relation == "behind":
            title = f"有新版本 · 落后 {snapshot.behind} 个 Commit"
            detail = "一键更新会保存旧版本，然后拉取源码、安装依赖并构建。"
            color = BLUE
            if snapshot.remote and snapshot.failed_target == snapshot.remote.sha:
                detail = "此版本上次更新失败，自动重试已暂停。处理日志中的问题后，可手动重试。"
        elif snapshot.relation == "up_to_date":
            if snapshot.build_current:
                title, detail, color = "已与官方最新版本同步", "源码与构建记录对应。更新后可重新启动 Harness。", GREEN
            else:
                title, detail, color = "源码已最新，需要完成构建", "点击右侧按钮安装依赖并构建，生成可运行的 Web 产物。", AMBER
        self.status_title.configure(text=title, foreground=color)
        self.status_detail.configure(text=detail)
        self.update_button.configure(text="安装依赖并构建" if snapshot.relation == "up_to_date" and not snapshot.build_current else "一键更新")
        self.update_button.state(["!disabled"] if snapshot.can_update and snapshot.needs_update and not self.busy else ["disabled"])
        self.commit_text.configure(state="normal")
        self.commit_text.delete("1.0", "end")
        if snapshot.commits:
            self.commit_text.insert("end", "最新提交（最多显示 12 条）\n\n")
            for commit in snapshot.commits:
                self.commit_text.insert("end", f"{commit.short}   {self._date(commit.date)}\n{commit.subject}\n\n")
        else:
            self.commit_text.insert("end", "没有新的官方提交。" if snapshot.relation == "up_to_date" else "完成检查后显示官方新增提交。")
        self.commit_text.configure(state="disabled")

    def _show_error(self, message: str, *, dialog: bool = False):
        message = redact(message)
        self._log(message)
        self.status_title.configure(text="操作未完成", foreground=AMBER)
        self.status_detail.configure(text=message[:220] + ("…\n完整原因请查看操作日志。" if len(message) > 220 else ""))
        if dialog and self.root.state() != "withdrawn":
            messagebox.showerror("Harness 更新助手", message, parent=self.root)

    def _notify(self, title: str, message: str, key: str):
        if self.tray_ready and self.tray and key != self.last_notification:
            self.last_notification = key
            try:
                self.tray.notify(message, title)
            except Exception:
                pass

    def _drain(self):
        if self.closing:
            return
        for _ in range(250):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(str(value))
            elif kind == "local":
                self._render(value, local_only=True)
            elif kind == "snapshot":
                self._render(value)
            elif kind == "stage":
                self.progress.stop()
                self.progress.configure(mode="determinate", value=value["index"])
                self.progress_label.configure(text=f"{value['index'] + 1} / 6   {value['text']}")
                if "恢复" in value["text"]:
                    self.cancel_button.state(["disabled"])
            elif kind == "done":
                action, automatic, result = value
                self.progress.stop()
                self.progress.configure(mode="determinate", value=6 if action != "check" else 0)
                self._set_busy(False)
                self._render(result)
                self.progress_label.configure(text="检查完成" if action == "check" else "旧版本已恢复" if action == "recover" else "更新完成")
                self.next_check = time.monotonic() + self.settings.interval_minutes * 60
                if action == "check" and result.remote:
                    if result.relation == "behind":
                        self._notify("Harness 有新版本", f"落后 {result.behind} 个 Commit，打开更新助手查看。", "available:" + result.remote.sha)
                    if self.settings.auto_update and result.can_auto_update:
                        self.root.after(200, lambda: self._start("update", automatic=True))
                elif action == "update":
                    self._notify("Harness 更新完成", "新版本已构建，可以重新启动 Harness。", "updated:" + result.local.sha)
            elif kind == "error":
                action, automatic, message, current = value
                self.progress.stop()
                self.progress.configure(mode="determinate", value=0)
                self._set_busy(False)
                if action != "check":
                    # Re-inspect the local recovery journal without relying on
                    # the network which may have caused the original failure.
                    self.snapshot = current
                    self.update_button.state(["disabled"])
                    if current:
                        self._render(current)
                    else:
                        self.recover_button.state(["disabled"])
                    self.auto.set(False)
                    self.settings = replace(self.settings, auto_update=False)
                    try:
                        self.settings.save(self.state_dir / "settings.json")
                    except OSError:
                        pass
                self._show_error(message, dialog=not automatic)
                self.progress_label.configure(text="操作失败，详情见日志")
                self.next_check = time.monotonic() + self.settings.interval_minutes * 60
                self._notify("Harness 更新助手需要处理", message[:180], f"error:{action}:{message[:80]}")
            elif kind == "tray_ready":
                self.tray_ready = True
                self._log("系统托盘已就绪，关闭窗口后可从托盘重新打开。")
            elif kind == "tray_failed":
                self.tray_ready = False
                self._log("系统托盘不可用。关闭窗口将最小化到任务栏，定时检查仍继续。")
                if self.root.state() == "withdrawn":
                    self.root.deiconify()
                    self.root.iconify()
            elif kind == "show":
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            elif kind == "check_request":
                self._start("check")
            elif kind == "exit_request":
                self._exit()
        self.root.after(100, self._drain)

    def _tick(self):
        if self.closing:
            return
        if self.busy:
            self.schedule_label.configure(text="正在执行操作 · 后台检查等待完成")
        elif self.settings.watch and self.settings.directory:
            remaining = max(0, int(self.next_check - time.monotonic()))
            self.schedule_label.configure(text=f"后台检查运行中 · 下次检查 {remaining // 60:02d}:{remaining % 60:02d}")
            if remaining == 0:
                self._start("check", automatic=True)
        else:
            self.schedule_label.configure(text="选择目录后开始后台检查" if self.settings.watch else "后台检查已暂停")
        self.root.after(1000, self._tick)

    def _start_tray(self):
        def work():
            try:
                import pystray
                from PIL import Image, ImageDraw

                icon = Image.new("RGB", (64, 64), BLUE)
                draw = ImageDraw.Draw(icon)
                draw.line([(18, 17), (18, 47)], fill=WHITE, width=6)
                draw.line([(42, 17), (42, 47)], fill=WHITE, width=6)
                draw.line([(18, 31), (42, 31)], fill=WHITE, width=6)
                draw.polygon([(40, 44), (55, 44), (47, 55)], fill=WHITE)
                menu = pystray.Menu(
                    pystray.MenuItem("打开更新助手", lambda *_: self.events.put(("show", None)), default=True),
                    pystray.MenuItem("检查更新", lambda *_: self.events.put(("check_request", None))),
                    pystray.MenuItem("退出", lambda *_: self.events.put(("exit_request", None))),
                )
                self.tray = pystray.Icon("HarnessUpdater", icon, "Harness 更新助手", menu)

                def ready(tray):
                    tray.visible = True
                    self.events.put(("tray_ready", None))

                self.tray.run(setup=ready)
            except Exception:
                self.events.put(("tray_failed", None))

        threading.Thread(target=work, name="harness-updater-tray", daemon=True).start()

    def _hide(self):
        if self.tray_ready:
            self.root.withdraw()
        else:
            self.root.iconify()

    def _exit(self):
        if self.busy:
            messagebox.showinfo("操作仍在进行", "请先点击「取消操作」，等待更新或旧版本恢复结束后，再退出助手。", parent=self.root)
            return
        self.closing = True
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()


def launch(settings: Settings, state_dir: Path, *, minimized: bool = False):
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    Application(root, settings, state_dir, minimized=minimized)
    root.mainloop()
