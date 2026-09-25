// Push locally-traced spend data to your hosted AIght Workspace.
// Uses the built-in fetch (Node >=18) — no HTTP client dependency.
import { randomUUID } from 'node:crypto'

import { snippetFor } from './tracing.js'

export const DEFAULT_INGEST_URL = 'https://api.aight.studio/api/ingest/spans'

// Must match projects.CURRENT_SDK_VERSIONS["node"] on the backend — bump
// both together on release.
export const SDK_VERSION = '0.1.0'

function requireKey(apiKey) {
  const key = apiKey || process.env.AIGHT_API_KEY
  if (!key) {
    throw new Error(
      'No API key. Pass { apiKey }, or set AIGHT_API_KEY (get one from the Integrate tab of your aight.studio dashboard).'
    )
  }
  return key
}

/**
 * POST a JSON body and return the Response. All three ingest endpoints go
 * through here, so all three carry the same headers and the same timeout.
 *
 * On failure it keeps the server's own explanation. `res.ok === false` alone
 * says nothing: the ingest endpoints answer 401/400/402 with a `{"detail": ...}`
 * body saying *why*, and "Unknown or revoked API key" is the difference between
 * re-minting a key in five seconds and an afternoon of reading logs. Mirrors
 * aight.remote._send.
 */
async function send(url, key, payload) {
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${key}`,
      'X-Aight-Sdk-Language': 'node',
      'X-Aight-Sdk-Version': SDK_VERSION,
    },
    body: JSON.stringify(payload),
    // Matches the 10s the other three SDKs use. Without it a black-holing
    // endpoint (proxy, SG misconfig) leaves the agent's shutdown path pending
    // forever instead of failing with an error it can act on.
    signal: AbortSignal.timeout(10_000),
  })
  if (!res.ok) {
    const detail = await res
      .json()
      .then((body) => body?.detail)
      .catch(() => null)
    throw new Error(
      `aight push to ${url} failed: HTTP ${res.status} ${res.statusText}` +
        (detail ? ` — ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : '')
    )
  }
  return res
}

/** One chain frame, plus the captured source window when there is one.
 * Mirrors remote.py's _frame_dict. */
function frameDict(filepath, lineno, fn) {
  const frame = { filepath, lineno, function: fn }
  const snippet = snippetFor(filepath, lineno)
  if (snippet) {
    frame.snippet_start = snippet.start
    frame.snippet = snippet.text
  }
  return frame
}

export async function push(costProcessor, { apiKey, url } = {}) {
  const key = requireKey(apiKey)
  const endpoint = url || process.env.AIGHT_INGEST_URL || DEFAULT_INGEST_URL
  // Every row carries a full call chain (see README.md "Wire protocol").
  // This SDK attributes a call to exactly one line, so that line goes out as a
  // one-frame chain. Sending it flat instead is what the server rejects with
  // "missing 'chain'" — real multi-frame chains need stack walking here, the
  // way the Python SDK does it.
  //
  // No price on the row: this SDK computes none. The row names its model and
  // carries the token counts, and the server prices it from its own table.
  const rows = costProcessor.entries().map((e) => ({
    chain: [frameDict(e.filepath, e.lineno, e.function)],
    calls: e.calls,
    model: e.model,
    input_tokens: e.inputTokens,
    output_tokens: e.outputTokens,
    // Their own counts, never folded into input_tokens. The platform sums four
    // rates and freezes the result at receive, so an inclusive input count
    // would bill the cached tokens twice, permanently.
    cache_read_tokens: e.cacheReadTokens,
    cache_creation_tokens: e.cacheCreationTokens,
    // `undefined` drops the key from JSON.stringify entirely, which is what
    // this wants: an unmeasured call shows as "—" in the Workspace, where a
    // stored 0 would read as a real, impossibly fast measurement.
    latency_ms: e.latencyMs || undefined,
    // Ties this spend to any business event pushed with the same trace_id, so
    // the server matches the two EXPLICITLY instead of guessing by time window.
    // '' means nobody set one — not a match, rather than a wrong one.
    trace_id: e.traceId,
  }))
  if (rows.length === 0) return

  await send(endpoint, key, rows)
  // Only after the server has acknowledged. The ingest endpoint *adds* each row
  // to what it already holds, so re-sending these running totals on a later
  // push would count every call a second time and quietly inflate the
  // AIght Workspace's spend. Clearing on success makes a second push send
  // only what happened since the first.
  // ponytail: no idempotency key, so a push whose response is lost still
  // double-counts on retry. Add a client run-id if that ever bites.
  costProcessor.clear()
}

/**
 * Sends everything reportValue() recorded to /api/ingest/value. Same auth and
 * shape as push(), and the same clear-on-success for the same reason:
 * reportValue() only ever *adds* to the map it hands you, so a second push of
 * the same running totals would insert the same earnings again. The value
 * endpoint is a plain INSERT with no dedup, so that inflation would be
 * permanent.
 */
export async function pushValue(valueByFile, { apiKey, url } = {}) {
  const key = requireKey(apiKey)
  const endpoint = url || process.env.AIGHT_VALUE_INGEST_URL || DEFAULT_INGEST_URL.replace('/spans', '/value')
  const rows = [...valueByFile.entries()].map(([filepath, value]) => ({ filepath, value_usd: value }))
  if (rows.length === 0) return

  await send(endpoint, key, rows)
  // Only after the server has acknowledged. A failed push keeps the map, so a
  // retry can still send it.
  valueByFile.clear()
}

/**
 * Report a business KPI/event (a Stripe checkout, a signup, a support ticket
 * closing, ...) to power the AIght Workspace's fleet view and hybrid ROI
 * attribution. One event is one object: /api/ingest/events takes a single
 * object or a list, unlike the array-shaped /spans and /value endpoints.
 *
 * Pass `traceId` (the same one you gave tracedLlmCall) to tie this event to
 * that run as an EXPLICIT match. Without it the server falls back to its
 * time-window guess, which is the weakest data in the system.
 *
 * Returns the ingest endpoint's JSON response, {"ingested": n, "results": [...]}
 * — each result carries the match_type ("EXPLICIT"/"IMPLICIT"/"UNMATCHED") it
 * resolved to, plus its confidence_score. It is not the match result itself.
 */
export async function pushEvent(
  eventName,
  value = 0,
  { currency = 'USD', eventId, traceId = '', timestamp, apiKey, url } = {}
) {
  const key = requireKey(apiKey)
  const endpoint =
    url || process.env.AIGHT_EVENTS_INGEST_URL || DEFAULT_INGEST_URL.replace('/spans', '/events')
  const event = {
    event_id: eventId || randomUUID(),
    event_name: eventName,
    value,
    currency,
    // Unix *seconds*, float — not milliseconds. A ms timestamp lands ~50,000
    // years out and matches nothing.
    timestamp: timestamp ?? Date.now() / 1000,
    trace_id: traceId,
  }
  const res = await send(endpoint, key, event)
  return res.json()
}
