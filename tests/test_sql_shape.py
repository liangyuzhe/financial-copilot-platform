"""Tests for SQL AST shape extraction."""

import pytest


PROFIT_LOSS_SQL = """
SELECT
CASE
WHEN SUM(ji.credit_amount - ji.debit_amount) < 0 THEN '去年亏损'
WHEN SUM(ji.credit_amount - ji.debit_amount) > 0 THEN '去年盈利'
ELSE '去年不盈不亏'
END AS 盈亏情况,
ABS(SUM(ji.credit_amount - ji.debit_amount)) AS 金额
FROM t_journal_item ji
JOIN t_journal_entry je ON ji.entry_id = je.id
JOIN t_account a ON ji.account_code = a.account_code
WHERE je.entry_date >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 YEAR), '%Y-01-01')
AND je.entry_date <= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 YEAR), '%Y-12-31')
AND a.account_type = '损益'
AND je.status = '已过账';
"""


def test_extract_sql_shape_resolves_tables_aliases_columns_and_joins():
    from agents.tool.sql_tools.sql_shape import extract_sql_shape

    shape = extract_sql_shape(PROFIT_LOSS_SQL, dialect="mysql")

    assert shape.tables == ["t_journal_item", "t_journal_entry", "t_account"]
    assert shape.table_aliases == {
        "ji": "t_journal_item",
        "je": "t_journal_entry",
        "a": "t_account",
    }
    assert ("ji", "credit_amount", "t_journal_item") in [
        (column.table_alias, column.name, column.resolved_table)
        for column in shape.columns
    ]
    assert ("ji", "debit_amount", "t_journal_item") in [
        (column.table_alias, column.name, column.resolved_table)
        for column in shape.columns
    ]
    assert {
        (join.left_table_alias, join.left_column, join.right_table_alias, join.right_column)
        for join in shape.joins
    } == {
        ("ji", "entry_id", "je", "id"),
        ("ji", "account_code", "a", "account_code"),
    }


def test_extract_sql_shape_captures_aggregations_cases_and_filters():
    from agents.tool.sql_tools.sql_shape import extract_sql_shape

    shape = extract_sql_shape(PROFIT_LOSS_SQL, dialect="mysql")

    assert any(
        aggregation.function == "SUM"
        and ("ji", "credit_amount") in aggregation.column_refs
        and ("ji", "debit_amount") in aggregation.column_refs
        for aggregation in shape.aggregations
    )
    assert len(shape.case_expressions) == 1
    assert any("account_type" in item.sql and "损益" in item.sql for item in shape.filters)
    assert any("status" in item.sql and "已过账" in item.sql for item in shape.filters)
    assert any("entry_date" in item.sql and ">=" in item.sql for item in shape.filters)
    assert any("entry_date" in item.sql and "<=" in item.sql for item in shape.filters)


def test_extract_sql_shape_raises_parse_error_for_invalid_sql():
    from agents.tool.sql_tools.sql_shape import SqlParseError, extract_sql_shape

    with pytest.raises(SqlParseError):
        extract_sql_shape("SELECT FROM", dialect="mysql")
