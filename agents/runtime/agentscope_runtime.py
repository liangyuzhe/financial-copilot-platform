"""Minimal AgentScope runtime adapter for read-only analysis tasks."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from agents.runtime.result import AgentRunResult, JsonDict
from agents.runtime.skill_registry import SkillDefinition, SkillRegistry
from agents.runtime.tool_catalog import ToolCatalog
from agents.runtime.tool_exposure_policy import ToolExposurePolicy
from agents.runtime.tool_contracts import RuntimeTool, ToolCallResult, ToolTrace
from agents.tool.security.policies import SecurityContext

logger = logging.getLogger(__name__)


SUPPORTED_TASK_TYPES: tuple[str, ...] = (
    "exploratory_analysis",
    "report_generation",
    "complex_analysis",
    "data_analysis",
)


@dataclass(slots=True)
class _CallbackSpan:
    run_manager: Any = None
    run_id: Any = None

COMMON_ANALYSIS_AGENT_PROMPT = """\
agent: common_analysis_agent

你是 Financial Copilot Platform 的开放式数据探索 Agent。
你只能通过 ToolCatalog 暴露的只读工具理解数据源、表、字段、业务口径和表关系。
你不能直接执行 SQL，不能绕过 SQL Harness，也不能替代权限、安全、审批、审计或评测链路。
如果需要 SQL，只能输出草稿；草稿必须回到 SQL Harness 完成 safety_check、authorize_sql、approve 和 execute_sql。
输出必须能被平台转换为 AgentRunResult，包括 answer、tool_trace、sql_drafts、artifacts、clarification_questions、risk_flags、state_patch 和 events。
"""

DATA_ANALYSIS_AGENT_PROMPT = """\
agent: data_analysis_agent

你是 Financial Copilot Platform 的数据分析规划 Agent。
你只能通过 ToolCatalog 暴露的规划和只读校验工具理解上下文、业务知识、SQL 示例、表结构、字段语义、表关系、可行性和当前时间。
你不能直接执行 SQL，也不能绕过 SQL Harness。
你必须自主决定是继续探索、提出澄清、生成单步 analysis_plan，还是拆成多步 analysis_plan。
你可以使用 query.context_rewrite、business_knowledge.search、sql_examples.search、query.enhance、schema.list_tables、schema.describe_table、schema.select_candidates、semantic_model.search、schema.related_tables、plan.assess_feasibility、sql.normalize、sql.safety_check、sql.authorize_draft、current_time.now 和 analysis_plan.submit。
当问题包含省略主体、代词、相对时间或业务口径不清时，应主动调用相应工具；不要只描述“应该调用工具”。
当需要多个表的字段语义时，优先调用 semantic_model.search(table_names=[...]) 或 schema.select_candidates；schema.describe_table 只用于 1-2 张需要深挖的表。
完成规划后必须通过 analysis_plan.submit 提交包含非空 steps 的结构化 analysis_plan；如果确实不能规划，只返回澄清问题，不要伪造执行事实。
你可以在计划中包含 SQL 草稿，但最终执行、权限检查、安全检查和审批都必须回到 SQL Harness；本地 SQL 工具只用于格式、安全和授权预检查。
analysis_plan 应尽量包含 display_schema，声明最终给用户看的字段；每项包含 role、label、column、type，SQL step/report 应输出与 column 对齐的稳定别名。
最终回复只输出简洁 answer、analysis_plan 或 clarification_questions；不要回写 tool_trace、events、state_patch 或完整 AgentRunResult，这些由 runtime 组装。
"""

REPORT_AGENT_PROMPT = """\
agent: report_agent

你是 Financial Copilot Platform 的报告生成 Agent。
你只能读取已有 result/artifact，并基于这些已执行结果生成报告。
你不能调用 schema、semantic model 或 SQL execution 工具，不能补充未在 artifact 中出现的事实。
报告输出必须包含：结论、关键指标、异常点、后续追查建议。
如需图表，只能把已有指标渲染为 ECharts 配置，不能自行执行 SQL 获取新数据。
输出必须能被平台转换为 AgentRunResult，包括 answer、tool_trace、artifacts、risk_flags、state_patch 和 events。
"""

COMPLEX_ANALYSIS_AGENT_PROMPT = """\
agent: complex_analysis_agent

