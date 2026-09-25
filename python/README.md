# aight (Python SDK)

Attributes LLM spend to the exact source line that issued it, and pushes it
to your [aight.studio](https://aight.studio) AIght Workspace.

No price table ships in this SDK, by design. The SDK sends the model and the
token counts; **the platform prices them** — it recomputes `cost_usd` for
every row at receive time — so the dollar figures in the AIght Workspace are
always the platform's current prices, never a snapshot frozen at install
time. `traced_llm_call` therefore returns `None`, not a cost.

## Install

```bash
pip install aight
```

That is the whole install, and it also puts `aight-collect` on your path — the
Claude Code collector, which is a separate thing from the SDK and is described
under [External agents](#external-agents-claude-code) below.

## Deploy with your AI coding agent

Paste this into Claude Code, Cursor, or any other coding agent, in whatever
project you want traced:

> Run `pip install "aight[openai,anthropic]"`, then read the `AGENTS.md` file
> next to the installed `aight` package and follow it to wire up cost
> attribution for this project.

`AGENTS.md` ships inside the package (`python -c "import aight, pathlib; print(pathlib.Path(aight.__file__).parent / 'AGENTS.md')"`)
so the agent has the full integration recipe locally — no extra fetch, and
nothing it has to be able to reach over the network.

## Use

```python
from aight.tracing import traced_llm_call, COST_PROCESSOR
from aight.remote import push

traced_llm_call("gpt-4o-mini", input_tokens=180, output_tokens=60)
# ...wrap every LLM call your agent makes the same way...

push(COST_PROCESSOR.buckets)  # reads AIGHT_API_KEY from the environment
```

Get an API key from the Integrate tab of your AIght Workspace, then:

```bash
export AIGHT_API_KEY="aight_..."
```

`traced_llm_call` walks the call stack past known framework packages
(LangGraph, LangChain) to attribute the span to your code, not theirs.

### Latency is yours to measure

This SDK does not time the LLM call. It is called *after* your call returns,
not around it, so a duration taken here would measure the cost of recording
rather than the cost of the model. Time the call yourself and pass it:

```python
import time
from aight.tracing import traced_llm_call

started = time.perf_counter()
response = client.chat.completions.create(...)
elapsed_ms = (time.perf_counter() - started) * 1000

traced_llm_call(model, input_tokens, output_tokens, latency_ms=elapsed_ms)
```

Leave `latency_ms` out and no latency is claimed — the Workspace shows "—" on
that agent's Speed axis rather than an impossibly fast number. The
auto-instrumented path cannot measure it either, so rows it records carry
none: if you want Speed for a call, wrap that call by hand.

## Auto-instrument (no manual wrapping)

```bash
pip install "aight[openai]"   # or aight[anthropic]
```

```python
from aight.auto import auto_instrument
from aight.remote import push
from aight.tracing import COST_PROCESSOR

auto_instrument()
# ...call the OpenAI/Anthropic client exactly as you normally would...

push(COST_PROCESSOR.buckets)
```

`auto_instrument()` silently skips any provider whose instrumentor package
isn't installed, so it's safe to call unconditionally. It patches the provider
client, not the call, so the rows it records carry no latency — see above.

## Spent vs earned

Report the value your agent generates alongside its spend, to power the
AIght Workspace's spent-vs-earned view:

```python
from aight.tracing import report_value, VALUE_BY_FILE
from aight.remote import push_value

report_value(49.0)  # a closed deal, a resolved ticket — whatever you count
push_value(VALUE_BY_FILE)
```

`push_value` sends what has accumulated and clears it once the server has
acknowledged — so calling it every flush sends each earning once, and a failed
push keeps the total for the retry instead of losing it.

## Local-only mode

Skip `push()` and call `COST_PROCESSOR.report()` instead — a plain-text
calls-and-tokens-by-line report with no network calls, useful for local
debugging or fully offline use. It has no dollar figure: pricing is the
platform's, so spend only appears in the AIght Workspace once you push.

```
examples/snippet_check.py:19 (plan_step) [gpt-4o-mini] = 3 call(s), 540 in / 180 out
```

## External agents (Claude Code)

Everything above is for an agent *you wrote*: the SDK walks your call stack to
find the line that issued the call. An agent you run but did not write has no
line of yours on the stack, so there is nothing to instrument and no library to
import. AIght traces those by reading the agent's own transcripts instead.

`pip install aight` also installs `aight-collect`:

```bash
export AIGHT_API_KEY="aight_..."

aight-collect --dry-run   # print the rows instead of sending them
aight-collect             # push them
```

It reads Claude Code's transcripts under `~/.claude/projects` and pushes one
row per step the agent took, under an external agent called `claude-code`. Cost
lands on the step — the tool it called and the file it touched — rather than on
a source line, because no source line of yours is involved.

Leave it running and the spend appears as you work, rather than whenever you
next remember to run it:

```bash
aight-collect --watch              # re-scan every 30s, until Ctrl-C
aight-collect --watch --interval 10
```

`--watch` cannot be combined with `--since` or `--all-time`. Those name a scope
to re-send, and a loop would re-send it every interval — which the API *adds* to
what it already holds, so the second tick would double the first. Run them once
to catch up, then start the watcher.

Three things are worth knowing before the first run:

- **A plain run resumes.** It records the newest call it pushed for each
  transcript file in `~/.aight/`, so a second run sends only what is new.
  Running it repeatedly is the intended use.
- **`--all-time` does not resume.** It re-sends the whole history, and the API
  *adds* what it receives to what it already holds — so pushing the same calls
  twice doubles the recorded spend permanently. Cost is frozen at receive time
  and there is no repair path. Reach for it only on a transcript that has never
  been pushed.
- **The key alone decides the destination.** A run prints the project its rows
  landed in and stops rather than resuming when that differs from the project
  its earlier pushes went to. A key copied out of another project's Integrate
  tab otherwise reads exactly like success, and fills a workspace nobody is
  looking at.

Without installing anything:

```bash
uvx --from aight aight-collect --dry-run
```

## Any other agent (the proxy)

The collector above reads Claude Code's own transcripts, which means one parser
per agent — and several agents keep no readable per-call record at all. Cursor
keeps no token counts on disk; Amp emits no model name anywhere. So for those
there is nothing to parse, and the answer is not another parser.

`aight-proxy` stands between the agent and the API instead. It sees the request
and the response, so it does not need to know what the agent is:

```bash
aight-proxy                                  # 127.0.0.1:8787
export OPENAI_BASE_URL=http://127.0.0.1:8787/openai
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787/anthropic
```

The path prefix picks the upstream, because both APIs live under `/v1/` and
`/v1/messages` would otherwise be ambiguous. Everything after the prefix is
forwarded unchanged, including your `Authorization` header — the proxy holds no
credentials of its own and stores nothing from the request or the response.

It works for anything that lets you point its base URL somewhere else, which is
most agents. Name them so several through one proxy stay distinct:

```bash
AIGHT_PROXY_AGENT=codex aight-proxy
```

Four things worth knowing:

- **It measures latency**, which the SDKs cannot. They record after the call
  returns, so a duration taken there would time the recording; the proxy sits
  around the call, so `latency_ms` is real.
- **OpenAI's cached tokens are subtracted.** `prompt_tokens` counts the cached
  prefix and AIght's `input_tokens` does not, so sending the inclusive number
  would bill those tokens twice — permanently, since cost is frozen at receive
  time. Anthropic reports the split itself and nothing is subtracted there.
- **Streaming OpenAI requests get `stream_options.include_usage` added.** That
  is a change to your request, not a passive read: without it a streaming call
  carries no usage at all, and most calls stream. Anthropic needs no equivalent.
- **Nothing is priced here.** Rows go up with `cost_usd` 0.0 and the platform
  prices them from its own table at receive time.

## Example

`examples/langgraph_spike.py` traces a 3-node LangGraph agent and prints the
attribution report (also pushes it, if `AIGHT_API_KEY` is set). It ships in the
repo rather than in the wheel, so run it from a clone:

```bash
cd /path/to/aight-clients/python
pip install ".[examples]"     # langgraph; ".[dev,examples]" adds pytest and ruff
python -m examples.langgraph_spike
```
