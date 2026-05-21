from __future__ import annotations

import pytest
from langchain_core.documents import Document


def _finance_catalog():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    metadata = [
        {"table_name": "finance_revenue", "table_comment": "收入确认事实表"},
        {"table_name": "finance_cost", "table_comment": "成本归集事实表"},
        {"table_name": "finance_budget", "table_comment": "预算执行事实表"},
        {"table_name": "finance_receivable", "table_comment": "回款应收事实表"},
        {"table_name": "finance_expense", "table_comment": "费用报销事实表"},
    ]

    def semantic_model_loader(table_names):
        return {
            table: {
                "biz_date": {
                    "table_name": table,
                    "column_name": "biz_date",
                    "business_name": "业务日期",
                },
                "amount": {
                    "table_name": table,
                    "column_name": "amount",
                    "business_name": "金额",
                },
                "project_id": {
                    "table_name": table,
                    "column_name": "project_id",
                    "business_name": "项目",
                },
            }
            for table in table_names
        }

    return ToolCatalog(
        providers=ToolProviders(
            business_knowledge_search=lambda query, top_k=5: [
                Document(
                    page_content="收入、成本、预算、回款、费用需要按期间和项目统一口径后比较。",
                    metadata={"source": "finance_metric_policy"},
                )
            ],
            table_metadata_loader=lambda: metadata,
            semantic_model_loader=semantic_model_loader,
            table_relationship_loader=lambda table_names: [
                {
                    "from_table": left,
                    "from_column": "project_id",
                    "to_table": right,
                    "to_column": "project_id",
                    "relation_type": "logical_fk",
                }
                for left in table_names
                for right in table_names
                if left < right
            ],
        )
    )


def _catalog_with_non_amount_fields():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    return ToolCatalog(
        providers=ToolProviders(
            business_knowledge_search=lambda query, top_k=5: [],
            table_metadata_loader=lambda: [
                {"table_name": "t_budget", "table_comment": "预算管理表"},
                {"table_name": "t_fixed_asset", "table_comment": "固定资产表"},
            ],
            semantic_model_loader=lambda table_names: {
                "t_budget": {
                    "budget_year": {
                        "table_name": "t_budget",
                        "column_name": "budget_year",
                        "business_name": "预算年度",
                    },
                    "budget_month": {
                        "table_name": "t_budget",
                        "column_name": "budget_month",
                        "business_name": "预算月份",
                    },
                    "budget_amount": {
                        "table_name": "t_budget",
                        "column_name": "budget_amount",
                        "business_name": "预算金额",
                    },
                },
                "t_fixed_asset": {
                    "acquisition_date": {
                        "table_name": "t_fixed_asset",
                        "column_name": "acquisition_date",
                        "column_type": "date",
                        "business_name": "购入日期",
                    },
                    "acquisition_cost": {
                        "table_name": "t_fixed_asset",
                        "column_name": "acquisition_cost",
                        "column_type": "decimal(15,2)",
                        "business_name": "原值",
                        "business_description": "固定资产原始购置成本",
                    },
                    "monthly_depreciation": {
                        "table_name": "t_fixed_asset",
                        "column_name": "monthly_depreciation",
                        "column_type": "decimal(15,2)",
                        "business_name": "月折旧额",
                        "business_description": "每月应计提折旧额",
                    },
                    "depreciation_method": {
                        "table_name": "t_fixed_asset",
                        "column_name": "depreciation_method",
                        "column_type": "enum('直线法','双倍余额递减法')",
                        "business_name": "折旧方法",
                        "business_description": "直线法/双倍余额递减法",
                    },
                },
            },
            table_relationship_loader=lambda table_names: [],
        )
    )


