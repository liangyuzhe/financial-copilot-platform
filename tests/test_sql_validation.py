"""Tests for SQL schema and relationship validation gates."""


SEMANTIC_MODEL = {
    "t_journal_item": {
        "entry_id": {"column_name": "entry_id"},
        "account_code": {"column_name": "account_code"},
        "credit_amount": {"column_name": "credit_amount"},
        "debit_amount": {"column_name": "debit_amount"},
    },
    "t_journal_entry": {
        "id": {"column_name": "id"},
        "entry_date": {"column_name": "entry_date"},
        "status": {"column_name": "status"},
    },
    "t_account": {
        "account_code": {"column_name": "account_code"},
        "account_type": {"column_name": "account_type"},
    },
}


def test_schema_validation_accepts_known_alias_columns():
    from agents.tool.sql_tools.sql_shape import extract_sql_shape
    from agents.tool.sql_tools.sql_validation import validate_sql_schema

    shape = extract_sql_shape(
        "SELECT SUM(ji.credit_amount - ji.debit_amount) FROM t_journal_item ji",
        dialect="mysql",
    )

    report = validate_sql_schema(shape, SEMANTIC_MODEL)

    assert report.passed is True
    assert report.problems == []


def test_schema_validation_blocks_unknown_table():
    from agents.tool.sql_tools.sql_shape import extract_sql_shape
    from agents.tool.sql_tools.sql_validation import validate_sql_schema

    shape = extract_sql_shape("SELECT x.amount FROM t_missing x", dialect="mysql")

    report = validate_sql_schema(shape, SEMANTIC_MODEL)

    assert report.passed is False
    assert report.problems[0]["code"] == "UNKNOWN_TABLE"
    assert report.problems[0]["table"] == "t_missing"


def test_schema_validation_blocks_unknown_column():
    from agents.tool.sql_tools.sql_shape import extract_sql_shape
    from agents.tool.sql_tools.sql_validation import validate_sql_schema

    shape = extract_sql_shape("SELECT ji.missing_amount FROM t_journal_item ji", dialect="mysql")

    report = validate_sql_schema(shape, SEMANTIC_MODEL)

    assert report.passed is False
    assert report.problems[0]["code"] == "UNKNOWN_COLUMN"
    assert report.problems[0]["table"] == "t_journal_item"
    assert report.problems[0]["column"] == "missing_amount"


def test_schema_validation_blocks_unknown_table_alias():
    from agents.tool.sql_tools.sql_shape import extract_sql_shape
    from agents.tool.sql_tools.sql_validation import validate_sql_schema

    shape = extract_sql_shape(
        "SELECT je.entry_date FROM t_journal_item ji WHERE je.status = '已过账'",
        dialect="mysql",
    )

    report = validate_sql_schema(shape, SEMANTIC_MODEL)

    assert report.passed is False
    assert report.problems[0]["code"] == "UNKNOWN_TABLE_ALIAS"
    assert report.problems[0]["table_alias"] == "je"
    assert report.problems[0]["column"] == "entry_date"


def test_semantic_check_blocks_unknown_schema_column():
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    report = check_sql_semantics(
        query="查询净利润",
        sql="SELECT SUM(ji.credit_amount - ji.missing_amount) FROM t_journal_item ji",
        semantic_model=SEMANTIC_MODEL,
    )

    assert report.passed is False
    assert report.problems[0].code == "UNKNOWN_COLUMN"
    assert report.problems[0].actual == "t_journal_item.missing_amount"


def test_relationship_validation_accepts_known_join_path():
    from agents.tool.sql_tools.sql_shape import extract_sql_shape
    from agents.tool.sql_tools.sql_validation import validate_sql_relationships

    shape = extract_sql_shape(
        "SELECT 1 FROM t_journal_item ji JOIN t_journal_entry je ON ji.entry_id = je.id",
        dialect="mysql",
    )

    report = validate_sql_relationships(
        shape,
        relationships=[
            {
                "from_table": "t_journal_item",
                "from_column": "entry_id",
                "to_table": "t_journal_entry",
                "to_column": "id",
            }
        ],
    )

    assert report.passed is True
    assert report.problems == []


def test_relationship_validation_blocks_unknown_join_path():
    from agents.tool.sql_tools.sql_shape import extract_sql_shape
    from agents.tool.sql_tools.sql_validation import validate_sql_relationships

    shape = extract_sql_shape(
        "SELECT 1 FROM t_journal_item ji JOIN t_account a ON ji.entry_id = a.account_code",
        dialect="mysql",
    )

    report = validate_sql_relationships(
        shape,
        relationships=[
            {
                "from_table": "t_journal_item",
                "from_column": "account_code",
                "to_table": "t_account",
                "to_column": "account_code",
            }
        ],
    )

    assert report.passed is False
    assert report.problems[0]["code"] == "UNKNOWN_JOIN_RELATIONSHIP"


def test_semantic_check_blocks_unknown_join_relationship():
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    report = check_sql_semantics(
        query="查询净利润",
        sql=(
            "SELECT SUM(ji.credit_amount - ji.debit_amount) "
            "FROM t_journal_item ji JOIN t_account a ON ji.entry_id = a.account_code"
        ),
        semantic_model=SEMANTIC_MODEL,
        relationships=[
            {
                "from_table": "t_journal_item",
                "from_column": "account_code",
                "to_table": "t_account",
                "to_column": "account_code",
            }
        ],
    )

    assert report.passed is False
    assert any(problem.code == "UNKNOWN_JOIN_RELATIONSHIP" for problem in report.problems)


def test_semantic_check_exposes_ordered_pipeline_gate_reports():
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    report = check_sql_semantics(
        query="查询净利润",
        sql=(
            "SELECT SUM(ji.credit_amount - ji.debit_amount) AS net_profit "
            "FROM t_journal_item ji"
        ),
        semantic_model=SEMANTIC_MODEL,
        relationships=[],
    )

    payload = report.to_dict()

    assert [gate["name"] for gate in payload["gate_reports"]] == [
        "sql.parse",
        "sql.ast_shape_extract",
        "sql.schema_validate",
        "sql.semantic_metric_validate",
        "sql.relationship_validate",
    ]
    assert payload["gate_reports"][0]["passed"] is True
    assert payload["gate_reports"][1]["extracted_facts"]["tables"] == ["t_journal_item"]
    assert payload["gate_reports"][2]["decision"] == "continue"
    assert payload["gate_reports"][3]["matched_signals"] == ["metric_expression:net_profit"]
    assert payload["gate_reports"][4]["decision"] == "skipped"
