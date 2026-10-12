"""Small native-session interface: select resources, choose a directory, download."""
import argparse
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .folder_picker import choose_directory
from .jobs import download_batch
from .native import NativeClient
from .service import discover_videos, selected_resources

ROOT = Path(__file__).resolve().parents[2]


class Application:
    def __init__(self, root, project=ROOT, connect=True):
        self.root, self.project = root, Path(project)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.downloading = False
        self.closing = False
        self.resources = {}
        self.checked = set()
        self.course_members = {}
        self.course_nodes = {}
        self.resource_parents = {}
        self.selection_anchor = None
        self.folder = tk.StringVar(value="")
        self.status = tk.StringVar(value="连接小鹅通学员客户端…")
        self.summary = tk.StringVar(value="0 个视频 · 0 份文档")
        root.title("小鹅通视频下载器")
        root.geometry("980x600")
        root.minsize(700, 400)
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(frame, columns=("type", "state"), show="tree headings", selectmode="extended")
        for column, title, width in (("#0", "课程 / 资源", 660), ("type", "类型", 90), ("state", "状态", 180)):
            self.table.heading(column, text=title)
            self.table.column(column, width=width, minwidth=80)
        self.check_images = self.create_check_images()
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        if connect:
            self.table.insert("", "end", iid="connection_status", text="连接小鹅通学员客户端…")
        controls = ttk.Frame(root, padding=(12, 4))
        controls.pack(fill="x")
        ttk.Entry(controls, textvariable=self.folder).pack(side="left", fill="x", expand=True)
        ttk.Button(controls, text="保存目录", command=self.choose_folder).pack(side="left", padx=8)
        self.button = ttk.Button(controls, text="下载选中资源", command=self.download, state="disabled")
        self.button.pack(side="left")
        self.cancel_button = ttk.Button(controls, text="取消下载", command=self.cancel_download, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        ttk.Label(root, textvariable=self.summary, padding=(12, 8)).pack(anchor="w")
        self.folder.trace_add("write", lambda *_: self.update_button())
        self.table.bind("<<TreeviewSelect>>", lambda _: self.update_button())
        self.table.bind("<Button-1>", self.check_click)
        self.table.bind("<space>", self.check_keyboard)
        self.table.bind("<Control-a>", lambda _: self.check_all(True))
        self.table.bind("<Control-A>", lambda _: self.check_all(True))
        self.table.bind("<Control-Shift-A>", lambda _: self.check_all(False))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_after = root.after(100, self.poll)
        self.connect_after = root.after(0, self.connect) if connect else None

    def update_button(self):
        busy = self.worker is not None and self.worker.is_alive()
        count = len(self.checked)
        self.button.configure(text=f"下载选中资源 ({count})" if count else "下载选中资源",
                              state="normal" if count and self.folder.get().strip() and not busy else "disabled")
        self.cancel_button.configure(state="normal" if self.downloading and busy and not self.cancel.is_set() else "disabled")

    def create_check_images(self):
        images = {}
        for state in ("empty", "checked", "partial"):
            image = tk.PhotoImage(master=self.root, width=16, height=16)
            image.put("#ffffff", to=(2, 2, 14, 14))
            for box in ((2, 2, 14, 3), (2, 13, 14, 14), (2, 2, 3, 14), (13, 2, 14, 14)):
                image.put("#6b7280", to=box)
            if state == "checked":
                for x, y in ((4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5)):
                    image.put("#1677ff", to=(x, y, x + 2, y + 2))
            elif state == "partial":
                image.put("#1677ff", to=(5, 7, 11, 10))
            images[state] = image
        return images

    def set_checked(self, nodes, value=True):
        members = set()
        for node in nodes:
            members.update(self.course_members.get(node, {node} if node in self.resources else set()))
        changed = members - self.checked if value else members & self.checked
        if value:
            self.checked.update(members)
        else:
            self.checked.difference_update(members)
        for key in changed:
            self.table.item(key, image=self.check_images["checked" if key in self.checked else "empty"])
        for parent in {self.resource_parents[key] for key in changed}:
            count = len(self.course_members[parent] & self.checked)
            state = "empty" if not count else "checked" if count == len(self.course_members[parent]) else "partial"
            self.table.item(parent, image=self.check_images[state])
        self.update_button()

    def toggle_checked(self, nodes):
        items = selected_resources(self.table, self.resources, nodes)
        self.set_checked(nodes, not all(item.key in self.checked for item in items))

    def visible_nodes(self):
        nodes = []
        def walk(parent=""):
            for node in self.table.get_children(parent):
                nodes.append(node)
                if self.table.item(node, "open"):
                    walk(node)
        walk()
        return nodes

    def check_click(self, event):
        node = self.table.identify_row(event.y)
        element = self.table.identify_element(event.x, event.y)
        if not node or "indicator" in element or self.table.identify_column(event.x) != "#0":
            return None
        self.table.focus_set()
        self.table.focus(node)
        self.table.selection_set(node)
        if event.state & 1 and self.selection_anchor:
            visible = self.visible_nodes()
            if self.selection_anchor in visible and node in visible:
                start, end = sorted((visible.index(self.selection_anchor), visible.index(node)))
                self.set_checked(visible[start:end + 1])
            else:
                self.toggle_checked([node])
        else:
            self.toggle_checked([node])
            self.selection_anchor = node
        return "break"

    def check_keyboard(self, _):
        nodes = self.table.selection() or ((self.table.focus(),) if self.table.focus() else ())
        self.toggle_checked(nodes)
        return "break"

    def check_all(self, value):
        self.set_checked(self.table.get_children(), value)
        return "break"

    def choose_folder(self):
        try:
            folder = choose_directory(self.root, self.folder.get().strip())
        except OSError:
            messagebox.showerror("保存目录", "无法打开目录选择窗口，请重试或直接输入目录。", parent=self.root)
            return
        if folder:
            self.folder.set(folder)

    def connect(self):
        self.connect_after = None
        if self.closing or (self.worker and self.worker.is_alive()):
            return

        def run():
            client = None
            try:
                client = NativeClient(self.project)
                first = True
                def catalog(result):
                    nonlocal first
                    self.events.put(("catalog" if first else "catalog_update", result))
                    first = False
                discover_videos(client, lambda n, total: self.events.put(("scan", n, total)),
                                self.cancel.is_set, on_catalog=catalog)
                self.events.put(("scan_done",))
            except Exception as error:
                self.events.put(("connection", isinstance(error, PermissionError)))
            finally:
                if client:
                    client.close()
                self.events.put(("idle",))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def populate_catalog(self, result, reset=False):
        if reset:
            self.table.delete(*self.table.get_children())
            self.resources.clear()
            self.checked.clear()
            self.course_members.clear()
            self.course_nodes.clear()
            self.resource_parents.clear()
            self.selection_anchor = None
        changed_parents = set()
        for index, course in enumerate(result.course_rows):
            identity = (str(course.get("app_id", "")), str(course.get("resource_id") or f"course-{index}"))
            if identity not in self.course_nodes:
                node = self.table.insert("", "end", text=str(course.get("title") or identity[1]),
                                         open=False, image=self.check_images["empty"])
                self.course_nodes[identity] = node
                self.course_members[node] = set()
        for item in result.resources:
            if item.key in self.resources:
                continue
            identity = (item.app_id, item.course_id)
            if identity not in self.course_nodes:
                self.course_nodes[identity] = self.table.insert("", "end", text=item.course_title,
                                                               open=False, image=self.check_images["empty"])
            parent = self.course_nodes[identity]
            self.resources[item.key] = item
            self.course_members.setdefault(parent, set()).add(item.key)
            changed_parents.add(parent)
            self.resource_parents[item.key] = parent
            self.table.insert(parent, "end", iid=item.key, text=item.title, image=self.check_images["empty"],
                              values=("视频" if item.resource_type == 3 else "文档", "待下载"))
        for parent in changed_parents:
            count = len(self.course_members[parent] & self.checked)
            state = "empty" if not count else "checked" if count == len(self.course_members[parent]) else "partial"
            self.table.item(parent, image=self.check_images[state])
        self.summary.set(f"{sum(v.resource_type == 3 for v in result.resources)} 个视频 · "
                         f"{sum(v.resource_type == 51 for v in result.resources)} 份文档")

    def download(self):
        if self.worker and self.worker.is_alive():
            return
        selected = selected_resources(self.table, self.resources, self.checked)
        folder = self.folder.get().strip()
        if not selected or not folder:
            self.status.set("选择资源和保存目录")
            return
        try:
            destination = Path(folder).expanduser().resolve()
        except (OSError, ValueError):
            self.status.set("保存目录无效")
            return
        self.cancel.clear()
        self.downloading = True
        self.button.configure(state="disabled")

        def run():
            try:
                counts = download_batch(lambda: NativeClient(self.project), selected, destination,
                                        self.cancel, self.events.put)
                self.events.put(("finished", *counts))
            except Exception:
                self.events.put(("download_error",))
            finally:
                self.events.put(("idle",))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def cancel_download(self):
        if self.downloading and self.worker and self.worker.is_alive():
            self.cancel.set()
            self.cancel_button.configure(state="disabled")
            self.status.set("正在取消下载…")

    def poll(self):
        if self.poll_after is not None:
            self.root.after_cancel(self.poll_after)
            self.poll_after = None
        try:
            while True:
                event = self.events.get_nowait()
                if self.closing:
                    continue
                kind = event[0]
                if kind == "scan":
                    self.status.set(f"读取课程 {event[1]}/{event[2]}…")
                    roots = self.table.get_children()
                    for node in roots:
                        self.table.set(node, "state", "")
                    if 0 < event[1] <= len(roots):
                        self.table.set(roots[event[1] - 1], "state", "读取中…")
                elif kind in ("catalog", "catalog_update"):
                    self.populate_catalog(event[1], reset=kind == "catalog")
                    self.status.set(self.summary.get())
                elif kind == "scan_done":
                    for node in self.table.get_children():
                        self.table.set(node, "state", "")
                    self.status.set(self.summary.get())
                elif kind == "connection":
                    self.status.set("等待小鹅通学员客户端登录…" if event[1] else "等待小鹅通学员客户端连接…")
                    if not self.table.get_children():
                        self.table.insert("", "end", iid="connection_status", text=self.status.get())
                    elif self.table.exists("connection_status"):
                        self.table.item("connection_status", text=self.status.get())
                    self.connect_after = self.root.after(5000, self.connect)
                elif kind == "job":
                    self.table.set(event[1], "state", event[2])
                    self.status.set(f"下载中 · 共 {event[4]} 项")
                elif kind == "batch":
                    self.status.set(f"已处理 {event[1]}/{event[2]}")
                elif kind == "finished":
                    prefix = "已取消 · " if self.cancel.is_set() else ""
                    self.status.set(prefix + f"完成 {event[1]} · 已存在 {event[3]} · 未完成 {event[2]}")
                elif kind == "download_error":
                    self.status.set("下载失败：请检查小鹅通学员客户端登录")
                elif kind == "idle":
                    self.downloading = False
                    self.update_button()
        except queue.Empty:
            pass
        if not self.closing:
            self.update_button()
            self.poll_after = self.root.after(100, self.poll)

    def close(self):
        self.closing = True
        self.cancel.set()
        for after in (self.poll_after, self.connect_after):
            if after is not None:
                self.root.after_cancel(after)
        self.poll_after = self.connect_after = None
        self.root.withdraw()
        self.finish_close()

    def finish_close(self):
        if self.worker and self.worker.is_alive():
            self.root.after(100, self.finish_close)
        else:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="Check UI construction without login or network")
    args = parser.parse_args()
    root = tk.Tk()
    if args.smoke:
        root.withdraw()
    app = Application(root, connect=not args.smoke)
    if args.smoke:
        root.update_idletasks()
        app.close()
    else:
        root.mainloop()


if __name__ == "__main__":
    main()
