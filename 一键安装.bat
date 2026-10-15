@echo off
chcp 65001 >nul
setlocal
echo ============================================================
echo   ffmpeg-Easy-Use · 一键安装
echo.
echo   默认：先扫描机器上的 ffmpeg。
echo     扫到  -^> 装到它同级的 script\，直接复用这个 ffmpeg（不复制、不建 bin）；
echo     没扫到 -^> 装到当前盘 X:\Tool\FFmpeg，并询问是否下载 essentials 版。
echo.
echo   想装到指定目录：
echo     把那个目录拖到本文件图标上 -^> 装到「目录\FFmpeg\」；
echo     或改 install\install.ps1 顶部的 $DefaultParentDir（脚本更改方法）。
echo ============================================================
echo.
set "TGT="
if not "%~1"=="" (set TGT=-Target "%~1")
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install\install.ps1" -Mode auto %TGT%
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   安装未成功完成（退出码 %RC%）
echo   按任意键关闭本窗口...
pause >nul
exit /b %RC%
