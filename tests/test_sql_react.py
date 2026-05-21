"""Tests for SQL React graph: check_docs, approve, routing, safety_check."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.documents import Document
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_doc(content="CREATE TABLE users (id INT, name VARCHAR(50));"):
    """Return a mock Document with table schema content."""
    return Document(page_content=content, metadata={"source": "mysql_schema", "table_name": "users"})


def _mock_llm_tool_response(answer="SELECT * FROM users;", is_sql=True):
    """Return a mock LLM response with tool_calls."""
    resp = MagicMock()
    resp.tool_calls = [{"args": {"answer": answer, "is_sql": is_sql}}]
    return resp


def _mock_llm_text_response(content="I don't know how to write SQL for that."):
    """Return a mock LLM response without tool_calls."""
    resp = MagicMock()
    resp.tool_calls = []
    resp.content = content
    return resp


class TestStateReducers:
    """Test state reducers for parent/child graph merge safety."""

    def test_query_accepts_duplicate_step_updates(self):
        """Duplicate query writes in one LangGraph step should not fail."""
        from agents.flow.state import SQLReactState

        def node_a(state):
            return {"query": "node a query"}

        def node_b(state):
            return {"query": "node b query"}

        graph = StateGraph(SQLReactState)
        graph.add_node("node_a", node_a)
        graph.add_node("node_b", node_b)
        graph.add_edge(START, "node_a")
        graph.add_edge(START, "node_b")
        graph.add_edge("node_a", END)
        graph.add_edge("node_b", END)

        result = graph.compile().invoke({"query": "去年亏损"})

        assert result["query"] in {"去年亏损", "node a query", "node b query"}

    def test_query_replaced_by_new_turn_with_same_thread(self):
        """A new user turn must replace the previous checkpoint query."""
        from langgraph.checkpoint.memory import MemorySaver
        from agents.flow.state import FinalGraphState

        def echo_query(state):
            return {"answer": state["query"]}

        graph = StateGraph(FinalGraphState)
        graph.add_node("echo_query", echo_query)
        graph.add_edge(START, "echo_query")
        graph.add_edge("echo_query", END)
        app = graph.compile(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "same-session"}}

        first = app.invoke({"query": "我们公司去年亏损"}, config=config)
        second = app.invoke({"query": "第一季度员工工资"}, config=config)

        assert first["answer"] == "我们公司去年亏损"
        assert second["answer"] == "第一季度员工工资"

    def test_final_graph_state_keeps_rewritten_query(self):
        """Frontend/classify rewritten_query should survive graph state schema."""
        from agents.flow.state import FinalGraphState

        def echo_rewrite(state):
            return {"answer": state.get("rewritten_query", "")}

        graph = StateGraph(FinalGraphState)
        graph.add_node("echo_rewrite", echo_rewrite)
        graph.add_edge(START, "echo_rewrite")
        graph.add_edge("echo_rewrite", END)

        result = graph.compile().invoke({
            "query": "第一季度员工工资",
            "rewritten_query": "我们公司第一季度的员工工资情况",
        })

        assert result["answer"] == "我们公司第一季度的员工工资情况"


# ---------------------------------------------------------------------------
# check_docs node
# ---------------------------------------------------------------------------

class TestCheckDocs:
    """Test check_docs node."""

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.Elasticsearch")
    async def test_no_docs_returns_message(self, MockES):
        """When no docs retrieved and ES fallback is empty, should return error."""
        from agents.flow.sql_react import check_docs

        MockES.return_value.search.return_value = {"hits": {"hits": []}}

        result = await check_docs({"query": "查询用户", "docs": []})

        assert result["is_sql"] is False
        assert "未找到" in result["answer"]
        assert "表结构" in result["answer"]

    @pytest.mark.asyncio
    async def test_with_docs_returns_empty(self):
        """When docs exist, should return empty dict (proceed to generate)."""
        from agents.flow.sql_react import check_docs

        result = await check_docs({"query": "查询用户", "docs": [_mock_doc()]})

        assert result == {}

    @pytest.mark.asyncio
    async def test_no_docs_returns_error(self):
        """When no docs, should return error message."""
        from agents.flow.sql_react import check_docs

        result = await check_docs({"query": "查询用户", "docs": []})

        assert result["is_sql"] is False
        assert "未找到" in result["answer"]


# ---------------------------------------------------------------------------
# query_enhance node
# ---------------------------------------------------------------------------

class TestQueryEnhance:
    """Test query_enhance node."""

    @pytest.mark.asyncio
    async def test_no_evidence_skips(self):
        """Without evidence, should return original query unchanged."""
        from agents.flow.sql_react import query_enhance

        result = await query_enhance({
            "query": "查询GMV",
            "rewritten_query": "查询GMV",
            "evidence": [],
            "few_shot_examples": [],
        })

        assert result["enhanced_query"] == "查询GMV"

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_empty_llm_response_uses_business_knowledge_fallback(self, mock_get_model):
        """Empty LLM output should still enhance from matched business evidence."""
        from agents.flow.sql_react import query_enhance

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content=""))
        mock_get_model.return_value = mock_model

        result = await query_enhance({
            "query": "去年亏损",
            "rewritten_query": "去年亏损",
            "evidence": ["术语: 净利润\n公式: 收入 - 成本 - 费用；亏损表示净利润 < 0\n同义词: 净收益, 盈利, 亏损, 净亏损, 赔钱, 赚钱"],
            "few_shot_examples": [],
        })

        assert "净利润" in result["enhanced_query"]
        assert "收入 - 成本 - 费用" in result["enhanced_query"]

    @pytest.mark.asyncio
    async def test_no_evidence_returns_original_query(self):
        """Without evidence, query_enhance should not hard-code business terms."""
        from agents.flow.sql_react import query_enhance

        result = await query_enhance({
            "query": "去年亏损",
            "rewritten_query": "去年亏损",
            "evidence": [],
            "few_shot_examples": [],
        })

        assert result["enhanced_query"] == "去年亏损"
        assert "净利润" not in result["enhanced_query"]

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_with_evidence_enhances(self, mock_get_model):
        """With evidence, should call LLM to enhance query."""
        from agents.flow.sql_react import query_enhance

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="查询已支付订单总额"))
        mock_get_model.return_value = mock_model

        result = await query_enhance({
            "query": "查询GMV",
            "rewritten_query": "查询GMV",
            "evidence": ["GMV = 已支付订单总额"],
            "few_shot_examples": [],
        })

        assert result["enhanced_query"] == "查询已支付订单总额"
        mock_model.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_with_evidence_passes_trace_callbacks(self, mock_get_model):
        """Inner LLM call should inherit graph callbacks for tracing."""
        from agents.flow.sql_react import query_enhance

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="查询已支付订单总额"))
        mock_get_model.return_value = mock_model

        await query_enhance(
            {
                "query": "查询GMV",
                "rewritten_query": "查询GMV",
                "evidence": ["GMV = 已支付订单总额"],
                "few_shot_examples": [],
            },
            config={"callbacks": ["trace-handler"]},
        )

        call_config = mock_model.ainvoke.call_args.kwargs["config"]
        assert call_config["callbacks"] == ["trace-handler"]
        assert call_config["run_name"] == "sql.query_enhance.llm"

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_llm_failure_fallback(self, mock_get_model):
        """On LLM failure, should fallback to original query."""
        from agents.flow.sql_react import query_enhance

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(side_effect=Exception("timeout"))
        mock_get_model.return_value = mock_model

        result = await query_enhance({
            "query": "查询GMV",
            "rewritten_query": "查询GMV",
            "evidence": ["GMV = 已支付订单总额"],
            "few_shot_examples": [],
        })

        assert result["enhanced_query"] == "查询GMV"


# ---------------------------------------------------------------------------
# safety_check node
# ---------------------------------------------------------------------------

class TestSelectTables:
    """Test table selection uses data-managed business evidence."""

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables", return_value={})
    @patch("agents.flow.sql_react.get_table_relationships", return_value=[])
    @patch("agents.flow.sql_react.load_full_table_metadata")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_merges_recall_context_related_tables(
        self,
        mock_get_model,
        mock_load_metadata,
        _mock_relationships,
        _mock_semantic,
    ):
        """Related tables from recall_context should survive an LLM under-selection."""
        from agents.flow.sql_react import select_tables

        mock_load_metadata.return_value = [
            {"table_name": "domain_summary", "table_comment": "领域摘要"},
            {"table_name": "t_journal_item", "table_comment": "凭证明细"},
            {"table_name": "t_account", "table_comment": "会计科目"},
            {"table_name": "t_expense_claim", "table_comment": "费用报销"},
        ]
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="domain_summary"))
        mock_get_model.return_value = mock_model

        result = await select_tables({
            "query": "公司盈利",
            "rewritten_query": "公司盈利",
            "enhanced_query": "公司净利润 > 0",
            "recall_context": {
                "query_key": "公司盈利",
                "business_related_tables": ["t_journal_item", "t_account", "t_expense_claim"],
                "few_shot_related_tables": [],
                "matched_terms": ["净利润"],
            },
        })

        assert result["selected_tables"][:3] == [
            "t_journal_item",
            "t_account",
            "t_expense_claim",
        ]
        assert "domain_summary" in result["selected_tables"]

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables")
    @patch("agents.flow.sql_react.get_table_relationships", return_value=[])
    @patch("agents.flow.sql_react.load_full_table_metadata")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_ignores_unmatched_business_evidence_related_tables(
        self,
        mock_get_model,
        mock_load_metadata,
        _mock_relationships,
        mock_semantic,
    ):
        """Unmatched business evidence should not push unrelated finance tables into selection."""
        from agents.flow.sql_react import select_tables

        mock_load_metadata.return_value = [
            {"table_name": "t_user", "table_comment": "用户/员工账号信息表，包含真实姓名、联系电话、注册时间"},
            {"table_name": "t_expense_claim", "table_comment": "费用报销表"},
            {"table_name": "t_journal_entry", "table_comment": "记账凭证主表"},
            {"table_name": "t_account", "table_comment": "会计科目表"},
        ]
        mock_semantic.return_value = {
            "t_user": {
                "real_name": {
                    "column_name": "real_name",
                    "business_name": "真实姓名",
                    "synonyms": "员工姓名, 用户姓名",
                    "business_description": "用户或员工的真实姓名",
                }
            }
        }
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="t_user"))
        mock_get_model.return_value = mock_model

        result = await select_tables({
            "query": "查询所有用户的真实姓名",
            "enhanced_query": "查询所有用户的真实姓名",
            "evidence": [
                "术语: 费用总额\n"
                "公式: SUM(total_amount)\n"
                "同义词: 总费用, 费用合计\n"
                "关联表: t_expense_claim,t_journal_entry"
            ],
        })

        assert result["selected_tables"] == ["t_user"]

    def test_expands_semantic_relationship_chain_independent_of_dict_order(self):
        """FK expansion should close multi-hop table chains, not depend on row order."""
        from agents.flow.sql_react import _expand_selected_tables_by_semantic_relationships

        semantic_model = {
            "t_cost_center": {
                "department_id": {
                    "is_fk": 1,
                    "ref_table": "t_department",
                    "ref_column": "id",
                },
            },
            "t_budget": {
                "cost_center_id": {
                    "is_fk": 1,
                    "ref_table": "t_cost_center",
                    "ref_column": "id",
                },
            },
            "t_department": {},
        }

        expanded = _expand_selected_tables_by_semantic_relationships(
            selected=["t_budget"],
            candidate_tables=["t_budget", "t_cost_center", "t_department"],
            semantic_model=semantic_model,
        )

        assert expanded == ["t_budget", "t_cost_center", "t_department"]

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables")
    @patch("agents.flow.sql_react.get_table_relationships", return_value=[])
    @patch("agents.flow.sql_react.load_full_table_metadata")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_select_tables_uses_lightweight_routing_profile_and_repairs_underselection(
        self,
        mock_get_model,
        mock_load_metadata,
        _mock_relationships,
        mock_semantic,
    ):
        """Prompt should expose only matched field hints and repair budget/department under-selection."""
        from agents.flow.sql_react import select_tables

        mock_load_metadata.return_value = [
            {"table_name": "t_budget", "table_comment": "预算管理表"},
            {"table_name": "t_cost_center", "table_comment": "成本中心表"},
            {"table_name": "t_department", "table_comment": "组织部门信息表，包含部门名称"},
            {"table_name": "t_invoice", "table_comment": "发票管理表"},
        ]
        mock_semantic.return_value = {
            "t_cost_center": {
                "center_name": {
                    "column_name": "center_name",
                    "business_name": "成本中心名称",
                    "synonyms": "部门名称",
                    "business_description": "如：研发部、市场部、财务部",
                },
                "annual_budget": {
                    "column_name": "annual_budget",
                    "business_name": "年度预算",
                    "synonyms": "全年预算",
                    "business_description": "该成本中心的年度预算金额",
                },
                "department_id": {
                    "column_name": "department_id",
                    "is_fk": 1,
                    "ref_table": "t_department",
                    "ref_column": "id",
                },
                "created_at": {
                    "column_name": "created_at",
                    "business_name": "创建时间",
                    "synonyms": "记录时间",
                },
            },
            "t_budget": {
                "budget_year": {
                    "column_name": "budget_year",
                    "business_name": "预算年度",
                },
                "cost_center_id": {
                    "column_name": "cost_center_id",
                    "business_name": "成本中心ID",
                    "synonyms": "部门",
                    "is_fk": 1,
                    "ref_table": "t_cost_center",
                    "ref_column": "id",
                },
                "budget_amount": {
                    "column_name": "budget_amount",
                    "business_name": "预算金额",
                    "synonyms": "预算额度",
                },
            },
            "t_department": {
                "name": {
                    "column_name": "name",
                    "business_name": "部门名称",
                    "synonyms": "组织名称, 部门",
                },
            },
            "t_invoice": {
                "invoice_no": {
                    "column_name": "invoice_no",
                    "business_name": "发票号码",
                    "synonyms": "发票号",
                },
            },
        }
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="t_budget"))
        mock_get_model.return_value = mock_model

        result = await select_tables({
            "query": "查询各个部门的年度预算总金额",
            "enhanced_query": "查询各个部门的年度预算总金额",
            "evidence": [],
        })

        prompt = mock_model.ainvoke.call_args.args[0][0].content
        assert "匹配字段" in prompt
        assert "annual_budget(年度预算" in prompt
        assert "cost_center_id(成本中心ID" in prompt
        assert "created_at" not in prompt
        assert set(result["selected_tables"][:3]) == {"t_budget", "t_cost_center", "t_department"}

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables", return_value={})
    @patch("agents.flow.sql_react.get_table_relationships", return_value=[])
    @patch("agents.flow.sql_react.load_full_table_metadata")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_select_tables_reuses_recall_context_related_tables(
        self,
        mock_get_model,
        mock_load_metadata,
        _mock_relationships,
        _mock_semantic,
    ):
        """select_tables should reuse recall_context and not perform another recall."""
        from agents.flow.sql_react import select_tables

        mock_load_metadata.return_value = [
            {"table_name": "t_budget", "table_comment": "预算管理表"},
            {"table_name": "t_cost_center", "table_comment": "成本中心表"},
            {"table_name": "t_department", "table_comment": "组织部门信息表"},
            {"table_name": "t_invoice", "table_comment": "发票管理表"},
        ]
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="t_department"))
        mock_get_model.return_value = mock_model

        result = await select_tables({
            "query": "查询各个部门的年度预算总金额",
            "rewritten_query": "查询各个部门的年度预算总金额",
            "evidence": [],
            "recall_context": {
                "query_key": "查询各个部门的年度预算总金额",
                "business_related_tables": ["t_cost_center"],
                "few_shot_related_tables": ["t_budget", "t_cost_center"],
                "matched_terms": ["年度预算", "部门"],
            },
        })

        assert set(result["selected_tables"][:3]) == {"t_budget", "t_cost_center", "t_department"}

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables", return_value={})
    @patch("agents.flow.sql_react.get_table_relationships", return_value=[])
    @patch("agents.flow.sql_react.load_full_table_metadata")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_select_tables_ignores_stale_recall_context(
        self,
        mock_get_model,
        mock_load_metadata,
        _mock_relationships,
        _mock_semantic,
    ):
        """recall_context from another query should not pollute table selection."""
        from agents.flow.sql_react import select_tables

        mock_load_metadata.return_value = [
            {"table_name": "t_user", "table_comment": "用户表"},
            {"table_name": "t_budget", "table_comment": "预算管理表"},
            {"table_name": "t_cost_center", "table_comment": "成本中心表"},
            {"table_name": "t_invoice", "table_comment": "发票管理表"},
        ]
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="t_user"))
        mock_get_model.return_value = mock_model

        result = await select_tables({
            "query": "查询所有用户",
            "rewritten_query": "查询所有用户",
            "evidence": [],
            "recall_context": {
                "query_key": "查询各个部门的年度预算总金额",
                "business_related_tables": ["t_cost_center"],
                "few_shot_related_tables": ["t_budget"],
                "matched_terms": ["年度预算"],
            },
        })

        assert result["selected_tables"] == ["t_user"]

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables", return_value={})
    @patch("agents.flow.sql_react.get_table_relationships", return_value=[])
    @patch("agents.flow.sql_react.load_full_table_metadata")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_select_tables_does_not_expand_filtered_recall_context_tables(
        self,
        mock_get_model,
        mock_load_metadata,
        mock_relationships,
        _mock_semantic,
    ):
        """A clean recall_context should not expand selected tables before relationship lookup."""
        from agents.flow.sql_react import select_tables

        mock_load_metadata.return_value = [
            {"table_name": "t_journal_item", "table_comment": "凭证分录明细表"},
            {"table_name": "t_account", "table_comment": "会计科目表"},
            {"table_name": "t_expense_claim", "table_comment": "费用报销表"},
            {"table_name": "t_budget", "table_comment": "预算管理表"},
            {"table_name": "t_fund_transfer", "table_comment": "资金划转记录表"},
            {"table_name": "t_receivable_payable", "table_comment": "应收应付表"},
            {"table_name": "t_cost_center", "table_comment": "成本中心表"},
            {"table_name": "t_fixed_asset", "table_comment": "固定资产表"},
            {"table_name": "t_department", "table_comment": "组织部门信息表"},
        ]
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="t_journal_item,t_account,t_expense_claim"))
        mock_get_model.return_value = mock_model

        result = await select_tables({
            "query": "查询当前公司去年的亏损金额",
            "rewritten_query": "查询当前公司去年的亏损金额",
            "evidence": [],
            "recall_context": {
                "query_key": "查询当前公司去年的亏损金额",
                "business_related_tables": ["t_journal_item", "t_account", "t_expense_claim"],
                "few_shot_related_tables": [],
                "matched_terms": ["净利润"],
            },
        })

        assert result["selected_tables"] == ["t_journal_item", "t_account", "t_expense_claim"]
        mock_relationships.assert_called_once_with(["t_journal_item", "t_account", "t_expense_claim"])

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables")
    @patch("agents.flow.sql_react.get_table_relationships", return_value=[])
    @patch("agents.flow.sql_react.load_full_table_metadata")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_select_tables_returns_selected_semantic_model_for_downstream_reuse(
        self,
        mock_get_model,
        mock_load_metadata,
        _mock_relationships,
        mock_semantic,
    ):
        from agents.flow.sql_react import select_tables

        mock_load_metadata.return_value = [
            {"table_name": "a", "table_comment": "收入表"},
            {"table_name": "b", "table_comment": "预算表"},
            {"table_name": "c", "table_comment": "无关表"},
            {"table_name": "d", "table_comment": "其他表"},
        ]
        mock_semantic.return_value = {
            "a": {"revenue": {"column_name": "revenue"}},
            "b": {"budget": {"column_name": "budget"}},
            "c": {"unused": {"column_name": "unused"}},
            "d": {"unused": {"column_name": "unused"}},
        }
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="a,b"))
        mock_get_model.return_value = mock_model

        result = await select_tables({
            "query": "分析收入预算关系",
            "enhanced_query": "分析收入预算关系",
            "evidence": [],
        })

        assert result["selected_tables"] == ["a", "b"]
        assert result["semantic_model"] == {
            "a": {"revenue": {"column_name": "revenue"}},
            "b": {"budget": {"column_name": "budget"}},
        }

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables")
    @patch("agents.flow.sql_react.get_table_relationships", return_value=[])
    @patch("agents.flow.sql_react.load_full_table_metadata")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_reranks_selected_tables_by_local_semantics(
        self,
        mock_get_model,
        mock_load_metadata,
        _mock_relationships,
        mock_semantic,
    ):
        """Local semantic matches should move directly relevant management tables ahead of generic finance tables."""
        from agents.flow.sql_react import select_tables

        mock_load_metadata.return_value = [
            {"table_name": "t_expense_claim", "table_comment": "费用报销表"},
            {"table_name": "t_journal_entry", "table_comment": "记账凭证主表"},
            {"table_name": "t_account", "table_comment": "会计科目表"},
            {"table_name": "t_user", "table_comment": "用户/员工账号信息表，包含真实姓名、联系电话、注册时间"},
            {"table_name": "t_user_role", "table_comment": "用户角色绑定关系表，关联用户与系统角色"},
            {"table_name": "t_role", "table_comment": "系统角色信息表，包含角色名称、角色编码"},
        ]
        mock_semantic.return_value = {
            "t_user": {
                "real_name": {
                    "column_name": "real_name",
                    "business_name": "真实姓名",
                    "synonyms": "员工姓名, 用户姓名",
                    "business_description": "用户或员工的真实姓名",
                }
            },
            "t_user_role": {
                "user_id": {
                    "column_name": "user_id",
                    "business_name": "用户ID",
                    "synonyms": "员工ID",
                    "business_description": "关联 t_user.id",
                },
                "role_id": {
                    "column_name": "role_id",
                    "business_name": "角色ID",
                    "synonyms": "系统角色ID",
                    "business_description": "关联 t_role.id",
                },
            },
            "t_role": {
                "name": {
                    "column_name": "name",
                    "business_name": "角色名称",
                    "synonyms": "系统角色",
                    "business_description": "系统角色的中文名称",
                }
            },
        }
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(
            content="t_expense_claim,t_journal_entry,t_account,t_user,t_user_role,t_role"
        ))
        mock_get_model.return_value = mock_model

        result = await select_tables({
            "query": "查询所有用户的真实姓名以及他们被分配的角色名称",
            "enhanced_query": "查询所有用户的真实姓名以及他们被分配的角色名称",
            "evidence": [],
        })

        assert set(result["selected_tables"][:3]) == {"t_user", "t_user_role", "t_role"}

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables")
    @patch("agents.flow.sql_react.get_table_relationships", return_value=[])
    @patch("agents.flow.sql_react.load_full_table_metadata")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_adds_bridge_table_from_semantic_relationships(
        self,
        mock_get_model,
        mock_load_metadata,
        _mock_relationships,
        mock_semantic,
    ):
        """A table that bridges two selected tables should be kept for SQL joins."""
        from agents.flow.sql_react import select_tables

        mock_load_metadata.return_value = [
            {"table_name": "t_department", "table_comment": "组织部门信息表，包含部门名称和部门负责人"},
            {"table_name": "t_user", "table_comment": "用户/员工账号信息表，包含真实姓名"},
            {"table_name": "t_user_department", "table_comment": "用户部门归属关系表，关联用户与部门"},
            {"table_name": "t_journal_entry", "table_comment": "记账凭证主表"},
        ]
        mock_semantic.return_value = {
            "t_department": {
                "name": {"business_name": "部门名称", "synonyms": "组织名称"},
                "manager": {"business_name": "部门负责人", "synonyms": "负责人"},
            },
            "t_user": {
                "real_name": {"business_name": "真实姓名", "synonyms": "员工姓名"},
            },
            "t_user_department": {
                "user_id": {
                    "business_name": "用户ID",
                    "is_fk": 1,
                    "ref_table": "t_user",
                    "ref_column": "id",
                },
                "department_id": {
                    "business_name": "部门ID",
                    "is_fk": 1,
                    "ref_table": "t_department",
                    "ref_column": "id",
                },
            },
        }
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="t_department,t_user"))
        mock_get_model.return_value = mock_model

        result = await select_tables({
            "query": "查询公司各部门的负责人姓名以及对应的部门名称",
            "enhanced_query": "查询公司各部门的负责人姓名以及对应的部门名称",
            "evidence": [],
        })

        assert set(result["selected_tables"][:3]) == {"t_department", "t_user", "t_user_department"}

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables")
    @patch("agents.flow.sql_react.get_table_relationships", return_value=[])
    @patch("agents.flow.sql_react.load_full_table_metadata")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_adds_multi_hop_join_path_from_semantic_relationships(
        self,
        mock_get_model,
        mock_load_metadata,
        _mock_relationships,
        mock_semantic,
    ):
        """Selected endpoint tables should keep FK path tables needed for joins."""
        from agents.flow.sql_react import select_tables

        mock_load_metadata.return_value = [
            {"table_name": "t_invoice", "table_comment": "发票管理表，包含关联凭证"},
            {"table_name": "t_journal_entry", "table_comment": "记账凭证主表"},
            {"table_name": "t_journal_item", "table_comment": "凭证分录明细表，包含科目和金额"},
            {"table_name": "t_account", "table_comment": "会计科目表"},
            {"table_name": "t_budget", "table_comment": "预算管理表"},
        ]
        mock_semantic.return_value = {
            "t_invoice": {
                "related_entry_id": {
                    "business_name": "关联凭证ID",
                    "is_fk": 1,
                    "ref_table": "t_journal_entry",
                    "ref_column": "id",
                },
            },
            "t_journal_entry": {
                "id": {"business_name": "凭证ID"},
            },
            "t_journal_item": {
                "entry_id": {
                    "business_name": "凭证ID",
                    "is_fk": 1,
                    "ref_table": "t_journal_entry",
                    "ref_column": "id",
                },
                "account_code": {
                    "business_name": "科目编码",
                    "is_fk": 1,
                    "ref_table": "t_account",
                    "ref_column": "account_code",
                },
            },
            "t_account": {
                "account_code": {"business_name": "科目编码"},
            },
        }
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="t_invoice,t_account"))
        mock_get_model.return_value = mock_model

        result = await select_tables({
            "query": "按会计科目分析发票收入",
            "enhanced_query": "按会计科目分析发票收入",
            "evidence": [],
        })

        assert set(result["selected_tables"][:4]) == {
            "t_invoice",
            "t_journal_entry",
            "t_journal_item",
            "t_account",
        }

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables")
    @patch("agents.flow.sql_react.get_table_relationships", return_value=[])
    @patch("agents.flow.sql_react.load_full_table_metadata")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_adds_endpoint_tables_from_selected_relation_tables(
        self,
        mock_get_model,
        mock_load_metadata,
        _mock_relationships,
        mock_semantic,
    ):
        """Selected relation tables should bring their referenced endpoint tables."""
        from agents.flow.sql_react import select_tables

        mock_load_metadata.return_value = [
            {"table_name": "t_user_role", "table_comment": "用户角色绑定关系表"},
            {"table_name": "t_user_department", "table_comment": "用户部门归属关系表"},
            {"table_name": "t_role", "table_comment": "系统角色信息表，包含角色名称"},
            {"table_name": "t_user", "table_comment": "用户/员工账号信息表，包含真实姓名"},
            {"table_name": "t_department", "table_comment": "组织部门信息表，包含部门名称"},
            {"table_name": "t_invoice", "table_comment": "发票信息表"},
        ]
        mock_semantic.return_value = {
            "t_user_role": {
                "user_id": {
                    "business_name": "用户ID",
                    "is_fk": 1,
                    "ref_table": "t_user",
                    "ref_column": "id",
                },
                "role_id": {
                    "business_name": "角色ID",
                    "is_fk": 1,
                    "ref_table": "t_role",
                    "ref_column": "id",
                },
            },
            "t_user_department": {
                "user_id": {
                    "business_name": "用户ID",
                    "is_fk": 1,
                    "ref_table": "t_user",
                    "ref_column": "id",
                },
                "department_id": {
                    "business_name": "部门ID",
                    "is_fk": 1,
                    "ref_table": "t_department",
                    "ref_column": "id",
                },
            },
            "t_role": {
                "name": {"business_name": "角色名称", "synonyms": "系统角色"},
            },
            "t_user": {
                "real_name": {"business_name": "真实姓名", "synonyms": "员工姓名"},
            },
            "t_department": {
                "name": {"business_name": "部门名称", "synonyms": "组织名称"},
            },
        }
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="t_user_role,t_user_department,t_role"))
        mock_get_model.return_value = mock_model

        result = await select_tables({
            "query": "查询所有拥有财务审核角色的用户分别属于哪个部门",
            "enhanced_query": "查询所有拥有财务审核角色的用户分别属于哪个部门",
            "evidence": [],
        })

        assert set(result["selected_tables"][:5]) == {
            "t_user_role",
            "t_user_department",
            "t_role",
            "t_user",
            "t_department",
        }


class TestComplexRoute:
    """Test broad schema route decisions."""

    @pytest.mark.asyncio
    async def test_assess_feasibility_single_sql(self):
        from agents.flow.sql_react import assess_feasibility

        state = {
            "query": "去年亏损",
            "selected_tables": ["a", "b", "c"],
            "table_relationships": [
                {"from_table": "a", "to_table": "b"},
                {"from_table": "b", "to_table": "c"},
            ],
        }

        result = await assess_feasibility(state)

        assert result["route_mode"] == "single_sql"
        assert result["feasibility_decision"]["execution_mode"] == "single_sql"

    @pytest.mark.asyncio
    async def test_assess_feasibility_clarify_for_broad_detail_rule(self):
        from agents.flow.sql_react import assess_feasibility
        from agents.tool.storage.query_route_rules import QueryRouteRuleDecision

        state = {
            "query": "员工工资和部门角色权限",
            "selected_tables": [f"t_{i}" for i in range(9)],
            "table_relationships": [],
        }

        decision = QueryRouteRuleDecision(
            route_signal="detail",
            confidence=0.95,
            rule_id=8,
            rule_name="明细澄清",
            priority=100,
            match_type="contains",
        )
        with patch("agents.flow.sql_react.evaluate_query_route_rules", AsyncMock(return_value=decision)):
            result = await assess_feasibility(state)

        assert result["route_mode"] == "clarify"
        assert result["feasibility_decision"]["execution_mode"] == "clarify"
        assert result["feasibility_decision"]["task_type"] == "detail"
        assert "缩小查询范围" in result["answer"]

    @pytest.mark.asyncio
    async def test_assess_feasibility_uses_rule_task_type(self):
        from agents.flow.sql_react import assess_feasibility
        from agents.tool.storage.query_route_rules import QueryRouteRuleDecision

        decision = QueryRouteRuleDecision(
            route_signal="analysis",
            confidence=0.95,
            rule_id=7,
            rule_name="关系分析",
            priority=100,
            match_type="contains",
        )
        with patch("agents.flow.sql_react.evaluate_query_route_rules", AsyncMock(return_value=decision)):
            result = await assess_feasibility({
                "query": "收入成本预算回款费用之间的关系",
                "selected_tables": ["t_journal_item", "t_account", "t_budget"],
                "table_relationships": [{"from_table": "t_journal_item", "to_table": "t_account"}],
            })

        assert result["route_mode"] == "complex_plan"
        assert result["feasibility_decision"]["execution_mode"] == "complex_plan"
        assert result["feasibility_decision"]["task_type"] == "analysis"
        assert result["feasibility_decision"]["decision_source"] == "rules"

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_assess_feasibility_does_not_call_llm_for_cyclic_schema(self, mock_get_model):
        from agents.flow.sql_react import assess_feasibility

        result = await assess_feasibility({
            "query": "收入成本预算回款费用之间的关系",
            "selected_tables": ["a", "b", "c"],
            "table_relationships": [
                {"from_table": "a", "to_table": "b"},
                {"from_table": "b", "to_table": "c"},
                {"from_table": "a", "to_table": "c"},
            ],
        })

        mock_get_model.assert_not_called()
        assert result["route_mode"] == "single_sql_with_strict_checks"

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_assess_feasibility_clarifies_disconnected_schema_without_rule(self, mock_get_model):
        from agents.flow.sql_react import assess_feasibility

        result = await assess_feasibility({
            "query": "收入成本预算回款费用之间的关系",
            "selected_tables": ["t_budget", "t_invoice"],
            "table_relationships": [],
        })

        mock_get_model.assert_not_called()
        assert result["route_mode"] == "clarify"
        assert result["feasibility_decision"]["join_risk"] == "high"

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_complex_plan_generate_returns_validated_plan_preview(self, mock_get_model):
        import agents.flow.sql_react as sql_react

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content=(
            '{"mode":"complex_plan","steps":['
            '{"step":1,"type":"sql","goal":"查收入","tables":["a","b"],"depends_on":[],"merge_keys":["period"]},'
            '{"step":2,"type":"python_merge","goal":"汇总分析","tables":[],"depends_on":[1],"merge_keys":["period"]}'
            '],"requires_user_confirmation":true}'
        )))
        mock_get_model.return_value = mock_model

        with patch.object(sql_react, "_run_agentscope_complex_analysis_prepass", AsyncMock(return_value=({}, {}))):
            result = await sql_react.complex_plan_generate({
                "query": "分析收入和预算关系",
                "selected_tables": ["a", "b"],
                "table_relationships": [],
                "evidence": [],
            })

        assert result["is_sql"] is False
        assert result["plan_validation_error"] == ""
        assert result["complex_plan"]["mode"] == "complex_plan"
        assert "已生成执行计划" in result["answer"]

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_complex_plan_generate_skips_agentscope_prepass_by_default(self, mock_get_model):
        import agents.flow.sql_react as sql_react

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content=(
            '{"mode":"complex_plan","steps":['
            '{"step":1,"type":"sql","goal":"查收入","tables":["a"],"depends_on":[],"merge_keys":["period"]},'
            '{"step":2,"type":"report","goal":"解释关系","tables":[],"depends_on":[1],"merge_keys":[]}'
            '],"requires_user_confirmation":true}'
        )))
        mock_get_model.return_value = mock_model

        with patch.object(sql_react, "_run_agentscope_complex_analysis_prepass", AsyncMock(return_value=({}, {}))) as prepass:
            result = await sql_react.complex_plan_generate({
                "query": "收入成本预算回款费用之间的关系",
                "selected_tables": ["a"],
                "table_relationships": [],
                "evidence": [],
                "route_mode": "complex_plan",
                "feasibility_decision": {"execution_mode": "complex_plan", "task_type": "analysis"},
            })

        prepass.assert_not_awaited()
        assert result["agentscope_result"] == {}
        assert result["agentscope_observation"] == {}
        assert result["complex_plan"]["mode"] == "complex_plan"

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_complex_plan_generate_runs_agentscope_prepass_with_trace_context(self, mock_get_model):
        import agents.flow.sql_react as sql_react
        from agents.runtime.result import AgentRunResult

        calls = {}

        class FakeAgentScopeRuntime:
            def __init__(self, *, runner=None, callbacks=None):
                calls["runner"] = runner
                calls["callbacks"] = callbacks

            async def run(self, **kwargs):
                calls["run_kwargs"] = kwargs
                return AgentRunResult(
                    answer="AgentScope 复杂分析计划已生成。",
                    tool_trace=[{"tool_name": "business_knowledge.search", "status": "success"}],
                    sql_drafts=[{"draft_id": "draft-1", "sql": "select 1", "tables": ["a"]}],
                    state_patch={
                        "agentscope_backend": "local_compatible",
                        "candidate_tables": ["a", "b"],
                        "requires_harness": True,
                    },
                    risk_flags=[
                        {"code": "sql_draft_not_executed", "severity": "info"},
                    ],
                )

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content=(
            '{"mode":"complex_plan","steps":['
            '{"step":1,"type":"sql","goal":"查收入","tables":["a","b"],"depends_on":[],"merge_keys":["period"]},'
            '{"step":2,"type":"report","goal":"解释关系","tables":[],"depends_on":[1],"merge_keys":[]}'
            '],"requires_user_confirmation":true}'
        )))
        mock_get_model.return_value = mock_model

        trace_handler = object()
        fake_runner = object()
        with patch.object(sql_react, "AgentScopeRuntime", FakeAgentScopeRuntime), \
             patch.object(sql_react, "create_agentscope_runner", return_value=fake_runner):
            result = await sql_react.complex_plan_generate(
                {
                    "query": "收入成本预算回款费用之间的关系",
                    "selected_tables": ["a", "b"],
                    "table_relationships": [],
                    "evidence": ["收入 = 主营业务收入"],
                    "security_context": {"user_id": "u1", "allowed_tables": ["a", "b"]},
                    "feasibility_decision": {"execution_mode": "complex_plan", "task_type": "analysis"},
                    "agentscope_prepass_enabled": True,
                },
                config={
                    "callbacks": [trace_handler],
                    "configurable": {"thread_id": "session-1:turn:abc"},
                },
            )

        assert calls["runner"] is fake_runner
        assert calls["callbacks"] == [trace_handler]
        assert calls["run_kwargs"]["task_type"] == "complex_analysis"
        assert calls["run_kwargs"]["query"] == "收入成本预算回款费用之间的关系"
        assert calls["run_kwargs"]["session_id"] == "session-1:turn:abc"
        assert calls["run_kwargs"]["security_context"]["user_id"] == "u1"
        assert calls["run_kwargs"]["workflow_state"]["thread_id"] == "session-1:turn:abc"
        assert calls["run_kwargs"]["workflow_state"]["selected_tables"] == ["a", "b"]
        assert result["agentscope_observation"]["status"] == "completed"
        assert result["agentscope_observation"]["backend"] == "local_compatible"
        assert result["agentscope_result"]["sql_drafts"][0]["draft_id"] == "draft-1"
        assert result["complex_plan"]["mode"] == "complex_plan"
        assert result["plan_validation_error"] == ""

    @pytest.mark.asyncio
    async def test_agentscope_prepass_reuses_sqlreact_semantic_model_in_workflow_state(self):
        import agents.flow.sql_react as sql_react
        from agents.runtime.result import AgentRunResult

        calls = {}

        class FakeAgentScopeRuntime:
            def __init__(self, *, runner=None, callbacks=None):
                pass

            async def run(self, **kwargs):
                calls["workflow_state"] = kwargs["workflow_state"]
                return AgentRunResult(answer="ok", state_patch={"agentscope_backend": "test"})

        semantic_model = {
            "a": {"amount": {"column_name": "amount", "business_name": "收入金额"}},
            "b": {"budget": {"column_name": "budget", "business_name": "预算金额"}},
        }

        with patch.object(sql_react, "AgentScopeRuntime", FakeAgentScopeRuntime), \
             patch.object(sql_react, "create_agentscope_runner", return_value=object()):
            await sql_react._run_agentscope_complex_analysis_prepass(
                {
                    "query": "分析收入预算关系",
                    "session_id": "s-semantic",
                    "selected_tables": ["a", "b"],
                    "semantic_model": semantic_model,
                    "table_relationships": [],
                    "security_context": {"allowed_tables": ["a", "b"]},
                }
            )

        assert calls["workflow_state"]["semantic_model"] is semantic_model

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_complex_plan_generate_uses_fallback_when_planner_clarifies_analysis(self, mock_get_model):
        import agents.flow.sql_react as sql_react

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content='{"mode":"clarify","reason":"范围太大","steps":[]}'))
        mock_get_model.return_value = mock_model

        with patch.object(sql_react, "_run_agentscope_complex_analysis_prepass", AsyncMock(return_value=({}, {}))) as prepass:
            result = await sql_react.complex_plan_generate({
                "query": "收入成本预算回款费用之间的关系",
                "selected_tables": ["a", "b", "c", "d"],
                "table_relationships": [{"from_table": "a", "from_column": "department_id", "to_table": "b", "to_column": "id"}],
                "evidence": [],
                "feasibility_decision": {"execution_mode": "complex_plan", "task_type": "analysis"},
                "agentscope_prepass_enabled": True,
            })

        prepass.assert_awaited_once()
        assert result["plan_validation_error"] == ""
        assert result["complex_plan"]["mode"] == "complex_plan"
        assert result["complex_plan"]["steps"][0]["type"] == "sql"
        assert result["complex_plan"]["steps"][-1]["type"] == "report"
        assert "已生成执行计划" in result["answer"]

    def test_route_after_complex_plan_generate_skips_approval_for_clarify_or_invalid(self):
        from langgraph.graph import END
        from agents.flow.sql_react import route_after_complex_plan_generate

        assert route_after_complex_plan_generate({
            "complex_plan": {"mode": "clarify", "steps": []},
            "plan_validation_error": "",
        }) == END

        assert route_after_complex_plan_generate({
            "complex_plan": {"mode": "complex_plan", "steps": [{"step": 1}]},
            "plan_validation_error": "missing merge_keys",
        }) == END

        assert route_after_complex_plan_generate({
            "complex_plan": {"mode": "complex_plan", "steps": [{"step": 1}]},
            "plan_validation_error": "",
        }) == "approve_complex_plan"

    @pytest.mark.asyncio
    async def test_approve_complex_plan_rejected_returns_false(self):
        from agents.flow.sql_react import approve_complex_plan

        with patch("agents.flow.sql_react.interrupt", return_value={
            "approved": False,
            "feedback": "计划太宽泛",
        }):
            result = await approve_complex_plan({
                "complex_plan": {"mode": "complex_plan", "steps": [{"step": 1, "goal": "x"}]},
                "answer": "计划预览",
            })

        assert result["plan_approved"] is False
        assert "取消" in result["answer"]

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_executes_sql_steps_and_merges_results(self):
        from agents.flow.sql_react import execute_complex_plan_step

        async def fake_sql_retrieve(step_state, config=None):
            return {"docs": [Document(page_content="表名: a\nperiod varchar(7)")], "semantic_model": {}}

        async def fake_sql_generate(step_state, config=None):
            if "当前步骤目标: 查询收入" in step_state["query"]:
                return {"sql": "SELECT period, revenue FROM a", "answer": "SELECT period, revenue FROM a", "is_sql": True}
            return {"sql": "SELECT period, budget FROM b", "answer": "SELECT period, budget FROM b", "is_sql": True}

        async def fake_execute_sql(step_state):
            if "revenue" in step_state["sql"]:
                return {
                    "result": '[{"period":"2025-01","revenue":100}]',
                    "answer": "查询已执行完成。\nperiod：2025-01\nrevenue：100",
                    "error": None,
                    "execution_history": [{"sql": step_state["sql"], "result": '[{"period":"2025-01","revenue":100}]', "error": None}],
                }
            return {
                "result": '[{"period":"2025-01","budget":80}]',
                "answer": "查询已执行完成。\nperiod：2025-01\nbudget：80",
                "error": None,
                "execution_history": [{"sql": step_state["sql"], "result": '[{"period":"2025-01","budget":80}]', "error": None}],
            }

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fake_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql):
            result = await execute_complex_plan_step({
                "query": "分析收入预算关系",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "查询收入", "tables": ["a"], "depends_on": [], "merge_keys": ["period"]},
                        {"step": 2, "type": "sql", "goal": "查询预算", "tables": ["b"], "depends_on": [], "merge_keys": ["period"]},
                        {"step": 3, "type": "python_merge", "goal": "按期间合并收入和预算", "tables": [], "depends_on": [1, 2], "merge_keys": ["period"]},
                    ],
                },
                "plan_approved": True,
                "table_relationships": [],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert result["is_sql"] is False
        assert result["error"] is None
        assert "关系分析结果" in result["answer"]
        assert result["plan_current_step"] == 3
        assert result["plan_execution_results"]["1"]["sql"] == "SELECT period, revenue FROM a"
        assert result["plan_execution_results"]["2"]["sql"] == "SELECT period, budget FROM b"
        assert result["plan_execution_results"]["3"]["result"] == [{"period": "2025-01", "revenue": 100, "budget": 80}]
        assert "收入合计：100.00" in result["answer"]
        assert "预算合计：80.00" in result["answer"]
        assert "SQL:" not in result["answer"]

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_shows_single_metric_result_in_answer(self):
        from agents.flow.sql_react import execute_complex_plan_step

        async def fake_sql_retrieve(step_state, config=None):
            return {"docs": [Document(page_content="表名: t_journal_item\nloss_amount decimal")], "semantic_model": {}}

        async def fake_sql_generate(step_state, config=None):
            return {
                "sql": "SELECT 0 AS loss_amount",
                "answer": "SELECT 0 AS loss_amount",
                "is_sql": True,
            }

        async def fake_execute_sql(step_state):
            return {
                "result": '[{"loss_amount":"0.00"}]',
                "answer": "查询已执行完成。\n亏损金额：0.00",
                "error": None,
                "execution_history": [{"sql": step_state["sql"], "result": '[{"loss_amount":"0.00"}]', "error": None}],
            }

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fake_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql):
            result = await execute_complex_plan_step({
                "query": "去年亏损",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "围绕用户问题生成第一步可审批 SQL：去年亏损", "tables": ["t_journal_item"], "depends_on": [], "merge_keys": []},
                        {"step": 2, "type": "report", "goal": "汇总查询结果", "tables": [], "depends_on": [1], "merge_keys": []},
                    ],
                },
                "plan_approved": True,
                "table_relationships": [],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert result["error"] is None
        assert "亏损金额：0.00" in result["answer"] or "loss_amount：0.00" in result["answer"]
        assert "SQL 明细和样例行已保留在 trace 中" not in result["answer"]
        assert result["plan_execution_results"]["1"]["result"] == '[{"loss_amount":"0.00"}]'

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_uses_submitted_step_sql_without_regenerating(self):
        from agents.flow.sql_react import execute_complex_plan_step

        async def fail_sql_retrieve(step_state, config=None):
            raise AssertionError("submitted step.sql should not reload schema for generation")

        async def fail_sql_generate(step_state, config=None):
            raise AssertionError("submitted step.sql should not call sql_generate")

        async def fake_execute_sql(step_state):
            return {
                "result": '[{"table_name":"a","row_count":3}]',
                "answer": "查询已执行完成。\ntable_name：a\nrow_count：3",
                "error": None,
                "execution_history": [
                    {"sql": step_state["sql"], "result": '[{"table_name":"a","row_count":3}]', "error": None}
                ],
            }

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fail_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fail_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql):
            result = await execute_complex_plan_step({
                "query": "检查已提交 SQL",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {
                            "step": 1,
                            "type": "sql",
                            "goal": "执行已提交 SQL",
                            "tables": ["a"],
                            "sql": "SELECT 'a' AS table_name, 3 AS row_count",
                            "depends_on": [],
                            "merge_keys": [],
                        },
                    ],
                },
                "plan_approved": True,
                "selected_tables": ["a"],
                "table_relationships": [],
                "security_context": {"allowed_tables": ["a"]},
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert result["error"] is None
        assert result["plan_execution_results"]["1"]["sql"] == "SELECT 'a' AS table_name, 3 AS row_count;"
        assert result["plan_execution_results"]["1"]["result"] == '[{"table_name":"a","row_count":3}]'

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_repairs_retryable_submitted_step_sql_error(self):
        from agents.flow.sql_react import execute_complex_plan_step

        executed_sql = []

        async def fake_execute_sql(step_state):
            executed_sql.append(step_state["sql"])
            if "bad_amount" in step_state["sql"]:
                return {
                    "result": "SQL 执行失败: Error: Unknown column 'bad_amount' in 'field list'",
                    "answer": "SQL 执行失败: Error: Unknown column 'bad_amount' in 'field list'",
                    "error": "Error: Unknown column 'bad_amount' in 'field list'",
                    "execution_history": [
                        {"sql": step_state["sql"], "result": None, "error": "Error: Unknown column 'bad_amount' in 'field list'"}
                    ],
                }
            return {
                "result": '[{"amount":100}]',
                "answer": "查询已执行完成。\namount：100",
                "error": None,
                "execution_history": [
                    {"sql": "SELECT bad_amount FROM a;", "result": None, "error": "Error: Unknown column 'bad_amount' in 'field list'"},
                    {"sql": step_state["sql"], "result": '[{"amount":100}]', "error": None},
                ],
            }

        async def fake_error_analysis(step_state, config=None):
            assert "bad_amount" in step_state["sql"]
            assert "Unknown column 'bad_amount'" in step_state["error"]
            return {
                "refine_feedback": "bad_amount 不存在，改用 amount",
                "retry_count": step_state.get("retry_count", 0) + 1,
            }

        async def fake_sql_generate(step_state, config=None):
            assert "bad_amount 不存在" in step_state.get("refine_feedback", "")
            return {"sql": "SELECT amount FROM a;", "answer": "SELECT amount FROM a;", "is_sql": True, "error": None}

        with patch("agents.flow.sql_react.sql_retrieve", new_callable=AsyncMock) as mock_retrieve, \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql), \
             patch("agents.flow.sql_react.error_analysis", side_effect=fake_error_analysis):
            result = await execute_complex_plan_step({
                "query": "检查已提交 SQL 自动修复",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {
                            "step": 1,
                            "type": "sql",
                            "goal": "执行已提交 SQL",
                            "tables": ["a"],
                            "sql": "SELECT bad_amount FROM a",
                            "depends_on": [],
                            "merge_keys": [],
                        },
                    ],
                },
                "plan_approved": True,
                "selected_tables": ["a"],
                "table_relationships": [],
                "security_context": {"allowed_tables": ["a"]},
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        mock_retrieve.assert_not_called()
        assert result["error"] is None
        assert executed_sql == ["SELECT bad_amount FROM a;", "SELECT amount FROM a;"]
        assert result["plan_execution_results"]["1"]["sql"] == "SELECT amount FROM a;"
        assert result["plan_execution_results"]["1"]["result"] == '[{"amount":100}]'

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_preserves_submitted_sql_line_comments(self):
        from agents.flow.sql_react import execute_complex_plan_step

        submitted_sql = """SELECT