def _realistic_finance_catalog():
    from agents.runtime.tool_catalog import ToolCatalog, ToolProviders

    metadata = [
        {"table_name": "t_journal_entry", "table_comment": "记账凭证主表"},
        {"table_name": "t_journal_item", "table_comment": "凭证分录明细表"},
        {"table_name": "t_account", "table_comment": "会计科目表"},
        {"table_name": "t_budget", "table_comment": "预算管理表"},
        {"table_name": "t_receivable_payable", "table_comment": "应收应付表"},
        {"table_name": "t_expense_claim", "table_comment": "费用报销表"},
        {"table_name": "t_cost_center", "table_comment": "成本中心表"},
        {"table_name": "t_user_role", "table_comment": "用户角色表"},
    ]
    semantic_model = {
        "t_journal_entry": {
            "period": {
                "table_name": "t_journal_entry",
                "column_name": "period",
                "business_name": "会计期间",
            },
        },
        "t_journal_item": {
            "posting_date": {
                "table_name": "t_journal_item",
                "column_name": "posting_date",
                "column_type": "date",
                "business_name": "记账日期",
            },
            "credit_amount": {
                "table_name": "t_journal_item",
                "column_name": "credit_amount",
                "column_type": "decimal(15,2)",
                "business_name": "贷方金额",
                "synonyms": "收入,回款,发生额",
                "business_description": "收入类科目的贷方发生额。",
            },
            "debit_amount": {
                "table_name": "t_journal_item",
                "column_name": "debit_amount",
                "column_type": "decimal(15,2)",
                "business_name": "借方金额",
                "synonyms": "成本,费用,发生额",
                "business_description": "成本费用类科目的借方发生额。",
            },
            "account_code": {
                "table_name": "t_journal_item",
                "column_name": "account_code",
                "business_name": "会计科目编码",
            },
        },
        "t_account": {
            "account_name": {
                "table_name": "t_account",
                "column_name": "account_name",
                "business_name": "科目名称",
                "synonyms": "收入科目,成本科目,费用科目",
            },
        },
        "t_budget": {
            "budget_year": {
                "table_name": "t_budget",
                "column_name": "budget_year",
                "business_name": "预算年度",
            },
            "budget_month": {
                "table_name": "t_budget",
                "column_name": "budget_month",
                "business_name": "预算月份",
            },
            "budget_amount": {
                "table_name": "t_budget",
                "column_name": "budget_amount",
                "column_type": "decimal(15,2)",
                "business_name": "预算金额",
                "synonyms": "预算额度",
            },
        },
        "t_receivable_payable": {
            "settle_date": {
                "table_name": "t_receivable_payable",
                "column_name": "settle_date",
                "column_type": "date",
                "business_name": "结算日期",
            },
            "settled_amount": {
                "table_name": "t_receivable_payable",
                "column_name": "settled_amount",
                "column_type": "decimal(15,2)",
                "business_name": "已结算金额",
                "synonyms": "回款金额,收款金额",
                "business_description": "客户回款或应收款已结算金额。",
            },
        },
        "t_expense_claim": {
            "claim_date": {
                "table_name": "t_expense_claim",
                "column_name": "claim_date",
                "column_type": "date",
                "business_name": "报销日期",
            },
            "total_amount": {
                "table_name": "t_expense_claim",
                "column_name": "total_amount",
                "column_type": "decimal(15,2)",
                "business_name": "报销总额",
                "synonyms": "费用金额",
            },
        },
        "t_cost_center": {
            "center_name": {
                "table_name": "t_cost_center",
                "column_name": "center_name",
                "business_name": "成本中心名称",
            },
        },
        "t_user_role": {
            "role_id": {
                "table_name": "t_user_role",
                "column_name": "role_id",
                "business_name": "角色ID",
            },
        },
    }

    return ToolCatalog(
        providers=ToolProviders(
            business_knowledge_search=lambda query, top_k=5: [
                Document(
                    page_content=(
                        "收入/成本通常来自凭证分录和会计科目，预算来自预算表，"
                        "回款来自应收应付结算，费用来自报销。"
                    ),
                    metadata={
                        "related_tables": (
                            "t_journal_entry,t_journal_item,t_account,"
                            "t_budget,t_receivable_payable,t_expense_claim"
                        )
                    },
                )
            ],
            table_metadata_loader=lambda: metadata,
            semantic_model_loader=lambda table_names: {
                table: semantic_model.get(table, {})
                for table in table_names
            },
            table_relationship_loader=lambda table_names: [],
        )
    )


def test_create_agentscope_runner_uses_agentscope_backend_by_default_when_available(monkeypatch):
    from agents.runtime.agentscope_adapter import (
        AgentScopePackageRunner,
        create_agentscope_runner,
    )

    monkeypatch.delenv("AGENTSCOPE_RUNTIME_BACKEND", raising=False)

    runner = create_agentscope_runner()

    assert isinstance(runner, AgentScopePackageRunner)


def test_create_agentscope_runner_auto_falls_back_to_local_when_agentscope_missing(monkeypatch):
    import agents.runtime.agentscope_adapter as adapter
    from agents.runtime.agentscope_adapter import (
        LocalAgentScopeCompatibleRunner,
        create_agentscope_runner,
    )

    real_import_module = adapter.importlib.import_module

    def fake_import_module(name):
        if name == "agentscope":
            raise ImportError("missing agentscope")
        return real_import_module(name)

    monkeypatch.setenv("AGENTSCOPE_RUNTIME_BACKEND", "auto")
    monkeypatch.setattr(adapter.importlib, "import_module", fake_import_module)

    runner = create_agentscope_runner()

    assert isinstance(runner, LocalAgentScopeCompatibleRunner)


@pytest.mark.asyncio
async def test_package_backend_returns_structured_error_when_model_call_fails():
    from agents.runtime.agentscope_adapter import AgentScopePackageRunner
    from agents.runtime.agentscope_runtime import AgentScopeRunContext

    class BrokenAgent:
        async def __call__(self, msg):
            raise RuntimeError("model is not configured")

    runner = AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: BrokenAgent(),
    )
    context = AgentScopeRunContext(
        task_type="complex_analysis",
        query="分析收入成本关系",
        session_id="s-package",
        thread_id="th-package",
        security_context={},
        workflow_state={},
        enabled_skills=[],
        tools=[],
        tool_catalog=_finance_catalog(),
        system_prompt="",
    )

    result = await runner(context)

    assert result.answer == ""
    assert result.risk_flags[0]["code"] == "agentscope_adapter_error"
    assert result.risk_flags[0]["severity"] == "error"
    assert "model is not configured" in result.risk_flags[0]["message"]


