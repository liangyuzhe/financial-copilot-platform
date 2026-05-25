from __future__ import annotations

from pathlib import Path


def test_record_script_includes_agentscope_complex_demo_entry():
    script = Path("scripts/record_readme_demos.mjs").read_text(encoding="utf-8")

    assert "sql-complex-budget-expense-analysis-approved" in script
    assert "Complex Plan: 预算执行与报销费用分析" in script
    assert "2025年按部门分析预算执行率，并对比已审批报销费用与预算差异" in script
    assert "收入成本预算回款费用之间的关系" not in script
    assert "agentScopeBtn" not in script


def test_record_script_does_not_overlay_demo_content():
    script = Path("scripts/record_readme_demos.mjs").read_text(encoding="utf-8")

    assert "readme-demo-overlay" not in script
    assert 'DEMO_GIF_TRIM_START || "0.8"' in script
