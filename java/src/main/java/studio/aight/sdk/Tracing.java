package studio.aight.sdk;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.InvalidPathException;
import java.nio.file.Path;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.DoubleAdder;
import java.util.concurrent.atomic.LongAdder;
import java.util.logging.Logger;
import java.util.stream.Collectors;

/**
 * Attributes a (simulated) LLM call to the user-code line that issued it,
 * walking the call stack past this SDK's own frames. Mirrors
 * python/aight/tracing.py.
 */
public final class Tracing {

    private Tracing() {}

    /**
     * Identifies one traced (file, line, function, model). The model is part
     * of the identity, not a field read off the bucket: a line that calls two
     * models stays two buckets, each priced on its own tokens. Merging them
     * summed both models' tokens under whichever name was recorded last, and
     * the server then repriced every cheap call at the expensive model's rate.
     */
    public record BucketKey(String filepath, int lineno, String function, String model, Map<String, String> tags) {

        /**
         * Tags are part of the key for the same reason the model is: merging
         * order o_1's calls into order o_2's row would hand the server one row
         * it can only match to one of them. Map equality ignores order, so
         * equal tag sets always share a bucket.
         */
        public BucketKey {
            tags = tags == null ? Map.of() : Map.copyOf(tags);
        }

        /** A key with no tags — what every call outside a {@link #tags} scope gets. */
        public BucketKey(String filepath, int lineno, String function, String model) {
            this(filepath, lineno, function, model, Map.of());
        }
    }

    // Span tags: business keys (order_id, user_id, ...) the server can match a
    // business event against when the event shares one. Limits match the
    // ingest validator, so the SDK drops what the server would refuse instead
    // of the whole push failing on it. Mirrors tracing.py's tags().
    public static final int MAX_TAGS = 16;
    public static final int MAX_TAG_KEY = 64;
    public static final int MAX_TAG_VALUE = 256;

    private static final ThreadLocal<Map<String, String>> ACTIVE_TAGS = ThreadLocal.withInitial(Map::of);
    private static final Logger LOG = Logger.getLogger("studio.aight.sdk");
    private static final Set<String> WARNED = ConcurrentHashMap.newKeySet();

    // Once per kind per process: a hot loop over a bad tag would otherwise log
    // the same line on every call.
    private static void warnOnce(String kind, String message) {
        if (WARNED.add(kind)) {
            LOG.warning("aight: " + message);
        }
    }

    /**
     * {@code base} plus {@code add}, add winning, with what the server would
     * refuse dropped and every value stringified. add's keys go in sorted, so
     * which extras get dropped past {@link #MAX_TAGS} never depends on map
     * order. Never throws: a bad tag costs you that tag, not your request.
     */
    static Map<String, String> mergeTags(Map<String, String> base, Map<String, ?> add) {
        Map<String, String> out = new LinkedHashMap<>(base);
        Map<String, Object> sorted = new TreeMap<>();
        if (add != null) {
            add.forEach((k, v) -> {
                if (k != null && v != null) {
                    sorted.put(String.valueOf(k), v);
                }
            });
        }
        sorted.forEach((key, raw) -> {
            String value = String.valueOf(raw);
            if (key.length() > MAX_TAG_KEY) {
                warnOnce("key", "tag key longer than " + MAX_TAG_KEY + " chars dropped");
            } else if (value.length() > MAX_TAG_VALUE) {
                warnOnce("value", "tag \"" + key + "\" dropped: value longer than " + MAX_TAG_VALUE + " chars");
            } else if (!out.containsKey(key) && out.size() >= MAX_TAGS) {
                warnOnce("count", "more than " + MAX_TAGS + " tags; extras dropped");
            } else {
                out.put(key, value);
            }
        });
        return Collections.unmodifiableMap(out);
    }

    /**
     * An open tags scope; closing it restores the scope that was active before.
     * Use it in try-with-resources so the restore can't be skipped.
     */
    public static final class Tags implements AutoCloseable {
        private final Map<String, String> previous;

        private Tags(Map<String, String> previous) {
            this.previous = previous;
        }

