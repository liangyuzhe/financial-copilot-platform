"""FastAPI 应用入口。"""

from contextlib import asynccontextmanager
import asyncio
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from agents.api.routers import agentscope, chat, rag, query, document, admin, eval
from agents.config.settings import settings

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def _run_startup_check(name: str, func, logger):
    """Run blocking startup checks with a short timeout so UI/API can boot."""
    timeout = float(os.getenv("STARTUP_CHECK_TIMEOUT", "3"))
    try:
        await asyncio.wait_for(
            asyncio.to_thread(func),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("%s startup check timed out after %.1fs; continuing startup", name, timeout)
    except Exception as e:
        logger.warning("%s startup check failed: %s", name, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化所有组件，关闭时清理。"""
    import asyncio
    import logging

    from agents.tool.storage.redis_client import init_redis, close_redis
    from agents.model.chat_model import init_chat_models
    from agents.model.embedding_model import init_embedding_models
    from agents.tool.trace.tracing import init_tracing, close_cozeloop

    logger = logging.getLogger(__name__)

    # 初始化基础设施
    await init_redis()

    # 初始化 Milvus 连接（pymilvus 兼容 patch + 连接验证）
    await _run_startup_check("Milvus", lambda: _init_milvus(logger), logger)

    # 确保 MySQL 表存在
    try:
        from agents.tool.storage.doc_metadata import ensure_doc_metadata_table
        await _run_startup_check("doc_metadata", ensure_doc_metadata_table, logger)
    except Exception as e:
        logger.warning("Failed to schedule doc_metadata table check: %s", e)

    try:
        from agents.tool.storage.intent_rules import ensure_intent_rule_table
        await _run_startup_check("intent_rules", ensure_intent_rule_table, logger)
    except Exception as e:
        logger.warning("Failed to schedule intent_rules table check: %s", e)

    try:
        from agents.tool.storage.query_route_rules import ensure_query_route_rule_table
        await _run_startup_check("query_route_rules", ensure_query_route_rule_table, logger)
    except Exception as e:
        logger.warning("Failed to schedule query_route_rules table check: %s", e)

    # 初始化模型
    init_chat_models()
    init_embedding_models()

    # 注册工具（import 触发 @register）
    import agents.tool.sql_tools  # noqa: F401

    # 初始化链路追踪
    init_tracing()

    # 后台异步：检查领域摘要，按需生成（不阻塞服务启动）
    asyncio.create_task(_ensure_domain_summary(logger))

    # 后台异步：t_semantic_model 全量初始化 + binlog 增量同步
    try:
        from agents.init.schema_sync import start_schema_sync
        asyncio.create_task(start_schema_sync(logger))
    except Exception as e:
        logger.warning("Schema sync init failed: %s", e)

    yield

    # 清理
    close_cozeloop()
    await close_redis()


def _init_milvus(logger):
    """Apply pymilvus compatibility patch and verify Milvus connection."""
    from agents.rag.retriever import _patch_milvus_connections

    _patch_milvus_connections()

    # Verify connection and ensure collection exists
    try:
        from pymilvus import MilvusClient
        uri = f"http://{settings.milvus.addr}"
        client = MilvusClient(uri=uri)
        coll = settings.milvus.collection_name
        if coll in client.list_collections():
            stats = client.get_collection_stats(coll)
            logger.info("Milvus connected: %s has %s rows", coll, stats.get("row_count", "?"))
        else:
            _create_knowledge_base_collection(client, coll, logger)
        client.close()
    except Exception as e:
        logger.warning("Milvus connection check failed: %s", e)


def _create_knowledge_base_collection(client, coll: str, logger):
    """Create the unified knowledge_base collection with all required fields."""
    from pymilvus import CollectionSchema, FieldSchema, DataType

    schema = CollectionSchema(fields=[
        FieldSchema("pk", DataType.VARCHAR, is_primary=True, max_length=65535),
        FieldSchema("text", DataType.VARCHAR, max_length=65535),
        FieldSchema("vector", DataType.FLOAT_VECTOR, dim=1024),
        FieldSchema("source", DataType.VARCHAR, max_length=65535),
        FieldSchema("table_name", DataType.VARCHAR, max_length=65535),
        FieldSchema("doc_id", DataType.VARCHAR, max_length=65535),
        FieldSchema("session_id", DataType.VARCHAR, max_length=255),
    ], enable_dynamic_field=True)

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="vector",
        index_type="IVF_FLAT",
        metric_type="COSINE",
        params={"nlist": 128},
    )

    client.create_collection(
        collection_name=coll,
        schema=schema,
        index_params=index_params,
    )
    client.load_collection(coll)
    logger.info("Created Milvus collection '%s' with schema (pk, text, vector, source, table_name, doc_id)", coll)


async def _ensure_domain_summary(logger):
    """后台任务：如果 domain_summary 为空，从 t_semantic_model 生成领域摘要。"""
    try:
        from agents.tool.storage.domain_summary import (
            ensure_domain_summary_table,
            get_domain_summary,
        )

        await ensure_domain_summary_table()

        existing = await get_domain_summary()
        if existing:
            logger.info("Domain summary cached (%d chars), skipping generation", len(existing))
            return

        logger.info("No domain summary found, generating from semantic model...")
        from agents.rag.domain_summary_builder import generate_domain_summary

        summary = await generate_domain_summary()
        if not summary:
            logger.info("No tables found, skipping domain summary generation")
            return
        logger.info("Domain summary generated (%d chars)", len(summary))
    except Exception as e:
        logger.warning("Domain summary generation failed: %s", e)


app = FastAPI(
    title="Financial Copilot",
    description="Financial Copilot Platform built with LangChain and LangGraph",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 路由注册
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(rag.router, prefix="/api/rag", tags=["rag"])
app.include_router(query.router, prefix="/api/query", tags=["query"])
app.include_router(agentscope.router, prefix="/api/agentscope", tags=["agentscope"])
app.include_router(document.router, prefix="/api/document", tags=["document"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(eval.router, prefix="/api/eval", tags=["eval"])


@app.get("/health")
async def health():
    return {"status": "ok"}


# 静态文件
try:
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
except Exception:
    pass  # static 目录不存在时忽略
