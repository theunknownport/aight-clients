import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  tracedLlmCall,
  COST_PROCESSOR,
  reportValue,
  VALUE_BY_FILE,
  push,
  pushValue,
  pushEvent,
  autoInstrument,
  DEFAULT_INGEST_URL,
} from '../src/index.js'
import { SDK_VERSION } from '../src/remote.js'
import { snippetFor } from '../src/tracing.js'

/** Runs push() with global fetch stubbed, returning the JSON payload it sent. */
async function capturePush() {
  const originalFetch = globalThis.fetch
  let sent = null
  globalThis.fetch = async (_url, init) => {
    sent = JSON.parse(init.body)
    return { ok: true, status: 200, statusText: 'OK' }
  }
  try {
    await push(COST_PROCESSOR, { apiKey: 'aight_test_key' })
  } finally {
    globalThis.fetch = originalFetch
  }
  return sent
}

/** Runs pushEvent() with global fetch stubbed, returning the request it made
 * and the parsed response pushEvent handed back. */
async function capturePushEvent(...args) {
  const originalFetch = globalThis.fetch
  let sent = null
  globalThis.fetch = async (url, init) => {
    sent = { url, body: JSON.parse(init.body), headers: init.headers }
    return {
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ ingested: 1, results: [] }),
    }
  }
  try {
    const result = await pushEvent(...args)
    return { sent, result }
  } finally {
    globalThis.fetch = originalFetch
  }
}

test('tracedLlmCall carries cache tokens onto the bucket', () => {
  COST_PROCESSOR.buckets.clear()
  tracedLlmCall('gpt-4o', 400, 500, 9600, 3000)

  const [entry] = COST_PROCESSOR.entries()
  assert.equal(entry.inputTokens, 400, 'inputTokens stays the uncached count')
  assert.equal(entry.cacheReadTokens, 9600)
  assert.equal(entry.cacheCreationTokens, 3000)
})

test('latency reaches the row when measured, and is absent when it is not', async () => {
  // Absent, not 0: a stored 0 reads in the Workspace as a real, impossibly fast
  // measurement, where a missing key shows as "—".
  COST_PROCESSOR.buckets.clear()
  tracedLlmCall('gpt-4o', 100, 50, 0, 0, 0)
  const [unmeasured] = await capturePush()
  assert.equal('latency_ms' in unmeasured, false)

  COST_PROCESSOR.buckets.clear()
  tracedLlmCall('gpt-4o', 100, 50, 0, 0, 120)
  const [measured] = await capturePush()
  assert.equal(measured.latency_ms, 120)
})

test('push sends the cache counts as their own fields', async () => {
  // The gap this closes: the counts used to stop at the process boundary, so a
  // cache-bearing call under-reported cost through every SDK — against a server
  // that already priced all four rates and froze the result at receive.
  COST_PROCESSOR.buckets.clear()
  tracedLlmCall('gpt-4o', 400, 500, 9600, 3000)

  const [row] = await capturePush()
  assert.equal(row.cache_read_tokens, 9600)
  assert.equal(row.cache_creation_tokens, 3000)
  // Folding the cache read into input_tokens is what would double-bill it.
  assert.equal(row.input_tokens, 400)
})

test('tracedLlmCall attributes to the calling line, not this file', () => {
  COST_PROCESSOR.buckets.clear()
  function callSite() {
    return tracedLlmCall('gpt-4o-mini', 180, 60) // <-- this exact line should be recorded
  }
  assert.equal(callSite(), undefined, 'records the call and returns no price')

  const entries = COST_PROCESSOR.entries()
  assert.equal(entries.length, 1)
  const entry = entries[0]
  assert.ok(entry.filepath.endsWith('index.test.js'))
  assert.equal(entry.function, 'callSite')
  assert.equal(entry.model, 'gpt-4o-mini')
  assert.equal(entry.calls, 1)
  assert.ok(entry.lineno > 0)
})

test('one line calling two models keeps two entries', () => {
  // The model is part of the bucket key. Merging them summed both models'
  // tokens under whichever name was recorded last, and the server then
  // repriced every cheap call at the expensive model's rate.
  COST_PROCESSOR.buckets.clear()

  for (const model of ['gpt-4o-mini', 'gpt-4o']) {
    tracedLlmCall(model, 1000, 1000) // same line both times
  }

  const entries = COST_PROCESSOR.entries()
  assert.equal(entries.length, 2)
  const byModel = Object.fromEntries(entries.map((e) => [e.model, e]))
  assert.deepEqual(Object.keys(byModel).sort(), ['gpt-4o', 'gpt-4o-mini'])
  assert.equal(byModel['gpt-4o-mini'].inputTokens, 1000)
  assert.equal(byModel['gpt-4o'].inputTokens, 1000)
  assert.equal(byModel['gpt-4o'].calls, 1)
  assert.equal(byModel['gpt-4o-mini'].calls, 1)
  assert.equal(byModel['gpt-4o-mini'].lineno, byModel['gpt-4o'].lineno)
})

