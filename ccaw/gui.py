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
from tkinter import filedialog, messagebox, ttk
import webbrowser

from . import __version__
from .config import WorkflowError
from .desktop import DesktopService


STATUS_NAMES = {
    "READY": "待开始", "WAITING_JOB": "等待作业", "QUEUED": "已排队",
    "STARTING": "启动中", "RUNNING": "运行中", "SUCCEEDED": "执行成功",
    "VERIFYING": "待验收", "DONE": "已完成", "FAILED": "失败",
    "CANCELLED": "已取消", "WAITING_CAPACITY": "等待额度", "UNKNOWN": "需核对",
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
        self.jobs = {}
        self.selected_task = None
        self.selected_job = None
        self.job_signature = None
        self.current_config = None
        self.config_hash = None
        self.config_vars = {}
        self.status_text = tk.StringVar(value="正在载入本地账本…")
        self.location_text = tk.StringVar(value=str(self.home))
        self.summary_text = tk.StringVar(value="任务和作业保存在本机，关闭窗口不会取消独立作业。")
        self.model_hint = tk.StringVar(value="先在“模型设置”中填写你要使用的模型。")
        self.key_hint = tk.StringVar(value="未读取或显示任何密钥")
        self._style()
        self._layout()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self._drain)
        self._submit("正在载入本地账本…", self._initial_data, self._loaded)
        self.root.after(2000, self._poll)
        if configure:
            self.tabs.select(self.settings_tab)

    def _style(self):
        self.root.title(f"CCAW · 我的工作流 {__version__}")
        self.root.geometry("1180x800")
        self.root.minsize(1040, 710)
        self.root.configure(background="#f3f6fa")
        font = "Segoe UI" if os.name == "nt" else "TkDefaultFont"
        self.root.option_add("*Font", (font, 10))
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#f3f6fa")
        style.configure("TLabel", background="#f3f6fa", foreground="#17263c")
        style.configure("Title.TLabel", font=(font, 23, "bold"))
        style.configure("Muted.TLabel", foreground="#52647c")
        style.configure("TButton", padding=(10, 6))
        style.configure("Primary.TButton", background="#2563eb", foreground="white")
        style.map("Primary.TButton", background=[("active", "#1d4ed8"), ("disabled", "#a9b8d0")])
        style.configure("TNotebook", background="#f3f6fa", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9))
        style.configure("Treeview", rowheight=29, background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=(font, 10, "bold"), padding=5)
        style.configure("TLabelframe", background="#f3f6fa")
        style.configure("TLabelframe.Label", font=(font, 11, "bold"))

    def _layout(self):
        header = ttk.Frame(self.root, padding=(22, 15, 22, 10))
        header.pack(fill="x")
        ttk.Label(header, text="CCAW / 我的工作流", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, textvariable=self.summary_text, style="Muted.TLabel").pack(anchor="w", pady=(3, 8))
        location = ttk.Frame(header)
        location.pack(fill="x")
        ttk.Label(location, text="当前账本：", style="Muted.TLabel").pack(side="left")
        ttk.Label(location, textvariable=self.location_text).pack(side="left", fill="x", expand=True)
        ttk.Button(location, text="打开已有账本…", command=self._switch_home).pack(side="right")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        self.work_tab = ttk.Frame(self.tabs, padding=12)
        self.settings_tab = ttk.Frame(self.tabs, padding=16)
        self.help_tab = ttk.Frame(self.tabs, padding=20)
        self.tabs.add(self.work_tab, text="工作台")
        self.tabs.add(self.settings_tab, text="模型设置")
        self.tabs.add(self.help_tab, text="使用帮助")
        self._workbench()
        self._settings()
        self._help()
        footer = ttk.Frame(self.root, padding=(22, 2, 22, 10))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status_text, style="Muted.TLabel", wraplength=1100).pack(anchor="w")

    def _tree(self, parent, columns, height):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=[item[0] for item in columns], show="headings", height=height, selectmode="browse")
        for name, title, width in columns:
            tree.heading(name, text=title)
            tree.column(name, width=width, minwidth=65, stretch=True)
        scrollbar = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return tree

    def _workbench(self):
        self.work_tab.columnconfigure(0, weight=3)
        self.work_tab.columnconfigure(1, weight=2)
        self.work_tab.rowconfigure(0, weight=1)
        left = ttk.Frame(self.work_tab)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        right = ttk.Frame(self.work_tab)
        right.grid(row=0, column=1, sticky="nsew")
        toolbar = ttk.Frame(left)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="＋ 新建任务", style="Primary.TButton", command=self._new_task).pack(side="left")
        ttk.Button(toolbar, text="ChatGPT 免费网页对话", command=self._chat).pack(side="left", padx=6)
        ttk.Button(toolbar, text="刷新", command=self.refresh).pack(side="right")
        tasks_frame = ttk.LabelFrame(left, text="任务", padding=8)
        tasks_frame.pack(fill="both", expand=True)
        self.task_tree = self._tree(tasks_frame, [("title", "任务", 260), ("status", "状态", 105), ("role", "执行方式", 95)], 6)
        self.task_tree.bind("<<TreeviewSelect>>", self._select_task)
        jobs_frame = ttk.LabelFrame(left, text="所选任务的作业", padding=8)
        jobs_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.job_tree = self._tree(jobs_frame, [("id", "作业 ID", 185), ("kind", "类型", 65), ("status", "状态", 110)], 5)
        self.job_tree.bind("<<TreeviewSelect>>", self._select_job)
        actions = ttk.Frame(jobs_frame)
        actions.pack(fill="x", pady=(7, 0))
        for text, action in (("取消作业", "cancel"), ("重启等待", "watch"), ("停止自动继续", "unwatch")):
            ttk.Button(actions, text=text, command=lambda value=action: self._job_action(value)).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text="打开日志目录", command=self._open_logs).pack(side="right")
        execute = ttk.LabelFrame(right, text="按角色执行所选任务", padding=12)
        execute.pack(fill="x")
        row = ttk.Frame(execute)
        row.pack(fill="x")
        self.role_var = tk.StringVar(value="主力档")
        self.key_var = tk.StringVar(value="main")
        role = ttk.Combobox(row, textvariable=self.role_var, values=list(ROLE_NAMES), state="readonly", width=9)
        role.pack(side="left")
        role.bind("<<ComboboxSelected>>", lambda event: self._update_model_hint())
        ttk.Label(row, text="阶段键：").pack(side="left", padx=(10, 0))
        ttk.Entry(row, textvariable=self.key_var, width=18).pack(side="left", fill="x", expand=True)
        ttk.Label(execute, textvariable=self.model_hint, wraplength=385, style="Muted.TLabel").pack(anchor="w", pady=(7, 5))
        self.prompt = tk.Text(execute, height=5, wrap="word", relief="solid", borderwidth=1, background="white")
        self.prompt.pack(fill="x")
        self.prompt.insert("1.0", "依据任务目标和验收条件完成工作；先核对现有成果，再做必要修改与验证。")
        self.auto_var = tk.BooleanVar(value=False)
        self.allow_api_var = tk.BooleanVar(value=False)
        self.auto_check = ttk.Checkbutton(execute, text="额度自然恢复后自动继续（Codex）", variable=self.auto_var)
        self.auto_check.pack(anchor="w", pady=(6, 0))
        self.api_check = ttk.Checkbutton(execute, text="本次允许把任务包发送到付费 API", variable=self.allow_api_var)
        self.api_check.pack(anchor="w")
        self.run_button = ttk.Button(execute, text="启动所选角色", style="Primary.TButton", command=self._start_agent)
        self.run_button.pack(fill="x", pady=(8, 0))
        details = ttk.LabelFrame(right, text="任务 / 作业详情", padding=8)
        details.pack(fill="both", expand=True, pady=(12, 0))
        self.details = tk.Text(details, wrap="word", relief="flat", background="white", state="disabled", height=10)
        self.details.pack(fill="both", expand=True)
        self._set_details("选择左侧任务查看目标和验收条件。\n\n基础对话使用 ChatGPT 免费网页入口；产品不会将它替换成其他模型。\n\n请先到“模型设置”填写模型，再启动真实执行。")

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
            "先配置，再运行\n\n"
            "1. 在“模型设置”填入本机 Codex 程序与账号实际可用的模型 ID。经济、主力、专家只是角色，不会自动猜测最便宜或最强模型。\n\n"
            "2. 新建任务，写清目标、项目目录和可观察的验收条件。ChatGPT 基础对话由网页处理；代码工作可按角色交给 Codex。文本 API 只返回文本，不会自动修改项目文件。\n\n"
            "3. 工作台按任务显示作业。启动时同一阶段复用相同阶段键；明确的新阶段再换键。执行成功后仍需核对验收条件。\n\n"
            "额度与后台执行\n\n"
            "勾选自动继续只适用于 Codex 订阅。实际额度中断后由普通后台程序等待，明确恢复才接续原 session。未知状态必须先人工核对，界面不会强制重启。\n\n"
            "关闭这个窗口不取消独立作业。停止自动继续只撤销后续接续；取消实际执行请选中当前作业并使用“取消作业”。机器重启后的等待可使用“重启等待”。\n\n"
            "ChatGPT 免费网页对话\n\n"
            "选择任务后点击“ChatGPT 免费网页对话”，交接包会复制到本机剪贴板并打开 chatgpt.com。请使用你的免费账号，自己粘贴并发送。打开网页不代表已发送或已完成对话。\n\n"
            "密钥与账本\n\n"
            "API Key 只在本次应用会话内生效，不写入配置、任务或日志。每次 API 执行还需要单独勾选发送授权。付费 API 默认关闭，本地预算不能替代供应商的硬限额。\n\n"
            "顶部始终显示当前账本目录。打开已有账本不会搬移或重置原任务。公开仓库前仍需要真实模型验证、许可与发布检查，本地离线测试不等同于远端服务验收。"
        )
        content = tk.Text(self.help_tab, wrap="word", relief="flat", background="#f3f6fa", padx=10, pady=8)
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
        return self.service.snapshot()

    def _loaded(self, snapshot):
        self._render(snapshot)
        self._reload_config()
        self.status_text.set("已载入本地账本。模型配置由你填写，尚未启动任何模型请求。")

    def _poll(self):
        if not self.closed:
            if not self.operations:
                self.refresh(quiet=True)
            self.root.after(2000, self._poll)

    def refresh(self, quiet=False):
        if self.refreshing:
            return
        self.refreshing = True
        self._submit(None if quiet else "正在刷新任务与作业…", self.service.snapshot, self._render, quiet=quiet)

    def _render_rows(self, tree, rows, values):
        present = {row["id"] for row in rows}
        for old in tree.get_children():
            if old not in present:
                tree.delete(old)
        for index, row in enumerate(rows):
            if tree.exists(row["id"]):
                tree.item(row["id"], values=values(row))
            else:
                tree.insert("", "end", iid=row["id"], values=values(row))
            tree.move(row["id"], "", index)

    def _render(self, snapshot):
        self.refreshing = False
        self.tasks = {row["id"]: row for row in snapshot["tasks"]}
        self.jobs = {row["id"]: row for row in snapshot["jobs"]}
        role_labels = {"default": "主力档", "economy": "经济档", "expert": "专家档", "chat": "ChatGPT", "script": "脚本"}
        self._render_rows(self.task_tree, snapshot["tasks"], lambda row: (row["title"], STATUS_NAMES.get(row["status"], row["status"]), role_labels.get(row.get("role"), row.get("role", ""))))
        if self.selected_task and self.task_tree.exists(self.selected_task):
            self.task_tree.selection_set(self.selected_task)
        elif snapshot["tasks"]:
            self.task_tree.selection_set(snapshot["tasks"][0]["id"])
        rows = [row for row in snapshot["jobs"] if not self.selected_task or row["task_id"] == self.selected_task]
        self._render_rows(self.job_tree, rows, lambda row: (row["id"], row["kind"], STATUS_NAMES.get(row["status"], row["status"])))
        if self.selected_job and self.job_tree.exists(self.selected_job):
            self.job_tree.selection_set(self.selected_job)
            job = self.jobs[self.selected_job]
            signature = (job["id"], job["status"], job.get("exit_code"), job.get("error_kind"))
            if signature != self.job_signature:
                self.job_signature = signature
                self._load_job(job["id"])
        elif rows:
            self.job_tree.selection_set(rows[0]["id"])
        active = sum(row["status"] in {"QUEUED", "STARTING", "RUNNING"} for row in snapshot["jobs"])
        waiting = sum(row["status"] in {"WAITING_CAPACITY", "UNKNOWN"} for row in snapshot["jobs"])
        self.summary_text.set(f"{len(self.tasks)} 个任务 · {active} 个执行中 · {waiting} 个等待 / 待核对 · 当前操作均在本机账本记录")

    def _set_details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def _select_task(self, event=None):
        selected = self.task_tree.selection()
        if not selected or selected[0] == self.selected_task:
            return
        self.selected_task = selected[0]
        self.selected_job = None
        self.job_signature = None
        task_id = self.selected_task
        self._submit(None, lambda: self.service.task_details(task_id), self._show_task, quiet=True)
        rows = [row for row in self.jobs.values() if row["task_id"] == task_id]
        self._render_rows(self.job_tree, rows, lambda row: (row["id"], row["kind"], STATUS_NAMES.get(row["status"], row["status"])))

    def _show_task(self, packet):
        task = packet["task"]
        if task["id"] != self.selected_task:
            return
        self._set_details(f"{task['title']}\n\n任务：{task['id']}\n状态：{STATUS_NAMES.get(task['status'], task['status'])}\n项目：{task['project']}\n\n目标\n{task['goal']}\n\n验收条件\n{task['acceptance']}\n\n已记录 {len(packet['jobs'])} 次作业；匹配 {len(packet['lessons'])} 条已启用经验。")

    def _select_job(self, event=None):
        selected = self.job_tree.selection()
        if selected and selected[0] != self.selected_job:
            self.selected_job = selected[0]
            self._load_job(self.selected_job)

    def _load_job(self, job_id):
        self._submit(None, lambda: self.service.job_details(job_id), self._show_job, quiet=True)

    def _show_job(self, job):
        if job["id"] != self.selected_job:
            return
        self.last_job_details = job
        diagnostics = job.get("diagnostics", {})
        lines = [f"作业：{job['id']}", f"状态：{STATUS_NAMES.get(job['status'], job['status'])}", f"类型：{job['kind']}", f"退出码：{job.get('exit_code')}"]
        if job.get("session_id"):
            lines.append(f"Session：{job['session_id']}")
        if job.get("error_kind"):
            lines.append(f"原因类别：{job['error_kind']}")
        lines += ["", diagnostics.get("next_action", "")]
        auto = diagnostics.get("auto_resume")
        if auto:
            lines += ["", "自动继续", f"已授权：{'是' if auto.get('enabled') else '否'}", f"后台存活：{'是' if auto.get('watcher_running') else '否'}", f"当前作业：{auto.get('current_job_id', '')}", f"当前状态：{auto.get('current_status', '')}", auto.get("reason") or ""]
        lines += ["", "日志与结果"]
        lines += [f"{name}\n{path}" for name, path in diagnostics.get("files", {}).items()]
        self._set_details("\n".join(str(line) for line in lines))

    def _require_task(self):
        if not self.selected_task:
            messagebox.showinfo("先选择任务", "请新建或选择一个任务。", parent=self.root)
            return None
        return self.selected_task

    def _chat(self):
        task_id = self._require_task()
        if task_id:
            self._submit("正在准备 ChatGPT 交接包…", lambda: self.service.chat_handoff(task_id), self._open_chat)

    def _open_chat(self, handoff):
        self.root.clipboard_clear()
        self.root.clipboard_append(handoff["text"])
        opened = webbrowser.open(handoff["url"])
        self.status_text.set("任务包已复制。请在 ChatGPT 免费账号中粘贴并发送；产品尚未发送对话。" if opened else "任务包已复制。请自行打开 https://chatgpt.com/，用免费账号粘贴并发送。")

    def _start_agent(self):
        task_id = self._require_task()
        if not task_id:
            return
        if self.tasks.get(task_id, {}).get("kind") == "chat":
            messagebox.showinfo("ChatGPT 基础对话", "基础对话使用左侧的 ChatGPT 免费网页入口，不由其他模型执行。", parent=self.root)
            return
        key = self.key_var.get().strip()
        prompt = self.prompt.get("1.0", "end").strip()
        if not key or not prompt:
            messagebox.showinfo("补全执行信息", "请填写阶段键和本次执行指令。", parent=self.root)
            return
        role = ROLE_NAMES[self.role_var.get()]
        auto, allow = self.auto_var.get(), self.allow_api_var.get()
        def done(job):
            self.selected_job = job["id"]
            self.status_text.set(f"作业已持久化：{job['id']}。关闭界面不会取消独立执行。")
            self.refresh(quiet=True)
        self._submit("正在保存并启动作业…", lambda: self.service.start_agent(task_id, role, prompt, key, auto_resume=auto, allow_api=allow), done, widget=self.run_button)

    def _job_action(self, action):
        job_id = self.selected_job
        if not job_id:
            messagebox.showinfo("先选择作业", "请选择一个已有作业。", parent=self.root)
            return
        def done(result):
            self.status_text.set("操作已记录，请查看作业状态。")
            self.job_signature = None
            self.refresh(quiet=True)
        self._submit("正在处理作业…", lambda: self.service.job_action(job_id, action), done)

    def _open_logs(self):
        job = getattr(self, "last_job_details", None)
        if not job or job["id"] != self.selected_job:
            return
        folder = Path(job.get("diagnostics", {}).get("run_directory", ""))
        if not folder.is_dir():
            messagebox.showinfo("尚无日志", "作业尚未创建日志目录。", parent=self.root)
            return
        if os.name == "nt":
            os.startfile(folder)
        else:
            self.status_text.set(f"日志目录：{folder}")

    def _new_task(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("新建任务")
        dialog.geometry("670x600")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        title, project, kind, risk, complexity, tags = (tk.StringVar(value=value) for value in ("", str(self.project), "代码工作", "medium", "medium", ""))
        ttk.Label(body, text="任务标题").grid(row=0, column=0, sticky="w", pady=5)
        entry = ttk.Entry(body, textvariable=title)
        entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Label(body, text="项目目录").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=project).grid(row=1, column=1, sticky="ew", pady=5)
        def browse():
            path = filedialog.askdirectory(parent=dialog, initialdir=project.get())
            if path:
                project.set(path)
        ttk.Button(body, text="选择…", command=browse).grid(row=1, column=2, padx=(6, 0))
        ttk.Label(body, text="任务类型").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Combobox(body, textvariable=kind, values=list(KIND_NAMES), state="readonly").grid(row=2, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Label(body, text="目标").grid(row=3, column=0, sticky="nw", pady=7)
        goal = tk.Text(body, height=5, wrap="word", relief="solid", borderwidth=1)
        goal.grid(row=3, column=1, columnspan=2, sticky="nsew", pady=7)
        ttk.Label(body, text="验收条件").grid(row=4, column=0, sticky="nw", pady=7)
        acceptance = tk.Text(body, height=5, wrap="word", relief="solid", borderwidth=1)
        acceptance.grid(row=4, column=1, columnspan=2, sticky="nsew", pady=7)
        options = ttk.Frame(body)
        options.grid(row=5, column=1, columnspan=2, sticky="ew", pady=6)
        ttk.Label(options, text="风险").pack(side="left")
        ttk.Combobox(options, textvariable=risk, values=("low", "medium", "high"), state="readonly", width=10).pack(side="left", padx=(8, 18))
        ttk.Label(options, text="复杂度").pack(side="left")
        ttk.Combobox(options, textvariable=complexity, values=("low", "medium", "high"), state="readonly", width=10).pack(side="left", padx=8)
        ttk.Label(body, text="标签").grid(row=6, column=0, sticky="w", pady=6)
        ttk.Entry(body, textvariable=tags).grid(row=6, column=1, columnspan=2, sticky="ew", pady=6)
        ttk.Label(body, text="标签用逗号分隔；ChatGPT 对话需要你在网页中粘贴交接包。", style="Muted.TLabel").grid(row=7, column=1, columnspan=2, sticky="w", pady=6)
        save = ttk.Button(body, text="创建任务", style="Primary.TButton")
        save.grid(row=8, column=1, sticky="e", pady=12)
        def create():
            values = {"title": title.get().strip(), "goal": goal.get("1.0", "end").strip(), "acceptance": acceptance.get("1.0", "end").strip(), "project": project.get().strip(), "kind": KIND_NAMES[kind.get()], "risk": risk.get(), "complexity": complexity.get(), "tags": [item.strip() for item in tags.get().replace("，", ",").split(",") if item.strip()]}
            if not values["title"] or not values["acceptance"] or not values["project"]:
                messagebox.showinfo("补全任务", "标题、项目目录和验收条件不能为空。", parent=dialog)
                return
            values["goal"] = values["goal"] or values["title"]
            def done(task):
                self.selected_task = task["id"]
                self.selected_job = None
                dialog.destroy()
                self.status_text.set(f"任务已创建：{task['id']}")
                self.refresh(quiet=True)
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
        self._update_model_hint()

    def _role_fields_state(self):
        for backend, model, entry in self.role_fields.values():
            entry.configure(state="normal" if backend.get() == "codex" else "disabled")

    def _update_model_hint(self):
        if not self.current_config:
            return
        role = ROLE_NAMES[self.role_var.get()]
        setting = self.current_config["roles"][role]
        if setting["backend"] == "api":
            model = self.current_config["paid_api"]["model"] or "尚未填写 API 模型"
            self.model_hint.set(f"{self.role_var.get()}：文本 API / {model}")
            self.auto_var.set(False)
            self.auto_check.configure(state="disabled")
            self.api_check.configure(state="normal")
        else:
            self.model_hint.set(f"{self.role_var.get()}：Codex / {setting['model'] or '本机默认模型（未指定 ID）'}")
            self.allow_api_var.set(False)
            self.auto_check.configure(state="normal")
            self.api_check.configure(state="disabled")

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
            self._set_details(json.dumps(report, ensure_ascii=False, indent=2))
            self.status_text.set("环境检查完成；未发起推理。结果显示在工作台详情中。")
            messagebox.showinfo("环境检查", "离线运行环境就绪。" if report.get("ready") else "环境尚需处理：\n" + "\n".join(report.get("issues", [])), parent=self.root)
        self._submit("正在检查环境，不调用模型…", self.service.health, done)

    def _switch_home(self):
        if self.operations:
            messagebox.showinfo("稍候", "请等待当前操作完成后再切换账本。", parent=self.root)
            return
        path = filedialog.askdirectory(parent=self.root, title="选择包含 config.json 和 state.sqlite3 的账本目录", initialdir=self.home)
        if not path:
            return
        home = Path(path)
        if not (home / "config.json").is_file() or not (home / "state.sqlite3").is_file():
            messagebox.showinfo("未找到已有账本", "所选目录需要包含 config.json 和 state.sqlite3；没有移动或修改原账本。", parent=self.root)
            return
        self.home = home.resolve()
        self.location_text.set(str(self.home))
        self.service = DesktopService(self.home, self.project)
        self.current_config = None
        self.selected_task = self.selected_job = None
        self.job_signature = None
        self._submit("正在打开已有账本…", self._initial_data, self._loaded)

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
