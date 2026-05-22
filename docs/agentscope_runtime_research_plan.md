# AgentScope Runtime 调研与迭代计划

> 当前实现状态（2026-05-21）：`data` 主路由已前置到 AgentScope data planner，AgentScope 负责提交结构化 `analysis_plan`，SQL Harness 继续负责计划校验、安全、权限、人工审批、执行、审计和评测。本文保留为历史调研和迭代背景；最新架构图与请求生命周期以 [current_architecture.md](current_architecture.md) 为准。

本文沉淀 Financial Copilot Platform 是否以及如何引入 AgentScope 的调研结论。目标不是把现有 SQL React 主链路整体替换为自由 ReAct Agent，而是在保留企业级 NL2SQL harness 的前提下，引入可控的 agentic analysis 能力。

Phase 0 只做文档边界确认和 README 定位同步，不修改 `dispatcher`、`sql_react`、API 路由或 SQL 执行代码。后续 Phase 1 起才进入 ToolCatalog、AgentScopeRuntime 等运行时开发。

## 1. 背景与问题

当前项目已经形成一条强约束 NL2SQL 主链路：

```text
Final Graph -> SQL React
  -> recall_evidence
  -> query_enhance
  -> select_tables
  -> authorize_selected_tables
  -> assess_feasibility
  -> sql_retrieve / complex_plan
  -> sql_generate
  -> safety_check
  -> authorize_sql
  -> approve
  -> execute_sql
  -> error_analysis / result_reflection
```

这条链路的核心价值不是“能生成 SQL”，而是把 LLM 约束在可控、可审批、可审计、可评测的工程体系内，包括：

- 单次 evidence 召回复用。
- 语义模型选表和逻辑外键补表。
- 复杂查询模式切换。
- SQL 安全检查。
- 数据权限门禁。
- Human-in-the-Loop 审批。
- 执行失败修复和异常结果反思。
- LangSmith/CozeLoop 追踪和 Evaluation 闭环。

因此，引入 AgentScope 时必须回答两个问题：

1. 它能补足现有系统的什么能力？
2. 它是否会削弱现有 harness 的安全、审批、审计和评测能力？

## 2. DataAgent 调研结论

本次对 `/Users/a0000/project/DataAgent` 当前 `feat-agentscope` 分支做了只读调研，关键结论如下。

### 2.1 它不是典型多 agent 协作

DataAgent 当前实现不是 `Planner Agent + SQL Agent + Validator Agent + Report Agent` 的多 agent 协作模式，而是：

```text
一个 CommonAgent
  + AgentScope ReActAgent
  + ToolCatalog
  + Memory
  + Hook
  + SkillBox
```

代码上只有一个主要 agent 模板 `commonagent`。仓库约束也明确要求：

- 业务侧只保留一个默认 `agentType=commonagent`。
- 不恢复多 `agentType` 模板体系。

因此，DataAgent 证明的是“单 ReAct Agent + 工具体系”的产品形态，而不是“多 agent 并发协作”。

### 2.2 它为什么改用 AgentScope

DataAgent 的迁移提交删除了大量旧 StateGraph 节点和 dispatcher，新增了 AgentScope runtime、tool、memory、session、hook 等模块。合理推断其主要动机是：

- 降低固定 StateGraph 节点链路的维护成本。
- 让 agent 自主决定工具调用顺序。
- 适配“用户创建数据智能体、绑定数据源/知识/技能”的开放产品形态。
- 复用 AgentScope 的 ReActAgent、toolkit、memory、skillBox、hook、session 能力。

### 2.3 它的价值边界

DataAgent 的做法适合开放式数据分析助手：

- 用户问题形态更开放。
- 数据源、知识、技能按 agent 配置动态变化。
- Agent 可以先探索表结构，再决定是否生成 SQL。
- 产品更强调灵活分析，而不是固定审批链路。

但这种模式不天然适合 Financial Copilot Platform 的核心财务 NL2SQL 主链路，因为：

- 工具调用顺序更依赖 prompt。
- 路径不稳定，回归评测更难。
- SQL 审批、权限、审计如果交给自由 agent，风险更高。
- 业务 workflow state 不再像 LangGraph TypedDict 一样显式。

## 3. 对 Financial Copilot Platform 的架构判断

### 3.1 不建议整体替换 SQL React

不建议把现有 SQL React 整体替换为单个 AgentScope ReActAgent。原因：

