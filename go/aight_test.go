package aight

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

func TestPushSendsChainShapeAndClearsTheProcessor(t *testing.T) {
	// The server requires each row to carry "chain" — a flat row is rejected
	// with "missing 'chain'", which made every push fail with 400. It also
	// *adds* each row to what it already holds, so the processor is cleared
	// once the push is acknowledged, or a second push would double-count.
	var sent []map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&sent); err != nil {
			t.Errorf("decode body: %v", err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	p := newProcessor()
	p.record(BucketKey{Filepath: "agent.go", Lineno: 42, Function: "CallLLM", Model: "gpt-4o-mini"}, 180, 60, 0, 0, 0, "")

	if err := Push(p, "aight_test_key", srv.URL); err != nil {
		t.Fatalf("Push: %v", err)
	}

	if len(sent) != 1 {
		t.Fatalf("expected 1 row, got %d", len(sent))
	}
	row := sent[0]
	chain, ok := row["chain"].([]any)
	if !ok || len(chain) != 1 {
		t.Fatalf("row must carry a non-empty chain, got %#v", row["chain"])
	}
	frame, _ := chain[0].(map[string]any)
	if frame["filepath"] != "agent.go" || frame["function"] != "CallLLM" || frame["lineno"] != float64(42) {
		t.Fatalf("unexpected frame: %#v", frame)
	}
	if _, flat := row["filepath"]; flat {
		t.Fatalf("must not also send the flat shape: %#v", row)
	}
	// "agent.go" is not a readable file, so this frame has no source to carry.
	// The keys stay off the wire entirely and the Workspace reads that as
	// "not captured" — an empty string would render as a blank code panel.
	if _, captured := frame["snippet"]; captured {
		t.Fatalf("a line with no readable source must not claim a snippet: %#v", frame)
	}
	if _, captured := frame["snippet_start"]; captured {
		t.Fatalf("snippet_start must not travel without a snippet: %#v", frame)
	}
	// Without these the server has nothing to price the row with.
	if row["model"] != "gpt-4o-mini" || row["input_tokens"] != float64(180) || row["output_tokens"] != float64(60) {
		t.Fatalf("row must carry model and token counts, got %#v", row)
	}
	if n := len(p.Snapshot()); n != 0 {
		t.Fatalf("expected the processor to be cleared after a push, %d bucket(s) left", n)
	}
}

func TestPushCarriesCacheTokensAsTheirOwnCounts(t *testing.T) {
	// The gap this closes: the counts used to stop at the process boundary, so a
	// cache-bearing call under-reported cost through every SDK — against a server
	// that already priced all four rates and froze the result at receive.
	var sent []map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&sent); err != nil {
			t.Errorf("decode body: %v", err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	p := newProcessor()
	p.record(BucketKey{Filepath: "agent.go", Lineno: 42, Function: "CallLLM", Model: "gpt-4o"}, 400, 500, 9600, 0, 0, "")

	if err := Push(p, "aight_test_key", srv.URL); err != nil {
		t.Fatalf("Push: %v", err)
	}

	row := sent[0]
	if row["cache_read_tokens"] != float64(9600) || row["cache_creation_tokens"] != float64(0) {
		t.Fatalf("row must carry the cache counts, got %#v", row)
	}
	// input_tokens stays the *uncached* count. Folding the cache read in here is
	// what would double-bill it: the platform sums four rates, not one rate with
	// a discount.
	if row["input_tokens"] != float64(400) {
		t.Fatalf("input_tokens must stay uncached, got %#v", row["input_tokens"])
	}
}

func TestTracedLLMCallInfoAccumulatesCacheCountsAndLatency(t *testing.T) {
	CostProcessor.Clear()
	TracedLLMCallInfo("gpt-4o", 400, 500, CallInfo{
		CacheReadTokens:     9600,
		CacheCreationTokens: 3000,
		LatencyMs:           250,
	})

	snap := CostProcessor.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("expected 1 bucket, got %d", len(snap))
	}
	for _, b := range snap {
		if b.InputTokens != 400 || b.CacheReadTokens != 9600 || b.CacheCreationTokens != 3000 {
			t.Fatalf("got input=%d cacheRead=%d cacheCreation=%d, want 400/9600/3000",
				b.InputTokens, b.CacheReadTokens, b.CacheCreationTokens)
		}
		if b.LatencyMs != 250 {
			t.Fatalf("got latency %v, want 250", b.LatencyMs)
		}
	}
}

func TestLatencyIsOmittedFromTheRowWhenNobodyMeasuredIt(t *testing.T) {
	// Omitted rather than sent as 0. The server stores whatever arrives, and the
	// Workspace reads a stored 0 as a real — impossibly fast — measurement,
	// where an absent field shows as "—".
	send := func(latencyMs float64) map[string]any {
		var sent []map[string]any
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_ = json.NewDecoder(r.Body).Decode(&sent)
			w.WriteHeader(http.StatusOK)
		}))
		defer srv.Close()

		p := newProcessor()
		p.record(BucketKey{Filepath: "agent.go", Lineno: 1, Function: "f", Model: "gpt-4o"},
			100, 50, 0, 0, latencyMs, "")
		if err := Push(p, "aight_test_key", srv.URL); err != nil {
			t.Fatalf("Push: %v", err)
		}
		return sent[0]
	}

	if _, present := send(0)["latency_ms"]; present {
		t.Fatalf("an unmeasured latency must not be sent as a number")
	}
	if got := send(120)["latency_ms"]; got != float64(120) {
		t.Fatalf("want 120 on the wire, got %#v", got)
	}
}

