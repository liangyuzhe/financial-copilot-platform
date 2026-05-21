"""SQL React 图：自然语言 -> SQL -> 审批 -> 执行，支持自动纠错重试。"""

import asyncio
import json
import logging
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.documents import Document

from agents.flow.state import SQLReactState
from agents.flow.complex_query import assess_query_feasibility, validate_complex_plan
from agents.model.chat_model import get_chat_model
from agents.model.format_tool import create_format_tool, normalize_sql_answer
from agents.runtime import AgentScopeRuntime, create_agentscope_runner
from agents.tool.sql_tools.safety import SQLSafetyChecker
from agents.tool.sql_tools.error_codes import is_retryable
from agents.rag.retriever import recall_business_knowledge, recall_agent_knowledge, get_semantic_model_by_tables, load_full_table_metadata, get_table_relationships
from agents.tool.storage.checkpoint import get_checkpointer
from agents.tool.storage.query_route_rules import evaluate_query_route_rules
from agents.tool.security.policies import authorize_tables, build_audit_event
from agents.tool.security.presentation import format_result_for_user
from agents.tool.security.audit import write_audit_log
from agents.config.settings import settings
from agents.tool.trace.tracing import callbacks_from_config, child_trace_config, traced_async_tool_call, traced_tool_call

try:
    from elasticsearch import Elasticsearch
except ImportError:
    Elasticsearch = None

logger = logging.getLogger(__name__)


_EXECUTION_TIME_RE = re.compile(r"\s*Query execution time:\s*[\d.]+\s*ms\s*$", re.IGNORECASE)


def _strip_execution_time(result: str) -> str:
    return _EXECUTION_TIME_RE.sub("", result or "").strip()


def _format_result_value(value) -> str:
    if value is None:
        return "无数据"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _is_truthy_flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是"}
    return bool(value)


def _is_flag_field(name: str, value) -> bool:
    normalized = str(name or "").lower()
    if isinstance(value, bool):
        return True
    if normalized.startswith(("is_", "has_", "whether_", "if_")):
        return True
    if re.search(r"(^|_)(flag|boolean|bool|positive|negative)(_|$)", normalized):
        return True
    if "是否" in str(name):
        return True
    return False


def _extract_business_entries(evidence: list[str]) -> list[dict[str, str | list[str]]]:
    return _parse_business_evidence(evidence)


def _query_matched_terms(query: str, evidence: list[str]) -> list[dict[str, str]]:
    matches = []
    for entry in _extract_business_entries(evidence):
        term = str(entry.get("term") or "")
        aliases = [*entry.get("synonyms", []), term]  # type: ignore[list-item]
        for alias in aliases:
            if alias and alias in query:
                matches.append({"term": term or alias, "alias": alias})
                break
    return matches


def _query_asks_quantity(query: str) -> bool:
    quantity_markers = ("多少", "金额", "数额", "额度", "几", "多大", "合计", "总额")
    return any(marker in query for marker in quantity_markers)


def _query_has_relation_analysis_signal(query: str) -> bool:
    relation_markers = ("关系", "关联", "相关", "对比", "比较", "分析", "影响", "差异", "趋势")
    return any(marker in query for marker in relation_markers)


def _infer_task_type_from_recall_context(
    state: SQLReactState,
    query: str,
    selected_tables: list[str],
) -> tuple[str, dict]:
    """Infer decomposable analysis when recall evidence spans many business terms.

    This is deliberately driven by runtime recall terms and selected-table
    breadth, not by product table names or hardcoded finance table catalogs.
    """
    context = state.get("recall_context") or {}
    if not isinstance(context, dict):
        return "", {}

    query_text = " ".join(
        str(value or "")
        for value in (
            query,
            state.get("enhanced_query"),
            state.get("rewritten_query"),
            state.get("query"),
        )
    )
    matched_terms = _unique_ordered([
        str(term).strip()
        for term in context.get("matched_terms", [])
        if str(term).strip()
    ])
    related_tables = _unique_ordered([
        str(table).strip()
        for table in [
            *(context.get("business_related_tables") or []),
            *(context.get("few_shot_related_tables") or []),
        ]
        if str(table).strip()
    ])
    selected_count = len(set(selected_tables or []))
    related_selected_count = len(set(related_tables) & set(selected_tables or []))

    if (
        _query_has_relation_analysis_signal(query_text)
        and len(matched_terms) >= 3
        and (selected_count >= 4 or related_selected_count >= 3 or len(related_tables) >= 3)
    ):
        return "analysis", {
            "matched_terms": matched_terms,
            "business_related_tables_count": len(related_tables),
            "selected_tables_count": selected_count,
            "reason": "multi-term relation analysis inferred from recall context",
        }
    return "", {}


def _amount_label_from_query(query: str, matched_terms: list[dict[str, str]]) -> str | None:
    if not matched_terms or not _query_asks_quantity(query):
        return None
    alias = matched_terms[0]["alias"]
    if not alias:
        return None
    if alias.endswith(("金额", "数额", "额度", "总额")):
        return alias
    return f"{alias}金额"


def _field_label_from_docs(field: str, docs: list[Document]) -> str | None:
    field_re = re.compile(rf"(^|\s|`){re.escape(field)}(`|\s|$)", re.IGNORECASE)
    for doc in docs:
        for line in doc.page_content.splitlines():
            if not field_re.search(line):
                continue
            business = re.search(r"\[业务名:\s*([^\]]+)\]", line)
            if business and business.group(1).strip():
                return business.group(1).strip()
            comment = re.search(r"--\s*([^\[]+)", line)
            if comment and comment.group(1).strip():
                return comment.group(1).strip()
    return None


def _field_label_from_sql_alias(field: str, sql: str) -> str | None:
    alias_re = re.compile(r"\bAS\s+`?([^`,\s;]+)`?", re.IGNORECASE)
    for alias in alias_re.findall(sql or ""):
        if alias == field and re.search(r"[\u4e00-\u9fff]", alias):
            return alias
    if re.search(r"[\u4e00-\u9fff]", field):
        return field
    return None


def _field_label_from_semantic_model(field: str, state: SQLReactState) -> str | None:
    semantic_model = state.get("semantic_model", {}) or {}
    selected_tables = state.get("selected_tables", []) or []
    ordered_tables = [
        table for table in selected_tables
        if table in semantic_model
    ]
    ordered_tables.extend(
        table for table in semantic_model
        if table not in ordered_tables
    )
    for table in ordered_tables:
        column_meta = (semantic_model.get(table) or {}).get(field)
        if not isinstance(column_meta, dict):
            continue
        business_name = str(column_meta.get("business_name") or "").strip()
        if business_name:
            return business_name
        column_comment = str(column_meta.get("column_comment") or "").strip()
        if column_comment:
            return column_comment
    return None


def _friendly_field_label(field: str, value, state: SQLReactState, matched_terms: list[dict[str, str]], row: dict) -> str:
    query = state.get("enhanced_query") or state.get("rewritten_query") or state.get("query", "")
    non_flag_fields = [k for k, v in row.items() if not _is_flag_field(k, v)]
    if len(non_flag_fields) == 1 and non_flag_fields[0] == field:
        query_amount_label = _amount_label_from_query(query, matched_terms)
        if query_amount_label:
            return query_amount_label

    alias_label = _field_label_from_sql_alias(field, state.get("sql", ""))
    if alias_label:
        return alias_label

    semantic_label = _field_label_from_semantic_model(field, state)
    if semantic_label:
        return semantic_label

    docs_label = _field_label_from_docs(field, state.get("docs", []))
    if docs_label:
        return docs_label

    if _is_flag_field(field, value) and matched_terms:
        return f"是否{matched_terms[0]['alias']}"

    if matched_terms and len(non_flag_fields) == 1 and non_flag_fields[0] == field:
        return matched_terms[0]["term"]

    return field


def _format_row_for_user(row: dict, state: SQLReactState, matched_terms: list[dict[str, str]]) -> list[str]:
    lines = []
    for key, value in row.items():
        label = _friendly_field_label(str(key), value, state, matched_terms, row)
        display_value = "是" if _is_flag_field(str(key), value) and _is_truthy_flag(value) else (
            "否" if _is_flag_field(str(key), value) else _format_result_value(value)
        )
        lines.append(f"{label}：{display_value}")
    return lines


def _parse_sql_result_rows(result: str):
    clean = _strip_execution_time(result)
    if not clean:
        return None

    try:
        payload = json.loads(clean)
    except Exception:
        return clean

    if payload is None:
        return None

    rows = payload
    if isinstance(payload, dict):
        for key in ("rows", "data", "result", "items"):
            if key in payload:
                rows = payload[key]
                break
        else:
            rows = [payload]

    if not isinstance(rows, list):
        rows = [rows]

    return rows


def _format_sql_result_fallback(result: str, state: SQLReactState | None = None) -> str:
    """Generic non-business fallback; it does not translate field names."""
    state = state or {}
    query = state.get("enhanced_query") or state.get("rewritten_query") or state.get("query", "")
    matched_terms = _query_matched_terms(query, state.get("evidence", []))
    rows = _parse_sql_result_rows(result)
    if rows is None:
        return "查询已执行完成，但结果为空。"
    if isinstance(rows, str):
        return f"查询已执行完成。\n{rows}"
    if not rows:
        return "查询已执行完成，未查询到符合条件的数据。"

    if len(rows) == 1 and isinstance(rows[0], dict):
        row = rows[0]
        if not row:
            return "查询已执行完成，但返回行为空。"
        parts = _format_row_for_user(row, state, matched_terms)
        return "查询已执行完成。\n" + "\n".join(parts)

    if len(rows) == 1:
        return f"查询已执行完成。\n结果：{_format_result_value(rows[0])}"

    preview_lines = []
    for row in rows[:5]:
        if isinstance(row, dict):
            preview_lines.append("，".join(_format_row_for_user(row, state, matched_terms)))
        else:
            preview_lines.append(str(row))
    suffix = f"\n仅展示前 5 条。" if len(rows) > 5 else ""
    return f"查询已执行完成，共返回 {len(rows)} 条记录。\n" + "\n".join(preview_lines) + suffix


async def _summarize_sql_result(state: SQLReactState, result: str) -> str:
    """Summarize SQL result locally from SQL aliases, schema docs, evidence and query."""
    query = state.get("enhanced_query") or state.get("rewritten_query") or state.get("query", "")
    if not _query_matched_terms(query, state.get("evidence", [])):
        text, _metadata = format_result_for_user(
            result,
            semantic_model=state.get("semantic_model", {}),
            table_names=state.get("selected_tables", []),
            sql=state.get("sql", ""),
        )
        return text
    return _format_sql_result_fallback(result, state)


def _response_text(response) -> str:
    """Extract text from common LangChain chat response shapes."""
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return "".join(parts).strip()
    if content is None:
        return ""
    return str(content).strip()


def _parse_json_object(text: str) -> dict:
    clean = (text or "").strip()
    if not clean:
        return {}
    try:
        payload = json.loads(clean)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", clean, flags=re.S)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(0))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}


