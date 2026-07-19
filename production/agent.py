"""Bounded evidence-gathering loop; it exposes only read tools to the LLM."""
from __future__ import annotations
import json
from datetime import date
from typing import Any
from .actions import planner_action_catalog, planner_action_instructions, registered_action_types
from .config import settings
from .llm_gateway import LLMGateway
from .tool_result import ToolResult
from .tool_scheduler import ReadToolCall, ReadToolScheduler
SYSTEM='''You are ResolveOps' investigation agent for ERP business exceptions.
Use a deliberate loop: form a hypothesis, call the single most useful read tool, and update your hypothesis from its result. Never invent facts or propose direct ERP writes.
Investigate dynamically from case_context.scope.event_type:
- inventory_shortage: may be caused by available stock, reservations, inbound supply, transfer feasibility, or customer constraints.
- price_mismatch: compare the order item rate against reference price evidence; do not propose changing ERP prices directly. If a mismatch is supported, propose create_price_review_ticket.
- delivery_delay: compare customer delivery date with inbound purchase schedule evidence. If an inbound purchase is late, propose create_supplier_followup_task; do not change ERP purchase or sales dates directly.
A tool error means that fact is UNKNOWN: never convert an error, timeout, or missing permission into a negative business fact (for example, "no inbound purchase"). Put it in missing_information or hand off.
Customer profile includes allows_partial_delivery. true means partial/split delivery may be considered; false means do not recommend split delivery; null means the constraint is unknown and must not be assumed.
Before recommending a transfer, you must have read the order, target inventory, proposed source inventory, and transfer options for the target warehouse. Before recommending a purchase request, read the order, target inventory, item supply profile, and inbound purchase status; failed inbound-purchase lookup remains UNKNOWN, not evidence of no supply. Do not repeat an identical tool call.
Before recommending create_price_review_ticket, you must have read the order and get_reference_price for the same SKU. For price_mismatch, do not call inventory, transfer, inbound purchase, or supply profile tools unless the Case context explicitly includes a fulfilment problem too.
Before recommending create_supplier_followup_task, you must have read the order and get_inbound_purchase for the same SKU. The inbound schedule_date must be later than the customer delivery date.
When evidence is sufficient, return ONLY JSON with: status (ready|handoff), recommended_actions (array), alternatives (array), rationale (string), missing_information (array), and evidence_summary (array). A recommended plan may contain one action or a coordinated set of actions. Choose based on evidence; do not always combine actions. Write actions are not directly callable tools; propose them only in recommended_actions using the Action Schemas supplied by the runtime. Include risk, preconditions and expected_effect.'''
PLANNER_SYSTEM_BASE='''You are the planning phase of ResolveOps. Use only the supplied ERP observations as facts. Tool errors mean unknown, never a negative fact. Return JSON only with status, recommended_actions, alternatives, rationale, missing_information, evidence_summary. missing_information must be an array. recommended_actions is an array with one to three actions. Choose a single action when it is best; choose a coordinated set only when the combined evidence makes it better.
For inventory_shortage, recommended actions must jointly resolve the shortage. Customer allows_partial_delivery=true permits split delivery as an option; false blocks it; null is unknown. get_transfer_options is the only evidence for transfer transit days and unit cost. get_item_supply_profile is the only evidence for replenishment lead time. Never claim a route or purchase can meet the delivery date without a tool result supporting that claim. Do not hand off only because purchase unit cost is unknown when the action is a reversible draft purchase request and lead time evidence meets the delivery date; put non-blocking gaps in missing_information and return ready.
For price_mismatch, compare the Sales Order item rate with get_reference_price.reference_rate. If they differ and both facts are observed, propose create_price_review_ticket. Never propose directly changing the Sales Order price.
For price_mismatch, do not use inventory or replenishment actions.
For delivery_delay, compare Sales Order delivery_date with inbound purchase schedule_date. If schedule_date is later, propose create_supplier_followup_task. Never propose changing ERP delivery dates directly.
Return handoff only when no executable plan can be safely proposed from the evidence.'''
REPAIR_SYSTEM='''You repair ResolveOps planner output into valid JSON only.
Do not add new business facts. Do not invent tool results. Use only the provided evidence and available action schemas.
Return exactly one JSON object with keys: status, recommended_actions, alternatives, rationale, missing_information, evidence_summary.
status must be "ready" or "handoff"; recommended_actions, alternatives, missing_information, and evidence_summary must be arrays.
Each recommended action must use action_type and input matching one available action schema.'''
class InvestigationAgent:
    def __init__(self, tools, llm_gateway: LLMGateway | None = None):
        self.tools=tools
        self.llm=llm_gateway or LLMGateway()
    def run(self, order_id, on_observation, context: str | dict[str, Any] = ''):
        user=f'Investigate order {order_id}. Start by reading the order.'
        context_text = self._context_text(context)
        if context_text: user+=f'\nCase context: {context_text}. Treat previous availability as stale when context shows replanning or preflight failure; gather fresh evidence before proposing any action.'
        messages=[{'role':'system','content':SYSTEM},{'role':'user','content':user}]
        seen={}
        failed_tools=[]
        observations=[]
        budget_exhausted=False
        scheduler=ReadToolScheduler(self.tools, max_workers=settings.agent_read_tool_parallelism)
        max_turns=max(1, settings.agent_max_investigation_turns)
        max_read_tool_calls=max(1, settings.agent_max_read_tool_calls)
        for _ in range(max_turns):
            # DeepSeek Thinking mode supports automatic tool choice, not a
            # forced function name. Evidence validation below prevents a plan
            # being created when the model skips the necessary read calls.
            payload={'messages':messages,'tools':self.tools.definitions(),'tool_choice':'auto','temperature':0}
            llm_result=self.llm.chat(payload)
            if not llm_result.ok:
                return {'status':'handoff','recommended_actions':[],'alternatives':[],'rationale':'LLM investigation call failed before sufficient evidence was gathered.','missing_information':[llm_result.error_code or 'llm_error'],'llm':llm_result.telemetry()}
            message=llm_result.first_message() or {}; messages.append(message); calls=message.get('tool_calls') or []
            if not calls:
                break
            batch=[]
            malformed=[]
            for call in calls:
                if len(observations) >= max_read_tool_calls:
                    budget_exhausted=True
                    break
                try:
                    args=json.loads(call['function']['arguments'] or '{}')
                    if not isinstance(args, dict):
                        raise ValueError('tool arguments must be a JSON object')
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    malformed.append((call, ToolResult.failure('invalid_tool_arguments', error_type=type(exc).__name__)))
                    continue
                batch.append(ReadToolCall(call_id=call['id'],name=call['function']['name'],arguments=args))
            for call, tool_result in malformed:
                failed_tools.append(call['function']['name'])
                result=tool_result.observation_result()
                on_observation(call['function']['name'],{},result,tool_result.to_dict())
                observations.append({'tool':call['function']['name'],'arguments':{},'result':result,'tool_result':tool_result.to_dict(),'scheduler':{'source':'invalid_arguments'}})
                messages.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(tool_result.to_dict(),ensure_ascii=False)})
            for execution in scheduler.execute_batch(batch, order_id, seen):
                call=execution.call
                tool_result=execution.result
                result=tool_result.observation_result()
                scheduler_meta={'source':execution.source,'signature':execution.signature}
                tool_result_dict=tool_result.to_dict()
                tool_result_dict['scheduler'] = scheduler_meta
                on_observation(call.name,call.arguments,result,tool_result_dict)
                observations.append({'tool':call.name,'arguments':call.arguments,'result':result,'tool_result':tool_result_dict,'scheduler':scheduler_meta})
                if result.get('error'): failed_tools.append(call.name)
                messages.append({'role':'tool','tool_call_id':call.call_id,'content':json.dumps(tool_result_dict,ensure_ascii=False)})
            if budget_exhausted:
                break
        return self._plan(order_id, observations, failed_tools, context, budget_exhausted)

    def _plan(self, order_id, observations, failed_tools, context: str | dict[str, Any], budget_exhausted=False):
        if not observations:
            return {'status':'handoff','recommended_actions':[],'alternatives':[],'rationale':'Agent produced no read-tool evidence.','missing_information':['order and inventory evidence']}
        event_type = self._event_type(context)
        evidence={'order_id':order_id,'current_date':date.today().isoformat(),'case_context':context or None,'observations':observations,'available_action_schemas':planner_action_catalog(event_type)}
        planner_system=PLANNER_SYSTEM_BASE + '\n\n' + planner_action_instructions(event_type)
        payload={'messages':[{'role':'system','content':planner_system},{'role':'user','content':json.dumps(evidence,ensure_ascii=False)}],'response_format':{'type':'json_object'},'temperature':0}
        llm_result=self.llm.chat(payload)
        if not llm_result.ok:
            return {'status':'handoff','recommended_actions':[],'alternatives':[],'rationale':'LLM planning call failed; automation stopped before write planning.','missing_information':[llm_result.error_code or 'llm_error'],'llm':llm_result.telemetry()}
        raw_content=(llm_result.first_message() or {}).get('content')
        conclusion=self._parse_conclusion(raw_content)
        conclusion['llm']=llm_result.telemetry()
        if self._needs_schema_repair(conclusion):
            repaired=self._repair_conclusion(raw_content,evidence,planner_system,conclusion)
            if repaired:
                conclusion=repaired
                conclusion['llm']=llm_result.telemetry()
        if failed_tools:
            unknown=[f'{name} unavailable: its business fact remains unknown.' for name in sorted(set(failed_tools))]
            conclusion['missing_information']=list(dict.fromkeys((conclusion.get('missing_information') or [])+unknown))
        if budget_exhausted:
            conclusion['missing_information']=list(dict.fromkeys((conclusion.get('missing_information') or [])+['read-tool budget exhausted; plan uses only collected evidence']))
        return conclusion

    def _repair_conclusion(self, raw_content: Any, evidence: dict[str, Any], planner_system: str, parse_error: dict[str, Any]) -> dict[str, Any] | None:
        payload={
            'messages':[
                {'role':'system','content':REPAIR_SYSTEM},
                {'role':'user','content':json.dumps({
                    'planner_system': planner_system,
                    'parse_error': parse_error,
                    'invalid_output': raw_content,
                    'evidence': evidence,
                },ensure_ascii=False)},
            ],
            'response_format':{'type':'json_object'},
            'temperature':0,
        }
        repair_result=self.llm.chat(payload)
        if not repair_result.ok:
            parse_error['schema_repair']={
                'status':'failed',
                'reason':repair_result.error_code or 'llm_error',
                'llm':repair_result.telemetry(),
            }
            return None
        repaired=self._parse_conclusion((repair_result.first_message() or {}).get('content'))
        repaired['llm_repair']=repair_result.telemetry()
        if self._needs_schema_repair(repaired):
            parse_error['schema_repair']={
                'status':'failed',
                'reason':'repair_output_invalid',
                'llm':repair_result.telemetry(),
            }
            return None
        repaired['schema_repair']={'status':'repaired','reason':'planner_output_repaired_once'}
        return repaired

    @staticmethod
    def _parse_conclusion(content):
        """Models often wrap valid JSON in ```json fences; accept that, never prose."""
        if not isinstance(content, str):
            return {'status':'handoff','recommended_actions':[],'alternatives':[],'rationale':'Model returned no structured conclusion.','missing_information':[]}
        cleaned=content.strip()
        if cleaned.startswith('```'):
            cleaned=cleaned.split('\n',1)[1] if '\n' in cleaned else ''
            if cleaned.rstrip().endswith('```'): cleaned=cleaned.rstrip()[:-3].strip()
        try:
            result=json.loads(cleaned)
            required={'status','alternatives','rationale','missing_information'}
            if not isinstance(result,dict) or not required <= result.keys(): raise ValueError('missing required fields')
            if 'recommended_actions' not in result:
                result['recommended_actions']=[result['recommended_action']] if result.get('recommended_action') else []
            if not isinstance(result['recommended_actions'],list): raise ValueError('recommended_actions must be an array')
            if isinstance(result.get('missing_information'),str):
                result['missing_information']=[result['missing_information']]
            # Accept only two documented provider variants, then return to the
            # strict Action Registry boundary.  This is schema normalization,
            # not permission or policy inference.
            if result.get('status') == 'shortage': result['status']='ready'
            allowed_actions=registered_action_types()
            for action in result['recommended_actions']:
                if isinstance(action,dict) and 'action_type' not in action:
                    if action.get('action') in allowed_actions:
                        action['action_type']=action.pop('action')
                    elif action.get('tool') in allowed_actions:
                        action['action_type']=action.pop('tool')
            if result.get('status') not in {'ready','handoff'}: raise ValueError('unsupported status')
            return result
        except (ValueError,json.JSONDecodeError):
            return {'status':'handoff','recommended_actions':[],'alternatives':[],'rationale':'Model conclusion did not meet the required JSON schema.','missing_information':[],'parse_error':'required_json_schema_mismatch'}

    @staticmethod
    def _needs_schema_repair(conclusion: dict[str, Any]) -> bool:
        return conclusion.get('parse_error') == 'required_json_schema_mismatch'

    @staticmethod
    def _context_text(context: str | dict[str, Any]) -> str:
        if not context:
            return ''
        if isinstance(context, str):
            return context
        return json.dumps(context, ensure_ascii=False)

    @staticmethod
    def _event_type(context: str | dict[str, Any]) -> str | None:
        if not isinstance(context, dict):
            return None
        scope = context.get('scope') if isinstance(context.get('scope'), dict) else {}
        event_type = scope.get('event_type')
        return str(event_type) if event_type else None