func TestOneLineCallingTwoModelsKeepsTwoBuckets(t *testing.T) {
	// The model is part of the bucket key. Merging them summed both models'
	// tokens under whichever name was recorded last, and the server then
	// repriced every cheap call at the expensive model's rate.
	p := newProcessor()
	site := BucketKey{Filepath: "agent.go", Lineno: 42, Function: "CallLLM"}

	for _, model := range []string{"gpt-4o-mini", "gpt-4o"} {
		key := site.Site()
		key.Model = model
		p.record(key, 1000, 1000, 0, 0, 0, "")
	}

	snap := p.Snapshot()
	if len(snap) != 2 {
		t.Fatalf("expected 2 buckets, one per model, got %d", len(snap))
	}
	for k, b := range snap {
		if b.InputTokens != 1000 {
			t.Fatalf("%s: expected only its own 1000 tokens, got %d", k.Model, b.InputTokens)
		}
	}
}

func callSite() {
	TracedLLMCall("gpt-4o-mini", 180, 60) // <-- this exact line should be recorded
}

func TestPushSendsNoCostClaim(t *testing.T) {
	// The price table lives in one place: the backend recomputes the dollar
	// figure for every row at receive, from the model and the four token counts.
	// An SDK-side copy drifted from the server's, and a row naming a model the
	// SDK knew and the server had dropped stopped being repriced. So the field
	// is gone from the wire entirely, and the shape below is the whole contract
	// — the row carries what the server prices from, and no figure of its own.
	var sent []map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&sent); err != nil {
			t.Errorf("decode body: %v", err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	CostProcessor.Clear()
	callSite()
	if err := Push(CostProcessor, "aight_test_key", srv.URL); err != nil {
		t.Fatalf("Push: %v", err)
	}

	row := sent[0]
	if row["model"] != "gpt-4o-mini" || row["input_tokens"] != float64(180) || row["output_tokens"] != float64(60) {
		t.Fatalf("the server prices from model + tokens, so all three must travel: %#v", row)
	}
	keys := make([]string, 0, len(row))
	for k := range row {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	const want = "cache_creation_tokens,cache_read_tokens,calls,chain,input_tokens,model,output_tokens,trace_id"
	if got := strings.Join(keys, ","); got != want {
		t.Fatalf("the row must carry exactly what the server prices from, nothing more:\n got %s\nwant %s", got, want)
	}
}

func TestTracedLLMCallAttributesToCallingLineNotThisFile(t *testing.T) {
	CostProcessor.Clear()
	callSite()

	snap := CostProcessor.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("expected exactly 1 bucket, got %d", len(snap))
	}
	for k, b := range snap {
		if strings.HasSuffix(k.Filepath, "tracing.go") {
			t.Fatalf("attributed to the SDK's own file: %s", k.Filepath)
		}
		if !strings.HasSuffix(k.Filepath, "aight_test.go") {
			t.Fatalf("expected aight_test.go, got %s", k.Filepath)
		}
		if k.Function != "callSite" {
			t.Fatalf("expected function callSite, got %s", k.Function)
		}
		if b.Calls != 1 {
			t.Fatalf("expected 1 call, got %d", b.Calls)
		}
	}
}

func TestReportFormat(t *testing.T) {
	CostProcessor.Clear()
	TracedLLMCall("gpt-4o-mini", 180, 60)
	report := CostProcessor.Report()
	re := regexp.MustCompile(`aight_test\.go:\d+ \(\w+\)( \[[\w.\-]+\])? = \d+ call\(s\), \d+ in / \d+ out`)
	if !re.MatchString(report) {
		t.Fatalf("report didn't match expected format: %q", report)
	}
}

func callSiteWithTrace() {
	// snippet-marker: both assertions below read this exact line's window
	TracedLLMCallInfo("gpt-4o", 400, 500, CallInfo{TraceID: "run-abc"})
}

func TestPushCarriesTraceIDAndTheSnippetAroundTheAttributedLine(t *testing.T) {
	// The trace id is what gives the backend an explicit spend/event match
	// instead of its time window; the snippet is what the Line review panel
	// renders. Sending neither left the Workspace's "not captured" state
	// permanent, however good the attribution underneath was.
	var sent []map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&sent); err != nil {
			t.Errorf("decode body: %v", err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	CostProcessor.Clear()
	callSiteWithTrace()
	if err := Push(CostProcessor, "aight_test_key", srv.URL); err != nil {
		t.Fatalf("Push: %v", err)
	}

	row := sent[0]
	if row["trace_id"] != "run-abc" {
		t.Fatalf("row must carry the run's trace_id, got %#v", row["trace_id"])
	}
	chain, _ := row["chain"].([]any)
	frame, _ := chain[0].(map[string]any)
	snippet, _ := frame["snippet"].(string)
	if !strings.Contains(snippet, "snippet-marker") {
		t.Fatalf("frame must carry the source around the attributed line, got %q", snippet)
	}
	start, _ := frame["snippet_start"].(float64)
	if start < 1 || start > frame["lineno"].(float64) {
		t.Fatalf("snippet_start %v must be 1-indexed and no later than the attributed line %v",
			start, frame["lineno"])
	}
}

func TestPushEventPostsASingleObjectWithTraceID(t *testing.T) {
	// /api/ingest/events is the one ingest endpoint that takes a single object
	// rather than an array, and the server 400s any row missing event_id,
	// event_name or timestamp — so each default here is load-bearing.
	var body []byte
	var headers http.Header
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ = io.ReadAll(r.Body)
		headers = r.Header.Clone()
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ingested":1,"results":[{"match_type":"EXPLICIT"}]}`))
	}))
	defer srv.Close()

	resp, err := PushEvent("checkout.completed", 49.0, EventOptions{
		TraceID: "run-abc",
		APIKey:  "aight_test_key",
		URL:     srv.URL,
	})
	if err != nil {
		t.Fatalf("PushEvent: %v", err)
	}

	var event map[string]any
	if err := json.Unmarshal(body, &event); err != nil {
		t.Fatalf("body must be a single JSON object, got %s: %v", body, err)
	}
	if event["event_name"] != "checkout.completed" || event["value"] != 49.0 || event["trace_id"] != "run-abc" {
		t.Fatalf("unexpected event: %#v", event)
	}
	if event["currency"] != "USD" {
		t.Fatalf("currency must default to USD, got %#v", event["currency"])
	}
	if id, _ := event["event_id"].(string); id == "" {
		t.Fatalf("event_id must be generated when the caller passes none, got %#v", event["event_id"])
	}
	if ts, _ := event["timestamp"].(float64); ts <= 0 {
		t.Fatalf("timestamp must default to now in Unix seconds, got %#v", event["timestamp"])
	}
	if headers.Get("X-Aight-Sdk-Language") != "go" || headers.Get("X-Aight-Sdk-Version") != SDKVersion {
		t.Fatalf("SDK headers missing from the events post: %#v", headers)
	}
	if resp["ingested"] != float64(1) {
		t.Fatalf("must return the decoded response body, got %#v", resp)
	}
}

