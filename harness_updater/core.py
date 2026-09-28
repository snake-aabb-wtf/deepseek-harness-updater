from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .process import Cancelled, Runner, runtime_blockers
from .storage import FileLock, Settings, UpdaterError, atomic_json, read_json, repository_key

OFFICIAL_URL = "https://github.com/deepseek-ai/deepseek-harness.git"
BUILD_RECORD = ".dsh-build/client-build-environment.json"
PENDING_STATES = {"prepared", "updating", "installing", "building", "verifying", "recovering", "recovery_required"}


def official_remote(url: str) -> bool:
    value = url.removeprefix("git+").strip()
    if value.startswith("git@github.com:"):
        value = "ssh://git@github.com/" + value[len("git@github.com:"):]
    parsed = urlparse(value)
    return (
        parsed.scheme in {"https", "ssh"}
        and parsed.hostname == "github.com"
        and parsed.path.rstrip("/").removesuffix(".git").lower() == "/deepseek-ai/deepseek-harness"
        and not parsed.query and not parsed.fragment
    )


def _version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", value.strip())
    if not match:
        raise UpdaterError(f"无法识别版本号：{value}")
    return tuple(int(part or 0) for part in match.groups())


def node_satisfies(version: str, requirement: str) -> bool:
    """Support upstream's comparator, caret, tilde, wildcard and OR ranges.

    Unknown syntax is rejected rather than accidentally accepting an unsupported
    runtime. No third-party semver module is necessary to launch the updater.
    """
    actual = _version(version)
    alternatives = requirement.strip().split("||")
    matched = False
    for alternative in alternatives:
        expression = alternative.strip()
        if expression in {"", "*"}:
            matched = True
            continue
        expression = re.sub(r"(>=|<=|>|<|=|\^|~)\s+", r"\1", expression)
        terms = expression.split()
        accepted = True
        for term in terms:
            token = re.fullmatch(r"(>=|<=|>|<|=|\^|~)?(\d+)(?:\.(\d+|x|X|\*))?(?:\.(\d+|x|X|\*))?", term)
            if not token:
                raise UpdaterError(f"暂不支持此 Node.js 版本范围：{requirement}。请核对官方安装要求。")
            operator, major, minor, patch = token.groups()
            floor = (int(major), int(minor) if minor and minor.isdigit() else 0, int(patch) if patch and patch.isdigit() else 0)
            if operator == ">=":
                ok = actual >= floor
            elif operator == "<=":
                ok = actual <= floor
            elif operator == ">":
                ok = actual > floor
            elif operator == "<":
                ok = actual < floor
            elif operator == "^":
                ceiling = (floor[0] + 1, 0, 0) if floor[0] else ((0, floor[1] + 1, 0) if floor[1] else (0, 0, floor[2] + 1))
                ok = floor <= actual < ceiling
            elif operator == "~":
                ceiling = (floor[0], floor[1] + 1, 0) if minor else (floor[0] + 1, 0, 0)
                ok = floor <= actual < ceiling
            elif minor is None or minor in {"x", "X", "*"}:
                ok = actual[0] == floor[0]
            elif patch is None or patch in {"x", "X", "*"}:
                ok = actual[:2] == floor[:2]
            else:
                ok = actual == floor
            accepted = accepted and ok
        matched = matched or accepted
    return matched


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    date: str

    @property
    def short(self) -> str:
        return self.sha[:12]


@dataclass
class Snapshot:
    directory: str
    local: Commit
    remote: Commit | None = None
    branch: str = ""
    target_branch: str = ""
    relation: str = "unknown"
    behind: int = 0
    ahead: int = 0
    dirty: bool = False
    build_commit: str = ""
    build_current: bool = False
    reasons: list[str] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    recovery_needed: bool = False
    failed_target: str = ""
    checked_at: str = ""

    @property
    def can_update(self) -> bool:
        return self.relation in {"behind", "up_to_date"} and not self.reasons

    @property
    def needs_update(self) -> bool:
        return self.relation == "behind" or (self.relation == "up_to_date" and not self.build_current)

    @property
    def can_auto_update(self) -> bool:
        return self.can_update and self.needs_update and bool(self.remote) and self.failed_target != self.remote.sha


