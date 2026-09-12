<#
.SYNOPSIS
  TickFlow 桌面版一键构建 —— 自动备份 / 还原用户数据目录。

.DESCRIPTION
  构建链: 前端 pnpm build → PyInstaller onedir → 还原用户数据 → 启动。

  为什么必须有这个脚本
  --------------------
  PyInstaller 的 --noconfirm 会先删除整个 dist/TickFlowStockPanel/ 目录，
  而桌面版的用户数据 backend/dist/TickFlowStockPanel/data/ 正在其中：
  行情 Parquet、DuckDB、自选、回测记录、监控规则、界面偏好(localStorage)。

  直接手工重建 = 一次性清空上述全部数据。

  本脚本的处理: 构建前把 data/ 复制到 dist 之外的持久位置(默认 %LOCALAPPDATA%)，
  构建后原样还原，并对文件数做一致性校验；构建失败时保留备份不做还原。

.PARAMETER BackupRoot
  备份根目录; 默认 %LOCALAPPDATA%\TickFlowStockPanel\data-backups

.PARAMETER KeepBackups
  保留最近 N 份历史备份 (默认 3), 仅在本次构建成功后清理旧备份。

.PARAMETER SkipFrontend
  跳过前端构建, 复用现有 frontend/dist (只改了后端时的快速通道)。

.PARAMETER SkipRestore
  只备份不还原 (排查用)。

.PARAMETER NoLaunch
  构建完成后不自动启动应用。

.PARAMETER Force
  发现应用在运行时直接结束, 不再询问。

.EXAMPLE
  .\build-desktop.ps1
  .\build-desktop.ps1 -SkipFrontend -NoLaunch
  .\build-desktop.ps1 -Force -KeepBackups 5
#>
[CmdletBinding()]
param(
    [string]$BackupRoot = (Join-Path $env:LOCALAPPDATA 'TickFlowStockPanel\data-backups'),
    [int]$KeepBackups = 3,
    [switch]$SkipFrontend,
    [switch]$SkipRestore,
    [switch]$NoLaunch,
    [switch]$SkipBackupTool,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# 子进程(pnpm / PyInstaller)日志统一 UTF-8, 避免中文乱码
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $OutputEncoding           = New-Object System.Text.UTF8Encoding $false
} catch {}

function Log-Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Log-Info($m) { Write-Host "    $m" -ForegroundColor DarkGray }
function Log-Ok($m)   { Write-Host "    [OK] $m" -ForegroundColor Green }
function Log-Warn($m) { Write-Host "    [!] $m"  -ForegroundColor Yellow }
function Log-Err($m)  { Write-Host "    [X] $m"  -ForegroundColor Red }
function Fail($m)     { Log-Err $m; Write-Host ''; exit 1 }

function Confirm-Action($question) {
    # 非交互环境(如 CI)下 Read-Host 会抛异常, 此时按「是」处理
    try { $ans = Read-Host "$question [Y/n]" } catch { return $true }
    return ($ans -eq '' -or $ans -match '^[Yy]')
}

function Get-DirStat($path) {
    if (-not (Test-Path $path)) { return [pscustomobject]@{ Files = 0; Bytes = 0L } }
    $m = Get-ChildItem $path -Recurse -File -Force -ErrorAction SilentlyContinue |
         Measure-Object -Property Length -Sum
    return [pscustomobject]@{ Files = [int]$m.Count; Bytes = [long]($m.Sum) }
}

function Invoke-CopyTree($src, $dst, $label) {
    # robocopy: exit code < 8 视为成功 (1 = 有文件被复制, 属正常)
    if (-not (Test-Path $src)) { Log-Warn "$label : 源不存在, 跳过 ($src)"; return $true }
    New-Item -ItemType Directory -Path $dst -Force | Out-Null
    $null = robocopy $src $dst /E /COPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS
    if ($LASTEXITCODE -ge 8) { Log-Err "$label 失败 (robocopy exit=$LASTEXITCODE)"; return $false }
    return $true
}

# ── 路径 ─────────────────────────────────────────────────────────────
$Root        = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir  = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$Spec        = Join-Path $Root 'packaging\tickflow.spec'
$DistDir     = Join-Path $BackendDir 'dist\TickFlowStockPanel'
$DataDir     = Join-Path $DistDir 'data'
$ExePath     = Join-Path $DistDir 'TickFlowStockPanel.exe'
$VenvPy      = Join-Path $BackendDir '.venv\Scripts\python.exe'
$AppProc     = 'TickFlowStockPanel'

Write-Host ''
Write-Host '==================================================' -ForegroundColor Blue
Write-Host '  TickFlow 桌面版构建 (含 data 自动备份/还原)' -ForegroundColor Blue
Write-Host '==================================================' -ForegroundColor Blue
Log-Info "项目根      : $Root"
Log-Info "产物目录    : $DistDir"
Log-Info "数据目录    : $DataDir"
Log-Info "备份根目录  : $BackupRoot"

