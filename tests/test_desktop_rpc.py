from contextlib import closing
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ccaw.config import WorkflowError
from ccaw.desktop_rpc import DesktopBridge, MAX_LOG_BYTES, serve
from ccaw.runner import enqueue, finish
from ccaw.store import Store


def request(identity, op, **args):
    message = {"id": identity, "op": op, "args": args}
    return json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"


class DesktopBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ccaw-desktop-rpc-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "测试 项目"
        self.project.mkdir()
        self.home = self.root / "本地账本"
        self.bridge = DesktopBridge(self.home, self.project)

    def task(self):
        return self.bridge.call("create_task", {
            "title": "核对交付", "goal": "运行任务", "acceptance": "核对输出",
            "project": str(self.project), "kind": "script",
        })

    def job(self, task):
        with closing(Store(self.home)) as store:
            job, _ = enqueue(store, task["id"], "script", command=[sys.executable, "private-command"])
        return job

    def test_module_stdio_is_utf8_and_answers_multiple_requests(self):
        input_data = b"{invalid}\n" + request("界面-1", "create_task",
            title="脚本任务", goal="英文和中文可一起使用", acceptance="核对中文输出",
            project=str(self.project), kind="script",
        ) + request(2, "snapshot")
        result = subprocess.run(
            [sys.executable, "-m", "ccaw.desktop_rpc", "--home", str(self.home), "--project", str(self.project)],
            input=input_data, capture_output=True, check=True, timeout=15,
        )
        error, created, snapshot = map(json.loads, result.stdout.splitlines())
        self.assertEqual((error["id"], error["ok"]), (None, False))
        self.assertEqual((created["id"], created["ok"]), ("界面-1", True))
        self.assertEqual(snapshot["result"]["tasks"], [created["result"]])
        self.assertEqual(snapshot["result"]["tasks"][0]["title"], "脚本任务")
        self.assertEqual(set(snapshot["result"]), {"tasks", "jobs", "home", "project", "selected_task"})

    def test_reopen_keeps_selected_task_and_accepted_job_evidence(self):
        task = self.task()
        job = self.job(task)
        self.assertEqual(self.bridge.call("select", {"task_id": task["id"]}), {"selected_task": task["id"]})
        with closing(Store(self.home)) as store:
            finish(store, job["id"], "SUCCEEDED", 0)
        self.bridge.call("accept_task", {
            "task_id": task["id"], "job_id": job["id"], "evidence": "已核对原作业的输出。",
        })
        reopened = DesktopBridge(self.home, self.project)
        snapshot = reopened.call("snapshot", {})
        self.assertEqual(snapshot["selected_task"], task["id"])
        self.assertEqual(snapshot["jobs"][0]["id"], job["id"])
        detail = reopened.call("task_details", {"task_id": task["id"]})
        self.assertEqual(detail["task"]["status"], "DONE")
        self.assertEqual(detail["acceptance_event"]["job_id"], job["id"])
        self.assertEqual(detail["acceptance_event"]["payload"], {"evidence": "已核对原作业的输出。"})

    def test_unknown_keeps_original_script_job_and_blocks_another_start(self):
        task = self.task()
        job = self.job(task)
        with closing(Store(self.home)) as store:
            store.db.execute("UPDATE jobs SET status='UNKNOWN' WHERE id=?", (job["id"],))
        fields = {"task_id": task["id"], "command": [sys.executable, "private-command"], "key": "main"}
        with patch("ccaw.desktop.start") as start:
            repeated = self.bridge.call("start_script", fields)
            self.assertEqual((repeated["id"], repeated["status"]), (job["id"], "UNKNOWN"))
            self.assertNotIn("private-command", json.dumps(repeated))
            with self.assertRaisesRegex(WorkflowError, job["id"]):
                self.bridge.call("start_script", dict(fields, key="different-stage"))
        start.assert_not_called()

    def test_diagnostics_and_logs_only_expose_allowlisted_files(self):
        task = self.task()
        job = self.job(task)
        run_dir = self.home / "runs" / job["id"]
        run_dir.mkdir(parents=True)
        (run_dir / "prompt.md").write_text("private-prompt", encoding="utf-8")
        (run_dir / "stdout.log").write_bytes("核对\n".encode("utf-8") + b"x" * MAX_LOG_BYTES)
        fields = {"job_id": job["id"]}
        details = self.bridge.call("job_details", fields)
        self.assertEqual(set(details["diagnostics"]["files"]), {"stdout.log"})
        for private in ("private-command", "private-prompt", "prompt.md", "snapshot"):
            self.assertNotIn(private, json.dumps(details))
        log = self.bridge.call("read_log", dict(fields, name="stdout.log"))
        self.assertTrue(log["truncated"])
        self.assertTrue(log["text"].startswith("核对\n"))
        self.assertEqual(len(log["text"].encode("utf-8")), MAX_LOG_BYTES)
        self.assertEqual(self.bridge.call("folder_path", dict(fields, destination="run")), {"path": str(run_dir)})
        self.assertEqual(self.bridge.call("folder_path", dict(fields, destination="project")), {"path": str(self.project)})
        for name in ("prompt.md", "execution.json", "../config.json"):
            with self.subTest(name=name), self.assertRaises(WorkflowError):
                self.bridge.call("read_log", dict(fields, name=name))

    def test_paid_api_arguments_and_existing_api_queue_cannot_dispatch(self):
        task = self.task()
        job = self.job(task)
        with closing(Store(self.home)) as store:
            store.db.execute("UPDATE jobs SET kind='api' WHERE id=?", (job["id"],))
        with patch.object(self.bridge.service, "job_action") as action:
            with self.assertRaisesRegex(WorkflowError, "付费 API"):
                self.bridge.call("job_action", {"job_id": job["id"], "action": "launch"})
        action.assert_not_called()
        source = io.BytesIO(request("extra", "start_agent", task_id=task["id"], role="default",
                                    prompt="never sent", key="api", allow_api=True))
        output = io.BytesIO()
        serve(self.bridge, source, output)
        denied = json.loads(output.getvalue())
        self.assertEqual((denied["id"], denied["ok"]), ("extra", False))
        self.assertNotIn("never sent", denied["error"])


if __name__ == "__main__":
    unittest.main()
