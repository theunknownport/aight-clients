# AIght SDKs

Client libraries that instrument your agent's LLM calls, attribute the cost
to the exact source line that issued them, and push that data to the
[AIght Workspace](https://aight.studio) at aight.studio (the Integrate tab
mints the API key). The Python install also carries four collectors and a
proxy, which bring in the same spend from agents you run but did not write —
see **Collectors, and the proxy** below.

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
  keeps the earnings for a retry. Python and Node take the filepath → value map
  as an argument, so they can report earnings against any agent by name
  (`pushValue({"claude-code": 49.0})`); Go and Java push the accumulator
  `reportValue` filled.
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
| Python | [`python/`](python/) | `pip install aight` — also installs the `aight-collect*` and `aight-proxy` commands |
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

## Collectors, and the proxy

`pip install aight` installs more than the library. It also puts five commands
on your path that report spend from agents you run but did not write:

| Command | What it does |
|---|---|
| `aight-collect` | Collects from Claude Code's transcripts (`~/.claude/projects`) |
| `aight-collect-codex` | Codex CLI sessions (`~/.codex/sessions`, `CODEX_HOME` respected) |
| `aight-collect-gemini` | Gemini CLI sessions (reads `~/.gemini/tmp`) |
| `aight-collect-aider` | Aider's analytics log — **you must pass the path**; see below |
| `aight-proxy` | Proxies any agent's API traffic and reports what it spends |

**Why these exist beside the SDKs.** The SDKs instrument *your* code: they
walk the call stack to attribute spend to the line that issued the call. An
agent you run but did not write — Claude Code, Codex — has no line of yours on
the stack, so there is nothing to instrument. AIght reads the agent's own
on-disk record instead, and those rows arrive as `kind: "external"`: frame 0
is the agent id and frame 1 is the step it took (the tool and the file),
because there is no source line to point at.

**Earnings work the same way, and there they are reported rather than
inferred.** An external agent's value goes in against its agent id — the same
`POST /api/ingest/value` the SDKs use, with `{"filepath": "claude-code",
"value_usd": 49.0}` — and the Workspace shows it on that agent exactly as it
shows an internal agent's reported value: the earned total, the net, the ratio.
What an external agent never gets is a *matched* figure. The matching engine's
time-window fallback excludes external rows outright, because a collector pushes
no timestamp and an external row would always be the newest thing in the window
and win the guess by default. Reported, yes; guessed, no — the guess is the one
number this product does not put on an agent nobody wrote.

**Aider cannot backfill, and that is the one asymmetry.** Every other
collector reads a record its agent kept by default. Aider writes a per-call
log only if it was started with `--analytics-log FILE` or `AIDER_ANALYTICS_LOG`
was set, and it has no default path at all — a session that already ran
without that flag recorded nothing, and nothing recovers it.
`aight-collect-aider` says so when it has nothing to read, rather than
reporting an empty result as success.

The four collectors share their flags: a per-file resume marker under
`~/.aight/` (one file per collector — `claude_code_collect.json`,
`codex_collect.json`, `gemini_collect.json`, `aider_collect.json`) so a plain
re-run sends only what is new, plus `--dry-run`, `--since <ISO date|git rev>`,
`--all-time`, `--root`, and `--watch [--interval SECONDS]`. `--all-projects`
widens past the default root where the default is not already everything.

`--watch` re-scans on an interval — 30 seconds unless `--interval` says
otherwise — until you interrupt it, so spend is reported while the agent is
still running rather than whenever someone remembers to run the command. It
refuses `--since` and `--all-time`, deliberately: those name a scope to
re-send, and the ingest API *adds* what it receives, so a loop would re-send
that scope every interval and double the figures permanently.

`--all-time` is the other way to double them, which is why it is worth saying
twice. A plain run resumes from the marker; `--all-time` does not, and
re-pushes the history. Because the API adds on conflict and the platform
freezes cost at receive time, running it twice over the same transcripts
doubles the recorded spend with no repair path.

**The proxy is the fallback for what no parser covers**, including agents
whose records cannot be read at all: Cursor keeps no per-call token counts on
disk, and Amp emits no model name anywhere in its stream. Rather than a fourth
parser, it stands between the agent and the API:

```bash
aight-proxy                       # listens on 127.0.0.1:8787
export OPENAI_BASE_URL=http://127.0.0.1:8787/openai
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787/anthropic
```

The path prefix picks the upstream — both APIs live under `/v1/`, so the
prefix is what separates them. Everything after it is forwarded unchanged,
including the client's own `Authorization` header; the proxy holds no
credentials of its own. It measures real latency, which the SDKs cannot,
because they record after the call and would only be timing the recording.
Streaming responses are forwarded event by event rather than buffered, so a
streaming agent stays streaming. The one request it alters is a streaming
OpenAI call, where it sets `stream_options.include_usage` — without it the
stream carries no token counts at all, and most agents do not ask for it.

**Each parser reconciles a different provider convention**, and this is the
part to get right if you write your own. Claude Code's cache fields are
*additive* to `input_tokens`; Codex's `cached_input_tokens` is a *subset* of
`input_tokens` and is subtracted; the proxy subtracts OpenAI's `cached_tokens`
for that same reason and subtracts nothing for Anthropic, which already
reports the uncached prompt. Every row the wire protocol below describes wants
the Anthropic shape, so a converter has to know which convention its source
used.

One path is stated with less confidence than the rest: the Gemini collector
reads `~/.gemini/tmp`, and that root is a named constant in the code rather
than a path confirmed against a running Gemini. A wrong root is not a wrong
number — the run says it found nothing to collect, and `--root` or
`--all-projects` is the way past it.

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
    "kind": "external",              # optional, default "internal";
                                     # "external" for a collected agent
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

`kind` is optional and defaults to `"internal"`; send `"external"` for an
agent you run but did not write, where frame 0 is the agent id rather than a
source file — the shape the collectors above produce. It is the one field on a
row that is not last-write-wins: the first row that establishes an agent fixes
its kind, and a later push that disagrees is rejected with a 400, with the
whole batch rolled back and nothing from it stored. A row that omits `kind`
cannot reset an agent that is already external; it inherits what is stored.

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
  { "filepath": "/app/agent.py", "value_usd": 42.50 },
  { "filepath": "claude-code",   "value_usd": 49.00 }  # an external agent's id
]
```

`filepath` is whatever the agent is called: a source file for an agent you
wrote, the agent id a collector roots its rows at for one you only run. Every
row here is a *report* — a number someone stated — and a report is shown for
either kind of agent. What no row here can be is a *match*: this endpoint takes
no `trace_id`, and the event endpoint that does will not hand an event to an
external agent through its time-window fallback. See **Collectors** above.

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
