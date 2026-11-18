"use strict";

const $ = id => document.getElementById(id);
const webview = window.chrome?.webview;
const waiting = new Map();
let sequence = 0;
let snapshot;
let currentTask;
let filter = "all";
let refreshing;
let changing = false;
let viewKey = "";
let jobShown;
let evidenceLoad = 0;

const states = {
  READY: ["待开始", "ready", "任务已经保存，可以启动作业。"],
  WAITING_JOB: ["等待作业", "active", "等待独立进程回报最新状态。"],
  QUEUED: ["已排队", "active", "作业已记录，等待派发。"],
  STARTING: ["启动中", "active", "正在启动独立进程。"],
  RUNNING: ["运行中", "active", "独立进程正在执行。关闭窗口后可以回到同一任务。"],
  VERIFYING: ["待验收", "review", "作业已成功退出。请核对产物和验收条件，再保存验收证据。"],
  SUCCEEDED: ["执行成功", "review", "程序已成功退出。"],
  FAILED: ["执行失败", "attention", "先查看错误日志，修正原因后可以开始新阶段。"],
  CANCELLED: ["已取消", "attention", "核对已保留的结果，再决定是否开始新阶段。"],
  WAITING_INPUT: ["需人工处理", "attention", "查看最近作业和现有证据。"],
  WAITING_CAPACITY: ["等待额度", "attention", "查看原 session、额度说明和已授权的等待状态。"],
  UNKNOWN: ["状态未知", "attention", "先核对原进程和日志。确认状态前，此工作目录暂停派发。"],
  DONE: ["验收完成", "done", "已保存人工核对的验收证据。"],
};
const headings = {
  all: "工作台", attention: "需处理", review: "待验收", active: "执行中",
  ready: "待开始", done: "已完成",
};
const fileLabels = {
  "result.md": "结果", "stdout.log": "标准输出", "stderr.log": "错误日志",
  "worker.log": "运行器日志", "capacity.log": "额度等待日志",
};
const activeStates = new Set(["UNKNOWN", "STARTING", "QUEUED", "RUNNING"]);

if (webview) {
  webview.addEventListener("message", event => {
    const reply = event.data;
    const request = waiting.get(reply?.id);
    if (!request) return;
    waiting.delete(reply.id);
    if (reply.ok) request.resolve(reply.result);
    else request.reject(new Error(reply.error ?? "操作未完成。"));
  });
}

function request(op, args = {}) {
  if (!webview) return Promise.reject(new Error("请用 Start-CCAW.cmd 打开桌面版。"));
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    waiting.set(id, { resolve, reject });
    webview.postMessage({ id, op, args });
  });
}

function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").classList.toggle("error", error);
}

function errorIn(container, error) {
  container.querySelector(".form-error").textContent = error?.message ?? "";
}

function make(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text ?? "";
  if (className) node.className = className;
  return node;
}

