# 迭代优化记录

## Iteration 43：AgentScope Data Planner 主链路重构

### 背景

最近的复杂查询链路把 SQLReact 放在主路径：先做召回、选表、复杂度判断，再把 `workflow_state` 注入 AgentScope prepass。实际验证发现，AgentScope 只是在已有上下文上补一个规划动作，既没有充分发挥自主决策能力，又增加了 LLM 和上下文注入延迟。

### 方案

入口路由收敛为三类：

- `data`：当前系统数据处理请求。
- `chat`：通用对话或外部信息。
- `clarify`：需要补充目标、口径、时间或主体。

所有 `data` 请求统一进入 `AgentScopePlanner`。Planner 自主调用 business knowledge、schema、semantic model、relationship、time 等工具，最终提交结构化 `analysis_plan`。简单查询是单 SQL step，复杂分析是多 SQL step 加 merge/report step。

SQL 执行仍由 Harness 负责：

```text
AgentScopePlanner -> analysis_plan.submit -> validate -> safety -> authorize -> approve -> execute -> report
```

### 验证要求

- 单测验证 `data/chat/clarify` 入口路由。
- 单测验证 `data` 不再进入 SQLReact 主路径。
- 单测验证 `analysis_plan.submit` 只做 handoff，不执行 SQL。
- 真实 query 验证 tool trace 和 LangSmith span：`dispatcher.classify_route.llm`、`agentscope.runtime.data_analysis`、`agentscope.tool.*`、`analysis_plan.submit`。

### 2026-05-24 设计记录：AgentScope SQL Draft 与 SQL Harness 边界

LangSmith trace 排查确认，当前 `data_analysis` 链路的真实行为是：

```text
AgentScope data planner LLM 生成 analysis_plan，并可在 SQL step 中提前生成 SQL draft
-> agentscope.tool.sql.normalize / safety_check / authorize_draft 做草稿级预检
-> agentscope.tool.analysis_plan.submit 提交 plan_only handoff
-> approve_analysis_plan 做计划审批
-> execute_analysis_plan 进入 SQL Harness
-> SQL Harness 使用已提交 SQL 或在缺失 SQL 时调用 sql.sql_generate.llm
-> safety_check / authorize_sql / execute_sql / error_analysis / repair
```

这说明 `approve_analysis_plan` 之前已经可能存在 SQL。若 `analysis_plan.steps[*].sql` 已存在，`execute_analysis_plan` 会走“使用已提交 SQL”的分支，不再调用 `sql.sql_generate.llm`；只有 step 中没有 SQL 时，才进入 SQL Harness 的 `sql.sql_generate.llm` 生成 SQL。

接受当前设计：**AgentScope 可以提前生成 SQL draft，但它生成的是草稿，不是最终执行事实。SQL Harness 仍是正式复核、审批、授权、执行和修复边界。**

当前 SQL Harness 在 trace 中体现为分散节点：

- `approve_analysis_plan`：校验 plan、提取引用表、做计划级授权，并通过 `interrupt(...)` 等待用户审批。
- `execute_analysis_plan`：将 approved `analysis_plan` 转成 `complex_plan` 并进入复杂计划执行。
- `execute_complex_plan_step` 内部：执行正式 `sql.safety_check`、`sql.authorize_sql`、`sql.execute_sql`；失败时再调用 `sql.error_analysis.llm` 和 `sql.sql_generate.llm` 做修复。

当前 trace 命名的问题：

- `agentscope.llm.data_analysis_agent` 实际包住了整个 AgentScope agent run，不是纯 LLM call，容易误导为所有 tool 都发生在 LLM 内部。
- `agentscope.tool.sql.safety_check` 和后续 `sql.safety_check` 名称相近，但职责不同：前者只是 planner draft preflight，后者是 Harness 执行前正式安全门。
- SQL Harness 没有统一的 `sql_harness.*` wrapper，导致审批、授权、执行边界在 LangSmith 中不够直观。

目标 trace 结构：

```text
agentscope_data_planner
  agentscope.agent.data_analysis_agent
    agentscope.llm.data_analysis_agent.reasoning
    agentscope.tool.query.context_rewrite
    agentscope.tool.schema.select_candidates
    agentscope.llm.data_analysis_agent.reasoning
    agentscope.preflight.sql.normalize
    agentscope.preflight.sql.safety_check
    agentscope.preflight.sql.authorize_draft
    agentscope.handoff.analysis_plan.submit

approve_analysis_plan
  sql_harness.validate_analysis_plan
  sql_harness.authorize_plan_tables
  sql_harness.request_approval

execute_analysis_plan
  sql_harness.execute_plan
    sql_harness.step.1.prepare_submitted_sql
    sql_harness.step.1.safety_check
    sql_harness.step.1.authorize_sql
    sql_harness.step.1.execute_sql
    sql_harness.step.1.repair_sql_generate   # only on failure
    sql_harness.merge_report
```

关于重复 safety check 的取舍：

- 必须保留 SQL Harness 阶段的正式 `sql.safety_check` / `sql.authorize_sql`，这是执行前最终 gate。
- AgentScope 阶段的 `sql.safety_check` / `sql.authorize_draft` 不是最终 gate，只能定位为 advisory preflight。
- 若保留 AgentScope 预检，trace 必须改名为 `agentscope.preflight.*`，避免与 Harness 正式检查混淆。
- 更清晰的方案是 AgentScope 阶段只保留轻量 SQL 格式规范化，安全和授权统一交给 SQL Harness；若为了提前拦截明显错误而保留预检，也不能把它展示成最终安全结论。

后续实现建议：

- 将 `agentscope.llm.data_analysis_agent` 改为 `agentscope.agent.data_analysis_agent`，真实 LLM call 单独打 span。
- 不再基于最终 `tool_trace` 后补 `agentscope.react.*` 或 `agentscope.plan.*` 伪节点；SQL draft/plan 的生成应体现在真实 `agentscope.llm.data_analysis_agent.reasoning` 输出中。
- 将 AgentScope 提交前校验命名为 `agentscope.preflight.*`。
- 给审批和执行阶段补充 `sql_harness.*` wrapper/span，让 Harness 在 LangSmith 中成为一眼可见的阶段。
- 不让 LLM 最终回复回写完整 `AgentRunResult.tool_trace/events/state_patch`；这些应由 runtime 组装，LLM 只输出 answer、analysis_plan 或澄清问题。

### 2026-05-24 调查记录：AgentRunResult 组装与 ReAct 内部 Trace 缺失

针对 LangSmith 中 `agentscope.llm.data_analysis_agent` output 出现大段 JSON 的现象，确认当前机制如下：

```text
1. AgentScopeRuntime 创建 AgentScopeRunContext
2. AgentScopePackageRunner 注册 Toolkit
3. ReActAgent 开始内部循环
4. LLM 决定调用某个 tool
5. AgentScope 调度 toolkit function
6. ToolCatalog 执行 tool，runtime 记录 context.tool_trace / context.events
7. LLM 读取 tool observation 并继续决策
8. LLM 生成 SQL draft / analysis_plan
9. LLM 调 analysis_plan_submit
10. ToolCatalog 保存 plan_only handoff
11. LLM 输出最终 assistant 文本
12. AgentScopePackageRunner._convert_reply(...) 将最终 reply 归一化为 AgentRunResult
13. AgentScopeRuntime.run(...) 合并 context.tool_trace / context.events 到 AgentRunResult
14. Dispatcher 读取 result.state_patch.analysis_plan 继续进入 approve_analysis_plan
```

结论：

- `AgentRunResult` 不是每次 tool 调用后都组装。
- 每次 tool 调用只会记录真实 `ToolTrace` 到 `context.tool_trace`，不会生成完整 `AgentRunResult`。
- `AgentRunResult` 的组装发生在 AgentScope 最终 reply 返回之后，由 adapter/runtime 完成。
- LLM 不应该理解或输出完整 `AgentRunResult`，只需要输出简洁 `answer`、`analysis_plan` 或 `clarification_questions`。
- 之前 LangSmith 看到的大 JSON，是因为 prompt 要求“输出必须能被平台转换为 AgentRunResult，包括 tool_trace/events/state_patch”，导致模型把 observation 自己整理进最终 assistant 文本。

LangSmith 缺少以下节点：

```text
LLM 调 tool
LLM 继续看 observation
LLM 生成 SQL draft / plan
LLM 调 analysis_plan_submit
```

原因不是这些动作没有发生，而是此前可观测性只记录了：

- 一个手工包裹的 `agentscope.llm.data_analysis_agent` span；
- 每个 ToolCatalog 执行 span；
- 没有记录 ReAct 内部“决策/观察/生成/提交意图”的中间 span。

最终修复方向：

- 将 `agentscope.llm.data_analysis_agent` 改为 `agentscope.agent.data_analysis_agent`，避免把整个 agent run 伪装成单次 LLM call。
- 在 AgentScope model 对象外包一层 `_TracingModelProxy`，把每次真实 `model(...)` 调用桥接到 LangSmith LLM callback，命名为 `agentscope.llm.data_analysis_agent.reasoning`。
- 不基于最终 `tool_trace` 生成后补 span。后补 `agentscope.react.tool_call.*`、`agentscope.react.observation.*`、`agentscope.plan.sql_draft_generate` 会让 LangSmith 看起来像发生了额外真实调用，已放弃。
- 收窄 data_analysis prompt，明确最终回复不要回写 `tool_trace/events/state_patch` 或完整 `AgentRunResult`。

实现状态：

- 已新增 `AgentScopeRunContext.start_chain_span/end_chain_span`，用于真实 AgentScope agent wrapper。
- 已将 package runner 的外层 span 从 `agentscope.llm.data_analysis_agent` 改为 `agentscope.agent.data_analysis_agent`。
- 已新增 `_TracingModelProxy`，真实 AgentScope ReAct reasoning 每次调用模型时产生 `agentscope.llm.data_analysis_agent.reasoning`。
- 已移除后补的 `agentscope.react.*` 和 `agentscope.plan.*` 合成 span。
- 已更新 prompt，要求最终回复只输出简洁 answer、analysis_plan 或 clarification_questions。

验证：

```bash
.venv/bin/python -m pytest tests/test_agentscope_adapter.py::test_package_runner_emits_real_llm_spans_for_data_analysis_without_synthetic_react_nodes -q
# 1 passed
```

### 2026-05-19 修复记录：真实 AgentScope 计划提交失败

真实查询 `收入成本预算回款费用之间的关系` 暴露了两个问题：

- AgentScope package 会把 `analysis_plan.submit` 参数提交成半结构化草稿，例如只有 `reason/steps/sql_draft`，缺少 `mode/type/tables`。工具层现在在 `ToolCatalog` 内统一归一化 `plan`、`analysis_plan`、`plan_text`，既保留已经结构化的 plan，也能从 SQL/Markdown 草稿提取表并生成可校验 plan。
- AgentScope 最终 reply metadata 可能带有旧的 `analysis_plan` wrapper。adapter 之前用 `setdefault` 合并，导致成功的工具输出无法覆盖旧 metadata，dispatcher 最终误判为“未提交 analysis_plan”。现在成功的 `analysis_plan.submit` 输出是权威来源，会覆盖模型 reply metadata。

验证结果：

```bash
.venv/bin/python -m pytest tests/test_runtime_tool_catalog.py tests/test_agentscope_adapter.py tests/test_dispatcher.py tests/test_agentscope_runtime.py::test_data_analysis_runtime_hands_analysis_plan_to_harness tests/test_agentscope_runtime.py::test_runtime_emits_langsmith_spans_for_runner_and_tools -q
# 56 passed
```

真实接口验证：

```bash
curl -sS -X POST http://localhost:8080/api/query/invoke \
  -H 'Content-Type: application/json' \
  -d '{"query":"收入成本预算回款费用之间的关系","session_id":"debug-agentscope-toolcatalog-normalized-20260519","route":"data","rewritten_query":"收入成本预算回款费用之间的关系"}'
```

返回 `status=pending_approval`、`pending_approval=true`、`approval_type=complex_plan`，展示给用户的是可读的 SQL Harness 审批计划，不再是“AgentScope 未提交可执行 analysis_plan”的失败文案。

### 2026-05-19 修复记录：计划 SQL 行注释被压平导致 MySQL 语法错误

审批执行阶段曾出现：

```text
复杂查询计划执行失败。共处理 1/1 个步骤：
错误: You have an error in your SQL syntax ... near '' at line 1
```

根因是 `ToolCatalog._normalize_sql_text()` 用空格压缩了所有 whitespace，导致多行 SQL 里的 `-- 收入...` 行注释和下一行 `SUM(...)` 被合并到同一行。MySQL 会把 `--` 后面的整段 SQL 当作注释，执行时只剩不完整的 `SELECT je.period,`。

修复：

- SQL 文本归一化只去掉行尾空白和末尾分号，保留换行，避免破坏 `--` 行注释语义。
- `analysis_plan.submit` 会从 step SQL 的 `FROM/JOIN` 中提取真实物理表并并入 `step.tables`，避免模型提交 `tables:["business"]` 但 SQL 实际引用 `t_receivable_payable` 时绕过授权。

验证：

```bash
.venv/bin/python -m pytest tests/test_runtime_tool_catalog.py tests/test_agentscope_adapter.py tests/test_dispatcher.py tests/test_sql_react.py::TestComplexRoute::test_execute_complex_plan_step_uses_submitted_step_sql_without_regenerating tests/test_sql_react.py::TestComplexRoute::test_execute_complex_plan_step_preserves_submitted_sql_line_comments tests/test_sql_react.py::TestComplexRoute::test_execute_complex_plan_step_blocks_unsafe_submitted_step_sql -q
# 59 passed
```

## 迭代 1：Schema 召回策略优化（参考 DataAgent）

### 为什么优化

对比阿里 DataAgent 项目，发现我们的 schema 召回策略存在根本性问题：

| 对比项 | DataAgent | 我们（优化前） |
|--------|-----------|----------------|
| Schema 召回方式 | LLM 选表 → metadata 精确过滤 | 向量相似度检索（模糊匹配） |
| 语义模型 | 业务名称 + 同义词 + 业务描述 | 只有 information_schema 原始字段 |
| 列信息丰富度 | 表/列分开存储，列有 samples、foreignKey | 扁平文本，一个表一个 doc |

**核心问题**：表名和列名是确定性的，不需要"语义模糊匹配"。用户问"查询各部门费用"，向量检索可能召回不相关的表，而 LLM 直接看表名列表就能判断需要用 `t_cost_center` 和 `t_journal_item`。

### 优化了什么

将 schema 召回从"向量相似度检索"改为"LLM 选表 + metadata 过滤精确拉取"两步走：

**Step 1: 表名列表召回**（轻量级）
- 启动时从 MySQL 拉取所有表名，缓存为表名列表
- 用户提问时，LLM 从表名列表中选择相关的表

**Step 2: Schema 精确拉取**（metadata 过滤）
- 按选中的表名，从 Milvus 用 metadata 过滤精确拉取 schema 文档
- 不依赖向量相似度，100% 准确

### 怎么优化的

#### 新增节点：`select_tables`

```

## Iteration 42：移除 SQL 子图二次查询重写节点

### 背景

`classify_intent` 已经一次性完成意图分类和查询重写，返回 `intent + rewritten_query`。SQL React 子图中的 `contextualize_query` 原本用于兼容直接调用 SQL 子图且没有传入 `rewritten_query` 的旧路径，但当前产品链路已经明确不支持绕过 `classify_intent` 直接进入 SQL 子图。

保留该节点会带来三个问题：

- 链路追踪中多一个“看起来会 rewrite”的节点，但正常请求只是透传，容易误导排障。
- 文档和图节点职责重复，让人以为 SQL 子图还会做第二次查询重写。
- 旧兼容逻辑可能重新读取历史消息并产生新的 rewritten query，和外层分类节点的稳定输入原则冲突。

### 方案

- 删除 `agents/flow/sql_react.py` 中的 `contextualize_query` 节点和 `rewrite_query` 依赖。
- SQL React 子图入口从 `START -> contextualize_query -> recall_evidence` 改为 `START -> recall_evidence`。
- 保留 `query/rewritten_query` 作为稳定 state 字段，下游节点继续按 `enhanced_query -> rewritten_query -> query` 的优先级读取。
- README 和技术设计文档同步更新，明确查询重写只在外层 `classify_intent` 完成。

### TDD 验证

先新增图结构测试，要求：

- SQL React 图中不再包含 `contextualize_query` 节点。
- `__start__` 直接连接 `recall_evidence`。

红灯结果：

```bash
.venv/bin/python -m pytest tests/test_sql_react.py::TestBuildSqlReactGraph::test_graph_has_all_nodes tests/test_sql_react.py::TestBuildSqlReactGraph::test_graph_starts_at_recall_evidence -q
# 2 failed，原因是图中仍存在 contextualize_query，START 仍连向 contextualize_query
```

实现后再跑 SQL React 相关测试，保证结构变更没有破坏现有节点行为。
用户问题 + 表名列表 → LLM 判断需要哪些表 → 返回 table_names
```

- 从 Milvus 中获取所有 schema 文档的 table_name（去重）
- LLM 看到表名列表 + 用户问题，输出需要的表名
- 比向量检索更准确：LLM 理解"费用"对应 `t_expense_claim`，而向量检索可能匹配不到

#### 改造 `sql_retrieve`

```
优化前: query → vector similarity search → docs
优化后: query → select_tables (LLM) → metadata filter by table_name → docs
```

向量检索保留为 fallback：当 LLM 无法判断需要哪些表时（如模糊查询），回退到向量检索。

### 提升预期

| 指标 | 优化前（向量检索） | 优化后（LLM 选表 + 精确过滤） |
|------|-------------------|-------------------------------|
| 召回准确率 | 依赖 embedding 质量 | LLM 直接判断，理论上接近 100% |
| 延迟 | 向量检索 ~200ms | LLM 调用 ~500ms（多一次调用） |
| 可解释性 | 低（黑盒相似度） | 高（LLM 给出选表理由） |

**取舍**：用一次额外的 LLM 调用换取更高的召回准确率。对于 SQL 场景，错误的 schema 会导致生成错误 SQL，准确率比延迟更重要。

> **后续优化**：迭代 5 进一步优化为"向量粗筛 top-10 + LLM 精选"两阶段方案，避免全量表名发 LLM 的 token 浪费。

---

## 迭代 2：语义模型（字段级业务映射）

### 为什么优化

现在 schema 文档只包含 information_schema 的原始信息（字段名、类型、COMMENT）。用户说"查记账金额"，LLM 看到的是 `amount decimal(18,2)`，无法确定哪个字段对应"记账金额"。

DataAgent 的语义模型为每个字段维护：
- `business_name`：业务名称（如"记账金额"）
- `synonyms`：同义词（如"交易金额, 发生额"）
- `business_description`：业务描述（解释枚举值、状态码等）

这些信息注入 SQL 生成 prompt 后，LLM 能准确映射业务语言到物理字段。

### 优化了什么

1. MySQL 新建 `t_semantic_model` 表，存储字段级业务映射
2. Admin API 支持 CRUD 语义模型配置
3. `schema_indexer` 索引时 JOIN 语义模型，丰富 schema 文档内容
4. `sql_generate` prompt 自动带上增强后的 schema（含业务名称和同义词）

### 怎么优化的

#### 数据模型

```sql
CREATE TABLE t_semantic_model (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    table_name VARCHAR(128) NOT NULL,
    column_name VARCHAR(128) NOT NULL,
    business_name VARCHAR(256) COMMENT '业务名称',
    synonyms TEXT COMMENT '同义词，逗号分隔',
    business_description TEXT COMMENT '业务描述',
    UNIQUE KEY uk_table_col (table_name, column_name)
);
```

#### Schema 文档增强

索引时，对每个字段查找语义模型，丰富 page_content：

```
优化前:
  表名: t_journal_item
  字段:
    amount decimal(18,2) COMMENT '金额'

优化后:
  表名: t_journal_item
  字段:
    amount decimal(18,2) -- 记账金额
      同义词: 交易金额, 发生额, 借贷金额
      描述: 凭证行的借方或贷方金额，正值表示借方，负值表示贷方
```

### 提升预期

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| "查记账金额" | LLM 可能猜错字段 | 直接映射到 amount |
| "交易金额是多少" | 需要 LLM 理解 COMMENT | 同义词直接命中 |
| 枚举值查询 | LLM 不知道 status=1 含义 | 业务描述解释清楚 |

---

## 迭代 3：业务知识配置 ✅

### 为什么优化

用户问"毛利率是多少"，LLM 不知道"毛利率 = (收入 - 成本) / 收入 * 100"，也无法知道这个公式关联 `t_journal_item` 和 `t_account` 表。业务知识是"不存在于数据库 schema 中的计算逻辑和领域定义"。

DataAgent 的 BusinessKnowledge 模块存储业务术语 + 公式 + 同义词，向量检索后注入 SQL 生成 prompt。

### 优化了什么

1. MySQL 新建 `t_business_knowledge` 表，存储业务术语、公式、同义词
2. 向量化存入 Milvus（metadata.source = "business_knowledge"）
3. `sql_react` 图新增 `recall_evidence` 节点，向量检索业务知识
4. 检索结果注入 `sql_generate` prompt
5. Admin API 新增业务知识 CRUD（GET/POST/DELETE/batch/reindex）

### 怎么优化的

#### 数据模型

```sql
CREATE TABLE t_business_knowledge (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    term VARCHAR(128) NOT NULL COMMENT '业务术语',
    formula TEXT NOT NULL COMMENT '公式/定义',
    synonyms TEXT COMMENT '同义词，逗号分隔',
    related_tables TEXT COMMENT '关联表名，逗号分隔',
    UNIQUE KEY uk_term (term)
);
```

#### 图流程变更

```
优化前: START → load_table_names → select_tables → sql_retrieve → ...
优化后: START → load_table_names → select_tables → recall_evidence → sql_retrieve → ...
```

#### 消费路径

```
recall_evidence:
  用户问题 → 向量检索 t_business_knowledge (score > 0.3) → 匹配的业务知识
  ↓
sql_generate:
  prompt += "业务知识:\n毛利率 = (收入-成本)/收入*100\n预算执行率 = 实际/预算*100"
```

### 提升预期

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| "毛利率是多少" | LLM 不知道公式，无法生成 SQL | 注入公式 + 关联表，直接生成 |
| "预算执行率" | LLM 可能误解为普通字段查询 | 注入"实际/预算*100"公式 |
| 术语同义词匹配 | 无法识别 | 向量检索匹配同义词 |

## 迭代 4：SQL 领域智能体知识库 ✅

### 为什么优化

用户问"查询各部门费用汇总"，LLM 需要从零开始构造 SQL，可能遗漏 JOIN 条件、GROUP BY 逻辑。如果有相似问题的 SQL 示例（few-shot），LLM 可以参考模式生成更准确的 SQL。

DataAgent 的 AgentKnowledge 模块存储 Q&A 对，向量检索后注入 prompt 作为 few-shot 示例。

### 优化了什么

1. MySQL 新建 `t_agent_knowledge` 表，存储问题、SQL、说明、分类
2. 向量化存入 Milvus（metadata.source = "agent_knowledge"）
3. `recall_evidence` 节点同时检索业务知识 + 智能体知识库
4. 检索结果作为 few-shot 示例注入 `sql_generate` prompt
5. Admin API 新增智能体知识库 CRUD（GET/POST/DELETE/batch/reindex）
6. 种子数据：12 个常见财务 SQL Q&A 对

### 怎么优化的

#### 数据模型

```sql
CREATE TABLE t_agent_knowledge (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    question TEXT NOT NULL COMMENT '用户问题',
    sql_text TEXT NOT NULL COMMENT '参考 SQL',
    description TEXT COMMENT '说明',
    category VARCHAR(64) COMMENT '分类: query/report/analysis',
    UNIQUE KEY uk_question (question(128))
);
```

#### 图流程

```
recall_evidence:
  用户问题 → 向量检索 business_knowledge (score > 0.3) → 业务知识
  用户问题 → 向量检索 agent_knowledge (score > 0.3) → SQL Q&A few-shot
  ↓
sql_generate:
  prompt += "业务知识:\n毛利率 = ..."
  prompt += "相似问题参考:\n问题: 查询所有科目余额\nSQL: SELECT ..."
```

### 提升预期

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| "各部门费用汇总" | LLM 从零构造 SQL | 参考相似 Q&A，JOIN 逻辑更准确 |
| "预算执行情况" | LLM 可能忘记计算公式 | 注入 few-shot + 业务知识双保险 |
| 复杂多表查询 | LLM 容易遗漏关联条件 | 参考已有示例模式 |

---

## 迭代 5：表选择两阶段优化（向量粗筛 + LLM 精选）✅

### 为什么优化

迭代 1 把表选择从"向量检索"改为"LLM 选表"，但问题是把**所有表名**都发给 LLM。表少时没问题，表多（50+）时浪费 token。

对比 DataAgent 的方案：先向量检索 top-10 候选表，再让 LLM 从候选中精选。两阶段组合兼顾效率和准确率。

### 优化了什么

1. `select_tables` 节点改为两阶段：向量粗筛 → LLM 精选
2. 新增 `search_schema_tables` 函数：向量检索 schema 文档，返回 top-K 候选表名
3. 候选 ≤ 3 个时直接使用，省一次 LLM 调用
4. 向量检索失败时 fallback 到全量表名 + LLM 选表
5. 移除 `load_table_names` 独立节点（合并到 `select_tables` 内部）

### 怎么优化的

#### 图流程变更

```
优化前: START → load_table_names（全量） → select_tables（LLM 从全部选） → ...
优化后: START → select_tables（向量 top-10 → LLM 精选） → recall_evidence → ...
```

#### select_tables 逻辑

```python
# Stage 1: 向量粗筛
candidate_tables = search_schema_tables(query, top_k=10)

# Stage 2: 候选少，直接用
if len(candidate_tables) <= 3:
    return candidate_tables

# Stage 2: 候选多，LLM 精选
response = llm.invoke(f"从 {candidate_tables} 中选出需要的表")
```

### 提升预期

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| 50+ 表库 | 全量表名发 LLM（浪费 token） | 向量 top-10 → LLM 只看 10 个 |
| 3 个表命中 | LLM 调用（不必要） | 直接使用，省一次 LLM |
| 向量检索失败 | 报错 | fallback 到全量 + LLM |

### 待优化（后续迭代）

- **外键扩展**：向量粗筛后，自动补全外键关联的缺失表（需外键元数据）

---

## 迭代 6：上下文记忆系统 ✅

### 为什么优化

用户先问"zhangsan01是谁"（SQL 正确返回），接着问"他在哪个部门"时，系统无法解析"他"指的是 zhangsan01，生成的 SQL 缺少真实用户名。

**根因**：`FinalGraphState` 和 `SQLReactState` 没有 `chat_history` 字段，API 端点不传对话历史，SQL 生成流水线每次都是独立执行，没有上下文。

### 优化了什么

1. `FinalGraphState` 和 `SQLReactState` 新增 `chat_history` 和 `rewritten_query` 字段
2. API 端点（classify/invoke/approve）从 session store 加载对话历史，注入 graph state
3. SQL React 图新增 `contextualize_query` 入口节点，调用 `rewrite_query` 将代词化查询重写为独立查询
4. 意图分类（`classify_intent`）注入最近 3 轮对话历史，帮助理解代词
5. 下游节点（`select_tables`/`recall_evidence`/`sql_generate`）使用重写后的查询
6. SQL 审批中断时暂存原始 query，approve 后正确恢复并保存 Q&A

### 怎么优化的

#### 状态变更

```python
class FinalGraphState(TypedDict):
    query: str
    session_id: str
    chat_history: list[dict]     # 新增：对话历史
    intent: str
    ...

class SQLReactState(TypedDict):
    query: str
    rewritten_query: str         # 新增：上下文化后的独立问题
    chat_history: list[dict]     # 新增：对话历史
    ...
```

#### 图流程变更

```
优化前: START → select_tables → recall_evidence → sql_retrieve → ...
优化后: START → contextualize_query → select_tables → recall_evidence → sql_retrieve → ...
```

#### contextualize_query 逻辑

```python
async def contextualize_query(state):
    chat_history = state.get("chat_history", [])
    if not chat_history:
        return {"rewritten_query": state["query"]}  # 无历史，原样返回

    rewritten = await rewrite_query(
        summary=summary,
        history=chat_history[-6:],  # 最近 3 轮
        query=state["query"],
    )
    return {"rewritten_query": rewritten}
```

#### Session 持久化流程

```
invoke 端点:
  1. _load_chat_history(session_id) → 从 session store 加载历史
  2. graph.ainvoke({query, chat_history})
  3. _save_qa_to_session(session_id, query, answer) → 保存本轮 Q&A

approve 端点:
  1. invoke 中断时: _save_pending_query(session_id, query) → 暂存 query
  2. approve 恢复时: _pop_pending_query(session_id) → 取出原始 query
  3. _save_qa_to_session(session_id, original_query, answer) → 保存完整 Q&A
```

### Bug 修复

| Bug | 原因 | 修复 |
|-----|------|------|
| Q&A 未保存到 session | `compress_session` 是 async 函数，但在同步函数中直接调用未 await | 移除 compress_session 调用（历史 < 20 条不需要压缩） |
| approve 后 query 为空 | graph 恢复后 state 中 query 字段可能丢失 | 中断时暂存 query 到 session preferences，approve 后恢复 |
| Redis 未启动导致服务不可用 | `init_redis()` 连接失败直接 raise，阻断服务启动 | 改为 warning 日志，允许无 Redis 环境启动（session store 有内存 fallback） |

### 提升预期

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| "zhangsan01是谁" → "他在哪个部门" | "他"无法解析，SQL 缺少条件 | 重写为"zhangsan01在哪个部门" |
| "查一下这个月的费用" → "按部门分呢" | "按部门分"无法理解上下文 | 重写为"这个月的费用按部门分组" |
| 意图分类带代词 | "他"可能被分类为 chat | 注入历史后正确分类为 sql_query |

---

## 迭代 7：熔断降级与超时保护 ✅

### 为什么优化

项目依赖 Milvus、MySQL、Redis、LLM API、Elasticsearch 5 个外部服务。分析发现 Milvus 直接查询（5 个函数）、LLM API（8+ 调用点）、MySQL MCP 三个关键路径**无超时**，一旦下游挂起，整个请求链阻塞。

select_tables 节点在 Milvus 不可用时无限阻塞，是最先暴露的问题。

### 优化了什么

**Phase 1：超时保护（本次实施）**

| 文件 | 改动 |
|------|------|
| `agents/rag/retriever.py` | 5 个 Milvus 直接查询函数加 try/except，失败返回空 |
| `agents/flow/sql_react.py` | select_tables/recall_evidence/sql_retrieve/contextualize_query 加 `asyncio.wait_for` 超时 |
| `agents/model/providers/*.py` | 所有 5 个 provider 加 `request_timeout=60, max_retries=2` |
| `agents/tool/sql_tools/mcp_client.py` | execute_sql 加 `asyncio.wait_for(timeout=15)` |
| `agents/flow/rag_chat.py` | rewrite/chat/compress_and_save 加超时 |
| `agents/tool/storage/redis_client.py` | init_redis 失败不再 raise，允许无 Redis 启动 |

**Phase 2：Fallback 降级（已内置）**

| 组件 | 降级策略 |
|------|----------|
| Milvus 向量检索超时 | 返回空列表，跳过该路召回 |
| LLM 重写超时 | 使用原始 query |
| LLM 选表超时 | 使用向量检索结果 |
| MySQL 执行超时 | 进入 error_analysis 重试 |
| Redis 不可用 | session store 使用内存 dict |
| Redis checkpointer 不可用 | 使用 MemorySaver |

### 超时配置汇总

| 调用 | 超时 | 降级行为 |
|------|------|----------|
| Milvus 向量检索 | 8s | 返回空 |
| Milvus metadata 查询 | 10s | 返回空 |
| LLM request_timeout | 60s | 自动重试 2 次 |
| LLM rewrite/compress | 15s/30s | 使用原始值 |
| MySQL MCP execute_sql | 15s | 返回错误 |
| Milvus HybridRetriever | 30s 外层 | 返回空 |

### DataAgent 对比

对比本地 DataAgent 项目（`/Users/a0000/project/DataAgent`），熔断降级方面：

| 能力 | DataAgent | 我们 | 状态 |
|------|-----------|------|------|
| SQL 执行重试 | LLM 引导，最多 10 次 | LLM 引导，最多 5 次（可配置） | ✅ 已有 |
| DB 连接重试 | 3 次 + 线性退避 | MCP 长连接，无重试 | ⚠️ 后续补齐 |
| SQL 执行超时 | 30s | 15s（可配置） | ✅ 已有 |
| LLM 超时 | 仅图表生成 3s | 所有调用点 15~60s（可配置） | ✅ 超越 |
| LLM 重试 | ❌ 无 | max_retries=2 | ✅ 超越 |
| 向量检索降级 | catch → 返回空 | catch → 返回空 | ✅ 已有 |
| 知识入库降级 | mark FAILED | log only | ⚠️ 后续补齐 |
| 错误码映射 | 20+ SQLState | 16 种 SQLState + is_retryable() | ✅ 已有 |
| 可配置重试次数 | 配置文件 | ResilienceSettings（环境变量） | ✅ 已有 |
| 熔断器 | ❌ 无 | ❌ 无 | 双方都无 |

