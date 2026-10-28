"""Process birth identity, detached starts, and targeted cancellation."""
import os
from pathlib import Path
import signal
import subprocess
import sys

from .config import WorkflowError


def identity(pid):
    if not pid:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return f"win:{times[0].dwHighDateTime}:{times[0].dwLowDateTime}"
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            # A container can expose a host /proc with different visible PIDs.
            # pidfd fdinfo resolves only the requested process, without a tree scan.
            if pid == os.getpid():
                visible_pid = Path("/proc/self/stat").read_text().split(" ", 1)[0]
            elif hasattr(os, "pidfd_open"):
                descriptor = os.pidfd_open(pid)
                try:
                    info = Path(f"/proc/self/fdinfo/{descriptor}").read_text()
                    visible_pid = next(x.split(":", 1)[1].strip() for x in info.splitlines() if x.startswith("Pid:"))
                finally:
                    os.close(descriptor)
            else:
                own_visible = int(Path("/proc/self/stat").read_text().split(" ", 1)[0])
                if own_visible != os.getpid():
                    return None
                visible_pid = str(pid)
            stat = Path(f"/proc/{visible_pid}/stat").read_text()
            fields = stat[stat.rfind(")") + 2:].split()
            if fields[0] == "Z":
                return None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return f"linux:{boot}:{visible_pid}:{fields[19]}"
        except (OSError, IndexError, StopIteration):
            return None
    raise WorkflowError("当前版本支持 Windows 与 Linux 的进程身份核对。")


def same_process(pid, token):
    return bool(token) and identity(pid) == token


def detached_options():
    if os.name == "nt":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate(pid, token):
    if not same_process(pid, token):
        raise WorkflowError("进程身份无法确认，未发送终止信号。")
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        if result.returncode:
            raise WorkflowError("无法取消该进程树，请核对作业状态。")
    else:
        os.killpg(pid, signal.SIGTERM)