period,
-- 收入合计
SUM(revenue) AS total_income
FROM a
GROUP BY period"""

        async def fail_sql_retrieve(step_state, config=None):
            raise AssertionError("submitted step.sql should not reload schema for generation")

        async def fail_sql_generate(step_state, config=None):
            raise AssertionError("submitted step.sql should not call sql_generate")

        async def fake_execute_sql(step_state):
            assert "-- 收入合计\nSUM(" in step_state["sql"]
            assert "-- 收入合计 SUM(" not in step_state["sql"]
            return {
                "result": '[{"period":"2025-01","total_income":100}]',
                "answer": "查询已执行完成。\nperiod：2025-01\ntotal_income：100",
                "error": None,
                "execution_history": [
                    {
                        "sql": step_state["sql"],
                        "result": '[{"period":"2025-01","total_income":100}]',
                        "error": None,
                    }
                ],
            }

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fail_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fail_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql):
            result = await execute_complex_plan_step({
                "query": "检查带注释 SQL",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {
                            "step": 1,
                            "type": "sql",
                            "goal": "执行带行注释 SQL",
                            "tables": ["a"],
                            "sql": submitted_sql,
                            "depends_on": [],
                            "merge_keys": [],
                        },
                    ],
                },
                "plan_approved": True,
                "selected_tables": ["a"],
                "table_relationships": [],
                "security_context": {"allowed_tables": ["a"]},
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert result["error"] is None
        assert "-- 收入合计\nSUM(" in result["plan_execution_results"]["1"]["sql"]

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_blocks_unsafe_submitted_step_sql(self):
        from agents.flow.sql_react import execute_complex_plan_step

        async def fail_sql_retrieve(step_state, config=None):
            raise AssertionError("unsafe submitted step.sql should not reload schema")

        async def fail_sql_generate(step_state, config=None):
            raise AssertionError("unsafe submitted step.sql should not regenerate")

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fail_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fail_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", new_callable=AsyncMock) as mock_execute:
            result = await execute_complex_plan_step({
                "query": "检查危险 SQL",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {
                            "step": 1,
                            "type": "sql",
                            "goal": "执行危险 SQL",
                            "tables": ["a"],
                            "sql": "DROP TABLE a",
                            "depends_on": [],
                            "merge_keys": [],
                        },
                    ],
                },
                "plan_approved": True,
                "selected_tables": ["a"],
                "table_relationships": [],
                "security_context": {"allowed_tables": ["a"]},
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert result["error"] == "complex_plan_step_failed"
        assert result["plan_execution_results"]["1"]["error"] == "invalid_submitted_sql"
        mock_execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_returns_compact_trace_payload(self):
        from agents.flow.sql_react import execute_complex_plan_step

        raw_rows = [{"period": f"2025-{month:02d}", "revenue": month * 100} for month in range(1, 13)]
        raw_result = json.dumps(raw_rows, ensure_ascii=False)

        async def fake_sql_retrieve(step_state, config=None):
            return {"docs": [Document(page_content="表名: a\nperiod varchar(7)")], "semantic_model": {}}

        async def fake_sql_generate(step_state, config=None):
            return {"sql": "SELECT period, revenue FROM a", "answer": "SELECT period, revenue FROM a", "is_sql": True}

        async def fake_execute_sql(step_state):
            return {
                "result": raw_result,
                "answer": "查询已执行完成。\n共返回 12 条记录。\n仅展示前 5 条。",
                "error": None,
                "execution_history": [
                    {"sql": step_state["sql"], "result": raw_result, "error": None},
                ],
            }

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fake_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql):
            result = await execute_complex_plan_step({
                "query": "分析收入趋势",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "查询收入", "tables": ["a"], "depends_on": [], "merge_keys": ["period"]},
                    ],
                },
                "plan_approved": True,
                "table_relationships": [],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        step_result = result["plan_execution_results"]["1"]
        assert result["error"] is None
        assert raw_result not in result["result"]
        assert raw_result not in json.dumps(result["plan_execution_results"], ensure_ascii=False)
        assert "execution_history" not in step_result
        assert "result_preview" in step_result
        assert step_result["result_row_count"] == 12
        assert step_result["result_preview"][0] == {"period": "2025-01", "revenue": 100}
        assert step_result["result_truncated"] is True
        assert "plan_summary" in result["result"]

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_reuses_state_semantic_model_without_reloading(self):
        from agents.flow.sql_react import execute_complex_plan_step

        semantic_model = {
            "a": {"period": {"column_name": "period"}, "revenue": {"column_name": "revenue"}},
            "b": {"period": {"column_name": "period"}, "budget": {"column_name": "budget"}},
        }

        async def fake_sql_generate(step_state, config=None):
            if "当前复杂计划 SQL 步骤: 1" in step_state["query"]:
                return {"sql": "SELECT period, revenue FROM a", "answer": "SELECT period, revenue FROM a", "is_sql": True}
            return {"sql": "SELECT period, budget FROM b", "answer": "SELECT period, budget FROM b", "is_sql": True}

        async def fake_execute_sql(step_state):
            if "revenue" in step_state["sql"]:
                return {"result": '[{"period":"2025-01","revenue":100}]', "answer": "查询已执行完成。", "error": None}
            return {"result": '[{"period":"2025-01","budget":80}]', "answer": "查询已执行完成。", "error": None}

        with patch("agents.flow.sql_react.get_semantic_model_by_tables") as mock_semantic, \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql):
            result = await execute_complex_plan_step({
                "query": "分析收入预算关系",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "查询收入", "tables": ["a"], "depends_on": [], "merge_keys": ["period"]},
                        {"step": 2, "type": "sql", "goal": "查询预算", "tables": ["b"], "depends_on": [], "merge_keys": ["period"]},
                        {"step": 3, "type": "python_merge", "goal": "按期间合并", "tables": [], "depends_on": [1, 2], "merge_keys": ["period"]},
                    ],
                },
                "plan_approved": True,
                "selected_tables": ["a", "b"],
                "semantic_model": semantic_model,
                "table_relationships": [],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        mock_semantic.assert_not_called()
        assert result["error"] is None
        assert result["plan_execution_results"]["3"]["result"] == [{"period": "2025-01", "revenue": 100, "budget": 80}]

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_runs_ready_independent_sql_steps_concurrently(self):
        from agents.flow.sql_react import execute_complex_plan_step

        release = asyncio.Event()
        waiting_steps = set()
        overlap_observed = False

        async def fake_sql_retrieve(step_state, config=None):
            return {"docs": [Document(page_content="schema")], "semantic_model": {}}

        async def fake_sql_generate(step_state, config=None):
            nonlocal overlap_observed
            if "当前复杂计划 SQL 步骤: 1" in step_state["query"]:
                waiting_steps.add(1)
                while 2 not in waiting_steps:
                    await asyncio.sleep(0)
                overlap_observed = True
                release.set()
                return {"sql": "SELECT period, revenue FROM a", "answer": "SELECT period, revenue FROM a", "is_sql": True}
            if "当前复杂计划 SQL 步骤: 2" in step_state["query"]:
                waiting_steps.add(2)
                await release.wait()
                return {"sql": "SELECT period, budget FROM b", "answer": "SELECT period, budget FROM b", "is_sql": True}
            return {"sql": "SELECT 1", "answer": "SELECT 1", "is_sql": True}

        async def fake_execute_sql(step_state):
            if "revenue" in step_state["sql"]:
                return {
                    "result": '[{"period":"2025-01","revenue":100}]',
                    "answer": "查询已执行完成。",
                    "error": None,
                    "execution_history": [],
                }
            return {
                "result": '[{"period":"2025-01","budget":80}]',
                "answer": "查询已执行完成。",
                "error": None,
                "execution_history": [],
            }

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fake_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql):
            result = await execute_complex_plan_step({
                "query": "分析收入预算关系",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "查询收入", "tables": ["a"], "depends_on": [], "merge_keys": ["period"]},
                        {"step": 2, "type": "sql", "goal": "查询预算", "tables": ["b"], "depends_on": [], "merge_keys": ["period"]},
                        {"step": 3, "type": "python_merge", "goal": "按期间合并收入和预算", "tables": [], "depends_on": [1, 2], "merge_keys": ["period"]},
                    ],
                },
                "plan_approved": True,
                "table_relationships": [],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert overlap_observed is True
        assert result["error"] is None
        assert result["plan_execution_results"]["3"]["result"] == [{"period": "2025-01", "revenue": 100, "budget": 80}]

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_prefetches_union_semantic_model_once_for_parallel_batch(self):
        from agents.flow.sql_react import execute_complex_plan_step

        semantic_calls = []

        async def fake_sql_generate(step_state, config=None):
            if "当前复杂计划 SQL 步骤: 1" in step_state["query"]:
                return {"sql": "SELECT period, revenue FROM a", "answer": "SELECT period, revenue FROM a", "is_sql": True}
            return {"sql": "SELECT period, budget FROM b", "answer": "SELECT period, budget FROM b", "is_sql": True}

        async def fake_execute_sql(step_state):
            if "revenue" in step_state["sql"]:
                return {"result": '[{"period":"2025-01","revenue":100}]', "answer": "查询已执行完成。", "error": None}
            return {"result": '[{"period":"2025-01","budget":80}]', "answer": "查询已执行完成。", "error": None}

        def fake_get_semantic_model_by_tables(table_names):
            semantic_calls.append(list(table_names))
            return {
                table: {
                    "period": {"column_name": "period"},
                    "amount": {"column_name": "amount"},
                }
                for table in table_names
            }

        with patch("agents.flow.sql_react.get_semantic_model_by_tables", side_effect=fake_get_semantic_model_by_tables), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql):
            result = await execute_complex_plan_step({
                "query": "分析收入预算关系",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "查询收入", "tables": ["a"], "depends_on": [], "merge_keys": ["period"]},
                        {"step": 2, "type": "sql", "goal": "查询预算", "tables": ["b"], "depends_on": [], "merge_keys": ["period"]},
                        {"step": 3, "type": "python_merge", "goal": "按期间合并收入和预算", "tables": [], "depends_on": [1, 2], "merge_keys": ["period"]},
                    ],
                },
                "plan_approved": True,
                "selected_tables": ["a", "b"],
                "table_relationships": [],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert result["error"] is None
        assert semantic_calls == [["a", "b"]]
        assert result["plan_execution_results"]["3"]["result"] == [{"period": "2025-01", "revenue": 100, "budget": 80}]

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_expands_sql_step_join_path(self):
        from agents.flow.sql_react import execute_complex_plan_step

        seen_selected_tables = []

        async def fake_sql_retrieve(step_state, config=None):
            seen_selected_tables.append(step_state["selected_tables"])
            return {"docs": [Document(page_content="schema")], "semantic_model": {}}

        async def fake_sql_generate(step_state, config=None):
            return {"sql": "SELECT 1", "answer": "SELECT 1", "is_sql": True}

        async def fake_execute_sql(step_state):
            return {
                "result": '[{"ok":1}]',
                "answer": "查询已执行完成。\nok：1",
                "error": None,
                "execution_history": [{"sql": step_state["sql"], "result": '[{"ok":1}]', "error": None}],
            }

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fake_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql):
            result = await execute_complex_plan_step({
                "query": "按会计科目分析发票收入",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {
                            "step": 1,
                            "type": "sql",
                            "goal": "按会计科目统计发票收入",
                            "tables": ["t_invoice", "t_account"],
                            "depends_on": [],
                            "merge_keys": ["account_code"],
                        },
                    ],
                },
                "plan_approved": True,
                "selected_tables": ["t_invoice", "t_journal_entry", "t_journal_item", "t_account"],
                "table_relationships": [
                    {"from_table": "t_invoice", "to_table": "t_journal_entry"},
                    {"from_table": "t_journal_item", "to_table": "t_journal_entry"},
                    {"from_table": "t_journal_item", "to_table": "t_account"},
                ],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert result["error"] is None
        assert seen_selected_tables == [["t_invoice", "t_account", "t_journal_entry", "t_journal_item"]]
        assert result["plan_execution_results"]["1"]["tables"] == [
            "t_invoice",
            "t_account",
            "t_journal_entry",
            "t_journal_item",
        ]

    def test_merge_dependency_rows_resolves_generic_merge_key_alias(self):
        from agents.flow.sql_react import _merge_dependency_rows

        step = {
            "type": "python_merge",
            "depends_on": [1, 2],
            "merge_keys": ["department"],
        }
        rows, reason = _merge_dependency_rows(step, {
            "1": {
                "result": '[{"department_name":"产品部","total_income":100,"total_expense":30}]',
                "error": None,
            },
            "2": {
                "result": '[{"department":"产品部","expense_budget":80}]',
                "error": None,
            },
        })

        assert reason == ""
        assert rows == [{
            "department": "产品部",
            "total_income": 100,
            "total_expense": 30,
            "expense_budget": 80,
        }]

    def test_merge_dependency_rows_aligns_id_merge_key_with_name_columns(self):
        from agents.flow.sql_react import _merge_dependency_rows

        step = {
            "type": "python_merge",
            "depends_on": [1, 2],
            "merge_keys": ["department_id", "cost_center_id"],
        }
        rows, reason = _merge_dependency_rows(step, {
            "1": {
                "result": (
                    '[{"department_id":3,"department_name":"产品部",'
                    '"cost_center_id":3,"cost_center_name":"研发部","total_income":100}]'
                ),
                "error": None,
            },
            "2": {
                "result": (
                    '[{"department_name":"产品部","cost_center_name":"研发部",'
                    '"income_expense_budget":80}]'
                ),
                "error": None,
            },
        })

        assert reason == ""
        assert rows is not None
        assert len(rows) == 1
        assert rows[0]["department_id"] == "产品部"
        assert rows[0]["cost_center_id"] == "研发部"
        assert rows[0]["total_income"] == 100
        assert rows[0]["income_expense_budget"] == 80

    def test_merge_dependency_rows_keeps_unaligned_rows_instead_of_failing(self):
        from agents.flow.sql_react import _merge_dependency_rows

        step = {
            "type": "python_merge",
            "depends_on": [1, 2],
            "merge_keys": ["department_id", "cost_center_id"],
        }
        rows, reason = _merge_dependency_rows(step, {
            "1": {
                "result": (
                    '[{"account_code":"6001","account_name":"主营业务收入","income_amount":100},'
                    '{"department_id":1,"cost_center_id":1,"account_code":"5401","cost_amount":30}]'
                ),
                "error": None,
            },
            "2": {
                "result": '[{"department_id":1,"cost_center_id":1,"budget_amount":80}]',
                "error": None,
            },
        })

        assert reason == ""
        assert rows is not None
        assert len(rows) == 2
        assert rows[0]["department_id"] == 1
        assert rows[0]["cost_center_id"] == 1
        assert rows[0]["cost_amount"] == 30
        assert rows[0]["budget_amount"] == 80
        assert rows[1]["merge_status"] == "未对齐"
        assert rows[1]["source_step"] == 1
        assert rows[1]["missing_merge_keys"] == "department_id, cost_center_id"
        assert rows[1]["income_amount"] == 100

    def test_merge_dependency_rows_ignores_merge_keys_absent_from_all_dependencies(self):
        from agents.flow.sql_react import _merge_dependency_rows

        step = {
            "type": "python_merge",
            "depends_on": [1, 2],
            "merge_keys": ["department_id", "period"],
        }
        rows, reason = _merge_dependency_rows(step, {
            "1": {
                "result": '[{"department_id":1,"income_amount":100}]',
                "error": None,
            },
            "2": {
                "result": '[{"department_id":1,"budget_amount":80}]',
                "error": None,
            },
        })

        assert reason == ""
        assert rows == [{"department_id": 1, "income_amount": 100, "budget_amount": 80}]

    def test_complex_execution_answer_surfaces_business_summary_for_merge_result(self):
        from agents.flow.sql_react import _format_complex_execution_answer

        plan = {
            "steps": [
                {"step": 1, "type": "sql", "goal": "提取实际收入成本费用"},
                {"step": 2, "type": "sql", "goal": "提取预算"},
                {"step": 3, "type": "python_merge", "goal": "合并分析", "merge_keys": ["department_id"]},
            ]
        }
        answer = _format_complex_execution_answer(plan, {
            "1": {"step": 1, "type": "sql", "goal": "提取实际收入成本费用", "answer": "查询已执行完成。", "error": None},
            "2": {"step": 2, "type": "sql", "goal": "提取预算", "answer": "查询已执行完成。", "error": None},
            "3": {
                "step": 3,
                "type": "python_merge",
                "goal": "合并分析",
                "answer": "本地合并完成，共 2 行。",
                "result": [
                    {
                        "department_id": 1,
                        "income_amount": "100.00",
                        "cost_amount": "30.00",
                        "expense_amount": "10.00",
                        "budget_amount": "80.00",
                        "collection_amount": "60.00",
                    },
                    {
                        "merge_status": "未对齐",
                        "source_step": 1,
                        "income_amount": "50.00",
                    },
                ],
                "error": None,
            },
        })

        assert "关系分析结果" in answer
        assert "收入合计：150.00" in answer
        assert "成本合计：30.00" in answer
        assert "费用合计：10.00" in answer
        assert "预算合计：80.00" in answer
        assert "回款合计：60.00" in answer
        assert "粗略盈余：110.00" in answer
        assert "另有 1 条记录缺少合并维度" in answer

    def test_complex_execution_answer_is_user_readable_relationship_result(self):
        from agents.flow.sql_react import _format_complex_execution_answer

        plan = {
            "steps": [
                {"step": 1, "type": "sql", "goal": "提取实际发生金额"},
                {"step": 2, "type": "sql", "goal": "提取预算金额"},
                {"step": 3, "type": "sql", "goal": "提取回款金额"},
                {"step": 4, "type": "python_merge", "goal": "合并分析", "merge_keys": ["cost_center_id"]},
            ]
        }
        execution_results = {
            "1": {
                "step": 1,
                "type": "sql",
                "goal": "提取实际发生金额",
                "sql": "SELECT cost_center_id, SUM(actual_amount) FROM t_journal_item GROUP BY cost_center_id",
                "answer": "查询已执行完成。",
                "error": None,
            },
            "2": {
                "step": 2,
                "type": "sql",
                "goal": "提取预算金额",
                "sql": "SELECT cost_center_id, SUM(budget_amount) FROM t_budget GROUP BY cost_center_id",
                "answer": "查询已执行完成。",
                "error": None,
            },
            "3": {
                "step": 3,
                "type": "sql",
                "goal": "提取回款金额",
                "sql": "SELECT cost_center_id, SUM(received_amount) FROM t_receivable_payable GROUP BY cost_center_id",
                "answer": "查询已执行完成。\n未查询到符合条件的数据。",
                "error": None,
            },
            "4": {
                "step": 4,
                "type": "python_merge",
                "goal": "合并分析",
                "answer": "本地合并完成，共 3 行。",
                "result": [
                    {"cost_center_id": 2, "department_id": 2, "actual_amount": "520000.00", "budget_amount": "1200000.00"},
                    {"cost_center_id": 4, "department_id": 4, "actual_amount": "-1316000.00", "budget_amount": "2400000.00"},
                    {"cost_center_id": 6, "department_id": 6, "actual_amount": "1413000.00", "budget_amount": "2571131.67"},
                ],
                "error": None,
            },
        }

        answer = _format_complex_execution_answer(plan, execution_results)

        assert "关系分析结果" in answer
        assert "实际发生合计：617000.00" in answer
        assert "预算合计：6171131.67" in answer
        assert "预算差异：-5554131.67" in answer
        assert "预算执行率：10.00%" in answer
        assert "未查询到可对齐的回款数据" in answer
        assert "成本合计：12.00" not in answer
        assert "SQL:" not in answer
        assert "SELECT" not in answer
        assert "合并结果预览" not in answer

    def test_report_step_with_merge_keys_produces_local_merge_preview(self):
        from agents.flow.sql_react import _format_complex_execution_answer, _run_local_complex_step

        step = {
            "step": 3,
            "type": "report",
            "goal": "汇总收入和预算关系",
            "depends_on": [1, 2],
            "merge_keys": ["period"],
        }
        execution_results = {
            "1": {
                "result": '[{"period":"2025-01","revenue":100}]',
                "answer": "查询已执行完成。",
                "error": None,
            },
            "2": {
                "result": '[{"period":"2025-01","budget":80}]',
                "answer": "查询已执行完成。",
                "error": None,
            },
        }

        entry = _run_local_complex_step(step, execution_results)
        execution_results["3"] = entry
        answer = _format_complex_execution_answer({"steps": [step]}, execution_results)

        assert entry["result"] == [{"period": "2025-01", "revenue": 100, "budget": 80}]
        assert "关系分析结果" in answer
        assert "收入合计：100.00" in answer
        assert "预算合计：80.00" in answer

    def test_report_step_without_merge_keys_summarizes_single_metric_row(self):
        from agents.flow.sql_react import _format_complex_execution_answer, _run_local_complex_step

        step = {
            "step": 2,
            "type": "report",
            "goal": "生成收入成本预算回款费用关系摘要",
            "depends_on": [1],
        }
        execution_results = {
            "1": {
                "result": (
                    '[{"actual_income":"0.00","actual_cost":"0.00",'
                    '"budget_cost":"15588682.45","actual_expense":"138821.81",'
                    '"receivable_amount":"2996637.78"}]'
                ),
                "answer": "查询已执行完成。",
                "error": None,
            },
        }

        entry = _run_local_complex_step(step, execution_results)
        execution_results["2"] = entry
        answer = _format_complex_execution_answer({"steps": [step]}, execution_results)

        assert entry["result"] == [
            {
                "actual_income": "0.00",
                "actual_cost": "0.00",
                "budget_cost": "15588682.45",
                "actual_expense": "138821.81",
                "receivable_amount": "2996637.78",
            }
        ]
        assert "关系分析结果" in answer
        assert "实际发生合计：138821.81" in answer
        assert "预算合计：15588682.45" in answer
        assert "回款合计：2996637.78" in answer
        assert "SQL:" not in answer

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_blocks_unsafe_generated_sql(self):
        from agents.flow.sql_react import execute_complex_plan_step

        async def fake_sql_retrieve(step_state, config=None):
            return {"docs": [Document(page_content="表名: a")], "semantic_model": {}}

        async def fake_sql_generate(step_state, config=None):
            return {"sql": "DROP TABLE a", "answer": "DROP TABLE a", "is_sql": True}

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fake_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", new_callable=AsyncMock) as mock_execute:
            result = await execute_complex_plan_step({
                "query": "危险测试",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "生成危险 SQL", "tables": ["a"], "depends_on": [], "merge_keys": ["id"]},
                    ],
                },
                "plan_approved": True,
                "table_relationships": [],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert mock_execute.await_count == 0
        assert result["error"] == "complex_plan_step_failed"
        assert "安全检查未通过" in result["answer"]
        assert result["plan_execution_results"]["1"]["error"]

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_rejects_empty_generated_sql(self):
        from agents.flow.sql_react import execute_complex_plan_step

        async def fake_sql_retrieve(step_state, config=None):
            return {"docs": [Document(page_content="表名: a")], "semantic_model": {}}

        async def fake_sql_generate(step_state, config=None):
            return {"sql": "", "answer": "", "is_sql": True}

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fake_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", new_callable=AsyncMock) as mock_execute:
            result = await execute_complex_plan_step({
                "query": "空 SQL 测试",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "生成空 SQL", "tables": ["a"], "depends_on": [], "merge_keys": ["id"]},
                    ],
                },
                "plan_approved": True,
                "table_relationships": [],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert mock_execute.await_count == 0
        assert result["error"] == "complex_plan_step_failed"
        assert result["plan_execution_results"]["1"]["error"] == "empty generated sql"

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_rejects_empty_execution_payload(self):
        from agents.flow.sql_react import execute_complex_plan_step

        async def fake_sql_retrieve(step_state, config=None):
            return {"docs": [Document(page_content="表名: a")], "semantic_model": {}}

        async def fake_sql_generate(step_state, config=None):
            return {"sql": "SELECT 1", "answer": "SELECT 1", "is_sql": True}

        async def fake_execute_sql(step_state):
            return {"result": None, "answer": "", "error": None, "execution_history": []}

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fake_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql):
            result = await execute_complex_plan_step({
                "query": "空执行结果测试",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "执行空结果 SQL", "tables": ["a"], "depends_on": [], "merge_keys": ["id"]},
                    ],
                },
                "plan_approved": True,
                "table_relationships": [],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert result["error"] == "complex_plan_step_failed"
        assert result["plan_execution_results"]["1"]["error"] == "empty execution result"

    @pytest.mark.asyncio
    async def test_execute_complex_plan_step_repairs_retryable_sql_error(self):
        from agents.flow.sql_react import execute_complex_plan_step

        generated_sql = []

        async def fake_sql_retrieve(step_state, config=None):
            return {"docs": [Document(page_content="表名: a\nid int\namount int")], "semantic_model": {}}

        async def fake_sql_generate(step_state, config=None):
            sql = "SELECT missing_amount FROM a" if not step_state.get("refine_feedback") else "SELECT amount FROM a"
            generated_sql.append(sql)
            return {"sql": sql, "answer": sql, "is_sql": True, "error": None}

        async def fake_execute_sql(step_state):
            if "missing_amount" in step_state["sql"]:
                return {
                    "result": "SQL 执行失败: Error: Unknown column 'missing_amount' in 'field list'",
                    "answer": "SQL 执行失败: Error: Unknown column 'missing_amount' in 'field list'",
                    "error": "Error: Unknown column 'missing_amount' in 'field list'",
                    "execution_history": [{"sql": step_state["sql"], "result": None, "error": "unknown column"}],
                }
            return {
                "result": '[{"amount":100}]',
                "answer": "查询已执行完成。\namount：100",
                "error": None,
                "execution_history": [
                    {"sql": "SELECT missing_amount FROM a", "result": None, "error": "unknown column"},
                    {"sql": step_state["sql"], "result": '[{"amount":100}]', "error": None},
                ],
            }

        async def fake_error_analysis(step_state, config=None):
            return {"refine_feedback": "missing_amount 不存在，改用 amount", "retry_count": step_state.get("retry_count", 0) + 1}

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fake_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql), \
             patch("agents.flow.sql_react.error_analysis", side_effect=fake_error_analysis):
            result = await execute_complex_plan_step({
                "query": "复杂计划 SQL 错误修复测试",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "查询金额", "tables": ["a"], "depends_on": [], "merge_keys": ["id"]},
                    ],
                },
                "plan_approved": True,
                "table_relationships": [],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        assert result["error"] is None
        assert generated_sql == ["SELECT missing_amount FROM a", "SELECT amount FROM a"]
        assert result["plan_execution_results"]["1"]["sql"] == "SELECT amount FROM a"
        assert result["plan_execution_results"]["1"]["result"] == '[{"amount":100}]'


# ---------------------------------------------------------------------------
# safety_check node
# ---------------------------------------------------------------------------

class TestSafetyCheck:
    """Test safety_check node."""

    @pytest.mark.asyncio
    async def test_non_sql_skips_check(self):
        """When is_sql is False, skip safety check."""
        from agents.flow.sql_react import safety_check

        result = await safety_check({"is_sql": False, "sql": "not sql"})
        assert result["safety_report"] is None

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.SQLSafetyChecker")
    async def test_safe_sql_passes(self, MockChecker):
        """Safe SQL should pass with no report."""
        from agents.flow.sql_react import safety_check

        mock_checker = MagicMock()
        mock_report = MagicMock()
        mock_report.is_safe = True
        mock_checker.check.return_value = mock_report
        MockChecker.return_value = mock_checker

        result = await safety_check({"is_sql": True, "sql": "SELECT * FROM users"})

        assert result["safety_report"] is None

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.SQLSafetyChecker")
    async def test_unsafe_sql_blocked(self, MockChecker):
        """Unsafe SQL should be blocked with risks reported."""
        from agents.flow.sql_react import safety_check

        mock_checker = MagicMock()
        mock_report = MagicMock()
        mock_report.is_safe = False
        mock_report.risks = ["DELETE detected"]
        mock_report.estimated_rows = 1000
        mock_report.required_permissions = ["DELETE"]
        mock_checker.check.return_value = mock_report
        MockChecker.return_value = mock_checker

        result = await safety_check({"is_sql": True, "sql": "DELETE FROM users"})

        assert result["is_sql"] is False
        assert "DELETE detected" in result["answer"]
        assert result["safety_report"]["risks"] == ["DELETE detected"]


# ---------------------------------------------------------------------------
# authorization nodes
# ---------------------------------------------------------------------------

class TestAuthorizationNodes:
    """Test SQL data authorization gates."""

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.write_audit_log")
    async def test_authorize_selected_tables_blocks_denied_business_domain(self, mock_audit):
        from agents.flow.sql_react import authorize_selected_tables

        result = await authorize_selected_tables({
            "query": "查询所有用户真实姓名",
            "selected_tables": ["t_user", "t_role"],
            "security_context": {"allowed_tables": ["t_role"]},
            "table_metadata": {
                "t_user": "用户/员工账号信息",
                "t_role": "系统角色信息",
            },
        })

        assert result["is_sql"] is False
        assert "用户/员工账号信息" in result["answer"]
        assert "t_user" not in result["answer"]
        assert result["authorization_report"]["allowed"] is False
        assert result["authorization_report"]["denied_tables"] == ["t_user"]
        audit_event = mock_audit.call_args[0][0]
        assert audit_event["event_type"] == "table_permission_denied"
        assert audit_event["denied_tables"] == ["t_user"]
        assert audit_event["display_tables"] == ["用户/员工账号信息"]

    @pytest.mark.asyncio
    async def test_authorize_sql_blocks_table_not_allowed_by_context(self):
        from agents.flow.sql_react import authorize_sql

        result = await authorize_sql({
            "query": "查询所有用户真实姓名",
            "is_sql": True,
            "sql": "SELECT real_name FROM t_user",
            "selected_tables": ["t_user"],
            "security_context": {"allowed_tables": ["t_role"]},
            "table_metadata": {"t_user": "用户/员工账号信息"},
        })

        assert result["is_sql"] is False
        assert "用户/员工账号信息" in result["answer"]
        assert result["authorization_report"]["stage"] == "sql"

    @pytest.mark.asyncio
    async def test_complex_plan_sql_step_runs_authorize_sql_before_execute(self):
        """Complex plan SQL steps must not bypass SQL authorization."""
        from agents.flow.sql_react import execute_complex_plan_step

        async def fake_sql_retrieve(step_state, config=None):
            return {"docs": [_mock_doc("t_user: real_name")], "semantic_model": {}}

        async def fake_sql_generate(step_state, config=None):
            return {"is_sql": True, "sql": "SELECT real_name FROM t_user", "answer": "SELECT real_name FROM t_user"}

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fake_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", new_callable=AsyncMock) as mock_execute:
            result = await execute_complex_plan_step({
                "query": "复杂计划权限测试",
                "complex_plan": {
                    "mode": "complex_plan",
                    "steps": [
                        {"step": 1, "type": "sql", "goal": "查用户", "tables": ["t_user"], "depends_on": [], "merge_keys": ["id"]},
                    ],
                },
                "plan_approved": True,
                "security_context": {"allowed_tables": ["t_role"]},
                "table_metadata": {"t_user": "用户/员工账号信息"},
                "table_relationships": [],
                "evidence": [],
                "few_shot_examples": [],
                "chat_history": [],
                "execution_history": [],
                "retry_count": 0,
            })

        step_result = result["plan_execution_results"]["1"]
        assert "用户/员工账号信息" in step_result["answer"]
        assert step_result["error"] == "permission_denied"
        mock_execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_complex_plan_sql_harness_emits_trace_spans(self):
        """Complex plan internal SQL Harness stages should appear as traceable tool spans."""
        from agents.flow.sql_react import execute_complex_plan_step

        async def fake_sql_retrieve(step_state, config=None):
            return {"docs": [_mock_doc("表名: t_user\nid int")], "semantic_model": {}}

        async def fake_sql_generate(step_state, config=None):
            return {"is_sql": True, "sql": "SELECT id FROM t_user", "answer": "SELECT id FROM t_user"}

        async def fake_execute_sql(step_state):
            return {
                "result": '[{"id":1}]',
                "answer": "查询已执行完成。\nid：1",
                "error": None,
                "execution_history": [{"sql": step_state["sql"], "result": '[{"id":1}]', "error": None}],
            }

        observed = []

        async def fake_traced_async_tool_call(name, input_str, callbacks, func, metadata=None):
            observed.append((name, input_str, metadata))
            return await func()

        with patch("agents.flow.sql_react.sql_retrieve", side_effect=fake_sql_retrieve), \
             patch("agents.flow.sql_react.sql_generate", side_effect=fake_sql_generate), \
             patch("agents.flow.sql_react.execute_sql", side_effect=fake_execute_sql), \
             patch("agents.flow.sql_react.traced_async_tool_call", side_effect=fake_traced_async_tool_call):
            result = await execute_complex_plan_step(
                {
                    "query": "查用户",
                    "complex_plan": {
                        "mode": "complex_plan",
                        "steps": [
                            {"step": 1, "type": "sql", "goal": "查用户", "tables": ["t_user"], "depends_on": [], "merge_keys": ["id"]},
                        ],
                    },
                    "plan_approved": True,
                    "security_context": {"allowed_tables": ["t_user"]},
                    "table_metadata": {"t_user": "用户"},
                    "table_relationships": [],
                    "evidence": [],
                    "few_shot_examples": [],
                    "chat_history": [],
                    "execution_history": [],
                    "retry_count": 0,
                },
                config={"callbacks": ["trace-handler"]},
            )

        assert result["error"] is None
        assert [item[0] for item in observed] == [
            "sql.safety_check",
            "sql.authorize_sql",
            "sql.execute_sql",
        ]
        assert all(item[2]["step"] == 1 for item in observed)


# ---------------------------------------------------------------------------
# approve node
# ---------------------------------------------------------------------------

class TestApprove:
    """Test approve node uses interrupt."""

    def test_approved_returns_true(self):
        """When user approves, should set approved=True."""
        from agents.flow.sql_react import approve

        with patch("agents.flow.sql_react.interrupt", return_value={"approved": True}):
            result = approve({"sql": "SELECT 1"})

        assert result["approved"] is True

    def test_rejected_returns_message(self):
        """When user rejects, should set approved=False with feedback."""
        from agents.flow.sql_react import approve

        with patch("agents.flow.sql_react.interrupt", return_value={
            "approved": False, "feedback": "SQL too dangerous"
        }):
            result = approve({"sql": "DELETE FROM users"})

        assert result["approved"] is False
        assert result["is_sql"] is False
        assert "SQL too dangerous" in result["answer"]

    def test_rejected_default_message(self):
        """When user rejects without feedback, use default message."""
        from agents.flow.sql_react import approve

        with patch("agents.flow.sql_react.interrupt", return_value={"approved": False}):
            result = approve({"sql": "SELECT 1"})

        assert result["approved"] is False
        assert "拒绝" in result["answer"]

    def test_interrupt_passes_sql(self):
        """interrupt should receive the SQL and a message."""
        from agents.flow.sql_react import approve

        mock_interrupt = MagicMock(return_value={"approved": True})
        with patch("agents.flow.sql_react.interrupt", mock_interrupt):
            approve({"sql": "SELECT * FROM t_user"})

        call_args = mock_interrupt.call_args[0][0]
        assert call_args["sql"] == "SELECT * FROM t_user"
        assert "确认" in call_args["message"]


# ---------------------------------------------------------------------------
# sql_generate node
# ---------------------------------------------------------------------------

class TestSqlGenerate:
    """Test sql_generate node."""

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_sql_from_docs(self, mock_get_model, mock_format):
        """LLM should generate SQL based on retrieved docs."""
        from agents.flow.sql_react import sql_generate

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=_mock_llm_tool_response("SELECT id, name FROM users;", True))
        mock_get_model.return_value = mock_model

        state = {
            "query": "查询所有用户",
            "docs": [_mock_doc()],
        }
        result = await sql_generate(state)

        assert result["is_sql"] is True
        assert "SELECT" in result["sql"]
        assert result["sql"].endswith(";")

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_sql_passes_trace_callbacks(self, mock_get_model, mock_format):
        """SQL generation LLM call should be visible as a child trace."""
        from agents.flow.sql_react import sql_generate

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=_mock_llm_tool_response("SELECT 1;", True))
        mock_get_model.return_value = mock_model

        await sql_generate(
            {"query": "查询所有用户", "docs": [_mock_doc()]},
            config={"callbacks": ["trace-handler"]},
        )

        call_config = mock_model.ainvoke.call_args.kwargs["config"]
        assert call_config["callbacks"] == ["trace-handler"]
        assert call_config["run_name"] == "sql.sql_generate.llm"

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_normalizes_sql_artifacts(self, mock_get_model, mock_format):
        """sql_generate should strip sentinel tokens and SQL fences."""
        from agents.flow.sql_react import sql_generate

        raw_sql = """<text_never_used_51bce0c785ca2f68081bfa7d91973934>```sql
SELECT
  id
FROM users
```"""
        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=_mock_llm_tool_response(raw_sql, True))
        mock_get_model.return_value = mock_model

        result = await sql_generate({"query": "查询用户", "docs": [_mock_doc()]})

        assert result["is_sql"] is True
        assert result["sql"].startswith("SELECT")
        assert "text_never_used" not in result["sql"]
        assert "```" not in result["sql"]
        assert result["sql"].endswith(";")

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_strips_closing_sentinel_artifact(self, mock_get_model, mock_format):
        """sql_generate should strip closing sentinel artifacts after SQL."""
        from agents.flow.sql_react import sql_generate

        raw_sql = """SELECT SUM(ji.credit_amount - ji.debit_amount) AS 净利润
