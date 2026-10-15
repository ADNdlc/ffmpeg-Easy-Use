# ffmpeg-Easy-Use · 本地视频批量压缩管线（vpipe）

针对编码/封装/分辨率/帧率各异的视频做批量压缩，用于本机存储与播放；压缩后与源视频逐一对比，再由你决定删除谁、以及哪些文件以后不再处理。

**核心保证：只读取源文件，绝不修改/删除源文件。**（删除只在「人工审阅」里显式确认后进行，且是送进回收站）

> 不需要懂 ffmpeg 参数，也不需要敲命令行：把视频或文件夹拖进一个 `.bat` 入口即可。

---

## 一键安装

先下载或克隆本仓库，然后在仓库目录里**双击 `一键安装.bat`**（无需任何参数）。安装器会自动判断两种情况：

| 情况 | 装成什么样 |
|------|------------|
| 机器上**有** ffmpeg（`PATH` 或 `where.exe` 能找到） | 工具装到 ffmpeg 所在目录的**同级** `script\`；**不复制 ffmpeg、不新建 `bin\`**，而是在 `<安装根>\bin` 建一个**目录联接（junction）**指向你已有的 ffmpeg 目录 |
| 机器上**没有** ffmpeg | 默认装到**当前盘** `<盘>:\Tool\FFmpeg`，在根下创建 `bin\`（自动下载 essentials 版）和 `script\`（工具本体）；**下载前会先问你一次** |

> - 不想被问、直接下载：加 `-Mode download`
> - 即使没有 ffmpeg 也**绝不下载**：加 `-NoDownload`（此时需自己把 `ffmpeg.exe` / `ffprobe.exe` 放进 `<安装根>\bin`）

### 安装到哪（根目录解析规则）

1. 显式给了 `-Root <目录>` → 直接用它作为**安装根**（高级用法）；
2. 显式给了 `-Target <目录>` → 装到 `<目录>\FFmpeg`（若该目录名本身就是 `FFmpeg`，就直接当安装根）；
3. **检测到 `ffmpeg.exe`**（`PATH` 或 `where.exe` 能找到）→ 取它所在目录：
   - 若该层目录名是 `bin`，则安装根取它的**上一级** → 形成 `{bin, script}` 结构；
   - 否则直接用它所在目录作为安装根（不会去创建奇怪的盘根目录）；
4. **都没检测到** → 默认装到**安装器所在盘**的 `<盘>:\Tool\FFmpeg`。

### 装到指定目录（两种做法）

**做法一（不改脚本）**：把目标目录**拖到 `一键安装.bat` 图标上**，即装到「该目录\FFmpeg\」：
- 那里没有可用 ffmpeg → 创建 `FFmpeg\bin\`（下载）与 `FFmpeg\script\`（工具）；
- 那里已有可用 ffmpeg → 只创建 `FFmpeg\script\`，`FFmpeg\bin` 用**联接**指向已有的 ffmpeg。

等价的命令行方式：

```powershell
cd "<仓库目录>\install"
powershell -ExecutionPolicy Bypass -File install.ps1 -Target D:\MyTools
```

**做法二（脚本更改方法）**：编辑 `install\install.ps1` 顶部这一行，之后双击安装就固定装到那里：

```powershell
$DefaultParentDir = ''      # 例：改成 'D:\MyTools'  ->  装到 D:\MyTools\FFmpeg\
```

安装后的结构（以 `D:\Tool\FFmpeg` 为例）：

```
D:\Tool\FFmpeg\
├─ bin\                 ← ffmpeg.exe / ffprobe.exe
│   └─ ...                  （没 ffmpeg 时下载到这里；已有 ffmpeg 时这里是指向它的“联接”，不额外占空间）
└─ script\              ← 本工具
    ├─ ffmpeg批量压缩.bat      ← 日常就拖这一个
    ├─ 打开命令行(高级).bat
    ├─ 一键卸载.bat
    ├─ install\                ← 安装/卸载脚本本体
    ├─ README.md
    └─ core\                   ← 程序与配置（compress_gui.py / vpipe.py / review.py / config.json 等）
