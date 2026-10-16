#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
review.py —— vpipe 人工审阅服务（本地单页）

职责
  * 转换完成后提供一个可反复双击打开的本地网页，逐项对比「源 / 各结果」。
  * 每一项（源、每一个结果）独立控制两个显式开关：删除(delete) / 过滤(filtered)。
  * 全部看完后汇总确认，把勾选的项送入 Windows 回收站（可回滚），并写回过滤器状态。

设计要点
  * 零第三方依赖：仅用标准库；import vpipe 复用探测/工具/命名等既有函数。
  * 只监听 127.0.0.1；媒体按“令牌白名单”映射，禁止任意路径读取。
  * 状态 review.json 采用原子写（.tmp + os.replace）；损坏时备份 .bad-<时间戳> 后重建。
  * 删除走回收站（SHFileOperationW + FOF_ALLOWUNDO）；失败只告警，绝不降级为永久删除。
  * 工作区（目标目录）独立管理：审阅状态 review.json 与端口文件都放在工作区的 .data 下，
    「审阅.bat」放在工作区根目录；每个工作区各有自己的单实例（重复双击只打开该工作区的页面）。

用法
  python review.py                 # 启动服务并打开浏览器（已有实例则直接打开）
  python review.py --work <目录>   # 指定工作区（源目录与 .data 都取自该目录）
  python review.py --no-browser    # 只起服务，不自动开浏览器
  python review.py --rescan        # 启动前重新扫描源目录
  python review.py --idle-timeout 10   # 空闲 10 分钟后自动关闭（0=禁用）
  python review.py --stop          # 关闭已运行的实例

生命周期
  * 服务以「页面是否还在活动」为存活依据：页面打开期间任何请求都会刷新活动时间，
    页面关闭后超过空闲阈值（config.review_idle_timeout_min，默认 10 分钟，0=禁用）
    即自动优雅退出（复用 /api/shutdown 同款清理路径）。
    这样无论由「审阅.bat」还是压缩页面拉起，都不会留下无页面、无窗口的隐蔽驻留进程。

退出码：0=正常；1=运行期错误；2=参数/配置错误
"""
import argparse
import ctypes
import hashlib
import json
import mimetypes
import os
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
import vpipe  # noqa: E402  复用探测/工具/命名规则，避免重复实现

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_NAME = 'review_page.html'
REVIEW_NAME = 'review.json'
PORT_NAME = 'review.port'
APP_MARK = 'vpipe-review'                    # /api/ping 响应用于识别本应用实例
DATA_DIR_NAME = vpipe.DATA_DIR_NAME          # 工作区下的数据目录（json + 端口文件都放这里）

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

CHUNK = 256 * 1024
HISTORY_MAX = 400
MAX_BODY = 8 * 1024 * 1024              # 单个请求体上限，避免超大 body 占用内存
MISSING = object()

_save_lock = threading.Lock()
_cfg_cache = {'cfg': None, 'tools': None}


# ---------------------------------------------------------------- 基础工具
def key_of(path):
    """状态表主键：绝对路径的规范化形式（Windows 下不区分大小写）。"""
    return os.path.normcase(os.path.abspath(path))


def now_str():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def load_cfg(path):
    """复用 vpipe.load_config，但把它的 die() 退出转换为本脚本的错误码。"""
    if _cfg_cache['cfg'] is None:
        try:
            _cfg_cache['cfg'] = vpipe.load_config(path)
        except SystemExit:
            sys.exit(EXIT_USAGE)
        except Exception as e:
            sys.stderr.write("错误: 无法读取配置文件: %s（%s）\n" % (path, e))
            sys.exit(EXIT_USAGE)
    return _cfg_cache['cfg']


def tools_of(cfg):
    if _cfg_cache['tools'] is None:
        try:
            _cfg_cache['tools'] = vpipe.tools_from_cfg(cfg)
        except SystemExit:
            sys.exit(EXIT_USAGE)
        except Exception as e:
            sys.stderr.write("错误: 找不到 ffmpeg/ffprobe: %s\n" % e)
            sys.exit(EXIT_USAGE)
    return _cfg_cache['tools']


def source_root(cfg, override=None):
    v = override or cfg.get('input_dir') or ''
    return os.path.abspath(v) if v else ''          # 未配置时返回空串，交由调用方回退到工作区


def workspace_of(cfg, args, root):
    """工作区（目标目录）：显式 --work 优先，其次源目录，最后脚本目录。
    工作区的根目录放「审阅.bat」，.data 子目录放 review.json 与端口文件。"""
    return os.path.abspath(args.work or root or HERE)


# ---------------------------------------------------------------- review.json 持久化
def review_path(work):
    return os.path.join(work, REVIEW_NAME)


def empty_review(root):
    return {'version': 2, 'root': root, 'updated': now_str(), 'items': {}}


def load_review(work, root):
    """读取审阅状态；不存在返回空表；损坏则备份 .bad-<时间戳> 后重建（只告警不崩溃）。"""
    vpipe.ensure_review_migrated(work)
    p = review_path(work)
    empty = empty_review(root)
    if not os.path.isfile(p):
        return empty
    try:
        with open(p, 'r', encoding='utf-8') as f:
            rv = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        bad = "%s.bad-%s" % (p, time.strftime('%Y%m%d_%H%M%S'))
        try:
            os.replace(p, bad)
            vpipe.log("警告: 审阅状态损坏，已备份为 %s 并重建（%s）" % (os.path.basename(bad), e))
        except OSError:
            vpipe.log("警告: 审阅状态损坏且无法备份，已忽略并重建（%s）" % e)
        return empty
    if not isinstance(rv, dict):
        vpipe.log("警告: 审阅状态内容异常，已重建: %s" % p)
        return empty
    rv.setdefault('version', 2)
    rv.setdefault('root', root)
    if not isinstance(rv.get('items'), dict):
        rv['items'] = {}
    return rv


def save_review(work, rv):
    """原子写：review.json.tmp -> os.replace。失败仅告警。
    加 _save_lock（进程内）与 review_file_lock（跨进程，压缩服务 vpipe 也会写同一个文件）。"""
    p = review_path(work)
    tmp = "%s.tmp.%d" % (p, os.getpid())
    rv['updated'] = now_str()
    with _save_lock:
        with vpipe.review_file_lock(work):
            try:
                os.makedirs(work, exist_ok=True)
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(rv, f, ensure_ascii=False, indent=2)
                os.replace(tmp, p)
                return True
            except OSError as e:
                vpipe.log("警告: 审阅状态未能写入（%s）" % e)
                vpipe._remove_quiet(tmp)
                return False


# ---------------------------------------------------------------- 项与配对
def tag_of(cfg, path):
    """若文件名以 _<tag> 结尾则返回该 tag（长 tag 优先，避免 _hevcHQ 被 _hevc 抢先匹配）。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    for t in sorted(vpipe.output_tags(cfg), key=len, reverse=True):
        if stem.endswith('_' + t):
            return t
    return None


