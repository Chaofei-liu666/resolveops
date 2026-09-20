import asyncio

import pytest

pytest.importorskip('textual')

from production.console import (
    ResolveOpsWorkbench,
    action_evidence_text,
    build_investigation_runs,
    build_turn_batches,
    case_option_text,
    context_update_text,
    context_snapshot_text,
    event_detail,
    event_summary,
    planner_decision_data,
    planner_decision_text,
    tool_card_text,
)


CASE = {
    'id': 'CASE-1',
    'event_type': 'inventory_shortage',
    'order_id': 'SO-1',
    'status': 'waiting_approval',
    'events': [
        {
            'id': 'event-1',
            'kind': 'tool_observation',
            'message': 'Agent called read tool: get_inventory.',
            'data': {'tool': 'get_inventory', 'result': {'available_qty': 40}},
        },
    ],
    'approvals': [],
}


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append({'method': method, 'path': path, 'payload': payload})
        if path == '/v1/runtime/status':
            return {'status': 'ready', 'checks': {'database': {'ok': True}, 'configuration': {'ok': True}}}
        if path.startswith('/v1/cases?'):
            return [CASE]
        if path == '/v1/cases/CASE-1':
            return CASE
        if path == '/v1/chat':
            return {'answer': 'General answer.', 'source': 'llm'}
        if path == '/v1/cases/CASE-1/ask':
            return {'answer': 'Case answer.', 'observations': []}
        if path.startswith('/v1/evals/'):
            return {'total_cases': 1, 'task_success_rate': 1, 'evidence_faithfulness_rate': 1, 'verification_pass_rate': 1}
        raise AssertionError(f'unexpected request: {method} {path}')

    def stream(self, method, path, payload=None):
        self.calls.append({'method': method, 'path': path, 'payload': payload, 'stream': True})
        if path == '/v1/chat/stream':
            yield 'start', {'source': 'llm'}
            yield 'delta', {'text': 'General answer.'}
            yield 'done', {'answer': 'General answer.'}
            return
        if path == '/v1/cases/CASE-1/ask/stream':
            yield 'tool', {'tool': 'get_inventory', 'status': 'success'}
            yield 'delta', {'text': 'Case answer.'}
            yield 'done', {'answer': 'Case answer.', 'used_tools': ['get_inventory']}
            return
        raise AssertionError(f'unexpected stream: {method} {path}')


def test_console_event_rendering_separates_summary_card_from_detail_payload():
    assert '[等待审批]' in str(case_option_text(CASE))
    summary = event_summary(CASE['events'][0])
    assert summary == '获得 Tool Observation'
    card = tool_card_text(1, 1, 'get_inventory', {'warehouse': 'Stores - ROPS', 'actual_qty': 40, 'reserved_qty': 10})
    assert card.startswith('get_inventory\n  目的：核实仓库实际库存、预留量与可用库存')
    assert '[OBSERVATION 01]' in card
    assert '可用=30' in card
    detail = event_detail(CASE['events'][0])
    assert '"available_qty": 40' in detail


def test_event_detail_preserves_the_complete_payload_for_technical_inspection():
    detail = event_detail({'kind': 'agent_plan_created', 'data': {'large': 'x' * 1800}})
    assert '内容已截断' not in detail
    assert 'x' * 1800 in detail


