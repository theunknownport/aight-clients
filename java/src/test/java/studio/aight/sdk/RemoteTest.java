package studio.aight.sdk;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Drives Remote against a stub ingest server built from the JDK's own
 * {@code com.sun.net.httpserver} — no test dependency, and it checks the two
 * things a hand-built body can silently get wrong: that /events takes a single
 * object rather than an array, and that the response is parsed back into a map.
 */
class RemoteTest {

    private HttpServer server;
    private final AtomicReference<String> body = new AtomicReference<>();
    private final AtomicReference<Headers> headers = new AtomicReference<>();
    private String url;
    private String spansUrl;

    @BeforeEach
    void startStubServer() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/api/ingest/events", exchange -> {
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            headers.set(exchange.getRequestHeaders());
            byte[] response = ("{\"ingested\":1,\"results\":[{\"event_name\":\"checkout.completed\","
                    + "\"match_type\":\"EXPLICIT\",\"confidence_score\":0.9,\"matched\":true}]}")
                    .getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.createContext("/api/ingest/spans", exchange -> {
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            headers.set(exchange.getRequestHeaders());
            byte[] response = "{\"ingested\":2}".getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();
        String base = "http://127.0.0.1:" + server.getAddress().getPort() + "/api/ingest";
        url = base + "/events";
        spansUrl = base + "/spans";
    }

    @AfterEach
    void stopStubServer() {
        server.stop(0);
    }

    @Test
    void pushEventSendsASingleObjectWithAllFourHeaders() throws Exception {
        Map<String, Object> response = Remote.pushEvent(
                "checkout.completed", 49.0,
                EventOptions.defaults()
                        .withApiKey("aight_test_key")
                        .withUrl(url)
                        .withTraceId("run-7")
                        .withEventId("evt_1")
                        .withTimestamp(1758307200.0));

        // A single object, never an array — /api/ingest/events is the one
        // ingest endpoint that differs from /spans and /value.
        assertTrue(body.get().startsWith("{") && body.get().endsWith("}"), body.get());
        assertTrue(body.get().contains("\"event_id\":\"evt_1\""), body.get());
        assertTrue(body.get().contains("\"event_name\":\"checkout.completed\""), body.get());
        assertTrue(body.get().contains("\"value\":49.0"), body.get());
        assertTrue(body.get().contains("\"currency\":\"USD\""), body.get());
        // Unix seconds, whatever spelling Java gives the double.
        assertTrue(body.get().matches("(?s).*\"timestamp\":(1\\.7583072E9|1758307200(?:\\.0)?).*"), body.get());
        // The trace id is what turns a guess into an explicit match.
        assertTrue(body.get().contains("\"trace_id\":\"run-7\""), body.get());

        assertEquals("Bearer aight_test_key", headers.get().getFirst("Authorization"));
        assertEquals("java", headers.get().getFirst("X-Aight-Sdk-Language"));
        assertEquals(Remote.SDK_VERSION, headers.get().getFirst("X-Aight-Sdk-Version"));
        assertEquals("application/json", headers.get().getFirst("Content-Type"));

        // The parsed response, not the raw string: results carry the match type
        // and confidence the backend resolved.
        assertEquals(1L, response.get("ingested"));
        List<?> results = (List<?>) response.get("results");
        assertNotNull(results);
        @SuppressWarnings("unchecked")
        Map<String, Object> first = (Map<String, Object>) results.get(0);
        assertEquals("EXPLICIT", first.get("match_type"));
        assertEquals(0.9, first.get("confidence_score"));
        assertEquals(Boolean.TRUE, first.get("matched"));
    }

    @Test
    void pushSendsAnArrayCarryingTheTraceIdAndTheSnippetWhenThereIsOne() throws Exception {
        Tracing.COST_PROCESSOR.clear();
        Tracing.SNIPPETS.clear();
        Tracing.SnippetKey withSnippet = new Tracing.SnippetKey("RemoteTest.java", 1);
        Tracing.SNIPPETS.put(withSnippet, new Tracing.Snippet(1, "line one\nline two"));

        Tracing.COST_PROCESSOR.record(
                new Tracing.BucketKey("RemoteTest.java", 1, "stub", "gpt-4o"),
                100, 50, new Tracing.CallInfo(0, 0, 0, "run-9"));
        // A second line that captured nothing: its frame must carry no snippet
        // keys at all, so the Workspace can tell it apart from an empty one.
        Tracing.COST_PROCESSOR.record(
                new Tracing.BucketKey("RemoteTest.java", 2, "stub", "gpt-4o"),
                10, 5, Tracing.CallInfo.NONE);

        Remote.push(Tracing.COST_PROCESSOR, "k", spansUrl);

        String sent = body.get();
        assertTrue(sent.startsWith("[") && sent.endsWith("]"), sent);
        assertTrue(sent.contains("\"trace_id\":\"run-9\""), sent);
        assertTrue(sent.contains("\"snippet_start\":1,\"snippet\":\"line one\\nline two\""), sent);
        assertEquals(1, sent.split("\"snippet\":", -1).length - 1, "only the captured frame carries a snippet");
        assertEquals("java", headers.get().getFirst("X-Aight-Sdk-Language"));
        assertEquals(Remote.SDK_VERSION, headers.get().getFirst("X-Aight-Sdk-Version"));
    }

    @Test
    void aTracedCallPushesModelAndTokensAndNoCostFigure() throws Exception {
        // The whole point of dropping the SDK's price table: the row makes no
        // claim about cost, so no cost field is sent at all — the server's
        // fabrication check reads an absent one as "no claim" and a present one
        // as a number it has to trust or overwrite.
        //
        // The row's scalar tail is pinned character for character rather than
        // spot-checked with contains(), so a cost field added to the writer
        // fails this test instead of sliding through.
        Tracing.COST_PROCESSOR.clear();
        Tracing.SNIPPETS.clear();
        Tracing.tracedLlmCall("gpt-4o", 400, 500, new Tracing.CallInfo(9600, 3000, 250, "run-1"));

        Remote.push(Tracing.COST_PROCESSOR, "k", spansUrl);

        String sent = body.get();
        int tail = sent.indexOf("],\"calls\":");
        assertTrue(tail > 0, sent);
        assertEquals("\"calls\":1,\"model\":\"gpt-4o\""
                + ",\"input_tokens\":400,\"output_tokens\":500"
                + ",\"cache_read_tokens\":9600,\"cache_creation_tokens\":3000"
                + ",\"trace_id\":\"run-1\",\"latency_ms\":250.0}]",
                sent.substring(tail + 2), sent);
        // The model travels with the row, or the platform has nothing to price.
        assertTrue(sent.contains("\"model\":\"gpt-4o\""), sent);
    }

    @Test
    void pushValueClearsTheAccumulatorOnceTheServerAcknowledges() throws Exception {
        // reportValue only ever adds to VALUE_BY_FILE and nothing drained it, so
        // passing it to pushValue — which is what the docs tell you to do —
        // re-sent every earlier earning on each flush. /api/ingest/value is a
        // plain INSERT with no dedup, so the same closed deal was counted again
        // and again, and the inflation was permanent.
        server.createContext("/api/ingest/value", exchange -> {
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            byte[] response = "{\"ingested\":1}".getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        String valueUrl = spansUrl.replace("/spans", "/value");

        Tracing.VALUE_BY_FILE.clear();
        Tracing.reportValue(49.0);
        Remote.pushValue("aight_test_key", valueUrl);

        String first = body.get();
        assertTrue(first.contains("\"value_usd\":49.0"), first);

        body.set(null);
        Remote.pushValue("aight_test_key", valueUrl);
        assertNull(body.get(), "the second push had nothing left, so it must not send");
    }

    @Test
    void pushValueKeepsTheAccumulatorWhenThePushFails() {
        // Clearing is only safe after an acknowledged push — losing unsent
        // earnings to a network blip is worse than the double-count it prevents.
        Tracing.VALUE_BY_FILE.clear();
        Tracing.reportValue(49.0);

        assertThrows(IOException.class, () -> Remote.pushValue("aight_test_key", "http://127.0.0.1:1/api/ingest/value"));
        assertEquals(1, Tracing.VALUE_BY_FILE.size(), "a failed push must not discard unsent earnings");
    }

    @Test
    void pushEventDefaultsTheIdCurrencyAndTimestampRatherThanOmittingThem() throws Exception {
        // The server requires event_id, event_name and timestamp on every row,
        // so "unset" has to arrive as a real value, never as a missing field.
        Remote.pushEvent("signup", 0.0, EventOptions.defaults().withApiKey("k").withUrl(url));

        assertTrue(body.get().contains("\"currency\":\"USD\""), body.get());
        assertTrue(body.get().contains("\"trace_id\":\"\""), body.get());
        assertTrue(body.get().matches(".*\"event_id\":\"[0-9a-f\\-]{36}\".*"), body.get());
        assertTrue(body.get().matches(".*\"timestamp\":1\\.7\\d+E9.*"), body.get());
    }
}
