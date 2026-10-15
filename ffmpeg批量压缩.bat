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

REM 成功时直接退出：若已有实例在运行，本次只是把路径追加进待处理列表，
REM 不需要页面也不需要驻留窗口，闪一下即可。失败/中断才停下来让用户看清提示。
if "%RC%"=="0" exit /b 0

echo.
if "%RC%"=="130" (
  echo   已中断，服务已关闭。
) else (
  echo   注意: 入口启动失败（退出码 %RC%）  ——  常见原因: 未安装 Python 或未加入 PATH
)
echo   按任意键关闭本窗口...
pause >nul
exit /b %RC%