def test_console_summary_merges_tool_and_observation_while_events_keeps_lifecycle():
    call = {'id': 'call-1', 'kind': 'tool_call', 'data': {'tool': 'get_inventory', 'arguments': {'warehouse': 'Stores - ROPS'}}}
    scheduled = {'id': 'scheduled-1', 'kind': 'tool_scheduled', 'data': {'tool': 'get_inventory', 'status': 'success'}}
    observation = {
        'id': 'observation-1',
        'kind': 'tool_observation',
        'data': {'tool': 'get_inventory', 'result': {'warehouse': 'Stores - ROPS', 'actual_qty': 40, 'reserved_qty': 10}},
    }
    result = {'id': 'result-1', 'kind': 'tool_result', 'data': {'tool': 'get_inventory', 'status': 'success'}}

    summary_app = ResolveOpsWorkbench(FakeClient())
    summary_writes = []
    summary_app._write = lambda label, text, style='white': summary_writes.append((label, text, style))
    for event in (call, scheduled, observation, result):
        summary_app._append_event(event)
    assert [label for label, _, _ in summary_writes] == ['TOOL 01']
    assert '[OBSERVATION 01]' in summary_writes[0][1]

    detail_app = ResolveOpsWorkbench(FakeClient())
    detail_app.expanded_events = True
    detail_writes = []
    detail_app._write = lambda label, text, style='white': detail_writes.append((label, text, style))
    for event in (call, scheduled, observation, result):
        detail_app._append_event(event)
    assert [label for label, _, _ in detail_writes] == ['TOOL 01', 'SCHEDULER 01', 'OBSERVATION 01', 'TOOL RESULT 01']


def test_context_snapshot_only_mentions_memory_when_it_was_really_matched():
    case = {
        **CASE,
        'events': [{'kind': 'context_built', 'data': {
            'scope': {'case_id': 'CASE-123456', 'tenant_id': 'demo', 'order_id': 'SO-42'},
            'current_state': {'status': 'running', 'plan_version': 2},
            'confirmed_observation_count': 3,
            'memory_count': 0,
        }}],
    }
    assert 'Context Snapshot' in context_snapshot_text(case)
    assert 'Memory' not in context_snapshot_text(case)
    case['events'][0]['data']['memory_count'] = 2
    assert 'Memory · 使用 2 条 verified lessons' in context_snapshot_text(case)


def test_turn_batches_only_mark_real_same_turn_multi_tool_calls_as_parallel():
    case = {
        **CASE,
        'events': [
            {'id': 'call-1', 'kind': 'tool_call', 'data': {'turn': 1, 'tool': 'get_order', 'arguments': {}}},
            {'id': 'call-2', 'kind': 'tool_call', 'data': {'turn': 1, 'tool': 'get_inventory', 'arguments': {}}},
            {'id': 'obs-1', 'kind': 'tool_observation', 'data': {'tool': 'get_order', 'result': {'name': 'SO-42', 'items': []}}},
            {'id': 'obs-2', 'kind': 'tool_observation', 'data': {'tool': 'get_inventory', 'result': {'warehouse': 'Stores', 'actual_qty': 20, 'reserved_qty': 5}}},
            {'id': 'call-3', 'kind': 'tool_call', 'data': {'turn': 2, 'tool': 'get_customer_profile', 'arguments': {}}},
            {'id': 'obs-3', 'kind': 'tool_observation', 'data': {'tool': 'get_customer_profile', 'result': {'customer_name': 'Acme'}}},
        ],
    }
    batches = build_turn_batches(case)
    assert batches[0]['parallel'] is True
    assert [item['tool'] for item in batches[0]['tools']] == ['get_order', 'get_inventory']
    assert batches[1]['parallel'] is False


def test_canvas_uses_durable_tool_evidence_ids_for_action_plan_links():
    case = {
        **CASE,
        'tool_trace': {
            'observations': [
                {'evidence_id': 'E-001', 'tool': 'get_order'},
                {'evidence_id': 'E-002', 'tool': 'get_inventory'},
            ],
            'action_evidence': {'A-transfer': ['E-001', 'E-002']},
        },
    }
    assert action_evidence_text(case, {'action_id': 'A-transfer'}) == 'TOOL 01 / E-001 · TOOL 02 / E-002'
    assert action_evidence_text(case, {'evidence_refs': ['E-001']}, {'E-001': 7}) == 'TOOL 07 / E-001'
    assert 'runtime-overview' not in ResolveOpsWorkbench.CSS


