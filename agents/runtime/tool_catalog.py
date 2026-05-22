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
    recall_agent_knowledge,
    recall_business_knowledge,
)
from agents.flow.complex_query import assess_query_feasibility
from agents.model.format_tool import normalize_sql_answer
from agents.rag.query_rewrite import rewrite_query
from agents.runtime.tool_contracts import (
    RuntimeTool,
    ToolCallResult,
    ToolContract,
    ToolTrace,
)
from agents.tool.security.policies import SecurityContext, authorize_tables
from agents.tool.sql_tools.safety import SQLSafetyChecker

logger = logging.getLogger(__name__)

_SQL_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+`?([a-zA-Z_][\w]*)`?", re.IGNORECASE)

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
        "query.context_rewrite",
        "business_knowledge.search",
        "sql_examples.search",
        "query.enhance",
        "schema.list_tables",
        "schema.describe_table",
        "schema.select_candidates",
        "semantic_model.search",
        "schema.related_tables",
        "plan.assess_feasibility",
        "sql.normalize",
        "sql.safety_check",
        "sql.authorize_draft",
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

_DATA_ANALYSIS_TOOL_ORDER: tuple[str, ...] = (
    "query.context_rewrite",
    "business_knowledge.search",
    "sql_examples.search",
    "query.enhance",
    "schema.list_tables",
    "schema.describe_table",
    "schema.select_candidates",
    "semantic_model.search",
    "schema.related_tables",
    "plan.assess_feasibility",
    "sql.normalize",
    "sql.safety_check",
    "sql.authorize_draft",
    "current_time.now",
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


def _default_agent_knowledge_search(query: str, top_k: int, callbacks=None) -> list[Document]:
    return recall_agent_knowledge(query, top_k=top_k, callbacks=callbacks)


async def _default_query_rewriter(summary: str, history: Any, query: str, config=None) -> str:
    return await rewrite_query(summary=summary, history=history, query=query, config=config)


def _default_time_provider() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ToolProviders:
    """Injectable data providers for ToolCatalog."""

    business_knowledge_search: Any = _default_business_knowledge_search
    agent_knowledge_search: Any = _default_agent_knowledge_search
    query_rewriter: Any = _default_query_rewriter
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
        call_when: str | None = None,
    ) -> str:
        call_when_text = call_when or (
            "you need this capability for the current step. Do not merely describe using this tool; "
            "call it when its output is needed to build or validate the plan."
        )
        return (
            f"Purpose: {purpose}\n"
            f"Call it when: {call_when_text}\n"
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
            "query.context_rewrite": ToolContract(
                name="query.context_rewrite",
                description=self._tool_description(
                    purpose=(
                        "Rewrite an ambiguous follow-up or omitted-subject user question into a standalone "
                        "data-analysis query using conversation summary and recent chat history."
                    ),
                    boundary=(
                        "Planning-only text transformation. It does not retrieve schema, recall knowledge, "
                        "draft SQL, or execute SQL."
                    ),
                    required_input=(
                        "query is required. summary and history are optional; when omitted the tool reads "
                        "workflow_state conversation context."
                    ),
                    output=(
                        "An object with original_query, rewritten_query, summary_used, and history_count."
                    ),
                    negative=(
                        "the query is already self-contained and there is no relevant conversation context, "
                        "or you need business definitions rather than context resolution."
                    ),
                    call_when=(
                        "the user says something like '去年亏损', '这个呢', or uses pronouns/omitted subjects. "
                        "Call this tool instead of merely saying the query should be rewritten."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Latest user data-analysis question to rewrite.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Optional conversation or domain summary to use for context.",
                        },
                        "history": {
                            "description": "Optional recent chat history as text or message objects.",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "original_query": {
                            "type": "string",
                            "description": "Input query before context rewriting.",
                        },
                        "rewritten_query": {
                            "type": "string",
                            "description": "Standalone query suitable for recall, schema search, and planning.",
                        },
                        "summary_used": {
                            "type": "boolean",
                            "description": "Whether a non-empty summary was supplied to the rewrite provider.",
                        },
                        "history_count": {
                            "type": "integer",
                            "description": "Number of history messages used when history was a list.",
                        },
                    },
                },
            ),
            "sql_examples.search": ToolContract(
                name="sql_examples.search",
                description=self._tool_description(
                    purpose=(
                        "Recall prior agent SQL examples, few-shot patterns, and successful query templates "
                        "that can guide analysis-plan design."
                    ),
                    boundary=(
                        "Read-only example recall. It returns examples and related metadata but does not draft, "
                        "approve, or execute SQL."
                    ),
                    required_input=(
                        "query is required and should be the rewritten or enhanced analytical question; top_k "
                        "optionally limits returned examples."
                    ),
                    output=(
                        "An object with results and few_shot_examples. Each result keeps content and metadata "
                        "for traceability."
                    ),
                    negative=(
                        "you need business metric definitions, actual rows, or already have enough examples in "
                        "workflow_state."
                    ),
                    call_when=(
                        "you need examples of similar SQL or decomposition patterns before submitting an "
                        "analysis_plan. Call it; do not just mention example recall."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Current standalone analysis question.",
                        },
                        "top_k": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum number of example snippets to return.",
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
                            "description": "Matched SQL/example snippets with content and metadata.",
                        },
                        "few_shot_examples": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Example texts extracted from results for prompt construction.",
                        },
                        **self._readthrough_output_properties(),
                    },
                },
            ),
            "query.enhance": ToolContract(
                name="query.enhance",
                description=self._tool_description(
                    purpose=(
                        "Enhance the standalone user query with recalled business evidence such as metric "
                        "definitions, formulas, synonyms, and related table hints."
                    ),
                    boundary=(
                        "Planning-only text enrichment. It uses supplied evidence or workflow_state evidence "
                        "and never queries database rows or executes SQL."
                    ),
                    required_input=(
                        "query is required. evidence is optional but should include business_knowledge.search "
                        "results or evidence strings when available."
                    ),
                    output=(
                        "An object with enhanced_query and evidence_used."
                    ),
                    negative=(
                        "there is no business evidence to apply, or enhancement would add filters the user did "
                        "not ask for."
                    ),
                    call_when=(
                        "business_knowledge.search has returned definitions or formulas that should be reflected "
                        "in downstream schema selection and planning."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Standalone analysis question to enrich.",
                        },
                        "evidence": {
                            "type": "array",
                            "items": {},
                            "description": "Business evidence strings or result objects to apply.",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "enhanced_query": {
                            "type": "string",
                            "description": "Query after deterministic evidence-driven enrichment.",
                        },
                        "evidence_used": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Evidence strings considered by the enhancement logic.",
                        },
                    },
                },
            ),
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
            "schema.select_candidates": ToolContract(
                name="schema.select_candidates",
                description=self._tool_description(
                    purpose=(
                        "Select likely physical tables for the analysis by scoring visible table metadata, "
                        "semantic columns, and recalled evidence."
                    ),
                    boundary=(
                        "Read-only deterministic planning helper. It filters by security context and returns "
                        "candidate metadata; it does not call an LLM or execute SQL."
                    ),
                    required_input=(
                        "query is required. candidate_tables, top_k, evidence, and few_shot_examples are optional "
                        "signals for narrowing."
                    ),
                    output=(
                        "An object with selected_tables, table_metadata, semantic_model, candidate_scores, and "
                        "recall_context."
                    ),
                    negative=(
                        "the final table set is already known with sufficient semantic_model, or you need rows "
                        "rather than schema candidates."
                    ),
                    call_when=(
                        "you have a rewritten/enhanced query and need a scoped table set before relationship "
                        "lookup, feasibility assessment, or analysis_plan.submit."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Standalone or enhanced analysis question.",
                        },
                        "candidate_tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional authorized table names to score instead of all visible tables.",
                        },
                        "top_k": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum number of selected tables to return.",
                        },
                        "evidence": {
                            "type": "array",
                            "items": {},
                            "description": "Business evidence strings or objects with related table metadata.",
                        },
                        "few_shot_examples": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional few-shot examples whose SQL can hint related tables.",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "selected_tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Ranked authorized candidate tables selected for planning.",
                        },
                        "table_metadata": {
                            "type": "object",
                            "description": "Table comments keyed by selected table name.",
                        },
                        "semantic_model": {
                            "type": "object",
                            "description": "Semantic metadata keyed by selected table and then column.",
                        },
                        "candidate_scores": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Scoring diagnostics for visible candidates.",
                        },
                        "recall_context": {
                            "type": "object",
                            "description": "Evidence-derived related tables and matched terms used for ranking.",
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
            "plan.assess_feasibility": ToolContract(
                name="plan.assess_feasibility",
                description=self._tool_description(
                    purpose=(
                        "Assess whether selected tables are suitable for single SQL, strict single SQL, "
                        "multi-step analysis_plan, or clarification."
                    ),
                    boundary=(
                        "Planning-only structural check over selected tables and relationships. It does not "
                        "draft SQL, approve SQL, execute SQL, or create user-facing data facts."
                    ),
                    required_input=(
                        "query and selected_tables are required. relationships and task_type are optional; "
                        "missing relationships are loaded from read-only metadata."
                    ),
                    output=(
                        "An object with feasibility_decision, relationships, selected_tables, and route_mode."
                    ),
                    negative=(
                        "no candidate tables have been selected, or the plan has already been submitted and is "
                        "waiting for SQL Harness."
                    ),
                    call_when=(
                        "you need to decide whether to submit a single-step or multi-step analysis_plan after "
                        "candidate table selection."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Standalone or enhanced analysis question.",
                        },
                        "selected_tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Authorized candidate tables being considered.",
                        },
                        "relationships": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Optional relationship edges among selected tables.",
                        },
                        "task_type": {
                            "type": "string",
                            "description": "Optional semantic task type such as analysis, report, comparison, detail, export, sensitive, or ambiguous.",
                        },
                    },
                    "required": ["query", "selected_tables"],
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "feasibility_decision": {
                            "type": "object",
                            "description": "Execution-mode decision and structural risk fields.",
                        },
                        "relationships": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Relationship edges used for the decision.",
                        },
                        "selected_tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Authorized tables considered by the decision.",
                        },
                        "route_mode": {
                            "type": "string",
                            "description": "Convenience copy of feasibility_decision.execution_mode.",
                        },
                    },
                },
            ),
            "sql.normalize": ToolContract(
                name="sql.normalize",
                description=self._tool_description(
                    purpose=(
                        "Normalize and validate a draft SQL string with the same local format rules used by "
                        "SQLReact before any Harness handoff."
                    ),
                    boundary=(
                        "Local syntax/format validation only. It does not authorize, approve, execute, explain, "
                        "or repair SQL."
                    ),
                    required_input=(
                        "answer or sql is required and should contain a proposed SELECT/WITH SQL draft."
                    ),
                    output=(
                        "An object with sql, is_valid, and error."
                    ),
                    negative=(
                        "you need table authorization, safety risk analysis, execution, or final result rows."
                    ),
                    call_when=(
                        "you have drafted SQL inside an analysis step and need to check whether it is formatted "
                        "as a valid SELECT/WITH statement before submit."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "answer": {
                            "type": "string",
                            "description": "Raw model answer that may contain fenced SQL or prose.",
                        },
                        "sql": {
                            "type": "string",
                            "description": "Raw SQL draft to normalize when answer is not provided.",
                        },
                    },
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "Normalized SQL candidate, possibly empty or invalid.",
                        },
                        "is_valid": {
                            "type": "boolean",
                            "description": "Whether the SQL passed local format validation.",
                        },
                        "error": {
                            "type": "string",
                            "description": "Validation error message, or empty string when valid.",
                        },
                    },
                },
            ),
            "sql.safety_check": ToolContract(
                name="sql.safety_check",
                description=self._tool_description(
                    purpose=(
                        "Run local static SQL safety analysis for destructive or risky patterns before Harness "
                        "handoff."
                    ),
                    boundary=(
                        "Local static analysis only. It does not authorize tables, approve SQL, or execute SQL."
                    ),
                    required_input=(
                        "sql is required and should be the normalized draft SQL."
                    ),
                    output=(
                        "An object with is_safe, risks, estimated_rows, and required_permissions."
                    ),
                    negative=(
                        "the SQL draft is missing, you need permission checks, or you expect query results."
                    ),
                    call_when=(
                        "you have a SQL draft in an analysis_plan step and want to flag obvious risk before "
                        "analysis_plan.submit."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "Draft SQL to inspect with static safety rules.",
                        },
                    },
                    "required": ["sql"],
                    "additionalProperties": False,
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "is_safe": {
                            "type": "boolean",
                            "description": "Whether the draft passed local static safety checks.",
                        },
                        "risks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Detected safety risk descriptions.",
                        },
                        "estimated_rows": {
                            "description": "Estimated row count when a simple LIMIT is detected, otherwise null.",
                        },
                        "required_permissions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Permission hints implied by detected SQL patterns.",
                        },
                    },
                },
            ),
            "sql.authorize_draft": ToolContract(
                name="sql.authorize_draft",
                description=self._tool_description(
                    purpose=(
                        "Check whether tables referenced by a draft SQL are visible under the current security "
                        "context before SQL Harness approval."
                    ),
                    boundary=(
                        "Authorization validation only. It never grants permissions, approves, executes, or "
                        "returns database rows."
                    ),
                    required_input=(
                        "sql is required unless tables is provided. tables may explicitly list referenced physical "
                        "tables when SQL parsing is not enough."
                    ),
                    output=(
                        "An object with tables, authorization_report, execution_mode=validation_only, and "
                        "requires_harness=true."
                    ),
                    negative=(
                        "you need SQL safety checks, approval, execution, or row-level/column masking results."
                    ),
                    call_when=(
                        "you have a draft SQL or step table list and need to verify table permissions before "
                        "analysis_plan.submit."
                    ),
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "Draft SQL whose referenced tables should be checked.",
                        },
                        "tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional explicit referenced table names.",
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
                            "description": "Referenced tables checked under current security context.",
                        },
                        "authorization_report": {
                            "type": "object",
                            "description": "Permission decision from the platform authorization policy.",
                        },
                        "execution_mode": {
                            "type": "string",
                            "description": "Always validation_only because this tool does not execute SQL.",
                        },
                        "requires_harness": {
                            "type": "boolean",
                            "description": "Always true; SQL still requires SQL Harness approval/execution.",
                        },
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
        tool_order = _DATA_ANALYSIS_TOOL_ORDER if task_type == "data_analysis" else _TOOL_ORDER
        for tool_name in tool_order:
            if tool_name not in allowed_names:
                continue
            if tool_name in {"semantic_model.search", "schema.describe_table", "schema.select_candidates", "schema.related_tables"}:
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
        if tool_name == "query.context_rewrite":
            return await self._rewrite_query_with_context(payload, workflow_state)

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

        if tool_name == "sql_examples.search":
            query = str(payload.get("query", "")).strip()
            if not query:
                raise ValueError("query is required")
            top_k = int(payload.get("top_k", 5) or 5)
            cached = self._agent_knowledge_from_workflow_state(query, top_k, workflow_state)
            if cached is not None:
                return cached
            docs = await self._call_provider(
                self.providers.agent_knowledge_search,
                query=query,
                top_k=top_k,
                callbacks=None,
            )
            results = [
                {
                    "content": doc.page_content,
                    "metadata": dict(doc.metadata or {}),
                }
                for doc in docs
            ]
            return {
                "results": results,
                "few_shot_examples": [str(item["content"]) for item in results if str(item.get("content") or "").strip()],
            }

        if tool_name == "query.enhance":
            return self._enhance_query_with_evidence(payload, workflow_state)

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

        if tool_name == "schema.select_candidates":
            return await self._select_candidate_tables(payload, context, workflow_state)

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

        if tool_name == "plan.assess_feasibility":
            return await self._assess_plan_feasibility(payload, context)

        if tool_name == "sql.normalize":
            raw_sql = str(payload.get("answer") or payload.get("sql") or "").strip()
            sql, ok, error = normalize_sql_answer(raw_sql)
            return {
                "sql": sql,
                "is_valid": ok,
                "error": error or "",
            }

        if tool_name == "sql.safety_check":
            sql = str(payload.get("sql") or "").strip()
            if not sql:
                raise ValueError("sql is required")
            report = SQLSafetyChecker().check(sql)
            return {
                "is_safe": report.is_safe,
                "risks": list(report.risks),
                "estimated_rows": report.estimated_rows,
                "required_permissions": list(report.required_permissions),
            }

        if tool_name == "sql.authorize_draft":
            return self._authorize_sql_draft(payload, context, workflow_state)

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
        try:
            result = provider(*args, **kwargs)
        except TypeError:
            filtered_kwargs = self._filter_provider_kwargs(provider, kwargs)
            if filtered_kwargs == kwargs:
                raise
            result = provider(*args, **filtered_kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _filter_provider_kwargs(self, provider, kwargs: dict[str, Any]) -> dict[str, Any]:
        try:
            signature = inspect.signature(provider)
        except (TypeError, ValueError):
            return kwargs
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
            return kwargs
        allowed = set(signature.parameters)
        return {key: value for key, value in kwargs.items() if key in allowed}

    async def _rewrite_query_with_context(
        self,
        payload: dict[str, Any],
        workflow_state: dict[str, Any],
    ) -> dict[str, Any]:
        query = str(payload.get("query") or self._workflow_state_query(workflow_state)).strip()
        if not query:
            raise ValueError("query is required")
        summary = str(
            payload.get("summary")
            or workflow_state.get("conversation_summary")
            or workflow_state.get("summary")
            or workflow_state.get("domain_summary")
            or ""
        )
        history = payload.get("history")
        if history is None:
            history = workflow_state.get("chat_history", [])
        rewritten = await self._call_provider(
            self.providers.query_rewriter,
            summary=summary,
            history=history,
            query=query,
            config=None,
        )
        rewritten_query = str(rewritten or "").strip() or query
        return {
            "original_query": query,
            "rewritten_query": rewritten_query,
            "summary_used": bool(summary.strip()),
            "history_count": len(history) if isinstance(history, list) else (1 if str(history or "").strip() else 0),
        }

    def _enhance_query_with_evidence(
        self,
        payload: dict[str, Any],
        workflow_state: dict[str, Any],
    ) -> dict[str, Any]:
        query = str(payload.get("query") or self._workflow_state_query(workflow_state)).strip()
        if not query:
            raise ValueError("query is required")
        evidence = self._evidence_strings(payload.get("evidence"))
        if not evidence:
            evidence = self._evidence_strings(workflow_state.get("evidence"))
        enhanced = query
        additions: list[str] = []
        for entry in self._parse_business_evidence(evidence):
            term = str(entry.get("term") or "")
            formula = str(entry.get("formula") or "")
            aliases = [term, *[str(item) for item in entry.get("synonyms", [])]]
            if not term:
                continue
            if any(alias and alias in query for alias in aliases):
                addition = f"{term}: {formula}" if formula else term
                if addition not in enhanced and (not formula or formula not in enhanced):
                    additions.append(addition)
        if additions:
            enhanced = f"{enhanced}（业务口径: {'; '.join(additions)}）"
        return {
            "enhanced_query": enhanced,
            "evidence_used": evidence,
        }

    async def _select_candidate_tables(
        self,
        payload: dict[str, Any],
        context: SecurityContext,
        workflow_state: dict[str, Any],
    ) -> dict[str, Any]:
        query = str(payload.get("query") or self._workflow_state_query(workflow_state)).strip()
        if not query:
            raise ValueError("query is required")

        metadata = await self._call_provider(self.providers.table_metadata_loader)
        visible_rows = self._filter_table_metadata(metadata, context)
        visible_names = [str(row.get("table_name") or "").strip() for row in visible_rows if str(row.get("table_name") or "").strip()]
        requested = [str(item).strip() for item in payload.get("candidate_tables", []) if str(item).strip()]
        if requested:
            candidate_names = [table for table in self._filter_requested_tables(requested, context) if table in set(visible_names)]
        else:
            candidate_names = visible_names
        if not candidate_names:
            return {
                "selected_tables": [],
                "table_metadata": {},
                "semantic_model": {},
                "candidate_scores": [],
                "recall_context": {},
            }

        table_metadata = {
            str(row.get("table_name")): str(row.get("table_comment") or "")
            for row in visible_rows
            if str(row.get("table_name") or "") in set(candidate_names)
        }
        semantic_model = await self._call_provider(self.providers.semantic_model_loader, candidate_names)
        evidence = self._evidence_strings(payload.get("evidence")) or self._evidence_strings(workflow_state.get("evidence"))
        few_shot_examples = self._string_list(payload.get("few_shot_examples")) or self._string_list(workflow_state.get("few_shot_examples"))
        recall_context = self._build_recall_context(query, evidence, few_shot_examples)
        evidence_tables = [
            table
            for table in [
                *recall_context.get("business_related_tables", []),
                *recall_context.get("few_shot_related_tables", []),
            ]
            if table in set(candidate_names)
        ]

        scored = []
        for index, table in enumerate(candidate_names):
            score = self._table_semantic_score(table, query, table_metadata, semantic_model)
            if table in evidence_tables:
                score += 30.0
            scored.append({"table": table, "score": score, "rank_input_order": index})
        scored.sort(key=lambda item: (float(item["score"]), -int(item["rank_input_order"])), reverse=True)
        top_k = int(payload.get("top_k", min(12, len(scored))) or min(12, len(scored)))
        selected = [row["table"] for row in scored if float(row["score"]) > 0]
        if not selected:
            selected = [row["table"] for row in scored]
        selected = self._unique_strings([*evidence_tables, *selected])[:top_k]
        selected_model = {
            table: semantic_model.get(table, {})
            for table in selected
            if isinstance(semantic_model, dict) and table in semantic_model
        }
        return {
            "selected_tables": selected,
            "table_metadata": {table: table_metadata.get(table, "") for table in selected},
            "semantic_model": selected_model,
            "candidate_scores": scored,
            "recall_context": recall_context,
        }

    async def _assess_plan_feasibility(
        self,
        payload: dict[str, Any],
        context: SecurityContext,
    ) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
        selected_tables = self._filter_requested_tables(
            [str(item).strip() for item in payload.get("selected_tables", []) if str(item).strip()],
            context,
        )
        if not query:
            raise ValueError("query is required")
        if not selected_tables:
            raise ValueError("selected_tables is required")
        relationships = payload.get("relationships")
        if not isinstance(relationships, list):
            relationships = await self._call_provider(self.providers.table_relationship_loader, selected_tables)
        relationships = [
            dict(row)
            for row in relationships
            if isinstance(row, dict)
            and self._is_table_visible(str(row.get("from_table", "")), context)
            and self._is_table_visible(str(row.get("to_table", "")), context)
        ]
        decision = assess_query_feasibility(
            query=query,
            selected_tables=selected_tables,
            relationships=relationships,
            task_type=str(payload.get("task_type") or "") or None,
            decision_source="agentscope_tool",
        )
        feasibility_decision = {
            "execution_mode": decision.execution_mode,
            "task_type": decision.task_type,
            "can_single_sql": decision.can_single_sql,
            "can_decompose": decision.can_decompose,
            "needs_clarification": decision.needs_clarification,
            "join_risk": decision.join_risk,
            "decision_source": decision.decision_source,
            "reason": decision.reason,
            "selected_tables_count": decision.selected_tables_count,
            "relationship_count": decision.relationship_count,
            "estimated_join_count": decision.estimated_join_count,
        }
        return {
            "feasibility_decision": feasibility_decision,
            "relationships": relationships,
            "selected_tables": selected_tables,
            "route_mode": decision.execution_mode,
        }

    def _authorize_sql_draft(
        self,
        payload: dict[str, Any],
        context: SecurityContext,
        workflow_state: dict[str, Any],
    ) -> dict[str, Any]:
        tables = [str(table).strip() for table in payload.get("tables", []) if str(table).strip()]
        sql = str(payload.get("sql") or "").strip()
        if not tables and sql:
            tables = self._tables_from_sql(sql)
        if not tables:
            tables = self._workflow_selected_tables(workflow_state)
        if not tables:
            raise ValueError("sql or tables is required")
        table_metadata = workflow_state.get("table_metadata")
        auth_metadata = table_metadata if isinstance(table_metadata, dict) else None
        report = authorize_tables(tables, context, table_metadata=auth_metadata, stage="sql.authorize_draft")
        if not report.allowed:
            raise PermissionError(report.message or "SQL draft references unauthorized tables")
        return {
            "tables": report.allowed_tables,
            "authorization_report": report.to_dict(),
            "execution_mode": "validation_only",
            "requires_harness": True,
        }

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

    def _agent_knowledge_from_workflow_state(
        self,
        query: str,
        top_k: int,
        workflow_state: dict[str, Any],
    ) -> dict[str, Any] | None:
        examples = workflow_state.get("few_shot_examples")
        if not query or not isinstance(examples, list) or not examples:
            return None
        if not self._workflow_query_matches(query, workflow_state):
            reused_from = self._workflow_evidence_can_answer_subquery(query, workflow_state)
            if not reused_from:
                return None
        rows = [str(item).strip() for item in examples if str(item).strip()]
        if not rows:
            return None
        return {
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
            "few_shot_examples": rows[:top_k],
            "source": "workflow_state",
            "cache_hit": True,
        }

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

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _evidence_strings(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        rows: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(item.get("content") or item.get("text") or "").strip()
            else:
                text = str(item).strip()
            if text:
                rows.append(text)
        return rows

    def _label_value(self, line: str, labels: tuple[str, ...]) -> str | None:
        for label in labels:
            for sep in (":", "："):
                prefix = f"{label}{sep}"
                if line.startswith(prefix):
                    return line[len(prefix):].strip()
        return None

    def _split_terms(self, value: str) -> list[str]:
        normalized = value.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
        return [item.strip() for item in normalized.split(",") if item.strip()]

    def _parse_business_evidence(self, evidence: list[str]) -> list[dict[str, Any]]:
        entries = []
        for item in evidence:
            entry: dict[str, Any] = {
                "term": "",
                "formula": "",
                "synonyms": [],
                "related_tables": [],
            }
            for line in item.splitlines():
                line = line.strip()
                term = self._label_value(line, ("术语",))
                formula = self._label_value(line, ("公式", "定义"))
                synonyms = self._label_value(line, ("同义词",))
                related_tables = self._label_value(line, ("关联表",))
                if term is not None:
                    entry["term"] = term
                elif formula is not None:
                    entry["formula"] = formula
                elif synonyms is not None:
                    entry["synonyms"] = self._split_terms(synonyms)
                elif related_tables is not None:
                    entry["related_tables"] = self._split_terms(related_tables)
            if entry.get("term"):
                entries.append(entry)
        return entries

    def _build_recall_context(
        self,
        query: str,
        evidence: list[str],
        few_shot_examples: list[str],
    ) -> dict[str, Any]:
        matched_terms: list[str] = []
        business_related_tables: list[str] = []
        evidence_related_tables: list[str] = []
        for item in evidence:
            evidence_related_tables.extend(self._extract_candidate_tables(item, []))
        for entry in self._parse_business_evidence(evidence):
            if not self._business_entry_matches_query(entry, query):
                continue
            term = str(entry.get("term") or "")
            if term:
                matched_terms.append(term)
            business_related_tables.extend(str(table) for table in entry.get("related_tables", []) if str(table))
        return {
            "query_key": query,
            "business_evidence": evidence,
            "few_shot_examples": few_shot_examples,
            "business_related_tables": self._unique_strings([*business_related_tables, *evidence_related_tables]),
            "few_shot_related_tables": self._tables_from_few_shot_examples(few_shot_examples),
            "matched_terms": self._unique_strings(matched_terms),
        }

    def _business_entry_matches_query(self, entry: dict[str, Any], query: str) -> bool:
        aliases = [str(entry.get("term") or ""), *[str(item) for item in entry.get("synonyms", [])]]
        if any(alias and alias in query for alias in aliases):
            return True
        profile = "\n".join(
            [
                str(entry.get("term") or ""),
                str(entry.get("formula") or ""),
                ",".join(str(item) for item in entry.get("synonyms", [])),
            ]
        )
        query_terms = {term for term in self._ranking_terms(query) if len(term) >= 2}
        profile_terms = {term for term in self._ranking_terms(profile) if len(term) >= 2}
        return bool(query_terms & profile_terms)

    def _tables_from_few_shot_examples(self, few_shot_examples: list[str]) -> list[str]:
        tables: list[str] = []
        for item in few_shot_examples:
            tables.extend(self._tables_from_sql(item))
        return self._unique_strings(tables)

    def _tables_from_sql(self, sql: str) -> list[str]:
        return self._unique_strings([table for table in _SQL_TABLE_RE.findall(sql or "") if table])

    def _ranking_terms(self, text: str) -> set[str]:
        normalized = str(text or "").lower()
        terms = set(re.findall(r"[a-z0-9_]+", normalized))
        terms.update(ch for ch in normalized if "\u4e00" <= ch <= "\u9fff")
        for size in (2, 3, 4):
            for index in range(0, max(0, len(normalized) - size + 1)):
                chunk = normalized[index : index + size]
                if any("\u4e00" <= ch <= "\u9fff" for ch in chunk):
                    terms.add(chunk)
        return terms

    def _table_semantic_text(
        self,
        table: str,
        table_metadata: dict[str, str],
        semantic_model: dict[str, Any],
    ) -> str:
        parts = [table, table_metadata.get(table, "")]
        columns = semantic_model.get(table) if isinstance(semantic_model, dict) else {}
        if isinstance(columns, dict):
            for column_name, meta in columns.items():
                parts.append(str(column_name))
                if isinstance(meta, dict):
                    parts.extend(
                        [
                            str(meta.get("column_comment") or ""),
                            str(meta.get("business_name") or ""),
                            str(meta.get("synonyms") or ""),
                            str(meta.get("business_description") or ""),
                        ]
                    )
        return "\n".join(part for part in parts if part)

    def _table_semantic_score(
        self,
        table: str,
        query: str,
        table_metadata: dict[str, str],
        semantic_model: dict[str, Any],
    ) -> float:
        query_terms = self._ranking_terms(query)
        if not query_terms:
            return 0.0
        table_terms = self._ranking_terms(self._table_semantic_text(table, table_metadata, semantic_model))
        overlap = query_terms & table_terms
        score = sum(max(1, len(term)) for term in overlap)
        columns = semantic_model.get(table) if isinstance(semantic_model, dict) else {}
        if isinstance(columns, dict):
            for meta in columns.values():
                if not isinstance(meta, dict):
                    continue
                phrases = [
                    str(meta.get("business_name") or ""),
                    str(meta.get("column_comment") or ""),
                    *self._split_terms(str(meta.get("synonyms") or "")),
                ]
                for phrase in phrases:
                    if phrase and phrase in query:
                        score += 6 + min(len(phrase), 8)
        return float(score)

    def _unique_strings(self, rows: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for row in rows:
            value = str(row).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            deduped.append(value)
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
