"""AgentScope analysis endpoints.

These endpoints expose the AgentScope-side planning workspace without changing
the existing SQL Harness execution path.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from agents.api.routers.query import _build_security_context
from agents.runtime.agentscope_adapter import create_agentscope_runner
from agents.runtime.agentscope_runtime import AgentScopeRuntime

logger = logging.getLogger(__name__)

router = APIRouter()


class AgentScopeComplexRequest(BaseModel):
    query: str
    session_id: str = "default_user"
    enabled_skills: list[str] = []
    workflow_state: dict[str, Any] = {}


class AgentScopeComplexResponse(BaseModel):
    query: str
    answer: str
    status: str
    session_id: str
    tool_trace: list[dict[str, Any]] = []
    sql_drafts: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    clarification_questions: list[str] = []
    risk_flags: list[dict[str, Any]] = []
    state_patch: dict[str, Any] = {}


def _is_complex_analysis_enabled() -> bool:
    value = os.getenv("AGENTSCOPE_COMPLEX_ANALYSIS_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


@router.post("/complex-analysis", response_model=AgentScopeComplexResponse)
async def agentscope_complex_analysis(
    req: AgentScopeComplexRequest,
    request: Request = None,
) -> AgentScopeComplexResponse:
    """Generate a complex-analysis plan and SQL draft without executing SQL."""

    if not _is_complex_analysis_enabled():
        return AgentScopeComplexResponse(
            query=req.query,
            answer="AgentScope complex analysis endpoint is disabled.",
            status="disabled",
            session_id=req.session_id,
        )

    workflow_state = dict(req.workflow_state or {})
    workflow_state.setdefault("thread_id", f"{req.session_id}:agentscope:{uuid.uuid4().hex[:12]}")
    runtime = AgentScopeRuntime(runner=create_agentscope_runner())

    try:
        result = await runtime.run(
            task_type="complex_analysis",
            query=req.query,
            session_id=req.session_id,
            security_context=_build_security_context(
                req.session_id,
                request.headers if request is not None else None,
            ),
            workflow_state=workflow_state,
            enabled_skills=req.enabled_skills,
        )
    except Exception as exc:
        logger.error("agentscope_complex_analysis failed: %s", exc, exc_info=True)
        return AgentScopeComplexResponse(
            query=req.query,
            answer=f"系统错误: {exc}",
            status="error",
            session_id=req.session_id,
            risk_flags=[
                {
                    "code": "agentscope_api_error",
                    "severity": "error",
                    "message": str(exc),
                }
            ],
        )

    return AgentScopeComplexResponse(
        query=req.query,
        answer=result.answer,
        status=_status_for_result(result),
        session_id=req.session_id,
        tool_trace=result.tool_trace,
        sql_drafts=result.sql_drafts,
        artifacts=result.artifacts,
        clarification_questions=result.clarification_questions,
        risk_flags=result.risk_flags,
        state_patch=result.state_patch,
    )


def _has_error_flag(risk_flags: list[dict[str, Any]]) -> bool:
    return any(str(flag.get("severity", "")).lower() == "error" for flag in risk_flags)


def _status_for_result(result) -> str:
    if _has_error_flag(result.risk_flags):
        return "error"
    presentation = result.state_patch.get("presentation", {})
    if isinstance(presentation, dict) and presentation.get("status"):
        return str(presentation["status"])
    if result.sql_drafts:
        return "needs_harness"
    return "completed"
