"""Unit tests to verify all module imports work correctly."""

import pytest


class TestConfigImports:
    """Test config module imports."""

    def test_settings_import(self):
        from agents.config.settings import settings, get_settings, Settings
        assert settings is not None
        assert callable(get_settings)
        assert isinstance(settings, Settings)

    def test_settings_rag_mode(self):
        from agents.config.settings import settings
        assert hasattr(settings.rag, 'mode')
        assert settings.rag.mode in ("traditional", "parent")

    def test_settings_rag_parent_params(self):
        from agents.config.settings import settings
        assert hasattr(settings.rag, 'parent_chunk_size')
        assert hasattr(settings.rag, 'parent_chunk_overlap')
        assert hasattr(settings.rag, 'child_chunk_size')
        assert hasattr(settings.rag, 'child_chunk_overlap')


class TestModelImports:
    """Test model module imports."""

    def test_chat_model_import(self):
        from agents.model.chat_model import get_chat_model, init_chat_models, register_chat_model
        assert callable(get_chat_model)
        assert callable(init_chat_models)
        assert callable(register_chat_model)

    def test_embedding_model_import(self):
        from agents.model.embedding_model import get_embedding_model, init_embedding_models, register_embedding_model
        assert callable(get_embedding_model)
        assert callable(init_embedding_models)
        assert callable(register_embedding_model)

    def test_format_tool_import(self):
        from agents.model.format_tool import FormatOutput, create_format_tool
        assert callable(create_format_tool)


class TestRAGImports:
    """Test RAG module imports."""

    def test_indexing_import(self):
        from agents.rag.indexing import (
            load_document,
            split_documents,
            build_indexing_graph,
            build_parent_indexing_graph,
        )
        assert callable(load_document)
        assert callable(split_documents)
        assert callable(build_indexing_graph)
        assert callable(build_parent_indexing_graph)

    def test_retriever_import(self):
        from agents.rag.retriever import HybridRetriever, build_milvus_retriever, build_es_retriever
        assert callable(build_milvus_retriever)
        assert callable(build_es_retriever)

    def test_parent_retriever_import(self):
        from agents.rag.parent_retriever import ParentDocumentRetriever

    def test_reranker_import(self):
        from agents.rag.reranker import CrossEncoderReranker

    def test_query_rewrite_import(self):
        from agents.rag.query_rewrite import rewrite_query
        assert callable(rewrite_query)


class TestFlowImports:
    """Test flow module imports."""

    def test_state_import(self):
        from agents.flow.state import RAGChatState, SQLReactState, AnalystState, FinalGraphState

    def test_rag_chat_import(self):
        from agents.flow.rag_chat import build_rag_chat_graph
        assert callable(build_rag_chat_graph)

    def test_sql_react_import(self):
        from agents.flow.sql_react import build_sql_react_graph
        assert callable(build_sql_react_graph)

    def test_analyst_import(self):
        from agents.flow.analyst import build_analyst_graph
        assert callable(build_analyst_graph)

    def test_dispatcher_import(self):
        from agents.flow.dispatcher import build_final_graph
        assert callable(build_final_graph)


class TestRuntimeImports:
    """Test runtime module imports."""

    def test_tool_catalog_imports(self):
        from agents.runtime.agentscope_runtime import COMPLEX_ANALYSIS_AGENT_PROMPT
        from agents.runtime.agentscope_runtime import AgentScopeRunContext, AgentScopeRuntime
        from agents.runtime.agentscope_runtime import REPORT_AGENT_PROMPT
        from agents.runtime.result import AgentRunResult
        from agents.runtime.shadow_benchmark import ShadowBenchmark, ShadowRunRecord
        from agents.runtime.skill_registry import SkillDefinition, SkillRegistry
        from agents.runtime.tool_catalog import ToolCatalog, ToolProviders
        from agents.runtime.tool_contracts import ToolCallResult, ToolContract

        assert AgentScopeRuntime is not None
        assert AgentScopeRunContext is not None
        assert "complex_analysis_agent" in COMPLEX_ANALYSIS_AGENT_PROMPT
        assert "report_agent" in REPORT_AGENT_PROMPT
        assert AgentRunResult is not None
        assert ShadowBenchmark is not None
        assert ShadowRunRecord is not None
        assert SkillDefinition is not None
        assert SkillRegistry is not None
        assert ToolCatalog is not None
        assert ToolProviders is not None
        assert ToolContract is not None
        assert ToolCallResult is not None


class TestToolImports:
    """Test tool module imports."""

    def test_memory_imports(self):
        from agents.tool.memory.session import Session
        from agents.tool.memory.store import get_session, save_session
        from agents.tool.memory.compressor import compress_session
        from agents.tool.memory.knowledge import extract_knowledge

    def test_storage_imports(self):
        from agents.tool.storage.redis_client import init_redis, close_redis
        from agents.tool.storage.checkpoint import get_checkpointer

    def test_document_imports(self):
        from agents.tool.document.loader import get_loader
        from agents.tool.document.parser import parse_document
        from agents.tool.document.splitter import get_splitter

    def test_sql_tools_imports(self):
        from agents.tool.sql_tools.safety import SQLSafetyChecker

    def test_analyst_tools_imports(self):
        from agents.tool.analyst_tools.parser import parse_sql_result
        from agents.tool.analyst_tools.statistics import compute_statistics
        from agents.tool.analyst_tools.chart import generate_chart_config

    def test_token_counter_import(self):
        from agents.tool.token_counter import TokenCounter


class TestAlgorithmImports:
    """Test algorithm module imports."""

    def test_bm25_import(self):
        from agents.algorithm.bm25 import BM25

    def test_rrf_import(self):
        from agents.algorithm.rrf import reciprocal_rank_fusion
        assert callable(reciprocal_rank_fusion)


class TestAPIImports:
    """Test API module imports."""

    def test_app_import(self):
        from agents.api.app import app
        assert app is not None

    def test_routers_import(self):
        from agents.api.routers import agentscope, chat, rag, query, document

    def test_sse_import(self):
        from agents.api.sse import sse_response
        assert callable(sse_response)