def test_package_runner_uses_openai_compatible_model_for_ark_provider(monkeypatch):
    import agents.runtime.agentscope_adapter as adapter

    seen = {}

    class FakeOpenAIModel:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs

    monkeypatch.setattr(adapter, "OpenAIChatModel", FakeOpenAIModel)
    monkeypatch.setattr(adapter.settings, "chat_model_type", "ark")
    monkeypatch.setattr(adapter.settings.ark, "chat_model", "doubao-seed-2-0-code-preview-260215")
    monkeypatch.setattr(adapter.settings.ark, "key", "ark-key")
    monkeypatch.setenv("CHAT_MODEL_TYPE", "ark")

    runner = adapter.AgentScopePackageRunner(
        model_factory=None,
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: None,
    )

    model = runner._build_model()

    assert isinstance(model, FakeOpenAIModel)
    assert seen["kwargs"]["model_name"] == "doubao-seed-2-0-code-preview-260215"
    assert seen["kwargs"]["api_key"] == "ark-key"
    assert seen["kwargs"]["client_kwargs"]["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"


def test_package_runner_uses_openai_compatible_model_for_qwen_provider(monkeypatch):
    import agents.runtime.agentscope_adapter as adapter

    seen = {}

    class FakeOpenAIModel:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs

    monkeypatch.setattr(adapter, "OpenAIChatModel", FakeOpenAIModel)
    monkeypatch.setattr(adapter.settings, "chat_model_type", "ark")
    monkeypatch.setattr(adapter.settings.qwen, "chat_model", "qwen-max-latest")
    monkeypatch.setattr(adapter.settings.qwen, "key", "qwen-key")
    monkeypatch.setattr(adapter.settings.qwen, "base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("CHAT_MODEL_TYPE", "qwen")

    runner = adapter.AgentScopePackageRunner(
        model_factory=None,
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: None,
    )

    model = runner._build_model()

    assert isinstance(model, FakeOpenAIModel)
    assert seen["kwargs"]["model_name"] == "qwen-max-latest"
    assert seen["kwargs"]["api_key"] == "qwen-key"
    assert seen["kwargs"]["client_kwargs"]["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_package_runner_uses_openai_formatter_for_qwen_provider(monkeypatch):
    import agents.runtime.agentscope_adapter as adapter

    class FakeOpenAIFormatter:
        pass

    monkeypatch.setattr(adapter, "OpenAIChatFormatter", FakeOpenAIFormatter)
    monkeypatch.setenv("CHAT_MODEL_TYPE", "qwen")

    runner = adapter.AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=None,
        agent_factory=lambda **kwargs: None,
    )

    formatter = runner._build_formatter()

    assert isinstance(formatter, FakeOpenAIFormatter)


@pytest.mark.asyncio
async def test_package_runner_emits_llm_span_for_agent_invocation_failure():
    from agents.runtime.agentscope_adapter import AgentScopePackageRunner
    from agents.runtime.agentscope_runtime import AgentScopeRunContext

    events = []

    class RecordingHandler:
        async def on_llm_start(self, serialized, prompts, **kwargs):
            events.append(("llm_start", serialized.get("name"), prompts, kwargs.get("metadata")))

        async def on_llm_error(self, error, **kwargs):
            events.append(("llm_error", str(error)))

    class BrokenAgent:
        async def __call__(self, msg, structured_model=None):
            raise RuntimeError("StreamReader decode failed")

    runner = AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: BrokenAgent(),
    )
    context = AgentScopeRunContext(
        task_type="data_analysis",
        query="去年亏损",
        session_id="s-llm-span",
        thread_id="th-llm-span",
        security_context={},
        workflow_state={},
        enabled_skills=[],
        tools=[],
        tool_catalog=_finance_catalog(),
        system_prompt="system prompt",
        callbacks=[RecordingHandler()],
    )

    result = await runner(context)

    assert result.risk_flags[0]["code"] == "agentscope_adapter_error"
    assert ("llm_start", "agentscope.llm.data_analysis_agent", ["去年亏损"], {
        "task_type": "data_analysis",
        "session_id": "s-llm-span",
        "thread_id": "th-llm-span",
        "agent": "data_analysis_agent",
        "runner_backend": "agentscope",
    }) in events
    assert ("llm_error", "StreamReader decode failed") in events


@pytest.mark.asyncio
async def test_package_runner_uses_agentscope_agent_and_toolkit_with_injected_factory():
    from agents.runtime.agentscope_adapter import AgentScopePackageRunner
    from agents.runtime.agentscope_runtime import AgentScopeRunContext
    from agentscope.message import Msg

    seen = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            seen["agent_kwargs"] = kwargs

        async def __call__(self, msg):
            seen["msg"] = msg
            toolkit = seen["agent_kwargs"]["toolkit"]
            seen["schemas"] = toolkit.get_json_schemas()
            return Msg(
                name="assistant",
                role="assistant",
                content=(
                    "AgentScope 复杂分析计划已生成。\n"
                    "请将草稿交回 SQL Harness。"
                ),
                metadata={
                    "structured_output": {
                        "answer": "AgentScope 复杂分析计划已生成。",
                        "sql_drafts": [
                            {
                                "sql": "select count(*) from finance_revenue",
                                "tables": ["finance_revenue"],
                            }
                        ],
                        "state_patch": {"real_agentscope": True},
                    }
                },
            )

    def fake_agent_factory(**kwargs):
        return FakeAgent(**kwargs)

    runner = AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=lambda: object(),
        agent_factory=fake_agent_factory,
    )
    catalog = _finance_catalog()
    tools = catalog.get_tools(
        "complex_analysis",
        security_context={"allowed_tables": ["finance_revenue"]},
    )
    context = AgentScopeRunContext(
        task_type="complex_analysis",
        query="分析收入关系",
        session_id="s-real",
        thread_id="th-real",
        security_context={"user_id": "real-user", "allowed_tables": ["finance_revenue"]},
        workflow_state={},
        enabled_skills=[],
        tools=tools,
        tool_catalog=catalog,
        system_prompt="system prompt",
    )

    result = await runner(context)

    assert seen["agent_kwargs"]["name"] == "complex_analysis_agent"
    assert seen["agent_kwargs"]["sys_prompt"].startswith("system prompt")
    assert "sql_draft_submit" in seen["agent_kwargs"]["sys_prompt"]
    assert seen["agent_kwargs"]["max_iters"] == 6
    assert seen["msg"].get_text_content() == "分析收入关系"
    assert any(
        schema["function"]["name"] == "sql_draft_submit"
        for schema in seen["schemas"]
    )
    assert "AgentScope 复杂分析计划" in result.answer
    assert result.sql_drafts[0]["sql"] == "select count(*) from finance_revenue"
    assert result.state_patch["real_agentscope"] is True
    assert result.state_patch["agentscope_backend"] == "agentscope"


