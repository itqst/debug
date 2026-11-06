# 从源码启动；解释器顺序和 start_windows.bat 一样：DOG_PYTHON → 原项目环境 → build_windows.bat 建的 .venv-win
# 存成带 BOM 的 UTF-8：Windows PowerShell 5.1 把不带 BOM 的文件按系统代码页读，中文会乱
param([switch]$Demo)
$ErrorActionPreference = 'Stop'
$candidates = @(
    $env:DOG_PYTHON,
    (Join-Path $env:USERPROFILE '.codex\runtimes\robotdog-debug-studio\Scripts\python.exe'),
    (Join-Path $PSScriptRoot '.venv-win\Scripts\python.exe')
)
$pythonPath = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (!$pythonPath) {
    throw '没有找到 Python 环境：先运行一次 build_windows.bat（会建 .venv-win），或设置 DOG_PYTHON，见 README.md'
}
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($Demo) { & $pythonPath (Join-Path $PSScriptRoot 'main.py') --demo }
    else { & $pythonPath (Join-Path $PSScriptRoot 'main.py') }
} finally { Pop-Location }
