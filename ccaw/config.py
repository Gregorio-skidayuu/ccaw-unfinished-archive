import hashlib
import json
from pathlib import Path


class WorkflowError(Exception):
    pass


DEFAULT_CONFIG = {
    "schema_version": 1,
    "revision": 1,
    "codex": {"executable": "codex", "sandbox": "workspace-write"},
    "roles": {
        "economy": {"backend": "codex", "model": None},
        "default": {"backend": "codex", "model": None},
        "expert": {"backend": "codex", "model": None},
    },
    "paid_api": {
        "enabled": False,
        "base_url": "https://api.deepseek.com",
        "key_env": "DEEPSEEK_API_KEY",
        "model": "",
        "reasoning_effort": "none",
        "input_usd_per_million": None,
        "output_usd_per_million": None,
        "max_output_tokens": 2048,
        "max_input_bytes": 32768,
        "monthly_limit_usd": "0",
    },
    "max_lessons": 5,
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile

    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def validate(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_CONFIG):
        raise WorkflowError("配置字段不完整或含未知字段；密钥只能存环境变量。")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise WorkflowError("不支持该配置版本，保留原文件并先执行版本迁移。")
    if any(not isinstance(value.get(key), dict) for key in ("codex", "roles", "paid_api")):
        raise WorkflowError("codex、roles 和 paid_api 必须是配置对象。")
    for key in ("codex", "roles", "paid_api"):
        if set(value[key]) != set(DEFAULT_CONFIG[key]):
            raise WorkflowError(f"{key} 配置字段不完整或含未知字段；请使用 init 生成的配置结构。")
    if not isinstance(value["codex"].get("executable"), str) or not value["codex"]["executable"]:
        raise WorkflowError("codex.executable 必须为可执行程序的名称或路径。")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise WorkflowError("配置 revision 必须是正整数。")
    if value["codex"]["sandbox"] not in ("read-only", "workspace-write"):
        raise WorkflowError("sandbox 仅支持 read-only 或 workspace-write。")
    if type(value["max_lessons"]) is not int or not 0 <= value["max_lessons"] <= 20:
        raise WorkflowError("max_lessons 必须为 0 到 20。")
    for role in ("economy", "default", "expert"):
        setting = value.get("roles", {}).get(role, {})
        if not isinstance(setting, dict):
            raise WorkflowError(f"{role} 必须是模型角色配置对象。")
        if set(setting) != set(DEFAULT_CONFIG["roles"][role]):
            raise WorkflowError(f"{role} 配置必须只包含 backend 和 model 字段。")
        if setting["backend"] not in ("codex", "api"):
            raise WorkflowError(f"{role} 的 backend 必须为 codex 或 api。")
        if setting.get("model") is not None and not isinstance(setting["model"], str):
            raise WorkflowError("模型 ID 必须是字符串或 null。")
    from decimal import Decimal, InvalidOperation

    api = value.get("paid_api", {})
    if not isinstance(api.get("enabled"), bool):
        raise WorkflowError("paid_api.enabled 必须为布尔值。")
    if not isinstance(api["model"], str):
        raise WorkflowError("paid_api.model 必须为字符串。")
    if api["reasoning_effort"] not in ("none", "low", "high", "max"):
        raise WorkflowError("API reasoning_effort 必须为 none、low、high 或 max。")
    try:
        for field in ("monthly_limit_usd", "input_usd_per_million", "output_usd_per_million"):
            if api[field] is None and field != "monthly_limit_usd":
                continue
            if type(api[field]) not in (str, int, float):
                raise InvalidOperation()
            number = Decimal(str(api[field]))
            if not number.is_finite() or number < 0:
                raise InvalidOperation()
    except (InvalidOperation, ValueError):
        raise WorkflowError("价格和预算必须是有限的非负数。") from None
    for field in ("max_output_tokens", "max_input_bytes"):
        if type(api[field]) is not int or api[field] < 1:
            raise WorkflowError(f"{field} 必须是正整数。")
    from urllib.parse import urlparse

    try:
        if not isinstance(api["base_url"], str):
            raise ValueError()
        parsed = urlparse(api["base_url"])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        _ = parsed.port
    except ValueError:
        raise WorkflowError("API base_url 必须是无凭据和查询参数的 HTTPS 地址。") from None
    if not isinstance(api.get("key_env"), str) or not api["key_env"]:
        raise WorkflowError("key_env 必须是保存密钥的环境变量名。")


def load(root):
    path = Path(root) / "config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"无法读取配置：{exc}") from exc
    validate(value)
    return value


def save_version(root, value):
    validate(value)
    name = fingerprint(value)
    atomic_write(Path(root) / "config-history" / f"{name}.json", canonical(value) + "\n")
    atomic_write(Path(root) / "config.json", json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return name


def route(kind, risk, complexity, batch=False):
    if kind == "script":
        return {"role": "script", "reason": "规则明确，直接交给确定性程序。"}
    if kind == "chat":
        return {"role": "chat", "reason": "基础讨论使用 ChatGPT 免费网页对话；交接任务包，不调用其他模型或付费 API。"}
    if risk == "high" or complexity == "high":
        return {"role": "expert", "reason": "高风险或高推理难度，需要关键审阅能力。"}
    if complexity == "low" or (batch and risk == "low"):
        return {"role": "economy", "reason": "边界明确或低风险批量工作，优先经济档。"}
    return {"role": "default", "reason": "常规工程实现，使用主力档。"}
