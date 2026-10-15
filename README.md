# ffmpeg-Easy-Use · 本地视频批量压缩

把编码、封装、分辨率各异的视频批量压小，压完逐一对比，再决定删谁、哪些以后不再处理。

**只读取源文件，绝不修改或删除它。**（删除只发生在人工审阅里，显式确认后才送进回收站）

## 快速开始

1. **下载**：`git clone https://github.com/ADNdlc/ffmpeg-Easy-Use.git`，或在页面右上角 **Code → Download ZIP** 后解压。
2. **安装**：进目录双击 `一键安装.bat`。机器上有 ffmpeg 就装到它同级并直接复用；没有就装到当前盘 `X:\Tool\FFmpeg`，下载 ffmpeg 前会先问你一句。
3. **使用**：把视频文件或文件夹拖到 `ffmpeg批量压缩.bat` 图标上，在打开的页面里点「开始压缩」。默认档位（HEVC / NVENC）适合大多数情况，不用改任何参数。
4. **卸载**：双击 `一键卸载.bat`，只移除工具本身，**不动 ffmpeg**（详见「卸载」）。

需要 Windows 10+ 与 Python 3，见文末「依赖」。

---

## 使用与产物

- 拖单个或多个视频：逐个压缩；拖文件夹：递归处理其中所有视频。
- 结果写在源文件同目录，文件名加档位后缀，如 `円香.mp4` → `円香_hevc.mp4`，**永不与源文件同名**。
- 每个被处理的目录是一个独立工作区，产物都留在该目录内：

  ```
  <目标目录>\report_<档位>.html / .csv   ← 体积对比报告
  <目标目录>\审阅.bat                    ← 人工审阅入口
  <目标目录>\.data\                      ← 每个文件的编码日志与状态 json
  ```

- 入口可反复拖入复用（服务已在运行就把新路径追加到列表）；关掉入口窗口即结束服务。

## 输出与安全

- **原子输出**：先写 `源名_<tag>.part.mp4`，编码成功才改名为成品；失败或中断会删掉 `.part`，所以目录里不会留半截文件，重编失败也不会毁掉已有成品。
- **断点续跑**：成品已存在且时长一致就跳过，重编加 `--force`。
- **不重复处理**：已生成的后缀产物、被标记「过滤」的文件都会被自动跳过（`--include-filtered` 可临时忽略）。
- **中断**：Ctrl+C 或关窗会立刻终止 ffmpeg 并清理中间文件；父进程消失时子进程由 Windows Job Object 连带终止，不会留下孤儿进程占满 CPU 与显存。
- **退出码**：`0` 全部成功 / `1` 有失败 / `2` 参数或配置错误 / `130` 用户中断，`.bat` 会透传给调用方。
- 源本身码率已经很低时会提示“压缩收益有限”。

## 压缩档位

`config.json` 的 `default_profile` 默认 `hevc_nvenc`，换档位在页面上选，或改这个字段。

| profile | 编码器 | 特点 |
|------|------|------|
| `hevc_nvenc`（默认） | hevc_nvenc | 速度极快，体积大幅下降 |
| `hevc_nvenc_hq` | hevc_nvenc | CQ24 / p7，更小更好，稍慢 |
| `av1_nvenc` | av1_nvenc | 同画质体积最小，需较新播放器 |
| `hevc_x265_hq` | libx265 | 最慢，体积最小、画质最佳 |
| `h264_x264` | libx264 | 兼容性最好，体积下降中等 |

统一策略：分辨率默认保持源视频原样，音频直接复制，MP4 + `faststart`，偶数尺寸、`yuv420p`，文件名只加后缀。

分辨率是独立参数，可和任一档位组合：`resolution.max_long_side` 是长边上限（`0` = 不缩放）、`max_fps` 是帧率上限（`0` = 保持原帧率）；不改配置也能临时加 `--max-long-side 1920 --max-fps 30`，只缩小、不放大。

嫌慢的话先调档位参数：去掉 `-multipass fullres` 和 `-rc-lookahead`、`preset` 由 `p6` 降到 `p5`，体积几乎不变而耗时约快 3 倍。

## GPU 加速（默认开启）

解码与缩放优先交给显卡，**编码档位本身不变**（NVENC 档位本来就走显卡编码）。三级自动降级：全 GPU（NVDEC + CUDA 缩放）→ 仅 GPU 解码 → 纯软件，哪一级不适用就自动换下一级，不会因为开了 GPU 反而编不出文件。

`hevc_x265_hq` / `h264_x264` 这两个软件档位只加速解码。要压帧率时也不走全 GPU，因为 `fps` 滤镜不认显存帧。关掉：命令行加 `--no-gpu`，或 `config.json` 里设 `"gpu": {"enabled": false}`。

它减轻的是 CPU 压力，不等于提速：瓶颈通常落在编码器上，想缩短总耗时先调上一节的档位参数。

## 安装细节

安装位置按优先级决定：

