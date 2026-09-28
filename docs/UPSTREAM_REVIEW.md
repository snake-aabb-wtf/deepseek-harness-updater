# 上游代码阅读与适配说明

阅读日期：2026-09-27。读取官方默认分支 `master`，固定分析 Commit：`477b4f420553e8a52c2fbccc464d7561b239c443`，提交时间为 2026-09-24 21:39:59 +08:00，根包版本 `0.1.7-rc.2`。

本次围绕安装、启动、构建、用户数据路径和构建来源进行代码阅读，没有逐一审计仓库所有插件与业务模块。

## 直接影响更新程序的源码

| 上游文件 | 读到的行为 | 更新助手的适配 |
| --- | --- | --- |
| [README.zh.md](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/README.zh.md) | 源码运行先 `pnpm install`，再 `pnpm run build`，最后 `pnpm dsh web` | 完整更新包含安装和构建；构建后由用户重新启动 Harness |
| [package.json](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/package.json) | pnpm 固定为 11.7.0，Node 为 `^22.19.0 || >=24.0.0`；`build` 和 `dsh` 脚本是公共入口 | 从目标 Commit 读取要求，独立缓存精确 pnpm 版本 |
| [scripts/build.ts](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/scripts/build.ts) | 顺序构建 native-system、lib、web，最后写入完整构建记录 | 运行完整 `build`，退出码成功后再验证记录 |
| [scripts/client-build-environment.ts](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/scripts/client-build-environment.ts) | 七位 Commit 写入 `DSH_CLIENT_COMMIT_HASH`；记录位于 `.dsh-build/client-build-environment.json`；`readClientBuildRecord` 校验产物摘要 | GUI 显示构建记录；更新后调用上游自己的校验函数，并验证 Commit 和 dirty 标志 |
| [scripts/pnpm-invocation.ts](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/scripts/pnpm-invocation.ts) | 构建子命令通过当前 pnpm 的 `npm_execpath` 解析 | 用 Node 直接启动 pnpm.cjs，保持标准 pnpm 生命周期环境 |
| [scripts/install-lefthook.mjs](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/scripts/install-lefthook.mjs) | postinstall 设置 worktree 本地开发钩子；`CI=true` 时跳过 | 用户更新时设置 CI，不改动开发钩子配置；源码快进也不执行本地钩子 |
| [pnpm-workspace.yaml](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/pnpm-workspace.yaml) | workspace 与经过上游允许的依赖构建脚本 | 使用上游锁文件和策略，不使用 `--ignore-scripts` 安装 Harness 依赖 |
| [apps/cli/src/bin.ts](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/apps/cli/src/bin.ts)、[args.ts](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/apps/cli/src/args.ts) | CLI 解析版本、profile 及启动参数 | 使用 `pnpm dsh --version` 验证 CLI 入口，避免实际启动会话 |
| [packages/util/home-paths/src/index.ts](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/packages/util/home-paths/src/index.ts) | 用户数据默认在 `~/.dsh`；可由 DSH_HOME 或显式配置覆盖 | 安装目录与数据目录分开；更新程序不读取或迁移数据根目录 |
| [.gitignore](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/.gitignore) | `.env`、依赖、lib、Web dist、会话及构建记录被忽略 | 更新时保留忽略文件；不自动清理工作树 |
| [native/system/scripts/build.ts](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/native/system/scripts/build.ts)、[docs/development.zh.md](https://github.com/deepseek-ai/deepseek-harness/blob/477b4f420553e8a52c2fbccc464d7561b239c443/docs/development.zh.md) | Windows 与 WSL 需各自安装依赖；部分 Unix 原生构建需要编译器与 Node 头文件 | 不跨环境复用依赖；构建错误保留诊断，依赖恢复尽力执行 |

## 实现取舍

- 采用标准库 Tkinter，另外仅使用 psutil 检查进程、pystray / Pillow 提供托盘；便于在 Windows 打包成单文件 EXE。
- 官方分支名称由 `git ls-remote --symref ... HEAD` 查询，不假定主分支永远叫 main 或 master。
- 本地 HEAD 与远端 SHA 直接来自 Git。使用祖先关系和提交数量区分相同、落后、领先和分叉。
- 读取远端使用专用引用 `refs/dsh-updater/remotes/...`，不会改动用户的 origin/upstream 配置。
- 手动和自动更新共用 Engine；GUI 通过队列接收工作线程事件，所有 Tk 操作保持在主线程。
- 更新状态原子写入；备份引用在修改源码前创建。崩溃后暂停更新，先恢复旧 Commit。
- 完整构建能力归属上游。Python 不尝试自己实现 TypeScript 构建，也不安装系统 Git、Node、编译器或修改系统级设置。

## 已执行的验证与边界

核心测试、GUI 和 EXE 实测结果见 [VALIDATION.md](VALIDATION.md)。真实官方源码克隆已通过程序的 Commit 查询，准确识别本机和远端均为 `477b4f420553e8a52c2fbccc464d7561b239c443`，默认分支 `master`，ahead / behind 均为 0。

本机 3080 端口占用及权限不足的 Node 进程也被识别为阻止更新的原因。程序因此没有对该临时克隆执行真实上游依赖安装或完整构建；完整 Harness 安装/构建仍需在用户自己的空闲安装环境验证。临时仓库中的快进、失败恢复、取消恢复使用真实 Git 操作，而构建成功/失败由受控替身提供。

GUI 当前显示的“构建记录对应源码”是快速读取记录的结果；完整产物摘要校验在一次实际更新/构建成功后执行。它不是对正在运行的另一个 Harness 进程的版本判断，也不是应用功能或 API 的端到端健康证明。
