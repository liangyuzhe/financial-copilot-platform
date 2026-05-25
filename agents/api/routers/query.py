"""查询端点：意图分类 + 子图调用 + SQL 审批。"""

import logging
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from langgraph.types import Command

from agents.flow.dispatcher import build_final_graph
from agents.tool.trace.tracing import get_trace_callbacks
from agents.tool.memory.store import get_session, save_session
from agents.tool.memory.session import Message
from agents.tool.memory.manager import schedule_memory_maintenance
from agents.config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter()


class QueryRequest(BaseModel):
    query: str
    session_id: str = "default_user"
    route: str = ""  # 前端预分类的路由，非空时跳过 LLM 分类
    intent: str = ""  # 兼容旧字段
    route_confidence: float = 0.0
    route_reason: str = ""
    rewritten_query: str = ""  # 前端预重写的查询，非空时跳过上下文重写


class QueryResponse(BaseModel):
    query: str
    answer: str
    status: str
    session_id: str
    pending_approval: bool = False
    sql: str = ""
    result: str = ""
    approval_type: str = ""


class ClassifyResponse(BaseModel):
    route: str
    intent: str = ""
    route_confidence: float = 0.0
    route_reason: str = ""
    rewritten_query: str  # 重写后的独立查询
    session_id: str


class ApproveRequest(BaseModel):
    session_id: str = "default_user"
    approved: bool = True
    feedback: str = ""


def _split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _build_security_context(session_id: str, headers=None) -> dict:
    """Build V1 security context from request headers with session fallback."""
    headers = headers or {}
    user_id = headers.get("x-user-id") or headers.get("X-User-Id") or session_id
    username = headers.get("x-user-name") or headers.get("X-User-Name") or user_id
    company_id_raw = headers.get("x-company-id") or headers.get("X-Company-Id")
    try:
        company_id = int(company_id_raw) if company_id_raw else None
    except ValueError:
        company_id = None
    department_ids = []
    for value in _split_csv(headers.get("x-department-ids") or headers.get("X-Department-Ids")):
        try:
            department_ids.append(int(value))
        except ValueError:
            continue
    allowed_tables_header = headers.get("x-allowed-tables") or headers.get("X-Allowed-Tables")
    allowed_tables = _split_csv(allowed_tables_header) if allowed_tables_header else None
    return {
        "user_id": user_id,
        "username": username,
        "role_ids": _split_csv(headers.get("x-role-ids") or headers.get("X-Role-Ids")),
        "department_ids": department_ids,
        "company_id": company_id,
        "data_scopes": {},
        "allowed_tables": allowed_tables,
        "denied_tables": _split_csv(headers.get("x-denied-tables") or headers.get("X-Denied-Tables")),
        "column_policies": {},
    }


def _load_chat_history(session_id: str, query: str = "") -> list[dict]:
    """从 session store 加载对话历史。"""
    session = get_session(session_id)
    recent = session.history[-settings.memory.short_window_messages:]
    history = [{"role": m.role, "content": m.content} for m in recent]
    if query and session.preferences.get("_has_long_term_memory"):
        try:
            from agents.tool.memory.vector_store import recall_long_term_memory

            memories = recall_long_term_memory(session_id, query)
            if memories:
                memory_text = "\n---\n".join(d.page_content for d in memories)
                history.insert(0, {"role": "system", "content": f"[长期记忆]\n{memory_text}"})
        except Exception as e:
            logger.warning("Failed to recall long-term memory: %s", e)
    if session.summary:
        history.insert(0, {"role": "system", "content": f"[对话摘要] {session.summary}"})
    sql_context = session.preferences.get("_last_sql_context", "")
    if sql_context:
        history.insert(0, {"role": "system", "content": f"[上一轮SQL上下文]\n{sql_context}"})
    logger.info("Loaded chat history for session %s: %d messages", session_id, len(history))
    return history


def _save_qa_to_session(session_id: str, query: str, answer: str) -> None:
    """将本轮 Q&A 保存到 session store。"""
    try:
        session = get_session(session_id)
        session.history.append(Message(role="user", content=query))
        session.history.append(Message(role="assistant", content=answer))
        save_session(session_id, session)
        schedule_memory_maintenance(session_id)
        logger.info("Saved Q&A to session %s, history length: %d", session_id, len(session.history))
    except Exception as e:
        logger.warning("Failed to save Q&A to session: %s", e)


