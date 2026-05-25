"""Static UI checks for SQL approval quality gate display."""

from pathlib import Path


def test_static_sql_approval_renders_quality_gate_payload():
    html = Path("agents/static/index.html").read_text(encoding="utf-8")

    assert "function renderQualityGate" in html
    assert "data.quality_gate" in html
    assert "SQL 质量门" in html
    assert "semantic" in html
    assert "dry_run" in html