def parent_of(path, tag):
    """产物对应的源文件（同目录、去掉 _<tag> 后缀、任意视频扩展名）；找不到返回 None。"""
    if not tag:
        return None
    d = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    base = stem[:-(len(tag) + 1)]
    for ext in sorted(vpipe.VIDEO_EXT):
        cand = os.path.join(d, base + ext)
        if os.path.isfile(cand):
            return cand
    return None


def fmt_meta(m):
    if not m:
        return '—'
    parts = []
    if m.get('w') and m.get('h'):
        parts.append("%d×%d" % (m['w'], m['h']))
    if m.get('fps'):
        parts.append("%.2ffps" % m['fps'])
    if m.get('vbitrate_kbps'):
        parts.append(vpipe.fmt_bitrate(m['vbitrate_kbps']))
    if m.get('duration'):
        parts.append(vpipe.human_time(m['duration']))
    if m.get('codec'):
        parts.append(str(m['codec']))
    return ' · '.join(parts) if parts else '—'


def meta_of(tools, path, cached):
    """探测媒体信息；size+mtime 未变则复用缓存（避免每次启动都跑 ffprobe）。"""
    try:
        st = os.stat(path)
    except OSError as e:
        return None, '无法读取文件（%s）' % e
    if cached and cached.get('meta'):
        c = cached['meta']
        if c.get('size') == st.st_size and abs((c.get('mtime') or 0) - st.st_mtime) < 1:
            c.setdefault('ctime', st.st_ctime)   # 旧缓存可能没记创建时间，就地补齐
            return c, None
    rec = vpipe.make_record(tools['ffprobe'], path, os.path.basename(path))
    if rec is None:
        return {'size': st.st_size, 'mtime': st.st_mtime, 'ctime': st.st_ctime, 'w': 0, 'h': 0, 'fps': 0,
                'vbitrate_kbps': 0, 'duration': 0, 'codec': ''}, (vpipe.LAST_PROBE_ERR or '无法解析')
    return {'size': st.st_size, 'mtime': st.st_mtime, 'ctime': st.st_ctime, 'w': rec['disp_w'], 'h': rec['disp_h'],
            'fps': rec['fps'], 'vbitrate_kbps': rec['vbitrate_kbps'],
            'duration': rec['duration'], 'codec': rec['vcodec']}, None


def make_item(path, kind, tag, parent, meta, warn=''):
    return {
        'path': path, 'name': os.path.basename(path),
        'kind': kind, 'parent': parent, 'tag': tag,
        'meta': meta, 'warn': warn,
        'delete': False,
        'filtered': (kind == 'result'),
        'reviewed': False, 'cleaned': False,
        'history': [],
    }


def scan_items(cfg, work, rv, root=None):
    """重扫源目录：为新文件建项、刷新元数据、剔除已消失的文件。
    在 items 的副本上增删，最后整体换回 rv['items']——这样调用方可以把它放在主锁之外执行
    （ffprobe 很慢），并发的读取线程仍能安全地遍历旧的对象。"""
    tools = tools_of(cfg)
    root = os.path.abspath(root or rv.get('root') or '')
    if not root or not os.path.isdir(root):
        return {'scanned': 0, 'added': 0, 'dropped': 0, 'warned': 0,
                'error': '源目录不存在或未配置 input_dir: %s' % (root or '(未设置)')}
    rv['root'] = root
    items = dict(rv['items'])
    seen, added, warned = set(), 0, 0

    for path, rel in vpipe.iter_files(root, exclude_dirs=[os.path.join(root, '_work'),
                                                          os.path.join(root, DATA_DIR_NAME)]):
        k = key_of(path)
        seen.add(k)
        tag = tag_of(cfg, path)
        parent = parent_of(path, tag) or None
        kind = 'result' if parent else 'source'
        old = items.get(k)
        meta, warn = meta_of(tools, path, old)
        if meta is None:
            warned += 1
            continue
        if old:
            old['path'] = path
            old['name'] = os.path.basename(path)
            old['meta'] = meta
            old['warn'] = warn or ''
            old['tag'] = tag or old.get('tag')
            if parent:
                # 同目录能找到对应源文件：明确是产物，按产物归档
                old['kind'] = 'result'
                old['parent'] = parent
            else:
                # 找不到源文件时保留既有身份：源被删掉后留下来的产物仍应留在同一组里，
                # 不能因为“同目录找不到无后缀源文件”就被重新判定成一个新源。
                old.setdefault('kind', 'source')
                old.setdefault('parent', None)
            old.setdefault('delete', False)
            old.setdefault('filtered', old.get('kind') == 'result')
            old.setdefault('reviewed', False)
            old.setdefault('cleaned', False)
            old.setdefault('history', [])
        else:
            items[k] = make_item(path, kind, tag, parent, meta, warn or '')
            added += 1
        if warn:
            warned += 1

    dropped = 0
    root_nc = os.path.normcase(os.path.abspath(root))
    for k, it in list(items.items()):
        if k in seen:
            continue
        if it.get('cleaned'):
            # 已由用户删除（送回收站）的项：文件不在磁盘属预期，保留条目本身。
            # 待其从回收站还原后，_reconcile 会撤销 cleaned/delete 标记，
            # 并连同原 filtered/history 一起恢复（否则会被当成新项重建，丢失用户选择）。
            continue
        # 源目录之外的项（例如拖拽处理过的、input_dir 之外的文件）不归本次扫描管：
        # 只要文件还在就保留它；文件确实消失了才剔除（既清理已删除项，也保住手动登记的内容）。
        inside = k.startswith(root_nc + os.sep)
        if inside or not os.path.isfile(it.get('path') or ''):
            del items[k]
            dropped += 1

    # 源目录之外的项（拖拽登记进来的）不参与上面的目录扫描，但同样需要补齐媒体信息
    for k, it in items.items():
        if k in seen or it.get('meta'):
            continue
        p = it.get('path') or ''
        if not os.path.isfile(p):
            continue
        meta, warn = meta_of(tools, p, it)
        if meta:
            it['meta'] = meta
            it['warn'] = warn or ''
            if not it.get('tag'):
                it['tag'] = tag_of(cfg, p)

    rv['items'] = items
    return {'scanned': len(seen), 'added': added, 'dropped': dropped,
            'warned': warned, 'error': None}


