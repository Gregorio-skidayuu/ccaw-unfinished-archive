import argparse
import copy
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from . import __version__
from .config import DEFAULT_CONFIG, WorkflowError, atomic_write, fingerprint, load, route, save_version, validate
from .context import render_packet, task_packet
from .runner import ACTIVE, cancel, continuation, enqueue, finish, reconcile, start, wait_for
from .store import Store, now


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parser():
    root = argparse.ArgumentParser(
        prog="ccaw", description="可恢复、可纠错、按成本分流的 Codex 工作流",
        epilog="首次使用：ccaw init → ccaw doctor → ccaw task add。查看具体用法：ccaw <命令> --help。--home 放在命令之前。")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--home", default=os.environ.get("CCAW_HOME", str(Path.cwd() / ".ccaw")), help="状态目录；默认当前目录/.ccaw")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化任务账本和安全默认配置")
    commands.add_parser("doctor", help="检查环境，不调用任何模型")
    gui = commands.add_parser("gui", help="打开本地图形工作台与模型配置")
    gui.add_argument("--project", default=str(Path.cwd()), help="默认项目目录")
    gui.add_argument("--configure", action="store_true", help="直接打开模型配置页")
    route_p = commands.add_parser("route", help="离线路由建议")
    add_features(route_p)
    config = commands.add_parser("config", help="查看、发布和回退配置").add_subparsers(dest="action", required=True)
    config.add_parser("show", help="查看当前完整配置")
    apply = config.add_parser("apply", help="校验并发布完整 JSON 配置")
    apply.add_argument("file", help="完整配置 JSON；验证后生成新版本")
    rollback = config.add_parser("rollback", help="将历史配置发布为新版本")
    rollback.add_argument("hash", help="config-history 中的完整哈希")
    config.add_parser("history", help="列出配置哈希与当前版本")
    task = commands.add_parser("task", help="新建、查看、交接和验收任务").add_subparsers(dest="action", required=True)
    add = task.add_parser("add", help="记录目标、验收条件和项目目录")
    add.add_argument("title")
    add.add_argument("--goal")
    add.add_argument("--accept", required=True, help="可观察的验收条件")
    add.add_argument("--project", default=str(Path.cwd()))
    add.add_argument("--tag", action="append", default=[])
    add_features(add)
    task.add_parser("list", help="列出任务及当前状态")
    show = task.add_parser("show", help="查看任务、作业和适用经验")
    show.add_argument("id")
    export = task.add_parser("export", help="导出 Markdown 任务交接包")
    export.add_argument("id")
    export.add_argument("--out", help="新建 Markdown 文件；不指定则直接打印")
    done = task.add_parser("done", help="凭验收证据确认任务完成")
    done.add_argument("id")
    done.add_argument("--evidence", required=True)
    jobs = commands.add_parser("job", help="运行、等待、诊断和核对作业").add_subparsers(dest="action", required=True)
    script = jobs.add_parser("run", help="独立执行明确的程序命令",
                             epilog="示例：ccaw job run <TASK_ID> --key step-1 -- python script.py。PowerShell 调用 .\\ccaw.ps1 时请将分隔符写为 '--'，避免被 PowerShell 吞掉。")
    script.add_argument("task_id")
    add_execution(script)
    script.add_argument("--then", help="成功后由 Codex 自动接续的指令")
    script.add_argument("--then-role", choices=["economy", "default", "expert"], default="default")
    agent = jobs.add_parser("agent", help="由指定角色执行任务包")
    agent.add_argument("task_id")
    agent.add_argument("--prompt", default="依据任务目标和验收条件完成工作。")
    agent.add_argument("--role", choices=["economy", "default", "expert"])
    agent.add_argument("--resume", help="接续该作业保存的具体 Codex session")
    agent.add_argument("--allow-api", action="store_true", help="允许将当前任务包发送到配置的付费 API")
    agent.add_argument("--auto-resume", action="store_true", help="订阅额度自然恢复后后台接续本作业；成功、撤销或需要人工处理时停止")
    add_execution(agent)
    jobs.add_parser("list", help="列出作业及执行状态")
    for action, help_text in (("show", "核对状态，查看日志路径和最近事件"),
                              ("cancel", "请求取消作业"), ("launch", "派发尚未启动的 QUEUED 作业"),
                              ("continue", "补发成功作业尚未派发的接续"),
                              ("watch", "重启已获自动接续授权的后台等待程序"),
                              ("unwatch", "停止本作业恢复链的后续自动接续")):
        item = jobs.add_parser(action, help=help_text)
        item.add_argument("id")
    wait = jobs.add_parser("wait", help="普通程序等待，不调用模型；关闭此窗口不取消计算")
    wait.add_argument("id")
    wait.add_argument("--timeout", type=float, default=None)
    resolve = jobs.add_parser("resolve", help="人工核对 UNKNOWN 后确认结束，不重启作业")
    resolve.add_argument("id")
    resolve.add_argument("--status", choices=["SUCCEEDED", "FAILED", "CANCELLED"], required=True)
    resolve.add_argument("--evidence", required=True)
    incident = commands.add_parser("incident", help="记录和查看故障证据").add_subparsers(dest="action", required=True)
    report = incident.add_parser("add", help="记录故障特征、描述和证据")
    report.add_argument("task_id")
    report.add_argument("--signature", required=True, help="稳定的错误或根因标识")
    report.add_argument("--description", required=True)
    report.add_argument("--evidence", required=True, help="日志/复现路径或可核对证据")
    incident.add_parser("list", help="列出已记录故障")
    lessons = commands.add_parser("lesson", help="验证、复用、撤回和修订纠错经验").add_subparsers(dest="action", required=True)
    candidate = lessons.add_parser("propose", help="从故障提出有范围的候选措施")
    candidate.add_argument("incident_id")
    candidate.add_argument("--cause", required=True)
    candidate.add_argument("--correction", required=True)
    candidate.add_argument("--tag", action="append", required=True)
    lessons.add_parser("list", help="列出经验及生效状态")
    for action, help_text in (("enable", "启用已验证经验"), ("revoke", "撤回经验，停止后续注入"),
                              ("history", "查看经验版本历史")):
        item = lessons.add_parser(action, help=help_text)
        item.add_argument("id")
    revise = lessons.add_parser("revise", help="修订为新候选，之后需重新验证")
    revise.add_argument("id")
    revise.add_argument("--cause", required=True)
    revise.add_argument("--correction", required=True)
    verify = lessons.add_parser("verify", help="关联同项目成功回归作业作为证据")
    verify.add_argument("id")
    verify.add_argument("--job", required=True, help="候选建立后运行的同项目成功回归作业")
    backup = commands.add_parser("backup", help="新建 SQLite 一致性备份")
    backup.add_argument("destination")
    return root


