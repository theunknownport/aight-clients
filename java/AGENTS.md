aight — instructions for an AI coding agent
You are integrating aight into a Java project. aight attributes LLM spend to the exact source line that issued it, and pushes it to the hosted AIght Workspace at aight.studio. This file is your complete instruction set for the Java SDK — it is a smaller surface than the Python SDK (no real auto-instrumentation, no custom labels), so don't guess at capabilities that aren't here. Requires Java 17+.
1. Install
Not on Maven Central yet — publishing there needs a Sonatype account, a GPG signature on every artifact and a manual release review, none of which is set up. The repository is public, so build and install it locally from a clone, with no credentials:
git clone https://github.com/theunknownport/aight-clients
cd aight-clients/java
mvn install
Until it is published, that local install is what makes the dependency resolvable. Then depend on it from the target project's pom.xml:
<dependency>
  <groupId>studio.aight</groupId>
  <artifactId>aight-sdk</artifactId>
  <version>0.1.0</version>
</dependency>
2. Get an API key
Tell the user to mint one from the project's settings page in the AIght Workspace — the project selector at the top left, then Project settings — and set it as an environment variable, never hardcoded in source:
export AIGHT_API_KEY="aight_..."
3. Instrument the LLM calls — pick ONE path
Path A — an HTTP-level wrapper, for projects that call the provider over java.net.http.HttpClient. This is NOT auto-instrumentation and you must not describe it as such: Java's real one is the OpenTelemetry javaagent, a -javaagent: JVM flag that no library can set. What this is: you hand the provider SDK a client that records as the bytes go by.
import studio.aight.sdk.AutoInstrument;
import java.net.http.HttpClient;

AutoInstrument.enable(AutoInstrument.OPENAI);
HttpClient client = AutoInstrument.tracedHttpClient(AutoInstrument.OPENAI);
// then hand `client` to the provider SDK, e.g. OpenAIClient.builder().httpClient(client).build()
enable patches nothing by itself — it only declares which providers this process traces, and returns the names it enabled. Skip tracedHttpClient and nothing is traced, silently. It reads only complete, non-streaming responses collected as a String; streamed calls need Path B.
Path B — wrap every call site manually with Tracing.tracedLlmCall, passing the real model name and token counts from that call's actual response (never estimate them):
import studio.aight.sdk.Tracing;

Tracing.tracedLlmCall(model, response.usage().inputTokens(), response.usage().outputTokens());
tracedLlmCall returns void and the SDK ships no price table: it records the model and the token counts, and the backend prices them from its own table at receive time. Do not compute, estimate or report a cost yourself — there is nothing here that returns one, and the platform's number is the only one shown.
Do not do both for the same call — instrumenting it twice counts it twice.
Latency is the caller's to measure, on both paths. This SDK records a call after it returns, not around it, so it has no duration of its own and must never invent one: time the call yourself and pass it as CallInfo's third field, leaving it zero where you didn't measure it. A row with no latency shows as "—" on the agent's Speed axis — the correct answer for a call nobody timed. Path A cannot measure it at all, so those rows never carry one.
4. Push the data home
Call this after a traced run completes (end of a request handler, a batch job, a shutdown hook — wherever makes sense for how this project runs):
import studio.aight.sdk.Remote;

Remote.push(Tracing.COST_PROCESSOR, null, null); // null args mean "read AIGHT_API_KEY / use the default ingest URL"
5. Verify before you push
Confirm attribution worked locally, with no network call, before wiring up Remote.push:
System.out.println(Tracing.COST_PROCESSOR.report());
You should see the project's own file/line, not a framework's internal dispatch code. The report lists calls and tokens per line and carries no dollar figure — pricing happens on the platform, not here.
6. Optional: Business value
Only call Tracing.reportValue if this project has a real, countable signal for what it earned — never invent or estimate a number:
Tracing.reportValue(49.0);
Remote.pushValue(null, null);
7. Optional: Trace ids and business events
A trace id makes the match between a run and its outcome explicit instead of a time-window guess. Pass one to the traced call:
Tracing.tracedLlmCall(model, in, out, new Tracing.CallInfo(0, 0, 0, runId));
Tracing.CallInfo is (cacheReadTokens, cacheCreationTokens, latencyMs, traceId) — the three-argument constructor and CallInfo.NONE both mean "nothing measured but the tokens", so pass zeros for what you don't have.
Then report the outcome with the same id. pushEvent returns the ingest endpoint's parsed response ({"ingested": n, "results": [...]}, each result carrying its match_type and confidence_score) — read it if you want to know whether the match was EXPLICIT, IMPLICIT or UNMATCHED:
Map<String, Object> response = Remote.pushEvent("checkout.completed", 49.0,
        EventOptions.defaults().withTraceId(runId));
EventOptions.defaults() fills in a fresh event id, "USD", the current time and the environment's API key; withEventId / withTraceId / withCurrency / withTimestamp / withApiKey / withUrl override any of them. Only report an event whose value is real — never invent or estimate one.
8. Known limitation: the agent identity is a file basename
Tracing resolves the calling frame with StackWalker, which reports a file basename (App.java), never a path. Say this out loud to the user rather than discovering it in the Workspace later:
Two files with the same name in different directories count as one agent, because the server groups spend by that string.
Source snippets are read from that basename relative to the process working directory, so they only appear when the file sits in the directory the app was started from (running from a project root finds none of the files below it). A missing snippet shows as "not captured" in the Workspace; it is never an error and never breaks a traced call.
Do not try to work around this by passing a path yourself — the API has no such parameter, and a classloader-based source lookup breaks once the app is packaged.
Guardrails
Never fabricate a metric aight doesn't actually capture.
The platform prices; the SDK does not. It has no price table, tracedLlmCall returns void, and the pushed row carries model and tokens but no cost figure — so never state, print or return a cost as if the SDK produced it.
Real auto-instrumentation (a patch with no call-site changes), the TraceContext/labels block, and multi-frame call chains are still Python-only — Python and Node have the first, Python alone the other two. AutoInstrument here is wrapping you opt into, so never call it automatic, and never say a metric came from it that it cannot see (latency, trace id).
Never fabricate a metric aight doesn't actually capture — latency included: it is zero (absent) until the caller measures it, never estimated and never defaulted to a guess.
AIGHT_API_KEY is a secret: env var or secrets manager, never committed.