@pytest.mark.asyncio
async def test_package_runner_prompts_with_toolkit_function_names_for_data_analysis():
    from agents.runtime.agentscope_adapter import AgentScopePackageRunner
    from agents.runtime.agentscope_runtime import AgentScopeRunContext
    from agentscope.message import Msg

    seen = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            seen["sys_prompt"] = kwargs["sys_prompt"]
            seen["toolkit"] = kwargs["toolkit"]

        async def __call__(self, msg, structured_model=None):
            seen["message_text"] = msg.get_text_content()
            return Msg(name="assistant", role="assistant", content="no plan", metadata={})

    runner = AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: FakeAgent(**kwargs),
    )
    catalog = _finance_catalog()
    context = AgentScopeRunContext(
        task_type="data_analysis",
        query="分析收入成本预算回款费用之间的关系",
        session_id="s-tool-names",
        thread_id="th-tool-names",
        security_context={"allowed_tables": ["finance_revenue", "finance_budget"]},
        workflow_state={},
        enabled_skills=[],
        tools=catalog.get_tools(
            "data_analysis",
            security_context={"allowed_tables": ["finance_revenue", "finance_budget"]},
        ),
        tool_catalog=catalog,
        system_prompt="system prompt",
    )

    await runner(context)

    schema_names = {schema["function"]["name"] for schema in seen["toolkit"].get_json_schemas()}
    assert "business_knowledge_search" in schema_names
    assert "analysis_plan_submit" in schema_names
    assert "business_knowledge_search" in seen["sys_prompt"]
    assert "analysis_plan_submit" in seen["message_text"]
    assert "business_knowledge.search" not in seen["message_text"]


@pytest.mark.asyncio
async def test_package_runner_extracts_submitted_analysis_plan_from_tool_trace():
    from agents.runtime.agentscope_adapter import AgentScopePackageRunner
    from agents.runtime.agentscope_runtime import AgentScopeRunContext
    from agentscope.message import Msg

    plan = {
        "mode": "analysis_plan",
        "reason": "单步分析计划",
        "steps": [
            {
                "step": 1,
                "type": "sql",
                "goal": "统计收入",
                "tables": ["finance_revenue"],
                "depends_on": [],
                "merge_keys": [],
            }
        ],
    }

    class FakeAgent:
        def __init__(self, **kwargs):
            self.toolkit = kwargs["toolkit"]

        async def __call__(self, msg, structured_model=None):
            tool = self.toolkit.tools["analysis_plan_submit"]
            await tool.original_func(
                purpose="测试提交计划",
                plan=plan,
            )
            return Msg(name="assistant", role="assistant", content="plan submitted", metadata={})

    runner = AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: FakeAgent(**kwargs),
    )
    catalog = _finance_catalog()
    context = AgentScopeRunContext(
        task_type="data_analysis",
        query="统计收入",
        session_id="s-plan-trace",
        thread_id="th-plan-trace",
        security_context={"allowed_tables": ["finance_revenue"]},
        workflow_state={},
        enabled_skills=[],
        tools=catalog.get_tools(
            "data_analysis",
            security_context={"allowed_tables": ["finance_revenue"]},
        ),
        tool_catalog=catalog,
        system_prompt="system prompt",
    )

    result = await runner(context)

    assert result.state_patch["analysis_plan"] == plan
    assert result.state_patch["requires_harness"] is True
    assert result.state_patch["agentscope_backend"] == "agentscope"