def _split_terms(value: str) -> list[str]:
    normalized = value.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _label_value(line: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        for sep in (":", "："):
            prefix = f"{label}{sep}"
            if line.startswith(prefix):
                return line[len(prefix):].strip()
    return None


def _parse_business_evidence(evidence: list[str]) -> list[dict[str, str | list[str]]]:
    entries = []
    for item in evidence:
        entry: dict[str, str | list[str]] = {
            "term": "",
            "formula": "",
            "synonyms": [],
            "related_tables": [],
        }
        for line in item.splitlines():
            line = line.strip()
            term = _label_value(line, ("术语",))
            formula = _label_value(line, ("公式", "定义"))
            synonyms = _label_value(line, ("同义词",))
            related_tables = _label_value(line, ("关联表",))
            if term is not None:
                entry["term"] = term
            elif formula is not None:
                entry["formula"] = formula
            elif synonyms is not None:
                entry["synonyms"] = _split_terms(synonyms)
            elif related_tables is not None:
                entry["related_tables"] = _split_terms(related_tables)
        if entry.get("term"):
            entries.append(entry)
    return entries


_SQL_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+`?([a-zA-Z_][\w]*)`?", re.IGNORECASE)


def _unique_ordered(values: list[str]) -> list[str]:
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _tables_from_few_shot_examples(few_shot_examples: list[str]) -> list[str]:
    tables = []
    for item in few_shot_examples:
        for table in _SQL_TABLE_RE.findall(item or ""):
            tables.append(table)
    return _unique_ordered(tables)


def _business_entry_matches_query(entry: dict[str, str | list[str]], query: str) -> bool:
    aliases = [str(entry.get("term") or ""), *entry.get("synonyms", [])]  # type: ignore[list-item]
    if any(alias and alias in query for alias in aliases):
        return True

    profile = "\n".join([
        str(entry.get("term") or ""),
        str(entry.get("formula") or ""),
        ",".join(str(item) for item in entry.get("synonyms", [])),  # type: ignore[union-attr]
    ])
    query_terms = {term for term in _ranking_terms(query) if len(term) >= 2}
    profile_terms = {term for term in _ranking_terms(profile) if len(term) >= 2}
    return bool(query_terms & profile_terms)


def _few_shot_matches_query(example: str, query: str) -> bool:
    question = ""
    for line in (example or "").splitlines():
        question = _label_value(line.strip(), ("问题", "Query"))
        if question:
            break
    if not question:
        return False
    query_terms = {term for term in _ranking_terms(query) if len(term) >= 2}
    question_terms = {term for term in _ranking_terms(question) if len(term) >= 2}
    overlap = query_terms & question_terms
    return sum(len(term) for term in overlap) >= 4


def _tables_from_matched_few_shot_examples(few_shot_examples: list[str], query: str) -> list[str]:
    tables = []
    for item in few_shot_examples:
        if not _few_shot_matches_query(item, query):
            continue
        for table in _SQL_TABLE_RE.findall(item or ""):
            tables.append(table)
    return _unique_ordered(tables)


def _questions_from_few_shot_examples(few_shot_examples: list[str]) -> list[str]:
    questions = []
    for item in few_shot_examples:
        for line in (item or "").splitlines():
            value = _label_value(line.strip(), ("问题", "Query"))
            if value:
                questions.append(value)
                break
    return _unique_ordered(questions)


def _build_recall_context(query: str, evidence: list[str], few_shot_examples: list[str]) -> dict:
    business_entries = _parse_business_evidence(evidence)
    matched_terms = []
    business_related_tables = []
    for entry in business_entries:
        if not _business_entry_matches_query(entry, query):
            continue
        term = str(entry.get("term") or "")
        if term:
            matched_terms.append(term)
        for table in entry.get("related_tables", []):  # type: ignore[union-attr]
            business_related_tables.append(str(table))

    return {
        "query_key": query,
        "business_evidence": evidence,
        "few_shot_examples": few_shot_examples,
        "business_related_tables": _unique_ordered(business_related_tables),
        "few_shot_related_tables": _tables_from_matched_few_shot_examples(few_shot_examples, query),
        "matched_terms": _unique_ordered(matched_terms),
        "few_shot_questions": _questions_from_few_shot_examples(few_shot_examples),
    }


def _recall_context_for_query(state: SQLReactState, query: str) -> dict:
    context = state.get("recall_context") or {}
    if not isinstance(context, dict):
        return {}
    context_query = state.get("rewritten_query") or state.get("query", "")
    if context.get("query_key") != context_query:
        return {}
    return context


def _filter_candidate_tables(tables: list[str], candidate_tables: list[str]) -> list[str]:
    candidate_set = set(candidate_tables)
    return [table for table in _unique_ordered(tables) if table in candidate_set]


def _merge_selected_tables(selected: list[str], evidence_tables: list[str]) -> list[str]:
    merged = []
    for table in [*evidence_tables, *selected]:
        if table and table not in merged:
            merged.append(table)
    return merged


def _semantic_fk_edges(semantic_model: dict) -> list[dict[str, str]]:
    edges = []
    for table, columns in semantic_model.items():
        for column, meta in (columns or {}).items():
            ref_table = str(meta.get("ref_table") or "")
            ref_column = str(meta.get("ref_column") or "")
            if not ref_table:
                continue
            is_fk = meta.get("is_fk")
            if str(is_fk).lower() not in {"1", "true", "yes", "y", "是"}:
                continue
            edges.append({
                "from_table": str(table),
                "from_column": str(column),
                "to_table": ref_table,
                "to_column": ref_column,
            })
    return edges


def _shortest_join_path(
    start: str,
    end: str,
    edges: list[dict[str, str]],
    candidate_tables: set[str],
    max_edges: int = 3,
) -> list[str]:
    """Find a short undirected FK path between two tables."""
    if start == end or start not in candidate_tables or end not in candidate_tables:
        return []

    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        from_table = edge.get("from_table")
        to_table = edge.get("to_table")
        if from_table not in candidate_tables or to_table not in candidate_tables:
            continue
        adjacency.setdefault(from_table, []).append(to_table)
        adjacency.setdefault(to_table, []).append(from_table)

    queue: list[list[str]] = [[start]]
    seen = {start}
    while queue:
        path = queue.pop(0)
        if len(path) - 1 >= max_edges:
            continue
        for next_table in adjacency.get(path[-1], []):
            if next_table in seen:
                continue
            next_path = [*path, next_table]
            if next_table == end:
                return next_path
            seen.add(next_table)
            queue.append(next_path)
    return []


def _expand_selected_tables_by_join_paths(
    selected: list[str],
    candidate_tables: list[str],
    edges: list[dict[str, str]],
    max_edges: int = 3,
) -> list[str]:
    """Add intermediate FK path tables between already-selected endpoints."""
    expanded = list(selected)
    candidate_set = set(candidate_tables)
    anchors = [table for table in selected if table in candidate_set]

    for left_index, left in enumerate(anchors):
        for right in anchors[left_index + 1:]:
            path = _shortest_join_path(left, right, edges, candidate_set, max_edges=max_edges)
            for table in path[1:-1]:
                if table not in expanded:
                    expanded.append(table)

    return expanded


def _expand_selected_tables_by_semantic_relationships(
    selected: list[str],
    candidate_tables: list[str],
    semantic_model: dict,
    query: str = "",
    table_metadata: dict[str, str] | None = None,
    recall_context: dict | None = None,
) -> list[str]:
    """Add join tables and endpoint tables using FK metadata from t_semantic_model."""
    selected_set = set(selected)
    candidate_set = set(candidate_tables)
    expanded = list(selected)
    edges = _semantic_fk_edges(semantic_model)
    table_metadata = table_metadata or {}
    recall_context = recall_context or {}

    def append(table: str) -> None:
        if table in candidate_set and table not in expanded:
            expanded.append(table)
            selected_set.add(table)

    refs_by_from: dict[str, set[str]] = {}
    for edge in edges:
        refs_by_from.setdefault(edge["from_table"], set()).add(edge["to_table"])

    changed = True
    while changed:
        changed = False

        # If a selected relation table references another candidate table, keep
        # the endpoint when that edge is relevant to the user query. Running this
        # as a closure avoids order-dependent one-hop expansion.
        for edge in edges:
            before = len(expanded)
            if edge["from_table"] in selected_set and _edge_relevant_to_query(edge, query, table_metadata, semantic_model):
                append(edge["to_table"])
            changed = changed or len(expanded) > before

        # If two selected tables are connected through an unselected bridge
        # table, keep that bridge table. This is driven by semantic FK metadata,
        # not by business keyword rules.
        for bridge, refs in refs_by_from.items():
            before = len(expanded)
            if (
                bridge not in selected_set
                and bridge in candidate_set
                and len(refs & selected_set) >= 2
                and (
                    _table_recall_context_score(bridge, recall_context) > 0
                    or (
                        _is_relation_table(bridge, semantic_model)
                        and _table_relevant_to_query_or_context(bridge, query, table_metadata, semantic_model, recall_context)
                    )
                )
            ):
                append(bridge)
            changed = changed or len(expanded) > before

    expanded = _expand_selected_tables_by_join_paths(expanded, candidate_tables, edges)
    return expanded


def _ranking_terms(text: str) -> set[str]:
    normalized = str(text or "").lower()
    terms = set(re.findall(r"[a-z0-9_]+", normalized))
    terms.update(ch for ch in normalized if "\u4e00" <= ch <= "\u9fff")
    for size in (2, 3, 4):
        for i in range(0, max(0, len(normalized) - size + 1)):
            chunk = normalized[i : i + size]
            if any("\u4e00" <= ch <= "\u9fff" for ch in chunk):
                terms.add(chunk)
    return terms


def _table_semantic_text(table: str, table_metadata: dict[str, str], semantic_model: dict) -> str:
    parts = [table, table_metadata.get(table, "")]
    for col_name, meta in (semantic_model.get(table) or {}).items():
        parts.extend([
            str(col_name),
            str(meta.get("column_comment") or ""),
            str(meta.get("business_name") or ""),
            str(meta.get("synonyms") or ""),
            str(meta.get("business_description") or ""),
        ])
    return "\n".join(part for part in parts if part)


def _table_semantic_score(table: str, query: str, table_metadata: dict[str, str], semantic_model: dict) -> float:
    query_terms = _ranking_terms(query)
    if not query_terms:
        return 0.0

    text = _table_semantic_text(table, table_metadata, semantic_model)
    table_terms = _ranking_terms(text)
    overlap = query_terms & table_terms
    score = sum(max(1, len(term)) for term in overlap)

    # Phrase matches are stronger than isolated character overlap.
    for meta in (semantic_model.get(table) or {}).values():
        phrases = [
            str(meta.get("business_name") or ""),
            str(meta.get("column_comment") or ""),
            *_split_terms(str(meta.get("synonyms") or "")),
        ]
        for phrase in phrases:
            if phrase and phrase in query:
                score += 6 + min(len(phrase), 8)

    comment = table_metadata.get(table, "")
    if comment and any(term in comment for term in query_terms if len(term) >= 2):
        score += 2
    return float(score)


def _table_recall_context_score(table: str, recall_context: dict) -> float:
    score = 0.0
    if table in recall_context.get("business_related_tables", []):
        score += 18
    if table in recall_context.get("few_shot_related_tables", []):
        score += 14
    return score


_ROUTING_PROFILE_MAX_FIELDS_PER_TABLE = 3
_ROUTING_PROFILE_MIN_FIELD_SCORE = 4.0


def _column_semantic_phrases(meta: dict) -> list[str]:
    phrases = [
        str(meta.get("business_name") or ""),
        str(meta.get("column_comment") or ""),
        *_split_terms(str(meta.get("synonyms") or "")),
    ]
    return [phrase for phrase in phrases if phrase]


def _column_semantic_text(column_name: str, meta: dict) -> str:
    parts = [
        column_name,
        str(meta.get("column_comment") or ""),
        str(meta.get("business_name") or ""),
        str(meta.get("synonyms") or ""),
        str(meta.get("business_description") or ""),
    ]
    ref_table = str(meta.get("ref_table") or "")
    ref_column = str(meta.get("ref_column") or "")
    if ref_table:
        parts.append(f"{ref_table}.{ref_column}" if ref_column else ref_table)
    return "\n".join(part for part in parts if part)


def _column_semantic_score(column_name: str, meta: dict, query: str) -> float:
    query_terms = _ranking_terms(query)
    if not query_terms:
        return 0.0

    column_terms = _ranking_terms(_column_semantic_text(column_name, meta))
    overlap = query_terms & column_terms
    score = sum(max(1, len(term)) for term in overlap)

    query_chunks = {term for term in query_terms if len(term) >= 2}
    for phrase in _column_semantic_phrases(meta):
        if phrase in query:
            score += 8 + min(len(phrase), 8)
        else:
            score += sum(4 + len(term) for term in query_chunks if term in phrase)
    return float(score)


def _column_recall_context_score(column_name: str, meta: dict, recall_context: dict) -> float:
    terms = [
        *[str(term) for term in recall_context.get("matched_terms", [])],
        *[str(question) for question in recall_context.get("few_shot_questions", [])],
    ]
    if not terms:
        return 0.0
    text = _column_semantic_text(column_name, meta)
    score = 0.0
    for term in terms:
        if not term:
            continue
        if term in text:
            score += 16 + min(len(term), 8)
            continue
        term_parts = _ranking_terms(term)
        field_terms = _ranking_terms(text)
        overlap = {part for part in term_parts & field_terms if len(part) >= 2}
        score += sum(3 + len(part) for part in overlap)
    return score


def _format_routing_field_hint(column_name: str, meta: dict) -> str:
    labels = []
    business_name = str(meta.get("business_name") or "").strip()
    column_comment = str(meta.get("column_comment") or "").strip()
    synonyms = _split_terms(str(meta.get("synonyms") or ""))
    ref_table = str(meta.get("ref_table") or "").strip()
    ref_column = str(meta.get("ref_column") or "").strip()

    if business_name:
        labels.append(business_name)
    elif column_comment:
        labels.append(column_comment)
    labels.extend(synonyms[:2])
    if ref_table:
        labels.append(f"-> {ref_table}.{ref_column}" if ref_column else f"-> {ref_table}")

    unique_labels = list(dict.fromkeys(labels))
    return f"{column_name}({'/'.join(unique_labels)})" if unique_labels else column_name


def _matched_routing_field_hints(
    table: str,
    query: str,
    semantic_model: dict,
    recall_context: dict | None = None,
    limit: int = _ROUTING_PROFILE_MAX_FIELDS_PER_TABLE,
) -> list[str]:
    recall_context = recall_context or {}
    scored = []
    for index, (column_name, meta) in enumerate((semantic_model.get(table) or {}).items()):
        score = _column_semantic_score(str(column_name), meta or {}, query)
        score += _column_recall_context_score(str(column_name), meta or {}, recall_context)
        if score < _ROUTING_PROFILE_MIN_FIELD_SCORE:
            continue
        if (meta or {}).get("ref_table") and str((meta or {}).get("ref_table")) != table:
            score += 8
        scored.append((score, -index, _format_routing_field_hint(str(column_name), meta or {})))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [hint for _, _, hint in scored[:limit]]


def _build_table_routing_profiles(
    candidate_tables: list[str],
    table_metadata: dict[str, str],
    semantic_model: dict,
    query: str,
    recall_context: dict | None = None,
) -> str:
    """Render compact table-selection hints without sending full field schema."""
    lines = []
    for table in candidate_tables:
        desc = table_metadata.get(table, "")
        line = f"- {table}: {desc}" if desc else f"- {table}"
        field_hints = _matched_routing_field_hints(table, query, semantic_model, recall_context=recall_context)
        if field_hints:
            line += " | 匹配字段: " + "；".join(field_hints)
        lines.append(line)
    return "\n".join(lines)


def _edge_relevant_to_query(edge: dict[str, str], query: str, table_metadata: dict[str, str], semantic_model: dict) -> bool:
    if not query:
        return True

    from_table = edge["from_table"]
    from_column = edge["from_column"]
    to_table = edge["to_table"]
    meta = (semantic_model.get(from_table) or {}).get(from_column) or {}

    if _column_semantic_score(from_column, meta, query) >= _ROUTING_PROFILE_MIN_FIELD_SCORE:
        return True
    if _table_semantic_score(to_table, query, table_metadata, semantic_model) >= _ROUTING_PROFILE_MIN_FIELD_SCORE:
        return True
    return False


def _table_relevant_to_query_or_context(
    table: str,
    query: str,
    table_metadata: dict[str, str],
    semantic_model: dict,
    recall_context: dict | None = None,
) -> bool:
    recall_context = recall_context or {}
    if _table_recall_context_score(table, recall_context) > 0:
        return True
    return _table_semantic_score(table, query, table_metadata, semantic_model) >= _ROUTING_PROFILE_MIN_FIELD_SCORE


def _is_relation_table(table: str, semantic_model: dict) -> bool:
    columns = semantic_model.get(table) or {}
    if not columns:
        return False
    non_system_columns = [
        str(column)
        for column in columns
        if str(column) not in {"id", "created_at", "updated_at"}
    ]
    if not non_system_columns:
        return False
    fk_columns = [
        str(column)
        for column, meta in columns.items()
        if str((meta or {}).get("is_fk")).lower() in {"1", "true", "yes", "y", "是"}
        and str(column) in non_system_columns
    ]
    return len(fk_columns) >= 2 and len(non_system_columns) <= len(fk_columns) + 2


def _rerank_selected_tables(
    selected: list[str],
    query: str,
    table_metadata: dict[str, str],
    semantic_model: dict,
    evidence_tables: list[str],
    recall_context: dict | None = None,
) -> list[str]:
    recall_context = recall_context or {}
    evidence_set = set(evidence_tables)
    indexed = []
    for index, table in enumerate(selected):
        score = _table_semantic_score(table, query, table_metadata, semantic_model)
        score += _table_recall_context_score(table, recall_context)
        if table in evidence_set:
            score += 10
        indexed.append((score, -index, table))
    indexed.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [table for _, _, table in indexed]


def _heuristic_enhance_query(query: str, evidence: list[str]) -> str:
    """Deterministic fallback driven by configured business knowledge."""
    enhanced = query.strip()

    additions = []
    for entry in _parse_business_evidence(evidence):
        term = str(entry.get("term") or "")
        formula = str(entry.get("formula") or "")
        aliases = [term, *entry.get("synonyms", [])]  # type: ignore[list-item]
        if not term or term in enhanced:
            continue
        if any(alias and alias in query for alias in aliases):
            if formula:
                additions.append(f"{term}: {formula}")
            else:
                additions.append(term)

    if additions:
        enhanced = f"{enhanced}（业务口径: {'; '.join(additions)}）"

    return enhanced or query


async def query_enhance(state: SQLReactState, config=None) -> dict:
    """用证据（业务知识）翻译查询中的业务术语，增强向量检索命中率。

    示例：
        Query: "华东区上月GMV是多少"
        Evidence: "GMV = 已支付订单总额", "华东包含上海、江苏、浙江..."
        Enhanced: "华东区（上海、江苏、浙江）上月已支付订单总额是多少"
    """
    query = state.get("rewritten_query") or state.get("query", "")
    evidence = state.get("evidence", [])

    if not evidence:
        enhanced = _heuristic_enhance_query(query, [])
        if enhanced != query:
            logger.info("query_enhance heuristic without evidence: '%s' -> '%s'", query[:80], enhanced[:80])
        return {"enhanced_query": enhanced}

    evidence_text = "\n".join(evidence)
    fallback_query = _heuristic_enhance_query(query, evidence)

    try:
        model = get_chat_model(settings.chat_model_type)
        response = await asyncio.wait_for(
            model.ainvoke(
                [
                    SystemMessage(content=(
                        "你是一个查询增强助手。根据业务知识，将用户查询中的业务术语、缩写、"
                        "隐含条件翻译/展开为数据库字段或通用表达，使查询更适合数据库检索。\n\n"
                        "规则：\n"
                        "1. 只翻译/展开查询中出现的业务术语，不要添加查询中没有的筛选条件\n"
                        "2. 保持查询的原始意图不变\n"
                        "3. 如果知识中有区域/维度的映射（如华东包含哪些省），用括号补充\n"
                        "4. 如果知识中有术语定义（如GMV=已支付订单总额），替换为更明确的表达\n"
                        "5. 如果用户使用了业务知识同义词或口语化表达，按召回到的术语、同义词、公式映射到最相关的业务口径；"
                        "不要混淆名称相近但公式或含义不同的指标\n"
                        "6. 只输出增强后的查询，不要解释；如果无需增强，原样输出用户查询，禁止输出空内容"
                    )),
                    HumanMessage(content=f"业务知识:\n{evidence_text}\n\n用户查询: {query}"),
                ],
                config=child_trace_config(config, "sql.query_enhance.llm", tags=["llm", "sql_react"]),
            ),
            timeout=settings.resilience.llm_rewrite_timeout,
        )
        enhanced = _response_text(response)
        if enhanced:
            logger.info("query_enhance: '%s' -> '%s'", query[:80], enhanced[:80])
            return {"enhanced_query": enhanced}
        logger.warning("query_enhance returned empty content, using fallback: %s", fallback_query[:80])
    except Exception as e:
        logger.warning("query_enhance failed, using fallback: %s", e)

    return {"enhanced_query": fallback_query}


async def select_tables(state: SQLReactState, config=None) -> dict:
    """表选择：从 MySQL t_semantic_model 加载表名+描述 → LLM 精选。

    不再依赖 Milvus 向量检索，直接从统一语义模型获取表元数据。
    """
    query = state.get("enhanced_query") or state.get("rewritten_query") or state.get("query", "")

    # Stage 1: 从 MySQL 加载全量表名 + 描述
    metadata_list = []
    callbacks = callbacks_from_config(config)
    try:
        metadata_list = await asyncio.wait_for(
            asyncio.to_thread(
                traced_tool_call,
                "schema.load_full_table_metadata",
                query,
                callbacks,
                load_full_table_metadata,
                {"storage": "redis_mysql", "node": "select_tables"},
            ),
            timeout=settings.resilience.milvus_timeout,
        )
    except Exception as e:
        logger.warning("Failed to load table metadata: %s", e)

    if not metadata_list:
        return {"selected_tables": []}

    candidate_tables = [m["table_name"] for m in metadata_list]
    table_metadata = {m["table_name"]: m.get("table_comment", "") for m in metadata_list}
    recall_context = _recall_context_for_query(state, query)
    evidence_tables = _filter_candidate_tables(
        [
            *recall_context.get("business_related_tables", []),
            *recall_context.get("few_shot_related_tables", []),
        ],
        candidate_tables,
    )
    semantic_model = {}
    try:
        semantic_model = await asyncio.wait_for(
            asyncio.to_thread(
                traced_tool_call,
                "schema.get_semantic_model_for_table_ranking",
                ",".join(candidate_tables),
                callbacks,
                lambda: get_semantic_model_by_tables(candidate_tables),
                {"storage": "redis_mysql", "node": "select_tables"},
            ),
            timeout=settings.resilience.milvus_timeout,
        )
    except Exception as e:
        logger.warning("Failed to load semantic model for table ranking: %s", e)

    # Stage 2: 候选少于等于 3 个，直接使用（省一次 LLM 调用）
    if len(candidate_tables) <= 3:
        selected = _merge_selected_tables(candidate_tables, evidence_tables)
        selected = _expand_selected_tables_by_semantic_relationships(
            selected,
            candidate_tables,
            semantic_model,
            query=query,
            table_metadata=table_metadata,
            recall_context=recall_context,
        )
        selected = _rerank_selected_tables(
            selected,
            query,
            table_metadata,
            semantic_model,
            evidence_tables,
            recall_context=recall_context,
        )
        relationships = []
        try:
            relationships = await asyncio.wait_for(
                asyncio.to_thread(
                    traced_tool_call,
                    "schema.get_table_relationships",
                    ",".join(selected),
                    callbacks,
                    lambda: get_table_relationships(selected),
                    {"storage": "redis_mysql", "node": "select_tables"},
                ),
                timeout=settings.resilience.milvus_timeout,
            )
        except Exception as e:
            logger.warning("Failed to load table relationships: %s", e)
        logger.info("select_tables: %d candidates, using directly: %s, %d relationships",
                    len(candidate_tables), selected, len(relationships))
        selected_semantic_model = {
            table: semantic_model.get(table, {})
            for table in selected
            if table in semantic_model
        }
        return {
            "selected_tables": selected,
            "table_relationships": relationships,
            "table_metadata": table_metadata,
            "semantic_model": selected_semantic_model,
        }

    # Stage 2: 候选 > 3 个，LLM 精选
    model = get_chat_model(settings.chat_model_type)

    names_text = _build_table_routing_profiles(
        candidate_tables,
        table_metadata,
        semantic_model,
        query,
        recall_context=recall_context,
    )

    response = await model.ainvoke(
        [
            SystemMessage(content=f"""你是一个数据库专家。根据用户的问题，从候选表名中选出需要用到的表。

候选表路由画像（只包含表说明和与当前问题匹配的少量字段提示，不是完整 schema）:
{names_text}

要求：
1. 只返回需要用到的表名，用逗号分隔
2. 如果需要多表关联，选出所有涉及的表
3. 如果问题与数据库无关（如闲聊），返回空
4. 字段提示只用于判断表是否相关，生成 SQL 前会按已选表加载完整 schema
5. 只返回表名，不要其他内容"""),
            HumanMessage(content=query),
        ],
        config=child_trace_config(config, "sql.select_tables.llm", tags=["llm", "sql_react"]),
    )

    raw = response.content.strip()
    if not raw:
        selected = candidate_tables
    else:
        selected = [n.strip() for n in raw.split(",") if n.strip() in candidate_tables]
        if not selected:
            selected = candidate_tables
    selected = _merge_selected_tables(selected, evidence_tables)
    selected = _expand_selected_tables_by_semantic_relationships(
        selected,
        candidate_tables,
        semantic_model,
        query=query,
        table_metadata=table_metadata,
        recall_context=recall_context,
    )
    selected = _rerank_selected_tables(
        selected,
        query,
        table_metadata,
        semantic_model,
        evidence_tables,
        recall_context=recall_context,
    )

    relationships = []
    try:
        relationships = await asyncio.wait_for(
            asyncio.to_thread(
                traced_tool_call,
                "schema.get_table_relationships",
                ",".join(selected),
                callbacks,
                lambda: get_table_relationships(selected),
                {"storage": "redis_mysql", "node": "select_tables"},
            ),
            timeout=settings.resilience.milvus_timeout,
        )
    except Exception as e:
        logger.warning("Failed to load table relationships: %s", e)

    logger.info("select_tables: LLM selected %d from %d candidates: %s, %d relationships",
                len(selected), len(candidate_tables), selected, len(relationships))
    selected_semantic_model = {
        table: semantic_model.get(table, {})
        for table in selected
        if table in semantic_model
    }
    return {
        "selected_tables": selected,
        "table_relationships": relationships,
        "table_metadata": table_metadata,
        "semantic_model": selected_semantic_model,
    }


async def authorize_selected_tables(state: SQLReactState, config=None) -> dict:
    """Block the SQL path when selected tables require unavailable permissions."""
    selected = state.get("selected_tables", [])
    report = authorize_tables(
        selected,
        state.get("security_context"),
        table_metadata=state.get("table_metadata", {}),
        stage="selected_tables",
    )
    if report.allowed:
        return {"authorization_report": report.to_dict()}
    _write_permission_denied_audit(state, report, event_type="table_permission_denied")
    return {
        "authorization_report": report.to_dict(),
        "answer": report.message,
        "is_sql": False,
    }


def _write_permission_denied_audit(state: SQLReactState, report, *, event_type: str) -> None:
    write_audit_log(build_audit_event(
        event_type,
        query=state.get("query", ""),
        context=state.get("security_context"),
        selected_tables=state.get("selected_tables", []),
        denied_tables=report.denied_tables,
        display_tables=report.display_denied_tables,
        status="denied",
        error="permission_denied",
        extra={"stage": report.stage},
    ))


async def assess_feasibility(state: SQLReactState, config=None) -> dict:
    """Assess SQL execution mode from DB rules and selected schema structure."""
    query = state.get("enhanced_query") or state.get("rewritten_query") or state.get("query", "")
    selected_tables = state.get("selected_tables", [])
    rule_decision = await evaluate_query_route_rules(query)
    task_type = ""
    decision_source = "default"
    report = dict(state.get("complexity_report") or {})
    if rule_decision and rule_decision.confidence >= 0.8:
        task_type = rule_decision.route_signal
        decision_source = "rules"
        report["route_rule"] = rule_decision.to_dict()
    if not task_type:
        inferred_task_type, inferred_report = _infer_task_type_from_recall_context(
            state,
            query,
            selected_tables,
        )
        if inferred_task_type:
            task_type = inferred_task_type
            decision_source = "recall_context"
            report["recall_route_signal"] = inferred_report

    decision = assess_query_feasibility(
        query=query,
        selected_tables=selected_tables,
        relationships=state.get("table_relationships", []),
        task_type=task_type or None,
        decision_source=decision_source,
    )
    feasibility_decision = {
        "execution_mode": decision.execution_mode,
        "task_type": decision.task_type,
        "can_single_sql": decision.can_single_sql,
        "can_decompose": decision.can_decompose,
        "needs_clarification": decision.needs_clarification,
        "join_risk": decision.join_risk,
        "decision_source": decision.decision_source,
        "reason": decision.reason,
        "selected_tables_count": decision.selected_tables_count,
        "relationship_count": decision.relationship_count,
        "estimated_join_count": decision.estimated_join_count,
    }
    report.update(feasibility_decision)
    answer = ""
    if decision.execution_mode == "clarify":
        answer = (
            "这个问题涉及的表和业务范围较大。请缩小查询范围，例如指定时间、部门、指标，"
            "或说明你希望查看汇总分析还是明细列表。"
        )
    return {
        "route_mode": decision.execution_mode,
        "route_reason": decision.reason,
        "feasibility_decision": feasibility_decision,
        "complexity_report": report,
        "answer": answer,
        "is_sql": False if decision.execution_mode == "clarify" else state.get("is_sql", False),
    }


def _format_complex_plan_preview(plan: dict) -> str:
    steps = plan.get("steps") or []
    lines = ["检测到复杂多表分析问题，已生成执行计划，请确认是否按计划执行："]
    for item in steps:
        step_no = item.get("step")
        step_type = item.get("type", "")
        goal = item.get("goal", "")
        tables = item.get("tables") or []
        suffix = f"（{step_type}"
        if tables:
            suffix += f": {', '.join(tables)}"
        suffix += "）"
        lines.append(f"{step_no}. {goal}{suffix}")
    return "\n".join(lines)


def _normalize_complex_plan_tables(plan: dict, selected_tables: list[str], relationships: list[dict]) -> dict:
    """Expand each SQL step table list with available relationship path tables."""
    if not isinstance(plan, dict):
        return plan
    normalized = {**plan}
    steps = []
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            steps.append(step)
            continue
        item = {**step}
        if item.get("type") == "sql":
            item["tables"] = _expand_step_tables_by_relationship_paths(
                item.get("tables") or [],
                selected_tables,
                relationships,
            )
        steps.append(item)
    normalized["steps"] = steps
    return normalized


def _fallback_merge_keys(relationships: list[dict]) -> list[str]:
    candidates = []
    for rel in relationships or []:
        for key in ("from_column", "to_column"):
            column = str(rel.get(key) or "").strip()
            normalized = _normalize_merge_name(column)
            if not column or normalized in {"id"}:
                continue
            if normalized.endswith(("id", "code", "编码", "代码")) or any(token in normalized for token in ("period", "date", "year", "month", "期间", "日期", "年度", "月份")):
                candidates.append(column)

    preferred_markers = tuple(_normalize_merge_name(item) for item in (
        "department", "dept", "cost_center", "period", "month", "year", "project", "customer", "account"
    ))
    low_value_markers = tuple(_normalize_merge_name(item) for item in (
        "entry", "journal", "item", "invoice", "claim", "asset"
    ))

    preferred = [
        column for column in _unique_ordered(candidates)
        if any(marker and marker in _normalize_merge_name(column) for marker in preferred_markers)
        and not any(marker and marker in _normalize_merge_name(column) for marker in low_value_markers)
    ]
    if preferred:
        return preferred[:2]
    unique_candidates = _unique_ordered(candidates)
    return unique_candidates[:2] if unique_candidates else ["period"]


def _should_use_complex_plan_fallback(state: SQLReactState) -> bool:
    decision = state.get("feasibility_decision") or {}
    if decision.get("execution_mode") == "complex_plan" or decision.get("task_type") in {"analysis", "report", "comparison"}:
        return True
    report = state.get("complexity_report") or {}
    return report.get("execution_mode") == "complex_plan" or report.get("task_type") in {"analysis", "report", "comparison"}


def _should_run_agentscope_complex_prepass(state: SQLReactState) -> bool:
    explicit = state.get("agentscope_prepass_enabled")
    if explicit is not None:
        return str(explicit).strip().lower() in {"1", "true", "yes", "y", "on"}
    return os.getenv("AGENTSCOPE_COMPLEX_PREPASS_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def _fallback_complex_plan(state: SQLReactState, selected_tables: list[str], relationships: list[dict]) -> dict:
    if not selected_tables or not _should_use_complex_plan_fallback(state):
        return {}
    tables = list(dict.fromkeys(selected_tables))
    merge_keys = _fallback_merge_keys(relationships)
    return {
        "mode": "complex_plan",
        "reason": "planner fallback for decomposable analysis",
        "steps": [
            {
                "step": 1,
                "type": "sql",
                "goal": "汇总用户问题中的实际发生类指标，输出可用于后续合并的共同维度",
                "tables": tables,
                "depends_on": [],
                "merge_keys": merge_keys,
            },
            {
                "step": 2,
                "type": "sql",
                "goal": "汇总用户问题中的计划、预算或目标类指标，输出同名合并维度",
                "tables": tables,
                "depends_on": [],
                "merge_keys": merge_keys,
            },
            {
                "step": 3,
                "type": "sql",
                "goal": "汇总用户问题中的回收、结算或效率类关联指标，输出同名合并维度",
                "tables": tables,
                "depends_on": [],
                "merge_keys": merge_keys,
            },
            {
                "step": 4,
                "type": "python_merge",
                "goal": "按共同维度合并多类指标并保留未对齐记录",
                "tables": [],
                "depends_on": [1, 2, 3],
                "merge_keys": merge_keys,
            },
            {
                "step": 5,
                "type": "report",
                "goal": "基于合并结果输出多指标关系分析结论",
                "tables": [],
                "depends_on": [4],
                "merge_keys": [],
            },
        ],
        "requires_user_confirmation": True,
    }


def _config_thread_id(config) -> str:
    if not config or not isinstance(config, dict):
        return ""
    configurable = config.get("configurable") or {}
    if not isinstance(configurable, dict):
        return ""
    return str(configurable.get("thread_id") or "")


def _agentscope_callbacks_for_runtime(config) -> list:
    callbacks = callbacks_from_config(config)
    if not callbacks:
        return []
    if isinstance(callbacks, (list, tuple)):
        return list(callbacks)
    return [callbacks]


async def _run_agentscope_complex_analysis_prepass(state: SQLReactState, config=None) -> tuple[dict, dict]:
    query = state.get("enhanced_query") or state.get("rewritten_query") or state.get("query", "")
    thread_id = _config_thread_id(config) or str(state.get("session_id") or "")
    runtime = AgentScopeRuntime(
        runner=create_agentscope_runner(),
        callbacks=_agentscope_callbacks_for_runtime(config),
    )
    result = await runtime.run(
        task_type="complex_analysis",
        query=query,
        session_id=str(state.get("session_id") or thread_id or ""),
        security_context=state.get("security_context", {}),
        workflow_state={
            "thread_id": thread_id or str(state.get("session_id") or ""),
            "session_id": str(state.get("session_id") or thread_id or ""),
            "query": query,
            "rewritten_query": state.get("rewritten_query") or "",
            "enhanced_query": state.get("enhanced_query") or "",
            "selected_tables": state.get("selected_tables", []),
            "table_relationships": state.get("table_relationships", []),
            "semantic_model": state.get("semantic_model") or {},
            "evidence": state.get("evidence", []),
            "few_shot_examples": state.get("few_shot_examples", []),
            "recall_context": state.get("recall_context") or {},
            "feasibility_decision": state.get("feasibility_decision") or {},
            "complexity_report": state.get("complexity_report") or {},
            "security_context": state.get("security_context") or {},
        },
    )
    observation = {
        "task_type": "complex_analysis",
        "status": "error" if any(str(flag.get("severity", "")).lower() == "error" for flag in result.risk_flags) else "completed",
        "backend": str(result.state_patch.get("agentscope_backend") or "unknown"),
        "thread_id": thread_id or str(state.get("session_id") or ""),
        "session_id": str(state.get("session_id") or thread_id or ""),
        "tool_trace_count": len(result.tool_trace),
        "sql_draft_count": len(result.sql_drafts),
        "risk_codes": [str(flag.get("code") or "") for flag in result.risk_flags if str(flag.get("code") or "")],
    }
    return result.to_dict(), observation


async def complex_plan_generate(state: SQLReactState, config=None) -> dict:
    """Generate and validate a complex query plan without executing it."""
    query = state.get("enhanced_query") or state.get("rewritten_query") or state.get("query", "")
    selected_tables = state.get("selected_tables", [])
    relationships = state.get("table_relationships", [])
    evidence = state.get("evidence", [])
    agentscope_result = {}
    agentscope_observation = {}

    if _should_run_agentscope_complex_prepass(state):
        try:
            agentscope_result, agentscope_observation = await _run_agentscope_complex_analysis_prepass(state, config)
        except Exception as exc:
            logger.warning("AgentScope complex analysis prepass failed: %s", exc, exc_info=True)
            agentscope_observation = {
                "task_type": "complex_analysis",
                "status": "error",
                "backend": "unavailable",
                "error": str(exc),
                "thread_id": _config_thread_id(config) or str(state.get("session_id") or ""),
                "session_id": str(state.get("session_id") or _config_thread_id(config) or ""),
            }

    model = get_chat_model(settings.chat_model_type)
    response = await model.ainvoke(
        [
            SystemMessage(content=(
                "你是复杂 NL2SQL 计划生成器。请把用户问题拆成可审计的执行计划，不要生成 SQL。\n"
                "要求：\n"
                "1. 只使用候选表中的表名\n"
                "2. 按业务目标和可稳定合并的公共维度拆分 SQL 步骤，不要按表数量机械拆分\n"
                "3. 每个 SQL 步骤的 tables 必须列全完成该步骤目标所需的分类表、维度表和 JOIN 桥接表；"
                "不要只列事实表或端点表\n"
                "4. 多步骤合并必须给出 merge_keys；依赖 SQL 步骤必须能输出与 merge_keys 同名的列别名\n"
                "5. 如果无法稳定合并，返回 mode=clarify 且 steps 为空\n"
                "6. 只返回 JSON，不要 Markdown\n"
                "JSON 格式：{\"mode\":\"complex_plan|clarify\",\"reason\":\"...\",\"steps\":["
                "{\"step\":1,\"type\":\"sql|python_merge|report\",\"goal\":\"...\","
                "\"tables\":[\"...\"],\"depends_on\":[],\"merge_keys\":[\"...\"]}],"
                "\"requires_user_confirmation\":true}"
            )),
            HumanMessage(content=(
                f"用户问题:\n{query}\n\n"
                f"候选表:\n{', '.join(selected_tables)}\n\n"
                f"表关系:\n{json.dumps(relationships, ensure_ascii=False)}\n\n"
                f"业务证据:\n{chr(10).join(evidence)}"
            )),
        ],
        config=child_trace_config(config, "sql.complex_plan.llm", tags=["llm", "sql_react"]),
    )
    plan = _parse_json_object(_response_text(response))
    plan = _normalize_complex_plan_tables(plan, selected_tables, relationships)
    base_result = {
        "agentscope_result": agentscope_result,
        "agentscope_observation": agentscope_observation,
    }
    if plan.get("mode") == "clarify":
        fallback_plan = _fallback_complex_plan(state, selected_tables, relationships)
        if fallback_plan:
            return {
                **base_result,
                "complex_plan": fallback_plan,
                "plan_validation_error": "",
                "answer": _format_complex_plan_preview(fallback_plan),
                "is_sql": False,
            }
        return {
            **base_result,
            "complex_plan": plan,
            "plan_validation_error": "",
            "answer": plan.get("reason") or "这个问题需要进一步明确查询范围后再执行。",
            "is_sql": False,
        }

    ok, error = validate_complex_plan(plan, allowed_tables=set(selected_tables))
    if not ok:
        fallback_plan = _fallback_complex_plan(state, selected_tables, relationships)
        if fallback_plan:
            fallback_ok, fallback_error = validate_complex_plan(fallback_plan, allowed_tables=set(selected_tables))
            if fallback_ok:
                return {
                    **base_result,
                    "complex_plan": fallback_plan,
                    "plan_validation_error": "",
                    "answer": _format_complex_plan_preview(fallback_plan),
                    "is_sql": False,
                }
            error = f"{error}; fallback failed: {fallback_error}"
        return {
            **base_result,
            "complex_plan": plan,
            "plan_validation_error": error,
            "answer": f"复杂查询计划校验失败：{error}。请缩小查询范围或明确需要分析的指标和维度。",
            "is_sql": False,
        }

    return {
        **base_result,
        "complex_plan": plan,
        "plan_validation_error": "",
        "answer": _format_complex_plan_preview(plan),
        "is_sql": False,
    }


def route_after_complex_plan_generate(state: SQLReactState):
    """Only executable complex plans should enter approval."""
    plan = state.get("complex_plan") or {}
    if state.get("plan_validation_error"):
        return END
    if plan.get("mode") == "clarify":
        return END
    steps = plan.get("steps") or []
    if not steps:
        return END
    return "approve_complex_plan"


async def approve_complex_plan(state: SQLReactState) -> dict:
    """Ask the user to approve a generated complex plan before execution."""
    plan = state.get("complex_plan") or {}
    message = state.get("answer") or _format_complex_plan_preview(plan)
    approved = interrupt({
        "complex_plan": plan,
        "message": message,
        "approval_type": "complex_plan",
    })
    if approved.get("approved"):
        return {
            "plan_approved": True,
            "answer": "复杂查询计划已确认，准备进入分步执行。",
            "is_sql": False,
        }
    return {
        "plan_approved": False,
        "answer": "已取消复杂查询计划执行。",
        "is_sql": False,
    }


def _filter_relationships_for_tables(relationships: list[dict], tables: set[str]) -> list[dict]:
    """Keep only relationships fully inside the current step table set."""
    if not tables:
        return []
    result = []
    for rel in relationships or []:
        from_table = rel.get("from_table")
        to_table = rel.get("to_table")
        if from_table in tables and to_table in tables:
            result.append(rel)
    return result


def _expand_step_tables_by_relationship_paths(
    step_tables: list[str],
    selected_tables: list[str],
    relationships: list[dict],
) -> list[str]:
    """Add bridge tables available in the global selected schema for a SQL step."""
    if len(step_tables) < 2:
        return step_tables
    allowed_tables = selected_tables or step_tables
    return _expand_selected_tables_by_join_paths(
        step_tables,
        allowed_tables,
        relationships or [],
    )


def _short_text(value, limit: int = 500) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _build_complex_step_query(state: SQLReactState, step: dict, dependency_results: dict[str, dict]) -> str:
    """Build a focused query for a single SQL step."""
    original_query = state.get("enhanced_query") or state.get("rewritten_query") or state.get("query", "")
    step_no = step.get("step")
    goal = step.get("goal", "")
    tables = ", ".join(step.get("tables") or [])
    merge_keys = ", ".join(step.get("merge_keys") or [])
    depends_on = step.get("depends_on") or []
    dependency_text = ""
    if depends_on:
        snippets = []
        for dep in depends_on:
            dep_entry = dependency_results.get(str(dep)) or {}
            if dep_entry:
                snippets.append(f"步骤 {dep}: {_short_text(dep_entry.get('answer') or dep_entry.get('result'), 300)}")
        if snippets:
            dependency_text = "\n已完成依赖步骤摘要:\n" + "\n".join(snippets)

    return (
        f"整体问题: {original_query}\n"
        f"当前复杂计划 SQL 步骤: {step_no}\n"
        f"当前步骤目标: {goal}\n"
        f"当前步骤可用表: {tables}\n"
        f"后续合并键: {merge_keys or '无'}\n"
        "请只生成完成当前步骤目标所需的 SELECT SQL，不要处理其他计划步骤。"
        "如果存在后续合并键，请在 SELECT 中输出同名别名；无法输出 ID/编码时，至少输出同一业务维度的名称别名。"
        f"{dependency_text}"
    )


def _docs_from_complex_step_context(state: SQLReactState, tables: list[str]) -> list[Document]:
    """Build lightweight schema docs from cached complex-plan context for SQL repair."""
    docs: list[Document] = []
    table_metadata = state.get("table_metadata") or {}
    semantic_model = state.get("semantic_model") or {}
    for table in tables:
        parts = [f"表名: {table}"]
        comment = table_metadata.get(table)
        if comment:
            parts.append(f"表描述: {comment}")
        columns = semantic_model.get(table) or {}
        if isinstance(columns, dict):
            for column_name, meta in columns.items():
                if isinstance(meta, dict):
                    label = meta.get("business_name") or meta.get("column_comment") or ""
                    column_type = meta.get("column_type") or ""
                    synonyms = meta.get("synonyms") or ""
                    description = meta.get("business_description") or ""
                    details = " ".join(str(item) for item in (column_type, label, synonyms, description) if item)
                    parts.append(f"{column_name} {details}".rstrip())
                else:
                    parts.append(str(column_name))
        docs.append(Document(page_content="\n".join(parts), metadata={"table_name": table, "source": "complex_plan_context"}))
    return docs


def _parse_rows_from_sql_result(result) -> list[dict] | None:
    """Parse common MCP SQL result shapes into a list of row dictionaries."""
    value = result
    if isinstance(value, str):
        text = _strip_execution_time(value)
        if not text:
            return None
        try:
            value = json.loads(text)
        except Exception:
            return None

    if isinstance(value, dict):
        for key in ("rows", "data", "result", "items"):
            rows = value.get(key)
            if isinstance(rows, list):
                value = rows
                break
        else:
            value = [value]

    if not isinstance(value, list):
        return None
    rows = [row for row in value if isinstance(row, dict)]
    return rows if len(rows) == len(value) else None


def _normalize_merge_name(name: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(name or "").lower())


def _resolve_merge_key_column(row: dict, merge_key: str) -> str | None:
    if merge_key in row:
        return merge_key

    key_norm = _normalize_merge_name(merge_key)
    if not key_norm:
        return None

    normalized_columns = {
        _normalize_merge_name(column): column
        for column in row
        if _normalize_merge_name(column)
    }
    if key_norm in normalized_columns:
        return normalized_columns[key_norm]

    # LLM-generated SQL often returns descriptive aliases such as
    # department_name for a planned merge key department. Keep this generic and
    # conservative: only use prefix matches for non-trivial key names.
    if len(key_norm) < 4 and not any("\u4e00" <= ch <= "\u9fff" for ch in key_norm):
        return None
    for column_norm, column in normalized_columns.items():
        if column_norm.startswith(key_norm) or key_norm.startswith(column_norm):
            return column
    return None


def _resolve_merge_label_column(row: dict, merge_key: str) -> str | None:
    """Resolve a human-readable sibling column for an id/code merge key.

    Complex plan steps are generated independently. One step may output a
    stable id while another can only output the same business dimension's name.
    This keeps the fallback structural by relying on common column suffixes
    instead of business-specific terms.
    """
    key_norm = _normalize_merge_name(merge_key)
    if not key_norm:
        return None

    stems = []
    for suffix in ("id", "code", "编号", "编码", "代码"):
        if key_norm.endswith(suffix) and len(key_norm) > len(suffix):
            stems.append(key_norm[: -len(suffix)])
    if not stems:
        return None

    normalized_columns = {
        _normalize_merge_name(column): column
        for column in row
        if _normalize_merge_name(column)
    }
    for stem in stems:
        for suffix in ("name", "名称", "名"):
            candidate = f"{stem}{suffix}"
            if candidate in normalized_columns:
                return normalized_columns[candidate]
    return None


def _resolve_canonical_merge_key_column(row: dict, merge_key: str, prefer_label: bool) -> str | None:
    if prefer_label:
        return _resolve_merge_label_column(row, merge_key) or _resolve_merge_key_column(row, merge_key)
    return _resolve_merge_key_column(row, merge_key)


def _merge_dependency_rows(
    step: dict,
    execution_results: dict[str, dict],
) -> tuple[list[dict] | None, str]:
    """Merge dependency SQL rows by merge_keys with a conservative outer merge."""
    merge_keys = step.get("merge_keys") or []
    depends_on = step.get("depends_on") or []
    if not merge_keys:
        return None, "missing merge_keys"
    if not depends_on:
        return None, "missing depends_on"

    rows_by_dep: dict[int | str, list[dict]] = {}
    for dep in depends_on:
        dep_key = str(dep)
        dep_entry = execution_results.get(dep_key) or {}
        if dep_entry.get("error"):
            return None, f"dependency step {dep} failed"
        rows = _parse_rows_from_sql_result(dep_entry.get("result"))
        if rows is None:
            return None, f"dependency step {dep} result is not structured rows"
        rows_by_dep[dep] = rows

    effective_merge_keys = [
        str(merge_key)
        for merge_key in merge_keys
        if any(
            _resolve_merge_key_column(row, str(merge_key)) is not None
            or _resolve_merge_label_column(row, str(merge_key)) is not None
            for rows in rows_by_dep.values()
            for row in rows
        )
    ]
    if not effective_merge_keys:
        return [
            {
                **row,
                "merge_status": "未对齐",
                "source_step": dep,
                "missing_merge_keys": ", ".join(str(key) for key in merge_keys),
            }
            for dep, rows in rows_by_dep.items()
            for row in rows
        ], ""

    prefer_label_key: dict[str, bool] = {}
    for merge_key in effective_merge_keys:
        key = str(merge_key)
        prefer_label_key[key] = any(
            _resolve_merge_key_column(row, key) is None and _resolve_merge_label_column(row, key) is not None
            for rows in rows_by_dep.values()
            for row in rows
        )

    merged: dict[tuple, dict] = {}
    unaligned_rows: list[dict] = []
    for dep, rows in rows_by_dep.items():
        for row in rows:
            key_columns = {
                k: _resolve_canonical_merge_key_column(row, str(k), prefer_label_key.get(str(k), False))
                for k in effective_merge_keys
            }
            key = tuple(row.get(key_columns[k]) if key_columns[k] else None for k in effective_merge_keys)
            if any(v is None for v in key):
                missing_keys = [
                    str(k)
                    for k in effective_merge_keys
                    if key_columns[k] is None or row.get(key_columns[k]) is None
                ]
                unaligned_rows.append({
                    **row,
                    "merge_status": "未对齐",
                    "source_step": dep,
                    "missing_merge_keys": ", ".join(missing_keys),
                })
                continue
            bucket = merged.setdefault(key, {k: row.get(k) for k in effective_merge_keys})
            for merge_key in effective_merge_keys:
                column = key_columns[merge_key]
                if column:
                    bucket[merge_key] = row.get(column)
            for col, value in row.items():
                if col in key_columns.values():
                    continue
                out_col = col
                if out_col in bucket and bucket[out_col] != value:
                    out_col = f"step{dep}_{col}"
                bucket[out_col] = value

    return [*merged.values(), *unaligned_rows], ""


def _dependency_summary(step: dict, execution_results: dict[str, dict]) -> str:
    depends_on = step.get("depends_on") or []
    if not depends_on:
        return "该步骤没有依赖结果可汇总。"
    lines = []
    for dep in depends_on:
        entry = execution_results.get(str(dep)) or {}
        if not entry:
            lines.append(f"步骤 {dep}: 未执行")
            continue
        if entry.get("error"):
            lines.append(f"步骤 {dep}: 失败 - {entry['error']}")
            continue
        lines.append(f"步骤 {dep}: {_short_text(entry.get('answer') or entry.get('result'), 300)}")
    return "\n".join(lines)


def _dependency_structured_rows(
    step: dict,
    execution_results: dict[str, dict],
) -> tuple[list[dict] | None, str]:
    depends_on = step.get("depends_on") or []
    if not depends_on:
        return None, "missing depends_on"

    rows: list[dict] = []
    for dep in depends_on:
        entry = execution_results.get(str(dep)) or {}
        if entry.get("error"):
            return None, f"dependency step {dep} failed"
        parsed = _parse_rows_from_sql_result(entry.get("result"))
        if parsed is None:
            return None, f"dependency step {dep} result is not structured rows"
        if len(depends_on) > 1:
            rows.extend({**row, "source_step": dep} for row in parsed)
        else:
            rows.extend(parsed)
    return rows, ""


def _decimal_value(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _column_matches_metric(column: str, terms: tuple[str, ...]) -> bool:
    if _is_metric_dimension_column(column):
        return False
    normalized = _normalize_merge_name(column)
    if not _metric_column_allowed_for_terms(normalized, terms):
        return False
    return any(term in normalized for term in terms)


def _metric_column_allowed_for_terms(normalized: str, terms: tuple[str, ...]) -> bool:
    term_set = set(terms)
    if "budget" in normalized or "预算" in normalized:
        return bool(term_set & {"budget", "预算"})
    if "balance" in normalized or "余额" in normalized:
        return False
    return True


def _sum_metric_columns(rows: list[dict], terms: tuple[str, ...]) -> Decimal | None:
    total = Decimal("0")
    matched = False
    for row in rows:
        for column, value in row.items():
            if not _column_matches_metric(str(column), terms):
                continue
            amount = _decimal_value(value)
            if amount is None:
                continue
            total += amount
            matched = True
    return total if matched else None


def _format_decimal_amount(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.01'))}"


def _format_decimal_percent(numerator: Decimal, denominator: Decimal) -> str:
    if denominator == 0:
        return "无法计算"
    return f"{(numerator / denominator * Decimal('100')).quantize(Decimal('0.01'))}%"


def _is_metric_dimension_column(column: str) -> bool:
    normalized = _normalize_merge_name(column)
    compact = normalized.replace("_", "")
    if not normalized:
        return True
    if normalized in {"id", "code", "name", "type", "status"}:
        return True
    if normalized.endswith(("id", "code", "name", "type", "status")):
        return True
    dimension_markers = (
        "cost_center",
        "costcenter",
        "department",
        "dept",
        "period",
        "month",
        "year",
        "date",
        "account",
        "center_name",
        "成本中心",
        "部门",
        "期间",
        "月份",
        "年度",
        "日期",
        "科目",
    )
    return any(marker in normalized or marker in compact for marker in dimension_markers)


def _format_complex_business_summary(execution_results: dict[str, dict]) -> str:
    final_rows = None
    for key in sorted(execution_results, key=lambda x: int(x) if str(x).isdigit() else 10**9):
        entry = execution_results.get(key) or {}
        result = entry.get("result")
        if entry.get("type") in {"python_merge", "report"} and isinstance(result, list):
            if all(isinstance(row, dict) for row in result):
                final_rows = result

    if final_rows is None:
        return ""

    unaligned_count = sum(1 for row in final_rows if row.get("merge_status") == "未对齐")
    aligned_count = len(final_rows) - unaligned_count
    income = _sum_metric_columns(final_rows, ("income", "revenue", "收入"))
    cost = _sum_metric_columns(final_rows, ("cost", "成本"))
    expense = _sum_metric_columns(final_rows, ("expense", "费用"))
    budget = _sum_metric_columns(final_rows, ("budget", "预算"))
    actual = _sum_metric_columns(final_rows, ("actual", "实际"))
    collection = _sum_metric_columns(final_rows, ("collection", "settled", "received", "receivable", "回款", "收款", "应收"))

    lines = [
        "经营关系摘要：",
        f"- 合并结果：{aligned_count} 行，未对齐记录：{unaligned_count} 行。",
    ]
    if income is not None:
        lines.append(f"- 收入合计：{_format_decimal_amount(income)}")
    if cost is not None:
        lines.append(f"- 成本合计：{_format_decimal_amount(cost)}")
    if expense is not None:
        lines.append(f"- 费用合计：{_format_decimal_amount(expense)}")
    if budget is not None:
        lines.append(f"- 预算合计：{_format_decimal_amount(budget)}")
    if actual is not None:
        lines.append(f"- 实际执行合计：{_format_decimal_amount(actual)}")
    if collection is not None:
        lines.append(f"- 回款合计：{_format_decimal_amount(collection)}")
    if income is not None and (cost is not None or expense is not None):
        rough_surplus = income - (cost or Decimal("0")) - (expense or Decimal("0"))
        lines.append(f"- 粗略盈余：{_format_decimal_amount(rough_surplus)}")
    if income is not None and income != 0 and collection is not None:
        ratio = (collection / income * Decimal("100")).quantize(Decimal("0.01"))
        lines.append(f"- 回款/收入比：{ratio}%")
    return "\n".join(lines)


def _final_complex_rows(execution_results: dict[str, dict]) -> list[dict] | None:
    final_rows = None
    for key in sorted(execution_results, key=lambda x: int(x) if str(x).isdigit() else 10**9):
        entry = execution_results.get(key) or {}
        result = entry.get("result")
        if entry.get("type") in {"python_merge", "report"} and isinstance(result, list):
            structured_rows = [row for row in result if isinstance(row, dict)]
            if len(structured_rows) == len(result):
                final_rows = structured_rows
    return final_rows


def _row_metric_sum(row: dict, terms: tuple[str, ...]) -> Decimal | None:
    total = Decimal("0")
    matched = False
    for column, value in row.items():
        if not _column_matches_metric(str(column), terms):
            continue
        amount = _decimal_value(value)
        if amount is None:
            continue
        total += amount
        matched = True
    return total if matched else None


def _format_row_identity(row: dict, index: int) -> str:
    labels = []
    for key, label in (
        ("department_name", "部门"),
        ("department", "部门"),
        ("department_id", "部门"),
        ("cost_center_name", "成本中心"),
        ("center_name", "成本中心"),
        ("cost_center_id", "成本中心"),
        ("period", "期间"),
        ("account_name", "科目"),
        ("account_code", "科目"),
    ):
        value = row.get(key)
        if value is None or value == "":
            continue
        rendered = _format_result_value(value)
        item = f"{label}{rendered}"
        if item not in labels:
            labels.append(item)
        if len(labels) >= 3:
            break
    return " / ".join(labels) if labels else f"记录 {index + 1}"


def _top_budget_variance_lines(rows: list[dict], max_rows: int = 3) -> list[str]:
    candidates = []
    for index, row in enumerate(rows):
        actual = _row_metric_sum(row, ("actual", "实际"))
        budget = _row_metric_sum(row, ("budget", "预算"))
        if actual is None or budget is None:
            continue
        variance = actual - budget
        candidates.append((abs(variance), index, actual, budget, variance, row))

    candidates.sort(key=lambda item: item[0], reverse=True)
    lines = []
    for _abs_variance, index, actual, budget, variance, row in candidates[:max_rows]:
        lines.append(
            f"- {_format_row_identity(row, index)}：实际 {_format_decimal_amount(actual)}，"
            f"预算 {_format_decimal_amount(budget)}，差异 {_format_decimal_amount(variance)}"
        )
    return lines


def _has_collection_gap(execution_results: dict[str, dict], collection: Decimal | None) -> bool:
    if collection not in (None, Decimal("0")):
        return False
    saw_collection_scope = False
    for entry in execution_results.values():
        text = f"{entry.get('goal', '')}\n{entry.get('answer', '')}\n{entry.get('result', '')}"
        if any(marker in text for marker in ("回款", "收款", "应收", "settled", "received")):
            saw_collection_scope = True
            if any(marker in text for marker in ("未查询到", "无数据", "空结果", "0 条")):
                return True
    return saw_collection_scope and collection is None


def _format_complex_relationship_answer(
    plan: dict,
    execution_results: dict[str, dict],
) -> str:
    rows = _final_complex_rows(execution_results)
    if not rows:
        return ""

    unaligned_count = sum(1 for row in rows if row.get("merge_status") == "未对齐")
    aligned_count = len(rows) - unaligned_count
    income = _sum_metric_columns(rows, ("income", "revenue", "收入"))
    cost = _sum_metric_columns(rows, ("cost", "成本"))
    expense = _sum_metric_columns(rows, ("expense", "费用"))
    budget = _sum_metric_columns(rows, ("budget", "预算"))
    actual = _sum_metric_columns(rows, ("actual", "实际"))
    collection = _sum_metric_columns(rows, ("collection", "settled", "received", "receivable", "回款", "收款", "应收"))

    steps = plan.get("steps") or []
    total_steps = len(steps) or len(execution_results)
    processed_steps = (
        sum(1 for step in steps if str(step.get("step")) in execution_results)
        if steps else len(execution_results)
    )
    lines = ["关系分析结果："]
    lines.append(f"- 本次按计划完成 {processed_steps}/{total_steps} 个步骤，合并得到 {aligned_count} 条可对齐记录。")
    if unaligned_count:
        lines.append(f"- 另有 {unaligned_count} 条记录缺少合并维度，未纳入关系判断。")

    if actual is not None:
        lines.append(f"- 实际发生合计：{_format_decimal_amount(actual)}。")
    if budget is not None:
        lines.append(f"- 预算合计：{_format_decimal_amount(budget)}。")
    if actual is not None and budget is not None:
        variance = actual - budget
        lines.append(
            f"- 预算差异：{_format_decimal_amount(variance)}，"
            f"预算执行率：{_format_decimal_percent(actual, budget)}。"
        )
    if income is not None:
        lines.append(f"- 收入合计：{_format_decimal_amount(income)}。")
    if cost is not None:
        lines.append(f"- 成本合计：{_format_decimal_amount(cost)}。")
    if expense is not None:
        lines.append(f"- 费用合计：{_format_decimal_amount(expense)}。")
    if collection is not None:
        lines.append(f"- 回款合计：{_format_decimal_amount(collection)}。")
    if income is not None and (cost is not None or expense is not None):
        rough_surplus = income - (cost or Decimal("0")) - (expense or Decimal("0"))
        lines.append(f"- 粗略盈余：{_format_decimal_amount(rough_surplus)}。")

    if _has_collection_gap(execution_results, collection):
        lines.append("- 未查询到可对齐的回款数据，暂不能计算回款效率。")
    elif collection is not None:
        denominator = income if income not in (None, Decimal("0")) else actual
        if denominator not in (None, Decimal("0")):
            lines.append(f"- 回款效率：{_format_decimal_percent(collection, denominator)}。")

    metric_lines = _format_result_rows_for_answer(rows)
    if metric_lines:
        lines.append("结果明细：")
        lines.extend(metric_lines)

    variance_lines = _top_budget_variance_lines(rows)
    if variance_lines:
        lines.append("差异较大的维度：")
        lines.extend(variance_lines)

    lines.append("执行概况：SQL 已通过安全检查、权限检查并执行。")
    return "\n".join(lines)


def _run_local_complex_step(step: dict, execution_results: dict[str, dict]) -> dict:
    """Run python_merge/report steps without another LLM call."""
    step_no = step.get("step")
    step_type = step.get("type")
    missing = [
        dep for dep in step.get("depends_on", [])
        if str(dep) not in execution_results or execution_results.get(str(dep), {}).get("error")
    ]
    if missing:
        return {
            "step": step_no,
            "type": step_type,
            "goal": step.get("goal", ""),
            "depends_on": step.get("depends_on", []),
            "merge_keys": step.get("merge_keys", []),
            "result": None,
            "answer": f"依赖步骤未完成，无法执行本地步骤: {missing}",
            "error": f"missing dependency steps: {missing}",
        }

    if step_type == "python_merge":
        rows, reason = _merge_dependency_rows(step, execution_results)
        if rows is not None:
            return {
                "step": step_no,
                "type": step_type,
                "goal": step.get("goal", ""),
                "depends_on": step.get("depends_on", []),
                "merge_keys": step.get("merge_keys", []),
                "result": rows,
                "answer": f"本地合并完成，共 {len(rows)} 行。",
                "error": None,
            }
        return {
            "step": step_no,
            "type": step_type,
            "goal": step.get("goal", ""),
            "depends_on": step.get("depends_on", []),
            "merge_keys": step.get("merge_keys", []),
            "result": _dependency_summary(step, execution_results),
            "answer": f"未能进行结构化行合并（{reason}），已保留依赖步骤摘要。",
            "error": None,
        }

    if step_type == "report" and step.get("merge_keys"):
        rows, reason = _merge_dependency_rows(step, execution_results)
        if rows is not None:
            return {
                "step": step_no,
                "type": step_type,
                "goal": step.get("goal", ""),
                "depends_on": step.get("depends_on", []),
                "merge_keys": step.get("merge_keys", []),
                "result": rows,
                "answer": f"报告步骤已完成本地合并，共 {len(rows)} 行。",
                "error": None,
            }
        return {
            "step": step_no,
            "type": step_type,
            "goal": step.get("goal", ""),
            "depends_on": step.get("depends_on", []),
            "merge_keys": step.get("merge_keys", []),
                "result": _dependency_summary(step, execution_results),
                "answer": f"报告步骤未能进行结构化行合并（{reason}），已保留依赖步骤摘要。",
                "error": None,
            }

    if step_type == "report":
        rows, reason = _dependency_structured_rows(step, execution_results)
        if rows is not None:
            return {
                "step": step_no,
                "type": step_type,
                "goal": step.get("goal", ""),
                "depends_on": step.get("depends_on", []),
                "merge_keys": step.get("merge_keys", []),
                "result": rows,
                "answer": f"报告步骤已完成本地汇总，共 {len(rows)} 行。",
                "error": None,
            }
        return {
            "step": step_no,
            "type": step_type,
            "goal": step.get("goal", ""),
            "depends_on": step.get("depends_on", []),
            "merge_keys": step.get("merge_keys", []),
            "result": _dependency_summary(step, execution_results),
            "answer": f"报告步骤未能进行结构化汇总（{reason}），已保留依赖步骤摘要。",
            "error": None,
        }

    return {
        "step": step_no,
        "type": step_type,
        "goal": step.get("goal", ""),
        "depends_on": step.get("depends_on", []),
        "merge_keys": step.get("merge_keys", []),
        "result": _dependency_summary(step, execution_results),
        "answer": "报告步骤已基于依赖步骤结果生成摘要。",
        "error": None,
    }


def _format_complex_execution_answer(plan: dict, execution_results: dict[str, dict], failed: bool = False) -> str:
    steps = plan.get("steps") or []
    if not failed:
        relationship_answer = _format_complex_relationship_answer(plan, execution_results)
        if relationship_answer:
            return relationship_answer

    title = "复杂查询计划执行失败。" if failed else "复杂查询计划执行完成。"
    lines = [f"{title}共处理 {len(execution_results)}/{len(steps)} 个步骤："]
    business_summary = _format_complex_business_summary(execution_results)
    if business_summary:
        lines.append(business_summary)
    for step in steps:
        step_no = step.get("step")
        entry = execution_results.get(str(step_no))
        if not entry:
            lines.append(f"{step_no}. {step.get('goal', '')}：未执行")
            continue
        status = "失败" if entry.get("error") else "完成"
        lines.append(f"{step_no}. {entry.get('goal') or step.get('goal', '')}：{status}")
        if entry.get("sql"):
            lines.append(f"   SQL: {_short_text(entry['sql'], 260)}")
        if entry.get("error"):
            display_error = entry.get("error") or entry.get("answer")
            lines.append(f"   错误: {_short_text(display_error, 300)}")
        else:
            lines.append(f"   结果: {_format_complex_entry_result(entry)}")
    return "\n".join(lines)


def _format_row_preview(row: dict, max_columns: int = 8) -> str:
    items = list(row.items())
    rendered = "，".join(
        f"{key}：{_format_result_value(value)}"
        for key, value in items[:max_columns]
    )
    if len(items) > max_columns:
        rendered += "，..."
    return rendered


def _format_structured_rows_preview(rows: list[dict], max_rows: int = 3) -> str:
    if not rows:
        return ""
    preview_rows = [_format_row_preview(row) for row in rows[:max_rows]]
    suffix = f"\n   仅展示前 {max_rows} 行，共 {len(rows)} 行。" if len(rows) > max_rows else ""
    return "\n   合并结果预览:\n   " + "\n   ".join(preview_rows) + suffix


def _format_result_rows_for_answer(rows: list[dict], max_rows: int = 5) -> list[str]:
    if not rows:
        return []
    lines = [f"- 查询结果：共 {len(rows)} 条记录。"]
    for row in rows[:max_rows]:
        lines.append(f"- {_format_row_preview(row)}")
    if len(rows) > max_rows:
        lines.append(f"- 仅展示前 {max_rows} 条。")
    return lines


def _format_complex_entry_result(entry: dict) -> str:
    answer = str(entry.get("answer") or "").strip()
    result = entry.get("result")
    preview = ""
    if entry.get("type") in {"python_merge", "report"} and isinstance(result, list):
        structured_rows = [row for row in result if isinstance(row, dict)]
        if len(structured_rows) == len(result):
            preview = _format_structured_rows_preview(structured_rows)
    if answer:
        return _short_text(answer + preview, 700)
    return _short_text(result, 700)


def _result_rows_for_compaction(result):
    if result is None:
        return None
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("rows", "data", "result", "items"):
            value = result.get(key)
            if isinstance(value, list):
                return value
        return [result]
    if isinstance(result, str):
        parsed = _parse_sql_result_rows(result)
        return parsed if isinstance(parsed, list) else None
    return None


def _compact_complex_step_entry(
    entry: dict,
    *,
    max_preview_rows: int = 5,
    max_result_chars: int = 2000,
) -> dict:
    """Return a trace-friendly step entry without duplicating large raw payloads."""
    compacted = {
        key: value
        for key, value in entry.items()
        if key != "execution_history"
    }

    history = entry.get("execution_history")
    if isinstance(history, list) and history:
        compacted["execution_history_count"] = len(history)

    if "result" not in compacted:
        return compacted

    result = compacted.get("result")
    rows = _result_rows_for_compaction(result)
    if rows is not None:
        compacted["result_row_count"] = len(rows)
        if len(rows) > max_preview_rows:
            compacted["result_preview"] = rows[:max_preview_rows]
            compacted["result_truncated"] = True
            compacted.pop("result", None)
            return compacted
        compacted["result_truncated"] = False

    if isinstance(result, str) and len(result) > max_result_chars:
        compacted["result_preview"] = _short_text(result, max_result_chars)
        compacted["result_truncated"] = True
        compacted.pop("result", None)

    return compacted


def _compact_plan_execution_results(execution_results: dict[str, dict]) -> dict[str, dict]:
    return {
        str(key): _compact_complex_step_entry(entry)
        for key, entry in execution_results.items()
    }


def _complex_plan_result_summary(
    plan: dict,
    execution_results: dict[str, dict],
    *,
    failed: bool,
) -> dict:
    steps = plan.get("steps") or []
    failed_steps = [
        str(key)
        for key, entry in execution_results.items()
        if isinstance(entry, dict) and entry.get("error")
    ]
    row_counts = {}
    for key, entry in execution_results.items():
        if not isinstance(entry, dict):
            continue
        rows = _result_rows_for_compaction(entry.get("result"))
        if rows is not None:
            row_counts[str(key)] = len(rows)

    return {
        "plan_summary": {
            "status": "failed" if failed else "completed",
            "processed_steps": len(execution_results),
            "total_steps": len(steps),
            "failed_steps": failed_steps,
            "result_row_counts": row_counts,
        }
    }


def _complex_plan_return_payload(
    plan: dict,
    execution_results: dict[str, dict],
    *,
    current_step: int,
    failed: bool,
    error: str | None,
) -> dict:
    return {
        "answer": _format_complex_execution_answer(plan, execution_results, failed=failed),
        "is_sql": False,
        "sql": _joined_plan_sql(execution_results),
        "result": json.dumps(
            _complex_plan_result_summary(plan, execution_results, failed=failed),
            ensure_ascii=False,
        ),
        "error": error,
        "plan_current_step": current_step,
        "plan_execution_results": _compact_plan_execution_results(execution_results),
    }


def _joined_plan_sql(execution_results: dict[str, dict]) -> str:
    blocks = []
    for key in sorted(execution_results, key=lambda x: int(x) if str(x).isdigit() else 10**9):
        entry = execution_results[key]
        sql = entry.get("sql")
        if sql:
            blocks.append(f"-- Step {key}: {entry.get('goal', '')}\n{sql}")
    return "\n\n".join(blocks)


def _completed_complex_step_keys(execution_results: dict[str, dict]) -> set[str]:
    return {
        str(step_no)
        for step_no, entry in execution_results.items()
        if isinstance(entry, dict) and not entry.get("error")
    }


def _semantic_model_covers_tables(semantic_model: dict, tables: list[str]) -> bool:
    if not isinstance(semantic_model, dict):
        return False
    return all(table in semantic_model for table in tables)


async def _ensure_complex_step_semantic_model(
    state: SQLReactState,
    steps: list[dict],
    config=None,
) -> dict:
    """Load missing semantic metadata once for the current ready step batch."""

    selected_tables = state.get("selected_tables") or []
    relationships = state.get("table_relationships", [])
    expanded_tables: list[str] = []
    for step in steps:
        if step.get("type") != "sql":
            continue
        step_tables = _expand_step_tables_by_relationship_paths(
            step.get("tables") or [],
            selected_tables,
            relationships,
        )
        expanded_tables.extend(step_tables)

    requested = _unique_ordered([str(table).strip() for table in expanded_tables if str(table).strip()])
    if not requested:
        return state.get("semantic_model") or {}

    cached_semantic = state.get("semantic_model") or {}
    if _semantic_model_covers_tables(cached_semantic, requested):
        return cached_semantic

    missing = [table for table in requested if table not in cached_semantic]
    if not missing:
        return cached_semantic

    callbacks = callbacks_from_config(config)
    try:
        loaded = await asyncio.wait_for(
            asyncio.to_thread(
                traced_tool_call,
                "schema.get_semantic_model_for_complex_batch",
                ",".join(missing),
                callbacks,
                lambda: get_semantic_model_by_tables(missing),
                {"storage": "redis_mysql", "node": "execute_complex_plan_step"},
            ),
            timeout=settings.resilience.milvus_timeout,
        )
    except Exception as exc:
        logger.warning("Complex plan semantic batch load failed: %s", exc)
        loaded = {}

    return {
        **{
            table: cached_semantic.get(table, {})
            for table in cached_semantic
        },
        **(loaded or {}),
    }


def _ready_complex_plan_batch(
    steps: list[dict],
    execution_results: dict[str, dict],
) -> list[dict]:
    completed = _completed_complex_step_keys(execution_results)
    ready: list[dict] = []
    for step in steps:
        step_key = str(step.get("step"))
        if step_key in completed or step_key in execution_results:
            continue
        depends_on = [str(dep) for dep in step.get("depends_on", []) or []]
        if all(dep in completed for dep in depends_on):
            ready.append(step)
    return ready


def _max_completed_complex_step(steps: list[dict], execution_results: dict[str, dict]) -> int:
    completed = _completed_complex_step_keys(execution_results)
    current = 0
    for step in steps:
        step_no = step.get("step")
        if str(step_no) not in completed:
            continue
        try:
            current = max(current, int(step_no))
        except (TypeError, ValueError):
            continue
    return current


def _complex_sql_step_entry(
    step_no,
    step: dict,
    tables: list[str],
    *,
    sql: str = "",
    result=None,
    answer: str = "",
    error: str | None = None,
    execution_history: list | None = None,
    **extra,
) -> dict:
    entry = {
        "step": step_no,
        "type": "sql",
        "goal": step.get("goal", ""),
        "tables": tables,
        "sql": sql,
        "result": result,
        "answer": answer,
        "error": error,
    }
    if execution_history is not None:
        entry["execution_history"] = execution_history
    entry.update(extra)
    return entry


async def _run_complex_sql_harness(
    step_state: dict,
    step: dict,
    tables: list[str],
    callbacks=None,
) -> dict:
    step_no = step.get("step")
    if step_state.get("is_sql") and not (step_state.get("sql") or "").strip():
        return _complex_sql_step_entry(
            step_no,
            step,
            tables,
            answer="SQL 生成节点返回了空 SQL，已停止执行当前复杂计划步骤。",
            error="empty generated sql",
        )
    if not step_state.get("is_sql"):
        return _complex_sql_step_entry(
            step_no,
            step,
            tables,
            sql=step_state.get("sql", ""),
            answer=step_state.get("answer", "未生成 SQL"),
            error=step_state.get("error") or step_state.get("answer", "not sql"),
        )

    safety = await traced_async_tool_call(
        "sql.safety_check",
        str(step_state.get("sql", "")),
        callbacks,
        lambda: safety_check(step_state),
        {"step": step_no, "node": "execute_complex_plan_step"},
    )
    if safety.get("is_sql") is False:
        return _complex_sql_step_entry(
            step_no,
            step,
            tables,
            sql=step_state.get("sql", ""),
            answer=safety.get("answer", "SQL 安全检查未通过"),
            error=safety.get("answer", "SQL 安全检查未通过"),
            safety_report=safety.get("safety_report"),
        )
    step_state.update(safety)

    authorization = await traced_async_tool_call(
        "sql.authorize_sql",
        str(step_state.get("sql", "")),
        callbacks,
        lambda: authorize_sql(step_state),
        {"step": step_no, "node": "execute_complex_plan_step"},
    )
    if authorization.get("is_sql") is False:
        return _complex_sql_step_entry(
            step_no,
            step,
            tables,
            sql=step_state.get("sql", ""),
            answer=authorization.get("answer", "SQL 权限检查未通过"),
            error=authorization.get("error") or "permission_denied",
            authorization_report=authorization.get("authorization_report"),
        )
    step_state.update(authorization)

    executed = await traced_async_tool_call(
        "sql.execute_sql",
        str(step_state.get("sql", "")),
        callbacks,
        lambda: execute_sql(step_state),
        {"step": step_no, "node": "execute_complex_plan_step"},
    )
    step_state.update(executed)
    if executed.get("error") is None and executed.get("result") is None and not executed.get("answer"):
        return _complex_sql_step_entry(
            step_no,
            step,
            tables,
            sql=step_state.get("sql", ""),
            answer="SQL 执行节点返回空结果，已停止当前复杂计划步骤。",
            error="empty execution result",
            execution_history=executed.get("execution_history", []),
        )
    if not executed.get("error"):
        return _complex_sql_step_entry(
            step_no,
            step,
            tables,
            sql=step_state.get("sql", ""),
            result=executed.get("result"),
            answer=executed.get("answer", ""),
            error=None,
            execution_history=executed.get("execution_history", []),
        )

    error_msg = str(executed.get("error") or "")
    return _complex_sql_step_entry(
        step_no,
        step,
        tables,
        sql=step_state.get("sql", ""),
        result=executed.get("result"),
        answer=executed.get("answer", ""),
        error=error_msg,
        execution_history=executed.get("execution_history", []),
    )


async def _execute_complex_sql_step(
    state: SQLReactState,
    step: dict,
    execution_results: dict[str, dict],
    config=None,
) -> dict:
    step_no = step.get("step")
    callbacks = callbacks_from_config(config)
    tables = _expand_step_tables_by_relationship_paths(
        step.get("tables") or [],
        state.get("selected_tables") or [],
        state.get("table_relationships", []),
    )
    table_set = set(tables)
    step_for_query = {**step, "tables": tables}
    step_query = _build_complex_step_query(state, step_for_query, execution_results)
    step_state = {
        **state,
        "query": step_query,
        "rewritten_query": step_query,
        "enhanced_query": step_query,
        "selected_tables": tables,
        "table_relationships": _filter_relationships_for_tables(state.get("table_relationships", []), table_set),
        "table_metadata": state.get("table_metadata", {}),
        "semantic_model": {
            table: (state.get("semantic_model") or {}).get(table, {})
            for table in tables
            if table in (state.get("semantic_model") or {})
        },
        "security_context": state.get("security_context", {}),
        "docs": [],
        "sql": "",
        "is_sql": False,
        "answer": "",
        "error": None,
        "execution_history": [],
        "reflection_notice": "",
    }

    try:
        submitted_sql = str(step.get("sql") or "").strip()
        if submitted_sql:
            normalized_sql, sql_ok, sql_error = normalize_sql_answer(submitted_sql)
            if not sql_ok:
                return _complex_sql_step_entry(
                    step_no,
                    step,
                    tables,
                    sql=normalized_sql,
                    answer=f"已提交 SQL 格式不完整或不规范: {sql_error}",
                    error="invalid_submitted_sql",
                )
            step_state.update({
                "sql": normalized_sql,
                "answer": normalized_sql,
                "is_sql": True,
            })
            step_state["docs"] = _docs_from_complex_step_context(state, tables)
            while True:
                result = await _run_complex_sql_harness(step_state, step, tables, callbacks=callbacks)
                error_msg = str(result.get("error") or "")
                if not error_msg:
                    return result
                if not _should_repair_sql_error(error_msg):
                    return result
                if step_state.get("retry_count", 0) >= settings.resilience.max_sql_retries:
                    return result

                step_state.update(result)
                repair = await error_analysis(step_state, config=config)
                step_state.update(repair)
                generation = await sql_generate(step_state, config=config)
                step_state.update(generation)
                if not step_state.get("is_sql") or not step_state.get("sql"):
                    return _complex_sql_step_entry(
                        step_no,
                        step,
                        tables,
                        sql=step_state.get("sql", ""),
                        answer=step_state.get("answer", "已提交 SQL 修复失败"),
                        error="repair_failed",
                        execution_history=step_state.get("execution_history", []),
                    )
            # unreachable
            return _complex_sql_step_entry(
                step_no,
                step,
                tables,
                sql=step_state.get("sql", ""),
                answer=step_state.get("answer", "已提交 SQL 修复失败"),
                error="repair_failed",
            )

        retrieve_update = await sql_retrieve(step_state, config=config)
        step_state.update(retrieve_update)
        docs_check = await check_docs(step_state)
        if docs_check.get("is_sql") is False:
            return _complex_sql_step_entry(
                step_no,
                step,
                tables,
                answer=docs_check.get("answer", ""),
                error=docs_check.get("answer", "missing schema docs"),
            )

        while True:
            generation = await sql_generate(step_state, config=config)
            step_state.update(generation)
            result = await _run_complex_sql_harness(step_state, step, tables, callbacks=callbacks)
            error_msg = str(result.get("error") or "")
            if not error_msg:
                return result
            if not _should_repair_sql_error(error_msg):
                return result
            if step_state.get("retry_count", 0) >= settings.resilience.max_sql_retries:
                return result

            repair = await error_analysis(step_state, config=config)
            step_state.update(repair)
    except Exception as e:
        logger.warning("complex plan step %s failed: %s", step_no, e, exc_info=True)
        return _complex_sql_step_entry(
            step_no,
            step,
            tables,
            sql=step_state.get("sql", ""),
            answer=f"复杂计划步骤 {step_no} 执行失败: {e}",
            error=str(e),
        )


async def execute_complex_plan_step(state: SQLReactState, config=None) -> dict:
    """Execute an approved complex plan with dependency-aware async batches."""
    plan = state.get("complex_plan") or {}
    steps = plan.get("steps") or []
    execution_results = {
        str(key): value for key, value in (state.get("plan_execution_results") or {}).items()
    }
    if not state.get("plan_approved"):
        return {
            "answer": "复杂查询计划尚未确认，无法执行。",
            "is_sql": False,
            "error": "complex_plan_not_approved",
            "plan_execution_results": _compact_plan_execution_results(execution_results),
        }
    if not steps:
        return {
            "answer": "复杂查询计划为空，无法执行。",
            "is_sql": False,
            "error": "empty_complex_plan",
            "plan_execution_results": _compact_plan_execution_results(execution_results),
        }

    current_step = state.get("plan_current_step", 0) or 0
    while len(_completed_complex_step_keys(execution_results)) < len(steps):
        batch = _ready_complex_plan_batch(steps, execution_results)
        if not batch:
            return _complex_plan_return_payload(
                plan,
                execution_results,
                current_step=current_step,
                failed=True,
                error="complex_plan_step_failed",
            )

        sql_steps = [step for step in batch if step.get("type") == "sql"]
        local_steps = [step for step in batch if step.get("type") != "sql"]

        if sql_steps:
            batch_semantic_model = await _ensure_complex_step_semantic_model(
                state,
                sql_steps,
                config=config,
            )
            step_state = {
                **state,
                "semantic_model": batch_semantic_model,
            }
            sql_entries = await asyncio.gather(
                *[
                    _execute_complex_sql_step(step_state, step, execution_results, config=config)
                    for step in sql_steps
                ]
            )
            for step, entry in zip(sql_steps, sql_entries, strict=False):
                execution_results[str(step.get("step"))] = entry

        for step in local_steps:
            execution_results[str(step.get("step"))] = _run_local_complex_step(step, execution_results)

        current_step = _max_completed_complex_step(steps, execution_results)
        failed = [
            entry
            for step in batch
            for entry in [execution_results.get(str(step.get("step"))) or {}]
            if entry.get("error")
        ]
        if failed:
            return _complex_plan_return_payload(
                plan,
                execution_results,
                current_step=current_step,
                failed=True,
                error="complex_plan_step_failed",
            )

    return _complex_plan_return_payload(
        plan,
        execution_results,
        current_step=current_step,
        failed=False,
        error=None,
    )


async def recall_evidence(state: SQLReactState, config=None) -> dict:
    """并行检索业务知识 + 智能体知识库，注入 SQL 生成上下文。"""
    query = state.get("rewritten_query") or state.get("query", "")
    if not query:
        return {"evidence": [], "few_shot_examples": []}
    callbacks = callbacks_from_config(config)

    async def _recall_business():
        try:
            docs = await asyncio.wait_for(
                asyncio.to_thread(recall_business_knowledge, query, 5, callbacks=callbacks),
                timeout=settings.resilience.milvus_timeout,
            )
            result = [d.page_content for d in docs if d.metadata.get("score", 0) > 0.3]
            logger.info("recall_evidence: %d business knowledge entries", len(result))
            return result
        except Exception as e:
            logger.warning("Business knowledge recall failed: %s", e)
            return []

    async def _recall_agent():
        try:
            # 增加 top_k 以确保过滤后仍有足够的 SQL 示例
            docs = await asyncio.wait_for(
                asyncio.to_thread(recall_agent_knowledge, query, 10, callbacks=callbacks),
                timeout=settings.resilience.milvus_timeout,
            )
            result = [d.page_content for d in docs if d.metadata.get("score", 0) > 0.3]
            logger.info("recall_evidence: %d agent knowledge entries", len(result))
            return result
        except Exception as e:
            logger.warning("Agent knowledge recall failed: %s", e)
            return []

    # 并行检索，耗时 = max(单个) 而非 sum
    evidence, few_shot = await asyncio.gather(_recall_business(), _recall_agent())

    return {
        "evidence": evidence,
        "few_shot_examples": few_shot,
        "recall_context": _build_recall_context(query, evidence, few_shot),
    }




def _build_schema_docs_from_semantic(semantic_model: dict) -> list[Document]:
    """从语义模型构建 schema 文档（替代 Milvus 向量检索）。"""
    docs = []
    for table_name, columns in semantic_model.items():
        lines = [f"表名: {table_name}"]
        for col_name, meta in columns.items():
            col_type = meta.get("column_type", "")
            col_comment = meta.get("column_comment", "")
            business_name = meta.get("business_name", "")
            synonyms = meta.get("synonyms", "")
            description = meta.get("business_description", "")
            is_pk = meta.get("is_pk", 0)
            is_fk = meta.get("is_fk", 0)
            ref_table = meta.get("ref_table", "")
            ref_column = meta.get("ref_column", "")

            parts = [col_name]
            if col_type:
                parts.append(col_type)
            if is_pk:
                parts.append("PRIMARY KEY")
            if is_fk and ref_table:
                parts.append(f"REFERENCES {ref_table}({ref_column})")
            if col_comment:
                parts.append(f"-- {col_comment}")
            if business_name:
                parts.append(f"[业务名: {business_name}]")
            if synonyms:
                parts.append(f"[同义词: {synonyms}]")
            if description:
                parts.append(f"[描述: {description}]")

            lines.append(" ".join(parts))

        doc = Document(
            page_content="\n".join(lines),
            metadata={"table_name": table_name, "source": "semantic_model"},
        )
        docs.append(doc)
    return docs


async def sql_retrieve(state: SQLReactState, config=None) -> dict:
    """从 MySQL t_semantic_model 按表名加载完整 schema + 业务映射。"""
    selected = state.get("selected_tables", [])
    tables = selected or state.get("table_names", [])
    result = {}
    callbacks = callbacks_from_config(config)

    # 1. 加载语义模型（包含完整 schema + 业务映射）
    if tables:
        cached_semantic = state.get("semantic_model") or {}
        semantic_tables = set(cached_semantic.keys()) if isinstance(cached_semantic, dict) else set()
        requested_tables = set(tables)
        if semantic_tables and requested_tables.issubset(semantic_tables):
            result["semantic_model"] = {
                table: cached_semantic.get(table, {})
                for table in tables
                if table in cached_semantic
            }
        else:
            missing_tables = [table for table in tables if table not in semantic_tables]
            try:
                loaded_semantic = await asyncio.wait_for(
                    asyncio.to_thread(
                        traced_tool_call,
                        "schema.get_semantic_model_by_tables",
                        ",".join(missing_tables or tables),
                        callbacks,
                        lambda: get_semantic_model_by_tables(missing_tables or tables),
                        {"storage": "redis_mysql", "node": "sql_retrieve"},
                    ),
                    timeout=settings.resilience.milvus_timeout,
                )
                result["semantic_model"] = {
                    **{
                        table: cached_semantic.get(table, {})
                        for table in tables
                        if table in cached_semantic
                    },
                    **loaded_semantic,
                }
            except Exception as e:
                logger.warning("Semantic model load failed: %s", e)
                result["semantic_model"] = {
                    table: cached_semantic.get(table, {})
                    for table in tables
                    if table in cached_semantic
                }
    else:
        result["semantic_model"] = {}

    # 2. 从语义模型构建 schema 文档（不做过滤，靠业务知识+语义模型让 LLM 判断字段）
    if result["semantic_model"]:
        result["docs"] = _build_schema_docs_from_semantic(result["semantic_model"])
    else:
        result["docs"] = []

    return result


async def check_docs(state: SQLReactState) -> dict:
    """检查是否检索到相关表结构。"""
    docs = state.get("docs", [])
    if not docs:
        return {
            "answer": "未找到相关的数据库表结构信息，无法生成 SQL。请先上传数据库表结构文档。",
            "is_sql": False,
        }
    return {}


_MAX_TABLE_SEARCH_ROUNDS = 3
_TABLE_NAME_VALUE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
_TABLE_NAME_IN_CONTENT_RE = re.compile(
    r"(?:^|\n)\s*(?:表名|table(?:\s+name)?)\s*[:：]\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)",
    re.IGNORECASE,
)


def _normalize_missing_tables(value) -> list[str]:
    """Normalize tool-call missing_tables into a deduplicated table-name list."""
    if not value:
        return []

    raw_items: list = []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, (list, tuple, set)):
            raw_items.extend(parsed)
        elif isinstance(parsed, str):
            raw_items.append(parsed)
        else:
            raw_items.extend(re.split(r"[,，;\n]+", text))
    elif isinstance(value, (list, tuple, set)):
        raw_items.extend(value)
    else:
        raw_items.append(value)

    tables: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        table = str(item or "").strip().strip("'\"`[]() ")
        if not table or not _TABLE_NAME_VALUE_RE.match(table):
            continue
        if table not in seen:
            seen.add(table)
            tables.append(table)
    return tables


def _provided_table_names(docs: list) -> set[str]:
    """Extract physical table names already present in schema docs."""
    names: set[str] = set()
    for doc in docs or []:
        metadata = getattr(doc, "metadata", {}) or {}
        for key in ("table_name", "TABLE_NAME"):
            value = str(metadata.get(key) or "").strip()
            if value and _TABLE_NAME_VALUE_RE.match(value):
                names.add(value)

        content = str(getattr(doc, "page_content", "") or "")
        for match in _TABLE_NAME_IN_CONTENT_RE.finditer(content):
            names.add(match.group(1))
    return names


def _filter_unprovided_missing_tables(missing_tables: list[str], docs: list) -> list[str]:
    """Keep only table names that are not already included in provided docs."""
    provided = _provided_table_names(docs)
    if not provided:
        return missing_tables
    provided_lower = {name.lower() for name in provided}
    return [table for table in missing_tables if table.lower() not in provided_lower]


async def _retrieve_missing_tables(missing_tables: list[str], existing_docs: list, callbacks=None) -> list:
    """Re-retrieve schema docs for missing table names from t_semantic_model."""
    semantic = await asyncio.to_thread(
        traced_tool_call,
        "schema.retrieve_missing_tables",
        ",".join(missing_tables),
        callbacks,
        lambda: get_semantic_model_by_tables(missing_tables),
        {"storage": "redis_mysql", "node": "sql_generate"},
    )
    if not semantic:
        return []
    new_docs = _build_schema_docs_from_semantic(semantic)
    existing_names = {d.metadata.get("table_name", "") for d in existing_docs}
    unique_new = [d for d in new_docs if d.metadata.get("table_name", "") not in existing_names]
    return unique_new


def _authorize_missing_tables_or_response(state: SQLReactState, missing_tables: list[str]) -> dict | None:
    report = authorize_tables(
        missing_tables,
        state.get("security_context"),
        table_metadata=state.get("table_metadata", {}),
        stage="missing_tables",
    )
    if report.allowed:
        return None
    _write_permission_denied_audit(state, report, event_type="missing_table_permission_denied")
    return {
        "authorization_report": report.to_dict(),
        "answer": report.message,
        "sql": "",
        "is_sql": False,
        "error": "permission_denied",
    }


def _build_sql_messages(query: str, docs_text: str, refine_context: str, history_context: str, evidence_text: str, few_shot_text: str, relationships_text: str = "") -> list:
    """Build messages for sql_generate LLM call."""
    return [
        SystemMessage(content=f"""你是一个 SQL 专家。根据用户的问题和数据库表结构信息，生成正确的 SQL 查询。

当前日期: {date.today().isoformat()}。遇到相对时间表达时，按当前日期换算为明确的自然年/月/日期范围。

表结构信息:
        {docs_text}{relationships_text}{evidence_text}{few_shot_text}{refine_context}{history_context}

要求：
1. 使用 MySQL 语法
2. 只生成 SELECT 查询（禁止 DROP/DELETE/TRUNCATE 等危险操作）
3. 如果有执行历史和错误信息，请分析错误原因并生成修正后的 SQL
4. 只有当 SQL 必须引用的物理表完全未出现在“表结构信息”中时，才设置 needs_more_tables=true。
   已提供表结构中的表不得再列入 missing_tables；missing_tables 必须是数组，不要返回字符串形式的 JSON。
5. 参考相似问题的 SQL 示例，但要根据实际表结构调整
6. 表结构中已包含字段的业务名称、同义词和描述，生成 SQL 时优先使用物理字段名，但可参考业务信息理解字段含义
7. 使用表关系信息来确定正确的 JOIN 条件
8. 生成盈亏/正负判断时必须区分三种情况：大于 0、小于 0、等于 0；不要用 ELSE 把 0 归为亏损或盈利
9. 当用户问“亏损多少/亏损金额”时，应返回亏损金额：净利润 < 0 时为 ABS(净利润)，否则为 0；不要仅返回名为“净利润”的字段
10. 如果字段/科目辅助信息已在已提供表中，例如已提供表存在 account_type='损益' 或“科目类型”字段，必须直接使用，不要声称缺少辅助信息
11. 如果上下文中提供了上一轮 SQL，且用户是在追问或省略表达，必须沿用上一轮的时间范围、状态过滤、表连接、指标计算口径和排除条件；除非用户明确要求变更口径
12. 不要在同一层 SELECT 中嵌套聚合函数，例如 SUM(CASE WHEN SUM(...) THEN ... END)；需要二次聚合时先在子查询中产出净利润/亏损金额，再在外层 SUM(亏损金额)
13. 外层查询只能引用子查询输出列或子查询别名，不能引用内层表别名（如 a.account_type、ji.debit_amount）
16. CASE 语法必须选一种，不得混用：
    - 简单 CASE：CASE expr WHEN value1 THEN r1 WHEN value2 THEN r2 ELSE r3 END（WHEN 后是字面值，不是布尔条件）
    - 搜索 CASE：CASE WHEN bool_expr1 THEN r1 WHEN bool_expr2 THEN r2 ELSE r3 END（WHEN 后是布尔条件）
    生成盈亏判断时必须用搜索 CASE，正确写法示例：
      CASE WHEN net_profit < 0 THEN ABS(net_profit) ELSE 0 END
    错误写法（禁止）：CASE net_profit WHEN net_profit < 0 THEN ...（不能在简单 CASE 中写布尔条件）
17. 净利润/亏损的正确分层计算模式（当需要先聚合再判断时）：
    SELECT CASE WHEN net_profit < 0 THEN ABS(net_profit) ELSE 0 END AS 亏损金额
    FROM (
        SELECT SUM(credit_amount - debit_amount) AS net_profit
        FROM ...
        WHERE ...
    ) t
14. 当 is_sql=true 时，answer 只能包含 SQL，不要包含解释性文字
15. 使用 sql_format_response 工具输出结果"""),
        HumanMessage(content=query),
    ]


async def sql_generate(state: SQLReactState, config=None) -> dict:
    """LLM 生成 SQL，支持自动补表（最多重试 3 次）。"""
    model = get_chat_model(settings.chat_model_type)
    model_with_tools = model.bind_tools([create_format_tool()])
    callbacks = callbacks_from_config(config)

    # 如果有修改意见，加入上下文
    refine_context = ""
    if state.get("refine_feedback"):
        refine_context = f"\n修改意见: {state['refine_feedback']}"

    # 如果有执行历史（纠错场景），加入上下文
    history_context = ""
    execution_history = state.get("execution_history", [])
    if execution_history:
        history_lines = []
        for i, h in enumerate(execution_history, 1):
            entry = f"第{i}次尝试: SQL={h['sql']}"
            if h.get("error"):
                entry += f"\n  错误: {h['error']}"
            elif h.get("result"):
                entry += f"\n  结果: {h['result'][:200]}"
            history_lines.append(entry)
        history_context = f"\n执行历史:\n" + "\n".join(history_lines)

    prior_sql_contexts = [
        h.get("content", "")
        for h in state.get("chat_history", [])
        if h.get("role") == "system" and h.get("content", "").startswith("[上一轮SQL上下文]")
    ]
    if prior_sql_contexts:
        history_context += "\n\n上一轮SQL上下文（追问时优先沿用口径）:\n" + "\n---\n".join(prior_sql_contexts[-2:])

    # Business knowledge evidence
    evidence = state.get("evidence", [])
    evidence_text = ""
    if evidence:
        evidence_text = "\n\n业务知识:\n" + "\n".join(evidence)

    # Agent knowledge few-shot examples
    few_shot = state.get("few_shot_examples", [])
    few_shot_text = ""
    if few_shot:
        few_shot_text = "\n\n相似问题参考:\n" + "\n---\n".join(few_shot)

    # Semantic model (字段级业务映射) - 已合并到 docs 中，不再单独构建
    # 保留 semantic_model 用于其他用途（如关键词过滤）

    # Table relationships
    relationships = state.get("table_relationships", [])
    relationships_text = ""
    if relationships:
        lines = ["\n\n表关系（外键关联）:"]
        for rel in relationships:
            lines.append(f"  {rel['from_table']}.{rel['from_column']} -> {rel['to_table']}.{rel['to_column']}")
        relationships_text = "\n".join(lines)

    # Accumulate docs across re-retrieval rounds
    all_docs = list(state.get("docs", []))

    # 使用上下文化后的查询生成 SQL，但保留原始查询作为参考
    effective_query = state.get("enhanced_query") or state.get("rewritten_query") or state.get("query", "")
    original_query = state.get("query", "")
    query_for_sql = effective_query
    if effective_query != original_query:
        query_for_sql = f"{effective_query}\n（用户原始问题: {original_query}）"

    for round_idx in range(_MAX_TABLE_SEARCH_ROUNDS):
        docs_text = "\n\n".join([d.page_content for d in all_docs])

        messages = _build_sql_messages(query_for_sql, docs_text, refine_context, history_context, evidence_text, few_shot_text, relationships_text)
        try:
            response = await model_with_tools.ainvoke(
                messages,
                config=child_trace_config(config, "sql.sql_generate.llm", tags=["llm", "sql_react"]),
            )
        except Exception as exc:
            logger.warning("sql_generate llm failed: %s", exc, exc_info=True)
            return {
                "answer": "SQL 生成模型暂时不可用，无法生成可执行 SQL。请稍后重试或切换可用模型配置。",
                "sql": "",
                "is_sql": False,
                "error": "sql_generation_llm_unavailable",
            }

        if not response.tool_calls:
            return {"answer": response.content, "sql": response.content, "is_sql": False, "error": None}

        tool_call = response.tool_calls[0]
        args = tool_call["args"]
        missing = _normalize_missing_tables(args.get("missing_tables", []))
        missing = _filter_unprovided_missing_tables(missing, all_docs)
        needs_more = bool(args.get("needs_more_tables", False)) and bool(missing)

        # If LLM says it has enough tables, return the result
        if not needs_more:
            answer_text = args.get("answer", "")
            is_sql = args.get("is_sql", False)
            if is_sql:
                answer_text, sql_ok, sql_error = normalize_sql_answer(answer_text)
                if not sql_ok:
                    logger.warning("sql_generate: invalid SQL format: %s", sql_error)
                    return {
                        "answer": f"生成的 SQL 格式不完整或不规范: {sql_error}\n{answer_text}",
                        "sql": answer_text,
                        "is_sql": False,
                        "error": "invalid_sql_format",
                    }
            logger.info("sql_generate: produced SQL after %d round(s)", round_idx + 1)
            return {
                "answer": answer_text,
                "sql": answer_text if is_sql else answer_text,
                "is_sql": is_sql,
                "error": None,
            }

        # LLM needs more tables — re-retrieve
        logger.info("sql_generate: round %d, LLM needs tables: %s", round_idx + 1, missing)
        denied_response = _authorize_missing_tables_or_response(state, missing)
        if denied_response:
            return denied_response
        new_docs = await _retrieve_missing_tables(missing, all_docs, callbacks=callbacks)
        if not new_docs:
            logger.info("sql_generate: no new docs found for %s, using what we have", missing)
            answer_text = args.get("answer", "")
            is_sql = args.get("is_sql", False)
            if is_sql:
                answer_text, sql_ok, sql_error = normalize_sql_answer(answer_text)
                if not sql_ok:
                    return {
                        "answer": f"生成的 SQL 格式不完整或不规范: {sql_error}\n{answer_text}",
                        "sql": answer_text,
                        "is_sql": False,
                        "error": "invalid_sql_format",
                    }
            return {
                "answer": answer_text,
                "sql": answer_text if is_sql else answer_text,
                "is_sql": is_sql,
                "error": None,
            }
        all_docs.extend(new_docs)
        logger.info("sql_generate: added %d new docs, total %d", len(new_docs), len(all_docs))

    # Exhausted retries — return what we have
    answer_text = args.get("answer", "")
    is_sql = args.get("is_sql", False)
    if is_sql:
        answer_text, sql_ok, sql_error = normalize_sql_answer(answer_text)
        if not sql_ok:
            return {
                "answer": f"生成的 SQL 格式不完整或不规范: {sql_error}\n{answer_text}",
                "sql": answer_text,
                "is_sql": False,
                "error": "invalid_sql_format",
            }
    return {
        "answer": answer_text,
        "sql": answer_text if is_sql else answer_text,
        "is_sql": is_sql,
        "error": None,
    }


async def safety_check(state: SQLReactState) -> dict:
    """SQL 安全分析。"""
    if not state.get("is_sql"):
        return {"safety_report": None}

    checker = SQLSafetyChecker()
    report = checker.check(state["sql"])

    if not report.is_safe:
        return {
            "safety_report": {
                "risks": report.risks,
                "estimated_rows": report.estimated_rows,
                "required_permissions": report.required_permissions,
            },
            "answer": f"SQL 安全检查未通过: {', '.join(report.risks)}",
            "is_sql": False,
        }

    return {"safety_report": None}


def _tables_from_sql(sql: str) -> list[str]:
    tables: list[str] = []
    seen: set[str] = set()
    for table in _SQL_TABLE_RE.findall(sql or ""):
        if table not in seen:
            seen.add(table)
            tables.append(table)
    return tables


async def authorize_sql(state: SQLReactState, config=None) -> dict:
    """Conservative V1 SQL authorization over referenced tables."""
    if not state.get("is_sql"):
        return {"authorization_report": state.get("authorization_report", {})}
    sql_tables = _tables_from_sql(state.get("sql", ""))
    if not sql_tables:
        sql_tables = state.get("selected_tables", [])
    report = authorize_tables(
        sql_tables,
        state.get("security_context"),
        table_metadata=state.get("table_metadata", {}),
        stage="sql",
    )
    if report.allowed:
        return {"authorization_report": report.to_dict()}
    _write_permission_denied_audit(state, report, event_type="sql_permission_denied")
    return {
        "authorization_report": report.to_dict(),
        "answer": report.message,
        "is_sql": False,
        "error": "permission_denied",
    }


def approve(state: SQLReactState) -> dict:
    """人工审批 SQL。使用 interrupt 暂停图执行，等待用户确认。"""
    is_reflected_sql = bool(state.get("reflection_notice"))
    has_execution_error = any(h.get("error") for h in state.get("execution_history", []))
    message = "请确认是否执行以上 SQL？"
    if is_reflected_sql:
        message = "上次执行结果疑似异常，系统已反思并生成修正后的 SQL。请确认是否执行修正后的 SQL？"
    elif has_execution_error:
        message = "上次 SQL 执行失败，系统已分析错误并生成修正后的 SQL。请确认是否执行修正后的 SQL？"

    result = interrupt({
        "sql": state["sql"],
        "message": message,
        "reflection": is_reflected_sql,
        "approval_type": "sql",
    })

    if result.get("approved"):
        return {"approved": True}

    return {
        "approved": False,
        "answer": result.get("feedback", "SQL 已被拒绝。"),
        "is_sql": False,
    }


async def execute_sql(state: SQLReactState) -> dict:
    """通过 MCP 执行 SQL。捕获错误用于自动纠错。"""
    from agents.tool.sql_tools.mcp_client import execute_sql as mcp_execute

    sql = state["sql"]
    execution_history = list(state.get("execution_history", []))

    try:
        result = await mcp_execute(sql)
        answer = await _summarize_sql_result(state, result)
        execution_history.append({"sql": sql, "result": result, "error": None})
        return {
            "result": result,
            "answer": answer,
            "error": None,
            "execution_history": execution_history,
        }
    except Exception as e:
        error_msg = str(e)
        logger.warning("SQL execution failed (retry %d/%d): %s", state.get("retry_count", 0), settings.resilience.max_sql_retries, error_msg)
        execution_history.append({"sql": sql, "result": None, "error": error_msg})
        return {
            "result": f"SQL 执行失败: {error_msg}",
            "answer": f"SQL 执行失败: {error_msg}",
            "error": error_msg,
            "execution_history": execution_history,
        }


def _result_anomaly_reason(result) -> str | None:
    """Return a reason when an executed SQL result is suspicious."""
    value = result
    text_anomaly_reason = None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "执行结果为空字符串"
        if text.lower() in {"null", "none"}:
            return "执行结果为 NULL"
        if text == "[]":
            return "执行结果为空集"
        lowered = text.lower()
        if "[]" in lowered:
            text_anomaly_reason = "执行结果包含空数组 []"
        if "null" in lowered:
            text_anomaly_reason = "执行结果包含 NULL 值"
        try:
            value = json.loads(text)
        except Exception:
            return text_anomaly_reason

    if value is None:
        return "执行结果为 NULL"

    if isinstance(value, dict):
        for key in ("rows", "data", "result", "items"):
            if key in value:
                nested_reason = _result_anomaly_reason(value.get(key))
                if nested_reason:
                    return nested_reason
        if not value:
            return "执行结果为空对象"

    if isinstance(value, list):
        if not value:
            return "执行结果为空集"
        rows = value
    else:
        rows = [value]

    scalar_values = []
    for row in rows:
        if isinstance(row, dict):
            scalar_values.extend(row.values())
        elif isinstance(row, (list, tuple)):
            scalar_values.extend(row)
        else:
            scalar_values.append(row)

    if scalar_values and all(v is None or v == "" for v in scalar_values):
        return "执行结果所有字段均为 NULL"

    return text_anomaly_reason


def _should_repair_sql_error(error_msg: str) -> bool:
    """Return whether an execution error should go through LLM SQL repair."""
    if not error_msg:
        return False
    lowered = error_msg.lower()
    non_repairable_markers = (
        "access denied",
        "permission denied",
        "权限",
        "认证",
        "密码",
        "connection",
        "connect",
        "timeout",
        "timed out",
    )
    if any(marker in lowered for marker in non_repairable_markers):
        return is_retryable(error_msg)

    repairable_markers = (
        "unknown column",
        "unknown table",
        "doesn't exist",
        "does not exist",
        "syntax",
        "sql syntax",
        "invalid use of group function",
        "aggregate",
        "field list",
        "group by",
        "not in group by",
        "ambiguous",
        "operand should contain",
        "subquery returns",
        "42s02",
        "42s22",
        "42000",
        "1054",
        "1064",
        "1111",
    )
    return is_retryable(error_msg) or any(marker in lowered for marker in repairable_markers)


async def error_analysis(state: SQLReactState, config=None) -> dict:
    """分析 SQL 执行错误，生成修正建议。"""
    model = get_chat_model(settings.chat_model_type)

    docs_text = "\n".join([d.page_content for d in state.get("docs", [])])
    last_error = state.get("error", "")
    last_sql = state.get("sql", "")
    retry_count = state.get("retry_count", 0)

    response = await model.ainvoke(
        [
            SystemMessage(content=f"""你是一个 SQL 调试专家。以下 SQL 执行失败，请分析错误原因并给出修正建议。

表结构信息:
{docs_text}

失败的 SQL:
{last_sql}

错误信息:
{last_error}

请简要分析错误原因（1-2 句话），并给出修正建议。"""),
            HumanMessage(content=f"这是第 {retry_count} 次重试，请分析错误并给出修正建议。"),
        ],
        config=child_trace_config(config, "sql.error_analysis.llm", tags=["llm", "sql_react"]),
    )

    return {
        "refine_feedback": response.content.strip(),
        "retry_count": retry_count + 1,
    }


async def result_reflection(state: SQLReactState, config=None) -> dict:
    """反思执行成功但结果异常的 SQL，直接生成修正后的 SQL。"""
    model = get_chat_model(settings.chat_model_type)

    query = state.get("enhanced_query") or state.get("rewritten_query") or state.get("query", "")
    docs_text = "\n".join([d.page_content for d in state.get("docs", [])])
    result = state.get("result", "")
    last_sql = state.get("sql", "")
    retry_count = state.get("retry_count", 0)
    reason = _result_anomaly_reason(result) or "执行结果疑似异常"

    try:
        response = await model.ainvoke(
            [
                SystemMessage(content=f"""你是一个 NL2SQL 结果校验与 SQL 修复专家。以下 SQL 执行没有报错，但返回结果异常。请反思 SQL 是否错误表达了用户意图，并直接生成修正后的 SQL。

用户问题:
{query}

表结构信息:
{docs_text}

已执行 SQL:
{last_sql}

执行结果:
{result}

异常原因:
{reason}

请直接输出修正后的 SQL，不要解释，不要 Markdown，不要注释。重点检查：
1. 聚合结果为 NULL 时，是否需要用 COALESCE 或调整过滤条件
2. 空结果是否由 HAVING/WHERE 条件过度过滤造成
3. 是否应该返回可解释的指标值或判断结果，而不是把不满足条件的数据过滤掉
4. 不要引入表结构中不存在的字段
5. 只生成 SELECT/WITH 查询"""),
                HumanMessage(content=f"这是第 {retry_count} 次修正，请直接输出修正后的 SQL。"),
            ],
            config=child_trace_config(config, "sql.result_reflection.llm", tags=["llm", "sql_react"]),
        )
        reflected_sql = _response_text(response)
    except Exception as e:
        logger.warning("result_reflection failed: %s", e)
        return {
            "answer": f"SQL 结果反思失败，无法自动修正: {e}",
            "is_sql": False,
            "error": "result_reflection_failed",
            "retry_count": retry_count + 1,
        }

    reflected_sql, sql_ok, sql_error = normalize_sql_answer(reflected_sql)
    if not sql_ok:
        logger.warning("result_reflection returned invalid SQL: %s", sql_error)
        return {
            "answer": f"反思生成的 SQL 不完整或不规范: {sql_error}\n{reflected_sql}",
            "sql": reflected_sql,
            "is_sql": False,
            "error": "invalid_reflected_sql",
            "retry_count": retry_count + 1,
        }

    return {
        "answer": reflected_sql,
        "sql": reflected_sql,
        "is_sql": True,
        "error": None,
        "refine_feedback": "",
        "reflection_notice": f"检测到异常结果：{reason}。已反思并生成修正后的 SQL。",
        "retry_count": retry_count + 1,
    }


def build_sql_react_graph():
    """构建 SQL React 图，支持自动纠错重试。

    流程: recall_evidence → query_enhance → select_tables → sql_retrieve → check_docs → generate → ...
    """
    graph = StateGraph(SQLReactState)

    graph.add_node("recall_evidence", recall_evidence)
    graph.add_node("query_enhance", query_enhance)
    graph.add_node("select_tables", select_tables)
    graph.add_node("authorize_selected_tables", authorize_selected_tables)
    graph.add_node("assess_feasibility", assess_feasibility)
    graph.add_node("sql_retrieve", sql_retrieve)
    graph.add_node("check_docs", check_docs)
    graph.add_node("sql_generate", sql_generate)
    graph.add_node("complex_plan_generate", complex_plan_generate)
    graph.add_node("approve_complex_plan", approve_complex_plan)
    graph.add_node("execute_complex_plan_step", execute_complex_plan_step)
    graph.add_node("safety_check", safety_check)
    graph.add_node("authorize_sql", authorize_sql)
    graph.add_node("approve", approve)
    graph.add_node("execute_sql", execute_sql)
    graph.add_node("error_analysis", error_analysis)
    graph.add_node("result_reflection", result_reflection)

    graph.add_edge(START, "recall_evidence")
    graph.add_edge("recall_evidence", "query_enhance")
    graph.add_edge("query_enhance", "select_tables")
    graph.add_edge("select_tables", "authorize_selected_tables")

    def route_after_table_authorization(state: SQLReactState) -> str:
        if state.get("is_sql") is False and state.get("answer"):
            return END
        return "assess_feasibility"

    graph.add_conditional_edges("authorize_selected_tables", route_after_table_authorization)

    def route_after_complexity(state: SQLReactState) -> str:
        if state.get("route_mode") == "clarify":
            return END
        if state.get("route_mode") == "complex_plan":
            return "complex_plan_generate"
        return "sql_retrieve"

    graph.add_conditional_edges("assess_feasibility", route_after_complexity)
    graph.add_conditional_edges("complex_plan_generate", route_after_complex_plan_generate)

    def route_after_complex_plan_approve(state: SQLReactState) -> str:
        if state.get("plan_approved"):
            return "execute_complex_plan_step"
        return END

    graph.add_conditional_edges("approve_complex_plan", route_after_complex_plan_approve)
    graph.add_edge("execute_complex_plan_step", END)
    graph.add_edge("sql_retrieve", "check_docs")

    def route_after_check(state: SQLReactState) -> str:
        if state.get("is_sql") is False and state.get("answer"):
            return END
        return "sql_generate"

    graph.add_conditional_edges("check_docs", route_after_check)
    graph.add_edge("sql_generate", "safety_check")

    def route_after_safety(state: SQLReactState) -> str:
        if state.get("is_sql"):
            return "authorize_sql"
        return END

    graph.add_conditional_edges("safety_check", route_after_safety)

    def route_after_sql_authorization(state: SQLReactState) -> str:
        if state.get("is_sql"):
            return "approve"
        return END

    graph.add_conditional_edges("authorize_sql", route_after_sql_authorization)

    def route_after_approve(state: SQLReactState) -> str:
        if state.get("approved"):
            return "execute_sql"
        return END

    graph.add_conditional_edges("approve", route_after_approve)

    def route_after_execute(state: SQLReactState) -> str:
        if not state.get("error"):
            reason = _result_anomaly_reason(state.get("result"))
            max_retries = settings.resilience.max_sql_retries
            if reason and state.get("retry_count", 0) < max_retries:
                logger.info("route_after_execute: suspicious result, retrying via reflection: %s", reason)
                return "result_reflection"
            return END
        # SQL 生成类错误进入 LLM 修复；权限/认证等不可由 SQL 改写修复的错误直接结束。
        if not _should_repair_sql_error(state["error"]):
            logger.info("route_after_execute: non-retryable error, ending: %s", state["error"][:200])
            return END
        # 可重试错误：检查次数
        max_retries = settings.resilience.max_sql_retries
        if state.get("retry_count", 0) < max_retries:
            return "error_analysis"
        # 超过最大重试次数
        logger.warning("route_after_execute: max retries (%d) reached", max_retries)
        return END

    graph.add_conditional_edges("execute_sql", route_after_execute)
    graph.add_edge("error_analysis", "sql_generate")
    graph.add_edge("result_reflection", "safety_check")

    checkpointer = get_checkpointer()
    return graph.compile(checkpointer=checkpointer)
