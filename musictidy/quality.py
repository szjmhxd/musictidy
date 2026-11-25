"""音质比对：同一 (歌手, 专辑, 曲序) 组内，确认 _整理 里留的是最高音质。

原 dedup 规则是「格式优先(flac>ape>wav>mp3) + 同格式比体积」，
24bit/多声道 WAV 会输给 16bit FLAC——必须按真实规格翻案。

产出 quality_report.json：
  swaps      需要把 _重复待删 的高音质换进 _整理 的组
  covered    待确认文件中，曲目在 _整理 已有另一份的（删了不丢歌）
  unique_pend 待确认里 _整理 没有的歌
"""
import os
import sys
import re
import csv
import json
import collections
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")

KEEP_ROOT = os.environ.get("MTCLI_KEEP_ROOT", r"M:\_整理")
DUP_ROOT = os.environ.get("MTCLI_DUP_ROOT", r"M:\_重复待删")
PEND_ROOT = os.environ.get("MTCLI_PEND_ROOT", r"M:\_待确认")
PRIORITY = {".flac": 0, ".ape": 1, ".wav": 2, ".mp3": 3, ".m4a": 4, ".aac": 5, ".wma": 6}


def wav_fmt(path):
    """手动解析 WAV fmt 块——DTS/非 PCM 的 wave 模块读不了。"""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF":
            return {}
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                return {}
            cid, size = hdr[:4], int.from_bytes(hdr[4:8], "little")
            if cid == b"fmt ":
                d = f.read(min(size, 40))
                if len(d) < 16:
                    return {}
                fmt_tag = int.from_bytes(d[0:2], "little")
                ch = int.from_bytes(d[2:4], "little")
                rate = int.from_bytes(d[4:8], "little")
                bits = int.from_bytes(d[14:16], "little") if len(d) >= 16 else 0
                return {"channels": ch, "rate": rate, "bits": bits, "fmt_tag": fmt_tag}
            f.seek(size + (size & 1), 1)


def probe(path):
    ext = os.path.splitext(path)[1].lower()
    info = {"path": path, "ext": ext, "bits": 0, "rate": 0, "channels": 2,
            "bitrate": 0, "lossless": False, "err": ""}
    try:
        if ext == ".wav":
            info.update({k: v for k, v in wav_fmt(path).items() if k != "fmt_tag"})
            info["lossless"] = True
        elif ext == ".flac":
            from mutagen.flac import FLAC
            f = FLAC(path)
            info["bits"] = f.info.bits_per_sample or 0
            info["rate"] = f.info.sample_rate or 0
            info["channels"] = getattr(f.info, "channels", 2) or 2
            info["lossless"] = True
        elif ext == ".ape":
            from mutagen.monkeysaudio import MonkeysAudio
            f = MonkeysAudio(path)
            info["bits"] = getattr(f.info, "bits_per_sample", 0) or 0
            info["rate"] = f.info.sample_rate or 0
            info["channels"] = getattr(f.info, "channels", 2) or 2
            info["lossless"] = True
        elif ext == ".mp3":
            from mutagen.mp3 import MP3
            f = MP3(path)
            info["bitrate"] = f.info.bitrate or 0
            info["rate"] = f.info.sample_rate or 0
            info["channels"] = getattr(f.info, "channels", 2) or 2
        else:
            import mutagen
            f = mutagen.File(path)
            if f and f.info:
                info["bitrate"] = getattr(f.info, "bitrate", 0) or 0
                info["rate"] = getattr(f.info, "sample_rate", 0) or 0
    except Exception as e:
        info["err"] = str(e)[:60]
    return info


def probe_retry(path, tries=3):
    for i in range(tries):
        r = probe(path)
        if not r["err"]:
            return r
    return r


def qkey(info):
    """(无损档, 规格, 码率) 越大越好。规格 = 位深×采样率×声道。"""
    if info["lossless"]:
        spec = (info["bits"] or 16) * (info["rate"] or 44100) * (info["channels"] or 2)
        return (2, spec, 0)
    return (1, info["bitrate"], 0)


def fmt_desc(info):
    if info["lossless"]:
        return (f"{info['ext']} {info['bits']}bit/{info['rate']}Hz "
                f"{info['channels']}ch")
    return f"{info['ext']} {info['bitrate'] // 1000}kbps"


def norm_stem(fn):
    """'01 雨天 [wav]~2.wav' -> '01 雨天'"""
    stem = os.path.splitext(fn)[0]
    stem = re.sub(r"\s*\[[a-z0-9]+\]", "", stem, flags=re.I)
    stem = re.sub(r"~\d+$", "", stem)
    return stem.strip().lower()


def key_of(path):
    """(歌手, 专辑目录, 归一曲干) —— CDx 层忽略。"""
    rel = os.path.relpath(path, KEEP_ROOT if path.startswith(KEEP_ROOT) else DUP_ROOT)
    parts = rel.split("\\")
    artist = parts[0].lower()
    album = parts[1].lower() if len(parts) > 1 else ""
    stem = norm_stem(parts[-1])
    return (artist, album, stem)


