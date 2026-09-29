"""Read-only natural-language analytics over ResolveOps runtime data.

This is intentionally separate from the Case Agent.  A model may compose SQL
over a small tenant-scoped semantic layer, but it never receives a database
URL, physical table access, or any ability to alter Case/ERP state.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .agent_core.contracts import LLMClient
from .llm_gateway import LLMGateway


MAX_QUERY_LENGTH = 5_000
MAX_ROWS = 100
SEMANTIC_VIEWS = {
    'analytics_cases',
    'analytics_approvals',
    'analytics_events',
    'analytics_tasks',
    'analytics_invocations',
}

SEMANTIC_SCHEMA = """Available tenant-scoped read-only relations:
- analytics_cases(case_id, event_type, order_id, status, plan_version, created_at, updated_at)
- analytics_approvals(approval_id, case_id, order_id, action_type, status, required_roles, approved_roles, expires_at, revoked_at, rejected_at)
- analytics_events(event_id, case_id, order_id, event_kind, message, created_at)
- analytics_tasks(task_id, case_id, order_id, task_kind, status, attempts, started_at, last_error)
- analytics_invocations(invocation_id, case_id, order_id, tool, status, external_id)

You may use SELECT, JOIN, WHERE, GROUP BY, ORDER BY, aggregate functions and CTEs.
Use only the relations above. Do not use physical table names or write SQL.
"""

def semantic_ctes(dialect_name: str) -> str:
    action_type = "a.action ->> 'action_type'" if dialect_name == 'postgresql' else "json_extract(a.action, '$.action_type')"
    return f"""
