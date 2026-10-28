"""Entry point shared by the portable Windows app and its known worker modes."""
import os
from pathlib import Path
import runpy
import sys


WORKER_MODULES = {"ccaw.worker", "ccaw.capacity"}


def restore_standard_streams():
    """Windowed executables still need inherited pipes for CLI and worker logs."""
    modes = (("stdin", -10, "r"), ("stdout", -11, "w"), ("stderr", -12, "w"))
    for name, number, mode in modes:
        if getattr(sys, name) is not None:
            continue
        stream = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            kernel.GetStdHandle.restype = wintypes.HANDLE
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                              ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                              wintypes.BOOL, wintypes.DWORD]
            kernel.DuplicateHandle.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.GetStdHandle(number)
            if handle and handle != wintypes.HANDLE(-1).value:
                duplicate = wintypes.HANDLE()
                process = kernel.GetCurrentProcess()
                if kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
                    flags = os.O_RDONLY if mode == "r" else os.O_WRONLY
                    try:
                        descriptor = msvcrt.open_osfhandle(duplicate.value, flags | os.O_BINARY)
                        stream = open(descriptor, mode, encoding="utf-8", errors="replace", buffering=1)
                    except OSError:
                        kernel.CloseHandle(duplicate)
        if stream is None:
            stream = open(os.devnull, mode, encoding="utf-8")
        setattr(sys, name, stream)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if arguments[:1] == ["-m"]:
        restore_standard_streams()
        if len(arguments) < 2 or arguments[1] not in WORKER_MODULES:
            print("CCAW 仅允许启动已知的后台工作模块。", file=sys.stderr)
            return 2
        module = arguments[1]
        sys.argv = [module, *arguments[2:]]
        runpy.run_module(module, run_name="__main__")
        return 0
    if arguments[:1] == ["--cli"]:
        restore_standard_streams()
        from ccaw.cli import main as cli_main

        return cli_main(arguments[1:])
    from ccaw.gui import main as gui_main

    return gui_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
