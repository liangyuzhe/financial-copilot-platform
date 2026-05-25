"""Configurable semantic metric definitions for SQL quality gates."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Iterable

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
class MetricColumnRule:
    """Configurable rule for mapping result columns to business metric roles."""

    role: str
    aliases: list[str]
    include_terms: list[str]
    exclude_terms: list[str] = field(default_factory=list)

    def matches_request(self, requested: Iterable[str]) -> bool:
        normalized_aliases = {_normalize_metric_text(alias) for alias in [self.role, *self.aliases]}
        return any(_normalize_metric_text(item) in normalized_aliases for item in requested)

    def matches_column(self, column: str) -> bool:
        normalized = _normalize_metric_text(column)
        if not normalized:
            return False
        exclude_terms = [_normalize_metric_text(term) for term in self.exclude_terms]
        include_terms = [_normalize_metric_text(term) for term in self.include_terms]
        if any(term and term in normalized for term in exclude_terms):
            return False
        return any(term and term in normalized for term in include_terms)


@dataclass(frozen=True, slots=True)
class MetricValidationResult:
    passed: bool
    metric_id: str
    problem_code: str = ""
    matched_signals: list[str] = field(default_factory=list)


def _normalize_metric_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "").lower())


def _default_metric_column_rules_path() -> Path:
    return Path(__file__).with_name("metric_column_rules.json")


def load_metric_column_rules(path: str | Path | None = None) -> list[MetricColumnRule]:
    """Load metric result-column matching rules from a JSON seed file."""
    rule_path = Path(path) if path is not None else _default_metric_column_rules_path()
    if not rule_path.exists():
        return []
    payload = json.loads(rule_path.read_text(encoding="utf-8"))
    items = payload.get("rules", payload) if isinstance(payload, dict) else payload
    rules = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if not role:
            continue
        rules.append(
            MetricColumnRule(
                role=role,
                aliases=[str(value) for value in item.get("aliases", [])],
                include_terms=[str(value) for value in item.get("include_terms", [])],
                exclude_terms=[str(value) for value in item.get("exclude_terms", [])],
            )
        )
    return rules


class MetricRegistry:
    """In-memory metric registry for deterministic SQL semantic checks."""

    def __init__(self, metrics: list[MetricDefinition], column_rules: list[MetricColumnRule] | None = None):
        self._metrics = {
            metric.metric_id: metric
            for metric in metrics
        }
        self._column_rules = column_rules or []

    def get(self, metric_id: str) -> MetricDefinition:
        return self._metrics[metric_id]

    def match_query(self, query: str) -> list[MetricDefinition]:
        text = query or ""
        matches: list[MetricDefinition] = []
        for metric in self._metrics.values():
            if any(name and name in text for name in metric.business_names):
                matches.append(metric)
        return matches

    def column_matches(self, column: str, requested_roles: Iterable[str]) -> bool:
        requested = list(requested_roles)
        return any(
            rule.matches_request(requested) and rule.matches_column(column)
            for rule in self._column_rules
        )


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
            )
        ],
        column_rules=load_metric_column_rules(),
    )


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
