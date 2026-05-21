from __future__ import annotations

import importlib.util
import asyncio
import json

import pytest
from langchain_core.documents import Document


def _catalog_with_static_schema():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    return ToolCatalog(
        providers=ToolProviders(
            table_metadata_loader=lambda: [
                {"table_name": "t_orders", "table_comment": "订单数据"},
                {"table_name": "t_customer", "table_comment": "客户数据"},
            ],
            semantic_model_loader=lambda table_names: {
                table: {
                    "id": {
                        "table_name": table,
                        "column_name": "id",
                        "business_name": "ID",
                    }
                }
                for table in table_names
            },
            table_relationship_loader=lambda table_names: [
                {
                    "from_table": "t_orders",
                    "from_column": "customer_id",
                    "to_table": "t_customer",
                    "to_column": "id",
                }
            ],
        )
    )


def _skill_registry():
    from agents.runtime.skill_registry import SkillRegistry

    return SkillRegistry.builtin()


def test_agent_run_result_serializes_structured_fields_and_sse_events():
    from agents.runtime.result import AgentRunResult

    result = AgentRunResult(
        answer="订单表和客户表通过 customer_id 关联。",
        tool_trace=[
            {
                "tool_name": "schema.related_tables",
                "status": "success",
            }
        ],
        sql_drafts=[{"sql": "select * from t_orders", "execution_mode": "draft_only"}],
        artifacts=[{"type": "markdown", "content": "关系说明"}],
        clarification_questions=["是否需要限定时间范围？"],
        risk_flags=[{"code": "sql_draft_only", "severity": "info"}],
        state_patch={"analysis_mode": "exploratory"},
        events=[{"event": "message", "data": "已完成关系探索"}],
    )

    data = result.to_dict()

    assert data["answer"] == "订单表和客户表通过 customer_id 关联。"
    assert data["tool_trace"][0]["tool_name"] == "schema.related_tables"
    assert data["sql_drafts"][0]["execution_mode"] == "draft_only"
    assert data["artifacts"][0]["type"] == "markdown"
    assert data["clarification_questions"] == ["是否需要限定时间范围？"]
    assert data["risk_flags"][0]["code"] == "sql_draft_only"
    assert data["state_patch"] == {"analysis_mode": "exploratory"}
    assert data["events"] == [{"event": "message", "data": "已完成关系探索"}]

    sse_events = result.to_sse_events()
    assert sse_events[0] == {"event": "message", "data": "已完成关系探索"}
    assert sse_events[-1] == {"event": "done", "data": "[DONE]"}
    result_event = next(event for event in sse_events if event["event"] == "result")
    assert json.loads(result_event["data"])["answer"] == "订单表和客户表通过 customer_id 关联。"


@pytest.mark.asyncio
async def test_runtime_passes_only_read_only_exploratory_tools_to_runner():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    seen = {}

    async def fake_runner(context):
        seen["task_type"] = context.task_type
        seen["query"] = context.query
        seen["session_id"] = context.session_id
        seen["thread_id"] = context.thread_id
        seen["tool_names"] = [tool.name for tool in context.tools]
        seen["tool_contracts"] = [tool.to_dict() for tool in context.tools]
        seen["prompt"] = context.system_prompt
        return {"answer": "可见表关系已读取。"}

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="exploratory_analysis",
        query="订单和客户有什么关系？",
        session_id="s-1",
        security_context={"user_id": "u-1", "allowed_tables": ["t_orders", "t_customer"]},
        workflow_state={"thread_id": "th-1"},
        enabled_skills=["schema_explorer"],
    )

    assert result.answer == "可见表关系已读取。"
    assert seen["task_type"] == "exploratory_analysis"
    assert seen["query"] == "订单和客户有什么关系？"
    assert seen["session_id"] == "s-1"
    assert seen["thread_id"] == "th-1"
    assert seen["tool_names"] == [
        "semantic_model.search",
        "business_knowledge.search",
        "schema.list_tables",
        "schema.describe_table",
        "schema.related_tables",
        "current_time.now",
    ]
    assert all(contract["read_only"] is True for contract in seen["tool_contracts"])
    assert all("sql" not in tool_name for tool_name in seen["tool_names"])
    assert "common_analysis_agent" in seen["prompt"]
    assert "不能直接执行 SQL" in seen["prompt"]


