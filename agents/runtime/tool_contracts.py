"""Serializable contracts and runtime tool descriptors for AgentScope."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable


JsonDict = dict[str, Any]
ToolHandler = Callable[..., Any | Awaitable[Any]]


@dataclass(frozen=True)
class ToolContract:
    """Static contract for a tool exposed by the runtime."""

    name: str
    description: str
    input_schema: JsonDict
    output_contract: JsonDict
    read_only: bool = True
    direct_execution_allowed: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_contract": self.output_contract,
            "read_only": self.read_only,
            "direct_execution_allowed": self.direct_execution_allowed,
        }


@dataclass(frozen=True)
class ToolTrace:
    """Structured execution trace for a single tool call."""

    tool_name: str
    task_type: str
    session_id: str = ""
    thread_id: str = ""
    user_id: str = ""
    status: str = ""
    elapsed_ms: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    input: JsonDict = field(default_factory=dict)
    output: Any = None
    error: str = ""

    def to_dict(self) -> JsonDict:
        return {
            "tool_name": self.tool_name,
            "task_type": self.task_type,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "started_at": self.started_at.isoformat() if self.started_at else "",
            "ended_at": self.ended_at.isoformat() if self.ended_at else "",
            "input": self.input,
            "output": self.output,
            "error": self.error,
        }


@dataclass(frozen=True)
class ToolCallResult:
    """Result returned by ToolCatalog.invoke."""

    ok: bool
    output: Any
    error: str
    trace: ToolTrace

    def to_dict(self) -> JsonDict:
        return {
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "trace": self.trace.to_dict(),
        }


@dataclass(frozen=True)
class RuntimeTool:
    """A tool descriptor returned to runtime consumers."""

    contract: ToolContract
    task_types: tuple[str, ...] = ()
    handler: ToolHandler | None = None

    @property
    def name(self) -> str:
        return self.contract.name

    @property
    def description(self) -> str:
        return self.contract.description

    @property
    def input_schema(self) -> JsonDict:
        return self.contract.input_schema

    @property
    def output_contract(self) -> JsonDict:
        return self.contract.output_contract

    @property
    def read_only(self) -> bool:
        return self.contract.read_only

    @property
    def direct_execution_allowed(self) -> bool:
        return self.contract.direct_execution_allowed

    def to_dict(self) -> JsonDict:
        data = self.contract.to_dict()
        data["task_types"] = list(self.task_types)
        return data