func TestCaptureSnippetTakesAWindowAndClampsAtTheFileEdges(t *testing.T) {
	path := filepath.Join(t.TempDir(), "src.go")
	content := "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}

	// Three lines either side, clamped at the top, and one trailing newline
	// dropped so the panel doesn't render a phantom last line.
	captureSnippet(path, 2)
	s, ok := snippetFor(path, 2)
	if !ok {
		t.Fatal("expected a snippet for line 2")
	}
	if s.Start != 1 || s.Text != "line 1\nline 2\nline 3\nline 4\nline 5" {
		t.Fatalf("got start=%d text=%q", s.Start, s.Text)
	}

	// Clamped at the bottom too — a short tail must not add blank lines.
	captureSnippet(path, 10)
	s, _ = snippetFor(path, 10)
	if s.Start != 7 || s.Text != "line 7\nline 8\nline 9\nline 10" {
		t.Fatalf("got start=%d text=%q", s.Start, s.Text)
	}
}

func TestCaptureSnippetSkipsAnUnreadableFile(t *testing.T) {
	// Deployed without source, a path from another machine, a stdin frame: the
	// capture degrades to "not captured". It is never worth raising over.
	const missing = "/nonexistent/aight/no-such-file.go"
	captureSnippet(missing, 10)
	if _, ok := snippetFor(missing, 10); ok {
		t.Fatal("an unreadable file must leave no snippet behind")
	}
}