function button(text, action, className) {
  const node = make("button", text, className);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

function stateInfo(state) {
  return states[state] ?? [state || "待核对", "attention", "查看对应的作业记录。"];
}

function records() {
  const byTask = new Map();
  for (const job of snapshot.jobs) {
    if (!byTask.has(job.task_id)) byTask.set(job.task_id, []);
    byTask.get(job.task_id).push(job);
  }
  return snapshot.tasks.map(task => {
    const jobs = byTask.get(task.id) ?? [];
    const job = jobs.find(item => item.status === "UNKNOWN")
      ?? jobs.find(item => activeStates.has(item.status)) ?? jobs[0];
    let status = job?.status ?? task.status;
    if (!activeStates.has(status) && task.status === "DONE") status = "DONE";
    if (status === "SUCCEEDED") status = "VERIFYING";
    return { task, jobs, job, status, group: stateInfo(status)[1] };
  });
}

function canDispatch({ task }) {
  return !snapshot.jobs.some(job => job.cwd === task.project && activeStates.has(job.status));
}

function render() {
  if (!snapshot) return;
  const rows = records();
  const row = rows.find(item => item.task.id === currentTask);
  for (const control of document.querySelectorAll("[data-filter]")) {
    const group = control.dataset.filter;
    const count = rows.filter(item => group === "all" ? item.group !== "done" : item.group === group).length;
    control.querySelector("span").textContent = count;
    if (!row && group === filter) control.setAttribute("aria-current", "page");
    else control.removeAttribute("aria-current");
  }
  $("ledger-path").textContent = snapshot.home;
  $("new-task").disabled = false;
  $("board").hidden = Boolean(row);
  $("detail").hidden = !row;
  $("page-title").textContent = row ? "任务详情" : headings[filter];
  const signature = row
    ? JSON.stringify([row, canDispatch(row)])
    : JSON.stringify([filter, rows.map(({ task, status }) => [task.id, task.title, status])]);
  if (signature === viewKey) return;
  viewKey = signature;
  if (row) renderDetail(row);
  else renderBoard(rows);
}

function renderBoard(rows) {
  const sections = [];
  const groups = filter === "all" ? ["attention", "review", "active", "ready"] : [filter];
  for (const group of groups) {
    const list = rows.filter(row => row.group === group);
    if (!list.length) continue;
    const section = make("section", "", "group");
    section.append(make("h2", headings[group]));
    for (const row of list) {
      const [label] = stateInfo(row.status);
      const card = button("", () => selectTask(row.task.id), "task-card");
      card.dataset.taskId = row.task.id;
      card.append(make("span", label, "pill " + row.group), make("strong", row.task.title));
      section.append(card);
    }
    sections.push(section);
  }
  if (!sections.length)
    sections.push(make("p", filter === "all" ? "没有未完成的任务。点击「新建任务」开始。" : "此状态暂无任务。", "empty"));
  $("board").replaceChildren(...sections);
}

async function selectTask(taskId) {
  currentTask = taskId;
  render();
  window.scrollTo(0, 0);
  try {
    await request("select", { task_id: taskId });
  } catch (error) {
    notice(error.message, true);
  }
}

function renderDetail(row) {
  const { task, job, status, group } = row;
  const [label, , next] = stateInfo(status);
  $("task-state").textContent = label;
  $("task-state").className = "pill " + group;
  $("task-title").textContent = task.title;
  $("task-goal").textContent = task.goal || "未记录";
  $("task-acceptance").textContent = task.acceptance || "未记录";
  $("task-project").textContent = "工作目录  " + task.project;
  $("next-step").textContent = next;
  const run = $("primary-action");
  const newAttempt = $("new-attempt");
  newAttempt.hidden = !(job && ["FAILED", "CANCELLED"].includes(status) && canDispatch(row) && task.kind !== "chat");
  newAttempt.onclick = () => showRun(row);
  run.hidden = !job && (status !== "READY" || task.kind === "chat");
  run.disabled = !job && !canDispatch(row);
  if (job) {
    run.textContent = {
      VERIFYING: "核对结果", UNKNOWN: "核对状态", FAILED: "查看错误", WAITING_CAPACITY: "查看等待详情",
    }[status] ?? "查看作业";
    run.onclick = () => showJob(job.id);
  } else {
    run.textContent = task.kind === "code" ? "启动 Codex 作业" : "运行本地脚本";
    run.onclick = () => showRun(row);
    if (status === "READY" && task.kind === "chat")
      $("next-step").textContent = "在终端使用 task export 复制任务包到 ChatGPT。";
    else if (status === "READY" && run.disabled)
      $("next-step").textContent = "此工作目录还有运行中或待核对的作业。";
  }
  const entries = row.jobs.map(item => {
    const label = stateInfo(item.status)[0] + (item.exit_code == null ? "" : " · 退出码 " + item.exit_code);
    const open = button(label, () => showJob(item.id), "job-link");
    open.append(make("small", [item.dedupe_key, item.id].filter(Boolean).join(" · ")));
    return open;
  });
  $("job-list").replaceChildren(...(entries.length ? entries : [make("p", "尚无作业。", "empty")]));
  $("accepted").hidden = status !== "DONE";
  if (status === "DONE") {
    $("acceptance-record").textContent = "正在读取验收证据…";
    request("task_details", { task_id: task.id }).then(packet => {
      if (currentTask === task.id)
        $("acceptance-record").textContent = packet.acceptance_event?.payload?.evidence ?? "此任务没有保存的验收证据。";
    }).catch(error => {
      if (currentTask === task.id) $("acceptance-record").textContent = error.message;
    });
  }
}

async function refresh() {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try {
      const first = !snapshot;
      snapshot = await request("snapshot");
      if (first) {
        currentTask = snapshot.selected_task;
        notice("");
      } else if ($("notice").classList.contains("error")) {
        notice("");
      }
      render();
    } catch (error) {
      notice(error.message, true);
    }
  })();
  try { await refreshing; } finally { refreshing = null; }
}

