"""Ordinary-process waiting for explicitly authorized Codex quota recovery."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time

from .config import WorkflowError, load
from .processes import detached_options, identity, same_process
from .store import Store, now


POLL_SECONDS = 300


class AutoResumeSetupError(WorkflowError):
    pass


def root_id(job):
    policy = json.loads(job["snapshot"]).get("auto_resume")
    return policy.get("root_job_id") if isinstance(policy, dict) else None


def _root(store, job_id):
    job = store.get("jobs", job_id)
    root = root_id(job)
    if not root:
        raise WorkflowError("该作业未授权自动继续；请在启动时显式指定 --auto-resume。")
    original = store.get("jobs", root)
    if original["kind"] != "codex" or root_id(original) != root or original["task_id"] != job["task_id"]:
        raise WorkflowError("自动继续授权链不一致；请人工核对。")
    return original


def _last(store, root, kind=None):
    sql = "SELECT * FROM events WHERE job_id=? AND "
    sql += "kind=?" if kind else "kind LIKE 'capacity.%'"
    rows = store.rows(sql + " ORDER BY id DESC LIMIT 1", (root, kind) if kind else (root,))
    if not rows:
        return None
    event = rows[0]
    event["payload"] = json.loads(event["payload"])
    return event


def is_enabled(store, job):
    if isinstance(job, str):
        job = store.get("jobs", job)
    root = root_id(job)
    return bool(root) and _last(store, root, "capacity.disabled") is None


def _current(store, original):
    current = original
    while True:
        rows = store.rows("SELECT * FROM jobs WHERE task_id=? AND dedupe_key=?",
                          (original["task_id"], "capacity:" + current["id"]))
        if not rows:
            return current
        following = rows[0]
        policy = json.loads(following["snapshot"]).get("auto_resume", {})
        if root_id(following) != original["id"] or policy.get("parent_job_id") != current["id"]:
            raise WorkflowError("自动继续幂等键已用于其他执行；请人工核对。")
        current = following


def _latest_is(store, job):
    latest = store.db.execute("SELECT id FROM jobs WHERE task_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                              (job["task_id"],)).fetchone()
    return latest is not None and latest["id"] == job["id"]


def _account(store, root):
    event = _last(store, root, "capacity.account")
    return event["payload"]["account_id"] if event else None


def _cycle_used(store, root, account_id, cycle):
    for event in store.rows("SELECT payload FROM events WHERE job_id=? AND kind='capacity.resumed'", (root,)):
        payload = json.loads(event["payload"])
        if payload.get("account_id") == account_id and payload.get("cycle") == cycle:
            return True
    return False


def authorize_resume(store, parent_id, account_id, cycle):
    """Called inside enqueue's transaction, immediately before persisting a child."""
    parent = store.get("jobs", parent_id)
    original = _root(store, parent_id)
    if not is_enabled(store, parent) or parent["cancel_requested"]:
        raise WorkflowError("自动继续已停止或撤销。")
    if parent["kind"] != "codex" or parent["status"] != "WAITING_CAPACITY" or not parent["session_id"]:
        raise WorkflowError("自动继续仅适用于已保存 session 的额度等待作业。")
    task = store.get("tasks", parent["task_id"])
    if task["status"] in {"DONE", "CANCELLED"} or not _latest_is(store, parent):
        raise WorkflowError("任务已结束或已人工推进，旧自动继续计划失效。")
    config = load(store.root)
    role = json.loads(parent["snapshot"])["role"]
    if config["roles"].get(role, {}).get("backend") != "codex":
        raise WorkflowError("角色已不再使用 Codex，停止自动继续。")
    if not account_id or not cycle or _account(store, original["id"]) != account_id:
        raise WorkflowError("额度账号或刷新周期无法核对，停止自动派发。")
    if _cycle_used(store, original["id"], account_id, cycle):
        raise WorkflowError("同一额度刷新周期已经自动接续，继续等待新周期。")
    if store.db.execute("SELECT id FROM jobs WHERE cwd=? AND status IN ('QUEUED','STARTING','RUNNING','UNKNOWN')",
                        (parent["cwd"],)).fetchone():
        raise WorkflowError("项目已有活动或待核对作业，不能自动派发。")
    return {"root_job_id": original["id"], "parent_job_id": parent_id,
            "account_id": account_id, "cycle": cycle}