func TestPushValueClearsTheAccumulatorOnceTheServerAcknowledges(t *testing.T) {
	// ReportValue only ever adds to the accumulator and nothing drained it, so
	// passing it to PushValue — which is what the docs tell you to do — re-sent
	// every earlier earning on each flush. /api/ingest/value is a plain INSERT
	// with no dedup, so the same closed deal was counted again and again, and
	// the inflation was permanent.
	pushes := 0
	var sent [][]map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var rows []map[string]any
		_ = json.NewDecoder(r.Body).Decode(&rows)
		sent = append(sent, rows)
		pushes++
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	ClearValue()
	ReportValue(49)

	for i := 0; i < 2; i++ {
		if err := PushValue("aight_test_key", srv.URL); err != nil {
			t.Fatalf("PushValue: %v", err)
		}
	}

	if pushes != 1 {
		t.Fatalf("the second push had nothing left, so it must not send: %d pushes", pushes)
	}
	if sent[0][0]["value_usd"] != float64(49) {
		t.Fatalf("unexpected value on the wire: %#v", sent[0])
	}
}

func TestPushValueKeepsTheAccumulatorWhenThePushFails(t *testing.T) {
	// Clearing is only safe after an acknowledged push — losing unsent earnings
	// to a network blip is worse than the double-count it prevents.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	ClearValue()
	ReportValue(49)
	if err := PushValue("aight_test_key", srv.URL); err == nil {
		t.Fatal("expected a 500 to come back as an error")
	}
	if len(ValueSnapshot()) == 0 {
		t.Fatal("a failed push must not discard unsent earnings")
	}
}

func autoInstrumentedCallSite(client *http.Client) {
	req, _ := http.NewRequest(http.MethodPost, "https://api.openai.com/v1/chat/completions", strings.NewReader(`{"model":"gpt-4o","stream":false}`))
	resp, err := client.Do(req)
	if err == nil {
		_, _ = io.ReadAll(resp.Body)
		_ = resp.Body.Close()
	}
}

func TestTracedHTTPClientRecordsTheCallsModelAndTokens(t *testing.T) {
	// Go cannot patch a provider's client at runtime, so this wrapper is the
	// whole of its auto-instrumentation: the caller hands it to the provider
	// SDK, and the usage the provider's own client parses is read off the way
	// past. Two conventions, and getting them the wrong way round double-bills
	// the cached tokens permanently — so both are covered here.
	body := `{"model":"gpt-4o",
		"usage":{"prompt_tokens":700,"completion_tokens":40,
		         "prompt_tokens_details":{"cached_tokens":600}}}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
	defer srv.Close()

	CostProcessor.Clear()
	client := TracedHTTPClient(ProviderOpenAI)
	// Same client, pointed at the fixture server: the transport is what matters.
	client.Transport = &tracingTransport{provider: ProviderOpenAI, base: &redirectTransport{to: srv.URL}}

	autoInstrumentedCallSite(client)

	snap := CostProcessor.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("expected 1 bucket, got %d", len(snap))
	}
	for k, b := range snap {
		if k.Model != "gpt-4o" {
			t.Fatalf("expected the model off the response, got %q", k.Model)
		}
		if !strings.HasSuffix(k.Filepath, "aight_test.go") || k.Function != "autoInstrumentedCallSite" {
			t.Fatalf("the call must attribute to the caller's line, got %s:%d (%s)",
				k.Filepath, k.Lineno, k.Function)
		}
		// 700 total minus the 600 cached: the wire wants the uncached prompt.
		if b.InputTokens != 100 || b.CacheReadTokens != 600 {
			t.Fatalf("got input=%d cacheRead=%d, want 100/600", b.InputTokens, b.CacheReadTokens)
		}
		if b.OutputTokens != 40 {
			t.Fatalf("got output=%d, want 40", b.OutputTokens)
		}
		if b.LatencyMs != 0 {
			t.Fatalf("a wrapper has no caller-measured latency to record, got %v", b.LatencyMs)
		}
	}
}

func TestTracedHTTPClientReadsAnthropicCountsWithoutSubtracting(t *testing.T) {
	// Anthropic's input_tokens is the uncached prompt already, with the cache
	// counts beside it. Subtracting there — or adding the cache read into the
	// prompt, as the OpenAI branch must not do — is what double-bills.
	body := `{"model":"claude-3-5-sonnet-20241022",
		"usage":{"input_tokens":180,"output_tokens":60,
		         "cache_read_input_tokens":500,"cache_creation_input_tokens":20}}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(body))
	}))
	defer srv.Close()

	CostProcessor.Clear()
	client := &http.Client{Transport: &tracingTransport{provider: ProviderAnthropic, base: &redirectTransport{to: srv.URL}}}
	autoInstrumentedCallSite(client)

	snap := CostProcessor.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("expected 1 bucket, got %d", len(snap))
	}
	for _, b := range snap {
		if b.InputTokens != 180 || b.OutputTokens != 60 {
			t.Fatalf("got input=%d output=%d, want 180/60", b.InputTokens, b.OutputTokens)
		}
		if b.CacheReadTokens != 500 || b.CacheCreationTokens != 20 {
			t.Fatalf("got cacheRead=%d cacheCreation=%d, want 500/20", b.CacheReadTokens, b.CacheCreationTokens)
		}
	}
}

