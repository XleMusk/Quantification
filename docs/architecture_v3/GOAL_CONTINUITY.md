# 主目标连续性与中断恢复

这份记录解决的是运行连续性，不改变量化研究、纸面交易或执行准入规则。
`Selected model is at capacity. Please try a different model.` 属于
`RETRYABLE_INFRA`：它表示本次模型/子任务没有获得服务容量，不表示主目标完成、
数据失败、策略失败或项目被取消。

## 唯一连续性来源

主线程仍是目标所有者。目标本身由 Codex 的 `/goal` 保存；跨会话的工程状态由
`D:\enforce\capability_lab\handoffs\current_handoff.md` 保存；仓库内的业务进度
由以下文件保存：

- `docs/architecture_v3/work_packages_v3_binding.json`
- `docs/architecture_v3/f2_work_packages_binding.json`
- `docs/architecture_v3/TRACEABILITY.md`
- `docs/architecture_v3/F2_TRACEABILITY.md`
- `docs/architecture_v3/github_plan_20261022_binding.json`
- `docs/architecture_v3/CURRENT_STATUS.md`

子任务只负责自己的代码、测试和证据。子任务容量错误、线程终止或模型切换不能
改变主目标状态，也不能把未完成的工作包写成完成。

## 中断分类

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `RETRYABLE_INFRA` | 模型容量、短暂 MCP/服务错误 | 保留目标和工作区，记录错误；检查容量后切换可用模型或由主线程接管 |
| `RETRYABLE_TOOL` | 命令超时、工具连接暂时失败 | 先检查进程句柄和输出；不要因观察超时而重复启动 |
| `BLOCKED_DATA` | 数据缺失、PIT 未验证、外部渠道缺证据 | 只暂停依赖该输入的路线，继续独立工程和纸面工作 |
| `BUSINESS_FAILURE` | 测试、回放或不变量真实失败 | 保留失败产物，修根因后再运行；不得包装成成功 |

## 发生容量错误时的恢复顺序

1. 主线程继续保有目标，不创建同一工作包的重复子任务。
2. 使用 `/status` 查看活动模型、上下文和容量；使用 `/usage` 查看账户使用窗口。
3. 使用 `/model` 选择当前可用模型。优先使用界面实际列出的可用模型；需要较
   轻任务时可选择 `gpt-6-luna`，普通工程任务可选择 `gpt-6.1-sol`。
4. 让主线程先完成不依赖模型容量的本地检查、文件编辑和测试；容量恢复后再
   继续原工作包。容量错误最多重试一次同一子任务，随后改为主线程接管或换模型。
5. 长会话接近上下文上限时先运行 `/compact`，然后更新 handoff；新会话使用
   `/resume` 或读取 handoff 的 Resume Prompt。不要从聊天标题或最后一条状态猜测
   当前进度。

## 每个检查点必须保存

当前目标、最新用户请求、已完成证据、当前状态、下一步、关键文件、完整验证命令、
安全约束、阻塞及其类别。保存入口是：

```powershell
.\tools\save_goal_handoff.ps1
```

它调用全局 `D:\enforce\capability_lab\scripts\save_handoff.ps1`，不提交代码、
不重置工作树，也不访问券商凭证。

## 本项目的硬边界

即使模型恢复或切换成功，`execution_admitted=false` 仍然有效。真实登录、下单、
撤单、转账和付费数据购买不因恢复流程获得授权；`PARTIAL`、`UNKNOWN`、
`BLOCKED_DATA` 和 `NO_OPPORTUNITY` 必须保持原状态。
