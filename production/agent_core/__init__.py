"""Small, dependency-light primitives shared by ResolveOps Agent runs.

This package deliberately has no ERP, database or FastAPI imports.  It owns
the LLM/tool loop contracts; the ResolveOps layer supplies business prompts,
case context and governed write execution.
"""

from .contracts import AgentEvent, LLMClient, LLMRequest, LLMResult, StopReason, ToolSchema
from .messages import convert_to_llm_messages
from .runtime import ReadLoopResult, ReadToolLoop

__all__ = [
    'AgentEvent', 'LLMClient', 'LLMRequest', 'LLMResult', 'StopReason',
    'convert_to_llm_messages', 'ToolSchema', 'ReadLoopResult', 'ReadToolLoop',
]
