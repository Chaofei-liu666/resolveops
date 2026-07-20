import json
import os
from datetime import UTC, date, datetime, timedelta

os.environ.setdefault('ERPNEXT_BASE_URL', 'https://erp.invalid')
os.environ.setdefault('ERPNEXT_API_KEY', 'test')
os.environ.setdefault('ERPNEXT_API_SECRET', 'test')
os.environ.setdefault('WEBHOOK_SECRET', 'test')
os.environ.setdefault('OPERATOR_API_KEY', 'test')

import pytest
import httpx

from production.actions import action_tool_spec, action_types_for_case, normalize_plan, normalize_proposal, planner_action_catalog, planner_action_instructions, registered_action_types
import production.agent as agent_module
import production.main as main_module
import production.runtime_status as runtime_status_module
from production.agent import InvestigationAgent
from production.case_ask import CaseQuestionAgent
from production.evidence import validate_plan_grounding
from production.executors import executor_for
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from production.context import CaseContextBuilder, build_case_context, validate_case_context_isolation
from production.main import CaseAskIn, CaseCreateIn, FaultInjectionRunIn, LogisticsLaneIn, OperatorIdentity, ask_case, audit_out, case_tool_trace, create_case, eval_case, eval_case_out, eval_summary_out, operator_identity_from_db, operator_key_hash, require_fault_injection_enabled, require_role, run_fault_injection
from production.memory import candidate_lessons_from_verified_action, record_verified_lessons, relevant_lessons_for_case
import production.migrations as migration_module
from production.migrations import apply_migrations, ensure_schema_migrations_table
from production.models import Approval, AuditLog, Base, Case, CaseLesson, Event, Invocation, Operator, PriceReview, SupplierFollowup, Task
from production.llm_gateway import LLMGateway, LLMResult
from production.policy import action_policy, allow_read_tool
from production.runtime_status import build_runtime_status, expected_migration_versions
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
    assert trace['summary']['read_tool_count'] == 2
    assert trace['summary']['action_count'] == 1
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


def test_case_tool_trace_does_not_attach_failed_replan_observations_to_stale_plan():
    plan = normalize_plan([
        {'action_type':'create_price_review_ticket','input':{
            'sku':'SKU-A12',
            'order_rate':5000,
            'reference_rate':4500,
            'difference':500,
        }},
    ], 'old grounded price mismatch')
    plan['evidence_grounding'] = {'allowed': True, 'reason': 'grounded'}
    case = Case(
        id='case-replan-handoff',
        tenant_id='demo',
        event_type='price_mismatch',
        order_id='SO-PRICE-1',
        plan=plan,
        evidence={
            'observations': price_mismatch_observations(),
            'conclusion': {'status': 'handoff', 'recommended_actions': []},
        },
    )

    trace = case_tool_trace(case)

    assert trace['summary']['observation_count'] == 2
    assert trace['summary']['action_count'] == 0
    assert trace['summary']['grounding_allowed'] is None
    assert trace['action_evidence'] == {}


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


def test_planner_decision_trace_is_preserved_for_audit_display():
    result = InvestigationAgent._parse_conclusion(json.dumps({
        'status': 'ready',
        'recommended_actions': [{'action_type': 'transfer_stock', 'input': {}}],
        'alternatives': ['purchase request'],
        'rationale': 'transfer can meet delivery date',
        'missing_information': 'purchase unit cost unknown',
        'evidence_summary': ['source warehouse has stock'],
        'decision_trace': ['Compared transfer transit days with purchase lead time.'],
        'rejected_actions': [{'action': 'create_purchase_request', 'rationale': 'lead time misses delivery date'}],
    }))

    assert result['decision_trace'] == ['Compared transfer transit days with purchase lead time.']
    assert result['missing_information'] == ['purchase unit cost unknown']
    assert result['rejected_actions'] == [{
        'action_type': 'create_purchase_request',
        'reason': 'lead time misses delivery date',
    }]


