package aight

import (
	"bytes"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"
)

// DefaultIngestURL is where Push sends data when no url is given and
// AIGHT_INGEST_URL isn't set.
const DefaultIngestURL = "https://api.aight.studio/api/ingest/spans"

// spanFrame is one frame of a row's call chain. The snippet is the source
// window captured around that line, when it could be read — omitted otherwise,
// which the Workspace shows as "not captured".
type spanFrame struct {
	Filepath string `json:"filepath"`
	Lineno   int    `json:"lineno"`
	Function string `json:"function"`
	// 1-indexed line the snippet text starts at; only meaningful with Snippet.
	SnippetStart int    `json:"snippet_start,omitempty"`
	Snippet      string `json:"snippet,omitempty"`
}

// frameFrom builds one chain frame, attaching the captured snippet if there is
// one — mirrors _frame_dict in the Python SDK's remote.py.
func frameFrom(filepath string, lineno int, function string) spanFrame {
	f := spanFrame{Filepath: filepath, Lineno: lineno, Function: function}
	if s, ok := snippetFor(filepath, lineno); ok {
		f.SnippetStart, f.Snippet = s.Start, s.Text
	}
	return f
}

// spanRow is one ingest row: a call chain plus the usage attributed to its
// leaf. The server requires "chain" — a flat (filepath, lineno, function)
// row is rejected with "missing 'chain'" (see README.md "Wire
// protocol"). This SDK attributes a call to exactly one line, so that line
// is sent as a one-frame chain; real multi-frame chains need stack walking
// here, the way the Python SDK does it.
//
// Model and the four token counts go with it, and nothing else: the server
// prices the row from its own table at receive time, and an absent cost is how
// this SDK says "making no claim" rather than "$0.00". Sending a cost from here
// would be a second copy of that table, and a row naming a model the SDK knew
// and the server didn't was priced locally and never repriced.
type spanRow struct {
	Chain        []spanFrame `json:"chain"`
	Calls        int         `json:"calls"`
	Model        string      `json:"model"`
	InputTokens  int         `json:"input_tokens"`
	OutputTokens int         `json:"output_tokens"`
	// Their own counts, never folded into InputTokens. The platform sums four
	// rates and freezes the result at receive, so an input count that already
	// contained the cached tokens would bill them twice, permanently.
	CacheReadTokens     int `json:"cache_read_tokens"`
	CacheCreationTokens int `json:"cache_creation_tokens"`
	// Summed over the bucket's calls; the server divides by `calls`. Absent
	// rather than 0 when the caller never timed the call, so the Workspace
	// shows "—" instead of a real 0ms.
	LatencyMs float64 `json:"latency_ms,omitempty"`
	// Empty when the caller never set one, in which case the backend matches
	// the spend to business events by time window instead of explicitly.
	TraceID string `json:"trace_id"`
}

// Push sends the processor's current buckets to your hosted AIght Workspace.
// apiKey and url may be empty, in which case AIGHT_API_KEY and
// AIGHT_INGEST_URL (or DefaultIngestURL) are used.
func Push(p *CostByLineProcessor, apiKey, url string) error {
	if apiKey == "" {
		apiKey = os.Getenv("AIGHT_API_KEY")
	}
	if apiKey == "" {
		return fmt.Errorf("no API key: pass one, or set AIGHT_API_KEY (get one from the Integrate tab of your aight.studio dashboard)")
	}
	if url == "" {
		url = os.Getenv("AIGHT_INGEST_URL")
	}
	if url == "" {
		url = DefaultIngestURL
	}

	snap := p.Snapshot()
	if len(snap) == 0 {
		return nil
	}
	rows := make([]spanRow, 0, len(snap))
	for k, b := range snap {
		rows = append(rows, spanRow{
			Chain:        []spanFrame{frameFrom(k.Filepath, k.Lineno, k.Function)},
			Calls:        b.Calls,
			Model:        k.Model,
			InputTokens:  b.InputTokens,
			OutputTokens: b.OutputTokens,

			CacheReadTokens:     b.CacheReadTokens,
			CacheCreationTokens: b.CacheCreationTokens,
			LatencyMs:           b.LatencyMs,
			TraceID:             b.TraceID,
		})
	}

	body, err := json.Marshal(rows)
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("X-Aight-Sdk-Language", "go")
	req.Header.Set("X-Aight-Sdk-Version", SDKVersion)

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("aight push failed: %s", resp.Status)
	}
	// Only after the server has acknowledged. The ingest endpoint *adds* each
	// row to what it already holds, so re-sending these running totals on a
	// later push would count every call a second time and quietly inflate
	// the AIght Workspace's spend. Clearing on success makes a second push
	// send only what happened since the first.
	//
	// ponytail: no idempotency key, so a push whose response is lost still
	// double-counts on retry. Add a client run-id if that ever bites.
	p.Clear()
	return nil
}

