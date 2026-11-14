# -*- coding: utf-8 -*-
"""
BiliBox - 下载引擎
多线程分片下载 B站 DASH 流，ffmpeg 无损合并音视频。
纯标准库实现。
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

CHUNK_MIN = 2 * 1024 * 1024      # 最小分片 2MB
CHUNK_MAX = 6 * 1024 * 1024      # 最大分片 6MB
SINGLE_THRESHOLD = 8 * 1024 * 1024  # 小于 8MB 直接单流下载（实测小文件分片反而更慢）


class CancelTokenError(Exception):
    """用户取消"""
    pass


class CancelToken:
    def __init__(self):
        self._e = threading.Event()

    def cancel(self):
        self._e.set()

    @property
    def cancelled(self):
        return self._e.is_set()


class Progress:
    """线程安全的进度累加器"""

    def __init__(self, total_hint=0, callback=None):
        self.total = total_hint
        self.done = 0
        self._lock = threading.Lock()
        self._callback = callback
        self._last_emit = 0
        self._samples = []
        self._max_pct = 0.0
        self._frozen = False
        self.start_ts = time.time()

    def add(self, n):
        with self._lock:
            self.done += n
            now = time.time()
            if self._callback and (now - self._last_emit > 0.25):
                self._last_emit = now
                self._emit()

    def set_total(self, total):
        with self._lock:
            self.total = total

    def add_total(self, n):
        """累加总量（音视频分成两条流时各加一次）"""
        if not n or n <= 0:
            return
        with self._lock:
            if self._frozen:
                return
            self.total += n

    def sub(self, n):
        with self._lock:
            self.done = max(0, self.done - n)

    def rewind(self):
        """整文件重下：清零已计量，总量也归零（之后会重新探测并重新累加）"""
        with self._lock:
            self.done = 0
            self.total = 0
            self._samples = []
            self._max_pct = 0.0
            self._frozen = False

    def freeze_total(self):
        """总量已经一次给全（两条流都探过），后续流不要再往上加"""
        with self._lock:
            self._frozen = True

    def reset(self):
        with self._lock:
            self.total = 0
            self.done = 0
            self._samples = []
            self._max_pct = 0.0
            self._frozen = False
            self.start_ts = time.time()

    def _emit(self):
        now = time.time()
        elapsed = max(now - self.start_ts, 0.001)
        # 速度取最近 1.5 秒窗口，避免用全程平均导致数字大起大落
        self._samples.append((now, self.done))
        cutoff = now - 1.5
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.pop(0)
        speed = 0
        if len(self._samples) >= 2:
            dt = self._samples[-1][0] - self._samples[0][0]
            db = self._samples[-1][1] - self._samples[0][1]
            if dt > 0.05 and db > 0:
                speed = db / dt
        if speed <= 0:
            speed = self.done / elapsed
        if self.total > 0:
            pct = min(100.0, self.done / self.total * 100)
            # 只增不减：万一流切换或重探总长，进度条也不能往回缩
            if pct > self._max_pct:
                self._max_pct = pct
            else:
                pct = self._max_pct
            remain = max(0, int((self.total - self.done) / speed)) if speed > 0 else 0
        else:
            pct = 0
            remain = 0
        try:
            if self._callback:
                self._callback({
                    "bytes": self.done, "total": self.total, "percent": round(pct, 1),
                    "speed": int(speed), "eta": remain,
                })
        except Exception:
            pass

    def flush(self):
        if self._callback:
            self._emit()


def _headers(base=None):
    h = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Origin": "https://www.bilibili.com",
    }
    if base:
        h.update(base)
    return h


class StreamFetcher:
    """单个流（视频或音频）的下载器"""

    def __init__(self, url, dest, headers=None, progress=None, cancel=None, threads=8,
                 on_ua_fallback=None, cache_dir=None, tag=""):
        self.url = url
        self.dest = dest
        self.cache_dir = cache_dir      # 给了就走断点续传，分片和 manifest 落在这个目录
        self.tag = tag or "stream"
        self.headers = _headers(headers)
        self.progress = progress
        self.cancel = cancel or CancelToken()
        self.threads = max(1, min(16, threads))
        self.on_ua_fallback = on_ua_fallback
        self._errors = []

    # ---------------------------------------------------------------
    def _request(self, url, method="GET", range_hdr=None, timeout=30):
        h = dict(self.headers)
        if range_hdr:
            h["Range"] = range_hdr
        req = urllib.request.Request(url, method=method, headers=h)
        return urllib.request.urlopen(req, timeout=timeout)

    def probe_size(self):
        """探测文件真实总长度。
        B站 CDN 的 HEAD 响应 Content-Length 经常不准（小于真实大小），
        只有 Range 响应里的 Content-Range: bytes 0-1/总长 才是可信值。"""
        try:
            with self._request(self.url, range_hdr="bytes=0-1") as r:
                cr = r.headers.get("Content-Range")  # bytes 0-1/12345
                if cr and "/" in cr:
                    total = cr.split("/")[-1].strip()
                    if total.isdigit() and int(total) > 0:
                        return int(total)
        except Exception:
            pass
        if getattr(self, "_force_range", False):
            return 0
        try:
            with self._request(self.url, method="HEAD") as r:
                cl = r.headers.get("Content-Length")
                if cl and cl.strip().isdigit() and int(cl) > 0:
                    return int(cl)
        except Exception:
            pass
        return 0

    def _download_range(self, idx, start, end, part_path):
        """下载 [start, end] 到分片文件；长度对不上就重下这一片"""
        exp = end - start + 1
        last_err = None
        for attempt in range(3):
            if self.cancel.cancelled:
                return
            got = 0
            last_err = None
            try:
                with self._request(self.url, range_hdr="bytes=%d-%d" % (start, end), timeout=60) as r:
                    with open(part_path, "wb") as f:
                        while True:
                            if self.cancel.cancelled:
                                return
                            chunk = r.read(256 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            got += len(chunk)
                            if self.progress:
                                self.progress.add(len(chunk))
            except Exception as e:
                last_err = str(e)
            if self.cancel.cancelled:
                return
            if got == exp:
                return
            last_err = "片%d 长度 %d，应为 %d" % (idx, got, exp)
            if self.progress and got:
                self.progress.sub(got)       # 这片作废，把进度扣回去重算
            if attempt < 2:
                time.sleep(0.6)
        self._errors.append(last_err or ("片%d 失败" % idx))

    def _download_single(self):
        """整体下载；上次留下 .tmp 就用 Range 接着下"""
        tmp = self.dest + ".tmp"
        have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        rng = ("bytes=%d-" % have) if have > 0 else None
        try:
            with self._request(self.url, range_hdr=rng, timeout=60) as r:
                status = getattr(r, "status", 200)
                append = (have > 0 and status == 206)
                if append:
                    if self.progress:
                        self.progress.add(have)      # 已下过的部分也计进进度
                else:
                    have = 0
                with open(tmp, "ab" if append else "wb") as f:
                    while True:
                        if self.cancel.cancelled:
                            break
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        if self.progress:
                            self.progress.add(len(chunk))
        except Exception as e:
            _silent_remove(tmp)
            if self.cancel.cancelled:
                return False
            raise IOError("下载失败：%s" % e)
        if self.cancel.cancelled:
            _silent_remove(tmp)
            return False
        os.replace(tmp, self.dest)
        return True

    # ---------------------------------------------------------------
    def run(self):
        """下载。分片一直对不上就降线程重来，最后退成单线程整取"""
        attempts = 0
        while True:
            try:
                return self._run_once()
            except IOError as e:
                msg = str(e)
                if self.cancel.cancelled:
                    raise
                if "分片校验失败" not in msg and "下载失败" not in msg:
                    raise
                attempts += 1
                if attempts >= 3:
                    raise
                self._force_range = True
                self.threads = max(1, self.threads // 2)
                self._errors = []
                if self.progress:
                    self.progress.rewind()

    def _run_once(self):
        if os.path.exists(self.dest) and os.path.getsize(self.dest) > 0:
            sz = os.path.getsize(self.dest)
            if self.progress:
                self.progress.add_total(sz)
                self.progress.add(sz)
            return True

        size = self.probe_size()
        if size and self.progress:
            self.progress.add_total(size)

        if not size or size < SINGLE_THRESHOLD or self.threads <= 1:
            ok = self._download_single()
            return ok

        base_dir = os.path.dirname(self.dest) or "."
        stem = os.path.basename(self.dest)
        n = max(1, min(self.threads, (size + CHUNK_MIN - 1) // CHUNK_MIN))
        chunk_size = max(CHUNK_MIN, min(CHUNK_MAX, (size + n - 1) // n))

        parts = []
        expected = []
        segments = []
        start = 0
        idx = 0
        while start < size:
            end = min(start + chunk_size - 1, size - 1)
            part_path = os.path.join(base_dir, ".%s.part%d" % (stem, idx))
            parts.append(part_path)
            expected.append(end - start + 1)
            segments.append((idx, start, end, part_path))
            idx += 1
            start = end + 1

        # 断点：上次下完的片直接跳过（片大小对不上就丢弃重下）
        done_idx = set()
        man_path = None
        if self.cache_dir:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                man_path = os.path.join(self.cache_dir, "parts-%s.json" % self.tag)
                with open(man_path, "r", encoding="utf-8") as f:
                    man = json.load(f)
                if int(man.get("size") or 0) == size and int(man.get("chunk") or 0) == chunk_size:
                    done_idx = {int(i) for i in (man.get("done") or [])}
            except Exception:
                done_idx = set()
        valid = set()
        for i in done_idx:
            if 0 <= i < len(parts) and os.path.exists(parts[i]) and os.path.getsize(parts[i]) == expected[i]:
                valid.add(i)
        done_idx = valid
        for i in done_idx:
            if self.progress:
                self.progress.add(expected[i])

        _parts_lock = threading.Lock()

        # 固定线程池：片数可能比线程数多，不能一片起一个线程；已完成的片不再排队
        job_q = queue.Queue()
        for seg in segments:
            if seg[0] not in done_idx:
                job_q.put(seg)

        def _pump():
            while True:
                if self.cancel.cancelled:
                    return
                try:
                    i, s0, e0, p0 = job_q.get_nowait()
                except queue.Empty:
                    return
                self._download_range(i, s0, e0, p0)
                if man_path and os.path.exists(p0) and os.path.getsize(p0) == (e0 - s0 + 1):
                    done_idx.add(i)
                    with _parts_lock:
                        _write_parts_manifest(man_path, size, chunk_size, done_idx)

        threads = [threading.Thread(target=_pump, daemon=True)
                   for _ in range(max(1, min(self.threads, len(segments))))]
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                if self.cancel.cancelled:
                    t.join(timeout=1.5)   # 取消时别死等，卡住的线程让它自己退
                    break
                t.join(timeout=0.2)

        if self.cancel.cancelled:
            for p in parts:
                _silent_remove(p)
            return False

        if self._errors:
            for p in parts:
                _silent_remove(p)
            raise IOError("下载失败：%s" % (self._errors[0] if self._errors else "未知错误"))

        # 校验各分片大小
        for p, exp in zip(parts, expected):
            if not os.path.exists(p) or os.path.getsize(p) != exp:
                for q in parts:
                    _silent_remove(q)
                raise IOError("分片校验失败，请重试")

        # 拼接
        tmp = self.dest + ".jointmp"
        with open(tmp, "wb") as out:
            for p in parts:
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, out, 1024 * 1024)
        os.replace(tmp, self.dest)
        for p in parts:
            _silent_remove(p)
        if man_path:
            _silent_remove(man_path)
        return True


def _write_parts_manifest(path, size, chunk, done):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"size": int(size), "chunk": int(chunk), "done": sorted(int(i) for i in done)}, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _silent_remove(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ---------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------
def find_ffmpeg(base_dir):
    """按优先级查找 ffmpeg"""
    candidates = []
    if sys.platform.startswith("win"):
        candidates += [
            os.path.join(base_dir, "bin", "ffmpeg.exe"),
            os.path.join(base_dir, "runtime", "ffmpeg.exe"),
        ]
        exe = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
    else:
        candidates += [
            os.path.join(base_dir, "bin", "ffmpeg"),
            os.path.join(base_dir, "runtime", "ffmpeg"),
        ]
        exe = shutil.which("ffmpeg")
    if exe:
        candidates.append(exe)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def find_ffprobe(base_dir):
    ff = find_ffmpeg(base_dir)
    if not ff:
        return None
    probe = ff.replace("ffmpeg", "ffprobe")
    return probe if os.path.isfile(probe) else None


def merge_av(ffmpeg, video_path, audio_path, out_path, cancel=None, on_log=None):
    """无损合并视频和音频"""
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，无法合并音视频")

    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    cmd += ["-i", video_path]
    if audio_path:
        cmd += ["-i", audio_path, "-c", "copy", "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", out_path]

    flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                              creationflags=flags)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg 合并超时")
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 合并失败：%s" % ((proc.stderr or "")[:400]))
    if on_log:
        on_log("已合并：%s" % os.path.basename(out_path))
    return out_path


def extract_audio(ffmpeg, video_path, out_path, fmt="mp3", cancel=None):
    """从视频中提取音频"""
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，无法提取音频")
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", video_path]
    if fmt == "mp3":
        cmd += ["-vn", "-acodec", "libmp3lame", "-q:a", "2", out_path]
    else:
        cmd += ["-vn", "-c:a", "copy", out_path]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, creationflags=flags)
    if proc.returncode != 0:
        raise RuntimeError("音频提取失败：%s" % ((proc.stderr or "")[:300])
                           )
    return out_path


# ---------------------------------------------------------------------
# 字幕 / 弹幕
# ---------------------------------------------------------------------
def subtitle_json_to_srt(data, out_path):
    """B站字幕 JSON -> SRT"""
    body = data.get("body") if isinstance(data, dict) else data
    if not body:
        return None

    def ts(sec):
        total = int(round(float(sec) * 1000))
        h = total // 3600000
        m = (total % 3600000) // 60000
        s = (total % 60000) // 1000
        ms = total % 1000
        return "%02d:%02d:%02d,%03d" % (h, m, s, ms)

    lines = []
    for i, item in enumerate(body, 1):
        start = float(item.get("from", 0))
        end = float(item.get("to", start + 1))
        text = str(item.get("content", "")).strip()
        if not text:
            continue
        lines.append("%d\n%s --> %s\n%s\n" % (i, ts(start), ts(end), text))
    with open(out_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))
    return out_path


DANMAKU_RE = re.compile(r'<d p="([^"]+)"[^>]*>([^<]*)</d>')


def danmaku_xml_to_ass(xml_text, out_path, width=1920, height=1080, font_size=48, duration=8.0):
    """弹幕 XML -> ASS 字幕（滚动弹幕）"""
    if not xml_text:
        return None
    items = []
    for m in DANMAKU_RE.finditer(xml_text):
        p, text = m.group(1), m.group(2)
        arr = p.split(",")
        if len(arr) < 4:
            continue
        try:
            t = float(arr[0])
            mode = int(arr[1])
        except Exception:
            continue
        if mode not in (1, 2, 3):
            continue
        text = text.replace("{", "｛").replace("}", "｝").replace("\n", " ")
        if not text.strip():
            continue
        items.append((t, mode, text))

    items.sort(key=lambda x: x[0])

    def ass_ts(sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return "%d:%02d:%05.2f" % (h, m, s)

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: %d
PlayResY: %d
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: DM,Microsoft YaHei,%d,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" % (width, height, font_size)

    lines = [header]
    # 分轨道避免重叠
    lanes = 12
    lane_free = [0.0] * lanes
    for t, mode, text in items:
        if mode == 1:
            lane = 0
            for i in range(lanes):
                if lane_free[i] <= t:
                    lane = i
                    break
            else:
                lane = int(t) % lanes
            lane_free[lane] = t + duration * 0.6
            y = 20 + lane * (font_size + 12)
            eff = r"{\move(%d,%d,%d,%d)}" % (width, y, -int(len(text) * font_size * 0.8), y)
        else:
            y = height / 2 + 60
            eff = r"{\pos(%d,%d)}" % (width // 2, int(y))
        lines.append("Dialogue: 0,%s,%s,DM,,0,0,0,,%s%s" % (
            ass_ts(t), ass_ts(t + duration), eff, text))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def human_size(n):
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def human_time(sec):
    sec = int(sec or 0)
    if sec < 60:
        return "%d秒" % sec
    if sec < 3600:
        return "%d分%d秒" % (sec // 60, sec % 60)
    return "%d小时%d分" % (sec // 3600, (sec % 3600) // 60)
