package studio.aight.sdk;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class TracingTest {

    private static void callSite() {
        Tracing.tracedLlmCall("gpt-4o-mini", 180, 60); // <-- this exact line should be recorded
    }

    @Test
    void tracedLlmCallAttributesToCallingLineNotThisFile() {
        Tracing.COST_PROCESSOR.clear();
        callSite();

        Map<Tracing.BucketKey, Tracing.Bucket> buckets = Tracing.COST_PROCESSOR.buckets();
        assertEquals(1, buckets.size());
        Map.Entry<Tracing.BucketKey, Tracing.Bucket> entry = buckets.entrySet().iterator().next();
        Tracing.BucketKey key = entry.getKey();

        assertEquals("TracingTest.java", key.filepath());
        assertEquals("callSite", key.function());
        assertEquals(1, entry.getValue().calls());
    }

    @Test
    void reportFormatsFileLineFunctionCallsAndTokensWithNoDollarFigure() {
        Tracing.COST_PROCESSOR.clear();
        Tracing.tracedLlmCall("gpt-4o-mini", 180, 60);
        String report = Tracing.COST_PROCESSOR.report();
        assertTrue(report.matches("TracingTest\\.java:\\d+ \\(\\w+\\)( \\[[\\w.\\-]+\\])? = \\d+ call\\(s\\), \\d+ in / \\d+ out"),
                "unexpected report format: " + report);
        // This SDK has no price table, so the report must not imply one.
        assertFalse(report.contains("$"), report);
    }

    @Test
    void theReportIsOrderedByCallsNotBySpend() {
        Tracing.COST_PROCESSOR.clear();
        busyLine();
        busyLine();
        quietLine();

        String report = Tracing.COST_PROCESSOR.report();
        assertEquals(2, report.split("\n", -1).length, report);
        assertTrue(report.indexOf("(busyLine)") < report.indexOf("(quietLine)"), report);
    }

    private static void busyLine() {
        Tracing.tracedLlmCall("gpt-4o-mini", 100, 20);
    }

    private static void quietLine() {
        Tracing.tracedLlmCall("gpt-4o", 10, 2);
    }

    @Test
    void oneLineCallingTwoModelsKeepsTwoBuckets() {
        // The model is part of the bucket key. Merging them summed both models'
        // tokens under whichever name was recorded last, and the server then
        // repriced every cheap call at the expensive model's rate.
        //
        // Both calls are issued from the same line (inside the loop), so only
        // the model distinguishes the two buckets.
        Tracing.COST_PROCESSOR.clear();
        for (String model : new String[] {"gpt-4o-mini", "gpt-4o"}) {
            Tracing.tracedLlmCall(model, 1000, 1000);
        }

        Map<Tracing.BucketKey, Tracing.Bucket> buckets = Tracing.COST_PROCESSOR.buckets();
        assertEquals(2, buckets.size());
        for (Map.Entry<Tracing.BucketKey, Tracing.Bucket> e : buckets.entrySet()) {
            assertEquals(1000L, e.getValue().inputTokens(),
                    e.getKey().model() + " must hold only its own tokens, not both models'");
        }
    }

    @Test
    void cacheTokensAndLatencyAreCarriedSeparatelyFromTheUncachedPrompt() {
        Tracing.COST_PROCESSOR.clear();
        Tracing.tracedLlmCall("gpt-4o", 400, 500, new Tracing.CallInfo(9600, 3000, 250));

        Map.Entry<Tracing.BucketKey, Tracing.Bucket> entry =
                Tracing.COST_PROCESSOR.buckets().entrySet().iterator().next();
        Tracing.Bucket b = entry.getValue();
        assertEquals(400L, b.inputTokens(), "inputTokens stays the uncached count");
        assertEquals(9600L, b.cacheReadTokens());
        assertEquals(3000L, b.cacheCreationTokens());
        assertEquals(250.0, b.latencyMs());
    }

    @Test
    void anUnmeasuredLatencyStaysZeroRatherThanBecomingANumber() {
        // Remote omits the key when this is zero, so the Workspace shows "—"
        // rather than a real, impossibly fast 0ms.
        Tracing.COST_PROCESSOR.clear();
        Tracing.tracedLlmCall("gpt-4o", 100, 50);

        Tracing.Bucket b = Tracing.COST_PROCESSOR.buckets().values().iterator().next();
        assertEquals(0.0, b.latencyMs());
    }

    @Test
    void capturesASnippetWindowAroundTheCallingLine() {
        // Tracing.SNIPPETS is keyed by the basename StackWalker reports, so
        // capture only finds the source when it is readable relative to the
        // process working directory — which is why surefire runs from the test
        // source directory (see pom.xml) and why this is best-effort in
        // production. See the README's "filepath is a basename" note.
        Tracing.COST_PROCESSOR.clear();
        Tracing.SNIPPETS.clear();
        callSite();

        Tracing.BucketKey key = Tracing.COST_PROCESSOR.buckets().keySet().iterator().next();
        Tracing.Snippet snippet = Tracing.SNIPPETS.get(new Tracing.SnippetKey(key.filepath(), key.lineno()));

        assertNotNull(snippet, "a readable source line should have been captured");
        assertEquals(Math.max(1, key.lineno() - Tracing.SNIPPET_CONTEXT), snippet.start());
        assertEquals(7, snippet.text().split("\n", -1).length, snippet.text());
        assertTrue(snippet.text().contains("this exact line should be recorded"), snippet.text());
    }

    @Test
    void anUnreadableSourceFileIsSkippedRatherThanThrownOver() {
        Tracing.SNIPPETS.clear();
        Tracing.captureSnippet("no/such/directory/Nothing.java", 3);
        Tracing.captureSnippet(null, 3);

        // Nothing captured, and nothing raised: the Workspace's "not captured"
        // state is the correct outcome for a source tree that isn't there.
        assertTrue(Tracing.SNIPPETS.isEmpty());

        // And a traced call through it still records normally.
        Tracing.COST_PROCESSOR.clear();
        Tracing.tracedLlmCall("gpt-4o", 10, 5);
        assertEquals(1, Tracing.COST_PROCESSOR.buckets().size());
        assertNull(Tracing.SNIPPETS.get(new Tracing.SnippetKey("no/such/directory/Nothing.java", 3)));
    }

    @Test
    void theTraceIdLandsOnTheBucketAndLastNonEmptyOneWins() {
        // All three calls are issued from the same line, so they share one
        // bucket — that is the aggregation the trace id has to survive.
        Tracing.COST_PROCESSOR.clear();
        for (Tracing.CallInfo info : List.of(
                new Tracing.CallInfo(0, 0, 0, "run-1"),
                Tracing.CallInfo.NONE,
                new Tracing.CallInfo(0, 0, 0, "run-2"))) {
            Tracing.tracedLlmCall("gpt-4o", 100, 50, info);
        }

        Tracing.Bucket b = Tracing.COST_PROCESSOR.buckets().values().iterator().next();
        assertEquals(3, b.calls());
        // The middle call carried no trace id and must not have wiped run-1 in
        // the meantime; the last non-empty one is what the row goes out with.
        assertEquals("run-2", b.traceId());
        assertEquals("", new Tracing.CallInfo(0, 0, 0, null).traceId());
    }

    @Test
    void reportValueAttributesToCallingFileAndAccumulates() {
        Tracing.VALUE_BY_FILE.clear();
        Tracing.reportValue(120);
        Tracing.reportValue(30);

        assertEquals(1, Tracing.VALUE_BY_FILE.size());
        Map.Entry<String, java.util.concurrent.atomic.DoubleAdder> entry = Tracing.VALUE_BY_FILE.entrySet().iterator().next();
        assertEquals("TracingTest.java", entry.getKey());
        assertEquals(150.0, entry.getValue().sum());
    }
}
