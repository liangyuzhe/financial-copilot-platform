"""CLI entry point for RAG retrieval evaluation.

Usage:
    # Step 1: Generate evaluation dataset
    python -m agents.eval.cli generate --num-per-table 3 --output eval_dataset.jsonl

    # Step 2: Run evaluation
    python -m agents.eval.cli run --dataset eval_dataset.jsonl --output eval_report.json

    # Step 3: View detailed report
    python -m agents.eval.cli detail --dataset eval_dataset.jsonl --report eval_report.json
"""

import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def cmd_generate(args):
    """Generate evaluation dataset."""
    from agents.eval.dataset_generator import generate_eval_dataset

    dataset = asyncio.run(generate_eval_dataset(
        num_queries_per_table=args.num_per_table,
        output_path=args.output,
        annotate_knowledge=not args.no_knowledge_labels,
    ))
    print(f"Generated {len(dataset)} evaluation queries -> {args.output}")


def cmd_run(args):
    """Run evaluation."""
    from agents.eval.runner import run_evaluation, format_detail_report

    reports = run_evaluation(
        dataset_path=args.dataset,
        output_path=args.output,
        include_online_pipeline=args.include_online_pipeline,
    )

    if args.detail and reports:
        print(format_detail_report(reports))


def cmd_detail(args):
    """Show detailed evaluation report."""
    import json
    from agents.eval.runner import StrategyReport, StrategyConfig, EvalResult, format_detail_report

    with open(args.report, "r") as f:
        data = json.load(f)

    print("\nEvaluation Report Summary:")
    print("=" * 60)
    entries = data.get("strategies", data if isinstance(data, list) else [])
    for entry in entries:
        print(f"\nStrategy: {entry['strategy']}")
        print(f"  Description: {entry['description']}")
        print(f"  Queries: {entry['num_queries']}")
        latency = entry.get("latency", {})
        print(f"  Avg Latency: {latency.get('avg_ms', entry.get('avg_latency_ms'))}ms")
        print(f"  Metrics:")
        for k, v in entry["metrics"].items():
            print(f"    {k}: {v:.4f}")


def cmd_run_nl2sql(args):
    """Run offline NL2SQL end-to-end evaluation."""
    from pathlib import Path

    from agents.eval.nl2sql_runner import run_nl2sql_evaluation, write_nl2sql_template

    dataset_path = Path(args.dataset)
    if args.init_template:
        output = write_nl2sql_template(dataset_path)
        print(f"Wrote NL2SQL evaluation template -> {output}")
        print("Fill generated_sql/actual_result/expected_result with recorded cases, then rerun without --init-template.")
        return

    if not dataset_path.exists():
        print(
            f"NL2SQL dataset not found: {dataset_path}\n"
            "This command evaluates recorded NL2SQL cases; it does not generate them automatically.\n"
            f"Create a template first with: python -m agents.eval.cli run-nl2sql --dataset {dataset_path} --init-template",
            file=sys.stderr,
        )
        sys.exit(2)

    report = run_nl2sql_evaluation(
        dataset_path=dataset_path,
        output_path=args.output,
    )
    print(f"Evaluated {report['num_queries']} NL2SQL cases -> {args.output}")
    for key, value in report.get("metrics", {}).items():
        print(f"  {key}: {value:.4f}")