test('report formats file:line (function) [model] = N call(s), IN in / OUT out', () => {
  // Calls and tokens only. The SDK carries no price table, so there is no $
  // figure it could honestly print.
  COST_PROCESSOR.buckets.clear()
  tracedLlmCall('gpt-4o-mini', 180, 60)
  const report = COST_PROCESSOR.report()
  assert.match(
    report,
    /index\.test\.js:\d+ \([^)]+\) \[gpt-4o-mini\] = 1 call\(s\), 180 in \/ 60 out/
  )
})

test('push sends the chain shape the ingest endpoint requires', async () => {
  // A flat {filepath, lineno, function, calls} row is rejected by the server
  // with "missing 'chain'" — sending one made every push 400.
  COST_PROCESSOR.buckets.clear()
  tracedLlmCall('gpt-4o-mini', 180, 60)

  const sent = await capturePush()

  assert.equal(sent.length, 1)
  const row = sent[0]
  assert.ok(Array.isArray(row.chain) && row.chain.length === 1, 'row must carry a non-empty chain')
  assert.ok(row.chain[0].filepath.endsWith('index.test.js'))
  assert.equal(typeof row.chain[0].lineno, 'number')
  assert.equal(typeof row.chain[0].function, 'string')
  assert.equal(row.calls, 1)
  assert.equal(row.filepath, undefined, 'must not also send the flat shape')
  // Without model + tokens the server can't price the row from its own table,
  // which is the one that decides what customers actually see.
  assert.equal(row.model, 'gpt-4o-mini')
  assert.equal(row.input_tokens, 180)
  assert.equal(row.output_tokens, 60)
})

test('the pushed row carries no price — the platform prices it', async () => {
  // The contract: the SDK names the model and the token counts and sends no
  // price. A bundled table here is a second one, and a second one drifts —
  // which is exactly how a push that named a model this SDK knew and the
  // server's no longer did stopped being repriced.
  COST_PROCESSOR.buckets.clear()
  tracedLlmCall('gpt-4o-mini', 180, 60)

  const [entry] = COST_PROCESSOR.entries()
  assert.equal('cost' in entry, false, 'nothing is priced locally')

  const [row] = await capturePush()
  assert.equal('cost_usd' in row, false, 'nothing is priced on the wire')
  assert.equal(row.model, 'gpt-4o-mini')
  assert.equal(row.input_tokens, 180)
  assert.equal(row.output_tokens, 60)
})

test('push clears the buckets once the server acknowledges it', async () => {
  // The endpoint *adds* each row to what it already holds, so a second push of
  // the same running totals would double-count every call.
  COST_PROCESSOR.buckets.clear()
  tracedLlmCall('gpt-4o-mini', 180, 60)

  await capturePush()

  assert.equal(COST_PROCESSOR.entries().length, 0)
})

test('push carries the trace id, so a later event can match it explicitly', async () => {
  COST_PROCESSOR.buckets.clear()
  tracedLlmCall('gpt-4o-mini', 180, 60, 0, 0, 0, 'trace-abc')
  const [traced] = await capturePush()
  assert.equal(traced.trace_id, 'trace-abc')

  // '' is not a missing field: it tells the server nobody set one, which is
  // what leaves it guessing by time window rather than matching a wrong run.
  COST_PROCESSOR.buckets.clear()
  tracedLlmCall('gpt-4o-mini', 180, 60)
  const [untraced] = await capturePush()
  assert.equal(untraced.trace_id, '')
})

test('a captured source window rides along with the chain frame', async () => {
  COST_PROCESSOR.buckets.clear()
  function snippetCallSite() {
    return tracedLlmCall('gpt-4o-mini', 180, 60) // SNIPPET_WINDOW_MARKER
  }
  snippetCallSite()

  const [{ chain: [frame] }] = await capturePush()

  // The attributed line ±3, so the call site's own source is in the window —
  // including this marker comment on it.
  assert.equal(frame.snippet_start, Math.max(1, frame.lineno - 3))
  assert.match(frame.snippet, /SNIPPET_WINDOW_MARKER/)
  assert.ok(frame.snippet.split('\n').length <= 7)
})

