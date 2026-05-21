from __future__ import annotations


def test_shadow_benchmark_default_cases_cover_routes_and_keep_strict_sql_on_harness():
    from agents.runtime.shadow_benchmark import ShadowBenchmark

    cases = ShadowBenchmark.default_cases()

    assert {case.task_type for case in cases} == {
        "strict_sql_query",
        "exploratory_analysis",
        "complex_analysis",
        "report_generation",
    }
    strict_case = next(case for case in cases if case.task_type == "strict_sql_query")
    assert strict_case.baseline_runtime == "sql_harness"
    assert strict_case.candidate_runtime == "sql_harness"
    assert ShadowBenchmark.should_enable_agentscope(
        task_type="strict_sql_query",
        summary={"latency": {"p95_ms": 100}, "quality": {"final_answer_usable_rate": 1.0}},
    ) is False


def test_shadow_benchmark_summarizes_latency_cost_and_quality_rates():
    from agents.runtime.shadow_benchmark import ShadowBenchmark, ShadowRunRecord

    records = [
        ShadowRunRecord(
            task_type="complex_analysis",
            runtime="agentscope",
            latency_ms=100,
            llm_calls=2,
            token_count=900,
            sql_draft_count=1,
            sql_draft_passed_count=1,
            approval_count=1,
            approval_passed_count=1,
            tool_call_count=4,
            tool_failure_count=0,
            final_answer_usable=True,
        ),
        ShadowRunRecord(
            task_type="complex_analysis",
            runtime="agentscope",
            latency_ms=200,
            llm_calls=3,
            token_count=1200,
            sql_draft_count=1,
            sql_draft_passed_count=0,
            approval_count=1,
            approval_passed_count=0,
            tool_call_count=4,
            tool_failure_count=1,
            final_answer_usable=False,
        ),
        ShadowRunRecord(
            task_type="complex_analysis",
            runtime="agentscope",
            latency_ms=500,
            llm_calls=1,
            token_count=600,
            sql_draft_count=0,
            sql_draft_passed_count=0,
            approval_count=0,
            approval_passed_count=0,
            tool_call_count=2,
            tool_failure_count=0,
            final_answer_usable=True,
        ),
    ]

    summary = ShadowBenchmark.summarize_group(records)

    assert summary["task_type"] == "complex_analysis"
    assert summary["runtime"] == "agentscope"
    assert summary["num_runs"] == 3
    assert summary["latency"] == {"avg_ms": 266.7, "p50_ms": 200, "p95_ms": 500}
    assert summary["cost"] == {"avg_llm_calls": 2.0, "avg_token_count": 900.0}
    assert summary["quality"]["sql_draft_pass_rate"] == 0.5
    assert summary["quality"]["approval_pass_rate"] == 0.5
    assert summary["quality"]["tool_failure_rate"] == 0.1
    assert summary["quality"]["final_answer_usable_rate"] == 0.6667


def test_shadow_benchmark_groups_records_by_task_and_runtime():
    from agents.runtime.shadow_benchmark import ShadowBenchmark, ShadowRunRecord

    report = ShadowBenchmark.summarize(
        [
            ShadowRunRecord(task_type="exploratory_analysis", runtime="agentscope", latency_ms=100),
            ShadowRunRecord(task_type="exploratory_analysis", runtime="sql_harness", latency_ms=80),
            ShadowRunRecord(task_type="report_generation", runtime="agentscope", latency_ms=40),
        ]
    )

    assert report["num_records"] == 3
    assert [(row["task_type"], row["runtime"]) for row in report["groups"]] == [
        ("exploratory_analysis", "agentscope"),
        ("exploratory_analysis", "sql_harness"),
        ("report_generation", "agentscope"),
    ]


def test_shadow_benchmark_enable_decision_uses_quality_thresholds():
    from agents.runtime.shadow_benchmark import ShadowBenchmark, ShadowThresholds

    good_summary = {
        "latency": {"p95_ms": 1200},
        "quality": {
            "tool_failure_rate": 0.02,
            "final_answer_usable_rate": 0.9,
            "sql_draft_pass_rate": 0.85,
            "approval_pass_rate": 0.9,
        },
    }
    thresholds = ShadowThresholds(
        p95_latency_ms=1500,
        max_tool_failure_rate=0.05,
        min_final_answer_usable_rate=0.8,
        min_sql_draft_pass_rate=0.8,
        min_approval_pass_rate=0.8,
    )

    assert ShadowBenchmark.should_enable_agentscope(
        task_type="complex_analysis",
        summary=good_summary,
        thresholds=thresholds,
    ) is True

    slow_summary = {**good_summary, "latency": {"p95_ms": 2000}}
    assert ShadowBenchmark.should_enable_agentscope(
        task_type="complex_analysis",
        summary=slow_summary,
        thresholds=thresholds,
    ) is False

    weak_summary = {
        **good_summary,
        "quality": {**good_summary["quality"], "final_answer_usable_rate": 0.7},
    }
    assert ShadowBenchmark.should_enable_agentscope(
        task_type="report_generation",
        summary=weak_summary,
        thresholds=thresholds,
    ) is False
