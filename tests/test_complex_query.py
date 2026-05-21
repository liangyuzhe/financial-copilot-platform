import pytest

from agents.flow.complex_query import assess_query_feasibility, validate_complex_plan


def test_small_schema_routes_to_single_sql():
    result = assess_query_feasibility(
        query="去年亏损",
        selected_tables=["t_journal_entry", "t_journal_item", "t_account"],
        relationships=[
            {"from_table": "t_journal_item", "to_table": "t_journal_entry"},
            {"from_table": "t_journal_item", "to_table": "t_account"},
        ],
    )

    assert result.execution_mode == "single_sql"
    assert result.selected_tables_count == 3


def test_connected_schema_without_task_rule_routes_to_single_sql():
    result = assess_query_feasibility(
        query="查询项目预算和费用明细",
        selected_tables=["t_budget", "t_cost_center", "t_department"],
        relationships=[
            {"from_table": "t_budget", "to_table": "t_cost_center"},
            {"from_table": "t_cost_center", "to_table": "t_department"},
        ],
    )

    assert result.execution_mode == "single_sql"
    assert result.task_type == "ambiguous"
    assert result.can_decompose is False


def test_cyclic_schema_without_task_rule_routes_to_strict_single_sql():
    result = assess_query_feasibility(
        query="查询项目预算和费用明细",
        selected_tables=["a", "b", "c"],
        relationships=[
            {"from_table": "a", "to_table": "b"},
            {"from_table": "b", "to_table": "c"},
            {"from_table": "a", "to_table": "c"},
        ],
    )

    assert result.execution_mode == "single_sql_with_strict_checks"
    assert result.join_risk == "medium"


def test_analysis_task_routes_to_complex_plan_independent_of_table_count():
    result = assess_query_feasibility(
        query="收入成本预算回款费用之间的关系",
        selected_tables=["t_journal_item", "t_account", "t_budget"],
        relationships=[{"from_table": "t_journal_item", "to_table": "t_account"}],
        task_type="analysis",
    )

    assert result.execution_mode == "complex_plan"
    assert result.task_type == "analysis"
    assert result.can_decompose is True


@pytest.mark.asyncio
async def test_multi_metric_finance_query_routes_to_complex_plan_by_recall_context(monkeypatch):
    from agents.flow import sql_react

    async def no_route_rule(query):
        return None

    monkeypatch.setattr(sql_react, "evaluate_query_route_rules", no_route_rule)

    result = await sql_react.assess_feasibility({
        "query": "收入成本预算回款费用之间的关系",
        "selected_tables": ["t_journal_item", "t_account", "t_budget", "t_receivable_payable", "t_expense_claim"],
        "table_relationships": [{"from_table": "t_journal_item", "to_table": "t_account"}],
        "recall_context": {
            "query_key": "收入成本预算回款费用之间的关系",
            "matched_terms": ["收入", "成本", "预算", "回款", "费用"],
            "business_related_tables": [
                "t_journal_item",
                "t_account",
                "t_budget",
                "t_receivable_payable",
                "t_expense_claim",
            ],
        },
    })

    assert result["route_mode"] == "complex_plan"
    assert result["feasibility_decision"]["task_type"] == "analysis"


def test_analysis_task_routes_to_complex_plan_even_when_schema_is_disconnected():
    result = assess_query_feasibility(
        query="今年收入成本预算回款费用之间的关系",
        selected_tables=["t_journal_item", "t_budget", "t_receivable_payable"],
        relationships=[],
        task_type="analysis",
    )

    assert result.execution_mode == "complex_plan"


def test_detail_signal_routes_to_clarify():
    result = assess_query_feasibility(
        query="员工工资和部门角色权限",
        selected_tables=["t_user", "t_role"],
        relationships=[],
        task_type="detail",
    )

    assert result.execution_mode == "clarify"
    assert result.needs_clarification is True


def test_disconnected_schema_without_task_rule_routes_to_clarify():
    result = assess_query_feasibility(
        query="员工工资和部门角色权限",
        selected_tables=["t_user", "t_journal_item"],
        relationships=[],
    )

    assert result.execution_mode == "clarify"
    assert result.task_type == "ambiguous"


def test_analysis_task_uses_rule_over_schema_size():
    result = assess_query_feasibility(
        query="去年收入成本关系",
        selected_tables=["t_journal_entry", "t_journal_item", "t_account"],
        relationships=[{"from_table": "t_journal_entry", "to_table": "t_journal_item"}],
        task_type="analysis",
    )

    assert result.execution_mode == "complex_plan"
    assert result.can_single_sql is False
    assert result.can_decompose is True


def test_validate_complex_plan_accepts_valid_plan():
    plan = {
        "mode": "complex_plan",
        "steps": [
            {"step": 1, "type": "sql", "goal": "查收入", "tables": ["a", "b"], "depends_on": [], "merge_keys": ["period"]},
            {"step": 2, "type": "sql", "goal": "查预算", "tables": ["c"], "depends_on": [], "merge_keys": ["period"]},
            {"step": 3, "type": "python_merge", "goal": "合并", "tables": [], "depends_on": [1, 2], "merge_keys": ["period"]},
        ],
        "requires_user_confirmation": True,
    }

    ok, error = validate_complex_plan(plan, allowed_tables={"a", "b", "c"})

    assert ok is True
    assert error == ""


def test_validate_complex_plan_accepts_allowed_tables_without_table_count_gate():
    plan = {
        "mode": "complex_plan",
        "steps": [
            {
                "step": 1,
                "type": "sql",
                "goal": "查跨域指标",
                "tables": ["a", "b", "c", "d", "e", "f"],
                "depends_on": [],
                "merge_keys": ["department"],
            }
        ],
    }

    ok, error = validate_complex_plan(plan, allowed_tables={"a", "b", "c", "d", "e", "f"})

    assert ok is True
    assert error == ""


def test_validate_complex_plan_rejects_unknown_table():
    plan = {
        "mode": "complex_plan",
        "steps": [
            {"step": 1, "type": "sql", "goal": "查收入", "tables": ["missing"], "depends_on": [], "merge_keys": ["period"]}
        ],
    }

    ok, error = validate_complex_plan(plan, allowed_tables={"a"})

    assert ok is False
    assert "unknown table" in error


def test_validate_complex_plan_rejects_missing_merge_key_for_merge():
    plan = {
        "mode": "complex_plan",
        "steps": [
            {"step": 1, "type": "sql", "goal": "查收入", "tables": ["a"], "depends_on": [], "merge_keys": []},
            {"step": 2, "type": "python_merge", "goal": "合并", "tables": [], "depends_on": [1], "merge_keys": []},
        ],
    }

    ok, error = validate_complex_plan(plan, allowed_tables={"a"})

    assert ok is False
    assert "merge_keys" in error
