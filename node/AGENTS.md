aight — instructions for an AI coding agent
You are integrating aight into a Node.js project. aight attributes LLM spend to the exact source line that issued it, and pushes it to the hosted AIght Workspace at aight.studio. This file is your complete instruction set for the Node SDK — it is a smaller surface than the Python SDK (no label context), so don't guess at capabilities that aren't here.
1. Install
The package is published on npm as aight-sdk, and that is the specifier every example in this file uses. It is not `aight` — that name belongs to an unrelated package, so the suffix is not decoration. There is nothing to clone:
npm install aight-sdk
2. Get an API key
Tell the user to mint one from the project's settings page in the AIght Workspace — the project selector at the top left, then Project settings — and set it as an environment variable, never hardcoded in source:
export AIGHT_API_KEY="aight_..."
3. Instrument the LLM calls — pick ONE path
Path A — the project calls the OpenAI or Anthropic client directly. This is a real patch: the SDK registers the provider's OpenTelemetry instrumentation for you. It needs packages this SDK does not carry, so install them first — a separate step, because the SDK itself stays dependency-free:
npm install @opentelemetry/api @opentelemetry/sdk-trace-node @opentelemetry/instrumentation
npm install @traceloop/instrumentation-openai   # and/or -anthropic
import { autoInstrument } from 'aight-sdk'

await autoInstrument() // call once, before the provider client is constructed
Path B — no supported client library, or you want explicit control. Wrap each call site with tracedLlmCall, passing the real model name and token counts from that call's actual response (never estimate them):
import { tracedLlmCall } from 'aight-sdk'

tracedLlmCall(model, resp.usage.input_tokens, resp.usage.output_tokens)
It returns nothing, and there is no price to print — the platform sets the price, not this SDK. The row carries the model and the token counts, and the server prices it from its own table at receive time. Do not add a price table to this project: a local one is a second table that drifts from the server's, and a model it knows and the server's doesn't stops being repriced.
Do not do both — instrumenting the same call on both paths counts it twice.
Latency is the caller's to measure, on both paths. This SDK records a call after it returns, not around it, so it has no duration of its own and must never invent one: time the call yourself and pass it as the sixth argument to tracedLlmCall, and let it stay 0 where you didn't. A row with no latency shows as "—" on the agent's Speed axis — that is the correct answer for a call nobody timed. The auto-instrumented path cannot measure it at all, so it never carries one.
4. Push the data home
Call this after a traced run completes (end of a request handler, a batch job, a shutdown hook — wherever makes sense for how this project runs):
import { COST_PROCESSOR, push } from 'aight-sdk'

await push(COST_PROCESSOR) // reads AIGHT_API_KEY from the environment
5. Verify before you push
Confirm attribution worked locally, with no network call, before wiring up push():
node -e "import('aight-sdk').then(({COST_PROCESSOR}) => console.log(COST_PROCESSOR.report()))"
You should see the project's own file/line, not a framework's internal dispatch code.
6. Optional: Business value
Only call reportValue if this project has a real, countable signal for what it earned — never invent or estimate a number:
import { reportValue, VALUE_BY_FILE, pushValue } from 'aight-sdk'

reportValue(49.0)
await pushValue(VALUE_BY_FILE)
For an agent the user runs but this project does not contain — Claude Code, Gemini, Codex, traced by aight's collectors — earnings go in by agent id, because there is no calling file of theirs to resolve. pushValue takes the map, so pass one:
await pushValue({ 'claude-code': 49.0 })  // the agent id, not a source file
It shows a reported figure in the Workspace like any other agent. It never shows a matched one — the matching engine excludes external rows from its time-window fallback — so do not try to give it one through pushEvent.
Use this for value you can attribute to a file and want summed across a run — it lands in the agent's earned_usd. For a discrete event with its own id, value and timestamp (a checkout, a signup) that you want matched to the run that produced it, use pushEvent in step 7 instead: that reports into its own field, business_value_by_currency, not into earned_usd. Pick whichever shape you actually have; don't wire both for the same revenue just to be safe.
7. Optional: Business events
Report a business KPI/event so the Workspace can tie spend to what it earned. Again: only if this project has a real signal, never an invented number.
import { pushEvent } from 'aight-sdk'

await pushEvent('checkout.completed', 49.0)
Pass a trace id to both the traced call and the event when you have one (an agent run id, a request id, a job id) — that is what makes the pairing an EXPLICIT match instead of a time-window guess:
import { randomUUID } from 'node:crypto' // Node 18 has no global crypto

const runId = randomUUID()
tracedLlmCall(model, resp.usage.input_tokens, resp.usage.output_tokens, 0, 0, latencyMs, runId)
await pushEvent('checkout.completed', 49.0, { traceId: runId })
pushEvent returns the server's response, { ingested, results }, where each result carries the match_type it resolved to (EXPLICIT / IMPLICIT / UNMATCHED) — worth logging when you are wiring this up, and not something to report as a result on its own.
Guardrails
Never fabricate a metric aight doesn't actually capture.
autoInstrument() is a real patch, but it only covers providers with an OpenTelemetry instrumentor installed and it needs those packages added to the project — say that plainly when you offer it, rather than implying it is built in. Don't invent labels, TraceContext blocks, or multi-frame call chains; those exist only in the Python SDK today.
AIGHT_API_KEY is a secret: env var or secrets manager, never committed.
