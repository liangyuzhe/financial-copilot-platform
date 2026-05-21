import re


def test_seed_rules_route_year_metric_queries_without_llm():
    from scripts.seed_intent_rules import load_seed_records

    records = load_seed_records()
    rule = next(record for record in records if record["name"] == "省略主体财务查数")

    assert rule["target_intent"] == "sql_query"
    assert re.search(rule["pattern"], "查询 2025 年销售收入总额")
    assert rule["rewrite_template"] == "公司{query}"
