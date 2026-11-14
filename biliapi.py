# -*- coding: utf-8 -*-
"""
BiliBox - B站 API 客户端
纯标准库实现：指纹 Cookie、wbi 签名、链接解析、取流、登录。

只用于下载你自己有权访问的内容（个人离线备份）。
"""

import gzip
import hashlib
import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

# wbi 签名使用的固定混淆表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
# 备用 UA：部分机房/代理 IP 用浏览器 UA 会被风控，降级后可正常访问
FALLBACK_UA = "curl/8.5.0"

API = "https://api.bilibili.com"

# 画质编号 -> 名称
QUALITY_NAMES = {
    127: "8K 超高清",
    126: "杜比视界",
    125: "HDR 真彩",
    120: "4K 超清",
    116: "1080P60 高帧率",
    112: "1080P+ 高码率",
    80: "1080P 高清",
    74: "720P60 高帧率",
    64: "720P 高清",
    32: "480P 清晰",
    16: "360P 流畅",
    6: "240P 极速",
}

AUDIO_NAMES = {
    30216: "64K",
    30232: "132K",
    30280: "192K",
    30250: "杜比全景声",
    30251: "Hi-Res 无损",
}


class BiliError(Exception):
    """B站接口错误"""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class BiliClient:
    """负责所有 B站 接口交互"""

    def __init__(self, cookie_str="", proxy=None):
        self.cookie = cookie_str or ""
        self.ua = BROWSER_UA
        self.proxy = proxy
        self._wbi_mixin = None
        self._wbi_ts = 0
        self._lock = threading.RLock()
        self._buvid_ready = False
        self._opener = None
        self._last_headers = None

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------
    def _build_opener(self):
        handlers = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        self._opener = urllib.request.build_opener(*handlers)

    def _raw(self, url, referer="https://www.bilibili.com/", extra_headers=None, timeout=30):
        headers = {
            "User-Agent": self.ua,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if extra_headers:
            headers.update(extra_headers)

        if self._opener is None:
            self._build_opener()

        req = urllib.request.Request(url, headers=headers)
        with self._opener.open(req, timeout=timeout) as resp:
            self._last_headers = resp.headers
            data = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                data = gzip.decompress(data)
            elif "deflate" in enc:
                # B站弹幕接口用的是 raw deflate，不带 zlib 头
                try:
                    data = zlib.decompress(data)
                except Exception:
                    try:
                        data = zlib.decompress(data, -zlib.MAX_WBITS)
                    except Exception:
                        pass
            return resp.status, data

    def get_json(self, url, referer="https://www.bilibili.com/", extra_headers=None):
        """发请求并返回 JSON。遇到 HTTP 412/403 临时降级 UA 重试一次，结束即恢复。"""
        data = None
        saved_ua = None
        try:
            for attempt in range(2):
                try:
                    status, data = self._raw(url, referer, extra_headers)
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (412, 403) and self.ua != FALLBACK_UA and attempt == 0:
                        # 数据中心 IP 用浏览器 UA 常被判风控，仅本次请求临时降级
                        with self._lock:
                            saved_ua = self.ua
                            self.ua = FALLBACK_UA
                        continue
                    body = ""
                    try:
                        raw = e.read().decode("utf-8", "ignore")
                        body = "".join(ch for ch in raw if 32 <= ord(ch) < 0x4e00 or 0x4e00 <= ord(ch) <= 0x9fff)[:120]
                        body = body.strip()
                    except Exception:
                        pass
                    raise BiliError("HTTP %s：接口拒绝访问%s" % (
                        e.code, ("（%s）" % body) if body else ""), code=e.code)
                except urllib.error.URLError as e:
                    raise BiliError("网络连接失败：%s" % getattr(e, "reason", e))
                except Exception as e:
                    raise BiliError("请求异常：%s" % e)
        finally:
            if saved_ua is not None:
                with self._lock:
                    self.ua = saved_ua
        if data is None:
            raise BiliError("请求失败")

        try:
            return json.loads(data.decode("utf-8", "ignore"))
        except Exception:
            raise BiliError("返回内容无法解析为 JSON（可能被风控拦截）")

    def api(self, path, params=None, allow_fallback=True, **kw):
        """请求 B站 API。allow_fallback=False 时不做 UA 降级（取流接口只认浏览器 UA）"""
        saved_ua = None
        try:
            for attempt in range(2):
                url = API + path
                if params:
                    url += "?" + urllib.parse.urlencode(params)
                js = self.get_json(url, **kw)
                code = js.get("code")
                if code == 0:
                    return js.get("data") or js.get("result") or {}
                # -412 / -352 是 B站 风控码：仅对本次请求临时降级 UA 重试，结束即恢复
                if (allow_fallback and code in (-412, -352, -799)
                        and self.ua != FALLBACK_UA and attempt == 0):
                    with self._lock:
                        saved_ua = self.ua
                        self.ua = FALLBACK_UA
                    continue
                msg = js.get("message") or "未知错误"
                if code in (-412, -352, -799):
                    msg = "请求被 B站 风控拦截，请稍后重试，或配置代理 / 登录后再试"
                raise BiliError(msg, code=code)
        finally:
            if saved_ua is not None:
                with self._lock:
                    self.ua = saved_ua

    # ------------------------------------------------------------------
    # 指纹 cookie / wbi
    # ------------------------------------------------------------------
    def ensure_fingerprint(self):
        """补齐 buvid3 / buvid4 指纹 cookie，降低被风控概率"""
        if self._buvid_ready or "buvid3=" in self.cookie:
            self._buvid_ready = True
            return
        try:
            js = self.get_json(API + "/x/frontend/finger/spi")
            d = js.get("data") or {}
            b3, b4 = d.get("b_3"), d.get("b_4")
            parts = []
            if b3:
                parts.append("buvid3=" + b3)
            if b4:
                parts.append("buvid4=" + b4)
            parts.append("b_nut=%d" % int(time.time()))
            if parts:
                self.cookie = (self.cookie + "; " if self.cookie else "") + "; ".join(parts)
            self._buvid_ready = True
        except Exception:
            pass

    def _get_wbi_mixin(self, force=False):
        with self._lock:
            if self._wbi_mixin and not force and (time.time() - self._wbi_ts) < 3600:
                return self._wbi_mixin
            js = self.get_json(API + "/x/web-interface/nav")
            wbi = ((js.get("data") or {}).get("wbi_img")) or {}
            img_url = wbi.get("img_url") or ""
            sub_url = wbi.get("sub_url") or ""
            img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
            sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
            if not img_key or not sub_key:
                raise BiliError("获取 wbi 签名密钥失败")
            raw = img_key + sub_key
            self._wbi_mixin = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
            self._wbi_ts = time.time()
            return self._wbi_mixin

    def wbi_query(self, params):
        """对参数做 wbi 签名，返回查询字符串"""
        for attempt in range(2):
            try:
                mixin = self._get_wbi_mixin(force=(attempt > 0))
                p = dict(params)
                p["wts"] = int(time.time())
                q = urllib.parse.urlencode(sorted(p.items()))
                p["w_rid"] = hashlib.md5((q + mixin).encode()).hexdigest()
                return urllib.parse.urlencode(p)
            except BiliError:
                if attempt:
                    raise

    # ------------------------------------------------------------------
    # 登录状态
    # ------------------------------------------------------------------
    def nav_info(self):
        try:
            js = self.get_json(API + "/x/web-interface/nav")
            d = js.get("data") or {}
            if js.get("code") == 0 and d.get("isLogin"):
                return {
                    "is_login": True,
                    "uname": d.get("uname") or "",
                    "mid": d.get("mid"),
                    "vip": bool(d.get("vipStatus")) or bool((d.get("vipInfo") or {}).get("status")),
                    "face": d.get("face") or "",
                    "level": (d.get("level_info") or {}).get("current_level"),
                }
            return {"is_login": False}
        except Exception:
            return {"is_login": False}

    # ------------------------------------------------------------------
    # 链接解析
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_url(url):
        url = (url or "").strip()
        if not url:
            raise BiliError("请输入链接")
        if not re.match(r"^https?://", url, re.I):
            # 允许直接输入 BV 号
            if re.match(r"^(BV[0-9A-Za-z]+|av\d+)", url, re.I):
                return "https://www.bilibili.com/video/" + url
            raise BiliError("链接格式不正确，需要 http:// 或 https:// 开头")
        return url

    def resolve_shortlink(self, url):
        """b23.tv 短链跟随重定向"""
        if "b23.tv" not in url:
            return url
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Referer": "https://www.bilibili.com/"})
            opener = self._opener or urllib.request.build_opener()
            with opener.open(req, timeout=20) as resp:
                return resp.geturl()
        except Exception:
            return url

    def parse(self, url, limit=0):
        """解析任意 B站 链接，返回统一结构"""
        self.ensure_fingerprint()
        url = self.normalize_url(url)
        url = self.resolve_shortlink(url)

        u = urllib.parse.urlparse(url)
        path = u.path or ""
        qs = urllib.parse.parse_qs(u.query)

        # 1) 番剧 / 影视
        m = re.search(r"/bangumi/play/(ss|ep)(\d+)", path)
        if m:
            return self._parse_bangumi(m.group(1), int(m.group(2)), limit)

        # 2) 视频（含分P、含合集信息）
        m = re.search(r"/video/(BV[0-9A-Za-z]+|av\d+)", path)
        if m:
            bvid = m.group(1)
            p = int(qs.get("p", ["1"])[0])
            return self._parse_video(bvid, p, limit)

        # 3) UP主合集 / 系列
        m = re.search(r"space\.bilibili\.com/(\d+)/channel/(collectiondetail|seriesdetail)", url)
        if m:
            mid = int(m.group(1))
            sid = int(qs.get("sid", ["0"])[0])
            if m.group(2) == "collectiondetail":
                return self._parse_season(mid, sid, limit)
            return self._parse_series(mid, sid, limit)

        # 4) UP主空间 / 投稿
        m = re.search(r"space\.bilibili\.com/(\d+)", url)
        if m:
            mid = int(m.group(1))
            return self._parse_space(mid, limit)

        # 5) 收藏夹（支持 /medialist/detail/ml、/favlist、新版 /list/ml<id>）
        if re.search(r"/favlist|/medialist/(detail|ml)|/list/ml\d+", url):
            fid = None
            if "fid=" in u.query:
                fid = int(qs.get("fid", ["0"])[0])
            else:
                mm = re.search(r"/(?:detail/)?ml(\d+)", path)
                if mm:
                    fid = int(mm.group(1))
            if not fid:
                raise BiliError("无法识别收藏夹 ID")
            return self._parse_favlist(fid, limit)

        # 6) 稍后再看 / 历史
        if "/list/watchlater" in url:
            return self._parse_watchlater(limit)
        if "/list/history" in url:
            return self._parse_history(limit)

        raise BiliError("暂不支持这种链接。请粘贴视频、番剧、合集、UP主空间或收藏夹页面链接。")

    # ------------------------------------------------------------------
    def _cover(self, s):
        if not s:
            return ""
        if s.startswith("//"):
            return "https:" + s
        if s.startswith("http://"):
            return "https://" + s[7:]
        return s

    def _parse_video(self, bvid, want_p=1, limit=0):
        d = self.api("/x/web-interface/view", {"bvid": bvid})
        pages = d.get("pages") or []
        base = {
            "platform": "bilibili",
            "title": d.get("title") or "",
            "cover": self._cover(d.get("pic")),
            "owner": (d.get("owner") or {}).get("name") or "",
            "owner_mid": (d.get("owner") or {}).get("mid"),
            "pubdate": d.get("pubdate"),
            "desc": (d.get("desc") or "")[:500],
            "aid": d.get("aid"),
            "bvid": d.get("bvid"),
            "duration": d.get("duration") or 0,
            "has_season": bool(d.get("ugc_season")),
        }

        # 多 P 视频：每个分 P 是一个条目
        if len(pages) > 1:
            items = []
            for i, pg in enumerate(pages, 1):
                items.append({
                    "bvid": d.get("bvid"),
                    "cid": pg.get("cid"),
                    "aid": d.get("aid"),
                    "index": i,
                    "title": pg.get("part") or ("P%d" % i),
                    "duration": pg.get("duration") or 0,
                    "cover": base["cover"],
                    "kind": "page",
                })
            base["type"] = "video_pages"
            base["items"] = items[:limit] if limit else items
            base["total"] = len(items)
            base["url"] = "https://www.bilibili.com/video/%s" % d.get("bvid")
            return base

        # 单 P
        cid = pages[0].get("cid") if pages else d.get("cid")
        base["type"] = "video"
        base["items"] = [{
            "bvid": d.get("bvid"),
            "cid": cid,
            "aid": d.get("aid"),
            "index": 1,
            "title": d.get("title") or "",
            "duration": d.get("duration") or 0,
            "cover": base["cover"],
            "kind": "video",
        }]
        base["total"] = 1
        base["url"] = "https://www.bilibili.com/video/%s" % d.get("bvid")

        # 附带合集信息（供用户选择是否下载整个合集）
        season = d.get("ugc_season")
        if season:
            eps = []
            idx = 0
            for sec in (season.get("sections") or []):
                for ep in (sec.get("episodes") or []):
                    idx += 1
                    arc = ep.get("arc") or {}
                    eps.append({
                        "bvid": ep.get("bvid"),
                        "cid": ep.get("cid"),
                        "aid": ep.get("aid"),
                        "index": idx,
                        "title": ep.get("title") or arc.get("title") or "",
                        "duration": arc.get("duration") or ep.get("duration") or 0,
                        "cover": self._cover(arc.get("pic")),
                        "kind": "season",
                    })
            if len(eps) > 1:
                base["season"] = {
                    "id": season.get("id"),
                    "title": season.get("title") or "",
                    "count": len(eps),
                    "items": eps,
                }
        return base

    def _parse_season(self, mid, sid, limit=0):
        items, page = [], 1
        while True:
            d = self.api("/x/polymer/web-space/seasons_archives_list",
                         {"mid": mid, "season_id": sid, "page_num": page, "page_size": 30})
            arcs = d.get("archives") or []
            for a in arcs:
                items.append({
                    "bvid": a.get("bvid"),
                    "cid": a.get("cid") or 0,
                    "aid": a.get("aid"),
                    "index": len(items) + 1,
                    "title": a.get("title") or "",
                    "duration": a.get("duration") or 0,
                    "cover": self._cover(a.get("pic")),
                    "kind": "season",
                })
            meta = d.get("meta") or {}
            total = meta.get("total") or len(items)
            if len(items) >= total or not arcs:
                break
            page += 1
            if page > 50:
                break
        return {
            "platform": "bilibili",
            "type": "season",
            "title": ((d.get("meta") or {}).get("name") or "合集"),
            "cover": self._cover((d.get("meta") or {}).get("cover")),
            "owner": "",
            "desc": (d.get("meta") or {}).get("description") or "",
            "items": items[:limit] if limit else items,
            "total": len(items),
            "url": "https://space.bilibili.com/%d/channel/collectiondetail?sid=%d" % (mid, sid),
        }

    def _parse_series(self, mid, sid, limit=0):
        items, page = [], 1
        while True:
            d = self.api("/x/series/archives", {"mid": mid, "series_id": sid, "pn": page, "ps": 30})
            arcs = (d.get("archives") or [])
            for a in arcs:
                items.append({
                    "bvid": a.get("bvid"),
                    "cid": a.get("cid") or 0,
                    "aid": a.get("aid"),
                    "index": len(items) + 1,
                    "title": a.get("title") or "",
                    "duration": int((a.get("duration") or 0)) if str(a.get("duration") or "").isdigit() else 0,
                    "cover": self._cover(a.get("pic")),
                    "kind": "series",
                })
            total = (d.get("page") or {}).get("total") or len(items)
            if len(items) >= total or not arcs:
                break
            page += 1
            if page > 50:
                break
        return {
            "platform": "bilibili",
            "type": "series",
            "title": "系列",
            "cover": "",
            "owner": "",
            "desc": "",
            "items": items[:limit] if limit else items,
            "total": len(items),
            "url": "https://space.bilibili.com/%d/channel/seriesdetail?sid=%d" % (mid, sid),
        }

    def _parse_space(self, mid, limit=0):
        """UP主投稿列表。优先 wbi 接口，被风控时自动回退到旧接口。"""
        items, page = [], 1
        mode = "wbi"
        while True:
            try:
                if mode == "wbi":
                    qs = self.wbi_query({
                        "mid": mid, "ps": 30, "pn": page, "order": "pubdate",
                        "index": 0, "platform": "web",
                    })
                    js = self.get_json(API + "/x/space/wbi/arc/search?" + qs,
                                       referer="https://space.bilibili.com/%d/video" % mid)
                else:
                    qs = urllib.parse.urlencode({
                        "mid": mid, "ps": 30, "pn": page, "order": "pubdate", "keyword": "",
                    })
                    js = self.get_json(API + "/x/space/arc/search?" + qs,
                                       referer="https://space.bilibili.com/%d/video" % mid)
            except BiliError:
                if mode == "wbi":
                    mode = "old"
                    continue
                raise
            if js.get("code") != 0:
                if mode == "wbi":
                    mode = "old"
                    continue
                raise BiliError(js.get("message") or "获取 UP主投稿失败", code=js.get("code"))

            d = js.get("data") or {}
            vlist = ((d.get("list") or {}).get("vlist")) or []
            for a in vlist:
                items.append({
                    "bvid": a.get("bvid"),
                    "cid": a.get("cid") or 0,
                    "aid": a.get("aid"),
                    "index": len(items) + 1,
                    "title": a.get("title") or "",
                    "duration": self._dur(a.get("length")),
                    "cover": self._cover(a.get("pic")),
                    "kind": "space",
                })
            total = (d.get("page") or {}).get("count") or len(items)
            if len(items) >= total or not vlist:
                break
            page += 1
            if page > 60:
                break
        if not items:
            raise BiliError("没有获取到该 UP主 的投稿。可能需要在设置中扫码登录后再试。")
        up_name = ""
        try:
            up_name = (vlist[0].get("author") if vlist else "") or ""
        except Exception:
            up_name = ""
        if not up_name:
            try:
                acc = self.api("/x/space/acc/info", {"mid": mid}, referer="https://space.bilibili.com/")
                up_name = (acc.get("name") or "")
            except Exception:
                up_name = ""
        return {
            "platform": "bilibili",
            "type": "space",
            "title": ("%s 的投稿" % up_name) if up_name else "UP主投稿",
            "cover": items[0]["cover"] if items else "",
            "owner": up_name,
            "desc": "共 %d 个投稿" % len(items),
            "items": items[:limit] if limit else items,
            "total": len(items),
            "url": "https://space.bilibili.com/%d/video" % mid,
        }

    @staticmethod
    def _dur(v):
        if not v:
            return 0
        if isinstance(v, (int, float)):
            return int(v)
        parts = str(v).split(":")
        try:
            nums = [int(x) for x in parts]
        except Exception:
            return 0
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        return 0

    def _expand_media(self, bvid, fallback=None):
        """把收藏夹/投稿条目展开为可下载的分P或合集分集。

        返回 (items, group_name)。group_name 用于下载时按来源分文件夹。
        """
        fallback = fallback or {}
        try:
            d = self.api("/x/web-interface/view", {"bvid": bvid})
        except Exception:
            return ([{
                "bvid": bvid, "cid": 0, "aid": fallback.get("aid") or 0, "index": 1,
                "title": fallback.get("title") or "", "duration": fallback.get("duration") or 0,
                "cover": fallback.get("cover") or "", "kind": fallback.get("kind") or "fav",
                "group": "", "group_total": 0,
            }], "")

        title = d.get("title") or ""
        cover = self._cover(d.get("pic"))
        aid = d.get("aid") or 0
        pages = d.get("pages") or []
        season = d.get("ugc_season") or {}

        # 1) UP主合集：多集、每集一个 bvid
        eps = []
        for sec in (season.get("sections") or []):
            for e in (sec.get("episodes") or []):
                eps.append(e)
        if season and len(eps) > 1:
            group = season.get("title") or title
            out = []
            for i, e in enumerate(eps, 1):
                arc = e.get("arc") or {}
                out.append({
                    "bvid": e.get("bvid"),
                    "cid": e.get("cid") or 0,
                    "aid": e.get("aid") or arc.get("aid") or 0,
                    "index": i,
                    "title": e.get("title") or arc.get("title") or ("第%d集" % i),
                    "duration": e.get("duration") or arc.get("duration") or 0,
                    "cover": self._cover(arc.get("pic") or cover),
                    "kind": "season_ep",
                    "group": group,
                    "group_total": len(eps),
                })
            return out, group

        # 2) 多分P：同一 bvid，不同 cid
        if len(pages) > 1:
            group = title
            out = []
            for i, p in enumerate(pages, 1):
                out.append({
                    "bvid": bvid, "cid": p.get("cid") or 0, "aid": aid, "index": i,
                    "title": p.get("part") or ("P%d" % i),
                    "duration": p.get("duration") or 0, "cover": cover,
                    "kind": "fav_page", "group": group, "group_total": len(pages),
                })
            return out, group

        # 3) 单条
        return ([{
            "bvid": bvid, "cid": d.get("cid") or 0, "aid": aid, "index": 1,
            "title": title, "duration": d.get("duration") or 0, "cover": cover,
            "kind": fallback.get("kind") or "fav", "group": "", "group_total": 0,
        }], "")

    def _parse_favlist(self, fid, limit=0):
        items, page = [], 1
        title, cover = "收藏夹", ""
        owner_name = ""
        top_pos = 0
        while True:
            d = self.api("/x/v3/fav/resource/list",
                         {"media_id": fid, "pn": page, "ps": 20, "keyword": "", "order": "mtime",
                          "type": 0, "tid": 0, "platform": "web"},
                         referer="https://www.bilibili.com/")
            info = d.get("info") or {}
            if page == 1:
                title = info.get("title") or "收藏夹"
                cover = self._cover(info.get("cover"))
                owner_name = (info.get("upper") or {}).get("name") or ""
                if owner_name and owner_name not in title:
                    title = "%s的「%s」收藏夹" % (owner_name, title)
            medias = d.get("medias") or []
            for a in medias:
                if a.get("type") != 2 and not a.get("bvid"):
                    continue  # 跳过失效稿件
                bvid = a.get("bvid")
                if not bvid:
                    continue
                top_pos += 1
                sub, _g = self._expand_media(bvid, {
                    "title": a.get("title") or "",
                    "duration": self._dur(a.get("duration")),
                    "cover": self._cover(a.get("cover")),
                    "kind": "fav",
                })
                for si in sub:
                    si["index"] = len(items) + 1
                    si["top_index"] = top_pos
                    items.append(si)
            if not d.get("has_more"):
                break
            page += 1
            if page > 50:
                break
        return {
            "platform": "bilibili",
            "type": "favlist",
            "title": title,
            "cover": cover,
            "owner": owner_name,
            "desc": "",
            "items": items[:limit] if limit else items,
            "total": len(items),
            "url": "",
        }

    def _parse_watchlater(self, limit=0):
        d = self.api("/x/v2/history/toview", referer="https://www.bilibili.com/")
        items = []
        for a in (d.get("list") or []):
            items.append({
                "bvid": a.get("bvid"),
                "cid": (a.get("cid") or 0),
                "aid": a.get("aid"),
                "index": len(items) + 1,
                "title": a.get("title") or "",
                "duration": a.get("duration") or 0,
                "cover": self._cover(a.get("pic")),
                "kind": "watchlater",
            })
        return {"platform": "bilibili", "type": "watchlater", "title": "稍后再看", "cover": "",
                "owner": "", "desc": "", "items": items[:limit] if limit else items,
                "total": len(items), "url": ""}

    def _parse_history(self, limit=0):
        items, page = [], 1
        while True:
            d = self.api("/x/v2/history", {"pn": page, "ps": 30}, referer="https://www.bilibili.com/")
            lst = d.get("list") or []
            for a in lst:
                items.append({
                    "bvid": a.get("bvid"),
                    "cid": a.get("cid") or 0,
                    "aid": a.get("aid"),
                    "index": len(items) + 1,
                    "title": a.get("title") or "",
                    "duration": a.get("duration") or 0,
                    "cover": self._cover(a.get("pic")),
                    "kind": "history",
                })
            if not lst:
                break
            page += 1
            if page > 30:
                break
        return {"platform": "bilibili", "type": "history", "title": "观看历史", "cover": "",
                "owner": "", "desc": "", "items": items[:limit] if limit else items,
                "total": len(items), "url": ""}

    def _parse_bangumi(self, kind, sid, limit=0):
        if kind == "ss":
            d = self.api("/pgc/view/web/season", {"season_id": sid},
                        referer="https://www.bilibili.com/bangumi/play/ss%d" % sid)
        else:
            d = self.api("/pgc/view/web/season", {"ep_id": sid},
                        referer="https://www.bilibili.com/bangumi/play/ep%d" % sid)
        title = d.get("title") or ""
        cover = self._cover(d.get("cover"))
        items = []
        for i, ep in enumerate(d.get("episodes") or [], 1):
            items.append({
                "bvid": ep.get("bvid") or "",
                "cid": ep.get("cid"),
                "ep_id": ep.get("id") or ep.get("ep_id"),
                "aid": ep.get("aid"),
                "index": ep.get("ep") or i,
                "title": ep.get("title") or ep.get("long_title") or ("第%d话" % i),
                "long_title": ep.get("long_title") or "",
                "duration": ep.get("duration") or 0,
                "cover": self._cover(ep.get("pic")) or cover,
                "kind": "bangumi",
            })
        if not items:
            total = d.get("total") or 0
            if total:
                raise BiliError(
                    "这部剧共 %d 集，但当前未登录，拿不到剧集列表。"
                    "请点右上角扫码登录（大会员账号可下付费内容）后重试。" % total)
            raise BiliError("没有获取到剧集列表。该剧可能需要大会员，或内容尚未上线。")
        return {
            "platform": "bilibili",
            "type": "bangumi",
            "title": title,
            "cover": cover,
            "owner": "",
            "desc": (d.get("evaluate") or "")[:500],
            "season_id": d.get("season_id"),
            "items": items[:limit] if limit else items,
            "total": len(items),
            "url": "https://www.bilibili.com/bangumi/play/ss%s" % (d.get("season_id") or sid),
        }

    # ------------------------------------------------------------------
    # 取流
    # ------------------------------------------------------------------
    def get_playinfo(self, bvid=None, cid=None, aid=None, ep_id=None, qn=127, audio_only=False):
        """获取 DASH 播放信息"""
        self.ensure_fingerprint()
        if ep_id:
            qs = self.wbi_query({
                "ep_id": ep_id, "cid": cid or 0, "qn": qn, "fnval": 4048,
                "fnver": 0, "fourk": 1,
            })
            path = "/pgc/player/web/playurl?" + qs
        else:
            params = {"cid": cid, "qn": qn, "fnval": 4048, "fnver": 0, "fourk": 1}
            if bvid:
                params["bvid"] = bvid
            else:
                params["avid"] = aid
            qs = self.wbi_query(params)
            path = "/x/player/wbi/playurl?" + qs

        d = self.api(path, allow_fallback=False, referer="https://www.bilibili.com/")
        if not d.get("dash") and not d.get("durl"):
            raise BiliError("取流失败：可能需要登录或该内容不可访问")

        result = {"quality": d.get("quality"), "timelength": d.get("timelength"),
                  "videos": [], "audios": [], "durl": [], "dash": bool(d.get("dash"))}

        if d.get("dash"):
            dash = d["dash"]
            for v in dash.get("video") or []:
                result["videos"].append({
                    "id": v.get("id"),
                    "width": v.get("width"), "height": v.get("height"),
                    "codecs": v.get("codecs"), "fps": v.get("frameRate"),
                    "bandwidth": v.get("bandwidth"),
                    "size_mb": round((v.get("bandwidth") or 0) / 8 / 1024 / 1024, 1),
                    "url": v.get("baseUrl") or (v.get("backupUrl") or [None])[0],
                    "backup": v.get("backupUrl") or [],
                })
            for a in dash.get("audio") or []:
                result["audios"].append({
                    "id": a.get("id"), "codecs": a.get("codecs"),
                    "bandwidth": a.get("bandwidth"),
                    "size_mb": round((a.get("bandwidth") or 0) / 8 / 1024 / 1024, 1),
                    "url": a.get("baseUrl") or (a.get("backupUrl") or [None])[0],
                    "backup": a.get("backupUrl") or [],
                })
            # 杜比 / 无损
            for key in ("dolby", "flac"):
                if dash.get(key):
                    item = dash[key]
                    auds = item.get("audio") if isinstance(item, dict) else None
                    if auds:
                        for a in auds:
                            result["audios"].append({
                                "id": a.get("id"), "codecs": a.get("codecs"),
                                "bandwidth": a.get("bandwidth"),
                                "size_mb": round((a.get("bandwidth") or 0) / 8 / 1024 / 1024, 1),
                                "url": a.get("baseUrl") or (a.get("backupUrl") or [None])[0],
                                "backup": a.get("backupUrl") or [],
                            })
        else:
            for u in d.get("durl") or []:
                result["durl"].append({
                    "order": u.get("order"), "url": u.get("url"),
                    "size": u.get("size"), "length": u.get("length"),
                })

        return result

    # ------------------------------------------------------------------
    # 字幕 / 弹幕
    # ------------------------------------------------------------------
    def get_subtitles(self, bvid=None, cid=None, aid=None):
        try:
            params = {"cid": cid}
            if bvid:
                params["bvid"] = bvid
            else:
                params["aid"] = aid
            d = self.api("/x/player/wbi/v2", params, referer="https://www.bilibili.com/")
        except BiliError:
            return []
        out = []
        for s in ((d.get("subtitle") or {}).get("subtitles")) or []:
            out.append({
                "lan": s.get("lan"), "lan_doc": s.get("lan_doc"),
                "url": s.get("subtitle_url") or "",
            })
        for s in out:
            if s["url"] and s["url"].startswith("//"):
                s["url"] = "https:" + s["url"]
        return out

    def get_danmaku_xml(self, cid):
        """返回弹幕 XML 文本"""
        url = "https://api.bilibili.com/x/v1/dm/list.so?oid=%s" % cid
        try:
            status, data = self._raw(url, referer="https://www.bilibili.com/")
            return data.decode("utf-8", "ignore")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 二维码登录
    # ------------------------------------------------------------------
    def qr_generate(self):
        js = self.get_json("https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                           referer="https://www.bilibili.com/")
        d = js.get("data") or {}
        if not d.get("url"):
            raise BiliError("获取登录二维码失败：%s" % (js.get("message") or "接口无返回"))
        return {"url": d["url"], "qrcode_key": d["qrcode_key"]}

    def qr_poll(self, qrcode_key):
        """返回 {'status': 0/86101/86090/86038, 'message':..., 'cookie':...}"""
        url = ("https://passport.bilibili.com/x/passport-login/web/qrcode/poll?"
               + urllib.parse.urlencode({"qrcode_key": qrcode_key}))
        try:
            status, data = self._raw(url, referer="https://www.bilibili.com/")
            js = json.loads(data.decode("utf-8", "ignore"))
        except BiliError as e:
            return {"status": -1, "message": "轮询失败：%s" % e}
        except Exception as e:
            return {"status": -1, "message": "轮询失败：%s" % e}

        d = js.get("data") or {}
        code = d.get("code")
        if code == 0:
            # 成功：从返回 url 里解析 cookie
            redirect = d.get("url") or ""
            cookies = self._parse_cookie_from_url(redirect)
            if not cookies:
                # 兜底：部分返回会直接带 Set-Cookie
                cookies = self._cookie_from_response()
            return {"status": 0, "message": "登录成功", "cookie": cookies}
        messages = {
            86101: "二维码已生成，等待扫码",
            86090: "已扫码，请在手机上确认",
            86038: "二维码已过期，请刷新",
        }
        return {"status": code, "message": messages.get(code, d.get("message") or "等待中")}

    @staticmethod
    def _parse_cookie_from_url(url):
        if not url:
            return ""
        parts = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parts.query)
        keys = ["SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"]
        out = []
        for k in keys:
            if qs.get(k):
                out.append("%s=%s" % (k, qs[k][0]))
        return "; ".join(out)

    def _cookie_from_response(self):
        """兜底：从最近一次响应的 Set-Cookie 头里提取登录凭证"""
        h = getattr(self, "_last_headers", None)
        if not h:
            return ""
        try:
            raws = h.get_all("Set-Cookie") or []
        except Exception:
            return ""
        keep = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")
        got = {}
        for line in raws:
            pair = line.split(";")[0].strip()
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k = k.strip()
            if k in keep and k not in got:
                got[k] = v.strip()
        order = [k for k in keep if k in got]
        return "; ".join("%s=%s" % (k, got[k]) for k in order)

    def validate_cookie(self, cookie):
        """用给定 cookie 校验登录状态"""
        old = self.cookie
        self.cookie = cookie
        try:
            info = self.nav_info()
            return info
        finally:
            self.cookie = old


WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}


def make_safe_filename(name, max_len=90):
    """清理文件名中的非法字符，兼容 Windows 限制"""
    if not name:
        return "未命名"
    name = str(name)
    # Windows / POSIX 非法字符
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name)
    # 控制字符
    name = "".join(ch for ch in name if ord(ch) >= 32)
    name = re.sub(r"\s+", " ", name).strip()
    # Windows 不允许结尾是点或空格
    name = name.rstrip(". ")
    if len(name) > max_len:
        name = name[:max_len].rstrip(". ")
    # 文件系统对文件名是按字节限的（Linux 255），中文一个字 3 字节，压到 200 字节内更保险
    while len(name.encode("utf-8", "ignore")) > 200 and len(name) > 12:
        name = name[:-1]
    name = name.rstrip(". ")
    # 保留设备名
    stem = name.split(".")[0].lower().strip()
    if stem in WINDOWS_RESERVED:
        name = "_" + name
    return name or "未命名"
