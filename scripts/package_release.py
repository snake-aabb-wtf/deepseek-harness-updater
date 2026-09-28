"""Build and verify the distributable Windows archive and checksum manifest."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness_updater import __version__  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_release(root: Path, tag: str, version: str = __version__) -> tuple[Path, Path, Path]:
    if tag != f"v{version}":
        raise ValueError(f"标签 {tag!r} 与程序版本 v{version} 不一致")

    output = root / "dist"
    executable = output / "HarnessUpdater.exe"
    guide = root / "docs" / "使用说明.txt"
    if not executable.is_file() or executable.stat().st_size == 0:
        raise FileNotFoundError(f"找不到非空的打包程序：{executable}")
    if not guide.is_file():
        raise FileNotFoundError(f"找不到使用说明：{guide}")

    archive = output / f"HarnessUpdater-{version}-Windows-x64.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
        bundle.write(executable, executable.name)
        bundle.write(guide, guide.name)
    with ZipFile(archive) as bundle:
        if bundle.namelist() != [executable.name, guide.name] or bundle.testzip() is not None:
            raise RuntimeError("发行压缩包校验失败")

    checksums = output / "SHA256SUMS.txt"
    checksums.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in (executable, archive)),
        encoding="ascii",
    )
    return executable, archive, checksums


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=f"v{__version__}", help="Git 发行标签，必须等于程序版本")
    arguments = parser.parse_args()
    for file in package_release(ROOT, arguments.tag):
        print(f"生成：{file}")


if __name__ == "__main__":
    main()
