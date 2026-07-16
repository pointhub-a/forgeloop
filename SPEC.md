# ForgeLoop Coding Agent Harness — 产品与系统规约

版本：0.1.0
日期：2026-07-17
状态：设计已批准；冷启动验证作为实现前独立门禁执行

## 1. 问题陈述

大语言模型能够提出代码修改，但一次生成无法保证修改正确，也无法安全地直接获得完整 shell 权限。现有高层 Agent 框架隐藏了主循环、治理和反馈细节，不适合用于研究这些机制如何保证 Coding Agent 可靠运行。

ForgeLoop 面向需要在本地代码仓库中完成小型修复任务的开发者和 AI4SE 学习者。它提供一个自行实现、可观察、可离线测试的 Coding Agent Harness：模型提出结构化动作，Harness 在受限工作区内执行动作，运行确定性校验器，将失败分类并回灌给模型，直到验证成功、没有进展、预算耗尽或需要人工审批。

一句话价值主张：**让 LLM 的代码修改进入一个可测试、可审计、有安全边界的自动修复闭环。**

## 2. 目标与非目标

### 2.1 目标

- 自行实现 Agent 主循环，不依赖现成 Agent Runner。
- 支持文件读取、文本替换、写文件、受限 shell 和测试执行。
- 以反馈闭环为主要贡献：校验、失败分类、重复动作/无进展检测和多轮修正。
- 以治理为次重点：路径围栏、危险命令规则和 HITL 审批状态机。
- 使用 Mock LLM 离线、确定性验证所有核心机制。
- 提供 WebUI 展示任务、事件轨迹、验证反馈及审批操作。
- 支持安全录入、更新和清除真实 LLM API Key。
- 提供 Docker 分发及 GitLab CI。

### 2.2 非目标

- 不做 IDE 插件、代码补全或大型代码库语义索引。
- 不做多 Agent 协作和并行写文件。
- 不支持任意网络工具、容器编排或远程部署执行。
- 不尝试替代成熟的通用 Coding Agent。
- 首个版本不提供向量数据库；记忆使用确定性标签检索。

## 3. 用户故事

1. 作为开发者，我希望提交一个修复任务并指定工作区，以便 Agent 只在该目录中行动。
2. 作为开发者，我希望看到每一步模型决策、工具结果和验证结果，以便理解 Agent 为什么成功或失败。
3. 作为开发者，我希望测试失败被分类并回灌给模型，以便 Agent 能基于客观证据继续修复。
4. 作为安全负责人，我希望危险命令在执行前暂停并等待审批，以便避免不可逆损害。
5. 作为安全负责人，我希望越界文件访问被直接拒绝且不可通过审批绕过，以便保护工作区外文件。
6. 作为使用者，我希望配置最大步数、允许命令和验证器，以便控制成本及权限。
7. 作为使用者，我希望安全保存、更新和清除 API Key，以便凭据不进入代码、日志或项目配置。
8. 作为维护者，我希望用 Scripted Mock LLM 重放完整轨迹，以便无需网络即可验证 Harness 行为。
9. 作为学习者，我希望运行一个固定演示，以便观察危险动作拦截、失败回灌和自我修正。

这些故事彼此可独立验收，均有明确价值，范围足以在单个迭代中测试。

## 4. 领域与机制设计

### 4.1 动作与工具

LLM 只能返回一个 JSON 动作，不得输出可直接解释执行的自然语言：

- `read_file(path)`：读取 UTF-8 文本，限制大小。
- `write_file(path, content)`：写入 UTF-8 文本；自动创建工作区内父目录。
- `replace_text(path, old, new, count)`：进行可验证的精确替换。
- `run_command(argv, timeout_seconds)`：以参数数组执行允许的命令，不经 shell 展开。
- `run_validation()`：执行配置中的全部验证器。
- `remember(key, value, tags)`：写入项目记忆。
- `recall(tags, limit)`：按标签交集和更新时间检索。
- `finish(summary)`：请求结束；只有最近一次验证成功才可转为成功状态。

动作经严格 schema 验证。未知动作、未知字段、错误类型和超限参数都返回结构化错误并计入步骤预算。

### 4.2 客观反馈信号——主要贡献

验证器由代码执行并生成统一 `ValidationReport`：

- 命令、退出码、耗时、标准输出/错误的截断结果。
- 状态：`passed`、`failed`、`timed_out`、`infra_error`。
- 失败分类：`syntax`、`test_failure`、`lint`、`type_error`、`timeout`、`infrastructure`、`unknown`。
- 指纹：分类、退出码和规范化尾部输出的哈希。

反馈引擎维护验证历史，并实现：

