"""musictidy 打包入口（PyInstaller）。

双击启动图形界面；`musictidy.exe --smoke` 跑无界面自检（打包后验证用）。
"""
import os
import sys


def smoke() -> int:
    """无 GUI 自检：写结果到 smoke_result.txt，返回退出码。"""
    out = []
    try:
        import musictidy
        from musictidy import pipeline, core
        out.append(f"version: {musictidy.__version__}")
        out.append(f"ffmpeg: {core.FFMPEG}")
        out.append(f"fpcalc: {core.FPCALC}")
        out.append(f"cache: {core.CACHE_DIR}")
        assert pipeline.sanitize('a/b:c') == "a b c"
        out.append("sanitize: OK")
        out.append("smoke: PASS")
        code = 0
    except Exception:
        import traceback
        out.append("smoke: FAIL")
        out.append(traceback.format_exc())
        code = 1
    with open("smoke_result.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return code


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        sys.exit(smoke())
    try:
        from musictidy.gui import main as gui_main
    except ImportError as e:
        import tkinter.messagebox as mb
        mb.showerror("musictidy", f"启动失败：{e}")
        sys.exit(1)
    gui_main()


if __name__ == "__main__":
    main()