def build_queue(cfg, work, rv):
    """把扁平 items 表整理成「项组」队列，并生成媒体令牌映射。"""
    items = rv['items']
    by_parent = {}
    groups = []
    for k, it in items.items():
        if it['kind'] == 'result' and it.get('parent'):
            by_parent.setdefault(key_of(it['parent']), []).append(k)

    used = set()
    for k, it in sorted(items.items(), key=lambda kv: kv[1]['name'].lower()):
        if it['kind'] == 'result':
            continue
        used.add(k)
        groups.append({'key': k, 'src': it,
                       'results': [items[x] for x in by_parent.get(k, [])]})
    # 源文件已不存在、但结果还在：补一个只有结果的组
    for pk in sorted(by_parent):
        if pk in used:
            continue
        res = [items[x] for x in by_parent[pk]]
        groups.append({'key': pk, 'src': None, 'results': res, 'src_path': res[0]['parent']})

    # 令牌映射（确定性）：sha1(normcase(path))[:16]
    tokens = {}
    for it in items.values():
        t = hashlib.sha1(key_of(it['path']).encode('utf-8', 'replace')).hexdigest()[:16]
        it['token'] = t
        tokens[t] = it['path']

    total = sum(1 for it in items.values() if not it.get('cleaned'))
    reviewed = sum(1 for it in items.values() if it.get('reviewed') and not it.get('cleaned'))
    unreviewed_groups = 0
    start = 0
    for i, g in enumerate(groups):
        grp = [g['src']] if g.get('src') else []
        grp += g['results']
        pend = [it for it in grp if not it.get('reviewed') and not it.get('cleaned')]
        if pend and unreviewed_groups == 0:
            start = i
        if pend:
            unreviewed_groups += 1
    return {'root': rv.get('root'), 'updated': rv.get('updated'),
            'groups': groups, 'tokens': tokens,
            'progress': {'total': total, 'reviewed': reviewed,
                         'unreviewed_groups': unreviewed_groups,
                         'checked': total - reviewed},
            'start_index': start}


# ---------------------------------------------------------------- 开关 / 撤销
def toggle_flag(rv, path, flag, value):
    """切换某一项的 delete / filtered 开关；记一条 history；成功后返回该项。"""
    if flag not in ('delete', 'filtered'):
        return None, '未知开关: %s' % flag
    k = key_of(path)
    it = rv['items'].get(k)
    if not it:
        return None, '项不在队列中: %s' % path
    if it.get('cleaned'):
        return None, '该项已执行删除，不再接受修改'
    old = bool(it.get(flag))
    new = bool(value)
    if old != new:
        it[flag] = new
        it.setdefault('history', []).append(
            {'t': time.time(), 'ts': now_str(), 'path': it['path'],
             'flag': flag, 'from': old, 'to': new})
        if len(it['history']) > HISTORY_MAX:
            del it['history'][:-HISTORY_MAX]
    it['reviewed'] = True
    return it, None


def undo_last(rv):
    """回滚全表时间上最近的一次开关切换。返回 (项, 说明) 或 (None, 原因)。"""
    best, bk = None, None
    for k, it in rv['items'].items():
        for e in (it.get('history') or []):
            if best is None or e['t'] > best['t']:
                best, bk = e, k
    if best is None:
        return None, '没有可撤销的操作'
    it = rv['items'][bk]
    it[best['flag']] = best['from']
    it['history'] = [e for e in it['history'] if e is not best]
    if not it['history']:
        it['reviewed'] = False
    return it, '已撤销：%s 的「%s」回到 %s' % (
        it['name'], '删除' if best['flag'] == 'delete' else '过滤',
        '开' if best['from'] else '关')


# ---------------------------------------------------------------- 执行前校验
def validate_item(it):
    """比对 size + mtime，判断记录是否仍然有效。返回 (ok, 原因)。"""
    if it.get('cleaned'):
        return False, '已执行过删除'
    p = it['path']
    if not os.path.isfile(p):
        return False, '文件已不存在'
    try:
        st = os.stat(p)
    except OSError as e:
        return False, '无法读取文件（%s）' % e
    m = it.get('meta') or {}
    if m.get('size') and st.st_size != m['size']:
        return False, '文件大小已变化（记录 %d 字节，实际 %d 字节）' % (m['size'], st.st_size)
    if m.get('mtime') and abs(st.st_mtime - m['mtime']) >= 1:
        return False, '文件修改时间已变化（可能已被移动/改名/重编）'
    return True, ''


