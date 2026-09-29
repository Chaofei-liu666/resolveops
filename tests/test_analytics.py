from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from production.agent_core import LLMResult
from production.analytics import AnalyticsAgent, AnalyticsQueryError, validate_analytics_sql
from production.models import Base, Case


class AnalyticsLLM:
    def __init__(self):
        self.payloads = []

    def chat(self, payload):
        self.payloads.append(payload)
        if len(self.payloads) == 1:
            return LLMResult(status='success', response={'choices': [{'message': {'content': (
                '{"sql":"WITH recent AS (SELECT event_type, status FROM analytics_cases) '
                'SELECT event_type, count(*) AS total FROM recent GROUP BY event_type",'
                '"query_intent":"按异常类型统计"}'
            )}}]})
        return LLMResult(status='success', response={'choices': [{'message': {'content': '库存短缺 Case 共 1 条。'}}]})


def test_analytics_sql_allows_select_cte_and_adds_limit():
    sql = validate_analytics_sql(
        'WITH recent AS (SELECT event_type FROM analytics_cases) SELECT event_type, count(*) AS total FROM recent GROUP BY event_type'
    )
    assert sql.endswith('LIMIT 100')


def test_analytics_sql_rejects_raw_tables_and_writes():
    for statement in ('SELECT * FROM cases', 'DELETE FROM analytics_cases', 'SELECT * FROM analytics_cases; SELECT 1'):
        try:
            validate_analytics_sql(statement)
        except AnalyticsQueryError:
            continue
        raise AssertionError(f'expected statement to be rejected: {statement}')


def test_analytics_agent_scopes_rows_to_operator_tenant():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            Case(id='demo-case', tenant_id='demo', event_type='inventory_shortage', order_id='SO-1'),
            Case(id='other-case', tenant_id='other', event_type='delivery_delay', order_id='SO-2'),
        ])
        db.commit()
        llm = AnalyticsLLM()
        result = AnalyticsAgent(llm).run(question='按异常类型统计', db=db, tenant_id='demo')

    assert result.rows == [{'event_type': 'inventory_shortage', 'total': 1}]
    assert result.answer == '库存短缺 Case 共 1 条。'
    assert len(llm.payloads) == 2