        @Override
        public void close() {
            if (previous.isEmpty()) {
                ACTIVE_TAGS.remove();
            } else {
                ACTIVE_TAGS.set(previous);
            }
        }
    }

    /**
     * Attaches business keys to every call this thread records until the scope
     * closes: {@code try (var t = Tracing.tags(Map.of("order_id", "o_123"))) {...}}.
     * Nests and merges, inner wins; values are stringified. {@link
     * Remote#pushEvent} inside the scope sends the same tags when it is given
     * none. Per thread: work handed to an executor does not inherit it.
     */
    public static Tags tags(Map<String, ?> tags) {
        Map<String, String> previous = ACTIVE_TAGS.get();
        ACTIVE_TAGS.set(mergeTags(previous, tags));
        return new Tags(previous);
    }

    /** The tags active on this thread, or an empty map outside any scope. */
    public static Map<String, String> currentTags() {
        return ACTIVE_TAGS.get();
    }

    /** Aggregates calls and tokens for one BucketKey. Thread-safe. */
    public static final class Bucket {
        final AtomicInteger calls = new AtomicInteger();
        final LongAdder inputTokens = new LongAdder();
        final LongAdder outputTokens = new LongAdder();
        // Kept beside inputTokens, never folded into it. The platform sums four
        // rates and freezes the result at receive, so an input count that
        // already contained the cached tokens would bill them twice — once at
        // the full input rate and again at the cache rate — permanently.
        final LongAdder cacheReadTokens = new LongAdder();
        final LongAdder cacheCreationTokens = new LongAdder();
        // Summed, not averaged: Remote divides by calls when it reports. Stays
        // zero when nobody timed the call, and Remote omits it from the row.
        final DoubleAdder latencyMs = new DoubleAdder();
        // Last non-empty trace id recorded against this line, matching Python.
        // Empty means "no explicit trace id", which the server reads as an
        // implicit time-window match rather than an explicit one.
        volatile String traceId = "";

        public int calls() {
            return calls.get();
        }

        public long inputTokens() {
            return inputTokens.sum();
        }

        public long outputTokens() {
            return outputTokens.sum();
        }

        public long cacheReadTokens() {
            return cacheReadTokens.sum();
        }

        public long cacheCreationTokens() {
            return cacheCreationTokens.sum();
        }

        /** Total milliseconds across this bucket's calls, or 0 if unmeasured. */
        public double latencyMs() {
            return latencyMs.sum();
        }

        /** The last non-empty trace id recorded here, or "" if none was. */
        public String traceId() {
            return traceId;
        }
    }

    /**
     * Aggregates the calls made from each (file, line, function, model), which
     * is what {@link Remote#push} sends for the platform to price.
     */
    public static final class CostByLineProcessor {
        private final ConcurrentHashMap<BucketKey, Bucket> buckets = new ConcurrentHashMap<>();

        void record(BucketKey key, int inputTokens, int outputTokens, CallInfo info) {
            Bucket b = buckets.computeIfAbsent(key, k -> new Bucket());
            b.calls.incrementAndGet();
            b.inputTokens.add(inputTokens);
            b.outputTokens.add(outputTokens);
            b.cacheReadTokens.add(info.cacheReadTokens());
            b.cacheCreationTokens.add(info.cacheCreationTokens());
            b.latencyMs.add(info.latencyMs());
            if (!info.traceId().isEmpty()) {
                b.traceId = info.traceId();
            }
        }

        /** A live, thread-safe view of the recorded buckets. */
        public Map<BucketKey, Bucket> buckets() {
            return buckets;
        }

        /** Resets all recorded buckets — mainly useful between test runs. */
        public void clear() {
            buckets.clear();
        }

