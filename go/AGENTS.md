aight — instructions for an AI coding agent
You are integrating aight into a Go project. aight attributes LLM spend to the exact source line that issued it, and pushes it to the hosted AIght Workspace at aight.studio. This file is your complete instruction set for the Go SDK — it is a smaller surface than the Python SDK (no custom labels, no multi-frame call chains), so don't guess at capabilities that aren't here.
1. Install
The module is public, so install it straight from the repository — no credentials, no GOPRIVATE:
go get github.com/theunknownport/aight-clients/go
The module lives in a subdirectory of the repo, so its release tags carry that directory as a prefix: the first tagged release is go/v0.1.0, not v0.1.0. A tag without the prefix leaves the module unreachable — go get reports no matching versions even though the tag exists.
To develop against an unreleased change, a `replace` directive pointing github.com/theunknownport/aight-clients/go at a local checkout of the repo's go/ directory still works, followed by `go mod tidy`.
2. Get an API key
Tell the user to mint one from the project's settings page in the AIght Workspace — the project selector at the top left, then Project settings — and set it as an environment variable, never hardcoded in source:
export AIGHT_API_KEY="aight_..."
3. Instrument the LLM calls — pick ONE path
Path A — wrapping the provider's HTTP client. Go cannot patch a package at runtime, so this is NOT automatic the way the Python and Node SDKs are: it is one line of wrapping you write at the client seam. Do not describe it to the user as auto-instrumentation.
aight.AutoInstrument(aight.ProviderOpenAI) // or aight.ProviderAnthropic
client := openai.NewClient(option.WithHTTPClient(
	aight.TracedHTTPClient(aight.ProviderOpenAI),
))
AutoInstrument patches nothing by itself — it only declares which providers TracedHTTPClient should read usage from, and returns the names it enabled. Skip the option.WithHTTPClient call and no call is traced, silently. It sees only complete, non-streaming responses; streamed calls must use Path B.
Path B — wrap every call site manually with TracedLLMCall, passing the real model name and token counts from that call's actual response (never estimate them):
import "github.com/theunknownport/aight-clients/go"

aight.TracedLLMCall(model, resp.Usage.InputTokens, resp.Usage.OutputTokens)
TracedLLMCall returns nothing, and there is no cost to read back: the model name and the token counts ARE the record, and the platform prices the row from its own table at receive. Never compute or pass a cost — there is no field for one, by design.
Do not do both for the same call — instrumenting it twice counts it twice.
Latency is the caller's to measure, on both paths. This SDK records a call after it returns, not around it, so it has no duration of its own and must never invent one: time the call yourself and set CallInfo.LatencyMs, and leave it zero where you didn't. A row with no latency shows as "—" on the agent's Speed axis — that is the correct answer for a call nobody timed. The wrapped path cannot measure it at all, so it never carries one.
4. Push the data home
Call this after a traced run completes (end of a request handler, a batch job, a shutdown hook — wherever makes sense for how this project runs):
err := aight.Push(aight.CostProcessor, "", "") // empty strings mean "read AIGHT_API_KEY / use the default ingest URL"
5. Verify before you push
Confirm attribution worked locally, with no network call, before wiring up Push:
fmt.Println(aight.CostProcessor.Report())
You should see the project's own file/line, not a framework's internal dispatch code.
6. Optional: Business value
Only call ReportValue if this project has a real, countable signal for what it earned — never invent or estimate a number:
aight.ReportValue(49.0)
err := aight.PushValue("", "")
7. Optional: Business events
PushEvent reports a business KPI (a checkout, a signup, a ticket closed) so the Workspace can put it next to the spend that produced it. Only call it when this project has a real event with a real name — never invent one:
_, err := aight.PushEvent("checkout.completed", 49.0, aight.EventOptions{})
event_id, currency (USD) and timestamp (now) default when left zero; the return value is the endpoint's JSON response, not the match result.
8. Optional: Tie a run together with a trace id
Set TraceID on the calls and events that belong to the same agent run — it gives the backend an explicit match instead of its time-window fallback:
aight.TracedLLMCallInfo(model, in, out, aight.CallInfo{TraceID: runID})
aight.PushEvent("checkout.completed", 49.0, aight.EventOptions{TraceID: runID})
Guardrails
Never fabricate a metric aight doesn't actually capture.
Never fabricate a metric aight doesn't actually capture — latency included. It is never estimated and never defaulted to a guess; it is zero (absent) until the caller measures it. A wrapped call carries none at all.
No custom labels or multi-frame call chains in this SDK — those exist only in the Python SDK today. Don't invent equivalents; if the user needs them, point them at the Python SDK.
AIGHT_API_KEY is a secret: env var or secrets manager, never committed.
