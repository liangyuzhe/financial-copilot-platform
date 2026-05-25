from __future__ import annotations

import json


def test_builtin_skill_registry_matches_budget_and_revenue_skills():
    from agents.runtime.skill_registry import SkillRegistry

    registry = SkillRegistry.builtin()

    budget = registry.match(
        task_type="exploratory_analysis",
        query="分析预算执行差异和费用偏差",
    )
    assert [skill.name for skill in budget] == ["budget_variance_analysis"]
    assert "预算差异" in budget[0].prompt
    assert "结论" in budget[0].output_format["required_sections"]

    revenue_cost = registry.match(
        task_type="exploratory_analysis",
        query="收入和成本之间的关系是什么？",
    )
    assert [skill.name for skill in revenue_cost] == ["revenue_cost_relation"]
    assert "收入成本关系" in revenue_cost[0].prompt


def test_skill_registry_loads_manifest_and_skill_prompt_from_directory(tmp_path):
    from agents.runtime.skill_registry import SkillRegistry

    skill_dir = tmp_path / "cash_flow_review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "现金流复核 skill prompt",
        encoding="utf-8",
    )
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "cash_flow_review",
                "description": "现金流复核",
                "task_types": ["report_generation"],
                "keywords": ["现金流", "回款"],
                "tool_allowlist": ["artifact.read", "report.render", "sql.execute"],
                "output_format": {
                    "required_sections": ["结论", "关键指标", "异常点", "后续追查建议"]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    registry = SkillRegistry.from_paths([tmp_path])
    skill = registry.get("cash_flow_review")

    assert skill.name == "cash_flow_review"
    assert skill.task_types == ("report_generation",)
    assert skill.prompt == "现金流复核 skill prompt"
    assert skill.tool_allowlist == ("artifact.read", "report.render", "sql.execute")
    assert registry.match(task_type="report_generation", query="生成现金流回款报告")[0].name == "cash_flow_review"


def test_skill_tool_allowlist_is_intersected_with_task_tools():
    from agents.runtime.skill_registry import SkillDefinition, SkillRegistry

    registry = SkillRegistry(
        skills=[
            SkillDefinition(
                name="dangerous_skill",
                description="tries to request unsafe tools",
                task_types=("report_generation",),
                keywords=("报告",),
                prompt="不要绕过 harness",
                tool_allowlist=("artifact.read", "report.render", "sql.execute", "schema.list_tables"),
                output_format={"required_sections": ["结论"]},
            )
        ]
    )

    safe_tools = registry.allowed_tool_names(
        task_type="report_generation",
        base_tool_names=["artifact.read", "report.render"],
        skills=registry.match(task_type="report_generation", query="报告"),
    )

    assert safe_tools == ["artifact.read", "report.render"]


def test_skill_output_format_is_serializable_and_fixed():
    from agents.runtime.skill_registry import SkillRegistry

    registry = SkillRegistry.builtin()
    skill = registry.get("revenue_cost_relation")

    data = skill.to_dict()

    assert data["name"] == "revenue_cost_relation"
    assert data["task_types"] == ["exploratory_analysis", "complex_analysis", "report_generation"]
    assert data["output_format"]["required_sections"] == [
        "结论",
        "关键指标",
        "异常点",
        "后续追查建议",
    ]


def test_skill_registry_can_register_executable_runtime_skill_definition():
    from agents.runtime.skill_registry import SkillDefinition, SkillRegistry
    from agents.runtime.skills.finance_relation_analysis import FinanceRelationAnalysisSkill

    runtime_skill = FinanceRelationAnalysisSkill().contract
    executable = SkillDefinition.from_runtime_skill(
        runtime_skill,
        keywords=("收入", "成本", "预算", "回款", "费用", "亏损"),
    )
    registry = SkillRegistry(
        skills=[
            SkillDefinition(
                name="revenue_cost_relation",
                description="prompt-only skill",
                task_types=("complex_analysis",),
                keywords=("收入", "成本"),
                prompt="收入成本关系 prompt",
                tool_allowlist=("business_knowledge.search",),
            ),
            executable,
        ]
    )

    matched = registry.match(
        task_type="data_analysis",
        query="分析收入成本预算回款费用之间的关系",
    )

    assert [skill.name for skill in matched] == ["finance_relation_analysis"]
    assert matched[0].kind == "executable"
    assert matched[0].runtime_contract == runtime_skill
    assert matched[0].tool_allowlist == runtime_skill.allowed_tools
    assert matched[0].prompt == ""
    assert matched[0].to_dict()["runtime_contract"]["name"] == "finance_relation_analysis"


def test_executable_skill_allowlist_still_intersects_base_tools():
    from agents.runtime.skill_registry import SkillDefinition, SkillRegistry
    from agents.runtime.skills.finance_relation_analysis import FinanceRelationAnalysisSkill

    registry = SkillRegistry(
        skills=[
            SkillDefinition.from_runtime_skill(
                FinanceRelationAnalysisSkill().contract,
                keywords=("亏损",),
            )
        ]
    )

    allowed = registry.allowed_tool_names(
        task_type="data_analysis",
        base_tool_names=[
            "business_knowledge.search",
            "analysis_plan.submit",
            "sql.execute",
        ],
        skills=registry.match(task_type="data_analysis", query="去年亏损"),
    )

    assert allowed == ["business_knowledge.search", "analysis_plan.submit"]


def test_builtin_registry_includes_finance_relation_executable_skill():
    from agents.runtime.skill_registry import SkillRegistry

    registry = SkillRegistry.builtin()
    skill = registry.get("finance_relation_analysis")

    assert skill.kind == "executable"
    assert skill.runtime_contract is not None
    assert skill.runtime_contract.name == "finance_relation_analysis"
    assert "analysis_plan.submit" in skill.tool_allowlist
    assert skill in registry.match(
        task_type="data_analysis",
        query="去年亏损",
    )