@pytest.mark.asyncio
async def test_runtime_auto_matches_skill_and_injects_skill_prompt():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    seen = {}

    async def fake_runner(context):
        seen["enabled_skills"] = list(context.enabled_skills)
        seen["prompt"] = context.system_prompt
        seen["tool_names"] = [tool.name for tool in context.tools]
        return {"answer": "已应用预算差异分析 skill。"}

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        skill_registry=_skill_registry(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="exploratory_analysis",
        query="分析预算执行差异和费用偏差",
        session_id="s-skill",
        security_context={"allowed_tables": ["t_orders"]},
    )

    assert result.answer == "已应用预算差异分析 skill。"
    assert seen["enabled_skills"] == ["budget_variance_analysis"]
    assert "budget_variance_analysis" in seen["prompt"]
    assert "预算差异" in seen["prompt"]
    assert "sql.execute" not in seen["tool_names"]


@pytest.mark.asyncio
async def test_runtime_respects_explicit_enabled_skills_and_safe_tool_boundary():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    seen = {}

    async def fake_runner(context):
        seen["enabled_skills"] = list(context.enabled_skills)
        seen["tool_names"] = [tool.name for tool in context.tools]
        seen["prompt"] = context.system_prompt
        return {"answer": "已应用收入成本关系 skill。"}

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        skill_registry=_skill_registry(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="report_generation",
        query="生成报告",
        session_id="s-skill-2",
        security_context={"allowed_tables": ["t_orders"]},
        workflow_state={"artifacts": [{"id": "a1", "type": "analysis_summary", "content": {}}]},
        enabled_skills=["revenue_cost_relation"],
    )

    assert result.answer == "已应用收入成本关系 skill。"
    assert seen["enabled_skills"] == ["revenue_cost_relation"]
    assert seen["tool_names"] == ["artifact.read", "report.render"]
    assert "收入成本关系" in seen["prompt"]
    assert "schema.list_tables" not in seen["tool_names"]


@pytest.mark.asyncio
async def test_report_generation_runtime_passes_only_report_tools_to_runner():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    seen = {}

    async def fake_runner(context):
        seen["task_type"] = context.task_type
        seen["tool_names"] = [tool.name for tool in context.tools]
        seen["prompt"] = context.system_prompt
        return {"answer": "报告已生成。"}

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="report_generation",
        query="生成收入成本报告",
        session_id="s-report",
        security_context={"user_id": "u-report", "allowed_tables": ["t_orders"]},
        workflow_state={
            "thread_id": "th-report",
            "artifacts": [{"id": "analysis-1", "type": "analysis_summary", "content": {}}],
        },
    )

    assert result.answer == "报告已生成。"
    assert seen["task_type"] == "report_generation"
    assert seen["tool_names"] == [
        "artifact.read",
        "report.render",
    ]
    assert all("schema." not in tool_name for tool_name in seen["tool_names"])
    assert all("sql" not in tool_name for tool_name in seen["tool_names"])
    assert "report_agent" in seen["prompt"]
    assert "只能读取已有 result/artifact" in seen["prompt"]


@pytest.mark.asyncio
async def test_runtime_collects_tool_trace_from_runner_invocations():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    async def fake_runner(context):
        tables = await context.invoke_tool("schema.list_tables", {})
        relationships = await context.invoke_tool(
            "schema.related_tables",
            {"table_names": ["t_orders", "t_customer"]},
        )
        return {
            "answer": (
                f"可见表 {len(tables.output['tables'])} 个，"
                f"关系 {len(relationships.output['relationships'])} 条。"
            )
        }

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="exploratory_analysis",
        query="有哪些表关系？",
        session_id="s-2",
        security_context={"user_id": "u-2", "allowed_tables": ["t_orders", "t_customer"]},
        workflow_state={"thread_id": "th-2"},
    )

    assert result.answer == "可见表 2 个，关系 1 条。"
    assert [trace["tool_name"] for trace in result.tool_trace] == [
        "schema.list_tables",
        "schema.related_tables",
    ]
    assert all(trace["status"] == "success" for trace in result.tool_trace)
    assert all(trace["session_id"] == "s-2" for trace in result.tool_trace)
    assert any(event["event"] == "tool_trace" for event in result.events)