@pytest.mark.asyncio
async def test_package_runner_prefers_successful_analysis_plan_handoff_over_reply_metadata():
    from agents.runtime.agentscope_adapter import AgentScopePackageRunner
    from agents.runtime.agentscope_runtime import AgentScopeRunContext
    from agentscope.message import Msg

    plan = {
        "mode": "analysis_plan",
        "reason": "工具提交的规范化计划",
        "steps": [
            {
                "step": 1,
                "type": "sql",
                "goal": "统计收入",
                "tables": ["finance_revenue"],
                "depends_on": [],
                "merge_keys": [],
            }
        ],
    }
    stale_metadata_plan = {
        "plan_id": "model-side-wrapper",
        "plan": plan,
    }

    class FakeAgent:
        def __init__(self, **kwargs):
            self.toolkit = kwargs["toolkit"]

        async def __call__(self, msg, structured_model=None):
            tool = self.toolkit.tools["analysis_plan_submit"]
            await tool.original_func(
                purpose="测试提交计划",
                plan=plan,
            )
            return Msg(
                name="assistant",
                role="assistant",
                content="plan submitted",
                metadata={"analysis_plan": stale_metadata_plan},
            )

    runner = AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: FakeAgent(**kwargs),
    )
    catalog = _finance_catalog()
    context = AgentScopeRunContext(
        task_type="data_analysis",
        query="统计收入",
        session_id="s-plan-trace-authority",
        thread_id="th-plan-trace-authority",
        security_context={"allowed_tables": ["finance_revenue"]},
        workflow_state={},
        enabled_skills=[],
        tools=catalog.get_tools(
            "data_analysis",
            security_context={"allowed_tables": ["finance_revenue"]},
        ),
        tool_catalog=catalog,
        system_prompt="system prompt",
    )

    result = await runner(context)

    assert result.state_patch["analysis_plan"] == plan
    assert result.state_patch["requires_harness"] is True


@pytest.mark.asyncio
async def test_package_runner_submits_structured_data_analysis_plan_to_harness():
    from agents.runtime.agentscope_adapter import AgentScopePackageRunner
    from agents.runtime.agentscope_runtime import AgentScopeRunContext
    from agentscope.message import Msg

    plan = {
        "mode": "analysis_plan",
        "reason": "结构化输出计划",
        "steps": [
            {
                "step": 1,
                "type": "sql",
                "goal": "统计收入",
                "tables": ["finance_revenue"],
                "depends_on": [],
                "merge_keys": [],
            }
        ],
        "requires_user_confirmation": True,
    }
    seen = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        async def __call__(self, msg, structured_model=None):
            seen["structured_model"] = structured_model
            return Msg(
                name="assistant",
                role="assistant",
                content="structured plan",
                metadata={
                    "analysis_plan": plan,
                    "answer": "计划已生成",
                },
            )

    runner = AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: FakeAgent(**kwargs),
    )
    catalog = _finance_catalog()
    context = AgentScopeRunContext(
        task_type="data_analysis",
        query="统计收入",
        session_id="s-structured-plan",
        thread_id="th-structured-plan",
        security_context={"allowed_tables": ["finance_revenue"]},
        workflow_state={},
        enabled_skills=[],
        tools=catalog.get_tools(
            "data_analysis",
            security_context={"allowed_tables": ["finance_revenue"]},
        ),
        tool_catalog=catalog,
        system_prompt="system prompt",
    )

    result = await runner(context)

    assert seen["structured_model"] is not None
    assert result.state_patch["analysis_plan"] == plan
    assert result.state_patch["requires_harness"] is True
    assert [trace["tool_name"] for trace in context.tool_trace] == ["analysis_plan.submit"]


@pytest.mark.asyncio
async def test_package_runner_toolkit_returns_tool_result_inline_not_background_task():
    from agents.runtime.agentscope_adapter import AgentScopePackageRunner
    from agents.runtime.agentscope_runtime import AgentScopeRunContext
    from agentscope.message import Msg

    seen = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            seen["toolkit"] = kwargs["toolkit"]

        async def __call__(self, msg, structured_model=None):
            tool = seen["toolkit"].tools["schema_list_tables"]
            response = await tool.original_func()
            seen["tool_response_text"] = response.content[0]["text"]
            return Msg(name="assistant", role="assistant", content="no plan", metadata={})

    runner = AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: FakeAgent(**kwargs),
    )
    catalog = _finance_catalog()
    context = AgentScopeRunContext(
        task_type="data_analysis",
        query="收入成本预算回款费用之间的关系",
        session_id="s-inline-tools",
        thread_id="th-inline-tools",
        security_context={"allowed_tables": ["finance_revenue", "finance_budget"]},
        workflow_state={},
        enabled_skills=[],
        tools=catalog.get_tools(
            "data_analysis",
            security_context={"allowed_tables": ["finance_revenue", "finance_budget"]},
        ),
        tool_catalog=catalog,
        system_prompt="system prompt",
    )

    await runner(context)

    assert "executing asynchronously" not in seen["tool_response_text"]
    assert "wait_task" not in seen["tool_response_text"]
    assert "finance_revenue" in seen["tool_response_text"]
    assert [trace["tool_name"] for trace in context.tool_trace] == ["schema.list_tables"]


