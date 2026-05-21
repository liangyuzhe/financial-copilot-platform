"""Structured result contract for AgentScope runtime executions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agents.runtime.tool_contracts import ToolTrace


JsonDict = dict[str, Any]


@dataclass(slots=True)
class AgentRunResult:
    """Platform-facing output from an agentic analysis run."""

    answer: str = ""
    tool_trace: list[JsonDict] = field(default_factory=list)
    sql_drafts: list[JsonDict] = field(default_factory=list)
    artifacts: list[JsonDict] = field(default_factory=list)
    clarification_questions: list[str] = field(default_factory=list)
    risk_flags: list[JsonDict] = field(default_factory=list)
    state_patch: JsonDict = field(default_factory=dict)
    events: list[JsonDict] = field(default_factory=list)

    @classmethod
    def from_value(cls, value: Any) -> "AgentRunResult":
        """Normalize runner output into the platform result contract."""

        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if isinstance(value, str):
            return cls(answer=value)
        if isinstance(value, dict):
            return cls(
                answer=str(value.get("answer", "") or ""),
                tool_trace=_list_of_dicts(value.get("tool_trace") or value.get("tool_traces")),
                sql_drafts=_list_of_dicts(value.get("sql_drafts")),
                artifacts=_list_of_dicts(value.get("artifacts")),
                clarification_questions=[
                    str(item) for item in value.get("clarification_questions", []) or []
                ],
                risk_flags=_list_of_dicts(value.get("risk_flags")),
                state_patch=dict(value.get("state_patch") or {}),
                events=_list_of_dicts(value.get("events")),
            )
        return cls(answer=str(value))

    def to_dict(self) -> JsonDict:
        return {
            "answer": self.answer,
            "tool_trace": [dict(item) for item in self.tool_trace],
            "sql_drafts": [dict(item) for item in self.sql_drafts],
            "artifacts": [dict(item) for item in self.artifacts],
            "clarification_questions": list(self.clarification_questions),
            "risk_flags": [dict(item) for item in self.risk_flags],
            "state_patch": dict(self.state_patch),
            "events": [dict(item) for item in self.events],
        }

    def to_sse_events(self, *, include_done: bool = True) -> list[JsonDict]:
        """Adapt the structured result into API-layer SSE event dictionaries."""

        events = [_normalize_event(event) for event in self.events]
        emitted_trace_payloads = {
            event["data"] for event in events if event.get("event") == "tool_trace"
        }
        for trace in self.tool_trace:
            data = json.dumps(trace, ensure_ascii=False, default=str)
            if data in emitted_trace_payloads:
                continue
            events.append({"event": "tool_trace", "data": data})

        events.append(
            {
                "event": "result",
                "data": json.dumps(self.to_dict(), ensure_ascii=False, default=str),
            }
        )
        if include_done:
            events.append({"event": "done", "data": "[DONE]"})
        return events


def _list_of_dicts(value: Any) -> list[JsonDict]:
    if not value:
        return []
    rows: list[JsonDict] = []
    for item in value:
        if isinstance(item, ToolTrace):
            rows.append(item.to_dict())
        elif hasattr(item, "to_dict") and callable(item.to_dict):
            rows.append(dict(item.to_dict()))
        elif isinstance(item, dict):
            rows.append(dict(item))
        else:
            rows.append({"value": item})
    return rows


def _normalize_event(event: JsonDict) -> JsonDict:
    name = str(event.get("event", "message") or "message")
    data = event.get("data", "")
    if isinstance(data, str):
        return {"event": name, "data": data}
    return {"event": name, "data": json.dumps(data, ensure_ascii=False, default=str)}