@pytest.mark.asyncio
async def test_runtime_deduplicates_identical_tool_invocations_within_context():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    business_calls = 0

    def business_search(query, top_k=5):
        nonlocal business_calls
        business_calls += 1
        return [
            Document(
                page_content="收入按确认金额统计。",
                metadata={"source": "business_knowledge"},
            )
        ]

    async def fake_runner(context):
        first = await context.invoke_tool("business_knowledge.search", {"query": "收入", "top_k": 3})
        second = await context.invoke_tool("business_knowledge.search", {"top_k": 3, "query": "收入"})
        return {
            "answer": "ok",
            "state_patch": {
                "same_results": first.output.get("results") == second.output.get("results"),
                "second_cache_hit": second.output.get("cache_hit") is True,
            },
        }

    runtime = AgentScopeRuntime(
        tool_catalog=ToolCatalog(
            providers=ToolProviders(
                business_knowledge_search=business_search,
                table_metadata_loader=lambda: [{"table_name": "t_orders", "table_comment": "订单表"}],
            )
        ),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="complex_analysis",
        query="分析收入",
        session_id="s-cache",
        security_context={"allowed_tables": ["t_orders"]},
        workflow_state={"thread_id": "th-cache"},
    )

    assert business_calls == 1
    assert result.state_patch["same_results"] is True
    assert result.state_patch["second_cache_hit"] is True
    assert [trace["tool_name"] for trace in result.tool_trace] == [
        "business_knowledge.search",
        "business_knowledge.search",
    ]
    assert result.tool_trace[1]["status"] == "cache_hit"


@pytest.mark.asyncio
async def test_runtime_coalesces_concurrent_identical_tool_invocations_within_context():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    business_calls = 0

    async def business_search(query, top_k=5):
        nonlocal business_calls
        business_calls += 1
        await asyncio.sleep(0.01)
        return [
            Document(
                page_content="收入按确认金额统计。",
                metadata={"source": "business_knowledge"},
            )
        ]

    async def fake_runner(context):
        first, second = await asyncio.gather(
            context.invoke_tool("business_knowledge.search", {"query": "收入", "top_k": 3}),
            context.invoke_tool("business_knowledge.search", {"top_k": 3, "query": "收入"}),
        )
        return {
            "answer": "ok",
            "state_patch": {
                "same_results": first.output.get("results") == second.output.get("results"),
                "cache_hits": sum(1 for result in (first, second) if result.output.get("cache_hit") is True),
            },
        }

    runtime = AgentScopeRuntime(
        tool_catalog=ToolCatalog(
            providers=ToolProviders(
                business_knowledge_search=business_search,
                table_metadata_loader=lambda: [{"table_name": "t_orders", "table_comment": "订单表"}],
            )
        ),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="complex_analysis",
        query="分析收入",
        session_id="s-cache-concurrent",
        security_context={"allowed_tables": ["t_orders"]},
        workflow_state={"thread_id": "th-cache-concurrent"},
    )

    assert business_calls == 1
    assert result.state_patch["same_results"] is True
    assert result.state_patch["cache_hits"] == 1
    assert sorted(trace["status"] for trace in result.tool_trace) == ["cache_hit", "success"]


