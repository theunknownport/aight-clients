// Attributes a (simulated) LLM call to the user-code line that issued it,
// walking the call stack past this file and past anything in node_modules
// (a reasonable proxy for "framework code" in Node — your own code is
// essentially never installed there). Mirrors python/aight/tracing.py.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const THIS_FILE = fileURLToPath(import.meta.url)

// V8 stack frame lines look like:
//   "    at functionName (/path/to/file.js:10:15)"
//   "    at /path/to/file.js:10:15"                 (anonymous)
const FRAME_RE = /^\s*at\s+(?:(.+?)\s+\()?(.+?):(\d+):(\d+)\)?$/

function normalizePath(raw) {
  // ESM stack frames report file:// URLs; CJS ones report plain paths.
  return raw.startsWith('file://') ? fileURLToPath(raw) : raw
}

function resolveCallerFrame() {
  const holder = {}
  Error.captureStackTrace(holder, resolveCallerFrame)
  const lines = holder.stack.split('\n').slice(1) // drop the "Error" header line
  for (const line of lines) {
    const m = FRAME_RE.exec(line)
    if (!m) continue
    const [, fn, rawFilepath, lineno] = m
    const filepath = normalizePath(rawFilepath)
    if (filepath === THIS_FILE) continue
    if (filepath.includes('/node_modules/')) continue
    return { filepath, lineno: Number(lineno), function: fn || '<anonymous>' }
  }
  return { filepath: '?', lineno: 0, function: '?' }
}

// How many source lines to grab on each side of an attributed line, for the
// Line review code panel. Not configurable — a fixed small window is plenty for
// "what's on this line" context and keeps payloads small.
const SNIPPET_CONTEXT = 3

// "filepath\x00lineno" -> {start, text}: the first line number in the window and
// the joined source. Populated once per unique line ever seen, read by
// push() to attach source context to each chain frame. Mirrors tracing.py's
// SNIPPETS.
const SNIPPETS = new Map()

function snippetKey(filepath, lineno) {
  return `${filepath}\x00${lineno}`
}

/**
 * Best-effort: if the file isn't readable (deployed without source, a path from
 * a different machine, a stdin frame) just skip it — a missing snippet degrades
 * to the existing "not captured" UI state, and is never worth throwing over.
 * Already-seen lines are never re-read.
 */
function captureSnippet(filepath, lineno) {
  const key = snippetKey(filepath, lineno)
  if (SNIPPETS.has(key)) return
  try {
    const lines = readFileSync(filepath, 'utf8').split(/\r?\n/)
    const start = Math.max(1, lineno - SNIPPET_CONTEXT)
    const window = lines.slice(start - 1, lineno + SNIPPET_CONTEXT)
    if (window.length) {
      SNIPPETS.set(key, { start, text: window.join('\n').replace(/\n+$/, '') })
    }
  } catch {
    // Unreadable file — no snippet, no exception.
  }
}

/** The captured source window for a line, or undefined if there isn't one. */
export function snippetFor(filepath, lineno) {
  return SNIPPETS.get(snippetKey(filepath, lineno))
}

// GenAI semantic conventions have used both key names across versions; check
// both rather than assuming one. Confirm which your installed
// @traceloop/instrumentation-* version actually emits before relying on this.
//
// The third element is a convention, not a name, and the two disagree about
// whether the cached tokens are already inside the input count:
//
//   ...input_tokens   Anthropic-shaped — the uncached prompt only, with the
//                     cache counts reported separately alongside it.
//   ...prompt_tokens  OpenAI-shaped — the *total* prompt, cached tokens
//                     included.
//
// The wire wants the exclusive one, so an inclusive match subtracts the cache
// read back out below. Skipping that bills a cached token twice — once at the
// full input rate, again at the cache rate — and because the platform freezes
// cost at receive the overcount is permanent. Mirrors tracing.py.
const TOKEN_ATTR_CANDIDATES = [
  ['gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', false],
  ['gen_ai.usage.prompt_tokens', 'gen_ai.usage.completion_tokens', true],
]

// Where the cache counts live, when the instrumentation reports them at all.
// Both spellings of the read are Anthropic's and the semconv has not settled on
// one; a missing key means "no caching reported", which is also the normal case
// for a provider that has none or a call that did not use it.
const CACHE_READ_ATTRS = ['gen_ai.usage.cache_read_input_tokens', 'gen_ai.usage.cached_tokens']
const CACHE_CREATION_ATTRS = ['gen_ai.usage.cache_creation_input_tokens']

function firstInt(attrs, keys) {
  for (const key of keys) {
    if (key in attrs) return Number(attrs[key] || 0)
  }
  return 0
}