def row_of(it, src_size=None):
    m = it.get('meta') or {}
    ct = m.get('ctime') or 0
    row = {'path': it['path'], 'name': it['name'], 'kind': it['kind'], 'tag': it.get('tag'),
          'size': m.get('size') or 0, 'size_text': vpipe.human_size(m.get('size') or 0),
          'meta_text': fmt_meta(m), 'warn': it.get('warn') or '',
          'filtered': bool(it.get('filtered')), 'reviewed': bool(it.get('reviewed')),
          'ctime': ct,
          'ctime_text': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ct)) if ct else '—'}
    if src_size:
        row['saving'] = round(100.0 - (m.get('size') or 0) * 100.0 / src_size, 1)
    return row


def group_results_of(rv, it):
    """某一项所属组里的全部结果项（源则返回它自己的结果；结果则返回同源的兄弟结果）。"""
    if it['kind'] == 'result' and it.get('parent'):
        pk = key_of(it['parent'])
        return [x for x in rv['items'].values()
                if x['kind'] == 'result' and key_of(x.get('parent')) == pk]
    k = key_of(it['path'])
    return [x for x in rv['items'].values()
            if x['kind'] == 'result' and key_of(x.get('parent')) == k]


def build_summary(cfg, work, rv):
    items = rv['items']
    delete_rows, keep_rows = [], []
    del_bytes = 0
    src_size = {}
    for it in items.values():
        if it['kind'] == 'source':
            src_size[key_of(it['path'])] = (it.get('meta') or {}).get('size') or 0
    for k in sorted(items, key=lambda x: (items[x]['kind'], items[x]['name'].lower())):
        it = items[k]
        ok, why = validate_item(it)
        ss = src_size.get(key_of(it.get('parent') or ''), 0) if it['kind'] == 'result' else 0
        row = row_of(it, ss if it['kind'] == 'result' else None)
        row['valid'] = ok
        row['invalid_reason'] = why
        if it.get('delete'):
            row['checked'] = True
            delete_rows.append(row)
            del_bytes += row['size']
        else:
            keep_rows.append(row)
    pend = [it for it in items.values()
            if not it.get('reviewed') and not it.get('cleaned')]
    return {'delete': delete_rows, 'keep': keep_rows,
            'delete_count': len(delete_rows), 'delete_bytes': del_bytes,
            'delete_bytes_text': vpipe.human_size(del_bytes),
            'source_count': sum(1 for r in delete_rows if r['kind'] == 'source'),
            'unreviewed': len(pend),
            'stale_count': sum(1 for r in delete_rows if not r['valid'])}


# ---------------------------------------------------------------- 回收站删除
FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ('hwnd', ctypes.c_void_p),
        ('wFunc', ctypes.c_uint),
        ('pFrom', ctypes.c_wchar_p),
        ('pTo', ctypes.c_wchar_p),
        ('fFlags', ctypes.c_ushort),
        ('fAnyOperationsAborted', ctypes.c_int),
        ('hNameMappings', ctypes.c_void_p),
        ('lpszProgressTitle', ctypes.c_wchar_p),
    ]


def recycle_one(path):
    """把一个文件送进 Windows 回收站。返回 (ok, 原因)。非 Windows 直接失败（不降级为永久删除）。"""
    if os.name != 'nt':
        return False, '当前系统不支持回收站删除'
    abspath = os.path.abspath(path)
    if abspath.startswith('\\\\?\\'):
        return False, '不支持 \\\\?\\ 前缀路径'
    try:
        shell32 = ctypes.WinDLL('shell32', use_last_error=True)
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
        shell32.SHFileOperationW.restype = ctypes.c_int
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = abspath + '\0\0'          # 必须双 \0 结尾，否则可能变成永久删除
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        rc = shell32.SHFileOperationW(ctypes.byref(op))
        if rc != 0:
            return False, 'SHFileOperation 返回码 %d' % rc
        if op.fAnyOperationsAborted:
            return False, '操作被中止（文件可能被占用）'
        return True, ''
    except OSError as e:
        return False, '调用回收站接口失败（%s）' % e
    except Exception as e:                                   # noqa: BLE001  兜底：绝不误降级
        return False, '回收站删除异常（%s）' % e


def report_row(it, path, name, kind, status, reason=''):
    """执行报告的一行（实际执行或跳过都走这里）。
    体积优先取队列里记录的 meta.size；查不到（例如不在队列中的未知项）再回落到磁盘实际大小，
    保证前端「体积」列永远有值。"""
    size = (it.get('meta') or {}).get('size') if it else None
    if not size:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
    return {'path': path, 'name': name, 'kind': kind, 'size': size,
            'size_text': vpipe.human_size(size), 'status': status, 'reason': reason}