class Toolchain:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir.resolve()

    def _node_npm(self) -> tuple[str, list[str]]:
        node = shutil.which("node")
        if not node:
            raise UpdaterError("未安装 Node.js。请从 nodejs.org 安装官方 LTS 版本，然后重新打开更新助手。")
        npm = shutil.which("npm")
        base = Path(node).resolve().parent
        candidates = [base / "node_modules/npm/bin/npm-cli.js", base.parent / "lib/node_modules/npm/bin/npm-cli.js"]
        if npm:
            npm_path = Path(npm).resolve()
            candidates.extend([npm_path.parent / "node_modules/npm/bin/npm-cli.js", npm_path])
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix in {".js", ".cjs", ".mjs"}:
                return node, [node, str(candidate)]
        if npm and os.name != "nt":
            return node, [npm]
        raise UpdaterError("未找到随 Node.js 安装的 npm。请重新安装完整的官方 Node.js，再打开更新助手。")

    def prepare(self, root: Path, manifest: dict, runner: Runner) -> list[str]:
        node, npm = self._node_npm()
        version = runner.run([node, "--version"]).stdout.strip()
        required = manifest.get("engines", {}).get("node", "")
        if not isinstance(required, str) or not required:
            raise UpdaterError("目标版本没有声明 Node.js 要求，请核对官方 package.json。")
        if not node_satisfies(version, required):
            raise UpdaterError(f"Node.js {version} 不满足目标版本要求 {required}。请先更新 Node.js，再重试。")
        manager = manifest.get("packageManager", "")
        match = re.fullmatch(r"pnpm@(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?:\+sha(?:224|256|512)\.[0-9a-fA-F]+)?", manager)
        if not match:
            raise UpdaterError("目标版本没有固定可识别的 pnpm 版本。为避免装错依赖，更新已暂停。")
        pnpm_version = match.group(1)
        cache = self.state_dir / "tools" / ("pnpm-" + pnpm_version)
        entry = cache / "node_modules/pnpm/bin/pnpm.cjs"
        with FileLock(self.state_dir / "tools.lock"):
            if not entry.exists():
                runner.log(f"准备 pnpm {pnpm_version}（更新助手独立缓存，无需全局安装）")
                cache.mkdir(parents=True, exist_ok=True)
                runner.run(npm + ["install", "--prefix", str(cache), "--no-audit", "--no-fund", "--ignore-scripts", "--save-exact", f"pnpm@{pnpm_version}"], cwd=self.state_dir, timeout=600, stream=True)
            command = [node, str(entry)]
            actual = runner.run(command + ["--version"], cwd=root, timeout=90).stdout.strip()
            if actual != pnpm_version:
                raise UpdaterError(f"pnpm 缓存版本不正确：需要 {pnpm_version}，实际为 {actual}。请保留日志后检查缓存 {cache}。")
        runner.log(f"环境通过：Node.js {version} / pnpm {pnpm_version}")
        return command

    def install(self, root: Path, command: list[str], runner: Runner) -> None:
        runner.run(command + ["install", "--frozen-lockfile"], cwd=root, timeout=1800, stream=True, env={"CI": "true"})

    def build(self, root: Path, command: list[str], runner: Runner) -> None:
        runner.run(command + ["run", "build"], cwd=root, timeout=3600, stream=True, env={"CI": "true"})

    def verify(self, root: Path, command: list[str], expected: str, runner: Runner) -> None:
        # Let upstream validate its own artifact schema and hash, so future
        # changes to the digest algorithm don't require a Python port.
        record_owner = root / "scripts/client-build-environment.ts"
        if record_owner.exists():
            script = (
                "import { readClientBuildRecord } from './scripts/client-build-environment.ts';"
                "const r = readClientBuildRecord(process.cwd());"
                "if (r.environment.DSH_CLIENT_COMMIT_HASH !== process.argv[1].slice(0,7)"
                " || r.environment.DSH_CLIENT_GIT_DIRTY === 'true')"
                " throw new Error('Build commit does not match the clean source commit');"
                "console.log('构建 Commit 与产物摘要校验通过');"
            )
            runner.run([command[0], "--import", "tsx/esm", "--input-type=module", "-e", script, expected], cwd=root, timeout=300, stream=True)
        runner.run(command + ["dsh", "--version"], cwd=root, timeout=90, stream=True, env={"CI": "true"})


