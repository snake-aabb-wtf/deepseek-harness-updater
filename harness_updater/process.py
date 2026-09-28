from __future__ import annotations

import locale
import os
import queue
import re
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Sequence

from .storage import UpdaterError

EventSink = Callable[[str, object], None]


class Cancelled(UpdaterError):
    pass


def redact(text: str) -> str:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[凭证已隐藏]@", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,})\b", "[密钥已隐藏]", text)
    return re.sub(r"(?i)((?:token|api[_-]?key|password|authorization)\s*[=:]\s*)\S+", r"\1[已隐藏]", text)


def terminate_tree(process: subprocess.Popen) -> None:
    try:
        import psutil

        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.Error:
                pass
        try:
            parent.terminate()
        except psutil.Error:
            pass
        _, alive = psutil.wait_procs(children + [parent], timeout=3)
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                pass
    except ImportError:
        process.kill()
    except Exception:
        if process.poll() is None:
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class Runner:
    def __init__(self, sink: EventSink | None = None, cancel: threading.Event | None = None):
        self.sink = sink or (lambda *_: None)
        self.cancel = cancel or threading.Event()

    def emit(self, kind: str, value: object) -> None:
        self.sink(kind, value)

    def log(self, text: str) -> None:
        self.emit("log", redact(text))

    def run(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
        *,
        timeout: float = 120,
        stream: bool = False,
        allowed: tuple[int, ...] = (0,),
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        if self.cancel.is_set():
            raise Cancelled("操作已取消。")
        command = [str(arg) for arg in args]
        process_env = os.environ.copy()
        # Ambient Git variables must never redirect commands to a different repo.
        for key in list(process_env):
            if key.startswith("GIT_") and key not in {"GIT_SSL_CAINFO", "GIT_SSL_CAPATH"}:
                process_env.pop(key)
        process_env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "NO_COLOR": "1"})
        if env:
            process_env.update(env)
        if stream:
            self.log("执行：" + " ".join(command))
        try:
            process = subprocess.Popen(
                command, cwd=cwd, env=process_env, shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except FileNotFoundError as exc:
            raise UpdaterError(f"未找到 {Path(command[0]).name}。请安装后重新打开更新助手。") from exc
        except OSError as exc:
            raise UpdaterError(f"无法启动 {Path(command[0]).name}：{exc}") from exc
        output: queue.Queue[bytes | None] = queue.Queue()

        def read_output():
            try:
                assert process.stdout is not None
                for line in iter(process.stdout.readline, b""):
                    output.put(line)
            finally:
                output.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        captured: list[str] = []
        tail: deque[str] = deque(maxlen=20)
        started = time.monotonic()
        try:
            while True:
                if self.cancel.is_set():
                    terminate_tree(process)
                    raise Cancelled("操作已取消；正在检查是否需要恢复旧版本。")
                if time.monotonic() - started > timeout:
                    terminate_tree(process)
                    raise UpdaterError(f"{Path(command[0]).name} 执行超时。请检查网络、代理或构建环境，再重试。")
                try:
                    line = output.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                try:
                    text = line.decode("utf-8")
                except UnicodeDecodeError:
                    text = line.decode(locale.getpreferredencoding(False), errors="replace")
                if stream:
                    # Build output can be large. Keep only an error tail in memory.
                    tail.append(text)
                    self.log(text.rstrip())
                else:
                    captured.append(text)
            code = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                terminate_tree(process)
            if process.stdout is not None:
                process.stdout.close()
            reader.join(timeout=1)
        stdout = "".join(tail if stream else captured)
        if code not in allowed:
            detail = redact(stdout.strip()[-3000:])
            raise UpdaterError(f"{Path(command[0]).name} 执行失败（退出码 {code}）。\n{detail}")
        return subprocess.CompletedProcess(command, code, stdout, "")


def runtime_blockers(root: Path, port: int) -> list[str]:
    """Conservatively refuse to overwrite a checkout used by a running process."""
    reasons: list[str] = []
    with socket.socket() as probe:
        probe.settimeout(0.25)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            reasons.append(f"本机 {port} 端口正在使用。请先退出 Harness；若用了其他端口，请修改服务端口设置。")
    try:
        import psutil
    except ImportError:
        return reasons + ["缺少进程检查组件 psutil。请运行 pip install -r requirements.txt，或使用打包版。"]
    runtime_names = {"node", "node.exe", "electron", "electron.exe", "dsh", "dsh.exe", "deepseek-harness", "deepseek-harness.exe"}
    canonical = os.path.normcase(str(root.resolve()))
    for process in psutil.process_iter(["pid", "name"]):
        if process.pid == os.getpid():
            continue
        name = (process.info["name"] or "").lower()
        if name not in runtime_names:
            continue
        try:
            working = os.path.normcase(str(Path(process.cwd()).resolve()))
            arguments = process.cmdline()
            in_directory = working == canonical or working.startswith(canonical + os.sep)
            explicit_path = any(canonical in os.path.normcase(arg.replace("/", os.sep)) for arg in arguments)
            if in_directory or explicit_path:
                reasons.append(f"此安装目录仍被 {name} 使用（PID {process.pid}）。请先退出 Harness 或开发构建进程。")
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError):
            # A relevant process which cannot be inspected is not proven idle.
            reasons.append(f"无法检查 {name}（PID {process.pid}）是否使用此目录。请关闭它，或用相同权限运行更新助手。")
    return list(dict.fromkeys(reasons))
