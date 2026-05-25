from __future__ import annotations

import ast
import json
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document


def _finance_catalog():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    business_docs = [
        Document(
            page_content=(
                "术语: 净利润\n"
                "公式: 收入 - 成本 - 费用；净利润 < 0 表示亏损\n"
                "同义词: 亏损, 盈利, 利润\n"
                "关联表: t_journal_entry,t_journal_item,t_account,t_expense_claim"
            ),
            metadata={"source": "business_knowledge", "doc_id": "net_profit"},
        ),
        Document(
            page_content=(
                "术语: 预算执行率\n"
                "公式: actual_amount / budget_amount\n"
                "同义词: 预算, 预算完成率, 执行率\n"
                "关联表: t_budget,t_cost_center"
            ),
            metadata={"source": "business_knowledge", "doc_id": "budget_execution"},
        ),
        Document(
            page_content=(
                "术语: 预算差异\n"
                "公式: actual_amount - budget_amount\n"
                "同义词: 预算偏差, 超支金额\n"
                "关联表: t_budget,t_cost_center"
            ),
            metadata={"source": "business_knowledge", "doc_id": "budget_variance"},
        ),
        Document(
            page_content=(
                "术语: 部门费用\n"
                "公式: SUM(total_amount) GROUP BY cost_center_id\n"
                "同义词: 报销费用, 部门开销, 费用合计\n"
                "关联表: t_expense_claim,t_cost_center"
            ),
            metadata={"source": "business_knowledge", "doc_id": "department_expense"},
        ),
        Document(
            page_content=(
                "术语: 回款效率\n"
                "公式: settled_amount / original_amount\n"
                "同义词: 回款, 已收金额\n"
                "关联表: t_receivable_payable"
            ),
            metadata={"source": "business_knowledge", "doc_id": "collection_efficiency"},
        ),
    ]

    metadata = [
        {"table_name": "t_journal_entry", "table_comment": "记账凭证主表"},
        {"table_name": "t_journal_item", "table_comment": "凭证分录明细表"},
        {"table_name": "t_account", "table_comment": "会计科目表"},
        {"table_name": "t_budget", "table_comment": "预算管理表"},
        {"table_name": "t_receivable_payable", "table_comment": "应收应付表"},
        {"table_name": "t_expense_claim", "table_comment": "费用报销表"},
        {"table_name": "t_cost_center", "table_comment": "成本中心表"},
    ]

    semantic = {
        table["table_name"]: {
            "period": {
                "table_name": table["table_name"],
                "column_name": "period",
                "business_name": "期间",
            },
            "cost_center_id": {
                "table_name": table["table_name"],
                "column_name": "cost_center_id",
                "business_name": "成本中心",
            },
            "amount": {
                "table_name": table["table_name"],
                "column_name": "amount",
                "business_name": "金额",
            },
        }
        for table in metadata
    }

    return ToolCatalog(
        providers=ToolProviders(
            business_knowledge_search=lambda query, top_k=5: business_docs[:top_k],
            table_metadata_loader=lambda: metadata,
            semantic_model_loader=lambda table_names: {
                table: semantic[table]
                for table in table_names
                if table in semantic
            },
            table_relationship_loader=lambda table_names: [
                {
                    "from_table": "t_journal_item",
                    "from_column": "entry_id",
                    "to_table": "t_journal_entry",
                    "to_column": "id",
                },
                {
                    "from_table": "t_journal_item",
                    "from_column": "account_code",
                    "to_table": "t_account",
                    "to_column": "account_code",
                },
                {
                    "from_table": "t_budget",
                    "from_column": "cost_center_id",
                    "to_table": "t_cost_center",
                    "to_column": "id",
                },
            ],
        )
    )