@pytest.mark.asyncio
async def test_runtime_omits_cache_hits_from_callback_tool_spans():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    tool_starts = []
    tool_ends = []

    class Callback:
        def on_tool_start(self, serialized, input_text, **kwargs):
            tool_starts.append(serialized["name"])

        def on_tool_end(self, output_text):
            tool_ends.append(output_text)

    async def fake_runner(context):
        await context.invoke_tool("business_knowledge.search", {"query": "收入"})
        await context.invoke_tool("business_knowledge.search", {"query": "收入"})
        return {"answer": "ok"}

    runtime = AgentScopeRuntime(
        tool_catalog=ToolCatalog(
            providers=ToolProviders(
                business_knowledge_search=lambda query, top_k=5: [
                    Document(page_content="收入按确认金额统计。", metadata={})
                ],
                table_metadata_loader=lambda: [{"table_name": "t_orders", "table_comment": "订单表"}],
            )
        ),
        runner=fake_runner,
        callbacks=[Callback()],
    )

    result = await runtime.run(
        task_type="complex_analysis",
        query="分析收入",
        session_id="s-cache-span",
        security_context={"allowed_tables": ["t_orders"]},
        workflow_state={"thread_id": "th-cache-span"},
    )

    assert [trace["status"] for trace in result.tool_trace] == ["success", "cache_hit"]
    assert tool_starts.count("agentscope.tool.business_knowledge.search") == 1
    assert len(tool_ends) == 1


@pytest.mark.asyncio
async def test_runtime_reuses_workflow_readthrough_business_search_across_subqueries_without_extra_spans():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    provider_calls = []
    tool_starts = []

    class Callback:
        def on_tool_start(self, serialized, input_text, **kwargs):
            tool_starts.append(serialized["name"])

    def business_search(query, top_k=5):
        provider_calls.append((query, top_k))
        return [
            Document(
                page_content="provider result",
                metadata={"source": "business_knowledge"},
            )
        ]

    async def fake_runner(context):
        first = await context.invoke_tool(
            "business_knowledge.search",
            {"query": "分析今年收入、成本、预算、回款和费用之间的关系"},
        )
        second = await context.invoke_tool(
            "business_knowledge.search",
            {"query": "收入", "top_k": 3},
        )
        return {
            "answer": "ok",
            "state_patch": {
                "first_source": first.output.get("source"),
                "second_source": second.output.get("source"),
            },
        }

    runtime = AgentScopeRuntime(
        tool_catalog=ToolCatalog(
            providers=ToolProviders(
                business_knowledge_search=business_search,
                table_metadata_loader=lambda: [{"table_name": "t_orders", "table_comment": "订单表"}],
            )
        ),
        runner=fake_runner,
        callbacks=[Callback()],
    )

    result = await runtime.run(
        task_type="complex_analysis",
        query="分析今年收入、成本、预算、回款和费用之间的关系",
        session_id="s-readthrough-spans",
        security_context={"allowed_tables": ["t_orders"]},
        workflow_state={
            "thread_id": "th-readthrough-spans",
            "query": "分析今年收入、成本、预算、回款和费用之间的关系",
            "selected_tables": ["t_orders"],
            "recall_context": {
                "query_key": "分析今年收入、成本、预算、回款和费用之间的关系",
                "matched_terms": ["收入", "成本", "预算", "回款", "费用"],
            },
            "evidence": ["术语: 收入成本预算回款费用\n公式: 按期间合并比较"],
        },
    )

    assert provider_calls == []
    assert result.state_patch["first_source"] == "workflow_state"
    assert result.state_patch["second_source"] == "runtime_tool_cache"
    assert [trace["status"] for trace in result.tool_trace] == ["success", "cache_hit"]
    assert tool_starts.count("agentscope.tool.business_knowledge.search") == 1


@pytest.mark.asyncio
async def test_complex_analysis_runtime_allows_only_one_sql_draft_submit_trace():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    async def fake_runner(context):
        first = await context.invoke_tool(
            "sql_draft.submit",
            {
                "sql": "select count(*) from t_orders",
                "purpose": "first draft",
                "tables": ["t_orders"],
            },
        )
        second = await context.invoke_tool(
            "sql_draft.submit",
            {
                "sql": "select count(*) from t_orders",
                "purpose": "duplicated draft",
                "tables": ["t_orders"],
            },
        )
        return {
            "answer": "ok",
            "sql_drafts": [first.output] if first.ok else [],
            "state_patch": {
                "second_ok": second.ok,
                "second_error": second.error,
            },
        }

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="complex_analysis",
        query="分析订单",
        session_id="s-one-draft",
        security_context={"allowed_tables": ["t_orders"]},
        workflow_state={"thread_id": "th-one-draft"},
    )

    assert result.state_patch["second_ok"] is False
    assert "already submitted" in result.state_patch["second_error"]
    assert [trace["tool_name"] for trace in result.tool_trace] == ["sql_draft.submit"]
    assert result.tool_trace[0]["status"] == "success"