```

### 会遇到的意外情况（安装器已处理）

- **`script\` 已存在但不是本工具**：默认交互确认；同意（或加 `-Force`）后会**改名备份**为 `script_backup_<时间戳>` 再安装，不会覆盖你的文件。
- **目标位置有一个同名“文件”**：同样先改名备份。
- **已有本工具**：执行**就地更新**，并**保留你 `config.json` 里的档位/画质等设置**（只更新 ffmpeg 与工作目录）。
- **`<安装根>\bin` 已有 ffmpeg**：跳过下载，直接复用。
- **检测到的 ffmpeg 只有 `ffmpeg.exe` 没有 `ffprobe.exe`**：视为不可用，会按“没有 ffmpeg”处理（询问后下载）。
- **建立 `bin` 联接失败**（少数环境/杀软拦截）：改为把配置里的 `ffmpeg_dir` 直接指向你已有目录，不复制文件。
- **下载失败/网络受限**：可用 `-FfmpegZip <本地zip或URL>`，或手动把 `ffmpeg.exe` / `ffprobe.exe` 放进 `<安装根>\bin` 后重试。
- 安装器**只新增/覆盖文件**，不删除任何用户数据；唯一的重命名只发生在冲突备份时。

### 想先看看会发生什么（预演）

```powershell
cd "<仓库目录>\install"
powershell -ExecutionPolicy Bypass -File install.ps1 -DryRun
```

`-DryRun` 只打印将要使用的安装根/工具目录/ffmpeg 来源，**不改动任何文件**。

### 一键卸载

双击 `一键卸载.bat`（或运行 `install\uninstall.ps1`）：

- 移除工具目录 `<安装根>\script`（**默认送进回收站，可还原**；加 `-Hard` 才是永久删除）；
- **不会删除 ffmpeg**，并会明确提示它保留在 `<安装根>\bin`；
- 有安全闸：若目标目录里没有 `core\vpipe.py`（不是本工具），会**拒绝删除**；
- 双击方式运行时，工具目录会在**窗口关闭后由后台进程移除**（因为 cmd 正在读取该窗口里的 `.bat`，当场删除会让它报“找不到路径”并以非零码退出）。看到“卸载完成”即表示已提交，稍等几秒目录就会消失。

---

## 最简用法：拖进统一入口

把**视频文件**或**整个文件夹**直接拖到 `ffmpeg批量压缩.bat` 图标上运行，会打开一个**轻量本地设置页面**（秒开，只监听 `127.0.0.1`）：

- 页面上选择**编码档位**（HEVC / AV1 / x265 / H264）与常用参数（画质、速度预设、分辨率/帧率上限、音频等）。
- 复杂/专业参数默认**折叠隐藏**，需要时再展开。
- 内置多套**默认预设**；也可点「选择预设文件」用**系统文件选择对话框**加载你自己的 `XXX_config.json` 预设/配置。
- 选好后点「开始压缩」，页面实时显示进度与日志；结束后可直接打开**体积对比报告**与**人工审阅页**。

> 分辨率默认保持源视频原分辨率（不缩放）；**源文件永远不会被改动**。

- 拖**单个/多个视频**：逐个压缩。
- 拖**文件夹**：递归处理目录下所有视频。
- 结果直接生成在**源文件同目录**，文件名加 `_<档位后缀>` 区分，例如 `円香.mp4` → `円香_hevc.mp4`。
- 一个入口服务可被**反复拖入复用**（已在运行就直接把新拖入的路径追加到列表）；关闭入口窗口即结束服务。

### 工作区（目标目录）与产物位置

被处理的目录会作为一个**独立工作区**自我管理，产物都放在该目录里，不再集中堆在脚本目录：

```
<目标目录>\report_<档位>.csv / .html   ← 体积对比报告
<目标目录>\审阅.bat                    ← 人工审阅入口（只读当前工作区，可反复双击）
<目标目录>\.data\log_*.txt             ← 每个文件的编码日志
<目标目录>\.data\*.json                ← 库存 / 编码状态 / 审阅状态
```

- `审阅.bat` 里已把源目录与数据目录都指向**本工作区**，双击只审阅这一个目录的内容；不同目录各起各的服务、互不干扰。
- 拖拽/`drag`/`encode` 均**不会生成 `_compare_xxx` 对比视频**：并排对比改由审阅页面（可拖动分界线）承担。

## 命令行用法（可选，更灵活）

```powershell
cd "<安装根>\script\core"

# 统一图形入口（等价于拖入 ffmpeg批量压缩.bat）
python compress_gui.py                             # 启动页面并自动打开浏览器
python compress_gui.py --open "D:\videos\视频.mp4" # 启动或复用实例，并把该路径加入列表
python compress_gui.py --config "D:\videos\my_config.json"   # 换一个基底配置（默认 config.json）
python compress_gui.py --stop                      # 关闭正在运行的入口服务

# 拖拽入口的等价命令（可指定编码档位）
python vpipe.py drag --profile av1_nvenc "D:\videos\视频.mp4" "D:\videos\目录"
python vpipe.py drag --quality "D:\videos\目录"   # 附带 SSIM/PSNR 画质度量