def _run_context(*, query: str = "收入成本预算回款费用之间的关系"):
    from agents.runtime.agentscope_runtime import AgentScopeRunContext

    catalog = _finance_catalog()
    security_context = {
        "allowed_tables": [
            "t_journal_entry",
            "t_journal_item",
            "t_account",
            "t_budget",
            "t_receivable_payable",
            "t_expense_claim",
            "t_cost_center",
        ]
    }
    return AgentScopeRunContext(
        task_type="data_analysis",
        query=query,
        session_id="s-skill-runtime",
        thread_id="th-skill-runtime",
        security_context=security_context,
        workflow_state={},
        enabled_skills=[],
        tools=catalog.get_tools("data_analysis", security_context=security_context),
        tool_catalog=catalog,
        system_prompt="system prompt",
    )


def test_executable_skill_definition_serializes_allowed_tools():
    from agents.runtime.skill_contracts import RuntimeSkill, SkillTracePolicy

    skill = RuntimeSkill(
        name="finance_relation_analysis_skill",
        version="2026-05-24",
        description="财务关系分析",
        task_types=("data_analysis",),
        allowed_tools=(
            "current_time.now",
            "business_knowledge.search",
            "schema.list_tables",
            "schema.select_candidates",
            "semantic_model.search",
            "schema.related_tables",
            "plan.assess_feasibility",
            "analysis_plan.submit",
        ),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        execution_modes=("single_sql", "plan_execute", "clarification"),
        trace_policy=SkillTracePolicy(max_observation_chars=2000),
    )

    data = skill.to_dict()

    assert data["name"] == "finance_relation_analysis_skill"
    assert data["version"] == "2026-05-24"
    assert "plan_execute" in data["execution_modes"]
    assert "analysis_plan.submit" in data["allowed_tools"]
    assert data["trace_policy"]["max_observation_chars"] == 2000


@pytest.mark.asyncio
async def test_skill_runtime_invokes_child_tools_and_returns_compact_observation():
    from agents.runtime.skill_runtime import SkillRuntime
    from agents.runtime.skills.finance_relation_analysis import FinanceRelationAnalysisSkill

    context = _run_context()
    runtime = SkillRuntime(skills=[FinanceRelationAnalysisSkill()])

    result = await runtime.invoke_skill(
        "finance_relation_analysis",
        {"query": context.query},
        context,
    )

    observation = result.to_observation(max_chars=2000)

    assert result.status == "plan_ready"
    assert result.execution_mode == "plan_execute"
    assert result.analysis_plan["mode"] == "analysis_plan"
    assert result.analysis_plan["execution_mode"] == "plan_execute"
    assert any(step["type"] == "python_merge" for step in result.analysis_plan["steps"])
    assert [trace["tool_name"] for trace in context.tool_trace] == [
        "current_time.now",
        "business_knowledge.search",
        "schema.select_candidates",
        "schema.related_tables",
        "semantic_model.search",
        "plan.assess_feasibility",
        "analysis_plan.submit",
    ]
    assert observation["skill_name"] == "finance_relation_analysis_skill"
    assert "tool_trace" not in observation
    assert len(str(observation)) < 2000


@pytest.mark.asyncio
async def test_skill_runtime_span_metadata_includes_visible_functions():
    from agents.runtime.skill_runtime import SkillRuntime
    from agents.runtime.skills.finance_relation_analysis import FinanceRelationAnalysisSkill

    context = _run_context()
    spans = []

    class RecordingHandler:
        async def on_chain_start(self, serialized, inputs, **kwargs):
            spans.append(("start", serialized.get("name"), inputs, kwargs.get("metadata") or {}))

        async def on_chain_end(self, outputs, **kwargs):
            spans.append(("end", outputs, kwargs))

    context.callbacks.append(RecordingHandler())
    runtime = SkillRuntime(skills=[FinanceRelationAnalysisSkill()])

    await runtime.invoke_skill(
        "finance_relation_analysis",
        {"query": context.query},
        context,
    )

    start = spans[0]
    assert start[0] == "start"
    assert start[1] == "agentscope.skill.finance_relation_analysis"
    assert start[3]["span_layer"] == "skill"
    assert start[3]["real_call"] is True
    assert start[3]["visible_functions"] == ["finance_relation_analysis"]
    assert "analysis_plan.submit" in start[3]["allowed_tools"]
    assert ast.literal_eval(spans[-1][1]["output"])["child_tool_count"] == 7


