# aight (Java SDK)

Attributes LLM spend to the exact source line that issued it, and pushes it
to your [aight.studio](https://aight.studio) AIght Workspace.

Zero third-party dependencies — `java.net.http.HttpClient` (built in since
Java 11) for the push, `StackWalker` (built in since Java 9) for caller
attribution. Requires Java 17+ (uses records).

## Install

Not on Maven Central yet — publishing there needs a Sonatype account, a GPG
signature on every artifact and a manual release review, none of which is set
up. The repository is public, so build and install it into your local
repository from a clone, with no credentials:

```bash
git clone https://github.com/theunknownport/aight-clients
cd aight-clients/java
mvn install
```

Until it is published, `mvn install` from that clone is what makes the
dependency resolvable. From your project's `pom.xml`:

```xml
<dependency>
  <groupId>studio.aight</groupId>
  <artifactId>aight-sdk</artifactId>
  <version>0.1.0</version>
</dependency>
```

## Deploy with your AI coding agent

Paste this into Claude Code, Cursor, or any other coding agent, in whatever
Java project you want traced:

> Clone `https://github.com/theunknownport/aight-clients`, run `mvn install`
> inside `java`, add the `studio.aight:aight-sdk:0.1.0` dependency to this
> project's `pom.xml`, then read `java/AGENTS.md` in the cloned repo and
> follow it to wire up cost attribution.

## Use

```java
import studio.aight.sdk.Tracing;
import studio.aight.sdk.Remote;

Tracing.tracedLlmCall("gpt-4o-mini", 180, 60);
// ...wrap every LLM call your agent makes the same way...

Remote.push(Tracing.COST_PROCESSOR, null, null); // reads AIGHT_API_KEY from the environment
```

Get an API key from the Integrate tab of your AIght Workspace, then:

```bash
export AIGHT_API_KEY="aight_..."
```

`tracedLlmCall` walks the call stack past this SDK's own frames to
attribute the span to your code, and captures a few lines of source around
each attributed line — that is what fills the code panel next to it in the
Workspace. Capture is best-effort and never raises; see
[the filepath limitation](#javas-filepath-limitation) for when it comes up
empty.

### The platform prices; the SDK does not

This SDK ships **no price table** and prices nothing. It records the model and
the four token counts, and the backend computes the spend from its own,
independently-refreshed table at receive time — so a push never claims a cost
the platform will have to trust or correct. `tracedLlmCall` returns nothing for
the same reason: there is no number here to return. Read spend in the AIght
Workspace, which is the one place it is computed.

## Spent vs earned

Report the value your agent generates alongside its spend, to power the
AIght Workspace's spent-vs-earned view:

```java
import studio.aight.sdk.Tracing;
import studio.aight.sdk.Remote;

Tracing.reportValue(49.0); // a closed deal, a resolved ticket — whatever you count
Remote.pushValue(null, null);
```

`pushValue` sends what has accumulated and clears it once the server has
acknowledged — so calling it every flush sends each earning once, and a failed
push keeps the total for the retry instead of losing it.

## Trace ids and business events

Give a run an explicit id, then report the outcome it produced with that same
id — the backend joins the two directly instead of guessing by time window:

```java
import studio.aight.sdk.EventOptions;
import studio.aight.sdk.Remote;
import studio.aight.sdk.Tracing;

String runId = "run-2026-09-23-1";
Tracing.tracedLlmCall(model, inputTokens, outputTokens, new Tracing.CallInfo(0, 0, 0, runId));

Map<String, Object> response = Remote.pushEvent(
        "checkout.completed", 49.0, EventOptions.defaults().withTraceId(runId));
```

`EventOptions.defaults()` fills in a fresh event id, `"USD"`, the current time
and the environment's API key; `withX(...)` overrides any of them. `response`
is the ingest endpoint's own report — `{"ingested": n, "results": [...]}`,
where each result carries the `match_type` it resolved to
(`EXPLICIT` / `IMPLICIT` / `UNMATCHED`) and a `confidence_score`. Without a
trace id the event still lands, matched by a time-window guess.

## Java's filepath limitation

`Tracing` attributes a call with `StackWalker`, which reports the caller's
file **basename** (`App.java`) and never a path. Two consequences worth
knowing before you read the Workspace:

- `App.java` in two different directories collapses into a single agent,
  because the server groups spend by that same string.
- Source snippets are read from that basename relative to the process working
  directory, so they only appear when the file sits in the directory the
  process was started from — running from a project root finds none of the
  files below it, and a packaged jar finds none at all. A missing snippet
  degrades to the Workspace's "not captured" state — it is never an error and
  never breaks a traced call.

Neither has a fix available to this SDK: `StackWalker` has no path to give,
and a classloader source lookup stops working once the app is packaged. The
Python SDK's stack walk does yield a full path; this one sends what it has.

### Latency is yours to measure

This SDK does not time the LLM call. It is called *after* your call returns,
not around it, so a duration taken here would measure the recording rather
than the model. Time the call yourself and put it in the `CallInfo`
(`cacheReadTokens`, `cacheCreationTokens`, `latencyMs`, `traceId`):

```java
long started = System.nanoTime();
var response = client.chat().completions().create(params);
double elapsedMs = (System.nanoTime() - started) / 1e6;

Tracing.tracedLlmCall(model, inputTokens, outputTokens,
        new Tracing.CallInfo(0, 0, elapsedMs, runId));
```

Leave it at zero and no latency is claimed — the Workspace shows "—" on that
agent's Speed axis rather than an impossibly fast number. `AutoInstrument` is
the same: it watches bytes go by, so it cannot time anything and records none.

## Auto-instrument: opt-in wrapping, not automation

Java's only true auto-instrumentation is the OpenTelemetry **javaagent** — a
`-javaagent:` flag on the JVM command line, which patches provider classes as
they load. No library method can set it for you, so this SDK does not pretend
to: what it ships is the wrapping it can do with zero dependencies, at the
`HttpClient` seam.

```java
import studio.aight.sdk.AutoInstrument;

AutoInstrument.enable(AutoInstrument.OPENAI);
HttpClient client = AutoInstrument.tracedHttpClient(AutoInstrument.OPENAI);
// then hand `client` to the provider SDK:
OpenAIClient openai = OpenAIClient.builder().httpClient(client).build();
```

`enable` declares which providers this process intends to trace and returns
the ones it enabled this call; it patches nothing on its own. Skip
`tracedHttpClient` and nothing is traced, silently — there is no error to
catch, because nothing was ever patched. A wrapper can only read usage out of
a complete, non-streaming response whose body was collected as a `String`:
streamed calls report usage incrementally or not at all and must go through
`Tracing.tracedLlmCall` instead. If you want the real thing, that is the
javaagent plus an OpenTelemetry instrumentation library — not this.

## Local-only mode

Skip `Remote.push` and call `Tracing.COST_PROCESSOR.report()` instead for a
plain-text, no-network check that attribution landed on your line — calls and
tokens per line, with no dollar figure, because this SDK has no price table
to draw one from.

## Example

```bash
mvn -q package -DskipTests
javac -d examples/out -cp target/classes examples/src/main/java/BasicExample.java
java -cp "examples/out:target/classes" BasicExample
```

## Test

```bash
mvn test
```