- `recall_evidence -> select_tables -> assess_feasibility` 是强依赖主干，不能简单并发。
- 节点内部 fan-out 用 Python `asyncio` 即可，不需要 AgentScope。
- 多个 SQL step 如果只是同一种能力处理不同 step，本质是 parallel map，不是多 agent 协作。
- 企业级权限、安全、审批、审计、评测是平台控制面，不应交给自由 agent。

### 3.2 推荐双运行区

推荐把产品分为两个运行区：

```text
强约束区：Financial SQL Harness
  - 负责 NL2SQL 主链路
  - 负责权限、安全、审批、执行、审计、评测

开放协作区：Agentic Analysis Workspace
  - 负责开放式数据探索
  - 负责复杂分析辅助规划
  - 负责报告生成
  - 负责技能扩展
```

入口仍由平台级 `Intent Router` 控制：

```text
用户请求
  -> Platform Intent Router
      -> strict_sql_query      -> SQL React
      -> exploratory_analysis  -> AgentScopeRuntime
      -> complex_analysis      -> SQL React + AgentScopeRuntime
      -> report_generation     -> AgentScopeRuntime
      -> chat / knowledge      -> RAG Chat
```

AgentScope 不应第一阶段接管入口路由。它可以在内部调用 `intent_router.classify` 做自检和 handoff，但最终路由权仍归平台。

### 3.3 Phase 0 边界结论

基于前文背景、DataAgent 调研和架构判断，Phase 0 的落地结论是先把运行区边界写清楚，而不是开始替换或重写 SQL React。

| 区域 | 当前归属 | Phase 0 结论 |
|------|----------|--------------|
| 严格 SQL 查询主链路 | `Final Graph -> SQL React` | 保持不变，仍是默认执行路径 |
| 权限、安全、审批、执行、审计、评测 | Financial SQL Harness | 不下放给 AgentScope |
| 开放式数据探索 | 规划中的 `Agentic Analysis Workspace` | 后续可由 AgentScopeRuntime 承担 |
| 复杂分析辅助规划 | SQL React 先判定复杂模式 | 后续可让 AgentScope 生成分析计划和 SQL 草稿，但草稿必须回到 harness |
| 报告生成与 skill 扩展 | 规划中的辅助能力 | 只读取已执行结果或扩展 prompt/tool allowlist，不成为执行事实源 |

因此，Phase 0 的验收不是“AgentScope 跑起来”，而是团队在 README 和技术文档中形成一致表述：SQL Harness 继续控制执行事实源，AgentScope 只作为后续开放分析运行区引入。

## 4. 核心概念设计

### 4.1 ToolCatalog

`ToolCatalog` 是受控工具注册表，不是工具本身。

它负责：

- 根据 `task_type` 返回允许暴露给 AgentScope 的工具。
- 根据 `security_context`、用户、角色、部门和 session 限制工具可见性。
- 为每个工具提供 name、description、input schema、output contract。
- 注入 `session_id`、`thread_id`、`security_context`、`workflow_state` 等运行时上下文。
- 统一记录 tool trace、错误、审计事件。
- 禁止 AgentScope 绕过 SQL harness 直接执行高风险动作。

建议第一批工具：

| 工具 | 用途 | 是否允许直接执行 |
|------|------|------------------|
| `semantic_model.search` | 查询表/字段补充语义 | 是 |
| `business_knowledge.search` | 查询业务术语、公式、FAQ、口径 | 是 |
| `schema.list_tables` | 查看可见表清单 | 是，但必须按权限过滤 |
| `schema.describe_table` | 查看表结构和字段语义 | 是，但必须按权限过滤 |
| `schema.related_tables` | 查看逻辑外键和关系图 | 是，但必须按权限过滤 |
| `sql_guard.check` | 校验 SQL 草稿结构、安全和口径 | 是 |
| `sql_draft.submit` | 提交 SQL 草稿给 SQL React harness | 否，不执行 |
| `artifact.read` | 读取已执行结果、图表、中间分析 | 是 |
| `report.render` | 生成 Markdown/HTML/ECharts 报告 | 是 |
| `current_time.now` | 获取当前时间 | 是 |

关键原则：

```text
AgentScope 可以生成 SQL 草稿，但不能绕过 safety_check / authorize_sql / approve / execute_sql。
```

### 4.2 AgentScopeRuntime

`AgentScopeRuntime` 是 AgentScope 的运行适配层。它负责：

