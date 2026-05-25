"""Policy for deciding which functions a runtime agent can see."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable

from agents.tool.token_counter import TokenCounter


DATA_ANALYSIS_SKILL_NAMES: tuple[str, ...] = ("finance_relation_analysis",)

DATA_ANALYSIS_PRIMITIVE_STAGES: dict[str, tuple[str, ...]] = {
    "": (
        "current_time.now",
        "business_knowledge.search",
        "schema.select_candidates",
    ),
    "current_time.now": (
        "business_knowledge.search",
        "schema.select_candidates",
    ),
    "business_knowledge.search": (
        "schema.select_candidates",
    ),
    "schema.select_candidates": (
        "semantic_model.search",
        "schema.related_tables",
        "analysis_plan.submit",
    ),
    "semantic_model.search": (
        "schema.related_tables",
        "analysis_plan.submit",
    ),
    "schema.related_tables": (
        "analysis_plan.submit",
    ),
    "analysis_plan.submit": (),
}


@dataclass(frozen=True, slots=True)
class ToolExposurePolicy:
    """Compute visible functions from task, stage, and allowed runtime names."""

    data_analysis_skill_names: tuple[str, ...] = DATA_ANALYSIS_SKILL_NAMES
    data_analysis_primitive_stages: dict[str, tuple[str, ...]] | None = None

    @classmethod
    def from_env(cls) -> "ToolExposurePolicy":
        raw = os.getenv("AGENTSCOPE_TOOL_EXPOSURE_POLICY_JSON", "").strip()
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        data_analysis = data.get("data_analysis")
        if not isinstance(data_analysis, dict):
            return cls()
        skill_names = _string_tuple(data_analysis.get("skill_names")) or DATA_ANALYSIS_SKILL_NAMES
        primitive_stages = _primitive_stages(data_analysis.get("primitive_stages"))
        return cls(
            data_analysis_skill_names=skill_names,
            data_analysis_primitive_stages=primitive_stages or None,
        )

    def visible_tool_names(
        self,
        *,
        task_type: str,
        base_tool_names: Iterable[str],
        expose_primitive_tools: bool = False,
        previous_tool_name: str = "",
    ) -> list[str]:
        base = list(dict.fromkeys(str(name) for name in base_tool_names if str(name)))
        if task_type != "data_analysis":
            return base

        if not expose_primitive_tools:
            return self._intersect(base, self.data_analysis_skill_names)

        stages = self.data_analysis_primitive_stages or DATA_ANALYSIS_PRIMITIVE_STAGES
        stage_names = stages.get(previous_tool_name, stages.get("", stages.get("start", ())))
        return self._intersect(base, stage_names)

    def _intersect(self, base_tool_names: list[str], allowed_names: Iterable[str]) -> list[str]:
        allowed = set(allowed_names)
        return [name for name in base_tool_names if name in allowed]


def tool_schema_diagnostics(
    schemas: Iterable[dict[str, Any]],
    *,
    visible_function_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    registered = [dict(schema) for schema in schemas]
    visible_set = set(visible_function_names or [])
    visible = [
        schema
        for schema in registered
        if not visible_set or _schema_function_name(schema) in visible_set
    ]
    registered_text = json.dumps(registered, ensure_ascii=False, sort_keys=True, default=str)
    visible_text = json.dumps(visible, ensure_ascii=False, sort_keys=True, default=str)
    counter = TokenCounter()
    return {
        "registered_function_count": len(registered),
        "visible_function_count": len(visible),
        "registered_function_names": [
            name for name in (_schema_function_name(schema) for schema in registered) if name
        ],
        "visible_function_names": [
            name for name in (_schema_function_name(schema) for schema in visible) if name
        ],
        "registered_schema_chars": len(registered_text),
        "visible_schema_chars": len(visible_text),
        "estimated_registered_tokens": counter.count(registered_text),
        "estimated_visible_tokens": counter.count(visible_text),
    }


def _schema_function_name(schema: dict[str, Any]) -> str:
    function = schema.get("function")
    if not isinstance(function, dict):
        return ""
    return str(function.get("name") or "")


def _primitive_stages(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        return {}
    stages: dict[str, tuple[str, ...]] = {}
    for key, raw_names in value.items():
        stage = "" if str(key) == "start" else str(key)
        names = _string_tuple(raw_names)
        if names or stage not in stages:
            stages[stage] = names
    return stages


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())
