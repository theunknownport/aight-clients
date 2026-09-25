package studio.aight.sdk;

import java.io.IOException;
import java.net.Authenticator;
import java.net.CookieHandler;
import java.net.ProxySelector;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executor;

import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLParameters;

/**
 * Wraps a provider's HTTP client so its calls are traced without a
 * {@code tracedLlmCall} at every call site. Mirrors the other SDKs' autos —
 * and, like Go's, it is <em>opt-in wrapping</em> rather than true automatic
 * instrumentation.
 *
 * <p>Java's only real auto-instrumentation is the OpenTelemetry javaagent: a
 * {@code -javaagent:} flag on the JVM command line, which patches provider
 * classes as they load. No library method can set it for you. The
 * instrumentation libraries that ship without the agent
 * ({@code OpenAITelemetry.wrap(client)} and friends) are wrappers too, and they
 * need OTel, the provider's Java SDK and the instrumentation library on the
 * classpath — none of which this SDK, with zero third-party dependencies,
 * can assume.
 *
 * <p>So the wrapping this can do without any of that is at the HTTP seam:
 *
 * <pre>{@code
 * AutoInstrument.enable(AutoInstrument.OPENAI);
 * HttpClient client = AutoInstrument.tracedHttpClient(AutoInstrument.OPENAI);
 * // then hand `client` to the provider SDK:
 * OpenAIClient openai = OpenAIClient.builder().httpClient(client).build();
 * }</pre>
 *
 * <p>{@link #enable} on its own changes nothing: it declares which providers
 * this process intends to trace, and it is a separate call only so an app can
 * log what it enabled. Skip {@code tracedHttpClient} and nothing is traced,
 * silently — there is no error to catch, because nothing was ever patched.
 *
 * <p>A wrapper can only read usage out of a complete, non-streaming response
 * whose body was collected as a {@code String}. Streamed calls report usage
 * incrementally or not at all and are not traced; neither is a response read
 * through {@code BodyHandlers.ofInputStream()} or {@code ofFile()}, since its
 * bytes belong to the caller. Use {@code Tracing.tracedLlmCall} for those.
 * Auto-instrumented rows carry no latency and no trace id either: the transport
 * never learns the caller's run id, and this SDK does not invent a duration.
 */
public final class AutoInstrument {

    /** Provider names {@link #enable} and {@link #tracedHttpClient} understand. */
    public static final String OPENAI = "openai";
    public static final String ANTHROPIC = "anthropic";

    private static final List<String> KNOWN = List.of(OPENAI, ANTHROPIC);
    private static final Set<String> ENABLED = ConcurrentHashMap.newKeySet();

    // This SDK's own source files, by the basename StackWalker reports. The
    // walk below skips frames from them so a caller's line is what gets
    // recorded — a file list rather than the package, because the white-box
    // tests share the package and are callers, not SDK internals.
    private static final Set<String> OUR_FILES =
            Set.of("AutoInstrument.java", "EventOptions.java", "Remote.java", "Tracing.java");

    private AutoInstrument() {}

    /**
     * Declares each named provider enabled (default: every known one) and
     * returns the ones this call enabled. Unknown names, and names already
     * enabled, are skipped.
     *
     * <p>This is bookkeeping, not patching — see the class doc. The tracing
     * itself comes from {@link #tracedHttpClient}.
     */
    public static List<String> enable(String... providers) {
        String[] names = providers.length > 0 ? providers : KNOWN.toArray(new String[0]);
        List<String> enabled = new ArrayList<>();
        for (String name : names) {
            if (!KNOWN.contains(name)) {
                System.err.println("aight: unknown provider \"" + name + "\", skipping");
                continue;
            }
            if (ENABLED.add(name)) {
                enabled.add(name);
            }
        }
        return enabled;
    }

    /**
     * An {@link HttpClient} that records every LLM call made through it into
     * {@link Tracing#COST_PROCESSOR}, so {@link Remote#push} sends them along
     * with everything recorded by hand. Pass it to your provider SDK's client
     * builder — this is the half of auto-instrumentation the JVM will not do
     * for a library.
     *
     * <p>{@code provider} decides which usage shape is read: {@link #OPENAI}
     * reports a prompt total with the cached tokens inside it (subtracted back
     * out here), {@link #ANTHROPIC} reports the uncached prompt with the cache
     * counts beside it. Getting that the wrong way round bills the cached
     * tokens twice, permanently — the platform freezes cost at receive.
     */
    public static HttpClient tracedHttpClient(String provider) {
        return new TracingClient(provider, HttpClient.newHttpClient());
    }

    /**
     * The wrapping client. Every method but the two that carry a response body
     * is a straight delegate; this only watches the bytes go by, so the
     * provider SDK still builds and parses its own requests.
     */
    private static final class TracingClient extends HttpClient {

        private final String provider;
        private final HttpClient delegate;

        TracingClient(String provider, HttpClient delegate) {
            this.provider = provider;
            this.delegate = delegate;
        }

        @Override
        public <T> HttpResponse<T> send(HttpRequest request, HttpResponse.BodyHandler<T> handler)
                throws IOException, InterruptedException {
            HttpResponse<T> response = delegate.send(request, handler);
            record(response);
            return response;
        }