# ── 0. 环境检查 ──────────────────────────────────────────────────────
Log-Step '0/8  环境检查'
if (-not (Test-Path $Spec))      { Fail "找不到打包配置: $Spec" }
if (-not (Test-Path $VenvPy))    { Fail "找不到后端虚拟环境: $VenvPy`n      请先执行: cd backend; uv sync --extra desktop" }
Log-Ok 'tickflow.spec / 后端 .venv 就位'

if (-not $SkipFrontend) {
    if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) { Fail '找不到 pnpm, 请先安装: npm i -g pnpm' }
    Log-Ok 'pnpm 就位'
} elseif (-not (Test-Path (Join-Path $FrontendDir 'dist'))) {
    # spec 会把 frontend/dist 打进产物, 缺了会导致 PyInstaller 报错
    Fail "-SkipFrontend 指定跳过前端构建, 但 frontend/dist 不存在"
}

$pyiOk = $false
try { & $VenvPy -c "import PyInstaller" 2>$null; $pyiOk = ($LASTEXITCODE -eq 0) } catch { $pyiOk = $false }
if (-not $pyiOk) {
    Log-Warn 'PyInstaller 未安装, 正在装入 .venv ...'
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail '找不到 uv, 无法安装 PyInstaller' }
    Push-Location $BackendDir
    try { uv pip install pyinstaller } finally { Pop-Location }
    try { & $VenvPy -c "import PyInstaller" 2>$null; $pyiOk = ($LASTEXITCODE -eq 0) } catch { $pyiOk = $false }
    if (-not $pyiOk) { Fail 'PyInstaller 安装失败' }
}
Log-Ok 'PyInstaller 就位'

# ── 1. 关闭运行中的实例 (否则 exe 被占用无法覆盖) ─────────────────────
Log-Step '1/8  关闭运行中的实例'
$running = @(Get-Process -Name $AppProc -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Log-Warn "检测到 $($running.Count) 个实例在运行 (PID: $($running.Id -join ', '))"
    if ($Force -or (Confirm-Action '重建需要关闭它, 现在关闭?')) {
        $running | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        Log-Ok '已关闭'
    } else {
        Fail '已取消 (未做任何改动)'
    }
} else {
    Log-Ok '没有运行中的实例'
}

# ── 2. 备份用户数据 ──────────────────────────────────────────────────
Log-Step '2/8  备份用户数据'
$stamp     = Get-Date -Format 'yyyyMMdd-HHmmss'
$BackupDir = Join-Path $BackupRoot $stamp
$backupOk  = $false
$srcStat   = $null

if (Test-Path $DataDir) {
    $srcStat = Get-DirStat $DataDir
    Log-Info "源: $($srcStat.Files) 个文件, $([math]::Round($srcStat.Bytes / 1MB, 1)) MB"
    Log-Info "-> $BackupDir"

    if (-not (Invoke-CopyTree $DataDir $BackupDir '备份')) {
        Fail "备份失败。未执行任何破坏性操作, 现有数据完好。"
    }

    $bakStat = Get-DirStat $BackupDir
    if ($bakStat.Files -lt $srcStat.Files) {
        Fail "备份校验不通过 (源 $($srcStat.Files) 文件 / 备份 $($bakStat.Files) 文件), 已中止构建。"
    }
    $backupOk = $true
    Log-Ok "备份完成并校验通过 ($($bakStat.Files) 个文件)"
} else {
    Log-Warn "未发现用户数据目录, 跳过备份 (首次构建属正常)"
}

# ── 3. 构建前端 ──────────────────────────────────────────────────────
Log-Step '3/8  构建前端'
if ($SkipFrontend) {
    Log-Warn '已按 -SkipFrontend 跳过, 复用现有 frontend/dist'
} else {
    Push-Location $FrontendDir
    try {
        pnpm build
        if ($LASTEXITCODE -ne 0) { Fail "前端构建失败 (exit=$LASTEXITCODE)" }
    } finally { Pop-Location }
    Log-Ok '前端已输出到 frontend/dist'
}

# ── 4. 还原 tsc 的副产物改动 ─────────────────────────────────────────
Log-Step '4/8  清理构建副产物'
$dts = Join-Path $FrontendDir 'vite.config.d.ts'
if ((Test-Path $dts) -and (Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $Root '.git'))) {
    Push-Location $Root
    try {
        $dirty = git status --porcelain -- frontend/vite.config.d.ts 2>$null
        if ($dirty) {
            git checkout -- frontend/vite.config.d.ts 2>$null
            Log-Ok '已还原 frontend/vite.config.d.ts (tsc -b 的行尾副产物)'
        } else {
            Log-Ok 'frontend/vite.config.d.ts 无改动'
        }
    } catch {
        Log-Warn "还原失败(可忽略): $($_.Exception.Message)"
    } finally { Pop-Location }
} else {
    Log-Ok '无需处理'
}

