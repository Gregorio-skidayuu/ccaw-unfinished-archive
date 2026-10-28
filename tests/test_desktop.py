import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ccaw.config import WorkflowError, load
from ccaw.desktop import DesktopService
from ccaw.runner import enqueue, finish
from ccaw.store import Store


class DesktopServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ccaw-desktop-")
        self.root = Path(self.temp.name)
        self.project = self.root / "工程 with spaces"
        self.project.mkdir()
        self.home = self.root / "状态"
        self.service = DesktopService(self.home, self.project)
        self.service.ensure_initialized()

    def tearDown(self):
        self.temp.cleanup()

    def task(self, kind="code"):
        return self.service.create_task("本地任务", "保留已有成果", "成果可以核对", self.project, kind=kind)

    def test_reinitialization_preserves_task_config_and_database_identity(self):
        task = self.task()
        config_path = self.home / "config.json"
        original = config_path.read_bytes()
        self.service.ensure_initialized()
        self.assertEqual(config_path.read_bytes(), original)
        self.assertEqual(self.service.task_details(task["id"])["task"], task)
        with closing(Store(self.home)) as store:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_snapshot_uses_connections_created_in_the_calling_thread(self):
        task = self.task()
        with ThreadPoolExecutor(max_workers=1) as pool:
            snapshot = pool.submit(self.service.snapshot).result(timeout=3)
        self.assertEqual(snapshot["tasks"][0]["id"], task["id"])
        self.assertEqual(snapshot["home"], str(self.home))
        self.assertEqual(snapshot["project"], str(self.project))
        self.assertEqual(snapshot["jobs"], [])
        self.assertTrue(snapshot["config_hash"])

    def test_invalid_task_inputs_are_rejected_before_writing(self):
        for fields in (("", "check", "code"), ("task", "", "code"), ("task", "check", "invalid")):
            with self.subTest(fields=fields), self.assertRaises(WorkflowError):
                self.service.create_task(fields[0], "goal", fields[1], self.project, kind=fields[2])
        self.assertEqual(self.service.snapshot()["tasks"], [])

    def test_chat_handoff_uses_chatgpt_web_and_cannot_start_another_model(self):
        task = self.task("chat")
        handoff = self.service.chat_handoff(task["id"])
        self.assertEqual(handoff["url"], "https://chatgpt.com/")
        self.assertEqual(handoff["channel"], "chatgpt_free_web")
        self.assertIn(task["id"], handoff["text"])
        with patch("ccaw.desktop.enqueue") as submit:
            with self.assertRaises(WorkflowError) as error:
                self.service.start_agent(task["id"], "default", "do work", "main")
        self.assertIn("ChatGPT", str(error.exception))
        submit.assert_not_called()
        with closing(Store(self.home)) as store:
            event = store.rows("SELECT payload FROM events WHERE kind='task.handoff'")[0]
        self.assertEqual(json.loads(event["payload"]), {"channel": "chatgpt_free_web"})

    def test_agent_dispatch_keeps_idempotent_job_identity(self):
        task = self.task()
        with patch("ccaw.desktop.start", side_effect=lambda store, identity: store.get("jobs", identity)) as start:
            first = self.service.start_agent(task["id"], "default", "do work", "main")
            second = self.service.start_agent(task["id"], "default", "do work", "main")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(start.call_count, 2)
        with closing(Store(self.home)) as store:
            self.assertEqual(len(store.rows("SELECT * FROM jobs")), 1)

    def test_configuration_saves_revision_and_rejects_stale_hash(self):
        original = self.service.configuration()
        candidate = copy.deepcopy(original["config"])
        candidate["roles"]["default"]["model"] = "user-selected-model"
        saved = self.service.save_configuration(candidate, original["hash"])
        self.assertEqual(saved["config"]["revision"], original["config"]["revision"] + 1)
        self.assertEqual(saved["config"]["roles"]["default"]["model"], "user-selected-model")
        with self.assertRaises(WorkflowError):
            self.service.save_configuration(original["config"], original["hash"])
        self.assertEqual(load(self.home), saved["config"])

    def test_bad_configuration_keeps_current_files_and_environment(self):
        original = self.service.configuration()
        config_bytes = (self.home / "config.json").read_bytes()
        history = set((self.home / "config-history").iterdir())
        bad = copy.deepcopy(original["config"])
        bad["roles"]["default"]["backend"] = []
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(WorkflowError):
                self.service.save_configuration(bad, original["hash"], api_key="private-test-key")
            self.assertNotIn(original["config"]["paid_api"]["key_env"], os.environ)
        self.assertEqual((self.home / "config.json").read_bytes(), config_bytes)
        self.assertEqual(set((self.home / "config-history").iterdir()), history)

    def test_api_key_only_lives_in_current_process_environment(self):
        original = self.service.configuration()
        candidate = copy.deepcopy(original["config"])
        candidate["paid_api"]["key_env"] = "CCAW_DESKTOP_TEST_KEY"
        key = "private-test-key-not-for-files"
        with patch.dict(os.environ, {}, clear=True):
            saved = self.service.save_configuration(candidate, original["hash"], api_key=key)
            self.assertEqual(os.environ["CCAW_DESKTOP_TEST_KEY"], key)
            self.assertTrue(saved["key_present"])
            self.assertNotIn(key, json.dumps(saved))
            newer = self.service.save_configuration(saved["config"], saved["hash"], api_key="")
            self.assertTrue(newer["key_present"])
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(key.encode(), path.read_bytes(), str(path))

    def test_job_actions_keep_unknown_and_automatic_queue_protections(self):
        task = self.task()
        with closing(Store(self.home)) as store:
            job, _ = enqueue(store, task["id"], "script", command=[sys.executable, "-V"])
            store.db.execute("UPDATE jobs SET status='UNKNOWN' WHERE id=?", (job["id"],))
        with patch("ccaw.desktop.start") as launch:
            with self.assertRaises(WorkflowError):
                self.service.job_action(job["id"], "launch")
        launch.assert_not_called()
        with self.assertRaises(WorkflowError):
            self.service.job_action(job["id"], "unsupported")
        details = self.service.job_details(job["id"])
        self.assertEqual(details["status"], "UNKNOWN")
        self.assertIn("diagnostics", details)

    def test_watch_reports_known_start_failure_instead_of_success(self):
        task = self.task()
        with closing(Store(self.home)) as store:
            job, _ = enqueue(store, task["id"], "codex", auto_resume=True)
            finish(store, job["id"], "WAITING_CAPACITY", 1, "CAPACITY")
        with patch("ccaw.desktop.capacity.ensure_watcher", return_value={"launched": False, "watcher_running": False}):
            with self.assertRaises(WorkflowError):
                self.service.job_action(job["id"], "watch")

    def test_health_uses_non_inference_diagnostics(self):
        with patch("ccaw.desktop.doctor", return_value={"ready": True}) as diagnose:
            self.assertEqual(self.service.health(), {"ready": True})
        diagnose.assert_called_once_with(self.home)


if __name__ == "__main__":
    unittest.main()
