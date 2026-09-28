from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from harness_updater.core import BUILD_RECORD, Engine, Toolchain, node_satisfies, official_remote
from harness_updater.process import Cancelled, Runner, redact, runtime_blockers
from harness_updater.storage import FileLock, Settings, UpdaterError, atomic_json, read_json


def git(path: Path, *args: str) -> str:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
    result = subprocess.run(["git", *args], cwd=path, env=env, text=True, encoding="utf-8", capture_output=True, check=True)
    return result.stdout.strip()


class FakeToolchain:
    def __init__(self, *, fail_target: str = "", modify_target: bool = False, cancel_target: bool = False):
        self.fail_target = fail_target
        self.modify_target = modify_target
        self.cancel_target = cancel_target
        self.installed: list[str] = []
        self.built: list[str] = []

    def prepare(self, root, manifest, runner):
        return ["fake-pnpm"]

    def install(self, root, command, runner):
        self.installed.append(git(root, "rev-parse", "HEAD"))

    def build(self, root, command, runner):
        sha = git(root, "rev-parse", "HEAD")
        self.built.append(sha)
        if sha == self.fail_target:
            if self.modify_target:
                (root / "source.txt").write_text("user edit made during build", encoding="utf-8")
            if self.cancel_target:
                runner.cancel.set()
                raise Cancelled("fixture cancellation")
            raise UpdaterError("fixture build failure")
        output = root / "apps/web/dist"
        output.mkdir(parents=True, exist_ok=True)
        (output / "index.html").write_text(sha, encoding="utf-8")
        atomic_json(root / BUILD_RECORD, {"environment": {"DSH_CLIENT_COMMIT_HASH": sha[:7]}})

    def verify(self, root, command, expected, runner):
        if (root / "apps/web/dist/index.html").read_text() != expected:
            raise UpdaterError("fixture artifact mismatch")


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-updater-test-")
        self.base = Path(self.temporary.name)
        self.origin = self.base / "origin.git"
        self.author = self.base / "author"
        # Non-ASCII, spaces and cmd metacharacters must work without a shell.
        self.local = self.base / "我的 Harness & 安装"
        self.state = self.base / "state"
        self.author.mkdir()
        git(self.base, "init", "--bare", "--initial-branch=release-line", str(self.origin))
        git(self.author, "init", "--initial-branch=release-line")
        git(self.author, "config", "user.email", "test@example.invalid")
        git(self.author, "config", "user.name", "Updater Test")
        git(self.author, "config", "core.hooksPath", str(self.base / "no-hooks"))
        (self.author / "package.json").write_text(json.dumps({
            "name": "@deepseek-ai/dsh-root", "version": "0.1.0",
            "packageManager": "pnpm@11.7.0", "engines": {"node": "^22.19.0 || >=24.0.0"},
            "scripts": {"build": "fixture", "dsh": "fixture"},
        }), encoding="utf-8")
        (self.author / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        (self.author / ".gitignore").write_text(".dsh-build/\napps/web/dist/\n.env\n.dsh/\n", encoding="utf-8")
        (self.author / "source.txt").write_text("old", encoding="utf-8")
        (self.author / "scripts").mkdir()
        (self.author / "scripts/client-build-environment.ts").write_text("fixture", encoding="utf-8")
        git(self.author, "add", ".")
        git(self.author, "commit", "-m", "initial")
        self.old = git(self.author, "rev-parse", "HEAD")
        git(self.author, "remote", "add", "origin", str(self.origin))
        git(self.author, "push", "origin", "release-line")
        git(self.base, "clone", str(self.origin), str(self.local))
        git(self.local, "remote", "set-url", "origin", "https://github.com/deepseek-ai/deepseek-harness.git")
        git(self.local, "config", "user.email", "test@example.invalid")
        git(self.local, "config", "user.name", "Updater Test")
        git(self.local, "config", "core.hooksPath", str(self.base / "no-hooks"))
        self.settings = Settings(directory=str(self.local))

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, text="new", message="new version"):
        (self.author / "source.txt").write_text(text, encoding="utf-8")
        git(self.author, "add", ".")
        git(self.author, "commit", "-m", message)
        git(self.author, "push", "origin", "release-line")
        return git(self.author, "rev-parse", "HEAD")

    def engine(self, toolkit=None, **kwargs):
        return Engine(self.settings, self.state, source_url=str(self.origin), toolchain=toolkit or FakeToolchain(), runtime_probe=kwargs.pop("runtime_probe", lambda *_: []), **kwargs)

    def test_detects_default_branch_local_remote_and_behind_count(self):
        target = self.publish()
        result = self.engine().check()
        self.assertEqual(result.local.sha, self.old)
        self.assertEqual(result.remote.sha, target)
        self.assertEqual(result.target_branch, "release-line")
        self.assertEqual((result.behind, result.ahead, result.relation), (1, 0, "behind"))
        self.assertTrue(result.can_update)
        self.assertEqual(result.commits[0].subject, "new version")

    def test_check_never_changes_working_tree(self):
        self.publish()
        before = (self.local / "source.txt").read_bytes()
        self.engine().check()
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), self.old)
        self.assertEqual((self.local / "source.txt").read_bytes(), before)

    def test_remote_commit_is_shown_even_if_future_install_flow_is_unsupported(self):
        (self.author / "package.json").write_text('{"name":"future-name","scripts":{}}', encoding="utf-8")
        target = self.publish(message="future install layout")
        result = self.engine().check()
        self.assertEqual(result.remote.sha, target)
        self.assertEqual(result.relation, "behind")
        self.assertFalse(result.can_update)
        self.assertIn("目标版本的安装流程", " ".join(result.reasons))

    def test_update_builds_and_keeps_user_config_and_backup_ref(self):
        target = self.publish()
        secret = self.local / ".env"
        secret.write_text("DEEPSEEK_API_KEY=local-private-value", encoding="utf-8")
        sessions = self.local / ".dsh/session.jsonl"
        sessions.parent.mkdir()
        sessions.write_text("user session", encoding="utf-8")
        toolkit = FakeToolchain()
        result = self.engine(toolkit).update()
        self.assertEqual(result.local.sha, target)
        self.assertEqual(result.remote.sha, target)
        self.assertTrue(result.build_current)
        self.assertEqual(result.relation, "up_to_date")
        self.assertEqual(toolkit.built, [target])
        self.assertEqual(secret.read_text(), "DEEPSEEK_API_KEY=local-private-value")
        self.assertEqual(sessions.read_text(), "user session")
        record = read_json(self.engine().journal_path)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(git(self.local, "rev-parse", record["backup_ref"]), self.old)

    def test_latest_source_can_repair_missing_build(self):
        result = self.engine().update()
        self.assertEqual(result.local.sha, self.old)
        self.assertTrue(result.build_current)
        toolkit = FakeToolchain()
        self.engine(toolkit).update()
        self.assertEqual(toolkit.built, [])

    def test_local_modified_file_is_never_overwritten(self):
        self.publish()
        changed = self.local / "source.txt"
        changed.write_text("my edits", encoding="utf-8")
        with self.assertRaisesRegex(UpdaterError, "本地修改"):
            self.engine().update()
        self.assertEqual(changed.read_text(), "my edits")
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), self.old)

    def test_untracked_file_blocks_update(self):
        self.publish()
        (self.local / "my-notes.txt").write_text("important", encoding="utf-8")
        self.assertFalse(self.engine().check().can_update)

    def test_ahead_commit_is_preserved(self):
        (self.local / "source.txt").write_text("my committed work", encoding="utf-8")
        git(self.local, "add", ".")
        git(self.local, "commit", "-m", "local change")
        current = git(self.local, "rev-parse", "HEAD")
        result = self.engine().check()
        self.assertEqual(result.relation, "ahead")
        with self.assertRaises(UpdaterError):
            self.engine().update()
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), current)

    def test_divergence_is_not_merged(self):
        self.publish()
        (self.local / "source.txt").write_text("local branch", encoding="utf-8")
        git(self.local, "add", ".")
        git(self.local, "commit", "-m", "local branch")
        result = self.engine().check()
        self.assertEqual(result.relation, "diverged")
        self.assertFalse(result.can_update)
        with self.assertRaises(UpdaterError):
            self.engine().update()

    def test_detached_head_is_recognized_and_blocked(self):
        git(self.local, "checkout", "--detach", self.old)
        result = self.engine().check()
        self.assertEqual(result.local.sha, self.old)
        self.assertFalse(result.can_update)

    def test_different_branch_is_not_switched(self):
        self.publish()
        git(self.local, "checkout", "-b", "my-branch")
        result = self.engine().check()
        self.assertFalse(result.can_update)
        self.assertIn("同名分支", " ".join(result.reasons))
        self.assertEqual(git(self.local, "branch", "--show-current"), "my-branch")

    def test_running_harness_blocks_update(self):
        self.publish()
        result = self.engine(runtime_probe=lambda *_: ["Harness 正在运行"]).check()
        self.assertFalse(result.can_update)

    def test_build_failure_restores_source_and_old_artifacts(self):
        target = self.publish()
        toolkit = FakeToolchain(fail_target=target)
        with self.assertRaisesRegex(UpdaterError, "旧版本源码、依赖和构建已恢复"):
            self.engine(toolkit).update()
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), self.old)
        self.assertEqual(toolkit.built, [target, self.old])
        self.assertEqual((self.local / "apps/web/dist/index.html").read_text(), self.old)
        result = self.engine().check()
        self.assertFalse(result.can_auto_update)
        self.assertTrue(result.can_update)
        self.assertEqual(read_json(self.engine().journal_path)["status"], "rolled_back")

    def test_edits_during_failure_are_preserved_and_manual_recovery_is_required(self):
        target = self.publish()
        with self.assertRaisesRegex(UpdaterError, "自动恢复未完成"):
            self.engine(FakeToolchain(fail_target=target, modify_target=True)).update()
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), target)
        self.assertEqual((self.local / "source.txt").read_text(), "user edit made during build")
        self.assertEqual(read_json(self.engine().journal_path)["status"], "recovery_required")
        git(self.local, "restore", "source.txt")
        result = self.engine().recover()
        self.assertEqual(result.local.sha, self.old)
        self.assertTrue(result.build_current)

    def test_cancelled_build_still_recovers_old_version(self):
        target = self.publish()
        with self.assertRaises(UpdaterError):
            self.engine(FakeToolchain(fail_target=target, cancel_target=True)).update()
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), self.old)
        self.assertEqual(read_json(self.engine().journal_path)["status"], "rolled_back")

    def test_interrupted_update_requires_recovery_and_recovers_from_journal(self):
        target = self.publish()
        engine = self.engine()
        engine.check()
        backup = "refs/dsh-updater/backups/test-interruption"
        git(self.local, "update-ref", backup, self.old)
        atomic_json(engine.journal_path, {
            "directory": str(self.local.resolve()), "previous": self.old, "target": target,
            "branch": "release-line", "backup_ref": backup, "status": "installing",
        })
        git(self.local, "merge", "--ff-only", target)
        self.assertTrue(engine.check().recovery_needed)
        self.assertFalse(engine.check().can_update)
        engine.recover()
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), self.old)

    def test_git_operation_in_progress_blocks_update(self):
        marker = self.local / ".git/MERGE_HEAD"
        marker.write_text(self.old, encoding="utf-8")
        result = self.engine().check()
        self.assertFalse(result.can_update)
        self.assertIn("MERGE_HEAD", " ".join(result.reasons))

    def test_wrong_repository_is_rejected(self):
        git(self.local, "remote", "set-url", "origin", "https://github.com/someone/other.git")
        with self.assertRaisesRegex(UpdaterError, "官方"):
            self.engine().check()

    def test_zip_directory_is_not_assigned_an_invented_commit(self):
        archive = self.base / "archive"
        archive.mkdir()
        self.settings = replace(self.settings, directory=str(archive))
        with self.assertRaisesRegex(UpdaterError, "无法准确识别"):
            self.engine().check()

    def test_nested_directory_is_not_treated_as_install_root(self):
        self.settings = replace(self.settings, directory=str(self.local / "scripts"))
        with self.assertRaises(UpdaterError):
            self.engine().check()

    def test_safety_lock_prevents_concurrent_update(self):
        engine = self.engine()
        with FileLock(engine._root_and_lock()):
            with self.assertRaisesRegex(UpdaterError, "另一个"):
                engine.update()

    def test_invalid_branch_is_rejected_without_shell_execution(self):
        self.settings = replace(self.settings, branch="--upload-pack=malicious")
        with self.assertRaisesRegex(UpdaterError, "分支名称无效"):
            self.engine().check()


