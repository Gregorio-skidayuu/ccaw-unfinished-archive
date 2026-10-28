import copy
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ccaw.cli import main
from ccaw.config import DEFAULT_CONFIG, WorkflowError, fingerprint, save_version
from ccaw.runner import cancel, enqueue, finish, run_worker, start
from ccaw.store import Store


class AutoResumeFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ccaw-auto-flow-")
        self.project = Path(self.temp.name) / "工程 with spaces"
        self.project.mkdir()
        self.home = Path(self.temp.name) / "状态"
        self.store = Store.initialize(self.home)
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        save_version(self.home, self.config)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def task(self):
        return self.store.create_task("自动接续", "额度恢复后继续原会话", "计算不重复", self.project,
                                      [], "code", "medium", "medium", "default")

    def waiting_job(self, auto_resume=True):
        job, _ = enqueue(self.store, self.task()["id"], "codex", prompt="完成原任务", auto_resume=auto_resume)
        self.store.db.execute("UPDATE jobs SET session_id=? WHERE id=?", ("specific-session", job["id"]))
        finish(self.store, job["id"], "WAITING_CAPACITY", 1, "CAPACITY")
        if auto_resume:
            # The real watcher binds the confirmed quota account before enqueue.
            self.store.event("capacity.account", {"account_id": "account-1"},
                             task=job["task_id"], job=job["id"])
        return self.store.get("jobs", job["id"])

    def resume(self, parent, cycle="cycle-1"):
        return enqueue(self.store, parent["task_id"], "codex", prompt="核对已有成果后接续", role="default",
                       key="capacity:" + parent["id"], resume_job=parent["id"],
                       capacity_parent=parent["id"], capacity_cycle=cycle, capacity_account="account-1")

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--home", str(self.home), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_job_preserves_old_idempotency_and_has_no_auto_authorization(self):
        task = self.task()
        job, created = enqueue(self.store, task["id"], "codex", prompt="原指令")
        legacy_specification = {
            "kind": "codex", "command": [], "cwd": task["project"], "prompt": "原指令",
            "role": "default", "session": None, "then_prompt": None, "then_role": "default",
        }
        snapshot = json.loads(job["snapshot"])
        self.assertTrue(created)
        self.assertNotIn("auto_resume", snapshot)
        self.assertEqual(snapshot["spec_hash"], fingerprint(legacy_specification))
        duplicate, created = enqueue(self.store, task["id"], "codex", prompt="原指令")
        self.assertFalse(created)
        self.assertEqual(duplicate, job)
        self.assertEqual(self.store.rows("SELECT * FROM events WHERE kind LIKE 'capacity.%'"), [])

    def test_auto_resume_requires_explicit_flag_and_codex_backend(self):
        task = self.task()
        with self.assertRaises(WorkflowError):
            enqueue(self.store, task["id"], "script", command=[sys.executable, "-V"], auto_resume=True)
        self.config["roles"]["default"]["backend"] = "api"
        self.config["paid_api"].update(enabled=True, model="configured", input_usd_per_million="1",
                                       output_usd_per_million="1", monthly_limit_usd="1")
        save_version(self.home, self.config)
        with self.assertRaises(WorkflowError):
            enqueue(self.store, task["id"], "codex", allow_api=True, auto_resume=True)
        self.assertEqual(self.store.rows("SELECT * FROM jobs"), [])
        self.assertEqual(self.store.rows("SELECT * FROM reservations"), [])

    def test_auto_authorization_cannot_be_added_by_reusing_an_old_key(self):
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "codex", prompt="原指令")
        with self.assertRaises(WorkflowError):
            enqueue(self.store, task["id"], "codex", prompt="原指令", auto_resume=True)
        self.assertNotIn("auto_resume", json.loads(self.store.get("jobs", job["id"])["snapshot"]))

    def test_automatic_resume_reuses_session_and_one_persisted_intent(self):
        parent = self.waiting_job()
        child, created = self.resume(parent)
        duplicate, created_again = self.resume(parent)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(duplicate["id"], child["id"])
        self.assertEqual(child["session_id"], "specific-session")
        metadata = json.loads(child["snapshot"])["auto_resume"]
        self.assertEqual(metadata["root_job_id"], parent["id"])
        self.assertEqual(metadata["parent_job_id"], parent["id"])
        events = self.store.rows("SELECT * FROM events WHERE kind='capacity.resumed'")
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]["payload"])["job_id"], child["id"])
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 2)

    def test_second_capacity_failure_waits_for_a_new_cycle(self):
        parent = self.waiting_job()
        child, _ = self.resume(parent)
        finish(self.store, child["id"], "WAITING_CAPACITY", 1, "CAPACITY")
        child = self.store.get("jobs", child["id"])
        with self.assertRaises(WorkflowError):
            self.resume(child)
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 2)
        grandchild, created = self.resume(child, cycle="cycle-2")
        self.assertTrue(created)
        self.assertEqual(grandchild["session_id"], "specific-session")

    def test_cancel_waiting_capacity_revokes_future_dispatch(self):
        from ccaw.capacity import is_enabled
        parent = self.waiting_job()
        self.assertTrue(is_enabled(self.store, parent))
        cancelled = cancel(self.store, parent["id"])
        self.assertEqual(cancelled["id"], parent["id"])
        self.assertFalse(is_enabled(self.store, cancelled))
        with self.assertRaises(WorkflowError):
            self.resume(cancelled)
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 1)

    def test_start_checks_revocation_before_creating_worker(self):
        from ccaw.capacity import disable
        parent = self.waiting_job()
        child, _ = self.resume(parent)
        disable(self.store, parent["id"])
        with patch("ccaw.runner.subprocess.Popen") as launch:
            try:
                result = start(self.store, child["id"])
            except WorkflowError:
                result = self.store.get("jobs", child["id"])
        launch.assert_not_called()
        self.assertNotIn(result["status"], {"STARTING", "RUNNING"})

    def test_capacity_worker_wakes_watcher_only_for_opted_in_jobs(self):
        code = ("import json,sys;sys.stdin.read();"
                "print(json.dumps({'type':'thread.started','thread_id':'specific-session'}),flush=True);"
                "print(json.dumps({'type':'turn.failed','error':{'message':'usage limit reached'}}),flush=True);"
                "sys.exit(1)")
        for opted_in in (False, True):
            with self.subTest(auto_resume=opted_in):
                job, _ = enqueue(self.store, self.task()["id"], "codex", auto_resume=opted_in)
                self.store.db.execute("UPDATE jobs SET status='STARTING' WHERE id=?", (job["id"],))
                with patch("ccaw.runner.codex_command", return_value=[sys.executable, "-u", "-c", code]), \
                        patch("ccaw.runner.subscription_environment", return_value=os.environ.copy()), \
                        patch("ccaw.capacity.read_capacity", return_value={"account_id": "account-1", "next_check_at": 0}), \
                        patch("ccaw.capacity.ensure_watcher") as wake:
                    run_worker(self.home, job["id"])
                current = self.store.get("jobs", job["id"])
                self.assertEqual(current["status"], "WAITING_CAPACITY")
                self.assertEqual(current["session_id"], "specific-session")
                self.assertEqual(wake.call_count, int(opted_in))
                if opted_in:
                    self.assertEqual(wake.call_args.args[1], job["id"])

    def test_initial_account_is_bound_before_model_and_failed_read_blocks_launch(self):
        job, _ = enqueue(self.store, self.task()["id"], "codex", auto_resume=True)
        self.store.db.execute("UPDATE jobs SET status='STARTING' WHERE id=?", (job["id"],))
        with patch("ccaw.capacity.read_capacity", side_effect=WorkflowError("无法核对额度账号")), \
                patch("ccaw.runner.codex_command") as command:
            run_worker(self.home, job["id"])
        command.assert_not_called()
        self.assertEqual(self.store.get("jobs", job["id"])["status"], "FAILED")
        self.assertEqual(self.store.get("jobs", job["id"])["error_kind"], "AUTO_RESUME_SETUP")
        self.assertEqual(self.store.rows("SELECT * FROM events WHERE kind='capacity.account'"), [])

    def test_acceptance_cannot_race_an_automatic_enqueue(self):
        parent = self.waiting_job()
        transaction = Store.transaction
        injected = False

        @contextmanager
        def enqueue_before_claim(store):
            nonlocal injected
            if not injected and store is not self.store:
                injected = True
                self.resume(parent)
            with transaction(store):
                yield

        with patch.object(Store, "transaction", enqueue_before_claim):
            code, output, error = self.cli("task", "done", parent["task_id"], "--evidence", "检查完成")
        self.assertTrue(injected)
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("未完成", error)
        self.assertNotEqual(self.store.get("tasks", parent["task_id"])["status"], "DONE")

    def test_cli_opt_in_watch_unwatch_and_diagnostics(self):
        task = self.task()
        with patch("ccaw.cli.start", side_effect=lambda store, job_id: store.get("jobs", job_id)):
            code, output, error = self.cli("job", "agent", task["id"], "--auto-resume")
        self.assertEqual((code, error), (0, ""))
        job = json.loads(output)["job"]
        self.assertEqual(json.loads(job["snapshot"])["auto_resume"]["root_job_id"], job["id"])
        self.store.db.execute("UPDATE jobs SET session_id='specific-session' WHERE id=?", (job["id"],))
        finish(self.store, job["id"], "WAITING_CAPACITY", 1, "CAPACITY")
        report = {"enabled": True, "reason": "本地等待", "watcher_running": False}
        launch = {"launched": True, "pid": 123}
        with patch("ccaw.capacity.ensure_watcher", return_value=launch) as wake, \
                patch("ccaw.capacity.status", return_value=dict(report)):
            code, output, error = self.cli("job", "watch", job["id"])
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output), dict(report, launch=launch, watcher_starting=True))
        self.assertEqual(wake.call_args.args[1], job["id"])
        with patch("ccaw.capacity.status", return_value=report):
            code, output, error = self.cli("job", "show", job["id"])
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["diagnostics"]["auto_resume"], report)
        self.assertIn("job unwatch", json.loads(output)["diagnostics"]["next_action"])
        code, output, error = self.cli("job", "unwatch", job["id"])
        self.assertEqual((code, error), (0, ""))
        self.assertFalse(json.loads(output)["enabled"])

    def test_cli_watch_reports_known_start_failure_with_nonzero_exit(self):
        parent = self.waiting_job()
        report = {"enabled": True, "reason": "启动失败", "watcher_running": False}
        launch = {"launched": False, "watcher_running": False}
        with patch("ccaw.capacity.ensure_watcher", return_value=launch), \
                patch("ccaw.capacity.status", return_value=report):
            code, output, error = self.cli("job", "watch", parent["id"])
        self.assertEqual((code, error), (2, ""))
        self.assertEqual(json.loads(output)["launch"], launch)
        self.assertFalse(json.loads(output)["watcher_starting"])

    def test_cli_help_describes_opt_in_and_wait_controls(self):
        for arguments, expected in ((["job", "agent", "--help"], ["--auto-resume"]),
                                    (["job", "--help"], ["watch", "unwatch"])):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as exit_code:
                    main(arguments)
                self.assertEqual(exit_code.exception.code, 0)
                for text in expected:
                    self.assertIn(text, output.getvalue())


if __name__ == "__main__":
    unittest.main()