def add_features(p):
    p.add_argument("--kind", choices=["code", "script", "chat"], default="code")
    p.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    p.add_argument("--complexity", choices=["low", "medium", "high"], default="medium")
    p.add_argument("--batch", action="store_true")


def add_execution(p):
    p.add_argument("--key", default="main", help="同任务同阶段使用同一键，防止重复启动；新阶段使用新键")
    p.add_argument("--wait", action="store_true")


def doctor(home):
    home = Path(home).resolve()
    issues, next_steps = [], []
    config = None
    if not (home / "config.json").is_file():
        issues.append("尚未初始化配置；请使用相同 --home 运行 init。")
    else:
        try:
            config = load(home)
        except WorkflowError as exc:
            issues.append(str(exc))
            next_steps.append("修正 config.json 中的配置错误，保留现有状态目录。")
    database = {"path": str(home / "state.sqlite3"), "schema_version": None, "integrity": None}
    if not (home / "state.sqlite3").is_file():
        issues.append("尚未初始化任务账本；请使用相同 --home 运行 init。")
    else:
        try:
            with closing(sqlite3.connect((home / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
                database["schema_version"] = db.execute("PRAGMA user_version").fetchone()[0]
                database["integrity"] = db.execute("PRAGMA quick_check").fetchone()[0]
            if database["schema_version"] != 1:
                issues.append("数据库版本不受支持；先备份并迁移，勿重新初始化或删除。")
            if database["integrity"] != "ok":
                issues.append("数据库完整性检查未通过；请保留原文件并检查备份。")
        except sqlite3.Error as exc:
            issues.append(f"无法检查任务账本：{exc}")
    if sys.version_info < (3, 11):
        issues.append("需要 Python 3.11 或更新版本。")
    if sys.platform != "win32" and not sys.platform.startswith("linux"):
        issues.append("当前运行器仅支持 Windows 和 Linux。")
    executable = shutil.which((config or DEFAULT_CONFIG)["codex"]["executable"])
    codex_version = None
    if executable:
        try:
            check = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
            if check.returncode == 0:
                codex_version = check.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not executable or not codex_version:
        next_steps.append("使用 Codex 执行前，请检查 codex.executable 并在本机完成 Codex 登录；离线脚本无需 Codex。")
    return {"version": __version__, "python": sys.version.split()[0], "platform": sys.platform,
            "home": str(home), "codex_executable": executable, "codex_version": codex_version,
            "models": config["roles"] if config else None,
            "paid_api_enabled": config["paid_api"]["enabled"] if config else None,
            "ready": not issues, "database": database, "issues": issues, "next_steps": next_steps,
            "note": "模型为 null 时沿用 Codex 自身默认值；本检查未调用模型、未验证模型权益或剩余额度。"}


def job_details(store, job_id):
    job = reconcile(store, job_id)
    folder = store.root / "runs" / job_id
    events = store.rows("SELECT id,kind,payload,created_at FROM events WHERE job_id=? ORDER BY id DESC LIMIT 10", (job_id,))
    for event in events:
        event["payload"] = json.loads(event["payload"])
    next_action = {
        "QUEUED": "使用 job launch 派发，或 job cancel 取消。",
        "STARTING": "正在启动；使用 job wait 等待，不要重复启动。",
        "RUNNING": "使用 job wait 等待；关闭等待窗口不会取消作业。",
        "UNKNOWN": "先核对真实进程和日志，确认结束后使用 job resolve；不要重启。",
        "SUCCEEDED": "按任务验收条件检查成果；符合条件后使用 task done 并提供证据。",
        "FAILED": "检查 stderr.log、worker.log 和最近事件；确认原因后再决定下一阶段。",
        "CANCELLED": "作业已取消；核对已保留的成果。",
        "WAITING_CAPACITY": "额度恢复后用新的 --key 和 --resume <旧JOB_ID> 显式接续；不会自动重试。",
    }
    job["diagnostics"] = {
        "run_directory": str(folder),
        "files": {name: str(folder / name) for name in ("worker.log", "stdout.log", "stderr.log", "result.md", "execution.json", "prompt.md", "capacity.log")
                  if (folder / name).is_file()},
        "recent_events": list(reversed(events)),
        "next_action": next_action.get(job["status"]),
    }
    if job["status"] == "UNKNOWN" and job["kind"] == "api":
        job["diagnostics"]["next_action"] = "先核对供应商请求记录和账单，再用 job resolve 确认状态；费用预留不会自动释放，不要重复请求。"
    if json.loads(job["snapshot"]).get("auto_resume"):
        from .capacity import status
        job["diagnostics"]["auto_resume"] = status(store, job_id)
        if job["status"] == "WAITING_CAPACITY":
            job["diagnostics"]["next_action"] = "查看 auto_resume 中的后台等待状态；job watch 可重启等待程序，job unwatch 可停止后续自动接续。"
    return job


def execute(args):
    home = Path(args.home).resolve()
    if args.command == "gui":
        from .gui import main as gui_main
        options = ["--home", str(home), "--project", args.project]
        if args.configure:
            options.append("--configure")
        return gui_main(options)
    if args.command == "init":
        home.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            home.chmod(0o700)
        atomic_write(home / ".gitignore", "*\n!.gitignore\n")
        if not (home / "config.json").exists():
            save_version(home, copy.deepcopy(DEFAULT_CONFIG))
        config = load(home)
        store = Store.initialize(home)
        store.close()
        output({"home": str(home), "status": "initialized", "paid_api": config["paid_api"]["enabled"]})
        return 0
    if args.command == "route":
        output(route(args.kind, args.risk, args.complexity, args.batch))
        return 0
    if args.command == "doctor":
        report = doctor(home)
        output(report)
        return 0 if report["ready"] else 2
    if args.command == "config":
        config = load(home)
        if args.action == "show":
            output(config)
        elif args.action == "history":
            output([{"hash": p.stem, "current": p.stem == fingerprint(config)} for p in sorted((home / "config-history").glob("*.json"))])
        else:
            if args.action == "apply":
                path = Path(args.file)
            else:
                if len(args.hash) != 64 or any(c not in "0123456789abcdef" for c in args.hash):
                    raise WorkflowError("请提供完整的配置版本哈希。")
                path = home / "config-history" / (args.hash + ".json")
            candidate = json.loads(path.read_text(encoding="utf-8-sig"))
            validate(candidate)
            candidate["revision"] = config["revision"] + 1
            output({"hash": save_version(home, candidate), "revision": candidate["revision"]})
        return 0
    store = Store(home)
    try:
        if args.command == "task":
            if args.action == "add":
                decision = route(args.kind, args.risk, args.complexity, args.batch)
                output(store.create_task(args.title, args.goal or args.title, args.accept, args.project,
                                         args.tag, args.kind, args.risk, args.complexity, decision["role"]))
            elif args.action == "list":
                output(store.rows("SELECT id,title,status,role,project FROM tasks ORDER BY created_at DESC"))
            elif args.action == "show":
                output(task_packet(store, args.id))
            elif args.action == "export":
                text = render_packet(task_packet(store, args.id))
                if args.out:
                    if Path(args.out).exists():
                        raise WorkflowError("导出目标已存在；请使用新文件名，避免覆盖。")
                    atomic_write(args.out, text)
                    output({"path": str(Path(args.out).resolve())})
                else:
                    print(text)
            elif args.action == "done":
                with store.transaction():
                    store.get("tasks", args.id)
                    if store.db.execute("SELECT id FROM jobs WHERE task_id=? AND status IN ('QUEUED','STARTING','RUNNING','UNKNOWN')", (args.id,)).fetchone():
                        raise WorkflowError("还有未完成或待核对作业，不能验收完成。")
                    store.db.execute("UPDATE tasks SET status='DONE',updated_at=? WHERE id=?", (now(), args.id))
                    store.event("task.accepted", {"evidence": args.evidence}, task=args.id)
                output(store.get("tasks", args.id))
        elif args.command == "job":
            if args.action == "list":
                output(store.rows("SELECT id,task_id,kind,status,session_id,exit_code,error_kind FROM jobs ORDER BY created_at DESC"))
            elif args.action in {"run", "agent"}:
                if args.action == "run":
                    argv = args.argv
                    if argv and argv[0] == "--":
                        argv = argv[1:]
                    job, created = enqueue(store, args.task_id, "script", command=argv, key=args.key,
                                           then_prompt=args.then, then_role=args.then_role)
                else:
                    job, created = enqueue(store, args.task_id, "codex", prompt=args.prompt, role=args.role,
                                           key=args.key, resume_job=args.resume, allow_api=args.allow_api,
                                           auto_resume=args.auto_resume)
                if job["status"] == "QUEUED":
                    job = start(store, job["id"])
                if args.wait:
                    job = wait_for(store, job["id"])
                output({"created": created, "job": job})
                if args.wait and job["status"] != "SUCCEEDED":
                    return 2
            elif args.action == "show":
                output(job_details(store, args.id))
            elif args.action in {"watch", "unwatch"}:
                from .capacity import disable, ensure_watcher, status
                if args.action == "watch":
                    launch = ensure_watcher(store, args.id)
                    report = status(store, args.id)
                    report["launch"] = launch
                    report["watcher_starting"] = bool(launch.get("launched") and not report["watcher_running"])
                    output(report)
                    return 0 if launch.get("launched") or launch.get("watcher_running") else 2
                else:
                    disable(store, args.id)
                output(status(store, args.id))
            elif args.action == "launch":
                job = store.get("jobs", args.id)
                if job["status"] != "QUEUED":
                    raise WorkflowError("仅可派发尚未启动的 QUEUED 作业；不能重启未知作业。")
                output(start(store, args.id))
            elif args.action == "continue":
                output({"continuation_job": continuation(store, args.id)})
            elif args.action == "cancel":
                output(cancel(store, args.id))
            elif args.action == "wait":
                job = wait_for(store, args.id, args.timeout)
                output(job)
                return 0 if job["status"] == "SUCCEEDED" else 2
            elif args.action == "resolve":
                job = reconcile(store, args.id)
                if job["status"] != "UNKNOWN":
                    raise WorkflowError("仅能人工核对 UNKNOWN 作业。")
                from .processes import same_process
                if same_process(job["pid"], job["process_token"]) or same_process(job["child_pid"], job["child_token"]):
                    raise WorkflowError("已记录进程仍存活，不能宣告作业结束。")
                finish(store, args.id, args.status, 0 if args.status == "SUCCEEDED" else None, "MANUAL_RESOLUTION", args.evidence)
                output(store.get("jobs", args.id))
        elif args.command == "incident":
            if args.action == "add":
                output(store.record_incident(args.task_id, args.signature, args.description, args.evidence))
            else:
                output(store.rows("SELECT * FROM incidents ORDER BY created_at DESC"))
        elif args.command == "lesson":
            if args.action == "propose":
                output(store.candidate(args.incident_id, args.cause, args.correction, args.tag))
            elif args.action == "list":
                output(store.rows("SELECT * FROM lessons ORDER BY updated_at DESC"))
            elif args.action == "verify":
                output(store.verify_lesson(args.id, args.job))
            elif args.action in {"enable", "revoke"}:
                output(store.set_lesson_status(args.id, "ACTIVE" if args.action == "enable" else "REVOKED"))
            elif args.action == "revise":
                output(store.revise_lesson(args.id, args.cause, args.correction))
            elif args.action == "history":
                output(store.rows("SELECT * FROM lesson_history WHERE lesson_id=? ORDER BY id", (args.id,)))
        elif args.command == "backup":
            output({"database": store.backup(args.destination), "note": "这是数据库一致性备份；完整恢复还需 config-history 与 runs 成果目录。"})
    finally:
        store.close()
    return 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        raw = list(sys.argv[1:] if argv is None else argv)
        if not raw:
            parser().print_help()
            return 0
        trailing = []
        if "--" in raw:
            separator = raw.index("--")
            raw, trailing = raw[:separator], raw[separator + 1:]
        arguments = parser().parse_args(raw)
        if arguments.command == "job" and arguments.action == "run":
            arguments.argv = trailing
        elif trailing:
            raise WorkflowError("只有 job run 支持 -- 后的程序参数。")
        return execute(arguments)
    except KeyboardInterrupt:
        print("等待已停止；独立作业继续运行。使用 job cancel 才会请求取消。", file=sys.stderr)
        return 130
    except (WorkflowError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