def authorize_dispatch(store, job, profile):
    """Recheck queued automatic work inside start's claim transaction."""
    original = _root(store, job["id"])
    policy = json.loads(job["snapshot"])["auto_resume"]
    if not is_enabled(store, job) or job["cancel_requested"]:
        raise WorkflowError("自动接续授权已停止，未派发该作业。")
    task = store.get("tasks", job["task_id"])
    if task["status"] in {"DONE", "CANCELLED"} or not _latest_is(store, job):
        raise WorkflowError("任务已结束或已人工推进，未派发旧自动队列。")
    role = json.loads(job["snapshot"])["role"]
    if load(store.root)["roles"].get(role, {}).get("backend") != "codex":
        raise WorkflowError("角色已不再使用 Codex，未派发自动队列。")
    if not isinstance(profile, dict) or profile.get("allowed") is not True:
        raise WorkflowError("自动队列需要重新确认额度；请使用 job watch，不能直接 job launch。")
    if (profile.get("account_id") != policy.get("account_id") or
            _account(store, original["id"]) != profile.get("account_id") or
            profile.get("cycle") != policy.get("cycle")):
        raise WorkflowError("额度账号或刷新周期已变化，未派发旧自动队列。")
    if store.db.execute("SELECT id FROM jobs WHERE cwd=? AND id<>? AND status IN ('QUEUED','STARTING','RUNNING','UNKNOWN')",
                        (job["cwd"], job["id"])).fetchone():
        raise WorkflowError("项目已有活动或待核对作业，不能自动派发。")


def status(store, job_id):
    job = store.get("jobs", job_id)
    root = root_id(job)
    if not root:
        return {"enabled": False, "root_job_id": None, "watcher_running": False}
    event = _last(store, root)
    started = _last(store, root, "capacity.started")
    waiting = _last(store, root, "capacity.waiting")
    process = started["payload"] if started else {}
    current = _current(store, _root(store, job_id))
    waiting_payload = waiting["payload"] if waiting and waiting["payload"].get("source_job_id") == current["id"] else {}
    return {"enabled": is_enabled(store, job), "root_job_id": root,
            "current_job_id": current["id"], "current_status": current["status"],
            "watcher_running": same_process(process.get("pid"), process.get("process_token")),
            "state": event["kind"].removeprefix("capacity.") if event else "authorized",
            "reason": (event["payload"].get("reason") or waiting_payload.get("reason")) if event else "发生额度限制时会启动后台等待程序。",
            "next_check_at": waiting_payload.get("next_check_at") if current["status"] == "WAITING_CAPACITY" and is_enabled(store, job) else None,
            "log_path": str(store.root / "runs" / root / "capacity.log")}


def disable(store, job_id):
    original = _root(store, job_id)
    with store.transaction():
        if is_enabled(store, original):
            store.event("capacity.disabled", {"reason": "已显式停止后续自动继续。"},
                        task=original["task_id"], job=original["id"])
        for job in store.rows("SELECT * FROM jobs WHERE task_id=? AND status='QUEUED'", (original["task_id"],)):
            policy = json.loads(job["snapshot"]).get("auto_resume", {})
            if root_id(job) == original["id"] and policy.get("parent_job_id"):
                store.db.execute("UPDATE jobs SET status='CANCELLED',cancel_requested=1,updated_at=? WHERE id=?",
                                 (now(), job["id"]))
                store.event("job.finished", {"status": "CANCELLED", "reason": "自动继续授权已撤销。"},
                            task=job["task_id"], job=job["id"])
                if _latest_is(store, job):
                    store.db.execute("UPDATE tasks SET status='WAITING_CAPACITY',updated_at=? WHERE id=?",
                                     (now(), job["task_id"]))
    return status(store, job_id)


@contextmanager
def watcher_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        yield acquired
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def ensure_watcher(store, job_id):
    original = _root(store, job_id)
    report = status(store, job_id)
    if not report["enabled"]:
        raise WorkflowError("自动继续授权已撤销，不能重新启动等待程序。")
    if report["watcher_running"]:
        return {"root_job_id": original["id"], "launched": False, "watcher_running": True}
    folder = store.root / "runs" / original["id"]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUTF8"] = "1"
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "capacity.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-m", "ccaw.capacity", "--home", str(store.root), original["id"]],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **detached_options())
        Thread(target=process.wait, daemon=True).start()
    except OSError:
        store.event("capacity.error", {"reason": "无法启动额度等待程序；可用 job watch 显式重启。"},
                    task=original["task_id"], job=original["id"])
        return {"root_job_id": original["id"], "launched": False, "watcher_running": False}
    return {"root_job_id": original["id"], "launched": True, "pid": process.pid}


