"""Tests for tracing initialization."""

import pytest
from unittest.mock import patch, MagicMock
from langchain_core.callbacks import AsyncCallbackManager, BaseCallbackHandler


class TestLangSmithInit:
    """Test LangSmith tracing initialization."""

    @patch("agents.tool.trace.tracing.settings")
    def test_langsmith_disabled_by_default(self, mock_settings):
        mock_settings.langsmith.tracing = False
        mock_settings.langsmith.api_key = ""

        from agents.tool.trace.tracing import init_langsmith
        # Should not raise
        init_langsmith()

    @patch("agents.tool.trace.tracing.settings")
    def test_langsmith_sets_env_vars(self, mock_settings):
        import os
        mock_settings.langsmith.tracing = True
        mock_settings.langsmith.api_key = "test-key"
        mock_settings.langsmith.url = "https://test.langchain.com"
        mock_settings.langsmith.project = "test-project"

        from agents.tool.trace.tracing import init_langsmith
        init_langsmith()

        assert os.environ.get("LANGCHAIN_API_KEY") == "test-key"
        assert os.environ.get("LANGCHAIN_TRACING_V2") == "true"
        assert os.environ.get("LANGCHAIN_ENDPOINT") == "https://test.langchain.com"
        assert os.environ.get("LANGCHAIN_PROJECT") == "test-project"

        # Cleanup
        for key in [
            "LANGCHAIN_API_KEY", "LANGCHAIN_TRACING_V2",
            "LANGCHAIN_ENDPOINT", "LANGCHAIN_PROJECT",
        ]:
            os.environ.pop(key, None)


class TestCozeLoopInit:
    """Test CozeLoop tracing initialization with JWT OAuth."""

    @patch("agents.tool.trace.tracing.settings")
    def test_cozeloop_disabled_by_default(self, mock_settings):
        mock_settings.cozeloop.tracing = False
        mock_settings.cozeloop.jwt_oauth_client_id = ""

        from agents.tool.trace.tracing import get_cozeloop_handler
        result = get_cozeloop_handler()
        assert result is None

    @patch("agents.tool.trace.tracing.settings")
    def test_cozeloop_returns_none_without_client_id(self, mock_settings):
        mock_settings.cozeloop.tracing = True
        mock_settings.cozeloop.jwt_oauth_client_id = ""

        from agents.tool.trace.tracing import get_cozeloop_handler
        result = get_cozeloop_handler()
        assert result is None

    @patch("agents.tool.trace.tracing.settings")
    def test_cozeloop_returns_none_without_package(self, mock_settings):
        mock_settings.cozeloop.tracing = True
        mock_settings.cozeloop.jwt_oauth_client_id = "test-client-id"
        mock_settings.cozeloop.jwt_oauth_private_key = "test-key"
        mock_settings.cozeloop.jwt_oauth_public_key_id = "test-key-id"
        mock_settings.cozeloop.workspace_id = "test-workspace"
        mock_settings.cozeloop.api_base_url = ""

        import agents.tool.trace.tracing as tracing_mod
        saved_client = tracing_mod._cozeloop_client
        tracing_mod._cozeloop_client = None

        # Mock the import to raise ImportError
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "cozeloop" or name.startswith("cozeloop."):
                raise ImportError("No module named 'cozeloop'")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = mock_import
        try:
            from agents.tool.trace.tracing import get_cozeloop_handler
            result = get_cozeloop_handler()
            assert result is None
        finally:
            builtins.__import__ = real_import
            tracing_mod._cozeloop_client = saved_client

    @patch("agents.tool.trace.tracing.settings")
    def test_cozeloop_sets_env_vars(self, mock_settings):
        import os
        mock_settings.cozeloop.tracing = True
        mock_settings.cozeloop.jwt_oauth_client_id = "test-client-id"
        mock_settings.cozeloop.jwt_oauth_private_key = "test-private-key"
        mock_settings.cozeloop.jwt_oauth_public_key_id = "test-public-id"
        mock_settings.cozeloop.workspace_id = "test-workspace"
        mock_settings.cozeloop.api_base_url = ""

        from agents.tool.trace.tracing import _set_cozeloop_env
        _set_cozeloop_env()

        assert os.environ.get("COZELOOP_WORKSPACE_ID") == "test-workspace"
        assert os.environ.get("COZELOOP_JWT_OAUTH_CLIENT_ID") == "test-client-id"
        assert os.environ.get("COZELOOP_JWT_OAUTH_PRIVATE_KEY") == "test-private-key"
        assert os.environ.get("COZELOOP_JWT_OAUTH_PUBLIC_KEY_ID") == "test-public-id"

        # Cleanup
        for key in [
            "COZELOOP_WORKSPACE_ID", "COZELOOP_JWT_OAUTH_CLIENT_ID",
            "COZELOOP_JWT_OAUTH_PRIVATE_KEY", "COZELOOP_JWT_OAUTH_PUBLIC_KEY_ID",
            "COZELOOP_API_BASE_URL",
        ]:
            os.environ.pop(key, None)


