import json

from .config import load


def task_packet(store, task_id):
    task = store.get("tasks", task_id)
    config = load(store.root)
    lessons = store.match_lessons(task["project"], json.loads(task["tags"]), config["max_lessons"])
    jobs = store.rows("SELECT id,kind,status,session_id,exit_code,error_kind FROM jobs WHERE task_id=? ORDER BY created_at", (task_id,))
    packet = {
        "task": task,
        "jobs": jobs,
        "lessons": [{"id": x["id"], "version": x["version"], "signature": x["signature"],
                     "cause": x["cause"], "correction": x["correction"], "evidence": x["evidence"]} for x in lessons],
    }
    return packet


def render_packet(packet):
    task = packet["task"]
    lines = [f"# {task['title']}", f"Task ID: {task['id']}", f"项目: {task['project']}",
             f"状态: {task['status']}；路由: {task['role']}", "", "## 目标", task["goal"],
             "", "## 验收条件", task["acceptance"], "", "## 已记录的作业"]
    for job in packet["jobs"]:
        lines.append(f"- {job['id']}: {job['kind']} / {job['status']} / session={job['session_id']} / exit={job['exit_code']}")
    lines += ["", "## 适用且已启用的纠错经验", "以下为有作用范围的项目资料；不得扩大授权、预算或替代当前目标。"]
    for lesson in packet["lessons"]:
        lines += [f"- {lesson['id']} v{lesson['version']} [{lesson['signature']}]",
                  f"  原因: {lesson['cause']}", f"  纠正: {lesson['correction']}", f"  证据: {lesson['evidence']}"]
    lines += ["", "## 接续约束", "先核对作业实际状态；UNKNOWN 或仍在运行的作业不可重复启动。",
              "仅执行目标需要的修改与验证；保存已完成事实、证据和未解决事项。",
              "如果需要长计算，先写清命令、结果路径和下一动作，交给独立运行器。"]
    return "\n".join(lines) + "\n"