def read_capacity(config):
    from .capacity_api import read_capacity as read
    return read(config)


def bind_execution(store, job, config):
    """Bind an opted-in initial attempt before its first model process starts."""
    policy = json.loads(job["snapshot"]).get("auto_resume", {})
    if not policy or policy.get("parent_job_id") or not is_enabled(store, job):
        return
    try:
        profile = read_capacity(config)
    except WorkflowError:
        raise AutoResumeSetupError("自动继续账号预检未完成，未启动模型；请检查 Codex 接口，或不带 --auto-resume 手动执行。") from None
    if not profile.get("account_id"):
        raise AutoResumeSetupError("无法确认自动继续所属账号，未启动模型；请检查 Codex 接口。")
    with store.transaction():
        if not is_enabled(store, job):
            return
        bound = _account(store, policy["root_job_id"])
        if bound is not None and bound != profile["account_id"]:
            raise AutoResumeSetupError("自动继续所属账号已变化，未启动模型。")
        if bound is None:
            store.event("capacity.account", {"account_id": profile["account_id"]},
                        task=job["task_id"], job=policy["root_job_id"])
        store.event("capacity.waiting", {"source_job_id": job["id"],
                    "next_check_at": max(time.time() + POLL_SECONDS, profile["next_check_at"]),
                    "reason": "已确认原作业账号；若发生额度限制，将按检查期限等待。"},
                    task=job["task_id"], job=policy["root_job_id"])


def _stop(store, original, reason):
    last = _last(store, original["id"])
    if not last or last["kind"] != "capacity.stopped" or last["payload"].get("reason") != reason:
        store.event("capacity.stopped", {"reason": reason}, task=original["task_id"], job=original["id"])
    return {"done": True, "reason": reason, "next_check_at": None}


def _stale_queue(store, original, current, reason):
    if current["status"] == "QUEUED":
        disable(store, original["id"])
        reason += "旧自动队列已撤销，请人工核对后另建接续。"
    return _stop(store, original, reason)


def _waiting(store, original, current, at, reason):
    store.event("capacity.waiting", {"source_job_id": current["id"], "next_check_at": at, "reason": reason},
                task=original["task_id"], job=original["id"])
    return {"done": False, "reason": reason, "next_check_at": at}


