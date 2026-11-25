#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mtcli.py — 音乐库元数据识别 CLI（双路线对比）

路线 A  match : 文件名/标签 → 网易云搜索单曲 → 专辑曲目表 → 曲序/年份/专辑
路线 B  fp    : Chromaprint 音频指纹 → AcoustID 查询 / 库内相似度比对

子命令:
  match    名称路线
  fp       指纹路线（--dedupe-check 时做库内比对，无需外部 key）
  compare  对比两份结果

示例:
  python mtcli.py match --pack 张韶涵 --out name_zs.json
  python mtcli.py fp    --pack 张韶涵 --limit 20 --out fp_zs.json
  python mtcli.py compare name_zs.json fp_zs.json
  python mtcli.py match --all --workers 4 --out name_all.json

限流: 网易云无 cookie 时约 3 req/s 会触发 405，默认 QPS=2.5 并指数退避。
"""
from __future__ import annotations

import argparse
import base64
import csv
from collections import Counter
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from functools import lru_cache

try:
    from zhconv import convert as _zh
except ImportError:
    def _zh(s, _t=None):
        return s

import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.environ.get("MTCLI_INDEX", os.path.join(HERE, "audio_files.csv"))
# 缓存（专辑曲目表、封面、歌词）默认放本地盘。
# 放网络盘上写临时文件再原子替换容易触发删除守卫，建议保持本地。
CACHE_DIR = os.environ.get("MTCLI_CACHE") or os.path.join(
    os.path.expanduser("~"), ".mtcli_cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def _find_exe(env_key, *candidates):
    v = os.environ.get(env_key)
    if v and os.path.exists(v):
        return v
    for c in candidates:
        if c and os.path.exists(c):
            return c
    found = shutil.which(env_key.lower()) or shutil.which(env_key.lower() + ".exe")
    return found or (candidates[-1] if candidates else env_key.lower())


FFMPEG = _find_exe("FFMPEG", shutil.which("ffmpeg"), "ffmpeg")
FPCALC = _find_exe("FPCALC", shutil.which("fpcalc"), "fpcalc")
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://music.163.com/",
}
AUDIO_EXT = {".flac", ".mp3", ".wav", ".ape", ".m4a", ".wma", ".aac", ".ogg", ".dff", ".dsf"}
LOSSLESS = {".flac", ".wav", ".ape", ".dff", ".dsf"}


# ---------------------------------------------------------------- 文本归一
def norm(s) -> str:
    if not s:
        return ""
    s = _zh(unicodedata.normalize("NFKC", str(s)), "zh-cn").lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", s)


def artist_key(s) -> str:
    """去掉拉丁前缀和混进来的曲序号，只留中文主干"""
    n = re.sub(r"^\d+", "", norm(s))
    m = re.search(r"[\u4e00-\u9fff]", n)
    return n[m.start():] if m else n


def title_key(s) -> str:
    """去掉混进标题的前导曲序号"""
    return re.sub(r"^\d+(?=[\u4e00-\u9fff])", "", norm(s))


def sim(a, b) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------- 限流
class RateLimiter:
    """令牌桶 + 自适应退避。网易云对无 cookie 请求限流很紧。"""

    def __init__(self, qps: float = 2.5):
        self.interval = 1.0 / max(qps, 0.1)
        self.lock = threading.Lock()
        self.next_at = time.time()
        self.qps = qps

    def wait(self):
        with self.lock:
            now = time.time()
            if self.next_at > now:
                delay = self.next_at - now
                self.next_at += self.interval
            else:
                delay = 0.0
                self.next_at = now + self.interval
        if delay > 0:
            time.sleep(delay)

    def backoff(self, seconds: float = 3.0):
        with self.lock:
            self.next_at = max(self.next_at, time.time() + seconds)


LIMITER = RateLimiter(float(os.environ.get("MTCLI_QPS", "2.5")))


def http_json(url: str, retries: int = 4, timeout: int = 25):
    last = None
    for i in range(retries):
        LIMITER.wait()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (405, 429, 403, 503):
                # 限流：整个进程一起退避，避免多线程继续加压
                LIMITER.backoff(2.0 * (i + 1))
                time.sleep(1.0 + i)
                continue
            if 400 <= e.code < 500:
                return None
            time.sleep(1.0 + i)
        except Exception as e:
            last = e
            time.sleep(1.0 + i)
    sys.stderr.write(f"[warn] 请求失败 {url[:70]}... {type(last).__name__}\n")
    return None


# ---------------------------------------------------------------- 网易云
def netease_search_songs(keyword: str, limit: int = 8):
    """用 cloudsearch/pc。注意 /api/search/get/web 已被返回 code=405 封禁，
    而 cloudsearch 用 ar/al/dt 字段而非 artists/album/duration，两种都要兼容。"""
    u = ("https://music.163.com/api/cloudsearch/pc?s=%s&type=1&offset=0&limit=%d"
         % (urllib.parse.quote(keyword), limit))
    d = http_json(u)
    if not d or d.get("code") != 200:
        # 退到旧接口
        d2 = http_json("https://music.163.com/api/search/get?s=%s&type=1&offset=0&limit=%d"
                       % (urllib.parse.quote(keyword), limit))
        d = d2 if d2 and d2.get("code") == 200 else None
    if not d:
        return []
    out = []
    for s in ((d.get("result") or {}).get("songs") or []):
        al = s.get("album") or s.get("al") or {}
        arts = s.get("artists") or s.get("ar") or []
        dur = s.get("duration") or s.get("dt") or 0
        out.append({
            "id": s.get("id"),
            "title": s.get("name") or "",
            "artists": [a.get("name") or "" for a in arts],
            "album": al.get("name") or "",
            "album_id": al.get("id"),
            "duration": int(dur or 0),
            "song_id": s.get("id"),
        })
    return out


def netease_search_albums(keyword: str, limit: int = 6):
    u = ("https://music.163.com/api/cloudsearch/pc?s=%s&type=10&offset=0&limit=%d"
         % (urllib.parse.quote(keyword), limit))
    d = http_json(u)
    if not d or d.get("code") != 200:
        return []
    out = []
    for a in ((d.get("result") or {}).get("albums") or []):
        out.append({
            "id": a.get("id"),
            "name": a.get("name") or "",
            "artist": (a.get("artist") or {}).get("name") or "",
        })
    return out


def _normalize_positions(songs: list):
    """网易云的曲序字段不可靠，两种坏法都见过：
    《G.E.M.X.X.X.Live》列表顺序是乱的，但 position 是 1..n 的正确排列；
    《新的心跳》position 几乎全是 3，只有接口返回顺序（下标+1）是对的。
    判定：position 构成 1..n 排列 → 信 position；否则退回列表顺序。
    """
    n = len(songs)
    ps = [int(s.get("position") or 0) for s in songs]
    if sorted(ps) != list(range(1, n + 1)):
        for i, s in enumerate(songs):
            s["position"] = i + 1


def netease_album(album_id, use_cache: bool = True):
    """返回 {'album','year','songs':[{position,title,duration,disc}]}"""
    if not album_id:
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    cp = os.path.join(CACHE_DIR, f"album_{album_id}.json")
    if use_cache and os.path.exists(cp):
        try:
            with open(cp, encoding="utf-8") as f:
                rec = json.load(f)
            _normalize_positions(rec.get("songs") or [])
            return rec
        except Exception:
            pass
    d = http_json(f"https://music.163.com/api/album/{album_id}")
    if not d or not d.get("album"):
        return None
    a = d["album"]
    pt = a.get("publishTime") or 0
    year = ""
    if pt:
        try:
            year = str(time.gmtime(pt / 1000).tm_year)
        except Exception:
            year = ""
    songs = []
    for s in (a.get("songs") or []):
        songs.append({
            "position": s.get("position") or s.get("no") or 0,
            "no": s.get("no") or 0,
            "title": s.get("name") or "",
            # 每首歌自己的演唱者：合辑/原声带里各曲歌手不同，写标签要用它而不是包名
            "artists": [x.get("name") for x in (s.get("artists") or []) if x.get("name")],
            "duration": int(s.get("duration") or 0),
            "cd": (str(s.get("cd")) if str(s.get("cd") or "") not in ("", "1") else ""),
            "song_id": s.get("id"),
        })
    _normalize_positions(songs)
    pic = a.get("picUrl") or a.get("blurPicUrl") or ""
    rec = {
        "album": a.get("name") or "",
        "year": year,
        "company": a.get("company") or "",
        "songs": songs,
        # 网易云的图片地址可以直接带尺寸参数取大图，不用另发请求
        "cover": (pic + "?param=300y300") if pic else "",
        "cover_big": (pic + "?param=300y300") if pic else "",
    }
    try:
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
    except Exception:
        pass
    return rec


# ---------------------------------------------------------------- QQ 音乐
# 用途有两个：
#   1) 周杰伦等歌手在网易云没有版权，搜出来是翻唱/beat，必须换源
#   2) 网易云的 position 字段在部分专辑是乱序的，QQ 的专辑曲目表顺序更接近实体 CD
QQ_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://y.qq.com/",
}


def _qq_json(url: str, retries: int = 3, timeout: int = 25):
    """QQ 接口的 JSON 有时带 callback 包裹，统一剥掉。"""
    for i in range(retries):
        LIMITER.wait()
        try:
            req = urllib.request.Request(url, headers=QQ_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", "ignore").strip()
            if txt.startswith("callback("):
                txt = txt[len("callback("):].rstrip(")")
            return json.loads(txt)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503):
                LIMITER.backoff(2.0)
                time.sleep(1.0 + i)
                continue
            return None
        except Exception:
            time.sleep(0.8 + i)
    return None


def _qq_musicu(method: str, module: str, param: dict):
    """新版统一网关。旧的 c.y.qq.com/soso/fcgi-bin/client_search_cp 已返回空列表，
    只能走 u.y.qq.com/cgi-bin/musicu.fcg。

    两个必须遵守的点：
      1) comm 要给全（platform/needNewCode/format/ct/cv）。缺任一项时接口返回
         req.code=2001、sum=0，看着像"搜不到"，实际是参数校验没过。
      2) 必须用 POST 提交 JSON。同样的 payload 放 URL 里发 GET，会随机返回 2001
         （实测 GET 连续 10 次全失败，POST 连续 5 次全成功）。
    """
    payload = {
        "comm": {"uin": "0", "format": "json", "ct": 19, "cv": 1859,
                 "platform": "yqq.json", "needNewCode": 0},
        "req": {"method": method, "module": module, "param": param},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = dict(QQ_UA)
    headers["Content-Type"] = "application/json"
    for i in range(3):
        LIMITER.wait()
        try:
            req = urllib.request.Request(
                "https://u.y.qq.com/cgi-bin/musicu.fcg", data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read())
            reqd = d.get("req") or {}
            if reqd.get("code") == 0:
                return reqd.get("data") or {}
            # 2001 是风控抖动，退避后重试
            LIMITER.backoff(1.5)
            time.sleep(1.5 + i)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503):
                LIMITER.backoff(2.0)
                time.sleep(1.5 + i)
                continue
            return None
        except Exception:
            time.sleep(1.0 + i)
    return None


def qq_search_songs(keyword: str, limit: int = 8):
    data = _qq_musicu("DoSearchForQQMusicDesktop", "music.search.SearchCgiService",
                      {"num_per_page": limit, "page_num": 1,
                       "query": keyword, "search_type": 0})
    if not data:
        return []
    body = data.get("body") or {}
    lst = ((body.get("song") or {}).get("list")) or []
    out = []
    for s in lst:
        al = s.get("album") or {}
        idx = s.get("index_album")
        out.append({
            "id": s.get("mid"),
            "title": s.get("title") or s.get("name") or "",
            "artists": [a.get("name") or "" for a in (s.get("singer") or [])],
            "album": al.get("name") or "",
            "album_id": al.get("mid") or "",
            # musicu 返回的 interval 单位是秒
            "duration": int((s.get("interval") or 0) * 1000),
            "track": idx if isinstance(idx, int) and idx > 0 else "",
        })
    return out


def qq_search_albums(keyword: str, limit: int = 6):
    """专辑搜索的兜底：desktop 网关的 search_type=2（专辑）稳定返回 req.code=2001，
    只有 search_type=0（单曲）和 3（歌单）可用。

    所以这里改用单曲搜索，再从结果里按 album mid 聚合出专辑列表。
    单曲结果本来就带 album 名和 mid，足够满足"拿 mid 去查曲目表"这个用途。
    """
    songs = qq_search_songs(keyword, limit=max(limit * 3, 12))
    seen = {}
    for s in songs:
        mid = s.get("album_id")
        if not mid or mid in seen:
            continue
        seen[mid] = {
            "id": mid,
            "name": s.get("album") or "",
            "artist": " / ".join([a for a in s.get("artists") or [] if a]),
            "year": "",
            "song_count": 0,
        }
        if len(seen) >= limit:
            break
    return list(seen.values())


def qq_album(album_mid: str, use_cache: bool = True):
    """返回 {'album','year','songs':[{position,title,duration,disc}],'cover','cover_big'}

    顺序取接口返回的列表下标 + 1，而不是字段里的 songnum（该字段为 None）。
    musicu 的 get_album_song 现在返回 code=40000（需要签名），改走旧 fcg 接口。
    """
    if not album_mid:
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    cp = os.path.join(CACHE_DIR, f"qqalbum_{album_mid}.json")
    if use_cache and os.path.exists(cp):
        try:
            with open(cp, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    u = ("https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg?"
         "albummid=%s&g_tk=5381&format=json&platform=yqq.json" % album_mid)
    d = _qq_json(u)
    if not d or d.get("code") != 0:
        return None
    a = d.get("data") or {}
    lst = a.get("list") or []
    if not lst:
        return None
    year = ""
    ad = a.get("aDate") or ""
    if re.match(r"^\d{4}", ad):
        year = ad[:4]
    # 两个序号的含义不同，不能混用：
    #   belongCD 是全专辑连续序号（双碟 25 首是 1..25，CD2 第一首是 23）
    #   cdIdx    是碟索引，0 起
    # 定位用连续序号（唯一），写标签用碟内序号（CD2 要从 1 重新数）。
    songs = []
    per_disc = {}
    for i, t in enumerate(lst):
        disc = int(t.get("cdIdx") or 0) + 1
        per_disc[disc] = per_disc.get(disc, 0) + 1
        songs.append({
            "position": i + 1,
            "no": per_disc[disc],
            "title": t.get("songname") or "",
            # 合辑/原声带各曲歌手不同，取每首歌的 singer 而不是专辑歌手
            "artists": [x.get("name") for x in (t.get("singer") or []) if x.get("name")],
            "duration": int((t.get("interval") or 0) * 1000),
            "cd": disc if disc > 1 else "",
            "songmid": t.get("songmid") or "",
        })
    rec = {
        "album": a.get("name") or "",
        "year": year,
        "company": a.get("company") or "",
        "genre": a.get("genre") or "",
        "songs": songs,
        "cover": qq_cover_url(album_mid, 500),
        "cover_big": qq_cover_url(album_mid, 500),
    }
    try:
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
    except Exception:
        pass
    return rec


def qq_cover_url(album_mid: str, size: int = 300) -> str:
    """T002 是专辑封面。mid 需前置 M000 才能拼出有效地址。"""
    if not album_mid:
        return ""
    return f"https://y.qq.com/music/photo_new/T002R{size}x{size}M000{album_mid}.jpg"


_lyric_fail = {"qq": 0}
_lyric_lock = threading.Lock()


def qq_lyric(song_mid: str):
    """nobase64=1 直接给 LRC 明文；base64 版要自己解。

    QQ 软封期间这个接口会一直超时（单次 25 秒），全库跑下来能耗掉几个小时。
    连续失败几次就整体放弃，交给酷狗兜底。
    """
    if not song_mid:
        return ""
    with _lyric_lock:
        if _lyric_fail["qq"] >= 5:
            return ""
    u = ("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?"
         "songmid=%s&format=json&nobase64=1&g_tk=5381&loginUin=0&hostUin=0"
         "&platform=yqq.json&needNewCode=0" % song_mid)
    d = _qq_json(u, retries=1, timeout=8)
    txt = (d.get("lyric") or "").strip() if d and d.get("retcode") == 0 else ""
    with _lyric_lock:
        _lyric_fail["qq"] = 0 if txt else _lyric_fail["qq"] + 1
    return txt


def netease_lyric(song_id):
    """网易云歌词。lv=1 原文，kv=1 翻译，tv=-1 不要逐字时间戳。"""
    if not song_id:
        return ""
    d = http_json("https://music.163.com/api/song/lyric?id=%s&lv=1&kv=1&tv=-1" % song_id,
                  retries=2)
    if not d:
        return ""
    return ((d.get("lrc") or {}).get("lyric") or "").strip()


# ---------------------------------------------------------------- 酷狗
# 酷狗是 QQ 的替补：周杰伦这类 QQ 独家歌手在网易云搜不到，而 QQ 会软封
# （接口返回 code=0 但 sum=0、结果全空）。酷狗元数据和 QQ 同源，
# 专辑名、曲序、时长都对得上，可以直接顶上。
KUGOU_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def _kugou_json(url: str, retries: int = 3):
    last = None
    for i in range(retries):
        LIMITER.wait()
        try:
            req = urllib.request.Request(url, headers=KUGOU_UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    if last:
        pass
    return None


def kugou_search_songs(keyword: str, limit: int = 8):
    u = ("http://mobilecdn.kugou.com/api/v3/search/song?format=json"
         f"&keyword={urllib.parse.quote(keyword)}&page=1&pagesize={max(limit, 5)}")
    d = _kugou_json(u)
    info = ((d or {}).get("data") or {}).get("info") or []
    out = []
    for s in info:
        out.append({
            "id": s.get("hash") or "",
            "title": s.get("songname") or "",
            "artists": [x.strip() for x in re.split(r"[、&/]", s.get("singername") or "")
                        if x.strip()],
            "album": s.get("album_name") or "",
            "album_id": str(s.get("album_id") or ""),
            # 酷狗 duration 单位是秒
            "duration": int((s.get("duration") or 0) * 1000),
            "track": "",
        })
    return out


def kugou_search_albums(keyword: str, limit: int = 6):
    """按专辑 id 聚合：酷狗没有干净的专辑搜索接口，用单曲结果反推。"""
    songs = kugou_search_songs(keyword, 20)
    seen = {}
    for s in songs:
        aid = s.get("album_id")
        if not aid or aid in seen or not s.get("album"):
            continue
        seen[aid] = {"id": aid, "name": s["album"],
                     "artist": " / ".join(s.get("artists") or []), "year": "",
                     "song_count": 0}
        if len(seen) >= limit:
            break
    return list(seen.values())


def kugou_album(album_id: str, use_cache: bool = True):
    """返回 {'album','year','songs':[{position,title,artists,duration,cd}],'cover','cover_big'}

    曲目表接口不返回曲名，只有 filename（"歌手 - 曲名"），要自己切。
    顺序即专辑曲序，碟号取 volume。
    """
    if not album_id:
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    cp = os.path.join(CACHE_DIR, f"kgalbum_{album_id}.json")
    if use_cache and os.path.exists(cp):
        try:
            with open(cp, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    info = (_kugou_json("http://mobilecdn.kugou.com/api/v3/album/info?format=json"
                        f"&albumid={album_id}") or {}).get("data") or {}
    lst = (_kugou_json("http://mobilecdn.kugou.com/api/v3/album/song?format=json"
                       f"&albumid={album_id}&page=1&pagesize=-1") or {}).get("data") or {}
    tracks = lst.get("info") or []
    if not tracks:
        return None

    year = ""
    pt = info.get("publishtime") or ""
    if re.match(r"^\d{4}", pt):
        year = pt[:4]

    songs, per_disc = [], {}
    for i, t in enumerate(tracks):
        fn = t.get("filename") or ""
        artists, title = [], fn
        if " - " in fn:
            left, right = fn.split(" - ", 1)
            artists, title = [left.strip()], right.strip()
        disc = int(t.get("volume") or 0) or 1
        per_disc[disc] = per_disc.get(disc, 0) + 1
        songs.append({
            "position": i + 1,
            "no": per_disc[disc],
            "title": title,
            "artists": artists,
            "duration": int((t.get("duration") or 0) * 1000),
            "cd": disc if disc > 1 else "",
            "songmid": t.get("hash") or "",
        })

    img = info.get("imgurl") or ""
    rec = {
        "album": info.get("albumname") or "",
        "year": year,
        "company": info.get("company") or "",
        "songs": songs,
        "cover": img.replace("{size}", "480") if img else "",
        "cover_big": img.replace("{size}", "480") if img else "",
    }
    try:
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
    except Exception:
        pass
    return rec


def kugou_lyric(song_hash: str, title: str = "", duration: float = 0):
    """酷狗歌词：先按 hash 搜 krc，再换 lrc 明文下载。

    krcs 的 duration 要毫秒，且必须和 hash 一起给，否则会配到别的版本。
    """
    if not song_hash:
        return ""
    kw = urllib.parse.quote(f"{title}" if title else "")
    u = (f"https://krcs.kugou.com/search?ver=1&man=yes&client=mobi"
         f"&keyword={kw}&duration={int(duration * 1000)}&hash={song_hash}")
    d = http_json(u, retries=2)
    for c in (d or {}).get("candidates") or []:
        if not c.get("id") or not c.get("accesskey"):
            continue
        t = http_json(f"https://lyrics.kugou.com/download?ver=1&client=pc"
                      f"&id={c['id']}&accesskey={c['accesskey']}&fmt=lrc&charset=utf8",
                      retries=2)
        raw = (t or {}).get("content") or ""
        if not raw:
            continue
        try:
            txt = base64.b64decode(raw).decode("utf-8", "ignore")
        except Exception:
            continue
        if txt.strip():
            return txt.replace("\ufeff", "").strip()
    return ""


def kugou_lyric_search(title: str, artist: str = "", duration: float = 0):
    """按歌名在酷狗找到对应版本再取歌词。

    QQ 的歌词接口和搜索是一起被限流的，软封期间所有歌词都返回空，
    所以歌词也要准备替补源。搜索结果走缓存，FLAC/MP3 双份不会重复请求。
    """
    if not title:
        return ""
    cands = search_cached(f"{artist} {title}".strip(), "kugou")
    best, best_s = None, -1.0
    for c in cands or []:
        s = sim(title_key(title), title_key(c["title"]))
        if c.get("duration") and duration:
            d = abs(c["duration"] / 1000.0 - duration)
            s += 0.35 if d <= 3 else (0.1 if d <= 12 else 0.0)
        if s > best_s:
            best, best_s = c, s
    if not best or best_s < 0.7:
        return ""
    return kugou_lyric(best["id"], best["title"], (best["duration"] or 0) / 1000)


def fetch_lyric(src: str, rec: dict) -> str:
    """按来源取歌词：QQ 用 songmid，酷狗用 hash，网易云用 song_id"""
    if src == "kugou" or (src or "").startswith("kugou"):
        return kugou_lyric(rec.get("songmid") or "",
                           rec.get("m_title") or rec.get("title_guess") or "",
                           rec.get("duration") or 0)
    if src == "qq" or (src or "").startswith("qq"):
        mid = rec.get("songmid") or ""
        return qq_lyric(mid) if mid else ""
    sid = rec.get("song_id") or ""
    return netease_lyric(sid) if sid else ""


# ---------------------------------------------------------------- 来源路由
# 网易云缺版权的歌手：搜出来全是翻唱、beat、DJ 版，必须走 QQ。
QQ_ONLY_ARTISTS = {
    "周杰伦", "周杰倫", "jay", "jaychou",
    "陈奕迅", "陳奕迅",  # 网易云版权不全（部分专辑下架）
    "五月天", "林俊杰", "林俊傑",  # 新专辑多在 QQ 独家
}
SOURCE_CN = {"netease": "网易云", "qq": "QQ音乐", "kugou": "酷狗"}


def qq_only(artist: str) -> bool:
    """这个歌手是不是只能用 QQ（网易云没版权，或者有大量假冒上传）。"""
    ak = artist_key(artist)
    if not ak:
        return False
    return any(artist_key(n) and artist_key(n) in ak for n in QQ_ONLY_ARTISTS)


def pick_source(artist: str) -> str:
    """默认网易云，命中黑名单走 QQ。可用 MTCLI_SOURCE 强制全局切换（qq/kugou/netease）。"""
    forced = os.environ.get("MTCLI_SOURCE", "").strip().lower()
    if forced in ("qq", "netease", "kugou"):
        return forced
    return "qq" if qq_only(artist) else "netease"


def search_songs_of(src: str, keyword: str, limit: int = 8):
    if src == "qq":
        return qq_search_songs(keyword, limit)
    if src == "kugou":
        return kugou_search_songs(keyword, limit)
    return netease_search_songs(keyword, limit)


def search_albums_of(src: str, keyword: str, limit: int = 6):
    if src == "qq":
        return qq_search_albums(keyword, limit)
    if src == "kugou":
        return kugou_search_albums(keyword, limit)
    return netease_search_albums(keyword, limit)


def fetch_album_of(src: str, album_id):
    if src == "qq":
        return qq_album(album_id)
    if src == "kugou":
        return kugou_album(album_id)
    return netease_album(album_id)


def source_chain(artist: str, primary: str):
    """某个歌手可用的音源顺序（去掉 primary）。

    黑名单歌手（QQ 独家）只用 QQ / 酷狗；其他人优先网易云，再 QQ / 酷狗。
    QQ 会软封，所以给它留一个同元数据的酷狗做替补。
    """
    order = ["qq", "kugou"] if qq_only(artist) else ["netease", "qq", "kugou"]
    return [x for x in order if x != primary]


# ---------------------------------------------------------------- 路径解析
GENERIC_ALBUM = {"单曲", "live", "演唱会", "演唱会live版本", "新歌", "ep", "ost",
                 "原声带", "合集", "精选", "专辑"}


_NOT_PERSON = re.compile(
    r"电影|原声|电视|剧|演唱会|音乐会|专辑|合辑|精选|群星|未知|歌手|伴奏|纯音乐|"
    r"版$|曲$|现场|live|ost|cd\d|vol", re.I)


def _looks_like_person(s: str) -> bool:
    """判断 "X - 歌名" 里的 X 像不像人名。

    用来识别原声带/合辑里夹带的他人曲目（黄雨勋、杨瑞代、江语晨…）。
    宁可漏判也不能错判：错判会把专辑名、电影名当成歌手去搜，反而更糟。
    """
    s = s.strip().strip(". ")
    if not (2 <= len(s) <= 12):
        return False
    if re.search(r"\d|[\[\]()（）【】<>]", s):
        return False
    if _NOT_PERSON.search(s):
        return False
    return True


# 常见的「非歌手」容器目录：扁平音乐库常直接叫 歌曲 / Music，
# 不能当歌手名去搜。宁缺毋滥——认不出歌手留给内嵌标签/在线匹配，好过错搜。
GENERIC_DIR = {"歌曲", "音乐", "music", "musics", "mp3", "flac", "lossless", "无损",
               "new folder", "新建文件夹", "新文件夹", "downloads", "下载", "audio",
               "audios", "songs", "song", "歌库", "曲库", "library", "collection",
               "album", "albums", "artist", "artists", "歌手", "我的音乐", "my music",
               "desktop", "桌面", "temp", "tmp", "测试", "整理", "backup", "备份"}


def _artist_from_name(s: str) -> str:
    """目录/歌手包名 → 歌手名；不像人名/歌手名就返回空。"""
    s = re.split(r"[\[\(（【]", s, maxsplit=1)[0].strip()
    s = re.sub(r"^\d{4}\s*[-_.]\s*", "", s).strip()
    s = re.sub(r"\[.*?\]|\(.*?\)|（.*?）", "", s).strip()
    if not s or len(s) > 24 or len(s) < 2:
        return ""
    if s.startswith((".", "_")):
        return ""
    # "01 你把我灌醉.flac" 这种文件名残片被当成包名 = 路径切错的信号
    if os.path.splitext(s)[1].lower() in AUDIO_EXT:
        return ""
    if re.match(r"^\d{1,3}\s*[.\-_、 ]", s):
        return ""
    if s.lower() in GENERIC_DIR or norm(s) in {norm(g) for g in GENERIC_DIR}:
        return ""
    return s


def parse_path(path: str) -> dict:
    """从 <歌手包>\\<...>\\<文件名> 推出 artist/title/album_guess

    先做路径归一：混用正反斜杠的路径（"E:/歌曲/xx.flac"）直接 split("\\")
    会把文件名当成歌手包，产生 "01 xx.flac" 这种脏歌手名。
    """
    parts = os.path.normpath(path).split(os.sep)
    pack = parts[1] if len(parts) > 1 else ""
    artist = _artist_from_name(pack)

    dirs = [d for d in parts[2:-1]
            if not re.fullmatch(r"(cd|disc|碟)\s*\d+", d.strip(), re.I)]
    album = ""
    disc_hint = ""
    if dirs:
        album = dirs[-1].strip()
        # 剥掉方括号里的音质/来源标记。除了 FLAC/MP3 这类，
        # 还有 MORA合购、Hires、24bit、DSD 等抓轨圈常见的写法。
        album = re.sub(r"[\[\(（【][^\])）】]{0,40}"
                       r"(flac|mp3|wav|ape|dsd|mora|合购|hires|hi-?res|24bit|16bit|"
                       r"\d+k|无损|资源|下载|网|库|自抓|扫图)"
                       r"[^\])）】]{0,40}[\]\)）】]", "", album, flags=re.I)
        album = re.sub(r"^\d{4}\s*[.\-_年]\s*", "", album).strip(" -_.")
        # "2013.07-天台电影原声带" 剥掉年份后还剩 "07-"，
        # 留着会让专辑名相似度白掉 0.12，还会干扰跨源匹配。
        album = re.sub(r"^\d{1,3}\s*[-_.、]\s*", "", album).strip(" -_.")
        # 目录名整串带着碟号（"The Moment CD2"），不是独立的 "CD2" 目录，
        # 上面的 dirs 过滤拦不住。不剥掉这张专辑永远搜不到，会把整碟配错。
        mcd = re.search(r"[\s._\-]*\d*\s*(?:cd|disc|碟)\s*(\d+)\s*$", album, re.I)
        disc_hint = ""
        if mcd:
            disc_hint = mcd.group(1)
            album = album[:mcd.start()].strip(" -_.")
        # 目录名里意外带着音频扩展名（"2009.自定义.flac"）
        album = re.sub(r"\.(flac|mp3|wav|ape|m4a|ogg|wma)$", "", album, flags=re.I)
        album = album.strip(" -_.")
        # "张韶涵 - 梦里花" → "梦里花"
        sp = re.split(r"\s*[-–—]\s*", album)
        if len(sp) == 2 and artist_key(artist) and artist_key(artist) in norm(sp[0]) \
                and artist_key(artist) not in norm(sp[1]):
            album = sp[1].strip()

    stem = os.path.splitext(os.path.basename(path))[0]
    track_hint = ""
    m = re.match(r"^(\d{1,3})\s*[.\-_、 ]\s*", stem)
    if m:
        track_hint = m.group(1)
        stem = stem[m.end():]
    # "歌手 - 曲名" 或 "曲名 - 歌手" 两种顺序都要认
    m2 = re.match(r"^(.{1,24}?)\s*[-–—]\s*(.+)$", stem)
    title = stem
    artist_hint = ""
    if m2:
        left, right = m2.group(1).strip(), m2.group(2).strip()
        if artist_key(artist) and artist_key(artist) in norm(left):
            title = right
        elif artist_key(artist) and artist_key(artist) in norm(right):
            title = left
            if not artist:
                artist = right
        else:
            title = right
            # 两边都不含包名歌手：左侧很可能是实际演唱者
            # （原声带/合辑里大量 "黄雨勋 - Goodbye"、"杨瑞代 - 孤单" 这种）。
            # 只当线索留用，不直接改 artist —— 有些文件名左侧是专辑/电影名。
            if _looks_like_person(left) and _album_key(left) != _album_key(album):
                artist_hint = left
    title = re.sub(r"^[.\-_、\s]+", "", title)
    return {"artist": artist, "title": title.strip(), "album_guess": album,
            "track_hint": track_hint, "artist_hint": artist_hint,
            "disc_hint": disc_hint}


@lru_cache(maxsize=8192)
def read_tag_info(path: str) -> dict:
    """读内嵌标签的歌手/曲名/专辑/年份/曲序。读不到返回空 dict。"""
    try:
        import mutagen
        mf = mutagen.File(path, easy=True)
        t = mf.tags or {}
    except Exception:
        return {}

    def g(*keys):
        for k in keys:
            v = t.get(k)
            if v:
                v = str(v[0] if isinstance(v, (list, tuple)) else v).strip()
                if v:
                    return v
        return ""

    return {
        "artist": g("albumartist", "artist", "album artist"),
        "title": g("title"),
        "album": g("album"),
        "year": re.sub(r"[^\d]", "", g("date", "year"))[:4],
        "track": re.sub(r"[^\d].*$", "", g("tracknumber")),
    }


def info_with_tags(path: str) -> dict:
    """parse_path（文件名/目录）+ 内嵌标签合并。

    扁平目录里文件名常不含歌手（"01 曲名.flac"），标签里的歌手/专辑比
    文件名可靠得多；但曲序仍以文件名为准——很多老标签 tracknumber 是错的
    （MusicTag 时代全写 1），文件名的序号反而可信。
    """
    info = dict(parse_path(path))
    tag = read_tag_info(path)
    if not tag:
        return info
    if not info.get("artist") and tag.get("artist"):
        info["artist"] = tag["artist"].split("/")[0].strip()
        info["artist_hint"] = info.get("artist_hint") or tag["artist"]
    if not info.get("title") and tag.get("title"):
        info["title"] = tag["title"]
    if not info.get("album_guess") and tag.get("album"):
        info["album_guess"] = re.sub(r"[《》「」]", "", tag["album"]).strip()
    # 标签曲序只做兜底线索，不并入 track_hint：老标签的 tracknumber 大量是错的
    # （MusicTag 时代全写 1），文件名序号才是抓轨真值。
    if tag.get("track"):
        info["tag_track"] = tag["track"]
    if tag.get("year"):
        info["year_hint"] = tag["year"]
    return info


# ---------------------------------------------------------------- 名称路线
_JUNK_ARTIST = re.compile(r"[^\w\s\u4e00-\u9fff&/、,，.·+\-'’．()（）【】]")


def score_candidate(cand: dict, artist: str, title: str, dur: float) -> float:
    # 网易云上一堆 "周杰伦♚"、"周杰伦. " 这种用户假冒上传，
    # 名字里带符号或纯装饰字符的直接判死，别让它们挤掉正常条目。
    if any(_JUNK_ARTIST.search(a) for a in cand["artists"] if a):
        return 0.0
    st = sim(title_key(title), title_key(cand["title"]))
    ca = [artist_key(a) for a in cand["artists"]]
    ak = artist_key(artist)
    sd = 0.0
    if dur and cand["duration"]:
        diff = abs(dur - cand["duration"] / 1000.0)
        sd = 1.0 if diff <= 2 else (0.6 if diff <= 5 else (0.25 if diff <= 15 else 0.0))
    if not ak:
        # 完全没有歌手线索（扁平目录 + 文件名无歌手）时不能按歌手打分，
        # 否则同曲永远卡在 0.45 过不了阈值。改用标题+时长。
        return 0.7 * st + 0.3 * sd
    sa = 1.0 if ak and ak in ca else (0.65 if any(ak and (ak in c or c in ak) for c in ca) else 0.0)
    return 0.45 * st + 0.40 * sa + 0.15 * sd


_search_cache = {}
_search_lock = threading.Lock()
_album_search_cache = {}

# QQ 软封检测：连续 N 次搜索返回空，就认为整体被限流。
# 不再为每个文件白等 2.5 秒，直接走替补源。
SOFT_BLOCK_LIMIT = 8
_soft_block = {"qq": 0}
_soft_lock = threading.Lock()


def _note_search(src: str, ok: bool):
    if src != "qq":
        return
    with _soft_lock:
        _soft_block["qq"] = 0 if ok else _soft_block["qq"] + 1


def _soft_block_count(src: str) -> int:
    with _soft_lock:
        return _soft_block.get(src, 0)


def probe_source_health():
    """开跑前探一次 QQ。软封时接口返回 code=0 但结果全空，
    提前探到就能省掉每个文件 2.5 秒的无效等待。"""
    if os.environ.get("MTCLI_SOURCE", "").strip().lower() == "kugou":
        return
    try:
        ok = bool(qq_search_songs("周杰伦 七里香", 3))
    except Exception:
        ok = False
    if not ok:
        with _soft_lock:
            _soft_block["qq"] = SOFT_BLOCK_LIMIT
        print("  ⚠ QQ 搜索返回空（软封/限流），本轮到酷狗取值")


def search_albums_cached(keyword: str, src: str = "netease"):
    key = f"{src}|{keyword}"
    with _search_lock:
        if key in _album_search_cache:
            return _album_search_cache[key]
    res = search_albums_of(src, keyword)
    if res:
        with _search_lock:
            _album_search_cache[key] = res
    return res


def search_cached(keyword: str, src: str = "netease"):
    """同一个 (歌手+曲名) 只搜一次，FLAC/MP3 双份共用结果。

    空结果不缓存：QQ 的风控抖动会返回空列表，把空结果记下来等于永久判死。
    """
    key = f"{src}|{keyword}"
    with _search_lock:
        if key in _search_cache:
            return _search_cache[key]
    res = search_songs_of(src, keyword)
    if res:
        with _search_lock:
            _search_cache[key] = res
    return res


def match_one(path: str, duration: float) -> dict:
    info = info_with_tags(path)
    artist, title = info["artist"], info["title"]
    src = pick_source(artist)
    out = {
        "path": path, "artist_guess": artist, "title_guess": title,
        "album_guess": info["album_guess"], "track_hint": info["track_hint"],
        "duration": round(duration, 1),
        "hit": False, "score": 0.0,
        "album": "", "year": "", "track": "", "disc": "",
        "m_title": "", "m_artist": "", "source": src,
        "cover": "", "lyric": "",
    }
    if not title:
        out["err"] = "曲名为空"
        return out

    cands = search_cached(f"{artist} {title}".strip(), src)
    if not cands:
        cands = search_cached(title, src)
    if not cands and _soft_block_count("qq") < SOFT_BLOCK_LIMIT:
        # QQ 风控会间歇性返回空，等几秒再试一次。
        # 但如果是整体软封（连续大量空结果），这句 sleep 会把整库拖成几十分钟，
        # 所以连续空到一定次数就不再等，直接换替补源。
        time.sleep(2.5)
        cands = search_cached(f"{artist} {title}".strip(), src)
    _note_search(src, bool(cands))
    if not cands:
        # 换替补源再试（版权差异、QQ 软封都会导致主源空）。
        # 黑名单歌手不会退到网易云，避免被 "周杰伦♚" 这类假冒条目顶替。
        for alt in source_chain(artist, src):
            cands = search_cached(f"{artist} {title}".strip(), alt)
            if cands:
                src = alt
                out["source"] = alt
                break
    if not cands:
        out["err"] = "无搜索结果"
        return out

    best, best_s = None, -1.0
    for c in cands:
        s = score_candidate(c, artist, title, duration)
        if s > best_s:
            best, best_s = c, s
    out["score"] = round(best_s, 3)
    if best_s < 0.55:
        # 分数太低说明搜到的是翻唱/同名曲/别人的歌（例如《追》搜到翻唱专辑）。
        # 宁可留空等人工确认，也不要把错误的专辑和曲序写进文件。
        out["err"] = f"低置信 {best_s:.2f}"
        out["suggest"] = f"{best['title']} / {' / '.join(best['artists'])}"
        return out
    out["m_title"] = best["title"]
    out["m_artist"] = " / ".join(best["artists"])
    out["album"] = best["album"]
    # 搜索结果里就带 id，先用它兜底取歌词；专辑曲目表更准，稍后会覆盖
    if best.get("song_id"):
        out["song_id"] = best["song_id"]
    if best.get("id") and src == "qq":
        out["songmid"] = best["id"]

    alb = fetch_album_of(src, best["album_id"])
    if alb:
        out["album"] = alb["album"] or best["album"]
        out["year"] = alb["year"]
        if alb.get("cover_big"):
            out["cover"] = alb["cover_big"]
        elif alb.get("cover"):
            out["cover"] = alb["cover"]
        locate_track(alb, info, title, duration, out)
        # QQ 专辑曲目自带 songmid，可直接取歌词
        _fill_songmid(alb, out)
    elif best.get("album"):
        out["album"] = best["album"]

    # 回退：单曲查不到曲序时，用专辑名反查专辑曲目表。
    # 这条路径更可靠 —— 同一张专辑的 track/year 天然一致，不会出现"每首都是第1首"。
    if not out["track"]:
        ak = artist_key(artist)
        kws = []
        for nm_ in (out.get("album"), info["album_guess"]):
            nm_ = (nm_ or "").strip()
            if nm_ and nm_.lower() not in GENERIC_ALBUM:
                kws.append(nm_)
                kws.append(f"{artist} {nm_}".strip())
        seen = set()
        for kw in kws:
            if kw in seen:
                continue
            seen.add(kw)
            for cand in search_albums_cached(kw, src):
                if ak and ak not in artist_key(cand["artist"]) \
                        and artist_key(cand["artist"]) not in ak:
                    continue
                if sim(title_key(cand["name"]), title_key(kw)) < 0.35 \
                        and title_key(cand["name"]) not in title_key(kw) \
                        and title_key(kw) not in title_key(cand["name"]):
                    continue
                alb = fetch_album_of(src, cand["id"])
                if not alb or not alb["songs"]:
                    continue
                if locate_track(alb, info, title, duration, out):
                    out.update({"album": alb["album"], "year": alb["year"],
                                "source": f"{src}-album"})
                    if alb.get("cover_big"):
                        out["cover"] = alb["cover_big"]
                    _fill_songmid(alb, out)
                    break
            if out["track"]:
                break

    out["hit"] = bool(out["track"]) and best_s >= 0.55
    return out


def _fill_songmid(alb: dict, out: dict):
    """把曲序对应的 songmid 挂到结果上（QQ 才有，用来取歌词）。

    曲序要按数字比：QQ 的 position 是 int，文件名 hint 可能带前导零（"001"），
    字符串直接比会全部失配。
    """
    try:
        want = int(str(out.get("track") or "").strip())
    except ValueError:
        return
    for s in alb.get("songs") or []:
        if s.get("position") != want:
            continue
        if s.get("songmid"):
            out["songmid"] = s["songmid"]
        if s.get("song_id"):
            out["song_id"] = s["song_id"]
        return


def locate_track(alb: dict, info: dict, title: str, duration: float, out: dict) -> str:
    """在专辑曲目表里定位曲目，返回 position（空串表示没找到）。

    优先级：文件名曲序号 > 曲名匹配 > 时长匹配 > 标签曲序兜底。
    文件名序号是抓轨真值，但槽位曲名对不上时（多碟按碟内编号、文件被重命名过）
    改信曲名定位。各路不一致时记录 conflict，留给人工复核。
    """
    hint = str(info.get("track_hint") or "").strip()
    by_dur = by_name = by_hint = ""
    positions = [s["position"] for s in alb["songs"] if s["position"]]

    for s in alb["songs"]:
        if duration and s["duration"] and abs(s["duration"] / 1000.0 - duration) <= 2:
            by_dur = str(s["position"])
            break
    # 曲名两遍：先严格，再包含（"偶尔" ⊂ "偶尔 (Piano Session Live)"）。
    # 取最高分而不是首个命中："是否 (Piano Session Live)" 与
    # "偶尔 (Piano Session Live)" 共享长英文后缀，模糊分也能过 0.85。
    best_s, best_p = 0.0, ""
    for s in alb["songs"]:
        sc = sim(title_key(title), title_key(s["title"]))
        if sc > best_s:
            best_s, best_p = sc, str(s["position"])
    if best_s >= 0.85:
        by_name = best_p
    if not by_name:
        tk = title_key(title)
        if len(tk) >= 2:
            best_len, best_p = None, ""
            for s in alb["songs"]:
                sk = title_key(s["title"])
                if sk and (tk in sk or sk in tk):
                    # 多个包含候选时取长度最接近的（差异后缀最短的）
                    d = abs(len(sk) - len(tk))
                    if best_len is None or d < best_len:
                        best_len, best_p = d, str(s["position"])
            by_name = best_p

    def _n(x):
        try:
            return int(str(x).strip())
        except (TypeError, ValueError):
            return None

    chosen, src = "", ""
    if hint.isdigit() and 1 <= int(hint) <= (max(positions) if positions else 0):
        by_hint = hint
        slot = next((s for s in alb["songs"] if _n(s["position"]) == int(hint)), None)
        slot_title = (slot or {}).get("title") or ""
        # 槽位存在且曲名对得上（或曲名没另给出定位）才认文件名序号；
        # 对不上说明这是碟内编号/重命名残留，以曲名定位为准。
        if slot is not None and (not slot_title or not by_name or _n(by_name) == int(hint)
                                 or sim(title_key(title), title_key(slot_title)) >= 0.5):
            chosen, src = hint, "filename"
        elif by_name:
            chosen, src = by_name, "title"
        else:
            chosen, src = hint, "filename"   # 没别的线索，仍以文件名序号为准
    elif by_name:
        chosen, src = by_name, "title"
    elif by_dur:
        chosen, src = by_dur, "duration"
    else:
        # 标签曲序是最后手段：老标签错误率高，能不用就不用。
        tt = str(info.get("tag_track") or "").strip()
        if tt.isdigit() and 1 <= int(tt) <= (max(positions) if positions else 0):
            by_hint = tt
            chosen, src = tt, "tag"

    if chosen:
        out["track"] = chosen
        out["track_src"] = src
        out["hit"] = True
        cn = _n(chosen)
        for s in alb["songs"]:
            if _n(s["position"]) == cn:
                # 碟内序号与碟号，写标签用；单碟时 no == position
                out["track_num"] = s.get("no") or s["position"]
                out["disc"] = s.get("cd") or ""
                # 演唱者以云端这首的为准。合辑/原声带里各曲歌手不同，
                # 用包名歌手会把整张原声带都写成周杰伦。
                if s.get("artists"):
                    out["m_artist"] = " / ".join(s["artists"])
                break
        # 各路曲序的分歧留痕，供人工复核。
        # 必须按数字比：文件名 hint 常带前导零（"001"），云端 position 是 "1"，
        # 直接比字符串会让每条记录都"冲突"。
        others = {x for x in (by_dur, by_name, by_hint)
                  if x and _n(x) != cn}
        if others:
            out["conflict"] = f"本地={chosen} 云端={'/'.join(sorted(others))}"
    return chosen


# ---------------------------------------------------------------- 指纹路线
def chromaprint_raw(path: str, length: int = 120, timeout: int = 180):
    """用官方 fpcalc 取指纹（int32 列表）。

    不用 ffmpeg 的 chromaprint muxer：实测同一录音的 FLAC 与 MP3 经过它只得到
    0.57 相似度（随机水平 0.5），而官方 fpcalc 给出 0.999~1.000。
    """
    if not os.path.exists(FPCALC):
        return None
    try:
        p = subprocess.run([FPCALC, "-raw", "-length", str(length), path],
                           capture_output=True, timeout=timeout)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    out = {}
    for line in p.stdout.decode("utf-8", "ignore").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    fp = out.get("FINGERPRINT", "")
    if not fp:
        return None
    try:
        return [int(x) for x in fp.split(",")]
    except ValueError:
        return None


def chromaprint_b64(path: str, length: int = 120, timeout: int = 180):
    """fpcalc 默认输出的压缩 base64 指纹，用于 AcoustID 查询。"""
    if not os.path.exists(FPCALC):
        return None
    try:
        p = subprocess.run([FPCALC, "-length", str(length), path],
                           capture_output=True, timeout=timeout)
        if p.returncode != 0:
            return None
        for line in p.stdout.decode("utf-8", "ignore").splitlines():
            if line.startswith("FINGERPRINT="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def fp_similarity(a, b, max_shift: int = 30) -> float:
    """带偏移对齐的位错误率相似度。

    同一录音不同编码 ≈ 1.000，不同曲 ≈ 0.50（32 位哈希的随机水平）。
    必须做偏移搜索：两个文件起始差一帧（0.124 s）就会让逐位比较退化成随机值。
    """
    if not a or not b:
        return 0.0
    best = 0.0
    for shift in range(-max_shift, max_shift + 1):
        n = min(len(a), len(b)) - abs(shift)
        if n < 40:
            continue
        same = 0
        lo = max(0, -shift)
        hi = min(len(a), len(b) - shift)
        for i in range(lo, hi):
            same += 32 - bin((a[i] ^ b[i + shift]) & 0xFFFFFFFF).count("1")
        r = same / (32.0 * (hi - lo))
        if r > best:
            best = r
            if best > 0.999:
                break
    return best


def acoustid_lookup(fp_b64: str, duration: float, key: str):
    """AcoustID 查询。需要免费 client key: https://acoustid.org/new-application"""
    if not key:
        return None
    u = ("https://api.acoustid.org/v2/lookup?client=%s&meta=recordings+releasegroups"
         "&duration=%d&fingerprint=%s"
         % (urllib.parse.quote(key), int(duration), urllib.parse.quote(fp_b64)))
    d = http_json(u, retries=2)
    if not d or d.get("status") != "ok":
        return None
    best = None
    for r in (d.get("results") or []):
        for rec in (r.get("recordings") or []):
            cand = {
                "score": r.get("score", 0),
                "title": rec.get("title") or "",
                "artists": [a.get("name") for a in (rec.get("artists") or []) if a.get("name")],
                "releasegroups": [g.get("title") for g in (rec.get("releasegroups") or [])],
            }
            if best is None or cand["score"] > best["score"]:
                best = cand
    return best


