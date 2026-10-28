"""Persist intent before detached execution; uncertain runs are never restarted."""
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time

from . import __version__
from .capacity import AutoResumeSetupError
from .config import WorkflowError, canonical, fingerprint, load
from .context import render_packet, task_packet
from .processes import detached_options, identity, same_process, terminate
from .providers import APIExecutionUncertain, api_reservation, call_api, classify_error, codex_command, subscription_environment
from .store import Store, now, uid


ACTIVE = ("QUEUED", "STARTING", "RUNNING", "UNKNOWN")
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "WAITING_CAPACITY")


def enqueue(store, task_id, kind, command=None, prompt="", role=None, key="main", resume_job=None,
            then_prompt=None, then_role="default", allow_api=False, auto_resume=False,
            capacity_parent=None, capacity_cycle=None, capacity_account=None):
    task = store.get("tasks", task_id)
    config = load(store.root)
    packet = task_packet(store, task_id)
    full_prompt = render_packet(packet) + "\n## 本次执行指令\n" + prompt if kind != "script" else None
    role = role or task["role"]
    session = None
    if resume_job:
        previous = store.get("jobs", resume_job)
        if previous["task_id"] != task_id or previous["kind"] != "codex" or not previous["session_id"]:
            raise WorkflowError("接续需要同任务内已保存的 Codex session ID。")
        if previous["status"] not in TERMINAL:
            raise WorkflowError("旧作业仍在运行或状态未知；先核对状态。")
        session = previous["session_id"]
    if kind in {"codex", "api"}:
        if task["kind"] == "chat":
            raise WorkflowError("基础对话使用 ChatGPT 免费网页入口；请交接任务包，不由其他模型或付费 API 执行。")
        if role not in config["roles"]:
            raise WorkflowError("请明确选择 economy、default 或 expert 角色。")
        kind = config["roles"][role]["backend"]
        if resume_job and kind != "codex":
            raise WorkflowError("Codex session 不能通过文本 API 恢复；可另建带任务包的执行尝试。")
    if kind == "script":
        if not command:
            raise WorkflowError("脚本命令不能为空；使用 -- 分隔，不通过 shell 拼接。")
        command = list(command)
    elif kind not in {"codex", "api"}:
        raise WorkflowError("不支持的执行方式。")
    if kind == "api" and not allow_api:
        raise WorkflowError("API 会把任务包发送到配置的服务；需显式指定 --allow-api。")
    if (auto_resume or capacity_parent) and kind != "codex":
        raise WorkflowError("额度自动接续仅支持 Codex 订阅作业。")
    if capacity_parent and resume_job != capacity_parent:
        raise WorkflowError("自动接续必须使用原作业保存的 session。")
    if then_prompt and then_role not in config["roles"]:
        raise WorkflowError("接续角色无效。")
    if then_prompt and config["roles"][then_role]["backend"] != "codex":
        raise WorkflowError("自动接续目前仅支持 Codex；付费 API 需独立明确授权。")
    snapshot = {"config": config, "config_hash": fingerprint(config), "version": __version__,
                "role": role, "lesson_versions": [{"id": x["id"], "version": x["version"]} for x in packet["lessons"]]}
    specification = {"kind": kind, "command": command or [], "cwd": task["project"], "prompt": prompt,
                     "role": role, "session": session, "then_prompt": then_prompt, "then_role": then_role}
    if capacity_parent:
        specification["auto_resume"] = {"parent_job_id": capacity_parent, "cycle": capacity_cycle,
                                          "account_id": capacity_account}
    elif auto_resume:
        specification["auto_resume"] = True
    snapshot["spec_hash"] = fingerprint(specification)
    identity_, timestamp = uid("job"), now()
    if auto_resume and not capacity_parent:
        snapshot["auto_resume"] = {"root_job_id": identity_, "instruction": prompt}
    with store.transaction():
        old = store.db.execute("SELECT * FROM jobs WHERE task_id=? AND dedupe_key=?", (task_id, key)).fetchone()
        if old:
            old = dict(old)
            if json.loads(old["snapshot"])["spec_hash"] != snapshot["spec_hash"]:
                raise WorkflowError("幂等键已用于不同命令或上下文；核对旧作业，明确新阶段后使用新的 --key。")
            return old, False
        if capacity_parent:
            from .capacity import authorize_resume
            snapshot["auto_resume"] = authorize_resume(store, capacity_parent, capacity_account, capacity_cycle)
        active = store.db.execute("SELECT id FROM jobs WHERE cwd=? AND status IN ('QUEUED','STARTING','RUNNING','UNKNOWN')", (task["project"],)).fetchone()
        if active:
            raise WorkflowError(f"项目已有未完成或待核对作业 {active['id']}；当前版本串行执行同项目。")
        store.db.execute("INSERT INTO jobs(id,task_id,dedupe_key,kind,command,cwd,status,snapshot,prompt,session_id,continuation_prompt,continuation_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (identity_, task_id, key, kind, canonical(command or []), task["project"], "QUEUED",
                          canonical(snapshot), full_prompt, session, then_prompt, then_role, timestamp, timestamp))
        if kind == "api":
            reserve = api_reservation(config, full_prompt)
            period = timestamp[:7]
            rows = store.rows("SELECT reserved_usd,actual_usd FROM reservations WHERE period=?", (period,))
            used = sum((Decimal(x["actual_usd"] if x["actual_usd"] is not None else x["reserved_usd"]) for x in rows), Decimal(0))
            if used + reserve > Decimal(str(config["paid_api"]["monthly_limit_usd"])):
                raise WorkflowError("本地月度 API 预算不足，未发起请求。")
            store.db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?)", (uid("cost"), identity_, period, str(reserve), None, "RESERVED", timestamp))
        store.db.execute("UPDATE tasks SET status='WAITING_JOB',updated_at=? WHERE id=?", (timestamp, task_id))
        store.event("job.queued", {"key": key, "config_hash": snapshot["config_hash"]}, task=task_id, job=identity_)
        if capacity_parent:
            store.event("capacity.resumed", {"parent_job_id": capacity_parent, "job_id": identity_,
                                            "account_id": capacity_account, "cycle": capacity_cycle},
                        task=task_id, job=snapshot["auto_resume"]["root_job_id"])
    return store.get("jobs", identity_), True