def test_turn_batches_preserve_a_real_second_llm_turn_without_another_tool_call():
    case = {
        **CASE,
        'events': [
            {'id': 'start-1', 'kind': 'turn_start', 'data': {'turn': 1}},
            {'id': 'call-1', 'kind': 'tool_call', 'data': {'turn': 1, 'tool': 'get_order'}},
            {'id': 'obs-1', 'kind': 'tool_observation', 'data': {'tool': 'get_order', 'result': {'name': 'SO-1'}}},
            {'id': 'end-1', 'kind': 'turn_end', 'data': {'turn': 1, 'reason': 'tool_calls_processed'}},
            {'id': 'start-2', 'kind': 'turn_start', 'data': {'turn': 2}},
            {'id': 'end-2', 'kind': 'turn_end', 'data': {'turn': 2, 'reason': 'no_tool_calls'}},
        ],
    }
    turns = build_turn_batches(case)
    assert [turn['turn'] for turn in turns] == [1, 2]
    assert len(turns[0]['tools']) == 1
    assert turns[1]['tools'] == []
    assert turns[1]['end_reason'] == 'no_tool_calls'


def test_planner_card_uses_saved_audit_trace_not_hidden_reasoning():
    case = {
        **CASE,
        'agent_decision': {
            'decision_trace': ['目标仓可用库存为 0，确认短缺。', '调拨来源可用库存覆盖全部缺口。'],
            'rejected_actions': [{'action_type': 'create_purchase_request', 'reason': '调拨可更快覆盖短缺。'}],
        },
    }
    decision = planner_decision_data(case)
    card = ResolveOpsWorkbench._planner_decision_card(decision)
    assert card is not None
    assert 'Decision Summary · auditable model output' in planner_decision_text(decision)
    assert 'Decision Trace' in planner_decision_text(decision)
    assert 'Rejected Actions' in planner_decision_text(decision)


def test_canvas_mounts_same_turn_tools_in_visible_horizontal_rows():
    case = {
        **CASE,
        'events': [
            {'id': 'context', 'kind': 'context_built', 'data': {
                'scope': {'case_id': 'CASE-1', 'tenant_id': 'demo', 'order_id': 'SO-1'},
                'current_state': {'status': 'running', 'plan_version': 0},
                'agent_limits': {'read_tool_parallelism': 4},
            }},
            {'id': 'agent', 'kind': 'agent_start', 'data': {}},
            {'id': 'call-1', 'kind': 'tool_call', 'data': {'turn': 1, 'tool': 'get_order'}},
            {'id': 'call-2', 'kind': 'tool_call', 'data': {'turn': 1, 'tool': 'get_inventory'}},
            {'id': 'obs-1', 'kind': 'tool_observation', 'data': {'tool': 'get_order', 'result': {'name': 'SO-1', 'items': []}}},
            {'id': 'obs-2', 'kind': 'tool_observation', 'data': {'tool': 'get_inventory', 'result': {'warehouse': 'Stores', 'actual_qty': 20, 'reserved_qty': 5}}},
        ],
        'tool_trace': {'observations': [
            {'evidence_id': 'E-001', 'tool': 'get_order'},
            {'evidence_id': 'E-002', 'tool': 'get_inventory'},
        ]},
    }
    app = ResolveOpsWorkbench(FakeClient(), poll_interval=60)

    async def scenario():
        async with app.run_test() as pilot:
            app._activate_case(case)
            await pilot.pause()
            await pilot.pause()
            assert len(app.query('.parallel-grid')) == 1
            assert len(app.query('.parallel-row')) == 1
            assert len(app.query('.tool-card')) == 2
            rendered = [str(widget.render()) for widget in app.query('Static')]
            assert any('Context Update' in text for text in rendered)
            assert any('fan-out' in text for text in rendered)
            assert any('join' in text for text in rendered)

    asyncio.run(scenario())


