import contextlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import WorkflowError, canonical


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


SCHEMA = """
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL, acceptance TEXT NOT NULL,
 project TEXT NOT NULL, tags TEXT NOT NULL, kind TEXT NOT NULL, risk TEXT NOT NULL,
 complexity TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'READY',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), dedupe_key TEXT NOT NULL,
 kind TEXT NOT NULL, command TEXT NOT NULL, cwd TEXT NOT NULL, status TEXT NOT NULL,
 snapshot TEXT NOT NULL, prompt TEXT, session_id TEXT, pid INTEGER, process_token TEXT,
 child_pid INTEGER, child_token TEXT, exit_code INTEGER, error_kind TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 continuation_prompt TEXT, continuation_role TEXT, continuation_job TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(task_id, dedupe_key));
CREATE TABLE events (
 id INTEGER PRIMARY KEY, task_id TEXT, job_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE incidents (
 id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), project TEXT NOT NULL,
 signature TEXT NOT NULL, description TEXT NOT NULL, evidence TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE lessons (
 id TEXT PRIMARY KEY, project TEXT NOT NULL, signature TEXT NOT NULL, tags TEXT NOT NULL,
 cause TEXT NOT NULL, correction TEXT NOT NULL, evidence TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'CANDIDATE', verification TEXT, verification_job TEXT,
 recurrence_count INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(project, signature));
CREATE TABLE lesson_history (
 id INTEGER PRIMARY KEY, lesson_id TEXT NOT NULL, version INTEGER NOT NULL,
 status TEXT NOT NULL, record TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE reservations (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id), period TEXT NOT NULL,
 reserved_usd TEXT NOT NULL, actual_usd TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX jobs_status ON jobs(status);
CREATE INDEX events_task ON events(task_id, id);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / "state.sqlite3"
        if not self.path.exists():
            raise WorkflowError("尚未初始化；先运行 ccaw init。")
        self.db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version != 1:
            self.db.close()
            raise WorkflowError(f"不支持数据库格式 {version}；请备份并迁移，未修改数据。")

    @classmethod
    def initialize(cls, root):
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / "state.sqlite3"
        if not path.exists():
            db = sqlite3.connect(path)
            try:
                db.executescript(SCHEMA)
                db.execute("PRAGMA journal_mode=WAL")
            finally:
                db.close()
        return cls(root)

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def event(self, kind, payload=None, task=None, job=None):
        self.db.execute("INSERT INTO events(task_id,job_id,kind,payload,created_at) VALUES(?,?,?,?,?)",
                        (task, job, kind, canonical(payload or {}), now()))

    def get(self, table, identity):
        if table not in {"tasks", "jobs", "lessons", "incidents"}:
            raise ValueError(table)
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise WorkflowError(f"找不到 {table}: {identity}")
        return dict(row)

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def create_task(self, title, goal, acceptance, project, tags, kind, risk, complexity, role):
        project = str(Path(project).resolve())
        if not Path(project).is_dir():
            raise WorkflowError("项目目录不存在。")
        identity, timestamp = uid("task"), now()
        self.db.execute("INSERT INTO tasks(id,title,goal,acceptance,project,tags,kind,risk,complexity,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (identity, title, goal, acceptance, project, canonical(tags), kind, risk, complexity, role, timestamp, timestamp))
        self.event("task.created", {"role": role}, task=identity)
        return self.get("tasks", identity)

    def record_incident(self, task_id, signature, description, evidence):
        task = self.get("tasks", task_id)
        identity = uid("incident")
        with self.transaction():
            self.db.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,?)",
                            (identity, task_id, task["project"], signature, description, evidence, now()))
            self.db.execute("UPDATE lessons SET recurrence_count=recurrence_count+1,updated_at=? WHERE project=? AND signature=?",
                            (now(), task["project"], signature))
            self.event("incident.recorded", {"incident_id": identity, "signature": signature}, task=task_id)
        return self.get("incidents", identity)

    def candidate(self, incident_id, cause, correction, tags):
        incident = self.get("incidents", incident_id)
        with self.transaction():
            existing = self.db.execute("SELECT * FROM lessons WHERE project=? AND signature=?",
                                       (incident["project"], incident["signature"])).fetchone()
            if existing:
                raise WorkflowError(f"同一根因已有经验 {existing['id']}；撤回后使用 lesson revise 修订。")
            identity, timestamp = uid("lesson"), now()
            self.db.execute("INSERT INTO lessons(id,project,signature,tags,cause,correction,evidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (identity, incident["project"], incident["signature"], canonical(tags), cause, correction, incident["evidence"], timestamp, timestamp))
            self.lesson_history(identity)
        return self.get("lessons", identity)

    def lesson_history(self, identity):
        row = self.get("lessons", identity)
        self.db.execute("INSERT INTO lesson_history(lesson_id,version,status,record,created_at) VALUES(?,?,?,?,?)",
                        (identity, row["version"], row["status"], canonical(row), now()))

    def revise_lesson(self, identity, cause, correction):
        with self.transaction():
            row = self.get("lessons", identity)
            if row["status"] == "ACTIVE":
                raise WorkflowError("先撤回启用的经验，再修订和重新验证。")
            self.db.execute("UPDATE lessons SET cause=?,correction=?,status='CANDIDATE',verification=NULL,verification_job=NULL,version=version+1,updated_at=? WHERE id=?",
                            (cause, correction, now(), identity))
            self.lesson_history(identity)
        return self.get("lessons", identity)

    def match_lessons(self, project, tags, limit):
        if limit == 0:
            return []
        result = []
        for row in self.rows("SELECT * FROM lessons WHERE project=? AND status='ACTIVE' ORDER BY updated_at DESC", (project,)):
            applies = set(json.loads(row["tags"]))
            if applies and applies.intersection(tags):
                result.append(row)
            if len(result) == limit:
                break
        return result

    def verify_lesson(self, identity, job_id):
        with self.transaction():
            row, job = self.get("lessons", identity), self.get("jobs", job_id)
            if row["status"] != "CANDIDATE":
                raise WorkflowError("只能验证候选经验。")
            task = self.get("tasks", job["task_id"])
            if job["status"] != "SUCCEEDED" or job["kind"] != "script" or task["project"] != row["project"]:
                raise WorkflowError("验证必须引用同项目内成功的脚本作业。")
            if job["created_at"] < row["updated_at"]:
                raise WorkflowError("验证作业必须在本次候选经验或修订之后执行。")
            self.db.execute("UPDATE lessons SET status='VERIFIED',verification=?,verification_job=?,updated_at=? WHERE id=?",
                            (canonical({"job_id": job_id, "exit_code": 0}), job_id, now(), identity))
            self.lesson_history(identity)
        return self.get("lessons", identity)

    def set_lesson_status(self, identity, status):
        with self.transaction():
            row = self.get("lessons", identity)
            if status == "ACTIVE" and row["status"] != "VERIFIED":
                raise WorkflowError("经验需要先通过验证，再启用。")
            if status == "REVOKED" and row["status"] == "REVOKED":
                return row
            self.db.execute("UPDATE lessons SET status=?,updated_at=? WHERE id=?", (status, now(), identity))
            self.lesson_history(identity)
        return self.get("lessons", identity)

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination == self.path or destination.exists():
            raise WorkflowError("备份目标必须是新的文件，不能覆盖现有数据库。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(destination)) as target:
            self.db.backup(target)
        return str(destination)