def start(store, job_id, *, capacity_profile=None):
    with store.transaction():
        job = store.get("jobs", job_id)
        if job["status"] != "QUEUED":
            return job
        if json.loads(job["snapshot"]).get("auto_resume", {}).get("parent_job_id"):
            from .capacity import authorize_dispatch
            authorize_dispatch(store, job, capacity_profile)
        store.db.execute("UPDATE jobs SET status='STARTING',updated_at=? WHERE id=?", (now(), job_id))
        store.event("job.starting", task=job["task_id"], job=job_id)
    child = None
    try:
        folder = store.root / "runs" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        source_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUTF8"] = "1"
        if getattr(sys, "frozen", False):
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        with (folder / "worker.log").open("ab") as log:
            child = subprocess.Popen([sys.executable, "-m", "ccaw.worker", "--home", str(store.root), job_id],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **detached_options())
        token = identity(child.pid)
        store.db.execute("UPDATE jobs SET pid=?,process_token=COALESCE(process_token,?),updated_at=? WHERE id=? AND status IN ('STARTING','RUNNING')",
                         (child.pid, token, now(), job_id))
    except OSError as exc:
        if child is None:
            finish(store, job_id, "FAILED", None, "START_FAILURE", str(exc))
        else:
            with store.transaction():
                current = store.get("jobs", job_id)
                if current["status"] not in TERMINAL:
                    store.db.execute("UPDATE jobs SET status='UNKNOWN',pid=COALESCE(pid,?),error_kind='START_STATE_UNKNOWN',updated_at=? WHERE id=?",
                                     (child.pid, now(), job_id))
                    store.db.execute("UPDATE tasks SET status='WAITING_INPUT',updated_at=? WHERE id=?", (now(), current["task_id"]))
                    store.event("job.unknown", {"error_kind": "START_STATE_UNKNOWN", "detail": type(exc).__name__}, task=current["task_id"], job=job_id)
    finally:
        if child is not None:
            # Reap even when post-launch bookkeeping fails; never keep the launcher alive.
            Thread(target=child.wait, daemon=True).start()
    return store.get("jobs", job_id)


def finish(store, job_id, status, exit_code, error_kind=None, detail=None):
    with store.transaction():
        job = store.get("jobs", job_id)
        if job["status"] in TERMINAL:
            return False
        store.db.execute("UPDATE jobs SET status=?,exit_code=?,error_kind=?,updated_at=? WHERE id=?",
                         (status, exit_code, error_kind, now(), job_id))
        task_status = {"SUCCEEDED": "VERIFYING", "FAILED": "FAILED", "CANCELLED": "CANCELLED", "WAITING_CAPACITY": "WAITING_CAPACITY"}[status]
        store.db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (task_status, now(), job["task_id"]))
        store.event("job.finished", {"status": status, "exit_code": exit_code, "error_kind": error_kind, "detail": detail}, task=job["task_id"], job=job_id)
    return True


