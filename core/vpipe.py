#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
vpipe —— 本地视频批量压缩管线（scan / plan / encode / report / compare / drag）

设计目标
  * 对编码/封装/分辨率/帧率各异的视频做批量压缩，便于本机存储与播放。
  * 绝不修改/删除源文件：产物要么写入源目录并加 _<tag> 后缀，要么写入独立 output_dir。
  * 全程可审阅：plan 阶段先输出每个文件的真实 ffmpeg 命令，确认后再 encode。
  * 容错优先：绝不留下“孤儿 ffmpeg 进程”或“看起来完整但实际截断”的半成品文件。

异常与中断处理要点（本版增强）
  * 进程树绑定（Windows Job Object）：父进程一旦退出/被杀，其 ffmpeg 子进程由内核连带终止。
  * 信号处理：Ctrl+C / Ctrl+Break 立即终止子进程、清理中间文件、落状态，退出码 130。
  * 原子输出：ffmpeg 先写 <stem>_<tag>.part.<ext>，成功后才 os.replace 到最终名；
    失败或被中断则删除 .part，绝不会用坏文件覆盖已存在的好文件。
  * 全局兜底：任何未捕获异常都转为一行友好错误 + 退出码 1（--debug 时保留完整堆栈）。
  * 各类子进程调用均带超时，磁盘/日志/状态写入失败均降级为告警而非崩溃。

用法（在 config.json 所在目录执行；也支持把文件/目录拖到 .bat 上运行）
  python vpipe.py scan                         # 扫描源目录 -> <工作区>/.data/inventory.json
  python vpipe.py plan --profile hevc_nvenc_1080
  python vpipe.py encode --profile hevc_nvenc_1080 [--only 文件名 ...] [--limit N] [--dry-run] [--force]
  python vpipe.py report --profile hevc_nvenc_1080 [--quality] [--quality-seg 30]
  python vpipe.py drag <文件或目录> [--profile xxx]     # 拖拽入口：就地压缩 + 出报告 + 出审阅入口

工作区（目标目录）：每个被处理的目录各自独立管理——体积报告(CSV/HTML)与「审阅.bat」放在
其根目录，状态/库存/审阅 json 与每个文件的编码日志放在其 .data 子目录。

退出码：0=全部成功；1=存在失败项；2=参数/配置错误；130=用户中断

