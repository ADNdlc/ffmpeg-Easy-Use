@echo off
chcp 65001 >nul
setlocal
echo ============================================================
echo   ffmpeg-Easy-Use · 一键安装（没有 ffmpeg，自动下载）
echo.
echo   未检测到 ffmpeg 时，自动下载官方 essentials 版到
echo   ^<安装根^>\bin，再把工具装到 ^<安装根^>\script。
echo   下载约 100 MB，请保持网络通畅。
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install\install.ps1" -Mode download
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   安装未成功完成（退出码 %RC%）
echo   按任意键关闭本窗口...
pause >nul
exit /b %RC%
