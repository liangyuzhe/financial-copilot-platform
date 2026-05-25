# ToolCatalog 工具清单

更新时间：2026-05-24

本文档整理当前 `ToolCatalog` 中定义的 primitive tools、各 `task_type` 的 runtime allowlist，以及 AgentScope/LLM 在生产和 debug 模式下实际看到的函数形态。

## 总览

- 工具定义位置：`agents/runtime/tool_catalog.py`
- 工具合同类型：`agents/runtime/tool_contracts.py`
- AgentScope 注册位置：`agents/runtime/agentscope_adapter.py`
- 当前 `ToolCatalog` 共定义 18 个 primitive tool contract。
- LLM 不会每次看到全部 18 个工具。
- 生产默认 `data_analysis` 下，外层 ReActAgent 只看到 executable skill：`finance_relation_analysis`。
- primitive tools 默认下沉到 skill runtime 内部，由 `finance_relation_analysis` 等 skill 按固定流程调用。
- 内部工具名使用点号，例如 `schema.describe_table`。
- 暴露给 AgentScope/LLM 的函数名会把点号替换为下划线，例如 `schema_describe_table`。
- 当前没有 SQL 执行工具；`sql_draft.submit` 和 `analysis_plan.submit` 都只是 handoff，必须回到 SQL Harness。

## 按 Task Type 暴露范围

| task_type | 数量 | 内部工具名 |
|---|---:|---|
| `exploratory_analysis` | 6 | `semantic_model.search`, `business_knowledge.search`, `schema.list_tables`, `schema.describe_table`, `schema.related_tables`, `current_time.now` |
| `complex_analysis` | 7 | `semantic_model.search`, `business_knowledge.search`, `schema.list_tables`, `schema.describe_table`, `schema.related_tables`, `current_time.now`, `sql_draft.submit` |
| `data_analysis` | 15 | `query.context_rewrite`, `business_knowledge.search`, `sql_examples.search`, `query.enhance`, `schema.list_tables`, `schema.describe_table`, `schema.select_candidates`, `semantic_model.search`, `schema.related_tables`, `plan.assess_feasibility`, `sql.normalize`, `sql.safety_check`, `sql.authorize_draft`, `current_time.now`, `analysis_plan.submit` |
| `report_generation` | 2 | `artifact.read`, `report.render` |

这张表描述的是 runtime/security 层的 primitive allowlist，不等于生产 LLM 每轮真实可见函数。真实可见函数还会经过 `ToolExposurePolicy` 过滤。

## Data Analysis 生产默认可见函数

当前生产默认策略：

```text
task_type=data_analysis
expose_data_analysis_primitive_tools=false
visible_functions=["finance_relation_analysis"]
```

也就是说，LangSmith 中 `agentscope.llm.data_analysis_agent.reasoning` 的 tool schema 应只包含：

| LLM 函数名 | 类型 | 输入 | 输出 | 描述 |
|---|---|---|---|---|
| `finance_relation_analysis` | executable skill | `query` 必填；`time_range`, `grain` 可选 | `status`, `execution_mode`, `summary`, `analysis_plan`, `clarification_questions` | 分析收入、成本、预算、回款、费用、利润/亏损等财务关系，内部调用 ToolCatalog primitive tools，最终提交 `analysis_plan` 给 SQL Harness，不执行 SQL。 |

`finance_relation_analysis` 内部允许调用的 primitive tools：

```text
current_time.now
business_knowledge.search
schema.list_tables
schema.select_candidates
semantic_model.search
schema.related_tables
plan.assess_feasibility
analysis_plan.submit
```

当前默认流程实际调用：

```text
current_time.now
business_knowledge.search
schema.select_candidates
semantic_model.search
schema.related_tables
plan.assess_feasibility
analysis_plan.submit
```

`schema.list_tables` 在 allowlist 中用于兼容可见表发现和本地 runner，不是默认必调步骤。

## Data Analysis Debug Primitive 可见函数

开发排障时可以显式开启 primitive debug 模式。此时也不会一次性暴露 15 个 data_analysis primitive tools，而是按上一成功工具做阶段过滤：

| 阶段 | 可见 primitive tools |
|---|---|
| start | `current_time.now`, `business_knowledge.search`, `schema.select_candidates` |
| after `current_time.now` | `business_knowledge.search`, `schema.select_candidates` |
| after `business_knowledge.search` | `schema.select_candidates` |
| after `schema.select_candidates` | `semantic_model.search`, `schema.related_tables`, `analysis_plan.submit` |
| after `semantic_model.search` | `schema.related_tables`, `analysis_plan.submit` |
| after `schema.related_tables` | `analysis_plan.submit` |
| after `analysis_plan.submit` | 无 |

策略实现位置：`agents/runtime/tool_exposure_policy.py`。

可用诊断命令：

```bash
.venv/bin/python scripts/diagnose_tool_exposure.py
```

当前基线：

