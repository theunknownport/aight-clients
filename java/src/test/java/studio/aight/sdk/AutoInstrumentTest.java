package studio.aight.sdk;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Drives AutoInstrument against a stub provider endpoint. Java cannot patch a
 * provider's classes from a library — the OpenTelemetry javaagent is a JVM
 * launch flag — so the wrapping client is the whole of what this SDK can do,
 * and these are the two things it must not get wrong: which convention the
 * prompt count follows, and that a response it cannot read is passed through
 * untouched and records nothing.
 */
class AutoInstrumentTest {

    private HttpServer server;
    private String base;

    @BeforeEach
    void startStubServer() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/openai", exchange -> send(exchange, 200,
                "{\"model\":\"gpt-4o\",\"usage\":{\"prompt_tokens\":700,\"completion_tokens\":40,"
                        + "\"prompt_tokens_details\":{\"cached_tokens\":600}}}"));
        server.createContext("/anthropic", exchange -> send(exchange, 200,
                "{\"model\":\"claude-3-5-sonnet-20241022\",\"usage\":{\"input_tokens\":180,\"output_tokens\":60,"
                        + "\"cache_read_input_tokens\":500,\"cache_creation_input_tokens\":20}}"));
        server.createContext("/error", exchange -> send(exchange, 429,
                "{\"error\":{\"message\":\"rate limited\"}}"));
        server.start();
        base = "http://127.0.0.1:" + server.getAddress().getPort();
    }

    private static void send(HttpExchange exchange, int status, String json) throws java.io.IOException {
        exchange.getRequestBody().readAllBytes(); // drained, so the client isn't left waiting
        byte[] response = json.getBytes(StandardCharsets.UTF_8);
        exchange.sendResponseHeaders(status, response.length);
        exchange.getResponseBody().write(response);
        exchange.close();
    }

    @AfterEach
    void stopStubServer() {
        server.stop(0);
    }

    /** One LLM call through the wrapping client, from a line the SDK can attribute. */
    private static HttpResponse<String> callSite(String provider, String path) throws Exception {
        HttpClient client = AutoInstrument.tracedHttpClient(provider);
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(path))
                .POST(HttpRequest.BodyPublishers.ofString("{\"model\":\"placeholder\"}"))
                .build();
        return client.send(request, HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void readsOpenAIPromptTotalAsInclusiveOfTheCachedTokens() throws Exception {
        // 700 prompt_tokens with 600 of them cached: the wire wants the uncached
        // 100. Folding the cache read in bills those tokens twice — once at the
        // full input rate and again at the cache rate — and the platform freezes
        // cost at receive, so the overcount could never be corrected.
        Tracing.COST_PROCESSOR.clear();
        callSite(AutoInstrument.OPENAI, base + "/openai");

        Map<Tracing.BucketKey, Tracing.Bucket> buckets = Tracing.COST_PROCESSOR.buckets();
        assertEquals(1, buckets.size());
        Map.Entry<Tracing.BucketKey, Tracing.Bucket> entry = buckets.entrySet().iterator().next();
        assertEquals("gpt-4o", entry.getKey().model());
        assertEquals("callSite", entry.getKey().function());
        Tracing.Bucket b = entry.getValue();
        assertEquals(100L, b.inputTokens());
        assertEquals(40L, b.outputTokens());
        assertEquals(600L, b.cacheReadTokens());
        // A wrapper measures nothing itself, and this SDK never invents a
        // duration: Remote omits the key, and the Workspace shows "—".
        assertEquals(0.0, b.latencyMs());
        assertEquals("", b.traceId());
    }

    @Test
    void readsAnthropicCountsWithoutSubtractingTheCacheRead() throws Exception {
        // Anthropic's input_tokens is already the uncached prompt, with the cache
        // counts reported beside it — subtracting there is the mirror-image of
        // the OpenAI mistake.
        Tracing.COST_PROCESSOR.clear();
        callSite(AutoInstrument.ANTHROPIC, base + "/anthropic");

        Tracing.Bucket b = Tracing.COST_PROCESSOR.buckets().values().iterator().next();
        assertEquals(180L, b.inputTokens());
        assertEquals(60L, b.outputTokens());
        assertEquals(500L, b.cacheReadTokens());
        assertEquals(20L, b.cacheCreationTokens());
    }

    @Test
    void anUnreadableResponseIsPassedThroughAndRecordsNothing() throws Exception {
        Tracing.COST_PROCESSOR.clear();
        HttpResponse<String> response = callSite(AutoInstrument.OPENAI, base + "/error");

        // The caller's own response comes back intact — tracing must never break
        // the call it is tracing — and nothing unpriceable reaches the buckets.
        assertEquals(429, response.statusCode());
        assertTrue(response.body().contains("rate limited"), response.body());
        assertTrue(Tracing.COST_PROCESSOR.buckets().isEmpty());
    }

    @Test
    void enableReportsEachProviderOnceAndSkipsUnknownNames() {
        // enable() declares; it patches nothing. Enabling the same provider
        // twice must not report it twice, and an unknown name is skipped rather
        // than fatal — the same shape the other three SDKs return.
        AutoInstrument.enable(AutoInstrument.OPENAI);
        assertEquals(List.of(), AutoInstrument.enable(AutoInstrument.OPENAI));
        assertEquals(List.of(), AutoInstrument.enable("gemini"));
        assertEquals(List.of(AutoInstrument.ANTHROPIC),
                AutoInstrument.enable(AutoInstrument.ANTHROPIC));
    }
}