function showBoard(group) {
  filter = group;
  currentTask = null;
  render();
  window.scrollTo(0, 0);
}

function onSubmit(formId, action) {
  const form = $(formId);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (changing) return;
    const submit = form.querySelector("button:last-child");
    const dialog = form.closest("dialog");
    changing = true;
    submit.disabled = true;
    errorIn(form);
    try {
      await action(Object.fromEntries(new FormData(form)));
      dialog.close();
      if (refreshing) await refreshing;
      await refresh();
    } catch (error) {
      errorIn(form, error);
    } finally {
      changing = false;
      submit.disabled = false;
    }
  });
}

function showRun(row) {
  const form = $("run-form");
  form.reset();
  errorIn(form);
  const script = row.task.kind !== "code";
  $("script-fields").hidden = $("script-fields").disabled = !script;
  $("codex-fields").hidden = $("codex-fields").disabled = script;
  $("run-title").textContent = script ? "运行本地脚本" : "启动 Codex 作业";
  $("start-job").textContent = script ? "启动脚本" : "启动 Codex 作业";
  const keys = new Set(row.jobs.map(job => job.dedupe_key));
  let key = "main";
  for (let stage = 2; keys.has(key); stage++) key = "run-" + stage;
  form.elements.key.value = key;
  if (script) form.elements.executable.value = "python.exe";
  else {
    form.elements.role.value = ["default", "economy", "expert"].includes(row.task.role) ? row.task.role : "default";
    form.elements.prompt.value = "按任务目标执行，并记录可核对的结果。";
  }
  form.dataset.taskId = row.task.id;
  form.dataset.kind = script ? "script" : "code";
  $("run-dialog").showModal();
  if (script) request("host_info").then(info => {
    if ($("run-dialog").open && form.dataset.taskId === row.task.id && form.elements.executable.value === "python.exe")
      form.elements.executable.value = info.python;
  }).catch(() => {});
}

async function showLog(job, name) {
  const token = ++evidenceLoad;
  try {
    const item = await request("read_log", { job_id: job.id, name });
    if (token !== evidenceLoad) return;
    $("log-preview").textContent = item.path + "\n\n" + (item.text || "文件暂时没有内容。")
      + (item.truncated ? "\n\n已显示前 128 KiB。作业目录包含完整文件。" : "");
  } catch (error) {
    if (token === evidenceLoad) $("log-preview").textContent = error.message;
  }
}

function diagnostics(job) {
  evidenceLoad++;
  const lines = [
    ["工作目录", job.cwd], ["作业目录", job.diagnostics.run_directory],
    ["Session", job.session_id], ["原因类别", job.error_kind], ["工作进程 PID", job.pid],
    ["进程标识", job.process_token], ["子进程 PID", job.child_pid], ["子进程标识", job.child_token],
    ["更新时间", job.updated_at],
  ].filter(([, value]) => value != null).map(([label, value]) => label + "  " + value);
  if (job.diagnostics.auto_resume)
    lines.push("", "额度接续", JSON.stringify(job.diagnostics.auto_resume, null, 2));
  for (const event of job.diagnostics.recent_events ?? [])
    lines.push("", event.created_at + "  " + event.kind, JSON.stringify(event.payload));
  $("log-preview").textContent = lines.join("\n");
}

