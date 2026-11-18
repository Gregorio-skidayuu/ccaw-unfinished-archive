import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from ccaw.cli import main
from ccaw.config import DEFAULT_CONFIG, load, save_version
from ccaw.runner import enqueue, finish
from ccaw.store import Store


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "状态 with spaces"

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--home", str(self.home), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_no_arguments_show_getting_started_help(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main([]), 0)
        self.assertIn("首次使用", stdout.getvalue())
        self.assertFalse(self.home.exists())

    def test_doctor_before_init_explains_next_step_without_creating_state(self):
        with patch("ccaw.cli.shutil.which", return_value=None):
            code, output, error = self.call("doctor")
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertFalse(report["ready"])
        self.assertIsNone(report["paid_api_enabled"])
        self.assertTrue(any("init" in issue for issue in report["issues"]))
        self.assertFalse(self.home.exists())

    def test_doctor_checks_database_without_requiring_codex(self):
        self.assertEqual(self.call("init")[0], 0)
        with patch("ccaw.cli.shutil.which", return_value=None):
            code, output, error = self.call("doctor")
        report = json.loads(output)
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(report["ready"])
        self.assertEqual(report["database"]["integrity"], "ok")
        self.assertEqual(report["database"]["schema_version"], 1)
        self.assertTrue(report["next_steps"])

    def test_doctor_reports_bad_config_and_future_database_without_repairing_them(self):
        self.call("init")
        config_path = self.home / "config.json"
        config_path.write_text("[]", encoding="utf-8")
        database = self.home / "state.sqlite3"
        db = sqlite3.connect(database)
        try:
            db.execute("PRAGMA user_version=999")
            db.commit()
        finally:
            db.close()
        original = database.read_bytes()
        with patch("ccaw.cli.shutil.which", return_value=None):
            code, output, error = self.call("doctor")
        report = json.loads(output)
        self.assertEqual((code, error), (2, ""))
        self.assertEqual(len(report["issues"]), 2)
        self.assertEqual(database.read_bytes(), original)
        self.assertEqual(config_path.read_text(encoding="utf-8"), "[]")

    def test_config_apply_rejects_non_object_without_changing_version(self):
        self.call("init")
        original = (self.home / "config.json").read_bytes()
        history = list((self.home / "config-history").iterdir())
        candidate = self.base / "candidate.json"
        for value in ([], None, "invalid"):
            with self.subTest(value=value):
                candidate.write_text(json.dumps(value), encoding="utf-8")
                code, output, error = self.call("config", "apply", str(candidate))
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertIn("错误", error)
                self.assertNotIn("Traceback", error)
                self.assertEqual((self.home / "config.json").read_bytes(), original)
                self.assertEqual(list((self.home / "config-history").iterdir()), history)

    def test_config_apply_accepts_utf8_bom(self):
        self.call("init")
        candidate = self.base / "candidate.json"
        candidate.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8-sig")
        code, output, error = self.call("config", "apply", str(candidate))
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["revision"], 2)
        self.assertEqual(load(self.home)["revision"], 2)

    def test_repeated_init_preserves_tasks_and_reports_actual_api_setting(self):
        self.call("init")
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paid_api"]["enabled"] = True
        save_version(self.home, config)
        original = (self.home / "config.json").read_bytes()
        store = Store(self.home)
        try:
            task = store.create_task("保留身份", "检查", "身份不变", self.base, [], "script", "low", "low", "script")
        finally:
            store.close()
        code, output, error = self.call("init")
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(json.loads(output)["paid_api"])
        self.assertEqual((self.home / "config.json").read_bytes(), original)
        store = Store(self.home)
        try:
            self.assertEqual(store.get("tasks", task["id"]), task)
        finally:
            store.close()

    def test_job_show_includes_failure_evidence_and_existing_log_paths(self):
        self.call("init")
        store = Store(self.home)
        try:
            task = store.create_task("故障诊断", "检查", "可以找到证据", self.base, [], "script", "low", "low", "script")
            job, _ = enqueue(store, task["id"], "script", command=[sys.executable, "-V"])
            folder = self.home / "runs" / job["id"]
            folder.mkdir(parents=True)
            (folder / "stderr.log").write_text("example failure", encoding="utf-8")
            finish(store, job["id"], "FAILED", 1, "EXECUTION", "可核对的失败原因")
        finally:
            store.close()
        code, output, error = self.call("job", "show", job["id"])
        details = json.loads(output)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(details["id"], job["id"])
        self.assertEqual(details["status"], "FAILED")
        diagnostics = details["diagnostics"]
        self.assertEqual(Path(diagnostics["files"]["stderr.log"]), folder / "stderr.log")
        self.assertNotIn("result.md", diagnostics["files"])
        self.assertEqual(diagnostics["recent_events"][-1]["payload"]["detail"], "可核对的失败原因")
        self.assertIn("检查", diagnostics["next_action"])

    def test_compact_handoff_keeps_recent_jobs_success_evidence_and_explicit_resume(self):
        self.assertEqual(self.call("init")[0], 0)
        store = Store(self.home)
        try:
            task = store.create_task("接续实验", "核对已有成果", "找到成功日志", self.base, [],
                                     "code", "low", "low", "default")
            task_id = task["id"]
            original, _ = enqueue(store, task_id, "codex", key="original")
            store.db.execute("UPDATE jobs SET session_id=? WHERE id=?", ("original-session", original["id"]))
            finish(store, original["id"], "WAITING_CAPACITY", 1)
            script = [sys.executable, "-V"]
            evidence, _ = enqueue(store, task_id, "script", command=script, key="evidence")
            finish(store, evidence["id"], "SUCCEEDED", 0)
            failures = []
            for index in range(10):
                job, _ = enqueue(store, task_id, "script", command=script, key=f"attempt-{index}")
                finish(store, job["id"], "FAILED", 1)
                failures.append(job["id"])
            resumed, _ = enqueue(store, task_id, "codex", key="resume", resume_job=original["id"], prompt="接续")
            self.assertEqual(resumed["session_id"], "original-session")
            self.assertIn(original["id"], resumed["prompt"])
            self.assertIn(evidence["id"], resumed["prompt"])
            self.assertNotIn(failures[0], resumed["prompt"])
            self.assertTrue(all(job_id in resumed["prompt"] for job_id in failures[-8:]))
        finally:
            store.close()

        code, compact, error = self.call("task", "export", task_id, "--compact")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("核对已有成果", compact)
        self.assertIn("找到成功日志", compact)
        self.assertIn(evidence["id"], compact)
        self.assertIn(resumed["id"], compact)
        self.assertIn(failures[-1], compact)
        self.assertNotIn(failures[0], compact)
        self.assertIn("另有 4 条早期作业", compact)
        self.assertIn(f"task show {task_id}", compact)
        code, full, error = self.call("task", "export", task_id)
        self.assertEqual((code, error), (0, ""))
        self.assertIn(failures[0], full)
        code, shown, error = self.call("task", "show", task_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(len(json.loads(shown)["jobs"]), 13)


if __name__ == "__main__":
    unittest.main()