@pytest.mark.asyncio
async def test_package_runner_recovers_markdown_analysis_plan_submit_attempt():
    from agents.runtime.agentscope_adapter import AgentScopePackageRunner
    from agents.runtime.agentscope_runtime import AgentScopeRunContext
    from agentscope.message import Msg

    markdown_plan = """
    ### Analysis Plan
    Data Sources:
    - `finance_revenue`
    - `finance_cost`
    - `finance_budget`
    - `finance_receivable`
    """

    class FakeAgent:
        def __init__(self, **kwargs):
            self.toolkit = kwargs["toolkit"]

        async def __call__(self, msg, structured_model=None):
            tool = self.toolkit.tools["analysis_plan_submit"]
            await tool.original_func(analysis_plan=markdown_plan)
            return Msg(name="assistant", role="assistant", content="", metadata={})

    runner = AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: FakeAgent(**kwargs),
    )
    catalog = _finance_catalog()
    context = AgentScopeRunContext(
        task_type="data_analysis",
        query="收入成本预算回款费用之间的关系",
        session_id="s-markdown-plan",
        thread_id="th-markdown-plan",
        security_context={
            "allowed_tables": [
                "finance_revenue",
                "finance_cost",
                "finance_budget",
                "finance_receivable",
            ]
        },
        workflow_state={},
        enabled_skills=[],
        tools=catalog.get_tools(
            "data_analysis",
            security_context={
                "allowed_tables": [
                    "finance_revenue",
                    "finance_cost",
                    "finance_budget",
                    "finance_receivable",
                ]
            },
        ),
        tool_catalog=catalog,
        system_prompt="system prompt",
    )

    result = await runner(context)

    plan = result.state_patch["analysis_plan"]
    trace_names = [trace["tool_name"] for trace in context.tool_trace]
    trace_statuses = [trace["status"] for trace in context.tool_trace]
    assert trace_names == ["analysis_plan.submit"]
    assert trace_statuses == ["success"]
    assert plan["mode"] == "analysis_plan"
    assert [step["type"] for step in plan["steps"]] == ["sql", "python_merge", "report"]
    assert plan["steps"][0]["tables"] == [
        "finance_revenue",
        "finance_cost",
        "finance_budget",
        "finance_receivable",
    ]
    assert result.state_patch["requires_harness"] is True


@pytest.mark.asyncio
async def test_package_runner_normalizes_partial_analysis_plan_submit_attempt():
    from agents.runtime.agentscope_adapter import AgentScopePackageRunner
    from agents.runtime.agentscope_runtime import AgentScopeRunContext
    from agentscope.message import Msg

    partial_plan = {
        "mode": "analysis_plan",
        "reason": "需要结合 t_journal_item、t_account、t_budget、t_receivable_payable 分析关系。",
        "steps": [
            {
                "step": 1,
                "name": "梳理指标关系",
                "description": "收入成本费用来自凭证分录，预算来自预算表，回款来自应收应付。",
            }
        ],
    }

    class FakeAgent:
        def __init__(self, **kwargs):
            self.toolkit = kwargs["toolkit"]

        async def __call__(self, msg, structured_model=None):
            await self.toolkit.tools["semantic_model_search"].original_func(
                table_names=[
                    "t_journal_item",
                    "t_account",
                    "t_budget",
                    "t_receivable_payable",
                ]
            )
            tool = self.toolkit.tools["analysis_plan_submit"]
            await tool.original_func(purpose="测试半结构化计划恢复", plan=partial_plan)
            return Msg(name="assistant", role="assistant", content="", metadata={})

    runner = AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: FakeAgent(**kwargs),
    )
    catalog = _realistic_finance_catalog()
    allowed_tables = [
        "t_journal_item",
        "t_account",
        "t_budget",
        "t_receivable_payable",
    ]
    context = AgentScopeRunContext(
        task_type="data_analysis",
        query="收入成本预算回款费用之间的关系",
        session_id="s-partial-plan",
        thread_id="th-partial-plan",
        security_context={"allowed_tables": allowed_tables},
        workflow_state={},
        enabled_skills=[],
        tools=catalog.get_tools(
            "data_analysis",
            security_context={"allowed_tables": allowed_tables},
        ),
        tool_catalog=catalog,
        system_prompt="system prompt",
    )

    result = await runner(context)

    plan = result.state_patch["analysis_plan"]
    trace_names = [trace["tool_name"] for trace in context.tool_trace]
    trace_statuses = [trace["status"] for trace in context.tool_trace if trace["tool_name"] == "analysis_plan.submit"]
    assert trace_names == [
        "semantic_model.search",
        "analysis_plan.submit",
    ]
    assert trace_statuses == ["success"]
    assert plan["mode"] == "analysis_plan"
    assert [step["type"] for step in plan["steps"]] == ["sql", "python_merge", "report"]
    assert plan["steps"][0]["tables"] == allowed_tables
    assert result.state_patch["requires_harness"] is True