def test_tool_error_is_unknown_not_negative_fact():
    conclusion = InvestigationAgent._parse_conclusion('not json')
    assert conclusion['status'] == 'handoff'
    assert 'schema' in conclusion['rationale'].lower()


def test_planner_schema_repair_retries_once_before_handoff():
    class RepairingGateway:
        def __init__(self):
            self.calls = []

        def chat(self, payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                return LLMResult(status='success', response={
                    'choices':[{'message':{'content':'not json'}}],
                    'usage': {'total_tokens': 3},
                }, model='fake-model', latency_ms=1, usage={'total_tokens': 3})
            return LLMResult(status='success', response={
                'choices':[{'message':{'content':'{"status":"ready","recommended_actions":[{"action_type":"create_price_review_ticket","input":{"sku":"SKU-A12","order_rate":5000,"reference_rate":4500,"difference":500}}],"alternatives":[],"rationale":"reference price mismatch is supported","missing_information":[],"evidence_summary":["order and reference price observed"]}'}}],
                'usage': {'total_tokens': 9},
            }, model='fake-model', latency_ms=1, usage={'total_tokens': 9})

    conclusion = InvestigationAgent(None, llm_gateway=RepairingGateway())._plan(
        'SO-1',
        price_mismatch_observations(),
        [],
        {'scope': {'event_type': 'price_mismatch'}},
    )

    assert conclusion['status'] == 'ready'
    assert conclusion['recommended_actions'][0]['action_type'] == 'create_price_review_ticket'
    assert conclusion['schema_repair']['status'] == 'repaired'
    assert conclusion['llm']['usage']['total_tokens'] == 3
    assert conclusion['llm_repair']['usage']['total_tokens'] == 9


def test_planner_schema_repair_failure_preserves_safe_handoff():
    class BrokenRepairGateway:
        def chat(self, _payload):
            return LLMResult(status='success', response={
                'choices':[{'message':{'content':'not json'}}],
            }, model='fake-model', latency_ms=1, usage={})

    conclusion = InvestigationAgent(None, llm_gateway=BrokenRepairGateway())._plan(
        'SO-1',
        price_mismatch_observations(),
        [],
        {'scope': {'event_type': 'price_mismatch'}},
    )

    assert conclusion['status'] == 'handoff'
    assert conclusion['parse_error'] == 'required_json_schema_mismatch'
    assert conclusion['schema_repair']['status'] == 'failed'


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


def test_runtime_status_reports_ready_when_migrations_are_complete():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with engine.begin() as db:
        ensure_schema_migrations_table(db)
        for version in expected_migration_versions():
            db.execute(
                text('INSERT INTO schema_migrations(version, filename, checksum) VALUES (:version, :filename, :checksum)'),
                {'version': version, 'filename': f'{version}_test.sql', 'checksum': 'test'},
            )
    with Session(engine) as db:
        db.add_all([
            Task(case_id='case-ready', kind='investigate', status='queued'),
            Task(case_id='case-ready', kind='execute', status='running'),
            Task(case_id='case-old', kind='investigate', status='done'),
            Task(case_id='case-old', kind='execute', status='failed'),
        ])
        db.commit()
        status = build_runtime_status(db)

    assert status['status'] == 'ready'
    assert status['checks']['migrations']['pending_versions'] == []
    assert status['queues']['queued'] == 1
    assert status['queues']['running'] == 1
    assert status['queues']['failed'] == 1
    assert status['queues']['active'] == {'queued': 1, 'running': 1, 'total': 2}
    assert status['queues']['history'] == {'done': 1, 'failed': 1, 'total': 2}


def test_runtime_status_reports_degraded_when_migration_is_missing():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    versions = expected_migration_versions()
    with engine.begin() as db:
        ensure_schema_migrations_table(db)
        for version in versions[:-1]:
            db.execute(
                text('INSERT INTO schema_migrations(version, filename, checksum) VALUES (:version, :filename, :checksum)'),
                {'version': version, 'filename': f'{version}_test.sql', 'checksum': 'test'},
            )
    with Session(engine) as db:
        status = build_runtime_status(db)

    assert status['status'] == 'degraded'
    assert status['checks']['migrations']['pending_versions'] == versions[-1:]


def test_runtime_status_warns_but_allows_local_operator_seed(monkeypatch):
    monkeypatch.setattr(runtime_status_module.settings, 'app_env', 'local')
    monkeypatch.setattr(runtime_status_module.settings, 'operator_seed_keys', 'local-sales:sales_manager:key')
    monkeypatch.setattr(runtime_status_module.settings, 'llm_base_url', None)
    monkeypatch.setattr(runtime_status_module.settings, 'llm_api_key', None)
    monkeypatch.setattr(runtime_status_module.settings, 'llm_model', None)
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with engine.begin() as db:
        ensure_schema_migrations_table(db)
        for version in expected_migration_versions():
            db.execute(
                text('INSERT INTO schema_migrations(version, filename, checksum) VALUES (:version, :filename, :checksum)'),
                {'version': version, 'filename': f'{version}_test.sql', 'checksum': 'test'},
            )
    with Session(engine) as db:
        status = build_runtime_status(db)

    config = status['checks']['configuration']
    assert status['status'] == 'ready'
    assert config['ok'] is True
    assert 'operator_seed_keys_enabled_for_local_development' in config['warnings']
    assert 'llm_not_configured_agent_may_use_deterministic_fallback' in config['warnings']


def test_runtime_status_blocks_production_like_placeholder_config(monkeypatch):
    monkeypatch.setattr(runtime_status_module.settings, 'app_env', 'production')
    monkeypatch.setattr(runtime_status_module.settings, 'erpnext_base_url', 'http://erpnext:8000')
    monkeypatch.setattr(runtime_status_module.settings, 'erpnext_api_key', 'replace-me')
    monkeypatch.setattr(runtime_status_module.settings, 'erpnext_api_secret', 'replace-me')
    monkeypatch.setattr(runtime_status_module.settings, 'webhook_secret', 'replace-me')
    monkeypatch.setattr(runtime_status_module.settings, 'operator_api_key', 'replace-me')
    monkeypatch.setattr(runtime_status_module.settings, 'operator_seed_keys', 'local-sales:sales_manager:key')
    monkeypatch.setattr(runtime_status_module.settings, 'llm_base_url', 'https://api.example.com/v1')
    monkeypatch.setattr(runtime_status_module.settings, 'llm_api_key', 'replace-me')
    monkeypatch.setattr(runtime_status_module.settings, 'llm_model', 'replace-me')
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with engine.begin() as db:
        ensure_schema_migrations_table(db)
        for version in expected_migration_versions():
            db.execute(
                text('INSERT INTO schema_migrations(version, filename, checksum) VALUES (:version, :filename, :checksum)'),
                {'version': version, 'filename': f'{version}_test.sql', 'checksum': 'test'},
            )
    with Session(engine) as db:
        status = build_runtime_status(db)

    config = status['checks']['configuration']
    assert status['status'] == 'degraded'
    assert config['ok'] is False
    assert config['production_like'] is True
    assert 'erpnext_credentials_missing_or_placeholder' in config['errors']
    assert 'llm_credentials_required_for_production_like_env' in config['errors']
    assert 'webhook_secret_missing_or_placeholder' in config['errors']
    assert 'operator_api_key_missing_or_placeholder' in config['errors']
    assert 'operator_seed_keys_not_allowed_in_production_like_env' in config['errors']


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


def test_operator_case_create_queues_investigation_and_is_idempotent(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main_module, 'engine', engine)
    with Session(engine) as db:
        db.add(Operator(
            tenant_id='demo',
            subject='sales',
            role='sales_manager',
            api_key_hash=operator_key_hash('sales-key'),
            status='active',
        ))
        db.commit()

    payload = CaseCreateIn(
        event_type='inventory_shortage',
        order_id='SAL-ORD-2026-00002',
        source_event_id='cli-event-1',
        reason='created from CLI',
    )
    first = create_case(payload, x_operator_key='sales-key')
    second = create_case(payload, x_operator_key='sales-key')

    assert first['duplicate'] is False
    assert second == {'case_id': first['case_id'], 'status': 'queued', 'duplicate': True}
    with Session(engine) as db:
        case = db.get(Case, first['case_id'])
        tasks = db.scalars(select(Task).where(Task.case_id == first['case_id'])).all()
        events = db.scalars(select(Event).where(Event.case_id == first['case_id'])).all()
        audit_logs = db.scalars(select(AuditLog).where(AuditLog.case_id == first['case_id'])).all()
    assert case.event_type == 'inventory_shortage'
    assert case.order_id == 'SAL-ORD-2026-00002'
    assert [task.kind for task in tasks] == ['investigate']
    assert any(event.kind == 'case_created' for event in events)
    assert any(log.action == 'case_created' for log in audit_logs)


def test_fault_injection_requires_non_production_explicit_enable(monkeypatch):
    monkeypatch.setattr(main_module.settings, 'app_env', 'production')
    monkeypatch.setattr(main_module.settings, 'enable_fault_injection', True)
    with pytest.raises(HTTPException) as exc:
        require_fault_injection_enabled()
    assert exc.value.status_code == 403

    monkeypatch.setattr(main_module.settings, 'app_env', 'local')
    monkeypatch.setattr(main_module.settings, 'enable_fault_injection', False)
    with pytest.raises(HTTPException) as exc:
        require_fault_injection_enabled()
    assert exc.value.status_code == 403


def test_runtime_status_blocks_fault_injection_in_production(monkeypatch):
    monkeypatch.setattr(runtime_status_module.settings, 'app_env', 'production')
    monkeypatch.setattr(runtime_status_module.settings, 'erpnext_base_url', 'https://erp.example.com')
    monkeypatch.setattr(runtime_status_module.settings, 'erpnext_api_key', 'real-key')
    monkeypatch.setattr(runtime_status_module.settings, 'erpnext_api_secret', 'real-secret')
    monkeypatch.setattr(runtime_status_module.settings, 'webhook_secret', 'real-webhook')
    monkeypatch.setattr(runtime_status_module.settings, 'operator_api_key', 'real-operator')
    monkeypatch.setattr(runtime_status_module.settings, 'operator_seed_keys', None)
    monkeypatch.setattr(runtime_status_module.settings, 'llm_base_url', 'https://llm.example.com/v1')
    monkeypatch.setattr(runtime_status_module.settings, 'llm_api_key', 'real-llm-key')
    monkeypatch.setattr(runtime_status_module.settings, 'llm_model', 'model')
    monkeypatch.setattr(runtime_status_module.settings, 'enable_fault_injection', True)
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        status = build_runtime_status(db)
    assert status['status'] == 'degraded'
    assert 'fault_injection_not_allowed_in_production_like_env' in status['checks']['configuration']['errors']


def test_fault_injection_api_changes_erpnext_through_adapter_and_audits(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main_module, 'engine', engine)
    monkeypatch.setattr(main_module.settings, 'app_env', 'local')
    monkeypatch.setattr(main_module.settings, 'enable_fault_injection', True)
    monkeypatch.setattr(main_module.settings, 'erpnext_company', 'ResolveOps Co')
    monkeypatch.setattr(main_module.settings, 'erpnext_stock_difference_account', 'Temporary Opening - ROPS')
    monkeypatch.setattr(main_module.settings, 'erpnext_default_valuation_rate', 100)

    calls = []

    class FakeERP:
        def __init__(self, *_args):
            self.qty = 10

        def stock(self, item_code, warehouse):
            return {'item_code': item_code, 'warehouse': warehouse, 'actual_qty': self.qty, 'reserved_qty': 0}

        def set_stock_balance_for_fault_injection(self, **kwargs):
            calls.append(kwargs)
            self.qty = kwargs['qty']
            return {'stock_reconciliation': 'MAT-RECO-TEST', 'submitted': True, 'docstatus': 1}

    monkeypatch.setattr(main_module, 'ERPNextAdapter', FakeERP)
    with Session(engine) as db:
        db.add(Operator(
            tenant_id='demo',
            subject='ops',
            role='ops_admin',
            api_key_hash=operator_key_hash('ops-key'),
            status='active',
        ))
        case = Case(id='case-fi', tenant_id='demo', order_id='SO-1', status='waiting_for_approval')
        db.add(case)
        db.commit()

    payload = FaultInjectionRunIn(
        fault_type='inventory_changed_before_execution',
        case_id='case-fi',
        item_code='SKU-A12',
        warehouse='重庆仓 - ROPS',
        new_qty=0,
        reason='simulate stock consumed before approval execution',
    )
    result = run_fault_injection(payload, x_operator_key='ops-key')

    assert result['status'] == 'applied'
    assert result['before']['actual_qty'] == 10
    assert result['after']['actual_qty'] == 0
    assert calls[0]['company'] == 'ResolveOps Co'
    assert calls[0]['difference_account'] == 'Temporary Opening - ROPS'
    with Session(engine) as db:
        event = db.scalar(select(Event).where(Event.case_id == 'case-fi', Event.kind == 'fault_injected'))
        audit_log = db.scalar(select(AuditLog).where(AuditLog.action == 'fault_injection_run'))
    assert event is not None
    assert audit_log is not None
    assert audit_log.case_id == 'case-fi'


def test_fault_injection_converts_erpnext_permission_error_to_gateway_error(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main_module, 'engine', engine)
    monkeypatch.setattr(main_module.settings, 'app_env', 'local')
    monkeypatch.setattr(main_module.settings, 'enable_fault_injection', True)
    monkeypatch.setattr(main_module.settings, 'erpnext_company', 'ResolveOps Co')
    monkeypatch.setattr(main_module.settings, 'erpnext_stock_difference_account', 'Temporary Opening - ROPS')
    monkeypatch.setattr(main_module.settings, 'erpnext_default_valuation_rate', 100)

    class ForbiddenERP:
        def __init__(self, *_args):
            pass

        def stock(self, item_code, warehouse):
            return {'item_code': item_code, 'warehouse': warehouse, 'actual_qty': 10, 'reserved_qty': 0}

        def set_stock_balance_for_fault_injection(self, **_kwargs):
            request = httpx.Request('POST', 'https://erp.invalid/api/resource/Stock%20Reconciliation')
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError('forbidden', request=request, response=response)

    monkeypatch.setattr(main_module, 'ERPNextAdapter', ForbiddenERP)
    with Session(engine) as db:
        db.add(Operator(
            tenant_id='demo',
            subject='ops',
            role='ops_admin',
            api_key_hash=operator_key_hash('ops-key'),
            status='active',
        ))
        db.commit()

    payload = FaultInjectionRunIn(
        fault_type='inventory_changed_before_execution',
        item_code='SKU-A12',
        warehouse='重庆仓 - ROPS',
        new_qty=0,
    )
    with pytest.raises(HTTPException) as exc:
        run_fault_injection(payload, x_operator_key='ops-key')

    assert exc.value.status_code == 502
    assert exc.value.detail['error'] == 'erpnext_fault_injection_failed'
    assert exc.value.detail['erpnext_status_code'] == 403


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


def test_eval_case_endpoint_requires_ops_role_and_returns_case_metrics(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main_module, 'engine', engine)
    with Session(engine) as db:
        db.add(Operator(
            tenant_id='demo',
            subject='ops',
            role='ops_admin',
            api_key_hash=operator_key_hash('ops-key'),
            status='active',
        ))
        db.add(Case(
            id='case-eval',
            tenant_id='demo',
            order_id='SO-1',
            event_type='inventory_shortage',
            status='resolved',
            plan_version=1,
            plan={'actions': [{'action_type': 'transfer_stock'}]},
        ))
        db.add(Event(case_id='case-eval', kind='tool_observation', message='read', data={'tool': 'get_order', 'result': {}, 'tool_result': {'status': 'success'}}))
        db.add(Event(case_id='case-eval', kind='verification_passed', message='verified', data={}))
        db.add(Invocation(case_id='case-eval', idempotency_key='case-eval:transfer_stock:1', tool='create_transfer_draft', status='succeeded'))
        db.commit()

    result = eval_case('case-eval', x_operator_key='ops-key')

    assert result['case_id'] == 'case-eval'
    assert result['resolved'] is True
    assert result['write_invocation_count'] == 1
    assert result['verification_complete'] is True
    assert result['tool_call_count'] == 1


def test_case_question_agent_can_call_read_tool_before_answering():
    calls = []

    class FakeTools:
        def definitions(self):
            return [{
                'type': 'function',
                'function': {
                    'name': 'get_item_supply_profile',
                    'description': 'Read item replenishment facts.',
                    'parameters': {
                        'type': 'object',
                        'properties': {'item_code': {'type': 'string'}},
                        'required': ['item_code'],
                    },
                },
            }]

        def execute_result(self, name, arguments, order_id):
            calls.append({'name': name, 'arguments': arguments, 'order_id': order_id})
            return ToolResult.success({'item_code': arguments['item_code'], 'lead_time_days': 3})

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, payload):
            self.calls += 1
            if self.calls == 1:
                return LLMResult(status='success', response={'choices': [{'message': {
                    'role': 'assistant',
                    'tool_calls': [{
                        'id': 'call-1',
                        'type': 'function',
                        'function': {'name': 'get_item_supply_profile', 'arguments': '{"item_code":"SKU-A12"}'},
                    }],
                }}]})
            return LLMResult(status='success', response={'choices': [{'message': {
                'role': 'assistant',
                'content': '{"answer":"Purchase takes 3 days, so it is slower than the current transfer plan.","rationale":"The read tool returned lead_time_days=3.","used_evidence":["lead_time_days=3"],"used_tools":["get_item_supply_profile"],"safe_next_steps":["Keep the current approval path or ask for replanning."]}',
            }}]})

    observations = []
    result = CaseQuestionAgent(FakeTools(), FakeLLM()).answer(
        order_id='SO-1',
        question='Why not purchase?',
        case_context={'scope': {'case_id': 'case-1', 'event_type': 'inventory_shortage', 'order_id': 'SO-1'}},
        on_observation=observations.append,
    )

    assert result['answer'].startswith('Purchase takes 3 days')
    assert result['used_tools'] == ['get_item_supply_profile']
    assert calls == [{'name': 'get_item_supply_profile', 'arguments': {'item_code': 'SKU-A12'}, 'order_id': 'SO-1'}]
    assert observations[0]['tool'] == 'get_item_supply_profile'


