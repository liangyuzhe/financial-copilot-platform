from __future__ import annotations

from datetime import datetime, timezone

import pytest
from langchain_core.documents import Document


def _tool_names(tools):
    return [tool.name for tool in tools]


def test_catalog_filters_tools_by_task_type_and_exposes_contracts():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()

    exploratory = catalog.get_tools(
        task_type="exploratory_analysis",
        security_context={"allowed_tables": ["t_orders"]},
    )
    assert _tool_names(exploratory) == [
        "semantic_model.search",
        "business_knowledge.search",
        "schema.list_tables",
        "schema.describe_table",
        "schema.related_tables",
        "current_time.now",
    ]

    complex_tools = catalog.get_tools(
        task_type="complex_analysis",
        security_context={"allowed_tables": ["t_orders"]},
    )
    assert _tool_names(complex_tools) == [
        "semantic_model.search",
        "business_knowledge.search",
        "schema.list_tables",
        "schema.describe_table",
        "schema.related_tables",
        "current_time.now",
        "sql_draft.submit",
    ]

    report_tools = catalog.get_tools(
        task_type="report_generation",
        security_context={"allowed_tables": ["t_orders"]},
    )
    assert _tool_names(report_tools) == [
        "artifact.read",
        "report.render",
    ]

    assert catalog.get_tools(task_type="strict_sql_query", security_context={}) == []

    contract = catalog.get_contract("schema.describe_table")
    assert contract.name == "schema.describe_table"
    assert contract.description
    assert contract.input_schema["type"] == "object"
    assert contract.input_schema["properties"]["table_name"]["type"] == "string"
    assert contract.output_contract["type"] == "object"
    assert contract.read_only is True
    assert contract.direct_execution_allowed is True

    draft_contract = catalog.get_contract("sql_draft.submit")
    assert draft_contract.read_only is False
    assert draft_contract.direct_execution_allowed is False


def test_data_analysis_tools_expose_analysis_plan_submit_not_sql_draft():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()

    tools = catalog.get_tools(
        task_type="data_analysis",
        security_context={"allowed_tables": ["t_orders"]},
    )

    assert _tool_names(tools) == [
        "semantic_model.search",
        "business_knowledge.search",
        "schema.list_tables",
        "schema.describe_table",
        "schema.related_tables",
        "current_time.now",
        "analysis_plan.submit",
    ]

    plan_contract = catalog.get_contract("analysis_plan.submit")
    assert plan_contract.read_only is False
    assert plan_contract.direct_execution_allowed is False
    assert plan_contract.input_schema["properties"]["plan"]["type"] == "object"
    assert "SQL Harness" in plan_contract.description


def test_empty_table_scope_hides_table_specific_schema_tools():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()

    tools = catalog.get_tools(
        task_type="exploratory_analysis",
        security_context={"allowed_tables": []},
    )

    assert _tool_names(tools) == [
        "business_knowledge.search",
        "schema.list_tables",
        "current_time.now",
    ]


def test_tool_contract_descriptions_explain_use_boundaries_io_and_negative_cases():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()
    expected = {
        "semantic_model.search": [
            "Purpose:",
            "Boundary:",
            "Required input:",
            "Output:",
            "Do not use when:",
        ],
        "business_knowledge.search": [
            "Purpose:",
            "Boundary:",
            "Required input:",
            "Output:",
            "Do not use when:",
        ],
        "schema.list_tables": [
            "Purpose:",
            "Boundary:",
            "Required input:",
            "Output:",
            "Do not use when:",
        ],
        "schema.describe_table": [
            "Purpose:",
            "Boundary:",
            "Required input:",
            "Output:",
            "Do not use when:",
        ],
        "schema.related_tables": [
            "Purpose:",
            "Boundary:",
            "Required input:",
            "Output:",
            "Do not use when:",
        ],
        "current_time.now": [
            "Purpose:",
            "Boundary:",
            "Required input:",
            "Output:",
            "Do not use when:",
        ],
        "artifact.read": [
            "Purpose:",
            "Boundary:",
            "Required input:",
            "Output:",
            "Do not use when:",
        ],
        "report.render": [
            "Purpose:",
            "Boundary:",
            "Required input:",
            "Output:",
            "Do not use when:",
        ],
        "sql_draft.submit": [
            "Purpose:",
            "Boundary:",
            "Required input:",
            "Output:",
            "Do not use when:",
        ],
        "analysis_plan.submit": [
            "Purpose:",
            "Boundary:",
            "Required input:",
            "Output:",
            "Do not use when:",
        ],
    }

    for tool_name, required_phrases in expected.items():
        description = catalog.get_contract(tool_name).description
        assert len(description) > 180, tool_name
        for phrase in required_phrases:
            assert phrase in description, tool_name


