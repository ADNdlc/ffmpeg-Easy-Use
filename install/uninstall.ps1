#Requires -Version 5.1
<#
  ffmpeg-Easy-Use —— 一键卸载脚本（Windows PowerShell 5.1+）

  移除工具目录 <根>\script （送进回收站，可还原）；
  不会删除 <根>\bin 里的 ffmpeg，也不动 ffmpeg 所在目录里的其它文件。

  用法:
    powershell -ExecutionPolicy Bypass -File uninstall.ps1
    powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Root <目录>   # 指定安装根
    powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Yes           # 跳过确认
    powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Hard          # 永久删除（不进回收站）
#>
param(
    [string]$Root = '',
    [switch]$Yes,
    [switch]$Hard
)

$ErrorActionPreference = 'Stop'

function Info($m) { Write-Host $m -ForegroundColor Cyan }
function Good($m) { Write-Host $m -ForegroundColor Green }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Bad($m) { Write-Host $m -ForegroundColor Red }
function Die($m) { Bad ''; Bad "[卸载失败] $m"; exit 2 }

function Norm([string]$p) {
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p)
    if ($full.Length -gt 3) { $full = $full.TrimEnd('\') }
    return $full
}

function Find-FfmpegDir {
    foreach ($n in @('ffmpeg.exe', 'ffmpeg')) {
        $c = $null
        try { $c = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1 } catch { }
        if ($c) {
            $p = if ($c.Source) { $c.Source } else { $c.Path }
            if ($p) { return (Split-Path -Parent ($p -replace '^\\\\\?\\', '')) }
        }
    }
    try {
        $w = @(& where.exe ffmpeg.exe 2>$null | Where-Object { $_ -and $_.Trim() } | Select-Object -First 1)
        if ($w.Count -gt 0) { return (Split-Path -Parent $w[0].Trim()) }
    } catch { }
    return ''
}

function ToRecycleBin([string]$path) {
    try {
        Add-Type -AssemblyName Microsoft.VisualBasic -ErrorAction Stop
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
            $path,
            [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
            [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)
        return $true
    } catch { return $false }
}

Info ''
Info '============================================================'
Info '  ffmpeg-Easy-Use · 一键卸载'
Info '  （只移除工具目录，ffmpeg 本体保留）'
Info '============================================================'
Info ''

# ---------- 定位要移除的 script 目录 ----------
$ScriptDir = ''
if ($Root) {
    $ScriptDir = Join-Path (Norm $Root) 'script'
} else {
    $here = Split-Path -Parent $PSScriptRoot                     # 若从已安装位置运行，这里就是 script\
    if ($here -and (Test-Path -LiteralPath (Join-Path $here 'core\vpipe.py'))) {
        $ScriptDir = $here
    } else {
        $bin = Find-FfmpegDir
        if ($bin) {
            $r = if ((Split-Path -Leaf $bin) -ieq 'bin') { Split-Path -Parent $bin } else { $bin }
            $ScriptDir = Join-Path $r 'script'
        }
    }
}

if (-not $ScriptDir) {
    Die '未能定位工具目录。请用 -Root 指定安装根（例如 -Root D:\Tool\FFmpeg）。'
}
if (-not (Test-Path -LiteralPath $ScriptDir)) {
    Die "工具目录不存在，无需卸载: $ScriptDir"
}

# 安全闸：确认它确实是本工具，避免误删同名目录
if (-not (Test-Path -LiteralPath (Join-Path $ScriptDir 'core\vpipe.py'))) {
    Die "该目录不是本工具（未找到 core\vpipe.py），已中止，未做任何删除: $ScriptDir"
}

$RootDir = Split-Path -Parent $ScriptDir
$BinDir = Join-Path $RootDir 'bin'
Info "将移除: $ScriptDir"
Info "将保留: $(if (Test-Path -LiteralPath $BinDir) { $BinDir + '（ffmpeg）' } else { $RootDir + '\ 下的 ffmpeg（若存在）' })"
Info ''

if (-not $Yes) {
    $a = ''
    try { $a = Read-Host '确认卸载？工具目录将' + $(if ($Hard) { '被永久删除' } else { '送入回收站（可还原）' }) + ' [Y/N]' } catch { }
    if ($a -notmatch '^(y|Y|是)') { Warn '已取消。'; exit 0 }
}

# ---------- 执行移除 ----------
$done = $false
if ($Hard) {
    Remove-Item -LiteralPath $ScriptDir -Recurse -Force
    $done = $true
} else {
    $done = ToRecycleBin $ScriptDir
    if (-not $done) {
        $bak = $ScriptDir + '.removed-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Move-Item -LiteralPath $ScriptDir -Destination $bak -Force
        Warn "当前环境不支持送回收站，已改名为: $bak（确认无用后可手动删除）"
        $done = $true
    }
}

if (-not $done) { Die "移除失败: $ScriptDir" }

Info ''
Good '=========================== 卸载完成 ==========================='
Info "已移除工具目录: $ScriptDir"
if (Test-Path -LiteralPath $BinDir) {
    Good "ffmpeg 已保留（未删除）: $BinDir"
} else {
    Info "ffmpeg 所在目录未被改动: $BinDir"
}
Info ''
Info '提示：'
Info '  1) ffmpeg 本体仍在原处，可继续被其它软件使用；如需彻底清理请手动删除它。'
Info '  2) 若你的视频目录里还留有本工具生成的产物，'
Info '     它们在各自工作区的 <目标目录>\ 与 <目标目录>\.data\ 下，可自行删除。'
Info '============================================================'
exit 0