def apply_delete(rv, paths=None, confirm_source=False, force=False):
    """执行删除：先结果后源；逐项校验；结果送入回收站。绝不永久删除。
    返回执行报告 dict。"""
    items = rv['items']
    candidates = []
    if paths:
        for p in paths:
            k = key_of(p)
            it = items.get(k)
            if not it:
                candidates.append((None, p, '未知项（不在当前队列中）'))
            else:
                candidates.append((k, p, None))
    else:
        for k, it in items.items():
            if it.get('delete'):
                candidates.append((k, it['path'], None))

    planned, skipped = [], []
    for k, p, why in candidates:
        if why:
            skipped.append(report_row(None, p, os.path.basename(p), '?', 'skipped', why))
            continue
        it = items[k]
        ok, why2 = validate_item(it)
        if not ok and not force:
            skipped.append(report_row(it, p, it['name'], it['kind'], 'skipped', why2))
            continue
        planned.append(it)

    # 先结果、后源（万一中途异常，源文件仍在）
    planned.sort(key=lambda it: 0 if it['kind'] == 'result' else 1)

    # 删源前确认：组内必须还有存活的（不被删的）结果
    doomed = set(key_of(it['path']) for it in planned)
    final = []
    for it in planned:
        if it['kind'] == 'source':
            survivors = [x for x in group_results_of(rv, it)
                         if key_of(x['path']) not in doomed and os.path.isfile(x['path'])]
            if not survivors and not confirm_source:
                skipped.append(report_row(it, it['path'], it['name'], 'source', 'skipped',
                                          '组内没有保留的结果，删除源会留下空内容（需显式确认）'))
                continue
        final.append(it)

    rows, ok_bytes, failed = [], 0, 0
    for it in final:
        size = (it.get('meta') or {}).get('size') or 0
        good, why = recycle_one(it['path'])
        if good:
            it['cleaned'] = True
            it['delete'] = True
            it['reviewed'] = True
            ok_bytes += size
            rows.append(report_row(it, it['path'], it['name'], it['kind'], 'recycled'))
        else:
            failed += 1
            rows.append(report_row(it, it['path'], it['name'], it['kind'], 'failed', why))
    return {'ok': len(final) - failed, 'failed': failed, 'skipped': len(skipped),
            'bytes': ok_bytes, 'bytes_text': vpipe.human_size(ok_bytes),
            'rows': rows, 'skipped_rows': skipped, 'at': now_str()}


# ---------------------------------------------------------------- HTTP 流（支持 Range）
def parse_range(header, size):
    """解析 Range 头，返回 (start, end, bad)。bad=True 表示语法非法（应回 416）。"""
    if not header or size <= 0:
        return 0, size - 1, False
    h = header.strip()
    if not h.lower().startswith('bytes='):
        return 0, size - 1, False
    spec = h[6:].split(',')[0].strip()
    if '-' not in spec:
        return 0, size - 1, True
    a, _, b = spec.partition('-')
    try:
        if a == '':
            n = int(b)
            if n <= 0:
                return 0, size - 1, True
            start = max(0, size - n)
            return start, size - 1, False
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        return 0, size - 1, True
    if start >= size:
        return 0, size - 1, True
    end = min(end, size - 1)
    if end < start:
        return 0, size - 1, True
    return start, end, False


# ---------------------------------------------------------------- 服务状态
def item_view(it):
    m = it.get('meta') or {}
    tok = it.get('token') or ''
    return {'path': it['path'], 'name': it['name'], 'kind': it['kind'],
            'tag': it.get('tag'), 'parent': it.get('parent'),
            'size': m.get('size') or 0, 'size_text': vpipe.human_size(m.get('size') or 0),
            'meta_text': fmt_meta(m), 'w': m.get('w') or 0, 'h': m.get('h') or 0,
            'fps': m.get('fps') or 0, 'duration': m.get('duration') or 0,
            'vbitrate_kbps': m.get('vbitrate_kbps') or 0,
            'delete': bool(it.get('delete')), 'filtered': bool(it.get('filtered')),
            'reviewed': bool(it.get('reviewed')), 'cleaned': bool(it.get('cleaned')),
            'exists': os.path.isfile(it['path']), 'warn': it.get('warn') or '',
            'media': ('/media/' + tok) if tok else None}