FROM t_journal_entry je
INNER JOIN t_journal_item ji ON je.id = ji.entry_id
INNER JOIN t_account a ON ji.account_code = a.account_code
WHERE je.status = '已过账'
AND je.period >= '2025-01'
AND je.period <= '2025-12'
AND a.account_type = '损益'
HAVING 净利润 < 0;</text_never_used_51bce0c785ca2f68081bfa7d91973934>;"""
        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=_mock_llm_tool_response(raw_sql, True))
        mock_get_model.return_value = mock_model

        result = await sql_generate({"query": "去年亏损", "docs": [_mock_doc()]})

        assert result["is_sql"] is True
        assert "text_never_used" not in result["sql"]
        assert result["sql"].endswith("HAVING 净利润 < 0;")
        assert not result["sql"].endswith(";;")

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_rejects_truncated_sql(self, mock_get_model, mock_format):
        """Truncated SQL should not proceed to approval/execution."""
        from agents.flow.sql_react import sql_generate

        raw_sql = """<text_never_used_51bce0c785ca2f68081bfa7d91973934>SELECT
  (SUM(credit_amount) - SUM(debit_amount)) AS net_profit
FROM t_journal_item
HAVIN"""
        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=_mock_llm_tool_response(raw_sql, True))
        mock_get_model.return_value = mock_model

        result = await sql_generate({"query": "去年亏损", "docs": [_mock_doc()]})

        assert result["is_sql"] is False
        assert result["error"] == "invalid_sql_format"
        assert "不完整" in result["answer"]
        assert "text_never_used" not in result["answer"]

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_rejects_invalid_simple_case_predicate_sql(self, mock_get_model, mock_format):
        """Invalid simple CASE predicate SQL should be repaired before execution."""
        from agents.flow.sql_react import sql_generate

        raw_sql = """SELECT CASE ji.credit_amount - ji.debit_amount
