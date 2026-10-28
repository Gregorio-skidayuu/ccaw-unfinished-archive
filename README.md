# CCAW — 未完成项目封存

状态：未完成，开发暂停。此副本用于保存截至 2026-10-28 的原型源码，不代表发布或完成验收。源码版本声明为 0.3.0；下方原有版本描述属于历史说明，以 [封存状态](docs/STATUS.md) 为准。

# CCAW · 我的工作流

以 Codex 为主执行器的个人工作流工具。当前交付为 **v0.2 命令行版本**，支持 Windows 和 Linux，运行时只需 Python 3.11+ 标准库。

目标是在验收质量不下降的前提下，减少重复推理、重复计算和无效交接，并把经过验证的工程经验用于后续任务。

## 已实现

- SQLite 持久化任务、执行尝试、完成事件、故障和纠错经验。
- 离线规则路由：脚本、基础对话、经济档、主力档、专家档。
- 独立进程运行计算；启动终端退出后，worker 继续等待实际计算结果。
- 同阶段幂等键去重；同项目串行派发；状态未知时阻止重新启动。
- Codex CLI JSONL 接入，实时记录具体 session ID；额度错误进入 WAITING_CAPACITY，手动恢复时使用指定 session。
- 可按作业显式开启额度自然恢复后的自动接续：独立后台程序读取额度，确认许可后沿原 session 继续，支持查看、停止和重启等待。
- 脚本成功后自动启动一次 Codex 分析；中断后可补发尚未派发的接续。
- 纠错闭环：故障记录 → 候选措施 → 实际回归作业 → 验证 → 启用 → 按项目及标签复用 → 撤回/修订。
- 模型映射和规则配置版本留档、回退；每次执行保留配置与经验版本快照。
- 可选 DeepSeek Chat Completions 文本适配器，默认关闭，显式授权、预算预留、费用记录、禁止自动重试。
- 数据库一致性备份、任务包导出和无需密钥的离线演示。

## Windows 安装

先安装 Python 3.11 或更新版本。解压源码包后，在该目录打开 PowerShell：

```powershell
.\Install.ps1
```

默认安装到 `C:\CCAW`，不下载任何 Python 依赖。目标目录非空时会停止复制，避免覆盖已有文件；如果已把源码解压到该目录，在那里直接运行安装脚本即可。