依赖：ffmpeg.exe / ffprobe.exe（路径由 config.json 的 ffmpeg_dir 指定，一键安装器会自动写入）
"""
import argparse
import csv
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

VIDEO_EXT = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv', '.wmv', '.m4v',
             '.ts', '.m2ts', '.mts', '.vob', '.3gp', '.mpg', '.mpeg', '.rmvb', '.rm'}

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


# ---------------------------------------------------------------- 全局运行状态
CANCELLED = False
CURRENT_PROC = None
LAST_PROBE_ERR = ''

# Windows Job Object：保证父进程消失时子进程被连带终止，杜绝孤儿进程
JOB_HANDLE = None
_job_k = None


def _init_job_object():
    """创建带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 的 Job Object（仅 Windows）。"""
    global JOB_HANDLE, _job_k
    if os.name != 'nt':
        return
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [('ReadOperationCount', ctypes.c_ulonglong),
                        ('WriteOperationCount', ctypes.c_ulonglong),
                        ('OtherOperationCount', ctypes.c_ulonglong),
                        ('ReadTransferCount', ctypes.c_ulonglong),
                        ('WriteTransferCount', ctypes.c_ulonglong),
                        ('OtherTransferCount', ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                        ('PerJobUserTimeLimit', ctypes.c_longlong),
                        ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t),
                        ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ('IoInfo', IO_COUNTERS),
                        ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]

        h = k.CreateJobObjectW(None, None)
        if not h:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k.CloseHandle(h)
            return
        JOB_HANDLE = h
        _job_k = k
    except Exception:
        JOB_HANDLE = None
        _job_k = None


def _assign_job(proc):
    """把子进程加入 Job Object；失败时静默降级（不影响正常编码）。"""
    if os.name != 'nt' or not JOB_HANDLE or _job_k is None or proc is None:
        return
    try:
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        hp = _job_k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
        if hp:
            _job_k.AssignProcessToJobObject(JOB_HANDLE, hp)
            _job_k.CloseHandle(hp)
    except Exception:
        pass


def _terminate_proc(proc, wait=5):
    """先 terminate，宽限期内不退出则 kill。"""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=wait)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _terminate_current():
    _terminate_proc(CURRENT_PROC)


def _on_signal(signum, frame):
    global CANCELLED
    CANCELLED = True
    _terminate_current()


def _install_signal_handlers():
    for name in ('SIGINT', 'SIGBREAK'):
        s = getattr(signal, name, None)
        if s is None:
            continue
        try:
            signal.signal(s, _on_signal)
        except (ValueError, OSError):
            pass


# ---------------------------------------------------------------- 基础工具
def ensure_utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8')
        except Exception:
            pass


def log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def die(msg, code=EXIT_USAGE):
    sys.stderr.write("错误: " + msg + "\n")
    sys.exit(code)


def _probe_reason():
    return (" —— " + LAST_PROBE_ERR) if LAST_PROBE_ERR else ""


def _remove_quiet(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def load_config(path):
    if not os.path.isfile(path):
        die("找不到配置文件: " + path)
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:   # utf-8-sig 兼容记事本 BOM
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        die("配置文件 JSON 格式有误（%s 第 %s 行，第 %s 列）: %s" % (
            os.path.basename(path), getattr(e, 'lineno', '?'),
            getattr(e, 'colno', '?'), getattr(e, 'msg', e)))
    except OSError as e:
        die("无法读取配置文件: %s（%s）" % (path, e))
    if not isinstance(cfg, dict):
        die("配置文件内容必须是 JSON 对象: " + path)
    if not isinstance(cfg.get('work_dir'), str) or not cfg.get('work_dir'):
        die("配置缺少必需的 work_dir（字符串）: " + path)
    if not isinstance(cfg.get('profiles'), dict) or not cfg.get('profiles'):
        die("配置缺少必需的 profiles（非空对象）: " + path)
    return cfg


def pick_profile(cfg, name):
    """统一解析 profile（显式指定 -> default_profile -> 第一个），未知时报友好错误。"""
    profiles = cfg.get('profiles') or {}
    chosen = name or cfg.get('default_profile') or next(iter(profiles), None)
    if not chosen:
        die("配置中没有可用的 profile，请检查 config.json")
    profile = profiles.get(chosen)
    if not profile or not isinstance(profile, dict):
        die("未知 profile: %s（可选: %s）" % (chosen, ", ".join(profiles) or "无"))
    return chosen, profile


def tools_from_cfg(cfg):
    d = cfg.get('ffmpeg_dir')
    if not d:
        # 未显式配置时：优先用 <安装目录>\bin（与本机结构一致），再回退到 PATH 里的 ffmpeg
        sibling = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bin')
        if os.path.isfile(os.path.join(sibling, 'ffmpeg.exe')):
            d = sibling
        else:
            d = os.path.dirname(shutil.which('ffmpeg') or '') or sibling
    t = {
        'ffmpeg': os.path.join(d, 'ffmpeg.exe'),
        'ffprobe': os.path.join(d, 'ffprobe.exe'),
        'ffplay': os.path.join(d, 'ffplay.exe'),
    }
    for k in ('ffmpeg', 'ffprobe'):
        if not os.path.isfile(t[k]):
            die("找不到 %s: %s" % (k, t[k]))
    return t


def parse_fps(rate):
    if not rate or rate in ('0/0', 'N/A'):
        return 0.0
    if '/' in rate:
        a, b = rate.split('/')[:2]
        try:
            b = float(b)
            return round(float(a) / b, 3) if b else 0.0
        except ValueError:
            return 0.0
    try:
        return round(float(rate), 3)
    except ValueError:
        return 0.0


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def to_int(x, default=0):
    return int(to_float(x, default))


def human_size(n):
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return ("%.1f %s" % (n, unit)) if unit != 'B' else ("%d %s" % (n, unit))
        n /= 1024


def human_time(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def fmt_bitrate(kbps):
    return ("%.2f Mbps" % (kbps / 1000.0)) if kbps >= 1000 else ("%d kbps" % kbps)


# ---------------------------------------------------------------- 探测
PROBE_TIMEOUT = 30


def probe(ffprobe, path):
    """探测媒体信息。失败/超时时返回 None，并把简短原因写入 LAST_PROBE_ERR。"""
    global LAST_PROBE_ERR
    LAST_PROBE_ERR = ''
    cmd = [ffprobe, '-v', 'error', '-print_format', 'json',
           '-show_format', '-show_streams', path]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        LAST_PROBE_ERR = "探测超时(%ds)" % PROBE_TIMEOUT
        return None
    except OSError as e:
        LAST_PROBE_ERR = "无法启动 ffprobe (%s)" % e
        return None
    if p.returncode != 0:
        err = p.stderr.decode('utf-8', 'replace').strip()
        LAST_PROBE_ERR = err.splitlines()[-1] if err else ("ffprobe 退出码 %d" % p.returncode)
        return None
    try:
        return json.loads(p.stdout.decode('utf-8', 'replace'))
    except json.JSONDecodeError:
        LAST_PROBE_ERR = "ffprobe 输出无法解析为 JSON"
        return None


def stream_bitrate_kbps(s, fmt):
    """流码率：优先流自身的 bit_rate，否则用 (文件大小*8/时长 - 其他流) 估算。"""
    b = to_int(s.get('bit_rate'))
    if b > 0:
        return int(b / 1000)
    return 0


def make_record(ffprobe, path, rel):
    data = probe(ffprobe, path)
    if not data:
        return None
    fmt = data.get('format', {}) or {}
    v = a = None
    n_v = n_a = 0
    for s in data.get('streams', []) or []:
        if s.get('codec_type') == 'video':
            n_v += 1
            if v is None:
                v = s
        elif s.get('codec_type') == 'audio':
            n_a += 1
            if a is None:
                a = s
    try:
        size = to_int(fmt.get('size')) or os.path.getsize(path)
    except OSError:
        size = to_int(fmt.get('size'))
    dur = to_float(fmt.get('duration'))
    # 视频码率
    vb = stream_bitrate_kbps(v, fmt) if v else 0
    if v and vb == 0 and dur > 0 and size:
        total_kbps = int(size * 8 / dur / 1000)
        ab = stream_bitrate_kbps(a, fmt) if a else 0
        vb = max(0, total_kbps - ab)
    # 旋转
    rotation = 0
    if v:
        for sd in (v.get('side_data_list') or []):
            if 'rotation' in sd:
                rotation = to_int(sd.get('rotation'))
        if not rotation:
            rotation = to_int((v.get('tags') or {}).get('rotate'))
    pix = (v or {}).get('pix_fmt', '')
    ct = (v or {}).get('color_transfer', '')
    bit_depth = 10 if any(k in pix for k in ('10', 'p010', 'y210')) else (12 if any(k in pix for k in ('12', 'p012')) else 8)
    hdr = ct in ('smpte2084', 'arib-std-b67') or bit_depth > 8
    inter = (v or {}).get('field_order', 'progressive') not in ('progressive', '', None)

    rec = {
        'path': path,
        'name': os.path.basename(path),
        'rel': rel,
        'size': size,
        'duration': round(dur, 3),
        'vcodec': (v or {}).get('codec_name', ''),
        'vprofile': (v or {}).get('profile', ''),
        'width': to_int((v or {}).get('width')),
        'height': to_int((v or {}).get('height')),
        'fps': parse_fps((v or {}).get('r_frame_rate')),
        'pix_fmt': pix,
        'bit_depth': bit_depth,
        'vbitrate_kbps': vb,
        'acodec': (a or {}).get('codec_name', ''),
        'abitrate_kbps': stream_bitrate_kbps(a, fmt) if a else 0,
        'achannels': to_int((a or {}).get('channels')),
        'asample': to_int((a or {}).get('sample_rate')),
        'rotation': rotation,
        'n_video': n_v,
        'n_audio': n_a,
        'hdr': bool(hdr),
        'interlaced': bool(inter),
    }
    # 旋转后真实显示尺寸
    if abs(rotation) in (90, 270) and rec['width'] and rec['height']:
        rec['disp_w'], rec['disp_h'] = rec['height'], rec['width']
    else:
        rec['disp_w'], rec['disp_h'] = rec['width'], rec['height']
    # 每像素每帧比特数：越低说明源本身越省，压缩收益越有限
    px = (rec['width'] or 0) * (rec['height'] or 0)
    rec['bpp'] = round(rec['vbitrate_kbps'] * 1000.0 / (px * rec['fps']), 4) if px and rec['fps'] else 0
    return rec


def is_part_file(name):
    return '.part.' in name


def iter_files(indir, exclude_dirs=()):
    """遍历视频文件；跳过 .part 中间文件；不可读目录仅告警不崩溃。"""
    exclude = set(os.path.normcase(os.path.abspath(d)) for d in exclude_dirs)
    unreadable = []

    def _onerror(err):
        unreadable.append(err)

    for root, dirs, files in os.walk(indir, onerror=_onerror):
        dirs[:] = [d for d in dirs
                   if os.path.normcase(os.path.abspath(os.path.join(root, d))) not in exclude
                   and not d.startswith('_work') and d != DATA_DIR_NAME]
        for fn in sorted(files):
            if is_part_file(fn):
                continue
            if os.path.splitext(fn)[1].lower() in VIDEO_EXT:
                p = os.path.join(root, fn)
                yield p, os.path.relpath(p, indir)
    for err in unreadable:
        log("  [警告] 目录无法读取，已跳过: %s" % getattr(err, 'filename', err))


# ---------------------------------------------------------------- 状态文件
def state_path(work, profile):
    return os.path.join(work, 'state_%s.json' % profile)


def load_state(work, profile):
    empty = {'profile': profile, 'out_dir': None, 'items': {}}
    p = state_path(work, profile)
    if not os.path.isfile(p):
        return dict(empty)
    try:
        with open(p, 'r', encoding='utf-8') as f:
            st = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log("警告: 状态文件损坏，已忽略并重建: %s（%s）" % (p, e))
        return dict(empty)
    if not isinstance(st, dict):
        log("警告: 状态文件内容异常，已忽略并重建: %s" % p)
        return dict(empty)
    st.setdefault('profile', profile)
    st.setdefault('out_dir', None)
    if not isinstance(st.get('items'), dict):
        st['items'] = {}
    return st


def save_state(work, profile, st):
    """原子写入状态：临时文件 + os.replace；失败仅告警，绝不因日志/缓存目录问题中断编码。"""
    p = state_path(work, profile)
    tmp = p + '.tmp'
    try:
        os.makedirs(work, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError as e:
        log("    警告: 状态未写入（%s）" % e)
        _remove_quiet(tmp)


# ---------------------------------------------------------------- 审阅过滤器（review.json）
# review.json 由 review.py 维护；这里只读它来得到「过滤」名单，并把新产物登记为待审阅。
# 约定（与 review.py 的 key_of 完全一致）：主键 = 绝对路径的 normcase 形式。
REVIEW_NAME = 'review.json'
REVIEW_BAT_NAME = '审阅.bat'

# 工作区（目标目录）：报告与「审阅.bat」放在工作区根目录，json 数据与日志放工作区下的 .data。
DATA_DIR_NAME = '.data'


def data_dir_of(ws):
    """工作区的数据目录（.data）：库存/状态/审阅 json 与日志都放这里。"""
    return os.path.join(os.path.abspath(ws), DATA_DIR_NAME)


def resolve_workspace(cfg, override=None, out_root=''):
    """确定工作区（目标目录）：显式 --work 优先，其次取产物所在目录（dir 模式取输出根，否则源目录）。"""
    if override:
        return os.path.abspath(override)
    if out_root:
        return os.path.abspath(out_root)
    return os.path.abspath(cfg.get('input_dir') or '')


def common_dir_of(paths):
    """一组文件的共同所在目录（拖拽时用来确定工作区）；无法归并时退化为第一个文件所在目录。"""
    dirs = [os.path.dirname(os.path.abspath(p)) for p in paths]
    if not dirs:
        return ''
    try:
        return os.path.commonpath(dirs)
    except ValueError:
        return dirs[0]


def _profile_out_root(cfg, prof_name):
    """dir 输出模式下该 profile 的输出根目录（samedir 模式返回 ''）。"""
    if cfg.get('output_mode', 'samedir') == 'samedir':
        return ''
    outdir = os.path.abspath(cfg.get('output_dir') or '')
    return os.path.join(outdir, prof_name) if outdir else ''


def review_enabled(cfg):
    return bool(cfg.get('review_enabled', True))


def review_key(path):
    return os.path.normcase(os.path.abspath(path))


def review_path(work):
    return os.path.join(os.path.abspath(work), REVIEW_NAME)


def ensure_review_migrated(work):
    """新版布局把 review.json 放到工作区内的 .data；若该位置缺失、而旧版 _work 里
    存在「root 与本工作区一致」的状态，则复制一份过去，保住用户既有的过滤/删除标记。
    只复制、绝不删除旧文件。"""
    work = os.path.abspath(work)
    dst = review_path(work)
    if os.path.isfile(dst):
        return False
    ws = os.path.normcase(os.path.dirname(work))          # work = <ws>/.data
    legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_work', REVIEW_NAME)
    if not os.path.isfile(legacy) or os.path.normcase(os.path.dirname(legacy)) == os.path.normcase(work):
        return False
    try:
        with open(legacy, 'r', encoding='utf-8') as f:
            rv = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(rv, dict) or not isinstance(rv.get('items'), dict):
        return False
    if not rv.get('root') or os.path.normcase(os.path.abspath(rv['root'])) != ws:
        return False                                          # 旧状态不属于本工作区，不动它
    try:
        os.makedirs(work, exist_ok=True)
        shutil.copy2(legacy, dst)
        log("已把旧版审阅状态复制到工作区: %s（保留原有过滤标记）" % dst)
        return True
    except OSError as e:
        log("警告: 旧版审阅状态复制失败（%s）" % e)
        return False


def load_review(work):
    """读取审阅状态；缺失或损坏时仅告警并返回 None（绝不中断压缩管线）。"""
    ensure_review_migrated(work)
    p = review_path(work)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            rv = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log("警告: 审阅状态无法读取，本次忽略过滤名单（%s）" % e)
        return None
    if not isinstance(rv, dict) or not isinstance(rv.get('items'), dict):
        log("警告: 审阅状态内容异常，本次忽略过滤名单: " + p)
        return None
    return rv


def filtered_keys(work):
    """被标记为「过滤」（不再进入处理管线）的所有项的主键集合。
    注意：源与产物一视同仁——只要该项 filtered=true 就跳过，避免把产物当成新源再压一遍。"""
    rv = load_review(work)
    if not rv:
        return set()
    return set(k for k, it in rv['items'].items()
               if isinstance(it, dict) and it.get('filtered'))


def is_review_filtered(path, keys):
    return review_key(path) in keys


def _review_item(path, kind, tag, parent):
    return {
        'name': os.path.basename(path), 'path': os.path.abspath(path),
        'kind': kind, 'tag': tag, 'parent': os.path.abspath(parent) if parent else None,
        'meta': None, 'warn': '',
        'delete': False, 'filtered': (kind == 'result'),
        'reviewed': False, 'cleaned': False, 'history': [],
    }


def mark_review_pending(work, pairs, root=''):
    """把本轮新产生的「源 / 产物」登记进 review.json（待审阅）。
    pairs: [(源路径, 产物路径 或 None, tag)]；已存在的项只补缺失字段，绝不覆盖用户已设的开关。
    root: 所属工作区（目标目录）；仅在状态尚无 root 时补上，便于审阅页锁定到本工作区。"""
    if not pairs:
        return
    p = review_path(work)
    rv = None
    if os.path.isfile(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                rv = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log("    警告: 审阅状态无法读取，本轮不登记待审阅项（%s）" % e)
            return
    if not isinstance(rv, dict):
        rv = {'version': 2, 'root': '', 'items': {}}
    if root and not rv.get('root'):
        rv['root'] = os.path.abspath(root)
    if not isinstance(rv.get('items'), dict):
        rv['items'] = {}
    items = rv['items']
    for src, out, tag in pairs:
        for path, kind, parent, tg in ((src, 'source', None, None),
                                       (out, 'result', src, tag)):
            if not path:
                continue
            fresh = _review_item(path, kind, tg, parent)
            k = review_key(path)
            it = items.get(k)
            if it is None:
                items[k] = fresh
            else:
                for f in ('kind', 'parent', 'delete', 'filtered', 'reviewed', 'cleaned', 'history'):
                    it.setdefault(f, fresh[f])
    rv['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    tmp = p + '.tmp'
    try:
        os.makedirs(work, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(rv, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError as e:
        log("    警告: 审阅状态未能写入（%s）" % e)
        _remove_quiet(tmp)


def write_review_shortcut(ws):
    """在工作区根目录生成/刷新「审阅.bat」（GBK + CRLF）。该入口把源目录与数据目录都固定到
    本工作区，只审阅当前工作区的内容；可反复双击。返回路径或 None。"""
    ws = os.path.abspath(ws)
    data = data_dir_of(ws)
    here = os.path.dirname(os.path.abspath(__file__))
    review_py = os.path.join(here, 'review.py')
    cfg_path = os.path.join(here, 'config.json')
    p = os.path.join(ws, REVIEW_BAT_NAME)
    lines = [
        '@echo off',
        'chcp 936 >nul',
        'python "%s" --config "%s" --input "%s" --work "%s" %%*' % (
            review_py, cfg_path, ws, ws),
        'set "RC=%ERRORLEVEL%"',
        'if not "%RC%"=="0" echo   注意: 审阅服务未能正常结束（退出码 %RC%）。',
        'if not "%RC%"=="0" pause',
        'exit /b %RC%',
        '',
    ]
    try:
        os.makedirs(ws, exist_ok=True)
        os.makedirs(data, exist_ok=True)
        with open(p, 'w', encoding='gbk', newline='\r\n') as f:
            f.write("\n".join(lines))
        return p
    except OSError as e:
        log("警告: 审阅入口 .bat 未能写入（%s）" % e)
        return None


def load_inventory(work):
    p = os.path.join(work, 'inventory.json')
    if not os.path.isfile(p):
        die("尚未生成 inventory，请先运行: vpipe.py scan")
    try:
        with open(p, 'r', encoding='utf-8') as f:
            inv = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        die("inventory 无法读取（%s），请重新运行 scan: %s" % (e, p))
    if not isinstance(inv, dict) or 'items' not in inv or not inv.get('input_dir'):
        die("inventory 内容不完整，请重新运行 scan: " + p)
    return inv


# ---------------------------------------------------------------- 分辨率（与编码档位解耦）
def resolve_resolution(cfg, args=None):
    """分辨率与编码档位相互独立，分开配置。
    优先级：命令行 > config.resolution > 保持原始。
    max_long_side = 0 -> 不缩放，保持源视频原分辨率；max_fps = 0 -> 保持源帧率。
    """
    res = cfg.get('resolution')
    if not isinstance(res, dict):
        res = {}
    mls = to_int(res.get('max_long_side'), 0)
    mf = to_int(res.get('max_fps'), 0)
    if args is not None:
        v = getattr(args, 'max_long_side', None)
        if v is not None:
            mls = to_int(v, 0)
        v = getattr(args, 'max_fps', None)
        if v is not None:
            mf = to_int(v, 0)
    return {'max_long_side': max(0, mls), 'max_fps': max(0, mf)}


def describe_resolution(res):
    mls = to_int(res.get('max_long_side'), 0)
    mf = to_int(res.get('max_fps'), 0)
    a = ("长边≤%d（超出则等比缩小）" % mls) if mls else "保持源分辨率（不缩放）"
    b = ("帧率≤%d" % mf) if mf else "保持源帧率"
    return a + "，" + b


# ---------------------------------------------------------------- 滤镜 / 编码参数
def build_vf(res, rec):
    """构建 -vf。分辨率独立于编码档位：
    max_long_side>0 且源长边超过该值时才等比缩小到该长边，否则保持原分辨率（仅保证偶数尺寸）。
    max_fps>0 且源帧率高于该值时限制帧率。"""
    parts = []
    mls = to_int(res.get('max_long_side'), 0)
    W = rec.get('width') or 0
    H = rec.get('height') or 0
    if mls > 0 and max(W, H) > mls:
        parts.append("scale=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"
                     % (mls, mls))
    else:
        # 对已经是偶数尺寸的源而言是恒等操作
        parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    mf = to_int(res.get('max_fps'), 0)
    if mf and rec.get('fps') and rec['fps'] > mf + 0.01:
        parts.append("fps=%d" % int(mf))
    return ",".join(parts)


def video_args(profile):
    enc = profile['encoder']
    args = ['-c:v', enc] + list(profile.get('vparams', []))
    if enc in ('libx265', 'hevc_nvenc', 'hevc_qsv', 'hevc_amf'):
        args += ['-tag:v', 'hvc1']
    elif 'av1' in enc:
        args += ['-tag:v', 'av01']
    return args


def audio_args(profile):
    if profile.get('audio_mode', 'copy') == 'copy':
        return ['-c:a', 'copy']
    return ['-c:a', 'aac', '-b:a', str(profile.get('audio_bitrate', '160k')), '-ac', '2']


# ---------------------------------------------------------------- GPU 加速（NVDEC + CUDA 滤镜 + NVENC）
NVIDIA_ENCODERS = ('h264_nvenc', 'hevc_nvenc', 'av1_nvenc')

# 与 yuv420p 同族（8bit 4:2:0）的源格式：NVDEC 解码出的显存帧本身就是这个格式，无需再转换
CUDA_420P = ('yuv420p', 'nv12', 'nv21', 'yuvj420p', 'yuv420p(tv)', 'yuv420p(tv, bt709)')

# 这些关键字说明「本机 GPU/CUDA 整体不可用」，命中后本次运行彻底停用 GPU 候选，
# 避免每个文件都白试一遍；其余错误（如源编码不受 NVDEC 支持）只做单文件降级。
GPU_FATAL_MARKS = (
    'no capable devices found',
    'no cuda-capable device',
    'cannot load nvcuda',
    'cannot load cuda',
    'failed loading nvcuda',
    'cuda_error_no_device',
    'driver does not support',
    'minimum required nvidia driver',
    'nvenc api version',
    'not supported by your gpu',
    'openencodesessionex failed',
)


def gpu_enabled(cfg, args=None):
    """是否允许使用 GPU 加速：config.gpu.enabled 未显式关闭，且命令行未加 --no-gpu。"""
    g = cfg.get('gpu')
    if isinstance(g, dict) and g.get('enabled') is False:
        return False
    if args is not None and getattr(args, 'no_gpu', False):
        return False
    return True


def needs_scale(res, rec):
    """源长边超过上限、需要缩小。"""
    mls = to_int(res.get('max_long_side'), 0)
    return mls > 0 and max(rec.get('width') or 0, rec.get('height') or 0) > mls


def needs_fps_limit(res, rec):
    """需要限制帧率。fps 滤镜不支持 CUDA 帧，故限帧时不能用全 GPU 管线。"""
    mf = to_int(res.get('max_fps'), 0)
    return bool(mf) and (rec.get('fps') or 0) > mf + 0.01


def build_vf_cuda(res, rec, pix_fmt=None):
    """CUDA 版滤镜链：只允许 CUDA 像素格式的滤镜（scale_cuda）。
    限帧（fps 滤镜）场景不能走这里，调用前须用 needs_fps_limit 排除。
    pix_fmt 为档位要求的软件像素格式（如 yuv420p）；当源不是同族的 8bit 4:2:0 时，
    借 scale_cuda 的 format 选项在显存内直接转换，保持与纯软件路径一致的输出格式。"""
    parts = []
    want_fmt = ''
    if (pix_fmt or '') == 'yuv420p' and (rec.get('pix_fmt') or '') not in CUDA_420P:
        want_fmt = 'format=yuv420p'
    if needs_scale(res, rec):
        mls = to_int(res.get('max_long_side'), 0)
        s = ("scale_cuda=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2:interp_algo=lanczos"
             % (mls, mls))
        parts.append(s + (':' + want_fmt if want_fmt else ''))
    elif ((rec.get('width') or 0) % 2) or ((rec.get('height') or 0) % 2):
        # 奇数尺寸源：NVENC 要求偶数，用 scale_cuda 归一到偶数（只裁掉 1 像素）
        s = "scale_cuda=trunc(iw/2)*2:trunc(ih/2)*2"
        parts.append(s + (':' + want_fmt if want_fmt else ''))
    elif want_fmt:
        # 尺寸无需处理，仅做显存内的像素格式转换
        parts.append("scale_cuda=" + want_fmt)
    return ",".join(parts)


def describe_hw(mode):
    if mode == 'cuda-full':
        return '全 GPU（NVDEC 解码 + CUDA 缩放）'
    if mode == 'cuda-decode':
        return 'GPU 解码（NVDEC）'
    return '纯软件（CPU 解码）'


def hw_candidates(cfg, profile, res, rec, args=None, gpu_ok=True):
    """按优先级返回候选加速模式，末位恒为 None（纯软件兜底）。
    cuda-full  : NVDEC 解码 + scale_cuda 缩放 + NVENC 编码（帧全程留在显存，最快）
    cuda-decode: 仅 NVDEC 解码（帧自动回内存），缩放/编码沿用原路径
    仅当编码器为 NVENC 且不需要限帧时才尝试 cuda-full（fps 滤镜不支持 CUDA 帧）。"""
    cands = []
    if gpu_ok and gpu_enabled(cfg, args):
        enc = profile.get('encoder') or ''
        if enc in NVIDIA_ENCODERS and not needs_fps_limit(res, rec):
            cands.append('cuda-full')
        cands.append('cuda-decode')
    cands.append(None)
    return cands


def gpu_fatal_find(tail):
    """从 ffmpeg 错误输出里识别「GPU/CUDA 整体不可用」关键字，返回命中的关键字或 None。"""
    low = (tail or '').lower()
    for mk in GPU_FATAL_MARKS:
        if mk in low:
            return mk
    return None


def build_encode_cmd(tools, profile, res, rec, out_path, hw=None):
    """hw: None=纯软件；'cuda-full'=NVDEC 解码 + CUDA 缩放 + GPU 编码；'cuda-decode'=仅 NVDEC 解码。
    cuda-full 时输入帧即为 CUDA 显存帧，滤镜必须用 CUDA 版，且不能指定软件侧 pix_fmt。"""
    cmd = [tools['ffmpeg'], '-hide_banner', '-y']
    if hw == 'cuda-full':
        cmd += ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda']
    elif hw == 'cuda-decode':
        cmd += ['-hwaccel', 'cuda']
    cmd += ['-i', rec['path'], '-map', '0:v:0', '-map', '0:a:0?']
    vf = build_vf_cuda(res, rec, profile.get('pix_fmt')) if hw == 'cuda-full' else build_vf(res, rec)
    if vf:
        cmd += ['-vf', vf]
    cmd += video_args(profile)
    if hw != 'cuda-full':
        cmd += ['-pix_fmt', profile.get('pix_fmt', 'yuv420p')]
    cmd += audio_args(profile)
    cmd += ['-map_metadata', '0', '-sn', '-dn']
    if profile.get('container', 'mp4') == 'mp4':
        cmd += ['-movflags', '+faststart']
    cmd += ['-progress', 'pipe:1', '-nostats', out_path]
    return cmd


def output_tags(cfg):
    return set(p.get('tag') or n for n, p in cfg['profiles'].items())


def is_pipeline_output(cfg, path):
    """是否为管线产物：仅当同目录存在对应的无后缀源文件时才认定，
    避免把用户真实存在的 xxx_hevc.mp4 误判为产物而跳过。"""
    d = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    for t in output_tags(cfg):
        suffix = '_' + t
        if stem.endswith(suffix):
            base = stem[:-len(suffix)]
            for ext in VIDEO_EXT:
                if os.path.isfile(os.path.join(d, base + ext)):
                    return True
    return False


def output_path_for(cfg, prof_name, profile, rec):
    """计算输出路径。
    output_mode=samedir -> 写入源文件同目录，命名加 _<tag> 后缀（绝不与源同名，故不会覆盖源文件）
    其它                 -> <output_dir>/<profile> 子目录，同名 .mp4
    """
    stem = os.path.splitext(rec['name'])[0]
    ext = cfg.get('output_ext', '.mp4')
    if cfg.get('output_mode', 'samedir') == 'samedir':
        tag = profile.get('tag') or prof_name
        return os.path.join(os.path.dirname(rec['path']), "%s_%s%s" % (stem, tag, ext))
    out_root = os.path.join(os.path.abspath(cfg.get('output_dir') or ''), prof_name)
    return os.path.join(out_root, "%s%s" % (stem, ext))


def part_path_for(out_path):
    """ffmpeg 实际写入的中间文件路径（保留扩展名，保证 ffmpeg 能识别封装格式）。"""
    base, ext = os.path.splitext(out_path)
    return base + '.part' + ext


def finalize_output(part, final):
    """成功后把 .part 原子改名到最终路径。返回 True/False。"""
    try:
        os.replace(part, final)
        return True
    except OSError as e:
        log("    失败: 无法写入最终文件（%s）" % e)
        _remove_quiet(part)
        return False


def collect_drag_files(cfg, paths, filter_keys=()):
    files = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            for f, _rel in iter_files(p, exclude_dirs=[os.path.join(p, '_work')]):
                files.append(f)
        elif os.path.isfile(p):
            if os.path.splitext(p)[1].lower() in VIDEO_EXT:
                files.append(p)
            else:
                log("  [忽略] 非视频文件: " + os.path.basename(p))
        else:
            log("  [忽略] 路径不存在: " + p)
    seen, out = set(), []
    for f in files:
        key = os.path.normcase(f)
        if key in seen:
            continue
        seen.add(key)
        base = os.path.basename(f)
        if is_part_file(base):
            log("  [忽略] 未完成的中间文件: " + base)
            continue
        if is_pipeline_output(cfg, f):
            log("  [忽略] 已是压缩产物: " + base)
            continue
        if filter_keys and is_review_filtered(f, filter_keys):
            log("  [忽略] 审阅中已标记为过滤（--include-filtered 可强制处理）: " + base)
            continue
        out.append(f)
    return out


def build_row(tools, name, in_path, out_path, size_in, size_out, dur, quality=False, seg=30.0):
    rin = make_record(tools['ffprobe'], in_path, name)
    rout = make_record(tools['ffprobe'], out_path, name)
    try:
        s_out = size_out or (os.path.getsize(out_path) if os.path.isfile(out_path) else 0)
    except OSError:
        s_out = size_out or 0
    row = {'name': name, 'size_in': size_in, 'size_out': s_out,
           'save_pct': (1 - s_out / size_in) * 100 if size_in else 0,
           'vb_in': (rin or {}).get('vbitrate_kbps', 0), 'vb_out': (rout or {}).get('vbitrate_kbps', 0),
           'res_in': '', 'res_out': '', 'psnr': '', 'ssim': ''}
    if rin:
        row['res_in'] = "%dx%d" % (rin['disp_w'], rin['disp_h'])
        row['fps_in'] = rin['fps']
    if rout:
        row['res_out'] = "%dx%d" % (rout['disp_w'], rout['disp_h'])
        row['fps_out'] = rout['fps']
    if quality and rout:
        start = max(0.0, (dur or 0) * 0.2)
        s = min(seg, max(1.0, (dur or 0) - start)) if dur else seg
        psnr, ssim = measure_quality(tools, out_path, in_path, start, s)
        row['psnr'] = psnr or ''
        row['ssim'] = ssim or ''
    return row


def emit_report(ws, prof_name, rows):
    """把体积对比报告（CSV + HTML）写到工作区根目录（目标目录）下。"""
    if not rows:
        log("没有可报告的结果。")
        return None
    try:
        os.makedirs(ws, exist_ok=True)
    except OSError as e:
        die("无法创建工作区目录 %s: %s" % (ws, e))
    csv_path = os.path.join(ws, 'report_%s.csv' % prof_name)
    cols = ['name', 'size_in', 'size_out', 'save_pct', 'vb_in', 'vb_out',
            'res_in', 'res_out', 'fps_in', 'fps_out', 'psnr', 'ssim']
    try:
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            w.writeheader()
            for r in rows:
                w.writerow(r)
    except OSError as e:
        log("警告: 报告 CSV 写入失败（%s）" % e)
    tot_in = sum(r['size_in'] for r in rows)
    tot_out = sum(r['size_out'] for r in rows)
    html_path = os.path.join(ws, 'report_%s.html' % prof_name)
    try:
        write_report_html(html_path, prof_name, rows, tot_in, tot_out)
    except OSError as e:
        log("警告: 报告 HTML 写入失败（%s）" % e)

    log("\n%-40s %10s %10s %8s %12s %12s %8s %8s" % (
        "文件", "原", "压缩后", "节省", "原码率", "新码率", "SSIM", "PSNR"))
    log("-" * 118)
    for r in rows:
        log("%-40s %10s %10s %7.1f%% %12s %12s %8s %8s" % (
            r['name'][:40], human_size(r['size_in']), human_size(r['size_out']), r['save_pct'],
            fmt_bitrate(r['vb_in']), fmt_bitrate(r['vb_out']), r.get('ssim', ''), r.get('psnr', '')))
    log("-" * 118)
    log("合计: %s -> %s  (节省 %.1f%%)" % (
        human_size(tot_in), human_size(tot_out),
        (1 - tot_out / tot_in) * 100 if tot_in else 0))
    log("报告: %s" % csv_path)
    log("报告: %s" % html_path)
    return html_path


COMPARE_TIMEOUT = 600


def run_compare(tools, pairs, cmp_dir, mode, start, dur):
    """pairs: [(name, orig_path, comp_path), ...]；生成人工对比产物。"""
    try:
        os.makedirs(cmp_dir, exist_ok=True)
    except OSError as e:
        die("无法创建对比产物目录 %s: %s" % (cmp_dir, e))
    for name, orig, comp in pairs:
        rec = make_record(tools['ffprobe'], comp, name) or {}
        W, H = rec.get('width'), rec.get('height')
        if not W or not H:
            log("[跳过] %s 无法读取分辨率%s" % (name, _probe_reason()))
            continue
        d = to_float((probe(tools['ffprobe'], comp) or {}).get('format', {}).get('duration'))
        st_ = start if start is not None else max(0.0, d * 0.35)
        sg = dur if dur is not None else min(20.0, max(3.0, d - st_))
        base = os.path.splitext(name)[0]
        scale_o = "scale=%d:%d:flags=lanczos,format=yuv420p" % (W, H)
        scale_c = "scale=%d:%d:flags=lanczos,format=yuv420p" % (W, H)

        if mode == 'frames':
            for k, f in enumerate([0.15, 0.45, 0.75], 1):
                t = max(0.0, d * f)
                outp = os.path.join(cmp_dir, "%s_对比图%d.png" % (base, k))
                fc = "[0:v]%s[o];[1:v]%s[c];[o][c]hstack=inputs=2" % (scale_o, scale_c)
                try:
                    rr = subprocess.run([tools['ffmpeg'], '-hide_banner', '-y',
                                         '-ss', "%.3f" % t, '-i', orig,
                                         '-ss', "%.3f" % t, '-i', comp, '-filter_complex', fc,
                                         '-frames:v', '1', outp],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=COMPARE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    log("  对比图生成超时: %s" % name)
                    continue
                except OSError as e:
                    log("  对比图生成失败（%s）: %s" % (e, name))
                    continue
                if rr.returncode == 0:
                    log("  对比图: %s  (@%.0fs)" % (outp, t))
                else:
                    log("  对比图生成失败: %s" % name)
            continue

        outp = os.path.join(cmp_dir, "%s_对比_%s.mp4" % (base, mode))
        if mode == 'diff':
            fc = ("[0:v]%s[a];[1:v]%s[b];[a][b]blend=all_mode=difference,lutrgb="
                  "r='clip(val*6,0,255)':g='clip(val*6,0,255)':b='clip(val*6,0,255)',format=yuv420p" %
                  (scale_o, scale_c))
        else:
            fc = "[0:v]%s[o];[1:v]%s[c];[o][c]hstack=inputs=2,format=yuv420p" % (scale_o, scale_c)
        try:
            r = subprocess.run([tools['ffmpeg'], '-hide_banner', '-y',
                                '-ss', "%.3f" % st_, '-t', "%.3f" % sg, '-i', orig,
                                '-ss', "%.3f" % st_, '-t', "%.3f" % sg, '-i', comp,
                                '-filter_complex', fc, '-c:v', 'libx264', '-crf', '20',
                                '-preset', 'veryfast', '-an', '-movflags', '+faststart', outp],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               timeout=COMPARE_TIMEOUT)
        except subprocess.TimeoutExpired:
            log("  生成超时: %s" % name)
            continue
        except OSError as e:
            log("  生成失败（%s）: %s" % (e, name))
            continue
        if r.returncode == 0:
            log("  对比视频(%s): %s  (左=原片 %s ~ %s, 右=压缩片)" % (
                mode, outp, human_time(st_), human_time(st_ + sg)))
        else:
            log("  生成失败: %s" % name)


# ---------------------------------------------------------------- scan
def cmd_scan(args):
    cfg = load_config(args.config)
    tools = tools_from_cfg(cfg)
    indir = os.path.abspath(args.input or cfg.get('input_dir') or '')
    if not indir or not os.path.isdir(indir):
        die("源目录不存在或未配置 input_dir: " + (indir or "(未设置)"))
    ws = resolve_workspace(cfg, args.work, out_root=indir)
    work = data_dir_of(ws)
    try:
        os.makedirs(work, exist_ok=True)
    except OSError as e:
        die("无法创建工作区数据目录 %s: %s" % (work, e))
    outdir = os.path.abspath(cfg.get('output_dir') or '')

    exclude = [os.path.join(indir, '_work')]
    if outdir:
        exclude.append(outdir)

    log("扫描目录: " + indir)
    rv_keys = filtered_keys(work) if (review_enabled(cfg) and not args.include_filtered) else set()
    if rv_keys:
        log("审阅过滤: 已跳过 %d 个被标记为「不需要转换」的文件（--include-filtered 可强制纳入）" % len(rv_keys))
    records = []
    for p, rel in iter_files(indir, exclude_dirs=exclude):
        if is_pipeline_output(cfg, p):
            log("  [跳过] 已存在对应源文件，判定为管线产物: " + rel)
            continue
        if rv_keys and is_review_filtered(p, rv_keys):
            log("  [跳过] 审阅中已标记为过滤: " + rel)
            continue
        rec = make_record(tools['ffprobe'], p, rel)
        if rec is None:
            log("  [跳过] 无法解析: %s%s" % (rel, _probe_reason()))
            continue
        records.append(rec)

    inv = {'input_dir': indir, 'count': len(records),
           'total_size': sum(r['size'] for r in records),
           'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
           'items': records}
    inv_path = os.path.join(work, 'inventory.json')
    try:
        with open(inv_path, 'w', encoding='utf-8') as f:
            json.dump(inv, f, ensure_ascii=False, indent=2)
    except OSError as e:
        die("无法写入 inventory（%s）: %s" % (inv_path, e))

    # 控制台表格
    log("\n%-42s %-8s %-10s %6s %10s %9s" % ("文件", "编码", "分辨率", "FPS", "码率", "体积"))
    log("-" * 95)
    for r in records:
        log("%-42s %-8s %-10s %6.1f %10s %9s" % (
            r['name'][:42], r['vcodec'], "%dx%d" % (r['disp_w'], r['disp_h']),
            r['fps'], fmt_bitrate(r['vbitrate_kbps']), human_size(r['size'])))
    log("-" * 95)
    log("共 %d 个文件，合计 %s。inventory 已写入: %s\n" % (
        len(records), human_size(inv['total_size']), inv_path))

    # 异常提示
    for r in records:
        warns = []
        if r['hdr']:
            warns.append("HDR/10bit")
        if r['interlaced']:
            warns.append("隔行")
        if r['n_audio'] > 1:
            warns.append("多音轨(%d)" % r['n_audio'])
        if r['n_video'] > 1:
            warns.append("多视频轨(%d)" % r['n_video'])
        if abs(r['rotation']) in (90, 270):
            warns.append("带旋转元数据")
        if r['bpp'] and r['bpp'] < 0.045:
            warns.append("源码率已较低，压缩收益有限")
        if warns:
            log("  [注意] %-40s %s" % (r['name'][:40], ", ".join(warns)))
    return EXIT_OK


# ---------------------------------------------------------------- plan
def decide_skip(cfg, rec):
    codecs = [c.lower() for c in cfg.get('skip_efficient_codecs', [])]
    thr = to_int(cfg.get('skip_bitrate_kbps'), 0)
    if codecs and rec['vcodec'].lower() in codecs and thr and rec['vbitrate_kbps'] and rec['vbitrate_kbps'] < thr:
        return "已是高效编码(%s)且码率仅 %s" % (rec['vcodec'], fmt_bitrate(rec['vbitrate_kbps']))
    return None


def cmd_plan(args):
    cfg = load_config(args.config)
    tools = tools_from_cfg(cfg)
    prof_name, profile = pick_profile(cfg, args.profile)
    res = resolve_resolution(cfg, args)

    mode = cfg.get('output_mode', 'samedir')
    outdir = os.path.abspath(args.out or cfg.get('output_dir') or '')
    out_root = os.path.join(outdir, prof_name) if (outdir and mode != 'samedir') else ''
    ws = resolve_workspace(cfg, args.work, out_root)
    work = data_dir_of(ws)
    inv = load_inventory(work)

    items = []
    for rec in inv['items']:
        out_path = output_path_for(cfg, prof_name, profile, rec)
        skip = decide_skip(cfg, rec)
        hw = hw_candidates(cfg, profile, res, rec, args)[0]
        item = {
            'name': rec['name'], 'in': rec['path'], 'out': out_path,
            'src': {'vcodec': rec['vcodec'], 'res': "%dx%d" % (rec['disp_w'], rec['disp_h']),
                    'fps': rec['fps'], 'vbitrate_kbps': rec['vbitrate_kbps'],
                    'size': rec['size'], 'duration': rec['duration']},
            'skip': skip,
            'vf': build_vf_cuda(res, rec, profile.get('pix_fmt')) if hw == 'cuda-full' else build_vf(res, rec),
            'hw': describe_hw(hw),
            'cmd': None if skip else build_encode_cmd(tools, profile, res, rec, part_path_for(out_path), hw=hw),
        }
        items.append(item)

    plan = {'profile': prof_name, 'output_dir': out_root, 'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
            'items': items}
    try:
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, 'plan.json'), 'w', encoding='utf-8') as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
    except OSError as e:
        die("无法写入 plan.json（%s）: %s" % (work, e))

    log("方案 profile = %s" % prof_name)
    log("说明: %s" % profile.get('description', ''))
    log("分辨率: %s" % describe_resolution(res))
    if mode == 'samedir':
        log("输出方式: 写入源文件同目录，命名加 _%s 后缀（先写 .part 中间文件，成功后改名）\n"
            % (profile.get('tag') or prof_name))
    else:
        log("输出目录: %s\n" % out_root)
    for it in items:
        s = it['src']
        if it['skip']:
            log("[跳过] %s  —— %s" % (it['name'], it['skip']))
            continue
        log("[编码] %s  %s %s %.1ffps %s" % (it['name'], s['vcodec'], s['res'], s['fps'], fmt_bitrate(s['vbitrate_kbps'])))
        log("       加速: %s" % it['hw'])
        log("       vf: %s" % (it['vf'] or "(无)"))
    log("\nplan 已写入: %s" % os.path.join(work, 'plan.json'))
    log("如需审阅完整 ffmpeg 命令: python vpipe.py encode --dry-run\n")
    return EXIT_OK


# ---------------------------------------------------------------- encode
def _watchdog_kill(proc):
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def run_encode(tools, cmd, duration, logfile):
    """执行 ffmpeg，解析 -progress 输出显示进度。
    返回 (returncode, err_tail)。具备：Job Object 绑定、看门狗超时、日志降级。"""
    global CURRENT_PROC
    errf = None
    try:
        errf = open(logfile, 'w', encoding='utf-8')
    except OSError:
        errf = None
    stderr_target = errf if errf is not None else subprocess.DEVNULL
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_target,
                            bufsize=1, universal_newlines=True,
                            encoding='utf-8', errors='replace')
    CURRENT_PROC = proc
    _assign_job(proc)

    limit = max(600.0, (duration or 0) * 12.0 + 300.0)
    timer = threading.Timer(limit, _watchdog_kill, args=[proc])
    timer.daemon = True
    timer.start()

    last = 0.0
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith('out_time_us=') or line.startswith('out_time_ms='):
                us = to_float(line.split('=', 1)[1])
                if line.startswith('out_time_ms='):
                    us *= 1000.0  # out_time_ms 实际是微秒
                if duration > 0:
                    pct = min(99.9, us / (duration * 1e6) * 100.0)
                    now = time.time()
                    if now - last > 0.5:
                        sys.stdout.write("\r    进度 %5.1f%%  (%.0f/%.0f 秒)" % (pct, us / 1e6, duration))
                        sys.stdout.flush()
                        last = now
            elif line == 'progress=end':
                sys.stdout.write("\r    进度 100%" + " " * 20)
                sys.stdout.flush()
    finally:
        try:
            proc.wait()
        except Exception:
            pass
        timer.cancel()
        CURRENT_PROC = None
        if errf is not None:
            errf.close()
    tail = ""
    if errf is not None:
        try:
            with open(logfile, 'r', encoding='utf-8', errors='replace') as f:
                tail = "".join(f.readlines()[-12:])
        except OSError:
            tail = ""
    return proc.returncode, tail


def run_encode_with_fallback(tools, profile, res, rec, part, duration, logfile, cands, gpu_state):
    """按候选加速模式依次尝试（GPU 优先，失败自动降级），末位为纯软件兜底。
    返回 (returncode, err_tail, 实际使用的模式)。
    * 每次尝试前清掉上一次留下的同名中间文件。
    * 若失败命中「GPU 整体不可用」关键字，则本次运行内停用后续 GPU 候选。"""
    last_rc, last_tail = None, ''
    for i, mode in enumerate(cands):
        if CANCELLED:
            break
        _remove_quiet(part)
        if i > 0:
            log("    改用%s重试 ..." % describe_hw(mode))
        cmd = build_encode_cmd(tools, profile, res, rec, part, hw=mode)
        last_rc, last_tail = run_encode(tools, cmd, duration, logfile)
        if last_rc == 0:
            if mode is not None or i == 0:
                log("    加速: %s" % describe_hw(mode))
            else:
                log("    加速: 纯软件（GPU 尝试失败后回退）")
            return last_rc, last_tail, mode
        if mode is not None and gpu_state is not None and gpu_state.get('gpu'):
            mk = gpu_fatal_find(last_tail)
            if mk:
                gpu_state['gpu'] = False
                log("    提示: 检测到 GPU/CUDA 不可用（%s），本次运行后续文件改用纯软件路径。" % mk)
                break
    return last_rc, last_tail, None


def cmd_encode(args):
    cfg = load_config(args.config)
    tools = tools_from_cfg(cfg)
    prof_name, profile = pick_profile(cfg, args.profile)
    res = resolve_resolution(cfg, args)

    mode = cfg.get('output_mode', 'samedir')
    out_root = ''
    if mode != 'samedir':
        outdir = os.path.abspath(args.out or cfg.get('output_dir') or '')
        if not outdir:
            die("未配置 output_dir")
        out_root = os.path.join(outdir, prof_name)
    ws = resolve_workspace(cfg, args.work, out_root)
    work = data_dir_of(ws)
    inv = load_inventory(work)

    src_dir = os.path.abspath(inv['input_dir'])
    log("GPU 加速: %s" % ("启用（优先 NVDEC/CUDA，失败自动降级）" if gpu_enabled(cfg, args) else "已关闭（纯软件）"))
    if mode == 'samedir':
        log("输出方式: 写入源文件同目录，命名加 _%s 后缀（源文件不会被改动）" % (profile.get('tag') or prof_name))
        log("分辨率: %s" % describe_resolution(res))
    else:
        # 安全：输出目录不得等于或包含源目录（避免误覆盖）
        if os.path.normcase(out_root) == os.path.normcase(src_dir) or \
           os.path.normcase(src_dir).startswith(os.path.normcase(out_root) + os.sep):
            die("输出目录与源目录冲突，拒绝执行以防覆盖源文件: " + out_root)
        try:
            os.makedirs(out_root, exist_ok=True)
        except OSError as e:
            die("无法创建输出目录 %s: %s" % (out_root, e))
    log("工作区: %s（报告与审阅入口在根目录，日志/json 在 .data）" % ws)

    only = set(args.only) if args.only else None
    todo = [r for r in inv['items'] if (only is None or r['name'] in only)]
    if args.limit:
        todo = todo[:args.limit]

    st = load_state(work, prof_name)
    st['out_dir'] = out_root or ws

    # dry-run：仅打印命令
    if args.dry_run:
        for rec in todo:
            out_path = output_path_for(cfg, prof_name, profile, rec)
            skip = decide_skip(cfg, rec)
            if skip:
                log("[跳过] %s —— %s" % (rec['name'], skip))
                continue
            log("### " + rec['name'])
            cands = hw_candidates(cfg, profile, res, rec, args)
            log("  加速: %s" % describe_hw(cands[0]))
            log("  " + subprocess.list2cmdline(
                build_encode_cmd(tools, profile, res, rec, part_path_for(out_path), hw=cands[0])))
        log("\n(dry-run，未执行任何编码；命令写的是 .part 中间文件，成功后自动改名为最终文件)")
        return EXIT_OK

    try:
        os.makedirs(work, exist_ok=True)
    except OSError as e:
        die("无法创建工作目录 %s: %s" % (work, e))

    done = err = skipped = 0
    interrupted = False
    gpu_state = {'gpu': True}   # 本次运行内是否仍可尝试 GPU（命中全局性 GPU 故障后置为 False）
    rv_keys = filtered_keys(work) if (review_enabled(cfg) and not args.include_filtered) else set()
    t0 = time.time()
    for i, rec in enumerate(todo, 1):
        if CANCELLED:
            interrupted = True
            break
        out_path = output_path_for(cfg, prof_name, profile, rec)
        part = part_path_for(out_path)
        log("\n[%d/%d] %s" % (i, len(todo), rec['name']))

        if rv_keys and is_review_filtered(rec['path'], rv_keys):
            log("    跳过：审阅中已标记为过滤（--include-filtered 可强制处理）")
            st['items'][rec['name']] = {'in': rec['path'], 'out': None, 'status': 'skipped',
                                        'reason': '审阅过滤', 'size_in': rec['size']}
            skipped += 1
            save_state(work, prof_name, st)
            continue

        skip = decide_skip(cfg, rec)
        if skip and not args.force:
            log("    跳过：%s" % skip)
            st['items'][rec['name']] = {'in': rec['path'], 'out': None, 'status': 'skipped',
                                        'reason': skip, 'size_in': rec['size']}
            skipped += 1
            save_state(work, prof_name, st)
            continue

        # 断点续跑：已存在且时长接近则跳过
        if os.path.isfile(out_path) and not args.force:
            od = probe(tools['ffprobe'], out_path)
            odur = to_float((od or {}).get('format', {}).get('duration'))
            if od and (rec['duration'] <= 0 or abs(odur - rec['duration']) <= max(2.0, rec['duration'] * 0.02)):
                log("    已存在，跳过（如需重编加 --force）")
                st['items'][rec['name']] = {'in': rec['path'], 'out': out_path, 'status': 'exists',
                                            'size_in': rec['size'], 'size_out': os.path.getsize(out_path)}
                done += 1
                save_state(work, prof_name, st)
                continue

        logfile = os.path.join(work, 'log_%s_%s.txt' % (prof_name, os.path.splitext(rec['name'])[0]))
        cands = hw_candidates(cfg, profile, res, rec, args, gpu_state['gpu'])
        tstart = time.time()
        rc, tail, _hw = run_encode_with_fallback(tools, profile, res, rec, part, rec['duration'],
                                                 logfile, cands, gpu_state)
        dt = time.time() - tstart

        if CANCELLED:
            _remove_quiet(part)
            interrupted = True
            break

        if rc == 0 and os.path.isfile(part) and os.path.getsize(part) > 0:
            if not finalize_output(part, out_path):
                st['items'][rec['name']] = {'in': rec['path'], 'out': None, 'status': 'error',
                                            'size_in': rec['size'], 'returncode': rc, 'log': logfile}
                err += 1
                save_state(work, prof_name, st)
                continue
            so = os.path.getsize(out_path)
            ratio = so / rec['size'] * 100 if rec['size'] else 0
            log("\r    完成  %s -> %s (%.1f%%)  用时 %s" % (
                human_size(rec['size']), human_size(so), ratio, human_time(dt)))
            st['items'][rec['name']] = {'in': rec['path'], 'out': out_path, 'status': 'ok',
                                        'size_in': rec['size'], 'size_out': so,
                                        'duration': rec['duration'], 'elapsed': round(dt, 1)}
            done += 1
        else:
            _remove_quiet(part)
            log("\r    失败 (returncode=%s)" % rc)
            if tail:
                log("    最后输出:\n" + "\n".join("      " + l for l in tail.strip().splitlines()))
            st['items'][rec['name']] = {'in': rec['path'], 'out': out_path, 'status': 'error',
                                        'size_in': rec['size'], 'returncode': rc, 'log': logfile}
            err += 1
        save_state(work, prof_name, st)

    save_state(work, prof_name, st)
    if review_enabled(cfg):
        tag = profile.get('tag') or prof_name
        pairs = [(v.get('in'), v.get('out'), tag) for v in st['items'].values()
                 if v.get('status') in ('ok', 'exists') and v.get('out')]
        mark_review_pending(work, pairs, ws)
        bat = write_review_shortcut(ws)
        if pairs and bat:
            log("审阅入口: %s（可反复双击，逐项对比后处理）" % bat)
    if interrupted:
        log("\n==== 已中断：成功 %d，跳过 %d，失败 %d ====" % (done, skipped, err))
        sys.exit(EXIT_INTERRUPTED)

    log("\n==== 完成：成功 %d，跳过 %d，失败 %d，总用时 %s ====" % (done, skipped, err, human_time(time.time() - t0)))
    if out_root:
        log("输出目录: " + out_root)
    return EXIT_FAIL if err else EXIT_OK


# ---------------------------------------------------------------- 画质度量
QUALITY_TIMEOUT = 300


def measure_quality(tools, comp, ref, start, dur, fps=30):
    """对同一时间窗，把原片缩放对齐到压缩片分辨率后计算 PSNR/SSIM。"""
    filt = ("[1:v][0:v]scale2ref[o][c];"
            "[o]fps=%d,format=yuv420p,split[oa][ob];"
            "[c]fps=%d,format=yuv420p,split[ca][cb];"
            "[oa][ca]psnr;[ob][cb]ssim" % (fps, fps))
    cmd = [tools['ffmpeg'], '-hide_banner', '-nostats',
           '-ss', str(start), '-t', str(dur), '-i', comp,
           '-ss', str(start), '-t', str(dur), '-i', ref,
           '-lavfi', filt, '-f', 'null', '-']
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=QUALITY_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    err = p.stderr.decode('utf-8', 'replace')
    psnr = ssim = None
    m = re.search(r'PSNR.*?average:\s*([0-9.]+|inf)', err, re.S)
    if m:
        psnr = m.group(1)
    m = re.search(r'SSIM.*?All:\s*([0-9.]+)', err, re.S)
    if m:
        ssim = m.group(1)
    return psnr, ssim


def cmd_report(args):
    cfg = load_config(args.config)
    tools = tools_from_cfg(cfg)
    prof_name, _profile = pick_profile(cfg, args.profile)
    out_root = _profile_out_root(cfg, prof_name)
    ws = resolve_workspace(cfg, args.work, out_root)
    work = data_dir_of(ws)
    st = load_state(work, prof_name)
    if not st['items']:
        die("尚无 %s 的编码结果，请先运行 encode" % prof_name)

    rows = []
    for name, it in st['items'].items():
        if it.get('status') not in ('ok', 'exists'):
            continue
        out_path = it['out']
        if not out_path or not os.path.isfile(out_path):
            continue
        if args.quality:
            log("  画质度量: %s ..." % name)
        rows.append(build_row(tools, name, it['in'], out_path, it['size_in'],
                              it.get('size_out'), it.get('duration') or 0,
                              quality=args.quality, seg=args.quality_seg))
    emit_report(ws, prof_name, rows)
    return EXIT_OK


def write_report_html(path, prof_name, rows, tot_in, tot_out):
    def e(x):
        return html.escape(str(x))
    trs = []
    for r in rows:
        trs.append(
            "<tr><td class='nm'>{n}</td><td>{si}</td><td>{so}</td>"
            "<td class='pct'>{sp:.1f}%</td><td>{vi}</td><td>{vo}</td>"
            "<td>{ri}</td><td>{ro}</td><td>{ss}</td><td>{ps}</td></tr>".format(
                n=e(r['name'][:60]), si=e(human_size(r['size_in'])), so=e(human_size(r['size_out'])),
                sp=r['save_pct'], vi=e(fmt_bitrate(r['vb_in'])), vo=e(fmt_bitrate(r['vb_out'])),
                ri=e(r.get('res_in', '')), ro=e(r.get('res_out', '')),
                ss=e(r.get('ssim', '')), ps=e(r.get('psnr', ''))))
    doc = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>压缩对比报告 - {prof}</title>
<style>
 body{{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#0f1115;color:#e6e6e6;margin:0;padding:32px}}
 h1{{font-size:22px;margin:0 0 6px}} .sub{{color:#9aa0a6;font-size:13px;margin-bottom:20px}}
 .cards{{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
 .card{{background:#181b21;border:1px solid #2a2f38;border-radius:12px;padding:16px 20px;min-width:150px}}
 .card .k{{font-size:12px;color:#9aa0a6}} .card .v{{font-size:22px;font-weight:600;margin-top:6px}}
 .v.green{{color:#4ade80}}
 table{{width:100%;border-collapse:collapse;background:#181b21;border-radius:12px;overflow:hidden;font-size:13px}}
 th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid #262b33;white-space:nowrap}}
 th{{background:#20242c;color:#c7ccd4;position:sticky;top:0}}
 td.nm{{max-width:340px;overflow:hidden;text-overflow:ellipsis;color:#fff}}
 td.pct{{color:#4ade80;font-weight:600}}
 tr:hover td{{background:#1d2128}}
</style></head><body>
<h1>视频压缩对比报告</h1>
<div class="sub">profile: {prof} &nbsp;|&nbsp; 生成时间: {ts} &nbsp;|&nbsp; 共 {n} 个文件（左侧为原片，压缩片同名于输出目录，便于逐个人工对比）</div>
<div class="cards">
 <div class="card"><div class="k">原片合计</div><div class="v">{ti}</div></div>
 <div class="card"><div class="k">压缩后合计</div><div class="v">{to}</div></div>
 <div class="card"><div class="k">总体节省</div><div class="v green">{tot:.1f}%</div></div>
</div>
<table><thead><tr>
<th>文件</th><th>原大小</th><th>压缩后</th><th>节省</th><th>原码率</th><th>新码率</th><th>原分辨率</th><th>新分辨率</th><th>SSIM</th><th>PSNR</th>
</tr></thead><tbody>
{rows}
</tbody></table>
</body></html>""".format(
        prof=e(prof_name), ts=e(time.strftime('%Y-%m-%d %H:%M:%S')), n=len(rows),
        ti=e(human_size(tot_in)), to=e(human_size(tot_out)),
        tot=(1 - tot_out / tot_in) * 100 if tot_in else 0, rows="\n".join(trs))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(doc)


