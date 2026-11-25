"""把 mtcli match 的结果（曲序/年份/专辑/封面/歌词）写进音频标签。

设计原则：
  1. 默认 dry-run，只报告"会写什么"，不动文件
  2. 封面/歌词先落盘缓存，同专辑的多个文件共用一张图，不重复下载
  3. 写标签用 mutagen，只改本模块负责的字段，其余保留
  4. WAV 单独处理：WAV 的 ID3 兼容性差，部分播放器读不到，默认跳过

用法：
    python mtcli.py write --from t_zjl.json --dry-run
    python mtcli.py write --from t_zjl.json --pack 周杰伦
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import core as mtcli
from .core import (
    CACHE_DIR, LIMITER, QQ_UA, qq_lyric, norm,
)

COVER_DIR = os.path.join(CACHE_DIR, "covers")
LYRIC_DIR = os.path.join(CACHE_DIR, "lyrics")
_cover_lock = threading.Lock()
_lyric_lock = threading.Lock()

# 原来跳过 WAV，因为担心 ID3 兼容性。但凤凰传奇那 120 个 WAV 里混着
# DTS 多声道和 24K 金碟版本，是刻意收藏的高规格音源，不能为了打标签去转码。
# mutagen 可以把 ID3v2 作为 id3 chunk 追加到 WAV 尾部，不动 data chunk，
# 所以照写即可。
SKIP_EXT = set()


def download(url: str, dest: str, timeout: int = 30) -> bool:
    """带缓存的封面下载。同专辑共用一个文件，避免 3121 次重复传输。"""
    with _cover_lock:
        if os.path.exists(dest) and os.path.getsize(dest) > 1024:
            return True
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for i in range(3):
        LIMITER.wait()
        try:
            req = urllib.request.Request(url, headers=QQ_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if len(data) < 1024:
                return False
            tmp = f"{dest}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
            return True
        except Exception:
            if i == 2:
                return False
    return False


def cover_path_for(url: str) -> str:
    """用 URL 里的 album mid 命名，便于同专辑复用"""
    import hashlib
    h = hashlib.md5(url.encode()).hexdigest()[:12]
    return os.path.join(COVER_DIR, f"{h}.jpg")


def lyric_path_for(songmid: str) -> str:
    return os.path.join(LYRIC_DIR, f"{songmid}.lrc")


def fetch_lyric_for(rec: dict, dry: bool = False) -> str:
    """按来源取歌词并缓存。QQ 用 songmid，网易云用 song_id。"""
    src = rec.get("source") or "netease"
    key = rec.get("songmid") if str(src).startswith("qq") else rec.get("song_id")
    if not key:
        # 有的记录只填了另一个，兜底
        key = rec.get("songmid") or rec.get("song_id")
    if not key:
        return ""
    p = lyric_path_for(str(key))
    with _lyric_lock:
        if os.path.exists(p):
            try:
                return open(p, encoding="utf-8").read()
            except Exception:
                pass
    if dry:
        return ""
    txt = mtcli.fetch_lyric(src, rec)
    if not txt:
        # 主源歌词取不到（QQ 软封时全都取不到），按歌名去酷狗找一份
        txt = mtcli.kugou_lyric_search(
            rec.get("m_title") or rec.get("title_guess") or "",
            rec.get("m_artist") or rec.get("artist_guess") or "",
            rec.get("duration") or 0)
    if txt:
        os.makedirs(LYRIC_DIR, exist_ok=True)
        # 同一首歌可能被多个线程同时命中，先写各自的临时文件再原子替换，
        # 否则并发打开同一个 .lrc 会直接 PermissionError 打断整批任务。
        tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(txt)
            os.replace(tmp, p)
        except Exception:
            # 换盘/守卫拦截时原地替换可能失败。缓存只是中间产物，
            # 内容已经拿到手了，写不进缓存不该影响这次写标签。
            try:
                os.remove(tmp)
            except Exception:
                pass
    return txt


# ---------------------------------------------------------------- 写标签
def tags_already_ok(path: str, rec: dict, want_cover: bool, want_lyric: bool) -> bool:
    """续写用：判断标签是否已达标，避免重复重写上百 MB 的文件。

    只比关键字段，不比封面图片二进制。
    """
    from mutagen import File as MFile
    try:
        f = MFile(path)
    except Exception:
        return False
    if f is None:
        return False
    t = f.tags or {}

    def g(*keys):
        for k in keys:
            # FLAC 的 VComment 遇到 MP4 键（\xa9lyr 之类）会直接抛 ValueError，
            # 所以逐个键 try，别让一个不兼容的键打断判断。
            try:
                if k in t:
                    v = t[k]
                    return str(v[0]) if isinstance(v, list) and v else str(v)
            except Exception:
                continue
        return ""

    ext = os.path.splitext(path)[1].lower()
    title = (rec.get("m_title") or rec.get("title_guess") or "").strip()
    album = (rec.get("album") or "").strip()
    year = str(rec.get("year") or "").strip()
    track = str(rec.get("track_num") or rec.get("track") or "").strip()

    cur_title = g("title", "TIT2", "\xa9nam", "Title").strip()
    cur_album = g("album", "TALB", "\xa9alb", "Album").strip()
    cur_track = g("tracknumber", "TRCK", "Track").strip()
    cur_date = g("date", "TDRC", "\xa9day", "Year").strip()

    if title and cur_title != title:
        return False
    if album and cur_album != album:
        return False
    if track and cur_track.lstrip("0") != track.lstrip("0"):
        return False
    if year and not cur_date.startswith(year[:4]):
        return False

    if want_cover:
        if ext in (".mp3", ".ape", ".wav"):
            if not any("APIC" in str(k) or "Cover Art" in str(k) for k in t.keys()):
                return False
        elif not (getattr(f, "pictures", None) or []):
            return False

    if want_lyric:
        lyr = g("lyrics", "Lyrics", "\xa9lyr")
        if not lyr:
            lyr = "".join(str(t[k]) for k in t.keys() if "USLT" in str(k))
        if not lyr.strip():
            return False
    return True


def _write_tags_once(path: str, rec: dict, cover_bytes: bytes, lyric: str) -> str:
    """返回 '' 表示成功，否则返回错误描述"""
    from mutagen import File as MFile
    from mutagen.flac import Picture
    ext = os.path.splitext(path)[1].lower()

    try:
        f = MFile(path)
    except Exception as e:
        return f"打不开: {e!r}"[:80]
    if f is None:
        return "不是可识别的音频"

    title = rec.get("m_title") or rec.get("title_guess") or ""
    artist = rec.get("m_artist") or rec.get("artist_guess") or ""
    album = rec.get("album") or ""
    year = str(rec.get("year") or "")
    # track_num 是碟内序号（双碟的 CD2 从 1 重新数），track 是专辑连续序号。
    # 写标签要用碟内序号，否则 CD2 第一首会变成第 23 首。
    track = str(rec.get("track_num") or rec.get("track") or "")
    disc = str(rec.get("disc") or "")

    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            f = FLAC(path)
            if title:
                f["title"] = title
            if artist:
                f["artist"] = artist
            if album:
                f["album"] = album
            if year:
                f["date"] = year
            if track:
                f["tracknumber"] = str(int(track)) if track.isdigit() else track
            if disc and disc not in ("0", ""):
                f["discnumber"] = str(int(disc)) if disc.isdigit() else disc
            elif "discnumber" in f:
                del f["discnumber"]
            if lyric:
                f["lyrics"] = lyric
            if cover_bytes:
                f.clear_pictures()
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.data = cover_bytes
                f.add_picture(pic)
            f.save()
            return ""

        if ext == ".ape":
            # Monkey's Audio 用 APEv2 标签，字段名是自定的字符串键
            from mutagen.apev2 import APEBinaryValue
            from mutagen.monkeysaudio import MonkeysAudio
            f = MonkeysAudio(path)
            if f.tags is None:
                f.add_tags()
            t = f.tags
            for k, v in (("Title", title), ("Artist", artist),
                         ("Album", album), ("Year", year)):
                if v:
                    t[k] = v
                elif k in t:
                    del t[k]
            if track:
                t["Track"] = str(int(track)) if track.isdigit() else track
            if disc and disc not in ("0", ""):
                t["Disc"] = str(int(disc)) if disc.isdigit() else disc
            elif "Disc" in t:
                del t["Disc"]
            if lyric:
                t["Lyrics"] = lyric
            if cover_bytes:
                # 键名固定，值必须带 "文件名\0" 前缀，否则播放器不认
                if "Cover Art (Front)" in t:
                    del t["Cover Art (Front)"]
                t["Cover Art (Front)"] = APEBinaryValue(b"cover.jpg\x00" + cover_bytes)
            f.save()
            return ""

        if ext in (".mp3", ".m4a", ".aac", ".wma"):
            # MP3 用 ID3，m4a 需要 MP4 标签，分开处理
            if ext == ".m4a":
                f["\xa9nam"] = [title] if title else []
                f["\xa9ART"] = [artist] if artist else []
                f["\xa9alb"] = [album] if album else []
                f["\xa9day"] = [year] if year else []
                if cover_bytes:
                    from mutagen.mp4 import MP4Cover
                    f["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
                if lyric:
                    f["\xa9lyr"] = [lyric]
                f.save()
                return ""
            from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TRCK, TPOS, USLT
            try:
                tags = ID3(path)
            except Exception:
                tags = ID3()
            if title:
                tags.setall("TIT2", [TIT2(encoding=3, text=title)])
            if artist:
                tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
            if album:
                tags.setall("TALB", [TALB(encoding=3, text=album)])
            if year:
                tags.setall("TDRC", [TDRC(encoding=3, text=year)])
            if track:
                tags.setall("TRCK", [TRCK(encoding=3, text=str(int(track)) if track.isdigit() else track)])
            if disc and disc not in ("0", ""):
                tags.setall("TPOS", [TPOS(encoding=3, text=str(int(disc)) if disc.isdigit() else disc)])
            else:
                tags.delall("TPOS")
            if lyric:
                tags.delall("USLT")
                tags.add(USLT(encoding=3, lang="chi", desc="", text=lyric))
            if cover_bytes:
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
            tags.save(path)
            return ""

        if ext == ".wav":
            # WAV 用 ID3v2，作为 id3 chunk 追加在文件尾部，音频数据不动。
            # DTS/24K 这类高规格音源必须走这条路，转码会破坏原编码。
            from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TRCK, TPOS, USLT
            from mutagen.wave import WAVE
            w = WAVE(path)
            if w.tags is None:
                w.add_tags()
            tags = w.tags
            if title:
                tags.setall("TIT2", [TIT2(encoding=3, text=title)])
            if artist:
                tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
            if album:
                tags.setall("TALB", [TALB(encoding=3, text=album)])
            if year:
                tags.setall("TDRC", [TDRC(encoding=3, text=year)])
            if track:
                tags.setall("TRCK", [TRCK(encoding=3, text=str(int(track)) if track.isdigit() else track)])
            if disc and disc not in ("0", ""):
                tags.setall("TPOS", [TPOS(encoding=3, text=str(int(disc)) if disc.isdigit() else disc)])
            else:
                tags.delall("TPOS")
            if lyric:
                tags.delall("USLT")
                tags.add(USLT(encoding=3, lang="chi", desc="", text=lyric))
            if cover_bytes:
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
            w.save()
            return ""

        return f"未支持的格式 {ext}"
    except Exception as e:
        return f"写入失败: {type(e).__name__} {e}"[:100]


def write_tags(path: str, rec: dict, cover_bytes: bytes, lyric: str) -> str:
    """SMB 并发读会偶发短读，mutagen 就抛一个空 ValueError。

    这种失败不是文件坏了，重试一次基本就过。只在最后仍失败时上报。
    """
    last = ""
    for i in range(3):
        last = _write_tags_once(path, rec, cover_bytes, lyric)
        if not last:
            return ""
        if "未支持" in last or "不是可识别" in last:
            return last
        time.sleep(1.0 + i)
    return last


def cmd_write(args):
    data = json.load(open(args.src_json, encoding="utf-8"))
    if args.pack:
        data = [r for r in data if args.pack in r["path"].split("\\")[1]]
    if args.limit:
        data = data[: args.limit]
    # 只处理已经拿到曲序的
    todo = [r for r in data if r.get("track")]
    # 只重跑失败清单里的文件（err txt 每行：错误\t路径）
    if getattr(args, "only", ""):
        with open(args.only, encoding="utf-8") as fh:
            want = {ln.rstrip("\n").split("\t")[-1] for ln in fh if ln.strip()}
        todo = [r for r in todo if r["path"] in want]
        print(f"按失败清单筛选出 {len(todo)} 个")
    # 被 NAS 会话锁住的文件读不动，会让线程永久挂住，先剔除
    if getattr(args, "exclude", ""):
        with open(args.exclude, encoding="utf-8") as fh:
            bad = {line.strip() for line in fh if line.strip()}
        before = len(todo)
        todo = [r for r in todo if r["path"] not in bad]
        print(f"排除锁定文件 {before - len(todo)} 个")
    print(f"待写入 {len(todo)} 个（共 {len(data)} 条结果）"
          f"{'  [dry-run 不写盘]' if args.dry else ''}")

    stat = {"ok": 0, "skip": 0, "fail": 0, "cover": 0, "lyric": 0, "nochange": 0}
    errs = []
    lock = threading.Lock()
    done = [0]

    def work(rec):
        path = rec["path"]
        ext = os.path.splitext(path)[1].lower()
        res = {"path": path, "cover": False, "lyric": False}
        # 任何单文件异常都不能打断整批，否则一次竞态就白跑几十分钟
        try:
            if ext in SKIP_EXT:
                res["skip"] = f"{ext} 跳过（ID3 兼容性差）"
            elif getattr(args, "resume", False) and tags_already_ok(
                    path, rec, args.cover, args.lyric):
                res["skip"] = "标签已达标"
            else:
                cover_bytes = b""
                if rec.get("cover") and args.cover:
                    cp = cover_path_for(rec["cover"])
                    if args.dry:
                        cover_bytes = b"" if not os.path.exists(cp) else b"x"
                        res["cover"] = os.path.exists(cp)
                    elif download(rec["cover"], cp):
                        # 另一线程可能正在 os.replace 这张图，读会瞬时失败
                        for _ in range(3):
                            try:
                                with open(cp, "rb") as cf:
                                    cover_bytes = cf.read()
                                break
                            except Exception:
                                time.sleep(0.4)
                        res["cover"] = bool(cover_bytes)
                    else:
                        res["cover_err"] = "封面下载失败"
                lyric = ""
                if args.lyric:
                    lyric = fetch_lyric_for(rec, dry=args.dry)
                    res["lyric"] = bool(lyric)
                if args.dry:
                    res["preview"] = {
                        "title": rec.get("m_title") or rec.get("title_guess"),
                        "artist": rec.get("m_artist") or rec.get("artist_guess"),
                        "album": rec.get("album"), "year": rec.get("year"),
                        "track": rec.get("track"), "disc": rec.get("disc"),
                        "cover": bool(rec.get("cover")), "lyric_len": len(lyric),
                    }
                else:
                    res["err"] = write_tags(path, rec, cover_bytes, lyric)
        except Exception as e:
            res["err"] = f"{type(e).__name__}: {e}"[:100]
        with lock:
            done[0] += 1
            if "skip" in res:
                stat["skip"] += 1
            elif res.get("err"):
                stat["fail"] += 1
                errs.append((path, res["err"]))
            else:
                stat["ok"] += 1
            if res.get("cover"):
                stat["cover"] += 1
            if res.get("lyric"):
                stat["lyric"] += 1
            if done[0] % 50 == 0 or done[0] == len(todo):
                print(f"  {done[0]}/{len(todo)}  成功 {stat['ok']}  跳过 {stat['skip']}  失败 {stat['fail']}")
        return res

    results = []
    with ThreadPoolExecutor(args.workers) as ex:
        for r in ex.map(work, todo):
            results.append(r)

    if args.dry:
        print("\n=== dry-run 预览（前 15 条）===")
        for r in results[:15]:
            p = r.get("preview") or {}
            print(f"  {r['path'].split(chr(92))[-1][:32]:34} "
                  f"{p.get('artist','')[:8]:9}/{p.get('album','')[:14]:15} "
                  f"{p.get('year','')} #{p.get('track','')} "
                  f"封面={'Y' if p.get('cover') else '-'} 歌词={p.get('lyric_len',0)}")
            if r.get("skip"):
                print(f"      → {r['skip']}")

    jp = os.path.splitext(args.src_json)[0] + ("_write_dry.json" if args.dry else "_write.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    print(f"\n合计：成功 {stat['ok']}  跳过 {stat['skip']}  失败 {stat['fail']}")
    print(f"  带封面 {stat['cover']}  带歌词 {stat['lyric']}")
    if errs:
        ep = os.path.splitext(args.src_json)[0] + "_write_err.txt"
        with open(ep, "w", encoding="utf-8") as f:
            for p, e in errs:
                f.write(f"{e}\t{p}\n")
        print("  失败样例：")
        for p, e in errs[:8]:
            print(f"    {p.split(chr(92))[-1][:34]:36} {e}")
        print(f"  失败清单: {ep}")
    print(f"  明细: {jp}")
