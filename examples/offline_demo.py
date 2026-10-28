"""A real, isolated, zero-model demonstration of execution and corrective reuse."""
import copy
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ccaw.config import DEFAULT_CONFIG, save_version
from ccaw.context import task_packet
from ccaw.runner import enqueue, start, wait_for
from ccaw.store import Store


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="ccaw-demo-") as folder:
        root = Path(folder)
        project = root / "experiment"
        project.mkdir()
        store = Store.initialize(root / "state")
        save_version(store.root, copy.deepcopy(DEFAULT_CONFIG))
        try:
            task = store.create_task("演示：一次计算与纠错复用", "独立运行后复用已验证经验", "计算只执行一次，经验启用后可匹配", project, ["recovery"], "script", "low", "low", "script")
            incident = store.record_incident(task["id"], "duplicate-launch", "演示故障：恢复时可能重复启动", "演示场景，非真实生产事故")
            lesson = store.candidate(incident["id"], "启动缺少稳定运行标识", "同阶段复用幂等键；未知状态先核对。", ["recovery"])
            code = "import time; from pathlib import Path; time.sleep(.5); p=Path('count'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
            argv = [sys.executable, "-c", code]
            job, _ = enqueue(store, task["id"], "script", command=argv, key="experiment-1")
            start(store, job["id"])
            same, created = enqueue(store, task["id"], "script", command=argv, key="experiment-1")
            assert not created and same["id"] == job["id"]
            assert wait_for(store, job["id"], 10)["status"] == "SUCCEEDED"
            assert (project / "count").read_text() == "x"
            check, _ = enqueue(store, task["id"], "script", key="regression", command=[sys.executable, "-c", "from pathlib import Path; assert Path('count').read_text() == 'x'"])
            start(store, check["id"])
            assert wait_for(store, check["id"], 10)["status"] == "SUCCEEDED"
            store.verify_lesson(lesson["id"], check["id"])
            store.set_lesson_status(lesson["id"], "ACTIVE")
            assert len(task_packet(store, task["id"])["lessons"]) == 1
            store.set_lesson_status(lesson["id"], "REVOKED")
            assert task_packet(store, task["id"])["lessons"] == []
            print("演示通过：独立计算、重复请求去重、真实断言验证、经验匹配与撤回。")
            print("本次未调用模型、未联网、未使用你的正式任务数据库。")
        finally:
            store.close()


if __name__ == "__main__":
    main()