def cmd_run_online_nl2sql(args):
    """Run live NL2SQL evaluation through the agent graph."""
    from pathlib import Path

    from agents.eval.online_nl2sql_runner import (
        run_online_nl2sql_evaluation,
        write_online_nl2sql_template,
    )

    dataset_path = Path(args.dataset)
    if args.init_template:
        output = write_online_nl2sql_template(dataset_path)
        print(f"Wrote online NL2SQL evaluation template -> {output}")
        print("Fill query/expected_result, then rerun without --init-template.")
        return

    if not dataset_path.exists():
        print(
            f"Online NL2SQL dataset not found: {dataset_path}\n"
            "This command replays cases through the live agent and may call external LLMs.\n"
            f"Create a template first with: python -m agents.eval.cli run-online-nl2sql --dataset {dataset_path} --init-template",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.auto_approve_sql:
        print(
            "Warning: --auto-approve-sql will execute safe SELECT/WITH SQL through the live SQL tool.",
            file=sys.stderr,
        )

    report = run_online_nl2sql_evaluation(
        dataset_path=dataset_path,
        output_path=args.output,
        auto_approve_sql=args.auto_approve_sql,
        max_approval_rounds=args.max_approval_rounds,
        force_sql_intent=not args.full_dispatch,
        session_prefix=args.session_prefix,
    )
    print(f"Evaluated {report['num_queries']} online NL2SQL cases -> {args.output}")
    for key, value in report.get("metrics", {}).items():
        print(f"  {key}: {value:.4f}")


def cmd_export_verified_queries(args):
    """Export verified NL-to-SQL records as regression JSONL cases."""
    from pathlib import Path

    from agents.tool.sql_tools.verified_query_repository import (
        VerifiedQueryRepository,
        write_verified_query_regression_dataset,
    )

    repository = VerifiedQueryRepository(args.repository)
    output = write_verified_query_regression_dataset(
        repository,
        Path(args.output),
        limit=args.limit,
    )
    count = len(output.read_text(encoding="utf-8").splitlines()) if output.exists() else 0
    print(f"Exported {count} verified query regression cases -> {output}")


def main():
    parser = argparse.ArgumentParser(description="RAG Retrieval Evaluation")
    sub = parser.add_subparsers(dest="command")

    # generate
    p_gen = sub.add_parser("generate", help="Generate evaluation dataset")
    p_gen.add_argument("--num-per-table", type=int, default=3, help="Queries per table")
    p_gen.add_argument("--output", default="eval_dataset.jsonl", help="Output path")
    p_gen.add_argument(
        "--no-knowledge-labels",
        action="store_true",
        help="Do not add local business/few-shot relevance labels",
    )

    # run
    p_run = sub.add_parser("run", help="Run evaluation")
    p_run.add_argument("--dataset", default="eval_dataset.jsonl", help="Dataset path")
    p_run.add_argument("--output", default="eval_report.json", help="Report output path")
    p_run.add_argument("--detail", action="store_true", help="Show detailed report")
    p_run.add_argument(
        "--include-online-pipeline",
        action="store_true",
        help="Also evaluate recall_evidence -> recall_context -> query_enhance -> select_tables; may call external LLM",
    )

    # detail
    p_det = sub.add_parser("detail", help="Show detailed report")
    p_det.add_argument("--dataset", default="eval_dataset.jsonl", help="Dataset path")
    p_det.add_argument("--report", default="eval_report.json", help="Report path")

    # run-nl2sql
    p_nl2sql = sub.add_parser("run-nl2sql", help="Run offline NL2SQL end-to-end evaluation")
    p_nl2sql.add_argument("--dataset", required=True, help="JSONL cases with generated_sql/result labels")
    p_nl2sql.add_argument("--output", default="data/eval/nl2sql_eval_report.json", help="Report output path")
    p_nl2sql.add_argument(
        "--init-template",
        action="store_true",
        help="Write a starter NL2SQL JSONL dataset template and exit",
    )

    # run-online-nl2sql
    p_online = sub.add_parser("run-online-nl2sql", help="Replay NL2SQL cases through the live agent")
    p_online.add_argument("--dataset", required=True, help="JSONL cases with query and optional expected_result")
    p_online.add_argument("--output", default="data/eval/online_nl2sql_eval_report.json", help="Report output path")
    p_online.add_argument(
        "--init-template",
        action="store_true",
        help="Write a starter online NL2SQL JSONL dataset template and exit",
    )
    p_online.add_argument(
        "--auto-approve-sql",
        action="store_true",
        help="Auto-approve safe SQL interrupts so cases execute end to end",
    )
    p_online.add_argument(
        "--max-approval-rounds",
        type=int,
        default=2,
        help="Maximum SQL approval/resume rounds per case",
    )
    p_online.add_argument(
        "--full-dispatch",
        action="store_true",
        help="Do not force sql_query intent; evaluate classify_intent as part of the path",
    )
    p_online.add_argument(
        "--session-prefix",
        default="eval-online-nl2sql",
        help="Session id prefix used for isolated replay threads",
    )

    # export-verified-queries
    p_export_verified = sub.add_parser(
        "export-verified-queries",
        help="Export Verified Query Repository records to NL2SQL regression JSONL",
    )
    p_export_verified.add_argument(
        "--repository",
        default="data/eval/verified_queries.jsonl",
        help="Verified Query Repository JSONL path",
    )
    p_export_verified.add_argument(
        "--output",
        default="data/eval/verified_query_regression.jsonl",
        help="Regression dataset output JSONL path",
    )
    p_export_verified.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum records to export",
    )

    args = parser.parse_args()

    if args.command == "generate":
        cmd_generate(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "detail":
        cmd_detail(args)
    elif args.command == "run-nl2sql":
        cmd_run_nl2sql(args)
    elif args.command == "run-online-nl2sql":
        cmd_run_online_nl2sql(args)
    elif args.command == "export-verified-queries":
        cmd_export_verified_queries(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
