"""Tests for local verified query repository."""


def test_verified_query_repository_stores_and_finds_records(tmp_path):
    from agents.tool.sql_tools.verified_query_repository import (
        VerifiedQueryRecord,
        VerifiedQueryRepository,
    )

    repository = VerifiedQueryRepository(tmp_path / "verified_queries.jsonl")
    record = VerifiedQueryRecord(
        question="去年亏损多少",
        sql="SELECT CASE WHEN net_profit < 0 THEN ABS(net_profit) ELSE 0 END AS loss_amount FROM t",
        tables=["t_journal_item", "t_account"],
        intent="profit_loss",
        verification_status="system_verified",
        result_signature="columns:loss_amount",
        quality_score=0.93,
        metadata={"source": "unit-test"},
    )

    repository.save(record)

    matches = repository.find(question="去年亏损", intent="profit_loss", tables=["t_account"])

    assert len(matches) == 1
    assert matches[0].question == "去年亏损多少"
    assert matches[0].tables == ["t_journal_item", "t_account"]
    assert matches[0].metadata["source"] == "unit-test"


def test_verified_query_repository_deduplicates_by_fingerprint(tmp_path):
    from agents.tool.sql_tools.verified_query_repository import (
        VerifiedQueryRecord,
        VerifiedQueryRepository,
    )

    repository = VerifiedQueryRepository(tmp_path / "verified_queries.jsonl")
    first = VerifiedQueryRecord(
        question="去年亏损多少",
        sql="SELECT 1 FROM t_journal_item",
        tables=["t_journal_item"],
        intent="profit_loss",
        verification_status="system_verified",
        result_signature="columns:loss_amount",
        quality_score=0.8,
    )
    second = VerifiedQueryRecord(
        question="去年亏损多少",
        sql="SELECT 1 FROM t_journal_item",
        tables=["t_journal_item"],
        intent="profit_loss",
        verification_status="human_verified",
        result_signature="columns:loss_amount",
        quality_score=0.95,
    )

    repository.save(first)
    repository.save(second)

    records = repository.list_all()
    assert len(records) == 1
    assert records[0].verification_status == "human_verified"
    assert records[0].quality_score == 0.95


def test_verified_query_repository_exports_regression_dataset_without_harness_bypass(tmp_path):
    import json

    from agents.tool.sql_tools.verified_query_repository import (
        VerifiedQueryRecord,
        VerifiedQueryRepository,
        write_verified_query_regression_dataset,
    )

    repository = VerifiedQueryRepository(tmp_path / "verified_queries.jsonl")
    repository.save(
        VerifiedQueryRecord(
            question="去年亏损多少",
            sql="SELECT SUM(credit_amount - debit_amount) AS net_profit FROM t_journal_item",
            tables=["t_journal_item"],
            intent="profit_loss",
            verification_status="human_verified",
            result_signature="columns:net_profit",
            quality_score=0.96,
            metadata={"owner": "finance"},
        )
    )

    output_path = write_verified_query_regression_dataset(
        repository,
        tmp_path / "verified_query_regression.jsonl",
    )

    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    case = json.loads(lines[0])
    assert case["query"] == "去年亏损多少"
    assert case["generated_sql"] == case["expected_sql"]
    assert case["tables"] == ["t_journal_item"]
    assert case["tags"] == ["verified_query", "profit_loss"]
    assert case["quality_gate_required"] is True
    assert case["harness_bypass_allowed"] is False
    assert case["metadata"]["owner"] == "finance"