/**
 * [input, output, cacheRead, cacheCreation] as the wire wants them, or null
 * when the span reported no usage at all. `input` is always the *uncached*
 * prompt by the time this returns — the one guarantee the function exists to
 * make.
 */
function usageFrom(attrs) {
  for (const [inputKey, outputKey, inclusive] of TOKEN_ATTR_CANDIDATES) {
    if (!(inputKey in attrs) || !(outputKey in attrs)) continue
    const cacheRead = firstInt(attrs, CACHE_READ_ATTRS)
    const cacheCreation = firstInt(attrs, CACHE_CREATION_ATTRS)
    const prompt = Number(attrs[inputKey] || 0)
    // Only an inclusive count can contain a cache figure, and only the read
    // one: a cache *write* is tokens being processed for the first time, not a
    // repeat of ones already in the prompt. Clamped at zero because
    // instrumentation reporting a cache count larger than the prompt it is
    // supposedly inside is telling us its own numbers disagree, and a negative
    // token count would be worse than a floor.
    const uncached = inclusive ? Math.max(prompt - cacheRead, 0) : prompt
    return [uncached, Number(attrs[outputKey] || 0), cacheRead, cacheCreation]
  }
  return null
}

class Bucket {
  constructor() {
    this.calls = 0
    this.inputTokens = 0
    this.outputTokens = 0
    // Kept beside inputTokens, never folded into it. The platform sums four
    // rates and freezes the result at receive, so an input count that already
    // contained the cached tokens would bill them twice — once at the full
    // input rate and again at the cache rate — and the overcount could never
    // be corrected.
    this.cacheReadTokens = 0
    this.cacheCreationTokens = 0
    // Summed, not averaged: the bucket divides by calls when it reports.
    // Only ever what the caller measured and passed — this SDK never times a
    // call itself, and remote.js omits the key when nobody did.
    this.latencyMs = 0
    // Last non-empty wins, matching Python: a line that recorded once with a
    // trace id and once without is still the traced run. '' means nobody set
    // one, which the server reads as "no explicit match available".
    this.traceId = ''
  }
}

export class CostByLineProcessor {
  constructor() {
    this.buckets = new Map() // "filepath\x00lineno\x00function\x00model" -> Bucket
  }

  /**
   * Records one call. The model is part of the bucket key, not just a field on
   * the bucket: a line that calls two models stays two buckets, each priced by
   * the server on its own tokens. Merging them summed both models' tokens under
   * whichever name was recorded last, and the server then repriced every cheap
   * call at the expensive model's rate.
   */
  record(
    filepath,
    lineno,
    fn,
    model,
    inputTokens,
    outputTokens,
    cacheReadTokens = 0,
    cacheCreationTokens = 0,
    latencyMs = 0,
    traceId = ''
  ) {
    const key = `${filepath}\x00${lineno}\x00${fn}\x00${model}`
    let bucket = this.buckets.get(key)
    if (!bucket) {
      bucket = new Bucket()
      this.buckets.set(key, bucket)
    }
    bucket.calls += 1
    bucket.inputTokens += inputTokens
    bucket.outputTokens += outputTokens
    bucket.cacheReadTokens += cacheReadTokens
    bucket.cacheCreationTokens += cacheCreationTokens
    bucket.latencyMs += latencyMs
    if (traceId) bucket.traceId = traceId
  }

  /**
   * One entry per (call site, model): {filepath, lineno, function, model,
   * calls, inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens,
   * latencyMs, traceId}. Nothing is priced here.
   */
  entries() {
    return [...this.buckets.entries()].map(([key, bucket]) => {
      const [filepath, lineno, fn, model] = key.split('\x00')
      return { filepath, lineno: Number(lineno), function: fn, model, ...bucket }
    })
  }

  /** Drops everything recorded so far. push() calls this once the server has
   * acknowledged, so a later push sends only what happened since — see the
   * double-counting note in remote.js. Mirrors Go's Clear() and Java's clear(). */
  clear() {
    this.buckets.clear()
  }

