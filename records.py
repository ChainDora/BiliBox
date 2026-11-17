# -*- coding: utf-8 -*-
"""
BiliBox - 下载记录与续传缓存管理

records.json  记住哪些视频已经下好了，用于「已下载」标记和增量勾选
cache/        每个视频一个稳定目录，存分片和 manifest，中断后从这里接着下
"""

import hashlib
import json
import os
import shutil
import threading
import time


class Records:
    """已下载条目记录（键 = bvid:cid 或 ep:ep_id）"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self.data = {"v": 1, "items": {}}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("items"), dict):
                self.data = d
        except Exception:
            self.data = {"v": 1, "items": {}}

    def save(self):
        with self._lock:
            try:
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception:
                pass

    @staticmethod
    def key(bvid, cid, ep_id=0):
        if bvid and cid:
            return "%s:%s" % (bvid, cid)
        if ep_id:
            return "ep:%s" % ep_id
        return ""

    def mark(self, k, title, file, size, qual="", qual_text="", max_h=0):
        """记一条下载。

        qual 存「实际拿到的画质标签」（如 v720_AVC），跳过判断靠它区分
        同一个视频的不同画质；max_h 存当时选的档位（2160/1080/720…），
        前端靠它判断「这条在本次选的画质下算不算已下载」。
        """
        if not k:
            return
        with self._lock:
            self.data.setdefault("items", {})[k] = {
                "title": title or "",
                "file": file or "",
                "size": int(size or 0),
                "qual": qual or "",
                "qual_text": qual_text or "",
                "max_h": int(max_h or 0),
                "ts": int(time.time()),
            }
        self.save()

    def forget(self, k):
        with self._lock:
            self.data.get("items", {}).pop(k, None)
        self.save()

    def get(self, k):
        return self.data.get("items", {}).get(k)

    def has(self, bvid, cid, ep_id=0):
        k = self.key(bvid, cid, ep_id)
        return bool(k and k in self.data.get("items", {}))

    def count(self):
        return len(self.data.get("items", {}))


class Cache:
    """续传缓存目录，按视频稳定命名，不随任务顺序变化"""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    @staticmethod
    def name_for(bvid, cid, ep_id=0, tag=""):
        raw = "%s|%s|%s|%s" % (bvid or "", cid or 0, ep_id or 0, tag or "")
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    def dir_for(self, bvid, cid, ep_id=0, tag=""):
        """只算路径不建目录：避免「已存在跳过」时也留一个空壳目录"""
        return os.path.join(self.root, self.name_for(bvid, cid, ep_id, tag))

    def drop(self, path):
        """删掉一个缓存目录；只允许删 root 下面的，别的地方一概不理"""
        try:
            if not path or not os.path.isdir(path):
                return
            root = os.path.abspath(self.root)
            target = os.path.abspath(path)
            if not target.startswith(root + os.sep):
                return
            shutil.rmtree(target, ignore_errors=True)
        except Exception:
            pass

    def size_mb(self):
        total = 0
        try:
            for name in os.listdir(self.root):
                p = os.path.join(self.root, name)
                if os.path.isdir(p):
                    for dp, _, fs in os.walk(p):
                        for f in fs:
                            try:
                                total += os.path.getsize(os.path.join(dp, f))
                            except Exception:
                                pass
        except Exception:
            pass
        return total / 1048576.0

    def prune(self, max_age_days=7, max_mb=4096):
        """清掉很久没动或总量超标的缓存，防止无限增长"""
        try:
            entries = []
            for name in os.listdir(self.root):
                p = os.path.join(self.root, name)
                if not os.path.isdir(p):
                    continue
                mtime = os.path.getmtime(p)
                sz = 0
                for dp, _, fs in os.walk(p):
                    for f in fs:
                        try:
                            sz += os.path.getsize(os.path.join(dp, f))
                        except Exception:
                            pass
                entries.append((mtime, sz, p))
            now = time.time()
            for mtime, sz, p in entries:
                if max_age_days and (now - mtime) > max_age_days * 86400:
                    self.drop(p)
            total = sum(e[1] for e in entries if os.path.isdir(e[2]))
            if max_mb and total > max_mb * 1048576:
                for mtime, sz, p in sorted(entries):
                    if os.path.isdir(p):
                        self.drop(p)
                        total -= sz
                        if total <= max_mb * 1048576:
                            break
        except Exception:
            pass