**核心结论**：DataAgent 也没有熔断器，其容错依赖"超时 + LLM 引导重试 + 节点级降级"三板斧。我们已在这三方面达到或超越 DataAgent。错误码映射和可配置重试已补齐。

### 详细设计

见 `docs/resilience_design.md`，包含 DataAgent 对比分析、三层方案、错误码映射设计。

---

## 迭代 8：错误码分类 + 可配置重试 ✅

### 为什么优化

迭代 7 实现了超时保护和基础重试，但存在两个问题：

1. **重试不区分错误类型**：语法错误（表不存在、列不存在）和连接错误（连接中断）同样重试，浪费 LLM 调用
2. **重试次数硬编码**：`_MAX_RETRIES = 3` 无法通过配置调整，需要改代码

DataAgent 有 16 种 SQLState 映射（`ErrorCodeEnum`）和可配置重试次数（`DataAgentProperties`），我们需要补齐。

### 优化了什么

1. 新建 `agents/tool/sql_tools/error_codes.py`，定义 16 种 SQLState 错误码分类 + `is_retryable()` 函数
2. 新增 `ResilienceSettings` 到 `agents/config/settings.py`，支持环境变量配置重试次数和超时
3. `sql_react.py` 的 `route_after_execute` 使用 `is_retryable()` 判断是否重试
4. 所有超时值改为从 `settings.resilience` 读取，不再硬编码

### 怎么优化的

#### 错误码分类

```python
# agents/tool/sql_tools/error_codes.py
SQL_ERROR_CODES = {
    "08001": ("连接建立失败", True),    # 可重试
    "08S01": ("连接中断", True),        # 可重试
    "28P01": ("密码错误", False),       # 不可重试
    "42S02": ("表不存在", False),       # 不可重试
    "42S22": ("列不存在", False),       # 不可重试
    # ... 共 16 种
}

def is_retryable(error_msg: str) -> bool:
    """连接类错误重试，语法/权限类不重试。未知错误默认不重试。"""
```

#### 可配置超时

```python
# agents/config/settings.py
class ResilienceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RESILIENCE_")
    max_sql_retries: int = 5            # RESILIENCE_MAX_SQL_RETRIES
    sql_execution_timeout: float = 15   # RESILIENCE_SQL_EXECUTION_TIMEOUT
    milvus_timeout: float = 8           # RESILIENCE_MILVUS_TIMEOUT
    llm_timeout: float = 60             # RESILIENCE_LLM_TIMEOUT
    llm_rewrite_timeout: float = 15     # RESILIENCE_LLM_REWRITE_TIMEOUT
```

#### 条件路由

```python
def route_after_execute(state):
    if not state.get("error"):
        return END
    if not is_retryable(state["error"]):  # 语法/权限错误不重试
        return END
    if state.get("retry_count", 0) < settings.resilience.max_sql_retries:
        return "error_analysis"
    return END
```

### 提升预期

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| "查不存在的表" | 重试 3 次 LLM（浪费 token） | 直接结束，返回"表不存在" |
| "密码错误" | 重试 3 次（无意义） | 直接结束，返回认证错误 |
| 连接超时 | 重试 3 次（正确） | 重试 5 次（可配置） |
| 生产环境调优 | 改代码重新部署 | 改环境变量重启 |

### 涉及文件

| 文件 | 改动 |
|------|------|
| 新建 `agents/tool/sql_tools/error_codes.py` | 16 种 SQLState + `is_retryable()` |
| `agents/config/settings.py` | 新增 `ResilienceSettings`，5 个可配置参数 |
| `agents/flow/sql_react.py` | `route_after_execute` 使用 `is_retryable`，超时从配置读取 |
| `agents/flow/rag_chat.py` | 超时从 `settings.resilience` 读取 |
| `agents/tool/sql_tools/mcp_client.py` | SQL 执行超时从配置读取 |

---

## Bug 修复记录

### Bug 1：ConnectionNotExistException 文件上传失败

**出现什么问题**：文件上传到 Milvus 时报 `ConnectionNotExistException`，langchain-milvus 内部调用 `Collection(alias=...)` 找不到连接。

**什么原因**：pymilvus 2.6.x 的 `MilvusClient` 不再自动注册到全局 `pymilvus.connections` 注册表。langchain-milvus 0.3.x 内部使用 `Collection(alias=...)` 时依赖全局注册表，导致找不到连接句柄。

**怎么解决的**：在 `agents/rag/retriever.py` 中实现 `_patch_milvus_connections()` 猴子补丁，拦截 `MilvusClient.__init__`，在创建后自动调用 `connections.add_connection()` 注册到全局注册表。补丁在应用启动时 (`app.py` 的 `_init_milvus()`) 执行一次。

### Bug 2：DataNotMatchException Insert missed field table_name

**出现什么问题**：用户文档上传时报 `DataNotMatchException: Insert missed field table_name`，Goagent2 集合的 `table_name` 字段为 NOT NULL。

**什么原因**：旧集合 Goagent2 的 schema 定义了 `table_name` 为非空字段，但用户上传的文档（PDF/TXT）没有这个字段。schema 文档有 `table_name`，用户文档没有。

**怎么解决的**：在 `agents/rag/indexing.py` 中，对所有非 schema 文档设置 `chunk.metadata["table_name"] = ""`。同时统一使用 `knowledge_base` 集合，通过 `source` 字段区分文档来源（mysql_schema / user_document / business_knowledge / agent_knowledge）。

### Bug 3：MCP MySQL Server 只读，无法写入 domain_summary

**出现什么问题**：`domain_summary` 表创建和写入操作静默失败，启动时无法保存领域摘要。

**什么原因**：MCP MySQL Server 配置为只读模式（`--read-only`），所有 DDL/DML 操作都被拒绝。之前所有数据库操作都通过 MCP，写操作无法执行。

**怎么解决的**：将 `agents/tool/storage/domain_summary.py` 中所有写操作改为 pymysql 直连（`ensure_domain_summary_table`、`save_domain_summary`），读操作保持通过 MCP。

### Bug 4：启动时 schema 索引被错误跳过

**出现什么问题**：启动后 Milvus 中没有 schema 文档，但 `_index_schemas_background` 认为已有数据而跳过索引。

**什么原因**：使用 `get_collection_stats().row_count` 判断是否有数据，但 Milvus 删除数据后 `row_count` 不立即更新（compaction 前显示旧值）。`domain_summary` 表中也有旧数据，导致双重误判。

**怎么解决的**：改为直接查询 `source == "mysql_schema"` 的文档是否存在，而不是依赖 `row_count`。同时检查 `domain_summary` 表中的摘要是否真正存在。

### Bug 5：Qwen Embedding 批量大小超限

**出现什么问题**：上传 PDF 文件时报 `Error code: 400 - batch size is invalid, it should not be larger than 10`。

**什么原因**：Qwen Embedding API 限制每次请求最多处理 10 个文本。langchain_milvus 的 `add_documents()` 内部调用 `embed_documents()` 时默认批量大小为 32 或更大，超出 API 限制。

**怎么解决的**：在所有 4 个 `_get_embeddings()` 函数中（`indexing.py`、`retriever.py`、`schema_indexer.py`、`parent_retriever.py`），为 Qwen provider 添加 `chunk_size=10` 参数传递给 `OpenAIEmbeddings`，强制限制批量大小。

---

## 迭代 9：RAG 知识体系重构 ✅

### 为什么优化

1. 意图识别做了两次（API `/classify` + Graph `classify_intent` 节点），浪费 LLM 调用
2. `final_graph.py` 命名不清晰，实际是意图调度器
3. 意图提示词中 `sql_query` 描述硬编码，不随数据库变化
4. 文档入库无 LLM 预处理，直接切块向量化，信息密度低
5. `recall_evidence` 串行检索，`select_tables` 输出冗余 `table_names`

### 优化了什么

#### 9.1 意图去重 + 文件重命名
- `final_graph.py` → `dispatcher.py`，`final.py` → `query.py`
- `classify_intent` 检测到 state 中已有 intent 时跳过 LLM 调用
- 前端 invoke 请求携带 intent 参数，Graph 直接路由
- API 路径 `/api/final/*` → `/api/query/*`

#### 9.2 动态意图提示词
- `sql_query` 描述从硬编码改为引用 `domain_summary`
- 数据库表结构变化时，意图分类自动适应

#### 9.3 LLM 文档预处理
- 新建 `agents/rag/doc_preprocessor.py`：DocumentPreprocessor 类
- 预处理流程：提取元数据（分类、标签、实体）→ 生成摘要 → 假设性问题 → 关键事实
- 新建 `t_document_metadata` MySQL 表存储元数据
- 每个 chunk 的 page_content 组合：`[摘要] + [原文] + [相关问题]`

#### 9.4 user_document 父子分块 + session_id 隔离
- 长文本（>3000 字）自动使用父子分块，短文本用普通分块
- Milvus schema 新增 `session_id` 字段
- 检索时按 `session_id` 过滤，用户文档互相隔离
- `get_hybrid_retriever` 对 session-scoped 检索单独创建实例（不走单例）

#### 9.5 recall_evidence 并行化
- `recall_business_knowledge` 和 `recall_agent_knowledge` 改为 `asyncio.gather` 并行
- 耗时从 sum(两个) 降为 max(单个)

#### 9.6 select_tables 精简
- 移除 `select_tables` 对 `table_names` 的输出，不再覆盖 state 中的全量表名

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/flow/final_graph.py` → `dispatcher.py` | 重命名 + 跳过逻辑 + 动态 prompt |
| `agents/api/routers/final.py` → `query.py` | 重命名 + intent 参数 |
| `agents/rag/doc_preprocessor.py` | 新建：LLM 文档预处理 |
| `agents/tool/storage/doc_metadata.py` | 新建：MySQL 元数据 CRUD |
| `agents/rag/indexing.py` | 集成预处理 + 父子分块阈值判断 |
| `agents/api/routers/document.py` | session_id 参数 + await 异步 |
| `agents/flow/sql_react.py` | select_tables 精简 + recall_evidence 并行 |
| `agents/flow/rag_chat.py` | session_id 检索过滤 |
| `agents/rag/retriever.py` | session_id_filter 支持 |
| `agents/api/app.py` | 路由注册 + schema 加 session_id + doc_metadata 表初始化 |
| `agents/static/index.html` | API 路径更新 + invoke 带 intent |
| `tests/test_final_api.py` | 适配新模块名 |
| `tests/test_imports.py` | 适配新模块名 |

---

## 迭代 10：意图识别 + 上下文重写合并 ✅

### 为什么优化

原有流程需要 **3 次 LLM 调用**：

```
/api/query/classify → LLM 1: 意图分类
/api/query/invoke   → LLM 2: 上下文重写（contextualize_query）
                    → LLM 3: SQL 生成（sql_generate）
```

问题：
1. 意图分类和上下文重写是独立的 LLM 调用，浪费 token
2. 重写后的查询可能改变意图（如"一季度营收多少"→"贵州茅台2026年第一季度营收多少"），但重写在分类之后，意图已经定了
3. 意图 prompt 中硬编码了示例（如 sql_query 的示例），导致 LLM 倾向于返回特定意图，而非根据实际数据库领域判断

### 优化了什么

**合并意图分类 + 上下文重写为一次 LLM 调用**，返回 JSON 结构：

```json
{
  "intent": "sql_query",
  "rewritten_query": "贵州茅台2026年第一季度营收多少"
}
```

### 怎么优化的

#### 1. 新 prompt 设计

```
你是一个智能助手，同时完成两个任务：

1. **意图分类**：根据数据库领域摘要和用户问题，判断意图类别
2. **查询重写**：结合对话历史，将代词化/省略的查询重写为独立完整的查询

意图类别说明：
- sql_query：用户想查询数据库中存储的结构化数据（必须与数据库领域摘要中的表/字段相关）
- chat：闲聊、通用问答、或问题与数据库领域无关

重要判断原则：
- 只有当问题明确指向数据库中的数据时，才归类为 sql_query
- 如果问题涉及的是公开信息、通用知识、股市行情等非数据库内容，应归类为 chat
- 结合对话历史重写查询时，只补充对话中明确提到的上下文，不要添加对话中没有的信息
```

**关键改进**：
- 移除硬编码示例，意图判断完全由 LLM 根据 domain_summary 决定
- 明确 sql_query 的边界：只有指向数据库的问题才是 sql_query
- 添加"不要添加对话中没有的信息"约束，防止 LLM 过度推断

#### 2. 返回 JSON 结构

classify 端点返回 `{intent, rewritten_query}`，前端捕获后：
- SQL 路径：invoke 请求带上 `{intent, rewritten_query}`
- Chat 路径：stream 请求直接用 `rewritten_query` 替代原始 query

#### 3. 下游节点跳过 LLM

- `classify_intent`：检测到 state 中有 intent + rewritten_query，跳过 LLM
- `contextualize_query`：检测到 state 中有 rewritten_query，跳过 LLM

#### 4. 优化效果

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| LLM 调用次数 | 3 次（classify + rewrite + generate） | 2 次（classify+rewrite 合并 + generate） |
| 意图准确性 | 硬编码示例导致偏见 | 纯 LLM 判断，结合 domain_summary |
| 上下文重写 | 重写在分类之后，意图可能不准 | 重写和分类同时完成，意图基于重写后的查询 |
| "一季度营收多少" | 可能误判为 sql_query | LLM 看到 domain_summary 中无此数据，归类为 chat |

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/flow/dispatcher.py` | classify_intent 合并重写，返回 JSON，prompt 去硬编码 |
| `agents/api/routers/query.py` | ClassifyResponse 加 rewritten_query，QueryRequest 加 rewritten_query |
| `agents/flow/sql_react.py` | contextualize_query 跳过逻辑 |
| `agents/static/index.html` | 捕获 rewritten_query，传给 invoke 和 stream |

---

## 迭代 11：下游节点去除对话历史依赖 ✅

### 为什么优化

迭代 10 将意图分类 + 上下文重写合并为一次 LLM 调用，返回 `rewritten_query`。但发现：

1. `dispatcher.py` 的 `sql_react` 节点仍然将 `chat_history` 传递给 SQL React 子图，子图中 `contextualize_query` 虽然检测到 `rewritten_query` 后跳过 LLM，但 `chat_history` 作为 state 字段被白白传递
2. `chat_direct` 节点将 `rewritten_query` 作为 `query` 传给 RAG Chat 子图，但未传递 `rewritten_query` 标记，导致 RAG Chat 的 `rewrite` 节点再次调用 LLM 重写（浪费 token）
3. 对话历史只在意图分析和查询重写两个阶段有价值，之后的节点（表选择、证据检索、SQL 生成、RAG 对话）都应该直接使用重写后的查询

### 优化了什么

**核心原则**：对话历史只在最外层 `classify_intent` 使用一次，之后所有下游节点只使用 `rewritten_query`。

1. `dispatcher.py` 的 `sql_react` 节点：移除 `chat_history` 传递，只传 `query` + `rewritten_query`
2. `dispatcher.py` 的 `chat_direct` 节点：将 `rewritten_query` 传入 RAG Chat 的 input dict
3. `rag_chat.py` 的 `preprocess` 节点：从 input 中读取 `rewritten_query`
4. `rag_chat.py` 的 `rewrite` 节点：检测到 `rewritten_query` 已存在时跳过 LLM 调用

### 怎么优化的

#### dispatcher.py 变更

```python
# 优化前：传递 chat_history
result = await sql_graph.ainvoke({
    "query": state["query"],
    "rewritten_query": state.get("rewritten_query", ""),
    "chat_history": state.get("chat_history", []),  # 多余
})

# 优化后：只传必要字段
result = await sql_graph.ainvoke({
    "query": state["query"],
    "rewritten_query": state.get("rewritten_query", ""),
})
```

```python
# 优化前：rag_chat 没有 rewritten_query 标记
result = await rag_graph.ainvoke({
    "input": {"session_id": ..., "query": rewritten or state["query"]},
})

# 优化后：明确传递 rewritten_query
result = await rag_graph.ainvoke({
    "input": {
        "session_id": ...,
        "query": rewritten or state["query"],
        "rewritten_query": rewritten,  # 标记已重写
    },
})
```

#### rag_chat.py 变更

```python
# preprocess: 读取 rewritten_query
return {
    "session": session.model_dump(),
    "query": inp["query"],
    "rewritten_query": inp.get("rewritten_query", ""),  # 新增
    ...
}

# rewrite: 跳过逻辑
async def rewrite(state):
    existing = state.get("rewritten_query", "")
    if existing:
        return {"rewritten_query": existing}  # 跳过 LLM
    # ... 原有重写逻辑
```

### 优化效果

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| SQL 路径 LLM 调用 | 3 次（classify+rewrite + contextualize 跳过 + generate） | 2 次（classify+rewrite + generate） |
| Chat 路径 LLM 调用 | 3 次（classify+rewrite + rag_rewrite + chat） | 2 次（classify+rewrite + chat） |
| chat_history 传递 | 从 API → dispatcher → sql_react 全链路携带 | 只在 API → dispatcher 使用，下游不传 |
| Token 消耗 | chat_history 占用 prompt token（3 轮 ≈ 500 token） | 下游节点无历史，prompt 更精简 |

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/flow/dispatcher.py` | sql_react 移除 chat_history 传递；chat_direct 传递 rewritten_query |
| `agents/flow/rag_chat.py` | preprocess 读取 rewritten_query；rewrite 跳过逻辑 |
| `agents/flow/sql_react.py` | 无变更（contextualize_query 跳过逻辑已有） |

---

## 迭代 12：recall_evidence 混合检索 + 质量过滤 ✅

### 为什么优化

`recall_evidence` 节点检索业务知识和智能体知识库，但存在两个问题：

1. **只用向量检索，不用 ES BM25**：`recall_business_knowledge` 和 `recall_agent_knowledge` 只查 Milvus 向量，缺少关键词精确匹配。当用户用精确业务术语提问时（如"预算执行率"），向量检索可能召回语义相似但内容无关的文档。
2. **无质量过滤**：智能体知识库（agent_knowledge）的核心价值是 SQL 示例（few-shot），但向量检索可能召回没有 SQL 的纯文本描述。业务知识（business_knowledge）的核心价值是公式和术语定义，但可能召回无公式的一般性描述。
3. **seed 脚本不索引到 ES**：`seed_business_knowledge.py` 和 `seed_agent_knowledge.py` 只存 Milvus，ES 中没有这些数据，BM25 检索无结果。

### 优化了什么

1. `recall_business_knowledge` 和 `recall_agent_knowledge` 改为混合检索：向量 + ES BM25 + RRF 融合
2. 新增质量过滤：`_filter_has_sql`（agent_knowledge 必须含 SQL）、`_filter_has_business_term`（business_knowledge 必须含公式/术语）
3. seed 脚本新增 ES 索引，Milvus + ES 双写
4. 抽取 `_milvus_vector_search` 和 `_es_bm25_search` 公共函数

### 怎么优化的

#### 混合检索流程

```
recall_business_knowledge / recall_agent_knowledge:
  query → Milvus 向量检索 (source filter) → vector_docs
  query → ES BM25 关键词检索 (metadata.source filter) → es_docs
  ↓
  RRF 融合 [vector_docs, es_docs] → fused_docs
  ↓
  质量过滤 → filtered_docs
```

#### ES BM25 检索

```python
def _es_bm25_search(query, source, top_k=10):
    body = {
        "size": top_k,
        "query": {
            "bool": {
                "must": [{"match": {"text": query}}],
                "filter": [{"term": {"metadata.source": source}}],
            }
        },
    }
    resp = es.search(index=settings.es.index, body=body)
```

使用 raw ES client（与 schema_indexer 和 seed 脚本格式一致），搜索 `text` 字段，过滤 `metadata.source`。

#### 质量过滤

```python
def _filter_has_sql(docs):
    """agent_knowledge 必须包含 SELECT/INSERT/UPDATE 等 SQL 关键词。"""
    sql_keywords = ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")
    return [d for d in docs if any(kw in d.page_content.upper() for kw in sql_keywords)]

def _filter_has_business_term(docs):
    """business_knowledge 必须包含公式/定义/术语关系。"""
    formula_indicators = ("=", "/", "*", "SUM", "COUNT", "公式", "定义", "计算", "比率", "率")
    return [d for d in docs if any(ind in d.page_content for ind in formula_indicators)]
```

#### Seed 脚本 ES 索引

```python
# seed_agent_knowledge.py / seed_business_knowledge.py
# 新增 ES 索引（与 Milvus 并行）
es = Elasticsearch(es_url)
for doc, doc_id in zip(docs, doc_ids):
    es.index(
        index=settings.es.index,
        id=doc_id,
        document={
            "text": doc["content"],
            "metadata": {"source": "agent_knowledge", "doc_id": doc_id},
        },
    )
```

### 优化效果

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| "预算执行率怎么算" | 向量检索可能召回相关但无公式的描述 | BM25 精确匹配"预算执行率" + 向量语义匹配，RRF 融合 |
| agent_knowledge 召回 | 可能召回无 SQL 的纯文本描述（浪费 token） | 质量过滤丢弃无 SQL 结果，只保留 few-shot 示例 |
| business_knowledge 召回 | 可能召回无公式的一般性描述 | 质量过滤丢弃无公式/术语结果，只保留定义和公式 |
| seed 数据 ES 检索 | ES 中无 business/agent knowledge 数据 | seed 脚本双写 Milvus + ES |

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/rag/retriever.py` | 新增 `_milvus_vector_search`、`_es_bm25_search`、`_filter_has_sql`、`_filter_has_business_term`；重写 `recall_business_knowledge`、`recall_agent_knowledge` |
| `scripts/seed_business_knowledge.py` | 新增 ES 索引 |
| `scripts/seed_agent_knowledge.py` | 新增 ES 索引 |

---

## 迭代 13：SQL React 图流程重构（证据前置 + 查询增强 + 语义模型） ✅

### 为什么优化

原流程 `select_tables → recall_evidence → sql_retrieve`，业务知识在选表之后才召回，导致：

1. **选表不准确**：用户问"GMV是多少"，向量检索 schema 文档时"GMV"无法匹配到 `orders` 表，因为 schema 中没有"GMV"这个词。但业务知识中有"GMV = 已支付订单总额"，如果先召回业务知识，就能用"已支付订单总额"去匹配 schema。
2. **缺少查询增强**：用户用业务术语提问（如"华东区GMV"），但数据库字段是物理名（如 `region`, `amount`），向量检索匹配度低。
3. **语义模型只在索引时使用**：`t_semantic_model` 的字段业务映射在 schema_indexer 索引时嵌入文档，但查询时无法直接访问结构化的语义模型数据来辅助 SQL 生成。

### 优化了什么

1. **流程重排**：`recall_evidence` 移到 `select_tables` 之前，业务知识先于选表
2. **新增 `query_enhance` 节点**：用证据翻译业务术语，增强向量检索命中率
3. **语义模型查询时加载**：`sql_retrieve` 阶段从 MySQL 加载选中表的语义模型，注入 `sql_generate` prompt

### 怎么优化的

#### 新流程

```
优化前: START → contextualize_query → select_tables → recall_evidence → sql_retrieve → ...
优化后: START → contextualize_query → recall_evidence → query_enhance → select_tables → sql_retrieve (+ semantic model) → ...
```

#### 新增 `query_enhance` 节点

```
输入: rewritten_query + evidence + few_shot_examples
输出: enhanced_query

示例:
  Query: "华东区上月GMV是多少"
  Evidence: "GMV = 已支付订单总额", "华东包含上海、江苏、浙江..."
  Enhanced: "华东区（上海、江苏、浙江）上月已支付订单总额是多少"
```

- 无证据时跳过（返回原查询）
- LLM 失败时 graceful degradation（返回原查询）
- 超时复用 `llm_rewrite_timeout`（15s）

#### `select_tables` 查询源变更

```python
# 优化前
query = state.get("rewritten_query") or state.get("query", "")

# 优化后
query = state.get("enhanced_query") or state.get("rewritten_query") or state.get("query", "")
```

#### `sql_retrieve` 扩展：加载语义模型

```python
# 新增：从 MySQL 加载选中表的字段业务映射
semantic = await asyncio.wait_for(
    asyncio.to_thread(get_semantic_model_by_tables, selected),
    timeout=settings.resilience.milvus_timeout,
)
return {"docs": docs, "semantic_model": semantic}
```

#### `sql_generate` prompt 增强

```python
# 语义模型文本格式
语义模型（字段业务映射）:
表 t_orders:
  amount | 业务名: 订单金额 | 同义词: 交易金额, GMV | 描述: 已支付订单的总金额
  region | 业务名: 区域 | 同义词: 地区 | 描述: 订单所属区域
```

prompt 新增要求："语义模型中提供了字段的业务名称和同义词，生成 SQL 时优先使用物理字段名，但可参考业务名称理解字段含义"

### 优化效果

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| "GMV是多少" | 向量检索匹配不到相关表 | 业务知识翻译 → "已支付订单总额" → 精准匹配 |
| "华东区费用" | 向量检索"华东"匹配度低 | 查询增强补充省份列表 → 更好的匹配 |
| SQL 字段映射 | LLM 只看 schema 文档中的 COMMENT | 语义模型提供结构化的业务名、同义词、描述 |
| 无业务知识 | 正常工作 | query_enhance 跳过，降级为原流程 |

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/flow/state.py` | SQLReactState 新增 `enhanced_query`、`semantic_model` |
| `agents/flow/sql_react.py` | 新增 `query_enhance` 节点；修改 `select_tables`/`sql_retrieve`/`sql_generate`/`_build_sql_messages`；更新图拓扑 |
| `agents/rag/retriever.py` | 新增 `get_semantic_model_by_tables()` |
| `tests/test_sql_react.py` | 新增 `query_enhance` 测试；更新 graph 节点断言 |

## 迭代 14：表描述 + 统一语义模型 + 表关系

### 为什么优化

1. **select_tables 只传英文表名**：LLM 看到 `t_order, t_user, t_payment` 无法判断哪个表与"订单金额"相关
2. **schema docs 与 semantic_model 重复**：Milvus 向量检索的 schema 文档和 MySQL 的 semantic_model 包含重叠信息
3. **缺少表关系信息**：sql_generate 不知道表之间如何 JOIN

### 优化了什么

**1. select_tables 表名带描述**

加载 `information_schema.tables` 的 TABLE_COMMENT，LLM prompt 格式：
```
候选表名:
- t_order: 订单主表
- t_user: 用户信息表
- t_payment: 支付记录表
```

**2. 统一到 t_semantic_model，去掉 Milvus schema 向量检索**

- 扩展 `t_semantic_model` 表，新增字段：`column_type`, `column_comment`, `is_pk`, `is_fk`, `ref_table`, `ref_column`
- 种子脚本自动从 `information_schema.columns` + `information_schema.key_column_usage` 同步技术 schema
- `sql_retrieve` 改为只查 MySQL `t_semantic_model`，不再从 Milvus 拉取 schema docs
- 从 semantic_model 构建 schema 文档（`_build_schema_docs_from_semantic`）

**3. select_tables 返回表关系**

- 新增 `get_table_relationships()` 函数，从 MySQL `information_schema.key_column_usage` 提取外键关系
- `select_tables` 返回 `selected_tables` + `table_relationships`
- `sql_generate` prompt 中加入表关系信息，帮助 LLM 生成正确的 JOIN 条件

**4. 关键词过滤字段**

- 新增 `_extract_keywords(query)` 使用 Jieba 分词提取关键词
- 新增 `_filter_columns_by_keywords()` 根据关键词过滤 schema 文档中的字段
- 保留匹配字段 + 时间字段 + PK/FK，精简 prompt

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/flow/state.py` | SQLReactState 新增 `table_relationships` |
| `agents/flow/sql_react.py` | `select_tables` 加载表描述+表关系；`sql_retrieve` 改为 MySQL-only；新增 `_build_schema_docs_from_semantic`、`_extract_keywords`、`_filter_columns_by_keywords` |
| `agents/rag/retriever.py` | `get_semantic_model_by_tables` 返回完整 schema 字段；新增 `get_table_relationships()` |
| `scripts/seed_semantic_model.py` | 扩展表结构；新增 `sync_schema_from_information_schema()` |
| `pyproject.toml` | 添加 `jieba>=0.42` 依赖 |
| `tests/test_sql_react.py` | 更新 `test_generate_retrieves_missing_tables` 使用 semantic_model mock |

### 优化效果

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| 表选择准确度 | LLM 只看英文表名 | 表名+描述，更易判断 |
| schema 信息来源 | Milvus 向量检索 + MySQL semantic_model | 统一 MySQL semantic_model |
| 表关联 | LLM 自己猜 JOIN 条件 | 提供外键关系信息 |
| prompt 大小 | 全量字段 | 关键词过滤后只保留 5-10 个核心字段 |

## 迭代 15：t_semantic_model 自动同步（全量初始化 + binlog 增量）

### 为什么优化

手动执行 `seed_semantic_model.py` 容易遗忘，新增表或字段后 semantic_model 不会自动更新。

### 优化了什么

新增 `agents/init/schema_sync.py` 模块，应用启动时自动同步：

**1. 全量初始化**
- 启动时检查 `t_semantic_model` 是否有数据
- 无数据则自动全量同步：从 `information_schema.tables` + `information_schema.columns` + `information_schema.key_column_usage` 读取所有表结构

**2. binlog 增量同步**
- 使用 `python-mysql-replication` 监听 MySQL binlog
- 检测 DDL 事件（CREATE TABLE / ALTER TABLE / DROP TABLE / RENAME TABLE）
- 自动增量更新 `t_semantic_model`

**3. 定时轮询 fallback**
- binlog 不可用时（权限、配置等），每 5 分钟轮询 `information_schema`
- 检测新增/删除的表，增量同步

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/init/__init__.py` | 新建 init 模块 |
| `agents/init/schema_sync.py` | 全量同步 + binlog 监听 + 轮询 fallback |
| `agents/api/app.py` | lifespan 中启动 schema_sync 后台任务 |
| `pyproject.toml` | 添加 `python-mysql-replication>=1.0` |

## 迭代 16：Bug 修复 + Jieba 过滤踩坑回顾

### Bug 1：ES BM25 检索返回无关结果

**现象**：`recall_agent_knowledge` 搜索 "毛利率" 时，ES 返回的全是财务报告摘要（贵州茅台一季报），而非 SQL 示例。

**原因**：ES 索引中 `metadata.source` 是 `text` 类型，`term` 查询无法精确匹配。财务报告被错误索引为 `agent_knowledge`，BM25 匹配到 "毛利" 关键词后返回。

**修复**：`_es_bm25_search` 的过滤条件从 `metadata.source` 改为 `metadata.source.keyword`（keyword 子字段支持精确匹配）。

### Bug 2：few_shot_examples 检索不到

**现象**：用户问 "查询上周毛利率" 时，`few_shot_examples` 返回空。

**原因**：
1. ES 返回无关结果（Bug 1）
2. `_recall_agent` 的 `top_k=3` 太小，向量检索返回的 3 个结果中，财务报告占了位置，过滤后无 SQL 示例

**修复**：
1. 修复 ES 精确匹配（Bug 1）
2. `_recall_agent` 的 `top_k` 从 3 增加到 10，确保过滤后仍有足够 SQL 示例

### Bug 3：逻辑外键未同步

**现象**：`t_journal_item.account_code` 在语义模型中 `is_fk=0`，schema 文档中没有 `REFERENCES` 标记。

**原因**：`sync_schema_from_information_schema` 只从 `information_schema.key_column_usage` 同步 FK，但数据库中未定义外键约束（业务逻辑上的关联，非数据库 FK）。

**修复**：在 `seed_semantic_model.py` 新增 `seed_logical_foreign_keys()` 函数，手动更新 6 个逻辑外键：
- `t_journal_item.entry_id` → `t_journal_entry.id`
- `t_journal_item.account_code` → `t_account.account_code`
- `t_journal_item.cost_center_id` → `t_cost_center.id`
- `t_budget.cost_center_id` → `t_cost_center.id`
- `t_budget.account_code` → `t_account.account_code`
- `t_expense_claim.cost_center_id` → `t_cost_center.id`

### Bug 4：MySQL 8.0 SHOW MASTER STATUS 废弃

**现象**：binlog 监听启动时报 SQL 语法错误。

**原因**：MySQL 8.0.22+ 废弃了 `SHOW MASTER STATUS`，改用 `SHOW BINARY LOG STATUS`。

**修复**：先尝试 `SHOW BINARY LOG STATUS`，失败则 fallback 到 `SHOW MASTER STATUS`。

### Bug 5：服务用系统 Python 启动

**现象**：`mysql-replication not installed, binlog listener disabled`。

**原因**：服务用 `/opt/homebrew/Cellar/python@3.14/...` 启动（系统 Python），但包装在 venv 里。

**修复**：用 venv Python 启动：`/path/to/.venv/bin/python -m agents.main`。

### 踩坑：Jieba 关键词过滤字段（已废弃）

**方案**：用 Jieba 分词提取查询关键词，过滤 schema 文档中不匹配的字段，精简 prompt。

**实现**：
```python
def _extract_keywords(query: str) -> list[str]:
    import jieba
    words = jieba.cut(query)
    return [w.strip() for w in words if len(w.strip()) >= 2]