# ---------------------------------------------------------------- compare
def cmd_compare(args):
    cfg = load_config(args.config)
    tools = tools_from_cfg(cfg)
    prof_name, _profile = pick_profile(cfg, args.profile)
    out_root = _profile_out_root(cfg, prof_name)
    ws = resolve_workspace(cfg, args.work, out_root)
    work = data_dir_of(ws)
    st = load_state(work, prof_name)
    if not st['items']:
        die("尚无编码结果，请先运行 encode")

    names = args.only if args.only else [n for n, it in st['items'].items() if it.get('status') in ('ok', 'exists')]
    pairs = []
    for name in names:
        it = st['items'].get(name)
        if it and it.get('out') and os.path.isfile(it['out']):
            pairs.append((name, it['in'], it['out']))
        else:
            log("[跳过] %s 无输出" % name)
    cmp_dir = os.path.join(work, 'compare')
    run_compare(tools, pairs, cmp_dir, args.mode, args.start, args.dur)
    log("\n对比产物目录: " + cmp_dir)
    log("提示: 左半屏=原片，右半屏=压缩片，同一时间点，可直接肉眼比对。")

    if args.play and pairs:
        name, orig, comp = pairs[0]
        if os.path.isfile(tools.get('ffplay', '')):
            subprocess.Popen([tools['ffplay'], '-i', orig])
            subprocess.Popen([tools['ffplay'], '-i', comp])
    return EXIT_OK


