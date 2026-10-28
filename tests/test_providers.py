import copy
from decimal import Decimal
from http.client import IncompleteRead
import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from ccaw.config import DEFAULT_CONFIG, WorkflowError
from ccaw.providers import APIExecutionUncertain, call_api


class ApiResponseTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config["paid_api"].update(model="configured", input_usd_per_million="2",
                                       output_usd_per_million="4")
        self.secret = "private-key-and-response"

    def request(self, *, data=None, open_error=None, read_error=None):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        if open_error is not None:
            opener.open.side_effect = open_error
        elif read_error is not None:
            response.read.side_effect = read_error
        else:
            response.read.return_value = data
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.secret}), \
                patch("ccaw.providers.urllib.request.build_opener", return_value=opener):
            try:
                return call_api(self.config, "sample")
            finally:
                opener.open.assert_called_once()

    def test_transport_and_incomplete_response_are_uncertain_without_retry(self):
        errors = [("open_error", URLError(self.secret)),
                  ("read_error", OSError(self.secret)),
                  ("read_error", IncompleteRead(self.secret.encode(), 100))]
        for argument, error in errors:
            with self.subTest(argument=argument, error=type(error).__name__):
                with self.assertRaises(APIExecutionUncertain) as caught:
                    self.request(**{argument: error})
                self.assertNotIn(self.secret, str(caught.exception))

    def test_invalid_json_or_response_shape_is_uncertain_without_retry(self):
        choice = {"message": {"content": "text"}, "finish_reason": "stop"}
        responses = [None, [], {}, {"choices": []}, {"choices": [None]},
                     {"choices": [{"message": {"content": None}}]},
                     {"choices": [choice], "usage": None},
                     {"choices": [choice], "usage": []},
                     {"choices": [choice], "usage": {"prompt_tokens": -1}},
                     {"choices": [choice], "usage": {"completion_tokens": True}}]
        payloads = [json.dumps(value).encode() for value in responses]
        payloads += [self.secret.encode(), b"\xff" + self.secret.encode()]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(APIExecutionUncertain) as caught:
                    self.request(data=payload)
                self.assertNotIn(self.secret, str(caught.exception))

    def test_http_error_remains_a_known_failure_and_hides_body(self):
        error = HTTPError("https://example.com", 429, self.secret, {}, io.BytesIO(self.secret.encode()))
        try:
            with self.assertRaises(WorkflowError) as caught:
                self.request(open_error=error)
            self.assertNotIsInstance(caught.exception, APIExecutionUncertain)
            self.assertIn("429", str(caught.exception))
            self.assertNotIn(self.secret, str(caught.exception))
        finally:
            error.close()

    def test_missing_key_does_not_send_a_request(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("ccaw.providers.urllib.request.build_opener") as build:
            with self.assertRaises(WorkflowError) as caught:
                call_api(self.config, "sample")
        self.assertNotIsInstance(caught.exception, APIExecutionUncertain)
        build.assert_not_called()

    def test_success_and_truncated_output_keep_text_and_usage(self):
        for reason, completed in (("stop", True), ("length", False)):
            with self.subTest(reason=reason):
                data = {"choices": [{"message": {"content": "retained text"}, "finish_reason": reason}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
                text, actual, usage, result = self.request(data=json.dumps(data).encode())
                self.assertEqual(text, "retained text")
                self.assertEqual(actual, Decimal("0.00028"))
                self.assertEqual(usage, data["usage"])
                self.assertEqual(result, completed)

    def test_success_without_usage_keeps_unknown_cost(self):
        data = {"choices": [{"message": {"content": "text"}, "finish_reason": "stop"}]}
        self.assertEqual(self.request(data=json.dumps(data).encode()), ("text", None, {}, True))


if __name__ == "__main__":
    unittest.main()