def test_case_question_agent_allows_bounded_small_talk_without_tools():
    class FakeTools:
        def definitions(self):
            return [{
                'type': 'function',
                'function': {
                    'name': 'get_order',
                    'description': 'Read order facts.',
                    'parameters': {'type': 'object', 'properties': {}},
                },
            }]

        def execute_result(self, name, arguments, order_id):
            raise AssertionError('small talk should not call read tools')

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def chat(self, payload):
            self.calls.append(payload)
            assert 'tools' not in payload
            return LLMResult(status='success', response={'choices': [{'message': {
                'role': 'assistant',
                'content': '{"answer":"我是 ResolveOps，专注于订单履约和业务异常 Case 的调查、解释与安全推进。你可以问我当前 Case 为什么停住、用了哪些工具、下一步应该如何处理。","rationale":"This is an identity/scope question, so no business read tool was needed.","used_evidence":[],"used_tools":[],"safe_next_steps":["Ask about the current Case status, tool trace, approvals, or safe next steps."]}',
            }}]})

    observations = []
    fake_llm = FakeLLM()
    result = CaseQuestionAgent(FakeTools(), fake_llm).answer(
        order_id='SO-1',
        question='你好，你是谁？',
        case_context={'scope': {'case_id': 'case-1', 'event_type': 'inventory_shortage', 'order_id': 'SO-1'}},
        on_observation=observations.append,
    )

    assert 'ResolveOps' in result['answer']
    assert result['used_tools'] == []
    assert observations == []
    assert len(result['llm']) > 0
    assert len(fake_llm.calls) == 1


