"""从嵌入标签导出外置歌词与封面。

  歌词: 每个音频旁写同名 .lrc（带 [mm:ss] 时间戳，播放器可直接滚动）
  封面: 每个专辑目录写 cover.jpg + folder.jpg（同一张图，兼容两种惯例）

嵌入标签保持不动；已有同名 .lrc/封面不覆盖（幂等）。
无歌词的纯音乐不生成 .lrc。封面非 JPEG 的用 ffmpeg 转成 JPEG。
"""
import os
import sys
import subprocess
import collections

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mutagen

ROOT = os.environ.get("MTCLI_ROOT", r"M:\_整理")
TMP = os.path.join(os.environ.get("MTCLI_CACHE") or os.path.join(
    os.path.expanduser("~"), ".mtcli_cache"), "cov")
AUDIO = {".flac", ".mp3", ".wav", ".ape", ".m4a"}
os.makedirs(TMP, exist_ok=True)


def get_lyrics(path):
    try:
        mf = mutagen.File(path)
    except Exception:
        return ""
    tags = getattr(mf, "tags", None)
    if not tags:
        return ""
    best = ""
    for k, v in tags.items():
        ks = str(k).upper()
        if "LYRICS" in ks or ks.startswith("USLT") or ks.startswith("SYLT"):
            s = v.text if hasattr(v, "text") else v
            if isinstance(s, (list, tuple)):
                s = "\n".join(str(x) for x in s)
            s = str(s)
            if len(s) > len(best):
                best = s
    return best.strip()


def get_cover(path):
    """返回 (bytes, mime) 或 None。"""
    try:
        mf = mutagen.File(path)
    except Exception:
        return None
    # FLAC
    pics = getattr(mf, "pictures", None)
    if pics:
        return pics[0].data, (pics[0].mime or "image/jpeg")
    tags = getattr(mf, "tags", None) or {}
    for k, v in tags.items():
        ks = str(k).upper()
        if ks.startswith("APIC"):
            data = getattr(v, "data", None)
            if data:
                return data, (getattr(v, "mime", None) or "image/jpeg")
        # APEv2: 'Cover Art (Front)' -> b'filename\x00<image bytes>'
        if ks.startswith("COVER ART"):
            try:
                raw = v.value if hasattr(v, "value") else bytes(v)
                data = raw.split(b"\x00", 1)[1] if b"\x00" in raw else raw
                if data:
                    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
                    return data, mime
            except Exception:
                pass
        if ks == "COVR" or "COVR" in ks:
            try:
                item = v[0] if isinstance(v, (list, tuple)) else v
                data = bytes(item)
                mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
                return data, mime
            except Exception:
                pass
    return None


def write_jpg(data, mime, dst):
    """dst 一律 .jpg；非 JPEG 先转码。"""
    if mime == "image/jpeg" or data[:3] == b"\xff\xd8\xff":
        with open(dst, "wb") as f:
            f.write(data)
        return True
    tmp_in = os.path.join(TMP, "in.bin")
    with open(tmp_in, "wb") as f:
        f.write(data)
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp_in, dst], check=True)
        return True
    except Exception:
        return False


def main():
    files = []
    for dp, dn, fn in os.walk(ROOT):
        for f in fn:
            if os.path.splitext(f)[1].lower() in AUDIO:
                files.append(os.path.join(dp, f))
    print(f"音频 {len(files)} 个", flush=True)

    n_lrc = n_skip = n_nolyr = 0
    covers_done = set()
    n_cov = n_covfail = n_covexist = 0

    for i, p in enumerate(files):
        # 歌词
        stem = os.path.splitext(p)[0]
        lrc = stem + ".lrc"
        if not os.path.exists(lrc):
            lyr = get_lyrics(p)
            if lyr:
                with open(lrc, "w", encoding="utf-8") as f:
                    f.write(lyr + "\n")
                n_lrc += 1
            else:
                n_nolyr += 1
        else:
            n_skip += 1

        # 封面（每专辑目录一次）
        d = os.path.dirname(p)
        if d in covers_done:
            continue
        covers_done.add(d)
        cpath = os.path.join(d, "cover.jpg")
        if os.path.exists(cpath):
            n_covexist += 1
            continue
        cov = get_cover(p)
        if not cov:
            # 同目录其他文件再试两个
            for f2 in os.listdir(d):
                if os.path.splitext(f2)[1].lower() in AUDIO:
                    cov = get_cover(os.path.join(d, f2))
                    if cov:
                        break
        if not cov:
            n_covfail += 1
            print("  无封面:", os.path.relpath(d, ROOT)[:70], flush=True)
            continue
        if write_jpg(cov[0], cov[1], cpath):
            # folder.jpg 与 cover.jpg 同内容，兼容两种播放器惯例
            with open(cpath, "rb") as f:
                jb = f.read()
            with open(os.path.join(d, "folder.jpg"), "wb") as f:
                f.write(jb)
            n_cov += 1
        else:
            n_covfail += 1
            print("  封面转码失败:", os.path.relpath(d, ROOT)[:70], flush=True)

        if (i + 1) % 300 == 0:
            print(f"  进度 {i + 1}/{len(files)}", flush=True)

    print(f"\n歌词: 新写 {n_lrc}，已存在 {n_skip}，无歌词(纯音乐等) {n_nolyr}", flush=True)
    print(f"封面: 新写 {n_cov} 个专辑目录（cover.jpg+folder.jpg），已存在 {n_covexist}，缺失 {n_covfail}", flush=True)


if __name__ == "__main__":
    main()
