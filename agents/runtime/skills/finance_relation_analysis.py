"""Finance relation analysis skill.

The skill keeps the outer ReActAgent at a business-capability level while
orchestrating the primitive ToolCatalog evidence flow internally.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from agents.runtime.result import JsonDict
from agents.runtime.skill_contracts import RuntimeSkill, SkillResult, SkillTracePolicy


_FINANCE_RELATION_ALLOWED_TOOLS: tuple[str, ...] = (
    "current_time.now",
    "business_knowledge.search",
    "schema.list_tables",
    "schema.select_candidates",
    "semantic_model.search",
    "schema.related_tables",
    "plan.assess_feasibility",
    "analysis_plan.submit",
)


@dataclass(slots=True)
class FinanceRelationAnalysisSkill:
    """Plan finance relation analysis without exposing primitive tools to the outer agent."""

    contract: RuntimeSkill = RuntimeSkill(
        name="finance_relation_analysis",
        version="2026-05-24",
        description=(
            "Analyze relationships among revenue, cost, budget, receivables/cash collection, "
            "and expenses. Produces an SQL Harness analysis_plan; does not execute SQL."
        ),
        task_types=("data_analysis",),
        allowed_tools=_FINANCE_RELATION_ALLOWED_TOOLS,
        execution_modes=("single_sql", "plan_execute", "clarification"),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "User finance-relation analysis question.",
                },
                "time_range": {
                    "type": "string",
                    "description": "Optional explicit time range supplied by the user.",
                },
                "grain": {
                    "type": "string",
                    "description": (
                        "Optional business analysis grain inferred by the planner, "
                        "such as department, month, project, or overall."
                    ),
                },
                "merge_keys": {
                    "type": "array",
                    "description": (
                        "Optional row-alignment keys inferred by the planner for plan_execute, "
                        "for example department_id, cost_center_id, period, or project_code."
                    ),
                    "items": {"type": "string"},
                },
                "display_schema": {
                    "type": "array",
                    "description": (
                        "Optional user-facing output contract inferred by the planner. "
                        "Each item declares role, label, output column, and type. "
                        "SQL steps must output these columns when relevant; formatter renders only this contract."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "label": {"type": "string"},
                            "column": {"type": "string"},
                            "type": {"type": "string"},
                        },
                        "required": ["role", "label", "column", "type"],
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "execution_mode": {"type": "string"},
                "summary": {"type": "string"},
                "analysis_plan": {"type": "object"},
                "clarification_questions": {"type": "array"},
            },
        },
        trace_policy=SkillTracePolicy(max_observation_chars=4000, max_evidence_items=8),
    )

    async def run(self, payload: JsonDict, context: Any) -> SkillResult:
        query = str(payload.get("query") or context.query or "").strip()
        requested_grain = payload.get("grain")
        requested_merge_keys = self._string_items(payload.get("merge_keys"))
        display_schema = self._display_schema_items(payload.get("display_schema"))
        if self._needs_clarification(query):
            return SkillResult(
                status="needs_clarification",
                skill_name=f"{self.contract.name}_skill",
                skill_version=self.contract.version,
                execution_mode="clarification",
                summary="当前问题缺少可识别的财务关系分析目标，无法可靠规划。",
                clarification_questions=[
                    "请补充要分析的财务主题，例如收入、成本、预算、回款、费用、亏损或利润。",
                    "请补充时间范围和分析粒度，例如 2025 年按部门分析。",
                ],
            )

        await context.invoke_tool("current_time.now", {})
        knowledge = await context.invoke_tool(
            "business_knowledge.search",
            {"query": query, "top_k": 5},
        )
        evidence = self._knowledge_evidence(knowledge.output)
        evidence = await self._expand_formula_dependency_evidence(context, query, evidence)
        candidate_payload: JsonDict = {
            "query": query,
            "evidence": evidence,
            "top_k": 12,
        }
        workflow_tables = self._workflow_selected_tables(context)
        if workflow_tables:
            candidate_payload["candidate_tables"] = workflow_tables
        candidates = await context.invoke_tool("schema.select_candidates", candidate_payload)
        selected_tables = self._selected_tables(candidates.output)
        if not selected_tables:
            return SkillResult(
                status="needs_clarification",
                skill_name=f"{self.contract.name}_skill",
                skill_version=self.contract.version,
                execution_mode="clarification",
                summary="没有找到可用于财务关系分析的授权候选表。",
                evidence=evidence,
                clarification_questions=["请确认当前用户是否具备财务表权限，或补充目标数据域。"],
            )
        if not self._has_analysis_evidence(candidates.output):
            return SkillResult(
                status="needs_clarification",
                skill_name=f"{self.contract.name}_skill",
                skill_version=self.contract.version,
                execution_mode="clarification",
                summary="没有召回到足以支撑财务分析的业务术语、候选表或字段证据。",
                evidence=evidence,
                clarification_questions=[
                    "请补充要分析的财务指标或业务口径。",
                    "请补充时间范围和分析粒度，例如 2025 年按部门分析。",
                ],
            )
        recall_context = self._recall_context(candidates.output)

        relationships = await context.invoke_tool(
            "schema.related_tables",
            {"table_names": selected_tables},
        )
        relationship_rows = self._relationships(relationships.output)
        selected_tables = self._prune_disconnected_tables_for_focused_recall(
            selected_tables=selected_tables,
            relationships=relationship_rows,
            recall_context=recall_context,
            candidate_output=candidates.output,
        )
        relationship_rows = self._relationships_for_tables(relationship_rows, selected_tables)
        semantic = await context.invoke_tool(
            "semantic_model.search",
            {"table_names": selected_tables},
        )
        feasibility = await context.invoke_tool(
            "plan.assess_feasibility",
            {
                "query": query,
                "selected_tables": selected_tables,
                "relationships": relationship_rows,
                "recall_context": recall_context,
            },
        )

        execution_mode = self._execution_mode(feasibility.output)
        if execution_mode != "plan_execute":
            selected_tables = self._focused_single_sql_tables(
                query=query,
                selected_tables=selected_tables,
                evidence=evidence,
                recall_context=recall_context,
                relationships=relationship_rows,
            )
            relationship_rows = self._relationships_for_tables(relationship_rows, selected_tables)
        if execution_mode == "clarification":
            return SkillResult(
                status="needs_clarification",
                skill_name=f"{self.contract.name}_skill",
                skill_version=self.contract.version,
                execution_mode="clarification",
                summary="候选表之间缺少可用于合并的关系，继续规划可能产生错配口径。",
                evidence=self._result_evidence(evidence, selected_tables, semantic.output, feasibility.output),
                clarification_questions=[
                    "请明确分析粒度，例如按部门、期间、项目或公司整体。",
                    "请明确是否接受先分指标汇总再按公共粒度合并。",
                ],
            )

        plan = (
            self._plan_execute_plan(
                query,
                selected_tables,
                evidence,
                feasibility.output,
                recall_context,
                relationship_rows,
                requested_grain=requested_grain,
                requested_merge_keys=requested_merge_keys,
                display_schema=display_schema,
                semantic_model=semantic.output.get("semantic_model", {}) if isinstance(semantic.output, dict) else {},
            )
            if execution_mode == "plan_execute"
            else self._single_sql_plan(query, selected_tables, evidence, display_schema=display_schema)
        )
        submitted = await context.invoke_tool(
            "analysis_plan.submit",
            {
                "purpose": "财务关系分析 skill 生成结构化计划，交由 SQL Harness 审批与执行。",
                "plan": plan,
            },
        )
        if not submitted.ok or not isinstance(submitted.output, dict):
            return SkillResult(
                status="failed",
                skill_name=f"{self.contract.name}_skill",
                skill_version=self.contract.version,
                execution_mode=execution_mode,
                summary="analysis_plan.submit 提交失败。",
                evidence=self._result_evidence(evidence, selected_tables, semantic.output, feasibility.output),
                analysis_plan=plan,
                risk_flags=[
                    {
                        "code": "finance_relation_analysis_plan_submit_failed",
                        "severity": "error",
                        "message": submitted.error,
                    }
                ],
            )

        submitted_plan = submitted.output.get("plan") if isinstance(submitted.output.get("plan"), dict) else plan
        submitted_plan = {
            **submitted_plan,
            "execution_mode": execution_mode,
        }
        return SkillResult(
            status="plan_ready",
            skill_name=f"{self.contract.name}_skill",
            skill_version=self.contract.version,
            execution_mode=execution_mode,
            summary=(
                "已完成财务关系分析取证并提交多步计划给 SQL Harness。"
                if execution_mode == "plan_execute"
                else "已完成财务关系分析取证并提交单步计划给 SQL Harness。"
            ),
            evidence=self._result_evidence(evidence, selected_tables, semantic.output, feasibility.output),
            analysis_plan=submitted_plan,
        )

    def _needs_clarification(self, query: str) -> bool:
        return not query

    def _execution_mode(self, feasibility_output: Any) -> str:
        decision = {}
        if isinstance(feasibility_output, dict) and isinstance(feasibility_output.get("feasibility_decision"), dict):
            decision = feasibility_output["feasibility_decision"]
        mode = str(decision.get("execution_mode") or "").strip()
        if mode in {"complex_plan", "plan_execute"}:
            return "plan_execute"
        if mode == "clarify":
            return "clarification"
        return "single_sql"

    def _knowledge_evidence(self, output: Any) -> list[str]:
        if not isinstance(output, dict):
            return []
        rows = []
        for item in output.get("results") or []:
            if isinstance(item, dict) and str(item.get("content") or "").strip():
                rows.append(str(item.get("content")).strip())
        return rows[:5]

    async def _expand_formula_dependency_evidence(
        self,
        context: Any,
        query: str,
        evidence: list[str],
    ) -> list[str]:
        dependency_terms = self._missing_formula_dependency_terms(query, evidence)
        if not dependency_terms:
            return evidence

        dependency_evidence: list[str] = []
        for term in dependency_terms[:3]:
            knowledge = await context.invoke_tool(
                "business_knowledge.search",
                {"query": term, "top_k": 3},
            )
            for row in self._knowledge_evidence(knowledge.output):
                if self._evidence_matches_term(row, term):
                    dependency_evidence.append(row)
        return self._unique_evidence([*dependency_evidence, *evidence])

    def _missing_formula_dependency_terms(self, query: str, evidence: list[str]) -> list[str]:
        entries = self._business_evidence_entries(evidence)
        known_terms = {
            self._normalize_schema_token(str(entry.get("term") or ""))
            for entry in entries
            if str(entry.get("term") or "").strip()
        }
        missing: list[str] = []
        for entry in entries:
            if self._business_entry_query_match_score(entry, query) <= 0:
                continue
            for component in self._formula_components(str(entry.get("formula") or "")):
                normalized = self._normalize_schema_token(component)
                if not normalized or normalized in known_terms:
                    continue
                if component not in missing:
                    missing.append(component)
        return missing

    def _evidence_matches_term(self, evidence: str, term: str) -> bool:
        target = self._normalize_schema_token(term)
        if not target:
            return False
        for entry in self._business_evidence_entries([evidence]):
            aliases = [
                str(entry.get("term") or "").strip(),
                *self._string_items(entry.get("synonyms") or []),
            ]
            if any(self._normalize_schema_token(alias) == target for alias in aliases):
                return True
        return False

    def _unique_evidence(self, rows: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for row in rows:
            text = str(row or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    def _selected_tables(self, output: Any) -> list[str]:
        if not isinstance(output, dict):
            return []
        return [
            str(table).strip()
            for table in output.get("selected_tables", []) or []
            if str(table).strip()
        ]

    def _workflow_selected_tables(self, context: Any) -> list[str]:
        workflow_state = getattr(context, "workflow_state", None)
        if not isinstance(workflow_state, dict):
            return []
        return [
            str(table).strip()
            for table in workflow_state.get("selected_tables", []) or []
            if str(table).strip()
        ]

    def _has_analysis_evidence(self, output: Any) -> bool:
        if not isinstance(output, dict):
            return False
        recall_context = output.get("recall_context")
        if isinstance(recall_context, dict):
            for key in ("matched_terms", "few_shot_related_tables"):
                if [item for item in recall_context.get(key, []) or [] if str(item).strip()]:
                    return True
        for row in output.get("candidate_scores", []) or []:
            if not isinstance(row, dict):
                continue
            try:
                if float(row.get("score") or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _recall_context(self, output: Any) -> dict[str, Any]:
        if not isinstance(output, dict):
            return {}
        recall_context = output.get("recall_context")
        return dict(recall_context) if isinstance(recall_context, dict) else {}

    def _relationships(self, output: Any) -> list[dict[str, Any]]:
        if not isinstance(output, dict):
            return []
        return [dict(row) for row in output.get("relationships", []) or [] if isinstance(row, dict)]

    def _prune_disconnected_tables_for_focused_recall(
        self,
        *,
        selected_tables: list[str],
        relationships: list[dict[str, Any]],
        recall_context: dict[str, Any],
        candidate_output: Any,
    ) -> list[str]:
        if not self._is_focused_recall_context(recall_context):
            return selected_tables
        components = self._relationship_components(selected_tables, relationships)
        if len(components) <= 1:
            return selected_tables

        score_by_table = self._candidate_score_map(candidate_output)
        evidence_tables = set(
            self._string_items(
                [
                    *(recall_context.get("business_related_tables") or []),
                    *(recall_context.get("few_shot_related_tables") or []),
                ]
            )
        )
        selected_rank = {table: index for index, table in enumerate(selected_tables)}

        def component_key(component: set[str]) -> tuple[int, int, float, int]:
            rank_weight = sum(len(selected_tables) - selected_rank.get(table, len(selected_tables)) for table in component)
            return (
                len(component & evidence_tables),
                len(component),
                sum(score_by_table.get(table, 0.0) for table in component),
                rank_weight,
            )

        best_component = max(components, key=component_key)
        if not best_component or len(best_component) == len(set(selected_tables)):
            return selected_tables
        return [table for table in selected_tables if table in best_component]

    def _is_focused_recall_context(self, recall_context: dict[str, Any]) -> bool:
        if not isinstance(recall_context, dict):
            return False
        task_type = str(recall_context.get("task_type") or "").strip().lower()
        if task_type in {"analysis", "comparison", "report"}:
            return False
        matched_terms = self._string_items(recall_context.get("matched_terms") or [])
        return 1 <= len(matched_terms) <= 2

    def _relationship_components(
        self,
        selected_tables: list[str],
        relationships: list[dict[str, Any]],
    ) -> list[set[str]]:
        tables = set(selected_tables)
        if not tables:
            return []
        parent = {table: table for table in tables}

        def find(table: str) -> str:
            while parent[table] != table:
                parent[table] = parent[parent[table]]
                table = parent[table]
            return table

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for rel in relationships:
            left = str(rel.get("from_table") or "").strip()
            right = str(rel.get("to_table") or "").strip()
            if left in tables and right in tables:
                union(left, right)

        components: dict[str, set[str]] = {}
        for table in tables:
            components.setdefault(find(table), set()).add(table)
        return list(components.values())

    def _relationships_for_tables(
        self,
        relationships: list[dict[str, Any]],
        selected_tables: list[str],
    ) -> list[dict[str, Any]]:
        tables = set(selected_tables)
        return [
            rel
            for rel in relationships
            if str(rel.get("from_table") or "").strip() in tables
            and str(rel.get("to_table") or "").strip() in tables
        ]

    def _candidate_score_map(self, output: Any) -> dict[str, float]:
        if not isinstance(output, dict):
            return {}
        scores: dict[str, float] = {}
        for row in output.get("candidate_scores", []) or []:
            if not isinstance(row, dict):
                continue
            table = str(row.get("table") or "").strip()
            if not table:
                continue
            try:
                scores[table] = float(row.get("score") or 0.0)
            except (TypeError, ValueError):
                scores[table] = 0.0
        return scores

    def _string_items(self, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        rows: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            if text and text not in seen:
                rows.append(text)
                seen.add(text)
        return rows

    def _result_evidence(
        self,
        evidence: list[str],
        selected_tables: list[str],
        semantic_output: Any,
        feasibility_output: Any,
    ) -> list[str]:
        rows = list(evidence[:4])
        if selected_tables:
            rows.append("候选表: " + ", ".join(selected_tables[:12]))
        if isinstance(semantic_output, dict) and semantic_output.get("tables"):
            rows.append("字段语义覆盖: " + ", ".join(str(table) for table in semantic_output.get("tables", [])[:12]))
        if isinstance(feasibility_output, dict):
            decision = feasibility_output.get("feasibility_decision")
            if isinstance(decision, dict):
                rows.append(
                    "复杂度判断: "
                    + str(decision.get("execution_mode") or "")
                    + " / "
                    + str(decision.get("reason") or "")
                )
        return rows

    def _display_schema_items(self, value: Any) -> list[JsonDict]:
        if not isinstance(value, list):
            return []
        items: list[JsonDict] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            label = str(item.get("label") or "").strip()
            column = str(item.get("column") or "").strip()
            value_type = str(item.get("type") or "").strip()
            if not (role and label and column and value_type):
                continue
            items.append({"role": role, "label": label, "column": column, "type": value_type})
        return items

    def _single_sql_plan(
        self,
        query: str,
        selected_tables: list[str],
        evidence: list[str],
        *,
        display_schema: list[JsonDict] | None = None,
    ) -> JsonDict:
        return {
            "mode": "analysis_plan",
            "execution_mode": "single_sql",
            "reason": "候选表和口径较集中，可交由 SQL Harness 生成单步 SQL。",
            "display_schema": list(display_schema or []),
            "evidence": evidence[:5],
            "steps": [
                {
                    "step": 1,
                    "type": "sql",
                    "goal": f"回答财务分析问题：{query}",
                    "tables": selected_tables[:5],
                    "depends_on": [],
                    "merge_keys": [],
                }
            ],
            "requires_user_confirmation": True,
        }

    def _plan_execute_plan(
        self,
        query: str,
        selected_tables: list[str],
        evidence: list[str],
        feasibility_output: Any,
        recall_context: dict[str, Any],
        relationships: list[dict[str, Any]],
        *,
        requested_grain: Any = "",
        requested_merge_keys: list[str] | None = None,
        display_schema: list[JsonDict] | None = None,
        semantic_model: dict[str, Any] | None = None,
    ) -> JsonDict:
        semantic_model = semantic_model or {}
        inferred_grain = self._infer_requested_grain(
            query=query,
            relationships=relationships,
            semantic_model=semantic_model,
        )
        effective_grain = requested_grain or inferred_grain.get("label") or ""
        effective_requested_merge_keys = requested_merge_keys or self._string_items(inferred_grain.get("merge_keys"))
        ignored_metric_labels = [str(effective_grain or "").replace("维度", "").strip()]
        subject_groups = self._subject_groups_from_evidence(
            query=query,
            selected_tables=selected_tables,
            evidence=evidence,
            recall_context=recall_context,
            relationships=relationships,
            ignored_query_labels=ignored_metric_labels,
        )
        if not subject_groups:
            subject_groups = self._subject_groups_from_components(selected_tables, relationships)
        subject_groups = self._limit_subject_groups(subject_groups, max_sql_steps=3)
        effective_display_schema = (
            list(display_schema or [])
            or self._infer_display_schema(
                query=query,
                evidence=evidence,
                subject_groups=subject_groups,
                grain_label=str(effective_grain or ""),
            )
        )
        merge_keys = self._merge_keys_from_relationships(
            relationships,
            requested_merge_keys=effective_requested_merge_keys,
            requested_grain=effective_grain,
        )
        grain_label = self._grain_label_for_merge_keys(
            merge_keys,
            requested_grain=effective_grain,
        )
        step_table_limit = 8

        steps: list[JsonDict] = []
        for index, group in enumerate(subject_groups, start=1):
            label = str(group.get("label") or f"指标组 {index}")
            group_terms = self._group_terms(group)
            tables = [
                table
                for table in group.get("tables", [])
                if isinstance(table, str) and table in selected_tables
            ]
            if not tables:
                tables = selected_tables[:step_table_limit]
            tables = self._expand_tables_for_merge_keys(
                tables,
                selected_tables,
                relationships,
                merge_keys,
            )
            output_schema = self._step_output_schema(
                effective_display_schema,
                group_terms=group_terms,
                group_tables=tables,
                evidence=evidence,
            )
            steps.append(
                {
                    "step": index,
                    "type": "sql",
                    "goal": f"按{grain_label}统计{label}",
                    "tables": tables[:step_table_limit],
                    "grain": merge_keys,
                    "depends_on": [],
                    "merge_keys": merge_keys,
                    **({"output_schema": output_schema} if output_schema else {}),
                }
            )

        last_step = len(steps)
        if len(steps) > 1:
            merge_step = last_step + 1
            steps.append(
                {
                    "step": merge_step,
                    "type": "python_merge",
                    "goal": f"按{grain_label}合并各指标组结果并计算对比指标",
                    "tables": [],
                    "depends_on": list(range(1, merge_step)),
                    "merge_keys": merge_keys,
                }
            )
            report_depends_on = [merge_step]
            report_step = merge_step + 1
        else:
            report_depends_on = [last_step] if last_step else []
            report_step = last_step + 1

        steps.append(
            {
                "step": report_step,
                "type": "report",
                "goal": "输出分析结论、异常点和后续追查建议",
                "tables": [],
                "depends_on": report_depends_on,
                "merge_keys": [],
            }
        )

        return {
            "mode": "analysis_plan",
            "execution_mode": "plan_execute",
            "reason": (
                "该问题召回到多个业务术语或事实表组。"
                "为降低多表 join 幻觉风险，先按证据分组汇总，再交由 SQL Harness 合并分析。"
            ),
            "source_query": query,
            "display_schema": list(effective_display_schema),
            "evidence": evidence[:5],
            "feasibility": self._compact_feasibility(feasibility_output),
            "steps": steps,
            "requires_user_confirmation": True,
        }

    def _subject_groups_from_evidence(
        self,
        *,
        query: str,
        selected_tables: list[str],
        evidence: list[str],
        recall_context: dict[str, Any],
        relationships: list[dict[str, Any]],
        ignored_query_labels: list[str] | None = None,
    ) -> list[JsonDict]:
        matched_terms = set(self._string_items(recall_context.get("matched_terms") or []))
        if not matched_terms:
            return []
        primary_evidence_tables = self._primary_evidence_tables(evidence, matched_terms)
        groups_by_tables: dict[tuple[str, ...], JsonDict] = {}
        groups_by_primary: dict[str, JsonDict] = {}
        for entry in self._business_evidence_entries(evidence):
            term = str(entry.get("term") or "").strip()
            if term not in matched_terms:
                continue
            if self._business_entry_query_match_score(entry, query, ignored_labels=ignored_query_labels) <= 0:
                continue
            tables = self._selected_ordered_tables(entry.get("related_tables", []), selected_tables)
            if not tables:
                continue
            primary_table = tables[0]
            tables = self._expand_group_tables(
                tables,
                selected_tables,
                relationships,
                primary_evidence_tables,
                primary_table=primary_table,
            )
            if primary_table:
                group = groups_by_primary.setdefault(primary_table, {"labels": [], "tables": []})
                group["tables"] = self._unique([*group.get("tables", []), *tables])
            else:
                key = tuple(tables)
                group = groups_by_tables.setdefault(key, {"labels": [], "tables": tables})
            group["labels"].append(term)
        groups: list[JsonDict] = []
        for group in [*groups_by_primary.values(), *groups_by_tables.values()]:
            labels = self._unique([str(label) for label in group.get("labels", [])])
            groups.append(
                {
                    "label": "、".join(labels) if labels else query,
                    "tables": group.get("tables", []),
                }
            )
        return groups

    def _group_terms(self, group: JsonDict) -> list[str]:
        terms = self._string_items(group.get("terms") or [])
        if terms:
            return terms
        label = str(group.get("label") or "").strip()
        return self._split_terms(label) if label else []

    def _step_output_schema(
        self,
        display_schema: list[JsonDict],
        *,
        group_terms: list[str],
        group_tables: list[str],
        evidence: list[str],
    ) -> list[JsonDict]:
        if not display_schema:
            return []
        searchable = self._step_schema_search_text(group_terms, group_tables, evidence)
        fields: list[JsonDict] = []
        for field in display_schema:
            role = str(field.get("role") or "").strip()
            label = str(field.get("label") or "").strip()
            column = str(field.get("column") or "").strip()
            value_type = str(field.get("type") or "").strip().lower()
            if value_type == "dimension":
                fields.append(dict(field))
                continue
            candidates = [role, label, column]
            if any(self._schema_token_matches(candidate, searchable) for candidate in candidates):
                fields.append(dict(field))
        return fields

    def _step_schema_search_text(
        self,
        group_terms: list[str],
        group_tables: list[str],
        evidence: list[str],
    ) -> str:
        selected_tables = set(group_tables)
        parts = [*group_terms, *group_tables]
        matched_terms = set(group_terms)
        for entry in self._business_evidence_entries(evidence):
            related_tables = set(self._string_items(entry.get("related_tables") or []))
            if selected_tables and related_tables and not selected_tables.intersection(related_tables):
                continue
            term = str(entry.get("term") or "")
            synonyms = self._string_items(entry.get("synonyms") or [])
            parts.append(term)
            parts.extend(synonyms)
            aliases = {term, *synonyms}
            if matched_terms.intersection(alias for alias in aliases if alias):
                parts.extend(self._formula_components(str(entry.get("formula") or "")))
        return " ".join(part for part in parts if part)

    def _schema_token_matches(self, candidate: str, searchable: str) -> bool:
        token = self._normalize_schema_token(candidate)
        haystack = self._normalize_schema_token(searchable)
        return bool(token and haystack and (token in haystack or haystack in token))

    def _normalize_schema_token(self, value: str) -> str:
        return "".join(ch for ch in str(value or "").lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")

    def _infer_requested_grain(
        self,
        *,
        query: str,
        relationships: list[dict[str, Any]],
        semantic_model: dict[str, Any],
    ) -> JsonDict:
        best: tuple[int, str, str] | None = None
        for rel in relationships:
            if not self._is_dimension_relationship(rel):
                continue
            merge_key = str(rel.get("from_column") or "").strip()
            score, label = self._best_query_label_match(
                query,
                self._relationship_business_labels(rel, semantic_model),
            )
            if score <= 0 or not label:
                continue
            candidate = (score, label, merge_key)
            if best is None or candidate[0] > best[0]:
                best = candidate
        if best is None:
            return {}
        return {"label": best[1], "merge_keys": [best[2]]}

    def _relationship_business_labels(
        self,
        rel: dict[str, Any],
        semantic_model: dict[str, Any],
    ) -> list[str]:
        labels = [
            self._merge_key_base(str(rel.get("from_column") or "")),
            self._table_base(str(rel.get("to_table") or "")),
        ]
        for table_name, column_name in (
            (str(rel.get("from_table") or ""), str(rel.get("from_column") or "")),
            (str(rel.get("to_table") or ""), str(rel.get("to_column") or "")),
        ):
            columns = semantic_model.get(table_name) if isinstance(semantic_model, dict) else None
            if not isinstance(columns, dict):
                continue
            column = columns.get(column_name)
            if not isinstance(column, dict):
                continue
            labels.extend(
                str(column.get(key) or "")
                for key in ("business_name", "column_comment", "synonyms", "business_description")
                if str(column.get(key) or "").strip()
            )
        return labels

    def _best_query_label_match(self, query: str, labels: list[str]) -> tuple[int, str]:
        query_norm = self._normalize_schema_token(query)
        best: tuple[int, str] = (0, "")
        for raw_label in labels:
            split_labels = self._split_terms(str(raw_label or "")) or [str(raw_label or "")]
            for label in split_labels:
                label_norm = self._normalize_schema_token(label)
                if not label_norm:
                    continue
                if label_norm in query_norm:
                    candidate = (100 + len(label_norm), label)
                else:
                    candidate = self._best_substring_label_match(query_norm, label_norm)
                if candidate[0] > best[0]:
                    best = candidate
        return best

    def _best_substring_label_match(self, query_norm: str, label_norm: str) -> tuple[int, str]:
        max_len = min(len(label_norm), 8)
        for length in range(max_len, 1, -1):
            for start in range(0, len(label_norm) - length + 1):
                piece = label_norm[start : start + length]
                if piece and piece in query_norm:
                    return (length, piece)
        return (0, "")

    def _infer_display_schema(
        self,
        *,
        query: str,
        evidence: list[str],
        subject_groups: list[JsonDict],
        grain_label: str,
    ) -> list[JsonDict]:
        fields: list[JsonDict] = []
        clean_grain = str(grain_label or "").replace("维度", "").strip()
        if clean_grain:
            fields.append(self._display_field(clean_grain, "dimension"))

        subject_terms = {
            term
            for group in subject_groups
            for term in self._group_terms(group)
            if term
        }
        amount_fields: list[JsonDict] = []
        ratio_dependency_fields: list[JsonDict] = []
        percent_fields: list[JsonDict] = []
        for entry in self._business_evidence_entries(evidence):
            term = str(entry.get("term") or "").strip()
            if term and subject_terms and term not in subject_terms:
                continue
            if self._business_entry_query_match_score(entry, query, ignored_labels=[clean_grain]) <= 0:
                continue
            for component in self._formula_components(str(entry.get("formula") or "")):
                if component in query:
                    amount_fields.append(self._display_field(component, "amount"))
            aliases = [term, *self._string_items(entry.get("synonyms") or [])]
            matched_aliases = [alias for alias in aliases if alias and alias in query]
            if matched_aliases:
                if any(self._looks_like_percent_label(alias) for alias in matched_aliases):
                    percent_label = next(alias for alias in matched_aliases if self._looks_like_percent_label(alias))
                    percent_fields.append(self._display_field(percent_label, "percent"))
                if term:
                    value_type = "percent" if self._looks_like_percent_label(term) else "amount"
                    (percent_fields if value_type == "percent" else amount_fields).append(
                        self._display_field(term, value_type)
                    )
                    if value_type == "percent":
                        for denominator in self._ratio_denominator_components(str(entry.get("formula") or "")):
                            ratio_dependency_fields.append(self._display_field(denominator, "amount"))
        return self._unique_display_fields([*fields, *ratio_dependency_fields, *amount_fields, *percent_fields])

    def _ratio_denominator_components(self, formula: str) -> list[str]:
        text = str(formula or "")
        if "/" not in text:
            return []
        denominator = text.split("/", 1)[1]
        denominator = denominator.split("*", 1)[0].split("%", 1)[0].split("；", 1)[0].split(";", 1)[0]
        return [
            item
            for item in self._formula_components(denominator)
            if item
        ]

    def _formula_components(self, formula: str) -> list[str]:
        text = str(formula or "")
        text = text.split("；", 1)[0].split(";", 1)[0].split("。", 1)[0]
        for symbol in ("(", ")", "（", "）", "+", "-", "*", "/", "=", ">", "<", "%", "，", "；", ";", ","):
            text = text.replace(symbol, " ")
        return [
            item.strip()
            for item in text.split()
            if item.strip() and not item.strip().isdigit()
        ]

    def _looks_like_percent_label(self, label: str) -> bool:
        text = str(label or "").lower()
        return bool("率" in text or "ratio" in text or "rate" in text or "percent" in text or "%" in text)

    def _display_field(self, label: str, value_type: str) -> JsonDict:
        clean_label = str(label or "").strip()
        return {
            "role": self._normalize_schema_token(clean_label) or clean_label,
            "label": clean_label,
            "column": clean_label,
            "type": value_type,
        }

    def _unique_display_fields(self, fields: list[JsonDict]) -> list[JsonDict]:
        result: list[JsonDict] = []
        seen: set[str] = set()
        for field in fields:
            column = str(field.get("column") or "").strip()
            if not column or column in seen:
                continue
            seen.add(column)
            result.append(field)
        return result

    def _business_evidence_entries(self, evidence: list[str]) -> list[JsonDict]:
        entries: list[JsonDict] = []
        for item in evidence:
            entry: JsonDict = {"synonyms": [], "related_tables": []}
            for raw_line in str(item or "").splitlines():
                key, value = self._split_business_evidence_line(raw_line)
                if not key:
                    continue
                if key in {"术语", "term"}:
                    entry["term"] = value
                elif key in {"公式", "formula", "定义", "definition"}:
                    entry["formula"] = value
                elif key in {"同义词", "synonyms"}:
                    entry["synonyms"] = self._split_terms(value)
                elif key in {"关联表", "related_tables", "tables"}:
                    entry["related_tables"] = self._split_terms(value)
            if entry.get("term"):
                entries.append(entry)
        return entries

    def _primary_evidence_tables(self, evidence: list[str], matched_terms: set[str]) -> set[str]:
        tables: set[str] = set()
        for entry in self._business_evidence_entries(evidence):
            term = str(entry.get("term") or "").strip()
            related_tables = self._string_items(entry.get("related_tables") or [])
            if term in matched_terms and related_tables:
                tables.add(related_tables[0])
        return tables

    def _split_business_evidence_line(self, line: str) -> tuple[str, str]:
        text = str(line or "").strip()
        for sep in (":", "："):
            if sep in text:
                left, right = text.split(sep, 1)
                return left.strip().lower(), right.strip()
        return "", ""

    def _split_terms(self, value: str) -> list[str]:
        return [
            term.strip()
            for term in (
                str(value or "")
                .replace("，", ",")
                .replace("；", ",")
                .replace(";", ",")
                .replace("、", ",")
                .split(",")
            )
            if term.strip()
        ]

    def _selected_ordered_tables(self, tables: Any, selected_tables: list[str]) -> list[str]:
        selected = set(selected_tables)
        return [table for table in self._string_items(tables if isinstance(tables, list) else []) if table in selected]

    def _expand_group_tables(
        self,
        tables: list[str],
        selected_tables: list[str],
        relationships: list[dict[str, Any]],
        primary_evidence_tables: set[str],
        *,
        primary_table: str,
    ) -> list[str]:
        selected = set(selected_tables)
        result = self._unique([table for table in tables if table in selected])
        seeds = {primary_table} if primary_table in selected else set(result)
        for rel in relationships:
            left = str(rel.get("from_table") or "").strip()
            right = str(rel.get("to_table") or "").strip()
            if (
                left in seeds
                and right in selected
                and right not in result
                and right not in primary_evidence_tables
            ):
                result.append(right)
            elif (
                right in seeds
                and left in selected
                and left not in result
                and left not in primary_evidence_tables
            ):
                result.append(left)
        return result

    def _focused_single_sql_tables(
        self,
        *,
        query: str,
        selected_tables: list[str],
        evidence: list[str],
        recall_context: dict[str, Any],
        relationships: list[dict[str, Any]],
    ) -> list[str]:
        matched_terms = set(self._string_items(recall_context.get("matched_terms") or []))
        if not matched_terms:
            return selected_tables

        primary_evidence_tables = self._primary_evidence_tables(evidence, matched_terms)
        scored_groups: list[tuple[int, list[str]]] = []
        for entry in self._business_evidence_entries(evidence):
            term = str(entry.get("term") or "").strip()
            if term not in matched_terms:
                continue
            tables = self._selected_ordered_tables(entry.get("related_tables", []), selected_tables)
            if not tables:
                continue
            score = self._business_entry_query_match_score(entry, query)
            if score <= 0:
                continue
            primary_table = tables[0]
            scoped_tables = self._expand_group_tables(
                tables,
                selected_tables,
                relationships,
                primary_evidence_tables,
                primary_table=primary_table,
            )
            scored_groups.append((score, scoped_tables))

        if not scored_groups:
            return selected_tables
        max_score = max(score for score, _tables in scored_groups)
        focused: list[str] = []
        for score, tables in scored_groups:
            if score == max_score:
                focused.extend(tables)
        focused_tables = self._unique([table for table in focused if table in selected_tables])
        if not focused_tables:
            return selected_tables
        return self._best_connected_component_tables(focused_tables, relationships)

    def _best_connected_component_tables(
        self,
        selected_tables: list[str],
        relationships: list[dict[str, Any]],
    ) -> list[str]:
        components = self._relationship_components(selected_tables, relationships)
        if len(components) <= 1:
            return selected_tables
        selected_rank = {table: index for index, table in enumerate(selected_tables)}

        def component_key(component: set[str]) -> tuple[int, int]:
            rank_weight = sum(len(selected_tables) - selected_rank.get(table, len(selected_tables)) for table in component)
            return (len(component), rank_weight)

        best_component = max(components, key=component_key)
        return [table for table in selected_tables if table in best_component] or selected_tables

    def _business_entry_query_match_score(
        self,
        entry: JsonDict,
        query: str,
        *,
        ignored_labels: list[str] | None = None,
    ) -> int:
        ignored = {
            self._normalize_schema_token(label)
            for label in self._string_items(ignored_labels or [])
            if self._normalize_schema_token(label)
        }
        aliases = [
            str(entry.get("term") or "").strip(),
            *self._string_items(entry.get("synonyms") or []),
        ]
        score = 0
        for alias in aliases:
            alias_norm = self._normalize_schema_token(alias)
            if alias_norm in ignored:
                continue
            if alias and alias in query:
                score += max(1, len(alias))
            elif alias and query in alias:
                score += max(1, len(query))
            else:
                overlap_score, _label = self._best_substring_label_match(
                    self._normalize_schema_token(query),
                    alias_norm,
                )
                if overlap_score and _label not in ignored:
                    score += overlap_score
        return score

    def _subject_groups_from_components(
        self,
        selected_tables: list[str],
        relationships: list[dict[str, Any]],
    ) -> list[JsonDict]:
        components = self._relationship_components(selected_tables, relationships)
        if not components:
            return [{"label": "候选指标", "tables": selected_tables[:5]}]
        groups: list[JsonDict] = []
        for index, component in enumerate(components, start=1):
            tables = [table for table in selected_tables if table in component]
            groups.append({"label": f"候选指标组 {index}", "tables": tables})
        return groups

    def _limit_subject_groups(self, groups: list[JsonDict], *, max_sql_steps: int) -> list[JsonDict]:
        if len(groups) <= max_sql_steps:
            return groups
        head = groups[: max_sql_steps - 1]
        tail = groups[max_sql_steps - 1 :]
        tail_labels = [str(group.get("label") or "") for group in tail if str(group.get("label") or "")]
        tail_tables: list[str] = []
        for group in tail:
            tail_tables.extend(str(table) for table in group.get("tables", []) if str(table))
        return [
            *head,
            {
                "label": "、".join(tail_labels) if tail_labels else f"指标组 {max_sql_steps}",
                "tables": self._unique([table for table in tail_tables]),
            },
        ]

    def _merge_keys_from_relationships(
        self,
        relationships: list[dict[str, Any]],
        *,
        requested_merge_keys: list[str],
        requested_grain: Any = "",
    ) -> list[str]:
        validated = [
            key
            for key in requested_merge_keys
            if self._is_valid_requested_merge_key(key, relationships)
        ]
        if validated:
            return self._unique(validated)[:3]

        candidate_counts: dict[str, set[str]] = {}
        for rel in relationships:
            if self._is_hierarchy_relationship(rel):
                continue
            column = str(rel.get("from_column") or "").strip()
            if not self._is_dimension_relationship(rel):
                continue
            from_table = str(rel.get("from_table") or "").strip()
            candidate_counts.setdefault(column, set()).add(from_table)

        candidates = [
            column
            for column, source_tables in candidate_counts.items()
            if column and len(source_tables) > 1
        ]
        ordered = self._order_merge_keys_by_relationship_grain(candidates, relationships)
        grain_filtered = self._filter_merge_keys_by_requested_grain(
            ordered,
            relationships,
            requested_grain=requested_grain,
        )
        return (grain_filtered or ordered)[:3]

    def _filter_merge_keys_by_requested_grain(
        self,
        merge_keys: list[str],
        relationships: list[dict[str, Any]],
        *,
        requested_grain: Any = "",
    ) -> list[str]:
        grain = self._normalize_grain_token(str(requested_grain or ""))
        if not grain:
            return []
        return [
            key
            for key in merge_keys
            if self._merge_key_matches_requested_grain(key, grain, relationships)
        ]

    def _merge_key_matches_requested_grain(
        self,
        merge_key: str,
        normalized_grain: str,
        relationships: list[dict[str, Any]],
    ) -> bool:
        candidates = {self._merge_key_base(merge_key)}
        for rel in relationships:
            if str(rel.get("from_column") or "").strip() == merge_key and self._is_dimension_relationship(rel):
                candidates.add(self._table_base(str(rel.get("to_table") or "").strip()))
        normalized_candidates = {self._normalize_grain_token(candidate) for candidate in candidates}
        return any(
            candidate == normalized_grain
            or (len(candidate) >= 4 and candidate in normalized_grain)
            or (len(normalized_grain) >= 4 and normalized_grain in candidate)
            for candidate in normalized_candidates
            if candidate
        )

    def _normalize_grain_token(self, value: str) -> str:
        token = str(value or "").strip().lower()
        for suffix in ("维度", "grain", "_id", "_code"):
            if token.endswith(suffix):
                token = token[: -len(suffix)]
        return "".join(ch for ch in token if ch.isalnum())

    def _is_valid_requested_merge_key(self, key: str, relationships: list[dict[str, Any]]) -> bool:
        if key == "period":
            return True
        if not self._is_business_merge_column(key):
            return False
        return any(
            str(rel.get("from_column") or "").strip() == key
            and self._is_dimension_relationship(rel)
            for rel in relationships
        )

    def _is_hierarchy_relationship(self, rel: dict[str, Any]) -> bool:
        left = str(rel.get("from_table") or "").strip()
        right = str(rel.get("to_table") or "").strip()
        return bool(left and left == right)

    def _is_business_merge_column(self, column: str) -> bool:
        if not column or column == "id":
            return False
        return column.endswith("_id") or column.endswith("_code") or column in {"period"}

    def _is_dimension_relationship(self, rel: dict[str, Any]) -> bool:
        if self._is_hierarchy_relationship(rel):
            return False
        column = str(rel.get("from_column") or "").strip()
        target_column = str(rel.get("to_column") or "").strip()
        target_table = str(rel.get("to_table") or "").strip()
        if not self._is_business_merge_column(column):
            return False
        column_base = self._merge_key_base(column)
        table_base = self._table_base(target_table)
        if column.endswith("_id") and target_column == "id":
            return column_base == table_base
        if column.endswith("_code") and target_column.endswith("_code"):
            return column_base == table_base
        return False

    def _grain_label_for_merge_keys(self, merge_keys: list[str], *, requested_grain: Any = "") -> str:
        grain = str(requested_grain or "").strip()
        if grain:
            return grain if grain.endswith("维度") else f"{grain}维度"
        if "period" in set(merge_keys):
            return "期间维度"
        return "公共维度"

    def _merge_key_base(self, key: str) -> str:
        text = str(key or "").strip()
        for suffix in ("_id", "_code"):
            if text.endswith(suffix):
                return text[: -len(suffix)]
        return text

    def _table_base(self, table: str) -> str:
        text = str(table or "").strip()
        return text[2:] if text.startswith("t_") else text

    def _dimension_tables_for_merge_key(
        self,
        merge_key: str,
        selected_tables: set[str],
        relationships: list[dict[str, Any]],
    ) -> list[str]:
        tables: list[str] = []
        for rel in relationships:
            if str(rel.get("from_column") or "").strip() != merge_key:
                continue
            if not self._is_dimension_relationship(rel):
                continue
            target = str(rel.get("to_table") or "").strip()
            if target in selected_tables and target not in tables:
                tables.append(target)
        return tables

    def _order_merge_keys_by_relationship_grain(
        self,
        merge_keys: list[str],
        relationships: list[dict[str, Any]],
    ) -> list[str]:
        ordered = self._unique(merge_keys)
        for parent_key in list(ordered):
            for child_key in list(ordered):
                if parent_key == child_key:
                    continue
                child_dimensions = {
                    str(rel.get("to_table") or "").strip()
                    for rel in relationships
                    if str(rel.get("from_column") or "").strip() == child_key
                    and self._is_dimension_relationship(rel)
                }
                is_parent_of_child = any(
                    str(rel.get("from_table") or "").strip() in child_dimensions
                    and str(rel.get("from_column") or "").strip() == parent_key
                    for rel in relationships
                )
                if is_parent_of_child and ordered.index(parent_key) > ordered.index(child_key):
                    ordered.remove(parent_key)
                    ordered.insert(ordered.index(child_key), parent_key)
        return ordered

    def _expand_tables_for_merge_keys(
        self,
        tables: list[str],
        selected_tables: list[str],
        relationships: list[dict[str, Any]],
        merge_keys: list[str],
    ) -> list[str]:
        result = self._unique([table for table in tables if table in selected_tables])
        selected = set(selected_tables)
        grain_tables = self._dimension_tables_for_merge_keys(merge_keys, selected, relationships)
        for table in grain_tables:
            if table in selected and table not in result:
                result = self._connect_tables(result, table, selected_tables, relationships)
        return self._unique([table for table in result if table in selected])

    def _dimension_tables_for_merge_keys(
        self,
        merge_keys: list[str],
        selected_tables: set[str],
        relationships: list[dict[str, Any]],
    ) -> list[str]:
        tables: list[str] = []
        for merge_key in merge_keys:
            for table in self._dimension_tables_for_merge_key(merge_key, selected_tables, relationships):
                if table not in tables:
                    tables.append(table)
        return tables

    def _connect_tables(
        self,
        tables: list[str],
        target_table: str,
        selected_tables: list[str],
        relationships: list[dict[str, Any]],
    ) -> list[str]:
        if target_table in tables:
            return tables
        selected = set(selected_tables)
        seeds = [table for table in tables if table in selected]
        if not seeds:
            return self._unique([*tables, target_table])
        graph: dict[str, set[str]] = {table: set() for table in selected}
        for rel in relationships:
            left = str(rel.get("from_table") or "").strip()
            right = str(rel.get("to_table") or "").strip()
            if left in selected and right in selected:
                graph.setdefault(left, set()).add(right)
                graph.setdefault(right, set()).add(left)

        queue: deque[str] = deque(seeds)
        previous: dict[str, str | None] = {seed: None for seed in seeds}
        while queue:
            current = queue.popleft()
            if current == target_table:
                break
            for neighbor in graph.get(current, set()):
                if neighbor in previous:
                    continue
                previous[neighbor] = current
                queue.append(neighbor)

        if target_table not in previous:
            return self._unique([*tables, target_table])

        path: list[str] = []
        cursor: str | None = target_table
        while cursor is not None:
            path.append(cursor)
            cursor = previous.get(cursor)
        path.reverse()
        return self._unique([*tables, *path])

    def _unique(self, values: list[str]) -> list[str]:
        rows = []
        for value in values:
            if value and value not in rows:
                rows.append(value)
        return rows

    def _compact_feasibility(self, output: Any) -> JsonDict:
        if not isinstance(output, dict):
            return {}
        decision = output.get("feasibility_decision")
        if not isinstance(decision, dict):
            return {}
        return {
            key: decision.get(key)
            for key in (
                "execution_mode",
                "can_single_sql",
                "can_decompose",
                "needs_clarification",
                "join_risk",
                "reason",
            )
            if key in decision
        }
