"""Executable skill contracts for AgentScope-facing runtime abilities."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from agents.runtime.result import JsonDict


@dataclass(frozen=True, slots=True)
class SkillTracePolicy:
    """Controls how much skill execution detail is returned to the LLM."""

    expose_child_tool_trace_to_llm: bool = False
    observation_mode: str = "summary"
    max_observation_chars: int = 4000
    max_evidence_items: int = 8

    def to_dict(self) -> JsonDict:
        return {
            "expose_child_tool_trace_to_llm": self.expose_child_tool_trace_to_llm,
            "observation_mode": self.observation_mode,
            "max_observation_chars": self.max_observation_chars,
            "max_evidence_items": self.max_evidence_items,
        }


@dataclass(frozen=True, slots=True)
class RuntimeSkill:
    """Static descriptor for an executable runtime skill."""

    name: str
    version: str
    description: str
    task_types: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    input_schema: JsonDict
    output_schema: JsonDict
    execution_modes: tuple[str, ...] = ()
    trace_policy: SkillTracePolicy = field(default_factory=SkillTracePolicy)

    def to_dict(self) -> JsonDict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "task_types": list(self.task_types),
            "allowed_tools": list(self.allowed_tools),
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "execution_modes": list(self.execution_modes),
            "trace_policy": self.trace_policy.to_dict(),
        }


@dataclass(slots=True)
class SkillResult:
    """Normalized result returned by an executable skill."""

    status: str
    skill_name: str
    skill_version: str
    execution_mode: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    analysis_plan: JsonDict = field(default_factory=dict)
    clarification_questions: list[str] = field(default_factory=list)
    artifacts: JsonDict = field(default_factory=dict)
    trace_refs: list[str] = field(default_factory=list)
    risk_flags: list[JsonDict] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "status": self.status,
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "execution_mode": self.execution_mode,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "analysis_plan": dict(self.analysis_plan),
            "clarification_questions": list(self.clarification_questions),
            "artifacts": dict(self.artifacts),
            "trace_refs": list(self.trace_refs),
            "risk_flags": [dict(item) for item in self.risk_flags],
        }

    def to_observation(self, *, max_chars: int = 4000, max_evidence_items: int = 8) -> JsonDict:
        payload: JsonDict = {
            "status": self.status,
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "execution_mode": self.execution_mode,
            "summary": self.summary,
            "evidence": list(self.evidence[:max_evidence_items]),
            "clarification_questions": list(self.clarification_questions),
        }
        if self.analysis_plan:
            payload["analysis_plan"] = self._analysis_plan_summary(self.analysis_plan)
        if self.risk_flags:
            payload["risk_flags"] = [dict(item) for item in self.risk_flags[:5]]
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) <= max_chars:
            return payload
        payload["evidence"] = list(self.evidence[:2])
        payload["summary"] = self.summary[: max(200, max_chars // 3)]
        return payload

    def _analysis_plan_summary(self, plan: JsonDict) -> JsonDict:
        steps = []
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            steps.append(
                {
                    "step": step.get("step"),
                    "type": step.get("type"),
                    "goal": step.get("goal", ""),
                    "tables": list(step.get("tables") or []),
                    "depends_on": list(step.get("depends_on") or []),
                    "merge_keys": list(step.get("merge_keys") or []),
                    "has_sql": bool(str(step.get("sql") or "").strip()),
                }
            )
        return {
            "mode": plan.get("mode", ""),
            "execution_mode": plan.get("execution_mode", ""),
            "reason": plan.get("reason", ""),
            "steps": steps,
            "requires_user_confirmation": bool(plan.get("requires_user_confirmation", True)),
        }


class ExecutableSkill(Protocol):
    """Protocol implemented by concrete skill runtimes."""

    contract: RuntimeSkill

    async def run(self, payload: JsonDict, context: Any) -> SkillResult:
        """Execute the skill and return a normalized result."""
