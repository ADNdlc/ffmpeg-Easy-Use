@echo off
chcp 65001 >nul
setlocal
echo ============================================================
echo   ffmpeg-Easy-Use · 初始化
echo.
echo   仓库目录就是使用目录，本脚本只检测/构建运行环境：
echo     扫到 ffmpeg -^> 记下它所在目录，直接复用（不下载、不复制）
echo     没扫到      -^> 询问是否下载 essentials 版到「仓库\bin」
echo   随后就地生成 core\config.json，不会复制或移动任何程序文件。
echo.
echo   用法：把本仓库放到合适位置，双击本文件即可；以后重新
echo   拉取代码或移动了仓库位置，再双击一次即可。
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install\install.ps1" -Mode auto
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   初始化未成功完成（退出码 %RC%）
echo   按任意键关闭本窗口...
pause >nul
exit /b %RC%
