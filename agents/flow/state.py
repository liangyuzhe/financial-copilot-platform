"""LangGraph 图共享状态定义。"""

from typing import Annotated, Any, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages
from langchain_core.documents import Document


def latest_non_empty(current: str | None, incoming: str | None) -> str:
    """Use the latest non-empty state value.

    LangGraph checkpointers keep state by ``thread_id``. New user turns must be
    able to replace the previous turn's query, while resume calls with no new
    value should keep the existing one.
    """
    return incoming or current or ""


class RAGChatState(TypedDict):
    """RAG Chat 图的状态。"""
    input: dict                                  # {"session_id": str, "query": str, "rag_mode"?: str}
    session_id: str
    query: str
    rag_mode: str                                # "traditional" or "parent"
    session: dict                                # Session 数据
    rewritten_query: str                         # 重写后的查询
    docs: list[Document]                         # 检索到的文档
    messages: Annotated[list[BaseMessage], add_messages]  # 对话消息
    answer: str                                  # 最终回答


class SQLReactState(TypedDict):
    """SQL React 图的状态。"""
    query: Annotated[str, latest_non_empty]      # 当前用户问题（可能是代词化的）
    session_id: str                              # 前端/API 会话 ID
    rewritten_query: Annotated[str, latest_non_empty]  # 上下文化后的独立问题
    enhanced_query: str                          # 业务术语增强后的查询
    chat_history: list[dict]                     # 对话历史 [{"role": str, "content": str}]
    table_names: list[str]                       # 所有可用表名（启动时缓存）
    selected_tables: list[str]                   # LLM 选中的相关表名
    table_metadata: dict                         # {table_name: display/business description}
    table_relationships: list[dict]              # 表关系 [{from_table, from_column, to_table, to_column}]
    security_context: dict                       # 当前用户、角色和数据权限上下文
    authorization_report: dict                   # 表/SQL 权限检查结果
    route_mode: str                              # single_sql | single_sql_with_strict_checks | complex_plan | clarify
    route_reason: str                            # 路由原因
    feasibility_decision: dict                   # execution_mode/task_type/can_decompose/join_risk 等可行性评估
    complexity_report: dict                      # 表数、关系数、估算 JOIN 数等复杂度信息
    complex_plan: dict                           # 复杂查询执行计划
    plan_validation_error: str                   # 复杂计划校验错误
    plan_approved: bool                          # 复杂计划是否已确认
    plan_current_step: int                       # 当前计划步骤编号
    plan_execution_results: dict                 # step_id -> result/error/sql
    agentscope_result: dict                      # AgentScope 内部规划观测结果，不作为执行事实
    agentscope_observation: dict                 # AgentScope 内部运行摘要，用于 trace/排障
    evidence: list[str]                          # 业务知识检索结果
    few_shot_examples: list[str]                 # SQL Q&A few-shot 参考
    recall_context: dict                         # 单次召回后的结构化上下文，供后续节点复用
    docs: list[Document]                         # 检索到的表结构（从 t_semantic_model 构建）
    semantic_model: dict                         # {table_name: {column_name: row_dict}}
    sql: str                                     # 生成的 SQL
    is_sql: bool                                 # 是否为 SQL 输出
    answer: str                                  # 非 SQL 回答
    approved: bool                               # 是否已审批
    refine_feedback: str                         # 修改意见（用户拒绝或错误分析生成）
    result: str                                  # SQL 执行结果
    safety_report: dict | None                   # 安全分析报告
    semantic_report: dict | None                 # SQL 语义一致性校验报告
    dry_run_report: dict | None                  # SQL EXPLAIN / dry-run 预执行报告
    result_sanity_report: dict | None            # SQL 执行结果 sanity check 报告
    error: str | None                            # SQL 执行错误信息
    retry_count: int                             # 重试次数
    execution_history: list[dict]                # 执行历史 [{sql, result, error}]
    reflection_notice: str                       # 结果异常反思提示


class AnalystState(TypedDict):
    """数据分析图的状态。"""
    sql_result: str
    parsed_data: dict                            # ParsedData
    statistics: dict                             # Statistics
    text_analysis: str
    chart_config: dict
    analysis_result: dict                        # AnalysisResult


class FinalGraphState(TypedDict):
    """主调度图的状态。"""
    query: Annotated[str, latest_non_empty]
    session_id: str
    chat_history: list[dict]                     # 对话历史 [{"role": str, "content": str}]
    security_context: dict                       # 当前用户、角色和数据权限上下文
    route: str                                   # data | chat | clarify
    route_confidence: float
    route_reason: str
    intent: str                                  # 兼容字段，逐步废弃
    rewritten_query: Annotated[str, latest_non_empty]  # classify 或前端预分类产出的独立查询
    analysis_plan: dict
    complex_plan: dict
    plan_validation_error: str
    plan_approved: bool
    plan_current_step: int
    plan_execution_results: dict
    enhanced_query: str
    selected_tables: list[str]
    table_metadata: dict
    table_relationships: list[dict]
    semantic_model: dict
    evidence: list[str]
    few_shot_examples: list[str]
    recall_context: dict
    agentscope_result: dict
    agentscope_observation: dict
    sql: str
    is_sql: bool
    result: str
    answer: str
    error: str | None
    status: str                                  # "pending" | "approved" | "rejected" | "completed"