- 选择 agent 模板。
- 组装 system prompt 和 task prompt。
- 从 ToolCatalog 获取工具。
- 加载 AgentScope memory。
- 注入 canonical workflow state。
- 启动 AgentScope ReActAgent。
- 收集 streaming event、tool trace、intermediate notes。
- 将结果转换为平台结构化输出。

建议接口语义：

```python
AgentScopeRuntime.run(
    task_type: str,
    query: str,
    session_id: str,
    security_context: dict,
    workflow_state: dict,
    enabled_skills: list[str],
) -> AgentRunResult
```

`AgentRunResult` 必须结构化，不能只返回自然语言：

```python
AgentRunResult(
    answer: str,
    tool_trace: list[dict],
    sql_drafts: list[dict],
    artifacts: list[dict],
    clarification_questions: list[str],
    risk_flags: list[dict],
    state_patch: dict,
)
```

### 4.3 Memory

必须区分两类 state：

```text
Canonical Workflow State
  - 由 Financial Copilot Platform 维护
  - 保存 query、rewritten_query、recall_context、selected_tables、sql、approval、permission、result、audit
  - 是执行、审批、审计和评测的事实源

AgentScope Memory
  - 由 AgentScopeRuntime 使用
  - 保存 agent 的分析过程、工具调用上下文、用户偏好、报告草稿
  - 只能作为辅助上下文，不能成为执行事实源
```

建议 memory 类型：

- `conversation_memory`：用户偏好和历史对话摘要。
- `tool_trace_memory`：工具调用轨迹。
- `artifact_memory`：SQL 结果、图表和报告片段。
- `analyst_notes`：Agent 生成的分析备注。

任何会影响 SQL 执行的内容，都必须回写到 canonical workflow state，并重新进入 SQL harness。

### 4.4 Skill

Skill 是能力包，不等于 agent。

建议 skill 目录结构：

```text
agent_skills/
  budget_variance_analysis/
    SKILL.md
    manifest.json
    examples.json

  revenue_cost_relation/
    SKILL.md
    manifest.json
    examples.json

  ar_ap_aging/
    SKILL.md
    manifest.json
    examples.json

  audit_sampling/
    SKILL.md
    manifest.json
    examples.json
```

每个 skill 定义：

- 适用场景。
- 财务口径。
- 推荐工具。
- 禁止行为。
- 输出格式。
- few-shot。
- 可能涉及的表、字段、指标提示。

AgentScopeRuntime 根据 `task_type` 和用户启用的 skill 注入 skill prompt 和 tool allowlist。

## 5. 适合引入 AgentScope 的场景

### 5.1 开放式数据探索

典型问题：

```text
这个数据源里有哪些财务指标？
哪些表可能和预算分析有关？
这些管理表之间是什么关系？
```

这类请求不一定要立即生成 SQL。AgentScope 可以通过 schema/semantic/business knowledge 工具探索并返回解释。

### 5.2 复杂分析辅助规划

典型问题：

```text
分析收入、成本、预算、回款、费用之间的关系。
找出今年利润下滑的可能原因。
比较不同部门预算执行偏差并给出解释。
```

推荐流程：

```text
SQL React 判定 complex_analysis
  -> AgentScopeRuntime 生成分析计划和 SQL 草稿
  -> SQL 草稿回到 SQL React harness
  -> safety_check / authorize_sql / approve / execute_sql
  -> AgentScopeRuntime 基于执行结果生成解释和报告
```

### 5.3 报告生成

AgentScope 只读取已执行结果和 artifacts：

```text
SQL results
Python analysis result
业务知识
图表配置
  -> Report Agent
  -> Markdown / HTML / ECharts
```

报告生成不得直接查库或执行 SQL。

### 5.4 技能扩展

新增财务分析能力时，优先通过 skill 扩展：

- 预算执行偏差分析。
- 收入成本关系分析。
- 回款风险分析。
- 费用异常分析。
- 审计抽样分析。

这样可以减少对 SQL React 主链路的侵入。

## 6. 不适合交给 AgentScope 的能力

以下能力继续留在 SQL React / 平台 harness：

- 平台级 intent routing 最终决策。
- 数据权限门禁。
- SQL 安全检查。
- 人工审批。
- SQL 执行。
- 审计日志。
- 重试次数和超时策略。
- 结果异常判定。
- Evaluation 回归评测。

这些是平台控制能力，不是 agent 自主能力。

## 7. 迭代计划

### Phase 0：文档与边界确认（当前阶段）

目标：把“SQL Harness 是事实源、AgentScope 是辅助运行区”的边界写进技术文档与 README，避免把强约束 SQL 主链路整体替换。

交付：

