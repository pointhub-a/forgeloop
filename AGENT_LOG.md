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

## 2026-07-17 — TASK-010-REVIEW

- 外部双重评审：发现 README `read -rsp` 非跨 shell（Important）与 Docker 静态合同测试过窄（Minor）。
- 修复：Python `getpass` + `O_EXCL/O_NOFOLLOW` + `0600` 原子 secret 写入；静态测试覆盖 multi-stage、wheel、USER、VOLUME、HEALTHCHECK、remote/allowed-host。
- 最终：250 tests passed；wheel/sdist 重建；Spec PASS；Task Quality APPROVED。
- 外部限制：本机无 Docker 兼容可执行程序，未声称镜像/health smoke 已通过。

## 2026-07-17 — TASK-010

- 技能：Superpowers `test-driven-development`、`writing-plans`（执行已批准的 Task10 brief）、`openai-docs`；实现者 `/root/task10_distribution_docs`，提交作者 `Codex Task Agent`。
- 上下文：严格按 `.superpowers/sdd/task-10-brief.md`，不重新 brainstorming、不派生 Agent。使用者明确禁止代写学生 1500–2500 字反思，因此 `REFLECTION.md` 仅包含学生本人填写的问题工作表与证据索引。
- RED：新增 distribution 与 `FORGELOOP_SECRET_FILE` 默认后端测试后，focused 命令得到 6 failed；缺口分别是 example/CI/Dockerfile/README/build dev 依赖不存在，以及 CLI 仍选择 Keyring。
- GREEN：最小实现后 6 focused passed；CLI/credentials/distribution 回归 40 passed；最终 `make test` 为 249 passed，`pip check` 与 `compileall` 通过。
- 分发：`python3 -m build` 成功生成 `forgeloop-0.1.0.tar.gz` 和 `forgeloop-0.1.0-py3-none-any.whl`；wheel 检查确认 CLI entry point、Jinja templates 和静态资源存在。GitLab/Compose YAML 可解析，静态合同检查通过。
- 文档来源：当前会话未热加载 OpenAI Developer Docs MCP；按技能流程已把官方 MCP endpoint 注册到 Codex 全局配置供后续会话使用，本次则回退到 OpenAI 官方模型目录与 GPT-5.6 Luna 页面。GitLab DinD 的 `DOCKER_HOST`/TLS 配置按 GitLab 官方 Docker build 文档复核。
- Docker 限制：`docker version`、`docker compose config` 均 exit 127（`docker: command not found`）；`podman`、`buildah`、`nerdctl`、`hadolint` 也不存在。因此未声称 image build 或 `/healthz` 容器 smoke 通过，需所有者在 Docker-enabled runner/目标机完成。
- 审计：当前 token-shaped fixtures 全部统一为 `sk-unmistakably-fake-*`/`Bearer unmistakably-fake-*`，相关不泄漏断言未删除；Git 历史命中项均为命名明确的测试假值。未发现实际 `.env`、tracked 绝对开发机路径、未条件化 skipped test 或 debug breakpoint；PLAN 中 `TODO`/`TBD` 仅出现在审计指令，三个 `skipif` 均为平台能力守卫。
- 评审：因使用者明确“不派生 agent”，未派 reviewer；实现者依次完成 spec 合规自审与代码质量/安全自审，无 Critical/Important 遗留。该流程偏离按事实记录，不伪造外部 review。
- 提交：`70ff745`（`docs: complete ForgeLoop delivery and verification [agent: Codex Task Agent]`）。人工变更：本 task 内无学生手写代码；使用者只规定执行边界、作者身份与反思不得代写。
- 外部待办：最终 GitLab CI pass、registry push、线上 URL、不同类型 Agent 冷启动复核与学生本人反思仍需要仓库所有者账户/本人完成。

## 2026-07-17 — FINAL-REVIEW-FIXES