@pytest.mark.asyncio
async def test_complex_analysis_runtime_builds_readable_answer_when_runner_returns_only_sql_draft():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    async def fake_runner(context):
        submitted = await context.invoke_tool(
            "sql_draft.submit",
            {
                "sql": "select count(*) from t_orders",
                "purpose": "订单数量分析",
                "tables": ["t_orders"],
            },
        )
        return {"sql_drafts": [submitted.output]}

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="complex_analysis",
        query="分析订单数量",
        session_id="s-readable",
        security_context={"allowed_tables": ["t_orders"]},
        workflow_state={"thread_id": "th-readable"},
    )

    assert result.answer
    assert "AgentScope" in result.answer
    assert "SQL Harness" in result.answer
    assert "draft_only" in result.answer
    assert result.sql_drafts


@pytest.mark.asyncio
async def test_data_analysis_runtime_hands_analysis_plan_to_harness():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    seen = {}

    async def fake_runner(context):
        seen["task_type"] = context.task_type
        seen["tool_names"] = [tool.name for tool in context.tools]
        seen["prompt"] = context.system_prompt
        submitted = await context.invoke_tool(
            "analysis_plan.submit",
            {
                "purpose": "订单数量分析",
                "plan": {
                    "mode": "analysis_plan",
                    "reason": "单 SQL 即可回答",
                    "steps": [
                        {
                            "step": 1,
                            "type": "sql",
                            "goal": "统计订单数量",
                            "tables": ["t_orders"],
                            "sql": "select count(*) as order_count from t_orders",
                            "depends_on": [],
                            "merge_keys": [],
                        }
                    ],
                    "requires_user_confirmation": True,
                },
            },
        )
        return {
            "answer": "",
            "state_patch": {"analysis_plan": submitted.output["plan"]},
        }

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="data_analysis",
        query="查询订单数量",
        session_id="s-data-plan",
        security_context={"user_id": "u-data", "allowed_tables": ["t_orders"]},
        workflow_state={"thread_id": "th-data-plan"},
    )

    assert seen["task_type"] == "data_analysis"
    assert seen["tool_names"] == [
        "semantic_model.search",
        "business_knowledge.search",
        "schema.list_tables",
        "schema.describe_table",
        "schema.related_tables",
        "current_time.now",
        "analysis_plan.submit",
    ]
    assert "data_analysis_agent" in seen["prompt"]
    assert "business_knowledge.search" in seen["prompt"]
    assert result.answer
    assert "SQL Harness" in result.answer
    assert result.state_patch["analysis_plan"]["mode"] == "analysis_plan"
    assert result.state_patch["requires_harness"] is True
    assert result.risk_flags[0]["code"] == "analysis_plan_not_executed"
    assert [trace["tool_name"] for trace in result.tool_trace] == ["analysis_plan.submit"]


