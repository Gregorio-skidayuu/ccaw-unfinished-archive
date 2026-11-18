"""UTF-8 request/reply bridge for the local Windows desktop."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from .config import WorkflowError, atomic_write
from .desktop import DesktopService
from .store import Store


MAX_REQUEST_BYTES = 1024 * 1024
MAX_LOG_BYTES = 128 * 1024
LOG_NAMES = frozenset(("stdout.log", "stderr.log", "worker.log", "result.md", "capacity.log"))
JOB_FIELDS = (
    "id", "task_id", "kind", "cwd", "status", "dedupe_key", "session_id", "exit_code",
    "error_kind", "created_at", "updated_at", "pid", "process_token", "child_pid",
    "child_token", "cancel_requested", "continuation_job",
)
FIELDS = {
    "snapshot": {},
    "select": {"task_id": str},
    "create_task": {"title": str, "goal": str, "acceptance": str, "project": str, "kind": str},
    "start_script": {"task_id": str, "command": list, "key": str},
    "start_agent": {"task_id": str, "role": str, "prompt": str, "key": str},
    "task_details": {"task_id": str},
    "job_details": {"job_id": str},
    "read_log": {"job_id": str, "name": str},
    "accept_task": {"task_id": str, "job_id": str, "evidence": str},
    "job_action": {"job_id": str, "action": str},
    "folder_path": {"job_id": str, "destination": str},
}


def _public_job(job):
    return {name: job[name] for name in JOB_FIELDS if name in job}


class DesktopBridge:
    def __init__(self, home, project):
        self.service = DesktopService(home, project)
        self.selection = self.service.home / "desktop-selection.txt"
        self.initialized = False
        self.operations = {
            "snapshot": self.snapshot,
            "select": self.select,
            "create_task": self.service.create_task,
            "start_script": self.start_script,
            "start_agent": self.start_agent,
            "task_details": self.task_details,
            "job_details": self.job_details,
            "read_log": self.read_log,
            "accept_task": self.service.accept_task,
            "job_action": self.job_action,
            "folder_path": self.folder_path,
        }

    def call(self, op, args):
        if not isinstance(op, str) or op not in self.operations:
            raise WorkflowError("不支持的桌面操作。")
        if not isinstance(args, dict):
            raise WorkflowError("桌面操作参数必须是对象。")
        fields = FIELDS[op]
        optional = {"auto_resume": bool} if op == "start_agent" else {}
        permitted = fields | optional
        if fields.keys() - args.keys() or args.keys() - permitted.keys():
            raise WorkflowError("桌面操作参数缺失或包含未授权字段。")
        for name, value in args.items():
            if type(value) is not permitted[name]:
                raise WorkflowError(f"参数类型无效：{name}。")
            strings = value if isinstance(value, list) else (value,)
            if any(isinstance(text, str) and "\0" in text for text in strings):
                raise WorkflowError(f"参数包含无效字符：{name}。")
        if not self.initialized:
            self.service.ensure_initialized()
            self.initialized = True
        return self.operations[op](**args)

    def snapshot(self):
        snapshot = self.service.snapshot()
        result = {name: snapshot[name] for name in ("tasks", "jobs", "home", "project")}
        try:
            selection = self.selection.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, UnicodeDecodeError):
            selection = None
        result["selected_task"] = selection if any(task["id"] == selection for task in result["tasks"]) else None
        return result

    def select(self, task_id):
        with closing(Store(self.service.home)) as store:
            store.get("tasks", task_id)
        atomic_write(self.selection, task_id + "\n")
        return {"selected_task": task_id}

    def start_script(self, task_id, command, key):
        return _public_job(self.service.start_script(task_id, command, key))

    def start_agent(self, task_id, role, prompt, key, auto_resume=False):
        return _public_job(self.service.start_agent(task_id, role, prompt, key, auto_resume=auto_resume))

    def task_details(self, task_id):
        packet = self.service.task_details(task_id)
        return {name: packet[name] for name in ("task", "acceptance_event")}

    def job_details(self, job_id):
        job = self.service.job_details(job_id)
        diagnostic = job["diagnostics"]
        result = _public_job(job)
        result["diagnostics"] = {
            "run_directory": diagnostic["run_directory"],
            "next_action": diagnostic["next_action"],
            "files": {name: path for name, path in diagnostic["files"].items() if name in LOG_NAMES},
            "recent_events": diagnostic["recent_events"],
        }
        if "auto_resume" in diagnostic:
            result["diagnostics"]["auto_resume"] = diagnostic["auto_resume"]
        return result

    def read_log(self, job_id, name):
        if name not in LOG_NAMES:
            raise WorkflowError("无法在桌面中读取该文件。")
        self._job(job_id)
        path = self._run_dir(job_id) / name
        if path.resolve() != path or not path.is_file():
            raise WorkflowError("尚未找到可读的作业日志。")
        with path.open("rb") as source:
            content = source.read(MAX_LOG_BYTES + 1)
        return {
            "text": content[:MAX_LOG_BYTES].decode("utf-8-sig", errors="replace"),
            "path": str(path),
            "truncated": len(content) > MAX_LOG_BYTES,
        }

    def job_action(self, job_id, action):
        if action not in {"launch", "cancel", "watch", "unwatch", "continue"}:
            raise WorkflowError("不支持该作业操作。")
        if action in {"launch", "watch", "continue"}:
            job = self._job(job_id)
            if job["kind"] == "api" or (job["continuation_job"] and action == "continue"
                                         and self._job(job["continuation_job"])["kind"] == "api"):
                raise WorkflowError("桌面入口不派发付费 API 作业。")
        result = self.service.job_action(job_id, action)
        return _public_job(result) if action in {"launch", "cancel"} else result

    def folder_path(self, job_id, destination):
        job = self._job(job_id)
        if destination == "project":
            folder = Path(job["cwd"]).resolve()
        elif destination == "run":
            folder = self._run_dir(job_id)
        else:
            raise WorkflowError("请选择项目目录或作业目录。")
        if not folder.is_dir():
            raise WorkflowError("所选目录不存在。")
        return {"path": str(folder)}

    def _job(self, job_id):
        with closing(Store(self.service.home)) as store:
            return store.get("jobs", job_id)

    def _run_dir(self, job_id):
        root = self.service.home / "runs"
        folder = root / job_id
        if folder.parent != root or folder.resolve() != folder:
            raise WorkflowError("作业目录的实际位置不在账本运行区。")
        return folder


def _reply(bridge, line):
    identity = None
    try:
        request = json.loads(line.decode("utf-8"))
        if not isinstance(request, dict) or type(request.get("id")) not in (str, int):
            raise WorkflowError("请求必须包含字符串或整数 id。")
        identity = request["id"]
        if request.keys() != {"id", "op", "args"}:
            raise WorkflowError("请求必须包含 id、op 和 args。")
        result = bridge.call(request["op"], request["args"])
        return {"id": identity, "ok": True, "result": result}
    except WorkflowError as exc:
        error = str(exc)
    except (ValueError, UnicodeError):
        error = "请求格式或内容无效，请检查 UTF-8 JSON 和参数。"
    except (OSError, sqlite3.Error, RuntimeError):
        error = "本地账本或作业文件暂不可用，请核对文件路径和权限。"
    return {"id": identity, "ok": False, "error": error}


def serve(bridge, source, sink):
    while line := source.readline(MAX_REQUEST_BYTES + 1):
        if len(line) > MAX_REQUEST_BYTES:
            while line and not line.endswith(b"\n"):
                line = source.readline(MAX_REQUEST_BYTES + 1)
            reply = {"id": None, "ok": False, "error": "桌面请求过长。"}
        else:
            reply = _reply(bridge, line)
        try:
            sink.write(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")
            sink.flush()
        except BrokenPipeError:
            return


def main(argv=None):
    parser = argparse.ArgumentParser(description="CCAW 本地桌面请求桥")
    parser.add_argument("--home", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args(argv)
    serve(DesktopBridge(args.home, args.project), sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
