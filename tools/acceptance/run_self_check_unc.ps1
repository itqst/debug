# 交付自检（网络路径版）：不走 ShellExecute 隐藏启动打包版并有界等待；网络路径经 ShellExecute 会弹安全警告卡住

$ErrorActionPreference = 'Stop'
# 不走 ShellExecute：网络路径上的 exe 经 ShellExecute 启动会弹“打开文件 - 安全警告”，隐藏窗口时看不见就一直等
function Run-Check($exe, $out) {
    if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Recurse -Force }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $exe
    $info.Arguments = "--db :memory: --self-check `"$out`""
    $info.UseShellExecute = $false
    $info.WindowStyle = 'Hidden'
    $p = [System.Diagnostics.Process]::Start($info)
    if (-not $p.WaitForExit(180000)) { $p.Kill(); return "超时，已结束进程" }
    return "退出码 $($p.ExitCode)"
}
try {
    "UNC: " + (Run-Check '\\wsl.localhost\Ubuntu-22.04\home\itqst\work\TTA\dog_debug\dist\KunKunDebug\KunKunDebug.exe' (Join-Path $env:TEMP 'unc 隐藏 自检'))
} catch { 'ERROR: ' + $_.Exception.Message }
