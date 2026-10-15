#Requires -Version 5.1
<#
  ffmpeg-Easy-Use —— 一键安装脚本（Windows PowerShell 5.1+）

  用法:
    powershell -ExecutionPolicy Bypass -File install.ps1 -Mode auto        # 复用已安装的 ffmpeg
    powershell -ExecutionPolicy Bypass -File install.ps1 -Mode download    # 没有 ffmpeg 时自动下载(essentials 版)
  可选参数:
    -Root <目录>    显式指定安装根目录（根内创建 script\ ；ffmpeg 位于 <根>\bin 或直接位于根）
    -Force          目标 script\ 已存在且不是本工具时，自动备份后继续（默认改为交互确认）
    -FfmpegZip <x>  用本地 zip 文件或 http(s) URL 代替在线下载

  安装根解析优先级:
    1) -Root 显式指定
    2) 检测到 ffmpeg.exe -> 取它所在目录的上一级（若该层目录名是 bin，则作为 <根>\bin；<根>\script 与之同级）
    3) 都没有 -> 安装器所在盘根下 <盘>:\Tool\FFmpeg

  本脚本只“新增/覆盖”文件，不删除任何用户数据；唯一的重命名只发生在命名冲突备份时。
#>
param(
    [ValidateSet('auto', 'download')]
    [string]$Mode = 'auto',
    [string]$Root = '',
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
function Die($m) { Bad ''; Bad "[安装失败] $m"; exit 2 }
function Yes($m) {
    if ($Force) { return $true }
    try { $a = Read-Host ("$m [Y/N]") } catch { return $false }
    return ($a -match '^(y|Y|是)')
}

function Norm([string]$p) {
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p)
    if ($full.Length -gt 3) { $full = $full.TrimEnd('\') }
    return $full
}
function Same([string]$a, [string]$b) { return [string]::Equals($a, $b, [StringComparison]::OrdinalIgnoreCase) }

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

function RootFromBinDir([string]$binDir) {
    if (-not $binDir) { return '' }
    if ((Split-Path -Leaf $binDir) -ieq 'bin') { return (Split-Path -Parent $binDir) }
    return $binDir
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
Info '  ffmpeg-Easy-Use · 一键安装'
Info '============================================================'
Info ("模式: " + $(if ($Mode -eq 'auto') { '复用已安装的 ffmpeg（auto）' } else { '无 ffmpeg 时自动下载（download）' }))
Info ''

$SrcRepo = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $SrcRepo 'core\vpipe.py'))) {
    Die "未找到程序文件: $SrcRepo\core\vpipe.py（请确保 install\ 与 core\ 在同一仓库目录下）"
}

# ---------- 1. 解析安装根 / ffmpeg 目录 ----------
$detected = Find-FfmpegDir
$Root = if ($Root) { Norm $Root } else { '' }

if ($Root) {
    if (Test-Path -LiteralPath (Join-Path $Root 'ffmpeg.exe')) {
        $FfmpegDir = $Root
    } elseif (Test-Path -LiteralPath (Join-Path $Root 'bin\ffmpeg.exe')) {
        $FfmpegDir = Join-Path $Root 'bin'
    } else {
        $FfmpegDir = Join-Path $Root 'bin'
        if ($detected) { Warn "指定根内没有 ffmpeg，将复用已检测到的: $detected" }
    }
    Info "安装根（-Root 指定）: $Root"
} else {
    if ($detected) {
        $FfmpegDir = $detected
        $Root = RootFromBinDir $FfmpegDir
        Good "检测到 ffmpeg: $FfmpegDir"
        Info "将安装到它的同级目录: $Root"
    } else {
        $drive = Split-Path -Qualifier $SrcRepo          # 例如 "C:"
        $Root = Norm ($drive + '\Tool\FFmpeg')
        $FfmpegDir = Join-Path $Root 'bin'
        Warn "未检测到 ffmpeg，默认安装到当前盘: $Root"
    }
}

