"""Offline shadow benchmark helpers for AgentScope runtime decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from typing import Any


JsonDict = dict[str, Any]
AGENTSCOPE_TASK_TYPES = {
    "exploratory_analysis",
    "complex_analysis",
    "report_generation",
}


@dataclass(frozen=True, slots=True)
class ShadowBenchmarkCase:
    query: str
    task_type: str
    baseline_runtime: str
    candidate_runtime: str
    tags: tuple[str, ...] = ()

    def to_dict(self) -> JsonDict:
        return {
            "query": self.query,
            "task_type": self.task_type,
            "baseline_runtime": self.baseline_runtime,
            "candidate_runtime": self.candidate_runtime,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class ShadowRunRecord:
    task_type: str
    runtime: str
    query: str = ""
    latency_ms: float = 0.0
    llm_calls: int = 0
    token_count: int = 0
    sql_draft_count: int = 0
    sql_draft_passed_count: int = 0
    approval_count: int = 0
    approval_passed_count: int = 0
    tool_call_count: int = 0
    tool_failure_count: int = 0
    final_answer_usable: bool = True
    extra: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "task_type": self.task_type,
            "runtime": self.runtime,
            "query": self.query,
            "latency_ms": self.latency_ms,
            "llm_calls": self.llm_calls,
            "token_count": self.token_count,
            "sql_draft_count": self.sql_draft_count,
            "sql_draft_passed_count": self.sql_draft_passed_count,
            "approval_count": self.approval_count,
            "approval_passed_count": self.approval_passed_count,
            "tool_call_count": self.tool_call_count,
            "tool_failure_count": self.tool_failure_count,
            "final_answer_usable": self.final_answer_usable,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True, slots=True)
class ShadowThresholds:
    p95_latency_ms: float = 15000.0
    max_tool_failure_rate: float = 0.05
    min_final_answer_usable_rate: float = 0.8
    min_sql_draft_pass_rate: float = 0.8
    min_approval_pass_rate: float = 0.8


class ShadowBenchmark:
    """Summarize shadow-run records without changing production routing."""

    @staticmethod
    def default_cases() -> list[ShadowBenchmarkCase]:
        return [
            ShadowBenchmarkCase(
                query="去年亏损多少？",
                task_type="strict_sql_query",
                baseline_runtime="sql_harness",
                candidate_runtime="sql_harness",
                tags=("nl2sql", "baseline"),
            ),
            ShadowBenchmarkCase(
                query="这个数据源里有哪些财务指标？",
                task_type="exploratory_analysis",
                baseline_runtime="manual_review",
                candidate_runtime="agentscope",
                tags=("exploration",),
            ),
            ShadowBenchmarkCase(
                query="分析今年收入、成本、预算、回款和费用之间的关系",
                task_type="complex_analysis",
                baseline_runtime="sql_harness",
                candidate_runtime="agentscope",
                tags=("complex", "sql_draft"),
            ),
            ShadowBenchmarkCase(
                query="基于已执行结果生成收入成本分析报告",
                task_type="report_generation",
                baseline_runtime="manual_report",
                candidate_runtime="agentscope",
                tags=("report",),
            ),
        ]

    @staticmethod
    def summarize(records: list[ShadowRunRecord]) -> JsonDict:
        groups: dict[tuple[str, str], list[ShadowRunRecord]] = {}
        for record in records:
            groups.setdefault((record.task_type, record.runtime), []).append(record)
        return {
            "num_records": len(records),
            "groups": [
                ShadowBenchmark.summarize_group(group_records)
                for _, group_records in sorted(groups.items())
            ],
        }

    @staticmethod
    def summarize_group(records: list[ShadowRunRecord]) -> JsonDict:
        if not records:
            return {}
        latencies = [record.latency_ms for record in records]
        tool_calls = sum(record.tool_call_count for record in records)
        tool_failures = sum(record.tool_failure_count for record in records)
        sql_drafts = sum(record.sql_draft_count for record in records)
        sql_drafts_passed = sum(record.sql_draft_passed_count for record in records)
        approvals = sum(record.approval_count for record in records)
        approvals_passed = sum(record.approval_passed_count for record in records)
        usable = sum(1 for record in records if record.final_answer_usable)

        return {
            "task_type": records[0].task_type,
            "runtime": records[0].runtime,
            "num_runs": len(records),
            "latency": {
                "avg_ms": _round(sum(latencies) / len(latencies)),
                "p50_ms": _percentile(latencies, 50),
                "p95_ms": _percentile(latencies, 95),
            },
            "cost": {
                "avg_llm_calls": _round(sum(record.llm_calls for record in records) / len(records)),
                "avg_token_count": _round(sum(record.token_count for record in records) / len(records)),
            },
            "quality": {
                "sql_draft_pass_rate": _rate(sql_drafts_passed, sql_drafts),
                "approval_pass_rate": _rate(approvals_passed, approvals),
                "tool_failure_rate": _rate(tool_failures, tool_calls, default=0.0),
                "final_answer_usable_rate": _round(usable / len(records), digits=4),
            },
        }

    @staticmethod
    def should_enable_agentscope(
        *,
        task_type: str,
        summary: JsonDict,
        thresholds: ShadowThresholds | None = None,
    ) -> bool:
        if task_type not in AGENTSCOPE_TASK_TYPES:
            return False
        thresholds = thresholds or ShadowThresholds()
        latency = summary.get("latency", {})
        quality = summary.get("quality", {})
        if float(latency.get("p95_ms", 0.0) or 0.0) > thresholds.p95_latency_ms:
            return False
        if float(quality.get("tool_failure_rate", 0.0) or 0.0) > thresholds.max_tool_failure_rate:
            return False
        if float(quality.get("final_answer_usable_rate", 0.0) or 0.0) < thresholds.min_final_answer_usable_rate:
            return False
        sql_draft_pass_rate = quality.get("sql_draft_pass_rate")
        if sql_draft_pass_rate is not None and float(sql_draft_pass_rate) < thresholds.min_sql_draft_pass_rate:
            return False
        approval_pass_rate = quality.get("approval_pass_rate")
        if approval_pass_rate is not None and float(approval_pass_rate) < thresholds.min_approval_pass_rate:
            return False
        return True


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = max(0, ceil((percentile / 100) * len(sorted_values)) - 1)
    return _round(sorted_values[min(index, len(sorted_values) - 1)])


def _rate(passed: int, total: int, *, default: float | None = None) -> float | None:
    if total <= 0:
        return default
    return _round(passed / total, digits=4)


def _round(value: float, *, digits: int = 1) -> float:
    return round(float(value), digits)
