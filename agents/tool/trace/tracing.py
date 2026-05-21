"""Tracing initialization for LangSmith and CozeLoop.

Creates callback handlers that are attached to every LangChain/LangGraph
invocation via get_trace_callbacks().
"""

from __future__ import annotations

import logging
import os
from typing import Any

from agents.config.settings import settings

logger = logging.getLogger(__name__)

# CozeLoop client singleton (lazily created, cleaned up on shutdown)
_cozeloop_client: Any = None


def init_langsmith() -> None:
    """Set env vars for LangSmith (also used by LangChainTracer)."""
    if not settings.langsmith.tracing or not settings.langsmith.api_key:
        logger.info("LangSmith tracing disabled")
        return

    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langsmith.api_key
    os.environ["LANGCHAIN_ENDPOINT"] = settings.langsmith.url
    if settings.langsmith.project:
        os.environ["LANGCHAIN_PROJECT"] = settings.langsmith.project

    # Show LangSmith trace send/flush logs in server output
    for name in ("langsmith", "langsmith.client", "langsmith._internal"):
        logging.getLogger(name).setLevel(logging.INFO)

    logger.info(
        "LangSmith tracing enabled (endpoint: %s, project: %s)",
        settings.langsmith.url,
        settings.langsmith.project or "(default)",
    )


def _get_langsmith_handler() -> Any | None:
    """Return a LangChainTracer, or None if disabled."""
    if not settings.langsmith.tracing or not settings.langsmith.api_key:
        return None

    try:
        from langchain_core.tracers.langchain import LangChainTracer
        tracer = LangChainTracer(project_name=settings.langsmith.project or None)
        return tracer
    except Exception as e:
        logger.warning("Failed to create LangChainTracer: %s", e)
        return None


def _set_cozeloop_env() -> None:
    """Push CozeLoop settings into env vars so the SDK can read them."""
    cfg = settings.cozeloop
    if cfg.workspace_id:
        os.environ["COZELOOP_WORKSPACE_ID"] = cfg.workspace_id
    if cfg.api_base_url:
        os.environ["COZELOOP_API_BASE_URL"] = cfg.api_base_url
    if cfg.jwt_oauth_client_id:
        os.environ["COZELOOP_JWT_OAUTH_CLIENT_ID"] = cfg.jwt_oauth_client_id
    if cfg.jwt_oauth_private_key:
        # Fix PEM: collapse double newlines that break cryptography parser
        key = cfg.jwt_oauth_private_key.replace("\n\n", "\n")
        os.environ["COZELOOP_JWT_OAUTH_PRIVATE_KEY"] = key
    if cfg.jwt_oauth_public_key_id:
        os.environ["COZELOOP_JWT_OAUTH_PUBLIC_KEY_ID"] = cfg.jwt_oauth_public_key_id


def get_cozeloop_handler() -> Any | None:
    """Return a CozeLoop LangChain callback handler, or None if disabled."""
    global _cozeloop_client

    if not settings.cozeloop.tracing or not settings.cozeloop.jwt_oauth_client_id:
        return None

    try:
        import cozeloop
        from cozeloop.integration.langchain.trace_callback import LoopTracer
    except ImportError:
        logger.warning(
            "CozeLoop tracing requested but 'cozeloop' package not installed. "
            "Install with: pip install cozeloop"
        )
        return None

    try:
        if _cozeloop_client is None:
            _set_cozeloop_env()
            _cozeloop_client = cozeloop.new_client()
            logger.info("CozeLoop client initialized (JWT OAuth)")

        handler = LoopTracer.get_callback_handler(_cozeloop_client)
        return handler
    except Exception as e:
        logger.warning("Failed to initialize CozeLoop: %s", e)
        return None


def get_trace_callbacks() -> list[Any]:
    """Return all enabled trace callback handlers."""
    callbacks = []

    # LangSmith
    langsmith = _get_langsmith_handler()
    if langsmith:
        callbacks.append(langsmith)

    # CozeLoop
    cozelop = get_cozeloop_handler()
    if cozelop:
        callbacks.append(cozelop)

    return callbacks


def callbacks_from_config(config: dict | None):
    """Extract LangChain callbacks from a Runnable config."""
    if not config:
        return []
    callbacks = config.get("callbacks", [])
    if not callbacks:
        return []
    if isinstance(callbacks, (list, tuple)):
        return list(callbacks)
    # LangGraph may pass CallbackManager/AsyncCallbackManager objects here.
    return callbacks