class Engine:
    def __init__(
        self, settings: Settings, state_dir: Path, runner: Runner | None = None,
        *, source_url: str = OFFICIAL_URL, toolchain: Toolchain | None = None,
        runtime_probe: Callable[[Path, int], list[str]] = runtime_blockers,
    ):
        settings.validate()
        if not settings.directory.strip():
            raise UpdaterError("请先选择 deepseek-harness 安装目录。")
        self.settings = settings
        self.root = Path(settings.directory).expanduser().resolve()
        self.state_dir = state_dir
        self.runner = runner or Runner()
        self.source_url = source_url
        self.toolchain = toolchain or Toolchain(state_dir)
        self.runtime_probe = runtime_probe
        self.git = shutil.which("git") or "git"
        self.ssl_backend: str | None = None
        self.journal_path = state_dir / "repositories" / (repository_key(self.root) + ".json")

    def _git(self, *args: str, **kwargs):
        return self.runner.run([self.git, *args], cwd=self.root, **kwargs)

    def _network_git(self, *args: str, **kwargs):
        prefix = ("-c", f"http.sslBackend={self.ssl_backend}") if self.ssl_backend else ()
        try:
            return self._git(*prefix, *args, **kwargs)
        except UpdaterError as exc:
            if os.name != "nt" or self.ssl_backend or "SEC_E_NO_CREDENTIALS" not in str(exc):
                raise
            # Some Windows sessions cannot acquire Schannel credentials. The
            # Git-for-Windows OpenSSL backend still validates TLS certificates.
            self.runner.log("Windows TLS 凭据不可用，改用 Git OpenSSL 后端重试（保留证书校验）。")
            self.ssl_backend = "openssl"
            return self._git("-c", "http.sslBackend=openssl", *args, **kwargs)

    def _root_and_lock(self) -> Path:
        if not self.root.is_dir():
            raise UpdaterError(f"安装目录不存在：{self.root}")
        # Requiring a .git entry also avoids mistakenly identifying a parent repo.
        if not (self.root / ".git").exists():
            raise UpdaterError("此目录没有 .git 历史，无法准确识别原 Commit。请选择 git clone 得到的源码根目录；ZIP、npm 缓存和桌面安装包不适用。")
        top = Path(self._git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if top != self.root:
            raise UpdaterError(f"请选择仓库根目录：{top}")
        raw = self._git("rev-parse", "--git-common-dir").stdout.strip()
        common = Path(raw)
        if not common.is_absolute():
            common = self.root / common
        return common.resolve() / "dsh-updater.lock"

    def _manifest(self, ref: str | None = None) -> dict:
        try:
            text = self._git("show", f"{ref}:package.json").stdout if ref else (self.root / "package.json").read_text(encoding="utf-8")
            manifest = json.loads(text)
        except (OSError, ValueError) as exc:
            raise UpdaterError("无法读取 Harness 的 package.json，请选择完整的源码仓库。") from exc
        if not isinstance(manifest, dict) or manifest.get("name") not in {"@deepseek-ai/dsh-root", "deepseek-harness"}:
            raise UpdaterError("此目录不是受支持的 deepseek-harness 源码安装目录。")
        scripts = manifest.get("scripts", {})
        if not isinstance(scripts, dict) or not all(isinstance(scripts.get(key), str) for key in ("build", "dsh")):
            raise UpdaterError("目标版本缺少 build / dsh 脚本，无法按官方源码流程更新。")
        return manifest

    def _commit(self, ref: str) -> Commit:
        fields = self._git("show", "-s", "--format=%H%x00%s%x00%cI", ref).stdout.strip().split("\0")
        if len(fields) != 3 or not re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
            raise UpdaterError("Git 返回的 Commit 信息不完整。")
        return Commit(*fields)

    def _branch(self) -> str:
        result = self._git("symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1))
        return result.stdout.strip() if result.returncode == 0 else ""

    def _dirty(self) -> bool:
        return bool(self._git("status", "--porcelain=v1", "-z", "--untracked-files=normal").stdout)

    def _local(self) -> Snapshot:
        self._manifest()
        remotes = self._git("remote").stdout.splitlines()
        urls = [self._git("remote", "get-url", name).stdout.strip() for name in remotes]
        if not any(official_remote(url) for url in urls):
            raise UpdaterError("该仓库没有指向官方 deepseek-ai/deepseek-harness 的远端。请确认选对目录；fork 用户需添加官方 upstream。")
        snapshot = Snapshot(str(self.root), self._commit("HEAD"), branch=self._branch(), dirty=self._dirty())
        if not snapshot.branch:
            snapshot.reasons.append("当前是游离 Commit（detached HEAD）。请先切回要更新的官方分支。")
        if snapshot.dirty:
            snapshot.reasons.append("目录里有本地修改或未跟踪文件。请先备份并处理这些文件，再更新。")
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer", "index.lock"):
            raw = self._git("rev-parse", "--git-path", marker).stdout.strip()
            path = Path(raw)
            if not path.is_absolute():
                path = self.root / path
            if path.exists():
                snapshot.reasons.append(f"Git 操作尚未结束（{marker}）。请先完成或取消该操作。")
        sparse = self._git("config", "--bool", "core.sparseCheckout", allowed=(0, 1)).stdout.strip()
        if sparse == "true":
            snapshot.reasons.append("当前是稀疏检出，请先恢复完整源码目录。")
        if (self.root / ".gitmodules").exists():
            snapshot.reasons.append("此版本包含 Git 子模块，当前更新流程不支持自动处理子模块。")
        journal = read_json(self.journal_path)
        snapshot.recovery_needed = journal.get("status") in PENDING_STATES
        if snapshot.recovery_needed:
            snapshot.reasons.append("上次更新未完成。请先点击「恢复旧版本」，再尝试更新。")
        if journal.get("status") in {"rolled_back", "failed", "recovery_required"}:
            snapshot.failed_target = journal.get("target", "")
        record = self.root / BUILD_RECORD
        try:
            build = read_json(record)
            environment = build.get("environment", {})
            value = environment.get("DSH_CLIENT_COMMIT_HASH", "") if isinstance(environment, dict) else ""
            snapshot.build_commit = value if isinstance(value, str) else ""
            snapshot.build_current = (
                bool(re.fullmatch(r"[0-9a-f]{7,40}", snapshot.build_commit))
                and snapshot.local.sha.startswith(snapshot.build_commit)
                and environment.get("DSH_CLIENT_GIT_DIRTY") != "true"
                and (self.root / "apps/web/dist").is_dir()
            )
        except UpdaterError:
            # Invalid build record is repairable; it must not hide the Git HEAD.
            snapshot.build_commit = "记录无效"
        if not record.exists() and not (self.root / "scripts/client-build-environment.ts").exists():
            snapshot.build_current = journal.get("verified_commit") == snapshot.local.sha
            snapshot.build_commit = snapshot.local.sha[:7] if snapshot.build_current else ""
        snapshot.reasons.extend(self.runtime_probe(self.root, self.settings.service_port))
        self.runner.emit("local", snapshot)
        return snapshot

    def _fetch(self) -> tuple[str, str]:
        requested = self.settings.branch.strip()
        if requested:
            result = self._git("check-ref-format", "--branch", requested, allowed=(0, 1, 128))
            if result.returncode != 0 or requested.startswith("-") or requested == "HEAD":
                raise UpdaterError("分支名称无效。留空可跟随官方默认分支。")
        advertisement = self._network_git("ls-remote", "--symref", self.source_url, "HEAD", timeout=90).stdout
        match = re.search(r"^ref: refs/heads/(.+)\s+HEAD$", advertisement, re.MULTILINE)
        if not match and not requested:
            raise UpdaterError("无法识别官方默认分支。请检查网络，或在设置里填写分支名称。")
        branch = requested or match.group(1)
        ref = "refs/dsh-updater/remotes/" + branch
        self.runner.log(f"读取官方 {branch} 分支最新 Commit…")
        self._network_git("fetch", "--no-tags", self.source_url, f"+refs/heads/{branch}:{ref}", timeout=300, stream=True)
        return branch, self._git("rev-parse", ref).stdout.strip()

    def _check(self) -> Snapshot:
        snapshot = self._local()
        branch, sha = self._fetch()
        snapshot.target_branch = branch
        snapshot.remote = self._commit(sha)
        try:
            self._manifest(sha)
        except UpdaterError as exc:
            snapshot.reasons.append(f"目标版本的安装流程不受支持：{exc}")
        counts = self._git("rev-list", "--left-right", "--count", f"{snapshot.local.sha}...{sha}").stdout.split()
        snapshot.ahead, snapshot.behind = map(int, counts)
        ancestor = self._git("merge-base", "--is-ancestor", snapshot.local.sha, sha, allowed=(0, 1))
        if snapshot.local.sha == sha:
            snapshot.relation = "up_to_date"
        elif ancestor.returncode == 0:
            snapshot.relation = "behind"
        elif snapshot.behind == 0:
            snapshot.relation = "ahead"
            snapshot.reasons.append("本地包含官方远端没有的 Commit，请自行检查；更新助手不会覆盖本地提交。")
        else:
            snapshot.relation = "diverged"
            snapshot.reasons.append("本地与官方历史已分叉（或浅克隆缺少共同历史）。请先处理分支；更新助手只执行快进更新。")
        if snapshot.branch and snapshot.branch != branch:
            snapshot.reasons.append(f"本地分支为 {snapshot.branch}，目标分支为 {branch}。请先切换到同名分支。")
        log = self._git("log", "--max-count=12", "--format=%H%x00%s%x00%cI", f"{snapshot.local.sha}..{sha}").stdout
        for line in log.splitlines():
            fields = line.split("\0")
            if len(fields) == 3:
                snapshot.commits.append(Commit(*fields))
        snapshot.checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.runner.emit("snapshot", snapshot)
        return snapshot

    def check(self) -> Snapshot:
        with FileLock(self._root_and_lock()):
            return self._check()

    def inspect_local(self) -> Snapshot:
        """Read the actual source/recovery state even when GitHub is offline."""
        with FileLock(self._root_and_lock()):
            return self._local()

    def _stage(self, stage: str, index: int) -> None:
        self.runner.log(stage)
        self.runner.emit("stage", {"text": stage, "index": index})

    def _save_journal(self, record: dict, status: str, **values) -> None:
        record.update(values)
        record["status"] = status
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(self.journal_path, record)

    def _assert_unchanged(self, sha: str, branch: str) -> None:
        if self._commit("HEAD").sha != sha or self._branch() != branch or self._dirty():
            raise UpdaterError("目录在检查后发生了变化。请关闭其他 Git / 编辑工具后重试，当前操作不会覆盖这些变化。")
        problems = self.runtime_probe(self.root, self.settings.service_port)
        if problems:
            raise UpdaterError("\n".join(problems))

    def update(self, *, automatic: bool = False) -> Snapshot:
        with FileLock(self._root_and_lock()):
            self._stage("检查版本与运行环境", 0)
            snapshot = self._check()
            if not snapshot.can_update:
                raise UpdaterError("暂时不能更新：\n" + "\n".join(snapshot.reasons))
            if automatic and not snapshot.can_auto_update:
                raise UpdaterError("此次自动更新已跳过。上次失败的目标版本需要手动重试。")
            if not snapshot.needs_update:
                self.runner.log("源码已是最新，并有对应构建记录。")
                return snapshot
            assert snapshot.remote is not None
            old, target = snapshot.local.sha, snapshot.remote.sha
            previous_manifest = self._manifest(old)
            target_manifest = self._manifest(target)
            if self._git("show", f"{target}:.gitmodules", allowed=(0, 128)).returncode == 0:
                raise UpdaterError("目标版本包含子模块，更新已暂停。")
            command = self.toolchain.prepare(self.root, target_manifest, self.runner)
            self._assert_unchanged(old, snapshot.branch)
            self._stage("保存旧版本 Commit", 1)
            backup = f"refs/dsh-updater/backups/{time.time_ns()}-{old[:12]}"
            self._git("update-ref", backup, old)
            record = {"directory": str(self.root), "previous": old, "target": target, "branch": snapshot.branch, "backup_ref": backup}
            self._save_journal(record, "prepared")
            try:
                self._stage("更新源码", 2)
                self._save_journal(record, "updating")
                self._assert_unchanged(old, snapshot.branch)
                # Disable local Git hooks for this fast-forward only. Hooks are
                # developer tooling; no user merge/checkout scripts should run.
                hooks = self.state_dir / "empty-hooks"
                hooks.mkdir(parents=True, exist_ok=True)
                self._git("-c", f"core.hooksPath={hooks}", "merge", "--ff-only", "--no-edit", target, timeout=120, stream=True)
                self._stage("安装目标版本依赖", 3)
                self._save_journal(record, "installing")
                self.toolchain.install(self.root, command, self.runner)
                self._stage("构建 Harness（可能需要几分钟）", 4)
                self._save_journal(record, "building")
                self.toolchain.build(self.root, command, self.runner)
                self._stage("校验 Commit 与构建产物", 5)
                self._save_journal(record, "verifying")
                self._assert_unchanged(target, snapshot.branch)
                self.toolchain.verify(self.root, command, target, self.runner)
                self._assert_unchanged(target, snapshot.branch)
                self._save_journal(record, "complete", verified_commit=target)
                self.runner.log(f"更新完成：{old[:12]} → {target[:12]}。可以重新启动 Harness。")
            except Exception as exc:
                self.runner.log(f"更新未完成：{exc}")
                # Cancellation stops the foreground command. Recovery has its
                # own token so it can finish restoring a usable old version.
                original_runner = self.runner
                self.runner = Runner(original_runner.sink)
                try:
                    self._restore(record, previous_manifest)
                    message = "旧版本源码、依赖和构建已恢复。"
                except Exception as recovery_error:
                    self._save_journal(record, "recovery_required", error=str(exc), recovery_error=str(recovery_error))
                    message = f"自动恢复未完成：{recovery_error}\n请点击「恢复旧版本」。恢复记录：{self.journal_path}"
                finally:
                    self.runner = original_runner
                raise UpdaterError(f"更新失败：{exc}\n{message}") from exc
            result = self._local()
            result.remote = snapshot.remote
            result.target_branch = snapshot.target_branch
            result.relation = "up_to_date"
            result.checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.runner.emit("snapshot", result)
            return result

    def _restore(self, record: dict, manifest: dict | None = None) -> None:
        self._stage("正在恢复旧版本，请保持更新助手运行", 1)
        previous, target = record.get("previous", ""), record.get("target", "")
        if not all(re.fullmatch(r"[0-9a-f]{40,64}", sha) for sha in (previous, target)):
            raise UpdaterError("恢复记录中的 Commit 无效。")
        if record.get("directory") != str(self.root):
            raise UpdaterError("恢复记录与安装目录不一致。")
        current = self._commit("HEAD").sha
        if current not in {previous, target}:
            raise UpdaterError("当前 Commit 已被其他程序修改。请自行处理，更新助手不会重置它。")
        self._assert_unchanged(current, record.get("branch", ""))
        if self._git("rev-parse", record["backup_ref"]).stdout.strip() != previous:
            raise UpdaterError("备份引用与旧版本 Commit 不一致。")
        command = self.toolchain.prepare(self.root, manifest or self._manifest(previous), self.runner)
        self._assert_unchanged(current, record.get("branch", ""))
        self._save_journal(record, "recovering")
        if current != previous:
            self._git("reset", "--keep", previous, timeout=120, stream=True)
        self.toolchain.install(self.root, command, self.runner)
        self.toolchain.build(self.root, command, self.runner)
        self._assert_unchanged(previous, record["branch"])
        self.toolchain.verify(self.root, command, previous, self.runner)
        self._assert_unchanged(previous, record["branch"])
        self._save_journal(record, "rolled_back", verified_commit=previous)
        self.runner.log(f"旧版本 {previous[:12]} 已恢复；配置和会话目录保留在原处。")

    def recover(self) -> Snapshot:
        with FileLock(self._root_and_lock()):
            self._manifest()
            record = read_json(self.journal_path)
            if record.get("status") not in PENDING_STATES:
                raise UpdaterError("没有需要恢复的未完成更新。")
            try:
                self._restore(record)
            except Exception as exc:
                self._save_journal(record, "recovery_required", recovery_error=str(exc))
                raise
            return self._local()


def snapshot_dict(snapshot: Snapshot) -> dict:
    return {**asdict(snapshot), "can_update": snapshot.can_update, "needs_update": snapshot.needs_update, "can_auto_update": snapshot.can_auto_update}