def continuation(store, job_id):
    job = store.get("jobs", job_id)
    if job["status"] != "SUCCEEDED" or not job["continuation_prompt"]:
        return None
    if job["continuation_job"]:
        following = store.get("jobs", job["continuation_job"])
        if following["status"] == "QUEUED":
            start(store, following["id"])
        return following["id"]
    try:
        following, created = enqueue(store, job["task_id"], "codex", role=job["continuation_role"],
                                     key="after:" + job_id,
                                     prompt=job["continuation_prompt"] + f"\n已成功完成的作业: {job_id}\n结果目录: {store.root / 'runs' / job_id}")
        store.db.execute("UPDATE jobs SET continuation_job=? WHERE id=?", (following["id"], job_id))
        # QUEUED is safe to claim even after a crash between enqueue and start.
        if following["status"] == "QUEUED":
            start(store, following["id"])
        return following["id"]
    except WorkflowError as exc:
        store.event("continuation.blocked", {"reason": str(exc)}, task=job["task_id"], job=job_id)
        return None


def reconcile(store, job_id):
    job = store.get("jobs", job_id)
    if job["status"] in {"STARTING", "RUNNING"}:
        if job["status"] == "STARTING" and not job["pid"]:
            from datetime import datetime, timezone
            if (datetime.now(timezone.utc) - datetime.fromisoformat(job["updated_at"])).total_seconds() < 10:
                return job
        supervisor = same_process(job["pid"], job["process_token"])
        if not supervisor:
            # A child can still be alive. UNKNOWN deliberately blocks relaunch.
            with store.transaction():
                changed = store.db.execute("UPDATE jobs SET status='UNKNOWN',updated_at=? WHERE id=? AND status IN ('STARTING','RUNNING') AND pid IS ? AND process_token IS ?",
                                           (now(), job_id, job["pid"], job["process_token"])).rowcount
                if changed:
                    store.db.execute("UPDATE tasks SET status='WAITING_INPUT',updated_at=? WHERE id=?", (now(), job["task_id"]))
                    store.event("job.unknown", {"child_alive": same_process(job["child_pid"], job["child_token"])}, task=job["task_id"], job=job_id)
    return store.get("jobs", job_id)


def cancel(store, job_id):
    job = store.get("jobs", job_id)
    if json.loads(job["snapshot"]).get("auto_resume"):
        from .capacity import disable
        disable(store, job_id)
        job = store.get("jobs", job_id)
    if job["status"] in TERMINAL:
        return job
    store.db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
    if job["status"] == "QUEUED":
        finish(store, job_id, "CANCELLED", None)
    elif job["child_pid"] and same_process(job["child_pid"], job["child_token"]):
        terminate(job["child_pid"], job["child_token"])
        if not same_process(job["pid"], job["process_token"]):
            finish(store, job_id, "CANCELLED", None)
    elif job["status"] == "UNKNOWN":
        raise WorkflowError("作业状态未知且无法确认子进程，需人工核对后 job resolve。")
    return store.get("jobs", job_id)


