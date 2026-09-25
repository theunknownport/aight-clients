package aight

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
)

// SDKVersion must match projects.CURRENT_SDK_VERSIONS["go"] on the backend —
// bump both together on release.
const SDKVersion = "0.1.0"

// thisFile identifies this package's own source file at runtime, so
// resolveCallerFrame can walk past it to find the caller's actual line —
// mirrors python/aight/tracing.py's _resolve_caller_frame.
var thisFile = func() string {
	_, file, _, _ := runtime.Caller(0)
	return file
}()

// sdkDir is this package's own directory. The walk below skips every frame in
// it except a _test.go one, so TracedHTTPClient's transport — a second file of
// ours, sitting between the caller and net/http — is skipped too without
// listing our files by name. The test files are exempt because they are
// callers: Go keeps them in the package directory, and their call sites are
// exactly what should be recorded.
var sdkDir = filepath.Dir(thisFile)

// SnippetContext is how many source lines are grabbed on each side of an
// attributed line for the Workspace's line-review code panel. A fixed small
// window is plenty for "what's on this line" and keeps payloads small — the
// same constant, and the same reasoning, as the Python SDK's _SNIPPET_CONTEXT.
const SnippetContext = 3

// SnippetKey identifies one attributed source line: the (filepath, lineno)
// pair a snippet belongs to.
type SnippetKey struct {
	Filepath string
	Lineno   int
}

// Snippet is the source window captured around one attributed line.
type Snippet struct {
	// Start is the 1-indexed line number Text's first line came from.
	Start int
	Text  string
}

// Snippets maps each attributed line ever seen to its captured source window.
// Push reads it to attach context to each chain frame; an absent entry means
// "not captured" and degrades to the Workspace's existing empty state.
var (
	snippetsMu sync.Mutex
	Snippets   = map[SnippetKey]Snippet{}
)

// snippetFor returns the captured snippet for one line, if there is one.
func snippetFor(filepath string, lineno int) (Snippet, bool) {
	snippetsMu.Lock()
	defer snippetsMu.Unlock()
	s, ok := Snippets[SnippetKey{Filepath: filepath, Lineno: lineno}]
	return s, ok
}