```text
skill_only: function_count=1, schema_chars=652, estimated_tokens=152
primitive_debug_start_visible: function_count=3, schema_chars=1603, estimated_tokens=368
primitive_debug_registered: function_count=6, schema_chars=2820, estimated_tokens=642
```

## 给 LLM 的工具形态

AgentScope 适配层会把每个 `RuntimeTool` 注册成 toolkit function：

```python
toolkit.register_tool_function(
    self._tool_wrapper(context, tool.name),
    func_name=self._toolkit_func_name(tool.name),
    func_description=tool.description,
    json_schema={
        "type": "function",
        "function": {
            "name": self._toolkit_func_name(tool.name),
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    },
    namesake_strategy="override",
    async_execution=False,
)
```

映射规则：

```python
def _toolkit_func_name(self, tool_name: str) -> str:
    return tool_name.replace(".", "_")
```

所以 LLM 必须调用下划线函数名，不能调用带点号的内部名：

```text
schema.describe_table -> schema_describe_table
analysis_plan.submit -> analysis_plan_submit
```

Adapter 会按当前 toolkit 追加可用函数提示。生产 skill-only 模式下，可用函数只应包含 `finance_relation_analysis`；primitive debug 模式下才会出现下方这类 primitive 函数名列表：

```text
重要：调用工具时必须使用 AgentScope toolkit 暴露的函数名，不要使用带点号的内部工具名。
可用函数名：query_context_rewrite, business_knowledge_search, ...
完成规划后必须调用 analysis_plan_submit 提交结构化且 steps 非空的 analysis_plan。
```

### Function Schema 示例

以 `schema.describe_table` 为例，LLM 侧看到的是：

```json
{
  "type": "function",
  "function": {
    "name": "schema_describe_table",
    "description": "Purpose: Inspect one visible table in detail...\nCall it when: ...\nBoundary: ...\nRequired input: table_name is required...\nOutput: An object with table_name, table_comment, and columns...\nDo not use when: you need columns or semantics for multiple tables...",
    "parameters": {
      "type": "object",
      "properties": {
        "table_name": {
          "type": "string",
          "description": "One authorized physical table name to describe."
        }
      },
      "required": ["table_name"],
      "additionalProperties": false
    }
  }
}
```

注意：

- `parameters` 只包含输入 schema。
- 输出结构没有作为 JSON schema 单独传给 LLM。
- 输出口径写在工具 `description` 的 `Output:` 段里。
- 真实工具返回会进入 `tool_trace.output` 和 LangSmith tool span output。

## 完整工具清单

| 内部 name | LLM 函数名 | 输入 | 输出 | 描述 |
|---|---|---|---|---|
| `query.context_rewrite` | `query_context_rewrite` | `query` 必填；`summary`, `history` 可选 | `original_query`, `rewritten_query`, `summary_used`, `history_count` | 将省略主体、代词、追问式问题改写成可独立召回和规划的数据分析问题。 |
| `sql_examples.search` | `sql_examples_search` | `query` 必填；`top_k` 可选 | `results`, `few_shot_examples` | 召回历史 SQL 示例、few-shot 模板和成功查询模式，用于辅助 analysis plan 设计。 |
| `query.enhance` | `query_enhance` | `query` 必填；`evidence` 可选 | `enhanced_query`, `evidence_used` | 用业务知识证据增强独立 query，例如指标定义、公式、同义词和关联表提示。 |
| `semantic_model.search` | `semantic_model_search` | `table_names` 可选 | `tables`, `semantic_model`, `source`, `cache_hit`, `from_workflow_state`, `fetched` | 加载可见表字段语义、业务名、同义词和字段描述。多表字段语义优先用它。 |
| `business_knowledge.search` | `business_knowledge_search` | `query` 必填；`top_k` 可选 | `results`, `source`, `cache_hit` | 召回业务术语、公式、指标定义、同义词和关联表线索。 |
| `schema.list_tables` | `schema_list_tables` | 无 | `tables`, `source`, `cache_hit` | 列出当前安全上下文下可见的表及表注释/元数据。 |
| `schema.describe_table` | `schema_describe_table` | `table_name` 必填 | `table_name`, `table_comment`, `columns` | 查看单张可见表的详细字段结构和字段语义。不要用于多表字段批量探查。 |
| `schema.select_candidates` | `schema_select_candidates` | `query` 必填；`candidate_tables`, `top_k`, `evidence`, `few_shot_examples` 可选 | `selected_tables`, `table_metadata`, `semantic_model`, `candidate_scores`, `recall_context` | 根据 query、表元数据、语义字段和召回证据筛选候选物理表。 |
| `schema.related_tables` | `schema_related_tables` | `table_names` 可选 | `relationships`, `source`, `cache_hit` | 返回候选可见表之间的关系边，用于判断 join、桥接表和拆分依赖。 |
| `plan.assess_feasibility` | `plan_assess_feasibility` | `query`, `selected_tables` 必填；`relationships`, `task_type` 可选 | `feasibility_decision`, `relationships`, `selected_tables`, `route_mode` | 判断候选表是否适合单 SQL、严格单 SQL、多步骤 analysis plan，或需要澄清。 |
| `sql.normalize` | `sql_normalize` | `answer` 或 `sql` | `sql`, `is_valid`, `error` | 对 SQL 草稿做本地格式规范化和 SELECT/WITH 校验，不授权、不审批、不执行。 |
| `sql.safety_check` | `sql_safety_check` | `sql` 必填 | `is_safe`, `risks`, `estimated_rows`, `required_permissions` | 对 SQL 草稿做本地静态安全检查，识别破坏性或高风险模式，不执行 SQL。 |
| `sql.authorize_draft` | `sql_authorize_draft` | `sql` 或 `tables` | `tables`, `authorization_report`, `execution_mode`, `requires_harness` | 校验 SQL 草稿引用表是否在当前安全上下文可见。只做授权预检，不执行。 |
| `current_time.now` | `current_time_now` | 无 | `iso`, `date`, `timezone` | 获取当前日期时间，用于解析“今年”、“本月”、“当前”等相对时间。 |
| `artifact.read` | `artifact_read` | `artifact_ids`, `types` 可选 | `artifacts` | 从 `workflow_state` 读取已有执行结果或分析 artifacts，用于报告生成。 |
| `report.render` | `report_render` | `title`, `artifact_ids`, `types`, `include_echarts` 可选 | `markdown`, `echarts`, `source_artifact_ids` | 基于已有 artifacts 渲染 Markdown 报告和可选 ECharts 配置。 |
| `sql_draft.submit` | `sql_draft_submit` | `sql` 必填；`purpose`, `tables` 可选 | `draft_id`, `sql`, `purpose`, `tables`, `execution_mode`, `requires_harness`, `status`, `harness_steps` | 将 SELECT SQL 草稿交回 SQL Harness。它是 handoff tool，不执行 SQL。 |
| `analysis_plan.submit` | `analysis_plan_submit` | `plan` 必填；`purpose` 可选 | `plan_id`, `plan`, `purpose`, `execution_mode`, `requires_harness`, `status`, `harness_steps` | 提交结构化 data analysis plan 给 SQL Harness。它是 handoff tool，不执行计划。 |

