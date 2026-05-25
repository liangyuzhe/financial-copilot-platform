"""SQL AST shape extraction.

This module extracts structural facts from SQL. It does not decide whether the
SQL is semantically correct for a business question.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class SqlParseError(ValueError):
    """Raised when SQL cannot be parsed into an AST."""


@dataclass(frozen=True, slots=True)
class SqlColumnRef:
    name: str
    table_alias: str = ""
    resolved_table: str = ""


@dataclass(frozen=True, slots=True)
class SqlJoin:
    left_table_alias: str
    left_column: str
    right_table_alias: str
    right_column: str
    sql: str


@dataclass(frozen=True, slots=True)
class SqlFilter:
    sql: str
    column_refs: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SqlAggregation:
    function: str
    sql: str
    column_refs: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SqlShape:
    sql: str
    dialect: str
    normalized_sql: str
    tables: list[str]
    table_aliases: dict[str, str]
    columns: list[SqlColumnRef]
    joins: list[SqlJoin]
    filters: list[SqlFilter]
    aggregations: list[SqlAggregation]
    case_expressions: list[str]
    group_by: list[str]
    having: list[str]
    order_by: list[str]
    limit: str = ""


def extract_sql_shape(sql: str, dialect: str = "mysql") -> SqlShape:
    """Parse SQL and return structural facts extracted from the AST."""
    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.errors import ParseError
    except ImportError as exc:  # pragma: no cover - exercised only without deps
        raise SqlParseError("sqlglot is required for SQL AST parsing.") from exc

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except ParseError as exc:
        raise SqlParseError(str(exc)) from exc
    except Exception as exc:
        raise SqlParseError(str(exc)) from exc

    table_aliases: dict[str, str] = {}
    tables: list[str] = []
    seen_tables: set[str] = set()
    for table in tree.find_all(exp.Table):
        table_name = table.name
        alias = table.alias_or_name
        if table_name and table_name not in seen_tables:
            seen_tables.add(table_name)
            tables.append(table_name)
        if alias:
            table_aliases[alias] = table_name
        if table_name:
            table_aliases.setdefault(table_name, table_name)

    columns: list[SqlColumnRef] = []
    seen_columns: set[tuple[str, str, str]] = set()
    for column in tree.find_all(exp.Column):
        table_alias = column.table or ""
        resolved_table = table_aliases.get(table_alias, table_alias)
        key = (table_alias, column.name, resolved_table)
        if key in seen_columns:
            continue
        seen_columns.add(key)
        columns.append(
            SqlColumnRef(
                name=column.name,
                table_alias=table_alias,
                resolved_table=resolved_table,
            )
        )

    joins: list[SqlJoin] = []
    for join in tree.find_all(exp.Join):
        on_expr = join.args.get("on")
        if not isinstance(on_expr, exp.EQ):
            continue
        left = on_expr.this
        right = on_expr.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        joins.append(
            SqlJoin(
                left_table_alias=left.table or "",
                left_column=left.name,
                right_table_alias=right.table or "",
                right_column=right.name,
                sql=on_expr.sql(dialect=dialect),
            )
        )

    filters = _extract_filters(tree, dialect=dialect)
    aggregations = [
        SqlAggregation(
            function="SUM",
            sql=aggregation.sql(dialect=dialect),
            column_refs=_column_refs(aggregation),
        )
        for aggregation in tree.find_all(exp.Sum)
    ]
    case_expressions = [
        case.sql(dialect=dialect)
        for case in tree.find_all(exp.Case)
    ]

    return SqlShape(
        sql=sql,
        dialect=dialect,
        normalized_sql=tree.sql(dialect=dialect),
        tables=tables,
        table_aliases={
            alias: table
            for alias, table in table_aliases.items()
            if alias != table
        },
        columns=columns,
        joins=joins,
        filters=filters,
        aggregations=aggregations,
        case_expressions=case_expressions,
        group_by=_expression_list_sql(tree.args.get("group"), dialect=dialect),
        having=_single_expression_sql(tree.args.get("having"), dialect=dialect),
        order_by=_expression_list_sql(tree.args.get("order"), dialect=dialect),
        limit=(tree.args.get("limit").sql(dialect=dialect) if tree.args.get("limit") else ""),
    )


def _column_refs(expression) -> list[tuple[str, str]]:
    from sqlglot import exp

    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for column in expression.find_all(exp.Column):
        ref = (column.table or "", column.name)
        if ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
    return refs


def _extract_filters(tree, dialect: str) -> list[SqlFilter]:
    from sqlglot import exp

    where = tree.args.get("where")
    if where is None:
        return []
    filters: list[SqlFilter] = []
    for predicate in where.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In):
        filters.append(
            SqlFilter(
                sql=predicate.sql(dialect=dialect),
                column_refs=_column_refs(predicate),
            )
        )
    return filters


def _expression_list_sql(expression, dialect: str) -> list[str]:
    if expression is None:
        return []
    expressions = getattr(expression, "expressions", None) or []
    return [item.sql(dialect=dialect) for item in expressions]


def _single_expression_sql(expression, dialect: str) -> list[str]:
    if expression is None:
        return []
    return [expression.sql(dialect=dialect)]