def _filter_columns_by_keywords(docs, semantic_model, keywords):
    # 只保留匹配字段 + 时间字段 + PK/FK
    ...
```

**遇到的问题**：

1. **派生指标无法匹配**：用户问 "上周毛利率"，Jieba 切成 `["上周", "毛利率"]`。但 `毛利率` 的计算公式是 `(SUM(credit_amount) - SUM(debit_amount)) / SUM(credit_amount)`，字段名 `credit_amount` 和 `debit_amount` 不包含 "毛利率" 关键词，被错误过滤掉。

2. **业务术语到物理字段的映射只有业务知识能桥接**：关键词匹配是字符串层面的，无法理解 "毛利率 = (借方 - 贷方) / 借方" 这种业务定义。

**结论**：参考 DataAgent 项目，**不做字段级过滤**。DataAgent 的做法是：
- 选中表后返回全部字段（最多 50 列/表）
- 靠业务知识（evidence）+ 语义模型（semantic_model）+ SQL 生成 LLM 自己判断用哪些列
- 字段选择的负担放在 LLM 上，而非关键词匹配

**最终方案**：删除 `_extract_keywords` 和 `_filter_columns_by_keywords`，移除 `jieba` 依赖。`sql_retrieve` 直接返回选中表的全部字段。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/flow/sql_react.py` | 删除 `_extract_keywords`、`_filter_columns_by_keywords`；`_recall_agent` top_k 3→10 |
| `agents/rag/retriever.py` | `_es_bm25_search` 过滤条件改用 `metadata.source.keyword` |
| `agents/init/schema_sync.py` | binlog 兼容 MySQL 8.0.22+；线程池执行阻塞操作 |
| `scripts/seed_semantic_model.py` | 新增 `seed_logical_foreign_keys()`；修复 DictCursor |
| `pyproject.toml` | 移除 `jieba>=0.42`；修正 `mysql-replication>=1.0` 包名 |
| `tests/test_sql_react.py` | 新增 `TestContextualizeQuery` 测试 |

### 教训总结

| 问题 | 教训 |
|------|------|
| Jieba 过滤字段 | 关键词匹配无法处理派生指标，业务术语到物理字段的映射需要业务知识桥接 |
| ES 精确匹配 | `text` 字段用 `term` 查询会匹配失败，要用 `keyword` 子字段 |
| top_k 太小 | 有质量过滤时，top_k 要放大，否则过滤后可能为空 |
| MySQL 版本兼容 | `SHOW MASTER STATUS` 在 8.0.22+ 废弃，用 `SHOW BINARY LOG STATUS` |
| 系统 Python vs venv | 包装在 venv 里但用系统 Python 启动会找不到包 |

---

## 迭代 17：去除 Milvus Schema 索引依赖 ✅

### 为什么优化

统一 `t_semantic_model` 后，`sql_retrieve` 已经只从 MySQL 加载 schema，但 `select_tables` 仍然依赖 Milvus 做表发现（向量检索 schema 文档提取表名）。同时 admin 页面的"刷新 Schema"按钮、启动时的 `_index_schemas_background` 自动索引、`schema_indexer.py` 的 Milvus+ES 双写都已不再需要。

**核心矛盾**：schema 数据已经统一到 MySQL `t_semantic_model`，但表发现仍在绕道 Milvus，增加了不必要的依赖和延迟。

### 优化了什么

**1. select_tables 去除 Milvus 依赖**

- 移除 `search_schema_tables`（Milvus 向量检索 schema 文档返回候选表名）
- 移除 `get_schema_table_names`（Milvus metadata 查询所有表名）
- 移除 `load_table_names` 节点（已不在图中，但函数定义仍存在）
- `select_tables` 改为直接从 `load_full_table_metadata()` 加载表名+描述（MySQL `information_schema.tables`）

```
优化前: query → Milvus 向量检索 top-10 schema docs → 提取候选表名 → LLM 精选
优化后: query → MySQL information_schema.tables 加载全量表名+描述 → LLM 精选
```

**2. 移除 admin 刷新 Schema 功能**

- 删除 `POST /api/admin/refresh-schemas` 端点（调用 `schema_indexer.index_mysql_schemas`）
- 删除前端 Admin Tab 的"刷新 Schema"按钮和 `refreshSchemas()` 函数
- Admin Tab 改为提示用户使用 seed 脚本管理语义模型

**3. 移除启动时 schema 自动索引**

- 删除 `_index_schemas_background`（向量化 schema 到 Milvus + ES）
- 替换为 `_ensure_domain_summary`（仅在 domain_summary 为空时，从 MySQL 表元数据生成领域摘要）
- 领域摘要仍用于意图分类，但不再依赖 Milvus schema 文档

**4. 清理死代码**

- 删除 `get_schema_table_names`（retriever.py）
- 删除 `search_schema_tables`（retriever.py）
- 删除 `get_schema_docs_by_tables`（retriever.py，零调用方）

**保留的代码**

- `schema_indexer.py`：保留，seed 脚本仍在使用（`seed_financial.py`、`seed_all.py`）
- `VectorOnlyRetriever`：保留，eval runner 仍在使用
- Milvus + ES：保留用于业务知识、智能体知识、用户文档的向量检索

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/flow/sql_react.py` | `select_tables` 改为 MySQL-only；移除 `load_table_names`；移除 `search_schema_tables`/`get_schema_table_names` 导入 |
| `agents/rag/retriever.py` | 删除 `get_schema_table_names`、`search_schema_tables`、`get_schema_docs_by_tables` |
| `agents/api/routers/admin.py` | 删除 `POST /refresh-schemas` 端点和 `RefreshResponse` 模型 |
| `agents/api/app.py` | `_index_schemas_background` → `_ensure_domain_summary` |
| `agents/static/index.html` | 删除 Admin Tab 的刷新 Schema 按钮和 JS |

### 优化效果

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| 表发现 | Milvus 向量检索 + embedding 计算（~200ms） | MySQL 直查（~10ms） |
| 外部依赖 | select_tables 依赖 Milvus 可用 | select_tables 只依赖 MySQL |
| 启动索引 | 启动时向量化所有表 schema 到 Milvus + ES | 仅生成 domain_summary（如缺失） |
| Admin 功能 | 手动刷新 schema 索引 | 不再需要（schema 自动同步） |
| 代码量 | 3 个 Milvus schema 函数 + 启动索引 + admin 端点 | 全部移除 |

---

## Iteration 18：Schema 元数据 Redis 缓存

### 出现了什么问题

`load_full_table_metadata` 和 `get_semantic_model_by_tables` 每次请求都查 MySQL。高频调用时（如 SQL Agent 每次对话都触发 select_tables → sql_retrieve），产生不必要的 DB 压力。

### 为什么要解决

- 表元数据变化频率低（仅 DDL 时变更），适合缓存
- `start_schema_sync` 已有全量同步 + binlog 增量机制，可作为缓存维护者
- Redis 读取延迟 ~1ms vs MySQL ~10ms，减少 SQL Agent 响应时间

### 怎么解决

**Redis Key 设计**

| Key | 类型 | 内容 | TTL |
|-----|------|------|-----|
| `schema:table_metadata` | string (JSON) | `[{table_name, table_comment}, ...]` | 无（由 sync 任务维护） |
| `schema:semantic_model:{table}` | string (JSON) | `{column_name: {col_type, is_pk, ...}}` | 无 |

**Cache-Aside 模式**

1. 查询时：Redis → MySQL fallback → 回填 Redis
2. 同步时：更新 MySQL → 刷新 Redis

**涉及文件**

| 文件 | 改动 |
|------|------|
| `agents/rag/retriever.py` | 新增 `_get_sync_redis()` + Redis 常量；`load_full_table_metadata` 改为 Redis→MySQL→回填；`get_semantic_model_by_tables` 改为 Redis pipeline→MySQL→回填 |
| `agents/init/schema_sync.py` | 新增 `_refresh_redis_cache()`；全量同步后刷新 Redis；binlog 增量同步后更新指定表缓存；轮询检测到新增/删除表后更新缓存 |

**关键实现**

- Sync Redis 客户端（非 async），因为 `retriever.py` 的函数通过 `asyncio.to_thread` 调用
- Redis pipeline 批量操作（多个 `get` 或 `set` 一次 round-trip）
- Per-table key 便于增量失效（binlog 只刷新受影响的表）
- Redis 不可用时 graceful fallback 到 MySQL（try/except 兜底）

### 优化效果

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| load_full_table_metadata | 每次查 MySQL information_schema (~10ms) | Redis hit ~1ms，miss 时回填 |
| get_semantic_model_by_tables | 每次查 MySQL t_semantic_model (~5ms×N) | Redis pipeline 一次性取 (~1ms) |
| DDL 变更后 | 立即生效（直查 MySQL） | binlog → 更新 MySQL → 刷新 Redis（秒级延迟） |
| Redis 不可用 | N/A | 自动 fallback 到 MySQL，无影响 |

---

## Iteration 19：MCP 错误日志 + API 异常捕获

### 出现了什么问题

SQL Agent 执行 SQL 返回 HTTP 500，但服务器日志中看不到任何错误信息。错误被静默吞掉，无法排查。

### 为什么要解决

- 无日志 = 无法排查。500 可能来自 MCP 执行失败、LLM 调用超时、图执行异常等多种原因
- 需要在关键路径记录错误，快速定位问题

### 怎么解决

**涉及文件**

| 文件 | 改动 |
|------|------|
| `agents/tool/sql_tools/mcp_client.py` | `execute_sql` 记录入参 SQL + 返回结果；检测 `result.isError` 并 `raise RuntimeError` |
| `agents/api/routers/query.py` | `query_invoke` / `approve_sql` 添加 `try/except` + `logger.error(exc_info=True)`，返回错误而非 500 |

**关键改动**

- MCP 返回 `isError=True` 时，之前静默返回错误文本 → 现在 `logger.error` + `raise`
- API 端点无 try/except → 现在捕获异常并返回 `"系统错误: ..."` + 完整 traceback 日志

---

## Iteration 20：MCP MySQL Collation 冲突修复（二）

### 出现了什么问题

上次修复将 MCP MySQL charset 设为 `utf8mb4`，解决了 `utf8mb4_unicode_ci` 冲突。但 `utf8mb4` 在 mysql2 中默认 collation 是 `utf8mb4_general_ci`，与 MySQL 8.0 的 `utf8mb4_0900_ai_ci` 仍然不同，导致：

```
Illegal mix of collations (utf8mb4_0900_ai_ci,IMPLICIT) and (utf8mb4_general_ci,IMPLICIT) for operation '='
```

### 为什么要解决

- Collation 不一致导致 JOIN / WHERE 中的字符串比较失败
- `utf8mb4` 只指定字符集，不指定 collation，mysql2 会用旧版默认值

### 怎么解决

**涉及文件**

| 文件 | 改动 |
|------|------|
| mcp-server-mysql config | `charset` 默认值改为 `utf8mb4_0900_ai_ci` |
| `agents/tool/sql_tools/mcp_client.py` | `mcp_env` 中增加 `MYSQL_CHARSET=utf8mb4_0900_ai_ci`，不依赖 npx 缓存 |

**关键点**

- mysql2 的 `charset` 选项同时控制字符集和 collation
- 设置 `MYSQL_CHARSET` 环境变量确保即使 npx 缓存清除后修复仍生效

---

## Iteration 21：NL2SQL 增强稳定性 + 异常结果反思

### 出现了什么问题

SQL Agent 在处理类似“去年亏损”的查询时暴露了几类问题：

1. `query_enhance` 依赖 LLM 输出，LLM 空响应时直接退回原 query，业务术语增强不稳定。
2. 业务知识召回只依赖向量/BM25，口语化同义词（如“亏损”）召回不到时，增强链路无法使用 `t_business_knowledge`。
3. LLM 生成的 SQL 可能带有异常 token、Markdown 代码块、尾部截断关键字（如 `HAVIN`）或多余分号。
4. SQL 执行成功但结果异常（空集、NULL、包装结构中的 `rows: []` 等）时，原流程直接结束，无法自我修正。
5. 结果异常后反思生成修正 SQL，会再次触发审批；如果前端没有明确过程提示，用户会以为同一条 SQL 被重复审批。
6. approve 恢复父图时，缺失 `query` 会报 `KeyError: 'query'`；补回 `query` 后若节点同一步再次写入 `query`，会触发 LangGraph `INVALID_CONCURRENT_GRAPH_UPDATE`。

### 为什么要解决

- NL2SQL 的错误不只来自 SQL 执行异常，也可能来自“结果可执行但语义不可信”。
- 业务口径应来自可配置业务知识，而不是在代码里硬编码某个 query 的词表。
- 审批是用户交互节点，自动反思和二次确认必须让用户看见过程，否则体验上像“重复弹窗”。
- LangGraph 恢复时状态字段要稳定，避免 approve 后在父图/子图之间丢上下文。

### 怎么解决

**1. 业务知识驱动的 query_enhance**

- 移除 `_PROFIT_LOSS_HINTS`、`去年` 等 case 级硬编码。
- `query_enhance` 只解析召回到的业务知识 evidence：`术语`、`公式/定义`、`同义词`。
- `recall_business_knowledge` 增加 MySQL 词典兜底：当 Milvus/ES 未召回足够结果时，从 `t_business_knowledge.term/synonyms` 做通用同义词匹配。
- 业务词扩展通过维护 `t_business_knowledge` 完成，不再改代码。

**2. SQL 输出格式化与校验**

- 新增 `normalize_sql_answer()`，统一处理：
  - `<text_never_used_...>` / `</text_never_used_...>` 异常 token
  - Markdown SQL 代码块
  - SQL 前的解释性文本
  - 尾部多余分号
  - 截断关键字（如 `HAVIN`、`WHERE`、`AND`、`GROUP BY`）
  - 括号不匹配、非 `SELECT/WITH` 开头
- `sql_generate` 和 `result_reflection` 都显式调用本地 formatter，不能只依赖 LLM tool schema。
- 格式不合法时返回 `is_sql=False`，不进入审批/执行。

**3. 执行结果异常检测 + 反思修正**

- 新增 `_result_anomaly_reason()`，识别：
  - 裸 `[]`
  - `{"rows": []}`、`{"data": []}`、`{"result": []}`、`{"items": []}`
  - `{"columns": [...], "rows": []}`
  - `null` / `None`
  - 结构化结果中全字段为 `NULL` 或空字符串
  - 原始结果字符串中包含 `null` 或 `[]` 的可疑信号
- 新增 `result_reflection` 节点：执行成功但结果异常时，LLM 直接反思并生成修正后的 SQL。
- 反思后的 SQL 不再回到 `sql_generate`，而是走：

```text
execute_sql -> result_reflection -> safety_check -> approve -> execute_sql
```

这样避免“反思节点给建议，再让 sql_generate 再生成一次”的重复生成。

**4. 审批与 SSE 体验**

- `approve` 对反思后的 SQL 使用不同 interrupt 文案：

```text
上次执行结果疑似异常，系统已反思并生成修正后的 SQL。请确认是否执行修正后的 SQL？
```

- 新增 `POST /api/query/approve/stream`：
  - 审批后推送“已确认，正在执行 SQL...”
  - 推送“如果执行结果异常，系统会自动反思并生成修正 SQL...”
  - 若再次进入审批，推送“检测到执行结果异常，已完成反思并生成修正 SQL”
  - 最后通过 `result` 事件返回 `QueryResponse`
- 前端 SQL Agent 审批按钮改为读取 SSE，显示带动画的进度行，再渲染最终结果或修正 SQL 审批卡片。

**5. approve 恢复状态修复**

- SQL 审批中断时仍暂存原始 query。
- approve 恢复时只发送 `Command(resume=...)`：

```python
Command(resume={
    "approved": req.approved,
    "feedback": req.feedback,
})
```

- `sql_react` 子图不再返回 `query`，减少父图/子图状态合并时的重复写入。
- `FinalGraphState.query` 和 `SQLReactState.query` 增加 `Annotated[..., keep_existing_query]` reducer。父图和 SQL 子图在 approve/resume 后若同一步携带 `query`，保留已有原始 query，避免 LangGraph `INVALID_CONCURRENT_GRAPH_UPDATE`。
- approve 完成后仍通过 session preference 中暂存的 `_pending_query` 恢复原始问题，用于保存本轮 Q&A；不再把 query 写入 graph update。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/flow/sql_react.py` | `query_enhance` 改为 evidence 驱动；新增结果异常检测和 `result_reflection`；反思 SQL 直接走 `safety_check`；approve 文案区分修正 SQL |
| `agents/rag/retriever.py` | 业务知识召回增加 MySQL term/synonyms 兜底 |
| `agents/model/format_tool.py` | 新增 `normalize_sql_answer()`；format tool 内部也执行 SQL 清洗校验 |
| `agents/api/routers/query.py` | 新增 `/approve/stream`；approve 恢复只使用 `Command(resume=...)`；pending query 仅用于最终 Q&A 保存 |
| `agents/static/index.html` | SQL 审批改为 SSE 进度展示；反思后修正 SQL 显示明确状态 |
| `agents/flow/state.py` | 新增 `reflection_notice` 状态字段；`query` 增加 `keep_existing_query` reducer |
| `scripts/seed_business_knowledge.py` | 补充“净利润”业务知识及口语化同义词 |
| `tests/test_sql_react.py` | 覆盖 SQL formatter、空/NULL 结果异常、result_reflection 直接生成 SQL |
| `tests/test_final_api.py` | 覆盖 approve 恢复状态和 approve SSE 流式事件 |

### 优化效果

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| LLM query_enhance 空响应 | 退回原 query | 使用召回到的业务知识做确定性增强 |
| 业务同义词召回失败 | 只能依赖向量/BM25 | MySQL `term/synonyms` 兜底召回 |
| SQL 带异常 token | 可能进入审批/执行 | formatter 清理或拦截 |
| SQL 尾部截断 | 可能执行失败或报错不清晰 | 本地识别为 invalid SQL |
| SQL 执行返回 `[]` / `NULL` | 直接结束 | 进入 `result_reflection` 生成修正 SQL |
| 反思后 SQL | 先给建议再进 `sql_generate` | 直接生成修正 SQL，走安全检查和审批 |
| 二次审批体验 | 用户只看到又弹出 SQL | SSE 展示执行、异常检测、反思、修正 SQL 确认 |
| approve 恢复缺 query | 报 `KeyError: 'query'` | approve resume 不依赖写回 query；最终保存从 `_pending_query` 取原始问题 |
| 重复写 query | 触发 LangGraph 并发写错误 | 子图不返回 `query`，且 `query` 字段使用 reducer 保留已有值 |

---

## Iteration 22：追问场景 SQL 口径继承

### 出现了什么问题

连续追问时，第二轮 SQL 可能和第一轮 SQL 口径不一致。例如第一轮问“去年亏损”，第二轮问“亏损多少”：

- 第一轮可能使用 `je.status = '已过账'`
- 第二轮可能改成 `je.status IN ('已审核','已过账')`
- 第一轮可能用简单借贷发生额差额
- 第二轮可能改用 `balance_direction` 公式
- 第一轮字段别名是“净利润/盈亏状态”
- 第二轮又回到“去年净利润”

这些 SQL 都可能能执行，但语义口径已经漂移，用户看到的结果会互相矛盾。

### 根因

- 外层 `classify_intent` 读取了 session history，但 `dispatcher.sql_react` 只把 `query` 和 `rewritten_query` 传给 SQL React 子图，没有传 `chat_history`。
- SQL 执行完成后只保存了自然语言 answer，没有保存上一轮 SQL 的时间范围、状态过滤、JOIN、指标公式、排除条件等口径信息。
- 后续追问进入 `sql_generate` 时缺少上一轮 SQL 上下文，LLM 会重新推断口径，因此发生状态过滤和公式漂移。

### 解决方案

**1. 保存最近一次 SQL 口径**

SQL 执行完成后，将以下内容写入 session preference 的 `_last_sql_context`：

```text
用户问题
生成 SQL
展示结果
```

这不是对业务问题硬编码，而是保存当前会话中已经确认执行过的 SQL 口径。

**2. 加载会话时注入 SQL 上下文**

`_load_chat_history()` 会把 `_last_sql_context` 注入为 system message：

```text
[上一轮SQL上下文]
用户问题: ...
生成SQL:
...
展示结果: ...
```

**3. SQL 子图接收 chat_history**

`dispatcher.sql_react` 调用 SQL React 子图时传入 `chat_history`，让 SQL 生成节点能看到上一轮 SQL 口径。

**4. Prompt 明确追问继承规则**

`sql_generate` prompt 增加规则：

- 如果上下文中提供了上一轮 SQL，且用户是在追问或省略表达，必须沿用上一轮的时间范围、状态过滤、表连接、指标计算口径和排除条件。
- 除非用户明确要求变更口径。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/api/routers/query.py` | 保存 `_last_sql_context`；加载 history 时注入上一轮 SQL 上下文 |
| `agents/flow/dispatcher.py` | 调用 SQL React 子图时传入 `chat_history` |
| `agents/flow/sql_react.py` | SQL 生成 prompt 加入追问继承规则；把上一轮 SQL 上下文放入生成上下文 |
| `tests/test_final_api.py` | 覆盖 SQL 上下文保存与加载 |
| `tests/test_sql_react.py` | 覆盖 prompt 约束和 SQL 上下文注入 |

### 优化效果

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| “去年亏损”后追问“亏损多少” | 第二轮重新推断 SQL 口径 | 第二轮沿用上一轮时间范围、状态过滤和指标公式 |
| 状态过滤 | 可能从 `已过账` 漂移到 `已审核/已过账` | 默认继承上一轮状态过滤 |
| 指标公式 | 可能换公式 | 默认继承上一轮指标计算口径 |
| 用户明确改口径 | 不确定 | 用户明确要求时允许变更 |

---

## Iteration 23：上一年度亏损测试数据补齐

### 出现了什么问题

用户执行“去年亏损”相关 SQL 时，最终结果仍然是 0：

- `net_profit = 0.00`
- `profit_status = 不盈不亏`
- `loss_amount = 0`

这不是 SQL 执行异常，而是测试数据缺口。

### 根因

- `scripts.seed_financial` 只生成最近 6 个月数据，按当前日期查询上一年度时可能没有完整年度数据。
- 随机记账凭证只从前 15 个科目里抽样，里面没有 `6001`、`6401`、`5401` 等损益类科目。
- 因此按 `status = '已过账'`、`account_type = '损益'`、上一年度期间过滤时，收入、成本、费用都可能没有发生额，结果自然为 0。

### 解决方案

在 `scripts.seed_financial` 中补充可重复刷新的上一年度亏损场景：

- 每次 seed 先删除同年度 `LOSS-YYYY-*` 测试凭证，避免重复累加。
- 重新插入上一年度 12 个月已过账凭证。
- 每月包含主营业务收入、主营业务成本、期间费用三类分录。
- 每张凭证借贷平衡，损益科目与银行存款科目配平。
- 年度合计保证成本和费用大于收入，使净利润为负、亏损金额为正。

这是测试数据造数，不是运行时 query 规则硬编码。SQL Agent 仍然通过 schema、语义模型、业务知识和对话上下文生成 SQL。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `scripts/seed_financial.py` | 新增 `seed_prior_year_loss_scenario()`，写入稳定的上一年度亏损凭证 |
| `README.md` | 初始化脚本教程说明 `seed_financial` 包含可重复刷新的亏损测试数据 |

### 验证口径

执行 `python -m scripts.seed_financial` 后，用上一年度、已过账、损益类科目过滤，收入减成本减费用应返回负数；“亏损多少”应返回正的亏损金额。

---

## Iteration 24：清理遗留 mysql_schema 向量索引

### 出现了什么问题

Milvus 中仍然存在 `source=mysql_schema` 的历史 schema 文档。当前 SQL Agent 的候选表选择和 schema 加载已经改为 Redis/MySQL：

- 候选表：`load_full_table_metadata()`，优先 Redis，miss 后查 MySQL `information_schema.tables`
- 字段 schema：`get_semantic_model_by_tables()`，优先 Redis，miss 后查 MySQL `t_semantic_model`

因此这些旧向量记录不再参与 SQL 生成，但会造成维护和排查上的混淆。

### 解决方案

- `scripts.seed_financial` 不再执行 schema re-index。
- `scripts.seed_all` 不再执行 `schema_indexer.index_mysql_schemas()`。
- 新增 `scripts.cleanup_schema_indexes`，一次性删除 Milvus/ES 中 `source=mysql_schema` 的历史记录。
- README 初始化脚本说明同步更新：schema 数据统一由 MySQL/Redis 提供，Milvus/ES 只保留业务知识、SQL few-shot 和用户文档等非结构化检索数据。

### 检索分工

| 数据类型 | 权威来源/缓存 | 检索方式 | 原因 |
|----------|---------------|----------|------|
| 表名、表注释 | MySQL `information_schema.tables`；Redis `schema:table_metadata` 缓存 | 精确加载全量表元数据，再让 LLM 精选候选表 | 表元数据必须完整实时，向量召回可能漏表 |
| 字段 schema、业务名、同义词、字段描述 | MySQL `t_semantic_model`；Redis `schema:semantic_model:<table>` 缓存 | 按选中表精确加载 | SQL 生成需要精确字段、类型、PK/FK 和业务描述 |
| 表关系/JOIN 关系 | `information_schema.key_column_usage` + `t_semantic_model` 逻辑外键 | 按表名精确查询 | JOIN 条件不能靠语义相似度猜测 |
| 业务知识 | MySQL `t_business_knowledge` + Milvus `business_knowledge` + ES `business_knowledge` | Milvus 向量 + ES BM25 + RRF；MySQL term/synonyms 字符匹配兜底 | 业务表达有同义词和口语化说法，需要语义召回和关键词召回结合 |
| SQL few-shot | MySQL `t_agent_knowledge` + Milvus `agent_knowledge` + ES `agent_knowledge` | Milvus 向量 + ES BM25 + RRF，过滤无 SQL 内容 | 用相似问题和关键词命中补充 SQL 写法示例 |
| 用户上传文档 | Milvus/ES 文档索引 | 向量/关键词检索 + 重排 | 文档是非结构化文本，适合 RAG 检索 |
| 会话、checkpoint、schema 缓存、领域摘要缓存 | Redis | key-value 精确读写 | 高频状态数据需要低延迟缓存 |
| 业务明细数据 | MySQL 业务表 | 审批后的 SELECT SQL 执行 | MySQL 是业务事实数据的权威来源 |

### 涉及文件

| 文件 | 改动 |
|------|------|
| `scripts/seed_financial.py` | 去掉 schema re-index 调用 |
| `scripts/seed_all.py` | 去掉第 5 步 schema re-index |
| `scripts/cleanup_schema_indexes.py` | 新增历史 `mysql_schema` 索引清理脚本 |
| `README.md` | 更新 seed 流程和清理脚本说明 |

---

## Iteration 25：旧 schema 向量索引入口降级

### 出现了什么问题

Iteration 24 已经停止 seed 流程重建 `source=mysql_schema` 数据，但代码里仍有两个容易误用的入口：

- `agents.rag.schema_indexer.index_mysql_schemas()` 仍可直接把 MySQL schema 写入 Milvus/ES。
- `agents.eval.dataset_generator` 仍从 Milvus `source=mysql_schema` 生成评测数据。

这会让新架构和旧评测/维护脚本出现口径不一致：线上 NL2SQL 从 Redis/MySQL 读 schema，但评测和手工脚本仍可能依赖旧向量记录。

### 解决方案

- 新增 `agents/rag/domain_summary_builder.py`，领域摘要改为基于 Redis/MySQL 语义模型生成，不再依赖 `schema_indexer`。
- `agents/api/app.py` 启动时调用新的领域摘要生成模块。
- `schema_indexer.index_mysql_schemas()` 默认禁用，只有显式设置 `ENABLE_LEGACY_SCHEMA_INDEX=1` 才会运行旧版 schema 向量索引。
- `agents/eval/dataset_generator.py` 改为从 MySQL `t_semantic_model` 生成评测数据，不再查询 Milvus `source=mysql_schema`。
- README 和技术设计文档标明 `schema_indexer` 是 legacy 兼容入口，当前 schema 权威来源是 MySQL/Redis。

### 影响

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| app 启动生成领域摘要 | 间接复用 `schema_indexer.generate_domain_summary` | 使用 `domain_summary_builder`，来源统一为语义模型 |
| 手工调用 `index_mysql_schemas()` | 默认写入 Milvus/ES `mysql_schema` | 默认返回 disabled，避免误建旧索引 |
| 评测数据生成 | 依赖 Milvus 旧 schema 文档 | 依赖 MySQL `t_semantic_model` |
| 架构说明 | schema_indexer 容易被理解为当前链路 | 明确为旧版兼容入口 |

---

## Iteration 26：评测报告与页面可视化

### 背景

生产环境需要用量化指标指导迭代，而不是只靠单条 case 观察。当前已有 `agents.eval` CLI，但报告偏命令行，缺少：

- 可视化展示
- per-query 回溯明细
- 业界常用指标的统一口径
- 首字延迟等端到端指标的扩展位置

### 解决方案

- `agents.eval.metrics` 新增 `accuracy@K`、`precision@K`，保留 `recall@K`、`MRR`、`NDCG@K`。
- 新增 `agents.eval.reporting`，统一报告 JSON 格式，包含：
  - `run_id`
  - `created_at`
  - `dataset_path`
  - 策略级指标
  - 平均/P50/P95 延迟
  - 首字延迟预留字段
  - per-query 明细
- `agents.eval.runner` 输出新的可回溯报告格式。
- 新增 `/api/eval/reports`、`/api/eval/reports/latest`。
- 前端新增 `Evaluation` tab，展示最佳策略、Accuracy@5、Recall@5、P95 延迟、策略对比和 query 明细。
- 新增 `docs/evaluation_design.md` 说明指标、报告格式、API、页面和后续端到端评测计划。

### 后续

低优先级可接入 LLM 对失败样本给优化建议，但建议只作为辅助分析，不自动改生产配置。

---

## Iteration 27：线上预选链路评测补齐

### 背景

Iteration 26 先补齐了报告格式和页面，但默认评测仍偏 schema metadata 基线。实际 NL2SQL 流程里，选表前还有两类关键输入：

- `business_knowledge_recall`：召回业务术语、公式、口径。
- `agent_knowledge_recall`：召回 SQL few-shot 示例。

这些证据会进入 `query_enhance`，再由 `select_tables` 选择表。单独评测本地 schema metadata 有价值，但不能完整回答“线上这条 query 经过证据召回和增强后，最终能不能选对表”。

### 解决方案

- 新增 `agents.eval.strategies`：
  - `run_preselect_pipeline(query)`：执行 `recall_evidence -> query_enhance -> select_tables`，输出 `schema_<table_name>`。
  - `run_business_knowledge_recall(query)`：只评测业务知识召回结果。
  - `run_agent_knowledge_recall(query)`：只评测 SQL few-shot 召回结果。
- `agents.eval.runner` 支持策略直接返回 `retrieved_doc_ids`，并继续复用统一指标计算。
- 默认评测策略扩展为：
  - `schema_lexical`
  - `schema_table_name`
  - `business_knowledge_recall`
  - `agent_knowledge_recall`
- `preselect_pipeline` 支持通过 `--include-online-pipeline` 显式开启。
- 对业务知识和 Agent 知识召回采用独立标注字段：
  - `relevant_business_doc_ids`
  - `relevant_agent_doc_ids`

### 设计取舍

`business_knowledge_recall` 和 `agent_knowledge_recall` 发生在选表之前，它们召回的是公式、业务定义和 few-shot 示例，不是 schema 表。因此 runner 不会拿它们和 `relevant_doc_ids` 强行对比；只有数据集显式包含对应标注字段时才计算指标，没有标注时报告显示 `num_queries = 0`。

`preselect_pipeline` 才是线上表选择链路评测：它消费前置证据和 query 增强结果，最后把 `select_tables` 产出的表名转为 `schema_<table_name>`，再和 `relevant_doc_ids` 对比。由于该链路会调用 LLM 节点，默认不启用，避免普通本地评测消耗 token；需要真实线上链路指标时显式加 `--include-online-pipeline`。

### 验证

新增单元测试覆盖：

- 预选链路按 `recall_evidence -> query_enhance -> select_tables` 顺序执行。
- 业务知识召回只使用 `relevant_business_doc_ids` 计算。
- Agent 知识召回只使用 `relevant_agent_doc_ids` 计算。
- 没有对应标注的数据集行会跳过，不污染指标。
- 默认 `run_evaluation` 报告包含新增策略。

本地验证命令：

```bash
pytest tests/test_eval_pipeline_strategies.py tests/test_eval_runner_schema.py tests/test_eval_metrics.py tests/test_eval_reporting.py tests/test_eval_dataset_generator.py -q
```

结果：`39 passed`。完整回归 `tests/test_eval_pipeline_strategies.py tests/test_eval_runner_schema.py tests/test_eval_metrics.py tests/test_eval_reporting.py tests/test_eval_dataset_generator.py tests/test_imports.py tests/test_api.py` 为 `76 passed`。

---

## Iteration 28：知识召回标注与 NL2SQL 离线端到端评测

### 背景

Iteration 27 已经能分别评测 schema、业务知识和 Agent few-shot 召回，但数据集默认只有 `relevant_doc_ids`，导致业务知识和 few-shot 策略经常显示 `num_queries = 0`。同时生产环境最终关心的不只是召回，还包括 SQL 是否规范、是否执行成功、执行结果是否符合预期、端到端延迟和首字延迟。

### 解决方案

- `generate` 默认基于本地知识表补充可选标注：
  - `relevant_business_doc_ids`
  - `relevant_agent_doc_ids`
- 知识标注不调用 LLM：
  - 业务知识用 `term` / `synonyms` 命中 query。
  - Agent few-shot 用 query 与 `question` / `description` / `category` 词法重叠匹配。
- 增加 `--no-knowledge-labels`，必要时只生成 schema 表标注。
- 新增 `agents.eval.nl2sql_runner` 和 CLI：
  - `python -m agents.eval.cli run-nl2sql --dataset ... --output ...`
- NL2SQL 离线报告包含：
  - `sql_valid`
  - `execution_success`
  - `result_exact_match`
  - P50/P95 延迟
  - 首字延迟
  - per-query 明细
- 前端 Evaluation 页面支持：
  - retrieval 报告按策略切换 query 明细。
  - NL2SQL 端到端报告独立展示核心指标。

### 设计取舍

`run-nl2sql` 默认只评测已记录样本，不调用 Agent，不执行数据库。这保证本地 TDD 和 CI 不依赖外部 LLM token、MySQL 数据状态或人工审批。后续接生产回放时，只需要把线上生成的 `generated_sql`、`actual_result`、`latency_ms`、`first_token_latency_ms` 写成 JSONL，即可复用同一套报告和页面。

### 验证

```bash
pytest -q
```

结果：`234 passed`。测试过程中 LangSmith 网络上报失败为本地网络限制，不影响测试结论。

---

## Iteration 29：评测报告历史回溯

### 背景

Evaluation 页面只能读取 latest 报告，不方便对比和回溯历史评测。生产迭代时需要能查看某一次 run 的完整报告，否则指标变化无法定位到具体数据集和策略结果。

### 解决方案

- `GET /api/eval/reports` 返回每个报告的 `name`，作为前端选择历史报告的稳定标识。
- 新增 `GET /api/eval/reports/{name}`，只允许读取已发现报告文件名，拒绝路径穿越。
- Evaluation 页面增加报告下拉菜单：
  - 默认展示最新报告。
  - 可切换历史 retrieval 或 NL2SQL 端到端报告。
  - 切换后保留原有策略明细和 NL2SQL 明细展示能力。

### 验证

新增 API 测试覆盖：

- 报告列表按更新时间返回，并包含 `name`。
- 可按报告文件名读取完整报告。
- 路径穿越请求返回 404。

完整回归：

```bash
pytest -q
```

结果：`236 passed`。LangSmith 网络上报失败为本地网络限制，不影响测试结论。

---

## Iteration 30：NL2SQL 离线评测 CLI 体验修复

### 背景

`run-nl2sql` 需要读取已经记录好的回放样本，但文档示例里的 `data/eval/nl2sql_cases.jsonl` 不一定存在。直接运行会触发 `FileNotFoundError` traceback，用户无法判断是命令错误、代码错误，还是缺少输入样本。

### 解决方案

- `run-nl2sql` 在 dataset 缺失时输出明确提示并以退出码 2 结束，不再展示 Python traceback。
- 新增 `--init-template`，可生成一份 JSONL 样本模板：

```bash
python -m agents.eval.cli run-nl2sql \
  --dataset data/eval/nl2sql_cases.jsonl \
  --init-template
