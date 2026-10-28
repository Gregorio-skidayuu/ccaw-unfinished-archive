import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ccaw.capacity import disable, ensure_watcher, status, tick, watcher_lock
from ccaw.config import DEFAULT_CONFIG, WorkflowError, save_version
from ccaw.processes import identity, same_process, terminate
from ccaw.runner import enqueue, finish, start
from ccaw.store import Store


SOURCE = Path(__file__).resolve().parents[1]


class CapacityWatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ccaw-capacity-")
        self.base = Path(self.temp.name)
        self.project = self.base / "工程 with spaces"
        self.project.mkdir()
        self.home = self.base / "状态"
        self.store = Store.initialize(self.home)
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config["codex"]["executable"] = "ccaw-test-codex-not-installed"
        save_version(self.home, self.config)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def task(self, project=None):
        return self.store.create_task("额度等待", "恢复原任务", "没有重复执行", project or self.project,
                                      [], "code", "medium", "medium", "default")

    def waiting(self, *, session="specific-session", auto=True):
        job, _ = enqueue(self.store, self.task()["id"], "codex", prompt="原指令", auto_resume=auto)
        self.store.db.execute("UPDATE jobs SET session_id=? WHERE id=?", (session, job["id"]))
        finish(self.store, job["id"], "WAITING_CAPACITY", 1, "CAPACITY")
        return self.store.get("jobs", job["id"])

    def profile(self, *, allowed=True, cycle="cycle-1", account="account-1", at=1300):
        return {"account_id": account, "allowed": allowed, "cycle": cycle,
                "next_check_at": at, "reason": "明确许可" if allowed else "仍在等待额度"}

    def child(self, root):
        rows = self.store.rows("SELECT * FROM jobs WHERE dedupe_key=?", ("capacity:" + root["id"],))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def create_queued_child(self, root):
        with patch("ccaw.capacity.read_capacity", return_value=self.profile()), patch("ccaw.runner.start"):
            result = tick(self.store, root["id"], at=1000)
        self.assertFalse(result["done"])
        return self.child(root)

    def test_wait_deadline_survives_reopen_without_model_or_early_query(self):
        root = self.waiting()
        with patch("ccaw.capacity.read_capacity", return_value=self.profile(allowed=False, at=5000)) as read, \
                patch("ccaw.runner.start") as launch:
            first = tick(self.store, root["id"], at=1000)
            self.assertEqual(first["next_check_at"], 5000)
            self.store.close()
            self.store = Store(self.home)
            self.assertEqual(tick(self.store, root["id"], at=4999)["next_check_at"], 5000)
            self.assertEqual(read.call_count, 1)
            later = tick(self.store, root["id"], at=5000)
            self.assertFalse(later["done"])
            self.assertGreaterEqual(later["next_check_at"], 5300)
            self.assertEqual(read.call_count, 2)
            launch.assert_not_called()
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 1)

    def test_unavailable_query_keeps_waiting_without_exposing_error_details(self):
        root = self.waiting()
        with patch("ccaw.capacity.read_capacity", side_effect=WorkflowError("private-provider-response")), \
                patch("ccaw.runner.start") as launch:
            result = tick(self.store, root["id"], at=1000)
        self.assertEqual(result["next_check_at"], 1300)
        self.assertNotIn("private-provider-response", json.dumps(result))
        launch.assert_not_called()
        self.assertEqual(self.store.get("jobs", root["id"])["status"], "WAITING_CAPACITY")

    def test_explicit_permission_resumes_specific_session_and_persists_once(self):
        root = self.waiting()
        child = self.create_queued_child(root)
        self.assertEqual(child["session_id"], root["session_id"])
        policy = json.loads(child["snapshot"])["auto_resume"]
        self.assertEqual(policy["root_job_id"], root["id"])
        self.assertEqual(policy["parent_job_id"], root["id"])
        self.assertEqual(policy["cycle"], "cycle-1")
        with patch("ccaw.capacity.read_capacity") as read, patch("ccaw.runner.start") as launch:
            tick(self.store, root["id"], at=1005)
        read.assert_not_called()
        launch.assert_not_called()
        with patch("ccaw.capacity.read_capacity", return_value=self.profile(at=1600)) as read, \
                patch("ccaw.runner.start") as launch:
            tick(self.store, root["id"], at=1300)
        read.assert_called_once()
        self.assertEqual(launch.call_args.args[1], child["id"])
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 2)
        self.assertEqual(len(self.store.rows("SELECT * FROM events WHERE kind='capacity.resumed'")), 1)

    def test_again_limited_waits_for_a_new_cycle_before_creating_another_attempt(self):
        root = self.waiting()
        child = self.create_queued_child(root)
        finish(self.store, child["id"], "WAITING_CAPACITY", 1, "CAPACITY")
        with patch("ccaw.capacity.read_capacity", return_value=self.profile(at=1600)), \
                patch("ccaw.runner.start") as launch:
            result = tick(self.store, root["id"], at=1300)
        self.assertFalse(result["done"])
        launch.assert_not_called()
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 2)
        with patch("ccaw.capacity.read_capacity", return_value=self.profile(cycle="cycle-2", at=1900)), \
                patch("ccaw.runner.start") as launch:
            tick(self.store, root["id"], at=1600)
        following = self.child(child)
        self.assertEqual(launch.call_args.args[1], following["id"])
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 3)

    def test_repeated_resume_keeps_original_instruction_without_nesting_packets(self):
        instruction = "必须读取指定输入 abc-XYZ，不得改用样例数据"
        job, _ = enqueue(self.store, self.task()["id"], "codex", prompt=instruction, auto_resume=True)
        self.store.db.execute("UPDATE jobs SET session_id='specific-session' WHERE id=?", (job["id"],))
        finish(self.store, job["id"], "WAITING_CAPACITY", 1, "CAPACITY")
        with patch("ccaw.capacity.read_capacity", return_value=self.profile()), patch("ccaw.runner.start"):
            tick(self.store, job["id"], at=1000)
        child = self.child(job)
        finish(self.store, child["id"], "WAITING_CAPACITY", 1, "CAPACITY")
        with patch("ccaw.capacity.read_capacity", return_value=self.profile(cycle="cycle-2", at=1600)), \
                patch("ccaw.runner.start"):
            tick(self.store, job["id"], at=1300)
        following = self.child(child)
        self.assertEqual(child["prompt"].count(instruction), 1)
        self.assertEqual(following["prompt"].count(instruction), 1)

    def test_missing_session_and_unknown_stop_before_reading_quota(self):
        root = self.waiting(session=None)
        with patch("ccaw.capacity.read_capacity") as read, patch("ccaw.runner.start") as launch:
            self.assertTrue(tick(self.store, root["id"], at=1000)["done"])
        read.assert_not_called()
        launch.assert_not_called()
        self.store.db.execute("UPDATE jobs SET status='UNKNOWN',session_id=? WHERE id=?", ("session", root["id"]))
        with patch("ccaw.capacity.read_capacity") as read, patch("ccaw.runner.start") as launch:
            self.assertTrue(tick(self.store, root["id"], at=1001)["done"])
        read.assert_not_called()
        launch.assert_not_called()

    def test_manual_advance_and_task_acceptance_stop_the_old_plan(self):
        root = self.waiting()
        manual, _ = enqueue(self.store, root["task_id"], "codex", prompt="人工接续", key="manual",
                            resume_job=root["id"])
        with patch("ccaw.capacity.read_capacity") as read, patch("ccaw.runner.start") as launch:
            self.assertTrue(tick(self.store, root["id"], at=1000)["done"])
        read.assert_not_called()
        launch.assert_not_called()
        finish(self.store, manual["id"], "SUCCEEDED", 0)
        self.store.db.execute("UPDATE tasks SET status='DONE' WHERE id=?", (root["task_id"],))
        with patch("ccaw.capacity.read_capacity") as read:
            self.assertTrue(tick(self.store, root["id"], at=1300)["done"])
        read.assert_not_called()

    def test_account_change_stops_without_dispatch(self):
        root = self.waiting()
        self.store.event("capacity.account", {"account_id": "account-1"}, task=root["task_id"], job=root["id"])
        with patch("ccaw.capacity.read_capacity", return_value=self.profile(account="account-2")), \
                patch("ccaw.runner.start") as launch:
            result = tick(self.store, root["id"], at=1000)
        self.assertTrue(result["done"])
        self.assertIn("账号", result["reason"])
        launch.assert_not_called()
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 1)

    def test_disable_cancels_queued_automatic_child_and_blocks_watcher_restart(self):
        root = self.waiting()
        child = self.create_queued_child(root)
        report = disable(self.store, child["id"])
        self.assertFalse(report["enabled"])
        self.assertEqual(self.store.get("jobs", child["id"])["status"], "CANCELLED")
        with patch("ccaw.capacity.read_capacity") as read, patch("ccaw.runner.subprocess.Popen") as popen:
            self.assertTrue(tick(self.store, root["id"], at=1300)["done"])
            self.assertEqual(start(self.store, child["id"])["status"], "CANCELLED")
            with self.assertRaises(WorkflowError):
                ensure_watcher(self.store, root["id"])
        read.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(len(self.store.rows("SELECT * FROM events WHERE kind='capacity.disabled'")), 1)
        disable(self.store, root["id"])
        self.assertEqual(len(self.store.rows("SELECT * FROM events WHERE kind='capacity.disabled'")), 1)

    def test_other_project_job_waits_and_unknown_job_stops_before_quota_read(self):
        root = self.waiting()
        other, _ = enqueue(self.store, self.task()["id"], "script", command=[sys.executable, "-V"])
        with patch("ccaw.capacity.read_capacity") as read, patch("ccaw.runner.start") as launch:
            result = tick(self.store, root["id"], at=1000)
            self.assertFalse(result["done"])
            self.assertEqual(result["next_check_at"], 1005)
            self.store.db.execute("UPDATE jobs SET status='UNKNOWN' WHERE id=?", (other["id"],))
            self.assertTrue(tick(self.store, root["id"], at=1005)["done"])
        read.assert_not_called()
        launch.assert_not_called()

    def test_running_attempt_only_checks_process_state_and_waits(self):
        root = self.waiting()
        self.store.db.execute("UPDATE jobs SET status='RUNNING' WHERE id=?", (root["id"],))
        running = self.store.get("jobs", root["id"])
        with patch("ccaw.runner.reconcile", return_value=running) as reconcile, \
                patch("ccaw.capacity.read_capacity") as read, patch("ccaw.runner.start") as launch:
            result = tick(self.store, root["id"], at=1000)
        self.assertFalse(result["done"])
        self.assertEqual(result["next_check_at"], 1005)
        self.assertEqual(reconcile.call_args.args[1], root["id"])
        read.assert_not_called()
        launch.assert_not_called()

    def test_unauthorized_job_cannot_start_a_waiter(self):
        root = self.waiting(auto=False)
        with patch("ccaw.capacity.subprocess.Popen") as popen:
            with self.assertRaises(WorkflowError):
                tick(self.store, root["id"], at=1000)
            with self.assertRaises(WorkflowError):
                ensure_watcher(self.store, root["id"])
        popen.assert_not_called()

    def test_file_lock_excludes_another_process_and_releases_after_exit(self):
        path = self.home / "watcher.lock"
        code = "from ccaw.capacity import watcher_lock\nimport os,sys\nwith watcher_lock(sys.argv[1]) as acquired:\n print('held' if acquired else 'busy',flush=True)\n os._exit(0)"
        command = [sys.executable, "-c", code, str(path)]
        with watcher_lock(path) as acquired:
            self.assertTrue(acquired)
            blocked = subprocess.run(command, cwd=SOURCE, capture_output=True, text=True, timeout=5)
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            self.assertEqual(blocked.stdout.strip(), "busy")
        exited = subprocess.run(command, cwd=SOURCE, capture_output=True, text=True, timeout=5)
        self.assertEqual(exited.returncode, 0, exited.stderr)
        self.assertEqual(exited.stdout.strip(), "held")
        with watcher_lock(path) as acquired:
            self.assertTrue(acquired)

    def test_detached_waiter_survives_launcher_and_stops_after_revocation(self):
        root = self.waiting()
        launcher = "import json,sys\nfrom ccaw.store import Store\nfrom ccaw.capacity import ensure_watcher\ns=Store(sys.argv[1])\ntry:\n print(json.dumps(ensure_watcher(s,sys.argv[2])))\nfinally:\n s.close()"
        result = subprocess.run([sys.executable, "-c", launcher, str(self.home), root["id"]],
                                cwd=SOURCE, capture_output=True, text=True, encoding="utf-8", timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        launched = json.loads(result.stdout)
        self.assertTrue(launched["launched"])
        process = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rows = self.store.rows("SELECT payload FROM events WHERE job_id=? AND kind='capacity.started'", (root["id"],))
                waiting = self.store.rows("SELECT id FROM events WHERE job_id=? AND kind='capacity.waiting'", (root["id"],))
                if rows and waiting:
                    process = json.loads(rows[-1]["payload"])
                    break
                time.sleep(.05)
            self.assertIsNotNone(process, "Detached waiter did not persist its waiting state")
            self.assertEqual(process["pid"], launched["pid"])
            self.assertTrue(same_process(process["pid"], process["process_token"]))
            self.assertTrue(status(self.store, root["id"])["watcher_running"])
            disable(self.store, root["id"])
            deadline = time.monotonic() + 7
            while same_process(process["pid"], process["process_token"]) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(same_process(process["pid"], process["process_token"]))
            self.assertFalse(status(self.store, root["id"])["enabled"])
            self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 1)
        finally:
            if process and same_process(process["pid"], process["process_token"]):
                terminate(process["pid"], process["process_token"])
                deadline = time.monotonic() + 3
                while same_process(process["pid"], process["process_token"]) and time.monotonic() < deadline:
                    time.sleep(.05)


if __name__ == "__main__":
    unittest.main()