@pytest.mark.asyncio
async def test_runtime_emits_langsmith_spans_for_runner_and_tools():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    class RecordingHandler:
        def __init__(self):
            self.events = []

        async def on_chain_start(self, serialized, inputs, **kwargs):
            self.events.append(("chain_start", serialized.get("name"), inputs, kwargs.get("metadata")))

        async def on_chain_end(self, outputs, **kwargs):
            self.events.append(("chain_end", outputs))

        async def on_tool_start(self, serialized, input_str, **kwargs):
            self.events.append(("tool_start", serialized.get("name"), input_str, kwargs.get("metadata")))

        async def on_tool_end(self, output, **kwargs):
            self.events.append(("tool_end", output))

    async def fake_runner(context):
        await context.invoke_tool("schema.list_tables", {})
        return {"answer": "已读取 schema。"}

    handler = RecordingHandler()
    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
        callbacks=[handler],
    )

    result = await runtime.run(
        task_type="exploratory_analysis",
        query="有哪些表？",
        session_id="s-trace",
        security_context={"allowed_tables": ["t_orders", "t_customer"]},
        workflow_state={"thread_id": "th-trace"},
    )

    assert result.answer == "已读取 schema。"
    chain_started_names = [event[1] for event in handler.events if event[0] == "chain_start"]
    tool_started_names = [event[1] for event in handler.events if event[0] == "tool_start"]
    assert "agentscope.runtime.exploratory_analysis" in chain_started_names
    assert "agentscope.tool.schema.list_tables" in tool_started_names
    tool_event = next(
        event for event in handler.events
        if event[0] == "tool_start" and event[1] == "agentscope.tool.schema.list_tables"
    )
    assert tool_event[3]["task_type"] == "exploratory_analysis"
    assert tool_event[3]["session_id"] == "s-trace"
    runtime_event = next(
        event for event in handler.events
        if event[0] == "chain_start" and event[1] == "agentscope.runtime.exploratory_analysis"
    )
    assert runtime_event[3]["runner_backend"] == "function"
    assert runtime_event[3]["tool_names"] == [
        "semantic_model.search",
        "business_knowledge.search",
        "schema.list_tables",
        "schema.describe_table",
        "schema.related_tables",
        "current_time.now",
    ]


@pytest.mark.asyncio
async def test_runtime_chain_span_wraps_runner_work_in_single_span():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    class RecordingHandler:
        def __init__(self):
            self.events = []

        async def on_chain_start(self, serialized, inputs, **kwargs):
            self.events.append(("chain_start", serialized.get("name")))

        async def on_chain_end(self, outputs, **kwargs):
            self.events.append(("chain_end", outputs.get("name")))

        async def on_tool_start(self, serialized, input_str, **kwargs):
            self.events.append(("tool_start", serialized.get("name")))

    async def fake_runner(context):
        await context.invoke_tool("schema.list_tables", {})
        return {"answer": "已读取 schema。"}

    handler = RecordingHandler()
    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
        callbacks=[handler],
    )

    result = await runtime.run(
        task_type="exploratory_analysis",
        query="有哪些表？",
        session_id="s-trace-wrap",
        security_context={"allowed_tables": ["t_orders", "t_customer"]},
        workflow_state={"thread_id": "th-trace-wrap"},
    )

    assert result.answer == "已读取 schema。"
    runtime_events = [
        event for event in handler.events
        if event[1] == "agentscope.runtime.exploratory_analysis"
    ]
    assert runtime_events == [
        ("chain_start", "agentscope.runtime.exploratory_analysis"),
        ("chain_end", "agentscope.runtime.exploratory_analysis"),
    ]
    assert handler.events.index(("chain_start", "agentscope.runtime.exploratory_analysis")) < handler.events.index(
        ("tool_start", "agentscope.tool.schema.list_tables")
    ) < handler.events.index(("chain_end", "agentscope.runtime.exploratory_analysis"))


@pytest.mark.asyncio
async def test_runtime_accepts_async_callback_manager_without_metadata_collision():
    from langchain_core.callbacks.base import AsyncCallbackHandler
    from langchain_core.callbacks.manager import AsyncCallbackManager

    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    class RecordingHandler(AsyncCallbackHandler):
        def __init__(self):
            super().__init__()
            self.chain_starts = []
            self.tool_starts = []

        async def on_chain_start(self, serialized, inputs, **kwargs):
            self.chain_starts.append((serialized.get("name"), inputs, kwargs.get("metadata")))

        async def on_tool_start(self, serialized, input_str, **kwargs):
            self.tool_starts.append((serialized.get("name"), input_str, kwargs.get("metadata")))

    async def fake_runner(context):
        await context.invoke_tool("schema.list_tables", {})
        return {"answer": "已读取 schema。"}

    handler = RecordingHandler()
    manager = AsyncCallbackManager.configure(inheritable_callbacks=[handler])
    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
        callbacks=[manager],
    )

    result = await runtime.run(
        task_type="exploratory_analysis",
        query="有哪些表？",
        session_id="s-manager",
        security_context={"allowed_tables": ["t_orders", "t_customer"]},
        workflow_state={"thread_id": "th-manager"},
    )

    assert result.answer == "已读取 schema。"
    assert any(name == "agentscope.runtime.exploratory_analysis" for name, _, _ in handler.chain_starts)
    assert any(name == "agentscope.tool.schema.list_tables" for name, _, _ in handler.tool_starts)
    tool_metadata = next(
        metadata
        for name, _, metadata in handler.tool_starts
        if name == "agentscope.tool.schema.list_tables"
    )
    assert tool_metadata["task_type"] == "exploratory_analysis"
    assert tool_metadata["session_id"] == "s-manager"


