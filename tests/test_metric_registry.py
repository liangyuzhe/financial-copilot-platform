"""Tests for SQL semantic metric definitions."""


def test_default_metric_registry_matches_net_profit_business_terms():
    from agents.tool.sql_tools.metric_registry import default_metric_registry

    registry = default_metric_registry()

    assert [metric.metric_id for metric in registry.match_query("去年亏损情况")] == ["net_profit"]
    assert [metric.metric_id for metric in registry.match_query("今年净利润")] == ["net_profit"]


def test_net_profit_metric_validates_sum_difference_shape():
    from agents.tool.sql_tools.metric_registry import default_metric_registry, validate_metric_shape
    from agents.tool.sql_tools.sql_shape import extract_sql_shape

    metric = default_metric_registry().get("net_profit")
    shape = extract_sql_shape(
        "SELECT SUM(ji.credit_amount - ji.debit_amount) AS 金额 FROM t_journal_item ji",
        dialect="mysql",
    )

    result = validate_metric_shape(metric, shape)

    assert result.passed is True
    assert result.matched_signals == ["metric_expression:net_profit"]


def test_net_profit_metric_rejects_single_sided_amount_shape():
    from agents.tool.sql_tools.metric_registry import default_metric_registry, validate_metric_shape
    from agents.tool.sql_tools.sql_shape import extract_sql_shape

    metric = default_metric_registry().get("net_profit")
    shape = extract_sql_shape(
        "SELECT SUM(ji.credit_amount) AS 金额 FROM t_journal_item ji",
        dialect="mysql",
    )

    result = validate_metric_shape(metric, shape)

    assert result.passed is False
    assert result.problem_code == "MISSING_METRIC_EXPRESSION"


def test_semantic_check_uses_supplied_metric_registry_for_non_hardcoded_metric():
    from agents.tool.sql_tools.metric_registry import MetricDefinition, MetricExpression, MetricRegistry
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    registry = MetricRegistry([
        MetricDefinition(
            metric_id="gross_margin",
            business_names=["毛利"],
            expression=MetricExpression(
                expression_type="sum_difference",
                aggregation="SUM",
                left_column="revenue_amount",
                right_column="cost_amount",
                operator="-",
            ),
        )
    ])

    report = check_sql_semantics(
        query="查询毛利",
        sql="SELECT SUM(o.revenue_amount - o.cost_amount) AS 毛利 FROM t_order o",
        metric_registry=registry,
    )

    assert report.passed is True
    assert report.decision == "safe_to_execute"
    assert "metric_expression:gross_margin" in report.matched_signals


def test_semantic_check_reports_generic_metric_error_for_non_hardcoded_metric():
    from agents.tool.sql_tools.metric_registry import MetricDefinition, MetricExpression, MetricRegistry
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    registry = MetricRegistry([
        MetricDefinition(
            metric_id="gross_margin",
            business_names=["毛利"],
            expression=MetricExpression(
                expression_type="sum_difference",
                aggregation="SUM",
                left_column="revenue_amount",
                right_column="cost_amount",
                operator="-",
            ),
        )
    ])

    report = check_sql_semantics(
        query="查询毛利",
        sql="SELECT SUM(o.revenue_amount) AS 毛利 FROM t_order o",
        metric_registry=registry,
    )

    assert report.passed is False
    assert report.problems[0].code == "MISSING_METRIC_EXPRESSION"
    assert "净利润" not in report.problems[0].title


def test_metric_registry_matches_columns_by_configured_rules():
    from agents.tool.sql_tools.metric_registry import default_metric_registry

    registry = default_metric_registry()

    assert registry.column_matches("total_budget", ("budget",))
    assert not registry.column_matches("budget_variance", ("budget",))
    assert registry.column_matches("budget_variance", ("variance",))
    assert not registry.column_matches("execution_rate", ("budget",))
    assert registry.column_matches("execution_rate", ("execution_rate",))
    assert registry.column_matches("total_approved_amount", ("approved_expense",))
    assert not registry.column_matches("expense_count", ("expense",))
    assert registry.column_matches("expense_count", ("expense_count",))
    assert registry.column_matches("net_profit", ("net_profit",))
    assert registry.column_matches("net_margin", ("net_margin",))
    assert registry.column_matches("gross_margin", ("gross_margin",))


def test_metric_column_rules_can_load_from_external_file(tmp_path):
    import json

    from agents.tool.sql_tools.metric_registry import MetricDefinition, MetricExpression, MetricRegistry, load_metric_column_rules

    rule_file = tmp_path / "metric_rules.json"
    rule_file.write_text(
        json.dumps({
            "rules": [
                {
                    "role": "custom_amount",
                    "aliases": ["自定义金额"],
                    "include_terms": ["custom_amount"],
                    "exclude_terms": ["rate"],
                }
            ]
        }),
        encoding="utf-8",
    )

    registry = MetricRegistry(
        metrics=[
            MetricDefinition(
                metric_id="custom_metric",
                business_names=["自定义"],
                expression=MetricExpression(
                    expression_type="sum_difference",
                    aggregation="SUM",
                    left_column="left_amount",
                    right_column="right_amount",
                    operator="-",
                ),
            )
        ],
        column_rules=load_metric_column_rules(rule_file),
    )

    assert registry.column_matches("custom_amount", ("自定义金额",))
    assert not registry.column_matches("custom_amount_rate", ("自定义金额",))
