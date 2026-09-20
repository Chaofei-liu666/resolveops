from production.agent_core import LLMRequest, LLMResult, ReadToolLoop, convert_to_llm_messages
from production.tool_result import ToolResult


class FakeTools:
    def definitions(self):
        return []


class OneTurnLLM:
    def chat(self, payload):
        return LLMResult(status='success', response={'choices': [{'message': {'role': 'assistant', 'content': 'done'}}]})


class TurnAwareTools:
    def definitions(self):
        return [
            {'type': 'function', 'function': {'name': 'get_order', 'parameters': {'type': 'object'}}},
            {'type': 'function', 'function': {'name': 'get_inventory', 'parameters': {'type': 'object'}}},
        ]

    def execute_result(self, name, arguments, _order_id):
        return ToolResult.success({'tool': name, 'arguments': arguments})


class TwoTurnLLM:
    def __init__(self):
        self.payloads = []

    def chat(self, payload):
        self.payloads.append(payload)
        if len(self.payloads) == 1:
            return LLMResult(status='success', response={'choices': [{'message': {
                'role': 'assistant', 'tool_calls': [
                    {'id': 'order-1', 'function': {'name': 'get_order', 'arguments': '{}'}},
                ],
            }}]})
        return LLMResult(status='success', response={'choices': [{'message': {'role': 'assistant', 'content': 'enough evidence'}}]})


def test_llm_request_omits_unset_transport_fields():
    assert LLMRequest(messages=[{'role': 'user', 'content': 'hi'}]).to_payload() == {
        'messages': [{'role': 'user', 'content': 'hi'}],
    }


def test_message_boundary_keeps_only_provider_roles():
    messages = convert_to_llm_messages(
        system_prompt='system', user_message='question', case_context={'scope': {'case_id': 'C-1'}},
        transcript=[
            {'role': 'assistant', 'content': 'previous'},
            {'role': 'internal', 'content': 'must not leak'},
        ],
    )
    assert [message['role'] for message in messages] == ['system', 'assistant', 'user']
    assert 'must not leak' not in str(messages)


def test_read_loop_emits_lifecycle_and_finishes_without_tools():
    events = []
    run = ReadToolLoop(llm=OneTurnLLM(), tools=FakeTools(), max_turns=2, max_tool_calls=2, parallelism=1).run(
        messages=[{'role': 'system', 'content': 'test'}], order_id='SO-1',
        on_observation=lambda *_: None, on_event=events.append,
    )
    assert run.stop_reason == 'completed'
    assert [event.kind for event in events] == ['agent_start', 'turn_start', 'turn_end', 'agent_end']


def test_turn_specific_tool_surface_can_force_order_before_followup_tools():
    tools = TurnAwareTools()
    llm = TwoTurnLLM()
    order_only = [item for item in tools.definitions() if item['function']['name'] == 'get_order']
    run = ReadToolLoop(
        llm=llm,
        tools=tools,
        max_turns=3,
        max_tool_calls=3,
        parallelism=1,
        tool_definitions_for_turn=lambda turn: order_only if turn == 1 else tools.definitions(),
    ).run(messages=[{'role': 'system', 'content': 'test'}], order_id='SO-1', on_observation=lambda *_: None)

    assert run.stop_reason == 'completed'
    assert [item['function']['name'] for item in llm.payloads[0]['tools']] == ['get_order']
    assert [item['function']['name'] for item in llm.payloads[1]['tools']] == ['get_order', 'get_inventory']