```

- README 和评测设计文档改为先初始化模板，再填写真实 `generated_sql` / `actual_result` / `expected_result`，最后运行评测。

### 验证

- 缺失 dataset 场景返回可读错误和模板生成命令。
- `--init-template` 可生成 JSONL 模板。
- 使用模板可成功生成 `nl2sql_eval_report.json`。

---

## Iteration 31：链路追踪子调用细化

### 背景

LangSmith / CozeLoop 能看到 LangGraph 节点，但部分节点内部的 LLM 调用、Milvus 向量检索、Elasticsearch BM25、Redis/MySQL 元数据加载没有形成子 span。排查问题时只能看到节点耗时，无法判断时间花在 LLM、向量库、关键词检索还是 schema 元数据读取。

### 解决方案

- 新增 tracing helper：
  - `child_trace_config`：从 LangGraph config 继承 callbacks，传给内部 LLM/Runnable。
  - `traced_retriever_call`：把裸 Milvus/ES 检索包装成 retriever span。
  - `traced_tool_call` / `traced_async_tool_call`：把 Redis/MySQL 等非 Runnable IO 包装成 tool span。
- SQL React 细化：
  - `contextualize_query`
  - `recall_evidence`
  - `query_enhance`
  - `select_tables`
  - `sql_retrieve`
  - `sql_generate`
  - `error_analysis`
  - `result_reflection`
- RAG Chat / Dispatcher / Analyst 细化：
  - query rewrite、retrieve、chat LLM、intent classify、domain summary load、analyst report LLM。
- 业务知识和 Agent 知识召回细化：
  - Milvus vector search
  - ES BM25 search
  - MySQL lexical fallback

### 验证

新增/更新测试覆盖 callbacks 继承和手动 span 触发：

- 内部 LLM 调用会收到 graph callbacks。
- knowledge retriever 会收到 graph callbacks。
- `traced_retriever_call` 会触发 retriever start/end。
- `traced_tool_call` 会触发 tool start/end。

---

## Iteration 32：在线 NL2SQL 评测 Runner 与微调方案

### 背景

离线 `run-nl2sql` 只能评测已记录的 SQL 和结果，不会调用真实 Agent，也不会覆盖审批中断、SQL 执行、异常结果反思等线上链路。生产迭代需要能用同一批 query 回放当前 Agent，并把生成 SQL、执行结果、延迟和失败原因写成可回溯报告。

同时，后续准备微调 SQL 生成模型，需要明确模型选择、样本来源和数据飞轮，避免只拿公开 Text-to-SQL 数据训练，导致财务口径和项目 schema 不稳定。

### 解决方案

- 新增 `agents.eval.online_nl2sql_runner`：
  - `run_online_nl2sql_case`：单条 query 真实调用 LangGraph Agent。
  - `run_online_nl2sql_evaluation_async`：批量回放 JSONL 数据集并写报告。
  - `write_online_nl2sql_template`：生成在线评测模板。
- 新增 CLI：

```bash
python -m agents.eval.cli run-online-nl2sql \
  --dataset data/eval/online_nl2sql_cases.jsonl \
  --output data/eval/online_nl2sql_eval_report.json
```

- 默认停在 SQL 审批中断，只记录生成 SQL 和首次响应延迟。
- 增加 `--auto-approve-sql`，可在测试库中自动恢复审批中断并执行 SQL，覆盖结果反思后的二次 SQL。
- 增加 `--full-dispatch`，可选择是否把意图分类也纳入评测。
- 新增 `docs/evaluation_user_guide.md`，整理已完成评测能力的使用手册。
- 新增 `docs/sql_finetuning_plan.md`：
  - 推荐 `Qwen/Qwen2.5-Coder-7B-Instruct` 作为 7B code 基座。
  - 明确公开数据只做泛化补充，主数据来自本项目 schema、业务知识、few-shot、线上日志、失败修正和人工黄金集。

### 验证

- 在线 Runner 单测覆盖：
  - 自动审批后可进入执行结果。
  - 不自动审批时停在 `pending_approval`。
  - 批量评测会写 `online_nl2sql_end_to_end` 报告。
  - 模板命令可生成 JSONL。

---

## Iteration 33：修复跨轮 Query 被 Checkpoint 旧状态污染

### 背景

同一前端会话连续提问时，`thread_id` 直接使用 `session_id`，LangGraph checkpoint 会把上一轮 SQL 图状态带到下一轮。例如用户新问“第一季度员工工资”，`classify_intent.llm` 已返回：

```json
{"intent": "sql_query", "rewritten_query": "我们公司第一季度的员工工资情况"}
```

但进入 `route_intent` / `sql_react` 时，状态里的 `query` 仍可能是上一轮“我们公司去年亏损”，导致后续 SQL 全部沿用旧问题。

### 原因

- `FinalGraphState.query` 使用的 reducer 是“已有就保留”，新一轮输入无法覆盖旧 checkpoint query。
- `FinalGraphState` 没声明 `rewritten_query`，前端预分类传入的 rewritten query 可能无法稳定进入主图状态。
- graph checkpoint 以会话为粒度复用，上一轮 `sql`、`result`、`answer` 等状态也存在污染下一轮的风险。

### 解决方案

- 将 query reducer 改为 `latest_non_empty`：新输入覆盖旧值，approve resume 没有新值时保留当前值。
- `SQLReactState.rewritten_query` 和 `FinalGraphState.rewritten_query` 使用同样 reducer，保证预分类结果可传入子图。
- `/api/query/invoke` 每个新 query 生成独立 graph thread id：

```text
{session_id}:turn:{uuid}
```

- SQL 审批中断时暂存 `_pending_thread_id`，`approve` 使用该 thread 恢复图执行。
- 聊天历史仍然通过 session store 维护，不再依赖 LangGraph checkpoint 跨轮保存业务状态。

### 验证

- 新增 dispatcher 回归：同一 `thread_id` 连续两次输入不同 query，第二次进入 SQL 子图时必须使用新 query 和新 rewritten query。
- 新增 API 回归：`invoke` 使用单轮 graph thread，`approve` 使用 pending graph thread。
- 完整回归：

```bash
.venv/bin/python -m pytest -q
```

结果：`259 passed`。

---

## Iteration 34：公司财务查数意图防误判

> 后续已由 Iteration 36 替换为“数据库可配置规则 + LLM + 仲裁”的通用方案，避免把业务关键词写死在代码中。

### 背景

用户问“去年亏损”时，`classify_intent.llm` 在有历史对话干扰的情况下返回：

```json
{"intent": "chat", "rewritten_query": "我们公司去年是否亏损"}
```

但该问题属于当前企业财务数据库可回答的结构化查数问题，应该进入 `sql_query`。历史中曾出现“参考知识中未提供...”这类 RAG 回答，会误导 LLM 把本公司财务数据问题当成普通 chat/knowledge。

### 解决方案

- 在 `classify_intent` 解析 LLM 输出后增加 deterministic guard：
  - 当前 query 或 rewritten query 命中本公司/企业财务数据特征时，强制归为 `sql_query`。
  - 覆盖关键词包括亏损、盈利、利润、收入、成本、费用、工资、薪酬、余额、发生额、预算、报销、发票、应收应付、凭证、资产、折旧等。
  - 时间口径包括去年、今年、季度、本月、上月、具体年份/期间等。
- 明确排除外部公开公司问题，例如“贵州茅台去年亏损情况”，避免把公开知识问题强行路由到本地 SQL。

### 验证

- 新增回归：LLM 返回 `chat` 且 rewritten query 为“我们公司去年是否亏损”时，最终 intent 必须是 `sql_query`。
- 新增回归：外部公司“去年贵州茅台的亏损情况”仍可保持 `chat`。
- 完整回归：

```bash
.venv/bin/python -m pytest -q
```

结果：`262 passed`。

---

## Iteration 35：审批后 SQL 执行失败自动修复

### 背景

用户审批 SQL 后，执行阶段可能出现由 LLM 生成 SQL 导致的错误，例如：

```text
Error: Unknown column 'a.account_type' in 'field list'
```

这类错误通常是 SQL 作用域、字段别名、子查询外层引用内层表别名、嵌套聚合等生成问题，应该进入自动修复流程，而不是直接把执行失败返回给前端。

### 解决方案

- 新增 `_should_repair_sql_error`：
  - `Unknown column`
  - `SQL syntax`
  - `Invalid use of group function`
  - `42S02` / `42S22` / `42000`
  - `1054` / `1064` / `1111`
  - `ambiguous`、`GROUP BY`、子查询返回列数等常见 SQL 生成错误
- 权限、认证、密码、连接等不可由 SQL 改写修复的错误不进入 LLM 修复。
- `route_after_execute` 遇到可修复 SQL 错误时进入：

```text
execute_sql -> error_analysis -> sql_generate -> safety_check -> approve
```

- `sql_generate` prompt 增加约束：
  - 不要在同一层 SELECT 中嵌套聚合函数。
  - 外层查询不能引用内层表别名，只能引用子查询输出列。
- 二次审批文案改为：

```text
上次 SQL 执行失败，系统已分析错误并生成修正后的 SQL。请确认是否执行修正后的 SQL？
```

- SSE 进度文案同步覆盖“执行失败或结果异常”两类自动修复。

### 验证

- `Unknown column 'a.account_type' in 'field list'` 被判定为可修复。
- 权限错误不会进入 LLM SQL 修复。
- 修复后 SQL 的 approve interrupt 使用用户友好的执行失败修正文案。

---

## Iteration 36：意图规则配置化与 LLM 仲裁

### 背景

Iteration 34 为了解决“去年亏损”被历史 RAG 回答误导成 `chat`，在 `dispatcher.py` 中加入了本公司、时间、财务指标、外部公司等关键词 guard。该方式能解决单个问题，但会带来两个明显风险：

- 业务词、主体词和时间词写在代码里，后续换查询会不断增加 hardcode。
- 外部主体问题可能被“时间 + 财务词”误判为本地 SQL 查询。

### 解决方案

- 移除 `dispatcher.py` 中的业务关键词常量和 deterministic keyword guard。
- 新增 `t_intent_rule`，规则字段包括 `target_intent`、`match_type`、`pattern`、`rewrite_template`、`priority`、`confidence`、`enabled`。
- 新增 `agents.tool.storage.intent_rules`：
  - 只负责规则表 DDL、CRUD、匹配算法。
  - 不内置任何业务关键词或公开公司名单。
  - MySQL 不可用时返回空规则，不影响 LLM 意图识别。
- 新增 `data/intent_rules_seed.json` 和 `scripts.seed_intent_rules`，用于把默认规则作为数据写入 MySQL，而不是写在 `dispatcher.py`。
- `classify_intent` 改为并行执行：

```text
用户问题
  ├─ LLM 意图识别 + 查询重写
  └─ 数据库规则引擎匹配
        ↓
      仲裁器
        ↓
  intent + rewritten_query
```

- 仲裁策略：
  - 没有规则命中时，使用 LLM 意图。
  - 有规则命中且目标意图合法时，使用规则意图。
  - 有规则命中且配置了 `rewrite_template` 时，用数据里的模板补齐查询主体，例如把“第一季度毛利率”补齐为“公司第一季度毛利率”。
  - 规则内容由 Admin 页面维护，而不是写入代码。
- Admin 页面新增意图规则维护入口，可新增、编辑、删除、启停规则和维护重写模板。
- 移除“只有 intent 没有 rewritten_query 时兼容旧版跳过分类”的分支；只有同时传入 `intent` 和 `rewritten_query` 才视为前端已完成预分类，避免旧状态或旧客户端把后续问题强行路由到错误意图。

### 验证

- 新增回归：无规则命中时，LLM 返回 `chat` 的问题保持 `chat`。
- 新增回归：规则引擎可通过数据库规则把 LLM 的 `chat` 仲裁为 `sql_query`。
- 新增回归：规则引擎可通过 `rewrite_template` 把“第一季度毛利率”重写为“公司第一季度毛利率”。
- 新增回归：只有 `intent` 没有 `rewritten_query` 时必须重新走当前轮 LLM 分类。
- 新增 API 回归：`/api/admin/intent-rules` 支持列表和保存。
- 局部回归：

```bash
.venv/bin/python -m pytest tests/test_dispatcher.py tests/test_intent_rules.py tests/test_api.py -q
```

结果：`19 passed`。

---

## Iteration 37：分层记忆体系细化

### 背景

原记忆实现把历史、摘要、SQL 上下文都放在 session 里，读取时容易把过多旧消息直接塞给 LLM。这样有三个问题：

- 短期上下文太粗，旧问题容易干扰当前意图识别。
- 中期摘要只在 RAG Chat 后台压缩，SQL Query 路径追加历史后不会触发摘要。
- 长期历史没有进入向量库，压缩掉的对话无法按语义回溯。

### 解决方案

- 短期记忆：`_load_chat_history` 只注入最近 `MEMORY_SHORT_WINDOW_MESSAGES` 条消息，SQL 上下文和摘要仍以 system message 形式置顶。
- 中期记忆：新增 `agents.tool.memory.manager`，统一维护 session 记忆；SQL Query 与 RAG Chat 完成后都会异步触发压缩。
- 长期记忆：新增 `agents.tool.memory.vector_store`，把被压缩归档的旧消息写入 Milvus，`source=conversation_memory`，并按 `session_id` 隔离。
- 召回策略：只有 session 标记了 `_has_long_term_memory` 时才按当前 query 检索长期记忆，避免新会话每轮都访问向量库。
- 配置项：
  - `MEMORY_SHORT_WINDOW_MESSAGES`
  - `MEMORY_SUMMARY_MAX_HISTORY_LEN`
  - `MEMORY_SUMMARY_KEEP_RECENT`
  - `MEMORY_LONG_TERM_TOP_K`
  - `MEMORY_ENABLE_LONG_TERM_VECTOR`

### 验证

- 新增回归：加载 chat history 时只保留短期滑动窗口。
- 新增回归：`compress_session` 返回被归档消息，便于长期记忆索引。
- 新增回归：memory manager 压缩后会把归档消息写入长期向量记忆接口。
- 新增回归：存在长期记忆标记时，加载 chat history 会按 query 注入 `[长期记忆]`。

```bash
.venv/bin/python -m pytest tests/test_memory.py tests/test_final_api.py::TestSessionSqlContext -q
```

结果：`13 passed`。扩展回归：

```bash
.venv/bin/python -m pytest tests/test_memory.py tests/test_final_api.py tests/test_rag_flow.py tests/test_token_counter.py tests/test_imports.py tests/test_api.py -q
```

结果：`84 passed`。

---

## Iteration 38：管理表选表召回与逻辑外键补全

### 背景

管理类查询（用户、角色、部门、用户角色绑定、用户部门归属）在 `select_tables` 阶段长期排不进 Top5。典型问题包括：

- “查询所有用户的真实姓名以及他们被分配的角色名称”需要 `t_user + t_user_role + t_role`。
- “查询公司各部门的负责人姓名以及对应的部门名称”需要 `t_department + t_user_department + t_user`。
- “查询所有拥有财务审核角色的用户分别属于哪个部门”需要 `t_user_role + t_role + t_user + t_user_department + t_department`。

优化前管理表专项评测表现为：管理表能在 Top10 附近出现，但经常被财务核心表挤到 6-10 位，导致 `Recall@5 = 0%`，`Recall@10 = 96.67%`。这说明问题不是“完全找不到表”，而是排序和关联补表链路不稳定。

### 根因

1. **业务知识 evidence 污染选表**
   `business_knowledge` 中的 `related_tables` 会无条件并入候选结果。即使当前 query 没命中该业务术语，也可能把财务表推到管理表前面。

2. **LLM 输出顺序被直接信任**
   `select_tables` 让 LLM 从全量表名/表描述中精选表，但后续没有用本地语义模型做稳定重排。管理表虽然被选中，仍可能排在 Top5 之外。

3. **桥接表和端点表缺失**
   多表查询经常需要关系表，例如 `t_user_department`、`t_user_role`。如果 LLM 只选了两个端点表，SQL 生成阶段就缺少 JOIN 桥接表；如果 LLM 只选了绑定表，又可能缺少被引用的端点实体表。

4. **表关系只读物理外键**
   `get_table_relationships` 只查 `information_schema.key_column_usage`。当前业务库很多关系是逻辑外键，已维护在 `t_semantic_model.is_fk/ref_table/ref_column` 中，但没有注入给 SQL 生成。

5. **管理表缺少“表级可见语义”**
   财务核心表天然有更丰富的字段名、业务名和描述，LLM 更容易判断；管理表如果只有英文表名或弱表注释，就会输给财务表。

### 解决方案

#### 1. 管理表语义补齐

在 `scripts.seed_semantic_model` 中补充管理表的表级描述和字段级业务语义：

- `t_user`：用户/员工账号信息，真实姓名、联系电话、邮箱、注册时间、账号状态。
- `t_role`：系统角色信息，角色名称、角色编码、角色状态。
- `t_user_role`：用户角色绑定关系。
- `t_department`：组织部门信息，部门名称、上级部门、部门负责人、联系电话、状态。
- `t_user_department`：用户部门归属关系，是否部门负责人。

同时补充逻辑外键：

- `t_user_role.user_id -> t_user.id`
- `t_user_role.role_id -> t_role.id`
- `t_user_department.user_id -> t_user.id`
- `t_user_department.department_id -> t_department.id`
- `t_department.parent_id -> t_department.id`
- `t_cost_center.department_id -> t_department.id`
- `t_expense_claim.department_id -> t_department.id`

seed 脚本会清理 Redis `schema:table_metadata` 缓存，避免旧表注释继续影响 `select_tables`。

#### 2. 业务 evidence 改为 query-aware

`_related_tables_from_business_evidence` 不再无条件使用 `related_tables`。只有当前 query 命中 evidence 的 `term` 或 `synonyms` 时，才把对应关联表合并进选表结果。

这样可以保留“净利润/亏损/预算”等业务知识对财务查询的帮助，同时避免未命中的财务术语把管理表 query 污染成财务表优先。

#### 3. select_tables 增加本地语义重排

`select_tables` 在 LLM 精选后加载候选表的 `t_semantic_model`，基于以下信息做本地重排：

- 表名
- 表注释
- 字段名
- 字段注释
- `business_name`
- `synonyms`
- `business_description`

匹配策略不是写死“用户/角色/部门”等业务关键词，而是读取数据库中的语义模型。短语命中（例如“真实姓名”“角色名称”“部门负责人”）权重高于孤立字符重叠。

#### 4. 基于语义外键补桥接表和端点表

新增 `_expand_selected_tables_by_semantic_relationships`：

- 如果已选关系表，则补齐其引用的端点表。
- 如果两个已选端点表被某个未选桥接表同时引用，则补齐桥接表。

示例：

```text
t_department + t_user
  -> 根据 t_user_department.department_id/user_id
  -> 自动补 t_user_department

t_user_role + t_user_department + t_role
  -> 根据逻辑外键
  -> 自动补 t_user、t_department
```

该逻辑仍然是数据驱动的：只依赖 `t_semantic_model` 中的 `is_fk/ref_table/ref_column`，不是运行时代码硬编码业务表名。

#### 5. 表关系读取合并逻辑外键

`get_table_relationships` 现在合并两类来源：

- 物理外键：`information_schema.key_column_usage`
- 逻辑外键：`t_semantic_model.is_fk/ref_table/ref_column`

如果逻辑外键查询失败，会降级保留已查到的物理外键，避免老库或不完整环境直接丢失关系信息。

#### 6. online 评测初始化模型注册

`preselect_pipeline` 是线上选表前置链路评测，会真实执行：

```text
recall_evidence -> query_enhance -> select_tables
```

该链路会调用 embedding、ES/Milvus、`query_enhance` LLM 和 `select_tables` LLM。评测入口补充 `init_chat_models()`，避免直接运行 CLI 时模型注册未初始化。

### 评测结果

使用管理表专项数据集：

```bash
.venv/bin/python -m agents.eval.cli run \
  --dataset data/eval/management_eval_dataset.jsonl \
  --output data/eval/management_preselect_report.json \
  --include-online-pipeline
```

`preselect_pipeline` 线上链路结果：

| 指标 | 结果 |
| --- | ---: |
| 样本数 | 12 |
| MRR | 1.0000 |
| Accuracy@5 | 1.0000 |
| Recall@5 | 1.0000 |
| NDCG@5 | 1.0000 |
| 平均延迟 | 8389.2 ms |
| P50 延迟 | 6575.3 ms |
| P95 延迟 | 10593.5 ms |

典型 Top5 结果：

| Query | Top5 召回 |
| --- | --- |
| 查询所有用户的真实姓名以及他们被分配的角色名称 | `t_role, t_user, t_user_role` |
| 查询公司各部门的负责人姓名以及对应的部门名称 | `t_department, t_user_department, t_user` |
| 查询所有拥有财务审核角色的用户分别属于哪个部门 | `t_user_role, t_department, t_user_department, t_role, t_user` |

该 `Recall@5=100%` 是管理表专项评测结果，不能直接等同于全量业务评测结果。随后使用全量 `eval_dataset.jsonl` 重跑线上预选链路：

```bash
.venv/bin/python -m agents.eval.cli run \
  --dataset data/eval/eval_dataset.jsonl \
  --output data/eval/eval_report.json \
  --include-online-pipeline
```

全量评测结果（45 条，2026-05-13）：

| 策略 | MRR | Accuracy@5 | Precision@5 | Recall@5 | NDCG@5 | 平均延迟 | P50 延迟 | P95 延迟 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `schema_lexical` | 96.67% | 77.78% | 35.56% | 90.63% | 90.03% | 0.1 ms | 0.0 ms | 0.1 ms |
| `preselect_pipeline` | 96.67% | 88.89% | 84.93% | 94.07% | 94.13% | 7545.6 ms | 7426.8 ms | 10541.4 ms |

结论：管理表专项已经修复到 Top5 全召回；全量线上预选链路 `Recall@5` 从旧报告的 67.59% 提升到 94.07%，但还未超过 95%。剩余缺口需要继续从失败明细中定位是标注粒度、query_enhance 扩表，还是 `select_tables` 过度/不足补表。

### 验证

新增/更新回归：

- 未命中的业务 knowledge 不再把财务表塞进管理表 query。
- 本地语义重排能把管理表排到 LLM 原始输出前面。
- 已选端点表能通过语义外键补桥接表。
- 已选关系表能补齐被引用端点表。
- `get_table_relationships` 能合并物理外键与 `t_semantic_model` 逻辑外键。
- 逻辑外键查询失败时保留物理外键降级。
- 兼容 `pymysql.fetchall()` 返回 tuple 的情况，避免逻辑外键合并时报 `'tuple' object has no attribute 'extend'`。
- `preselect_pipeline` CLI 运行前初始化 chat model registry。

局部回归：

```bash
.venv/bin/python -m pytest \
  tests/test_seed_semantic_model.py \
  tests/test_sql_react.py::TestSelectTables \
  tests/test_eval_pipeline_strategies.py \
  tests/test_retriever_relationships.py
