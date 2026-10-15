@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   ffmpeg 批量压缩 · 统一入口
echo   把视频文件或文件夹拖到本 .bat 图标上，即可打开简易设置页面
echo   分辨率默认保持源视频原分辨率；源文件永远不会被改动
echo ============================================================
echo.
python "%~dp0core\compress_gui.py" --open %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   注意: 入口启动失败（退出码 %RC%）
echo   按任意键关闭本窗口（关闭本窗口会同时关闭入口服务）。
pause >nul
exit /b %RC%
