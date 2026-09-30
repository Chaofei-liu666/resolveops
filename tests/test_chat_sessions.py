from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi import HTTPException

from production import main as main_module
from production.main import (
    ChatSessionCreateIn,
    ChatSessionDeleteIn,
    OperatorChatIn,
    _first_case_id,
    create_chat_session,
    get_chat_session,
    operator_chat,
    persist_chat_memory,
    remove_chat_session,
)
from production.models import Base, Case, ChatMemory, ChatMessage, Operator
from production.local_file_tool import is_local_file_question


class FakeOperatorChatAgent:
    def answer(self, question, history=None, attachments=None, memories=None):
        return {
            'question': question,
            'answer': f'答复：{question}',
            'source': 'fake',
            'tools_used': [],
            'llm': {'status': 'fake'},
            'memory_ids': [item['id'] for item in memories or []],
        }

    def select_relevant_memories(self, question, candidates):
        return [item['id'] for item in candidates if '偏好' in question]

    def plan_local_file_reads(self, question, candidates):
        return []

    def summarize_memory(self, title, messages):
        return f'{title}：已确认的长期偏好。'


def _seed(engine):
    with Session(engine) as db:
        db.add_all([
            Operator(tenant_id='demo', subject='alice', role='ops_admin', api_key_hash=main_module.operator_key_hash('alice-key'), status='active'),
            Operator(tenant_id='demo', subject='bob', role='ops_admin', api_key_hash=main_module.operator_key_hash('bob-key'), status='active'),
        ])
        db.commit()


def test_chat_session_persists_messages_and_isolated_by_operator(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main_module, 'engine', engine)
    monkeypatch.setattr(main_module, 'OperatorChatAgent', FakeOperatorChatAgent)
    _seed(engine)

    created = create_chat_session(ChatSessionCreateIn(), x_operator_key='alice-key')
    result = operator_chat(OperatorChatIn(question='记录这个决定', session_id=created['id']), x_operator_key='alice-key')

    assert result['session']['message_count'] == 2
    restored = get_chat_session(created['id'], x_operator_key='alice-key')
    assert [item['role'] for item in restored['messages']] == ['user', 'assistant']
    assert restored['title'] == '记录这个决定'
    try:
        get_chat_session(created['id'], x_operator_key='bob-key')
        assert False, 'another operator must not read this session'
    except HTTPException as exc:
        assert exc.status_code == 404


def test_persisted_memory_is_separate_from_messages_and_delete_can_keep_it(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main_module, 'engine', engine)
    monkeypatch.setattr(main_module, 'OperatorChatAgent', FakeOperatorChatAgent)
    _seed(engine)

    created = create_chat_session(ChatSessionCreateIn(title='偏好'), x_operator_key='alice-key')
    operator_chat(OperatorChatIn(question='我的偏好是简洁回答', session_id=created['id']), x_operator_key='alice-key')
    persisted = persist_chat_memory(created['id'], x_operator_key='alice-key')
    assert persisted['memory_saved'] is True
    with Session(engine) as db:
        assert db.scalar(ChatMemory.__table__.select().where(ChatMemory.session_id == created['id'])) is not None

    remove_chat_session(created['id'], ChatSessionDeleteIn(delete_memory=False), x_operator_key='alice-key')
    with Session(engine) as db:
        assert db.scalar(ChatMessage.__table__.select().where(ChatMessage.session_id == created['id'])) is None
        assert db.scalar(ChatMemory.__table__.select().where(ChatMemory.session_id == created['id'])) is not None


def test_main_chat_routes_a_case_id_to_tenant_scoped_read_only_answer(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main_module, 'engine', engine)
    monkeypatch.setattr(main_module, 'OperatorChatAgent', FakeOperatorChatAgent)

    class FakeCaseQuestionAgent:
        def __init__(self, _tools):
            pass

        def answer(self, **_kwargs):
            return {'answer': 'Case 已通过只读工具查询。', 'used_tools': ['get_order'], 'observations': []}

    monkeypatch.setattr(main_module, 'CaseQuestionAgent', FakeCaseQuestionAgent)
    _seed(engine)
    with Session(engine) as db:
        db.add(Case(id='11a11111-1111-4111-8111-111111111111', tenant_id='demo', event_type='inventory_shortage', order_id='SO-1'))
        db.commit()

    created = create_chat_session(ChatSessionCreateIn(), x_operator_key='alice-key')
    result = operator_chat(OperatorChatIn(
        question='11a11111-1111-4111-8111-111111111111 这个 Case 怎么样？', session_id=created['id'],
    ), x_operator_key='alice-key')

    assert result['route'] == 'case'
    assert result['case_id'] == '11a11111-1111-4111-8111-111111111111'
    assert result['used_tools'] == ['get_order']


def test_case_id_parser_accepts_chinese_punctuation_and_parentheses():
    case_id = '11a11111-1111-4111-8111-111111111111'
    assert _first_case_id(f'查看{case_id}的处理链路') == case_id
    assert _first_case_id(f'（{case_id}）现在怎么样？') == case_id
    assert _first_case_id(f'Case：{case_id}。') == case_id


def test_main_chat_routes_an_unambiguous_order_reference_to_case(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main_module, 'engine', engine)
    monkeypatch.setattr(main_module, 'OperatorChatAgent', FakeOperatorChatAgent)

    class FakeCaseQuestionAgent:
        def __init__(self, _tools):
            pass

        def answer(self, **_kwargs):
            return {'answer': '订单已通过只读工具查询。', 'used_tools': [], 'observations': []}

    monkeypatch.setattr(main_module, 'CaseQuestionAgent', FakeCaseQuestionAgent)
    _seed(engine)
    with Session(engine) as db:
        db.add(Case(id='22a22222-2222-4222-8222-222222222222', tenant_id='demo', event_type='delivery_delay', order_id='SAL-ORD-2026-00002'))
        db.commit()

    created = create_chat_session(ChatSessionCreateIn(), x_operator_key='alice-key')
    result = operator_chat(OperatorChatIn(
        question='SAL-ORD-2026-00002 的审批和执行轨迹是什么？', session_id=created['id'],
    ), x_operator_key='alice-key')

    assert result['route'] == 'case'
    assert result['case_id'] == '22a22222-2222-4222-8222-222222222222'


def test_local_file_router_does_not_scan_for_feature_questions():
    assert not is_local_file_question('本地文件工具如何实现？')
    assert not is_local_file_question('我想读取电脑上的文件并做总结，该怎么问？')
    assert is_local_file_question('帮我读取电脑下载目录中的报价单并总结。')