```

结果：`17 passed`。

格式检查：

```bash
git diff --check
```

结果：通过。

## Iteration 39：复杂多表查询的计划模式

### 背景

`select_tables` 的 Top5 仍然适合作为召回评测指标，因为绝大多数 NL2SQL 问题只需要少量核心表。但真实线上问题暴露出另一个难点：有些 query 表面上会召回较多表，真正的风险不在“表多”，而在任务是否能用一条稳定 SQL 表达。

典型失败 case：

```text
Query: 收入成本预算回款费用之间的关系
```

这个问题横跨凭证、预算、费用报销、应收应付、发票和组织维度。直接交给单个 `sql_generate` 容易出现三类问题：

1. **口径混杂**：收入、成本、预算、回款、费用不是同一事实表的字段，强行一条 SQL 会把不同粒度的指标拼在一起。
2. **JOIN 幻觉**：LLM 容易在错误表别名上引用字段，或补出不存在的日期/金额字段。
3. **合并不可审计**：即使 SQL 执行成功，也很难解释每个指标来自哪个步骤、按什么维度合并。

因此复杂查询不能靠“选中了几张表”判断。表数量只保留为观测指标，真正的路由依据是任务类型、关系图结构和计划自洽性。

### 方案取舍

#### 方案 A：遇到复杂查询直接要求用户缩小范围

优点：

- 风险低，不会生成不可读的大 SQL。
- 适合明细导出、敏感字段、权限范围不清的请求。

缺点：

- 对真实分析问题不友好。
- 用户已经表达了分析目标时，系统仍要求用户手动拆问题，Agent 价值有限。

#### 方案 B：进入 Planner，多步查询后合并

优点：

- 可以把多个业务口径拆成独立 SQL 步骤，每步都有明确目标和表范围。
- 可以用 `python_merge` 做确定性合并，不让 LLM 硬编结论。
- 计划先给用户确认，执行过程保留每步 SQL、结果和错误，便于审计。

缺点：

- 需要计划校验、依赖管理、合并键治理和失败恢复。
- 如果没有稳定 merge key，不能强行合并，必须降级为摘要或澄清。

最终选择方案 B，但加严格约束：只有规则引擎标注为 `analysis/report/comparison` 的可拆任务，或结构上可安全执行的请求才进入对应模式；`detail/export/sensitive` 先澄清。

### 当前路由策略

当前实现不再使用表数阈值，也不再调用 LLM 仲裁复杂路由。

```text
recall_evidence
-> query_enhance
-> select_tables
-> assess_feasibility
-> single_sql / single_sql_with_strict_checks / complex_plan / clarify
```

`assess_feasibility` 的输入：

- DB 规则引擎给出的 `task_type`：`analysis | report | comparison | detail | export | sensitive | ambiguous`
- `selected_tables`：选表节点产出的候选执行表
- `table_relationships`：物理外键 + 逻辑外键形成的关系图
- 关系图特征：是否连通、是否存在多条 JOIN 路径、估算 JOIN 数

决策原则：

- `analysis/report/comparison`：进入 `complex_plan`，因为这类问题通常需要多个口径分步计算再合并。
- `detail/export/sensitive`：进入 `clarify`，避免自动生成大范围明细查询或敏感数据查询。
- 未命中规则且关系图断开：进入 `clarify`。
- 未命中规则且关系图连通但存在多条 JOIN 路径：进入 `single_sql_with_strict_checks`。
- 未命中规则且关系图连通、路径简单：进入 `single_sql`。

Planner 需要输出结构化计划：

```json
{
  "mode": "complex_plan",
  "reason": "涉及多个业务域，需要按公共维度拆分为多个可执行 SQL 子任务",
  "steps": [
    {
      "step": 1,
      "type": "sql",
      "goal": "查询 2025 年各月收入和成本费用",
      "tables": ["t_journal_entry", "t_journal_item", "t_account"],
      "depends_on": [],
      "merge_keys": ["period"]
    },
    {
      "step": 2,
      "type": "sql",
      "goal": "查询 2025 年各部门预算执行情况",
      "tables": ["t_budget", "t_cost_center", "t_department"],
      "depends_on": [],
      "merge_keys": ["period", "department_id"]
    },
    {
      "step": 3,
      "type": "python_merge",
      "goal": "按期间和部门合并结果，生成综合分析",
      "depends_on": [1, 2],
      "merge_keys": ["period", "department_id"]
    }
  ],
  "requires_user_confirmation": true
}
```

### 需要澄清或拒绝自动拆分的场景

以下场景不应该直接让 LLM 自动拆分执行：

- 用户要的是强一致明细表，必须跨很多表精确 JOIN。
- 子任务之间没有稳定 join key。
- 用户问题过宽泛，例如“分析公司所有经营情况”。
- 涉及工资、个人信息、银行账号等敏感字段。
- 需要事务一致性或同一时间点快照，但系统无法保证。
- Planner 无法说明每一步的表、目标、依赖和合并键。

这类请求应该返回澄清问题，或者先给前端一个计划预览，让用户确认范围后再执行。

### 迭代开发拆分

#### Task 1：可行性评估节点

目标：在 `select_tables` 之后增加 `assess_feasibility`，不改变普通 SQL 生成行为。

输出：

- `execution_mode`: `single_sql | single_sql_with_strict_checks | complex_plan | clarify`
- `task_type`: 规则引擎给出的任务类型证据
- `join_risk`: `low | medium | high`
- `selected_tables_count`、`relationship_count`、`estimated_join_count`：仅作为观测指标
- `reason`: 可解释路由原因

验证：

- 财务核心表普通查询仍走 `single_sql`。
- 关系图断开的请求走 `clarify`。
- 存在多条 JOIN 路径但仍是单目标查询时走 `single_sql_with_strict_checks`。
- 命中 `analysis/report/comparison` 规则时走 `complex_plan`，与表数量无关。
- 命中 `detail/export/sensitive` 规则时走 `clarify`。
- `assess_feasibility` 不调用 LLM。

#### Task 2：Complex Query Planner 节点

目标：新增计划节点，只生成结构化计划，不执行 SQL。

计划必须包含：

- 每步 `type`: `sql | python_merge | report`
- 每步 `goal`
- 每步 `tables`
- 每步 `depends_on`
- 每步 `merge_keys`
- 是否需要用户确认

约束：

- 按业务目标和可稳定合并的公共维度拆分 SQL 步骤，不按表数量机械拆分。
- 无法给出 merge key 时必须转 `clarify` 或降级为摘要。
- 涉及敏感字段时必须转 `clarify` 或要求权限确认。

#### Task 3：计划校验与用户确认

目标：防止 LLM 生成不可执行或不可审计的计划。

校验规则：

- 所有表必须来自当前 schema 候选。
- `depends_on` 必须引用已存在步骤。
- `python_merge` 必须至少依赖一个 SQL 步骤。
- 有多个 SQL 步骤需要合并时必须提供 `merge_keys`。
- 步骤数设置上限，例如 5 步。

前端/SSE 展示：

```text
检测到这是复杂多表分析问题，系统已拆分为 3 个步骤：
1. 查询收入和成本费用
2. 查询预算执行情况
3. 按期间和部门合并分析
请确认是否按该计划执行？
```

#### Task 4：多 SQL 执行与结果聚合

目标：复用现有 `sql_generate -> safety_check -> approve -> execute_sql`，逐步执行计划。

执行策略：

- 无依赖 SQL 步骤可以并行，但初期建议先串行，降低状态复杂度。
- 每个 SQL step 都必须经过 `safety_check`，继续只允许安全 `SELECT/WITH`；计划已经在 `approve_complex_plan` 统一确认，当前实现不再对每个 step 二次 interrupt，避免复杂计划审批与普通 SQL 审批状态互相污染。
- 每步结果写入 `plan_execution_results[step_id]`。
- `python_merge` 或本地聚合节点只消费已完成步骤结果。

本次实现：

- `execute_complex_plan_step` 从“计划确认占位”推进为串行执行器。
- 对每个 `sql` step 构造独立临时 state，只暴露该 step 的目标、表集合和表内关系，复用 `sql_retrieve -> check_docs -> sql_generate -> safety_check -> execute_sql`。
- 执行结果按 step id 写入 `plan_execution_results`，并在最终 answer 中展示每步目标、SQL、结果摘要或错误。
- `python_merge` step 先实现保守本地合并：当依赖 SQL 返回 JSON 行数组且包含 `merge_keys` 时按 key 做外连接合并；无法结构化合并时不让 LLM 硬编结论，而是保留依赖步骤摘要。
- `report` step 当前生成依赖步骤摘要，后续可扩展为 LLM 报告生成节点。
- 任一步生成危险 SQL 或执行失败时立即停止后续步骤，返回 `complex_plan_step_failed` 和已完成步骤明细。

#### Task 5：评测与回归

新增复杂查询专项数据集：

- 关系图连通、目标明确、应走单 SQL 的样本。
- 关系图连通但存在多条 JOIN 路径、应走严格单 SQL 的样本。
- 任务类型为 analysis/report/comparison、应走复杂计划的样本。
- 关系图断开、明细导出或敏感范围过大、应该澄清的样本。

指标：

- route accuracy：路由模式是否正确。
- plan validity：计划结构是否合法。
- step success rate：每步执行成功率。
- final answer correctness：最终回答是否符合预期。
- latency P50/P95：复杂计划整体耗时。

### 当前结论

Top5 仍然适合作为默认选表和评测指标，因为绝大多数 NL2SQL 问题不应依赖很多表。

表数量不应作为拒绝阈值或计划模式切换阈值；它只能作为观测指标。真正的切换依据是任务类型、关系图连通性、JOIN 多路径风险和计划自洽性。

复杂多表查询的关键不是“给 LLM 更多表”，而是“让 Agent 先规划、再分步执行、最后可审计地合并结果”。

### 开发计划链接

详细 TDD 拆分见：[Complex Query Planner Implementation Plan](superpowers/plans/2026-05-14-complex-query-planner.md)。

## Iteration 40：轻量表路由画像与链式补表优化

### 背景

全量线上预选链路中，“查询各个部门的年度预算总金额”暴露出两个问题：

- `select_tables` prompt 只给表名和表注释，LLM 看到“预算”后容易只选 `t_budget`，看不到 `t_budget.cost_center_id` 同义词“部门”、`t_cost_center.annual_budget` 业务名“年度预算”等字段级证据。
- 语义外键补表是一轮扫描，`t_budget -> t_cost_center -> t_department` 这类多跳关系可能受字典顺序或缓存状态影响，补表不稳定。

同时不能把完整 schema/字段列表全部放进 `select_tables`，否则会把 SQL 生成阶段的 token 压力提前到选表阶段。

### 方案

- 新增轻量 table routing profile：每张候选表只展示表说明，以及与当前 query 命中的少量字段提示。
- 字段提示由 `t_semantic_model` 的 `business_name / column_comment / synonyms / ref_table` 动态计算，不在代码里写业务关键词。
- 字段提示只用于选表；完整字段类型和完整 schema 仍然只在 `sql_retrieve -> sql_generate` 阶段按已选表加载。
- 语义外键补表改成闭包式扩展，并结合 query 相关性判断，避免只补一跳或过度补无关外键。
- `scripts.seed_semantic_model` 清理 Redis `schema:table_metadata` 与 `schema:semantic_model:*` 缓存，避免逻辑外键和表注释更新后线上仍读旧缓存。

### 实际案例

Query：

```text
查询各个部门的年度预算总金额
```

优化前 `select_tables` 只给：

```text
- t_budget: 预算管理表
- t_cost_center: 成本中心表
- t_department: 组织部门信息表，包含部门名称
```

优化后会给轻量画像：

```text
- t_budget: 预算管理表 | 匹配字段: budget_amount(预算金额/预算额度), budget_year(预算年度), cost_center_id(成本中心ID/部门/-> t_cost_center.id)
- t_cost_center: 成本中心表 | 匹配字段: annual_budget(年度预算/全年预算), department_id(关联部门ID/-> t_department.id), center_code(成本中心编码/部门编码)
- t_department: 组织部门信息表，包含部门名称 | 匹配字段: name(部门名称/组织名称/部门), manager(部门负责人/负责人/部门经理)
```

如果 LLM 仍然只返回 `t_budget`，链式补表会继续补出：

```text
t_budget -> t_cost_center -> t_department
```

这样 Top5 不再依赖 LLM 一次性选全维表。

### 验证

- 新增测试覆盖链式外键补表不依赖字典顺序。
- 新增测试覆盖 `select_tables` 只暴露命中的轻量字段画像，不包含无关字段如 `created_at`。
- 本地评测样本 `data/eval/eval_dataset.jsonl` 中该 case 的人工标注已补充 `schema_t_budget`，因为“年度预算总金额”本身需要预算事实表或明确的预算主数据口径。

## Iteration 41：单次召回的 recall_context 与选表语义复用

### 背景

Iteration 40 中轻量字段画像仍然依赖 `_ranking_terms()` 的中文 n-gram 词面匹配。它能解决“部门/年度预算”这类字面命中问题，但会产生噪声片段，例如“的年”“门的年”，也不能理解“人员成本”和“工资薪酬”这类同义表达。

系统已经有 `recall_evidence` 节点，并且它位于 `select_tables` 之前：

```text
recall_evidence -> query_enhance -> select_tables
```

因此不应在 `_ranking_terms()` 或 `select_tables` 内重复召回。正确做法是：每轮 query 只执行一次召回，把结构化结果放入 LangGraph state/checkpoint，后续节点全部复用。

### 方案

`recall_evidence` 节点升级为“召回 + 结构化整理”：

```python
{
  "evidence": [...],
  "few_shot_examples": [...],
  "recall_context": {
    "query_key": rewritten_query,
    "business_evidence": [...],
    "few_shot_examples": [...],
    "business_related_tables": ["t_budget", "t_cost_center"],
    "few_shot_related_tables": ["t_budget", "t_cost_center"],
    "matched_terms": ["年度预算", "部门费用"],
    "few_shot_questions": ["查询各部门年度预算总额"]
  }
}
```

约束：

- 每个用户问题只调用一次 `recall_evidence`。
- `query_enhance`、`select_tables`、`sql_generate` 只读 state 中的 `recall_context`，不再触发召回。
- `recall_context.query_key` 必须等于当前 `rewritten_query/query`，否则视为旧 checkpoint 污染并忽略。
- `select_tables` 使用 `business_related_tables` 和 `few_shot_related_tables` 给候选表加权，并把这些表合并进候选结果。
- 字段打分优先使用 `recall_context.matched_terms` 这类业务词，再 fallback 到 `_ranking_terms()`，降低 n-gram 噪声。

### 实施结果

- 召回链路只跑一次，降低重复 IO 和外部检索开销。
- select-table 阶段能使用业务知识和 few-shot 的表证据，不再只靠表注释和字段 n-gram。
- checkpoint/resume 后仍可复用同一份召回结果，同时通过 `query_key` 避免跨轮污染。
- LangSmith/Codzloop 中能看到召回证据如何影响选表，链路更可解释。

本次实现不再兼容旧的“`select_tables` 从原始 `evidence` 字符串里临时解析相关表”的路径。选表阶段的语义来源统一为 `recall_context`：

- `recall_evidence` 是唯一召回节点。
- `select_tables` 只读取 `recall_context`，不再触发召回。
- `recall_context.query_key` 不匹配当前 `rewritten_query/query` 时直接忽略，避免 checkpoint 污染。
- 原始 `evidence` 和 `few_shot_examples` 继续保留给 `query_enhance/sql_generate` prompt 使用，但不作为选表语义加权的兼容入口。

### TDD 验证

1. 新增 `recall_context` 状态字段。
2. 测试 `recall_evidence` 产出结构化 `recall_context`，并从业务知识和 few-shot 中抽取相关表。
3. 测试 `select_tables` 在已有 `recall_context` 时不会调用召回，只复用相关表和 matched terms。
4. 测试 `query_key` 不一致时忽略旧 `recall_context`。
5. 复测第 12 条“查询各个部门的年度预算总金额”的 Top5 召回。

已执行的回归测试：

```bash
.venv/bin/python -m pytest tests/test_sql_react.py -q
# 70 passed

.venv/bin/python -m pytest tests/test_sql_react.py tests/test_seed_semantic_model.py tests/test_eval_dataset_generator.py -q
# 79 passed
```

重点样本复测：

```text
Query: 查询各个部门的年度预算总金额
recall_context:
  business_related_tables: [t_cost_center]
  few_shot_related_tables: [t_budget, t_cost_center]
  matched_terms: [年度预算, 部门]
selected_tables:
  [t_cost_center, t_budget, t_department]
Accuracy@3 = 1.0
Recall@3 = 1.0
Precision@3 = 1.0
Accuracy@5 = 1.0
Recall@5 = 1.0
Precision@5 = 1.0
```

## Iteration 43：recall_context 相关表污染修复

### 问题

用户问题：

```text
查询当前公司去年的（净利润 < 0 时的 ABS(净利润)，即亏损金额），关联 t_journal_item、t_account、t_expense_claim 表
```

`sql.select_tables.llm` 返回：

```text
t_journal_item,t_account,t_expense_claim
```

但后续 `schema.get_table_relationships` 收到：

```text
t_expense_claim,t_budget,t_journal_item,t_journal_entry,t_account,t_fund_transfer,
t_receivable_payable,t_cost_center,t_fixed_asset,t_department
```

这说明问题不在 LLM 选表，而在 LLM 选表后的本地合并/补表阶段。

### 根因

`recall_context` 初版会把召回到的所有业务知识 `关联表` 都写入 `business_related_tables`。RAG 召回可能同时带出预算、资金、应收应付、固定资产等相近财务知识；这些知识虽然被召回，但并不一定命中当前用户问题。

随后 `select_tables` 会把 `business_related_tables` 与 LLM 选表结果合并，再调用 `get_table_relationships(selected)`，导致关系查询入参从 3 张表膨胀到 10 张表。

### 修复

- `_build_recall_context()` 只收录当前 query 命中 `term/synonyms` 的业务知识相关表。
- 未命中的业务知识仍保留在原始 `evidence` 中，供 `query_enhance/sql_generate` 参考，但不参与选表合并。
- few-shot 表证据也只从与当前 query 有足够词面重叠的示例中抽取，避免相似度召回噪声污染选表。
- 不引入版本兼容逻辑；如果线上已有旧 checkpoint/cache 污染，部署后清空 checkpoint/cache。

### TDD 验证

新增两个回归：

1. `recall_evidence` 召回多个业务知识时，只把 query 命中的“净利润/亏损金额”相关表写入 `recall_context.business_related_tables`。
2. 清洗后的 `recall_context` 进入 `select_tables` 后，`get_table_relationships` 只接收 LLM 选出的 3 张表，不再扩散到预算、资金、应收应付、固定资产等表。

已执行：

```bash
.venv/bin/python -m pytest tests/test_sql_react.py tests/test_dispatcher.py tests/test_final_api.py tests/test_eval_pipeline_strategies.py -q
# 108 passed, 16 warnings
```

## Iteration 44：复杂查询路由从表数阈值升级为可行性评估

### 问题

复杂查询初版通过 `infer_route_signal -> route_complexity` 判断是否进入 `complex_plan`。实际测试：

```text
Query: 收入成本预算回款费用之间的关系
selected_tables:
  [t_budget, t_expense_claim, t_journal_item, t_cost_center,
   t_journal_entry, t_receivable_payable, t_account, t_department]
table_relationships: 10
```

旧逻辑把“选中表数量”当作主要判断依据，直接决定走单 SQL、严格单 SQL 或复杂计划。随后普通 SQL 生成出现跨域口径混杂和字段幻觉风险，例如在不合适的表别名上引用时间字段。

单纯把阈值从 8 调到 5 可以让当前 case 触发，但本质仍是调参：下一个 6 张表的明细查询可能不该拆，4 张表的跨指标分析也可能需要计划模式。

### 方案

参考 DataAgent 的设计思想：`SchemaRecall/TableRelation` 之后，不直接按表数路由，而是先做 `FeasibilityAssessment`，再进入 Planner。

本项目改为：

```text
recall_evidence
-> query_enhance
-> select_tables
-> assess_feasibility
-> single_sql / single_sql_with_strict_checks / complex_plan / clarify
```

关键决策：

- 顶层 `intent` 仍只判断是否进入 SQL 子图。
- SQL 子图内部不再使用 LLM 仲裁复杂路由。
- `t_query_route_rule.route_signal` 保留为兼容字段名，但运行时解释为 `task_type` 证据。
- 最终图分支只看 `feasibility_decision.execution_mode`。
- 表数量只保留为观测指标，不再作为路由阈值或计划校验阈值。
- 未命中任务规则时，默认看关系图是否连通；断开的 schema 进入 `clarify`，连通且无多路径风险走 `single_sql`，存在多条 JOIN 路径时走 `single_sql_with_strict_checks`。

`feasibility_decision` 结构：

```json
{
  "execution_mode": "single_sql | single_sql_with_strict_checks | complex_plan | clarify",
  "task_type": "analysis | report | comparison | detail | export | sensitive | ambiguous",
  "can_single_sql": true,
  "can_decompose": false,
  "needs_clarification": false,
  "join_risk": "low | medium | high",
  "decision_source": "rules | default",
  "selected_tables_count": 8,
  "relationship_count": 10,
  "estimated_join_count": 7,
  "reason": "..."
}
```

### 实现

- 新增 `assess_query_feasibility()`，基于任务类型、关系图连通性和 JOIN 路径风险计算执行模式。
- 新增 `assess_feasibility` LangGraph 节点。
- 移除 SQL 子图中的 `infer_route_signal` LLM 仲裁节点和 `route_complexity` 节点。
- `analysis/report/comparison` 由规则引擎明确标注为可拆任务后进入 `complex_plan`，不再依赖表数触发。
- `detail/export/sensitive` 进入 `clarify`，避免自动生成大范围明细 JOIN。
- 未命中规则时，关系图断开则 `clarify`；关系图连通且存在多条 JOIN 路径则走 `single_sql_with_strict_checks`；普通连通图走 `single_sql`。
- `validate_complex_plan()` 不再因为单个 SQL step 使用了多少张表而拒绝，只校验表白名单、步骤依赖、`merge_keys` 和步骤类型。

### 当前复杂计划案例拆解

以 README GIF 中的 query 为例：

```text
收入成本预算回款费用之间的关系
```

这条问题不是单纯“查某个指标”，而是让系统解释多类经营指标之间的关系。收入/成本来自凭证分录和会计科目，预算来自预算表，费用可能来自报销表，回款可能来自应收应付或资金相关表。这些数据天然不是同一张事实表，也不一定处在同一粒度。如果强行让 `sql_generate` 产出一条大 SQL，容易把不同粒度指标直接 JOIN 到一起，产生重复计数、字段幻觉或不可解释的结果。

当前链路的执行过程是：

1. `classify_intent` 先把问题路由到 `sql_query`。省略主体的情况由意图规则补齐为当前公司口径。
2. `recall_evidence` 只执行一次，召回业务术语、公式和 few-shot，并沉淀为 `recall_context`。
3. `select_tables` 只给 LLM 表说明和少量命中字段提示，选出候选业务表，再通过逻辑外键补齐必要桥接表。
4. `assess_feasibility` 读取 `t_query_route_rule` 的任务类型证据。如果规则命中 `analysis`，说明这是可拆分析任务，直接产出 `execution_mode=complex_plan`，不再按表数量阈值切换。
5. `complex_plan_generate` 只生成计划，不生成 SQL。计划必须声明每一步的 `type`、`goal`、`tables`、`depends_on` 和 `merge_keys`。
6. `validate_complex_plan` 校验计划：表必须在本轮候选表白名单内，依赖只能指向已出现的步骤，`python_merge` 必须有 `merge_keys`，步骤数量不能超过上限。
7. `approve_complex_plan` 把计划预览交给用户确认。
8. `execute_complex_plan_step` 串行推进步骤。`sql` 步骤复用 `sql_retrieve -> check_docs -> sql_generate -> safety_check -> execute_sql`，本地步骤由 Python 执行，不再调用 LLM 硬编结论。

一个符合当前实现约束的计划形态如下，实际步骤会随 Planner 输出和候选表变化，但语义结构一致：

```json
{
  "mode": "complex_plan",
  "steps": [
    {
      "step": 1,
      "type": "sql",
      "goal": "按会计期间统计收入、成本费用和净利润",
      "tables": ["t_journal_entry", "t_journal_item", "t_account"],
      "depends_on": [],
      "merge_keys": ["period"]
    },
    {
      "step": 2,
      "type": "sql",
      "goal": "按会计期间统计预算金额和实际金额",
      "tables": ["t_budget", "t_cost_center", "t_department"],
      "depends_on": [],
      "merge_keys": ["period"]
    },
    {
      "step": 3,
      "type": "sql",
      "goal": "按会计期间统计费用、回款或应收应付结算情况",
      "tables": ["t_expense_claim", "t_receivable_payable", "t_fund_transfer"],
      "depends_on": [],
      "merge_keys": ["period"]
    },
    {
      "step": 4,
      "type": "python_merge",
      "goal": "按会计期间合并收入成本、预算、费用和回款指标",
      "tables": [],
      "depends_on": [1, 2, 3],
      "merge_keys": ["period"]
    },
    {
      "step": 5,
      "type": "report",
      "goal": "基于合并结果输出关系分析摘要",
      "tables": [],
      "depends_on": [4],
      "merge_keys": ["period"]
    }
  ],
  "requires_user_confirmation": true
}
```

这里的 `merge_keys` 不是数据库 JOIN 外键，而是“步骤结果之间的行对齐键”。本例选择 `period`，原因是收入、成本、预算、费用、回款都可以按月或会计期间聚合，`period` 是跨步骤最稳定的公共维度。如果某个计划要求分析部门维度，则 `merge_keys` 会升级为类似 `["period", "department_id"]` 或 `["period", "cost_center_id"]`。这时每个依赖 SQL 步骤必须在 SELECT 中输出同名别名；无法输出 ID/编码时，至少要输出同一业务维度的名称别名。

`python_merge` 的合并逻辑是确定性的：

1. `_parse_rows_from_sql_result()` 把每个依赖 SQL 的执行结果解析为 JSON 行数组。
2. `_resolve_merge_key_column()` 先找完全同名的列，例如 `period`。
3. 如果列名不完全一致，会做保守归一化匹配，例如去掉大小写、空格、下划线等符号。
4. 如果计划键是 `department_id`、`cost_center_id` 这类 ID/编码，而某个步骤只输出了 `department_name`、`cost_center_name`，`_resolve_merge_label_column()` 会用通用后缀规则把名称列作为可读维度兜底。
5. `_merge_dependency_rows()` 用 `merge_keys` 的取值组成 tuple key，按 key 做外连接式合并。不同步骤返回同名非 key 字段且值不一致时，会加上 `step{n}_` 前缀保留冲突字段，避免覆盖。
6. 如果依赖结果不是结构化行数组，或某行缺少合并键，系统不会让 LLM 猜结果，而是返回依赖步骤摘要。

`report` 当前也是本地步骤，不调用 LLM。它通过 `_dependency_summary()` 汇总依赖步骤的 SQL、结果摘要或错误信息，作用是把执行链路收束成可展示的最终说明。后续如果要升级为 LLM 报告生成，也应该只消费已经结构化的 `python_merge` 结果，并保留指标来源，不允许重新发明口径。

当前步骤没有并发执行，是刻意的工程取舍：

- `python_merge` 和 `report` 天然依赖前置步骤，必须等依赖 SQL 完成后才能运行。
- 每个 SQL 步骤内部还包含 schema 加载、SQL 生成、安全检查、MCP 执行和可修复错误重试；并发执行会让 `sql/result/error/retry_count/execution_history` 等状态更难隔离。
- 复杂计划走过一次 `approve_complex_plan` 后统一执行，当前没有为每个 step 设计独立 interrupt 和审批恢复上下文，贸然并发会增加 LangGraph checkpoint 恢复和状态合并风险。
- `plan_execution_results` 是按 step id 逐步写入的审计结果。串行执行可以保证最终 answer 的顺序、失败短路和已完成步骤都稳定可解释。

后续可以做有限并发，但前提是按 DAG 拓扑层调度：同一层、无依赖的 `sql` step 才允许 `asyncio.gather` 并发；每个 step 必须使用独立临时 state、独立 retry 计数和独立执行历史；所有结果按 step id 汇总后，再执行 `python_merge/report`。这属于性能优化，不影响当前功能正确性。

### TDD 验证

新增/更新回归：

1. 连通关系图默认 `single_sql`。
2. 连通但存在多条 JOIN 路径时走 `single_sql_with_strict_checks`。
3. 命中 `analysis` 规则时走 `complex_plan`，与表数量无关。
4. 大范围 `detail/export/sensitive` 规则走 `clarify`。
5. `assess_feasibility` 不调用 LLM。
6. 复杂计划校验不再按 SQL step 表数量拒绝，只做白名单、依赖和 merge key 自洽校验。
7. Graph 节点包含 `assess_feasibility`，不再包含 `infer_route_signal/route_complexity`。

已执行：

```bash
.venv/bin/python -m pytest tests/test_complex_query.py tests/test_sql_react.py::TestComplexRoute tests/test_sql_react.py::TestBuildSqlReactGraph::test_graph_has_all_nodes tests/test_complex_query_eval.py -q
# 28 passed
```

## Iteration 45：数据权限前置、业务化结果展示与全链路审计

### 背景

当前 NL2SQL 链路已经具备意图路由、选表、SQL 生成、安全检查、人工审批、执行、自动修复和复杂计划能力，但企业级数据接入还缺少三个关键闭环：

1. **数据权限不能只靠 SQL 执行失败兜底**
   如果用户无权访问某类数据，系统不应该把无权限表静默过滤后继续让 LLM 猜 SQL。这样会导致两类问题：一是 LLM 找不到正确表后产生幻觉；二是用户无法理解为什么“同一个问题有时查不到”。正确做法是先识别问题需要的数据域，再明确告诉用户缺少哪个业务数据域的权限，并停止后续 SQL 链路。

2. **自动补表也必须经过权限门禁**
   当前 `sql_generate` 支持 `needs_more_tables=true` 后调用 `_retrieve_missing_tables()` 补表。如果只在 `select_tables` 后做权限检查，LLM 仍可能在 SQL 生成阶段要求补充一张无权限表，从而绕过前置门禁。因此补表入口必须复用同一套权限检查。

3. **用户可见结果不能暴露物理 schema**
   SQL 执行必须使用物理表名和字段名，但用户看到的结果、权限提示、记忆摘要和报告应该展示业务数据名称和业务字段名称。例如 `t_user.real_name` 应展示为“真实姓名”，`t_role.name` 应展示为“角色名称”。物理表名和字段名只应进入审计日志和管理员排障视图。

同时，企业环境还要求**全链路审计**：记录每一次自然语言提问、重写后的问题、AI 生成 SQL、审批状态、执行参数、结果行数、脱敏字段和错误信息。一旦发生敏感数据泄露，需要能定位到“哪个用户、什么时间、通过什么问题、拿到了哪些数据域和多少行结果”。

### 设计原则

- **先识别，再授权，不静默过滤**：`select_tables` 负责识别业务相关表；新增权限节点负责判定是否可访问。无权限时返回友好提示并结束链路。
- **用户提示用业务名，审计记录用物理名**：普通用户只看到“员工薪酬数据”“费用报销数据”等业务数据域；审计日志保留 `t_xxx.column` 级别证据。
- **权限是多道门，不是单点检查**：选表后、补表时、SQL 执行前都要检查。
- **SQL 授权兜底分阶段落地**：V1 在执行审批前提取 SQL 涉及表并做表级授权；后续再引入 SQL AST 解析、列级授权和行级条件注入。
- **结果出站统一治理**：V1 先将物理字段名映射为业务字段名；后续在同一出站层增加列级脱敏，再进入用户展示、记忆和报告。
- **审计默认不存明细值**：审计日志默认记录元数据、行数、字段、SQL hash、脱敏字段和错误，不保存完整敏感结果，避免审计库变成新的敏感数据源。

### 目标链路

普通单 SQL 链路升级为：

```text
classify_intent
-> recall_evidence
-> query_enhance
-> select_tables
-> authorize_selected_tables
-> assess_feasibility
-> sql_retrieve
-> check_docs
-> sql_generate
-> safety_check
-> authorize_sql
-> approve
-> execute_sql
-> format_result_for_user
```

权限拒绝分支统一写入审计：

```text
authorize_selected_tables / authorize_missing_tables / authorize_sql
-> write_audit_log(permission_denied)
-> END
```

`sql_generate` 的补表分支升级为：

```text
LLM returns needs_more_tables + missing_tables
-> authorize_missing_tables
-> retrieve_missing_tables
-> continue sql_generate
```

复杂计划链路升级为：

```text
complex_plan_generate
-> validate_complex_plan
-> authorize_complex_plan_tables
-> approve_complex_plan
-> execute_complex_plan_step
   -> per-step safety_check
   -> per-step authorize_sql
   -> per-step execute_sql
   -> per-step format_result_for_user
```

### 权限模型

新增运行时 `SecurityContext`，由 API 层或登录态注入到 LangGraph state：

```json
{
  "user_id": "u_001",
  "username": "alice",
  "role_ids": ["finance_manager"],
  "department_ids": [10, 12],
  "company_id": 1,
  "data_scopes": {
    "department": [10, 12],
    "cost_center": [1001, 1002],
    "owner_user": ["u_001"]
  }
}
```

语义模型和权限策略拆开维护：

| 层级 | 建议表 | 作用 |
|------|--------|------|
| 表/数据域权限 | `t_role_table_permission` | 定义角色可访问哪些业务数据域或物理表 |
| 字段权限 | `t_role_column_permission` | 定义字段可见、拒绝、脱敏策略 |
| 行级策略 | `t_role_row_policy` | 定义某角色在某表上的行过滤模板 |
| 敏感字段策略 | `t_sensitive_column_policy` | 定义手机号、邮箱、身份证、薪酬等字段的敏感级别和 mask 策略 |
| 审计日志 | `t_query_audit_log` | 记录每次自然语言查询和 SQL 执行链路 |

`t_semantic_model` 继续作为 schema 与业务语义权威源，维护字段业务名、同义词、字段描述和逻辑外键；权限策略表只引用其 `table_name/column_name`，不把业务语义重复写一份。

### 表级权限交互

`select_tables` 仍然从全部可见 schema 元数据中识别“问题需要哪些表”，但不直接把无权限表过滤掉后继续执行。新增 `authorize_selected_tables`：

```text
selected_tables = [t_user, t_user_role, t_role]
unauthorized_tables = [t_user]
```

普通用户可见回答：

```text
当前问题需要访问「用户/员工账号信息」相关数据，但你暂无该数据权限。
请联系管理员开通后再查询。
```

审计日志记录：

```json
{
  "event": "table_permission_denied",
  "physical_tables": ["t_user"],
  "display_tables": ["用户/员工账号信息"],
  "query": "查询所有用户的真实姓名以及他们被分配的角色名称",
  "user_id": "u_001"
}
```

这里不建议对普通用户暴露 `t_user`、`t_user_role` 等物理表名。物理信息仅用于管理员审计和排障。

### 补表权限交互

当前 `sql_generate` 在 LLM 返回 `needs_more_tables=true` 时会调用 `_retrieve_missing_tables(missing, ...)`。该入口必须改为：

```text
authorize_missing_tables(missing_tables)
  if denied:
    return permission_denied answer and stop
  else:
    retrieve_missing_tables
```

示例：

```text
LLM: 现有表不足，需要补充 t_salary_detail
权限节点: 当前用户无权访问 t_salary_detail 对应的「员工薪酬明细」
用户提示: 当前查询需要访问「员工薪酬明细」数据，但你暂无权限。
```

这可以防止“选表阶段没选中无权限表，但 SQL 生成阶段又补出来”的绕过路径。

### SQL 执行前授权兜底

即使前面两道门通过，也必须在 `safety_check` 后新增 `authorize_sql`：

V1 已落地的是保守表级授权：

1. 从生成 SQL 的 `FROM/JOIN` 中提取真实访问表。
2. 如果解析不到表，回退到 `selected_tables` 做授权。
3. 使用同一套 `authorize_tables` 策略检查 `allowed_tables/denied_tables`。
4. 无权限时在进入人工审批前停止，返回业务数据域提示，并写入 `sql_permission_denied` 审计事件。

后续增强再引入 SQL AST：

1. 解析表、列、别名和子查询。
2. 校验列级权限，发现无权限列时拒绝或转脱敏。
3. 根据行级策略注入过滤条件，例如：

```sql
-- 原始 SQL
SELECT claim_no, total_amount FROM t_expense_claim;