  /**
   * The OTel SpanProcessor surface, so autoInstrument() can register this same
   * processor on the TracerProvider it installs: a span an auto-instrumentor
   * emits lands in the buckets a hand-wrapped tracedLlmCall writes to. Mirrors
   * CostByLineProcessor.on_start / on_end in tracing.py.
   *
   * onStart captures the caller's line while the instrumented call is still on
   * the stack; onEnd reads it back with the model and usage the instrumentor
   * attached. A span carrying neither is not a priced call (an HTTP client's, a
   * DB driver's) and is skipped — this processor sees every span in the host
   * process, and recording the rest would fill the Workspace with calls=1 rows
   * that name no model and no tokens.
   */
  onStart(span) {
    // No "already attributed" guard needed: resolveCallerFrame() walks past
    // node_modules, so an instrumentor's own nested spans (an HTTP client's
    // inside the provider's) resolve to the same user line as the outer one.
    const { filepath, lineno, function: fn } = resolveCallerFrame()
    span.setAttribute('code.chain.length', 1)
    span.setAttribute('code.chain.0.filepath', filepath)
    span.setAttribute('code.chain.0.lineno', lineno)
    span.setAttribute('code.chain.0.function', fn)
  }

  onEnd(span) {
    const attrs = span.attributes || {}
    const model = attrs['gen_ai.request.model'] || ''
    const usage = usageFrom(attrs)
    if (!model || !usage) return
    const filepath = attrs['code.chain.0.filepath'] || '?'
    const lineno = Number(attrs['code.chain.0.lineno'] || 0)
    const fn = attrs['code.chain.0.function'] || '?'
    captureSnippet(filepath, lineno)
    this.record(filepath, lineno, fn, model, ...usage, 0, attrs['aight.trace_id'] || '')
  }

  shutdown() {}

  forceFlush() {
    return Promise.resolve()
  }

  /**
   * Plain-text report of what was traced locally, calls first. There is no $
   * figure by design: this SDK carries no price table, so the only honest
   * thing it can show is the call count and the tokens the server will price.
   */
  report() {
    return this.entries()
      .sort((a, b) => b.calls - a.calls)
      .map((e) => {
        const base = e.filepath.split('/').pop()
        const model = e.model ? ` [${e.model}]` : ''
        return `${base}:${e.lineno} (${e.function})${model} = ${e.calls} call(s), ${e.inputTokens} in / ${e.outputTokens} out`
      })
      .join('\n')
  }
}

export const COST_PROCESSOR = new CostByLineProcessor()

/**
 * Record a (simulated) LLM call, attributed to the caller's source line, so the
 * platform can price it. Returns nothing: this SDK carries no price table by
 * design — the server prices every row from its own, and a bundled copy here
 * would be a second table that drifts from it.
 *
 * Pass `cacheReadTokens` / `cacheCreationTokens` for a call that used prompt
 * caching — omitting them under-reports badly, since a cache read is about a
 * tenth of the input rate and a write about 125%.
 *
 * `inputTokens` must be the *uncached* prompt. That is the Anthropic
 * convention, where a cache read is reported beside the prompt rather than
 * inside it, and it is what the ingest endpoints expect. OpenAI-shaped usage is
 * the other way round — `prompt_tokens` already contains `cached_tokens` — so
 * subtract the cached count out before calling. An inclusive count here bills
 * the cached tokens twice, and the platform freezes cost at receive, so the
 * overcount is permanent.
 *
 * `latencyMs` is yours to measure. This SDK does not time the LLM call: it is
 * called after your call returns rather than around it, so there is nothing
 * here to time. Time the call yourself and pass the milliseconds; omit it and
 * no latency is claimed, and the Workspace shows "—" on the Speed axis rather
 * than an impossibly fast number.
 *
 * `traceId` is what upgrades a later pushEvent() from the server's time-window
 * guess to an EXPLICIT match: record your agent run's id here and report the
 * same id on the business event that run produced, and the two are tied
 * together without any inference. Omitting it stores '' — no match, not a wrong
 * one.
 *
 * The source line this attributes to is read once and cached, and its
 * surrounding lines ride along with the next push() as `snippet`. Unreadable
 * source is silently skipped.
 */
export function tracedLlmCall(
  model,
  inputTokens,
  outputTokens,
  cacheReadTokens = 0,
  cacheCreationTokens = 0,
  latencyMs = 0,
  traceId = ''
) {
  const { filepath, lineno, function: fn } = resolveCallerFrame()
  captureSnippet(filepath, lineno)
  COST_PROCESSOR.record(
    filepath,
    lineno,
    fn,
    model,
    inputTokens,
    outputTokens,
    cacheReadTokens,
    cacheCreationTokens,
    latencyMs,
    traceId
  )
}

/** filepath -> total value_usd earned. Reported via reportValue(), pushed via pushValue(). */
export const VALUE_BY_FILE = new Map()

/** Record revenue/value this agent earned, attributed to the calling file (the same "agent" identity tracedLlmCall uses for spend). */
export function reportValue(valueUsd) {
  const { filepath } = resolveCallerFrame()
  VALUE_BY_FILE.set(filepath, (VALUE_BY_FILE.get(filepath) || 0) + valueUsd)
}
