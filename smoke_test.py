"""冒烟测试：导入、配置发现、GUI 构建（不进主循环）、流水线小样本。"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ok = True

def check(name, fn):
    global ok
    try:
        fn()
        print(f"[PASS] {name}")
    except Exception:
        ok = False
        print(f"[FAIL] {name}")
        traceback.print_exc()

def t_imports():
    import musictidy
    from musictidy import pipeline, core, writer, quality, exporter
    assert musictidy.__version__
    print("       version:", musictidy.__version__)
    print("       ffmpeg :", core.FFMPEG)
    print("       fpcalc :", core.FPCALC)
    print("       cache  :", core.CACHE_DIR)

def t_sanitize():
    from musictidy.pipeline import sanitize
    assert sanitize('a/b:c*d?e"f<g>h|i') == "a b c d e f g h i"
    assert sanitize("") == "未知"

def t_plan():
    import tempfile
    from musictidy import pipeline
    tmp = tempfile.mkdtemp()
    srcs = []
    for i in (1, 2, 3):
        p = os.path.join(tmp, f"{i}.flac")
        open(p, "wb").close()
        srcs.append(p)
    recs = [
        {"path": srcs[0], "ext": ".flac", "m_title": "晴天", "m_artist": "周杰伦",
         "album": "叶惠美", "year": "2003", "track": "3", "duration": 269},
        {"path": srcs[1], "ext": ".flac", "m_title": "晴天", "m_artist": "周杰伦",
         "album": "叶惠美", "year": "2003", "track": "3", "duration": 270},  # 同录音冗余
        {"path": srcs[2], "ext": ".flac", "m_title": "晴天 (Live)", "m_artist": "周杰伦",
         "album": "叶惠美", "year": "2003", "track": "", "duration": 300},  # 无曲序
    ]
    plan = pipeline.build_plan(recs, os.path.join(tmp, "lib"))
    moves = [p for p in plan if p["action"] == "move"]
    dups = [p for p in plan if p["action"] == "dup"]
    assert len(moves) == 2 and len(dups) == 1, (len(moves), len(dups))
    names = sorted(os.path.basename(p["dst"]) for p in moves)
    assert names[0] == "03 晴天.flac", names
    assert names[1] == "晴天 (Live).flac", names
    print("       plan:", [(p["action"], os.path.basename(p["dst"])) for p in plan])

def t_gui():
    import musictidy.gui as g
    app = g.App()
    app.update_idletasks()
    app.destroy()
    print("       GUI 构建/销毁 OK")

check("imports & config", t_imports)
check("sanitize", t_sanitize)
check("build_plan", t_plan)
check("GUI construct", t_gui)
print("\nALL PASS" if ok else "\nHAS FAILURES")
sys.exit(0 if ok else 1)
