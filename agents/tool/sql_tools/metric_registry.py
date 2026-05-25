"""Configurable semantic metric definitions for SQL quality gates."""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.tool.sql_tools.sql_shape import SqlShape


@dataclass(frozen=True, slots=True)
class MetricExpression:
    """A metric expression definition that can be matched against SqlShape."""

    expression_type: str
    aggregation: str
    left_column: str
    right_column: str = ""
    operator: str = ""


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric_id: str
    business_names: list[str]
    expression: MetricExpression
    required_tables: list[str] = field(default_factory=list)
    required_filters: list[dict] = field(default_factory=list)
    time_field: str = ""


@dataclass(frozen=True, slots=True)
class MetricValidationResult:
    passed: bool
    metric_id: str
    problem_code: str = ""
    matched_signals: list[str] = field(default_factory=list)


class MetricRegistry:
    """In-memory metric registry for deterministic SQL semantic checks."""

    def __init__(self, metrics: list[MetricDefinition]):
        self._metrics = {
            metric.metric_id: metric
            for metric in metrics
        }

    def get(self, metric_id: str) -> MetricDefinition:
        return self._metrics[metric_id]

    def match_query(self, query: str) -> list[MetricDefinition]:
        text = query or ""
        matches: list[MetricDefinition] = []
        for metric in self._metrics.values():
            if any(name and name in text for name in metric.business_names):
                matches.append(metric)
        return matches


def default_metric_registry() -> MetricRegistry:
    """Return built-in metric definitions until they are DB/config-backed."""
    return MetricRegistry([
        MetricDefinition(
            metric_id="net_profit",
            business_names=["净利润", "利润", "盈亏", "亏损", "盈利"],
            expression=MetricExpression(
                expression_type="sum_difference",
                aggregation="SUM",
                left_column="credit_amount",
                right_column="debit_amount",
                operator="-",
            ),
            required_tables=["t_journal_item"],
        )
    ])


def validate_metric_shape(metric: MetricDefinition, shape: SqlShape) -> MetricValidationResult:
    """Validate whether a SQL shape contains the configured metric expression."""
    expression = metric.expression
    if expression.expression_type == "sum_difference":
        for aggregation in shape.aggregations:
            if aggregation.function.upper() != expression.aggregation.upper():
                continue
            refs = {name for _table_alias, name in aggregation.column_refs}
            if {expression.left_column, expression.right_column}.issubset(refs) and expression.operator in aggregation.sql:
                return MetricValidationResult(
                    passed=True,
                    metric_id=metric.metric_id,
                    matched_signals=[f"metric_expression:{metric.metric_id}"],
                )
    return MetricValidationResult(
        passed=False,
        metric_id=metric.metric_id,
        problem_code="MISSING_METRIC_EXPRESSION",
    )
