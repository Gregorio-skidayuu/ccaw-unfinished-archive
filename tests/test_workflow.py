import copy
from contextlib import closing
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ccaw.config import DEFAULT_CONFIG, WorkflowError, load, route, save_version
from ccaw.context import task_packet
from ccaw.processes import identity, same_process, terminate
from ccaw.providers import APIExecutionUncertain, api_reservation, call_api, codex_command, subscription_environment
from ccaw.runner import cancel, continuation, enqueue, finish, reconcile, run_worker, start, wait_for
from ccaw.store import Store


SOURCE = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = self.base / "工程 with spaces"
        self.project.mkdir()
        self.home = self.base / "状态目录"
        self.store = Store.initialize(self.home)
        save_version(self.home, copy.deepcopy(DEFAULT_CONFIG))
        self.config = load(self.home)

    def tearDown(self):
        for row in self.store.rows("SELECT * FROM jobs WHERE status IN ('STARTING','RUNNING','UNKNOWN')"):
            try:
                cancel(self.store, row["id"])
                wait_for(self.store, row["id"], 3)
            except WorkflowError:
                pass
        # A terminal state is persisted before the detached worker closes its logs.
        deadline = time.monotonic() + 5
        while any(row["pid"] != os.getpid() and same_process(row["pid"], row["process_token"])
                  for row in self.store.rows("SELECT pid,process_token FROM jobs")):
            if time.monotonic() >= deadline:
                self.fail("Detached worker did not exit before temporary-directory cleanup")
            time.sleep(.05)
        self.store.close()
        self.temp.cleanup()

    def task(self, tags=None):
        return self.store.create_task("实验", "写入成果一次", "计数为一", self.project, tags or ["recovery"], "code", "medium", "medium", "default")

    def run_script(self, task, code="print('完成')", key="main"):
        job, _ = enqueue(self.store, task["id"], "script", command=[sys.executable, "-c", code], key=key)
        start(self.store, job["id"])
        result = wait_for(self.store, job["id"], 8)
        self.assertEqual(result["status"], "SUCCEEDED", result)
        return result

    def test_detached_job_survives_launcher_exit_and_deduplicates(self):
        task = self.task()
        command = [sys.executable, "-c", "import time; from pathlib import Path; time.sleep(.4); p=Path('count'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]
        invocation = [sys.executable, "-m", "ccaw", "--home", str(self.home), "job", "run", task["id"], "--key", "experiment", "--", *command]
        first = subprocess.run(invocation, cwd=SOURCE, capture_output=True, text=True, encoding="utf-8", timeout=5)
        self.assertEqual(first.returncode, 0, first.stderr)
        job = json.loads(first.stdout)["job"]
        duplicate, created = enqueue(self.store, task["id"], "script", command=command, key="experiment")
        self.assertFalse(created)
        self.assertEqual(duplicate["id"], job["id"])
        self.assertEqual(wait_for(self.store, job["id"], 8)["status"], "SUCCEEDED")
        self.assertEqual((self.project / "count").read_text(), "x")
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 1)

    def test_same_key_different_work_is_rejected_and_project_is_serial(self):
        task = self.task()
        enqueue(self.store, task["id"], "script", command=[sys.executable, "-V"])
        with self.assertRaises(WorkflowError):
            enqueue(self.store, task["id"], "script", command=[sys.executable, "-c", "print(1)"])
        with self.assertRaises(WorkflowError):
            enqueue(self.store, self.task()["id"], "script", command=[sys.executable, "-V"])

    def test_worker_claim_is_once(self):
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "script", command=[sys.executable, "-c", "from pathlib import Path; p=Path('once'); p.write_text('x')"])
        self.store.db.execute("UPDATE jobs SET status='STARTING' WHERE id=?", (job["id"],))
        run_worker(self.home, job["id"])
        run_worker(self.home, job["id"])
        self.assertEqual((self.project / "once").read_text(), "x")
        self.assertEqual(len(self.store.rows("SELECT * FROM events WHERE kind='job.finished'")), 1)

    def test_uncertain_start_blocks_restart(self):
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "script", command=[sys.executable, "-V"])
        self.store.db.execute("UPDATE jobs SET status='STARTING',pid=999999999,process_token='old' WHERE id=?", (job["id"],))
        self.assertEqual(reconcile(self.store, job["id"])["status"], "UNKNOWN")
        self.assertEqual(start(self.store, job["id"])["status"], "UNKNOWN")
        with self.assertRaises(WorkflowError):
            enqueue(self.store, task["id"], "script", command=[sys.executable, "-V"], key="retry")

    def test_post_launch_identity_error_blocks_new_work(self):
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "script", command=[sys.executable, "-c", "from pathlib import Path; import time; Path('started').write_text('once'); time.sleep(30)"])
        children = []
        popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            deadline = time.monotonic() + 5
            while not (self.project / "started").exists() and time.monotonic() < deadline:
                time.sleep(.05)
            return child
        try:
            with patch("ccaw.runner.subprocess.Popen", side_effect=launch), \
                    patch("ccaw.runner.identity", side_effect=OSError("simulated post-launch lookup failure")):
                result = start(self.store, job["id"])
            self.assertEqual((self.project / "started").read_text(), "once")
            self.assertIsNone(children[0].poll())
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(self.store.get("tasks", task["id"])["status"], "WAITING_INPUT")
            with self.assertRaises(WorkflowError):
                enqueue(self.store, task["id"], "script", command=[sys.executable, "-V"], key="retry")
        finally:
            cancel(self.store, job["id"])
            for child in children:
                child.wait(timeout=5)

    def test_post_launch_identity_error_preserves_completed_worker(self):
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "script", command=[sys.executable, "-V"])
        popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = popen(*args, **kwargs)
            child.wait(timeout=5)
            return child
        with patch("ccaw.runner.subprocess.Popen", side_effect=launch), \
                patch("ccaw.runner.identity", side_effect=OSError("simulated post-launch lookup failure")):
            self.assertEqual(start(self.store, job["id"])["status"], "SUCCEEDED")
        self.assertEqual(self.store.get("tasks", task["id"])["status"], "VERIFYING")
        self.assertEqual(self.store.rows("SELECT * FROM events WHERE kind='job.unknown'"), [])

    def test_worker_creation_failure_is_a_confirmed_failure(self):
        job, _ = enqueue(self.store, self.task()["id"], "script", command=[sys.executable, "-V"])
        with patch("ccaw.runner.subprocess.Popen", side_effect=FileNotFoundError("worker executable missing")):
            result = start(self.store, job["id"])
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_kind"], "START_FAILURE")

    def test_reconcile_does_not_overwrite_a_concurrent_completion(self):
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "script", command=[sys.executable, "-V"])
        self.store.db.execute("UPDATE jobs SET status='RUNNING',pid=999999999,process_token='old' WHERE id=?", (job["id"],))
        def completed_before_health_check_returns(pid, token):
            finish(self.store, job["id"], "SUCCEEDED", 0)
            return False
        with patch("ccaw.runner.same_process", side_effect=completed_before_health_check_returns):
            self.assertEqual(reconcile(self.store, job["id"])["status"], "SUCCEEDED")
        self.assertEqual(self.store.get("tasks", task["id"])["status"], "VERIFYING")
        self.assertEqual(self.store.rows("SELECT * FROM events WHERE kind='job.unknown'"), [])

    def test_worker_error_with_live_child_preserves_unknown_and_blocks_new_work(self):
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "script", command=[sys.executable, "-c", "import time; time.sleep(30)"])
        self.store.db.execute("UPDATE jobs SET status='STARTING' WHERE id=?", (job["id"],))
        children = []
        popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child
        def identify(pid):
            if pid == os.getpid():
                return identity(pid)
            raise OSError("simulated child identity lookup failure")
        try:
            with patch("ccaw.runner.subprocess.Popen", side_effect=launch), patch("ccaw.runner.identity", side_effect=identify):
                run_worker(self.home, job["id"])
            self.assertIsNone(children[0].poll())
            self.assertEqual(self.store.get("jobs", job["id"])["status"], "UNKNOWN")
            self.assertEqual(self.store.get("tasks", task["id"])["status"], "WAITING_INPUT")
            with self.assertRaises(WorkflowError):
                enqueue(self.store, task["id"], "script", command=[sys.executable, "-V"], key="retry")
        finally:
            for child in children:
                if child.poll() is None:
                    terminate(child.pid, identity(child.pid))
                child.wait(timeout=5)

    def test_cancel_running_program(self):
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "script", command=[sys.executable, "-c", "import time; time.sleep(30)"])
        start(self.store, job["id"])
        deadline = time.monotonic() + 5
        while not self.store.get("jobs", job["id"])["child_token"] and time.monotonic() < deadline:
            time.sleep(.05)
        cancel(self.store, job["id"])
        self.assertEqual(wait_for(self.store, job["id"], 5)["status"], "CANCELLED")

    def test_process_birth_mismatch_is_not_signalled(self):
        self.assertTrue(same_process(os.getpid(), identity(os.getpid())))
        self.assertFalse(same_process(os.getpid(), "another-process"))
        with self.assertRaises(WorkflowError):
            terminate(os.getpid(), "another-process")

    def test_correction_must_be_verified_scoped_and_revocable(self):
        task = self.task()
        old_check = self.run_script(task, key="before-candidate")
        incident = self.store.record_incident(task["id"], "duplicate-run", "重复实验", "logs/original.log")
        lesson = self.store.candidate(incident["id"], "缺少启动回执", "恢复先核对唯一运行 ID", ["recovery"])
        with self.assertRaises(WorkflowError):
            self.store.set_lesson_status(lesson["id"], "ACTIVE")
        with self.assertRaises(WorkflowError):
            self.store.verify_lesson(lesson["id"], old_check["id"])
        check = self.run_script(task, key="regression")
        self.store.verify_lesson(lesson["id"], check["id"])
        self.store.set_lesson_status(lesson["id"], "ACTIVE")
        self.assertEqual(len(task_packet(self.store, task["id"])["lessons"]), 1)
        other = self.base / "other-project"
        other.mkdir()
        self.assertEqual(self.store.match_lessons(str(other), ["recovery"], 5), [])
        self.assertEqual(self.store.match_lessons(str(self.project), ["unrelated"], 5), [])
        self.store.record_incident(task["id"], "duplicate-run", "再次复发", "logs/second.log")
        self.assertEqual(self.store.get("lessons", lesson["id"])["recurrence_count"], 1)
        self.store.set_lesson_status(lesson["id"], "REVOKED")
        self.assertEqual(task_packet(self.store, task["id"])["lessons"], [])
        revised = self.store.revise_lesson(lesson["id"], "根因更新", "新修正")
        self.assertEqual(revised["version"], 2)
        self.assertIsNone(revised["verification_job"])

    def test_paid_api_disabled_and_reservations_enforce_shared_budget(self):
        task = self.task()
        self.config["roles"]["economy"]["backend"] = "api"
        save_version(self.home, self.config)
        with self.assertRaises(WorkflowError):
            enqueue(self.store, task["id"], "api", prompt="review", role="economy", allow_api=True)
        self.assertEqual(self.store.rows("SELECT * FROM jobs"), [])
        self.config["paid_api"].update(enabled=True, model="user-verified-model", input_usd_per_million="1", output_usd_per_million="1", monthly_limit_usd="0.02")
        save_version(self.home, self.config)
        with self.assertRaises(WorkflowError):
            enqueue(self.store, task["id"], "api", prompt="review", role="economy")
        job, _ = enqueue(self.store, task["id"], "api", prompt="review", role="economy", allow_api=True)
        self.assertTrue(self.store.rows("SELECT * FROM reservations"))
        finish(self.store, job["id"], "FAILED", None)
        self.config["paid_api"]["monthly_limit_usd"] = "0.001"
        save_version(self.home, self.config)
        with self.assertRaises(WorkflowError):
            enqueue(self.store, task["id"], "api", prompt="again", role="economy", key="second", allow_api=True)

    def test_api_completion_and_usage_without_live_request(self):
        self.config["paid_api"].update(enabled=True, model="configured", input_usd_per_million="2", output_usd_per_million="4")
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"choices": [{"message": {"content": "reviewed"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}}).encode()
        opener = unittest.mock.MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key-never-written"}), patch("urllib.request.build_opener", return_value=opener):
            text, actual, usage, completed = call_api(self.config, "sample")
        self.assertEqual(text, "reviewed")
        self.assertTrue(completed)
        self.assertEqual(actual, Decimal("0.00028"))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")

    def fake_codex(self, *, capacity=False, incomplete=False, non_object=False):
        path = self.base / ("fake-codex.py" if os.name == "nt" else "fake-codex")
        source = f'''#!{sys.executable}
import json,sys
from pathlib import Path
args=sys.argv[1:]
if args==['login','status']:
    print('Logged in using ChatGPT');sys.exit(0)
Path('codex-args.json').write_text(json.dumps(args))
sys.stdin.read()
{ "print('[]',flush=True);print('null',flush=True)" if non_object else "" }
print(json.dumps({{"type":"thread.started","thread_id":"specific-session"}}),flush=True)
'''
        if capacity:
            source += 'print(json.dumps({"type":"turn.failed","error":{"message":"usage limit reached"}}));sys.exit(1)\n'
        elif not incomplete:
            source += 'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":3}}))\n'
        path.write_text(source, encoding="utf-8")
        self.fake_codex_path = path
        if os.name != "nt":
            path.chmod(0o755)
        self.config["codex"]["executable"] = str(path)
        save_version(self.home, self.config)
        return path

    def start_fake_codex_job(self, job_id):
        if os.name != "nt":
            return start(self.store, job_id)
        # Windows cannot execute a shebang fixture. Use the real Python process
        # through the same worker protocol; detached scripts are tested separately.
        def command(config, role, output, session=None):
            config = copy.deepcopy(config)
            config["codex"]["executable"] = sys.executable
            argv = codex_command(config, role, output, session)
            return [argv[0], str(self.fake_codex_path), *argv[1:]]
        self.store.db.execute("UPDATE jobs SET status='STARTING' WHERE id=?", (job_id,))
        with patch("ccaw.runner.codex_command", side_effect=command), \
                patch("ccaw.runner.subscription_environment", return_value=os.environ.copy()), \
                patch("ccaw.runner.start", side_effect=lambda store, identity: self.start_fake_codex_job(identity)):
            run_worker(self.home, job_id)
        return self.store.get("jobs", job_id)

    def test_codex_capacity_persists_session_and_resume_is_explicit(self):
        self.fake_codex(capacity=True)
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "codex", prompt="first")
        self.start_fake_codex_job(job["id"])
        result = wait_for(self.store, job["id"], 8)
        self.assertEqual(result["status"], "WAITING_CAPACITY", result)
        self.assertEqual(result["session_id"], "specific-session")
        self.fake_codex()
        following, _ = enqueue(self.store, task["id"], "codex", prompt="continue", key="resume-1", resume_job=job["id"])
        self.start_fake_codex_job(following["id"])
        self.assertEqual(wait_for(self.store, following["id"], 8)["status"], "SUCCEEDED")
        argv = json.loads((self.project / "codex-args.json").read_text())
        self.assertIn("specific-session", argv)
        self.assertNotIn("--last", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_codex_exit_zero_without_completion_is_failure(self):
        self.fake_codex(incomplete=True)
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "codex")
        self.start_fake_codex_job(job["id"])
        self.assertEqual(wait_for(self.store, job["id"], 8)["status"], "FAILED")

    def test_codex_ignores_non_object_jsonl(self):
        self.fake_codex(non_object=True)
        job, _ = enqueue(self.store, self.task()["id"], "codex")
        self.start_fake_codex_job(job["id"])
        self.assertEqual(wait_for(self.store, job["id"], 8)["status"], "SUCCEEDED")

    def test_success_event_continuation_is_once(self):
        self.fake_codex()
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "script", command=[sys.executable, "-c", "print('result')"], then_prompt="分析结果")
        self.start_fake_codex_job(job["id"])
        self.assertEqual(wait_for(self.store, job["id"], 8)["status"], "SUCCEEDED")
        deadline = time.monotonic() + 8
        while not self.store.get("jobs", job["id"])["continuation_job"] and time.monotonic() < deadline:
            time.sleep(.05)
        following = self.store.get("jobs", job["id"])["continuation_job"]
        self.assertIsNotNone(following)
        self.assertEqual(wait_for(self.store, following, 8)["status"], "SUCCEEDED")
        continuation(self.store, job["id"])
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 2)

    def test_database_backup_and_future_version_guard(self):
        task = self.task()
        path = self.base / "backup.sqlite3"
        self.store.backup(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT title FROM tasks WHERE id=?", (task["id"],)).fetchone()[0], "实验")
        self.store.db.execute("PRAGMA user_version=999")
        with self.assertRaises(WorkflowError):
            Store(self.home)
        self.store.db.execute("PRAGMA user_version=1")

    def test_new_configuration_does_not_rewrite_queued_snapshot(self):
        task = self.task()
        job, _ = enqueue(self.store, task["id"], "script", command=[sys.executable, "-V"])
        self.config["roles"]["default"]["model"] = "replacement-model"
        save_version(self.home, self.config)
        saved = json.loads(self.store.get("jobs", job["id"])["snapshot"])
        self.assertIsNone(saved["config"]["roles"]["default"]["model"])

    def test_router_treats_high_risk_and_large_simple_work_differently(self):
        self.assertEqual(route("script", "low", "low", True)["role"], "script")
        self.assertEqual(route("code", "high", "low")["role"], "expert")
        self.assertEqual(route("code", "low", "medium", True)["role"], "economy")

    def test_subscription_auth_does_not_silently_use_api_key(self):
        response = subprocess.CompletedProcess([], 0, "Logged in using API key: do-not-log", "")
        with patch("ccaw.providers.subprocess.run", return_value=response) as run, patch.dict(os.environ, {"CODEX_API_KEY": "secret", "OPENAI_API_KEY": "secret"}):
            with self.assertRaises(WorkflowError) as error:
                subscription_environment("codex")
        self.assertNotIn("do-not-log", str(error.exception))
        self.assertNotIn("CODEX_API_KEY", run.call_args.kwargs["env"])
        self.assertNotIn("OPENAI_API_KEY", run.call_args.kwargs["env"])

    def test_linux_pidfd_resolves_namespace_pid_without_process_scan(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux namespace-specific regression")
        fields = ["S"] + ["0"] * 18 + ["321"]
        reads = {"/proc/self/fdinfo/77": "Pid:\t7000\nNSpid:\t7000\t123\n",
                 "/proc/7000/stat": "7000 (worker with spaces) " + " ".join(fields),
                 "/proc/sys/kernel/random/boot_id": "boot-identity\n"}
        with patch("ccaw.processes.os.pidfd_open", return_value=77), patch("ccaw.processes.os.close"), patch.object(Path, "read_text", lambda p: reads[str(p)]):
            self.assertEqual(identity(123), "linux:boot-identity:7000:321")

    def test_concurrent_submission_creates_one_persisted_intent(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        task = self.task()
        gate = Barrier(2)
        def submit():
            store = Store(self.home)
            try:
                gate.wait(timeout=3)
                job, created = enqueue(store, task["id"], "script", command=[sys.executable, "-V"])
                return job["id"], created
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(len({x[0] for x in results}), 1)
        self.assertEqual(sum(x[1] for x in results), 1)

    def api_job(self):
        self.config["roles"]["economy"]["backend"] = "api"
        self.config["paid_api"].update(enabled=True, model="configured", input_usd_per_million="1", output_usd_per_million="1", monthly_limit_usd="1")
        save_version(self.home, self.config)
        job, _ = enqueue(self.store, self.task()["id"], "api", prompt="review", role="economy", allow_api=True)
        self.store.db.execute("UPDATE jobs SET status='STARTING' WHERE id=?", (job["id"],))
        return job

    def test_truncated_api_output_is_retained_but_not_completed(self):
        job = self.api_job()
        with patch("ccaw.runner.call_api", return_value=("partial result", Decimal("0.0001"), {}, False)):
            run_worker(self.home, job["id"])
        self.assertEqual(self.store.get("jobs", job["id"])["error_kind"], "INCOMPLETE_OUTPUT")
        self.assertEqual(self.store.get("jobs", job["id"])["status"], "FAILED")
        self.assertEqual((self.home / "runs" / job["id"] / "result.md").read_text(), "partial result")

    def test_api_revocation_before_dispatch_prevents_request(self):
        job = self.api_job()
        self.config["paid_api"]["enabled"] = False
        save_version(self.home, self.config)
        with patch("ccaw.runner.call_api") as call:
            run_worker(self.home, job["id"])
            call.assert_not_called()
        self.assertEqual(self.store.get("jobs", job["id"])["status"], "FAILED")

    def test_api_uncertain_execution_blocks_new_work_and_retains_reservation(self):
        job = self.api_job()
        reserved = self.store.rows("SELECT * FROM reservations WHERE job_id=?", (job["id"],))[0]
        with patch("ccaw.runner.call_api", side_effect=APIExecutionUncertain("请求结果无法确认；先核对供应商记录。")):
            run_worker(self.home, job["id"])
        self.assertEqual(self.store.get("jobs", job["id"])["status"], "UNKNOWN")
        self.assertEqual(self.store.get("tasks", job["task_id"])["status"], "WAITING_INPUT")
        retained = self.store.rows("SELECT * FROM reservations WHERE job_id=?", (job["id"],))[0]
        self.assertEqual(retained["status"], "UNCERTAIN")
        self.assertEqual(retained["reserved_usd"], reserved["reserved_usd"])
        self.assertIsNone(retained["actual_usd"])
        with self.assertRaises(WorkflowError):
            enqueue(self.store, job["task_id"], "api", role="economy", prompt="again", key="retry", allow_api=True)


if __name__ == "__main__":
    unittest.main()
