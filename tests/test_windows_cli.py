import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell")


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell entry-point regression")
class WindowsCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="ccaw-windows-")
        cls.root = Path(cls.temp.name) / "中文 entry with spaces"
        cls.root.mkdir()
        shutil.copy2(SOURCE / "ccaw.ps1", cls.root)
        shutil.copytree(SOURCE / "ccaw", cls.root / "ccaw", ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(cls.root / ".venv")],
                       check=True, capture_output=True, timeout=60)
        cls.wrapper = cls.root / "ccaw.ps1"
        result = cls.powershell(f"& {literal(cls.wrapper)} init")
        if result.returncode:
            raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        # A worker commits terminal status before closing its Windows log handles.
        deadline = time.monotonic() + 5
        while True:
            try:
                cls.temp.cleanup()
                return
            except PermissionError as exc:
                if exc.winerror != 32 or time.monotonic() >= deadline:
                    raise
                time.sleep(.05)

    @classmethod
    def powershell(cls, script, timeout=60):
        script = "[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)\n" + script
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-EncodedCommand", encoded], cwd=cls.root, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout)

    def test_wrapper_preserves_arguments_and_json_pipeline(self):
        result_path = self.root / "argv.json"
        expected = ["中文 with spaces", "", 'embedded"quote', "C:\\trailing path\\", 'slash\\"quote']
        code = "import json,sys;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:],ensure_ascii=False),encoding='utf-8')"
        argv = " ".join(literal(value) for value in expected)
        script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$task = & {literal(self.wrapper)} task add '中文任务' --kind script --accept '参数保持原样' --project {literal(self.root)} | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $task.title -ne '中文任务') {{ throw 'Task JSON pipeline failed.' }}
$run = & {literal(self.wrapper)} job run $task.id --key argv --wait '--' {literal(self.root / '.venv/Scripts/python.exe')} -c {literal(code)} {literal(result_path)} {argv} | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $run.job.status -ne 'SUCCEEDED') {{ throw 'Job JSON pipeline failed.' }}
$run.job.command
"""
        result = self.powershell(script)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), expected)
        stored = json.loads(result.stdout)
        self.assertEqual(stored[-len(expected):], expected)

    def test_wrapper_preserves_failure_exit_code_and_stderr(self):
        result = self.powershell(f"& {literal(self.wrapper)} task show missing-task; exit $LASTEXITCODE")
        self.assertEqual(result.returncode, 2)
        self.assertIn("找不到", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_install_child_directory_excludes_state_and_repeated_install_preserves_tasks(self):
        source = self.root / "installer-source"
        source.mkdir()
        for name in ("Install.ps1", "ccaw.ps1", "README.md", "pyproject.toml", "AGENTS.md", ".gitignore"):
            shutil.copy2(SOURCE / name, source / name)
        shutil.copytree(SOURCE / "ccaw", source / "ccaw", ignore=shutil.ignore_patterns("__pycache__"))
        for name in (".ccaw", ".venv", ".validation-tmp"):
            (source / name).mkdir()
            (source / name / "private-sentinel").write_text("excluded", encoding="utf-8")
        (source / "old-release.zip").write_bytes(b"excluded")
        target = source / "installed child"
        installed = self.powershell(f"& {literal(source / 'Install.ps1')} -Destination {literal(target)}", timeout=120)
        self.assertEqual(installed.returncode, 0, installed.stderr + installed.stdout)
        self.assertTrue((target / ".ccaw/state.sqlite3").is_file())
        self.assertTrue((target / ".venv/Scripts/python.exe").is_file())
        for name in (".ccaw", ".venv"):
            self.assertFalse((target / name / "private-sentinel").exists())
        self.assertFalse((target / ".validation-tmp").exists())
        self.assertFalse((target / "old-release.zip").exists())
        self.assertFalse((target / "installed child").exists())
        rejected = self.powershell(f"& {literal(source / 'Install.ps1')} -Destination {literal(target)}")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("not empty", rejected.stderr)
        config = (target / ".ccaw/config.json").read_bytes()
        added = self.powershell(f"[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false); & {literal(target / 'ccaw.ps1')} task add '保留身份' --accept '身份不变' --project {literal(target)}")
        self.assertEqual(added.returncode, 0, added.stderr)
        task_id = json.loads(added.stdout)["id"]
        repeated = self.powershell(f"& {literal(target / 'Install.ps1')} -Destination {literal(target)}", timeout=120)
        self.assertEqual(repeated.returncode, 0, repeated.stderr + repeated.stdout)
        self.assertEqual((target / ".ccaw/config.json").read_bytes(), config)
        shown = self.powershell(f"[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false); & {literal(target / 'ccaw.ps1')} task show {literal(task_id)}")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["task"]["id"], task_id)


if __name__ == "__main__":
    unittest.main()
