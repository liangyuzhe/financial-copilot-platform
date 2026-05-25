"""Complex NL2SQL routing and plan validation helpers.

This module is intentionally structural. Runtime business keywords belong in
metadata, database-backed rules, or LLM arbitration, not in Python constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MAX_COMPLEX_PLAN_STEPS = 5

ExecutionMode = Literal["single_sql", "single_sql_with_strict_checks", "complex_plan", "clarify"]
TaskType = Literal["analysis", "report", "comparison", "detail", "export", "sensitive", "ambiguous"]
JoinRisk = Literal["low", "medium", "high"]
VALID_STEP_TYPES = {"sql", "python_merge", "report"}
DECOMPOSABLE_TASK_TYPES = {"analysis", "report", "comparison"}
CLARIFY_TASK_TYPES = {"detail", "export", "sensitive"}
DECOMPOSABLE_TASK_SIGNAL_TERMS = (
    "分析",
    "对比",
    "比较",
    "差异",
    "偏差",
    "趋势",
    "关系",
    "关联",
    "影响",
    "报告",
)


@dataclass(frozen=True)
class FeasibilityDecision:
    execution_mode: ExecutionMode
    selected_tables_count: int
    relationship_count: int
    estimated_join_count: int
    task_type: TaskType
    can_single_sql: bool
    can_decompose: bool
    needs_clarification: bool
    join_risk: JoinRisk
    decision_source: str
    reason: str

    @property
    def route_mode(self) -> ExecutionMode:
        """Backward-compatible alias for older evaluation callers."""
        return self.execution_mode

    @property
    def query_intent_complexity(self) -> TaskType:
        """Backward-compatible alias for older reports."""
        return self.task_type


def assess_query_feasibility(
    query: str,
    selected_tables: list[str],
    relationships: list[dict],
    task_type: TaskType | str | None = None,
    decision_source: str = "rules",
) -> FeasibilityDecision:
    """Assess the execution mode from rules and schema structure.

    The query text is intentionally not parsed here. Runtime business semantics
    should come from DB-backed route rules, recall context, and schema metadata.
    """
    del query

    unique_tables = list(dict.fromkeys(selected_tables))
    selected_count = len(unique_tables)
    relationship_count = len(relationships or [])
    estimated_join_count = max(0, selected_count - 1)
    normalized_task_type = _normalize_task_type(task_type)
    components = _schema_components(unique_tables, relationships or [])
    schema_connected = len(components) <= 1
    join_risk = _join_risk(unique_tables, relationships or [])

    if not unique_tables:
        return FeasibilityDecision(
            execution_mode="clarify",
            selected_tables_count=selected_count,
            relationship_count=relationship_count,
            estimated_join_count=estimated_join_count,
            task_type=normalized_task_type,
            can_single_sql=False,
            can_decompose=False,
            needs_clarification=True,
            join_risk=join_risk,
            decision_source=decision_source,
            reason="no executable schema was selected",
        )

    if normalized_task_type in CLARIFY_TASK_TYPES:
        return FeasibilityDecision(
            execution_mode="clarify",
            selected_tables_count=selected_count,
            relationship_count=relationship_count,
            estimated_join_count=estimated_join_count,
            task_type=normalized_task_type,
            can_single_sql=False,
            can_decompose=False,
            needs_clarification=True,
            join_risk=join_risk,
            decision_source=decision_source,
            reason="configured task type should be clarified before execution",
        )

    if normalized_task_type in DECOMPOSABLE_TASK_TYPES:
        return FeasibilityDecision(
            execution_mode="complex_plan",
            selected_tables_count=selected_count,
            relationship_count=relationship_count,
            estimated_join_count=estimated_join_count,
            task_type=normalized_task_type,
            can_single_sql=False,
            can_decompose=True,
            needs_clarification=False,
            join_risk=join_risk,
            decision_source=decision_source,
            reason="configured task type is decomposable",
        )

    if not schema_connected:
        return FeasibilityDecision(
            execution_mode="clarify",
            selected_tables_count=selected_count,
            relationship_count=relationship_count,
            estimated_join_count=estimated_join_count,
            task_type=normalized_task_type,
            can_single_sql=False,
            can_decompose=False,
            needs_clarification=True,
            join_risk=join_risk,
            decision_source=decision_source,
            reason="selected schema has disconnected relationship components",
        )

    if join_risk == "medium":
        return FeasibilityDecision(
            execution_mode="single_sql_with_strict_checks",
            selected_tables_count=selected_count,
            relationship_count=relationship_count,
            estimated_join_count=estimated_join_count,
            task_type=normalized_task_type,
            can_single_sql=True,
            can_decompose=False,
            needs_clarification=False,
            join_risk=join_risk,
            decision_source=decision_source,
            reason="selected schema is connected but has multiple join paths",
        )

    return FeasibilityDecision(
        execution_mode="single_sql",
        selected_tables_count=selected_count,
        relationship_count=relationship_count,
        estimated_join_count=estimated_join_count,
        task_type=normalized_task_type,
        can_single_sql=True,
        can_decompose=False,
        needs_clarification=False,
        join_risk=join_risk,
        decision_source=decision_source,
        reason="selected schema is connected and suitable for single SQL",
    )


def classify_query_complexity(
    query: str,
    selected_tables: list[str],
    relationships: list[dict],
    route_signal: TaskType | str | None = None,
) -> FeasibilityDecision:
    """Compatibility wrapper for older evaluation code."""
    return assess_query_feasibility(
        query=query,
        selected_tables=selected_tables,
        relationships=relationships,
        task_type=route_signal,
    )


def infer_task_type_from_recall_context(
    *,
    query: str,
    selected_tables: list[str],
    recall_context: dict | None,
    query_variants: list[str] | None = None,
) -> tuple[TaskType | str, dict]:
    """Infer a decomposable task type from runtime recall evidence.

    Business semantics stay in recall data: matched business terms and related
    tables come from business knowledge/few-shot retrieval. The query text is
    only used for generic task-action signals such as analysis/comparison.
    """
    if not isinstance(recall_context, dict):
        return "", {}

    explicit_task_type = _normalize_task_type(str(recall_context.get("task_type") or ""))
    if explicit_task_type != "ambiguous":
        return explicit_task_type, {
            "reason": "task type supplied by recall context",
            "task_type": explicit_task_type,
        }

    query_text = " ".join(
        str(value or "")
        for value in [
            query,
            *(query_variants or []),
        ]
    )
    matched_terms = _unique_strings([
        str(term).strip()
        for term in recall_context.get("matched_terms", []) or []
        if str(term).strip()
    ])
    related_tables = _unique_strings([
        str(table).strip()
        for table in [
            *(recall_context.get("business_related_tables") or []),
            *(recall_context.get("few_shot_related_tables") or []),
        ]
        if str(table).strip()
    ])
    selected_count = len(set(selected_tables or []))
    related_selected_count = len(set(related_tables) & set(selected_tables or []))

    if (
        _has_decomposable_task_signal(query_text)
        and len(matched_terms) >= 3
        and (selected_count >= 3 or related_selected_count >= 3 or len(related_tables) >= 3)
    ):
        return "analysis", {
            "matched_terms": matched_terms,
            "business_related_tables_count": len(related_tables),
            "selected_tables_count": selected_count,
            "related_selected_tables_count": related_selected_count,
            "reason": "multi-term decomposable analysis inferred from recall context",
        }
    return "", {}


def _normalize_task_type(value: str | None) -> TaskType:
    task_type = (value or "ambiguous").strip().lower()
    if task_type in {"analysis", "report", "comparison", "detail", "export", "sensitive", "ambiguous"}:
        return task_type  # type: ignore[return-value]
    return "ambiguous"


def _has_decomposable_task_signal(query: str) -> bool:
    return any(term in query for term in DECOMPOSABLE_TASK_SIGNAL_TERMS)


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _schema_components(selected_tables: list[str], relationships: list[dict]) -> list[set[str]]:
    tables = set(selected_tables)
    if not tables:
        return []

    parent = {table: table for table in tables}

    def find(table: str) -> str:
        while parent[table] != table:
            parent[table] = parent[parent[table]]
            table = parent[table]
        return table

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for rel in relationships or []:
        left = rel.get("from_table")
        right = rel.get("to_table")
        if left in tables and right in tables:
            union(left, right)

    components: dict[str, set[str]] = {}
    for table in tables:
        components.setdefault(find(table), set()).add(table)
    return list(components.values())


def _has_multiple_join_paths(selected_tables: list[str], relationships: list[dict]) -> bool:
    tables = set(selected_tables)
    edges = {
        tuple(sorted((rel.get("from_table"), rel.get("to_table"))))
        for rel in relationships or []
        if rel.get("from_table") in tables and rel.get("to_table") in tables
    }
    if not tables or len(_schema_components(selected_tables, relationships)) != 1:
        return False
    # In an undirected connected graph, more edges than a tree means at least
    # one alternate join path. That should run with stricter SQL checks.
    return len(edges) > max(0, len(tables) - 1)


def _join_risk(selected_tables: list[str], relationships: list[dict]) -> JoinRisk:
    components = _schema_components(selected_tables, relationships)
    if not selected_tables or len(components) > 1:
        return "high"
    if _has_multiple_join_paths(selected_tables, relationships):
        return "medium"
    return "low"


def validate_complex_plan(plan: dict, allowed_tables: set[str]) -> tuple[bool, str]:
    """Validate a planner output before any SQL generation or execution."""
    if not isinstance(plan, dict):
        return False, "plan must be an object"

    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return False, "plan.steps must be a non-empty list"
    if len(steps) > MAX_COMPLEX_PLAN_STEPS:
        return False, f"plan has too many steps: {len(steps)}"

    seen_steps = set()
    has_sql = False
    for item in steps:
        if not isinstance(item, dict):
            return False, "each step must be an object"

        step_no = item.get("step")
        step_type = item.get("type")
        if not isinstance(step_no, int):
            return False, "each step must have integer step"
        if step_no in seen_steps:
            return False, f"duplicate step: {step_no}"
        seen_steps.add(step_no)

        if step_type not in VALID_STEP_TYPES:
            return False, f"unsupported step type: {step_type}"
        if not item.get("goal"):
            return False, f"step {step_no} missing goal"

        tables = item.get("tables") or []
        if step_type == "sql":
            has_sql = True
            if not tables:
                return False, f"sql step {step_no} missing tables"
            unknown = set(tables) - allowed_tables
            if unknown:
                return False, f"step {step_no} uses unknown table(s): {sorted(unknown)}"

        depends_on = item.get("depends_on") or []
        if any(dep not in seen_steps for dep in depends_on):
            return False, f"step {step_no} depends on unknown or future step"
        if step_type == "python_merge" and not item.get("merge_keys"):
            return False, f"python_merge step {step_no} missing merge_keys"

    if not has_sql:
        return False, "plan must include at least one sql step"
    return True, ""
