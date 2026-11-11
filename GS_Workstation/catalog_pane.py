# Copyright © 2026 Swiftchen
# GS 双市场实时看盘终端 | 作者：Swiftchen
"""搜索栏和分池侧边栏。复用工作台的加载和保存入口。"""
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import instrument_directory
import pools
import watch

BG, PANEL, FG, ACCENT = "#0b1220", "#111d30", "#edf4ff", "#60a5fa"
MARKETS = {"CN": "A 股 · 量价代理", "CN_L2": "A 股 · 授权 L2", "US": "美股 · 量价代理"}


class CatalogPane:
    def mount_search(self, top):
        self.catalog = instrument_directory.Catalog()
        self.search_text = tk.StringVar()
        self.search_job = None
        self.search_rows = []
        self.search_selected = None
        self.search_menu = None
        ttk.Label(top, text="标的搜索").pack(side="left", padx=(0, 8))
        self.search_entry = ttk.Entry(top, width=31, textvariable=self.search_text)
        self.search_entry.pack(side="left")
        self.search_text.trace_add("write", self.search_changed)
        self.search_entry.bind("<Down>", self.search_down)
        self.search_entry.bind("<Return>", self.search_enter)
        self.search_entry.bind("<Escape>", lambda _: self.hide_search())
        self.root.bind("<ButtonPress-1>", self.search_outside, add="+")
        ttk.Button(top, text="搜索／打开", command=lambda: self.search_enter(None)).pack(side="left", padx=6)
        ttk.Button(top, text="添加到自选", command=self.add_search).pack(side="left")
        self.directory_note = tk.StringVar(value=f"目录 {len(self.catalog.rows):,} 条")
        tk.Label(top, textvariable=self.directory_note, fg=ACCENT, bg=BG).pack(side="left", padx=12)
        ttk.Button(top, text="更新目录", command=self.refresh_directory).pack(side="right")

    def search_outside(self,event):
        if self.search_menu and event.widget is not self.search_entry:
            self.hide_search()

    def search_changed(self, *_):
        self.search_selected = None
        if self.search_job:
            self.root.after_cancel(self.search_job)
        self.search_job = self.root.after(170, self.show_search)

    def query_results(self):
        text = self.search_text.get().strip()
        hits = self.catalog.search(text)
        if not hits:
            if len(text) == 6 and text.isascii() and text.isdigit():
                hits = [["CN", text, "手工代码", "A股", ""]]
            elif text.isascii() and text.upper() == text and text.replace("-", "").replace(".", "").isalnum() and 1 <= len(text) <= 10:
                hits = [["US", text, "手工代码", "美股", ""]]
        return hits

    def show_search(self):
        self.search_job = None
        self.hide_search()
        self.search_rows = self.query_results()
        if not self.search_rows:
            return
        menu = self.search_menu = tk.Toplevel(self.root)
        menu.overrideredirect(True)
        menu.transient(self.root)
        menu.geometry(f"690x{min(len(self.search_rows),9)*25 + 32}+"
                      f"{self.search_entry.winfo_rootx()}+{self.search_entry.winfo_rooty()+self.search_entry.winfo_height()}")
        tree = self.result_table = ttk.Treeview(menu, columns=("code", "name", "market"), show="headings",
                                               selectmode="browse", height=min(len(self.search_rows),9))
        for col, label, size in [("code","代码",85),("name","标的名称",410),("market","市场",180)]:
            tree.heading(col,text=label)
            tree.column(col,width=size)
        scroll=ttk.Scrollbar(menu,command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right",fill="y")
        tree.pack(fill="both",expand=True)
        for index, row in enumerate(self.search_rows):
            group, code, title, board, _ = row
            tree.insert("", "end", iid=str(index), values=(code,title,("A股 · " if group=="CN" else "美股 · ")+board))
        tree.bind("<ButtonRelease-1>", self.search_click)
        tree.bind("<Return>", self.search_enter)
        tree.bind("<Escape>", lambda _: self.hide_search())

    def hide_search(self):
        if self.search_menu and self.search_menu.winfo_exists():
            self.search_menu.destroy()
        self.search_menu = None

    def search_down(self, _):
        if not self.search_menu:
            self.show_search()
        if self.search_menu:
            self.result_table.focus_set()
            self.result_table.focus("0")
            self.result_table.selection_set("0")
        return "break"

    def search_click(self, event):
        iid = self.result_table.identify_row(event.y)
        if iid:
            self.open_result(self.search_rows[int(iid)])

    def search_enter(self, _):
        hits = self.query_results()
        if hits:
            choice = self.result_table.selection() if self.search_menu else ()
            self.open_result(hits[int(choice[0])] if choice and int(choice[0]) < len(hits) else hits[0])
        else:
            self.hide_search()
            self.directory_note.set("未找到。可试代码、简称或英文片段。")
        return "break"

    def open_result(self, row):
        self.hide_search()
        self.search_selected = row
        self.open_symbol(row[0], row[1], row[2])

    def open_symbol(self, group, code, title=""):
        self.hide_search()
        self.mode.set(MARKETS[group])
        if group != "US" and self.period.current() == 2:
            self.period.current(0)
        self.symbol.set(f"{code}  {title}".strip())
        self.load(refresh=group != "CN_L2" and not (self.cache_root / f"{group}_{code}.json").is_file())

    def refresh_directory(self):
        self.directory_note.set("正在更新交易所目录…")
        def refresh():
            return instrument_directory.build(instrument_directory.download())
        future = self.pool.submit(refresh)
        def poll():
            if not future.done():
                self.root.after(200, poll)
                return
            try:
                count = future.result()
                self.catalog = instrument_directory.Catalog()
                self.directory_note.set(f"目录已更新 · {count:,} 条")
            except (ValueError, OSError, KeyError) as exc:
                self.directory_note.set(f"目录更新失败，继续用本地索引：{exc}")
        self.root.after(200, poll)

    def mount_sidebar(self, parent):
        pane = tk.Frame(parent, bg=PANEL, padx=9, pady=8, width=230)
        pane.pack(side="left",fill="y",padx=(0,8))
        pane.pack_propagate(False)
        tk.Label(pane,text="自选股池",bg=PANEL,fg=FG,font=("Microsoft YaHei UI",11,"bold")).pack(anchor="w")
        self.active_pool = tk.StringVar(value=self.watch_state["pools"][0])
        self.pool_picker = ttk.Combobox(pane,textvariable=self.active_pool,state="readonly")
        self.pool_picker.pack(fill="x",pady=4)
        self.pool_picker.bind("<<ComboboxSelected>>",lambda _:self.refresh_sidebar())
        top=tk.Frame(pane,bg=PANEL)
        top.pack(fill="x")
        for title, action in [("新建",self.new_pool),("改名",self.rename_pool),("清空",self.clear_pool)]:
            ttk.Button(top,text=title,command=action).pack(side="left",fill="x",expand=True)
        box=tk.Frame(pane,bg=PANEL)
        box.pack(fill="both",expand=True,pady=4)
        self.side = ttk.Treeview(box,columns=("paper","quote"),show="tree headings",selectmode="browse")
        self.side.heading("#0",text="标的")
        self.side.heading("paper",text="模拟")
        self.side.heading("quote",text="行情")
        self.side.column("quote",width=58,minwidth=48,stretch=False,anchor="e")
        self.side.column("#0",width=138,minwidth=95)
        self.side.column("paper",width=46,minwidth=42,stretch=False,anchor="center")
        scroll=ttk.Scrollbar(box,command=self.side.yview)
        self.side.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right",fill="y")
        self.side.pack(fill="both",expand=True)
        self.side.bind("<Double-1>",self.side_double)
        self.side.bind("<<TreeviewSelect>>",self.sync_paper_check)
        self.side.bind("<Return>",lambda _:self.open_side())
        self.side.bind("<ButtonPress-1>",self.side_start,add="+")
        self.side.bind("<ButtonRelease-1>",self.side_drop,add="+")
        self.side.bind("<B1-Motion>",self.side_move,add="+")
        self.drag_source = None
        self.paper_check = tk.BooleanVar(value=False)
        self.paper_toggle = ttk.Checkbutton(pane,text="所选标的参与实时模拟",variable=self.paper_check,
                                            command=self.set_side_participation)
        self.paper_toggle.pack(anchor="w",pady=(0,4))
        bottom=tk.Frame(pane,bg=PANEL)
        bottom.pack(fill="x")
        for title, action in [("打开",self.open_side),("移除",self.remove_side),("暂停／恢复",self.toggle_side)]:
            ttk.Button(bottom,text=title,command=action).pack(side="left",fill="x",expand=True)
        io=tk.Frame(pane,bg=PANEL)
        io.pack(fill="x",pady=(4,0))
        ttk.Button(io,text="导入 JSON",command=self.import_watch).pack(side="left",fill="x",expand=True)
        ttk.Button(io,text="导出 JSON",command=self.export_watch).pack(side="left",fill="x",expand=True)
        self.refresh_sidebar()

    def refresh_sidebar(self):
        if not hasattr(self,"side"):
            return
        self.pool_picker["values"]=self.watch_state["pools"]
        current = self.side.selection()
        self.side.delete(*self.side.get_children())
        for key, label in (("CN","A 股"),("US","美股")):
            self.side.insert("", "end", iid=key, text=label, open=True)
        for i, entry in enumerate(self.watch_state["stocks"]):
            if entry["pool"] != self.active_pool.get():
                continue
            code, title = entry["code"], entry["name"]
            suffix = " · 暂停" if not entry["enabled"] else " · L2" if entry["group"]=="CN_L2" else ""
            self.side.insert(pools.subgroup(entry["group"]), "end", iid=f"r{i}",
                             text=f"{code}  {title}{suffix}", values=("☑" if entry["paper_enabled"] else "☐",self.quote_text(entry)))
        if current and self.side.exists(current[0]):
            self.side.selection_set(current[0])
        self.sync_paper_check()
        self.populate_watch()

    def quote_text(self,entry):
        quote=self.pool_quotes.get(watch.key(entry["group"],entry["code"]))
        return f"{quote['price']:.2f}" if quote is not None else "—"

    def refresh_quote_labels(self):
        for parent in self.side.get_children():
            for iid in self.side.get_children(parent):
                index=int(iid[1:])
                self.side.set(iid,"quote",self.quote_text(self.watch_state["stocks"][index]))

    def add_search(self):
        row=self.search_selected
        if row is None and self.search_text.get().strip():
            hits=self.query_results()
            row=hits[0] if hits else None
        if row is None:
            group=next(key for key,value in MARKETS.items() if value==self.mode.get())
            raw=self.symbol.get().split(maxsplit=1)
            if not raw:
                return
            row=[group,raw[0],raw[1] if len(raw)>1 else "", "", ""]
        self.hide_search()
        group, code, title, *_=row
        before=len(self.watch_state["stocks"])
        self.watch_state=pools.normalize({**self.watch_state,"stocks":[*self.watch_state["stocks"],
            {"pool":self.active_pool.get(),"group":group,"code":code,"name":title,"enabled":True}]})
        self.persist_watch()
        self.refresh_sidebar()
        self.directory_note.set(f"{code} {'已加入' if len(self.watch_state['stocks'])>before else '已在'} {self.active_pool.get()}")

    def new_pool(self):
        text=simpledialog.askstring("新建自选池","名称",parent=self.root)
        if text is None:
            return
        try:
            label=pools.name(text)
            if label in self.watch_state["pools"]:
                raise ValueError("已存在同名池")
            self.watch_state["pools"].append(label)
            self.active_pool.set(label)
            self.persist_watch()
            self.refresh_sidebar()
        except ValueError as exc:
            messagebox.showerror("自选池",str(exc),parent=self.root)

    def rename_pool(self):
        before=self.active_pool.get()
        text=simpledialog.askstring("重命名自选池","名称",initialvalue=before,parent=self.root)
        if text is None:
            return
        try:
            after=pools.name(text)
            if after==before:
                return
            if after in self.watch_state["pools"]:
                raise ValueError("已存在同名池")
            self.watch_state["pools"]=[after if p==before else p for p in self.watch_state["pools"]]
            for row in self.watch_state["stocks"]:
                if row["pool"]==before:
                    row["pool"]=after
            self.active_pool.set(after)
            self.persist_watch()
            self.refresh_sidebar()
        except ValueError as exc:
            messagebox.showerror("自选池",str(exc),parent=self.root)

    def clear_pool(self):
        name=self.active_pool.get()
        if messagebox.askyesno("清空当前自选池", f"移除「{name}」中的全部标的？", parent=self.root):
            self.watch_state["stocks"]=[r for r in self.watch_state["stocks"] if r["pool"]!=name]
            self.persist_watch()
            self.refresh_sidebar()

    def side_index(self):
        current=self.side.selection()
        return int(current[0][1:]) if current and current[0].startswith("r") else None

    def side_double(self,event):
        if self.side.identify_column(event.x)!="#1":
            self.open_side()

    def sync_paper_check(self,_=None):
        index=self.side_index()
        self.paper_toggle.state(["disabled" if index is None else "!disabled"])
        self.paper_check.set(index is not None and self.watch_state["stocks"][index]["paper_enabled"])

    def set_side_participation(self):
        index=self.side_index()
        if index is None:
            return
        row=self.watch_state["stocks"][index]
        pools.set_participation(self.watch_state,row["group"],row["code"],self.paper_check.get())
        self.persist_watch()
        self.directory_note.set(f"{row['code']} · {'参与后台监控' if self.paper_check.get() else '只看图'}")

    def open_side(self):
        index=self.side_index()
        if index is not None:
            row=self.watch_state["stocks"][index]
            self.open_symbol(row["group"],row["code"],row["name"])

    def remove_side(self):
        index=self.side_index()
        if index is not None:
            self.watch_state["stocks"].pop(index)
            self.persist_watch()
            self.refresh_sidebar()

    def toggle_side(self):
        index=self.side_index()
        if index is not None:
            row=self.watch_state["stocks"][index]
            row["enabled"]=not row["enabled"]
            self.persist_watch()
            self.refresh_sidebar()
            self.side.selection_set(f"r{index}")

    def side_start(self,event):
        iid=self.side.identify_row(event.y)
        if iid.startswith("r") and self.side.identify_column(event.x)=="#1":
            self.drag_source=None
            self.side.selection_set(iid)
            self.sync_paper_check()
            self.paper_check.set(not self.paper_check.get())
            self.set_side_participation()
            return "break"
        self.drag_source=iid if iid.startswith("r") else None

    def side_move(self,event):
        target=self.side.identify_row(event.y)
        if self.drag_source and target.startswith("r"):
            self.side.selection_set(target)

    def side_drop(self,event):
        target=self.side.identify_row(event.y)
        source,self.drag_source=self.drag_source,None
        if source and target.startswith("r") and pools.move(self.watch_state["stocks"],int(source[1:]),int(target[1:])):
            self.persist_watch()
            self.refresh_sidebar()
            self.side.selection_set(target)

    def import_watch(self):
        filename=filedialog.askopenfilename(parent=self.root,filetypes=[("自选 JSON","*.json")])
        if filename:
            try:
                path=Path(filename)
                if path.stat().st_size>4_000_000:
                    raise ValueError("自选文件超过 4 MB")
                self.watch_state=pools.merge(self.watch_state,json.loads(path.read_text(encoding="utf-8-sig")))
                self.persist_watch()
                self.refresh_sidebar()
            except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
                messagebox.showerror("导入自选",str(exc),parent=self.root)

    def export_watch(self):
        filename=filedialog.asksaveasfilename(parent=self.root,defaultextension=".json",
                                             filetypes=[("自选 JSON","*.json")],initialfile="GS-自选池.json")
        if filename:
            watch.save_state(Path(filename),{k:self.watch_state[k] for k in ("pools","stocks")})