async function showJob(id) {
  const dialog = $("job-dialog");
  const token = ++evidenceLoad;
  jobShown = null;
  errorIn(dialog);
  $("job-state").textContent = "正在核对…";
  $("job-title").textContent = id;
  $("job-hint").textContent = "";
  $("job-files").replaceChildren();
  $("log-preview").textContent = "";
  $("accept-job").hidden = true;
  $("job-operations").hidden = true;
  if (!dialog.open) dialog.showModal();
  try {
    const job = await request("job_details", { job_id: id });
    if (!dialog.open || token !== evidenceLoad) return;
    jobShown = job;
    $("job-state").textContent = stateInfo(job.status)[0];
    $("job-title").textContent = job.id + (job.exit_code == null ? "" : " · 退出码 " + job.exit_code);
    $("job-hint").textContent = job.diagnostics.next_action ?? stateInfo(job.status)[2];
    const files = job.diagnostics.files;
    const controls = [button("运行记录", () => diagnostics(job))];
    for (const [name, label] of Object.entries(fileLabels)) {
      if (!files[name]) continue;
      const control = button(label, () => showLog(job, name));
      control.dataset.log = name;
      controls.push(control);
    }
    $("job-files").replaceChildren(...controls);
    $("job-operations").hidden = false;
    const row = records().find(item => item.task.id === job.task_id);
    $("accept-job").hidden = job.status !== "SUCCEEDED" || row?.job?.id !== job.id || row.task.status === "DONE";
    $("recheck-job").onclick = async () => { await refresh(); await showJob(job.id); };
    for (const control of $("job-operations").querySelectorAll("[data-job-action]")) control.remove();
    if (job.status === "QUEUED") appendJobAction("启动已排队作业", "launch");
    if (["QUEUED", "RUNNING", "STARTING"].includes(job.status)) appendJobAction("取消作业…", "cancel");
    if (job.status === "WAITING_CAPACITY" && job.diagnostics.auto_resume?.enabled) {
      appendJobAction("重启等待", "watch");
      appendJobAction("停止自动接续", "unwatch");
    }
    const firstLog = job.status === "FAILED" ? "stderr.log" : job.status === "SUCCEEDED"
      ? (files["result.md"] ? "result.md" : "stdout.log") : null;
    if (firstLog && files[firstLog]) showLog(job, firstLog);
    else diagnostics(job);
  } catch (error) {
    if (token === evidenceLoad) errorIn(dialog, error);
  }
}

function appendJobAction(label, action) {
  const control = button(label, async () => {
    const job = jobShown;
    if (changing || !job) return;
    if (action === "cancel" && control.dataset.armed !== "yes") {
      control.dataset.armed = "yes";
      control.textContent = "确认取消作业";
      return;
    }
    changing = true;
    control.disabled = true;
    try {
      await request("job_action", { job_id: job.id, action });
      await refresh();
      await showJob(job.id);
    } catch (error) {
      errorIn($("job-dialog"), error);
    } finally {
      changing = false;
      control.disabled = false;
    }
  });
  control.dataset.jobAction = action;
  $("job-operations").append(control);
}

async function openFolder(destination) {
  if (!jobShown) return;
  try {
    await request("folder_path", { job_id: jobShown.id, destination });
  } catch (error) {
    errorIn($("job-dialog"), error);
  }
}

for (const control of document.querySelectorAll("[data-filter]"))
  control.onclick = () => showBoard(control.dataset.filter);
for (const control of document.querySelectorAll("[data-close]"))
  control.onclick = () => { if (!changing) control.closest("dialog").close(); };
for (const dialog of document.querySelectorAll("dialog"))
  dialog.addEventListener("cancel", event => { if (changing) event.preventDefault(); });

$("back").onclick = () => showBoard(filter);
$("refresh").onclick = refresh;
$("new-task").onclick = () => {
  const form = $("task-form");
  form.reset();
  errorIn(form);
  form.elements.project.value = snapshot.project;
  $("task-dialog").showModal();
};
$("open-project").onclick = () => openFolder("project");
$("open-run").onclick = () => openFolder("run");
$("accept-job").onclick = () => {
  const task = snapshot.tasks.find(item => item.id === jobShown?.task_id);
  if (!task) return;
  const form = $("accept-form");
  form.reset();
  errorIn(form);
  $("accept-criteria").textContent = task.acceptance;
  $("accept-dialog").showModal();
};

onSubmit("task-form", async values => {
  const task = await request("create_task", values);
  currentTask = task.id;
  notice("任务已创建。");
  try { await request("select", { task_id: task.id }); }
  catch (error) { notice("任务已创建。查看位置保存失败：" + error.message, true); }
});
onSubmit("run-form", async values => {
  const { taskId: task_id, kind } = $("run-form").dataset;
  const job = kind === "script"
    ? await request("start_script", {
      task_id, key: values.key,
      command: [values.executable.trim(), ...(values.arguments ? values.arguments.split("\n") : [])],
    })
    : await request("start_agent", {
      task_id, key: values.key, role: values.role, prompt: values.prompt, auto_resume: Boolean(values.auto_resume),
    });
  notice("作业已记录  " + job.id);
});
onSubmit("accept-form", async values => {
  await request("accept_task", { task_id: jobShown.task_id, job_id: jobShown.id, evidence: values.evidence });
  $("job-dialog").close();
  notice("验收已保存。");
});

refresh();
window.setInterval(() => { if (!changing) refresh(); }, 2000);
