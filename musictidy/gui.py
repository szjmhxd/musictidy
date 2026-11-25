"""musictidy.gui — Tkinter 图形界面。

五个步骤对应五个操作区：
  1. 选择文件夹 → 扫描     2. 识别（联网匹配元数据）
  3. 写标签（含封面歌词）   4. 归档（按 歌手\\年份 - 专辑\\NN 曲名 收拢）
  5. 导出歌词/封面外置文件

长任务跑在后台线程，进度回传到日志区，随时可看结果。
"""
from __future__ import annotations

import os
import queue
import threading
import traceback
import webbrowser
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "tkinter 不可用。官方 python.org 安装包自带 tkinter；\n"
        "  - Windows: 重新安装 Python 时勾选 tcl/tk 组件\n"
        "  - Debian/Ubuntu: sudo apt install python3-tk\n"
        "  - 或改用 CLI: python -m musictidy --help"
    ) from e

from . import pipeline
from . import exporter as _exporter


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("musictidy — 音乐库整理")
        self.geometry("980x680")
        self.minsize(760, 520)

        self.files: list[dict] = []
        self.records: list[dict] = []
        self.plan: list[dict] = []
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None

        self._build()
        self.after(100, self._poll)

    # ------------------------------------------------------------ UI
    def _build(self):
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="音乐库目录:").pack(side="left")
        self.var_root = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_root).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="浏览…", command=self._pick_dir).pack(side="left")
        self.btn_scan = ttk.Button(top, text="1. 扫描", command=self.do_scan)
        self.btn_scan.pack(side="left", padx=6)

        # ---- 步骤按钮行 ----
        steps = ttk.Frame(self)
        steps.pack(fill="x", **pad)
        self.btn_match = ttk.Button(steps, text="2. 识别元数据",
                                    command=self.do_match, state="disabled")
        self.btn_match.pack(side="left", padx=4)
        self.btn_write = ttk.Button(steps, text="3. 写入标签",
                                    command=self.do_write, state="disabled")
        self.btn_write.pack(side="left", padx=4)
        self.btn_write_dry = ttk.Button(steps, text="3b. 写入预览",
                                        command=self.do_write_dry, state="disabled")
        self.btn_write_dry.pack(side="left", padx=4)
        self.btn_plan = ttk.Button(steps, text="4. 归档计划",
                                   command=self.do_plan, state="disabled")
        self.btn_plan.pack(side="left", padx=4)
        self.btn_apply = ttk.Button(steps, text="4b. 执行归档",
                                    command=self.do_apply, state="disabled")
        self.btn_apply.pack(side="left", padx=4)
        self.btn_export = ttk.Button(steps, text="5. 导出歌词/封面",
                                     command=self.do_export)
        self.btn_export.pack(side="left", padx=4)

        # ---- 选项 ----
        opts = ttk.Frame(self)
        opts.pack(fill="x", **pad)
        self.var_cover = tk.BooleanVar(value=True)
        self.var_lyric = tk.BooleanVar(value=True)
        self.var_workers = tk.IntVar(value=8)
        ttk.Checkbutton(opts, text="获取封面", variable=self.var_cover).pack(side="left")
        ttk.Checkbutton(opts, text="获取歌词", variable=self.var_lyric).pack(side="left")
        ttk.Label(opts, text="线程数:").pack(side="left", padx=(12, 2))
        ttk.Spinbox(opts, from_=2, to=32, width=4,
                    textvariable=self.var_workers).pack(side="left")

        self.var_status = tk.StringVar(value="就绪。请选择音乐库目录。")
        ttk.Label(self, textvariable=self.var_status, relief="sunken",
                  anchor="w").pack(fill="x", padx=8, pady=(2, 0))

        # ---- 结果表 + 日志 ----
        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, **pad)

        table_frame = ttk.Frame(pane)
        cols = ("file", "title", "artist", "album", "year", "track", "score")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings",
                                 selectmode="extended")
        heads = {"file": "文件", "title": "曲名", "artist": "歌手",
                 "album": "专辑", "year": "年份", "track": "曲序", "score": "置信"}
        widths = {"file": 260, "title": 150, "artist": 110, "album": 170,
                  "year": 55, "track": 50, "score": 55}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor="w")
        sb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pane.add(table_frame, weight=3)

        log_frame = ttk.Frame(pane)
        self.txt = tk.Text(log_frame, height=10, wrap="word",
                           state="disabled", font=("Consolas", 9))
        sb2 = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb2.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")
        pane.add(log_frame, weight=1)

    # ------------------------------------------------------------ helpers
    def _pick_dir(self):
        d = filedialog.askdirectory(title="选择音乐库目录")
        if d:
            self.var_root.set(d)

    def _log(self, msg: str):
        self.q.put(("log", msg))

    def _set_status(self, msg: str):
        self.q.put(("status", msg))

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.txt.configure(state="normal")
                    self.txt.insert("end", payload + "\n")
                    self.txt.see("end")
                    self.txt.configure(state="disabled")
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "rows":
                    self._fill_table(payload)
                elif kind == "done":
                    self._set_busy(False)
                    self.var_status.set(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _fill_table(self, rows: list[dict]):
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            name = os.path.basename(r.get("path", ""))
            self.tree.insert("", "end", values=(
                name,
                r.get("m_title") or r.get("title_guess") or "",
                r.get("m_artist") or r.get("artist_guess") or "",
                r.get("album") or r.get("album_guess") or "",
                r.get("year") or "",
                r.get("track") or "",
                r.get("score") or "",
            ))

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for b in (self.btn_scan, self.btn_match, self.btn_write, self.btn_write_dry,
                  self.btn_plan, self.btn_apply, self.btn_export):
            b.configure(state=state)
        if not busy:
            # 按数据可用性细化
            if not self.files:
                for b in (self.btn_match, self.btn_write, self.btn_write_dry,
                          self.btn_plan, self.btn_apply):
                    b.configure(state="disabled")
            elif not self.records:
                for b in (self.btn_write, self.btn_write_dry,
                          self.btn_plan, self.btn_apply):
                    b.configure(state="disabled")
            elif not self.plan:
                self.btn_apply.configure(state="disabled")

    def _run(self, fn):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("忙", "有任务正在运行，请稍候。")
            return
        self._set_busy(True)
        self.worker = threading.Thread(target=self._wrap, args=(fn,), daemon=True)
        self.worker.start()

    def _wrap(self, fn):
        try:
            fn()
        except Exception:
            self._log("出错：\n" + traceback.format_exc())
        finally:
            self.q.put(("done", "就绪。"))

    # ------------------------------------------------------------ actions
    def do_scan(self):
        root = self.var_root.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning("提示", "请先选择有效的音乐库目录。")
            return

        def job():
            self._set_status("扫描中…")
            self.files = pipeline.scan_files(root)
            self._log(f"扫描完成：{len(self.files)} 个音频文件")
            self.q.put(("rows", self.files))
            self._set_status(f"扫描到 {len(self.files)} 个音频文件。"
                             "下一步：识别元数据。")
        self._run(job)

    def do_match(self):
        if not self.files:
            return

        def job():
            self._set_status("识别中（联网查询，可能需要几分钟）…")
            self.records = pipeline.match_files(
                self.files, workers=self.var_workers.get(), log=self._log)
            self.q.put(("rows", self.records))
            hit = sum(1 for r in self.records if r.get("track"))
            self._set_status(f"识别完成：{hit}/{len(self.records)} 命中。"
                             "下一步：写入标签。")
        self._run(job)

    def do_write_dry(self):
        self._write(dry=True)

    def do_write(self):
        if not messagebox.askyesno(
                "确认写入",
                "将把识别结果写入音频文件标签（含封面/歌词）。\n"
                "建议先跑一次「写入预览」核对。继续？"):
            return
        self._write(dry=False)

    def _write(self, dry: bool):
        if not self.records:
            return

        def job():
            self._set_status("写入中…" if not dry else "预览中（不写盘）…")
            results = pipeline.write_records(
                self.records, workers=self.var_workers.get(), dry=dry,
                cover=self.var_cover.get(), lyric=self.var_lyric.get(),
                log=self._log)
            if dry:
                for r in results[:15]:
                    p = r.get("preview") or {}
                    self._log(
                        f"  {os.path.basename(r['path'])[:32]:34} "
                        f"{p.get('artist', '')[:8]:9}/{p.get('album', '')[:14]:15} "
                        f"{p.get('year', '')} #{p.get('track', '')} "
                        f"封面={'Y' if p.get('cover') else '-'} "
                        f"歌词={p.get('lyric_len', 0)}")
                self._set_status("预览完成（未写盘）。核对无误后点「写入标签」。")
            else:
                self._set_status("写入完成。下一步：归档计划。")
        self._run(job)

    def do_plan(self):
        if not self.records:
            return
        root = self.var_root.get().strip()

        def job():
            self.plan = pipeline.build_plan(self.records, root)
            dup = sum(1 for p in self.plan if p["action"] == "dup")
            self._log(f"归档计划：移动 {len(self.plan) - dup}，冗余 {dup}")
            for p in self.plan[:20]:
                self._log(f"  [{p['action']}] {os.path.basename(p['src'])[:30]}"
                          f" -> {os.path.relpath(p['dst'], root)}")
            if len(self.plan) > 20:
                self._log(f"  … 共 {len(self.plan)} 条")
            self.q.put(("rows", [{"path": p["src"], "m_title": os.path.basename(p["dst"]),
                                  "m_artist": p["action"], "score": p["note"]}
                                 for p in self.plan]))
            self._set_status(f"计划就绪：{len(self.plan)} 条。确认后点「执行归档」。")
        self._run(job)

    def do_apply(self):
        if not self.plan:
            return
        n = sum(1 for p in self.plan if p["action"] == "move")
        if not messagebox.askyesno(
                "确认归档",
                f"将移动 {n} 个文件到归档结构（同卷移动，速度快）。\n"
                "目标已存在的文件会跳过。继续？"):
            return

        def job():
            self._set_status("归档中…")
            results = pipeline.execute_plan(self.plan, log=self._log)
            pipeline.save_csv(results, os.path.join(root_or_tmp(self.var_root.get()),
                                                    "musictidy_move_log.csv"))
            self._set_status("归档完成。日志: musictidy_move_log.csv")
        self._run(job)

    def do_export(self):
        root = self.var_root.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning("提示", "请先选择音乐库目录。")
            return

        def job():
            self._set_status("导出歌词/封面中…")
            old_root = _exporter.ROOT
            _exporter.ROOT = root
            try:
                _exporter.main()
            finally:
                _exporter.ROOT = old_root
            self._set_status("歌词/封面导出完成（同名 .lrc + cover.jpg/folder.jpg）。")
        self._run(job)


def root_or_tmp(root: str) -> str:
    return root if root and os.path.isdir(root) else os.getcwd()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
