# AIght SDKs

Client libraries that instrument your agent's LLM calls, attribute the cost
to the exact source line that issued them, and push that data to the
[AIght Workspace](https://aight.studio) at aight.studio (the Integrate tab
mints the API key).

Every SDK implements the same small surface:

- **`tracedLlmCall(model, inputTokens, outputTokens)`** (naming varies by
  language convention) — records the call, attributed to your source line,
  with its model and token counts. It returns nothing: pricing is the
  platform's, and the dollar figure appears in the Workspace once the row
  arrives.
- **`push(...)`** — sends everything recorded so far to
  `POST /api/ingest/spans`, authenticated with your `AIGHT_API_KEY`. Clears
  the buckets once the server has acknowledged, so a later push sends only
  what happened since — the endpoint *adds* each row to what it holds, so
  re-sending the same running totals would count every call twice.
- **`reportValue(valueUsd)`** — records revenue/value this agent earned (a
  closed deal, a resolved ticket — whatever your business counts),
  attributed to the same calling file `tracedLlmCall` uses as the "agent."
  Powers the AIght Workspace's spent-vs-earned view.
- **`pushValue(...)`** — sends everything recorded by `reportValue` to
  `POST /api/ingest/value`, same auth as `push`, and clears its accumulator on
  the same terms: only once the server has acknowledged, so a failed push
  keeps the earnings for a retry.
- **`report()`** — a local, plain-text call-by-call summary, no network call.
  Useful for fully offline, local-only use without ever pushing anywhere.

Each is dependency-free (or as close as the language allows): stdlib HTTP
client and stdlib stack/frame introspection to find the calling line.

**No SDK times the LLM call.** Every one of them records a call *after* it
returns, not around it, so a duration taken here would be the recording's
rather than the model's. Measure the call where you make it and pass the
milliseconds; leave it out and the row claims no latency, and the Workspace
shows "—" on that agent's Speed axis instead of a number nobody took.

**No SDK ships a price table.** There is exactly one, on the server, and it
prices every row at receive time from the model name and token counts the SDK
sends. A price change lands for every customer immediately, with no SDK
upgrade and no version of a rate to drift out of date in four languages at
once — which is exactly what happened when these were bundled: the copies were
silently pricing against models the platform no longer carried. It also means
the SDK never needs to know what a model costs, so an unreleased or
self-hosted one traces correctly the moment the platform learns its rates.

Python and Node.js are on their registries; Go resolves straight from this
repo; Java is built from a clone. Each README below has the exact commands.

| Language | Path | Install |
|---|---|---|
| Python | [`python/`](python/) | `pip install aight` — also installs `aight-collect` |
| Node.js | [`node/`](node/) | `npm install aight-sdk` |
| Go | [`go/`](go/) | `go get github.com/theunknownport/aight-clients/go` |
| Java | [`java/`](java/) | `mvn install` from a clone — not yet on Maven Central |

The npm name is `aight-sdk` rather than `aight`: the shorter name belongs to an
unrelated package, so `aight-sdk` is what the Node examples import.

Each SDK is a fully independent package with its own build/test toolchain —
none of them depend on each other or on any server-side code. Run their tests
from inside each directory (`pytest`, `npm test`, `go test ./...`, `mvn test`).

All four carry the full surface: `tracedLlmCall` (with cache-token counts
and a trace id), `push`, `reportValue`, `pushValue`, `report`, and the
business-event `push_event`/`pushEvent`/`PushEvent` that feeds cost-vs-value
matching. Spans include a `trace_id` when you pass one, and each SDK captures
a few lines of source around the attributed line and sends them as
`snippet`/`snippet_start` for the drilldown.

Each SDK does as much auto-instrumentation as its language allows, and says
which of the two it is doing:

- **Python and Node patch the provider client for real**, via OpenTelemetry
  instrumentation — `auto_instrument()` / `autoInstrument()`. Python's tracing
  is built on OTel already; Node's is not, so its auto path is an optional
  extra install that degrades to an empty list, with a message, when those
  packages are absent. Neither can time the call, so rows they record carry no
  latency.
