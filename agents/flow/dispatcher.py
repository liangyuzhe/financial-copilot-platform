"""Route dispatcher: classify -> data/chat/clarify -> subgraphs."""

from __future__ import annotations

import asyncio
import json
import logging
import os

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agents.config.settings import settings
from agents.flow.complex_query import validate_complex_plan
from agents.flow.state import FinalGraphState
from agents.flow.rag_chat import build_rag_chat_graph
from agents.flow.sql_react import execute_complex_plan_step
from agents.model.chat_model import get_chat_model
from agents.runtime.agentscope_adapter import create_agentscope_runner
from agents.runtime.agentscope_adapter import LocalAgentScopeCompatibleRunner
from agents.runtime.agentscope_runtime import AgentScopeRuntime
from agents.runtime.result import AgentRunResult
from agents.tool.security.policies import authorize_tables
from agents.tool.storage.checkpoint import get_checkpointer
from agents.tool.storage.domain_summary import get_domain_summary
from agents.tool.storage.intent_rules import evaluate_intent_rules
from agents.tool.trace.tracing import callbacks_from_config, child_trace_config, traced_async_tool_call

logger = logging.getLogger(__name__)

_ROUTE_DATA = "data"
_ROUTE_CHAT = "chat"
_ROUTE_CLARIFY = "clarify"
_ROUTES = {_ROUTE_DATA, _ROUTE_CHAT, _ROUTE_CLARIFY}

_CLASSIFY_SYSTEM_PROMPT = """你是一个路由分类器，同时完成两个任务：

1. 判断用户问题应该走哪个入口路由：data、chat、clarify
2. 将省略主体或代词化的问题重写成独立完整的问题

路由定义：
- data：用户要处理当前系统数据、统计、分析、关系、对比、报表、预算、回款、费用、异常归因等
- chat：闲聊、通用问答、外部公开信息、与当前数据域无关的问题
- clarify：问题目标、时间范围、主体、口径或范围不够明确，需要用户补充

请只返回 JSON，不要输出其他内容：
{{"route": "data|chat|clarify", "rewritten_query": "...", "confidence": 0.0, "reason": "..."}}"""


def _agentscope_data_planner_fallback_enabled() -> bool:
    value = os.getenv("AGENTSCOPE_DATA_PLANNER_FALLBACK", "").strip().lower()
    return value in {"1", "true", "yes", "on", "local", "local_compatible"}


def _normalize_route(value: str) -> str:
    route = (value or "").strip().lower()
    if route in _ROUTES:
        return route
    if route in {
        "sql_query",
        "analysis",
        "anomaly_detect",
        "reconciliation",
        "report",
        "audit",
        "knowledge",
        "complex_analysis",
        "data_task",
    }:
        return _ROUTE_DATA
    if route in {"need_clarify", "needs_clarification", "ambiguous", "clarification"}:
        return _ROUTE_CLARIFY
    if route == "chat":
        return _ROUTE_CHAT
    return _ROUTE_CHAT


def _route_from_rule_decision(rule_decision) -> str | None:
    if not rule_decision:
        return None
    raw = str(getattr(rule_decision, "intent", "") or getattr(rule_decision, "route_signal", "") or "")
    route = _normalize_route(raw)
    return route if route in _ROUTES else None


def _apply_rule_rewrite_template(query: str, rewritten_query: str, rule_decision) -> str:
    template = str(getattr(rule_decision, "rewrite_template", "") or "").strip()
    if not template:
        return rewritten_query
    rendered = (
        template
        .replace("{query}", query or "")
        .replace("{rewritten_query}", rewritten_query or query or "")
        .strip()
    )
    return rendered or rewritten_query


def _arbitrate_route(llm_route: str, rule_decision) -> str:
    rule_route = _route_from_rule_decision(rule_decision)
    if rule_route in _ROUTES:
        return rule_route
    return _normalize_route(llm_route)


def _rule_confidence(rule_decision) -> float:
    try:
        return float(getattr(rule_decision, "confidence", 1.0) or 1.0)
    except (TypeError, ValueError):
        return 1.0


