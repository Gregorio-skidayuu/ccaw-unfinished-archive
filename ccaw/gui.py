"""A local Tk desktop for the existing CCAW task and execution service."""
import argparse
import copy
import json
import os
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Thread
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk
import webbrowser

from .config import WorkflowError, atomic_write
from .desktop import DesktopService


STATUS_NAMES = {
    "READY": "待开始", "WAITING_JOB": "等待作业", "QUEUED": "已排队",
    "STARTING": "启动中", "RUNNING": "运行中", "SUCCEEDED": "执行成功",
    "VERIFYING": "待验收", "DONE": "验收完成", "FAILED": "执行失败",
    "CANCELLED": "已取消", "WAITING_CAPACITY": "等待额度", "UNKNOWN": "状态未知",
    "WAITING_INPUT": "等待人工处理",
}
ROLE_NAMES = {"经济档": "economy", "主力档": "default", "专家档": "expert"}
KIND_NAMES = {"代码工作": "code", "ChatGPT 基础对话": "chat", "脚本计算": "script"}


def default_home():
    configured = os.environ.get("CCAW_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CCAW"
    return Path.cwd() / ".ccaw"


class DesktopApp:
    def __init__(self, root, home, project=None, configure=False):
        self.root = root
        self.service = DesktopService(home, project)
        self.home = Path(home).resolve()
        self.project = Path(project or Path.cwd()).resolve()
        self.events = Queue()
        self.closed = False
        self.refreshing = False
        self.operations = 0
        self.tasks = {}
        self.task_jobs = {}
        self.jobs = {}
        self.project_blocks = set()
        self.selected_task = None
        self.selected_job = None
        self.current_config = None
        self.config_hash = None
        self.config_vars = {}
        self.status_text = tk.StringVar(value="正在载入本地账本…")
        self.location_text = tk.StringVar(value=str(self.home))
        self.show_all = tk.BooleanVar(value=False)
        self.board_signature = None
        self.detail_signature = None
        self.task_buttons = {}
        self.ready = False
        self.key_hint = tk.StringVar(value="未读取或显示任何密钥")
        self._style()
        self._layout()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self._drain)
        self._submit("正在载入本地账本…", self._initial_data, self._loaded, quiet=True)
        self.root.after(2000, self._poll)
        if configure:
            self.tabs.select(self.settings_tab)

    def _style(self):
        self.root.title("CCAW · 本地工作台")
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.font = "Microsoft YaHei UI" if os.name == "nt" else "TkDefaultFont"
        width = min(int(1110 * self.scale), self.root.winfo_screenwidth() - 55)
        height = min(int(730 * self.scale), self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(760, width), min(540, height))
        self.root.configure(background="#edf2f7")
        font.nametofont("TkDefaultFont", root=self.root).configure(family=self.font, size=10)
        font.nametofont("TkTextFont", root=self.root).configure(family=self.font, size=10)
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#edf2f7")
        style.configure("TLabel", background="#edf2f7", foreground="#172943")
        style.configure("Muted.TLabel", foreground="#526982")
        style.configure("Paper.TFrame", background="white")
        for name, size, color in (("Paper", 10, "#172943"), ("Soft", 10, "#526982"),
                                  ("Heading", 14, "#172943"), ("TaskTitle", 19, "#172943")):
            weight = "bold" if name in {"Heading", "TaskTitle"} else "normal"
            style.configure(f"{name}.TLabel", background="white", foreground=color, font=(self.font, size, weight))
        style.configure("TButton", padding=(10, 6), background="#ffffff", foreground="#203957", bordercolor="#c4d2e2")
        style.map("TButton", background=[("active", "#e3edfb")])
        style.configure("Primary.TButton", background="#1956b4", foreground="white", bordercolor="#1956b4")
        style.map("Primary.TButton", background=[("disabled", "#d7e0ee"), ("active", "#124589")],
                  foreground=[("disabled", "#526982"), ("!disabled", "white")])
        style.configure("TNotebook", background="#edf2f7", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 7), background="#e3ebf5", foreground="#344d6b")
        style.map("TNotebook.Tab", background=[("selected", "#ffffff")], foreground=[("selected", "#1956b4")])
        style.configure("TCheckbutton", background="white", foreground="#526982")
        style.map("TCheckbutton", background=[("active", "#eaf2ff")])
        style.configure("Vertical.TScrollbar", background="#e1eaf3", troughcolor="#f2f6fa",
                        bordercolor="#e1eaf3", arrowcolor="#526982")
        style.configure("TLabelframe", background="#edf2f7")
        style.configure("TLabelframe.Label", font=(self.font, 11, "bold"))

    def _layout(self):
        header = tk.Frame(self.root, background="#142742", padx=21, pady=12)
        header.pack(fill="x")
        tk.Label(header, text="CCAW", background="#142742", foreground="white",
                 font=(self.font, 17, "bold")).pack(side="left", padx=(0, 15))
        tk.Label(header, text="本地工作台", background="#142742", foreground="#c2d0df").pack(side="left")
        self.new_button = ttk.Button(header, text="新建任务", style="Primary.TButton", command=self._new_task, state="disabled")
        self.new_button.pack(side="right")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=17, pady=(12, 5))
        self.work_tab = ttk.Frame(self.tabs, padding=(0, 9, 0, 0))
        self.settings_tab = ttk.Frame(self.tabs, padding=13)
        self.help_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.work_tab, text="任务")
        self.tabs.add(self.settings_tab, text="模型设置")
        self.tabs.add(self.help_tab, text="帮助")
        self._workbench()
        self._settings()
        self._help()
        footer = ttk.Frame(self.root, padding=(18, 1, 18, 7))
        footer.pack(side="bottom", fill="x", before=self.tabs)
        ttk.Button(footer, text="切换账本…", command=self._switch_home).pack(side="right")
        ttk.Label(footer, textvariable=self.status_text, style="Muted.TLabel").pack(anchor="w", fill="x")
        ttk.Label(footer, textvariable=self.location_text, style="Muted.TLabel").pack(anchor="w", fill="x")

    def _viewport(self, parent):
        holder = ttk.Frame(parent, style="Paper.TFrame")
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, background="white", highlightthickness=0)
        bar = ttk.Scrollbar(holder, command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(canvas, style="Paper.TFrame")
        item = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        content.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        content.canvas = canvas
        return content

    def _workbench(self):
        self.work_tab.columnconfigure(0, weight=1, minsize=232, uniform="workbench")
        self.work_tab.columnconfigure(1, weight=2, minsize=420, uniform="workbench")
        self.work_tab.rowconfigure(0, weight=1)
        left = ttk.Frame(self.work_tab, padding=(13, 12), style="Paper.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right = ttk.Frame(self.work_tab, padding=(19, 13), style="Paper.TFrame")
        right.grid(row=0, column=1, sticky="nsew")
        toolbar = ttk.Frame(left, style="Paper.TFrame")
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="任务", style="Heading.TLabel").pack(side="left")
        ttk.Button(toolbar, text="刷新", command=self.refresh).pack(side="right")
        ttk.Checkbutton(left, text="包含已完成任务", variable=self.show_all, command=self._render_board).pack(anchor="w", pady=(0, 6))
        self.board = self._viewport(left)
        self.detail = self._viewport(right)
        self.board.canvas.bind("<Configure>", lambda event: self._resize_wrap(self.board, event.width), add="+")
        self.detail.canvas.bind("<Configure>", lambda event: self._resize_wrap(self.detail, event.width), add="+")
        self.root.bind("<MouseWheel>", self._wheel, add="+")
        ttk.Label(self.detail, text="正在读取任务…", style="Soft.TLabel").pack(anchor="w")

    def _resize_wrap(self, pane, width):
        for widget in pane.winfo_children():
            if getattr(widget, "wraps_in_pane", False):
                widget.configure(wraplength=max(100, width - 30))

    def _wheel(self, event):
        widget = event.widget
        while widget is not None:
            if widget in (self.board, self.detail):
                widget.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return "break"
            widget = getattr(widget, "master", None)
        return None

    def _paragraph(self, parent, text, style="Paper.TLabel", pady=(0, 6)):
        width = parent.canvas.winfo_width() if hasattr(parent, "canvas") else 460
        label = ttk.Label(parent, text=text, style=style, wraplength=max(width - 30, 100), justify="left")
        label.wraps_in_pane = True
        label.pack(fill="x", anchor="w", pady=pady)
        return label

    def _settings(self):
        self.settings_tab.columnconfigure(0, weight=1)
        self.settings_tab.columnconfigure(1, weight=1)
        self.settings_tab.rowconfigure(0, weight=1)
        left = ttk.LabelFrame(self.settings_tab, text="Codex 与执行角色", padding=14)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        right = ttk.LabelFrame(self.settings_tab, text="一组兼容 Chat Completions 的文本 API", padding=14)
        right.grid(row=0, column=1, sticky="nsew")
        ttk.Label(left, text="Codex 可执行程序", style="Muted.TLabel").pack(anchor="w")
        executable_row = ttk.Frame(left)
        executable_row.pack(fill="x", pady=(4, 14))
        self.executable_var = tk.StringVar()
        ttk.Entry(executable_row, textvariable=self.executable_var).pack(side="left", fill="x", expand=True)
        ttk.Button(executable_row, text="选择…", command=self._choose_codex).pack(side="right", padx=(5, 0))
        ttk.Label(left, text="模型 ID 留空时沿用 Codex 本机默认值。", wraplength=420).pack(anchor="w", pady=(0, 12))
        mapping = ttk.Frame(left)
        mapping.pack(fill="x")
        mapping.columnconfigure(2, weight=1)
        for col, text in enumerate(("角色", "后端", "Codex 模型 ID")):
            ttk.Label(mapping, text=text, style="Muted.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 6), pady=(0, 5))
        self.role_fields = {}
        for row, (label, role) in enumerate(ROLE_NAMES.items(), 1):
            backend, model = tk.StringVar(), tk.StringVar()
            ttk.Label(mapping, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=7)
            select = ttk.Combobox(mapping, textvariable=backend, values=("codex", "api"), state="readonly", width=9)
            select.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=7)
            entry = ttk.Entry(mapping, textvariable=model)
            entry.grid(row=row, column=2, sticky="ew", pady=7)
            self.role_fields[role] = (backend, model, entry)
            select.bind("<<ComboboxSelected>>", lambda event: self._role_fields_state())
        ttk.Label(left, text="API 角色统一使用右侧 API 模型；这里的模型 ID 仅用于 Codex。当前版本不是多供应商配置中心。", wraplength=420, style="Muted.TLabel").pack(anchor="w", pady=(13, 18))
        ttk.Separator(left).pack(fill="x")
        ttk.Label(left, text="ChatGPT 基础对话", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(16, 6))
        ttk.Label(left, text="使用 ChatGPT 网页与免费账号。\n工作台会复制交接包，再由你在网页粘贴发送；不调用免费对话的非官方接口，也不替换为其他模型。", wraplength=420).pack(anchor="w")
        ttk.Label(left, text="配置只保存模型与预算，不保存密钥。\n更改只影响后续执行，已有作业保留当时快照。", wraplength=420, style="Muted.TLabel").pack(anchor="w", pady=(16, 0))
        self.enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="启用付费文本 API（默认关闭）", variable=self.enabled_var).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        labels = (
            ("base_url", "HTTPS 地址"), ("model", "API 模型 ID"), ("key_env", "密钥环境变量名"),
            ("input_usd_per_million", "输入价 / 百万 token（美元）"),
            ("output_usd_per_million", "输出价 / 百万 token（美元）"),
            ("monthly_limit_usd", "本地月预算（美元）"),
            ("max_output_tokens", "输出 token 上限"), ("max_input_bytes", "输入字节上限"),
            ("reasoning_effort", "推理档位"),
        )
        right.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate(labels, 1):
            value = tk.StringVar()
            self.config_vars[key] = value
            ttk.Label(right, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            if key == "reasoning_effort":
                widget = ttk.Combobox(right, textvariable=value, values=("none", "low", "high", "max"), state="readonly", width=18)
            else:
                widget = ttk.Entry(right, textvariable=value, width=23)
            widget.grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Separator(right).grid(row=10, column=0, columnspan=2, sticky="ew", pady=(10, 8))
        ttk.Label(right, text="本次会话 API Key").grid(row=11, column=0, sticky="w", padx=(0, 10))
        self.api_key_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.api_key_var, show="●").grid(row=11, column=1, sticky="ew")
        ttk.Label(right, text="只进入本次界面进程的环境，保存后清空输入；关闭应用后需重新填写。请不要把密钥放进任务或提示词。", wraplength=430, style="Muted.TLabel").grid(row=12, column=0, columnspan=2, sticky="w", pady=(7, 5))
        ttk.Label(right, textvariable=self.key_hint, wraplength=430, style="Muted.TLabel").grid(row=13, column=0, columnspan=2, sticky="w")
        buttons = ttk.Frame(self.settings_tab)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.save_button = ttk.Button(buttons, text="保存模型配置", style="Primary.TButton", command=self._save_config)
        self.save_button.pack(side="left")
        ttk.Button(buttons, text="重新读取", command=self._reload_config).pack(side="left", padx=7)
        ttk.Button(buttons, text="检查环境（不推理）", command=self._health).pack(side="right")
        self.revision_text = tk.StringVar(value="尚未读取配置")
        ttk.Label(buttons, textvariable=self.revision_text, style="Muted.TLabel").pack(side="left", padx=8)

    def _help(self):
        text = (
            "从任务开始\n\n"
            "新建任务时填写目标、工作目录和验收条件。可启动本地脚本或按角色交给 Codex，"
            "也可复制任务包到 ChatGPT。\n\n"
            "关窗和重开\n\n"
            "独立作业在窗口关闭后继续运行。重开同一账本即可找回任务和证据。"
            "等待额度时查看原作业的 session 和接续许可。状态未知时，先从作业证据核对进程和日志。\n\n"
            "验证结果\n\n"
            "打开作业证据查看输出和产物。核对验收条件后保存证据，任务才会显示验收完成。"
            "脚本可把自定义产物写入工作目录，并在标准输出中注明相对路径。"
        )
        content = tk.Text(self.help_tab, wrap="word", relief="flat", padx=14, pady=14)
        content.pack(fill="both", expand=True)
        content.insert("1.0", text)
        content.configure(state="disabled")

    def _submit(self, label, operation, completed=None, widget=None, error_parent=None, quiet=False):
        if self.closed:
            return
        self.operations += 1
        if label:
            self.status_text.set(label)
        if widget:
            widget.configure(state="disabled")
        def work():
            try:
                self.events.put((True, operation(), completed, widget, error_parent, quiet))
            except Exception as exc:
                message = str(exc) if isinstance(exc, (WorkflowError, OSError, ValueError)) else f"操作未完成（{type(exc).__name__}），请查看账本与日志。"
                self.events.put((False, message, completed, widget, error_parent, quiet))
        Thread(target=work, daemon=True).start()

    def _drain(self):
        if self.closed:
            return
        try:
            while True:
                ok, value, completed, widget, error_parent, quiet = self.events.get_nowait()
                self.operations = max(0, self.operations - 1)
                if widget and widget.winfo_exists():
                    widget.configure(state="normal")
                if ok:
                    if completed:
                        completed(value)
                else:
                    self.refreshing = False
                    self.status_text.set(value)
                    if not quiet:
                        parent = error_parent if error_parent and error_parent.winfo_exists() else self.root
                        messagebox.showerror("操作未完成", value, parent=parent)
        except Empty:
            pass
        self.root.after(100, self._drain)

    def _initial_data(self):
        self.service.ensure_initialized()
        snapshot = self.service.snapshot()
        try:
            snapshot["selected_task"] = (self.home / "desktop-selection.txt").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            snapshot["selected_task"] = None
        return snapshot

    def _loaded(self, snapshot):
        self.selected_task = snapshot.get("selected_task")
        self.ready = True
        self.new_button.configure(state="normal")
        self._render(snapshot)
        self._reload_config()
        self.status_text.set("本地账本已载入")

    def _poll(self):
        if not self.closed:
            if self.ready and not self.operations:
                self.refresh(quiet=True)
            self.root.after(2000, self._poll)

    def refresh(self, quiet=False):
        if self.refreshing:
            return
        self.refreshing = True
        if self.ready:
            self._submit(None if quiet else "正在刷新…", self.service.snapshot, self._render, quiet=quiet)
        else:
            self._submit("正在读取账本…", self._initial_data, self._loaded, quiet=True)

    def _render(self, snapshot):
        self.refreshing = False
        self.tasks = {row["id"]: row for row in snapshot["tasks"]}
        self.jobs = {row["id"]: row for row in snapshot["jobs"]}
        self.task_jobs = {task_id: [] for task_id in self.tasks}
        for job in self.jobs.values():
            self.task_jobs[job["task_id"]].append(job)
        self.project_blocks = {
            job["cwd"] for job in self.jobs.values() if job["status"] in {"QUEUED", "STARTING", "RUNNING", "UNKNOWN"}
        }
        if self.selected_task not in self.tasks:
            self.selected_task = next(iter(self._ordered_tasks()), None)
        self._render_board()
        self._render_detail()

    def _current_job(self, task_id):
        jobs = self.task_jobs.get(task_id, ())
        for statuses in ({"UNKNOWN"}, {"RUNNING", "STARTING", "QUEUED"}):
            for job in jobs:
                if job["status"] in statuses:
                    return job
        return next(iter(jobs), {})

    def _task_state(self, task):
        status = self._current_job(task["id"]).get("status")
        if status in {"UNKNOWN", "RUNNING", "STARTING", "QUEUED"}:
            return status
        if task["status"] == "DONE":
            return "DONE"
        if status == "SUCCEEDED":
            return "VERIFYING"
        return status or task["status"]

    def _group(self, state):
        if state in {"UNKNOWN", "FAILED", "WAITING_INPUT", "WAITING_CAPACITY", "CANCELLED"}:
            return "需处理"
        if state == "VERIFYING":
            return "待验收"
        if state in {"RUNNING", "STARTING", "QUEUED", "WAITING_JOB"}:
            return "执行中"
        return "已完成" if state == "DONE" else "待开始"

    def _ordered_tasks(self):
        groups = ("需处理", "待验收", "执行中", "待开始", "已完成")
        return sorted(self.tasks, key=lambda task_id: groups.index(self._group(self._task_state(self.tasks[task_id]))))

    def _render_board(self):
        visible = [(task_id, self.tasks[task_id]["title"], self._task_state(self.tasks[task_id]))
                   for task_id in self._ordered_tasks()]
        visible = [item for item in visible if item[2] != "DONE" or self.show_all.get() or item[0] == self.selected_task]
        signature = (tuple(visible), self.selected_task)
        if signature == self.board_signature:
            return
        self.board_signature = signature
        for widget in self.board.winfo_children():
            widget.destroy()
        self.task_buttons.clear()
        group = None
        for task_id, title, state in visible:
            heading = self._group(state)
            if group != heading:
                self._paragraph(self.board, heading, "Soft.TLabel", pady=(10, 4))
                group = heading
            current = task_id == self.selected_task
            colors = {"需处理": "#903c27", "待验收": "#255aa4", "执行中": "#07636b"}
            button = tk.Button(self.board, text=f"{STATUS_NAMES.get(state, state)}\n{title}", anchor="w", justify="left",
                               foreground=colors.get(group, "#203957"), background="#e9f2ff" if current else "#f4f7fb",
                               activebackground="#dceafd", activeforeground="#172943",
                               borderwidth=0, highlightthickness=1, highlightbackground="#a9c5ec" if current else "#e0e8f2",
                               highlightcolor="#1956b4", relief="flat", padx=10, pady=8,
                               wraplength=max(100, self.board.canvas.winfo_width() - 30),
                               command=lambda identity=task_id: self._choose_task(identity))
            button.wraps_in_pane = True
            button.pack(fill="x", pady=(0, 5))
            self.task_buttons[task_id] = button
        if not visible:
            self._paragraph(self.board, "暂无任务。点击「新建任务」开始。" if not self.tasks else "暂无未完成的任务。", "Soft.TLabel")

    def _choose_task(self, task_id):
        self.selected_task = task_id
        try:
            atomic_write(self.home / "desktop-selection.txt", task_id + "\n")
        except OSError as exc:
            self.status_text.set(f"无法保存上次查看的位置：{exc}")
        self._render_board()
        self._render_detail()

    def _render_detail(self):
        task = self.tasks.get(self.selected_task)
        job = self._current_job(self.selected_task)
        self.selected_job = job.get("id")
        signature = (task, job, task["project"] in self.project_blocks if task else False)
        if signature == self.detail_signature:
            return
        old_task = self.detail_signature[0] if self.detail_signature else None
        if not old_task or not task or old_task["id"] != task["id"]:
            self.detail.canvas.yview_moveto(0)
        self.detail_signature = signature
        for widget in self.detail.winfo_children():
            widget.destroy()
        self.primary_button = None
        if not task:
            self._paragraph(self.detail, "还没有任务", "TaskTitle.TLabel", pady=(0, 12))
            self._paragraph(self.detail, "新建一个任务，写下目标和可以核对的验收条件。", "Soft.TLabel")
            return
        state = self._task_state(task)
        explanation = {
            "READY": "任务已保存。选择执行方式后即可开始。",
            "QUEUED": "作业已入队，等待派发。打开作业查看可用操作。",
            "STARTING": "正在启动独立进程。",
            "RUNNING": "独立作业正在执行。关闭窗口后可以回到同一任务。",
            "WAITING_JOB": "等待作业状态更新。",
            "WAITING_CAPACITY": "订阅额度暂停了执行。查看原 session、等待原因和接续许可。",
            "WAITING_INPUT": "任务需要人工处理。查看最后一次作业和已保存的证据。",
            "UNKNOWN": "无法确认原作业已结束。先核对进程和日志，此项目暂不可派发。",
            "FAILED": "先查看错误信息，再决定是否启动新阶段。",
            "CANCELLED": "作业已取消。核对已保留的成果后可开始新阶段。",
            "VERIFYING": "作业已成功退出。请核对成果并记录验收证据。",
            "DONE": "任务已确认完成，验收证据已保存在账本。",
        }.get(state, "请查看任务与最近作业。")
        self._paragraph(self.detail, "任务详情", "Soft.TLabel", pady=(0, 4))
        self._paragraph(self.detail, task["title"], "TaskTitle.TLabel", pady=(0, 13))
        group = self._group(state)
        tint, ink = {"需处理": ("#fff1e9", "#903c27"), "待验收": ("#eaf2ff", "#255aa4"),
                     "执行中": ("#e7f5f4", "#07636b"), "已完成": ("#e7f5ed", "#246344")}.get(group, ("#eff3f8", "#203957"))
        band = tk.Label(self.detail, text=f"{STATUS_NAMES.get(state, state)}\n{explanation}",
                        background=tint, foreground=ink, padx=12, pady=10, anchor="w", justify="left",
                        wraplength=max(100, self.detail.canvas.winfo_width() - 30))
        band.wraps_in_pane = True
        band.pack(fill="x", pady=(0, 11))
        bar = ttk.Frame(self.detail, style="Paper.TFrame")
        bar.pack(fill="x", pady=(0, 13))
        execute = self._chat if task["kind"] == "chat" else self._run_dialog
        new_allowed = task["project"] not in self.project_blocks or task["kind"] == "chat"
        if job:
            action = {"VERIFYING": "核对结果", "FAILED": "查看错误", "UNKNOWN": "核对状态",
                      "WAITING_CAPACITY": "查看等待详情", "DONE": "查看作业"}.get(state, "查看作业")
            self.primary_button = ttk.Button(bar, text=action, style="Primary.TButton", command=lambda: self._evidence(job["id"]))
            self.primary_button.pack(side="left")
            if len(self.task_jobs[task["id"]]) > 1:
                ttk.Button(bar, text="作业历史", command=self._history).pack(side="left", padx=6)
            if state in {"FAILED", "CANCELLED"} and new_allowed:
                ttk.Button(bar, text="新阶段…", command=execute).pack(side="right")
        elif state == "READY":
            action = {"script": "运行本地脚本", "chat": "复制 ChatGPT 交接包", "code": "配置并启动作业"}.get(task["kind"], "配置并启动作业")
            self.primary_button = ttk.Button(bar, text=action, style="Primary.TButton", command=execute,
                                             state="normal" if new_allowed else "disabled")
            self.primary_button.pack(side="left")
            if not new_allowed:
                self._paragraph(self.detail, "此工作目录还有运行中或待核对的作业。", "Soft.TLabel")
        for label, content in (("目标", task["goal"]), ("验收条件", task["acceptance"])):
            self._paragraph(self.detail, label, "Heading.TLabel", pady=(0, 4))
            self._paragraph(self.detail, content or "未记录", pady=(0, 12))
        if state == "DONE":
            self._paragraph(self.detail, "验收记录", "Heading.TLabel", pady=(0, 4))
            record = self._paragraph(self.detail, "正在读取验收证据…")
            task_id = task["id"]

            def show_acceptance(packet):
                if record.winfo_exists():
                    event = packet["acceptance_event"]
                    record.configure(text=event["payload"].get("evidence", "未记录") if event else "此任务未记录验收证据。")

            self._submit(None, lambda: self.service.task_details(task_id), show_acceptance, quiet=True)
        if job:
            ttk.Separator(self.detail).pack(fill="x", pady=(2, 11))
            self._paragraph(self.detail, "当前作业", "Heading.TLabel", pady=(0, 3))
            label = {"script": "本地脚本", "codex": "Codex", "api": "文本 API"}.get(job["kind"], job["kind"])
            exit_code = f"   ·   退出码 {job['exit_code']}" if job["exit_code"] is not None else ""
            self._paragraph(self.detail, f"{label}   ·   {STATUS_NAMES.get(job['status'], job['status'])}{exit_code}")
            self._paragraph(self.detail, f"{job['id']}   ·   阶段 {job.get('dedupe_key', '')}", "Soft.TLabel")
        self._paragraph(self.detail, f"工作目录  {task['project']}", "Soft.TLabel")

    def _history(self):
        task = self.tasks[self.selected_task]
        dialog, body = self._dialog("作业历史")
        content = self._viewport(body)
        for job in self.task_jobs[task["id"]]:
            status = STATUS_NAMES.get(job["status"], job["status"])
            text = f"{status}   {job.get('dedupe_key', '')}   {job['id']}"
            ttk.Button(content, text=text, command=lambda identity=job["id"]: self._evidence(identity, dialog)).pack(fill="x", pady=3)

    def _dialog(self, title, parent=None, modal=False):
        parent = parent or self.root
        dialog = tk.Toplevel(parent)
        dialog.title(title)
        dialog.transient(parent)
        width = min(int(665 * self.scale), self.root.winfo_screenwidth() - 65)
        height = min(int(535 * self.scale), self.root.winfo_screenheight() - 110)
        dialog.geometry(f"{width}x{height}")
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        if modal:
            dialog.grab_set()
        return dialog, body

    def _evidence(self, job_id, parent=None):
        dialog, body = self._dialog("作业证据", parent)
        ttk.Label(body, text="正在读取作业…").pack(anchor="w")
        task = self.tasks[self.jobs[job_id]["task_id"]]

        def render(job):
            if not dialog.winfo_exists():
                return
            for item in body.winfo_children():
                item.destroy()
            diagnostic = job["diagnostics"]
            state = job["status"]
            ttk.Label(body, text=STATUS_NAMES.get(state, state), font=(self.font, 15, "bold")).pack(anchor="w")
            ttk.Label(body, text=job["id"] + (f"   ·   退出码 {job['exit_code']}" if job["exit_code"] is not None else "")).pack(anchor="w", pady=(2, 4))
            ttk.Label(body, text=diagnostic.get("next_action") or "", wraplength=max(200, body.winfo_width() - 40),
                      justify="left").pack(anchor="w", pady=(0, 8))
            actions = ttk.Frame(body)
            actions.pack(fill="x", side="bottom", pady=(9, 0))
            files = ttk.Frame(body)
            files.pack(fill="x", pady=(0, 4))
            preview = tk.Text(body, height=10, wrap="word", relief="flat", padx=8, pady=7, state="disabled")
            scrollbar = ttk.Scrollbar(body, command=preview.yview)
            preview.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            preview.pack(fill="both", expand=True)

            def show(text):
                preview.configure(state="normal")
                preview.delete("1.0", "end")
                preview.insert("1.0", text)
                preview.configure(state="disabled")

            def read_log(path):
                try:
                    with Path(path).open("rb") as stream:
                        data = stream.read(128 * 1024 + 1)
                    limit = 128 * 1024
                    text = data[:limit].decode("utf-8", errors="replace")
                    suffix = "\n\n仅显示前 128 KiB。可在作业目录打开完整文件。" if len(data) > limit else ""
                    show(f"{path}\n\n{text or '文件暂时没有内容。'}{suffix}")
                except OSError as exc:
                    show(f"无法读取文件：{exc}")

            def show_diagnostics():
                lines = [f"工作目录  {job['cwd']}", f"作业目录  {diagnostic['run_directory']}"]
                for label, field in (("Session", "session_id"), ("原因类别", "error_kind"), ("工作进程 PID", "pid"),
                                     ("进程标识", "process_token"), ("子进程 PID", "child_pid"),
                                     ("子进程标识", "child_token"), ("状态更新时间", "updated_at")):
                    if job.get(field) is not None:
                        lines.append(f"{label}  {job[field]}")
                if diagnostic.get("auto_resume"):
                    lines.extend(("", "额度接续", json.dumps(diagnostic["auto_resume"], ensure_ascii=False, indent=2)))
                for event in diagnostic["recent_events"]:
                    lines.extend(("", f"{event['created_at']}  {event['kind']}", json.dumps(event["payload"], ensure_ascii=False)))
                show("\n".join(lines))

            ttk.Button(files, text="运行记录", command=show_diagnostics).pack(side="left", padx=(0, 4))
            existing = diagnostic["files"]
            for label, name in (("标准输出", "stdout.log"), ("错误日志", "stderr.log"), ("结果", "result.md"),
                                ("运行器日志", "worker.log")):
                if name in existing:
                    ttk.Button(files, text=label, command=lambda file=existing[name]: read_log(file)).pack(side="left", padx=(0, 4))
            default_file = {"FAILED": "stderr.log", "SUCCEEDED": "result.md" if "result.md" in existing else "stdout.log"}.get(state)
            if default_file in existing:
                read_log(existing[default_file])
            else:
                show_diagnostics()

            ttk.Button(actions, text="打开项目目录", command=lambda: self._open_folder(job["cwd"], dialog)).pack(side="left")
            if Path(diagnostic["run_directory"]).is_dir():
                ttk.Button(actions, text="打开作业目录", command=lambda: self._open_folder(diagnostic["run_directory"], dialog)).pack(side="left", padx=4)
            refreshed_task = self.tasks.get(job["task_id"], task)
            if state == "SUCCEEDED" and refreshed_task["status"] != "DONE" and self._current_job(task["id"]).get("id") == job_id:
                ttk.Button(actions, text="确认验收", style="Primary.TButton",
                           command=lambda: self._accept_dialog(task, job_id, dialog)).pack(side="right")
            else:
                ttk.Button(actions, text="重新核对", command=load).pack(side="right")
            operations = ttk.Frame(body)
            operations.pack(fill="x", side="bottom")
            if state == "QUEUED":
                ttk.Button(operations, text="启动已排队作业", command=lambda: job_action("launch")).pack(side="left")
            if state in {"QUEUED", "STARTING", "RUNNING"}:
                ttk.Button(operations, text="取消作业…",
                           command=lambda: job_action("cancel") if messagebox.askyesno("取消作业", "请求停止这个作业？", parent=dialog) else None).pack(side="left")
            resume = diagnostic.get("auto_resume")
            if state == "WAITING_CAPACITY" and resume and resume.get("enabled"):
                ttk.Button(operations, text="重启等待", command=lambda: job_action("watch")).pack(side="left")
                ttk.Button(operations, text="停止自动接续", command=lambda: job_action("unwatch")).pack(side="left")

        def job_action(action):
            def done(result):
                self.refresh(quiet=True)
                load()
            self._submit("正在处理作业…", lambda: self.service.job_action(job_id, action), done, error_parent=dialog)

        def load():
            if dialog.winfo_exists():
                self._submit(None, lambda: self.service.job_details(job_id), render, error_parent=dialog)

        load()

    def _open_folder(self, path, parent):
        try:
            if not Path(path).is_dir():
                raise WorkflowError(f"找不到目录：{path}")
            if os.name == "nt":
                os.startfile(str(path))
            else:
                self.root.clipboard_clear()
                self.root.clipboard_append(path)
                self.status_text.set("目录已复制")
        except (OSError, WorkflowError) as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=parent)

    def _accept_dialog(self, task, job_id, parent):
        dialog, body = self._dialog("确认验收", parent, modal=True)
        ttk.Label(body, text="核对验收条件", font=(self.font, 14, "bold")).pack(anchor="w")
        ttk.Label(body, text=task["acceptance"], wraplength=max(200, int(560 * self.scale)), justify="left").pack(fill="x", pady=(5, 12))
        ttk.Label(body, text="验收证据（填写核对过的文件、输出或结论）").pack(anchor="w")
        evidence = tk.Text(body, height=6, wrap="word", relief="solid", borderwidth=1, padx=6, pady=5)
        evidence.pack(fill="both", expand=True, pady=(5, 10))
        save = ttk.Button(body, style="Primary.TButton", text="保存验收")
        save.pack(anchor="e")

        def done(record):
            if parent.winfo_exists():
                parent.destroy()
            self.status_text.set("验收已保存")
            self.refresh(quiet=True)

        def commit():
            value = evidence.get("1.0", "end-1c")
            self._submit("正在保存验收…", lambda: self.service.accept_task(task["id"], job_id, value),
                         done, widget=save, error_parent=dialog)
        save.configure(command=commit)
        evidence.focus_set()

    def _chat(self):
        task_id = self.selected_task
        self._submit("正在准备 ChatGPT 交接包…", lambda: self.service.chat_handoff(task_id), self._open_chat)

    def _open_chat(self, handoff):
        self.root.clipboard_clear()
        self.root.clipboard_append(handoff["text"])
        opened = webbrowser.open(handoff["url"])
        self.status_text.set("已复制交接包，请在 ChatGPT 网页粘贴并发送。" if opened else "已复制交接包。请打开 chatgpt.com 粘贴并发送。")

    def _run_dialog(self):
        task = self.tasks[self.selected_task]
        script = task["kind"] == "script"
        dialog, body = self._dialog("运行本地脚本" if script else "配置并启动作业", modal=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        ttk.Label(body, text=task["title"], wraplength=int(560 * self.scale), justify="left",
                  font=(self.font, 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        roles = {value: name for name, value in ROLE_NAMES.items()}
        config = self.current_config
        if script:
            python = Path(sys.executable)
            if python.stem.lower() == "pythonw" and python.with_name("python.exe").is_file():
                python = python.with_name("python.exe")
            executable = tk.StringVar(value="" if getattr(sys, "frozen", False) else str(python))
            ttk.Label(body, text="可执行文件").grid(row=1, column=0, sticky="w")
            field = ttk.Entry(body, textvariable=executable)
            field.grid(row=2, column=0, sticky="ew", pady=(4, 8))

            def browse():
                path = filedialog.askopenfilename(parent=dialog, initialdir=task["project"], title="选择可执行文件")
                if path:
                    executable.set(path)
            ttk.Button(body, text="选择…", command=browse).grid(row=2, column=1, padx=(6, 0), pady=(4, 8))
            caption = "参数（一行一个参数，含空格的路径也填写在同一行）"
        else:
            ttk.Label(body, text="执行角色").grid(row=1, column=0, sticky="w")
            role = tk.StringVar(value=roles.get(task["role"], "主力档"))
            field = ttk.Combobox(body, textvariable=role, values=list(ROLE_NAMES), state="readonly")
            field.grid(row=2, column=0, sticky="ew", pady=(4, 8))
            caption = "本次执行指令"
        ttk.Label(body, text=caption).grid(row=3, column=0, columnspan=2, sticky="w")
        arguments = tk.Text(body, height=6, wrap="word", padx=6, pady=5, relief="solid", borderwidth=1)
        arguments.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(4, 8))
        keys = {job.get("dedupe_key") for job in self.task_jobs[task["id"]]}
        default_key = "main"
        attempt = 2
        while default_key in keys:
            default_key = f"run-{attempt}"
            attempt += 1
        key = tk.StringVar(value=default_key)
        ttk.Label(body, text="阶段键（重复提交相同阶段会找回原作业）").grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Entry(body, textvariable=key).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        if not script:
            arguments.insert("1.0", "按任务目标执行，并记录可核对的结果。")
            auto, api = tk.BooleanVar(), tk.BooleanVar()
            auto_button = ttk.Checkbutton(body, text="额度自然恢复后自动接续原 Codex session", variable=auto)
            api_button = ttk.Checkbutton(body, text="允许本次将任务包发送到已配置的付费 API", variable=api)
            auto_button.grid(row=7, column=0, columnspan=2, sticky="w")
            api_button.grid(row=8, column=0, columnspan=2, sticky="w")

            def update_role(event=None):
                use_api = bool(config and config["roles"][ROLE_NAMES[role.get()]]["backend"] == "api")
                auto.set(False)
                api.set(False)
                auto_button.configure(state="disabled" if use_api else "normal")
                api_button.configure(state="normal" if use_api else "disabled")
            field.bind("<<ComboboxSelected>>", update_role)
            update_role()
        submit = ttk.Button(body, text="启动脚本" if script else "启动作业", style="Primary.TButton")
        submit.grid(row=9, column=0, columnspan=2, sticky="e", pady=(7, 0))

        def done(job):
            if dialog.winfo_exists():
                dialog.destroy()
            self.status_text.set(f"作业已记录  {job['id']}")
            self.refresh(quiet=True)

        def launch():
            stage = key.get()
            text = arguments.get("1.0", "end-1c")
            if script:
                command = [executable.get().strip(), *(text.split("\n") if text else [])]
                operation = lambda: self.service.start_script(task["id"], command, stage)
            else:
                selected_role, resume, allow = ROLE_NAMES[role.get()], auto.get(), api.get()
                operation = lambda: self.service.start_agent(task["id"], selected_role, text, stage, auto_resume=resume, allow_api=allow)
            self._submit("正在保存并派发作业…", operation, done, widget=submit, error_parent=dialog)
        submit.configure(command=launch)
        field.focus_set()

    def _new_task(self):
        if not self.ready:
            return
        dialog, body = self._dialog("新建任务", modal=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(3, weight=1)
        body.rowconfigure(4, weight=1)
        title, project, kind = tk.StringVar(), tk.StringVar(value=str(self.project)), tk.StringVar(value="脚本计算")
        for row, label in enumerate(("任务名称", "工作目录", "任务类型", "目标", "验收条件")):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=6)
        entry = ttk.Entry(body, textvariable=title)
        entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Entry(body, textvariable=project).grid(row=1, column=1, sticky="ew", pady=5)

        def browse():
            directory = filedialog.askdirectory(parent=dialog, initialdir=project.get())
            if directory:
                project.set(directory)
        ttk.Button(body, text="选择…", command=browse).grid(row=1, column=2, padx=(6, 0))
        ttk.Combobox(body, textvariable=kind, values=list(KIND_NAMES), state="readonly").grid(row=2, column=1, columnspan=2, sticky="ew", pady=5)
        goal = tk.Text(body, height=4, wrap="word", relief="solid", borderwidth=1, padx=6, pady=5)
        goal.grid(row=3, column=1, columnspan=2, sticky="nsew", pady=5)
        acceptance = tk.Text(body, height=4, wrap="word", relief="solid", borderwidth=1, padx=6, pady=5)
        acceptance.grid(row=4, column=1, columnspan=2, sticky="nsew", pady=5)
        save = ttk.Button(body, text="创建任务", style="Primary.TButton")
        save.grid(row=5, column=2, sticky="e", pady=(10, 0))

        def done(task):
            if dialog.winfo_exists():
                dialog.destroy()
            self.selected_task = task["id"]
            try:
                atomic_write(self.home / "desktop-selection.txt", task["id"] + "\n")
                self.status_text.set("任务已创建")
            except OSError as exc:
                self.status_text.set(f"任务已创建，无法保存查看位置：{exc}")
            self.refresh(quiet=True)

        def create():
            directory = project.get().strip()
            if not directory or not Path(directory).is_dir():
                messagebox.showerror("检查工作目录", "请选择存在的工作目录。", parent=dialog)
                return
            values = {"title": title.get(), "goal": goal.get("1.0", "end-1c").strip(),
                      "acceptance": acceptance.get("1.0", "end-1c"), "project": str(Path(directory).resolve()),
                      "kind": KIND_NAMES[kind.get()]}
            self._submit("正在创建任务…", lambda: self.service.create_task(**values), done, widget=save, error_parent=dialog)

        save.configure(command=create)
        entry.focus_set()

    def _reload_config(self):
        self._submit("正在读取模型配置…", self.service.configuration, self._fill_config)

    def _fill_config(self, data):
        self.current_config = copy.deepcopy(data["config"])
        self.config_hash = data["hash"]
        config = self.current_config
        self.executable_var.set(config["codex"]["executable"])
        for role, (backend, model, entry) in self.role_fields.items():
            backend.set(config["roles"][role]["backend"])
            model.set(config["roles"][role]["model"] or "")
        for key, variable in self.config_vars.items():
            value = config["paid_api"][key]
            variable.set("" if value is None else str(value))
        self.enabled_var.set(config["paid_api"]["enabled"])
        self.api_key_var.set("")
        self.key_hint.set("当前环境中已存在对应密钥，界面不会显示它。" if data.get("key_present") else "当前会话尚无对应密钥；需要 API 时再填写。")
        self.revision_text.set(f"当前配置版本：{config['revision']}")
        self._role_fields_state()

    def _role_fields_state(self):
        for backend, model, entry in self.role_fields.values():
            entry.configure(state="normal" if backend.get() == "codex" else "disabled")

    def _save_config(self):
        if not self.current_config:
            return
        candidate = copy.deepcopy(self.current_config)
        candidate["codex"]["executable"] = self.executable_var.get().strip()
        for role, (backend, model, entry) in self.role_fields.items():
            candidate["roles"][role] = {"backend": backend.get(), "model": (model.get().strip() or None) if backend.get() == "codex" else None}
        api = candidate["paid_api"]
        api["enabled"] = self.enabled_var.get()
        for key, variable in self.config_vars.items():
            value = variable.get().strip()
            if key in {"max_output_tokens", "max_input_bytes"}:
                try:
                    value = int(value)
                except ValueError:
                    messagebox.showinfo("检查输入", "输出 token 上限和输入字节上限必须为整数。", parent=self.root)
                    return
            elif key in {"input_usd_per_million", "output_usd_per_million"} and not value:
                value = None
            api[key] = value
        api_key = self.api_key_var.get() or None
        expected = self.config_hash
        def done(data):
            self._fill_config(data)
            self.status_text.set("模型配置已保存。可返回工作台选择任务；真实执行仍需你点击启动。")
        self._submit("正在校验并保存配置…", lambda: self.service.save_configuration(candidate, expected, api_key=api_key), done, widget=self.save_button)

    def _choose_codex(self):
        path = filedialog.askopenfilename(parent=self.root, title="选择 Codex 可执行程序", filetypes=[("可执行程序", "*.exe *.cmd *.bat"), ("全部文件", "*")])
        if path:
            self.executable_var.set(path)

    def _health(self):
        def done(report):
            dialog, body = self._dialog("环境检查")
            content = tk.Text(body, wrap="word", relief="flat")
            content.pack(fill="both", expand=True)
            content.insert("1.0", json.dumps(report, ensure_ascii=False, indent=2))
            content.configure(state="disabled")
            self.status_text.set("环境检查已完成")
        self._submit("正在检查环境…", self.service.health, done)

    def _switch_home(self):
        if self.operations:
            messagebox.showinfo("稍候", "请等待当前操作完成后再切换账本。", parent=self.root)
            return
        if any(isinstance(widget, tk.Toplevel) for widget in self.root.winfo_children()):
            messagebox.showinfo("关闭任务窗口", "请关闭任务和作业证据窗口后切换账本。", parent=self.root)
            return
        path = filedialog.askdirectory(parent=self.root, title="选择已有账本目录", initialdir=self.home)
        if not path:
            return
        home = Path(path)
        if not (home / "config.json").is_file() or not (home / "state.sqlite3").is_file():
            messagebox.showinfo("未找到已有账本", "所选目录需要包含 config.json 和 state.sqlite3。", parent=self.root)
            return
        self.home = home.resolve()
        self.location_text.set(str(self.home))
        self.service = DesktopService(self.home, self.project)
        self.current_config = None
        self.selected_task = self.selected_job = None
        self.ready = False
        self.new_button.configure(state="disabled")
        self._render({"tasks": [], "jobs": []})
        self.refresh()

    def close(self):
        self.closed = True
        self.api_key_var.set("")
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="CCAW 本地图形工作台")
    parser.add_argument("--home", default=str(default_home()))
    parser.add_argument("--project", default=str(Path.cwd()))
    parser.add_argument("--configure", action="store_true", help="打开后显示模型设置页")
    args = parser.parse_args(argv)
    root = tk.Tk()
    DesktopApp(root, args.home, args.project, args.configure)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
