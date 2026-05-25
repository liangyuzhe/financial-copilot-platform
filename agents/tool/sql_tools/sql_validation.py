"""Validation gates over extracted SQL shape."""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.tool.sql_tools.sql_shape import SqlShape


@dataclass(frozen=True, slots=True)
class SqlValidationReport:
    passed: bool
    problems: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "problems": list(self.problems),
            "warnings": list(self.warnings),
        }


def validate_sql_relationships(shape: SqlShape, relationships: list[dict]) -> SqlValidationReport:
    """Validate JOIN clauses against known table relationships."""
    normalized_relationships = {
        (
            rel.get("from_table"),
            rel.get("from_column"),
            rel.get("to_table"),
            rel.get("to_column"),
        )
        for rel in relationships or []
    }
    problems: list[dict] = []

    for join in shape.joins:
        left_table = shape.table_aliases.get(join.left_table_alias, join.left_table_alias)
        right_table = shape.table_aliases.get(join.right_table_alias, join.right_table_alias)
        candidate = (left_table, join.left_column, right_table, join.right_column)
        reverse_candidate = (right_table, join.right_column, left_table, join.left_column)
        if candidate in normalized_relationships or reverse_candidate in normalized_relationships:
            continue
        problems.append({
            "code": "UNKNOWN_JOIN_RELATIONSHIP",
            "severity": "high",
            "left_table": left_table,
            "left_column": join.left_column,
            "right_table": right_table,
            "right_column": join.right_column,
            "message": f"JOIN {join.sql} is not covered by known relationships.",
        })

    return SqlValidationReport(
        passed=not any(problem.get("severity") == "high" for problem in problems),
        problems=problems,
    )


def validate_sql_schema(shape: SqlShape, semantic_model: dict) -> SqlValidationReport:
    """Validate referenced tables and columns against semantic model metadata."""
    known_tables = set((semantic_model or {}).keys())
    problems: list[dict] = []

    for table in shape.tables:
        if table not in known_tables:
            problems.append({
                "code": "UNKNOWN_TABLE",
                "severity": "high",
                "table": table,
                "message": f"SQL references unknown table {table}.",
            })

    for column in shape.columns:
        if (
            column.table_alias
            and column.table_alias not in shape.table_aliases
            and column.table_alias not in known_tables
        ):
            problems.append({
                "code": "UNKNOWN_TABLE_ALIAS",
                "severity": "high",
                "table_alias": column.table_alias,
                "column": column.name,
                "message": f"SQL references unknown table alias {column.table_alias}.{column.name}.",
            })
            continue
        if not column.resolved_table:
            continue
        if column.resolved_table not in known_tables:
            continue
        known_columns = set((semantic_model.get(column.resolved_table) or {}).keys())
        if column.name not in known_columns:
            problems.append({
                "code": "UNKNOWN_COLUMN",
                "severity": "high",
                "table": column.resolved_table,
                "column": column.name,
                "message": f"SQL references unknown column {column.resolved_table}.{column.name}.",
            })

    return SqlValidationReport(
        passed=not any(problem.get("severity") == "high" for problem in problems),
        problems=problems,
    )