        @Override
        public <T> CompletableFuture<HttpResponse<T>> sendAsync(
                HttpRequest request, HttpResponse.BodyHandler<T> handler) {
            return delegate.sendAsync(request, handler).thenApply(response -> {
                record(response);
                return response;
            });
        }

        @Override
        public <T> CompletableFuture<HttpResponse<T>> sendAsync(
                HttpRequest request,
                HttpResponse.BodyHandler<T> handler,
                HttpResponse.PushPromiseHandler<T> pushPromiseHandler) {
            return delegate.sendAsync(request, handler, pushPromiseHandler).thenApply(response -> {
                record(response);
                return response;
            });
        }

        /**
         * Turns one response into a bucket. Anything it cannot read — an error
         * body, a streamed chunk, a body the caller asked for as a stream, a
         * provider that reported no usage — records nothing at all, which is
         * the correct answer: a row with no model or no tokens is one the
         * server cannot price, and a row it cannot price is noise.
         */
        private <T> void record(HttpResponse<T> response) {
            if (!(response.body() instanceof String text)) {
                return;
            }
            Object parsed;
            try {
                parsed = new Remote.Json(text).parse();
            } catch (RuntimeException e) {
                return; // not JSON, or not a document we can read — not our business
            }
            if (!(parsed instanceof Map<?, ?> body)) {
                return;
            }
            if (!(body.get("model") instanceof String model) || model.isEmpty()) {
                return;
            }
            if (!(body.get("usage") instanceof Map<?, ?> usage)) {
                return;
            }

            long input;
            long output;
            long cacheRead;
            long cacheCreation;
            if (ANTHROPIC.equals(provider)) {
                input = numberOf(usage.get("input_tokens"));
                output = numberOf(usage.get("output_tokens"));
                cacheRead = numberOf(usage.get("cache_read_input_tokens"));
                cacheCreation = numberOf(usage.get("cache_creation_input_tokens"));
            } else {
                cacheRead = usage.get("prompt_tokens_details") instanceof Map<?, ?> details
                        ? numberOf(details.get("cached_tokens"))
                        : 0;
                // OpenAI's prompt_tokens already contains the cached tokens.
                // Folding them in here would bill every cached token twice —
                // once at the full input rate and again at the cache rate — and
                // the platform freezes cost at receive, so the overcount would
                // be permanent. Clamped at zero in case the provider reports a
                // cache read larger than the prompt it is supposedly inside.
                input = Math.max(0, numberOf(usage.get("prompt_tokens")) - cacheRead);
                output = numberOf(usage.get("completion_tokens"));
                cacheCreation = 0;
            }
            if (input == 0 && output == 0 && cacheRead == 0 && cacheCreation == 0) {
                return;
            }

            Tracing.BucketKey site = resolveCallerFrame();
            Tracing.captureSnippet(site.filepath(), site.lineno());
            Tracing.COST_PROCESSOR.record(
                    new Tracing.BucketKey(site.filepath(), site.lineno(), site.function(), model),
                    (int) input,
                    (int) output,
                    // No latency: this SDK does not time a call it did not make,
                    // and a number nobody measured shows as "—" rather than as
                    // an impossibly fast call. No trace id either — the
                    // transport never learns the caller's run.
                    new Tracing.CallInfo(cacheRead, cacheCreation, 0, ""));
        }

        private static long numberOf(Object value) {
            return value instanceof Number number ? number.longValue() : 0;
        }

        /**
         * The caller's own line, walking past this SDK's frames and past the
         * JDK's — the provider SDK calls {@code httpClient.send} from its own
         * classes, so without the framework prefixes every auto-instrumented
         * call would attribute to {@code com.openai.*} rather than to the line
         * that issued it. The same idea as the Python SDK's known-framework
         * list and Node's {@code node_modules} walk.
         */
        private static Tracing.BucketKey resolveCallerFrame() {
            return StackWalker.getInstance().walk(frames -> frames
                    .filter(f -> !OUR_FILES.contains(f.getFileName()) && !isFramework(f.getClassName()))
                    .findFirst()
                    .map(f -> new Tracing.BucketKey(f.getFileName(), f.getLineNumber(), f.getMethodName(), ""))
                    .orElse(new Tracing.BucketKey("?", 0, "?", "")));
        }

        private static boolean isFramework(String className) {
            return className.startsWith("java.")
                    || className.startsWith("jdk.")
                    || className.startsWith("sun.")
                    || className.startsWith("com.openai.")
                    || className.startsWith("com.anthropic.");
        }

        @Override
        public Optional<CookieHandler> cookieHandler() {
            return delegate.cookieHandler();
        }

        @Override
        public Optional<Duration> connectTimeout() {
            return delegate.connectTimeout();
        }

        @Override
        public Redirect followRedirects() {
            return delegate.followRedirects();
        }

        @Override
        public Optional<ProxySelector> proxy() {
            return delegate.proxy();
        }

        @Override
        public SSLContext sslContext() {
            return delegate.sslContext();
        }

        @Override
        public SSLParameters sslParameters() {
            return delegate.sslParameters();
        }

        @Override
        public Optional<Authenticator> authenticator() {
            return delegate.authenticator();
        }

        @Override
        public Version version() {
            return delegate.version();
        }

        @Override
        public Optional<Executor> executor() {
            return delegate.executor();
        }
    }
}
