"""Build the portable Windows GUI with an already prepared PyInstaller toolchain."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="用本地 PyInstaller 构建无需 Python 的 CCAW.exe。")
    parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if os.name != "nt":
            raise RuntimeError("Windows EXE 必须在 Windows 上构建。")
        tools = ROOT / ".ccaw" / "build-tools" / "Scripts" / "python.exe"
        executable = str(tools) if tools.is_file() else sys.executable
        checked = subprocess.run([executable, "-c", "import PyInstaller;print(PyInstaller.__version__)"],
                                 capture_output=True, text=True, timeout=15)
        if checked.returncode:
            raise RuntimeError("请先在 .ccaw/build-tools 开发虚拟环境安装 PyInstaller>=6.15；构建脚本不会联网安装。")
        if not (ROOT / "ccaw" / "gui.py").is_file():
            raise RuntimeError("缺少 ccaw/gui.py，不能构建桌面产品。")
        work = ROOT / ".ccaw" / "build" / "windows"
        work.mkdir(parents=True, exist_ok=True)
        destination = ROOT / "dist" / "windows"
        subprocess.run([executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
                        "--name", "CCAW", "--paths", str(ROOT), "--collect-submodules", "ccaw",
                        "--distpath", str(destination), "--workpath", str(work), "--specpath", str(work),
                        str(ROOT / "scripts" / "desktop_entry.py")], cwd=ROOT, check=True)
        artifact = destination / "CCAW.exe"
        print(json.dumps({"path": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                          "bytes": artifact.stat().st_size, "pyinstaller": checked.stdout.strip()}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Windows 打包未完成：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