def test_handoff_candidates_are_not_rendered_as_an_accepted_action_proposal():
    """A Planner fallback must not look like a policy-approved write plan."""
    case = {
        **CASE,
        'status': 'manual_review',
        'events': [
            {'id': 'context', 'kind': 'context_built', 'data': {
                'scope': {'case_id': 'CASE-1', 'tenant_id': 'demo', 'order_id': 'SO-1'},
                'current_state': {'status': 'running', 'plan_version': 0},
            }},
            {'id': 'trace', 'kind': 'agent_decision_trace', 'data': {
                'recommended_actions': [{'action_type': 'create_manual_ticket'}],
                'missing_information': ['get_order unavailable: its business fact remains unknown.'],
            }},
            {'id': 'handoff', 'kind': 'handoff', 'data': {}},
        ],
    }
    app = ResolveOpsWorkbench(FakeClient(), poll_interval=60)

    async def scenario():
        async with app.run_test() as pilot:
            app._activate_case(case)
            await pilot.pause()
            texts = [str(widget.render()) for widget in app.query('Static')]
            assert not any('Action Proposal · typed write intent' in text for text in texts)
            assert any('PLAN · 未生成可执行 Write Tool' in text for text in texts)

    asyncio.run(scenario())


def test_parallel_rows_respect_scheduler_cap_without_hiding_tools_offscreen():
    assert ResolveOpsWorkbench._parallel_columns(4, 8) == 4
    assert ResolveOpsWorkbench._parallel_columns(None, 8) == 4
    assert ResolveOpsWorkbench._parallel_columns(1, 3) == 1


def test_context_update_exposes_confirmed_evidence_as_the_next_turn_input():
    tools = [
        {'tool': 'get_order', 'observation_index': 1, 'result': {'name': 'SO-1'}},
        {'tool': 'get_inventory', 'observation_index': 2, 'result': {'error': 'timeout'}},
    ]
    text = context_update_text(tools, {1: 'E-001', 2: 'E-002'}, 3)
    assert '写入：E-001 · get_order' in text
    assert 'E-002' not in text
    assert 'Short-term Context：3 → 4' in text


def test_case_run_metrics_show_only_measured_single_case_facts():
    app = ResolveOpsWorkbench(FakeClient())
    app._case_metrics['CASE-1'] = {
        'llm_call_count': 2,
        'llm_telemetry_call_count': 2,
        'llm_total_tokens': 240,
        'llm_token_usage_call_count': 2,
        'llm_prompt_tokens': 180,
        'llm_completion_tokens': 60,
        'avg_llm_latency_ms': 120,
        'llm_latency_sample_count': 2,
        'tool_call_count': 3,
        'scheduled_tool_call_count': 3,
        'tool_failure_count': 1,
        'avg_tool_latency_ms': 30,
        'max_tool_latency_ms': 50,
        'observed_tool_latency_count': 3,
        'duration_seconds': 1.5,
        'replan_count': 0,
        'plan_repair_count': 0,
        'duplicate_tool_call_count': 0,
    }
    summary = str(app._run_metrics_widget(CASE).render())
    assert 'Run Metrics · LLM 请求 2 次 · Token 240' in summary
    assert '任务完成率' not in summary
    app.metrics_expanded = True
    detail = str(app._run_metrics_widget(CASE).render())
    assert 'LLM · telemetry=2/2 · input=180 · output=60 · avg latency=120 ms（样本 2/2）' in detail
    assert 'Tool · success=2 · failure=1' in detail


