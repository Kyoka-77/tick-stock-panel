@echo off
rem ============================================================
rem  TickFlow 桌面版一键构建 —— 双击运行入口
rem
rem  作用: 转发到 build-desktop.ps1 (自动备份/还原 data 用户数据)
rem  用法: 直接双击, 或在命令行追加参数, 例如
rem          build-desktop.cmd -SkipFrontend -NoLaunch
rem  失败时窗口不关闭, 方便查看报错。
rem ============================================================
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-desktop.ps1" %*

if errorlevel 1 (
    echo.
    echo ------------------------------------------------------------
    echo  构建失败, 请向上滚动查看报错信息。
    echo ------------------------------------------------------------
    pause
)

endlocal