# ── 5. PyInstaller 打包 ──────────────────────────────────────────────
Log-Step '5/8  PyInstaller 打包 (onedir)'
Log-Warn "注意: --noconfirm 会先删除 $DistDir"
$buildOk = $false
Push-Location $BackendDir
try {
    & $VenvPy -m PyInstaller --noconfirm $Spec
    $buildOk = ($LASTEXITCODE -eq 0)
} catch {
    Log-Err "打包异常: $($_.Exception.Message)"
    $buildOk = $false
} finally { Pop-Location }

if (-not $buildOk) {
    Write-Host ''
    Log-Err '打包失败。'
    if ($backupOk) { Log-Warn "备份仍完整保留在: $BackupDir" }
    Log-Info '处理建议: 修掉上面的报错后重跑本脚本; 数据未受影响。'
    exit 1
}
Log-Ok '打包完成'

# ── 6. 还原用户数据 ──────────────────────────────────────────────────
Log-Step '6/8  还原用户数据'
if (-not (Test-Path $DistDir)) {
    Log-Err "产物目录不存在: $DistDir"
    if ($backupOk) { Log-Warn "备份保留在: $BackupDir" }
} elseif ($SkipRestore) {
    Log-Warn '已按 -SkipRestore 跳过还原'
    if ($backupOk) { Log-Info "备份位置: $BackupDir" }
} elseif (-not $backupOk) {
    Log-Warn '本次没有备份可还原 (首次构建属正常)'
} else {
    if (-not (Invoke-CopyTree $BackupDir $DataDir '还原')) {
        Log-Err '还原失败, 备份完好保留, 可手工复制:'
        Log-Info "  robocopy `"$BackupDir`" `"$DataDir`" /E"
    } else {
        $resStat = Get-DirStat $DataDir
        if ($resStat.Files -ge $srcStat.Files) {
            Log-Ok "还原完成并校验通过 ($($resStat.Files) 个文件, $([math]::Round($resStat.Bytes / 1MB, 1)) MB)"
        } else {
            Log-Warn "还原后文件数偏少 ($($resStat.Files) < $($srcStat.Files))"
            Log-Warn "备份仍保留在: $BackupDir"
        }
    }
}

# ── 7. 构建窗口版备份/还原工具, 放入应用目录 ─────────────────────────
Log-Step '7/8 构建备份还原工具 (窗口版)'
$ToolExeName = 'TickFlowDataBackup.exe'
if ($SkipBackupTool) {
    Log-Warn '已按 -SkipBackupTool 跳过'
} else {
    $ToolSpec = Join-Path $Root 'packaging\backup_tool.spec'
    $ToolBuilt = Join-Path $BackendDir "dist\$ToolExeName"
    $ToolFinal = Join-Path $DistDir $ToolExeName

    if (-not (Test-Path $ToolSpec)) {
        Log-Warn "找不到打包配置: $ToolSpec, 跳过"
    } else {
        Push-Location $BackendDir
        try {
            & $VenvPy -m PyInstaller --noconfirm $ToolSpec
            $toolOk = ($LASTEXITCODE -eq 0)
        } finally { Pop-Location }

        if (-not $toolOk) {
            Log-Warn '备份还原工具构建失败 (主程序不受影响, 可稍后单独构建)'
        } elseif (-not (Test-Path $ToolBuilt)) {
            Log-Warn "未找到工具产物: $ToolBuilt"
        } else {
            if (-not (Test-Path $DistDir)) { New-Item -ItemType Directory -Path $DistDir -Force | Out-Null }
            Copy-Item $ToolBuilt $ToolFinal -Force
            $toolMb = [math]::Round((Get-Item $ToolFinal).Length / 1MB, 1)
            Log-Ok "已放入应用目录: $ToolExeName ($toolMb MB) —— 与主程序同目录"
        }
    }
}

# ── 8. 清理旧备份 + 启动 ─────────────────────────────────────────────
Log-Step '8/8  收尾'
if ($buildOk -and (Test-Path $BackupRoot)) {
    $all = @(Get-ChildItem $BackupRoot -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
    if ($all.Count -gt $KeepBackups) {
        $all | Select-Object -Skip $KeepBackups | ForEach-Object {
            Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            Log-Info "清理旧备份: $($_.Name)"
        }
    }
    Log-Ok "保留最近 $([math]::Min($all.Count, $KeepBackups)) 份备份"
}

if ((Test-Path $ExePath) -and -not $NoLaunch) {
    Start-Process -FilePath $ExePath
    Log-Ok "已启动: $ExePath"
} elseif (Test-Path $ExePath) {
    Log-Ok "产物就绪: $ExePath"
} else {
    Log-Err "未找到主程序: $ExePath"
    exit 1
}

$exeStat = Get-DirStat $DistDir
Write-Host ''
Write-Host '==================================================' -ForegroundColor Green
Write-Host '  构建成功' -ForegroundColor Green
Write-Host '==================================================' -ForegroundColor Green
Log-Info "主程序   : $ExePath"
Log-Info "产物大小 : $([math]::Round($exeStat.Bytes / 1MB, 1)) MB ($($exeStat.Files) 个文件)"
if ($backupOk) { Log-Info "本次备份 : $BackupDir" }
Write-Host ''
exit 0