def _save_sql_context_to_session(session_id: str, query: str, sql: str, answer: str) -> None:
    """保存最近一次 SQL 查询口径，供追问沿用。"""
    if not sql:
        return
    try:
        session = get_session(session_id)
        context = (
            f"用户问题: {query}\n"
            f"生成SQL:\n{sql}\n"
            f"展示结果: {answer}"
        )
        session.preferences["_last_sql_context"] = context[:4000]
        save_session(session_id, session)
    except Exception as e:
        logger.warning("Failed to save SQL context to session: %s", e)


def _new_graph_thread_id(session_id: str) -> str:
    """Create an isolated graph checkpoint thread for one user turn."""
    return f"{session_id}:turn:{uuid.uuid4().hex[:12]}"


def _save_pending_query(session_id: str, query: str, thread_id: str = "") -> None:
    """中断时暂存原始 query，供 approve 后恢复。"""
    try:
        session = get_session(session_id)
        session.preferences["_pending_query"] = query
        if thread_id:
            session.preferences["_pending_thread_id"] = thread_id
        save_session(session_id, session)
    except Exception as e:
        logger.warning("Failed to save pending query: %s", e)


def _pop_pending_query(session_id: str) -> str:
    """取出中断时暂存的 query。"""
    try:
        session = get_session(session_id)
        query = session.preferences.pop("_pending_query", "")
        pending_thread = session.preferences.pop("_pending_thread_id", None)
        if query or pending_thread:
            save_session(session_id, session)
        return query
    except Exception:
        return ""


def _get_pending_query(session_id: str) -> str:
    """读取中断时暂存的 query，不删除。"""
    try:
        session = get_session(session_id)
        return session.preferences.get("_pending_query", "")
    except Exception:
        return ""


def _get_pending_thread_id(session_id: str) -> str:
    """读取中断时暂存的 graph thread id。"""
    try:
        session = get_session(session_id)
        return session.preferences.get("_pending_thread_id", "")
    except Exception:
        return ""


def _make_config(session_id: str, thread_id: str | None = None) -> dict:
    """构建包含 thread_id 和 trace callbacks 的 config。"""
    callbacks = get_trace_callbacks()
    config = {
        "configurable": {"thread_id": thread_id or session_id},
    }
    if callbacks:
        config["callbacks"] = callbacks
    return config


def _extract_interrupt(result: dict) -> dict | None:
    """从结果中提取 interrupt 信息。"""
    interrupts = result.get("__interrupt__", [])
    if not interrupts:
        return None
    interrupt = interrupts[0] if isinstance(interrupts, list) else interrupts
    value = interrupt.value if hasattr(interrupt, "value") else interrupt
    if isinstance(value, list) and value:
        value = value[0]
    return value if isinstance(value, dict) else None


async def _approve_sql_result(req: ApproveRequest) -> QueryResponse:
    """Resume the graph after SQL approval and convert the result to API response."""
    graph = build_final_graph()
    graph_thread_id = _get_pending_thread_id(req.session_id) or req.session_id
    config = _make_config(req.session_id, graph_thread_id)

    result = await graph.ainvoke(
        Command(resume={
            "approved": req.approved,
            "feedback": req.feedback,
        }),
        config=config,
    )

    interrupt_val = _extract_interrupt(result)
    if interrupt_val:
        return QueryResponse(
            query="",
            answer=interrupt_val.get("message", "请确认是否执行该 SQL"),
            status="pending_approval",
            session_id=req.session_id,
            pending_approval=True,
            sql=interrupt_val.get("sql", ""),
            result=result.get("result", ""),
            approval_type=interrupt_val.get("approval_type", "sql"),
        )

    answer = result.get("answer", "")
    original_query = _pop_pending_query(req.session_id) or result.get("query", "")
    sql = result.get("sql", "")
    if answer and original_query:
        _save_qa_to_session(req.session_id, original_query, answer)
        _save_sql_context_to_session(req.session_id, original_query, sql, answer)

    return QueryResponse(
        query=original_query,
        answer=answer,
        status=result.get("status", "completed"),
        session_id=req.session_id,
        sql=sql,
        result=result.get("result", ""),
    )


@router.post("/classify", response_model=ClassifyResponse)
async def classify_intent_endpoint(req: QueryRequest):
    """意图分类（非流式），前端据此选择流式端点。"""
    from agents.flow.dispatcher import classify_intent
    chat_history = _load_chat_history(req.session_id, req.query)
    result = await classify_intent(
        {"query": req.query, "chat_history": chat_history},
        config=_make_config(req.session_id),
    )
    return ClassifyResponse(
        route=result.get("route", "chat"),
        intent=result.get("route", "chat"),
        route_confidence=float(result.get("route_confidence", 0.0) or 0.0),
        route_reason=result.get("route_reason", ""),
        rewritten_query=result.get("rewritten_query", req.query),
        session_id=req.session_id,
    )


