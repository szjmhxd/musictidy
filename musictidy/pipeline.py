"""musictidy.pipeline — 高层流水线 API（GUI 与 CLI 共用）。

把 core/writer/quality/exporter 的批处理流程封装成带进度回调的函数，
不绑定具体文件夹结构，任何音乐库都能跑：

    scan → match（识别） → write（写标签） → organize（归档） → export（歌词封面）
"""
from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import core
from .core import (
    AUDIO_EXT, parse_path, pick_source, resolve_album, locate_track,
    match_one, duration_of, artist_key, title_key, sim, _album_key,
    _fill_songmid,
)
from . import writer as _writer

BAD_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize(name: str) -> str:
    """目录/文件名安全化。"""
    name = BAD_CHARS.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "未知"


def scan_files(root: str) -> list[dict]:
    """扫描文件夹，返回 [{path, ext, size}] 列表。"""
    out = []
    for dp, dn, fn in os.walk(root):
        for f in fn:
            ext = os.path.splitext(f)[1].lower()
            if ext in AUDIO_EXT:
                p = os.path.join(dp, f)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = 0
                out.append({"path": p, "ext": ext, "size": sz})
    return out


def match_files(rows: list[dict], workers: int = 8, log=print) -> list[dict]:
    """名称路线识别：按（歌手, 专辑）分组查专辑曲目表，剩余走单曲搜索。

    rows: scan_files 的输出（可只给子集）；返回与 core.cmd_match 相同结构的记录，
    每条含 m_title/m_artist/album/year/track/cover 等字段。
    """
    t0 = time.time()
    rows = [dict(r) for r in rows]
    log(f"识别 {len(rows)} 个文件，{workers} 线程")

    log("探测时长 ...")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        durations = list(ex.map(lambda r: duration_of(r["path"]), rows))
    for r, d in zip(rows, durations):
        r["dur"] = d

    groups = {}
    for r in rows:
        info = parse_path(r["path"])
        key = (artist_key(info["artist"]), _album_key(info["album_guess"]))
        groups.setdefault(key, {"info": info, "files": []})["files"].append(r)
    log(f"分 {len(groups)} 组（每组一次专辑查询）")

    def _title_hits(alb: dict, titles: list) -> int:
        if not alb:
            return 0
        keys = [title_key(s.get("title") or "") for s in alb.get("songs") or []]
        return sum(1 for t in titles
                   if any(sim(title_key(t), k) >= 0.85 for k in keys if k))

    gdone = [0]
    glock = threading.Lock()

    def do_group(item):
        (_, _), g = item
        info = g["info"]
        src = pick_source(info["artist"])
        durations_ = [f["dur"] for f in g["files"]]
        titles = [parse_path(f["path"])["title"] for f in g["files"]]
        alb, q = resolve_album(info["artist"], info["album_guess"], durations_, src)
        opts = [(alb, q, src)]
        hints = [parse_path(f["path"]).get("artist_hint") for f in g["files"]]
        hints = [h for h in hints if h]
        if hints:
            hint = Counter(hints).most_common(1)[0][0]
            if artist_key(hint) != artist_key(info["artist"]):
                src2 = pick_source(hint)
                alb2, q2 = resolve_album(hint, info["album_guess"], durations_, src2)
                opts.append((alb2, q2, src2))
        ranked = [(_title_hits(o[0], titles), (o[0] or {}).get("_name_sim") or 0, o[1], o)
                  for o in opts]
        hits_best, _, _, best = max(ranked, key=lambda x: x[:3])
        alb, q, src = best
        if alb and hits_best == 0 and q < 0.45 \
                and (alb.get("_name_sim") or 0) < 0.7:
            alb = None
            q = 0.0
        recs = []
        for f in g["files"]:
            pinfo = parse_path(f["path"])
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
            if gdone[0] % 10 == 0 or gdone[0] == len(groups):
                log(f"  专辑 {gdone[0]}/{len(groups)}  {'命中' if alb else '未命中'}")
        return recs

    all_recs = []
    with ThreadPoolExecutor(max_workers=max(2, workers // 2)) as ex:
        for recs in ex.map(do_group, list(groups.items())):
            all_recs.extend(recs)

    result_map = {out["path"]: out for f, out in all_recs}
    pending = [f for f, o in all_recs if not o.get("track")]
    log(f"专辑定位成功 {len(all_recs) - len(pending)}/{len(rows)}，"
        f"剩余 {len(pending)} 个走单曲搜索")

    done = [0]
    lock = threading.Lock()

    def work(f):
        try:
            rec = match_one(f["path"], f["dur"])
        except Exception as e:
            rec = {"path": f["path"], "hit": False, "err": repr(e)[:80]}
        rec["ext"] = f["ext"]
        prev = result_map.get(f["path"])
        if prev and prev.get("track") and not rec.get("track"):
            rec = prev
        with lock:
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == len(pending):
                log(f"  单曲 {done[0]}/{len(pending)}")
        return rec

    if pending:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for rec in ex.map(work, pending):
                result_map[rec["path"]] = rec

    results = [result_map[r["path"]] for r in rows]
    hit = sum(1 for r in results if r.get("track"))
    log(f"识别完成 {len(results)} 个，命中 {hit} 个，用时 {(time.time()-t0)/60:.1f} 分钟")
    return results


def write_records(records: list[dict], workers: int = 8, dry: bool = False,
                  cover: bool = True, lyric: bool = True, resume: bool = True,
                  log=print) -> list[dict]:
    """把识别结果写进音频标签。dry=True 只预览不写盘。"""
    todo = [r for r in records if r.get("track")]
    log(f"待写入 {len(todo)} 个（共 {len(records)} 条）"
        f"{'  [dry-run 不写盘]' if dry else ''}")

    stat = {"ok": 0, "skip": 0, "fail": 0, "cover": 0, "lyric": 0}
    errs = []
    lock = threading.Lock()
    done = [0]

    def work(rec):
        path = rec["path"]
        res = {"path": path, "cover": False, "lyric": False}
        try:
            if resume and _writer.tags_already_ok(path, rec, cover, lyric):
                res["skip"] = "标签已达标"
            else:
                cover_bytes = b""
                if rec.get("cover") and cover:
                    cp = _writer.cover_path_for(rec["cover"])
                    if dry:
                        res["cover"] = os.path.exists(cp)
                    elif _writer.download(rec["cover"], cp):
                        for _ in range(3):
                            try:
                                with open(cp, "rb") as cf:
                                    cover_bytes = cf.read()
                                break
                            except Exception:
                                time.sleep(0.4)
                        res["cover"] = bool(cover_bytes)
                lyric_txt = ""
                if lyric:
                    lyric_txt = _writer.fetch_lyric_for(rec, dry=dry)
                    res["lyric"] = bool(lyric_txt)
                if dry:
                    res["preview"] = {
                        "title": rec.get("m_title") or rec.get("title_guess"),
                        "artist": rec.get("m_artist") or rec.get("artist_guess"),
                        "album": rec.get("album"), "year": rec.get("year"),
                        "track": rec.get("track"),
                        "cover": bool(rec.get("cover")), "lyric_len": len(lyric_txt),
                    }
                else:
                    res["err"] = _writer.write_tags(path, rec, cover_bytes, lyric_txt)
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
            if done[0] % 25 == 0 or done[0] == len(todo):
                log(f"  进度 {done[0]}/{len(todo)}  成功 {stat['ok']}  "
                    f"跳过 {stat['skip']}  失败 {stat['fail']}")
        return res

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(work, todo):
            results.append(r)

    log(f"写入完成：成功 {stat['ok']}  跳过 {stat['skip']}  失败 {stat['fail']}  "
        f"封面 {stat['cover']}  歌词 {stat['lyric']}")
    if errs:
        log("失败样例：")
        for p, e in errs[:5]:
            log(f"  {os.path.basename(p)[:32]:34} {e}")
    return results


def build_plan(records: list[dict], library_root: str) -> list[dict]:
    """从识别结果生成归档计划。

    目标结构：<library_root>\\<歌手>\\<年份> - <专辑>\\NN 曲名.ext
    无曲序的用「曲名.ext」（不带序号前缀）；同目标撞车时同录音留高音质，
    不同录音加 " (2)" 后缀。返回 [{src, dst, action, note}]。
    """
    def lead_artist(rec):
        a = rec.get("m_artist") or rec.get("artist_guess") or "未知"
        for sep in ("、", ",", "/", "&", ";", " feat. ", " feat ", "（", "("):
            if sep in a:
                a = a.split(sep)[0]
        return sanitize(a)

    plan = []
    by_target = {}
    for rec in records:
        if not rec.get("m_title") and not rec.get("title_guess"):
            continue
        artist = lead_artist(rec)
        album = sanitize(rec.get("album") or rec.get("album_guess") or "未收录")
        year = str(rec.get("year") or "").strip()
        album_dir = f"{year} - {album}" if year and year != "0" else album
        title = sanitize(rec.get("m_title") or rec.get("title_guess"))
        track = str(rec.get("track") or "").strip()
        stem = f"{int(track):02d} {title}" if track.isdigit() and int(track) > 0 else title
        ext = rec.get("ext") or os.path.splitext(rec["path"])[1]
        dst = os.path.join(library_root, artist, album_dir, stem + ext)
        key = dst.lower()
        note = ""
        if key in by_target:
            prev = by_target[key]
            same = abs((prev.get("duration") or 0) - (rec.get("duration") or 0)) <= 3.0
            if same:
                # 同录音：留曲序/命中更可靠的一份，另一份标 dup
                plan.append({"src": rec["path"], "dst": dst, "action": "dup",
                             "note": "与已计划条目同录音，冗余"})
                continue
            stem2 = stem + " (2)"
            dst = os.path.join(library_root, artist, album_dir, stem2 + ext)
            note = "同名不同录音，加 (2)"
        by_target[dst.lower()] = rec
        plan.append({"src": rec["path"], "dst": dst,
                     "action": "move" if os.path.exists(rec["path"]) else "missing",
                     "note": note})
    return plan


def execute_plan(plan: list[dict], log=print) -> list[dict]:
    """执行归档移动（同卷 rename）。返回执行结果，失败不中断。"""
    results = []
    for i, item in enumerate(plan):
        if item["action"] != "move":
            results.append({**item, "result": item["action"]})
            continue
        src, dst = item["src"], item["dst"]
        res = dict(item)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                res["result"] = "目标已存在"
            else:
                os.replace(src, dst)
                res["result"] = "已移动"
        except Exception as e:
            res["result"] = f"失败: {type(e).__name__}"
        results.append(res)
        if (i + 1) % 50 == 0 or i + 1 == len(plan):
            log(f"  移动 {i + 1}/{len(plan)}")
    ok = sum(1 for r in results if r.get("result") == "已移动")
    log(f"归档完成：移动 {ok}/{len(plan)}")
    return results


def save_json(obj, path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def save_csv(rows: list[dict], path: str, fields: list[str] | None = None):
    if not rows:
        return
    fields = fields or list(rows[0].keys())
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