// captureSnippet reads filepath and stores a SnippetContext-line window around
// lineno, once per line. Best-effort: an unreadable file (deployed without
// source, a path from another machine, a stdin frame) is skipped silently — a
// missing snippet degrades to "not captured", it is never worth raising over.
func captureSnippet(filepath string, lineno int) {
	if _, ok := snippetFor(filepath, lineno); ok {
		return
	}
	start := lineno - SnippetContext
	if start < 1 {
		start = 1
	}
	// Read outside the lock: a racing duplicate read is harmless (identical
	// content, last write wins), holding a global mutex across file I/O is not.
	data, err := os.ReadFile(filepath)
	if err != nil {
		return
	}
	// CRLF in, LF out — the panel renders this text, not a terminal.
	lines := strings.Split(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n")
	if start > len(lines) {
		return
	}
	end := lineno + SnippetContext
	if end > len(lines) {
		end = len(lines)
	}
	snippetsMu.Lock()
	Snippets[SnippetKey{Filepath: filepath, Lineno: lineno}] = Snippet{
		Start: start,
		// Split leaves a trailing empty element for a file that ends in a
		// newline, which would join back in as a phantom last line.
		Text: strings.TrimSuffix(strings.Join(lines[start-1:end], "\n"), "\n"),
	}
	snippetsMu.Unlock()
}

// BucketKey identifies one traced (file, line, function, model). The model is
// part of the identity, not a field read off the bucket: a line that calls two
// models stays two buckets, each priced on its own tokens. Merging them summed
// both models' tokens under whichever name was recorded last, and the server
// then repriced every cheap call at the expensive model's rate.
type BucketKey struct {
	Filepath string
	Lineno   int
	Function string
	Model    string
}

// Bucket aggregates calls and tokens for one BucketKey. No cost: only the
// platform prices a call (see TracedLLMCall).
type Bucket struct {
	Calls        int
	InputTokens  int
	OutputTokens int
	// Kept beside InputTokens, never folded into it. The platform sums four
	// rates and freezes the result at receive, so an input count that already
	// contained the cached tokens would bill them twice — once at the full
	// input rate and again at the cache rate — and the overcount could never
	// be corrected.
	CacheReadTokens     int
	CacheCreationTokens int
	// Summed, not averaged: the server divides by Calls when it stores the row.
	LatencyMs float64
	// The last non-empty trace id recorded against this bucket, so an explicit
	// id from the run that produced the spend lands on its row. Empty means the
	// caller never set one, and the server falls back to its time-window match.
	TraceID string
}

// Site returns the key with the model cleared — the "where in the code" half
// of the identity, which is all resolveCallerFrame can determine on its own.
func (k BucketKey) Site() BucketKey {
	k.Model = ""
	return k
}

// CostByLineProcessor aggregates calls and tokens by the (file, line, function)
// that issued them — the attribution half of the picture; the platform
// computes the money half.
type CostByLineProcessor struct {
	mu      sync.Mutex
	Buckets map[BucketKey]*Bucket
}

func newProcessor() *CostByLineProcessor {
	return &CostByLineProcessor{Buckets: make(map[BucketKey]*Bucket)}
}

// CostProcessor is the package-level singleton every TracedLLMCall records into.
var CostProcessor = newProcessor()

func (p *CostByLineProcessor) record(
	key BucketKey, inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens int,
	latencyMs float64, traceID string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	b, ok := p.Buckets[key]
	if !ok {
		b = &Bucket{}
		p.Buckets[key] = b
	}
	b.Calls++
	b.InputTokens += inputTokens
	b.OutputTokens += outputTokens
	b.CacheReadTokens += cacheReadTokens
	b.CacheCreationTokens += cacheCreationTokens
	b.LatencyMs += latencyMs
	// Last non-empty wins, not last-set: a later call that reported no trace id
	// must not blank the id an earlier call on the same line did report.
	if traceID != "" {
		b.TraceID = traceID
	}
}

// Clear resets all recorded buckets — mainly useful between test runs.
func (p *CostByLineProcessor) Clear() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.Buckets = make(map[BucketKey]*Bucket)
}

// Snapshot returns a copy of the current buckets, safe to range over concurrently.
func (p *CostByLineProcessor) Snapshot() map[BucketKey]Bucket {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make(map[BucketKey]Bucket, len(p.Buckets))
	for k, b := range p.Buckets {
		out[k] = *b
	}
	return out
}

// Report renders a plain-text calls-by-line report, busiest line first. No
// dollar figure: this SDK holds no prices, so the honest local report is what
// it actually knows — where the calls happened, and how many tokens each line
// sent. The platform prices them (see TracedLLMCall).
func (p *CostByLineProcessor) Report() string {
	snap := p.Snapshot()
	keys := make([]BucketKey, 0, len(snap))
	for k := range snap {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool { return snap[keys[i]].Calls > snap[keys[j]].Calls })

	lines := make([]string, 0, len(keys))
	for _, k := range keys {
		b := snap[k]
		model := ""
		if k.Model != "" {
			model = fmt.Sprintf(" [%s]", k.Model)
		}
		lines = append(lines, fmt.Sprintf("%s:%d (%s)%s = %d call(s), %d in / %d out",
			filepath.Base(k.Filepath), k.Lineno, k.Function, model, b.Calls, b.InputTokens, b.OutputTokens))
	}
	return strings.Join(lines, "\n")
}

func resolveCallerFrame() BucketKey {
	// Skip resolveCallerFrame and TracedLLMCall's own frames (2), then walk
	// past this file itself in case of extra wrapper frames.
	pc := make([]uintptr, 32)
	n := runtime.Callers(2, pc)
	frames := runtime.CallersFrames(pc[:n])
	for {
		frame, more := frames.Next()
		// Skip this SDK's own frames, and net/http's — a call routed through
		// TracedHTTPClient would otherwise attribute to http.Client.Do rather
		// than to the line that issued it. The skip is on the package path in
		// the function name, so a user package that happens to be named "http"
		// still resolves to its own frame.
		ownSource := filepath.Dir(frame.File) == sdkDir && !strings.HasSuffix(frame.File, "_test.go")
		if !ownSource && !strings.HasPrefix(frame.Function, "net/http.") {
			fn := frame.Function
			if i := strings.LastIndex(fn, "."); i != -1 {
				fn = fn[i+1:] // drop the package/receiver prefix, keep just the function name
			}
			return BucketKey{Filepath: frame.File, Lineno: frame.Line, Function: fn}
		}
		if !more {
			break
		}
	}
	return BucketKey{Filepath: "?", Lineno: 0, Function: "?"}
}

