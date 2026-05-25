"""Tests for evaluation CLI behavior."""

from __future__ import annotations

from argparse import Namespace

import pytest

from agents.eval.cli import cmd_export_verified_queries
from agents.eval.cli import cmd_run_nl2sql
from agents.eval.cli import cmd_run_online_nl2sql
from agents.tool.sql_tools.verified_query_repository import VerifiedQueryRecord, VerifiedQueryRepository


def test_run_nl2sql_missing_dataset_prints_actionable_error(tmp_path, capsys):
    missing = tmp_path / "missing.jsonl"

    with pytest.raises(SystemExit) as exc:
        cmd_run_nl2sql(Namespace(dataset=str(missing), output=str(tmp_path / "report.json"), init_template=False))

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "NL2SQL dataset not found" in captured.err
    assert "--init-template" in captured.err


def test_run_nl2sql_init_template_writes_dataset(tmp_path, capsys):
    dataset = tmp_path / "cases.jsonl"

    cmd_run_nl2sql(Namespace(dataset=str(dataset), output=str(tmp_path / "report.json"), init_template=True))

    assert dataset.exists()
    assert "Wrote NL2SQL evaluation template" in capsys.readouterr().out


def test_run_online_nl2sql_missing_dataset_prints_actionable_error(tmp_path, capsys):
    missing = tmp_path / "missing_online.jsonl"

    with pytest.raises(SystemExit) as exc:
        cmd_run_online_nl2sql(Namespace(
            dataset=str(missing),
            output=str(tmp_path / "report.json"),
            init_template=False,
            auto_approve_sql=False,
            max_approval_rounds=2,
            full_dispatch=False,
            session_prefix="eval",
        ))

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Online NL2SQL dataset not found" in captured.err
    assert "run-online-nl2sql" in captured.err


def test_run_online_nl2sql_init_template_writes_dataset(tmp_path, capsys):
    dataset = tmp_path / "online_cases.jsonl"

    cmd_run_online_nl2sql(Namespace(
        dataset=str(dataset),
        output=str(tmp_path / "report.json"),
        init_template=True,
        auto_approve_sql=False,
        max_approval_rounds=2,
        full_dispatch=False,
        session_prefix="eval",
    ))

    assert dataset.exists()
    assert "Wrote online NL2SQL evaluation template" in capsys.readouterr().out


def test_export_verified_queries_writes_regression_dataset(tmp_path, capsys):
    repository_path = tmp_path / "verified_queries.jsonl"
    repository = VerifiedQueryRepository(repository_path)
    repository.save(
        VerifiedQueryRecord(
            question="去年亏损多少",
            sql="SELECT SUM(credit_amount - debit_amount) AS net_profit FROM t_journal_item",
            tables=["t_journal_item"],
            intent="profit_loss",
            verification_status="human_verified",
        )
    )
    output = tmp_path / "regression.jsonl"

    cmd_export_verified_queries(Namespace(repository=str(repository_path), output=str(output), limit=None))

    assert output.exists()
    assert "Exported 1 verified query regression cases" in capsys.readouterr().out