class TestTraceCallbacks:
    """Test get_trace_callbacks."""

    @patch("agents.tool.trace.tracing.settings")
    def test_returns_empty_list_when_all_disabled(self, mock_settings):
        mock_settings.langsmith.tracing = False
        mock_settings.langsmith.api_key = ""
        mock_settings.cozeloop.tracing = False
        mock_settings.cozeloop.jwt_oauth_client_id = ""

        from agents.tool.trace.tracing import get_trace_callbacks
        callbacks = get_trace_callbacks()
        assert callbacks == []

    @patch("agents.tool.trace.tracing.settings")
    def test_returns_langsmith_handler_when_enabled(self, mock_settings):
        mock_settings.langsmith.tracing = True
        mock_settings.langsmith.api_key = "test-key"
        mock_settings.langsmith.url = "https://test.langchain.com"
        mock_settings.langsmith.project = "test-project"
        mock_settings.cozeloop.tracing = False
        mock_settings.cozeloop.jwt_oauth_client_id = ""

        from agents.tool.trace.tracing import get_trace_callbacks
        callbacks = get_trace_callbacks()
        assert len(callbacks) == 1
        assert type(callbacks[0]).__name__ == "LangChainTracer"


class _RecordingHandler(BaseCallbackHandler):
    def __init__(self):
        self.events = []

    def on_retriever_start(self, serialized, query, **kwargs):
        self.events.append(("retriever_start", serialized.get("name"), query))

    def on_retriever_end(self, documents, **kwargs):
        self.events.append(("retriever_end", len(documents)))

    def on_tool_start(self, serialized, input_str, **kwargs):
        self.events.append(("tool_start", serialized.get("name"), input_str))

    def on_tool_end(self, output, **kwargs):
        self.events.append(("tool_end", output))


class _AsyncRecordingHandler(BaseCallbackHandler):
    def __init__(self):
        self.events = []

    async def on_tool_start(self, serialized, input_str, **kwargs):
        self.events.append(("tool_start", serialized.get("name"), input_str, kwargs.get("metadata")))

    async def on_tool_end(self, output, **kwargs):
        self.events.append(("tool_end", output))


class TestTraceHelpers:
    """Test helper spans for non-Runnable operations."""

    def test_child_trace_config_extracts_callbacks(self):
        from agents.tool.trace.tracing import child_trace_config

        cfg = child_trace_config(
            {"callbacks": ["handler"]},
            "node.llm",
            tags=["llm"],
            metadata={"node": "node"},
        )

        assert cfg["callbacks"] == ["handler"]
        assert cfg["run_name"] == "node.llm"
        assert cfg["tags"] == ["llm"]
        assert cfg["metadata"] == {"node": "node"}

    def test_traced_retriever_call_emits_retriever_events(self):
        from agents.tool.trace.tracing import traced_retriever_call

        handler = _RecordingHandler()
        result = traced_retriever_call(
            "milvus.vector_search.business_knowledge",
            "去年亏损",
            [handler],
            lambda: [MagicMock()],
        )

        assert len(result) == 1
        assert handler.events[0] == (
            "retriever_start",
            "milvus.vector_search.business_knowledge",
            "去年亏损",
        )
        assert handler.events[-1] == ("retriever_end", 1)

    def test_traced_tool_call_emits_tool_events(self):
        from agents.tool.trace.tracing import traced_tool_call

        handler = _RecordingHandler()
        result = traced_tool_call(
            "schema.load_full_table_metadata",
            "query",
            [handler],
            lambda: [{"table_name": "t_account"}],
        )

        assert result == [{"table_name": "t_account"}]
        assert handler.events[0] == ("tool_start", "schema.load_full_table_metadata", "query")
        assert handler.events[-1][0] == "tool_end"
        assert handler.events[-1][1] == [{"table_name": "t_account"}]

    @pytest.mark.asyncio
    async def test_traced_async_tool_call_emits_async_tool_events(self):
        from agents.tool.trace.tracing import traced_async_tool_call

        async def load_domain():
            return "领域摘要"

        handler = _AsyncRecordingHandler()
        manager = AsyncCallbackManager.configure(inheritable_callbacks=[handler])

        result = await traced_async_tool_call(
            "domain_summary.load",
            "去年亏损",
            manager,
            load_domain,
            metadata={"storage": "mysql"},
        )

        assert result == "领域摘要"
        assert handler.events[0][:3] == (
            "tool_start",
            "domain_summary.load",
            "去年亏损",
        )
        assert handler.events[0][3]["storage"] == "mysql"
        assert handler.events[-1][0] == "tool_end"
        assert handler.events[-1][1] == "领域摘要"

    @pytest.mark.asyncio
    async def test_traced_async_tool_call_deduplicates_manager_handlers(self):
        from agents.tool.trace.tracing import traced_async_tool_call

        class CountingHandler(BaseCallbackHandler):
            def __init__(self):
                self.events = []

            async def on_tool_start(self, serialized, input_str, **kwargs):
                self.events.append(("start", serialized.get("name"), kwargs["run_id"]))

            async def on_tool_end(self, output, **kwargs):
                self.events.append(("end", output, kwargs["run_id"]))

        async def load_domain():
            return "领域摘要"

        handler = CountingHandler()
        manager = AsyncCallbackManager.configure(
            inheritable_callbacks=[handler],
            local_callbacks=[handler],
        )

        result = await traced_async_tool_call(
            "domain_summary.load",
            "去年亏损",
            manager,
            load_domain,
            metadata={"storage": "mysql"},
        )

        assert result == "领域摘要"
        assert [event[0] for event in handler.events] == ["start", "end"]
        assert handler.events[0][2] == handler.events[1][2]
