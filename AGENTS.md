# 项目工作说明

本文件适用于整个仓库。本项目是独立的 Python 更新助手，目标是通过 Git 克隆安装的 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)，不是 Harness 官方仓库或发行包。面向普通用户的说明见 [README.md](README.md)；上游代码依据和验证边界分别记录在 `docs/UPSTREAM_REVIEW.md` 与 `docs/VALIDATION.md`。

## 代码位置

- `main.py`：GUI、单次检查/更新和无窗口守护模式的入口。
- `harness_updater/core.py`：仓库识别、Commit 比较、更新事务及恢复。
- `harness_updater/process.py`：子进程执行、取消、超时及运行中保护。
- `harness_updater/storage.py`：设置、日志、更新记录和锁。
- `harness_updater/gui.py`：Tk 界面、后台任务与系统托盘。
- `scripts/package_release.py`、`build.ps1`：Windows 发行包及 SHA-256 文件。
- `tests/test_core.py`、`tests/test_package_release.py`：自动化测试；`tests/gui_smoke.py`、`tests/packaged_smoke.py`：需要真实 Windows 桌面会话的界面验证。

## 更新行为的安全边界

- Commit 必须来自实际 Git HEAD 和可信的官方远端查询，不要根据包版本号、文件内容或 ZIP 目录猜测来源。源码已更新与构建产物已验证是两种不同状态。
- 仅在可证明是目标分支的快进时更新。遇到本地修改、未跟踪文件、分叉历史、detached HEAD、正在运行的 Harness 或无法检查的进程，应暂停并说明原因；不要自动 stash、强推、合并或删除用户文件。
- 修改源码前保留旧 Commit 引用并写入恢复记录。构建失败或取消时，只有确认 HEAD 和工作树仍处于预期状态才能尝试恢复；若用户在过程中改动了目录，保留其改动并要求人工处理。不要用 `git clean` 或 `git reset --hard` 实现更新或恢复。
- 不读取或迁移 `~/.dsh`、`DSH_HOME` 中的用户数据和密钥，不把凭据写入日志。忽略文件、会话及本地配置留在原处。自动更新默认关闭；失败后不能持续重试同一个失败 Commit。
- 从目标 Commit 的 `package.json` 读取 Node.js 和 pnpm 要求；不要把当前上游版本号当成永久常量。构建使用目标版本的 pnpm 和上游脚本，不改动用户的全局 Git、Node.js 或 Corepack 配置。
- Tk 控件只能在主线程操作。耗时的 Git、网络和构建工作在后台执行，并通过现有队列更新界面；保持取消与退出时的恢复路径。

## 修改与验证

- 行为或打包逻辑变更后运行 `python -m unittest discover -s tests -v` 和 `python -m compileall -q main.py harness_updater scripts`。核心测试使用临时 Git 仓库与受控构建替身，不等同于完整 Harness 构建或应用端到端测试。
- 涉及 GUI 时，在有桌面会话的 Windows 上运行 `python tests/gui_smoke.py`；涉及 EXE 时运行 `.\build.ps1`，再运行 `python tests/packaged_smoke.py`。打包脚本会预检 Tcl/Tk；不要发布缺失 Tk 资源的 EXE。
- `docs/images/gui-update.png` 是真实 Tk 界面配演示 Commit 数据；`docs/images/gui-packaged.png` 是真实打包 EXE 的窗口。更换截图时实际启动对应程序、目视核对画面，保持 README 的截图说明准确。
- 更改支持范围、安全行为或上游适配时同步更新 README 和相关 `docs/` 说明。仅修改文档时检查链接、格式和 `git diff --check` 即可。
- 不提交 `dist/`、`build/`、`.build-venv/`、`.gui-state/`、`.test-state/` 或任何本地凭据、日志。它们已列在 `.gitignore` 中。

## CI 与发行

- `.github/workflows/ci.yml` 在 Windows/Ubuntu、Python 3.10/3.12 上测试，并在 Windows 构建 CI Artifact。推送代码后检查远程结果；本机测试不能代替远程 CI。
- 发布新版本时同步更新 `pyproject.toml` 与 `harness_updater/__init__.py` 的版本，检查 `docs/使用说明.txt`、README 中的版本示例，然后先让 `main` 的 CI 通过。推送与程序版本一致的注释标签 `vX.Y.Z` 后，`.github/workflows/release.yml` 会从该标签重新测试、构建并发布 EXE、ZIP 和 `SHA256SUMS.txt`。
- 发行文件由 Action 在干净的 Windows runner 上生成。发布后核对 Release 的附件、ZIP 内容和 SHA-256；不要把本地 `dist/` 直接当作远程构建结果。
