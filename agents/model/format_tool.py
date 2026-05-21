"""Structured-output tool using Pydantic and LangChain ``@tool`` decorator."""

from __future__ import annotations

import re

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field


class FormatOutput(BaseModel):
    """Schema for the format-response tool."""

    answer: str = Field(description="The direct answer to the question. When is_sql=true, include only the SQL query.")
    is_sql: bool = Field(description="Whether the answer is a SQL query")
    needs_more_tables: bool = Field(
        default=False,
        description="Set to true only if required physical tables are absent from the provided schemas.",
    )
    missing_tables: list[str] = Field(
        default_factory=list,
        description="Array of table names absent from the provided schemas. Do not include tables already shown in the prompt.",
    )

    @classmethod
    def json_schema(cls) -> dict:
        """Return a JSON-schema dict suitable for tool calling."""
        return cls.model_json_schema()


_SQL_CODE_FENCE_RE = re.compile(r"^```(?:sql|mysql)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_SENTINEL_RE = re.compile(r"</?text_never_used_[^>]+>", re.IGNORECASE)
_TRAILING_INCOMPLETE_RE = re.compile(
    r"\b(?:HAVIN|HAVING|WHERE|AND|OR|GROUP\s+BY|ORDER\s+BY|LIMIT|JOIN|ON|BETWEEN)\s*$",
    re.IGNORECASE,
)
_SIMPLE_CASE_BOOLEAN_WHEN_RE = re.compile(
    r"\bCASE\s+(?!WHEN\b)(?:(?!\bEND\b).)*\bWHEN\b(?:(?!\bTHEN\b).)*(?:=|<>|!=|>=|<=|>|<)\s*",
    re.IGNORECASE | re.DOTALL,
)


def normalize_sql_answer(answer: str) -> tuple[str, bool, str | None]:
    """Clean and validate SQL returned by an LLM.

    Returns ``(sql, is_valid, error)``. This is intentionally conservative:
    it removes transport/format artifacts, but it does not invent missing SQL.
    """
    sql = (answer or "").strip()
    sql = _SENTINEL_RE.sub("", sql)
    sql = _SQL_CODE_FENCE_RE.sub("", sql).strip()

    # Keep only the SQL if the model added prose before a SELECT/WITH query.
    match = re.search(r"\b(WITH|SELECT)\b", sql, flags=re.IGNORECASE)
    if match:
        sql = sql[match.start():].strip()

    sql = re.sub(r"[ \t]+", " ", sql)
    sql = re.sub(r"\s*\n\s*", "\n", sql).strip()

    if not sql:
        return "", False, "SQL 为空"

    if _TRAILING_INCOMPLETE_RE.search(sql):
        return sql, False, "SQL 末尾存在未完成的关键字或条件"

    if sql.count("(") != sql.count(")"):
        return sql, False, "SQL 括号不匹配"

    if not re.match(r"^\s*(WITH|SELECT)\b", sql, flags=re.IGNORECASE):
        return sql, False, "SQL 必须以 SELECT 或 WITH 开头"

    if _SIMPLE_CASE_BOOLEAN_WHEN_RE.search(sql):
        return sql, False, "SQL CASE 条件写法不规范；条件判断应使用 CASE WHEN ... THEN ..."

    sql = sql.rstrip(" ;") + ";"

    return sql, True, None


@tool
def sql_format_response(answer: str, is_sql: bool, needs_more_tables: bool = False, missing_tables: list[str] | None = None) -> dict:
    """Format the SQL output answer and indicate whether it is a SQL query.

    Args:
        answer: The direct answer to the question. If is_sql is true, this must contain only SQL.
        is_sql: Whether the answer is a SQL query.
        needs_more_tables: Set to true only if required physical tables are absent from the provided schemas.
        missing_tables: Array of table names absent from the provided schemas; do not include already-provided tables.
    """
    if is_sql:
        answer, ok, error = normalize_sql_answer(answer)
        if not ok:
            return {
                "answer": f"生成的 SQL 格式不完整或不规范: {error}\n{answer}",
                "is_sql": False,
                "needs_more_tables": needs_more_tables,
                "missing_tables": missing_tables or [],
            }

    return {
        "answer": answer,
        "is_sql": is_sql,
        "needs_more_tables": needs_more_tables,
        "missing_tables": missing_tables or [],
    }


def create_format_tool() -> BaseTool:
    """Create and return the SQL format-response tool."""
    return sql_format_response
