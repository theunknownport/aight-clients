package studio.aight.sdk;

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
        String eventId, String traceId, String currency, double timestamp, String apiKey, String url) {

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
    }

    /** A fresh event: new id, "USD", now, no trace id, key and URL from the environment. */
    public static EventOptions defaults() {
        return new EventOptions(null, null, null, 0, null, null);
    }

    public EventOptions withEventId(String value) {
        return new EventOptions(value, traceId, currency, timestamp, apiKey, url);
    }

    /**
     * The trace id of the run this event belongs to, if you have one. Set it
     * and the server matches the event to that spend explicitly; leave it
     * empty and it falls back to a time-window guess.
     */
    public EventOptions withTraceId(String value) {
        return new EventOptions(eventId, value, currency, timestamp, apiKey, url);
    }

    public EventOptions withCurrency(String value) {
        return new EventOptions(eventId, traceId, value, timestamp, apiKey, url);
    }

    /** Unix seconds. Defaults to the time of the push. */
    public EventOptions withTimestamp(double value) {
        return new EventOptions(eventId, traceId, currency, value, apiKey, url);
    }

    public EventOptions withApiKey(String value) {
        return new EventOptions(eventId, traceId, currency, timestamp, value, url);
    }

    public EventOptions withUrl(String value) {
        return new EventOptions(eventId, traceId, currency, timestamp, apiKey, value);
    }
}
