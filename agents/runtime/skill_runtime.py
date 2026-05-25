"""Runtime for executable skills that orchestrate ToolCatalog primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from agents.runtime.result import JsonDict
from agents.runtime.skill_contracts import ExecutableSkill, SkillResult


@dataclass(slots=True)
class SkillRuntime:
    """Invoke executable skills while preserving ToolCatalog as the primitive boundary."""

    skills: Iterable[ExecutableSkill] = ()
    _skills: dict[str, ExecutableSkill] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._skills = {}
        for skill in self.skills:
            self._skills[skill.contract.name] = skill

    def list_skills(self) -> list[ExecutableSkill]:
        return list(self._skills.values())

    def get(self, name: str) -> ExecutableSkill:
        return self._skills[name]

    async def invoke_skill(self, name: str, payload: JsonDict | None, context) -> SkillResult:
        skill = self.get(name)
        allowed = {tool.name for tool in context.tools}
        missing = [tool_name for tool_name in skill.contract.allowed_tools if tool_name not in allowed]
        if missing:
            return SkillResult(
                status="failed",
                skill_name=skill.contract.name,
                skill_version=skill.contract.version,
                execution_mode="failed",
                summary="Skill cannot run because required primitive tools are not available.",
                risk_flags=[
                    {
                        "code": "skill_missing_allowed_tools",
                        "severity": "error",
                        "missing_tools": missing,
                    }
                ],
            )

        start_index = len(context.tool_trace)
        active_spans = await context.start_chain_span(
            f"agentscope.skill.{skill.contract.name}",
            str(payload or {}),
            metadata={
                "span_layer": "skill",
                "real_call": True,
                "skill_name": skill.contract.name,
                "skill_version": skill.contract.version,
                "visible_functions": [skill.contract.name],
                "allowed_tools": list(skill.contract.allowed_tools),
            },
        )
        try:
            result = await skill.run(dict(payload or {}), context)
        except Exception as exc:
            await context.end_chain_span(
                active_spans,
                f"agentscope.skill.{skill.contract.name}",
                "error",
                str(exc),
            )
            raise

        child_tool_count = max(0, len(context.tool_trace) - start_index)
        context.events.append(
            {
                "event": "skill_result",
                "data": {
                    "skill_name": result.skill_name,
                    "skill_version": result.skill_version,
                    "status": result.status,
                    "execution_mode": result.execution_mode,
                    "child_tool_count": child_tool_count,
                },
            }
        )
        await context.end_chain_span(
            active_spans,
            f"agentscope.skill.{skill.contract.name}",
            {
                "status": result.status,
                "execution_mode": result.execution_mode,
                "summary": result.summary,
                "child_tool_count": child_tool_count,
            },
        )
        return result
