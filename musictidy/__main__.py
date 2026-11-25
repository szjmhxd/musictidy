"""musictidy CLI 入口。

    python -m musictidy gui                      打开图形界面
    python -m musictidy scan  --root D:/Music    扫描
    python -m musictidy match --root D:/Music --out name.json
    python -m musictidy write --from name.json [--dry-run]
    python -m musictidy organize --from name.json --root D:/Music [--apply]
    python -m musictidy export --root D:/Music   导出歌词/封面外置文件

底层识别/指纹/对比子命令与旧版 mtcli 一致：
    python -m musictidy match --pack 张韶涵 --out name.json
"""
from __future__ import annotations

import argparse
import sys


def _launch_gui():
    try:
        __import__("musictidy.gui", fromlist=["main"]).main()
    except ImportError as e:
        sys.exit(f"无法启动图形界面：{e}\n可改用命令行：python -m musictidy --help")


def main():
    ap = argparse.ArgumentParser(prog="musictidy", description="音乐库整理工具")
    sub = ap.add_subparsers(dest="cmd")

    g = sub.add_parser("gui", help="打开图形界面")
    g.set_defaults(func=lambda a: _launch_gui())

    s = sub.add_parser("scan", help="扫描音频文件")
    s.add_argument("--root", required=True)
    s.add_argument("--out", default="scan.json")
    s.set_defaults(func=cmd_scan)

    o = sub.add_parser("organize", help="归档：歌手/年份 - 专辑/NN 曲名")
    o.add_argument("--from", dest="src_json", required=True, help="match 输出的 json")
    o.add_argument("--root", required=True, help="归档根目录")
    o.add_argument("--apply", action="store_true", help="实际移动（默认只出计划）")
    o.set_defaults(func=cmd_organize)

    e = sub.add_parser("export", help="导出外置歌词 (.lrc) 与封面 (cover.jpg/folder.jpg)")
    e.add_argument("--root", required=True)
    e.set_defaults(func=cmd_export)

    from . import core
    for name, help_, func in (
        ("match", "名称路线识别", core.cmd_match),
        ("fp", "指纹路线", core.cmd_fp),
        ("compare", "对比名称/指纹结果", core.cmd_compare),
        ("write", "写入标签", core._cmd_write_proxy),
    ):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=func)

    # match 需要自己的参数
    # （core.main 里有完整定义，这里复用 core 的解析做兼容入口）

    args, unknown = ap.parse_known_args()
    if not getattr(args, "cmd", None):
        # 未指定子命令：有 Windows GUI 时开界面，否则打印帮助
        if sys.platform.startswith("win"):
            _launch_gui()
            return
        ap.print_help()
        return
    if args.cmd in ("match", "fp", "compare", "write"):
        # 交给 core 的完整解析器（含 --pack/--workers 等参数）
        sys.argv = ["musictidy"] + [args.cmd] + unknown
        core.main()
        return
    args.func(args)


def cmd_scan(args):
    from . import pipeline
    files = pipeline.scan_files(args.root)
    pipeline.save_json(files, args.out)
    print(f"扫描 {len(files)} 个音频文件 -> {args.out}")


def cmd_organize(args):
    import json
    import os
    from . import pipeline
    with open(args.src_json, encoding="utf-8") as f:
        records = json.load(f)
    plan = pipeline.build_plan(records, args.root)
    dup = sum(1 for p in plan if p["action"] == "dup")
    print(f"归档计划：移动 {len(plan) - dup}，冗余 {dup}")
    for p in plan[:30]:
        print(f"  [{p['action']}] {os.path.basename(p['src'])[:32]}"
              f" -> {os.path.relpath(p['dst'], args.root)}")
    if len(plan) > 30:
        print(f"  … 共 {len(plan)} 条")
    out = os.path.splitext(args.src_json)[0] + "_plan.csv"
    pipeline.save_csv(plan, out)
    print(f"计划明细: {out}")
    if not args.apply:
        print("（未移动。确认无误后加 --apply 执行）")
        return
    results = pipeline.execute_plan(plan)
    log = os.path.splitext(args.src_json)[0] + "_move_log.csv"
    pipeline.save_csv(results, log)
    print(f"移动日志: {log}")


def cmd_export(args):
    from . import exporter
    exporter.ROOT = args.root
    exporter.main()


if __name__ == "__main__":
    main()