def child_trace_config(
    config: dict | None,
    run_name: str,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build child Runnable config so inner LLM/retriever calls appear in traces."""
    child: dict[str, Any] = {"run_name": run_name}
    callbacks = callbacks_from_config(config)
    if callbacks:
        child["callbacks"] = callbacks
    if tags:
        child["tags"] = tags
    if metadata:
        child["metadata"] = metadata
    return child


def _callback_handlers_for_manager(callbacks: Any | None) -> list[Any]:
    """Return callback handlers suitable for LangChain CallbackManager.configure."""
    if not callbacks:
        return []
    items = list(callbacks) if isinstance(callbacks, (list, tuple)) else [callbacks]
    handlers: list[Any] = []
    for item in items:
        manager_handlers = [
            *list(getattr(item, "inheritable_handlers", []) or []),
            *list(getattr(item, "handlers", []) or []),
        ]
        if manager_handlers:
            handlers.extend(manager_handlers)
            continue
        if hasattr(item, "run_inline"):
            handlers.append(item)
    return handlers


def traced_retriever_call(
    name: str,
    query: str,
    callbacks: Any | None,
    func,
    metadata: dict[str, Any] | None = None,
):
    """Trace a non-Runnable retriever operation as a retriever span."""
    handlers = _callback_handlers_for_manager(callbacks)
    if not handlers:
        return func()

    from langchain_core.callbacks import CallbackManager

    manager = CallbackManager.configure(
        inheritable_callbacks=handlers,
        inheritable_tags=["retriever", name],
        inheritable_metadata=metadata or {},
    )
    run_manager = manager.on_retriever_start(
        {"name": name},
        query,
    )
    try:
        result = func()
        run_manager.on_retriever_end(result)
        return result
    except Exception as e:
        run_manager.on_retriever_error(e)
        raise


def traced_tool_call(
    name: str,
    input_str: str,
    callbacks: Any | None,
    func,
    metadata: dict[str, Any] | None = None,
):
    """Trace non-Runnable IO work such as Redis/MySQL metadata loading."""
    handlers = _callback_handlers_for_manager(callbacks)
    if not handlers:
        return func()

    from langchain_core.callbacks import CallbackManager

    manager = CallbackManager.configure(
        inheritable_callbacks=handlers,
        inheritable_tags=["tool", name],
        inheritable_metadata=metadata or {},
    )
    run_manager = manager.on_tool_start(
        {"name": name},
        input_str,
        inputs={"input": input_str, **(metadata or {})},
    )
    try:
        result = func()
        run_manager.on_tool_end(str(result)[:4000])
        return result
    except Exception as e:
        run_manager.on_tool_error(e)
        raise


async def traced_async_tool_call(
    name: str,
    input_str: str,
    callbacks: Any | None,
    func,
    metadata: dict[str, Any] | None = None,
):
    """Trace non-Runnable async IO work as a tool span."""
    handlers = _callback_handlers_for_manager(callbacks)
    if not handlers:
        return await func()

    from langchain_core.callbacks import AsyncCallbackManager

    manager = AsyncCallbackManager.configure(
        inheritable_callbacks=handlers,
        inheritable_tags=["tool", name],
        inheritable_metadata=metadata or {},
    )
    run_manager = await manager.on_tool_start(
        {"name": name},
        input_str,
        inputs={"input": input_str, **(metadata or {})},
    )
    try:
        result = await func()
        await run_manager.on_tool_end(str(result)[:4000])
        return result
    except Exception as e:
        await run_manager.on_tool_error(e)
        raise


def close_cozeloop() -> None:
    """Shut down the CozeLoop client (call on app shutdown)."""
    global _cozeloop_client
    if _cozeloop_client is not None:
        try:
            _cozeloop_client.close()
        except Exception:
            pass
        _cozeloop_client = None


def init_tracing() -> None:
    """Initialize all tracing systems."""
    init_langsmith()
    if settings.cozeloop.tracing and settings.cozeloop.jwt_oauth_client_id:
        logger.info("CozeLoop tracing configured (JWT OAuth, will attach per-request)")
    elif settings.cozeloop.tracing:
        logger.warning("CozeLoop tracing enabled but jwt_oauth_client_id is empty")
