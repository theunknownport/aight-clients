"""AIght SDK: attribute LLM spend to the exact source line that issued it,
and push that data to your hosted AIght Workspace.

Auto-instrument a supported provider so every real call is traced with no
per-call wrapping. Needs that provider's extra (`aight[openai]` /
`aight[anthropic]`), installed from the git URL in the README — this SDK is
not on PyPI:

    from aight.auto import auto_instrument
    from aight.remote import push
    from aight.tracing import COST_PROCESSOR

    auto_instrument()
    # ...call your LLM client as normal...
    push(COST_PROCESSOR.buckets)

Or trace manually, call by call:

    from aight.tracing import traced_llm_call, COST_PROCESSOR
    from aight.remote import push

    traced_llm_call("gpt-4o-mini", input_tokens=180, output_tokens=60)
    push(COST_PROCESSOR.buckets)
"""

__version__ = "0.2.0"