# ---------------------------------------------------------------- 索引
def load_index() -> list:
    rows = []
    with open(INDEX, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            p = r["Filename"]
            ext = os.path.splitext(p)[1].lower()
            if ext not in AUDIO_EXT:
                continue
            rows.append({"path": p, "size": int(r["Size"] or 0), "ext": ext})
    return rows


def filter_pack(rows, pack_kw):
    if not pack_kw:
        return rows
    return [r for r in rows if pack_kw in r["path"].split("\\")[1]]


# ---------------------------------------------------------------- 时长探测
def probe_duration(path: str) -> float:
    """ffprobe 取时长（比 mutagen 稳，WAV/APE 都认）"""
    exe = FFMPEG.replace("ffmpeg.exe", "ffprobe.exe")
    try:
        p = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, timeout=30)
        if p.returncode == 0:
            return float(p.stdout.decode().strip() or 0)
    except Exception:
        pass
    return 0.0


_dur_cache = {}
_dur_lock = threading.Lock()


def duration_of(path: str) -> float:
    with _dur_lock:
        if path in _dur_cache:
            return _dur_cache[path]
    d = probe_duration(path)
    with _dur_lock:
        _dur_cache[path] = d
    return d


# ---------------------------------------------------------------- 子命令
# ---------------------------------------------------------------- 专辑优先批量匹配
def _album_key(s: str) -> str:
    """专辑名归一，去掉音质标记和年份前缀，用于分组与相似度比较"""
    n = norm(s)
    # 碟号可能在前面（"CD2 xxx"）、后面（"xxx CD2"）或粘连（"xxx 2CD1"）
    n = re.sub(r"\d*\s*(?:cd|disc|碟)\s*\d+", "", n)
    return n.strip()