# ---------------------------------------------------------------- drag
def merge_results_into_state(work, prof_name, results, out_dir=None):
    """把 drag 的结果合并进 state，使 report / compare 对拖拽产物同样可用。"""
    if not results:
        return
    st = load_state(work, prof_name)
    if out_dir:
        st['out_dir'] = out_dir
    for r in results:
        name = r['name']
        if r['status'] in ('ok', 'exists'):
            entry = {'in': r['in'], 'out': r['out'], 'status': r['status'],
                     'size_in': r.get('size_in'), 'size_out': r.get('size_out')}
            if r.get('duration') is not None:
                entry['duration'] = r['duration']
            st['items'][name] = entry
        else:
            st['items'].setdefault(name, {'in': r.get('in'), 'out': None, 'status': r['status'],
                                          'size_in': r.get('size_in'), 'reason': r.get('reason')})
    save_state(work, prof_name, st)


def cmd_drag(args):
    """拖拽/命令行入口：把文件或目录直接压缩到源目录（命名加后缀），并出体积报告与审阅入口。
    每个目标目录视为一个独立工作区：报告与「审阅.bat」在其根目录，日志/json 在其 .data 下。"""
    cfg = load_config(args.config)
    tools = tools_from_cfg(cfg)
    prof_name, profile = pick_profile(cfg, args.profile)
    res = resolve_resolution(cfg, args)

    ws = args.work
    if not ws:
        ps = [os.path.abspath(p) for p in (args.paths or [])]
        if len(ps) == 1:
            ws = ps[0] if os.path.isdir(ps[0]) else os.path.dirname(ps[0])   # 拖入单个目录即该目录
        elif ps:
            ws = common_dir_of(ps)
    ws = os.path.abspath(ws or cfg.get('input_dir') or '')
    if not ws:
        die("无法确定工作区：请拖入视频文件或目录")
    work = data_dir_of(ws)
    try:
        os.makedirs(work, exist_ok=True)
    except OSError as e:
        die("无法创建工作区数据目录 %s: %s" % (work, e))

    rv_keys = filtered_keys(work) if (review_enabled(cfg) and not args.include_filtered) else set()
    files = collect_drag_files(cfg, args.paths or [], rv_keys)
    if not files:
        log("没有可处理的视频文件。")
        log("用法：把视频文件或文件夹拖到本 .bat 上运行；或 python vpipe.py drag <文件或目录> ...")
        return EXIT_OK

    log("profile = %s" % prof_name)
    log("说明: %s" % profile.get('description', ''))
    log("GPU 加速: %s" % ("启用（优先 NVDEC/CUDA，失败自动降级）" if gpu_enabled(cfg, args) else "已关闭（纯软件）"))
    log("分辨率: %s" % describe_resolution(res))
    log("输出: 写入源文件同目录，命名加 _%s 后缀（源文件不会被改动）" % (profile.get('tag') or prof_name))
    log("工作区: %s（报告与审阅入口在根目录，日志/json 在 .data）" % ws)
    log("共 %d 个待处理文件\n" % len(files))

    results = []
    interrupted = False
    gpu_state = {'gpu': True}   # 本次运行内是否仍可尝试 GPU（命中全局性 GPU 故障后置为 False）
    for i, f in enumerate(files, 1):
        if CANCELLED:
            interrupted = True
            break
        rec = make_record(tools['ffprobe'], f, os.path.basename(f))
        if rec is None:
            log("[%d/%d] [跳过] 无法解析: %s%s" % (i, len(files), os.path.basename(f), _probe_reason()))
            results.append({'name': os.path.basename(f), 'in': f, 'out': None, 'status': 'error',
                            'size_in': 0, 'reason': LAST_PROBE_ERR or '无法解析'})
            continue
        out_path = output_path_for(cfg, prof_name, profile, rec)
        part = part_path_for(out_path)
        if os.path.normcase(os.path.abspath(out_path)) == os.path.normcase(os.path.abspath(rec['path'])):
            log("[%d/%d] [跳过] 输出与源同名，拒绝覆盖: %s" % (i, len(files), rec['name']))
            continue
        log("\n[%d/%d] %s" % (i, len(files), rec['name']))
        log("    源: %s %dx%d %.1ffps %s" % (rec['vcodec'], rec['disp_w'], rec['disp_h'],
                                             rec['fps'], fmt_bitrate(rec['vbitrate_kbps'])))

        skip = decide_skip(cfg, rec)
        if skip and not args.force:
            log("    跳过：%s" % skip)
            results.append({'name': rec['name'], 'in': rec['path'], 'out': out_path,
                            'status': 'skipped', 'size_in': rec['size'], 'duration': rec['duration'],
                            'reason': skip})
            continue

        if os.path.isfile(out_path) and not args.force:
            od = probe(tools['ffprobe'], out_path)
            odur = to_float((od or {}).get('format', {}).get('duration'))
            if od and (rec['duration'] <= 0 or abs(odur - rec['duration']) <= max(2.0, rec['duration'] * 0.02)):
                log("    已存在，跳过（--force 可强制重编）")
                results.append({'name': rec['name'], 'in': rec['path'], 'out': out_path,
                                'status': 'exists', 'size_in': rec['size'],
                                'size_out': os.path.getsize(out_path), 'duration': rec['duration']})
                continue

        logfile = os.path.join(work, 'log_%s_%s.txt' % (prof_name, os.path.splitext(rec['name'])[0]))
        cands = hw_candidates(cfg, profile, res, rec, args, gpu_state['gpu'])
        tstart = time.time()
        rc, tail, _hw = run_encode_with_fallback(tools, profile, res, rec, part, rec['duration'],
                                                 logfile, cands, gpu_state)
        dt = time.time() - tstart

        if CANCELLED:
            _remove_quiet(part)
            interrupted = True
            break

        if rc == 0 and os.path.isfile(part) and os.path.getsize(part) > 0:
            if not finalize_output(part, out_path):
                results.append({'name': rec['name'], 'in': rec['path'], 'out': None,
                                'status': 'error', 'size_in': rec['size'],
                                'reason': '无法写入最终文件'})
                continue
            so = os.path.getsize(out_path)
            log("\r    完成  %s -> %s (%.1f%%)  用时 %s" % (
                human_size(rec['size']), human_size(so),
                (so / rec['size'] * 100) if rec['size'] else 0, human_time(dt)))
            results.append({'name': rec['name'], 'in': rec['path'], 'out': out_path,
                            'status': 'ok', 'size_in': rec['size'], 'size_out': so,
                            'duration': rec['duration']})
        else:
            _remove_quiet(part)
            log("\r    失败 (returncode=%s)" % rc)
            if tail:
                log("    最后输出:\n" + "\n".join("      " + l for l in tail.strip().splitlines()))
            results.append({'name': rec['name'], 'in': rec['path'], 'out': None,
                            'status': 'error', 'size_in': rec['size'],
                            'reason': 'ffmpeg 退出码 %s' % rc})

    ok = [r for r in results if r['status'] in ('ok', 'exists')]
    err = [r for r in results if r['status'] == 'error']
    out_dir = os.path.dirname(ok[0]['out']) if ok else None
    merge_results_into_state(work, prof_name, results, out_dir)

    if review_enabled(cfg):
        tag = profile.get('tag') or prof_name
        pairs = [(r['in'], r['out'], tag) for r in ok]
        mark_review_pending(work, pairs, ws)
        bat = write_review_shortcut(ws)
        if pairs and bat:
            log("审阅入口: %s（可反复双击，逐项对比后处理）" % bat)

    if interrupted:
        log("\n==== 已中断：成功 %d，失败 %d ====" % (len(ok), len(err)))
        sys.exit(EXIT_INTERRUPTED)

    if not ok:
        log("\n没有成功产物。")
        return EXIT_FAIL

    rows = []
    for r in ok:
        if args.quality:
            log("  画质度量: %s ..." % r['name'])
        rows.append(build_row(tools, r['name'], r['in'], r['out'], r['size_in'],
                              r.get('size_out'), r.get('duration') or 0,
                              quality=args.quality, seg=args.quality_seg))
    emit_report(ws, prof_name, rows)

    log("\n全部完成。压缩产物在源目录（文件名带 _%s 后缀），源文件未改动。"
        % (profile.get('tag') or prof_name))
    log("报告: %s" % os.path.join(ws, 'report_%s.html' % prof_name))
    if err:
        log("注意: 有 %d 个文件未能成功处理。" % len(err))
    return EXIT_FAIL if err else EXIT_OK


