# Evaluation results

This page records the current local fixed-suite result. Do not mix these numbers with older debugging Cases.

Command:

```powershell
python resolveops.py eval summary --suite core-v4 --limit 50
```

Snapshot:

```text
scope: suite=core-v4 source_event_prefix=eval:core-v4:
cases: total=30 resolved=0 manual_review=0 waiting_approval=30
core: task_success=100.0% resolution=0.0% tool_selection=100.0% argument_correctness=100.0% evidence_faithfulness=100.0% verification=100.0%
efficiency: avg_read_calls=6.73 avg_duration=206.11s replan_success=n/a
runtime: llm_calls=32 total_tokens=318801 avg_tokens_per_case=10626.7 avg_llm_latency=11561ms avg_tool_latency=15ms max_tool_latency=42ms avg_queue_wait=206107ms read_budget_used=56.1% budget_exhausted_cases=0
trajectory: quality=99.0% duplicate_tools=6 self_correction_cases=7 unsafe_continuation_cases=0
diagnostics: grounding_failures=0 policy_denials=0 context_failures=0 task_failures=0
```

## How to read this result

`resolved=0` is expected for this fixed suite snapshot because the seeded Cases stop at approval. This suite is mainly used to evaluate investigation, planning, evidence grounding, policy routing and approval creation. Use separate approved execution runs when reporting end-to-end resolution.

The stronger numbers in this suite are:

| Metric | Result | Meaning |
|---|---:|---|
| Task success | 100.0% | Every Case reached a valid safe state: approval wait, resolved, or manual review. |
| Tool selection | 100.0% | The Agent selected valid read tools for the Case type. |
| Argument correctness | 100.0% | Planned action arguments matched observed tool evidence. |
| Evidence faithfulness | 100.0% | No action plan was accepted without grounding evidence. |
| Self-correction Cases | 7 | Seven Cases used the bounded plan repair path before producing a valid plan. |
| Unsafe continuation | 0 | No Case continued after a blocking safety condition. |

## Comparison with the previous suite

Before bounded plan repair:

```text
suite=core-v3
task_success=70.0%
argument_correctness=85.0%
evidence_faithfulness=70.0%
grounding_failures=9
self_correction_cases=0
```

After bounded plan repair:

```text
suite=core-v4
task_success=100.0%
argument_correctness=100.0%
evidence_faithfulness=100.0%
grounding_failures=0
self_correction_cases=7
```

Tradeoff:

```text
avg_tokens_per_case: 9751.7 -> 10626.7
avg_llm_latency: 11238ms -> 11561ms
avg_duration: 184.60s -> 206.11s
```

The added cost comes from one bounded repair call when the first plan fails deterministic grounding. The repair loop is intentionally limited so the Agent cannot keep reflecting indefinitely.

## Reporting guidance

For a concise project summary:

```text
Built a fixed 30-Case evaluation suite and measured task success, tool selection, action argument correctness, evidence faithfulness, token use, LLM/tool latency, self-correction and unsafe continuation. After adding bounded plan repair, evidence grounding failures dropped from 9 to 0 in the fixed suite, while self-correction was triggered in 7 Cases.
```

Avoid claiming production accuracy from this local suite. It is a sandbox evaluation, not a production benchmark.
