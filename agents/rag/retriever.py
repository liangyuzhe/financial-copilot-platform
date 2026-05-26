"""Hybrid retrieval: Milvus (dense) + Elasticsearch BM25 (sparse) + RRF fusion + rerank."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_elasticsearch import ElasticsearchStore
from langchain_milvus import Milvus

from agents.config.settings import settings
from agents.algorithm.rrf import reciprocal_rank_fusion
from agents.rag.reranker import CrossEncoderReranker
from agents.tool.trace.tracing import traced_retriever_call, traced_tool_call

try:
    from elasticsearch import Elasticsearch
except ImportError:
    Elasticsearch = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# pymilvus compatibility patch
# ---------------------------------------------------------------------------

_milvus_patched = False


def _patch_milvus_connections() -> None:
    """Register MilvusClient connections in the global pymilvus registry.

    langchain-milvus 0.3.x creates a MilvusClient internally, but pymilvus
    2.6.x's MilvusClient no longer registers its connection in the global
    ``pymilvus.connections`` registry.  ``Collection(alias=...)`` then fails
    with *ConnectionNotExistException*.  This one-time patch wraps
    ``MilvusClient.__init__`` to auto-register the handler.
    """
    global _milvus_patched
    if _milvus_patched:
        return

    from pymilvus import MilvusClient, connections

    _orig_init = MilvusClient.__init__

    def _wrapped_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        alias = self._using
        if not connections.has_connection(alias):
            connections._alias_handlers[alias] = self._handler

    MilvusClient.__init__ = _wrapped_init
    _milvus_patched = True


# ---------------------------------------------------------------------------
# Embedding helper (shared with indexing)
# ---------------------------------------------------------------------------

def _get_embeddings():
    """Return an embedding model instance based on the configured provider."""
    provider = settings.embedding_model_type
    if provider == "ark":
        from langchain_community.embeddings import VolcengineEmbeddings
        return VolcengineEmbeddings(
            ark_api_key=settings.ark.key,
            model=settings.ark.embedding_model,
        )
    if provider == "openai":
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(
            openai_api_key=settings.openai.key,
            model=settings.openai.embedding_model,
        )
    if provider == "qwen":
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(
            openai_api_key=settings.qwen.key,
            openai_api_base=settings.qwen.base_url,
            model=settings.qwen.embedding_model,
            tiktoken_enabled=False,
            check_embedding_ctx_length=False,
            chunk_size=10,
        )
    raise ValueError(f"Unsupported embedding_model_type: {provider!r}")


# ---------------------------------------------------------------------------
# Individual retriever builders
# ---------------------------------------------------------------------------

def build_milvus_retriever(
    milvus_uri: str | None = None,
    collection: str | None = None,
    search_kwargs: dict | None = None,
    source_filter: str | None = None,
    session_id_filter: str | None = None,
) -> BaseRetriever:
    """Build a dense vector retriever backed by Milvus.

    Parameters
    ----------
    source_filter:
        If set, add ``source == "<value>"`` filter to Milvus search.
    session_id_filter:
        If set, add ``session_id == "<value>"`` filter (combined with source_filter).
    """
    uri = milvus_uri or f"http://{settings.milvus.addr}"
    coll = collection or settings.milvus.collection_name
    embeddings = _get_embeddings()

    store = Milvus(
        embedding_function=embeddings,
        connection_args={"uri": uri},
        collection_name=coll,
    )
    kwargs = search_kwargs or {"search_type": "similarity", "k": 20}
    # 构建过滤表达式
    filters = []
    if source_filter:
        filters.append(f'source == "{source_filter}"')
    if session_id_filter:
        filters.append(f'session_id == "{session_id_filter}"')
    if filters:
        kwargs["expr"] = " && ".join(filters)
    return store.as_retriever(**kwargs)


def build_es_retriever(
    es_url: str | None = None,
    index: str | None = None,
    search_kwargs: dict | None = None,
) -> BaseRetriever:
    """Build a sparse (BM25) retriever backed by Elasticsearch.

    Uses ``BM25Strategy`` so that queries are matched purely by keyword
    relevance -- no dense vector search.

    Parameters
    ----------
    es_url:
        Elasticsearch connection URL.  Falls back to ``settings.es.address``.
    index:
        Elasticsearch index name.  Defaults to ``settings.es.index``.
    search_kwargs:
        Extra keyword arguments forwarded to ``as_retriever``.
    """
    from langchain_elasticsearch import BM25Strategy

    url = es_url or settings.es.address
    idx = index or settings.es.index
    embeddings = _get_embeddings()

    store = ElasticsearchStore(
        es_url=url,
        index_name=idx,
        embedding=embeddings,
        strategy=BM25Strategy(),
    )
    kwargs = search_kwargs or {"search_type": "similarity", "k": 20}
    return store.as_retriever(**kwargs)


# ---------------------------------------------------------------------------
# Hybrid retriever
# ---------------------------------------------------------------------------

class HybridRetriever:
    """Retrieve from Milvus (dense) and Elasticsearch BM25 (sparse) in
    parallel, fuse results with Reciprocal Rank Fusion, then optionally
    rerank with a Cross-Encoder model.

    Parameters
    ----------
    milvus_uri:
        Milvus connection URI.
    milvus_collection:
        Milvus collection name.  Defaults to ``settings.milvus.collection_name``.
    es_url:
        Elasticsearch connection URL.
    es_index:
        Elasticsearch index name.  Defaults to ``settings.es.index``.
    retrieve_k:
        Number of candidates to fetch from each retriever (Milvus / ES).
    reranker_model:
        Sentence-Transformers Cross-Encoder model name for reranking.
        Set to *None* to skip reranking entirely (faster).
    reranker_top_k:
        Default number of results to keep after reranking.
    """

    # Class-level cache for reranker (heavy to load)
    _reranker_cache: dict[str, CrossEncoderReranker] = {}

    def __init__(
        self,
        milvus_uri: str | None = None,
        milvus_collection: str | None = None,
        es_url: str | None = None,
        es_index: str | None = None,
        retrieve_k: int = 5,
        reranker_model: str | None = "BAAI/bge-reranker-v2-m3",
        reranker_top_k: int = 5,
        rerank_threshold: float = 0.1,
        source_filter: str | None = None,
        session_id_filter: str | None = None,
    ) -> None:
        search_kwargs = {"search_type": "similarity", "k": retrieve_k}
        self._milvus = build_milvus_retriever(
            milvus_uri=milvus_uri,
            collection=milvus_collection,
            search_kwargs=search_kwargs,
            source_filter=source_filter,
            session_id_filter=session_id_filter,
        )
        self._es = build_es_retriever(
            es_url=es_url,
            index=es_index,
            search_kwargs=search_kwargs,
        )
        # Cache the reranker to avoid reloading the model on every request
        self._reranker = None
        if reranker_model:
            if reranker_model not in self._reranker_cache:
                self._reranker_cache[reranker_model] = CrossEncoderReranker(
                    model_name=reranker_model
                )
            self._reranker = self._reranker_cache[reranker_model]
        self._reranker_top_k = reranker_top_k
        self._rerank_threshold = rerank_threshold

    # -- internal helpers ---------------------------------------------------

    def _retrieve_milvus(self, query: str, callbacks=None) -> list[Document]:
        import time
        t0 = time.monotonic()
        try:
            docs = self._milvus.invoke(query, config={"callbacks": callbacks or []})
            for d in docs:
                d.metadata["retriever_source"] = "milvus"
            elapsed = time.monotonic() - t0
            logger.info("Milvus retrieve: %d docs in %.2fs", len(docs), elapsed)
            return docs
        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.warning("Milvus retrieve failed after %.2fs: %s", elapsed, e)
            return []

    def _retrieve_es(self, query: str, callbacks=None) -> list[Document]:
        import time
        t0 = time.monotonic()
        try:
            docs = self._es.invoke(query, config={"callbacks": callbacks or []})
            for d in docs:
                d.metadata["retriever_source"] = "es"
            elapsed = time.monotonic() - t0
            logger.info("ES retrieve: %d docs in %.2fs", len(docs), elapsed)
            return docs
        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.warning("ES retrieve failed after %.2fs: %s", elapsed, e)
            return []

    # -- public API ---------------------------------------------------------

    def retrieve(self, query: str, top_k: int | None = None, callbacks=None) -> list[Document]:
        """Run hybrid retrieval and return the top *top_k* documents.

        Steps:
        1. Retrieve from Milvus (dense) and ES BM25 (sparse) sequentially.
        2. Fuse the two ranked lists with Reciprocal Rank Fusion (RRF).
        3. (Optional) Rerank the fused list with a Cross-Encoder.
        4. Return the top *top_k* results.

        Parameters
        ----------
        query:
            The search query string.
        top_k:
            Number of final results to return.  Defaults to the value passed
            at construction time (``reranker_top_k``).
        callbacks:
            LangChain callback handlers. Propagated to child retriever calls
            so each step appears as a separate span in LangSmith.
        """
        k = top_k or self._reranker_top_k

        # 1. Parallel retrieval — pass callbacks explicitly so worker threads
        #    create child spans in LangSmith (contextvars don't cross threads).
        import time as _time
        t0 = _time.monotonic()
        doc_lists: list[list[Document]] = [None, None]  # type: ignore[list-item]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(self._retrieve_milvus, query, callbacks=callbacks): 0,
                pool.submit(self._retrieve_es, query, callbacks=callbacks): 1,
            }
            for future in as_completed(futures, timeout=30):
                idx = futures[future]
                try:
                    doc_lists[idx] = future.result(timeout=5)
                except Exception as e:
                    logger.warning("Retriever[%d] exception: %s", idx, e)
                    doc_lists[idx] = []

        for idx, docs in enumerate(doc_lists):
            if docs is None:
                logger.warning("Retriever[%d] did not complete (timeout)", idx)
                doc_lists[idx] = []

        elapsed_total = _time.monotonic() - t0
        logger.info("Hybrid retrieve total: %.2fs", elapsed_total)

        # 2. RRF fusion
        milvus_count = len(doc_lists[0]) if doc_lists[0] else 0
        es_count = len(doc_lists[1]) if doc_lists[1] else 0
        logger.info(
            "Hybrid retrieve: milvus=%d, es=%d docs",
            milvus_count, es_count,
        )
        fused = reciprocal_rank_fusion(doc_lists, k=60)

        # 3. Cross-Encoder rerank (optional) + threshold filter
        if self._reranker:
            import time as _t
            _rt0 = _t.monotonic()
            reranked = self._reranker.rerank(query, fused, top_k=k)
            _rt = _t.monotonic() - _rt0
            logger.info("Rerank: %d → %d docs in %.2fs", len(fused), len(reranked), _rt)
            results = [
                d for d in reranked
                if d.metadata.get("rerank_score", 0) >= self._rerank_threshold
            ]
            logger.info(
                "After rerank (threshold=%.2f): %d docs kept — %s",
                self._rerank_threshold,
                len(results),
                [
                    (d.metadata.get("table_name", "?"),
                     d.metadata.get("retriever_source", "?"),
                     round(d.metadata.get("rerank_score", 0), 4))
                    for d in results
                ],
            )
            return results

        return fused[:k]


# ---------------------------------------------------------------------------
# Vector-only retriever (for schema retrieval)
# ---------------------------------------------------------------------------

class VectorOnlyRetriever:
    """Retrieve using only Milvus dense vector search.

    Evaluation shows this outperforms hybrid+rerank for schema retrieval
    (MRR=0.97 vs 0.89, Recall@5=0.94 vs 0.81, 4x faster).
    """

    def __init__(
        self,
        milvus_uri: str | None = None,
        milvus_collection: str | None = None,
        retrieve_k: int = 10,
        source_filter: str | None = None,
    ) -> None:
        self._retriever = build_milvus_retriever(
            milvus_uri=milvus_uri,
            collection=milvus_collection,
            search_kwargs={"search_type": "similarity", "k": retrieve_k},
            source_filter=source_filter,
        )

    def retrieve(self, query: str, top_k: int | None = None, callbacks=None) -> list[Document]:
        docs = self._retriever.invoke(query, config={"callbacks": callbacks or []})
        if top_k:
            docs = docs[:top_k]
        return docs


# Module-level singletons to avoid reconnecting Milvus/ES on every request
_retriever_instance: HybridRetriever | None = None


def get_hybrid_retriever(
    milvus_collection: str | None = None,
    es_index: str | None = None,
    retrieve_k: int = 5,
    reranker_model: str | None = None,
    reranker_top_k: int | None = None,
    rerank_threshold: float | None = None,
    source_filter: str | None = None,
    session_id_filter: str | None = None,
) -> HybridRetriever:
    """Return a HybridRetriever.

    Uses a singleton for shared retrievers (no session_id_filter).
    Creates a new instance per call for session-scoped retrievers.
    """
    global _retriever_instance

    # session-scoped retrievers: always create new instance
    if session_id_filter:
        if reranker_model is None:
            rm = settings.rag.reranker_model
            reranker_model = rm if rm else None
        if reranker_top_k is None:
            reranker_top_k = settings.rag.reranker_top_k
        if rerank_threshold is None:
            rerank_threshold = settings.rag.rerank_threshold
        return HybridRetriever(
            milvus_collection=milvus_collection,
            es_index=es_index,
            retrieve_k=retrieve_k,
            reranker_model=reranker_model,
            reranker_top_k=reranker_top_k,
            rerank_threshold=rerank_threshold,
            source_filter=source_filter,
            session_id_filter=session_id_filter,
        )

    # Shared retrievers: use singleton
    if _retriever_instance is None:
        if reranker_model is None:
            rm = settings.rag.reranker_model
            reranker_model = rm if rm else None
        if reranker_top_k is None:
            reranker_top_k = settings.rag.reranker_top_k
        if rerank_threshold is None:
            rerank_threshold = settings.rag.rerank_threshold
        _retriever_instance = HybridRetriever(
            milvus_collection=milvus_collection,
            es_index=es_index,
            retrieve_k=retrieve_k,
            reranker_model=reranker_model,
            reranker_top_k=reranker_top_k,
            rerank_threshold=rerank_threshold,
            source_filter=source_filter,
        )
    return _retriever_instance


_vector_only_instance: VectorOnlyRetriever | None = None


def get_vector_only_retriever(
    milvus_collection: str | None = None,
    retrieve_k: int = 10,
    source_filter: str | None = None,
) -> VectorOnlyRetriever:
    """Return a cached VectorOnlyRetriever singleton.

    Use this for schema retrieval where evaluation shows vector-only
    outperforms hybrid+rerank.
    """
    global _vector_only_instance
    if _vector_only_instance is None:
        _vector_only_instance = VectorOnlyRetriever(
            milvus_collection=milvus_collection,
            retrieve_k=retrieve_k,
            source_filter=source_filter,
        )
    return _vector_only_instance


# ---------------------------------------------------------------------------
# Sync Redis client (for schema cache)
# ---------------------------------------------------------------------------

_REDIS_KEY_TABLE_META = "schema:table_metadata"
_REDIS_KEY_SEMANTIC_PREFIX = "schema:semantic_model:"
_sync_redis_client = None


def _get_sync_redis():
    """获取同步 Redis 客户端（单例）。失败返回 None。"""
    global _sync_redis_client
    if _sync_redis_client is not None:
        return _sync_redis_client
    try:
        import redis as _redis_mod
        addr = settings.redis.addr
        host, port = addr.rsplit(":", 1) if ":" in addr else (addr, "6379")
        _sync_redis_client = _redis_mod.Redis(
            host=host, port=int(port), db=settings.redis.db,
            password=settings.redis.password or None,
            decode_responses=True, socket_timeout=2,
        )
        _sync_redis_client.ping()
        logger.info("Sync Redis client connected for schema cache")
    except Exception as e:
        logger.debug("Sync Redis not available: %s", e)
        _sync_redis_client = None
    return _sync_redis_client


# ---------------------------------------------------------------------------
# Metadata-based schema retrieval (for SQL React)
# ---------------------------------------------------------------------------

def _load_table_metadata_from_mysql() -> list[dict]:
    """从 MySQL information_schema 加载表元数据。"""
    import pymysql

    try:
        conn = pymysql.connect(
            host=settings.mysql.host,
            port=settings.mysql.port,
            user=settings.mysql.username,
            password=settings.mysql.password,
            database=settings.mysql.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, table_comment "
                "FROM information_schema.tables "
                "WHERE table_schema = DATABASE() "
                "ORDER BY table_name"
            )
            rows = list(cur.fetchall())
        conn.close()
        # information_schema returns UPPERCASE keys (TABLE_NAME, TABLE_COMMENT)
        result = [
            {"table_name": r.get("TABLE_NAME") or r.get("table_name", ""),
             "table_comment": r.get("TABLE_COMMENT") or r.get("table_comment", "")}
            for r in rows if r.get("TABLE_NAME") or r.get("table_name")
        ]
        return result
    except Exception as e:
        logger.warning("_load_table_metadata_from_mysql failed: %s", e)
        return []


def load_full_table_metadata() -> list[dict]:
    """Load table_name + table_comment. Redis → MySQL fallback → 回填 Redis。"""
    r = _get_sync_redis()
    if r:
        try:
            cached = r.get(_REDIS_KEY_TABLE_META)
            if cached:
                result = json.loads(cached)
                logger.info("Loaded %d table metadata from Redis", len(result))
                return result
        except Exception:
            pass

    result = _load_table_metadata_from_mysql()
    if r and result:
        try:
            r.set(_REDIS_KEY_TABLE_META, json.dumps(result, ensure_ascii=False))
        except Exception:
            pass
    logger.info("Loaded %d table metadata from MySQL", len(result))
    return result


def _milvus_vector_search(query: str, source: str, top_k: int, callbacks=None) -> list[Document]:
    """Vector search in Milvus with source filter."""
    return traced_retriever_call(
        f"milvus.vector_search.{source}",
        query,
        callbacks,
        lambda: _milvus_vector_search_untraced(query, source, top_k),
        metadata={"vector_db": "milvus", "source": source, "top_k": top_k},
    )


def _milvus_vector_search_untraced(query: str, source: str, top_k: int) -> list[Document]:
    """Vector search in Milvus with source filter."""
    from pymilvus import MilvusClient

    embeddings = _get_embeddings()
    uri = f"http://{settings.milvus.addr}"
    client = MilvusClient(uri=uri)
    try:
        vector = embeddings.embed_query(query)
        results = client.search(
            collection_name=settings.milvus.collection_name,
            data=[vector],
            limit=top_k,
            filter=f'source == "{source}"',
            output_fields=["text", "doc_id"],
        )
        docs = []
        for hit in results[0]:
            entity = hit.get("entity", {})
            docs.append(Document(
                page_content=entity.get("text", ""),
                metadata={
                    "source": source,
                    "doc_id": entity.get("doc_id", ""),
                    "score": hit.get("distance", 0),
                    "retriever_source": "milvus",
                },
            ))
        return docs
    except Exception as e:
        logger.warning("Milvus vector search failed for source=%s: %s", source, e)
        return []
    finally:
        client.close()


def _es_bm25_search(query: str, source: str, top_k: int = 10, callbacks=None) -> list[Document]:
    return traced_retriever_call(
        f"es.bm25_search.{source}",
        query,
        callbacks,
        lambda: _es_bm25_search_untraced(query, source, top_k),
        metadata={"search_engine": "elasticsearch", "source": source, "top_k": top_k},
    )


def _es_bm25_search_untraced(query: str, source: str, top_k: int = 10) -> list[Document]:
    """BM25 keyword search in Elasticsearch with source filter.

    Uses raw ES client to match the indexing format from schema_indexer and seed scripts.
    """
    if Elasticsearch is None:
        return []
    try:
        es_url = settings.es.address if settings.es.address.startswith("http") else f"http://{settings.es.address}"
        es = Elasticsearch(es_url)
        body = {
            "size": top_k,
            "query": {
                "bool": {
                    "must": [
                        {"match": {"text": query}},
                    ],
                    "filter": [
                        {"term": {"metadata.source.keyword": source}},
                    ],
                }
            },
        }
        resp = es.search(index=settings.es.index, body=body)
        docs = []
        for hit in resp["hits"]["hits"]:
            src = hit["_source"]
            docs.append(Document(
                page_content=src.get("text", ""),
                metadata={
                    "source": source,
                    "doc_id": src.get("metadata", {}).get("doc_id", hit["_id"]),
                    "score": hit["_score"],
                    "retriever_source": "es",
                },
            ))
        return docs
    except Exception as e:
        logger.warning("ES BM25 search failed for source=%s: %s", source, e)
        return []


def _filter_has_sql(docs: list[Document]) -> list[Document]:
    """过滤：agent_knowledge 必须包含 SQL 语句才保留。"""
    sql_keywords = ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")
    result = []
    for doc in docs:
        content_upper = doc.page_content.upper()
        if any(kw in content_upper for kw in sql_keywords):
            result.append(doc)
        else:
            logger.debug("Filtered out agent_knowledge (no SQL): %s", doc.page_content[:80])
    return result


def _filter_has_business_term(docs: list[Document]) -> list[Document]:
    """过滤：business_knowledge 必须包含公式/定义/术语关系才保留。"""
    formula_indicators = ("=", "/", "*", "SUM", "COUNT", "AVG", "公式", "定义", "计算", "比率", "率", "总额", "合计")
    result = []
    for doc in docs:
        content = doc.page_content
        if any(indicator in content for indicator in formula_indicators):
            result.append(doc)
        else:
            logger.debug("Filtered out business_knowledge (no formula/term): %s", content[:80])
    return result


def _business_knowledge_content(row: dict) -> str:
    """Render one t_business_knowledge row as retriever evidence text."""
    content = f"术语: {row.get('term', '')}\n公式: {row.get('formula', '')}"
    if row.get("synonyms"):
        content += f"\n同义词: {row['synonyms']}"
    if row.get("related_tables"):
        content += f"\n关联表: {row['related_tables']}"
    return content


def _load_business_knowledge_from_mysql() -> list[dict]:
    """Load business terms from MySQL for lexical fallback recall."""
    import pymysql

    try:
        conn = pymysql.connect(
            host=settings.mysql.host,
            port=settings.mysql.port,
            user=settings.mysql.username,
            password=settings.mysql.password,
            database=settings.mysql.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT term, formula, synonyms, related_tables FROM t_business_knowledge")
            rows = list(cur.fetchall())
        conn.close()
        return rows
    except Exception as e:
        logger.warning("Load business knowledge from MySQL failed: %s", e)
        return []


def _split_synonyms(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = value.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _lexical_business_knowledge_search(query: str, top_k: int, callbacks=None) -> list[Document]:
    """Fallback business recall by matching query against configured term/synonyms."""
    return traced_tool_call(
        "mysql.lexical_business_knowledge_search",
        query,
        callbacks,
        lambda: _lexical_business_knowledge_search_untraced(query, top_k),
        metadata={"storage": "mysql", "source": "business_knowledge", "top_k": top_k},
    )


def _lexical_business_knowledge_search_untraced(query: str, top_k: int) -> list[Document]:
    """Fallback business recall by matching query against configured term/synonyms."""
    if not query:
        return []

    docs = []
    for row in _load_business_knowledge_from_mysql():
        term = (row.get("term") or "").strip()
        synonyms = _split_synonyms(row.get("synonyms"))
        matched = []

        if term and (term in query or query in term):
            matched.append(term)
        for synonym in synonyms:
            if synonym and (synonym in query or query in synonym):
                matched.append(synonym)

        if not matched:
            continue

        score = 1.0 if term in matched else 0.8
        docs.append(Document(
            page_content=_business_knowledge_content(row),
            metadata={
                "source": "business_knowledge",
                "doc_id": f"bk_{term}",
                "score": score,
                "retriever_source": "mysql_lexical",
                "matched_terms": matched,
            },
        ))

    docs.sort(key=lambda d: (d.metadata.get("score", 0), len(d.metadata.get("matched_terms", []))), reverse=True)
    return docs[:top_k]


def recall_business_knowledge(query: str, top_k: int = 5, callbacks=None) -> list[Document]:
    """混合检索业务知识：向量 + BM25 + RRF，过滤无公式/术语的结果。"""
    # 并行：向量检索 + BM25
    vector_docs = _milvus_vector_search(query, "business_knowledge", top_k, callbacks=callbacks)
    es_docs = _es_bm25_search(query, "business_knowledge", top_k, callbacks=callbacks)

    # RRF 融合
    if vector_docs and es_docs:
        fused = reciprocal_rank_fusion([vector_docs, es_docs], k=60)
    elif vector_docs:
        fused = vector_docs
    else:
        fused = es_docs

    # 质量过滤：必须包含公式/术语。MySQL 中的精确术语/同义词命中是治理资产，
    # 不应因为向量/ES 已经占满 topK 而被挤掉。
    filtered = _filter_has_business_term(fused)
    lexical_docs = _lexical_business_knowledge_search(query, top_k, callbacks=callbacks)
    if lexical_docs:
        merged: list[Document] = []
        seen_ids: set[str] = set()
        for doc in [*lexical_docs, *filtered]:
            doc_id = str(doc.metadata.get("doc_id") or doc.page_content)
            if doc_id in seen_ids:
                continue
            seen_ids.add(doc_id)
            merged.append(doc)
        filtered = merged

    logger.info(
        "Business knowledge recall: vector=%d, es=%d, fused=%d, filtered=%d",
        len(vector_docs), len(es_docs), len(fused), len(filtered),
    )
    return filtered[:top_k]


def recall_agent_knowledge(query: str, top_k: int = 3, callbacks=None) -> list[Document]:
    """混合检索智能体知识：向量 + BM25 + RRF，过滤无 SQL 的结果。"""
    # 并行：向量检索 + BM25
    vector_docs = _milvus_vector_search(query, "agent_knowledge", top_k, callbacks=callbacks)
    es_docs = _es_bm25_search(query, "agent_knowledge", top_k, callbacks=callbacks)

    # RRF 融合
    if vector_docs and es_docs:
        fused = reciprocal_rank_fusion([vector_docs, es_docs], k=60)
    elif vector_docs:
        fused = vector_docs
    else:
        fused = es_docs

    # 质量过滤：必须包含 SQL
    filtered = _filter_has_sql(fused)

    logger.info(
        "Agent knowledge recall: vector=%d, es=%d, fused=%d, filtered=%d",
        len(vector_docs), len(es_docs), len(fused), len(filtered),
    )
    return filtered[:top_k]


def _load_semantic_model_from_mysql(table_names: list[str]) -> dict[str, dict[str, dict]]:
    """从 MySQL t_semantic_model 加载指定表的语义模型。"""
    if not table_names:
        return {}

    import pymysql

    try:
        conn = pymysql.connect(
            host=settings.mysql.host,
            port=settings.mysql.port,
            user=settings.mysql.username,
            password=settings.mysql.password,
            database=settings.mysql.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
        placeholders = ", ".join(["%s"] * len(table_names))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT table_name, column_name, column_type, column_comment, "
                f"is_pk, is_fk, ref_table, ref_column, "
                f"business_name, synonyms, business_description "
                f"FROM t_semantic_model WHERE table_name IN ({placeholders})",
                table_names,
            )
            rows = cur.fetchall()
        conn.close()

        result: dict[str, dict[str, dict]] = {}
        for row in rows:
            tbl = row["table_name"]
            col = row["column_name"]
            result.setdefault(tbl, {})[col] = row
        return result
    except Exception as e:
        logger.warning("_load_semantic_model_from_mysql failed: %s", e)
        return {}


def get_semantic_model_by_tables(table_names: list[str]) -> dict[str, dict[str, dict]]:
    """Load semantic model. Redis per-table → MySQL fallback → 回填 Redis。"""
    if not table_names:
        return {}

    r = _get_sync_redis()
    result: dict[str, dict[str, dict]] = {}
    missing = list(table_names)

    # Redis pipeline 批量获取
    if r:
        try:
            pipe = r.pipeline()
            for t in table_names:
                pipe.get(f"{_REDIS_KEY_SEMANTIC_PREFIX}{t}")
            values = pipe.execute()
            for t, v in zip(table_names, values):
                if v:
                    result[t] = json.loads(v)
                    missing.remove(t)
            if result:
                logger.info("Loaded %d tables from Redis cache", len(result))
        except Exception:
            pass

    # MySQL 补全缺失的表
    if missing:
        mysql_result = _load_semantic_model_from_mysql(missing)
        result.update(mysql_result)
        # 回填 Redis
        if r and mysql_result:
            try:
                pipe = r.pipeline()
                for t, cols in mysql_result.items():
                    pipe.set(f"{_REDIS_KEY_SEMANTIC_PREFIX}{t}", json.dumps(cols, ensure_ascii=False))
                pipe.execute()
            except Exception:
                pass

    logger.info("Semantic model: %d tables, %d entries", len(result),
                sum(len(v) for v in result.values()))
    return result


def get_table_relationships(table_names: list[str]) -> list[dict]:
    """获取表之间的外键关系。

    Returns: [{"from_table": "t_order", "from_column": "user_id",
               "to_table": "t_user", "to_column": "id"}, ...]
    """
    if not table_names:
        return []

    import pymysql

    try:
        conn = pymysql.connect(
            host=settings.mysql.host,
            port=settings.mysql.port,
            user=settings.mysql.username,
            password=settings.mysql.password,
            database=settings.mysql.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
        # 查找涉及这些表的外键关系（双向）
        placeholders = ", ".join(["%s"] * len(table_names))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT table_name AS from_table, column_name AS from_column, "
                f"referenced_table_name AS to_table, referenced_column_name AS to_column "
                f"FROM information_schema.key_column_usage "
                f"WHERE table_schema = DATABASE() "
                f"AND referenced_table_name IS NOT NULL "
                f"AND (table_name IN ({placeholders}) OR referenced_table_name IN ({placeholders}))",
                table_names + table_names,
            )
            rows = list(cur.fetchall())
            try:
                cur.execute(
                    f"SELECT table_name AS from_table, column_name AS from_column, "
                    f"ref_table AS to_table, ref_column AS to_column "
                    f"FROM t_semantic_model "
                    f"WHERE is_fk = 1 "
                    f"AND ref_table IS NOT NULL "
                    f"AND ref_table <> '' "
                    f"AND (table_name IN ({placeholders}) OR ref_table IN ({placeholders}))",
                    table_names + table_names,
                )
                rows.extend(list(cur.fetchall()))
            except Exception as e:
                logger.warning("semantic FK relationship lookup failed: %s", e)
        conn.close()

        # 去重
        seen = set()
        result = []
        for r in rows:
            key = (r["from_table"], r["from_column"], r["to_table"], r["to_column"])
            if key not in seen:
                seen.add(key)
                result.append(r)
        logger.info("Found %d table relationships", len(result))
        return result
    except Exception as e:
        logger.warning("get_table_relationships failed: %s", e)
        return []
