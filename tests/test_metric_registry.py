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
