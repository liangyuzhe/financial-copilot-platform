"""Skill metadata registry for AgentScope runtime prompts and tool hints."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from agents.runtime.skill_contracts import RuntimeSkill


JsonDict = dict[str, Any]
SkillKind = Literal["prompt", "executable"]


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    """Serializable skill definition loaded from manifest metadata."""

    name: str
    description: str
    task_types: tuple[str, ...]
    keywords: tuple[str, ...] = ()
    prompt: str = ""
    tool_allowlist: tuple[str, ...] = ()
    output_format: JsonDict = field(default_factory=dict)
    examples: tuple[JsonDict, ...] = ()
    kind: SkillKind = "prompt"
    runtime_contract: RuntimeSkill | None = None

    @classmethod
    def from_manifest(cls, manifest_path: str | Path) -> "SkillDefinition":
        path = Path(manifest_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        skill_prompt = str(data.get("prompt", "") or "")
        skill_file = path.parent / "SKILL.md"
        if skill_file.exists():
            skill_prompt = skill_file.read_text(encoding="utf-8").strip()
        return cls(
            name=str(data["name"]),
            description=str(data.get("description", "") or ""),
            task_types=tuple(str(item) for item in data.get("task_types", []) or []),
            keywords=tuple(str(item) for item in data.get("keywords", []) or []),
            prompt=skill_prompt,
            tool_allowlist=tuple(str(item) for item in data.get("tool_allowlist", []) or []),
            output_format=dict(data.get("output_format") or {}),
            examples=tuple(dict(item) for item in data.get("examples", []) or []),
        )

    @classmethod
    def from_runtime_skill(
        cls,
        skill: RuntimeSkill,
        *,
        keywords: Iterable[str] = (),
        examples: Iterable[JsonDict] = (),
    ) -> "SkillDefinition":
        return cls(
            name=skill.name,
            description=skill.description,
            task_types=skill.task_types,
            keywords=tuple(str(item) for item in keywords),
            prompt="",
            tool_allowlist=skill.allowed_tools,
            output_format={"output_schema": dict(skill.output_schema)},
            examples=tuple(dict(item) for item in examples),
            kind="executable",
            runtime_contract=skill,
        )

    def matches(self, *, task_type: str, query: str) -> bool:
        if task_type not in self.task_types:
            return False
        normalized_query = query.lower()
        return any(keyword.lower() in normalized_query for keyword in self.keywords)

    def to_dict(self) -> JsonDict:
        return {
            "name": self.name,
            "description": self.description,
            "task_types": list(self.task_types),
            "keywords": list(self.keywords),
            "prompt": self.prompt,
            "tool_allowlist": list(self.tool_allowlist),
            "output_format": dict(self.output_format),
            "examples": [dict(item) for item in self.examples],
            "kind": self.kind,
            "runtime_contract": self.runtime_contract.to_dict() if self.runtime_contract else None,
        }


@dataclass(slots=True)
class SkillRegistry:
    """In-memory registry for built-in and manifest-loaded skills."""

    skills: Iterable[SkillDefinition] = ()
    _skills: dict[str, SkillDefinition] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._skills = {skill.name: skill for skill in self.skills}

    @classmethod
    def builtin(cls) -> "SkillRegistry":
        return cls(skills=_builtin_skills())

    @classmethod
    def from_paths(cls, paths: Iterable[str | Path]) -> "SkillRegistry":
        skills: list[SkillDefinition] = []
        for raw_path in paths:
            path = Path(raw_path)
            if path.is_file() and path.name == "manifest.json":
                skills.append(SkillDefinition.from_manifest(path))
                continue
            if not path.exists():
                continue
            for manifest_path in sorted(path.glob("*/manifest.json")):
                skills.append(SkillDefinition.from_manifest(manifest_path))
        return cls(skills=skills)

    def get(self, name: str) -> SkillDefinition:
        return self._skills[name]

    def list(self) -> list[SkillDefinition]:
        return list(self._skills.values())

    def match(
        self,
        *,
        task_type: str,
        query: str,
        enabled_skills: Iterable[str] | None = None,
    ) -> list[SkillDefinition]:
        if enabled_skills is not None:
            return [
                self._skills[name]
                for name in enabled_skills
                if name in self._skills and task_type in self._skills[name].task_types
            ]
        return [
            skill
            for skill in self._skills.values()
            if skill.matches(task_type=task_type, query=query)
        ]

    def allowed_tool_names(
        self,
        *,
        task_type: str,
        base_tool_names: list[str],
        skills: Iterable[SkillDefinition],
    ) -> list[str]:
        requested: set[str] = set()
        for skill in skills:
            if task_type not in skill.task_types:
                continue
            requested.update(skill.tool_allowlist)
        if not requested:
            return list(base_tool_names)
        return [tool_name for tool_name in base_tool_names if tool_name in requested]


def _builtin_skills() -> list[SkillDefinition]:
    from agents.runtime.skills.finance_relation_analysis import FinanceRelationAnalysisSkill

    report_sections = ["结论", "关键指标", "异常点", "后续追查建议"]
    return [
        SkillDefinition.from_runtime_skill(
            FinanceRelationAnalysisSkill().contract,
            keywords=("收入", "成本", "预算", "回款", "费用", "应收", "利润", "亏损", "净利"),
        ),
        SkillDefinition(
            name="budget_variance_analysis",
            description="预算差异分析方法，用于比较预算、实际和偏差原因。",
            task_types=("exploratory_analysis", "complex_analysis", "report_generation"),
            keywords=("预算", "差异", "偏差", "budget", "variance"),
            prompt=(
                "预算差异分析 skill：围绕预算金额、实际金额、执行率、偏差金额和偏差率组织分析；"
                "明确口径来源，不能绕过 SQL Harness 执行查询。"
            ),
            tool_allowlist=(
                "semantic_model.search",
                "business_knowledge.search",
                "schema.list_tables",
                "schema.describe_table",
                "schema.related_tables",
                "artifact.read",
                "report.render",
                "sql_draft.submit",
            ),
            output_format={"required_sections": report_sections},
        ),
        SkillDefinition(
            name="revenue_cost_relation",
            description="收入成本关系分析方法，用于解释收入、成本、毛利和异常倒挂。",
            task_types=("exploratory_analysis", "complex_analysis", "report_generation"),
            keywords=("收入", "成本", "毛利", "倒挂", "revenue", "cost", "gross margin"),
            prompt=(
                "收入成本关系 skill：优先说明收入、成本、毛利、毛利率之间的关系；"
                "区分已执行结果、分析草稿和需要 SQL Harness 处理的查询请求。"
            ),
            tool_allowlist=(
                "semantic_model.search",
                "business_knowledge.search",
                "schema.list_tables",
                "schema.describe_table",
                "schema.related_tables",
                "artifact.read",
                "report.render",
                "sql_draft.submit",
            ),
            output_format={"required_sections": report_sections},
        ),
    ]
