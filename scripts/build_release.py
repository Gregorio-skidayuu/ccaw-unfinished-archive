"""Build a wheel and an allowlisted source archive without downloading packages."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = (
    ".gitignore", "AGENTS.md", "README.md", "LICENSE", "pyproject.toml", "Install.ps1", "ccaw.ps1", "Start-CCAW.cmd", "ccaw.cmd",
    "ccaw", "tests", "docs", "scripts", "examples", ".github",
)
EXCLUDED_DIRECTORIES = {"__pycache__", ".ccaw", ".venv", ".validation-tmp", ".git", "build", "dist"}


def source_files():
    files = []
    for name in SOURCE_PATHS:
        source = ROOT / name
        if not source.exists():
            raise RuntimeError(f"产品文件缺失：{name}")
        for path in source.rglob("*") if source.is_dir() else (source,):
            relative = path.relative_to(ROOT)
            if path.is_symlink() or not path.is_file():
                continue
            if any(part in EXCLUDED_DIRECTORIES or part.endswith(".egg-info") for part in relative.parts):
                continue
            if (path.name == "auth.json" or path.name.startswith(".env") or ".sqlite3" in path.name
                    or path.suffix in {".pyc", ".pyo", ".log"}):
                continue
            files.append(relative)
    return sorted(files, key=lambda path: path.as_posix())


def main():
    parser = argparse.ArgumentParser(description="离线生成 CCAW wheel、源码包和 SHA256 校验清单。")
    parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.path.insert(0, str(ROOT))
        from ccaw import __version__

        with (ROOT / "pyproject.toml").open("rb") as file:
            declared = tomllib.load(file)["project"]["version"]
        if declared != __version__:
            raise RuntimeError("pyproject.toml 与 ccaw.__version__ 不一致，未开始构建。")
        files = source_files()
        missing = [name for name in ("pip", "setuptools") if importlib.util.find_spec(name) is None]
        if missing:
            raise RuntimeError("缺少本地构建工具：" + ", ".join(missing) + "；请在开发环境准备，脚本不会联网安装。")
        destination = ROOT / "dist"
        destination.mkdir(exist_ok=True)
        subprocess.run([sys.executable, "-m", "pip", "--disable-pip-version-check", "wheel", str(ROOT), "--no-deps",
                        "--no-build-isolation", "--no-index", "--wheel-dir", str(destination)],
                       cwd=ROOT, check=True)
        wheels = list(destination.glob(f"ccaw-{__version__}-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("未找到唯一的本版本 wheel；请检查 dist 中的构建结果。")
        archive = destination / f"ccaw-v{__version__}.zip"
        temporary = archive.with_suffix(".zip.tmp")
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as bundle:
                for relative in files:
                    entry = ZipInfo(f"ccaw-v{__version__}/{relative.as_posix()}", date_time=(1980, 1, 1, 0, 0, 0))
                    entry.compress_type = ZIP_DEFLATED
                    entry.external_attr = 0o100644 << 16
                    bundle.writestr(entry, (ROOT / relative).read_bytes())
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
        artifacts = [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                     for path in (wheels[0], archive)]
        checksum = destination / f"SHA256SUMS-v{__version__}.txt"
        checksum.write_text("".join(f"{item['sha256']}  {Path(item['path']).name}\n" for item in artifacts), encoding="utf-8")
        print(json.dumps({"version": __version__, "artifacts": artifacts, "checksums": str(checksum)},
                         ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"打包失败：{exc}\n未联网安装构建工具；请检查本地 pip/setuptools 和源码。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