你是 Financial Copilot Platform 的复杂分析规划 Agent。
你可以探索表关系、业务口径和已知语义，并生成结构化分析计划和 SQL 草稿。
你不能直接执行 SQL；所有 SQL 草稿必须通过 sql_draft.submit 回到 SQL Harness。
SQL Harness 负责 safety_check、authorize_sql、approve 和 execute_sql，AgentScopeRuntime 不产生执行事实。
输出必须能被平台转换为 AgentRunResult，包括 answer、tool_trace、sql_drafts、risk_flags、state_patch 和 events。
"""

Runner = Callable[["AgentScopeRunContext"], Any | Awaitable[Any]]


@dataclass(slots=True)
class AgentScopeRunContext:
    """Context passed to the AgentScope runner implementation."""

    task_type: str
    query: str
    session_id: str
    thread_id: str
    security_context: SecurityContext | dict | None
    workflow_state: JsonDict
    enabled_skills: list[str]
    tools: list[RuntimeTool]
    tool_catalog: ToolCatalog
    system_prompt: str
    callbacks: list[Any] = field(default_factory=list)
    tool_trace: list[JsonDict] = field(default_factory=list)
    events: list[JsonDict] = field(default_factory=list)
    tool_cache: dict[str, ToolCallResult] = field(default_factory=dict)
    tool_readthrough_cache: dict[str, ToolCallResult] = field(default_factory=dict)
    tool_inflight: dict[str, asyncio.Task[ToolCallResult]] = field(default_factory=dict)
    _sql_draft_submitted: bool = False
    _analysis_plan_submitted: bool = False

    async def invoke_tool(
        self,
        tool_name: str,
        payload: JsonDict | None = None,
    ) -> ToolCallResult:
        """Invoke an allowlisted ToolCatalog tool and collect trace events."""

        payload = dict(payload or {})
        allowed_names = {tool.name for tool in self.tools}
        active_spans: list[tuple[Any, Any]] | None = None
        if tool_name not in allowed_names:
            active_spans = await self._emit_span_start(
                kind="tool",
                name=f"agentscope.tool.{tool_name}",
                input_text=str(payload),
                metadata={
                    "task_type": self.task_type,
                    "session_id": self.session_id,
                    "thread_id": self.thread_id,
                    "tool_name": tool_name,
                },
            )
            result = self._forbidden_tool_result(tool_name, payload)
        else:
            if tool_name in {"sql_draft.submit", "analysis_plan.submit"} and self._handoff_submitted(tool_name):
                result = self._duplicate_sql_draft_result(payload)
                self.events.append(
                    {
                        "event": "tool_deduped",
                        "data": {
                            "tool_name": tool_name,
                            "reason": result.error,
                        },
                    }
                )
                return result
            cache_key = self._tool_cache_key(tool_name, payload)
            cached = self.tool_cache.get(cache_key) or self._readthrough_cached_result(tool_name, payload)
            if cached is not None:
                result = self._cached_tool_result(cached, payload)
                self._record_cache_hit(tool_name, cache_key, result)
                return await self._finalize_tool_result(tool_name, result)
            else:
                inflight = self.tool_inflight.get(cache_key)
                if inflight is not None:
                    shared_result = await inflight
                    result = (
                        self._cached_tool_result(shared_result, payload)
                        if shared_result.ok
                        else shared_result
                    )
                    self._record_cache_hit(tool_name, cache_key, result)
                    return await self._finalize_tool_result(tool_name, result)
                else:
                    active_spans = await self._emit_span_start(
                        kind="tool",
                        name=f"agentscope.tool.{tool_name}",
                        input_text=str(payload),
                        metadata={
                            "task_type": self.task_type,
                            "session_id": self.session_id,
                            "thread_id": self.thread_id,
                            "tool_name": tool_name,
                        },
                    )
                    inflight = asyncio.create_task(
                        self.tool_catalog.invoke(
                            tool_name,
                            payload,
                            task_type=self.task_type,
                            security_context=self.security_context,
                            session_id=self.session_id,
                            thread_id=self.thread_id,
                            workflow_state=self.workflow_state,
                        )
                    )
                    self.tool_inflight[cache_key] = inflight
                    try:
                        result = await inflight
                    finally:
                        self.tool_inflight.pop(cache_key, None)
                    if result.ok:
                        self.tool_cache[cache_key] = result
                        self._remember_readthrough_result(tool_name, result)
                        if tool_name in {"sql_draft.submit", "analysis_plan.submit"}:
                            self._mark_handoff_submitted(tool_name)
        return await self._finalize_tool_result(tool_name, result, active_spans=active_spans)

    def _readthrough_cached_result(
        self,
        tool_name: str,
        payload: JsonDict,
    ) -> ToolCallResult | None:
        cached = self.tool_readthrough_cache.get(tool_name)
        if cached is None:
            return None
        if tool_name == "business_knowledge.search":
            return cached
        if tool_name == "semantic_model.search":
            return cached if self._semantic_cache_covers(cached.output, payload) else None
        if tool_name == "schema.list_tables":
            return cached
        return None

    def _remember_readthrough_result(self, tool_name: str, result: ToolCallResult) -> None:
        if tool_name not in {
            "business_knowledge.search",
            "semantic_model.search",
            "schema.list_tables",
        }:
            return
        output = result.output if isinstance(result.output, dict) else {}
        if output.get("source") != "workflow_state" or output.get("cache_hit") is not True:
            return
        self.tool_readthrough_cache[tool_name] = result

    def _semantic_cache_covers(self, output: Any, payload: JsonDict) -> bool:
        if not isinstance(output, dict):
            return False
        requested = {
            str(table).strip()
            for table in payload.get("table_names", []) or []
            if str(table).strip()
        }
        if not requested:
            return True
        semantic_model = output.get("semantic_model")
        if not isinstance(semantic_model, dict):
            return False
        return requested.issubset(set(semantic_model.keys()))

    def _duplicate_sql_draft_result(self, payload: JsonDict) -> ToolCallResult:
        context = SecurityContext.from_dict(self.security_context)
        now = datetime.now(timezone.utc)
        message = "handoff tool already submitted for this run"
        trace = ToolTrace(
            tool_name="sql_draft.submit",
            task_type=self.task_type,
            session_id=self.session_id,
            thread_id=self.thread_id,
            user_id=context.user_id,
            status="deduped",
            elapsed_ms=0,
            started_at=now,
            ended_at=now,
            input=payload,
            output=None,
            error=message,
        )
        return ToolCallResult(ok=False, output=None, error=message, trace=trace)

    def _handoff_submitted(self, tool_name: str) -> bool:
        if tool_name == "analysis_plan.submit":
            return self._analysis_plan_submitted
        return self._sql_draft_submitted

    def _mark_handoff_submitted(self, tool_name: str) -> None:
        if tool_name == "analysis_plan.submit":
            self._analysis_plan_submitted = True
        else:
            self._sql_draft_submitted = True

    def _tool_cache_key(self, tool_name: str, payload: JsonDict) -> str:
        normalized_payload = self._normalize_tool_payload(tool_name, payload)
        return json.dumps(
            {
                "tool_name": tool_name,
                "payload": normalized_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _normalize_tool_payload(self, tool_name: str, payload: JsonDict) -> JsonDict:
        normalized = dict(payload)
        if tool_name == "business_knowledge.search":
            normalized["query"] = str(normalized.get("query", "") or "").strip()
            if "top_k" in normalized and normalized["top_k"] is not None:
                normalized["top_k"] = int(normalized["top_k"])
        elif tool_name in {"semantic_model.search", "schema.related_tables"}:
            table_names = [
                str(item).strip()
                for item in normalized.get("table_names", []) or []
                if str(item).strip()
            ]
            normalized["table_names"] = sorted(dict.fromkeys(table_names))
        elif tool_name == "schema.describe_table":
            normalized["table_name"] = str(normalized.get("table_name", "") or "").strip()
        elif tool_name == "sql_draft.submit":
            normalized["sql"] = self._normalize_sql_text(str(normalized.get("sql", "") or ""))
            tables = [
                str(item).strip()
                for item in normalized.get("tables", []) or []
                if str(item).strip()
            ]
            normalized["tables"] = sorted(dict.fromkeys(tables))
        elif tool_name == "artifact.read":
            artifact_ids = [
                str(item).strip()
                for item in normalized.get("artifact_ids", []) or []
                if str(item).strip()
            ]
            types = [
                str(item).strip()
                for item in normalized.get("types", []) or []
                if str(item).strip()
            ]
            normalized["artifact_ids"] = artifact_ids
            normalized["types"] = sorted(dict.fromkeys(types))
        elif tool_name == "report.render":
            normalized["title"] = str(normalized.get("title", "") or "").strip()
            artifact_ids = [
                str(item).strip()
                for item in normalized.get("artifact_ids", []) or []
                if str(item).strip()
            ]
            types = [
                str(item).strip()
                for item in normalized.get("types", []) or []
                if str(item).strip()
            ]
            normalized["artifact_ids"] = artifact_ids
            normalized["types"] = sorted(dict.fromkeys(types))
            normalized["include_echarts"] = bool(normalized.get("include_echarts", False))
        return normalized

    def _normalize_sql_text(self, sql: str) -> str:
        text = sql.strip()
        if text.endswith(";"):
            text = text[:-1]
        return " ".join(text.split())

    def _record_cache_hit(self, tool_name: str, cache_key: str, result: ToolCallResult) -> None:
        logger.info(
            "AgentScope tool cache hit: task_type=%s tool=%s session_id=%s thread_id=%s cache_key=%s",
            self.task_type,
            tool_name,
            self.session_id,
            self.thread_id,
            cache_key,
        )
        trace = result.trace.to_dict()
        self.tool_trace.append(trace)
        self.events.append(
            {
                "event": "tool_cache_hit",
                "data": {
                    "tool_name": tool_name,
                    "cache_key": cache_key,
                    "trace": trace,
                },
            }
        )

    async def _finalize_tool_result(
        self,
        tool_name: str,
        result: ToolCallResult,
        active_spans: list[tuple[Any, Any]] | None = None,
    ) -> ToolCallResult:
        if result.trace.status not in {"cache_hit"}:
            await self._emit_tool_span_end(tool_name, result, active_spans=active_spans)
        trace = result.trace.to_dict()
        self.tool_trace.append(trace)
        self.events.append({"event": "tool_trace", "data": trace})
        return result

    async def _emit_tool_span_end(
        self,
        tool_name: str,
        result: ToolCallResult,
        active_spans: list[tuple[Any, Any]] | None = None,
    ) -> None:
        # Tool spans are only emitted for real provider calls and forbidden attempts.
        output = result.output if result.output is not None else {"error": result.error}
        await self._emit_span_end(
            kind="tool",
            name=f"agentscope.tool.{tool_name}",
            output_text=output,
            error_text=result.error if not result.ok else "",
            active_spans=active_spans,
        )

    def _cached_tool_result(self, cached: ToolCallResult, payload: JsonDict) -> ToolCallResult:
        now = datetime.now(timezone.utc)
        output = self._cache_hit_output(cached.output)
        trace = ToolTrace(
            tool_name=cached.trace.tool_name,
            task_type=cached.trace.task_type,
            session_id=self.session_id,
            thread_id=self.thread_id,
            user_id=cached.trace.user_id,
            status="cache_hit",
            elapsed_ms=0,
            started_at=now,
            ended_at=now,
            input=payload,
            output=output,
            error="",
        )
        return ToolCallResult(ok=True, output=output, error="", trace=trace)

    def _cache_hit_output(self, output: Any) -> Any:
        if isinstance(output, dict):
            cached = dict(output)
            cached["source"] = "runtime_tool_cache"
            cached["cache_hit"] = True
            cached.setdefault("cached_from", "previous_tool_call")
            return cached
        return {
            "value": output,
            "source": "runtime_tool_cache",
            "cache_hit": True,
            "cached_from": "previous_tool_call",
        }

    async def emit_runtime_span(self, phase: str, message: str, metadata: JsonDict | None = None) -> None:
        span = await self._emit_span_start(
            kind="chain",
            name=f"agentscope.runtime.{self.task_type}",
            input_text=message,
            metadata={
                "task_type": self.task_type,
                "session_id": self.session_id,
                "thread_id": self.thread_id,
                **(metadata or {}),
            },
        )
        await self._emit_span_end(
            kind="chain",
            name=f"agentscope.runtime.{self.task_type}",
            output_text=phase,
            active_spans=span,
        )

    async def start_runtime_span(self, message: str, metadata: JsonDict | None = None) -> list[tuple[Any, Any]]:
        return await self._emit_span_start(
            kind="chain",
            name=f"agentscope.runtime.{self.task_type}",
            input_text=message,
            metadata={
                "task_type": self.task_type,
                "session_id": self.session_id,
                "thread_id": self.thread_id,
                **(metadata or {}),
            },
        )

    async def start_chain_span(
        self,
        name: str,
        input_text: Any,
        metadata: JsonDict | None = None,
    ) -> list[tuple[Any, Any]]:
        return await self._emit_span_start(
            kind="chain",
            name=name,
            input_text=self._span_payload_text(input_text),
            metadata={
                "task_type": self.task_type,
                "session_id": self.session_id,
                "thread_id": self.thread_id,
                **(metadata or {}),
            },
        )

    async def start_llm_span(
        self,
        name: str,
        prompt: str,
        metadata: JsonDict | None = None,
    ) -> list[tuple[Any, Any]]:
        return await self._emit_span_start(
            kind="llm",
            name=name,
            input_text=prompt,
            metadata={
                "task_type": self.task_type,
                "session_id": self.session_id,
                "thread_id": self.thread_id,
                **(metadata or {}),
            },
        )

    async def end_runtime_span(
        self,
        active_spans: list[tuple[Any, Any]],
        status: str,
        error_text: str = "",
    ) -> None:
        await self._emit_span_end(
            kind="chain",
            name=f"agentscope.runtime.{self.task_type}",
            output_text=status,
            error_text=error_text,
            active_spans=active_spans,
        )

    async def end_chain_span(
        self,
        active_spans: list[tuple[Any, Any]],
        name: str,
        output_text: Any,
        error_text: str = "",
    ) -> None:
        await self._emit_span_end(
            kind="chain",
            name=name,
            output_text=output_text,
            error_text=error_text,
            active_spans=active_spans,
        )

    async def end_llm_span(
        self,
        active_spans: list[tuple[Any, Any]],
        name: str,
        output_text: str,
        error_text: str = "",
    ) -> None:
        await self._emit_span_end(
            kind="llm",
            name=name,
            output_text=output_text,
            error_text=error_text,
            active_spans=active_spans,
        )

    def _span_payload_text(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    def _run_manager_output_payload(
        self,
        kind: str,
        name: str,
        output_text: Any,
    ) -> Any:
        if kind == "llm":
            return _llm_result(self._span_payload_text(output_text)).model_dump()
        if kind == "chain":
            return {
                "output": self._span_payload_text(output_text),
                "name": name,
            }
        return {
            "output": output_text,
        }

    def _forbidden_tool_result(
        self,
        tool_name: str,
        payload: JsonDict,
    ) -> ToolCallResult:
        context = SecurityContext.from_dict(self.security_context)
        now = datetime.now(timezone.utc)
        message = f"tool {tool_name} is not available for task_type {self.task_type}"
        trace = ToolTrace(
            tool_name=tool_name,
            task_type=self.task_type,
            session_id=self.session_id,
            thread_id=self.thread_id,
            user_id=context.user_id,
            status="forbidden",
            elapsed_ms=0,
            started_at=now,
            ended_at=now,
            input=payload,
            output=None,
            error=message,
        )
        return ToolCallResult(ok=False, output=None, error=message, trace=trace)

    async def _emit_span_start(
        self,
        *,
        kind: str,
        name: str,
        input_text: str,
        metadata: JsonDict | None = None,
    ) -> list[tuple[Any, Any]]:
        active_spans: list[tuple[Any, Any]] = []
        for callback in self.callbacks:
            if hasattr(callback, "on_chain_start") and kind == "chain":
                run_id = uuid4()
                maybe = self._callback_start_call(
                    callback,
                    "on_chain_start",
                    {"name": name},
                    {"input": input_text, **(metadata or {})},
                    metadata or {},
                    name=name,
                    run_type="chain",
                    run_id=run_id,
                )
                if inspect.isawaitable(maybe):
                    maybe = await maybe
                active_spans.append((callback, _CallbackSpan(run_manager=maybe, run_id=run_id)))
            elif hasattr(callback, "on_tool_start") and kind == "tool":
                run_id = uuid4()
                maybe = self._callback_start_call(
                    callback,
                    "on_tool_start",
                    {"name": name},
                    input_text,
                    metadata or {},
                    inputs={"input": input_text, **(metadata or {})},
                    name=name,
                    run_id=run_id,
                )
                if inspect.isawaitable(maybe):
                    maybe = await maybe
                active_spans.append((callback, _CallbackSpan(run_manager=maybe, run_id=run_id)))
            elif hasattr(callback, "on_llm_start") and kind == "llm":
                run_id = uuid4()
                maybe = self._callback_start_call(
                    callback,
                    "on_llm_start",
                    {"name": name},
                    [input_text],
                    metadata or {},
                    name=name,
                    run_id=run_id,
                )
                if inspect.isawaitable(maybe):
                    maybe = await maybe
                active_spans.append((callback, _CallbackSpan(run_manager=maybe, run_id=run_id)))
        return active_spans

    def _callback_start_call(
        self,
        callback: Any,
        method_name: str,
        serialized: JsonDict,
        positional_input: Any,
        metadata: JsonDict,
        **kwargs: Any,
    ) -> Any:
        method = getattr(callback, method_name)
        # CallbackManager instances already inject their own metadata into
        # child handlers. Passing metadata again makes LangChain raise
        # "multiple values for keyword argument 'metadata'" in real graph runs.
        if hasattr(callback, "handlers") and hasattr(callback, "metadata"):
            manager_metadata = dict(getattr(callback, "metadata", {}) or {})
            try:
                manager_metadata.update(metadata)
                from langchain_core.callbacks import AsyncCallbackManager, CallbackManager

                manager_cls = AsyncCallbackManager if getattr(callback, "is_async", False) else CallbackManager
                callback = manager_cls.configure(
                    inheritable_callbacks=list(getattr(callback, "inheritable_handlers", []) or []),
                    local_callbacks=list(getattr(callback, "handlers", []) or []),
                    inheritable_tags=list(getattr(callback, "inheritable_tags", []) or []),
                    local_tags=list(getattr(callback, "tags", []) or []),
                    inheritable_metadata=manager_metadata,
                )
                method = getattr(callback, method_name)
            except Exception:
                pass
            return method(serialized, positional_input, **kwargs)
        filtered_kwargs = self._filtered_callback_kwargs(callback, method_name, {"metadata": metadata, **kwargs})
        return method(serialized, positional_input, **filtered_kwargs)

    async def _emit_span_end(
        self,
        *,
        kind: str,
        name: str,
        output_text: Any,
        error_text: str = "",
        active_spans: list[tuple[Any, Any]] | None = None,
    ) -> None:
        targets = active_spans if active_spans is not None else [
            (callback, _CallbackSpan()) for callback in self.callbacks
        ]
        for callback, span in targets:
            if isinstance(span, _CallbackSpan):
                run_manager = span.run_manager
                run_id = span.run_id
            else:
                run_manager = span
                run_id = getattr(span, "run_id", None)
            callback_kwargs = {"run_id": run_id} if run_id is not None else {}
            rendered_output = str(output_text)
            if kind == "chain":
                if run_manager is not None and hasattr(run_manager, "end"):
                    maybe = run_manager.end(
                        outputs=self._run_manager_output_payload(kind, name, output_text),
                        error=error_text or None,
                    )
                elif run_manager is not None and hasattr(run_manager, "finish"):
                    if not error_text:
                        try:
                            run_manager.set_tags({"output": self._span_payload_text(output_text)})
                        except Exception:
                            pass
                    else:
                        try:
                            run_manager.set_tags({"error": error_text})
                        except Exception:
                            pass
                    maybe = run_manager.finish()
                elif run_manager is not None and hasattr(run_manager, "on_chain_error") and error_text:
                    maybe = run_manager.on_chain_error(Exception(error_text))
                elif run_manager is not None and hasattr(run_manager, "on_chain_end"):
                    maybe = run_manager.on_chain_end({"output": rendered_output, "name": name})
                elif error_text and hasattr(callback, "on_chain_error"):
                    filtered_kwargs = self._filtered_callback_kwargs(callback, "on_chain_error", callback_kwargs)
                    maybe = callback.on_chain_error(Exception(error_text), **filtered_kwargs)
                elif hasattr(callback, "on_chain_end"):
                    filtered_kwargs = self._filtered_callback_kwargs(callback, "on_chain_end", callback_kwargs)
                    maybe = callback.on_chain_end({"output": rendered_output, "name": name}, **filtered_kwargs)
                else:
                    maybe = None
                if inspect.isawaitable(maybe):
                    await maybe
            elif kind == "tool":
                if run_manager is not None and hasattr(run_manager, "end"):
                    maybe = run_manager.end(
                        outputs=self._run_manager_output_payload(kind, name, output_text),
                        error=error_text or None,
                    )
                elif run_manager is not None and hasattr(run_manager, "finish"):
                    if not error_text:
                        try:
                            run_manager.set_tags({"output": self._span_payload_text(output_text)})
                        except Exception:
                            pass
                    else:
                        try:
                            run_manager.set_tags({"error": error_text})
                        except Exception:
                            pass
                    maybe = run_manager.finish()
                elif run_manager is not None and hasattr(run_manager, "on_tool_error") and error_text:
                    maybe = run_manager.on_tool_error(Exception(error_text))
                elif run_manager is not None and hasattr(run_manager, "on_tool_end"):
                    maybe = run_manager.on_tool_end(output_text)
                elif error_text and hasattr(callback, "on_tool_error"):
                    filtered_kwargs = self._filtered_callback_kwargs(callback, "on_tool_error", callback_kwargs)
                    maybe = callback.on_tool_error(Exception(error_text), **filtered_kwargs)
                elif hasattr(callback, "on_tool_end"):
                    filtered_kwargs = self._filtered_callback_kwargs(callback, "on_tool_end", callback_kwargs)
                    maybe = callback.on_tool_end(output_text, **filtered_kwargs)
                else:
                    maybe = None
                if inspect.isawaitable(maybe):
                    await maybe
            elif kind == "llm":
                run_managers = run_manager if isinstance(run_manager, list) else [run_manager]
                for llm_manager in run_managers:
                    if llm_manager is not None and hasattr(llm_manager, "end"):
                        maybe = llm_manager.end(
                            outputs=self._run_manager_output_payload(kind, name, output_text),
                            error=error_text or None,
                        )
                    elif llm_manager is not None and hasattr(llm_manager, "finish"):
                        if not error_text:
                            try:
                                llm_manager.set_tags({"output": self._span_payload_text(output_text)})
                            except Exception:
                                pass
                        else:
                            try:
                                llm_manager.set_tags({"error": error_text})
                            except Exception:
                                pass
                        maybe = llm_manager.finish()
                    elif llm_manager is not None and hasattr(llm_manager, "on_llm_error") and error_text:
                        maybe = llm_manager.on_llm_error(Exception(error_text))
                    elif llm_manager is not None and hasattr(llm_manager, "on_llm_end"):
                        maybe = llm_manager.on_llm_end(_llm_result(rendered_output))
                    elif error_text and hasattr(callback, "on_llm_error"):
                        filtered_kwargs = self._filtered_callback_kwargs(callback, "on_llm_error", callback_kwargs)
                        maybe = callback.on_llm_error(Exception(error_text), **filtered_kwargs)
                    elif hasattr(callback, "on_llm_end"):
                        filtered_kwargs = self._filtered_callback_kwargs(callback, "on_llm_end", callback_kwargs)
                        maybe = callback.on_llm_end(_llm_result(rendered_output), **filtered_kwargs)
                    else:
                        maybe = None
                    if inspect.isawaitable(maybe):
                        await maybe

    def _filtered_callback_kwargs(self, callback: Any, method_name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        method = getattr(callback, method_name, None)
        if method is None:
            return {}
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            return kwargs
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
            return kwargs
        allowed = set(signature.parameters)
        return {key: value for key, value in kwargs.items() if key in allowed}


@dataclass(slots=True)
class AgentScopeRuntime:
    """Thin runtime boundary between platform state and AgentScope execution."""

    tool_catalog: ToolCatalog = field(default_factory=ToolCatalog)
    skill_registry: SkillRegistry = field(default_factory=SkillRegistry.builtin)
    tool_exposure_policy: ToolExposurePolicy = field(default_factory=ToolExposurePolicy.from_env)
    runner: Runner | None = None
    callbacks: list[Any] = field(default_factory=list)

    async def run(
        self,
        *,
        task_type: str,
        query: str,
        session_id: str,
        security_context: SecurityContext | dict | None = None,
        workflow_state: JsonDict | None = None,
        enabled_skills: list[str] | None = None,
    ) -> AgentRunResult:
        """Run a supported AgentScope-side analysis task."""

        workflow_state = dict(workflow_state or {})
        requested_skill_names = list(enabled_skills or [])
        if task_type not in SUPPORTED_TASK_TYPES:
            return self._unsupported_task_result(task_type)

        selected_skills = self.skill_registry.match(
            task_type=task_type,
            query=query,
            enabled_skills=requested_skill_names or None,
        )
        base_tools = self.tool_catalog.get_tools(
            task_type=task_type,
            security_context=security_context,
        )
        allowed_tool_names = self.skill_registry.allowed_tool_names(
            task_type=task_type,
            base_tool_names=[tool.name for tool in base_tools],
            skills=selected_skills,
        )
        tools = [
            tool
            for tool in base_tools
            if tool.name in set(allowed_tool_names)
        ]
        context = AgentScopeRunContext(
            task_type=task_type,
            query=query,
            session_id=session_id,
            thread_id=str(workflow_state.get("thread_id", "") or ""),
            security_context=security_context,
            workflow_state=workflow_state,
            enabled_skills=[skill.name for skill in selected_skills],
            tools=tools,
            tool_catalog=self.tool_catalog,
            system_prompt=self._build_system_prompt(task_type, selected_skills),
            callbacks=list(self.callbacks),
        )

        runner_backend = self._runner_backend_name()
        runtime_span = await context.start_runtime_span(
            query,
            metadata={
                "runner_backend": runner_backend,
                "tool_names": [tool.name for tool in tools],
                "enabled_skills": [skill.name for skill in selected_skills],
            },
        )
        try:
            raw_result = await self._call_runner(context)
        except Exception as exc:
            await context.end_runtime_span(runtime_span, "error", str(exc))
            return self._runner_error_result(exc, context)
        else:
            await context.end_runtime_span(runtime_span, "completed")

        result = AgentRunResult.from_value(raw_result)
        result.tool_trace = _merge_dict_lists(context.tool_trace, result.tool_trace)
        result.events = _merge_dict_lists(context.events, result.events)
        result.sql_drafts = self._normalize_sql_drafts(result.sql_drafts)
        if task_type == "data_analysis":
            result.state_patch.setdefault(
                "requires_harness",
                bool(result.sql_drafts) or bool(result.state_patch.get("analysis_plan")),
            )
        if task_type == "complex_analysis" or result.sql_drafts:
            result.state_patch.setdefault("presentation", self._presentation_patch(result))
        if task_type == "data_analysis" and result.state_patch.get("analysis_plan"):
            self._add_analysis_plan_guardrail(result)
        if result.sql_drafts:
            self._add_sql_draft_guardrail(result)
        if not result.answer:
            result.answer = self._fallback_answer(task_type, result)
        tool_summary = _tool_trace_summary(result.tool_trace)
        logger.info(
            "AgentScope runtime tool summary: task_type=%s session_id=%s thread_id=%s summary=%s",
            task_type,
            session_id,
            context.thread_id,
            json.dumps(tool_summary, ensure_ascii=False, sort_keys=True),
        )
        return result

    async def _call_runner(self, context: AgentScopeRunContext) -> Any:
        runner = self.runner or _default_agentscope_runner
        if hasattr(runner, "tool_exposure_policy"):
            setattr(runner, "tool_exposure_policy", self.tool_exposure_policy)
        result = runner(context)
        if inspect.isawaitable(result):
            return await result
        return result

    def _runner_backend_name(self) -> str:
        runner = self.runner or _default_agentscope_runner
        if runner is _default_agentscope_runner:
            return "default"
        if inspect.isfunction(runner) or inspect.ismethod(runner) or inspect.iscoroutinefunction(runner):
            return "function"
        class_name = runner.__class__.__name__
        if class_name == "AgentScopePackageRunner":
            return "agentscope"
        if class_name == "LocalAgentScopeCompatibleRunner":
            return "local_compatible"
        return class_name

    def _build_system_prompt(
        self,
        task_type: str,
        selected_skills: list[SkillDefinition],
    ) -> str:
        base_prompt = (
            REPORT_AGENT_PROMPT
            if task_type == "report_generation"
            else DATA_ANALYSIS_AGENT_PROMPT
            if task_type == "data_analysis"
            else COMPLEX_ANALYSIS_AGENT_PROMPT
            if task_type == "complex_analysis"
            else COMMON_ANALYSIS_AGENT_PROMPT
        )
        if not selected_skills:
            return base_prompt
        skill_sections = "\n\n".join(
            self._format_skill_prompt(skill) for skill in selected_skills
        )
        return f"{base_prompt}\n启用 skills:\n{skill_sections}\n"

    def _format_skill_prompt(self, skill: SkillDefinition) -> str:
        required_sections = skill.output_format.get("required_sections", [])
        output_format = "、".join(str(item) for item in required_sections)
        return (
            f"- {skill.name}: {skill.description}\n"
            f"  prompt: {skill.prompt}\n"
            f"  output_format: {output_format}"
        )

    def _unsupported_task_result(self, task_type: str) -> AgentRunResult:
        return AgentRunResult(
            answer="",
            risk_flags=[
                {
                    "code": "unsupported_task_type",
                    "severity": "warning",
                    "task_type": task_type,
                    "message": (
                        "AgentScopeRuntime currently supports exploratory_analysis, report_generation, "
                        "and complex_analysis; "
                        "route SQL execution and other task types through existing platform paths."
                    ),
                }
            ],
            events=[
                {
                    "event": "risk",
                    "data": {
                        "code": "unsupported_task_type",
                        "task_type": task_type,
                    },
                }
            ],
        )

    def _runner_error_result(
        self,
        exc: Exception,
        context: AgentScopeRunContext,
    ) -> AgentRunResult:
        message = str(exc)
        lower_message = message.lower()
        code = (
            "agentscope_unavailable"
            if "agentscope" in lower_message and "not installed" in lower_message
            else "agentscope_runner_error"
        )
        return AgentRunResult(
            answer="",
            tool_trace=list(context.tool_trace),
            sql_drafts=[],
            risk_flags=[
                {
                    "code": code,
                    "severity": "error",
                    "message": message,
                }
            ],
            events=_merge_dict_lists(
                context.events,
                [
                    {
                        "event": "error",
                        "data": {
                            "code": code,
                            "message": message,
                        },
                    }
                ],
            ),
        )

    def _normalize_sql_drafts(self, sql_drafts: list[JsonDict]) -> list[JsonDict]:
        normalized: list[JsonDict] = []
        for draft in sql_drafts:
            row = dict(draft)
            row["execution_mode"] = "draft_only"
            row["requires_harness"] = True
            normalized.append(row)
        return normalized

    def _add_sql_draft_guardrail(self, result: AgentRunResult) -> None:
        if any(flag.get("code") == "sql_draft_not_executed" for flag in result.risk_flags):
            return
        result.risk_flags.append(
            {
                "code": "sql_draft_not_executed",
                "severity": "info",
                "message": "SQL drafts are not executed by AgentScopeRuntime; route them through SQL Harness.",
            }
        )

    def _add_analysis_plan_guardrail(self, result: AgentRunResult) -> None:
        if any(flag.get("code") == "analysis_plan_not_executed" for flag in result.risk_flags):
            return
        result.risk_flags.append(
            {
                "code": "analysis_plan_not_executed",
                "severity": "info",
                "message": "Analysis plans are not executed by AgentScopeRuntime; route them through SQL Harness.",
            }
        )

    def _fallback_answer(self, task_type: str, result: AgentRunResult) -> str:
        if result.sql_drafts:
            return (
                "AgentScope 复杂分析计划已生成。\n"
                "当前返回的是 draft_only SQL 草稿，不是最终经营结论。\n"
                "请将草稿交回 SQL Harness 完成 safety_check、authorize_sql、approve、execute_sql 后，"
                "再基于执行结果输出可读结论。"
            )
        if result.state_patch.get("analysis_plan"):
            return (
                "AgentScope 数据分析计划已生成。\n"
                "当前返回的是 plan_only 分析计划，不是最终经营结论。\n"
                "请将计划交回 SQL Harness 完成 validate_analysis_plan、safety_check、authorize_sql、approve、execute_sql、merge_report 后，"
                "再基于执行结果输出可读结论。"
            )
        if task_type == "report_generation" and result.artifacts:
            return "报告 artifact 已生成，请在 artifacts 中查看 Markdown 报告。"
        return ""

    def _presentation_patch(self, result: AgentRunResult) -> JsonDict:
        if result.sql_drafts:
            return {
                "status": "needs_harness",
                "headline": "分析计划已生成",
                "summary": "当前返回的是可执行前的分析计划和 SQL 草稿，不是最终经营结论。",
                "next_action": "run_sql_harness",
                "node_notes": [
                    {"node": "business_knowledge.search", "meaning": "提取业务口径和计算方法。"},
                    {"node": "schema.list_tables", "meaning": "列出可见表。"},
                    {"node": "semantic_model.search", "meaning": "加载字段语义并筛选候选指标列。"},
                    {"node": "schema.related_tables", "meaning": "补充表关系，便于后续分步执行。"},
                    {"node": "sql_draft.submit", "meaning": "把草稿交回 SQL Harness 审批和执行。"},
                ],
                "coverage": {
                    "missing_topics": [],
                },
            }
        if result.state_patch.get("analysis_plan"):
            return {
                "status": "needs_harness",
                "headline": "数据分析计划已生成",
                "summary": "当前返回的是结构化分析计划，不是最终经营结论。",
                "next_action": "run_sql_harness",
                "node_notes": [
                    {"node": "business_knowledge.search", "meaning": "提取业务口径和计算方法。"},
                    {"node": "schema.list_tables", "meaning": "列出可见表。"},
                    {"node": "semantic_model.search", "meaning": "加载字段语义并筛选候选指标列。"},
                    {"node": "schema.related_tables", "meaning": "补充表关系，便于分步执行。"},
                    {"node": "analysis_plan.submit", "meaning": "把结构化计划交回 SQL Harness 审批和执行。"},
                ],
                "coverage": {
                    "missing_topics": [],
                },
            }
        return {
            "status": "completed",
            "headline": "分析已完成",
            "summary": "当前返回的是可阅读结论。",
            "next_action": "none",
            "node_notes": [],
            "coverage": {"missing_topics": []},
        }


async def _default_agentscope_runner(context: AgentScopeRunContext) -> AgentRunResult:
    """Lazy placeholder for the real AgentScope ReActAgent adapter."""

    if importlib.util.find_spec("agentscope") is None:
        raise RuntimeError(
            "AgentScope package is not installed. Install `agentscope` or inject a runner "
            "before enabling AgentScopeRuntime."
        )
    raise RuntimeError(
        "AgentScope package is installed, but the concrete ReActAgent adapter is not configured. "
        "Inject runner=... before enabling AgentScopeRuntime tasks."
    )


def _merge_dict_lists(left: list[JsonDict], right: list[JsonDict]) -> list[JsonDict]:
    merged: list[JsonDict] = []
    seen: set[str] = set()
    for item in [*left, *right]:
        row = dict(item)
        marker = repr(sorted(row.items()))
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(row)
    return merged


def _tool_trace_summary(tool_trace: list[JsonDict]) -> JsonDict:
    tool_counts: Counter[str] = Counter()
    provider_tool_counts: Counter[str] = Counter()
    cache_hit_counts: Counter[str] = Counter()
    for trace in tool_trace:
        tool_name = str(trace.get("tool_name") or "")
        if not tool_name:
            continue
        status = str(trace.get("status") or "")
        tool_counts[tool_name] += 1
        if status == "cache_hit":
            cache_hit_counts[tool_name] += 1
        elif status != "deduped":
            provider_tool_counts[tool_name] += 1

    duplicate_provider_tool_names = [
        tool_name
        for tool_name, count in sorted(provider_tool_counts.items())
        if count > 1
    ]
    return {
        "tool_counts": dict(sorted(tool_counts.items())),
        "provider_tool_counts": dict(sorted(provider_tool_counts.items())),
        "cache_hit_counts": dict(sorted(cache_hit_counts.items())),
        "duplicate_provider_tool_names": duplicate_provider_tool_names,
    }


def _llm_result(text: str):
    from langchain_core.outputs import Generation, LLMResult

    return LLMResult(generations=[[Generation(text=text)]])
