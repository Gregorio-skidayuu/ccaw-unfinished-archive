# 本产品开发中已经纠正的问题

这些是可核对的开发记录，不是自动注入用户所有项目的全局规则。

## Linux 容器的进程编号不一致

- 触发：容器中 `os.getpid()` 与挂载的 `/proc/self/stat` 对外可见 PID 不同。
- 结果：直接读取 `/proc/<本地PID>/stat` 可能失败，存活作业被误标 UNKNOWN；取消也可能无法核对目标身份。
- 原因：将进程命名空间中的 PID 与 procfs 挂载所使用的 PID 命名空间视为相同。
- 纠正：优先用 pidfd 的 fdinfo 定位指定进程的可见 PID，再核对启动时间和本次启动标识；不遍历其他进程。
- 验证：`test_linux_pidfd_resolves_namespace_pid_without_process_scan` 模拟两套 PID；其余独立运行、取消和去重测试覆盖实际运行路径。
- 范围：Linux 运行器；Windows 使用进程句柄和创建时间。

## 完成判定必须核对协议结果

- 触发：子进程返回 0，但 Codex 未发出完成事件；或文本 API 输出因长度限制被截断。
- 纠正：Codex 同时核对退出状态、完成事件和错误；API 保存已生成文本并标为未完成。
- 验证：`test_codex_exit_zero_without_completion_is_failure` 与 `test_truncated_api_output_is_retained_but_not_completed`。
- 范围：相应接入适配器；不将正常进程退出直接等同于用户目标验收通过。

## 凭据与计费方式需要在派发前核对

- 触发风险：继承的 API Key 可使本来预计使用订阅的 Codex 命令改用按量计费。
- 纠正：Codex 适配器只接受确认的 ChatGPT 登录，从子进程环境移除 API Key 覆盖；可选 API 使用单独配置和显式授权。
- 验证：`test_subscription_auth_does_not_silently_use_api_key`、`test_paid_api_disabled_and_reservations_enforce_shared_budget`、`test_api_revocation_before_dispatch_prevents_request`。
- 范围：CCAW 的 Codex 与 API 适配器；不代表其他程序的账单受本工具控制。

## 内部异常不能证明实际执行已结束

- 触发：worker 或子进程创建后，启动身份回执核对发生 `OSError`；原实现可能直接记为 FAILED，放行同项目新作业。
- 纠正：区分创建前失败与创建后状态不明；后者保留 UNKNOWN，阻止新派发，已提交的完成状态不被覆盖。Codex 的非对象 JSONL 行直接跳过，仍按真实完成事件验收。
- 并发边界：健康检查拿到旧状态后，worker 可能已经完成；更新 UNKNOWN 使用带原进程身份与活动状态条件的事务写入，只在实际更新时记录事件和等待状态。
- 验证：`test_post_launch_identity_error_blocks_new_work`、`test_post_launch_identity_error_preserves_completed_worker`、`test_worker_error_with_live_child_preserves_unknown_and_blocks_new_work`、`test_reconcile_does_not_overwrite_a_concurrent_completion`、`test_codex_ignores_non_object_jsonl`。活进程用例创建真实本地进程，并在结束后清理。
- 范围：本地运行器的启动回执、执行异常与状态核对；不证明机器重启后可自动恢复。

## Windows 参数和资源释放需要实际验证

- 触发：Windows PowerShell 5.1 原生参数转发丢失嵌入引号和空参数；调用 `.ps1` 时未引用的 `--` 也会被 PowerShell 吞掉。
- 纠正：包装脚本按 Windows argv 规则传参，并用 UTF-8 并发读取两条输出流；stdout 进入 PowerShell 管道。示例将分隔符写为 `'--'`。
- 触发：SQLite 连接上下文只提交/回滚，不负责关闭；worker 提交终态后也可能仍持有日志文件。
- 纠正：备份连接显式关闭；关闭 Codex 管道；测试确认 worker 退出后再删除临时目录，不以忽略清理错误掩盖泄漏。
- 验证：`test_windows_cli.py` 的安装、argv/JSON 管道和错误退出码端到端用例；`test_database_backup_and_future_version_guard`、`test_cancel_running_program`。定向回归启用 `ResourceWarning` 严格检查。
- 范围：Windows 安装和 PowerShell 入口、本地文件与管道生命周期；真实 Codex CLI 后台管道仍需单独验收。

## API 响应未知需要独立于明确失败

- 触发：请求发出后网络断开、响应未完整读入或结构不兼容；仅保留预算、却把作业记为 FAILED，仍会允许新键再次发送。
- 纠正：用 `APIExecutionUncertain` 区分未知结果与明确 HTTP 错误，进入 UNKNOWN 并保留 UNCERTAIN 费用预留，不自动重试。人工确认作业状态不自动退还预留。
- 验证：`test_providers.py` 的本地响应替身验证请求只发送一次且不泄漏密钥/响应体；`test_api_uncertain_execution_blocks_new_work_and_retains_reservation` 验证持久化状态、阻止新派发和费用预留。
- 范围：当前文本 API 接入；没有实现供应商账单对账或费用调整。

## 自动继续需要分开验证许可、队列和实际执行

- 触发风险：到达预估重置时间不代表账户允许继续；队列持久化后，角色、账号或窗口也可能改变。原作业的原始执行指令不能因接续而丢失或不断嵌套。
- 纠正：初始执行绑定账号，后台使用明确额度信号；授权检查与入队同事务，派发再核对当前账号/角色/周期。撤销只取消尚未启动的自动队列，状态不明的实际作业仍须人工核对；恢复固定引用初始指令。
- 验证：`test_capacity_api.py` 覆盖非模型协议与许可缺失；`test_capacity_dispatch.py` 覆盖队列跨周期、账号和角色变化；`test_capacity.py` 覆盖持久化等待、同周期一次、原指令及独立进程锁与生命周期；`test_auto_resume_flow.py` 覆盖旧键兼容、撤销和任务验收并发保护。v0.2 wheel 的独立本地假协议验收进一步覆盖真实跨进程限额、等待、原 session 接续和恰好两次执行记录。
- 范围：每个作业显式授权的 `codex` 额度桶自然恢复；本机真实额度读取已核验，但真实模型自然耗尽与刷新后的完整接续尚未验收，不将离线替身结果扩大为真实服务保证。
