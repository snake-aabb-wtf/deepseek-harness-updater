# 本次验证记录

日期：2026-09-27 至 2026-09-28。环境：Windows 11 x64、Python 3.12.10、Git 2.54.0、Node.js 24.16.0。

## 核心逻辑

命令：

```powershell
python -m unittest discover -s tests -v
```

最终完整运行 32 项测试，全部通过，其中包含 30 项版本检查、Git 更新与恢复等核心测试，以及 2 项发行包内容和校验文件测试。

若只需复查远端版本检测相关用例，可运行：

```powershell
python -m unittest discover -s tests -p test_core.py -k remote -v
```

覆盖：真实 Git fetch 和 HEAD 读取、默认分支发现、落后数量、快进更新、旧 Commit 引用保存、中文 / 空格 / & 路径、配置和会话保留、本地修改、未跟踪文件、本地领先、历史分叉、detached HEAD、分支不同、错误目录、ZIP 目录、运行中保护、Git 操作未结束、并发文件锁、失败恢复、取消恢复、中断后的恢复、Node 版本范围、日志脱敏和子进程取消 / 超时。

依赖安装和构建使用受控替身。Git 操作与恢复记录使用真实临时仓库，测试不会修改用户的实际 Harness 安装。

## 官方仓库与工具链

通过程序执行：

```powershell
python main.py --check --directory "<临时官方克隆目录>" --state-dir .test-state
```

结果：

- 本机 HEAD：`477b4f420553e8a52c2fbccc464d7561b239c443`
- 官方最新 HEAD：`477b4f420553e8a52c2fbccc464d7561b239c443`
- 本地与远端分支：`master`
- 关系：`up_to_date`；ahead / behind 均为 0
- 工作树：干净
- 构建记录：尚无构建记录，正确显示需要构建
- 运行前检查：正确识别 3080 占用，以及当前沙箱权限下无法检查的 Node 进程，阻止实际更新

`Toolchain.prepare()` 使用目标 Commit 的真实 package.json，成功通过 Node 版本检查，下载独立缓存的 pnpm `11.7.0`，并执行其入口验证版本。源码目录未进行真实依赖安装与完整 Harness 构建。

## GUI 与打包版

```powershell
python tests/gui_smoke.py
python tests/packaged_smoke.py
```

实际 Tk 控件验证通过：空目录引导、可更新状态、修改保护、忙碌时禁用控件、恢复按钮、小窗口滚动、工作线程失败后的 Commit / 恢复状态刷新、失败关闭自动更新、后台任务使用已保存设置、最小化。

Windows 单文件 EXE 实际启动验证通过：完整 Tk 资源、窗口创建、托盘初始化、关闭窗口后仍在后台运行。测试程序使用独立配置目录，完成后仅终止自身启动的 EXE 进程。

构建脚本现先验证 Tcl/Tk 可正常创建窗口，再允许 PyInstaller 打包；已重新构建并再次运行上述打包版冒烟测试。发行 ZIP 经解包校验，SHA-256 文件包含 EXE 与 ZIP 的摘要。

截图：

- `docs/images/gui-update.png`：更新界面，使用演示版本数据
- `docs/images/gui-protected-small.png`：小窗口保护提示
- `docs/images/gui-protected-small-bottom.png`：小窗口滚动到底部后的控件
- `docs/images/gui-packaged.png`：实际打包 EXE 启动窗口

## 验证范围

这些结果证明版本识别、Git 事务保护、恢复状态和 Windows 界面 / 打包功能在本次环境可以运行。未执行真实 Harness 的完整依赖安装、TypeScript / 原生产物构建或应用功能端到端测试；也未在 macOS / Linux 上验证 GUI 和托盘。GUI 中的对应构建记录是快速状态提示，完整摘要校验在实际更新构建结束后由上游函数执行。