## Data Analysis Primitive 函数名

以下是 `data_analysis` primitive allowlist 映射到 LLM 函数名后的全集。它们不是生产默认全部可见，只在 skill 内部、local runner 或 debug primitive 模式下使用。

```text
query_context_rewrite,
business_knowledge_search,
sql_examples_search,
query_enhance,
schema_list_tables,
schema_describe_table,
schema_select_candidates,
semantic_model_search,
schema_related_tables,
plan_assess_feasibility,
sql_normalize,
sql_safety_check,
sql_authorize_draft,
current_time_now,
analysis_plan_submit
```

这些函数中没有 SQL 执行能力。规划完成后，必须通过 `analysis_plan_submit` 提交结构化计划，后续执行、权限检查、审批和结果生成由 SQL Harness 负责。

## AgentRunResult 组装边界

`AgentRunResult` 是 runtime 返回给 dispatcher 的平台结果对象，不是要求 LLM 每次 tool 调用后输出的结构。

当前组装时机：

```text
1. ReActAgent 调用 LLM reasoning。
2. LLM 调用 `finance_relation_analysis`。
3. SkillRuntime 内部调用 primitive tools，并把结果写入 `context.tool_trace`、`context.events`、`state_patch`。
4. ReActAgent 返回最终 assistant reply。
5. AgentScopePackageRunner 将 reply 归一化。
6. AgentScopeRuntime 合并 context 中的 trace/events/state_patch，生成 `AgentRunResult`。
```

因此 LangSmith 中不应该再看到 LLM final text 自己拼出完整 `tool_trace/events/state_patch` 大 JSON。完整工具输出应出现在 tool span output 和 runtime 结果对象中，而不是作为 assistant 文本让模型复述。

## 使用边界

- `schema.describe_table` 只适合单表深挖；多表字段语义应使用 `semantic_model_search(table_names=[...])` 或 `schema_select_candidates`。
- `sql.normalize`、`sql.safety_check`、`sql.authorize_draft` 都是本地校验或预检工具，不产生用户可见的数据事实。
- `sql_draft.submit` 和 `analysis_plan.submit` 的 `direct_execution_allowed=False`，只用于 handoff。
- `artifact.read` 和 `report.render` 只在 `report_generation` 路径暴露，不能用于新取数。
- Skill 或 prompt 只能在当前 `task_type` allowlist 内缩小工具集合，不能突破 allowlist 暴露额外工具。
- 生产默认 `data_analysis` 不应直接暴露 `sql.safety_check`、`sql.authorize_draft` 给外层 ReActAgent；正式 SQL safety/authorize 属于 SQL Harness。