def _rule_reason(rule_decision) -> str:
    return str(getattr(rule_decision, "rule_name", "") or "").strip()


async def classify_intent(state: FinalGraphState, config=None) -> dict:
    """Classify the request into data/chat/clarify and rewrite the query."""
    existing_route = str(state.get("route", "") or state.get("intent", "") or "").strip()
    existing_rewrite = str(state.get("rewritten_query", "") or "").strip()
    if existing_route and existing_route in _ROUTES and existing_rewrite:
        return {
            "route": existing_route,
            "rewritten_query": existing_rewrite,
            "route_confidence": float(state.get("route_confidence", 1.0) or 1.0),
            "route_reason": str(state.get("route_reason", "") or ""),
        }

    callbacks = callbacks_from_config(config)
    query = str(state.get("query", "") or "").strip()

    async def _evaluate_rules():
        return await evaluate_intent_rules(
            query,
            valid_intents={"data", "chat", "clarify", "sql_query"},
        )

    rule_decision = await traced_async_tool_call(
        "intent_rules.evaluate",
        query,
        callbacks,
        _evaluate_rules,
        metadata={"storage": "mysql", "node": "classify_route"},
    )

    chat_history = state.get("chat_history", [])
    rule_route = _route_from_rule_decision(rule_decision)
    if rule_route in _ROUTES and not chat_history:
        rewritten_query = _apply_rule_rewrite_template(query, query, rule_decision)
        logger.info(
            "classify_route: rule_short_circuit route=%s, query='%s' -> '%s', rule=%s",
            rule_route,
            query,
            rewritten_query,
            rule_decision.to_dict() if rule_decision else None,
        )
        return {
            "route": rule_route,
            "rewritten_query": rewritten_query,
            "route_confidence": _rule_confidence(rule_decision),
            "route_reason": _rule_reason(rule_decision),
        }

    domain_task = asyncio.create_task(
        traced_async_tool_call(
            "domain_summary.load",
            query,
            callbacks,
            get_domain_summary,
            metadata={"storage": "mysql", "node": "classify_route"},
        )
    )
    model = get_chat_model(settings.chat_model_type)
    domain = await domain_task

    history_context = ""
    if chat_history:
        recent = chat_history[-6:]
        lines = [f"[{item['role']}]: {item['content']}" for item in recent]
        history_context = "\n\n最近对话历史:\n" + "\n".join(lines)

    user_msg = f"""数据库领域摘要：
{domain if domain else "（暂无领域摘要）"}
{history_context}

用户问题: {query}"""

    try:
        response = await model.ainvoke(
            [
                SystemMessage(content=_CLASSIFY_SYSTEM_PROMPT),
                HumanMessage(content=user_msg),
            ],
            config=child_trace_config(
                config,
                "dispatcher.classify_route.llm",
                tags=["llm", "route"],
            ),
        )
    except Exception as exc:
        logger.warning("classify_route llm failed, falling back to data route: %s", exc)
        return {
            "route": _ROUTE_DATA,
            "rewritten_query": query,
            "route_confidence": 0.0,
            "route_reason": "classify_llm_unavailable_domain_fallback" if domain else "classify_llm_unavailable",
        }

    raw = str(response.content or "").strip()
    route = _ROUTE_CHAT
    rewritten_query = query
    route_confidence = 0.0
    route_reason = ""

    try:
        clean = raw
        if clean.startswith("```"):
            lines = clean.split("\n")
            json_lines = []
            in_block = False
            for line in lines:
                if line.strip().startswith("```") and not in_block:
                    in_block = True
                    continue
                if line.strip() == "```" and in_block:
                    break
                if in_block:
                    json_lines.append(line)
            clean = "\n".join(json_lines)

        data = json.loads(clean)
        raw_route = str(data.get("route") or data.get("intent") or "").strip()
        rewritten_query = str(data.get("rewritten_query") or query).strip() or query
        route_confidence = float(data.get("confidence") or data.get("route_confidence") or 0.0)
        route_reason = str(data.get("reason") or data.get("route_reason") or "").strip()
        route = _normalize_route(raw_route)
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        for candidate in raw.lower().split():
            if candidate in _ROUTES:
                route = candidate
                break

    route = _arbitrate_route(route, rule_decision)
    rewritten_query = _apply_rule_rewrite_template(query, rewritten_query, rule_decision)
    if not route_reason and rule_decision:
        route_reason = _rule_reason(rule_decision)

    logger.info(
        "classify_route: route=%s, query='%s' -> '%s', rule=%s",
        route,
        query,
        rewritten_query,
        rule_decision.to_dict() if rule_decision else None,
    )
    return {
        "route": route,
        "rewritten_query": rewritten_query,
        "route_confidence": route_confidence,
        "route_reason": route_reason,
    }