func TestTracedHTTPClientRecordsNothingForAnUnpriceableResponse(t *testing.T) {
	// An error body, a streamed chunk, a response that reports no usage: the
	// row the server cannot price is noise, so it is not recorded — and the
	// caller still gets its own response bytes back untouched.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"error":{"message":"rate limited"}}`))
	}))
	defer srv.Close()

	CostProcessor.Clear()
	client := &http.Client{Transport: &tracingTransport{provider: ProviderOpenAI, base: &redirectTransport{to: srv.URL}}}
	req, _ := http.NewRequest(http.MethodPost, "https://api.openai.com/v1/chat/completions", nil)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("the wrapper must not turn a 429 into a transport error: %v", err)
	}
	got, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusTooManyRequests || !strings.Contains(string(got), "rate limited") {
		t.Fatalf("the caller's response must come back intact, got %d %s", resp.StatusCode, got)
	}
	if len(CostProcessor.Snapshot()) != 0 {
		t.Fatal("an unpriceable response must record nothing")
	}
}

func TestAutoInstrumentReturnsEachProviderOnceAndSkipsUnknownNames(t *testing.T) {
	// AutoInstrument decides which providers TracedHTTPClient reads usage from;
	// it patches nothing. Enabling the same provider twice must not report it
	// twice, and an unknown name is skipped rather than fatal.
	instrumentedMu.Lock()
	instrumented = map[string]bool{}
	instrumentedMu.Unlock()

	got := AutoInstrument(ProviderOpenAI, ProviderOpenAI)
	if len(got) != 1 || got[0] != ProviderOpenAI {
		t.Fatalf("expected just [openai], got %#v", got)
	}
	if again := AutoInstrument(ProviderOpenAI); len(again) != 0 {
		t.Fatalf("an already-enabled provider must not be reported twice, got %#v", again)
	}
	if unknown := AutoInstrument("gemini"); len(unknown) != 0 {
		t.Fatalf("an unknown provider must be skipped, got %#v", unknown)
	}
}

// redirectTransport sends every request to one test server, so a client built
// for a real provider endpoint can be pointed at a fixture. Test-only.
type redirectTransport struct{ to string }

func (t *redirectTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	clone := req.Clone(req.Context())
	clone.URL.Scheme, clone.URL.Host = "http", strings.TrimPrefix(t.to, "http://")
	return http.DefaultTransport.RoundTrip(clone)
}

func TestReportValueAttributesToCallingFileAndAccumulates(t *testing.T) {
	ClearValue()
	ReportValue(120)
	ReportValue(30)

	snap := ValueSnapshot()
	if len(snap) != 1 {
		t.Fatalf("expected exactly 1 file, got %d", len(snap))
	}
	for filepath, value := range snap {
		if !strings.HasSuffix(filepath, "aight_test.go") {
			t.Fatalf("expected aight_test.go, got %s", filepath)
		}
		if value != 150 {
			t.Fatalf("expected accumulated value 150, got %v", value)
		}
	}
}