def test_tool_trace_summary_reports_duplicate_provider_tools_separately_from_cache_hits():
    from agents.runtime.agentscope_runtime import _tool_trace_summary

    summary = _tool_trace_summary([
        {"tool_name": "business_knowledge.search", "status": "success"},
        {"tool_name": "business_knowledge.search", "status": "cache_hit"},
        {"tool_name": "schema.related_tables", "status": "success"},
        {"tool_name": "schema.related_tables", "status": "success"},
        {"tool_name": "sql_draft.submit", "status": "deduped"},
    ])

    assert summary["tool_counts"] == {
        "business_knowledge.search": 2,
        "schema.related_tables": 2,
        "sql_draft.submit": 1,
    }
    assert summary["provider_tool_counts"] == {
        "business_knowledge.search": 1,
        "schema.related_tables": 2,
    }
    assert summary["cache_hit_counts"] == {
        "business_knowledge.search": 1,
    }
    assert summary["duplicate_provider_tool_names"] == ["schema.related_tables"]


@pytest.mark.asyncio
async def test_report_generation_runner_can_read_artifacts_and_render_report():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    async def fake_runner(context):
        artifacts = await context.invoke_tool("artifact.read", {"artifact_ids": ["analysis-1"]})
        forbidden_schema = await context.invoke_tool("schema.list_tables", {})
        rendered = await context.invoke_tool(
            "report.render",
            {"title": "收入成本分析报告", "include_echarts": True},
        )
        return {
            "answer": rendered.output["markdown"],
            "artifacts": [
                {
                    "type": "markdown_report",
                    "content": rendered.output["markdown"],
                    "source_artifact_ids": rendered.output["source_artifact_ids"],
                },
                *rendered.output["echarts"],
            ],
            "risk_flags": [
                {
                    "code": "schema_tool_blocked",
                    "severity": "info",
                    "status": forbidden_schema.trace.status,
                }
            ],
            "state_patch": {"report_artifact_count": len(artifacts.output["artifacts"])},
        }

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="report_generation",
        query="生成收入成本报告",
        session_id="s-report-2",
        security_context={"user_id": "u-report"},
        workflow_state={
            "thread_id": "th-report-2",
            "artifacts": [
                {
                    "id": "analysis-1",
                    "type": "analysis_summary",
                    "content": {
                        "conclusion": "收入总体高于成本。",
                        "metrics": {"revenue_total": 2100, "cost_total": 1750},
                        "anomalies": ["2 月成本高于收入"],
                        "next_steps": ["下钻 2 月费用明细"],
                    },
                }
            ],
        },
    )

    assert "# 收入成本分析报告" in result.answer
    assert "## 结论" in result.answer
    assert "## 关键指标" in result.answer
    assert "## 异常点" in result.answer
    assert "## 后续追查建议" in result.answer
    assert result.artifacts[0]["type"] == "markdown_report"
    assert result.artifacts[1]["type"] == "echarts_config"
    assert result.state_patch == {"report_artifact_count": 1}
    assert [trace["tool_name"] for trace in result.tool_trace] == [
        "artifact.read",
        "schema.list_tables",
        "report.render",
    ]
    assert result.tool_trace[1]["status"] == "forbidden"
    assert result.risk_flags[0]["status"] == "forbidden"
    assert result.sql_drafts == []