- 每次修改后提示模型运行验证。
- 将报告作为独立、不可伪造的 `feedback` 消息回灌。
- 同一验证失败指纹连续出现达到阈值时判定无进展。
- 同一动作指纹重复达到阈值时拒绝继续盲目执行。
- 验证成功后才允许 `finish` 进入 `succeeded`。
- 达到最大步骤、最大验证次数或墙钟预算时确定性停止。

### 4.3 危险动作与治理

治理决策分为：

- `allow`：安全动作直接执行。
- `deny`：越界路径、shell 元字符、未知可执行程序等不可审批风险。
- `require_approval`：潜在破坏性但用户可能确实需要的动作。

最低危险规则包括：递归删除、Git 强制推送/硬重置、数据库删除、提权、修改权限、网络下载后执行。命令以 `argv` 表示，执行时使用 `shell=False`。包含 `;`、`&&`、管道、重定向或命令替换的参数默认拒绝。

路径在解析符号链接后必须仍位于工作区；该边界不可通过 HITL 放宽。审批记录绑定任务 ID、动作指纹和一次性审批令牌，审批其他动作时不能复用。

### 4.4 记忆

跨会话记忆保存项目约定和历史决策，不保存完整提示或 API Key。SQLite 实体包含 `project_id`、`key`、`value`、`tags`、`created_at`、`updated_at`。检索只返回匹配标签且未超过数量/字符预算的记录，避免全量载入上下文。

### 4.5 为什么选择反馈闭环作为重点

Coding 领域拥有测试、lint 和类型检查等客观信号，能在移除真实 LLM 后完全验证。相较仅增加工具数量，失败分类、指纹、无进展判断和验证门禁更直接地回答“Harness 如何让不确定模型可靠工作”。治理仍提供完整最低实现和 HITL 演示。

## 5. 功能规约

### 5.1 Agent Core

输入：任务描述、工作区、Harness 配置、Provider。
行为：创建任务和初始上下文；逐步调用 Provider；解析单一动作；经过治理后分发；将结果或反馈追加到上下文；检查停止条件。
输出：最终任务状态、摘要及完整事件轨迹。
边界：一次只执行一个动作；默认最多 20 步；Provider 异常转为结构化错误。
错误处理：非法响应可以重试，连续达到配置阈值后以 `failed` 停止。

状态机：

```text
created -> running -> waiting_approval -> running
                   -> denied -> running
running -> succeeded | failed | budget_exhausted | no_progress | cancelled
```

### 5.2 LLM Provider

统一接口 `complete(messages, tools_schema) -> str`。

- `ScriptedProvider` 按预设响应依次返回，耗尽时明确报错。
- `OpenAICompatibleProvider` 使用单次 Chat Completions 风格 HTTP API，不含 Agent Runner。
- Provider 不获得工具对象，不能绕过 Harness 执行动作。

### 5.3 Tool Runtime

输入：已通过治理的结构化动作。
输出：`ToolResult(ok, output, error, metadata)`。
边界：输出、文件大小、运行时间均有限制；子进程工作目录固定为工作区；环境变量使用精简白名单。
错误处理：文件不存在、替换目标不唯一、超时和非零退出码均转为结构化结果，而非抛出到主循环。

### 5.4 Feedback Engine

输入：一个或多个验证器结果。
输出：统一报告、失败分类、失败指纹和进展判断。
边界：分类只依赖退出码、命令和输出模式，不调用 LLM。
错误处理：验证器未安装归为 `infrastructure`，不冒充代码失败。

### 5.5 Policy Engine 与 HITL

输入：动作、工作区和策略配置。
输出：治理决策、匹配规则和可读原因。
边界：路径越界恒为 `deny`；只对动作指纹完全匹配的审批生效。
错误处理：规则解析失败时 fail closed；审批超时保持等待状态。

### 5.6 Memory Store

输入：项目标识、键值、标签或检索条件。
输出：受预算限制的记忆列表。
边界：每项目隔离；键在项目内唯一；不接受疑似凭据字段。
错误处理：数据库错误不会使 Agent 获得额外权限，并形成可观察事件。

### 5.7 配置

使用 TOML。`HarnessConfig` 的字段、类型和默认值固定如下：