- 技能：Superpowers `test-driven-development`；实现者 `/root/final_review_fixes`。范围为整分支终审提出的 6 组问题，不改动外部部署限制。
- 策略 RED：`pytest -q tests/test_policy.py -k 'destructive_signatures or signature_words'` → `7 failed, 2 passed`；GREEN → `9 passed`。补齐数据库 DROP、提权、权限修改、联网获取并执行的 pre-allowlist 签名；每个签名均由 `approval_rule_ids` 决定 `require_approval` 或 fail-closed `deny`，并覆盖安全词边界。
- 拒绝恢复 RED：Loop/Service 两个 focused tests → `2 failed`（状态停在 `denied`）；GREEN → `2 passed`。现在先发出可审计 `denied` 状态和结构化反馈，再显式回到 `running`；持久化与 Web API 回归确认可继续自我修正。
- 验证进展 RED：多验证器 focused + bounded-tail/reset 回归 → `2 failed, 1 passed`；GREEN → `4 passed`。每轮验证只观察一次稳定有序失败聚合；仅整轮通过时清空失败历史；单验证器指纹行为保持不变。
- Action schema RED：严格参数与 discriminated schema → `11 failed, 8 passed`，非法参数步骤反馈 → `1 failed`；GREEN 分别为 `19 passed` 与 `1 passed`。8 种动作现有精确必填/允许字段、严格类型和范围，Provider 收到顶层 kind-discriminated `oneOf` schema；`recall.limit` 同时受请求值和配置上限约束。
- Action schema 补充边界：NUL path/argv focused → `2 failed, 10 passed`；加入 schema pattern 后 → `12 passed`，因此 OS 级非法字符串也在 parser 阶段被拒绝。
- SQLite 跨线程 RED：Service worker-thread memory 动作 → `1 failed`；显式 lifecycle test → `1 failed`。GREEN → `2 passed`。连接使用 `check_same_thread=False` 且所有访问由 `RLock` 串行化，并提供 `close`/context-manager 生命周期。
- Task 4 账本已关闭：新增“不同前缀位于 8192 字符尾部窗口之外时指纹相同”与 `A,A,B,A,A,A` 连续失败重置回归。
- 最终验证：`PATH="$PWD/.venv/bin:$PATH" make test`、离线机制演示、`pip check`、`compileall` 与 `git diff --check` 全部通过；Docker/GitLab/线上 URL 等外部限制保持与 Task 10 记录一致，未伪造验证结果。

## 2026-07-21 — NEWAPI-IMPLEMENTATION

- 范围：新增独立 `newapi` 模式，首先适配 njusehub/New API；官方 DeepSeek 与 DashScope 原生协议不在本轮范围。
- RED/GREEN：Provider 请求首测 `1 failed`（类不存在）后转为 `17 passed`；提取/重试边界 `16 failed, 3 passed` 后转为 `19 passed`，Provider 全量 `35 passed`。
- 安全审计：结构化 Provider 错误 `4 failed` 后转为 `4 passed`；Provider/Loop 回归 `67 passed`。错误事件只记录稳定 code、HTTP status、attempts 和 retryable，不记录 Key、上游正文或 reasoning。
- 组合：CLI `newapi`/secret-file 三项 `3 failed` 后转绿；Web provider/model 显示 `1 failed, 1 passed` 后为 `2 passed`；伪网关完整 `read_file → replace_text → run_validation → finish` 流程通过。
- 文档与配置：`njusehub.example.toml` 缺失测试 `1 failed` 后转为 `1 passed`；本地 `njusehub.toml` 加入 Git ignore，示例不含凭据。
- 最终自动化：`compileall` 通过；全量 `319 passed in 6.81s`；离线机制演示的 `final_status=succeeded`、`no_progress_status=no_progress`；wheel 与 sdist 均构建成功并包含 CLI、模板和静态资源。
- 真实 njusehub 冒烟：`newapi · qwen-turbo` 在独立回环端口运行，一次性任务 4 步完成 `read_file → write_file → run_validation → finish`，单次推进约 1.3–2.6 秒，最终 `succeeded` 且最新验证通过；服务随后正常停止。记录不含 Key、响应正文或 reasoning。
- 仓库审计：无 tracked `njusehub.toml`、`.forgeloop` 或 SQLite；令牌扫描只命中文档中的 Bearer/凭据说明文字与明确 fake fixtures，未发现真实凭据。
