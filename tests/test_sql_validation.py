"""Tests for SQL schema and relationship validation gates."""


SEMANTIC_MODEL = {
    "t_journal_item": {
        "entry_id": {"column_name": "entry_id"},
        "account_code": {"column_name": "account_code"},
        "cost_center_id": {"column_name": "cost_center_id"},
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
        "balance_direction": {"column_name": "balance_direction"},
    },
    "t_expense_claim": {
        "id": {"column_name": "id"},
        "cost_center_id": {"column_name": "cost_center_id"},
        "approved_amount": {"column_name": "approved_amount"},
        "claim_date": {"column_name": "claim_date"},
        "status": {"column_name": "status"},
    },
    "t_cost_center": {
        "id": {"column_name": "id"},
        "department_id": {"column_name": "department_id"},
        "center_name": {"column_name": "center_name"},
    },
    "t_department": {
        "id": {"column_name": "id"},
        "name": {"column_name": "name"},
    },
}

RELATIONSHIPS = [
    {
        "from_table": "t_journal_item",
        "from_column": "entry_id",
        "to_table": "t_journal_entry",
        "to_column": "id",
    },
    {
        "from_table": "t_journal_item",
        "from_column": "account_code",
        "to_table": "t_account",
        "to_column": "account_code",
    },
    {
        "from_table": "t_journal_item",
        "from_column": "cost_center_id",
        "to_table": "t_cost_center",
        "to_column": "id",
    },
    {
        "from_table": "t_cost_center",
        "from_column": "department_id",
        "to_table": "t_department",
        "to_column": "id",
    },
    {
        "from_table": "t_expense_claim",
        "from_column": "cost_center_id",
        "to_table": "t_cost_center",
        "to_column": "id",
    },
]


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


def test_sql_shape_extracts_non_aggregate_output_aliases():
    from agents.tool.sql_tools.sql_shape import extract_sql_shape

    shape = extract_sql_shape(
        "SELECT d.name AS department_name, 0 AS revenue, SUM(ec.approved_amount) AS approved_expense "
        "FROM t_department d LEFT JOIN t_expense_claim ec ON ec.department_id = d.id "
        "GROUP BY d.name",
        dialect="mysql",
    )

    aliases = [item.alias for item in shape.select_items]

    assert "department_name" in aliases
    assert "revenue" in aliases
    assert "approved_expense" in aliases
    assert any(item.alias == "revenue" and item.is_constant for item in shape.select_items)


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


def test_semantic_check_blocks_fabricated_profit_metrics_from_expense_only_sql():
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    report = check_sql_semantics(
        query="当前步骤目标: 统计2025年部门已审批报销费用",
        sql=(
            "SELECT d.name AS department_name, "
            "0 AS revenue, "
            "COALESCE(SUM(ec.approved_amount), 0) AS cost, "
            "0 - COALESCE(SUM(ec.approved_amount), 0) AS profit, "
            "CASE WHEN SUM(ec.approved_amount) > 0 "
            "THEN ROUND((0 - SUM(ec.approved_amount)) / SUM(ec.approved_amount) * 100, 2) "
            "ELSE 0 END AS profit_margin "
            "FROM t_department d "
            "LEFT JOIN t_cost_center cc ON d.id = cc.department_id "
            "LEFT JOIN t_expense_claim ec ON cc.id = ec.cost_center_id "
            "GROUP BY d.name"
        ),
        semantic_model=SEMANTIC_MODEL,
        relationships=RELATIONSHIPS,
    )

    assert report.passed is False
    assert any(problem.code == "FABRICATED_BUSINESS_METRIC" for problem in report.problems)


def test_relationship_validation_blocks_cost_center_id_joined_directly_to_department_id():
    from agents.tool.sql_tools.sql_shape import extract_sql_shape
    from agents.tool.sql_tools.sql_validation import validate_sql_relationships

    shape = extract_sql_shape(
        "SELECT d.name, SUM(ji.credit_amount - ji.debit_amount) "
        "FROM t_journal_item ji "
        "JOIN t_department d ON ji.cost_center_id = d.id "
        "GROUP BY d.name",
        dialect="mysql",
    )

    report = validate_sql_relationships(shape, RELATIONSHIPS)

    assert report.passed is False
    assert report.problems[0]["code"] == "UNKNOWN_JOIN_RELATIONSHIP"


def test_semantic_check_blocks_profit_formula_with_reverse_difference_branch():
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    report = check_sql_semantics(
        query="2025年按部门分析盈利率，亏损，成本",
        sql=(
            "SELECT d.name AS department, "
            "SUM(CASE "
            "WHEN a.balance_direction = '贷' THEN ji.credit_amount - ji.debit_amount "
            "WHEN a.balance_direction = '借' THEN ji.debit_amount - ji.credit_amount "
            "ELSE 0 END) AS profit "
            "FROM t_journal_item ji "
            "JOIN t_account a ON ji.account_code = a.account_code "
            "JOIN t_department d ON d.id = ji.cost_center_id "
            "GROUP BY d.name"
        ),
        semantic_model=SEMANTIC_MODEL,
    )

    assert report.passed is False
    assert any(problem.code == "REVERSED_METRIC_EXPRESSION" for problem in report.problems)
    assert any("不要把同一指标的左右字段反向相减后再加回" in hint for hint in report.fix_suggestions)