// CallInfo carries everything a caller knows about one call beyond its tokens.
//
// A struct rather than more parameters because the set has grown twice — cache
// counts, then latency — and every addition to a positional signature breaks
// every existing caller. Zero values mean "not reported", which is the honest
// default for all three.
type CallInfo struct {
	// inputTokens must be the *uncached* prompt when these are set. That is the
	// Anthropic convention, where a cache read is reported beside the prompt
	// rather than inside it, and it is what the ingest endpoints expect.
	// OpenAI-shaped usage is the other way round — prompt_tokens already
	// includes cached_tokens — so subtract the cached count out before calling.
	// An inclusive count bills the cached tokens twice, once at full rate and
	// once at the cache rate, and the platform freezes cost at receive.
	CacheReadTokens     int
	CacheCreationTokens int

	// LatencyMs is how long the call took. This SDK cannot measure it: it is
	// called after your LLM call returns, not around it, so there is no span to
	// time. Yours to measure, ours to record. Zero stores as 0, which the
	// Workspace reads as "not measured" rather than as an impossibly fast call.
	LatencyMs float64

	// TraceID ties this spend to the business events emitted during the same
	// run, so the backend matches them explicitly instead of falling back to a
	// time window. Empty means "no run id", which is the correct default for a
	// caller that never had one.
	TraceID string
}

// TracedLLMCall records an LLM call, attributed to the caller's source line, so
// the platform can price it. Nothing is returned, deliberately: the price table
// lives in exactly one place — the backend, which recomputes cost from the model and the
// four token counts at receive time. This SDK ships no price table. A bundled
// one is a second copy that drifts from the server's, and once it did, a row
// naming a model the SDK knew and the server didn't stopped being repriced.
func TracedLLMCall(model string, inputTokens, outputTokens int) {
	TracedLLMCallInfo(model, inputTokens, outputTokens, CallInfo{})
}

// TracedLLMCallInfo is TracedLLMCall for a call that reported cache tokens,
// a duration, a trace id, or any combination of the three.
func TracedLLMCallInfo(model string, inputTokens, outputTokens int, info CallInfo) {
	site := resolveCallerFrame()
	// Grab the source around the line before recording, so Push can send it
	// alongside the chain frame. Best-effort — a miss changes nothing here.
	captureSnippet(site.Filepath, site.Lineno)
	key := site.Site()
	key.Model = model
	CostProcessor.record(
		key, inputTokens, outputTokens, info.CacheReadTokens, info.CacheCreationTokens,
		info.LatencyMs, info.TraceID)
}

// ValueByFile tracks revenue/value earned, attributed to the calling file —
// the same "agent" identity TracedLLMCall uses for spend.
var ValueByFile = struct {
	mu     sync.Mutex
	values map[string]float64
}{values: make(map[string]float64)}

// ReportValue records value this agent earned (a closed deal, a resolved
// ticket, whatever the caller's business counts). Pushed home via PushValue.
func ReportValue(valueUSD float64) {
	key := resolveCallerFrame()
	ValueByFile.mu.Lock()
	defer ValueByFile.mu.Unlock()
	ValueByFile.values[key.Filepath] += valueUSD
}

// ValueSnapshot returns a copy of the current filepath -> value_usd totals.
func ValueSnapshot() map[string]float64 {
	ValueByFile.mu.Lock()
	defer ValueByFile.mu.Unlock()
	out := make(map[string]float64, len(ValueByFile.values))
	for k, v := range ValueByFile.values {
		out[k] = v
	}
	return out
}

// ClearValue resets recorded value — mainly useful between test runs.
func ClearValue() {
	ValueByFile.mu.Lock()
	defer ValueByFile.mu.Unlock()
	ValueByFile.values = make(map[string]float64)
}
