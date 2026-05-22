"""Optional AgentScope runner adapter plus a local compatible runner.

The real AgentScope package is intentionally optional at this stage.  The
local runner exercises the same ToolCatalog boundary so API/UI flows can be
tested without granting AgentScope direct SQL execution.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field

from agents.config.settings import settings
from agents.runtime.agentscope_runtime import AgentScopeRunContext
from agents.runtime.result import AgentRunResult, JsonDict

from agentscope.agent import ReActAgent
from agentscope.formatter import (
    GeminiChatFormatter,
    OpenAIChatFormatter,
)
from agentscope.message import Msg, TextBlock
from agentscope.model import (
    GeminiChatModel,
    OpenAIChatModel,
)
from agentscope.tool import Toolkit, ToolResponse


class AgentScopeAdapterUnavailable(RuntimeError):
    """Raised when the requested AgentScope backend cannot be constructed."""


class _DataAnalysisPlanOutput(BaseModel):
    """Structured output requested from the package AgentScope planner."""

    answer: str = Field(default="", description="Short planner summary for the user.")
    analysis_plan: JsonDict = Field(
        default_factory=dict,
        description="Structured analysis_plan to be submitted to SQL Harness.",
    )
    clarification_questions: list[str] = Field(
        default_factory=list,
        description="Questions if a plan cannot be generated.",
    )


def create_agentscope_runner(backend: str | None = None):
    """Create a runner for AgentScopeRuntime.

    ``auto`` is the default production-oriented mode: use the real AgentScope
    adapter when available, otherwise fall back to the local compatibility
    harness for development and CI.
    """

    selected = (backend or os.getenv("AGENTSCOPE_RUNTIME_BACKEND") or "auto").strip().lower()
    if selected in {"", "local", "compatible", "local_compatible"}:
        return LocalAgentScopeCompatibleRunner()
    if selected in {"agentscope", "real", "package"}:
        return AgentScopePackageRunner()
    if selected == "auto":
        try:
            importlib.import_module("agentscope")
        except ImportError:
            return LocalAgentScopeCompatibleRunner()
        return AgentScopePackageRunner()
    raise ValueError(f"Unsupported AgentScope runtime backend: {selected}")


@dataclass(slots=True)
class AgentScopePackageRunner:
    """Concrete AgentScope ReActAgent adapter."""

    model_factory: Callable[[], Any] | None = None
    formatter_factory: Callable[[], Any] | None = None
    agent_factory: Callable[..., Any] | None = None
    max_iters: int = 6

    def __post_init__(self) -> None:
        self._ensure_package()

    def _ensure_package(self) -> None:
        try:
            importlib.import_module("agentscope")
        except ImportError as exc:
            raise AgentScopeAdapterUnavailable(
                "AgentScope package is not installed. Install `agentscope` and set "
                "AGENTSCOPE_RUNTIME_BACKEND=agentscope to exercise the real adapter."
            ) from exc

    async def __call__(self, context: AgentScopeRunContext) -> AgentRunResult:
        toolkit = self._build_toolkit(context)
        agent = self._build_agent(context, toolkit)
        input_msg = Msg(name="user", role="user", content=self._build_initial_user_message(context))
        llm_span_name = f"agentscope.llm.{self._agent_name_for_task(context.task_type)}"
        llm_span = await context.start_llm_span(
            llm_span_name,
            context.query,
            metadata={
                "agent": self._agent_name_for_task(context.task_type),
                "runner_backend": "agentscope",
            },
        )
        try:
            reply = await self._call_agent(agent, input_msg, context)
        except Exception as exc:
            await context.end_llm_span(llm_span, llm_span_name, "", str(exc))
            return self._adapter_error_result(exc)
        await context.end_llm_span(
            llm_span,
            llm_span_name,
            reply.get_text_content() if hasattr(reply, "get_text_content") else str(reply),
        )
        result = self._convert_reply(reply, context=context)
        if context.task_type == "data_analysis":
            await self._submit_structured_analysis_plan(result, context)
        return result

    async def _call_agent(
        self,
        agent: Any,
        input_msg: Msg,
        context: AgentScopeRunContext,
    ) -> Msg:
        reply = await agent(input_msg)
        if context.task_type != "data_analysis" or not self._data_analysis_needs_evidence_retry(reply, context):
            return reply
        retry_msg = Msg(
            name="user",
            role="user",
            content=self._build_evidence_retry_message(context),
        )
        return await agent(retry_msg)

    def _data_analysis_needs_evidence_retry(
        self,
        reply: Msg,
        context: AgentScopeRunContext,
    ) -> bool:
        if context.tool_trace:
            return False
        result = self._convert_reply(reply, context=None)
        plan = result.state_patch.get("analysis_plan")
        if isinstance(plan, dict) and plan.get("steps"):
            return False
        return True

    def _build_evidence_retry_message(self, context: AgentScopeRunContext) -> str:
        return (
            f"用户问题: {context.query}\n\n"
            "上一轮没有任何 ToolCatalog 工具调用，也没有提交可执行 analysis_plan。"
            "这不满足 data_analysis_agent 的证据门槛。\n"
            "请保留自主决策，但必须先实际调用至少一个与你判断相关的工具，例如 "
            "query_context_rewrite、business_knowledge_search、sql_examples_search、"
            "schema_list_tables、schema_select_candidates、semantic_model_search、"
            "schema_related_tables、plan_assess_feasibility 或 current_time_now。"
            "不要只描述应该调用工具。\n"
            "取得证据后，如果足以规划，调用 analysis_plan_submit 提交 mode=analysis_plan 且 steps 非空的计划；"
            "如果仍需澄清，请基于已调用工具的证据提出澄清问题。"
        )

    def _build_initial_user_message(self, context: AgentScopeRunContext) -> str:
        if context.task_type == "data_analysis":
            tool_names = self._tool_name_instruction(context)
            return (
                f"用户问题: {context.query}\n\n"
                "你现在是数据分析规划 Agent。请自主决定是否需要 context rewrite、业务知识、SQL 示例、query enhance、"
                "schema、semantic model、候选表选择、表关系、可行性评估、当前时间或 SQL 本地校验，然后提交 analysis_plan。\n"
                "不要只描述要调用哪些工具；当工具输出是规划所需证据时必须实际调用工具。\n"
                "analysis_plan 必须包含 mode=analysis_plan 和非空 steps；如果无法规划，只返回澄清问题。\n"
                f"{tool_names}"
            )
        return context.query

    def _tool_name_instruction(self, context: AgentScopeRunContext) -> str:
        visible_names = [
            self._toolkit_func_name(tool.name)
            for tool in context.tools
        ]
        if not visible_names:
            return "当前没有可用工具。"
        lines = [
            "重要：调用工具时必须使用 AgentScope toolkit 暴露的函数名，不要使用带点号的内部工具名。",
            "可用函数名：" + ", ".join(visible_names),
        ]
        if "analysis_plan_submit" in visible_names:
            lines.append("完成规划后必须调用 analysis_plan_submit 提交结构化且 steps 非空的 analysis_plan。")
        return "\n".join(lines)

    def _workflow_context_packet(self, context: AgentScopeRunContext) -> str:
        state = context.workflow_state or {}
        sections: list[str] = []
        if state.get("selected_tables"):
            sections.append("候选表: " + ", ".join(str(item) for item in state.get("selected_tables", []) if str(item)))
        if state.get("table_relationships"):
            sections.append("已知表关系: " + self._short_json(state.get("table_relationships"), 1200))
        if state.get("evidence"):
            evidence = [str(item) for item in state.get("evidence", []) if str(item)]
            sections.append("业务证据:\n" + "\n---\n".join(evidence[:5]))
        if state.get("semantic_model"):
            sections.append("字段语义摘要: " + self._semantic_summary(state.get("semantic_model")))
        for key, label in (
            ("recall_context", "召回上下文"),
            ("feasibility_decision", "复杂度判断"),
            ("complexity_report", "复杂度报告"),
        ):
            if state.get(key):
                sections.append(f"{label}: {self._short_json(state.get(key), 800)}")
        return "\n\n".join(sections)

    def _semantic_summary(self, semantic_model: Any) -> str:
        if not isinstance(semantic_model, dict):
            return self._short_json(semantic_model, 800)
        rows = []
        for table, columns in semantic_model.items():
            names = []
            if isinstance(columns, dict):
                for column_name, meta in list(columns.items())[:8]:
                    if isinstance(meta, dict):
                        label = meta.get("business_name") or meta.get("column_comment") or column_name
                    else:
                        label = column_name
                    names.append(f"{column_name}({label})")
            rows.append(f"{table}: {', '.join(names)}")
            if len(rows) >= 8:
                break
        return "; ".join(rows)

    def _short_json(self, value: Any, limit: int) -> str:
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= limit else text[:limit] + "..."

    def _build_agent(
        self,
        context: AgentScopeRunContext,
        toolkit: Toolkit,
    ) -> Any:
        model = self._build_model()
        formatter = self._build_formatter()
        factory = self.agent_factory or ReActAgent
        agent = factory(
            name=self._agent_name_for_task(context.task_type),
            sys_prompt=self._system_prompt_for_agent(context),
            model=model,
            formatter=formatter,
            toolkit=toolkit,
            max_iters=self.max_iters,
        )
        if context.task_type == "data_analysis":
            self._guard_structured_finish_until_tool_evidence(agent, context)
        return agent

    def _guard_structured_finish_until_tool_evidence(
        self,
        agent: Any,
        context: AgentScopeRunContext,
    ) -> None:
        finish_name = getattr(agent, "finish_function_name", "generate_response")
        original = getattr(agent, finish_name, None)
        if not callable(original):
            return

        def _guarded_finish(**kwargs: Any) -> ToolResponse:
            if not self._has_successful_toolcatalog_evidence(context):
                return ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                "ToolCatalog evidence is required before structured output. "
                                "Call at least one available planning tool such as current_time_now, "
                                "business_knowledge_search, schema_list_tables, schema_select_candidates, "
                                "semantic_model_search, schema_related_tables, or plan_assess_feasibility, "
                                "then call generate_response or analysis_plan_submit."
                            ),
                        )
                    ],
                    metadata={
                        "success": False,
                        "structured_output": {},
                        "error": "missing_toolcatalog_evidence",
                    },
                )
            return original(**kwargs)

        _guarded_finish.__name__ = str(finish_name)
        _guarded_finish.__doc__ = getattr(original, "__doc__", None)
        setattr(agent, finish_name, _guarded_finish)

    def _has_successful_toolcatalog_evidence(self, context: AgentScopeRunContext) -> bool:
        return any(
            trace.get("status") == "success"
            and trace.get("tool_name") not in {"analysis_plan.submit", "sql_draft.submit"}
            for trace in context.tool_trace
        )

    def _system_prompt_for_agent(self, context: AgentScopeRunContext) -> str:
        return f"{context.system_prompt}\n\n{self._tool_name_instruction(context)}"

    def _build_model(self) -> Any:
        if self.model_factory is not None:
            return self.model_factory()
        provider = self._chat_model_provider()
        if provider == "openai":
            return OpenAIChatModel(
                model_name=settings.openai.chat_model,
                api_key=settings.openai.key or None,
            )
        if provider == "qwen":
            return OpenAIChatModel(
                model_name=settings.qwen.chat_model or "qwen-plus",
                api_key=settings.qwen.key or None,
                client_kwargs={"base_url": settings.qwen.base_url},
            )
        if provider == "deepseek":
            return OpenAIChatModel(
                model_name=settings.deepseek.chat_model or "deepseek-chat",
                api_key=settings.deepseek.key or None,
                client_kwargs={"base_url": settings.deepseek.base_url},
            )
        if provider == "gemini":
            return GeminiChatModel(
                model_name=settings.gemini.chat_model or "gemini-2.0-flash",
                api_key=settings.gemini.key or "",
            )
        return OpenAIChatModel(
            model_name=settings.ark.chat_model or "doubao-seed-2-0-code-preview-260215",
            api_key=settings.ark.key or None,
            client_kwargs={"base_url": "https://ark.cn-beijing.volces.com/api/v3"},
        )

    def _build_formatter(self) -> Any:
        if self.formatter_factory is not None:
            return self.formatter_factory()
        provider = self._chat_model_provider()
        if provider == "openai":
            return OpenAIChatFormatter()
        if provider == "qwen":
            return OpenAIChatFormatter()
        if provider == "gemini":
            return GeminiChatFormatter()
        return OpenAIChatFormatter()

    def _chat_model_provider(self) -> str:
        return (os.getenv("CHAT_MODEL_TYPE") or settings.chat_model_type or "ark").strip().lower()

    def _build_toolkit(self, context: AgentScopeRunContext) -> Toolkit:
        toolkit = Toolkit()
        for tool in context.tools:
            toolkit.register_tool_function(
                self._tool_wrapper(context, tool.name),
                func_name=self._toolkit_func_name(tool.name),
                func_description=tool.description,
                json_schema={
                    "type": "function",
                    "function": {
                        "name": self._toolkit_func_name(tool.name),
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                },
                namesake_strategy="override",
                async_execution=False,
            )
        return toolkit

    def _tool_wrapper(self, context: AgentScopeRunContext, tool_name: str):
        async def _runner(**kwargs: Any) -> ToolResponse:
            result = await context.invoke_tool(tool_name, kwargs)
            payload = result.output if result.ok else {"error": result.error}
            content = json.dumps(payload, ensure_ascii=False, default=str)
            return ToolResponse(
                content=[TextBlock(type="text", text=content)],
                metadata={
                    "ok": result.ok,
                    "tool_name": tool_name,
                    "trace": result.trace.to_dict(),
                    "output": payload,
                    "error": result.error,
                },
            )

        return _runner

    def _convert_reply(
        self,
        reply: Msg,
        *,
        context: AgentScopeRunContext | None = None,
    ) -> AgentRunResult:
        answer = reply.get_text_content() or ""
        metadata = dict(reply.metadata or {})
        result = AgentRunResult.from_value(metadata.get("structured_output") or metadata)
        if not result.answer:
            result.answer = answer
        if isinstance(metadata.get("sql_drafts"), list):
            result.sql_drafts = metadata["sql_drafts"]
        if isinstance(metadata.get("artifacts"), list):
            result.artifacts = metadata["artifacts"]
        if isinstance(metadata.get("risk_flags"), list):
            result.risk_flags = metadata["risk_flags"]
        if isinstance(metadata.get("state_patch"), dict):
            result.state_patch.update(metadata["state_patch"])
        if isinstance(metadata.get("analysis_plan"), dict):
            result.state_patch.setdefault("analysis_plan", metadata["analysis_plan"])
        if isinstance(metadata.get("clarification_questions"), list) and not result.clarification_questions:
            result.clarification_questions = [
                str(item) for item in metadata.get("clarification_questions", []) or []
            ]
        if not result.answer:
            result.answer = answer
        result.state_patch.setdefault("agentscope_backend", "agentscope")
        result.state_patch.setdefault("agentscope_reply_id", getattr(reply, "invocation_id", ""))
        if result.sql_drafts:
            result.state_patch.setdefault("requires_harness", True)
        if context is not None:
            self._merge_handoff_from_context(result, context)
        return result

    def _merge_handoff_from_context(
        self,
        result: AgentRunResult,
        context: AgentScopeRunContext,
    ) -> None:
        for trace in reversed(context.tool_trace):
            if trace.get("status") != "success":
                continue
            output = trace.get("output")
            if not isinstance(output, dict):
                continue
            if trace.get("tool_name") == "analysis_plan.submit" and isinstance(output.get("plan"), dict):
                result.state_patch["analysis_plan"] = output["plan"]
                result.state_patch["requires_harness"] = bool(output.get("requires_harness", True))
                result.state_patch["presentation"] = self._analysis_plan_presentation(output)
                break
            if trace.get("tool_name") == "sql_draft.submit" and isinstance(output.get("sql"), str):
                if not result.sql_drafts:
                    result.sql_drafts.append(dict(output))
                result.state_patch.setdefault("requires_harness", True)
                break

    async def _submit_structured_analysis_plan(
        self,
        result: AgentRunResult,
        context: AgentScopeRunContext,
    ) -> None:
        if self._has_successful_handoff(context, "analysis_plan.submit"):
            return
        plan = result.state_patch.get("analysis_plan")
        if not isinstance(plan, dict):
            plan = self._extract_analysis_plan_from_events(result)
        if not (isinstance(plan, dict) and plan.get("steps")):
            plan = self._recover_analysis_plan_from_failed_handoff(context)
        if not (isinstance(plan, dict) and plan.get("steps")):
            return

        submitted = await context.invoke_tool(
            "analysis_plan.submit",
            {
                "purpose": "AgentScope package structured output generated a data analysis plan for SQL Harness.",
                "plan": plan,
            },
        )
        if submitted.ok and isinstance(submitted.output, dict):
            result.state_patch["analysis_plan"] = submitted.output["plan"]
            result.state_patch["requires_harness"] = bool(submitted.output.get("requires_harness", True))
            result.state_patch.setdefault(
                "presentation",
                self._analysis_plan_presentation(submitted.output),
            )
        else:
            result.risk_flags.append(
                {
                    "code": "analysis_plan_submit_failed",
                    "severity": "error",
                    "message": submitted.error,
                }
            )

    def _has_successful_handoff(self, context: AgentScopeRunContext, tool_name: str) -> bool:
        return any(
            trace.get("tool_name") == tool_name and trace.get("status") == "success"
            for trace in context.tool_trace
        )

    def _extract_analysis_plan_from_events(self, result: AgentRunResult) -> JsonDict:
        for event in result.events:
            if isinstance(event.get("analysis_plan"), dict):
                return event["analysis_plan"]
            data = event.get("data")
            if isinstance(data, dict) and isinstance(data.get("analysis_plan"), dict):
                return data["analysis_plan"]
        return {}

    def _recover_analysis_plan_from_failed_handoff(self, context: AgentScopeRunContext) -> JsonDict:
        for trace in reversed(context.tool_trace):
            if trace.get("tool_name") != "analysis_plan.submit" or trace.get("status") == "success":
                continue
            payload = trace.get("input")
            if not isinstance(payload, dict):
                continue
            text = self._handoff_payload_text(payload)
            tables = self._recover_handoff_tables(context, payload, text)
            if not tables:
                continue
            return self._recovered_data_analysis_plan(context.query, tables, text)
        return {}

    def _handoff_payload_text(self, payload: JsonDict) -> str:
        text = str(payload.get("analysis_plan") or payload.get("plan_text") or "").strip()
        plan = payload.get("plan")
        if isinstance(plan, dict):
            parts = [
                str(plan.get("reason") or ""),
                json.dumps(plan.get("steps") or [], ensure_ascii=False, default=str),
            ]
            text = "\n".join(part for part in parts if part.strip()) or text
        return text

    def _recover_handoff_tables(
        self,
        context: AgentScopeRunContext,
        payload: JsonDict,
        text: str,
    ) -> list[str]:
        visible = self._visible_tables(context)
        sources: list[str] = []
        if text:
            sources.append(text)
        plan = payload.get("plan")
        if isinstance(plan, dict):
            sources.append(json.dumps(plan, ensure_ascii=False, default=str))
        for source in sources:
            tables = self._tables_from_text(source, visible)
            if tables:
                return tables
        traced_tables = self._tables_from_successful_tool_traces(context, visible)
        if traced_tables:
            return traced_tables
        return []

    def _visible_tables(self, context: AgentScopeRunContext) -> list[str]:
        security_context = context.security_context
        if isinstance(security_context, dict):
            allowed = security_context.get("allowed_tables")
        else:
            allowed = getattr(security_context, "allowed_tables", None)
        if not isinstance(allowed, list):
            return []
        return [str(table).strip() for table in allowed if str(table).strip()]

    def _tables_from_text(self, text: str, fallback_tables: list[str]) -> list[str]:
        candidates = re.findall(r"`([^`]+)`", text)
        candidates.extend(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", text))
        fallback_set = set(fallback_tables)
        selected = []
        for item in candidates:
            table = item.strip()
            if not table:
                continue
            if fallback_set and table not in fallback_set:
                continue
            if table not in selected:
                selected.append(table)
        if selected:
            return selected[:12]
        return []

    def _tables_from_successful_tool_traces(
        self,
        context: AgentScopeRunContext,
        visible_tables: list[str],
    ) -> list[str]:
        visible_set = set(visible_tables)
        selected: list[str] = []
        for trace in context.tool_trace:
            if trace.get("status") != "success":
                continue
            if trace.get("tool_name") not in {"semantic_model.search", "schema.related_tables", "schema.list_tables"}:
                continue
            output = trace.get("output")
            if not isinstance(output, dict):
                continue
            candidates: list[str] = []
            if isinstance(output.get("tables"), list):
                for row in output.get("tables") or []:
                    if isinstance(row, dict):
                        candidates.append(str(row.get("table_name") or ""))
                    else:
                        candidates.append(str(row))
            if isinstance(output.get("relationships"), list):
                for row in output.get("relationships") or []:
                    if not isinstance(row, dict):
                        continue
                    candidates.extend([str(row.get("from_table") or ""), str(row.get("to_table") or "")])
            for table in candidates:
                table_name = table.strip()
                if not table_name:
                    continue
                if visible_set and table_name not in visible_set:
                    continue
                if table_name not in selected:
                    selected.append(table_name)
        return selected[:12]

    def _recovered_data_analysis_plan(self, query: str, table_names: list[str], source_text: str) -> JsonDict:
        return {
            "mode": "analysis_plan",
            "reason": (
                "AgentScope package 调用了 analysis_plan_submit，但提交的是自然语言草稿。"
                "适配层已从草稿中提取候选表并转换为结构化计划，仍需 SQL Harness 审批执行。"
            ),
            "steps": [
                {
                    "step": 1,
                    "type": "sql",
                    "goal": f"基于候选表回答数据分析问题：{query}",
                    "tables": table_names,
                    "depends_on": [],
                    "merge_keys": ["period"],
                },
                {
                    "step": 2,
                    "type": "python_merge",
                    "goal": "按 SQL Harness 生成结果中的公共维度合并实际、预算、回款和费用数据。",
                    "tables": [],
                    "depends_on": [1],
                    "merge_keys": ["period"],
                },
                {
                    "step": 3,
                    "type": "report",
                    "goal": "基于已执行结果生成用户可读的关系分析报告，不在 Planner 阶段编造结论。",
                    "tables": [],
                    "depends_on": [2],
                    "merge_keys": [],
                },
            ],
            "requires_user_confirmation": True,
            "planner_source": "recovered_from_markdown_handoff",
            "source_excerpt": source_text[:1200],
        }

    def _analysis_plan_presentation(self, submitted: JsonDict) -> JsonDict:
        return {
            "status": "needs_harness",
            "headline": "数据分析计划已生成",
            "summary": "当前返回的是结构化分析计划，不是最终经营结论。",
            "next_action": "run_sql_harness",
            "node_notes": [
                {"node": "analysis_plan.submit", "meaning": f"提交计划 {submitted.get('plan_id', '')} 给 SQL Harness。"},
            ],
            "coverage": {"missing_topics": []},
        }

    def _adapter_error_result(self, exc: Exception) -> AgentRunResult:
        return AgentRunResult(
            answer="",
            risk_flags=[
                {
                    "code": "agentscope_adapter_error",
                    "severity": "error",
                    "message": str(exc),
                }
            ],
        )

    def _agent_name_for_task(self, task_type: str) -> str:
        if task_type == "data_analysis":
            return "data_analysis_agent"
        if task_type == "report_generation":
            return "report_agent"
        if task_type == "complex_analysis":
            return "complex_analysis_agent"
        return "common_analysis_agent"

    def _toolkit_func_name(self, tool_name: str) -> str:
        return tool_name.replace(".", "_")


@dataclass(slots=True)
class LocalAgentScopeCompatibleRunner:
    """Deterministic ToolCatalog-driven runner for complex-analysis testing."""

    max_tables: int = 12

    async def __call__(self, context: AgentScopeRunContext) -> AgentRunResult:
        if context.task_type == "data_analysis":
            return await self._run_data_analysis(context)
        if context.task_type == "complex_analysis":
            return await self._run_complex_analysis(context)
        if context.task_type == "exploratory_analysis":
            return await self._run_exploratory_analysis(context)
        if context.task_type == "report_generation":
            return await self._run_report_generation(context)
        return AgentRunResult(
            risk_flags=[
                {
                    "code": "unsupported_local_runner_task",
                    "severity": "warning",
                    "task_type": context.task_type,
                }
            ]
        )

    async def _run_data_analysis(self, context: AgentScopeRunContext) -> AgentRunResult:
        knowledge = await context.invoke_tool(
            "business_knowledge.search",
            {"query": context.query, "top_k": 5},
        )
        tables = await context.invoke_tool("schema.list_tables", {})
        table_names = self._visible_table_names(tables.output)
        if not table_names:
            return AgentRunResult(
                answer="当前没有可见数据表，无法生成数据分析计划。请确认数据权限或补充可分析的数据源。",
                clarification_questions=["请确认当前用户是否具备目标数据表权限。"],
                state_patch={
                    "agentscope_backend": "local_compatible",
                    "requires_harness": False,
                    "candidate_tables": [],
                },
                risk_flags=[
                    {
                        "code": "local_runner_no_visible_tables",
                        "severity": "warning",
                        "message": "schema.list_tables returned no visible tables for data_analysis.",
                    }
                ],
            )

        selected_tables = self._rank_data_analysis_tables(
            context.query,
            table_names,
            self._visible_table_metadata(tables.output),
            knowledge.output,
        )
        semantic = await context.invoke_tool(
            "semantic_model.search",
            {"table_names": selected_tables},
        )
        selected_tables = self._rank_data_analysis_tables(
            context.query,
            selected_tables,
            self._visible_table_metadata(tables.output),
            knowledge.output,
            semantic.output,
        )
        relationships = await context.invoke_tool(
            "schema.related_tables",
            {"table_names": selected_tables},
        )
        plan = self._data_analysis_plan(
            context.query,
            selected_tables,
            semantic.output,
            relationships.output,
            knowledge.output,
        )
        submitted = await context.invoke_tool(
            "analysis_plan.submit",
            {
                "purpose": "本地兼容 runner 生成结构化数据分析计划，供 SQL Harness 审批执行。",
                "plan": plan,
            },
        )
        if not submitted.ok:
            return AgentRunResult(
                answer="AgentScope 数据分析计划生成失败：分析计划未能提交给 SQL Harness。",
                state_patch={
                    "agentscope_backend": "local_compatible",
                    "candidate_tables": selected_tables,
                    "requires_harness": True,
                },
                risk_flags=[
                    {
                        "code": "analysis_plan_submit_failed",
                        "severity": "error",
                        "message": submitted.error,
                    }
                ],
            )

        presentation = self._build_data_presentation(selected_tables, submitted.output)
        return AgentRunResult(
            answer=self._format_data_analysis_answer(selected_tables, submitted.output, presentation),
            state_patch={
                "agentscope_backend": "local_compatible",
                "analysis_plan": submitted.output["plan"],
                "candidate_tables": selected_tables,
                "requires_harness": True,
                "presentation": presentation,
            },
            risk_flags=[
                {
                    "code": "local_compatible_runner",
                    "severity": "info",
                    "message": "This run used the ToolCatalog-compatible local data-analysis runner.",
                }
            ],
        )

    async def _run_complex_analysis(self, context: AgentScopeRunContext) -> AgentRunResult:
        table_names = self._workflow_selected_tables(context)
        if not table_names:
            return AgentRunResult(
                answer=(
                    "当前未运行真实 AgentScope，local runner 不会根据业务词硬编码生成复杂分析计划。"
                    "请启用 AgentScope package runner，或先由 SQLReact 提供 selected_tables 后再进行本地 smoke test。"
                ),
                clarification_questions=["请确认 AgentScope 后端配置，或先完成 SQLReact 表选择。"],
                state_patch={
                    "agentscope_backend": "local_compatible",
                    "candidate_tables": [],
                    "requires_harness": False,
                },
                risk_flags=[
                    {
                        "code": "local_runner_no_context",
                        "severity": "warning",
                        "message": (
                            "AgentScope package is unavailable or local runner was explicitly selected; "
                            "local runner requires workflow_state.selected_tables and contains no business-topic routing."
                        ),
                    }
                ],
            )

        semantic = await context.invoke_tool("semantic_model.search", {"table_names": table_names})
        await context.invoke_tool("schema.related_tables", {"table_names": table_names})

        sql = self._build_draft_sql(table_names, semantic.output, context.query)
        draft = await context.invoke_tool(
            "sql_draft.submit",
            {
                "sql": sql,
                "purpose": "基于 SQLReact 已选表生成本地兼容 runner 的 SQL 草稿，供 SQL Harness 审批执行。",
                "tables": table_names,
            },
        )

        if not draft.ok:
            return AgentRunResult(
                answer="AgentScope 复杂分析计划生成失败：SQL 草稿未能提交给 SQL Harness。",
                risk_flags=[
                    {
                        "code": "sql_draft_submit_failed",
                        "severity": "error",
                        "message": draft.error,
                    }
                ],
                state_patch={
                    "agentscope_backend": "local_compatible",
                    "candidate_tables": table_names,
                    "requires_harness": True,
                },
            )

        analysis_plan = self._analysis_plan(table_names, draft.output["draft_id"])
        presentation = self._build_presentation(table_names)
        answer = self._format_complex_answer(table_names, analysis_plan, presentation)
        return AgentRunResult(
            answer=answer,
            sql_drafts=[draft.output],
            state_patch={
                "agentscope_backend": "local_compatible",
                "analysis_plan": analysis_plan,
                "candidate_tables": table_names,
                "requires_harness": True,
                "presentation": presentation,
            },
            risk_flags=[
                {
                    "code": "local_compatible_runner",
                    "severity": "info",
                    "message": "The real AgentScope package is optional; this run used the ToolCatalog-compatible local runner.",
                }
            ],
        )

    async def _run_exploratory_analysis(self, context: AgentScopeRunContext) -> AgentRunResult:
        tables = await context.invoke_tool("schema.list_tables", {})
        table_names = self._visible_table_names(tables.output)
        if table_names:
            await context.invoke_tool("schema.related_tables", {"table_names": table_names})
        return AgentRunResult(
            answer="可见数据表：" + "、".join(table_names or []),
            state_patch={
                "agentscope_backend": "local_compatible",
                "candidate_tables": table_names,
            },
        )

    async def _run_report_generation(self, context: AgentScopeRunContext) -> AgentRunResult:
        rendered = await context.invoke_tool(
            "report.render",
            {"title": "AgentScope 分析报告", "include_echarts": True},
        )
        if not rendered.ok:
            return AgentRunResult(
                answer="AgentScope 报告生成失败。",
                risk_flags=[
                    {
                        "code": "report_render_failed",
                        "severity": "error",
                        "message": rendered.error,
                    }
                ],
            )
        return AgentRunResult(
            answer=rendered.output["markdown"],
            artifacts=[
                {
                    "type": "markdown_report",
                    "content": rendered.output["markdown"],
                    "source_artifact_ids": rendered.output["source_artifact_ids"],
                },
                *rendered.output["echarts"],
            ],
            state_patch={"agentscope_backend": "local_compatible"},
        )

    def _workflow_selected_tables(self, context: AgentScopeRunContext) -> list[str]:
        selected = [
            str(table).strip()
            for table in (context.workflow_state or {}).get("selected_tables", []) or []
            if str(table).strip()
        ]
        if not selected:
            return []
        security = context.security_context if isinstance(context.security_context, dict) else {}
        allowed = security.get("allowed_tables") if isinstance(security, dict) else None
        denied = set(security.get("denied_tables") or []) if isinstance(security, dict) else set()
        if allowed is not None:
            allowed_set = set(str(table) for table in allowed)
            selected = [table for table in selected if table in allowed_set]
        return [table for table in selected if table not in denied][: self.max_tables]

    def _visible_table_names(self, list_tables_output: Any) -> list[str]:
        rows = list_tables_output.get("tables", []) if isinstance(list_tables_output, dict) else []
        return [
            str(row.get("table_name", "")).strip()
            for row in rows
            if isinstance(row, dict) and str(row.get("table_name", "")).strip()
        ]

    def _visible_table_metadata(self, list_tables_output: Any) -> dict[str, str]:
        rows = list_tables_output.get("tables", []) if isinstance(list_tables_output, dict) else []
        metadata: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            table = str(row.get("table_name", "")).strip()
            if not table:
                continue
            metadata[table] = str(
                row.get("table_comment")
                or row.get("comment")
                or row.get("description")
                or ""
            )
        return metadata

    def _rank_data_analysis_tables(
        self,
        query: str,
        table_names: list[str],
        table_metadata: dict[str, str],
        knowledge_output: Any,
        semantic_output: Any | None = None,
    ) -> list[str]:
        if not table_names:
            return []
        related_tables = self._knowledge_related_tables(knowledge_output)
        if related_tables:
            evidence_selected = [table for table in table_names if table in related_tables]
            if evidence_selected:
                return evidence_selected[: self.max_tables]
        semantic_model = (
            semantic_output.get("semantic_model", {})
            if isinstance(semantic_output, dict)
            else {}
        )
        query_terms = self._rank_terms(query)
        original_index = {table: index for index, table in enumerate(table_names)}
        scored = []
        for table in table_names:
            score = 0.0
            table_text = " ".join(
                part for part in [
                    table,
                    table_metadata.get(table, ""),
                    self._semantic_text(semantic_model.get(table, {})),
                ] if part
            )
            for term in query_terms:
                if term and term in table_text:
                    score += max(1.0, min(float(len(term)), 6.0))
            if table in related_tables:
                score += 100.0
            scored.append((score, original_index[table], table))

        positive = [item for item in scored if item[0] > 0]
        if not positive:
            return table_names[: self.max_tables]
        positive.sort(key=lambda item: (-item[0], item[1]))
        return [table for _score, _index, table in positive[: self.max_tables]]

    def _knowledge_related_tables(self, knowledge_output: Any) -> set[str]:
        if not isinstance(knowledge_output, dict):
            return set()
        tables: set[str] = set()
        for row in knowledge_output.get("results", []) or []:
            if not isinstance(row, dict):
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            raw_values = [
                metadata.get("related_tables"),
                metadata.get("tables"),
                metadata.get("table_names"),
            ]
            content = str(row.get("content") or "")
            if content:
                raw_values.append(self._extract_related_tables_line(content))
            for raw in raw_values:
                tables.update(self._split_table_names(raw))
        return tables

    def _extract_related_tables_line(self, content: str) -> str:
        lines = []
        for line in content.splitlines():
            if any(marker in line for marker in ("关联表", "相关表", "related_tables", "tables")):
                lines.append(line)
        return ",".join(lines)

    def _split_table_names(self, value: Any) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, (list, tuple, set)):
            raw_items = [str(item) for item in value]
        else:
            raw_items = re.split(r"[,，、\s]+", str(value))
        return {
            item.strip().strip("`")
            for item in raw_items
            if item and item.strip()
        }

    def _rank_terms(self, query: str) -> list[str]:
        terms = [term for term in re.split(r"[\s,，、。；;:：/\\()（）]+", query or "") if term]
        cjk_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", query or "")
        for chunk in cjk_chunks:
            terms.extend(chunk[index:index + 2] for index in range(max(0, len(chunk) - 1)))
            terms.extend(chunk[index:index + 3] for index in range(max(0, len(chunk) - 2)))
        latin_terms = re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}", query or "")
        terms.extend(latin_terms)
        seen: set[str] = set()
        ranked: list[str] = []
        for term in terms:
            normalized = term.strip()
            if len(normalized) < 2 or normalized in seen:
                continue
            seen.add(normalized)
            ranked.append(normalized)
        return ranked

    def _semantic_text(self, columns: Any) -> str:
        if isinstance(columns, dict):
            rows = columns.values()
        elif isinstance(columns, list):
            rows = columns
        else:
            return ""
        parts = []
        for meta in rows:
            if not isinstance(meta, dict):
                continue
            parts.extend(
                str(meta.get(key) or "")
                for key in (
                    "column_name",
                    "business_name",
                    "synonyms",
                    "business_description",
                    "column_comment",
                )
            )
        return " ".join(part for part in parts if part)

    def _data_analysis_plan(
        self,
        query: str,
        table_names: list[str],
        semantic_output: Any,
        relationships_output: Any,
        knowledge_output: Any,
    ) -> JsonDict:
        semantic_model = (
            semantic_output.get("semantic_model", {})
            if isinstance(semantic_output, dict)
            else {}
        )
        relationships = (
            relationships_output.get("relationships", [])
            if isinstance(relationships_output, dict)
            else []
        )
        knowledge_count = len(knowledge_output.get("results", [])) if isinstance(knowledge_output, dict) else 0
        tables_comment = ", ".join(table_names)
        relation_note = f"已发现 {len(relationships)} 条表关系" if relationships else "未发现显式表关系"
        semantic_note = self._semantic_plan_note(semantic_model)
        return {
            "mode": "analysis_plan",
            "reason": (
                "本地兼容 runner 仅用于验证 AgentScope Planner 到 SQL Harness 的交接链路；"
                f"已读取 {knowledge_count} 条业务知识，候选表为 {tables_comment}，{relation_note}，{semantic_note}。"
            ),
            "steps": [
                {
                    "step": 1,
                    "type": "sql",
                    "goal": f"围绕用户问题生成第一步可审批 SQL：{query}",
                    "tables": table_names,
                    "depends_on": [],
                    "merge_keys": [],
                },
                {
                    "step": 2,
                    "type": "report",
                    "goal": "基于 SQL Harness 执行结果生成可读关系分析，不在 Planner 阶段编造结论。",
                    "tables": table_names,
                    "depends_on": [1],
                    "merge_keys": [],
                },
            ],
            "requires_user_confirmation": True,
        }

    def _semantic_plan_note(self, semantic_model: Any) -> str:
        if not isinstance(semantic_model, dict) or not semantic_model:
            return "字段语义为空"
        column_count = 0
        for columns in semantic_model.values():
            if isinstance(columns, dict):
                column_count += len(columns)
            elif isinstance(columns, list):
                column_count += len(columns)
        return f"字段语义覆盖 {len(semantic_model)} 张表/{column_count} 个字段"

    def _build_data_presentation(self, table_names: list[str], submitted: JsonDict) -> dict[str, Any]:
        return {
            "status": "needs_harness",
            "headline": "数据分析计划已生成",
            "summary": "当前返回的是结构化分析计划，不是最终经营结论。",
            "next_action": "run_sql_harness",
            "coverage": {
                "selected_tables": table_names,
                "missing_topics": [],
            },
            "node_notes": [
                {"node": "business_knowledge.search", "meaning": "读取业务口径和历史知识。"},
                {"node": "schema.list_tables", "meaning": "列出当前用户可见表。"},
                {"node": "semantic_model.search", "meaning": "读取候选表字段语义。"},
                {"node": "schema.related_tables", "meaning": "读取候选表关系。"},
                {"node": "analysis_plan.submit", "meaning": f"提交计划 {submitted.get('plan_id', '')} 给 SQL Harness。"},
            ],
        }

    def _format_data_analysis_answer(
        self,
        table_names: list[str],
        submitted: JsonDict,
        presentation: dict[str, Any],
    ) -> str:
        plan = submitted.get("plan", {}) if isinstance(submitted, dict) else {}
        steps = plan.get("steps", []) if isinstance(plan, dict) else []
        step_lines = "\n".join(
            f"{step.get('step')}. {step.get('goal')}"
            for step in steps
            if isinstance(step, dict)
        )
        return (
            "AgentScope 数据分析计划已生成。\n"
            f"候选表：{'、'.join(table_names)}。\n"
            f"当前状态：{presentation.get('status', 'needs_harness')}。\n"
            "当前还不是最终经营结论，不能把计划内容当作执行事实。\n"
            "下一步：交回 SQL Harness 完成 validate_analysis_plan、safety_check、authorize_sql、approve、execute_sql、merge_report。\n"
            f"{step_lines}"
        )

    def _build_draft_sql(
        self,
        table_names: list[str],
        semantic_output: Any,
        query: str,
    ) -> str:
        selects = [
            f"select '{table}' as table_name, count(*) as row_count from {table}"
            for table in table_names
        ]
        return "\nunion all\n".join(selects)

    def _analysis_plan(self, table_names: list[str], draft_id: str) -> list[JsonDict]:
        return [
            {
                "step": 1,
                "type": "schema_research",
                "goal": "复用 SQLReact 已选表和字段语义，确认本地兼容 runner 的 SQL 草稿输入。",
                "tables": table_names,
            },
            {
                "step": 2,
                "type": "sql_draft",
                "goal": "生成最小 SQL 草稿并提交 SQL Harness 审批执行。",
                "draft_id": draft_id,
                "tables": table_names,
                "merge_keys": ["period"],
            },
            {
                "step": 3,
                "type": "report",
                "goal": "等待 SQL Harness 执行事实后再生成业务结论。",
                "depends_on": [2],
            },
        ]

    def _build_presentation(
        self,
        table_names: list[str],
    ) -> dict[str, Any]:
        return {
            "status": "needs_harness",
            "headline": "分析计划已生成",
            "summary": "当前返回的是待 SQL Harness 执行的分析计划，不是最终经营结论。",
            "next_action": "run_sql_harness",
            "coverage": {
                "selected_tables": table_names,
                "missing_topics": [],
            },
            "node_notes": [
                {
                    "node": "semantic_model.search",
                    "meaning": "读取 SQLReact 已选表的字段语义。",
                },
                {
                    "node": "schema.related_tables",
                    "meaning": "补充已选表之间的关联线索。",
                },
                {
                    "node": "sql_draft.submit",
                    "meaning": "把草稿交回 SQL Harness 做安全检查和执行。",
                },
            ],
        }

    def _format_complex_answer(
        self,
        table_names: list[str],
        analysis_plan: list[JsonDict],
        presentation: dict[str, Any],
    ) -> str:
        steps = "\n".join(
            f"{step['step']}. {step['goal']}" for step in analysis_plan
        )
        node_notes = "；".join(
            f"{note['node']}：{note['meaning']}"
            for note in presentation.get("node_notes", [])
        )
        return (
            "AgentScope 复杂分析计划已生成。\n"
            f"候选表：{'、'.join(table_names)}。\n"
            f"当前状态：{presentation.get('status', 'needs_harness')}。\n"
            f"下一步：{presentation.get('next_action', 'run_sql_harness')}。\n"
            f"节点说明：{node_notes}。\n"
            f"{steps}\n"
            "当前还不是最终经营结论，SQL 草稿已提交为 draft_only，必须回到 SQL Harness 完成 "
            "safety_check、authorize_sql、approve、execute_sql 后才能形成执行事实。"
        )