def title_norm(t):
    t = re.sub(r"[\s\-–—_·\.、，,()（）\[\]【】'\"“”‘’!?？!~*]", "", str(t or "").lower())
    return t


def main():
    CACHE = "quality_cache.json"
    if os.path.exists(CACHE):
        infos = json.load(open(CACHE, encoding="utf-8"))
        print(f"读探测缓存 {len(infos)} 条", flush=True)
    else:
        jobs = []
        for root in (KEEP_ROOT, DUP_ROOT):
            for dp, dn, fn in os.walk(root):
                for f in fn:
                    if os.path.splitext(f)[1].lower() in PRIORITY:
                        jobs.append(os.path.join(dp, f))
        print(f"待探测 {len(jobs)} 个文件…", flush=True)
        with ThreadPoolExecutor(max_workers=8) as ex:
            infos = list(ex.map(probe_retry, jobs))
        for i in infos:
            try:
                i["size"] = os.path.getsize(i["path"])
            except Exception:
                i["size"] = 0
        json.dump(infos, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"探测完成，缓存 {len(infos)} 条", flush=True)
    bad = [i for i in infos if i.get("err")]
    print(f"探测失败 {len(bad)} 个", flush=True)

    # ---- 分组 ----
    groups = collections.defaultdict(list)
    for i in infos:
        if i["err"]:
            continue
        groups[key_of(i["path"])].append(i)

    swaps, kept_best, tie = [], [], []
    for k, items in sorted(groups.items()):
        if len(items) < 2:
            continue
        items.sort(key=lambda x: (qkey(x), PRIORITY.get(x["ext"], 9), -(x.get("size") or 0)),
                   reverse=True)
        best = items[0]
        in_keep = [x for x in items if x["path"].startswith(KEEP_ROOT)]
        in_dup = [x for x in items if x["path"].startswith(DUP_ROOT)]
        if not in_keep:
            continue  # 全在重复区的组不该出现
        keeper = in_keep[0]
        if best["path"] == keeper["path"]:
            kept_best.append({"key": list(k), "kept": fmt_desc(keeper),
                              "others": [fmt_desc(x) for x in items[1:]]})
        elif qkey(best) == qkey(keeper):
            tie.append({"key": list(k), "a": fmt_desc(keeper), "b": fmt_desc(best)})
        else:
            swaps.append({
                "key": list(k),
                "now_kept": {"path": keeper["path"], "spec": fmt_desc(keeper), "q": qkey(keeper)},
                "should_keep": {"path": best["path"], "spec": fmt_desc(best), "q": qkey(best)},
            })

    print(f"\n多版本组: {len(kept_best) + len(tie) + len(swaps)} 个", flush=True)
    print(f"  已是最高音质: {len(kept_best)}", flush=True)
    print(f"  同规格并列:   {len(tie)}", flush=True)
    print(f"  *** 需要翻案换入: {len(swaps)}", flush=True)
    for s in swaps:
        print(f"    {s['key'][2][:30]}  [{s['now_kept']['spec']}] -> [{s['should_keep']['spec']}]", flush=True)

    # ---- 待确认 vs 整理 重叠（用于删除范围） ----
    # 宁可漏判（多留文件）不可误判（误删独立曲目）：只认精确曲名匹配。
    try:
        import mtcli
    except Exception:
        mtcli = None

    def fname_title(p):
        base = os.path.basename(p)
        if mtcli is not None:
            try:
                info = mtcli.parse_path(p)
                t = info.get("title") or ""
                if t:
                    return title_norm(t)
            except Exception:
                pass
        stem = re.sub(r"^\d{1,3}\s*[.\-、_]\s*", "", norm_stem(base))
        stem = re.sub(r"^[^-]{1,20}\s*-\s*", "", stem)  # 去 "歌手 - " 前缀
        return title_norm(stem)

    plan_titles = collections.defaultdict(set)  # artist -> {title_norm}
    for dp, dn, fn in os.walk(KEEP_ROOT):
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), KEEP_ROOT)
            artist = rel.split("\\")[0].lower()
            stem = re.sub(r"^\d{1,3}\s*", "", norm_stem(f))
            plan_titles[artist.lower()].add(title_norm(stem))

    pend = []
    for dp, dn, fn in os.walk(PEND_ROOT):
        for f in fn:
            pend.append(os.path.join(dp, f))

    covered, unique = [], []
    for p in pend:
        rel = os.path.relpath(p, PEND_ROOT)
        artist = rel.split("\\")[0].lower()
        tn = fname_title(p)
        hit = bool(tn) and tn in plan_titles.get(artist, set())
        (covered if hit else unique).append(p)

    print(f"\n_待确认 {len(pend)} 个：", flush=True)
    print(f"  同歌手同曲名在 _整理 已有: {len(covered)}", flush=True)
    print(f"  _整理 没有的独立曲目:     {len(unique)}", flush=True)

    out = {"swaps": swaps, "tie": tie, "kept_best_n": len(kept_best),
           "pend_covered": covered, "pend_unique": unique}
    with open("quality_report.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n-> quality_report.json", flush=True)


if __name__ == "__main__":
    main()