type valueRow struct {
	Filepath string  `json:"filepath"`
	ValueUSD float64 `json:"value_usd"`
}

// PushValue sends the current ValueSnapshot to /api/ingest/value. Same auth
// and defaulting rules as Push — including the clear-on-success, for the same
// reason: ReportValue only ever *adds* to the value it hands you, so a second
// push of the same running totals would insert the same earnings again.
// /api/ingest/value is a plain INSERT with no dedup, so that inflation would
// be permanent.
func PushValue(apiKey, url string) error {
	if apiKey == "" {
		apiKey = os.Getenv("AIGHT_API_KEY")
	}
	if apiKey == "" {
		return fmt.Errorf("no API key: pass one, or set AIGHT_API_KEY (get one from the Integrate tab of your aight.studio dashboard)")
	}
	if url == "" {
		url = os.Getenv("AIGHT_VALUE_INGEST_URL")
	}
	if url == "" {
		url = DefaultIngestURL[:len(DefaultIngestURL)-len("spans")] + "value"
	}

	snap := ValueSnapshot()
	if len(snap) == 0 {
		return nil
	}
	rows := make([]valueRow, 0, len(snap))
	for filepath, value := range snap {
		rows = append(rows, valueRow{Filepath: filepath, ValueUSD: value})
	}

	body, err := json.Marshal(rows)
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("X-Aight-Sdk-Language", "go")
	req.Header.Set("X-Aight-Sdk-Version", SDKVersion)

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("aight push failed: %s", resp.Status)
	}
	// Only after the server has acknowledged. A failed push keeps the total, so
	// a retry can still send it.
	ClearValue()
	return nil
}

// eventRow is one business event. A single object on the wire, not an array —
// /api/ingest/events is the one ingest endpoint shaped that way.
type eventRow struct {
	EventID   string  `json:"event_id"`
	EventName string  `json:"event_name"`
	Value     float64 `json:"value"`
	Currency  string  `json:"currency"`
	// Unix seconds, fractional — not milliseconds.
	Timestamp float64 `json:"timestamp"`
	TraceID   string  `json:"trace_id"`
}

// EventOptions carries the optional half of PushEvent. Every zero value means
// "use the default": a generated event id, USD, now, no trace id, and the
// default events endpoint (AIGHT_EVENTS_INGEST_URL / AIGHT_API_KEY).
type EventOptions struct {
	EventID  string
	TraceID  string
	Currency string
	APIKey   string
	URL      string
	// Timestamp is Unix seconds. Zero means now.
	Timestamp float64
}

// PushEvent reports a business KPI — a Stripe checkout, a signup, a support
// ticket closed, whatever the caller's business counts — to power the AIght
// Workspace's fleet view and ROI attribution.
//
// Set EventOptions.TraceID to the same id passed to TracedLLMCallInfo for the
// run that produced the event; that is what gives the backend an EXPLICIT
// match instead of its time-window fallback.
//
// Returns the ingest endpoint's decoded JSON response, shaped
// {"ingested": n, "results": [...]} — each entry of "results" carries the
// match_type it resolved to (EXPLICIT/IMPLICIT/UNMATCHED). It is not the match
// result itself.
func PushEvent(eventName string, value float64, opts EventOptions) (map[string]any, error) {
	apiKey := opts.APIKey
	if apiKey == "" {
		apiKey = os.Getenv("AIGHT_API_KEY")
	}
	if apiKey == "" {
		return nil, fmt.Errorf("no API key: pass one, or set AIGHT_API_KEY (get one from the Integrate tab of your aight.studio dashboard)")
	}
	url := opts.URL
	if url == "" {
		url = os.Getenv("AIGHT_EVENTS_INGEST_URL")
	}
	if url == "" {
		url = strings.Replace(DefaultIngestURL, "/spans", "/events", 1)
	}

	eventID := opts.EventID
	if eventID == "" {
		var err error
		if eventID, err = newEventID(); err != nil {
			return nil, err
		}
	}
	currency := opts.Currency
	if currency == "" {
		currency = "USD"
	}
	timestamp := opts.Timestamp
	if timestamp == 0 {
		timestamp = float64(time.Now().UnixNano()) / 1e9
	}

	body, err := json.Marshal(eventRow{
		EventID:   eventID,
		EventName: eventName,
		Value:     value,
		Currency:  currency,
		Timestamp: timestamp,
		TraceID:   opts.TraceID,
	})
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("X-Aight-Sdk-Language", "go")
	req.Header.Set("X-Aight-Sdk-Version", SDKVersion)

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return nil, fmt.Errorf("aight push failed: %s", resp.Status)
	}
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return out, nil
}

// newEventID returns a random RFC 4122 v4 UUID. Hand-rolled rather than pulled
// in: this SDK is stdlib-only, and 16 random bytes plus two version bits is
// the whole algorithm.
func newEventID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("aight: could not generate an event id: %w", err)
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}