@pytest.mark.asyncio
async def test_finance_relation_skill_keeps_loss_query_focused_on_profit_loss():
    from agents.runtime.skill_runtime import SkillRuntime
    from agents.runtime.skills.finance_relation_analysis import FinanceRelationAnalysisSkill

    context = _run_context(query="去年亏损")
    runtime = SkillRuntime(skills=[FinanceRelationAnalysisSkill()])

    result = await runtime.invoke_skill(
        "finance_relation_analysis",
        {"query": context.query},
        context,
    )

    goals = "\n".join(str(step.get("goal") or "") for step in result.analysis_plan["steps"])

    assert result.status == "plan_ready"
    assert result.execution_mode == "single_sql"
    assert len(result.analysis_plan["steps"]) == 1
    assert "亏损" in goals or "利润" in goals
    assert "预算" not in goals
    assert "回款" not in goals
    assert "t_expense_claim" not in result.analysis_plan["steps"][0]["tables"]
    assert "t_budget" not in result.analysis_plan["steps"][0]["tables"]


@pytest.mark.asyncio
async def test_finance_relation_skill_routes_budget_expense_analysis_to_plan_execute():
    from agents.runtime.skill_runtime import SkillRuntime
    from agents.runtime.skills.finance_relation_analysis import FinanceRelationAnalysisSkill

    context = _run_context(query="2025年按部门分析预算执行率，并对比已审批报销费用与预算差异")
    runtime = SkillRuntime(skills=[FinanceRelationAnalysisSkill()])

    result = await runtime.invoke_skill(
        "finance_relation_analysis",
        {"query": context.query},
        context,
    )

    goals = "\n".join(str(step.get("goal") or "") for step in result.analysis_plan["steps"])

    assert result.status == "plan_ready"
    assert result.execution_mode == "plan_execute"
    assert [step["type"] for step in result.analysis_plan["steps"]] == ["sql", "sql", "python_merge", "report"]
    assert "预算" in goals
    assert "费用" in goals or "报销" in goals
    assert "回款" not in goals
    assert result.analysis_plan["steps"][0]["tables"] == ["t_budget", "t_cost_center"]
    assert result.analysis_plan["steps"][1]["tables"] == ["t_expense_claim", "t_cost_center"]


@pytest.mark.asyncio
async def test_finance_relation_skill_does_not_override_feasibility_with_keywords():
    from agents.runtime.skills.finance_relation_analysis import FinanceRelationAnalysisSkill

    class FixedFeasibilityContext:
        query = "预算执行率和报销费用对比分析"

        def __init__(self):
            self.calls = []

        async def invoke_tool(self, tool_name, payload):
            self.calls.append((tool_name, payload))
            if tool_name == "current_time.now":
                return SimpleNamespace(ok=True, output={}, error="")
            if tool_name == "business_knowledge.search":
                return SimpleNamespace(
                    ok=True,
                    output={
                        "results": [
                            {
                                "content": (
                                    "术语: 预算执行率\n"
                                    "公式: actual_amount / budget_amount\n"
                                    "关联表: t_budget,t_expense_claim,t_cost_center"
                                )
                            }
                        ]
                    },
                    error="",
                )
            if tool_name == "schema.select_candidates":
                return SimpleNamespace(
                    ok=True,
                    output={
                        "selected_tables": ["t_budget", "t_expense_claim", "t_cost_center"],
                        "recall_context": {
                            "matched_terms": ["预算执行率"],
                            "business_related_tables": ["t_budget", "t_expense_claim", "t_cost_center"],
                        },
                    },
                    error="",
                )
            if tool_name == "semantic_model.search":
                return SimpleNamespace(ok=True, output={"tables": payload["table_names"]}, error="")
            if tool_name == "schema.related_tables":
                return SimpleNamespace(
                    ok=True,
                    output={
                        "relationships": [
                            {"from_table": "t_budget", "to_table": "t_cost_center"},
                            {"from_table": "t_expense_claim", "to_table": "t_cost_center"},
                        ]
                    },
                    error="",
                )
            if tool_name == "plan.assess_feasibility":
                return SimpleNamespace(
                    ok=True,
                    output={
                        "feasibility_decision": {
                            "execution_mode": "single_sql",
                            "task_type": "ambiguous",
                            "reason": "selected schema is connected and suitable for single SQL",
                        }
                    },
                    error="",
                )
            if tool_name == "analysis_plan.submit":
                return SimpleNamespace(ok=True, output={"plan": payload["plan"]}, error="")
            raise AssertionError(f"Unexpected tool call: {tool_name}")

    result = await FinanceRelationAnalysisSkill().run(
        {"query": "预算执行率和报销费用对比分析"},
        FixedFeasibilityContext(),
    )

    assert result.status == "plan_ready"
    assert result.execution_mode == "single_sql"
    assert result.analysis_plan["execution_mode"] == "single_sql"