class ServerState(object):
    def __init__(self, cfg, work, root):
        self.cfg = cfg
        self.work = work
        self.root = root
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()   # 串行化重扫；重扫的 ffprobe 在主锁之外执行
        self.rv = load_review(work, root)
        self.info = None
        self.queue = None
        self.tokens = {}
        self.httpd = None
        self.last_activity = time.time()   # 最近一次页面活动；空闲看门狗据此判断是否仍需存活

    def touch(self):
        """记录一次「页面活动」（任意请求入口调用）；空闲看门狗据此续期。"""
        self.last_activity = time.time()

    def rebuild(self):
        """只重算分组/令牌（不跑 ffprobe），用于开关变更后刷新队列。"""
        self.queue = build_queue(self.cfg, self.work, self.rv)
        self.tokens = self.queue['tokens']

    def rescan(self):
        """重扫源目录：ffprobe 很慢，放在 scan_lock 内、主锁之外执行，
        避免长时间占用主锁而让页面轮询/播放请求全部卡住。"""
        with self.scan_lock:
            return scan_items(self.cfg, self.work, self.rv, self.root)

    def refresh(self, rescan=True):
        """（可选重扫）+ 重算分组 + 落盘。重扫在锁外执行，只有结果落盘时才持主锁。"""
        if rescan:
            self.info = self.rescan()
        with self.lock:
            self.rebuild()
            save_review(self.work, self.rv)
        return self.info

    def payload(self):
        with self.lock:
            self._reconcile()
            if self.queue is None:
                self.rebuild()
            q = self.queue
            groups = []
            for g in q['groups']:
                groups.append({
                    'key': g['key'],
                    'src': item_view(g['src']) if g.get('src') else None,
                    'src_path': (g.get('src') or {}).get('path') or g.get('src_path'),
                    'results': [item_view(x) for x in g['results']],
                })
            return {'root': q['root'], 'updated': q['updated'],
                    'start_index': q['start_index'], 'progress': q['progress'],
                    'groups': groups,
                    'scan': {'added': (self.info or {}).get('added', 0),
                             'dropped': (self.info or {}).get('dropped', 0),
                             'warned': (self.info or {}).get('warned', 0),
                             'error': (self.info or {}).get('error')}}

    def _reconcile(self):
        """每次对外提供状态前，按文件系统实时对账（不跑 ffprobe）：
        被删除（回收站）后又还原的项应恢复正常，撤销 cleaned/delete 标记，
        避免出现「文件明明回来了，却仍显示已删除/不可播放」。"""
        changed = False
        for it in self.rv['items'].values():
            p = it.get('path') or ''
            if it.get('cleaned') and os.path.isfile(p):
                it['cleaned'] = False
                it['delete'] = False
                changed = True
        if changed:
            self.rebuild()
            save_review(self.work, self.rv)


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'vpipe-review'
    sys_version = ''

    # 顶层：任何未捕获异常都转成 500，绝不让服务线程崩掉
    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True
        except Exception:                                    # noqa: BLE001
            self.close_connection = True
            sys.stderr.write("请求处理异常:\n" + traceback.format_exc())

    def log_message(self, fmt, *args):
        pass                                                  # 静音访问日志（视频请求量很大）

    # ---- 输出工具
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
        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self._send(code, raw, 'application/json; charset=utf-8', head_only=head_only)

    def _text(self, code, msg):
        self._send(code, msg.encode('utf-8'), 'text/plain; charset=utf-8')

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > MAX_BODY:
            return {}                       # 拒绝超大请求体，避免无谓的内存占用
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _origin_ok(self):
        """写操作仅接受来自本页面的请求，挡住其它站点对本服务的跨站调用。"""
        o = self.headers.get('Origin')
        if not o:
            return True
        host = self.headers.get('Host') or ''
        port = self.server.server_address[1]
        return o in ('http://%s' % host, 'http://127.0.0.1:%d' % port,
                     'http://localhost:%d' % port)

    # ---- GET
    def do_GET(self):
        self._route(head_only=False)

    def do_HEAD(self):
        self._route(head_only=True)

    def _route(self, head_only):
        self.server.state.touch()          # 任何请求都算「页面还在用」，为空闲看门狗续期
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ('/', '/index.html'):
            return self._page(head_only)
        if p == '/favicon.ico':
            return self._send(204, b'', 'image/x-icon', head_only=head_only)
        if p == '/api/ping':
            return self._json({'ok': True, 'app': 'vpipe-review',
                               'version': 2, 'at': now_str()}, head_only=head_only)
        if p == '/api/queue':
            # 只读：GET 不再触发重扫（状态变更类操作统一走 POST /api/rescan），
            # 避免被跨站 GET（无需预检即可发出）在用户不知情下触发全量扫描。
            return self._json(self.server.state.payload(), head_only=head_only)
        if p == '/api/summary':
            st = self.server.state
            with st.lock:
                return self._json(build_summary(st.cfg, st.work, st.rv), head_only=head_only)
        if p.startswith('/media/'):
            return self._media(p[len('/media/'):], head_only)
        return self._text(404, '未找到: ' + p)

    def _page(self, head_only):
        path = os.path.join(HERE, PAGE_NAME)
        if not os.path.isfile(path):
            return self._text(500, '缺少页面文件: %s\n（请确认 review_page.html 与 review.py 在同一目录）' % path)
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError as e:
            return self._text(500, '页面读取失败: %s' % e)
        return self._send(200, body, 'text/html; charset=utf-8', head_only=head_only)

    def _media(self, token, head_only=False):
        st = self.server.state
        with st.lock:
            path = st.tokens.get(token)
        if not path or not os.path.isfile(path):
            return self._json({'error': '媒体不存在或已失效，请重新扫描'}, 404)
        try:
            size = os.path.getsize(path)
        except OSError as e:
            return self._json({'error': '无法读取媒体: %s' % e}, 404)
        rng = self.headers.get('Range')
        start, end, bad = parse_range(rng, size)
        if bad:
            return self._send(416, b'', 'text/plain',
                              extra={'Content-Range': 'bytes */%d' % size}, head_only=True)
        length = max(0, end - start + 1)
        ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        code = 206 if rng else 200
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if rng:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.end_headers()
        if head_only or length <= 0:
            return
        try:
            with open(path, 'rb') as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True
        except OSError as e:
            sys.stderr.write("媒体流中断: %s（%s）\n" % (path, e))
            self.close_connection = True

    # ---- POST
    def do_POST(self):
        self.server.state.touch()          # 写操作同样续期（页面在交互）
        if not self._origin_ok():
            return self._json({'error': '拒绝跨站请求'}, 403)
        u = urllib.parse.urlparse(self.path)
        p = u.path
        st = self.server.state
        data = self._body()
        if p == '/api/toggle':
            path, flag = data.get('path'), data.get('flag')
            if not path or 'value' not in data:
                return self._json({'error': '缺少 path 或 value'}, 400)
            with st.lock:
                it, err = toggle_flag(st.rv, path, flag, data.get('value'))
                if err:
                    return self._json({'error': err}, 400)
                st.rebuild()
                save_review(st.work, st.rv)
                return self._json({'ok': True, 'item': item_view(it),
                                   'progress': st.queue['progress']})
        if p == '/api/reviewed':
            path = data.get('path')
            with st.lock:
                it = st.rv['items'].get(key_of(path or ''))
                if not it:
                    return self._json({'error': '项不在队列中'}, 400)
                it['reviewed'] = True
                st.rebuild()
                save_review(st.work, st.rv)
                return self._json({'ok': True, 'progress': st.queue['progress']})
        if p == '/api/undo':
            with st.lock:
                it, msg = undo_last(st.rv)
                if it is None:
                    return self._json({'error': msg}, 400)
                st.rebuild()
                save_review(st.work, st.rv)
                return self._json({'ok': True, 'message': msg, 'item': item_view(it),
                                   'progress': st.queue['progress']})
        if p == '/api/apply':
            with st.lock:
                report = apply_delete(st.rv, data.get('delete') or None,
                                      confirm_source=bool(data.get('confirm_source')),
                                      force=bool(data.get('force')))
            it_after = st.rescan()          # 删完重扫（在锁外执行，避免卡住页面轮询）
            with st.lock:
                st.info = it_after
                st.rebuild()
                save_review(st.work, st.rv)
                return self._json({'ok': True, 'report': report,
                                   'progress': st.queue['progress'],
                                   'scan': it_after})
        if p == '/api/rescan':
            info = st.rescan()
            with st.lock:
                st.info = info
                st.rebuild()
                save_review(st.work, st.rv)
                if info.get('error'):
                    return self._json({'error': info['error']}, 400)
                return self._json({'ok': True, 'scan': info, 'progress': st.queue['progress']})
        if p == '/api/shutdown':
            self._json({'ok': True, 'message': '服务正在关闭'})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        return self._text(404, '未找到: ' + p)


