#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
compress_gui.py —— vpipe 统一压缩入口（本地单页 GUI）

职责
  * 一个入口：把视频文件/文件夹拖到「ffmpeg批量压缩.bat」上（或直接运行本脚本），
    即打开一个只监听 127.0.0.1 的简易网页；在页面上选档位与常用参数后开始压缩。
  * 复用 vpipe.py 既有管线：通过「临时 config + python vpipe.py drag」子进程执行，
    vpipe.py 本身零改动。
  * 内置几套预设，并支持用系统文件对话框按路径加载 XXX_config.json 预设/配置。

设计要点
  * 零第三方依赖（仅标准库）；服务骨架与 review.py 同构（HTTP/1.1、Origin 校验、原子写、单实例）。
  * 同一时刻只允许一个压缩任务；取消走 CTRL_BREAK_EVENT（vpipe 已把 SIGBREAK 绑为中断，退出 130），
    8 秒未退则由 Job Object 硬杀兜底，杜绝孤儿进程。
  * 进度：读取 vpipe 子进程 stdout（drag 已打印进度/条目/完成等），环形缓冲 + /api/status?since=n 轮询。

用法
  python compress_gui.py                  # 启动并打开页面（已有实例则复用）
  python compress_gui.py --open <路径...>  # 启动（或复用已运行实例）并把路径加入列表；.bat 用这个。
                                          # 首次启动会打开页面；已运行时只追加列表，页面自动刷新，不再新开
  python compress_gui.py --no-browser     # 只起服务
  python compress_gui.py --port 0         # 固定端口（0=自动）
  python compress_gui.py --stop           # 关闭已运行实例
  python compress_gui.py --debug          # 保留完整异常堆栈

退出码：0=正常；1=运行期错误；2=参数/配置错误；130=用户中断
"""
import argparse
import copy
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vpipe  # noqa: E402  复用配置读取/工具探测/日志/Job Object 等既有实现

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_NAME = 'gui_page.html'
APP_NAME = 'vpipe-compress'
PORT_NAME = 'gui.port'
VPIPE_PY = os.path.join(HERE, 'vpipe.py')
CONFIG_JSON = os.path.join(HERE, 'config.json')
REVIEW_PY = os.path.join(HERE, 'review.py')

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130

# 服务自身的运行数据统一放在脚本目录（不污染任何目标工作区）
GUI_HOME = os.path.join(HERE, '_work', vpipe.DATA_DIR_NAME)
RUN_DIR = os.path.join(GUI_HOME, 'gui', 'run')
LAST_STATE = os.path.join(GUI_HOME, 'gui', 'last_state.json')

EVENTS_MAX = 400
DEFAULT_PRESET_FILE = '默认预设_config.json'   # 脚本目录下的示例预设，启动时自动加载
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
NEW_GROUP = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)

NVENC = ('h264_nvenc', 'hevc_nvenc', 'av1_nvenc')


# ---------------------------------------------------------------- 基础工具
def now_str():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def atomic_write(path, text):
    """原子写：先写 .part，再 os.replace。失败抛 OSError 由调用方处理。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + '.part'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def load_cfg(path):
    """复用 vpipe.load_config，把它的 die() 退出转换为本脚本的错误码。"""
    try:
        return vpipe.load_config(path)
    except SystemExit:
        sys.exit(EXIT_USAGE)
    except Exception as e:                                     # noqa: BLE001
        sys.stderr.write("错误: 无法读取配置文件: %s（%s）\n" % (path, e))
        sys.exit(EXIT_USAGE)


def init_job():
    """用 vpipe 的 Job Object 能力（本脚本不与其同进程运行 vpipe，故共用句柄安全）。"""
    try:
        vpipe._init_job_object()
    except Exception:                                          # noqa: BLE001
        pass


def norm_paths(paths):
    """规范化路径列表：去重（不区分大小写）、过滤不存在的项。返回 (合法, 被拒)。"""
    ok, bad, seen = [], [], set()
    for p in paths or []:
        if not isinstance(p, str) or not p.strip():
            continue
        p = os.path.abspath(p.strip().strip('"').strip())
        k = os.path.normcase(p)
        if k in seen:
            continue
        seen.add(k)
        if os.path.exists(p):
            ok.append(p)
        else:
            bad.append(p)
    return ok, bad


def _post_json(port, path, obj):
    try:
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path),
                                     data=json.dumps(obj).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'},
                                     method='POST')
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except Exception:                                          # noqa: BLE001
        return {}