$ScriptDir = Join-Path $Root 'script'
$needFfmpeg = -not ((Test-Path -LiteralPath (Join-Path $FfmpegDir 'ffmpeg.exe')) -and (Test-Path -LiteralPath (Join-Path $FfmpegDir 'ffprobe.exe')))

if ($DryRun) {
    $exists = Test-Path -LiteralPath $ScriptDir
    $ours = $exists -and (Test-Path -LiteralPath (Join-Path $ScriptDir 'core\vpipe.py'))
    Info '------------------ 预演（不修改任何文件） ------------------'
    Info "安装根   : $Root"
    Info "工具目录 : $ScriptDir" + $(if (-not $exists) { '（新建）' } elseif ($ours) { '（已存在·本工具，将就地更新）' } else { '（已存在·非本工具，将需确认/备份）' })
    Info "ffmpeg   : $FfmpegDir" + $(if ($needFfmpeg) { '（需要获取）' } else { '（已就绪）' })
    Info "工作目录 : $ScriptDir\core\_work"
    Info "来源     : " + $(if ($FfmpegZip) { $FfmpegZip } elseif ($Mode -eq 'download') { $ONLINE[0] } elseif ($detected) { "复用 $detected" } else { '无（需改用自动下载入口）' })
    Info '----------------------------------------------------------'
    exit 0
}

# ---------- 2. 先读取既有 config.json（保留用户设置） ----------
$tplCfg = Join-Path $SrcRepo 'core\config.json'
$cfgPath = Join-Path $ScriptDir 'core\config.json'
$tplText = Get-Content -LiteralPath $tplCfg -Raw -Encoding UTF8
$cfgText = ''
if (Test-Path -LiteralPath $cfgPath) {
    try {
        $raw = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8
        $null = $raw | ConvertFrom-Json
        $cfgText = $raw
        Info '已存在 config.json：保留其中的档位/画质等设置，仅更新 ffmpeg 与工作目录'
    } catch {
        $bak = $cfgPath + '.bad-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Move-Item -LiteralPath $cfgPath -Destination $bak -Force
        Warn "原 config.json 无法解析，已备份为: $bak"
    }
}
if (-not $cfgText) {
    $cfgText = $tplText
    Info '使用仓库自带的基础 config.json 作为模板'
}
if ($cfgText -notmatch '"profiles"') {
    Die "基础 config.json 无效（缺少 profiles 段）: $tplCfg"
}

