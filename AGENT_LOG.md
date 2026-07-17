# Agent Log

所有时间使用 Asia/Shanghai。

## 2026-07-17 — DESIGN-001

- 技能：Superpowers `using-superpowers`、`brainstorming`。
- 上下文：课程通用要求与 Coding Agent Harness 专项要求。
- 关键决策：反馈闭环为主要贡献，治理为次重点；Python/FastAPI/SQLite；Docker + GitLab CI；轻量 WebUI。
- 人工授权：使用者在方案展示后要求助手持续完成可交付版本。
- 偏离说明：未对书面 SPEC 逐节等待使用者再次确认，因为使用者已明确授权夜间自主推进；以主智能体自检、陌生 Agent 冷启动和最终人工复核补偿。
- 教训：最终交付清单比章节中的一般描述更具体；CI 应以 `.gitlab-ci.yml` 为准。

## 2026-07-17 — PLAN-001

- 技能：Superpowers `writing-plans`。
- 输入：已批准且提交的 `SPEC.md`（commit `6e268b6`）。
- 输出：10 个可独立评审任务，明确文件、接口、失败测试、验证命令、依赖和提交边界。
- 关键约束：Task 1 后 Task 2–5 可并行；Task 6 集成主循环；Task 8 才增加 Web；Task 9 固化课程机制演示。
- 人工变更：使用者将课程要求原文移动到 `requests/`，项目根目录只保留交付物。

## 2026-07-17 — COLDSTART-001

- 工具：全新 Codex 子 Agent，隔离 worktree `coldstart/spec-validation`。
- 输入限制：只允许 SPEC、PLAN；不得读取主对话与过程文档。
- 结果：首次 Agent 在读取设计前被 Superpowers brainstorming 门禁阻塞，未产生代码或测试。
- 暴露缺陷：PLAN 未显式告诉陌生执行者“设计已经批准，不要重新 brainstorming”。已补入全局约束并用第二个新 Agent 重试。
- 限制：机器只有 Codex 客户端，无法完成“不同 Agent 类型”要求；最终提交前需在另一种 Agent 上补做并替换/追加证据。

## 2026-07-17 — COLDSTART-002

- 输入：第二个无对话历史 Codex 子 Agent，仅有 SPEC、PLAN 和 TDD 技能。
- RED：`python -m pytest tests/test_models.py -q` → exit 127，系统无 `python` 命令，因此不是预期红色。
- 停止点：HarnessConfig 缺少精确字段、类型和默认值，执行者拒绝猜测。
- 人工修订：所有裸机命令改用 `python3`；SPEC §5.7 与 PLAN Task 1 补全完整配置 schema。
- 结果：冷启动成功发现两项可复现规约缺陷；验证分支不合并。

## 2026-07-17 — TASK-001

- 技能：Superpowers `subagent-driven-development`、`test-driven-development`。
- 实现者：`/root/task1_domain_config`；commit `5c07791`。
- RED：models 与 config 分别以 `ModuleNotFoundError` 正确失败。
- GREEN：30 tests passed；warnings-as-errors、pip check、compileall、diff check 通过。
- 环境干预：系统 Python 为 3.9；主控定位 Codex 内置 Python 3.12.13 并要求使用 worktree `.venv`，未降低产品版本要求。
- 双重评审：Spec compliant；Task quality Approved；无 Critical/Important/Minor。
- 主控复核：枚举成员与 SPEC 状态机一致；Provider URL 与 SPEC §5.7 一致。

## 2026-07-17 — TASK-002

- 实现者：`/root/task2_policy`；首个 commit `15106fb`，三轮安全修复至 `fab93b8`。
- RED/GREEN：路径围栏、危险命令、元字符、指纹和 Git 参数共 22 个策略测试；全套 52 passed。
- 规约澄清：危险签名必须先于普通 allowlist，否则 `rm -rf` 无法进入 HITL。
- 评审修复：补齐 `&`/括号/换行；解析 Git global options；最终对 `git -c` 与 `--config-env` 动态配置全部 fail-closed deny，阻断别名绕过。
- 最终双重评审：Spec compliant；Task quality Approved；无遗留问题。

## 2026-07-17 — TASK-003

- 实现者：`/root/task3_tools`；commits `5554519`、`95d287e`。
- 接口裁决：`error_code`、`exit_code`、`duration_ms` 进入 `ToolResult.metadata`，不破坏 Task 1 模型。
- 首轮：24 focused / 76 full tests passed。
- 评审修复：用 Popen reader threads 保证执行期内存有界；POSIX 终止整个进程组；原子覆盖保留已有 mode。5 个回归测试先红后绿。
- 最终：29 focused / 81 full tests passed；Spec compliant；Task quality Approved。

## 2026-07-17 — TASK-004