- 本调研文档完成 Phase 0 边界收束。
- README 中补充“Agentic Analysis Workspace”规划定位。
- 明确当前阶段不修改 `Final Graph`、`SQL React`、API 路由、权限门禁、SQL 安全检查、审批、执行、审计或 Evaluation 链路。
- 明确 AgentScope 在后续阶段只能先进入开放探索、复杂分析辅助规划、报告生成和 skill 扩展，不直接执行 SQL。

验收：

- 团队能区分 SQL Harness、ToolCatalog、AgentScopeRuntime、Skill、Memory 的职责。
- 不再用“AgentScope 更快”作为引入理由，而用“开放探索、动态分析、技能扩展”作为引入理由。
- README 和本调研文档对“AgentScope 不替换 SQL React 主链路”的表述一致。
- Phase 0 diff 只包含文档变更，不包含运行时代码变更。

### Phase 1：ToolCatalog 基础层

目标：把可暴露给 agent 的工具统一注册和授权。

交付：

- `agents/runtime/tool_catalog.py`
- `agents/runtime/tool_contracts.py`
- 首批只读工具：
  - `semantic_model.search`
  - `business_knowledge.search`
  - `schema.list_tables`
  - `schema.describe_table`
  - `schema.related_tables`
  - `current_time.now`
- 工具调用 trace 结构。
- 工具 allowlist 按 `task_type` 过滤。

验收：

- AgentScope 未接入时，也可以单测 ToolCatalog。
- 任何 schema 工具都必须按 `security_context` 过滤。
- 不提供直接执行 SQL 的工具。

当前实施状态：

- 已新增 `agents/runtime/tool_contracts.py`，定义 `ToolContract`、`RuntimeTool`、`ToolTrace` 和 `ToolCallResult`。
- 已新增 `agents/runtime/tool_catalog.py`，实现 `ToolCatalog`、`ToolProviders`、task allowlist、只读工具合同、权限过滤和调用 trace。
- 已接入首批只读工具：`semantic_model.search`、`business_knowledge.search`、`schema.list_tables`、`schema.describe_table`、`schema.related_tables`、`current_time.now`。
- 已通过 `tests/test_runtime_tool_catalog.py` 单测验证：AgentScope 未接入时可独立测试 ToolCatalog；schema/semantic/relationship 输出不会暴露 denied table；`report_generation` 不暴露 schema 工具；当前不提供 SQL 执行工具。

### Phase 2：AgentScopeRuntime 最小可用版

目标：接入 AgentScope 作为开放式探索 runtime，不碰 SQL 执行。

交付：

- `agents/runtime/agentscope_runtime.py`
- `agents/runtime/result.py`
- `exploratory_analysis` task type。
- 一个 `common_analysis_agent` prompt。
- SSE/trace 事件适配。

验收：

- 用户可以询问数据源/表/字段关系，AgentScope 通过只读工具返回解释。
- AgentScopeRuntime 返回结构化 `AgentRunResult`。
- 不产生 SQL 执行副作用。

当前实施状态：

- 已新增 `agents/runtime/result.py`，定义 `AgentRunResult`，固定 `answer`、`tool_trace`、`sql_drafts`、`artifacts`、`clarification_questions`、`risk_flags`、`state_patch` 和 `events` 输出字段，并提供 SSE event 适配。
- 已新增 `agents/runtime/agentscope_runtime.py`，实现 Phase 2 最小 `AgentScopeRuntime`，从 `ToolCatalog` 注入任务 allowlist 工具和对应 agent prompt。
- 已安装并声明 `agentscope>=1.0.20` 依赖；`agents/runtime/agentscope_adapter.py` 提供真实 `AgentScopePackageRunner` 和本地兼容 `LocalAgentScopeCompatibleRunner`。
- `AgentScopePackageRunner` 已接入官方 `ReActAgent`、`Toolkit`、`Msg` 和 `ToolResponse`：平台 `RuntimeTool` 会注册为 AgentScope tool function，工具调用仍通过 `AgentScopeRunContext.invoke_tool(...)` 回到 `ToolCatalog`，输出再转换为 `AgentRunResult`。
- 本地兼容 runner 仅作为测试/fallback 后端，用于没有模型配置或不希望触发真实 LLM 时验证同一 ToolCatalog 边界。
- `AgentScopeRunContext.invoke_tool(...)` 统一通过 `ToolCatalog.invoke(...)` 调用工具并收集 trace/event；不在运行时提供 SQL 执行工具。
- 真实 backend 如果模型或 AgentScope 调用失败，会返回结构化 `agentscope_adapter_error` 风险，避免影响现有 SQL Harness。
- AgentScope 生成的 SQL 只会作为 `sql_drafts` 返回，运行时强制标记 `execution_mode=draft_only` 和 `requires_harness=True`，必须回到 SQL Harness 完成 safety、authorize、approve、execute。
- 已通过 `tests/test_agentscope_runtime.py` 和 `tests/test_agentscope_adapter.py` 单测验证：结构化结果序列化、只读工具注入、工具 trace 收集、unsupported task type 风险返回、AgentScope 缺失风险返回、本地兼容 runner 工具流和 SQL 草稿不执行约束。