def test_finance_relation_skill_merges_subject_groups_by_relationship_expansion():
    from agents.runtime.skills.finance_relation_analysis import FinanceRelationAnalysisSkill

    skill = FinanceRelationAnalysisSkill()

    groups = skill._subject_groups_from_evidence(
        query="2025年按部门分析预算执行率，并对比已审批报销费用与预算差异",
        selected_tables=["t_budget", "t_cost_center", "t_expense_claim"],
        evidence=[
            "术语: 预算执行率\n公式: actual_amount / budget_amount\n关联表: t_budget",
            "术语: 部门费用\n公式: SUM(total_amount) GROUP BY cost_center_id\n关联表: t_expense_claim,t_cost_center",
            "术语: 费用总额\n公式: SUM(total_amount)\n关联表: t_expense_claim",
        ],
        recall_context={"matched_terms": ["预算执行率", "部门费用", "费用总额"]},
        relationships=[
            {"from_table": "t_budget", "from_column": "cost_center_id", "to_table": "t_cost_center", "to_column": "id"},
            {"from_table": "t_expense_claim", "from_column": "cost_center_id", "to_table": "t_cost_center", "to_column": "id"},
        ],
    )

    assert groups == [
        {"label": "预算执行率", "tables": ["t_budget", "t_cost_center"]},
        {"label": "部门费用、费用总额", "tables": ["t_expense_claim", "t_cost_center"]},
    ]


def test_finance_relation_skill_focuses_single_sql_on_best_connected_component():
    from agents.runtime.skills.finance_relation_analysis import FinanceRelationAnalysisSkill

    skill = FinanceRelationAnalysisSkill()

    tables = skill._focused_single_sql_tables(
        query="去年亏损",
        selected_tables=["t_journal_entry", "t_journal_item", "t_account", "t_expense_claim"],
        evidence=[
            (
                "术语: 净利润\n"
                "公式: 收入 - 成本 - 费用\n"
                "同义词: 亏损,盈利,利润\n"
                "关联表: t_journal_entry,t_journal_item,t_account,t_expense_claim"
            )
        ],
        recall_context={"matched_terms": ["净利润"]},
        relationships=[
            {"from_table": "t_journal_item", "from_column": "entry_id", "to_table": "t_journal_entry", "to_column": "id"},
            {"from_table": "t_journal_item", "from_column": "account_code", "to_table": "t_account", "to_column": "account_code"},
        ],
    )

    assert tables == ["t_journal_entry", "t_journal_item", "t_account"]


@pytest.mark.asyncio
async def test_finance_relation_skill_clarifies_when_query_lacks_relation_intent():
    from agents.runtime.skill_runtime import SkillRuntime
    from agents.runtime.skills.finance_relation_analysis import FinanceRelationAnalysisSkill

    context = _run_context(query="帮我看看")
    runtime = SkillRuntime(skills=[FinanceRelationAnalysisSkill()])

    result = await runtime.invoke_skill(
        "finance_relation_analysis",
        {"query": context.query},
        context,
    )

    assert result.status == "needs_clarification"
    assert result.execution_mode == "clarification"
    assert result.clarification_questions
    assert not result.analysis_plan
    assert "analysis_plan.submit" not in [trace["tool_name"] for trace in context.tool_trace]
