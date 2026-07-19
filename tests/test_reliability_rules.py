import os
from datetime import UTC, date, datetime, timedelta

os.environ.setdefault('ERPNEXT_BASE_URL', 'https://erp.invalid')
os.environ.setdefault('ERPNEXT_API_KEY', 'test')
os.environ.setdefault('ERPNEXT_API_SECRET', 'test')
os.environ.setdefault('WEBHOOK_SECRET', 'test')
os.environ.setdefault('OPERATOR_API_KEY', 'test')

import pytest

from production.actions import action_tool_spec, action_types_for_case, normalize_plan, normalize_proposal, planner_action_catalog, planner_action_instructions, registered_action_types
import production.agent as agent_module
from production.agent import InvestigationAgent
from production.evidence import validate_plan_grounding
from production.executors import executor_for
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from production.context import CaseContextBuilder, build_case_context, validate_case_context_isolation
from production.main import LogisticsLaneIn, OperatorIdentity, audit_out, case_tool_trace, eval_case_out, eval_summary_out, operator_identity_from_db, operator_key_hash, require_role
from production.memory import candidate_lessons_from_verified_action, record_verified_lessons, relevant_lessons_for_case
import production.migrations as migration_module
from production.migrations import apply_migrations
from production.models import Approval, AuditLog, Base, Case, CaseLesson, Event, Invocation, Operator, PriceReview, SupplierFollowup, Task
from production.llm_gateway import LLMGateway, LLMResult
from production.policy import action_policy, allow_read_tool
from production.tool_result import ToolResult
from production.tool_scheduler import ReadToolCall, ReadToolScheduler, tool_signature
from production.tools import BusinessReadTools, ToolSpec, summarize_customer_profile
from production.tool_trace import build_tool_trace
from production.worker import digest, execute


def future_required_by(days: int = 10) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def test_illegal_action_plan_cannot_be_normalized():
    with pytest.raises(ValueError):
        normalize_proposal({'action_type': 'transfer_stock', 'input': {'sku': 'A'}}, 'bad model output')


def test_unregistered_action_type_is_rejected():
    with pytest.raises(ValueError):
        normalize_proposal({'action_type': 'delete_sales_order', 'input': {}}, 'unsafe request')


def test_read_tool_cannot_escape_warehouse_scope():
    order = {'items': [{'warehouse': '成都仓 - ROPS'}]}
    allowed, reason = allow_read_tool('get_inventory', {'warehouse': '海外仓', 'item_code': 'SKU-A12'}, order)
    assert not allowed
    assert reason == 'warehouse_out_of_scope'


def test_high_amount_requires_two_roles():
    plan = {'action_type': 'transfer_stock'}
    evidence = {'observations': [{'tool': 'get_order', 'result': {'grand_total': 150000}}, {'tool': 'get_customer_profile', 'result': {}}]}
    decision = action_policy(plan, evidence)
    assert decision['allowed']
    assert set(decision['required_roles']) == {'warehouse_manager', 'sales_manager'}


def test_purchase_request_has_a_strict_action_contract():
    action = normalize_proposal({
        'action_type': 'create_purchase_request',
        'input': {'target': 'Stores - ROPS', 'sku': 'SKU-A12', 'quantity': 30, 'required_by': '2026-07-20'},
    }, 'No safe transfer source remains.')
    assert action['executable']
    assert action['execution']['executor'] == 'erp.create_purchase_request'
    assert action['tool']['llm_callable'] is False
    assert action['tool']['requires_approval'] is True
    assert action['tool']['idempotency_required'] is True


def test_purchase_request_requires_procurement_approval():
    plan = {'action_type': 'create_purchase_request'}
    evidence = {'observations': [{'tool': 'get_order', 'result': {'grand_total': 1}}, {'tool': 'get_customer_profile', 'result': {}}]}
    assert action_policy(plan, evidence)['required_roles'] == ['procurement_manager']


def test_price_review_action_has_strict_contract_and_dual_approval():
    action = normalize_proposal({
        'action_type': 'create_price_review_ticket',
        'input': {
            'sku': 'SKU-A12',
            'order_rate': 5000,
            'reference_rate': 4500,
            'difference': 500,
            'reason': 'Sales Order rate is higher than reference price.',
        },
    }, 'Price mismatch is supported by order and reference price evidence.')
    assert action['executable']
    assert action['execution']['executor'] == 'resolveops.create_price_review_ticket'
    assert action['tool']['llm_callable'] is False
    assert action['tool']['risk_level'] == 'high'
    decision = action_policy({'action_type': 'create_price_review_ticket'}, {'observations':[{'tool':'get_order','result':{'grand_total':150000}}]})
    assert decision['allowed']
    assert decision['required_roles'] == ['sales_manager', 'finance_manager']


def test_supplier_followup_action_has_strict_contract_and_policy():
    action = normalize_proposal({
        'action_type': 'create_supplier_followup_task',
        'input': {
            'sku': 'SKU-A12',
            'purchase_order': 'PO-1',
            'supplier': 'Supplier A',
            'expected_delivery_date': '2026-07-25',
            'delayed_by_days': 5,
            'reason': 'Inbound purchase is later than customer delivery date.',
        },
    }, 'Delivery delay is supported by inbound purchase evidence.')
    assert action['executable']
    assert action['execution']['executor'] == 'resolveops.create_supplier_followup_task'
    assert action['tool']['llm_callable'] is False
    decision = action_policy({'action_type':'create_supplier_followup_task'}, {'observations':[{'tool':'get_order','result':{'grand_total':150000}}]})
    assert decision['allowed']
    assert decision['required_roles'] == ['procurement_manager', 'sales_manager']


def test_policy_tolerates_empty_customer_group():
    plan = {'action_type': 'transfer_stock'}
    evidence = {'observations': [{'tool': 'get_order', 'result': {'grand_total': 1}}, {'tool': 'get_customer_profile', 'result': {'customer_group': None}}]}
    assert action_policy(plan, evidence)['required_roles'] == ['warehouse_manager']


