"""Explicit Codex CLI integration and opt-in, text-only compatible API calls."""
from decimal import Decimal
from http.client import HTTPException
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.error
import urllib.request

from .config import WorkflowError


class APIExecutionUncertain(WorkflowError):
    """The request may have executed, but its result cannot be confirmed."""


def codex_command(config, role, output, session=None):
    executable = config["codex"]["executable"]
    resolved = shutil.which(executable)
    if not resolved:
        raise WorkflowError("未找到 Codex CLI。请安装并登录，或配置 codex.executable。")
    command = [resolved, "exec", "--json", "--output-last-message", str(output)]
    model = config["roles"][role].get("model")
    if model:
        command += ["--model", model]
    # A top-level override applies equally to new and resumed exec sessions.
    command += ["-c", "sandbox_mode=" + json.dumps(config["codex"]["sandbox"])]
    command += ["-c", 'model_provider="openai"']
    if session:
        command += ["resume", session, "-"]
    else:
        command += ["-"]
    return command


def subscription_environment(executable):
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
        env.pop(key, None)
    try:
        status = subprocess.run([executable, "login", "status"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkflowError("无法核对 Codex 登录方式；未启动模型调用。") from None
    # Never save auth output: some CLI versions include fragments of an API key.
    if status.returncode != 0 or "chatgpt" not in (status.stdout + status.stderr).lower():
        raise WorkflowError("Codex 适配器需要已登录的 ChatGPT 订阅；未确认订阅登录，未发起调用。")
    return env


def classify_error(text):
    value = text.lower()
    if any(x in value for x in ("usage_limit_reached", "usage limit", "quota", "rate_limit", "rate limit", "429", "用量限制", "额度")):
        return "CAPACITY"
    if any(x in value for x in ("unauthorized", "authentication", "401", "login", "sign in")):
        return "AUTH"
    if any(x in value for x in ("timeout", "timed out", "connection", "network", "503")):
        return "TRANSIENT"
    return "EXECUTION"


def api_reservation(config, prompt):
    api = config["paid_api"]
    if not api["enabled"]:
        raise WorkflowError("付费 API 默认关闭；明确配置并授权后才能调用。")
    if not api["model"] or api["input_usd_per_million"] is None or api["output_usd_per_million"] is None:
        raise WorkflowError("请先核实并填写 API 模型、输入和输出价格。")
    if len(prompt.encode("utf-8")) > api["max_input_bytes"]:
        raise WorkflowError("任务包超过 API 输入字节上限；请缩小上下文。")
    # Conservative application estimate, not a provider-side billing guarantee.
    input_estimate = len(prompt.encode("utf-8")) + 2048
    return (Decimal(input_estimate) * Decimal(str(api["input_usd_per_million"])) +
            Decimal(api["max_output_tokens"]) * Decimal(str(api["output_usd_per_million"]))) / Decimal(1_000_000)


def call_api(config, prompt):
    api = config["paid_api"]
    key = os.environ.get(api["key_env"])
    if not key:
        raise WorkflowError(f"未设置密钥环境变量 {api['key_env']}；不在文件中保存密钥。")
    body = {
        "model": api["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": api["max_output_tokens"],
        "stream": False,
        "reasoning_effort": api["reasoning_effort"],
    }
    request = urllib.request.Request(api["base_url"].rstrip("/") + "/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=180) as response:
            data = json.loads(response.read(8 * 1024 * 1024))
    except urllib.error.HTTPError as exc:
        # Do not echo bodies or request headers, which may include secrets.
        raise WorkflowError(f"API HTTP {exc.code}；请求未自动重试，按需核对账单和状态。") from None
    except (OSError, ValueError, HTTPException) as exc:
        raise APIExecutionUncertain(f"API 响应状态未知（{type(exc).__name__}）；未自动重试，费用预留保留。") from None
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
        if not isinstance(content, str):
            raise ValueError()
        usage = data.get("usage", {})
        if not isinstance(usage, dict):
            raise ValueError()
        for field in ("prompt_tokens", "completion_tokens"):
            if field in usage and (type(usage[field]) is not int or usage[field] < 0):
                raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise APIExecutionUncertain("API 响应格式不兼容；费用预留保留，未自动重试。") from None
    actual = None
    if isinstance(usage.get("prompt_tokens"), int) and isinstance(usage.get("completion_tokens"), int):
        actual = (Decimal(usage["prompt_tokens"]) * Decimal(str(api["input_usd_per_million"])) +
                  Decimal(usage["completion_tokens"]) * Decimal(str(api["output_usd_per_million"]))) / Decimal(1_000_000)
    return content, actual, usage, choice.get("finish_reason") == "stop"
