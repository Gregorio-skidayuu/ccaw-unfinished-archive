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

from ccaw.capacity_api import parse_capacity, read_capacity
from ccaw.config import DEFAULT_CONFIG, WorkflowError


ACCOUNT = {"account": {"type": "chatgpt", "email": "private@example.test", "id": "account-one"}}
LIMITS = {"accountId": "account-one", "ordinaryUsageAllowed": True,
          "rateLimitsByLimitId": {"codex": {"limitId": "codex",
              "primary": {"usedPercent": 10, "resetsAt": 1400},
              "secondary": {"usedPercent": 20, "resetsAt": 5000},
              "rateLimitReachedType": None, "spendControlReached": False}}}


class CapacityParsingTests(unittest.TestCase):
    def test_allowed_signal_and_cycle_are_explicit_and_stable(self):
        result = parse_capacity(ACCOUNT, LIMITS, now=1000)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["account_id"], "account-one")
        self.assertEqual(result["next_check_at"], 1300)
        changed = copy.deepcopy(LIMITS)
        changed["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = 11
        self.assertEqual(parse_capacity(ACCOUNT, changed, now=1000)["cycle"], result["cycle"])
        changed["rateLimitsByLimitId"]["codex"]["secondary"]["resetsAt"] += 1
        self.assertNotEqual(parse_capacity(ACCOUNT, changed, now=1000)["cycle"], result["cycle"])

    def test_percentages_and_past_reset_do_not_replace_permission(self):
        for value in (None, False, 0, 1, "true"):
            with self.subTest(value=value):
                limits = copy.deepcopy(LIMITS)
                limits["ordinaryUsageAllowed"] = value
                limits["rateLimitsByLimitId"]["codex"]["primary"].update(usedPercent=0, resetsAt=1)
                self.assertFalse(parse_capacity(ACCOUNT, limits, now=1000)["allowed"])
        limits.pop("ordinaryUsageAllowed")
        self.assertFalse(parse_capacity(ACCOUNT, limits, now=1000)["allowed"])

    def test_blocked_windows_wait_until_latest_reset_and_account_limits_block(self):
        limits = copy.deepcopy(LIMITS)
        bucket = limits["rateLimitsByLimitId"]["codex"]
        bucket["primary"]["usedPercent"] = bucket["secondary"]["usedPercent"] = 100
        result = parse_capacity(ACCOUNT, limits, now=1000)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["next_check_at"], 5010)
        for field, value in (("rateLimitReachedType", "rate_limit_reached"),
                             ("spendControlReached", True), ("spendControlReached", "unknown")):
            limits = copy.deepcopy(LIMITS)
            limits["rateLimitsByLimitId"]["codex"][field] = value
            self.assertFalse(parse_capacity(ACCOUNT, limits, now=1000)["allowed"])

    def test_missing_or_malformed_windows_never_allow_resume(self):
        for value in (None, [], {}, {"usedPercent": True, "resetsAt": 1400},
                      {"usedPercent": -1, "resetsAt": 1400}, {"usedPercent": 10, "resetsAt": "1400"}):
            with self.subTest(value=value):
                limits = copy.deepcopy(LIMITS)
                limits["rateLimitsByLimitId"]["codex"] = {"primary": value}
                self.assertFalse(parse_capacity(ACCOUNT, limits, now=1000)["allowed"])
        for buckets in ({}, [], {"other": LIMITS["rateLimitsByLimitId"]["codex"]}):
            limits = copy.deepcopy(LIMITS)
            limits["rateLimitsByLimitId"] = buckets
            self.assertFalse(parse_capacity(ACCOUNT, limits, now=1000)["allowed"])
        limits = copy.deepcopy(LIMITS)
        limits["rateLimitsByLimitId"]["codex"]["limitId"] = "other"
        self.assertFalse(parse_capacity(ACCOUNT, limits, now=1000)["allowed"])

    def test_legacy_codex_bucket_and_hashed_email_identity(self):
        account = {"account": {"type": "chatgpt", "email": "private@example.test"}}
        limits = {"ordinaryUsageAllowed": True, "rateLimits": LIMITS["rateLimitsByLimitId"]["codex"]}
        result = parse_capacity(account, limits, now=1000)
        self.assertTrue(result["allowed"])
        self.assertTrue(result["account_id"].startswith("email-sha256:"))
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(parse_capacity(account, limits, now=1000)["account_id"], result["account_id"])

    def test_wrong_auth_and_mismatched_identity_are_safe_errors(self):
        accounts = [{"account": {"type": "apiKey"}}, {"account": None}]
        for account in accounts:
            with self.assertRaises(WorkflowError):
                parse_capacity(account, LIMITS, now=1000)
        limits = copy.deepcopy(LIMITS)
        limits["accountId"] = "other-account"
        with self.assertRaises(WorkflowError) as error:
            parse_capacity(ACCOUNT, limits, now=1000)
        self.assertNotIn("other-account", str(error.exception))


class CapacityProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ccaw-capacity-api-")
        self.root = Path(self.temp.name)
        self.processes = []
        self.fixture = self.root / "fake_server.py"
        self.log = self.root / "requests.json"
        self.fixture.write_text(f'''import json,sys,time
from pathlib import Path
mode=sys.argv[1]
seen=[]
for line in sys.stdin:
    message=json.loads(line)
    seen.append(message.get('method'))
    Path({str(self.log)!r}).write_text(json.dumps(seen),encoding='utf-8')
    if 'id' not in message:
        continue
    if mode=='stall':
        time.sleep(10)
    if mode=='server-request':
        print(json.dumps({{"id":91,"method":"account/chatgptAuthTokens/refresh","params":{{}}}}),flush=True)
        continue
    if mode=='error':
        print(json.dumps({{"id":message['id'],"error":{{"message":"private-key-and-body"}}}}),flush=True)
        continue
    if mode=='malformed':
        print('private-key-and-body',flush=True)
        continue
    print(json.dumps({{"method":"account/rateLimits/updated","params":{{}}}}),flush=True)
    print(json.dumps({{"id":999,"result":{{}}}}),flush=True)
    method=message['method']
    result={{}} if method=='initialize' else ({ACCOUNT!r} if method=='account/read' else {LIMITS!r})
    print(json.dumps({{"id":message['id'],"result":result}}),flush=True)
''', encoding="utf-8")

    def tearDown(self):
        for child in self.processes:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
        self.temp.cleanup()

    def query(self, mode="normal", timeout=2):
        popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = popen(*args, **kwargs)
            self.processes.append(child)
            return child
        with patch("ccaw.capacity_api.shutil.which", return_value=sys.executable), \
                patch("ccaw.capacity_api.subscription_environment", return_value=os.environ.copy()), \
                patch("ccaw.capacity_api._app_server_command", return_value=[sys.executable, str(self.fixture), mode]), \
                patch("ccaw.capacity_api.subprocess.Popen", side_effect=launch), \
                patch("ccaw.capacity_api.REQUEST_TIMEOUT", timeout):
            return read_capacity(DEFAULT_CONFIG)

    def assert_closed(self):
        self.assertEqual(len(self.processes), 1)
        child = self.processes[0]
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)

    def test_real_pipe_handshake_ignores_notifications_and_never_starts_turn(self):
        result = self.query()
        self.assertTrue(result["allowed"])
        self.assertEqual(json.loads(self.log.read_text(encoding="utf-8")),
                         ["initialize", "initialized", "account/read", "account/rateLimits/read"])
        self.assert_closed()

    def test_timeout_terminates_and_reaps_helper(self):
        started = time.monotonic()
        with self.assertRaises(WorkflowError) as error:
            self.query(mode="stall", timeout=.2)
        self.assertIn("超时", str(error.exception))
        self.assertLess(time.monotonic() - started, 3)
        self.assert_closed()

    def test_rpc_errors_malformed_output_and_server_requests_do_not_leak(self):
        for mode in ("error", "malformed", "server-request"):
            with self.subTest(mode=mode):
                self.processes = []
                with self.assertRaises(WorkflowError) as error:
                    self.query(mode=mode)
                self.assertNotIn("private-key-and-body", str(error.exception))
                self.assert_closed()


if __name__ == "__main__":
    unittest.main()