# ---------------------------------------------------------------- 预设
# 内置预设：档位 + 画质 + preset + 分辨率/帧率 的完整组合（无外部文件也可用）
BUILTIN_PRESETS = [
    {'name': '默认 HEVC（快 · 推荐）', 'description': 'NVENC HEVC，速度极快，体积大幅下降',
     'base_profile': 'hevc_nvenc', 'quality': 28, 'preset': 'p5',
     'resolution': {'max_long_side': 0, 'max_fps': 0}},
    {'name': 'HEVC 高质量归档', 'description': '更慢但更小更好，适合长期归档',
     'base_profile': 'hevc_nvenc_hq', 'quality': 24, 'preset': 'p6',
     'resolution': {'max_long_side': 0, 'max_fps': 0}},
    {'name': 'AV1 同画质最小', 'description': '同画质体积最小，需较新播放器/设备',
     'base_profile': 'av1_nvenc', 'quality': 30, 'preset': 'p6',
     'resolution': {'max_long_side': 0, 'max_fps': 0}},
    {'name': 'x265 极致（最慢最小）', 'description': '软件 x265，体积最小画质最佳',
     'base_profile': 'hevc_x265_hq', 'quality': 24, 'preset': 'medium',
     'resolution': {'max_long_side': 0, 'max_fps': 0}},
    {'name': 'H264 兼容优先', 'description': '任何设备都能播，体积下降中等',
     'base_profile': 'h264_x264', 'quality': 20, 'preset': 'slow',
     'resolution': {'max_long_side': 0, 'max_fps': 0}},
    {'name': '4K → 1080p HEVC', 'description': '长边限制 1920（小分辨率不会被放大）',
     'base_profile': 'hevc_nvenc', 'quality': 28, 'preset': 'p5',
     'resolution': {'max_long_side': 1920, 'max_fps': 0}},
    {'name': '网页友好 30fps', 'description': '限制帧率 30，适合网页/移动端',
     'base_profile': 'hevc_nvenc', 'quality': 28, 'preset': 'p5',
     'resolution': {'max_long_side': 0, 'max_fps': 30}},
]

_PRESET_KEYS = ('encoder', 'tag', 'quality', 'preset', 'vparams', 'pix_fmt',
                'audio_mode', 'audio_bitrate', 'container', 'resolution', 'flags')


def norm_preset(obj, default_base=''):
    """规范化一个预设对象；非法（缺 base_profile）返回 None。"""
    if not isinstance(obj, dict):
        return None
    name = str(obj.get('name') or '').strip()
    base = str(obj.get('base_profile') or default_base or '').strip()
    if not base:
        return None
    out = {'name': name or '未命名预设',
           'description': str(obj.get('description') or ''),
           'base_profile': base}
    for k in _PRESET_KEYS:
        v = obj.get(k)
        if k == 'vparams':
            out[k] = v if isinstance(v, list) and v else None
        elif k in ('resolution', 'flags'):
            out[k] = v if isinstance(v, dict) else {}
        elif k == 'quality':
            out[k] = v if isinstance(v, (int, float)) else None
        else:
            out[k] = v if isinstance(v, str) and v else None
    out['from_file'] = bool(obj.get('from_file'))
    return out


def load_preset_file(path):
    """读取预设/配置文件。支持三种形态：
       (a) {"presets":[...]} 库  (b) 单个预设对象  (c) 完整 vpipe 配置（含 profiles）。
    返回 {'ok':True,'kind':'presets'|'config', ...} 或 {'error':...}。"""
    if not os.path.isfile(path):
        return {'error': '文件不存在: %s' % path}
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return {'error': '预设 JSON 格式有误（第 %s 行）: %s'
                         % (getattr(e, 'lineno', '?'), getattr(e, 'msg', e))}
    except OSError as e:
        return {'error': '无法读取预设文件: %s' % e}

    if isinstance(data, dict) and isinstance(data.get('profiles'), dict) and data['profiles']:
        return {'ok': True, 'kind': 'config', 'path': path, 'config': data,
                'profiles': list(data['profiles'].keys()),
                'default_profile': data.get('default_profile')}

    if isinstance(data, dict) and isinstance(data.get('presets'), list):
        items = data['presets']
    elif isinstance(data, list):
        items = data
    else:
        items = [data]

    base = ''
    if isinstance(data, dict):
        base = str(data.get('base_profile') or '')
    out = []
    for it in items:
        n = norm_preset(it, base)
        if n:
            n['from_file'] = True
            n['path'] = path
            out.append(n)
    if not out:
        return {'error': '预设文件里没有可用预设（每条需包含 base_profile；'
                         '整份配置需包含 profiles）'}
    return {'ok': True, 'kind': 'presets', 'path': path, 'presets': out}


def absorb_config(data, base):
    """把外部完整配置吸收为可用基底：补齐 vpipe 必需的 work_dir。失败返回 None。"""
    cfg = copy.deepcopy(data)
    if not isinstance(cfg.get('work_dir'), str) or not cfg.get('work_dir'):
        cfg['work_dir'] = base.get('work_dir') or os.path.join(HERE, '_work')
    if not isinstance(cfg.get('profiles'), dict) or not cfg['profiles']:
        return None
    return cfg


# ---------------------------------------------------------------- 参数覆盖
def _set_flag(vp, keys, val, after=()):
    """在 vparams 里定位并改写 keys[0] 的值；不存在则插入到 after 参数之后。"""
    for k in keys:
        if k in vp:
            i = vp.index(k)
            if i + 1 < len(vp):
                vp[i + 1] = val
            else:
                vp.append(val)
            return vp
    pos = len(vp)
    for a in after:
        if a in vp:
            pos = max(pos, vp.index(a) + 2)
    pos = min(pos, len(vp))
    vp[pos:pos] = [keys[0], val]
    return vp