test('an unreadable source file yields no snippet and does not throw', () => {
  // Deployed without source, a path from another machine, a stdin frame: the
  // push still has to go out, just without the window.
  assert.equal(snippetFor('/no/such/aight/file.js', 10), undefined)
})

test('pushEvent sends one event object, not an array', async () => {
  // /spans and /value take arrays; /events takes a single object. Posting a
  // one-element list is the easy mistake, and it is the same payload shape
  // wrapped wrong.
  const { sent, result } = await capturePushEvent('checkout.completed', 49, {
    traceId: 'trace-abc',
    apiKey: 'aight_test_key',
  })

  assert.equal(Array.isArray(sent.body), false)
  assert.equal(sent.body.event_name, 'checkout.completed')
  assert.equal(sent.body.value, 49)
  assert.equal(sent.body.currency, 'USD')
  assert.equal(sent.body.trace_id, 'trace-abc')
  assert.equal(typeof sent.body.event_id, 'string')
  assert.ok(sent.body.event_id.length > 0, 'an event_id is generated when none is passed')
  // Unix seconds, not milliseconds — a ms timestamp matches nothing.
  assert.ok(Math.abs(sent.body.timestamp - Date.now() / 1000) < 5)

  assert.equal(sent.url, DEFAULT_INGEST_URL.replace('/spans', '/events'))
  assert.equal(sent.headers['X-Aight-Sdk-Language'], 'node')
  assert.equal(sent.headers['X-Aight-Sdk-Version'], SDK_VERSION)
  assert.deepEqual(result, { ingested: 1, results: [] })
})

test('pushEvent honours an explicit event id, timestamp and currency', async () => {
  const { sent } = await capturePushEvent('signup', 0, {
    eventId: 'evt_123',
    timestamp: 1758307200.0,
    currency: 'EUR',
    apiKey: 'aight_test_key',
  })

  assert.equal(sent.body.event_id, 'evt_123')
  assert.equal(sent.body.timestamp, 1758307200.0)
  assert.equal(sent.body.currency, 'EUR')
  assert.equal(sent.body.trace_id, '', 'no trace id means no explicit match, not a wrong one')
})

test('reportValue attributes earned value to the calling file, accumulating', () => {
  VALUE_BY_FILE.clear()
  reportValue(120)
  reportValue(30)
  const entries = [...VALUE_BY_FILE.entries()]
  assert.equal(entries.length, 1)
  const [filepath, value] = entries[0]
  assert.ok(filepath.endsWith('index.test.js'))
  assert.equal(value, 150)
})

/** Runs pushValue() with global fetch stubbed, returning every payload it sent. */
async function capturePushValue(map) {
  const originalFetch = globalThis.fetch
  const sent = []
  globalThis.fetch = async (_url, init) => {
    sent.push(JSON.parse(init.body))
    return { ok: true, status: 200, statusText: 'OK' }
  }
  try {
    await pushValue(map, { apiKey: 'aight_test_key' })
  } finally {
    globalThis.fetch = originalFetch
  }
  return sent
}

test('pushValue clears the map once the server acknowledges it', async () => {
  // reportValue() only ever adds to VALUE_BY_FILE and nothing drained it, so
  // passing it to pushValue() — which is what the docs tell you to do —
  // re-sent every earlier earning on each flush. /api/ingest/value is a plain
  // INSERT, so the same closed deal was counted again and again, permanently.
  VALUE_BY_FILE.clear()
  reportValue(49)

  const sent = await capturePushValue(VALUE_BY_FILE)
  assert.equal(sent.length, 1)
  assert.equal(sent[0][0].value_usd, 49)

  const second = await capturePushValue(VALUE_BY_FILE)
  assert.equal(second.length, 0, 'the second push had nothing left, so it must not send')
})

test('pushValue keeps the map when the push fails', async () => {
  // Clearing is only safe after an acknowledged push — losing unsent earnings
  // to a network blip is worse than the double-count it prevents.
  VALUE_BY_FILE.clear()
  reportValue(49)

  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => ({ ok: false, status: 500, statusText: 'Server Error' })
  try {
    await assert.rejects(() => pushValue(VALUE_BY_FILE, { apiKey: 'aight_test_key' }))
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.ok(VALUE_BY_FILE.size > 0, 'a failed push must not discard unsent earnings')
})

test('autoInstrument degrades to an empty list without the OTel packages', async () => {
  // The OTel SDK is optional on purpose: this SDK is dependency-free by
  // default. Asking to instrument without it must return "nothing enabled"
  // rather than throwing into an app's startup path.
  const enabled = await autoInstrument()
  assert.deepEqual(enabled, [])
})