### Phase 3：Report Agent

目标：让 AgentScope 只基于已执行结果生成报告。

交付：

- `artifact.read`
- `report.render`
- `report_generation` task type。
- Markdown 报告输出。
- 可选 ECharts 配置输出。

验收：

- Report Agent 只能读取已有 result/artifact。
- 不能调用 schema SEARCH 或 SQL execution。
- 输出包括结论、关键指标、异常点、后续追查建议。

当前实施状态：

- 已在 `ToolCatalog` 中新增 `artifact.read` 和 `report.render` 两个只读工具。
- `report_generation` allowlist 已收紧为只包含 `artifact.read` 和 `report.render`，不再暴露 schema、semantic、business knowledge、current time 或 SQL execution 工具。
- `artifact.read` 只读取 canonical `workflow_state` 中已有的 `artifacts`、`result`、`results`、`execution_result` 或 `query_result`。
- `report.render` 基于已有 result/artifact 生成 Markdown 报告，固定包含“结论、关键指标、异常点、后续追查建议”，并可基于已有指标输出 ECharts 配置。
- `AgentScopeRuntime` 已支持 `report_generation` task type，并注入 `report_agent` prompt；真实 AgentScope runner 仍保持懒加载。
- 已通过 `tests/test_runtime_tool_catalog.py` 和 `tests/test_agentscope_runtime.py` 验证：Report Agent 只能读取已有 artifact/result，schema 工具和 business knowledge 工具在 `report_generation` 下被拒绝，报告输出包含规定章节且不产生 SQL 草稿或执行副作用。

### Phase 4：SkillRegistry

目标：把常见财务分析方法沉淀为可插拔 skill。

交付：

- `agents/runtime/skill_registry.py`
- skill manifest 加载。
- 首批两个 skill：
  - `budget_variance_analysis`
  - `revenue_cost_relation`

验收：

- task_type 能自动匹配 skill。
- skill 只能扩展 prompt 和 tool allowlist，不能绕过权限和 SQL harness。
- skill 输出格式可被测试固定。

当前实施状态：

- 已新增 `agents/runtime/skill_registry.py`，定义 `SkillDefinition` 和 `SkillRegistry`。
- 已支持从 `manifest.json` + `SKILL.md` 加载 skill metadata、prompt、关键词、task type、推荐工具和输出格式。
- 已内置 `budget_variance_analysis` 与 `revenue_cost_relation` 两个 skill，覆盖预算差异分析和收入成本关系分析。
- `AgentScopeRuntime` 已接入 `SkillRegistry`：未显式传入 skill 时按 `task_type + query` 自动匹配，显式传入时按名称启用。
- Skill 只能注入 prompt，并且工具列表只会在 `ToolCatalog.get_tools(...)` 已允许的工具集合内做交集；即使 skill manifest 声明 `sql.execute` 或 schema 工具，也不能突破当前 task type allowlist。
- 已通过 `tests/test_skill_registry.py` 和 `tests/test_agentscope_runtime.py` 验证：内置 skill 匹配、manifest 加载、输出格式序列化、runtime skill prompt 注入，以及 SQL execution/schema 工具不被 skill 绕过。

### Phase 5：Complex Analysis Bridge

目标：把 AgentScope 引入复杂分析，但 SQL 仍由 SQL React harness 执行。

交付：

- `complex_analysis` task type。
- AgentScope 生成结构化分析计划和 SQL 草稿。
- `sql_draft.submit` 工具。
- SQL 草稿进入现有 `safety_check -> authorize_sql -> approve -> execute_sql`。

验收：

- AgentScope 不能直接执行 SQL。
- 所有 SQL 草稿必须经过 safety、permission、approval。
- complex analysis 的端到端结果可被 Evaluation 记录。

当前实施状态：

