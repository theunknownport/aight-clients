package aight

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"sync"
)

// Provider names AutoInstrument and TracedHTTPClient understand.
const (
	ProviderOpenAI    = "openai"
	ProviderAnthropic = "anthropic"
)

// knownProviders is what AutoInstrument() enables when called with no names.
var knownProviders = []string{ProviderOpenAI, ProviderAnthropic}

var (
	instrumentedMu sync.Mutex
	instrumented   = map[string]bool{}
)

// AutoInstrument enables tracing for each named provider (default: every known
// one) and returns the providers it enabled this call. Unknown names, and names
// already enabled, are skipped.
//
// Go cannot patch a package at runtime. There is no monkey-patching and no
// in-process hook that swaps a provider's HTTP client out from under it, so
// this is NOT the automatic instrumentation the Python and Node SDKs provide —
// it is opt-in wrapping, and the wrapper is one line you have to write:
//
//	aight.AutoInstrument(aight.ProviderOpenAI)
//	client := openai.NewClient(option.WithHTTPClient(
//		aight.TracedHTTPClient(aight.ProviderOpenAI),
//	))
//
// AutoInstrument on its own changes nothing about your process: it only decides
// which providers TracedHTTPClient will read usage from. Skip the
// option.WithHTTPClient call and no call is traced, silently — there is no
// error to catch, because nothing was ever patched.
//
// A wrapper can only read usage out of a complete, non-streaming response.
// Streamed calls (OpenAI's stream: true, Anthropic's SSE) report usage
// incrementally or not at all, and are not traced: use TracedLLMCallInfo for
// those. Auto-instrumented rows carry no latency and no trace id either — the
// transport never learns the caller's run id. Same reason, same fallback.
func AutoInstrument(providers ...string) []string {
	names := providers
	if len(names) == 0 {
		names = knownProviders
	}

	instrumentedMu.Lock()
	defer instrumentedMu.Unlock()
	enabled := []string{}
	for _, name := range names {
		if name != ProviderOpenAI && name != ProviderAnthropic {
			log.Printf("aight: unknown provider %q, skipping", name)
			continue
		}
		if instrumented[name] {
			continue
		}
		instrumented[name] = true
		enabled = append(enabled, name)
	}
	return enabled
}

// providerResponse is the union of the two providers' non-streaming response
// shapes: enough to read the model and the four token counts off either one
// without branching on which answered. Both providers echo the model in the
// response, so the request body is never parsed.
type providerResponse struct {
	Model string `json:"model"`
	Usage struct {
		// OpenAI-shaped. PromptTokens is the *total* prompt, cached tokens
		// included, which is why record() subtracts the cache read back out.
		PromptTokens     int `json:"prompt_tokens"`
		CompletionTokens int `json:"completion_tokens"`
		PromptDetails    struct {
			CachedTokens int `json:"cached_tokens"`
		} `json:"prompt_tokens_details"`
		// Anthropic-shaped. InputTokens is the uncached prompt already, with the
		// cache counts reported separately beside it.
		InputTokens    int `json:"input_tokens"`
		OutputTokens   int `json:"output_tokens"`
		CacheReadInput int `json:"cache_read_input_tokens"`
		CacheCreation  int `json:"cache_creation_input_tokens"`
	} `json:"usage"`
}

// tracingTransport records every LLM call that passes through it. It sits
// under the provider's own client, so the provider still builds and parses its
// own requests — this only watches the bytes go by.
type tracingTransport struct {
	provider string
	base     http.RoundTripper
}

// TracedHTTPClient returns an *http.Client that records every LLM call made
// through it into CostProcessor, so Push sends them along with everything
// recorded by hand. Pass it to your provider SDK's client constructor — this
// is the half of AutoInstrument that Go cannot do for you.
//
// The returned client has no timeout of its own beyond the standard transport's
// dial and TLS timeouts; set Timeout on it if you want one. It buffers each
// response in order to read the usage out of it, which is fine for the
// non-streaming calls this supports and is not something a streamed call would
// survive anyway.
func TracedHTTPClient(provider string) *http.Client {
	return &http.Client{Transport: &tracingTransport{provider: provider, base: http.DefaultTransport}}
}

func (t *tracingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := t.base.RoundTrip(req)
	if err != nil || resp == nil || resp.Body == nil {
		return resp, err
	}
	// Read the body to read the usage, then hand back the very same bytes: the
	// provider's client still has to parse this response itself. A failed read
	// is not worth raising over — the truncated body is what the connection
	// gave us either way, and tracing must never break the call it is tracing.
	body, readErr := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	resp.Body = io.NopCloser(bytes.NewReader(body))
	if readErr != nil {
		return resp, nil
	}
	t.record(body)
	return resp, nil
}

// record turns one response body into a bucket. Anything it cannot read — an
// error body, a streamed chunk, a provider that reports no usage — records
// nothing at all, which is the correct answer: a row with no model or no tokens
// is one the server cannot price, and a row it cannot price is noise.
func (t *tracingTransport) record(body []byte) {
	var out providerResponse
	if err := json.Unmarshal(body, &out); err != nil || out.Model == "" {
		return
	}

	u := out.Usage
	var input, output, cacheRead, cacheCreation int
	if t.provider == ProviderAnthropic {
		input, output = u.InputTokens, u.OutputTokens
		cacheRead, cacheCreation = u.CacheReadInput, u.CacheCreation
	} else {
		cacheRead = u.PromptDetails.CachedTokens
		// OpenAI's prompt_tokens already contains the cached tokens. Folding
		// them in here would bill every cached token twice — once at the full
		// input rate and again at the cache rate — and the platform freezes
		// cost at receive, so the overcount would be permanent. Clamped at zero
		// in case the provider reports a cache read larger than the prompt it
		// is supposedly inside.
		input = u.PromptTokens - cacheRead
		if input < 0 {
			input = 0
		}
		output = u.CompletionTokens
	}
	if input == 0 && output == 0 && cacheRead == 0 && cacheCreation == 0 {
		return
	}

	// The caller's own line: resolveCallerFrame walks past this package and the
	// net/http stack the transport sits inside, exactly as it walks past the
	// framework code in the other SDKs.
	site := resolveCallerFrame()
	captureSnippet(site.Filepath, site.Lineno)
	key := site.Site()
	key.Model = out.Model
	// No latency — the transport could time the round trip, but a number this
	// SDK measured on its own would be the one claim the Workspace cannot
	// corroborate. And no trace id: the transport never learns the caller's run.
	CostProcessor.record(key, input, output, cacheRead, cacheCreation, 0, "")
}