def tick(store, job_id, at=None):
    """One recoverable scheduling step. Waiting never invokes a model."""
    from .runner import enqueue, reconcile, start
    at = time.time() if at is None else at
    original = _root(store, job_id)
    if not is_enabled(store, original):
        return _stop(store, original, "自动继续授权已撤销。")
    current = _current(store, original)
    task = store.get("tasks", original["task_id"])
    if task["status"] in {"DONE", "CANCELLED"} or not _latest_is(store, current):
        return _stop(store, original, "任务已结束或已人工推进，停止旧等待。")
    if current["cancel_requested"]:
        return _stop(store, original, "作业已请求取消，停止自动继续。")
    if current["status"] in {"STARTING", "RUNNING"}:
        current = reconcile(store, current["id"])
        if current["status"] in {"STARTING", "RUNNING"}:
            return {"done": False, "next_check_at": at + 5, "reason": "实际作业正在执行。"}
    queued = current["status"] == "QUEUED"
    if queued:
        policy = json.loads(current["snapshot"])["auto_resume"]
        if not policy.get("parent_job_id"):
            return _stop(store, original, "原作业尚未启动；请先核对并使用 job launch。")
    if current["status"] != "WAITING_CAPACITY" and not queued:
        return _stop(store, original, "作业已结束或需要人工核对，停止自动继续。")
    if current["kind"] != "codex" or not current["session_id"]:
        return _stop(store, original, "缺少可核对的 Codex session，不能自动继续。")
    config = load(store.root)
    role = json.loads(current["snapshot"])["role"]
    if config["roles"].get(role, {}).get("backend") != "codex":
        return _stale_queue(store, original, current, "角色已不再使用 Codex，停止自动继续。")
    active = store.rows("SELECT id,status FROM jobs WHERE cwd=? AND id<>? AND status IN ('QUEUED','STARTING','RUNNING','UNKNOWN')", (current["cwd"], current["id"]))
    if any(job["status"] == "UNKNOWN" for job in active):
        return _stop(store, original, "项目有状态未知的作业，必须先人工核对。")
    if active:
        return {"done": False, "next_check_at": at + 5, "reason": "项目有其他活动作业，继续等待。"}
    waiting = _last(store, original["id"], "capacity.waiting")
    if waiting and waiting["payload"]["next_check_at"] > at:
        return {"done": False, **waiting["payload"]}
    try:
        profile = read_capacity(config)
    except WorkflowError:
        return _waiting(store, original, current, at + POLL_SECONDS, "额度查询未成功，保留等待；未调用模型。")
    next_check = max(at + POLL_SECONDS, profile["next_check_at"])
    account_id, cycle = profile.get("account_id"), profile.get("cycle")
    if account_id:
        with store.transaction():
            if not is_enabled(store, original):
                return _stop(store, original, "自动继续授权已撤销。")
            bound = _account(store, original["id"])
            changed_account = bound is not None and bound != account_id
            if bound is None:
                store.event("capacity.account", {"account_id": account_id}, task=original["task_id"], job=original["id"])
        if changed_account:
            return _stale_queue(store, original, current, "额度账号已改变，停止自动继续并等待人工核对。")
    if queued and account_id and cycle and (account_id != policy.get("account_id") or cycle != policy.get("cycle")):
        return _stale_queue(store, original, current, "额度账号或刷新周期已变化，不能使用旧队列许可。")
    if profile.get("allowed") is not True or not account_id or not cycle:
        return _waiting(store, original, current, next_check, profile["reason"])
    if queued:
        _waiting(store, original, current, next_check, "额度与原队列许可一致，核对状态后派发。")
        try:
            start(store, current["id"], capacity_profile=profile)
        except WorkflowError:
            return _waiting(store, original, current, next_check, "派发前状态或授权已变化，保留等待并重新核对。")
        return {"done": False, "next_check_at": at + 5, "reason": "已派发持久化的自动接续。"}
    if _cycle_used(store, original["id"], account_id, cycle):
        return _waiting(store, original, current, next_check, "同一刷新周期已尝试一次，继续等待新的明确额度信号。")
    _waiting(store, original, current, next_check, "额度明确可用，核对状态后接续原 session。")
    try:
        instruction = json.loads(original["snapshot"])["auto_resume"].get("instruction", original["prompt"])
        following, _ = enqueue(store, current["task_id"], "codex", role=role, key="capacity:" + current["id"],
                               prompt=instruction + "\n从已完成进度继续；先核对现有作业与成果，不重复运行已完成计算。",
                               resume_job=current["id"], auto_resume=True, capacity_parent=current["id"],
                               capacity_cycle=cycle, capacity_account=account_id)
        if following["status"] == "QUEUED":
            start(store, following["id"], capacity_profile=profile)
    except WorkflowError:
        return _waiting(store, original, current, next_check, "派发前状态或授权已变化，保留等待并重新核对。")
    return {"done": False, "next_check_at": at + 5, "reason": "已接续原 Codex session。"}


def watch(home, job_id):
    store = Store(home)
    try:
        original = _root(store, job_id)
        with watcher_lock(store.root / "runs" / original["id"] / "capacity.lock") as acquired:
            if not acquired:
                return 0
            store.event("capacity.started", {"pid": os.getpid(), "process_token": identity(os.getpid())},
                        task=original["task_id"], job=original["id"])
            while True:
                result = tick(store, original["id"])
                if result["done"]:
                    return 0
                time.sleep(max(0, min(5, result["next_check_at"] - time.time())))
    except Exception as exc:
        if "original" in locals():
            store.event("capacity.error", {"reason": f"等待程序异常停止（{type(exc).__name__}）；请核对后使用 job watch 重启。"},
                        task=original["task_id"], job=original["id"])
        print(f"额度等待程序已停止（{type(exc).__name__}）；请查看作业状态后用 job watch 重新启动。", file=sys.stderr)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CCAW 普通后台额度等待程序")
    parser.add_argument("--home", required=True)
    parser.add_argument("job_id")
    arguments = parser.parse_args()
    raise SystemExit(watch(arguments.home, arguments.job_id))