- `AgentScopeRuntime` 已支持 `complex_analysis` task type，并注入 `complex_analysis_agent` prompt。
- `ToolCatalog` 已在 `complex_analysis` allowlist 中新增 `sql_draft.submit`，但不提供任何 SQL execution 工具。
- `sql_draft.submit` 只生成 SQL 草稿 handoff payload，固定返回 `execution_mode=draft_only`、`requires_harness=True` 和 `harness_steps=[safety_check, authorize_sql, approve, execute_sql]`。
- `sql_draft.submit` 会对 payload 中声明的 `tables` 先做表级权限预检查；最终 SQL 安全检查、SQL 权限校验、人工审批和执行仍必须回到 SQL Harness。
- `AgentRunResult.sql_drafts` 会被 runtime 再次归一化为 draft-only，并追加 `sql_draft_not_executed` 风险标记，避免把 AgentScope 输出误认为执行事实。
- 已保留 `/api/agentscope/complex-analysis` 后端调试入口，但产品前端不再提供独立 AgentScope 按钮；复杂分析必须从现有 SQL Agent 入口进入，再由意图分析/规则引擎切到复杂计划链路。
- 录制脚本仅保留主 `SQL Agent` 的复杂计划 demo，用于验证复杂分析仍然经过用户确认和 SQL Harness。
- 已通过 `tests/test_runtime_tool_catalog.py`、`tests/test_agentscope_runtime.py`、`tests/test_agentscope_adapter.py`、`tests/test_agentscope_api.py` 和静态页面/录制脚本测试验证：复杂分析可提交 SQL 草稿、不会返回执行结果、不会暴露 execute_sql 工具，草稿包含回到 Harness 的步骤。

### Phase 6：Shadow Benchmark

目标：评估 AgentScope 局部能力是否真的提升质量或体验。

交付：

- 对比集：
  - 普通 NL2SQL。
  - 开放探索。
  - 复杂分析。
  - 报告生成。
- 指标：
  - P50/P95 延迟。
  - LLM 调用次数。
  - token 消耗。
  - SQL 草稿通过率。
  - 人工审批通过率。
  - 工具调用失败率。
  - 最终回答可用率。

验收：

- AgentScope 只在优势场景启用。
- 普通严格 SQL 查询默认继续走 SQL React。
- 若 AgentScope 路径质量或成本不达标，可以配置关闭。

当前实施状态：

- 已新增 `agents/runtime/shadow_benchmark.py`，定义 `ShadowBenchmarkCase`、`ShadowRunRecord`、`ShadowThresholds` 和 `ShadowBenchmark`。
- `ShadowBenchmark.default_cases()` 固定覆盖普通 NL2SQL、开放探索、复杂分析和报告生成四类对比样本；其中 `strict_sql_query` 的 baseline/candidate 都是 `sql_harness`，默认不建议启用 AgentScope。
- `ShadowBenchmark.summarize(...)` 可按 `task_type + runtime` 聚合 P50/P95 延迟、平均 LLM 调用次数、平均 token、SQL 草稿通过率、人工审批通过率、工具调用失败率和最终回答可用率。
- `ShadowBenchmark.should_enable_agentscope(...)` 根据阈值判断是否建议启用 AgentScope；普通严格 SQL 查询始终返回 `False`。
- 已通过 `tests/test_shadow_benchmark.py` 验证：默认对比集、指标聚合、分组汇总和启用阈值判断。

## 8. 推荐最终形态

```text
Financial Copilot Platform
  ├── Platform Intent Router
  │   ├── strict_sql_query -> SQL React Harness
  │   ├── exploratory_analysis -> AgentScopeRuntime
  │   ├── complex_analysis -> SQL React Harness + AgentScopeRuntime
  │   ├── report_generation -> AgentScopeRuntime
  │   └── knowledge/chat -> RAG Chat
  │
  ├── SQL React Harness
  │   ├── recall_evidence
  │   ├── select_tables
  │   ├── permission gate
  │   ├── safety_check
  │   ├── approval
  │   ├── execute_sql
  │   └── evaluation
  │
  └── Agentic Analysis Workspace
      ├── AgentScopeRuntime
      ├── ToolCatalog
      ├── SkillRegistry
      ├── AgentScope Memory
      └── Artifact Store
```

最终目标不是“把 LangGraph 换成 AgentScope”，而是：

```text
强约束 NL2SQL 继续由 SQL Harness 保障；
开放探索、复杂分析、报告生成和技能扩展由 AgentScope 提供弹性。
```
