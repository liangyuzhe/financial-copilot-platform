from __future__ import annotations

from pathlib import Path


def test_record_script_includes_agentscope_complex_demo_entry():
    script = Path("scripts/record_readme_demos.mjs").read_text(encoding="utf-8")

    assert "sql-complex-finance-relation-plan-approved" in script
    assert "Complex Plan: 收入成本预算回款费用关系" in script
    assert "复杂查询计划执行完成" in script
    assert "agentScopeBtn" not in script