@pytest.mark.asyncio
async def test_complex_analysis_runtime_hands_sql_drafts_back_to_harness():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    seen = {}

    async def fake_runner(context):
        seen["tool_names"] = [tool.name for tool in context.tools]
        seen["prompt"] = context.system_prompt
        submitted = await context.invoke_tool(
            "sql_draft.submit",
            {
                "sql": "select month, sum(revenue) as revenue from t_orders group by month",
                "purpose": "收入按月分析",
                "tables": ["t_orders"],
            },
        )
        return {
            "answer": "已生成复杂分析计划和 SQL 草稿，等待 SQL Harness 审批执行。",
            "sql_drafts": [submitted.output],
            "state_patch": {
                "analysis_plan": [
                    {"step": "收入按月聚合", "draft_id": submitted.output["draft_id"]}
                ]
            },
        }

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="complex_analysis",
        query="分析今年收入和成本的关系",
        session_id="s-complex",
        security_context={"user_id": "u-complex", "allowed_tables": ["t_orders"]},
        workflow_state={"thread_id": "th-complex"},
    )

    assert "sql_draft.submit" in seen["tool_names"]
    assert "execute_sql" not in seen["tool_names"]
    assert "complex_analysis_agent" in seen["prompt"]
    assert result.answer.startswith("已生成复杂分析计划")
    assert result.sql_drafts[0]["execution_mode"] == "draft_only"
    assert result.sql_drafts[0]["requires_harness"] is True
    assert result.sql_drafts[0]["harness_steps"] == [
        "safety_check",
        "authorize_sql",
        "approve",
        "execute_sql",
    ]
    assert result.risk_flags[0]["code"] == "sql_draft_not_executed"
    assert result.state_patch["analysis_plan"][0]["draft_id"] == result.sql_drafts[0]["draft_id"]
    assert [trace["tool_name"] for trace in result.tool_trace] == ["sql_draft.submit"]


@pytest.mark.asyncio
async def test_runtime_returns_structured_risk_for_unsupported_task_type():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    async def fake_runner(context):
        raise AssertionError("unsupported task types must not reach runner")

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="strict_sql_query",
        query="去年亏损了吗？",
        session_id="s-3",
        security_context={"allowed_tables": ["t_orders"]},
    )

    assert result.answer == ""
    assert result.tool_trace == []
    assert result.sql_drafts == []
    assert result.risk_flags[0]["code"] == "unsupported_task_type"
    assert result.risk_flags[0]["task_type"] == "strict_sql_query"
    assert "AgentScopeRuntime" in result.risk_flags[0]["message"]


@pytest.mark.asyncio
async def test_default_runner_reports_missing_agentscope_as_structured_failure(monkeypatch):
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "agentscope":
            return None
        return original_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    runtime = AgentScopeRuntime(tool_catalog=_catalog_with_static_schema())

    result = await runtime.run(
        task_type="exploratory_analysis",
        query="探索订单表",
        session_id="s-4",
        security_context={"allowed_tables": ["t_orders"]},
    )

    assert result.answer == ""
    assert result.risk_flags[0]["code"] == "agentscope_unavailable"
    assert "agentscope" in result.risk_flags[0]["message"].lower()
    assert result.sql_drafts == []


@pytest.mark.asyncio
async def test_runner_sql_drafts_are_marked_as_draft_only_and_never_executed():
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    async def fake_runner(context):
        return {
            "answer": "生成了一个草稿，需要回到 SQL Harness 审批执行。",
            "sql_drafts": [{"sql": "select * from t_orders"}],
        }

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_static_schema(),
        runner=fake_runner,
    )

    result = await runtime.run(
        task_type="exploratory_analysis",
        query="给我一个订单查询草稿",
        session_id="s-5",
        security_context={"allowed_tables": ["t_orders"]},
    )

    assert result.sql_drafts == [
        {
            "sql": "select * from t_orders",
            "execution_mode": "draft_only",
            "requires_harness": True,
        }
    ]
    assert result.risk_flags == [
        {
            "code": "sql_draft_not_executed",
            "severity": "info",
            "message": "SQL drafts are not executed by AgentScopeRuntime; route them through SQL Harness.",
        }
    ]