# ---------------------------------------------------------------- CLI
def _add_resolution_args(p):
    """给子命令加上分辨率覆盖参数（分辨率与编码档位解耦，可独立组合）。"""
    p.add_argument('--max-long-side', type=int, default=None,
                   help='分辨率：长边上限像素；0=保持源分辨率。默认取 config.resolution')
    p.add_argument('--max-fps', type=int, default=None,
                   help='帧率上限；0=保持源帧率。默认取 config.resolution')


def _add_gpu_arg(p):
    """给子命令加上 GPU 开关（默认启用，失败自动降级；--no-gpu 可强制纯软件）。"""
    p.add_argument('--no-gpu', action='store_true',
                   help='禁用 GPU 加速（NVDEC/CUDA），强制走纯软件解码与缩放')


def main():
    ensure_utf8()
    _init_job_object()
    _install_signal_handlers()
    ap = argparse.ArgumentParser(
        prog='vpipe', description='本地视频批量压缩管线（FFmpeg）',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json'))
    ap.add_argument('--work', default=None,
                    help='工作区（目标目录）：报告与审阅入口放其根目录，日志/json 放其 .data；默认按产物所在目录自动判定')
    ap.add_argument('--debug', action='store_true', help='出错时保留完整异常堆栈（用于排查）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('scan', help='扫描源目录，生成 inventory')
    p.add_argument('-i', '--input', default=None)
    p.add_argument('--include-filtered', action='store_true',
                   help='忽略审阅过滤器，强制纳入被标记为「不需要转换」的文件')
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser('plan', help='按 profile 生成处理方案')
    p.add_argument('--profile', default=None)
    p.add_argument('--out', default=None)
    _add_resolution_args(p)
    _add_gpu_arg(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser('encode', help='批量编码')
    p.add_argument('--profile', default=None)
    p.add_argument('--out', default=None)
    p.add_argument('--only', nargs='+', default=None, help='仅处理指定文件名')
    p.add_argument('--limit', type=int, default=None, help='最多处理 N 个文件')
    p.add_argument('--dry-run', action='store_true', help='只打印命令不执行')
    p.add_argument('--force', action='store_true', help='忽略断点续跑/跳过规则，强制重编')
    p.add_argument('--include-filtered', action='store_true',
                   help='忽略审阅过滤器，强制处理被标记为「不需要转换」的文件')
    _add_resolution_args(p)
    _add_gpu_arg(p)
    p.set_defaults(func=cmd_encode)

    p = sub.add_parser('report', help='生成压缩对比报告')
    p.add_argument('--profile', default=None)
    p.add_argument('--quality', action='store_true', help='额外计算 SSIM/PSNR（较慢）')
    p.add_argument('--quality-seg', type=float, default=30.0, help='画质度量取样秒数，默认 30')
    p.set_defaults(func=cmd_report)

    p = sub.add_parser('compare', help='生成人工对比产物')
    p.add_argument('--profile', default=None)
    p.add_argument('--only', nargs='+', default=None)
    p.add_argument('--mode', choices=['side', 'diff', 'frames'], default='side',
                   help='side=左右并排, diff=差异放大, frames=抽样对比图')
    p.add_argument('--start', type=float, default=None)
    p.add_argument('--dur', type=float, default=None)
    p.add_argument('--play', action='store_true', help='同时用 ffplay 打开原片与压缩片')
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser('drag', help='拖拽/命令行批量压缩（结果写入源目录，命名加后缀）')
    p.add_argument('paths', nargs='*', help='视频文件或目录（可由 .bat 拖入）')
    p.add_argument('--profile', default=None)
    p.add_argument('--force', action='store_true', help='忽略断点续跑/跳过规则，强制重编')
    p.add_argument('--quality', action='store_true', help='额外计算 SSIM/PSNR')
    p.add_argument('--quality-seg', type=float, default=30.0)
    p.add_argument('--include-filtered', action='store_true',
                   help='忽略审阅过滤器，强制处理被标记为「不需要转换」的文件')
    _add_resolution_args(p)
    _add_gpu_arg(p)
    p.set_defaults(func=cmd_drag)

    args = ap.parse_args()
    if args.debug:
        rc = args.func(args)
        sys.exit(rc or EXIT_OK)
    try:
        rc = args.func(args)
        sys.exit(rc or EXIT_OK)
    except KeyboardInterrupt:
        _terminate_current()
        sys.stderr.write("\n已中断。\n")
        sys.exit(EXIT_INTERRUPTED)
    except Exception as e:
        sys.stderr.write("错误: %s: %s\n" % (e.__class__.__name__, e))
        sys.exit(EXIT_FAIL)


if __name__ == '__main__':
    main()