        /**
         * Renders a plain-text call report, busiest line first. Carries no
         * dollar figure: this SDK has no price table, so the platform prices
         * these calls — see {@link #tracedLlmCall}.
         */
        public String report() {
            List<Map.Entry<BucketKey, Bucket>> sorted = buckets.entrySet().stream()
                    .sorted(Comparator.comparingInt((Map.Entry<BucketKey, Bucket> e) -> e.getValue().calls()).reversed())
                    .collect(Collectors.toList());

            StringBuilder sb = new StringBuilder();
            for (Map.Entry<BucketKey, Bucket> e : sorted) {
                if (sb.length() > 0) {
                    sb.append('\n');
                }
                BucketKey k = e.getKey();
                Bucket b = e.getValue();
                String base = k.filepath().contains("/") ? k.filepath().substring(k.filepath().lastIndexOf('/') + 1) : k.filepath();
                String model = (k.model() == null || k.model().isEmpty()) ? "" : String.format(" [%s]", k.model());
                sb.append(String.format("%s:%d (%s)%s = %d call(s), %d in / %d out",
                        base, k.lineno(), k.function(), model, b.calls(), b.inputTokens(), b.outputTokens()));
            }
            return sb.toString();
        }
    }

    /** The package-level singleton every tracedLlmCall records into. */
    public static final CostByLineProcessor COST_PROCESSOR = new CostByLineProcessor();

    private static final String THIS_CLASS = Tracing.class.getName();

    /**
     * The "where in the code" half of a BucketKey, with the model left empty —
     * this walk can only see the stack, not which model the call is for.
     * Callers that know the model rebuild the key with it.
     */
    private static BucketKey resolveCallerFrame() {
        // Exact-match this class only (not the whole package) — a caller
        // that happens to live in the same package as this SDK, like our
        // own white-box tests, must still be attributed correctly.
        return StackWalker.getInstance(StackWalker.Option.RETAIN_CLASS_REFERENCE)
                .walk(frames -> frames
                        .filter(f -> !f.getClassName().equals(THIS_CLASS))
                        .findFirst()
                        .map(f -> new BucketKey(f.getFileName(), f.getLineNumber(), f.getMethodName(), ""))
                        .orElse(new BucketKey("?", 0, "?", "")));
    }

    // How many source lines to grab on each side of an attributed line, for
    // the Workspace's Line Review code panel. Not configurable — a fixed small
    // window is plenty for "what's on this line" context and keeps payloads
    // small. Mirrors python/aight/tracing.py's _SNIPPET_CONTEXT.
    public static final int SNIPPET_CONTEXT = 3;

    /** One captured window of source: the 1-indexed line it starts at, and the text. */
    public record Snippet(int start, String text) {}

    /** The (filepath, lineno) of one attributed call site. */
    public record SnippetKey(String filepath, int lineno) {}

    /**
     * (filepath, lineno) -&gt; that line's source window, read once per unique
     * line ever seen and attached to the chain frame by {@link Remote#push}.
     */
    public static final Map<SnippetKey, Snippet> SNIPPETS = new ConcurrentHashMap<>();

    /**
     * Best-effort: a file that isn't readable — deployed without source, a
     * path recorded on another machine, an app running from a JAR — is skipped
     * silently. A missing snippet degrades to the Workspace's existing "not
     * captured" state; it is never worth raising over, and it must never break
     * a traced call.
     */
    // Package-private rather than private so the white-box tests can exercise
    // the unreadable-file path directly; not part of the public API.
    static void captureSnippet(String filepath, int lineno) {
        if (filepath == null) {
            return;
        }
        SnippetKey key = new SnippetKey(filepath, lineno);
        if (SNIPPETS.containsKey(key)) {
            return;
        }
        int start = Math.max(1, lineno - SNIPPET_CONTEXT);
        try {
            List<String> lines = Files.readAllLines(Path.of(filepath));
            int end = Math.min(lines.size(), lineno + SNIPPET_CONTEXT);
            if (end < start) {
                return;
            }
            String text = String.join("\n", lines.subList(start - 1, end));
            if (text.endsWith("\n")) {
                text = text.substring(0, text.length() - 1);
            }
            SNIPPETS.putIfAbsent(key, new Snippet(start, text));
        } catch (IOException | InvalidPathException e) {
            // Same "no snippet" outcome for every reason the read can fail.
        }
    }

