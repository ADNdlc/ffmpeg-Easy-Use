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
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install\uninstall.ps1"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   卸载未成功完成（退出码 %RC%）
echo   按任意键关闭本窗口...
pause >nul
exit /b %RC%