def test_plan_can_coordinate_transfer_and_purchase_without_hardcoding_it():
    plan = normalize_plan([
        {'action_type': 'transfer_stock', 'input': {'source': '重庆仓 - ROPS', 'target': 'Stores - ROPS', 'sku': 'SKU-A12', 'quantity': 10}},
        {'action_type': 'create_purchase_request', 'input': {'target': 'Stores - ROPS', 'sku': 'SKU-A12', 'quantity': 20, 'required_by': '2026-07-20'}},
    ], 'Combined action is preferred for this evidence set.')
    assert [action['action_type'] for action in plan['actions']] == ['transfer_stock', 'create_purchase_request']
    assert len({action['action_id'] for action in plan['actions']}) == 2


def grounded_observations():
    required_by = future_required_by()
    return [
        {'tool':'get_order','arguments':{},'result':{
            'name':'SO-1',
            'delivery_date': required_by,
            'items':[{'item_code':'SKU-A12','warehouse':'Stores - ROPS','qty':30}],
        }},
        {'tool':'get_inventory','arguments':{'item_code':'SKU-A12','warehouse':'Stores - ROPS'},'result':{
            'warehouse':'Stores - ROPS','item_code':'SKU-A12','actual_qty':0,'reserved_qty':0,
        }},
        {'tool':'get_inventory','arguments':{'item_code':'SKU-A12','warehouse':'重庆仓 - ROPS'},'result':{
            'warehouse':'重庆仓 - ROPS','item_code':'SKU-A12','actual_qty':10,'reserved_qty':0,
        }},
        {'tool':'get_transfer_options','arguments':{'item_code':'SKU-A12','target':'Stores - ROPS'},'result':{
            'lanes':[{'source':'重庆仓 - ROPS','target':'Stores - ROPS','transit_days':2,'cost_per_unit':8,'currency':'CNY'}],
        }},
        {'tool':'get_item_supply_profile','arguments':{'item_code':'SKU-A12'},'result':{
            'item_code':'SKU-A12','lead_time_days':3,
        }},
        {'tool':'get_inbound_purchase','arguments':{'item_code':'SKU-A12'},'result':{
            'purchase_items':[],
        }},
    ]


def test_evidence_grounding_accepts_supported_multi_action_plan():
    required_by = future_required_by()
    plan = normalize_plan([
        {'action_type':'transfer_stock','input':{'source':'重庆仓 - ROPS','target':'Stores - ROPS','sku':'SKU-A12','quantity':10}},
        {'action_type':'create_purchase_request','input':{'target':'Stores - ROPS','sku':'SKU-A12','quantity':20,'required_by': required_by}},
    ], 'grounded')
    result = validate_plan_grounding(plan, grounded_observations())
    assert result['allowed']
    assert result['shortage'] == 30
    assert result['covered_quantity'] == 30


def test_evidence_grounding_rejects_transfer_without_lane_evidence():
    required_by = future_required_by()
    plan = normalize_plan([
        {'action_type':'transfer_stock','input':{'source':'重庆仓 - ROPS','target':'Stores - ROPS','sku':'SKU-A12','quantity':10}},
        {'action_type':'create_purchase_request','input':{'target':'Stores - ROPS','sku':'SKU-A12','quantity':20,'required_by': required_by}},
    ], 'not grounded')
    observations = [item for item in grounded_observations() if item['tool'] != 'get_transfer_options']
    result = validate_plan_grounding(plan, observations)
    assert not result['allowed']
    assert any('missing transfer lane evidence' in problem for problem in result['problems'])


def price_mismatch_observations():
    return [
        {'tool':'get_order','arguments':{},'result':{
            'name':'SO-PRICE-1',
            'grand_total':150000,
            'items':[{'item_code':'SKU-A12','warehouse':'Stores - ROPS','qty':30,'rate':5000}],
        }},
        {'tool':'get_reference_price','arguments':{'item_code':'SKU-A12'},'result':{
            'item_code':'SKU-A12',
            'reference_rate':4500,
            'price_list':'Standard Selling',
            'currency':'CNY',
        }},
    ]


def test_price_mismatch_grounding_accepts_supported_review_action():
    plan = normalize_plan([
        {'action_type':'create_price_review_ticket','input':{
            'sku':'SKU-A12',
            'order_rate':5000,
            'reference_rate':4500,
            'difference':500,
            'reason':'Order rate exceeds reference price.',
        }},
    ], 'grounded price mismatch')
    result = validate_plan_grounding(plan, price_mismatch_observations(), 'price_mismatch')
    assert result['allowed']
    assert result['case_type'] == 'price_mismatch'


def test_tool_trace_links_observations_to_supported_action():
    plan = normalize_plan([
        {'action_type':'create_price_review_ticket','input':{
            'sku':'SKU-A12',
            'order_rate':5000,
            'reference_rate':4500,
            'difference':500,
            'reason':'Order rate exceeds reference price.',
        }},
    ], 'grounded price mismatch')
    grounding = validate_plan_grounding(plan, price_mismatch_observations(), 'price_mismatch')
    trace = build_tool_trace(price_mismatch_observations(), plan, grounding)
    action_id = plan['actions'][0]['action_id']

    assert trace['summary']['observation_count'] == 2
    assert trace['summary']['failed_observation_count'] == 0
    assert trace['summary']['grounding_allowed'] is True
    assert trace['action_evidence'][action_id] == ['E-001', 'E-002']
    assert trace['observations'][1]['tool'] == 'get_reference_price'
    assert trace['observations'][1]['result_summary']['reference_rate'] == 4500
    assert trace['observations'][1]['supports_actions'] == [action_id]


def test_case_tool_trace_is_derived_for_legacy_case_without_stored_trace():
    plan = normalize_plan([
        {'action_type':'create_price_review_ticket','input':{
            'sku':'SKU-A12',
            'order_rate':5000,
            'reference_rate':4500,
            'difference':500,
        }},
    ], 'grounded price mismatch')
    plan['evidence_grounding'] = {'allowed': True, 'reason': 'grounded'}
    case = Case(id='case-trace', tenant_id='demo', event_type='price_mismatch', order_id='SO-PRICE-1', plan=plan, evidence={'observations':price_mismatch_observations()})

    trace = case_tool_trace(case)

    assert trace['summary']['tools_used'] == ['get_order', 'get_reference_price']
    assert trace['summary']['grounding_allowed'] is True
    assert trace['action_evidence'][plan['actions'][0]['action_id']] == ['E-001', 'E-002']