# ---------------------------------------------------------------- 空闲看门狗
DEFAULT_IDLE_MIN = 10         # 分钟；页面关闭后多久自动退出（0=禁用）
IDLE_CHECK_MAX = 30.0         # 看门狗最长检查间隔（秒）；阈值更短时自动缩短，便于测试


def _watch_idle(httpd, timeout_sec):
    """空闲看门狗：把「服务存活」绑定到「页面是否在活动」。

    Handler 在每个请求入口刷新 httpd.state.last_activity；页面关闭后不再有任何请求，
    超过 timeout_sec 秒即主动调用 httpd.shutdown()，走与 /api/shutdown / Ctrl+C 相同的
    清理路径（关闭监听、删除端口文件、返回 0）。因此「审阅.bat 拉起 / 压缩页面拉起 /
    父进程被强杀」三条路径最终都会收敛，不会留下无页面、无窗口的隐蔽驻留进程。
    """
    step = max(1.0, min(IDLE_CHECK_MAX, float(timeout_sec)))
    while True:
        time.sleep(step)
        try:
            idle = time.time() - httpd.state.last_activity
        except Exception:                                     # noqa: BLE001
            return
        if idle > timeout_sec:
            vpipe.log("审阅服务空闲 %.1f 分钟无页面活动（阈值 %.1f 分钟），自动关闭。"
                      % (idle / 60.0, timeout_sec / 60.0))
            threading.Thread(target=httpd.shutdown, daemon=True).start()
            return


# ---------------------------------------------------------------- 单实例 / 入口
def port_path(work):
    return os.path.join(work, PORT_NAME)


def read_port_file(work):
    p = port_path(work)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _ping_review_port(port):
    """探测给定端口上是否为 vpipe 审阅服务实例。"""
    try:
        with socket.create_connection(('127.0.0.1', int(port)), 1.5) as s:
            s.sendall(b'GET /api/ping HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n')
            data = s.recv(512).decode('utf-8', 'replace')
        return APP_MARK in data
    except (OSError, ValueError):
        return False


def alive_port(work):
    """返回正在运行实例的端口号，否则 None。"""
    d = read_port_file(work)
    if not d or not d.get('port'):
        return None
    return int(d['port']) if _ping_review_port(d['port']) else None


def claim_port_file(work, port):
    """以独占（O_EXCL）方式登记端口文件，消除「两个实例同时启动、端口文件互相覆盖」的竞态。
    返回 'ok'（已登记本实例）或 'taken'（已有活跃实例，调用方应复用而不是再起一个）。"""
    p = port_path(work)
    payload = json.dumps({'pid': os.getpid(), 'port': port, 'app': APP_MARK, 'at': now_str()},
                         ensure_ascii=False, indent=2)
    try:
        os.makedirs(work, exist_ok=True)
    except OSError as e:
        vpipe.log("警告: 工作区数据目录无法创建（%s）" % e)
        return 'ok'
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload.encode('utf-8'))
        finally:
            os.close(fd)
        return 'ok'
    except FileExistsError:
        d = read_port_file(work)
        if d and d.get('port') and _ping_review_port(d['port']):
            return 'taken'
        tmp = "%s.tmp.%d" % (p, os.getpid())
        try:                                                 # 遗留（进程已死）：接管覆盖
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(payload)
            os.replace(tmp, p)
            return 'ok'
        except OSError:
            vpipe._remove_quiet(tmp)
            return 'taken'
    except OSError as e:
        vpipe.log("警告: 端口文件未能写入（%s）" % e)
        return 'ok'


def write_review_bat(ws):
    """在工作区根目录生成/刷新可重复双击的「审阅.bat」（单一实现放在 vpipe 中，避免文本漂移）。"""
    return vpipe.write_review_shortcut(ws)


# ---------------------------------------------------------------- CLI
def _parse_args():
    ap = argparse.ArgumentParser(
        prog='review', description='vpipe 人工审阅服务（源 / 结果对比 · 删除与过滤开关）')
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--work', default=None,
                    help='工作区（目标目录）：.data 下放 review.json/端口文件，根目录放「审阅.bat」；默认取源目录')
    ap.add_argument('--input', default=None, help='源目录，默认取 config.input_dir（也可用 --work 指定）')
    ap.add_argument('--port', type=int, default=None, help='固定端口；默认取 config.review_page_port（0=自动）')
    ap.add_argument('--rescan', action='store_true', help='启动前重新扫描源目录')
    ap.add_argument('--no-browser', action='store_true', help='只起服务，不自动打开浏览器')
    ap.add_argument('--stop', action='store_true', help='关闭正在运行的审阅服务')
    ap.add_argument('--idle-timeout', type=float, default=None, metavar='MIN',
                    help='页面关闭后空闲多少分钟自动关闭服务（0=禁用）；'
                         '默认取 config.review_idle_timeout_min（默认 %d）' % DEFAULT_IDLE_MIN)
    ap.add_argument('--debug', action='store_true', help='出错时保留完整异常堆栈')
    return ap.parse_args()


