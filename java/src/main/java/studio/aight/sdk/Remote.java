package studio.aight.sdk;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Pushes locally-traced spend data to your hosted AIght Workspace.
 * Uses java.net.http.HttpClient (built in since Java 11) — no HTTP client
 * dependency, and no JSON dependency: rows are written by hand (a call chain
 * of a few string/int frames plus scalars) and responses are read by the
 * minimal parser at the bottom of this file.
 */
public final class Remote {

    public static final String DEFAULT_INGEST_URL = "https://api.aight.studio/api/ingest/spans";

    // Must match projects.CURRENT_SDK_VERSIONS["java"] on the backend —
    // bump both together on release.
    public static final String SDK_VERSION = "0.1.0";

    private Remote() {}

    /** apiKey/url may be null; falls back to AIGHT_API_KEY / AIGHT_INGEST_URL (or the default URL). */
    public static void push(Tracing.CostByLineProcessor processor, String apiKey, String url) throws IOException, InterruptedException {
        String key = apiKey != null ? apiKey : System.getenv("AIGHT_API_KEY");
        if (key == null || key.isEmpty()) {
            throw new IllegalStateException(
                    "No API key. Pass one, or set AIGHT_API_KEY (get one from the Integrate tab of your aight.studio dashboard).");
        }
        String endpoint = url != null ? url : System.getenv().getOrDefault("AIGHT_INGEST_URL", DEFAULT_INGEST_URL);

        Map<Tracing.BucketKey, Tracing.Bucket> buckets = processor.buckets();
        if (buckets.isEmpty()) {
            return;
        }

        StringBuilder json = new StringBuilder("[");
        boolean first = true;
        for (Map.Entry<Tracing.BucketKey, Tracing.Bucket> e : buckets.entrySet()) {
            if (!first) {
                json.append(',');
            }
            first = false;
            Tracing.BucketKey k = e.getKey();
            Tracing.Bucket b = e.getValue();
            // The server requires each row to carry a call chain (see
            // README.md "Wire protocol") — a flat row is rejected with
            // "missing 'chain'". This SDK attributes a call to exactly one
            // line, so that line is sent as a one-frame chain; real
            // multi-frame chains need stack walking here, the way the Python
            // SDK does it.
            // model + token counts go with the row, and no cost figure does:
            // the platform prices this from its own table, and the server reads
            // an absent cost as "this row makes no claim about cost" — which is
            // exactly true, since this SDK has no price table.
            json.append("{\"chain\":[{\"filepath\":").append(quote(k.filepath()))
                    .append(",\"lineno\":").append(k.lineno())
                    .append(",\"function\":").append(quote(k.function()));
            // Source context for the Workspace's code panel, when capture
            // managed to read the file. Omitted entirely rather than sent
            // empty — the panel distinguishes "not captured" from "captured
            // and empty", and only one of those is worth showing.
            Tracing.Snippet snippet = Tracing.SNIPPETS.get(new Tracing.SnippetKey(k.filepath(), k.lineno()));
            if (snippet != null) {
                json.append(",\"snippet_start\":").append(snippet.start())
                        .append(",\"snippet\":").append(quote(snippet.text()));
            }
            json.append("}],\"calls\":").append(b.calls())
                    .append(",\"model\":").append(quote(k.model() == null ? "" : k.model()))
                    .append(",\"input_tokens\":").append(b.inputTokens())
                    .append(",\"output_tokens\":").append(b.outputTokens())
                    // Their own counts, never folded into input_tokens. The
                    // platform sums four rates and freezes the result at
                    // receive, so an inclusive input count would bill the
                    // cached tokens twice, permanently.
                    .append(",\"cache_read_tokens\":").append(b.cacheReadTokens())
                    .append(",\"cache_creation_tokens\":").append(b.cacheCreationTokens())
                    // Empty means "no explicit trace id", which the server
                    // reads as "match business events by time window" rather
                    // than as a bogus explicit id.
                    .append(",\"trace_id\":").append(quote(b.traceId()));
            // Omitted entirely when nobody timed the call, rather than sent as
            // 0. The server stores whatever arrives, and the Workspace reads a
            // stored 0 as a real — impossibly fast — measurement where an
            // absent field shows as "—".
            if (b.latencyMs() > 0) {
                json.append(",\"latency_ms\":").append(b.latencyMs());
            }
            json.append('}');
        }
        json.append(']');

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(endpoint))
                .timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + key)
                .header("X-Aight-Sdk-Language", "java")
                .header("X-Aight-Sdk-Version", SDK_VERSION)
                .POST(HttpRequest.BodyPublishers.ofString(json.toString()))
                .build();

        HttpResponse<Void> response = HttpClient.newHttpClient().send(request, HttpResponse.BodyHandlers.discarding());
        if (response.statusCode() >= 300) {
            throw new IOException("aight push failed: HTTP " + response.statusCode());
        }
        // Only after the server has acknowledged. The ingest endpoint *adds*
        // each row to what it already holds, so re-sending these running
        // totals on a later push would count every call a second time and
        // quietly inflate the AIght Workspace's spend. Clearing on success
        // makes a second push send only what happened since the first.
        //
        // ponytail: no idempotency key, so a push whose response is lost still
        // double-counts on retry. Add a client run-id if that ever bites.
        processor.clear();
    }

    /**
     * apiKey/url may be null; falls back to AIGHT_API_KEY / AIGHT_VALUE_INGEST_URL
     * (or the spend URL's /value sibling). Same clear-on-success as {@link #push},
     * for the same reason: {@link Tracing#reportValue} only ever <em>adds</em> to
     * the map it hands you, so a second push of the same running totals would
     * insert the same earnings again. /api/ingest/value is a plain INSERT with no
     * dedup, so that inflation would be permanent.
     */
    public static void pushValue(String apiKey, String url) throws IOException, InterruptedException {
        String key = apiKey != null ? apiKey : System.getenv("AIGHT_API_KEY");
        if (key == null || key.isEmpty()) {
            throw new IllegalStateException(
                    "No API key. Pass one, or set AIGHT_API_KEY (get one from the Integrate tab of your aight.studio dashboard).");
        }
        String endpoint = url != null
                ? url
                : System.getenv().getOrDefault("AIGHT_VALUE_INGEST_URL", DEFAULT_INGEST_URL.replace("/spans", "/value"));

        Map<String, java.util.concurrent.atomic.DoubleAdder> values = Tracing.VALUE_BY_FILE;
        if (values.isEmpty()) {
            return;
        }

        StringBuilder json = new StringBuilder("[");
        boolean first = true;
        for (Map.Entry<String, java.util.concurrent.atomic.DoubleAdder> e : values.entrySet()) {
            if (!first) {
                json.append(',');
            }
            first = false;
            json.append("{\"filepath\":").append(quote(e.getKey()))
                    .append(",\"value_usd\":").append(e.getValue().sum())
                    .append('}');
        }
        json.append(']');

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(endpoint))
                .timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + key)
                .header("X-Aight-Sdk-Language", "java")
                .header("X-Aight-Sdk-Version", SDK_VERSION)
                .POST(HttpRequest.BodyPublishers.ofString(json.toString()))
                .build();

        HttpResponse<Void> response = HttpClient.newHttpClient().send(request, HttpResponse.BodyHandlers.discarding());
        if (response.statusCode() >= 300) {
            throw new IOException("aight push failed: HTTP " + response.statusCode());
        }
        // Only after the server has acknowledged. A failed push keeps the
        // totals, so a retry can still send them.
        Tracing.VALUE_BY_FILE.clear();
    }

    /**
     * Reports a business KPI/event — a checkout, a signup, a ticket closed —
     * to power the Workspace's spent-vs-earned view. The event's value is what
     * that outcome actually earned; never invent or estimate one.
     *
     * <p>Returns the endpoint's parsed response, shaped
     * {@code {"ingested": n, "results": [...]}} where each result carries the
     * {@code match_type} ({@code EXPLICIT} / {@code IMPLICIT} /
     * {@code UNMATCHED}) and {@code confidence_score} it resolved to. It is the
     * match *report*, not a confirmation that a match was found.
     *
     * <p>{@code opts} may be null, which is {@link EventOptions#defaults()} —
     * a fresh id, "USD", now, and the API key from {@code AIGHT_API_KEY}.
     */
    public static Map<String, Object> pushEvent(String eventName, double value, EventOptions opts)
            throws IOException, InterruptedException {
        EventOptions o = opts != null ? opts : EventOptions.defaults();
        String key = o.apiKey() != null ? o.apiKey() : System.getenv("AIGHT_API_KEY");
        if (key == null || key.isEmpty()) {
            throw new IllegalStateException(
                    "No API key. Pass one, or set AIGHT_API_KEY (get one from the Integrate tab of your aight.studio dashboard).");
        }

        // A single object, not an array — /api/ingest/events is the one ingest
        // endpoint that takes one event per request.
        String body = "{\"event_id\":" + quote(o.eventId())
                + ",\"event_name\":" + quote(eventName)
                + ",\"value\":" + value
                + ",\"currency\":" + quote(o.currency())
                + ",\"timestamp\":" + o.timestamp()
                + ",\"trace_id\":" + quote(o.traceId())
                + '}';

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(o.url()))
                .timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + key)
                .header("X-Aight-Sdk-Language", "java")
                .header("X-Aight-Sdk-Version", SDK_VERSION)
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();

        HttpResponse<String> response =
                HttpClient.newHttpClient().send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() >= 300) {
            throw new IOException("aight push failed: HTTP " + response.statusCode());
        }
        Object parsed = new Json(response.body()).parse();
        @SuppressWarnings("unchecked")
        Map<String, Object> result = parsed instanceof Map ? (Map<String, Object>) parsed : Map.of();
        return result;
    }

    /**
     * A JSON string literal. Escapes the control characters too, not just the
     * quotes: a snippet is a multi-line window of source, so a raw newline or
     * tab inside the quotes makes the whole body invalid JSON and the server
     * rejects the push.
     */
    private static String quote(String s) {
        StringBuilder out = new StringBuilder(s.length() + 2).append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                default -> {
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
                }
            }
        }
        return out.append('"').toString();
    }

    /**
     * The smallest JSON reader that can decode an ingest response: objects,
     * arrays, strings, numbers, booleans and null.
     *
     * <p>Hand-rolled because this SDK has zero third-party dependencies on
     * purpose (see pom.xml) and the JDK ships no JSON parser. It only ever
     * reads a response we already trust the shape of, so it stays a reader —
     * no writer, no streaming, no annotations.
     *
     * <p>Package-private rather than private so {@link AutoInstrument} can
     * decode a provider response with it too, instead of carrying a second
     * parser.
     */
    static final class Json {
        private final String s;
        private int i;

        Json(String s) {
            this.s = s;
        }

        /** Parses the whole document, or throws IllegalArgumentException if it isn't JSON. */
        Object parse() {
            Object value = value();
            ws();
            if (i != s.length()) {
                throw new IllegalArgumentException("trailing JSON at offset " + i);
            }
            return value;
        }

        private Object value() {
            ws();
            char c = s.charAt(i);
            switch (c) {
                case '{':
                    return object();
                case '[':
                    return array();
                case '"':
                    return string();
                case 't':
                    return literal("true", Boolean.TRUE);
                case 'f':
                    return literal("false", Boolean.FALSE);
                case 'n':
                    return literal("null", null);
                default:
                    return number();
            }
        }

        private Map<String, Object> object() {
            Map<String, Object> map = new LinkedHashMap<>();
            i++; // '{'
            ws();
            if (s.charAt(i) == '}') {
                i++;
                return map;
            }
            while (true) {
                ws();
                String name = string();
                ws();
                i++; // ':'
                map.put(name, value());
                ws();
                char c = s.charAt(i++);
                if (c == '}') {
                    return map;
                }
            }
        }

        private List<Object> array() {
            List<Object> list = new ArrayList<>();
            i++; // '['
            ws();
            if (s.charAt(i) == ']') {
                i++;
                return list;
            }
            while (true) {
                list.add(value());
                ws();
                char c = s.charAt(i++);
                if (c == ']') {
                    return list;
                }
            }
        }

        private String string() {
            if (s.charAt(i) != '"') {
                throw new IllegalArgumentException("expected string at offset " + i);
            }
            i++;
            StringBuilder out = new StringBuilder();
            while (true) {
                char c = s.charAt(i++);
                if (c == '"') {
                    return out.toString();
                }
                if (c != '\\') {
                    out.append(c);
                    continue;
                }
                char esc = s.charAt(i++);
                switch (esc) {
                    case 'n' -> out.append('\n');
                    case 't' -> out.append('\t');
                    case 'r' -> out.append('\r');
                    case 'b' -> out.append('\b');
                    case 'f' -> out.append('\f');
                    case 'u' -> {
                        out.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        i += 4;
                    }
                    default -> out.append(esc); // \" \\ \/ and anything else
                }
            }
        }

        private Object number() {
            int start = i;
            while (i < s.length() && "-+.eE0123456789".indexOf(s.charAt(i)) >= 0) {
                i++;
            }
            String text = s.substring(start, i);
            // Integral values stay Long, the way a JSON number that names a
            // count should — "ingested" is 1, not 1.0.
            if (text.indexOf('.') < 0 && text.indexOf('e') < 0 && text.indexOf('E') < 0) {
                return Long.parseLong(text);
            }
            return Double.parseDouble(text);
        }

        private Object literal(String text, Object value) {
            if (!s.startsWith(text, i)) {
                throw new IllegalArgumentException("bad literal at offset " + i);
            }
            i += text.length();
            return value;
        }

        private void ws() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
                i++;
            }
        }
    }
}