def test_tool_contract_io_schema_properties_are_documented():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()
    tool_names = [
        "semantic_model.search",
        "business_knowledge.search",
        "schema.list_tables",
        "schema.describe_table",
        "schema.related_tables",
        "current_time.now",
        "artifact.read",
        "report.render",
        "sql_draft.submit",
        "analysis_plan.submit",
    ]

    for tool_name in tool_names:
        contract = catalog.get_contract(tool_name)
        for schema_name, schema in (
            ("input_schema", contract.input_schema),
            ("output_contract", contract.output_contract),
        ):
            for prop_name, prop_schema in schema.get("properties", {}).items():
                assert prop_schema.get("description"), (tool_name, schema_name, prop_name)


def test_readthrough_tool_contracts_document_cache_output_fields():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()

    for tool_name in [
        "semantic_model.search",
        "business_knowledge.search",
        "schema.list_tables",
        "schema.related_tables",
    ]:
        props = catalog.get_contract(tool_name).output_contract["properties"]
        assert props["source"]["description"]
        assert props["cache_hit"]["description"]

    semantic_props = catalog.get_contract("semantic_model.search").output_contract["properties"]
    assert semantic_props["from_workflow_state"]["description"]
    assert semantic_props["fetched"]["description"]


@pytest.mark.asyncio
async def test_schema_tools_filter_tables_and_deny_describe_for_unauthorized_table():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    providers = ToolProviders(
        table_metadata_loader=lambda: [
            {"table_name": "t_orders", "table_comment": "订单数据"},
            {"table_name": "t_payroll", "table_comment": "薪资数据"},
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
    )
    catalog = ToolCatalog(providers=providers)
    context = {
        "user_id": "u-1",
        "allowed_tables": ["t_orders"],
        "denied_tables": ["t_payroll"],
    }

    list_result = await catalog.invoke(
        "schema.list_tables",
        {},
        task_type="exploratory_analysis",
        security_context=context,
        session_id="s-1",
        thread_id="th-1",
    )
    assert list_result.ok is True
    assert list_result.output == {
        "tables": [{"table_name": "t_orders", "table_comment": "订单数据"}]
    }
    assert list_result.trace.status == "success"
    assert list_result.trace.user_id == "u-1"

    denied = await catalog.invoke(
        "schema.describe_table",
        {"table_name": "t_payroll"},
        task_type="exploratory_analysis",
        security_context=context,
        session_id="s-1",
        thread_id="th-1",
    )
    assert denied.ok is False
    assert denied.output is None
    assert denied.trace.status == "denied"
    assert "薪资数据" in denied.error
    assert "t_payroll" not in denied.error


@pytest.mark.asyncio
async def test_semantic_model_and_related_tables_never_expose_denied_tables():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    semantic_calls = []
    relationship_calls = []

    def semantic_loader(table_names):
        semantic_calls.append(list(table_names))
        return {
            "t_orders": {
                "customer_id": {
                    "table_name": "t_orders",
                    "column_name": "customer_id",
                    "business_name": "客户ID",
                }
            },
            "t_payroll": {
                "salary": {
                    "table_name": "t_payroll",
                    "column_name": "salary",
                    "business_name": "薪资",
                }
            },
        }

    def relationship_loader(table_names):
        relationship_calls.append(list(table_names))
        return [
            {
                "from_table": "t_orders",
                "from_column": "customer_id",
                "to_table": "t_customer",
                "to_column": "id",
            },
            {
                "from_table": "t_payroll",
                "from_column": "user_id",
                "to_table": "t_orders",
                "to_column": "user_id",
            },
        ]

    catalog = ToolCatalog(
        providers=ToolProviders(
            table_metadata_loader=lambda: [
                {"table_name": "t_orders", "table_comment": "订单数据"},
                {"table_name": "t_customer", "table_comment": "客户数据"},
                {"table_name": "t_payroll", "table_comment": "薪资数据"},
            ],
            semantic_model_loader=semantic_loader,
            table_relationship_loader=relationship_loader,
        )
    )
    context = {
        "allowed_tables": ["t_orders", "t_customer"],
        "denied_tables": ["t_payroll"],
    }

    semantic_result = await catalog.invoke(
        "semantic_model.search",
        {"table_names": ["t_orders", "t_payroll"]},
        task_type="exploratory_analysis",
        security_context=context,
    )
    assert semantic_result.ok is True
    assert semantic_calls == [["t_orders"]]
    assert semantic_result.output == {
        "tables": ["t_orders"],
        "semantic_model": {
            "t_orders": {
                "customer_id": {
                    "table_name": "t_orders",
                    "column_name": "customer_id",
                    "business_name": "客户ID",
                }
            }
        },
    }

    relationship_result = await catalog.invoke(
        "schema.related_tables",
        {"table_names": ["t_orders", "t_payroll"]},
        task_type="exploratory_analysis",
        security_context=context,
    )
    assert relationship_result.ok is True
    assert relationship_calls == [["t_orders"]]
    assert relationship_result.output == {
        "relationships": [
            {
                "from_table": "t_orders",
                "from_column": "customer_id",
                "to_table": "t_customer",
                "to_column": "id",
            }
        ]
    }


@pytest.mark.asyncio
async def test_business_knowledge_current_time_and_trace_context():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    providers = ToolProviders(
        business_knowledge_search=lambda query, top_k: [
            Document(
                page_content="术语: 毛利率\n公式: (收入 - 成本) / 收入",
                metadata={"source": "business_knowledge", "score": 0.91},
            )
        ],
        time_provider=lambda: datetime(2026, 5, 16, 4, 30, tzinfo=timezone.utc),
    )
    catalog = ToolCatalog(providers=providers)
    context = {"user_id": "u-2"}

    knowledge = await catalog.invoke(
        "business_knowledge.search",
        {"query": "毛利率怎么算", "top_k": 3},
        task_type="exploratory_analysis",
        security_context=context,
        session_id="s-2",
        thread_id="th-2",
    )

    assert knowledge.ok is True
    assert knowledge.output == {
        "results": [
            {
                "content": "术语: 毛利率\n公式: (收入 - 成本) / 收入",
                "metadata": {"source": "business_knowledge", "score": 0.91},
            }
        ]
    }
    assert knowledge.trace.tool_name == "business_knowledge.search"
    assert knowledge.trace.task_type == "exploratory_analysis"
    assert knowledge.trace.session_id == "s-2"
    assert knowledge.trace.thread_id == "th-2"
    assert knowledge.trace.user_id == "u-2"
    assert knowledge.trace.status == "success"
    assert knowledge.trace.elapsed_ms >= 0

    now = await catalog.invoke(
        "current_time.now",
        {},
        task_type="exploratory_analysis",
        security_context=context,
    )
    assert now.ok is True
    assert now.output == {
        "iso": "2026-05-16T04:30:00+00:00",
        "date": "2026-05-16",
        "timezone": "UTC",
    }


@pytest.mark.asyncio
async def test_analysis_plan_submit_validates_structure_and_authorized_tables():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()
    plan = {
        "mode": "analysis_plan",
        "reason": "单 SQL 可回答订单数量",
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
    }

    result = await catalog.invoke(
        "analysis_plan.submit",
        {
            "plan": plan,
            "purpose": "订单数量分析",
        },
        task_type="data_analysis",
        security_context={"user_id": "u-plan", "allowed_tables": ["t_orders"]},
        session_id="s-plan",
        thread_id="th-plan",
    )

    assert result.ok is True
    assert result.output["plan_id"].startswith("plan-")
    assert result.output["plan"] == plan
    assert result.output["execution_mode"] == "plan_only"
    assert result.output["requires_harness"] is True
    assert result.output["harness_steps"] == [
        "validate_analysis_plan",
        "safety_check",
        "authorize_sql",
        "approve",
        "execute_sql",
        "merge_report",
    ]
    assert result.trace.tool_name == "analysis_plan.submit"
    assert result.trace.task_type == "data_analysis"

    denied = await catalog.invoke(
        "analysis_plan.submit",
        {
            "plan": {
                "mode": "analysis_plan",
                "steps": [
                    {
                        "step": 1,
                        "type": "sql",
                        "goal": "读取薪资",
                        "tables": ["t_payroll"],
                        "sql": "select * from t_payroll",
                    }
                ],
            }
        },
        task_type="data_analysis",
        security_context={"allowed_tables": ["t_orders"]},
    )

    assert denied.ok is False
    assert denied.trace.status == "denied"
    assert "权限" in denied.error or "permission" in denied.error.lower()


@pytest.mark.asyncio
async def test_analysis_plan_submit_authorizes_tables_extracted_from_step_sql():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()

    denied = await catalog.invoke(
        "analysis_plan.submit",
        {
            "plan": {
                "mode": "analysis_plan",
                "steps": [
                    {
                        "step": 1,
                        "type": "sql",
                        "goal": "读取应收数据",
                        "tables": ["business"],
                        "sql": "select * from t_receivable_payable",
                    }
                ],
            }
        },
        task_type="data_analysis",
        security_context={"allowed_tables": ["business"]},
    )

    assert denied.ok is False
    assert denied.trace.status == "denied"
    assert "权限" in denied.error or "permission" in denied.error.lower()


@pytest.mark.asyncio
async def test_analysis_plan_submit_normalizes_partial_package_runner_plan():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()
    partial_plan = {
        "reason": "需要关联 t_journal_entry、t_journal_item、t_budget、t_receivable_payable。",
        "steps": [
            {
                "step_number": 1,
                "description": "查询收入成本发生额",
                "sql": "select je.period from t_journal_entry je join t_journal_item ji on je.id=ji.entry_id",
            },
            {
                "step_number": 2,
                "description": "查询预算金额",
                "sql": "select budget_year, budget_month, budget_amount from t_budget",
            },
        ],
    }

    result = await catalog.invoke(
        "analysis_plan.submit",
        {
            "purpose": "分析收入成本预算回款费用关系",
            "plan": partial_plan,
        },
        task_type="data_analysis",
        security_context={
            "allowed_tables": [
                "t_journal_entry",
                "t_journal_item",
                "t_budget",
                "t_receivable_payable",
            ]
        },
    )

    assert result.ok is True
    plan = result.output["plan"]
    assert plan["mode"] == "analysis_plan"
    assert [step["type"] for step in plan["steps"]] == ["sql", "sql"]
    assert plan["steps"][0]["step"] == 1
    assert plan["steps"][0]["goal"] == "查询收入成本发生额"
    assert plan["steps"][0]["tables"] == ["t_journal_entry", "t_journal_item"]
    assert plan["steps"][1]["tables"] == ["t_budget"]


@pytest.mark.asyncio
async def test_analysis_plan_submit_preserves_line_comments_in_step_sql():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()
    sql = """SELECT
je.period,
-- 收入：贷方科目中收入类的总和
SUM(CASE WHEN a.account_type = '损益' THEN ji.credit_amount ELSE 0 END) AS total_income
FROM t_journal_entry je
JOIN t_journal_item ji ON je.id = ji.entry_id
JOIN t_account a ON ji.account_code = a.account_code;"""

    result = await catalog.invoke(
        "analysis_plan.submit",
        {
            "purpose": "分析收入成本预算回款费用关系",
            "plan": {
                "reason": "验证 SQL 注释不被压平",
                "steps": [
                    {
                        "step": 1,
                        "description": "查询收入",
                        "sql": sql,
                    }
                ],
            },
        },
        task_type="data_analysis",
        security_context={
            "allowed_tables": [
                "t_journal_entry",
                "t_journal_item",
                "t_account",
            ]
        },
    )

    assert result.ok is True
    normalized_sql = result.output["plan"]["steps"][0]["sql"]
    assert "-- 收入：贷方科目中收入类的总和\nSUM(" in normalized_sql
    assert "-- 收入：贷方科目中收入类的总和 SUM(" not in normalized_sql
    assert normalized_sql.endswith("a.account_code")
    assert not normalized_sql.endswith(";")


@pytest.mark.asyncio
async def test_analysis_plan_submit_normalizes_markdown_plan_text():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()

    result = await catalog.invoke(
        "analysis_plan.submit",
        {
            "purpose": "分析收入成本预算回款费用关系",
            "analysis_plan": "- `t_journal_item`\n- `t_budget`\n- `t_receivable_payable`",
        },
        task_type="data_analysis",
        security_context={
            "allowed_tables": [
                "t_journal_item",
                "t_budget",
                "t_receivable_payable",
            ]
        },
    )

    assert result.ok is True
    plan = result.output["plan"]
    assert plan["mode"] == "analysis_plan"
    assert [step["type"] for step in plan["steps"]] == ["sql", "python_merge", "report"]
    assert plan["steps"][0]["tables"] == [
        "t_journal_item",
        "t_budget",
        "t_receivable_payable",
    ]


@pytest.mark.asyncio
async def test_business_knowledge_search_reuses_matching_workflow_state_evidence():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    provider_calls = []

    def provider(query, top_k):
        provider_calls.append((query, top_k))
        return [
            Document(
                page_content="术语: provider\n公式: provider_formula",
                metadata={"source": "business_knowledge", "score": 0.2},
            )
        ]

    catalog = ToolCatalog(
        providers=ToolProviders(business_knowledge_search=provider)
    )
    workflow_state = {
        "query": "分析收入成本关系",
        "recall_context": {"query_key": "分析收入成本关系"},
        "evidence": [
            "术语: 收入成本关系\n公式: 收入 - 成本\n关联表: t_revenue,t_cost"
        ],
    }

    result = await catalog.invoke(
        "business_knowledge.search",
        {"query": "分析收入成本关系", "top_k": 5},
        task_type="complex_analysis",
        security_context={"user_id": "u-cache"},
        workflow_state=workflow_state,
    )

    assert result.ok is True
    assert provider_calls == []
    assert result.output["cache_hit"] is True
    assert result.output["source"] == "workflow_state"
    assert result.output["results"] == [
        {
            "content": "术语: 收入成本关系\n公式: 收入 - 成本\n关联表: t_revenue,t_cost",
            "metadata": {
                "source": "workflow_state",
                "score": 1.0,
                "retriever_source": "workflow_state",
            },
        }
    ]


@pytest.mark.asyncio
async def test_business_knowledge_search_reuses_workflow_state_evidence_for_agent_subqueries():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    provider_calls = []

    def provider(query, top_k):
        provider_calls.append((query, top_k))
        return [
            Document(
                page_content="术语: provider\n公式: provider_formula",
                metadata={"source": "business_knowledge"},
            )
        ]

    catalog = ToolCatalog(
        providers=ToolProviders(business_knowledge_search=provider)
    )
    workflow_state = {
        "query": "分析今年收入、成本、预算、回款和费用之间的关系",
        "recall_context": {
            "query_key": "分析今年收入、成本、预算、回款和费用之间的关系",
            "matched_terms": ["收入", "成本", "预算", "回款", "费用"],
        },
        "selected_tables": ["t_journal_item", "t_budget"],
        "evidence": [
            "术语: 收入成本预算回款费用\n公式: 按期间合并收入、成本、预算、回款、费用"
        ],
    }

    result = await catalog.invoke(
        "business_knowledge.search",
        {"query": "收入", "top_k": 5},
        task_type="complex_analysis",
        security_context={"user_id": "u-cache"},
        workflow_state=workflow_state,
    )

    assert result.ok is True
    assert provider_calls == []
    assert result.output["cache_hit"] is True
    assert result.output["source"] == "workflow_state"
    assert result.output["query_reused_from"] == workflow_state["query"]
    assert result.output["results"][0]["metadata"]["retriever_source"] == "workflow_state"


@pytest.mark.asyncio
async def test_semantic_model_search_fetches_only_missing_workflow_state_tables():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    semantic_calls = []

    def semantic_loader(table_names):
        semantic_calls.append(list(table_names))
        return {
            table: {
                "amount": {
                    "table_name": table,
                    "column_name": "amount",
                    "business_name": f"{table}金额",
                }
            }
            for table in table_names
        }

    catalog = ToolCatalog(
        providers=ToolProviders(
            semantic_model_loader=semantic_loader,
        )
    )
    workflow_state = {
        "semantic_model": {
            "t_revenue": {
                "amount": {
                    "table_name": "t_revenue",
                    "column_name": "amount",
                    "business_name": "收入金额",
                }
            }
        }
    }

    result = await catalog.invoke(
        "semantic_model.search",
        {"table_names": ["t_revenue", "t_cost"]},
        task_type="complex_analysis",
        security_context={"allowed_tables": ["t_revenue", "t_cost"]},
        workflow_state=workflow_state,
    )

    assert result.ok is True
    assert semantic_calls == [["t_cost"]]
    assert result.output["source"] == "mixed"
    assert result.output["cache_hit"] is False
    assert result.output["from_workflow_state"] == ["t_revenue"]
    assert result.output["fetched"] == ["t_cost"]
    assert result.output["tables"] == ["t_revenue", "t_cost"]
    assert result.output["semantic_model"]["t_revenue"]["amount"]["business_name"] == "收入金额"
    assert result.output["semantic_model"]["t_cost"]["amount"]["business_name"] == "t_cost金额"


@pytest.mark.asyncio
async def test_semantic_model_search_without_table_names_reuses_workflow_state_selected_tables():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    def fail_loader(*args, **kwargs):
        raise AssertionError("provider should not be called when workflow_state has selected semantic_model")

    catalog = ToolCatalog(
        providers=ToolProviders(
            table_metadata_loader=fail_loader,
            semantic_model_loader=fail_loader,
        )
    )
    workflow_state = {
        "selected_tables": ["t_revenue", "t_cost"],
        "semantic_model": {
            "t_revenue": {
                "amount": {
                    "table_name": "t_revenue",
                    "column_name": "amount",
                    "business_name": "收入金额",
                }
            },
            "t_cost": {
                "amount": {
                    "table_name": "t_cost",
                    "column_name": "amount",
                    "business_name": "成本金额",
                }
            },
        },
    }

    result = await catalog.invoke(
        "semantic_model.search",
        {},
        task_type="complex_analysis",
        security_context={"allowed_tables": ["t_revenue", "t_cost"]},
        workflow_state=workflow_state,
    )

    assert result.ok is True
    assert result.output["source"] == "workflow_state"
    assert result.output["cache_hit"] is True
    assert result.output["tables"] == ["t_revenue", "t_cost"]
    assert result.output["fetched"] == []


@pytest.mark.asyncio
async def test_related_tables_reuses_workflow_state_when_it_covers_requested_tables():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    relationship_calls = []

    def relationship_loader(table_names):
        relationship_calls.append(list(table_names))
        return []

    catalog = ToolCatalog(
        providers=ToolProviders(table_relationship_loader=relationship_loader)
    )
    workflow_state = {
        "table_relationships": [
            {
                "from_table": "t_revenue",
                "from_column": "project_id",
                "to_table": "t_cost",
                "to_column": "project_id",
            },
            {
                "from_table": "t_payroll",
                "from_column": "user_id",
                "to_table": "t_revenue",
                "to_column": "user_id",
            },
        ]
    }

    result = await catalog.invoke(
        "schema.related_tables",
        {"table_names": ["t_revenue", "t_cost"]},
        task_type="complex_analysis",
        security_context={
            "allowed_tables": ["t_revenue", "t_cost"],
            "denied_tables": ["t_payroll"],
        },
        workflow_state=workflow_state,
    )

    assert result.ok is True
    assert relationship_calls == []
    assert result.output == {
        "relationships": [
            {
                "from_table": "t_revenue",
                "from_column": "project_id",
                "to_table": "t_cost",
                "to_column": "project_id",
            }
        ],
        "source": "workflow_state",
        "cache_hit": True,
    }


@pytest.mark.asyncio
async def test_related_tables_reuses_empty_workflow_state_relationships():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    def fail_loader(*args, **kwargs):
        raise AssertionError("relationship provider should not be called for known-empty workflow relationships")

    catalog = ToolCatalog(
        providers=ToolProviders(table_relationship_loader=fail_loader)
    )
    workflow_state = {
        "selected_tables": ["t_revenue", "t_cost"],
        "table_relationships": [],
    }

    result = await catalog.invoke(
        "schema.related_tables",
        {"table_names": ["t_revenue", "t_cost"]},
        task_type="complex_analysis",
        security_context={"allowed_tables": ["t_revenue", "t_cost"]},
        workflow_state=workflow_state,
    )

    assert result.ok is True
    assert result.output == {
        "relationships": [],
        "source": "workflow_state",
        "cache_hit": True,
    }


@pytest.mark.asyncio
async def test_report_generation_reads_only_existing_artifacts_and_results():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()
    workflow_state = {
        "result": {
            "rows": [
                {"month": "2026-01", "revenue": 1200, "cost": 800},
                {"month": "2026-02", "revenue": 900, "cost": 950},
            ]
        },
        "artifacts": [
            {
                "id": "summary-1",
                "type": "analysis_summary",
                "content": {
                    "conclusion": "2 月收入低于成本。",
                    "metrics": {"revenue_total": 2100, "cost_total": 1750},
                    "anomalies": ["2026-02 成本高于收入"],
                    "next_steps": ["下钻 2 月成本明细"],
                },
            }
        ],
    }

    read_result = await catalog.invoke(
        "artifact.read",
        {"artifact_ids": ["summary-1", "result"]},
        task_type="report_generation",
        security_context={"user_id": "u-report"},
        session_id="s-report",
        thread_id="th-report",
        workflow_state=workflow_state,
    )

    assert read_result.ok is True
    assert [artifact["id"] for artifact in read_result.output["artifacts"]] == [
        "summary-1",
        "result",
    ]
    assert read_result.trace.status == "success"
    assert read_result.trace.user_id == "u-report"

    denied_schema = await catalog.invoke(
        "schema.list_tables",
        {},
        task_type="report_generation",
        security_context={"user_id": "u-report"},
        workflow_state=workflow_state,
    )
    assert denied_schema.ok is False
    assert denied_schema.trace.status == "forbidden"
    assert denied_schema.output is None

    denied_knowledge = await catalog.invoke(
        "business_knowledge.search",
        {"query": "收入成本口径"},
        task_type="report_generation",
        security_context={"user_id": "u-report"},
        workflow_state=workflow_state,
    )
    assert denied_knowledge.ok is False
    assert denied_knowledge.trace.status == "forbidden"


@pytest.mark.asyncio
async def test_report_render_outputs_markdown_sections_and_optional_echarts():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()
    workflow_state = {
        "artifacts": [
            {
                "id": "analysis-1",
                "type": "analysis_summary",
                "content": {
                    "conclusion": "收入总体高于成本，但 2 月出现倒挂。",
                    "metrics": {"revenue_total": 2100, "cost_total": 1750},
                    "anomalies": ["2026-02 成本高于收入"],
                    "next_steps": ["核对 2 月供应商费用"],
                },
            }
        ]
    }

    render_result = await catalog.invoke(
        "report.render",
        {"title": "收入成本分析报告", "include_echarts": True},
        task_type="report_generation",
        security_context={"user_id": "u-report"},
        workflow_state=workflow_state,
    )

    assert render_result.ok is True
    markdown = render_result.output["markdown"]
    assert markdown.startswith("# 收入成本分析报告")
    assert "## 结论" in markdown
    assert "收入总体高于成本" in markdown
    assert "## 关键指标" in markdown
    assert "revenue_total: 2100" in markdown
    assert "## 异常点" in markdown
    assert "2026-02 成本高于收入" in markdown
    assert "## 后续追查建议" in markdown
    assert "核对 2 月供应商费用" in markdown
    assert render_result.output["source_artifact_ids"] == ["analysis-1"]
    assert render_result.output["echarts"]
    assert render_result.output["echarts"][0]["option"]["series"][0]["type"] == "bar"


@pytest.mark.asyncio
async def test_sql_draft_submit_returns_harness_handoff_without_execution():
    from agents.runtime.tool_catalog import ToolCatalog

    catalog = ToolCatalog()

    result = await catalog.invoke(
        "sql_draft.submit",
        {
            "sql": "select month, sum(revenue) from t_orders group by month",
            "purpose": "按月分析收入",
            "tables": ["t_orders"],
        },
        task_type="complex_analysis",
        security_context={"user_id": "u-complex", "allowed_tables": ["t_orders"]},
        session_id="s-complex",
        thread_id="th-complex",
    )

    assert result.ok is True
    assert result.output["execution_mode"] == "draft_only"
    assert result.output["requires_harness"] is True
    assert result.output["status"] == "pending_harness_review"
    assert result.output["harness_steps"] == [
        "safety_check",
        "authorize_sql",
        "approve",
        "execute_sql",
    ]
    assert "execution_result" not in result.output
    assert result.trace.status == "success"

    forbidden = await catalog.invoke(
        "sql_draft.submit",
        {"sql": "select 1"},
        task_type="exploratory_analysis",
        security_context={"user_id": "u-complex"},
    )
    assert forbidden.ok is False
    assert forbidden.trace.status == "forbidden"
