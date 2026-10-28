"""Source checkout launcher: no installation, downloads, or system changes."""
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def show_error(message):
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "CCAW 启动未完成", 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if sys.version_info < (3, 11):
        show_error("需要 Python 3.11 或更新版本。也可使用无需安装 Python 的 CCAW.exe 便携版。")
        return 2
    sys.path.insert(0, str(ROOT))
    if arguments[:1] == ["--cli"]:
        from ccaw.cli import main as cli_main

        return cli_main(["--home", str(ROOT / ".ccaw"), *arguments[1:]])
    home = ROOT / ".ccaw"
    try:
        import tkinter

        if Path(sys.executable).stem.lower() == "pythonw":
            from ccaw.gui import main as gui_main

            return gui_main(["--home", str(home), "--project", str(ROOT), *arguments])
        windowless = Path(sys.executable).with_name("pythonw.exe")
        executable = str(windowless) if windowless.is_file() else sys.executable
        home.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUTF8"] = "1"
        options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with (home / "desktop-start.log").open("ab") as log:
            subprocess.Popen([executable, "-m", "ccaw.gui", "--home", str(home), "--project", str(ROOT), *arguments],
                             cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
        return 0
    except (ImportError, OSError) as exc:
        show_error(f"无法启动 CCAW 界面（{type(exc).__name__}）。请使用含 Tcl/Tk 的 Python 3.11+，或使用 CCAW.exe。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
