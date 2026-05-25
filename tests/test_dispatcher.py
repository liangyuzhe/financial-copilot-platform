"""Tests for the final dispatcher graph."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import AIMessage


class _RecordingSqlGraph:
    def __init__(self):
        self.states = []

    async def ainvoke(self, state, config=None):
        self.states.append(dict(state))
        return {
            "sql": "SELECT 1;",
            "result": [{"x": 1}],
            "answer": state.get("rewritten_query", ""),
        }


class _RecordingAgentScopeRuntime:
    calls = []
    result = None

    def __init__(self, runner=None, callbacks=None):
        self.runner = runner
        self.callbacks = callbacks or []

    async def run(
        self,
        *,
        task_type,
        query,
        session_id,
        security_context=None,
        workflow_state=None,
        enabled_skills=None,
    ):
        from agents.runtime.result import AgentRunResult

        self.__class__.calls.append(
            {
                "task_type": task_type,
                "query": query,
                "session_id": session_id,
                "security_context": security_context,
                "workflow_state": workflow_state,
                "enabled_skills": enabled_skills,
                "callbacks": self.callbacks,
            }
        )
        if self.__class__.result is not None:
            return self.__class__.result
        return AgentRunResult(
            answer=f"planned:{query}",
            state_patch={
                "analysis_plan": {
                    "mode": "analysis_plan",
                    "steps": [{"step": 1, "type": "sql", "goal": query, "tables": ["t_orders"]}],
                }
            },
        )


class _RecordingRAGGraph:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, state, config=None):
        self.calls.append(dict(state))
        return {"answer": f"chat:{state['input']['query']}"}


@pytest.mark.asyncio
async def test_dispatcher_routes_data_to_agentscope_planner_with_current_query():
    """New data turns must reach AgentScopePlanner without going through SQLReact."""
    from agents.flow.dispatcher import build_final_graph

    fake_rag_graph = _RecordingRAGGraph()
    _RecordingAgentScopeRuntime.calls = []
    _RecordingAgentScopeRuntime.result = None

    with (
        patch("agents.flow.dispatcher.get_checkpointer", return_value=MemorySaver()),
        patch("agents.flow.dispatcher.build_rag_chat_graph", return_value=fake_rag_graph),
        patch("agents.flow.dispatcher.AgentScopeRuntime", _RecordingAgentScopeRuntime),
        patch("agents.flow.dispatcher.create_agentscope_runner", return_value=object()),
    ):
        app = build_final_graph()
        config = {"configurable": {"thread_id": "same-web-session"}}

        await app.ainvoke(
            {
                "query": "我们公司去年亏损",
                "route": "data",
                "rewritten_query": "我们公司去年亏损",
                "session_id": "same-web-session",
                "chat_history": [],
            },
            config=config,
        )
        await app.ainvoke(
            {
                "query": "第一季度员工工资",
                "route": "data",
                "rewritten_query": "我们公司第一季度的员工工资情况",
                "session_id": "same-web-session",
                "chat_history": [],
            },
            config=config,
        )

    assert fake_rag_graph.calls == []
    assert _RecordingAgentScopeRuntime.calls[0]["task_type"] == "data_analysis"
    assert _RecordingAgentScopeRuntime.calls[0]["query"] == "我们公司去年亏损"
    assert _RecordingAgentScopeRuntime.calls[0]["session_id"] == "same-web-session"
    assert _RecordingAgentScopeRuntime.calls[1]["task_type"] == "data_analysis"
    assert _RecordingAgentScopeRuntime.calls[1]["query"] == "我们公司第一季度的员工工资情况"
    assert _RecordingAgentScopeRuntime.calls[1]["workflow_state"]["query"] == "第一季度员工工资"


@pytest.mark.asyncio
async def test_dispatcher_forwards_security_context_to_agentscope_planner():
    """Security context from API state must reach AgentScope planner tools."""
    from agents.flow.dispatcher import agentscope_data_planner

    _RecordingAgentScopeRuntime.calls = []
    _RecordingAgentScopeRuntime.result = None

    with (
        patch("agents.flow.dispatcher.AgentScopeRuntime", _RecordingAgentScopeRuntime),
        patch("agents.flow.dispatcher.create_agentscope_runner", return_value=object()),
    ):
        await agentscope_data_planner({
            "query": "查询所有用户真实姓名",
            "session_id": "secure-session",
            "rewritten_query": "查询所有用户真实姓名",
            "chat_history": [],
            "security_context": {"user_id": "u-1", "allowed_tables": ["t_role"]},
        })

    assert _RecordingAgentScopeRuntime.calls[0]["session_id"] == "secure-session"
    assert _RecordingAgentScopeRuntime.calls[0]["security_context"] == {
        "user_id": "u-1",
        "allowed_tables": ["t_role"],
    }


@pytest.mark.asyncio
async def test_dispatcher_forwards_langsmith_callbacks_to_agentscope_runtime():
    """AgentScope internal runtime/tool spans should be connected to the graph trace."""
    from agents.flow.dispatcher import agentscope_data_planner

    handler = object()
    _RecordingAgentScopeRuntime.calls = []
    _RecordingAgentScopeRuntime.result = None

    with (
        patch("agents.flow.dispatcher.AgentScopeRuntime", _RecordingAgentScopeRuntime),
        patch("agents.flow.dispatcher.create_agentscope_runner", return_value=object()),
    ):
        await agentscope_data_planner(
            {
                "query": "分析收入成本预算关系",
                "session_id": "trace-session",
                "rewritten_query": "分析收入成本预算关系",
                "chat_history": [],
            },
            config={"callbacks": [handler]},
        )

    assert _RecordingAgentScopeRuntime.calls[0]["callbacks"] == [handler]


@pytest.mark.asyncio
async def test_dispatcher_does_not_fallback_to_local_compatible_by_default_when_agentscope_returns_no_plan(monkeypatch):
    """If AgentScope returns no plan, default data route should surface a planner error diagnostic."""
    from agents.flow.dispatcher import agentscope_data_planner
    from agents.runtime.result import AgentRunResult

    monkeypatch.delenv("AGENTSCOPE_DATA_PLANNER_FALLBACK", raising=False)
    _RecordingAgentScopeRuntime.calls = []
    _RecordingAgentScopeRuntime.result = AgentRunResult(
        answer=(
            "I encountered tool execution delays and errors during attempts to retrieve necessary information. "
            "I was unable to retrieve these details within the maximum allowed iterations."
        ),
        state_patch={"agentscope_backend": "agentscope"},
        risk_flags=[
            {
                "code": "agentscope_no_handoff",
                "severity": "error",
                "message": "AgentScope did not submit analysis_plan.",
            }
        ],
    )

    with (
        patch("agents.flow.dispatcher.AgentScopeRuntime", _RecordingAgentScopeRuntime),
        patch("agents.flow.dispatcher.create_agentscope_runner", return_value=object()),
    ):
        result = await agentscope_data_planner({
            "query": "收入成本预算回款费用之间的关系",
            "session_id": "no-plan-session",
            "rewritten_query": "收入成本预算回款费用之间的关系",
            "chat_history": [],
        })

    _RecordingAgentScopeRuntime.result = None
    assert result["status"] == "error"
    assert result["analysis_plan"] == {}
    assert len(_RecordingAgentScopeRuntime.calls) == 1
    assert "I encountered tool execution delays" not in result["answer"]
    assert result["agentscope_observation"]["backend"] == "agentscope"
    assert result["agentscope_observation"]["fallback_disabled"] is True
    assert result["agentscope_observation"]["fallback_reason"] == "missing_analysis_plan"
    assert result["agentscope_observation"]["risk_flags"][0]["code"] == "agentscope_no_handoff"


@pytest.mark.asyncio
async def test_dispatcher_returns_clarify_when_agentscope_collected_evidence_but_needs_more_input(monkeypatch):
    from agents.flow.dispatcher import agentscope_data_planner
    from agents.runtime.result import AgentRunResult

    monkeypatch.delenv("AGENTSCOPE_DATA_PLANNER_FALLBACK", raising=False)
    _RecordingAgentScopeRuntime.calls = []
    _RecordingAgentScopeRuntime.result = AgentRunResult(
        answer="请说明亏损主体和净利润口径。",
        tool_trace=[
            {"tool_name": "business_knowledge.search", "status": "success"},
            {"tool_name": "schema.list_tables", "status": "success"},
        ],
        clarification_questions=[
            "请说明亏损主体是公司整体、部门还是项目？",
            "亏损是否按净利润 < 0 计算？",
        ],
        state_patch={"agentscope_backend": "agentscope"},
    )

    with (
        patch("agents.flow.dispatcher.AgentScopeRuntime", _RecordingAgentScopeRuntime),
        patch("agents.flow.dispatcher.create_agentscope_runner", return_value=object()),
    ):
        result = await agentscope_data_planner({
            "query": "去年亏损",
            "session_id": "clarify-session",
            "rewritten_query": "去年亏损",
            "chat_history": [],
        })

    _RecordingAgentScopeRuntime.result = None
    assert result["status"] == "clarify"
    assert result["analysis_plan"] == {}
    assert result["answer"] == "请说明亏损主体和净利润口径。"
    assert result["clarification_questions"] == [
        "请说明亏损主体是公司整体、部门还是项目？",
        "亏损是否按净利润 < 0 计算？",
    ]
    assert result["agentscope_observation"]["fallback_disabled"] is True
    assert result["agentscope_observation"]["tool_trace_count"] == 2


@pytest.mark.asyncio
async def test_dispatcher_traces_agentscope_primary_and_fallback_runtime_calls_when_enabled(monkeypatch):
    from agents.flow.dispatcher import agentscope_data_planner
    from agents.runtime.result import AgentRunResult

    monkeypatch.setenv("AGENTSCOPE_DATA_PLANNER_FALLBACK", "local_compatible")
    traced_names = []
    results = [
        AgentRunResult(
            answer="",
            state_patch={"agentscope_backend": "agentscope"},
            risk_flags=[
                {
                    "code": "agentscope_adapter_error",
                    "severity": "error",
                    "message": "StreamReader decode failed",
                }
            ],
        ),
        AgentRunResult(
            answer="fallback plan ready",
            state_patch={
                "agentscope_backend": "local_compatible",
                "analysis_plan": {
                    "mode": "analysis_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "fallback", "tables": ["t_orders"]}
                    ],
                },
                "requires_harness": True,
            },
        ),
    ]

    class FallbackRecordingRuntime(_RecordingAgentScopeRuntime):
        async def run(self, **kwargs):
            self.__class__.calls.append(kwargs)
            return results.pop(0)

    async def fake_traced_async_tool_call(name, input_str, callbacks, func, metadata=None):
        traced_names.append((name, metadata or {}))
        return await func()

    with (
        patch("agents.flow.dispatcher.AgentScopeRuntime", FallbackRecordingRuntime),
        patch("agents.flow.dispatcher.create_agentscope_runner", side_effect=[object(), None]),
        patch("agents.flow.dispatcher.traced_async_tool_call", side_effect=fake_traced_async_tool_call),
    ):
        result = await agentscope_data_planner({
            "query": "去年亏损",
            "session_id": "trace-fallback-session",
            "rewritten_query": "去年亏损",
            "chat_history": [],
        })

    assert result["status"] == "needs_harness"
    assert [name for name, _ in traced_names] == [
        "agentscope.data_planner.primary",
        "agentscope.data_planner.fallback.local_compatible",
    ]
    assert traced_names[0][1]["backend"] == "auto"
    assert traced_names[1][1]["fallback_from_backend"] == "agentscope"


@pytest.mark.asyncio
async def test_dispatcher_injects_available_workflow_context_to_agentscope_planner():
    """AgentScope planner should reuse already prepared workflow context when present."""
    from agents.flow.dispatcher import agentscope_data_planner

    _RecordingAgentScopeRuntime.calls = []
    _RecordingAgentScopeRuntime.result = None

    state = {
        "query": "分析收入和预算",
        "session_id": "context-session",
        "rewritten_query": "分析收入和预算",
        "chat_history": [],
        "security_context": {"allowed_tables": ["t_budget"]},
        "selected_tables": ["t_budget"],
        "table_metadata": {"t_budget": "预算管理表"},
        "table_relationships": [{"from_table": "t_budget", "to_table": "t_account"}],
        "semantic_model": {"t_budget": {"budget_amount": {"business_name": "预算金额"}}},
        "evidence": ["术语: 预算\n关联表: t_budget"],
        "few_shot_examples": ["用户: 查预算\nSQL: select sum(budget_amount) from t_budget"],
        "recall_context": {"business_related_tables": ["t_budget"]},
        "enhanced_query": "分析预算金额",
        "feasibility_decision": {"execution_mode": "complex_plan"},
        "complexity_report": {"selected_tables_count": 1},
    }

    with (
        patch("agents.flow.dispatcher.AgentScopeRuntime", _RecordingAgentScopeRuntime),
        patch("agents.flow.dispatcher.create_agentscope_runner", return_value=object()),
    ):
        await agentscope_data_planner(state)

    workflow_state = _RecordingAgentScopeRuntime.calls[0]["workflow_state"]
    assert workflow_state["selected_tables"] == ["t_budget"]
    assert workflow_state["table_metadata"] == {"t_budget": "预算管理表"}
    assert workflow_state["table_relationships"][0]["from_table"] == "t_budget"
    assert workflow_state["semantic_model"]["t_budget"]["budget_amount"]["business_name"] == "预算金额"
    assert workflow_state["evidence"] == ["术语: 预算\n关联表: t_budget"]
    assert workflow_state["few_shot_examples"][0].startswith("用户: 查预算")
    assert workflow_state["recall_context"]["business_related_tables"] == ["t_budget"]
    assert workflow_state["enhanced_query"] == "分析预算金额"


@pytest.mark.asyncio
async def test_dispatcher_routes_agentscope_analysis_plan_to_harness_approval():
    """A submitted AgentScope analysis_plan should be approved by SQL Harness before execution."""
    from agents.flow.dispatcher import build_final_graph

    _RecordingAgentScopeRuntime.calls = []
    _RecordingAgentScopeRuntime.result = None

    with (
        patch("agents.flow.dispatcher.get_checkpointer", return_value=MemorySaver()),
        patch("agents.flow.dispatcher.AgentScopeRuntime", _RecordingAgentScopeRuntime),
        patch("agents.flow.dispatcher.create_agentscope_runner", return_value=object()),
        patch("agents.flow.dispatcher.interrupt", return_value={"approved": False, "feedback": "先不执行"}) as mock_interrupt,
    ):
        app = build_final_graph()
        result = await app.ainvoke(
            {
                "query": "查询订单数量",
                "route": "data",
                "rewritten_query": "查询订单数量",
                "session_id": "s-plan-approval",
                "chat_history": [],
                "security_context": {"allowed_tables": ["t_orders"]},
            },
            config={"configurable": {"thread_id": "s-plan-approval:turn:1"}},
        )

    payload = mock_interrupt.call_args.args[0]
    assert payload["approval_type"] == "complex_plan"
    assert payload["complex_plan"]["mode"] == "complex_plan"
    assert payload["complex_plan"]["steps"][0]["goal"] == "查询订单数量"
    assert result["plan_approved"] is False
    assert "取消" in result["answer"]


@pytest.mark.asyncio
async def test_dispatcher_approves_finance_skill_plan_execute_without_sql_execution_before_approval():
    """finance_relation_analysis_skill plan_execute output should stop at approval before SQL Harness execution."""
    from agents.flow.dispatcher import build_final_graph
    from agents.runtime.result import AgentRunResult

    _RecordingAgentScopeRuntime.calls = []
    _RecordingAgentScopeRuntime.result = AgentRunResult(
        answer="已提交财务关系分析计划。",
        state_patch={
            "analysis_plan": {
                "mode": "analysis_plan",
                "execution_mode": "plan_execute",
                "reason": "多事实域财务关系分析，需分步执行。",
                "steps": [
                    {
                        "step": 1,
                        "type": "sql",
                        "goal": "统计收入成本费用",
                        "tables": ["t_journal_item", "t_account"],
                        "depends_on": [],
                        "merge_keys": ["period", "cost_center_id"],
                    },
                    {
                        "step": 2,
                        "type": "sql",
                        "goal": "统计预算",
                        "tables": ["t_budget"],
                        "depends_on": [],
                        "merge_keys": ["period", "cost_center_id"],
                    },
                    {
                        "step": 3,
                        "type": "sql",
                        "goal": "统计回款",
                        "tables": ["t_receivable_payable"],
                        "depends_on": [],
                        "merge_keys": ["period", "cost_center_id"],
                    },
                    {
                        "step": 4,
                        "type": "python_merge",
                        "goal": "合并关系指标",
                        "tables": [],
                        "depends_on": [1, 2, 3],
                        "merge_keys": ["period", "cost_center_id"],
                    },
                    {
                        "step": 5,
                        "type": "report",
                        "goal": "输出关系分析报告",
                        "tables": [],
                        "depends_on": [4],
                        "merge_keys": [],
                    },
                ],
            }
        },
    )

    with (
        patch("agents.flow.dispatcher.get_checkpointer", return_value=MemorySaver()),
        patch("agents.flow.dispatcher.AgentScopeRuntime", _RecordingAgentScopeRuntime),
        patch("agents.flow.dispatcher.create_agentscope_runner", return_value=object()),
        patch("agents.flow.dispatcher.execute_complex_plan_step", new_callable=AsyncMock) as mock_execute,
        patch("agents.flow.dispatcher.interrupt", return_value={"approved": False}) as mock_interrupt,
    ):
        app = build_final_graph()
        result = await app.ainvoke(
            {
                "query": "收入成本预算回款费用之间的关系",
                "route": "data",
                "rewritten_query": "收入成本预算回款费用之间的关系",
                "session_id": "s-finance-skill-plan",
                "chat_history": [],
                "security_context": {
                    "allowed_tables": [
                        "t_journal_item",
                        "t_account",
                        "t_budget",
                        "t_receivable_payable",
                    ]
                },
            },
            config={"configurable": {"thread_id": "s-finance-skill-plan:turn:1"}},
        )

    payload = mock_interrupt.call_args.args[0]
    assert payload["approval_type"] == "complex_plan"
    assert payload["analysis_plan"]["execution_mode"] == "plan_execute"
    assert payload["complex_plan"]["mode"] == "complex_plan"
    assert payload["complex_plan"]["steps"][3]["type"] == "python_merge"
    mock_execute.assert_not_called()
    assert result["plan_approved"] is False
    assert "取消" in result["answer"]


@pytest.mark.asyncio
async def test_dispatcher_preserves_agentscope_context_into_analysis_execution():
    """Approved analysis execution should reuse planner context instead of clearing it."""
    from agents.flow.dispatcher import execute_analysis_plan

    state = {
        "complex_plan": {
            "mode": "complex_plan",
            "steps": [{"step": 1, "type": "sql", "goal": "查询预算", "tables": ["t_budget"], "depends_on": [], "merge_keys": []}],
        },
        "plan_approved": True,
        "selected_tables": ["t_budget"],
        "table_relationships": [{"from_table": "t_budget", "from_column": "budget_id", "to_table": "t_account", "to_column": "account_id"}],
        "table_metadata": {"t_budget": "预算管理表"},
        "semantic_model": {"t_budget": {"budget_amount": {"business_name": "预算金额"}}},
        "evidence": ["术语: 预算\n关联表: t_budget"],
        "few_shot_examples": ["预算分析示例"],
        "recall_context": {"business_related_tables": ["t_budget"]},
        "enhanced_query": "分析预算金额",
        "query": "分析预算",
        "session_id": "plan-session",
        "security_context": {"allowed_tables": ["t_budget"]},
    }

    with patch("agents.flow.dispatcher.execute_complex_plan_step", new_callable=AsyncMock) as mock_execute:
        mock_execute.return_value = {
            "answer": "ok",
            "is_sql": False,
            "error": None,
            "plan_current_step": 1,
            "plan_execution_results": {},
        }
        result = await execute_analysis_plan(state)

    complex_state = mock_execute.call_args.args[0]
    assert complex_state["selected_tables"] == ["t_budget"]
    assert complex_state["table_relationships"][0]["from_table"] == "t_budget"
    assert complex_state["table_metadata"] == {"t_budget": "预算管理表"}
    assert complex_state["semantic_model"]["t_budget"]["budget_amount"]["business_name"] == "预算金额"
    assert complex_state["evidence"] == ["术语: 预算\n关联表: t_budget"]
    assert complex_state["few_shot_examples"] == ["预算分析示例"]
    assert complex_state["recall_context"]["business_related_tables"] == ["t_budget"]
    assert complex_state["enhanced_query"] == "分析预算金额"
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_dispatcher_emits_sql_harness_approval_and_execution_spans():
    """AgentScope plans should show a clear SQL Harness boundary in trace."""
    from agents.flow.dispatcher import approve_analysis_plan, execute_analysis_plan

    traced = []

    async def fake_traced_async_tool_call(name, input_str, callbacks, func, metadata=None):
        traced.append((name, input_str, metadata or {}))
        return await func()

    plan = {
        "mode": "analysis_plan",
        "execution_mode": "single_sql",
        "steps": [
            {
                "step": 1,
                "type": "sql",
                "goal": "统计亏损",
                "tables": ["t_journal_item"],
                "depends_on": [],
                "merge_keys": [],
            }
        ],
    }
    state = {
        "analysis_plan": plan,
        "query": "去年亏损",
        "session_id": "trace-harness-session",
        "security_context": {"allowed_tables": ["t_journal_item"]},
    }

    with (
        patch("agents.flow.dispatcher.traced_async_tool_call", side_effect=fake_traced_async_tool_call),
        patch("agents.flow.dispatcher.interrupt", return_value={"approved": True}),
        patch("agents.flow.dispatcher.execute_complex_plan_step", new_callable=AsyncMock) as mock_execute,
    ):
        approved = await approve_analysis_plan(state, config={"callbacks": ["handler"]})
        mock_execute.return_value = {
            "answer": "ok",
            "is_sql": False,
            "error": None,
            "plan_current_step": 1,
            "plan_execution_results": {},
        }
        executed = await execute_analysis_plan({**state, **approved}, config={"callbacks": ["handler"]})

    assert [item[0] for item in traced] == [
        "sql_harness.approve_analysis_plan",
        "sql_harness.execute_analysis_plan",
    ]
    assert traced[0][2]["span_layer"] == "sql_harness"
    assert traced[0][2]["real_call"] is True
    assert traced[0][2]["stage"] == "approval"
    assert traced[0][2]["approval_type"] == "complex_plan"
    assert traced[0][2]["step_count"] == 1
    assert traced[1][2]["span_layer"] == "sql_harness"
    assert traced[1][2]["stage"] == "execution"
    assert traced[1][2]["approved"] is True
    assert executed["status"] == "completed"


def test_arbitration_uses_llm_route_when_no_rule_signal():
    from agents.flow.dispatcher import _arbitrate_route

    route = _arbitrate_route("chat", None)

    assert route == "chat"


def test_arbitration_prefers_database_rule_signal():
    from agents.flow.dispatcher import _arbitrate_route

    rule = SimpleNamespace(intent="data")
    route = _arbitrate_route("chat", rule)

    assert route == "data"


@pytest.mark.asyncio
@patch("agents.flow.dispatcher.evaluate_intent_rules", new_callable=AsyncMock)
@patch("agents.flow.dispatcher.get_domain_summary")
@patch("agents.flow.dispatcher.get_chat_model")
async def test_classify_intent_short_circuits_authoritative_rule_without_llm(
    mock_get_model,
    mock_domain,
    mock_rules,
):
    """A DB route rule with a rewrite template should avoid the classify LLM path."""
    from agents.flow.dispatcher import classify_intent

    mock_rules.return_value = SimpleNamespace(
        intent="data",
        confidence=0.96,
        rule_name="经营关系分析",
        rewrite_template="分析公司{query}",
        to_dict=lambda: {"intent": "data", "rule_id": 9, "rewrite_template": "分析公司{query}"},
    )

    result = await classify_intent({"query": "收入成本预算回款费用之间的关系", "chat_history": []})

    assert result["route"] == "data"
    assert result["rewritten_query"] == "分析公司收入成本预算回款费用之间的关系"
    assert result["route_confidence"] == 0.96
    assert result["route_reason"] == "经营关系分析"
    mock_domain.assert_not_called()
    mock_get_model.assert_not_called()


@pytest.mark.asyncio
@patch("agents.flow.dispatcher.evaluate_intent_rules", new_callable=AsyncMock)
@patch("agents.flow.dispatcher.get_domain_summary", return_value="企业财务核算，可回答财务指标计算")
@patch("agents.flow.dispatcher.get_chat_model")
async def test_classify_intent_keeps_llm_chat_without_rule_override(mock_get_model, mock_domain, mock_rules):
    """Public-company questions should stay chat when LLM classifies them as chat."""
    from agents.flow.dispatcher import classify_intent

    mock_rules.return_value = None
    mock_model = mock_get_model.return_value
    mock_model.ainvoke = AsyncMock(return_value=AIMessage(
        content='{"route": "chat", "rewritten_query": "茅台第一季度盈利", "confidence": 0.91, "reason": "外部公司公开信息"}'
    ))

    result = await classify_intent({"query": "茅台第一季度盈利", "chat_history": []})

    assert result["route"] == "chat"
    assert result["rewritten_query"] == "茅台第一季度盈利"


@pytest.mark.asyncio
@patch("agents.flow.dispatcher.evaluate_intent_rules", new_callable=AsyncMock)
@patch("agents.flow.dispatcher.get_domain_summary", return_value="企业财务核算，可回答当前系统收入、预算等经营数据查询")
@patch("agents.flow.dispatcher.get_chat_model")
async def test_classify_intent_falls_back_to_data_when_llm_unavailable(
    mock_get_model,
    mock_domain,
    mock_rules,
):
    """Classifier LLM outages should not surface as API system errors for local data questions."""
    from agents.flow.dispatcher import classify_intent

    mock_rules.return_value = None
    mock_model = mock_get_model.return_value
    mock_model.ainvoke = AsyncMock(side_effect=RuntimeError("AccountOverdueError"))

    result = await classify_intent({"query": "查询 2025 年销售收入总额", "chat_history": []})

    assert result["route"] == "data"
    assert result["rewritten_query"] == "查询 2025 年销售收入总额"
    assert result["route_confidence"] == 0.0
    assert result["route_reason"] == "classify_llm_unavailable_domain_fallback"


@pytest.mark.asyncio
@patch("agents.flow.dispatcher.evaluate_intent_rules", new_callable=AsyncMock)
@patch("agents.flow.dispatcher.get_domain_summary", return_value="企业财务核算，可回答财务指标计算")
@patch("agents.flow.dispatcher.get_chat_model")
async def test_classify_route_allows_database_rule_to_override_llm(mock_get_model, mock_domain, mock_rules):
    """Rules are data-driven overrides, not keyword lists embedded in dispatcher code."""
    from agents.flow.dispatcher import classify_intent

    mock_rules.return_value = SimpleNamespace(
        intent="data",
        to_dict=lambda: {"intent": "data", "rule_id": 1},
    )
    mock_model = mock_get_model.return_value
    mock_model.ainvoke = AsyncMock(return_value=AIMessage(
        content='{"route": "chat", "rewritten_query": "我们公司去年是否亏损"}'
    ))

    result = await classify_intent({
        "query": "去年亏损",
        "chat_history": [
            {"role": "assistant", "content": "参考知识中未提供您公司去年亏损的相关信息。"},
            {"role": "user", "content": "第一季度员工工资"},
        ],
    })

    assert result["route"] == "data"
    assert result["rewritten_query"] == "我们公司去年是否亏损"


@pytest.mark.asyncio
@patch("agents.flow.dispatcher.evaluate_intent_rules", new_callable=AsyncMock)
@patch("agents.flow.dispatcher.get_domain_summary", return_value="企业财务核算，可回答财务指标计算")
@patch("agents.flow.dispatcher.get_chat_model")
async def test_classify_intent_applies_rule_rewrite_template_for_omitted_subject(
    mock_get_model,
    mock_domain,
    mock_rules,
):
    """A DB rule can normalize omitted-subject finance questions without code keywords."""
    from agents.flow.dispatcher import classify_intent

    mock_rules.return_value = SimpleNamespace(
        intent="data",
        rewrite_template="公司{query}",
        to_dict=lambda: {"intent": "data", "rule_id": 2, "rewrite_template": "公司{query}"},
    )
    mock_model = mock_get_model.return_value
    mock_model.ainvoke = AsyncMock(return_value=AIMessage(
        content='{"route": "chat", "rewritten_query": "第一季度毛利率"}'
    ))

    result = await classify_intent({"query": "第一季度毛利率", "chat_history": []})

    assert result["route"] == "data"
    assert result["rewritten_query"] == "公司第一季度毛利率"


@pytest.mark.asyncio
@patch("agents.flow.dispatcher.evaluate_intent_rules", new_callable=AsyncMock)
@patch("agents.flow.dispatcher.get_domain_summary", return_value="企业财务核算，可回答财务指标计算")
@patch("agents.flow.dispatcher.get_chat_model")
async def test_classify_route_does_not_skip_when_only_route_is_provided(
    mock_get_model,
    mock_domain,
    mock_rules,
):
    """Old clients without rewritten_query should still run current classification."""
    from agents.flow.dispatcher import classify_intent

    mock_rules.return_value = None
    mock_model = mock_get_model.return_value
    mock_model.ainvoke = AsyncMock(return_value=AIMessage(
        content='{"route": "chat", "rewritten_query": "茅台第一季度盈利"}'
    ))

    result = await classify_intent({
        "query": "茅台第一季度盈利",
        "route": "data",
        "chat_history": [],
    })

    assert result["route"] == "chat"
    assert result["rewritten_query"] == "茅台第一季度盈利"
    mock_model.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
@patch("agents.flow.dispatcher.evaluate_intent_rules", new_callable=AsyncMock)
@patch("agents.flow.dispatcher.get_domain_summary", return_value="企业财务核算，可回答当前系统数据分析")
@patch("agents.flow.dispatcher.get_chat_model")
async def test_classify_route_returns_data_without_simple_complex_split(mock_get_model, mock_domain, mock_rules):
    from agents.flow.dispatcher import classify_intent

    mock_rules.return_value = None
    mock_model = mock_get_model.return_value
    mock_model.ainvoke = AsyncMock(return_value=AIMessage(
        content=(
            '{"route": "data", "rewritten_query": "分析今年收入、成本、预算、回款和费用之间的关系", '
            '"confidence": 0.88, "reason": "需要处理当前系统经营数据"}'
        )
    ))

    result = await classify_intent({
        "query": "分析今年收入、成本、预算、回款和费用之间的关系",
        "chat_history": [],
    })

    assert result["route"] == "data"
    assert result["route_confidence"] == 0.88
    assert result["route_reason"] == "需要处理当前系统经营数据"
    assert "intent" not in result or result["intent"] == "data"
