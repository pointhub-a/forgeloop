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