class UtilityTests(unittest.TestCase):
    def test_node_engine_range(self):
        for version in ("22.19.0", "22.20.0", "24.0.0", "26.0.0"):
            self.assertTrue(node_satisfies(version, "^22.19.0 || >=24.0.0"))
        for version in ("20.19.0", "22.18.9", "23.10.0"):
            self.assertFalse(node_satisfies(version, "^22.19.0 || >=24.0.0"))
        self.assertTrue(node_satisfies("24.2.1", ">= 24.0.0 <25.0.0"))
        self.assertTrue(node_satisfies("24.2.1", "24.x"))
        self.assertFalse(node_satisfies("24.3.0", "~24.2.0"))
        with self.assertRaises(UpdaterError):
            node_satisfies("24.2.1", "strange-future-syntax")

    def test_official_remote_accepts_common_git_urls(self):
        for url in ("https://github.com/deepseek-ai/deepseek-harness.git", "git@github.com:deepseek-ai/deepseek-harness.git", "ssh://git@github.com/deepseek-ai/deepseek-harness.git", "git+https://github.com/deepseek-ai/deepseek-harness"):
            self.assertTrue(official_remote(url))
        for url in ("https://github.com.evil.test/deepseek-ai/deepseek-harness", "https://github.com/other/deepseek-harness", "file:///some/deepseek-harness"):
            self.assertFalse(official_remote(url))

    def test_settings_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = Settings(directory="中文目录", auto_update=True)
            settings.save(path)
            self.assertEqual(Settings.load(path), settings)
            with self.assertRaises(UpdaterError):
                replace(settings, interval_minutes=0).save(path)
            path.write_text("broken JSON", encoding="utf-8")
            with self.assertRaises(UpdaterError):
                Settings.load(path)

    def test_logs_redact_common_secrets(self):
        text = "https://user:password@github.com/repo token=private sk-abcdefghijklmnop"
        safe = redact(text)
        self.assertNotIn("password@", safe)
        self.assertNotIn("private", safe)
        self.assertNotIn("abcdefghijklmnop", safe)

    def test_runner_nonzero_result_and_cancel(self):
        with self.assertRaisesRegex(UpdaterError, "退出码 4"):
            Runner().run([sys.executable, "-c", "raise SystemExit(4)"])
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            Runner(cancel=event).run([sys.executable, "-c", "print('never')"])

    def test_runner_cancellation_interrupts_silent_child(self):
        event = threading.Event()
        timer = threading.Timer(0.25, event.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(Cancelled):
                Runner(cancel=event).run([sys.executable, "-c", "import time; time.sleep(30)"])
            self.assertLess(time.monotonic() - started, 8)
        finally:
            timer.cancel()

    def test_runner_timeout_interrupts_silent_child(self):
        with self.assertRaisesRegex(UpdaterError, "超时"):
            Runner().run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)

    def test_port_probe_prevents_running_service_update(self):
        with socket.socket() as server, tempfile.TemporaryDirectory() as directory:
            server.bind(("127.0.0.1", 0))
            server.listen()
            reasons = runtime_blockers(Path(directory), server.getsockname()[1])
            self.assertTrue(any("端口" in reason for reason in reasons))


if __name__ == "__main__":
    unittest.main()
