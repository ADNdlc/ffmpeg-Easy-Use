@echo off
chcp 65001 >nul
setlocal
echo ============================================================
echo   ffmpeg-Easy-Use · 一键卸载
echo.
echo   只移除工具目录（默认送进回收站，可还原）；
echo   ffmpeg 本体不会被删除。
echo ============================================================
echo.
rem 关键：把工作目录移出待删除的目录。双击运行时 cmd 的工作目录就在 script\ 里，
rem 导致“目录正在使用中”而无法删除；先切到系统目录即可解决。
cd /d "%SystemRoot%" >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install\uninstall.ps1"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   卸载未成功完成（退出码 %RC%）
echo   按任意键关闭本窗口...
pause >nul
exit /b %RC%
