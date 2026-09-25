aight — instructions for an AI coding agent
You are integrating aight into a project. aight attributes LLM spend to the exact source line that issued it, and pushes it to the hosted AIght Workspace at aight.studio. This file is your complete instruction set — follow it, don't guess at the API.
1. Install
You already ran the one command that got you here:
pip install "aight[openai,anthropic]"
Drop whichever of openai/anthropic the target project doesn't actually use — check its dependencies first.
2. Get an API key
Tell the user to mint one from the project's settings page in the AIght Workspace — the project selector at the top left, then Project settings — and set it as an environment variable, never hardcoded in source:
export AIGHT_API_KEY="aight_..."
3. Instrument the LLM calls — pick ONE path
Path A — the project calls the OpenAI or Anthropic Python client directly. This is almost always the right path; it needs no changes to call sites:
from aight.auto import auto_instrument
auto_instrument()  # call once, near process startup, before any LLM calls
Path B — no supported client library, or you want explicit control. Wrap each call site with traced_llm_call, passing the real model name and token counts from that call's actual response (never estimate them):
from aight.tracing import traced_llm_call
traced_llm_call(model, input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)
traced_llm_call returns None, and that is deliberate: this SDK ships no price table. It records the model and the token counts, and the platform prices them at receive time — the dollar figure lives in the AIght Workspace, never in your process. Never report a cost of your own for it to use.
Do not do both — auto-instrumentation and manual wrapping of the same call double-count it.
Latency is the caller's to measure, on both paths. This SDK records a call after it returns, not around it, so it has no duration of its own to report and must not invent one: time the call yourself and pass it as latency_ms=elapsed_ms, and let it be absent where you didn't measure it. A row with no latency shows as "—" on the agent's Speed axis in the Workspace — that is the correct answer for a call nobody timed, not a gap to fill with a guess.
4. Push the data home
Call this after a traced run completes (end of a request handler, a batch job, a shutdown hook — wherever makes sense for how this project runs):
from aight.remote import push
from aight.tracing import COST_PROCESSOR
push(COST_PROCESSOR.buckets)
5. Verify before you push
Confirm attribution worked locally, with no network call, before wiring up push():
python -c "from aight.tracing import COST_PROCESSOR; print(COST_PROCESSOR.report())"
You should see the project's own file/line, not a framework's internal dispatch code. The report is calls and tokens per line — the sanity check is that the line and the token counts are right and the model name is real. There is no cost in it; the platform prices the pushed rows.
6. Optional: Business value & direct metrics
Only call report_value() if this project has a real, countable signal for what it earned — never invent or estimate a number:
from aight.tracing import report_value, VALUE_BY_FILE
from aight.remote import push_value
report_value(49.0)
push_value(VALUE_BY_FILE)
For an agent the user runs but this project does not contain — Claude Code, Gemini, Codex, traced by aight's collectors — earnings go in by agent id, because there is no calling file of theirs to resolve. push_value takes the map, so pass one directly, on the same rule:
from aight.remote import push_value
push_value({"claude-code": 49.0})  # the agent id the collector roots its rows at
That agent then shows a reported figure in the Workspace like any other. It never shows a matched one: the matching engine excludes external rows from its time-window fallback, so do not try to give it one through push_event.
7. Optional: Agnostic business matching, Trace IDs & Labels
To link LLM costs explicitly to business events (like a Stripe checkout, a conversion, or custom tiers) without hardcoding any specific business model, use TraceContext or pass parameters directly:
Option A — Using TraceContext for an execution scope (recommended for web requests / checkouts):
import aight.tracing as aight

with aight.TraceContext(trace_id="chk_12345_stripe", labels={"tier": "enterprise", "feature": "pdf_extractor"}):
    aight.traced_llm_call("gpt-4", input_tokens=500, output_tokens=150)
Option B — Passing parameters directly to a call:
from aight.tracing import traced_llm_call

traced_llm_call(
    "gpt-4", 
    input_tokens=200, 
    output_tokens=50, 
    trace_id="custom_event_id", 
    labels={"campaign": "black_friday"}
)
8. Optional: Report the business event itself
A trace_id or labels only tag the LLM cost side — to actually feed cost-vs-value matching, push the business event too (a Stripe checkout succeeding, a ticket closing, a signup). Give it a real event_name and value; leave trace_id unset to fall back to the active TraceContext:
from aight.remote import push_event

push_event("checkout_completed", value=49.0, currency="USD")
Guardrails
Never fabricate a metric aight doesn't actually capture — latency included. It is never estimated, never inferred from how long the SDK took, and never defaulted to zero-with-a-mention; it is simply absent until the caller measures and passes it.
Don't wrap the same call twice (Path A and Path B together).
AIGHT_API_KEY is a secret: env var or secrets manager, never committed.