# ---------- 3. 命名冲突处理 ----------
if (Test-Path -LiteralPath $ScriptDir) {
    $isDir = Test-Path -LiteralPath $ScriptDir -PathType Container
    $isOurs = $isDir -and (Test-Path -LiteralPath (Join-Path $ScriptDir 'core\vpipe.py'))
    if (-not $isDir) {
        Warn "目标已存在同名文件（非目录）: $ScriptDir"
        if (-not (Yes '是否将其改名备份后继续？')) { Die "已取消（可用 -Force 自动备份，或改用 -Root 换一个安装根）" }
        $bk = "$ScriptDir.conflict-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Move-Item -LiteralPath $ScriptDir -Destination $bk -Force
        Warn "已备份为: $bk"
    } elseif (-not $isOurs) {
        Warn "目标已存在 script\ 目录，但它不是本工具: $ScriptDir"
        if (-not (Yes '是否把它改名备份后继续安装？')) { Die "已取消（可用 -Force 自动备份，或改用 -Root 换一个安装根）" }
        $bk = Join-Path $Root ('script_backup_' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        Move-Item -LiteralPath $ScriptDir -Destination $bk -Force
        Warn "已备份为: $bk"
    } else {
        Info "检测到已安装的本工具，执行就地更新（不会删除你的其他文件）"
    }
}

# ---------- 4. 获取 ffmpeg（如需要） ----------
if ($needFfmpeg) {
    New-Item -ItemType Directory -Force -Path $FfmpegDir | Out-Null
    if ($FfmpegZip -or $Mode -eq 'download') {
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
                Install-FromZip $zipPath $FfmpegDir $env:TEMP
            } catch { $err = $_.Exception.Message } finally {
                Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
            }
            if ($err) { Die "获取 ffmpeg 失败: $err" }
        } else {
            $ok = $false
            foreach ($u in $ONLINE) {
                $zipPath = Join-Path $env:TEMP ('ffmpeg_easyuse_' + (Get-Date -Format 'yyyyMMddHHmmss') + '.zip')
                try {
                    Info "下载中（essentials 版，约 100 MB）: $u"
                    Download $u $zipPath
                    Install-FromZip $zipPath $FfmpegDir $env:TEMP
                    $ok = $true
                } catch { Warn ("该来源失败: " + $_.Exception.Message) } finally {
                    Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
                }
                if ($ok) { break }
            }
            if (-not $ok) { Die '自动下载 ffmpeg 失败（网络受限？）。可改用“一键安装(已有ffmpeg)”，或手动放置 ffmpeg.exe/ffprobe.exe 后重试。' }
        }
    } elseif ($detected) {
        Info "从已检测到的位置复制 ffmpeg 到 $FfmpegDir"
        foreach ($n in @('ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe')) {
            $s = Join-Path $detected $n
            if (Test-Path -LiteralPath $s) { Copy-Item -LiteralPath $s -Destination (Join-Path $FfmpegDir $n) -Force }
        }
    } else {
        Die "未检测到 ffmpeg。请改用“一键安装(自动下载ffmpeg)”入口，或先用 -Root 指定已有 ffmpeg 的目录。"
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $FfmpegDir 'ffmpeg.exe'))) { Die "缺少 ffmpeg.exe: $FfmpegDir" }
if (-not (Test-Path -LiteralPath (Join-Path $FfmpegDir 'ffprobe.exe'))) { Die "缺少 ffprobe.exe: $FfmpegDir" }
Good "ffmpeg 就绪: $FfmpegDir"

# ---------- 5. 复制程序到 <根>\script ----------
if (Same $SrcRepo $ScriptDir) {
    Info '安装器已在目标位置运行，跳过文件复制'
} else {
    Info "复制程序到 $ScriptDir"
    New-Item -ItemType Directory -Force -Path $ScriptDir | Out-Null
    $skip = @('.git', '.gitignore', '_work', '__pycache__', '_dl_tmp', '_out', '.vs', '.vscode')
    $items = @(Get-ChildItem -LiteralPath $SrcRepo -Force | Where-Object { ($skip -notcontains $_.Name) -and ($_.Name -notlike '*.pyc') })
    foreach ($it in $items) {
        Copy-Item -LiteralPath $it.FullName -Destination $ScriptDir -Recurse -Force
    }
}

foreach ($need in @('core\vpipe.py', 'core\compress_gui.py', 'ffmpeg批量压缩.bat')) {
    if (-not (Test-Path -LiteralPath (Join-Path $ScriptDir $need))) { Die "复制后仍缺少 $need（源目录是否完整？）" }
}

# ---------- 6. 回写 config.json ----------
$workDir = Join-Path $ScriptDir 'core\_work'
$cfgText = Set-JsonStrField $cfgText 'ffmpeg_dir' $FfmpegDir
$cfgText = Set-JsonStrField $cfgText 'work_dir' $workDir
[System.IO.File]::WriteAllText($cfgPath, $cfgText, (New-Object System.Text.UTF8Encoding($false)))

try { $null = (Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json) }
catch { Warn "写入后的 config.json 校验失败，请检查: $cfgPath" }

# ---------- 7. 完成 ----------
Info ''
Good '=========================== 安装完成 ==========================='
Info "工具入口 : $ScriptDir\ffmpeg批量压缩.bat"
Info "ffmpeg   : $FfmpegDir"
Info "配置文件 : $cfgPath"
Info ''
Info '用法：把视频文件或文件夹拖到「ffmpeg批量压缩.bat」图标上即可。'
Info '卸载：双击「一键卸载.bat」——只移除工具目录，ffmpeg 会保留。'
Info '============================================================'
exit 0