1. `-Root <目录>`：直接用它作为安装根；
2. `-Target <目录>`：装到 `<目录>\FFmpeg`（该目录名本身就是 `FFmpeg` 时直接采用）；
3. 检测到 `ffmpeg.exe`：装到它所在目录的同级 `script\`，并在 `<安装根>\bin` 建一个目录联接指向它，不复制、不额外占空间；
4. 都没检测到：`<安装器所在盘>:\Tool\FFmpeg`，其中 `bin\` 放 ffmpeg、`script\` 放工具。

装到指定目录有两种做法：

- 把目标目录**拖到 `一键安装.bat` 图标上**；
- 或改 `install\install.ps1` 顶部的 `$DefaultParentDir`（例：填 `'D:\MyTools'` → 装到 `D:\MyTools\FFmpeg\`）。

其他开关：`-Mode download` 不询问直接下载；`-NoDownload` 绝不下载（需自己把 `ffmpeg.exe` / `ffprobe.exe` 放进 `<安装根>\bin`）；`-FfmpegZip <本地zip或URL>` 用指定包代替在线下载；`-DryRun` 只预演、不改动任何文件。

安装器**只新增或覆盖文件**，不删除你的数据，唯一的重命名发生在冲突备份时：

- 目标 `script\` 已存在但不是本工具 → 先确认（或加 `-Force`），改名为 `script_backup_<时间戳>` 再装；
- 已装过本工具 → 就地更新，并**保留你 `config.json` 里的档位与画质设置**；
- `<安装根>\bin` 已有 ffmpeg → 跳过下载；
- 只有 `ffmpeg.exe` 而缺 `ffprobe.exe` → 视为不可用，按“没有 ffmpeg”处理；
- 联接建不起来（少数杀软拦截）→ 退化为把 `ffmpeg_dir` 直接指向你已有的目录。

## 卸载

双击 `一键卸载.bat`（等价 `install\uninstall.ps1`）：

- 移除工具目录 `<安装根>\script`，默认**送进回收站、可还原**；加 `-Hard` 才是永久删除；
- **不删除 ffmpeg**，并明确提示它保留在 `<安装根>\bin`；
- 目标目录里没有 `core\vpipe.py`（不是本工具）时会**拒绝删除**。

## 人工审阅：逐项对比，决定删除与过滤

每次压缩完成后会在工作区根目录生成 `审阅.bat`，可反复双击，接着上次的进度看。

- 打开一个只监听 `127.0.0.1` 的本地网页：左侧源视频、右侧转换结果，中间分界线跟随鼠标拖动，两边同步播放、暂停、拖动进度与逐帧。
- 同一目录的多个转换结果以标签页并列，随时切换。
- 每一项（**源**、**每个结果**）各有两个**互不影响**的开关：
  - **删除**：汇总确认后送入 **Windows 回收站**（可还原），绝不永久删除；
  - **过滤**：标记为「不需要转换」，之后 `drag` / `scan` / `encode` 都自动跳过。
- 默认状态：源不带过滤，结果自带过滤（结果不会作为待处理输入，免得把产物再压一遍）。
- 按钮在左下（源）与右下（当前结果）；快捷键 `D` / `Shift+D` 切删除，`F` / `Shift+F` 切过滤，`Ctrl+Z` 撤销，`,` / `.` 切组，`S` 汇总。
- 点「汇总」列出删除清单与保留清单，确认后统一执行；执行前逐项校验，文件已被改动或消失的项会跳过并说明原因。
- 删除某个源、而该组已无任何保留结果时，要求**显式确认**，避免把一组删成空壳。
- 不想要审阅功能：`config.json` 里设 `"review_enabled": false`。状态在 `<目标目录>\.data\review.json`，原子写入，损坏会自动备份并重建。

## 命令行（可选）

```powershell
cd "<安装根>\script\core"

python compress_gui.py                              # 图形入口，等同于拖入
python compress_gui.py --open "D:\videos\视频.mp4"   # 服务已在运行则追加路径
python compress_gui.py --stop                       # 关闭入口服务

python vpipe.py drag --profile av1_nvenc "D:\videos\视频.mp4" "D:\videos\目录"
python vpipe.py drag --profile hevc_nvenc --max-long-side 1280 --max-fps 30 "D:\videos\目录"
python vpipe.py drag --no-gpu "D:\videos\视频.mp4"   # 纯软件解码/缩放

python vpipe.py scan                                  # 扫描待处理文件
python vpipe.py plan --profile hevc_nvenc             # 规划，逐条列出将采用的加速模式
python vpipe.py encode --profile hevc_nvenc           # 编码，断点续跑；--force 强制重编
python vpipe.py report --profile hevc_nvenc --quality # 出报告，附带 SSIM/PSNR

python review.py --work "D:\videos\目录"              # 审阅服务，review.py --stop 关闭
```

## 目录结构（仓库）

```
ffmpeg-Easy-Use\
├─ 一键安装.bat / 一键卸载.bat
├─ ffmpeg批量压缩.bat        ← 统一入口，拖入即用
├─ 打开命令行(高级).bat
├─ install\                  ← 安装与卸载脚本
├─ README.md
└─ core\                     ← 程序与默认配置
    ├─ compress_gui.py / vpipe.py / review.py
    ├─ gui_page.html / review_page.html
    ├─ config.json           ← 默认配置，安装时写入 ffmpeg 与工作目录
    └─ 默认预设_config.json
```

安装时这份内容会原样复制到 `<安装根>\script\`。程序内部一律按“脚本自身目录”相对定位，所以 `core\` 里的脚本、`config.json`、两个 `.html` 和预设必须待在同一目录。

## 依赖

- **Windows 10 或更高版本**，安装脚本需要 Windows PowerShell 5.1。
- **Python 3**，且 `python` 能直接在 `PATH` 中调用（推荐官方安装包，自带 `tkinter`）。
- **ffmpeg**：检测到就复用，否则自动下载 essentials 版到 `<安装根>\bin`；需要 `ffmpeg.exe` 与 `ffprobe.exe` 同时存在。

## 许可与致谢

本项目只封装调用 FFmpeg，其版权与许可见官方项目。自动下载的 Windows 构建来自 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)（essentials），备用来源为 [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)。
