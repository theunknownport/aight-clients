// Auto-instrumentation: registers the OpenTelemetry instrumentation for the
// supported provider SDKs, so every real LLM call is traced without wrapping
// each one in tracedLlmCall(). Mirrors python/aight/auto.py.
//
// Each provider's instrumentation package is optional. Install the one for
// whatever you use — `npm install @traceloop/instrumentation-openai` — and
// anything not installed is skipped rather than raised over, so a caller using
// one provider never has to install the other's.
//
// The OTel packages this needs are also optional, and for the same reason:
// without them nothing can emit a span at all, so there is no partial state
// worth pretending about.
import { COST_PROCESSOR } from './tracing.js'

const INSTRUMENTATIONS = {
  openai: ['@traceloop/instrumentation-openai', 'OpenAIInstrumentation'],
  anthropic: ['@traceloop/instrumentation-anthropic', 'AnthropicInstrumentation'],
}

// Install all three or none: a provider instrumentor with no TracerProvider to
// emit into records nothing, and a provider with no registerInstrumentations
// has nothing to patch with.
const OTEL_PACKAGES = ['@opentelemetry/api', '@opentelemetry/sdk-trace-node', '@opentelemetry/instrumentation']

const INSTALL_HINT =
  'npm install @opentelemetry/api @opentelemetry/sdk-trace-node @opentelemetry/instrumentation'

const _instrumented = new Set()

/**
 * Registers OTel instrumentation for each named provider's client library
 * (default: every known provider), so calls through it are traced with no
 * per-call wrapping. Returns the providers actually instrumented this call.
 *
 * A provider whose instrumentation package isn't installed, or that is already
 * instrumented, is skipped — so it is safe to call unconditionally. Missing
 * OTel packages are not an error either: nothing is instrumented, nothing
 * throws, and the reason is logged. Wrap calls with tracedLlmCall instead.
 *
 * The installed TracerProvider carries aight's own span processor, not an
 * exporter: spans land in the same buckets a hand-wrapped tracedLlmCall writes
 * to, and push() sends them the same way. An app that already registered its
 * own provider keeps it — OTel refuses to replace one and only logs a warning.
 *
 * Auto-instrumented calls carry no latency: the instrumentor times the call but
 * reports no duration attribute, and this SDK does not invent one. They show
 * "—" on the Workspace's Speed axis — see tracedLlmCall if you want a real one.
 */
export async function autoInstrument(providers = null) {
  const names = providers || Object.keys(INSTRUMENTATIONS)
  const wanted = names.filter((name) => !_instrumented.has(name))
  if (wanted.length === 0) return []

  let registerInstrumentations
  try {
    const [api, sdk, instrumentation] = await Promise.all(OTEL_PACKAGES.map((name) => import(name)))
    const provider = new sdk.NodeTracerProvider()
    provider.addSpanProcessor(COST_PROCESSOR)
    provider.register()
    // Referenced so the import isn't tree-shaken away before register() has had
    // its effect; OTel's API module must be loaded for the global to be set.
    void api
    registerInstrumentations = instrumentation.registerInstrumentations
  } catch (err) {
    console.warn(
      `aight: auto-instrumentation needs the OpenTelemetry SDK (${err.message}). ` +
        `Nothing was instrumented — install it and retry: ${INSTALL_HINT}`
    )
    return []
  }

  const enabled = []
  for (const name of wanted) {
    const target = INSTRUMENTATIONS[name]
    if (!target) {
      console.warn(`aight: unknown provider ${JSON.stringify(name)}, skipping`)
      continue
    }
    const [moduleName, className] = target
    let instrumentation
    try {
      const mod = await import(moduleName)
      instrumentation = new mod[className]()
    } catch {
      continue
    }
    registerInstrumentations({ instrumentations: [instrumentation] })
    _instrumented.add(name)
    enabled.push(name)
  }
  return enabled
}
