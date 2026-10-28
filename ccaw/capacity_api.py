"""Read ordinary Codex allowance through app-server without starting a turn."""
import hashlib
import json
import os
from queue import Empty, Full, Queue
import shutil
import subprocess
from threading import Event, Thread
import time

from . import __version__
from .config import WorkflowError, canonical
from .providers import subscription_environment


REQUEST_TIMEOUT = 30
CHECK_INTERVAL = 300


def parse_capacity(account_response, limits_response, now=None):
    timestamp = time.time() if now is None else now
    account = account_response.get("account") if isinstance(account_response, dict) else None
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise WorkflowError("自动接续需要可确认的 ChatGPT 订阅账号。")
    if not isinstance(limits_response, dict):
        raise WorkflowError("无法确认额度响应格式；继续等待。")
    account_id = limits_response.get("accountId")
    recorded_id = account_response.get("chatgptAccountId") or account.get("chatgptAccountId") or account.get("id")
    if account_id is not None and (not isinstance(account_id, str) or not account_id):
        raise WorkflowError("无法确认额度所属账号；继续等待。")
    if recorded_id is not None and (not isinstance(recorded_id, str) or not recorded_id):
        raise WorkflowError("无法确认当前账号身份；继续等待。")
    if account_id and recorded_id and account_id != recorded_id:
        raise WorkflowError("额度响应与当前账号不一致；停止自动接续。")
    account_id = account_id or recorded_id
    if not account_id:
        email = account.get("email")
        if not isinstance(email, str) or not email.strip():
            raise WorkflowError("当前接口未提供可确认的账号身份；继续等待。")
        account_id = "email-sha256:" + hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()

    report = {"account_id": account_id, "allowed": False, "cycle": None,
              "next_check_at": timestamp + CHECK_INTERVAL, "reason": "额度信息不完整，继续等待。"}
    buckets = limits_response.get("rateLimitsByLimitId")
    if buckets is not None:
        bucket = buckets.get("codex") if isinstance(buckets, dict) else None
    else:
        bucket = limits_response.get("rateLimits")
        if isinstance(bucket, dict) and bucket.get("limitId") not in (None, "codex"):
            bucket = None
    if not isinstance(bucket, dict):
        return report
    if bucket.get("limitId") not in (None, "codex"):
        return report
    windows, resets, exhausted = [], [], []
    for name in ("primary", "secondary"):
        window = bucket.get(name)
        if window is None:
            windows.append((name, None))
            continue
        if not isinstance(window, dict):
            return report
        used = window.get("usedPercent")
        reset = window.get("resetsAt")
        if type(used) is not int or not 0 <= used <= 100:
            return report
        if reset is not None and (type(reset) is not int or reset <= 0):
            return report
        windows.append((name, reset))
        if reset is not None:
            resets.append(reset)
        if used >= 100:
            exhausted.append(reset)
    if not resets:
        return report
    report["cycle"] = hashlib.sha256(canonical(windows).encode("utf-8")).hexdigest()
    known_blocked_resets = [reset + 10 for reset in exhausted if reset is not None]
    if known_blocked_resets:
        report["next_check_at"] = max(report["next_check_at"], *known_blocked_resets)
    if limits_response.get("ordinaryUsageAllowed") is not True:
        report["reason"] = "接口尚未明确允许订阅额度使用，继续等待。"
    elif exhausted or bucket.get("rateLimitReachedType") is not None or (bucket.get("spendControlReached") is not None and bucket["spendControlReached"] is not False):
        report["reason"] = "当前额度窗口或账户限制仍未恢复，继续等待。"
    else:
        report.update(allowed=True, reason="接口已明确允许订阅额度使用。")
    return report


def _app_server_command(executable):
    return [executable, "app-server", "-c", 'model_provider="openai"', "--listen", "stdio://"]


def _read_snapshot(executable, env, deadline):
    child = None
    reader = None
    messages, failed = Queue(maxsize=32), Event()
    try:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        child = subprocess.Popen(_app_server_command(executable), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env, **options)

        def read_lines():
            try:
                for line in iter(lambda: child.stdout.readline(1024 * 1024 + 1), ""):
                    if len(line) > 1024 * 1024:
                        failed.set()
                        return
                    messages.put_nowait(line)
                messages.put_nowait(None)
            except (OSError, UnicodeError, ValueError, Full):
                failed.set()

        reader = Thread(target=read_lines, daemon=True)
        reader.start()

        def send(value):
            child.stdin.write(json.dumps(value) + "\n")
            child.stdin.flush()

        def request(identity, method, params):
            send({"id": identity, "method": method, "params": params})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkflowError("额度读取超时；未启动模型，继续等待。")
                if failed.is_set():
                    raise WorkflowError("额度读取协议异常；未启动模型，继续等待。")
                try:
                    line = messages.get(timeout=remaining)
                except Empty:
                    raise WorkflowError("额度读取超时；未启动模型，继续等待。") from None
                if line is None:
                    raise WorkflowError("额度读取进程已退出；未启动模型，继续等待。")
                try:
                    message = json.loads(line)
                except ValueError:
                    raise WorkflowError("额度读取协议异常；未启动模型，继续等待。") from None
                if not isinstance(message, dict):
                    raise WorkflowError("额度读取协议异常；未启动模型，继续等待。")
                if "id" in message and "method" in message:
                    send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported server request"}})
                    raise WorkflowError("额度读取需要交互处理；未启动模型，继续等待。")
                if type(message.get("id")) is not int or message["id"] != identity:
                    continue
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise WorkflowError("当前 Codex 接口无法读取额度；未启动模型，继续等待。")
                return message["result"]

        request(0, "initialize", {"clientInfo": {"name": "ccaw", "title": "CCAW", "version": __version__}})
        send({"method": "initialized", "params": {}})
        account = request(1, "account/read", {"refreshToken": False})
        limits = request(2, "account/rateLimits/read", {})
        return account, limits
    except (OSError, UnicodeError, ValueError):
        raise WorkflowError("无法读取 Codex 额度；未启动模型，继续等待。") from None
    finally:
        if child is not None:
            if child.poll() is None:
                try:
                    child.terminate()
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
                except OSError:
                    child.wait(timeout=2)
            for stream in (child.stdin, child.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if reader is not None:
                reader.join(timeout=1)


def read_capacity(config):
    deadline = time.monotonic() + REQUEST_TIMEOUT
    executable = shutil.which(config["codex"]["executable"])
    if not executable:
        raise WorkflowError("未找到 Codex CLI；自动接续继续等待。")
    env = subscription_environment(executable)
    if time.monotonic() >= deadline:
        raise WorkflowError("额度读取超时；未启动模型，继续等待。")
    account, limits = _read_snapshot(executable, env, deadline)
    return parse_capacity(account, limits)
