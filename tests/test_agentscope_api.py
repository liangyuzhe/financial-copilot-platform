from __future__ import annotations

from fastapi.testclient import TestClient


def _api_catalog():
    from tests.test_agentscope_adapter import _finance_catalog

    return _finance_catalog()


def test_agentscope_complex_analysis_endpoint_returns_draft_only_result(monkeypatch):
    from agents.api.app import app
    from agents.api.routers import agentscope as agentscope_router
    from agents.runtime.agentscope_adapter import LocalAgentScopeCompatibleRunner
    from agents.runtime.agentscope_runtime import AgentScopeRuntime

    def fake_runtime(*, runner):
        return AgentScopeRuntime(
            tool_catalog=_api_catalog(),
            runner=runner or LocalAgentScopeCompatibleRunner(),
        )

    monkeypatch.setattr(agentscope_router, "AgentScopeRuntime", fake_runtime)
    monkeypatch.setattr(
        agentscope_router,
        "create_agentscope_runner",
        lambda: LocalAgentScopeCompatibleRunner(),
    )

    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/agentscope/complex-analysis",
        json={
            "query": "分析今年收入、成本、预算、回款和费用之间的关系",
            "session_id": "api-complex",
            "workflow_state": {
                "selected_tables": [
                    "finance_revenue",
                    "finance_cost",
                    "finance_budget",
                    "finance_receivable",
                    "finance_expense",
                ],
            },
        },
        headers={
            "x-user-id": "api-user",
            "x-allowed-tables": (
                "finance_revenue,finance_cost,finance_budget,"
                "finance_receivable,finance_expense"
            ),
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "分析今年收入、成本、预算、回款和费用之间的关系"
    assert data["status"] == "needs_harness"
    assert data["session_id"] == "api-complex"
    assert "AgentScope 复杂分析计划" in data["answer"]
    assert "当前还不是最终经营结论" in data["answer"]
    assert data["sql_drafts"]
    assert data["sql_drafts"][0]["execution_mode"] == "draft_only"
    assert data["sql_drafts"][0]["requires_harness"] is True
    assert data["state_patch"]["requires_harness"] is True
    assert data["state_patch"]["agentscope_backend"] == "local_compatible"
    assert data["state_patch"]["presentation"]["next_action"] == "run_sql_harness"
    assert any(trace["tool_name"] == "sql_draft.submit" for trace in data["tool_trace"])


def test_agentscope_complex_analysis_endpoint_can_be_feature_disabled(monkeypatch):
    from agents.api.app import app

    monkeypatch.setenv("AGENTSCOPE_COMPLEX_ANALYSIS_ENABLED", "false")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/agentscope/complex-analysis",
        json={"query": "分析收入成本关系", "session_id": "api-disabled"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "disabled"
    assert data["answer"] == "AgentScope complex analysis endpoint is disabled."
    assert data["sql_drafts"] == []
