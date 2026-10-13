package studio.aight.sdk;

import java.util.Map;
import java.util.UUID;

/**
 * The optional half of {@link Remote#pushEvent}: everything about a business
 * event except its name and value, all of it defaulted.
 *
 * <p>A record with withers rather than a long parameter list because Java has
 * no default arguments — {@link #defaults()} gives you the common case, and
 * only the fields you actually care about need overriding:
 *
 * <pre>{@code
 * Remote.pushEvent("checkout.completed", 49.0,
 *         EventOptions.defaults().withTraceId(runId));
 * }</pre>
 *
 * <p>Every component is null-defaulted in the compact constructor, so
 * {@code new EventOptions(null, null, null, 0, null, null)} and
 * {@code EventOptions.defaults()} mean the same thing.
 */
public record EventOptions(
        String eventId,
        String traceId,
        String currency,
        double timestamp,
        String apiKey,
        String url,
        String agent,
        Map<String, String> tags) {

    public EventOptions {
        eventId = (eventId == null || eventId.isEmpty()) ? UUID.randomUUID().toString() : eventId;
        traceId = traceId == null ? "" : traceId;
        currency = (currency == null || currency.isEmpty()) ? "USD" : currency;
        // Unix seconds, matching the wire contract — not millis.
        timestamp = timestamp > 0 ? timestamp : System.currentTimeMillis() / 1000.0;
        // apiKey is deliberately left as it came in (null means "read
        // AIGHT_API_KEY at push time"), matching Remote.push/pushValue.
        url = (url == null || url.isEmpty())
                ? System.getenv().getOrDefault(
                        "AIGHT_EVENTS_INGEST_URL",
                        Remote.DEFAULT_INGEST_URL.replace("/spans", "/events"))
                : url;
        agent = agent == null ? "" : agent;
        // Same limits and stringifying as a tags scope; empty means "use the
        // thread's tags scope, if any" at push time.
        tags = tags == null || tags.isEmpty() ? Map.of() : Tracing.mergeTags(Map.of(), tags);
    }

    /** The pre-agent/tags shape, kept so existing callers still compile. */
    public EventOptions(String eventId, String traceId, String currency, double timestamp, String apiKey, String url) {
        this(eventId, traceId, currency, timestamp, apiKey, url, "", Map.of());
    }

    /** A fresh event: new id, "USD", now, no trace id, key and URL from the environment. */
    public static EventOptions defaults() {
        return new EventOptions(null, null, null, 0, null, null, null, null);
    }

    public EventOptions withEventId(String value) {
        return new EventOptions(value, traceId, currency, timestamp, apiKey, url, agent, tags);
    }

    /**
     * The trace id of the run this event belongs to, if you have one. Set it
     * and the server matches the event to that spend explicitly; leave it
     * empty and it falls back to a time-window guess.
     */
    public EventOptions withTraceId(String value) {
        return new EventOptions(eventId, value, currency, timestamp, apiKey, url, agent, tags);
    }

    public EventOptions withCurrency(String value) {
        return new EventOptions(eventId, traceId, value, timestamp, apiKey, url, agent, tags);
    }

    /** Unix seconds. Defaults to the time of the push. */
    public EventOptions withTimestamp(double value) {
        return new EventOptions(eventId, traceId, currency, value, apiKey, url, agent, tags);
    }

    public EventOptions withApiKey(String value) {
        return new EventOptions(eventId, traceId, currency, timestamp, value, url, agent, tags);
    }

    public EventOptions withUrl(String value) {
        return new EventOptions(eventId, traceId, currency, timestamp, apiKey, value, agent, tags);
    }

    /**
     * The agent that produced this event: the same filepath as the chain
     * frame on its span rows (a bare file name such as "Agent.java" in this
     * SDK) and the one pushValue uses. The server matches a declared agent
     * before shared tags or the time window.
     */
    public EventOptions withAgent(String value) {
        return new EventOptions(eventId, traceId, currency, timestamp, apiKey, url, value, tags);
    }

    /**
     * Business keys for this event; when unset, the thread's tags scope is
     * used. An event matches on a tag only when the same key=value is also on
     * a span, and only when one agent's spans carry it — a tag two agents
     * share is ambiguous and is not matched.
     */
    public EventOptions withTags(Map<String, ?> value) {
        return new EventOptions(eventId, traceId, currency, timestamp, apiKey, url, agent,
                value == null ? null : Tracing.mergeTags(Map.of(), value));
    }
}