def patch_vparams(vp, encoder, quality, preset):
    """按档位编码器改写画质（-cq/-crf/-qp）与速度（-preset）。"""
    out = list(vp or [])
    if quality is not None:
        try:
            qv = str(int(float(quality)))
        except (TypeError, ValueError):
            qv = None
        if qv is not None:
            if encoder in NVENC:
                out = _set_flag(out, ['-cq'], qv, after=('-rc', '-b:v'))
            elif 'qsv' in encoder or 'amf' in encoder:
                out = _set_flag(out, ['-qp'], qv)
            else:
                out = _set_flag(out, ['-crf'], qv, after=('-preset',))
    if preset:
        out = _set_flag(out, ['-preset'], str(preset))
    return out


def build_tmp_config(base_cfg, req):
    """按 /api/start 的请求，深拷贝基底配置并只覆盖目标档位。返回 (cfg, prof_name)。"""
    cfg = copy.deepcopy(base_cfg)
    profiles = cfg.get('profiles') or {}
    name = req.get('profile') or cfg.get('default_profile') or next(iter(profiles), None)
    if not name or name not in profiles:
        raise ValueError('未知档位: %s（可选: %s）' % (name, ', '.join(profiles) or '无'))
    prof = profiles[name]
    opts = req.get('options') or {}
    for k in ('encoder', 'tag', 'pix_fmt', 'audio_mode', 'audio_bitrate', 'container'):
        v = opts.get(k)
        if isinstance(v, str) and v.strip():
            prof[k] = v.strip()
    if isinstance(opts.get('vparams'), list) and opts['vparams']:
        prof['vparams'] = [str(x) for x in opts['vparams']]
    else:
        prof['vparams'] = patch_vparams(prof.get('vparams'), prof.get('encoder') or '',
                                        opts.get('quality'), opts.get('preset'))
    return cfg, name


def build_cmd(tmp_cfg_path, prof_name, req, paths):
    """拼接 vpipe CLI。注意 --config 是全局参数，必须放在子命令 drag 之前。"""
    res = req.get('resolution') or {}
    flags = req.get('flags') or {}
    mls = vpipe.to_int(res.get('max_long_side'), 0)
    mf = vpipe.to_int(res.get('max_fps'), 0)
    cmd = [sys.executable, VPIPE_PY, '--config', tmp_cfg_path, 'drag',
           '--profile', prof_name,
           '--max-long-side', str(max(0, mls)),
           '--max-fps', str(max(0, mf))]
    if flags.get('force'):
        cmd.append('--force')
    if flags.get('include_filtered'):
        cmd.append('--include-filtered')
    if flags.get('no_gpu'):
        cmd.append('--no-gpu')
    if flags.get('quality_metrics'):
        cmd += ['--quality', '--quality-seg', str(flags.get('quality_seg') or 30)]
    cmd += list(paths)
    return cmd


# ---------------------------------------------------------------- 输出解析
P_TOTAL = re.compile(r'^共 (\d+) 个待处理文件')
P_ENTRY = re.compile(r'^\[(\d+)/(\d+)\] (.+)$')
P_PROG = re.compile(r'^进度\s+([\d.]+)%\s+\((\d+)/(\d+) 秒\)$')
P_HW = re.compile(r'^加速: (.+)$')
P_SKIP = re.compile(r'^跳过：(.+)$')
P_EXIST = re.compile(r'^已存在，跳过.*$')
P_DONE = re.compile(r'^完成\s+(.+?)\s*->\s*(.+?)\s+\(([\d.]+)%\)\s+用时\s+(.+)$')
P_FAIL = re.compile(r'^失败 \(returncode=(\d+)\)$')
P_REVIEW = re.compile(r'^审阅入口: (.+)$')
P_REPORT = re.compile(r'^报告: (.+)$')
P_END = re.compile(r'^(全部完成。|==== 已中断)')
P_NOTE = re.compile(r'^(注意: .*|提示: .*|说明: .*|GPU 加速: .*|分辨率: .*|输出: .*'
                    r'|工作区: .*|profile = .*|源: .*|改用.*|最后输出:.*)$')


def parse_line(line):
    """把 vpipe 的一行输出归类成事件；无意义的行返回 None。"""
    s = line.strip()
    if not s:
        return None
    m = P_PROG.match(s)
    if m:
        return {'kind': 'progress', 'text': s, 'pct': float(m.group(1))}
    m = P_ENTRY.match(s)
    if m:
        return {'kind': 'entry', 'text': s, 'index': int(m.group(1)),
                'total': int(m.group(2)), 'name': m.group(3)}
    m = P_TOTAL.match(s)
    if m:
        return {'kind': 'total', 'text': s, 'total': int(m.group(1))}
    m = P_DONE.match(s)
    if m:
        return {'kind': 'done', 'text': s, 'pct': float(m.group(3))}
    m = P_FAIL.match(s)
    if m:
        return {'kind': 'fail', 'text': s}
    m = P_REVIEW.match(s)
    if m:
        return {'kind': 'review', 'text': s, 'path': m.group(1).split('（')[0].strip()}
    m = P_REPORT.match(s)
    if m:
        return {'kind': 'report', 'text': s, 'path': m.group(1).strip()}
    m = P_END.match(s)
    if m:
        return {'kind': 'end', 'text': s}
    m = P_HW.match(s)
    if m:
        return {'kind': 'note', 'text': s}
    if P_SKIP.match(s) or P_EXIST.match(s):
        return {'kind': 'skip', 'text': s}
    if P_NOTE.match(s):
        return {'kind': 'note', 'text': s}
    return None