def resolve_album(artist: str, album_guess: str, durations: list, src: str):
    """给一个（歌手, 专辑名）找云端曲目表。

    打分依据两条，缺一不可：
      - 专辑名相似度（防止《全面沦陷》被配到《Undefined 演唱会》）
      - 组内文件时长在曲目表里的命中数（防止同名的翻唱专辑）
    返回 (album_dict, quality) 或 (None, 0)
    """
    if not album_guess or album_guess.strip().lower() in GENERIC_ALBUM:
        return None, 0.0
    ak = artist_key(artist)
    akey = _album_key(album_guess)
    if not akey:
        return None, 0.0

    # 主源搜不到就换替补源（版权差异、QQ 软封）。
    # 黑名单歌手不许退网易云：那边全是用户假冒上传的"周杰伦♚"。
    sources = [src] + source_chain(artist, src)
    cands_src = None
    for s_ in sources:
        c = search_albums_cached(f"{artist} {album_guess}".strip(), s_)
        if not c:
            c = search_albums_cached(album_guess, s_)
        if c:
            cands_src = s_
            break
    if not cands_src:
        return None, 0.0
    cands = search_albums_cached(f"{artist} {album_guess}".strip(), cands_src) \
        or search_albums_cached(album_guess, cands_src)
    src = cands_src

    scored = []
    for c in cands:
        if ak:
            ca = artist_key(c.get("artist") or "")
            if ca and ak not in ca and ca not in ak:
                continue
        ck = _album_key(c.get("name") or "")
        if not ck:
            continue
        s = sim(akey, ck)
        if akey in ck or ck in akey:
            s = max(s, 0.88)
        if s < 0.45:
            continue
        scored.append((s, c))
    scored.sort(key=lambda x: -x[0])

    best, best_q = None, 0.0
    for s, c in scored[:3]:
        alb = fetch_album_of(src, c["id"])
        if not alb or not alb["songs"]:
            continue
        hit = 0
        for d in durations:
            if not d:
                continue
            if any(sg["duration"] and abs(sg["duration"] / 1000.0 - d) <= 2
                   for sg in alb["songs"]):
                hit += 1
        q = (hit / max(len(durations), 1)) * 0.75 + s * 0.25
        if q > best_q:
            best, best_q = alb, q
            # 记下专辑名相似度：原声带里纯音乐多，时长对不上导致 q 很低，
            # 但专辑名对得上的仍然可信。用它做二次闸门，避免被同名系列误导。
            alb["_name_sim"] = round(s, 3)
    return best, best_q


