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
    if os.name == "nt":
        assert "creationflags" in core.NO_WINDOW, "缺少 CREATE_NO_WINDOW 静默标志"
        print("       subprocess 静默标志 OK")

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

def t_gui_import_stdout_none():
    """模拟 --windowed 打包：sys.stdout 为 None 时导入 gui 不能崩。"""
    import builtins
    real_stdout, real_stderr = sys.stdout, sys.stderr
    # 先按打包入口的方式修 stdio，再导入
    sys.stdout = sys.stderr = None
    try:
        import importlib
        import musictidy.exporter  # noqa: F401  (含 reconfigure 调用)
        import musictidy.quality   # noqa: F401
        importlib.reload(sys.modules["musictidy.exporter"])
        importlib.reload(sys.modules["musictidy.quality"])
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
    print("       stdout=None 导入安全 OK")

def t_parse_flat():
    """扁平目录 + 混用斜杠：歌手不能被解析成文件名/容器目录名。"""
    from musictidy.core import parse_path, _normalize_positions, locate_track
    i = parse_path("E:/songs/01 你把我灌醉.flac")
    assert i["artist"] == "", i["artist"]
    assert i["title"] == "你把我灌醉" and i["track_hint"] == "01", i
    i2 = parse_path("E:\\Music\\x\\02 晴天.mp3")
    assert i2["artist"] == "", i2["artist"]  # 单字符目录名不是歌手
    i3 = parse_path("M:\\周杰伦\\叶惠美\\03 晴天.flac")
    assert i3["artist"] == "周杰伦" and i3["album_guess"] == "叶惠美", i3
    print("       parse_path 扁平/M: 结构 OK")

def t_positions():
    """网易云 position 坏数据归一：排列保留，重复/缺号退回列表序。"""
    from musictidy.core import _normalize_positions
    a = [{"position": 3}, {"position": 1}, {"position": 2}]   # 排列 → 保留
    _normalize_positions(a)
    assert [s["position"] for s in a] == [3, 1, 2], a
    b = [{"position": 3}, {"position": 3}, {"position": 1}]   # 坏 → 列表序
    _normalize_positions(b)
    assert [s["position"] for s in b] == [1, 2, 3], b
    print("       _normalize_positions OK")

def t_locate_best():
    """曲名定位取最高分：长共享后缀不能让 '偶尔 (Piano)' 抢走 '是否 (Piano)'。"""
    from musictidy.core import locate_track
    alb = {"songs": [
        {"position": 16, "no": 4, "title": "偶尔 (Piano Session Live)",
         "artists": ["G.E.M.邓紫棋"], "duration": 200000, "cd": "", "song_id": 1},
        {"position": 20, "no": 8, "title": "是否 (Piano Session Live)",
         "artists": ["G.E.M.邓紫棋"], "duration": 210000, "cd": "", "song_id": 2},
    ]}
    out = {}
    r = locate_track(alb, {"track_hint": "9"}, "是否 (Piano Session Live)", 210, out)
    assert r == "20", (r, out)
    out2 = {}
    r2 = locate_track(alb, {}, "偶尔", 200, out2)      # 包含匹配
    assert r2 == "16", (r2, out2)
    print("       locate_track 最佳匹配 OK")

check("imports & config", t_imports)
check("sanitize", t_sanitize)
check("build_plan", t_plan)
check("parse flat paths", t_parse_flat)
check("netease positions", t_positions)
check("locate best match", t_locate_best)
check("GUI construct", t_gui)
check("import with stdout=None", t_gui_import_stdout_none)
print("\nALL PASS" if ok else "\nHAS FAILURES")
sys.exit(0 if ok else 1)