@pytest.mark.asyncio
async def test_package_runner_does_not_inject_sqlreact_context_into_initial_message():
    from agents.runtime.agentscope_adapter import AgentScopePackageRunner
    from agents.runtime.agentscope_runtime import AgentScopeRunContext
    from agentscope.message import Msg

    seen = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        async def __call__(self, msg):
            seen["message_text"] = msg.get_text_content()
            return Msg(name="assistant", role="assistant", content="ok", metadata={})

    runner = AgentScopePackageRunner(
        model_factory=lambda: object(),
        formatter_factory=lambda: object(),
        agent_factory=lambda **kwargs: FakeAgent(**kwargs),
    )
    context = AgentScopeRunContext(
        task_type="complex_analysis",
        query="分析收入成本关系",
        session_id="s-context",
        thread_id="th-context",
        security_context={},
        workflow_state={
            "selected_tables": ["t_revenue", "t_cost"],
            "table_relationships": [
                {
                    "from_table": "t_revenue",
                    "from_column": "project_id",
                    "to_table": "t_cost",
                    "to_column": "project_id",
                }
            ],
            "evidence": ["术语: 收入成本关系\n公式: 收入 - 成本"],
            "semantic_model": {
                "t_revenue": {
                    "amount": {"business_name": "收入金额"},
                }
            },
            "feasibility_decision": {"execution_mode": "complex_plan"},
        },
        enabled_skills=[],
        tools=[],
        tool_catalog=_finance_catalog(),
        system_prompt="system",
    )

    await runner(context)

    assert seen["message_text"] == "分析收入成本关系"
    assert "已知 SQLReact 上下文" not in seen["message_text"]
    assert "优先使用上述上下文" not in seen["message_text"]


@pytest.mark.asyncio
async def test_local_runner_submits_complex_analysis_sql_draft_to_harness():
    from agents.runtime.agentscope_adapter import LocalAgentScopeCompatibleRunner
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    runtime = AgentScopeRuntime(
        tool_catalog=_finance_catalog(),
        runner=LocalAgentScopeCompatibleRunner(),
    )

    result = await runtime.run(
        task_type="complex_analysis",
        query="分析今年收入、成本、预算、回款和费用之间的关系",
        session_id="s-complex-local",
        security_context={
            "user_id": "finance-user",
            "allowed_tables": [
                "finance_revenue",
                "finance_cost",
                "finance_budget",
                "finance_receivable",
                "finance_expense",
            ],
        },
        workflow_state={
            "thread_id": "th-complex-local",
            "selected_tables": [
                "finance_revenue",
                "finance_cost",
                "finance_budget",
                "finance_receivable",
                "finance_expense",
            ],
        },
    )

    trace_names = [trace["tool_name"] for trace in result.tool_trace]

    assert "AgentScope 复杂分析计划" in result.answer
    assert "SQL Harness" in result.answer
    assert trace_names[0] == "semantic_model.search"
    assert "schema.related_tables" in trace_names
    assert trace_names[-1] == "sql_draft.submit"
    assert result.sql_drafts
    assert result.sql_drafts[0]["execution_mode"] == "draft_only"
    assert result.sql_drafts[0]["requires_harness"] is True
    assert result.sql_drafts[0]["harness_steps"] == [
        "safety_check",
        "authorize_sql",
        "approve",
        "execute_sql",
    ]
    assert result.state_patch["agentscope_backend"] == "local_compatible"
    assert result.state_patch["requires_harness"] is True
    assert result.state_patch["candidate_tables"] == [
        "finance_revenue",
        "finance_cost",
        "finance_budget",
        "finance_receivable",
        "finance_expense",
    ]
    assert all("execute" not in trace["tool_name"] for trace in result.tool_trace)


