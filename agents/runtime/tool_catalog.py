"""Controlled tool registry for future AgentScope runtimes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import inspect
import json
import logging
import re
from typing import Any

from langchain_core.documents import Document

from agents.rag.retriever import (
    get_semantic_model_by_tables,
    get_table_relationships,
    load_full_table_metadata,
    recall_business_knowledge,
)
from agents.runtime.tool_contracts import (
    RuntimeTool,
    ToolCallResult,
    ToolContract,
    ToolTrace,
)
from agents.tool.security.policies import SecurityContext, authorize_tables

logger = logging.getLogger(__name__)

_TASK_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "exploratory_analysis": (
        "semantic_model.search",
        "business_knowledge.search",
        "schema.list_tables",
        "schema.describe_table",
        "schema.related_tables",
        "current_time.now",
    ),
    "complex_analysis": (
        "semantic_model.search",
        "business_knowledge.search",
        "schema.list_tables",
        "schema.describe_table",
        "schema.related_tables",
        "current_time.now",
        "sql_draft.submit",
    ),
    "data_analysis": (
        "semantic_model.search",
        "business_knowledge.search",
        "schema.list_tables",
        "schema.describe_table",
        "schema.related_tables",
        "current_time.now",
        "analysis_plan.submit",
    ),
    "report_generation": (
        "artifact.read",
        "report.render",
    ),
}

_TOOL_ORDER: tuple[str, ...] = (
    "semantic_model.search",
    "business_knowledge.search",
    "schema.list_tables",
    "schema.describe_table",
    "schema.related_tables",
    "current_time.now",
    "artifact.read",
    "report.render",
    "sql_draft.submit",
    "analysis_plan.submit",
)


def _default_table_metadata_loader() -> list[dict]:
    return load_full_table_metadata()


def _default_semantic_model_loader(table_names: list[str]) -> dict[str, dict[str, dict]]:
    return get_semantic_model_by_tables(table_names)


def _default_relationship_loader(table_names: list[str]) -> list[dict]:
    return get_table_relationships(table_names)


def _default_business_knowledge_search(query: str, top_k: int) -> list[Document]:
    return recall_business_knowledge(query, top_k=top_k)


def _default_time_provider() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ToolProviders:
    """Injectable data providers for ToolCatalog."""

    business_knowledge_search: Any = _default_business_knowledge_search
    table_metadata_loader: Any = _default_table_metadata_loader
    semantic_model_loader: Any = _default_semantic_model_loader
    table_relationship_loader: Any = _default_relationship_loader
    time_provider: Any = _default_time_provider

    @classmethod
    def default(cls) -> "ToolProviders":
        return cls()


@dataclass(slots=True)
class ToolCatalog:
    """Registry of read-only tools that can be exposed to AgentScope."""

    providers: ToolProviders = field(default_factory=ToolProviders.default)
    _contracts: dict[str, ToolContract] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._contracts: dict[str, ToolContract] = self._build_contracts()

    def _tool_description(
        self,
        *,
        purpose: str,
        boundary: str,
        required_input: str,
        output: str,
        negative: str,
    ) -> str:
        return (
            f"Purpose: {purpose}\n"
            f"Boundary: {boundary}\n"
            f"Required input: {required_input}\n"
            f"Output: {output}\n"
            f"Do not use when: {negative}"
        )

    def _readthrough_output_properties(self) -> dict[str, dict[str, Any]]:
        return {
            "source": {
                "type": "string",
                "description": (
                    "Optional data source marker. workflow_state means existing workflow context was reused; "
                    "mixed means workflow_state was reused and missing parts were fetched."
                ),
            },
            "cache_hit": {
                "type": "boolean",
                "description": (
                    "True when the tool returned fully from workflow_state without calling the backing provider."
                ),
            },
        }

    def _build_contracts(self) -> dict[str, ToolContract]:
        return {
            "semantic_model.search": ToolContract(
                name="semantic_model.search",
                description=self._tool_description(
                    purpose=(
                        "Load configured field semantics for visible tables so an analysis agent can understand "
                        "business names, synonyms, descriptions, and candidate columns before drafting SQL."
                    ),
                    boundary=(
                        "Read-only metadata access only. It filters requested table_names through the current "
                        "security context and may reuse workflow_state.semantic_model before loading missing tables."
                    ),
                    required_input=(
                        "table_names is optional but recommended. If omitted, the tool loads semantics for all "
                        "currently visible tables, which can be broader and slower."
                    ),
                    output=(
                        "An object with tables and semantic_model keyed by table name; cache metadata may appear "
                        "when workflow_state was reused."
                    ),
                    negative=(
                        "you already have sufficient semantic_model for the exact tables, you need row data, "
                        "or you intend to bypass SQL Harness authorization."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "table_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of table names to inspect.",
                        },
                    },
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Authorized table names covered by the returned semantic model.",
                        },
                        "semantic_model": {
                            "type": "object",
                            "description": "Semantic metadata keyed by table name and then column name.",
                        },
                        **self._readthrough_output_properties(),
                        "from_workflow_state": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tables whose semantic metadata came from workflow_state.",
                        },
                        "fetched": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tables loaded from the backing semantic model provider because context was missing.",
                        },
                    },
                },
            ),
            "business_knowledge.search": ToolContract(
                name="business_knowledge.search",
                description=self._tool_description(
                    purpose=(
                        "Find configured business terms, formulas, metric definitions, synonyms, and related-table "
                        "hints that clarify the user's analytical intent."
                    ),
                    boundary=(
                        "Read-only recall over business knowledge. It may reuse matching workflow_state evidence; "
                        "otherwise providers can query vector/BM25/lexical stores. It does not select final tables."
                    ),
                    required_input=(
                        "query is required and should be the user's current analytical question; top_k optionally "
                        "limits the number of returned evidence items."
                    ),
                    output=(
                        "An object with results, where each item has content and metadata such as source, score, "
                        "matched terms, or related_tables."
                    ),
                    negative=(
                        "the question is not about business meaning, workflow_state already has matching evidence, "
                        "or you need actual database rows."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Current user analysis question to match against configured business knowledge.",
                        },
                        "top_k": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum number of evidence items to return.",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "results": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Matched knowledge snippets with content and metadata for traceability.",
                        },
                        **self._readthrough_output_properties(),
                    },
                },
            ),
            "schema.list_tables": ToolContract(
                name="schema.list_tables",
                description=self._tool_description(
                    purpose=(
                        "List tables visible to the current user so an agent can understand the available schema "
                        "surface before narrowing candidates."
                    ),
                    boundary=(
                        "Read-only table metadata access filtered by security context. It may reuse complete "
                        "workflow_state.table_metadata, and it never grants access to denied tables."
                    ),
                    required_input=(
                        "No input is required; the current security context determines visibility."
                    ),
                    output=(
                        "An object with tables containing table_name and table comments or metadata."
                    ),
                    negative=(
                        "selected_tables already gives the needed scoped candidates, you need columns for one table, "
                        "or you need relationship edges."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "tables": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Visible table metadata after security filtering.",
                        },
                        **self._readthrough_output_properties(),
                    },
                },
            ),
            "schema.describe_table": ToolContract(
                name="schema.describe_table",
                description=self._tool_description(
                    purpose=(
                        "Inspect one visible table in detail, including its comment and semantic column metadata, "
                        "when a specific table needs clarification."
                    ),
                    boundary=(
                        "Read-only schema and semantic metadata for a single authorized table. It denies requests "
                        "for tables outside the current security context."
                    ),
                    required_input=(
                        "table_name is required and must be one visible physical table name."
                    ),
                    output=(
                        "An object with table_name, table_comment, and columns; each column includes available "
                        "semantic metadata."
                    ),
                    negative=(
                        "you need to inspect many tables at once, you already have semantic_model.search output, "
                        "or the table is not visible."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "One authorized physical table name to describe.",
                        },
                    },
                    "required": ["table_name"],
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Authorized table that was described.",
                        },
                        "table_comment": {
                            "type": "string",
                            "description": "Configured table comment or an empty string when unavailable.",
                        },
                        "columns": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Column-level schema and semantic metadata for the table.",
                        },
                    },
                },
            ),
            "schema.related_tables": ToolContract(
                name="schema.related_tables",
                description=self._tool_description(
                    purpose=(
                        "Return configured relationship edges among visible tables so a planner can reason about "
                        "safe joins, bridge tables, and decomposition dependencies."
                    ),
                    boundary=(
                        "Read-only relationship metadata filtered by table permissions. It may reuse "
                        "workflow_state.table_relationships when they cover the requested tables."
                    ),
                    required_input=(
                        "table_names is optional but recommended. Provide the candidate table set whose internal "
                        "relationships you need."
                    ),
                    output=(
                        "An object with relationships containing from_table/from_column/to_table/to_column and any "
                        "available relation metadata."
                    ),
                    negative=(
                        "you need field semantics, row data, or relationships involving unauthorized tables."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "table_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Candidate authorized table names whose internal relationships are needed.",
                        },
                    },
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "relationships": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Authorized relationship edges between requested visible tables.",
                        },
                        **self._readthrough_output_properties(),
                    },
                },
            ),
            "current_time.now": ToolContract(
                name="current_time.now",
                description=self._tool_description(
                    purpose=(
                        "Resolve the current date and time for relative-date interpretation in analysis text."
                    ),
                    boundary=(
                        "Read-only clock lookup. It does not query business calendars, fiscal periods, or database "
                        "data."
                    ),
                    required_input=(
                        "No input is required."
                    ),
                    output=(
                        "An object with iso, date, and timezone strings."
                    ),
                    negative=(
                        "the user supplied exact dates, fiscal calendar rules are needed, or the answer requires "
                        "data retrieval instead of clock context."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "iso": {
                            "type": "string",
                            "description": "Current timestamp in ISO 8601 format.",
                        },
                        "date": {
                            "type": "string",
                            "description": "Current calendar date in YYYY-MM-DD format.",
                        },
                        "timezone": {
                            "type": "string",
                            "description": "Timezone name reported by the time provider.",
                        },
                    },
                },
            ),
            "artifact.read": ToolContract(
                name="artifact.read",
                description=self._tool_description(
                    purpose=(
                        "Read existing executed results or analysis artifacts from workflow_state for report "
                        "generation without fetching new data."
                    ),
                    boundary=(
                        "Read-only access to artifacts already present in workflow_state. It must not call schema, "
                        "semantic, business-knowledge, or SQL execution paths."
                    ),
                    required_input=(
                        "artifact_ids and types are optional filters; omit both to read all available artifacts."
                    ),
                    output=(
                        "An object with artifacts, each preserving id, type, and content."
                    ),
                    negative=(
                        "you need new facts, missing database rows, or schema exploration; ask SQL Harness or the "
                        "appropriate read-only tool instead."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "artifact_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional artifact ids to read in order.",
                        },
                        "types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional artifact types to include.",
                        },
                    },
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "artifacts": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Workflow artifacts or executed results selected by the input filters.",
                        },
                    },
                },
            ),
            "report.render": ToolContract(
                name="report.render",
                description=self._tool_description(
                    purpose=(
                        "Render a Markdown report and optional chart artifacts from already executed workflow "
                        "results or analysis summaries."
                    ),
                    boundary=(
                        "Presentation-only transformation. It reads workflow_state artifacts and does not discover "
                        "schema, recall knowledge, draft SQL, or execute SQL."
                    ),
                    required_input=(
                        "title is optional; artifact_ids/types optionally scope inputs; include_echarts controls "
                        "whether simple chart configs are emitted."
                    ),
                    output=(
                        "An object with markdown, echarts, and source_artifact_ids."
                    ),
                    negative=(
                        "there are no executed result artifacts, the user asks for fresh analysis, or conclusions "
                        "would require data not present in workflow_state."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Optional Markdown report title.",
                        },
                        "artifact_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional artifact ids to include in the report.",
                        },
                        "types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional artifact types to include in the report.",
                        },
                        "include_echarts": {
                            "type": "boolean",
                            "description": "Whether to emit simple ECharts configs from metric-like artifacts.",
                        },
                    },
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "markdown": {
                            "type": "string",
                            "description": "Rendered Markdown report based only on available workflow artifacts.",
                        },
                        "echarts": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Optional chart configuration objects derived from available metrics.",
                        },
                        "source_artifact_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Artifact ids used as report inputs.",
                        },
                    },
                },
            ),
            "sql_draft.submit": ToolContract(
                name="sql_draft.submit",
                description=self._tool_description(
                    purpose=(
                        "Hand a proposed SELECT SQL draft back to SQL Harness for safety_check, authorize_sql, "
                        "approval, and execution."
                    ),
                    boundary=(
                        "This is a handoff tool, not an execution tool. It validates referenced tables against "
                        "permissions and always returns draft_only metadata."
                    ),
                    required_input=(
                        "sql is required. purpose should explain why the draft exists. tables should list every "
                        "referenced table so authorization can run before execution."
                    ),
                    output=(
                        "An object with draft_id, sql, purpose, tables, execution_mode=draft_only, "
                        "requires_harness=true, status, and harness_steps."
                    ),
                    negative=(
                        "the SQL is unsafe, non-SELECT, references unauthorized tables, or you expect immediate "
                        "query results."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "Proposed SELECT statement to hand off for SQL Harness review.",
                        },
                        "purpose": {
                            "type": "string",
                            "description": "Reason the SQL draft is needed for the current analysis.",
                        },
                        "tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Every physical table referenced by the SQL draft.",
                        },
                    },
                    "required": ["sql"],
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "draft_id": {
                            "type": "string",
                            "description": "Stable id for the submitted draft.",
                        },
                        "sql": {
                            "type": "string",
                            "description": "Submitted SELECT statement; it has not been executed by this tool.",
                        },
                        "purpose": {
                            "type": "string",
                            "description": "Caller-provided purpose for the SQL draft.",
                        },
                        "tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Caller-provided referenced tables after authorization.",
                        },
                        "execution_mode": {
                            "type": "string",
                            "description": "Always draft_only because execution belongs to SQL Harness.",
                        },
                        "requires_harness": {
                            "type": "boolean",
                            "description": "Always true; caller must route the draft through SQL Harness.",
                        },
                        "status": {
                            "type": "string",
                            "description": "Review status for the SQL Harness handoff.",
                        },
                        "harness_steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Required downstream SQL Harness steps before execution.",
                        },
                    },
                },
                read_only=False,
                direct_execution_allowed=False,
            ),
            "analysis_plan.submit": ToolContract(
                name="analysis_plan.submit",
                description=self._tool_description(
                    purpose=(
                        "Hand a structured data analysis plan back to SQL Harness. The plan can represent either "
                        "a one-step SQL query or a multi-step analysis with SQL, merge, and report stages."
                    ),
                    boundary=(
                        "This is a handoff tool, not an execution tool. It validates plan structure and referenced "
                        "tables against permissions, then returns plan_only metadata for SQL Harness."
                    ),
                    required_input=(
                        "plan is required and must contain mode=analysis_plan plus a non-empty steps array. "
                        "purpose should explain why this plan answers the current data task."
                    ),
                    output=(
                        "An object with plan_id, plan, purpose, execution_mode=plan_only, requires_harness=true, "
                        "status, and SQL Harness steps for validation, safety, authorization, approval, execution, "
                        "and readable report generation."
                    ),
                    negative=(
                        "you need immediate database rows, the plan references unauthorized tables, the user needs "
                        "clarification first, or you intend to bypass SQL Harness."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "plan": {
                            "type": "object",
                            "description": "Structured analysis_plan containing reason, steps, and optional SQL drafts.",
                        },
                        "purpose": {
                            "type": "string",
                            "description": "Why this analysis plan is needed for the current user data task.",
                        },
                    },
                    "required": ["plan"],
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "plan_id": {
                            "type": "string",
                            "description": "Stable id for the submitted analysis plan.",
                        },
                        "plan": {
                            "type": "object",
                            "description": "Submitted analysis plan; it has not been executed by this tool.",
                        },
                        "purpose": {
                            "type": "string",
                            "description": "Caller-provided purpose for the analysis plan.",
                        },
                        "execution_mode": {
                            "type": "string",
                            "description": "Always plan_only because execution belongs to SQL Harness.",
                        },
                        "requires_harness": {
                            "type": "boolean",
                            "description": "Always true; caller must route the plan through SQL Harness.",
                        },
                        "status": {
                            "type": "string",
                            "description": "Review status for the SQL Harness handoff.",
                        },
                        "harness_steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Required downstream SQL Harness stages before user-facing execution facts.",
                        },
                    },
                },
                read_only=False,
                direct_execution_allowed=False,
            ),
        }

    def get_contract(self, tool_name: str) -> ToolContract:
        return self._contracts[tool_name]

    def get_tools(
        self,
        task_type: str,
        security_context: SecurityContext | dict | None = None,
    ) -> list[RuntimeTool]:
        allowed_names = _TASK_ALLOWLISTS.get(task_type, ())
        if not allowed_names:
            return []

        visible_tables = self._visible_tables_for_task(security_context)
        tools: list[RuntimeTool] = []
        for tool_name in _TOOL_ORDER:
            if tool_name not in allowed_names:
                continue
            if tool_name in {"semantic_model.search", "schema.describe_table", "schema.related_tables"}:
                if visible_tables is not None and not visible_tables:
                    continue
            tools.append(RuntimeTool(contract=self.get_contract(tool_name), task_types=(task_type,)))
        return tools

    def _visible_tables_for_task(self, security_context: SecurityContext | dict | None) -> list[str] | None:
        context = SecurityContext.from_dict(security_context)
        if context.allowed_tables is None:
            if context.denied_tables:
                return None
            return None
        allowed = [table for table in context.allowed_tables if table not in set(context.denied_tables)]
        return allowed

    async def invoke(
        self,
        tool_name: str,
        payload: dict[str, Any] | None,
        *,
        task_type: str,
        security_context: SecurityContext | dict | None = None,
        session_id: str = "",
        thread_id: str = "",
        workflow_state: dict[str, Any] | None = None,
    ) -> ToolCallResult:
        contract = self.get_contract(tool_name)
        context = SecurityContext.from_dict(security_context)
        payload = dict(payload or {})
        workflow_state = dict(workflow_state or {})
        started = datetime.now(timezone.utc)

        if tool_name not in _TASK_ALLOWLISTS.get(task_type, ()):
            trace = self._build_trace(
                tool_name,
                task_type,
                context,
                session_id=session_id,
                thread_id=thread_id,
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                status="forbidden",
                input_payload=payload,
                output=None,
                error=f"tool {tool_name} is not allowed for task_type {task_type}",
            )
            return ToolCallResult(ok=False, output=None, error=trace.error, trace=trace)

        try:
            output = await self._dispatch(tool_name, payload, context, workflow_state)
            ended = datetime.now(timezone.utc)
            trace = self._build_trace(
                tool_name,
                task_type,
                context,
                session_id=session_id,
                thread_id=thread_id,
                started_at=started,
                ended_at=ended,
                status="success",
                input_payload=payload,
                output=output,
                error="",
            )
            return ToolCallResult(ok=True, output=output, error="", trace=trace)
        except PermissionError as exc:
            ended = datetime.now(timezone.utc)
            trace = self._build_trace(
                tool_name,
                task_type,
                context,
                session_id=session_id,
                thread_id=thread_id,
                started_at=started,
                ended_at=ended,
                status="denied",
                input_payload=payload,
                output=None,
                error=str(exc),
            )
            return ToolCallResult(ok=False, output=None, error=str(exc), trace=trace)
        except Exception as exc:
            ended = datetime.now(timezone.utc)
            trace = self._build_trace(
                tool_name,
                task_type,
                context,
                session_id=session_id,
                thread_id=thread_id,
                started_at=started,
                ended_at=ended,
                status="error",
                input_payload=payload,
                output=None,
                error=str(exc),
            )
            return ToolCallResult(ok=False, output=None, error=str(exc), trace=trace)

    async def _dispatch(
        self,
        tool_name: str,
        payload: dict[str, Any],
        context: SecurityContext,
        workflow_state: dict[str, Any],
    ) -> Any:
        if tool_name == "business_knowledge.search":
            query = str(payload.get("query", "")).strip()
            top_k = int(payload.get("top_k", 5) or 5)
            cached = self._business_knowledge_from_workflow_state(query, top_k, workflow_state)
            if cached is not None:
                return cached
            docs = await self._call_provider(self.providers.business_knowledge_search, query=query, top_k=top_k)
            return {
                "results": [
                    {
                        "content": doc.page_content,
                        "metadata": dict(doc.metadata or {}),
                    }
                    for doc in docs
                ]
            }

        if tool_name == "current_time.now":
            current = await self._call_provider(self.providers.time_provider)
            return {
                "iso": current.isoformat(),
                "date": current.date().isoformat(),
                "timezone": current.tzname() or "UTC",
            }

        if tool_name == "schema.list_tables":
            cached = self._table_metadata_from_workflow_state(context, workflow_state)
            if cached is not None:
                return cached
            metadata = await self._call_provider(self.providers.table_metadata_loader)
            allowed = self._filter_table_metadata(metadata, context)
            return {"tables": allowed}

        if tool_name == "schema.describe_table":
            table_name = str(payload.get("table_name", "")).strip()
            if not table_name:
                raise ValueError("table_name is required")
            metadata = await self._call_provider(self.providers.table_metadata_loader)
            metadata_map = {row.get("table_name", ""): row for row in metadata}
            table_comment = metadata_map.get(table_name, {}).get("table_comment", "")
            visible = authorize_tables([table_name], context, table_metadata={table_name: table_comment}, stage="schema.describe_table")
            if not visible.allowed:
                raise PermissionError(visible.message or f"table {table_name} is not allowed")
            semantic_model = await self._call_provider(self.providers.semantic_model_loader, [table_name])
            columns = self._semantic_model_columns(table_name, semantic_model)
            return {
                "table_name": table_name,
                "table_comment": table_comment,
                "columns": columns,
            }

        if tool_name == "semantic_model.search":
            requested = [str(item).strip() for item in payload.get("table_names", []) if str(item).strip()]
            if not requested:
                requested = self._workflow_selected_tables(workflow_state)
            if not requested:
                metadata = await self._call_provider(self.providers.table_metadata_loader)
                requested = [row.get("table_name", "") for row in self._filter_table_metadata(metadata, context)]
            allowed = self._filter_requested_tables(requested, context)
            cached_model, missing = self._semantic_model_parts_from_workflow_state(allowed, workflow_state)
            fetched_model = {}
            if missing:
                fetched_model = await self._call_provider(self.providers.semantic_model_loader, missing)
            semantic_model = {
                **cached_model,
                **{
                    table: columns
                    for table, columns in fetched_model.items()
                    if table in set(missing)
                },
            }
            semantic_model = {
                table: columns
                for table, columns in semantic_model.items()
                if table in set(allowed)
            }
            source = "workflow_state" if cached_model and not missing else ("mixed" if cached_model else "provider")
            return {
                "tables": allowed,
                "semantic_model": semantic_model,
                **({
                    "source": source,
                    "cache_hit": bool(cached_model) and not missing,
                    "from_workflow_state": [table for table in allowed if table in cached_model],
                    "fetched": list(missing),
                } if cached_model else {}),
            }

        if tool_name == "schema.related_tables":
            requested = [str(item).strip() for item in payload.get("table_names", []) if str(item).strip()]
            if not requested:
                requested = self._workflow_selected_tables(workflow_state)
            if not requested:
                metadata = await self._call_provider(self.providers.table_metadata_loader)
                requested = [row.get("table_name", "") for row in self._filter_table_metadata(metadata, context)]
            allowed = self._filter_requested_tables(requested, context)
            cached = self._relationships_from_workflow_state(allowed, context, workflow_state)
            if cached is not None:
                return cached
            relationships = await self._call_provider(self.providers.table_relationship_loader, allowed)
            filtered = [
                row for row in relationships
                if self._is_table_visible(str(row.get("from_table", "")), context)
                and self._is_table_visible(str(row.get("to_table", "")), context)
            ]
            return {"relationships": filtered}

        if tool_name == "artifact.read":
            return {
                "artifacts": self._read_workflow_artifacts(payload, workflow_state),
            }

        if tool_name == "report.render":
            return self._render_report(payload, workflow_state)

        if tool_name == "sql_draft.submit":
            return self._submit_sql_draft(payload, context)

        if tool_name == "analysis_plan.submit":
            return self._submit_analysis_plan(payload, context)

        raise KeyError(f"Unknown tool: {tool_name}")

    async def _call_provider(self, provider, *args, **kwargs):
        result = provider(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _workflow_query_matches(self, query: str, workflow_state: dict[str, Any]) -> bool:
        query = str(query or "").strip()
        recall_context = workflow_state.get("recall_context")
        query_key = ""
        if isinstance(recall_context, dict):
            query_key = str(recall_context.get("query_key") or "").strip()
        if query_key:
            return query_key == query
        for key in ("enhanced_query", "rewritten_query", "query"):
            state_query = str(workflow_state.get(key) or "").strip()
            if state_query and state_query == query:
                return True
        return False

    def _workflow_state_query(self, workflow_state: dict[str, Any]) -> str:
        recall_context = workflow_state.get("recall_context")
        if isinstance(recall_context, dict):
            query_key = str(recall_context.get("query_key") or "").strip()
            if query_key:
                return query_key
        for key in ("enhanced_query", "rewritten_query", "query"):
            state_query = str(workflow_state.get(key) or "").strip()
            if state_query:
                return state_query
        return ""

    def _workflow_selected_tables(self, workflow_state: dict[str, Any]) -> list[str]:
        selected = workflow_state.get("selected_tables")
        if not isinstance(selected, list):
            return []
        return [
            str(table).strip()
            for table in selected
            if str(table).strip()
        ]

    def _workflow_evidence_can_answer_subquery(
        self,
        query: str,
        workflow_state: dict[str, Any],
    ) -> str:
        query = str(query or "").strip()
        source_query = self._workflow_state_query(workflow_state)
        if not query or not source_query:
            return ""
        if query == source_query:
            return ""
        if query in source_query or source_query in query:
            return source_query
        recall_context = workflow_state.get("recall_context")
        matched_terms = []
        if isinstance(recall_context, dict):
            matched_terms = [
                str(term).strip()
                for term in recall_context.get("matched_terms", []) or []
                if str(term).strip()
            ]
        for term in matched_terms:
            if term in query or query in term:
                return source_query
        return ""

    def _business_knowledge_from_workflow_state(
        self,
        query: str,
        top_k: int,
        workflow_state: dict[str, Any],
    ) -> dict[str, Any] | None:
        evidence = workflow_state.get("evidence")
        if not query or not isinstance(evidence, list) or not evidence:
            return None
        query_reused_from = ""
        if not self._workflow_query_matches(query, workflow_state):
            query_reused_from = self._workflow_evidence_can_answer_subquery(query, workflow_state)
        if not self._workflow_query_matches(query, workflow_state) and not query_reused_from:
            return None
        rows = [str(item).strip() for item in evidence if str(item).strip()]
        if not rows:
            return None
        output = {
            "results": [
                {
                    "content": row,
                    "metadata": {
                        "source": "workflow_state",
                        "score": 1.0,
                        "retriever_source": "workflow_state",
                    },
                }
                for row in rows[:top_k]
            ],
            "source": "workflow_state",
            "cache_hit": True,
        }
        if query_reused_from:
            output["query_reused_from"] = query_reused_from
        return output

    def _table_metadata_from_workflow_state(
        self,
        context: SecurityContext,
        workflow_state: dict[str, Any],
    ) -> dict[str, Any] | None:
        metadata = workflow_state.get("table_metadata")
        if isinstance(metadata, dict) and metadata:
            rows = [
                {
                    "table_name": table,
                    "table_comment": comment,
                }
                for table, comment in metadata.items()
                if str(table).strip()
            ]
        elif isinstance(metadata, list) and metadata:
            rows = [dict(row) for row in metadata if isinstance(row, dict)]
        else:
            return None
        allowed = self._filter_table_metadata(rows, context)
        return {
            "tables": allowed,
            "source": "workflow_state",
            "cache_hit": True,
        }

    def _semantic_model_parts_from_workflow_state(
        self,
        allowed: list[str],
        workflow_state: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        model = workflow_state.get("semantic_model")
        if not isinstance(model, dict):
            return {}, list(allowed)
        cached = {
            table: model.get(table)
            for table in allowed
            if isinstance(model.get(table), (dict, list))
        }
        missing = [table for table in allowed if table not in cached]
        return cached, missing

    def _relationships_from_workflow_state(
        self,
        allowed: list[str],
        context: SecurityContext,
        workflow_state: dict[str, Any],
    ) -> dict[str, Any] | None:
        relationships = workflow_state.get("table_relationships")
        if "table_relationships" not in workflow_state:
            return None
        if not isinstance(relationships, list):
            return None
        allowed_set = set(allowed)
        filtered = [
            dict(row)
            for row in relationships
            if isinstance(row, dict)
            and str(row.get("from_table", "")) in allowed_set
            and str(row.get("to_table", "")) in allowed_set
            and self._is_table_visible(str(row.get("from_table", "")), context)
            and self._is_table_visible(str(row.get("to_table", "")), context)
        ]
        selected_set = set(self._workflow_selected_tables(workflow_state))
        if not filtered and relationships and not (selected_set and allowed_set.issubset(selected_set)):
            return None
        return {
            "relationships": filtered,
            "source": "workflow_state",
            "cache_hit": True,
        }

    def _filter_table_metadata(self, metadata: list[dict], context: SecurityContext) -> list[dict]:
        visible_names = [row.get("table_name", "") for row in metadata if row.get("table_name")]
        auth = authorize_tables(visible_names, context, {row.get("table_name", ""): row.get("table_comment", "") for row in metadata}, stage="schema.list_tables")
        allowed_lookup = set(auth.allowed_tables)
        return [
            row for row in metadata
            if row.get("table_name") in allowed_lookup
        ]

    def _filter_requested_tables(self, requested: list[str], context: SecurityContext) -> list[str]:
        if not requested:
            return []
        auth = authorize_tables(requested, context, stage="schema.visible_tables")
        return auth.allowed_tables

    def _is_table_visible(self, table_name: str, context: SecurityContext) -> bool:
        if not table_name:
            return False
        if table_name in set(context.denied_tables):
            return False
        if context.allowed_tables is None:
            return True
        return table_name in set(context.allowed_tables)

    def _semantic_model_columns(self, table_name: str, semantic_model: dict[str, dict[str, dict]]) -> list[dict]:
        columns = semantic_model.get(table_name, {})
        rows = []
        for column_name in sorted(columns):
            row = dict(columns[column_name])
            row.setdefault("table_name", table_name)
            row.setdefault("column_name", column_name)
            rows.append(row)
        return rows

    def _read_workflow_artifacts(
        self,
        payload: dict[str, Any],
        workflow_state: dict[str, Any],
    ) -> list[dict]:
        artifacts = self._workflow_artifacts(workflow_state)
        requested_ids = [str(item) for item in payload.get("artifact_ids", []) if str(item)]
        requested_types = {str(item) for item in payload.get("types", []) if str(item)}

        if requested_ids:
            by_id = {str(artifact.get("id", "")): artifact for artifact in artifacts}
            return [by_id[artifact_id] for artifact_id in requested_ids if artifact_id in by_id]

        if requested_types:
            return [
                artifact
                for artifact in artifacts
                if str(artifact.get("type", "")) in requested_types
            ]

        return artifacts

    def _workflow_artifacts(self, workflow_state: dict[str, Any]) -> list[dict]:
        artifacts: list[dict] = []
        for index, item in enumerate(workflow_state.get("artifacts", []) or []):
            if isinstance(item, dict):
                artifact = dict(item)
            else:
                artifact = {"content": item}
            artifact.setdefault("id", f"artifact-{index + 1}")
            artifact.setdefault("type", "artifact")
            artifacts.append(artifact)

        for key in ("result", "results", "execution_result", "query_result"):
            if key not in workflow_state:
                continue
            content = workflow_state.get(key)
            if content is None or content == "":
                continue
            artifacts.append(
                {
                    "id": key,
                    "type": "result",
                    "content": content,
                }
            )
        return artifacts

    def _render_report(
        self,
        payload: dict[str, Any],
        workflow_state: dict[str, Any],
    ) -> dict[str, Any]:
        title = str(payload.get("title") or workflow_state.get("report_title") or "分析报告").strip()
        artifacts = self._read_workflow_artifacts(payload, workflow_state)
        sections = self._report_sections(artifacts)
        markdown = "\n\n".join(
            [
                f"# {title}",
                self._markdown_section("结论", sections["conclusions"]),
                self._markdown_section("关键指标", sections["metrics"]),
                self._markdown_section("异常点", sections["anomalies"]),
                self._markdown_section("后续追查建议", sections["next_steps"]),
            ]
        )
        return {
            "markdown": markdown,
            "echarts": (
                self._echarts_configs(sections["metric_values"])
                if bool(payload.get("include_echarts", False))
                else []
            ),
            "source_artifact_ids": [str(artifact.get("id", "")) for artifact in artifacts],
        }

    def _report_sections(self, artifacts: list[dict]) -> dict[str, Any]:
        conclusions: list[str] = []
        metrics: list[str] = []
        metric_values: list[dict[str, Any]] = []
        anomalies: list[str] = []
        next_steps: list[str] = []

        for artifact in artifacts:
            content = artifact.get("content")
            if isinstance(content, dict):
                conclusions.extend(
                    self._strings_from_values(
                        content.get("conclusion"),
                        content.get("summary"),
                        content.get("answer"),
                    )
                )
                extracted_metrics, extracted_values = self._extract_metrics(content)
                metrics.extend(extracted_metrics)
                metric_values.extend(extracted_values)
                anomalies.extend(self._strings_from_values(content.get("anomalies")))
                next_steps.extend(
                    self._strings_from_values(
                        content.get("next_steps"),
                        content.get("recommendations"),
                    )
                )
                rows = content.get("rows")
                if isinstance(rows, list):
                    conclusions.append(f"已读取 {len(rows)} 行已执行结果。")
            elif isinstance(content, list):
                conclusions.append(f"已读取 {len(content)} 条已执行结果。")
            elif content:
                conclusions.append(str(content))

        if not conclusions:
            conclusions.append("当前 workflow_state 未提供可归纳的结论 artifact。")
        if not metrics:
            metrics.append("当前 workflow_state 未提供关键指标 artifact。")
        if not anomalies:
            anomalies.append("当前 workflow_state 未提供异常点 artifact。")
        if not next_steps:
            next_steps.append("补充已执行结果或分析 artifact 后再继续追查。")

        return {
            "conclusions": self._dedupe_strings(conclusions),
            "metrics": self._dedupe_strings(metrics),
            "metric_values": metric_values,
            "anomalies": self._dedupe_strings(anomalies),
            "next_steps": self._dedupe_strings(next_steps),
        }

    def _strings_from_values(self, *values: Any) -> list[str]:
        rows: list[str] = []
        for value in values:
            if value is None or value == "":
                continue
            if isinstance(value, list):
                rows.extend(str(item) for item in value if item is not None and item != "")
            else:
                rows.append(str(value))
        return rows

    def _extract_metrics(self, content: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
        metrics = content.get("metrics")
        if not isinstance(metrics, dict):
            return [], []
        formatted: list[str] = []
        values: list[dict[str, Any]] = []
        for name, value in metrics.items():
            formatted.append(f"{name}: {value}")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append({"name": str(name), "value": value})
        return formatted, values

    def _markdown_section(self, title: str, rows: list[str]) -> str:
        body = "\n".join(f"- {row}" for row in rows)
        return f"## {title}\n{body}"

    def _echarts_configs(self, metric_values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not metric_values:
            return []
        return [
            {
                "type": "echarts_config",
                "option": {
                    "title": {"text": "关键指标"},
                    "tooltip": {},
                    "xAxis": {
                        "type": "category",
                        "data": [item["name"] for item in metric_values],
                    },
                    "yAxis": {"type": "value"},
                    "series": [
                        {
                            "type": "bar",
                            "data": [item["value"] for item in metric_values],
                        }
                    ],
                },
            }
        ]

    def _dedupe_strings(self, rows: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if row in seen:
                continue
            seen.add(row)
            deduped.append(row)
        return deduped

    def _submit_sql_draft(
        self,
        payload: dict[str, Any],
        context: SecurityContext,
    ) -> dict[str, Any]:
        sql = str(payload.get("sql", "")).strip()
        if not sql:
            raise ValueError("sql is required")
        purpose = str(payload.get("purpose", "") or "").strip()
        tables = [str(table).strip() for table in payload.get("tables", []) if str(table).strip()]
        if tables:
            auth = authorize_tables(tables, context, stage="sql_draft.submit")
            if not auth.allowed:
                raise PermissionError(auth.message or "SQL draft references unauthorized tables")
        draft_id = "draft-" + sha256(f"{purpose}\n{sql}".encode("utf-8")).hexdigest()[:12]
        return {
            "draft_id": draft_id,
            "sql": sql,
            "purpose": purpose,
            "tables": tables,
            "execution_mode": "draft_only",
            "requires_harness": True,
            "status": "pending_harness_review",
            "harness_steps": [
                "safety_check",
                "authorize_sql",
                "approve",
                "execute_sql",
            ],
        }

    def _submit_analysis_plan(
        self,
        payload: dict[str, Any],
        context: SecurityContext,
    ) -> dict[str, Any]:
        plan_input = payload.get("plan")
        if plan_input is None:
            plan_input = payload.get("analysis_plan")
        if plan_input is None:
            plan_input = payload.get("plan_text")

        plan = self._normalize_analysis_plan(plan_input, context, purpose=str(payload.get("purpose", "") or "").strip())
        if not isinstance(plan, dict):
            raise ValueError("plan is required")
        if str(plan.get("mode", "")).strip() != "analysis_plan":
            raise ValueError("plan.mode must be analysis_plan")

        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("plan.steps must be a non-empty list")

        referenced_tables: list[str] = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                raise ValueError(f"plan.steps[{index}] must be an object")
            if step.get("step") is None:
                raise ValueError(f"plan.steps[{index}].step is required")
            if not str(step.get("type", "")).strip():
                raise ValueError(f"plan.steps[{index}].type is required")
            if not str(step.get("goal", "")).strip():
                raise ValueError(f"plan.steps[{index}].goal is required")
            for table in step.get("tables", []) or []:
                table_name = str(table).strip()
                if table_name:
                    referenced_tables.append(table_name)

        if referenced_tables:
            auth = authorize_tables(referenced_tables, context, stage="analysis_plan.submit")
            if not auth.allowed:
                raise PermissionError(auth.message or "Analysis plan references unauthorized tables")

        purpose = str(payload.get("purpose", "") or "").strip()
        plan_text = json.dumps(plan, ensure_ascii=False, sort_keys=True, default=str)
        plan_id = "plan-" + sha256(f"{purpose}\n{plan_text}".encode("utf-8")).hexdigest()[:12]
        return {
            "plan_id": plan_id,
            "plan": plan,
            "purpose": purpose,
            "execution_mode": "plan_only",
            "requires_harness": True,
            "status": "pending_harness_review",
            "harness_steps": [
                "validate_analysis_plan",
                "safety_check",
                "authorize_sql",
                "approve",
                "execute_sql",
                "merge_report",
            ],
        }

    def _normalize_analysis_plan(
        self,
        plan_input: Any,
        context: SecurityContext,
        *,
        purpose: str,
    ) -> dict[str, Any] | None:
        if isinstance(plan_input, str):
            plan: dict[str, Any] = {"analysis_plan": plan_input}
            source_text = plan_input
        elif isinstance(plan_input, dict):
            plan = dict(plan_input)
            source_text = json.dumps(plan, ensure_ascii=False, default=str)
        else:
            return None

        visible_tables = self._visible_tables_for_task(context) or []
        candidate_tables = self._extract_candidate_tables(source_text, visible_tables)
        raw_steps = plan.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            if not candidate_tables:
                return None
            return self._synthesize_analysis_plan(
                purpose=purpose,
                source_text=source_text,
                tables=candidate_tables,
            )

        if (
            len(raw_steps) == 1
            and not self._step_has_sql_text(raw_steps[0])
            and not self._is_structured_analysis_step(raw_steps[0])
        ):
            if not candidate_tables:
                candidate_tables = visible_tables
            if not candidate_tables:
                return None
            return self._synthesize_analysis_plan(
                purpose=purpose,
                source_text=source_text,
                tables=candidate_tables,
            )

        normalized_steps: list[dict[str, Any]] = []
        has_sql_step = False
        total_steps = len(raw_steps)
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                return None
            normalized = self._normalize_analysis_step(
                raw_step,
                index,
                total_steps,
                candidate_tables,
                visible_tables,
            )
            if normalized is None:
                return None
            if normalized.get("type") == "sql":
                has_sql_step = True
            normalized_steps.append(normalized)

        if not has_sql_step:
            if not candidate_tables:
                candidate_tables = visible_tables
            if not candidate_tables:
                return None
            return self._synthesize_analysis_plan(
                purpose=purpose,
                source_text=source_text,
                tables=candidate_tables,
            )

        normalized_plan: dict[str, Any] = {
            "mode": "analysis_plan",
            "reason": str(plan.get("reason") or purpose or "").strip(),
            "steps": normalized_steps,
        }
        if isinstance(plan_input, dict) and "requires_user_confirmation" in plan_input:
            normalized_plan["requires_user_confirmation"] = bool(plan_input.get("requires_user_confirmation"))
        return normalized_plan

    def _is_structured_analysis_step(self, step: Any) -> bool:
        if not isinstance(step, dict):
            return False
        try:
            step_no = int(step.get("step"))
        except (TypeError, ValueError):
            return False
        if step_no <= 0:
            return False
        step_type = str(step.get("type", "")).strip()
        if step_type not in {"sql", "python_merge", "report"}:
            return False
        if not str(step.get("goal", "")).strip():
            return False
        if step_type != "sql":
            return True
        return bool(self._normalize_tables(step.get("tables"), [], []))

    def _normalize_analysis_step(
        self,
        raw_step: dict[str, Any],
        index: int,
        total_steps: int,
        candidate_tables: list[str],
        visible_tables: list[str],
    ) -> dict[str, Any] | None:
        step_no = self._coerce_step_number(raw_step.get("step"), raw_step.get("step_number"), index)
        goal = str(
            raw_step.get("goal")
            or raw_step.get("description")
            or raw_step.get("name")
            or f"步骤 {step_no}"
        ).strip()
        if not goal:
            return None

        sql_text = str(raw_step.get("sql") or raw_step.get("sql_draft") or "").strip()
        step_type = self._normalize_step_type(
            raw_step.get("type"),
            sql_text=sql_text,
            index=index,
            total_steps=total_steps,
        )
        if not step_type:
            return None

        sql_tables = self._extract_candidate_tables(sql_text, [])
        text_tables = self._extract_candidate_tables(
            "\n".join(
                part for part in [
                    str(raw_step.get("description") or ""),
                    str(raw_step.get("name") or ""),
                ]
                if part
            ),
            candidate_tables or visible_tables,
        )
        extracted_tables = self._dedupe_strings([*sql_tables, *text_tables])
        tables = self._normalize_tables(raw_step.get("tables"), extracted_tables, [])
        if step_type == "sql" and not tables:
            tables = list(candidate_tables or visible_tables)
        if step_type == "sql" and not tables:
            return None

        depends_on = self._normalize_depends_on(raw_step.get("depends_on"), step_no, index, total_steps, step_type)
        merge_keys = self._normalize_merge_keys(raw_step.get("merge_keys"), step_type)

        normalized: dict[str, Any] = {
            "step": step_no,
            "type": step_type,
            "goal": goal,
            "tables": tables,
            "depends_on": depends_on,
            "merge_keys": merge_keys,
        }
        if sql_text:
            normalized["sql"] = self._normalize_sql_text(sql_text)
        return normalized

    def _normalize_sql_text(self, sql_text: str) -> str:
        text = "\n".join(line.rstrip() for line in str(sql_text).strip().splitlines())
        if text.endswith(";"):
            text = text[:-1].strip()
        return text

    def _synthesize_analysis_plan(
        self,
        *,
        purpose: str,
        source_text: str,
        tables: list[str],
    ) -> dict[str, Any]:
        title = purpose or "AgentScope 数据分析计划"
        return {
            "mode": "analysis_plan",
            "reason": (
                "AgentScope 提交的是半结构化分析草稿，工具已按可见表和草稿内容规范化为可执行计划。"
            ),
            "steps": [
                {
                    "step": 1,
                    "type": "sql",
                    "goal": title,
                    "tables": tables,
                    "depends_on": [],
                    "merge_keys": [],
                },
                {
                    "step": 2,
                    "type": "python_merge",
                    "goal": "按公共维度合并已执行结果，形成可比较的实际、预算、回款和费用视图。",
                    "tables": [],
                    "depends_on": [1],
                    "merge_keys": ["period"],
                },
                {
                    "step": 3,
                    "type": "report",
                    "goal": "基于 SQL Harness 执行结果生成用户可读的关系分析结论。",
                    "tables": [],
                    "depends_on": [2],
                    "merge_keys": [],
                },
            ],
            "requires_user_confirmation": True,
            "source_excerpt": source_text[:1200],
        }

    def _step_has_sql_text(self, step: dict[str, Any]) -> bool:
        return bool(str(step.get("sql") or step.get("sql_draft") or "").strip())

    def _normalize_step_type(
        self,
        step_type: Any,
        *,
        sql_text: str,
        index: int,
        total_steps: int,
    ) -> str | None:
        normalized = str(step_type or "").strip()
        if normalized in {"sql", "python_merge", "report"}:
            return normalized
        if sql_text:
            return "sql"
        if total_steps == 1:
            return "sql"
        if index == total_steps:
            return "report"
        if index > 1:
            return "python_merge"
        return "sql"

    def _normalize_tables(
        self,
        tables: Any,
        extracted_tables: list[str],
        fallback_tables: list[str],
    ) -> list[str]:
        normalized: list[str] = []
        explicit_tables = tables if isinstance(tables, (list, tuple, set)) else []
        for source in (explicit_tables, extracted_tables, fallback_tables):
            for table in source or []:
                table_name = str(table).strip()
                if not table_name or table_name in normalized:
                    continue
                normalized.append(table_name)
        return normalized

    def _normalize_depends_on(
        self,
        depends_on: Any,
        step_no: int,
        index: int,
        total_steps: int,
        step_type: str,
    ) -> list[int]:
        normalized: list[int] = []
        if isinstance(depends_on, list):
            for dep in depends_on:
                try:
                    dep_no = int(dep)
                except (TypeError, ValueError):
                    continue
                if dep_no not in normalized:
                    normalized.append(dep_no)
        if normalized:
            return normalized
        if step_type == "python_merge":
            return list(range(1, step_no))
        if step_type == "report":
            return list(range(1, step_no))
        if total_steps == 1:
            return []
        return []

    def _normalize_merge_keys(self, merge_keys: Any, step_type: str) -> list[str]:
        normalized: list[str] = []
        if isinstance(merge_keys, list):
            for key in merge_keys:
                key_name = str(key).strip()
                if key_name and key_name not in normalized:
                    normalized.append(key_name)
        if normalized:
            return normalized
        if step_type == "python_merge":
            return ["period"]
        return []

    def _coerce_step_number(self, *values: Any) -> int:
        for value in values:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                return number
        return 1

    def _extract_candidate_tables(self, text: str, candidates: list[str]) -> list[str]:
        if not text:
            return []
        selected: list[str] = []
        if not candidates:
            sources = re.findall(r"`([^`]+)`", text)
            sources.extend(re.findall(r"(?i)\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", text))
            sources.extend(re.findall(r"\bt_[A-Za-z0-9_]+\b", text))
            for table in sources:
                table_name = str(table).strip()
                if table_name and table_name not in selected:
                    selected.append(table_name)
            return selected
        for table in candidates:
            escaped = re.escape(str(table))
            if re.search(rf"(?<![0-9A-Za-z_]){escaped}(?![0-9A-Za-z_])", text):
                if table not in selected:
                    selected.append(table)
        return selected

    def _build_trace(
        self,
        tool_name: str,
        task_type: str,
        context: SecurityContext,
        *,
        session_id: str,
        thread_id: str,
        started_at: datetime,
        ended_at: datetime,
        status: str,
        input_payload: dict[str, Any],
        output: Any,
        error: str,
    ) -> ToolTrace:
        elapsed_ms = max(0, int((ended_at - started_at).total_seconds() * 1000))
        return ToolTrace(
            tool_name=tool_name,
            task_type=task_type,
            session_id=session_id,
            thread_id=thread_id,
            user_id=context.user_id,
            status=status,
            elapsed_ms=elapsed_ms,
            started_at=started_at,
            ended_at=ended_at,
            input=input_payload,
            output=output,
            error=error,
        )
