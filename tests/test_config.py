import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ccaw.config import DEFAULT_CONFIG, WorkflowError, load, validate


class ConfigTests(unittest.TestCase):
    def test_default_and_explicit_model_configuration_are_valid(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        validate(config)
        config["roles"]["economy"].update(backend="api", model="configured")
        config["paid_api"].update(model="configured", enabled=True,
                                  input_usd_per_million="0.1", output_usd_per_million=1,
                                  monthly_limit_usd=2.5)
        validate(config)

    def test_configuration_objects_require_the_known_fields(self):
        for path in ((), ("codex",), ("roles",), ("roles", "default"), ("paid_api",)):
            for change in ("missing", "extra"):
                with self.subTest(path=path, change=change):
                    config = copy.deepcopy(DEFAULT_CONFIG)
                    target = config
                    for key in path:
                        target = target[key]
                    if change == "missing":
                        target.pop(next(iter(target)))
                    else:
                        target["unexpected"] = "value"
                    with self.assertRaises(WorkflowError):
                        validate(config)

    def test_invalid_json_values_raise_readable_configuration_errors(self):
        invalid = [
            (("schema_version",), [True, 1.0, "1", None, [], {}]),
            (("revision",), [True, 0, -1, 1.0, "1", None, [], {}]),
            (("max_lessons",), [True, -1, 21, 1.0, "1", None, [], {}]),
            (("codex",), [None, [], "codex"]),
            (("roles",), [None, [], "roles"]),
            (("paid_api",), [None, [], "api"]),
            (("codex", "executable"), [None, [], {}, True, 1, ""]),
            (("codex", "sandbox"), [None, [], {}, True, 1, "invalid"]),
            (("roles", "default"), [None, [], "codex"]),
            (("roles", "default", "backend"), [None, [], {}, True, 1, "invalid"]),
            (("roles", "default", "model"), [[], {}, True, 1]),
            (("paid_api", "model"), [None, [], {}, True, 1]),
            (("paid_api", "enabled"), [None, [], {}, 1, "true"]),
            (("paid_api", "reasoning_effort"), [None, [], {}, True, 1, "invalid"]),
            (("paid_api", "max_output_tokens"), [None, [], {}, True, 0, 1.0, "1"]),
            (("paid_api", "max_input_bytes"), [None, [], {}, True, 0, 1.0, "1"]),
            (("paid_api", "monthly_limit_usd"), [None, [], {}, True, -1, "NaN", "Infinity"]),
            (("paid_api", "input_usd_per_million"), [[], {}, True, -1, "NaN", "Infinity"]),
            (("paid_api", "output_usd_per_million"), [[], {}, True, -1, "NaN", "Infinity"]),
            (("paid_api", "key_env"), [None, [], {}, True, 1, ""]),
            (("paid_api", "base_url"), [None, [], {}, True, 1, "", "http://example.com",
                                         "https://[invalid", "https://example.com:invalid",
                                         "https://user:pass@example.com", "https://example.com?key=secret"]),
        ]
        for path, values in invalid:
            for value in values:
                with self.subTest(path=path, value=value):
                    config = copy.deepcopy(DEFAULT_CONFIG)
                    target = config
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    with self.assertRaises(WorkflowError) as error:
                        validate(config)
                    self.assertTrue(str(error.exception))

    def test_load_accepts_utf8_bom_from_windows_editors(self):
        data = b"\xef\xbb\xbf" + json.dumps(DEFAULT_CONFIG).encode("utf-8")

        def open_config(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
            return io.TextIOWrapper(io.BytesIO(data), encoding=encoding, errors=errors, newline=newline)

        with patch.object(Path, "open", autospec=True, side_effect=open_config):
            self.assertEqual(load("unused"), DEFAULT_CONFIG)


if __name__ == "__main__":
    unittest.main()
