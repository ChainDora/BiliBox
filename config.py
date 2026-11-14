# -*- coding: utf-8 -*-
"""BiliBox - 配置管理"""

import json
import os
import sys

DEFAULT_CONFIG = {
    "download_dir": "",
    "max_height": 1080,          # 画质上限
    "codec_pref": "avc",         # avc / hevc / av01 / auto
    "audio_quality": "best",     # best / 192 / 132 / 64
    "concurrency": 2,            # 同时下载的任务数
    "threads_per_file": 8,       # 单个文件的分片线程数
    "download_cover": True,
    "download_subtitle": False,
    "download_danmaku": False,
    "sub_dir_per_item": True,    # 多条内容时建子目录
    "output_layout": "flat",     # flat 平铺 / per_item 每条一个文件夹 / by_type 按类型分目录
    "cookie": "",
    "proxy": "",
    "theme": "light",
}


class Config:
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.path = os.path.join(base_dir, "config.json")
        self.data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self):
        try:
            if os.path.isfile(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    self.data.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
        except Exception:
            pass
        if not self.data.get("download_dir"):
            self.data["download_dir"] = os.path.join(self.base_dir, "downloads")

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        if key in DEFAULT_CONFIG:
            self.data[key] = value
            self.save()
            return True
        return False

    def update(self, patch):
        changed = False
        for k, v in (patch or {}).items():
            if k in DEFAULT_CONFIG:
                self.data[k] = v
                changed = True
        if changed:
            self.save()
        return changed

    def download_dir(self):
        d = self.data.get("download_dir") or os.path.join(self.base_dir, "downloads")
        os.makedirs(d, exist_ok=True)
        return d
