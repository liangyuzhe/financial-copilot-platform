from __future__ import annotations


def test_tool_exposure_policy_defaults_to_skill_only_for_data_analysis():
    from agents.runtime.tool_exposure_policy import ToolExposurePolicy

    policy = ToolExposurePolicy()

    decision = policy.visible_tool_names(
        task_type="data_analysis",
        base_tool_names=[
            "query.context_rewrite",
            "business_knowledge.search",
            "schema.list_tables",
            "schema.select_candidates",
            "semantic_model.search",
            "schema.related_tables",
            "plan.assess_feasibility",
            "sql.normalize",
            "sql.safety_check",
            "sql.authorize_draft",
            "current_time.now",
            "analysis_plan.submit",
            "finance_relation_analysis",
        ],
    )

    assert decision == ["finance_relation_analysis"]


def test_tool_exposure_policy_can_expose_stage_primitives_in_debug_mode():
    from agents.runtime.tool_exposure_policy import ToolExposurePolicy

    policy = ToolExposurePolicy()

    decision = policy.visible_tool_names(
        task_type="data_analysis",
        base_tool_names=[
            "query.context_rewrite",
            "business_knowledge.search",
            "schema.list_tables",
            "schema.select_candidates",
            "semantic_model.search",
            "schema.related_tables",
            "plan.assess_feasibility",
            "sql.normalize",
            "sql.safety_check",
            "sql.authorize_draft",
            "current_time.now",
            "analysis_plan.submit",
            "finance_relation_analysis",
        ],
        expose_primitive_tools=True,
        previous_tool_name="business_knowledge.search",
    )

    assert decision == [
        "schema.select_candidates",
    ]


def test_tool_exposure_policy_can_be_loaded_from_json_env(monkeypatch):
    from agents.runtime.tool_exposure_policy import ToolExposurePolicy

    monkeypatch.setenv(
        "AGENTSCOPE_TOOL_EXPOSURE_POLICY_JSON",
        """
        {
          "data_analysis": {
            "skill_names": ["finance_relation_analysis", "budget_variance_analysis"],
            "primitive_stages": {
              "start": ["business_knowledge.search"],
              "business_knowledge.search": ["semantic_model.search", "analysis_plan.submit"]
            }
          }
        }
        """,
    )

    policy = ToolExposurePolicy.from_env()

    assert policy.visible_tool_names(
        task_type="data_analysis",
        base_tool_names=[
            "finance_relation_analysis",
            "budget_variance_analysis",
            "business_knowledge.search",
        ],
    ) == ["finance_relation_analysis", "budget_variance_analysis"]
    assert policy.visible_tool_names(
        task_type="data_analysis",
        base_tool_names=[
            "business_knowledge.search",
            "semantic_model.search",
            "analysis_plan.submit",
        ],
        expose_primitive_tools=True,
    ) == ["business_knowledge.search"]
    assert policy.visible_tool_names(
        task_type="data_analysis",
        base_tool_names=[
            "business_knowledge.search",
            "semantic_model.search",
            "analysis_plan.submit",
        ],
        expose_primitive_tools=True,
        previous_tool_name="business_knowledge.search",
    ) == ["semantic_model.search", "analysis_plan.submit"]


def test_tool_schema_diagnostics_reports_visible_schema_size():
    from agents.runtime.tool_exposure_policy import tool_schema_diagnostics

    schemas = [
        {
            "type": "function",
            "function": {
                "name": "finance_relation_analysis",
                "description": "Analyze finance relation",
                "parameters": {"type": "object"},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "schema_select_candidates",
                "description": "Select candidates",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        },
    ]

    report = tool_schema_diagnostics(
        schemas,
        visible_function_names=["finance_relation_analysis"],
    )

    assert report["registered_function_count"] == 2
    assert report["visible_function_count"] == 1
    assert report["visible_function_names"] == ["finance_relation_analysis"]
    assert report["registered_schema_chars"] > report["visible_schema_chars"]
    assert report["estimated_visible_tokens"] > 0


def test_tool_exposure_policy_script_is_importable():
    import scripts.diagnose_tool_exposure as script

    assert callable(script.main)