def test_replan_starts_a_distinct_context_and_turn_sequence():
    case = {
        **CASE,
        'events': [
            {'id': 'context-1', 'kind': 'context_built', 'data': {'scope': {'case_id': 'CASE-1'}}},
            {'id': 'start-1', 'kind': 'turn_start', 'data': {'turn': 1}},
            {'id': 'call-1', 'kind': 'tool_call', 'data': {'turn': 1, 'tool': 'get_order'}},
            {'id': 'obs-1', 'kind': 'tool_observation', 'data': {'tool': 'get_order', 'result': {'name': 'SO-1'}}},
            {'id': 'replan', 'kind': 'replan_requested', 'data': {}},
            {'id': 'context-2', 'kind': 'context_built', 'data': {'scope': {'case_id': 'CASE-1'}, 'confirmed_observation_count': 1}},
            {'id': 'start-2', 'kind': 'turn_start', 'data': {'turn': 1}},
            {'id': 'call-2', 'kind': 'tool_call', 'data': {'turn': 1, 'tool': 'get_inventory'}},
            {'id': 'obs-2', 'kind': 'tool_observation', 'data': {'tool': 'get_inventory', 'result': {'warehouse': 'Stores', 'actual_qty': 1, 'reserved_qty': 0}}},
        ],
    }
    runs = build_investigation_runs(case)
    batches = build_turn_batches(case)
    assert [run['run'] for run in runs] == [1, 2]
    assert [(batch['run'], batch['turn']) for batch in batches] == [(1, 1), (2, 1)]


def test_case_poll_does_not_redraw_an_unchanged_execution_canvas():
    app = ResolveOpsWorkbench(FakeClient())
    app.active_case_id = 'CASE-1'
    app.active_case = CASE
    app._last_canvas_signature = app._canvas_signature(CASE)
    renders = []
    app._render_case = lambda case, append_only=False: renders.append((case, append_only))

    app._apply_case_poll('CASE-1', CASE)

    assert renders == []


def test_local_role_switch_changes_the_real_api_client_key():
    app = ResolveOpsWorkbench(FakeClient())
    app._demo_operator_keys = {'warehouse_manager': 'warehouse-key', 'sales_manager': 'sales-key'}
    app._render_operator_identity = lambda: None
    app._write = lambda *_args, **_kwargs: None
    app.action_refresh = lambda: None

    app.switch_demo_role('warehouse_manager')

    assert app.active_operator_role == 'warehouse_manager'
    assert app.client.operator_key == 'warehouse-key'


def test_approval_controls_keep_a_clickable_button_height():
    css = ResolveOpsWorkbench.CSS
    assert '#approval-card { height: auto; min-height: 8;' in css
    assert '#approval-actions { height: 3; min-height: 3;' in css
    assert '#approve-action, #reject-replan-action { height: 3; min-height: 3; }' in css


def test_workbench_title_prioritizes_agent_and_business_scenario():
    assert ResolveOpsWorkbench.TITLE == 'ResolveOps Agent - 企业 ERP 订单异常处理'
    assert 'operator-identity' not in ResolveOpsWorkbench.compose.__code__.co_consts


def test_reset_demo_opens_an_explicit_confirmation_boundary():
    app = ResolveOpsWorkbench(FakeClient())
    opened = []
    app.push_screen = lambda screen, callback: opened.append((screen, callback))

    app.open_sandbox_reset()

    assert opened
    assert opened[0][0].__class__.__name__ == 'ConfirmSandboxResetScreen'


def test_console_routes_general_and_case_questions_without_cross_case_context():
    client = FakeClient()
    app = ResolveOpsWorkbench(client, poll_interval=60)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.ask_general('hello', [])
            await pilot.pause()
            await pilot.pause()
            assert any(call['path'] == '/v1/chat/stream' for call in client.calls)
            assert app.active_case_id is None

            app._activate_case(CASE)
            app.ask_case('CASE-1', 'Why transfer?')
            await pilot.pause()
            await pilot.pause()
            case_calls = [call for call in client.calls if call['path'] == '/v1/cases/CASE-1/ask/stream']
            assert case_calls[-1]['payload'] == {'question': 'Why transfer?'}
            assert app.general_history[-1]['content'] == 'General answer.'
            assert app.case_transcript['CASE-1'][-1][1] == 'Case answer.'

            app.action_back_to_general()
            assert app.active_case_id is None
            assert 'CASE-1' in app.case_transcript

    asyncio.run(scenario())