# 分辨率与编码档位可自由组合（不改配置也能临时指定）
python vpipe.py drag --profile hevc_nvenc --max-long-side 1920 "D:\videos\4K视频.mp4"
python vpipe.py drag --profile hevc_nvenc --max-long-side 1280 --max-fps 30 "D:\videos\目录"

# 强制不使用 GPU（纯软件解码/缩放）
python vpipe.py drag --no-gpu "D:\videos\视频.mp4"

# 目录批处理流水线（报告写在工作区根目录，日志/json 在其 .data，输出目录可控）
python vpipe.py scan
python vpipe.py plan --profile hevc_nvenc
python vpipe.py encode --profile hevc_nvenc            # 断点续跑，--force 强制重编
python vpipe.py report --profile hevc_nvenc --quality

# 人工审阅（逐项对比 + 删除/过滤开关）
python review.py                      # 启动审阅服务并打开页面（已运行则直接复用）
python review.py --work "D:\videos\目录"  # 指定工作区（.data 下放状态，根目录放审阅.bat）
python review.py --stop               # 关闭正在运行的审阅服务
```

## 分辨率与编码档位是分开的

- **编码档位**（`config.json` 的 `profiles`）只描述编码器与质量参数（编码器、`cq`/`crf`、preset、音频、封装等），**不含分辨率**。
- **分辨率**由独立的 `config.json → resolution` 控制，两者可任意组合：

  ```json
  "resolution": {
    "max_long_side": 0,
    "max_fps": 0
  }
  ```

  - `max_long_side`：长边上限像素。**`0`（默认）= 不缩放，保持源视频原分辨率**；设为 `1920` 时仅当源长边**超过** 1920 才等比缩小，小分辨率源不会被放大。
  - `max_fps`：帧率上限。`0`（默认）= 保持源帧率。
- 命令行可覆盖（不改配置文件）：`--max-long-side N`、`--max-fps N`（`0` 表示保持原始），对 `plan` / `encode` / `drag` 均有效。
- 无论是否缩放，都会保证输出尺寸为偶数（编码器要求），`yuv420p`。

## GPU 加速（默认开启）

解码与缩放优先交给显卡，**编码档位本身不变**（NVENC 档位本来就走显卡编码）：

| 环节 | 关闭 GPU 时（纯软件） | 默认（GPU 优先） |
|------|------|------|
| 解码 | CPU 软解（4K60 会吃满多核） | NVDEC 硬解 |
| 缩放（需要缩小时） | CPU swscale | GPU `scale_cuda` |
| 编码 | 档位对应的编码器 | 不变 |

- **三级自动降级**：`全 GPU（NVDEC + CUDA 缩放）` → `仅 GPU 解码` → `纯软件`。任一步失败都会自动换下一级重试（例如源是 H.264 4:4:4 这类 NVDEC 不支持的规格、或滤镜不适用），**不会因为开启 GPU 而编不出文件**。
- 若失败原因属于「显卡/CUDA 整体不可用」（驱动缺失、`nvcuda` 加载失败等），本次运行后续文件直接走纯软件，不再逐个白试。
- 软件编码档位（`hevc_x265_hq` / `h264_x264`）只加速解码，编码仍在 CPU。
- 需要压低帧率（`max_fps`）时**不会**走「全 GPU」——`fps` 滤镜不支持显存帧，会自动降为「仅 GPU 解码」。
- 关闭方式：命令行加 `--no-gpu`（对 `plan`/`encode`/`drag` 生效），或在 `config.json` 里设 `"gpu": {"enabled": false}`。
- `plan` 会逐条列出每条将采用的加速模式，`encode --dry-run` 会打印实际 ffmpeg 命令。

作者机器（RTX 4070 + i5-12490F，4K60 20 秒素材）实测，供参考：

| 场景 | 纯软件 | GPU 优先 |
|------|------|------|
| 4K→1080p 缩放 | 5.1 秒 / CPU 37 秒 | 2.5 秒 / CPU 1.4 秒 |
| 只解码 4K HEVC | 4.0 秒 / CPU 29 秒 | 3.5 秒 / CPU 4 秒 |

> GPU 加速解除的是 **CPU 压力**，不是万能提速。若编码档位带了 `-multipass fullres`、`-rc-lookahead 32` 这类重参数，瓶颈会落在 NVENC 编码上，这时开不开 GPU 总耗时几乎一样（实测 4K60 20 秒两者都约 32 秒，但 CPU 从 31 秒降到 1 秒）。想缩短总耗时，先调档位参数——同一素材同为 CQ28：
>
> | 档位参数 | 耗时 | 体积 | PSNR |
> |------|------|------|------|
> | `-preset p6 -multipass fullres -rc-lookahead 32`（仓库默认） | 32 秒 | 76.9 MB | 41.79 |
> | `-preset p5`（单遍，无 multipass/lookahead） | 10 秒 | 77.1 MB | 41.83 |
>
> 即：去掉 `-multipass fullres` 与 `-rc-lookahead`、`preset` 从 `p6` 降到 `p5`，体积几乎不变而耗时约快 3 倍。

## 压缩档位与策略

`config.json` 中 `default_profile` 默认 `hevc_nvenc`。统一策略：

- 分辨率见上一节：默认保持源分辨率；需要缩小时通过 `resolution` 或 `--max-long-side` 指定
- 音频默认直接复制（源为 AAC 时不二次损失）
- MP4 + `faststart`；偶数尺寸；`yuv420p`
- 文件名保持不变，仅加后缀区分

想改默认档位：编辑 `config.json` 的 `default_profile`，或调某个 profile 的 `cq`/`crf`（数值越小画质越好体积越大）、`preset`、`audio_mode`（改 `aac` 更小）。

内置档位一览：

| profile | 编码器 | 特点 |
|------|------|------|
| `hevc_nvenc`（默认） | hevc_nvenc | 速度极快，体积大幅下降 |
| `hevc_nvenc_hq` | hevc_nvenc | CQ24 / p7，更小更好，稍慢 |
| `av1_nvenc` | av1_nvenc | 同画质体积最小，需较新播放器 |
| `hevc_x265_hq` | libx265 | 最慢，体积最小、画质最佳 |
| `h264_x264` | libx264 | 兼容性最好，体积下降中等 |

## 输出与安全

- **输出模式** `output_mode`：默认 `samedir`（写入源目录、命名加后缀）。设为其它值时改用 `output_dir\<profile>` 子目录，此时若输出目录与源目录冲突会直接拒绝执行。
- 输出名恒为 `源名_<tag>.mp4`，**永不与源文件同名**，`drag` 还会再次校验，同名即拒绝，绝不覆盖源文件。
- **原子输出**：ffmpeg 先写 `源名_<tag>.part.mp4` 中间文件，编码成功后原子改名为最终文件；失败或被中断时删除该中间文件。因此任何时刻目录里都不会出现“半截的成品”，重编失败也不会毁掉已有的好文件。`.part` 文件会被自动忽略，不会被当成待处理视频。
- 断点续跑：输出已存在且时长一致时自动跳过（`--force` 强制重编）；再次拖入同一文件夹时，已生成的后缀产物会被自动忽略，不会重复处理。
- 审阅过滤：被标记为「过滤」的文件（含被删源后留下的产物）同样会被自动跳过，见下方「人工审阅」；`--include-filtered` 可临时忽略。
- 低收益提示：源本身已是低码率时会提示“压缩收益有限”。

## 中断与异常处理

- **Ctrl+C / 关闭窗口**：立即终止正在运行的 ffmpeg，删除 `.part` 中间文件，写入状态后退出（退出码 130），不会留下半成品。
- **父进程意外消失**：已用 Windows Job Object 绑定子进程，父进程一旦退出/被杀，ffmpeg 会被内核连带终止，杜绝孤儿进程占满 CPU/显存。
- **各类超时**：探测 30s、画质度量 300s、对比生成 600s、编码看门狗 `max(600, 时长×12+300)` 秒，超时即终止，不会无限卡死。
- **配置/输入异常**：config 损坏、缺少 `profiles`/`work_dir`、profile 名称错误、源目录或工作目录不存在、inventory 损坏等，均给出中文提示而非 Python 堆栈。
- **退出码**：`0`=全部成功；`1`=存在失败/无法解析的文件；`2`=参数或配置错误；`130`=用户中断。`.bat` 会把这个退出码透传给调用方。
- 排查问题时加 `--debug` 可保留完整异常堆栈。

## 人工审阅：逐项对比，决定删除与过滤

每次压缩完成后会自动在**该工作区（目标目录）根目录**生成审阅入口（也可随时手动打开）：

```
<目标目录>\审阅.bat    ← 可反复双击；只读本工作区（工作到一半关掉了，再双击接着看）
python review.py --work "<目标目录>"   ← 等价命令行入口；python review.py --stop 关闭服务
```

- 打开一个**只监听 `127.0.0.1`** 的本地网页：左侧源视频、右侧转换结果，中间一条分界线跟随鼠标拖动，两边同步播放（播放/暂停/拖动进度/逐帧都可用）。
- 同一目录下多次转换产生的多个结果，会作为「结果」标签页并列，随时切换对比。
- 每一项（**源**、**每一个结果**）都有两个**彼此独立**的开关：
  - **删除**：汇总确认后把该项送入 **Windows 回收站**（可还原），**绝不永久删除**。
  - **过滤**：给该项打上「不需要转换」标记，之后**不再进入处理管线**（下次拖入 / `scan` / `encode` 会自动跳过）。
- 默认状态：**源**不带过滤；**结果**自带过滤（结果永远不作为待处理输入，避免把自己的产物又压一遍）。
- 按钮位置：左下是**源**的删除/过滤，右下是**当前结果**的删除/过滤（小尺寸按钮）。快捷键：`D` / `Shift+D` 切删除，`F` / `Shift+F` 切过滤，`Ctrl+Z` 撤销，`,` / `.` 切上一/下一组，`S` 汇总。
- 看完点「汇总」：列出**删除清单**（勾选即送回收站）与**保留清单**（可临时补勾加入删除），确认后统一执行。执行前会逐项校验，文件已被改动/消失的项会跳过并给出原因。
- 安全闸：删除某个源文件时，若该组已经没有任何保留的结果，会要求**显式确认**（避免把一组删成空壳）。

### 被过滤的文件如何重新处理

- 过滤器在 `scan` / `encode` / `drag` 中生效：被标记为「过滤」的文件会被跳过并打印原因。
- 想临时忽略过滤器：加 `--include-filtered`（例如 `python vpipe.py drag --include-filtered "D:\videos\目录"`）。
- 想整体关掉审阅功能：`config.json` 里设 `"review_enabled": false`。

### 状态文件与安全

- 状态在 `<目标目录>\.data\review.json`，采用**原子写入**（先写 `.tmp` 再改名），中断不会写坏。
  （首次升级到新布局时，若 `.data` 下还没有状态，会自动把旧 `_work\review.json` 里**属于同一目录**的内容复制过来，保留原有过滤/删除标记。）
- 文件损坏时自动备份为 `review.json.bad-<时间戳>` 并重建，**只告警不崩溃**。
- 媒体播放走**令牌白名单**（`/media/<token>`，token 由路径哈希得到），不接受任意路径，杜绝越权读取。
- 端口记录在 `<目标目录>\.data\review.port`；重复双击入口**不会**重复起服务（已在运行就直接打开页面），进度原样保留。每个工作区各自独立，互不影响。
- `config.json` 相关字段：

  ```json
  "review_enabled": true,
  "review_page_port": 0
  ```

  `review_page_port`：固定端口号，`0`（默认）= 自动分配空闲端口。

## 目录结构（仓库）

```
ffmpeg-Easy-Use\
├─ ffmpeg批量压缩.bat      ← 统一入口（拖入即用）
├─ 打开命令行(高级).bat
├─ 一键安装.bat            ← 双击即装；拖入目录 = 装到「目录\FFmpeg\」
├─ 一键卸载.bat
├─ install\                ← 安装/卸载脚本（PowerShell）
│   ├─ install.ps1
│   └─ uninstall.ps1
├─ README.md
└─ core\                   ← 程序与默认配置
    ├─ compress_gui.py     ← 简易图形入口
    ├─ vpipe.py            ← 管线核心（探测/规划/编码/报告）
    ├─ review.py           ← 人工审阅服务
    ├─ gui_page.html
    ├─ review_page.html
    ├─ config.json         ← 默认配置（安装时写入 ffmpeg/work 路径）
    └─ 默认预设_config.json
```

> 安装后这份仓库内容会**原样复制**到 `<安装根>\script\`，所以 `core\` 里的脚本必须与 `config.json`、两个 `.html`、预设保持同一个目录（程序内部一律用“脚本自身目录”相对定位，不含任何硬编码路径）。

## 依赖

- **Windows 10 或更高版本**（安装/卸载脚本用 Windows PowerShell 5.1）。
- **Python 3**，且 `python` 能在 `PATH` 中直接调用（推荐官方安装包，默认自带 `tkinter`）。
- **ffmpeg**：由安装器提供——检测到就复用，否则自动下载 essentials 版到 `<安装根>\bin`。程序需要 `ffmpeg.exe` 与 `ffprobe.exe` 同时存在。
- `tkinter` 仅「选择预设文件」的系统文件对话框用到；Windows 官方 Python 默认包含。

## 许可与致谢

- 本项目仅封装调用 FFmpeg，FFmpeg 版权与许可见其官方项目；自动下载的 Windows 构建来自 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)（essentials），备用来源为 [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)。