@router.post("/invoke", response_model=QueryResponse)
async def query_invoke(req: QueryRequest, request: Request = None):
    """查询调用：传入 route 时跳过分类，直接路由到子图。"""
    graph = build_final_graph()
    graph_thread_id = _new_graph_thread_id(req.session_id)
    config = _make_config(req.session_id, graph_thread_id)
    chat_history = _load_chat_history(req.session_id, req.query)

    initial_state = {
        "query": req.query,
        "session_id": req.session_id,
        "chat_history": chat_history,
        "route": req.route or req.intent or "",
        "intent": req.intent or req.route or "",
        "route_confidence": req.route_confidence,
        "route_reason": req.route_reason,
        "rewritten_query": req.rewritten_query or "",
        "security_context": _build_security_context(
            req.session_id,
            request.headers if request is not None else None,
        ),
    }

    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as e:
        logger.error("query_invoke failed: %s", e, exc_info=True)
        return QueryResponse(
            query=req.query,
            answer=f"系统错误: {e}",
            status="error",
            session_id=req.session_id,
        )

    # 检查是否被 interrupt（等待审批）
    interrupt_val = _extract_interrupt(result)
    if interrupt_val:
        # 暂存原始 query，供 approve 后恢复
        _save_pending_query(req.session_id, req.query, graph_thread_id)
        return QueryResponse(
            query=req.query,
            answer=interrupt_val.get("message", "请确认是否执行该 SQL"),
            status="pending_approval",
            session_id=req.session_id,
            pending_approval=True,
            sql=interrupt_val.get("sql", ""),
            result=result.get("result", ""),
            approval_type=interrupt_val.get("approval_type", "sql"),
        )

    answer = result.get("answer", "")
    sql = result.get("sql", "")
    # 保存本轮 Q&A 到 session
    if answer:
        _save_qa_to_session(req.session_id, req.query, answer)
        _save_sql_context_to_session(req.session_id, req.query, sql, answer)

    return QueryResponse(
        query=req.query,
        answer=answer,
        status=result.get("status", "completed"),
        session_id=req.session_id,
        sql=sql,
        result=result.get("result", ""),
    )


@router.post("/approve")
async def approve_sql(req: ApproveRequest):
    """审批 SQL：继续执行被中断的图。"""
    try:
        return await _approve_sql_result(req)
    except Exception as e:
        logger.error("approve_sql failed: %s", e, exc_info=True)
        return QueryResponse(
            query="",
            answer=f"系统错误: {e}",
            status="error",
            session_id=req.session_id,
        )


@router.post("/approve/stream")
async def approve_sql_stream(req: ApproveRequest, request: Request):
    """审批 SQL：SSE 返回执行、反思、等待确认等阶段。"""

    async def generate() -> AsyncGenerator[dict, None]:
        try:
            if not req.approved:
                yield {"event": "status", "data": "正在提交拒绝意见..."}
            else:
                yield {"event": "status", "data": "已确认，正在继续处理审批请求..."}

            result = await _approve_sql_result(req)

            if result.pending_approval:
                if result.approval_type == "complex_plan":
                    yield {"event": "status", "data": "复杂查询计划已生成，请确认是否进入分步执行。"}
                else:
                    yield {"event": "status", "data": "检测到执行失败或结果异常，已完成反思/分析并生成修正 SQL。"}
                    yield {"event": "status", "data": "请确认是否执行修正后的 SQL。"}
            else:
                if "复杂查询计划执行完成" in result.answer:
                    yield {"event": "status", "data": "复杂查询计划已执行完成。"}
                elif "复杂查询计划已确认" in result.answer:
                    yield {"event": "status", "data": "复杂查询计划已确认。"}
                else:
                    yield {"event": "status", "data": "SQL 执行完成。"}

            yield {"event": "result", "data": result.model_dump_json()}
        except Exception as e:
            logger.error("approve_sql_stream failed: %s", e, exc_info=True)
            payload = QueryResponse(
                query="",
                answer=f"系统错误: {e}",
                status="error",
                session_id=req.session_id,
            )
            yield {"event": "result", "data": payload.model_dump_json()}
        finally:
            yield {"event": "done", "data": "[DONE]"}

    return EventSourceResponse(generate())