async def agentscope_data_planner(state: FinalGraphState, config=None) -> dict:
    """Run AgentScope as the primary data planner."""
    query = str(state.get("rewritten_query", "") or state.get("query", "") or "").strip()
    callbacks = _agentscope_callbacks_for_runtime(config)
    runtime = AgentScopeRuntime(runner=create_agentscope_runner(), callbacks=callbacks)

    async def _run_primary_agentscope():
        return await runtime.run(
            task_type="data_analysis",
            query=query,
            session_id=str(state.get("session_id", "") or ""),
            security_context=state.get("security_context", {}),
            workflow_state=_agentscope_workflow_state(state),
        )

    try:
        result = await traced_async_tool_call(
            "agentscope.data_planner.primary",
            query,
            callbacks,
            _run_primary_agentscope,
            metadata={
                "node": "agentscope_data_planner",
                "backend": "auto",
                "task_type": "data_analysis",
                "session_id": str(state.get("session_id", "") or ""),
            },
        )
    except Exception as exc:
        logger.error("agentscope_data_planner failed: %s", exc, exc_info=True)
        return {
            "answer": f"系统错误: {exc}",
            "status": "error",
            "agentscope_result": {},
            "agentscope_observation": {"error": str(exc)},
        }

    result, observation = await _ensure_agentscope_analysis_plan(result, state, query, callbacks)
    if observation is None:
        observation = {
            "backend": result.state_patch.get("agentscope_backend", ""),
            "task_type": "data_analysis",
            "tool_trace_count": len(result.tool_trace),
            "risk_flags": list(result.risk_flags),
        }
    presentation = result.state_patch.get("presentation", {})
    analysis_plan = result.state_patch.get("analysis_plan", {})
    if not (isinstance(analysis_plan, dict) and analysis_plan.get("steps")):
        if _agentscope_result_is_clarification(result):
            logger.info(
                "agentscope_data_planner returned clarification without analysis_plan: backend=%s answer=%s",
                observation["backend"],
                result.answer[:500],
            )
            questions = _agentscope_clarification_questions(result)
            return {
                "answer": result.answer or "\n".join(questions) or "请补充查询对象、时间范围或口径后再问。",
                "status": "clarify",
                "analysis_plan": {},
                "clarification_questions": questions,
                "agentscope_result": result.to_dict(),
                "agentscope_observation": observation,
            }
        logger.error(
            "agentscope_data_planner returned no analysis_plan: backend=%s risks=%s answer=%s",
            observation["backend"],
            observation["risk_flags"],
            result.answer[:500],
        )
        return {
            "answer": "数据分析计划生成失败：AgentScope 未提交可执行的 analysis_plan，请查看 trace 中的 agentscope_observation 和工具调用日志。",
            "status": "error",
            "analysis_plan": {},
            "agentscope_result": result.to_dict(),
            "agentscope_observation": observation,
        }

    status = str(presentation.get("status") or "")
    if not status:
        status = "needs_harness" if result.state_patch.get("requires_harness") else "completed"
    selected_tables = (
        result.state_patch.get("selected_tables")
        or result.state_patch.get("candidate_tables")
        or []
    )
    return {
        "answer": result.answer,
        "status": status,
        "analysis_plan": analysis_plan,
        "selected_tables": selected_tables,
        "table_relationships": result.state_patch.get("table_relationships", state.get("table_relationships", [])),
        "table_metadata": result.state_patch.get("table_metadata", state.get("table_metadata", {})),
        "semantic_model": result.state_patch.get("semantic_model", state.get("semantic_model", {})),
        "evidence": result.state_patch.get("evidence", state.get("evidence", [])),
        "few_shot_examples": result.state_patch.get("few_shot_examples", state.get("few_shot_examples", [])),
        "recall_context": result.state_patch.get("recall_context", state.get("recall_context", {})),
        "enhanced_query": result.state_patch.get("enhanced_query", state.get("enhanced_query", "")),
        "agentscope_result": result.to_dict(),
        "agentscope_observation": observation,
    }


