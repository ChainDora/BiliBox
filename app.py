# -*- coding: utf-8 -*-
"""
BiliBox - 主程序
本地网页界面的 B站视频下载工具（个人离线备份用）。
纯 Python 标准库，无需安装任何第三方依赖。
"""

import json
import mimetypes
import os
import queue
import re
import shutil
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# Windows 控制台编码兜底，避免中文输出报 UnicodeEncodeError
if sys.platform.startswith("win"):
    for stream_name in ("stdout", "stderr"):
        try:
            stream = getattr(sys, stream_name, None)
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from biliapi import (BiliClient, BiliError, QUALITY_NAMES, AUDIO_NAMES, make_safe_filename, BROWSER_UA)
from config import Config
from records import Records, Cache
import downloader as dl

APP_NAME = "BiliBox"
APP_VER = "2.1"
WEB_DIR = os.path.join(BASE_DIR, "web")

config = Config(BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
records = Records(os.path.join(DATA_DIR, "records.json"))
cache = Cache(os.path.join(DATA_DIR, "cache"))


# =====================================================================
# 工具函数
# =====================================================================
def now():
    return time.time()


def fmt_sec(s):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(s))


def pick_folder(title="选择下载目录"):
    """Windows 原生文件夹选择框：纯 ctypes 调 shell32，不引入任何依赖。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class BROWSEINFO(ctypes.Structure):
            _fields_ = [
                ("hwndOwner", wintypes.HWND),
                ("pidlRoot", ctypes.c_void_p),
                ("pszDisplayName", ctypes.c_wchar_p),
                ("lpszTitle", ctypes.c_wchar_p),
                ("ulFlags", ctypes.c_uint),
                ("lpfn", ctypes.c_void_p),
                ("lParam", ctypes.c_void_p),
                ("iImage", ctypes.c_int),
            ]

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFO)]
        shell32.SHGetPathFromIDListW.restype = ctypes.c_bool
        shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        try:
            ole32.CoInitialize(None)
        except Exception:
            pass

        display = ctypes.create_unicode_buffer(260)
        bi = BROWSEINFO()
        bi.hwndOwner = None
        bi.pidlRoot = None
        bi.pszDisplayName = ctypes.cast(display, ctypes.c_wchar_p)
        bi.lpszTitle = title
        bi.ulFlags = 0x41   # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
        bi.lpfn = None
        bi.lParam = None
        bi.iImage = 0
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ""
        buf = ctypes.create_unicode_buffer(1024)
        shell32.SHGetPathFromIDListW(pidl, buf)
        ole32.CoTaskMemFree(pidl)
        return buf.value
    except Exception:
        return ""


def qn_for_height(h):
    """请求档位：按 B站标准清晰度取整档，避免直接跳到 60 帧/大会员档位"""
    if h >= 2160:
        return 120     # 4K
    if h >= 1080:
        return 80      # 1080P
    if h >= 720:
        return 64      # 720P
    if h >= 480:
        return 32      # 480P
    return 16          # 360P


def qn_candidates(target):
    """从高到低可尝试的档位（跳过 60 帧档，未登录/无大会员时只会白跑）"""
    seq = [120, 112, 80, 64, 32, 16]
    out = [q for q in seq if q <= target]
    return out or [16]


def pick_video(videos, max_height, codec_pref):
    """从可选的视频流中挑一路"""
    if not videos:
        return None
    filtered = [v for v in videos if (v.get("height") or 0) <= max_height] or videos
    top_h = max((v.get("height") or 0) for v in filtered)
    same_h = [v for v in filtered if (v.get("height") or 0) == top_h]

    def codec_rank(v):
        c = (v.get("codecs") or "").lower()
        if codec_pref == "auto":
            order = ["avc", "hev", "hvc", "av01"]
        else:
            order = [codec_pref] + [c2 for c2 in ["avc", "hev", "hvc", "av01"] if c2 != codec_pref]
        for i, key in enumerate(order):
            if key in c:
                return i
        return 99

    same_h.sort(key=lambda v: (codec_rank(v), -(v.get("bandwidth") or 0)))
    return same_h[0]


def pick_audio(audios, pref):
    if not audios:
        return None
    if pref == "best":
        return max(audios, key=lambda a: (a.get("bandwidth") or 0))
    mapping = {"192": 30280, "132": 30232, "64": 30216}
    want = mapping.get(str(pref))
    if want:
        for a in audios:
            if a.get("id") == want:
                return a
    return max(audios, key=lambda a: (a.get("bandwidth") or 0))


def open_folder(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


def human(n):
    return dl.human_size(n)


# =====================================================================
# 任务模型
# =====================================================================
class Job:
    def __init__(self, jid, parsed, options):
        self.id = jid
        self.title = parsed.get("title") or "未命名"
        self.cover = parsed.get("cover") or ""
        self.owner = parsed.get("owner") or ""
        self.url = parsed.get("url") or ""
        self.options = options
        self.created = now()
        self.status = "queued"       # queued / running / done / error / cancelled
        self.error = ""
        self.logs = []
        self.cancel_token = dl.CancelToken()
        self.items = []
        src_items = parsed.get("items") or []
        sel = options.get("selected")
        for i, it in enumerate(src_items):
            if sel is not None and i not in sel:
                continue
            self.items.append({
                "index": it.get("index") or (len(self.items) + 1),
                "title": it.get("title") or "",
                "bvid": it.get("bvid") or "",
                "cid": it.get("cid") or 0,
                "aid": it.get("aid") or 0,
                "ep_id": it.get("ep_id") or 0,
                "duration": it.get("duration") or 0,
                "cover": it.get("cover") or "",
                "group": it.get("group") or "",
                "group_total": it.get("group_total") or 0,
                "top_index": it.get("top_index") or 0,
                "status": "pending",
                "file": "",
                "filesize": 0,
                "error": "",
                "progress": {"percent": 0, "bytes": 0, "total": 0, "speed": 0, "eta": 0},
                "stage": "",
                "_token": dl.CancelToken(),
            })

    def log(self, msg):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        self.logs.append(line)
        if len(self.logs) > 300:
            self.logs = self.logs[-300:]

    def to_dict(self):
        return {
            "id": self.id, "title": self.title, "cover": self.cover, "owner": self.owner,
            "url": self.url, "options": self.options, "created": self.created,
            "status": self.status, "error": self.error, "logs": self.logs[-30:],
            "items": [{k: v for k, v in it.items() if not k.startswith("_")} for it in self.items],
        }

    @classmethod
    def from_dict(cls, d):
        j = cls.__new__(cls)
        j.id = d.get("id") or ""
        j.title = d.get("title") or "未命名"
        j.cover = d.get("cover") or ""
        j.owner = d.get("owner") or ""
        j.url = d.get("url") or ""
        j.options = d.get("options") or {}
        j.created = d.get("created") or now()
        j.status = d.get("status") or "queued"
        j.error = d.get("error") or ""
        j.logs = d.get("logs") or []
        j.cancel_token = dl.CancelToken()
        j.items = []
        for it in d.get("items") or []:
            it = dict(it)
            it["_token"] = dl.CancelToken()
            it.setdefault("progress", {"percent": 0, "bytes": 0, "total": 0, "speed": 0, "eta": 0})
            it.setdefault("stage", "")
            it.setdefault("error", "")
            it.setdefault("filesize", 0)
            it.setdefault("file", "")
            j.items.append(it)
        return j

    def snapshot(self):
        done = sum(1 for i in self.items if i["status"] == "done")
        err = sum(1 for i in self.items if i["status"] == "error")
        cur = next((i for i in self.items if i["status"] == "running"), None)
        # 总进度按时长加权：长视频不会因为「集数少」而一小格就跳到底
        durs = [max(1, int(i.get("duration") or 0)) for i in self.items]
        tot_d = float(sum(durs)) or 1.0
        acc = 0.0
        for k, it in enumerate(self.items):
            if it["status"] == "done":
                acc += durs[k]
            elif it["status"] == "running":
                acc += durs[k] * (float(it["progress"].get("percent") or 0) / 100.0)
        overall = acc / tot_d * 100
        return {
            "id": self.id,
            "title": self.title,
            "cover": self.cover,
            "owner": self.owner,
            "url": self.url,
            "status": self.status,
            "error": self.error,
            "created": self.created,
            "created_text": fmt_sec(self.created),
            "total": len(self.items),
            "done_count": done,
            "error_count": err,
            "percent": round(min(overall, 100), 1),
            "current_title": cur["title"] if cur else "",
            "items": [{k: v for k, v in it.items() if not k.startswith("_")} for it in self.items],
            "logs": self.logs[-40:],
        }


_NON_RETRY_WORDS = ("啥都木有", "不可见", "已下架", "地区", "会员", "付费", "订阅", "权限", "不存在", "未经允许")


def _retryable_error(msg):
    """权限/下架这类错误重试也没用，网络抖动、风控、分片错位才值得重试"""
    return not any(w in (msg or "") for w in _NON_RETRY_WORDS)


class JobManager:
    def __init__(self):
        self.jobs = {}
        self.order = []
        self._q = queue.Queue()
        self._lock = threading.RLock()
        self._workers = []
        self._seq = 0
        self._stop = False
        self._dirty = False
        self.tasks_path = os.path.join(DATA_DIR, "tasks.json")
        self._load_tasks()
        self._ensure_workers()
        threading.Thread(target=self._saver_loop, daemon=True, name="task-saver").start()

    def _load_tasks(self):
        """启动时把上次的任务捞回来：没下完的标成「已中断」，点继续就能接着下"""
        try:
            with open(self.tasks_path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        for jd in d.get("jobs") or []:
            try:
                j = Job.from_dict(jd)
            except Exception:
                continue
            if not j.id:
                continue
            if j.status in ("running", "queued"):
                j.status = "interrupted"
                j.error = "上次没有下完，点「继续」接着下"
                for it in j.items:
                    if it["status"] in ("running", "pending"):
                        it["status"] = "pending"
            self.jobs[j.id] = j
            self.order.append(j.id)
            try:
                self._seq = max(self._seq, int(j.id.split("_")[0][1:]))
            except Exception:
                pass

    def _save_tasks(self):
        with self._lock:
            data = {"v": 1, "jobs": []}
            for jid in self.order[:60]:
                j = self.jobs.get(jid)
                if j:
                    data["jobs"].append(j.to_dict())
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                tmp = self.tasks_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, self.tasks_path)
            except Exception:
                pass

    def _saver_loop(self):
        while not self._stop:
            time.sleep(1.5)
            try:
                live = any(j.status in ("running", "queued") for j in self.jobs.values())
                if live or self._dirty:
                    self._dirty = False
                    self._save_tasks()
            except Exception:
                pass

    def _mark_dirty(self):
        self._dirty = True

    def _ensure_workers(self):
        want = max(1, int(config.get("concurrency") or 2))
        with self._lock:
            alive = [w for w in self._workers if w.is_alive()]
            self._workers = alive
            while len(self._workers) < want:
                t = threading.Thread(target=self._worker_loop, daemon=True, name="worker-%d" % len(self._workers))
                t.start()
                self._workers.append(t)

    def add(self, parsed, options):
        with self._lock:
            self._seq += 1
            jid = "j%d_%d" % (self._seq, int(now()))
            job = Job(jid, parsed, options)
            if not job.items:
                raise BiliError("没有可下载的条目，请检查勾选")
            self.jobs[jid] = job
            self.order.insert(0, jid)
            self._dirty = True
            self._q.put(jid)
            self._ensure_workers()
            return job

    def get(self, jid):
        return self.jobs.get(jid)

    def list(self):
        with self._lock:
            return [self.jobs[j].snapshot() for j in self.order if j in self.jobs]

    def cancel(self, jid):
        job = self.jobs.get(jid)
        if not job:
            return False
        job.cancel_token.cancel()
        for it in job.items:
            tk = it.get("_token")
            if tk:
                tk.cancel()
        job.status = "cancelled"
        self._dirty = True
        job.log("已取消")
        for it in job.items:
            if it["status"] in ("pending", "running"):
                it["status"] = "cancelled"
        return True

    def cancel_item(self, jid, index):
        """取消单个条目：正在下的立刻断流，排队中的直接跳过"""
        job = self.jobs.get(jid)
        if not job:
            return False
        for it in job.items:
            if int(it.get("index") or 0) == int(index):
                tk = it.get("_token")
                if tk:
                    tk.cancel()
                if it["status"] in ("pending", "running"):
                    it["status"] = "cancelled"
                    job.log("已取消条目《%s》" % (it["title"][:36]))
                return True
        return False

    def clear_finished(self):
        with self._lock:
            for jid in list(self.order):
                j = self.jobs.get(jid)
                if j and j.status in ("done", "error", "cancelled", "interrupted"):
                    self.order.remove(jid)
                    self.jobs.pop(jid, None)
                    self._dirty = True

    def _worker_loop(self):
        while not self._stop:
            try:
                jid = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            job = self.jobs.get(jid)
            if not job or job.status == "cancelled":
                continue
            try:
                self._run_job(job)
            except Exception as e:
                job.status = "error"
                job.error = str(e)
                job.log("任务异常：%s" % e)

    # ---------------------------------------------------------------
    def _run_job(self, job):
        job.status = "running"
        self._dirty = True
        job.log("开始处理《%s》，共 %d 个条目" % (job.title, len(job.items)))
        opts = job.options
        cookie = config.get("cookie") or ""
        client = BiliClient(cookie_str=cookie, proxy=config.get("proxy") or None)

        out_root = config.download_dir()
        multi = len(job.items) > 1
        subdir = ""
        if multi and opts.get("sub_dir", True):
            subdir = make_safe_filename(job.title)
            out_root = os.path.join(out_root, subdir)
            os.makedirs(out_root, exist_ok=True)

        total_ok = 0
        # 任务内序号与组内序号（决定文件名编号）
        seq, gseq = 0, {}
        for it in job.items:
            seq += 1
            it["seq"] = seq
            g = it.get("group") or ""
            gseq[g] = gseq.get(g, 0) + 1
            it["seq_in_group"] = gseq[g]

        for it in job.items:
            if job.cancel_token.cancelled:
                break
            if it["status"] == "done":
                total_ok += 1
                continue
            if it["status"] == "cancelled":
                continue
            tk = it.get("_token")
            attempts = 0
            while True:
                if job.cancel_token.cancelled or (tk and tk.cancelled):
                    it["status"] = "cancelled"
                    break
                it["status"] = "running"
                it["error"] = ""
                it["progress"] = {"percent": 0, "bytes": 0, "total": 0, "speed": 0, "eta": 0}
                try:
                    self._download_one(job, it, client, out_root, multi)
                    it["status"] = "done"
                    total_ok += 1
                    break
                except dl.CancelTokenError:
                    it["status"] = "cancelled"
                    if job.cancel_token.cancelled:
                        job.status = "cancelled"
                        job.log("任务已取消")
                        return
                    break
                except Exception as e:
                    attempts += 1
                    msg = str(e)
                    if attempts >= 3 or not _retryable_error(msg):
                        it["status"] = "error"
                        it["error"] = msg
                        job.log("条目《%s》失败：%s" % (it["title"][:40], msg))
                        break
                    wait = 2 * attempts
                    job.log("《%s》第 %d 次失败，%d 秒后自动重试：%s"
                            % (it["title"][:30], attempts, wait, msg[:70]))
                    time.sleep(wait)

        if job.cancel_token.cancelled:
            job.status = "cancelled"
            job.log("任务已取消")
            return
        cancelled_n = sum(1 for i in job.items if i["status"] == "cancelled")
        err_n = sum(1 for i in job.items if i["status"] == "error")
        if err_n == 0:
            # 主动取消的条目不算失败
            if total_ok == 0 and cancelled_n > 0:
                job.status = "cancelled"
                job.log("全部条目已取消")
            else:
                job.status = "done"
                if cancelled_n:
                    job.log("完成 %d 个，取消 %d 个" % (total_ok, cancelled_n))
                else:
                    job.log("全部完成，共 %d 个文件" % total_ok)
        elif total_ok > 0:
            job.status = "error"
            job.error = "部分条目失败"
        else:
            job.status = "error"
            job.error = job.error or "全部条目失败"
        self._dirty = True


    # ---------------------------------------------------------------
    def _download_one(self, job, it, client, out_root, multi):
        opts = job.options
        cancel = it.get("_token") or job.cancel_token
        bvid, cid, aid, ep_id = it["bvid"], it["cid"], it["aid"], it["ep_id"]

        # 1. 补齐 cid
        if not cid:
            it["stage"] = "获取信息"
            if bvid:
                d = client.api("/x/web-interface/view", {"bvid": bvid})
                cid = (d.get("pages") or [{}])[0].get("cid") or d.get("cid")
                if not it["title"]:
                    it["title"] = d.get("title") or it["title"]
            elif ep_id:
                pgc = client.api("/pgc/view/web/season", {"ep_id": ep_id})
                for ep in pgc.get("episodes") or []:
                    if ep.get("id") == ep_id:
                        cid = ep.get("cid")
                        break
            if not cid:
                raise BiliError("无法获取 cid")

        # 2. 取流
        it["stage"] = "解析画质"
        max_h = int(opts.get("max_height") or config.get("max_height") or 1080)
        target_qn = qn_for_height(max_h)
        if not (config.get("cookie") or "").strip():
            # 未登录最高只有 720P，别从 1080/4K 一路白试，省时间也少挨风控
            target_qn = min(target_qn, 64)
        info = None
        last_err = None
        for qn in qn_candidates(target_qn):
            try:
                info = client.get_playinfo(bvid=bvid or None, cid=cid, aid=aid or None,
                                           ep_id=ep_id or None, qn=qn)
                if info.get("videos") or info.get("durl"):
                    break
            except BiliError as e:
                last_err = e
                info = None
                continue
        if not info or not (info.get("videos") or info.get("durl")):
            raise BiliError("取流失败：可能需要登录，或该视频有地区/会员限制（%s）" % (last_err or ""))

        ffmpeg = dl.find_ffmpeg(BASE_DIR)
        # 每个视频一个稳定缓存目录：中断后重开能从已下好的分片接着走
        tmp_dir = cache.dir_for(bvid, cid, ep_id)

        # 按来源（合集 / 多分P 视频）分组存放到子文件夹，避免混在一个目录里
        grp = it.get("group") or ""
        item_dir = out_root
        if grp:
            safe_grp = make_safe_filename(grp, 60)
            top = it.get("top_index") or 0
            dir_name = ("%02d %s" % (top, safe_grp)) if top else safe_grp
            if safe_grp and os.path.basename(out_root.rstrip("/")) != dir_name:
                item_dir = os.path.join(out_root, dir_name)
                os.makedirs(item_dir, exist_ok=True)

        # Windows 整条路径上限 260 字符，按当前目录长度动态压标题，避免写盘失败
        max_title = 84
        try:
            budget = 238 - len(os.path.abspath(out_root)) - 16
            max_title = max(20, min(60, budget))
        except Exception:
            pass
        title = make_safe_filename(it["title"] or bvid or "video", max_title)
        if it.get("group"):
            n_total = it.get("group_total") or it.get("seq_in_group") or 1
            width = max(2, len(str(n_total)))
            fname = ("%0*d %s" % (width, it.get("seq_in_group") or 1, title))
        elif multi:
            top = it.get("top_index") or 0
            n = top or it.get("seq") or it["index"]
            fname = ("%02d %s" % (n, title))
        else:
            fname = title

        # 文件整理方式：flat 平铺（默认）/ per_item 每条一个文件夹 / by_type 按类型分目录
        layout = (opts.get("layout") or config.get("output_layout") or "flat")
        stem = fname
        if layout == "per_item":
            item_dir = os.path.join(item_dir, make_safe_filename(fname, 80))
            os.makedirs(item_dir, exist_ok=True)
            stem = title
        video_dir = item_dir
        if layout == "by_type":
            video_dir = os.path.join(item_dir, "1 视频")
            os.makedirs(video_dir, exist_ok=True)

        def side_path(ext, kind):
            if layout == "by_type":
                d = os.path.join(item_dir, {"cover": "4 封面", "sub": "2 字幕", "dan": "3 弹幕"}[kind])
                os.makedirs(d, exist_ok=True)
            else:
                d = video_dir
            return os.path.join(d, stem + ext)

        audio_only = bool(opts.get("audio_only"))
        out_path = os.path.join(video_dir, stem + (".m4a" if audio_only else ".mp4"))

        # 目标文件已经在了就跳过，重复下同一个收藏夹不重复吃带宽
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            it["file"] = out_path
            it["filesize"] = os.path.getsize(out_path)
            it["progress"]["percent"] = 100
            it["stage"] = "已存在"
            job.log("已存在，跳过：%s" % os.path.basename(out_path))
            return

        # 真要下载了才建缓存目录；提前建会在跳过路径上留下空壳
        os.makedirs(tmp_dir, exist_ok=True)

        # 3. 下载
        if info.get("dash"):
            vstream = pick_video(info["videos"], max_h, opts.get("codec") or "avc")
            astream = pick_audio(info["audios"], opts.get("audio") or "best")
            if not vstream:
                raise BiliError("没有找到合适的视频流")

            qname = QUALITY_NAMES.get(vstream.get("id"), str(vstream.get("id")))
            it["stage"] = "下载中 · %s" % qname
            job.log("《%s》 画质 %s %sx%s %s" % (
                it["title"][:30], qname, vstream.get("width"), vstream.get("height"),
                vstream.get("codecs")))
            got_h = vstream.get("height") or 0
            if int(max_h or 0) >= 1080 and got_h and got_h < 1080:
                job.log("画质提示：你选的是 %dp，实际只拿到 %s（未登录最高 720P，点右上角扫码登录后可下 1080P 及以上）"
                        % (int(max_h), qname))

            if audio_only:
                # 只下音频
                if not astream:
                    raise BiliError("没有音频流")
                a_path = os.path.join(tmp_dir, "audio_%s.m4a" % (it.get("seq") or it["index"]))
                prog = dl.Progress(0, callback=self._mk_progress_cb(it))
                fetcher = dl.StreamFetcher(astream["url"], a_path,
                                           progress=prog, cancel=cancel,
                                           threads=int(config.get("threads_per_file") or 8),
                                           cache_dir=tmp_dir, tag="audio")
                fetcher.run()
                if cancel.cancelled:
                    raise dl.CancelTokenError()
                os.replace(a_path, out_path)
            else:
                v_path = os.path.join(tmp_dir, "video_%s.m4s" % (it.get("seq") or it["index"]))
                a_path = os.path.join(tmp_dir, "audio_%s.m4s" % (it.get("seq") or it["index"])) if astream else None

                v_total = 0
                if info.get("timelength") and vstream.get("bandwidth"):
                    # bandwidth: bit/s, timelength: ms -> bytes
                    v_total = int(vstream["bandwidth"] * info["timelength"] / 1000 / 8)
                a_total = 0
                if astream and info.get("timelength") and astream.get("bandwidth"):
                    a_total = int(astream["bandwidth"] * info["timelength"] / 1000 / 8)

                prog = dl.Progress(0, callback=self._mk_progress_cb(it))
                fv = dl.StreamFetcher(vstream["url"], v_path, progress=prog,
                                      cancel=cancel,
                                      threads=int(config.get("threads_per_file") or 8),
                                      cache_dir=tmp_dir, tag="video")
                fa = None
                if a_path:
                    fa = dl.StreamFetcher(a_stream_url(astream), a_path, progress=prog,
                                          cancel=cancel,
                                          threads=int(config.get("threads_per_file") or 8),
                                          cache_dir=tmp_dir, tag="audio")
                # 先把两条流的真实大小都量好，一次给全总量，避免音频开始下载时百分比往回掉
                v_sz = fv.probe_size()
                a_sz = fa.probe_size() if fa else 0
                if v_sz and (not fa or a_sz):
                    prog.add_total(v_sz + (a_sz or 0))
                    prog.freeze_total()
                fv.run()
                if cancel.cancelled:
                    raise dl.CancelTokenError()
                if fa:
                    fa.run()
                    if cancel.cancelled:
                        raise dl.CancelTokenError()

                it["stage"] = "合并音视频"
                dl.merge_av(ffmpeg, v_path, a_path, out_path, cancel=cancel)
                dl._silent_remove(v_path)
                if a_path:
                    dl._silent_remove(a_path)
                cache.drop(tmp_dir)
        else:
            # durl 老格式（低清单文件）
            it["stage"] = "下载中"
            durls = info.get("durl") or []
            if not durls:
                raise BiliError("没有可用下载地址")
            if len(durls) == 1:
                prog = dl.Progress(durls[0].get("size") or 0, callback=self._mk_progress_cb(it))
                f = dl.StreamFetcher(durls[0]["url"], out_path, progress=prog,
                                     cancel=cancel,
                                     threads=int(config.get("threads_per_file") or 8),
                                     cache_dir=tmp_dir, tag="durl")
                f.run()
            else:
                tmp_parts = []
                for i, u in enumerate(durls):
                    p = os.path.join(tmp_dir, "seg_%s_%d.flv" % (it.get("seq") or it["index"], i))
                    f = dl.StreamFetcher(u["url"], p, cancel=cancel, threads=4,
                                         cache_dir=tmp_dir, tag="seg%d" % i)
                    f.run()
                    tmp_parts.append(p)
                if ffmpeg:
                    lst = os.path.join(tmp_dir, "list_%s.txt" % (it.get("seq") or it["index"]))
                    with open(lst, "w", encoding="utf-8") as f2:
                        for p in tmp_parts:
                            f2.write("file '%s'\n" % p.replace("\\", "/"))
                    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat",
                                    "-safe", "0", "-i", lst, "-c", "copy", out_path],
                                   capture_output=True, timeout=1800)
                    for p in tmp_parts:
                        dl._silent_remove(p)
                else:
                    shutil.move(tmp_parts[0], out_path)
            cache.drop(tmp_dir)

        if cancel.cancelled:
            raise dl.CancelTokenError()

        it["file"] = out_path
        try:
            it["filesize"] = os.path.getsize(out_path)
        except Exception:
            it["filesize"] = 0
        it["progress"]["percent"] = 100
        it["stage"] = "完成"
        job.log("完成：%s (%s)" % (os.path.basename(out_path), human(it["filesize"])))
        try:
            records.mark(records.key(bvid, cid, ep_id), it["title"], out_path, it["filesize"])
        except Exception:
            pass

        # 4. 附加：封面
        if opts.get("cover", config.get("download_cover")):
            try:
                pic = it.get("cover") or job.cover
                if pic:
                    pic_path = side_path(".jpg", "cover")
                    self._save_url(pic, pic_path, client)
            except Exception:
                pass

        # 5. 字幕
        if opts.get("subtitle", config.get("download_subtitle")):
            try:
                subs = client.get_subtitles(bvid=bvid or None, cid=cid, aid=aid or None)
                for s in subs[:2]:
                    if not s.get("url"):
                        continue
                    js = json.loads(self._fetch_text(s["url"], client))
                    srt = side_path(".%s.srt" % (s.get("lan") or "zh"), "sub")
                    dl.subtitle_json_to_srt(js, srt)
                    job.log("字幕：%s" % os.path.basename(srt))
            except Exception as e:
                job.log("字幕下载跳过：%s" % e)

        # 6. 弹幕
        if opts.get("danmaku", config.get("download_danmaku")):
            try:
                xml = client.get_danmaku_xml(cid)
                if xml:
                    ass = side_path(".danmaku.ass", "dan")
                    dl.danmaku_xml_to_ass(xml, ass)
                    job.log("弹幕：%s" % os.path.basename(ass))
            except Exception as e:
                job.log("弹幕下载跳过：%s" % e)


    def _mk_progress_cb(self, it):
        def cb(p):
            it["progress"] = p
        return cb

    def _save_url(self, url, path, client):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": client.ua,
                "Referer": "https://www.bilibili.com/",
            })
            opener = client._opener or urllib.request.build_opener()
            with opener.open(req, timeout=30) as r, open(path, "wb") as f:
                shutil.copyfileobj(r, f)
            return path
        except Exception:
            return None

    def _fetch_text(self, url, client):
        req = urllib.request.Request(url, headers={
            "User-Agent": client.ua, "Referer": "https://www.bilibili.com/"})
        opener = client._opener or urllib.request.build_opener()
        with opener.open(req, timeout=30) as r:
            return r.read().decode("utf-8", "ignore")


def a_stream_url(a):
    return a.get("url")


# =====================================================================
# HTTP 服务
# =====================================================================
manager = JobManager()


class Handler(BaseHTTPRequestHandler):
    server_version = "%s/%s" % (APP_NAME, APP_VER)

    def log_message(self, fmt, *args):
        pass  # 静默

    # ---------------------------------------------------------------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _serve_file(self, path, ctype=None):
        if not os.path.isfile(path):
            self.send_error(404, "Not Found")
            return
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", ctype or mimetypes.guess_type(path)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
        except Exception:
            pass

    # ---------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/" or path == "/index.html":
                self._serve_file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
                return

            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                full = os.path.normpath(os.path.join(WEB_DIR, rel))
                if not full.startswith(os.path.abspath(WEB_DIR)):
                    self.send_error(403)
                    return
                self._serve_file(full)
                return

            if path == "/api/status":
                return self._api_status()
            if path == "/api/config":
                return self._send_json({"ok": True, "config": self._public_config()})
            if path == "/api/jobs":
                return self._send_json({"ok": True, "jobs": manager.list()})
            if path == "/api/library":
                return self._api_library()
            if path == "/api/login/qr":
                return self._api_qr_generate()
            if path == "/api/login/poll":
                key = (qs.get("key") or [""])[0]
                return self._api_qr_poll(key)
            if path == "/api/login/state":
                return self._api_login_state()
            if path == "/api/quality":
                return self._send_json({"ok": True, "qualities": [
                    {"value": 2160, "label": "4K / 最高"},
                    {"value": 1080, "label": "1080P"},
                    {"value": 720, "label": "720P"},
                    {"value": 480, "label": "480P"},
                    {"value": 360, "label": "360P"},
                ]})
            if path == "/api/cover":
                return self._api_cover((qs.get("url") or [""])[0])

            self.send_error(404, "Not Found")
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self._send_json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        body = self._read_json()

        try:
            if path == "/api/config":
                cfg = body.get("config") or {}
                if "download_dir" in cfg:
                    d = (cfg["download_dir"] or "").strip()
                    if d:
                        try:
                            os.makedirs(d, exist_ok=True)
                        except Exception as e:
                            return self._send_json({"ok": False, "error": "目录不可用：%s" % e}, 400)
                config.update(cfg)
                if "concurrency" in cfg:
                    manager._ensure_workers()
                return self._send_json({"ok": True, "config": self._public_config()})

            if path == "/api/parse":
                return self._api_parse(body)
            if path == "/api/jobs":
                return self._api_create_job(body)
            if path.startswith("/api/jobs/"):
                rest = path[len("/api/jobs/"):]
                jid, _, act = rest.partition("/")
                if act == "cancel":
                    ok = manager.cancel(jid)
                    return self._send_json({"ok": ok})
                if act == "cancel_item":
                    ok = manager.cancel_item(jid, body.get("index"))
                    return self._send_json({"ok": ok})
                if act == "retry":
                    return self._api_retry(jid)
            if path == "/api/jobs/clear":
                manager.clear_finished()
                return self._send_json({"ok": True})
            if path == "/api/pick_dir":
                picked = pick_folder()
                return self._send_json({"ok": True, "path": picked, "supported": os.name == "nt"})
            if path == "/api/open":
                p = body.get("path") or config.download_dir()
                if os.path.exists(p):
                    return self._send_json({"ok": open_folder(os.path.abspath(p))})
                return self._send_json({"ok": False, "error": "路径不存在"}, 400)
            if path == "/api/login/logout":
                config.set("cookie", "")
                return self._send_json({"ok": True})
            if path == "/api/login/cookie":
                ck = (body.get("cookie") or "").strip()
                if not ck:
                    return self._send_json({"ok": False, "error": "cookie 为空"}, 400)
                client = BiliClient(cookie_str=ck, proxy=config.get("proxy") or None)
                info = client.nav_info()
                if info.get("is_login"):
                    config.set("cookie", ck)
                    return self._send_json({"ok": True, "user": info})
                return self._send_json({"ok": False, "error": "cookie 无效或已过期"}, 400)
            if path == "/api/shutdown":
                threading.Timer(0.6, lambda: os._exit(0)).start()
                return self._send_json({"ok": True})

            self.send_error(404, "Not Found")
        except BiliError as e:
            return self._send_json({"ok": False, "error": str(e), "code": e.code}, 400)
        except Exception as e:
            traceback.print_exc()
            try:
                self._send_json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass

    # ---------------------------------------------------------------
    def _public_config(self):
        c = dict(config.data)
        c["download_dir"] = config.download_dir()
        base = os.path.basename(os.path.dirname(WEB_DIR))
        return c

    def _api_status(self):
        ff = dl.find_ffmpeg(BASE_DIR)
        cookie = config.get("cookie") or ""
        user = {"is_login": False}
        if cookie:
            try:
                client = BiliClient(cookie_str=cookie, proxy=config.get("proxy") or None)
                user = client.nav_info()
            except Exception:
                pass
        self._send_json({
            "ok": True,
            "app": APP_NAME,
            "version": APP_VER,
            "python": sys.version.split()[0],
            "ffmpeg": bool(ff),
            "ffmpeg_path": ff or "",
            "download_dir": config.download_dir(),
            "user": user,
            "platform": sys.platform,
        })

    def _api_parse(self, body):
        url = body.get("url") or ""
        if isinstance(url, list):
            url = url[0] if url else ""
        url = (url or "").strip()
        if not url:
            return self._send_json({"ok": False, "error": "请输入 B站 链接"}, 400)
        client = BiliClient(cookie_str=config.get("cookie") or "", proxy=config.get("proxy") or None)
        data = client.parse(url)
        data["ok"] = True
        # 标记哪些已经下过了，前端据此只勾选新增
        try:
            for it in data.get("items") or []:
                it["downloaded"] = records.has(it.get("bvid"), it.get("cid"), it.get("ep_id"))
        except Exception:
            pass
        # 附上画质名称映射，前端友好展示
        data["quality_names"] = {str(k): v for k, v in QUALITY_NAMES.items()}
        self._send_json(data)

    def _api_create_job(self, body):
        parsed = body.get("parsed") or {}
        options = body.get("options") or {}
        if not parsed.get("items"):
            return self._send_json({"ok": False, "error": "请先解析链接"}, 400)
        job = manager.add(parsed, options)
        self._send_json({"ok": True, "job": job.snapshot()})

    def _api_retry(self, jid):
        job = manager.get(jid)
        if not job:
            return self._send_json({"ok": False, "error": "任务不存在"}, 404)
        if job.status in ("running", "queued"):
            return self._send_json({"ok": False, "error": "任务还在进行中，先取消再重试"}, 400)
        job.error = ""
        job.cancel_token = dl.CancelToken()
        for it in job.items:
            if it["status"] in ("error", "cancelled", "pending", "running"):
                it["status"] = "pending"
                it["error"] = ""
                it["progress"] = {"percent": 0, "bytes": 0, "total": 0, "speed": 0, "eta": 0}
                # 条目自己的取消标记也要换掉，否则重试时会被旧标记秒判取消
                it["_token"] = dl.CancelToken()
        job.status = "queued"
        manager._dirty = True
        manager._q.put(job.id)
        return self._send_json({"ok": True, "job": job.snapshot()})

    def _api_cover(self, url):
        """代理加载 B站 封面图（B站 图片有防盗链，浏览器直连会裂图）"""
        if not url:
            return self.send_error(400, "missing url")
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except Exception:
            return self.send_error(400, "bad url")
        allowed = ("hdslb.com", "bilibili.com", "bilivideo.com", "biliimg.com")
        if not any(host.endswith(s) for s in allowed):
            return self.send_error(403, "domain not allowed")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": BROWSER_UA,
                "Referer": "https://www.bilibili.com/",
                "Accept": "image/*,*/*",
            })
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
                ctype = r.headers.get("Content-Type") or "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            try:
                self.send_error(404)
            except Exception:
                pass

    def _api_library(self):
        root = config.download_dir()
        entries = []
        try:
            for name in sorted(os.listdir(root), reverse=True)[:400]:
                full = os.path.join(root, name)
                if os.path.isdir(full):
                    n = 0
                    for dirpath, _, files in os.walk(full):
                        n += len([f for f in files if f.lower().endswith((".mp4", ".mkv", ".m4a", ".mp3", ".flv"))])
                    entries.append({"name": name, "type": "dir", "path": full, "count": n, "size": 0})
                elif name.lower().endswith((".mp4", ".mkv", ".m4a", ".mp3", ".flv", ".ass", ".srt", ".jpg", ".png")):
                    entries.append({"name": name, "type": "file", "path": full,
                                    "size": os.path.getsize(full)})
        except Exception:
            pass
        self._send_json({"ok": True, "root": root, "entries": entries[:200]})

    def _api_qr_generate(self):
        client = BiliClient(cookie_str=config.get("cookie") or "", proxy=config.get("proxy") or None)
        try:
            d = client.qr_generate()
        except BiliError as e:
            return self._send_json({"ok": False, "error": str(e)}, 400)
        self._send_json({"ok": True, "url": d["url"], "key": d["qrcode_key"]})

    def _api_qr_poll(self, key):
        if not key:
            return self._send_json({"ok": False, "error": "缺少 key"}, 400)
        client = BiliClient(proxy=config.get("proxy") or None)
        try:
            r = client.qr_poll(key)
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, 500)
        if r.get("status") == 0:
            cookie = r.get("cookie") or ""
            if "SESSDATA=" in cookie:
                # 只要拿到凭证就存下来：nav 校验有时会被风控误判，不能因此把登录结果丢掉
                config.set("cookie", cookie)
                info = {}
                try:
                    info = client.validate_cookie(cookie)
                except Exception:
                    info = {}
                if not info.get("is_login"):
                    info = {"uname": "已登录", "unverified": True}
                return self._send_json({"ok": True, "status": "success", "user": info})
            return self._send_json({
                "ok": True, "status": "fail",
                "message": "扫码已确认，但没拿到登录凭证，请刷新二维码重新扫一次",
            })
        self._send_json({"ok": True, "status": r.get("status"), "message": r.get("message")})

    def _api_login_state(self):
        cookie = config.get("cookie") or ""
        if not cookie:
            return self._send_json({"ok": True, "is_login": False})
        try:
            client = BiliClient(cookie_str=cookie, proxy=config.get("proxy") or None)
            info = client.nav_info()
            if info.get("is_login"):
                return self._send_json({"ok": True, "is_login": True, "user": info})
        except Exception:
            pass
        self._send_json({"ok": True, "is_login": False})


def find_free_port(start=8931, end=8999):
    for p in range(start, end):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            continue
    return 8931


def main():
    print("=" * 62)
    print("  BiliBox  ·  B站视频下载工具（个人离线备份）")
    print("=" * 62)
    print("  程序目录：%s" % BASE_DIR)
    print("  下载目录：%s" % config.download_dir())
    ff = dl.find_ffmpeg(BASE_DIR)
    print("  ffmpeg  ：%s" % (ff if ff else "未找到（无法合并音视频）"))
    print("  登录状态：%s" % ("已登录" if (config.get("cookie") or "") else "未登录（最高 720P）"))
    print("-" * 62)

    port = int(os.environ.get("BILIBOX_PORT") or 0) or find_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    url = "http://127.0.0.1:%d/" % port
    # 写入端口文件，方便启动脚本 / 外部工具定位
    try:
        rt = os.path.join(BASE_DIR, "runtime")
        os.makedirs(rt, exist_ok=True)
        with open(os.path.join(rt, "port.txt"), "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:
        pass
    print("  已启动：%s" % url)
    print("  浏览器没自动打开的话，手动复制上面的地址访问。")
    print("  关闭这个窗口即退出程序。")
    print("=" * 62)

    def open_browser():
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        try:
            server.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