    /**
     * Everything a caller knows about one call beyond its tokens.
     *
     * <p>A record rather than more parameters because the set keeps growing —
     * cache counts, then latency, then a trace id — and every addition to a
     * positional signature breaks every existing caller. {@link #NONE} is the
     * "nothing measured" value, and each zero means the same thing.
     *
     * <p>{@code latencyMs} is yours to measure. This SDK records a call after
     * it returns, not around it, so a duration taken here would measure the
     * recording rather than the model. Zero means "not measured", which reads
     * as "—" on the Workspace's Speed axis rather than as an impossibly fast
     * call.
     *
     * <p>{@code traceId} is the id a business event can name to match this
     * spend explicitly instead of by time window (see
     * {@link Remote#pushEvent}); it is empty by default.
     */
    public record CallInfo(
            long cacheReadTokens, long cacheCreationTokens, double latencyMs, String traceId) {

        public CallInfo {
            traceId = traceId == null ? "" : traceId;
        }

        /** For a caller that has cache counts and a duration but no trace id. */
        public CallInfo(long cacheReadTokens, long cacheCreationTokens, double latencyMs) {
            this(cacheReadTokens, cacheCreationTokens, latencyMs, "");
        }

        public static final CallInfo NONE = new CallInfo(0, 0, 0, "");
    }

    /**
     * Records a LLM call, attributed to the caller's source line, so the
     * platform can price it.
     *
     * <p>Nothing is priced here, and the return type says so: this SDK ships no
     * price table by design. Spend is computed in exactly one place — the
     * backend — which recomputes it from the model and the four token counts at
     * receive time. A local copy drifts from that table, and the drift is
     * silent in the worst direction: a push naming a model the local table knew
     * and the server's no longer did stopped being repriced.
     */
    public static void tracedLlmCall(String model, int inputTokens, int outputTokens) {
        tracedLlmCall(model, inputTokens, outputTokens, CallInfo.NONE);
    }

    /**
     * As above, for a call that reported cache tokens, a duration, or both.
     *
     * <p>{@code inputTokens} must be the <em>uncached</em> prompt. That is the
     * Anthropic convention, where a cache read is reported beside the prompt
     * rather than inside it, and it is what the ingest endpoints expect.
     * OpenAI-shaped usage is the other way round — {@code prompt_tokens}
     * already contains {@code cached_tokens} — so subtract the cached count out
     * before calling. An inclusive count bills those tokens twice, once at the
     * full input rate and again at the cache rate, and the platform freezes
     * cost at receive, so the overcount would be permanent.
     *
     * <p>{@code latencyMs} is yours to measure: this is called after your LLM
     * call returns rather than around it, so there is nothing here to time.
     * Leaving it at zero stores nothing, which the Workspace shows as "—"
     * rather than as a real, impossibly fast call.
     *
     * <p>{@code traceId} is how a business event names this run for an explicit
     * match — pass the same id to {@link Remote#pushEvent} and the server joins
     * them directly instead of guessing by time window.
     */
    public static void tracedLlmCall(
            String model, int inputTokens, int outputTokens, CallInfo info) {
        BucketKey site = resolveCallerFrame();
        captureSnippet(site.filepath(), site.lineno());
        BucketKey key = new BucketKey(site.filepath(), site.lineno(), site.function(), model, currentTags());
        COST_PROCESSOR.record(key, inputTokens, outputTokens, info);
    }

    /** filepath -> total value_usd earned. Reported via reportValue(), pushed via Remote.pushValue(). */
    public static final ConcurrentHashMap<String, DoubleAdder> VALUE_BY_FILE = new ConcurrentHashMap<>();

    /**
     * Records revenue/value this agent earned (a closed deal, a resolved
     * ticket, whatever the caller's business counts), attributed to the
     * calling file — the same "agent" identity tracedLlmCall uses for spend.
     */
    public static void reportValue(double valueUsd) {
        BucketKey key = resolveCallerFrame();
        VALUE_BY_FILE.computeIfAbsent(key.filepath(), k -> new DoubleAdder()).add(valueUsd);
    }
}