def _agentscope_clarification_questions(result: AgentRunResult) -> list[str]:
    if result.clarification_questions:
        return [str(item) for item in result.clarification_questions if str(item).strip()]
    answer = str(result.answer or "")
    lines = []
    for line in answer.splitlines():
        stripped = line.strip(" -0123456789.、\t")
        if stripped.endswith(("？", "?")) and stripped:
            lines.append(stripped)
    return lines[:5]


def _agentscope_result_is_clarification(result: AgentRunResult) -> bool:
    if any(str(flag.get("severity", "")).lower() == "error" for flag in result.risk_flags):
        return False
    if _agentscope_clarification_questions(result):
        return True
    answer = str(result.answer or "")
    return any(marker in answer for marker in ("请说明", "请补充", "请明确", "需要澄清", "澄清以下"))


async def _ensure_agentscope_analysis_plan(
    result: AgentRunResult,
    state: FinalGraphState,
    query: str,
    callbacks: list,
) -> tuple[AgentRunResult, dict | None]:
    analysis_plan = result.state_patch.get("analysis_plan", {})
    if isinstance(analysis_plan, dict) and analysis_plan.get("steps"):
        return result, None

    original_observation = {
        "backend": result.state_patch.get("agentscope_backend", ""),
        "task_type": "data_analysis",
        "tool_trace_count": len(result.tool_trace),
        "risk_flags": list(result.risk_flags),
    }
    logger.error(
        "agentscope_data_planner returned no analysis_plan: backend=%s risks=%s answer=%s",
        original_observation["backend"],
        original_observation["risk_flags"],
        result.answer[:500],
    )

    if not _agentscope_data_planner_fallback_enabled():
        original_observation.update(
            {
                "fallback_disabled": True,
                "fallback_reason": "missing_analysis_plan",
                "no_evidence_tool_calls": len(result.tool_trace) == 0,
            }
        )
        return result, original_observation

    fallback_runtime = AgentScopeRuntime(
        runner=LocalAgentScopeCompatibleRunner(),
        callbacks=callbacks,
    )

    async def _run_fallback_agentscope():
        return await fallback_runtime.run(
            task_type="data_analysis",
            query=query,
            session_id=str(state.get("session_id", "") or ""),
            security_context=state.get("security_context", {}),
            workflow_state=_agentscope_workflow_state(state),
        )

    try:
        fallback = await traced_async_tool_call(
            "agentscope.data_planner.fallback.local_compatible",
            query,
            callbacks,
            _run_fallback_agentscope,
            metadata={
                "node": "agentscope_data_planner",
                "backend": "local_compatible",
                "fallback_from_backend": original_observation["backend"],
                "task_type": "data_analysis",
                "session_id": str(state.get("session_id", "") or ""),
            },
        )
    except Exception as exc:
        logger.error("agentscope_data_planner fallback failed: %s", exc, exc_info=True)
        return result, original_observation

    fallback_plan = fallback.state_patch.get("analysis_plan", {})
    if not (isinstance(fallback_plan, dict) and fallback_plan.get("steps")):
        return result, original_observation

    observation = {
        "backend": fallback.state_patch.get("agentscope_backend", ""),
        "fallback_from_backend": original_observation["backend"],
        "task_type": "data_analysis",
        "tool_trace_count": len(fallback.tool_trace),
        "risk_flags": original_observation["risk_flags"],
        "fallback_risk_flags": list(fallback.risk_flags),
    }
    return fallback, observation


