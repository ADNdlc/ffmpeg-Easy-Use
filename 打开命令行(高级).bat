@echo off
chcp 65001 >nul
cd /d "%~dp0core"
echo 已进入程序目录 core，可直接使用: python vpipe.py scan / plan / encode / report / compare / drag
echo 退出码约定: 0=成功  1=存在失败项  2=参数或配置错误  130=用户中断
echo 分辨率与编码档位相互独立: 默认保持源分辨率；可用 --max-long-side N / --max-fps N 覆盖
cmd /k