def test_case_question_agent_falls_back_to_case_context_when_final_json_is_invalid():
    class FakeTools:
        def definitions(self):
            return [{
                'type': 'function',
                'function': {
                    'name': 'get_order',
                    'description': 'Read order facts.',
                    'parameters': {'type': 'object', 'properties': {}},
                },
            }]

        def execute_result(self, name, arguments, order_id):
            return ToolResult.failure('tool_execution_failed', error_type='ConnectError', retryable=True)

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, payload):
            self.calls += 1
            if self.calls == 1:
                return LLMResult(status='success', response={'choices': [{'message': {
                    'role': 'assistant',
                    'tool_calls': [{
                        'id': 'call-1',
                        'type': 'function',
                        'function': {'name': 'get_order', 'arguments': '{}'},
                    }],
                }}]})
            return LLMResult(status='success', response={'choices': [{'message': {
                'role': 'assistant',
                'content': '',
            }}]})

    result = CaseQuestionAgent(FakeTools(), FakeLLM()).answer(
        order_id='SO-1',
        question='Why did this case stop?',
        case_context={
            'scope': {'case_id': 'case-1', 'event_type': 'inventory_shortage', 'order_id': 'SO-1'},
            'current_state': {'status': 'manual_review'},
            'last_failure': {'message': 'Agent ended investigation without a safe executable proposal.'},
        },
        on_observation=lambda _record: None,
    )

    assert 'case-1' in result['answer']
    assert 'manual_review' in result['answer']
    assert result['fallback'] == 'case_context_summary'
    assert result['used_tools'] == ['get_order']


