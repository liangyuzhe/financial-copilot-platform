"""Tests for Query API endpoints: invoke, approve, interrupt handling."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.api.routers.query import _extract_interrupt


# ---------------------------------------------------------------------------
# _extract_interrupt helper
# ---------------------------------------------------------------------------

class TestExtractInterrupt:
    """Test interrupt extraction from graph result."""

    def test_extract_from_dict_value(self):
        """Should extract dict from interrupt value."""
        interrupt = MagicMock()
        interrupt.value = {"sql": "SELECT 1", "message": "confirm?"}
        result = _extract_interrupt({"__interrupt__": [interrupt]})

        assert result is not None
        assert result["sql"] == "SELECT 1"
        assert result["message"] == "confirm?"

    def test_extract_from_list_value(self):
        """Should extract first item when value is a list."""
        interrupt = MagicMock()
        interrupt.value = [{"sql": "SELECT 1", "message": "confirm?"}]
        result = _extract_interrupt({"__interrupt__": [interrupt]})

        assert result is not None
        assert result["sql"] == "SELECT 1"

    def test_no_interrupt_returns_none(self):
        """Should return None when no interrupt."""
        result = _extract_interrupt({"answer": "done"})
        assert result is None

    def test_empty_interrupt_returns_none(self):
        """Should return None when interrupt list is empty."""
        result = _extract_interrupt({"__interrupt__": []})
        assert result is None

    def test_non_dict_value_returns_none(self):
        """Should return None when value is not a dict."""
        interrupt = MagicMock()
        interrupt.value = "just a string"
        result = _extract_interrupt({"__interrupt__": [interrupt]})
        assert result is None


class TestSessionSqlContext:
    """Test SQL context memory for follow-up queries."""

    @patch("agents.api.routers.query.save_session")
    @patch("agents.api.routers.query.get_session")
    def test_save_sql_context_to_session(self, mock_get_session, mock_save_session):
        from agents.api.routers.query import _save_sql_context_to_session
        from agents.tool.memory.session import Session

        session = Session(id="s1")
        mock_get_session.return_value = session

        _save_sql_context_to_session(
            "s1",
            "去年亏损",
            "SELECT * FROM t WHERE status = '已过账'",
            "净利润：0.00",
        )

        context = session.preferences["_last_sql_context"]
        assert "去年亏损" in context
        assert "status = '已过账'" in context
        assert "净利润：0.00" in context
        mock_save_session.assert_called_once()

    @patch("agents.api.routers.query.get_session")
    def test_load_chat_history_includes_last_sql_context(self, mock_get_session):
        from agents.api.routers.query import _load_chat_history
        from agents.tool.memory.session import Session

        session = Session(id="s1")
        session.preferences["_last_sql_context"] = "生成SQL:\nSELECT 1"
        mock_get_session.return_value = session

        history = _load_chat_history("s1")

        assert history[0]["role"] == "system"
        assert history[0]["content"].startswith("[上一轮SQL上下文]")
        assert "SELECT 1" in history[0]["content"]

    @patch("agents.api.routers.query.get_session")
    def test_load_chat_history_uses_short_sliding_window(self, mock_get_session):
        from agents.api.routers.query import _load_chat_history
        from agents.tool.memory.session import Message, Session

        session = Session(
            id="s1",
            summary="用户关注公司经营指标",
            history=[
                Message(role="user" if i % 2 == 0 else "assistant", content=f"msg-{i}")
                for i in range(10)
            ],
        )
        session.preferences["_last_sql_context"] = "生成SQL:\nSELECT 1"
        mock_get_session.return_value = session

        history = _load_chat_history("s1")

        assert history[0]["content"].startswith("[上一轮SQL上下文]")
        assert history[1]["content"].startswith("[对话摘要]")
        assert [m["content"] for m in history[2:]] == [f"msg-{i}" for i in range(4, 10)]

    @patch("agents.api.routers.query.get_session")
    def test_load_chat_history_includes_long_term_vector_memory(self, mock_get_session, monkeypatch):
        from langchain_core.documents import Document

        from agents.api.routers.query import _load_chat_history
        from agents.tool.memory.session import Session

        session = Session(id="s1")
        session.preferences["_has_long_term_memory"] = "1"
        mock_get_session.return_value = session
        monkeypatch.setattr(
            "agents.tool.memory.vector_store.recall_long_term_memory",
            lambda session_id, query: [Document(page_content="用户之前关注去年亏损口径")],
        )

        history = _load_chat_history("s1", "亏损多少")

        assert history[0]["role"] == "system"
        assert history[0]["content"].startswith("[长期记忆]")
        assert "去年亏损口径" in history[0]["content"]


# ---------------------------------------------------------------------------
# /api/query/invoke
# ---------------------------------------------------------------------------

class TestQueryInvoke:
    """Test POST /api/query/invoke."""

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_invoke_returns_answer(self, mock_build_graph, mock_callbacks):
        """Normal completion should return answer."""
        from agents.api.routers.query import query_invoke, QueryRequest

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={
            "answer": "42 rows found",
            "status": "completed",
        })
        mock_build_graph.return_value = mock_graph

        req = QueryRequest(query="count users", session_id="s1")
        result = await query_invoke(req)

        assert result.answer == "42 rows found"
        assert result.status == "completed"
        assert result.pending_approval is False

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_invoke_returns_execution_result_payload(self, mock_build_graph, mock_callbacks):
        """Execution result payload should be exposed to the frontend response."""
        from agents.api.routers.query import query_invoke, QueryRequest

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={
            "answer": "查询已执行完成。\n亏损金额：0.00",
            "status": "completed",
            "sql": "SELECT 0 AS loss_amount;",
            "result": '[{"loss_amount":"0.00"}]',
        })
        mock_build_graph.return_value = mock_graph

        result = await query_invoke(QueryRequest(query="去年亏损", session_id="s1"))

        assert result.result == '[{"loss_amount":"0.00"}]'

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_invoke_returns_pending_approval(self, mock_build_graph, mock_callbacks):
        """When graph returns interrupt, should return pending_approval."""
        from agents.api.routers.query import query_invoke, QueryRequest

        interrupt_obj = MagicMock()
        interrupt_obj.value = {"sql": "SELECT * FROM users", "message": "请确认?", "approval_type": "sql"}

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={
            "__interrupt__": [interrupt_obj],
        })
        mock_build_graph.return_value = mock_graph

        req = QueryRequest(query="查询用户", session_id="s1")
        result = await query_invoke(req)

        assert result.pending_approval is True
        assert result.status == "pending_approval"
        assert result.sql == "SELECT * FROM users"
        assert result.approval_type == "sql"
        assert "确认" in result.answer

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_invoke_returns_pending_complex_plan_approval(self, mock_build_graph, mock_callbacks):
        """Complex plan interrupts should be exposed even without SQL text."""
        from agents.api.routers.query import query_invoke, QueryRequest

        interrupt_obj = MagicMock()
        interrupt_obj.value = {
            "complex_plan": {"mode": "complex_plan", "steps": [{"step": 1, "goal": "分析收入"}]},
            "message": "检测到复杂多表分析问题，已生成执行计划，请确认是否按计划执行：",
            "approval_type": "complex_plan",
        }

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={
            "__interrupt__": [interrupt_obj],
        })
        mock_build_graph.return_value = mock_graph

        req = QueryRequest(query="复杂分析", session_id="s1")
        result = await query_invoke(req)

        assert result.pending_approval is True
        assert result.status == "pending_approval"
        assert result.sql == ""
        assert result.approval_type == "complex_plan"
        assert "执行计划" in result.answer

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_invoke_passes_thread_id(self, mock_build_graph, mock_callbacks):
        """Should pass thread_id in config."""
        from agents.api.routers.query import query_invoke, QueryRequest

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={"answer": "ok", "status": "completed"})
        mock_build_graph.return_value = mock_graph

        req = QueryRequest(query="test", session_id="my-session")
        await query_invoke(req)

        call_kwargs = mock_graph.ainvoke.call_args
        config = call_kwargs[1].get("config") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs[1].get("config")
        # Each new query should use an isolated graph thread under the session.
        assert config["configurable"]["thread_id"].startswith("my-session:turn:")

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_invoke_skips_classify_with_intent(self, mock_build_graph, mock_callbacks):
        """When intent is provided, should pass it to graph (skip LLM classify)."""
        from agents.api.routers.query import query_invoke, QueryRequest

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={"answer": "ok", "status": "completed"})
        mock_build_graph.return_value = mock_graph

        req = QueryRequest(query="查用户", session_id="s1", intent="sql_query")
        await query_invoke(req)

        call_args = mock_graph.ainvoke.call_args
        initial_state = call_args[0][0]
        assert initial_state["intent"] == "sql_query"
        assert initial_state["rewritten_query"] == ""

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_invoke_passes_route_to_graph_without_old_sql_intent(self, mock_build_graph, mock_callbacks):
        """Route-prefilled requests should enter the graph as data/chat/clarify routes."""
        from agents.api.routers.query import query_invoke, QueryRequest

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={"answer": "ok", "status": "completed"})
        mock_build_graph.return_value = mock_graph

        req = QueryRequest(
            query="分析今年收入、成本和预算关系",
            session_id="s1",
            route="data",
            rewritten_query="分析公司今年收入、成本和预算关系",
        )
        await query_invoke(req)

        initial_state = mock_graph.ainvoke.call_args[0][0]
        assert initial_state["route"] == "data"
        assert initial_state["intent"] == "data"
        assert initial_state["rewritten_query"] == "分析公司今年收入、成本和预算关系"

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_invoke_passes_rewritten_query_to_graph(self, mock_build_graph, mock_callbacks):
        """Pre-classified rewritten_query should be present in graph input."""
        from agents.api.routers.query import query_invoke, QueryRequest

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={"answer": "ok", "status": "completed"})
        mock_build_graph.return_value = mock_graph

        req = QueryRequest(
            query="第一季度员工工资",
            session_id="s1",
            intent="sql_query",
            rewritten_query="我们公司第一季度的员工工资情况",
        )
        await query_invoke(req)

        initial_state = mock_graph.ainvoke.call_args[0][0]
        assert initial_state["query"] == "第一季度员工工资"
        assert initial_state["rewritten_query"] == "我们公司第一季度的员工工资情况"


# ---------------------------------------------------------------------------
# /api/query/approve
# ---------------------------------------------------------------------------

class TestQueryApprove:
    """Test POST /api/query/approve."""

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_approve_returns_result(self, mock_build_graph, mock_callbacks):
        """Approved SQL should return execution result."""
        from agents.api.routers.query import approve_sql, ApproveRequest

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={
            "answer": '[{"id": 1}]',
            "status": "completed",
        })
        mock_build_graph.return_value = mock_graph

        req = ApproveRequest(session_id="s1", approved=True)
        result = await approve_sql(req)

        assert result.answer == '[{"id": 1}]'
        assert result.status == "completed"

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_approve_sends_command_resume(self, mock_build_graph, mock_callbacks):
        """Should send Command(resume=...) to graph."""
        from agents.api.routers.query import approve_sql, ApproveRequest
        from langgraph.types import Command

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={"answer": "ok", "status": "completed"})
        mock_build_graph.return_value = mock_graph

        req = ApproveRequest(session_id="s1", approved=True, feedback="looks good")
        await approve_sql(req)

        call_args = mock_graph.ainvoke.call_args
        cmd = call_args[0][0]
        assert isinstance(cmd, Command)
        assert cmd.resume["approved"] is True
        assert cmd.resume["feedback"] == "looks good"

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.save_session")
    @patch("agents.api.routers.query.get_session")
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_approve_uses_pending_graph_thread(
        self,
        mock_build_graph,
        mock_callbacks,
        mock_get_session,
        mock_save_session,
    ):
        """Approval should resume the pending turn thread, not the chat session id."""
        from agents.api.routers.query import approve_sql, ApproveRequest
        from agents.tool.memory.session import Session

        session = Session(id="s1")
        session.preferences["_pending_query"] = "查询用户"
        session.preferences["_pending_thread_id"] = "s1:turn:abc123"
        mock_get_session.return_value = session

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={"answer": "ok", "status": "completed"})
        mock_build_graph.return_value = mock_graph

        await approve_sql(ApproveRequest(session_id="s1", approved=True))

        config = mock_graph.ainvoke.call_args.kwargs["config"]
        assert config["configurable"]["thread_id"] == "s1:turn:abc123"

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_approve_reject_still_returns(self, mock_build_graph, mock_callbacks):
        """Rejection should still return a result."""
        from agents.api.routers.query import approve_sql, ApproveRequest

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={
            "answer": "SQL 已被拒绝。",
            "status": "completed",
        })
        mock_build_graph.return_value = mock_graph

        req = ApproveRequest(session_id="s1", approved=False, feedback="too dangerous")
        result = await approve_sql(req)

        assert "拒绝" in result.answer

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_approve_stream_returns_progress_and_result(self, mock_build_graph, mock_callbacks):
        """Streaming approval should emit user-friendly progress and final result."""
        from agents.api.routers.query import approve_sql_stream, ApproveRequest

        interrupt_obj = MagicMock()
        interrupt_obj.value = {
            "sql": "SELECT COALESCE(SUM(x), 0) FROM t;",
            "message": "上次执行结果疑似异常，系统已反思并生成修正后的 SQL。请确认是否执行修正后的 SQL？",
            "reflection": True,
            "approval_type": "sql",
        }

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={"__interrupt__": [interrupt_obj]})
        mock_build_graph.return_value = mock_graph

        req = ApproveRequest(session_id="s1", approved=True)
        response = await approve_sql_stream(req, MagicMock())
        events = []
        async for event in response.body_iterator:
            events.append(event)

        assert any(e.get("event") == "status" and "反思" in e.get("data", "") for e in events)
        result_events = [e for e in events if e.get("event") == "result"]
        assert result_events
        assert "pending_approval" in result_events[0]["data"]
        assert "COALESCE" in result_events[0]["data"]

    @pytest.mark.asyncio
    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    @patch("agents.api.routers.query.build_final_graph")
    async def test_approve_stream_returns_complex_plan_progress(self, mock_build_graph, mock_callbacks):
        """Complex plan approval should emit plan-oriented progress text."""
        from agents.api.routers.query import approve_sql_stream, ApproveRequest

        interrupt_obj = MagicMock()
        interrupt_obj.value = {
            "message": "检测到复杂多表分析问题，已生成执行计划，请确认是否按计划执行：",
            "approval_type": "complex_plan",
        }

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={"__interrupt__": [interrupt_obj]})
        mock_build_graph.return_value = mock_graph

        req = ApproveRequest(session_id="s1", approved=True)
        response = await approve_sql_stream(req, MagicMock())
        events = []
        async for event in response.body_iterator:
            events.append(event)

        assert any(e.get("event") == "status" and "复杂查询计划已生成" in e.get("data", "") for e in events)
        result_events = [e for e in events if e.get("event") == "result"]
        assert result_events
        assert "complex_plan" in result_events[0]["data"]


# ---------------------------------------------------------------------------
# _make_config
# ---------------------------------------------------------------------------

class TestMakeConfig:
    """Test config construction."""

    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    def test_config_has_thread_id(self, mock_callbacks):
        from agents.api.routers.query import _make_config

        config = _make_config("session-123")

        assert config["configurable"]["thread_id"] == "session-123"

    @patch("agents.api.routers.query.get_trace_callbacks", return_value=["handler1"])
    def test_config_has_callbacks(self, mock_callbacks):
        from agents.api.routers.query import _make_config

        config = _make_config("s1")

        assert "callbacks" in config
        assert config["callbacks"] == ["handler1"]

    @patch("agents.api.routers.query.get_trace_callbacks", return_value=[])
    def test_config_no_empty_callbacks(self, mock_callbacks):
        from agents.api.routers.query import _make_config

        config = _make_config("s1")

        assert "callbacks" not in config
