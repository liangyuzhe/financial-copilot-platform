"""Local repository for verified NL-to-SQL pairs.

The repository is intentionally small and file-backed for the first quality
gate iteration. It is a governance/eval asset, not an execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import re
from typing import Iterable


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip()).rstrip(";").lower()


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _dedupe_tables(tables: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for table in tables or []:
        value = str(table or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


@dataclass(slots=True)
class VerifiedQueryRecord:
    """A reviewed or system-verified NL-to-SQL example."""

    question: str
    sql: str
    tables: list[str]
    intent: str
    verification_status: str
    result_signature: str = ""
    quality_score: float = 0.0
    metadata: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        payload = "|".join([
            _normalize_text(self.question),
            _normalize_sql(self.sql),
            ",".join(sorted(_dedupe_tables(self.tables))),
            _normalize_text(self.intent),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "question": self.question,
            "sql": self.sql,
            "tables": _dedupe_tables(self.tables),
            "intent": self.intent,
            "verification_status": self.verification_status,
            "result_signature": self.result_signature,
            "quality_score": self.quality_score,
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "VerifiedQueryRecord":
        return cls(
            question=str(payload.get("question") or ""),
            sql=str(payload.get("sql") or ""),
            tables=_dedupe_tables(payload.get("tables") or []),
            intent=str(payload.get("intent") or ""),
            verification_status=str(payload.get("verification_status") or ""),
            result_signature=str(payload.get("result_signature") or ""),
            quality_score=float(payload.get("quality_score") or 0.0),
            metadata=dict(payload.get("metadata") or {}),
        )


class VerifiedQueryRepository:
    """Append-friendly JSONL store with fingerprint dedupe."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def list_all(self) -> list[VerifiedQueryRecord]:
        if not self.path.exists():
            return []
        records: dict[str, VerifiedQueryRecord] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            record = VerifiedQueryRecord.from_dict(payload)
            fingerprint = str(payload.get("fingerprint") or record.fingerprint)
            records[fingerprint] = record
        return list(records.values())

    def save(self, record: VerifiedQueryRecord) -> VerifiedQueryRecord:
        existing = {
            item.fingerprint: item
            for item in self.list_all()
        }
        existing[record.fingerprint] = record
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True)
            for item in existing.values()
        ]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return record

    def find(
        self,
        *,
        question: str = "",
        intent: str = "",
        tables: Iterable[str] | None = None,
        limit: int = 5,
    ) -> list[VerifiedQueryRecord]:
        query_terms = set(_normalize_text(question).split())
        required_tables = set(_dedupe_tables(tables or []))
        matches: list[tuple[float, VerifiedQueryRecord]] = []
        for record in self.list_all():
            if intent and record.intent != intent:
                continue
            table_overlap = required_tables & set(record.tables)
            if required_tables and not table_overlap:
                continue
            record_terms = set(_normalize_text(record.question).split())
            score = record.quality_score
            if query_terms and record_terms:
                score += len(query_terms & record_terms) / max(len(query_terms), 1)
            score += len(table_overlap) * 0.1
            matches.append((score, record))
        matches.sort(key=lambda item: item[0], reverse=True)
        return [record for _score, record in matches[:limit]]


def verified_record_to_regression_case(record: VerifiedQueryRecord) -> dict:
    """Convert a verified query into an eval fixture, not an execution bypass."""
    tags = ["verified_query"]
    if record.intent:
        tags.append(record.intent)
    return {
        "query": record.question,
        "generated_sql": record.sql,
        "expected_sql": record.sql,
        "tables": _dedupe_tables(record.tables),
        "intent": record.intent,
        "verification_status": record.verification_status,
        "result_signature": record.result_signature,
        "quality_score": record.quality_score,
        "tags": tags,
        "quality_gate_required": True,
        "harness_bypass_allowed": False,
        "metadata": dict(record.metadata or {}),
    }


def write_verified_query_regression_dataset(
    repository: VerifiedQueryRepository,
    output_path: str | Path,
    *,
    limit: int | None = None,
) -> Path:
    """Write verified queries as JSONL regression cases for eval/replay."""
    records = repository.list_all()
    records.sort(key=lambda record: (record.intent, record.question, record.fingerprint))
    if limit is not None:
        records = records[:max(limit, 0)]

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(verified_record_to_regression_case(record), ensure_ascii=False, sort_keys=True)
        for record in records
    ]
    output.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return output
