"""Semantic consistency checks for SQL drafts.

This module provides a deterministic quality gate that compares a SQL draft
with the query intent and returns an explainable pass/fail report. It is
deliberately conservative: it blocks obvious intent mismatches, but keeps
domain-specific warnings soft when the intent signal is incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable

from agents.tool.sql_tools.metric_registry import MetricRegistry, default_metric_registry, validate_metric_shape
from agents.tool.sql_tools.sql_shape import SqlParseError, SqlShape, extract_sql_shape
from agents.tool.sql_tools.sql_validation import validate_sql_relationships, validate_sql_schema


@dataclass(frozen=True, slots=True)
class SemanticCheckProblem:
    """One semantic mismatch found between query intent and SQL draft."""

    code: str
    title: str
    severity: str
    message: str
    why: str
    expected: str
    actual: str
    evidence: str = ""
    repair_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "title": self.title,
            "severity": self.severity,
            "message": self.message,
            "why": self.why,
            "expected": self.expected,
            "actual": self.actual,
            "evidence": self.evidence,
            "repair_hint": self.repair_hint,
        }


@dataclass(frozen=True, slots=True)
class QualityGateReport:
    """One stage report in the SQL quality gate pipeline."""

    name: str
    passed: bool
    decision: str
    score: float = 1.0
    problems: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    extracted_facts: dict = field(default_factory=dict)
    repair_hints: list[str] = field(default_factory=list)
    matched_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "decision": self.decision,
            "score": self.score,
            "problems": list(self.problems),
            "warnings": list(self.warnings),
            "extracted_facts": dict(self.extracted_facts),
            "repair_hints": list(self.repair_hints),
            "matched_signals": list(self.matched_signals),
        }


@dataclass(slots=True)
class SemanticCheckReport:
    """Explainable SQL intent consistency report."""

    passed: bool
    decision: str
    score: float
    summary: str
    intent: str
    problems: list[SemanticCheckProblem] = field(default_factory=list)
    fix_suggestions: list[str] = field(default_factory=list)
    matched_signals: list[str] = field(default_factory=list)
    detected_tables: list[str] = field(default_factory=list)
    gate_reports: list[QualityGateReport] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "decision": self.decision,
            "score": self.score,
            "summary": self.summary,
            "intent": self.intent,
            "problems": [problem.to_dict() for problem in self.problems],
            "fix_suggestions": list(self.fix_suggestions),
            "matched_signals": list(self.matched_signals),
            "detected_tables": list(self.detected_tables),
            "gate_reports": [gate.to_dict() for gate in self.gate_reports],
        }


_TABLE_NAME_RE = re.compile(r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)`?", re.IGNORECASE)


def check_sql_semantics(
    *,
    query: str,
    sql: str,
    semantic_model: dict | None = None,
    relationships: Iterable[dict] | None = None,
    evidence: Iterable[str] | None = None,
    metric_registry: MetricRegistry | None = None,
    expected_output_columns: Iterable[str] | None = None,
    expected_output_schema: Iterable[dict] | None = None,
) -> SemanticCheckReport:
    """Return a deterministic semantic consistency report for a SQL draft."""

    normalized_query = _normalize_text(query)
    normalized_sql = _normalize_text(sql)
    evidence_items = [str(item) for item in (evidence or []) if str(item).strip()]
    evidence_text = "\n".join(evidence_items)
    business_entries = _business_evidence_entries(evidence_items)
    sql_shape = _try_extract_shape(sql)

    if not normalized_sql:
        problem = SemanticCheckProblem(
            code="EMPTY_SQL",
            title="SQL 为空",
            severity="high",
            message="SQL draft is empty.",
            why="没有可执行的 SQL，自然无法判断它是否对齐用户意图。",
            expected="提供一条可解析、可执行的只读 SQL 草稿。",
            actual="当前 SQL 为空。",
            repair_hint="先生成一条可解析的 SELECT / WITH SQL，再做语义一致性校验。",
        )
        return SemanticCheckReport(
            passed=False,
            decision="revise_sql",
            score=0.0,
            summary="SQL 为空，无法通过语义一致性校验。",
            intent="unknown",
            problems=[problem],
            fix_suggestions=[problem.repair_hint],
            detected_tables=[],
            gate_reports=[
                QualityGateReport(
                    name="sql.parse",
                    passed=False,
                    decision="revise_sql",
                    score=0.0,
                    problems=[problem.to_dict()],
                    repair_hints=[problem.repair_hint],
                )
            ],
        )

    detected_tables = _extract_tables(normalized_sql)
    intent = _detect_intent(normalized_query, evidence_text)
    registry = metric_registry or default_metric_registry()
    matched_metrics = registry.match_query(f"{normalized_query} {evidence_text}")
    matched_metric_ids = {metric.metric_id for metric in matched_metrics}
    output_alias_metrics = [
        metric
        for metric in (
            registry.match_output_aliases(_sql_output_aliases(sql_shape))
            if sql_shape
            else []
        )
        if metric.metric_id not in matched_metric_ids
    ]
    problems: list[SemanticCheckProblem] = []
    fix_suggestions: list[str] = []
    matched_signals: list[str] = []
    gate_reports: list[QualityGateReport] = []

    score = 0.6
    if sql_shape:
        detected_tables = sql_shape.tables or detected_tables
        gate_reports.append(
            QualityGateReport(
                name="sql.parse",
                passed=True,
                decision="continue",
                extracted_facts={
                    "dialect": sql_shape.dialect,
                    "normalized_sql": sql_shape.normalized_sql,
                },
            )
        )
        gate_reports.append(
            QualityGateReport(
                name="sql.ast_shape_extract",
                passed=True,
                decision="continue",
                extracted_facts=_shape_extracted_facts(sql_shape),
            )
        )
    else:
        problem = SemanticCheckProblem(
            code="SQL_PARSE_FAILED",
            title="SQL 解析失败",
            severity="high",
            message="SQL 无法解析为 AST，不能进入结构化语义校验。",
            why="SQL Quality Gate 依赖 AST 提取表、字段、JOIN 和聚合表达式；解析失败时继续执行风险不可控。",
            expected="提供一条可被 MySQL dialect 解析的 SELECT / WITH SQL。",
            actual="当前 SQL 无法被解析。",
            repair_hint="先修复 SQL 语法，再重新进入语义一致性校验。",
        )
        problems.append(problem)
        fix_suggestions.append(problem.repair_hint)
        score -= 0.4
        gate_reports.append(
            QualityGateReport(
                name="sql.parse",
                passed=False,
                decision="revise_sql",
                score=0.0,
                problems=[problem.to_dict()],
                repair_hints=[problem.repair_hint],
            )
        )

    fabricated_metric_gate_problems: list[SemanticCheckProblem] = []
    if sql_shape and output_alias_metrics:
        for metric in output_alias_metrics:
            metric_result = validate_metric_shape(metric, sql_shape)
            if metric_result.passed:
                continue
            problem = _fabricated_metric_problem(
                metric,
                evidence_text=evidence_text,
                problem_code=metric_result.problem_code,
            )
            fabricated_metric_gate_problems.append(problem)
            problems.append(problem)
            fix_suggestions.append(problem.repair_hint)
        if fabricated_metric_gate_problems:
            score -= 0.35
        gate_reports.append(
            QualityGateReport(
                name="sql.output_metric_contract_validate",
                passed=not fabricated_metric_gate_problems,
                decision="continue" if not fabricated_metric_gate_problems else "revise_sql",
                score=1.0 if not fabricated_metric_gate_problems else 0.0,
                problems=[problem.to_dict() for problem in fabricated_metric_gate_problems],
                extracted_facts={
                    "output_aliases": _sql_output_aliases(sql_shape),
                    "matched_output_metrics": [metric.metric_id for metric in output_alias_metrics],
                },
                repair_hints=[problem.repair_hint for problem in fabricated_metric_gate_problems],
            )
        )
    else:
        gate_reports.append(
            QualityGateReport(
                name="sql.output_metric_contract_validate",
                passed=True,
                decision="skipped",
                extracted_facts={"reason": "no_governed_output_metric_alias"},
            )
        )

    output_contract_problems: list[SemanticCheckProblem] = []
    expected_schema = _normalize_expected_output_schema(expected_output_schema)
    expected_columns = [
        str(field.get("column") or "").strip()
        for field in expected_schema
        if str(field.get("column") or "").strip()
    ]
    expected_columns.extend([
        str(column).strip()
        for column in (expected_output_columns or [])
        if str(column).strip()
    ])
    expected_columns = _dedupe(expected_columns)
    if sql_shape and expected_columns:
        output_aliases = _sql_output_aliases(sql_shape)
        normalized_aliases = {_normalize_output_column(alias) for alias in output_aliases}
        missing_columns = [
            column
            for column in expected_columns
            if _normalize_output_column(column) not in normalized_aliases
        ]
        if missing_columns:
            problem = _missing_output_schema_column_problem(missing_columns, output_aliases)
            output_contract_problems.append(problem)
            problems.append(problem)
            fix_suggestions.append(problem.repair_hint)
            score -= 0.25
        formula_contract_problems = _output_schema_formula_contract_problems(
            sql_shape=sql_shape,
            expected_schema=expected_schema,
            business_entries=business_entries,
            evidence_text=evidence_text,
        )
        if formula_contract_problems:
            output_contract_problems.extend(formula_contract_problems)
            problems.extend(formula_contract_problems)
            fix_suggestions.extend(problem.repair_hint for problem in formula_contract_problems)
            score -= 0.3
        gate_reports.append(
            QualityGateReport(
                name="sql.output_schema_validate",
                passed=not output_contract_problems,
                decision="continue" if not output_contract_problems else "revise_sql",
                score=1.0 if not output_contract_problems else 0.0,
                problems=[problem.to_dict() for problem in output_contract_problems],
                extracted_facts={
                    "expected_output_columns": expected_columns,
                    "expected_output_schema": expected_schema,
                    "output_aliases": output_aliases,
                },
                repair_hints=[problem.repair_hint for problem in output_contract_problems],
            )
        )
    else:
        gate_reports.append(
            QualityGateReport(
                name="sql.output_schema_validate",
                passed=True,
                decision="skipped",
                extracted_facts={"reason": "missing_sql_shape_or_expected_output_columns"},
            )
        )

    if sql_shape and semantic_model:
        schema_report = validate_sql_schema(sql_shape, semantic_model)
        for schema_problem in schema_report.problems:
            problem = _schema_problem_to_semantic_problem(schema_problem)
            problems.append(problem)
            fix_suggestions.append(problem.repair_hint)
        if not schema_report.passed:
            score -= 0.35
        gate_reports.append(
            QualityGateReport(
                name="sql.schema_validate",
                passed=schema_report.passed,
                decision="continue" if schema_report.passed else "revise_sql",
                score=1.0 if schema_report.passed else 0.0,
                problems=list(schema_report.problems),
                warnings=list(schema_report.warnings),
                repair_hints=[
                    _schema_problem_to_semantic_problem(schema_problem).repair_hint
                    for schema_problem in schema_report.problems
                ],
            )
        )
    else:
        gate_reports.append(
            QualityGateReport(
                name="sql.schema_validate",
                passed=True,
                decision="skipped",
                extracted_facts={"reason": "missing_sql_shape_or_semantic_model"},
            )
        )

    if matched_metrics:
        requires_loss_amount = _query_asks_loss_amount(normalized_query)
        metric_validation_passed = False
        metric_gate_signals: list[str] = []
        metric_gate_problems: list[SemanticCheckProblem] = []
        for metric in matched_metrics:
            metric_result = validate_metric_shape(metric, sql_shape) if sql_shape else None
            if metric_result and metric_result.passed:
                metric_validation_passed = True
                metric_gate_signals.extend(metric_result.matched_signals)
                matched_signals.extend(metric_result.matched_signals)
            elif metric_result and metric_result.problem_code == "REVERSED_METRIC_EXPRESSION":
                problem = _metric_expression_problem(
                    metric,
                    requires_loss_amount=requires_loss_amount,
                    evidence_text=evidence_text,
                    problem_code=metric_result.problem_code,
                )
                metric_gate_problems.append(problem)
        if metric_validation_passed:
            score += 0.28
            if any(metric.metric_id == "net_profit" for metric in matched_metrics):
                matched_signals.append("profit_loss_formula")
        else:
            problem = metric_gate_problems[0] if metric_gate_problems else _metric_expression_problem(
                matched_metrics[0],
                requires_loss_amount=requires_loss_amount,
                evidence_text=evidence_text,
            )
            problems.append(problem)
            if problem not in metric_gate_problems:
                metric_gate_problems.append(problem)
            fix_suggestions.append(problem.repair_hint)
            score -= 0.28

        gate_reports.append(
            QualityGateReport(
                name="sql.semantic_metric_validate",
                passed=metric_validation_passed,
                decision="continue" if metric_validation_passed else "revise_sql",
                score=1.0 if metric_validation_passed else 0.0,
                problems=[problem.to_dict() for problem in metric_gate_problems],
                extracted_facts={"matched_metrics": [metric.metric_id for metric in matched_metrics]},
                repair_hints=[problem.repair_hint for problem in metric_gate_problems],
                matched_signals=_dedupe(metric_gate_signals),
            )
        )

        if requires_loss_amount and not _has_loss_amount_formula(normalized_sql, sql_shape=sql_shape):
            problem = SemanticCheckProblem(
                code="MISSING_LOSS_AMOUNT_FORMULA",
                title="缺少亏损金额分支",
                severity="high",
                message="用户询问亏损金额，但 SQL 没有看到净利润为负时才取 ABS、否则为 0 的亏损金额分支。",
                why="ABS(SUM(...)) 在盈利时也会返回正数，不等价于亏损金额。",
                expected="CASE WHEN net_profit < 0 THEN ABS(net_profit) ELSE 0 END AS 亏损金额。",
                actual="当前 SQL 没有看到严格的亏损金额条件分支。",
                evidence=evidence_text,
                repair_hint="先聚合净利润，再在外层用 CASE WHEN net_profit < 0 THEN ABS(net_profit) ELSE 0 END。",
            )
            problems.append(problem)
            fix_suggestions.append(problem.repair_hint)
            score -= 0.28

        if _has_positive_negative_zero_split(normalized_sql) or _has_profit_status_case(sql_shape):
            score += 0.1
            matched_signals.append("loss_zero_boundary")
        if _has_profit_alias(normalized_sql):
            score += 0.05
            matched_signals.append("profit_alias")

    else:
        gate_reports.append(
            QualityGateReport(
                name="sql.semantic_metric_validate",
                passed=True,
                decision="skipped",
                extracted_facts={"reason": "no_metric_matched"},
            )
        )

        if intent == "budget":
            needs_budget_vs_actual = _query_asks_budget_vs_actual(normalized_query)
            if _has_budget_fields(normalized_sql) and (
                not needs_budget_vs_actual or _has_budget_actual_fields(normalized_sql)
            ):
                score += 0.22
                matched_signals.append("budget_fields")
            else:
                problem = SemanticCheckProblem(
                    code="MISSING_BUDGET_FORMULA",
                    title="缺少预算/实际口径",
                    severity="high",
                    message="查询意图是预算相关，但 SQL 没有看到足够的预算口径。",
                    why="预算差异或执行率类问题需要同时区分预算值和实际值；单独查询预算时至少需要预算字段。",
                    expected=(
                        "SQL 应覆盖 budget_amount 与 actual_amount，或等价的预算/实际字段口径。"
                        if needs_budget_vs_actual
                        else "SQL 应覆盖 budget/budget_amount 等预算字段。"
                    ),
                    actual="当前 SQL 没有看到预算/实际口径。",
                    evidence=evidence_text,
                    repair_hint="把预算金额和实际金额一起纳入 SQL，再计算差异或执行率。",
                )
                problems.append(problem)
                fix_suggestions.append(problem.repair_hint)
                score -= 0.28

        elif intent == "receivable":
            if _has_receivable_fields(normalized_sql):
                score += 0.22
                matched_signals.append("receivable_fields")
            else:
                problem = SemanticCheckProblem(
                    code="MISSING_RECEIVABLE_FORMULA",
                    title="缺少回款/应收口径",
                    severity="high",
                    message="查询意图是回款或应收，但 SQL 没有看到已结/应收金额口径。",
                    why="回款类问题不能只看订单总额，必须能体现已结金额、原始金额或对应的应收应付状态。",
                    expected="SQL 应覆盖 settled_amount / original_amount / receivable 相关口径。",
                    actual="当前 SQL 没有看到回款或应收口径。",
                    evidence=evidence_text,
                    repair_hint="把已结金额和原始金额纳入 SQL，再判断回款效率或结清情况。",
                )
                problems.append(problem)
                fix_suggestions.append(problem.repair_hint)
                score -= 0.28

        elif intent == "expense":
            if _has_expense_fields(normalized_sql):
                score += 0.18
                matched_signals.append("expense_fields")
            else:
                score -= 0.12
                fix_suggestions.append("把费用报销或费用金额口径纳入 SQL。")

        elif intent == "revenue":
            if _has_revenue_fields(normalized_sql):
                score += 0.18
                matched_signals.append("revenue_fields")
            else:
                score -= 0.12
                fix_suggestions.append("把收入口径纳入 SQL，例如收入金额、贷方收入或营收字段。")

    if sql_shape and relationships:
        relationship_report = validate_sql_relationships(sql_shape, list(relationships))
        for relationship_problem in relationship_report.problems:
            problem = _schema_problem_to_semantic_problem(relationship_problem)
            problems.append(problem)
            fix_suggestions.append(problem.repair_hint)
        if not relationship_report.passed:
            score -= 0.35
        gate_reports.append(
            QualityGateReport(
                name="sql.relationship_validate",
                passed=relationship_report.passed,
                decision="continue" if relationship_report.passed else "revise_sql",
                score=1.0 if relationship_report.passed else 0.0,
                problems=list(relationship_report.problems),
                warnings=list(relationship_report.warnings),
                repair_hints=[
                    _schema_problem_to_semantic_problem(relationship_problem).repair_hint
                    for relationship_problem in relationship_report.problems
                ],
            )
        )
    else:
        gate_reports.append(
            QualityGateReport(
                name="sql.relationship_validate",
                passed=True,
                decision="skipped",
                extracted_facts={"reason": "missing_sql_shape_or_relationships"},
            )
        )

    if _query_requires_time_scope(normalized_query) and not _has_time_scope(normalized_sql):
        score -= 0.08
        fix_suggestions.append("补上时间范围过滤，避免把去年/本月/当前口径答偏。")
        matched_signals.append("time_scope_warning")

    if _query_suggests_grouping(normalized_query) and not _has_group_by(normalized_sql):
        score -= 0.06
        fix_suggestions.append("如果问题按部门/按月/按项目聚合，请补上 GROUP BY 或等价分组逻辑。")
        matched_signals.append("grouping_warning")

    if len(detected_tables) > 1 and " join " not in normalized_sql and " from (" not in normalized_sql:
        score -= 0.05
        fix_suggestions.append("多表查询应显式写出 JOIN 关系，避免依赖逗号笛卡尔或隐式拼接。")
        matched_signals.append("join_visibility_warning")

    score = max(0.0, min(1.0, score))
    has_blocking_problem = any(problem.severity == "high" for problem in problems)
    requires_strict_score = intent in {"profit_loss", "budget", "receivable"}
    passed = not has_blocking_problem and (score >= 0.8 if requires_strict_score else True)
    if passed:
        score = max(score, 0.82 if intent in {"profit_loss", "budget", "receivable"} else score)

    summary = (
        "SQL 通过了当前规则版语义一致性校验。"
        if passed
        else f"检测到 {len(problems)} 个可能影响答案正确性的语义一致性问题。"
    )
    decision = "safe_to_execute" if passed else "revise_sql"

    return SemanticCheckReport(
        passed=passed,
        decision=decision,
        score=score,
        summary=summary,
        intent=intent,
        problems=problems,
        fix_suggestions=_dedupe(fix_suggestions) or (
            ["当前规则校验通过；如要进一步提高置信度，可继续核对执行结果与最终答案解释。"]
            if passed
            else []
        ),
        matched_signals=_dedupe(matched_signals),
        detected_tables=detected_tables,
        gate_reports=gate_reports,
    )


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _extract_tables(sql: str) -> list[str]:
    tables: list[str] = []
    seen: set[str] = set()
    for table in _TABLE_NAME_RE.findall(sql or ""):
        if table not in seen:
            seen.add(table)
            tables.append(table)
    return tables


def _try_extract_shape(sql: str) -> SqlShape | None:
    try:
        return extract_sql_shape(sql, dialect="mysql")
    except SqlParseError:
        return None


def _shape_extracted_facts(shape: SqlShape) -> dict:
    return {
        "tables": list(shape.tables),
        "aliases": dict(shape.table_aliases),
        "columns": [
            {
                "table_alias": column.table_alias,
                "name": column.name,
                "resolved_table": column.resolved_table,
            }
            for column in shape.columns
        ],
        "joins": [
            {
                "left_table_alias": join.left_table_alias,
                "left_column": join.left_column,
                "right_table_alias": join.right_table_alias,
                "right_column": join.right_column,
                "sql": join.sql,
            }
            for join in shape.joins
        ],
        "filters": [
            {
                "sql": filter_.sql,
                "column_refs": list(filter_.column_refs),
            }
            for filter_ in shape.filters
        ],
        "aggregations": [
            {
                "function": aggregation.function,
                "sql": aggregation.sql,
                "column_refs": list(aggregation.column_refs),
                "alias": aggregation.alias,
            }
            for aggregation in shape.aggregations
        ],
        "select_items": [
            {
                "sql": item.sql,
                "alias": item.alias,
                "column_refs": list(item.column_refs),
                "is_constant": item.is_constant,
            }
            for item in shape.select_items
        ],
        "case_expressions": list(shape.case_expressions),
        "group_by": list(shape.group_by),
        "having": list(shape.having),
        "order_by": list(shape.order_by),
        "limit": shape.limit,
    }


def _sql_output_aliases(shape: SqlShape | None) -> list[str]:
    if not shape:
        return []
    aliases: list[str] = []
    for item in shape.select_items:
        alias = str(getattr(item, "alias", "") or "").strip()
        if alias and alias not in aliases:
            aliases.append(alias)
    for aggregation in shape.aggregations:
        alias = str(getattr(aggregation, "alias", "") or "").strip()
        if alias and alias not in aliases:
            aliases.append(alias)
    return aliases


def _normalize_output_column(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "").lower())


def _normalize_expected_output_schema(value: Iterable[dict] | None) -> list[dict]:
    fields: list[dict] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        label = str(item.get("label") or "").strip()
        column = str(item.get("column") or "").strip()
        value_type = str(item.get("type") or "").strip().lower()
        if not (role and label and column and value_type):
            continue
        fields.append({"role": role, "label": label, "column": column, "type": value_type})
    return fields


def _business_evidence_entries(evidence: Iterable[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in evidence:
        entry: dict[str, Any] = {"term": "", "formula": "", "synonyms": [], "related_tables": []}
        for raw_line in str(item or "").splitlines():
            key, value = _split_evidence_line(raw_line)
            if not key:
                continue
            if key in {"术语", "term"}:
                entry["term"] = value
            elif key in {"公式", "formula", "定义", "definition"}:
                entry["formula"] = value
            elif key in {"同义词", "synonyms"}:
                entry["synonyms"] = _split_terms(value)
            elif key in {"关联表", "related_tables", "tables"}:
                entry["related_tables"] = _split_terms(value)
        if entry.get("term"):
            entries.append(entry)
    return entries


def _split_evidence_line(line: str) -> tuple[str, str]:
    text = str(line or "").strip()
    for sep in (":", "："):
        if sep in text:
            left, right = text.split(sep, 1)
            return left.strip().lower(), right.strip()
    return "", ""


def _split_terms(value: str) -> list[str]:
    return [
        term.strip()
        for term in (
            str(value or "")
            .replace("，", ",")
            .replace("；", ",")
            .replace(";", ",")
            .replace("、", ",")
            .split(",")
        )
        if term.strip()
    ]


def _output_schema_formula_contract_problems(
    *,
    sql_shape: SqlShape,
    expected_schema: list[dict],
    business_entries: list[dict[str, Any]],
    evidence_text: str,
) -> list[SemanticCheckProblem]:
    problems: list[SemanticCheckProblem] = []
    for field in expected_schema:
        if str(field.get("type") or "").strip().lower() not in {"percent", "ratio", "rate"}:
            continue
        column = str(field.get("column") or "").strip()
        label = str(field.get("label") or "").strip()
        entry = _business_entry_for_output_field(field, business_entries)
        denominator_terms = _ratio_denominator_terms(str(entry.get("formula") or "")) if entry else []
        if not denominator_terms:
            continue
        select_item = _select_item_for_alias(sql_shape, column)
        if select_item is None:
            continue
        denominator_sql = _ratio_denominator_sql(select_item.sql)
        if not denominator_sql:
            continue
        if not (
            _denominator_matches_any_term(denominator_sql, denominator_terms, business_entries)
            or _denominator_matches_output_alias(denominator_sql, denominator_terms, business_entries, sql_shape)
        ):
            expected = "、".join(denominator_terms)
            problems.append(
                SemanticCheckProblem(
                    code="INVALID_RATIO_DENOMINATOR",
                    title=f"比例指标分母不符合业务公式: {label or column}",
                    severity="high",
                    message=f"SQL 输出比例列 {label or column}，但分母没有对齐业务知识公式中的 {expected}。",
                    why="比例类指标不能只凭输出别名通过校验，分子和分母都必须来自业务知识声明的口径。",
                    expected=f"{label or column} 的分母应按业务公式使用 {expected} 口径。",
                    actual=f"当前分母表达式：{denominator_sql}",
                    evidence=evidence_text,
                    repair_hint=f"按业务知识公式重写 {label or column}：先计算分子，再用 {expected} 作为分母；不要使用无关科目或空分母过滤。",
                )
            )
    for field in expected_schema:
        if str(field.get("type") or "").strip().lower() not in {"amount", "number", "currency"}:
            continue
        column = str(field.get("column") or "").strip()
        label = str(field.get("label") or "").strip()
        entry = _business_entry_for_output_field(field, business_entries)
        required_literals = _formula_required_literals(str(entry.get("formula") or "")) if entry else []
        if not required_literals:
            continue
        select_item = _select_item_for_alias(sql_shape, column)
        if select_item is None:
            continue
        expression_norm = _normalize_output_column(select_item.sql)
        if not any(_normalize_output_column(literal) in expression_norm for literal in required_literals):
            expected = "、".join(required_literals)
            problems.append(
                SemanticCheckProblem(
                    code="MISSING_FORMULA_FILTER",
                    title=f"输出指标缺少业务公式过滤条件: {label or column}",
                    severity="high",
                    message=f"SQL 输出金额列 {label or column}，但表达式没有使用业务知识公式中的关键过滤值：{expected}。",
                    why="金额类指标不能只凭列名通过校验；当业务知识声明了明确过滤条件时，SQL 必须把该口径落到对应 SELECT 表达式里。",
                    expected=f"{label or column} 的 SELECT 表达式应包含业务公式过滤值：{expected}。",
                    actual=f"当前表达式：{select_item.sql}",
                    evidence=evidence_text,
                    repair_hint=f"按业务知识公式重写 {label or column}，在该列表达式中使用 {expected} 对应的过滤口径。",
                )
            )
    return problems


def _business_entry_for_output_field(field: dict, business_entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    field_terms = [
        str(field.get("label") or ""),
        str(field.get("column") or ""),
        str(field.get("role") or ""),
    ]
    normalized_field_terms = {_normalize_output_column(term) for term in field_terms if str(term).strip()}
    best: tuple[int, dict[str, Any]] | None = None
    for entry in business_entries:
        aliases = [
            str(entry.get("term") or ""),
            *[str(item) for item in entry.get("synonyms", []) or []],
        ]
        score = 0
        for alias in aliases:
            alias_norm = _normalize_output_column(alias)
            if not alias_norm:
                continue
            if alias_norm in normalized_field_terms:
                score = max(score, 100 + len(alias_norm))
                continue
            for field_norm in normalized_field_terms:
                if alias_norm in field_norm or field_norm in alias_norm:
                    score = max(score, min(len(alias_norm), len(field_norm)))
        if score and (best is None or score > best[0]):
            best = (score, entry)
    return best[1] if best else None


def _ratio_denominator_terms(formula: str) -> list[str]:
    text = str(formula or "")
    if "/" not in text:
        return []
    denominator = text.split("/", 1)[1]
    denominator = re.split(r"[*%×xX；;，,。)\n]", denominator, maxsplit=1)[0]
    return [
        token
        for token in _formula_tokens(denominator)
        if not _is_formula_noise_token(token)
    ]


def _formula_tokens(value: str) -> list[str]:
    text = str(value or "")
    for symbol in ("(", ")", "（", "）", "+", "-", "*", "/", "=", ">", "<", "%", "，", "；", ";", ","):
        text = text.replace(symbol, " ")
    return [item.strip() for item in text.split() if item.strip()]


def _is_formula_noise_token(token: str) -> bool:
    normalized = _normalize_output_column(token)
    return not normalized or normalized.isdigit()


def _select_item_for_alias(sql_shape: SqlShape, alias: str):
    target = _normalize_output_column(alias)
    for item in sql_shape.select_items:
        if _normalize_output_column(getattr(item, "alias", "") or "") == target:
            return item
    return None


def _formula_required_literals(formula: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"'([^']+)'|\"([^\"]+)\"", str(formula or "")):
        value = (match.group(1) or match.group(2) or "").strip()
        if value:
            values.append(value)
    return _dedupe(values)


def _ratio_denominator_sql(select_sql: str) -> str:
    text = str(select_sql or "")
    slash_index = _operator_index(text, "/")
    if slash_index < 0:
        return ""
    after = text[slash_index + 1 :]
    end = _ratio_denominator_end(after)
    return after[:end].strip()


def _operator_index(text: str, operator: str) -> int:
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == operator:
            return index
    return -1


def _ratio_denominator_end(text: str) -> int:
    quote = ""
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")":
            if depth == 0:
                return index
            depth -= 1
            continue
        if depth == 0 and char in {"*", "+", "-"}:
            return index
    return len(text)


def _denominator_matches_any_term(
    denominator_sql: str,
    denominator_terms: list[str],
    business_entries: list[dict[str, Any]],
) -> bool:
    denominator_norm = _normalize_output_column(denominator_sql)
    if not denominator_norm:
        return False
    for term in denominator_terms:
        candidates = _term_candidate_tokens(term, business_entries)
        if any(candidate and candidate in denominator_norm for candidate in candidates):
            return True
    return False


def _denominator_matches_output_alias(
    denominator_sql: str,
    denominator_terms: list[str],
    business_entries: list[dict[str, Any]],
    sql_shape: SqlShape,
) -> bool:
    denominator_norm = _normalize_sql_expression(denominator_sql)
    if not denominator_norm:
        return False
    accepted_aliases = {
        candidate
        for term in denominator_terms
        for candidate in _term_candidate_tokens(term, business_entries)
        if candidate
    }
    for item in sql_shape.select_items:
        alias_norm = _normalize_output_column(getattr(item, "alias", "") or "")
        if alias_norm not in accepted_aliases:
            continue
        expression_norm = _normalize_sql_expression(_select_expression_without_alias(item.sql))
        if expression_norm and expression_norm == denominator_norm:
            return True
    return False


def _select_expression_without_alias(sql: str) -> str:
    return re.split(r"\s+AS\s+", str(sql or ""), maxsplit=1, flags=re.IGNORECASE)[0].strip()


def _normalize_sql_expression(sql: str) -> str:
    return re.sub(r"[\s`]+", "", str(sql or "").lower())


def _term_candidate_tokens(term: str, business_entries: list[dict[str, Any]]) -> list[str]:
    term_norm = _normalize_output_column(term)
    candidates = [term_norm] if term_norm else []
    for entry in business_entries:
        aliases = [
            str(entry.get("term") or ""),
            *[str(item) for item in entry.get("synonyms", []) or []],
        ]
        if not any(_normalize_output_column(alias) == term_norm for alias in aliases):
            continue
        alias_norms = [_normalize_output_column(alias) for alias in aliases if _normalize_output_column(alias)]
        for token in _formula_tokens(str(entry.get("formula") or "")):
            token_norm = _normalize_output_column(token)
            if not token_norm:
                continue
            if any(alias_norm in token_norm or token_norm in alias_norm for alias_norm in alias_norms):
                candidates.append(token_norm)
                continue
            if any(ch.isdigit() for ch in token_norm):
                candidates.append(token_norm)
    return _dedupe([candidate for candidate in candidates if candidate])


def _missing_output_schema_column_problem(
    missing_columns: list[str],
    output_aliases: list[str],
) -> SemanticCheckProblem:
    missing = "、".join(missing_columns)
    actual = "、".join(output_aliases) if output_aliases else "无输出别名"
    return SemanticCheckProblem(
        code="MISSING_OUTPUT_SCHEMA_COLUMN",
        title="SQL 缺少计划声明的输出列",
        severity="high",
        message=f"SQL 没有输出当前步骤 output_schema 要求的列：{missing}。",
        why="复杂计划的每个 SQL step 必须履行自己的输出契约，否则最终展示会把缺列渲染成无数据，掩盖 SQL 没有计算该指标。",
        expected=f"SELECT 中应使用 output_schema.column 作为稳定别名输出：{missing}。",
        actual=f"当前 SQL 输出别名：{actual}。",
        repair_hint=f"按当前步骤 output_schema 补齐 SELECT 别名：{missing}；无法计算的指标应从该步骤 output_schema 中移除。",
    )


def _fabricated_metric_problem(
    metric,
    *,
    evidence_text: str,
    problem_code: str = "MISSING_METRIC_EXPRESSION",
) -> SemanticCheckProblem:
    if problem_code == "REVERSED_METRIC_EXPRESSION":
        return _metric_expression_problem(
            metric,
            requires_loss_amount=False,
            evidence_text=evidence_text,
            problem_code=problem_code,
        )
    return SemanticCheckProblem(
        code="FABRICATED_BUSINESS_METRIC",
        title=f"输出指标未满足治理公式: {metric.metric_id}",
        severity="high",
        message="SQL 输出了受治理指标别名，但表达式没有满足该指标定义。",
        why="SQL step 不能用常量、单边金额或其他业务表金额伪造受治理指标，否则最终展示会把无关口径当成业务结论。",
        expected=f"输出 {metric.metric_id} 时必须按 MetricDefinition 的受治理表达式计算；否则不要输出该指标别名。",
        actual="当前 SQL 的输出别名命中了受治理指标，但 AST 中没有匹配到对应指标表达式。",
        evidence=evidence_text,
        repair_hint="删除当前步骤无法计算的指标列，或补齐支撑该指标的表、字段、JOIN 和公式。",
    )


def _metric_expression_problem(
    metric,
    *,
    requires_loss_amount: bool,
    evidence_text: str,
    problem_code: str = "MISSING_METRIC_EXPRESSION",
) -> SemanticCheckProblem:
    if problem_code == "REVERSED_METRIC_EXPRESSION":
        return SemanticCheckProblem(
            code="REVERSED_METRIC_EXPRESSION",
            title=f"指标表达式方向不一致: {metric.metric_id}",
            severity="high",
            message="SQL 在同一个指标聚合中同时出现正向差额和反向差额。",
            why="方向型指标不能把 left-right 和 right-left 分支相加，否则成本或扣减项可能被算成正向贡献。",
            expected="同一个指标聚合应保持受治理公式方向一致。",
            actual="当前 SQL 在同一聚合表达式里混用了左右字段的反向相减。",
            evidence=evidence_text,
            repair_hint="不要把同一指标的左右字段反向相减后再加回；按治理公式保持统一方向，扣减项应使用负号或统一的差额表达式。",
        )

    if metric.metric_id == "net_profit":
        return SemanticCheckProblem(
            code="MISSING_PROFIT_LOSS_FORMULA",
            title="缺少净利润/亏损金额公式",
            severity="high",
            message="查询意图是亏损或利润，但 SQL 没有看到明确的净利润/亏损金额计算。",
            why="亏损类问题不能只挑收入或成本单边字段，必须先算净利润，再做正负判断或亏损金额转换。",
            expected=(
                "先计算净利润，再用 CASE WHEN net_profit < 0 THEN ABS(net_profit) ELSE 0 END 之类的方式输出亏损金额。"
                if requires_loss_amount
                else "SQL 应明确计算净利润，例如 SUM(credit_amount - debit_amount) AS net_profit。"
            ),
            actual="当前 SQL 没有看到明确的净利润/亏损金额公式。",
            evidence=evidence_text,
            repair_hint="补上净利润公式和亏损金额分支，不要只选单边金额字段。",
        )

    return SemanticCheckProblem(
        code="MISSING_METRIC_EXPRESSION",
        title=f"缺少指标表达式: {metric.metric_id}",
        severity="high",
        message=f"查询意图匹配到指标 {metric.metric_id}，但 SQL 没有覆盖该指标定义的表达式。",
        why="指标类问题必须按 MetricDefinition 中的受治理表达式计算，不能只选单边字段或临时别名。",
        expected=f"SQL 应覆盖指标 {metric.metric_id} 的表达式定义。",
        actual="当前 SQL 没有看到完整指标表达式。",
        evidence=evidence_text,
        repair_hint="按 MetricDefinition 补齐聚合函数、参与字段和运算符。",
    )


def _schema_problem_to_semantic_problem(problem: dict) -> SemanticCheckProblem:
    code = str(problem.get("code") or "SCHEMA_VALIDATION_FAILED")
    table = str(problem.get("table") or "")
    column = str(problem.get("column") or "")
    table_alias = str(problem.get("table_alias") or "")
    actual = f"{table}.{column}" if table and column else table
    title = "SQL 引用了不存在的表或字段"
    why = "SQL 引用的物理表或字段必须存在于语义模型中，否则执行或结果口径不可靠。"
    expected = "只引用 semantic_model 中存在的表和字段。"
    repair_hint = "根据 schema/semantic_model 修正表名、字段名或别名映射。"
    if code == "UNKNOWN_TABLE_ALIAS":
        actual = f"{table_alias}.{column}" if table_alias and column else table_alias
        title = "SQL 引用了未声明的表别名"
        why = "字段前缀必须来自 FROM/JOIN 中声明过的表或别名，否则 SQL 执行会失败或口径无法校验。"
        expected = "先在 FROM/JOIN 中声明对应表别名，再引用该别名下的字段。"
        repair_hint = "补齐缺失的 JOIN，或把字段前缀改成已声明的表别名。"
    if code == "UNKNOWN_JOIN_RELATIONSHIP":
        title = "SQL JOIN 关系未被语义模型覆盖"
        why = "JOIN 必须来自已知关系，否则会引入错误关联或笛卡尔放大。"
        expected = "只使用已知 table_relationships 中的 JOIN 路径。"
        repair_hint = "根据 table_relationships 修正 JOIN 条件或补充桥表。"
    return SemanticCheckProblem(
        code=code,
        title=title,
        severity=str(problem.get("severity") or "high"),
        message=str(problem.get("message") or "SQL schema validation failed."),
        why=why,
        expected=expected,
        actual=actual,
        repair_hint=repair_hint,
    )


def _detect_intent(query: str, evidence_text: str = "") -> str:
    text = f"{query} {evidence_text}"
    if any(term in text for term in ("亏损", "利润", "净利", "盈利")):
        return "profit_loss"
    if any(term in text for term in ("预算", "执行率", "偏差", "差异", "超支")):
        return "budget"
    if any(term in text for term in ("回款", "应收", "收款", "核销", "逾期")):
        return "receivable"
    if any(term in text for term in ("费用", "报销", "开销")):
        return "expense"
    if any(term in text for term in ("收入", "营收", "销售额")):
        return "revenue"
    return "generic"


def _query_asks_loss_amount(query: str) -> bool:
    return "亏损" in query and any(term in query for term in ("多少", "金额", "数额", "额度"))


def _has_loss_amount_formula(sql: str, sql_shape: SqlShape | None = None) -> bool:
    return bool(
        re.search(r"case\s+when.+?<\s*0.+?then\s+abs\s*\(", sql, re.IGNORECASE | re.DOTALL)
        and re.search(r"else\s+0", sql, re.IGNORECASE | re.DOTALL)
    ) or bool(
        sql_shape
        and any("case" in case_sql.lower() and "abs(" in case_sql.lower() for case_sql in sql_shape.case_expressions)
    )


def _has_positive_negative_zero_split(sql: str) -> bool:
    return bool(re.search(r"case\s+when.+?<\s*0.+?then.+?else\s+0", sql, re.IGNORECASE | re.DOTALL))


def _has_profit_status_case(sql_shape: SqlShape | None) -> bool:
    if not sql_shape:
        return False
    return any("< 0" in case_sql and "> 0" in case_sql for case_sql in sql_shape.case_expressions)


def _has_profit_alias(sql: str) -> bool:
    return bool(re.search(r"\b(net_profit|profit|loss_amount|亏损金额)\b", sql, re.IGNORECASE))


def _has_budget_fields(sql: str) -> bool:
    return any(token in sql for token in ("budget", "budget_amount", "预算", "执行率"))


def _has_budget_actual_fields(sql: str) -> bool:
    return ("budget" in sql or "budget_amount" in sql) and ("actual" in sql or "actual_amount" in sql)


def _query_asks_budget_vs_actual(query: str) -> bool:
    return any(term in query for term in ("差异", "偏差", "执行率", "实际", "超支", "对比"))


def _has_receivable_fields(sql: str) -> bool:
    return any(token in sql for token in ("settled_amount", "original_amount", "receivable", "payable", "回款"))


def _has_expense_fields(sql: str) -> bool:
    return any(token in sql for token in ("expense", "expense_claim", "total_amount", "approved_amount", "报销"))


def _has_revenue_fields(sql: str) -> bool:
    return any(token in sql for token in ("revenue", "income", "营收", "收入")) or "credit_amount" in sql


def _query_requires_time_scope(query: str) -> bool:
    return any(term in query for term in ("去年", "今年", "本月", "本年", "当前", "当月", "上月"))


def _has_time_scope(sql: str) -> bool:
    return bool(
        re.search(r"\b(where|having)\b", sql, re.IGNORECASE)
        and re.search(r"\b(period|date|created_at|entry_date|claim_date|due_date|month|year)\b", sql, re.IGNORECASE)
    )


def _query_suggests_grouping(query: str) -> bool:
    return any(term in query for term in ("按", "各", "每个", "每月", "每个部门", "部门", "月份", "项目"))


def _has_group_by(sql: str) -> bool:
    return bool(re.search(r"\bgroup\s+by\b", sql, re.IGNORECASE))


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
