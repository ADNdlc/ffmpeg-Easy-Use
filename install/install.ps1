#Requires -Version 5.1
<#
  ffmpeg-Easy-Use —— 初始化脚本（Windows PowerShell 5.1+）

  仓库目录就是使用目录：把仓库放到合适位置后，双击「初始化.bat」（或运行本脚本），
  脚本只负责检测/构建运行环境并就地生成配置文件，不会复制、不会移动任何程序文件。

  用法（默认，无需任何参数）:
    powershell -ExecutionPolicy Bypass -File install.ps1
      先扫描机器上的 ffmpeg：扫到就记下它所在目录直接复用；没扫到就询问是否下载到 <仓库>\bin

  可选参数:
    -Mode download   未检测到 ffmpeg 时跳过询问，直接下载 essentials 版
    -NoDownload      即使没扫到 ffmpeg 也绝不下载（只用 -FfmpegZip 或手动放置）
    -FfmpegZip <x>   用本地 zip 文件或 http(s) URL 代替在线下载
    -Force           所有交互询问一律确认（下载不再询问）
    -DryRun          只预演，不修改任何文件

  ffmpeg 解析优先级:
    1) <仓库>\bin 里已有 ffmpeg.exe + ffprobe.exe  -> 直接用（<仓库>\bin）
    2) PATH 里检测到 ffmpeg.exe                    -> 记下它所在目录，直接复用、不下载、不复制
    3) 都没有                                       -> 下载 essentials 到 <仓库>\bin（先询问）

  初始化产物（全部落在仓库目录内）:
    core\config.json   ← 由 core\config.example.json 就地生成；
                          已存在则保留其中的档位/画质设置，只更新 ffmpeg 目录
    bin\               ← 仅在没有可用 ffmpeg 时产生（自动下载的 ffmpeg）
#>
param(
    [ValidateSet('auto', 'download')]
    [string]$Mode = 'auto',
    [switch]$NoDownload,
    [switch]$Force,
    [switch]$DryRun,
    [string]$FfmpegZip = ''
)

$ErrorActionPreference = 'Stop'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch { }  # Tls12

$ONLINE = @(
    'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
    'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
)

function Info($m) { Write-Host $m -ForegroundColor Cyan }
function Good($m) { Write-Host $m -ForegroundColor Green }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Bad($m) { Write-Host $m -ForegroundColor Red }
function Die($m) { Bad ''; Bad "[初始化失败] $m"; exit 2 }
function Yes($m) {
    if ($Force) { return $true }
    try { $a = Read-Host ("$m [Y/N]") } catch { return $false }
    return ($a -match '^(y|Y|是)')
}

