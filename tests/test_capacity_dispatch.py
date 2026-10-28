import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ccaw.capacity import is_enabled, tick
from ccaw.config import DEFAULT_CONFIG, WorkflowError, save_version
from ccaw.runner import enqueue, finish, start
from ccaw.store import Store


class CapacityDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ccaw-capacity-dispatch-")
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.store = Store.initialize(Path(self.temp.name) / "state")
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        save_version(self.store.root, self.config)
        task = self.store.create_task("恢复队列", "继续原任务", "不会使用过期许可", self.project,
                                      [], "code", "medium", "medium", "default")
        self.parent, _ = enqueue(self.store, task["id"], "codex", prompt="保留原指令", auto_resume=True)
        self.store.db.execute("UPDATE jobs SET session_id='original-session' WHERE id=?", (self.parent["id"],))
        finish(self.store, self.parent["id"], "WAITING_CAPACITY", 1, "CAPACITY")
        self.store.event("capacity.account", {"account_id": "account-1"}, task=task["id"], job=self.parent["id"])
        self.child, _ = enqueue(self.store, task["id"], "codex", prompt="恢复原任务",
                                key="capacity:" + self.parent["id"], resume_job=self.parent["id"],
                                capacity_parent=self.parent["id"], capacity_cycle="cycle-1",
                                capacity_account="account-1")
        self.store.event("capacity.waiting", {"source_job_id": self.parent["id"], "next_check_at": 1300,
                                              "reason": "等待重新确认"},
                         task=task["id"], job=self.parent["id"])
        self.profile = {"account_id": "account-1", "allowed": True, "cycle": "cycle-1",
                        "next_check_at": 1600, "reason": "额度明确可用"}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def assert_not_started(self, launch):
        launch.assert_not_called()
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 2)
        self.assertNotIn(self.store.get("jobs", self.child["id"])["status"], {"STARTING", "RUNNING"})

    def test_queued_recovery_respects_persisted_check_time_and_reuses_same_intent(self):
        with patch("ccaw.capacity.read_capacity", return_value=self.profile) as query, \
                patch("ccaw.runner.start", return_value=self.child) as launch:
            early = tick(self.store, self.parent["id"], at=1100)
            self.assertEqual(early["next_check_at"], 1300)
            query.assert_not_called()
            launch.assert_not_called()
            result = tick(self.store, self.parent["id"], at=1300)
        self.assertFalse(result["done"])
        query.assert_called_once()
        launch.assert_called_once_with(self.store, self.child["id"], capacity_profile=self.profile)
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 2)
        self.assertEqual(len(self.store.rows("SELECT * FROM events WHERE kind='capacity.resumed'")), 1)

    def test_missing_explicit_permission_keeps_queued_work_waiting(self):
        for index, value in enumerate((None, False, 1, "true")):
            with self.subTest(allowed=value):
                profile = dict(self.profile, allowed=value)
                with patch("ccaw.capacity.read_capacity", return_value=profile), \
                        patch("ccaw.runner.start") as launch:
                    result = tick(self.store, self.parent["id"], at=2000 + 400 * index)
                self.assertFalse(result["done"])
                self.assert_not_started(launch)
                self.assertEqual(self.store.get("jobs", self.child["id"])["status"], "QUEUED")
                self.assertTrue(is_enabled(self.store, self.parent))

    def test_role_change_revokes_old_queue_before_any_quota_read(self):
        self.config["roles"]["default"]["backend"] = "api"
        save_version(self.store.root, self.config)
        with patch("ccaw.capacity.read_capacity") as query, patch("ccaw.runner.start") as launch:
            result = tick(self.store, self.parent["id"], at=1100)
        self.assertTrue(result["done"])
        query.assert_not_called()
        self.assert_not_started(launch)
        self.assertEqual(self.store.get("jobs", self.child["id"])["status"], "CANCELLED")
        self.assertFalse(is_enabled(self.store, self.parent))

    def test_changed_account_revokes_only_unstarted_queue(self):
        profile = dict(self.profile, account_id="other-account")
        with patch("ccaw.capacity.read_capacity", return_value=profile), \
                patch("ccaw.runner.start") as launch:
            result = tick(self.store, self.parent["id"], at=1300)
        self.assertTrue(result["done"])
        self.assert_not_started(launch)
        self.assertFalse(is_enabled(self.store, self.parent))
        self.assertEqual(self.store.get("jobs", self.child["id"])["status"], "CANCELLED")
        self.assertEqual(self.store.get("jobs", self.parent["id"])["status"], "WAITING_CAPACITY")

    def test_changed_cycle_revokes_old_queue(self):
        with patch("ccaw.capacity.read_capacity", return_value=dict(self.profile, cycle="cycle-2")), \
                patch("ccaw.runner.start") as launch:
            result = tick(self.store, self.parent["id"], at=1300)
        self.assertTrue(result["done"])
        self.assert_not_started(launch)
        self.assertFalse(is_enabled(self.store, self.parent))
        self.assertEqual(self.store.get("jobs", self.child["id"])["status"], "CANCELLED")

    def test_direct_launch_cannot_bypass_fresh_capacity_confirmation(self):
        profiles = [None, dict(self.profile, allowed=False), dict(self.profile, account_id="other"),
                    dict(self.profile, cycle="cycle-2")]
        for profile in profiles:
            with self.subTest(profile=profile):
                with patch("ccaw.runner.subprocess.Popen") as launch:
                    with self.assertRaises(WorkflowError):
                        start(self.store, self.child["id"], capacity_profile=profile)
                self.assert_not_started(launch)
                self.assertEqual(self.store.get("jobs", self.child["id"])["status"], "QUEUED")

    def test_dispatch_rechecks_role_after_network_response(self):
        def changed_role(config):
            self.config["roles"]["default"]["backend"] = "api"
            save_version(self.store.root, self.config)
            return self.profile
        with patch("ccaw.capacity.read_capacity", side_effect=changed_role), \
                patch("ccaw.runner.subprocess.Popen") as launch:
            result = tick(self.store, self.parent["id"], at=1300)
        self.assertFalse(result["done"])
        self.assert_not_started(launch)
        self.assertEqual(self.store.get("jobs", self.child["id"])["status"], "QUEUED")
        result = tick(self.store, self.parent["id"], at=1301)
        self.assertTrue(result["done"])
        self.assertEqual(self.store.get("jobs", self.child["id"])["status"], "CANCELLED")

    def test_dispatch_rejects_a_task_accepted_after_queuing(self):
        self.store.db.execute("UPDATE tasks SET status='DONE' WHERE id=?", (self.child["task_id"],))
        with patch("ccaw.runner.subprocess.Popen") as launch:
            with self.assertRaises(WorkflowError):
                start(self.store, self.child["id"], capacity_profile=self.profile)
        self.assert_not_started(launch)


if __name__ == "__main__":
    unittest.main()