如果系统的脚本策略阻止执行，可只对这一次进程执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\Install.ps1
```

这条命令只用于运行你已检查的本地安装脚本，不更改系统的永久脚本策略。

安装后：

```powershell
Set-Location 'C:\CCAW'
.\ccaw.ps1 doctor
.\ccaw.ps1 demo
```

直接运行 `.\ccaw.ps1` 可查看入门帮助，具体操作可用 `.\ccaw.ps1 task --help` 或 `.\ccaw.ps1 job --help` 查询。`doctor` 会检查配置、数据库版本和完整性；未初始化时会给出下一步。Codex 未安装不影响离线脚本与演示。

## 第一个完整任务（无需模型）

在安装目录运行以下 PowerShell 命令，自动取得任务与作业 ID：

```powershell
$task = .\ccaw.ps1 task add '检查本地工作流' --kind script --accept '脚本成功输出 CCAW ready' | ConvertFrom-Json
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
$run = .\ccaw.ps1 job run $task.id --key quickstart --wait '--' $python -c 'print("CCAW ready")' | ConvertFrom-Json
.\ccaw.ps1 job show $run.job.id
# 确认 status 为 SUCCEEDED，并检查 diagnostics.files 中的 stdout.log 后验收：
.\ccaw.ps1 task done $task.id --evidence '已检查 stdout.log，输出 CCAW ready'
```

`SUCCEEDED` 表示本次程序成功退出，`task done` 才表示你已按任务验收条件确认完成。

Windows 入口默认使用安装目录下的 `.ccaw`；直接运行 `python -m ccaw` 默认使用**当前目录**下的 `.ccaw`。跨目录查看同一任务时，用 `python -m ccaw --home 'C:\CCAW\.ccaw' task list` 明确指定账本；`--home` 放在命令之前。

Codex 集成还需在本机安装并使用 ChatGPT 账号登录 Codex CLI；任务账本、脚本与演示不依赖 Codex 或 API 密钥。适配器会核对订阅登录，并从 Codex 子进程移除 API Key 覆盖，避免意外走按量计费。自定义 Codex 模型供应商暂不接入。模型 ID 为 null 时沿用 Codex 本机默认值，**初始的三个模型角色并未自动选择三种不同模型**。

## Linux / 开发环境

在源码目录直接运行，无需 pip：

```bash
python3 -m ccaw init
python3 -m ccaw doctor
python3 examples/offline_demo.py
python3 -m unittest discover -s tests -v
```

也可以在自己的虚拟环境执行 `pip install -e .`，然后使用 `ccaw` 命令。

## 1. 新建任务与路由

以下 PowerShell 命令输出 JSON；记录其中的 task ID：

```powershell
.\ccaw.ps1 task add '修复实验恢复逻辑' --goal '恢复时复用原运行，避免重复计算' --accept '中断恢复后只保留一次计算' --project 'C:\ExampleProject' --tag recovery --risk medium --complexity medium
.\ccaw.ps1 task list
.\ccaw.ps1 route --risk high --complexity low
```

路由不调用任何模型。`--kind script` 表示规则明确的计算，`--kind chat` 表示需求或讨论；高风险或高复杂度进入 expert，低复杂度及低风险批量任务进入 economy，其余进入 default。

## 2. 独立运行长计算

把 `<TASK_ID>` 替换为真实 ID；分隔符后是程序和独立参数，不拼接 shell 字符串。PowerShell 调用 `.ps1` 时必须把分隔符写成 `'--'`，否则 PowerShell 会先吞掉它；直接调用 Python 或 `ccaw` 可使用普通 `--`：

```powershell
.\ccaw.ps1 job run <TASK_ID> --key experiment-001 '--' python train.py --epochs 30
.\ccaw.ps1 job list
.\ccaw.ps1 job show <JOB_ID>
.\ccaw.ps1 job wait <JOB_ID>
```

`job show` 保留原作业字段，并在 `diagnostics` 中提供已有日志和结果文件的绝对路径、最近 10 条事件及下一步提示。失败时先检查 `stderr.log`、`worker.log` 和事件原因；无需手动在数据库中寻找日志位置。

`job wait` 由普通程序等待，不调用模型。关闭等待窗口不取消作业；`job cancel <JOB_ID>` 才请求终止。worker 自身使用进程原生等待。

同一 task 和同一阶段重复提交同一 `--key`，返回同一作业；命令或指令不同但复用旧键会报错。明确的新实验阶段使用新键，避免误启动昂贵计算。

机器关机、休眠或操作系统杀死进程仍会影响计算；实验自身的检查点与恢复参数由实验程序提供。若 worker 中断而无法确认计算结果，状态变成 UNKNOWN，阻止自动重启。先核对真实运行和日志，再明确确认：

```powershell
.\ccaw.ps1 job resolve <JOB_ID> --status FAILED --evidence '已核对进程已退出，见实验日志末尾'
```

仍能识别到存活进程时，工具会拒绝宣告其结束。

## 3. Codex 执行与额度恢复

项目需是已初始化的 Git 仓库；CCAW 不绕过 Codex 的仓库检查、沙箱或登录要求。

```powershell
.\ccaw.ps1 job agent <TASK_ID> --role default --key implement-1 --prompt '完成目标并运行相关验证'
```

脚本完成后自动分析：

```powershell
.\ccaw.ps1 job run <TASK_ID> --key experiment-002 --then '读取此次实验结果，报告结论和下一步，不重跑训练' --then-role default '--' python train.py
```

只在脚本成功时派发接续，重复完成事件不会生成第二次相同接续。若中断发生在成功与接续派发之间，可用 `job continue <原作业ID>` 补发；未启动的 QUEUED 作业也可通过 `job launch <作业ID>` 派发。

额度不足时保存 WAITING_CAPACITY 和 session ID，**不会反复尝试或静默改走付费通道**。额度恢复后：

```powershell
.\ccaw.ps1 job agent <TASK_ID> --key resume-1 --resume <旧JOB_ID> --prompt '从已完成进度继续，先核对现有作业与成果'
```

同一 Codex session 可继续；跨模型或跨供应商时，应建立带任务包的新执行尝试，不假定会话格式互通。未开启自动继续的现有作业保持手动恢复。

### 额度自然恢复后自动继续

启动 Codex 作业时显式添加 `--auto-resume`：

```powershell
.\ccaw.ps1 job agent <TASK_ID> --key implement-auto-1 --auto-resume --prompt '完成目标并验证；接续前核对已有成果'
.\ccaw.ps1 job show <JOB_ID>
```

首次启动带此开关的作业时，会先确认并绑定当前订阅账号；接口无法读取时会报告 `AUTO_RESUME_SETUP` 并停止启动，你仍可不带该开关手动执行。遇到额度限制后，独立 Python 后台程序接管等待。它通过 Codex app-server 读取订阅额度，不启动模型来探测。到达窗口重置时间后会再次查询，只有接口明确允许使用、账号和刷新周期可核对时才接续具体 session；缺失或不支持的字段会继续等待。同一等待链每次额度查询至少相隔 5 分钟，耗尽窗口重置更晚时按该时间等待；同一刷新周期至多一次自动接续。该实现使用 [官方额度读取协议](https://learn.chatgpt.com/docs/app-server)，不消费重置券、不购买额度，也不切换到付费 API。

授权沿当前作业的恢复链保留；成功、失败、取消或需要人工核对时停止。原指令、任务与 session 身份会保留，每次接续是独立 Job。`job show` 的 `diagnostics.auto_resume` 显示当前后继作业、等待原因、下次检查时间和日志路径；初始作业可能仍显示 WAITING_CAPACITY，应以该字段中的 `current_job_id` / `current_status` 查看恢复进度。

```powershell
# 只停止后续自动继续；已在执行的计算保持运行
.\ccaw.ps1 job unwatch <原作业或后继JOB_ID>
# 请求取消正在执行的作业，并撤销此恢复链的自动继续
.\ccaw.ps1 job cancel <当前JOB_ID>
# 等待程序意外退出或机器重启后，重新启动已获授权的等待链
.\ccaw.ps1 job watch <原作业或后继JOB_ID>
```

`unwatch` 会取消尚未派发的自动队列，撤销后不能用 `watch` 静默重新授权；需要另建显式带 `--auto-resume` 的执行尝试。已排队的自动接续在恢复时会重新核对账号、角色、许可和周期；账号、角色或周期变化会取消旧自动队列并要求人工核对。人工推进任务、任务已验收或项目中存在 UNKNOWN 作业时不会自动启动。

关闭聊天或等待窗口不终止后台等待；机器关机、进程被系统结束仍会中断后台程序。此版本提供 `job watch` 的显式恢复入口，尚未安装开机自启服务。`--wait` 等待的是本次 Job，遇到 WAITING_CAPACITY 会返回退出码 2，即使后台自动继续已获授权。

## 4. 把错误变成后续会应用的措施

```powershell
.\ccaw.ps1 incident add <TASK_ID> --signature duplicate-launch --description '恢复时重复启动实验' --evidence 'logs\run-001.log'
.\ccaw.ps1 lesson propose <INCIDENT_ID> --cause '缺少启动回执核对' --correction '恢复先检查唯一运行 ID，状态不明时停止重启' --tag recovery
.\ccaw.ps1 job run <TASK_ID> --key regression-1 --wait '--' python tests\test_recovery.py
.\ccaw.ps1 lesson verify <LESSON_ID> --job <回归JOB_ID>
.\ccaw.ps1 lesson enable <LESSON_ID>
```

工具检查回归作业必须来自同一项目、成功退出，且在本次候选或修订之后执行。测试是否足以证明根因修复，仍需要工程判断；随意运行一个返回 0 的脚本不能建立有意义的证据。

后续同项目、标签匹配的任务会自动带入该经验，默认最多 5 条。可读取注入结果、撤回经验并重新修订：

```powershell
.\ccaw.ps1 task show <TASK_ID>
.\ccaw.ps1 lesson revoke <LESSON_ID>
.\ccaw.ps1 lesson revise <LESSON_ID> --cause '新确认的根因' --correction '更精确的措施'
.\ccaw.ps1 lesson history <LESSON_ID>
```

修订后重新验证才能启用。一次性故障可以只记录 incident；不强制为每次小错误新增长期规则。当前版本由你或执行中的智能体明确录入和验证，尚未自动推断根因或自行修改生产代码。

## 5. 模型配置与可选 API

```powershell
.\ccaw.ps1 config show
Copy-Item '.ccaw\config.json' 'candidate-config.json'
# 编辑 candidate-config.json 后：
.\ccaw.ps1 config apply candidate-config.json
.\ccaw.ps1 config history
.\ccaw.ps1 config rollback <完整版本哈希>
```

按账号实际可用能力设置 `roles.economy/default/expert.model`。配置中不固定“最强”或“最便宜”型号；发布与价格变化后，应先核验并做小范围验证。当前配置发布提供结构验证和版本记录，尚无自动模型排行榜或自动灰度系统。

可选 API 使用官方 DeepSeek 风格 `/chat/completions` 文本接口。启用前核实模型、价格和数据发送范围，填写 `paid_api.model`、输入/输出美元每百万 token 价格、月度限额和 `enabled`，并把相应角色的 backend 改为 `api`。默认 `reasoning_effort=none`，输出上限 2048，均可显式配置。

密钥只放环境变量，不写入配置、命令参数或 Git。使用角色对应的 API 时还需显式添加 `--allow-api`：

```powershell
.\ccaw.ps1 job agent <TASK_ID> --role economy --key review-1 --allow-api --prompt '针对任务包进行低风险文本审阅'
```

API 发送的是目标、验收、作业摘要及匹配经验，**不会自动上传仓库文件或执行 API 生成的代码**。任务包可能包含本地路径和你录入的文字，发送前可以 `task export` 检查。需要分析具体文本时，明确将必要内容写入本次指令，保持范围可控。

预算是本应用按配置价格的保守估算与预留；账户外调用、价格变化、供应商额外计费不在本地控制范围内，不能替代供应商硬限额。请求结果未知时进入 UNKNOWN，保留费用预留并阻止同项目新派发；先核对供应商请求记录和账单，再人工确认状态。`job resolve` 不会自动释放费用预留。响应被截断时保存已有文本并报告未完成。

## 6. 交接、验收与备份

```powershell
.\ccaw.ps1 task export <TASK_ID> --out handoff.md
.\ccaw.ps1 task done <TASK_ID> --evidence '回归通过，结果位于项目 reports 目录'
.\ccaw.ps1 backup 'E:\Backups\ccaw-20261026.sqlite3'
```

导出包用于基础 ChatGPT 对话、人工交接或后续智能体。摘要引用真实作业与证据；不保存模型内部推理。完整迁移需要同时保留 `.ccaw/config.json`、`config-history/`、`runs/` 及数据库的一致性备份；运行中的 SQLite 文件不要直接作为完整备份复制。

## 权限与当前边界

本地脚本按你的操作系统账户权限执行；仅运行你已授权、可信的命令。Codex 使用 workspace-write 沙箱，并继承其原有审批设置；需要人工批准的动作可能无法在非交互模式完成。工具不添加任何跳过审批的开关。

当前没有图形界面、开机自启守护服务、远程计算集群、自动根因判断、无人值守全自动模型升级或跨机器容灾。Windows / Python 3.14.3 和 Windows PowerShell 5.1 的验证结果、真实服务核验范围及未完成事项见 [当前进度](docs/STATUS.md)。GitHub CI 已配置 Windows/Linux 测试，本轮未核验远端运行结果。

常见问题：

| 现象 | 处理方法 |
| --- | --- |
| 提示尚未初始化 | 核对 `--home`，在同一账本运行 `init`；不要删除已有状态目录。 |
| 更换目录后任务列表为空 | 不同目录可能使用不同账本，明确指定原有 `--home`。 |
| 配置导入失败 | 使用完整 JSON 配置；支持 UTF-8（含 BOM），错误会拒绝发布，保留原版本。 |
| 作业失败但不知道原因 | `job show <JOB_ID>` 查看 `diagnostics`，再读取对应日志。 |
| 作业为 UNKNOWN | 核对进程与成果后再 `job resolve`，不要用新键绕过核对。 |
| 等待命令返回退出码 2 | 查看 JSON 中的状态；可能是失败、额度等待、UNKNOWN 或等待超时，原计算未因此被取消。 |
| 自动等待没有继续 | 查看 `diagnostics.auto_resume.reason` 和 `log_path`；确认额度接口提供明确许可、账号未变化且没有 UNKNOWN 作业。仅到达重置时间不代表额度已恢复。 |
| 后台等待程序已退出 | `job watch <JOB_ID>` 重启已授权等待；不会重启实际状态不明的计算。 |

设计依据见 [产品设计](docs/product-design.md)、[当前进度](docs/STATUS.md)、[纠错与恢复约定](docs/OPERATIONS.md)。

## GitHub 仓库

仓库预定名称为 `ccaw-workflow`，默认私有。当前会话若尚不能创建远端，可以在本机安装 Git 和 GitHub CLI、完成 `gh auth login` 后执行：

```powershell
.\scripts\Publish-GitHub.ps1
```

脚本先核对登录账号和 Git 作者，只提交列出的产品源码目录，然后创建私有仓库并推送。不会替换已存在的 origin；远端操作部分失败时应先检查实际仓库状态。仓库是否已创建，以 GitHub 返回的实际链接为准。

执行与文本 API 文档核对日期：2026-10-26；额度读取协议和本机 schema 核对日期：2026-10-27。参考 [Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[Codex app-server](https://learn.chatgpt.com/docs/app-server) 和 [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。

## 离线构建交付包

开发环境已安装 pip 和 setuptools 时，在源码目录执行 `python scripts/build_release.py`。脚本仅使用本机构建工具，不下载依赖，生成当前版本的源码 ZIP、wheel 和 SHA-256 清单到 `dist/`。源码包只包含明确列出的产品文件，排除任务账本、虚拟环境、凭据和临时验证产物。构建工具缺失时会停止，不自动联网安装。

从 v0.1 升级时，数据库和配置格式保持不变。先备份，确认没有运行中或 UNKNOWN 作业，再替换产品源码；保留原 `.ccaw` 和虚拟环境，不要用新账本覆盖原账本。安装器仍拒绝向非空的不同目标目录复制文件；源码已经放到原安装目录时可原地运行 `Install.ps1`，重复初始化会保留任务和配置身份。本次工作目录中的源码已更新，无需解压旧 v0.1 压缩包覆盖。
