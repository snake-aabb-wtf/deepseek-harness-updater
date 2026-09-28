param([switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskEnvironment = Join-Path $PSScriptRoot '.build-venv'
$taskPython = Join-Path $taskEnvironment 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv $taskEnvironment
    if ($LASTEXITCODE -ne 0) { throw '无法创建构建环境，请先安装 Python 3.10 或更高版本。' }
}
if (-not $SkipInstall) {
    & $taskPython -m pip install --timeout 25 --retries 2 -r (Join-Path $PSScriptRoot 'requirements-build.txt')
    if ($LASTEXITCODE -ne 0) { throw '构建依赖安装失败，请检查网络和 pip 配置。' }
}
& $taskPython -c 'import tkinter; root = tkinter.Tk(); root.withdraw(); root.destroy()'
if ($LASTEXITCODE -ne 0) { throw '当前 Python 的 Tcl/Tk 不可用，不能打包有界面的 EXE。' }
& $taskPython -m PyInstaller --noconfirm --clean --onefile --windowed --name HarnessUpdater --collect-submodules pystray --hidden-import PIL._tkinter_finder (Join-Path $PSScriptRoot 'main.py')
if ($LASTEXITCODE -ne 0) { throw 'EXE 打包失败，请检查上方日志。' }
& $taskPython (Join-Path $PSScriptRoot 'scripts\package_release.py')
if ($LASTEXITCODE -ne 0) { throw '发行文件打包或校验失败。' }
Write-Output "打包完成：$(Join-Path $PSScriptRoot 'dist\HarnessUpdater.exe')"