def test_price_mismatch_grounding_rejects_missing_reference_price():
    plan = normalize_plan([
        {'action_type':'create_price_review_ticket','input':{
            'sku':'SKU-A12',
            'order_rate':5000,
            'reference_rate':4500,
            'difference':500,
        }},
    ], 'not grounded')
    observations = [item for item in price_mismatch_observations() if item['tool'] != 'get_reference_price']
    result = validate_plan_grounding(plan, observations, 'price_mismatch')
    assert not result['allowed']
    assert any('missing reference price evidence' in problem for problem in result['problems'])


def delivery_delay_observations():
    return [
        {'tool':'get_order','arguments':{},'result':{
            'name':'SO-DELAY-1',
            'delivery_date':'2026-07-20',
            'grand_total':150000,
            'items':[{'item_code':'SKU-A12','warehouse':'Stores - ROPS','qty':30,'delivery_date':'2026-07-20'}],
        }},
        {'tool':'get_inbound_purchase','arguments':{'item_code':'SKU-A12'},'result':{
            'purchase_items':[{'purchase_order':'PO-1','supplier':'Supplier A','remaining_qty':30,'schedule_date':'2026-07-25','status':'To Receive'}],
        }},
        {'tool':'get_item_supply_profile','arguments':{'item_code':'SKU-A12'},'result':{
            'item_code':'SKU-A12','lead_time_days':3,
        }},
    ]


def test_delivery_delay_grounding_accepts_supported_supplier_followup():
    plan = normalize_plan([
        {'action_type':'create_supplier_followup_task','input':{
            'sku':'SKU-A12',
            'purchase_order':'PO-1',
            'supplier':'Supplier A',
            'expected_delivery_date':'2026-07-25',
            'delayed_by_days':5,
        }},
    ], 'grounded delivery delay')
    result = validate_plan_grounding(plan, delivery_delay_observations(), 'delivery_delay')
    assert result['allowed']
    assert result['case_type'] == 'delivery_delay'


def test_delivery_delay_grounding_rejects_non_late_inbound_purchase():
    observations = delivery_delay_observations()
    observations[1]['result']['purchase_items'][0]['schedule_date'] = '2026-07-19'
    plan = normalize_plan([
        {'action_type':'create_supplier_followup_task','input':{
            'sku':'SKU-A12',
            'purchase_order':'PO-1',
            'supplier':'Supplier A',
            'expected_delivery_date':'2026-07-19',
            'delayed_by_days':1,
        }},
    ], 'not grounded')
    result = validate_plan_grounding(plan, observations, 'delivery_delay')
    assert not result['allowed']
    assert any('not later than customer delivery date' in problem for problem in result['problems'])


def test_known_model_schema_variant_is_normalized_before_policy_checks():
    result = InvestigationAgent._parse_conclusion('{"status":"shortage","recommended_actions":[{"action":"transfer_stock","input":{}}],"alternatives":[],"rationale":"x","missing_information":[]}')
    assert result['status'] == 'ready'
    assert result['recommended_actions'][0]['action_type'] == 'transfer_stock'


def test_known_model_tool_field_variant_is_normalized_before_policy_checks():
    result = InvestigationAgent._parse_conclusion('{"status":"ready","recommended_actions":[{"tool":"create_purchase_request","input":{}}],"alternatives":[],"rationale":"x","missing_information":[]}')
    assert result['recommended_actions'][0]['action_type'] == 'create_purchase_request'


def test_tool_error_is_unknown_not_negative_fact():
    conclusion = InvestigationAgent._parse_conclusion('not json')
    assert conclusion['status'] == 'handoff'
    assert 'schema' in conclusion['rationale'].lower()


def test_tool_budget_exhaustion_is_preserved_as_missing_information():
    class FakeGateway:
        def chat(self, _payload):
            return LLMResult(status='success', response={
                'choices':[{'message':{'content':'{"status":"ready","recommended_actions":[],"alternatives":[],"rationale":"enough evidence","missing_information":[],"evidence_summary":[]}'}}],
                'usage': {'total_tokens': 10},
            }, model='fake-model', latency_ms=1, usage={'total_tokens': 10})

    conclusion = InvestigationAgent(None, llm_gateway=FakeGateway())._plan(
        'SO-1',
        [{'tool':'get_order','arguments':{},'result':{'name':'SO-1'}}],
        [],
        '',
        budget_exhausted=True,
    )
    assert 'read-tool budget exhausted; plan uses only collected evidence' in conclusion['missing_information']
    assert conclusion['llm']['usage']['total_tokens'] == 10