def _agentscope_callbacks_for_runtime(config) -> list:
    callbacks = callbacks_from_config(config)
    if not callbacks:
        return []
    if isinstance(callbacks, (list, tuple)):
        return list(callbacks)
    return [callbacks]


def _agentscope_workflow_state(state: FinalGraphState) -> dict:
    workflow_state = {
        "query": state.get("query", ""),
        "rewritten_query": state.get("rewritten_query", ""),
        "chat_history": state.get("chat_history", []),
        "route": state.get("route", ""),
        "route_reason": state.get("route_reason", ""),
        "route_confidence": state.get("route_confidence", 0.0),
        "security_context": state.get("security_context", {}),
    }
    for key in (
        "enhanced_query",
        "selected_tables",
        "table_metadata",
        "table_relationships",
        "semantic_model",
        "evidence",
        "few_shot_examples",
        "recall_context",
        "feasibility_decision",
        "complexity_report",
    ):
        value = state.get(key)
        if value:
            workflow_state[key] = value
    return workflow_state


def _tables_from_analysis_plan(plan: dict) -> list[str]:
    tables: list[str] = []
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for table in step.get("tables") or []:
            table_name = str(table).strip()
            if table_name and table_name not in tables:
                tables.append(table_name)
    return tables


def _analysis_plan_to_complex_plan(plan: dict) -> dict:
    normalized = dict(plan or {})
    normalized["mode"] = "complex_plan"
    return normalized


def route_after_agentscope_data_planner(state: FinalGraphState) -> str:
    """Plans submitted by AgentScope must go through Harness approval/execution."""
    plan = state.get("analysis_plan") or {}
    if isinstance(plan, dict) and plan.get("steps"):
        return "approve_analysis_plan"
    return END


def _format_analysis_plan_preview(plan: dict) -> str:
    steps = plan.get("steps") or []
    lines = ["AgentScope 已生成数据分析计划，请确认是否交由 SQL Harness 审批执行："]
    reason = str(plan.get("reason") or "").strip()
    if reason:
        lines.append(f"原因：{reason}")
    for item in steps:
        if not isinstance(item, dict):
            continue
        step_no = item.get("step")
        step_type = item.get("type", "")
        goal = item.get("goal", "")
        tables = [str(table) for table in item.get("tables") or [] if str(table)]
        suffix = f"（{step_type}"
        if tables:
            suffix += f": {', '.join(tables)}"
        suffix += "）"
        lines.append(f"{step_no}. {goal}{suffix}")
    return "\n".join(lines)


async def approve_analysis_plan(state: FinalGraphState, config=None) -> dict:
    """Validate and ask user approval for an AgentScope-submitted analysis plan."""
    raw_plan = state.get("analysis_plan") or {}
    if not isinstance(raw_plan, dict):
        return {
            "answer": "AgentScope 未提交有效分析计划。",
            "status": "error",
        }
    plan = _analysis_plan_to_complex_plan(raw_plan)
    referenced_tables = _tables_from_analysis_plan(plan)
    auth = authorize_tables(
        referenced_tables,
        state.get("security_context", {}),
        stage="analysis_plan.approve",
    )
    if not auth.allowed:
        return {
            "answer": auth.message or "分析计划引用了无权限的数据表。",
            "status": "error",
        }
    ok, error = validate_complex_plan(plan, allowed_tables=set(auth.allowed_tables or referenced_tables))
    if not ok:
        return {
            "answer": f"AgentScope 分析计划校验失败：{error}",
            "status": "error",
        }
    approved = interrupt({
        "complex_plan": plan,
        "analysis_plan": raw_plan,
        "message": _format_analysis_plan_preview(plan),
        "approval_type": "complex_plan",
    })
    if approved.get("approved"):
        return {
            "complex_plan": plan,
            "plan_approved": True,
            "analysis_plan": raw_plan,
            "answer": "AgentScope 数据分析计划已确认，准备进入 SQL Harness 分步执行。",
            "status": "approved",
        }
    return {
        "complex_plan": plan,
        "plan_approved": False,
        "analysis_plan": raw_plan,
        "answer": "已取消 AgentScope 数据分析计划执行。",
        "status": "rejected",
    }


