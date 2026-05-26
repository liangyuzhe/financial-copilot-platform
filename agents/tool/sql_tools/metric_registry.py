"""Semantic metric definitions for SQL quality gates."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

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
    output_aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MetricValidationResult:
    passed: bool
    metric_id: str
    problem_code: str = ""
    matched_signals: list[str] = field(default_factory=list)


def _normalize_metric_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "").lower())


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

    def match_output_aliases(self, aliases: list[str]) -> list[MetricDefinition]:
        """Return governed metrics whose declared output aliases appear in SQL output."""
        normalized_aliases = {_normalize_metric_text(alias) for alias in aliases if str(alias or "").strip()}
        if not normalized_aliases:
            return []
        matches: list[MetricDefinition] = []
        for metric in self._metrics.values():
            candidates = metric.output_aliases or [metric.metric_id, *metric.business_names]
            normalized_candidates = {
                _normalize_metric_text(candidate)
                for candidate in candidates
                if str(candidate or "").strip()
            }
            if normalized_aliases & normalized_candidates:
                matches.append(metric)
        return matches


def default_metric_registry() -> MetricRegistry:
    """Return built-in metric definitions until they are DB/config-backed."""
    return MetricRegistry(
        [
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
                output_aliases=["net_profit", "profit", "loss", "loss_amount", "净利润", "利润", "亏损", "亏损金额"],
            )
        ],
    )


def validate_metric_shape(metric: MetricDefinition, shape: SqlShape) -> MetricValidationResult:
    """Validate whether a SQL shape contains the configured metric expression."""
    expression = metric.expression
    if expression.expression_type == "sum_difference":
        aggregations = _target_metric_aggregations(metric, shape) or shape.aggregations
        saw_reversed_expression = False
        for aggregation in aggregations:
            if aggregation.function.upper() != expression.aggregation.upper():
                continue
            refs = {name for _table_alias, name in aggregation.column_refs}
            if not {expression.left_column, expression.right_column}.issubset(refs):
                continue
            if _contains_reversed_difference(
                aggregation.sql,
                left_column=expression.left_column,
                right_column=expression.right_column,
            ):
                saw_reversed_expression = True
                continue
            if expression.operator in aggregation.sql:
                return MetricValidationResult(
                    passed=True,
                    metric_id=metric.metric_id,
                    matched_signals=[f"metric_expression:{metric.metric_id}"],
                )
        if saw_reversed_expression:
            return MetricValidationResult(
                passed=False,
                metric_id=metric.metric_id,
                problem_code="REVERSED_METRIC_EXPRESSION",
            )
    return MetricValidationResult(
        passed=False,
        metric_id=metric.metric_id,
        problem_code="MISSING_METRIC_EXPRESSION",
    )


def _target_metric_aggregations(metric: MetricDefinition, shape: SqlShape) -> list:
    aliases = [_normalize_metric_text(alias) for alias in metric.output_aliases]
    if not aliases:
        aliases = [_normalize_metric_text(metric.metric_id), *(_normalize_metric_text(name) for name in metric.business_names)]
    aliases = [alias for alias in aliases if alias]
    if not aliases:
        return []
    return [
        aggregation
        for aggregation in shape.aggregations
        if _normalize_metric_text(getattr(aggregation, "alias", "")) in aliases
    ]


def _contains_reversed_difference(sql: str, *, left_column: str, right_column: str) -> bool:
    """Return true when one aggregate mixes left-right with right-left.

    A configured ``sum_difference`` metric has a directional meaning. Seeing
    both directions in the same aggregate is usually a sign that the SQL turned
    costs into positive profit by adding the reverse branch back into the same
    metric.
    """
    if not str(left_column or "").strip() or not str(right_column or "").strip():
        return False
    text = sql or ""
    forward = _difference_pattern(left_column, right_column)
    reverse = _difference_pattern(right_column, left_column)
    return bool(forward.search(text) and any(not _is_unary_negated(text, match.start()) for match in reverse.finditer(text)))


def _is_unary_negated(sql: str, match_start: int) -> bool:
    prefix = sql[:match_start].rstrip()
    return prefix.endswith("-") or prefix.endswith("-(")


def _difference_pattern(left_column: str, right_column: str) -> re.Pattern:
    return re.compile(
        rf"{_sql_column_pattern(left_column)}\s*-\s*{_sql_column_pattern(right_column)}",
        re.IGNORECASE,
    )


def _sql_column_pattern(column: str) -> str:
    name = re.escape(str(column or "").strip("` "))
    optional_alias = r"(?:`?[A-Za-z_][\w]*`?\s*\.\s*)?"
    return rf"{optional_alias}`?{name}`?"