# ---------------------------------------------------------------- 任务执行
class Runner:
    """一次只跑一个压缩任务：起 vpipe 子进程、解析输出、支持取消。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.thread = None
        self.job_id = ''
        self.profile = ''
        self.paths = []
        self.tmp_cfg = ''
        self.total = 0
        self.index = 0
        self.current = ''
        self.percent = 0.0
        self.started = 0.0
        self.finished = 0.0
        self.exit = None
        self.seq = 0
        self.events = []
        self.report = ''
        self.review_bat = ''
        self.ok_count = 0
        self.fail_count = 0
        self.on_finish = None      # 任务结束时回调（由 State 注入），用于清理已处理的待处理路径

    # ---- 内部
    def _push(self, ev):
        with self.lock:
            self.seq += 1
            ev = dict(ev)
            ev['i'] = self.seq
            ev['at'] = now_str()
            self.events.append(ev)
            if len(self.events) > EVENTS_MAX:
                del self.events[:len(self.events) - EVENTS_MAX]
            return ev

    @property
    def running(self):
        p = self.proc
        return p is not None and p.poll() is None

    def _elapsed(self):
        if not self.started:
            return 0.0
        end = self.finished or time.time()
        return max(0.0, end - self.started)

    def _on_event(self, ev):
        k = ev.get('kind')
        if k == 'total':
            self.total = ev.get('total') or self.total
        elif k == 'entry':
            self.index = ev.get('index') or 0
            self.total = ev.get('total') or self.total
            self.current = ev.get('name') or ''
            self.percent = 0.0
        elif k == 'progress':
            self.percent = ev.get('pct') or 0.0
        elif k == 'report':
            self.report = ev.get('path') or self.report
        elif k == 'review':
            self.review_bat = ev.get('path') or self.review_bat
        elif k == 'done':
            self.ok_count += 1

    def _safe_remove(self, path):
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    # ---- 对外
    def start(self, base_cfg, req):
        """返回 (ok, 信息)。失败时信息为中文原因。"""
        with self.lock:
            if self.running:
                return False, '已有任务正在运行，请先等待完成或取消'
        paths = req.get('paths') or []
        if not paths:
            return False, '没有可处理的路径，请先拖入视频或文件夹'
        ok_paths, bad = norm_paths(paths)
        if not ok_paths:
            return False, '路径不存在: %s' % '；'.join(bad or ['(空)'])
        try:
            cfg, prof_name = build_tmp_config(base_cfg, req)
        except ValueError as e:
            return False, str(e)

        job_id = time.strftime('%Y%m%d_%H%M%S')
        tmp = os.path.join(RUN_DIR, '%s.json' % job_id)
        try:
            atomic_write(tmp, json.dumps(cfg, ensure_ascii=False, indent=2))
        except OSError:
            # 脚本目录不可写时，回落到首个路径所在工作区的 .data 下
            first = ok_paths[0]
            ws = first if os.path.isdir(first) else os.path.dirname(first)
            tmp = os.path.join(vpipe.data_dir_of(ws), 'gui', 'run', '%s.json' % job_id)
            try:
                atomic_write(tmp, json.dumps(cfg, ensure_ascii=False, indent=2))
            except OSError as e:
                return False, '无法写入临时配置: %s' % e

        cmd = build_cmd(tmp, prof_name, req, ok_paths)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, bufsize=1, universal_newlines=True,
                encoding='utf-8', errors='replace', cwd=HERE,
                creationflags=NEW_GROUP)
        except OSError as e:
            self._safe_remove(tmp)
            return False, '无法启动压缩任务: %s' % e

        try:
            vpipe._assign_job(proc)     # 父进程消失时连带终止（vpipe 自身的 Job 再管住 ffmpeg）
        except Exception:               # noqa: BLE001
            pass

        with self.lock:
            self.proc = proc
            self.job_id = job_id
            self.profile = prof_name
            self.paths = list(ok_paths)
            self.tmp_cfg = tmp
            self.total = 0
            self.index = 0
            self.current = ''
            self.percent = 0.0
            self.started = time.time()
            self.finished = 0.0
            self.exit = None
            self.seq = 0
            self.events = []
            self.report = ''
            self.review_bat = ''
            self.ok_count = 0
            self.fail_count = 0
        self.thread = threading.Thread(target=self._reader, args=(proc, tmp), daemon=True)
        self.thread.start()
        self._push({'kind': 'start', 'text': '开始压缩：档位 %s，共 %d 个路径'
                                              % (prof_name, len(ok_paths))})
        return True, job_id

    def _reader(self, proc, tmp):
        try:
            for raw in proc.stdout:
                ev = parse_line(raw)
                if ev:
                    self._push(ev)
                    self._on_event(ev)
        except Exception as e:                                 # noqa: BLE001
            self._push({'kind': 'note', 'text': '读取输出异常: %s' % e})
        finally:
            try:
                proc.wait()
            except Exception:                                  # noqa: BLE001
                pass
            code = proc.returncode
            with self.lock:
                self.finished = time.time()
                self.exit = code
                self.percent = 100.0
                tmp_cfg = self.tmp_cfg
            self._push({'kind': 'end', 'text': '任务结束（退出码 %s）' % code,
                        'exit': code})
            self._safe_remove(tmp_cfg)
            self._save_last()
            cb = self.on_finish
            if cb:
                try:
                    cb(list(self.paths))      # 通知 State：本批路径已处理完
                except Exception:              # noqa: BLE001
                    pass

    def _save_last(self):
        try:
            atomic_write(LAST_STATE, json.dumps({
                'job_id': self.job_id, 'profile': self.profile,
                'report': self.report, 'review_bat': self.review_bat,
                'exit': self.exit, 'at': now_str(),
            }, ensure_ascii=False, indent=2))
        except OSError:
            pass

    def cancel(self):
        p = self.proc
        if p is None or p.poll() is not None:
            return False, '当前没有正在运行的任务'
        self._push({'kind': 'note', 'text': '正在取消…'})
        if os.name == 'nt':
            try:
                p.send_signal(getattr(__import__('signal'), 'CTRL_BREAK_EVENT'))
            except Exception:                                  # noqa: BLE001
                try:
                    p.terminate()
                except Exception:                              # noqa: BLE001
                    pass
        else:
            try:
                p.terminate()
            except Exception:                                  # noqa: BLE001
                pass
        try:
            p.wait(timeout=8)
            return True, '已取消'
        except Exception:                                      # noqa: BLE001
            try:
                p.kill()          # Job Object 会连带终止其 ffmpeg 子进程
            except Exception:                                  # noqa: BLE001
                pass
            return True, '已强制终止'

    def status(self, since, job=''):
        with self.lock:
            first = self.events[0]['i'] if self.events else self.seq + 1
            # 需要客户端清空日志并重置游标的三种情况：
            #   1) job 与上一轮不同（换了任务）；
            #   2) since 超过当前 seq（新任务 seq 已归零，旧游标越界）；
            #   3) since 落后于环形缓冲首位（旧事件已被淘汰）。
            stale = (job != (self.job_id or '')) or (since > self.seq) or (since < first - 1)
            evs = list(self.events) if stale else [e for e in self.events if e['i'] > since]
            return {
                'seq': self.seq,
                'reset': stale,
                'running': self.running,
                'job': {'id': self.job_id, 'profile': self.profile,
                        'total': self.total, 'index': self.index,
                        'current': self.current, 'percent': round(self.percent, 1),
                        'elapsed': round(self._elapsed(), 1),
                        'count': len(self.paths)},
                'events': evs,
                'result': {'exit': self.exit, 'report': self.report,
                           'review_bat': self.review_bat,
                           'ok_count': self.ok_count, 'fail_count': self.fail_count},
            }


# ---------------------------------------------------------------- 服务状态
class State:
    def __init__(self, cfg, cfg_path):
        self.lock = threading.Lock()
        self.base_cfg = cfg
        self.base_path = cfg_path
        self.paths = []
        self.last_ws = ''            # 最近一次任务的工作区，用于「打开审阅」定位
        self.file_presets = []
        self.file_preset_path = ''
        self.runner = Runner()
        self.runner.on_finish = self._on_runner_finish   # 任务结束后清理已处理的待处理路径
        self.httpd = None
        # 启动时自动加载脚本目录下的示例预设文件（若有）
        p = os.path.join(HERE, DEFAULT_PRESET_FILE)
        if os.path.isfile(p):
            r = load_preset_file(p)
            if r.get('ok') and r.get('kind') == 'presets':
                self.file_presets = r['presets']
                self.file_preset_path = p

    def add_paths(self, paths):
        ok, bad = norm_paths(paths)
        with self.lock:
            exist = set(os.path.normcase(p) for p in self.paths)
            added = [p for p in ok if os.path.normcase(p) not in exist]
            self.paths.extend(added)
        return added, bad

    def remove_path(self, path):
        k = os.path.normcase(os.path.abspath(path or ''))
        with self.lock:
            self.paths = [p for p in self.paths if os.path.normcase(p) != k]
        return True

    def clear_paths(self):
        with self.lock:
            self.paths = []
        return True

    def first_workspace(self):
        """「打开审阅」使用的工作区：优先待处理列表首项；列表已清空时回退到最近一次任务的工作区。
        目录本身即工作区；文件取其所在目录。"""
        with self.lock:
            first = self.paths[0] if self.paths else self.last_ws
        if not first:
            return ''
        return first if os.path.isdir(first) else os.path.dirname(first)

    def _on_runner_finish(self, batch):
        """任务结束回调：把本批已处理的路径从待处理列表移除，并记录工作区。
        避免「拖入新目录后仍在旧路径上重复启动」，也让「打开审阅」指向最近一次任务。"""
        with self.lock:
            keys = set(os.path.normcase(os.path.abspath(p)) for p in (batch or []))
            if keys:
                self.paths = [p for p in self.paths
                              if os.path.normcase(os.path.abspath(p)) not in keys]
            if batch:
                b0 = batch[0]
                self.last_ws = b0 if os.path.isdir(b0) else os.path.dirname(b0)

    def set_base_config(self, data, path):
        cfg = absorb_config(data, self.base_cfg)
        if cfg is None:
            return False
        with self.lock:
            self.base_cfg = cfg
            self.base_path = path
        return True

    def payload(self):
        cfg = self.base_cfg
        profiles = []
        for name, p in (cfg.get('profiles') or {}).items():
            profiles.append({
                'name': name,
                'tag': p.get('tag') or name,
                'description': p.get('description') or '',
                'encoder': p.get('encoder') or '',
                'vparams': p.get('vparams') or [],
                'pix_fmt': p.get('pix_fmt') or 'yuv420p',
                'audio_mode': p.get('audio_mode') or 'copy',
                'audio_bitrate': p.get('audio_bitrate') or '160k',
                'container': p.get('container') or 'mp4',
            })
        presets = list(BUILTIN_PRESETS)
        presets += [dict(x, from_file=True, path=self.file_preset_path)
                    for x in self.file_presets]
        with self.lock:
            paths = list(self.paths)
        return {
            'config_path': self.base_path,
            'default_profile': cfg.get('default_profile') or (profiles[0]['name'] if profiles else ''),
            'profiles': profiles,
            'presets': presets,
            'resolution': cfg.get('resolution') or {},
            'gpu': bool((cfg.get('gpu') or {}).get('enabled', True)),
            'review_enabled': bool(cfg.get('review_enabled', True)),
            'preset_file': self.file_preset_path,
            'paths': paths,
        }


# ---------------------------------------------------------------- 文件对话框
TK_PICK = (
    "import sys\n"
    "try:\n"
    "    import tkinter as tk\n"
    "    from tkinter import filedialog\n"
    "except Exception:\n"
    "    sys.exit(3)\n"
    "root = tk.Tk()\n"
    "root.withdraw()\n"
    "root.attributes('-topmost', True)\n"
    "p = filedialog.askopenfilename(\n"
    "    title='选择预设配置文件（XXX_config.json）',\n"
    "    filetypes=[('预设/配置文件', '*_config.json'), ('JSON 文件', '*.json'),\n"
    "               ('所有文件', '*.*')])\n"
    "sys.stdout.write(p or '')\n"
    "root.destroy()\n"
)


def pick_preset_file():
    """弹系统文件对话框选择预设文件。
    返回路径字符串；用户取消返回 ''；tkinter 不可用/异常返回 None。"""
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        r = subprocess.run([sys.executable, '-c', TK_PICK],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=900, env=env, creationflags=NO_WINDOW)
    except Exception:                                          # noqa: BLE001
        return None
    if r.returncode == 3:
        return None
    return r.stdout.decode('utf-8', 'replace').strip()


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = APP_NAME
    sys_version = ''

    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True
        except Exception:                                      # noqa: BLE001
            self.close_connection = True
            sys.stderr.write("请求处理异常:\n" + traceback.format_exc())

    def log_message(self, fmt, *args):
        pass

    # ---- 输出
    def _send(self, code, body, ctype, extra=None, head_only=False):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only and body:
            try:
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True

    def _json(self, obj, code=200, head_only=False):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8', head_only=head_only)

    def _text(self, code, msg):
        self._send(code, msg.encode('utf-8'), 'text/plain; charset=utf-8')

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _origin_ok(self):
        o = self.headers.get('Origin')
        if not o:
            return True
        host = self.headers.get('Host') or ''
        port = self.server.server_address[1]
        return o in ('http://%s' % host, 'http://127.0.0.1:%d' % port,
                     'http://localhost:%d' % port)

    # ---- GET
    def do_GET(self):
        self._route(False)

    def do_HEAD(self):
        self._route(True)

    def _route(self, head_only):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        st = self.server.state
        if p in ('/', '/index.html'):
            return self._page(head_only)
        if p == '/favicon.ico':
            return self._send(204, b'', 'image/x-icon', head_only=head_only)
        if p == '/api/ping':
            return self._json({'ok': True, 'app': APP_NAME, 'version': 1,
                               'at': now_str()}, head_only=head_only)
        if p == '/api/state':
            return self._json(dict(st.payload(), ok=True), head_only=head_only)
        if p == '/api/status':
            q = urllib.parse.parse_qs(u.query)
            since = vpipe.to_int((q.get('since') or ['0'])[0], 0)
            job = (q.get('job') or [''])[0]
            body = dict(st.runner.status(since, job), ok=True)
            with st.lock:
                body['paths'] = list(st.paths)
            return self._json(body, head_only=head_only)
        if p == '/api/preset':
            q = urllib.parse.parse_qs(u.query)
            path = (q.get('path') or [''])[0]
            if not path:
                return self._json({'error': '缺少 path'}, 400)
            r = load_preset_file(path)
            if r.get('error'):
                return self._json(r, 400)
            if r.get('kind') == 'config':
                if not st.set_base_config(r['config'], path):
                    return self._json({'error': '该配置缺少可用的 profiles'}, 400)
                return self._json({'ok': True, 'kind': 'config', 'path': path,
                                   'state': dict(st.payload(), ok=True)})
            st.file_presets = r['presets']
            st.file_preset_path = path
            return self._json({'ok': True, 'kind': 'presets', 'path': path,
                               'presets': r['presets']})
        return self._text(404, '未找到: ' + p)

    def _page(self, head_only):
        path = os.path.join(HERE, PAGE_NAME)
        if not os.path.isfile(path):
            return self._text(500, '缺少页面文件: %s\n（请确认 %s 与 compress_gui.py 在同一目录）'
                                   % (path, PAGE_NAME))
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError as e:
            return self._text(500, '页面读取失败: %s' % e)
        return self._send(200, body, 'text/html; charset=utf-8', head_only=head_only)

    # ---- POST
    def do_POST(self):
        if not self._origin_ok():
            return self._json({'error': '拒绝跨站请求'}, 403)
        p = urllib.parse.urlparse(self.path).path
        st = self.server.state
        data = self._body()

        if p == '/api/start':
            ok, info = st.runner.start(st.base_cfg, data)
            if not ok:
                code = 409 if '正在运行' in info else 400
                return self._json({'error': info}, code)
            return self._json({'ok': True, 'job_id': info})

        if p == '/api/cancel':
            ok, msg = st.runner.cancel()
            return self._json({'ok': True, 'message': msg})

        if p == '/api/add_paths':
            added, bad = st.add_paths(data.get('paths') or [])
            return self._json({'ok': True, 'added': added, 'rejected': bad,
                               'paths': st.paths})

        if p == '/api/remove_path':
            st.remove_path(data.get('path'))
            return self._json({'ok': True, 'paths': st.paths})

        if p == '/api/clear_paths':
            st.clear_paths()
            return self._json({'ok': True, 'paths': st.paths})

        if p == '/api/open_report':
            path = data.get('path') or st.runner.report
            if not path or not os.path.isfile(path) \
                    or os.path.splitext(path)[1].lower() not in ('.html', '.csv'):
                return self._json({'error': '报告文件不存在或类型不支持: %s' % path}, 400)
            try:
                os.startfile(path)                             # noqa: S606
            except OSError as e:
                return self._json({'error': '打开失败: %s' % e}, 400)
            return self._json({'ok': True})

        if p == '/api/open_review':
            ws = data.get('path') or st.first_workspace()
            if not ws or not os.path.isdir(ws):
                return self._json({'error': '工作区目录不存在: %s（请先加入路径）' % (ws or '')}, 400)
            try:
                subprocess.Popen([sys.executable, REVIEW_PY, '--work', ws],
                                 cwd=HERE, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=NO_WINDOW)
            except OSError as e:
                return self._json({'error': '启动审阅失败: %s' % e}, 400)
            return self._json({'ok': True})

        if p == '/api/pick_preset':
            path = pick_preset_file()
            if path is None:
                return self._json({'error': '系统文件对话框不可用（请手动输入路径）'}, 400)
            if not path:
                return self._json({'ok': True, 'cancelled': True})
            r = load_preset_file(path)
            if r.get('error'):
                return self._json(r, 400)
            if r.get('kind') == 'config':
                if not st.set_base_config(r['config'], path):
                    return self._json({'error': '该配置缺少可用的 profiles'}, 400)
                return self._json({'ok': True, 'kind': 'config', 'path': path,
                                   'state': dict(st.payload(), ok=True)})
            st.file_presets = r['presets']
            st.file_preset_path = path
            return self._json({'ok': True, 'kind': 'presets', 'path': path,
                               'presets': r['presets']})

        if p == '/api/save_preset':
            path = data.get('path') or ''
            preset = data.get('preset')
            if not path or not path.lower().endswith('.json'):
                return self._json({'error': '请提供以 .json 结尾的保存路径'}, 400)
            n = norm_preset(preset, st.base_cfg.get('default_profile') or '')
            if not n:
                return self._json({'error': '预设缺少 base_profile'}, 400)
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                atomic_write(os.path.abspath(path),
                             json.dumps(n, ensure_ascii=False, indent=2))
            except OSError as e:
                return self._json({'error': '保存失败: %s' % e}, 400)
            return self._json({'ok': True, 'path': path})

        if p == '/api/shutdown':
            had = st.runner.running
            if had:
                st.runner.cancel()
            self._json({'ok': True, 'message': '服务正在关闭'})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        return self._text(404, '未找到: ' + p)


# ---------------------------------------------------------------- 单实例 / 入口
def port_path():
    return os.path.join(GUI_HOME, PORT_NAME)


def read_port_file():
    try:
        with open(port_path(), 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_port_file(port):
    try:
        atomic_write(port_path(), json.dumps(
            {'pid': os.getpid(), 'port': port, 'app': APP_NAME, 'at': now_str()},
            ensure_ascii=False, indent=2))
    except OSError as e:
        vpipe.log("警告: 端口文件未能写入（%s）" % e)


def alive_port():
    d = read_port_file()
    if not d or not d.get('port'):
        return None
    try:
        with socket.create_connection(('127.0.0.1', int(d['port'])), 1.5) as s:
            s.sendall(b'GET /api/ping HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n')
            data = s.recv(512).decode('utf-8', 'replace')
        return int(d['port']) if APP_NAME in data else None
    except (OSError, ValueError):
        return None


def _parse_args():
    ap = argparse.ArgumentParser(prog='compress_gui',
                                 description='vpipe 统一压缩入口（本地单页 GUI）')
    ap.add_argument('paths', nargs='*', help='要加入列表的视频文件或目录（.bat 拖入时自动传入）')
    ap.add_argument('--config', default=CONFIG_JSON, help='作为默认基底的 config.json')
    ap.add_argument('--open', action='store_true', help='启动/复用后打开浏览器（.bat 使用）')
    ap.add_argument('--no-browser', action='store_true', help='只起服务，不自动打开浏览器')
    ap.add_argument('--port', type=int, default=None, help='固定端口；0/缺省=自动分配')
    ap.add_argument('--stop', action='store_true', help='关闭正在运行的实例')
    ap.add_argument('--debug', action='store_true', help='出错时保留完整异常堆栈')
    return ap.parse_args()


def _stop_instance():
    port = alive_port()
    if not port:
        vpipe.log("没有正在运行的压缩入口服务。")
        return EXIT_OK
    r = _post_json(port, '/api/shutdown', {})
    vpipe.log("已请求关闭: http://127.0.0.1:%d/（%s）" % (port, r.get('message') or 'ok'))
    return EXIT_OK


def _run(args):
    cfg = load_cfg(args.config)
    try:
        os.makedirs(GUI_HOME, exist_ok=True)
    except OSError as e:
        vpipe.log("错误: 无法创建服务数据目录 %s: %s" % (GUI_HOME, e))
        return EXIT_USAGE

    if args.stop:
        return _stop_instance()

    existed = alive_port()
    if existed and args.port is None:
        url = 'http://127.0.0.1:%d/' % existed
        vpipe.log("压缩入口已在运行，直接复用: " + url)
        if args.paths:
            # 拖入路径时只把路径追加进待处理列表：任务本来就是串行的，既没必要并行，
            # 也不必再开一个页面——已在运行的页面每 0.6 秒拉一次 /api/status，
            # 列表变化会自动刷新出来。
            r = _post_json(existed, '/api/add_paths', {'paths': args.paths})
            for p in (r.get('added') or []):
                vpipe.log("  已加入待处理列表: " + p)
            for b in (r.get('rejected') or []):
                vpipe.log("  [忽略] 路径不存在: " + b)
            return EXIT_OK
        if not args.no_browser:
            webbrowser.open(url)
        return EXIT_OK

    st = State(cfg, args.config)
    if args.paths:
        added, bad = st.add_paths(args.paths)
        for b in bad:
            vpipe.log("  [忽略] 路径不存在: %s" % b)

    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', args.port or 0), Handler)
    except OSError as e:
        vpipe.log("错误: 无法监听 127.0.0.1:%s（%s）" % (args.port or 0, e))
        return EXIT_FAIL
    httpd.daemon_threads = True
    httpd.state = st
    st.httpd = httpd
    real_port = httpd.server_address[1]
    write_port_file(real_port)

    url = 'http://127.0.0.1:%d/' % real_port
    vpipe.log("压缩入口已就绪: " + url)
    vpipe.log("当前配置: %s" % st.base_path)
    vpipe.log("已加入 %d 个路径；在页面上选好档位与参数后点「开始压缩」。" % len(st.paths))
    vpipe.log("按 Ctrl+C 关闭服务（或执行 python compress_gui.py --stop）。")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        vpipe.log("\n正在关闭压缩入口服务...")
    finally:
        if st.runner.running:
            st.runner.cancel()
        try:
            httpd.server_close()
        except OSError:
            pass
        d = read_port_file()
        if d and d.get('port') == real_port:
            try:
                os.remove(port_path())
            except OSError:
                pass
    return EXIT_OK


def main():
    vpipe.ensure_utf8()
    init_job()
    args = _parse_args()
    if args.debug:
        return _run(args)
    try:
        return _run(args)
    except KeyboardInterrupt:
        sys.stderr.write("\n已中断。\n")
        return EXIT_INTERRUPTED
    except Exception as e:                                     # noqa: BLE001
        sys.stderr.write("错误: %s: %s\n" % (e.__class__.__name__, e))
        return EXIT_FAIL


if __name__ == '__main__':
    sys.exit(main() or EXIT_OK)
