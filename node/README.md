# aight (Node.js SDK)

Attributes LLM spend to the exact source line that issued it, and pushes it
to your [aight.studio](https://aight.studio) AIght Workspace.

Zero dependencies — uses Node's built-in `fetch` (Node >=18). The one
optional install is auto-instrumentation, which needs OpenTelemetry.

## Install

```bash
npm install aight-sdk
```

The package is published on npm as `aight-sdk`, and that is the specifier
every example below uses. It is not `aight` — that name belongs to an
unrelated package, so the suffix is not decoration.

## Deploy with your AI coding agent

Paste this into Claude Code, Cursor, or any other coding agent, in whatever
Node.js project you want traced:

> Run `npm install aight-sdk`, then read the `AGENTS.md` file in
> `node_modules/aight-sdk/AGENTS.md` and follow it to wire up cost
> attribution for this project.

## Use

```js
import { tracedLlmCall, COST_PROCESSOR, push } from 'aight-sdk'

tracedLlmCall('gpt-4o-mini', 180, 60)
// ...wrap every LLM call your agent makes the same way...

await push(COST_PROCESSOR) // reads AIGHT_API_KEY from the environment
```

`tracedLlmCall` returns nothing, and no price is computed here. It records
which model was called and how many tokens, and **the platform sets the
price**: the server prices each row from its own table at receive time. This
SDK ships no price table on purpose — a bundled copy is a second table, and a
second table drifts.

Get an API key from the Integrate tab of your AIght Workspace, then:

```bash
export AIGHT_API_KEY="aight_..."
```

`tracedLlmCall` walks the call stack past this SDK's own file and past
anything under `node_modules` (framework code) to attribute the span to
your code. The attributed line's surrounding source (3 lines either side) is
read once, cached, and pushed with the next `push()` — that's what fills the
code panel in Line review. Unreadable source is skipped silently.

### Latency is yours to measure

This SDK does not time the LLM call. It is called *after* `await` returns,
not around the request, so a duration taken here would measure the recording
rather than the model. Time the call yourself and pass it as the sixth
argument:

```js
const started = Date.now()
const response = await client.chat.completions.create({ ... })
tracedLlmCall(model, inputTokens, outputTokens, 0, 0, Date.now() - started)
```

Leave it at `0` and no latency is claimed — the Workspace shows "—" on that
agent's Speed axis rather than an impossibly fast number. `autoInstrument()`
cannot measure it either, so rows from that path never carry one.

## Auto-instrument (no manual wrapping)

Patch the provider client instead of every call site. This is a real patch —
the SDK registers OpenTelemetry instrumentation for the provider, and your
client calls are traced as they are:

```bash
npm install @opentelemetry/api @opentelemetry/sdk-trace-node @opentelemetry/instrumentation
npm install @traceloop/instrumentation-openai   # and/or -anthropic
```

```js
import { autoInstrument, COST_PROCESSOR, push } from 'aight-sdk'

await autoInstrument() // every known provider; pass ['openai'] to narrow
// ...construct the OpenAI/Anthropic client and call it exactly as before...

await push(COST_PROCESSOR)
```

It returns the provider names it enabled, and re-calling it enables nothing
twice. Those packages do not ship with this SDK — without them it warns and
returns an empty list rather than throwing, so it is safe to call
unconditionally. Call it before you construct the provider's client.

This path records no latency (see above) and no trace id: pass those by hand
with `tracedLlmCall` if you need them, and don't do both for the same call —
that double-counts it.

## Spent vs earned

Report the value your agent generates alongside its spend, to power the
AIght Workspace's spent-vs-earned view:

```js
import { reportValue, VALUE_BY_FILE, pushValue } from 'aight-sdk'

reportValue(49.0) // a closed deal, a resolved ticket — whatever you count
await pushValue(VALUE_BY_FILE)
```

This sums value per file into the agent's `earned_usd`. `pushValue` sends what
has accumulated and clears it once the server has acknowledged, so calling it
every flush sends each earning once, and a failed push keeps the total for the
retry instead of losing it.

If what you have is a discrete event rather than a running total, use
`pushEvent` below — it reports into its own field and carries the match to the
run that produced it.

## Business events

Report a business event — a checkout, a signup, a ticket closing — so the
Workspace can tie spend to what it earned:

```js
import { pushEvent } from 'aight-sdk'

await pushEvent('checkout.completed', 49.0)
```

`event_id` defaults to a fresh UUID and `timestamp` to now (Unix seconds);
pass `{ eventId, currency, timestamp }` to set them yourself.

If you have an id for the agent run that produced the event (a request id, a
job id), pass it to both calls as the trace id. That turns the spend-to-event
pairing into an **explicit** match; without it the server falls back to
matching by time window:

```js
import { randomUUID } from 'node:crypto'

const runId = randomUUID()
tracedLlmCall(model, resp.usage.input_tokens, resp.usage.output_tokens, 0, 0, latencyMs, runId)
await pushEvent('checkout.completed', 49.0, { traceId: runId })
```

`pushEvent` returns the server's response, `{ ingested, results }`, where each
result carries the `match_type` it resolved to (`EXPLICIT` / `IMPLICIT` /
`UNMATCHED`) and a confidence score.

## Local-only mode

Skip `push()` and call `COST_PROCESSOR.report()` instead for a plain-text
report per call site — calls and tokens, no network calls. No dollar figure:
this SDK doesn't price anything.

## Example

```bash
node examples/basic.js
```

## Test

```bash
npm test
```

Both this and the example run from a checkout of the repository: the
published package ships only `src` and `AGENTS.md`.