def test_semantic_check_checks_profit_alias_not_unrelated_revenue_aggregation():
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    report = check_sql_semantics(
        query="2025年按部门分析盈利率，亏损，成本",
        sql=(
            "SELECT d.name AS department, "
            "SUM(ji.credit_amount - ji.debit_amount) AS revenue, "
            "SUM(ji.debit_amount - ji.credit_amount) AS cost, "
            "SUM(CASE "
            "WHEN a.balance_direction = '贷' THEN ji.credit_amount - ji.debit_amount "
            "WHEN a.balance_direction = '借' THEN ji.debit_amount - ji.credit_amount "
            "ELSE 0 END) AS profit "
            "FROM t_journal_item ji "
            "JOIN t_account a ON ji.account_code = a.account_code "
            "JOIN t_department d ON d.id = ji.cost_center_id "
            "GROUP BY d.name"
        ),
        semantic_model=SEMANTIC_MODEL,
    )

    assert report.passed is False
    assert any(problem.code == "REVERSED_METRIC_EXPRESSION" for problem in report.problems)


def test_semantic_check_blocks_missing_expected_output_schema_columns():
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    report = check_sql_semantics(
        query="按部门维度统计净利润、净利率、毛利率",
        sql=(
            "SELECT cc.center_name AS 部门, "
            "SUM(ji.credit_amount - ji.debit_amount) AS 净利润 "
            "FROM t_journal_item ji "
            "JOIN t_account a ON ji.account_code = a.account_code "
            "JOIN t_cost_center cc ON ji.cost_center_id = cc.id "
            "GROUP BY cc.center_name"
        ),
        semantic_model=SEMANTIC_MODEL,
        relationships=RELATIONSHIPS,
        expected_output_columns=["部门", "成本", "净利润", "盈利率"],
    )

    assert report.passed is False
    assert any(problem.code == "MISSING_OUTPUT_SCHEMA_COLUMN" for problem in report.problems)
    assert any("成本" in problem.message and "盈利率" in problem.message for problem in report.problems)


def test_semantic_check_blocks_profitability_rate_without_governed_denominator():
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    report = check_sql_semantics(
        query="2026年按部门分析盈利率，亏损，成本",
        sql=(
            "SELECT ji.cost_center_id AS cost_center_id, cc.center_name AS 部门, "
            "SUM(CASE WHEN a.account_type = '损益' AND a.account_code LIKE '6%' "
            "THEN ji.credit_amount - ji.debit_amount ELSE 0 END) AS 净利润, "
            "CASE WHEN SUM(CASE WHEN a.account_type = '损益' AND a.account_code LIKE '5%' "
            "THEN ji.credit_amount - ji.debit_amount ELSE 0 END) = 0 THEN NULL "
            "ELSE ROUND(SUM(CASE WHEN a.account_type = '损益' AND a.account_code LIKE '6%' "
            "THEN ji.credit_amount - ji.debit_amount ELSE 0 END) / "
            "SUM(CASE WHEN a.account_type = '损益' AND a.account_code LIKE '5%' "
            "THEN ji.credit_amount - ji.debit_amount ELSE 0 END) * 100, 2) END AS 盈利率 "
            "FROM t_journal_item ji "
            "JOIN t_account a ON ji.account_code = a.account_code "
            "JOIN t_cost_center cc ON ji.cost_center_id = cc.id "
            "GROUP BY ji.cost_center_id, cc.center_name"
        ),
        semantic_model=SEMANTIC_MODEL,
        relationships=RELATIONSHIPS,
        evidence=[
            "术语: 盈利率\n公式: 净利润 / 收入 * 100；收入为 0 时盈利率不可计算\n同义词: 利润率,净利率\n关联表: t_journal_entry,t_journal_item,t_account",
            "术语: 收入\n公式: 主营业务收入；通常取贷方收入发生额，例如 account_code='6001'\n同义词: 营收,主营业务收入\n关联表: t_journal_entry,t_journal_item,t_account",
        ],
        expected_output_columns=["部门", "净利润", "盈利率"],
        expected_output_schema=[
            {"role": "department", "label": "部门", "column": "部门", "type": "dimension"},
            {"role": "net_profit", "label": "净利润", "column": "净利润", "type": "amount"},
            {"role": "profitability", "label": "盈利率", "column": "盈利率", "type": "percent"},
        ],
    )

    assert report.passed is False
    assert any(problem.code == "INVALID_RATIO_DENOMINATOR" for problem in report.problems)
    assert any("分母" in hint and "收入" in hint for hint in report.fix_suggestions)


