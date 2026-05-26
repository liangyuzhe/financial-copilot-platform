from __future__ import annotations

from pathlib import Path


def test_record_script_includes_agentscope_complex_demo_entry():
    script = Path("scripts/record_readme_demos.mjs").read_text(encoding="utf-8")

    assert "sql-complex-dept-profitability-2026-approved" in script
    assert "Complex Plan: 2026 部门盈利率、亏损与成本分析" in script
    assert "2026年按部门分析盈利率，亏损，成本" in script
    assert "sql-complex-budget-expense-analysis-approved" not in script
    assert "收入成本预算回款费用之间的关系" not in script
    assert "agentScopeBtn" not in script


def test_record_script_does_not_overlay_demo_content():
    script = Path("scripts/record_readme_demos.mjs").read_text(encoding="utf-8")

    assert "readme-demo-overlay" not in script
    assert 'DEMO_GIF_TRIM_START || "0.8"' in script


def test_record_script_can_disable_browser_channel_for_bundled_chromium():
    script = Path("scripts/record_readme_demos.mjs").read_text(encoding="utf-8")

    assert 'DEMO_BROWSER_CHANNEL || "chrome"' in script
    assert 'browserChannel !== "none"' in script