def test_llm_gateway_normalizes_success_usage_and_message(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'choices':[{'message':{'content':'{"ok":true}'}}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}

    monkeypatch.setattr('production.llm_gateway.httpx.post', lambda *args, **kwargs: Response())
    result = LLMGateway(base_url='https://llm.test/v1', api_key='key', model='model-x').chat({'messages':[]})

    assert result.ok is True
    assert result.model == 'model-x'
    assert result.first_message()['content'] == '{"ok":true}'
    assert result.telemetry()['usage']['total_tokens'] == 5


def test_llm_gateway_failure_is_retryable_for_timeout(monkeypatch):
    import httpx

    def raise_timeout(*_args, **_kwargs):
        raise httpx.TimeoutException('timeout')

    monkeypatch.setattr('production.llm_gateway.httpx.post', raise_timeout)
    result = LLMGateway(base_url='https://llm.test/v1', api_key='key', model='model-x').chat({'messages':[]})

    assert result.ok is False
    assert result.error_code == 'llm_timeout'
    assert result.retryable is True


def test_customer_custom_field_is_normalized_as_business_evidence():
    profile = summarize_customer_profile({
        'customer_name': 'Acme',
        'customer_group': 'Commercial',
        'custom_\u5141\u8bb8\u62c6\u5355\u53d1\u8d27': 1,
    })
    assert profile['allows_partial_delivery'] is True
    assert profile['evidence_fields']['allows_partial_delivery'] == 'custom_\u5141\u8bb8\u62c6\u5355\u53d1\u8d27'


def test_tool_spec_generates_llm_function_schema():
    spec = ToolSpec(
        name='get_inventory',
        description='Read inventory.',
        llm_description='Read stock.',
        parameters={'type':'object','properties':{'sku':{'type':'string'}},'required':['sku'],'additionalProperties':False},
        permission='inventory:read',
        side_effect='none',
        risk_level='low',
        source_system='WMSAdapter',
        executor=lambda _args, _order_id: {},
    )
    schema = spec.to_openai_tool()
    assert schema['type'] == 'function'
    assert schema['function']['name'] == 'get_inventory'
    assert schema['function']['description'] == 'Read stock.'
    assert schema['function']['parameters']['additionalProperties'] is False
    assert spec.metadata()['source_system'] == 'WMSAdapter'


def test_llm_tool_schema_excludes_runtime_governance_metadata():
    class FakeAdapter:
        pass

    tool = next(item for item in BusinessReadTools(FakeAdapter()).definitions() if item['function']['name'] == 'get_inventory')
    payload = tool['function']
    assert set(payload.keys()) == {'name', 'description', 'parameters'}
    dumped = str(tool)
    assert 'permission' not in dumped
    assert 'side_effect' not in dumped
    assert 'source_system' not in dumped
    assert 'requires_approval' not in dumped
    assert 'verification' not in dumped
    assert len(payload['description']) <= 80


def test_business_read_tools_expose_business_names_not_erpnext_doctypes():
    class FakeAdapter:
        pass

    names = {item['function']['name'] for item in BusinessReadTools(FakeAdapter()).definitions()}
    assert {'get_order', 'get_inventory', 'get_customer_profile'} <= names
    assert not {'Sales Order', 'Bin', 'Stock Entry', 'Material Request'} & names


def test_read_tool_profile_limits_llm_visible_tools_by_case_type():
    class FakeAdapter:
        pass

    inventory_names = {item['function']['name'] for item in BusinessReadTools(FakeAdapter(), 'inventory_shortage').definitions()}
    price_names = {item['function']['name'] for item in BusinessReadTools(FakeAdapter(), 'price_mismatch').definitions()}
    delivery_names = {item['function']['name'] for item in BusinessReadTools(FakeAdapter(), 'delivery_delay').definitions()}
    assert {'get_order', 'get_inventory', 'get_transfer_options', 'get_inbound_purchase'} <= inventory_names
    assert price_names == {'get_order', 'get_reference_price', 'get_customer_profile'}
    assert delivery_names == {'get_order', 'get_inbound_purchase', 'get_item_supply_profile', 'get_customer_profile'}
    assert not {'get_inventory', 'get_transfer_options', 'get_inbound_purchase', 'get_item_supply_profile'} & price_names
    assert 'get_inventory' not in delivery_names
    denied = BusinessReadTools(FakeAdapter(), 'price_mismatch').execute_result('get_inventory', {'item_code':'SKU-A12','warehouse':'Stores - ROPS'}, 'SO-1')
    assert denied.to_dict()['error_code'] == 'tool_not_enabled_for_case_type'


def test_business_read_tool_metadata_exposes_runtime_boundaries():
    class FakeAdapter:
        pass

    metadata = BusinessReadTools(FakeAdapter()).metadata('get_inventory')
    assert metadata['permission'] == 'inventory:read'
    assert metadata['side_effect'] == 'none'
    assert metadata['risk_level'] == 'low'
    assert metadata['source_system'] == 'ERPNextAdapter'


def test_tool_result_preserves_business_data_and_runtime_status():
    result = ToolResult.success({'actual_qty': 10}, source_system='WMSAdapter')
    assert result.observation_result() == {'actual_qty': 10}
    assert result.to_dict()['status'] == 'success'
    failed = ToolResult.failure('warehouse_out_of_scope', retryable=False, source_system='WMSAdapter')
    assert failed.observation_result()['error'] == 'warehouse_out_of_scope'
    assert failed.to_dict()['evidence_usable'] is False


def test_read_tool_scheduler_deduplicates_batch_calls_and_reuses_cache():
    class FakeTools:
        def __init__(self):
            self.calls = []

        def execute_result(self, name, arguments, order_id):
            self.calls.append((name, arguments, order_id))
            return ToolResult.success({'name': name, 'arguments': arguments, 'order_id': order_id}, source_system='FakeAdapter')

    tools = FakeTools()
    scheduler = ReadToolScheduler(tools, max_workers=4)
    seen = {}
    calls = [
        ReadToolCall('call-1', 'get_order', {}),
        ReadToolCall('call-2', 'get_order', {}),
        ReadToolCall('call-3', 'get_customer_profile', {'customer_id': 'CUST-1'}),
    ]

    first = scheduler.execute_batch(calls, 'SO-1', seen)
    second = scheduler.execute_batch([ReadToolCall('call-4', 'get_order', {})], 'SO-1', seen)

    assert len(tools.calls) == 2
    assert [item.result.status for item in first] == ['success', 'success', 'success']
    assert first[0].result.data == first[1].result.data
    assert second[0].source == 'cache'
    assert tool_signature('get_order', {}) in seen


def test_read_tool_scheduler_converts_runtime_exceptions_to_tool_result():
    class ExplodingTools:
        def execute_result(self, _name, _arguments, _order_id):
            raise TimeoutError('upstream timeout')

    result = ReadToolScheduler(ExplodingTools(), max_workers=2).execute_batch(
        [ReadToolCall('call-1', 'get_order', {})],
        'SO-1',
        {},
    )[0].result

    assert result.status == 'failed'
    assert result.error_code == 'tool_scheduler_failed'
    assert result.error_type == 'TimeoutError'
    assert result.retryable is True


def test_sql_migration_runner_records_versions_idempotently(tmp_path, monkeypatch):
    (tmp_path / '0001_test_migration.sql').write_text('CREATE TABLE example_migrated(id INTEGER PRIMARY KEY);', encoding='utf-8')
    monkeypatch.setattr(migration_module, 'MIGRATIONS_DIR', tmp_path)
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as db:
        Base.metadata.create_all(db)
        first = apply_migrations(db)
        second = apply_migrations(db)
        rows = db.execute(text('SELECT version, filename FROM schema_migrations ORDER BY version')).all()

    assert [row[0] for row in rows] == ['0001']
    assert rows[0][1] == '0001_test_migration.sql'
    assert [item['version'] for item in first] == ['0001']
    assert second == []


def test_case_context_builder_isolates_concurrent_case_state():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        case_a = Case(id='case-a', tenant_id='demo', order_id='SO-A', status='replanning', plan_version=2, evidence={'observations':[{'tool':'get_order','result':{'name':'SO-A'}}]})
        case_b = Case(id='case-b', tenant_id='demo', order_id='SO-B', status='waiting_approval', plan_version=1, evidence={'observations':[{'tool':'get_order','result':{'name':'SO-B'}}]})
        db.add_all([case_a, case_b])
        db.add_all([
            Event(case_id='case-a', kind='replan_requested', message='A changed', data={'reason':'inventory changed'}),
            Event(case_id='case-b', kind='tool_observation', message='B read', data={'secret':'do not leak'}),
        ])
        db.add_all([
            Approval(case_id='case-a', plan_version=2, action_hash='hash-a', action={'action_type':'transfer_stock'}, status='pending'),
            Approval(case_id='case-b', plan_version=1, action_hash='hash-b', action={'action_type':'transfer_stock'}, status='pending'),
        ])
        db.add_all([
            Invocation(case_id='case-a', idempotency_key='case-a:action:v2', tool='create_transfer_draft', status='succeeded'),
            Invocation(case_id='case-b', idempotency_key='case-b:action:v1', tool='create_transfer_draft', status='succeeded'),
        ])
        db.add_all([
            Task(case_id='case-a', kind='investigate', status='running', attempts=1),
            Task(case_id='case-b', kind='execute', status='queued', attempts=0),
        ])
        db.commit()

        context = CaseContextBuilder(db).build('case-a', {'reason':'fresh inventory required'})

    assert context['scope']['case_id'] == 'case-a'
    assert context['scope']['order_id'] == 'SO-A'
    assert context['confirmed_observations'][0]['result']['name'] == 'SO-A'
    assert context['last_failure']['kind'] == 'replan_requested'
    assert context['isolation']['case_ids_present'] == ['case-a']
    assert all(ref['case_id'] == 'case-a' for ref in context['approval_refs'])
    assert all(ref['case_id'] == 'case-a' for ref in context['invocation_refs'])
    assert all(ref['case_id'] == 'case-a' for ref in context['task_refs'])
    assert validate_case_context_isolation(context)['allowed'] is True


def test_expired_approval_blocks_executor_before_write_invocation():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    action = normalize_proposal({
        'action_type': 'create_price_review_ticket',
        'input': {
            'sku': 'SKU-A12',
            'order_rate': 5000,
            'reference_rate': 4500,
            'difference': 500,
            'reason': 'contract mismatch',
        },
        'risk': 'medium',
    }, 'Price mismatch must be reviewed before changing the sales order.')
    plan = {'actions': [action], 'rationale': 'test'}

    with Session(engine) as db:
        case = Case(id='case-expired', tenant_id='demo', event_type='price_mismatch', order_id='SO-1', status='approved', plan_version=1, plan=plan)
        approval = Approval(
            case_id=case.id,
            plan_version=case.plan_version,
            action_hash=digest(action, case.plan_version),
            action=action,
            status='approved',
            required_roles=['sales_manager'],
            approved_roles=['sales_manager'],
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        db.add_all([case, approval])
        db.commit()

        execute(db, case, approval.id)
        db.commit()

        refreshed_case = db.get(Case, case.id)
        refreshed_approval = db.get(Approval, approval.id)
        invocations = db.scalars(select(Invocation).where(Invocation.case_id == case.id)).all()
        events = db.scalars(select(Event).where(Event.case_id == case.id)).all()

    assert refreshed_case.status == 'manual_review'
    assert refreshed_approval.status == 'expired'
    assert invocations == []
    assert [event.kind for event in events] == ['approval_expired']


def test_revoked_approval_blocks_executor_before_write_invocation():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    action = normalize_proposal({
        'action_type': 'create_price_review_ticket',
        'input': {
            'sku': 'SKU-A12',
            'order_rate': 5000,
            'reference_rate': 4500,
            'difference': 500,
            'reason': 'contract mismatch',
        },
        'risk': 'medium',
    }, 'Price mismatch must be reviewed before changing the sales order.')
    plan = {'actions': [action], 'rationale': 'test'}

    with Session(engine) as db:
        case = Case(id='case-revoked', tenant_id='demo', event_type='price_mismatch', order_id='SO-1', status='approved', plan_version=1, plan=plan)
        approval = Approval(
            case_id=case.id,
            plan_version=case.plan_version,
            action_hash=digest(action, case.plan_version),
            action=action,
            status='revoked',
            required_roles=['sales_manager'],
            approved_roles=['sales_manager'],
            revoked_at=datetime.now(UTC),
            revoked_by='sales-manager',
            revocation_reason='new customer constraint',
        )
        db.add_all([case, approval])
        db.commit()

        execute(db, case, approval.id)
        db.commit()

        refreshed_case = db.get(Case, case.id)
        invocations = db.scalars(select(Invocation).where(Invocation.case_id == case.id)).all()
        events = db.scalars(select(Event).where(Event.case_id == case.id)).all()

    assert refreshed_case.status == 'manual_review'
    assert invocations == []
    assert [event.kind for event in events] == ['approval_revoked']


def test_case_context_sanitizes_foreign_scheduler_payload_scope_before_llm():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        case = Case(id='case-a', tenant_id='tenant-a', order_id='SO-A', status='replanning', plan_version=2)
        db.add(case); db.commit()
        context = CaseContextBuilder(db).build('case-a', {
            'reason': 'preflight failed',
            'previous_plan': {
                'case_id': 'case-b',
                'tenant_id': 'tenant-b',
                'order_id': 'SO-B',
                'actions': [{
                    'action_type': 'transfer_stock',
                    'input': {'sku': 'SKU-A12', 'quantity': 10},
                }],
            },
        })

    previous_plan = context['task_context']['previous_plan']
    assert 'case_id' not in previous_plan
    assert 'tenant_id' not in previous_plan
    assert 'order_id' not in previous_plan
    assert previous_plan['actions'][0]['input']['sku'] == 'SKU-A12'
    isolation = validate_case_context_isolation(context)
    assert isolation['allowed'] is True
    assert isolation['warnings']
    assert '$.previous_plan.case_id' in context['isolation']['task_context_removed_scope_paths']


def test_case_context_isolation_guard_blocks_mixed_durable_records():
    case = Case(id='case-a', tenant_id='tenant-a', order_id='SO-A', status='queued', plan_version=0)
    context = build_case_context(
        case=case,
        events=[Event(case_id='case-b', kind='tool_observation', message='foreign event', data={})],
        approvals=[],
        invocations=[],
        tasks=[],
        lessons=[],
    )

    isolation = validate_case_context_isolation(context)
    assert isolation['allowed'] is False
    assert any('other cases' in problem for problem in isolation['problems'])


def test_verified_case_lessons_are_generated_only_from_verified_resolved_case():
    case = Case(
        id='case-lesson-1',
        tenant_id='demo',
        order_id='SO-1',
        status='resolved',
        plan_version=1,
        evidence={'observations':[
            {'tool':'get_order','result':{'name':'SO-1','customer':'CUST-1','items':[{'item_code':'SKU-A12','warehouse':'Stores - ROPS','qty':30}]}},
            {'tool':'get_customer_profile','result':{'allows_partial_delivery':True}},
        ]},
    )
    action = {'action_type':'transfer_stock','input':{'source':'重庆仓 - ROPS','target':'Stores - ROPS','sku':'SKU-A12','quantity':10}}
    lessons = candidate_lessons_from_verified_action(case, action, {'verified':True,'event_data':{'external_id':'MAT-STE-1'}})
    assert {lesson['lesson_type'] for lesson in lessons} >= {'resolution_pattern', 'operational_lesson', 'customer_preference'}
    assert all('planning hint' in lesson['content'] or 'Re-check' in lesson['content'] or 'Before executing' in lesson['content'] for lesson in lessons)

    unresolved = Case(id='case-lesson-2', tenant_id='demo', order_id='SO-2', status='waiting_approval', plan_version=1, evidence=case.evidence)
    assert candidate_lessons_from_verified_action(unresolved, action, {'verified':True}) == []
    assert candidate_lessons_from_verified_action(case, action, {'verified':False}) == []


def test_case_context_includes_only_same_tenant_active_lessons_as_hints():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        case = Case(
            id='case-memory-a',
            tenant_id='tenant-a',
            order_id='SO-A',
            status='queued',
            plan_version=0,
            evidence={'observations':[{'tool':'get_order','result':{'name':'SO-A','customer':'CUST-A','items':[{'item_code':'SKU-A12','warehouse':'Stores - ROPS'}]}}]},
        )
        db.add(case)
        db.add_all([
            CaseLesson(
                tenant_id='tenant-a',
                lesson_type='resolution_pattern',
                subject_type='sku',
                subject_id='SKU-A12',
                content='Tenant A SKU lesson.',
                evidence_case_id='old-case-a',
                source_action_type='create_purchase_request',
                status='active',
            ),
            CaseLesson(
                tenant_id='tenant-b',
                lesson_type='resolution_pattern',
                subject_type='sku',
                subject_id='SKU-A12',
                content='Tenant B lesson must not leak.',
                evidence_case_id='old-case-b',
                source_action_type='create_purchase_request',
                status='active',
            ),
            CaseLesson(
                tenant_id='tenant-a',
                lesson_type='resolution_pattern',
                subject_type='sku',
                subject_id='SKU-A12',
                content='Inactive lesson must not appear.',
                evidence_case_id='old-case-c',
                source_action_type='create_purchase_request',
                status='retired',
            ),
        ])
        db.commit()

        lessons = relevant_lessons_for_case(db, case)
        context = CaseContextBuilder(db).build('case-memory-a')

    assert [lesson['content'] for lesson in lessons] == ['Tenant A SKU lesson.']
    memory = context['long_term_memory']
    assert memory['type'] == 'verified_case_lessons'
    assert memory['lessons'][0]['tenant_id'] == 'tenant-a'
    assert memory['lessons'][0]['content'] == 'Tenant A SKU lesson.'
    assert context['isolation']['lesson_tenant_ids_present'] == ['tenant-a']


def test_record_verified_lessons_is_idempotent_per_evidence_case():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        case = Case(
            id='case-memory-record',
            tenant_id='demo',
            order_id='SO-1',
            status='resolved',
            plan_version=1,
            evidence={'observations':[{'tool':'get_order','result':{'name':'SO-1','items':[{'item_code':'SKU-A12','warehouse':'Stores - ROPS'}]}}]},
        )
        db.add(case); db.commit()
        action = {'action_type':'create_purchase_request','input':{'target':'Stores - ROPS','sku':'SKU-A12','quantity':20,'required_by':future_required_by()}}
        first = record_verified_lessons(db, case, action, {'verified':True,'event_data':{'external_id':'MAT-MR-1'}})
        second = record_verified_lessons(db, case, action, {'verified':True,'event_data':{'external_id':'MAT-MR-1'}})
        db.commit()

    assert len(first) == 1
    assert second == []


def test_write_action_tools_share_schema_but_are_not_llm_callable():
    spec = action_tool_spec('transfer_stock')
    assert spec is not None
    assert spec.permission == 'inventory:transfer:create_draft'
    assert spec.side_effect == 'create_draft_record'
    assert spec.requires_approval is True
    assert spec.idempotency_required is True
    with pytest.raises(ValueError):
        spec.to_openai_tool()


def test_executor_registry_maps_action_type_to_write_adapter_tool():
    transfer = executor_for('transfer_stock')
    purchase = executor_for('create_purchase_request')
    price_review = executor_for('create_price_review_ticket')
    supplier_followup = executor_for('create_supplier_followup_task')
    assert transfer is not None
    assert transfer.invocation_tool == 'create_transfer_draft'
    assert purchase is not None
    assert purchase.invocation_tool == 'create_purchase_request_draft'
    assert price_review is not None
    assert price_review.invocation_tool == 'create_price_review_ticket'
    assert supplier_followup is not None
    assert supplier_followup.invocation_tool == 'create_supplier_followup_task'
    assert executor_for('draft_customer_notification') is None


def test_price_review_executor_writes_and_verifies_local_review_record():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        case = Case(id='case-price', tenant_id='demo', event_type='price_mismatch', order_id='SO-PRICE-1')
        db.add(case)
        db.flush()
        executor = executor_for('create_price_review_ticket')
        action_input = {'sku':'SKU-A12','order_rate':5000,'reference_rate':4500,'difference':500,'reason':'mismatch'}
        review_id = executor.write(db, None, action_input, None, 'case-price:action:v1', case)
        verification = executor.verify(db, None, review_id, action_input)
        db.commit()

    assert verification['verified'] is True
    with Session(engine) as db:
        review = db.get(PriceReview, review_id)
        assert review.status == 'draft'
        assert review.order_id == 'SO-PRICE-1'


def test_supplier_followup_executor_writes_and_verifies_local_record():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        case = Case(id='case-delay', tenant_id='demo', event_type='delivery_delay', order_id='SO-DELAY-1')
        db.add(case)
        db.flush()
        executor = executor_for('create_supplier_followup_task')
        action_input = {'sku':'SKU-A12','purchase_order':'PO-1','supplier':'Supplier A','expected_delivery_date':'2026-07-25','delayed_by_days':5,'reason':'late inbound'}
        followup_id = executor.write(db, None, action_input, None, 'case-delay:action:v1', case)
        verification = executor.verify(db, None, followup_id, action_input)
        db.commit()

    assert verification['verified'] is True
    with Session(engine) as db:
        followup = db.get(SupplierFollowup, followup_id)
        assert followup.status == 'draft'
        assert followup.order_id == 'SO-DELAY-1'


def test_transfer_executor_preflight_detects_source_inventory_change():
    class FakeDb:
        def scalar(self, *_args, **_kwargs): return True
    class FakeErp:
        def stock(self, sku, source):
            return {'item_code':sku,'warehouse':source,'actual_qty':5,'reserved_qty':0}

    executor = executor_for('transfer_stock')
    result = executor.preflight(FakeDb(), FakeErp(), {'source':'WH-A','target':'WH-B','sku':'SKU-1','quantity':10})
    assert not result['ok']
    assert result['reason'] == 'source_inventory_changed'
    assert result['fresh_inventory']['actual_qty'] == 5


def test_purchase_executor_context_reads_company_from_order():
    class FakeErp:
        def sales_order(self, order_id):
            return {'name':order_id,'company':'ResolveOps Co'}

    executor = executor_for('create_purchase_request')
    assert executor.context(FakeErp(), 'SO-1', {}) == {'company':'ResolveOps Co'}


def test_planner_action_catalog_is_generated_from_action_registry():
    catalog = planner_action_catalog()
    by_type = {item['action_type']: item for item in catalog}
    assert registered_action_types() <= set(by_type)
    assert by_type['transfer_stock']['input_schema'] == action_tool_spec('transfer_stock').parameters
    assert by_type['transfer_stock']['llm_directly_callable'] is False
    assert by_type['create_purchase_request']['requires_approval'] is True


def test_action_profile_limits_planner_visible_actions_by_case_type():
    inventory_actions = {item['action_type'] for item in planner_action_catalog('inventory_shortage')}
    price_actions = {item['action_type'] for item in planner_action_catalog('price_mismatch')}
    delivery_actions = {item['action_type'] for item in planner_action_catalog('delivery_delay')}
    assert {'transfer_stock', 'create_purchase_request'} <= inventory_actions
    assert 'create_price_review_ticket' not in inventory_actions
    assert price_actions == {'create_price_review_ticket', 'create_manual_ticket'}
    assert delivery_actions == {'create_supplier_followup_task', 'create_manual_ticket'}
    assert action_types_for_case('price_mismatch') == {'create_price_review_ticket', 'create_manual_ticket'}
    assert action_types_for_case('delivery_delay') == {'create_supplier_followup_task', 'create_manual_ticket'}
    with pytest.raises(ValueError):
        normalize_plan([
            {'action_type':'transfer_stock','input':{'source':'WH-A','target':'WH-B','sku':'SKU-A12','quantity':1}},
        ], 'wrong case type', allowed_action_types=action_types_for_case('price_mismatch'))


def test_planner_instructions_are_generated_not_handwritten():
    instructions = planner_action_instructions()
    assert 'transfer_stock input=' in instructions
    assert 'create_purchase_request input=' in instructions
    assert 'directly callable tools' in instructions
    assert 'source:string' in instructions
    assert 'required_by:string' in instructions


def test_agent_planner_base_prompt_does_not_hardcode_action_input_contracts():
    assert 'transfer_stock uses input=' not in agent_module.PLANNER_SYSTEM_BASE
    assert 'create_purchase_request uses input=' not in agent_module.PLANNER_SYSTEM_BASE


def test_logistics_lane_config_rejects_invalid_transit_time():
    with pytest.raises(ValueError):
        LogisticsLaneIn(
            source_warehouse='Source - ROPS',
            target_warehouse='Target - ROPS',
            transit_days=0,
            cost_per_unit=1,
        )


def test_config_write_requires_config_admin_role():
    require_role(OperatorIdentity(subject='ops', role='config_admin'), 'config_admin', 'ops_admin')
    with pytest.raises(HTTPException) as exc:
        require_role(OperatorIdentity(subject='sales', role='sales_manager'), 'config_admin', 'ops_admin')
    assert exc.value.status_code == 403


def test_operator_identity_comes_from_database_not_request_role_header():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Operator(
            tenant_id='demo',
            subject='alice',
            role='sales_manager',
            api_key_hash=operator_key_hash('alice-key'),
            status='active',
        ))
        db.add(Operator(
            tenant_id='demo',
            subject='disabled',
            role='ops_admin',
            api_key_hash=operator_key_hash('disabled-key'),
            status='disabled',
        ))
        db.commit()

        identity = operator_identity_from_db(db, 'alice-key')
        with pytest.raises(HTTPException) as exc:
            operator_identity_from_db(db, 'disabled-key')

    assert identity.subject == 'alice'
    assert identity.role == 'sales_manager'
    assert identity.tenant_id == 'demo'
    assert exc.value.status_code == 401


def test_audit_log_serializes_actor_role_and_resource():
    log = AuditLog(
        actor='alice',
        role='warehouse_manager',
        action='approval_granted',
        resource_type='approval',
        resource_id='ap-1',
        case_id='case-1',
        data={'action_hash':'abc','plan_version':2},
    )
    result = audit_out(log)
    assert result['actor'] == 'alice'
    assert result['role'] == 'warehouse_manager'
    assert result['resource_type'] == 'approval'
    assert result['data']['plan_version'] == 2


def test_eval_case_requires_write_verification():
    case = Case(id='case-1', tenant_id='demo', order_id='SO-1', status='resolved', plan_version=1, plan={'actions':[{'action_type':'transfer_stock'}]})
    events = [
        Event(case_id='case-1', kind='case_created', message='created', data={}),
        Event(case_id='case-1', kind='context_built', message='context', data={}),
        Event(case_id='case-1', kind='tool_scheduled', message='scheduled', data={'scheduler':{'source':'executed'},'status':'success'}),
        Event(case_id='case-1', kind='tool_observation', message='read', data={'result':{'name':'SO-1'},'tool_result':{'status':'success'}}),
        Event(case_id='case-1', kind='evidence_grounding_passed', message='grounded', data={}),
        Event(case_id='case-1', kind='execution_started', message='execute', data={}),
        Event(case_id='case-1', kind='verification_passed', message='ok', data={}),
    ]
    approvals = [Approval(case_id='case-1', plan_version=1, action_hash='abc', action={}, status='consumed')]
    invocations = [Invocation(case_id='case-1', idempotency_key='k', tool='create_transfer_draft', status='succeeded')]
    tasks = [Task(case_id='case-1', kind='execute', status='done')]
    result = eval_case_out(case, events, approvals, invocations, tasks)
    assert result['resolved'] is True
    assert result['write_invocation_count'] == 1
    assert result['verification_complete'] is True
    assert result['tool_call_count'] == 1
    assert result['scheduled_tool_call_count'] == 1
    assert result['tool_failure_count'] == 0
    assert result['tool_scheduler_sources'] == {'executed': 1}
    assert result['has_evidence_grounding_passed'] is True
    assert 'execution_started' in result['stage_sequence']


def test_eval_case_counts_tool_failures_and_context_isolation():
    case = Case(id='case-2', tenant_id='demo', order_id='SO-2', status='manual_review', plan_version=0)
    events = [
        Event(case_id='case-2', kind='context_isolation_sanitized', message='cleaned', data={}),
        Event(case_id='case-2', kind='context_isolation_failed', message='blocked', data={}),
        Event(case_id='case-2', kind='tool_scheduled', message='scheduled', data={'scheduler':{'source':'cache'},'status':'failed','error_code':'tool_scheduler_failed'}),
        Event(case_id='case-2', kind='tool_observation', message='read failed', data={'result':{'error':'tool_scheduler_failed'},'tool_result':{'status':'failed'}}),
        Event(case_id='case-2', kind='handoff', message='manual', data={}),
    ]
    result = eval_case_out(case, events, [], [], [])

    assert result['manual_review'] is True
    assert result['tool_failure_count'] == 1
    assert result['tool_scheduler_sources'] == {'cache': 1}
    assert result['has_context_isolation_sanitized'] is True
    assert result['has_context_isolation_failure'] is True
    assert result['blocked_event_count'] == 2


def test_eval_summary_counts_recovery_and_failure_signals():
    rows = [
        {
            'resolved': True,
            'manual_review': False,
            'write_invocation_count': 1,
            'verification_complete': True,
            'verification_failed_count': 0,
            'tool_call_count': 4,
            'tool_failure_count': 0,
            'pending_approval_count': 0,
            'has_policy_denial': False,
            'has_evidence_grounding_passed': True,
            'has_evidence_grounding_failure': False,
            'has_context_isolation_sanitized': False,
            'has_context_isolation_failure': False,
            'has_replan': True,
            'has_manual_handoff': False,
            'task_failure_count': 0,
        },
        {
            'resolved': False,
            'manual_review': True,
            'write_invocation_count': 0,
            'verification_complete': True,
            'verification_failed_count': 1,
            'tool_call_count': 2,
            'tool_failure_count': 1,
            'pending_approval_count': 1,
            'has_policy_denial': True,
            'has_evidence_grounding_passed': False,
            'has_evidence_grounding_failure': True,
            'has_context_isolation_sanitized': True,
            'has_context_isolation_failure': True,
            'has_replan': False,
            'has_manual_handoff': True,
            'task_failure_count': 1,
        },
    ]
    summary = eval_summary_out(rows)
    assert summary['total_cases'] == 2
    assert summary['case_resolution_rate'] == 0.5
    assert summary['verification_pass_rate'] == 1
    assert summary['avg_read_tool_calls'] == 3
    assert summary['tool_failure_rate'] == 1 / 6
    assert summary['approval_waiting_cases'] == 1
    assert summary['policy_denials'] == 1
    assert summary['evidence_grounding_passed_cases'] == 1
    assert summary['evidence_grounding_failures'] == 1
    assert summary['context_isolation_sanitized_cases'] == 1
    assert summary['context_isolation_failures'] == 1
    assert summary['replanned_cases'] == 1