def test_semantic_check_accepts_profitability_rate_using_governed_denominator_alias():
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    report = check_sql_semantics(
        query="2026年按部门分析盈利率，亏损，成本",
        sql=(
            "SELECT cc.center_name AS 部门, "
            "SUM(CASE WHEN a.account_code = '6001' THEN ji.credit_amount ELSE 0 END) AS 收入, "
            "SUM(CASE WHEN a.account_code = '6401' THEN ji.debit_amount ELSE 0 END) AS 成本, "
            "SUM(ji.credit_amount - ji.debit_amount) AS 净利润, "
            "CASE WHEN SUM(CASE WHEN a.account_code = '6001' THEN ji.credit_amount ELSE 0 END) = 0 "
            "THEN NULL ELSE SUM(ji.credit_amount - ji.debit_amount) / "
            "SUM(CASE WHEN a.account_code = '6001' THEN ji.credit_amount ELSE 0 END) * 100 END AS 盈利率 "
            "FROM t_journal_item ji "
            "JOIN t_account a ON ji.account_code = a.account_code "
            "JOIN t_cost_center cc ON ji.cost_center_id = cc.id "
            "GROUP BY cc.center_name"
        ),
        semantic_model=SEMANTIC_MODEL,
        relationships=RELATIONSHIPS,
        evidence=[
            "术语: 收入\n公式: 主营业务收入；通常取损益类收入科目的贷方发生额，例如 t_account.account_code='6001'\n同义词: 营收,主营业务收入\n关联表: t_journal_entry,t_journal_item,t_account",
            "术语: 成本\n公式: 主营业务成本；通常取损益类成本科目的借方发生额，例如 t_account.account_code='6401'\n同义词: 营业成本\n关联表: t_journal_entry,t_journal_item,t_account",
            "术语: 盈利率\n公式: 净利润 / 收入 * 100；收入为 0 时盈利率不可计算\n同义词: 利润率,净利率\n关联表: t_journal_entry,t_journal_item,t_account",
        ],
        expected_output_schema=[
            {"role": "department", "label": "部门", "column": "部门", "type": "dimension"},
            {"role": "revenue", "label": "收入", "column": "收入", "type": "amount"},
            {"role": "cost", "label": "成本", "column": "成本", "type": "amount"},
            {"role": "net_profit", "label": "净利润", "column": "净利润", "type": "amount"},
            {"role": "profitability", "label": "盈利率", "column": "盈利率", "type": "percent"},
        ],
    )

    assert report.passed is True


def test_semantic_check_blocks_amount_metric_missing_formula_filter():
    from agents.tool.sql_tools.semantic_check import check_sql_semantics

    report = check_sql_semantics(
        query="2026年按部门分析盈利率，亏损，成本",
        sql=(
            "SELECT cc.center_name AS 部门, "
            "SUM(CASE WHEN a.account_type = '成本' THEN ji.debit_amount ELSE 0 END) AS 成本, "
            "SUM(ji.credit_amount - ji.debit_amount) AS 净利润 "
            "FROM t_journal_item ji "
            "JOIN t_account a ON ji.account_code = a.account_code "
            "JOIN t_cost_center cc ON ji.cost_center_id = cc.id "
            "GROUP BY cc.center_name"
        ),
        semantic_model=SEMANTIC_MODEL,
        relationships=RELATIONSHIPS,
        evidence=[
            "术语: 成本\n公式: 主营业务成本；通常取损益类成本科目的借方发生额，例如 t_account.account_code='6401'\n同义词: 营业成本\n关联表: t_journal_entry,t_journal_item,t_account",
        ],
        expected_output_schema=[
            {"role": "department", "label": "部门", "column": "部门", "type": "dimension"},
            {"role": "cost", "label": "成本", "column": "成本", "type": "amount"},
            {"role": "net_profit", "label": "净利润", "column": "净利润", "type": "amount"},
        ],
    )

    assert report.passed is False
    assert any(problem.code == "MISSING_FORMULA_FILTER" for problem in report.problems)


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
        "sql.output_metric_contract_validate",
        "sql.output_schema_validate",
        "sql.schema_validate",
        "sql.semantic_metric_validate",
        "sql.relationship_validate",
    ]
    assert payload["gate_reports"][0]["passed"] is True
    assert payload["gate_reports"][1]["extracted_facts"]["tables"] == ["t_journal_item"]
    assert payload["gate_reports"][2]["decision"] == "skipped"
    assert payload["gate_reports"][3]["decision"] == "skipped"
    assert payload["gate_reports"][4]["decision"] == "continue"
    assert payload["gate_reports"][5]["matched_signals"] == ["metric_expression:net_profit"]
    assert payload["gate_reports"][6]["decision"] == "skipped"
