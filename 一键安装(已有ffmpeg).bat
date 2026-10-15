@echo off
chcp 65001 >nul
setlocal
echo ============================================================
echo   ffmpeg-Easy-Use · 一键安装（复用已安装的 ffmpeg）
echo.
echo   检测到 ffmpeg 时，把工具装到它同级的 script\ 目录；
echo   未检测到会提示改用「一键安装(自动下载ffmpeg)」入口。
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install\install.ps1" -Mode auto
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   安装未成功完成（退出码 %RC%）
echo   按任意键关闭本窗口...
pause >nul
exit /b %RC%