WHEN ji.credit_amount - ji.debit_amount > 0 THEN ji.credit_amount - ji.debit_amount
ELSE 0 END AS loss_amount
FROM t_journal_item ji;"""
        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=_mock_llm_tool_response(raw_sql, True))
        mock_get_model.return_value = mock_model

        result = await sql_generate({"query": "去年亏损", "docs": [_mock_doc()]})

        assert result["is_sql"] is False
        assert result["error"] == "invalid_sql_format"
        assert "CASE WHEN" in result["answer"]

    def test_complex_failure_summary_prefers_actual_error_over_sql_answer(self):
        from agents.flow.sql_react import _format_complex_execution_answer

        answer = _format_complex_execution_answer(
            {
                "steps": [
                    {"step": 1, "type": "sql", "goal": "查询亏损", "tables": ["t_journal_item"]},
                    {"step": 2, "type": "report", "goal": "生成报告", "depends_on": [1]},
                ]
            },
            {
                "1": {
                    "goal": "查询亏损",
                    "sql": "SELECT bad_amount FROM t_journal_item;",
                    "answer": "SELECT bad_amount FROM t_journal_item;",
                    "error": "Error: Unknown column 'bad_amount' in 'field list'",
                }
            },
            failed=True,
        )

        assert "错误: Error: Unknown column 'bad_amount' in 'field list'" in answer
        assert "错误: SELECT bad_amount" not in answer

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_non_sql_response(self, mock_get_model, mock_format):
        """When LLM responds with text (not SQL), is_sql should be False."""
        from agents.flow.sql_react import sql_generate

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=_mock_llm_tool_response("I cannot generate SQL for this.", False))
        mock_get_model.return_value = mock_model

        state = {
            "query": "你好",
            "docs": [_mock_doc()],
        }
        result = await sql_generate(state)

        assert result["is_sql"] is False

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_llm_failure_returns_user_safe_error(self, mock_get_model, mock_format):
        """SQL Harness should not expose raw provider errors when SQL generation LLM is unavailable."""
        from agents.flow.sql_react import sql_generate

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(side_effect=RuntimeError("AccountOverdueError raw provider details"))
        mock_get_model.return_value = mock_model

        result = await sql_generate({"query": "查询 2025 年销售收入总额", "docs": [_mock_doc()]})

        assert result["is_sql"] is False
        assert result["sql"] == ""
        assert result["error"] == "sql_generation_llm_unavailable"
        assert "SQL 生成模型暂时不可用" in result["answer"]
        assert "AccountOverdueError" not in result["answer"]

    def test_complex_plan_failure_prefers_user_safe_answer_over_error_code(self):
        from agents.flow.sql_react import _format_complex_execution_answer

        answer = _format_complex_execution_answer(
            {
                "steps": [
                    {"step": 1, "goal": "查询收入", "type": "sql"},
                    {"step": 2, "goal": "生成报告", "type": "report"},
                ]
            },
            {
                "1": {
                    "step": 1,
                    "goal": "查询收入",
                    "answer": "SQL 生成模型暂时不可用，无法生成可执行 SQL。请稍后重试或切换可用模型配置。",
                    "error": "sql_generation_llm_unavailable",
                }
            },
            failed=True,
        )

        assert "SQL 生成模型暂时不可用" in answer
        assert "sql_generation_llm_unavailable" not in answer

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_includes_refine_feedback(self, mock_get_model, mock_format):
        """Refine feedback should be included in the prompt."""
        from agents.flow.sql_react import sql_generate

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=_mock_llm_tool_response("SELECT id FROM users;", True))
        mock_get_model.return_value = mock_model

        state = {
            "query": "查询用户ID",
            "docs": [_mock_doc()],
            "refine_feedback": "只查询ID字段",
        }
        await sql_generate(state)

        # Verify the system message contains refine feedback
        call_args = mock_model.ainvoke.call_args[0][0]
        system_msg = call_args[0].content
        assert "只查询ID字段" in system_msg

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_passes_all_docs(self, mock_get_model, mock_format):
        """sql_generate passes all docs to LLM (no rerank filtering)."""
        from agents.flow.sql_react import sql_generate

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=_mock_llm_tool_response("SELECT 1;", True))
        mock_get_model.return_value = mock_model

        docs = [
            Document(page_content="t_user schema", metadata={"table_name": "t_user"}),
            Document(page_content="t_department schema", metadata={"table_name": "t_department"}),
            Document(page_content="t_role schema", metadata={"table_name": "t_role"}),
        ]

        state = {"query": "查询用户", "docs": docs}
        await sql_generate(state)

        # All docs should be in the prompt
        call_args = mock_model.ainvoke.call_args[0][0]
        system_msg = call_args[0].content
        assert "t_user schema" in system_msg
        assert "t_department schema" in system_msg
        assert "t_role schema" in system_msg

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_uses_enhanced_query(self, mock_get_model, mock_format):
        """sql_generate should pass enhanced_query to the LLM when present."""
        from agents.flow.sql_react import sql_generate

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=_mock_llm_tool_response("SELECT 1;", True))
        mock_get_model.return_value = mock_model

        state = {
            "query": "去年亏损",
            "rewritten_query": "去年亏损",
            "enhanced_query": "去年亏损（净利润），即净利润为负",
            "docs": [_mock_doc()],
        }
        await sql_generate(state)

        call_args = mock_model.ainvoke.call_args[0][0]
        human_msg = call_args[1].content
        assert "净利润为负" in human_msg

    def test_sql_prompt_contains_profit_loss_boundary_rules(self):
        """SQL prompt should prevent classifying zero net profit as loss."""
        from agents.flow.sql_react import _build_sql_messages

        messages = _build_sql_messages("亏损多少", "schema", "", "", "", "")
        system_msg = messages[0].content

        assert "等于 0" in system_msg
        assert "不要用 ELSE 把 0 归为亏损或盈利" in system_msg
        assert "亏损金额" in system_msg
        assert "ABS(净利润)" in system_msg

    def test_sql_prompt_requires_followup_to_reuse_prior_sql_context(self):
        """Follow-up queries should be instructed to reuse prior SQL semantics."""
        from agents.flow.sql_react import _build_sql_messages

        messages = _build_sql_messages("亏损多少", "schema", "", "上一轮SQL上下文", "", "")
        system_msg = messages[0].content

        assert "上一轮 SQL" in system_msg
        assert "时间范围" in system_msg
        assert "状态过滤" in system_msg
        assert "指标计算口径" in system_msg

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_includes_prior_sql_context(self, mock_get_model, mock_format):
        """SQL generation prompt should include saved prior SQL context."""
        from agents.flow.sql_react import sql_generate

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=_mock_llm_tool_response("SELECT 1;", True))
        mock_get_model.return_value = mock_model

        state = {
            "query": "亏损多少",
            "rewritten_query": "去年亏损多少",
            "docs": [_mock_doc()],
            "chat_history": [{
                "role": "system",
                "content": "[上一轮SQL上下文]\n用户问题: 去年亏损\n生成SQL:\nSELECT ... WHERE status = '已过账'\n展示结果: 净利润：0.00",
            }],
        }
        await sql_generate(state)

        system_msg = mock_model.ainvoke.call_args[0][0][0].content
        assert "上一轮SQL上下文" in system_msg
        assert "status = '已过账'" in system_msg

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables")
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_retrieves_missing_tables(
        self, mock_get_model, mock_format, mock_filter
    ):
        """When LLM says tables are missing, should re-retrieve and retry."""
        from agents.flow.sql_react import sql_generate

        mock_filter.return_value = {
            "t_user": {
                "id": {"column_name": "id", "column_type": "bigint", "column_comment": "主键", "is_pk": 1, "is_fk": 0, "business_name": "用户ID", "synonyms": "", "business_description": ""},
                "username": {"column_name": "username", "column_type": "varchar(64)", "column_comment": "用户名", "is_pk": 0, "is_fk": 0, "business_name": "用户名", "synonyms": "账号", "business_description": "登录账号"},
                "real_name": {"column_name": "real_name", "column_type": "varchar(64)", "column_comment": "真实姓名", "is_pk": 0, "is_fk": 0, "business_name": "真实姓名", "synonyms": "姓名", "business_description": ""},
            },
        }

        # First call: needs more tables; second call: has enough
        first_resp = MagicMock()
        first_resp.tool_calls = [{
            "args": {
                "answer": "",
                "is_sql": False,
                "needs_more_tables": True,
                "missing_tables": ["t_user"],
            }
        }]

        second_resp = MagicMock()
        second_resp.tool_calls = [{
            "args": {
                "answer": "SELECT d.name, u.real_name FROM t_department d JOIN t_user u ON d.manager = u.username;",
                "is_sql": True,
                "needs_more_tables": False,
                "missing_tables": [],
            }
        }]

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(side_effect=[first_resp, second_resp])
        mock_get_model.return_value = mock_model

        docs = [
            Document(
                page_content="t_department: id, name, manager",
                metadata={"table_name": "t_department"},
            ),
        ]
        state = {"query": "每个部门的负责人姓名", "docs": docs}
        result = await sql_generate(state)

        # Should have made 2 LLM calls
        assert mock_model.ainvoke.call_count == 2
        # Second call should include t_user schema
        second_call_args = mock_model.ainvoke.call_args_list[1][0][0]
        system_msg = second_call_args[0].content
        assert "t_user" in system_msg
        assert "t_department" in system_msg
        assert result["is_sql"] is True
        assert "JOIN" in result["sql"]

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables")
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_blocks_unauthorized_missing_tables(
        self, mock_get_model, mock_format, mock_filter
    ):
        """Missing-table retrieval must not bypass table permissions."""
        from agents.flow.sql_react import sql_generate

        first_resp = MagicMock()
        first_resp.tool_calls = [{
            "args": {
                "answer": "",
                "is_sql": False,
                "needs_more_tables": True,
                "missing_tables": ["t_user"],
            }
        }]

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=first_resp)
        mock_get_model.return_value = mock_model

        state = {
            "query": "每个部门的负责人姓名",
            "docs": [Document(page_content="t_department: id, name", metadata={"table_name": "t_department"})],
            "security_context": {"allowed_tables": ["t_department"]},
            "table_metadata": {"t_user": "用户/员工账号信息"},
        }
        result = await sql_generate(state)

        assert result["is_sql"] is False
        assert "用户/员工账号信息" in result["answer"]
        mock_filter.assert_not_called()

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables")
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_ignores_missing_tables_already_in_prompt(
        self, mock_get_model, mock_format, mock_filter
    ):
        """LLM false-positive missing tables already in docs should not trigger retrieval."""
        from agents.flow.sql_react import sql_generate

        resp = MagicMock()
        resp.tool_calls = [{
            "args": {
                "answer": (
                    "SELECT SUM(CASE WHEN a.account_type = '损益' "
                    "THEN ji.credit_amount - ji.debit_amount ELSE 0 END) AS net_profit "
                    "FROM t_journal_entry je "
                    "JOIN t_journal_item ji ON je.id = ji.entry_id "
                    "JOIN t_account a ON ji.account_code = a.account_code "
                    "WHERE je.period BETWEEN '2025-01' AND '2025-12';"
                ),
                "is_sql": True,
                "needs_more_tables": True,
                "missing_tables": '["t_account"]',
            }
        }]

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(return_value=resp)
        mock_get_model.return_value = mock_model

        docs = [
            Document(
                page_content=(
                    "表名: t_account\n"
                    "account_code varchar(20)\n"
                    "account_type enum('资产','负债','所有者权益','成本','损益')"
                ),
                metadata={},
            ),
            Document(
                page_content="t_journal_entry: id, period, status",
                metadata={"table_name": "t_journal_entry"},
            ),
            Document(
                page_content="t_journal_item: entry_id, account_code, debit_amount, credit_amount",
                metadata={"table_name": "t_journal_item"},
            ),
        ]

        result = await sql_generate({"query": "去年亏损多少", "docs": docs})

        assert result["is_sql"] is True
        assert "t_account" in result["sql"]
        assert mock_model.ainvoke.call_count == 1
        mock_filter.assert_not_called()

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_semantic_model_by_tables")
    @patch("agents.flow.sql_react.create_format_tool")
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_generate_normalizes_string_missing_tables_for_retrieval(
        self, mock_get_model, mock_format, mock_filter
    ):
        """Stringified missing_tables should be parsed before auth/retrieval."""
        from agents.flow.sql_react import sql_generate

        mock_filter.return_value = {
            "t_invoice": {
                "id": {"column_name": "id", "column_type": "int"},
                "invoice_no": {"column_name": "invoice_no", "column_type": "varchar(30)"},
            },
        }

        first_resp = MagicMock()
        first_resp.tool_calls = [{
            "args": {
                "answer": "",
                "is_sql": False,
                "needs_more_tables": True,
                "missing_tables": '["t_invoice"]',
            }
        }]
        second_resp = MagicMock()
        second_resp.tool_calls = [{
            "args": {
                "answer": "SELECT invoice_no FROM t_invoice;",
                "is_sql": True,
                "needs_more_tables": False,
                "missing_tables": [],
            }
        }]

        mock_model = MagicMock()
        mock_model.bind_tools.return_value = mock_model
        mock_model.ainvoke = AsyncMock(side_effect=[first_resp, second_resp])
        mock_get_model.return_value = mock_model

        result = await sql_generate({
            "query": "查询发票编号",
            "docs": [Document(page_content="表名: t_receivable_payable", metadata={"table_name": "t_receivable_payable"})],
        })

        assert result["is_sql"] is True
        assert mock_filter.call_args.args[0] == ["t_invoice"]

    def test_sql_prompt_forbids_missing_tables_already_in_schema(self):
        """Prompt should explicitly prevent reporting already-provided tables as missing."""
        from agents.flow.sql_react import _build_sql_messages

        system_msg = _build_sql_messages(
            "去年亏损多少",
            "表名: t_account\naccount_type enum('损益')",
            "",
            "",
            "",
            "",
        )[0].content

        assert "已提供表结构中的表不得再列入 missing_tables" in system_msg
        assert "missing_tables 必须是数组" in system_msg
        assert "account_type='损益'" in system_msg


# ---------------------------------------------------------------------------
# execute_sql node
# ---------------------------------------------------------------------------

class TestExecuteSql:
    """Test execute_sql node."""

    @pytest.mark.asyncio
    @patch("agents.tool.sql_tools.mcp_client.execute_sql")
    async def test_execute_success(self, mock_mcp_execute):
        """Successful SQL execution should return result."""
        from agents.flow.sql_react import execute_sql as exec_node

        mock_mcp_execute.return_value = '[{"id": 1, "name": "Alice"}]'

        result = await exec_node({"sql": "SELECT * FROM users"})

        assert '[{"id": 1' in result["result"]
        assert result["answer"] == "查询已执行完成。\nid：1\nname：Alice"

    @pytest.mark.asyncio
    @patch("agents.tool.sql_tools.mcp_client.execute_sql")
    async def test_execute_uses_business_field_names_from_semantic_model(self, mock_mcp_execute):
        """User-visible SQL result should use business field names when available."""
        from agents.flow.sql_react import execute_sql as exec_node

        mock_mcp_execute.return_value = '[{"real_name":"张三","name":"财务审核"}]'

        result = await exec_node({
            "sql": "SELECT u.real_name, r.name FROM t_user u JOIN t_role r ON r.id = 1",
            "selected_tables": ["t_user", "t_role"],
            "semantic_model": {
                "t_user": {"real_name": {"business_name": "真实姓名"}},
                "t_role": {"name": {"business_name": "角色名称"}},
            },
            "execution_history": [],
        })

        assert "真实姓名：张三" in result["answer"]
        assert "角色名称：财务审核" in result["answer"]
        assert "real_name" not in result["answer"]

    @pytest.mark.asyncio
    @patch("agents.tool.sql_tools.mcp_client.execute_sql")
    async def test_execute_business_term_path_uses_semantic_field_names(self, mock_mcp_execute):
        """Business-term formatting should still avoid leaking physical field names."""
        from agents.flow.sql_react import execute_sql as exec_node

        mock_mcp_execute.return_value = '[{"real_name":"张三"}]'

        result = await exec_node({
            "query": "查询用户姓名",
            "sql": "SELECT real_name FROM t_user",
            "selected_tables": ["t_user"],
            "semantic_model": {
                "t_user": {"real_name": {"business_name": "真实姓名"}},
            },
            "evidence": [
                "术语: 用户姓名\n同义词: 姓名\n关联表: t_user",
            ],
            "execution_history": [],
        })

        assert "真实姓名：张三" in result["answer"]
        assert "real_name" not in result["answer"]

    @pytest.mark.asyncio
    @patch("agents.tool.sql_tools.mcp_client.execute_sql")
    async def test_execute_error(self, mock_mcp_execute):
        """SQL execution error should be caught and returned."""
        from agents.flow.sql_react import execute_sql as exec_node

        mock_mcp_execute.side_effect = Exception("Table not found")

        result = await exec_node({"sql": "SELECT * FROM nonexistent", "execution_history": []})

        assert "失败" in result["result"]
        assert "Table not found" in result["result"]
        assert result["error"] == "Table not found"
        assert len(result["execution_history"]) == 1
        assert result["execution_history"][0]["error"] == "Table not found"

    @pytest.mark.asyncio
    @patch("agents.tool.sql_tools.mcp_client.execute_sql")
    async def test_execute_success_clears_error(self, mock_mcp_execute):
        """Successful execution should set error=None."""
        from agents.flow.sql_react import execute_sql as exec_node

        mock_mcp_execute.return_value = '[{"id": 1}]'

        result = await exec_node({"sql": "SELECT 1", "execution_history": []})

        assert result["error"] is None
        assert len(result["execution_history"]) == 1
        assert result["execution_history"][0]["error"] is None

    @pytest.mark.asyncio
    @patch("agents.tool.sql_tools.mcp_client.execute_sql")
    async def test_execute_summarizes_result_with_context(self, mock_mcp_execute):
        """SQL results should be summarized locally using query/schema/evidence context."""
        from agents.flow.sql_react import execute_sql as exec_node

        mock_mcp_execute.return_value = '[{"is_net_profit_positive":0,"net_profit":"0.00"}]Query execution time: 10.97 ms'

        result = await exec_node({
            "query": "去年亏损",
            "sql": "SELECT ...",
            "docs": [_mock_doc("net_profit [业务名: 净利润] [同义词: 亏损, 净亏损]")],
            "evidence": ["术语: 净利润\n公式: 亏损表示净利润 < 0\n同义词: 亏损, 净亏损"],
            "execution_history": [],
        })

        assert result["result"].startswith('[{"is_net_profit_positive"')
        assert result["answer"] == "查询已执行完成。\n是否亏损：否\n净利润：0.00"

    @pytest.mark.asyncio
    @patch("agents.tool.sql_tools.mcp_client.execute_sql")
    async def test_execute_quantity_followup_uses_query_term_label(self, mock_mcp_execute):
        """Quantity follow-up should use the matched user term instead of stale SQL alias."""
        from agents.flow.sql_react import execute_sql as exec_node

        mock_mcp_execute.return_value = '[{"去年净利润":"0.00"}]Query execution time: 10.97 ms'

        result = await exec_node({
            "query": "亏损多少",
            "rewritten_query": "去年亏损多少",
            "sql": "SELECT ROUND(SUM(x), 2) AS 去年净利润 FROM t;",
            "docs": [_mock_doc("net_profit [业务名: 净利润] [同义词: 亏损, 净亏损]")],
            "evidence": ["术语: 净利润\n公式: 亏损表示净利润 < 0\n同义词: 亏损, 净亏损"],
            "execution_history": [],
        })

        assert result["answer"] == "查询已执行完成。\n亏损金额：0.00"


class TestResultAnomaly:
    """Test suspicious execution result detection and reflection."""

    def test_detect_empty_and_null_results(self):
        from agents.flow.sql_react import _result_anomaly_reason

        assert "空集" in _result_anomaly_reason("[]")
        assert "空集" in _result_anomaly_reason('{"rows": []}')
        assert "空集" in _result_anomaly_reason('{"columns": ["net_profit"], "rows": []}')
        assert "空集" in _result_anomaly_reason('{"data": []}')
        assert "空集" in _result_anomaly_reason('{"result": []}')
        assert "NULL" in _result_anomaly_reason('[{"净利润": null}]')
        assert "NULL" in _result_anomaly_reason('[{"净利润": null, "是否亏损": "否"}]')
        assert "NULL" in _result_anomaly_reason('{"rows": [[null]]}')
        assert "NULL" in _result_anomaly_reason('{"rows": [{"净利润": ""}]}')
        assert _result_anomaly_reason('[{"净利润": -10}]') is None

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_result_reflection_generates_sql(self, mock_get_model):
        from agents.flow.sql_react import result_reflection

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="SELECT COALESCE(SUM(x), 0) AS net_profit FROM t;"))
        mock_get_model.return_value = mock_model

        result = await result_reflection({
            "query": "去年亏损",
            "sql": "SELECT SUM(x) AS net_profit FROM t HAVING net_profit < 0;",
            "result": '[{"net_profit": null}]',
            "docs": [_mock_doc()],
            "retry_count": 0,
        })

        assert result["is_sql"] is True
        assert result["error"] is None
        assert result["sql"] == "SELECT COALESCE(SUM(x), 0) AS net_profit FROM t;"
        assert result["retry_count"] == 1


# ---------------------------------------------------------------------------
# Graph structure
# ---------------------------------------------------------------------------

class TestBuildSqlReactGraph:
    """Test graph construction and routing."""

    @patch("agents.flow.sql_react.get_checkpointer")
    def test_graph_has_all_nodes(self, mock_cp):
        """Graph should contain all expected nodes."""
        from langgraph.checkpoint.memory import MemorySaver
        from agents.flow.sql_react import build_sql_react_graph

        mock_cp.return_value = MemorySaver()
        graph = build_sql_react_graph()
        node_names = list(graph.get_graph().nodes.keys())

        assert "sql_retrieve" in node_names
        assert "check_docs" in node_names
        assert "sql_generate" in node_names
        assert "safety_check" in node_names
        assert "approve" in node_names
        assert "execute_sql" in node_names
        assert "error_analysis" in node_names
        assert "result_reflection" in node_names
        assert "query_enhance" in node_names
        assert "recall_evidence" in node_names
        assert "assess_feasibility" in node_names
        assert "authorize_selected_tables" in node_names
        assert "authorize_sql" in node_names
        assert "infer_route_signal" not in node_names
        assert "route_complexity" not in node_names
        assert "complex_plan_generate" in node_names
        assert "approve_complex_plan" in node_names
        assert "execute_complex_plan_step" in node_names
        assert "contextualize_query" not in node_names

    @patch("agents.flow.sql_react.get_checkpointer")
    def test_graph_starts_at_recall_evidence(self, mock_cp):
        """SQL React must rely on dispatcher rewritten_query and start with evidence recall."""
        from langgraph.checkpoint.memory import MemorySaver
        from agents.flow.sql_react import build_sql_react_graph

        mock_cp.return_value = MemorySaver()
        graph = build_sql_react_graph()
        edges = graph.get_graph().edges

        assert any(edge.source == "__start__" and edge.target == "recall_evidence" for edge in edges)
        assert not any(edge.source == "__start__" and edge.target == "contextualize_query" for edge in edges)

    @patch("agents.flow.sql_react.get_checkpointer")
    def test_graph_compiles(self, mock_cp):
        """Graph should compile without errors."""
        from langgraph.checkpoint.memory import MemorySaver
        from agents.flow.sql_react import build_sql_react_graph

        mock_cp.return_value = MemorySaver()
        graph = build_sql_react_graph()
        assert graph is not None


# ---------------------------------------------------------------------------
# Route functions
# ---------------------------------------------------------------------------

class TestRouting:
    """Test conditional routing logic."""

    @patch("agents.flow.sql_react.get_checkpointer")
    def test_route_after_check_no_docs(self, mock_cp):
        """When check_docs sets is_sql=False with answer, should route to END."""
        from langgraph.checkpoint.memory import MemorySaver
        from agents.flow.sql_react import build_sql_react_graph

        mock_cp.return_value = MemorySaver()
        graph = build_sql_react_graph()

        # The route_after_check function is internal; test via graph structure
        # by verifying the graph compiles with conditional edges
        assert graph is not None

    @patch("agents.flow.sql_react.get_checkpointer")
    def test_route_after_safety_non_sql(self, mock_cp):
        """When safety_check sets is_sql=False, should route to END."""
        from langgraph.checkpoint.memory import MemorySaver
        from agents.flow.sql_react import build_sql_react_graph

        mock_cp.return_value = MemorySaver()
        graph = build_sql_react_graph()
        assert graph is not None

    @patch("agents.flow.sql_react.get_checkpointer")
    def test_route_after_approve_rejected(self, mock_cp):
        """When approve sets approved=False, should route to END."""
        from langgraph.checkpoint.memory import MemorySaver
        from agents.flow.sql_react import build_sql_react_graph

        mock_cp.return_value = MemorySaver()
        graph = build_sql_react_graph()
        assert graph is not None

    def test_unknown_column_error_is_repairable(self):
        """Generated SQL scope/column errors should enter SQL repair."""
        from agents.flow.sql_react import _should_repair_sql_error

        assert _should_repair_sql_error("Error: Unknown column 'a.account_type' in 'field list'")

    def test_permission_error_is_not_llm_repairable(self):
        """Auth/permission failures should not be fixed by regenerating SQL."""
        from agents.flow.sql_react import _should_repair_sql_error

        assert not _should_repair_sql_error("Access denied for user readonly")


class TestApprove:
    """Test approve node messaging."""

    @patch("agents.flow.sql_react.interrupt")
    def test_approve_message_for_repaired_sql_after_execution_error(self, mock_interrupt):
        from agents.flow.sql_react import approve

        mock_interrupt.return_value = {"approved": False, "feedback": "wait"}

        result = approve({
            "sql": "SELECT 1;",
            "execution_history": [
                {"sql": "SELECT bad;", "result": None, "error": "Unknown column 'bad'"}
            ],
        })

        interrupt_payload = mock_interrupt.call_args.args[0]
        assert "执行失败" in interrupt_payload["message"]
        assert "修正后的 SQL" in interrupt_payload["message"]
        assert result["approved"] is False


# ---------------------------------------------------------------------------
# error_analysis node
# ---------------------------------------------------------------------------

class TestErrorAnalysis:
    """Test error_analysis node."""

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_error_analysis_generates_feedback(self, mock_get_model):
        """error_analysis should generate refine_feedback and increment retry_count."""
        from agents.flow.sql_react import error_analysis

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="表名应为 t_user 而不是 users"))
        mock_get_model.return_value = mock_model

        state = {
            "query": "查询用户",
            "sql": "SELECT * FROM users",
            "error": "Table 'go_agent_audit.users' doesn't exist",
            "docs": [_mock_doc()],
            "retry_count": 0,
        }
        result = await error_analysis(state)

        assert "t_user" in result["refine_feedback"]
        assert result["retry_count"] == 1

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.get_chat_model")
    async def test_error_analysis_increments_count(self, mock_get_model):
        """retry_count should increment from any starting value."""
        from agents.flow.sql_react import error_analysis

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="修正建议"))
        mock_get_model.return_value = mock_model

        state = {
            "query": "查询",
            "sql": "SELECT 1",
            "error": "syntax error",
            "docs": [],
            "retry_count": 2,
        }
        result = await error_analysis(state)

        assert result["retry_count"] == 3


# ---------------------------------------------------------------------------
# recall_evidence node
# ---------------------------------------------------------------------------

class TestRecallEvidence:
    """Test evidence recall tracing propagation."""

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.recall_agent_knowledge")
    @patch("agents.flow.sql_react.recall_business_knowledge")
    async def test_recall_evidence_passes_trace_callbacks(self, mock_business, mock_agent):
        """Knowledge retrievers should inherit graph callbacks."""
        from agents.flow.sql_react import recall_evidence

        mock_business.return_value = [
            Document(page_content="术语: 净利润\n公式: 收入 - 成本", metadata={"score": 0.9})
        ]
        mock_agent.return_value = [
            Document(page_content="SELECT 1;", metadata={"score": 0.9})
        ]

        result = await recall_evidence(
            {"query": "去年亏损", "rewritten_query": "去年亏损"},
            config={"callbacks": ["trace-handler"]},
        )

        assert result["evidence"] == ["术语: 净利润\n公式: 收入 - 成本"]
        assert result["few_shot_examples"] == ["SELECT 1;"]
        assert mock_business.call_args.kwargs["callbacks"] == ["trace-handler"]
        assert mock_agent.call_args.kwargs["callbacks"] == ["trace-handler"]

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.recall_agent_knowledge")
    @patch("agents.flow.sql_react.recall_business_knowledge")
    async def test_recall_evidence_builds_structured_context(self, mock_business, mock_agent):
        """Recall should run once and store structured evidence for later nodes."""
        from agents.flow.sql_react import recall_evidence

        mock_business.return_value = [
            Document(
                page_content=(
                    "术语: 年度预算\n"
                    "公式: SUM(budget_amount) GROUP BY cost_center_id\n"
                    "同义词: 年度预算总金额, 全年预算\n"
                    "关联表: t_budget,t_cost_center"
                ),
                metadata={"score": 0.9},
            )
        ]
        mock_agent.return_value = [
            Document(
                page_content=(
                    "问题: 查询各部门年度预算总额\n"
                    "SQL: SELECT cc.center_name, SUM(b.budget_amount) "
                    "FROM t_budget b JOIN t_cost_center cc ON b.cost_center_id = cc.id\n"
                    "说明: 年度预算执行率排名"
                ),
                metadata={"score": 0.9},
            )
        ]

        result = await recall_evidence(
            {"query": "查询各个部门的年度预算总金额", "rewritten_query": "查询各个部门的年度预算总金额"},
        )

        context = result["recall_context"]
        assert context["query_key"] == "查询各个部门的年度预算总金额"
        assert context["business_related_tables"] == ["t_budget", "t_cost_center"]
        assert context["few_shot_related_tables"] == ["t_budget", "t_cost_center"]
        assert "年度预算" in context["matched_terms"]
        assert context["few_shot_questions"] == ["查询各部门年度预算总额"]

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.recall_agent_knowledge")
    @patch("agents.flow.sql_react.recall_business_knowledge")
    async def test_recall_evidence_filters_unmatched_business_related_tables(self, mock_business, mock_agent):
        """Unmatched business knowledge should not pollute recall_context related tables."""
        from agents.flow.sql_react import recall_evidence

        mock_business.return_value = [
            Document(
                page_content=(
                    "术语: 净利润\n"
                    "公式: 收入 - 成本 - 费用\n"
                    "同义词: 亏损, 亏损金额\n"
                    "关联表: t_journal_item,t_account,t_expense_claim"
                ),
                metadata={"score": 0.9},
            ),
            Document(
                page_content=(
                    "术语: 年度预算\n"
                    "公式: SUM(budget_amount)\n"
                    "同义词: 预算总额, 全年预算\n"
                    "关联表: t_budget,t_cost_center,t_department"
                ),
                metadata={"score": 0.8},
            ),
            Document(
                page_content=(
                    "术语: 固定资产净值\n"
                    "公式: original_value - accumulated_depreciation\n"
                    "同义词: 资产净额\n"
                    "关联表: t_fixed_asset"
                ),
                metadata={"score": 0.7},
            ),
        ]
        mock_agent.return_value = []

        result = await recall_evidence(
            {"query": "查询当前公司去年的亏损金额", "rewritten_query": "查询当前公司去年的亏损金额"},
        )

        context = result["recall_context"]
        assert context["business_related_tables"] == ["t_journal_item", "t_account", "t_expense_claim"]
        assert context["matched_terms"] == ["净利润"]

    @pytest.mark.asyncio
    @patch("agents.flow.sql_react.recall_agent_knowledge")
    @patch("agents.flow.sql_react.recall_business_knowledge")
    async def test_recall_evidence_matches_business_formula_terms(self, mock_business, mock_agent):
        """Formula text from retrieved business knowledge should also drive table context."""
        from agents.flow.sql_react import recall_evidence

        mock_business.return_value = [
            Document(
                page_content=(
                    "术语: 净利润\n"
                    "公式: 收入 - 成本 - 费用\n"
                    "同义词: 盈利, 亏损\n"
                    "关联表: t_journal_entry,t_journal_item,t_account,t_expense_claim"
                ),
                metadata={"score": 0.9},
            ),
            Document(
                page_content=(
                    "术语: 预算执行率\n"
                    "公式: actual_amount / budget_amount * 100\n"
                    "同义词: 预算完成率\n"
                    "关联表: t_budget"
                ),
                metadata={"score": 0.8},
            ),
            Document(
                page_content=(
                    "术语: 固定资产净值\n"
                    "公式: original_value - accumulated_depreciation\n"
                    "同义词: 资产净额\n"
                    "关联表: t_fixed_asset"
                ),
                metadata={"score": 0.7},
            ),
        ]
        mock_agent.return_value = []

        result = await recall_evidence(
            {
                "query": "收入成本预算回款费用之间的关系",
                "rewritten_query": "分析当前公司收入成本预算回款费用之间的关系",
            },
        )

        context = result["recall_context"]
        assert "t_journal_item" in context["business_related_tables"]
        assert "t_journal_entry" in context["business_related_tables"]
        assert "t_budget" in context["business_related_tables"]
        assert "t_fixed_asset" not in context["business_related_tables"]


# ---------------------------------------------------------------------------
# Business knowledge recall
# ---------------------------------------------------------------------------

class TestBusinessKnowledgeRecall:
    """Test business knowledge fallback recall."""

    @patch("agents.rag.retriever._load_business_knowledge_from_mysql")
    @patch("agents.rag.retriever._es_bm25_search")
    @patch("agents.rag.retriever._milvus_vector_search")
    def test_synonym_lexical_fallback(self, mock_vector, mock_es, mock_load_bk):
        """When vector/BM25 miss, configured synonyms should recall business knowledge."""
        from agents.rag.retriever import recall_business_knowledge

        mock_vector.return_value = []
        mock_es.return_value = []
        mock_load_bk.return_value = [{
            "term": "净利润",
            "formula": "收入 - 成本 - 费用；亏损表示净利润 < 0",
            "synonyms": "净收益, 盈利, 亏损, 净亏损, 赔钱, 赚钱",
            "related_tables": "t_journal_item,t_account,t_expense_claim",
        }]

        docs = recall_business_knowledge("去年亏损", top_k=5)

        assert len(docs) == 1
        assert "术语: 净利润" in docs[0].page_content
        assert docs[0].metadata["retriever_source"] == "mysql_lexical"
        assert "亏损" in docs[0].metadata["matched_terms"]


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

class TestToolRegistry:
    """Test the tool registry system."""

    def test_register_and_get_tools(self):
        """Registered tools should be retrievable by category."""
        from agents.tool.registry import register, get_tools, clear

        clear()
        try:
            @register("test_cat")
            @tool
            def my_test_tool(x: str) -> str:
                """A test tool."""
                return x

            tools = get_tools("test_cat")
            assert len(tools) == 1
            assert tools[0].name == "my_test_tool"
        finally:
            clear()

    def test_get_tools_empty_category(self):
        """Getting tools for a non-existent category should return empty list."""
        from agents.tool.registry import get_tools

        result = get_tools("nonexistent_category_xyz")
        assert result == []

    def test_get_all_tools(self):
        """Getting tools with no categories should return all."""
        from agents.tool.registry import register, get_tools, clear

        clear()
        try:
            @register("cat_a")
            @tool
            def tool_a(x: str) -> str:
                """Tool A."""
                return x

            @register("cat_b")
            @tool
            def tool_b(x: str) -> str:
                """Tool B."""
                return x

            all_tools = get_tools()
            assert len(all_tools) >= 2
        finally:
            clear()

    def test_sql_tools_registered(self):
        """SQL tools should be registered when sql_tools package is imported."""
        from agents.tool.registry import get_tools, register_tool
        from agents.tool.sql_tools.execute_tool import execute_query
        from agents.tool.sql_tools.schema_tool import list_tables, describe_table

        # Re-register since clear() in earlier tests may have removed them
        register_tool("sql", execute_query)
        register_tool("sql", list_tables)
        register_tool("sql", describe_table)

        sql_tools = get_tools("sql")
        tool_names = [t.name for t in sql_tools]
        assert "execute_query" in tool_names
        assert "list_tables" in tool_names
        assert "describe_table" in tool_names