def _locate_in_album(alb: dict, path: str, duration: float, out: dict) -> str:
    info = info_with_tags(path)
    return locate_track(alb, info, info["title"], duration, out)


def cmd_match(args):
    rows = filter_pack(load_index(), args.pack)
    if args.limit:
        rows = rows[: args.limit]
    print(f"名称路线：{len(rows)} 个文件，{args.workers} 线程，QPS={LIMITER.qps}")
    probe_source_health()

    t0 = time.time()

    # ---------- Phase 1：并行探测时长 ----------
    print("  探测时长 ...")
    with ThreadPoolExecutor(args.workers) as ex:
        durations = list(ex.map(lambda r: duration_of(r["path"]), rows))
    for r, d in zip(rows, durations):
        r["dur"] = d
    print(f"  完成，用时 {time.time()-t0:.0f}s")

    # ---------- Phase 2：按（歌手, 专辑）分组，每组只查一次专辑 ----------
    groups = {}
    for r in rows:
        info = info_with_tags(r["path"])
        key = (artist_key(info["artist"]), _album_key(info["album_guess"]))
        groups.setdefault(key, {"info": info, "files": []})["files"].append(r)
    print(f"  分 {len(groups)} 组（每组一次专辑查询，不是每首歌一次）")

    pending = []
    album_ok = 0
    gdone = [0]
    glock = threading.Lock()

    def _title_hits(alb: dict, titles: list) -> int:
        """专辑曲目表能对上组内多少个曲名。判断专辑配得准不准，它比质量分可靠：
        质量分受组内文件数和时长误差影响，跨组不可比。"""
        if not alb:
            return 0
        keys = [title_key(s.get("title") or "") for s in alb.get("songs") or []]
        return sum(1 for t in titles
                   if any(sim(title_key(t), k) >= 0.85 for k in keys if k))

    def do_group(item):
        (ak, alk), g = item
        info = g["info"]
        src = pick_source(info["artist"])
        durations_ = [f["dur"] for f in g["files"]]
        titles = [info_with_tags(f["path"])["title"] for f in g["files"]]
        alb, q = resolve_album(info["artist"], info["album_guess"], durations_, src)
        opts = [(alb, q, src)]
        # 包名歌手搜不到时，改用文件名里的演唱者再试一次。
        # 原声带/合辑里混着别人的专辑（杨瑞代的《黄俊郎的黑》），
        # 用周杰伦去搜那张专辑永远搜不到。
        hints = [info_with_tags(f["path"]).get("artist_hint") for f in g["files"]]
        hints = [h for h in hints if h]
        if hints:
            hint = Counter(hints).most_common(1)[0][0]
            if artist_key(hint) != artist_key(info["artist"]):
                src2 = pick_source(hint)
                alb2, q2 = resolve_album(hint, info["album_guess"], durations_, src2)
                opts.append((alb2, q2, src2))
        # 用"专辑里有几首歌能对上文件名"选优，而不是比质量分
        ranked = [(_title_hits(o[0], titles), (o[0] or {}).get("_name_sim") or 0, o[1], o)
                  for o in opts]
        hits_best, _, _, best = max(ranked, key=lambda x: x[:3])
        alb, q, src = best
        # 闸门：一首都对不上，且分数和专辑名都不硬，说明配错了专辑
        # （例如把《天台电影原声带》配成《不能说的秘密 电影原声带》）。
        # 宁可弃用、退回单曲搜索，也不要把整组文件的专辑/年份/曲序一起写错。
        if alb and hits_best == 0 and q < 0.45 \
                and (alb.get("_name_sim") or 0) < 0.7:
            alb = None
            q = 0.0
        recs = []
        for f in g["files"]:
            pinfo = info_with_tags(f["path"])
            out = {
                "path": f["path"], "ext": f["ext"],
                "artist_guess": pinfo["artist"], "title_guess": pinfo["title"],
                "album_guess": pinfo["album_guess"], "track_hint": pinfo["track_hint"],
                "duration": round(f["dur"], 1), "hit": False, "score": round(q, 3),
                "album": "", "year": "", "track": "", "disc": "",
                "m_title": "", "m_artist": "", "source": f"{src}-album",
                "cover": "", "lyric": "",
            }
            if alb:
                out.update({"album": alb["album"], "year": alb["year"],
                            "cover": alb.get("cover_big") or alb.get("cover") or ""})
                locate_track(alb, pinfo, pinfo["title"], f["dur"], out)
                _fill_songmid(alb, out)
            recs.append((f, out))
        with glock:
            gdone[0] += 1
            if gdone[0] % 20 == 0 or gdone[0] == len(groups):
                el = time.time() - t0
                print(f"    专辑 {gdone[0]}/{len(groups)}  "
                      f"已用 {el/60:.1f}min  {'命中' if alb else '未命中'}")
        return recs

    all_recs = []
    items = list(groups.items())
    with ThreadPoolExecutor(max_workers=max(2, args.workers // 2)) as ex:
        for recs in ex.map(do_group, items):
            for f, out in recs:
                all_recs.append((f, out))

    result_map = {out["path"]: out for f, out in all_recs}
    album_ok = sum(1 for f, o in all_recs if o.get("track"))

    # ---------- Phase 3：专辑没定位到的，退回单曲搜索 ----------
    pending = [f for f, o in all_recs if not o.get("track")]
    print(f"  专辑定位成功 {album_ok}/{len(rows)}，剩余 {len(pending)} 个走单曲搜索")
    done = [0]
    lock = threading.Lock()

    def work(f):
        try:
            rec = match_one(f["path"], f["dur"])
        except Exception as e:
            rec = {"path": f["path"], "hit": False, "err": repr(e)[:80]}
        rec["ext"] = f["ext"]
        # 专辑阶段给出的结果更可信时保留
        prev = result_map.get(f["path"])
        if prev and prev.get("track") and not rec.get("track"):
            rec = prev
        with lock:
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == len(pending):
                el = time.time() - t0
                print(f"    单曲 {done[0]}/{len(pending)}  已用 {el/60:.1f}min")
        return rec

    if pending:
        with ThreadPoolExecutor(args.workers) as ex:
            for rec in ex.map(work, pending):
                result_map[rec["path"]] = rec

    results = [result_map[r["path"]] for r in rows]

    out = args.out or "name_result.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    csvp = os.path.splitext(out)[0] + ".csv"
    fields = ["path", "ext", "artist_guess", "title_guess", "duration", "hit", "score",
              "m_title", "m_artist", "album", "year", "track", "track_src", "conflict",
              "disc", "track_num", "source", "cover", "songmid", "song_id", "suggest", "err"]
    with open(csvp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    hit = sum(1 for r in results if r.get("track"))
    havetrack = sum(1 for r in results if r.get("track"))
    haveyear = sum(1 for r in results if r.get("year"))
    print(f"\n完成 {len(results)} 个，用时 {(time.time()-t0)/60:.1f} 分钟")
    print(f"  高置信命中(含曲序): {hit} ({hit*100//max(len(results),1)}%)")
    print(f"  有曲序: {havetrack}   有年份: {haveyear}")
    src = {}
    for r in results:
        if r.get("track_src"):
            src[r["track_src"]] = src.get(r["track_src"], 0) + 1
    print(f"  曲序来源: {src}")
    cf = sum(1 for r in results if r.get("conflict"))
    print(f"  曲序信号冲突(待复核): {cf}")
    print(f"  输出: {out} / {csvp}")


def cmd_fp(args):
    rows = filter_pack(load_index(), args.pack)
    if args.limit:
        rows = rows[: args.limit]
    key = args.acoustid_key or os.environ.get("ACOUSTID_KEY", "")
    print(f"指纹路线：{len(rows)} 个文件，{args.workers} 线程，"
          f"AcoustID {'已配置' if key else '未配置（只算指纹，不做外部查询）'}")

    def work(r):
        dur = duration_of(r["path"])
        raw = chromaprint_raw(r["path"])
        rec = {"path": r["path"], "ext": r["ext"], "duration": round(dur, 1),
               "fp_ok": bool(raw), "fp_frames": len(raw) if raw else 0}
        if raw:
            rec["_raw"] = raw
            rec["fp"] = ",".join(str(x) for x in raw[:6]) + "..."
            if key:
                b64 = chromaprint_b64(r["path"])
                if b64:
                    hit = acoustid_lookup(b64, dur, key)
                    if hit:
                        rec["fp_title"] = hit["title"]
                        rec["fp_artist"] = " / ".join(hit["artists"])
                        rec["fp_album"] = " / ".join([g for g in hit["releasegroups"] if g][:2])
                        rec["fp_score"] = hit["score"]
        return rec

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(args.workers) as ex:
        for rec in ex.map(work, rows):
            results.append(rec)
            if len(results) % 20 == 0:
                print(f"  {len(results)}/{len(rows)}  已用 {(time.time()-t0)/60:.1f}min")

    # 库内两两相似度（只在样本量可控时做，避免 O(n^2) 爆炸）
    if args.dedupe_check:
        n = len(results)
        print(f"\n库内指纹比对：{n} 个文件，{n*(n-1)//2} 组")
        pairs = []
        for i in range(n):
            if not results[i].get("_raw"):
                continue
            for j in range(i + 1, n):
                if not results[j].get("_raw"):
                    continue
                s = fp_similarity(results[i]["_raw"], results[j]["_raw"])
                if s >= args.threshold:
                    pairs.append({"a": results[i]["path"], "b": results[j]["path"],
                                  "sim": round(s, 3)})
        pairs.sort(key=lambda x: -x["sim"])
        pp = os.path.splitext(args.out or "fp_result.json")[0] + "_pairs.csv"
        with open(pp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["sim", "a", "b"])
            w.writeheader()
            w.writerows(pairs)
        print(f"  相似度≥{args.threshold} 的组: {len(pairs)}  输出 {pp}")

    for r in results:
        r.pop("_fp", None)
    out = args.out or "fp_result.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    ok = sum(1 for r in results if r.get("fp_ok"))
    ided = sum(1 for r in results if r.get("fp_title"))
    print(f"\n完成 {len(results)} 个，用时 {(time.time()-t0)/60:.1f} 分钟")
    print(f"  指纹计算成功: {ok} ({ok*100//max(len(results),1)}%)")
    if key:
        print(f"  AcoustID 认出: {ided}")
    print(f"  输出: {out}")


_fp_decode_cache = {}


def decode_fp(b64: str):
    """chromaprint 的 base64 是 URL-safe 且无 padding：'-'→'+'，'_'→'/'，再补齐 '='。"""
    import base64, struct
    if b64 in _fp_decode_cache:
        return _fp_decode_cache[b64]
    try:
        s = b64.replace("-", "+").replace("_", "/").strip()
        s += "=" * (-len(s) % 4)
        buf = base64.b64decode(s)
        n = len(buf) // 4
        v = list(struct.unpack(f"<{n}i", buf[: n * 4]))
    except Exception:
        v = []
    _fp_decode_cache[b64] = v
    return v


def cmd_compare(args):
    with open(args.name, encoding="utf-8") as f:
        names = {r["path"]: r for r in json.load(f)}
    with open(args.fp, encoding="utf-8") as f:
        fps = {r["path"]: r for r in json.load(f)}

    both, only_n, only_f, agree, disagree = [], [], [], [], []
    for p, n in names.items():
        f = fps.get(p)
        if not f or not f.get("fp_title"):
            (only_n if n.get("hit") else both).append(p) if False else None
            if n.get("hit"):
                only_n.append(p)
            continue
        if not n.get("hit") and not n.get("m_title"):
            only_f.append(p)
            continue
        a = n.get("m_title") or ""
        b = f.get("fp_title") or ""
        rec = {"path": p, "name_title": a, "name_artist": n.get("m_artist"),
               "name_album": n.get("album"), "name_year": n.get("year"),
               "name_track": n.get("track"), "name_score": n.get("score"),
               "fp_title": b, "fp_artist": f.get("fp_artist"),
               "fp_album": f.get("fp_album"), "fp_score": f.get("fp_score"),
               "title_sim": round(sim(title_key(a), title_key(b)), 3)}
        both.append(rec)
        (agree if rec["title_sim"] >= 0.72 else disagree).append(rec)

    print(f"名称路线有结果: {len(names)}  指纹路线有结果: {sum(1 for x in fps.values() if x.get('fp_title'))}")
    print(f"两条都有: {len(both)}   仅名称: {len(only_n)}   仅指纹: {len(only_f)}")
    print(f"  曲名一致(≥0.72): {len(agree)}   不一致: {len(disagree)}")

    out = args.out or "compare.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["path", "name_title", "name_artist", "name_album",
                                          "name_year", "name_track", "name_score",
                                          "fp_title", "fp_artist", "fp_album", "fp_score",
                                          "title_sim"], extrasaction="ignore")
        w.writeheader()
        w.writerows(both)
    print(f"  输出: {out}")
    if disagree:
        print("\n  不一致样例:")
        for r in disagree[:8]:
            print(f"    名称={r['name_title'][:20]!r} 指纹={r['fp_title'][:20]!r} "
                  f"| {r['path'].split(chr(92))[-1][:40]}")


def main():
    ap = argparse.ArgumentParser(description="音乐库元数据识别 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("match", help="名称路线：搜单曲 + 查专辑曲目表")
    m.add_argument("--pack", default="", help="只处理某个歌手包（如 张韶涵）")
    m.add_argument("--all", action="store_true", help="全库")
    m.add_argument("--limit", type=int, default=0)
    m.add_argument("--workers", type=int, default=4)
    m.add_argument("--out", default="")
    m.set_defaults(func=cmd_match)

    f = sub.add_parser("fp", help="指纹路线：Chromaprint")
    f.add_argument("--pack", default="")
    f.add_argument("--limit", type=int, default=0)
    f.add_argument("--workers", type=int, default=4)
    f.add_argument("--acoustid-key", default="")
    f.add_argument("--dedupe-check", action="store_true", help="做库内两两相似度比对")
    f.add_argument("--threshold", type=float, default=0.85)
    f.add_argument("--out", default="")
    f.set_defaults(func=cmd_fp)

    c = sub.add_parser("compare", help="对比两份结果")
    c.add_argument("name")
    c.add_argument("fp")
    c.add_argument("--out", default="")
    c.set_defaults(func=cmd_compare)

    w = sub.add_parser("write", help="把结果写进音频标签（曲序/年份/专辑/封面/歌词）")
    w.add_argument("--from", dest="src_json", required=True, help="match 输出的 json")
    w.add_argument("--pack", default="", help="只处理某个歌手包")
    w.add_argument("--limit", type=int, default=0)
    w.add_argument("--workers", type=int, default=4)
    w.add_argument("--dry-run", dest="dry", action="store_true", help="只预览不写盘")
    w.add_argument("--no-cover", dest="cover", action="store_false", help="不写封面")
    w.add_argument("--no-lyric", dest="lyric", action="store_false", help="不写歌词")
    w.add_argument("--resume", action="store_true",
                   help="断点续写：标签已达标的文件直接跳过，不重复重写")
    w.add_argument("--exclude", default="", help="文件路径清单（每行一个），这些文件跳过")
    w.add_argument("--only", default="", help="只跑指定清单里的文件（格式：错误\\t路径）")
    w.set_defaults(func=_cmd_write_proxy, cover=True, lyric=True)

    args = ap.parse_args()
    args.func(args)


def _cmd_write_proxy(args):
    """write 的实现放在 writer.py，按需导入以免 mutagen 缺失影响其他子命令。"""
    try:
        from . import writer as mtcli_write
    except ImportError as e:
        sys.exit(f"缺少写标签依赖：{e}\n  python -m pip install --index-url "
                 f"https://pypi.org/simple/ mutagen")
    mtcli_write.cmd_write(args)


if __name__ == "__main__":
    main()