- 实现者：`/root/task4_feedback`；commit `486e2e0`。
- 接口裁决：ValidatorRunner 复用 bounded ToolRuntime；`all_validations_passed` 为纯函数，空验证集 false。
- RED/GREEN：分类、指纹、进展、真实子进程验证器共 14 focused；全套 95 passed。
- 双重评审：Spec compliant；Task quality Approved。
- Minor 账本：缺少 bounded-tail 和不同验证 fingerprint 重置的直接回归测试；实现已正确，交最终评审决定是否补齐。

## 2026-07-17 — TASK-005

- 实现者：`/root/task5_memory_credentials`；commits `aeb374e`、`8b047e7`。
- RED/GREEN：真实 SQLite 记忆、fake credential facade、真实 secret-file mode，共 23 focused / 118 full 初始通过。
- 外部评审：发现 memory value 可携 token-shaped 明文落库（Critical）及空凭据误报（Important）。
- 修复：共享 token predicate，写 SQL 前拒绝敏感 value；直接查询 DB 证明零落盘；空白 secret 在 backend 前拒绝并统一旧值语义。
- 最终：28 focused / 123 full；Spec PASS；Task Quality PASS—READY。

## 2026-07-17 — DOCS-API-001

- 技能：`openai-docs`；官方 MCP 未暴露，按技能规则回退至 OpenAI 官方开发者文档。
- 发现：2026-07-17 模型指南推荐 GPT-5.6 系列；GPT-4.1 mini 已列为 deprecated。
- 决策：真实 Provider 的默认配置改为成本敏感 `gpt-5.6-luna`；仍允许 TOML 覆盖；离线 Mock 不受影响。
- 来源：https://developers.openai.com/api/docs/models 与 https://developers.openai.com/api/docs/models/all

## 2026-07-17 — TASK-006

- 实现者：`/root/task6_agent_loop`；commits `5dd624b`、`ff329f2`。
- 初始验证：Provider/Loop 32 focused，155 full passed；完全离线 ScriptedProvider。
- 外部评审：发现 approval 可变引用绕过、stale validation（2 Critical），已消费指纹死锁、审批恢复未结算预算、memory异常逃逸（3 Important）。
- 修复：私有 canonical action snapshot + fingerprint + policy重评；所有潜在 workspace mutation 尝试前使验证失效；used指纹反馈；统一 `_finish_step`；memory operational error脱敏。
- 最终：41 focused / 164 full；Spec PASS；Task Quality PASS—Ready；无遗留 finding。

## 2026-07-17 — TASK-007

- 实现者：`/root/task7_repository_service`；commits `e253f94`、`568bb21`、`d21dcde`。
- 初始：18 focused / 182 full；审计持久化，活跃 Loop 重启后显式 `TaskNotLoaded`。
- 首轮评审修复：atomic `commit_transition`、工具前 durable approval intent、每任务 RLock、显式迁移版本、并发与失败注入。
- 二轮评审修复：reject checkpoint/restore；intent 前完整无副作用审批校验；approved final sync 幂等重试且工具只执行一次；reason 定位 approval event。
- 最终：54 focused / 193 full；Spec PASS；Task Quality PASS；Ready。

## 2026-07-17 — TASK-008

- 技能：`sites:sites-building`（仅用于现有 FastAPI WebUI 的信息架构/视觉约束，未运行会覆盖仓库的 Sites initializer）。
- 实现者：`/root/task8_webui`；commits `058961b`、`8a5dc5d`。
- 初始：20 focused / 213 full；无 warning；wheel 包含 templates/static。当前无 browser backend，完成静态 HTML/CSS 自审。
- 依赖修订：Starlette 1.3.1 警告 legacy httpx，改用可安装的 `httpx2>=2,<3`，不隐藏 warning。
- 评审修复：trusted Host 抵御 DNS rebinding；credential 全路由绑定 provider_name；状态检查进入 TaskService 锁内，Web 映射 409。
- 最终：47 focused / 223 full；Spec compliant；Task quality Approved。

## 2026-07-17 — TASK-009

- 实现者：`/root/task9_demo_cli`；commits `48b423e`、`8a758e6`。
- 机制演示：真实 Policy/Tool/Validator/Progress/Memory/Loop；危险动作仅等待审批；失败反馈改变下一动作并成功；重复指纹 no_progress。
- 初始：10 focused / 233 full；脚本 JSON 通过。
- 评审修复：demo响应跟随可配置阈值；wildcard bind强制具体 `--allowed-host`；credentials/composition/runner错误统一脱敏，运行期Provider错误仅安全回灌。
- 最终：20 focused / 243 full；Spec compliant；Task quality Approved。

## 2026-07-17 — HOSTING-001

- 技能：`sites:sites-hosting`。
- 结论：Sites 要求 Cloudflare Workers 兼容输出；ForgeLoop 需要本地 shell、挂载工作区和 SQLite，不能安全发布到该运行时。
- 决策：交付 Docker/OCI 部署；真实线上 URL 需使用者的容器平台账户，不能伪造。