def run_worker(root, job_id):
    store = Store(root)
    child = None
    try:
        with store.transaction():
            job = store.get("jobs", job_id)
            if job["status"] != "STARTING":
                return
            store.db.execute("UPDATE jobs SET status='RUNNING',pid=?,process_token=?,updated_at=? WHERE id=?",
                             (os.getpid(), identity(os.getpid()), now(), job_id))
        job = store.get("jobs", job_id)
        folder = store.root / "runs" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        snapshot = json.loads(job["snapshot"])
        config = snapshot["config"]
        from .config import atomic_write
        atomic_write(folder / "execution.json", canonical({"job_id": job_id, "snapshot": snapshot}) + "\n")
        if job["prompt"]:
            atomic_write(folder / "prompt.md", job["prompt"])
        if store.get("jobs", job_id)["cancel_requested"]:
            finish(store, job_id, "CANCELLED", None)
            return
        if job["kind"] == "api":
            current = load(store.root)
            if not current["paid_api"]["enabled"] or current["paid_api"] != config["paid_api"]:
                raise WorkflowError("API 授权或配置已变更，派发前停止；请重新核对任务。")
            content, actual, usage, completed = call_api(config, job["prompt"])
            atomic_write(folder / "result.md", content)
            store.db.execute("UPDATE reservations SET actual_usd=?,status=? WHERE job_id=?",
                             (str(actual) if actual is not None else None, "SETTLED" if actual is not None else "UNCERTAIN", job_id))
            store.event("api.usage", usage, task=job["task_id"], job=job_id)
            status = "CANCELLED" if store.get("jobs", job_id)["cancel_requested"] else ("SUCCEEDED" if completed else "FAILED")
            finish(store, job_id, status, 0, None if completed else "INCOMPLETE_OUTPUT")
            return
        if job["kind"] == "codex":
            # Current revocation can tighten the sandbox of a pinned attempt.
            if load(store.root)["codex"]["sandbox"] == "read-only":
                config["codex"]["sandbox"] = "read-only"
            if snapshot.get("auto_resume"):
                from .capacity import bind_execution
                bind_execution(store, job, config)
            command = codex_command(config, snapshot["role"], folder / "result.md", job["session_id"])
            child_env = subscription_environment(command[0])
        else:
            command = json.loads(job["command"])
            child_env = None
        with (folder / "stdout.log").open("wb") as stdout, (folder / "stderr.log").open("wb") as stderr:
            child = subprocess.Popen(command, cwd=job["cwd"],
                                     stdin=subprocess.PIPE if job["kind"] == "codex" else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE if job["kind"] == "codex" else stdout, stderr=stderr, env=child_env, **detached_options())
            store.db.execute("UPDATE jobs SET child_pid=?,child_token=?,updated_at=? WHERE id=?",
                             (child.pid, identity(child.pid), now(), job_id))
            if store.get("jobs", job_id)["cancel_requested"] and child.poll() is None:
                token = identity(child.pid)
                if token:
                    terminate(child.pid, token)
            if job["kind"] == "codex":
                try:
                    child.stdin.write(job["prompt"].encode("utf-8"))
                    child.stdin.close()
                except BrokenPipeError:
                    pass
                for line in child.stdout:
                    stdout.write(line)
                    stdout.flush()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "thread.started" and event.get("thread_id"):
                        store.db.execute("UPDATE jobs SET session_id=? WHERE id=?", (event["thread_id"], job_id))
            # Ordinary blocking process wait: no inference, status polling or log narration.
            code = child.wait()
        current = store.get("jobs", job_id)
        if current["cancel_requested"]:
            finish(store, job_id, "CANCELLED", code)
            return
        success, error_text = code == 0, ""
        if job["kind"] == "codex":
            completed = False
            with (folder / "stdout.log").open(encoding="utf-8", errors="replace") as events:
                for line in events:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "thread.started" and event.get("thread_id"):
                        store.db.execute("UPDATE jobs SET session_id=? WHERE id=?", (event["thread_id"], job_id))
                    if event.get("type") == "turn.completed":
                        completed = True
                        store.event("codex.usage", event.get("usage", {}), task=job["task_id"], job=job_id)
                    if event.get("type") in {"error", "turn.failed"}:
                        error_text += canonical(event)[-12000:]
            success = success and completed and not error_text
        if not success:
            with (folder / "stderr.log").open("rb") as source:
                source.seek(max(0, source.seek(0, 2) - 12000))
                error_text += source.read().decode("utf-8", errors="replace")
        category = None if success else classify_error(error_text)
        finish(store, job_id, "SUCCEEDED" if success else ("WAITING_CAPACITY" if category == "CAPACITY" else "FAILED"), code, category)
        if category == "CAPACITY" and snapshot.get("auto_resume"):
            from .capacity import ensure_watcher
            ensure_watcher(store, job_id)
        if success:
            continuation(store, job_id)
    except Exception as exc:
        # Persist only safe diagnostics; API exceptions never include response bodies or keys.
        category = "AUTO_RESUME_SETUP" if isinstance(exc, AutoResumeSetupError) else (classify_error(str(exc)) if isinstance(exc, WorkflowError) else "WORKER")
        detail = str(exc) if isinstance(exc, WorkflowError) else type(exc).__name__
        uncertain = isinstance(exc, APIExecutionUncertain)
        if child is not None:
            try:
                uncertain = uncertain or child.poll() is None
            except OSError:
                uncertain = True
        if uncertain:
            with store.transaction():
                current = store.get("jobs", job_id)
                if current["status"] not in TERMINAL:
                    store.db.execute("UPDATE jobs SET status='UNKNOWN',child_pid=COALESCE(child_pid,?),error_kind=?,updated_at=? WHERE id=?",
                                     (child.pid if child is not None else None, category, now(), job_id))
                    store.db.execute("UPDATE tasks SET status='WAITING_INPUT',updated_at=? WHERE id=?", (now(), current["task_id"]))
                    if isinstance(exc, APIExecutionUncertain):
                        store.db.execute("UPDATE reservations SET status='UNCERTAIN' WHERE job_id=?", (job_id,))
                    store.event("job.unknown", {"error_kind": category, "detail": detail}, task=current["task_id"], job=job_id)
        else:
            finish(store, job_id, "WAITING_CAPACITY" if category == "CAPACITY" else "FAILED", None, category, detail)
            if category == "CAPACITY" and json.loads(store.get("jobs", job_id)["snapshot"]).get("auto_resume"):
                from .capacity import ensure_watcher
                ensure_watcher(store, job_id)
    finally:
        if child is not None:
            for stream in (child.stdin, child.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        store.close()


def wait_for(store, job_id, timeout=None):
    started = time.monotonic()
    while True:
        job = reconcile(store, job_id)
        if job["status"] in TERMINAL or job["status"] == "UNKNOWN":
            return job
        if timeout is not None and time.monotonic() - started >= timeout:
            return job
        time.sleep(0.25)