function JsonStr([string]$s) { return '"' + $s.Replace('\', '\\').Replace('"', '\"') + '"' }

# 只改 config.json 里的两个字符串字段，其余内容保持原样（避免整体重序列化破坏格式）
function Set-JsonStrField([string]$text, [string]$name, [string]$value) {
    $rx = New-Object System.Text.RegularExpressions.Regex('("' + [regex]::Escape($name) + '"\s*:\s*)"(?:\\.|[^"\\])*"')
    if ($rx.IsMatch($text)) {
        $ev = [System.Text.RegularExpressions.MatchEvaluator] { param($m) $m.Groups[1].Value + (JsonStr $value) }
        return $rx.Replace($text, $ev)
    }
    return [regex]::Replace($text, '\{', ('{' + [Environment]::NewLine + '  ' + (JsonStr $name) + ': ' + (JsonStr $value) + ','), 1)
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

# 目录里是否同时具备 ffmpeg.exe 与 ffprobe.exe
function Has-Tools([string]$dir) {
    if (-not $dir) { return $false }
    return ((Test-Path -LiteralPath (Join-Path $dir 'ffmpeg.exe')) -and (Test-Path -LiteralPath (Join-Path $dir 'ffprobe.exe')))
}

function Download([string]$url, [string]$dest) {
    $old = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 3600
    } finally { $ProgressPreference = $old }
}

# 解开 zip，把 ffmpeg/ffprobe/ffplay 三个 exe 拷进 destBin
function Install-FromZip([string]$zip, [string]$destBin, [string]$tmpRoot) {
    if (-not (Test-Path -LiteralPath $zip)) { throw "压缩包不存在: $zip" }
    $tmp = Join-Path $tmpRoot '_dl_tmp'
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
    try {
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        Info "解压中…"
        Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
        $all = @(Get-ChildItem -LiteralPath $tmp -Recurse -File | Where-Object { $_.Name -ieq 'ffmpeg.exe' -or $_.Name -ieq 'ffprobe.exe' -or $_.Name -ieq 'ffplay.exe' })
        if (-not ($all | Where-Object { $_.Name -ieq 'ffmpeg.exe' })) { throw "压缩包内未找到 ffmpeg.exe" }
        if (-not ($all | Where-Object { $_.Name -ieq 'ffprobe.exe' })) { throw "压缩包内未找到 ffprobe.exe" }
        New-Item -ItemType Directory -Force -Path $destBin | Out-Null
        foreach ($e in $all) {
            Copy-Item -LiteralPath $e.FullName -Destination (Join-Path $destBin $e.Name) -Force
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ------------------------------------------------------------------ 开始
Info ''
Info '============================================================'
Info '  ffmpeg-Easy-Use · 初始化'
Info '  仓库目录即使用目录：只检测/构建运行环境，不复制任何文件'
Info '============================================================'
Info ("模式: " + $(if ($Mode -eq 'auto') { '复用已有 ffmpeg（auto）' } else { '无 ffmpeg 时直接下载（download）' }))
Info ''

$Repo = Split-Path -Parent $PSScriptRoot                        # install\ 的上一级 = 仓库根
if (-not (Test-Path -LiteralPath (Join-Path $Repo 'core\vpipe.py'))) {
    Die "未找到程序文件: $Repo\core\vpipe.py（请确保 install\ 与 core\ 在同一仓库目录下）"
}

$CoreDir = Join-Path $Repo 'core'
$BinDir = Join-Path $Repo 'bin'
$ExampleCfg = Join-Path $CoreDir 'config.example.json'
$CfgPath = Join-Path $CoreDir 'config.json'

if (-not (Test-Path -LiteralPath $ExampleCfg)) { Die "缺少配置模板: $ExampleCfg" }
Info "仓库目录 : $Repo"

# ---------- 1. 解析 ffmpeg 目录 ----------
$detected = Find-FfmpegDir
$FfmpegDir = ''
$needFfmpeg = $false
if (Has-Tools $BinDir) {
    $FfmpegDir = $BinDir                                        # 仓库内已有可用 bin\
    Good "使用仓库内的 ffmpeg: $BinDir"
} elseif (Has-Tools $detected) {
    $FfmpegDir = $detected                                      # 直接复用系统 ffmpeg
    Good "检测到系统 ffmpeg: $detected（直接复用，不下载、不复制）"
} else {
    $FfmpegDir = $BinDir                                        # 没有可用 ffmpeg：需要在 bin\ 里获取
    $needFfmpeg = $true
    Warn "未检测到可用的 ffmpeg（也可把 ffmpeg.exe / ffprobe.exe 手动放进 $BinDir）"
}

if ($DryRun) {
    $hasCfg = Test-Path -LiteralPath $CfgPath
    Info '------------------ 预演（不修改任何文件） ------------------'
    Info "配置文件 : $CfgPath$(if ($hasCfg) { '（已存在，将保留档位/画质设置，仅更新路径）' } else { '（将由 config.example.json 生成）' })"
    Info "ffmpeg   : $(if ($needFfmpeg) { "$BinDir（需要获取：会先询问是否下载）" } else { $FfmpegDir })"
    Info "来源     : $(if ($FfmpegZip) { "指定来源: $FfmpegZip" } elseif ($needFfmpeg) { $ONLINE[0] } else { '复用已有 ffmpeg' })"
    Info '----------------------------------------------------------'
    exit 0
}

# ---------- 2. 获取 ffmpeg（如需要） ----------
if ($needFfmpeg) {
    if ($NoDownload -and -not $FfmpegZip) {
        Die "未检测到可用的 ffmpeg，且已指定 -NoDownload。请把手动准备好的 ffmpeg.exe / ffprobe.exe 放到 $BinDir 后重试。"
    }
    if (-not ($FfmpegZip -or $Mode -eq 'download')) {
        if (-not (Yes "未检测到 ffmpeg。是否自动下载 essentials 版（主源 gyan.dev，约 109 MB；失败自动改用 BtbN 备用源）？")) {
            Die "已取消：未获取 ffmpeg。可把 ffmpeg.exe / ffprobe.exe 手动放到 $BinDir，或用 -Mode download 直接下载后重试。"
        }
    }
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    if ($FfmpegZip) {
        Info "使用指定来源: $FfmpegZip"
        $zipPath = Join-Path $env:TEMP ('ffmpeg_easyuse_' + (Get-Date -Format 'yyyyMMddHHmmss') + '.zip')
        $err = ''
        try {
            if ($FfmpegZip -match '^https?://') {
                Info "下载中…（文件较大，请耐心等待）"
                Download $FfmpegZip $zipPath
            } else {
                Copy-Item -LiteralPath $FfmpegZip -Destination $zipPath -Force
            }
            Install-FromZip $zipPath $BinDir $env:TEMP
        } catch { $err = $_.Exception.Message } finally {
            Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
        }
        if ($err) { Die "获取 ffmpeg 失败: $err" }
    } else {
        $ok = $false
        foreach ($u in $ONLINE) {
            $zipPath = Join-Path $env:TEMP ('ffmpeg_easyuse_' + (Get-Date -Format 'yyyyMMddHHmmss') + '.zip')
            try {
                Info "下载中（essentials 版，约 109 MB）: $u"
                Download $u $zipPath
                Install-FromZip $zipPath $BinDir $env:TEMP
                $ok = $true
            } catch { Warn ("该来源失败: " + $_.Exception.Message) } finally {
                Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
            }
            if ($ok) { break }
        }
        if (-not $ok) { Die '自动下载 ffmpeg 失败（网络受限？）。可先用 -FfmpegZip 指定本地 zip，或手动把 ffmpeg.exe / ffprobe.exe 放到 ' + $BinDir + ' 后重试。' }
    }
    $FfmpegDir = $BinDir
}

if (-not (Test-Path -LiteralPath (Join-Path $FfmpegDir 'ffmpeg.exe'))) { Die "缺少 ffmpeg.exe: $FfmpegDir" }
if (-not (Test-Path -LiteralPath (Join-Path $FfmpegDir 'ffprobe.exe'))) { Die "缺少 ffprobe.exe: $FfmpegDir" }
Good "ffmpeg 就绪: $FfmpegDir"

# ---------- 3. 就地生成 / 更新 core\config.json ----------
$cfgText = ''
if (Test-Path -LiteralPath $CfgPath) {
    try {
        $raw = Get-Content -LiteralPath $CfgPath -Raw -Encoding UTF8
        $null = $raw | ConvertFrom-Json
        $cfgText = $raw
        Info '已存在 config.json：保留其中的档位/画质等设置，仅更新 ffmpeg 目录'
    } catch {
        $bak = $CfgPath + '.bad-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Move-Item -LiteralPath $CfgPath -Destination $bak -Force
        Warn "原 config.json 无法解析，已备份为: $bak"
    }
}
if (-not $cfgText) {
    $cfgText = Get-Content -LiteralPath $ExampleCfg -Raw -Encoding UTF8
    Info '由 core\config.example.json 就地生成 config.json'
}
if ($cfgText -notmatch '"profiles"') { Die "配置模板无效（缺少 profiles 段）: $ExampleCfg" }

$cfgText = Set-JsonStrField $cfgText 'ffmpeg_dir' $FfmpegDir
[System.IO.File]::WriteAllText($CfgPath, $cfgText, (New-Object System.Text.UTF8Encoding($false)))

try { $null = (Get-Content -LiteralPath $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json) }
catch { Warn "写入后的 config.json 校验失败，请检查: $CfgPath" }

# ---------- 4. 完成 ----------
Info ''
Good '=========================== 初始化完成 ==========================='
Info "仓库目录 : $Repo"
Info "ffmpeg   : $FfmpegDir"
Info "配置文件 : $CfgPath"
Info ''
Info '用法：把视频文件或文件夹拖到「ffmpeg批量压缩.bat」图标上即可。'
Info '以后重新拉取代码或移动了仓库位置，再运行一次本初始化即可。'
Info '==============================================================='
exit 0