-- 注入行级权限后
SELECT claim_no, total_amount
FROM t_expense_claim
WHERE department_id IN (10, 12);
```

4. 对解析失败、未知表、未知列、`SELECT *` 展开失败等情况默认拒绝，不交给 LLM 自行修复。

该节点与 `SQLSafetyChecker` 的职责不同：

| 节点 | 关注点 |
|------|--------|
| `safety_check` | SQL 是否危险，例如 DDL/DML、DROP、DELETE、权限变更 |
| `authorize_sql` | 当前用户是否有权访问 SQL 中涉及的表、列和行范围 |

### 结果业务化展示

新增 `format_result_for_user`，它不改变数据库执行结果，只改变用户可见结果：

```text
SQL 原始结果
-> physical column/table -> business name
-> answer / memory / report
```

字段映射来源：

```text
t_semantic_model.business_name
t_semantic_model.column_comment
t_semantic_model.business_description
```

例子：

```json
{
  "columns": ["real_name", "name"],
  "rows": [["张三", "财务审核"]]
}
```

在用户侧展示为：

```text
真实姓名：张三
角色名称：财务审核
```

对同名字段必须结合表或 SQL alias 判断，例如：

| 物理字段 | 所属表 | 展示名 |
|----------|--------|--------|
| `t_role.name` | 角色表 | 角色名称 |
| `t_department.name` | 部门表 | 部门名称 |
| `t_account.account_name` | 会计科目表 | 科目名称 |

因此 SQL 生成 prompt 仍要求 LLM 尽量使用业务别名：

```sql
SELECT u.real_name AS 真实姓名, r.name AS 角色名称
```

但最终不能依赖 LLM，必须通过结果后处理兜底。

### 脱敏与记忆边界

本轮 V1 已实现业务字段名替换，后续列级脱敏需要在以下动作之前完成：

- 用户展示
- `_summarize_sql_result`
- `[上一轮SQL上下文]` 的展示结果
- 摘要记忆
- 长期向量记忆
- 复杂计划 `python_merge/report`

这样可以保证后续多轮追问、记忆召回、报告生成都不会重新暴露未脱敏字段。当前版本先保证用户侧不直接暴露 `real_name/name` 这类物理字段名，手机号、邮箱、薪酬明细等敏感字段的具体 mask 策略放到下一阶段。

推荐策略：

| 策略 | 行为 |
|------|------|
| `allow` | 原样展示 |
| `mask` | 展示部分内容，例如手机号 `138****1234` |
| `aggregate_only` | 允许聚合结果，不允许明细 |
| `deny` | 直接拒绝查询 |

### 全链路审计

本轮新增 `write_audit_log` 的 best-effort 骨架，权限拒绝路径会记录审计事件，包括选表阶段拒绝、补表阶段拒绝和 SQL 执行前拒绝。该写入当前使用日志落地并保证 no-throw，不阻断用户请求。

完整生产版审计需要覆盖所有终态，包括成功、拒绝、用户未审批、SQL 安全拒绝、权限拒绝、执行失败和自动修复失败。

建议字段：

| 字段 | 说明 |
|------|------|
| `audit_id` | 审计 ID |
| `parent_audit_id` | 复杂计划父记录 |
| `step_id` | 复杂计划步骤 ID |
| `session_id` / `thread_id` | 会话与 LangGraph 线程 |
| `user_id` / `role_ids` | 用户与角色 |
| `query` / `rewritten_query` / `enhanced_query` | 原始问题与改写问题 |
| `intent` / `execution_mode` | 意图与执行模式 |
| `selected_tables` | 选中的物理表 |
| `display_tables` | 用户可见业务数据域 |
| `generated_sql` | LLM 生成 SQL |
| `authorized_sql` | 注入行级策略后的 SQL |
| `sql_hash` | SQL hash，便于去重和检索 |
| `approval_status` | 待审批、已通过、已拒绝 |
| `requested_columns` | SQL 涉及字段 |
| `denied_tables` / `denied_columns` | 被拒绝的数据 |
| `masked_columns` | 已脱敏字段 |
| `row_count` | 返回行数 |
| `result_schema` | 返回字段结构 |
| `status` / `error` | 执行状态与错误 |
| `latency_ms` | 总耗时 |
| `trace_id` | LangSmith/CozeLoop trace |
| `created_at` | 创建时间 |

审计日志默认不保存完整结果值。如需合规留样，只能保存已脱敏样例，并通过配置开关控制。

### 迭代开发拆分

#### Task 1：权限数据模型与 SecurityContext

- 已实现：在 API 请求进入 LangGraph 前构建 `SecurityContext`，支持从请求头读取 `x-user-id`、`x-role-ids`、`x-department-ids`、`x-allowed-tables`、`x-denied-tables`。
- 已实现：`SQLReactState` / `FinalGraphState` 增加 `security_context`、`authorization_report`，dispatcher 会把权限上下文传入 SQL 子图。
- 后续：新增权限策略表和 Admin 维护入口，覆盖表级、列级、行级和脱敏策略。

验收：

- 已覆盖 API 默认上下文和请求头解析单测。

#### Task 2：选表后权限门禁

- 已实现：新增 `authorize_selected_tables` 节点。
- 已实现：`select_tables` 不静默过滤无权限表，而是保留选中表和表级业务描述。
- 已实现：无权限时返回业务数据域名称提示，并结束 SQL 链路。
- 已实现：审计记录 `table_permission_denied`。

验收：

- 用户查询无权限表时不会进入 `sql_retrieve/sql_generate`。
- 用户提示不暴露物理表名。

#### Task 3：补表权限门禁

- 已实现：改造 `sql_generate` 中 `_retrieve_missing_tables()` 调用点。
- 已实现：LLM 要求补表时先执行 `_authorize_missing_tables_or_response`。
- 已实现：无权限补表直接停止，返回业务数据域权限提示，并记录审计。

验收：

- 构造 LLM 返回 `missing_tables=["t_salary_detail"]` 的测试，确认不会加载 schema。
- 审计记录补表阶段的拒绝事件。

#### Task 4：SQL 执行前授权

- 已实现：新增 `authorize_sql` 节点，位于 `safety_check` 与 `approve` 之间。
- 已实现：V1 通过 `FROM/JOIN` 提取 SQL 涉及表并校验表级权限。
- 已实现：复杂计划的每个 SQL step 在 `safety_check` 后、`execute_sql` 前复用 `authorize_sql`。
- 后续：引入 SQL AST 解析，提取列、别名和子查询；实现列级权限、行级 predicate 注入、`SELECT *` 展开与拒绝策略。

验收：

- 已覆盖 SQL 生成绕过选表后直接 JOIN 无权限表时被 `authorize_sql` 拒绝。
- 已覆盖复杂计划 SQL step 不会绕过执行前授权。

#### Task 5：结果脱敏与业务化展示

- 已实现：新增 `format_result_for_user`。
- 已实现：从 `semantic_model` 和 SQL alias 构建 `physical -> display` 映射，中文别名保持不变。
- 已实现：`_summarize_sql_result` 使用业务化展示；业务术语命中路径也优先读取 `semantic_model` 字段业务名。
- 后续：新增 `mask_result`，在字段展示前执行列级脱敏，并扩展到复杂计划 merge/report 的结构化结果。

验收：

- 已覆盖 `t_role.name` 与 `t_department.name` 按表顺序展示为不同业务名。
- 已覆盖 SQL 执行结果将 `real_name/name` 展示为“真实姓名/角色名称”。

#### Task 6：全链路审计日志

- 已实现：新增 `write_audit_log` no-throw 骨架。
- 已实现：表级权限拒绝、补表权限拒绝、SQL 执行前权限拒绝写审计事件。
- 后续：新增 `t_query_audit_log`，成功、失败、拒绝、审批取消、安全拒绝都写审计；复杂计划写父审计和 step 审计。
- 后续：审计默认只存元数据、行数、字段结构、SQL hash，不存完整明细。

验收：

- 已覆盖权限拒绝能定位用户、query、被拒绝物理表和业务数据域。

### 回归测试计划

1. 表级无权限：查询“所有用户真实姓名”，提示无权限，不进入 SQL 生成。
2. 补表无权限：LLM 要求补充薪酬明细表，系统拒绝补表并结束。
3. SQL 兜底：LLM 生成未授权表 JOIN，`authorize_sql` 拒绝。
4. 业务化展示：结果列显示“真实姓名/角色名称”，不显示 `real_name/name`。
5. 复杂计划：每个 SQL step 都走执行前权限检查。
6. 后续补充：列级脱敏、行级权限、审计落库、复杂计划 step 审计和脱敏 merge/report。

### 当前结论

数据权限不能作为 SQL 执行失败后的补丁，而应该进入 NL2SQL 主链路：先让系统判断“这个问题需要什么业务数据”，再判断“当前用户是否有权访问”。本轮 V1 已经把表级权限接入选表后、补表时、SQL 审批前和复杂计划 SQL step，避免 LLM 因看不到表而幻觉，也避免补表和复杂计划绕过表级权限边界。下一阶段重点是把表级策略扩展到 SQL AST、行级权限、列级脱敏和持久化审计。

## Iteration：AgentScope Data Planner 端到端修复与延迟复测

日期：2026-05-18

### 背景

本轮重构目标是让入口收敛为 `data / chat / clarify`，数据类请求统一由 AgentScope Planner 产出 `analysis_plan`，再交给 SQL Harness 做审批、安全检查、SQL 生成、执行和 report。验证过程中发现两个核心问题：

- 本地 AgentScope 兼容 runner 在 `analysis_plan.steps[0].sql` 中塞入了 `select ... count(*) ...` smoke SQL。由于 `execute_complex_plan_step` 已经支持信任提交的 `step.sql`，这段 fake SQL 被直接执行，导致最终结果是假分析。
- 成功执行后的最终展示仍可能回退到“复杂查询计划执行完成 + SQL 明细 + 样例行”，用户不可读。

### 本轮改动

1. `LocalAgentScopeCompatibleRunner._data_analysis_plan()` 不再提交可执行 SQL，只提交结构化 step 目标、表、依赖和 report 步骤。SQL 仍由 SQL Harness 在执行阶段基于 schema/semantic/evidence 生成。
2. `classify_intent()` 增加规则短路：数据库规则命中且当前没有对话历史依赖时，直接返回 `data/chat/clarify` 和 rewrite，不再加载 domain summary，也不再调用分类 LLM。需要结合历史补全的追问仍保留 LLM 分类/重写路径。
3. `report` 步骤即使没有 `merge_keys`，也会把依赖 SQL 的结构化行带入最终 formatter，生成业务可读的“关系分析结果”。
4. 指标汇总做了两个展示修正：
   - `budget_cost` 不再被误算进“成本合计”；
   - `receivable_amount` 可被识别为回款/应收相关指标。
5. README GIF 脚本中 complex demo 的等待文案从旧的“复杂查询计划执行完成”改成“关系分析结果”，并重新录制 GIF。

### 端到端验证

浏览器真实链路：

```text
问题：收入成本预算回款费用之间的关系
入口：Data Agent
服务：APP_PORT=8081 AGENTSCOPE_RUNTIME_BACKEND=local
```

复测结果：

```text
TOTAL_MS=71306
CLASSIFY_MS=253
PLAN_MS=572
HAS_COUNT_SQL=false
HAS_RELATION=true
HAS_PLAN_COMPLETE=false
```

最终 UI 展示已经变为：

```text
关系分析结果：
- 本次按计划完成 2/2 个步骤，合并得到 1 条可对齐记录。
- 预算合计：15588682.45。
- 收入合计：1316000.00。
- 成本合计：1413000.00。
- 费用合计：167108.81。
- 回款合计：1372036.02。
- 粗略盈余：-264108.81。
- 回款效率：104.26%。
执行概况：SQL 明细和样例行已保留在 trace 中，这里只展示面向业务判断的结论。
```

GIF 已重新生成：

```text
docs/assets/demos/sql-complex-finance-relation-plan-approved.gif  8.4M
docs/assets/demos/raw/sql-complex-finance-relation-plan-approved.webm  6.8M
```

### 延迟结论

分类和规划阶段已经明显缩短：

- `/api/query/classify`：从此前约 12s 级别降到本次 253ms，因为命中 DB rule 后不再调用分类 LLM。
- `/api/query/invoke` planner：本次约 572ms，主要是业务知识召回、schema.list、semantic_model、related_tables 和 `analysis_plan.submit`。
- 总耗时仍可能较长，主要在审批后的 SQL 生成与修复回路。实际日志里仍出现过 `rp.cost_center_id`、`ji.entry_date` 等不存在字段导致的 repair retry，说明下一步瓶颈不是 AgentScope Planner，而是 SQL generation prompt/schema 约束和错误修复质量。

### Trace 状态

服务启动日志确认：

- `LangSmith tracing enabled (endpoint: https://api.smith.langchain.com, project: agent-platform-py)`
- AgentScope runtime tool summary 覆盖 `business_knowledge.search`、`schema.list_tables`、`semantic_model.search`、`schema.related_tables`、`analysis_plan.submit`
- CozeLoop trace ingest 返回 200

受当前运行环境限制，未在浏览器中直接打开私有 LangSmith UI 链接做人工点查；本轮以服务端 trace 日志和工具链路日志作为可复现证据。

补充排查结论：

- `agentscope_data_planner` 原先没有把 LangGraph config 里的 callbacks 传给 `AgentScopeRuntime`，因此 LangSmith 只能看到顶层 graph node，看不到 AgentScope runtime/tool 子 span。已补齐 callback 透传，并用单测固定。
- 当前 `AGENTSCOPE_RUNTIME_BACKEND=local` 时，`agentscope_data_planner` 不会产生 LLM call。它是本地兼容 runner，确定性调用 ToolCatalog 工具并提交 `analysis_plan`，LangSmith 中预期看到的是 `agentscope.runtime.data_analysis` 和 `agentscope.tool.*`，不是模型调用。
- 只有启用真实 AgentScope package backend 时，Planner 才会通过 AgentScope `ReActAgent` 调用模型。那时是否能在 LangSmith 中展示为 LLM span，取决于 AgentScope package 的 model adapter 是否接入 LangChain callback；平台侧至少需要保证 runtime/tool span 已经透传。
- `/api/query/invoke` 在 `approve_analysis_plan` 处 `interrupt()` 后会结束本次 root run；用户点击确认后 `/api/query/approve/stream` 用同一个 graph thread resume，但通常会在 LangSmith 中形成另一条 root run。因此截图只停在 `approve_analysis_plan` 时，看到的是审批前半段，不代表审批后的执行节点没有跑。

### 后续优化

1. 优化 SQL generation prompt，只传当前 step 必需字段，减少全 schema 噪声。
2. 在 `sql_generate` 前加入字段存在性约束或候选字段白名单，降低 `rp.cost_center_id` / `ji.entry_date` 这类幻觉字段。
3. 对高频复杂分析沉淀结构化 metric plan，而不是让单个 SQL step 自由拼所有指标。
4. LangSmith 私有 UI 可访问时，按本轮 session_id / timestamp 对照检查 graph node、tool span 和 LLM span 是否完整。

## Iteration：data 入口简单查询报错修复

日期：2026-05-19

### 问题

用户问题 `收入成本预算回款费用之间的关系` 曾返回：

```text
数据分析计划生成失败：AgentScope 未提交可执行的 analysis_plan
```

复测当前代码后确认，复杂关系查询已经能返回 `pending_approval`。随后用简单查询 `查询 2025 年销售收入总额` 发现另一条失败链路：

1. 未命中 DB intent rule 时，`classify_route.llm` 调用默认 `ark` 模型。
2. 当前环境 Ark 账号返回 `AccountOverdueError`，API 直接返回系统错误。
3. 命中规则进入 data 后，真实 AgentScope package 同样因 Ark 403 无法提交 plan，但 dispatcher 已 fallback 到 `LocalAgentScopeCompatibleRunner`，能生成 `analysis_plan`。
4. 用户审批后，SQL Harness 的 `sql_generate` 仍依赖 chat LLM；当前 Ark 403 会导致 SQL 生成失败。

### 本轮修复

- `classify_intent()` 捕获分类 LLM 异常，降级进入 `data`，不再把 provider 403 直接暴露为 API 系统错误。
- `data/intent_rules_seed.json` 扩展省略主体财务查数规则，支持 `2025 年` 这类明确年份表达；运行 `python -m scripts.seed_intent_rules` 后，`查询 2025 年销售收入总额` 可由 DB rule 短路到 data，避免分类 LLM。
- `sql_generate()` 捕获 SQL 生成 LLM 异常，返回用户可读的 `SQL 生成模型暂时不可用...`，同时保留结构化错误码 `sql_generation_llm_unavailable` 供 trace/排障使用。
- 复杂计划失败展示优先使用 step 的用户可读 `answer`，不再只展示内部错误码。

### 验证

单测：

```text
.venv/bin/python -m pytest tests/test_dispatcher.py tests/test_seed_intent_rules.py -q
17 passed

.venv/bin/python -m pytest tests/test_sql_react.py::TestSqlGenerate::test_generate_llm_failure_returns_user_safe_error tests/test_sql_react.py::TestSqlGenerate::test_complex_plan_failure_prefers_user_safe_answer_over_error_code -q
2 passed
```

真实 API：

```text
POST /api/query/invoke
query=查询 2025 年销售收入总额

返回：status=pending_approval，approval_type=complex_plan
日志：classify_route: rule_short_circuit route=data

POST /api/query/approve
返回：status=error
用户可见错误：SQL 生成模型暂时不可用，无法生成可执行 SQL。请稍后重试或切换可用模型配置。
```

### 当前限制

当前环境 `CHAT_MODEL_TYPE=ark`，Ark 调用返回 `AccountOverdueError`；`OPENAI_CHAT_MODEL` 和 `QWEN_CHAT_MODEL` 仍是占位值 `your-chat-model`。因此本轮只能验证到错误收敛和审批链路正常，不能在当前模型配置下验证 SQL 真正生成并执行成功。

## Iteration：AgentScope 工具暴露与 token 成本控制

日期：2026-05-24

### 背景

LangSmith 里 `agentscope.llm.data_analysis_agent.reasoning` 调用次数和单次 input token 都偏高。排查后确认，真实 AgentScope package runner 走 ReActAgent，每轮模型调用都会携带当前可见工具 schema；如果一次性把 data_analysis 的全量规划、schema、SQL 预检查和 handoff 工具都暴露给模型，工具描述、参数 schema 和历史 observation 会持续放大上下文成本。

此前已做紧急止血：真实 AgentScope data_analysis runner 只注册 6 个规划必要工具，并压缩工具描述和大 observation，`data_analysis_max_iters` 降到 5。这能降低成本，但它仍是静态过滤，不表达“当前阶段/上一工具调用之后到底允许看哪些工具”。

### 本轮设计

- 引入可配置的 `ToolExposurePolicy`，工具可见性按交集计算：

```text
runtime/security allowlist ∩ stage/previous-tool policy ∩ skill allowlist
```

- data_analysis 默认策略：
  - start：`current_time.now`、`business_knowledge.search`、`schema.select_candidates`
  - after `current_time.now`：`business_knowledge.search`、`schema.select_candidates`
  - after `business_knowledge.search`：`schema.select_candidates`
  - after `schema.select_candidates`：`semantic_model.search`、`schema.related_tables`、`analysis_plan.submit`
  - after `semantic_model.search`：`schema.related_tables`、`analysis_plan.submit`
  - after `schema.related_tables`：`analysis_plan.submit`
  - after `analysis_plan.submit`：不再暴露工具
- AgentScope toolkit 可以注册策略允许的 data_analysis 规划工具全集，但每次 LLM call 只在 `_TracingModelProxy` 中把当前阶段可见的 tool schema 传给模型，从而减少每轮 input token。
- 非 data_analysis 任务保持原行为，避免扩大改动面。

### SQL Harness 边界

AgentScope Planner 只负责取证和提交 `analysis_plan`。SQL safety、authorize、approve、execute 仍属于 SQL Harness：

- `agentscope.tool.analysis_plan.submit` 是 handoff，不是执行。
- Planner 不再把 `sql.safety_check`、`sql.authorize_draft` 暴露给真实 package ReActAgent。
- 后续 `approve_analysis_plan`、SQL 生成、SQL 安全检查、权限检查、执行和结果组织仍由 `agents/flow/sql_react.py` 与 dispatcher 负责。

### 后续改进

1. 把工具描述分成 runtime/internal 与 LLM compact 两种 contract，避免 prompt 手写漂移。
2. 把 observation 压缩策略沉淀成工具级 contract，例如 `summary/raw/hidden`、`max_items`、`max_chars`。
3. 在 LangSmith trace 中区分真实调用与平台合成节点，避免 `agentscope.plan.*` 这类 span 被误解为独立 LLM 调用。
4. 继续检查 DataAgent 的 toolkit/callback 机制，确认是否也按阶段缩窄工具 schema，而不是每轮携带全量工具。

## Iteration Plan：Skill 作为 ReActAgent 的能力调用单元

日期：2026-05-24

### 目标

把真实 AgentScope ReActAgent 从“直接编排底层 tools”上移为“选择和调用业务 skill”。底层 ToolCatalog 仍存在，但默认只暴露给 skill runtime；ReActAgent 看到的是少量稳定业务能力，而不是 15 个以上 primitive tools。

这不是把 DataAgent 简化成 NL2SQL。DataAgent 的能力范围应覆盖指标口径、数据发现、表字段取证、权限感知规划、复杂计划拆解、SQL Harness handoff、执行结果解释和报告生成。skill 是这些能力的稳定 contract。

### 核心概念

```text
tool:
  原子能力，例如 schema.select_candidates、semantic_model.search、schema.related_tables。

skill:
  面向业务任务的固定或半固定流程，例如 finance_relation_analysis_skill。
  skill 内部可以调用多个 tool，也可以做局部复杂度判断和计划拆解。

ReActAgent:
  默认只看到 skill 列表，负责选择哪个 skill，而不是逐个选择底层 tool。

SQL Harness:
  仍负责 approve、sql_generate、sql.safety_check、authorize、execute、repair、merge/report。
```

### 设计原则

1. skill 不是“大 prompt 包装”。skill 必须是：

```text
contract + allowed_tools + workflow/state machine + compact output + trace policy
```

2. ReActAgent 负责选择能力，skill runtime 负责可靠执行流程。
3. skill 内部 observation 返回给 LLM 时只给摘要，不把完整 tool output、完整 semantic model、完整 trace 全塞回上下文。
4. primitive tools 仍可在开发/debug/探索模式下受控开放，但生产 data_analysis 默认走 skill 级暴露。
5. SQL 执行边界不迁移到 AgentScope。skill 只能提交 plan/handoff，不能直接执行 SQL。

### `finance_relation_analysis_skill` 设计

适用问题：

```text
收入成本预算回款费用之间的关系
分析当前公司收入、成本、预算、回款、费用
按部门分析收入成本费用预算执行和回款效率
```

建议输入：

```json
{
  "query": "用户原始问题",
  "session_id": "会话 ID",
  "security_context": {},
  "workflow_state": {},
  "constraints": {
    "time_range": "可选",
    "grain": "可选，例如 department/month/project",
    "budget_status": "可选，例如 已审批/执行中",
    "cash_vs_accrual": "可选"
  }
}
```

内部阶段：

```text
1. resolve_context
   - current_time.now
   - 解析相对时间、本年/去年/当前等表达

2. business_grounding
   - business_knowledge.search
   - 召回净利润、净利率、预算差异、预算执行率、应收周转/回款效率等口径

3. schema_grounding
   - schema.select_candidates
   - semantic_model.search
   - schema.related_tables
   - 获取候选表、字段语义、表关系

4. complexity_assessment
   - plan.assess_feasibility 或等价的 skill 内部复杂度判断
   - 判断 single_sql、plan_execute、needs_clarification

5. plan_building
   - simple case: 生成单步 analysis_plan
   - complex case: 生成多步 decomposition analysis_plan
   - unclear case: 返回 clarification_questions

6. handoff
   - analysis_plan.submit
   - 提交给 SQL Harness，进入审批/执行链路
```

建议输出：

```json
{
  "status": "plan_ready | needs_clarification | failed",
  "skill_name": "finance_relation_analysis_skill",
  "skill_version": "2026-05-24",
  "execution_mode": "single_sql | plan_execute | clarification",
  "summary": "给外层 Agent 的简短摘要",
  "evidence": [
    "使用了哪些业务口径",
    "选择了哪些核心表",
    "关键关系或缺口"
  ],
  "analysis_plan": {},
  "clarification_questions": [],
  "trace_refs": []
}
```

### 复杂查询分流

复杂财务关系查询不能让 LLM 一次性写大 SQL join。`finance_relation_analysis_skill` 必须先做复杂度判断：

```text
if 缺少必要时间/主体/粒度/口径:
  execution_mode = clarification

elif 候选表少、join 风险低、指标口径简单:
  execution_mode = single_sql

else:
  execution_mode = plan_execute
```

复杂度判断输入：

```text
- 用户问题
- 业务知识召回结果
- selected_tables
- semantic_model_summary
- relationships
- 安全上下文允许范围
- 是否涉及多指标、多粒度、多事实表、多口径合并
```

复杂度判断输出：

```json
{
  "can_single_sql": false,
  "can_decompose": true,
  "execution_mode": "plan_execute",
  "reasons": [
    "收入、预算、回款、费用来自不同事实表",
    "需要按共同粒度合并",
    "一次性多表 join 容易产生字段和关系幻觉"
  ],
  "risky_joins": [],
  "missing_fields": [],
  "recommended_grain": ["period", "cost_center_id"]
}
```

### Plan Execute React 模式

当 `execution_mode=plan_execute` 时，skill 输出多步 `analysis_plan`，而不是输出大 join SQL。每个 SQL step 应尽量控制表数量和粒度：

```json
{
  "mode": "analysis_plan",
  "execution_mode": "plan_execute",
  "steps": [
    {
      "step": 1,
      "type": "sql",
      "goal": "按部门和期间统计收入",
      "tables": ["t_journal_entry", "t_journal_item", "t_account", "t_cost_center"],
      "grain": ["period", "cost_center_id"],
      "depends_on": [],
      "merge_keys": ["period", "cost_center_id"]
    },
    {
      "step": 2,
      "type": "sql",
      "goal": "按部门和期间统计成本与费用",
      "tables": ["t_journal_entry", "t_journal_item", "t_account", "t_expense_claim", "t_cost_center"],
      "grain": ["period", "cost_center_id"],
      "depends_on": [],
      "merge_keys": ["period", "cost_center_id"]
    },
    {
      "step": 3,
      "type": "sql",
      "goal": "按部门和期间统计预算与实际",
      "tables": ["t_budget", "t_cost_center"],
      "grain": ["budget_year", "budget_month", "cost_center_id"],
      "depends_on": [],
      "merge_keys": ["period", "cost_center_id"]
    },
    {
      "step": 4,
      "type": "python_merge",
      "goal": "合并收入、成本、费用、预算、回款并计算关系指标",
      "depends_on": [1, 2, 3],
      "merge_keys": ["period", "cost_center_id"]
    },
    {
      "step": 5,
      "type": "report",
      "goal": "输出关系分析结论、异常点和后续追查建议",
      "depends_on": [4]
    }
  ]
}
```

后续执行仍由 SQL Harness 完成：

```text
approve_analysis_plan
  -> per-step sql_generate
  -> sql.normalize
  -> sql.safety_check
  -> authorize_sql
  -> execute_sql
  -> repair retry
  -> merge/report
```

### Trace 形态目标

LangSmith/平台 trace 应能区分真实调用层级：

```text
agentscope.llm.data_analysis_agent.reasoning
  -> agentscope.skill.finance_relation_analysis
       -> agentscope.tool.current_time.now
       -> agentscope.tool.business_knowledge.search
       -> agentscope.tool.schema.select_candidates
       -> agentscope.tool.semantic_model.search
       -> agentscope.tool.schema.related_tables
       -> agentscope.skill.complexity_assess
       -> agentscope.tool.analysis_plan.submit
  -> route_after_agentscope_data_planner
  -> approve_analysis_plan
  -> sql_harness.*
```

trace 要求：

- skill span 是真实 runtime 执行，不伪装成 LLM。
- tool span 保留完整结构化 output 给平台排障。
- 返给 ReActAgent 的 observation 是 compact summary。
- `analysis_plan.submit` 之后的 SQL 相关节点归 SQL Harness，不归 AgentScope Planner。

### 实施计划

#### 阶段 1：Skill Contract 与 Registry

目标：定义 skill 作为一等 runtime contract。

建议文件：

```text
agents/runtime/skill_contracts.py
agents/runtime/skill_registry.py
tests/test_skill_registry.py
```

任务：

1. 增加 `RuntimeSkill`、`SkillInput`、`SkillResult`、`SkillTracePolicy`。
2. 扩展现有 `SkillDefinition`，区分 prompt-only skill 与 executable skill。
3. 支持 skill 的 `allowed_tools`、`input_schema`、`output_schema`、`version`、`execution_mode_hints`。
4. 测试 skill allowlist 只能收窄工具，不能扩大安全上下文允许范围。

依赖：无。可独立开发。

#### 阶段 2：Skill Runtime / Tool Workflow 执行器

目标：让 skill 内部能按固定流程调用 ToolCatalog，并记录 child tool trace。

建议文件：

```text
agents/runtime/skill_runtime.py
agents/runtime/agentscope_runtime.py
tests/test_skill_runtime.py
tests/test_agentscope_runtime.py
```

任务：

1. 实现 `SkillRuntime.invoke_skill(skill_name, payload, context)`。
2. skill 内部通过现有 `AgentScopeRunContext.invoke_tool()` 调用工具，复用缓存、权限和 LangSmith callback。
3. skill 返回 compact observation 给外层 ReActAgent。
4. child tool trace 保留在 `result.tool_trace` 和 LangSmith span 中。

依赖：阶段 1。

#### 阶段 3：`finance_relation_analysis_skill`

目标：沉淀收入、成本、预算、回款、费用关系分析的固定取证流程。

建议文件：

```text
agents/runtime/skills/finance_relation_analysis.py
tests/test_finance_relation_analysis_skill.py
```

任务：

1. 实现 context/time/business/schema/relationship 取证流程。
2. 使用 compact semantic model，不把全量字段模型传回 LLM。
3. 信息不足时返回 `needs_clarification`，不伪造 plan。
4. 信息充分时进入复杂度判断。

依赖：阶段 1、阶段 2。可与阶段 4 并行开发接口草案，但最终集成依赖阶段 2。

#### 阶段 4：复杂度判断与 Plan Execute 分流

目标：在 skill 内部保留“是否复杂 SQL”的判断，复杂场景走 Plan Execute React。

建议文件：

```text
agents/runtime/skills/complexity.py
agents/runtime/skills/finance_relation_analysis.py
tests/test_finance_relation_analysis_skill.py
tests/test_sql_react.py
```

任务：

1. 将 `plan.assess_feasibility` 或等价逻辑封装为 skill 内部 `complexity_assess` 阶段。
2. 输出 `single_sql`、`plan_execute`、`clarification` 三种模式。
3. 对多事实表、多口径、多粒度合并默认倾向 `plan_execute`。
4. 对 `plan_execute` 生成多步 `analysis_plan`，每个 step 控制表数量、grain 和 merge_keys。
5. 增加回归用例：`收入成本预算回款费用之间的关系` 必须走 `plan_execute`，不能生成单条大 join SQL。

依赖：阶段 3。可先独立写纯函数和测试。

#### 阶段 5：AgentScope ReActAgent 暴露 skill，而不是 primitive tools

目标：真实 package runner 的 data_analysis agent 默认只看到 executable skills。

建议文件：

```text
agents/runtime/agentscope_adapter.py
agents/runtime/tool_exposure_policy.py
tests/test_agentscope_adapter.py
```

任务：

1. 把 `finance_relation_analysis_skill` 注册成 AgentScope toolkit function，例如 `finance_relation_analysis`。
2. 默认不向 data_analysis ReActAgent 暴露 `schema.select_candidates`、`semantic_model.search` 等 primitive tools。
3. 保留受控 debug 配置：允许临时暴露 primitive tools，用于开发排障。
4. `_TracingModelProxy` 记录每轮可见 skill/tool names，方便验证 token 缩减。
5. 测试工具 schema JSON 长度、每轮可见 function names、`analysis_plan.submit` 不直接暴露给外层 Agent。

依赖：阶段 1、阶段 2。可与阶段 3/4 并行开发 adapter 外壳。

#### 阶段 6：SQL Harness 边界回归

目标：确认 skill 化不会绕过 SQL Harness。

建议文件：

```text
agents/flow/dispatcher.py
agents/flow/sql_react.py
tests/test_dispatcher.py
tests/test_sql_react.py
```

任务：

1. 验证 `finance_relation_analysis_skill` 只提交 `analysis_plan`，不执行 SQL。
2. 验证审批前 API 返回 `pending_approval`。
3. 验证审批后仍走 per-step SQL generate/safety/authorize/execute。
4. 验证 SQL repair 仍发生在 SQL Harness，不发生在 skill runtime。

依赖：阶段 3、阶段 4。可提前补测试夹具。

#### 阶段 7：Trace 与成本验收

目标：让 LangSmith 中能看懂真实 skill/tool/SQL Harness 边界，并量化 token 降幅。

建议文件：

```text
agents/tool/trace/tracing.py
agents/runtime/agentscope_adapter.py
tests/test_tracing.py
tests/test_agentscope_adapter.py
```

任务：

1. 增加 `agentscope.skill.*` span。
2. 标记 span metadata：`real_call=true`、`span_layer=skill|tool|sql_harness`、`visible_functions=[...]`。
3. 移除或重命名容易误解的合成 span，例如把非真实 LLM 的 `agentscope.plan.*` 标为 `synthetic=true`。
4. 增加本地 token/schema size 统计脚本或测试断言。
5. 用真实 query `去年亏损` 和 `收入成本预算回款费用之间的关系` 做 LangSmith 验证。

依赖：阶段 5。可与阶段 6 并行。

### 可并行开发拆分

```text
Track A: Skill Contract + Runtime
  阶段 1 -> 阶段 2

Track B: Finance Relation Skill
  阶段 3 -> 阶段 4
  可先基于 mock SkillRuntime 写纯单测

Track C: AgentScope Adapter
  阶段 5
  可先实现 toolkit 暴露 skill function 的外壳，再接真实 skill runtime

Track D: SQL Harness Regression
  阶段 6
  可先补审批、执行边界测试

Track E: Trace/Cost
  阶段 7
  可先补 span metadata 和 schema size 验收，再接真实链路
```

推荐执行顺序：

```text
1. Track A 先完成最小 contract/runtime
2. Track B 和 Track C 并行
3. Track D 在 B 的 plan output 稳定后接入
4. Track E 全程跟随，最后用真实 LangSmith query 验收
```

### 验收标准

1. data_analysis 真实 AgentScope package runner 首轮不再暴露全量 primitive tools。
2. `finance_relation_analysis_skill` 能对 `收入成本预算回款费用之间的关系` 产出 `execution_mode=plan_execute` 的多步 plan。
3. skill 返回给 LLM 的 observation 是 compact summary，完整 tool output 只进入 trace/artifact。
4. `analysis_plan.submit` 后仍进入 SQL Harness 的审批和执行链路。
5. LangSmith 中能看到清晰层级：Agent reasoning -> skill -> child tools -> SQL Harness。
6. 单测覆盖 skill contract、skill runtime、finance relation plan_execute、AgentScope visible functions、SQL Harness boundary。

### 2026-05-24 小方案执行状态

本轮先完成可观测性和 SQL Harness 边界命名，不继续扩大到 `skill_registry` 与 `ToolExposurePolicy` 统一重构。

已完成：

1. `agentscope.skill.finance_relation_analysis` span 增加 `span_layer=skill`、`real_call=true`、`visible_functions=["finance_relation_analysis"]` 和 `allowed_tools` 元数据。
2. `approve_analysis_plan` 增加 `sql_harness.approve_analysis_plan` span，标记 `span_layer=sql_harness`、`real_call=true`、`stage=approval`、`approval_type=complex_plan`、`step_count`。
3. `execute_analysis_plan` 增加 `sql_harness.execute_analysis_plan` span，标记 `span_layer=sql_harness`、`real_call=true`、`stage=execution`、`approved`、`step_count`。
4. 保持业务行为不变：AgentScope skill 仍只提交 `analysis_plan`，审批后才进入 SQL Harness 分步执行。

验证：

```bash
.venv/bin/python -m pytest \
  tests/test_skill_runtime.py::test_skill_runtime_span_metadata_includes_visible_functions \
  tests/test_dispatcher.py::test_dispatcher_emits_sql_harness_approval_and_execution_spans -q
# 2 passed
```

下一阶段：

1. 统一旧 `SkillRegistry` 与 executable `RuntimeSkill`，明确 prompt-only skill 与 executable skill 的注册边界。
2. 实现可配置 `ToolExposurePolicy`，支持按 task/stage/previous-tool/skill allowlist 计算每轮可见工具。
3. 将 schema size / visible function names 的统计固化为测试或诊断脚本。

### 2026-05-24 Registry 与 ToolExposurePolicy 小步统一

本轮在小方案验证通过后，继续完成 `skill_registry` 和 `ToolExposurePolicy` 的最小统一，不引入完整 skill marketplace 或复杂动态路由。

已完成：

1. `SkillDefinition` 增加 `kind="prompt" | "executable"` 与 `runtime_contract`，保留原有 prompt-only skill 行为。
2. 新增 `SkillDefinition.from_runtime_skill(...)`，将 `RuntimeSkill.allowed_tools/input_schema/output_schema/execution_modes` 统一映射到 registry 可序列化定义。
3. `SkillRegistry.builtin()` 已纳入 `finance_relation_analysis` executable skill。旧的 `budget_variance_analysis`、`revenue_cost_relation` 仍保持 prompt-only skill。
4. 新增 `ToolExposurePolicy`：
   - 生产默认 `data_analysis` 只暴露 `finance_relation_analysis`。
   - debug primitive 模式按阶段暴露有限工具，而不是全量 15 个 data_analysis tools。
   - 阶段例子：start 暴露 `current_time.now`、`business_knowledge.search`、`schema.select_candidates`；after `business_knowledge.search` 只暴露 `schema.select_candidates`。
5. `AgentScopePackageRunner` 接入 `ToolExposurePolicy`：
   - 默认 toolkit 只注册 skill function。
   - debug primitive toolkit 注册有限 primitive 工具集合。
   - `_TracingModelProxy` 在每次真实模型调用前按 `context.tool_trace` 的上一成功工具过滤 tool schema，LangSmith LLM span metadata 中的 `tool_names` 反映本轮真实可见函数。
6. `finance_relation_analysis` executable skill allowlist 补充 `schema.list_tables`，保证 registry allowlist 收窄后本地兼容 runner 仍能做可见表发现；skill 默认执行流程未新增该工具调用。
7. `ToolExposurePolicy` 支持从 `AGENTSCOPE_TOOL_EXPOSURE_POLICY_JSON` 加载最小配置，`scripts/diagnose_tool_exposure.py` 可输出 skill-only / primitive-debug 的 schema 大小对比和近似 token 统计。
8. `AgentScopeRuntime` 会把自身 `tool_exposure_policy` 注入支持该字段的 runner，避免 runtime 层配置和 package runner 实际执行策略脱节。

当前边界：

- `ToolExposurePolicy` 已控制真实模型调用前的 schema 可见性。
- 真实 ReActAgent 内部下一步可调用范围通过模型代理层过滤传入 `tools` 参数实现；toolkit 注册集合仍需要包含 debug/recovery 流程可能直接调用的函数。
- 非 `data_analysis` 任务保持旧逻辑，避免扩大行为面。
- policy 配置失败时回退默认值，不中断启动。

验证：

```bash
.venv/bin/python -m pytest \
  tests/test_tool_exposure_policy.py \
  tests/test_skill_registry.py \
  tests/test_skill_runtime.py \
  tests/test_agentscope_adapter.py -q
# 46 passed
```

诊断脚本实测输出：

```text
skill_only: function_count=1, schema_chars=652, estimated_tokens=152
primitive_debug_start_visible: function_count=3, schema_chars=1603, estimated_tokens=368
primitive_debug_registered: function_count=6, schema_chars=2820, estimated_tokens=642
```

真实 query 验证：

```text
去年亏损 -> pending_approval, execution_mode=single_sql
收入成本预算回款费用之间的关系 -> pending_approval, execution_mode=plan_execute
```

LangSmith 最新 trace 确认：

```text
agentscope.llm.data_analysis_agent.reasoning 可见函数为 finance_relation_analysis
agentscope.skill.finance_relation_analysis 真实执行并记录 child_tool_count=7
sql_harness.approve_analysis_plan 在 analysis_plan.submit 后出现
```

### 2026-05-24 文档补齐记录：当前实现与可观测边界

本轮把实现说明补进长期文档，目标是让后续排障的人只看文档就能回答三个问题：

1. 生产默认到底暴露了哪些工具。
2. `AgentRunResult` 在什么时候组装。
3. LangSmith 里哪些节点是真实调用，哪些只是平台边界。

这次文档补齐的核心结论如下：

- 生产默认 `data_analysis` 只向外层 ReActAgent 暴露 `finance_relation_analysis` 一个 executable skill。
- `finance_relation_analysis` 是代码状态机，不是大 prompt；它内部通过 `current_time.now`、`business_knowledge.search`、`schema.select_candidates`、`semantic_model.search`、`schema.related_tables`、`plan.assess_feasibility`、`analysis_plan.submit` 完成财务关系分析取证。
- `AgentRunResult` 不是每次 tool 调用后都组装，而是在 agent 最终 reply 返回后，由 runtime 把 `tool_trace`、`events`、`state_patch` 合并进去。
- SQL safety / authorize / execute 的正式边界仍然在 SQL Harness，不在 AgentScope skill。
- debug primitive 模式只是一种受控排障视图，不代表生产默认暴露 15 个 primitive tools。

对应实现落点：

- `agents/runtime/skill_contracts.py`
- `agents/runtime/skill_runtime.py`
- `agents/runtime/skills/finance_relation_analysis.py`
- `agents/runtime/tool_exposure_policy.py`
- `agents/runtime/skill_registry.py`
- `agents/runtime/agentscope_runtime.py`
- `agents/runtime/agentscope_adapter.py`
- `agents/flow/dispatcher.py`
- `scripts/diagnose_tool_exposure.py`

文档更新后的验证仍沿用当前已完成的单测和真实 query 结果，不新增业务行为。

### 2026-05-24 SQL Quality Gate：SQL 正确性治理前移

当前 `approve_analysis_plan` 只解决了“业务是否接受继续执行”，还没有解决“SQL 本身是否真的对”。如果用户不懂 SQL，只靠 approve 很容易把错误 SQL 放进执行链路。这里需要把 SQL 正确性治理前移到 SQL Harness 内部，形成系统级的 `SQL Quality Gate`。

#### 参考实现：DataAgent

DataAgent 的主流程不是“生成 SQL 后直接执行”，而是：

```text
Planner -> PlanExecutor validate -> Human review -> SqlGenerateNode -> SemanticConsistencyNode -> SqlExecuteNode
```

也就是说，SQL 生成后、执行前有一层语义一致性校验，不通过就回到生成阶段。它还提供了 SQL 重试、优化次数和分数阈值等配置，说明业界实践已经把“SQL 是否靠谱”当成独立治理问题，而不是只交给用户 approve。

DataAgent 的 `SqlVerifyExplainService` 也表明了这种思路：先解析 SQL，再检查聚合、分组、时间过滤、时间窗口、排序、limit、distinct、排序方向，以及人工反馈约束等规则；校验结果不是简单 pass/fail，而是 `safe_to_execute` / `revise_sql` + 规则解释 + 修复建议。

#### 业界共识

- **Semantic model / governed metrics**：把指标、维度、时间口径、表关系先建模，减少 LLM 自由猜口径。
- **Verified queries**：沉淀已验证的问题- SQL 对，作为高可信回归集和提示样本。
- **Execution-guided / self-correction**：先解析、再执行、再根据错误或空结果修复，不把第一次生成当最终答案。
- **Query checker**：在执行前做结构、语义、权限和口径一致性检查。

#### 我们要落的方案

把一个系统级 `sql.semantic_check` 放进 SQL Harness，而不是放进 AgentScope skill：

```text
analysis_plan approve
  -> sql_generate / submitted_sql
  -> sql.parse_and_shape
  -> sql.semantic_check
  -> sql.safety_check
  -> sql.authorize_sql
  -> sql.dry_run / explain
  -> execute_sql
  -> result_sanity_check
  -> report
```

`sql.semantic_check` 的职责建议包括：

1. intent 对齐：用户问收入、成本、预算、回款、费用，SQL 是否真的覆盖这些指标。
2. 时间对齐：去年、本月、当前期间是否映射到正确日期字段或 period。
3. 粒度对齐：按部门、按月、按项目时，GROUP BY 是否匹配。
4. 指标口径：净利润、预算差异、回款效率等是否符合既定公式。
5. 关系校验：join 是否来自已知 relationship，禁止未经聚合的 fact-to-fact 直连。
6. 结果风险：空结果、重复放大、异常 row count、金额方向反了等。

#### UI / 产品变化

用户侧不应再只看到“请 approve SQL”，而应看到一张可解释的校验卡片：

```text
计划回答：2025 年公司亏损情况
使用指标：收入、成本、费用、净利润
时间口径：2025-01 至 2025-12
数据来源：t_journal_entry, t_journal_item, t_account
系统校验：通过 8 项，警告 1 项
风险：未发现预算/回款字段，本问题仅判断损益亏损，不分析现金回款
```

#### 分阶段落地

第一阶段优先做确定性规则，不先上复杂 LLM judge：

1. AST 解析和 SQL shape 抽取。
2. query intent / analysis_plan 的结构化比对。
3. `sql.semantic_check` 输出 `pass / warn / fail + score + problems + repair_hints`。
4. 失败自动进入现有 SQL repair，超过次数则停止执行。
5. approval 页面展示业务口径摘要和系统校验结果。

第二阶段再补治理闭环：

- Verified Query Repository：沉淀已确认 SQL 和口径。
- SQL eval dataset：把失败、修复、用户反馈沉淀为回归集。
- semantic model 强化：补指标、维度、时间字段、状态枚举和 join cardinality。
- 高风险查询可用双 SQL 候选或 validator LLM，但 validator 只能给建议，不能绕过确定性规则。

#### 结论

`approve_analysis_plan` 解决的是“人是否同意继续”，`sql.semantic_check` 解决的是“SQL 是否值得继续”。两者不能互相替代。我们这里更适合采用“规则校验 + 修复循环 + verified queries 回归”的组合，而不是继续把正确性责任压给用户。

#### 2026-05-24 V1 实现记录

已完成第一阶段的最小可用 `SQL Quality Gate`：

1. 新增 `agents/tool/sql_tools/semantic_check.py`。
   - 定义 `SemanticCheckProblem`、`SemanticCheckReport`。
   - 提供 `check_sql_semantics(...)`，返回 `safe_to_execute` / `revise_sql`、score、problems、fix_suggestions、detected_tables。
2. 新增 SQL Harness 节点 `semantic_check`。
   - 简单 SQL 主链路从 `sql_generate -> safety_check` 改成 `sql_generate -> semantic_check -> safety_check`。
   - 复杂计划 step 的内部 harness 从 `safety_check -> authorize_sql -> execute_sql` 改成 `semantic_check -> safety_check -> authorize_sql -> execute_sql`。
3. V1 规则先覆盖确定性高的场景：
   - 亏损金额问题必须有净利润/亏损金额公式。
   - 净利润问题允许 `SUM(credit_amount - debit_amount)` 这类净利润公式，不强制必须有亏损金额 `CASE`。
   - 预算、回款、费用、收入意图有基础字段覆盖检查。
   - 时间和分组是 warning，不在 V1 中默认阻断。
4. generic 查询不会因为低分被误杀。只有明确业务意图且出现 high problem 时才阻断，避免在权限检查前拦截“查用户”等普通查询。
5. LangSmith/trace 中复杂计划内部会出现 `sql.semantic_check`，位于 `sql.safety_check` 之前。

#### 2026-05-25 V2 实现记录

在 V1 的 `sql.semantic_check` 后继续补齐执行前治理链路：

1. 新增 SQL Harness 节点 `dry_run_sql`。
   - 简单 SQL 主链路从 `semantic_check -> safety_check -> authorize_sql -> approve` 改成：
     `semantic_check -> safety_check -> authorize_sql -> dry_run_sql -> approve`。
   - `dry_run_sql` 使用 `EXPLAIN <sql>` 做只读预执行，不执行用户 SQL。
   - EXPLAIN 成功时输出 `dry_run_report`，包含 `safe_to_approve`、`explain_sql`、原始 explain 结果、估算行数和 explain 中识别的表。
   - EXPLAIN 失败时阻断到用户审批前，返回 `sql_dry_run_failed` 和修复建议。
2. 复杂计划 SQL step 的正式 Harness 顺序改成：

```text
sql.semantic_check
-> sql.safety_check
-> sql.authorize_sql
-> sql.dry_run
-> sql.execute_sql
```

   成功或执行失败的 step result 中都会保留 `semantic_report` 和 `dry_run_report`，便于前端展示和 LangSmith 排障。
3. `approve(...)` 的 interrupt payload 增加 `quality_gate`：
   - `quality_gate.semantic`
   - `quality_gate.safety`
   - `quality_gate.authorization`
   - `quality_gate.dry_run`
   - `quality_gate.summary.status`

   这让用户即使不懂 SQL，也能看到系统校验结论，而不是只看到“是否执行 SQL”。
4. 新增本地 `VerifiedQueryRepository` 骨架：
   - 文件：`agents/tool/sql_tools/verified_query_repository.py`
   - 存储：JSONL，本地文件路径由调用方传入。
   - 能力：保存 `question + sql + tables + intent + verification_status + result_signature + quality_score + metadata`，按 fingerprint 去重，并支持按问题、intent、表交集查询。
   - 当前定位：治理资产和回归/eval fixture，不作为生成时权威 SQL 来源，也不绕过 SQL Harness。

当前仍未完成：

- Verified Query Repository 还没有接入运行时自动沉淀或前端人工确认入口。

#### 长期目标：专业版 SQL Quality Gate

目标是把当前正则版 `semantic_check` 升级为结构化、可配置、可回归的 SQL 治理流水线：

```text
sql.parse
-> sql.ast_shape_extract
-> sql.schema_validate
-> sql.semantic_metric_validate
-> sql.relationship_validate
-> sql.policy_authorize
-> sql.explain_dry_run
-> sql.result_sanity_check
-> verified_query_regression
```

核心原则：

- 不再在代码里写 `intent == "profit_loss"` 这类业务 hardcode。
- SQL 正确性判断分成“SQL 结构事实”和“业务语义规则”两层。
- SQL 结构事实来自 AST，不靠字符串正则猜。
- 业务语义规则来自可配置 `MetricDefinition` / semantic model，不写死在 Python 分支里。
- 每个 gate 都输出统一 `QualityGateReport`，包含 `passed / decision / score / problems / warnings / extracted_facts / repair_hints`。
- LLM 只能生成候选 SQL，不能绕过 SQL Harness。

目标组件：

1. `sql.parse`
   - 使用 `sqlglot` 解析 MySQL SQL。
   - 解析失败直接输出 `SQL_PARSE_FAILED`。
   - 保留原始 SQL、规范化 SQL、dialect、AST dump。
2. `sql.ast_shape_extract`
   - 从 AST 提取 `SqlShape`：
     - tables / aliases
     - columns / qualified columns
     - joins / join conditions
     - filters / date filters / status filters
     - aggregations
     - case expressions
     - group_by / having / order_by / limit
   - 这个阶段只描述 SQL 写了什么，不判断业务对错。
3. `sql.schema_validate`
   - 用 semantic model 校验表和字段存在。
   - 做 alias resolution，例如 `ji.credit_amount -> t_journal_item.credit_amount`。
   - 阻断未知表、未知字段、歧义字段。
4. `sql.semantic_metric_validate`
   - 引入 `MetricDefinition`：

```yaml
metric_id: net_profit
business_names: ["净利润", "利润", "盈亏", "亏损"]
expression:
  type: aggregate
  op: sum
  formula: "credit_amount - debit_amount"
required_tables:
  - t_journal_item
  - t_journal_entry
  - t_account
required_filters:
  - field: t_account.account_type
    op: "="
    value: "损益"
  - field: t_journal_entry.status
    op: "="
    value: "已过账"
time_field: t_journal_entry.entry_date
```

   - 用户问题先匹配到 metric，再用 AST shape 比对 SQL 是否覆盖 metric expression、过滤条件、时间字段和粒度。
5. `sql.relationship_validate`
   - JOIN 必须能映射到 `table_relationships`。
   - 禁止未经聚合的 fact-to-fact 直连。
   - 对桥表、维表、成本中心等关系给出解释。
6. `sql.policy_authorize`
   - 保留当前正式权限 gate。
   - 表权限、列权限、行级策略、数据域策略都在这里统一判断。
7. `sql.explain_dry_run`
   - 保留当前 `EXPLAIN <sql>`。
   - 后续抽象为 dialect adapter，支持不同数据库。
8. `sql.result_sanity_check`
   - 对执行结果做空结果、NULL、金额方向、重复放大、异常行数等检查。
   - 高风险结果进入 SQL repair / human review。
9. `verified_query_regression`
   - Verified Query Repository 从“本地骨架”升级为治理资产。
   - 已验证 SQL 用于回归/eval，不直接绕过 Harness。

拆分任务：

- [x] Task 1：引入 `sqlglot`，新增 `SqlShape` 数据结构和 `extract_sql_shape(sql, dialect="mysql")`。
- [x] Task 2：用 `SqlShape` 修复当前表别名误判：`SUM(ji.credit_amount - ji.debit_amount)` 应能识别为净利润表达式。
- [x] Task 3：新增 `MetricDefinition` / `MetricRegistry`，把 `net_profit` 从 hardcode 分支迁移到配置。
- [x] Task 4：实现 `semantic_metric_validate(shape, metric_defs, query_intent)`，替换 `intent == "profit_loss"` 主分支。
- [x] Task 5：实现 schema/table/column validation，支持 alias resolution 和未知字段阻断。
- [x] Task 6：实现 relationship/join validation，接入现有 `table_relationships`。
- [x] Task 7：把 `semantic_check.py` 改成 pipeline orchestrator，聚合各 gate report。
- [x] Task 8：把 Verified Query Repository 接入 eval/regression，不进入执行绕过路径。
- [x] Task 9：补前端 approval 卡片需要的 payload 字段和展示文档。
- [x] Task 10：新增 `sql.result_sanity_check` gate，输出独立 report，并接入简单 SQL 与复杂计划 SQL step。
- [x] Task 11：为 `dry_run_sql` 增加 dialect adapter，默认 MySQL 行为不变，并补端到端验证记录。

当前剩余任务边界：

- Task 7 只做 SQL Quality Gate 内部结构化，不改变 `check_sql_semantics(...)` 对外返回主字段，避免影响 SQL Harness 调用链。
- Task 8 只把 Verified Query Repository 作为回归/eval 数据源接入，不把历史 SQL 当成执行白名单，也不绕过 `semantic_check / safety_check / authorize_sql / dry_run`。
- Task 9 只展示 approval payload 中已有的质量门结果，用户仍然只是在确认是否继续执行，不承担 SQL 正确性判断责任。

Task 9 落地说明：

- SQL approval interrupt payload 继续使用现有 `quality_gate`：
  - `quality_gate.summary.status`
  - `quality_gate.semantic`
  - `quality_gate.safety`
  - `quality_gate.authorization`
  - `quality_gate.dry_run`
- `agents/static/index.html` 的 SQL 审批卡片展示“SQL 质量门”，按语义、安全、权限、预执行四类显示通过、关注或阻断。
- 该卡片是系统治理结果展示，不改变审批语义：用户确认的是是否继续执行，SQL 正确性仍由 Harness gates 先行阻断。

Task 10/11 落地说明：

- `sql.result_sanity_check` 已成为独立 SQL Harness gate：
  - 简单 SQL 图中位于 `execute_sql -> result_sanity_check -> result_reflection/END`。
  - 复杂计划 SQL step 中位于 `sql.execute_sql` 之后，并在 trace 中显示为 `sql.result_sanity_check`。
  - 输出 `result_sanity_report`，包含 `passed / decision / summary / problems / warnings`。
- `dry_run_sql` 已支持 dialect adapter：
  - 默认 `mysql/postgres` 使用 `EXPLAIN <sql>`。
  - `sqlite` 使用 `EXPLAIN QUERY PLAN <sql>`。
  - 当 MCP 只读 wrapper 拒绝 `EXPLAIN` 时，MySQL 路径会回退到直连 MySQL 执行 `EXPLAIN`，但仍不执行用户 SQL。
- 端到端真实 HTTP 验证发现并修复两类问题：
  - SQL 引用未声明表别名 `je` 时，schema gate 会以 `UNKNOWN_TABLE_ALIAS` 阻断。
  - SQL 生成 prompt 明确要求引用 `je.entry_date/status` 前必须 `JOIN t_journal_entry je ON ji.entry_id = je.id`。

端到端验证记录：

```bash
APP_HOST=127.0.0.1 APP_PORT=8081 AGENTSCOPE_RUNTIME_BACKEND=local LANGCHAIN_TRACING_V2=false STARTUP_CHECK_TIMEOUT=1 .venv/bin/python -m agents.main

curl -sS http://127.0.0.1:8081/health
# {"status":"ok"}

curl -sS -X POST http://127.0.0.1:8081/api/query/invoke \
  -H 'Content-Type: application/json' \
  -H 'X-Allowed-Tables: t_journal_entry,t_journal_item,t_account' \
  --data '{"query":"去年亏损","session_id":"e2e-sql-quality-gate-2","route":"data","intent":"data","rewritten_query":"去年公司亏损情况"}'
# pending_approval=true, approval_type=complex_plan

curl -sS -N -X POST http://127.0.0.1:8081/api/query/approve/stream \
  -H 'Content-Type: application/json' \
  --data '{"session_id":"e2e-sql-quality-gate-2","approved":true,"feedback":""}'
# status=completed，返回亏损金额 617000.00
```

验证：

```bash
.venv/bin/python -m pytest tests/test_sql_react.py::TestSemanticCheck -q
# 4 passed

.venv/bin/python -m pytest tests/test_sql_react.py tests/test_verified_query_repository.py -q
# 130 passed

.venv/bin/python -m pytest tests/test_sql_shape.py tests/test_sql_react.py::TestSemanticCheck -q
# SQL AST shape extractor + semantic check alias regression passed
```

## Iteration：AgentScope 财务分析 skill 去硬编码与证据驱动收敛

### 背景

真实 case 暴露出两个问题：

1. `finance_relation_analysis_skill` 曾用 Python 关键词元组判断“是否复杂关系查询”，这会把复杂 SQL 路由重新写回 hardcode。
2. 单指标查询“去年亏损”会被业务知识里的辅助关联表污染，复杂案例也可能固定生成收入/回款等用户未问到的 step。

### 本轮调整

- 复杂度判断改为三层证据：
  - 可配置 `t_query_route_rule`，置信度 `>= 0.8` 时作为 `task_type`。
  - `recall_context` 中命中的业务术语、相关表和通用分析动作信号。
  - schema relationship graph 的连通性和 JOIN 风险。
- `business_knowledge` 的 `formula` 不再参与“业务术语是否命中 query”的表范围判断，只保留为口径说明，避免公式里的“费用/预算”等词污染选表。
- `finance_relation_analysis_skill` 的单 SQL 路径新增 focused scope：
  - 选择和用户 query 直接命中的主业务证据组。
  - 在该组内保留最佳可执行连通组件。
  - “去年亏损”真实 API 计划表收敛为 `t_journal_entry, t_journal_item, t_account`。
- `plan_execute` 路径改为证据驱动分组：
  - 按业务术语 `关联表` 的主事实表分组。
  - 同一事实表上的多个指标合并为一个 SQL step。
  - 只补直接相连的维表/桥表，不跨事实组扩展。
- skill 内部工具顺序调整为先取关系图再加载字段语义：`schema.select_candidates -> schema.related_tables -> focused scope -> semantic_model.search -> plan.assess_feasibility`，减少被剪枝表进入后续 observation。

### 真实验证

```text
去年亏损
-> pending_approval=true
-> approval_type=complex_plan
-> plan step tables: t_journal_entry, t_journal_item, t_account

2025年按部门分析预算执行率，并对比已审批报销费用与预算差异
-> pending_approval=true
-> approval_type=complex_plan
-> steps:
   1. 预算执行率/预算差异: t_budget, t_cost_center
   2. 部门费用/费用总额: t_expense_claim, t_cost_center, t_department
   3. python_merge
   4. report
```

### 回归

```bash
.venv/bin/python -m pytest tests/test_skill_runtime.py tests/test_runtime_tool_catalog.py tests/test_agentscope_adapter.py::test_local_runner_submits_data_analysis_plan_to_harness_without_sqlreact_context tests/test_agentscope_adapter.py::test_local_runner_prefers_workflow_state_selected_tables_without_business_topic_hardcode tests/test_complex_query.py tests/test_sql_react.py -q
# 187 passed
```

## Iteration 77：复杂计划结果业务化展示与指标列规则化

### 问题

真实 case“2025年按部门分析预算执行率，并对比已审批报销费用与预算差异”执行后，最终回答直接暴露了内部 merge 字段：

- 顶部出现“另有 6 条记录缺少合并维度，未纳入关系判断”和大量 `merge_status=未对齐`。
- 业务人员看不到最需要的对比视图：年度、部门、预算执行率、已审批报销费用、预算、已审批报销费用与预算差异。
- trace 显示计划使用 `parent_id, department_id, cost_center_id` 作为合并键，但预算 SQL 只从 `t_budget/t_cost_center` 取数，无法输出 `parent_id`。旧 merge 逻辑只要任一依赖出现某个 key，就把它作为所有依赖的必需键，导致预算侧 6 条记录被错误标记为未对齐。

### 调整

1. `_merge_dependency_rows()` 的有效合并键改为“每个依赖结果都能提供的公共键”。当计划给出 `parent_id, department_id, cost_center_id`，但预算结果没有 `parent_id` 时，会自动降级用 `department_id + cost_center_id` 合并。
2. 复杂计划最终 formatter 新增预算执行率/已审批报销费用业务表格：
   - 年度
   - 部门
   - 预算执行率
   - 已审批报销费用
   - 预算
   - 已审批报销费用与预算差异
3. 没有报销费用的部门在业务表中显示 `0.00`，并继续计算“已审批报销费用 - 预算”差异。
4. 技术诊断下沉到“数据诊断”区，只说明缺少哪些维度、建议 SQL step 同时输出哪些字段，不再把 `merge_status` 展示给业务用户。
5. 复盘后废弃 `agents/tool/sql_tools/metric_column_rules.json`：
   - 该文件原本用于结果列角色识别，但容易被误扩展成 SQL 生成 prompt 和业务口径配置。
   - `MetricRegistry` 只保留 SQL 语义指标表达式校验，不再承担结果列识别或 prompt hint。
   - 复杂计划 SQL 生成只接收原问题、当前步骤目标、schema/evidence 和合并粒度要求，不再从静态 JSON 注入 `net_margin/loss_amount/cost_amount` 等别名建议。
   - 结果展示迁移到 planner/skill 显式产出的 `display_schema`，formatter 只渲染该契约；没有契约时只做通用预览，不再维护静态业务词表。

### 真实数据回放

使用 LangSmith trace `019e5e31-3db6-75d3-9f57-7b9070bbae83` 中的两步 SQL 结果本地回放：

```text
MERGED_ROWS 6

预算执行率与已审批报销费用对比：
| 年度 | 部门 | 预算执行率 | 已审批报销费用 | 预算 | 已审批报销费用与预算差异 |
| 2025 | 总裁办 | 94.08% | 0.00 | 573323.46 | -573323.46 |
| 2025 | 财务部 | 97.92% | 26238.19 | 373754.83 | -347516.64 |
| 2025 | 研发部 | 97.06% | 0.00 | 462095.66 | -462095.66 |
| 2025 | 销售部 | 99.72% | 0.00 | 421524.37 | -421524.37 |
| 2025 | 人力资源部 | 90.68% | 0.00 | 490722.29 | -490722.29 |
| 2025 | 生产部 | 80.45% | 0.00 | 631254.43 | -631254.43 |
```

### 回归

```bash
.venv/bin/python -m pytest tests/test_metric_registry.py tests/test_sql_react.py
# 144 passed
```

## Iteration 78：LangSmith 真实 case Bug Fix 与踩坑记录

### 背景

连续排查多个真实 LangSmith case 后，确认当前主链路已经是：

```text
Final Graph
-> classify_intent
-> AgentScope data planner / finance_relation_analysis skill
-> analysis_plan.submit
-> approve_analysis_plan
-> execute_analysis_plan
-> SQL Harness per-step sql_generate / semantic_check / safety / authorize / dry_run / execute / sanity
```

本轮重点不是新增能力，而是把真实线上 case 中暴露的“链路信息丢失、trace 误读、复杂计划上下文错配”记录清楚，并补上回归。

### Bug Fix 1：分类 LLM 的详细澄清原因被前端最终回复覆盖

真实 case：

```text
用户：删除所有部门表
LangSmith classifier 输出：
route=clarify
reason=用户请求删除所有部门表，这是数据库操作请求，不应该允许删除核心主数据表...

前端最终输出：
请补充查询对象、时间范围或口径后再问。
```

根因：

- `/api/query/classify` 已经返回 `route_reason` / `route_confidence`。
- 前端随后调用 `/api/query/invoke` 时只传了 `query/session_id/route/rewritten_query`。
- 后端 `QueryRequest` 也没有把 `route_reason` 放进 graph initial state。
- `classify_intent` 因已有 `route + rewritten_query` 直接短路，导致 `clarify_direct` 拿不到原始详细原因，只能返回通用 fallback。

修复：

- `QueryRequest` 增加 `route_reason` / `route_confidence`。
- `/api/query/invoke` initial state 保留这两个字段。
- 前端 classify 后调用 invoke 时透传 `route_reason` / `route_confidence`。
- 增加回归测试 `test_invoke_passes_prefilled_route_reason_to_graph`。

提交：

```text
8a8e5d3 Preserve classify route reason in clarify flow
```

验证：

```bash
.venv/bin/python -m pytest tests/test_final_api.py tests/test_api.py
# 43 passed
```

踩坑：

- `classify` 和 `invoke` 是两个 HTTP 请求，不能只看 classifier LangSmith run 判断最终回复。
- 只要前端做了预分类，就必须把分类结果作为正式上下文传给后端，否则 graph 短路会丢解释信息。

### Bug Fix 2：复杂计划报告把内部 merge 诊断暴露给业务用户

真实 case：

```text
2025年按部门分析预算执行率，并对比已审批报销费用与预算差异
```

旧输出问题：

- 回答顶部出现大量“未对齐”“缺少合并维度”等工程诊断。
- 业务用户最需要的列没有被优先展示：
  - 年度
  - 部门
  - 预算执行率
  - 已审批报销费用
  - 预算
  - 已审批报销费用与预算差异
- merge 逻辑把 `parent_id / department_id / cost_center_id` 都当成必需键；但预算 SQL 没有 `parent_id`，导致预算侧大量记录被错误标成未对齐。

修复：

- `_merge_dependency_rows()` 改为使用“所有依赖结果都能提供的公共合并键”，而不是任一依赖出现的 key。
- 复杂计划 formatter 优先生成业务表格。
- 未匹配到报销费用的部门显示 `0.00`，继续计算费用与预算差异。
- 技术诊断下沉到“数据诊断”，不再把 `merge_status` 暴露给业务用户。
- 已废弃 `agents/tool/sql_tools/metric_column_rules.json`。这类静态 JSON 不应定义 SQL 生成口径，也不应向 LLM prompt 注入指标别名；展示层按 planner/skill 输出的 `display_schema` 渲染，不维护 alias 词表。

提交：

```text
4ab5270 Improve complex plan business report formatting
```

验证：

```bash
.venv/bin/python -m pytest tests/test_metric_registry.py tests/test_sql_react.py
# 144 passed
```

踩坑：

- 复杂计划的 merge key 是工程契约，不应该直接成为用户可见文案。
- 多步 SQL 的合并键必须按“每个依赖结果都具备”动态收敛，否则会把可合并数据误判成未对齐。
- 业务结果展示应该先给用户可比较的业务表格，数据质量问题放到下方诊断区，并说明缺少什么字段、怎么补数。

### Bug Fix 3：审批后复杂计划第 1 步 SQL 被 semantic_check 误判 UNKNOWN_TABLE

真实 case：

```text
2025年按部门分析盈利率，亏损，成本
```

LangSmith 现象：

- 审批前 trace `019e5eb3-f1fc-7ea0-b0bb-29c8f3e270d4` 停在 `approve_analysis_plan`，节点显示红色。
- 用户 approve 后 trace `019e5eb4-31e7-7e52-9d08-3f203d81b5ff` 最终失败。
- approve 后 trace 中实际存在两个 `sql.sql_generate.llm` 节点，说明 SQL 生成并未缺失。
- 失败点是第 1 步 SQL 的 `sql.semantic_check`：

```text
UNKNOWN_TABLE: t_cost_center
UNKNOWN_TABLE: t_department
```

根因：

- AgentScope skill 生成的第 1 步计划目标是“按公共粒度统计净利润、净利率、毛利率”。
- 该 step 声明表主要是凭证/科目事实表：`t_journal_entry / t_journal_item / t_account / t_expense_claim`。
- SQL 生成器为了满足“按部门”粒度，合理引用了 `t_cost_center` 和 `t_department`。
- 但复杂计划执行层给该 step 的 `semantic_model` 仍只按 step 声明表裁剪。
- `sql.schema_validate` 只看到事实表语义模型，于是把 SQL 实际引用的部门/成本中心维表误判为 unknown table。

修复：

- 在复杂计划 SQL step 的 `sql_generate` 之后，解析生成 SQL 实际引用的表。
- 如果这些表属于已批准复杂计划的全局 `selected_tables` 范围，则补入当前 step 的：
  - `selected_tables`
  - `table_relationships`
  - `semantic_model`
- 再进入 `sql.semantic_check -> sql.safety_check -> sql.authorize_sql -> sql.dry_run -> sql.execute_sql`。
- 增加回归测试 `test_complex_plan_step_keeps_merge_dimension_tables_for_semantic_check`。

提交：

```text
bce37d9 Preserve complex step dimension context
```

验证：

```bash
.venv/bin/python -m pytest tests/test_sql_react.py tests/test_final_api.py tests/test_api.py
# 181 passed
```

踩坑：

- plan step 的声明表不是 SQL 最终引用表的完整集合；SQL Harness 必须以“生成 SQL 的 AST/引用表”为准更新当前 step 上下文。
- 不能通过放松 `semantic_check` 解决这个问题。正确修复是补齐上下文，让 schema gate 继续严格拦截真正未知的表和字段。
- 维表/桥表经常来自分析粒度和 merge key，而不是用户显式说出的业务指标。

### LangSmith Trace 解读边界

本轮排查形成几条固定判断规则：

1. `approve_analysis_plan` 上的 `GraphInterrupt` 在 LangGraph 语义上是“等待人工审批”，不是业务失败。
2. 用户点击 approve 后，`/api/query/approve/stream` 会用同一 graph thread resume，但 LangSmith 通常展示为另一条 root run。
3. 所以审批前 trace 看不到 `sql.sql_generate.llm` 是正常的；SQL 生成发生在 approve 后的执行 trace。
4. AgentScope data planner 阶段只负责提交 `analysis_plan`。当前生产 skill 不是每个 plan step 都单独开 LLM 生成 SQL，真正 SQL 由 SQL Harness 在执行阶段生成。
5. LangSmith 中要区分：
   - `agentscope.llm.data_analysis_agent.reasoning`：外层 AgentScope 选择 skill / 看 observation / 输出计划。
   - `agentscope.skill.finance_relation_analysis`：skill 内部按固定工具流取证并提交 plan。
   - `sql.sql_generate.llm`：审批后 SQL Harness 为每个 SQL step 生成 SQL。
   - `sql.semantic_check`：SQL 生成后的正式质量门。

### 后续约束

- 前端 classify/invoke 之间新增字段时，必须补 API passthrough 测试。
- 复杂计划 step 执行时，质量门上下文必须来自“计划范围 + SQL 实际引用 + 安全权限策略”的交集。
- 业务展示层不能直接泄露内部状态字段，例如 `merge_status`、`source_step`、raw execution row。
- LangSmith 排障时必须同时看审批前 run 和 approve 后 resume run，避免把审批暂停误判为执行失败。

### Bug Fix 4：盈利/费用复杂计划最终回答仍暴露 raw id 和内部 merge 状态

真实 case：

```text
2025年按部门分析盈利率，亏损，成本
```

旧输出问题：

```text
关系分析结果：
- 本次按计划完成 4/4 个步骤，合并得到 4 条可对齐记录。
- 另有 8 条记录缺少合并维度，未纳入关系判断。
- 费用合计：121089.27。
结果明细：
- parent_id：1，department_id：3，cost_center_id：3，department_name：产品部...
- cost_center_id：2，department_id：2，parent_id：无数据，净利润：-1239964.99...
```

业务问题：

- 用户问的是“按部门分析盈利率、亏损、成本”，最终回复却优先展示 `parent_id / department_id / cost_center_id / merge_status / source_step`。
- “未对齐”是工程诊断，不是业务结论，不能作为主结果给业务用户。
- 查询结果没有按用户关心的字段组织成表格，例如年度、部门、净利润、净利率、毛利率、费用合计、费用笔数。
- step 1 盈利 SQL 输出了 `NULL AS parent_id`，step 2 费用 SQL 输出了非空 `parent_id`，合并器仍把 `parent_id` 当有效 key，导致本可按 `department_id + cost_center_id` 合并的行被误判为未对齐。
- 指标列规则把 `expense_count` 也识别成费用金额，存在把笔数加进费用合计的风险。

根因：

- `_merge_dependency_rows()` 只判断某个合并键的列是否存在，没有判断该列在依赖结果中是否有非空值。
- 复杂计划 formatter 缺少“盈利能力 + 费用”这一类结果的业务表格输出，只能落到通用“关系分析结果”。
- 通用 `_format_result_rows_for_answer()` 使用原始 row preview，直接暴露数据库 id 和内部 merge 字段。
- metric column rules 对金额类费用和笔数类费用没有区分。

修复：

- 合并键有效性改为“每个依赖结果都至少能提供一个非空值”，全空的 `parent_id` 会被自动降级，不再阻断 `department_id + cost_center_id` 合并。
- 展示层不再新增盈利/费用、预算/报销等专用 formatter。Planner/skill 必须在计划中给出 `display_schema`，声明用户最终要看的列：
  - `role`：指标或维度角色。
  - `label`：最终展示列名。
  - `column`：SQL step/report 必须输出的稳定别名。
  - `type`：`dimension / amount / percent / count / text`。
- formatter 只按 `display_schema` 渲染表格；amount/count 可按维度聚合，percent 只在同组唯一时展示，不再用 Python 业务关键词推导公式。
- 没有 `display_schema` 时只输出通用结果预览，过滤内部字段，不生成业务结论或合计。
- 数据质量问题下沉到“数据诊断”，说明缺少哪些合并字段，以及建议各步骤 SQL 同时输出哪些字段用于同一分析粒度合并。
- 废弃 `metric_column_rules.json` 方向：`net_profit / net_margin / gross_margin / expense_count` 等展示识别不再通过 JSON 配置驱动，避免把结果展示规则误当成财务口径规则。
- 通用 row preview 只过滤内部字段，不维护业务列名映射；中文展示列名只能来自 `display_schema.label`。

新增回归：

- `test_merge_dependency_rows_ignores_merge_key_with_only_null_values`
- `test_complex_execution_answer_renders_metric_display_schema_without_internal_fields`
- `test_complex_execution_generic_preview_hides_internal_columns`
- `test_metric_registry_does_not_drive_result_column_or_prompt_rules`

验证：

```bash
.venv/bin/python -m pytest tests/test_sql_react.py tests/test_metric_registry.py tests/test_final_api.py tests/test_api.py
# 191 passed, 2 warnings
```

踩坑：

- “列存在”不等于“合并维度有效”。LLM 有时会为了对齐计划结构输出 `NULL AS parent_id`，这种字段必须被视为无效合并键。
- 专用业务 formatter 只能解决当前意图族，不能继续扩展；展示列契约必须由 Planner/skill 输出。
- 指标规则要区分 SQL 语义校验和结果展示。`expense_count` 这样的字段是否展示为数量，应由 `display_schema.type=count` 声明，而不是靠 alias 猜测。
- 主结果应该面向业务问题，技术诊断只能作为附属说明，并明确告诉用户缺什么维度、怎么补数据。

### 后续修正：移除预算/费用专用 formatter，展示层统一改为 display_schema contract

背景：

用户指出 `_format_budget_expense_comparison_answer` 这类方法名和设计本身就是 hardcode：如果 query 换成其他字段，继续新增 `_format_xxx_answer` 不可持续。`metric_column_rules.json`、结果列 alias map、query 关键词判断也都属于同类问题。

设计结论：

- `display_schema` 是 Planner/Skill/LLM 产出的结构化输出契约，不是 Python formatter 根据业务词猜出来的。
- SQL step/report 必须尽量输出与 `display_schema.column` 对齐的稳定别名。
- `_format_budget_expense_comparison_answer` 这种方法名本身就是错误抽象：它把“某个 query 的展示需求”固化成 Python 分支。换成盈利率、现金流、应收账龄或任何其他组合字段后都会继续膨胀，最终变成不可维护的 formatter 菜单。
- 展示层只允许保留业务无关的通用函数，例如读取 `display_schema`、按维度分组、按字段类型格式化、输出诊断说明；不得新增 `budget_expense`、`profitability_expense` 这类场景命名分支。
- formatter 只负责通用渲染：
  - 维度字段作为分组键。
  - amount/number/currency/count 可在同维度下聚合。
  - percent/rate/ratio 只有同组唯一值时展示，避免错误平均。
  - 没有 `display_schema` 时只做通用预览，并过滤 `_id`、`merge_status`、`source_step`、`missing_merge_keys` 等内部字段。
- `MetricRegistry` 只保留 SQL AST/shape 层的语义指标表达式校验，不参与结果列匹配和 prompt hint。

代码调整：

- 删除 `_format_complex_business_summary` 残留调用，避免复杂计划 fallback 继续做业务合计推断。
- 通用 preview 去掉 `department_name -> 部门`、`budget_amount -> 预算` 这类业务列名映射；中文列名必须来自 `display_schema.label`。
- 数据诊断文案从“部门/成本中心口径”改为“同一分析粒度”，避免 fallback 暗含部门场景。
- AgentScope data planner prompt 增加 `display_schema` 输入要求，同时保持轻量 prompt token 预算。
- tests 迁移为：
  - 有 `display_schema` 时渲染业务表格。
  - 无 `display_schema` 时不生成业务合计或专用表格，只做通用预览。

新增/更新回归：

- `test_complex_execution_answer_renders_requested_display_schema_before_diagnostics`
- `test_complex_execution_answer_renders_metric_display_schema_without_internal_fields`
- `test_complex_execution_answer_requires_display_schema_for_business_metric_table`
- `test_complex_execution_answer_uses_generic_preview_without_display_schema`
- `test_complex_answer_formatter_has_no_business_specific_branches`
- `test_metric_registry_does_not_drive_result_column_or_prompt_rules`

验证：

```bash
.venv/bin/python -m pytest tests/test_sql_react.py::TestComplexRoute::test_execute_complex_plan_step_executes_sql_steps_and_merges_results tests/test_sql_react.py::TestComplexRoute::test_complex_execution_answer_renders_display_schema_for_merge_result tests/test_sql_react.py::TestComplexRoute::test_complex_execution_answer_uses_generic_preview_without_display_schema tests/test_sql_react.py::TestComplexRoute::test_complex_execution_answer_renders_requested_display_schema_before_diagnostics tests/test_sql_react.py::TestComplexRoute::test_complex_execution_answer_renders_metric_display_schema_without_internal_fields tests/test_sql_react.py::TestComplexRoute::test_complex_execution_answer_renders_requested_profitability_loss_and_cost_metrics tests/test_sql_react.py::TestComplexRoute::test_complex_execution_answer_requires_display_schema_for_business_metric_table tests/test_sql_react.py::TestComplexRoute::test_report_step_with_merge_keys_produces_local_merge_preview tests/test_sql_react.py::TestComplexRoute::test_report_step_without_merge_keys_summarizes_single_metric_row tests/test_sql_react.py::TestComplexRoute::test_complex_execution_generic_preview_hides_internal_columns
# 10 passed

.venv/bin/python -m pytest tests/test_agentscope_adapter.py::test_package_runner_prompts_with_toolkit_function_names_for_data_analysis tests/test_agentscope_adapter.py::test_package_runner_exposes_finance_relation_skill_instead_of_primitive_tools_for_data_analysis
# 2 passed
```

踩坑：

- 给 AgentScope prompt 增加 `display_schema` 后第一次超过了 900 字符预算。修复方式是压缩系统提示，而不是放宽 token 成本约束。
- “通用展示”不能偷偷保留业务字段中文映射，否则只是把 hardcode 从专用 formatter 移到了 fallback。

### Bug Fix 5：部门维度复杂计划在 Planner 阶段丢失业务 grain

真实 case：

```text
2025年按部门分析盈利率，亏损，成本
LangSmith trace: 019e5eeb-d630-76e2-8c23-305d93879bad
```

LangSmith MCP 复核结果：

```text
sql_harness.approve_analysis_plan
input: 2025年按部门分析盈利率，亏损，成本

step 1:
goal: 按公共粒度统计净利润、净利率、毛利率
tables: t_journal_entry, t_journal_item, t_account, t_expense_claim
merge_keys: parent_id, department_id, cost_center_id

step 2:
goal: 按公共粒度统计部门费用
tables: t_expense_claim, t_cost_center, t_department
merge_keys: parent_id, department_id, cost_center_id
```

问题：

- 用户明确说“按部门”，但 AgentScope skill 提交给 SQL Harness 的 step goal 仍是“按公共粒度”。
- 第 1 步没有声明 `t_cost_center / t_department`，但 merge key 却要求 `department_id / cost_center_id`。
- `_merge_keys_from_relationships()` 从所有关系字段里按顺序取列，导致 `t_department.parent_id -> t_department.id` 里的 `parent_id` 被误当成跨步骤合并键。
- 后续 SQL Harness 为满足部门维度会自然生成部门/成本中心 JOIN，但 plan step 的 schema 上下文先天缺维表，容易引出 semantic_check unknown table、merge 未对齐和用户不可读输出。

根因：

- Planner 把 `merge_keys` 当成“数据库关系列”，而不是“步骤结果之间的业务对齐粒度”。
- step goal 文案固定写成“公共粒度”，没有由 grain 推导。
- 计划生成只按指标证据分组表，没有根据 grain 自动补齐维表和桥表。

修复：

- 去掉 `finance_grain_rules.json` 文件设计，不把业务 grain 选择下沉成静态规则文件。
- AgentScope prompt 明确要求 LLM/Planner 在调用 `finance_relation_analysis` 时根据用户语义推断：
  - `grain`：例如部门、成本中心、期间、项目。
  - `merge_keys`：步骤结果之间的行对齐键，例如 `department_id / cost_center_id / period`。
- `FinanceRelationAnalysisSkill` 的职责收敛为结构校验和兜底：
  - 优先使用 LLM/Planner 传入的 `merge_keys`。
  - 只保留 schema relationship 中存在、且不是层级自关联的业务维度列。
  - 如果 Planner 没有传 `merge_keys`，再按 relationship graph 选择多个事实/维度组都可输出的公共维度列。
  - `parent_id` 这类同表层级键不会作为默认跨步骤合并键。
- SQL step 的 goal 由 Planner 传入的 grain label 推导：
  - `按部门维度统计净利润、净利率、毛利率`
  - `按部门维度统计部门费用`
  - merge step 为 `按部门维度合并各指标组结果并计算对比指标`
- 每个 SQL step 会根据 merge key 自动补齐所需维表，并通过 relationship graph 补齐从事实表到维表的路径，不依赖单独配置文件。

新增回归：

- `test_finance_relation_skill_plan_execute_keeps_requested_department_grain`
- `test_finance_relation_skill_filters_invalid_planner_merge_keys_by_schema_graph`

验证：

```bash
.venv/bin/python -m pytest tests/test_skill_runtime.py -k "finance_relation_skill"
# 8 passed, 3 deselected

.venv/bin/python -m pytest tests/test_skill_runtime.py tests/test_sql_react.py -k "finance_relation or complex_plan_step_keeps_merge_dimension_tables or public or grain"
# 9 passed, 143 deselected

.venv/bin/python -m pytest tests/test_agentscope_adapter.py -k "finance_relation or complex"
# 2 passed, 30 deselected
```

踩坑：

- “按部门”不应该只影响最终展示，也必须进入 Planner 的 grain contract。
- `merge_keys` 不是外键列表。它必须是所有依赖步骤都能 SELECT/GROUP BY 输出的业务对齐键。
- `parent_id` 适合做部门层级分析，不适合作为默认合并键；否则会把可按部门/成本中心合并的数据误判成未对齐。
- 业务 grain 的主判断应该由 LLM/Planner 根据用户语义完成；runtime 只做 schema graph 约束、非法 key 过滤和维表上下文补齐。

### Bug Fix 6：display_schema、merge_keys 与 SQL alias 不一致导致多数部门展示为空

真实 case：

```text
2025年按部门分析盈利率，亏损，成本
LangSmith trace: 019e5f85-92fe-79d2-b829-7515370860f4
```

现象：

- 最终表格里只有市场部、财务部、技术部的“部门”列有值，其余部门行部门名为空。
- 用户看到的是“只有前三个部门不为空”，容易误以为数据库只有 3 个部门有盈利/成本数据。

Trace 复核：

- `analysis_plan.display_schema` 要求展示列：

```json
{"label": "部门", "column": "department", "type": "dimension"}
```

- `merge_keys` 要求跨步骤按 `department_id` 合并。
- step 1 SQL 输出：

```sql
d.id AS department_id,
d.name AS department
```

  该步骤返回 3 行，因此 3 个部门有 `department=部门名`。

- step 2 SQL 输出：

```sql
d.id AS department,
d.name AS department_name
```

  该步骤返回 12 行，但没有输出 `department_id`。同时它把 `department` 这个展示列别名用成了 ID。

根因：

- SQL 生成 prompt 没有把两类别名强制分开：
  - 合并键别名：必须和 `merge_keys` 完全一致，例如 `department_id`。
  - 展示列别名：必须和 `display_schema.column` 对齐，例如 `department`。
- 本地 merge 会把用于合并的 key 列消费成 `department_id`；step 2 的可读部门名留在 `department_name`，但 formatter 只读 `display_schema.column=department`，因此显示为空。
- 这不是单纯的 formatter 问题，也不是单纯的 SQL 问题，而是 Planner 契约、SQL SELECT alias 和展示契约没有形成强约束。

修复：

- `_build_complex_step_query` 增加明确约束：
  - 合并键列别名必须与 `merge_keys` 完全一致。
  - 不要用展示字段别名代替合并键别名。
  - 展示字段要按 `display_schema.column` 另行输出。
- `_format_display_schema_answer` 增加业务无关的维度 sibling 兜底：
  - 当展示字段是维度列、精确列缺失或值像 ID 时，尝试用同 stem 的可读列，例如 `department_name`、`xxx_name`、`xxx名称`。
  - 该逻辑不写部门专用分支，适用于任意 `xxx / xxx_name` 形态。
- `execute_analysis_plan` 增加关系图兜底加载：
  - 如果 AgentScope handoff 后 `table_relationships` 为空，SQL Harness 执行前按计划引用表重新加载关系。
  - 避免 semantic relationship gate 因 `missing_sql_shape_or_relationships` 被跳过。

额外发现：

- step 1 SQL 使用了可疑 JOIN：

```sql
LEFT JOIN t_department d ON ji.cost_center_id = d.id
```

  按当前 schema，`t_journal_item.cost_center_id` 应先 JOIN `t_cost_center.id`，再通过 `t_cost_center.department_id` JOIN `t_department.id`。这类问题应该由 relationship validation 拦住；之前因为执行态没有关系图，校验被跳过。

新增回归：

- `test_complex_execution_answer_uses_readable_dimension_sibling_when_display_alias_missing`
- `test_complex_step_query_uses_display_schema_contract_and_readable_dimension_guidance`
- `test_dispatcher_loads_analysis_plan_relationships_before_execution_when_missing`

验证：

```bash
.venv/bin/python -m pytest tests/test_sql_react.py::TestComplexRoute::test_complex_step_query_uses_display_schema_contract_and_readable_dimension_guidance tests/test_sql_react.py::TestComplexRoute::test_complex_execution_answer_uses_readable_dimension_sibling_when_display_alias_missing
# 2 passed

.venv/bin/python -m pytest tests/test_dispatcher.py::test_dispatcher_loads_analysis_plan_relationships_before_execution_when_missing tests/test_dispatcher.py::test_dispatcher_preserves_agentscope_context_into_analysis_execution
# 2 passed
```

踩坑：

- `display_schema.column` 不一定是合并键；`department` 可以是展示列，`department_id` 才是 merge key。两者必须允许同时出现在 SELECT 里。
- 不要让 LLM 自行决定“department”到底是 ID 还是名称；prompt 必须明确 SQL alias contract。
- 复杂计划执行不能假设 AgentScope handoff 一定带齐关系图。SQL Harness 自己必须有兜底，否则关系校验会静默跳过。
- 展示层可以做结构化兜底，但只能是通用的 sibling 解析，不能退回 `department_name -> 部门` 这类业务字段映射。

后续真实 E2E 复验发现：

- 维度名称兜底后，部门列已经能显示；但部分部门只来自某个依赖步骤，其他展示指标列缺值时仍被渲染成空白，例如：

```text
| 测试组 |  | 0.00 |  |  |
| 运维组 |  | 0.00 |  |  |
```

- 这仍然是用户不可读输出。空白单元格无法表达“指标没有返回值”“没有业务发生”还是“系统合并失败”。

进一步根因：

- 多步骤 SQL 没有保证每个依赖步骤都覆盖同一批合并粒度成员。一个步骤从凭证事实表出发，只返回有账务发生的部门；另一个步骤从部门维表出发，返回所有部门。
- formatter 对 `display_schema` 声明的列只做取值和格式化，缺值时直接输出空字符串。
- percent/rate 指标在同一展示维度下出现多个不同值时，之前也会被留空；这会把“无法通用汇总”伪装成空白。

追加修复：

- `_format_display_schema_value()` 对所有 `display_schema` 缺值统一输出 `无数据`，避免 Markdown 表格出现业务人员无法理解的空单元格。
- `_merge_display_schema_rows()` 对 percent/rate 类指标只在同组唯一值时展示；如果同一维度出现多个不同百分比，输出 `无法汇总`，不再留空。
- `_merge_grain_guidance()` 补充通用 SQL 生成约束：
  - 多步骤合并时，各 SQL 步骤应尽量从同一合并粒度全集出发。
  - 用 `LEFT JOIN` / 条件聚合覆盖没有业务发生的数据行。
  - `display_schema` 中的数值指标缺失时用 `COALESCE` 输出 0。

新增回归：

- `test_complex_execution_answer_renders_missing_display_schema_metrics_as_no_data`
- `test_complex_step_query_uses_display_schema_contract_and_readable_dimension_guidance` 增加“同一合并粒度全集 / COALESCE”断言。

踩坑补充：

- “没有值”不能在最终业务表里表现为空白。即使不能在展示层判断其业务含义，也至少要显式展示 `无数据`。
- 比例类指标不能像金额一样直接求和；当同一维度有多个不同百分比且没有公式上下文时，展示 `无法汇总` 比空白更可解释。
- 这个修复仍然不引入业务字段硬编码，展示含义完全来自 `display_schema` 的 `column/type/label`。

后续真实 E2E 继续发现：

- 直接调用 `build_final_graph()` 做 E2E 时，如果没有像 FastAPI lifespan 一样先执行 `init_chat_models()` / `init_embedding_models()`，`sql_generate` 会报：

```text
Unsupported chat model type: qwen
```

  这不是线上配置错，而是测试脚本没有初始化 provider registry。真实端到端脚本必须复用应用启动初始化路径。

- 修复空白展示后，模型一度生成过错误利润方向：

```sql
WHEN a.balance_direction = '借' THEN ji.debit_amount - ji.credit_amount
```

  这会把成本/费用类借方发生额加成正利润，导致“收入 0、成本 > 0”的部门显示正利润。

- 原 `sql.semantic_metric_validate` 只要看到某个 `SUM(credit_amount - debit_amount)` 聚合就认为净利润指标存在。多指标 SQL 中，`revenue` 聚合可能正确，但 `profit` 聚合错误；校验被 revenue 遮住，仍会放行。

追加修复：

- `SqlAggregation` 增加 `alias`，从 AST 中提取 `SUM(...) AS alias`。
- `MetricDefinition` 增加 `output_aliases`，作为受治理指标的目标输出别名集合；这是指标定义的一部分，不进入展示 formatter，也不用于 prompt 硬编码。
- `validate_metric_shape()` 优先校验目标别名聚合：
  - 如果存在 `profit/net_profit/loss` 等目标别名，只校验这些聚合是否符合净利润公式。
  - 不再让 `revenue` 这种非目标聚合替代 `profit` 通过净利润校验。
  - 如果目标聚合在同一表达式里混用 `left-right` 和未取负的 `right-left`，返回 `REVERSED_METRIC_EXPRESSION`。
  - `-(right-left)` 视为等价的正向差额，不误杀。
- 不在 formatter 中根据 `label/column` 猜测 `display_schema.type`。比例字段是否按百分比展示必须由 Planner/Skill 在 `display_schema.type=percent` 中明确声明。
- AgentScope/complex plan prompt 补充契约：`display_schema.type` 只能使用 `dimension/amount/percent/count/text`，且必须准确表达单位；不要把比例类指标声明为 `decimal`。

新增回归：

- `test_sum_difference_metric_rejects_same_aggregation_with_reverse_difference`
- `test_semantic_check_blocks_profit_formula_with_reverse_difference_branch`
- `test_semantic_check_checks_profit_alias_not_unrelated_revenue_aggregation`

真实 E2E 验证：

```text
query: 2025年按部门分析盈利率，亏损，成本
session_id: codex-e2e-dept-profit-572fff07
model: CHAT_MODEL_TYPE=qwen, QWEN_CHAT_MODEL=qwen3-coder-plus
final_status: completed
blank_cell_markers: 0
has_percent_sign: true
has_negative_profit_for_cost_department: true
```

最终回答核心表格：

```text
| 部门 | 收入 | 成本 | 利润 | 盈利率 |
| 技术部 | 0.00 | 520000.00 | -520000.00 | 0.00% |
| 市场部 | 1316000.00 | 0.00 | 1316000.00 | 100.00% |
| 财务部 | 0.00 | 1413000.00 | -1413000.00 | 0.00% |
```

验证命令：

```bash
.venv/bin/python -m pytest tests/test_metric_registry.py tests/test_sql_validation.py
# 18 passed

.venv/bin/python -m pytest tests/test_sql_react.py -q -k "display_schema or complex_step_query_uses_display_schema_contract_and_readable_dimension_guidance"
# 8 passed

.venv/bin/python -m pytest tests/test_metric_registry.py tests/test_sql_validation.py tests/test_sql_react.py tests/test_dispatcher.py -q
# 190 passed
```

## Iteration 59：复杂路由移除 query 动作词硬编码

继续检查复杂财务查询链路时发现，`infer_task_type_from_recall_context()` 仍然保留了一组 Python 常量：

```text
分析 / 对比 / 比较 / 差异 / 偏差 / 趋势 / 关系 / 关联 / 影响 / 报告
```

这会把“是否进入复杂计划”的判断重新绑回 query 文本关键词。用户一旦换一种表达，例如只输入“收入 成本 预算 回款 费用”，即使业务知识召回已经命中多个指标和多张事实表，系统仍可能因为没有动作词而不进入复杂计划。

修复原则：

- 复杂路由不再依赖 query 动作词。
- `t_query_route_rule` / recall context 中显式 `task_type` 仍优先。
- 没有显式规则时，只用结构化证据推断：`matched_terms >= 3`，并且候选表、相关表或交集表达到多表证据条件。
- query 文本只保留为原始上下文传递，不参与动作词列表判断。

代码变更：

- 删除 `DECOMPOSABLE_TASK_SIGNAL_TERMS` 和 `_has_decomposable_task_signal()`。
- `infer_task_type_from_recall_context()` 不再拼接 `query/query_variants` 做关键词判断。
- 推断原因从 `multi-term decomposable analysis inferred from recall context` 改为 `multi-term decomposable evidence inferred from recall context`，避免把“analysis”当作 query 动作词条件。

新增回归：

- `test_multi_metric_recall_routes_without_query_action_words`
  - query 使用 `收入 成本 预算 回款 费用`，不包含“分析/查询/统计/查看/计算/对比/比较”等动作词。
  - recall context 提供 5 个业务术语和 5 张相关表。
  - 期望仍进入 `complex_plan`，且 `decision_source=recall_context`。

验证：

```bash
.venv/bin/python -m pytest tests/test_complex_query.py::test_multi_metric_recall_routes_without_query_action_words -q
# 1 passed

.venv/bin/python -m pytest tests/test_complex_query.py tests/test_sql_react.py::TestComplexRoute tests/test_runtime_tool_catalog.py -q
# 94 passed
```

踩坑：

- “动作词 + 多指标”看似是一个通用规则，但本质仍是硬编码。复杂计划是否需要拆分，应该由治理规则、召回到的业务指标、相关表跨度和关系图风险决定。
- 业务人员经常输入短语而不是完整句子。短语里可能没有任何动作词，但它依然可能是一个复杂分析请求。

## Iteration 60：2026 部门盈利率 demo 与受治理 SQL draft 证据链

README 最后一个复杂案例从旧的“预算执行与报销费用差异”替换为更通用的财务经营分析：

```text
2026年按部门分析盈利率，亏损，成本
```

目标输出不是内部字段或 merge 诊断，而是业务人员能直接阅读的表格：

```text
| 部门 | 收入 | 成本 | 净利润 | 盈利率 |
```

真实 E2E 首次复验失败：

- `display_schema` 已声明了 `部门 / 收入 / 成本 / 净利润 / 盈利率`。
- 但 `analysis_plan.evidence` 没有保留“收入 = account_code='6001'”口径，SQL Harness 的 governed draft 不敢生成。
- 系统回退到 LLM `sql_generate`，模型继续把盈利率分母写成 `account_code LIKE '5%'`，或在模型不可用时直接失败。
- 这再次证明“让 LLM 临场修 SQL”既贵又不稳定，不能作为受治理指标的主路径。

根因：

- `finance_relation_analysis` 只用初始业务知识召回结果。初始召回命中“盈利率/成本/净利润”，但未必命中“收入”这个公式依赖项。
- `_step_output_schema()` 早先只按当前指标名匹配，没有把“盈利率 = 净利润 / 收入”的公式组件带入 SQL step contract。
- `analysis_plan.submit` 归一化计划时保留了 `display_schema/output_schema`，但丢掉了 `evidence`，导致执行阶段拿不到受治理口径。
- 本地 runner 后续还从最后一次 `business_knowledge.search` trace 取 evidence，二次召回后可能只剩依赖项或错误 evidence。

修复：

- `finance_relation_analysis` 增加公式依赖 evidence 扩展：
  - 对已命中业务术语读取公式组件。
  - 如果公式组件没有对应已知术语 evidence，则按组件名做二次 `business_knowledge.search`。
  - 只接受术语名或同义词精确匹配的 evidence，避免把公式文本当 query 关键词硬编码。
- `_step_schema_search_text()` 对已匹配指标加入其公式组件，让 SQL step output_schema 自动包含依赖字段，例如盈利率需要收入作为分母。
- `_formula_components()` 只解析公式主体，截断分号后的说明文本，避免把“收入为 0 时不可计算”这类说明误当依赖项。
- `analysis_plan.submit` 保留规范化后的 `evidence`，并限制为去重后的前 8 条。
- `LocalAgentScopeCompatibleRunner` 优先使用 `result.analysis_plan.evidence` 写入 `state_patch.evidence`，不再依赖最后一次 tool trace。
- SQL Harness 中的 governed account metric draft 在 `output_schema + evidence` 同时满足时生成 SQL，仍然经过 `semantic_check -> safety_check -> authorize_sql -> dry_run -> execute`。

真实 E2E 结果：

```text
query: 2026年按部门分析盈利率，亏损，成本
session_id: readme_profitability_e2e_20260526_04
status: completed
```

最终核心输出：

```text
| 部门 | 收入 | 成本 | 净利润 | 盈利率 |
| 研发部 | 900000.00 | 620000.00 | 280000.00 | 31.11% |
| 财务部 | 180000.00 | 210000.00 | -30000.00 | -16.67% |
| 采购部 | 520000.00 | 470000.00 | 50000.00 | 9.62% |
| IT部 | 650000.00 | 390000.00 | 260000.00 | 40.00% |
| 总裁办 | 260000.00 | 180000.00 | 80000.00 | 30.77% |
| 生产部 | 1200000.00 | 1450000.00 | -250000.00 | -20.83% |
| 销售部 | 1500000.00 | 950000.00 | 550000.00 | 36.67% |
| 人力资源部 | 160000.00 | 220000.00 | -60000.00 | -37.50% |
```

新增/更新回归：

- `test_finance_relation_skill_recalls_formula_dependency_terms_for_sql_contract`
- `test_finance_relation_step_output_schema_uses_formula_components_not_synonym_hacks`
- `test_analysis_plan_submit_validates_structure_and_authorized_tables`
- `test_record_script_includes_agentscope_complex_demo_entry`

演示资产：

```text
docs/assets/demos/sql-complex-dept-profitability-2026-approved.gif
```

踩坑：

- `display_schema` 只是展示契约，不等于 SQL 可生成。没有业务知识 evidence 里的物理口径，受治理 SQL draft 必须拒绝生成。
- `analysis_plan.submit` 不能把 `evidence` 当成展示噪声丢掉。它不是执行事实，但它是 SQL Harness 后续口径校验和治理 draft 的输入契约。
- 二次召回必须基于业务公式组件，而不是 query 关键词列表；否则又会退回不可维护的 hardcode。
- 模型不可用或额度不足时，受治理 draft 路径可以把稳定指标案例跑通，但仍然不能绕过 SQL Harness。
