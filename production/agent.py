"""Bounded evidence-gathering loop; it exposes only read tools to the LLM."""
from __future__ import annotations
import json
from datetime import date
from typing import Any
import httpx
from .actions import planner_action_catalog, planner_action_instructions, registered_action_types
from .config import settings
from .tool_result import ToolResult
SYSTEM='''You are ResolveOps' investigation agent for ERP business exceptions.
Use a deliberate loop: form a hypothesis, call the single most useful read tool, and update your hypothesis from its result. Never invent facts or propose direct ERP writes.
Investigate dynamically from case_context.scope.event_type:
- inventory_shortage: may be caused by available stock, reservations, inbound supply, transfer feasibility, or customer constraints.
- price_mismatch: compare the order item rate against reference price evidence; do not propose changing ERP prices directly. If a mismatch is supported, propose create_price_review_ticket.
A tool error means that fact is UNKNOWN: never convert an error, timeout, or missing permission into a negative business fact (for example, "no inbound purchase"). Put it in missing_information or hand off.
Customer profile includes allows_partial_delivery. true means partial/split delivery may be considered; false means do not recommend split delivery; null means the constraint is unknown and must not be assumed.
Before recommending a transfer, you must have read the order, target inventory, proposed source inventory, and transfer options for the target warehouse. Before recommending a purchase request, read the order, target inventory, item supply profile, and inbound purchase status; failed inbound-purchase lookup remains UNKNOWN, not evidence of no supply. Do not repeat an identical tool call.
Before recommending create_price_review_ticket, you must have read the order and get_reference_price for the same SKU.
When evidence is sufficient, return ONLY JSON with: status (ready|handoff), recommended_actions (array), alternatives (array), rationale (string), missing_information (array), and evidence_summary (array). A recommended plan may contain one action or a coordinated set of actions. Choose based on evidence; do not always combine actions. Write actions are not directly callable tools; propose them only in recommended_actions using the Action Schemas supplied by the runtime. Include risk, preconditions and expected_effect.'''
PLANNER_SYSTEM_BASE='''You are the planning phase of ResolveOps. Use only the supplied ERP observations as facts. Tool errors mean unknown, never a negative fact. Return JSON only with status, recommended_actions, alternatives, rationale, missing_information, evidence_summary. missing_information must be an array. recommended_actions is an array with one to three actions. Choose a single action when it is best; choose a coordinated set only when the combined evidence makes it better.
For inventory_shortage, recommended actions must jointly resolve the shortage. Customer allows_partial_delivery=true permits split delivery as an option; false blocks it; null is unknown. get_transfer_options is the only evidence for transfer transit days and unit cost. get_item_supply_profile is the only evidence for replenishment lead time. Never claim a route or purchase can meet the delivery date without a tool result supporting that claim. Do not hand off only because purchase unit cost is unknown when the action is a reversible draft purchase request and lead time evidence meets the delivery date; put non-blocking gaps in missing_information and return ready.
For price_mismatch, compare the Sales Order item rate with get_reference_price.reference_rate. If they differ and both facts are observed, propose create_price_review_ticket. Never propose directly changing the Sales Order price.
Return handoff only when no executable plan can be safely proposed from the evidence.'''
class InvestigationAgent:
    def __init__(self, tools): self.tools=tools
    def run(self, order_id, on_observation, context: str | dict[str, Any] = ''):
        user=f'Investigate order {order_id}. Start by reading the order.'
        context_text = self._context_text(context)
        if context_text: user+=f'\nCase context: {context_text}. Treat previous availability as stale when context shows replanning or preflight failure; gather fresh evidence before proposing any action.'
        messages=[{'role':'system','content':SYSTEM},{'role':'user','content':user}]
        seen={}
        failed_tools=[]
        observations=[]
        budget_exhausted=False
        max_turns=max(1, settings.agent_max_investigation_turns)
        max_read_tool_calls=max(1, settings.agent_max_read_tool_calls)
        for _ in range(max_turns):
            # DeepSeek Thinking mode supports automatic tool choice, not a
            # forced function name. Evidence validation below prevents a plan
            # being created when the model skips the necessary read calls.
            payload={'model':settings.llm_model,'messages':messages,'tools':self.tools.definitions(),'tool_choice':'auto','temperature':0}
            r=httpx.post(settings.llm_base_url.rstrip('/')+'/chat/completions',headers={'Authorization':f'Bearer {settings.llm_api_key}'},json=payload,timeout=30); r.raise_for_status()
            message=r.json()['choices'][0]['message']; messages.append(message); calls=message.get('tool_calls') or []
            if not calls:
                break
            for call in calls:
                if len(observations) >= max_read_tool_calls:
                    budget_exhausted=True
                    break
                args=json.loads(call['function']['arguments']); signature=(call['function']['name'],json.dumps(args,sort_keys=True,ensure_ascii=False))
                tool_result=seen[signature] if signature in seen else self._execute_tool(call['function']['name'],args,order_id)
                seen[signature]=tool_result
                result=tool_result.observation_result()
                on_observation(call['function']['name'],args,result,tool_result.to_dict())
                observations.append({'tool':call['function']['name'],'arguments':args,'result':result,'tool_result':tool_result.to_dict()})
                if result.get('error'): failed_tools.append(call['function']['name'])
                messages.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(tool_result.to_dict(),ensure_ascii=False)})
            if budget_exhausted:
                break
        return self._plan(order_id, observations, failed_tools, context, budget_exhausted)

    def _plan(self, order_id, observations, failed_tools, context: str | dict[str, Any], budget_exhausted=False):
        if not observations:
            return {'status':'handoff','recommended_actions':[],'alternatives':[],'rationale':'Agent produced no read-tool evidence.','missing_information':['order and inventory evidence']}
        evidence={'order_id':order_id,'current_date':date.today().isoformat(),'case_context':context or None,'observations':observations,'available_action_schemas':planner_action_catalog()}
        planner_system=PLANNER_SYSTEM_BASE + '\n\n' + planner_action_instructions()
        payload={'model':settings.llm_model,'messages':[{'role':'system','content':planner_system},{'role':'user','content':json.dumps(evidence,ensure_ascii=False)}],'response_format':{'type':'json_object'},'temperature':0}
        r=httpx.post(settings.llm_base_url.rstrip('/')+'/chat/completions',headers={'Authorization':f'Bearer {settings.llm_api_key}'},json=payload,timeout=30); r.raise_for_status()
        conclusion=self._parse_conclusion(r.json()['choices'][0]['message'].get('content'))
        if failed_tools:
            unknown=[f'{name} unavailable: its business fact remains unknown.' for name in sorted(set(failed_tools))]
            conclusion['missing_information']=list(dict.fromkeys((conclusion.get('missing_information') or [])+unknown))
        if budget_exhausted:
            conclusion['missing_information']=list(dict.fromkeys((conclusion.get('missing_information') or [])+['read-tool budget exhausted; plan uses only collected evidence']))
        return conclusion

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
            return {'status':'handoff','recommended_actions':[],'alternatives':[],'rationale':'Model conclusion did not meet the required JSON schema.','missing_information':[]}

    def _execute_tool(self, name: str, args: dict[str, Any], order_id: str) -> ToolResult:
        if hasattr(self.tools, 'execute_result'):
            return self.tools.execute_result(name, args, order_id)
        result = self.tools.execute(name, args, order_id)
        if isinstance(result, dict) and result.get('error'):
            return ToolResult.failure(result.get('error'), error_type=result.get('error_type'))
        return ToolResult.success(result if isinstance(result, dict) else {'value': result})

    @staticmethod
    def _context_text(context: str | dict[str, Any]) -> str:
        if not context:
            return ''
        if isinstance(context, str):
            return context
        return json.dumps(context, ensure_ascii=False)