- **Go and Java cannot patch anything at runtime.** Go has no monkey-patching
  at all, and Java's only true auto-instrumentation is the OpenTelemetry
  `-javaagent` JVM flag, which no library method can set. Both ship **opt-in
  wrapping** instead: the SDK hands you an HTTP client that records as the
  bytes go by, and you give that client to the provider SDK. Their READMEs say
  plainly that this is wrapping, not automation — it is not the same thing,
  and it should not be described as if it were. Both stay dependency-free,
  because the seam is HTTP.

One thing remains Python-only, deliberately rather than pending:

- **`TraceContext` and `labels`** — Python's ambient context manager. The
  others take the trace id as an explicit argument on the call and on
  `pushEvent`, which is the same capability without a contextvar to reason
  about. Note the id must then be threaded to the event yourself.

One known limitation in Java: its caller frame comes from `StackWalker`,
which reports a **basename**, so two files with the same name in different
directories collapse into one agent. There is no fix available to the SDK —
see `java/README.md`.

Every SDK ships an `AGENTS.md` next to its package root with the exact,
language-specific integration recipe for an AI coding agent — each
language's README has a "Deploy with your AI coding agent" section with the
one-line prompt to hand it.

## Wire protocol

All four push the same JSON to the same endpoints, so a new language SDK just
needs to reproduce this:

```
POST /api/ingest/spans
Authorization: Bearer <AIGHT_API_KEY>
Content-Type: application/json
X-Aight-Sdk-Language: python        # optional, feeds the project's SDK badge
X-Aight-Sdk-Version: 0.1.0          # optional, ditto

[
  {
    "chain": [
      { "filepath": "/app/agent.py",  "lineno": 12, "function": "run" },
      { "filepath": "/app/steps.py",  "lineno": 42, "function": "call_llm" }
    ],
    "calls": 3,
    "model": "gpt-4o-mini",          # optional
    "input_tokens": 540,             # optional, the *uncached* prompt
    "output_tokens": 180,            # optional
    "cache_read_tokens": 4000,       # optional, priced at the cache-read rate
    "cache_creation_tokens": 800,    # optional, at the cache-write rate
    "latency_ms": 820.5,             # optional
    "trace_id": "..."                # optional, for explicit event matching
  }
]
```

There is deliberately no `cost_usd` — no SDK computes one. A row that names
its model gets priced from the platform's table; one that doesn't is stored
at 0. (A hand-rolled client *may* still send `cost_usd`; it is used only
where the platform has no rate for the model, and it is checked against the
platform's own number otherwise — see `store/quality.py`.)

`input_tokens` must be the **uncached** prompt — the Anthropic convention.
OpenAI-shaped usage (`prompt_tokens` inclusive of `cached_tokens`) has to be
converted before it gets here: an inclusive count bills the cached tokens
twice, once at the full input rate and again at the cache rate, and because
the platform freezes cost at receive that overcount is permanent.

`chain` is **required** and is a non-empty list of `{filepath, lineno,
function}` frames, outermost first. The **last** frame is the call site the
spend is attributed to; the **first** frame's filepath is the "agent" the
fleet view groups by — so a one-frame chain (what the Node, Go and Java SDKs
send today) means "the calling line is the agent". Frames may also carry
`snippet` / `snippet_start` to show source context in the drilldown.

When `model` and the token counts are present the server prices the row from
its own table. Rows are stored per **(call site, model)**, so send one row per
model a line used rather than pre-merging them — merged rows get priced as if
every call were the one model named on the row, which is why the model is part
of the row key and not just a field on it.

```
POST /api/ingest/value
Authorization: Bearer <AIGHT_API_KEY>
Content-Type: application/json

[
  { "filepath": "/app/agent.py", "value_usd": 42.50 }
]
```

```
POST /api/ingest/events
Authorization: Bearer <AIGHT_API_KEY>
Content-Type: application/json

{
  "event_id": "evt_123",              # required, unique per event
  "event_name": "checkout.completed", # required
  "timestamp": 1758307200.0,          # required, Unix seconds
  "value": 49.0,                      # optional, default 0
  "currency": "USD",                  # optional, default USD
  "trace_id": "..."                   # optional — an explicit match; without
}                                     # it the server guesses by time window
```

A single object or a list of them. Responds `{"ingested": n, "results":
[...]}`, where each result carries the `match_type`
(`EXPLICIT`/`IMPLICIT`/`UNMATCHED`) and `confidence_score` it resolved to.
