# aight (Go SDK)

Attributes LLM spend to the exact source line that issued it, and pushes it
to your [aight.studio](https://aight.studio) AIght Workspace.

Standard library only — no third-party dependencies.

The price table is the platform's, not this package's. The SDK ships none
and computes no cost: it sends the model name and the four token counts, and
the AIght Workspace prices each row from its own, independently-refreshed
table when it receives it. So a price change on our side applies to your data
without an SDK upgrade, and a bundled table can never quietly disagree with
the number you are billed from.

## Install

```bash
go get github.com/theunknownport/aight-clients/go
```

The repository is public, so `go get` resolves the module with no credentials
and no `GOPRIVATE`. One detail decides whether it resolves at all: this module
sits in a subdirectory of the repo, so its release tags carry that directory
as a prefix — the first tagged release is `go/v0.1.0`, not `v0.1.0`. A tag
without the prefix is invisible to `go get`, which reports no matching
versions for the module even though the tag exists.

To develop against an unreleased change, a `replace` directive pointing at a
local checkout still works: `go mod edit -replace
github.com/theunknownport/aight-clients/go=/path/to/aight-clients/go`, then
`go mod tidy`.

## Deploy with your AI coding agent

Paste this into Claude Code, Cursor, or any other coding agent, in whatever
Go project you want traced:

> Add `github.com/theunknownport/aight-clients/go` to this project with
> `go get`, then read `go/AGENTS.md` in
> https://github.com/theunknownport/aight-clients and follow it to wire up cost
> attribution for this project.

## Use

```go
import "github.com/theunknownport/aight-clients/go"

aight.TracedLLMCall("gpt-4o-mini", 180, 60)
// ...wrap every LLM call your agent makes the same way...

aight.Push(aight.CostProcessor, "", "") // reads AIGHT_API_KEY from the environment
```

Get an API key from the Integrate tab of your AIght Workspace, then:

```bash
export AIGHT_API_KEY="aight_..."
```

`TracedLLMCall` walks the call stack past this package's own frames to
attribute the span to your code, and captures a few lines of source around the
line it lands on — that is what the Workspace's Line review panel renders. A
file it can't read (deployed without source, a path from another machine) just
shows as "not captured"; nothing fails.

### Latency is yours to measure

This SDK does not time the LLM call. It is called *after* your call returns,
not around it, so a duration taken here would measure the recording rather
than the model. Time the call yourself and put it on the `CallInfo`:

```go
started := time.Now()
resp, err := client.Chat.Completions.New(ctx, params)
elapsed := time.Since(started)

aight.TracedLLMCallInfo(model, resp.Usage.InputTokens, resp.Usage.OutputTokens,
	aight.CallInfo{LatencyMs: float64(elapsed.Milliseconds())})
```

Leave it zero and no latency is claimed — the Workspace shows "—" on that
agent's Speed axis rather than an impossibly fast number.

## Spent vs earned

Report the value your agent generates alongside its spend, to power the
AIght Workspace's spent-vs-earned view:

```go
aight.ReportValue(49.0) // a closed deal, a resolved ticket — whatever you count
aight.PushValue("", "")
```

`PushValue` sends what has accumulated and clears it once the server has
acknowledged — so calling it every flush sends each earning once, and a failed
push keeps the total for the retry instead of losing it.

Reporting value for an agent you did **not** write — Claude Code, Gemini, Codex,
which AIght collects rather than instruments — means posting it directly.
`PushValue` flushes the accumulator `ReportValue` filled, and that is keyed by
*your* calling file; an external agent has no file of yours to bind to, so it is
addressed by its agent id instead:

```go
body := strings.NewReader(`[{"filepath":"claude-code","value_usd":49.0}]`)
req, _ := http.NewRequest("POST", "https://api.aight.studio/api/ingest/value", body)
req.Header.Set("Authorization", "Bearer "+os.Getenv("AIGHT_API_KEY"))
req.Header.Set("Content-Type", "application/json")
http.DefaultClient.Do(req) // check the error and the status in real code
```

The Workspace shows that on `claude-code` exactly as it shows an agent of your
own: the earned total, the net, the ratio. What an external agent never gets is
a **matched** figure — the matching engine's time-window fallback excludes
external rows outright, because a collector pushes no timestamp and an external
row would always be the newest thing in the window and win the guess by default.
Reported, yes; guessed, no.

## Business events

Report a business KPI — a checkout, a signup, a ticket closed — so the
Workspace can attribute it to the agent run that produced it:

```go
_, err := aight.PushEvent("checkout.completed", 49.0, aight.EventOptions{})
```

`event_id` (a generated UUID), `currency` (USD) and `timestamp` (now) are
filled in when left zero. The call returns the ingest endpoint's JSON
response, which reports how the event matched — not the match itself.

## Tying a run together

Pass the same `TraceID` to the calls and events of one agent run for an
explicit spend-to-event match, instead of the backend's time-window fallback:

```go
runID := "run-42"
aight.TracedLLMCallInfo(model, resp.Usage.InputTokens, resp.Usage.OutputTokens,
	aight.CallInfo{TraceID: runID})
aight.PushEvent("checkout.completed", 49.0, aight.EventOptions{TraceID: runID})
```

## Auto-instrument: opt-in wrapping, not automation

Go cannot patch a package at runtime — there is no monkey-patching, and no way
to swap a provider's HTTP client out from under it. So this is **not** the
automatic instrumentation the Python and Node SDKs do. It is one line of
wrapping at the HTTP seam, which you have to write:

```go
import (
	"github.com/openai/openai-go"
	"github.com/openai/openai-go/option"
	"github.com/theunknownport/aight-clients/go"
)

aight.AutoInstrument(aight.ProviderOpenAI) // or aight.ProviderAnthropic
client := openai.NewClient(option.WithHTTPClient(
	aight.TracedHTTPClient(aight.ProviderOpenAI),
))
```

`AutoInstrument` returns the providers it enabled and drops unknown names with
a log line; it patches nothing by itself. Skip `option.WithHTTPClient` and
nothing is traced, silently — there is no error to catch, because nothing was
ever patched. A wrapper can only read usage out of a complete, non-streaming
response: streamed calls report usage incrementally or not at all and must go
through `TracedLLMCallInfo` instead. Wrapped rows carry no latency and no
trace id either, for the same reason as everything else here — nothing
measured them.

## Local-only mode

Skip `Push` and call `aight.CostProcessor.Report()` instead for a plain-text
calls-by-line report with no network calls — where each call happened, how
many, and how many tokens in and out. No dollar figure, because the prices
aren't here:

```
agent.go:42 (callLLM) [gpt-4o-mini] = 3 call(s), 540 in / 180 out
```

## Example

```bash
go run ./examples/basic
```

## Test

```bash
go test ./...
```