| 字段 | 类型 | 默认值 |
|---|---|---|
| `max_steps` | 正整数 | `20` |
| `max_validation_runs` | 正整数 | `8` |
| `wall_time_seconds` | 正整数 | `900` |
| `command_timeout_seconds` | 1–120 的整数 | `60` |
| `provider_timeout_seconds` | 1–120 的整数 | `60` |
| `max_output_bytes` | 正整数 | `32768` |
| `max_file_bytes` | 正整数 | `1048576` |
| `max_identical_failures` | 大于 1 的整数 | `2` |
| `max_identical_actions` | 大于 1 的整数 | `3` |
| `memory_recall_limit` | 正整数 | `10` |
| `memory_char_budget` | 正整数 | `4096` |
| `allowed_executables` | 非空字符串列表 | `python3, pytest, ruff, mypy, git` |
| `approval_rule_ids` | 字符串列表 | `command.recursive_delete, git.force_push, git.hard_reset, database.drop, privilege.escalation, permission.change, network.execute` |
| `validators` | `ValidatorConfig` 列表 | 空列表 |
| `provider_base_url` | HTTPS URL | `https://api.openai.com/v1/chat/completions` |
| `provider_model` | 非空字符串 | `gpt-5.6-luna` |

`ValidatorConfig` 只包含 `argv: list[str]` 和 `timeout_seconds: 1–120 的整数（默认 60）`。工作区是每个 Task 的必填输入，而不是全局配置，避免一个服务只能操作单一仓库。API Key 不得出现在 TOML 中。

启动时完成 schema 校验；未知字段和非法值导致清晰错误。

### 5.8 WebUI 与 API

页面包括：

- 首页：系统说明、新建任务表单和安全边界。
- 任务页：状态、步骤、工具结果、验证报告和自动刷新事件轨迹。
- 审批区：显示动作及风险原因，允许批准一次或拒绝。
- 设置页：Provider、凭据状态、录入/更新/清除，不回显 Key。
- 演示页：启动固定 Mock 场景并展示确定性结果。

API 最低集合：创建任务、读取任务、推进任务、批准/拒绝动作、取消任务、凭据状态/写入/清除、运行演示。修改接口使用 CSRF token；监听默认仅为 `127.0.0.1`。

## 6. 非功能性需求

### 6.1 性能

- 不含 LLM 和外部测试耗时的单步 Harness 开销目标低于 100 ms。
- WebUI 查询 1000 个事件以内的任务时目标低于 200 ms。
- 输出默认截断为 32 KiB，单文件默认限制 1 MiB。

### 6.2 安全与凭据威胁模型

威胁包括：凭据误提交、日志泄漏、网页回显、模型诱导读取环境变量、命令注入、路径穿越、符号链接逃逸和审批重放。

对策：

- API Key 通过隐藏输入录入操作系统 Keychain；开发环境可选 `.env`，但默认不创建且明确其明文风险。
- Key 不写入数据库、配置、事件、异常或 Provider 请求日志。
- 日志过滤已知 Key 及常见令牌模式。
- Provider 只接收必要上下文；工具子进程使用环境白名单且不含 API Key。
- 命令数组执行、工作区路径围栏和 fail-closed 策略。
- 一次性、动作绑定审批。
- `.gitignore` 排除 `.env`、数据库、运行轨迹和临时工作区。

### 6.3 可用性

- `make dev` 本地启动，`make test` 一键测试；裸机命令统一使用 `python3`，不假设存在 `python` 别名。
- 首次启动向导说明工作区、Provider 和安全录入方式。
- 错误信息包含问题、影响和下一步操作。

### 6.4 可观测性

每个事件包含时间、任务 ID、步骤、类型、摘要和经过脱敏的结构化数据。事件类型包括模型请求、动作、治理决策、工具结果、验证、状态变化和人工审批。

## 7. 系统架构

```text
Browser
  | HTTP
Web/API Layer ---- Credential Service ---- OS Keychain
  |
Task Service ---- SQLite task/event store
  |
Agent Loop ---- Provider Adapter ---- LLM HTTP API
  |     |            (or Scripted Mock)
  |     +---- Memory Store
  |     +---- Policy Engine ---- Approval Store
  |     +---- Tool Runtime ---- Workspace/Subprocess
  +---------- Feedback Engine ---- Validators
```

依赖方向始终指向协议：Agent Core 依赖 Provider、Tool、Policy、Memory 和 Event 接口；具体 SQLite、Keychain、HTTP 和 Web 实现位于外层。

## 8. 数据模型

- `Task`：id、description、workspace、status、step_count、last_validation_passed、pending_action、created_at、updated_at。
- `Event`：id、task_id、sequence、kind、summary、data_json、created_at。
- `Approval`：id、task_id、action_fingerprint、decision、used_at、created_at。
- `Memory`：project_id、key、value、tags_json、created_at、updated_at。
- `ValidationReport`：status、classification、exit_code、duration_ms、stdout、stderr、fingerprint。

约束：事件序号在任务内唯一；审批只能消费一次；任务路径保存规范化绝对路径；敏感字段进入持久层前统一拒绝或脱敏。