def _pid_is_python(pid):
    """校验 pid 是一个 python 进程，避免误杀复用该 PID 的无关进程。"""
    try:
        out = subprocess.run(['tasklist', '/FI', 'PID eq %d' % int(pid), '/FO', 'CSV', '/NH'],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return 'python' in out.stdout.decode('utf-8', 'replace').lower()


def _kill_by_pid(pid):
    """按进程号终止遗留实例（连带子进程）。非 python 进程则拒绝，避免误杀。"""
    if not pid or not _pid_is_python(pid):
        return False
    try:
        subprocess.run(['taskkill', '/PID', str(int(pid)), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _stop_instance(work):
    d = read_port_file(work) or {}
    port = alive_port(work)
    if port:
        try:
            req = urllib.request.Request('http://127.0.0.1:%d/api/shutdown' % port,
                                         data=b'{}', method='POST')
            urllib.request.urlopen(req, timeout=3).read()
            vpipe.log("已请求关闭: http://127.0.0.1:%d/" % port)
            return EXIT_OK
        except OSError as e:
            vpipe.log("提示: HTTP 关闭未成功（%s），改为按进程号兜底终止 ..." % e)
    # HTTP 不可达或关闭失败时，按端口文件里的 pid 兜底，避免 --stop 形同虚设。
    pid = d.get('pid')
    if pid and d.get('app') == APP_MARK and _kill_by_pid(pid):
        vpipe.log("已按进程号终止遗留实例（pid=%s）。" % pid)
        vpipe._remove_quiet(port_path(work))
        return EXIT_OK
    vpipe.log("没有正在运行的审阅服务。")
    return EXIT_OK


def _run(args):
    cfg = load_cfg(args.config)
    root = source_root(cfg, args.input or args.work)   # --work 同时指定工作区与源目录；--input 可单独覆盖
    ws = workspace_of(cfg, args, root)
    if not root:
        root = ws                              # 未配置 input_dir 时，以工作区本身为源目录
    work = os.path.join(ws, DATA_DIR_NAME)     # json/日志/端口文件都放工作区的 .data 下

    if args.stop:
        return _stop_instance(work)

    if not os.path.isdir(root):
        vpipe.log("错误: 源目录不存在或未配置 input_dir: %s" % (root or '(未设置)'))
        return EXIT_USAGE

    existed = alive_port(work)
    if existed and args.port is None:
        url = 'http://127.0.0.1:%d/' % existed
        vpipe.log("审阅服务已在运行，直接复用: " + url)
        vpipe.log("（重复双击不会重启服务，进度原样保留）")
        if not args.no_browser:
            webbrowser.open(url)
        return EXIT_OK

    st = ServerState(cfg, work, root)
    st.refresh(rescan=True)
    if st.info and st.info.get('error'):
        vpipe.log("错误: %s" % st.info['error'])
        return EXIT_USAGE

    port = args.port if args.port is not None else vpipe.to_int(cfg.get('review_page_port'), 0)
    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', port or 0), Handler)
    except OSError as e:
        vpipe.log("错误: 无法监听 127.0.0.1:%s（%s）" % (port or 0, e))
        return EXIT_FAIL
    httpd.daemon_threads = True
    httpd.state = st
    st.httpd = httpd
    real_port = httpd.server_address[1]
    if claim_port_file(work, real_port) == 'taken':
        # 启动竞态：另一实例抢先登记并存活，关闭本实例监听并复用它。
        try:
            httpd.server_close()
        except OSError:
            pass
        other = alive_port(work)
        if other:
            url = 'http://127.0.0.1:%d/' % other
            vpipe.log("审阅服务已在运行，直接复用: " + url)
            if not args.no_browser:
                webbrowser.open(url)
        return EXIT_OK
    bat = write_review_bat(ws)

    url = 'http://127.0.0.1:%d/' % real_port
    prog = st.queue['progress']
    vpipe.log("审阅已就绪: " + url)
    vpipe.log("工作区: %s" % ws)
    vpipe.log("源目录: %s" % root)
    vpipe.log("共 %d 组 / %d 项，还有 %d 项未审阅（%d 组含未审阅项）" % (
        len(st.queue['groups']), prog['total'], prog['checked'], prog['unreviewed_groups']))
    if bat:
        vpipe.log("快捷入口: %s（可重复双击）" % bat)
    vpipe.log("按 Ctrl+C、页面「关闭服务」或 review.py --stop 结束；页面关闭后超时会自动退出。")

    idle_min = args.idle_timeout
    if idle_min is None:
        idle_min = vpipe.to_int(cfg.get('review_idle_timeout_min'), DEFAULT_IDLE_MIN)
    idle_sec = max(0.0, float(idle_min) * 60.0)
    if idle_sec > 0:
        threading.Thread(target=_watch_idle, args=(httpd, idle_sec), daemon=True).start()
        vpipe.log("空闲保护：页面关闭后 %.1f 分钟无活动将自动关闭服务"
                  "（config.review_idle_timeout_min / --idle-timeout，0=禁用）。" % (idle_sec / 60.0))
    else:
        vpipe.log("空闲自动退出已禁用（--idle-timeout 0）。")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        vpipe.log("\n正在关闭审阅服务...")
    finally:
        try:
            httpd.server_close()
        except OSError:
            pass
        d = read_port_file(work)
        if d and d.get('port') == real_port:
            vpipe._remove_quiet(port_path(work))
    return EXIT_OK


def main():
    vpipe.ensure_utf8()
    args = _parse_args()
    if args.debug:
        return _run(args)
    try:
        return _run(args)
    except KeyboardInterrupt:
        sys.stderr.write("\n已中断。\n")
        return vpipe.EXIT_INTERRUPTED
    except Exception as e:                                    # noqa: BLE001  全局兜底
        sys.stderr.write("错误: %s: %s\n" % (e.__class__.__name__, e))
        return EXIT_FAIL


if __name__ == '__main__':
    sys.exit(main() or EXIT_OK)