def test_case_ask_endpoint_records_read_only_answer(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main_module, 'engine', engine)

    class FakeQuestionAgent:
        def __init__(self, _tools):
            pass

        def answer(self, *, order_id, question, case_context, on_observation):
            on_observation({'tool': 'get_order', 'arguments': {}, 'result': {'name': order_id}, 'scheduler': {'source': 'executed'}})
            return {
                'question': question,
                'answer': 'The Case is still waiting for approval.',
                'rationale': 'The Case context status is waiting_approval.',
                'used_evidence': ['current_state.status'],
                'used_tools': ['get_order'],
                'safe_next_steps': ['Approve through the normal approval API if appropriate.'],
                'observations': [{'tool': 'get_order', 'arguments': {}, 'result': {'name': order_id}, 'scheduler': {'source': 'executed'}}],
            }

    monkeypatch.setattr(main_module, 'CaseQuestionAgent', FakeQuestionAgent)
    with Session(engine) as db:
        db.add(Operator(
            tenant_id='demo',
            subject='ops',
            role='ops_admin',
            api_key_hash=operator_key_hash('ops-key'),
            status='active',
        ))
        db.add(Case(
            id='case-ask',
            tenant_id='demo',
            order_id='SO-1',
            event_type='inventory_shortage',
            status='waiting_approval',
            plan_version=1,
            plan={'actions': [{'action_type': 'transfer_stock'}]},
        ))
        db.commit()

    result = ask_case('case-ask', CaseAskIn(question='What is next?'), x_operator_key='ops-key')

    assert result['case_id'] == 'case-ask'
    assert result['answer'] == 'The Case is still waiting for approval.'
    assert result['used_tools'] == ['get_order']
    with Session(engine) as db:
        events = db.scalars(select(Event).where(Event.case_id == 'case-ask').order_by(Event.created_at)).all()
        approvals = db.scalars(select(Approval).where(Approval.case_id == 'case-ask')).all()
        invocations = db.scalars(select(Invocation).where(Invocation.case_id == 'case-ask')).all()
    assert [event.kind for event in events] == ['case_question_asked', 'case_question_tool_observation', 'case_question_answered']
    assert approvals == []
    assert invocations == []


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