async def execute_analysis_plan(state: FinalGraphState, config=None) -> dict:
    """Execute an approved AgentScope analysis plan through the existing SQL Harness."""
    selected_tables = state.get("selected_tables") or _tables_from_analysis_plan(state.get("complex_plan") or state.get("analysis_plan") or {})
    complex_state = {
        **state,
        "complex_plan": state.get("complex_plan") or _analysis_plan_to_complex_plan(state.get("analysis_plan") or {}),
        "plan_approved": bool(state.get("plan_approved")),
        "selected_tables": selected_tables,
        "table_relationships": state.get("table_relationships", []),
        "table_metadata": state.get("table_metadata", {}),
        "semantic_model": state.get("semantic_model", {}),
        "evidence": state.get("evidence", []),
        "few_shot_examples": state.get("few_shot_examples", []),
        "recall_context": state.get("recall_context", {}),
        "enhanced_query": state.get("enhanced_query", ""),
        "execution_history": state.get("execution_history", []),
        "retry_count": state.get("retry_count", 0),
    }
    result = await execute_complex_plan_step(complex_state, config=config)
    return {
        **result,
        "complex_plan": complex_state.get("complex_plan", {}),
        "plan_approved": bool(complex_state.get("plan_approved")),
        "status": "completed" if not result.get("error") else "error",
    }


async def chat_direct(state: FinalGraphState, config=None) -> dict:
    """Normal chat path."""
    rag_graph = build_rag_chat_graph()
    rewritten = state.get("rewritten_query", "")
    result = await rag_graph.ainvoke(
        {
            "input": {
                "session_id": state["session_id"],
                "query": rewritten or state["query"],
                "rewritten_query": rewritten,
            },
        },
        config=config,
    )
    return {
        "answer": result.get("answer", ""),
        "status": "completed",
    }


async def clarify_direct(state: FinalGraphState, config=None) -> dict:
    """Ask for missing information."""
    reason = str(state.get("route_reason", "") or "").strip()
    answer = reason or "请补充查询对象、时间范围或口径后再问。"
    return {
        "answer": answer,
        "status": "completed",
    }


def route_target(state: FinalGraphState) -> str:
    route = str(state.get("route", "") or _ROUTE_CHAT).strip().lower()
    if route == _ROUTE_DATA:
        return "agentscope_data_planner"
    if route == _ROUTE_CLARIFY:
        return "clarify_direct"
    return "chat_direct"


def route_after_analysis_plan_approval(state: FinalGraphState) -> str:
    if state.get("plan_approved"):
        return "execute_analysis_plan"
    return END


def build_final_graph():
    """Build the final router graph."""
    graph = StateGraph(FinalGraphState)

    graph.add_node("classify_intent", classify_intent)
    graph.add_node("agentscope_data_planner", agentscope_data_planner)
    graph.add_node("approve_analysis_plan", approve_analysis_plan)
    graph.add_node("execute_analysis_plan", execute_analysis_plan)
    graph.add_node("chat_direct", chat_direct)
    graph.add_node("clarify_direct", clarify_direct)

    graph.add_edge(START, "classify_intent")
    graph.add_conditional_edges("classify_intent", route_target)
    graph.add_conditional_edges("agentscope_data_planner", route_after_agentscope_data_planner)
    graph.add_conditional_edges("approve_analysis_plan", route_after_analysis_plan_approval)
    graph.add_edge("execute_analysis_plan", END)
    graph.add_edge("chat_direct", END)
    graph.add_edge("clarify_direct", END)

    checkpointer = get_checkpointer()
    return graph.compile(checkpointer=checkpointer)
