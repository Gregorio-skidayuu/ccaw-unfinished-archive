"""Local desktop operations over the existing workflow ledger and runner."""
import copy
from contextlib import closing
import os
from pathlib import Path

from . import capacity
from .cli import doctor, job_details
from .config import DEFAULT_CONFIG, WorkflowError, atomic_write, fingerprint, load, route, save_version, validate
from .context import render_packet, task_packet
from .runner import cancel, continuation, enqueue, reconcile, start
from .store import Store


class DesktopService:
    def __init__(self, home, project=None):
        self.home = Path(home).resolve()
        self.project = Path(project or Path.cwd()).resolve()

    def ensure_initialized(self):
        self.home.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.home.chmod(0o700)
        atomic_write(self.home / ".gitignore", "*\n!.gitignore\n")
        if not (self.home / "config.json").exists():
            save_version(self.home, copy.deepcopy(DEFAULT_CONFIG))
        load(self.home)
        with closing(Store.initialize(self.home)):
            pass
        return {"home": str(self.home), "project": str(self.project)}

    def snapshot(self):
        with closing(Store(self.home)) as store:
            for job in store.rows("SELECT id FROM jobs WHERE status IN ('STARTING','RUNNING')"):
                reconcile(store, job["id"])
            tasks = store.rows("SELECT * FROM tasks ORDER BY created_at DESC,rowid DESC")
            jobs = store.rows("SELECT id,task_id,kind,cwd,status,session_id,exit_code,error_kind,created_at,updated_at FROM jobs ORDER BY created_at DESC,rowid DESC")
        config = load(self.home)
        return {"tasks": tasks, "jobs": jobs, "config": config, "config_hash": fingerprint(config),
                "home": str(self.home), "project": str(self.project)}

    def create_task(self, title, goal, acceptance, project, kind="code", risk="medium", complexity="medium", tags=None):
        if not isinstance(title, str) or not title.strip():
            raise WorkflowError("请填写任务名称。")
        if not isinstance(acceptance, str) or not acceptance.strip():
            raise WorkflowError("请填写可观察的验收条件。")
        if kind not in {"code", "script", "chat"} or risk not in {"low", "medium", "high"} or complexity not in {"low", "medium", "high"}:
            raise WorkflowError("任务类型、风险或复杂度无效。")
        decision = route(kind, risk, complexity)
        with closing(Store(self.home)) as store:
            return store.create_task(title.strip(), goal or title.strip(), acceptance.strip(), project,
                                     tags or [], kind, risk, complexity, decision["role"])

    def task_details(self, task_id):
        with closing(Store(self.home)) as store:
            return task_packet(store, task_id)

    def job_details(self, job_id):
        with closing(Store(self.home)) as store:
            return job_details(store, job_id)

    def start_agent(self, task_id, role, prompt, key, auto_resume=False, allow_api=False):
        if not isinstance(key, str) or not key.strip():
            raise WorkflowError("请填写本次执行的阶段键；同阶段使用同一键可避免重复启动。")
        with closing(Store(self.home)) as store:
            task = store.get("tasks", task_id)
            if task["kind"] == "chat":
                raise WorkflowError("基础对话使用 ChatGPT 免费网页入口；请复制任务包并在 ChatGPT 中继续。")
            job, _ = enqueue(store, task_id, "codex", role=role, prompt=prompt, key=key.strip(),
                             auto_resume=auto_resume, allow_api=allow_api)
            return start(store, job["id"]) if job["status"] == "QUEUED" else job

    def job_action(self, job_id, action):
        with closing(Store(self.home)) as store:
            store.get("jobs", job_id)
            if action == "cancel":
                return cancel(store, job_id)
            if action == "watch":
                launch = capacity.ensure_watcher(store, job_id)
                report = capacity.status(store, job_id)
                report["launch"] = launch
                report["watcher_starting"] = bool(launch.get("launched") and not report["watcher_running"])
                if not launch.get("launched") and not launch.get("watcher_running"):
                    raise WorkflowError("后台等待程序未能启动；请查看作业中的等待原因和日志。")
                return report
            if action == "unwatch":
                return capacity.disable(store, job_id)
            if action == "continue":
                return {"continuation_job": continuation(store, job_id)}
            if action == "launch":
                job = store.get("jobs", job_id)
                if job["status"] != "QUEUED":
                    raise WorkflowError("仅可派发尚未启动的 QUEUED 作业；不能重启未知作业。")
                return start(store, job_id)
            raise WorkflowError("不支持该作业操作。")

    def configuration(self):
        config = load(self.home)
        return {"config": config, "hash": fingerprint(config),
                "key_present": bool(os.environ.get(config["paid_api"]["key_env"]))}

    def save_configuration(self, config_dict, expected_hash, api_key=None):
        candidate = copy.deepcopy(config_dict)
        validate(candidate)
        current = load(self.home)
        if fingerprint(current) != expected_hash:
            raise WorkflowError("配置已被其他操作更新；请重新载入后再保存，避免覆盖新配置。")
        if api_key is not None and not isinstance(api_key, str):
            raise WorkflowError("API 密钥必须是文本，仅用于当前会话。")
        candidate["revision"] = current["revision"] + 1
        save_version(self.home, candidate)
        if api_key:
            os.environ[candidate["paid_api"]["key_env"]] = api_key
        return self.configuration()

    def chat_handoff(self, task_id):
        with closing(Store(self.home)) as store:
            packet = task_packet(store, task_id)
            store.event("task.handoff", {"channel": "chatgpt_free_web"}, task=task_id)
        return {"url": "https://chatgpt.com/", "text": render_packet(packet), "channel": "chatgpt_free_web"}

    def health(self):
        return doctor(self.home)