## 9. 技术选型

- Python 3.12：标准库子进程、路径和 SQLite 能力成熟，适合快速构建可测试内核。
- FastAPI + Uvicorn：类型化 API、轻量 Web 服务；不承担 Agent 编排。
- Pydantic：仅用于输入/配置 schema 校验。
- SQLite：无需外部服务，适合任务、事件和记忆持久化。
- Jinja2 + 少量原生 JavaScript/CSS：避免独立前端构建链，满足 WebUI 交付。
- pytest：参数化和临时目录适合确定性机制测试。
- `keyring`：跨平台调用操作系统凭据存储；不可用时明确报错，不静默降级为明文。
- Docker：提供一致分发；容器环境无法使用宿主 Keychain 时，以运行时 secret 文件作为安全来源，不写入镜像。
- OpenAI-compatible HTTP adapter：减少供应商锁定；默认离线 Mock 可运行。真实适配器默认模型为 2026-07-17 官方模型指南中的成本敏感选项 `gpt-5.6-luna`，仍可通过 TOML 覆盖。

不使用任何高层 Agent 框架。

## 10. 凭据与分发设计

本机：设置页或 `forgeloop credentials set` 使用隐藏输入，将 Key 写入系统 Keychain；支持 status、update、clear。状态只显示“已配置”和来源。

容器：使用只读 Docker secret 文件挂载至 `/run/secrets/forgeloop_api_key`；环境变量仅作为显式兼容模式并在 README 标注进程环境可见风险。

分发：

- `docker build -t forgeloop .`
- 挂载待修复工作区、数据目录和 secret 后 `docker run`
- 另提供 `pip install .` 和 `forgeloop serve`

目标平台为 Linux amd64/arm64 容器，以及 Python 3.12 可运行的平台。

## 11. 验收标准

1. `make test` 在无网络、无 API Key 环境通过。
2. Mock Provider 能驱动完整主循环并产生可预测事件序列。
3. `rm -rf` 等危险动作进入 `waiting_approval`，未批准前不执行。
4. 工作区外文件和符号链接逃逸被直接拒绝。
5. 一次注入测试失败后，反馈进入下一次模型消息，下一动作与前一步不同并最终验证成功。
6. 相同动作或失败指纹达到阈值时以 `no_progress` 停止。
7. 未成功验证时，`finish` 不能将任务标为成功。
8. 记忆跨实例持久化，按项目与标签隔离，并遵守字符预算。
9. WebUI 可创建 Mock 任务、显示事件并批准/拒绝待审批动作。
10. 凭据可录入、更新、清除，页面、日志、数据库和测试快照中无明文。
11. `docker build` 成功，README 的冷启动命令可复现。
12. `.gitlab-ci.yml` 包含名为 `unit-test` 的 job，最终流水线通过。

## 12. 测试策略

- 单元测试：动作 schema、路径围栏、命令策略、工具、失败分类、指纹、预算、记忆、脱敏、凭据接口。
- 主循环契约测试：Scripted Provider + fake tools/policy/validators，覆盖所有停机状态。
- 集成测试：临时 Git 风格项目中执行文本修复和 pytest 验证。
- Web 测试：API 创建、推进、审批、凭据不回显及演示端点。
- 机制演示：固定脚本依次展示危险动作拦截、注入失败、反馈改变下一动作、最终成功和无进展停止。
- 所有实现遵循 red-green-refactor，关键红/绿证据记录到 AGENT_LOG。

## 13. 风险与已决问题

- LLM 输出可能不符合 JSON：严格解析并有限重试。
- 命令规则不可能证明任意程序安全：只允许少量可执行程序，危险动作审批，容器进一步隔离。
- Docker 中 Keychain 不可用：采用只读 secret 文件，不将 Key 放入镜像或配置。
- WebUI 是课程强制交付而非核心贡献：保持轻量，不引入 SPA 构建链。
- 通用要求同时提及 GitHub Actions 与 `.gitlab-ci.yml`：以最终清单为准实现 GitLab CI；可选附加 GitHub Actions。
- 文档将 PR 用于泛指评审请求：NJU GitLab 上使用 Merge Request。
- 当前目录初始没有 Git 仓库：设计批准后初始化仓库，并从规约提交开始保留历史。

## 14. 完成定义

只有当全部验收标准通过、文档齐全、无真实凭据、机制演示可重复、Docker 构建成功、WebUI 可访问且最终 CI 为通过状态时，项目才算可交付。线上 URL 和远程 CI/MR 需要仓库与部署平台权限；本地版本将提供完整配置，等待所有者推送部署。