analytics_cases AS (
    SELECT id AS case_id, event_type, order_id, status, plan_version, created_at, updated_at
    FROM cases WHERE tenant_id = :tenant_id
),
analytics_approvals AS (
    SELECT a.id AS approval_id, a.case_id, c.order_id,
           {action_type} AS action_type,
           a.status, a.required_roles, a.approved_roles, a.expires_at, a.revoked_at, a.rejected_at
    FROM approvals a JOIN cases c ON c.id = a.case_id
    WHERE c.tenant_id = :tenant_id
),
analytics_events AS (
    SELECT e.id AS event_id, e.case_id, c.order_id, e.kind AS event_kind, e.message, e.created_at
    FROM case_events e JOIN cases c ON c.id = e.case_id
    WHERE c.tenant_id = :tenant_id
),
analytics_tasks AS (
    SELECT t.id AS task_id, t.case_id, c.order_id, t.kind AS task_kind,
           t.status, t.attempts, t.started_at, t.last_error
    FROM tasks t JOIN cases c ON c.id = t.case_id
    WHERE c.tenant_id = :tenant_id
),
analytics_invocations AS (
    SELECT i.id AS invocation_id, i.case_id, c.order_id, i.tool, i.status, i.external_id
    FROM tool_invocations i JOIN cases c ON c.id = i.case_id
    WHERE c.tenant_id = :tenant_id
)
""".strip()

_FORBIDDEN = re.compile(
    r"\b(?:insert|update|delete|merge|replace|upsert|create|alter|drop|truncate|grant|revoke|"
    r"copy|call|execute|attach|detach|pragma|vacuum|reindex|analyze|load_extension|readfile|writefile)\b",
    re.IGNORECASE,
)
_RELATION = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w$]*)", re.IGNORECASE)
_CTE = re.compile(r"(?:\bwith|,)\s*([a-zA-Z_][\w$]*)\s+as\s*\(", re.IGNORECASE)
_LIMIT = re.compile(r"\blimit\s+(\d+)\b", re.IGNORECASE)
_PHYSICAL_RELATION = re.compile(
    r"\b(?:cases|approvals|case_events|tasks|tool_invocations|audit_logs|operators|"
    r"case_lessons|price_reviews|supplier_followups|logistics_lanes|sqlite_master)\b",
    re.IGNORECASE,
)


class AnalyticsQueryError(ValueError):
    """A generated statement does not meet the analytics query contract."""


def is_analytics_question(question: str) -> bool:
    """Route operational data questions out of the general Workbench chat.

    The rule deliberately requires both an operational subject and an analysis
    cue.  A question such as "审批如何工作" remains normal chat; "哪些审批
    已过期" becomes analytics.
    """
    normalized = question.lower()
    subject = re.search(r'case|审批|任务|执行轨迹|事件|工具调用|调用记录|replan|运行记录|审计', normalized)
    analysis = re.search(r'统计|多少|哪些|最近|近\s*\d|平均|数量|趋势|分组|排序|列表|最多|最少|top|查询|汇总', normalized)
    return bool(subject and analysis)


@dataclass(frozen=True)
class AnalyticsResult:
    answer: str
    sql: str
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    llm: dict[str, Any]


def _strip_fence(value: str) -> str:
    value = value.strip()
    if value.startswith('```'):
        value = re.sub(r"^```(?:json|sql)?\s*", '', value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", '', value)
    return value.strip()


def _parse_json_object(content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise AnalyticsQueryError('模型没有返回结构化分析请求。')
    candidate = _strip_fence(content)
    try:
        result = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AnalyticsQueryError('模型返回的分析请求不是有效 JSON。') from exc
    if not isinstance(result, dict):
        raise AnalyticsQueryError('模型返回的分析请求不是对象。')
    return result


def validate_analytics_sql(sql: str) -> str:
    """Validate a model-authored read query before it reaches SQLAlchemy.

    The parser is intentionally conservative.  It permits analytical SQL and
    user-defined CTEs, but every data relation must originate from the scoped
    semantic layer declared above.
    """
    statement = _strip_fence(sql)
    if not statement or len(statement) > MAX_QUERY_LENGTH:
        raise AnalyticsQueryError('查询为空或超过长度上限。')
    if ';' in statement or '--' in statement or '/*' in statement or '*/' in statement:
        raise AnalyticsQueryError('查询不能包含多语句或 SQL 注释。')
    if not re.match(r"^(?:select|with)\b", statement, re.IGNORECASE):
        raise AnalyticsQueryError('仅允许 SELECT 查询或以 WITH 开头的查询。')
    if _FORBIDDEN.search(statement):
        raise AnalyticsQueryError('查询包含不允许的写入或数据库管理操作。')
    if _PHYSICAL_RELATION.search(statement):
        raise AnalyticsQueryError('查询不能访问底层物理表，只能使用开放的分析关系。')

    declared_ctes = {item.lower() for item in _CTE.findall(statement)}
    allowed_relations = SEMANTIC_VIEWS | declared_ctes
    relations = {item.lower() for item in _RELATION.findall(statement)}
    unknown = relations - allowed_relations
    if unknown:
        raise AnalyticsQueryError(f'查询引用了未开放的数据关系：{", ".join(sorted(unknown))}。')

    limit = _LIMIT.search(statement)
    if limit and int(limit.group(1)) > MAX_ROWS:
        raise AnalyticsQueryError(f'单次查询最多返回 {MAX_ROWS} 行。')
    if not limit:
        statement = f'{statement}\nLIMIT {MAX_ROWS}'
    return statement


def _scoped_statement(statement: str, dialect_name: str) -> str:
    scoped_ctes = semantic_ctes(dialect_name)
    if re.match(r"^with\b", statement, re.IGNORECASE):
        return f"WITH {scoped_ctes},\n{re.sub(r'^with\s+', '', statement, count=1, flags=re.IGNORECASE)}"
    return f"WITH {scoped_ctes}\n{statement}"


class AnalyticsAgent:
    """Two-call Text-to-SQL workflow over the restricted semantic layer."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMGateway()

    def run(self, *, question: str, db: Session, tenant_id: str) -> AnalyticsResult:
        generation = self.llm.chat({
            'messages': [
                {
                    'role': 'system',
                    'content': (
                        'You generate one read-only SQL query for ResolveOps operational analytics. '
                        'Return only JSON: {"sql":"...","query_intent":"..."}.\n' + SEMANTIC_SCHEMA
                    ),
                },
                {'role': 'user', 'content': question},
            ],
            'temperature': 0,
        })
        if not generation.ok:
            raise AnalyticsQueryError(f'无法生成分析查询：{generation.error_code or generation.error_type or "LLM 调用失败"}。')
        proposal = _parse_json_object((generation.first_message() or {}).get('content'))
        sql = validate_analytics_sql(str(proposal.get('sql') or ''))
        try:
            result = db.execute(text(_scoped_statement(sql, db.bind.dialect.name)), {'tenant_id': tenant_id})
            rows = [dict(row) for row in result.mappings().fetchmany(MAX_ROWS + 1)]
        except Exception as exc:
            raise AnalyticsQueryError(f'分析查询未能执行：{type(exc).__name__}。') from exc

        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        summary = self.llm.chat({
            'messages': [
                {
                    'role': 'system',
                    'content': (
                        'You summarize a read-only ResolveOps analytics result in concise Chinese. '
                        'Use only the supplied rows; do not invent facts. Mention when the result is empty or truncated.'
                    ),
                },
                {'role': 'user', 'content': json.dumps({
                    'question': question, 'query_intent': proposal.get('query_intent') or '',
                    'rows': rows, 'truncated': truncated,
                }, ensure_ascii=False, default=str)},
            ],
            'temperature': 0,
        })
        answer = (summary.first_message() or {}).get('content') if summary.ok else None
        if not isinstance(answer, str) or not answer.strip():
            answer = f'查询返回 {len(rows)} 行结果。'
            if truncated:
                answer += f' 页面仅展示前 {MAX_ROWS} 行。'
        return AnalyticsResult(
            answer=answer.strip(), sql=sql, rows=rows, row_count=len(rows), truncated=truncated,
            llm={'sql_generation': generation.telemetry(), 'answer_summary': summary.telemetry()},
        )
