"""Package CCAW source or an existing Windows desktop build."""
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
    "ccaw", "desktop", "tests", "docs", "scripts", "examples", ".github",
)
EXCLUDED_DIRECTORIES = {"__pycache__", ".ccaw", ".venv", ".validation-tmp", ".git", "build", "dist"}
DESKTOP_BUILD_DIRECTORIES = {"bin", "obj", "packages", ".vs", ".nuget"}
DESKTOP_RUNTIME_FILES = (
    "CCAW.Desktop.exe",
    "Microsoft.Web.WebView2.Core.dll",
    "Microsoft.Web.WebView2.Wpf.dll",
    "WebView2Loader.dll",
    "WebView2-LICENSE.txt",
    "assets/index.html",
    "assets/app.css",
    "assets/app.js",
)
WINDOWS_README = """# CCAW Windows x64

需要 Windows x64、Python 3.11 或更高版本、.NET Framework 4.8 和 Microsoft Edge WebView2 Evergreen Runtime。

1. 将 ZIP 完整解压到可写目录。
2. 双击 `Start-CCAW.cmd` 打开桌面工作台。保持解压后的目录结构。
3. 新建脚本任务，填写可观察的验收条件并运行。重新打开工作台后可核对同一作业和日志。

默认账本为解压目录下的 `.ccaw`。既有账本可通过 `Start-CCAW.cmd --home "C:\\path\\to\\ledger"` 打开。
在解压目录运行 `ccaw.cmd init` 初始化账本，运行 `ccaw.cmd task list` 查看任务。
"""


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
            if relative.parts[0] == "desktop" and any(
                part.casefold() in DESKTOP_BUILD_DIRECTORIES for part in relative.parts[1:-1]
            ):
                continue
            if (path.name == "auth.json" or path.name.startswith(".env") or ".sqlite3" in path.name
                    or path.suffix in {".pyc", ".pyo", ".log"}):
                continue
            files.append(relative)
    return sorted(files, key=lambda path: path.as_posix())


def _add_to_archive(bundle, prefix, relative, content):
    entry = ZipInfo(f"{prefix}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = ZIP_DEFLATED
    entry.external_attr = 0o100644 << 16
    bundle.writestr(entry, content)


def _windows_package(version, destination):
    files = [Path(name) for name in ("Start-CCAW.cmd", "ccaw.cmd", "LICENSE", "scripts/bootstrap.py")]
    files.extend(path.relative_to(ROOT) for path in sorted((ROOT / "ccaw").glob("*.py")))
    files.extend(Path("dist/desktop") / name for name in DESKTOP_RUNTIME_FILES)
    for relative in files:
        source = ROOT / relative
        if not source.is_file() or source.resolve() != source:
            raise RuntimeError(f"Windows 用户包所需文件缺失或为重定向路径：{relative.as_posix()}")

    destination.mkdir(parents=True, exist_ok=True)
    prefix = f"ccaw-v{version}-windows-x64"
    archive = destination / f"{prefix}.zip"
    checksum = destination / f"{archive.name}.sha256"
    if archive.exists() or checksum.exists():
        raise RuntimeError("目标版本文件已存在，请使用新的 --output-dir。")

    wrote_archive = wrote_checksum = False
    try:
        with archive.open("xb") as output:
            wrote_archive = True
            with ZipFile(output, "w") as bundle:
                _add_to_archive(bundle, prefix, "README.md", WINDOWS_README.encode("utf-8"))
                for relative in files:
                    _add_to_archive(bundle, prefix, relative.as_posix(), (ROOT / relative).read_bytes())
        with archive.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        with checksum.open("x", encoding="utf-8") as file:
            wrote_checksum = True
            file.write(f"{digest}  {archive.name}\n")
    except BaseException:
        if wrote_checksum:
            checksum.unlink(missing_ok=True)
        if wrote_archive:
            archive.unlink(missing_ok=True)
        raise
    return {"version": version, "artifacts": [{"path": str(archive), "sha256": digest}], "checksums": str(checksum)}


def main():
    parser = argparse.ArgumentParser(description="离线生成 CCAW 源码包或已构建的 Windows 用户包。")
    parser.add_argument("--windows-desktop", action="store_true", help="将 dist/desktop 与 Python 运行文件打成用户 ZIP")
    parser.add_argument("--output-dir", type=Path, help="Windows 用户包的输出目录")
    args = parser.parse_args()
    if args.output_dir is not None and not args.windows_desktop:
        parser.error("--output-dir 仅用于 --windows-desktop")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.path.insert(0, str(ROOT))
        from ccaw import __version__

        with (ROOT / "pyproject.toml").open("rb") as file:
            declared = tomllib.load(file)["project"]["version"]
        if declared != __version__:
            raise RuntimeError("pyproject.toml 与 ccaw.__version__ 不一致，未开始构建。")
        if args.windows_desktop:
            output = (args.output_dir or ROOT / "dist").resolve()
            print(json.dumps(_windows_package(__version__, output), ensure_ascii=False, indent=2))
            return 0
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
                    _add_to_archive(bundle, f"ccaw-v{__version__}", relative.as_posix(), (ROOT / relative).read_bytes())
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
        print(f"打包失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