@pytest.mark.asyncio
async def test_local_runner_submits_data_analysis_plan_to_harness_without_sqlreact_context():
    from agents.runtime.agentscope_adapter import LocalAgentScopeCompatibleRunner
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    runtime = AgentScopeRuntime(
        tool_catalog=_realistic_finance_catalog(),
        runner=LocalAgentScopeCompatibleRunner(),
    )

    result = await runtime.run(
        task_type="data_analysis",
        query="分析今年收入、成本、预算、回款和费用之间的关系",
        session_id="s-data-local",
        security_context={"allowed_tables": [
            "t_journal_entry",
            "t_journal_item",
            "t_account",
            "t_budget",
            "t_receivable_payable",
            "t_expense_claim",
            "t_cost_center",
        ]},
        workflow_state={"thread_id": "th-data-local"},
    )

    trace_names = [trace["tool_name"] for trace in result.tool_trace]

    assert trace_names == [
        "business_knowledge.search",
        "schema.list_tables",
        "semantic_model.search",
        "schema.related_tables",
        "analysis_plan.submit",
    ]
    assert result.sql_drafts == []
    assert result.state_patch["analysis_plan"]["mode"] == "analysis_plan"
    plan_steps = result.state_patch["analysis_plan"]["steps"]
    assert plan_steps[0]["type"] == "sql"
    assert all("sql" not in step for step in plan_steps)
    assert any(step["type"] in {"python_merge", "report"} and step.get("depends_on") for step in plan_steps)
    assert result.state_patch["requires_harness"] is True
    assert "SQLReact" not in result.answer
    assert "SQL Harness" in result.answer
    assert "最终经营结论" in result.answer
    candidate_tables = result.state_patch["candidate_tables"]
    assert candidate_tables == [
        "t_journal_entry",
        "t_journal_item",
        "t_account",
        "t_budget",
        "t_receivable_payable",
        "t_expense_claim",
    ]
    assert "t_user_role" not in candidate_tables
    assert "t_cost_center" not in candidate_tables


@pytest.mark.asyncio
async def test_local_runner_uses_semantic_model_to_cover_finance_topics():
    from agents.runtime.agentscope_adapter import LocalAgentScopeCompatibleRunner
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    runtime = AgentScopeRuntime(
        tool_catalog=_realistic_finance_catalog(),
        runner=LocalAgentScopeCompatibleRunner(),
    )

    result = await runtime.run(
        task_type="complex_analysis",
        query="收入成本预算回款费用之间的关系",
        session_id="s-realistic-finance",
        security_context={"allowed_tables": [
            "t_journal_entry",
            "t_journal_item",
            "t_account",
            "t_budget",
            "t_receivable_payable",
            "t_expense_claim",
            "t_cost_center",
            "t_user_role",
        ]},
    )

    candidate_tables = result.state_patch["candidate_tables"]
    assert candidate_tables == []
    assert result.sql_drafts == []
    assert "AgentScope package is unavailable" in result.risk_flags[0]["message"]
    assert "当前未运行真实 AgentScope" in result.answer


@pytest.mark.asyncio
async def test_local_runner_prefers_workflow_state_selected_tables_without_business_topic_hardcode():
    from agents.runtime.agentscope_adapter import LocalAgentScopeCompatibleRunner
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    runtime = AgentScopeRuntime(
        tool_catalog=_realistic_finance_catalog(),
        runner=LocalAgentScopeCompatibleRunner(),
    )

    result = await runtime.run(
        task_type="complex_analysis",
        query="收入成本预算回款费用之间的关系",
        session_id="s-local-state",
        security_context={"allowed_tables": [
            "t_journal_entry",
            "t_journal_item",
            "t_account",
            "t_budget",
            "t_receivable_payable",
            "t_expense_claim",
            "t_cost_center",
            "t_user_role",
        ]},
        workflow_state={
            "selected_tables": ["t_journal_item", "t_budget"],
            "semantic_model": {
                "t_journal_item": {
                    "posting_date": {
                        "table_name": "t_journal_item",
                        "column_name": "posting_date",
                        "column_type": "date",
                        "business_name": "记账日期",
                    },
                    "credit_amount": {
                        "table_name": "t_journal_item",
                        "column_name": "credit_amount",
                        "column_type": "decimal(15,2)",
                        "business_name": "贷方金额",
                    },
                },
                "t_budget": {
                    "budget_year": {
                        "table_name": "t_budget",
                        "column_name": "budget_year",
                        "business_name": "预算年度",
                    },
                    "budget_month": {
                        "table_name": "t_budget",
                        "column_name": "budget_month",
                        "business_name": "预算月份",
                    },
                    "budget_amount": {
                        "table_name": "t_budget",
                        "column_name": "budget_amount",
                        "column_type": "decimal(15,2)",
                        "business_name": "预算金额",
                    },
                },
            },
        },
    )

    assert result.state_patch["candidate_tables"] == ["t_journal_item", "t_budget"]
    assert "t_user_role" not in result.state_patch["candidate_tables"]
    assert result.state_patch["presentation"]["coverage"]["missing_topics"] == []
    assert "当前还不是最终经营结论" in result.answer
    assert "t_receivable_payable" not in result.sql_drafts[0]["sql"]
    assert "t_expense_claim" not in result.sql_drafts[0]["sql"]


@pytest.mark.asyncio
async def test_local_runner_without_selected_tables_returns_no_business_sql_draft():
    from agents.runtime.agentscope_adapter import LocalAgentScopeCompatibleRunner
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    runtime = AgentScopeRuntime(
        tool_catalog=_catalog_with_non_amount_fields(),
        runner=LocalAgentScopeCompatibleRunner(),
    )

    result = await runtime.run(
        task_type="complex_analysis",
        query="分析预算和固定资产折旧关系",
        session_id="s-non-amount",
        security_context={"allowed_tables": ["t_budget", "t_fixed_asset"]},
    )

    assert result.sql_drafts == []
    assert result.state_patch["candidate_tables"] == []
    assert result.risk_flags[0]["code"] == "local_runner_no_context"
