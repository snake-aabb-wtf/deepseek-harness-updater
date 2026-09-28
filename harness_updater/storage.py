from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class UpdaterError(Exception):
    """An actionable error which is safe to display to the user."""


def default_state_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "DeepSeekHarnessUpdater"


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise UpdaterError(f"无法读取记录文件 {path}。请先保留该文件，再检查权限或文件内容。") from exc
    if not isinstance(value, dict):
        raise UpdaterError(f"记录文件格式不正确：{path}")
    return value


@dataclass(frozen=True)
class Settings:
    directory: str = ""
    branch: str = ""
    interval_minutes: int = 15
    watch: bool = True
    auto_update: bool = False
    service_port: int = 3080

    @classmethod
    def load(cls, path: Path) -> "Settings":
        values = read_json(path)
        known = {key: val for key, val in values.items() if key in cls.__dataclass_fields__}
        result = cls(**known)
        result.validate()
        return result

    def validate(self) -> None:
        if not isinstance(self.directory, str) or not isinstance(self.branch, str):
            raise UpdaterError("安装目录和分支必须是文本。")
        if type(self.interval_minutes) is not int or not 1 <= self.interval_minutes <= 1440:
            raise UpdaterError("检查间隔应为 1～1440 分钟。")
        if type(self.service_port) is not int or not 1 <= self.service_port <= 65535:
            raise UpdaterError("Harness 服务端口应为 1～65535。")
        if type(self.watch) is not bool or type(self.auto_update) is not bool:
            raise UpdaterError("自动检查和自动更新设置格式不正确。")

    def save(self, path: Path) -> None:
        self.validate()
        atomic_json(path, asdict(self))


class FileLock:
    """OS-owned lock: a crashed process releases it without stale-PID guessing."""

    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise UpdaterError("另一个更新助手正在操作此目录，请等它完成后再试。") from exc
        self.stream = stream
        return self

    def __exit__(self, *_):
        if self.stream is not None:
            # Closing the descriptor releases the lock on all supported OSes.
            self.stream.close()
            self.stream = None


def repository_key(root: Path) -> str:
    canonical = os.path.normcase(str(root.resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
